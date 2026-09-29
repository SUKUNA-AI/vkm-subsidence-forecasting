"""Endpoint logic of the VKM API, independent of HTTP (the FastAPI routes and the tests call it).

Flow rules (task §32, CP-19, H-13):

* search/graph backends return IDs only; every object is hydrated from the canonical snapshot (``CanonStore``); an
  ID that the canon does not know is dropped with the warning ``STALE_PROJECTION`` — projection text is never served
  as the object's content;
* rerank candidates are IDs; the text comes only from D's view ``rerank_text``, images only from stored artifacts;
  ≤ 24 text candidates and ≤ 8 images per call, never split into batches;
* write = a plan request in the control plane (worker plans, a human confirms ``plan_sha256``); the canon is never
  written.
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from vkm_corpus.api import images
from vkm_corpus.api.backends import (ArtifactBlobs, ControlPlane, GraphBackend, HybridBackend, RerankBackend,
                                     SearchBackend)
from vkm_corpus.api.canon import QUERYABLE_KINDS, CanonStore, jsonable, kind_of
from vkm_corpus.api.envelope import (API_VERSION, ApiWarning, Envelope, Geometry, Item, ModelInfo, Projection,
                                     Provenance, SourceScope)
from vkm_corpus.api.errors import ApiFailure
from vkm_corpus.contracts import vocab
from vkm_corpus.ids import grammar
from vkm_corpus.ops.worker import ALLOWED_OPTIONS as REPROCESS_OPTIONS

MAX_TEXT_CANDIDATES = 24          # H-13 (listwise text reranker: one call, no splicing)
MAX_VISUAL_CANDIDATES = 8         # H-13
TEXT_TOKEN_BUDGET = 4096          # whole listwise prompt of the text reranker (agent F)
MAX_CANDIDATE_CHARS = 32_768
DEFAULT_PAGE_CHARS, MAX_PAGE_CHARS = 12_000, 60_000
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_IMAGE_SOURCE_BYTES = 30 * 1024 * 1024
ACTIVE_JOB_QUOTA = {"REPROCESS_PAGE": 20, "REPROCESS_SOURCE": 3}
DOC_KINDS = frozenset({"DOCUMENT", "PAGE", "BLOCK", "FIGURE", "TABLE", "FORMULA", "BIBLIOGRAPHY_ENTRY"})
REG_KINDS = frozenset({"SOURCE", "WORK", "AUTHOR", "VENUE"})
# canonical columns shown in the envelope (dropped from the record to avoid two copies)
ENVELOPE_COLUMNS = frozenset({
    "schema_version", "object_kind", "source_sha256", "source_site_scope", "source_site_scope_raw",
    "source_site_scope_mapping", "origin", "pipeline_version", "processing_run_id", "extractor_id",
    "extractor_version", "extraction_generation", "model_id", "model_revision", "models", "config_hash",
    "raw_config_hash", "extraction_signature", "raw_content_sha256", "content_sha256", "raw_artifact_id", "created_at",
    "review_status", "quality_flags", "bbox_x0", "bbox_y0", "bbox_x1", "bbox_y1", "bbox_space", "region_origin",
    "text_layer", "input_ref", "input_sha256", "input_row"})
TEXT_WINDOW_COLUMNS = {"PAGE": ("normalized_text",), "BLOCK": ("text", "normalized_text"),
                       "TABLE": ("raw_output", "normalized_text", "normalized_html"),
                       "BIBLIOGRAPHY_ENTRY": ("text", "normalized_text"), "FORMULA": ("raw_output",)}
IMAGE_COLUMNS = {"PAGE": ("preview_artifact_id", "render_artifact_id"),
                 "FIGURE": ("image_artifact_id", "embedded_image_artifact_id"),
                 "TABLE": ("image_artifact_id",), "FORMULA": ("image_artifact_id",)}
HOST_ROLES = {"CORE": ["vkm-api", "vkm-mcp (read)", "vkm-mcp-admin (write, plan-first)", "neo4j (loopback)",
                       "opensearch (loopback)", "canonical data root"],
              "EDGE": ["vkm-rerank-gateway", "text reranker", "visual reranker", "postgresql control plane"],
              "WORKSTATION": ["pipeline (only producer)", "GLM-OCR", "vkm-cad (stdio)", "vkm-drawio (stdio)"]}


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class Result:
    item: Item | None = None
    items: list[Item] | None = None
    warnings: list[ApiWarning] = field(default_factory=list)
    next_cursor: str | None = None


@dataclass
class ApiDeps:
    canon: CanonStore
    blobs: ArtifactBlobs | None = None
    search: SearchBackend | None = None
    graph: GraphBackend | None = None
    rerank: RerankBackend | None = None
    control: ControlPlane | None = None
    hybrid: HybridBackend | None = None
    nav: Any = None                     # vkm_corpus.navigation.store.NavStore (navigation layer, optional)
    catalogues: Any = None              # vkm_corpus.catalogues.store.CatalogueStore (PUBLIC catalogues, optional)
    topic_retrieval: Any = None         # retrieval of the topic dossier (api.topic.TopicRetrieval); None → hybrid


def _require(dep: Any, name: str, stage: str) -> Any:
    if dep is None:
        raise ApiFailure("DEPENDENCY_UNAVAILABLE", f"{name} is not configured on this API instance", stage=stage,
                         tool=name)
    return dep


def _window(text: str | None, offset: int, max_chars: int) -> tuple[str | None, dict[str, Any]]:
    if text is None:
        return None, {"offset": 0, "chars_total": 0, "truncated": False}
    part = text[offset:offset + max_chars]
    return part, {"offset": offset, "chars_total": len(text), "truncated": offset > 0 or offset + max_chars < len(text)}


class ApiService:
    def __init__(self, deps: ApiDeps) -> None:
        self.deps = deps
        self._topic_cache: dict[str, Any] = {}           # section index of the served NAV build (topic dossier)
        self._search_status: tuple[float, dict[str, Any]] | None = None
        self._snapshot_cache: tuple[str | None, dict[str, str | None], dict[str, dict[str, Any]] | None] = (
            None, {}, None)

    @property
    def canon(self) -> CanonStore:
        return self.deps.canon

    def _cache(self) -> tuple[dict[str, str | None], dict[str, dict[str, Any]] | None]:
        snapshot = self.canon.snapshot_id()
        if self._snapshot_cache[0] != snapshot:              # a new snapshot drops everything derived from the old
            self._snapshot_cache = (snapshot, {}, None)
        return self._snapshot_cache[1], self._snapshot_cache[2]

    def works_of_sources(self, source_ids: Any) -> dict[str, str]:
        """INSTANCE_OF work of each source — the ``work_id`` of its pages and objects (H-16, as E's documents)."""
        works, _foreign = self._cache()
        wanted = {s for s in source_ids if s}
        missing = sorted(wanted - works.keys())
        if missing:
            found = self.canon.primary_work_of_sources(missing)
            works.update({s: found.get(s) for s in missing})
        return {s: works[s] for s in wanted if works.get(s)}                 # type: ignore[misc]

    def foreign_content_of_page(self, page_id: str | None) -> dict[str, Any] | None:
        """``{"work_ids": [...], "unidentified_work": bool}`` if the page carries another work's content (H-16)."""
        works, foreign = self._cache()
        if foreign is None:
            foreign = self.canon.foreign_content_pages()
            self._snapshot_cache = (self._snapshot_cache[0], works, foreign)
        found = foreign.get(page_id or "")
        return {"work_ids": list(found["work_ids"]), "unidentified_work": found["unidentified_work"]} if found else None

    # ================================================================================================ envelopes
    def envelope(self, kind: str, row: dict[str, Any], *, payload_form: str = "NORMALIZED", layer: str = "CANONICAL",
                 flags: list[str] | None = None, projection: Projection | None = None,
                 page: dict[str, Any] | None = None, work_id: str | None = None,
                 copy_counts: dict[str, int] | None = None) -> Envelope:
        snapshot = self.canon.snapshot_id()
        flags = list(flags or [])
        if kind in DOC_KINDS:
            object_id = row.get("object_id") or row.get("page_id")
            page_id = row.get("page_id")
            if kind == "PAGE":
                page = row
            elif page is None and page_id:
                page = self.canon.page_for(page_id)
            geometry = None
            if row.get("bbox_x0") is not None and row.get("bbox_space") not in (None, "NONE"):
                geometry = Geometry(bbox_space=row["bbox_space"], bbox=[row["bbox_x0"], row["bbox_y0"], row["bbox_x1"],
                                                                        row["bbox_y1"]],
                                    unit="pt" if row["bbox_space"] == "PAGE_PT_TL" else "px", origin="TOP_LEFT")
            elif kind == "PAGE" and row.get("width_pt") and row.get("height_pt"):
                geometry = Geometry(bbox_space="PAGE_PT_TL", bbox=[0.0, 0.0, row["width_pt"], row["height_pt"]],
                                    unit="pt", origin="TOP_LEFT")
            flags.append("SCOPE_INHERITED_FROM_SOURCE")
            if work_id is None and row.get("source_id"):
                work_id = self.works_of_sources([row["source_id"]]).get(row["source_id"])
            if self.foreign_content_of_page(page_id):
                flags.append("PAGE_HAS_FOREIGN_CONTENT")
            commit = self.canon.commit_of(row.get("source_id"))
            return Envelope(
                object_id=object_id, object_kind=kind,
                object_version=f"{row['content_sha256']}@{commit}" if commit and row.get("content_sha256") else None,
                source_id=row.get("source_id"), work_id=work_id, page_id=page_id,
                page_index=(page or {}).get("page_index"), page_label=(page or {}).get("printed_page_raw"),
                review_status=row["review_status"], layer=layer, payload_form=payload_form, origin=row.get("origin"),
                text_layer=row.get("text_layer") or (row.get("primary_text_layer") if kind == "PAGE" else None),
                region_origin=row.get("region_origin"),
                processing_status=row.get("page_status") if kind == "PAGE" else None,
                quality_flags=list(row.get("quality_flags") or []), flags=sorted(set(flags)),
                source_scope=SourceScope(raw=row.get("source_site_scope_raw"),
                                         values=list(row.get("source_site_scope") or []),
                                         mapping=row.get("source_site_scope_mapping"), inherited_from_source=True),
                geometry=geometry, provenance=self._doc_provenance(row, object_id),
                canonical_snapshot_id=snapshot, projection=projection)
        if kind in REG_KINDS:
            object_id = row.get("object_id") or row.get({"SOURCE": "source_id", "WORK": "work_id", "AUTHOR":
                                                         "author_id", "VENUE": "venue_id"}[kind])
            scope = None
            source_id = row.get("source_id") if kind == "SOURCE" else None
            if kind == "SOURCE":
                flags.append("REGISTER_NOTES_NOT_EVIDENCE")
                scope = SourceScope(raw=row.get("site_scope_raw"), values=list(row.get("site_scope") or []),
                                    mapping=row.get("site_scope_mapping"), inherited_from_source=False)
                payload_form = "REFERENCE"
            if kind == "WORK":
                work_id = row.get("work_id")
                if row.get("status") == "MERGED_INTO":
                    flags.append("TOMBSTONE")
                if copy_counts and copy_counts.get(work_id, 0) > 1:
                    flags.append("WORK_HAS_MULTIPLE_COPIES")
            if kind == "AUTHOR" and row.get("identity_status") == "NAME_KEY_ONLY":
                flags.append("AUTHOR_NAME_KEY_ONLY")
            commit = self.canon.commit_of("REGISTRY")
            return Envelope(
                object_id=object_id, object_kind=kind,
                object_version=f"{row['content_sha256']}@{commit}" if commit and row.get("content_sha256") else None,
                source_id=source_id, work_id=work_id, review_status=row["review_status"], layer=layer,
                payload_form=payload_form, origin=row.get("origin"),
                processing_status=row.get("register_skip_status") if kind == "SOURCE" else None,
                lifecycle_status=row.get("lifecycle_status") if kind == "SOURCE" else None,
                quality_flags=list(row.get("quality_flags") or []), flags=sorted(set(flags)), source_scope=scope,
                provenance=Provenance(processing_run_id=row.get("processing_run_id"),
                                      pipeline_version=row.get("pipeline_version"),
                                      extractor_id=row.get("extractor_id"),
                                      extractor_version=row.get("extractor_version"),
                                      config_hash=row.get("config_hash"),
                                      source_sha256=row.get("source_sha256") if kind == "SOURCE" else None,
                                      created_at=row.get("created_at"), input_ref=row.get("input_ref"),
                                      input_sha256=row.get("input_sha256"), input_row=row.get("input_row"),
                                      trace=f"/v1/provenance/{object_id}"),
                canonical_snapshot_id=snapshot, projection=projection)
        if kind == "ARTIFACT":
            geometry = None
            if row.get("coordinate_space"):
                geometry = Geometry(bbox_space=row["coordinate_space"], crs_status=row.get("crs_status"))
            page_id = row.get("registered_page_id")
            source_id = row.get("registered_source_id") or (page_id.split(":")[0] if page_id else None)
            return Envelope(object_id=row["artifact_id"], object_kind="ARTIFACT", review_status="NOT_APPLICABLE",
                            layer="ARTIFACT", payload_form=payload_form, source_id=source_id, page_id=page_id,
                            geometry=geometry,
                            provenance=Provenance(processing_run_id=row.get("created_by_run_id"),
                                                  created_at=row.get("created_at")),
                            canonical_snapshot_id=snapshot)
        raise ApiFailure("INVALID_ARGUMENT", f"objects of kind {kind} have no envelope here")

    def _doc_provenance(self, row: dict[str, Any], object_id: str) -> Provenance:
        return Provenance(
            processing_run_id=row.get("processing_run_id"), pipeline_version=row.get("pipeline_version"),
            extractor_id=row.get("extractor_id"), extractor_version=row.get("extractor_version"),
            extraction_generation=row.get("extraction_generation"), model_id=row.get("model_id"),
            model_revision=row.get("model_revision"),
            models=[ModelInfo(role=str(m.get("role")), model_id=str(m.get("model_id")),
                              model_revision=str(m.get("model_revision"))) for m in (row.get("models") or [])],
            config_hash=row.get("config_hash"), source_sha256=row.get("source_sha256"),
            raw_artifact_id=row.get("raw_artifact_id"), created_at=row.get("created_at"),
            trace=f"/v1/provenance/{object_id}")

    @staticmethod
    def record(row: dict[str, Any], *, drop: frozenset[str] = ENVELOPE_COLUMNS) -> dict[str, Any]:
        return jsonable({k: v for k, v in row.items() if k not in drop})

    def _item(self, kind: str, row: dict[str, Any], *, record: dict[str, Any] | None = None, **kw: Any) -> Item:
        return Item(envelope=self.envelope(kind, row, **kw), record=record if record is not None else self.record(row))

    def _get(self, kind: str, object_id: str) -> dict[str, Any]:
        row = self.canon.row(kind, object_id)
        if row is None:
            raise ApiFailure("NOT_FOUND", f"{object_id} is not in the current canonical snapshot",
                             object_id=object_id, hint=self._not_found_hint(kind, object_id))
        return row

    def _not_found_hint(self, kind: str, object_id: str) -> str:
        snap = self.canon.snapshot_id()
        if kind == "SOURCE":
            return f"not a registered source in snapshot {snap} (the register has VKM-SRC-001…VKM-SRC-251)"
        if kind in ("BLOCK", "FIGURE", "TABLE", "FORMULA", "BIBLIOGRAPHY_ENTRY"):
            return (f"not in snapshot {snap}: the object may come from an older extraction (object IDs change with "
                    "a new producer); list the page's current objects with /v1/page/{page_id}")
        return f"not in snapshot {snap}"

    # ================================================================================================ objects
    def get_source(self, source_id: str) -> Result:
        self._check("source", source_id)
        row = self._get("SOURCE", source_id)
        record = self.record(row)
        record["register_notes_are_evidence"] = False
        summary = self.canon.source_summary(source_id)
        record["processing_summary"] = jsonable(summary) if summary else None
        record["work_links"] = jsonable(self.canon.source_links(source_id))
        return Result(item=self._item("SOURCE", row, record=record))

    def list_source_pages(self, source_id: str, from_page: int = 1, to_page: int = 9999, limit: int = 100,
                          cursor: str | None = None) -> Result:
        self._check("source", source_id)
        self._get("SOURCE", source_id)
        if not 1 <= limit <= 500:
            raise ApiFailure("INVALID_ARGUMENT", "limit must be 1…500")
        offset = int(cursor) if cursor and cursor.isdigit() else 0
        rows = self.canon.pages_of_source(source_id, from_page, to_page, limit + 1, offset)
        more = len(rows) > limit
        items = [Item(envelope=self.envelope("PAGE", self._get("PAGE", r["page_id"]), payload_form="REFERENCE"),
                      record=jsonable(r)) for r in rows[:limit]]
        return Result(items=items, next_cursor=str(offset + limit) if more else None)

    def get_work(self, work_id: str) -> Result:
        self._check("work", work_id)
        row = self._get("WORK", work_id)
        record = self.record(row)
        resolved = self.canon.resolve_work(work_id)
        record["resolved_work_id"] = resolved["resolved_work_id"] if resolved else work_id
        record["sources"] = jsonable(self.canon.work_sources(work_id))
        authors = jsonable(self.canon.work_authors(work_id))
        for author in authors:
            author["identity_note"] = "a cluster of one name key (NAME_KEY_ONLY), not an identified person" \
                if author.get("identity_status") == "NAME_KEY_ONLY" else None
        record["authors"] = authors
        counts = self.canon.work_copy_counts([work_id])
        record["work_copy_count"] = counts.get(work_id, 0)
        warnings = []
        if row.get("status") == "MERGED_INTO":
            warnings.append(ApiWarning(code="TOMBSTONE", message=f"{work_id} was merged into "
                                                                 f"{record['resolved_work_id']}"))
        return Result(item=self._item("WORK", row, record=record, copy_counts=counts), warnings=warnings)

    def get_page(self, page_id: str, include: list[str] | None = None, max_chars: int = DEFAULT_PAGE_CHARS,
                 text_offset: int = 0) -> Result:
        self._check("page", page_id)
        if not 1 <= max_chars <= MAX_PAGE_CHARS or text_offset < 0:
            raise ApiFailure("INVALID_ARGUMENT", f"max_chars 1…{MAX_PAGE_CHARS}, text_offset ≥ 0")
        include = include or ["text", "objects"]
        row = self._get("PAGE", page_id)
        record = self.record(row)
        flags: list[str] = []
        if "text" in include:
            text, window = _window(row.get("normalized_text"), text_offset, max_chars)
            record["normalized_text"] = text
            record["text_window"] = window
            if window["truncated"]:
                flags.append("TEXT_TRUNCATED")
        else:
            record.pop("normalized_text", None)
        record["foreign_content"] = self.foreign_content_of_page(page_id)
        if "objects" in include:
            record["objects"] = jsonable(self.canon.objects_on_page(page_id))
        if "blocks" in include:
            record["blocks"] = jsonable(self.canon.blocks_of_page(page_id))
        return Result(item=self._item("PAGE", row, record=record, flags=flags))

    def get_object(self, object_id: str, *, max_chars: int = 20_000) -> Result:
        kind = kind_of(object_id)
        dispatch = {"SOURCE": lambda: self.get_source(object_id), "WORK": lambda: self.get_work(object_id),
                    "PAGE": lambda: self.get_page(object_id), "ARTIFACT": lambda: self.get_artifact(object_id)}
        if kind in dispatch:
            return dispatch[kind]()
        if kind not in ("DOCUMENT", "BLOCK", "FIGURE", "TABLE", "FORMULA", "BIBLIOGRAPHY_ENTRY", "AUTHOR", "VENUE"):
            raise ApiFailure("INVALID_ARGUMENT", f"{kind} objects are not served by this endpoint")
        row = self._get(kind, object_id)
        record = self.record(row)
        flags: list[str] = []
        for column in TEXT_WINDOW_COLUMNS.get(kind, ()):
            if record.get(column) is not None:
                record[column], window = _window(record[column], 0, max_chars)
                if window["truncated"]:
                    record[f"{column}_window"] = window
                    flags.append("TEXT_TRUNCATED")
        if kind == "BIBLIOGRAPHY_ENTRY":
            record["links"] = jsonable(self.canon.entry_links([object_id]).get(object_id, []))
            if row.get("citing_work_is_container"):
                flags.append("CITING_WORK_IS_CONTAINER")
        if kind in ("FIGURE", "TABLE", "FORMULA"):
            record["image"] = {"artifact_id": self._image_artifact_id(kind, row), "endpoint": f"/v1/object/"
                                                                                                f"{object_id}/image"}
        if kind in DOC_KINDS and row.get("page_id"):
            record["foreign_content"] = self.foreign_content_of_page(row["page_id"])
        extra = {"flags": flags} if kind in DOC_KINDS else {}
        return Result(item=self._item(kind, row, record=record, **extra))

    def _check(self, grammar_kind: str, value: str) -> None:
        if not grammar.matches(grammar_kind, value):
            raise ApiFailure("INVALID_ID", f"{value!r} is not a valid {grammar_kind} id",
                             hint=f"pattern {grammar.PATTERNS[grammar_kind]}")

    # ================================================================================================ artifacts
    def get_artifact(self, artifact_id: str) -> Result:
        self._check("artifact", artifact_id)
        row = self._get("ARTIFACT", artifact_id)
        record = jsonable(row)
        record["content_endpoint"] = f"/v1/artifact/{artifact_id}/content"
        return Result(item=Item(envelope=self.envelope("ARTIFACT", row, payload_form="REFERENCE"), record=record))

    def artifact_bytes(self, artifact_id: str, max_bytes: int = MAX_ARTIFACT_BYTES) -> tuple[bytes, dict[str, Any]]:
        self._check("artifact", artifact_id)
        row = self._get("ARTIFACT", artifact_id)
        blobs = _require(self.deps.blobs, "artifact store", "artifact_read")
        data = blobs.read(artifact_id, row.get("storage_relpath"), max_bytes)
        return data, jsonable(row)

    def _image_artifact_id(self, kind: str, row: dict[str, Any]) -> str | None:
        for column in IMAGE_COLUMNS.get(kind, ()):
            if row.get(column):
                return row[column]
        return None

    def object_image(self, object_id: str, max_side: int = images.DEFAULT_MAX_SIDE,
                     fmt: str = "auto") -> tuple[images.PreparedImage, dict[str, Any]]:
        kind = kind_of(object_id)
        if kind not in IMAGE_COLUMNS:
            raise ApiFailure("INVALID_ARGUMENT", f"{kind} objects have no image")
        row = self._get(kind, object_id)
        candidates = [row.get(c) for c in IMAGE_COLUMNS[kind] if row.get(c)]
        if not candidates:
            raise ApiFailure("NO_IMAGE_ARTIFACT", f"{object_id} has no image artifact", object_id=object_id)
        last: ApiFailure | None = None
        for artifact_id in candidates:                    # preview first for pages, crop first for figures
            try:
                data, meta = self.artifact_bytes(artifact_id, MAX_IMAGE_SOURCE_BYTES)
            except ApiFailure as exc:
                if exc.code != "ARTIFACT_NOT_MATERIALIZED":
                    raise
                last = exc
                continue
            try:
                prepared = images.prepare(data, meta["media_type"], max_side, fmt)
            except ApiFailure as exc:
                exc.object_id = exc.object_id or artifact_id
                raise
            region =(0.0, 0.0, row["width_pt"], row["height_pt"]) if kind == "PAGE" and row.get("width_pt") \
                else (row["bbox_x0"], row["bbox_y0"], row["bbox_x1"], row["bbox_y1"]) \
                if row.get("bbox_x0") is not None and row.get("bbox_space") == "PAGE_PT_TL" else None
            info = {"object_id": object_id, "artifact_id": artifact_id, "artifact_kind": meta.get("artifact_kind"),
                    "media_type": prepared.media_type, "px": list(prepared.size),
                    "original_px": list(prepared.original_size),
                    "served_sha256": hashlib.sha256(prepared.data).hexdigest(),
                    "pixel_to_page": images.pixel_to_page(prepared.size, region),
                    "coordinate_space": "PAGE_PT_TL" if region else None}
            return prepared, info
        raise last or ApiFailure("NO_IMAGE_ARTIFACT", f"{object_id} has no stored image", object_id=object_id)

    # ================================================================================================ search
    def _projection_status(self) -> dict[str, Any]:
        """Search build info (cached 30 s) → alias → (build_id, snapshot)."""
        now = time.monotonic()
        if self._search_status is None or now - self._search_status[0] > 30:
            try:
                status = _require(self.deps.search, "opensearch", "opensearch").status()
            except ApiFailure:
                status = {}
            self._search_status = (now, status)
        return self._search_status[1]

    def search(self, query: str, kinds: list[str], filters: dict[str, Any], limit: int, cursor: str | None,
               include_duplicates: bool = False, exact: bool = False) -> Result:
        backend = _require(self.deps.search, "opensearch", "opensearch")
        offset = int(cursor) if cursor and cursor.isdigit() else 0
        request = {"query": query, "kinds": tuple(kinds), "filters": filters, "size": limit, "offset": offset,
                   "include_duplicates": include_duplicates, "exact": exact}
        response = backend.search(request)
        items, warnings = self._search_items(response)
        hits = response.get("hits", [])
        more = len(hits) == limit and offset + limit < 1000
        return Result(items=items, warnings=warnings, next_cursor=str(offset + limit) if more else None)

    def search_hybrid(self, query: str, kinds: list[str], filters: dict[str, Any], limit: int, cursor: str | None,
                      candidates: int = 100, include_duplicates: bool = False, exact: bool = False, *,
                      late: bool | None = None, late_candidates: int = 100,
                      bib_route: bool | None = None, translate: bool | None = None) -> Result:
        """BM25 + dense k-NN fused by RRF, optionally re-scored by late interaction (``vkm_corpus.search.hybrid``);
        hits are hydrated from the canon exactly as in :meth:`search` and carry the per-stage trace. Without the query
        encoder, the vectors build or (with late) the token store the answer is DEPENDENCY_UNAVAILABLE — never BM25
        or RRF results in disguise. ``translate`` adds the query in the other language (NAV term dictionary) as
        extra RRF legs (None → :meth:`_hybrid_translate_default`); without the dictionary the search runs without them
        and says so (a warning when the flag was asked for)."""
        backend = _require(self.deps.hybrid, "hybrid search", "hybrid")
        offset = int(cursor) if cursor and cursor.isdigit() else 0
        use_translation = self._hybrid_translate_default(late) if translate is None else bool(translate)
        expansions, translation = self._query_translation(query) if use_translation else ([], None)
        request = {"query": query, "kinds": tuple(kinds), "filters": filters, "size": limit, "offset": offset,
                   "candidates": candidates, "include_duplicates": include_duplicates, "exact": exact,
                   "late": late, "late_candidates": late_candidates, "bib_route": bib_route}
        if expansions:
            request["expansions"] = tuple(expansions)
        response = backend.search(request)
        dense = (response.get("stages") or {}).get("dense") or {}
        items, warnings = self._search_items(response, extra_built={
            dense.get("build_id"): dense.get("built_from_snapshot_id")}, hybrid=True)
        record = {k: response.get(k) for k in ("fusion", "rrf_k", "candidates", "fused_total", "totals", "stages",
                                               "timings_ms", "late", "late_candidates", "route")}
        record.update({"kinds": list(kinds), "query_sha256": sha256_text(query),
                       "scores_are": "rank-fusion signals of a projection, not evidence"})
        if use_translation:
            record["translation"] = translation
            if translate and translation.get("status") == "UNAVAILABLE":
                warnings.append(ApiWarning(code="TRANSLATION_UNAVAILABLE",
                                           message=str(translation.get("reason"))[:200]))
        envelope = Envelope(object_id=f"hybrid-{sha256_text(query)[:16]}", object_kind="SEARCH_RESULT",
                            review_status="NOT_APPLICABLE", layer="SERVICE", payload_form="NORMALIZED",
                            provenance=Provenance(model_id=dense.get("model_key"), model_revision=None),
                            canonical_snapshot_id=self.canon.snapshot_id())
        more = offset + limit < min(int(response.get("fused_total") or 0), candidates * len(kinds))
        return Result(item=Item(envelope=envelope, record=jsonable(record)), items=items, warnings=warnings,
                      next_cursor=str(offset + limit) if more else None)

    def _search_items(self, response: dict[str, Any], *, extra_built: dict[Any, Any] | None = None,
                      hybrid: bool = False) -> tuple[list[Item], list[ApiWarning]]:
        """Hydrate search hits (IDs only) from the canonical snapshot; projection text never becomes content."""
        hits = response.get("hits", [])
        hydrated = self.canon.hydrate([h["id"] for h in hits])
        snapshot = self.canon.snapshot_id()
        aliases = (self._projection_status().get("aliases") or {})
        built = {entry.get("build_id"): entry.get("built_from_snapshot_id") for entry in aliases.values()
                 if isinstance(entry, dict)}
        built.update({k: v for k, v in (extra_built or {}).items() if k})
        warnings: list[ApiWarning] = []
        stale = [h["id"] for h in hits if h["id"] not in hydrated]
        if stale:
            warnings.append(ApiWarning(code="STALE_PROJECTION", message="index hits missing from the canonical "
                                                                        "snapshot were dropped", count=len(stale)))
        mismatched = 0
        nav_numbers = self._nav_equation_numbers(
            oid for oid, (k, row) in hydrated.items() if k == "FORMULA" and not row.get("equation_label"))
        works = self.works_of_sources(row.get("source_id") for _k, row in hydrated.values())
        copy_counts = self.canon.work_copy_counts(works.values())
        items: list[Item] = []
        seen_works: dict[str, int] = {}
        for hit in hits:
            if hit["id"] not in hydrated:
                continue
            kind, row = hydrated[hit["id"]]
            built_from = built.get(hit.get("build_id"))
            matches = None if built_from is None else built_from == snapshot
            if matches is False:
                mismatched += 1
            projection = Projection(engine="opensearch", index_or_graph=hit.get("index"), build_id=hit.get("build_id"),
                                    built_from_snapshot_id=built_from, matches_canonical_snapshot=matches)
            work_id = hit.get("work_id") or works.get(row.get("source_id"))
            if work_id:
                seen_works[work_id] = seen_works.get(work_id, 0) + 1
            ordered_blocks = sorted(hit.get("best_blocks") or [], key=lambda b: (
                b.get("reading_order") is None, b.get("reading_order"), b.get("id")))
            record = {"rank": hit.get("rank"), "bm25_score": hit.get("score"), "rrf_score": hit.get("rrf_score"),
                      "rank_in_kind": hit.get("rank_in_kind"), "object_type": hit.get("object_type"),
                      "highlights": list(hit.get("highlights") or [])[:3],
                      "highlight_origin": "SEARCH_INDEX (projection; not the object's text)",
                      "best_blocks": [{"id": b.get("id"), "reading_order": b.get("reading_order")}
                                      for b in ordered_blocks],
                      "duplicates": list(hit.get("duplicates") or []), "work_id": work_id,
                      "foreign_content": self.foreign_content_of_page(row.get("page_id")),
                      "work_copy_count": copy_counts.get(work_id) if work_id else None,
                      "title_or_caption": _title(kind, row),
                      **(object_label(kind, row, nav_numbers.get(hit["id"])) or {}),
                      "rerank_candidate": {"candidate_id": hit.get("page_id") or hit["id"],
                                           "object_ids": [b.get("id") for b in ordered_blocks] or [hit["id"]],
                                           "rule": "rerank_text_v1"}}
            if hybrid:
                trace = dict(hit.get("trace") or {})
                record.update({"bm25_score": trace.get("bm25_score"), "rrf_score": trace.get("rrf_score"),
                               "dense_score": trace.get("dense_score"), "late_score": trace.get("late_score"),
                               "rank_in_kind": None, "trace": trace})
                unit = trace.get("dense_unit") or {}
                if not ordered_blocks and hit.get("object_type") == "PAGE" and unit.get("object_ids"):
                    record["rerank_candidate"]["object_ids"] = list(unit["object_ids"])[:20]
            items.append(Item(envelope=self.envelope(kind, row, payload_form="REFERENCE", projection=projection,
                                                     work_id=work_id), record=jsonable(record)))
        if mismatched:
            warnings.append(ApiWarning(code="PROJECTION_BUILD_MISMATCH",
                                       message="the index was built from another canonical snapshot",
                                       count=mismatched))
        repeated = {w: n for w, n in seen_works.items() if n > 1 or copy_counts.get(w, 0) > 1}
        if repeated:
            warnings.append(ApiWarning(code="WORK_HAS_MULTIPLE_COPIES",
                                       message="several hits or registered files belong to one work: copies are not "
                                               "independent evidence", count=len(repeated)))
        for code in response.get("warnings") or []:
            warnings.append(ApiWarning(code="SEARCH_WARNING", message=str(code)[:200]))
        return items, warnings

    def _nav_equation_numbers(self, formula_ids: Any) -> dict[str, str]:
        """formula id → equation number of the navigation layer, for formulas the canon gives no label (best effort:
        an unavailable NAV layer only means no fallback label)."""
        ids = sorted(set(formula_ids))
        nav = self.deps.nav
        if not ids or nav is None or not hasattr(nav, "query"):
            return {}
        try:
            ph = ", ".join("?" for _ in ids)
            rows = nav.query(f"SELECT formula_id, equation_number FROM formula_context WHERE formula_id IN ({ph}) "
                             "AND equation_number IS NOT NULL", ids)
        except Exception:  # noqa: BLE001 - NavUnavailable, missing table: the label stays unknown
            return {}
        return {r["formula_id"]: str(r["equation_number"]) for r in rows if r.get("equation_number")}

    def query_objects(self, kinds: list[str], limit: int, cursor: str | None, **filters: Any) -> Result:
        offset = int(cursor) if cursor and cursor.isdigit() else 0
        if not kinds or any(k not in QUERYABLE_KINDS for k in kinds):
            raise ApiFailure("INVALID_ARGUMENT", f"kinds must be values of {QUERYABLE_KINDS}")
        items: list[Item] = []
        for kind in kinds:
            rows = self.canon.query_objects(kind, limit=limit + 1, offset=offset, **filters)
            for row in rows[:limit]:
                record = {k: v for k, v in self.record(row).items()
                          if k not in ("text", "normalized_text", "raw_output", "normalized_html", "cells")}
                record.update(object_label(kind, row) or {})
                items.append(Item(envelope=self.envelope(kind, row, payload_form="REFERENCE"), record=record))
        items.sort(key=lambda it: it.envelope.object_id)
        more = len(items) > limit
        return Result(items=items[:limit], next_cursor=str(offset + limit) if more else None)

    # ================================================================================================ rerank
    async def rerank_text(self, query: str, candidate_ids: list[str], top_n: int | None,
                          passages: dict[str, list[str]], request_id: str,
                          run_sync: Callable[..., Any]) -> Result:
        backend = _require(self.deps.rerank, "text reranker", "rerank_text")
        if len(candidate_ids) > MAX_TEXT_CANDIDATES:
            raise ApiFailure("PAYLOAD_TOO_LARGE", f"at most {MAX_TEXT_CANDIDATES} text candidates per call (H-13); "
                                                  "batches are not spliced", details={"given": len(candidate_ids)})
        if len(set(candidate_ids)) != len(candidate_ids):
            raise ApiFailure("INVALID_ARGUMENT", "candidate ids must be unique")
        wanted = set(candidate_ids) | {o for ids in passages.values() for o in ids}
        texts = await run_sync(self.canon.rerank_texts, wanted)
        hydrated = await run_sync(self.canon.hydrate, candidate_ids)
        sent: list[tuple[str, str]] = []
        used: dict[str, list[str]] = {}
        rejected = []
        for cid in candidate_ids:
            parts = passages.get(cid) or [cid]
            chunks = [texts[o]["text"] for o in parts if o in texts and texts[o].get("text")]
            if cid not in hydrated:
                rejected.append({"id": cid, "code": "NOT_FOUND"})
            elif not chunks:
                rejected.append({"id": cid, "code": "NO_RERANK_TEXT"})
            else:
                sent.append((cid, "\n".join(chunks)[:MAX_CANDIDATE_CHARS]))
                used[cid] = [o for o in parts if o in texts]
        if not sent:
            raise ApiFailure("NO_RERANK_TEXT", "no candidate has canonical text (view rerank_text)",
                             details={"rejected": rejected})
        per_candidate = max(16, min(512, (TEXT_TOKEN_BUDGET - 256) // len(sent)))
        response = await backend.rerank_text(query, sent, top_n, request_id, per_candidate)
        text_of = dict(sent)
        items = []
        for result in response.get("results", []):
            cid = result["id"]
            kind, row = hydrated[cid]
            record = {"rank": result["rank"], "score": result["score"],
                      "input_text_sha256": result.get("input_text_sha256"),
                      "sent_text_sha256": sha256_text(text_of[cid]), "text_chars": result.get("text_chars"),
                      "text_char_range": result.get("text_char_range"), "truncated": result.get("truncated"),
                      "n_tokens": result.get("n_tokens"), "passage_object_ids": used.get(cid, []),
                      "rule": "rerank_text_v1"}
            items.append(Item(envelope=self.envelope(kind, row, payload_form="REFERENCE"), record=jsonable(record)))
        return Result(item=self._service_item("text", response, request_id, rejected), items=items)

    async def rerank_visual(self, query: str, candidate_ids: list[str], top_n: int | None, request_id: str,
                            run_sync: Callable[..., Any], max_side: int = images.DEFAULT_MAX_SIDE) -> Result:
        backend = _require(self.deps.rerank, "visual reranker", "rerank_visual")
        if len(candidate_ids) > MAX_VISUAL_CANDIDATES:
            raise ApiFailure("PAYLOAD_TOO_LARGE", f"at most {MAX_VISUAL_CANDIDATES} images per call (H-13)",
                             details={"given": len(candidate_ids)})
        if len(set(candidate_ids)) != len(candidate_ids):
            raise ApiFailure("INVALID_ARGUMENT", "candidate ids must be unique")
        sent: list[tuple[str, bytes]] = []
        info: dict[str, dict[str, Any]] = {}
        rejected = []
        for cid in candidate_ids:
            try:
                prepared, meta = await run_sync(self._visual_image, cid, max_side)
            except ApiFailure as exc:
                if exc.code in ("INVALID_ID", "NOT_FOUND", "NO_IMAGE_ARTIFACT", "ARTIFACT_NOT_MATERIALIZED",
                                "ARTIFACT_NOT_DECODABLE", "INVALID_ARGUMENT"):
                    rejected.append({"id": cid, "code": "NO_IMAGE_ARTIFACT" if exc.code == "INVALID_ARGUMENT"
                                     else exc.code})
                    continue
                raise
            sent.append((cid, prepared.data))
            info[cid] = meta
        if not sent:
            raise ApiFailure("NO_IMAGE_ARTIFACT", "no candidate has a stored image (text-only candidates are "
                                                  "rejected by the visual reranker)", details={"rejected": rejected})
        response = await backend.rerank_visual(query, sent, top_n, request_id)
        items = []
        for result in response.get("results", []):
            cid = result["id"]
            meta = info[cid]
            record = {"rank": result["rank"], "score": result["score"], "image_artifact_id": meta["artifact_id"],
                      "sent_image_sha256": meta["served_sha256"], "sent_px": meta["px"],
                      "model_image_size_px": result.get("image_size_px"), "image_sha256": result.get("image_sha256"),
                      "pixel_sha256": result.get("pixel_sha256"), "image_tokens": result.get("image_tokens")}
            if meta.get("row") is not None:
                envelope = self.envelope(meta["kind"], meta["row"], payload_form="REFERENCE")
            else:
                envelope = self.envelope("ARTIFACT", meta["artifact_row"], payload_form="REFERENCE")
            items.append(Item(envelope=envelope, record=jsonable(record)))
        return Result(item=self._service_item("visual", response, request_id, rejected), items=items)

    def _visual_image(self, cid: str, max_side: int) -> tuple[images.PreparedImage, dict[str, Any]]:
        kind = kind_of(cid)
        if kind == "ARTIFACT":
            data, row = self.artifact_bytes(cid, MAX_IMAGE_SOURCE_BYTES)
            if not str(row.get("media_type", "")).startswith("image/"):
                raise ApiFailure("NO_IMAGE_ARTIFACT", f"{cid} is not an image")
            prepared = images.prepare(data, row["media_type"], max_side)
            return prepared, {"artifact_id": cid, "served_sha256": hashlib.sha256(prepared.data).hexdigest(),
                              "px": list(prepared.size), "row": None, "kind": "ARTIFACT", "artifact_row": row}
        prepared, meta = self.object_image(cid, max_side)
        return prepared, {**meta, "row": self._get(kind, cid), "kind": kind}

    def _service_item(self, kind: str, response: dict[str, Any], request_id: str,
                      rejected: list[dict[str, str]]) -> Item:
        record = {k: response.get(k) for k in ("model_id", "model_revision", "quant", "placement", "backend",
                                               "backend_version", "weights_sha256", "model_config_sha256",
                                               "score_semantics", "license", "n_candidates", "top_n",
                                               "query_sha256", "input_sha256", "n_tokens_total", "latency_ms",
                                               "gateway_version", "created_at", "warnings")}
        record.update({"kind": kind, "candidate_ids": response.get("candidate_ids"), "rejected": rejected,
                       "scores_are": "a retrieval signal of one model/revision/quant, not evidence",
                       "rule": "rerank_text_v1" if kind == "text" else None})
        envelope = Envelope(object_id=f"rerank-{kind}-{request_id}", object_kind="RERANK_RESULT",
                            review_status="NOT_APPLICABLE", layer="SERVICE", payload_form="NORMALIZED",
                            provenance=Provenance(model_id=response.get("model_id"),
                                                  model_revision=response.get("model_revision")),
                            canonical_snapshot_id=self.canon.snapshot_id())
        return Item(envelope=envelope, record=jsonable(record))

    # ================================================================================================ graph
    def neighbors(self, object_id: str, rel_types: list[str] | None, direction: str, limit: int) -> Result:
        kind = kind_of(object_id)
        if direction not in ("in", "out", "both"):
            raise ApiFailure("INVALID_ARGUMENT", "direction is in, out or both")
        if not 1 <= limit <= 200:
            raise ApiFailure("INVALID_ARGUMENT", "limit must be 1…200")
        self._get(kind, object_id)
        edges: list[tuple[str, str, str]] = []          # (neighbor id, relationship, direction)
        projection = None
        if kind in ("PAGE", "BLOCK", "FIGURE", "TABLE", "FORMULA", "BIBLIOGRAPHY_ENTRY"):
            graph = _require(self.deps.graph, "neo4j", "neo4j")
            state = graph.state()
            if state.get("state") != "READY":
                raise ApiFailure("DEPENDENCY_UNAVAILABLE", f"the document graph is {state.get('state', 'unknown')}",
                                 stage="neo4j", tool="neo4j", hint="retry after the rebuild finishes")
            projection = Projection(engine="neo4j", index_or_graph="DocumentLayer", build_id=state.get("build_id"),
                                    built_from_snapshot_id=state.get("built_from_snapshot_id"),
                                    matches_canonical_snapshot=state.get("built_from_snapshot_id") ==
                                    self.canon.snapshot_id())
            page_id = object_id if kind == "PAGE" else object_id.rsplit(":", 1)[0]
            found = graph.page_neighbors(page_id) or {}
            if kind == "PAGE":
                edges += [(found.get("prev_page_id"), "PRECEDES", "in"), (found.get("next_page_id"), "PRECEDES", "out"),
                          (found.get("source_id"), "HAS_PAGE", "in"), (found.get("work_id"), "INSTANCE_OF", "via")]
                edges += [(o.get("id"), o.get("rel"), "out") for o in found.get("objects") or []]
            else:
                own = next((o.get("rel") for o in found.get("objects") or [] if o.get("id") == object_id), None)
                edges.append((page_id, own or "HAS_OBJECT", "in"))
                edges += [(o.get("id"), o.get("rel"), "sibling") for o in found.get("objects") or []
                          if o.get("id") != object_id]
        elif kind == "WORK":
            graph = _require(self.deps.graph, "neo4j", "neo4j")
            for direction_name, rows in (("out", graph.citations(object_id, "cites")),
                                         ("in", graph.citations(object_id, "cited_by"))):
                for r in rows:
                    edges.append((r.get("cited_work_id") or r.get("citing_work_id"), "CITES", direction_name))
            edges += [(s["source_id"], "INSTANCE_OF", "in") for s in self.canon.work_sources(object_id)]
        elif kind == "SOURCE":
            edges += [(link["work_id"], "INSTANCE_OF" if link["link_type"] != "FOREIGN_CONTENT" else
                       "CARRIES_FOREIGN_CONTENT_OF", "out") for link in self.canon.source_links(object_id)
                      if link.get("work_id")]
        else:
            raise ApiFailure("INVALID_ARGUMENT", f"neighbors of {kind} objects are not served")
        wanted = {r.upper() for r in rel_types} if rel_types else None
        edges = [(i, rel, d) for i, rel, d in edges if i and (wanted is None or (rel or "").upper() in wanted)
                 and (direction == "both" or d == direction or d in ("via", "sibling"))]
        hydrated = self.canon.hydrate(i for i, _r, _d in edges)
        items = []
        stale = 0
        for neighbor_id, rel, d in edges[:limit]:
            if neighbor_id not in hydrated:
                stale += 1
                continue
            n_kind, row = hydrated[neighbor_id]
            items.append(Item(envelope=self.envelope(n_kind, row, payload_form="REFERENCE", projection=projection),
                              record={"relationship": rel, "direction": d, "neighbor_of": object_id}))
        warnings = [ApiWarning(code="STALE_PROJECTION", message="graph neighbors missing from the canon were dropped",
                               count=stale)] if stale else []
        return Result(items=items, warnings=warnings)

    # ------------------------------------------------------------------ NAV graph (agent G): paths and neighbourhoods
    # in the Neo4j projection of the navigation layer (vkm_corpus.graph.nav_query); DERIVED, never evidence.
    NAV_GRAPH_FAMILIES = ("concepts", "formulas", "sections", "topics")

    def _nav_graph(self) -> tuple[Any, dict[str, Any]]:
        graph = _require(self.deps.graph, "neo4j", "neo4j")
        if not hasattr(graph, "nav_state"):
            raise ApiFailure("DEPENDENCY_UNAVAILABLE", "the NAV graph is not available on this API instance",
                             stage="nav_graph", tool="neo4j")
        state = graph.nav_state()
        if state.get("state") != "READY":
            raise ApiFailure("DEPENDENCY_UNAVAILABLE", f"the NAV graph is {str(state.get('state', 'unknown')).lower()}",
                             stage="nav_graph", tool="neo4j",
                             hint="vkm-corpus nav graph-load --nav-dir derived/navigation/<snapshot_id>")
        return graph, state

    def _nav_graph_result(self, kind: str, object_id: str, record: dict[str, Any], state: dict[str, Any], *,
                          source_id: str | None = None, page_id: str | None = None) -> Result:
        from vkm_corpus.graph.nav_query import NOTE

        canon_snapshot = self.canon.snapshot_id()
        nav_snapshot = state.get("snapshot_id")
        env = Envelope(object_id=object_id, object_kind=kind, source_id=source_id, page_id=page_id,
                       review_status="AUTO_EXTRACTED_UNREVIEWED", layer="PROJECTION", payload_form="NORMALIZED",
                       origin="DERIVED",
                       projection=Projection(engine="neo4j", index_or_graph="NavigationLayer",
                                             build_id=state.get("run_id"), built_from_snapshot_id=nav_snapshot,
                                             matches_canonical_snapshot=nav_snapshot == canon_snapshot))
        warnings = []
        if nav_snapshot and nav_snapshot != canon_snapshot:
            warnings.append(ApiWarning(code="NAV_SNAPSHOT_BEHIND", message="the NAV graph was built from another "
                                       "canonical snapshot; ids are stable, counts may differ"))
        body = jsonable({**record, "nav_snapshot_id": nav_snapshot, "note": NOTE})
        return Result(item=Item(envelope=env, record=body), warnings=warnings)

    def _nav_resolve_term(self, graph: Any, text: str) -> tuple[list[dict[str, Any]], str]:
        from vkm_corpus.graph.nav_query import rank_terms, term_keys
        from vkm_corpus.navigation.ids import norm_text

        if not text or not text.strip() or len(text) > 200:
            raise ApiFailure("INVALID_ARGUMENT", "a term is 1..200 characters (a phrase in any form or a TRM- id)")
        keys, method = term_keys(text.strip())
        found = rank_terms(graph.nav_find_terms(text.strip(), keys, norm_text(text), 5), text.strip(), keys)
        if not found:
            raise ApiFailure("NOT_FOUND", f"no term of the NAV graph matches {text[:60]!r}", stage="nav_graph",
                             tool="neo4j", hint="try explore_concept or search_sections, or a shorter phrase")
        return found, method

    def nav_graph_paths(self, term_a: str, term_b: str, max_len: int = 4, limit: int = 5,
                        via: list[str] | None = None) -> Result:
        from vkm_corpus.graph.nav_query import MAX_PATH_LEN, PATH_CAP, path_rel_types, shape_paths

        if not 1 <= int(max_len) <= MAX_PATH_LEN:
            raise ApiFailure("INVALID_ARGUMENT", f"max_len is 1..{MAX_PATH_LEN}")
        if not 1 <= int(limit) <= 20:
            raise ApiFailure("INVALID_ARGUMENT", "limit is 1..20")
        bad = sorted(set(via or []) - set(self.NAV_GRAPH_FAMILIES))
        if bad:
            raise ApiFailure("INVALID_ARGUMENT", f"via is a subset of {list(self.NAV_GRAPH_FAMILIES)}",
                             details={"unknown": bad})
        graph, state = self._nav_graph()
        a_found, method = self._nav_resolve_term(graph, term_a)
        b_found = self._nav_resolve_term(graph, term_b)[0]
        a, b = a_found[0], b_found[0]
        rel_types = path_rel_types(list(via) if via else None)
        raw = [] if a["term_id"] == b["term_id"] else graph.nav_paths(a["term_id"], b["term_id"], rel_types,
                                                                        int(max_len), PATH_CAP)
        paths = shape_paths(raw, int(limit)) if raw else []
        record = {"from": a, "to": b, "alternatives": {"from": a_found[1:4], "to": b_found[1:4]},
                  "via": list(via) if via else list(self.NAV_GRAPH_FAMILIES), "relationship_types": rel_types,
                  "max_len": int(max_len), "n_shortest_paths_found": len(raw), "paths": paths,
                  "term_resolution": method}
        if not paths:
            record["hint"] = ("the same term" if a["term_id"] == b["term_id"] else
                              f"no path within {max_len} hops: raise max_len, widen via, or check the alternatives")
        return self._nav_graph_result("NAV_GRAPH_PATHS", f"paths:{a['term_id']}:{b['term_id']}", record, state)

    def nav_graph_neighbourhood(self, node_id: str, depth: int = 1, limit: int = 50) -> Result:
        from vkm_corpus.graph.nav_query import depth2_ids, layer_of, shape_neighbourhood

        if not node_id or len(node_id) > 120 or not re.fullmatch(r"[A-Za-z0-9_:\-]+", node_id):
            raise ApiFailure("INVALID_ARGUMENT", "node_id is a NAV id (SEC-, TRM-, FSY-, FPR-, topic) or a VKM id")
        if int(depth) not in (1, 2):
            raise ApiFailure("INVALID_ARGUMENT", "depth is 1 or 2")
        if not 1 <= int(limit) <= 200:
            raise ApiFailure("INVALID_ARGUMENT", "limit is 1..200")
        graph, state = self._nav_graph()
        raw = graph.nav_neighbourhood(node_id, layer_of(node_id), int(limit))
        if not raw:
            raise ApiFailure("NOT_FOUND", f"{node_id} is not a node of the NAV or DOCUMENT graph", stage="nav_graph",
                             tool="neo4j", object_id=node_id)
        shaped = shape_neighbourhood(raw["node"], raw.get("groups") or [], {}, int(limit))
        if int(depth) == 2:
            mids = depth2_ids(shaped, per_node=min(10, int(limit)))
            second = graph.nav_neighbourhood_2(mids, node_id, 3) if mids else {}
            shaped = shape_neighbourhood(raw["node"], raw.get("groups") or [], second, int(limit))
        node = shaped["node"]
        source_id = raw["node"].get("source_id") if layer_of(node_id) == "DOCUMENT" else None
        page_id = node_id if node.get("kind") == "PAGE" and source_id else None
        return self._nav_graph_result("NAV_GRAPH_NEIGHBOURHOOD", node_id, {"depth": int(depth), **shaped}, state,
                                      source_id=source_id, page_id=page_id)
    # ------------------------------------------------------------------ end NAV graph (agent G)

    # ------------------------------------------------------------------ navigation layer (NAV, derived, not evidence)
    _NAV_NOTE = "navigation layer: derived from the canon without models, AUTO_EXTRACTED_UNREVIEWED, not evidence"

    def _nav_run(self, fn: Callable[[Any], Any]) -> tuple[Any, str | None]:
        from vkm_corpus.navigation.store import NavUnavailable

        nav = self.deps.nav
        if nav is None:
            raise ApiFailure("DEPENDENCY_UNAVAILABLE", "the navigation layer is not configured on this API instance",
                             stage="navigation", tool="nav")
        try:
            return fn(nav), nav.snapshot_id()
        except NavUnavailable as exc:
            raise ApiFailure("DEPENDENCY_UNAVAILABLE", str(exc), stage="navigation", tool="nav",
                             hint="vkm-corpus nav build → nav pack → nav publish") from exc
        except LookupError as exc:
            raise ApiFailure("NOT_FOUND", f"not in the navigation layer: {exc}", stage="navigation",
                             tool="nav") from exc

    def _nav_result(self, kind: str, object_id: str, data: Any, nav_snapshot: str | None, *,
                    source_id: str | None = None, page_id: str | None = None, search: bool = False) -> Result:
        """A NAV answer; a lookup of one object that is not there is NOT_FOUND, a search without hits is an empty
        list (200)."""
        if search and not data:
            data = []
        elif data is None or data == [] or data == {}:
            raise ApiFailure("NOT_FOUND", f"{object_id} is not in the navigation layer", stage="navigation",
                             tool="nav")
        record = data if isinstance(data, dict) else {"items": data}
        record = jsonable({**record, "nav_snapshot_id": nav_snapshot, "note": self._NAV_NOTE})
        canon_snapshot = self.canon.snapshot_id()
        env = Envelope(object_id=object_id, object_kind=kind, source_id=source_id, page_id=page_id,
                       review_status="AUTO_EXTRACTED_UNREVIEWED", layer="PROJECTION", payload_form="NORMALIZED",
                       origin="DERIVED",
                       projection=Projection(engine="navigation", index_or_graph="nav.duckdb", build_id=nav_snapshot,
                                             built_from_snapshot_id=nav_snapshot,
                                             matches_canonical_snapshot=nav_snapshot == canon_snapshot))
        warnings = []
        if nav_snapshot and nav_snapshot != canon_snapshot:
            warnings.append(ApiWarning(code="NAV_SNAPSHOT_BEHIND", message="the navigation layer was built from "
                                       "another canonical snapshot; ids are stable, counts may differ"))
        return Result(item=Item(envelope=env, record=record), warnings=warnings)

    def nav_outline(self, source_id: str) -> Result:
        self._check("source", source_id)
        data, snap = self._nav_run(lambda nav: nav.run("outline", source_id))
        return self._nav_result("NAV_OUTLINE", source_id, data, snap, source_id=source_id)

    def nav_section(self, section_id: str) -> Result:
        if not re.fullmatch(r"SEC-[0-9a-f]{16}", section_id or ""):
            raise ApiFailure("INVALID_ARGUMENT", "section_id is SEC-<16 hex>")
        data, snap = self._nav_run(lambda nav: nav.run("section", section_id))
        src = data.get("source_id") if isinstance(data, dict) else None
        return self._nav_result("NAV_SECTION", section_id, data, snap, source_id=src)

    def nav_sections(self, text: str, source_id: str | None, limit: int) -> Result:
        if source_id:
            self._check("source", source_id)
        data, snap = self._nav_run(lambda nav: nav.search_sections(text, source_id=source_id, limit=limit))
        return self._nav_result("NAV_SECTIONS", f"search:{text[:60]}", data or [], snap, search=True)

    def nav_formula(self, formula_id: str) -> Result:
        self._check("object", formula_id)
        data, snap = self._nav_run(lambda nav: nav.run("formula_context", formula_id))
        src = data.get("source_id") if isinstance(data, dict) else None
        page = data.get("page_id") if isinstance(data, dict) else None
        return self._nav_result("NAV_FORMULA", formula_id, data, snap, source_id=src, page_id=page)

    def nav_formulas(self, concept: str | None, symbol: str | None, source_id: str | None, limit: int) -> Result:
        if not (concept or symbol):
            raise ApiFailure("INVALID_ARGUMENT", "give a concept (words of symbol definitions) or a symbol")
        if symbol and not source_id:
            raise ApiFailure("INVALID_ARGUMENT", "a symbol is looked up inside one source (symbols are not global)")
        if source_id:
            self._check("source", source_id)
        data, snap = self._nav_run(lambda nav: nav.run("find_formulas", concept=concept, symbol=symbol,
                                                       source_id=source_id))
        items = list(data or [])[:limit] if not isinstance(data, dict) else data
        return self._nav_result("NAV_FORMULAS", f"formulas:{concept or symbol}", items, snap, source_id=source_id,
                                search=True)

    def nav_concept(self, term: str, limit: int) -> Result:
        if not term or not term.strip() or len(term) > 200:
            raise ApiFailure("INVALID_ARGUMENT", "term is 1..200 characters")
        data, snap = self._nav_run(lambda nav: nav.run("explore_concept", term, limit=limit))
        return self._nav_result("NAV_CONCEPT", f"concept:{term[:60]}", data, snap)

    # ------------------------------------------------------------------ topics (agent T) and duplicates (agent U)
    def nav_topic(self, topic_id: str) -> Result:
        if not re.fullmatch(r"TOP-[0-9a-f]{16}", topic_id or ""):
            raise ApiFailure("INVALID_ARGUMENT", "topic_id has the form TOP-<16 hex>")
        data, snap = self._nav_run(lambda nav: nav.run("topic", topic_id))
        return self._nav_result("NAV_TOPIC", topic_id, data, snap)

    def nav_topics(self, terms: list[str], limit: int, level: int | None = None) -> Result:
        terms = [t.strip() for t in terms or [] if t and t.strip()]
        if not terms or len(terms) > 5 or any(len(t) > 200 for t in terms):
            raise ApiFailure("INVALID_ARGUMENT", "1..5 terms of 1..200 characters")
        data, snap = self._nav_run(lambda nav: nav.run("find_topics", terms, limit=limit, level=level))
        return self._nav_result("NAV_TOPICS", "topics:" + "|".join(terms)[:60], data, snap, search=True)

    def nav_similar_sections(self, section_id: str, k: int, other_sources_only: bool) -> Result:
        if not re.fullmatch(r"SEC-[0-9a-f]{16}", section_id or ""):
            raise ApiFailure("INVALID_ARGUMENT", "section_id has the form SEC-<16 hex>")
        data, snap = self._nav_run(lambda nav: nav.run("similar_sections", section_id, k=k,
                                                       other_sources_only=other_sources_only))
        return self._nav_result("NAV_SIMILAR_SECTIONS", section_id, data, snap)

    def nav_section_topics(self, section_id: str) -> Result:
        if not re.fullmatch(r"SEC-[0-9a-f]{16}", section_id or ""):
            raise ApiFailure("INVALID_ARGUMENT", "section_id has the form SEC-<16 hex>")
        data, snap = self._nav_run(lambda nav: nav.run("section_topics", section_id))
        return self._nav_result("NAV_SECTION_TOPICS", section_id, data, snap, search=True)

    def nav_copies(self, ref: str, limit: int) -> Result:
        if not ref or len(ref) > 200:
            raise ApiFailure("INVALID_ARGUMENT", "ref is a unit (u1-…), page or block id")
        data, snap = self._nav_run(lambda nav: nav.run("copies_of", ref, limit=limit))
        return self._nav_result("NAV_COPIES", ref, data, snap)

    def nav_source_overlap(self, source_id: str, limit: int) -> Result:
        if not re.fullmatch(r"VKM-SRC-\d{3,}", source_id or ""):
            raise ApiFailure("INVALID_ARGUMENT", "source_id has the form VKM-SRC-NNN")
        data, snap = self._nav_run(lambda nav: nav.run("source_overlap", source_id, limit=limit))
        return self._nav_result("NAV_SOURCE_OVERLAP", source_id, data, snap, source_id=source_id)

    # ------------------------------------------------------------------ parameter candidates (agent P)
    _SCALES = frozenset({"LAB", "MASSIF", "NORMATIVE", "MODEL", "UNKNOWN"})

    def nav_parameters(self, property: str | None, material: str | None, site: str | None,  # noqa: A002
                       scale: str | None, source_id: str | None, limit: int) -> Result:
        if not any((property, material, site, source_id)):
            raise ApiFailure("INVALID_ARGUMENT", "give at least one of property, material, site, source_id")
        if scale is not None and scale.upper() not in self._SCALES:
            raise ApiFailure("INVALID_ARGUMENT", f"scale is one of {sorted(self._SCALES)}")
        if source_id is not None and not re.fullmatch(r"VKM-SRC-\d{3,}", source_id):
            raise ApiFailure("INVALID_ARGUMENT", "source_id has the form VKM-SRC-NNN")
        data, snap = self._nav_run(lambda nav: nav.run("find_parameters", property=property, material=material,
                                                       site=site, scale=scale.upper() if scale else None,
                                                       source_id=source_id, limit=limit))
        key = "|".join(str(x) for x in (property, material, site, scale, source_id) if x)[:60]
        return self._nav_result("NAV_PARAMETERS", f"parameters:{key}", data, snap, source_id=source_id)

    def nav_parameter_summary(self, property: str, material: str | None) -> Result:  # noqa: A002
        if not property or len(property) > 200:
            raise ApiFailure("INVALID_ARGUMENT", "property is 1..200 characters")
        data, snap = self._nav_run(lambda nav: nav.run("parameter_summary", property, material=material))
        return self._nav_result("NAV_PARAMETER_SUMMARY", f"parameter_summary:{property[:40]}", data, snap)

    # ------------------------------------------------------------------ term dictionary (agent TR)
    _TRANSLATE_LANGS = frozenset({"ru", "en", "de"})
    # hybrid ``translate`` when the request does not say (TERM_DICTIONARY_V1, benchmarks/term_dictionary_v1): on with
    # the late stage — the measured configuration (nDCG@10 and R@50 not worse on V and P); off when the late stage does
    # not run (exploratory: the extra legs then dilute same-language queries). VKM_HYBRID_TRANSLATE_DEFAULT (1/0)
    # overrides the constant without a rebuild.
    HYBRID_TRANSLATE_DEFAULT = True

    def _hybrid_translate_default(self, late: bool | None) -> bool:
        import os

        raw = os.environ.get("VKM_HYBRID_TRANSLATE_DEFAULT", "").strip().lower()
        on = {"1": True, "true": True, "on": True, "0": False, "false": False, "off": False}.get(
            raw, self.HYBRID_TRANSLATE_DEFAULT)
        if not on:
            return False
        if late is None:                                      # the backend's late default (VKM_HYBRID_LATE_DEFAULT)
            late = getattr(self.deps.hybrid, "late_default", None)
        if late is None:
            from vkm_corpus.search.hybrid import LATE_DEFAULT

            late = LATE_DEFAULT
        return bool(late)

    def nav_translate(self, term: str, target: str | None = None, limit: int = 10) -> Result:
        """Equivalents of a term in the other languages, its synonyms and abbreviations (NAV ``term_translations``:
        corpus evidence per pair, seed rows REVIEWED_BY_AGENT); a phrase without a pair is translated part by part."""
        if not term or not term.strip() or len(term) > 200:
            raise ApiFailure("INVALID_ARGUMENT", "term is 1..200 characters")
        if target is not None and target not in self._TRANSLATE_LANGS:
            raise ApiFailure("INVALID_ARGUMENT", f"target is one of {sorted(self._TRANSLATE_LANGS)}")
        data, snap = self._nav_run(lambda nav: nav.run("translate_term", term, target=target, limit=limit))
        return self._nav_result("NAV_TRANSLATION", f"translate:{term[:60]}", data, snap)

    def _query_translation(self, query: str) -> tuple[list[str], dict[str, Any]]:
        """The query in the other language for the hybrid ``translate`` flag: (expansions, what happened)."""
        nav = self.deps.nav
        if nav is None:
            return [], {"status": "UNAVAILABLE", "reason": "the navigation layer is not configured"}
        try:
            data = nav.run("translate_query", query)
        except Exception as exc:  # noqa: BLE001 - NavUnavailable, a build without term_translations …
            return [], {"status": "UNAVAILABLE", "reason": f"term dictionary: {type(exc).__name__}"}
        data = data if isinstance(data, dict) else {}
        text = data.get("translation")
        info = {"status": "APPLIED" if text else "NOT_COVERED", "text": text,
                "source_language": data.get("source_language"), "target_language": data.get("target_language"),
                "coverage": data.get("coverage"),
                "terms": [{k: x.get(k) for k in ("span", "translation", "score", "pair_id")}
                          for x in (data.get("terms") or [])[:8]],
                "note": "derived navigation (AUTO_EXTRACTED_UNREVIEWED): query expansion, not evidence"}
        return ([text] if text else []), info
    # ------------------------------------------------------------------ end term dictionary (agent TR)

    # ------------------------------------------------------------------ topic dossier (navigation + catalogues)
    def reconstruct_topic(self, query: str, *, budget_chars: int = 12_000, source_ids: list[str] | None = None,
                          max_sources: int = 10, max_sections: int = 12, max_formulas: int = 10,
                          paraphrases: list[str] | None = None, translate: bool | None = None) -> Result:
        """«От А до Я» on a topic in one call: ranked NAV sections in two tiers (the VKM core and the rest of the
        corpus; hybrid search over ≤ 5 formulations fused by RRF + titles), formulas, figures and tables near the
        hits, the concept, sources with provenance and CITES, the PUBLIC catalogues (processes with evidence records,
        models, conflicts, causal neighbours) and the UNKNOWN gaps — a budgeted, cited map (``api.topic``);
        navigation, not evidence. Parts whose dependency is missing are left out with a warning."""
        from vkm_corpus.api import topic

        query = (query or "").strip()
        if not query or len(query) > 512:
            raise ApiFailure("INVALID_ARGUMENT", "query is 1..512 characters")
        paraphrases = [p.strip() for p in (paraphrases or []) if p and p.strip()]
        if len(paraphrases) > topic.MAX_PARAPHRASES or any(len(p) > 512 for p in paraphrases):
            raise ApiFailure("INVALID_ARGUMENT", f"at most {topic.MAX_PARAPHRASES} paraphrases of ≤ 512 characters")
        if not topic.MIN_BUDGET <= int(budget_chars) <= topic.MAX_BUDGET:
            raise ApiFailure("INVALID_ARGUMENT", f"budget_chars must be {topic.MIN_BUDGET}…{topic.MAX_BUDGET}")
        if not (1 <= max_sources <= 50 and 1 <= max_sections <= 50 and 0 <= max_formulas <= 50):
            raise ApiFailure("INVALID_ARGUMENT", "max_sources and max_sections 1…50, max_formulas 0…50")
        sources = sorted(set(source_ids or []))
        if len(sources) > 20:
            raise ApiFailure("INVALID_ARGUMENT", "at most 20 source ids")
        for sid in sources:
            self._check("source", sid)
        canon_snapshot = self.canon.snapshot_id()          # SNAPSHOT_UNAVAILABLE without a canon: no dossier
        builder = topic.DossierBuilder(self.canon, self.deps.nav, self.deps.catalogues,
                                       topic.make_retrieval(self.deps.topic_retrieval, self.deps.hybrid),
                                       cache=self._topic_cache)
        dossier = builder.build(topic.TopicRequest(query=query, budget_chars=int(budget_chars),
                                                   source_ids=tuple(sources), max_sources=max_sources,
                                                   max_sections=max_sections, max_formulas=max_formulas,
                                                   paraphrases=tuple(paraphrases),
                                                   translate=topic.TRANSLATE_DEFAULT if translate is None
                                                   else bool(translate)))
        proj = dossier.projection or {}
        built_from = proj.get("built_from_snapshot_id")
        envelope = Envelope(
            object_id=topic.dossier_id(query, sources), object_kind="TOPIC_DOSSIER",
            review_status="AUTO_EXTRACTED_UNREVIEWED", layer="PROJECTION", payload_form="NORMALIZED",
            origin="DERIVED", canonical_snapshot_id=canon_snapshot,
            source_id=sources[0] if len(sources) == 1 else None,
            projection=Projection(engine=proj.get("engine", "navigation"), index_or_graph=proj.get("index_or_graph"),
                                  build_id=proj.get("build_id"), built_from_snapshot_id=built_from,
                                  matches_canonical_snapshot=None if built_from is None else
                                  built_from == canon_snapshot))
        warnings = [ApiWarning(code=w["code"], message=w["message"], count=w.get("count")) for w in dossier.warnings]
        return Result(item=Item(envelope=envelope, record=jsonable(dossier.record)), warnings=warnings)

    def citations(self, work_id: str, direction: str, include_unlinked: bool, limit: int) -> Result:
        self._check("work", work_id)
        self._get("WORK", work_id)
        if direction not in ("cites", "cited_by", "both"):
            raise ApiFailure("INVALID_ARGUMENT", "direction is cites, cited_by or both")
        items: list[Item] = []
        if direction in ("cites", "both"):
            entries = self.canon.entries_of_work(work_id, limit)
            links = self.canon.entry_links(e["object_id"] for e in entries)
            full = self.canon.hydrate(e["object_id"] for e in entries)
            for entry in entries:
                entry_links = links.get(entry["object_id"], [])
                if not include_unlinked and not entry_links:
                    continue
                flags = ["CITING_WORK_IS_CONTAINER"] if entry.get("citing_work_is_container") else []
                record = jsonable({**{k: entry[k] for k in ("entry_label", "ordinal_in_list", "parsed_title",
                                                            "parsed_year", "parsed_doi", "citing_work_resolution")},
                                   "direction": "cites", "links": entry_links,
                                   "status": "LINKED" if any(li["match_status"] in vocab.ACCEPTED_MATCH_STATUSES
                                                             for li in entry_links) else
                                   "CANDIDATE" if entry_links else "UNLINKED",
                                   "note": "a citation is not agreement"})
                _k, row = full[entry["object_id"]]
                items.append(Item(envelope=self.envelope("BIBLIOGRAPHY_ENTRY", row, payload_form="REFERENCE",
                                                         flags=flags), record=record))
        if direction in ("cited_by", "both"):
            incoming = self.canon.citations_in(work_id)
            works = self.canon.hydrate(r["citing_work_id"] for r in incoming)
            for r in incoming[:limit]:
                if r["citing_work_id"] not in works:
                    continue
                _k, row = works[r["citing_work_id"]]
                record = jsonable({"direction": "cited_by", "n_citing_entries": r.get("n_citing_entries"),
                                   "n_citing_sources": r.get("n_citing_sources"),
                                   "match_methods": r.get("match_methods"), "rule_version": r.get("rule_version")})
                flags = ["CITING_WORK_IS_CONTAINER"] if r.get("citing_work_is_container") else []
                items.append(Item(envelope=self.envelope("WORK", row, payload_form="REFERENCE", flags=flags),
                                  record=record))
        return Result(items=items)

    # ================================================================================================ provenance
    def provenance(self, object_id: str) -> Result:
        kind = kind_of(object_id)
        if kind not in DOC_KINDS | REG_KINDS:
            raise ApiFailure("INVALID_ARGUMENT", f"provenance is traced for document and registry objects, not {kind}")
        row = self._get(kind, object_id)
        trace = self.canon.provenance(object_id, kind)
        if trace is None:
            raise ApiFailure("NOT_FOUND", f"no provenance for {object_id}", object_id=object_id)
        trace = jsonable(trace)
        search_ok = bool(self.deps.search)
        graph_ok = bool(self.deps.graph)
        projections = [{"engine": "duckdb", "present": True, "deletable_without_canonical_loss": True},
                       {"engine": "opensearch", "configured": search_ok, "deletable_without_canonical_loss": True},
                       {"engine": "neo4j", "configured": graph_ok, "deletable_without_canonical_loss": True}]
        if kind in DOC_KINDS:
            run_models = jsonable(self.canon.run_models(trace.get("processing_run_id")))
            chain = [
                {"step": "RAW_SOURCE", "subject": trace.get("source_id"), "logical_path":
                    f"PRIVATE:{trace.get('source_canonical_path')}" if trace.get("source_canonical_path") else None,
                 "register_sha256": trace.get("register_sha256"),
                 "sha256_matches_register": trace.get("source_sha256_matches_register")},
                {"step": "PROCESSING_RUN", "subject": trace.get("processing_run_id"), "run_kind": trace.get("run_kind"),
                 "status": trace.get("run_status"), "completed": trace.get("run_has_end"),
                 "code_revision": trace.get("code_revision"), "host_role": trace.get("host_role"),
                 "models": run_models},
                {"step": "PRODUCER", "extractor_id": trace.get("extractor_id"),
                 "extractor_version": trace.get("extractor_version"),
                 "extraction_generation": trace.get("extraction_generation"), "models": trace.get("models"),
                 "config_hash": trace.get("config_hash"), "raw_config_hash": trace.get("raw_config_hash"),
                 "extraction_signature": trace.get("extraction_signature")},
                {"step": "RAW_OUTPUT", "subject": trace.get("raw_artifact_id"), "kind": trace.get("raw_artifact_kind"),
                 "materialization": trace.get("raw_materialization"), "retention": trace.get("raw_retention_class"),
                 "all_raw": trace.get("raw_artifacts")},
                {"step": "CANONICAL_COMMIT", "subject": trace.get("commit_id"), "snapshot_id": trace.get("snapshot_id"),
                 "object_version": trace.get("object_version")}]
            interpretability = {
                "what": f"{kind} {object_id} (document layer L1, automatic extraction)",
                "original": {"source_id": trace.get("source_id"), "page_id": trace.get("page_id"),
                             "page_index": trace.get("page_index"), "page_kind": trace.get("page_kind"),
                             "printed_page": trace.get("printed_page_raw"),
                             "bbox": [row.get("bbox_x0"), row.get("bbox_y0"), row.get("bbox_x1"), row.get("bbox_y1")]
                             if row.get("bbox_x0") is not None else None, "bbox_space": row.get("bbox_space"),
                             "source_lifecycle": trace.get("lifecycle_status")},
                "created_by": {"extractor": f"{trace.get('extractor_id')} {trace.get('extractor_version')}",
                               "models": trace.get("models"), "run": trace.get("processing_run_id")},
                "native_or_ocr": {"origin": trace.get("origin"), "text_layer": trace.get("text_layer"),
                                  "region_origin": trace.get("region_origin"),
                                  "is_primary_layer": trace.get("is_primary_layer")},
                "auto_or_reviewed": trace.get("review_status"),
                "pipeline_version": trace.get("pipeline_version"),
                "model_revision": trace.get("model_revision"),
                "rebuildable": bool(trace.get("rebuildable_from_raw")),
                "rebuild_inputs": [x for x in (trace.get("raw_artifact_id"), trace.get("config_hash")) if x],
                "projections": projections,
                "scope_note": "the area is the source's (inherited), not established for this object"}
        else:
            chain = [{"step": "REGISTRY_INPUT", "subject": trace.get("input_ref"),
                      "input_sha256": trace.get("input_sha256"), "input_row": trace.get("input_row")},
                     {"step": "PROCESSING_RUN", "subject": trace.get("processing_run_id"),
                      "run_kind": trace.get("run_kind"), "code_revision": trace.get("code_revision"),
                      "host_role": trace.get("host_role")},
                     {"step": "CANONICAL_COMMIT", "subject": trace.get("commit_id"),
                      "snapshot_id": trace.get("snapshot_id")}]
            interpretability = {"what": f"{kind} {object_id} (registry)", "original": {"input": trace.get("input_ref"),
                                                                                        "row": trace.get("input_row")},
                                "created_by": {"extractor": f"{trace.get('extractor_id')} "
                                                            f"{trace.get('extractor_version')}"},
                                "native_or_ocr": "not applicable (registry row)",
                                "auto_or_reviewed": trace.get("review_status"), "rebuildable": True,
                                "rebuild_inputs": [x for x in (trace.get("input_ref"), trace.get("input_sha256")) if x],
                                "projections": projections}
        record = {"kind": "PROCESSING", "object_id": object_id, "chain": chain,
                  "interpretability": interpretability,
                  "note": "processing provenance (contract §36), not scientific provenance"}
        return Result(item=Item(envelope=self.envelope(kind, row, payload_form="REFERENCE"), record=jsonable(record)))

    # ================================================================================================ processing
    def processing_status(self, *, source_id: str | None = None, page_id: str | None = None,
                          run_id: str | None = None, job_id: int | None = None) -> Result:
        given = [x for x in (source_id, page_id, run_id, job_id) if x is not None]
        if len(given) != 1:
            raise ApiFailure("INVALID_ARGUMENT", "give exactly one of source_id, page_id, run_id, job_id")
        warnings: list[ApiWarning] = []
        if job_id is not None:
            return self.get_job(job_id)
        if run_id is not None:
            self._check("run", run_id)
            row = self.canon.run(run_id)
            if row is None:
                raise ApiFailure("NOT_FOUND", f"{run_id} is not in the current snapshot", object_id=run_id)
            record = {"run": jsonable(row), "step_outcomes": self.canon.run_step_counts(run_id),
                      "errors": jsonable(self.canon.errors(run_id=run_id))}
            envelope = Envelope(object_id=run_id, object_kind="PROCESSING_RUN", review_status="NOT_APPLICABLE",
                                layer="CANONICAL", payload_form="NORMALIZED",
                                provenance=Provenance(processing_run_id=run_id,
                                                      pipeline_version=row.get("pipeline_version")),
                                canonical_snapshot_id=self.canon.snapshot_id())
            return Result(item=Item(envelope=envelope, record=record))
        if page_id is not None:
            self._check("page", page_id)
            row = self._get("PAGE", page_id)
            record = {"page_status": row.get("page_status"), "file_text_status": row.get("file_text_status"),
                      "ocr_status": row.get("ocr_status"), "primary_text_origin": row.get("primary_text_origin"),
                      "stages": jsonable(self.canon.processing_of_page(page_id)),
                      "errors": jsonable(self.canon.errors(page_id=page_id)),
                      "jobs": self._jobs_for(page_id=page_id, warnings=warnings)}
            return Result(item=Item(envelope=self.envelope("PAGE", row, payload_form="REFERENCE"), record=record),
                          warnings=warnings)
        self._check("source", source_id or "")
        row = self._get("SOURCE", source_id)  # type: ignore[arg-type]
        record = {"summary": jsonable(self.canon.source_summary(source_id)),  # type: ignore[arg-type]
                  "stages": jsonable(self.canon.processing_of_source(source_id)),  # type: ignore[arg-type]
                  "errors": jsonable(self.canon.errors(source_id=source_id)),
                  "jobs": self._jobs_for(source_id=source_id, warnings=warnings)}
        return Result(item=Item(envelope=self.envelope("SOURCE", row), record=record), warnings=warnings)

    def _jobs_for(self, *, warnings: list[ApiWarning], source_id: str | None = None,
                  page_id: str | None = None) -> list[dict[str, Any]] | None:
        if self.deps.control is None:
            warnings.append(ApiWarning(code="DEGRADED_DEPENDENCY", message="control plane not configured: job "
                                                                           "states are not shown"))
            return None
        try:
            jobs = []
            for kind in ("REPROCESS_SOURCE", "REPROCESS_PAGE"):
                jobs += [j for j in self.deps.control.active_jobs(kind)
                         if (source_id and j.get("source_id") == source_id)
                         or (page_id and j.get("page_id") == page_id)]
            return jsonable(jobs)
        except ApiFailure:
            warnings.append(ApiWarning(code="DEGRADED_DEPENDENCY", message="control plane unavailable: canonical "
                                                                           "status only"))
            return None

    def get_job(self, job_id: int) -> Result:
        control = _require(self.deps.control, "control plane", "control_plane")
        job = control.job(job_id)
        if job is None:
            raise ApiFailure("NOT_FOUND", f"job {job_id} does not exist", object_id=str(job_id))
        return Result(item=self._job_item(job))

    def _job_item(self, job: dict[str, Any], **extra: Any) -> Item:
        record = jsonable({k: job.get(k) for k in ("job_id", "kind", "state", "source_id", "page_id", "request",
                                                   "requested_by", "requested_at", "plan", "plan_sha256",
                                                   "planned_by", "planned_at", "confirmed_plan_sha256", "confirmed_by",
                                                   "confirmed_at", "run_id", "commit_ids", "snapshot_id", "attempts",
                                                   "note", "updated_at")})
        record.update(extra)
        source_id = job.get("source_id") or (job.get("page_id") or "").split(":")[0] or None
        envelope = Envelope(object_id=f"JOB-{job.get('job_id')}", object_kind="JOB", review_status="NOT_APPLICABLE",
                            layer="OPERATIONAL", payload_form="NORMALIZED", source_id=source_id,
                            page_id=job.get("page_id"))
        return Item(envelope=envelope, record=record)

    # ================================================================================================ reprocess
    def cancel_job(self, job_id: int, reason: str, token_label: str) -> Result:
        """A human declines a plan (or withdraws a request): PLAN_REQUESTED, PLANNED or CONFIRMED → CANCELLED."""
        control = _require(self.deps.control, "control plane", "control_plane")
        job = control.job(job_id)
        if job is None:
            raise ApiFailure("NOT_FOUND", f"job {job_id} does not exist", object_id=str(job_id))
        if job.get("kind") not in ("REPROCESS_SOURCE", "REPROCESS_PAGE"):
            raise ApiFailure("FORBIDDEN", f"job {job_id} is a {job.get('kind')} job; the API cancels only "
                                          "reprocess jobs")
        cancelled = control.cancel(job_id, token_label, reason)
        return Result(item=self._job_item(cancelled))

    def reprocess(self, kind: str, target_id: str, reason: str, options: dict[str, bool],
                  job_id: int | None, plan_sha256: str | None, token_label: str) -> tuple[int, Result]:
        """Plan-first (H-12): no job_id → PLAN_REQUESTED (202); job_id + plan_sha256 → confirm that plan (200).

        ``options`` are exactly the worker's (``force``, ``recall_model``, ``no_ocr``); they are stored as
        ``request.options`` — the only part of the request the worker executes."""
        control = _require(self.deps.control, "control plane", "control_plane")
        unknown = set(options) - set(REPROCESS_OPTIONS)
        if unknown:
            raise ApiFailure("INVALID_ARGUMENT", f"options {sorted(unknown)} are not executed by the worker "
                                                f"(allowed: {', '.join(REPROCESS_OPTIONS)})")
        if job_id is not None:
            if not plan_sha256:
                raise ApiFailure("INVALID_ARGUMENT", "confirming needs the plan_sha256 shown by get_job")
            job = control.job(job_id)
            if job is None:
                raise ApiFailure("NOT_FOUND", f"job {job_id} does not exist")
            if job.get("kind") != kind or (job.get("page_id") or job.get("source_id")) != target_id:
                raise ApiFailure("INVALID_ARGUMENT", f"job {job_id} is not a {kind} job for {target_id}")
            if job.get("state") == "PLAN_REQUESTED":
                raise ApiFailure("PLAN_NOT_READY", "the worker has not published the plan yet; poll get_job",
                                 hint="retry when state is PLANNED")
            if job.get("state") != "PLANNED":
                raise ApiFailure("PLAN_CHANGED", f"job {job_id} is {job.get('state')}; only a PLANNED job can be "
                                                 "confirmed")
            if job.get("plan_sha256") != plan_sha256:
                raise ApiFailure("PLAN_CHANGED", "the plan changed since it was shown; read the job again",
                                 details={"current_plan_sha256": job.get("plan_sha256")})
            confirmed = control.confirm(job_id, plan_sha256, token_label)
            return 200, Result(item=self._job_item(confirmed, next_step="the worker re-plans and runs only if the "
                                                                          "plan hash is unchanged"))
        if kind == "REPROCESS_PAGE":
            self._check("page", target_id)
            page = self._get("PAGE", target_id)
            source = self._get("SOURCE", page["source_id"])
        else:
            self._check("source", target_id)
            source = self._get("SOURCE", target_id)
        if source.get("lifecycle_status") != "ACTIVE":
            raise ApiFailure("NOT_REPROCESSABLE", f"{source['source_id']} is {source.get('lifecycle_status')} by the "
                                                  "register (not processed by policy)")
        active = control.active_jobs(kind)
        wanted = {o: bool(options.get(o)) for o in REPROCESS_OPTIONS}
        for job in active:
            if (job.get("page_id") or job.get("source_id")) == target_id:
                full = control.job(int(job["job_id"])) or job
                current = {o: bool(((full.get("request") or {}).get("options") or {}).get(o))
                           for o in REPROCESS_OPTIONS}
                if current != wanted:
                    raise ApiFailure("JOB_STATE_CONFLICT", f"job {full.get('job_id')} for {target_id} is active with "
                                                           "other options; cancel it first",
                                     details={"job_id": full.get("job_id"), "options": current})
                return 200, Result(item=self._job_item(full, deduplicated=True))
        if len(active) >= ACTIVE_JOB_QUOTA[kind]:
            raise ApiFailure("RATE_LIMITED", f"{len(active)} active {kind} jobs (limit {ACTIVE_JOB_QUOTA[kind]})")
        request = {"reason": reason, "options": {o: bool(options.get(o)) for o in REPROCESS_OPTIONS},
                   "requested_via": "vkm-api", "api_version": API_VERSION,
                   "canonical_snapshot_id": self.canon.snapshot_id()}
        new_id = control.request_plan(kind, token_label, source_id=source["source_id"],
                                      page_id=target_id if kind == "REPROCESS_PAGE" else None, request=request)
        job = control.job(new_id) or {"job_id": new_id, "kind": kind, "state": "PLAN_REQUESTED"}
        return 202, Result(item=self._job_item(job, next_step="poll get_job until PLANNED, review the plan, then "
                                                              "call again with job_id and plan_sha256"))

    # ================================================================================================ status
    async def status(self, run_sync: Callable[..., Any]) -> dict[str, Any]:
        out: dict[str, Any] = {"api_version": API_VERSION, "host_roles": HOST_ROLES}
        try:
            out["canonical"] = await run_sync(self.canon.status)
            out["canonical"]["counts"] = await run_sync(self.canon.corpus_counts)
        except ApiFailure as exc:
            out["canonical"] = {"available": False, "error": exc.code}
        out["dependencies"] = {}
        snapshot = (out.get("canonical") or {}).get("snapshot_id")
        if self.deps.search is not None:
            try:
                s = await run_sync(self.deps.search.status)
                aliases = {name: {"build_id": a.get("build_id"), "built_from_snapshot_id":
                                  a.get("built_from_snapshot_id"), "count": a.get("count"),
                                  "matches_canonical_snapshot": a.get("built_from_snapshot_id") == snapshot}
                           for name, a in (s.get("aliases") or {}).items()}
                out["dependencies"]["opensearch"] = {"available": True, "aliases": aliases,
                                                     "consistent_snapshot": s.get("consistent_snapshot"),
                                                     "server_version": (s.get("server") or {}).get("version")}
                vec = s.get("vectors") or {}
                if vec.get("indices"):
                    out["dependencies"]["opensearch"]["vectors"] = {
                        **{k: vec.get(k) for k in ("alias", "build_id", "built_from_snapshot_id", "count",
                                                   "model_key", "dimension", "space_type", "config_signature")},
                        "matches_canonical_snapshot": vec.get("built_from_snapshot_id") == snapshot}
            except ApiFailure as exc:
                out["dependencies"]["opensearch"] = {"available": False, "error": exc.code}
        if self.deps.hybrid is not None and hasattr(self.deps.hybrid, "status"):
            h = await run_sync(self.deps.hybrid.status)
            out["dependencies"]["query_encoder"] = h.get("query_encoder")
            if "late_default" in h:
                out["dependencies"]["late_interaction"] = {
                    "default": h["late_default"], "store": (h.get("query_encoder") or {}).get("late_store")}
        if self.deps.graph is not None:
            try:
                g = await run_sync(self.deps.graph.state)
                out["dependencies"]["neo4j"] = {k: g.get(k) for k in ("state", "build_id", "built_from_snapshot_id",
                                                                      "graph_schema_version", "finished_at")}
                out["dependencies"]["neo4j"]["matches_canonical_snapshot"] = g.get("built_from_snapshot_id") == \
                    snapshot
            except ApiFailure as exc:
                out["dependencies"]["neo4j"] = {"available": False, "error": exc.code}
        if self.deps.rerank is not None:
            try:
                r = await self.deps.rerank.status()
                out["dependencies"]["rerank"] = {"available": True, "gateway_version": r.get("gateway_version"),
                                                 "backends": {name: {k: b.get(k) for k in (
                                                     "kind", "status", "model_id", "model_revision", "quant",
                                                     "placement", "license", "deviation_note")}
                                                     for name, b in (r.get("backends") or {}).items()}}
            except ApiFailure as exc:
                out["dependencies"]["rerank"] = {"available": False, "error": exc.code}
        if self.deps.control is not None:
            try:
                c = await run_sync(self.deps.control.status)
                out["dependencies"]["control_plane"] = {"available": True, "jobs": c.get("jobs"),
                                                        "workers": c.get("workers"),
                                                        "errors_24h": c.get("errors_24h"),
                                                        "schema_version": c.get("schema_version")}
            except ApiFailure as exc:
                out["dependencies"]["control_plane"] = {"available": False, "error": exc.code}
        return jsonable(out)


_LABEL_NUMBER = re.compile(r"(\d+(?:[.\-–]\d+)*[a-zа-я]?)", re.IGNORECASE)
_LATIN_LABEL = re.compile(r"^\s*(?:fig|table|tab|eq)", re.IGNORECASE)


def object_label(kind: str, row: dict[str, Any], nav_number: str | None = None) -> dict[str, Any] | None:
    """Printed label of a figure / table / formula for search results (agent L): «рис. 3.1», «табл. 2», «(3.2)».

    From the canonical label (``figure_label`` / ``table_label`` / ``equation_label``); for a formula without one,
    the equation number of the navigation layer (``formula_context``, rules: AUTO_EXTRACTED_UNREVIEWED). None when the
    object has no number."""
    raw = {"FIGURE": row.get("figure_label"), "TABLE": row.get("table_label"),
           "FORMULA": row.get("equation_label")}.get(kind)
    origin = "CANON"
    if kind not in ("FIGURE", "TABLE", "FORMULA"):
        return None
    m = _LABEL_NUMBER.search(raw or "")
    if not m and kind == "FORMULA" and nav_number:
        raw, origin, m = nav_number, "NAV", _LABEL_NUMBER.search(nav_number)
    if not m:
        return None
    number = m.group(1).replace("–", "-").lower()
    latin = bool(_LATIN_LABEL.match(raw or ""))
    label = {"FIGURE": ("fig. " if latin else "рис. ") + number, "TABLE": ("table " if latin else "табл. ") + number,
             "FORMULA": f"({number})"}[kind]
    return {"object_label": label, "object_number": number, "object_label_raw": raw, "object_label_origin": origin}


def _title(kind: str, row: dict[str, Any]) -> str | None:
    if kind == "FIGURE":
        return " ".join(x for x in (row.get("figure_label"), row.get("caption_normalized") or row.get("caption"))
                        if x) or None
    if kind == "TABLE":
        return " ".join(x for x in (row.get("table_label"), row.get("caption_normalized") or row.get("caption"))
                        if x) or None
    if kind == "FORMULA":
        return row.get("equation_label")
    if kind == "PAGE":
        return row.get("printed_page_raw")
    return None

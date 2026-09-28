"""The single mapping of internal extraction results (``extract.model``) onto the canonical rows of the contract.

Everything that knows dataset field names lives here: envelopes (``contracts.builders``), object IDs
(``vkm_corpus.ids``: region anchor + producer key, H-14; DOCX objects are document-scoped with the paragraph path,
H-50), page text (``text_rules.page_text_v1``, H-04), block normalisation (``normalize_text_v1``), processing steps,
errors and the artifact index rows of every artifact the rows reference.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from vkm_corpus import ids
from vkm_corpus.contracts import vocab
from vkm_corpus.contracts.base import ArtifactRecipe, ModelRef as CModelRef
from vkm_corpus.contracts.builders import ProducerContext, SourceContext, build_row, doc_envelope
from vkm_corpus.contracts.datasets import DOCUMENT_DATASETS
from vkm_corpus.contracts.signatures import config_hash
from vkm_corpus.contracts.site_scope import map_site_scope
from vkm_corpus.contracts.text_rules import normalize_text_v1, page_text_v1
from vkm_corpus.extract.model import BlockX, FigureX, FormulaX, ObjectBase, PageX, SourceInput, SourceResult, TableX
from vkm_corpus.versions import PIPELINE_VERSION

_SAFE_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|/home/|/mnt/[a-z]/|/Users/|/root/)[^\s'\"]*")


def sanitize(message: str) -> str:
    """Error messages without absolute machine paths (CP-12)."""
    return _SAFE_PATH.sub("<path>", message or "")[:1000]


def source_context(src: SourceInput) -> SourceContext:
    m = map_site_scope(src.evidence_scope)
    return SourceContext(src.source_id, src.sha256, tuple(m.scopes), src.evidence_scope, m.mapping.value)


def _flags(values: Iterable[str], dataset_name: str) -> list[str]:
    known = {f.value for f in vocab.QualityFlag}
    out: list[str] = []
    for v in values:
        v = {"SIGNATURE_AFTER_BOM": "LEADING_BYTES_BEFORE_HEADER"}.get(v, v)
        if v in known and dataset_name in vocab.QUALITY_FLAG_SCOPE.get(v, frozenset()) and v not in out:
            out.append(v)
    return out


def _models(refs: Iterable[Any]) -> tuple[CModelRef, ...]:
    seen, out = set(), []
    for r in refs:
        if r.role in seen:
            continue
        seen.add(r.role)
        out.append(CModelRef(role=r.role, model_id=r.model_id, model_revision=r.model_revision))
    return tuple(out)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _page_id(src: SourceInput, unit: str, index: int) -> str:
    return ids.page_id(src.source_id, unit, index)


@dataclass
class CanonRows:
    tables: dict[str, list[Any]] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    artifact_ids: set[str] = field(default_factory=set)
    document_status: str | None = None
    page_count: int = 0
    counts: dict[str, int] = field(default_factory=dict)


class CanonMapper:
    def __init__(self, result: SourceResult, *, run_id: str, config_hashes: dict[str, str],
                 created_at: datetime | None = None, host_role: str = "WORKSTATION",
                 prep_run_id: str | None = None):
        self.r = result
        self.src = result.source
        self.run_id = run_id
        self.hashes = config_hashes
        self.now = created_at or datetime.now(timezone.utc)
        self.host_role = host_role
        self.prep_run_id = prep_run_id
        self.ctx = source_context(self.src)
        fmt = result.document.file_format if result.document else "UNKNOWN"
        self.unit = {"EPUB": "s", "DOCX": "r"}.get(fmt, "p")
        self.fmt = fmt
        self.alloc = ids.ObjectIdAllocator()
        self.out = CanonRows()
        self.block_ids: dict[int, str] = {}

    # ------------------------------------------------------------------ producers
    def producer(self, obj: ObjectBase, cfg_key: str = "objects") -> ProducerContext:
        return ProducerContext(pipeline_version=PIPELINE_VERSION, processing_run_id=self.run_id,
                               extractor_id=obj.extractor_id, extractor_version=obj.extractor_version,
                               config_hash=self.hashes.get(cfg_key, obj.raw_config_hash),
                               raw_config_hash=obj.raw_config_hash, extraction_generation=int(obj.generation),
                               models=_models(obj.models))

    @staticmethod
    def _docx_path(obj: ObjectBase) -> str | None:
        """Path of the DOCX body element anchoring the object (paragraph, table, ``…#mathN``, ``…#imgN``; H-50).
        The row's ``docx_paragraph_path`` and the id anchor are this same value, so validator B04 can recompute."""
        return obj.raw_locator or obj.extra.get("docx_paragraph_path")

    def _scope_and_anchor(self, obj: ObjectBase, text_key: str,
                          block_ordinal: int | None = None) -> tuple[str, str, str | None]:
        """(scope id, anchor, page_id) of an object — the anchors validator B04 recomputes from the row:
        DOCX element path; bbox in PAGE_PT_TL; for blocks without a box (EPUB) reading order + text."""
        if obj.region_origin == "DOCX_ELEMENT":
            anchor = ids.xml_anchor(self._docx_path(obj))
            pg = obj.extra.get("render_page_index")
            return ids.document_id(self.src.source_id), anchor, _page_id(self.src, "r", pg) if pg else None
        pid = _page_id(self.src, self.unit, obj.page_index)
        if obj.bbox is not None:
            return pid, ids.bbox_anchor(*obj.bbox), pid
        if block_ordinal is not None:
            return pid, ids.ordinal_anchor(block_ordinal, text_key), pid
        if obj.extra.get("element_path"):
            return pid, ids.xml_anchor(obj.extra["element_path"]), pid
        return pid, ids.ordinal_anchor(obj.anchor_ordinal or 0, text_key), pid

    def _allocate(self, obj: ObjectBase, kind: str, text_key: str,
                  block_ordinal: int | None = None) -> tuple[str, str | None, bool, ProducerContext]:
        prod = self.producer(obj)
        scope, anchor, page_id = self._scope_and_anchor(obj, text_key, block_ordinal)
        oid, dup = self.alloc.allocate(scope, kind, obj.origin, obj.region_origin, anchor, prod.producer_key())
        return oid, page_id, dup, prod

    def _bbox(self, obj: ObjectBase) -> dict[str, Any]:
        if obj.bbox is None:
            return {"bbox_x0": None, "bbox_y0": None, "bbox_x1": None, "bbox_y1": None, "bbox_space": "NONE"}
        x0, y0, x1, y1 = obj.bbox
        return {"bbox_x0": float(x0), "bbox_y0": float(y0), "bbox_x1": float(x1), "bbox_y1": float(y1),
                "bbox_space": "PAGE_PT_TL"}

    def _raw_refs(self, obj: ObjectBase) -> list[dict[str, str]]:
        out, seen = [], set()
        for role, aid in obj.raw_artifacts:
            if aid and (role, aid) not in seen:
                seen.add((role, aid))
                out.append({"role": role, "artifact_id": aid})
                self.out.artifact_ids.add(aid)
        if obj.raw_artifact_id:
            self.out.artifact_ids.add(obj.raw_artifact_id)
        return out

    def _envelope(self, obj: ObjectBase, kind: str, oid: str, page_id: str | None, prod: ProducerContext,
                  dataset_name: str, raw_hash: str | None, dup: bool) -> dict[str, Any]:
        flags = list(obj.quality_flags) + (["DUPLICATE_DETECTION_DISAMBIGUATED"] if dup else [])
        return doc_envelope(self.ctx, prod, object_kind=kind, object_id=oid, origin=obj.origin, created_at=self.now,
                            page_id=page_id, raw_artifact_id=obj.raw_artifact_id, raw_artifacts=self._raw_refs(obj),
                            raw_content_sha256=raw_hash, quality_flags=_flags(flags, dataset_name))

    # ------------------------------------------------------------------ objects
    def blocks(self) -> list[Any]:
        rows = []
        for i, b in enumerate(self.r.blocks):
            clean = b.text
            if b.origin == "OCR":
                from vkm_corpus.ocr.normalize import text_from_markdown

                clean = text_from_markdown(b.text)
            oid, page_id, dup, prod = self._allocate(b, "BLOCK", b.text, block_ordinal=int(b.reading_order or 0))
            self.block_ids[i] = oid
            env = self._envelope(b, "BLOCK", oid, page_id, prod, "blocks", _sha(b.text), dup)
            rows.append(build_row(
                "blocks", env, region_origin=b.region_origin, text_layer=b.text_layer, **self._bbox(b),
                docx_paragraph_path=self._docx_path(b) if b.region_origin == "DOCX_ELEMENT" else None,
                is_primary_layer=bool(b.is_primary_layer),
                block_type=b.block_type, reading_order=int(b.reading_order or 0),
                reading_order_method=b.extra.get("reading_order_method"), native_order=b.native_order,
                text=b.text, normalized_text=normalize_text_v1(clean), char_count=len(b.text),
                language=b.language, recognition_confidence=b.recognition_confidence, raw_locator=b.raw_locator,
                embedded_layer_evidence=b.extra.get("embedded_layer_evidence") if b.origin == "EMBEDDED_OCR" else None,
                text_layer_producer=b.extra.get("text_layer_producer")))
        return rows

    def figures(self) -> list[Any]:
        rows = []
        for f in self.r.figures:
            key = f"{f.image_artifact_id}|{f.embedded_image_artifact_id}|{f.raw_locator}"
            oid, page_id, dup, prod = self._allocate(f, "FIGURE", key)
            env = self._envelope(f, "FIGURE", oid, page_id, prod, "figures", _sha(key), dup)
            for aid in [f.image_artifact_id, f.embedded_image_artifact_id] + [a for _, a in f.vector_artifacts]:
                if aid:
                    self.out.artifact_ids.add(aid)
            rows.append(build_row(
                "figures", env, region_origin=f.region_origin, text_layer="NONE", **self._bbox(f),
                docx_paragraph_path=self._docx_path(f) if f.region_origin == "DOCX_ELEMENT" else None,
                figure_label=f.figure_label,
                caption=f.caption, caption_normalized=normalize_text_v1(f.caption) if f.caption else None,
                caption_block_id=self.block_ids.get(f.caption_block_index) if f.caption_block_index is not None
                else None, layout_class=f.layout_class, layout_score=f.layout_score,
                image_artifact_id=f.image_artifact_id, image_dpi=f.image_dpi,
                embedded_image_artifact_id=f.embedded_image_artifact_id,
                embedded_image_transcoded=f.embedded_image_transcoded, original_filter=f.original_filter,
                vector_artifacts=[{"format": fmt, "artifact_id": a} for fmt, a in f.vector_artifacts]))
        return rows

    def tables(self) -> list[Any]:
        rows = []
        for t in self.r.tables:
            oid, page_id, dup, prod = self._allocate(t, "TABLE", t.raw_output or "")
            env = self._envelope(t, "TABLE", oid, page_id, prod, "tables", _sha(t.raw_output or ""), dup)
            if t.image_artifact_id:
                self.out.artifact_ids.add(t.image_artifact_id)
            layer = {"OCR": "GLM_OCR"}.get(t.origin) or ("EPUB_XHTML" if t.region_origin == "EPUB_ELEMENT" else
                                                         "DOCX_XML" if t.region_origin == "DOCX_ELEMENT" else "NONE")
            rows.append(build_row(
                "tables", env, region_origin=t.region_origin, text_layer=layer, **self._bbox(t),
                docx_paragraph_path=self._docx_path(t) if t.region_origin == "DOCX_ELEMENT" else None,
                table_label=t.table_label, caption=t.caption,
                caption_normalized=normalize_text_v1(t.caption) if t.caption else None,
                caption_block_id=self.block_ids.get(t.caption_block_index) if t.caption_block_index is not None
                else None, recognition_method=t.recognition_method, raw_format=t.raw_format,
                raw_output=t.raw_output or "", n_rows=t.n_rows, n_cols=t.n_cols,
                header_rows=sum(1 for r in {c["row"] for c in t.cells if c.get("is_header")}) or None,
                cells=[{k: c[k] for k in ("row", "col", "row_span", "col_span", "is_header", "text")} for c in t.cells],
                normalized_text=normalize_text_v1(t.normalized_text) if t.normalized_text else None,
                structure_confidence=t.structure_confidence, image_artifact_id=t.image_artifact_id,
                image_dpi=t.image_dpi))
        return rows

    def formulas(self) -> list[Any]:
        rows = []
        for f in self.r.formulas:
            key = f.raw_output or f"image:{f.image_artifact_id}|{f.raw_locator}"
            oid, page_id, dup, prod = self._allocate(f, "FORMULA", key)
            env = self._envelope(f, "FORMULA", oid, page_id, prod, "formulas", _sha(key), dup)
            if f.image_artifact_id:
                self.out.artifact_ids.add(f.image_artifact_id)
            layer = "GLM_OCR" if f.origin == "OCR" else "DOCX_XML" if f.region_origin == "DOCX_ELEMENT" else "NONE"
            rows.append(build_row(
                "formulas", env, region_origin=f.region_origin, text_layer=layer, **self._bbox(f),
                docx_paragraph_path=self._docx_path(f) if f.region_origin == "DOCX_ELEMENT" else None,
                formula_kind=f.formula_kind,
                equation_label=f.equation_label, recognition_method=f.recognition_method, raw_format=f.raw_format,
                raw_output=f.raw_output, normalized_latex=f.normalized_latex, latex_parse_ok=f.latex_parse_ok,
                native_glyph_text=f.native_glyph_text, recognition_confidence=f.recognition_confidence,
                image_artifact_id=f.image_artifact_id, image_dpi=None))
        return rows

    # ------------------------------------------------------------------ pages, document
    def pages(self, block_rows: list[Any]) -> list[Any]:
        by_page: dict[str, list[Any]] = {}
        for b in block_rows:
            if b.page_id:
                by_page.setdefault(b.page_id, []).append(b)
        rows = []
        for p in self.r.pages:
            pid = _page_id(self.src, self.unit, p.page_index)
            pt = page_text_v1(by_page.get(pid, []))
            file_layer = self._file_layer(p)
            primary_layer = p.primary_text_layer if p.primary_text_layer and p.primary_text_layer != "NONE" else "NONE"
            primary_origin = vocab.TEXT_LAYER_ORIGIN.get(primary_layer) if primary_layer != "NONE" else None
            if p.page_status in ("NATIVE_OK",) and primary_origin is None:
                primary_layer, primary_origin = (file_layer, vocab.TEXT_LAYER_ORIGIN.get(file_layer)) \
                    if file_layer != "NONE" else ("PDF_TEXT_LAYER", "NATIVE")
            prod = ProducerContext(pipeline_version=PIPELINE_VERSION, processing_run_id=self.run_id,
                                   extractor_id="vkm-pipeline", extractor_version="0.1.0",
                                   config_hash=self.hashes.get("page", self.hashes.get("objects")),
                                   raw_config_hash=self.hashes.get("page", self.hashes.get("objects")),
                                   models=_models(p.models))
            for aid in [p.native_raw_artifact_id, p.layout_raw_artifact_id, p.render_artifact_id,
                        p.layout_render_artifact_id] + list(p.ocr_raw_artifact_ids):
                if aid:
                    self.out.artifact_ids.add(aid)
            env = doc_envelope(self.ctx, prod, object_kind="PAGE", object_id=pid,
                               origin="DERIVED" if self.fmt == "DOCX" else "NATIVE", created_at=self.now,
                               page_id=pid, raw_artifact_id=p.native_raw_artifact_id,
                               quality_flags=_flags(p.quality_flags, "pages"))
            page_class = p.page_class if p.page_class in {c.value for c in vocab.PageClass} else "UNKNOWN"
            route = p.route if p.route in {c.value for c in vocab.PageRoute} else None
            rows.append(build_row(
                "pages", env, page_index=p.page_index, page_kind=p.page_kind, page_class=page_class, page_route=route,
                printed_page_raw=p.printed_page_raw, printed_page_labels=list(p.printed_page_labels),
                printed_label_origin=p.printed_label_origin,
                printed_label_status=p.extra.get("printed_label_status", "NONE") if p.printed_page_labels else "NONE",
                printed_label_extractor=p.printed_label_extractor, is_spread=p.is_spread, width_pt=p.width_pt,
                height_pt=p.height_pt, rotation_deg=int(p.rotation_deg or 0), page_box=p.page_box,
                native_dpi=float(p.native_dpi) if p.native_dpi else None, bbox_space=p.bbox_space,
                file_text_layer=file_layer, file_text_status=self._file_status(p),
                file_text_char_count=p.native_char_count,
                embedded_layer_evidence=p.embedded_layer_evidence if file_layer in (
                    "PDF_EMBEDDED_OCR_LAYER", "DJVU_EMBEDDED_OCR_LAYER") else None,
                text_layer_producer=p.text_layer_producer, ocr_status=p.ocr_status,
                ocr_char_count=p.recognized_char_count, page_status=p.page_status,
                primary_text_layer=primary_layer, primary_text_origin=primary_origin,
                normalized_text=pt.normalized_text, text_rule=pt.rule, text_sha256=pt.text_sha256,
                char_count=pt.char_count, native_ocr_cer=p.native_ocr_cer,
                native_raw_artifact_id=p.native_raw_artifact_id, layout_raw_artifact_id=p.layout_raw_artifact_id,
                ocr_raw_artifact_id=p.ocr_raw_artifact_ids[0] if len(p.ocr_raw_artifact_ids) == 1 else None,
                render_artifact_id=p.layout_render_artifact_id,
                render_dpi=200 if p.layout_render_artifact_id else None, preview_artifact_id=p.render_artifact_id,
                spine_href=p.spine_href))
        return rows

    def _file_layer(self, p: PageX) -> str:
        if self.fmt == "EPUB":
            return "EPUB_XHTML"
        if self.fmt == "DOCX":
            return "DOCX_XML"
        if p.native_text_status in ("ABSENT", "NOT_CHECKED") and not p.native_char_count:
            return "NONE"
        if self.fmt == "DJVU":
            return "DJVU_EMBEDDED_OCR_LAYER"
        if p.route == "OCR_OPTIONAL" or p.page_class == "RASTER_SCAN":
            return "PDF_EMBEDDED_OCR_LAYER"
        return "PDF_TEXT_LAYER" if p.native_char_count else "NONE"

    @staticmethod
    def _file_status(p: PageX) -> str:
        return {"PRESENT_OK": "PRESENT_OK", "PRESENT_BROKEN": "PRESENT_BROKEN", "PRESENT_PARTIAL": "PRESENT_PARTIAL",
                "ABSENT": "ABSENT", "NOT_APPLICABLE": "NOT_APPLICABLE"}.get(p.native_text_status, "NOT_CHECKED")

    def document(self) -> list[Any]:
        d = self.r.document
        if d is None or d.pagination is None:
            return []
        pag = d.pagination
        meta = d.file_meta or {}
        hint = re.search(r"(\d+) pages", self.src.register_notes or "")
        hint_n = int(hint.group(1)) if hint else None
        flags = list(d.quality_flags)
        if hint_n is not None and hint_n != pag.count:
            flags.append("PAGECOUNT_DIFFERS_FROM_REGISTER_HINT")
        prod = ProducerContext(pipeline_version=PIPELINE_VERSION, processing_run_id=self.run_id,
                               extractor_id="vkm-pipeline", extractor_version="0.1.0",
                               config_hash=self.hashes.get("document", self.hashes.get("objects")),
                               raw_config_hash=self.hashes.get("document", self.hashes.get("objects")))
        if d.native_raw_artifact_id:
            self.out.artifact_ids.add(d.native_raw_artifact_id)
        if d.pagination_artifact_id:
            self.out.artifact_ids.add(d.pagination_artifact_id)
        env = doc_envelope(self.ctx, prod, object_kind="DOCUMENT", object_id=ids.document_id(self.src.source_id),
                           origin="NATIVE", created_at=self.now, raw_artifact_id=d.native_raw_artifact_id,
                           quality_flags=_flags(flags, "documents"))
        embedded_producer = next((p.text_layer_producer for p in self.r.pages if p.text_layer_producer), None)

        def m(*keys: str) -> str | None:
            for k in keys:
                v = meta.get(k)
                if v not in (None, ""):
                    return str(v)
            return None

        from vkm_corpus.extract.classify import CLASSIFIER_VERSION

        status = self.r.status if self.r.status in {s.value for s in vocab.SourceProcessingStatus} else "PARTIAL"
        return [build_row(
            "documents", env, format_detected=d.file_format, format_version=d.format_version,
            container_detail=d.container_detail, pagination_basis=pag.basis, page_unit=pag.unit,
            page_count=pag.count, page_count_check=pag.check_count, page_count_check_method=pag.check_method,
            register_page_count_hint=hint_n, shared_component_count=pag.extra.get("shared_component_count"),
            pagination_render_profile=d.pagination_render_profile, pagination_artifact_id=d.pagination_artifact_id,
            document_class=d.document_class if d.document_class in {c.value for c in vocab.DocumentClass} else "UNKNOWN",
            classifier_version=CLASSIFIER_VERSION, is_encrypted=bool(d.is_encrypted), text_extraction_permitted=None,
            has_native_page_labels=d.has_native_page_labels, text_layer_producer=embedded_producer,
            file_meta_title=m("title", "core_title"), file_meta_author=m("author", "creator", "core_creator"),
            file_meta_subject=m("subject", "core_subject"), file_meta_keywords=m("keywords", "core_keywords"),
            file_meta_creator=m("creator", "app_Application"), file_meta_producer=m("producer"),
            file_meta_created_raw=m("creationDate", "date", "core_created"),
            file_meta_modified_raw=m("modDate", "core_modified"), file_identifiers=list(d.file_identifiers),
            file_languages=list(d.file_languages), processing_status=status)]

    # ------------------------------------------------------------------ build
    def build(self) -> CanonRows:
        t = self.out.tables
        t["blocks"] = self.blocks()
        t["figures"] = self.figures()
        t["tables"] = self.tables()
        t["formulas"] = self.formulas()
        t["bibliography_entries"] = []
        t["pages"] = self.pages(t["blocks"])
        t["documents"] = self.document()
        missing = [n for n in DOCUMENT_DATASETS if n not in t]
        for n in missing:
            t[n] = []
        self.out.document_status = t["documents"][0].processing_status if t["documents"] else self.r.status
        self.out.page_count = len(t["pages"])
        self.out.counts = {n: len(v) for n, v in t.items()}
        return self.out


# ---------------------------------------------------------------------------------------------------- logs
def step_rows(result: SourceResult, *, run_id: str, host_role: str, started_at: datetime, finished_at: datetime,
              unit: str) -> list[dict[str, Any]]:
    rows = []
    seq: dict[tuple[Any, ...], int] = {}
    for s in result.steps:
        page_id = ids.page_id(result.source.source_id, unit, s.page_index) if s.page_index else None
        detail = (s.call_signature or "")[:16]
        key = (page_id, s.stage, s.attempt, detail)
        seq[key] = seq.get(key, 0) + 1
        if seq[key] > 1:
            detail = f"{detail}#{seq[key]}"
        started = s.started_at or started_at
        finished = s.finished_at or finished_at
        rows.append({
            "schema_version": "0.1.0",
            "step_id": ids.step_id(run_id, result.source.source_id, page_id, s.stage, s.attempt, detail),
            "processing_run_id": run_id, "source_id": result.source.source_id, "page_id": page_id,
            "page_index": s.page_index, "stage": s.stage, "attempt": s.attempt, "outcome": s.outcome,
            "status": s.status, "reason_code": s.reason_code, "stage_signature": s.stage_signature,
            "call_signature": s.call_signature, "source_sha256": result.source.sha256,
            "pipeline_version": PIPELINE_VERSION, "extractor_id": s.extractor_id,
            "extractor_version": s.extractor_version, "extraction_generation": 1, "config_hash": s.config_hash,
            "model_id": s.model_id, "model_revision": s.model_revision,
            "models": ([{"role": "RECOGNITION", "model_id": s.model_id, "model_revision": s.model_revision}]
                       if s.model_id else []),
            "input_artifact_ids": list(s.input_artifact_ids), "output_artifact_ids": list(s.output_artifact_ids),
            "n_objects_out": s.n_objects_out, "started_at": started, "finished_at": max(started, finished),
            "duration_ms": max(0, int((finished - started).total_seconds() * 1000)), "commit_id": None,
            "log_ref": None, "host_role": host_role})
    return rows


def error_rows(result: SourceResult, *, run_id: str, created_at: datetime, unit: str) -> list[dict[str, Any]]:
    rows = []
    known = {c.value for c in vocab.ErrorCode}
    known_stage = {c.value for c in vocab.Stage}
    # error ids are unique per run: the sequence number carries the source number (one run commits many sources)
    base = int(result.source.number) * 100_000
    for i, e in enumerate(result.errors):
        code = e.code if e.code in known else (vocab.C_ERROR_CODE_MAP.get(e.code) or "INTERNAL_ERROR")
        page_id = ids.page_id(result.source.source_id, unit, e.page_index) if e.page_index and \
            1 <= e.page_index <= 9999 else None
        rows.append({
            "schema_version": "0.1.0", "error_id": ids.error_id(run_id, None, str(code), base + i),
            "processing_run_id": run_id, "step_id": None, "source_id": result.source.source_id,
            "page_id": page_id, "page_index": e.page_index, "stage": e.stage if e.stage in known_stage else "COMMIT",
            "code": str(code), "tool": e.tool, "tool_version": e.tool_version, "message": sanitize(e.message),
            "retryable": bool(e.retryable), "severity": e.severity, "attempt": None, "log_ref": None,
            "exception_type": e.exception_type, "created_at": created_at})
    return rows


def artifact_rows(index: dict[str, dict[str, Any]], artifact_ids: Iterable[str], *, run_id: str,
                  created_at: datetime) -> tuple[list[dict[str, Any]], list[str]]:
    """Index rows for the referenced artifacts (from the staging artifact index); missing ids are returned."""
    rows, missing = [], []
    for aid in sorted(set(artifact_ids)):
        rec = index.get(aid)
        if rec is None:
            missing.append(aid)
            continue
        recipe = None
        if rec.get("recipe"):
            r = dict(rec["recipe"])
            tool = str(r.pop("renderer", r.pop("tool", "vkm_corpus")))
            version = str(r.pop("renderer_version", r.pop("tool_version", "unknown")) or "unknown")
            profile = str(r.pop("profile", rec.get("artifact_kind", "")) or "default")
            src_sha = r.pop("source_sha256", None)
            src_id = r.pop("source_id", None)
            page_index = r.get("page_index")
            page_id = None
            if src_id and page_index:
                unit = "r" if str(r.get("input", "")).startswith("DOCX") else "p"
                page_id = ids.page_id(src_id, unit, int(page_index))
            recipe = ArtifactRecipe(tool=tool, tool_version=version, profile=profile, source_sha256=src_sha,
                                    page_id=page_id,
                                    params_json=json.dumps(r, sort_keys=True, ensure_ascii=False,
                                                           separators=(",", ":"), default=str)).model_dump()
        kind = rec["artifact_kind"]
        coord = "PAGE_PT_TL" if kind in ("VECTOR_PATHS_JSON", "VECTOR_SVG") else None
        rows.append({
            "schema_version": "0.1.0", "artifact_id": aid, "artifact_kind": kind, "media_type": rec["media_type"],
            "size_bytes": rec.get("size_bytes"), "storage_relpath": rec.get("storage_relpath"),
            "retention_class": rec["retention_class"], "materialization": rec["materialization"], "recipe": recipe,
            "pixel_sha256": rec.get("pixel_sha256"), "image_width_px": rec.get("image_width_px"),
            "image_height_px": rec.get("image_height_px"), "image_dpi": rec.get("image_dpi"),
            "coordinate_space": coord, "crs_status": None, "producer_signature": rec.get("producer_signature"),
            "attempt": rec.get("attempt"), "producer_step_id": None, "created_by_run_id": run_id,
            "registered_source_id": rec.get("source_id"), "registered_page_id": rec.get("page_id"),
            "created_at": created_at})
    return rows, missing

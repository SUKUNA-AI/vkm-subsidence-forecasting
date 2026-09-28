"""Synthetic canonical snapshot for API/MCP contract tests and the local §60 dry run (synthetic values only).

``build_synthetic_canon(directory)`` writes a DuckDB file with agent D's canonical tables (rows validated by the
contract models), D's SQL views and macros (``vkm_corpus.duckdb.build.apply_sql``) and ``meta.snapshot``/``commits``,
plus stored PNG blobs under ``<directory>/artifacts``. Content:

* sources 001 (ACTIVE, FULLY_REVIEWED by Phase-1 coverage — its objects must still be AUTO_EXTRACTED_UNREVIEWED),
  002 (ACTIVE), 013 (ABSENT_BY_REGISTER) and 022 (RETIRED): the last two answer 200 with ``lifecycle_status``;
* works 001 (two registered copies: 001 FULL_COPY, 002 PARTIAL_COPY), 013 (anchored on an absent file), 050 (cited);
* pages 001:p0001…p0003 and 002:p0001 with blocks (a secondary text layer on p0001), a figure and a table with
  stored crops, a formula, bibliography entries whose DOI matches work 050 (→ CITES), a page preview, a render that
  is reproducible but not stored, one error and the START/END run records.

Needs pyarrow, duckdb and Pillow (extras ``corpus``).
"""
from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

SNAPSHOT_ID = "snap-20260928T100000Z-0a0b0c0d"


@dataclass
class SyntheticCanon:
    duckdb_path: Path
    artifacts_root: Path
    ids: dict[str, str] = field(default_factory=dict)
    snapshot_id: str = SNAPSHOT_ID


def _png(width: int, height: int, colour: tuple[int, int, int]) -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (width, height), colour)
    draw = ImageDraw.Draw(image)
    draw.rectangle((width // 8, height // 8, width * 7 // 8, height * 7 // 8), outline=(0, 0, 0), width=3)
    draw.line((0, 0, width, height), fill=(0, 0, 0), width=2)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def build_synthetic_canon(directory: Path) -> SyntheticCanon:
    import duckdb

    from vkm_corpus import ids
    from vkm_corpus.contracts import arrow as ca
    from vkm_corpus.contracts.builders import build_row
    from vkm_corpus.contracts.datasets import STORED_DATASETS
    from vkm_corpus.contracts.vocab import ARTIFACT_KIND_DIR, MEDIA_TYPE_EXT
    from vkm_corpus.duckdb.build import apply_sql
    from vkm_corpus.testing import rows as R

    directory = Path(directory)
    artifacts_root = directory / "artifacts"
    tables: dict[str, list[Any]] = {name: [] for name in STORED_DATASETS}
    out_ids: dict[str, str] = {}

    def blob(kind: str, data: bytes, media: str, page_id: str | None, source_id: str | None, *, width: int,
             height: int) -> str:
        aid = ids.artifact_id(data)
        rel = f"{ARTIFACT_KIND_DIR[kind]}/{aid[7:9]}/{aid[9:11]}/{aid[7:]}.{MEDIA_TYPE_EXT[media]}"
        target = artifacts_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        tables["artifacts"].append(build_row("artifacts", {
            "artifact_id": aid, "artifact_kind": kind, "media_type": media, "size_bytes": len(data),
            "storage_relpath": rel, "retention_class": "KEEP_REFERENCED", "materialization": "STORED",
            "image_width_px": width, "image_height_px": height, "image_dpi": 72.0, "created_by_run_id": R.RUN_ID,
            "registered_source_id": source_id, "registered_page_id": page_id, "created_at": R.T0}))
        return aid

    # ---------------------------------------------------------------------------------------- registry
    tables["sources"] += [
        R.make_source(sid="VKM-SRC-001"),
        R.make_source(sid="VKM-SRC-002", register_notes="QUICK LOOK: synthetic note, not evidence"),
        R.make_source(sid="VKM-SRC-013", lifecycle_status="ABSENT_BY_REGISTER", file_status="MISSING",
                      observed_sha256=None, observed_size_bytes=None, review_status="NOT_APPLICABLE",
                      review_status_basis="LIFECYCLE", register_skip_status="SKIPPED_BY_REGISTER",
                      register_skip_reason="ARCHIVE_DELETED_AFTER_ASSEMBLY", evidence_coverage_raw=None),
        R.make_source(sid="VKM-SRC-022", lifecycle_status="RETIRED", file_status="MISSING", observed_sha256=None,
                      observed_size_bytes=None, review_status="NOT_APPLICABLE", review_status_basis="LIFECYCLE",
                      register_skip_status="SKIPPED_BY_REGISTER", register_skip_reason="RETIRED_NOT_EVIDENCE",
                      evidence_coverage_raw=None, site_scope_raw="LEGACY_RETIRED", site_scope=[],
                      site_scope_mapping="NOT_A_SCOPE"),
    ]
    tables["works"] += [
        R.make_work(anchor="VKM-SRC-001"),
        R.make_work(anchor="VKM-SRC-013", title="Синтетическое пособие", doi=None, work_type="TEACHING_MANUAL"),
        R.make_work(anchor="VKM-SRC-050", title="Цитируемая синтетическая работа", doi="10.9999/synthetic.001"),
    ]
    tables["works"][0] = R.make_work(anchor="VKM-SRC-001", doi="10.9999/synthetic.100")
    links = [("VKM-SRC-001", "VKM-WRK-001", "FULL_COPY", True), ("VKM-SRC-002", "VKM-WRK-001", "PARTIAL_COPY", True),
             ("VKM-SRC-013", "VKM-WRK-013", "FULL_COPY", True)]
    for sid, wid, link_type, primary in links:
        oid = ids.swl_id(sid, wid, link_type, None, None)
        tables["source_work_links"].append(build_row("source_work_links", {
            **R.reg_env("source_work_links", oid, input_ref="PRIVATE:00_registry/work_registry/WORK_LINKS.csv"),
            "source_id": sid, "work_id": wid, "link_type": link_type, "is_primary": primary,
            "curation_status": "CURATED", "basis": "CURATED_MANUAL"}))
    for ordinal, name in enumerate(("Альфаев А.А.", "Бетин Б.Б."), 1):
        aid = ids.author_id(name)
        tables["authors"].append(build_row("authors", {
            **R.reg_env("authors", aid, origin="DERIVED"), "author_id": aid, "name_display": name,
            "name_key": str(ids.name_key(name)), "script": "CYRL", "identity_status": "NAME_KEY_ONLY",
            "derived_from_work_ids": ["VKM-WRK-001"]}))
        wau = ids.wau_id("VKM-WRK-001", ordinal)
        tables["work_authors"].append(build_row("work_authors", {
            **R.reg_env("work_authors", wau, input_ref="PRIVATE:00_registry/work_registry/WORK_REGISTER.csv"),
            "work_id": "VKM-WRK-001", "author_id": aid, "ordinal": ordinal, "role": "AUTHOR", "name_as_listed": name,
            "curation_status": "CURATED", "basis": "CURATED_MANUAL"}))

    # ---------------------------------------------------------------------------------------- documents, pages
    preview = blob("PAGE_PREVIEW", _png(362, 512, (240, 240, 230)), "image/png", "VKM-SRC-001:p0001", "VKM-SRC-001",
                   width=362, height=512)
    tables["documents"] += [R.make_document(sid="VKM-SRC-001", page_count=3),
                            R.make_document(sid="VKM-SRC-002", page_count=1)]
    texts = {1: "Оседание земной поверхности над выработками калийного рудника",
             2: "Мульда сдвижения и маркшейдерские наблюдения", 3: None}
    for index, text in texts.items():
        extra: dict[str, Any] = {}
        if index == 1:
            extra["preview_artifact_id"] = preview
        if index == 2:
            extra["render_artifact_id"] = R.artifact("render-not-stored")   # reproducible, not stored
        tables["pages"].append(R.make_page(index=index, sid="VKM-SRC-001", text=text, **extra))
    tables["pages"].append(R.make_page(index=1, sid="VKM-SRC-002", text="Синтетический текст второго источника"))
    tables["artifacts"].append(build_row("artifacts", {
        "artifact_id": R.artifact("render-not-stored"), "artifact_kind": "PAGE_RENDER", "media_type": "image/png",
        "retention_class": "KEEP_REFERENCED", "materialization": "NOT_STORED_REPRODUCIBLE",
        "recipe": {"tool": "pymupdf", "tool_version": "1.28.2", "profile": "r200g", "page_id": "VKM-SRC-001:p0002"},
        "created_by_run_id": R.RUN_ID, "registered_source_id": "VKM-SRC-001",
        "registered_page_id": "VKM-SRC-001:p0002", "created_at": R.T0}))
    tables["blocks"] += [R.make_block(page_index=1, order=1, text=texts[1]),
                         R.make_block(page_index=1, order=2, text="Вторая строка страницы"),
                         R.make_block(page_index=2, order=1, text=texts[2]),
                         R.make_block(page_index=1, sid="VKM-SRC-002", order=1,
                                      text="Синтетический текст второго источника")]
    secondary = R.block_dict(page_index=1, order=3, text="вторичный слой OCR")
    secondary.update(is_primary_layer=False, text_layer="GLM_OCR", origin="OCR", region_origin="OCR_MODEL",
                     model_id=R.GLM["model_id"], model_revision=R.GLM["model_revision"],
                     models=[R.GLM])
    secondary["object_id"] = ids.object_id("VKM-SRC-001:p0001", "BLOCK", "OCR", "OCR_MODEL",
                                           ids.bbox_anchor(56.7, 120.0, 538.6, 135.0), R.sha("synthetic-ocr-producer"))
    tables["blocks"].append(build_row("blocks", secondary))
    crop = blob("FIGURE_CROP", _png(300, 200, (200, 220, 255)), "image/png", "VKM-SRC-001:p0002", "VKM-SRC-001",
                width=300, height=200)
    table_crop = blob("TABLE_CROP", _png(480, 120, (255, 255, 255)), "image/png", "VKM-SRC-001:p0002",
                      "VKM-SRC-001", width=480, height=120)
    tables["figures"].append(R.make_figure(page_index=2, image_artifact_id=crop))
    tables["tables"].append(R.make_table(page_index=2, image_artifact_id=table_crop))
    tables["formulas"].append(R.make_formula(page_index=3))
    tables["bibliography_entries"] += [R.make_bibliography_entry(page_index=3, ordinal=1),
                                       R.make_bibliography_entry(page_index=3, ordinal=2,
                                                                 parsed_doi="10.9999/unknown.002",
                                                                 parsed_title="Несвязанная синтетическая работа")]

    # ---------------------------------------------------------------------------------------- journals
    run = {"processing_run_id": R.RUN_ID, "run_kind": "EXTRACTION", "started_at": R.T0, "cli_command":
           "vkm-corpus run extract --source VKM-SRC-001", "pipeline_version": "0.1.0", "code_revision": "0" * 40,
           "code_dirty": False, "host_role": "WORKSTATION", "python_version": "3.13", "platform": "linux",
           "config_hash": R.CONFIG_HASH, "created_at": R.T0,
           "models": [{"model_id": R.GLM["model_id"], "model_revision": R.GLM["model_revision"], "role":
                       "RECOGNITION", "backend": "vllm", "backend_version": "0.30.0", "quantization": "bf16",
                       "device": "cuda"}]}
    tables["processing_runs"] += [
        build_row("processing_runs", {**run, "record_phase": "START", "status": "STARTED"}),
        build_row("processing_runs", {**run, "record_phase": "END", "status": "PARTIAL",
                                      "finished_at": R.T0 + timedelta(minutes=5), "n_steps_executed": 3,
                                      "n_steps_failed": 1})]
    step_common = {"processing_run_id": R.RUN_ID, "source_id": "VKM-SRC-001", "attempt": 1,
                   "stage_signature": R.sha("stage"), "pipeline_version": "0.1.0", "extractor_id": "synthetic-native",
                   "extractor_version": "0.0.1", "config_hash": R.CONFIG_HASH, "started_at": R.T0,
                   "finished_at": R.T0 + timedelta(seconds=2), "duration_ms": 2000, "host_role": "WORKSTATION"}
    for index, status in ((1, "NATIVE_OK"), (2, "NATIVE_OK"), (3, "FAILED")):
        pid = ids.page_id("VKM-SRC-001", "p", index)
        tables["processing_steps"].append(build_row("processing_steps", {
            **step_common, "step_id": ids.step_id(R.RUN_ID, "VKM-SRC-001", pid, "NATIVE_TEXT", 1),
            "page_id": pid, "page_index": index, "stage": "NATIVE_TEXT",
            "outcome": "EXECUTED", "status": status}))
    tables["errors"].append(build_row("errors", {
        "error_id": ids.error_id(R.RUN_ID, None, "OCR_FAILED", 1), "processing_run_id": R.RUN_ID,
        "source_id": "VKM-SRC-001", "page_id": "VKM-SRC-001:p0003", "page_index": 3, "stage": "OCR",
        "code": "OCR_FAILED", "tool": "glm-ocr", "message": "synthetic failure", "retryable": True,
        "created_at": R.T0}))

    # ---------------------------------------------------------------------------------------- DuckDB
    path = directory / "duckdb" / "vkm_corpus.duckdb"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    con = duckdb.connect(str(path))
    try:
        con.execute("CREATE SCHEMA canonical; CREATE SCHEMA meta")
        for name in STORED_DATASETS:
            table = ca.rows_to_table(name, tables[name]) if tables[name] else ca.empty_table(name)
            con.register("_vkm_rows", table)
            con.execute(f'CREATE TABLE canonical."{name}" AS SELECT * FROM _vkm_rows')
            con.unregister("_vkm_rows")
        con.execute("CREATE TABLE meta.snapshot AS SELECT ?::VARCHAR AS snapshot_id, ?::VARCHAR AS manifest_sha256, "
                    "now() AS built_at, version() AS duckdb_version, '0.1.0'::VARCHAR AS pipeline_version",
                    [SNAPSHOT_ID, hashlib.sha256(b"synthetic-manifest").hexdigest()])
        con.execute("CREATE TABLE meta.commits (commit_key VARCHAR, commit_id VARCHAR)")
        con.executemany("INSERT INTO meta.commits VALUES (?, ?)",
                        [("VKM-SRC-001", "CMT-00000000000000a1"), ("VKM-SRC-002", "CMT-00000000000000a2"),
                         ("REGISTRY", "CMT-00000000000000f0")])
        apply_sql(con)
        con.execute("CHECKPOINT")
    finally:
        con.close()
    out_ids.update({
        "figure": tables["figures"][0].object_id, "table": tables["tables"][0].object_id,
        "formula": tables["formulas"][0].object_id, "block": tables["blocks"][0].object_id,
        "block2": tables["blocks"][1].object_id, "secondary_block": tables["blocks"][-1].object_id,
        "entry": tables["bibliography_entries"][0].object_id,
        "entry_unlinked": tables["bibliography_entries"][1].object_id,
        "figure_crop": crop, "table_crop": table_crop, "preview": preview,
        "render_not_stored": R.artifact("render-not-stored"), "run": R.RUN_ID})
    return SyntheticCanon(duckdb_path=path, artifacts_root=artifacts_root, ids=out_ids)


# ---------------------------------------------------------------------------------------------------- fake backends
class FakeSearch:
    """Deterministic stand-in for OpenSearch: a fixed hit list (IDs only, as agent E's search returns)."""

    def __init__(self, hits: list[dict[str, Any]], built_from_snapshot_id: str = SNAPSHOT_ID) -> None:
        self.hits = hits
        self.built_from = built_from_snapshot_id
        self.requests: list[dict[str, Any]] = []

    def search(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        size, offset = request.get("size", 20), request.get("offset", 0)
        hits = [{**h, "rank": i} for i, h in enumerate(self.hits[offset:offset + size], offset + 1)]
        return {"query": request["query"], "kinds": list(request["kinds"]), "hits": hits,
                "totals": {k: len(self.hits) for k in request["kinds"]}, "availability_counts": {},
                "fusion": "NONE", "warnings": []}

    def status(self) -> dict[str, Any]:
        return {"aliases": {"blocks": {"build_id": "b-test", "built_from_snapshot_id": self.built_from, "count": 5},
                            "figures": {"build_id": "b-test", "built_from_snapshot_id": self.built_from, "count": 1}},
                "consistent_snapshot": True, "server": {"version": "3.8.0"}}


class FakeHybrid:
    """Stand-in for the hybrid backend: fused hits with a stage trace (ids only), or a configured failure."""

    def __init__(self, hits: list[dict[str, Any]], *, fail: Any = None,
                 built_from_snapshot_id: str = SNAPSHOT_ID) -> None:
        self.hits, self.fail, self.built_from = hits, fail, built_from_snapshot_id
        self.requests: list[dict[str, Any]] = []

    def search(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        if self.fail is not None:
            raise self.fail
        size, offset = request.get("size", 20), request.get("offset", 0)
        hits = [{**h, "rank": i} for i, h in enumerate(self.hits[offset:offset + size], offset + 1)]
        return {"query": request["query"], "kinds": list(request["kinds"]), "hits": hits, "fusion": "RRF",
                "rrf_k": 60, "candidates": request.get("candidates", 100), "fused_total": len(self.hits),
                "totals": {k: {"bm25": len(self.hits)} for k in request["kinds"]}, "warnings": [],
                "stages": {"dense": {"alias": "vkm-vectors", "index": "vkm-vectors-m1-v-test", "build_id": "v-test",
                                     "built_from_snapshot_id": self.built_from, "model_key": "granite-311m-r2",
                                     "query_model": "granite-311m-r2", "dimension": 4}},
                "timings_ms": {"total": 1.0}}


def hybrid_hits(canon: SyntheticCanon) -> list[dict[str, Any]]:
    """Fused hits of a synthetic hybrid query: a page found by both stages, a page found only by the dense stage
    (with its unit's blocks), a figure and one stale id."""
    return [
        {"id": "VKM-SRC-001:p0001", "object_type": "PAGE", "index": "vkm-pages-m1-b-test", "build_id": "b-test",
         "score": 0.0328, "source_id": "VKM-SRC-001", "page_id": "VKM-SRC-001:p0001", "page_index": 1,
         "highlights": ["<em>Оседание</em> земной поверхности"], "best_blocks": [],
         "trace": {"bm25_rank": 1, "bm25_score": 7.5, "dense_rank": 1, "dense_score": 0.91, "fused_rank": 1,
                   "rrf_score": 0.0328, "rrf_k": 60,
                   "dense_unit": {"unit_id": "u1-0000000000000001", "unit_kind": "BLOCK_GROUP",
                                  "object_ids": [canon.ids["block"], canon.ids["block2"]]}}},
        {"id": "VKM-SRC-002:p0001", "object_type": "PAGE", "index": "vkm-vectors-m1-v-test", "build_id": "v-test",
         "score": 0.0161, "source_id": "VKM-SRC-002", "page_id": "VKM-SRC-002:p0001", "page_index": 1,
         "trace": {"bm25_rank": None, "dense_rank": 2, "dense_score": 0.83, "fused_rank": 2, "rrf_score": 0.0161,
                   "rrf_k": 60, "dense_unit": {"unit_id": "u1-0000000000000002", "unit_kind": "BLOCK_GROUP",
                                               "object_ids": ["VKM-SRC-002:p0001:b000000000001"]}}},
        {"id": canon.ids["figure"], "object_type": "FIGURE", "index": "vkm-figures-m1-b-test", "build_id": "b-test",
         "score": 0.0159, "source_id": "VKM-SRC-001", "page_id": "VKM-SRC-001:p0002", "page_index": 2,
         "trace": {"bm25_rank": 3, "bm25_score": 2.0, "dense_rank": None, "fused_rank": 3, "rrf_score": 0.0159,
                   "rrf_k": 60}},
        {"id": "VKM-SRC-001:p0009", "object_type": "PAGE", "index": "vkm-vectors-m1-v-test", "build_id": "v-test",
         "score": 0.01, "source_id": "VKM-SRC-001", "page_id": "VKM-SRC-001:p0009",
         "trace": {"dense_rank": 4, "fused_rank": 4, "rrf_score": 0.01}},
    ]


def search_hits(canon: SyntheticCanon) -> list[dict[str, Any]]:
    """Hits of a synthetic query: a collapsed page (best blocks), a page, a figure and one stale id."""
    return [
        {"id": canon.ids["block"], "object_type": "BLOCK", "index": "vkm-blocks-m1-b-test", "build_id": "b-test",
         "score": 7.5, "source_id": "VKM-SRC-001", "page_id": "VKM-SRC-001:p0001", "page_index": 1,
         "highlights": ["<em>Оседание</em> земной поверхности"],
         "best_blocks": [{"id": canon.ids["block2"], "reading_order": 2},
                         {"id": canon.ids["block"], "reading_order": 1}]},
        {"id": "VKM-SRC-002:p0001", "object_type": "PAGE", "index": "vkm-pages-m1-b-test", "build_id": "b-test",
         "score": 3.1, "source_id": "VKM-SRC-002", "page_id": "VKM-SRC-002:p0001", "page_index": 1},
        {"id": canon.ids["figure"], "object_type": "FIGURE", "index": "vkm-figures-m1-b-test", "build_id": "b-test",
         "score": 2.0, "source_id": "VKM-SRC-001", "page_id": "VKM-SRC-001:p0002", "page_index": 2,
         "fields": {"preview_artifact_id": canon.ids["figure_crop"]}},
        {"id": "VKM-SRC-001:p0009:b000000000000", "object_type": "BLOCK", "index": "vkm-blocks-m1-b-test",
         "build_id": "b-test", "score": 1.0, "source_id": "VKM-SRC-001", "page_id": "VKM-SRC-001:p0009"},
    ]


class FakeGraph:
    def __init__(self, canon: SyntheticCanon, state: str = "READY") -> None:
        self.canon = canon
        self.state_name = state

    def state(self) -> dict[str, Any]:
        if self.state_name != "READY":
            return {"state": self.state_name, "http_status": 503, "build_id": "g-test"}
        return {"state": "READY", "http_status": 200, "build_id": "g-test", "built_from_snapshot_id": SNAPSHOT_ID}

    def page_neighbors(self, page_id: str) -> dict[str, Any] | None:
        index = int(page_id[-4:])
        source = page_id.split(":")[0]
        objects = {"VKM-SRC-001:p0002": [{"rel": "HAS_FIGURE", "id": self.canon.ids["figure"]},
                                         {"rel": "HAS_TABLE", "id": self.canon.ids["table"]}],
                   "VKM-SRC-001:p0001": [{"rel": "HAS_BLOCK", "id": self.canon.ids["block"]}]}.get(page_id, [])
        return {"page_id": page_id, "prev_page_id": f"{source}:p{index - 1:04d}" if index > 1 else None,
                "next_page_id": f"{source}:p{index + 1:04d}" if source == "VKM-SRC-001" and index < 3 else None,
                "source_id": source, "work_id": "VKM-WRK-001", "objects": objects}

    def citations(self, work_id: str, direction: str) -> list[dict[str, Any]]:
        if direction == "cites" and work_id == "VKM-WRK-001":
            return [{"cited_work_id": "VKM-WRK-050", "n_citing_entries": 1}]
        if direction != "cites" and work_id == "VKM-WRK-050":
            return [{"citing_work_id": "VKM-WRK-001", "n_citing_entries": 1}]
        return []


class FakeRerank:
    """Scores candidates by the number of query words in their text (text) or by image size (visual)."""

    def __init__(self) -> None:
        self.text_calls: list[list[tuple[str, str]]] = []
        self.visual_calls: list[list[tuple[str, bytes]]] = []

    @staticmethod
    def _response(kind: str, scored: list[tuple[str, float]], extra: dict[str, dict[str, Any]],
                  top_n: int | None) -> dict[str, Any]:
        ranked = sorted(scored, key=lambda x: (-x[1], x[0]))[:top_n or len(scored)]
        results = [{"id": cid, "rank": i, "score": score, "input_index": 0, **extra.get(cid, {})}
                   for i, (cid, score) in enumerate(ranked, 1)]
        sha = hashlib.sha256(b"fake").hexdigest()
        return {"contract": "vkm.rerank/1", "kind": kind, "request_id": "r", "model_id": f"fake/{kind}-reranker",
                "model_revision": "0" * 40, "quant": "none", "placement": "cpu", "backend": "fake",
                "backend_version": "0", "model_config_sha256": sha, "score_semantics": "fake", "license": "none",
                "candidate_ids": [c for c, _s in ranked], "scores": [s for _c, s in ranked], "results": results,
                "n_candidates": len(scored), "top_n": len(ranked), "query_sha256": sha, "input_sha256": sha,
                "latency_ms": {"total": 1.0, "preprocess": 0.1, "queue": 0.0, "backend": 0.9},
                "layer": "SERVICE", "review_status": "NOT_APPLICABLE", "gateway_version": "fake",
                "created_at": "2026-09-28T10:00:00Z", "warnings": []}

    async def rerank_text(self, query: str, candidates: list[tuple[str, str]], top_n: int | None,
                          request_id: str | None, truncate_to_tokens: int | None) -> dict[str, Any]:
        self.text_calls.append(list(candidates))
        words = [w for w in query.lower().split() if w]
        scored = [(cid, float(sum(text.lower().count(w) for w in words))) for cid, text in candidates]
        extra = {cid: {"input_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                       "text_chars": len(text), "truncated": False} for cid, text in candidates}
        return self._response("text", scored, extra, top_n)

    async def rerank_visual(self, query: str, candidates: list[tuple[str, bytes]], top_n: int | None,
                            request_id: str | None) -> dict[str, Any]:
        self.visual_calls.append(list(candidates))
        scored = [(cid, float(len(data) % 997) / 997) for cid, data in candidates]
        extra = {cid: {"source_image_sha256": hashlib.sha256(data).hexdigest()} for cid, data in candidates}
        return self._response("visual", scored, extra, top_n)

    async def status(self) -> dict[str, Any]:
        return {"gateway_version": "fake",
                "backends": {"text": {"kind": "text", "status": "ready", "model_id": "fake/text-reranker",
                                      "license": "none"},
                             "visual": {"kind": "visual", "status": "ready", "model_id": "fake/visual-reranker",
                                        "license": "none"}}}


class FakeControlPlane:
    """In-memory plan-first jobs with the lifecycle of ``vkm_corpus.ops.jobs`` (the worker is simulated)."""

    def __init__(self) -> None:
        self.jobs: dict[int, dict[str, Any]] = {}
        self.available = True

    def _check(self) -> None:
        if not self.available:
            from vkm_corpus.api.errors import ApiFailure

            raise ApiFailure("DEPENDENCY_UNAVAILABLE", "control plane not reachable", stage="control_plane",
                             tool="postgres")

    def request_plan(self, kind: str, requested_by: str, *, source_id: str | None, page_id: str | None,
                     request: dict[str, Any]) -> int:
        self._check()
        job_id = len(self.jobs) + 1
        self.jobs[job_id] = {"job_id": job_id, "kind": kind, "state": "PLAN_REQUESTED", "source_id": source_id,
                             "page_id": page_id, "request": request, "requested_by": requested_by, "attempts": 0}
        return job_id

    def job(self, job_id: int) -> dict[str, Any] | None:
        self._check()
        job = self.jobs.get(job_id)
        return dict(job) if job else None

    def active_jobs(self, kind: str) -> list[dict[str, Any]]:
        self._check()
        return [dict(j) for j in self.jobs.values() if j["kind"] == kind and j["state"] in (
            "PLAN_REQUESTED", "PLANNED", "CONFIRMED", "RUNNING", "PUBLISHED")]

    def worker_plans(self, job_id: int, plan: dict[str, Any]) -> str:
        """Simulated worker: publish the plan (as ``ops.jobs.record_plan``)."""
        from vkm_corpus.ops.jobs import plan_sha256

        digest = plan_sha256(plan)
        self.jobs[job_id].update(state="PLANNED", plan=plan, plan_sha256=digest, planned_by="fake-worker")
        return digest

    def confirm(self, job_id: int, plan_sha256: str, confirmed_by: str) -> dict[str, Any]:
        self._check()
        job = self.jobs[job_id]
        if job["state"] != "PLANNED" or job.get("plan_sha256") != plan_sha256:
            from vkm_corpus.api.errors import ApiFailure

            raise ApiFailure("PLAN_CHANGED", "stale plan")
        job.update(state="CONFIRMED", confirmed_plan_sha256=plan_sha256, confirmed_by=confirmed_by)
        return dict(job)

    def cancel(self, job_id: int, by: str, note: str) -> dict[str, Any]:
        """As ``ops.jobs.cancel``: only PLAN_REQUESTED, PLANNED or CONFIRMED jobs can be cancelled."""
        self._check()
        job = self.jobs[job_id]
        if job["state"] not in ("PLAN_REQUESTED", "PLANNED", "CONFIRMED"):
            from vkm_corpus.api.errors import ApiFailure

            raise ApiFailure("JOB_STATE_CONFLICT", f"job {job_id}: illegal transition {job['state']} → CANCELLED")
        job.update(state="CANCELLED", note=f"cancelled by {by}: {note}".strip())
        return dict(job)

    def status(self) -> dict[str, Any]:
        self._check()
        counts: dict[str, int] = {}
        for job in self.jobs.values():
            counts[job["state"]] = counts.get(job["state"], 0) + 1
        return {"jobs": counts, "workers": [{"host_role": "WORKSTATION", "kind": "PIPELINE", "state": "IDLE"}],
                "errors_24h": 0, "schema_version": "ops-0.1.0"}


def synthetic_service(directory: Path, **overrides: Any) -> tuple[Any, SyntheticCanon, dict[str, Any]]:
    """An ``ApiService`` over a fresh synthetic canon with fake backends (tests, local §60 dry run)."""
    from vkm_corpus.api.backends import ArtifactBlobs
    from vkm_corpus.api.canon import CanonStore
    from vkm_corpus.api.service import ApiDeps, ApiService

    canon = build_synthetic_canon(directory)
    fakes: dict[str, Any] = {"search": FakeSearch(search_hits(canon)), "graph": FakeGraph(canon),
                             "rerank": FakeRerank(), "control": FakeControlPlane()}
    fakes.update(overrides)
    deps = ApiDeps(canon=CanonStore(canon.duckdb_path), blobs=ArtifactBlobs(canon.artifacts_root), **fakes)
    return ApiService(deps), canon, fakes

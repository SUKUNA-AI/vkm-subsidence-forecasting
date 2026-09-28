"""Factories of minimal *valid* canonical rows (synthetic values only) for tests of every agent.

Each ``make_<dataset>(**overrides)`` returns a validated row model; ``row_dict`` returns the underlying dict so that a
test can break one field and check the validation error. Text is synthetic (never taken from sources).
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from typing import Any

from vkm_corpus import ids
from vkm_corpus.contracts.builders import (
    ProducerContext,
    SourceContext,
    build_row,
    content_sha256,
    doc_envelope,
)
from vkm_corpus.contracts.datasets import dataset
from vkm_corpus.contracts.text_rules import normalize_text_v1, page_text_v1
from vkm_corpus.versions import PIPELINE_VERSION

T0 = datetime(2026, 9, 28, 10, 0, 0, tzinfo=timezone.utc)
RUN_ID = "RUN-20260928T100000Z-0a0b0c0d"
SID = "VKM-SRC-001"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


CONFIG_HASH = sha("synthetic-config")
RAW_CONFIG_HASH = sha("synthetic-raw-config")
SOURCE_SHA = sha("synthetic-source-001")
GLM = {"role": "RECOGNITION", "model_id": "zai-org/GLM-OCR", "model_revision": "0" * 40}
LAYOUT = {"role": "LAYOUT", "model_id": "PaddlePaddle/PP-DocLayoutV3", "model_revision": "1" * 40}


def artifact(tag: str) -> str:
    return ids.artifact_id(tag.encode("utf-8"))


def source_context(sid: str = SID, source_sha: str = SOURCE_SHA) -> SourceContext:
    return SourceContext(sid, source_sha, ("VKM_REGIONAL",), "VKM_regional", "CASE")


def producer(extractor: str = "synthetic-native", models: tuple = ()) -> ProducerContext:
    return ProducerContext(PIPELINE_VERSION, RUN_ID, extractor, "0.0.1", CONFIG_HASH, RAW_CONFIG_HASH).with_models(
        *models)


def _env(dataset_name: str, *, object_id: str, page_id: str | None, origin: str = "NATIVE",
         prod: ProducerContext | None = None, src: SourceContext | None = None, **extra) -> dict:
    spec = dataset(dataset_name)
    env = doc_envelope(src or source_context(), prod or producer(), object_kind=spec.object_kind,
                       object_id=object_id, page_id=page_id, origin=origin, created_at=T0, **extra)
    env["schema_version"] = spec.version
    return env


# ---------------------------------------------------------------- document datasets
def page_dict(index: int = 1, sid: str = SID, text: str | None = "альфа бета гамма", **overrides) -> dict:
    pid = ids.page_id(sid, "p", index)
    pt = page_text_v1([{"object_id": f"{pid}:b000000000000", "reading_order": 1, "normalized_text": text,
                        "block_type": "TEXT", "is_primary_layer": True}]) if text else None
    d = _env("pages", object_id=pid, page_id=pid, raw_artifact_id=artifact(f"native-raw-{sid}-{index}"),
             src=source_context(sid))
    d.update(page_index=index, page_kind="PDF_PAGE", page_class="NATIVE_TEXT", page_route="NATIVE",
             width_pt=595.3, height_pt=841.9, rotation_deg=0, page_box="CROPBOX", bbox_space="PAGE_PT_TL",
             file_text_layer="PDF_TEXT_LAYER", file_text_status="PRESENT_OK",
             file_text_char_count=len(text or ""), ocr_status="NOT_REQUIRED",
             page_status="NATIVE_OK" if text else "NEEDS_REVIEW",
             primary_text_layer="PDF_TEXT_LAYER" if text else "NONE",
             primary_text_origin="NATIVE" if text else None,
             normalized_text=pt.normalized_text if pt else None, text_rule="page_text_v1",
             text_sha256=pt.text_sha256 if pt else None, char_count=pt.char_count if pt else 0)
    d.update(overrides)
    return d


def make_page(**overrides):
    return build_row("pages", page_dict(**overrides))


def block_dict(page_index: int = 1, order: int = 1, text: str = "альфа бета гамма", sid: str = SID,
               **overrides) -> dict:
    pid = ids.page_id(sid, "p", page_index)
    prod = producer()
    bbox = (56.7, 60.0 + 20 * order, 538.6, 75.0 + 20 * order)
    oid = ids.object_id(pid, "BLOCK", "NATIVE", "PDF_TEXT_BLOCK", ids.bbox_anchor(*bbox), prod.producer_key())
    d = _env("blocks", object_id=oid, page_id=pid, prod=prod, src=source_context(sid),
             raw_artifact_id=artifact(f"native-raw-{sid}-{page_index}"), raw_content_sha256=sha(text))
    d.update(region_origin="PDF_TEXT_BLOCK", text_layer="PDF_TEXT_LAYER", bbox_x0=bbox[0], bbox_y0=bbox[1],
             bbox_x1=bbox[2], bbox_y1=bbox[3], bbox_space="PAGE_PT_TL", is_primary_layer=True, block_type="TEXT",
             reading_order=order, text=text, normalized_text=normalize_text_v1(text), char_count=len(text),
             language="ru")
    d.update(overrides)
    return d


def make_block(**overrides):
    return build_row("blocks", block_dict(**overrides))


def figure_dict(page_index: int = 1, sid: str = SID, **overrides) -> dict:
    pid = ids.page_id(sid, "p", page_index)
    prod = producer("synthetic-layout", models=(LAYOUT,))
    bbox = (100.0, 300.0, 400.0, 500.0)
    oid = ids.object_id(pid, "FIGURE", "NATIVE", "LAYOUT_MODEL", ids.bbox_anchor(*bbox), prod.producer_key())
    d = _env("figures", object_id=oid, page_id=pid, prod=prod, src=source_context(sid),
             raw_artifact_id=artifact(f"layout-raw-{sid}-{page_index}"))
    d.update(region_origin="LAYOUT_MODEL", text_layer="PDF_TEXT_LAYER", bbox_x0=bbox[0], bbox_y0=bbox[1],
             bbox_x1=bbox[2], bbox_y1=bbox[3], bbox_space="PAGE_PT_TL", figure_label="Рис. 1",
             caption="Рис. 1. Синтетическая схема", caption_normalized="Рис. 1. Синтетическая схема",
             layout_class="VECTOR_GRAPHICS", layout_score=0.91,
             image_artifact_id=artifact(f"figure-crop-{sid}-{page_index}"), image_dpi=200,
             vector_artifacts=[{"format": "PATHS_JSON", "artifact_id": artifact(f"paths-{sid}-{page_index}")},
                               {"format": "SVG", "artifact_id": artifact(f"svg-{sid}-{page_index}")}])
    d.update(overrides)
    return d


def make_figure(**overrides):
    return build_row("figures", figure_dict(**overrides))


def table_dict(page_index: int = 1, sid: str = SID, **overrides) -> dict:
    pid = ids.page_id(sid, "p", page_index)
    prod = producer("synthetic-tables")
    bbox = (60.0, 520.0, 540.0, 640.0)
    oid = ids.object_id(pid, "TABLE", "NATIVE", "NATIVE_TABLE_FINDER", ids.bbox_anchor(*bbox), prod.producer_key())
    raw = "<table><tr><td>a</td><td>1</td></tr></table>"
    d = _env("tables", object_id=oid, page_id=pid, prod=prod, src=source_context(sid),
             raw_artifact_id=artifact(f"table-raw-{sid}-{page_index}"), raw_content_sha256=sha(raw),
             raw_artifacts=[{"role": "NATIVE_TABLE_FINDER", "artifact_id": artifact(f"table-raw-{sid}-{page_index}")}])
    d.update(region_origin="NATIVE_TABLE_FINDER", text_layer="PDF_TEXT_LAYER", bbox_x0=bbox[0], bbox_y0=bbox[1],
             bbox_x1=bbox[2], bbox_y1=bbox[3], bbox_space="PAGE_PT_TL", table_label="Таблица 1",
             recognition_method="NATIVE_FIND_TABLES", raw_format="HTML", raw_output=raw, n_rows=1, n_cols=2,
             cells=[{"row": 0, "col": 0, "text": "a"}, {"row": 0, "col": 1, "text": "1"}],
             normalized_text="a | 1")
    d.update(overrides)
    return d


def make_table(**overrides):
    return build_row("tables", table_dict(**overrides))


def formula_dict(page_index: int = 1, sid: str = SID, **overrides) -> dict:
    pid = ids.page_id(sid, "p", page_index)
    prod = producer("synthetic-ocr", models=(LAYOUT, GLM))
    bbox = (200.0, 650.0, 400.0, 680.0)
    oid = ids.object_id(pid, "FORMULA", "OCR", "LAYOUT_MODEL", ids.bbox_anchor(*bbox), prod.producer_key())
    d = _env("formulas", object_id=oid, page_id=pid, origin="OCR", prod=prod, src=source_context(sid),
             raw_artifact_id=artifact(f"ocr-raw-{sid}-{page_index}"), raw_content_sha256=sha("E = m c^2"))
    d.update(region_origin="LAYOUT_MODEL", text_layer="GLM_OCR", bbox_x0=bbox[0], bbox_y0=bbox[1], bbox_x1=bbox[2],
             bbox_y1=bbox[3], bbox_space="PAGE_PT_TL", formula_kind="DISPLAY", equation_label="(1)",
             recognition_method="OCR_GLM", raw_format="LATEX", raw_output="$$E = m c^2$$",
             normalized_latex="E = m c^2", latex_parse_ok=True, native_glyph_text="E=mc2")
    d.update(overrides)
    return d


def make_formula(**overrides):
    return build_row("formulas", formula_dict(**overrides))


def bibliography_dict(page_index: int = 1, sid: str = SID, ordinal: int = 1, text: str | None = None,
                      **overrides) -> dict:
    pid = ids.page_id(sid, "p", page_index)
    prod = producer("synthetic-bib")
    text = text or f"{ordinal}. Альфаев А.А. Синтетическая статья // Вестник синтетики. 2020. doi:10.9999/synthetic.00{ordinal}"
    bbox = (56.0, 100.0 + 30 * ordinal, 540.0, 125.0 + 30 * ordinal)
    oid = ids.object_id(pid, "BIBLIOGRAPHY_ENTRY", "NATIVE", "PDF_TEXT_BLOCK", ids.bbox_anchor(*bbox),
                        prod.producer_key())
    d = _env("bibliography_entries", object_id=oid, page_id=pid, prod=prod, src=source_context(sid),
             raw_artifact_id=artifact(f"native-raw-{sid}-{page_index}"), raw_content_sha256=sha(text))
    d.update(region_origin="PDF_TEXT_BLOCK", text_layer="PDF_TEXT_LAYER", bbox_x0=bbox[0], bbox_y0=bbox[1],
             bbox_x1=bbox[2], bbox_y1=bbox[3], bbox_space="PAGE_PT_TL", entry_label=str(ordinal),
             ordinal_in_list=ordinal, text=text, normalized_text=normalize_text_v1(text),
             parsed_authors=["Альфаев А.А."], parsed_title="Синтетическая статья", parsed_year=2020,
             parsed_year_raw="2020", parsed_doi=f"10.9999/synthetic.00{ordinal}", parse_method="regex-v1",
             parse_confidence=0.9, language="ru")
    d.update(overrides)
    return d


def make_bibliography_entry(**overrides):
    return build_row("bibliography_entries", bibliography_dict(**overrides))


def document_dict(sid: str = SID, page_count: int = 1, **overrides) -> dict:
    d = _env("documents", object_id=ids.document_id(sid), page_id=None, src=source_context(sid))
    d.update(format_detected="PDF", format_version="1.7", pagination_basis="PDF_PAGE_TREE", page_unit="p",
             page_count=page_count, page_count_check=page_count, page_count_check_method="pypdfium2",
             document_class="NATIVE", classifier_version="synthetic-1", is_encrypted=False,
             text_extraction_permitted=True, has_native_page_labels=False, processing_status="COMPLETE")
    d.update(overrides)
    return d


def make_document(**overrides):
    return build_row("documents", document_dict(**overrides))


# ---------------------------------------------------------------- registry datasets
def _reg_env(dataset_name: str, object_id: str, *, review_status: str = "NOT_APPLICABLE", origin: str = "REGISTRY",
             input_ref: str = "PRIVATE:00_registry/SOURCE_REGISTER.csv", input_row: int | None = 1) -> dict:
    spec = dataset(dataset_name)
    return {"schema_version": spec.version, "object_id": object_id, "object_kind": spec.object_kind,
            "origin": origin, "pipeline_version": PIPELINE_VERSION, "processing_run_id": RUN_ID,
            "extractor_id": "registry-import", "extractor_version": PIPELINE_VERSION, "config_hash": CONFIG_HASH,
            "created_at": T0, "review_status": review_status, "quality_flags": [], "input_ref": input_ref,
            "input_sha256": sha("synthetic-register"), "input_row": input_row}


def source_dict(sid: str = SID, **overrides) -> dict:
    n = ids.source_number(sid)
    d = _reg_env("sources", sid, review_status="FULLY_REVIEWED" if n <= 41 else "UNSEEN", input_row=n)
    d.update(source_id=sid, source_sha256=SOURCE_SHA if sid == SID else sha(f"synthetic-source-{n:03d}"),
             size_bytes=1000 + n, canonical_path=f"04_articles/synthetic_{n:03d}.pdf",
             original_filename=f"synthetic_{n:03d}.pdf", file_extension="pdf", format_detected="PDF",
             file_status="PRESENT_VERIFIED", lifecycle_status="ACTIVE", source_class_raw="journal_article",
             priority="A", site_scope_raw="VKM_regional", site_scope=["VKM_REGIONAL"], site_scope_mapping="CASE",
             site_scope_map_version="1",
             review_status_basis="PHASE1_COVERAGE_MASTER" if n <= 41 else "DEFAULT_UNSEEN",
             evidence_coverage_raw="FULLY_REVIEWED" if n <= 41 else None, register_scientific_role="synthetic",
             register_migration_source="synthetic", register_migration_status="ADDED_BY_USER_EXACT",
             register_notes="", register_row_sha256=sha(f"row-{sid}"))
    d["observed_sha256"] = d["source_sha256"]
    d["observed_size_bytes"] = d["size_bytes"]
    d.update(overrides)
    return d


def make_source(**overrides):
    return build_row("sources", source_dict(**overrides))


def work_dict(anchor: str = SID, **overrides) -> dict:
    wid = ids.work_id(anchor)
    d = _reg_env("works", wid, input_ref="PRIVATE:00_registry/work_registry/WORK_REGISTER.csv")
    d.update(work_id=wid, status="ACTIVE", anchor_source_id=anchor, work_type="JOURNAL_ARTICLE",
             title="Синтетическая статья", authors_display="Альфаев А.А.; Бетин Б.Б.", publication_year=2020,
             publication_year_raw="2020", publication_date=date(2020, 1, 1), publication_date_precision="year",
             doi="10.9999/synthetic.001", languages=["ru"],
             metadata_basis={"title": "HUNT_TABLE", "authors": "HUNT_TABLE", "year": "HUNT_TABLE",
                             "venue": "UNKNOWN", "identifiers": "HUNT_TABLE"},
             identity_status="CATALOGUE_UNVERIFIED", curation_status="CURATED")
    d.update(overrides)
    return d


def make_work(**overrides):
    return build_row("works", work_dict(**overrides))


def reg_env(dataset_name: str, object_id: str, **kw: Any) -> dict:
    """Envelope of a registry/link row (for tests and the synthetic canon)."""
    return _reg_env(dataset_name, object_id, **kw)


__all__ = ["T0", "RUN_ID", "SID", "sha", "artifact", "source_context", "producer", "page_dict", "make_page",
           "block_dict", "make_block", "figure_dict", "make_figure", "table_dict", "make_table", "formula_dict",
           "make_formula", "bibliography_dict", "make_bibliography_entry", "document_dict", "make_document",
           "source_dict", "make_source", "work_dict", "make_work", "reg_env", "content_sha256", "GLM", "LAYOUT",
           "CONFIG_HASH", "RAW_CONFIG_HASH"]

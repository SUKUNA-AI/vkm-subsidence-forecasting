"""Projection input → search documents (one canonical row = exactly one document, including failed pages).

Documents are generated sorted by ``id``; absent values are omitted (no null placeholders under the strict mapping).
Denormalised fields come from the same rules as the graph (``vkm_corpus.graph.rules``): the work of a document is the
``INSTANCE_OF`` work of its source (``work_id``), foreign pages list ``foreign_content_work_ids`` (H-16), the source
area is ``source_site_scope*`` flagged ``SCOPE_INHERITED_FROM_SOURCE`` (H-18), availability is
``available_latest_day`` + ``available_basis`` (H-19), ``work_copy_count`` marks copies of one work (H-49).
"""
from __future__ import annotations

import re
from typing import Any, Iterator

from vkm_corpus.contracts import vocab
from vkm_corpus.graph.canon import ProjectionInput
from vkm_corpus.graph.common import StreamDigest, canonical_json, normalize_value
from vkm_corpus.graph.rules import ensure_rule_views
from vkm_corpus.search.mappings import INDEX_TYPES, OBJECT_KIND, properties

SCOPE_FLAG = vocab.QualityFlag.SCOPE_INHERITED_FROM_SOURCE.value
FOREIGN_FLAG = "FOREIGN_CONTENT"                  # page carries content of another work (identified or not)
FOREIGN_UNIDENTIFIED_FLAG = "FOREIGN_CONTENT_UNIDENTIFIED"
WORK_UNKNOWN_FLAG = "WORK_UNKNOWN"                # the source has no INSTANCE_OF work
PROJECTION_FLAGS = (SCOPE_FLAG, FOREIGN_FLAG, FOREIGN_UNIDENTIFIED_FLAG, WORK_UNKNOWN_FLAG)

_NUMBER_RE = re.compile(r"(\d+(?:[.\-–]\d+)*[a-zа-я]?)", re.IGNORECASE)

_CONTEXT = """
    src AS (SELECT s.source_id, s.site_scope, s.site_scope_raw, s.site_scope_mapping, i.work_id
            FROM e_sources s LEFT JOIN x_instance_links i ON i.source_id = s.source_id),
    pg AS (SELECT p.page_id, p.page_index, p.printed_page_labels, p.printed_page_raw,
                  coalesce(d.dup_group_id, p.page_id) AS dup_group_id,
                  coalesce(f.foreign_content_work_ids, []::VARCHAR[]) AS foreign_content_work_ids,
                  (f.page_id IS NOT NULL) AS has_foreign, coalesce(f.has_unidentified_foreign, false) AS has_unidentified
           FROM e_pages p LEFT JOIN x_page_dup d ON d.page_id = p.page_id
           LEFT JOIN x_page_foreign f ON f.page_id = p.page_id)"""

_SHARED_COLS = """
    src.work_id, src.site_scope AS source_site_scope, src.site_scope_raw AS source_site_scope_raw,
    src.site_scope_mapping AS source_site_scope_mapping,
    wm.authors, wm.author_ids, wm.title AS work_title, wm.publication_year AS year, wm.languages AS work_languages,
    wm.available_latest_day, coalesce(wm.available_basis, 'UNKNOWN') AS available_basis,
    coalesce(wm.work_copy_count, 0) AS work_copy_count,
    pg.page_index, pg.printed_page_labels AS page_label, pg.printed_page_raw AS page_label_raw, pg.dup_group_id,
    pg.foreign_content_work_ids, pg.has_foreign, pg.has_unidentified"""

_JOINS = """
    LEFT JOIN src ON src.source_id = o.source_id
    LEFT JOIN x_work_meta wm ON wm.work_id = src.work_id
    LEFT JOIN pg ON pg.page_id = o.page_id"""

DOC_SQL: dict[str, str] = {
    "pages": f"""WITH {_CONTEXT},
        bl AS (SELECT page_id, list_sort(list_distinct(list(language) FILTER (
                   WHERE language IS NOT NULL AND is_primary_layer IS NOT FALSE))) AS languages
               FROM e_blocks GROUP BY page_id)
        SELECT o.page_id AS id, o.source_id, o.page_id, {_SHARED_COLS}, bl.languages AS object_languages,
               o.text, o.text_sha256, o.origin, o.review_status, o.quality_flags, o.preview_artifact_id,
               o.schema_version, o.processing_run_id, o.extractor_id, o.model_id, o.model_revision,
               o.page_kind, o.page_class, o.page_status, o.file_text_status, o.ocr_status, o.primary_text_layer,
               o.primary_text_origin, o.text_rule, o.is_spread
        FROM e_pages o {_JOINS} LEFT JOIN bl ON bl.page_id = o.page_id ORDER BY id""",
    "blocks": f"""WITH {_CONTEXT}
        SELECT o.block_id AS id, o.source_id, o.page_id, {_SHARED_COLS}, o.language AS object_language,
               o.text, o.text_sha256, o.is_primary_layer, o.origin, o.text_layer, o.review_status, o.quality_flags,
               o.schema_version, o.processing_run_id, o.extractor_id, o.model_id, o.model_revision,
               o.block_type, o.reading_order, o.region_origin, o.bbox_x0, o.bbox_y0, o.bbox_x1, o.bbox_y1, o.bbox_space
        FROM e_blocks o {_JOINS} ORDER BY id""",
    "figures": f"""WITH {_CONTEXT}
        SELECT o.figure_id AS id, o.source_id, o.page_id, {_SHARED_COLS},
               o.caption, o.figure_label, o.is_primary_layer, o.origin, o.review_status, o.quality_flags,
               o.image_artifact_id AS preview_artifact_id, o.schema_version, o.processing_run_id, o.extractor_id,
               o.model_id, o.model_revision, o.figure_type, o.figure_type_method, o.figure_type_confidence,
               o.layout_class, o.region_origin, o.embedded_image_artifact_id, o.vector_artifact_ids,
               o.caption_block_id, o.bbox_x0, o.bbox_y0, o.bbox_x1, o.bbox_y1, o.bbox_space
        FROM e_figures o {_JOINS} ORDER BY id""",
    "tables": f"""WITH {_CONTEXT}
        SELECT o.table_id AS id, o.source_id, o.page_id, {_SHARED_COLS},
               o.caption, o.table_label, o.text, o.is_primary_layer, o.origin, o.review_status, o.quality_flags,
               o.image_artifact_id AS preview_artifact_id, o.schema_version, o.processing_run_id, o.extractor_id,
               o.model_id, o.model_revision, o.n_rows, o.n_cols, o.raw_format, o.recognition_method, o.region_origin,
               o.bbox_x0, o.bbox_y0, o.bbox_x1, o.bbox_y1, o.bbox_space
        FROM e_tables o {_JOINS} ORDER BY id""",
    "formulas": f"""WITH {_CONTEXT}
        SELECT o.formula_id AS id, o.source_id, o.page_id, {_SHARED_COLS},
               o.latex AS recognized_latex, o.text, o.equation_label, o.is_primary_layer, o.origin, o.review_status,
               o.quality_flags, o.image_artifact_id AS preview_artifact_id, o.schema_version, o.processing_run_id,
               o.extractor_id, o.model_id, o.model_revision, o.formula_kind, o.raw_format, o.recognition_method,
               o.region_origin, o.bbox_x0, o.bbox_y0, o.bbox_x1, o.bbox_y1, o.bbox_space
        FROM e_formulas o {_JOINS} ORDER BY id""",
}

_DIRECT = ("id", "source_id", "work_id", "page_id", "page_index", "page_label", "page_label_raw", "authors",
           "author_ids", "work_title", "year", "text", "text_sha256", "is_primary_layer", "origin", "text_layer",
           "review_status", "quality_flags", "source_site_scope", "source_site_scope_raw", "source_site_scope_mapping",
           "available_latest_day", "available_basis", "foreign_content_work_ids", "work_copy_count",
           "preview_artifact_id", "schema_version", "processing_run_id", "extractor_id", "model_id", "model_revision",
           # type-specific
           "page_kind", "page_class", "page_status", "file_text_status", "ocr_status", "primary_text_layer",
           "primary_text_origin", "text_rule",
           "dup_group_id", "is_spread", "block_type", "reading_order", "region_origin", "caption", "figure_type",
           "figure_type_method", "figure_type_confidence", "layout_class", "embedded_image_artifact_id",
           "vector_artifact_ids", "caption_block_id", "n_rows", "n_cols", "raw_format", "recognition_method",
           "recognized_latex", "formula_kind")


def object_number(label: str | None) -> str | None:
    """Printed number of a figure/table/equation label: «Рис. 3.1» → ``3.1``, «(3.12)» → ``3.12`` (search only)."""
    if not label:
        return None
    match = _NUMBER_RE.search(label)
    return match.group(1).replace("–", "-").lower() if match else None


def _bbox(row: dict[str, Any]) -> dict[str, Any] | None:
    values = [row.get(k) for k in ("bbox_x0", "bbox_y0", "bbox_x1", "bbox_y1")]
    if any(v is None for v in values):
        return None
    return {"x0": float(values[0]), "y0": float(values[1]), "x1": float(values[2]), "y1": float(values[3]),
            "space": row.get("bbox_space")}


def _language(index_type: str, row: dict[str, Any]) -> list[str]:
    if index_type == "blocks" and row.get("object_language"):
        return [row["object_language"]]
    if index_type == "pages" and row.get("object_languages"):
        return list(row["object_languages"])
    return list(row.get("work_languages") or [])


def to_document(index_type: str, row: dict[str, Any], allowed: frozenset[str]) -> dict[str, Any]:
    doc: dict[str, Any] = {"object_type": OBJECT_KIND[index_type]}
    for key in _DIRECT:
        if key in row and key in allowed:
            value = row[key]
            if isinstance(value, list):
                value = [v for v in value if v is not None]
            doc[key] = value
    if index_type in ("pages", "blocks", "tables", "formulas"):
        doc["text_chars"] = len(row.get("text") or "")
    if index_type == "pages":
        doc["is_primary_layer"] = True               # the page text is the primary layer by rule (page_text_v1)
    doc["language"] = _language(index_type, row)
    if index_type in ("figures", "tables"):
        label = row.get("figure_label") if index_type == "figures" else row.get("table_label")
        doc["object_label_raw"] = label
        doc["object_label"] = object_number(label)
    if index_type == "formulas":
        doc["equation_label_raw"] = row.get("equation_label")
        doc["equation_label"] = object_number(row.get("equation_label"))
    if index_type != "pages":
        doc["bbox"] = _bbox(row)
    doc["has_preview"] = bool(row.get("preview_artifact_id"))
    flags = [SCOPE_FLAG]
    if row.get("has_foreign"):
        flags.append(FOREIGN_FLAG)
    if row.get("has_unidentified"):
        flags.append(FOREIGN_UNIDENTIFIED_FLAG)
    if not row.get("work_id"):
        flags.append(WORK_UNKNOWN_FLAG)
    doc["projection_flags"] = flags
    return {k: normalize_value(v) for k, v in doc.items() if v is not None and k in allowed | {"object_type"}}


def iter_documents(inp: ProjectionInput, index_type: str) -> Iterator[dict[str, Any]]:
    ensure_rule_views(inp)
    allowed = frozenset(properties(index_type))
    for row in inp.iter_rows(DOC_SQL[index_type]):
        yield to_document(index_type, row, allowed)


def expected_doc_counts(inp: ProjectionInput) -> dict[str, int]:
    return {t: inp.count({"pages": "e_pages", "blocks": "e_blocks", "figures": "e_figures", "tables": "e_tables",
                          "formulas": "e_formulas"}[t]) for t in INDEX_TYPES}


def doc_line(doc: dict[str, Any]) -> str:
    return canonical_json(doc)


def doc_stream_digest(inp: ProjectionInput, index_type: str) -> tuple[int, str]:
    """(count, sha256) of the document stream of one type — identical for identical snapshots."""
    d = StreamDigest(index_type)
    for doc in iter_documents(inp, index_type):
        d.add((doc["id"],), doc_line(doc))
    return d.count, d.hexdigest()

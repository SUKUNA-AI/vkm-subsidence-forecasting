"""Index bodies of the retrieval projection: one index per object type + aliases (CP-17, H-44).

Names: ``<prefix>-<type>-m<MAPPING_VERSION>-<build_id>``; read aliases ``<prefix>-<type>``; group alias
``<prefix>-objects`` over figures, tables and formulas (ranking across types is fused by rank on the query side, never
by raw BM25 — ``vkm_corpus.search.query``). Mappings are ``dynamic: strict``: a field the code does not declare is
rejected at indexing time, so contract drift cannot pass silently. Common fields have identical names and mappings in
every index (``COMMON``).

Field notes: ``text`` is the normalised text of the primary layer (pages: ``page_text_v1``; blocks carry
``is_primary_layer``, H-30); the source area is ``source_site_scope*`` with the flag ``SCOPE_INHERITED_FROM_SOURCE``
(H-18); availability is ``available_latest_day`` + ``available_basis`` (H-19); ``foreign_content_work_ids`` and
``work_copy_count`` show foreign pages and copies of one work (H-16, H-49); ``preview_artifact_id`` is the image for
visual reranking. Texts are indexed for retrieval only and are never returned as the object's text (the API reads
canonical text from DuckDB).
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime
from typing import Any

from vkm_corpus.search.analysis import ANALYSIS_VERSION, analysis_settings

MAPPING_VERSION = "1"
INDEX_TYPES: tuple[str, ...] = ("pages", "blocks", "figures", "tables", "formulas")
OBJECT_KIND: dict[str, str] = {"pages": "PAGE", "blocks": "BLOCK", "figures": "FIGURE", "tables": "TABLE",
                               "formulas": "FORMULA"}
KIND_INDEX: dict[str, str] = {v: k for k, v in OBJECT_KIND.items()}
GROUP_TYPES: tuple[str, ...] = ("figures", "tables", "formulas")
GROUP_ALIAS_SUFFIX = "objects"

_KW = {"type": "keyword"}
_KW_NORM = {"type": "keyword", "normalizer": "vkm_keyword_norm"}
_NOT_INDEXED = {"type": "keyword", "index": False, "doc_values": False}
_DISABLED = {"type": "object", "enabled": False}


def _text(offsets: bool = False) -> dict[str, Any]:
    field: dict[str, Any] = {"type": "text", "analyzer": "vkm_text",
                             "fields": {"exact": {"type": "text", "analyzer": "vkm_exact"}}}
    if offsets:
        field["index_options"] = "offsets"
    return field


COMMON: dict[str, Any] = {
    "id": _KW, "object_type": _KW, "source_id": _KW, "work_id": _KW, "page_id": _KW,
    "page_index": {"type": "integer"}, "page_label": _KW_NORM, "page_label_raw": _KW,
    "authors": {"type": "text", "analyzer": "vkm_exact",
                "fields": {"stem": {"type": "text", "analyzer": "vkm_text"},
                           "kw": {"type": "keyword", "normalizer": "vkm_keyword_norm", "ignore_above": 256}}},
    "author_ids": _KW, "work_title": _text(), "year": {"type": "short"}, "language": _KW,
    "text": _text(), "text_sha256": _NOT_INDEXED, "text_chars": {"type": "integer"},
    "is_primary_layer": {"type": "boolean"}, "origin": _KW, "text_layer": _KW, "review_status": _KW,
    "quality_flags": _KW, "projection_flags": _KW,
    "source_site_scope": _KW, "source_site_scope_raw": _KW, "source_site_scope_mapping": _KW,
    "available_latest_day": {"type": "date", "format": "strict_date"}, "available_basis": _KW,
    "foreign_content_work_ids": _KW, "work_copy_count": {"type": "short"},
    "preview_artifact_id": _KW, "has_preview": {"type": "boolean"},
    "schema_version": _KW, "processing_run_id": _KW, "extractor_id": _KW, "model_id": _KW, "model_revision": _KW,
}
PER_TYPE: dict[str, dict[str, Any]] = {
    "pages": {"text": _text(offsets=True), "page_kind": _KW, "page_class": _KW, "page_status": _KW,
              "file_text_status": _KW, "ocr_status": _KW, "primary_text_layer": _KW, "primary_text_origin": _KW,
              "text_rule": _KW, "dup_group_id": _KW, "is_spread": {"type": "boolean"}},
    "blocks": {"block_type": _KW, "reading_order": {"type": "integer"}, "dup_group_id": _KW, "region_origin": _KW,
               "bbox": _DISABLED},
    "figures": {"caption": _text(), "object_label": _KW_NORM, "object_label_raw": _KW, "figure_type": _KW,
                "figure_type_method": _KW, "figure_type_confidence": {"type": "float"}, "layout_class": _KW,
                "region_origin": _KW, "embedded_image_artifact_id": _KW, "vector_artifact_ids": _KW,
                "caption_block_id": _KW, "bbox": _DISABLED},
    "tables": {"caption": _text(), "object_label": _KW_NORM, "object_label_raw": _KW, "n_rows": {"type": "integer"},
               "n_cols": {"type": "integer"}, "raw_format": _KW, "recognition_method": _KW, "region_origin": _KW,
               "bbox": _DISABLED},
    "formulas": {"recognized_latex": {"type": "text", "analyzer": "vkm_latex",
                                      "fields": {"kw": {"type": "keyword", "ignore_above": 4096}}},
                 "equation_label": _KW_NORM, "equation_label_raw": _KW, "formula_kind": _KW, "raw_format": _KW,
                 "recognition_method": _KW, "region_origin": _KW, "bbox": _DISABLED},
}
# fields that carry text; never returned in `_source` of query answers
TEXT_FIELDS: frozenset[str] = frozenset({"text", "caption", "recognized_latex", "work_title", "authors"})


def properties(index_type: str) -> dict[str, Any]:
    return {**copy.deepcopy(COMMON), **copy.deepcopy(PER_TYPE[index_type])}


def index_settings(building: bool = True) -> dict[str, Any]:
    return {"index": {"number_of_shards": 1, "number_of_replicas": 0,
                      "refresh_interval": "-1" if building else "1s", "max_result_window": 10000},
            "analysis": analysis_settings()}


def index_body(index_type: str, meta: dict[str, Any]) -> dict[str, Any]:
    return {"settings": index_settings(building=True),
            "mappings": {"dynamic": "strict", "_meta": {**meta, "vkm_mapping_version": MAPPING_VERSION,
                                                        "analysis_version": ANALYSIS_VERSION,
                                                        "object_kind": OBJECT_KIND[index_type]},
                         "properties": properties(index_type)}}


def body_sha256(index_type: str) -> str:
    """Hash of the build-independent part of the body (settings + properties)."""
    body = {"settings": index_settings(), "properties": properties(index_type)}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def make_build_id(manifest_sha256: str, now: datetime | None = None) -> str:
    from vkm_corpus.graph.common import utc_now

    moment = now or utc_now()
    return f"{moment.strftime('%Y%m%dt%H%M%Sz')}-{manifest_sha256[:8].lower()}"


def index_name(prefix: str, index_type: str, build_id: str) -> str:
    return f"{prefix}-{index_type}-m{MAPPING_VERSION}-{build_id}".lower()


def alias_name(prefix: str, index_type: str) -> str:
    return f"{prefix}-{index_type}".lower()


def group_alias(prefix: str) -> str:
    return f"{prefix}-{GROUP_ALIAS_SUFFIX}".lower()


def parse_index_name(prefix: str, name: str) -> tuple[str, str] | None:
    """``(index_type, build_id)`` of one of our indices, else None."""
    for index_type in INDEX_TYPES:
        head = f"{prefix}-{index_type}-m{MAPPING_VERSION}-".lower()
        if name.startswith(head):
            return index_type, name[len(head):]
    return None

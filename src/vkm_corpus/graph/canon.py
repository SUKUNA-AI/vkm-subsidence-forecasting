"""Projection input of agent E: the only place that knows agent D's column names.

Both projections (Neo4j graph, OpenSearch index) read a set of relations ``e_*`` with E-internal names and types
(``E_SCHEMA``). This module builds them from one CANONICAL snapshot:

1. an in-memory DuckDB loaded by agent D's own loader (``vkm_corpus.duckdb.build.attach_manifest``: only the files
   listed in the manifest, contract schemas, ``union_by_name``, ``meta.*``);
2. agent D's SQL rules (``apply_sql``: ``vkm_corpus/duckdb/sql/*.sql``, H-17) executed in that database, so every
   derived fact (instance links, foreign pages, citing work, bibliography matches, CITES, page order, duplicate pages,
   availability, source roll-up) is D's rule, never re-implemented here;
3. views ``e_*`` declared by ``MAPPINGS`` below — a declarative table *E column ← SQL over D relations*, with
   alternatives for names that D may still change and explicit defaults for optional columns. A missing required
   column or relation fails with ``E_CANON_MAPPING`` listing every gap at once.

Tests build the same ``e_*`` relations from synthetic rows (``ProjectionInput.from_rows``), so everything downstream
of this module is independent of the exact canonical names.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Mapping, Sequence

from vkm_corpus.contracts import vocab
from vkm_corpus.graph.common import ProjectionError, Snapshot, SnapshotInfo, sha256_bytes

# ---------------------------------------------------------------- E-internal schema of the projection input
_V, _I, _D, _B, _DT, _L = "VARCHAR", "INTEGER", "DOUBLE", "BOOLEAN", "DATE", "VARCHAR[]"
_ENV_DOC = {"origin": _V, "review_status": _V, "quality_flags": _L, "schema_version": _V, "processing_run_id": _V,
            "extractor_id": _V, "model_id": _V, "model_revision": _V, "content_sha256": _V}
_BBOX = {"bbox_x0": _D, "bbox_y0": _D, "bbox_x1": _D, "bbox_y1": _D, "bbox_space": _V}

E_SCHEMA: dict[str, dict[str, str]] = {
    "e_sources": {"source_id": _V, "source_sha256": _V, "lifecycle_status": _V, "lifecycle_reason_code": _V,
                  "file_status": _V, "format_detected": _V, "source_class_raw": _V, "priority": _V,
                  "site_scope_raw": _V, "site_scope": _L, "site_scope_mapping": _V, "review_status": _V,
                  "review_status_basis": _V, "processing_rollup": _V, "page_count": _I, "schema_version": _V,
                  "origin": _V, "processing_run_id": _V, "content_sha256": _V, "quality_flags": _L},
    "e_works": {"work_id": _V, "work_type": _V, "title": _V, "publication_year": _I, "doi": _V, "isbn": _L,
                "languages": _L, "external_ids": _L, "identity_status": _V, "curation_status": _V,
                "review_status": _V, "is_container": _B, "venue_id": _V, "volume": _V, "issue": _V,
                "pages_range": _V, "available_latest_day": _DT, "available_basis": _V, "schema_version": _V,
                "origin": _V, "processing_run_id": _V, "content_sha256": _V, "quality_flags": _L},
    "e_source_work_links": {"link_id": _V, "source_id": _V, "work_id": _V, "link_type": _V, "is_primary": _B,
                            "page_start": _I, "page_end": _I, "part_label": _V, "printed_range": _V, "basis": _V,
                            "curation_status": _V},
    # D's rule `work_sources` (instance link types, not rejected, with a work) — E keeps the primary link (H-16, CP-25)
    "e_instance_links": {"link_id": _V, "source_id": _V, "work_id": _V, "link_type": _V, "is_primary": _B,
                         "page_start": _I, "page_end": _I, "part_label": _V, "printed_range": _V, "basis": _V,
                         "curation_status": _V},
    # D's rule `foreign_content_pages`: pages covered by FOREIGN_CONTENT links (work NULL = unidentified)
    "e_foreign_pages": {"page_id": _V, "source_id": _V, "work_id": _V, "link_id": _V, "rule_version": _V},
    "e_work_relations": {"relation_id": _V, "from_work_id": _V, "relation": _V, "to_work_id": _V,
                         "is_symmetric": _B, "basis": _V, "curation_status": _V},
    "e_source_relations": {"relation_id": _V, "from_source_id": _V, "relation": _V, "to_source_id": _V,
                           "from_page_start": _I, "from_page_end": _I, "to_page_start": _I, "to_page_end": _I,
                           "basis": _V, "curation_status": _V},
    "e_authors": {"author_id": _V, "identity_status": _V, "script": _V, "same_as_author_id": _V,
                  "review_status": _V, "schema_version": _V, "origin": _V, "processing_run_id": _V,
                  "quality_flags": _L},
    "e_work_authors": {"row_id": _V, "work_id": _V, "author_id": _V, "ordinal": _I, "role": _V,
                       "name_as_listed": _V},
    "e_venues": {"venue_id": _V, "venue_type": _V, "identity_status": _V, "issn": _L, "same_as_venue_id": _V,
                 "review_status": _V, "schema_version": _V, "origin": _V, "processing_run_id": _V,
                 "quality_flags": _L},
    "e_pages": {"page_id": _V, "source_id": _V, "page_index": _I, "page_kind": _V, "page_class": _V,
                "printed_page_raw": _V, "printed_page_labels": _L, "is_spread": _B, "page_status": _V,
                "file_text_status": _V, "ocr_status": _V, "primary_text_layer": _V, "primary_text_origin": _V,
                "text": _V, "text_rule": _V, "text_sha256": _V, "render_artifact_id": _V, "preview_artifact_id": _V,
                **_ENV_DOC},
    "e_blocks": {"block_id": _V, "page_id": _V, "source_id": _V, "text_layer": _V, "is_primary_layer": _B,
                 "block_type": _V, "reading_order": _I, **_BBOX, "text": _V, "text_sha256": _V, "language": _V,
                 "region_origin": _V, **_ENV_DOC},
    "e_figures": {"figure_id": _V, "page_id": _V, "source_id": _V, **_BBOX, "figure_label": _V, "caption": _V,
                  "caption_block_id": _V, "layout_class": _V, "figure_type": _V, "figure_type_method": _V,
                  "figure_type_confidence": _D, "region_origin": _V, "image_artifact_id": _V,
                  "embedded_image_artifact_id": _V, "vector_artifact_ids": _L, "is_primary_layer": _B, **_ENV_DOC},
    "e_tables": {"table_id": _V, "page_id": _V, "source_id": _V, **_BBOX, "table_label": _V, "caption": _V,
                 "caption_block_id": _V, "n_rows": _I, "n_cols": _I, "text": _V, "raw_format": _V,
                 "recognition_method": _V, "region_origin": _V, "image_artifact_id": _V,
                 "continues_object_id": _V, "is_primary_layer": _B, **_ENV_DOC},
    "e_formulas": {"formula_id": _V, "page_id": _V, "source_id": _V, **_BBOX, "formula_kind": _V,
                   "equation_label": _V, "latex": _V, "text": _V, "raw_format": _V, "recognition_method": _V,
                   "region_origin": _V, "image_artifact_id": _V, "is_primary_layer": _B, **_ENV_DOC},
    "e_bibliography": {"entry_id": _V, "page_id": _V, "source_id": _V, "citing_work_id": _V,
                       "citing_work_resolution": _V, "citing_work_is_container": _B, "entry_label": _V,
                       "ordinal_in_list": _I, "parsed_doi": _V, "parsed_year": _I, "continues_on_page_id": _V,
                       "origin": _V, "review_status": _V, "quality_flags": _L, "schema_version": _V,
                       "processing_run_id": _V, "content_sha256": _V, "rule_version": _V},
    "e_bibliography_links": {"link_id": _V, "entry_id": _V, "cited_work_id": _V, "match_method": _V,
                             "match_score": _D, "match_status": _V, "matched_fields": _L, "curation_status": _V,
                             "accepted": _B, "rule_version": _V},
    # D's rule `cites` (cites_v1): one row per (citing work, cited work)
    "e_cites": {"citing_work_id": _V, "cited_work_id": _V, "n_citing_entries": _I, "n_citing_sources": _I,
                "citing_work_is_container": _B, "match_methods": _L, "entry_ids": _L, "rule_version": _V},
    "e_page_sequence": {"source_id": _V, "from_page_id": _V, "to_page_id": _V, "rule_version": _V},
    # D's rule `duplicate_page_candidates`: one row per page of a duplicate group
    "e_page_duplicates": {"dup_group_id": _V, "page_id": _V, "source_id": _V, "rule_version": _V},
    # D's `work_copy_counts` (CP-25): copies of a work; NULL columns until D provides them (E then counts itself)
    "e_work_copies": {"work_id": _V, "n_sources_total": _I, "n_sources_active": _I},
}
E_RELATIONS: tuple[str, ...] = tuple(E_SCHEMA)
# key columns (ordering and duplicate checks)
E_KEYS: dict[str, str] = {
    "e_sources": "source_id", "e_works": "work_id", "e_source_work_links": "link_id",
    "e_work_relations": "relation_id", "e_source_relations": "relation_id", "e_authors": "author_id",
    "e_work_authors": "row_id", "e_venues": "venue_id", "e_pages": "page_id", "e_blocks": "block_id",
    "e_figures": "figure_id", "e_tables": "table_id", "e_formulas": "formula_id", "e_bibliography": "entry_id",
    "e_bibliography_links": "link_id", "e_instance_links": "link_id", "e_work_copies": "work_id",
}

# matches that give RESOLVES_TO edges; D's `cites` view applies the same acceptance (checked by C14)
ACCEPTED_MATCH_STATUSES: frozenset[str] = frozenset(
    getattr(vocab, "ACCEPTED_MATCH_STATUSES", None) or {vocab.MatchStatus.AUTO_EXACT_ID_MATCH.value})
DUPLICATE_BASIS_DEFAULT = "TEXT_SHA256_EQUAL"


def _sql_list(values: Iterable[str]) -> str:
    items = sorted({str(v) for v in values})
    for v in items:
        if not re.fullmatch(r"[A-Za-z0-9_]+", v):
            raise ValueError(f"unsafe vocabulary value {v!r}")
    return "(" + ", ".join(f"'{v}'" for v in items) + ")" if items else "(NULL)"


# ---------------------------------------------------------------- mapping D → E
@dataclass(frozen=True)
class Col:
    """E column from the first alternative whose referenced D columns all exist; else ``default`` (if optional)."""

    name: str
    alternatives: tuple[str, ...]
    optional: bool = False
    default: str = "NULL"


def col(name: str, *alternatives: str, optional: bool = False, default: str = "NULL") -> Col:
    return Col(name, tuple(alternatives), optional, default)


def opt(name: str, *alternatives: str, default: str = "NULL") -> Col:
    return Col(name, tuple(alternatives), True, default)


@dataclass(frozen=True)
class Source:
    alias: str
    candidates: tuple[str, ...]          # D relation names tried in order (views of D first)
    join: str = ""                       # "" for the FROM relation, else "LEFT JOIN ... ON ..." template with {rel}
    optional: bool = False               # a missing optional relation contributes only defaults
    stub_columns: tuple[str, ...] = ()   # columns an empty stub must expose for the join condition


@dataclass(frozen=True)
class RelationMap:
    relation: str
    sources: tuple[Source, ...]
    columns: tuple[Col, ...]
    where: str = ""


_EMPTY_L = "[]::VARCHAR[]"
_ENV_DOC_COLS = lambda a: (  # noqa: E731 - small column factory
    col("origin", f"{a}.origin"), col("review_status", f"{a}.review_status"),
    opt("quality_flags", f"{a}.quality_flags", default=_EMPTY_L), col("schema_version", f"{a}.schema_version"),
    col("processing_run_id", f"{a}.processing_run_id"), opt("extractor_id", f"{a}.extractor_id"),
    opt("model_id", f"{a}.model_id"), opt("model_revision", f"{a}.model_revision"),
    opt("content_sha256", f"{a}.content_sha256"))
_BBOX_COLS = lambda a: tuple(opt(c, f"{a}.{c}") for c in ("bbox_x0", "bbox_y0", "bbox_x1", "bbox_y1")) + (  # noqa: E731
    opt("bbox_space", f"{a}.bbox_space"),)

MAPPINGS: tuple[RelationMap, ...] = (
    RelationMap("e_sources", (
        Source("s", ("main.sources", "canonical.sources")),
        Source("d", ("main.documents", "canonical.documents"), "LEFT JOIN {rel} d ON d.source_id = s.source_id",
               optional=True, stub_columns=("source_id",)),
        Source("r", ("main.source_status_summary",), "LEFT JOIN {rel} r ON r.source_id = s.source_id")), (
        col("source_id", "s.source_id"), col("source_sha256", "s.source_sha256"),
        col("lifecycle_status", "s.lifecycle_status"),
        opt("lifecycle_reason_code", "s.register_skip_reason", "s.lifecycle_reason_code"),
        opt("file_status", "s.file_status"), opt("format_detected", "s.format_detected"),
        opt("source_class_raw", "s.source_class_raw"), opt("priority", "s.priority"),
        col("site_scope_raw", "s.site_scope_raw"), opt("site_scope", "s.site_scope", default=_EMPTY_L),
        col("site_scope_mapping", "s.site_scope_mapping"), col("review_status", "s.review_status"),
        opt("review_status_basis", "s.review_status_basis"),
        col("processing_rollup", "r.source_rollup"),
        opt("page_count", "d.page_count"), col("schema_version", "s.schema_version"),
        opt("origin", "s.origin", default="'REGISTRY'"), opt("processing_run_id", "s.processing_run_id"),
        opt("content_sha256", "s.content_sha256"), opt("quality_flags", "s.quality_flags", default=_EMPTY_L))),
    RelationMap("e_works", (
        Source("w", ("main.works",)),
        Source("a", ("main.works_availability",), "LEFT JOIN {rel} a ON a.work_id = w.work_id", optional=True,
               stub_columns=("work_id",))), (
        col("work_id", "w.work_id"), col("work_type", "w.work_type"), opt("title", "w.title"),
        opt("publication_year", "w.publication_year"), opt("doi", "w.doi"),
        opt("isbn", "w.isbn", default=_EMPTY_L), opt("languages", "w.languages", default=_EMPTY_L),
        opt("external_ids",
            "list_transform(w.external_ids, x -> x.scheme || ':' || x.value || ':' || coalesce(x.relation, ''))",
            default=_EMPTY_L),
        opt("identity_status", "w.identity_status"), opt("curation_status", "w.curation_status"),
        col("review_status", "w.review_status"),
        opt("is_container", f"w.work_type IN {_sql_list(vocab.CONTAINER_WORK_TYPES)}", default="false"),
        opt("venue_id", "w.venue_id"), opt("volume", "w.volume"), opt("issue", "w.issue"),
        opt("pages_range", "w.pages_range"),
        opt("available_latest_day", "a.available_latest_day"),
        opt("available_basis", "coalesce(a.available_basis, 'UNKNOWN')", default="'UNKNOWN'"),
        col("schema_version", "w.schema_version"), opt("origin", "w.origin", default="'REGISTRY'"),
        opt("processing_run_id", "w.processing_run_id"), opt("content_sha256", "w.content_sha256"),
        opt("quality_flags", "w.quality_flags", default=_EMPTY_L))),
    RelationMap("e_source_work_links", (Source("l", ("canonical.source_work_links", "main.source_work_links")),), (
        col("link_id", "l.object_id"), col("source_id", "l.source_id"), col("work_id", "l.work_id"),
        col("link_type", "l.link_type"), col("is_primary", "l.is_primary"), opt("page_start", "l.page_start"),
        opt("page_end", "l.page_end"), opt("part_label", "l.part_label"), opt("printed_range", "l.printed_range"),
        opt("basis", "l.basis"), col("curation_status", "l.curation_status"))),
    RelationMap("e_instance_links", (Source("i", ("main.work_sources",)),), (
        col("link_id", "i.canonical_row_id"), col("source_id", "i.source_id"), col("work_id", "i.work_id"),
        col("link_type", "i.link_type"), col("is_primary", "i.is_primary"), opt("page_start", "i.page_start"),
        opt("page_end", "i.page_end"), opt("part_label", "i.part_label"), opt("printed_range", "i.printed_range"),
        opt("basis", "i.basis"), opt("curation_status", "i.curation_status"))),
    RelationMap("e_foreign_pages", (Source("f", ("main.foreign_content_pages",)),), (
        col("page_id", "f.page_id"), col("source_id", "f.source_id"), col("work_id", "f.foreign_work_id"),
        col("link_id", "f.canonical_row_id"),
        opt("rule_version", "f.rule_version", default=f"'{vocab.DerivedRule.CITING_WORK_V1.value}'"))),
    RelationMap("e_work_relations", (Source("r", ("canonical.work_relations", "main.work_relations")),), (
        col("relation_id", "r.object_id"), col("from_work_id", "r.from_work_id"), col("relation", "r.relation"),
        col("to_work_id", "r.to_work_id"),
        opt("is_symmetric", "r.is_symmetric", f"r.relation IN {_sql_list(vocab.SYMMETRIC_WORK_RELATIONS)}",
            default="false"),
        opt("basis", "r.basis"), col("curation_status", "r.curation_status"))),
    RelationMap("e_source_relations", (Source("r", ("canonical.source_relations", "main.source_relations")),), (
        col("relation_id", "r.object_id"), col("from_source_id", "r.from_source_id"), col("relation", "r.relation"),
        col("to_source_id", "r.to_source_id"), opt("from_page_start", "r.from_page_start"),
        opt("from_page_end", "r.from_page_end"), opt("to_page_start", "r.to_page_start"),
        opt("to_page_end", "r.to_page_end"), opt("basis", "r.basis"), col("curation_status", "r.curation_status"))),
    RelationMap("e_authors", (Source("a", ("main.authors", "canonical.authors")),), (
        col("author_id", "a.author_id"), opt("identity_status", "a.identity_status"), opt("script", "a.script"),
        opt("same_as_author_id", "a.same_as_author_id"), col("review_status", "a.review_status"),
        col("schema_version", "a.schema_version"), opt("origin", "a.origin", default="'DERIVED'"),
        opt("processing_run_id", "a.processing_run_id"), opt("quality_flags", "a.quality_flags", default=_EMPTY_L))),
    RelationMap("e_work_authors", (Source("wa", ("main.work_authors", "canonical.work_authors")),), (
        col("row_id", "wa.object_id"), col("work_id", "wa.work_id"), col("author_id", "wa.author_id"),
        col("ordinal", "wa.ordinal"), col("role", "wa.role"), col("name_as_listed", "wa.name_as_listed"))),
    RelationMap("e_venues", (Source("v", ("main.venues", "canonical.venues")),), (
        col("venue_id", "v.venue_id"), opt("venue_type", "v.venue_type"), opt("identity_status", "v.identity_status"),
        opt("issn", "v.issn", default=_EMPTY_L), opt("same_as_venue_id", "v.same_as_venue_id"),
        col("review_status", "v.review_status"), col("schema_version", "v.schema_version"),
        opt("origin", "v.origin", default="'DERIVED'"), opt("processing_run_id", "v.processing_run_id"),
        opt("quality_flags", "v.quality_flags", default=_EMPTY_L))),
    RelationMap("e_pages", (Source("p", ("main.pages", "canonical.pages")),), (
        col("page_id", "p.page_id"), col("source_id", "p.source_id"), col("page_index", "p.page_index"),
        col("page_kind", "p.page_kind"), opt("page_class", "p.page_class"),
        opt("printed_page_raw", "p.printed_page_raw"),
        opt("printed_page_labels", "p.printed_page_labels", default=_EMPTY_L), opt("is_spread", "p.is_spread"),
        col("page_status", "p.page_status"), opt("file_text_status", "p.file_text_status"),
        opt("ocr_status", "p.ocr_status"), opt("primary_text_layer", "p.primary_text_layer"),
        opt("primary_text_origin", "p.primary_text_origin"),
        col("text", "p.normalized_text"), col("text_rule", "p.text_rule"), col("text_sha256", "p.text_sha256"),
        opt("render_artifact_id", "p.render_artifact_id"),
        opt("preview_artifact_id", "coalesce(p.preview_artifact_id, p.render_artifact_id)"),
    ) + _ENV_DOC_COLS("p")),
    RelationMap("e_blocks", (Source("b", ("main.blocks", "canonical.blocks")),), (
        col("block_id", "b.object_id"), col("page_id", "b.page_id"), col("source_id", "b.source_id"),
        col("text_layer", "b.text_layer"), col("is_primary_layer", "b.is_primary_layer"),
        col("block_type", "b.block_type"), col("reading_order", "b.reading_order")) + _BBOX_COLS("b") + (
        col("text", "b.normalized_text"), col("text_sha256", "sha256(b.normalized_text)"),
        opt("language", "b.language"), col("region_origin", "b.region_origin"),
    ) + _ENV_DOC_COLS("b")),
    RelationMap("e_figures", (Source("f", ("main.figures", "canonical.figures")),), (
        col("figure_id", "f.object_id"), col("page_id", "f.page_id"), col("source_id", "f.source_id"),
    ) + _BBOX_COLS("f") + (
        opt("figure_label", "f.figure_label"),
        opt("caption", "coalesce(f.caption_normalized, f.caption)", "f.caption"),
        opt("caption_block_id", "f.caption_block_id"), opt("layout_class", "f.layout_class"),
        opt("figure_type", "f.detected_figure_type", default="'UNKNOWN_FIGURE_TYPE'"),
        opt("figure_type_method", "f.figure_type_method"), opt("figure_type_confidence", "f.figure_type_confidence"),
        col("region_origin", "f.region_origin"), opt("image_artifact_id", "f.image_artifact_id"),
        opt("embedded_image_artifact_id", "f.embedded_image_artifact_id"),
        opt("vector_artifact_ids", "list_transform(f.vector_artifacts, x -> x.artifact_id)", default=_EMPTY_L),
        opt("is_primary_layer", "f.is_primary_layer"),
    ) + _ENV_DOC_COLS("f")),
    RelationMap("e_tables", (Source("t", ('main."tables"', 'canonical."tables"')),), (
        col("table_id", "t.object_id"), col("page_id", "t.page_id"), col("source_id", "t.source_id"),
    ) + _BBOX_COLS("t") + (
        opt("table_label", "t.table_label"), opt("caption", "coalesce(t.caption_normalized, t.caption)", "t.caption"),
        opt("caption_block_id", "t.caption_block_id"), opt("n_rows", "t.n_rows"), opt("n_cols", "t.n_cols"),
        opt("text", "t.normalized_text"), opt("raw_format", "t.raw_format"),
        opt("recognition_method", "t.recognition_method"), col("region_origin", "t.region_origin"),
        opt("image_artifact_id", "t.image_artifact_id"), opt("continues_object_id", "t.continues_object_id"),
        opt("is_primary_layer", "t.is_primary_layer"),
    ) + _ENV_DOC_COLS("t")),
    RelationMap("e_formulas", (Source("m", ("main.formulas", "canonical.formulas")),), (
        col("formula_id", "m.object_id"), col("page_id", "m.page_id"), col("source_id", "m.source_id"),
    ) + _BBOX_COLS("m") + (
        opt("formula_kind", "m.formula_kind"), opt("equation_label", "m.equation_label"),
        opt("latex", "m.normalized_latex"), opt("text", "m.native_glyph_text"), opt("raw_format", "m.raw_format"),
        opt("recognition_method", "m.recognition_method"), col("region_origin", "m.region_origin"),
        opt("image_artifact_id", "m.image_artifact_id"), opt("is_primary_layer", "m.is_primary_layer"),
    ) + _ENV_DOC_COLS("m")),
    RelationMap("e_bibliography", (Source("e", ("main.bibliography",)),), (
        col("entry_id", "e.object_id"), col("page_id", "e.page_id"), col("source_id", "e.source_id"),
        col("citing_work_id", "e.citing_work_id"), col("citing_work_resolution", "e.citing_work_resolution"),
        opt("citing_work_is_container", "e.citing_work_is_container", default="false"),
        opt("entry_label", "e.entry_label"), opt("ordinal_in_list", "e.ordinal_in_list"),
        opt("parsed_doi", "e.parsed_doi"), opt("parsed_year", "e.parsed_year"),
        opt("continues_on_page_id", "e.continues_on_page_id"), col("origin", "e.origin"),
        col("review_status", "e.review_status"), opt("quality_flags", "e.quality_flags", default=_EMPTY_L),
        col("schema_version", "e.schema_version"), col("processing_run_id", "e.processing_run_id"),
        opt("content_sha256", "e.content_sha256"),
        opt("rule_version", "e.rule_version", default=f"'{vocab.DerivedRule.CITING_WORK_V1.value}'"))),
    RelationMap("e_bibliography_links", (Source("l", ("main.bibliography_links",)),), (
        col("link_id", "l.object_id"), col("entry_id", "l.entry_id"), col("cited_work_id", "l.cited_work_id"),
        col("match_method", "l.match_method"), opt("match_score", "l.match_score"),
        col("match_status", "l.match_status"), opt("matched_fields", "l.matched_fields", default=_EMPTY_L),
        opt("curation_status", "l.curation_status"),
        col("accepted", f"l.match_status IN {_sql_list(ACCEPTED_MATCH_STATUSES)}"),
        opt("rule_version", "l.rule_version", default=f"'{vocab.DerivedRule.BIBLIOGRAPHY_MATCH_V1.value}'"))),
    RelationMap("e_cites", (Source("c", ("main.cites",)),), (
        col("citing_work_id", "c.citing_work_id"), col("cited_work_id", "c.cited_work_id"),
        col("n_citing_entries", "c.n_citing_entries"), col("n_citing_sources", "c.n_citing_sources"),
        opt("citing_work_is_container", "c.citing_work_is_container", default="false"),
        opt("match_methods", "c.match_methods", default=_EMPTY_L), col("entry_ids", "c.entry_ids"),
        opt("rule_version", "c.rule_version", default=f"'{vocab.DerivedRule.CITES_V1.value}'"))),
    RelationMap("e_page_sequence", (Source("q", ("main.page_sequence",)),), (
        opt("source_id", "q.source_id"), col("from_page_id", "q.from_page_id"), col("to_page_id", "q.to_page_id"),
        opt("rule_version", "q.rule_version", default=f"'{vocab.DerivedRule.PAGE_SEQUENCE_V1.value}'"))),
    RelationMap("e_page_duplicates", (Source("u", ("main.duplicate_page_candidates",)),), (
        col("dup_group_id", "u.dup_group_id"), col("page_id", "u.page_id"), opt("source_id", "u.source_id"),
        opt("rule_version", "u.rule_version", default=f"'{vocab.DerivedRule.DUPLICATE_PAGES_V1.value}'"))),
    RelationMap("e_work_copies", (Source("k", ("main.work_copy_counts",)),), (
        col("work_id", "k.work_id"), opt("n_sources_total", "k.n_sources_total"),
        opt("n_sources_active", "k.n_sources_active"))),
)
assert tuple(m.relation for m in MAPPINGS) == E_RELATIONS, "every e_* relation needs exactly one mapping"

_REF_RE = re.compile(r"(?<![\w.])([a-z]{1,3})\.(\"?[A-Za-z_][A-Za-z0-9_]*\"?)")


# ---------------------------------------------------------------- the projection input
@dataclass
class ProjectionInput:
    """DuckDB connection exposing the ``e_*`` relations of one snapshot, plus what the receipts need."""

    con: Any
    info: SnapshotInfo
    rule_versions: dict[str, str] = field(default_factory=dict)
    derived_sql: dict[str, str] = field(default_factory=dict)      # D's SQL file → sha256
    mapping_report: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------ construction
    @classmethod
    def from_snapshot(cls, snapshot: Snapshot, *, threads: int | None = None) -> "ProjectionInput":
        import duckdb

        con = duckdb.connect(":memory:")
        if threads:
            con.execute(f"SET threads = {int(threads)}")
        derived = load_snapshot_into(con, snapshot)
        report = create_projection_views(con)
        return cls(con=con, info=snapshot.info, rule_versions=_rule_versions(), derived_sql=derived,
                   mapping_report=report)

    @classmethod
    def from_rows(cls, relations: Mapping[str, Iterable[Mapping[str, Any]]],
                  info: SnapshotInfo | None = None) -> "ProjectionInput":
        """Typed ``e_*`` tables from Python rows (synthetic tests). Unknown relations/columns are errors."""
        import duckdb

        unknown = set(relations) - set(E_SCHEMA)
        if unknown:
            raise ValueError(f"unknown projection relations: {sorted(unknown)}")
        con = duckdb.connect(":memory:")
        for rel, schema in E_SCHEMA.items():
            cols = ", ".join(f'"{c}" {t}' for c, t in schema.items())
            con.execute(f"CREATE TABLE {rel} ({cols})")
            rows = list(relations.get(rel, ()))
            if not rows:
                continue
            bad = {k for r in rows for k in r} - set(schema)
            if bad:
                raise ValueError(f"{rel}: unknown columns {sorted(bad)}")
            names = list(schema)
            values = [[r.get(c, [] if schema[c] == _L else None) for c in names] for r in rows]
            con.executemany(f"INSERT INTO {rel} VALUES ({', '.join('?' for _ in names)})", values)
        info = info or SnapshotInfo(snapshot_id="snap-synthetic", manifest_sha256="0" * 64)
        return cls(con=con, info=info, rule_versions=_rule_versions(), derived_sql={},
                   mapping_report={"mode": "synthetic-rows"})

    # ------------------------------------------------------------ reading
    def iter_rows(self, sql: str, params: Sequence[Any] | None = None, batch: int = 20_000) -> Iterator[dict]:
        """Stream rows as dicts (Arrow record batches when pyarrow is present; a cursor, so nested queries work)."""
        cur = self.con.cursor()
        try:
            cur.execute(sql, params or [])
            try:
                reader = cur.to_arrow_reader(batch)
            except Exception:  # pyarrow missing
                reader = None
            if reader is not None:
                for record_batch in reader:
                    yield from record_batch.to_pylist()
                return
            names = [d[0] for d in cur.description]
            while chunk := cur.fetchmany(batch):
                for row in chunk:
                    yield dict(zip(names, row))
        finally:
            cur.close()

    def fetch(self, sql: str, params: Sequence[Any] | None = None) -> list[dict]:
        return list(self.iter_rows(sql, params))

    def scalar(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        row = self.con.execute(sql, params or []).fetchone()
        return None if row is None else row[0]

    def count(self, relation: str) -> int:
        if relation not in E_SCHEMA:
            raise ValueError(relation)
        return int(self.scalar(f"SELECT count(*) FROM {relation}"))

    def close(self) -> None:
        try:
            self.con.close()
        except Exception:  # pragma: no cover - closing is best effort
            pass


def _rule_versions() -> dict[str, str]:
    from vkm_corpus.graph.schema import RULE_OF_REL

    return dict(RULE_OF_REL)


# ---------------------------------------------------------------- snapshot → DuckDB (agent D's loader and SQL, H-17)
def derived_sql_files() -> list[tuple[str, bytes]]:
    """Agent D's SQL rule files in execution order (``vkm_corpus/duckdb/sql/*.sql``)."""
    from vkm_corpus.duckdb import build as d_build

    return [(f.name, f.read_bytes()) for f in d_build.sql_files()]


def load_snapshot_into(con: Any, snapshot: Snapshot) -> dict[str, str]:
    """``canonical.<dataset>`` strictly from the manifest (D's ``attach_manifest``: contract schema, ``union_by_name``,
    ``meta.*``), then D's SQL rules (``apply_sql``). Returns ``{sql file: sha256}`` for the receipt."""
    from vkm_corpus.duckdb import build as d_build
    from vkm_corpus.parquet.layout import CanonLayout

    try:
        d_build.attach_manifest(con, CanonLayout(snapshot.root), dict(snapshot.manifest),
                                manifest_sha256=snapshot.manifest_sha256)
        d_build.apply_sql(con)
    except ProjectionError:
        raise
    except Exception as exc:  # duckdb.Error, contract errors
        raise ProjectionError("E_CANON_MAPPING", f"cannot load the snapshot with D's loader: {exc}",
                              stage="mapping") from exc
    return {name: sha256_bytes(data) for name, data in derived_sql_files()}


def _relation_columns(con: Any, name: str) -> set[str] | None:
    try:
        cur = con.execute(f"SELECT * FROM {name} LIMIT 0")
    except Exception:
        return None
    return {d[0] for d in cur.description}


def create_projection_views(con: Any) -> dict[str, Any]:
    """Create ``e_*`` views over D's relations. Raises ``E_CANON_MAPPING`` with every missing required item."""
    missing: list[str] = []
    report: dict[str, Any] = {"relations": {}, "defaulted": {}}
    statements: list[str] = []
    for mapping in MAPPINGS:
        schema = E_SCHEMA[mapping.relation]
        resolved: dict[str, tuple[str, set[str]]] = {}
        from_parts: list[str] = []
        for src in mapping.sources:
            found = None
            for cand in src.candidates:
                cols = _relation_columns(con, cand)
                if cols is not None:
                    found = (cand, cols)
                    break
            if found is None:
                if not src.optional:
                    missing.append(f"{mapping.relation}: relation {'/'.join(src.candidates)}")
                    continue
                stub_cols = ", ".join(f"NULL::VARCHAR AS {c}" for c in src.stub_columns) or "NULL AS _none"
                found = (f"(SELECT {stub_cols} WHERE false)", set())
            resolved[src.alias] = found
            rel_sql = found[0]
            from_parts.append(f"{rel_sql} {src.alias}" if not src.join else src.join.format(rel=rel_sql))
        if len(resolved) != len(mapping.sources):
            continue
        select: list[str] = []
        defaulted: list[str] = []
        for c in mapping.columns:
            expr = None
            for alt in c.alternatives:
                refs = [(a, cname.strip('"')) for a, cname in _REF_RE.findall(alt) if a in resolved]
                if all(cname in resolved[a][1] for a, cname in refs):
                    expr = alt
                    break
            if expr is None:
                if not c.optional:
                    missing.append(f"{mapping.relation}.{c.name} ← {' | '.join(c.alternatives)}")
                    continue
                expr = c.default
                defaulted.append(c.name)
            select.append(f'CAST({expr} AS {schema[c.name]}) AS "{c.name}"')
        where = f" WHERE {mapping.where}" if mapping.where else ""
        statements.append(f"CREATE OR REPLACE VIEW {mapping.relation} AS SELECT {', '.join(select)} "
                          f"FROM {' '.join(from_parts)}{where}")
        report["relations"][mapping.relation] = {a: r[0] for a, r in resolved.items()}
        if defaulted:
            report["defaulted"][mapping.relation] = defaulted
    if missing:
        raise ProjectionError("E_CANON_MAPPING", f"{len(missing)} canonical column(s)/relation(s) needed by the "
                              "projections are missing", stage="mapping", details={"missing": missing})
    for stmt in statements:
        try:
            con.execute(stmt)
        except Exception as exc:  # duckdb.Error: e.g. a struct field of D differs
            raise ProjectionError("E_CANON_MAPPING", f"cannot create {stmt.split()[4]}: {exc}",
                                  stage="mapping") from exc
    return report

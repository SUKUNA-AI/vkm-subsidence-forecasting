"""Projection input ``e_*`` → node and relationship rows of the DOCUMENT graph.

Rows are produced in a deterministic order (nodes by ``id``; relationships by ``from_id, to_id,
canonical_row_id``), carry only whitelisted properties (``vkm_corpus.graph.schema``) and never a text. Relationship
rows from one canonical row carry ``canonical_row_id``; rows of derived rules carry ``rule_version`` (H-16, H-17).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator

from vkm_corpus.contracts import vocab
from vkm_corpus.graph import schema as S
from vkm_corpus.graph.canon import ProjectionInput
from vkm_corpus.graph.common import clean_props
from vkm_corpus.graph.rules import CURATED, ensure_rule_views

CONTAINER_FLAG = vocab.QualityFlag.CITING_WORK_IS_CONTAINER.value

# ---------------------------------------------------------------- node queries
_NODE_SQL: dict[str, str] = {
    "Work": """SELECT w.*, coalesce(m.work_copy_count, 0) AS work_copy_count
               FROM e_works w LEFT JOIN x_work_meta m ON m.work_id = w.work_id ORDER BY w.work_id""",
    "Author": "SELECT * FROM e_authors ORDER BY author_id",
    "Venue": "SELECT * FROM e_venues ORDER BY venue_id",
    "Source": "SELECT * FROM e_sources ORDER BY source_id",
    "Page": """SELECT p.*, coalesce(d.dup_group_id, p.page_id) AS dup_group_id
               FROM e_pages p LEFT JOIN x_page_dup d ON d.page_id = p.page_id ORDER BY p.page_id""",
    "Block": "SELECT * FROM e_blocks ORDER BY block_id",
    "Figure": "SELECT * FROM e_figures ORDER BY figure_id",
    "Table": "SELECT * FROM e_tables ORDER BY table_id",
    "Formula": "SELECT * FROM e_formulas ORDER BY formula_id",
    "BibliographyEntry": "SELECT * FROM e_bibliography ORDER BY entry_id",
}

# ---------------------------------------------------------------- relationship queries (from_id, to_id, props...)
_ORDER = " ORDER BY from_id, to_id, canonical_row_id"
_REL_SQL: dict[str, str] = {
    "INSTANCE_OF": """SELECT source_id AS from_id, work_id AS to_id, link_id AS canonical_row_id, link_type,
                      page_start, page_end, part_label, printed_range, basis, curation_status
                      FROM x_instance_links""" + _ORDER,
    "AUTHORED_BY": """SELECT work_id AS from_id, author_id AS to_id, row_id AS canonical_row_id, ordinal, role,
                      name_as_listed FROM e_work_authors""" + _ORDER,
    "PUBLISHED_IN": """SELECT work_id AS from_id, venue_id AS to_id, work_id AS canonical_row_id, volume, issue,
                       pages_range FROM e_works WHERE venue_id IS NOT NULL""" + _ORDER,
    "HAS_PAGE": "SELECT source_id AS from_id, page_id AS to_id, page_id AS canonical_row_id FROM e_pages" + _ORDER,
    "PRECEDES": """SELECT from_page_id AS from_id, to_page_id AS to_id, rule_version FROM e_page_sequence
                   ORDER BY from_id, to_id""",
    **{rel: (f"SELECT page_id AS from_id, {key} AS to_id, {key} AS canonical_row_id FROM {table} "
             f"WHERE page_id IS NOT NULL" + _ORDER)
       for rel, table, key in (("HAS_BLOCK", "e_blocks", "block_id"), ("HAS_FIGURE", "e_figures", "figure_id"),
                               ("HAS_TABLE", "e_tables", "table_id"), ("HAS_FORMULA", "e_formulas", "formula_id"),
                               ("HAS_BIBLIOGRAPHY_ENTRY", "e_bibliography", "entry_id"))},
    "REFERENCE_OF": f"""SELECT entry_id AS from_id, citing_work_id AS to_id, entry_id AS canonical_row_id,
                        citing_work_resolution, rule_version,
                        CASE WHEN coalesce(citing_work_is_container, false) THEN ['{CONTAINER_FLAG}']
                             ELSE []::VARCHAR[] END AS flags
                        FROM e_bibliography WHERE citing_work_id IS NOT NULL""" + _ORDER,
    "RESOLVES_TO": """SELECT entry_id AS from_id, cited_work_id AS to_id, link_id AS canonical_row_id, match_method,
                      match_score, match_status, matched_fields, rule_version
                      FROM e_bibliography_links WHERE accepted""" + _ORDER,
    "CITES": f"""SELECT citing_work_id AS from_id, cited_work_id AS to_id, entry_ids AS via_entry_ids,
                   match_methods, n_citing_entries, n_citing_sources, rule_version,
                   CASE WHEN coalesce(citing_work_is_container, false) THEN ['{CONTAINER_FLAG}']
                        ELSE []::VARCHAR[] END AS flags
                 FROM e_cites ORDER BY from_id, to_id""",
    "CARRIES_FOREIGN_CONTENT_OF": """SELECT page_id AS from_id, work_id AS to_id, link_id AS canonical_row_id,
                                     rule_version FROM x_foreign_page_links WHERE work_id IS NOT NULL""" + _ORDER,
    "DUPLICATE_CANDIDATE_OF": """
        WITH mp AS (SELECT d.dup_group_id, d.rule_version, d.page_id, p.source_id
                    FROM e_page_duplicates d JOIN e_pages p ON p.page_id = d.page_id)
        SELECT a.page_id AS from_id, b.page_id AS to_id, min(a.dup_group_id) AS dup_group_id,
               min(a.rule_version) AS rule_version, 'TEXT_SHA256_EQUAL' AS basis
        FROM mp a JOIN mp b ON a.dup_group_id = b.dup_group_id AND a.page_id < b.page_id AND a.source_id <> b.source_id
        GROUP BY a.page_id, b.page_id ORDER BY from_id, to_id""",
}


def _work_relation_sql(rel: S.RelType) -> str:
    ends = ("least(from_work_id, to_work_id) AS from_id, greatest(from_work_id, to_work_id) AS to_id"
            if rel.symmetric else "from_work_id AS from_id, to_work_id AS to_id")
    return (f"SELECT {ends}, relation_id AS canonical_row_id, is_symmetric, basis, curation_status "
            f"FROM e_work_relations WHERE relation = '{rel.type}' AND curation_status = '{CURATED}'" + _ORDER)


def _source_relation_sql(rel: S.RelType) -> str:
    return (f"SELECT from_source_id AS from_id, to_source_id AS to_id, relation_id AS canonical_row_id, "
            f"from_page_start, from_page_end, to_page_start, to_page_end, basis, curation_status "
            f"FROM e_source_relations WHERE relation = '{rel.type}' AND curation_status = '{CURATED}'" + _ORDER)


def node_sql(node: S.NodeType) -> str:
    return _NODE_SQL[node.label]


def rel_sql(rel: S.RelType) -> str:
    if rel.type in S.WORK_RELATION_TYPES:
        return _work_relation_sql(rel)
    if rel.type in S.SOURCE_RELATION_TYPES:
        return _source_relation_sql(rel)
    return _REL_SQL[rel.type]


# ---------------------------------------------------------------- row → props
def _bbox(row: dict[str, Any]) -> list[float] | None:
    values = [row.get(k) for k in ("bbox_x0", "bbox_y0", "bbox_x1", "bbox_y1")]
    if any(v is None for v in values):
        return None
    return [float(v) for v in values]


def _clean_list(value: Any) -> Any:
    if isinstance(value, list):
        return [v for v in value if v is not None]
    return value


def node_props(node: S.NodeType, row: dict[str, Any], run_id: str | None) -> dict[str, Any]:
    props: dict[str, Any] = {}
    for prop in node.all_properties:
        if prop == "id":
            props["id"] = row[node.key]
        elif prop == "projection_run_id":
            props[prop] = run_id
        elif prop == "bbox":
            props[prop] = _bbox(row)
        elif prop == "has_latex":
            props[prop] = bool(row.get("latex"))
        elif prop in row:
            props[prop] = _clean_list(row[prop])
    return clean_props(props)


def rel_props(rel: S.RelType, row: dict[str, Any], run_id: str | None, rule_versions: dict[str, str]) -> dict[str, Any]:
    props: dict[str, Any] = {}
    for prop in rel.all_properties:
        if prop == "projection_run_id":
            props[prop] = run_id
        elif prop == "rule_version":
            props[prop] = row.get("rule_version") or rule_versions[rel.type]
        elif prop in row:
            props[prop] = _clean_list(row[prop])
    return clean_props(props)


@dataclass(frozen=True)
class NodeRow:
    id: str
    props: dict[str, Any]


@dataclass(frozen=True)
class RelRow:
    from_id: str
    to_id: str
    props: dict[str, Any]

    @property
    def sort_key(self) -> tuple[str, str, str]:
        return (self.from_id, self.to_id, str(self.props.get("canonical_row_id") or ""))


def iter_nodes(inp: ProjectionInput, node: S.NodeType, run_id: str | None) -> Iterator[NodeRow]:
    ensure_rule_views(inp)
    for row in inp.iter_rows(node_sql(node)):
        props = node_props(node, row, run_id)
        yield NodeRow(props["id"], props)


def iter_nodes_by_ids(inp: ProjectionInput, node: S.NodeType, ids: list[str]) -> Iterator[NodeRow]:
    ensure_rule_views(inp)
    sql = f"SELECT * FROM ({node_sql(node)}) sub WHERE list_contains(?::VARCHAR[], sub.{node.key}) ORDER BY sub.{node.key}"
    for row in inp.iter_rows(sql, [list(ids)]):
        props = node_props(node, row, None)
        yield NodeRow(props["id"], props)


def iter_rels(inp: ProjectionInput, rel: S.RelType, run_id: str | None) -> Iterator[RelRow]:
    ensure_rule_views(inp)
    for row in inp.iter_rows(rel_sql(rel)):
        yield RelRow(row["from_id"], row["to_id"], rel_props(rel, row, run_id, inp.rule_versions))


def expected_counts(inp: ProjectionInput) -> dict[str, dict[str, int]]:
    ensure_rule_views(inp)
    nodes = {n.label: int(inp.scalar(f"SELECT count(*) FROM ({node_sql(n)})")) for n in S.NODE_TYPES}
    rels = {r.type: int(inp.scalar(f"SELECT count(*) FROM ({rel_sql(r)})")) for r in S.REL_TYPES}
    return {"nodes": nodes, "rels": rels}


def not_projected_report(inp: ProjectionInput, limit: int = 50) -> dict[str, Any]:
    """Canonical rows that deliberately give no edge, by reason (receipts; never a silent drop)."""
    ensure_rule_views(inp)
    out: dict[str, Any] = {}
    limit = int(limit)
    links = inp.fetch(f"SELECT reason, count(*) AS n, list_slice(list(link_id ORDER BY link_id), 1, {limit}) "
                      "AS examples FROM x_links_not_projected GROUP BY reason ORDER BY reason")
    out["source_work_links"] = {r["reason"]: {"n": r["n"], "examples": r["examples"]} for r in links}
    for rel_name, table, key in (("work_relations", "e_work_relations", "relation_id"),
                                 ("source_relations", "e_source_relations", "relation_id")):
        rows = inp.fetch(f"SELECT coalesce(curation_status, 'NULL') AS status, count(*) AS n, "
                         f"list_slice(list({key} ORDER BY {key}), 1, {limit}) AS examples FROM {table} "
                         f"WHERE coalesce(curation_status, '') <> '{CURATED}' GROUP BY 1 ORDER BY 1")
        out[rel_name] = {r["status"]: {"n": r["n"], "examples": r["examples"]} for r in rows}
    unknown = inp.fetch("SELECT citing_work_resolution AS resolution, count(*) AS n FROM e_bibliography "
                        "WHERE citing_work_id IS NULL GROUP BY 1 ORDER BY 1")
    out["bibliography_citing_work_unknown"] = {r["resolution"] or "NULL": r["n"] for r in unknown}
    out["bibliography_links_not_accepted"] = {
        r["match_status"]: r["n"] for r in inp.fetch(
            "SELECT match_status, count(*) AS n FROM e_bibliography_links WHERE NOT accepted GROUP BY 1 ORDER BY 1")}
    return out

"""Cypher templates of the DOCUMENT projector.

Labels and relationship types come only from the registry (validated and backquoted); every value is a ``$``
parameter. The templates use the subset common to Cypher 5 (Neo4j 5.26 LTS) and Cypher 25 (2025.x/2026.x): no dynamic
labels, no APOC. ``CALL {…} IN TRANSACTIONS`` is used only by the wipe, which runs as an auto-commit transaction.
"""
from __future__ import annotations

from vkm_corpus.graph import schema as S
from vkm_corpus.graph.schema import Namespace, q


def node_merge(ns: Namespace, node: S.NodeType) -> str:
    """Batch upsert of nodes; ``row.props`` holds the whole whitelisted property map (``SET n = map``)."""
    return (f"UNWIND $rows AS row\n"
            f"MERGE (n:{q(ns.label(node.label))} {{id: row.id}})\n"
            f"SET n = row.props, n:{q(ns.layer_label)}\n"
            f"RETURN count(n) AS written")


def rel_merge(ns: Namespace, rel: S.RelType) -> str:
    """Batch upsert of relationships between existing nodes. A row whose endpoint is missing is not written: the
    caller compares ``written`` with the batch size and reports ``E_DANGLING_REFERENCE`` (no silent loss)."""
    keys = ", ".join(f"{k}: row.props.{k}" for k in rel.merge_keys)
    pattern = f"[r:{q(ns.rel(rel.type))} {{{keys}}}]" if keys else f"[r:{q(ns.rel(rel.type))}]"
    return (f"UNWIND $rows AS row\n"
            f"MATCH (a:{q(ns.label(rel.start))} {{id: row.from_id}})\n"
            f"MATCH (b:{q(ns.label(rel.end))} {{id: row.to_id}})\n"
            f"MERGE (a)-{pattern}->(b)\n"
            f"SET r = row.props\n"
            f"RETURN count(r) AS written")


def rel_missing_endpoints(ns: Namespace, rel: S.RelType) -> str:
    return (f"UNWIND $rows AS row\n"
            f"OPTIONAL MATCH (a:{q(ns.label(rel.start))} {{id: row.from_id}})\n"
            f"OPTIONAL MATCH (b:{q(ns.label(rel.end))} {{id: row.to_id}})\n"
            f"WITH row, a, b WHERE a IS NULL OR b IS NULL\n"
            f"RETURN row.from_id AS from_id, row.to_id AS to_id, a IS NULL AS missing_from, b IS NULL AS missing_to\n"
            f"LIMIT 50")


def wipe_layer(ns: Namespace, batch: int) -> str:
    """Delete the whole layer in batches (auto-commit transaction only)."""
    batch = int(batch)
    if not 100 <= batch <= 100_000:
        raise ValueError("wipe batch out of range")
    return (f"MATCH (n:{q(ns.layer_label)})\n"
            f"CALL (n) {{ DETACH DELETE n }} IN TRANSACTIONS OF {batch} ROWS")


def cross_layer_edges(ns: Namespace) -> str:
    """Edges between the layer and any node outside it (R3/R6 guard before a wipe)."""
    layer = q(ns.layer_label)
    return (f"MATCH (d:{layer})-[r]-(x) WHERE NOT x:{layer}\n"
            f"RETURN type(r) AS rel_type, labels(x) AS other_labels, count(r) AS n")


def count_label(ns: Namespace, label: str) -> str:
    return f"MATCH (n:{q(ns.label(label))}) RETURN count(n) AS n"


def count_layer(ns: Namespace) -> str:
    return f"MATCH (n:{q(ns.layer_label)}) RETURN count(n) AS n"


def count_rel(ns: Namespace, rel_type: str) -> str:
    return f"MATCH ()-[r:{q(ns.rel(rel_type))}]->() RETURN count(r) AS n"


def export_nodes(ns: Namespace, node: S.NodeType) -> str:
    return f"MATCH (n:{q(ns.label(node.label))}) RETURN n.id AS id, properties(n) AS props ORDER BY n.id"


def export_rels(ns: Namespace, rel: S.RelType) -> str:
    return (f"MATCH (a:{q(ns.label(rel.start))})-[r:{q(ns.rel(rel.type))}]->(b:{q(ns.label(rel.end))})\n"
            f"RETURN a.id AS from_id, b.id AS to_id, properties(r) AS props\n"
            f"ORDER BY from_id, to_id, coalesce(r.canonical_row_id, '')")


def nodes_by_ids(ns: Namespace, node: S.NodeType) -> str:
    return (f"UNWIND $ids AS id MATCH (n:{q(ns.label(node.label))} {{id: id}}) "
            f"RETURN n.id AS id, properties(n) AS props ORDER BY id")


# ---------------------------------------------------------------- read templates for the API/MCP (agent G)
# All run in READ transactions (routing READ / execute_read); they return IDs and statuses, texts come from DuckDB.
def read_neighbors(ns: Namespace) -> str:
    page, src, work = q(ns.label("Page")), q(ns.label("Source")), q(ns.label("Work"))
    precedes, has_page, instance = q(ns.rel("PRECEDES")), q(ns.rel("HAS_PAGE")), q(ns.rel("INSTANCE_OF"))
    objs = "|".join(q(ns.rel(t)) for t in S.HAS_OBJECT_REL.values())
    return (f"MATCH (p:{page} {{id: $page_id}})\n"
            f"OPTIONAL MATCH (prev:{page})-[:{precedes}]->(p)\n"
            f"OPTIONAL MATCH (p)-[:{precedes}]->(next:{page})\n"
            f"OPTIONAL MATCH (s:{src})-[:{has_page}]->(p)\n"
            f"OPTIONAL MATCH (s)-[:{instance}]->(w:{work})\n"
            f"CALL (p) {{ OPTIONAL MATCH (p)-[r:{objs}]->(o) "
            f"RETURN collect({{rel: type(r), id: o.id, review_status: o.review_status}}) AS objects }}\n"
            f"RETURN p.id AS page_id, prev.id AS prev_page_id, next.id AS next_page_id, s.id AS source_id, "
            f"w.id AS work_id, objects")


def read_citations(ns: Namespace, direction: str = "out") -> str:
    work, cites = q(ns.label("Work")), q(ns.rel("CITES"))
    if direction == "out":
        pattern, other = f"(w:{work} {{id: $work_id}})-[c:{cites}]->(x:{work})", "cited_work_id"
    else:
        pattern, other = f"(x:{work})-[c:{cites}]->(w:{work} {{id: $work_id}})", "citing_work_id"
    return (f"MATCH {pattern}\n"
            f"RETURN x.id AS {other}, c.n_citing_entries AS n_citing_entries, c.n_citing_sources AS n_citing_sources, "
            f"c.via_entry_ids AS via_entry_ids, c.match_methods AS match_methods, c.flags AS flags, "
            f"c.rule_version AS rule_version ORDER BY {other}")

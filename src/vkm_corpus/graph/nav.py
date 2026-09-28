"""``vkm-corpus nav graph-ddl | graph-load | graph-verify | graph-drop``: the NAV layer in Neo4j.

Flow of ``graph-load --nav-dir DIR`` (DIR = ``derived/navigation/<snapshot_id>`` of ``vkm-corpus nav build``)::

    manifest.json + <dataset>.parquet (sha256 and rows checked) → rows by the projection rules (vkm_corpus.graph.nav_rows)
      → preflight P1–P7 (ids, parents, acyclic trees, pages per section, row accounting, references, topic columns)
      → [--dry-run: counts and accounting only, no database]
      → DDL (idempotent) → gate: the DOCUMENT graph is READY and built from the same snapshot
      → DOCUMENT counts before → NavMeta LOADING → UNWIND/MERGE batches by id (nodes, then edges)
      → removal of NAV nodes and edges of any other snapshot → DOCUMENT counts after
      → checks N1–N7 → NavMeta COMPLETE | FAILED (+ manifest, counts, checks) → receipt

Checks: N1 every edge to a DOCUMENT node resolves; N2 DOCUMENT node/edge counts unchanged (and equal to the counts of
its build); N3 NAV trees acyclic, one parent; N4 each NavSection covers ≥ 1 page; N5 loaded counts equal the rows of
the projection rules (and every dataset row is accounted for); N6 no NAV label or type collides with DOCUMENT ones;
N7 every NAV node and edge carries ``layer = 'NAV'``, the loaded ``snapshot_id`` and a ``rule_version``.

Every Cypher statement that reads or writes data starts with a ``// vkm-nav:<op> [arg]`` comment (the offline fake
driver of the tests dispatches on it). The DOCUMENT layer is only read.
"""
from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, TypeVar

from vkm_corpus.config import Settings
from vkm_corpus.graph import nav_schema as N
from vkm_corpus.graph import schema as S
from vkm_corpus.graph.common import (LOCKS_DIR, CheckResult, FileLock, ProjectionError, check, failures,
                                     normalize_value, utc_now, utc_stamp, write_receipt)
from vkm_corpus.graph.nav_query import timed as _query
from vkm_corpus.graph.nav_rows import (NavInput, ProjectionOptions, accounting, document_references, expected_counts,
                                       iter_nodes, iter_rels, preflight, tree_problems)
from vkm_corpus.graph.schema import Namespace, q

T = TypeVar("T")
HEARTBEAT_SECONDS = 30.0
BUSY_AFTER_SECONDS = 900
RECEIPT_ENGINE = "neo4j-nav"
LOADING, COMPLETE, FAILED = "LOADING", "COMPLETE", "FAILED"


# ---------------------------------------------------------------- driver calls (the neo4j package is optional here)
def read(driver: Any, database: str, text: str, **params: Any) -> list[dict[str, Any]]:
    records, _, _ = driver.execute_query(text, parameters_=params, database_=database, routing_="r")
    return [dict(r) for r in records]


def write(driver: Any, database: str, text: str, timeout: float | None = 600.0, **params: Any) -> list[dict[str, Any]]:
    records, _, _ = driver.execute_query(_query(text, timeout), parameters_=params, database_=database)
    return [dict(r) for r in records]


def _one(rows: list[dict[str, Any]], key: str = "n") -> int:
    return int((rows[0].get(key) if rows else 0) or 0)


# ---------------------------------------------------------------- Cypher templates
def cy_merge_nodes(ns: Namespace, node: N.NavNodeType) -> str:
    return (f"// vkm-nav:merge-nodes {node.label}\n"
            f"UNWIND $rows AS row\n"
            f"MERGE (n:{q(ns.label(node.label))} {{id: row.id}})\n"
            f"SET n = row.props, n:{q(ns.label(N.LAYER_LABEL))}\n"
            f"RETURN count(n) AS written")


def cy_merge_rels(ns: Namespace, rel: N.NavRelType) -> str:
    pattern = (f"[r:{q(ns.rel(rel.type))} {{{rel.key}: row.key}}]" if rel.key else f"[r:{q(ns.rel(rel.type))}]")
    return (f"// vkm-nav:merge-rels {rel.name}\n"
            f"UNWIND $rows AS row\n"
            f"MATCH (a:{q(ns.label(rel.start))} {{id: row.from_id}})\n"
            f"MATCH (b:{q(ns.label(rel.end))} {{id: row.to_id}})\n"
            f"MERGE (a)-{pattern}->(b)\n"
            f"SET r = row.props\n"
            f"RETURN count(r) AS written")


def cy_missing_endpoints(ns: Namespace, rel: N.NavRelType) -> str:
    return (f"// vkm-nav:missing-endpoints {rel.name}\n"
            f"UNWIND $rows AS row\n"
            f"OPTIONAL MATCH (a:{q(ns.label(rel.start))} {{id: row.from_id}})\n"
            f"OPTIONAL MATCH (b:{q(ns.label(rel.end))} {{id: row.to_id}})\n"
            f"WITH row, a, b WHERE a IS NULL OR b IS NULL\n"
            f"RETURN row.from_id AS from_id, row.to_id AS to_id, a IS NULL AS missing_from, b IS NULL AS missing_to\n"
            f"LIMIT 20")


def cy_sweep_rels(ns: Namespace, rel_type: str) -> str:
    """Edges this load did not write (another snapshot, or rows the projection rules no longer keep)."""
    return (f"// vkm-nav:sweep-rels {rel_type}\n"
            f"MATCH ()-[r:{q(ns.rel(rel_type))}]->() WHERE coalesce(r.projection_run_id, '') <> $run_id\n"
            f"WITH r LIMIT $batch DELETE r RETURN count(r) AS n")


def cy_sweep_nodes(ns: Namespace) -> str:
    return (f"// vkm-nav:sweep-nodes\n"
            f"MATCH (n:{q(ns.label(N.LAYER_LABEL))}) WHERE coalesce(n.projection_run_id, '') <> $run_id "
            f"AND NOT n:{q(ns.label(N.META_LABEL))}\n"
            f"WITH n LIMIT $batch DETACH DELETE n RETURN count(n) AS n")


def cy_drop_rels(ns: Namespace, rel_type: str) -> str:
    return (f"// vkm-nav:drop-rels {rel_type}\n"
            f"MATCH ()-[r:{q(ns.rel(rel_type))}]->() WITH r LIMIT $batch DELETE r RETURN count(r) AS n")


def cy_drop_nodes(ns: Namespace) -> str:
    return (f"// vkm-nav:drop-nodes\n"
            f"MATCH (n:{q(ns.label(N.LAYER_LABEL))}) WITH n LIMIT $batch DETACH DELETE n RETURN count(n) AS n")


def cy_count_label(ns: Namespace, label: str) -> str:
    return f"// vkm-nav:count-label {label}\nMATCH (n:{q(ns.label(label))}) RETURN count(n) AS n"


def cy_count_rel(ns: Namespace, rel: N.NavRelType) -> str:
    shared = sum(1 for r in N.REL_TYPES if r.type == rel.type) > 1
    if shared:                     # NAV_CHILD_OF of sections and of topics: count by endpoints
        return (f"// vkm-nav:count-rel {rel.name}\n"
                f"MATCH (:{q(ns.label(rel.start))})-[r:{q(ns.rel(rel.type))}]->(:{q(ns.label(rel.end))}) "
                f"RETURN count(r) AS n")
    return f"// vkm-nav:count-rel {rel.name}\nMATCH ()-[r:{q(ns.rel(rel.type))}]->() RETURN count(r) AS n"


def cy_doc_count_label(ns: Namespace, label: str) -> str:
    return f"// vkm-nav:doc-count-label {label}\nMATCH (n:{q(ns.label(label))}) RETURN count(n) AS n"


def cy_doc_count_rel(ns: Namespace, rel_type: str) -> str:
    return f"// vkm-nav:doc-count-rel {rel_type}\nMATCH ()-[r:{q(ns.rel(rel_type))}]->() RETURN count(r) AS n"


def cy_doc_state(ns: Namespace) -> str:
    return (f"// vkm-nav:doc-state\n"
            f"MATCH (r:{q(ns.run_label)} {{layer: $layer}}) RETURN r.id AS id, r.status AS status, "
            f"r.built_from_snapshot_id AS snapshot_id, r.counts_json AS counts_json, r.content_digest AS content_digest "
            f"ORDER BY r.started_at DESC, r.id DESC LIMIT 1")


def cy_meta_get(ns: Namespace) -> str:
    return (f"// vkm-nav:meta-get\n"
            f"MATCH (m:{q(ns.label(N.META_LABEL))} {{id: $id}}) RETURN properties(m) AS props, "
            f"CASE WHEN m.heartbeat_at IS NULL THEN NULL "
            f"ELSE duration.inSeconds(m.heartbeat_at, datetime()).seconds END AS age")


def cy_meta_set(ns: Namespace) -> str:
    return (f"// vkm-nav:meta-set\n"
            f"MERGE (m:{q(ns.label(N.META_LABEL))} {{id: $id}})\n"
            f"SET m += $props, m:{q(ns.label(N.LAYER_LABEL))}, m.heartbeat_at = datetime()\n"
            f"RETURN m.id AS id")


def cy_tree_edges(ns: Namespace, label: str) -> str:
    return (f"// vkm-nav:tree-edges {label}\n"
            f"MATCH (c:{q(ns.label(label))})-[:{q(ns.rel('NAV_CHILD_OF'))}]->(p:{q(ns.label(label))}) "
            f"RETURN c.id AS child, p.id AS parent")


def cy_sections_without_pages(ns: Namespace) -> str:
    return (f"// vkm-nav:sections-without-pages\n"
            f"MATCH (s:{q(ns.label('NavSection'))}) WHERE NOT (s)-[:{q(ns.rel('COVERS_PAGE'))}]->() "
            f"RETURN count(s) AS n, collect(s.id)[0..20] AS examples")


def cy_collisions(ns: Namespace) -> str:
    return (f"// vkm-nav:collisions\n"
            f"MATCH (n:{q(ns.label(N.LAYER_LABEL))}) WHERE n:{q(ns.layer_label)} "
            f"OR any(l IN labels(n) WHERE l IN $doc_labels) OR size([l IN labels(n) WHERE l IN $nav_types]) <> 1 "
            f"RETURN count(n) AS n")


def cy_doc_with_nav_labels(ns: Namespace) -> str:
    return (f"// vkm-nav:doc-with-nav-labels\n"
            f"MATCH (n:{q(ns.layer_label)}) WHERE any(l IN labels(n) WHERE l IN $nav_labels) RETURN count(n) AS n")


def cy_bad_node_props(ns: Namespace) -> str:
    return (f"// vkm-nav:bad-node-props\n"
            f"MATCH (n:{q(ns.label(N.LAYER_LABEL))}) WHERE NOT n:{q(ns.label(N.META_LABEL))} AND (n.layer IS NULL "
            f"OR n.layer <> $layer OR coalesce(n.snapshot_id, '') <> $snapshot OR n.rule_version IS NULL "
            f"OR n.id IS NULL OR ($run_id IS NOT NULL AND coalesce(n.projection_run_id, '') <> $run_id)) "
            f"RETURN count(n) AS n")


def cy_bad_rel_props(ns: Namespace, rel_type: str) -> str:
    return (f"// vkm-nav:bad-rel-props {rel_type}\n"
            f"MATCH ()-[r:{q(ns.rel(rel_type))}]->() WHERE r.layer IS NULL OR r.layer <> $layer "
            f"OR coalesce(r.snapshot_id, '') <> $snapshot OR r.rule_version IS NULL "
            f"OR ($run_id IS NOT NULL AND coalesce(r.projection_run_id, '') <> $run_id) RETURN count(r) AS n")


SERVER_INFO = ("// vkm-nav:server\n"
               "CALL dbms.components() YIELD name, versions, edition RETURN name, versions, edition")


# ---------------------------------------------------------------- DDL
def apply_ddl(driver: Any, database: str, ns: Namespace) -> dict[str, Any]:
    """Idempotent NAV DDL, then confirm every constraint and index exists."""
    import hashlib

    items = N.ddl_items(ns)
    for item in items:
        write(driver, database, item.statement, timeout=None)
    names = {r["name"] for r in read(driver, database, "SHOW CONSTRAINTS YIELD name RETURN name")}
    names |= {r["name"] for r in read(driver, database, "SHOW INDEXES YIELD name RETURN name")}
    missing = [i.name for i in items if i.name not in names]
    if missing:
        raise ProjectionError("E_DDL", f"NAV DDL items missing after apply: {missing}", stage="ddl")
    return {"items": len(items), "ddl_sha256": hashlib.sha256(N.ddl_script(ns).encode("utf-8")).hexdigest()}


# ---------------------------------------------------------------- DOCUMENT state and counts (read only)
def document_state(driver: Any, database: str, ns: Namespace) -> dict[str, Any]:
    rows = read(driver, database, cy_doc_state(ns), layer=S.LAYER)
    if not rows:
        return {"state": "EMPTY"}
    row = rows[0]
    counts = json.loads(row["counts_json"]) if row.get("counts_json") else None
    return {"state": "READY" if row.get("status") == "COMPLETE" else str(row.get("status")), "build_id": row.get("id"),
            "snapshot_id": row.get("snapshot_id"), "counts": counts, "content_digest": row.get("content_digest")}


def document_counts(driver: Any, database: str, ns: Namespace) -> dict[str, dict[str, int]]:
    nodes = {n.label: _one(read(driver, database, cy_doc_count_label(ns, n.label))) for n in S.NODE_TYPES}
    rels = {r.type: _one(read(driver, database, cy_doc_count_rel(ns, r.type))) for r in S.REL_TYPES}
    return {"nodes": nodes, "rels": rels}


# ---------------------------------------------------------------- NavMeta
def meta_get(driver: Any, database: str, ns: Namespace) -> tuple[dict[str, Any] | None, int | None]:
    rows = read(driver, database, cy_meta_get(ns), id=N.META_ID)
    if not rows:
        return None, None
    return dict(rows[0].get("props") or {}), rows[0].get("age")


def meta_set(driver: Any, database: str, ns: Namespace, props: dict[str, Any]) -> None:
    data = {}
    for k, v in props.items():
        if v is None:
            continue
        data[k] = json.dumps(normalize_value(v), sort_keys=True, ensure_ascii=False) if isinstance(v, dict) \
            else normalize_value(v)
    write(driver, database, cy_meta_set(ns), id=N.META_ID, props=data)


def nav_state(driver: Any, database: str = "neo4j", ns: Namespace = Namespace()) -> dict[str, Any]:
    """Readiness of the NAV graph for readers: ``READY`` only when NavMeta says COMPLETE."""
    props, _age = meta_get(driver, database, ns)
    if not props:
        return {"state": "EMPTY", "http_status": 503}
    status = props.get("status")
    out = {"state": "READY" if status == COMPLETE else ("LOADING" if status == LOADING else "FAILED"),
           "http_status": 200 if status == COMPLETE else 503, "snapshot_id": props.get("snapshot_id"),
           "run_id": props.get("run_id"), "finished_at": normalize_value(props.get("finished_at")),
           "graph_schema_version": props.get("graph_schema_version"), "rule_versions": props.get("rule_versions")}
    return out


# ---------------------------------------------------------------- loading
def _batches(items: Iterable[T], size: int) -> Iterator[list[T]]:
    batch: list[T] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def load_nodes(driver: Any, database: str, ns: Namespace, inp: NavInput, skipped: dict[str, str], batch_size: int,
               beat: Callable[[], None], run_id: str | None = None) -> dict[str, int]:
    written: dict[str, int] = {}
    for node in N.NODE_TYPES:
        if node.label in skipped:
            continue
        query, total = cy_merge_nodes(ns, node), 0
        for batch in _batches(iter_nodes(inp, node, run_id), batch_size):
            rows = [{"id": r.id, "props": r.props} for r in batch]
            n = _one(write(driver, database, query, rows=rows), "written")
            if n != len(rows):
                raise ProjectionError("E_LOAD_MISMATCH", f"{node.label}: wrote {n} of {len(rows)} nodes", stage="load")
            total += n
            beat()
        written[node.label] = total
    return written


def load_rels(driver: Any, database: str, ns: Namespace, inp: NavInput, skipped: dict[str, str], batch_size: int,
              beat: Callable[[], None], run_id: str | None = None) -> tuple[dict[str, int], dict[str, dict[str, Any]]]:
    """Edges by registry entry; a row whose endpoint is missing is not written and is reported (check N1)."""
    written: dict[str, int] = {}
    dangling: dict[str, dict[str, Any]] = {}
    for rel in N.REL_TYPES:
        if rel.name in skipped:
            continue
        query, total = cy_merge_rels(ns, rel), 0
        for batch in _batches(iter_rels(inp, rel, run_id), batch_size):
            rows = [{"from_id": r.from_id, "to_id": r.to_id, "key": r.key, "props": r.props} for r in batch]
            n = _one(write(driver, database, query, rows=rows), "written")
            if n != len(rows):
                d = dangling.setdefault(rel.name, {"n": 0, "examples": []})
                d["n"] += len(rows) - n
                if len(d["examples"]) < 20:
                    d["examples"] += read(driver, database, cy_missing_endpoints(ns, rel), rows=rows)[:20]
            total += n
            beat()
        written[rel.name] = total
    return written, dangling


def sweep(driver: Any, database: str, ns: Namespace, run_id: str, batch: int) -> dict[str, int]:
    """Delete NAV edges and nodes this load did not write — another snapshot, or rows the projection rules no longer
    keep — in bounded write transactions."""
    out = {"rels_deleted": 0, "nodes_deleted": 0, "transactions": 0}
    for rel_type in N.REL_TYPE_NAMES:
        while True:
            n = _one(write(driver, database, cy_sweep_rels(ns, rel_type), run_id=run_id, batch=batch))
            out["transactions"] += 1
            out["rels_deleted"] += n
            if n < batch:
                break
    while True:
        n = _one(write(driver, database, cy_sweep_nodes(ns), run_id=run_id, batch=batch))
        out["transactions"] += 1
        out["nodes_deleted"] += n
        if n < batch:
            break
    return out


def drop_layer(driver: Any, database: str, ns: Namespace = Namespace(), batch: int = 10_000) -> dict[str, int]:
    """Remove the whole NAV layer (edges of every NAV type, then NAV nodes incl. NavMeta); DOCUMENT stays."""
    out = {"rels_deleted": 0, "nodes_deleted": 0, "transactions": 0}
    for rel_type in N.REL_TYPE_NAMES:
        while True:
            n = _one(write(driver, database, cy_drop_rels(ns, rel_type), batch=batch))
            out["transactions"] += 1
            out["rels_deleted"] += n
            if n < batch:
                break
    while True:
        n = _one(write(driver, database, cy_drop_nodes(ns), batch=batch))
        out["transactions"] += 1
        out["nodes_deleted"] += n
        if n < batch:
            break
    return out


def purge_test_namespace(driver: Any, database: str, ns: Namespace) -> dict[str, int]:
    """Remove the NAV layer and the NAV DDL of a *test* namespace (live tests); refuses the production namespace."""
    if not ns.is_test:
        raise ProjectionError("E_REFUSED", "refusing to purge the production NAV namespace", stage="cleanup")
    out = drop_layer(driver, database, ns)
    for item in reversed(N.ddl_items(ns)):
        write(driver, database, f"DROP {'CONSTRAINT' if item.kind == 'CONSTRAINT' else 'INDEX'} {item.name} IF EXISTS",
              timeout=None)
    return out


# ---------------------------------------------------------------- checks N1–N7
def graph_counts(driver: Any, database: str, ns: Namespace) -> dict[str, dict[str, int]]:
    nodes = {n.label: _one(read(driver, database, cy_count_label(ns, n.label))) for n in N.NODE_TYPES}
    rels = {r.name: _one(read(driver, database, cy_count_rel(ns, r))) for r in N.REL_TYPES}
    return {"nodes": nodes, "rels": rels}


def run_checks(driver: Any, database: str, ns: Namespace, *, expected: dict[str, Any], snapshot_id: str,
               dangling: dict[str, dict[str, Any]] | None = None, doc_before: dict[str, Any] | None = None,
               doc_after: dict[str, Any] | None = None, doc_state: dict[str, Any] | None = None,
               accounting_closed: list[str] | None = None,
               run_id: str | None = None) -> tuple[list[CheckResult], dict[str, Any]]:
    results: list[CheckResult] = []
    counts = graph_counts(driver, database, ns)

    # N1 edges to DOCUMENT nodes resolve
    viol = [f"{name}: {d['n']} row(s) without an endpoint" for name, d in sorted((dangling or {}).items())]
    for rel in N.REL_TYPES:
        if rel.cross_layer and rel.name not in expected["skipped"]:
            want, got = expected["rels"][rel.name], counts["rels"][rel.name]
            if got != want:
                viol.append(f"{rel.name}: graph {got} != rows {want}")
    results.append(check("N1", "every NAV edge to a DOCUMENT node resolves (Page, Block, Formula, Source)", viol,
                         code="E_DANGLING_REFERENCE",
                         details={"dangling": {k: {"n": v["n"], "examples": v["examples"][:5]}
                                               for k, v in (dangling or {}).items()}} if dangling else None))

    # N2 DOCUMENT untouched
    diff: list[str] = []
    after = doc_after or document_counts(driver, database, ns)
    if doc_before is not None:
        for part in ("nodes", "rels"):
            for k in sorted(set(doc_before[part]) | set(after[part])):
                if doc_before[part].get(k) != after[part].get(k):
                    diff.append(f"{k}: before {doc_before[part].get(k)} after {after[part].get(k)}")
    built = (doc_state or {}).get("counts")
    if built:
        for part in ("nodes", "rels"):
            for k, v in sorted((built.get(part) or {}).items()):
                if after[part].get(k) is not None and int(after[part][k]) != int(v):
                    diff.append(f"{k}: graph {after[part][k]} != DOCUMENT build {v}")
    results.append(check("N2", "DOCUMENT node and edge counts unchanged (before = after = its build)", diff,
                         code="E_INVARIANT_VIOLATION"))

    # N3 acyclic trees
    trees: list[str] = []
    for label in ("NavSection", "NavTopic"):
        pairs = [(r["child"], r["parent"]) for r in read(driver, database, cy_tree_edges(ns, label))]
        trees += [f"{label} {p}" for p in tree_problems(pairs)]
    results.append(check("N3", "NAV trees (sections, topics) are acyclic, one parent per node", trees,
                         code="E_INVARIANT_VIOLATION"))

    # N4 pages per section
    rows = read(driver, database, cy_sections_without_pages(ns))
    n = _one(rows)
    results.append(check("N4", "each NavSection covers at least one Page", n, code="E_ORPHAN_NODES",
                         details={"examples": (rows[0].get("examples") or [])[:20]} if n else None))

    # N5 loaded counts equal the rows of the projection rules
    mism = [f"{k}: graph {counts['nodes'][k]} != rows {v}" for k, v in expected["nodes"].items()
            if counts["nodes"][k] != v]
    mism += [f"{k}: graph {counts['rels'][k]} != rows {v}" for k, v in expected["rels"].items()
             if counts["rels"][k] != v]
    mism += [f"accounting not closed: {ds}" for ds in (accounting_closed or [])]
    results.append(check("N5", "loaded counts equal the parquet rows kept by the projection rules", mism,
                         code="E_COUNT_MISMATCH"))

    # N6 labels and types
    coll = [f"registry: {p}" for p in N.validate_registry()]
    doc_labels = sorted(ns.label(x) for x in N.document_labels())
    nav_types = sorted(ns.label(n.label) for n in N.NODE_TYPES) + [ns.label(N.META_LABEL)]
    nav_labels = sorted(ns.label(x) for x in N.NAV_LABELS)
    n = _one(read(driver, database, cy_collisions(ns), doc_labels=doc_labels, nav_types=nav_types))
    if n:
        coll.append(f"NAV nodes with DOCUMENT labels or not exactly one NAV type label: {n}")
    n = _one(read(driver, database, cy_doc_with_nav_labels(ns), nav_labels=nav_labels))
    if n:
        coll.append(f"DOCUMENT nodes with NAV labels: {n}")
    results.append(check("N6", "no NAV label or type collides with DOCUMENT labels and types", coll,
                         code="E_INVARIANT_VIOLATION"))

    # N7 provenance properties and no leftovers of another snapshot or load
    props = []
    n = _one(read(driver, database, cy_bad_node_props(ns), layer=N.LAYER_VALUE, snapshot=snapshot_id, run_id=run_id))
    if n:
        props.append(f"NAV nodes without layer/snapshot/rule_version of this load: {n}")
    for rel_type in N.REL_TYPE_NAMES:
        n = _one(read(driver, database, cy_bad_rel_props(ns, rel_type), layer=N.LAYER_VALUE, snapshot=snapshot_id,
                      run_id=run_id))
        if n:
            props.append(f"{rel_type}: {n} edge(s) without layer/snapshot/rule_version of this load")
    results.append(check("N7", "every NAV node and edge carries layer=NAV, the loaded snapshot_id and a rule_version",
                         props, code="E_INVARIANT_VIOLATION"))
    return results, counts


def summarize(results: list[CheckResult]) -> dict[str, int]:
    out = {"PASS": 0, "WARN": 0, "SKIP": 0, "FAIL": 0}
    for r in results:
        out[r.status] = out.get(r.status, 0) + 1
    return out


# ---------------------------------------------------------------- the run
@dataclass
class NavLoadOptions:
    nav_dir: Path
    batch_nodes: int = 5_000
    batch_rels: int = 10_000
    sweep_batch: int = 10_000
    dry_run: bool = False
    allow_snapshot_mismatch: bool = False
    canon_duckdb: Path | None = None
    projection: ProjectionOptions = field(default_factory=ProjectionOptions)
    namespace: Namespace = field(default_factory=Namespace)
    database: str | None = None
    command: str = "nav graph-load"


def new_run_id() -> str:
    return f"VKM-PRJ-NAV-{utc_stamp()}-{secrets.token_hex(4)}"


def plan(inp: NavInput, options: NavLoadOptions) -> dict[str, Any]:
    """Everything the dry run reports: input, rules, expected counts, accounting and preflight (no database)."""
    t0 = time.monotonic()
    expected = expected_counts(inp)
    acct = accounting(inp, expected)
    pre = preflight(inp, expected, acct)
    out: dict[str, Any] = {
        "input": {"snapshot_id": inp.snapshot_id, "nav_manifest_sha256": inp.manifest_sha256,
                  "manifest_built_at": inp.manifest.get("built_at"),
                  "parts": {k: {"status": v.get("status"), "rule_version": v.get("rule_version")}
                            for k, v in sorted((inp.manifest.get("parts") or {}).items())},
                  "datasets": {k: {"rows": (inp.manifest["datasets"][k] or {}).get("rows"),
                                   "sha256": (inp.manifest["datasets"][k] or {}).get("sha256")}
                               for k in sorted(inp.datasets)}},
        "rules": inp.options.rules(), "expected_counts": expected, "accounting": acct,
        "preflight": [r.as_dict() for r in pre], "preflight_summary": summarize(pre),
        "totals": {"nodes": sum(expected["nodes"].values()), "rels": sum(expected["rels"].values())}}
    if options.canon_duckdb:
        out["document_references"] = document_references(inp, options.canon_duckdb)
    out["plan_seconds"] = round(time.monotonic() - t0, 2)
    out["_preflight_results"] = pre
    return out


def load(settings: Settings | None, options: NavLoadOptions, *, driver: Any = None,
         data_root: Path | None = None) -> dict[str, Any]:
    """``nav graph-load``: returns the receipt (``--dry-run``: the plan only)."""
    ns = options.namespace
    database = options.database or (settings.neo4j_database if settings else "neo4j")
    receipt: dict[str, Any] = {"engine": "neo4j", "layer": N.LAYER_KEY, "command": options.command,
                               "graph_schema_version": N.NAV_GRAPH_SCHEMA_VERSION, "loader_version": N.NAV_LOADER_RULE,
                               "namespace": ns.prefix or None, "started_at": utc_now(), "status": "PLANNED",
                               "review_status": N.REVIEW_STATUS,
                               "note": "DERIVED navigation layer: never evidence; co-occurrence is not a claim"}
    t0 = time.monotonic()
    with NavInput.open(options.nav_dir, options.projection) as inp:
        planned = plan(inp, options)
        pre = planned.pop("_preflight_results")
        receipt.update(planned)
        bad = failures(pre)
        if options.dry_run:
            receipt["status"] = "DRY_RUN" if not bad else "DRY_RUN_PREFLIGHT_FAILED"
            receipt["finished_at"] = utc_now()
            receipt["timings_s"] = {"total": round(time.monotonic() - t0, 2)}
            return receipt
        if bad:
            raise ProjectionError(bad[0].code or "E_PREFLIGHT", "NAV preflight failed: " +
                                  ", ".join(c.check_id for c in bad), stage="preflight",
                                  details={"preflight": [c.as_dict() for c in bad]})
        root = data_root
        if root is None and settings is not None and settings.data_root is not None:
            try:
                root = settings.require_data_root()
            except Exception:  # noqa: BLE001 — receipts and the host lock are optional off CORE
                root = None
        lock = FileLock(root / LOCKS_DIR / f"neo4j-nav-projection{'-' + ns.prefix.lower() if ns.is_test else ''}.lock") \
            if root is not None else None
        if lock:
            lock.acquire()
        own_driver = driver is None
        try:
            if own_driver:
                from vkm_corpus.graph import client

                if settings is None:
                    raise ProjectionError("E_NO_SERVICE", "no Neo4j settings", stage="connect")
                driver = client.connect(settings)
            return _execute(driver, database, ns, inp, options, receipt, root, t0)
        finally:
            if own_driver and driver is not None:
                driver.close()
            if lock:
                lock.release()


def _execute(driver: Any, database: str, ns: Namespace, inp: NavInput, options: NavLoadOptions,
             receipt: dict[str, Any], root: Path | None, t0: float) -> dict[str, Any]:
    timings: dict[str, float] = {}
    run_id = new_run_id()
    receipt["run_id"] = run_id
    expected = receipt["expected_counts"]
    started_meta = False

    def lap(stage: str, started: float) -> None:
        timings[stage] = round(time.monotonic() - started, 2)

    def finish(status: str, error: dict[str, Any] | None = None) -> None:
        receipt["status"] = status
        if error:
            receipt["error"] = error
        receipt["finished_at"] = utc_now()
        timings["total"] = round(time.monotonic() - t0, 2)
        receipt["timings_s"] = timings
        if root is not None:
            receipt["receipt_ref"] = write_receipt(root, RECEIPT_ENGINE, run_id, receipt)

    try:
        started = time.monotonic()
        rows = read(driver, database, SERVER_INFO)
        kernel = next((r for r in rows if r.get("name") == "Neo4j Kernel"), {})
        receipt["server"] = {"version": (kernel.get("versions") or [None])[0], "edition": kernel.get("edition")}
        receipt["ddl"] = apply_ddl(driver, database, ns)
        doc = document_state(driver, database, ns)
        receipt["document"] = {k: doc.get(k) for k in ("state", "build_id", "snapshot_id", "content_digest")}
        if doc.get("state") != "READY":
            raise ProjectionError("E_REFUSED", f"the DOCUMENT graph is {doc.get('state')}: load NAV after a COMPLETE "
                                  "DOCUMENT build (vkm-corpus graph rebuild)", stage="gate")
        if doc.get("snapshot_id") != inp.snapshot_id:
            receipt["document"]["snapshot_mismatch"] = True
            if not options.allow_snapshot_mismatch:
                raise ProjectionError("E_REFUSED", f"NAV was built from {inp.snapshot_id}, the DOCUMENT graph from "
                                      f"{doc.get('snapshot_id')} (rebuild one of them, or --allow-snapshot-mismatch)",
                                      stage="gate")
        meta, age = meta_get(driver, database, ns)
        if meta and meta.get("status") == LOADING and age is not None and age < BUSY_AFTER_SECONDS:
            raise ProjectionError("E_PROJECTION_BUSY", f"another NAV load is running ({meta.get('run_id')})",
                                  stage="lock", retryable=True)
        previous = (meta or {}).get("snapshot_id")
        doc_before = document_counts(driver, database, ns)
        lap("prepare", started)

        meta_set(driver, database, ns, {
            "layer": N.LAYER_VALUE, "snapshot_id": previous or inp.snapshot_id, "rule_version": N.NAV_LOADER_RULE,
            "review_status": N.REVIEW_STATUS, "status": LOADING, "run_id": run_id,
            "loading_snapshot_id": inp.snapshot_id, "graph_schema_version": N.NAV_GRAPH_SCHEMA_VERSION,
            "loader_version": N.NAV_LOADER_RULE, "started_at": utc_now().isoformat(), "error_code": ""})
        started_meta = True
        last = [time.monotonic()]

        def beat() -> None:
            if time.monotonic() - last[0] >= HEARTBEAT_SECONDS:
                meta_set(driver, database, ns, {"status": LOADING})
                last[0] = time.monotonic()

        skipped = expected["skipped"]
        started = time.monotonic()
        nodes = load_nodes(driver, database, ns, inp, skipped, options.batch_nodes, beat, run_id)
        lap("nodes", started)
        started = time.monotonic()
        rels, dangling = load_rels(driver, database, ns, inp, skipped, options.batch_rels, beat, run_id)
        lap("rels", started)
        receipt["written"] = {"nodes": nodes, "rels": rels}
        started = time.monotonic()
        receipt["sweep"] = sweep(driver, database, ns, run_id, options.sweep_batch)
        lap("sweep", started)

        started = time.monotonic()
        doc_after = document_counts(driver, database, ns)
        open_acct = [ds for ds, a in receipt["accounting"].items() if isinstance(a, dict) and a.get("closed") is False]
        checks, counts = run_checks(driver, database, ns, expected=expected, snapshot_id=inp.snapshot_id,
                                    dangling=dangling, doc_before=doc_before, doc_after=doc_after, doc_state=doc,
                                    accounting_closed=open_acct, run_id=run_id)
        lap("checks", started)
        receipt["checks"] = [c.as_dict() for c in checks]
        receipt["checks_summary"] = summarize(checks)
        receipt["graph_counts"] = counts
        receipt["document"]["counts_unchanged"] = doc_before == doc_after
        bad = failures(checks)
        status = FAILED if bad else COMPLETE
        finish(status, {"code": bad[0].code, "stage": "verify",
                        "message": "checks failed: " + ", ".join(c.check_id for c in bad)} if bad else None)
        meta_set(driver, database, ns, {
            "status": status, "snapshot_id": inp.snapshot_id, "previous_snapshot_id": previous,
            "finished_at": utc_now().isoformat(), "nav_manifest_sha256": inp.manifest_sha256,
            "manifest_json": json.dumps(inp.manifest, sort_keys=True, ensure_ascii=False, default=str),
            "rule_versions": sorted(f"{k}={v.get('rule_version')}" for k, v in
                                    (inp.manifest.get("parts") or {}).items()) + [
                f"covers_page={N.RULE_COVERS_PAGE}", f"mentions={N.RULE_MENTIONS}",
                f"symbol_of={inp.symbol_of_info.get('rule_version', N.RULE_SYMBOL_OF)}"],
            "rules_json": inp.options.rules(), "counts_json": counts,
            "accounting_json": receipt["accounting"],
            "checks_json": {c.check_id: c.status for c in checks},
            "datasets": sorted(f"{k}={(inp.manifest['datasets'][k] or {}).get('rows')}" for k in inp.datasets),
            "doc_build_id": doc.get("build_id"), "doc_snapshot_id": doc.get("snapshot_id"),
            "receipt_ref": receipt.get("receipt_ref"), "error_code": bad[0].code if bad else ""})
        if bad:
            raise ProjectionError(bad[0].code or "E_CHECK_FAILED", receipt["error"]["message"], stage="verify",
                                  details={"receipt_ref": receipt.get("receipt_ref")})
        return receipt
    except ProjectionError as exc:
        if receipt.get("status") != FAILED:
            finish(FAILED, exc.as_dict())
            if started_meta:
                try:
                    meta_set(driver, database, ns, {"status": FAILED, "error_code": exc.code,
                                                    "finished_at": utc_now().isoformat()})
                except Exception:  # noqa: BLE001 — the server may be the reason of the failure
                    pass
        raise
    except Exception as exc:
        finish(FAILED, {"code": "E_INTERNAL", "stage": "load", "message": f"{type(exc).__name__}: {exc}"})
        if started_meta:
            try:
                meta_set(driver, database, ns, {"status": FAILED, "error_code": "E_INTERNAL",
                                                "finished_at": utc_now().isoformat()})
            except Exception:  # noqa: BLE001
                pass
        raise


def verify(settings: Settings | None, options: NavLoadOptions, *, driver: Any = None) -> dict[str, Any]:
    """``nav graph-verify``: checks N1–N7 of the loaded NAV graph against the NAV datasets, without writing."""
    ns = options.namespace
    database = options.database or (settings.neo4j_database if settings else "neo4j")
    with NavInput.open(options.nav_dir, options.projection) as inp:
        planned = plan(inp, options)
        planned.pop("_preflight_results")
        own = driver is None
        if own:
            from vkm_corpus.graph import client

            driver = client.connect(settings)
        try:
            doc = document_state(driver, database, ns)
            meta, _age = meta_get(driver, database, ns)
            open_acct = [ds for ds, a in planned["accounting"].items() if isinstance(a, dict) and a.get("closed") is False]
            checks, counts = run_checks(driver, database, ns, expected=planned["expected_counts"],
                                        snapshot_id=inp.snapshot_id, doc_state=doc, accounting_closed=open_acct,
                                        run_id=(meta or {}).get("run_id"))
        finally:
            if own:
                driver.close()
    return {"status": "FAIL" if failures(checks) else "PASS", "snapshot_id": inp.snapshot_id,
            "nav_meta": {k: (meta or {}).get(k) for k in ("status", "snapshot_id", "run_id", "finished_at")},
            "checks": [c.as_dict() for c in checks], "checks_summary": summarize(checks), "graph_counts": counts}

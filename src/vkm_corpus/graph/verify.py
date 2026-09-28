"""Checks of a built DOCUMENT graph against its projection input (C1–C16) and the digest pair.

``expected_digest`` is computed from the projection input (the rows the projector writes, without
``projection_run_id``); ``content_digest`` from an export of the graph with the same serialisation. Their equality
proves the graph is exactly the projection of the snapshot — no loss, no extra, no manual edit (R7). Digests use the
registry names (not the test namespace), so a test build and a production build of one snapshot agree.

H-25 corrections: registry Author/Venue/Work carry ``review_status = NOT_APPLICABLE`` (C8/C9 accept it and require
it); the source accounting C16 uses agent D's roll-up classes (``SourceProcessingStatus``), not «processed + skipped
+ failed».
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

from vkm_corpus.contracts import vocab
from vkm_corpus.graph import client
from vkm_corpus.graph import cypher as C
from vkm_corpus.graph import schema as S
from vkm_corpus.graph.canon import ProjectionInput
from vkm_corpus.graph.common import (PASS, SKIP, WARN, CheckResult, StreamDigest, canonical_json, check,
                                     combine_digests)
from vkm_corpus.graph.rows import iter_nodes, iter_nodes_by_ids, iter_rels, node_sql
from vkm_corpus.graph.schema import Namespace, q

DIGEST_EXCLUDE = ("projection_run_id",)
FORBIDDEN = sorted(vocab.FORBIDDEN_STATUS_VALUES)
ROLLUP_CLASSES = sorted(v.value for v in vocab.SourceProcessingStatus)


# ---------------------------------------------------------------- digests
@dataclass(frozen=True)
class Digest:
    digest: str
    parts: tuple[tuple[str, int, str], ...]

    def as_dict(self) -> dict[str, Any]:
        return {"digest": self.digest, "parts": [{"name": n, "count": c, "sha256": d} for n, c, d in self.parts]}


def node_line(label: str, node_id: str, props: dict[str, Any]) -> str:
    return f"{label}\t{node_id}\t{canonical_json({k: v for k, v in props.items() if k not in DIGEST_EXCLUDE})}"


def rel_line(rel_type: str, from_id: str, to_id: str, props: dict[str, Any]) -> str:
    body = canonical_json({k: v for k, v in props.items() if k not in DIGEST_EXCLUDE})
    return f"{rel_type}\t{from_id}\t{to_id}\t{body}"


def _rel_key(from_id: str, to_id: str, props: dict[str, Any]) -> tuple[str, str, str]:
    return (from_id, to_id, str(props.get("canonical_row_id") or ""))


def expected_digest(inp: ProjectionInput) -> Digest:
    parts = []
    for node in S.NODE_TYPES:
        d = StreamDigest(node.label)
        for row in iter_nodes(inp, node, None):
            d.add((row.id,), node_line(node.label, row.id, row.props))
        parts.append((node.label, d.count, d.hexdigest()))
    for rel in S.REL_TYPES:
        d = StreamDigest(rel.type)
        for row in iter_rels(inp, rel, None):
            d.add(_rel_key(row.from_id, row.to_id, row.props), rel_line(rel.type, row.from_id, row.to_id, row.props))
        parts.append((rel.type, d.count, d.hexdigest()))
    return Digest(combine_digests(parts), tuple(parts))


def _stream(driver: Any, database: str, query: str, fetch_size: int = 10_000) -> Iterable[dict[str, Any]]:
    with driver.session(database=database, default_access_mode="READ", fetch_size=fetch_size) as session:
        for record in session.run(query):
            yield {"id": record.get("id"), "from_id": record.get("from_id"), "to_id": record.get("to_id"),
                   "props": dict(record["props"])}


def content_digest(driver: Any, database: str, ns: Namespace) -> Digest:
    parts = []
    for node in S.NODE_TYPES:
        d = StreamDigest(node.label)
        for rec in _stream(driver, database, C.export_nodes(ns, node)):
            d.add((rec["id"],), node_line(node.label, rec["id"], rec["props"]))
        parts.append((node.label, d.count, d.hexdigest()))
    for rel in S.REL_TYPES:
        d = StreamDigest(rel.type)
        for rec in _stream(driver, database, C.export_rels(ns, rel)):
            d.add(_rel_key(rec["from_id"], rec["to_id"], rec["props"]),
                  rel_line(rel.type, rec["from_id"], rec["to_id"], rec["props"]))
        parts.append((rel.type, d.count, d.hexdigest()))
    return Digest(combine_digests(parts), tuple(parts))


# ---------------------------------------------------------------- checks
def _count(driver: Any, database: str, query: str, **params: Any) -> int:
    rows = client.read(driver, database, query, **params)
    return int(next(iter(rows[0].values()))) if rows else 0


def _ids(driver: Any, database: str, query: str, **params: Any) -> list[Any]:
    return [next(iter(r.values())) for r in client.read(driver, database, query, **params)]


def run_checks(driver: Any, database: str, ns: Namespace, inp: ProjectionInput, *,
               expected_counts: dict[str, dict[str, int]], expected: Digest | None = None,
               sample_per_label: int = 200, sample_seed: str = "vkm", previous_digest: str | None = None,
               progress: Callable[[str], None] | None = None) -> tuple[list[CheckResult], Digest | None, Digest | None]:
    """All checks; returns (results, expected digest, content digest)."""
    say = progress or (lambda _msg: None)
    L = ns.label
    R = ns.rel
    results: list[CheckResult] = []

    # C1 / C2 counts
    say("C1-C2 counts")
    node_diff = []
    for node in S.NODE_TYPES:
        got = _count(driver, database, C.count_label(ns, node.label))
        want = expected_counts["nodes"][node.label]
        if got != want:
            node_diff.append(f"{node.label}: graph {got} != canon {want}")
    results.append(check("C1", "node counts per label equal the canon", node_diff, code="E_COUNT_MISMATCH"))
    rel_diff = []
    for rel in S.REL_TYPES:
        got = _count(driver, database, C.count_rel(ns, rel.type))
        want = expected_counts["rels"][rel.type]
        if got != want:
            rel_diff.append(f"{rel.type}: graph {got} != expected {want}")
    results.append(check("C2", "relationship counts per type equal the expected rows", rel_diff,
                         code="E_COUNT_MISMATCH"))

    # C3 orphans and parents
    say("C3 orphans")
    orphan = []
    n = _count(driver, database, f"MATCH (p:{q(L('Page'))}) WHERE COUNT {{ (:{q(L('Source'))})-[:{q(R('HAS_PAGE'))}]->(p) }} "
                                 "<> 1 RETURN count(p)")
    if n:
        orphan.append(f"Page without exactly one HAS_PAGE: {n}")
    for label, rel_type in S.HAS_OBJECT_REL.items():
        n = _count(driver, database, f"MATCH (o:{q(L(label))}) WHERE o.page_id IS NOT NULL AND "
                                     f"COUNT {{ (:{q(L('Page'))})-[:{q(R(rel_type))}]->(o) }} <> 1 RETURN count(o)")
        if n:
            orphan.append(f"{label} without exactly one {rel_type}: {n}")
    results.append(check("C3", "every page and page object has exactly one parent", orphan, code="E_ORPHAN_NODES"))
    lonely = []
    for label, rel_type in (("Author", "AUTHORED_BY"), ("Venue", "PUBLISHED_IN")):
        ids = _ids(driver, database, f"MATCH (x:{q(L(label))}) WHERE NOT ()-[:{q(R(rel_type))}]->(x) "
                                     "RETURN x.id ORDER BY x.id LIMIT 20")
        lonely += [f"{label} {i}" for i in ids]
    no_work = _ids(driver, database, f"MATCH (s:{q(L('Source'))}) WHERE s.lifecycle_status = 'ACTIVE' AND "
                                     f"NOT (s)-[:{q(R('INSTANCE_OF'))}]->() RETURN s.id ORDER BY s.id LIMIT 50")
    results.append(check("C3w", "authors/venues without works; ACTIVE sources without INSTANCE_OF (WORK_UNKNOWN)",
                         lonely + [f"WORK_UNKNOWN {i}" for i in no_work], code="W_UNLINKED", warn_only=True))

    # C4 pages per source: graph = canon; canon partiality is reported (roll-up says why)
    say("C4 pages per source")
    graph_pages = {r["id"]: r["n"] for r in client.read(
        driver, database, f"MATCH (s:{q(L('Source'))}) OPTIONAL MATCH (s)-[:{q(R('HAS_PAGE'))}]->(p) "
                          "RETURN s.id AS id, count(p) AS n")}
    canon_pages = {r["source_id"]: r["n"] for r in inp.fetch(
        "SELECT s.source_id, count(p.page_id) AS n FROM e_sources s LEFT JOIN e_pages p ON p.source_id = s.source_id "
        "GROUP BY s.source_id")}
    lost = sorted(f"{sid}: graph {graph_pages.get(sid, 0)} != canon {n}" for sid, n in canon_pages.items()
                  if graph_pages.get(sid, 0) != n)
    results.append(check("C4", "pages per source in the graph equal the canon (no silent page loss)", lost,
                         code="E_COUNT_MISMATCH"))
    partial = [f"{r['source_id']} ({r['processing_rollup']}): {r['n']} of {r['page_count']}" for r in inp.fetch(
        "SELECT s.source_id, s.processing_rollup, s.page_count, count(p.page_id) AS n FROM e_sources s "
        "LEFT JOIN e_pages p ON p.source_id = s.source_id WHERE s.page_count IS NOT NULL "
        "GROUP BY ALL HAVING count(p.page_id) <> s.page_count ORDER BY s.source_id LIMIT 50")]
    results.append(check("C4w", "sources whose canon has fewer pages than page_count (D's roll-up explains)",
                         partial, code="W_PARTIAL_SOURCE", warn_only=True))

    # C5 PRECEDES chain
    say("C5 page order")
    bad = _count(driver, database, f"MATCH (a:{q(L('Page'))})-[:{q(R('PRECEDES'))}]->(b:{q(L('Page'))}) "
                                   "WHERE a.source_id <> b.source_id OR b.page_index <> a.page_index + 1 RETURN count(*)")
    bad += _count(driver, database, f"MATCH (p:{q(L('Page'))}) WHERE COUNT {{ (p)-[:{q(R('PRECEDES'))}]->() }} > 1 "
                                    f"OR COUNT {{ ()-[:{q(R('PRECEDES'))}]->(p) }} > 1 RETURN count(p)")
    results.append(check("C5", "PRECEDES links consecutive pages of one source, at most one in/out", bad,
                         code="E_INVARIANT_VIOLATION"))

    # C6 label hygiene
    say("C6-C7 labels and types")
    types = ns.type_labels()
    layer = q(ns.layer_label)
    viol = _count(driver, database, f"MATCH (n:{layer}) WHERE size([l IN labels(n) WHERE l IN $types]) <> 1 "
                                    "RETURN count(n)", types=types)
    viol += _count(driver, database, f"MATCH (n) WHERE any(l IN labels(n) WHERE l IN $types) AND NOT n:{layer} "
                                     "RETURN count(n)", types=types)
    foreign: list[str] = []
    if not ns.is_test:
        rows = client.read(driver, database,
                           f"MATCH (n) WHERE NOT n:{layer} AND NOT n:{q(ns.run_label)} AND NOT any(l IN labels(n) "
                           f"WHERE l STARTS WITH '{S.TEST_LABEL_PREFIX}') RETURN labels(n) AS labels, count(*) AS n "
                           "LIMIT 20")
        foreign = [f"{r['labels']}: {r['n']}" for r in rows]
    results.append(check("C6", "each layer node has one registry type label; no unregistered nodes (R7)",
                         ([f"bad type labels: {viol}"] if viol else []) + foreign, code="E_INVARIANT_VIOLATION"))

    # C7 relationship types and endpoints
    registry_types = set(ns.rel_types())
    rows = client.read(driver, database, "MATCH ()-[r]->() RETURN type(r) AS t, count(*) AS n")
    unknown = []
    for r in rows:
        t = r["t"]
        mine = t.startswith(ns.prefix.upper() + "_") if ns.is_test else not t.startswith(S.TEST_LABEL_PREFIX.upper())
        if mine and t not in registry_types:
            unknown.append(f"{t}: {r['n']}")
    for rel in S.REL_TYPES:
        n = _count(driver, database, f"MATCH (a)-[r:{q(R(rel.type))}]->(b) WHERE NOT (a:{q(L(rel.start))} AND "
                                     f"b:{q(L(rel.end))}) RETURN count(r)")
        if n:
            unknown.append(f"{rel.type} with wrong endpoints: {n}")
    results.append(check("C7", "relationship types come from the registry and join the declared labels", unknown,
                         code="E_INVARIANT_VIOLATION"))

    # C8 required properties and whitelists (replacement of Enterprise existence constraints)
    say("C8 properties")
    missing = []
    for node in S.NODE_TYPES:
        cond = " OR ".join(f"n.{p} IS NULL" for p in node.all_required)
        n = _count(driver, database, f"MATCH (n:{q(L(node.label))}) WHERE {cond} RETURN count(n)")
        if n:
            missing.append(f"{node.label}: {n} node(s) miss one of {list(node.all_required)}")
        extra = _ids(driver, database, f"MATCH (n:{q(L(node.label))}) UNWIND keys(n) AS k WITH DISTINCT k "
                                       "WHERE NOT k IN $allowed RETURN k", allowed=list(node.all_properties))
        if extra:
            missing.append(f"{node.label}: properties outside the whitelist {sorted(extra)}")
    for rel in S.REL_TYPES:
        cond = " OR ".join(f"r.{p} IS NULL" for p in rel.required)
        n = _count(driver, database, f"MATCH ()-[r:{q(R(rel.type))}]->() WHERE {cond} RETURN count(r)")
        if n:
            missing.append(f"{rel.type}: {n} edge(s) miss one of {list(rel.required)}")
        extra = _ids(driver, database, f"MATCH ()-[r:{q(R(rel.type))}]->() UNWIND keys(r) AS k WITH DISTINCT k "
                                       "WHERE NOT k IN $allowed RETURN k", allowed=list(rel.all_properties))
        if extra:
            missing.append(f"{rel.type}: properties outside the whitelist {sorted(extra)}")
    results.append(check("C8", "required properties present, no property outside the whitelist", missing,
                         code="E_INVARIANT_VIOLATION"))

    # C9 scientific safety
    say("C9 statuses")
    unsafe = []
    status_props = ("review_status", "page_status", "processing_rollup", "lifecycle_status", "curation_status",
                    "match_status")
    cond = " OR ".join(f"n.{p} IN $forbidden" for p in status_props)
    n = _count(driver, database, f"MATCH (n:{layer}) WHERE {cond} RETURN count(n)", forbidden=FORBIDDEN)
    if n:
        unsafe.append(f"forbidden status on {n} node(s)")
    for rel in S.REL_TYPES:
        if "match_status" in rel.all_properties or "curation_status" in rel.all_properties:
            n = _count(driver, database, f"MATCH ()-[r:{q(R(rel.type))}]->() WHERE r.match_status IN $forbidden OR "
                                         "r.curation_status IN $forbidden RETURN count(r)", forbidden=FORBIDDEN)
            if n:
                unsafe.append(f"forbidden status on {n} {rel.type} edge(s)")
    for node in S.NODE_TYPES:
        allowed = sorted(node.review_statuses or {v.value for v in vocab.ReviewStatus})
        n = _count(driver, database, f"MATCH (n:{q(L(node.label))}) WHERE NOT n.review_status IN $allowed "
                                     "RETURN count(n)", allowed=allowed)
        if n:
            unsafe.append(f"{node.label}: {n} node(s) with review_status outside {allowed}")
    n = _count(driver, database, f"MATCH (e:{q(L('BibliographyEntry'))}) WHERE COUNT {{ (e)-[:{q(R('RESOLVES_TO'))}]->() }} > 1 "
                                 "RETURN count(e)")
    if n:
        unsafe.append(f"{n} bibliography entr(y/ies) resolve to more than one work")
    n = _count(driver, database, f"MATCH (f:{q(L('Figure'))}) WHERE f.figure_type <> 'UNKNOWN_FIGURE_TYPE' AND "
                                 "coalesce(f.figure_type_method, 'NONE') = 'NONE' RETURN count(f)")
    if n:
        unsafe.append(f"{n} figure(s) typed without a method")
    results.append(check("C9", "no promoted statuses; objects AUTO_EXTRACTED_UNREVIEWED; registry entities "
                               "NOT_APPLICABLE", unsafe, code="E_FORBIDDEN_STATUS"))

    # C10 traceability sample: graph node == canonical row, property by property
    say("C10 trace sample")
    trace_bad = []
    for node in S.NODE_TYPES:
        ids = [r["id"] for r in inp.fetch(f"SELECT {node.key} AS id FROM ({node_sql(node)}) "
                                          f"ORDER BY md5({node.key} || ?) LIMIT {int(sample_per_label)}",
                                          [sample_seed])]
        if not ids:
            continue
        want = {row.id: node_line(node.label, row.id, row.props) for row in iter_nodes_by_ids(inp, node, ids)}
        got = {r["id"]: node_line(node.label, r["id"], dict(r["props"])) for r in client.read(
            driver, database, C.nodes_by_ids(ns, node), ids=ids)}
        for i in ids:
            if want.get(i) != got.get(i):
                trace_bad.append(f"{node.label} {i}")
    results.append(check("C10", "sampled nodes equal their canonical rows (traceability)", trace_bad,
                         code="E_TRACE_MISMATCH"))

    # C11 digest
    say("C11 digests")
    expected = expected or expected_digest(inp)
    content = content_digest(driver, database, ns)
    mismatched = [f"{e[0]}: canon {e[1]} rows {e[2][:12]} vs graph {g[1]} rows {g[2][:12]}"
                  for e, g in zip(expected.parts, content.parts) if e != g]
    results.append(check("C11", "content_digest (graph export) equals expected_digest (canon)", mismatched,
                         code="E_DIGEST_MISMATCH",
                         details={"expected_digest": expected.digest, "content_digest": content.digest}))

    # C12 repeatability: same snapshot → same digest as the previous COMPLETE run
    if previous_digest is None:
        results.append(CheckResult("C12", "same snapshot rebuilt → same content_digest", SKIP,
                                   details={"reason": "no previous COMPLETE run of this snapshot"}))
    else:
        results.append(check("C12", "same snapshot rebuilt → same content_digest",
                             [] if previous_digest == content.digest else [f"previous {previous_digest[:12]}"],
                             code="E_DIGEST_MISMATCH"))

    # C13 NOT_SAME never collapsed into one work
    say("C13-C16 relations and accounting")
    n = _count(driver, database, f"MATCH (a:{q(L('Work'))})-[:{q(R('NOT_SAME'))}]->(a) RETURN count(*)")
    results.append(check("C13", "no NOT_SAME loop (two 'not same' works merged)", n, code="E_RELATION_CONFLICT"))

    # C14 bibliography attribution and INSTANCE_OF discipline (H-16)
    attrib = []
    want_ref = int(inp.scalar("SELECT count(*) FROM e_bibliography WHERE citing_work_id IS NOT NULL"))
    got_ref = _count(driver, database, C.count_rel(ns, "REFERENCE_OF"))
    if want_ref != got_ref:
        attrib.append(f"REFERENCE_OF {got_ref} != entries with a citing work {want_ref}")
    n = _count(driver, database, f"MATCH (e:{q(L('BibliographyEntry'))}) WHERE COUNT {{ (e)-[:{q(R('REFERENCE_OF'))}]->() }} > 1 "
                                 "RETURN count(e)")
    if n:
        attrib.append(f"{n} entr(y/ies) with several REFERENCE_OF")
    n = _count(driver, database, f"MATCH (a:{q(L('Work'))})-[c:{q(R('CITES'))}]->(b:{q(L('Work'))}) WHERE NOT EXISTS {{ "
                                 f"MATCH (e:{q(L('BibliographyEntry'))})-[:{q(R('REFERENCE_OF'))}]->(a) "
                                 f"WHERE (e)-[:{q(R('RESOLVES_TO'))}]->(b) }} RETURN count(c)")
    if n:
        attrib.append(f"{n} CITES edge(s) without a REFERENCE_OF + RESOLVES_TO entry")
    n = _count(driver, database, f"MATCH ()-[r:{q(R('INSTANCE_OF'))}]->() WHERE r.link_type = 'FOREIGN_CONTENT' "
                                 "RETURN count(r)")
    if n:
        attrib.append(f"{n} INSTANCE_OF from FOREIGN_CONTENT")
    n = _count(driver, database, f"MATCH (s:{q(L('Source'))}) WHERE COUNT {{ (s)-[:{q(R('INSTANCE_OF'))}]->() }} > 1 "
                                 "RETURN count(s)")
    if n:
        attrib.append(f"{n} source(s) with several INSTANCE_OF")
    results.append(check("C14", "bibliography attribution (REFERENCE_OF only from citing_work_id, CITES only from "
                                "REFERENCE_OF+RESOLVES_TO) and INSTANCE_OF only from the primary non-foreign link",
                         attrib, code="E_INVARIANT_VIOLATION"))

    # C15 symmetric relations and duplicates
    sym = []
    for rel in S.REL_TYPES:
        if rel.symmetric:
            n = _count(driver, database, f"MATCH (a)-[r:{q(R(rel.type))}]->(b) WHERE a.id >= b.id OR "
                                         f"EXISTS {{ (b)-[:{q(R(rel.type))}]->(a) }} RETURN count(r)")
            if n:
                sym.append(f"{rel.type}: {n} edge(s) not stored once from the smaller id")
    n = _count(driver, database, f"MATCH (a:{q(L('Page'))})-[r:{q(R('DUPLICATE_CANDIDATE_OF'))}]->(b:{q(L('Page'))}) "
                                 "WHERE a.source_id = b.source_id OR a.dup_group_id <> b.dup_group_id "
                                 "OR r.dup_group_id <> a.dup_group_id RETURN count(r)")
    if n:
        sym.append(f"DUPLICATE_CANDIDATE_OF: {n} edge(s) inside one source or across groups")
    results.append(check("C15", "symmetric relations stored once; duplicate pages across sources, one group", sym,
                         code="E_INVARIANT_VIOLATION"))

    # C16 source accounting by D's roll-up classes (H-25)
    rows = client.read(driver, database, f"MATCH (s:{q(L('Source'))}) RETURN s.processing_rollup AS c, count(*) AS n")
    by_class = {r["c"]: r["n"] for r in rows}
    total = sum(by_class.values())
    acc = []
    if set(by_class) - set(ROLLUP_CLASSES):
        acc.append(f"unknown roll-up classes {sorted(str(c) for c in set(by_class) - set(ROLLUP_CLASSES))}")
    canon_total = int(inp.scalar("SELECT count(*) FROM e_sources"))
    if total != canon_total:
        acc.append(f"graph sources {total} != canon {canon_total}")
    manifest_total = inp.info.manifest_counts.get("sources_total")
    if manifest_total is not None and int(manifest_total) != total:
        acc.append(f"graph sources {total} != manifest sources_total {manifest_total}")
    n = _count(driver, database, f"MATCH (s:{q(L('Source'))}) WHERE s.processing_rollup = 'SKIPPED_BY_REGISTER' AND "
                                 f"(s.lifecycle_status = 'ACTIVE' OR COUNT {{ (s)-[:{q(R('HAS_PAGE'))}]->() }} > 0) "
                                 "RETURN count(s)")
    if n:
        acc.append(f"{n} skipped source(s) active or with pages")
    results.append(check("C16", "sources = sum of D's roll-up classes; skipped sources have no pages", acc,
                         code="E_COUNT_MISMATCH", details={"by_rollup": {str(k): v for k, v in sorted(
                             by_class.items(), key=lambda kv: str(kv[0]))}, "total": total}))
    return results, expected, content


def summarize(results: list[CheckResult]) -> dict[str, int]:
    out = {PASS: 0, WARN: 0, SKIP: 0, "FAIL": 0}
    for r in results:
        out[r.status] = out.get(r.status, 0) + 1
    return out

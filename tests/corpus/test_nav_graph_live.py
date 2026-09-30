"""NAV graph against a live Neo4j (marker ``services``; skipped as NOT_RUN without the service).

Environment as in ``test_graph_live.py``: ``VKM_TEST_NEO4J_URI`` and ``VKM_TEST_NEO4J_AUTH_CMD`` (a command printing
``<user>/<password>`` or the password; read into memory only). Everything runs in a namespace ``VkmTest<8 hex>``:
minimal DOCUMENT nodes for the synthetic NAV datasets, a COMPLETE DOCUMENT ProjectionRun, the NAV DDL, the load with
checks N1–N11 (tables, parameter values, the term dictionary and object duplicates included), the path and
neighbourhood Cypher, a second (idempotent) load; the namespace is purged afterwards, so production nodes are never
seen or touched. This validates the NAV Cypher on the server before a production load.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess

import pytest

pytestmark = pytest.mark.services

neo4j = pytest.importorskip("neo4j")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")

from vkm_corpus.graph import client  # noqa: E402
from vkm_corpus.graph import nav as L  # noqa: E402
from vkm_corpus.graph import nav_query as Q  # noqa: E402
from vkm_corpus.graph import nav_schema as N  # noqa: E402
from vkm_corpus.graph import schema as S  # noqa: E402
from vkm_corpus.graph.common import ProjectionError  # noqa: E402
from vkm_corpus.graph.loader import purge_test_namespace  # noqa: E402
from vkm_corpus.graph.nav_rows import ProjectionOptions  # noqa: E402
from vkm_corpus.graph.schema import q  # noqa: E402
from vkm_corpus.testing import nav_graph as SY  # noqa: E402

DB = os.environ.get("VKM_TEST_NEO4J_DATABASE", "neo4j")


def _auth() -> tuple[str, str]:
    user = os.environ.get("VKM_TEST_NEO4J_USER", "neo4j")
    cmd = os.environ.get("VKM_TEST_NEO4J_AUTH_CMD", "").strip()
    if not cmd:
        pytest.skip("NOT_RUN: VKM_TEST_NEO4J_AUTH_CMD is not set")
    try:
        out = subprocess.run(shlex.split(cmd), capture_output=True, text=True, timeout=60, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pytest.skip("NOT_RUN: the Neo4j credential command failed")
    return client.split_auth(user, out)


@pytest.fixture(scope="module")
def driver():
    uri = os.environ.get("VKM_TEST_NEO4J_URI", "").strip()
    if not uri:
        pytest.skip("NOT_RUN: VKM_TEST_NEO4J_URI is not set")
    try:
        drv = client.connect(uri=uri, auth=_auth())
    except ProjectionError as exc:
        pytest.skip(f"NOT_RUN: {exc.code}")
    yield drv
    drv.close()


@pytest.fixture()
def ns(driver):
    namespace = S.Namespace.for_test()
    yield namespace
    L.purge_test_namespace(driver, DB, namespace)
    left = purge_test_namespace(driver, DB, namespace)["left"]          # DOCUMENT nodes, runs and DDL of the test
    assert left == 0


def _document(driver, ns, nav_dir) -> None:
    """The DOCUMENT nodes the synthetic NAV rows reference, their page edges and a COMPLETE DOCUMENT run."""
    fake = SY.FakeNavNeo4j()
    SY.add_document_graph(fake, nav_dir)
    by_label: dict[str, list[dict]] = {}
    for node_id, node in fake.nodes.items():
        label = next(lb for lb in node["labels"] if lb != S.LAYER_LABELS[S.LAYER])
        by_label.setdefault(label, []).append({"id": node_id, "props": node["props"]})
    for label, rows in by_label.items():
        client.write(driver, DB, f"UNWIND $rows AS row MERGE (n:{q(ns.label(label))} {{id: row.id}}) "
                                 f"SET n = row.props, n:{q(ns.layer_label)}", rows=rows)
    ends = {"HAS_PAGE": ("Source", "Page"), "HAS_FORMULA": ("Page", "Formula"), "HAS_BLOCK": ("Page", "Block"),
            "HAS_TABLE": ("Page", "Table"), "HAS_FIGURE": ("Page", "Figure")}
    for rel_type, rels in fake.rels.items():
        start, end = ends[rel_type]
        client.write(driver, DB, f"UNWIND $rows AS row MATCH (a:{q(ns.label(start))} {{id: row.a}}) "
                                 f"MATCH (b:{q(ns.label(end))} {{id: row.b}}) MERGE (a)-[:{q(ns.rel(rel_type))}]->(b)",
                     rows=[{"a": a, "b": b} for (a, b, _k) in rels])
    counts = L.document_counts(driver, DB, ns)
    client.write(driver, DB, f"CREATE (r:{q(ns.run_label)}) SET r.id = $id, r.layer = 'DOCUMENT', "
                             "r.status = 'COMPLETE', r.built_from_snapshot_id = $snap, r.counts_json = $counts, "
                             "r.started_at = datetime(), r.finished_at = datetime()",
                 id=f"VKM-PRJ-DOC-{ns.prefix}", snap=SY.SNAPSHOT, counts=json.dumps(counts))


def test_nav_load_checks_and_queries_on_a_live_server(driver, ns, tmp_path):
    nav_dir = tmp_path / "nav"
    ids = SY.write_synthetic_nav(nav_dir)
    _document(driver, ns, nav_dir)
    options = L.NavLoadOptions(nav_dir=nav_dir, namespace=ns, database=DB,
                               projection=ProjectionOptions(symbol_morphology="surface"))
    first = L.load(None, options, driver=driver)
    assert first["status"] == "COMPLETE", first.get("checks")
    assert {c["check_id"]: c["status"] for c in first["checks"]} == {f"N{i}": "PASS" for i in range(1, 12)}
    names = {r["name"] for r in client.read(driver, DB, "SHOW CONSTRAINTS YIELD name RETURN name")}
    assert {i.name for i in N.ddl_items(ns) if i.kind == "CONSTRAINT"} <= names

    t = ids["terms"]
    rel_types = Q.path_rel_types(None)
    params = {"a": t["ползучесть соль"], "b": t["оседание"], "rel_types": rel_types, "labels": list(N.PATH_LABELS)}
    assert L.read(driver, DB, Q.cy_shortest_length(ns, rel_types, 4), **params) == [{"n": 3}]
    raw = L.read(driver, DB, Q.cy_paths(ns, rel_types, 3), cap=50, **params)
    best = Q.shape_paths(raw, 3, ns)[0]
    assert [n["name"] for n in best["nodes"]] == ["ползучесть соли", "скорость ползучести", "конвергенция", "оседание"]
    ordered = L.read(driver, DB, Q.cy_paths(ns, ["CO_OCCURS"], 1, ordered=True), cap=5,
                     **{**params, "b": t["скорость ползучесть"]})
    assert len(ordered) == 1
    rows = L.read(driver, DB, Q.cy_neighbourhood(ns, "DOCUMENT"), id=SY.F2, per_type=10)
    shaped = Q.shape_neighbourhood(rows[0]["node"], rows[0]["groups"], {}, 50, ns)
    assert shaped["node"]["name"] == "(1.2)"
    assert {("IN_SECTION", "out"), ("DEFINED_FOR", "in"), ("NAV_REFERS_TO", "in")} <= {
        (e["rel"], e["direction"]) for e in shaped["edges"]}
    mids = Q.depth2_ids(shaped)
    second = L.read(driver, DB, Q.cy_neighbourhood_2(ns, "NAVIGATION"),
                    ids=[m for m, layer in mids if layer == "NAVIGATION"], root=SY.F2, per_type=3)
    assert second and all("groups" in r for r in second)
    # the new parts: a dictionary hop, the neighbourhood of a structured table
    only, fam = ids["dictionary_only"], Q.path_rel_types(["dictionary"])
    raw = L.read(driver, DB, Q.cy_paths(ns, fam, 1, ordered=True), cap=5, a=only["взт"],
                 b=only["водозащитный толща"], rel_types=fam, labels=list(N.PATH_LABELS))
    assert [h["rel"] for h in Q.shape_paths(raw, 1, ns)[0]["hops"]] == ["ABBREVIATION_OF"]
    rows = L.read(driver, DB, Q.cy_neighbourhood(ns, "NAVIGATION"), id=ids["tables"][SY.T1], per_type=10)
    shaped = Q.shape_neighbourhood(rows[0]["node"], rows[0]["groups"], {}, 50, ns)
    assert shaped["node"]["kind"] == "STRUCTURED_TABLE"
    assert {("GRID_OF", "out"), ("TABULATES", "out"), ("IN_TABLE", "in")} <= {
        (e["rel"], e["direction"]) for e in shaped["edges"]}

    again = L.load(None, options, driver=driver)
    assert again["status"] == "COMPLETE" and again["graph_counts"] == first["graph_counts"]
    assert again["sweep"]["rels_deleted"] == 0

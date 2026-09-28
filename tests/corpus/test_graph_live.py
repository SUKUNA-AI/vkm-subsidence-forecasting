"""Live Neo4j tests (marker ``services``; without the service they are skipped as NOT_RUN).

Environment (never committed): ``VKM_TEST_NEO4J_URI`` (e.g. a local SSH-tunnel port) and ``VKM_TEST_NEO4J_AUTH_CMD`` —
a command whose standard output is ``<user>/<password>`` or the password (read into process memory only, never
printed or written). Every test works in its own namespace ``VkmTest<8 hex>`` (labels, relationship types and DDL
names are prefixed), so production nodes are never seen or touched, and the namespace is purged afterwards.
"""
from __future__ import annotations

import os
import shlex
import subprocess

import pytest

pytestmark = pytest.mark.services

neo4j = pytest.importorskip("neo4j")
pytest.importorskip("duckdb")

from vkm_corpus.config import load_settings  # noqa: E402
from vkm_corpus.graph import client, runs  # noqa: E402
from vkm_corpus.graph import schema as S  # noqa: E402
from vkm_corpus.graph.common import ProjectionError  # noqa: E402
from vkm_corpus.graph.loader import (RebuildOptions, apply_ddl, purge_test_namespace, rebuild,  # noqa: E402
                                     verify_current)
from vkm_corpus.graph.synthetic import W1, W3, synthetic_input, write_synthetic_canonical_root  # noqa: E402

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
    left = purge_test_namespace(driver, DB, namespace)["left"]
    assert left == 0


def _settings(**env):
    return load_settings({"VKM_NEO4J_DATABASE": DB, **env})


def test_ddl_is_idempotent(driver, ns):
    first = apply_ddl(driver, DB, ns)
    names = {r["name"] for r in client.read(driver, DB, "SHOW CONSTRAINTS YIELD name RETURN name")}
    second = apply_ddl(driver, DB, ns)
    again = {r["name"] for r in client.read(driver, DB, "SHOW CONSTRAINTS YIELD name RETURN name")}
    assert first == second and names == again
    assert {i.name for i in S.ddl_items(ns) if i.kind == "CONSTRAINT"} <= names


def test_rebuild_from_a_canonical_snapshot_and_repeat(driver, ns, tmp_path, monkeypatch):
    monkeypatch.delenv("VKM_DATA_ROLE", raising=False)
    root = write_synthetic_canonical_root(tmp_path / "data")
    settings = _settings(VKM_DATA_ROOT=str(root), VKM_DATA_ROLE="canonical")
    first = rebuild(settings, RebuildOptions(namespace=ns), driver=driver)
    assert first["status"] == "COMPLETE"
    checks = {c["check_id"]: c["status"] for c in first["checks"]}
    assert "FAIL" not in checks.values() and checks["C11"] == "PASS" and checks["C12"] == "SKIP"
    assert first["content_digest"]["digest"] == first["expected_digest"]["digest"]
    state = runs.graph_state(driver, DB, ns)
    assert state["state"] == "READY" and state["http_status"] == 200 and state["build_id"] == first["run_id"]
    L, R = ns.label, ns.rel
    k14 = client.read(driver, DB, f"MATCH (s:`{L('Source')}`)-[r:`{R('INSTANCE_OF')}`]->(w) "
                                  "RETURN s.id AS s, w.id AS w, s.lifecycle_status AS lc, r.link_type AS lt")
    pairs = {(r["s"], r["w"]): r for r in k14}
    assert {("VKM-SRC-013", "VKM-WRK-013"), ("VKM-SRC-025", "VKM-WRK-013")} <= set(pairs)    # K-14 as of CP-25
    assert pairs[("VKM-SRC-013", "VKM-WRK-013")]["lc"] == "ABSENT_BY_REGISTER"
    assert all(r["lt"] != "FOREIGN_CONTENT" for r in k14)
    copies = client.read(driver, DB, f"MATCH (w:`{L('Work')}` {{id: 'VKM-WRK-013'}}) RETURN w.work_copy_count AS n")
    assert copies == [{"n": 1}]                                                              # ACTIVE copies only
    second = rebuild(settings, RebuildOptions(namespace=ns), driver=driver)
    assert {c["check_id"]: c["status"] for c in second["checks"]}["C12"] == "PASS"
    assert second["content_digest"]["digest"] == first["content_digest"]["digest"]
    assert (tmp_path / "data" / second["receipt_ref"]).is_file()
    verified = verify_current(settings, driver=driver, namespace=ns, database=DB)      # `graph verify`, no writes
    assert not [c for c in verified if c.status == "FAIL"]
    client.write(driver, DB, f"MATCH (p:`{L('Page')}` {{id: 'VKM-SRC-001:p0001'}}) SET p.page_status = 'EDITED'")
    tampered = {c.check_id: c for c in verify_current(settings, driver=driver, namespace=ns, database=DB)}
    assert tampered["C11"].status == "FAIL" and tampered["C10"].status == "FAIL"   # manual edits are detected (R7)


def test_rich_synthetic_graph(driver, ns, tmp_path):
    receipt = rebuild(_settings(), RebuildOptions(namespace=ns), driver=driver, inp=synthetic_input(),
                      data_root=tmp_path)
    assert receipt["status"] == "COMPLETE"
    L, R = ns.label, ns.rel
    cites = client.read(driver, DB, f"MATCH (a:`{L('Work')}`)-[c:`{R('CITES')}`]->(b) RETURN a.id AS a, b.id AS b, "
                                    "c.n_citing_entries AS n, c.flags AS flags ORDER BY a")
    assert [(r["a"], r["b"], r["n"]) for r in cites][0] == (W1, W3, 2)
    foreign = client.read(driver, DB, f"MATCH (p:`{L('Page')}`)-[:`{R('CARRIES_FOREIGN_CONTENT_OF')}`]->(w) "
                                      "RETURN p.id AS p, w.id AS w")
    assert foreign == [{"p": "VKM-SRC-103:p0002", "w": W1}]
    reg = client.read(driver, DB, f"MATCH (n) WHERE n:`{L('Author')}` OR n:`{L('Venue')}` OR n:`{L('Work')}` "
                                  "RETURN DISTINCT n.review_status AS s")
    assert reg == [{"s": "NOT_APPLICABLE"}]


def test_busy_and_stale_runs(driver, ns, tmp_path):
    apply_ddl(driver, DB, ns)
    client.write(driver, DB, f"CREATE (:`{ns.run_label}` {{id: 'VKM-PRJ-DOC-FAKE', layer: 'DOCUMENT', "
                             "status: 'LOADING', started_at: datetime(), heartbeat_at: datetime()})")
    with pytest.raises(ProjectionError) as info:
        rebuild(_settings(), RebuildOptions(namespace=ns), driver=driver, inp=synthetic_input(), data_root=tmp_path)
    assert info.value.code == "E_PROJECTION_BUSY"
    assert runs.graph_state(driver, DB, ns)["http_status"] == 503
    client.write(driver, DB, f"MATCH (r:`{ns.run_label}` {{id: 'VKM-PRJ-DOC-FAKE'}}) "
                             "SET r.heartbeat_at = datetime() - duration('PT2H')")
    receipt = rebuild(_settings(), RebuildOptions(namespace=ns), driver=driver, inp=synthetic_input(),
                      data_root=tmp_path)
    assert receipt["status"] == "COMPLETE" and receipt["stale_runs_failed"] == ["VKM-PRJ-DOC-FAKE"]


def test_cross_layer_edges_block_a_wipe(driver, ns, tmp_path):
    rebuild(_settings(), RebuildOptions(namespace=ns), driver=driver, inp=synthetic_input(), data_root=tmp_path)
    claim, supported = ns.label("EvidenceLayer"), ns.rel("SUPPORTED_BY")
    client.write(driver, DB, f"MATCH (p:`{ns.label('Page')}` {{id: 'VKM-SRC-101:p0001'}}) "
                             f"CREATE (:`{claim}` {{id: 'claim-test'}})-[:`{supported}`]->(p)")
    try:
        with pytest.raises(ProjectionError) as info:
            rebuild(_settings(), RebuildOptions(namespace=ns), driver=driver, inp=synthetic_input(),
                    data_root=tmp_path)
        assert info.value.code == "E_CROSS_LAYER_LOSS"
    finally:
        client.write(driver, DB, f"MATCH (c:`{claim}`) DETACH DELETE c")


def test_read_transactions_cannot_write(driver, ns):
    with driver.session(database=DB, default_access_mode=neo4j.READ_ACCESS) as session:
        with pytest.raises(neo4j.exceptions.ClientError):
            session.execute_read(lambda tx: tx.run(f"CREATE (:`{ns.label('Page')}` {{id: 'x'}})").consume())
    assert client.read(driver, DB, f"MATCH (n:`{ns.label('Page')}`) RETURN count(n) AS n")[0]["n"] == 0

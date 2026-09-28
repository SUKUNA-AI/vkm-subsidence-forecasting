"""NAV graph through the VKM API and MCP (``/v1/nav/graph/*``, tools ``concept_paths`` and ``graph_neighbourhood``):
the real ``Neo4jBackend`` NAV methods over the in-memory fake driver loaded by the real NAV loader from synthetic
datasets (no Neo4j, no corpus text)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
pytest.importorskip("pytz")

from fastapi.testclient import TestClient  # noqa: E402

from vkm_corpus.api.app import ApiConfig, create_app  # noqa: E402
from vkm_corpus.api.backends import Neo4jBackend  # noqa: E402
from vkm_corpus.api.envelope import Envelope  # noqa: E402
from vkm_corpus.api.fixtures import SNAPSHOT_ID, synthetic_service  # noqa: E402
from vkm_corpus.graph import nav as L  # noqa: E402
from vkm_corpus.graph import nav_schema as N  # noqa: E402
from vkm_corpus.graph.nav_rows import ProjectionOptions  # noqa: E402
from vkm_corpus.testing import nav_graph as SY  # noqa: E402

READ = "read-token-for-tests-0000000000000000"
HR = {"Authorization": f"Bearer {READ}"}


@pytest.fixture()
def env(tmp_path):
    service, _canon, _fakes = synthetic_service(tmp_path / "canon")
    nav_dir = tmp_path / "nav"
    ids = SY.write_synthetic_nav(nav_dir, snapshot_id=SNAPSHOT_ID)
    fake = SY.FakeNavNeo4j()
    SY.add_document_graph(fake, nav_dir, snapshot_id=SNAPSHOT_ID)
    receipt = L.load(None, L.NavLoadOptions(nav_dir=nav_dir, projection=ProjectionOptions(symbol_morphology="surface")),
                     driver=fake)
    assert receipt["status"] == "COMPLETE"
    backend = Neo4jBackend(SimpleNamespace(neo4j_database="neo4j", neo4j_uri="bolt://fake"))
    backend._driver = fake
    service.deps.graph = backend
    app = create_app(service, ApiConfig(read_tokens={READ: "read"}, write_tokens={}))
    return TestClient(app), service, fake, ids, app


def _ok(resp, kind):
    assert resp.status_code == 200, resp.text
    body = resp.json()
    env = body["item"]["envelope"]
    Envelope.model_validate(env)
    assert env["object_kind"] == kind and env["layer"] == "PROJECTION" and env["origin"] == "DERIVED"
    assert env["review_status"] == "AUTO_EXTRACTED_UNREVIEWED"
    assert env["projection"]["engine"] == "neo4j" and env["projection"]["index_or_graph"] == "NavigationLayer"
    assert env["projection"]["built_from_snapshot_id"] == SNAPSHOT_ID
    record = body["item"]["record"]
    assert record["nav_snapshot_id"] == SNAPSHOT_ID and "never a physical or causal claim" in record["note"]
    return body, record


def test_concept_paths(env):
    client, _service, _fake, ids, _app = env
    body, rec = _ok(client.get("/v1/nav/graph/paths", params={"term_a": "ползучесть соли", "term_b": "оседание"},
                               headers=HR), "NAV_GRAPH_PATHS")
    assert body["meta"]["warnings"] == []
    assert rec["from"]["term_id"] == ids["terms"]["ползучесть соль"] and rec["to"]["lemma"] == "оседание"
    best = rec["paths"][0]
    assert best["length"] == 3 and [n["name"] for n in best["nodes"]] == [
        "ползучесть соли", "скорость ползучести", "конвергенция", "оседание"]
    assert all(h["pages"] and h["rel"] == "CO_OCCURS" and h["n_sources"] >= 2 for h in best["hops"])
    # through formulas only, with TRM- ids as input
    _body, rec = _ok(client.get("/v1/nav/graph/paths", params={
        "term_a": ids["terms"]["напряжение"], "term_b": ids["terms"]["скорость ползучесть"], "via": ["formulas"]},
        headers=HR), "NAV_GRAPH_PATHS")
    path = rec["paths"][0]
    assert rec["via"] == ["formulas"] and [n["kind"] for n in path["nodes"]] == ["TERM", "SYMBOL", "FORMULA", "SYMBOL",
                                                                                "TERM"]
    assert path["nodes"][2]["id"] == SY.F2 and path["nodes"][2]["name"] == "(1.2)"
    # no path within one hop
    _body, rec = _ok(client.get("/v1/nav/graph/paths", params={"term_a": "ползучесть соли", "term_b": "оседание",
                                                               "max_len": 1}, headers=HR), "NAV_GRAPH_PATHS")
    assert rec["paths"] == [] and "no path within 1 hops" in rec["hint"]


def test_graph_neighbourhood(env):
    client, _service, _fake, ids, _app = env
    body, rec = _ok(client.get(f"/v1/nav/graph/neighbourhood/{SY.F2}", params={"depth": 2, "limit": 20}, headers=HR),
                    "NAV_GRAPH_NEIGHBOURHOOD")
    assert body["item"]["envelope"]["source_id"] == SY.S1 and rec["node"]["name"] == "(1.2)"
    rels = {(e["rel"], e["direction"]) for e in rec["edges"]}
    assert {("IN_SECTION", "out"), ("DEFINED_FOR", "in"), ("NAV_REFERS_TO", "in"), ("NEAR_FORMULA", "in")} <= rels
    symbols = next(e for e in rec["edges"] if e["rel"] == "DEFINED_FOR")
    assert symbols["total"] == 2 and {n["kind"] for n in symbols["neighbours"]} == {"SYMBOL"}
    assert all(n["pages"] for n in symbols["neighbours"])
    assert rec["depth"] == 2 and rec["depth2"]
    _body, rec = _ok(client.get(f"/v1/nav/graph/neighbourhood/{ids['sections']['s12']}", headers=HR),
                     "NAV_GRAPH_NEIGHBOURHOOD")
    assert rec["node"]["kind"] == "SECTION" and rec["node"]["pages"] == [SY.pid(SY.S1, 3), SY.pid(SY.S1, 4)]
    page = SY.pid(SY.S1, 3)
    body, rec = _ok(client.get(f"/v1/nav/graph/neighbourhood/{page}", headers=HR), "NAV_GRAPH_NEIGHBOURHOOD")
    assert body["item"]["envelope"]["page_id"] == page
    covers = next(e for e in rec["edges"] if e["rel"] == "COVERS_PAGE")
    assert covers["direction"] == "in" and covers["total"] == 2                  # chapter 1 and section 1.2


def test_errors_and_availability(env):
    client, service, fake, _ids, _app = env
    r = client.get("/v1/nav/graph/paths", params={"term_a": "несуществующее понятие", "term_b": "оседание"},
                   headers=HR)
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"
    assert client.get(f"/v1/nav/graph/neighbourhood/{SY.F2}", params={"depth": 3}, headers=HR).json()["error"][
        "code"] == "INVALID_ARGUMENT"
    assert client.get("/v1/nav/graph/paths", params={"term_a": "a", "term_b": "b", "via": ["physics"]},
                      headers=HR).json()["error"]["code"] == "INVALID_ARGUMENT"
    assert client.get("/v1/nav/graph/neighbourhood/SEC-00000000000000ff", headers=HR).json()["error"][
        "code"] == "NOT_FOUND"
    assert client.get("/v1/nav/graph/neighbourhood/SEC!x", headers=HR).json()["error"]["code"] == "INVALID_ARGUMENT"
    assert client.get("/v1/nav/graph/neighbourhood/SEC-00000000000000ff").status_code == 401
    # another snapshot: served with a warning
    fake.nodes[N.META_ID]["props"]["snapshot_id"] = "snap-20260101T000000Z-00000000"
    body = client.get("/v1/nav/graph/paths", params={"term_a": "ползучесть соли", "term_b": "оседание"},
                      headers=HR).json()
    assert body["ok"] and body["meta"]["warnings"][0]["code"] == "NAV_SNAPSHOT_BEHIND"
    # a failed or absent NAV load → 503, never a partial answer
    fake.nodes[N.META_ID]["props"]["status"] = "FAILED"
    r = client.get("/v1/nav/graph/neighbourhood/" + SY.F2, headers=HR)
    assert r.status_code == 503 and r.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    L.drop_layer(fake, "neo4j")
    assert client.get("/v1/nav/graph/paths", params={"term_a": "a", "term_b": "b"}, headers=HR).status_code == 503
    service.deps.graph = None
    assert client.get("/v1/nav/graph/paths", params={"term_a": "a", "term_b": "b"}, headers=HR).json()["error"][
        "code"] == "DEPENDENCY_UNAVAILABLE"


def test_mcp_tools(env):
    pytest.importorskip("mcp")
    import httpx
    from mcp import Client

    from vkm_corpus.mcp.api_client import ApiClient
    from vkm_corpus.mcp.servers import build_read_server

    _client, _service, _fake, ids, app = env
    server = build_read_server(ApiClient("http://vkm-api", READ, transport=httpx.ASGITransport(app=app)))

    async def go():
        async with Client(server) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            paths = await client.call_tool("concept_paths", {"term_a": "ползучесть соли", "term_b": "оседание",
                                                             "via": ["concepts"]})
            near = await client.call_tool("graph_neighbourhood", {"node_id": ids["terms"]["конвергенция"],
                                                                  "depth": 1, "limit": 10})
            bad = await client.call_tool("graph_neighbourhood", {"node_id": "a/b"})
            return tools, paths, near, bad

    tools, paths, near, bad = asyncio.run(go())
    assert tools["concept_paths"].annotations.read_only_hint and tools["graph_neighbourhood"].annotations.read_only_hint
    assert paths.is_error is False and paths.structured_content["item"]["record"]["paths"][0]["length"] == 3
    record = near.structured_content["item"]["record"]
    assert record["node"]["name"] == "конвергенция"
    kinds = {e["rel"] for e in record["edges"]}
    assert {"CO_OCCURS", "MENTIONED_IN", "DEFINED_AS"} <= kinds
    assert bad.is_error                                                         # schema: ids have no slash

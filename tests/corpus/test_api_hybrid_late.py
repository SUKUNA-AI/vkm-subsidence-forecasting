"""VKM API ``/v1/search/hybrid`` with the late interaction stage (agent L): request fields reach the backend, the
record carries the late score and trace, and a missing token store is DEPENDENCY_UNAVAILABLE through the real
adapter (fake OpenSearch + fake RX580 service). MCP ``search_hybrid``/``retrieval_trace`` pass the late fields."""
from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
pytest.importorskip("PIL")
pytest.importorskip("pytz", reason="DuckDB TIMESTAMPTZ values in Python need pytz (decision note: extra corpus)")
httpx = pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from vkm_corpus.api.app import ApiConfig, create_app  # noqa: E402
from vkm_corpus.api.fixtures import FakeHybrid, hybrid_hits, synthetic_service  # noqa: E402

READ = "read-token-for-hybrid-late-tests-00000000"
H = {"Authorization": f"Bearer {READ}"}


def _late_hits(canon):
    hits = hybrid_hits(canon)
    for i, h in enumerate(hits):
        h["trace"] = {**h["trace"], "late_rank": i + 1, "late_score": 20.0 - i, "late_status": "SCORED",
                      "final_rank": i + 1, "late_unit": {"unit_id": f"u1-{i:016d}", "units": 1, "tokens": 30}}
    return hits


@pytest.fixture()
def env(tmp_path):
    service, canon, _fakes = synthetic_service(tmp_path)
    service.deps.hybrid = FakeHybrid(_late_hits(canon))
    return TestClient(create_app(service, ApiConfig(read_tokens={READ: "read"}))), canon, service


def test_late_fields_reach_the_backend_and_the_record(env):
    client, _canon, service = env
    r = client.post("/v1/search/hybrid", headers=H, json={"query": "ползучесть каменной соли", "limit": 3,
                                                          "late": True, "late_candidates": 50})
    assert r.status_code == 200, r.text
    body = r.json()
    sent = service.deps.hybrid.requests[-1]
    assert sent["late"] is True and sent["late_candidates"] == 50
    first = body["items"][0]["record"]
    assert first["late_score"] == 20.0 and first["trace"]["late_rank"] == 1
    assert first["trace"]["late_unit"]["unit_id"] == "u1-0000000000000000"
    client.get("/v1/search/hybrid", headers=H, params={"q": "мульда", "late": "false", "late_candidates": 10})
    sent = service.deps.hybrid.requests[-1]
    assert sent["late"] is False and sent["late_candidates"] == 10
    client.post("/v1/search/hybrid", headers=H, json={"query": "мульда"})
    assert service.deps.hybrid.requests[-1]["late"] is None                   # server default decides
    client.post("/v1/search/hybrid", headers=H, json={"query": "мульда", "late_candidates": 300})
    assert service.deps.hybrid.requests[-1]["late_candidates"] == 300                # the wider late pool
    for bad in ({"late_candidates": 0}, {"late_candidates": 301}, {"late": "maybe"}):
        resp = client.post("/v1/search/hybrid", headers=H, json={"query": "x", **bad})
        assert resp.status_code == 400 and resp.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_real_adapter_late_store_missing_is_dependency_unavailable(env):
    from vkm_corpus.api.backends import HybridBackend, OpenSearchBackend
    from vkm_corpus.config import load_settings
    from vkm_corpus.search.fakes import FakeOpenSearch
    from vkm_corpus.search.hybrid import EmbedClient

    client, canon, service = env
    settings = load_settings({"VKM_OPENSEARCH_URL": "http://opensearch:9200"})
    search = OpenSearchBackend(settings)
    fake = FakeOpenSearch(bm25=lambda index, body: [])
    name = "vkm-vectors-m1-b1"
    fake.indices.create(index=name, body={"mappings": {"_meta": {
        "build_id": "b1", "build_status": "COMPLETE", "dimension": 4, "model_key": "granite-311m-r2",
        "built_from_snapshot_id": "SNAP"}}})
    page = canon.ids.get("page") or "VKM-SRC-001:p0001"
    fake.indices_[name]["docs"]["u1-1"] = {"id": "u1-1", "unit_kind": "BLOCK_GROUP", "object_ids": ["b"],
                                           "page_id": page, "source_id": "VKM-SRC-001", "dup_group_id": page,
                                           "vector": [1.0, 0.0, 0.0, 0.0]}
    fake.indices.update_aliases({"actions": [{"add": {"index": name, "alias": "vkm-vectors"}}]})
    search._client = fake
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path == "/embed/query":
            return httpx.Response(200, json={"dense": {"model": "granite-311m-r2", "vector": [1.0, 0.0, 0.0, 0.0]}})
        return httpx.Response(503, json={"detail": "late-interaction token store MISSING: no published pack"})

    embed = EmbedClient("http://rx580-retrieval:8790", None, transport=httpx.MockTransport(handler))
    service.deps.hybrid = HybridBackend(settings, search, embed=embed)
    resp = client.post("/v1/search/hybrid", headers=H, json={"query": "оседание", "late": True})
    err = resp.json()["error"]
    assert resp.status_code == 503 and err["code"] == "DEPENDENCY_UNAVAILABLE" and err["stage"] == "hybrid_late"
    assert err["tool"] == "rx580-retrieval" and "http://" not in err["message"] and not resp.json().get("items")
    assert paths == ["/embed/query", "/search/late"]
    ok = client.post("/v1/search/hybrid", headers=H, json={"query": "оседание", "late": False})
    assert ok.status_code == 200 and paths[-1] == "/embed/query"
    # the operator default (VKM_HYBRID_LATE_DEFAULT) applies to requests that do not say
    service.deps.hybrid = HybridBackend(settings, search, embed=embed, late_default=True)
    resp = client.post("/v1/search/hybrid", headers=H, json={"query": "оседание"})
    assert resp.status_code == 503 and resp.json()["error"]["stage"] == "hybrid_late"
    deps = client.get("/v1/status", headers=H).json()["status"]["dependencies"]
    assert deps["late_interaction"]["default"] is True


def test_late_default_from_the_environment(monkeypatch):
    from vkm_corpus.api.backends import HybridBackend
    from vkm_corpus.config import load_settings

    settings = load_settings({"VKM_OPENSEARCH_URL": "http://opensearch:9200"})
    for raw, want in (("1", True), ("off", False), ("", None), ("maybe", None)):
        monkeypatch.setenv("VKM_HYBRID_LATE_DEFAULT", raw)
        assert HybridBackend(settings, embed=object()).late_default is want


def test_mcp_tools_pass_the_late_fields(env):
    pytest.importorskip("mcp")
    from mcp import Client

    from vkm_corpus.mcp.api_client import ApiClient
    from vkm_corpus.mcp.servers import build_read_server

    _client, _canon, service = env
    app = create_app(service, ApiConfig(read_tokens={READ: "read"}))
    server = build_read_server(ApiClient("http://vkm-api", READ, transport=httpx.ASGITransport(app=app)))

    async def run():
        async with Client(server) as c:
            tools = {t.name: t for t in (await c.list_tools()).tools}
            a = await c.call_tool("search_hybrid", {"query": "оседание", "late": True, "late_candidates": 40})
            b = await c.call_tool("retrieval_trace", {"query": "оседание", "late": True})
            return tools, a, b

    tools, a, b = asyncio.run(run())
    assert "late" in tools["search_hybrid"].input_schema["properties"]
    assert a.is_error is False and service.deps.hybrid.requests[-2]["late_candidates"] == 40
    assert service.deps.hybrid.requests[-1]["late"] is True
    rows = b.structured_content["trace"]
    assert rows and rows[0]["late_rank"] == 1 and json.dumps(rows[0]["late_unit"])

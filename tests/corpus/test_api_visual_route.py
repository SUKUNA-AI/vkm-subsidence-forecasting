"""The visual-route flag (agent VIS) reaches the hybrid backend through the API (POST and GET) and MCP
(``search_hybrid``, ``retrieval_trace``); the backend reads its server switch from the environment; the topic dossier
keeps the route off; ``/v1/status`` reports the switch."""
from __future__ import annotations

import asyncio

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

READ = "read-token-for-visual-route-tests-000"
H = {"Authorization": f"Bearer {READ}"}


@pytest.fixture()
def env(tmp_path):
    service, canon, _fakes = synthetic_service(tmp_path)
    service.deps.hybrid = FakeHybrid(hybrid_hits(canon))
    return TestClient(create_app(service, ApiConfig(read_tokens={READ: "read"}))), canon, service


def test_visual_route_flag_reaches_the_backend(env):
    client, _canon, service = env
    client.post("/v1/search/hybrid", headers=H, json={"query": "схема целиков", "visual_route": True})
    assert service.deps.hybrid.requests[-1]["visual_route"] is True
    client.get("/v1/search/hybrid", headers=H, params={"q": "карта", "visual_route": "false"})
    assert service.deps.hybrid.requests[-1]["visual_route"] is False
    client.post("/v1/search/hybrid", headers=H, json={"query": "мульда"})
    assert service.deps.hybrid.requests[-1]["visual_route"] is None
    pytest.importorskip("mcp")
    from mcp import Client

    from vkm_corpus.mcp.api_client import ApiClient
    from vkm_corpus.mcp.servers import build_read_server

    app = create_app(service, ApiConfig(read_tokens={READ: "read"}))
    server = build_read_server(ApiClient("http://vkm-api", READ, transport=httpx.ASGITransport(app=app)))

    async def run():
        async with Client(server) as c:
            tools = {t.name: t for t in (await c.list_tools()).tools}
            await c.call_tool("search_hybrid", {"query": "разрез пласта", "visual_route": True})
            first = dict(service.deps.hybrid.requests[-1])
            await c.call_tool("retrieval_trace", {"query": "разрез пласта", "visual_route": False})
            return tools, first

    tools, first = asyncio.run(run())
    assert "visual_route" in tools["search_hybrid"].input_schema["properties"]
    assert "visual_route" in tools["retrieval_trace"].input_schema["properties"]
    assert first["visual_route"] is True and service.deps.hybrid.requests[-1]["visual_route"] is False


def test_backend_switch_from_the_environment(monkeypatch):
    from vkm_corpus.api.backends import HybridBackend

    class _S:
        opensearch_index_prefix, embed_url, embed_token = "vkm", None, None

    monkeypatch.delenv("VKM_HYBRID_VISUAL_ROUTE", raising=False)
    off = HybridBackend(_S(), search=object(), embed=object())
    assert off.visual.enabled is False and off.visual.mode == "exact"
    monkeypatch.setenv("VKM_HYBRID_VISUAL_ROUTE", "1")
    monkeypatch.setenv("VKM_HYBRID_VISUAL_SEARCH", "hnsw")
    monkeypatch.setenv("VKM_HYBRID_VISUAL_EF_SEARCH", "256")
    on = HybridBackend(_S(), search=object(), embed=object())
    assert on.visual.enabled is True and on.visual.mode == "hnsw" and on.visual.ef_search == 256
    monkeypatch.setenv("VKM_HYBRID_VISUAL_SEARCH", "ivf")
    assert HybridBackend(_S(), search=object(), embed=object()).visual.mode == "exact"


def test_topic_retrieval_keeps_the_visual_route_off():
    from vkm_corpus.api.topic import HybridTopicRetrieval

    seen = []

    class _B:
        def search(self, request):
            seen.append(request)
            return {"hits": [], "stages": {}}

    HybridTopicRetrieval(_B()).search("схема отработки калийного пласта")
    assert seen[0]["visual_route"] is False

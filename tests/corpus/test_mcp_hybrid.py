"""MCP read tools ``search_hybrid`` and ``retrieval_trace`` over the VKM API (fake hybrid backend, in process)."""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("mcp")
pytest.importorskip("fastapi")
pytest.importorskip("duckdb")
pytest.importorskip("PIL")
pytest.importorskip("pytz", reason="DuckDB TIMESTAMPTZ values in Python need pytz (decision note: extra corpus)")

import httpx  # noqa: E402
from mcp import Client  # noqa: E402

from vkm_corpus.api.app import ApiConfig, create_app  # noqa: E402
from vkm_corpus.api.errors import ApiFailure  # noqa: E402
from vkm_corpus.api.fixtures import FakeHybrid, hybrid_hits, synthetic_service  # noqa: E402
from vkm_corpus.mcp.api_client import ApiClient  # noqa: E402
from vkm_corpus.mcp.servers import build_read_server  # noqa: E402

READ = "mcp-hybrid-read-token-0000000000000"


@pytest.fixture()
def stack(tmp_path):
    service, canon, _fakes = synthetic_service(tmp_path)
    service.deps.hybrid = FakeHybrid(hybrid_hits(canon))
    app = create_app(service, ApiConfig(read_tokens={READ: "read"}))
    return build_read_server(ApiClient("http://vkm-api", READ, transport=httpx.ASGITransport(app=app))), canon, service


async def _call(server, tool, args):
    async with Client(server) as client:
        return await client.call_tool(tool, args)


async def _tools(server):
    async with Client(server) as client:
        return {t.name: t for t in (await client.list_tools()).tools}


def test_hybrid_tools_are_read_only_and_typed(stack):
    server, *_ = stack
    tools = asyncio.run(_tools(server))
    for name in ("search_hybrid", "retrieval_trace"):
        assert tools[name].annotations.read_only_hint and not tools[name].annotations.destructive_hint
    kinds = tools["search_hybrid"].input_schema["properties"]["kinds"]
    assert "BLOCK" not in str(kinds) and "FIGURE" in str(kinds)


def test_search_hybrid_returns_the_api_response_with_traces(stack):
    server, canon, service = stack
    result = asyncio.run(_call(server, "search_hybrid", {"query": "оседание земной поверхности", "limit": 5,
                                                         "source_scope": ["SKRU1"]}))
    body = result.structured_content
    assert result.is_error is False and body["ok"]
    assert [it["record"]["trace"]["fused_rank"] for it in body["items"]] == [1, 2, 3]
    assert service.deps.hybrid.requests[-1]["filters"] == {"source_scope": ["SKRU1"]}
    trace = asyncio.run(_call(server, "retrieval_trace", {"query": "оседание",
                                                          "object_ids": [canon.ids["figure"], "VKM-SRC-009:p0001"]}))
    tb = trace.structured_content
    assert tb["ok"] and [r["object_id"] for r in tb["trace"]] == [canon.ids["figure"]]
    assert tb["trace"][0]["bm25_rank"] == 3 and tb["missing"] == ["VKM-SRC-009:p0001"]
    assert tb["item"]["record"]["fusion"] == "RRF"


def test_search_hybrid_reports_unavailable_dependencies(stack):
    server, _canon, service = stack
    service.deps.hybrid = FakeHybrid([], fail=ApiFailure("DEPENDENCY_UNAVAILABLE", "query encoder not reachable",
                                                         stage="hybrid_embed", tool="rx580-retrieval"))
    result = asyncio.run(_call(server, "search_hybrid", {"query": "мульда"}))
    assert result.is_error and result.structured_content["error"]["code"] == "DEPENDENCY_UNAVAILABLE"

"""Brief view of the search answers (``view = "brief"``, ``api.brief``).

* brief: structure (hits, source cards, defaults, warning codes, the hybrid run), size on a 20-hit answer of production
  shape (``search_size_fixture``), GET forms, errors unchanged; the full view stays the serialized ``ApiResponse`` of
  the service result, byte for byte, and is the API default;
* MCP: ``search_text`` / ``search_hybrid`` / ``search_objects`` answer brief by default, ``view="full"`` is the
  ``ApiResponse``;
* a page past the end in brief (the cursors themselves: ``test_api_cursor``).

Synthetic canon and fake backends only."""
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

import search_size_fixture as F  # noqa: E402
from vkm_corpus.api import brief  # noqa: E402
from vkm_corpus.api.app import ApiConfig, create_app  # noqa: E402
from vkm_corpus.api.envelope import ApiResponse, Envelope, Meta  # noqa: E402
from vkm_corpus.api.errors import ApiFailure  # noqa: E402
from vkm_corpus.api.fixtures import SNAPSHOT_ID, FakeHybrid, synthetic_service  # noqa: E402

READ = "brief-read-token-00000000000000000000"
H = {"Authorization": f"Bearer {READ}"}
Q = "оседание земной поверхности"
BRIEF_HYBRID_MAX_CHARS = 8000          # 20 hits of production shape (full: ~74 500 characters)
BRIEF_SEARCH_MAX_CHARS = 7000          # 20 BM25 hits (full: ~60 000)


@pytest.fixture()
def env(tmp_path):
    service, canon, _fakes = synthetic_service(tmp_path)
    service.deps.hybrid = F.rich_hybrid(canon)
    service.deps.search = F.rich_search(canon)
    return TestClient(create_app(service, ApiConfig(read_tokens={READ: "read"}))), canon, service


def _size(body) -> int:
    return len(json.dumps(body, ensure_ascii=False))


def _norm(body):
    body = json.loads(json.dumps(body))
    body["meta"].pop("request_id", None)
    body["meta"].pop("elapsed_ms", None)
    return body


def _ok(resp):
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    return resp.json()


# ---------------------------------------------------------------------------------------------------- brief
def test_hybrid_brief_is_compact_and_keeps_what_an_agent_needs(env):
    client, canon, _service = env
    full = _ok(client.post("/v1/search/hybrid", headers=H, json={"query": Q, "limit": 20}))
    body = _ok(client.post("/v1/search/hybrid", headers=H, json={"query": Q, "limit": 20, "view": "brief"}))
    assert body["view"] == "brief" and body["schema"] == brief.BRIEF_SCHEMA
    assert set(body) == {"ok", "view", "schema", "meta", "note", "defaults", "run", "sources", "items", "next_cursor"}
    assert "not evidence" in body["note"] and "AUTO_EXTRACTED_UNREVIEWED" in body["note"]
    assert body["meta"]["snapshot"] == SNAPSHOT_ID and body["meta"]["request_id"]
    assert {"code": "WORK_HAS_MULTIPLE_COPIES", "count": 1} in body["meta"]["warnings"]
    assert body["defaults"] == {"review_status": "AUTO_EXTRACTED_UNREVIEWED", "origin": "NATIVE"}
    # the same hits in the same order as the full view
    assert [it["id"] for it in body["items"]] == [it["envelope"]["object_id"] for it in full["items"]]
    assert len(body["items"]) == 20 and body["next_cursor"] == full["next_cursor"]
    first = body["items"][0]
    assert first == {"id": "VKM-SRC-001:p0001", "rank": 1, "object_type": "PAGE", "bm25_rank": 1, "dense_rank": 2,
                     "fused_rank": 1, "late_score": 21.53, "snippet": first["snippet"],
                     "duplicates": ["VKM-SRC-002:p0001"]}
    assert "<em>" not in first["snippet"] and len(first["snippet"]) <= brief.SNIPPET_CHARS
    figure = next(it for it in body["items"] if it["object_type"] == "FIGURE")
    assert figure["id"] == canon.ids["figure"] and figure["object_label"] == "рис. 1"
    assert figure["title_or_caption"] == "Рис. 1. Синтетическая схема"      # the label is not written twice
    formula = next(it for it in body["items"] if it["object_type"] == "FORMULA")
    assert formula["origin"] == "OCR"                                     # differs from defaults: said per hit
    for it in body["items"]:
        assert "envelope" not in it and "page_id" not in it and "source_id" not in it    # prefixes of the id
        assert not set(it) & {"trace", "rerank_candidate", "highlights", "provenance"}
    # one card per source: short name (stem of the register path), work, scope, copies of the work
    assert body["sources"] == {
        "VKM-SRC-001": {"name": "synthetic_001", "work_id": "VKM-WRK-001", "scope": ["VKM_REGIONAL"], "work_copies": 2},
        "VKM-SRC-002": {"name": "synthetic_002", "work_id": "VKM-WRK-001", "scope": ["VKM_REGIONAL"], "work_copies": 2}}
    run = body["run"]
    assert run["route"] == "default" and run["late"] is True and run["ms"] == 640.7
    assert run["stages"] == {"bib_route": "NOT_APPLIED", "visual_route": "DISABLED", "graph": {"collapse": "APPLIED"}}
    assert run["fused_total"] == 20 and run["totals"] == {"PAGE": {"bm25": 20}}
    # size: an order of magnitude below the full answer
    assert _size(body) <= BRIEF_HYBRID_MAX_CHARS and _size(body) * 8 <= _size(full)


def test_bm25_and_objects_brief(env):
    client, canon, _service = env
    full = _ok(client.post("/v1/search", headers=H, json={"query": Q, "limit": 20}))
    body = _ok(client.post("/v1/search", headers=H, json={"query": Q, "limit": 20, "view": "brief"}))
    assert "run" not in body and len(body["items"]) == 20
    assert body["items"][0] == {"id": "VKM-SRC-001:p0001", "rank": 1, "object_type": "PAGE", "bm25_score": 14.2,
                                "snippet": body["items"][0]["snippet"]}
    assert _size(body) <= BRIEF_SEARCH_MAX_CHARS and _size(body) * 8 <= _size(full)
    get = _ok(client.get("/v1/search", params={"q": Q, "limit": 3, "view": "brief"}, headers=H))
    assert get["view"] == "brief" and len(get["items"]) == 3
    objects = _ok(client.post("/v1/objects/query", headers=H,
                              json={"kinds": ["FIGURE", "TABLE", "FORMULA"], "view": "brief"}))
    by_type = {it["object_type"]: it for it in objects["items"]}
    assert by_type["FIGURE"] == {"id": canon.ids["figure"], "object_type": "FIGURE", "object_label": "рис. 1",
                                 "title_or_caption": "Рис. 1. Синтетическая схема", "has_image": True}
    assert by_type["FORMULA"]["latex"] == "E = m c^2" and by_type["FORMULA"]["origin"] == "OCR"
    assert by_type["TABLE"]["has_image"] is True and objects["sources"]["VKM-SRC-001"]["name"] == "synthetic_001"
    hybrid_get = _ok(client.get("/v1/search/hybrid", params={"q": Q, "limit": 2, "view": "brief"}, headers=H))
    assert hybrid_get["view"] == "brief" and hybrid_get["run"]["late"] is True


def test_full_view_is_the_serialized_api_response_and_the_default(env):
    client, _canon, service = env
    for path, req in (("/v1/search/hybrid", {"query": Q, "limit": 20}), ("/v1/search", {"query": Q, "limit": 20}),
                      ("/v1/objects/query", {"kinds": ["FIGURE", "TABLE", "FORMULA", "BLOCK"]})):
        default = _ok(client.post(path, headers=H, json=req))
        explicit = _ok(client.post(path, headers=H, json={**req, "view": "full"}))
        assert _norm(default) == _norm(explicit)
        assert set(default) == set(ApiResponse.model_fields)
        for item in default["items"]:
            Envelope.model_validate(item["envelope"])
    # byte for byte: the HTTP body is the service result serialized as before the brief view existed
    result = service.search_hybrid(Q, ["PAGE"], {}, 20, None)
    default = _ok(client.post("/v1/search/hybrid", headers=H, json={"query": Q, "limit": 20}))
    meta = Meta(request_id="x", canonical_snapshot_id=SNAPSHOT_ID, elapsed_ms=0.0, warnings=result.warnings)
    expected = ApiResponse(ok=True, meta=meta, item=result.item, items=result.items,
                           next_cursor=result.next_cursor).model_dump(mode="json")
    assert json.dumps(_norm(default), sort_keys=True) == json.dumps(_norm(expected), sort_keys=True)


def test_errors_are_the_same_in_both_views(env):
    client, _canon, service = env
    bad = client.post("/v1/search/hybrid", headers=H, json={"query": Q, "view": "short"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "INVALID_ARGUMENT"
    service.deps.hybrid = FakeHybrid([], fail=ApiFailure("DEPENDENCY_UNAVAILABLE", "query encoder not reachable",
                                                         stage="hybrid_embed", tool="rx580-retrieval"))
    a = client.post("/v1/search/hybrid", headers=H, json={"query": Q, "view": "brief"})
    b = client.post("/v1/search/hybrid", headers=H, json={"query": Q})
    assert a.status_code == b.status_code == 503
    assert a.json()["error"] == {**b.json()["error"], "log_ref": a.json()["error"]["log_ref"]}


def test_snippet_cut_and_source_names(env):
    _client, canon, service = env
    text = "начало " + "слово " * 40 + "<em>оседание</em> земной " + "поверхности " * 30
    s = brief.snippet(text)
    assert len(s) <= brief.SNIPPET_CHARS and "<em>" not in s and "оседание" in s
    assert s.startswith("…слово") and s.endswith("…")                       # cut at word boundaries
    assert brief.snippet("короткий <em>фрагмент</em>") == "короткий фрагмент"
    assert brief.cut("а" * 200, 60) == "а" * 59 + "…" and brief.cut(" a  b ", 10) == "a b"
    assert brief.source_names(service.canon, ["VKM-SRC-001", "VKM-SRC-999", None]) == {"VKM-SRC-001": "synthetic_001"}
    assert brief.source_names(service.canon, []) == {}


# ---------------------------------------------------------------------------------------------------- MCP
def test_mcp_search_tools_answer_brief_by_default(env):
    pytest.importorskip("mcp")
    from mcp import Client

    from vkm_corpus.mcp.api_client import ApiClient
    from vkm_corpus.mcp.servers import build_read_server

    client, _canon, service = env
    server = build_read_server(ApiClient("http://vkm-api", READ, transport=httpx.ASGITransport(app=client.app)))

    async def run():
        async with Client(server) as c:
            tools = {t.name: t for t in (await c.list_tools()).tools}
            out = {name: await c.call_tool(name, args) for name, args in (
                ("search_hybrid", {"query": Q, "limit": 20}), ("search_text", {"query": Q, "limit": 20}),
                ("search_objects", {"kinds": ["FIGURE"]}))}
            out["full"] = await c.call_tool("search_hybrid", {"query": Q, "limit": 20, "view": "full"})
            return tools, out

    tools, out = asyncio.run(run())
    for name in ("search_hybrid", "search_text", "search_objects"):
        prop = tools[name].input_schema["properties"]["view"]
        assert prop["default"] == "brief" and set(prop["enum"]) == {"brief", "full"}
        res = out[name]
        assert res.is_error is False and res.structured_content["view"] == "brief"
        assert json.loads(res.content[-1].text) == res.structured_content
    hybrid = out["search_hybrid"].structured_content
    assert len(hybrid["items"]) == 20 and _size(hybrid) <= BRIEF_HYBRID_MAX_CHARS
    full = out["full"].structured_content
    assert "view" not in full and full["items"][0]["record"]["trace"]["fused_rank"] == 1
    assert _size(full) >= 8 * _size(hybrid)


def test_brief_of_a_page_past_the_end(env):
    client, *_ = env
    body = _ok(client.post("/v1/search/hybrid", headers=H, json={"query": Q, "limit": 4, "candidates": 10,
                                                                 "cursor": "10", "view": "brief"}))
    assert body["items"] == [] and body["sources"] == {} and body["next_cursor"] is None
    assert {"code": "CURSOR_PAST_END", "count": 10} in body["meta"]["warnings"]

"""VKM API search results carry the printed object label (agent L): «рис. 1», «табл. 1», «(1)» from the canon, the NAV
equation number for a formula without one; the bibliographic-route flag reaches the hybrid backend (API and MCP)."""
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
from vkm_corpus.api.service import object_label  # noqa: E402

READ = "read-token-for-object-label-tests-0000"
H = {"Authorization": f"Bearer {READ}"}


@pytest.fixture()
def env(tmp_path):
    service, canon, _fakes = synthetic_service(tmp_path)
    service.deps.hybrid = FakeHybrid(hybrid_hits(canon))
    return TestClient(create_app(service, ApiConfig(read_tokens={READ: "read"}))), canon, service


def test_label_function():
    assert object_label("FIGURE", {"figure_label": "Рис. 3.1"}) == {
        "object_label": "рис. 3.1", "object_number": "3.1", "object_label_raw": "Рис. 3.1", "object_label_origin": "CANON"}
    assert object_label("FIGURE", {"figure_label": "Fig. 2"})["object_label"] == "fig. 2"
    assert object_label("TABLE", {"table_label": "Таблица 2а"})["object_label"] == "табл. 2а"
    assert object_label("TABLE", {"table_label": "Table 4"})["object_label"] == "table 4"
    assert object_label("FORMULA", {"equation_label": "(3.2)"})["object_label"] == "(3.2)"
    nav = object_label("FORMULA", {"equation_label": None}, "3.12")
    assert nav["object_label"] == "(3.12)" and nav["object_label_origin"] == "NAV"
    assert object_label("FORMULA", {"equation_label": None}) is None
    assert object_label("FIGURE", {"figure_label": None}) is None and object_label("PAGE", {}) is None


def test_search_and_hybrid_records_carry_the_label(env):
    client, canon, _service = env
    body = client.post("/v1/search", headers=H, json={"query": "схема", "kinds": ["FIGURE"], "limit": 5}).json()
    figs = [it["record"] for it in body["items"] if it["envelope"]["object_id"] == canon.ids["figure"]]
    assert figs and figs[0]["object_label"] == "рис. 1" and figs[0]["object_number"] == "1"
    hyb = client.post("/v1/search/hybrid", headers=H, json={"query": "схема", "limit": 5}).json()
    fig = next(it["record"] for it in hyb["items"] if it["envelope"]["object_id"] == canon.ids["figure"])
    assert fig["object_label"] == "рис. 1"
    page = next(it["record"] for it in hyb["items"] if it["record"]["object_type"] == "PAGE")
    assert "object_label" not in page


def test_nav_equation_numbers_fallback(env):
    _client, _canon, service = env

    class _Nav:
        def query(self, sql, params):
            assert "formula_context" in sql
            return [{"formula_id": p, "equation_number": "2.7"} for p in params]

    service.deps.nav = _Nav()
    assert service._nav_equation_numbers(["f-1", "f-1"]) == {"f-1": "2.7"}

    class _Broken:
        def query(self, sql, params):
            raise RuntimeError("the navigation layer is not published")

    service.deps.nav = _Broken()
    assert service._nav_equation_numbers(["f-1"]) == {}
    service.deps.nav = None
    assert service._nav_equation_numbers(["f-1"]) == {}


def test_bib_route_flag_reaches_the_backend(env):
    client, _canon, service = env
    client.post("/v1/search/hybrid", headers=H, json={"query": "работы Баряха", "bib_route": True})
    assert service.deps.hybrid.requests[-1]["bib_route"] is True
    client.get("/v1/search/hybrid", headers=H, params={"q": "мульда", "bib_route": "false"})
    assert service.deps.hybrid.requests[-1]["bib_route"] is False
    client.post("/v1/search/hybrid", headers=H, json={"query": "мульда"})
    assert service.deps.hybrid.requests[-1]["bib_route"] is None
    pytest.importorskip("mcp")
    from mcp import Client

    from vkm_corpus.mcp.api_client import ApiClient
    from vkm_corpus.mcp.servers import build_read_server

    app = create_app(service, ApiConfig(read_tokens={READ: "read"}))
    server = build_read_server(ApiClient("http://vkm-api", READ, transport=httpx.ASGITransport(app=app)))

    async def run():
        async with Client(server) as c:
            tools = {t.name: t for t in (await c.list_tools()).tools}
            await c.call_tool("search_hybrid", {"query": "список литературы", "bib_route": False})
            return tools

    tools = asyncio.run(run())
    assert "bib_route" in tools["search_hybrid"].input_schema["properties"]
    assert service.deps.hybrid.requests[-1]["bib_route"] is False

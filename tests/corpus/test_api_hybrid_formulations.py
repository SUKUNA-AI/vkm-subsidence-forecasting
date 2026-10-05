"""``/v1/search/hybrid`` and MCP ``search_hybrid`` with the opt-in fields: ``formulations`` (the caller's other
wordings), ``expand=terms`` (≤ 3 more from the NAV term dictionary, deterministic), ``max_per_source`` and the wider
late pool. The defaults send today's request. Fake hybrid backend and NAV query functions (stand-ins)."""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")
pytest.importorskip("PIL")
pytest.importorskip("pytz", reason="DuckDB TIMESTAMPTZ values in Python need pytz (decision note: extra corpus)")
httpx = pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from vkm_corpus.api.app import ApiConfig, create_app  # noqa: E402
from vkm_corpus.api.fixtures import FakeHybrid, hybrid_hits, synthetic_service  # noqa: E402
from vkm_corpus.search import hybrid as H  # noqa: E402

READ = "read-token-for-hybrid-formulations-000"
HDR = {"Authorization": f"Bearer {READ}"}
Q = "ползучесть каменной соли"
TRANSLATIONS = {Q: "rock salt creep", "ползучесть галита": "halite creep"}
SYNONYMS = {Q: "ползучесть галита"}
ABBREVIATIONS = {Q: "ползучесть КС"}


class FakeNav:
    """``run`` of the NAV store with stand-in query functions; a name it does not know raises (a build without it)."""

    def __init__(self, functions):
        self.functions, self.calls = functions, []

    def run(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        if name not in self.functions:
            raise LookupError(f"{name} is not in this build")
        return self.functions[name](None, *args, **kwargs)


def fake_translate_query(con, text, **kw):
    out = TRANSLATIONS.get(text)
    return {"text": text, "source_language": "ru", "target_language": "en", "translation": out, "coverage": 1.0,
            "terms": [{"span": text, "translation": out, "score": 0.95, "pair_id": "TTR-00000000000000a1"}] if out
            else []}


def fake_expand_query(con, text, *, narrower=True, relations=("SYNONYM", "ABBREVIATION"), **kw):
    table = SYNONYMS if tuple(relations) == ("SYNONYM",) else ABBREVIATIONS
    out = table.get(text)
    exp = [{"kind": "equivalents", "text": out, "terms": [{"span": "соли", "equivalent": out, "relation": relations[0],
                                                          "score": 0.9, "pair_id": "TTR-00000000000000b1"}]}]
    if narrower:
        exp.append({"kind": "narrower", "text": f"{text} установившаяся ползучесть", "terms": []})
    return {"query": text, "expansions": exp if out else [], "phrases": 2}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("VKM_HYBRID_TRANSLATE_DEFAULT", raising=False)
    service, canon, _fakes = synthetic_service(tmp_path)
    service.deps.hybrid = FakeHybrid(hybrid_hits(canon))
    service.deps.hybrid.late_default = False                       # no translation legs unless asked for
    service.deps.nav = FakeNav({"translate_query": fake_translate_query, "expand_query": fake_expand_query})
    return TestClient(create_app(service, ApiConfig(read_tokens={READ: "read"}))), service


def _ok(resp):
    assert resp.status_code == 200, resp.text
    return resp.json()


def _codes(body):
    return {w["code"] for w in body["meta"]["warnings"]}


def test_defaults_send_todays_request(env):
    client, service = env
    body = _ok(client.post("/v1/search/hybrid", headers=HDR, json={"query": Q}))
    sent = service.deps.hybrid.requests[-1]
    assert not {"formulations", "max_per_source", "expansions"} & set(sent)
    assert "formulations" not in body["item"]["record"] and service.deps.nav.calls == []
    _ok(client.get("/v1/search/hybrid", headers=HDR, params={"q": Q}))
    assert not {"formulations", "max_per_source"} & set(service.deps.hybrid.requests[-1])


def test_caller_formulations_and_the_cap_reach_the_backend(env):
    client, service = env
    body = _ok(client.post("/v1/search/hybrid", headers=HDR, json={
        "query": Q, "formulations": ["  ползучесть галита ", "creep of rock salt"], "max_per_source": 5}))
    sent = service.deps.hybrid.requests[-1]
    assert sent["formulations"] == ({"text": "ползучесть галита", "origin": "caller", "expansions": ()},
                                    {"text": "creep of rock salt", "origin": "caller", "expansions": ()})
    assert sent["max_per_source"] == 5
    plan = body["item"]["record"]["formulations"]
    assert plan["caller"] == 2 and plan["expand"] == "none" and [f["origin"] for f in plan["sent"]] == ["caller"] * 2
    # with the translation on, every formulation gets its own translation legs (as its own request would)
    _ok(client.post("/v1/search/hybrid", headers=HDR, json={"query": Q, "translate": True,
                                                            "formulations": ["ползучесть галита"]}))
    sent = service.deps.hybrid.requests[-1]
    assert sent["expansions"] == ("rock salt creep",)
    assert sent["formulations"][0]["expansions"] == ("halite creep",)
    _ok(client.get("/v1/search/hybrid", headers=HDR, params=[("q", Q), ("formulations", "ползучесть галита"),
                                                             ("formulations", "creep"), ("max_per_source", "3")]))
    sent = service.deps.hybrid.requests[-1]
    assert [f["text"] for f in sent["formulations"]] == ["ползучесть галита", "creep"] and sent["max_per_source"] == 3


def test_expand_terms_is_deterministic(env):
    client, service = env
    first = _ok(client.post("/v1/search/hybrid", headers=HDR, json={"query": Q, "expand": "terms"}))
    sent = service.deps.hybrid.requests[-1]
    assert [(f["origin"], f["text"]) for f in sent["formulations"]] == [
        ("translation", "rock salt creep"), ("synonyms", "ползучесть галита"), ("abbreviations", "ползучесть КС")]
    expand_calls = [kw for name, _a, kw in service.deps.nav.calls if name == "expand_query"]
    assert expand_calls == [{"narrower": False, "relations": ("SYNONYM",)},
                            {"narrower": False, "relations": ("ABBREVIATION",)}]
    terms = first["item"]["record"]["formulations"]["terms"]
    assert terms["status"] == "APPLIED" and terms["parts"]["translation"]["terms"][0]["pair_id"].startswith("TTR-")
    assert "not evidence" in terms["note"]
    _ok(client.post("/v1/search/hybrid", headers=HDR, json={"query": Q, "expand": "terms"}))
    assert service.deps.hybrid.requests[-1]["formulations"] == sent["formulations"]          # same answer again
    _ok(client.get("/v1/search/hybrid", headers=HDR, params={"q": Q, "expand": "terms"}))
    assert service.deps.hybrid.requests[-1]["formulations"] == sent["formulations"]
    other = _ok(client.post("/v1/search/hybrid", headers=HDR, json={"query": "мульда", "expand": "terms"}))
    assert "formulations" not in service.deps.hybrid.requests[-1]                            # nothing covered
    assert other["item"]["record"]["formulations"]["terms"]["status"] == "NOT_COVERED"


def test_budget_and_duplicates_are_listed_not_dropped(env):
    client, service = env
    body = _ok(client.post("/v1/search/hybrid", headers=HDR, json={
        "query": Q, "expand": "terms", "formulations": ["a", "ПОЛЗУЧЕСТЬ каменной соли", "b", "c"]}))
    sent = service.deps.hybrid.requests[-1]["formulations"]
    assert [f["text"] for f in sent] == ["a", "b", "c", "rock salt creep"]                    # caller's first
    skipped = body["item"]["record"]["formulations"]["skipped"]
    assert [(s["origin"], s["reason"]) for s in skipped] == [
        ("caller", "DUPLICATE"), ("synonyms", "BUDGET"), ("abbreviations", "BUDGET")]
    assert "FORMULATIONS_OVER_BUDGET" in _codes(body)


def test_terms_without_the_navigation_layer_warn(env):
    client, service = env
    service.deps.nav = None
    body = _ok(client.post("/v1/search/hybrid", headers=HDR, json={"query": Q, "expand": "terms"}))
    assert "formulations" not in service.deps.hybrid.requests[-1]
    assert body["item"]["record"]["formulations"]["terms"]["status"] == "UNAVAILABLE"
    assert "TERM_EXPANSION_UNAVAILABLE" in _codes(body)
    service.deps.nav = FakeNav({})                                     # a build without the dictionary functions
    body = _ok(client.post("/v1/search/hybrid", headers=HDR, json={"query": Q, "expand": "terms"}))
    parts = body["item"]["record"]["formulations"]["terms"]["parts"]
    assert {p["status"] for p in parts.values()} == {"UNAVAILABLE"} and "TERM_EXPANSION_UNAVAILABLE" in _codes(body)


@pytest.mark.parametrize("bad", [{"formulations": ["a", "b", "c", "d", "e"]}, {"formulations": [""]},
                                 {"formulations": ["x" * 513]}, {"expand": "all"}, {"max_per_source": 0},
                                 {"max_per_source": 51}, {"late_candidates": 301}])
def test_invalid_fields_are_refused(env, bad):
    client, _service = env
    resp = client.post("/v1/search/hybrid", headers=HDR, json={"query": Q, **bad})
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_invalid_get_fields_are_refused(env):
    client, _service = env
    many = [("q", Q)] + [("formulations", f"f{i}") for i in range(5)]
    for params in (many, [("q", Q), ("expand", "all")], [("q", Q), ("max_per_source", "0")],
                   [("q", Q), ("late_candidates", "301")]):
        resp = client.get("/v1/search/hybrid", headers=HDR, params=params)
        assert resp.status_code == 400 and resp.json()["error"]["code"] == "INVALID_ARGUMENT", params


class FusedHybrid(FakeHybrid):
    """The backend's answer when the formulations were fused: a longer list than the candidates of one query."""

    def __init__(self, hits, total, applied=True):
        super().__init__(hits)
        self.total, self.applied = total, applied

    def search(self, request):
        out = super().search(request)
        out["fused_total"] = self.total
        if self.applied:
            out["stages"]["formulations"] = {"status": "APPLIED", "runs": []}
        return out


def test_cursor_pages_through_the_fused_list(env):
    client, service = env
    service.deps.hybrid = FusedHybrid([], 150)
    body = _ok(client.post("/v1/search/hybrid", headers=HDR, json={"query": Q, "formulations": ["a"], "limit": 50,
                                                                   "cursor": "50"}))
    assert body["next_cursor"] == "100"                               # beyond one query's 100 candidates
    service.deps.hybrid = FusedHybrid([], 150, applied=False)
    body = _ok(client.post("/v1/search/hybrid", headers=HDR, json={"query": Q, "limit": 50, "cursor": "50"}))
    assert body["next_cursor"] is None                                # today's rule: min(fused_total, candidates)


def test_api_limits_follow_the_search_module(env):
    client, _service = env
    schema = client.app.openapi()["components"]["schemas"]["HybridSearchBody"]["properties"]
    assert schema["late_candidates"]["maximum"] == H.MAX_LATE_CANDIDATES
    assert schema["max_per_source"]["anyOf"][0]["maximum"] == H.MAX_PER_SOURCE
    assert schema["formulations"]["anyOf"][0]["maxItems"] == H.MAX_FORMULATIONS - 1


def test_mcp_search_hybrid_passes_the_opt_in_fields(env):
    pytest.importorskip("mcp")
    from mcp import Client

    from vkm_corpus.mcp.api_client import ApiClient
    from vkm_corpus.mcp.servers import build_read_server

    _client, service = env
    app = create_app(service, ApiConfig(read_tokens={READ: "read"}))
    server = build_read_server(ApiClient("http://vkm-api", READ, transport=httpx.ASGITransport(app=app)))

    async def run():
        async with Client(server) as c:
            tools = {t.name: t for t in (await c.list_tools()).tools}
            plain = await c.call_tool("search_hybrid", {"query": Q, "limit": 5})
            plain_req = dict(service.deps.hybrid.requests[-1])
            full = await c.call_tool("search_hybrid", {"query": Q, "formulations": ["ползучесть галита"],
                                                       "expand": "terms", "max_per_source": 4,
                                                       "late_candidates": 300})
            return tools, plain, plain_req, full

    tools, plain, plain_req, full = asyncio.run(run())
    props = tools["search_hybrid"].input_schema["properties"]
    assert {"formulations", "expand", "max_per_source"} <= set(props)
    assert "formulations" not in tools["search_hybrid"].input_schema.get("required", [])
    assert tools["retrieval_trace"].input_schema["properties"]["late_candidates"]["maximum"] == 300
    assert plain.is_error is False and not {"formulations", "max_per_source"} & set(plain_req)
    sent = service.deps.hybrid.requests[-1]
    assert full.is_error is False and sent["max_per_source"] == 4 and sent["late_candidates"] == 300
    assert [f["origin"] for f in sent["formulations"]] == ["caller", "translation", "abbreviations"]

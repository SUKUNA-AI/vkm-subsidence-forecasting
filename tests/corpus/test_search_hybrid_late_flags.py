"""Experiment flags of the late stage (agent Д, 13.10; off by default): ``late_fusion = "rrf"`` (RRF of the RRF order
and the late order inside each kind's head) and ``late_query = "best_translation"`` (the translation scored too; max of
MaxSim / query tokens). Without the flags the answer is the one of today, key for key; the flags travel through the API
and MCP, apply to every formulation and are recorded in ``stages.late`` and the trace. Fake OpenSearch + fake RX580
service (httpx MockTransport); synthetic ids."""
from __future__ import annotations

import asyncio
import json

import pytest

httpx = pytest.importorskip("httpx")

from vkm_corpus.search.fakes import FakeOpenSearch  # noqa: E402
from vkm_corpus.search.hybrid import (  # noqa: E402
    EmbedClient,
    HybridRequest,
    LateResult,
    best_translation,
    hybrid_search,
    late_order,
    late_order_rrf,
)
from vkm_corpus.search.query import SearchRequestError  # noqa: E402

DIM = 4
A1, A2, A3 = (f"VKM-SRC-001:p000{i}" for i in range(1, 4))
B1, C1 = "VKM-SRC-002:p0001", "VKM-SRC-003:p0001"
META = {"build_id": "20261013t120000z-aaaaaaaa-bbbbbbbb", "built_from_snapshot_id": "SNAP-1", "dimension": DIM,
        "model_key": "granite-311m-r2", "config_signature": "c" * 64, "space_type": "innerproduct",
        "build_status": "COMPLETE"}
# BM25 pages per query word (no dense vectors: a run's RRF order is its BM25 legs)
BM25 = {"альфа": [A1, A2, A3, B1, C1], "бета": [B1, A1, C1], "гамма": [C1], "дельта": [A2]}
# the late order before the flags (trace keys and stages.late keys of a late run with a translation leg)
TRACE_KEYS = {"bm25_rank", "bm25_score", "dense_rank", "dense_score", "fused_rank", "rrf_score", "rrf_k", "late_rank",
              "rerank_rank", "late_score", "late_status", "final_rank", "late_unit", "expansion_ranks"}
LATE_KEYS = {"engine", "model_key", "query_signature", "query_tokens", "store", "candidates", "scored", "unscored",
             "unscored_ids", "ordering", "page_score", "page_exclude_kinds", "timings_ms"}


def _client():
    def answer(index, body):
        if index != "vkm-pages":
            return []
        text = json.dumps(body, ensure_ascii=False)
        word = next((w for w in BM25 if w in text), None)
        return [{"_id": p, "_index": "vkm-pages-m1-b-test", "_score": 10.0 - i,
                 "_source": {"id": p, "object_type": "PAGE", "source_id": p.split(":")[0], "work_id": "VKM-WRK-001",
                             "page_id": p}} for i, p in enumerate(BM25.get(word, []))]

    c = FakeOpenSearch(bm25=answer)
    name = f"vkm-vectors-m1-{META['build_id']}"
    c.indices.create(index=name, body={"mappings": {"_meta": dict(META)}})
    c.indices.update_aliases({"actions": [{"add": {"index": name, "alias": "vkm-vectors"}}]})
    return c


def _embed(late=None, tokens=None):
    """Dense encoder + ``/search/late`` scoring every target with ``late[query][id]`` (default 1.0) and reporting
    ``tokens[query]`` query tokens (default 4; None → no count)."""
    calls = []
    late, tokens = late or {}, tokens or {}

    def handler(request):
        body = json.loads(request.content)
        calls.append((request.url.path, body))
        if request.url.path == "/search/late":
            q = body["query"]
            scores = late.get(q, {})
            results = [{"id": t["id"], "kind": t["kind"], "status": "SCORED" if scores.get(t["id"], 1.0) is not None
                        else "NO_TOKENS", "late_score": scores.get(t["id"], 1.0), "best_unit_id": f"{q}:{t['id']}",
                        "units": 1, "tokens": 9} for t in body["targets"]]
            return httpx.Response(200, json={"model": "mlateon", "query_signature": f"sig-{q}",
                                             "n_query_tokens": tokens.get(q, 4), "store": {"snapshot_id": "SNAP-1"},
                                             "timings_ms": {"maxsim": 1.0}, "results": results})
        return httpx.Response(200, json={"dense": {"model": "granite-311m-r2", "signature": "q" * 64,
                                                   "dimension": DIM, "vector": [1.0, 0.0, 0.0, 0.0]}})

    client = EmbedClient("http://rx580-retrieval:8790", "tok", transport=httpx.MockTransport(handler))
    client.calls = calls
    return client


def _search(query="альфа", embed=None, **kw):
    kw.setdefault("candidates", 10)
    kw.setdefault("late", True)
    return hybrid_search(_client(), embed or _embed(), HybridRequest(query=query, **kw), "vkm")


def _strip(x):
    if isinstance(x, dict):
        return {k: _strip(v) for k, v in x.items() if k not in ("timings_ms", "ms")}
    if isinstance(x, list):
        return [_strip(v) for v in x]
    return x


def _ids(out):
    return [h["id"] for h in out["hits"]]


LATE_A = {"альфа": {A1: 1.0, A2: 5.0, A3: 2.0, B1: 7.0, C1: 3.0}}


# ------------------------------------------------------------------------------------------------ defaults
def test_without_flags_the_answer_is_todays_key_for_key():
    """The default path: explicit default values give the same answer as no flags, no new trace or stage key, one
    late call, and the late order of today (replace)."""
    e0, e1 = _embed(LATE_A), _embed(LATE_A)
    plain = _search(embed=e0, expansions=("бета",))
    explicit = _search(embed=e1, expansions=("бета",), late_fusion="replace", late_query="original")
    assert _strip(plain) == _strip(explicit)
    assert json.dumps(_strip(plain), sort_keys=True, ensure_ascii=False) == \
        json.dumps(_strip(explicit), sort_keys=True, ensure_ascii=False)
    assert all(set(h["trace"]) == TRACE_KEYS for h in plain["hits"])
    assert set(plain["stages"]["late"]) == LATE_KEYS and "fragments" not in plain["stages"]
    assert [b["query"] for p, b in e0.calls if p == "/search/late"] == ["альфа"]
    # today's order: the late score alone (B1 7, A2 5, C1 3, A3 2, A1 1)
    assert _ids(plain) == [B1, A2, C1, A3, A1]
    assert not any(w.startswith("LATE_FLAGS_IGNORED") for w in plain["warnings"])
    req = HybridRequest(query="x")
    req.validate()
    assert (req.late_fusion, req.late_query, req.group_fragments) == ("replace", "original", False)


def test_flag_values_are_validated_and_ignored_flags_are_said():
    for bad in ({"late_fusion": "mix"}, {"late_query": "english"}, {"late_fusion": None}):
        with pytest.raises(SearchRequestError) as exc:
            HybridRequest(query="x", **bad).validate()
        assert exc.value.code == "E_BAD_MODE"
    out = _search(late=False, late_fusion="rrf", late_query="best_translation", expansions=("бета",))
    assert any(w.startswith("LATE_FLAGS_IGNORED: late_fusion=rrf, late_query=best_translation") for w in
               out["warnings"])
    assert "NOT_RUN" in out["stages"]["late"]


# ------------------------------------------------------------------------------------------------ late_fusion = rrf
def test_late_order_rrf_fuses_the_two_orders_inside_each_kind():
    fused = [("A", 0.5), ("F", 0.4), ("B", 0.3), ("G", 0.2), ("C", 0.1), ("T", 0.05)]
    kind = {"A": "PAGE", "B": "PAGE", "C": "PAGE", "T": "PAGE", "F": "FIGURE", "G": "FIGURE"}
    res = {"A": {"status": "SCORED", "late_score": 1.0}, "B": {"status": "NO_TOKENS"},
           "C": {"status": "SCORED", "late_score": 3.0}, "F": {"status": "SCORED", "late_score": 1.0},
           "G": {"status": "SCORED", "late_score": 50.0}}
    replace, _ = late_order(fused, kind, res, 5)
    assert [k for k, _s in replace] == ["C", "G", "A", "F", "B", "T"]
    order, late_rank, frank, fscore = late_order_rrf(fused, kind, res, 5, k=60)
    # pages A (rrf 1, late 2) > C (rrf 3, late 1) > B (rrf 2, unscored: the RRF term only); figures F, G tie (1+2 =
    # 2+1) → the RRF order; positions of the kinds kept (H-44), the tail T unchanged
    assert [k for k, _s in order] == ["A", "F", "C", "G", "B", "T"]
    assert late_rank == {"C": 1, "A": 2, "G": 1, "F": 2}
    assert frank == {"A": 1, "C": 2, "B": 3, "F": 1, "G": 2}
    assert fscore["A"] == pytest.approx(1 / 61 + 1 / 62) and fscore["B"] == pytest.approx(1 / 62)
    assert fscore["F"] == fscore["G"]


def test_rrf_mode_end_to_end_with_trace_and_stage():
    out = _search(embed=_embed(LATE_A), late_fusion="rrf")
    # RRF order A1..C1 (BM25), late order B1, A2, C1, A3, A1: RRF(60) → A2, B1, A1, A3, C1 (replace: B1, A2, C1, A3, A1)
    assert _ids(out) == [A2, B1, A1, A3, C1]
    t = {h["id"]: h["trace"] for h in out["hits"]}
    assert (t[A2]["late_rank"], t[A2]["late_fusion_rank"], t[A2]["fused_rank"]) == (2, 1, 2)
    assert t[A2]["late_fusion_score"] == round(2 / 62, 8) and t[B1]["late_rank"] == 1
    assert t[A2]["final_rank"] == 1 and t[A2]["late_score"] == 5.0                 # the late score is today's
    late = out["stages"]["late"]
    assert late["fusion"] == {"mode": "rrf", "k": 60, "orders": ["rrf", "late"]} and "RRF(k)" in late["ordering"]
    assert "late_query" not in late


# ------------------------------------------------------------------------------------------------ best_translation
def _lr(results, tokens, sig="s"):
    return LateResult({k: ({"status": "SCORED", "late_score": v, "best_unit_id": f"{sig}-{k}", "units": 1,
                            "tokens": 5} if v is not None else {"status": "NO_TOKENS", "late_score": None})
                       for k, v in results.items()}, "mlateon", sig, {}, {}, tokens)


def test_best_translation_takes_the_max_of_normalised_maxsim():
    original = _lr({"X": 8.0, "Y": 4.0, "W": 4.0, "Z": None}, 4, "o")
    x1 = _lr({"X": 3.0, "Y": 3.0, "W": 2.0, "Z": None}, 2, "t")
    results, record = best_translation(original, [("x1", x1)])
    assert results["X"]["late_score"] == 2.0 and results["X"]["late_query"] == "original"     # 8/4 > 3/2
    assert results["Y"]["late_score"] == 1.5 and results["Y"]["late_query"] == "x1"           # 4/4 < 3/2
    assert results["Y"]["best_unit_id"] == "t-Y" and results["Y"]["late_query_scores"] == {"original": 1.0, "x1": 1.5}
    assert results["W"]["late_query"] == "original"                                          # a tie: the original
    assert results["Z"]["status"] == "NO_TOKENS" and "late_query" not in results["Z"]
    assert record["status"] == "APPLIED" and record["best_counts"] == {"original": 2, "x1": 1}
    assert [q["tokens"] for q in record["queries"]] == [4, 2] and original.results["Y"]["late_score"] == 4.0
    results, record = best_translation(original, [("x1", _lr({"X": 3.0}, None))])
    assert record["status"] == "NOT_NORMALISABLE" and results is original.results


def test_best_translation_end_to_end():
    late = {"альфа": {A1: 4.0, A2: 2.0, A3: 2.4, B1: 1.2, C1: 0.8},          # 4 tokens: 1.0 .5 .6 .3 .2
            "бета": {A1: 1.0, A2: 0.2, A3: 0.2, B1: 1.8, C1: 1.0}}           # 2 tokens: .5 .1 .1 .9 .5
    e = _embed(late, {"альфа": 4, "бета": 2})
    out = _search(embed=e, expansions=("бета",), late_query="best_translation")
    assert [b["query"] for p, b in e.calls if p == "/search/late"] == ["альфа", "бета"]
    # max: A1 1.0 (orig), B1 0.9 (бета), A3 0.6, then A2 0.5 / C1 0.5 tie → RRF order (C1 fused 3rd, A2 4th)
    assert _ids(out) == [A1, B1, A3, C1, A2]
    t = {h["id"]: h["trace"] for h in out["hits"]}
    assert t[B1]["late_query"] == {"best": "x1", "scores": {"original": 0.3, "x1": 0.9}}
    assert t[B1]["late_score"] == 0.9 and t[B1]["late_unit"]["unit_id"] == f"бета:{B1}"
    assert t[A1]["late_query"]["best"] == "original"
    lq = out["stages"]["late"]["late_query"]
    assert lq["status"] == "APPLIED" and lq["best_counts"] == {"original": 3, "x1": 2}
    assert [(q["query"], q["tokens"], q.get("text")) for q in lq["queries"]] == [("original", 4, None),
                                                                                  ("x1", 2, "бета")]
    assert "every expansion" in out["stages"]["expansion"]["fusion"] and "late_x1" in out["timings_ms"]
    plain = _search(embed=_embed(late, {"альфа": 4, "бета": 2}), expansions=("бета",))
    assert _ids(plain) == [A1, A3, A2, B1, C1]                                   # today: the original query only


def test_best_translation_without_a_translation_changes_nothing():
    e = _embed(LATE_A)
    out = _search(embed=e, late_query="best_translation")
    assert [p for p, _b in e.calls].count("/search/late") == 1
    assert out["stages"]["late"]["late_query"]["status"] == "NO_TRANSLATION"
    assert _ids(out) == _ids(_search(embed=_embed(LATE_A)))
    assert all("late_query" not in h["trace"] for h in out["hits"])


def test_every_formulation_runs_with_the_same_flags():
    e = _embed(LATE_A)
    out = _search(embed=e, expansions=("дельта",), formulations=({"text": "бета", "expansions": ("гамма",)},),
                  late_fusion="rrf", late_query="best_translation")
    assert [b["query"] for p, b in e.calls if p == "/search/late"] == ["альфа", "дельта", "бета", "гамма"]
    assert out["stages"]["formulations"]["status"] == "APPLIED"
    assert out["stages"]["late"]["fusion"]["mode"] == "rrf"                        # the query's run (base stages)
    assert all("late_fusion_rank" in h["trace"] and "late_query" in h["trace"] for h in out["hits"])


# ------------------------------------------------------------------------------------------------ API, MCP, brief
@pytest.fixture()
def api(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")
    pytest.importorskip("PIL")
    pytest.importorskip("pytz")
    from fastapi.testclient import TestClient

    from vkm_corpus.api.app import ApiConfig, create_app
    from vkm_corpus.api.fixtures import FakeHybrid, hybrid_hits, synthetic_service

    service, canon, _fakes = synthetic_service(tmp_path)
    service.deps.hybrid = FakeHybrid(hybrid_hits(canon))
    token = "read-token-for-late-flag-tests-000000000"
    app = create_app(service, ApiConfig(read_tokens={token: "read"}))
    return TestClient(app), app, service, {"Authorization": f"Bearer {token}"}, token


def test_api_passes_the_flags_only_when_set(api):
    from vkm_corpus.api.errors import ApiFailure

    client, _app, service, h, _token = api
    r = client.post("/v1/search/hybrid", headers=h, json={"query": "мульда", "late": True, "late_fusion": "rrf",
                                                          "late_query": "best_translation"})
    assert r.status_code == 200, r.text
    sent = service.deps.hybrid.requests[-1]
    assert sent["late_fusion"] == "rrf" and sent["late_query"] == "best_translation"
    client.post("/v1/search/hybrid", headers=h, json={"query": "мульда", "late": True})
    assert not {"late_fusion", "late_query"} & set(service.deps.hybrid.requests[-1])     # default request unchanged
    client.post("/v1/search/hybrid", headers=h, json={"query": "мульда", "late_fusion": "replace",
                                                      "late_query": "original"})
    assert not {"late_fusion", "late_query"} & set(service.deps.hybrid.requests[-1])
    for bad in ({"late_fusion": "mix"}, {"late_query": "en"}):
        resp = client.post("/v1/search/hybrid", headers=h, json={"query": "x", **bad})
        assert resp.status_code == 400 and resp.json()["error"]["code"] == "INVALID_ARGUMENT"
    with pytest.raises(ApiFailure) as exc:
        service.search_hybrid("x", ["PAGE"], {}, 5, None, late_query="en")
    assert exc.value.code == "INVALID_ARGUMENT"


def test_mcp_search_hybrid_and_retrieval_trace_pass_the_flags(api):
    pytest.importorskip("mcp")
    from mcp import Client

    from vkm_corpus.mcp.api_client import ApiClient
    from vkm_corpus.mcp.servers import build_read_server

    _client, app, service, _h, token = api
    server = build_read_server(ApiClient("http://vkm-api", token, transport=httpx.ASGITransport(app=app)))

    async def run():
        async with Client(server) as c:
            tools = {t.name: t for t in (await c.list_tools()).tools}
            await c.call_tool("search_hybrid", {"query": "x", "late": True, "late_fusion": "rrf",
                                                "late_query": "best_translation"})
            a = service.deps.hybrid.requests[-1]
            await c.call_tool("search_hybrid", {"query": "x"})
            b = service.deps.hybrid.requests[-1]
            await c.call_tool("retrieval_trace", {"query": "x", "late": True, "late_fusion": "rrf"})
            return tools, a, b, service.deps.hybrid.requests[-1]

    tools, a, b, trace = asyncio.run(run())
    for name in ("search_hybrid", "retrieval_trace"):
        props = tools[name].input_schema["properties"]
        assert "late_fusion" in props and "late_query" in props
    assert a["late_fusion"] == "rrf" and a["late_query"] == "best_translation"
    assert not {"late_fusion", "late_query"} & set(b) and trace["late_fusion"] == "rrf"


def test_brief_view_shows_the_flags():
    from vkm_corpus.api.brief import brief_response

    body = {"ok": True, "meta": {}, "item": {"record": {"stages": {"late": {"fusion": {"mode": "rrf"},
                                                                            "late_query": {"status": "APPLIED"}}}}},
            "items": [{"envelope": {"object_id": A1, "object_kind": "PAGE", "source_id": "VKM-SRC-001",
                                    "page_id": A1},
                       "record": {"rank": 1, "object_type": "PAGE",
                                  "trace": {"late_rank": 2, "late_score": 0.9, "late_fusion_rank": 1,
                                            "late_query": {"best": "x1", "scores": {"original": 0.3, "x1": 0.9}}}}}]}
    brief = brief_response(body, kind="hybrid")
    assert brief["run"]["stages"]["late"] == {"fusion": "rrf", "late_query": "APPLIED"}
    assert brief["items"][0]["late_fusion_rank"] == 1 and brief["items"][0]["late_query"] == "x1"

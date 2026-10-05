"""Opt-in flags of the hybrid search: several formulations of one question fused by RRF of their final orders,
the per-source cap over the final order (the overflow moves back, nothing is dropped) and the wider late pool. The
defaults keep today's path. Fake OpenSearch + fake RX580 service (httpx MockTransport); synthetic ids."""
from __future__ import annotations

import json

import pytest

httpx = pytest.importorskip("httpx")

from vkm_corpus.search import hybrid as H  # noqa: E402
from vkm_corpus.search.fakes import FakeOpenSearch  # noqa: E402
from vkm_corpus.search.hybrid import (  # noqa: E402
    EmbedClient,
    HybridError,
    HybridRequest,
    cap_per_source,
    fuse_formulations,
    hybrid_search,
)
from vkm_corpus.search.query import SearchRequestError  # noqa: E402

DIM = 4
A1, A2, A3 = (f"VKM-SRC-001:p000{i}" for i in range(1, 4))
B1, C1 = "VKM-SRC-002:p0001", "VKM-SRC-003:p0001"
META = {"build_id": "20261005t120000z-aaaaaaaa-bbbbbbbb", "built_from_snapshot_id": "SNAP-1", "dimension": DIM,
        "model_key": "granite-311m-r2", "config_signature": "c" * 64, "space_type": "innerproduct",
        "build_status": "COMPLETE"}
# BM25 pages per query word (the dense leg finds nothing: the order of a formulation is its BM25 order)
BM25 = {"альфа": [A1, A2, A3, B1, C1], "бета": [B1, A1, C1], "гамма": [C1], "дельта": [A2]}
STAGES = {"bm25", "dense", "late", "bib_route", "visual_route", "expansion", "graph", "rerank"}


def _client(bm25=None):
    table = bm25 or BM25

    def answer(index, body):
        if index != "vkm-pages":
            return []
        text = json.dumps(body, ensure_ascii=False)
        word = next((w for w in table if w in text), None)
        pages = table.get(word, [])
        return [{"_id": p, "_index": "vkm-pages-m1-b-test", "_score": 10.0 - i,
                 "_source": {"id": p, "object_type": "PAGE", "source_id": p.split(":")[0], "work_id": "VKM-WRK-001",
                             "page_id": p}} for i, p in enumerate(pages)]

    c = FakeOpenSearch(bm25=answer)
    name = f"vkm-vectors-m1-{META['build_id']}"
    c.indices.create(index=name, body={"mappings": {"_meta": dict(META)}})
    c.indices.update_aliases({"actions": [{"add": {"index": name, "alias": "vkm-vectors"}}]})
    return c


def _embed(fail=(), late=None):
    """Dense encoder (fails for the texts in ``fail``) and, with ``late``, /search/late scoring every target by
    ``late`` (id → score)."""
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append((request.url.path, body))
        if request.url.path == "/search/late":
            return httpx.Response(200, json={"model": "mlateon", "query_signature": "l" * 64, "n_query_tokens": 4,
                                             "store": {"snapshot_id": "SNAP-1"}, "timings_ms": {"maxsim": 1.0},
                                             "results": [{"id": t["id"], "kind": t["kind"], "status": "SCORED",
                                                          "late_score": late.get(t["id"], 1.0),
                                                          "best_unit_id": "u1", "units": 1, "tokens": 9}
                                                         for t in body["targets"]]})
        if body["text"] in fail:
            return httpx.Response(503, json={"detail": "busy"})
        return httpx.Response(200, json={"dense": {"model": "granite-311m-r2", "signature": "q" * 64,
                                                   "dimension": DIM, "vector": [1.0, 0.0, 0.0, 0.0]}})

    client = EmbedClient("http://rx580-retrieval:8790", "tok", transport=httpx.MockTransport(handler))
    client.calls = calls
    return client


def _ids(out):
    return [h["id"] for h in out["hits"]]


def _search(query, **kw):
    kw.setdefault("candidates", 10)
    kw.setdefault("late", False)
    return hybrid_search(_client(), kw.pop("embed", None) or _embed(), HybridRequest(query=query, **kw), "vkm")


# ------------------------------------------------------------------------------------------------ defaults
def test_defaults_keep_todays_answer():
    out = _search("альфа", size=5)
    assert _ids(out) == [A1, A2, A3, B1, C1] and out["fused_total"] == 5
    assert set(out["stages"]) == STAGES                         # no formulations / source_cap stage
    assert all("formulations" not in h["trace"] and "source_cap" not in h["trace"] for h in out["hits"])
    req = HybridRequest(query="x")
    req.validate()
    assert req.formulations == () and req.max_per_source is None and H.MAX_LATE_CANDIDATES == 300


# ------------------------------------------------------------------------------------------------ fusion
def test_fusion_of_final_orders_by_rrf_with_ties_to_the_earlier_formulation():
    order, score, ranks, origin = fuse_formulations([(0, ["X", "Y"]), (1, ["Y", "X"]), (2, ["Z"])], k=60)
    assert order == ["X", "Y", "Z"] and score["X"] == score["Y"]           # a tie: X came first (the query)
    assert ranks["Y"] == {"f0": 2, "f1": 1} and origin == {"X": 0, "Y": 0, "Z": 2}
    deep = fuse_formulations([(0, [f"k{i}" for i in range(80)]), (1, ["k70"])], k=60, depth=50)[0]
    assert "k70" in deep and "k60" not in deep                             # only the first 50 of each list count


def test_formulations_run_the_pipeline_each_and_fuse():
    e = _embed()
    out = _search("альфа", formulations=("гамма",), size=5, embed=e)
    # RRF(60): C1 = 1/65 + 1/61 beats A1 = 1/61; then A2, A3, B1 of the query alone
    assert _ids(out) == [C1, A1, A2, A3, B1] and out["fused_total"] == 5
    assert [b["text"] for p, b in e.calls if p == "/embed/query"] == ["альфа", "гамма"]   # the query first
    stage = out["stages"]["formulations"]
    assert stage["status"] == "APPLIED" and stage["answered"] == 2 and stage["depth"] == H.FUSION_DEPTH
    assert [(r["n"], r["origin"], r["status"]) for r in stage["runs"]] == [(0, "query", "OK"), (1, "caller", "OK")]
    c1 = out["hits"][0]
    assert c1["trace"]["formulations"] == {"fused_rank": 1, "rrf_score": round(1 / 65 + 1 / 61, 8),
                                           "ranks": {"f0": 5, "f1": 1}, "hit_from": "f0"}
    assert c1["rank"] == 1 and c1["score"] == round(1 / 65 + 1 / 61, 8) and c1["source_id"] == "VKM-SRC-003"
    assert {"f0", "f1", "fusion", "total"} <= set(out["timings_ms"])


def test_fused_list_pages_with_the_cursor():
    whole = _ids(_search("альфа", formulations=("гамма", "бета"), size=5))
    pages = [_ids(_search("альфа", formulations=("гамма", "бета"), size=2, offset=o)) for o in (0, 2, 4)]
    assert sum(pages, []) == whole and len(set(whole)) == 5


def test_late_stage_runs_per_formulation():
    e = _embed(late={A2: 9.0, C1: 1.0})
    out = _search("альфа", formulations=("гамма",), late=True, size=5, embed=e)
    assert [p for p, _b in e.calls].count("/search/late") == 2
    assert all(r["late"] for r in out["stages"]["formulations"]["runs"])
    assert out["hits"][0]["id"] == C1 and out["hits"][1]["id"] == A2          # A2 led the query's late order


def test_failed_formulation_is_traced_not_silent():
    out = _search("альфа", formulations=("сбой", "гамма"), size=5, embed=_embed(fail=("сбой",)))
    runs = out["stages"]["formulations"]["runs"]
    assert runs[1]["status"] == "FAILED" and runs[1]["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert runs[1]["error"]["stage"] == "embed" and "http://" not in json.dumps(runs[1])
    assert any(w.startswith("FORMULATION_FAILED: f1 DEPENDENCY_UNAVAILABLE") for w in out["warnings"])
    assert out["stages"]["formulations"]["status"] == "APPLIED" and _ids(out)[0] == C1     # the others fused
    alone = _search("альфа", formulations=("сбой",), size=5, embed=_embed(fail=("сбой",)))
    assert alone["stages"]["formulations"]["status"] == "NOT_APPLIED"                  # nothing to fuse
    assert _ids(alone) == [A1, A2, A3, B1, C1] and alone["fused_total"] == 5
    assert all("formulations" not in h["trace"] for h in alone["hits"])
    with pytest.raises(HybridError) as exc:                                             # the query's own failure
        _search("сбой", formulations=("альфа",), embed=_embed(fail=("сбой",)))
    assert exc.value.code == "DEPENDENCY_UNAVAILABLE"


def test_duplicates_and_budget_are_skipped_with_a_trace(monkeypatch):
    out = _search("альфа", formulations=("Альфа ", "гамма", "ГАММА"), size=5)
    assert [r["status"] for r in out["stages"]["formulations"]["runs"]] == ["OK", "SKIPPED_DUPLICATE", "OK",
                                                                             "SKIPPED_DUPLICATE"]
    monkeypatch.setattr(H, "FUSION_BUDGET_S", -1.0)
    late = _search("альфа", formulations=("гамма",), size=5)
    assert late["stages"]["formulations"]["runs"][1]["status"] == "SKIPPED_BUDGET"
    assert any(w.startswith("FORMULATION_SKIPPED_BUDGET: f1") for w in late["warnings"])
    assert _ids(late) == [A1, A2, A3, B1, C1]


def test_duplicate_pages_of_other_formulations_merge_into_the_first_representative():
    client = _client({"альфа": [A1, A2], "бета": [A3]})
    first = client.bm25

    def with_dups(index, body):                       # E collapsed A3 into A1's group for the query
        hits = first(index, body)
        if "альфа" in json.dumps(body, ensure_ascii=False):
            hits[0]["inner_hits"] = {"duplicates": {"hits": {"hits": [{"_id": A1}, {"_id": A3}]}}}
        return hits

    client.bm25 = with_dups
    out = hybrid_search(client, _embed(), HybridRequest(query="альфа", formulations=("бета",), candidates=10,
                                                        late=False), "vkm")
    assert _ids(out) == [A1, A2] and out["stages"]["formulations"]["duplicates_merged"] == 1
    assert out["hits"][0]["trace"]["formulations"]["ranks"] == {"f0": 1, "f1": 1}


def test_formulation_validation():
    for bad in (("a",) * 5, ("x" * 513,), ("",), ({"text": "a", "expansions": ("b", "c", "d")},), (7,)):
        with pytest.raises(SearchRequestError) as exc:
            HybridRequest(query="q", formulations=bad).validate()
        assert exc.value.code == "E_BAD_QUERY"
    req = HybridRequest(query="q", formulations=("  a   b ", {"text": "c", "origin": "translation",
                                                                 "expansions": ["d"]}))
    req.validate()
    req.validate()                                                        # idempotent
    assert req.formulations == ({"text": "a b", "origin": "caller", "expansions": ()},
                                {"text": "c", "origin": "translation", "expansions": ("d",)})


# ------------------------------------------------------------------------------------------------ per-source cap
def test_cap_per_source_moves_the_overflow_back():
    keys = ["a1", "a2", "a3", "b1", "a4", "c1", "x"]
    src = {"a1": "A", "a2": "A", "a3": "A", "a4": "A", "b1": "B", "c1": "C"}
    order, over = cap_per_source(keys, src.get, 2)
    assert order == ["a1", "a2", "b1", "c1", "x", "a3", "a4"] and sorted(order) == sorted(keys)
    assert over == {"a3": {"round": 1, "rank_before_cap": 3}, "a4": {"round": 1, "rank_before_cap": 5}}
    assert cap_per_source(keys, src.get, 1)[0] == ["a1", "b1", "c1", "x", "a2", "a3", "a4"]


def test_max_per_source_on_the_final_order_keeps_every_hit_reachable():
    out = _search("альфа", max_per_source=1, size=3)
    assert _ids(out) == [A1, B1, C1] and out["fused_total"] == 5
    rest = _search("альфа", max_per_source=1, size=3, offset=3)
    assert _ids(rest) == [A2, A3]                                        # moved back, not dropped
    assert rest["hits"][0]["trace"]["source_cap"] == {"round": 1, "rank_before_cap": 2}
    assert rest["hits"][1]["trace"]["source_cap"] == {"round": 2, "rank_before_cap": 3}
    stage = out["stages"]["source_cap"]
    assert (stage["max_per_source"], stage["over_cap"], stage["moved"], stage["sources_over_cap"]) == (1, 2, 4, 1)
    assert "formulations" not in out["stages"]
    for bad in (0, 51, True):
        with pytest.raises(SearchRequestError) as exc:
            HybridRequest(query="q", max_per_source=bad).validate()
        assert exc.value.code == "E_BAD_SIZE"


def test_cap_applies_after_the_fusion():
    out = _search("альфа", formulations=("гамма",), max_per_source=1, size=5)
    # fused: C1, A1, A2, A3, B1 → one page per source first: C1, A1, B1, then A2, A3
    assert _ids(out) == [C1, A1, B1, A2, A3]
    assert out["stages"]["source_cap"]["applied_to"] == "the fused list of the formulations"
    assert out["hits"][3]["trace"]["source_cap"] == {"round": 1, "rank_before_cap": 3}
    a2 = out["hits"][3]
    assert a2["trace"]["formulations"]["fused_rank"] == 3 and a2["trace"]["final_rank"] == 4 and a2["rank"] == 4


# ------------------------------------------------------------------------------------------------ late pool
def test_wider_late_pool_reaches_the_service():
    e = _embed(late={})
    out = _search("альфа", late=True, late_candidates=300, size=5, embed=e)
    assert out["late_candidates"] == 300 and out["stages"]["late"]["candidates"] == 5   # every fused key
    with pytest.raises(SearchRequestError):
        HybridRequest(query="q", late_candidates=301).validate()

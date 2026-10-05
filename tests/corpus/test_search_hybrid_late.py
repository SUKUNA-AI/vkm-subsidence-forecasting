"""Late interaction stage of hybrid search (agent L): the RRF top-N re-scored by MaxSim on the RX580 service, ordering
per kind (H-44), unscored candidates after the scored ones, trace fields, and loud failures (never an RRF-only answer
in disguise). Fake OpenSearch + fake RX580 service (httpx MockTransport)."""
from __future__ import annotations

import json

import pytest

httpx = pytest.importorskip("httpx")

from vkm_corpus.search.fakes import FakeOpenSearch  # noqa: E402
from vkm_corpus.search.hybrid import (  # noqa: E402
    EmbedClient,
    HybridError,
    HybridRequest,
    hybrid_search,
    late_order,
)
from vkm_corpus.search.query import SearchRequestError  # noqa: E402

DIM = 4
Q = [1.0, 0.0, 0.0, 0.0]
P1, P2, P3, P5 = "VKM-SRC-001:p0001", "VKM-SRC-001:p0002", "VKM-SRC-001:p0003", "VKM-SRC-001:p0005"
F1 = "VKM-SRC-001:p0001:f000000000001"
META = {"build_id": "20260928t120000z-aaaaaaaa-bbbbbbbb", "built_from_snapshot_id": "SNAP-1", "dimension": DIM,
        "model_key": "granite-311m-r2", "config_signature": "c" * 64, "space_type": "innerproduct",
        "build_status": "COMPLETE"}
STORE = {"pack_id": "SNAP-1-0123456789ab", "snapshot_id": "SNAP-1", "config_signature": "d" * 64, "count": 4}


def _unit(uid, page, kind="BLOCK_GROUP", objects=None, vec=None):
    return uid, {"id": uid, "unit_kind": kind, "object_ids": objects or [f"{page}:b{uid[-12:]}"], "page_id": page,
                 "source_id": page.split(":")[0], "work_id": "VKM-WRK-001", "dup_group_id": page, "vector": vec}


def _client():
    bm = {"vkm-pages": [("pages", P2, 9.0, P2), ("pages", P5, 5.0, P5), ("pages", P1, 3.0, P1)],
          "vkm-figures": [("figures", F1, 4.0, P1)]}

    def answer(index, body):
        return [{"_id": oid, "_index": f"vkm-{t}-m1-b-test", "_score": s,
                 "_source": {"id": oid, "object_type": t[:-1].upper(), "source_id": "VKM-SRC-001",
                             "work_id": "VKM-WRK-001", "page_id": page}} for t, oid, s, page in bm.get(index, [])]

    c = FakeOpenSearch(bm25=answer)
    name = f"vkm-vectors-m1-{META['build_id']}"
    c.indices.create(index=name, body={"mappings": {"_meta": dict(META)}})
    for uid, doc in (_unit("u1-000000000001", P1, vec=[0.2, 0.9, 0.0, 0.0]),
                     _unit("u1-000000000002", P1, "FIGURE", [F1], vec=[0.99, 0.1, 0.0, 0.0]),
                     _unit("u1-000000000003", P2, vec=[0.9, 0.3, 0.0, 0.0]),
                     _unit("u1-000000000004", P3, vec=[0.5, 0.5, 0.5, 0.0])):
        c.indices_[name]["docs"][uid] = doc
    c.indices.update_aliases({"actions": [{"add": {"index": name, "alias": "vkm-vectors"}}]})
    return c


def _service(late_scores=None, *, late_status=200, late_body=None, store=None):
    """Fake RX580 service: /embed/query (dense) and /search/late (targets → the given late scores)."""
    calls = []
    scores = late_scores or {}

    def handler(request):
        body = json.loads(request.content)
        calls.append((request.url.path, body))
        if request.url.path == "/embed/query":
            return httpx.Response(200, json={"dense": {"model": "granite-311m-r2", "signature": "q" * 64,
                                                       "n_tokens": 5, "encode_ms": 9.5, "dimension": DIM,
                                                       "vector": Q}})
        if late_status != 200:
            return httpx.Response(late_status, json={"detail": "late-interaction token store MISSING: no pack"})
        if late_body is not None:
            return httpx.Response(200, json=late_body)
        results = []
        for t in body["targets"]:
            s = scores.get(t["id"])
            results.append({"id": t["id"], "kind": t["kind"], "status": "SCORED" if s is not None else "NO_TOKENS",
                            "late_score": s, "best_unit_id": f"unit-of-{t['id']}" if s is not None else None,
                            "units": 2 if s is not None else 0, "tokens": 40 if s is not None else 0})
        return httpx.Response(200, json={"mode": "late-targets", "model": "mlateon", "query_signature": "l" * 64,
                                         "n_query_tokens": 12, "store": store or STORE,
                                         "timings_ms": {"late_encode": 11.0, "maxsim": 3.2},
                                         "targets": len(results), "results": results})

    client = EmbedClient("http://rx580-retrieval:8790", "tok", transport=httpx.MockTransport(handler))
    client.calls = calls
    return client


def test_late_stage_reorders_pages_by_maxsim_with_a_full_trace():
    # RRF order (see test_search_hybrid): P2, P1, P5, P3; late: P3 best, P5 has no token vectors
    svc = _service({P2: 10.0, P1: 12.5, P3: 14.0})
    out = hybrid_search(_client(), svc, HybridRequest(query="оседание", candidates=10, late=True), "vkm")
    assert [h["id"] for h in out["hits"]] == [P3, P1, P2, P5]
    path, body = svc.calls[-1]
    assert path == "/search/late" and body["targets"] == [{"id": P2, "kind": "PAGE"}, {"id": P1, "kind": "PAGE"},
                                                           {"id": P5, "kind": "PAGE"}, {"id": P3, "kind": "PAGE"}]
    t = {h["id"]: h["trace"] for h in out["hits"]}
    assert (t[P3]["late_rank"], t[P3]["late_score"], t[P3]["fused_rank"], t[P3]["final_rank"]) == (1, 14.0, 4, 1)
    assert t[P3]["late_unit"] == {"unit_id": f"unit-of-{P3}", "units": 2, "tokens": 40}
    assert t[P5]["late_status"] == "NO_TOKENS" and t[P5]["late_rank"] is None and t[P5]["final_rank"] == 4
    assert out["hits"][0]["rank"] == 1 and out["hits"][0]["score"] == t[P3]["rrf_score"]     # score stays RRF
    late = out["stages"]["late"]
    assert late["model_key"] == "mlateon" and late["candidates"] == 4 and late["scored"] == 3
    assert late["unscored_ids"] == [P5] and late["store"]["pack_id"] == STORE["pack_id"]
    assert out["late"] is True and out["late_candidates"] == 100 and "late" in out["timings_ms"]
    assert not out["warnings"]


def test_candidates_beyond_late_candidates_keep_rrf_order_and_pagination():
    svc = _service({P2: 1.0, P1: 9.0, P5: 5.0, P3: 99.0})
    out = hybrid_search(_client(), svc, HybridRequest(query="x", candidates=10, late=True, late_candidates=2), "vkm")
    assert [h["id"] for h in out["hits"]] == [P1, P2, P5, P3]            # only P2, P1 re-scored; P3 not a candidate
    assert out["hits"][3]["trace"]["late_status"] == "NOT_CANDIDATE" and len(svc.calls[-1][1]["targets"]) == 2
    page2 = hybrid_search(_client(), _service({P2: 1.0, P1: 9.0}), HybridRequest(
        query="x", candidates=10, late=True, late_candidates=2, size=2, offset=2), "vkm")
    assert [h["id"] for h in page2["hits"]] == [P5, P3] and page2["hits"][0]["rank"] == 3


def test_two_kinds_keep_their_rrf_positions_and_reorder_inside_them():
    fused = [("A", 0.5), ("F", 0.4), ("B", 0.3), ("G", 0.2), ("C", 0.1)]
    kind = {"A": "PAGE", "B": "PAGE", "C": "PAGE", "F": "FIGURE", "G": "FIGURE"}
    res = {"A": {"status": "SCORED", "late_score": 1.0}, "B": {"status": "NO_TOKENS"},
           "C": {"status": "SCORED", "late_score": 3.0}, "F": {"status": "SCORED", "late_score": 1.0},
           "G": {"status": "SCORED", "late_score": 50.0}}
    order, ranks = late_order(fused, kind, res, 5)
    # pages keep positions 1, 3, 5 (C, A, then unscored B); figures 2, 4 (G, F); a figure's 50 never outranks a page
    assert [k for k, _s in order] == ["C", "G", "A", "F", "B"]
    assert ranks == {"C": 1, "A": 2, "G": 1, "F": 2}
    out = hybrid_search(_client(), _service({P2: 1.0, P1: 2.0, F1: 7.0}),
                        HybridRequest(query="x", kinds=("PAGE", "FIGURE"), candidates=10, late=True), "vkm")
    kinds = [h["object_type"] for h in out["hits"]]
    plain = hybrid_search(_client(), _service(), HybridRequest(query="x", kinds=("PAGE", "FIGURE"), candidates=10,
                                                               late=False), "vkm")
    assert kinds == [h["object_type"] for h in plain["hits"]]            # the kind interleaving of RRF is kept
    assert next(h for h in out["hits"] if h["id"] == F1)["trace"]["late_rank"] == 1


def test_late_off_makes_no_late_call_and_default_is_explicit():
    svc = _service({P1: 1.0})
    out = hybrid_search(_client(), svc, HybridRequest(query="x", candidates=10, late=False), "vkm")
    assert [p for p, _b in svc.calls] == ["/embed/query"] and "NOT_RUN" in out["stages"]["late"]
    assert all(h["trace"]["late_rank"] is None and "late_status" not in h["trace"] for h in out["hits"])
    req = HybridRequest(query="x")
    req.validate()
    from vkm_corpus.search.hybrid import LATE_DEFAULT

    assert req.late is LATE_DEFAULT


@pytest.mark.parametrize("case,code", [("store_missing", "DEPENDENCY_UNAVAILABLE"),
                                       ("no_late_model", "DEPENDENCY_UNAVAILABLE"),
                                       ("down", "DEPENDENCY_UNAVAILABLE"),
                                       ("unset", "DEPENDENCY_UNAVAILABLE"),
                                       ("old_contract", "DEPENDENCY_ERROR"),
                                       ("other_targets", "DEPENDENCY_ERROR"),
                                       ("token", "DEPENDENCY_ERROR")])
def test_late_failures_are_loud(case, code):
    if case == "store_missing":
        svc = _service(late_status=503)
    elif case == "no_late_model":
        svc = _service(late_status=404)
    elif case == "token":
        svc = _service(late_status=401)
    elif case == "old_contract":                               # a service without targets answers hybrid hits
        svc = _service(late_body={"mode": "late", "hits": []})
    elif case == "other_targets":
        svc = _service(late_body={"results": [{"id": "elsewhere", "status": "SCORED", "late_score": 1.0}]})
    elif case == "down":
        def handler(request):
            if request.url.path == "/embed/query":
                return httpx.Response(200, json={"dense": {"model": "granite-311m-r2", "vector": Q}})
            raise httpx.ConnectError("refused")
        svc = EmbedClient("http://rx580-retrieval:8790", None, transport=httpx.MockTransport(handler))
    else:
        svc = EmbedClient(None)
    if case == "unset":
        with pytest.raises(HybridError) as exc:
            svc.late_scores("x", [{"id": P1, "kind": "PAGE"}])
    else:
        with pytest.raises(HybridError) as exc:
            hybrid_search(_client(), svc, HybridRequest(query="оседание", candidates=10, late=True), "vkm")
    assert exc.value.code == code and exc.value.stage == "late" and exc.value.tool == "rx580-retrieval"
    assert "http://" not in exc.value.message


def test_snapshot_mismatch_of_the_pack_is_a_warning_and_validation():
    svc = _service({P1: 1.0}, store={**STORE, "snapshot_id": "SNAP-0"})
    out = hybrid_search(_client(), svc, HybridRequest(query="x", candidates=10, late=True), "vkm")
    assert any(w.startswith("LATE_STORE_SNAPSHOT_MISMATCH") for w in out["warnings"])
    for bad in (0, 301):                                       # the RX580 service takes ≤ 1000 targets: pool ≤ 300
        with pytest.raises(SearchRequestError) as exc:
            HybridRequest(query="x", late=True, late_candidates=bad).validate()
        assert exc.value.code == "E_BAD_SIZE"
    HybridRequest(query="x", late=True, late_candidates=300).validate()


def test_bibliography_units_never_rank_pages_cp42():
    """CP-42: a BIB_ENTRY unit (here the best dense match, on a page no other leg finds) does not bring its page
    into the PAGE ranking; the late stage asks for pages without BIB_ENTRY units; object kinds are unaffected."""
    c = _client()
    name = f"vkm-vectors-m1-{META['build_id']}"
    c.indices_[name]["docs"]["u1-00000000000b"] = _unit("u1-00000000000b", "VKM-SRC-001:p0009", "BIB_ENTRY",
                                                        ["VKM-SRC-001:p0009:r001"], vec=[1.0, 0.0, 0.0, 0.0])[1]
    svc = _service({P2: 1.0, P1: 2.0, P5: 3.0, P3: 4.0})
    out = hybrid_search(c, svc, HybridRequest(query="x", candidates=10, late=True), "vkm")
    assert "VKM-SRC-001:p0009" not in [h["id"] for h in out["hits"]]
    page_knn = next(b for _i, b in c.searches if "knn" in b["query"])["query"]["knn"]["vector"]
    assert {"terms": {"unit_kind": ["BIB_ENTRY"]}} in page_knn["filter"]["bool"]["must_not"]
    assert svc.calls[-1][1]["page_exclude_kinds"] == ["BIB_ENTRY"] and "CP-42" in out["stages"]["late"]["page_score"]
    fig = _client()
    hybrid_search(fig, _service({F1: 1.0}), HybridRequest(query="x", kinds=("FIGURE",), candidates=10, late=True),
                  "vkm")
    fig_knn = next(b for _i, b in fig.searches if "knn" in b["query"])["query"]["knn"]["vector"]
    assert "must_not" not in fig_knn["filter"]["bool"] or not fig_knn["filter"]["bool"]["must_not"]


def test_health_reports_the_late_store():
    def handler(request):
        return httpx.Response(200, json={"status": "ok", "models": [{"role": "late", "key": "mlateon"}],
                                         "late_store": {"status": "READY", "pack_id": "p1", "count": 5,
                                                        "releases": 3}})

    h = EmbedClient("http://rx580-retrieval:8790", None, transport=httpx.MockTransport(handler)).health()
    assert h["late_store"] == {"status": "READY", "pack_id": "p1", "count": 5}

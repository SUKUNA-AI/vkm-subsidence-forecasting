"""Hybrid retrieval (task §52, §54): BM25 (E's query path, unchanged) + dense k-NN over the vectors alias, RRF, stage
trace; failures of the encoder or the vector index never fall back to BM25. Fake OpenSearch + fake RX580 service."""
from __future__ import annotations

import json

import pytest

httpx = pytest.importorskip("httpx")

from vkm_corpus.search.fakes import FakeOpenSearch  # noqa: E402
from vkm_corpus.search.hybrid import EmbedClient, HybridError, HybridRequest, hybrid_search  # noqa: E402
from vkm_corpus.search.query import SearchRequestError  # noqa: E402

DIM = 4
Q = [1.0, 0.0, 0.0, 0.0]
P1, P2, P3, P4, P5 = (f"VKM-SRC-001:p000{i}" for i in range(1, 6))
F1 = "VKM-SRC-001:p0001:f000000000001"
META = {"build_id": "20260928t120000z-aaaaaaaa-bbbbbbbb", "built_from_snapshot_id": "SNAP-1", "dimension": DIM,
        "model_key": "granite-311m-r2", "config_signature": "c" * 64, "space_type": "innerproduct",
        "build_status": "COMPLETE"}


def _unit(uid, page, kind="BLOCK_GROUP", objects=None, vec=None, **extra):
    return uid, {"id": uid, "unit_kind": kind, "object_ids": objects or [f"{page}:b{uid[-12:]}"], "page_id": page,
                 "source_id": page.split(":")[0], "work_id": "VKM-WRK-001", "dup_group_id": extra.pop("dup", page),
                 "vector": vec, **extra}


def _bm25_hit(index_type, oid, score, page):
    return {"_id": oid, "_index": f"vkm-{index_type}-m1-b-test", "_score": score,
            "_source": {"id": oid, "object_type": index_type[:-1].upper(), "source_id": "VKM-SRC-001",
                        "work_id": "VKM-WRK-001", "page_id": page}}


def _client(meta=META, units=None, bm25=None, alias=True):
    bm = bm25 if bm25 is not None else {"vkm-pages": [("pages", P2, 9.0, P2), ("pages", P5, 5.0, P5),
                                                      ("pages", P1, 3.0, P1)],
                                        "vkm-figures": [("figures", F1, 4.0, P1)]}

    def answer(index, body):
        return [_bm25_hit(t, oid, s, page) for t, oid, s, page in bm.get(index, [])]

    c = FakeOpenSearch(bm25=answer)
    name = f"vkm-vectors-m1-{meta['build_id']}"
    c.indices.create(index=name, body={"mappings": {"_meta": dict(meta)}})
    for uid, doc in (units if units is not None else [
            _unit("u1-000000000001", P1, vec=[0.2, 0.9, 0.0, 0.0]),
            _unit("u1-000000000002", P1, "FIGURE", [F1], vec=[0.99, 0.1, 0.0, 0.0]),     # best unit of P1
            _unit("u1-000000000003", P2, vec=[0.9, 0.3, 0.0, 0.0]),
            _unit("u1-000000000004", P3, vec=[0.5, 0.5, 0.5, 0.0])]):
        c.indices_[name]["docs"][uid] = doc
    if alias:
        c.indices.update_aliases({"actions": [{"add": {"index": name, "alias": "vkm-vectors"}}]})
    return c


def _embed(vector=Q, model="granite-311m-r2", status=200, exc=None):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        if exc is not None:
            raise exc
        body = {"layer": "SERVICE", "dense": {"model": model, "signature": "q" * 64, "n_tokens": 5, "encode_ms": 9.5,
                                              "dimension": len(vector), "vector": vector}}
        return httpx.Response(status, json=body if status == 200 else {"detail": "x"})

    client = EmbedClient("http://rx580-retrieval:8790", "tok", transport=httpx.MockTransport(handler))
    client.calls = calls
    return client


def test_rrf_fuses_page_keys_with_a_full_trace():
    c, e = _client(), _embed()
    out = hybrid_search(c, e, HybridRequest(query="оседание земной поверхности", candidates=10), "vkm")
    assert e.calls == [{"text": "оседание земной поверхности", "role": "dense", "include_vectors": True}]
    ids = [h["id"] for h in out["hits"]]
    # BM25 pages: P2, P5, P1; dense pages (first unit per page): P1 (figure unit), P2, P3 → RRF k=60
    assert ids == [P2, P1, P5, P3]
    t = {h["id"]: h["trace"] for h in out["hits"]}
    assert (t[P2]["bm25_rank"], t[P2]["dense_rank"], t[P2]["fused_rank"]) == (1, 2, 1)
    assert (t[P1]["bm25_rank"], t[P1]["dense_rank"], t[P1]["fused_rank"]) == (3, 1, 2)
    assert t[P5]["dense_rank"] is None and t[P3]["bm25_rank"] is None
    assert t[P1]["dense_unit"] == {"unit_id": "u1-000000000002", "unit_kind": "FIGURE", "object_ids": [F1]}
    assert abs(t[P2]["rrf_score"] - (1 / 61 + 1 / 62)) < 1e-7 and t[P2]["late_rank"] is None
    hit = out["hits"][0]
    assert hit["object_type"] == "PAGE" and hit["page_id"] == P2 and hit["index"].startswith("vkm-pages-")
    dense_only = out["hits"][3]
    assert dense_only["index"] == "vkm-vectors-m1-" + META["build_id"] and dense_only["build_id"] == META["build_id"]
    stages = out["stages"]["dense"]
    assert stages["model_key"] == stages["query_model"] == "granite-311m-r2" and stages["dimension"] == DIM
    assert out["fusion"] == "RRF" and out["rrf_k"] == 60 and "NOT_RUN" in out["stages"]["late"]
    knn = next(b for _i, b in c.searches if "knn" in b["query"])
    assert knn["query"]["knn"]["vector"]["k"] == 30                      # pages: 3 units fetched per wanted page


def test_object_kinds_fuse_on_object_ids_and_filters_reach_both_legs():
    c, e = _client(), _embed()
    out = hybrid_search(c, e, HybridRequest(query="мульда", kinds=("FIGURE",), candidates=10,
                                            filters={"source_id": ["VKM-SRC-001"]}), "vkm")
    assert [h["id"] for h in out["hits"]] == [F1]
    assert out["hits"][0]["trace"]["bm25_rank"] == 1 and out["hits"][0]["trace"]["dense_rank"] == 1
    bm25_body = next(b for i, b in c.searches if i == "vkm-figures")
    knn = next(b for _i, b in c.searches if "knn" in b["query"])["query"]["knn"]["vector"]
    assert {"terms": {"source_id": ["VKM-SRC-001"]}} in bm25_body["query"]["bool"]["filter"]
    assert {"terms": {"source_id": ["VKM-SRC-001"]}} in knn["filter"]["bool"]["filter"]
    assert {"terms": {"unit_kind": ["FIGURE"]}} in knn["filter"]["bool"]["filter"]


def test_duplicate_pages_collapse_on_the_dense_side_unless_asked():
    units = [_unit("u1-000000000001", P3, vec=[0.9, 0.1, 0.0, 0.0], dup="G1"),
             _unit("u1-000000000002", P4, vec=[0.8, 0.2, 0.0, 0.0], dup="G1")]
    c = _client(units=units, bm25={})
    ids = [h["id"] for h in hybrid_search(c, _embed(), HybridRequest(query="x", candidates=10), "vkm")["hits"]]
    assert ids == [P3]
    ids = [h["id"] for h in hybrid_search(c, _embed(), HybridRequest(query="x", candidates=10,
                                                                     include_duplicates=True), "vkm")["hits"]]
    assert ids == [P3, P4]


@pytest.mark.parametrize("case,code,stage", [
    ("no_alias", "DEPENDENCY_UNAVAILABLE", "vectors"),
    ("incomplete", "DEPENDENCY_UNAVAILABLE", "vectors"),
    ("embed_down", "DEPENDENCY_UNAVAILABLE", "embed"),
    ("embed_unset", "DEPENDENCY_UNAVAILABLE", "embed"),
    ("embed_token", "DEPENDENCY_ERROR", "embed"),
    ("dimension", "DEPENDENCY_ERROR", "embed"),
    ("model", "DEPENDENCY_ERROR", "embed"),
])
def test_missing_prerequisites_fail_without_a_bm25_fallback(case, code, stage):
    c = _client(alias=case != "no_alias", meta={**META, "build_status": "BUILDING"} if case == "incomplete" else META)
    e = {"embed_down": _embed(exc=httpx.ConnectError("refused")), "embed_unset": EmbedClient(None),
         "embed_token": _embed(status=401), "dimension": _embed(vector=[1.0, 0.0]),
         "model": _embed(model="jina-v5-nano")}.get(case, _embed())
    with pytest.raises(HybridError) as exc:
        hybrid_search(c, e, HybridRequest(query="оседание"), "vkm")
    assert (exc.value.code, exc.value.stage) == (code, stage)
    assert not [i for i, _b in c.searches]                                # no BM25 query was made


@pytest.mark.parametrize("kw,code", [({"kinds": ("BLOCK",)}, "E_BAD_KIND"),
                                     ({"filters": {"figure_type": ["MAP"]}}, "E_BAD_FILTER"),
                                     ({"filters": {"bogus": 1}}, "E_UNKNOWN_FILTER"),
                                     ({"candidates": 500}, "E_BAD_SIZE"),
                                     ({"query": " "}, "E_BAD_QUERY")])
def test_request_validation(kw, code):
    req = HybridRequest(**{"query": "оседание", **kw})
    with pytest.raises(SearchRequestError) as exc:
        req.validate()
    assert exc.value.code == code

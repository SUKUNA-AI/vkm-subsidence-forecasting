"""Query builder, hit parsing, rank fusion and rerank candidates (offline; recorded synthetic responses)."""
from __future__ import annotations

import pytest

from vkm_corpus.search import query as Q


def body(kind="pages", **kw):
    return Q.build_body(kind, Q.SearchRequest("оседание земной поверхности", **kw), size=10, offset=0)


def test_filters_are_whitelisted():
    with pytest.raises(Q.SearchRequestError) as info:
        Q.compile_filters({"site_scope": ["SKRU1"]})              # the object-level name is not a filter (H-18)
    assert info.value.code == "E_UNKNOWN_FILTER"
    clauses, must_not = Q.compile_filters({"source_scope": "SKRU1", "year": {"gte": 2000}, "language": ["ru"],
                                           "quality_flags_none": ["LOW_OCR_CONFIDENCE"], "has_preview": True})
    assert {"terms": {"source_site_scope": ["SKRU1"]}} in clauses
    assert {"range": {"year": {"gte": 2000}}} in clauses and {"term": {"has_preview": True}} in clauses
    assert must_not == [{"terms": {"quality_flags": ["LOW_OCR_CONFIDENCE"]}}]
    for bad in ({"year": 2000}, {"year": {"from": 1}}, {"has_preview": "yes"}, {"source_id": []}):
        with pytest.raises(Q.SearchRequestError):
            Q.compile_filters(bad)


def test_availability_needs_an_unknown_policy_h19():
    with pytest.raises(Q.SearchRequestError) as info:
        Q.compile_filters({"available_until": "2010-12-31"})
    assert info.value.code == "E_UNKNOWN_POLICY_REQUIRED"
    with pytest.raises(Q.SearchRequestError):
        Q.compile_filters({"unknown_policy": "EXCLUDE"})
    excl, _ = Q.compile_filters({"available_until": "2010-12-31", "unknown_policy": "EXCLUDE"})
    assert excl == [{"range": {"available_latest_day": {"lte": "2010-12-31"}}}]
    incl, _ = Q.compile_filters({"available_until": "2010-12-31", "unknown_policy": "INCLUDE"})
    should = incl[0]["bool"]["should"]
    assert should[1] == {"bool": {"must_not": [{"exists": {"field": "available_latest_day"}}]}}
    assert body()["aggs"]["availability"]["terms"]["field"] == "available_basis"


def test_body_shape_and_primary_layer_default():
    b = body()
    assert b["sort"] == [{"_score": "desc"}, {"id": "asc"}] and b["track_total_hits"] is True
    assert {"term": {"is_primary_layer": False}} in b["query"]["bool"]["must_not"]
    assert "is_primary_layer" not in str(body(include_secondary_layers=True)["query"]["bool"]["must_not"])
    assert not {"text", "caption", "recognized_latex", "work_title", "authors"} & set(b["_source"]["includes"])
    must = b["query"]["bool"]["must"][0]["multi_match"]
    assert must["fields"] == ["text^1.0"]                       # title only boosts, never satisfies the match
    assert any("work_title^0.3" in str(s) for s in b["query"]["bool"]["should"])
    assert b["collapse"]["field"] == "dup_group_id"             # duplicate pages collapsed by default
    assert "collapse" not in body(include_duplicates=True)
    blocks = body("blocks")
    assert blocks["collapse"]["field"] == "page_id" and blocks["collapse"]["inner_hits"]["name"] == "best_blocks"


def test_exact_mode_switches_to_unstemmed_fields():
    b = body(exact=True)
    assert b["query"]["bool"]["must"][0]["multi_match"]["fields"] == ["text.exact^1.0"]
    assert "text.exact" in b["highlight"]["fields"]


def test_object_number_boost():
    assert Q.parse_object_label("рис. 3.1 мульда сдвижения") == "3.1"
    assert Q.parse_object_label("табл. 2") == "2" and Q.parse_object_label("формула (3.12)") == "3.12"
    fig = Q.build_body("figures", Q.SearchRequest("рис. 3.1 мульда"), size=5, offset=0)
    assert {"term": {"object_label": {"value": "3.1", "boost": 5.0}}} in fig["query"]["bool"]["should"]
    assert "object_label" not in str(body()["query"])


def test_request_validation():
    for kw in ({"query": ""}, {"query": "x", "kinds": ("PAGE", "PAGE")}, {"query": "x", "kinds": ("WORK",)},
               {"query": "x", "size": 0}, {"query": "x", "size": 201}):
        with pytest.raises(Q.SearchRequestError):
            Q.SearchRequest(**kw).validate()


def _resp(index, ids, kind_scores=None, highlight=True):
    hits = []
    for i, oid in enumerate(ids):
        hit = {"_index": index, "_id": oid, "_score": 10.0 - i,
               "_source": {"id": oid, "object_type": "BLOCK", "source_id": oid.split(":")[0],
                           "page_id": ":".join(oid.split(":")[:2]), "preview_artifact_id": "sha256:" + "0" * 64},
               "inner_hits": {"best_blocks": {"hits": {"hits": [
                   {"_id": oid, "_score": 9.0, "_source": {"reading_order": 2}},
                   {"_id": oid[:-1] + "0", "_score": 8.0, "_source": {"reading_order": 1}}]}}}}
        if highlight:
            hit["highlight"] = {"text": ["<em>оседания</em> земной"]}
        hits.append(hit)
    return {"hits": {"total": {"value": len(ids)}, "hits": hits},
            "aggregations": {"availability": {"buckets": [{"key": "UNKNOWN", "doc_count": len(ids)}]}}}


def test_parse_hits_keeps_ids_highlights_and_best_blocks():
    ids = [f"VKM-SRC-001:p000{i}:b00000000000{i}" for i in range(1, 4)]
    hits, warnings = Q.parse_hits(_resp("vkm-blocks-m1-20260928t101500z-3fa9c2d1", ids), "BLOCK")
    assert warnings == [] and [h.id for h in hits] == ids and [h.rank for h in hits] == [1, 2, 3]
    assert hits[0].build_id == "20260928t101500z-3fa9c2d1" and hits[0].highlight_origin == "SEARCH_INDEX"
    assert hits[0].highlights == ["<em>оседания</em> земной"] and len(hits[0].best_blocks) == 2


def test_rank_fusion_is_deterministic_and_by_rank_h44():
    fig, _ = Q.parse_hits(_resp("f", ["VKM-SRC-001:p0001:f000000000001", "VKM-SRC-001:p0002:f000000000002"]), "FIGURE")
    tab, _ = Q.parse_hits(_resp("t", ["VKM-SRC-002:p0001:t000000000001"]), "TABLE")
    for h in fig + tab:
        h.score *= 1000 if h.object_type == "TABLE" else 1       # raw BM25 scales must not matter
    fused = Q.fuse_rrf({"FIGURE": fig, "TABLE": tab}, ("FIGURE", "TABLE"))
    assert [h.id for h in fused] == ["VKM-SRC-001:p0001:f000000000001", "VKM-SRC-002:p0001:t000000000001",
                                     "VKM-SRC-001:p0002:f000000000002"]
    assert [h.rank for h in fused] == [1, 2, 3] and fused[0].rrf_score == pytest.approx(1 / 61)


def test_rerank_candidates_are_references_without_text_h13():
    ids = [f"VKM-SRC-001:p{i:04d}:b{i:012x}" for i in range(1, 31)]
    hits, _ = Q.parse_hits(_resp("b", ids), "BLOCK")
    resp = Q.SearchResponse("q", ("BLOCK",), hits, {"BLOCK": 30}, {}, "NONE")
    text = Q.rerank_candidates(resp)
    assert len(text["candidates"]) == 24 and text["truncated_from"] == 30
    first = text["candidates"][0]
    assert first["object_type"] == "PAGE_PASSAGE" and first["passage"]["rule"] == "rerank_text_v1"
    assert first["passage"]["object_ids"] == [ids[0][:-1] + "0", ids[0]]       # reading order
    assert "text" not in first
    visual = Q.rerank_candidates(resp, mode="visual")
    assert len(visual["candidates"]) == 8 and all(c["image_artifact_id"] for c in visual["candidates"])
    with pytest.raises(Q.SearchRequestError):
        Q.rerank_candidates(resp, max_candidates=25)
    with pytest.raises(Q.SearchRequestError):
        Q.rerank_candidates(resp, mode="visual", max_candidates=9)

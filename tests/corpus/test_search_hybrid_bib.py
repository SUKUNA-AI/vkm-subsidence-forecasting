"""Bibliographic route of hybrid search (agent L): a detected bibliographic query adds the BIB_ENTRY channel of the RX580
service (late MaxSim scan → pages by their best entry) as a third RRF leg, filters and duplicate groups go through the
page index, and the late stage scores those pages with their entries; every other query keeps CP-42. Fakes only."""
from __future__ import annotations

import json

import pytest

httpx = pytest.importorskip("httpx")

from vkm_corpus.search.fakes import FakeOpenSearch  # noqa: E402
from vkm_corpus.search.hybrid import EmbedClient, HybridError, HybridRequest, bib_route_status, hybrid_search  # noqa: E402

DIM = 4
Q = [1.0, 0.0, 0.0, 0.0]
P1, P2, P3 = "VKM-SRC-001:p0001", "VKM-SRC-001:p0002", "VKM-SRC-001:p0003"
R1, R2, R3 = "VKM-SRC-002:p0040", "VKM-SRC-003:p0101", "VKM-SRC-003:p0102"     # reference-list pages
META = {"build_id": "20260928t120000z-aaaaaaaa-bbbbbbbb", "built_from_snapshot_id": "SNAP-1", "dimension": DIM,
        "model_key": "jina-v5-nano-retrieval", "config_signature": "c" * 64, "space_type": "innerproduct",
        "build_status": "COMPLETE"}
BIB_QUERY = "работы Баряха о ползучести соли"
PAGE_INDEX = {P1: {"dup": P1, "year": 2010}, P2: {"dup": P2, "year": 2010}, P3: {"dup": P3, "year": 2010},
              R1: {"dup": R1, "year": 2015}, R2: {"dup": "G-R", "year": 2001}, R3: {"dup": "G-R", "year": 2001}}


def _client():
    def answer(index, body):
        q = body.get("query") or {}
        ids_clause = next((c for c in (q.get("bool") or {}).get("filter", []) if "ids" in c), None)
        if ids_clause is not None:                                    # the page filter of the BIB leg
            want = ids_clause["ids"]["values"]
            years = next((c["range"]["year"] for c in q["bool"]["filter"] if "range" in c), None)
            out = []
            for pid in want:
                meta = PAGE_INDEX.get(pid)
                if meta is None or (years and not years.get("gte", 0) <= meta["year"] <= years.get("lte", 9999)):
                    continue
                out.append({"_id": pid, "_index": "vkm-pages-m1-b", "_score": 0.0,
                            "_source": {"id": pid, "dup_group_id": meta["dup"]}})
            return out
        if index == "vkm-pages":
            return [{"_id": p, "_index": "vkm-pages-m1-b-test", "_score": s,
                     "_source": {"id": p, "object_type": "PAGE", "source_id": "VKM-SRC-001", "page_id": p}}
                    for p, s in ((P1, 9.0), (P2, 5.0))]
        return []

    c = FakeOpenSearch(bm25=answer)
    name = f"vkm-vectors-m1-{META['build_id']}"
    c.indices.create(index=name, body={"mappings": {"_meta": dict(META)}})
    for uid, page, kind, vec in (("u1-1", P1, "BLOCK_GROUP", [0.9, 0.1, 0.0, 0.0]),
                                 ("u1-2", P3, "BLOCK_GROUP", [0.5, 0.5, 0.0, 0.0]),
                                 ("u1-9", R1, "BIB_ENTRY", [1.0, 0.0, 0.0, 0.0])):
        c.indices_[name]["docs"][uid] = {"id": uid, "unit_kind": kind, "object_ids": [f"{page}:b{uid}"],
                                         "page_id": page, "source_id": page.split(":")[0], "dup_group_id": page,
                                         "year": PAGE_INDEX[page]["year"], "vector": vec}
    c.indices.update_aliases({"actions": [{"add": {"index": name, "alias": "vkm-vectors"}}]})
    return c


def _service(late_scores, scan=None, *, scan_status=200, scan_body=None):
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append((request.url.path, body))
        if request.url.path == "/embed/query":
            return httpx.Response(200, json={"dense": {"model": "jina-v5-nano-retrieval", "vector": Q}})
        if "scan_kind" in body:
            if scan_status != 200:
                return httpx.Response(scan_status, json={"detail": "late-interaction token store MISSING"})
            if scan_body is not None:
                return httpx.Response(200, json=scan_body)
            return httpx.Response(200, json={"mode": "late-scan", "model": "mlateon", "query_signature": "l" * 64,
                                             "store": {"pack_id": "P", "snapshot_id": "SNAP-1"}, "scan_units": 5,
                                             "timings_ms": {"late_encode": 12.0, "scan": 250.0},
                                             "scan": [{"page_id": p, "late_score": s, "best_unit_id": f"u-{p}",
                                                       "units": 3, "scan_rank": i}
                                                      for i, (p, s) in enumerate(scan or [], 1)]})
        results = [{"id": t["id"], "kind": t["kind"], "status": "SCORED" if t["id"] in late_scores else "NO_TOKENS",
                    "late_score": late_scores.get(t["id"]), "best_unit_id": None, "units": 1, "tokens": 9}
                   for t in body["targets"]]
        return httpx.Response(200, json={"model": "mlateon", "query_signature": "l" * 64, "store": {},
                                         "timings_ms": {"maxsim": 3.0}, "results": results})

    client = EmbedClient("http://rx580-retrieval:8790", "tok", transport=httpx.MockTransport(handler))
    client.calls = calls
    return client


def _late_call(svc):
    return next(b for p, b in reversed(svc.calls) if p == "/search/late" and "targets" in b)


def test_detected_query_adds_the_bib_leg_and_scores_pages_with_their_entries():
    svc = _service({P1: 5.0, P2: 1.0, P3: 2.0, R1: 9.0, R2: 7.0}, scan=[(R1, 20.0), (R2, 18.0), (R3, 17.0)])
    client = _client()
    out = hybrid_search(client, svc, HybridRequest(query=BIB_QUERY, candidates=10, late=True), "vkm")
    scan_call = next(b for p, b in svc.calls if "scan_kind" in b)
    assert scan_call["scan_kind"] == "BIB_ENTRY" and scan_call["scan_top"] == 10
    assert _late_call(svc)["page_exclude_kinds"] == []                     # BIB_ENTRY included for this route
    ids = [h["id"] for h in out["hits"]]
    assert ids[:2] == [R1, R2] and R3 not in ids                            # R3: same duplicate group as R2
    t = {h["id"]: h["trace"] for h in out["hits"]}
    assert t[R1]["bib_rank"] == 1 and t[R1]["bib_unit"] == {"unit_id": f"u-{R1}", "units": 3}
    assert t[P1]["bib_rank"] is None and t[P1]["bm25_rank"] == 1
    route = out["stages"]["bib_route"]
    assert route["status"] == "APPLIED" and "works_of_author" in route["cues"] and route["pages_kept"] == 2
    assert out["route"] == "bibliographic" and "BIB_ENTRY included" in out["stages"]["late"]["page_score"]
    assert out["hits"][0]["source_id"] == "VKM-SRC-002" and "bib_scan" in out["timings_ms"]
    # the dense PAGE leg still excludes BIB_ENTRY units (CP-42): the channel is the scan, not the page leg
    knn = next(b for _i, b in client.searches if "knn" in (b.get("query") or {}))["query"]["knn"]["vector"]
    assert {"terms": {"unit_kind": ["BIB_ENTRY"]}} in knn["filter"]["bool"]["must_not"]
    assert t[R1]["dense_rank"] is None                                      # its BIB unit never reached the page leg


def test_other_queries_keep_cp42_and_make_no_scan():
    svc = _service({P1: 5.0, P2: 1.0, P3: 2.0})
    out = hybrid_search(_client(), svc, HybridRequest(query="ползучесть соли", candidates=10, late=True), "vkm")
    assert not [b for _p, b in svc.calls if "scan_kind" in b]
    assert _late_call(svc)["page_exclude_kinds"] == ["BIB_ENTRY"]
    assert out["stages"]["bib_route"]["status"] == "NOT_DETECTED" and out["route"] == "default"
    assert all("bib_rank" not in h["trace"] for h in out["hits"])


@pytest.mark.parametrize("kw,status", [({"bib_route": False}, "OFF"), ({"late": False}, "SKIPPED_LATE_OFF"),
                                       ({"kinds": ("FIGURE",)}, "SKIPPED_NO_PAGE_KIND")])
def test_route_switches(kw, status):
    req = HybridRequest(**{"query": BIB_QUERY, "candidates": 10, "late": True, **kw})
    req.validate()
    assert bib_route_status(req)[0] == status
    forced = HybridRequest(query="ползучесть соли", late=True, bib_route=True)
    forced.validate()
    assert bib_route_status(forced)[0] == "APPLIED"


def test_filters_and_duplicates_go_through_the_page_index():
    svc = _service({R1: 9.0, R2: 7.0, R3: 6.5}, scan=[(R1, 20.0), (R2, 18.0), (R3, 17.0)])
    out = hybrid_search(_client(), svc, HybridRequest(query=BIB_QUERY, candidates=10, late=True,
                                                      filters={"year": {"gte": 2010}}), "vkm")
    ids = [h["id"] for h in out["hits"]]
    assert R1 in ids and R2 not in ids and R3 not in ids                   # 2001 pages filtered out
    svc = _service({R1: 9.0, R2: 7.0, R3: 6.5}, scan=[(R1, 20.0), (R2, 18.0), (R3, 17.0)])
    both = hybrid_search(_client(), svc, HybridRequest(query=BIB_QUERY, candidates=10, late=True,
                                                       include_duplicates=True), "vkm")
    assert {R2, R3} <= {h["id"] for h in both["hits"]}


@pytest.mark.parametrize("case,code", [("unavailable", "DEPENDENCY_UNAVAILABLE"), ("old_service", "DEPENDENCY_ERROR")])
def test_scan_failures_are_loud(case, code):
    svc = _service({}, scan_status=503) if case == "unavailable" else _service({}, scan_body={"mode": "late"})
    with pytest.raises(HybridError) as exc:
        hybrid_search(_client(), svc, HybridRequest(query=BIB_QUERY, candidates=10, late=True), "vkm")
    assert exc.value.code == code and exc.value.stage == "bib_scan"

"""Visual route of hybrid search (agent VIS): a query with a picture word adds the page-image channel (visual text
tower on the RX580 + exact inner product over the page vectors) and fuses it with the served page order by RRF;
every other query — and every query while the server switch is off — is answered exactly as before. Fakes only."""
from __future__ import annotations

import json

import pytest

httpx = pytest.importorskip("httpx")

from vkm_corpus.search.fakes import FakeOpenSearch  # noqa: E402
from vkm_corpus.search.hybrid import (EmbedClient, HybridError, HybridRequest, VisualRouteSettings,  # noqa: E402
                                      fuse_visual, hybrid_search, visual_route_status)

DIM = 4
Q = [1.0, 0.0, 0.0, 0.0]
QV = [0.0, 1.0, 0.0, 0.0]                         # visual query vector
P1, P2, P3 = "VKM-SRC-001:p0001", "VKM-SRC-001:p0002", "VKM-SRC-001:p0003"
V1, V2, V3 = "VKM-SRC-005:p0010", "VKM-SRC-005:p0011", "VKM-SRC-006:p0200"     # pages found by their image
F1 = "VKM-SRC-001:p0001:f0001"
META = {"build_id": "20260928t120000z-aaaaaaaa-bbbbbbbb", "built_from_snapshot_id": "SNAP-1", "dimension": DIM,
        "model_key": "jina-v5-nano-retrieval", "config_signature": "c" * 64, "space_type": "innerproduct",
        "build_status": "COMPLETE"}
VMETA = {"build_id": "20260929t100000z-aaaaaaaa-ef2f297d", "built_from_snapshot_id": "SNAP-1", "dimension": DIM,
         "model_key": "qwen3-vl-emb-2b", "config_signature": "e" * 64, "space_type": "innerproduct",
         "build_status": "COMPLETE"}
VIS_QUERY = "схема расположения целиков"
TEXT_QUERY = "ползучесть каменной соли"
ON = VisualRouteSettings(enabled=True)
PAGES = {P1: {"dup": P1, "year": 2010, "vec": [0.2, 0.3, 0.0, 0.0]}, P2: {"dup": P2, "year": 2010,
                                                                       "vec": [0.1, 0.1, 0.0, 0.0]},
         P3: {"dup": P3, "year": 2010, "vec": [0.0, 0.0, 1.0, 0.0]}, V1: {"dup": V1, "year": 2015,
                                                                         "vec": [0.0, 0.95, 0.1, 0.0]},
         V2: {"dup": "G-V", "year": 2001, "vec": [0.0, 0.9, 0.2, 0.0]},
         V3: {"dup": "G-V", "year": 2001, "vec": [0.0, 0.85, 0.0, 0.3]}}


def _client(*, pagevis: bool = True, figures: bool = False):
    def answer(index, body):
        if index == "vkm-pages":
            return [{"_id": p, "_index": "vkm-pages-m1-b-test", "_score": s,
                     "_source": {"id": p, "object_type": "PAGE", "source_id": "VKM-SRC-001", "page_id": p}}
                    for p, s in ((P1, 9.0), (P2, 5.0))]
        if index == "vkm-figures" and figures:
            return [{"_id": F1, "_index": "vkm-figures-m1-b-test", "_score": 7.0,
                     "_source": {"id": F1, "object_type": "FIGURE", "source_id": "VKM-SRC-001", "page_id": P1}}]
        return []

    c = FakeOpenSearch(bm25=answer)
    name = f"vkm-vectors-m1-{META['build_id']}"
    c.indices.create(index=name, body={"mappings": {"_meta": dict(META)}})
    for uid, page, vec in (("u1-1", P1, [0.9, 0.1, 0.0, 0.0]), ("u1-2", P3, [0.5, 0.5, 0.0, 0.0])):
        c.indices_[name]["docs"][uid] = {"id": uid, "unit_kind": "BLOCK_GROUP", "object_ids": [f"{page}:b1"],
                                         "page_id": page, "source_id": page.split(":")[0], "dup_group_id": page,
                                         "year": PAGES[page]["year"], "vector": vec}
    c.indices.update_aliases({"actions": [{"add": {"index": name, "alias": "vkm-vectors"}}]})
    if pagevis:
        vis = f"vkm-pagevis-m1-{VMETA['build_id']}"
        c.indices.create(index=vis, body={"mappings": {"_meta": dict(VMETA)}})
        for pid, m in PAGES.items():
            c.indices_[vis]["docs"][pid] = {"id": pid, "page_id": pid, "source_id": pid.split(":")[0],
                                            "dup_group_id": m["dup"], "year": m["year"], "page_index": 1,
                                            "vector": m["vec"]}
        c.indices.update_aliases({"actions": [{"add": {"index": vis, "alias": "vkm-pagevis"}}]})
    return c


def _service(late_scores, *, visual_status=200, visual_model="qwen3-vl-emb-2b", visual_dim=DIM):
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append((request.url.path, body))
        if request.url.path == "/embed/query":
            if body["role"] == "visual":
                if visual_status != 200:
                    return httpx.Response(visual_status, json={"detail": "no visual model is configured"})
                return httpx.Response(200, json={"visual": {"model": visual_model, "signature": "v" * 64,
                                                            "vector": (QV + [0.0] * 8)[:visual_dim],
                                                            "encode_ms": 400.0}})
            return httpx.Response(200, json={"dense": {"model": "jina-v5-nano-retrieval", "vector": Q}})
        results = [{"id": t["id"], "kind": t["kind"], "status": "SCORED" if t["id"] in late_scores else "NO_TOKENS",
                    "late_score": late_scores.get(t["id"]), "best_unit_id": None, "units": 1, "tokens": 9}
                   for t in body["targets"]]
        return httpx.Response(200, json={"model": "mlateon", "query_signature": "l" * 64, "store": {},
                                         "timings_ms": {"maxsim": 3.0}, "results": results})

    client = EmbedClient("http://rx580-retrieval:8790", "tok", transport=httpx.MockTransport(handler))
    client.calls = calls
    return client


LATE = {P1: 5.0, P2: 1.0, P3: 2.0}


def _visual_calls(svc):
    return [b for p, b in svc.calls if p == "/embed/query" and b["role"] == "visual"]


def test_detected_query_fuses_the_page_image_leg_with_the_served_order():
    svc, client = _service(LATE), _client()
    out = hybrid_search(client, svc, HybridRequest(query=VIS_QUERY, candidates=10, late=True), "vkm", visual=ON)
    assert len(_visual_calls(svc)) == 1
    body = next(b for i, b in client.searches if i == "vkm-pagevis")
    assert body["query"]["script_score"]["script"]["params"]["space_type"] == "innerproduct"   # exact, V2's scheme
    assert body["size"] == 20                                          # candidates × oversample (duplicates collapse)
    ids = [h["id"] for h in out["hits"]]
    # E (after late): P1, P3, P2; image leg: V1, V2 (V3 collapsed into V2's group), P1, P2, P3 → RRF: pages of both
    # legs first, then the image-only pages
    assert ids == [P1, P3, P2, V1, V2]
    t = {h["id"]: h["trace"] for h in out["hits"]}
    assert t[V1]["vis_rank"] == 1 and t[V1]["e_rank"] is None and t[V1]["fused_rank"] is None
    assert t[P1]["e_rank"] == 1 and t[P1]["vis_rank"] == 3 and t[P1]["late_rank"] == 1
    assert abs(t[V1]["vis_score"] - 0.95) < 1e-6 and t[V1]["visual_rrf_score"] == pytest.approx(1 / 61)
    assert t[P1]["visual_rrf_score"] == pytest.approx(1 / 61 + 1 / 63)
    assert t[V1]["late_status"] == "NOT_CANDIDATE" and t[V1]["final_rank"] == 4
    stage = out["stages"]["visual_route"]
    assert stage["status"] == "APPLIED" and stage["cues"] == ["scheme"] and stage["mode"] == "exact"
    assert stage["model_key"] == stage["query_model"] == "qwen3-vl-emb-2b" and stage["new_pages"] == 2
    assert out["route"] == "visual" and {"visual_embed", "visual_knn"} <= set(out["timings_ms"])
    v1 = next(h for h in out["hits"] if h["id"] == V1)
    assert v1["source_id"] == "VKM-SRC-005" and v1["index"].startswith("vkm-pagevis-m1-")
    assert v1["build_id"] == VMETA["build_id"] and out["fused_total"] == 5


def test_text_queries_are_answered_exactly_as_before():
    base = hybrid_search(_client(), _service(LATE), HybridRequest(query=TEXT_QUERY, candidates=10, late=True), "vkm")
    svc = _service(LATE)
    out = hybrid_search(_client(), svc, HybridRequest(query=TEXT_QUERY, candidates=10, late=True), "vkm", visual=ON)
    assert not _visual_calls(svc) and out["route"] == "default"
    assert out["stages"]["visual_route"] == {"status": "NOT_DETECTED", "cues": []}
    assert [h["id"] for h in out["hits"]] == [h["id"] for h in base["hits"]]
    assert [h["trace"] for h in out["hits"]] == [h["trace"] for h in base["hits"]]


def test_switch_off_means_no_channel_and_explicit_request_fails_loudly():
    svc = _service(LATE)
    out = hybrid_search(_client(), svc, HybridRequest(query=VIS_QUERY, candidates=10, late=True), "vkm",
                        visual=VisualRouteSettings(enabled=False))
    assert not _visual_calls(svc) and out["stages"]["visual_route"]["status"] == "DISABLED"
    assert out["route"] == "default"
    with pytest.raises(HybridError) as exc:
        hybrid_search(_client(), _service(LATE), HybridRequest(query=TEXT_QUERY, candidates=10, late=True,
                                                               visual_route=True), "vkm")
    assert exc.value.code == "DEPENDENCY_UNAVAILABLE" and exc.value.stage == "visual"


@pytest.mark.parametrize("kw,status", [({"visual_route": False}, "OFF"), ({"late": False}, "SKIPPED_LATE_OFF"),
                                       ({"kinds": ("FIGURE",)}, "SKIPPED_NO_PAGE_KIND")])
def test_route_switches(kw, status):
    req = HybridRequest(**{"query": VIS_QUERY, "candidates": 10, "late": True, **kw})
    req.validate()
    assert visual_route_status(req, ON)[0] == status
    forced = HybridRequest(query=TEXT_QUERY, late=True, visual_route=True)
    forced.validate()
    assert visual_route_status(forced, ON)[0] == "APPLIED"


def test_forced_route_on_a_text_query_and_both_routes_together():
    svc = _service(LATE)
    out = hybrid_search(_client(), svc, HybridRequest(query=TEXT_QUERY, candidates=10, late=True,
                                                      visual_route=True), "vkm", visual=ON)
    assert out["route"] == "visual" and out["stages"]["visual_route"]["cues"] == []


def test_filters_and_duplicates_apply_to_the_image_leg():
    client = _client()
    out = hybrid_search(client, _service(LATE), HybridRequest(query=VIS_QUERY, candidates=10, late=True,
                                                              filters={"year": {"gte": 2010}}), "vkm", visual=ON)
    body = next(b for i, b in client.searches if i == "vkm-pagevis")
    assert {"range": {"year": {"gte": 2010}}} in body["query"]["script_score"]["query"]["bool"]["filter"]
    ids = {h["id"] for h in out["hits"]}
    assert V1 in ids and not ({V2, V3} & ids)                          # 2001 pages filtered out
    both = hybrid_search(_client(), _service(LATE), HybridRequest(query=VIS_QUERY, candidates=10, late=True,
                                                                  include_duplicates=True), "vkm", visual=ON)
    assert {V2, V3} <= {h["id"] for h in both["hits"]}


def test_other_kinds_keep_their_positions():
    svc = _service({P1: 5.0, P2: 1.0, P3: 2.0, F1: 3.0})
    base = hybrid_search(_client(figures=True), _service({P1: 5.0, P2: 1.0, P3: 2.0, F1: 3.0}),
                         HybridRequest(query=VIS_QUERY, kinds=("PAGE", "FIGURE"), candidates=10, late=True), "vkm")
    out = hybrid_search(_client(figures=True), svc, HybridRequest(query=VIS_QUERY, kinds=("PAGE", "FIGURE"),
                                                                  candidates=10, late=True), "vkm", visual=ON)
    pos = lambda hits: [i for i, h in enumerate(hits) if h["object_type"] == "FIGURE"]  # noqa: E731
    assert pos(out["hits"]) == pos(base["hits"]) and len(out["hits"]) == len(base["hits"]) + 2


def test_fuse_visual_refills_page_slots_and_appends_new_pages():
    order = [("A", 0.3), ("F", 0.2), ("B", 0.1)]
    kind_of = {"A": "PAGE", "F": "FIGURE", "B": "PAGE", "X": "PAGE"}
    new, e_rank, score = fuse_visual(order, kind_of, [("X", 0.9), ("B", 0.8)], depth=100, rrf_k=60)
    assert [k for k, _s in new] == ["B", "F", "A", "X"]       # B: both legs; A and X tie → by id (RRF rule)
    assert e_rank == {"A": 1, "B": 2} and set(score) == {"A", "B", "X"}
    assert score["B"] == pytest.approx(1 / 62 + 1 / 62)


@pytest.mark.parametrize("case,code,stage", [
    ("no_index", "DEPENDENCY_UNAVAILABLE", "visual"), ("no_slot", "DEPENDENCY_UNAVAILABLE", "visual_embed"),
    ("other_model", "DEPENDENCY_ERROR", "visual_embed"), ("other_dim", "DEPENDENCY_ERROR", "visual_embed")])
def test_failures_are_loud(case, code, stage):
    svc = {"no_slot": _service(LATE, visual_status=404), "other_model": _service(LATE, visual_model="other"),
           "other_dim": _service(LATE, visual_dim=3)}.get(case, _service(LATE))
    with pytest.raises(HybridError) as exc:
        hybrid_search(_client(pagevis=case != "no_index"), svc, HybridRequest(query=VIS_QUERY, candidates=10,
                                                                             late=True), "vkm", visual=ON)
    assert exc.value.code == code and exc.value.stage == stage


def test_hnsw_mode_uses_the_graph_with_efficient_filtering():
    from vkm_corpus.search.page_vectors import page_vector_body

    b = page_vector_body([0.1] * 4, 50, {"year": {"gte": 2000}}, mode="hnsw", ef_search=256)
    knn = b["query"]["knn"]["vector"]
    assert knn["k"] == 50 and knn["method_parameters"] == {"ef_search": 256}
    assert {"range": {"year": {"gte": 2000}}} in knn["filter"]["bool"]["filter"]
    with pytest.raises(ValueError):
        page_vector_body([0.1], 5, {}, mode="ivf")


def _smoke_answer(route: str, trace: dict, stage: dict) -> dict:
    return {"ok": True, "items": [{"envelope": {"object_id": V1}, "record": {"trace": trace}}],
            "item": {"record": {"route": route, "timings_ms": {"total": 700.0, "visual_embed": 420.0,
                                                               "visual_knn": 60.0},
                                "stages": {"visual_route": stage}}}}


@pytest.mark.parametrize("expect,route,ok", [("visual", "visual", True), ("visual", "default", False),
                                             ("default", "default", True)])
def test_hybrid_smoke_checks_the_visual_route(monkeypatch, tmp_path, capsys, expect, route, ok):
    from vkm_corpus.search import cli

    token = tmp_path / "token"
    token.write_text("test-token-for-smoke", encoding="utf-8")
    monkeypatch.setenv("VKM_API_TOKEN_FILE", str(token))
    trace = {"fused_rank": None, "bm25_rank": None, "dense_rank": None, "vis_rank": 1, "e_rank": None}
    stage = {"status": "APPLIED" if route == "visual" else "NOT_DETECTED", "cues": ["scheme"], "mode": "exact",
             "pages_returned": 100, "new_pages": 40}
    if route == "default":
        trace = {"fused_rank": 1, "bm25_rank": 1, "dense_rank": 2}
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=_smoke_answer(route, trace, stage))), **kw))
    args = cli.argparse.Namespace(api_url="http://api.test", query=[VIS_QUERY], limit=10, late=None,
                                  late_candidates=100, expect_route=expect)
    rc = cli._cmd_hybrid_smoke(args)
    out = json.loads(capsys.readouterr().out)
    assert (rc == 0) is ok
    q = out["queries"][0]
    assert q["route"] == route and q["visual_route"]["cues"] == ["scheme"]
    if route == "visual":
        assert q["hits_with_vis_rank"] == 1 and q["server_ms"]["visual_embed"] == 420.0

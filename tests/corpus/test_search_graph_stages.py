"""Graph stages of the hybrid search (agent GS; ``search.graph_stages``): the lookup built from NAV rows, G1 collapse,
G2 section cohesion, G3 wordings, G4 citation sources, G5 topic prior, the two fusion points (after late with a
protected head; the late window), and their run inside ``hybrid_search`` (fake OpenSearch, fake RX580 service,
synthetic NAV lookups). Page and section ids are invented."""
from __future__ import annotations

import json

import pytest

from vkm_corpus.search import graph_stages as G

S = "VKM-SRC-001"


def p(n: int, src: str = S) -> str:
    return f"{src}:p{n:04d}"


def maps(**kw) -> G.GraphMaps:
    return G.build_maps(snapshot_id="SNAP-T", **kw)


# ---------------------------------------------------------------------------------------------------- basics
def test_parse_and_resolve_stages():
    assert G.parse_stages(None) is None
    assert G.parse_stages("none") == () and G.parse_stages("") == () and G.parse_stages([]) == ()
    assert G.parse_stages("topics, collapse") == ("collapse", "topics")          # STAGES order
    assert G.parse_stages(["cites", "cohesion"]) == ("cohesion", "cites")
    assert G.parse_stages("all") == G.STAGES
    with pytest.raises(ValueError):
        G.parse_stages("collapse,reranker")
    assert G.resolve(None) == tuple(s for s in G.STAGES if s in G.DEFAULTS)
    assert G.resolve(None, ["topics"]) == ("topics",) and G.resolve((), ["topics"]) == ()
    # coordinator 29.09: off by default (the per-stage rule passes no stage; GC only by a hair) — see RESULTS.md
    assert G.DEFAULTS == frozenset()
    P = G.GraphParams()
    assert (P.cohesion_weight, P.concepts_narrower, P.concepts_mode, P.cites_mode, P.topics_weight) == \
        (0.5, False, "post", "window", 0.5)
    with pytest.raises(ValueError):
        G.GraphParams(concepts_mode="late").validate()


def test_build_maps_coverage_sections_topics_and_cites():
    m = maps(sections=[("SEC-a", 2), ("SEC-b", 1)],
             section_pages=[("SEC-a", p(3), 3), ("SEC-a", p(1), 1), ("SEC-a", p(2), 2), ("SEC-b", p(9), 9)],
             topic_members=[("SEC-a", 1, "TOP-1"), ("SEC-a", 2, "TOP-9")],
             dup_members=[("DCL-1", "REPRINT", p(1), 800), ("DCL-1", "REPRINT", p(5, "VKM-SRC-002"), 600),
                          ("DCL-2", "PARTIAL_OVERLAP", p(2), 900), ("DCL-2", "PARTIAL_OVERLAP", p(6, "VKM-SRC-002"), 900),
                          ("DCL-3", "BOILERPLATE", p(3), 500), ("DCL-3", "BOILERPLATE", p(7, "VKM-SRC-002"), 500)],
             page_chars={p(1): 1000, p(5, "VKM-SRC-002"): 2000, p(2): 1000, p(3): 600},
             object_members=[("F1", "OCL-1", "REPRINT")],
             cites=[("VKM-WRK-001", "VKM-WRK-002"), ("VKM-WRK-003", "VKM-WRK-001")],
             source_works=[("VKM-SRC-001", "VKM-WRK-001"), ("VKM-SRC-002", "VKM-WRK-002"),
                           ("VKM-SRC-003", "VKM-WRK-003"), ("VKM-SRC-004", "VKM-WRK-001")])
    # shared characters / page characters, only copy kinds (a quote or a template never makes a copy)
    assert m.coverage == {p(1): {p(5, "VKM-SRC-002"): 0.8}, p(5, "VKM-SRC-002"): {p(1): 0.3}}
    assert m.copy_kinds[(p(1), p(5, "VKM-SRC-002"))] == ("REPRINT",)
    assert m.section_pages["SEC-a"] == ((p(1), 1), (p(2), 2), (p(3), 3))        # reading order
    assert m.sections_of(p(2)) == (G.SectionRef("SEC-a", 2, 3),) and m.sections_of("nope") == ()
    assert m.section_topics[("SEC-a", 1)] == "TOP-1" and m.object_groups["F1"] == ("OCL-1", "REPRINT")
    # CITES both ways, through works; two sources of one work are both linked
    assert m.cite_neighbours["VKM-SRC-001"] == {"VKM-SRC-002", "VKM-SRC-003"}
    assert m.cite_neighbours["VKM-SRC-004"] == {"VKM-SRC-002", "VKM-SRC-003"}
    assert m.cite_neighbours["VKM-SRC-002"] == {"VKM-SRC-001", "VKM-SRC-004"}


# ---------------------------------------------------------------------------------------------------- G1
def test_collapse_pages_objects_and_direction():
    a, a2, a3, b = p(1), p(1, "VKM-SRC-002"), p(1, "VKM-SRC-003"), p(2)
    m = G.GraphMaps(coverage={a2: {a: 0.9}, a3: {a2: 0.7, a: 0.2}, a: {a2: 0.4}},
                    copy_kinds={(a2, a): ("SAME_WORK_COPY",)},
                    object_groups={"F1": ("OCL-1", "REPRINT"), "F2": ("OCL-1", "REPRINT"),
                                   "L1": ("OCL-2", "BOILERPLATE"), "L2": ("OCL-2", "BOILERPLATE")})
    P = G.GraphParams()
    kinds = {"F1": "FIGURE", "F2": "FIGURE", "L1": "FIGURE", "L2": "FIGURE"}
    kept, copies = G.collapse([a, b, a2, "F1", a3, "F2", "L1", "L2"], kinds, m, P)
    # a2 is 0.9 covered by the shown a → a copy; a3 is covered by a2 (not shown) and only 0.2 by a → stays
    assert kept == [a, b, "F1", a3, "L1", "L2"]
    assert copies == {a: [{"page_id": a2, "coverage": 0.9, "kinds": ["SAME_WORK_COPY"]}],
                      "F1": [{"object_id": "F2", "cluster_id": "OCL-1", "kind": "REPRINT"}]}
    # the better-ranked page keeps the copy whatever the direction of the NAV primary; the covering page must be
    # shown earlier: a page covered only by a later page stays
    kept2, copies2 = G.collapse([a2, a], {}, m, P)
    assert kept2 == [a2, a] and copies2 == {}                     # a is only 0.4 covered by a2
    kept3, _ = G.collapse([a, a2], {}, m, G.GraphParams(copy_min_coverage=0.95))
    assert kept3 == [a, a2]                                        # below the threshold nothing leaves


# ---------------------------------------------------------------------------------------------------- G2
def _section_maps() -> G.GraphMaps:
    rows = [("SEC-deep", p(i), i) for i in range(10, 17)]            # level 2, 7 pages
    rows += [("SEC-chap", p(i), i) for i in range(30, 34)]           # level 1: never cohesive
    rows += [("SEC-long", p(i), i) for i in range(40, 60)]           # level 2, 20 pages: too long
    rows += [("SEC-other", p(i), i) for i in range(70, 74)]          # level 3, 4 pages
    return maps(sections=[("SEC-deep", 2), ("SEC-chap", 1), ("SEC-long", 2), ("SEC-other", 3)], section_pages=rows)


def test_cohesion_leg_selects_deep_short_sections_with_two_hits():
    m = _section_maps()
    P = G.GraphParams()
    order = [p(12), p(30), p(31), p(40), p(41), p(70), p(15), p(99), p(71), p(98), p(11), p(72)]
    leg, info = G.cohesion_leg(order, m, P)
    # cohesive: SEC-deep (hits 1, 7) and SEC-other (hits 6, 9); SEC-chap (level 1) and SEC-long (20 pages) are not
    assert [x["section_id"] for x in info] == ["SEC-deep", "SEC-other"]
    assert info[0]["hit_ranks"] == [1, 7] and info[0]["n_pages"] == 7
    # nearest to a hit first (p11/p13 at distance 1; p11 lies beyond the first 10 so it is a candidate), ≤ 4 each,
    # round robin between the sections
    assert info[0]["pages"] == [p(11), p(13), p(14), p(16)] and info[1]["pages"] == [p(72), p(73)]
    assert leg == [p(11), p(72), p(13), p(73), p(14), p(16)]
    small, _ = G.cohesion_leg(order, m, G.GraphParams(cohesion_leg=3, cohesion_per_section=1))
    assert small == [p(11), p(72)]
    none, info0 = G.cohesion_leg([p(12), p(99), p(98)], m, P)            # one hit only
    assert none == [] and info0 == []


# ---------------------------------------------------------------------------------------------------- G5, G4
def test_topic_leg_lifts_later_pages_of_the_hit_topics():
    m = maps(sections=[("SEC-1", 2), ("SEC-2", 2), ("SEC-3", 2)],
             section_pages=[("SEC-1", p(1), 1), ("SEC-2", p(5, "VKM-SRC-002"), 5), ("SEC-3", p(9), 9),
                            ("SEC-1", p(2), 2)],
             topic_members=[("SEC-1", 1, "TOP-a"), ("SEC-2", 1, "TOP-a"), ("SEC-3", 1, "TOP-b")])
    P = G.GraphParams(head=2, topics_top=2)
    order = [p(1), p(5, "VKM-SRC-002"), p(9), p(3), p(2)]
    leg, info = G.topic_leg(order, m, P)
    assert info == [{"topic_id": "TOP-a", "hit_ranks": [1, 2]}] and leg == [p(2)]
    assert G.topic_leg([p(1), p(9), p(2)], m, P) == ([], [])                 # two topics, one hit each


def test_cite_sources_from_the_first_pages():
    m = G.GraphMaps(cite_neighbours={"VKM-SRC-001": frozenset({"VKM-SRC-002", "VKM-SRC-005"}),
                                     "VKM-SRC-002": frozenset({"VKM-SRC-001"})})
    seeds, nb = G.cite_sources([p(1), p(4, "VKM-SRC-002"), p(2)], m, G.GraphParams())
    assert seeds == ["VKM-SRC-001", "VKM-SRC-002"] and nb == ["VKM-SRC-005"]
    assert G.cite_sources([p(1, "VKM-SRC-009")], m, G.GraphParams()) == (["VKM-SRC-009"], [])


# ---------------------------------------------------------------------------------------------------- fusion
def test_fuse_post_keeps_the_head_and_interleaves_with_e_first_on_ties():
    order = [f"E{i}" for i in range(1, 8)]
    P = G.GraphParams(head=2)
    new, ranks = G.fuse_post(order, [("cohesion", ["X1", "E1", "X2"], 1.0)], P)
    # the head stays; X1 ties with E3 at rank 1 of the rest (E wins), then X2 ties with E4
    assert new[:2] == ["E1", "E2"] and new == ["E1", "E2", "E3", "X1", "E4", "X2", "E5", "E6", "E7"]
    assert ranks == {"cohesion": {"X1": 1, "X2": 2}}                     # the head page E1 left the leg
    lifted, _ = G.fuse_post(order, [("topics", ["E6"], 1.0)], P)
    assert lifted[:3] == ["E1", "E2", "E6"]                               # a page in both lists is lifted
    weak, _ = G.fuse_post(order, [("cites", ["X9"], 0.5)], P)
    assert weak[-1] == "X9"                                               # 0.5/61 < 1/65: after E's five
    assert G.fuse_post(order, [("cites", ["X9"], 0.0)], P) == (order, {})
    assert G.fuse_post(order, [], P) == (order, {})


def test_widen_window_and_refill_page_positions():
    window, added = G.widen_window(["A", "B"], [("concepts", ["B", "C", "D", "E"], 2), ("cites", ["C", "F"], 5)])
    assert window == ["A", "B", "C", "D", "F"] and added == {"C": "concepts", "D": "concepts", "F": "cites"}
    kind = {"A": "PAGE", "F1": "FIGURE", "B": "PAGE"}
    assert G.on_pages(["A", "F1", "B"], kind, ["B", "X", "A"]) == ["B", "F1", "X", "A"]


def test_expansion_texts_skip_the_query_and_repeats():
    res = {"expansions": [{"kind": "equivalents", "text": "расчёт  МКЦ"}, {"kind": "narrower", "text": "Расчёт МКЦ"},
                          {"kind": "narrower", "text": "расчёт целиков"}]}
    assert G.expansion_texts(res, "расчёт целиков") == ["расчёт МКЦ"]
    assert G.expansion_texts(None, "x") == []


# ---------------------------------------------------------------------------------------------------- GraphRun gates
def test_graph_run_gates_and_record():
    run = G.GraphRun(("collapse", "cohesion"), G.GraphParams(), None)
    run.gate(page_kind=True, late=False)
    assert run.status["collapse"] == run.status["cohesion"] == "SKIPPED_LATE_OFF" and not run.on("collapse")
    run = G.GraphRun(("collapse", "cohesion"), G.GraphParams(), None)
    run.gate(page_kind=True, late=True)
    assert run.status["cohesion"] == "UNAVAILABLE" and run.warnings[0].startswith("GRAPH_UNAVAILABLE")
    run = G.GraphRun(("collapse", "cohesion", "topics"), G.GraphParams(), G.StaticSignals(G.GraphMaps()))
    run.gate(page_kind=False, late=True)
    assert run.on("collapse") and run.status["cohesion"] == run.status["topics"] == "SKIPPED_NO_PAGE_KIND"
    rec = run.record()
    assert rec["requested"] == ["collapse", "cohesion", "topics"] and rec["status"]["cites"] == "OFF"
    assert rec["review_status"] == "AUTO_EXTRACTED_UNREVIEWED" and json.dumps(rec)
    assert G.GraphRun((), G.GraphParams(), None).record().startswith("NOT_RUN")


# ---------------------------------------------------------------------------------------------------- hybrid_search
httpx = pytest.importorskip("httpx")

from vkm_corpus.search.fakes import FakeOpenSearch  # noqa: E402
from vkm_corpus.search.hybrid import EmbedClient, HybridRequest, hybrid_search  # noqa: E402
from vkm_corpus.search.query import SearchRequestError  # noqa: E402

DIM = 4
META = {"build_id": "20260929t120000z-aaaaaaaa-bbbbbbbb", "built_from_snapshot_id": "SNAP-T", "dimension": DIM,
        "model_key": "nano", "config_signature": "c" * 64, "space_type": "innerproduct", "build_status": "COMPLETE"}
STORE = {"pack_id": "SNAP-T-0123456789ab", "snapshot_id": "SNAP-T", "config_signature": "d" * 64, "count": 9}
Q = "расчёт целиков"
X = "расчёт МКЦ"                                          # the G3 wording (synthetic expansion)


def _client(bm25=None):
    """Pages p1..p6 of source 001 by BM25 (p1 best); the query X finds p9; a source filter on 004 finds p8 of 004."""
    lists = bm25 or {Q: [p(1), p(2), p(3), p(4), p(5), p(6)], X: [p(9)]}

    def answer(index, body):
        if not index.startswith("vkm-pages"):
            return []
        text = json.dumps(body, ensure_ascii=False)
        if '"VKM-SRC-004"' in text:
            pages = [p(8, "VKM-SRC-004")]
        else:
            pages = next((v for k, v in lists.items() if f'"{k}"' in text), [])
        return [{"_id": pid, "_index": "vkm-pages-m1-b-test", "_score": 10.0 - i,
                 "_source": {"id": pid, "object_type": "PAGE", "source_id": pid.split(":")[0],
                             "work_id": "VKM-WRK-001", "page_id": pid}} for i, pid in enumerate(pages)]

    c = FakeOpenSearch(bm25=answer)
    name = f"vkm-vectors-m1-{META['build_id']}"
    c.indices.create(index=name, body={"mappings": {"_meta": dict(META)}})
    c.indices.update_aliases({"actions": [{"add": {"index": name, "alias": "vkm-vectors"}}]})
    return c


def _rx(scores):
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append((request.url.path, body))
        if request.url.path == "/embed/query":
            return httpx.Response(200, json={"dense": {"model": "nano", "signature": "q" * 64, "dimension": DIM,
                                                       "vector": [1.0, 0.0, 0.0, 0.0]}})
        res = [{"id": t["id"], "kind": t["kind"], "status": "SCORED" if t["id"] in scores else "NO_TOKENS",
                "late_score": scores.get(t["id"])} for t in body["targets"]]
        return httpx.Response(200, json={"model": "mlateon", "query_signature": "l" * 64, "n_query_tokens": 5,
                                         "store": STORE, "timings_ms": {}, "results": res})

    client = EmbedClient("http://rx580:8790", "tok", transport=httpx.MockTransport(handler))
    client.calls = calls
    return client


LATE = {p(1): 9.0, p(2): 8.0, p(3): 7.0, p(4): 6.0, p(5): 5.0, p(6): 4.0, p(7): 3.5, p(9): 3.0,
        p(8, "VKM-SRC-004"): 2.0}


def _signals():
    m = maps(sections=[("SEC-x", 2)], section_pages=[("SEC-x", p(i), i) for i in (1, 2, 7)],
             dup_members=[("DCL-1", "SAME_WORK_COPY", p(1), 900), ("DCL-1", "SAME_WORK_COPY", p(3), 900)],
             page_chars={p(1): 1000, p(3): 1000},
             cites=[("VKM-WRK-001", "VKM-WRK-004")],
             source_works=[("VKM-SRC-001", "VKM-WRK-001"), ("VKM-SRC-004", "VKM-WRK-004")])
    return G.StaticSignals(m, {Q: {"query": Q, "expansions": [{"kind": "equivalents", "text": X, "terms": []}]}})


def _run(graph, params=None, late=True, defaults=(), signals="synthetic"):
    rx = _rx(LATE)
    out = hybrid_search(_client(), rx, HybridRequest(query=Q, candidates=10, late=late, graph=graph), "vkm",
                        graph=_signals() if signals == "synthetic" else signals,
                        graph_params=params or G.GraphParams(head=1, cites_mode="post"), graph_defaults=defaults)
    return out, rx


def test_no_stage_leaves_e_unchanged():
    out, _ = _run(None)
    assert [h["id"] for h in out["hits"]] == [p(i) for i in range(1, 7)]
    assert out["stages"]["graph"].startswith("NOT_RUN") and "copies" not in out["hits"][0]
    assert all("graph" not in h["trace"] for h in out["hits"])


def test_collapse_lists_the_copy_on_the_hit():
    out, _ = _run(("collapse",))
    ids = [h["id"] for h in out["hits"]]
    assert p(3) not in ids and ids[:2] == [p(1), p(2)]
    assert out["hits"][0]["copies"] == [{"page_id": p(3), "coverage": 0.9, "kinds": ["SAME_WORK_COPY"]}]
    assert out["hits"][1]["copies"] == [] and out["hits"][0]["trace"]["graph"]["copies"] == 1
    g = out["stages"]["graph"]
    assert g["status"]["collapse"] == "APPLIED" and g["collapse"]["collapsed"] == 1
    assert g["nav_snapshot_id"] == "SNAP-T" and "graph_collapse" in out["timings_ms"]


def test_cohesion_brings_the_other_page_of_the_section_after_the_head():
    out, _ = _run(("cohesion",), G.GraphParams(head=2, cohesion_top=2, cohesion_weight=1.0))
    ids = [h["id"] for h in out["hits"]]
    # p1, p2 (the head) share SEC-x with p7, which no leg found: it enters right after the head (ties: E first)
    assert ids[:2] == [p(1), p(2)] and ids[2:4] == [p(3), p(7)]
    t7 = next(h for h in out["hits"] if h["id"] == p(7))["trace"]
    assert t7["graph"]["legs"] == {"cohesion": 1} and "e_rank" not in t7["graph"] and t7["fused_rank"] is None
    assert out["stages"]["graph"]["cohesion"]["sections"][0]["section_id"] == "SEC-x"


def test_concepts_post_and_window_modes():
    out, rx = _run(("concepts",), G.GraphParams(head=6))
    ids = [h["id"] for h in out["hits"]]
    assert ids[:6] == [p(i) for i in range(1, 7)] and ids[6] == p(9)            # after the protected head
    g = out["stages"]["graph"]["concepts"]
    assert g["expansions"][0]["text"] == X and g["leg"] == 1
    late_targets = [t["id"] for path, body in rx.calls if path == "/search/late" for t in body["targets"]]
    assert p(9) not in late_targets                                             # post: the window is E's
    win, rx2 = _run(("concepts",), G.GraphParams(head=6, concepts_mode="window"))
    targets = [t["id"] for path, body in rx2.calls if path == "/search/late" for t in body["targets"]]
    assert p(9) in targets and len(targets) == 7                                # the window widened by one
    h9 = next(h for h in win["hits"] if h["id"] == p(9))
    assert h9["trace"]["graph"]["window_leg"] == "concepts" and h9["trace"]["late_score"] == 3.0
    assert win["stages"]["late"]["graph_window_added"] == 1


def test_cites_leg_searches_the_neighbour_sources():
    out, _ = _run(("cites",), G.GraphParams(head=6, cites_weight=1.0, cites_mode="post"))
    assert out["hits"][6]["id"] == p(8, "VKM-SRC-004")
    g = out["stages"]["graph"]["cites"]
    assert g["seed_sources"] == ["VKM-SRC-001"] and g["neighbour_sources"] == 1 and g["leg"] == 1


def test_late_off_and_unknown_stage():
    out, _ = _run(("collapse", "cohesion"), late=False)
    assert out["stages"]["graph"]["status"]["collapse"] == "SKIPPED_LATE_OFF"
    assert [h["id"] for h in out["hits"]][:3] == [p(1), p(2), p(3)]
    with pytest.raises(SearchRequestError):
        hybrid_search(_client(), _rx(LATE), HybridRequest(query=Q, late=True, graph=("pagerank",)), "vkm")


def test_server_default_applies_when_the_request_does_not_say():
    out, _ = _run(None, defaults=("collapse",))
    assert out["stages"]["graph"]["requested"] == ["collapse"] and p(3) not in [h["id"] for h in out["hits"]]
    out2, _ = _run((), defaults=("collapse",))                                  # [] = none, whatever the default
    assert out2["stages"]["graph"].startswith("NOT_RUN")
    # all five requested by the server without a navigation layer: E's answer, the status says so, no warning
    out3, _ = _run(None, defaults=G.STAGES, signals=None)
    assert [h["id"] for h in out3["hits"]] == [p(i) for i in range(1, 7)] and not out3["warnings"]
    assert set(out3["stages"]["graph"]["status"].values()) == {"UNAVAILABLE"}
    # a request that names a stage the server cannot run is told
    out4, _ = _run(("collapse",), signals=None)
    assert out4["warnings"] == ["GRAPH_UNAVAILABLE: the navigation layer is not configured; graph stages skipped"]


def test_all_five_stages_run_together():
    out, _ = _run(None, params=G.GraphParams(head=2, cohesion_top=2), defaults=G.STAGES)
    g = out["stages"]["graph"]
    assert g["requested"] == list(G.STAGES) and g["status"]["cohesion"] == "APPLIED"
    assert g["status"]["collapse"] == "APPLIED" and g["status"]["cites"] == "APPLIED"
    ids = [h["id"] for h in out["hits"]]
    assert p(3) not in ids and p(7) in ids                                       # G1 and G2 acted together

"""Fragments of one figure (agent Д, 13.10): FIGURE regions of one page that share a caption block are one position —
the largest region that is not tiny, at the place of the best-ranked one — and the others are listed in
``fragments``; search_text (E's BM25) and hybrid search, paging, formulations, the API record and the brief view.
Synthetic ids modelled on the live case VKM-SRC-014:p0099 (six regions with the caption block of «Рис. 3.15»)."""
from __future__ import annotations

import json

import pytest

from vkm_corpus.search import fragments as FR
from vkm_corpus.search import query as Q

PAGE, CAP = "VKM-SRC-014:p0099", "VKM-SRC-014:p0099:be8088286ff51"
BIG = f"{PAGE}:f3348c0ecb2c0"                      # the map body: large, no caption block (as in the canon)
SWATCH = [f"{PAGE}:f652e09908f2c", f"{PAGE}:f81766628e97d", f"{PAGE}:f91e4006814d6", f"{PAGE}:f992d347e95a8",
          f"{PAGE}:faeb16994cd22"]                 # ≈ 30 × 14 pt legend swatches of «Рис. 3.15»
WIDE = f"{PAGE}:fc062b37b57c8"                     # 77 × 16 pt: the largest region of the caption group (still tiny)
OTHER = "VKM-SRC-020:p0005:f000000000001"          # a figure of another page
P2A, P2B = "VKM-SRC-021:p0002:f00000000000a", "VKM-SRC-021:p0002:f00000000000b"   # panels «а», «б» of one figure
P2CAP = "VKM-SRC-021:p0002:b000000000009"


def _box(x0, y0, w, h):
    return {"x0": x0, "y0": y0, "x1": x0 + w, "y1": y0 + h, "space": "PAGE_PT_TL"}


REGIONS = {s: (PAGE, CAP, _box(234.0, 472.0 + 20 * i, 29.6, 14.1)) for i, s in enumerate(SWATCH)}
REGIONS.update({WIDE: (PAGE, CAP, _box(234.0, 600.0, 76.9, 16.1)), BIG: (PAGE, None, _box(49.2, 59.5, 377.9, 383.2)),
                OTHER: ("VKM-SRC-020:p0005", "VKM-SRC-020:p0005:b000000000003", _box(60.0, 80.0, 300.0, 200.0)),
                P2A: ("VKM-SRC-021:p0002", P2CAP, _box(50.0, 60.0, 200.0, 150.0)),
                P2B: ("VKM-SRC-021:p0002", P2CAP, _box(260.0, 60.0, 240.0, 150.0))})
# E's BM25 order for the figure kind: the caption group ties (same caption) and is ordered by id, as in the live answer
FIG_ORDER = [*SWATCH[:2], OTHER, SWATCH[2], WIDE, SWATCH[3], P2A, SWATCH[4], P2B]


class _Index:
    """Fake OpenSearch: a fixed BM25 order per index (honours ``size``/``from``), ``ids`` lookups, ``msearch``."""

    def __init__(self, order=None, pages=()):
        self.order = {"vkm-figures": list(order if order is not None else FIG_ORDER), "vkm-pages": list(pages)}
        self.bodies = []

    def _source(self, oid):
        page, cap, box = REGIONS.get(oid, (oid.rsplit(":", 1)[0], None, None))
        src = {"id": oid, "object_type": "FIGURE", "source_id": oid.split(":")[0], "page_id": page}
        if cap:
            src["caption_block_id"] = cap
        if box:
            src["bbox"] = box
        return src

    def search(self, index, body):
        self.bodies.append((index, body))
        if "ids" in body.get("query", {}):
            hits = [{"_id": i, "_index": "vkm-figures-m1-b1", "_score": 1.0, "_source": self._source(i)}
                    for i in body["query"]["ids"]["values"] if i in REGIONS]
            return {"hits": {"hits": hits, "total": {"value": len(hits)}}}
        ids = self.order.get(index, [])
        start, size = body.get("from", 0), body.get("size", 10)
        hits = []
        for rank, oid in enumerate(ids[start:start + size], start + 1):
            src = self._source(oid) if index == "vkm-figures" else {"id": oid, "object_type": "PAGE",
                                                                    "source_id": oid.split(":")[0], "page_id": oid}
            hits.append({"_id": oid, "_index": f"{index}-m1-b1", "_score": 10.0 - 0.01 * rank, "_source": src})
        return {"hits": {"hits": hits, "total": {"value": len(ids)}}, "aggregations": {}}

    def msearch(self, body):
        return {"responses": [self.search(h["index"], b) for h, b in zip(body[0::2], body[1::2])]}


# ---------------------------------------------------------------- the rule
def test_region_size_and_tiny_threshold():
    r = FR.Region.of(PAGE, {"caption_block_id": CAP, "bbox": _box(0, 0, 29.6, 14.1)})
    assert r.caption_block_id == CAP and r.tiny and round(r.area) == 417
    assert not FR.Region.of(PAGE, {"bbox": _box(0, 0, 60, 30)}).tiny                       # 1800 pt², sides ≥ 20
    assert FR.Region.of(PAGE, {"bbox": _box(0, 0, 300, 19)}).tiny                           # a thin strip is tiny
    assert FR.Region.of(PAGE, {"bbox": _box(0, 0, 38, 38)}).tiny                            # 1444 pt² < 1500
    unknown = FR.Region.of(PAGE, {"caption_block_id": CAP})
    assert unknown.bbox is None and unknown.area is None and not unknown.tiny              # UNKNOWN is not small
    pixels = FR.Region.of(PAGE, {"bbox": {**_box(0, 0, 5, 5), "space": "IMAGE_PX"}})
    assert pixels.bbox is None                                                              # another space: unknown
    canon = FR.Region.of(None, {"page_id": PAGE, "caption_block_id": CAP, "bbox_x0": 1.0, "bbox_y0": 2.0,
                                "bbox_x1": 101.0, "bbox_y1": 52.0, "bbox_space": "PAGE_PT_TL"})
    assert canon.page_id == PAGE and canon.area == 5000.0


def test_one_position_per_page_and_caption_block():
    regions = {k: FR.Region.of(p, {"caption_block_id": c, "bbox": b}) for k, (p, c, b) in REGIONS.items()}
    order, info = FR.group(FIG_ORDER, regions)
    # the caption group takes the place of its best-ranked member (position 1); its representative is the widest
    # region, not the 30 × 14 pt swatch the id tie-break put first; the panels «а», «б» are one figure too
    assert order == [WIDE, OTHER, P2B]
    g = info[WIDE]
    assert g["caption_block_id"] == CAP and g["members"] == 6 and g["rank_before"] == 1
    assert g["position_from"] == SWATCH[0] and g["fragments"] == [*SWATCH[:3], SWATCH[3], SWATCH[4]]
    assert set(g["tiny"]) == {*SWATCH, WIDE}                                               # all tiny: largest wins
    assert info[P2B]["fragments"] == [P2A] and info[P2B]["rank_before"] == 7 and info[P2B]["tiny"] == []
    assert BIG not in info and OTHER not in info


def test_representative_preference_non_tiny_unknown_tiny_then_rank():
    page, cap = "VKM-SRC-030:p0001", "VKM-SRC-030:p0001:b1"
    strip = FR.Region(page, cap, (0.0, 0.0, 400.0, 15.0))         # 6000 pt² but 15 pt high: tiny
    small = FR.Region(page, cap, (0.0, 0.0, 60.0, 40.0))          # 2400 pt², not tiny
    unknown = FR.Region(page, cap, None)
    order, info = FR.group(["s", "m", "u"], {"s": strip, "m": small, "u": unknown})
    assert order == ["m"] and info["m"]["fragments"] == ["s", "u"] and info["m"]["tiny"] == ["s"]
    order, info = FR.group(["s", "u"], {"s": strip, "u": unknown})
    assert order == ["u"]                                         # unknown size before a known tiny region
    twins = {"a": FR.Region(page, cap, (0.0, 0.0, 100.0, 100.0)), "b": FR.Region(page, cap, (0.0, 0.0, 100.0, 100.0))}
    assert FR.group(["b", "a"], twins)[0] == ["b"]                # equal areas: the better rank
    # no caption block → never grouped; another page with the same block id → another group
    loose = {"x": FR.Region(page, None, (0, 0, 10, 10)), "y": FR.Region(page, None, (0, 0, 10, 10)),
             "z": FR.Region("VKM-SRC-030:p0002", cap, (0, 0, 10, 10))}
    assert FR.group(["x", "y", "z"], loose) == (["x", "y", "z"], {})


# ---------------------------------------------------------------- search_text (E's BM25)
def test_search_text_groups_figures_only_when_asked():
    plain = Q.search(_Index(), Q.SearchRequest("план шахтного поля", kinds=("FIGURE",), size=20), "vkm")
    assert [h.id for h in plain.hits] == FIG_ORDER and "fragments" not in plain.hits[0].fields   # library default
    fake = _Index()
    resp = Q.search(fake, Q.SearchRequest("план шахтного поля", kinds=("FIGURE",), size=20, group_fragments=True),
                    "vkm")
    assert [h.id for h in resp.hits] == [WIDE, OTHER, P2B] and [h.rank for h in resp.hits] == [1, 2, 3]
    rep = resp.hits[0]
    assert rep.fields["fragments"] == [*SWATCH[:3], SWATCH[3], SWATCH[4]]
    assert rep.fields["fragment_group"]["position_from"] == SWATCH[0] and rep.fields["fragment_group"]["members"] == 6
    assert resp.totals == {"FIGURE": len(FIG_ORDER)}                         # totals count index objects
    assert fake.bodies[-1][1]["size"] == FR.FRAGMENT_DEPTH and fake.bodies[-1][1]["from"] == 0


def test_search_text_pages_through_one_grouped_list():
    many = [f"VKM-SRC-040:p{i:04d}:f00000000000{j}" for i in range(1, 41) for j in (1, 2)]
    for oid in many:                                    # 40 pages, two regions sharing one caption block on each
        page = oid.rsplit(":", 1)[0]
        REGIONS[oid] = (page, f"{page}:b000000000001", _box(50.0, 50.0, 100.0 + int(oid[-1]), 100.0))
    try:
        fake = _Index(order=many)
        pages = [Q.search(fake, Q.SearchRequest("x", kinds=("FIGURE",), size=15, offset=off, group_fragments=True),
                          "vkm") for off in (0, 15, 30)]
        ids = [h.id for p in pages for h in p.hits]
        assert len(ids) == 40 and len(set(ids)) == 40 and all(i.endswith("2") for i in ids)   # the larger region
        assert [h.rank for h in pages[1].hits][:2] == [16, 17]
        # a window deeper than the grouped list of FRAGMENT_DEPTH hits makes E's list deeper (here: until exhausted)
        deep = [f"VKM-SRC-041:p{i:04d}:f00000000000{j}" for i in range(1, 161) for j in (1, 2)]
        for oid in deep:
            page = oid.rsplit(":", 1)[0]
            REGIONS[oid] = (page, f"{page}:b000000000001", _box(50.0, 50.0, 100.0, 100.0))
        fake = _Index(order=deep)
        last = Q.search(fake, Q.SearchRequest("x", kinds=("FIGURE",), size=20, offset=140, group_fragments=True),
                        "vkm")
        assert len(last.hits) == 20 and last.hits[0].rank == 141
        assert [b["size"] for _i, b in fake.bodies] == [FR.FRAGMENT_DEPTH, 2 * FR.FRAGMENT_DEPTH]
    finally:
        for oid in [k for k in REGIONS if k.startswith(("VKM-SRC-040:", "VKM-SRC-041:"))]:
            del REGIONS[oid]


def test_search_text_several_kinds_groups_the_figure_list_before_fusion():
    pages = ["VKM-SRC-001:p0001", "VKM-SRC-001:p0002", "VKM-SRC-001:p0003"]
    fake = _Index(pages=pages)
    resp = Q.search(fake, Q.SearchRequest("план", kinds=("PAGE", "FIGURE"), size=10, group_fragments=True), "vkm")
    figs = [h for h in resp.hits if h.object_type == "FIGURE"]
    assert [h.id for h in figs] == [WIDE, OTHER, P2B] and [h.rank_in_kind for h in figs] == [1, 2, 3]
    fig_body = next(b for i, b in fake.bodies if i == "vkm-figures")
    assert fig_body["size"] == FR.FRAGMENT_DEPTH
    plain = Q.search(_Index(pages=pages), Q.SearchRequest("план", kinds=("PAGE", "FIGURE"), size=10), "vkm")
    assert sum(h.object_type == "FIGURE" for h in plain.hits) == 7      # 3 pages + the first 7 regions of 9


def test_search_text_label_gate_then_grouping():
    fake = _Index()
    resp = Q.search(fake, Q.SearchRequest("рис. 3.15 план шахтного поля", kinds=("FIGURE",), size=5,
                                          group_fragments=True), "vkm")
    assert resp.hits[0].id == WIDE and "fragments" in resp.hits[0].fields
    body = fake.bodies[-1][1]
    assert body["query"]["bool"]["must"][0]["multi_match"]["query"] == "план шахтного поля"   # topic query


# ---------------------------------------------------------------- hybrid
httpx = pytest.importorskip("httpx")


def _hybrid_client(bm25_figures):
    from vkm_corpus.search.fakes import FakeOpenSearch

    index = _Index(order=bm25_figures)
    meta = {"build_id": "20261013t120000z-aaaaaaaa-bbbbbbbb", "built_from_snapshot_id": "SNAP-1", "dimension": 4,
            "model_key": "granite-311m-r2", "space_type": "innerproduct", "build_status": "COMPLETE"}

    def bm25(idx, body):
        return index.search(idx, body)["hits"]["hits"]

    c = FakeOpenSearch(bm25=bm25)
    name = f"vkm-vectors-m1-{meta['build_id']}"
    c.indices.create(index=name, body={"mappings": {"_meta": meta}})
    # dense: every region of the caption group (one text) and one more swatch the BM25 leg never returned
    for i, oid in enumerate([*SWATCH, WIDE, OTHER]):
        c.indices_[name]["docs"][f"u1-{i:012d}"] = {
            "id": f"u1-{i:012d}", "unit_kind": "FIGURE", "object_ids": [oid], "page_id": REGIONS[oid][0],
            "source_id": oid.split(":")[0], "work_id": "VKM-WRK-014", "dup_group_id": REGIONS[oid][0],
            "vector": [1.0 if oid != OTHER else 0.5, 0.0, 0.0, 0.0]}
    c.indices.update_aliases({"actions": [{"add": {"index": name, "alias": "vkm-vectors"}}]})
    return c


def _embed():
    from vkm_corpus.search.hybrid import EmbedClient

    def handler(request):
        body = json.loads(request.content)
        if request.url.path == "/search/late":
            return httpx.Response(200, json={"model": "mlateon", "query_signature": "l" * 64, "n_query_tokens": 4,
                                             "store": {"snapshot_id": "SNAP-1"}, "timings_ms": {},
                                             "results": [{"id": t["id"], "kind": t["kind"], "status": "SCORED",
                                                          "late_score": 2.0, "best_unit_id": "u", "units": 1,
                                                          "tokens": 9} for t in body["targets"]]})
        return httpx.Response(200, json={"dense": {"model": "granite-311m-r2", "vector": [1.0, 0.0, 0.0, 0.0]}})

    return EmbedClient("http://rx580-retrieval:8790", None, transport=httpx.MockTransport(handler))


def test_hybrid_groups_fragments_after_g1_with_a_lookup_for_dense_only_regions():
    from vkm_corpus.search.hybrid import HybridRequest, hybrid_search

    bm25 = [SWATCH[0], SWATCH[1], OTHER, WIDE]                  # SWATCH[2:] come from the dense leg only
    c = _hybrid_client(bm25)
    plain = hybrid_search(c, _embed(), HybridRequest(query="план", kinds=("FIGURE",), candidates=20, late=True,
                                                     graph=()), "vkm")
    assert len(plain["hits"]) == 7 and "fragments" not in plain["stages"]                  # library default: off
    assert not any("ids" in (b.get("query") or {}) for _i, b in c.searches)
    c = _hybrid_client(bm25)
    out = hybrid_search(c, _embed(), HybridRequest(query="план", kinds=("FIGURE",), candidates=20, late=True,
                                                   graph=(), group_fragments=True), "vkm")
    ids = [h["id"] for h in out["hits"]]
    assert ids == [WIDE, OTHER] and out["fused_total"] == 2
    rep = out["hits"][0]
    assert sorted(rep["fragments"]) == sorted(SWATCH) and rep["trace"]["fragments"]["members"] == 6
    assert rep["trace"]["fragments"]["rank_before"] == 1 and rep["rank"] == 1
    lookups = [b for _i, b in c.searches if "ids" in (b.get("query") or {})]
    assert len(lookups) == 1 and sorted(lookups[0]["query"]["ids"]["values"]) == sorted(SWATCH[2:])
    st = out["stages"]["fragments"]
    assert st["status"] == "APPLIED" and st["groups"] == 1 and st["fragments"] == 5 and st["looked_up"] == 3
    assert st["tiny"] == {"area_pt2": FR.TINY_AREA_PT2, "side_pt": FR.TINY_SIDE_PT}


def test_hybrid_lookup_failure_groups_the_known_regions_and_warns():
    from vkm_corpus.search.hybrid import HybridRequest, hybrid_search

    c = _hybrid_client([SWATCH[0], SWATCH[1], OTHER, WIDE])
    real = c.search

    def search(index, body):
        if "ids" in (body.get("query") or {}):
            raise ConnectionError("figures index down")
        return real(index, body)

    c.search = search
    out = hybrid_search(c, _embed(), HybridRequest(query="план", kinds=("FIGURE",), candidates=20, late=True,
                                                   graph=(), group_fragments=True), "vkm")
    assert out["stages"]["fragments"]["lookup_error"] == "ConnectionError"
    assert any(w.startswith("FRAGMENTS_LOOKUP_FAILED") for w in out["warnings"])
    rep = next(h for h in out["hits"] if h.get("fragments"))
    assert rep["id"] == WIDE and sorted(rep["fragments"]) == sorted(SWATCH[:2])           # only the known regions


def test_formulation_fusion_merges_fragments_of_one_figure():
    from vkm_corpus.search.hybrid import HybridRequest, hybrid_search

    c = _hybrid_client([SWATCH[0], WIDE, OTHER])
    out = hybrid_search(c, _embed(), HybridRequest(query="план", kinds=("FIGURE",), candidates=20, late=True,
                                                   graph=(), group_fragments=True, formulations=("схема",)), "vkm")
    ids = [h["id"] for h in out["hits"]]
    assert ids.count(WIDE) == 1 and not set(SWATCH) & set(ids)
    assert out["stages"]["formulations"]["status"] == "APPLIED"


# ---------------------------------------------------------------- API record, brief view, MCP
def _api(tmp_path, hits):
    pytest.importorskip("fastapi")
    pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")
    pytest.importorskip("PIL")
    pytest.importorskip("pytz")
    from vkm_corpus.api.fixtures import FakeHybrid, FakeSearch, hybrid_hits, synthetic_service

    service, canon, _fakes = synthetic_service(tmp_path)
    service.deps.search = FakeSearch(hits(canon))
    service.deps.hybrid = FakeHybrid(hybrid_hits(canon))
    return service, canon


def test_api_sends_the_flag_for_figures_and_shows_fragments(tmp_path):
    from vkm_corpus.api.brief import brief_response
    from vkm_corpus.api.fixtures import search_hits

    def hits(canon):
        out = search_hits(canon)
        fig = next(h for h in out if h["object_type"] == "FIGURE")
        fig["fields"] = {**fig.get("fields", {}), "fragments": ["VKM-SRC-001:p0002:f0000000000ff"],
                         "fragment_group": {"caption_block_id": "VKM-SRC-001:p0002:b0000000000aa", "members": 2,
                                            "rank_before": 3, "position_from": fig["id"], "tiny": []}}
        return out

    service, canon = _api(tmp_path, hits)
    res = service.search("мульда", ["PAGE", "FIGURE"], {}, 10, None)
    assert service.deps.search.requests[-1]["group_fragments"] is True
    fig = next(it for it in res.items if it.envelope.object_id == canon.ids["figure"])
    assert fig.record["fragments"] == ["VKM-SRC-001:p0002:f0000000000ff"]
    assert fig.record["fragment_group"]["members"] == 2
    service.search("мульда", ["PAGE"], {}, 10, None)
    assert "group_fragments" not in service.deps.search.requests[-1]            # PAGE: the request is unchanged
    service.search("мульда", ["FIGURE"], {}, 10, None, group_fragments=False)
    assert "group_fragments" not in service.deps.search.requests[-1]
    body = {"ok": True, "meta": {}, "items": [{"envelope": fig.envelope.model_dump(mode="json"),
                                               "record": dict(fig.record)}]}
    brief = brief_response(body, kind="search")
    assert brief["items"][0]["fragments"] == ["VKM-SRC-001:p0002:f0000000000ff"]


def test_api_hybrid_flag_reaches_the_backend_only_for_figures(tmp_path):
    from vkm_corpus.api.fixtures import search_hits

    service, _canon = _api(tmp_path, search_hits)
    service.search_hybrid("мульда", ["FIGURE"], {}, 5, None)
    assert service.deps.hybrid.requests[-1]["group_fragments"] is True
    service.search_hybrid("мульда", ["PAGE"], {}, 5, None)
    assert "group_fragments" not in service.deps.hybrid.requests[-1]
    service.search_hybrid("мульда", ["FIGURE"], {}, 5, None, group_fragments=False)
    assert "group_fragments" not in service.deps.hybrid.requests[-1]


def test_http_and_mcp_carry_group_fragments(tmp_path):
    pytest.importorskip("mcp")
    import asyncio

    from fastapi.testclient import TestClient
    from mcp import Client

    from vkm_corpus.api.app import ApiConfig, create_app
    from vkm_corpus.api.fixtures import search_hits
    from vkm_corpus.mcp.api_client import ApiClient
    from vkm_corpus.mcp.servers import build_read_server

    service, _canon = _api(tmp_path, search_hits)
    token = "read-token-for-fragment-tests-0000000000"
    app = create_app(service, ApiConfig(read_tokens={token: "read"}))
    client = TestClient(app)
    h = {"Authorization": f"Bearer {token}"}
    assert client.post("/v1/search", headers=h, json={"query": "x", "kinds": ["FIGURE"]}).status_code == 200
    assert service.deps.search.requests[-1]["group_fragments"] is True
    client.post("/v1/search", headers=h, json={"query": "x", "kinds": ["FIGURE"], "group_fragments": False})
    assert "group_fragments" not in service.deps.search.requests[-1]
    client.post("/v1/search/hybrid", headers=h, json={"query": "x", "kinds": ["FIGURE"], "group_fragments": False})
    assert "group_fragments" not in service.deps.hybrid.requests[-1]
    server = build_read_server(ApiClient("http://vkm-api", token, transport=httpx.ASGITransport(app=app)))

    async def run():
        async with Client(server) as c:
            tools = {t.name: t for t in (await c.list_tools()).tools}
            await c.call_tool("search_text", {"query": "x", "kinds": ["FIGURE"], "group_fragments": False})
            a = service.deps.search.requests[-1]
            await c.call_tool("search_text", {"query": "x", "kinds": ["FIGURE"]})
            b = service.deps.search.requests[-1]
            await c.call_tool("search_hybrid", {"query": "x", "kinds": ["FIGURE"]})
            return tools, a, b, service.deps.hybrid.requests[-1]

    tools, a, b, hy = asyncio.run(run())
    assert "group_fragments" in tools["search_text"].input_schema["properties"]
    assert "group_fragments" in tools["search_hybrid"].input_schema["properties"]
    assert "group_fragments" not in a and b["group_fragments"] is True and hy["group_fragments"] is True

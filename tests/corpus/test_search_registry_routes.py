"""Registry routes of the hybrid search (agent Г, 13.10; ``search.registry_routes``): the author route (a registry
author's own pages — with a topic promoted inside E's first pages, without a topic first; the bibliographic route
kept), the entity route (entity-index pages of «скважина 75» — first without a topic, promoted with one), the gates
(no author / no numbered object → nothing changes; switches; no registry → UNAVAILABLE) and the promotion helper.
Fake OpenSearch and RX580 service; synthetic ids and names."""
from __future__ import annotations

import json
from typing import Any

import pytest

httpx = pytest.importorskip("httpx")

from vkm_corpus.search import authors as A  # noqa: E402
from vkm_corpus.search import entities as E  # noqa: E402
from vkm_corpus.search import registry_routes as RR  # noqa: E402
from vkm_corpus.search.fakes import FakeOpenSearch  # noqa: E402
from vkm_corpus.search.hybrid import EmbedClient, HybridRequest, hybrid_search  # noqa: E402

DIM = 4
META = {"build_id": "20261013t120000z-aaaaaaaa-bbbbbbbb", "built_from_snapshot_id": "SNAP-R", "dimension": DIM,
        "model_key": "nano", "config_signature": "c" * 64, "space_type": "innerproduct", "build_status": "COMPLETE"}
CITING = [f"VKM-SRC-{n:03d}:p0101" for n in range(60, 68)]          # pages that cite the author (E's order)
OWN = ["VKM-SRC-044:p0050", "VKM-SRC-025:p0020"]                   # pages of his own works on the topic
TITLE = ["VKM-SRC-044:p0001", "VKM-SRC-025:p0001", "VKM-SRC-044:p0002", "VKM-SRC-025:p0002"]
T31, MAP, BIB = "VKM-SRC-012:p0064", "VKM-SRC-012:p0028", "VKM-SRC-011:p0159"     # entity-index pages of «75»
PHRASE = [f"VKM-SRC-0{n:02d}:p0005" for n in range(70, 76)]       # E's pages for «скважина 75»
PAGES = {**{p: 0 for p in CITING + PHRASE}, **{p: int(p[-4:]) for p in OWN + TITLE + [T31, MAP, BIB]}}


def _walk(node: Any):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _terms(body: dict, field: str) -> set[str] | None:
    for n in _walk(body):
        t = n.get("terms") if isinstance(n.get("terms"), dict) else None
        if t and field in t:
            return set(t[field])
    return None


def _text(body: dict) -> str:
    return next((n["multi_match"]["query"] for n in _walk(body) if isinstance(n.get("multi_match"), dict)), "")


def _hit(p: str, score: float) -> dict:
    return {"_id": p, "_index": "vkm-pages-m1-b", "_score": score,
            "_source": {"id": p, "object_type": "PAGE", "page_id": p, "source_id": p[:11],
                        "page_index": PAGES.get(p, 1), "dup_group_id": p}}


def _client(e_pages: list[str], topical: dict[str, list[str]] | None = None) -> FakeOpenSearch:
    """``e_pages``: E's BM25 order for the query; ``topical``: BM25 order of a topic inside a source/page filter."""
    topical = topical or {}

    def answer(index: str, body: dict) -> list[dict]:
        if not index.startswith("vkm-pages"):
            return []
        text = json.dumps(body, ensure_ascii=False)
        ids = next((n["ids"]["values"] for n in _walk(body) if isinstance(n.get("ids"), dict)), None)
        if ids is not None:                                         # page_filter: every asked page passes
            return [_hit(p, 1.0) for p in ids if p in PAGES]
        sources = _terms(body, "source_id")
        if '"page_index"' in text and '"range"' in text:            # the author's first pages
            pages = [p for p in TITLE if p[:11] in (sources or set())]
            return [_hit(p, 1.0) for p in sorted(pages, key=lambda p: (p[:11], p))]
        allowed_pages = _terms(body, "page_id")
        q = _text(body)
        if sources is not None or allowed_pages is not None:        # a topic inside sources / pages
            order = topical.get(q, [])
            keep = [p for p in order if (sources is None or p[:11] in sources)
                    and (allowed_pages is None or p in allowed_pages)]
            return [_hit(p, 10.0 - i) for i, p in enumerate(keep)]
        return [_hit(p, 10.0 - i * 0.1) for i, p in enumerate(e_pages)]

    c = FakeOpenSearch(bm25=answer)
    name = f"vkm-vectors-m1-{META['build_id']}"
    c.indices.create(index=name, body={"mappings": {"_meta": dict(META)}})
    c.indices.update_aliases({"actions": [{"add": {"index": name, "alias": "vkm-vectors"}}]})
    return c


def _rx(late: dict[str, float], scan: list[str] = ()) -> EmbedClient:
    def handler(request):
        body = json.loads(request.content)
        if request.url.path == "/embed/query":
            return httpx.Response(200, json={"dense": {"model": "nano", "signature": "q" * 64, "dimension": DIM,
                                                       "vector": [1.0, 0.0, 0.0, 0.0]}})
        if body.get("scan_kind"):                                   # the bibliographic channel: citing pages
            return httpx.Response(200, json={"model": "mlateon", "query_signature": "l" * 64, "store": {},
                                             "timings_ms": {}, "scan_units": 10,
                                             "scan": [{"page_id": p, "late_score": 5.0 - i, "best_unit_id": None}
                                                      for i, p in enumerate(scan)]})
        return httpx.Response(200, json={"model": "mlateon", "query_signature": "l" * 64, "n_query_tokens": 5,
                                         "store": {}, "timings_ms": {},
                                         "results": [{"id": t["id"], "kind": t["kind"], "status": "SCORED",
                                                      "late_score": late.get(t["id"], 0.5)} for t in body["targets"]]})
    return EmbedClient("http://rx580:8790", "tok", transport=httpx.MockTransport(handler))


def _authors() -> A.AuthorIndex:
    authors = [{"author_id": "AUT-b", "name_display": "Барях А.А.", "name_key": "барях|аа||CYRL", "script": "CYRL"},
               {"author_id": "AUT-k", "name_display": "Кудряшов А.И.", "name_key": "кудряшов|аи||CYRL",
                "script": "CYRL"}]
    wa = [{"work_id": "VKM-WRK-044", "author_id": "AUT-b", "ordinal": 1, "role": "AUTHOR",
           "name_as_listed": "Барях А.А."},
          {"work_id": "VKM-WRK-025", "author_id": "AUT-b", "ordinal": 1, "role": "AUTHOR",
           "name_as_listed": "Барях А.А."}]
    works = [{"work_id": "VKM-WRK-044", "publication_year": 2010, "status": "ACTIVE"},
             {"work_id": "VKM-WRK-025", "publication_year": 2008, "status": "ACTIVE"}]
    src = [{"work_id": "VKM-WRK-044", "source_id": "VKM-SRC-044", "link_type": "FULL_COPY", "is_primary": True},
           {"work_id": "VKM-WRK-025", "source_id": "VKM-SRC-025", "link_type": "FULL_COPY", "is_primary": True}]
    return A.AuthorIndex.from_rows(authors, wa, works, src, "SNAP")


def _entities() -> E.EntityIndex:
    def row(rid, eid, pid, records, site):
        return {**{c: "" for c in E.COLUMNS}, "entity_row_id": rid, "entity_kind": "BOREHOLE", "entity_id": eid,
                "number": "75", "number_key": "75", "site": site, "source_id": pid[:11], "page_id": pid,
                "page_index": str(int(pid[-4:])), "record_ids": records}
    return E.EntityIndex.from_rows([row("ENT-1", "BH-SOL-75", MAP, "BH-SOL-75", "СКРУ-1"),
                                    row("ENT-2", "BH-SOL-75", T31, "BH-SOL-75;PK-1;PK-2", "СКРУ-1"),
                                    row("ENT-3", "BH-BIB-75", BIB, "BH-BIB-75", "библиография")], "pack-1")


REGISTRY = RR.RegistrySignals(authors=_authors(), entities=_entities())


def _run(query: str, client: FakeOpenSearch, *, late: dict | None = None, scan=(), registry=REGISTRY, **kw):
    req = HybridRequest(query=query, candidates=20, late=True, graph=(), **kw)
    return hybrid_search(client, _rx(late or {}, scan), req, "vkm", registry=registry)


def ids(out):
    return [h["id"] for h in out["hits"]]


# ---------------------------------------------------------------------------------------------------- author
def test_author_route_with_a_topic_promotes_his_own_pages_before_the_citing_ones():
    q = "работы Баряха по сдвижению"
    client = _client(CITING + OWN, topical={"по сдвижению": OWN})
    late = {p: 9.0 - i for i, p in enumerate(CITING)}                 # late prefers the citing pages
    out = _run(q, client, late=late, scan=CITING[:3])
    assert ids(out)[:2] == OWN and set(ids(out)) == set(CITING + OWN)
    st = out["stages"]["author_route"]
    assert st["status"] == "APPLIED" and st["mode"] == "TOPIC" and st["topic"] == "по сдвижению"
    assert [a["name_display"] for a in st["authors"]] == ["Барях А.А."] and st["authors"][0]["method"] == "INFLECTED"
    assert st["sources"] == 2 and st["pages"] == 2 and st["moved"] == 2 and st["in_head"] == 2
    assert st["mentions"] == [{"cue": "works_of", "text": "Баряха", "author_ids": ["AUT-b"]}]
    assert out["stages"]["bib_route"]["status"] == "APPLIED"                       # the bibliographic route stays
    tr = out["hits"][0]["trace"]
    assert tr["author_rank"] == 1 and tr["author"]["work_id"] == "VKM-WRK-044" and tr["author"]["mode"] == "TOPIC"
    assert tr["author"]["page_rank_before"] > 2 and tr["author"]["author_ids"] == ["AUT-b"]
    assert "RRF leg" in st["fusion"] and sum("author_rank" in h["trace"] for h in out["hits"]) == 2


def test_author_route_without_a_topic_puts_the_title_pages_first():
    out = _run("работы Баряха", _client(CITING), scan=CITING[:3])
    assert ids(out)[:4] == TITLE                       # page 1 of each work, then page 2 (lead author, newer first)
    st = out["stages"]["author_route"]
    assert st["mode"] == "FIRST_PAGES" and st["topic"] is None and st["pages"] == 4
    assert all(out["hits"][i]["trace"]["author"]["mode"] == "FIRST_PAGES" for i in range(4))


def test_a_citation_query_keeps_the_citing_pages_first():
    """«ссылки на работы X» asks for the citations: the author's pages stay one RRF leg, nothing is promoted."""
    late = {p: 9.0 - i for i, p in enumerate(CITING)}
    out = _run("ссылки на работы Баряха по сдвижению", _client(CITING + OWN, {"по сдвижению": OWN}), late=late,
               scan=CITING[:3])
    st = out["stages"]["author_route"]
    assert st["status"] == "APPLIED" and st["promotion"] == "OFF_CITATION_QUERY" and "moved" not in st
    assert ids(out)[0] == CITING[0] and "citation_phrase" in st["rule"]
    assert any(h["trace"].get("author_rank") for h in out["hits"])                 # the leg is still fused


def test_queries_without_a_registry_author_are_unchanged():
    client = _client(CITING)
    plain = _run("оседание земной поверхности", client)
    bare = _run("оседание земной поверхности", _client(CITING), registry=None)
    assert ids(plain) == ids(bare) == CITING
    assert plain["stages"]["author_route"] == {"status": "NOT_DETECTED"}
    assert all("author" not in h["trace"] for h in plain["hits"])
    # an eponym or a surname outside the registry: no leg either
    assert _run("закон Бингама", _client(CITING))["stages"]["author_route"]["status"] == "NOT_DETECTED"
    miss = _run("работы Петрова", _client(CITING))["stages"]["author_route"]
    assert miss["status"] == "NO_REGISTRY_MATCH" and "authors" not in miss


def test_author_route_switch_and_missing_registry():
    off = _run("работы Баряха по сдвижению", _client(CITING + OWN, {"по сдвижению": OWN}), author_route=False)
    assert off["stages"]["author_route"] == {"status": "OFF"} and ids(off)[:2] != OWN
    none = _run("работы Баряха по сдвижению", _client(CITING), registry=RR.RegistrySignals())
    assert none["stages"]["author_route"]["status"] == "UNAVAILABLE"


# ---------------------------------------------------------------------------------------------------- entity
def test_entity_route_without_a_topic_puts_the_index_pages_first():
    out = _run("скважина 75", _client(PHRASE))
    assert ids(out)[:3] == [T31, MAP, BIB]             # the table of picks (3 records) before the map labels
    er = out["stages"]["object_ref"]["entity_route"]
    assert er["status"] == "APPLIED" and er["objects"] == 2 and er["rows"] == 3 and er["pages"] == 3
    assert er["sites"] == ["СКРУ-1", "библиография"] and er["pack_id"] == "pack-1"
    t31 = out["hits"][0]["trace"]["object_ref"]
    assert t31["matched"] == ["entity"] and t31["entity"]["entity_ids"] == ["BH-SOL-75"]
    assert t31["entity"]["rank"] == 1 and t31["page_rank_before"] is not None
    assert out["stages"]["object_ref"]["moved"] >= 3


def test_entity_route_with_a_topic_lets_the_topic_rank():
    q = "скважина 75 мощность карналлитовой зоны"
    out = _run(q, _client(PHRASE, topical={"мощность карналлитовой зоны": [T31]}))
    er = out["stages"]["object_ref"]["entity_route"]
    assert er["topic_matched"] == 1 and "with a topic" in er["rule"]
    hit = next(h for h in out["hits"] if h["id"] == T31)
    assert hit["trace"]["object_ref"]["entity"]["topic_match"] is True
    # the index pages are promoted inside E's first pages in their fused order: the topic page first
    assert ids(out)[0] == T31 and {T31, MAP, BIB} <= set(ids(out)[:3])


def test_entity_route_gates():
    off = _run("скважина 75", _client(PHRASE), entity_route=False)
    assert off["stages"]["object_ref"]["entity_route"] == {"status": "OFF"} and T31 not in ids(off)
    none = _run("скважина 75", _client(PHRASE), registry=RR.RegistrySignals())
    assert none["stages"]["object_ref"]["entity_route"]["status"] == "UNAVAILABLE"
    fig = _run("рис. 3.1", _client(PHRASE))                 # a printed figure number is no catalogued object
    assert "entity_route" not in fig["stages"]["object_ref"]
    other = _run("скважина 76", _client(PHRASE))
    assert other["stages"]["object_ref"]["entity_route"]["status"] == "NOT_IN_INDEX"


# ---------------------------------------------------------------------------------------------------- helper
def test_promote_inside_k_and_in_front():
    kind = {"A": "PAGE", "B": "PAGE", "F": "FIGURE", "C": "PAGE", "D": "PAGE"}
    order, moved = RR.promote(["A", "B", "F", "C", "D"], kind, ["C", "D"], k=3)
    assert order == ["C", "A", "F", "B", "D"] and moved == {"C": 3}                # D lies beyond the first 3 pages
    order, moved = RR.promote(["A", "B", "F", "C", "D"], kind, ["D", "C"], front=True)
    assert order == ["D", "C", "F", "A", "B"] and moved == {"D": 4, "C": 3}
    assert RR.promote(["A", "B"], kind, ["A"], k=3) == (["A", "B"], {})


def test_registry_signals_status_and_errors():
    class Broken:
        def index(self):
            raise RuntimeError("no canon")

    sig = RR.RegistrySignals(authors=Broken(), entities=_entities())
    assert sig.author_index() is None and sig.errors == {"authors": "RuntimeError"}
    with pytest.raises(RuntimeError):
        sig.author_index(strict=True)
    st = sig.status()
    assert st["authors"] is None and st["entities"] == {"rows": 3, "pack_id": "pack-1"}


def test_a_bibliographic_query_with_a_topic_promotes_at_most_three_own_pages():
    """«работы X о …» with the bibliographic route reads both ways (E11 route check 13.10: 7–8 promoted own pages
    pushed the reference entries out of the first 10): at most AUTHOR_PROMOTE_MAX_BIB own pages move up."""
    own = [f"VKM-SRC-044:p00{n}" for n in range(50, 56)]
    for p in own:
        PAGES[p] = int(p[-4:])
    late = {p: 9.0 - i for i, p in enumerate(CITING)}
    out = _run("работы Баряха по сдвижению", _client(CITING + own, topical={"по сдвижению": own}), late=late,
               scan=CITING[:3])
    st = out["stages"]["author_route"]
    assert out["stages"]["bib_route"]["status"] == "APPLIED"
    assert st["promotion"] == "CAPPED_BIBLIOGRAPHIC" and st["promote_max"] == RR.AUTHOR_PROMOTE_MAX_BIB == 3
    assert st["moved"] <= 3 and sum(1 for p in ids(out)[:3] if p in own) == 3
    assert ids(out)[3] not in own                                   # the rest of the head is E's order again

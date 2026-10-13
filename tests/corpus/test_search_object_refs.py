"""Numbered objects in a query (agent Б, 13.10; ``search.object_refs``): the parser (positive and negative forms), the
topic, the query translation that keeps the number, the BM25 body (topic-gated phrase, a reference-only query, figure
labels left to the label gate, no change without a reference), the promotion inside the topic's first hits, and the
hybrid PAGE route (label route leg, promotion after late, trace and stage). Fake OpenSearch, fake RX580 service;
synthetic ids."""
from __future__ import annotations

import json

import pytest

from vkm_corpus.search import query as Q
from vkm_corpus.search.object_refs import has_topic, keep_refs, parse_refs, strip_refs


def refs(text):
    return [(r.kind, r.number) for r in parse_refs(text)]


# ---------------------------------------------------------------------------------------------------- parser
@pytest.mark.parametrize("text, expected", [
    ("скважина 75", [("BOREHOLE", "75")]),
    ("скв. 75", [("BOREHOLE", "75")]),
    ("скв.75", [("BOREHOLE", "75")]),
    ("скважина № 75", [("BOREHOLE", "75")]),
    ("данные скважины №75 и керн", [("BOREHOLE", "75")]),
    ("скважине 75а", [("BOREHOLE", "75а")]),
    ("скв. 1-С", [("BOREHOLE", "1-с")]),
    ("borehole 75", [("BOREHOLE", "75")]),
    ("профильная линия 12", [("LINE", "12")]),
    ("график по профильной линии № 8", [("LINE", "8")]),
    ("наблюдательная линия 3", [("LINE", "3")]),
    ("рудник СКРУ-1 блок 2", [("BLOCK", "2")]),
    ("расположение блока 201 и профильной линии 12", [("BLOCK", "201"), ("LINE", "12")]),
    ("панель 5", [("PANEL", "5")]),
    ("панели № 4-6", [("PANEL", "4-6")]),
    ("репер 104", [("BENCHMARK", "104")]),
    ("рис. 3.1 Соловьев Секунцов", [("FIGURE", "3.1")]),
    ("на рисунке 2.10", [("FIGURE", "2.10")]),
    ("Fig. 4", [("FIGURE", "4")]),
    ("табл. 3.1", [("TABLE", "3.1")]),
    ("таблица 2", [("TABLE", "2")]),
    ("формула (3.12)", [("FORMULA", "3.12")]),
    ("по формуле 3.12", [("FORMULA", "3.12")]),
])
def test_parser_recognises_numbered_objects(text, expected):
    assert refs(text) == expected


@pytest.mark.parametrize("text", [
    "75 м", "УДК О-75", "2.1 МПа", "СКРУ-1", "рудник СКРУ-2", "глубина 75 м скважины", "скважина 1975 г.",
    "блок 25 м", "блок-схема 3", "блоки 2,5", "линия 2-го порядка", "блок 3-й очереди", "линия 10 кВ", "сквер 5",
    "модуль деформации 2,5 ГПа", "оседание земной поверхности", "(3.12)",
])
def test_parser_negatives_are_not_object_numbers(text):
    assert refs(text) == []


def test_topic_and_translation_keep_the_number():
    q = "скважина 75 скорость продольных волн"
    assert strip_refs(q) == "скорость продольных волн" and has_topic(strip_refs(q))
    assert strip_refs("скважина 75") == "" and not has_topic(strip_refs("по профильной линии № 8"))
    assert not has_topic("и 76") and has_topic("Соловьев Секунцов")
    # the translation lost the number: it goes after the English type word, or the English reference is appended
    assert keep_refs("скважина 75", "borehole") == "borehole 75"
    assert keep_refs(q, "borehole longitudinal wave velocity") == "borehole 75 longitudinal wave velocity"
    assert keep_refs("рис. 3.1 мульда", "subsidence trough") == "subsidence trough figure 3.1"
    assert keep_refs("скважина 75", "borehole 75") == "borehole 75"                     # nothing lost
    assert keep_refs("мульда сдвижения", "subsidence trough") == "subsidence trough"    # no reference: unchanged
    assert keep_refs("скважина 75", None) is None
    assert keep_refs("скважина 75 керн", "керн скважины", english=False) == "керн скважины скважина 75"


def test_api_translation_keeps_the_number():
    from vkm_corpus.api.service import ApiDeps, ApiService

    class Nav:
        def run(self, name, text, **kw):
            return {"translation": {"скважина 75": "borehole", "мульда": "trough"}.get(text), "terms": []}

    svc = ApiService(ApiDeps(canon=None, nav=Nav()))
    assert svc._query_translation("скважина 75") == (["borehole 75"], svc._query_translation("скважина 75")[1])
    assert svc._query_translation("скважина 75")[1]["text"] == "borehole 75"
    assert svc._query_translation("мульда")[0] == ["trough"]


# ---------------------------------------------------------------------------------------------------- BM25 body
def _body(query, index_type="pages", **kw):
    return Q.build_body(index_type, Q.SearchRequest(query, **kw), size=10, offset=0)


def test_body_without_a_reference_is_the_old_body():
    body = _body("оседание земной поверхности")
    must = body["query"]["bool"]["must"][0]
    assert must == {"multi_match": {"query": "оседание земной поверхности", "type": "best_fields",
                                    "fields": Q.FIELDS["pages"], "tie_breaker": 0.2,
                                    "minimum_should_match": "3<70%"}}
    assert Q.OBJECT_REF_NAME not in json.dumps(body, ensure_ascii=False)


def test_body_topic_ranks_and_the_reference_only_names_its_hits():
    body = _body("скважина 75 скорость продольных волн")
    must = body["query"]["bool"]["must"][0]["multi_match"]
    assert must["query"] == "скорость продольных волн" and must["type"] == "cross_fields"
    assert must["fields"] == [*Q.FIELDS["pages"], *Q.REF_TOPIC_FIELDS]
    named = [c for c in body["query"]["bool"]["should"] if c.get("bool", {}).get("_name") == Q.OBJECT_REF_NAME]
    assert len(named) == 1 and named[0]["bool"]["boost"] == 0.0
    phrases = {c["match_phrase"]["text"]["query"] for c in named[0]["bool"]["should"]}
    assert phrases == {"скважина 75", "скв 75", "borehole 75"}


def test_body_reference_only_matches_the_phrase_or_the_terms():
    body = _body("скв. 75")
    must = body["query"]["bool"]["must"][0]["bool"]
    assert must["minimum_should_match"] == 1 and must["should"][0]["multi_match"]["query"] == "скв. 75"
    assert {c["match_phrase"]["text"]["query"] for c in must["should"][1]["bool"]["should"]} == \
        {"скважина 75", "скв 75", "borehole 75"}
    named = [c for c in body["query"]["bool"]["should"] if c.get("bool", {}).get("_name") == Q.OBJECT_REF_NAME]
    assert named[0]["bool"]["boost"] == Q.OBJECT_REF_BOOST
    # a figure number on the figure index stays with the label gate; a borehole in a table caption is boosted
    fig = _body("рис. 3.1", "figures")
    assert Q.OBJECT_REF_NAME not in json.dumps(fig, ensure_ascii=False)
    tab = _body("скважина 75 скорость", "tables")
    assert tab["query"]["bool"]["must"][0]["multi_match"]["query"] == "скважина 75 скорость"     # the query itself
    assert Q.OBJECT_REF_NAME in json.dumps(tab, ensure_ascii=False)


class _Pages:
    """Fake OpenSearch: page hits in a fixed topic order; ``holding`` hold the reference phrase (named query)."""

    def __init__(self, order, holding):
        self.order, self.holding, self.bodies = order, set(holding), []

    def search(self, index, body):
        self.bodies.append(body)
        named = Q.OBJECT_REF_NAME in json.dumps(body, ensure_ascii=False)
        hits = [{"_id": p, "_index": "vkm-pages-m1-b1", "_score": 100.0 - i,
                 "_source": {"id": p, "object_type": "PAGE", "page_id": p, "source_id": p[:11]},
                 **({"matched_queries": [Q.OBJECT_REF_NAME]} if named and p in self.holding else {})}
                for i, p in enumerate(self.order[:body["size"]])]
        return {"hits": {"hits": hits, "total": {"value": len(hits)}}, "aggregations": {}}


def test_search_promotes_the_pages_holding_the_reference_inside_the_topic():
    pages = [f"VKM-SRC-0{i:02d}:p0001" for i in range(1, 9)]
    fake = _Pages(pages, holding={pages[5]})
    resp = Q.search(fake, Q.SearchRequest("скважина 75 скорость продольных волн", size=4), "vkm")
    assert [h.id for h in resp.hits] == [pages[5], pages[0], pages[1], pages[2]]
    first = resp.hits[0]
    assert first.fields["object_ref_matched"] and first.fields["object_ref_promoted"]
    assert first.fields["topic_rank"] == 6
    assert fake.bodies[-1]["size"] == Q.OBJECT_REF_TOPIC_K
    # a query without a reference: the old single request with the asked size, nothing promoted
    plain = Q.search(fake, Q.SearchRequest("скорость продольных волн", size=4), "vkm")
    assert [h.id for h in plain.hits] == pages[:4] and fake.bodies[-1]["size"] == 4
    assert not any("object_ref_promoted" in h.fields for h in plain.hits)


# ---------------------------------------------------------------------------------------------------- hybrid
httpx = pytest.importorskip("httpx")

from vkm_corpus.search.fakes import FakeOpenSearch  # noqa: E402
from vkm_corpus.search.hybrid import EmbedClient, HybridRequest, hybrid_search, promote_ref_pages  # noqa: E402

DIM = 4
META = {"build_id": "20261013t120000z-aaaaaaaa-bbbbbbbb", "built_from_snapshot_id": "SNAP-R", "dimension": DIM,
        "model_key": "nano", "config_signature": "c" * 64, "space_type": "innerproduct", "build_status": "COMPLETE"}
P = [f"VKM-SRC-014:p{i:04d}" for i in range(1, 9)]
FIG = "VKM-SRC-014:p0080:f0123456789ab"
FIG_PAGE = "VKM-SRC-014:p0080"


def _client(phrase_pages=(), figures=True):
    def answer(index, body):
        text = json.dumps(body, ensure_ascii=False)
        if index.startswith("vkm-figures"):
            if not figures or '"object_label": "3.1"' not in text:
                return []
            return [{"_id": FIG, "_index": "vkm-figures-m1-b", "_score": 5.0,
                     "_source": {"id": FIG, "page_id": FIG_PAGE, "source_id": "VKM-SRC-014"}}]
        if not index.startswith("vkm-pages"):
            return []
        if '"ids"' in text:                                        # page_filter of the label route
            return [{"_id": FIG_PAGE, "_source": {"id": FIG_PAGE, "dup_group_id": FIG_PAGE}}]
        named = Q.OBJECT_REF_NAME in text
        return [{"_id": p, "_index": "vkm-pages-m1-b", "_score": 10.0 - i,
                 "_source": {"id": p, "object_type": "PAGE", "source_id": "VKM-SRC-014", "page_id": p},
                 **({"matched_queries": [Q.OBJECT_REF_NAME]} if named and p in phrase_pages else {})}
                for i, p in enumerate(P)]

    c = FakeOpenSearch(bm25=answer)
    name = f"vkm-vectors-m1-{META['build_id']}"
    c.indices.create(index=name, body={"mappings": {"_meta": dict(META)}})
    c.indices.update_aliases({"actions": [{"add": {"index": name, "alias": "vkm-vectors"}}]})
    return c


def _rx(late):
    def handler(request):
        body = json.loads(request.content)
        if request.url.path == "/embed/query":
            return httpx.Response(200, json={"dense": {"model": "nano", "signature": "q" * 64, "dimension": DIM,
                                                       "vector": [1.0, 0.0, 0.0, 0.0]}})
        return httpx.Response(200, json={"model": "mlateon", "query_signature": "l" * 64, "n_query_tokens": 5,
                                         "store": {}, "timings_ms": {},
                                         "results": [{"id": t["id"], "kind": t["kind"], "status": "SCORED",
                                                      "late_score": late.get(t["id"], 1.0)} for t in body["targets"]]})
    return EmbedClient("http://rx580:8790", "tok", transport=httpx.MockTransport(handler))


LATE = {p: 10.0 - i for i, p in enumerate(P)}


def _hybrid(query, client, **kw):
    return hybrid_search(client, _rx(LATE), HybridRequest(query=query, candidates=10, late=True, graph=(), **kw),
                         "vkm")


def test_hybrid_label_route_and_promotion_after_late():
    out = _hybrid("рис. 3.1 Соловьев Секунцов", _client(phrase_pages={P[4]}))
    ids = [h["id"] for h in out["hits"]]
    # the label route found the page of figure 3.1 (an RRF leg); after late it and the BM25 phrase page P5 move up
    assert ids[:2] == [P[4], FIG_PAGE] and set(ids) == {*P, FIG_PAGE}
    stage = out["stages"]["object_ref"]
    assert stage["refs"] == [{"kind": "FIGURE", "number": "3.1", "text": "рис. 3.1"}]
    assert stage["topic"] == "Соловьев Секунцов" and stage["label_route"]["status"] == "APPLIED"
    assert stage["status"] == "APPLIED" and stage["flagged"] == 2 and stage["moved"] == 2
    fig = next(h for h in out["hits"] if h["id"] == FIG_PAGE)["trace"]
    assert fig["object_ref"]["matched"] == ["label"] and fig["object_ref"]["label_object_id"] == FIG
    assert fig["object_ref"]["label_rank"] == 1 and fig["object_ref"]["page_rank_before"] == 9
    p5 = out["hits"][0]["trace"]["object_ref"]
    assert p5["matched"] == ["phrase"] and p5["page_rank_before"] == 5


def test_hybrid_without_a_reference_has_no_object_ref_stage():
    out = _hybrid("оседание земной поверхности", _client(phrase_pages={P[4]}))
    assert [h["id"] for h in out["hits"]] == P and "object_ref" not in out["stages"]
    assert all("object_ref" not in h["trace"] for h in out["hits"])
    # a bare number: no label route (every book has its own «рис. 3.1»)
    bare = _hybrid("рис. 3.1", _client())
    assert bare["stages"]["object_ref"]["label_route"]["status"] == "NO_TOPIC"
    assert FIG_PAGE not in [h["id"] for h in bare["hits"]]


def test_promote_ref_pages_keeps_other_kinds_in_place():
    kind = {"A": "PAGE", "B": "PAGE", "F": "FIGURE", "C": "PAGE", "D": "PAGE"}
    order, moved = promote_ref_pages(["A", "B", "F", "C", "D"], kind, {"C": ["phrase"], "D": ["label"]}, k=3)
    assert order == ["C", "A", "F", "B", "D"] and moved == {"C": 3}           # D lies beyond the first 3 pages
    assert promote_ref_pages(["A", "B"], kind, {"A": ["phrase"]}) == (["A", "B"], {})   # already in front

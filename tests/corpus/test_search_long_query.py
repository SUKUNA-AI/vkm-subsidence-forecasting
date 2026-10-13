"""Long questions in BM25 (agent M, 13.10; ``long_query``, ``vkm_corpus.search.long_query``; off by default): ``relax``
(``minimum_should_match`` "3<70% 5<50%"), ``strip`` (the question without its frame), ``relax_strip``. Without the
mode — and for a short query in every mode — the OpenSearch bodies and the answers are today's, byte for byte; the
dense leg, the late stage and the routes keep the original query; the mode travels through the API and MCP only when
set. Fake OpenSearch, fake RX580 service, fake API backends; questions written for these tests (no benchmark query)."""
from __future__ import annotations

import asyncio
import copy
import json

import pytest

httpx = pytest.importorskip("httpx")

from vkm_corpus.search import long_query as LQ  # noqa: E402
from vkm_corpus.search import query as Q  # noqa: E402
from vkm_corpus.search.fakes import FakeOpenSearch  # noqa: E402
from vkm_corpus.search.hybrid import EmbedClient, HybridRequest, hybrid_search  # noqa: E402

LONG = "Какие максимальные оседания земной поверхности наблюдались на СКРУ-1 и в каком году?"
LONG_STRIPPED = "максимальные оседания земной поверхности на СКРУ-1"
SHORT = "оседание земной поверхности"
CREEP = "Что известно о ползучести каменной соли при длительном нагружении?"
CREEP_STRIPPED = "ползучести каменной соли при длительном нагружении"
BOREHOLE = "В каком году была пробурена скважина 75 и какие пласты она вскрыла по данным керна?"
FIGURE = "Какие оседания показаны на рис. 3.1 для отработанных панелей рудника СКРУ-1?"
P1, P2, P3 = (f"VKM-SRC-001:p000{i}" for i in range(1, 4))
F1 = "VKM-SRC-001:p0001:f000000000001"


def _dump(x):
    return json.dumps(x, sort_keys=True, ensure_ascii=False, default=str)


def _boom(*_a, **_k):
    raise AssertionError("the long-question plan was consulted on the default path")


# ------------------------------------------------------------------------------------------------ the plan
def test_content_terms_count_like_the_analyzer():
    assert LQ.content_terms(LONG) == ["какие", "максимальные", "оседания", "земной", "поверхности", "наблюдались",
                                      "скру", "1", "каком", "году"]
    assert LQ.content_terms("какой модуль у каменной соли") == ["модуль", "каменной", "соли"]   # _russian_ stops
    assert LQ.content_terms("скважина № 75 рис. 3.1 керн") == ["керн"]          # numbered objects are not terms


def test_long_starts_at_six_terms():
    assert LQ.LONG_QUERY_MIN_TERMS == 6 and LQ.RELAXED_MSM == "3<70% 5<50%" and LQ.STRICT_MSM == "3<70%"
    assert not LQ.is_long("мульда сдвижения СКРУ-1")                                  # 4 terms
    assert not LQ.is_long("ползучесть каменной соли модель Нортона")                  # 5
    assert LQ.is_long("Каковы параметры мульды сдвижения на Соликамском руднике?")    # 6
    assert LQ.is_long(LONG) and LQ.is_long(CREEP)


@pytest.mark.parametrize("question,stripped", [
    (LONG, LONG_STRIPPED),
    (CREEP, CREEP_STRIPPED),
    ("Как влияет закладка выработанного пространства на скорость оседаний?",
     "закладка выработанного пространства на скорость оседаний"),
    ("Почему происходит затопление калийных рудников на Верхнекамском месторождении?",
     "затопление калийных рудников на Верхнекамском месторождении"),
    ("Расскажите, пожалуйста, о методах наблюдений за сдвижением горных пород",
     "методах наблюдений за сдвижением горных пород"),
    ("Что представляет собой водозащитная толща и какова её мощность?", "водозащитная толща и ее мощность"),
    ("В какие годы отмечались провалы и сколько их было?", "провалы"),       # edge stop words dropped too
    ("What subsidence was observed above the potash mines in which year?",
     "subsidence was observed above the potash mines"),
])
def test_strip_removes_the_question_frame(question, stripped):
    assert LQ.strip_frame(question)[0] == stripped


def test_strip_keeps_terms_numbers_and_numbered_objects():
    text, removed = LQ.strip_frame("Какие известняки, известные модели, наблюдения и наблюдаемые оседания в 1995 году "
                                   "даны на рис. 3.1, в скв. 75 и формуле (3.12)?")
    assert removed == ["какие"]
    assert text == ("известняки, известные модели, наблюдения и наблюдаемые оседания в 1995 году даны на рис. 3.1, "
                    "в скв. 75 и формуле (3.12)")
    # the numbered object stays as written («Скв.» with its dot and case)
    assert LQ.strip_frame("Где пробурена Скв. 75?")[0] == "пробурена Скв. 75"


def test_plan_statuses():
    p = LQ.plan(SHORT, "relax_strip")
    assert (p.status, p.long, p.msm, p.bm25_text, p.changed, p.strip) == ("SHORT", False, "3<70%", SHORT, False, None)
    p = LQ.plan(LONG, "relax")
    assert (p.status, p.msm, p.bm25_text, p.strip, p.changed) == ("APPLIED", "3<70% 5<50%", LONG, None, True)
    p = LQ.plan(LONG, "strip")
    assert (p.status, p.msm, p.bm25_text, p.strip, p.terms, p.terms_after_strip) == \
        ("APPLIED", "3<70%", LONG_STRIPPED, "STRIPPED", 10, 6)
    assert p.removed == ("в каком году", "какие", "наблюдались")
    p = LQ.plan(LONG, "relax_strip")
    assert (p.msm, p.bm25_text, p.stripped_text) == ("3<70% 5<50%", LONG_STRIPPED, LONG_STRIPPED)
    keywords = "оседания земной поверхности над калийными рудниками СКРУ-1"           # long, no frame
    p = LQ.plan(keywords, "strip")
    assert (p.status, p.strip, p.bm25_text, p.changed) == ("UNCHANGED", "NO_FRAME", keywords, False)
    assert LQ.plan(keywords, "relax_strip").status == "APPLIED"
    frame_only = "Расскажите пожалуйста, почему происходит, что известно и каковы?"      # 6 terms, all of them frame
    p = LQ.plan(frame_only, "strip")
    assert (p.status, p.strip, p.bm25_text) == ("UNCHANGED", "NOTHING_LEFT", frame_only)
    assert LQ.plan(LONG, "relax", msm="2<60%").msm == "3<70% 5<50%"
    assert LQ.plan(LONG, "strip", msm="2<60%").msm == "2<60%"                  # strip keeps the request's own
    rec = LQ.plan(LONG, "relax_strip").record()
    assert {k: rec[k] for k in ("mode", "status", "long", "terms", "min_terms", "msm", "strip", "stripped_text")} == {
        "mode": "relax_strip", "status": "APPLIED", "long": True, "terms": 10, "min_terms": 6, "msm": "3<70% 5<50%",
        "strip": "STRIPPED", "stripped_text": LONG_STRIPPED}
    with pytest.raises(ValueError):
        LQ.plan(LONG, "loose")


def test_mode_is_validated():
    for req in (Q.SearchRequest(SHORT, long_query="loose"), HybridRequest(query=SHORT, long_query="loose")):
        with pytest.raises(Q.SearchRequestError) as exc:
            req.validate()
        assert exc.value.code == "E_BAD_MODE"
    req, hreq = Q.SearchRequest(SHORT), HybridRequest(query=SHORT)
    req.validate()
    hreq.validate()
    assert req.long_query == hreq.long_query == "strict"


# ------------------------------------------------------------------------------------------------ BM25 (query.search)
class _Rec:
    """OpenSearch stand-in for ``query.search``: records every body (``search`` and ``msearch``), answers two pages
    or one figure."""

    def __init__(self):
        self.bodies = []

    @staticmethod
    def _resp(index):
        if "figures" in index:
            hits = [{"_id": F1, "_index": "vkm-figures-m1-b-test", "_score": 5.0,
                     "_source": {"id": F1, "object_type": "FIGURE", "source_id": "VKM-SRC-001", "page_id": P1,
                                 "object_label": "3.1"}}]
        else:
            hits = [{"_id": p, "_index": f"{index}-m1-b-test", "_score": 9.0 - i,
                     "_source": {"id": p, "object_type": "PAGE", "source_id": "VKM-SRC-001", "page_id": p}}
                    for i, p in enumerate((P2, P1))]
        return {"hits": {"hits": hits, "total": {"value": len(hits)}}, "aggregations": {}}

    def search(self, index, body):
        self.bodies.append((index, copy.deepcopy(body)))
        return self._resp(index)

    def msearch(self, body):
        pairs = list(zip(body[::2], body[1::2]))
        self.bodies += [(h["index"], copy.deepcopy(b)) for h, b in pairs]
        return {"responses": [self._resp(h["index"]) for h, _b in pairs]}


def _must(body):
    return body["query"]["bool"]["must"][0]


QUERIES = (SHORT, LONG, CREEP, BOREHOLE, FIGURE, "(3.12) ползучесть", "скважина 75")
KINDS = (("PAGE",), ("BLOCK",), ("FIGURE",), ("PAGE", "FIGURE", "TABLE"))


@pytest.mark.parametrize("query", QUERIES)
@pytest.mark.parametrize("kinds", KINDS)
def test_without_the_mode_bm25_bodies_and_answers_are_todays(query, kinds, monkeypatch):
    """No field = ``strict`` = the path that never consults the plan: same bodies, same hits in the same order."""
    plain, explicit, unplanned = _Rec(), _Rec(), _Rec()
    a = Q.search(plain, Q.SearchRequest(query, kinds=kinds), "vkm")
    b = Q.search(explicit, Q.SearchRequest(query, kinds=kinds, long_query="strict"), "vkm")
    monkeypatch.setattr(LQ, "plan", _boom)
    c = Q.search(unplanned, Q.SearchRequest(query, kinds=kinds), "vkm")
    assert _dump(plain.bodies) == _dump(explicit.bodies) == _dump(unplanned.bodies)
    assert _dump(a.as_dict()) == _dump(b.as_dict()) == _dump(c.as_dict())
    assert "long_query" not in a.as_dict() and a.query == query
    for _index, body in plain.bodies:                       # today's msm and the query as typed (or its topic)
        assert "5<50%" not in _dump(body)


@pytest.mark.parametrize("mode", ["relax", "strip", "relax_strip"])
def test_a_short_query_runs_todays_body_in_every_mode(mode):
    plain, other = _Rec(), _Rec()
    a = Q.search(plain, Q.SearchRequest(SHORT, kinds=("PAGE", "FIGURE")), "vkm")
    b = Q.search(other, Q.SearchRequest(SHORT, kinds=("PAGE", "FIGURE"), long_query=mode), "vkm")
    assert _dump(plain.bodies) == _dump(other.bodies)
    out = b.as_dict()
    rec = out.pop("long_query")
    assert _dump(out) == _dump(a.as_dict())
    assert (rec["mode"], rec["status"], rec["long"], rec["terms"], rec["msm"]) == (mode, "SHORT", False, 3, "3<70%")


def test_relax_and_strip_change_only_the_long_query_body():
    bodies, records = {}, {}
    for mode in ("strict", "relax", "strip", "relax_strip"):
        rec = _Rec()
        resp = Q.search(rec, Q.SearchRequest(LONG, long_query=mode), "vkm")
        assert resp.query == LONG and [h.id for h in resp.hits] == [P2, P1]
        bodies[mode], records[mode] = rec.bodies[0][1], resp.long_query
    mm = {m: _must(b)["multi_match"] for m, b in bodies.items()}
    assert (mm["strict"]["query"], mm["strict"]["minimum_should_match"]) == (LONG, "3<70%")
    assert (mm["relax"]["query"], mm["relax"]["minimum_should_match"]) == (LONG, "3<70% 5<50%")
    assert (mm["strip"]["query"], mm["strip"]["minimum_should_match"]) == (LONG_STRIPPED, "3<70%")
    assert (mm["relax_strip"]["query"], mm["relax_strip"]["minimum_should_match"]) == (LONG_STRIPPED, "3<70% 5<50%")
    # relax touches minimum_should_match only; strip the text of every clause (match, phrase, title boost) only
    relaxed = copy.deepcopy(bodies["relax"])
    _must(relaxed)["multi_match"]["minimum_should_match"] = "3<70%"
    assert relaxed == bodies["strict"]
    stripped = json.loads(_dump(bodies["strip"]).replace(LONG_STRIPPED, LONG))
    assert stripped == json.loads(_dump(bodies["strict"]))
    should = bodies["strip"]["query"]["bool"]["should"]
    assert should[0]["match_phrase"]["text"]["query"] == should[1]["multi_match"]["query"] == LONG_STRIPPED
    assert records["strict"] is None
    assert records["relax_strip"]["stripped_text"] == LONG_STRIPPED and records["relax"]["stripped_text"] is None


def test_strip_keeps_the_numbered_object_gates():
    stripped = LQ.strip_frame(BOREHOLE)[0]
    assert stripped == "пробурена скважина 75 и пласты она вскрыла по данным керна"
    rec = _Rec()
    Q.search(rec, Q.SearchRequest(BOREHOLE, long_query="relax_strip"), "vkm")
    body = rec.bodies[0][1]
    must = _must(body)["multi_match"]                        # the topic ranks (cross_fields), the reference lifts
    assert must["type"] == "cross_fields" and must["minimum_should_match"] == "3<70% 5<50%"
    assert must["query"] == "пробурена и пласты она вскрыла по данным керна"
    ref = next(s for s in body["query"]["bool"]["should"] if s.get("bool", {}).get("_name") == Q.OBJECT_REF_NAME)
    assert {"match_phrase": {"text": {"query": "скважина 75"}}} in ref["bool"]["should"]
    fig = _Rec()
    resp = Q.search(fig, Q.SearchRequest(FIGURE, kinds=("FIGURE",), long_query="strip"), "vkm")
    topic = Q.topic_of(LQ.strip_frame(FIGURE)[0])            # the label gate: the topic without «рис. 3.1»
    assert _must(fig.bodies[0][1])["multi_match"]["query"] == topic == "оседания показаны на для отработанных " \
                                                                      "панелей рудника СКРУ-1"
    assert resp.hits[0].fields["label_promoted"] is True


# ------------------------------------------------------------------------------------------------ hybrid
DIM = 4
META = {"build_id": "20261013t130000z-aaaaaaaa-bbbbbbbb", "built_from_snapshot_id": "SNAP-1", "dimension": DIM,
        "model_key": "granite-311m-r2", "config_signature": "c" * 64, "space_type": "innerproduct",
        "build_status": "COMPLETE"}
EXPANSION = "maximum ground surface subsidence observed at SKRU-1 in which year"


def _hclient():
    def answer(index, body):
        if index != "vkm-pages":
            return []
        return [{"_id": p, "_index": "vkm-pages-m1-b-test", "_score": 9.0 - i,
                 "_source": {"id": p, "object_type": "PAGE", "source_id": "VKM-SRC-001", "work_id": "VKM-WRK-001",
                             "page_id": p}} for i, p in enumerate((P2, P1))]

    c = FakeOpenSearch(bm25=answer)
    name = f"vkm-vectors-m1-{META['build_id']}"
    c.indices.create(index=name, body={"mappings": {"_meta": dict(META)}})
    c.indices.update_aliases({"actions": [{"add": {"index": name, "alias": "vkm-vectors"}}]})
    c.indices_[name]["docs"]["u1-000000000003"] = {
        "id": "u1-000000000003", "unit_kind": "BLOCK_GROUP", "object_ids": [f"{P3}:b000000000003"], "page_id": P3,
        "source_id": "VKM-SRC-001", "work_id": "VKM-WRK-001", "dup_group_id": P3, "vector": [0.9, 0.1, 0.0, 0.0]}
    return c


def _hembed():
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append((request.url.path, body))
        if request.url.path == "/search/late":
            results = [{"id": t["id"], "kind": t["kind"], "status": "SCORED", "late_score": 1.0 + i,
                        "best_unit_id": f"u-{i}", "units": 1, "tokens": 9} for i, t in enumerate(body["targets"])]
            return httpx.Response(200, json={"model": "mlateon", "query_signature": "sig", "n_query_tokens": 4,
                                             "store": {"snapshot_id": "SNAP-1"}, "timings_ms": {}, "results": results})
        return httpx.Response(200, json={"dense": {"model": "granite-311m-r2", "signature": "q" * 64,
                                                   "dimension": DIM, "vector": [1.0, 0.0, 0.0, 0.0]}})

    client = EmbedClient("http://rx580-retrieval:8790", "tok", transport=httpx.MockTransport(handler))
    client.calls = calls
    return client


def _strip_times(x):
    if isinstance(x, dict):
        return {k: _strip_times(v) for k, v in x.items() if k not in ("timings_ms", "ms")}
    if isinstance(x, list):
        return [_strip_times(v) for v in x]
    return x


def _hsearch(query=LONG, client=None, embed=None, **kw):
    kw.setdefault("candidates", 10)
    kw.setdefault("late", True)
    return hybrid_search(client or _hclient(), embed or _hembed(), HybridRequest(query=query, **kw), "vkm")


@pytest.mark.parametrize("query", [LONG, SHORT, BOREHOLE])
def test_hybrid_without_the_mode_is_todays_answer(query, monkeypatch):
    c0, c1, c2 = _hclient(), _hclient(), _hclient()
    plain = _hsearch(query, c0, expansions=(EXPANSION,))
    explicit = _hsearch(query, c1, expansions=(EXPANSION,), long_query="strict")
    monkeypatch.setattr(LQ, "plan", _boom)
    unplanned = _hsearch(query, c2, expansions=(EXPANSION,))
    assert _dump(_strip_times(plain)) == _dump(_strip_times(explicit)) == _dump(_strip_times(unplanned))
    assert _dump(c0.searches) == _dump(c1.searches) == _dump(c2.searches)
    assert "long_query" not in plain["stages"]


def test_hybrid_long_query_changes_only_the_bm25_legs():
    c, e = _hclient(), _hembed()
    out = _hsearch(LONG, c, e, expansions=(EXPANSION,), long_query="relax_strip")
    bm25 = [b for i, b in c.searches if i == "vkm-pages" and "multi_match" in _dump(b)]
    assert [_must(b)["multi_match"]["query"] for b in bm25] == [LONG_STRIPPED,
                                                                "maximum ground surface subsidence observed at SKRU-1"]
    assert {_must(b)["multi_match"]["minimum_should_match"] for b in bm25} == {"3<70% 5<50%"}
    # the dense leg and the late stage read the original wordings
    assert [b["text"] for p, b in e.calls if p == "/embed/query"] == [LONG, EXPANSION]
    assert [b["query"] for p, b in e.calls if p == "/search/late"] == [LONG]
    stage = out["stages"]["long_query"]
    assert (stage["mode"], stage["status"], stage["long"], stage["terms"], stage["msm"], stage["strip"],
            stage["stripped_text"]) == ("relax_strip", "APPLIED", True, 10, "3<70% 5<50%", "STRIPPED", LONG_STRIPPED)
    assert "original query" in stage["scope"]
    assert stage["expansions"][0]["text"] == EXPANSION and stage["expansions"][0]["status"] == "APPLIED"
    assert out["query"] == LONG and [h["trace"]["bm25_rank"] for h in out["hits"] if h["id"] == P2] == [1]
    # a short query in a mode: the BM25 legs of today, the plan says SHORT
    c2 = _hclient()
    short = _hsearch(SHORT, c2, long_query="relax_strip")
    assert short["stages"]["long_query"]["status"] == "SHORT" and "expansions" not in short["stages"]["long_query"]
    c3 = _hclient()
    _hsearch(SHORT, c3)
    assert _dump(c2.searches) == _dump(c3.searches)


def test_every_formulation_runs_with_the_same_mode():
    c = _hclient()
    out = _hsearch(LONG, c, formulations=({"text": CREEP},), long_query="strip")
    texts = [_must(b)["multi_match"]["query"] for i, b in c.searches if i == "vkm-pages" and "multi_match" in _dump(b)]
    assert LONG_STRIPPED in texts and CREEP_STRIPPED in texts and LONG not in texts and CREEP not in texts
    runs = out["stages"]["formulations"]["runs"]
    assert [r["long_query"]["status"] for r in runs] == ["APPLIED", "APPLIED"]
    assert out["stages"]["long_query"]["stripped_text"] == LONG_STRIPPED          # the query's run (base stages)


# ------------------------------------------------------------------------------------------------ API, MCP, brief
class _PlanSearch:
    """The fake BM25 backend answering like ``query.search``: the plan's record when the request names a mode."""

    def __init__(self, inner):
        self.inner, self.requests = inner, inner.requests

    def search(self, request):
        out = self.inner.search(request)
        if request.get("long_query", "strict") != "strict":
            out["long_query"] = LQ.plan(request["query"], request["long_query"]).record()
        return out

    def status(self):
        return self.inner.status()


class _FakeNav:
    def __init__(self):
        self.calls = []

    def run(self, name, *args, **kwargs):
        self.calls.append((name, args))
        if name != "translate_query":
            raise LookupError(name)
        text = args[0]
        out = {LONG_STRIPPED: "maximum ground surface subsidence SKRU-1", CREEP_STRIPPED: "rock salt creep"}.get(text)
        return {"translation": out, "source_language": "ru", "target_language": "en", "coverage": 1.0 if out else 0.0,
                "terms": []}


@pytest.fixture()
def api(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")
    pytest.importorskip("PIL")
    pytest.importorskip("pytz")
    monkeypatch.delenv("VKM_HYBRID_TRANSLATE_DEFAULT", raising=False)
    from fastapi.testclient import TestClient

    from vkm_corpus.api.app import ApiConfig, create_app
    from vkm_corpus.api.fixtures import FakeHybrid, hybrid_hits, synthetic_service

    service, canon, _fakes = synthetic_service(tmp_path)
    service.deps.search = _PlanSearch(service.deps.search)
    service.deps.hybrid = FakeHybrid(hybrid_hits(canon))
    service.deps.nav = _FakeNav()
    token = "read-token-for-long-query-tests-0000000"
    app = create_app(service, ApiConfig(read_tokens={token: "read"}))
    return TestClient(app), app, service, {"Authorization": f"Bearer {token}"}, token


def _body(resp):
    assert resp.status_code == 200, resp.text
    out = resp.json()
    out["meta"] = {k: v for k, v in out["meta"].items() if k not in ("request_id", "elapsed_ms")}
    return out


def test_api_search_passes_the_mode_only_when_set(api):
    client, _app, service, h, _token = api
    plain = _body(client.post("/v1/search", headers=h, json={"query": LONG}))
    assert "long_query" not in service.deps.search.requests[-1]
    explicit = _body(client.post("/v1/search", headers=h, json={"query": LONG, "long_query": "strict"}))
    assert "long_query" not in service.deps.search.requests[-1]
    assert _dump(plain) == _dump(explicit) and plain.get("item") is None
    body = _body(client.post("/v1/search", headers=h, json={"query": LONG, "long_query": "relax_strip"}))
    assert service.deps.search.requests[-1]["long_query"] == "relax_strip"
    record = body["item"]["record"]
    assert body["item"]["envelope"]["object_kind"] == "SEARCH_RESULT" and record["totals"]
    assert record["stages"]["long_query"]["stripped_text"] == LONG_STRIPPED
    assert [it["envelope"]["object_id"] for it in body["items"]] == \
        [it["envelope"]["object_id"] for it in plain["items"]]
    brief = _body(client.post("/v1/search", headers=h, json={"query": LONG, "long_query": "strip", "view": "brief"}))
    assert brief["run"]["stages"]["long_query"]["stripped_text"] == LONG_STRIPPED
    assert "run" not in _body(client.post("/v1/search", headers=h, json={"query": LONG, "view": "brief"}))
    bad = client.post("/v1/search", headers=h, json={"query": LONG, "long_query": "loose"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_api_hybrid_passes_the_mode_and_translates_the_stripped_question(api):
    from vkm_corpus.api.errors import ApiFailure

    client, _app, service, h, _token = api
    _body(client.post("/v1/search/hybrid", headers=h, json={"query": LONG, "translate": True}))
    assert "long_query" not in service.deps.hybrid.requests[-1]
    assert service.deps.nav.calls[-1] == ("translate_query", (LONG,))           # today: the question as typed
    body = _body(client.post("/v1/search/hybrid", headers=h, json={"query": LONG, "translate": True,
                                                                   "long_query": "strip"}))
    sent = service.deps.hybrid.requests[-1]
    assert sent["long_query"] == "strip" and sent["expansions"] == ("maximum ground surface subsidence SKRU-1",)
    assert service.deps.nav.calls[-1] == ("translate_query", (LONG_STRIPPED,))
    tr = body["item"]["record"]["translation"]
    assert (tr["status"], tr["built_from"], tr["source_text"]) == ("APPLIED", "STRIPPED_QUERY", LONG_STRIPPED)
    _body(client.post("/v1/search/hybrid", headers=h, json={"query": LONG, "translate": True, "long_query": "relax"}))
    assert service.deps.nav.calls[-1] == ("translate_query", (LONG,))           # relax: the question as typed
    # each formulation's translation comes from its own stripped text
    _body(client.post("/v1/search/hybrid", headers=h, json={"query": LONG, "translate": True,
                                                            "long_query": "relax_strip", "formulations": [CREEP]}))
    sent = service.deps.hybrid.requests[-1]
    assert sent["formulations"][0]["expansions"] == ("rock salt creep",) and sent["long_query"] == "relax_strip"
    _body(client.post("/v1/search/hybrid", headers=h, json={"query": LONG, "long_query": "strict"}))
    assert "long_query" not in service.deps.hybrid.requests[-1]
    bad = client.post("/v1/search/hybrid", headers=h, json={"query": LONG, "long_query": "loose"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "INVALID_ARGUMENT"
    for call in (lambda: service.search_hybrid("x", ["PAGE"], {}, 5, None, long_query="loose"),
                 lambda: service.search("x", ["PAGE"], {}, 5, None, long_query="loose")):
        with pytest.raises(ApiFailure) as exc:
            call()
        assert exc.value.code == "INVALID_ARGUMENT"


def test_mcp_search_tools_pass_the_mode_only_when_set(api):
    pytest.importorskip("mcp")
    from mcp import Client

    from vkm_corpus.mcp.api_client import ApiClient
    from vkm_corpus.mcp.servers import build_read_server

    _client, app, service, _h, token = api
    server = build_read_server(ApiClient("http://vkm-api", token, transport=httpx.ASGITransport(app=app)))

    async def run():
        async with Client(server) as c:
            tools = {t.name: t for t in (await c.list_tools()).tools}
            got = {}
            for tool, backend in (("search_text", service.deps.search), ("search_hybrid", service.deps.hybrid)):
                await c.call_tool(tool, {"query": LONG})
                plain = backend.requests[-1]
                res = await c.call_tool(tool, {"query": LONG, "long_query": "strip"})
                got[tool] = (plain, backend.requests[-1], res.structured_content)
            return tools, got

    tools, got = asyncio.run(run())
    for tool in ("search_text", "search_hybrid"):
        assert "long_query" in tools[tool].input_schema["properties"]
        plain, moded, _content = got[tool]
        assert "long_query" not in plain and moded["long_query"] == "strip"
    assert got["search_text"][2]["run"]["stages"]["long_query"]["status"] == "APPLIED"     # brief by default
    assert "long_query" not in tools["retrieval_trace"].input_schema["properties"]


def test_brief_hybrid_run_shows_the_mode():
    from vkm_corpus.api.brief import brief_response

    stage = LQ.plan(LONG, "relax_strip").record()
    body = {"ok": True, "meta": {}, "item": {"record": {"stages": {"long_query": stage}}}, "items": []}
    assert brief_response(body, kind="hybrid")["run"]["stages"]["long_query"] == {
        "mode": "relax_strip", "status": "APPLIED", "terms": 10, "msm": "3<70% 5<50%", "strip": "STRIPPED",
        "stripped_text": LONG_STRIPPED}
    assert "long_query" not in brief_response({"ok": True, "meta": {}, "item": {"record": {"stages": {}}},
                                               "items": []}, kind="hybrid")["run"]["stages"]

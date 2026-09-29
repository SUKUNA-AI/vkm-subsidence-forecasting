"""Term dictionary through the API and the search (agent TR): the route /v1/nav/translate, the ``translate`` flag of
the hybrid search (expansion legs of the real hybrid search over a fake OpenSearch and a fake RX580 service), the
``translation`` formulation of reconstruct_topic. Synthetic data; the NAV query functions are stand-ins."""
from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")
pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")
pytest.importorskip("PIL")
pytest.importorskip("pytz", reason="DuckDB TIMESTAMPTZ values in Python need pytz (decision note: extra corpus)")
httpx = pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from vkm_corpus.api import topic  # noqa: E402
from vkm_corpus.api.app import ApiConfig, create_app  # noqa: E402
from vkm_corpus.api.envelope import Envelope  # noqa: E402
from vkm_corpus.api.fixtures import FakeHybrid, hybrid_hits, synthetic_service  # noqa: E402
from vkm_corpus.navigation import store  # noqa: E402
from vkm_corpus.navigation.term_dictionary_query import NOTE  # noqa: E402
from vkm_corpus.search.fakes import FakeOpenSearch  # noqa: E402
from vkm_corpus.search.hybrid import EmbedClient, HybridRequest, hybrid_search  # noqa: E402
from vkm_corpus.search.query import SearchRequestError  # noqa: E402

READ = "read-token-for-term-dictionary-0000000"
H = {"Authorization": f"Bearer {READ}"}
NAV_SNAP = "snap-nav-test"
TRM = "TRM-00000000000000c1"


def fake_translate_term(con, term, target=None, limit=10):
    if term == "неизвестный термин":
        return {"query": term, "target": target, "match": None, "translations": [], "synonyms": [],
                "abbreviations": [], "composed": [], "note": NOTE}
    return {"query": term, "target": target,
            "match": {"lemma": "ползучесть", "language": "ru", "key": "ползучесть", "term_id": TRM},
            "translations": [{"term_id": "TRM-00000000000000c2", "lemma": "creep", "language": "en",
                              "relation": "TRANSLATION", "score": 1.0, "methods": ["CURATED_SEED", "KEYWORD_LISTS"],
                              "status": "REVIEWED_BY_AGENT", "n_sources": 2,
                              "example_page_ids": ["VKM-SRC-001:p0001"]}][:limit],
            "synonyms": [], "abbreviations": [], "composed": [], "note": NOTE}


def fake_translate_query(con, text, **kw):
    return {"text": text, "source_language": "ru", "target_language": "en", "translation": "salt creep",
            "coverage": 1.0, "terms": [{"span": text, "translation": "salt creep", "score": 0.97,
                                        "pair_id": "TTR-00000000000000aa"}], "note": NOTE}


def _nav(tmp_path, canon, functions):
    root = tmp_path / "root"
    nav_dir = root / "derived" / "navigation" / NAV_SNAP
    nav_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({"section_id": ["SEC-0000000000000001"], "source_id": ["VKM-SRC-001"], "level": [1],
                             "title": ["Глава 1. Ползучесть соли"], "title_path": ["Глава 1. Ползучесть соли"]}),
                   nav_dir / "sections.parquet")
    store.pack(nav_dir)
    store.publish(root, NAV_SNAP)
    return store.NavStore(root, canonical_db=canon.duckdb_path, functions=functions)


@pytest.fixture()
def env(tmp_path):
    service, canon, _fakes = synthetic_service(tmp_path / "canon")
    service.deps.nav = _nav(tmp_path, canon, {"translate_term": fake_translate_term,
                                              "translate_query": fake_translate_query})
    service.deps.hybrid = FakeHybrid(hybrid_hits(canon))
    return TestClient(create_app(service, ApiConfig(read_tokens={READ: "read"}))), service, canon


def _ok(resp):
    assert resp.status_code == 200, resp.text
    body = resp.json()
    for item in ([body["item"]] if body.get("item") else []) + list(body.get("items") or []):
        Envelope.model_validate(item["envelope"])
    return body


# ------------------------------------------------------------------------------------------------ /v1/nav/translate
def test_translate_route(env):
    client, service, _canon = env
    body = _ok(client.get("/v1/nav/translate", params={"term": "ползучести", "target": "en"}, headers=H))
    env_ = body["item"]["envelope"]
    assert env_["object_kind"] == "NAV_TRANSLATION" and env_["layer"] == "PROJECTION" and env_["origin"] == "DERIVED"
    assert env_["review_status"] == "AUTO_EXTRACTED_UNREVIEWED"
    rec = body["item"]["record"]
    assert rec["translations"][0]["lemma"] == "creep" and rec["target"] == "en" and rec["nav_snapshot_id"] == NAV_SNAP
    assert "not evidence" in rec["note"]
    empty = _ok(client.get("/v1/nav/translate", params={"term": "неизвестный термин"}, headers=H))
    assert empty["item"]["record"]["match"] is None and empty["item"]["record"]["translations"] == []   # 200
    bad = client.get("/v1/nav/translate", params={"term": "ползучесть", "target": "fr"}, headers=H).json()
    assert bad["error"]["code"] == "INVALID_ARGUMENT"
    long = client.get("/v1/nav/translate", params={"term": "x" * 201}, headers=H).json()
    assert long["error"]["code"] == "INVALID_ARGUMENT"
    assert client.get("/v1/nav/translate", params={"term": "ползучесть"}).status_code == 401
    service.deps.nav = store.NavStore(service.deps.nav.data_root / "missing")
    down = client.get("/v1/nav/translate", params={"term": "ползучесть"}, headers=H).json()
    assert down["error"]["code"] == "DEPENDENCY_UNAVAILABLE"


# ------------------------------------------------------------------------------------------------ hybrid flag
def test_hybrid_translate_flag(env, tmp_path):
    client, service, canon = env
    hybrid = service.deps.hybrid
    body = _ok(client.post("/v1/search/hybrid", json={"query": "ползучесть соли", "translate": True}, headers=H))
    assert hybrid.requests[-1]["expansions"] == ("salt creep",)
    tr = body["item"]["record"]["translation"]
    assert tr["status"] == "APPLIED" and tr["text"] == "salt creep" and tr["target_language"] == "en"
    plain = _ok(client.post("/v1/search/hybrid", json={"query": "ползучесть соли"}, headers=H))
    assert "expansions" not in hybrid.requests[-1] and "translation" not in plain["item"]["record"]   # off by default
    _ok(client.get("/v1/search/hybrid", params={"q": "ползучесть соли", "translate": "true"}, headers=H))
    assert hybrid.requests[-1]["expansions"] == ("salt creep",)
    # a NAV build without the dictionary: the search runs as asked and says so
    service.deps.nav = _nav(tmp_path / "other", canon, {})
    warned = _ok(client.post("/v1/search/hybrid", json={"query": "ползучесть соли", "translate": True}, headers=H))
    assert "expansions" not in hybrid.requests[-1]
    assert warned["item"]["record"]["translation"]["status"] == "UNAVAILABLE"
    assert "TRANSLATION_UNAVAILABLE" in {w["code"] for w in warned["meta"]["warnings"]}


DIM = 4
P1, P2, P3 = (f"VKM-SRC-001:p000{i}" for i in range(1, 4))
META = {"build_id": "20260928t120000z-aaaaaaaa-bbbbbbbb", "built_from_snapshot_id": "SNAP-1", "dimension": DIM,
        "model_key": "granite-311m-r2", "config_signature": "c" * 64, "space_type": "innerproduct",
        "build_status": "COMPLETE"}


def _search_client():
    def answer(index, body):                                  # BM25: the query text decides the hits
        text = json.dumps(body, ensure_ascii=False)
        page = P3 if "salt creep" in text else P2
        return [{"_id": page, "_index": "vkm-pages-m1-b-test", "_score": 5.0,
                 "_source": {"id": page, "object_type": "PAGE", "source_id": "VKM-SRC-001",
                             "work_id": "VKM-WRK-001", "page_id": page}}] if index == "vkm-pages" else []

    c = FakeOpenSearch(bm25=answer)
    name = f"vkm-vectors-m1-{META['build_id']}"
    c.indices.create(index=name, body={"mappings": {"_meta": dict(META)}})
    for uid, page, vec in (("u1-000000000001", P1, [0.9, 0.1, 0.0, 0.0]),
                           ("u1-000000000003", P3, [0.1, 0.9, 0.0, 0.0])):
        c.indices_[name]["docs"][uid] = {"id": uid, "unit_kind": "BLOCK_GROUP", "object_ids": [f"{page}:b1"],
                                         "page_id": page, "source_id": "VKM-SRC-001", "work_id": "VKM-WRK-001",
                                         "dup_group_id": page, "vector": vec}
    c.indices.update_aliases({"actions": [{"add": {"index": name, "alias": "vkm-vectors"}}]})
    return c


def _embed():
    calls = []

    def handler(request):
        text = json.loads(request.content)["text"]
        calls.append(text)
        vec = [0.0, 1.0, 0.0, 0.0] if text == "salt creep" else [1.0, 0.0, 0.0, 0.0]
        return httpx.Response(200, json={"dense": {"model": "granite-311m-r2", "signature": "q" * 64,
                                                   "dimension": DIM, "vector": vec}})

    client = EmbedClient("http://rx580-retrieval:8790", "tok", transport=httpx.MockTransport(handler))
    client.calls = calls
    return client


def test_hybrid_expansion_legs():
    c, e = _search_client(), _embed()
    base = hybrid_search(c, e, HybridRequest(query="ползучесть соли", candidates=10), "vkm")
    assert base["stages"]["expansion"] == "NOT_RUN" and e.calls == ["ползучесть соли"]
    out = hybrid_search(c, e, HybridRequest(query="ползучесть соли", candidates=10, expansions=("salt creep",)),
                        "vkm")
    assert e.calls[-1] == "salt creep"
    stage = out["stages"]["expansion"]
    assert stage["texts"] == ["salt creep"] and stage["legs"] == ["bm25:PAGE~x1", "dense:PAGE~x1"]
    ids = [h["id"] for h in out["hits"]]
    assert P3 in ids and P3 not in [h["id"] for h in base["hits"][:1]]       # found by the expansion legs
    hit3 = next(h for h in out["hits"] if h["id"] == P3)
    assert hit3["trace"]["expansion_ranks"]["bm25~x1"] == 1 and hit3["trace"]["expansion_ranks"]["dense~x1"] == 1
    same = hybrid_search(c, e, HybridRequest(query="ползучесть соли", candidates=10, expansions=("ползучесть соли",)),
                         "vkm")
    assert same["stages"]["expansion"] == "NOT_RUN"                          # the query itself is no expansion
    with pytest.raises(SearchRequestError):
        HybridRequest(query="q", expansions=("a", "b", "c")).validate()
    with pytest.raises(SearchRequestError):
        HybridRequest(query="q", expansions=("x" * 513,)).validate()


# ------------------------------------------------------------------------------------------------ dossier formulation
def test_topic_translation_formulation(env):
    client, service, _canon = env
    service.deps.hybrid = None
    rec = service.reconstruct_topic("ползучесть соли", translate=True).item.record
    kinds = [f["kind"] for f in rec["formulations"]]
    assert kinds[:2] == ["query", "translation"] and rec["formulations"][1]["text"] == "salt creep"
    assert rec["inputs"]["translation"]["status"] == "APPLIED"
    assert rec["inputs"]["translation"]["terms"][0]["pair_id"] == "TTR-00000000000000aa"
    off = service.reconstruct_topic("ползучесть соли", translate=False).item.record
    assert "translation" not in [f["kind"] for f in off["formulations"]]
    default = service.reconstruct_topic("ползучесть соли").item.record               # the measured default
    assert ("translation" in [f["kind"] for f in default["formulations"]]) is topic.TRANSLATE_DEFAULT
    body = _ok(client.get("/v1/topic", params={"q": "ползучесть соли", "translate": "false"}, headers=H))
    assert "translation" not in [f["kind"] for f in body["item"]["record"]["formulations"]]
    post = _ok(client.post("/v1/topic", json={"query": "ползучесть соли", "paraphrases": ["creep of rock salt"],
                                              "translate": True}, headers=H))
    assert [f["kind"] for f in post["item"]["record"]["formulations"]][:3] == ["query", "paraphrase", "translation"]

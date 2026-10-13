"""The area filter and what it leaves out (agent Б, 13.10; ``search.scope_filter``): AMBIGUOUS sources whose
candidates hold the area or a related one, sources of a related area (the SKRU-1/SKRU-2 pillar zone), the warning,
``include_ambiguous`` = «area OR these sources» compiled once for BM25, dense k-NN and the page filters, and the API
wiring (search, hybrid, object query). The scope mapping itself is not touched. Synthetic rows."""
from __future__ import annotations

import pytest

from vkm_corpus.contracts.site_scope import map_site_scope
from vkm_corpus.search import scope_filter as SF
from vkm_corpus.search.query import SearchRequestError, compile_filters


def _src(sid, raw):
    m = map_site_scope(raw)
    return {"source_id": sid, "site_scope": list(m.scopes), "site_scope_candidates": list(m.candidates),
            "site_scope_mapping": m.mapping.value, "site_scope_raw": raw}


SOURCES = [_src("VKM-SRC-002", "SKRU1_SKRU2_SKRU3"), _src("VKM-SRC-012", "SKRU1_SKRU2_SKRU3"),
           _src("VKM-SRC-045", "SKRU1_SKRU2_SKRU3"), _src("VKM-SRC-110", "SKRU1_SKRU2_PILLAR"),
           _src("VKM-SRC-120", "VKM_Solikamsk"), _src("VKM-SRC-014", "SKRU1"),
           _src("VKM-SRC-030", "VKM_REGIONAL_and_SKRU1"), _src("VKM-SRC-050", "VKM_REGIONAL"),
           _src("VKM-SRC-060", "BKPRU4_VKM")]


def test_the_mapping_stays_as_it_is():
    # the register values of the live check (012 Лебедева, 045 Ермашов, 002; VKM_Solikamsk; the pillar zone)
    assert map_site_scope("SKRU1_SKRU2_SKRU3").scopes == () and map_site_scope("VKM_Solikamsk").scopes == ()
    assert map_site_scope("SKRU1_SKRU2_PILLAR").scopes == ("SKRU1_SKRU2_PILLAR",)


def test_excluded_sources_for_skru1_skru3_and_other_areas():
    out = SF.excluded_sources(["SKRU1"], SOURCES)
    assert [(r["source_id"], r["reason"]) for r in out] == [
        ("VKM-SRC-002", "AMBIGUOUS_CANDIDATE"), ("VKM-SRC-012", "AMBIGUOUS_CANDIDATE"),
        ("VKM-SRC-045", "AMBIGUOUS_CANDIDATE"), ("VKM-SRC-110", "RELATED_SCOPE"),
        ("VKM-SRC-120", "AMBIGUOUS_CANDIDATE")]
    assert out[0]["matched"] == ["SKRU1", "SOLIKAMSK_GROUP"] and out[3]["matched"] == ["SKRU1_SKRU2_PILLAR"]
    assert out[4]["matched"] == ["SKRU1_OR_SKRU2_UNATTRIBUTED"] and out[0]["site_scope_mapping"] == "AMBIGUOUS"
    # 014 (SKRU1) and 030 (MULTI with SKRU1) are taken by the filter itself
    assert [r["source_id"] for r in SF.excluded_sources(["SKRU3"], SOURCES)] == ["VKM-SRC-002", "VKM-SRC-012",
                                                                                 "VKM-SRC-045"]
    assert SF.excluded_sources(["VKM_REGIONAL"], SOURCES) == [] and SF.excluded_sources(["BKPRU4"], SOURCES) == []
    # the request's own source filter narrows the list
    assert [r["source_id"] for r in SF.excluded_sources(["SKRU1"], SOURCES, within=["VKM-SRC-012", "VKM-SRC-014"])] \
        == ["VKM-SRC-012"]
    assert SF.related(["SKRU1"]) >= {"SKRU1", "SKRU1_SKRU2_PILLAR", "SKRU1_OR_SKRU2_UNATTRIBUTED"}


def test_widen_compiles_to_one_or_clause():
    found = SF.excluded_sources(["SKRU1"], SOURCES)
    filters = SF.widen({"source_scope": ["SKRU1"], "year": {"gte": 2000}}, found)
    assert "source_scope" not in filters and filters["year"] == {"gte": 2000}
    assert filters["source_scope_or_source_id"]["source_id"][:2] == ["VKM-SRC-002", "VKM-SRC-012"]
    clauses, _ = compile_filters(filters)
    ors = [c for c in clauses if "bool" in c]
    assert ors == [{"bool": {"should": [{"terms": {"source_site_scope": ["SKRU1"]}},
                                        {"terms": {"source_id": ["VKM-SRC-002", "VKM-SRC-012", "VKM-SRC-045",
                                                                 "VKM-SRC-110", "VKM-SRC-120"]}}],
                             "minimum_should_match": 1}}]
    assert SF.widen({"source_scope": ["SKRU1"]}, []) == {"source_scope": ["SKRU1"]}
    with pytest.raises(SearchRequestError):
        compile_filters({"source_scope_or_source_id": {"source_scope": [], "source_id": []}})
    with pytest.raises(SearchRequestError):
        compile_filters({"source_scope_or_source_id": {"site": ["SKRU1"]}})


def test_dense_knn_applies_the_same_clause_to_unit_documents():
    from vkm_corpus.search.fakes import FakeOpenSearch
    from vkm_corpus.search.hybrid import knn_body
    from vkm_corpus.search.vectors import VECTOR_FIELD

    c = FakeOpenSearch()
    c.indices.create(index="v", body={"mappings": {"_meta": {}}})
    docs = [("u-014", "VKM-SRC-014", ["SKRU1"]), ("u-012", "VKM-SRC-012", []),
            ("u-050", "VKM-SRC-050", ["VKM_REGIONAL"])]
    c.bulk([x for uid, sid, scope in docs for x in (
        {"create": {"_index": "v", "_id": uid}},
        {"id": uid, "source_id": sid, "source_site_scope": scope, "unit_kind": "BLOCK_GROUP",
         VECTOR_FIELD: [1.0, 0.0]})])

    def ids(filters):
        return sorted(h["_id"] for h in c.search(index="v", body=knn_body([1.0, 0.0], 10, filters, None))
                      ["hits"]["hits"])

    assert ids({}) == ["u-012", "u-014", "u-050"]
    assert ids({"source_scope": ["SKRU1"]}) == ["u-014"]
    widened = SF.widen({"source_scope": ["SKRU1"]}, SF.excluded_sources(["SKRU1"], SOURCES))
    assert ids(widened) == ["u-012", "u-014"]


# ---------------------------------------------------------------------------------------------------- API wiring
pytest.importorskip("fastapi")
pytest.importorskip("duckdb")
pytest.importorskip("pytz", reason="DuckDB TIMESTAMPTZ values in Python need pytz")
pytest.importorskip("PIL")


@pytest.fixture()
def svc(tmp_path, monkeypatch):
    from vkm_corpus.api.fixtures import FakeHybrid, hybrid_hits, synthetic_service

    service, canon, fakes = synthetic_service(tmp_path)
    service.deps.hybrid = FakeHybrid(hybrid_hits(canon))
    monkeypatch.setattr(service, "_scope_sources", lambda: SOURCES)
    return service, fakes


def _codes(result):
    return {w.code: w for w in result.warnings}


def test_api_search_warns_and_include_ambiguous_widens(svc):
    service, fakes = svc
    res = service.search("оседание", ["PAGE"], {"source_scope": ["SKRU1"]}, 10, None)
    w = _codes(res)["SCOPE_FILTER_EXCLUDED_AMBIGUOUS"]
    assert w.count == 5 and "VKM-SRC-012" in w.message and "VKM-SRC-110" in w.message
    assert fakes["search"].requests[-1]["filters"] == {"source_scope": ["SKRU1"]}       # the filter as asked
    res2 = service.search_hybrid("оседание", ["PAGE"], {"source_scope": ["SKRU1"], "include_ambiguous": True}, 10,
                                 None)
    assert _codes(res2)["SCOPE_FILTER_INCLUDED_AMBIGUOUS"].count == 5
    filters = service.deps.hybrid.requests[-1]["filters"]
    assert "source_scope" not in filters and "include_ambiguous" not in filters
    assert filters["source_scope_or_source_id"]["source_scope"] == ["SKRU1"]
    assert "VKM-SRC-045" in filters["source_scope_or_source_id"]["source_id"]
    # no area filter: nothing changes, no warning
    res3 = service.search_hybrid("оседание", ["PAGE"], {"include_ambiguous": True}, 10, None)
    assert not any(w.code.startswith("SCOPE_FILTER") for w in res3.warnings)
    assert service.deps.hybrid.requests[-1]["filters"] == {}


def test_api_object_query_widens_with_the_sources(svc):
    service, _fakes = svc
    plain = service.query_objects(["FIGURE"], 50, None, **_object_filters(source_scope=["SKRU1"]))
    assert _codes(plain)["SCOPE_FILTER_EXCLUDED_AMBIGUOUS"].count == 5
    wide = service.query_objects(["FIGURE"], 50, None, **_object_filters(source_scope=["SKRU1"],
                                                                         include_ambiguous=True))
    assert _codes(wide)["SCOPE_FILTER_INCLUDED_AMBIGUOUS"].count == 5


def test_request_bodies_carry_include_ambiguous():
    from vkm_corpus.api.app import ObjectsQueryBody, SearchFilters

    assert SearchFilters(source_scope=["SKRU1"], include_ambiguous=True).to_search() == {
        "source_scope": ["SKRU1"], "include_ambiguous": True}
    assert "include_ambiguous" not in SearchFilters(source_scope=["SKRU1"]).to_search()      # default: unchanged body
    assert ObjectsQueryBody(source_scope=["SKRU1"], include_ambiguous=True).include_ambiguous is True


def _object_filters(**kw):
    base = {"source_ids": [], "work_ids": [], "page_from": None, "page_to": None, "figure_types": [],
            "review_status": [], "quality_flags_any": [], "quality_flags_none": [], "has_image": None,
            "source_scope": [], "source_scope_raw": [], "caption_query": None, "origin": [],
            "include_ambiguous": False}
    base.update(kw)
    return base

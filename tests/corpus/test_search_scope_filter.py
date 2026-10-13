"""The area filter and what it leaves out (agent Б, 13.10; ``search.scope_filter``): AMBIGUOUS sources whose
candidates hold the area or a related one, sources of a related area (the SKRU-1/SKRU-2 pillar zone), the warning,
``include_ambiguous`` = «area OR these sources» compiled once for BM25, dense k-NN and the page filters, and the API
wiring (search, hybrid, object query). Since map v2 (owner 13.10.2026) ``SKRU1_SKRU2_SKRU3`` is MULTI → SKRU1, SKRU2,
SKRU3: on a snapshot built with v1 (stored AMBIGUOUS, the index without the area) the filter takes these sources by
``source_id`` (``remapped_sources``). Synthetic rows."""
from __future__ import annotations

import pytest

from vkm_corpus.contracts.site_scope import map_site_scope, with_current_scope
from vkm_corpus.search import scope_filter as SF
from vkm_corpus.search.query import SearchRequestError, compile_filters


def _src(sid, raw):
    m = map_site_scope(raw)
    return {"source_id": sid, "site_scope": list(m.scopes), "site_scope_candidates": list(m.candidates),
            "site_scope_mapping": m.mapping.value, "site_scope_raw": raw}


def _stored_v1(sid, raw):
    """A ``sources`` row of a snapshot built with map v1: ``SKRU1_SKRU2_SKRU3`` stored AMBIGUOUS."""
    if raw == "SKRU1_SKRU2_SKRU3":
        return {"source_id": sid, "site_scope": [], "site_scope_mapping": "AMBIGUOUS", "site_scope_raw": raw,
                "site_scope_candidates": ["SOLIKAMSK_GROUP", "SKRU1", "SKRU2", "SKRU3"]}
    return _src(sid, raw)


STORED = [_stored_v1("VKM-SRC-002", "SKRU1_SKRU2_SKRU3"), _stored_v1("VKM-SRC-012", "SKRU1_SKRU2_SKRU3"),
          _stored_v1("VKM-SRC-045", "SKRU1_SKRU2_SKRU3"), _src("VKM-SRC-110", "SKRU1_SKRU2_PILLAR"),
          _src("VKM-SRC-120", "VKM_Solikamsk"), _src("VKM-SRC-014", "SKRU1"),
          _src("VKM-SRC-030", "VKM_REGIONAL_and_SKRU1"), _src("VKM-SRC-050", "VKM_REGIONAL"),
          _src("VKM-SRC-060", "BKPRU4_VKM")]
SOURCES = [with_current_scope(r) for r in STORED]          # what the API reads: the current map over a v1 snapshot


def test_the_mapping_v2():
    # the register values of the live check (012 Лебедева, 045 Ермашов, 002; VKM_Solikamsk; the pillar zone)
    m = map_site_scope("SKRU1_SKRU2_SKRU3")
    assert m.scopes == ("SKRU1", "SKRU2", "SKRU3") and m.mapping == "MULTI" and m.candidates == ()
    assert map_site_scope("VKM_Solikamsk").scopes == ()
    assert map_site_scope("SKRU1_SKRU2_PILLAR").scopes == ("SKRU1_SKRU2_PILLAR",)
    assert SOURCES[1]["site_scope"] == ["SKRU1", "SKRU2", "SKRU3"] and SOURCES[1]["site_scope_stored"] == []
    assert "site_scope_stored" not in SOURCES[3]                       # unchanged rows carry no stored copy


def test_remapped_sources_are_taken_by_id_on_an_older_snapshot():
    assert [r["source_id"] for r in SF.remapped_sources(["SKRU1"], SOURCES)] == ["VKM-SRC-002", "VKM-SRC-012",
                                                                                 "VKM-SRC-045"]
    assert [r["source_id"] for r in SF.remapped_sources(["SKRU3"], SOURCES)] == ["VKM-SRC-002", "VKM-SRC-012",
                                                                                 "VKM-SRC-045"]
    assert SF.remapped_sources(["VKM_REGIONAL"], SOURCES) == []
    assert [r["source_id"] for r in SF.remapped_sources(["SKRU1"], SOURCES, within=["VKM-SRC-012"])] == \
        ["VKM-SRC-012"]
    assert SF.remapped_sources(["SKRU1"], [with_current_scope(_src("VKM-SRC-012", "SKRU1_SKRU2_SKRU3"))]) == []


def test_excluded_sources_for_skru1_skru3_and_other_areas():
    out = SF.excluded_sources(["SKRU1"], SOURCES)
    assert [(r["source_id"], r["reason"]) for r in out] == [
        ("VKM-SRC-110", "RELATED_SCOPE"), ("VKM-SRC-120", "AMBIGUOUS_CANDIDATE")]
    assert out[0]["matched"] == ["SKRU1_SKRU2_PILLAR"] and out[1]["matched"] == ["SKRU1_OR_SKRU2_UNATTRIBUTED"]
    assert out[1]["site_scope_mapping"] == "AMBIGUOUS"
    # 002/012/045 (MULTI since v2), 014 (SKRU1) and 030 (MULTI with SKRU1) are taken by the filter itself
    assert SF.excluded_sources(["SKRU3"], SOURCES) == []
    assert [r["source_id"] for r in SF.excluded_sources(["SKRU1"], STORED)][:3] == ["VKM-SRC-002", "VKM-SRC-012",
                                                                                    "VKM-SRC-045"]   # map v1
    assert SF.excluded_sources(["VKM_REGIONAL"], SOURCES) == [] and SF.excluded_sources(["BKPRU4"], SOURCES) == []
    # the request's own source filter narrows the list
    assert [r["source_id"] for r in SF.excluded_sources(["SKRU1"], SOURCES, within=["VKM-SRC-110", "VKM-SRC-014"])] \
        == ["VKM-SRC-110"]
    assert SF.related(["SKRU1"]) >= {"SKRU1", "SKRU1_SKRU2_PILLAR", "SKRU1_OR_SKRU2_UNATTRIBUTED"}


def test_widen_compiles_to_one_or_clause():
    found = SF.excluded_sources(["SKRU1"], SOURCES)
    filters = SF.widen({"source_scope": ["SKRU1"], "year": {"gte": 2000}}, found)
    assert "source_scope" not in filters and filters["year"] == {"gte": 2000}
    assert filters["source_scope_or_source_id"]["source_id"] == ["VKM-SRC-110", "VKM-SRC-120"]
    clauses, _ = compile_filters(filters)
    ors = [c for c in clauses if "bool" in c]
    assert ors == [{"bool": {"should": [{"terms": {"source_site_scope": ["SKRU1"]}},
                                        {"terms": {"source_id": ["VKM-SRC-110", "VKM-SRC-120"]}}],
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
    widened = SF.widen({"source_scope": ["SKRU1"]}, SF.remapped_sources(["SKRU1"], SOURCES))
    assert ids(widened) == ["u-012", "u-014"]                  # 012: v1 index without the area, taken by id


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
    assert w.count == 2 and "VKM-SRC-120" in w.message and "VKM-SRC-110" in w.message
    assert "VKM-SRC-012" not in w.message
    # the area as asked plus the sources the current map gives it (002/012/045 on a v1 snapshot)
    assert fakes["search"].requests[-1]["filters"] == {"source_scope_or_source_id": {
        "source_scope": ["SKRU1"], "source_id": ["VKM-SRC-002", "VKM-SRC-012", "VKM-SRC-045"]}}
    res2 = service.search_hybrid("оседание", ["PAGE"], {"source_scope": ["SKRU1"], "include_ambiguous": True}, 10,
                                 None)
    assert _codes(res2)["SCOPE_FILTER_INCLUDED_AMBIGUOUS"].count == 2
    filters = service.deps.hybrid.requests[-1]["filters"]
    assert "source_scope" not in filters and "include_ambiguous" not in filters
    assert filters["source_scope_or_source_id"]["source_scope"] == ["SKRU1"]
    assert filters["source_scope_or_source_id"]["source_id"] == ["VKM-SRC-002", "VKM-SRC-012", "VKM-SRC-045",
                                                                 "VKM-SRC-110", "VKM-SRC-120"]
    # a filter on an area nothing was remapped to or left out of: unchanged, no warning
    res4 = service.search("оседание", ["PAGE"], {"source_scope": ["VKM_REGIONAL"]}, 10, None)
    assert not any(w.code.startswith("SCOPE_FILTER") for w in res4.warnings)
    assert fakes["search"].requests[-1]["filters"] == {"source_scope": ["VKM_REGIONAL"]}
    # no area filter: nothing changes, no warning
    res3 = service.search_hybrid("оседание", ["PAGE"], {"include_ambiguous": True}, 10, None)
    assert not any(w.code.startswith("SCOPE_FILTER") for w in res3.warnings)
    assert service.deps.hybrid.requests[-1]["filters"] == {}


def test_api_object_query_widens_with_the_sources(svc):
    service, _fakes = svc
    plain = service.query_objects(["FIGURE"], 50, None, **_object_filters(source_scope=["SKRU1"]))
    assert _codes(plain)["SCOPE_FILTER_EXCLUDED_AMBIGUOUS"].count == 2
    wide = service.query_objects(["FIGURE"], 50, None, **_object_filters(source_scope=["SKRU1"],
                                                                         include_ambiguous=True))
    assert _codes(wide)["SCOPE_FILTER_INCLUDED_AMBIGUOUS"].count == 2


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

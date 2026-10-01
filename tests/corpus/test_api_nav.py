"""Navigation layer through the VKM API (/v1/nav/*): envelopes, errors, NOT_FOUND and the unavailable layer."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
duckdb = pytest.importorskip("duckdb")
pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")
pytest.importorskip("pytz")

from fastapi.testclient import TestClient  # noqa: E402

from vkm_corpus.api.app import ApiConfig, create_app  # noqa: E402
from vkm_corpus.api.envelope import Envelope  # noqa: E402
from vkm_corpus.api.fixtures import synthetic_service  # noqa: E402
from nav_manifest_fixture import write_nav_manifest  # noqa: E402
from vkm_corpus.navigation import store  # noqa: E402

READ = "read-token-for-tests-0000000000000000"
HR = {"Authorization": f"Bearer {READ}"}
NAV_SNAP = "snap-nav-test"
SEC = "SEC-0000000000000001"
TOP = "TOP-00000000000000aa"


def _functions():
    def outline(con, source_id):
        rows = con.execute("SELECT section_id, level, title FROM sections WHERE source_id = ? ORDER BY level",
                           [source_id]).fetchall()
        return {"source_id": source_id, "sections": [dict(zip(("section_id", "level", "title"), r)) for r in rows]} \
            if rows else None

    def section(con, section_id):
        row = con.execute("SELECT section_id, source_id, title FROM sections WHERE section_id = ?",
                          [section_id]).fetchone()
        if row is None:
            raise KeyError(section_id)
        return dict(zip(("section_id", "source_id", "title"), row))

    return {"outline": outline, "section": section,
            "explore_concept": lambda con, term, limit=20: {"term": term, "neighbours": [{"term": "реология",
                                                                                          "n_units": 3}][:limit]},
            "find_formulas": lambda con, concept=None, symbol=None, source_id=None: [{"formula_id": "f1",
                                                                                     "concept": concept}],
            "formula_context": lambda con, formula_id: None,
            # topics (agent T) and duplicates (agent U)
            "topic": lambda con, topic_id: {"topic_id": topic_id, "level": 1, "members": [{"section_id": SEC}]}
            if topic_id == TOP else None,
            "find_topics": lambda con, terms, limit=10, level=None: [{"topic_id": TOP, "terms": list(terms)}][:limit],
            "similar_sections": lambda con, section_id, k=10, other_sources_only=True:
                {"section_id": section_id, "similar": [{"section_id": "SEC-0000000000000002"}][:k]},
            "section_topics": lambda con, section_id: [{"topic_id": TOP, "level": 1}],
            "copies_of": lambda con, ref, limit=50: {"match": ref, "clusters": []},
            "source_overlap": lambda con, source_id, limit=50: {"source_id": source_id, "overlaps": []},
            "find_parameters": lambda con, property=None, material=None, site=None, scale=None, source_id=None,
            limit=50: {"query": {"property": property, "scale": scale}, "total": 1,
                       "candidates": [{"value_si_min": 1.0}]},
            "parameter_summary": lambda con, property, material=None: {"query": {"property": property}, "rows": []}}


@pytest.fixture()
def env(tmp_path):
    service, canon, _fakes = synthetic_service(tmp_path / "canon")
    root = tmp_path / "root"
    nav_dir = root / "derived" / "navigation" / NAV_SNAP
    nav_dir.mkdir(parents=True)
    pq.write_table(pa.table({"section_id": [SEC], "source_id": ["VKM-SRC-001"], "level": [1],
                             "title": ["Глава 1. Ползучесть соли"], "title_path": ["Глава 1. Ползучесть соли"]}),
                   nav_dir / "sections.parquet")
    write_nav_manifest(nav_dir, NAV_SNAP)
    store.pack(nav_dir)
    store.publish(root, NAV_SNAP)
    service.deps.nav = store.NavStore(root, canonical_db=canon.duckdb_path, functions=_functions())
    app = create_app(service, ApiConfig(read_tokens={READ: "read"}, write_tokens={}))
    return TestClient(app), service


def _ok(resp):
    assert resp.status_code == 200, resp.text
    body = resp.json()
    Envelope.model_validate(body["item"]["envelope"])
    env = body["item"]["envelope"]
    assert env["layer"] == "PROJECTION" and env["origin"] == "DERIVED"
    assert env["review_status"] == "AUTO_EXTRACTED_UNREVIEWED"
    assert body["item"]["record"]["nav_snapshot_id"] == NAV_SNAP and "not evidence" in body["item"]["record"]["note"]
    return body


def test_outline_section_search_concept_formulas(env):
    client, _ = env
    body = _ok(client.get("/v1/nav/outline/VKM-SRC-001", headers=HR))
    assert body["item"]["envelope"]["object_kind"] == "NAV_OUTLINE"
    assert body["item"]["record"]["sections"][0]["section_id"] == SEC
    assert body["meta"]["warnings"] and body["meta"]["warnings"][0]["code"] == "NAV_SNAPSHOT_BEHIND"
    assert _ok(client.get(f"/v1/nav/section/{SEC}", headers=HR))["item"]["envelope"]["source_id"] == "VKM-SRC-001"
    hits = _ok(client.get("/v1/nav/sections", params={"q": "ползучесть"}, headers=HR))["item"]["record"]["items"]
    assert hits[0]["section_id"] == SEC
    empty = _ok(client.get("/v1/nav/sections", params={"q": "гидрогеология"}, headers=HR))   # 200, not 404
    assert empty["item"]["record"]["items"] == []
    concept = _ok(client.get("/v1/nav/concept", params={"term": "ползучесть соли"}, headers=HR))
    assert concept["item"]["record"]["neighbours"][0]["term"] == "реология"
    formulas = _ok(client.get("/v1/nav/formulas", params={"concept": "скорость ползучести"}, headers=HR))
    assert formulas["item"]["record"]["items"][0]["concept"] == "скорость ползучести"


def test_errors(env):
    client, service = env
    assert client.get("/v1/nav/section/SEC-bad", headers=HR).json()["error"]["code"] == "INVALID_ARGUMENT"
    assert client.get("/v1/nav/section/SEC-00000000000000ff", headers=HR).json()["error"]["code"] == "NOT_FOUND"
    assert client.get("/v1/nav/outline/VKM-SRC-002", headers=HR).json()["error"]["code"] == "NOT_FOUND"
    r = client.get("/v1/nav/formulas", params={"symbol": "σ"}, headers=HR).json()
    assert r["error"]["code"] == "INVALID_ARGUMENT"                    # symbols are looked up inside one source
    assert client.get("/v1/nav/formula/VKM-SRC-001:p0003:f000000000000", headers=HR).json()["error"]["code"] in (
        "NOT_FOUND", "INVALID_ID")
    service.deps.nav = store.NavStore(service.deps.nav.data_root / "missing")
    assert client.get(f"/v1/nav/section/{SEC}", headers=HR).json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert client.get(f"/v1/nav/section/{SEC}").status_code == 401


def test_topics_and_duplicates_routes(env):
    client, _ = env
    body = _ok(client.get(f"/v1/nav/topic/{TOP}", headers=HR))
    assert body["item"]["envelope"]["object_kind"] == "NAV_TOPIC" and body["item"]["record"]["level"] == 1
    assert client.get("/v1/nav/topic/TOP-bad", headers=HR).json()["error"]["code"] == "INVALID_ARGUMENT"
    assert client.get("/v1/nav/topic/TOP-00000000000000bb", headers=HR).json()["error"]["code"] == "NOT_FOUND"
    topics = _ok(client.get("/v1/nav/topics", params=[("term", "закладка"), ("term", "усадка")], headers=HR))
    assert topics["item"]["record"]["items"][0]["terms"] == ["закладка", "усадка"]
    sim = _ok(client.get(f"/v1/nav/similar/{SEC}", params={"k": 5}, headers=HR))
    assert sim["item"]["envelope"]["object_kind"] == "NAV_SIMILAR_SECTIONS"
    assert _ok(client.get(f"/v1/nav/section_topics/{SEC}", headers=HR))["item"]["record"]["items"][0]["topic_id"] == TOP
    copies = _ok(client.get("/v1/nav/copies", params={"ref": "VKM-SRC-001:p0001"}, headers=HR))
    assert copies["item"]["envelope"]["object_kind"] == "NAV_COPIES"
    overlap = _ok(client.get("/v1/nav/overlap/VKM-SRC-001", headers=HR))
    assert overlap["item"]["envelope"]["source_id"] == "VKM-SRC-001"
    assert client.get("/v1/nav/overlap/SRC-1", headers=HR).json()["error"]["code"] == "INVALID_ARGUMENT"


def test_parameter_routes(env):
    client, _ = env
    body = _ok(client.get("/v1/nav/parameters", params={"property": "модуль деформации", "scale": "lab"}, headers=HR))
    assert body["item"]["envelope"]["object_kind"] == "NAV_PARAMETERS"
    assert body["item"]["record"]["query"]["scale"] == "LAB"
    assert client.get("/v1/nav/parameters", headers=HR).json()["error"]["code"] == "INVALID_ARGUMENT"
    assert client.get("/v1/nav/parameters", params={"property": "E", "scale": "X"},
                      headers=HR).json()["error"]["code"] == "INVALID_ARGUMENT"
    summary = _ok(client.get("/v1/nav/parameter_summary", params={"property": "ucs"}, headers=HR))
    assert summary["item"]["envelope"]["object_kind"] == "NAV_PARAMETER_SUMMARY"

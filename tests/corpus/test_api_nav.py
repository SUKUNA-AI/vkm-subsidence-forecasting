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
from vkm_corpus.navigation import store  # noqa: E402

READ = "read-token-for-tests-0000000000000000"
HR = {"Authorization": f"Bearer {READ}"}
NAV_SNAP = "snap-nav-test"
SEC = "SEC-0000000000000001"


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
            "formula_context": lambda con, formula_id: None}


@pytest.fixture()
def env(tmp_path):
    service, canon, _fakes = synthetic_service(tmp_path / "canon")
    root = tmp_path / "root"
    nav_dir = root / "derived" / "navigation" / NAV_SNAP
    nav_dir.mkdir(parents=True)
    pq.write_table(pa.table({"section_id": [SEC], "source_id": ["VKM-SRC-001"], "level": [1],
                             "title": ["Глава 1. Ползучесть соли"], "title_path": ["Глава 1. Ползучесть соли"]}),
                   nav_dir / "sections.parquet")
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

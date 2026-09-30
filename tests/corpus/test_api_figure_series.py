"""Digitized chart series through the VKM API and the MCP read server (agent FD2): /v1/nav/figure_series,
/v1/nav/figure_series/{ref}, the tools find_figure_series / get_figure_series — envelopes, argument errors,
NOT_FOUND, a NAV build without the part. The datasets are small handmade tables of the part's schema (no PDF, no
PyMuPDF); all values are invented."""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")
pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")
pytest.importorskip("pytz", reason="DuckDB TIMESTAMPTZ values in Python need pytz (decision note: extra corpus)")
httpx = pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from vkm_corpus.api.app import ApiConfig, create_app  # noqa: E402
from vkm_corpus.api.envelope import Envelope  # noqa: E402
from vkm_corpus.api.fixtures import synthetic_service  # noqa: E402
from vkm_corpus.navigation import figure_series as FS  # noqa: E402
from vkm_corpus.navigation import store  # noqa: E402

READ = "read-token-for-figure-series-000000000"
H = {"Authorization": f"Bearer {READ}"}
NAV_SNAP = "snap-nav-test"
FIG, FIG2 = "VKM-SRC-001:p0001:f0000000000a1", "VKM-SRC-001:p0002:f0000000000a2"
S1, S2 = "FS-00000000000000b1", "FS-00000000000000b2"


def _results() -> list[dict]:
    base = {"source_id": "VKM-SRC-001", "work_id": "VKM-WRK-001", "status": "DERIVATION",
            "review_status": "AUTO_EXTRACTED_UNREVIEWED", "available_from": "2015-12-31",
            "available_basis": "ASSUMED_FROM_PUBLICATION", "publication_year": 2015, "route": FS.ROUTE,
            "rule_version": FS.RULE_VERSION, "schema": FS.SCHEMA}
    axes = {"x_title_raw": "Время, сут", "x_quantity_raw": "Время", "x_unit_raw": "сут", "x_axis_kind": "LINEAR",
            "x_is_time": True, "x_cal_method": "SNAPPED_TO_TICKS", "x_cal_rms_pt": 0.01,
            "y_title_raw": "Оседание, мм", "y_quantity_raw": "Оседание", "y_unit_raw": "мм", "y_axis_kind": "LINEAR",
            "y_cal_method": "SNAPPED_TO_TICKS", "y_cal_rms_pt": 0.02}
    fig = {**base, **axes, "figure_id": FIG, "page_id": "VKM-SRC-001:p0001", "page_index": 1, "figure_label": "Рис. 1",
           "figure_status": "DIGITIZED", "axis_status": "OK", "n_series": 2, "n_series_both_axes": 2, "n_points": 5,
           "n_points_both_axes": 5, "n_time_series": 2, "flags": ["SCOPE_INHERITED_FROM_SOURCE"],
           "x_cal_label_source": "NATIVE", "y_cal_label_source": "NATIVE", "x_cal_n_labels": 4, "y_cal_n_labels": 4,
           "page_rotation": 0, "keywords_caption": [], "keywords_page": [], "legend_without_curve": [], "notes": []}
    fig2 = {**base, "figure_id": FIG2, "page_id": "VKM-SRC-001:p0002", "page_index": 2, "figure_status": "NO_AXES",
            "axis_status": "NO_AXES", "n_series": 0, "flags": [], "page_rotation": 0, "keywords_caption": [],
            "keywords_page": [], "legend_without_curve": [], "notes": []}
    series, points = [], []
    for sid, idx, label, pts in ((S1, 0, "1990", [(0, 0), (10, -5), (20, -9)]), (S2, 1, "2000", [(0, 0), (10, -2)])):
        series.append({**base, **axes, "series_id": sid, "figure_id": FIG, "page_id": "VKM-SRC-001:p0001",
                       "series_index": idx, "series_label_raw": label, "series_color": "#ff0000",
                       "series_nature": "UNCLASSIFIED", "sampling": "VECTOR_VERTICES", "axes_calibrated": "BOTH",
                       "n_points": len(pts), "x_min": 0.0, "x_max": float(pts[-1][0]),
                       "y_min": float(min(p[1] for p in pts)), "y_max": 0.0, "x_err_median": 0.05,
                       "y_err_median": 0.02, "error_model": FS.ERROR_MODEL,
                       "calibration": '{"x": {"kind": "LINEAR", "n_labels": 4}, "y": {"kind": "LINEAR"}}',
                       "provenance": '{"route": "A", "digitizer_version": "fd-0.1.4"}',
                       "flags": ["SECOND_Y_AXIS"] if sid == S2 else []})
        for i, (x, y) in enumerate(pts):
            points.append({"series_id": sid, "i": i, "figure_id": FIG, "source_id": "VKM-SRC-001",
                           "page_id": "VKM-SRC-001:p0001", "series_label_raw": label, "x": float(x), "y": float(y),
                           "x_err": 0.05, "y_err": 0.02, "x_unit_raw": "сут", "y_unit_raw": "мм",
                           "x_page_pt": 100.0 + x, "y_page_pt": 300.0 - y, "status": "DERIVATION",
                           "review_status": "AUTO_EXTRACTED_UNREVIEWED", "available_from": "2015-12-31",
                           "available_basis": "ASSUMED_FROM_PUBLICATION"})
    return [{"figure": fig, "series": series, "points": points}, {"figure": fig2, "series": [], "points": []}]


def _nav(root, with_part: bool = True) -> None:
    nav_dir = root / "derived" / "navigation" / NAV_SNAP
    nav_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({"section_id": ["SEC-0000000000000001"], "source_id": ["VKM-SRC-001"], "level": [1],
                             "title": ["Глава 1"], "title_path": ["Глава 1"]}), nav_dir / "sections.parquet")
    if with_part:
        for name, table in FS.to_tables(_results()).items():
            pq.write_table(table, nav_dir / f"{name}.parquet")
    store.pack(nav_dir)
    store.publish(root, NAV_SNAP)


@pytest.fixture()
def env(tmp_path):
    service, canon, _fakes = synthetic_service(tmp_path / "canon")
    _nav(tmp_path / "root")
    service.deps.nav = store.NavStore(tmp_path / "root", canonical_db=canon.duckdb_path)
    app = create_app(service, ApiConfig(read_tokens={READ: "read"}))
    return TestClient(app), service, canon, app, tmp_path


def _ok(resp, kind):
    assert resp.status_code == 200, resp.text
    body = resp.json()
    env_ = body["item"]["envelope"]
    Envelope.model_validate(env_)
    assert env_["object_kind"] == kind and env_["layer"] == "PROJECTION" and env_["origin"] == "DERIVED"
    assert env_["review_status"] == "AUTO_EXTRACTED_UNREVIEWED"
    rec = body["item"]["record"]
    assert rec["nav_snapshot_id"] == NAV_SNAP and "not evidence" in rec["note"] and rec["status"] == "DERIVATION"
    return rec


def test_find_route(env):
    client, *_ = env
    rec = _ok(client.get("/v1/nav/figure_series", params={"q": "оседание", "unit": "mm"}, headers=H),
              "NAV_FIGURE_SERIES_LIST")
    assert rec["total_figures"] == 1 and rec["figures"][0]["figure_id"] == FIG
    f = rec["figures"][0]
    assert f["y"]["unit_raw"] == "мм" and f["x"]["is_time"] is True and "axis_title" in f["matched_in"]
    assert [s["series_id"] for s in f["series"]] == [S1, S2] and "never an observation" in rec["status_note"]
    assert [s["suspect"] for s in f["series"]] == [False, True]
    clean = _ok(client.get("/v1/nav/figure_series", params={"q": "оседание", "clean_only": "true"}, headers=H),
                "NAV_FIGURE_SERIES_LIST")
    assert [s["series_id"] for s in clean["figures"][0]["series"]] == [S1]           # a second y axis is suspect
    one = _ok(client.get("/v1/nav/figure_series", params={"q": "1990"}, headers=H), "NAV_FIGURE_SERIES_LIST")
    assert [s["series_id"] for s in one["figures"][0]["series"]] == [S1]           # a series label
    times = _ok(client.get("/v1/nav/figure_series", params={"time_series": "true", "source_id": "VKM-SRC-001"},
                           headers=H), "NAV_FIGURE_SERIES_LIST")
    assert times["total_series"] == 2
    none = _ok(client.get("/v1/nav/figure_series", params={"q": "гравитация"}, headers=H), "NAV_FIGURE_SERIES_LIST")
    assert none["figures"] == [] and none["total_figures"] == 0                      # 200, empty
    loose = _ok(client.get("/v1/nav/figure_series", params={"source_id": "VKM-SRC-001", "calibrated_only": "false"},
                           headers=H), "NAV_FIGURE_SERIES_LIST")
    assert [f["figure_status"] for f in loose["figures"]] == ["DIGITIZED", "NO_AXES"]
    for bad in ({}, {"source_id": "SRC-1"}, {"q": " "}, {"unit": "x" * 41}, {"limit": 0}):
        body = client.get("/v1/nav/figure_series", params=bad, headers=H).json()
        assert body["ok"] is False and body["error"]["code"] == "INVALID_ARGUMENT", bad
    assert client.get("/v1/nav/figure_series", params={"q": "оседание"}).status_code == 401


def test_get_route(env):
    client, *_ = env
    rec = _ok(client.get(f"/v1/nav/figure_series/{S1}", params={"max_points": 2}, headers=H), "NAV_FIGURE_SERIES")
    body = client.get(f"/v1/nav/figure_series/{S1}", headers=H).json()
    assert body["item"]["envelope"]["source_id"] == "VKM-SRC-001"
    assert body["item"]["envelope"]["page_id"] == "VKM-SRC-001:p0001"
    s = rec["series"][0]
    assert s["series_id"] == S1 and s["points_returned"] == 2 and rec["points_truncated"] is True
    assert s["calibration"]["x"]["n_labels"] == 4 and s["provenance"]["digitizer_version"] == "fd-0.1.4"
    assert s["points"][0]["y_err"] == 0.02 and s["y"]["unit_raw"] == "мм"
    whole = _ok(client.get(f"/v1/nav/figure_series/{FIG}", headers=H), "NAV_FIGURE_SERIES")
    assert [x["series_id"] for x in whole["series"]] == [S1, S2] and whole["points_total"] == 5
    empty = _ok(client.get(f"/v1/nav/figure_series/{FIG2}", headers=H), "NAV_FIGURE_SERIES")
    assert empty["series"] == [] and empty["figure"]["figure_status"] == "NO_AXES"
    missing = client.get("/v1/nav/figure_series/FS-00000000000000ff", headers=H).json()
    assert missing["error"]["code"] == "NOT_FOUND"
    bad = client.get("/v1/nav/figure_series/VKM-SRC-001:p0001", headers=H).json()
    assert bad["error"]["code"] == "INVALID_ARGUMENT"


def test_build_without_the_part_is_a_dependency_error(env):
    client, service, canon, _app, tmp_path = env
    _nav(tmp_path / "other", with_part=False)
    service.deps.nav = store.NavStore(tmp_path / "other", canonical_db=canon.duckdb_path)
    body = client.get("/v1/nav/figure_series", params={"q": "оседание"}, headers=H).json()
    assert body["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    body = client.get(f"/v1/nav/figure_series/{S1}", headers=H).json()
    assert body["error"]["code"] == "DEPENDENCY_UNAVAILABLE"


def test_mcp_tools(env):
    pytest.importorskip("mcp")
    from mcp import Client

    from vkm_corpus.mcp.api_client import ApiClient
    from vkm_corpus.mcp.servers import build_read_server

    _client, _service, _canon, app, _tmp = env
    server = build_read_server(ApiClient("http://vkm-api", READ, transport=httpx.ASGITransport(app=app)))

    async def go():
        async with Client(server) as c:
            tools = {t.name: t for t in (await c.list_tools()).tools}
            found = await c.call_tool("find_figure_series", {"text": "оседание", "time_series": True, "limit": 5})
            got = await c.call_tool("get_figure_series", {"ref": FIG, "max_points": 3})
            bad = await c.call_tool("get_figure_series", {"ref": "not-an-id"})
            return tools, found, got, bad

    tools, found, got, bad = asyncio.run(go())
    assert tools["find_figure_series"].annotations.read_only_hint and tools["get_figure_series"].annotations.read_only_hint
    assert found.is_error is False and found.structured_content["item"]["record"]["total_series"] == 2
    rec = got.structured_content["item"]["record"]
    assert got.is_error is False and rec["points_returned"] == 3 and rec["figure"]["figure_id"] == FIG
    assert bad.is_error                                                                  # schema pattern

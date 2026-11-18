"""Exercise the current mine-history CLI with synthetic source files."""
from contextlib import closing
import csv
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from shapely import from_wkb


ROOT = Path(__file__).resolve().parents[2]
LAYOUT = "PANEL_BLOCK_LAYOUT_014"
VALID = {"type": "Polygon", "coordinates": [[[0, 0], [4, 0], [4, 4], [0, 0]]]}
CROSSED = {"type": "Polygon", "coordinates": [[[0, 0], [3, 3], [0, 3], [3, 0], [0, 0]]]}
OPEN_RING = {"type": "Polygon", "coordinates": [[[0, 0], [4, 0], [4, 4], [0, 4]]]}


def feature(geometry, id_, **properties):
    return {"type": "Feature", "geometry": geometry, "properties": {"id": id_, **properties}}


def collection(path, features):
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8")


def write_rows(path, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, rows[0])
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def source(tmp_path):
    canonical = tmp_path / "private/11_evidence_vnext/canonical"
    curated = canonical / "WORLD_PARAMETERS/curated"
    plan = curated / "c4_map/skru1"
    plan.mkdir(parents=True)
    paths = [
        curated / "skru1_mining_calendar_20261010.csv",
        curated / "skru1_c4_chamber_design.csv",
        canonical / "MINING/mining_chronology_catalog.csv",
        canonical / "MINING/mining_geometry_catalog.csv",
        canonical / "MINING/backfill_event_catalog.csv",
        canonical / "WORLD_OBSERVATIONS/world_observation_catalog.csv",
    ]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("unused_header\n", encoding="utf-8")
    collection(plan / "skru1_workings.geojson", [
        feature(VALID, "valid", layer=LAYOUT, kind="BLOCK"),
        feature(CROSSED, "invalid", layer=LAYOUT, kind="BLOCK"),
    ])
    collection(plan / "skru1_boundaries.geojson", [
        feature({"type": "LineString", "coordinates": [[0, 0], [4, 4]]}, "boundary", kind="BOUNDARY"),
    ])
    points = [
        ("PLAN-BHA-A", "BOREHOLE", 901),
        ("PLAN-BHA-B", "BOREHOLE", 902),
        ("PLAN-BHA-FALSE", "BOREHOLE", 903),
        ("control", "BOREHOLE", 8),
        ("shaft", "SHAFT", 9),
        ("zone", "ZONE_POINT_GRID023", 10),
    ]
    collection(plan / "skru1_points.geojson", [
        feature({"type": "Point", "coordinates": [x, 0]}, key, kind=kind) for key, kind, x in points
    ])
    unique = [
        {"key": key, "label_checked": "129", "borehole_id_catalogue": f"BH-{key}",
         "x_local_m": x, "y_local_m": y, "in_auto_layer": ref, "status": "DERIVATION"}
        for key, x, y, ref in [("129N", 1, 2, "PLAN-BHA-A"), ("129SE", 7, 5, "PLAN-BHA-B")]
    ]
    review = [
        {"point_id": row["in_auto_layer"], "label_checked": row["label_checked"],
         "borehole_id_catalogue": row["borehole_id_catalogue"], "x_local_m": row["x_local_m"],
         "y_local_m": row["y_local_m"], "status": "CORRECTED"}
        for row in unique
    ]
    review.append({**review[0], "point_id": "PLAN-BHA-FALSE", "status": "FALSE_POSITIVE"})
    write_rows(plan / "skru1_boreholes_014_unique.csv", unique)
    write_rows(plan / "skru1_boreholes_checked.csv", review)
    collection(tmp_path / "1990.geojson", [
        feature(CROSSED, "invalid-1990", **{"class": "WORKED_C1990"}, seam="АБ",
                source_id="S", page_id="P", status="DERIVATION"),
    ])
    return plan, unique


def run_history(root):
    return subprocess.run(
        [sys.executable, "-I", "-B", "-X", "utf8", str(ROOT / "scripts/build_skru1_mine_history.py"),
         "--private", str(root / "private"), "--stream-out", str(root / "stream"),
         "--delivery", str(root / "delivery"), "--state-1990", str(root / "1990.geojson"),
         "--no-figures"],
        cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=40,
    )


def gpkg(root):
    uri = (root / "delivery/data/skru1_mine_history_sh1.gpkg").as_uri()
    return closing(sqlite3.connect(uri + "?mode=ro", uri=True))


def test_invalid_source_is_held_with_unchanged_geometry(tmp_path, source):
    raw_path = source[0] / "skru1_workings.geojson"
    before = raw_path.read_bytes()
    result = run_history(tmp_path)
    assert result.returncode == 0, result.stderr
    with gpkg(tmp_path) as db:
        assert db.execute("SELECT region_id FROM plan_regions_014").fetchall() == [("valid",)]
        assert db.execute("SELECT feature_id FROM plan_states").fetchall() == []
        assert db.execute("SELECT boundary_id FROM boundaries").fetchall() == [("boundary",)]
    hold = json.loads((tmp_path / "delivery/data/source_geometry_holds.json").read_text(encoding="utf-8"))
    assert {row["feature"]["properties"]["id"] for row in hold} == {"invalid", "invalid-1990"}
    assert all(row["feature"]["geometry"] == CROSSED for row in hold)
    assert all(row["status"] == "HELD_SOURCE_GEOMETRY" for row in hold)
    assert hold[0]["source_sha256"] == hashlib.sha256(before).hexdigest()
    assert raw_path.read_bytes() == before


def test_open_ring_is_not_closed_by_the_geometry_parser(tmp_path, source):
    plan, _ = source
    collection(plan / "skru1_workings.geojson", [
        feature(VALID, "valid", layer=LAYOUT, kind="BLOCK"),
        feature(OPEN_RING, "open", layer=LAYOUT, kind="BLOCK"),
    ])
    result = run_history(tmp_path)
    assert result.returncode == 0, result.stderr
    with gpkg(tmp_path) as db:
        assert db.execute("SELECT region_id FROM plan_regions_014").fetchall() == [("valid",)]
    hold = json.loads((tmp_path / "delivery/data/source_geometry_holds.json").read_text(encoding="utf-8"))
    assert next(row for row in hold if row["source_row"] == 1)["feature"]["geometry"] == OPEN_RING


def test_points_use_checked_symbol_identities_and_keep_other_points(tmp_path, source):
    result = run_history(tmp_path)
    assert result.returncode == 0, result.stderr
    with gpkg(tmp_path) as db:
        rows = {row[0]: row[1:] for row in db.execute(
            "SELECT point_id, kind, label, object_id, geom FROM points"
        )}
    assert rows.keys() == {"C4:skru1:well_symbol:129N", "C4:skru1:well_symbol:129SE",
                           "control", "shaft", "zone"}
    for key, xyz in (("129N", (1, 2)), ("129SE", (7, 5))):
        kind, label, object_id, blob = rows[f"C4:skru1:well_symbol:{key}"]
        assert kind == "BOREHOLE_CHECKED_SYMBOL"
        assert label == "129"
        assert object_id == f"BH-{key}"
        assert tuple(from_wkb(blob[40:]).coords)[0] == xyz


@pytest.mark.parametrize("conflict", ["rejected", "duplicate_key", "changed_position"])
def test_invalid_checked_overlay_cannot_emit_delivery(tmp_path, source, conflict):
    plan, unique = source
    if conflict == "rejected":
        unique[0]["in_auto_layer"] = "PLAN-BHA-FALSE"
    elif conflict == "duplicate_key":
        unique[0]["key"] = unique[1]["key"]
    else:
        unique[0]["x_local_m"] = 999
    write_rows(plan / "skru1_boreholes_014_unique.csv", unique)
    result = run_history(tmp_path)
    assert result.returncode != 0, result.stdout
    assert "checked SKRU-1 symbol" in result.stderr
    assert not (tmp_path / "delivery").exists()

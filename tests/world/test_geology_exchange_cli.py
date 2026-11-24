"""Explicit object/frame exchange, with synthetic coordinates only."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

import numpy as np
import pytest

from vkm_world.geometry_build.contact_predictor import observations_by_borehole, supports_from_borehole_rows
from vkm_world.geometry_build.height_frames import declared_coordinate_frame

ROOT = Path(__file__).resolve().parents[2]


def frame(names):
    pair = {"horizontal_frame": "SYNTHETIC_XY", "height_frame": "SYNTHETIC_Z_UNKNOWN_DATUM"}
    return {"schema": "vkm.coordinate_frame_declaration/1", **pair,
            "length_unit": "m", "z_positive": "UP", "global_tie": "UNKNOWN",
            "basis": "SYNTHETIC_IDENTITY_ASSERTION_NO_TRANSFORM",
            "inputs": {key: pair.copy() for key in names}}


def fixture_files(directory):
    directory.mkdir(parents=True, exist_ok=True)
    def save(name, value):
        path = directory / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path
    xy = np.array([[0., 0.], [10., 0.], [10., 10.], [0., 10.]])
    tri = np.array([[0, 1, 2], [0, 2, 3]])
    surface = directory / "surface.npz"
    np.savez(surface, xy=xy, z=np.full(4, 100.), triangles=tri)
    names = ["top", "bottom"]
    contacts = save("contacts.json", {"ordered_contacts": names, "rows": [
        {"physical_id": f"w{i}", "depths_m": [10.+i, 20.+i]} for i in range(4)]})
    placements = save("placements.json", {"rows": [{"physical_id": f"w{i}", "x": x, "y": y,
        "terrain_height_m": 100.} for i, (x,y) in enumerate(xy)]})
    config = save("config.json", {"site_offset_m": 0., "cv_block_size_m": 5.})
    observations = save("observations.json", {"schema": "vkm.borehole_observation_matrix/1", "rows": []})
    def feature(ident, kind, coordinates):
        return {"type": "Feature", "properties": {"object_id": ident},
                "geometry": {"type": kind, "coordinates": coordinates}}
    objects = save("objects.geojson", {"type": "FeatureCollection", "features": [
        feature("start", "Point", [1., 1.]), feature("end", "Point", [9., 9.]),
        feature("unselected", "Point", [4., 4.])]})
    boundary = save("boundary.geojson", {"type": "FeatureCollection", "features": [
        feature("mine-synthetic", "Polygon", [[[0,0],[10,0],[10,10],[0,10],[0,0]]])]})
    declaration = save("frame.json", frame(["contacts", "placements", "surface", "boundary", "objects", "field", "sections"]))
    field = directory / "field.npz"
    levels = np.column_stack([np.full(4, 90.), np.full(4, 80.)])
    np.savez(field, xy=xy, contacts=levels, triangles=tri, contact_names=names,
             geometry_version="synthetic-v1", variant="synthetic", height_status="UNKNOWN_DATUM",
             support_mask=np.full(4, "INTERPOLATION"), dsm=np.full(4, 100.))
    sections = save("sections.json", {"trace-main": {"geometry_version": "synthetic-v1",
        "xy": [[1.,1.],[9.,9.]], "levels": [[90.,80.],[90.,80.]]},
        "trace-unselected": {"geometry_version": "unused", "xy": [], "levels": []}})
    for name in ["history.csv", "observations.csv"]:
        (directory / name).write_text("record_id,status\nr1,UNKNOWN\n", encoding="utf-8")
    return {"config": config, "contacts": contacts, "placements": placements,
            "observations": observations, "surface": surface, "boundary": boundary,
            "objects": objects, "frame": declaration, "field": field, "sections": sections}


def test_frame_requires_matching_explicit_inputs_preserving_unknown():
    d = frame(["surface"])
    assert declared_coordinate_frame(d, ["surface"])["global_tie"] == "UNKNOWN"
    d["inputs"]["surface"]["height_frame"] = "OTHER"
    with pytest.raises(ValueError, match="frame mismatch"):
        declared_coordinate_frame(d, ["surface"])


@pytest.mark.parametrize("key,value", [("length_unit", "ft"), ("z_positive", "DOWN"),
                                       ("horizontal_frame", "UNKNOWN"), ("basis", "")])
def test_frame_rejects_implicit_or_changed_coordinate_contract(key, value):
    d = frame(["field"]); d[key] = value
    with pytest.raises(ValueError):
        declared_coordinate_frame(d, ["field"])


def test_support_adapter_preserves_missing_bounds_and_absence_separately():
    data = {"schema": "vkm.borehole_observation_matrix/1", "rows": [
        {"physical_id": "partial", "kind": "not_reached", "contact": "bottom",
         "value_m": 20., "use_in_model": True},
        {"physical_id": "absent", "kind": "absent", "top_contact": "top", "bottom_contact": "bottom",
         "source_id": "synthetic-absence", "use_in_model": True}]}
    rows = [{"physical_id": "partial", "x": 0., "y": 0., "collar_scenario_m": 100., "depths_m": [10., None]},
            {"physical_id": "absent", "x": 1., "y": 0., "collar_scenario_m": 100., "depths_m": [10., None]}]
    supports = supports_from_borehole_rows(rows, ["top", "bottom"], observations_by_borehole(data, ["top", "bottom"]), {})
    assert supports.depth_lower[0, 1] == 20.
    assert np.isnan(supports.depth_upper[0, 1])
    assert np.isnan(supports.depth_lower[1, 1])
    assert supports.thickness == [{"row": 1, "top": 0, "bottom": 1, "lower": 0., "upper": 0., "id": "synthetic-absence"}]


def test_assessment_cli_uses_selected_object_trace_and_frame(tmp_path):
    files = fixture_files(tmp_path)
    output = ROOT / "work" / ("l03_assessment_test_" + uuid.uuid4().hex)
    keys = ("config", "contacts", "placements", "observations", "surface", "boundary", "objects", "frame")
    cmd = [sys.executable, str(ROOT / "scripts/assess_geology.py")]
    for key in keys:
        cmd += ["--"+key, str(files[key])]
    cmd += ["--output", str(output), "--object-id", "mine-synthetic", "--method", "idw2",
            "--withdraw-collar", "w0", "--trace-start", "start", "--trace-end", "end", "--query-points", "5"]
    try:
        result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
        report = json.loads((output / "assessment.json").read_text(encoding="utf-8"))
        assert report["object_id"] == "mine-synthetic"
        assert report["trace"]["frame"] == "SYNTHETIC_XY"
        assert report["coordinate_declaration"]["global_tie"] == "UNKNOWN"
        assert report["mechanical_QoI"]["status"] == "NOT_RUN"
        assert set(report["variants"]) == {"B_baseline", "B_relative_only", "C_baseline", "C_relative_only"}
        failed = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        assert failed.returncode != 0 and "new output" in failed.stderr
        wrong = cmd.copy(); wrong[wrong.index("mine-synthetic")] = "missing-object"
        wrong[wrong.index(str(output))] = str(output.with_name(output.name+"_invalid"))
        failed = subprocess.run(wrong, cwd=ROOT, capture_output=True, text=True)
        assert failed.returncode != 0 and "uniquely identified" in failed.stderr
        assert not output.with_name(output.name+"_invalid").exists()
    finally:
        assert output.resolve().is_relative_to((ROOT / "work").resolve())
        if output.exists():
            shutil.rmtree(output)


def test_cli_has_no_implicit_legacy_scene_or_object_defaults():
    result = subprocess.run([sys.executable, str(ROOT / "scripts/assess_geology.py"), "--help"],
                            cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0
    assert "--previous-scene" not in result.stdout
    assert "--object-id OBJECT_ID" in result.stdout and "--frame FRAME" in result.stdout
    result = subprocess.run([sys.executable, str(ROOT / "scripts/assess_geology.py")],
                            cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 2 and "--frame" in result.stderr and "--object-id" in result.stderr

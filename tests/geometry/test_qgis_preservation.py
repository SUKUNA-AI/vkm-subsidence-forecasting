"""Orchestrator regression tests with an explicit fake; native QGIS is separate."""
import copy
import json

import pytest
from shapely.geometry import shape

from vkm_geometry.qgis_check import check_qgis
from vkm_geometry.transport import assert_rows_equal, batch_rows, read_wkb_jsonl
from test_transport import synthetic_batch

BOWTIE = {"type": "Polygon", "coordinates": [[[0, 0], [10, 10], [0, 10], [10, 0], [0, 0]]]}
SQUARE = {"type": "Polygon", "coordinates": [[[20, 0], [30, 0], [30, 10], [20, 10], [20, 0]]]}


def source_rows(kind):
    batch = synthetic_batch()
    geometries = {"valid": [SQUARE], "invalid": [BOWTIE], "mixed": [BOWTIE, SQUARE],
                  "partial": [BOWTIE, {"type": "LineString", "coordinates": [[25, 5], [35, 15]]}]}[kind]
    batch["features"] = batch["features"][:len(geometries)]
    batch["project_matches"] = batch["project_matches"][:len(geometries)]
    for feature, geometry in zip(batch["features"], geometries): feature["geometry"] = copy.deepcopy(geometry)
    return batch_rows(batch)


def install_fake(monkeypatch, *, failure=None, corruption=None):
    instances = []
    class FakeService:
        def __init__(self, paths, timeout):
            self.output = paths.roots["work"]; self.layers = {}; self.tools = []; instances.append(self)
        def call(self, tool, args):
            self.tools.append((tool, args))
            if tool == failure: return {"ok": False, "error": {"code": "SYNTHETIC_FAILURE"}}
            result = {}
            if tool == "qgis_status": result = {"available": True}
            elif tool == "qgis_import_vector":
                features = json.loads((self.output / args["source_path"].removeprefix("work:")).read_text(encoding="utf-8"))["features"]
                self.layers[args["name"]] = (features, args)
                count = sum(not shape(f["geometry"]).is_valid for f in features)
                result = {"topology_status": "FAIL" if count else "PASS", "invalid_geometry_count": count}
            elif tool == "qgis_feature":
                features, original = self.layers[args["layer"]]; got = []
                for feature in features[args["offset"]:args["offset"] + args["limit"]]:
                    geometry = copy.deepcopy(feature["geometry"])
                    row = json.loads(feature["properties"]["transport_row_json"])
                    if corruption == "geometry": geometry["coordinates"][0][0][0] += 1
                    if corruption == "attributes":
                        source = json.loads(row["feature_json"]); source["source_label"] = "CHANGED"; row["feature_json"] = json.dumps(source)
                    got.append({"geometry": geometry, "properties": {"stable_id": feature["properties"]["stable_id"], "properties_json": json.dumps({"transport_row_json": json.dumps(row)})}})
                result = {"features": got, "coordinate_reference": {"frame_id": original["frame_id"], "unit": original["unit"]}}
            elif tool == "qgis_render":
                assert all(all(shape(f["geometry"]).is_valid for f in self.layers[name][0]) for name in args["layers"])
            return {"ok": True, "result": result}
    monkeypatch.setattr("vkm_qgis.service.QgisService", FakeService)
    monkeypatch.setattr("vkm_geometry.ta1_worker.normalize_new_cartesian_package", lambda path: {"status": "PASS"})
    return instances


def run(rows, output):
    return check_qgis(rows, output, public_root=output.parent, private_root=output.parent, qgis_root=output.parent)


@pytest.mark.parametrize("kind,render", [("valid", "PASS"), ("invalid", "SKIPPED_SOURCE_INVALID"), ("mixed", "SKIPPED_SOURCE_INVALID"), ("partial", "PARTIAL_SOURCE_INVALID")])
def test_source_topology_is_separate_from_persisted_lossless_transport(monkeypatch, tmp_path, kind, render):
    instances = install_fake(monkeypatch); rows = source_rows(kind); before = copy.deepcopy(rows)
    result = run(rows, tmp_path / "out")
    assert_rows_equal(rows, read_wkb_jsonl(tmp_path / "out/qgis_readback.jsonl")); assert rows == before
    assert result == json.loads((tmp_path / "out/receipt.json").read_text(encoding="utf-8"))
    assert result["status"] == result["geometry_transport_status"] == "PASS"
    assert result["status_scope"] == "LOSSLESS_GEOMETRY_TRANSPORT_ONLY"
    assert result["source_topology_status"] == ("PASS" if kind == "valid" else "FAIL")
    assert result["render_status"] == render and result["project_status"] == "PASS"
    assert result["qualification_status"] == ("PASS" if kind == "valid" else "PARTIAL_SOURCE_TOPOLOGY_FAILURE")
    if kind in {"invalid", "mixed"}: assert not any(tool == "qgis_render" for tool, _ in instances[0].tools)
    if kind != "valid": assert result["limitations"] and result["render_checks"][0]["skipped_invalid_source_layers"]


@pytest.mark.parametrize("failure,field", [("qgis_render", "render_status"), ("qgis_project", "project_status")])
def test_downstream_failure_retains_verified_readback_and_failure_receipt(monkeypatch, tmp_path, failure, field):
    install_fake(monkeypatch, failure=failure); rows = source_rows("valid")
    with pytest.raises(RuntimeError, match="SYNTHETIC_FAILURE"): run(rows, tmp_path / "out")
    assert_rows_equal(rows, read_wkb_jsonl(tmp_path / "out/qgis_readback.jsonl"))
    receipt = json.loads((tmp_path / "out/receipt.json").read_text(encoding="utf-8"))
    assert receipt["geometry_transport_status"] == "PASS" and receipt[field] == "FAIL"
    assert receipt["qualification_status"] == "DOWNSTREAM_CHECK_FAILED"


@pytest.mark.parametrize("corruption", ["geometry", "attributes"])
def test_actual_coordinate_or_attribute_loss_still_rejected(monkeypatch, tmp_path, corruption):
    install_fake(monkeypatch, corruption=corruption)
    with pytest.raises(ValueError): run(source_rows("valid"), tmp_path / "out")
    assert not (tmp_path / "out/qgis_readback.jsonl").exists()

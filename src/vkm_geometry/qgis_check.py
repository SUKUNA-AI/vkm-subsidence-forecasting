"""Real QGIS worker and native CAD checks using the existing project bridges."""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import re

from .transport import (assert_geometry_equal, assert_rows_equal, canonical_json, geometry_parts,
                        read_dxf, row_digest, validate_rows, write_wkb_jsonl, coordinate_frame)


def check_qgis(rows, output: Path, *, public_root: Path, private_root: Path, qgis_root: Path):
    from vkm_qgis.service import QgisService
    from vkm_qgis.policy import Paths
    from .ta1_worker import normalize_new_cartesian_package
    from shapely.geometry import shape
    from shapely import to_wkb
    validate_rows(rows)
    if output.exists() and any(output.iterdir()): raise FileExistsError(output)
    output.mkdir(parents=True, exist_ok=True)
    service = QgisService(Paths(public_root, output, private_root), timeout=180)
    calls = []
    def call(tool, **arguments):
        payload = service.call(tool, arguments)
        calls.append(payload)
        (output / "qgis_calls.json").write_text(json.dumps(calls, ensure_ascii=False, indent=2), encoding="utf-8")
        if not payload["ok"]: raise RuntimeError(f"{tool}: {payload['error']}")
        return payload["result"]
    original = os.environ.get("VKM_QGIS_ROOT")
    os.environ["VKM_QGIS_ROOT"] = str(qgis_root)
    try:
        status = call("qgis_status")
        if not status.get("available"): raise RuntimeError("Existing QGIS runtime unavailable")
        call("qgis_create_geopackage", path="work:source.qgis.gpkg", minimal_layers=False, frame_id="MULTIPLE_EXPLICIT_LAYERS")
        groups = defaultdict(list)
        for row in rows:
            feature = json.loads(row["feature_json"])
            groups[(canonical_json(coordinate_frame(feature)), row["geometry_type"])].append(row)
        (output / "inputs").mkdir()
        readback, frame_layers, layer_index = [], defaultdict(list), []
        for (frame_json, geometry_type), group in sorted(groups.items()):
            frame = json.loads(frame_json)
            chunks, chunk, byte_count = [], [], 0
            for row in group:
                feature = json.loads(row["feature_json"])
                item = {"type": "Feature", "geometry": feature["geometry"], "properties": {
                    "stable_id": row["source_feature_id"], "source_id": row["source_id"],
                    "figure_id": feature["figure_id"], "source_object_id": row["source_feature_id"],
                    "verification": "AUTO_EXTRACTED_UNREVIEWED", "epistemic_status": "DERIVATION",
                    "transport_row_json": canonical_json(row)}}
                encoded_size = len(canonical_json(item).encode("utf-8"))
                if encoded_size > 30_000_000: raise ValueError("Single source feature exceeds existing QGIS import limit")
                if chunk and byte_count + encoded_size > 8_000_000:
                    chunks.append(chunk); chunk = []; byte_count = 0
                chunk.append(item); byte_count += encoded_size
            if chunk: chunks.append(chunk)
            for index, features in enumerate(chunks):
                name = "g_" + hashlib.sha256((frame_json + geometry_type + str(index)).encode()).hexdigest()[:20]
                source_ref = f"work:inputs/{name}.geojson"
                source = {"type": "FeatureCollection", "vkm_coordinate_reference": frame, "features": features}
                (output / "inputs" / f"{name}.geojson").write_text(canonical_json(source), encoding="utf-8")
                imported = call("qgis_import_vector", source_path=source_ref, path="work:source.qgis.gpkg", name=name,
                                crs_kind="SOURCE_COORDINATES", frame_id=frame["id"], unit=frame["unit"], geometry_type=geometry_type,
                                preserve_invalid_source=True)
                # Existing worker writes OUR new GPKG. Normalize its standard
                # undefined Cartesian SRS too; source coordinates/attrs stay exact.
                normalized = normalize_new_cartesian_package(output / "source.qgis.gpkg")
                calls.append({"operation": "new_output_standard_Cartesian_SRS", "result": normalized})
                (output / "qgis_calls.json").write_text(json.dumps(calls, ensure_ascii=False, indent=2), encoding="utf-8")
                frame_layers[frame_json].append(name)
                layer_index.append({"layer": name, "frame": frame, "geometry_type": geometry_type,
                                    "features": len(features), "topology_status": imported["topology_status"]})
                offset = 0
                while offset < len(features):
                    page = call("qgis_feature", path="work:source.qgis.gpkg", layer=name, action="list", limit=100, offset=offset)
                    if not page["features"]: raise ValueError("QGIS readback unexpectedly truncated")
                    if page["coordinate_reference"]["frame_id"] != frame["id"] or page["coordinate_reference"]["unit"] != frame["unit"]:
                        raise ValueError("QGIS coordinate metadata changed")
                    for got in page["features"]:
                        extras = json.loads(got["properties"]["properties_json"])
                        row = json.loads(extras["transport_row_json"])
                        if got["properties"]["stable_id"] != row["source_feature_id"]: raise ValueError("QGIS stable ID changed")
                        original_geometry = json.loads(row["feature_json"])["geometry"]
                        assert_geometry_equal(original_geometry, got["geometry"])
                        actual = dict(row, wkb_hex=to_wkb(shape(got["geometry"]), byte_order=1, include_srid=False).hex())
                        actual["row_sha256"] = row_digest(actual)
                        readback.append(actual)
                    offset += len(page["features"])
                call("qgis_export_layer", path="work:source.qgis.gpkg", layer=name, output_path=f"work:{name}.export.geojson")
        call("qgis_list_layers", path="work:source.qgis.gpkg")
        # Different frames are never superposed in one render/project.
        for index, (frame_json, layers) in enumerate(sorted(frame_layers.items())):
            if len(layers) > 30: raise ValueError("Frame exceeds bounded QGIS render layer count")
            call("qgis_render", path="work:source.qgis.gpkg", layers=layers, output_path=f"work:frame_{index:03d}.png", width=1024, height=768)
            call("qgis_project", path=f"work:frame_{index:03d}.qgz", action="create",
                 layers=[{"path": "work:source.qgis.gpkg", "layer": name, "name": name} for name in layers])
        assert_rows_equal(rows, readback)
        write_wkb_jsonl(readback, output / "qgis_readback.jsonl")
        result = {"status": "PASS", "bridge": "vkm_qgis.QgisService -> existing isolated QGIS worker", "qgis": status,
                  "features": len(readback), "frames": len(frame_layers), "layers": layer_index,
                  "vertex_tolerance": 1e-9, "source_topology_failures_preserved": sum(x["topology_status"] == "FAIL" for x in layer_index),
                  "geometry_policy": "source coordinates preserved; no georeferencing/MakeValid/FACT promotion"}
        (output / "receipt.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result
    finally:
        if original is None: os.environ.pop("VKM_QGIS_ROOT", None)
        else: os.environ["VKM_QGIS_ROOT"] = original


def check_cad(rows, source_dxf: Path, output: Path):
    from vkm_cad.service import CadService
    validate_rows(rows)
    assert_rows_equal(rows, read_dxf(source_dxf))
    if output.exists() and any(output.iterdir()): raise FileExistsError(output)
    output.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, VKM_WORK=str(output), VKM_CAD_JOBS=str(output / "cad_jobs"))
    service = CadService.from_env(env)
    calls = []
    def save(name, result):
        calls.append({"operation": name, "result": result})
        (output / "cad_calls.json").write_text(json.dumps(calls, ensure_ascii=False, indent=2), encoding="utf-8")
        return result
    job_info = save("job_create", service.jobs.job_create(label="VKM source geometry roundtrip", product="ACAD"))
    job_id = job_info["job_id"]
    save("convert_dwg", service.jobs.convert(job_id, source_file=source_dxf, to="dwg", as_job_drawing=True))
    query = save("native_query", service.jobs.query(job_id, '(list (getvar "INSUNITS") (sslength (ssget "_X")))', kind="lisp", timeout_s=90))
    count = sum(len(geometry_parts(json.loads(row["feature_json"])["geometry"])) for row in rows)
    if query["value"] != f"(0 {count})": raise ValueError("Native CAD modelspace count/units changed")
    exported = save("convert_dxf", service.jobs.convert(job_id, source="drawing", to="dxf", name="native_readback", dxf_version="2013"))
    job = service.jobs.store.get(job_id)
    actual = read_dxf(job.dir(exported["path"]))
    assert_rows_equal(rows, actual)
    write_wkb_jsonl(actual, output / "cad_readback.jsonl")
    receipt = json.loads((job.path / "receipt.json").read_text(encoding="utf-8"))
    if not receipt["runs"] or not all(r["ok"] and r["exit_code"] == 0 and not r["crashed"] and not r["dialog_blocked"] for r in receipt["runs"]):
        raise ValueError("Native CAD run failed")
    if not all(r["side_effects_audit"]["status"] == "AUDITED" and r["side_effects_audit"]["registry_changes_count"] == 0
               and r["side_effects_audit"]["profile_file_changes_count"] == 0 for r in receipt["runs"]):
        raise ValueError("Native CAD profile/registry side effect")
    result = {"status": "PASS", "bridge": "vkm_cad.CadService/CadJobs, AutoCAD Core Console", "job_id": job_id,
              "features": len(rows), "modelspace_entities": count, "native_runs": len(receipt["runs"]),
              "vertex_tolerance": 1e-9, "full_metadata_xrecords": "PRESERVED_AND_READ_BACK", "gui": "NOT_USED",
              "source_dxf_sha256": hashlib.sha256(source_dxf.read_bytes()).hexdigest(), "native_dxf_sha256": exported["sha256"]}
    (output / "receipt.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result

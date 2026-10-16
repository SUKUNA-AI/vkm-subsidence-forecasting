"""Build an explicit joint-display scenario without changing source geometry.

Actual spatial data must remain in ignored work/ or the PRIVATE repository.
Requires NumPy, Matplotlib and VTK; no mechanics solver is executed.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import shutil
import sys
import xml.etree.ElementTree as ET

import numpy as np
import vtk
from vtk.util.numpy_support import numpy_to_vtk, vtk_to_numpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from vkm_world.geometry_build.column_display import TINSurface, column_translation


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(4*1024*1024), b""):
            h.update(block)
    return h.hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def save(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")


def encoded(array, dtype="f4"):
    a = np.asarray(array, dtype="<"+dtype, order="C")
    return {"dtype": dtype, "shape": list(a.shape), "data": base64.b64encode(a.tobytes()).decode("ascii")}


def decoded(spec):
    return np.frombuffer(base64.b64decode(spec["data"]), dtype="<"+spec["dtype"].lstrip("<")).reshape(spec["shape"])


def reader(path):
    r = vtk.vtkXMLUnstructuredGridReader()
    r.SetFileName(str(path)); r.Update()
    if r.GetOutput().GetNumberOfPoints() == 0:
        raise ValueError(str(path))
    return r.GetOutput()


def write_grid(grid, path):
    w = vtk.vtkXMLUnstructuredGridWriter()
    w.SetFileName(str(path)); w.SetInputData(grid)
    w.SetDataModeToAppended(); w.SetCompressorTypeToZLib()
    if not w.Write():
        raise ValueError("VTK write failed: "+str(path))


def run(args):
    repo = Path(__file__).resolve().parents[1]
    out = args.output.resolve()
    if not out.is_relative_to((repo / "work").resolve()):
        raise ValueError("real-data output must stay in ignored work/")
    (out / "data").mkdir(parents=True, exist_ok=True)
    (out / "native").mkdir(exist_ok=True)
    previous = args.previous.resolve()
    old_root = previous.parent
    geometry = read(previous / "geometry_receipt.json")
    manifest = read(previous / "data/complete_manifest.json")
    terrain_path = old_root / "full_massif_fix/terrain/terrain_display.json"
    terrain = read(terrain_path)
    terrain_points = np.asarray(terrain["points"], dtype=np.float64)
    terrain_triangles = np.asarray(terrain["triangles"], dtype=np.int64)
    inputs = {"builder": Path(__file__), "column_display": repo / "src/vkm_world/geometry_build/column_display.py",
              "source_geometry_receipt": previous / "geometry_receipt.json", "smoothed_surface": terrain_path,
              "source_manifest": previous / "data/complete_manifest.json",
              "source_scene": previous / "data/scene.js", "observations": previous / "data/supplement.js",
              "height_frame_review": out / "evidence/height_frame_review.json"}
    input_pins = {k: sha(v) for k, v in inputs.items() if v.exists()}
    print("Read native top contact and original TIN", flush=True)
    top_path = previous / "data/complete_Q.vtu"
    assert sha(top_path) == geometry["outputs_sha256"]["data/complete_Q.vtu"]
    top_grid = reader(top_path)
    p = vtk_to_numpy(top_grid.GetPoints().GetData())
    n = len(p)//2
    xy = p[:n, :2].copy(); native_top = p[n:, 2].copy()
    cells = vtk_to_numpy(top_grid.GetCells().GetConnectivityArray()).reshape(-1, 6)
    triangles = cells[:, [0, 2, 1]].copy()
    surface = TINSurface(terrain_points, terrain_triangles)
    surface_on_mesh = surface.sample(xy)
    if not np.isfinite(surface_on_mesh).all():
        raise ValueError("regional vertices outside original surface support")
    delta = column_translation(native_top, surface_on_mesh)
    top = TINSurface(np.column_stack([xy, native_top]), triangles)

    def offset_at(query):
        return surface.sample(query)-top.sample(query)

    stats = {"minimum_m": float(delta.min()), "maximum_m": float(delta.max()),
             "median_m": float(np.median(delta)), "range_is_measurement_uncertainty": False}
    joint = {"schema": "vkm.unified_display/1", "delta": encoded(delta),
             "meta": {"height_mode": "COLUMN_DSM_CLOSURE", "status": "ENGINEERING_ASSUMPTION_DISPLAY",
                      "formula": "z_joint(x,y)=z_native(x,y)+H_DSM_smooth(x,y)-z_top_native(x,y)",
                      "source_height_frame": "mine-source elevation frame; tie to EGM2008 UNKNOWN",
                      "delta_statistics": stats, "preserved": "vertical layer intervals and order at each XY",
                      "not_preserved": "native absolute roof bounds, dip and normal thickness",
                      "DSM_role": "observed surface model, not bedrock surface or 2026 survey"}}
    (out / "data/unified.js").write_text("window.VKM_UNIFIED="+json.dumps(joint, ensure_ascii=False, separators=(",", ":"))+";\n", encoding="utf-8")
    shutil.copyfile(previous / "data/scene.js", out / "data/scene.js")
    shutil.copyfile(previous / "data/supplement.js", out / "data/supplement.js")
    shutil.copyfile(previous / "data/terrain_source.vtp", out / "native/terrain_source.vtp")
    shutil.copyfile(previous / "data/terrain_display.vtp", out / "native/terrain_display.vtp")
    entries = [("Observed DSM source", "native/terrain_source.vtp"), ("Smoothed DSM display", "native/terrain_display.vtp")]
    checks = []
    prior_receipt = read(out / "data_receipt.json") if args.objects_only else None
    for unit in ["PDKS", "SZ", "SKZ", "PKS", "PP", "SMT", "TKT", "PCT", "Q"]:
        destination = out / ("native/joint_"+unit+".vtu")
        if args.objects_only:
            assert sha(destination) == prior_receipt["outputs_sha256"][destination.relative_to(out).as_posix()]
            checks.append(next(row for row in prior_receipt["native_checks"] if row["unit"] == unit))
            entries.append((unit+" column closure scenario", destination.relative_to(out).as_posix()))
            continue
        print("Translate full native volume "+unit, flush=True)
        source = previous / ("data/complete_"+unit+".vtu")
        assert sha(source) == geometry["outputs_sha256"]["data/complete_"+unit+".vtu"]
        grid = reader(source)
        points = vtk_to_numpy(grid.GetPoints().GetData()).copy()
        assert len(points) == 2*n and np.array_equal(points[:n, :2], xy)
        original_z = points[:, 2].copy()
        offsets = np.tile(delta, 2)
        points[:, 2] += offsets
        original_thickness = original_z[n:]-original_z[:n]
        error = float(np.max(np.abs((points[n:, 2]-points[:n, 2])-original_thickness)))
        grid.GetPoints().SetData(numpy_to_vtk(points, deep=True))
        for name, values in [("OriginalSourceZ", original_z), ("DisplayColumnTranslation", offsets)]:
            array = numpy_to_vtk(values, deep=True); array.SetName(name); grid.GetPointData().AddArray(array)
        note = vtk.vtkStringArray(); note.SetName("InterpretationStatus"); note.InsertNextValue("ENGINEERING_ASSUMPTION_DISPLAY; unresolved vertical tie")
        grid.GetFieldData().AddArray(note)
        write_grid(grid, destination)
        entries.append((unit+" column closure scenario", destination.relative_to(out).as_posix()))
        checks.append({"unit": unit, "vertices": len(points), "wedges": grid.GetNumberOfCells(),
                       "max_vertical_interval_change_m": error, "positive_thickness": bool(np.all(original_thickness > 0)),
                       "original_Z_retained": True})
    # All nested objects receive precisely the same regional field, not a
    # separate fit to their individual top contact.
    pack = out / "data/spatial_objects_source.json"
    if not pack.exists():
        save(out / "data/closure_cache.json", {"stats": stats, "checks": checks})
        print("Native complete; object pack not yet available", flush=True)
    else:
        inputs["spatial_source"] = pack; input_pins["spatial_source"] = sha(pack)
        spatial = read(pack)
        base_path = old_root / "delivery/data/geometry_manifest.json"
        inputs["base_manifest"] = base_path; input_pins["base_manifest"] = sha(base_path)
        base = read(base_path)
        seam_h = max(row["geological_thickness"]["value_m"] for row in base["source_mining_constraints"]["regions"])
        scene_text = (previous / "data/scene.js").read_text(encoding="utf-8")
        source_scene = json.loads(scene_text.split("=", 1)[1].strip().removesuffix(";"))
        source_levels = decoded(source_scene["levels"])
        kr2 = source_levels[2]-.8*((source_levels[2]-source_levels[1])-seam_h)
        for mesh in spatial["meshes"]:
            points = decoded(mesh["points"])
            offsets = offset_at(points[:, :2])
            if not np.isfinite(offsets).all():
                raise ValueError("local object outside joint support: "+mesh["id"])
            mesh["delta"] = encoded(offsets)
            mesh["joint_status"] = "ENGINEERING_ASSUMPTION_DISPLAY"
        for line in spatial.get("lines", []):
            for path in line.get("paths", []):
                points = np.asarray([[p[0], p[1]] for p in path["points"]], dtype=float)
                # Source plan has XY only: projected line does not establish
                # a surveyed seam elevation or an executed excavation.
                native = top.sample(points, z_values=kr2)
                offsets = offset_at(points)
                path["points"] = encoded(np.column_stack([points, native]))
                path["delta"] = encoded(offsets)
            line["z_status"] = "MODEL_CHOICE_PROJECTED_TO_REGIONAL_KRII_ROOF"
        for borehole in spatial.get("boreholes", []):
            query = np.asarray([[borehole["x"], borehole["y"]]], dtype=float)
            height = surface.sample(query)[0]
            borehole["display_z"] = float(height) if np.isfinite(height) else None
            borehole["display_anchor_status"] = "MODEL_CHOICE_SURFACE_MARKER_NOT_SURVEYED_COLLAR"
        (out / "data/spatial_objects.js").write_text("window.VKM_SPATIAL_OBJECTS="+json.dumps(spatial, ensure_ascii=False, separators=(",", ":"))+";\n", encoding="utf-8")
    document = ET.Element("VTKFile", type="vtkMultiBlockDataSet", version="1.0", byte_order="LittleEndian")
    block = ET.SubElement(document, "vtkMultiBlockDataSet")
    for i, (name, filename) in enumerate(entries):
        ET.SubElement(block, "DataSet", index=str(i), name=name, file=filename)
    ET.ElementTree(document).write(out / "unified_world.vtm", encoding="utf-8", xml_declaration=True)
    save(out / "data/joint_manifest.json", {"schema": "vkm.unified_world_interpretation/1", "source_domain": manifest["domain"],
         "height_assumption": joint["meta"], "native_checks": checks, "source_observations_changed": False,
         "mechanics_solver": "NOT_RUN", "field_validation": False,
         "source_refs": {"source_geometry": "skru1_linked_world_20261016/delivery_complete/geometry_receipt.json",
                         "height_review": "evidence/height_frame_review.json"}})
    files = [p for p in out.rglob("*") if p.is_file() and (p.parent.name in ("data", "native") or p.name == "unified_world.vtm")]
    save(out / "data_receipt.json", {"schema": "vkm.unified_data_receipt/1", "input_sha256": input_pins,
         "input_paths": {k: str(v.relative_to(repo)).replace("\\", "/") for k, v in inputs.items() if v.is_relative_to(repo)},
         "command": "pvpython scripts/build_unified_world_data.py --previous "+args.previous.as_posix()+" --output "+args.output.as_posix(),
         "native_reused_and_hash_checked": bool(args.objects_only),
         "outputs_sha256": {p.relative_to(out).as_posix(): sha(p) for p in sorted(files)},
         "status": "PASS_NATIVE_AND_OBJECTS" if pack.exists() else "PASS_NATIVE_OBJECTS_PENDING",
         "native_checks": checks, "delta_statistics": stats})
    print(json.dumps({"status": "PASS", "native_units": len(checks), "points_per_contact": n, "offset": stats}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--objects-only", action="store_true", help="reuse and hash-check already built native volumes")
    run(parser.parse_args())

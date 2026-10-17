"""Export native interpretation by default; DSM closure is an explicit display mode.

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
from vkm_world.geometry_build.height_frames import NATIVE, DISPLAY, geometry_coordinates, calculation_coordinates


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


def require_hash(path, expected):
    if not expected or sha(path) != expected:
        raise ValueError("input hash mismatch: " + str(path))


def run(args):
    repo = Path(__file__).resolve().parents[1]
    out = args.output.resolve()
    if not out.is_relative_to((repo / "work").resolve()):
        raise ValueError("real-data output must stay in ignored work/")
    (out / "data").mkdir(parents=True, exist_ok=True)
    (out / "native").mkdir(exist_ok=True)
    previous = args.previous.resolve()
    role = DISPLAY if args.geometry_mode == "display-only" else NATIVE
    geometry = read(previous / "geometry_receipt.json")
    manifest = read(previous / "data/complete_manifest.json")
    terrain_path = args.terrain.resolve()
    terrain = read(terrain_path)
    terrain_points = np.asarray(terrain["points"], dtype=np.float64)
    terrain_triangles = np.asarray(terrain["triangles"], dtype=np.int64)
    inputs = {"builder": Path(__file__), "column_display": repo / "src/vkm_world/geometry_build/column_display.py",
              "source_geometry_receipt": previous / "geometry_receipt.json", "smoothed_surface": terrain_path,
              "source_manifest": previous / "data/complete_manifest.json",
              "source_scene": previous / "data/scene.js", "observations": previous / "data/supplement.js",
              "source_terrain_vtp": previous / "data/terrain_source.vtp",
              "display_terrain_vtp": previous / "data/terrain_display.vtp",
              "height_gate": repo / "src/vkm_world/geometry_build/height_frames.py"}
    for unit in ["PDKS", "SZ", "SKZ", "PKS", "PP", "SMT", "TKT", "PCT", "Q"]:
        inputs["source_volume_"+unit] = previous / ("data/complete_"+unit+".vtu")
    input_pins = {k: sha(v) for k, v in inputs.items()}
    prior_receipt = read(out / "data_receipt.json") if args.objects_only else None
    if prior_receipt is not None:
        prior_pins = prior_receipt.get("input_sha256", {})
        if any(prior_pins.get(k) != value for k, value in input_pins.items()):
            raise ValueError("objects-only reuse requires identical geometry inputs, terrain and exporter")
    print("Read native top contact and original TIN", flush=True)
    top_path = previous / "data/complete_Q.vtu"
    require_hash(top_path, geometry["outputs_sha256"].get("data/complete_Q.vtu"))
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
    joint = {"schema": "vkm.unified_display/2", "delta": encoded(delta if role == DISPLAY else np.zeros_like(delta)),
             "display_delta": encoded(delta),
             "meta": {"height_mode": role, "geometry_role": role,
                      "status": "ENGINEERING_ASSUMPTION" if role == DISPLAY else "DERIVATION",
                      "vertical_operation": None, "vertical_tie_to_DSM": "UNKNOWN",
                      "formula": "z_native" if role == NATIVE else "z_native+H_DSM_smooth-z_top_native",
                      "source_height_frame": "mine-source elevation frame; tie to EGM2008 UNKNOWN",
                      "delta_statistics": stats,
                      "preserved": ("all source Points, native absolute roof bounds, dip and thickness"
                                    if role == NATIVE else "vertical layer intervals and order at each XY"),
                      "not_preserved": (None if role == NATIVE else
                                        "native absolute roof bounds, dip and normal thickness"),
                      "DSM_role": "observed surface model, not bedrock surface or 2026 survey",
                      "default_geometry": "unchanged source Points; source interpretation remains conditional"}}
    (out / "data/unified.js").write_text("window.VKM_UNIFIED="+json.dumps(joint, ensure_ascii=False, separators=(",", ":"))+";\n", encoding="utf-8")
    shutil.copyfile(previous / "data/scene.js", out / "data/scene.js")
    shutil.copyfile(previous / "data/supplement.js", out / "data/supplement.js")
    shutil.copyfile(previous / "data/terrain_source.vtp", out / "native/terrain_source.vtp")
    shutil.copyfile(previous / "data/terrain_display.vtp", out / "native/terrain_display.vtp")
    entries = [("Observed DSM source", "native/terrain_source.vtp"), ("Smoothed DSM display", "native/terrain_display.vtp")]
    checks = []
    for unit in ["PDKS", "SZ", "SKZ", "PKS", "PP", "SMT", "TKT", "PCT", "Q"]:
        destination = out / ("native/"+("display_" if role == DISPLAY else "source_")+unit+".vtu")
        if args.objects_only:
            if prior_receipt.get("geometry_role") != role:
                raise ValueError("cannot reuse a different geometry role")
            require_hash(destination, prior_receipt["outputs_sha256"].get(destination.relative_to(out).as_posix()))
            checks.append(next(row for row in prior_receipt["native_checks"] if row["unit"] == unit))
            entries.append((unit+" "+role, destination.relative_to(out).as_posix()))
            continue
        print("Export "+role+" "+unit, flush=True)
        source = previous / ("data/complete_"+unit+".vtu")
        require_hash(source, geometry["outputs_sha256"].get("data/complete_"+unit+".vtu"))
        grid = reader(source)
        points = vtk_to_numpy(grid.GetPoints().GetData()).copy()
        if len(points) != 2*n or not np.array_equal(points[:n, :2], xy) or not np.array_equal(points[n:, :2], xy):
            raise ValueError("source volume does not share the declared plan vertices")
        native_points = points.copy()
        original_z = points[:, 2].copy()
        offsets = np.tile(delta, 2)
        points = geometry_coordinates(points, role=role, source_frame="MINE_SOURCE_ABSOLUTE_UNSPECIFIED",
                                      display_delta=offsets)
        if role == NATIVE:
            calculation_coordinates(points, native_points, role=role,
                                    source_frame="MINE_SOURCE_ABSOLUTE_UNSPECIFIED",
                                    target_frame="MINE_SOURCE_ABSOLUTE_UNSPECIFIED")
        original_thickness = original_z[n:]-original_z[:n]
        error = float(np.max(np.abs((points[n:, 2]-points[:n, 2])-original_thickness)))
        grid.GetPoints().SetData(numpy_to_vtk(points, deep=True))
        for name, values in [("OriginalSourceZ", original_z), ("DisplayColumnTranslation", offsets if role == DISPLAY else np.zeros_like(offsets))]:
            array = numpy_to_vtk(values, deep=True); array.SetName(name); grid.GetPointData().AddArray(array)
        for name, value in [("GeometryRole", role), ("HeightFrame", "MINE_SOURCE_ABSOLUTE_UNSPECIFIED"),
                            ("VerticalTieToDSM", "UNKNOWN"), ("InterpretationStatus", "conditional source interpretation; not field validated")]:
            note = vtk.vtkStringArray(); note.SetName(name); note.InsertNextValue(value)
            grid.GetFieldData().AddArray(note)
        write_grid(grid, destination)
        entries.append((unit+" "+role, destination.relative_to(out).as_posix()))
        checks.append({"unit": unit, "vertices": len(points), "wedges": grid.GetNumberOfCells(),
                       "max_vertical_interval_change_m": error, "positive_thickness": bool(np.all(original_thickness > 0)),
                       "original_Z_retained": True, "geometry_role": role,
                       "Points_equal_source": bool(np.array_equal(points, native_points))})
    # All nested objects receive precisely the same regional field, not a
    # separate fit to their individual top contact.
    pack = args.spatial_source.resolve() if args.spatial_source else None
    if pack is None:
        save(out / "data/closure_cache.json", {"stats": stats, "checks": checks})
        print("Native complete; object pack not yet available", flush=True)
    else:
        inputs["spatial_source"] = pack; input_pins["spatial_source"] = sha(pack)
        spatial = read(pack)
        if args.base_manifest is None:
            raise ValueError("--base-manifest required with --spatial-source")
        base_path = args.base_manifest.resolve()
        inputs["base_manifest"] = base_path; input_pins["base_manifest"] = sha(base_path)
        base = read(base_path)
        for mesh in spatial["meshes"]:
            points = decoded(mesh["points"])
            offsets = offset_at(points[:, :2])
            if not np.isfinite(offsets).all():
                raise ValueError("local object outside joint support: "+mesh["id"])
            mesh["delta"] = encoded(offsets if role == DISPLAY else np.zeros_like(offsets))
            mesh["display_delta"] = encoded(offsets)
            mesh["joint_status"] = role
        # Preserve source XY plans for a separately configured seam-specific join.
        # This exporter cannot infer seam assignment or Z from a generic line.
        spatial["unplaced_plan_lines"] = spatial.pop("lines", [])
        for line in spatial["unplaced_plan_lines"]:
            line["z_status"] = "UNKNOWN_PENDING_EXPLICIT_SEAM_CONTACT_JOIN"
        spatial["lines"] = []
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
         "source_refs": {"source_geometry": inputs["source_geometry_receipt"].relative_to(repo).as_posix(),
                         "source_manifest": inputs["source_manifest"].relative_to(repo).as_posix()}})
    files = [p for p in out.rglob("*") if p.is_file() and (p.parent.name in ("data", "native") or p.name == "unified_world.vtm")]
    if input_pins != {k: sha(v) for k, v in inputs.items()}:
        raise RuntimeError("an input changed during export")
    save(out / "data_receipt.json", {"schema": "vkm.unified_data_receipt/2", "input_sha256": input_pins,
         "input_paths": {k: str(v.relative_to(repo)).replace("\\", "/") for k, v in inputs.items() if v.is_relative_to(repo)},
         "command": sys.argv, "geometry_role": role,
         "native_reused_and_hash_checked": bool(args.objects_only),
         "outputs_sha256": {p.relative_to(out).as_posix(): sha(p) for p in sorted(files)},
         "status": "PASS_GEOMETRY_EXPORT_AND_OBJECTS" if pack else "PASS_GEOMETRY_EXPORT_OBJECTS_NOT_REQUESTED",
         "native_checks": checks, "delta_statistics": stats})
    print(json.dumps({"status": "PASS", "native_units": len(checks), "points_per_contact": n, "offset": stats}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--objects-only", action="store_true", help="reuse and hash-check already built native volumes")
    parser.add_argument("--terrain", type=Path, required=True, help="explicit original/display TIN JSON for optional display closure")
    parser.add_argument("--spatial-source", type=Path, help="explicit source object pack")
    parser.add_argument("--base-manifest", type=Path, help="explicit parent source model manifest")
    parser.add_argument("--geometry-mode", choices=("native", "display-only"), default="native")
    run(parser.parse_args())

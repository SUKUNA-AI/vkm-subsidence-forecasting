"""Export the spatial package to ParaView without assigning unknown geological/mining heights.

Run under the existing GIS Python; the configured existing ``VKM_PARAVIEW_PYTHON`` writes/reads VTK and renders
with its CPU OSPRay backend. Source GeoPackages remain authoritative. Terrain uses its delivered vertical datum;
plan outlines are explicitly a display drape, and source geology is exported in separate coordinate frames.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess

import numpy as np


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for data in iter(lambda: stream.read(1 << 20), b""):
            digest.update(data)
    return digest.hexdigest()


def sample_grid(z, nodata, gt, x, y):
    """Bilinear display sample; no extrapolation and no replacement of missing heights by zero."""
    col, row = (x - gt[0]) / gt[1] - .5, (y - gt[3]) / gt[5] - .5
    i, j = math.floor(row), math.floor(col)
    if i < 0 or j < 0 or i + 1 >= z.shape[0] or j + 1 >= z.shape[1]:
        return None
    values = z[i:i+2, j:j+2]
    if not np.isfinite(values).all() or (nodata is not None and (values == nodata).any()):
        return None
    a, b = row - i, col - j
    return float(values[0, 0]*(1-a)*(1-b) + values[0, 1]*(1-a)*b + values[1, 0]*a*(1-b) + values[1, 1]*a*b)


def _parts(geometry):
    name = geometry.GetGeometryName()
    if name == "POINT":
        yield "point", [geometry.GetPoint()[:2]]
    elif name in {"LINESTRING", "LINEARRING"}:
        yield "line", [p[:2] for p in geometry.GetPoints()]
    elif name in {"POLYGON", "MULTIPOLYGON", "MULTILINESTRING", "MULTIPOINT", "GEOMETRYCOLLECTION"}:
        for i in range(geometry.GetGeometryCount()):
            yield from _parts(geometry.GetGeometryRef(i))


def _drape(points, z, nodata, gt):
    dense = []
    for start, end in zip(points, points[1:]):
        n = max(1, math.ceil(math.dist(start, end) / abs(gt[1])))
        dense.extend([(start[0]+(end[0]-start[0])*k/n, start[1]+(end[1]-start[1])*k/n) for k in range(n)])
    if points:
        dense.append(points[-1])
    xyz = []
    for x, y in dense:
        height = sample_grid(z, nodata, gt, x, y)
        if height is None:
            return None
        xyz.append([float(x), float(y), height])
    return xyz


def source_section_frame(properties):
    """Explicit metric section contract; an arbitrary spatial layer is never a vertical section.

    The geology adapter must supply these fields per feature. DOWN is a local depth axis and is drawn as -Z;
    neither direction supplies an absolute vertical datum or map trace.
    """
    if (properties.get("frame_kind") != "SOURCE_SECTION" or not properties.get("frame_id")
            or properties.get("source_x_unit") != "m" or properties.get("source_y_unit") != "m"
            or properties.get("source_y_axis") not in {"UP", "DOWN"}):
        return None
    return {"id": str(properties["frame_id"]), "y_sign": 1 if properties["source_y_axis"] == "UP" else -1}


def build_paraview_package(package_root: Path, out: Path) -> dict:
    from osgeo import gdal, ogr
    gdal.UseExceptions()
    package_root, out = package_root.resolve(), out.resolve()
    executable = os.environ.get("VKM_PARAVIEW_PYTHON")
    if not executable or not Path(executable).is_file():
        raise RuntimeError("Set VKM_PARAVIEW_PYTHON to an existing ParaView pvpython")
    if out.exists():
        raise FileExistsError("ParaView output must be a new directory")
    candidates = [package_root / "regional/dsm_utm30m.tif",
                  package_root / "terrain/data/dem_glo30_aoi_utm40n_30m.tif"]
    surface = next((path for path in candidates if path.is_file()), None)
    if surface is None:
        raise FileNotFoundError("No regional or focal DSM in the spatial package")
    ds = gdal.Open(str(surface))
    band = ds.GetRasterBand(1)
    z, nodata, gt = band.ReadAsArray(), band.GetNoDataValue(), ds.GetGeoTransform()
    srs = ds.GetSpatialRef()
    if gt[2] != 0 or gt[4] != 0 or gt[1] <= 0 or gt[5] >= 0:
        raise ValueError("Expected a north-up DSM; rotating grids require an explicit export implementation")
    valid = np.isfinite(z) & (True if nodata is None else z != nodata)
    if not valid.any():
        raise ValueError("DSM has no valid heights")
    source_wkt = srs.ExportToWkt()
    ds = None
    inputs = {surface.relative_to(package_root).as_posix(): _sha(surface)}
    arrays = {"z": z, "valid": valid, "gt": np.array(gt)}
    quality = []
    for path in sorted(surface.parent.glob("quality_*.tif")):
        code = next((c for c in ("HEM", "FLM", "EDM", "WBM") if c.lower() in path.stem.lower()), None)
        if code is None:
            continue
        ds = gdal.Open(str(path))
        if (ds.RasterYSize, ds.RasterXSize) != z.shape or ds.GetGeoTransform() != gt:
            raise ValueError("Quality layer is not on the DSM grid: " + path.name)
        arrays[code] = ds.ReadAsArray()
        quality.append({"code": code, "nodata": ds.GetRasterBand(1).GetNoDataValue(),
                        "unit": "m" if code == "HEM" else "category"})
        inputs[path.relative_to(package_root).as_posix()] = _sha(path)
        ds = None
    grid_id = "COPDEM-" + hashlib.sha256(z.tobytes()+json.dumps(gt).encode()+source_wkt.encode()).hexdigest()[:16]
    layers, omitted = [], []

    def read_layer(path, layer, drape, frame=None):
        groups = {}
        for feature in layer:
            geometry = feature.GetGeometryRef()
            if geometry is None or geometry.IsEmpty():
                continue
            definition = layer.GetLayerDefn()
            properties = {definition.GetFieldDefn(i).GetName(): feature.GetField(i) for i in range(definition.GetFieldCount())}
            identity = next((properties[key] for key in ("record_id", "geom_id", "stable_id", "pick_id") if properties.get(key)),
                            layer.GetName()+":FID:"+str(feature.GetFID()))
            section = None if drape else source_section_frame(properties)
            if not drape and section is None:
                omitted.append({"stable_id": str(identity), "source_layer": layer.GetName(),
                                "source": path.relative_to(package_root).as_posix(),
                                "status": "SOURCE_FRAME_NOT_EXPLICITLY_SUPPORTED",
                                "required": {"frame_kind": "SOURCE_SECTION", "frame_id": "nonempty",
                                             "source_x_unit": "m", "source_y_unit": "m", "source_y_axis": "UP|DOWN"}})
                continue
            target_frame = frame if drape else section["id"]
            y_sign = 1 if drape else section["y_sign"]
            features = groups.setdefault((target_frame, y_sign), [])
            for part_no, (kind, points) in enumerate(_parts(geometry)):
                stable_id = layer.GetName()+":"+str(identity)+":fid:"+str(feature.GetFID())+":part:"+str(part_no)
                xyz = _drape(points, z, nodata, gt) if drape else [[float(x), 0., y_sign*float(y)] for x, y in points]
                if xyz is None:
                    omitted.append({"stable_id": stable_id, "source_layer": layer.GetName(),
                                    "status": "DISPLAY_OMITTED_OUTSIDE_DSM_OR_NODATA", "physical_z": "UNKNOWN"})
                    continue
                features.append({"stable_id": stable_id, "source_object_id": str(identity), "source_fid": feature.GetFID(), "kind": kind,
                                 "xyz": xyz, "properties": properties})
        for (target_frame, y_sign), features in groups.items():
            if not features:
                continue
            layers.append({"name": layer.GetName(), "frame": target_frame, "features": features,
                           "source": path.relative_to(package_root).as_posix(), "source_sha256": _sha(path),
                           "geometry_role": "DISPLAY_DRAPE_PHYSICAL_Z_UNKNOWN" if drape else "SOURCE_SECTION_SEPARATE_FRAME",
                           "axis_mapping": "UTM E,N,DSM EGM2008" if drape else f"source_x -> X; {y_sign}*source_y -> Z; display Y=0 (section plane); units m",
                           "vertical_relation_to_scene": "UNKNOWN" if not drape else "DISPLAY_ONLY_NOT_UNDERGROUND_ELEVATION"})

    gpkg = package_root / "mining/mining.gpkg"
    if gpkg.is_file():
        inputs[gpkg.relative_to(package_root).as_posix()] = _sha(gpkg)
        ds = ogr.Open(str(gpkg))
        for layer in ds:
            if layer.GetName().endswith("_utm_scenario"):
                read_layer(gpkg, layer, True, "EPSG:32640")
        ds = None
    gpkg = package_root / "terrain/data/dma_linked.gpkg"
    if gpkg.is_file():
        inputs[gpkg.relative_to(package_root).as_posix()] = _sha(gpkg)
        ds = ogr.Open(str(gpkg))
        read_layer(gpkg, ds.GetLayerByName("contours"), True, "EPSG:32640")
        ds = None
    # Source sections stay separate. No WGS84 trace or zero vertical shift is inferred from a section diagram.
    for gpkg in sorted((package_root / "geology").rglob("*.gpkg")):
        inputs[gpkg.relative_to(package_root).as_posix()] = _sha(gpkg)
        ds = ogr.Open(str(gpkg))
        for layer in ds:
            if layer.GetGeomType() == ogr.wkbNone:
                continue
            read_layer(gpkg, layer, False)
        ds = None
    out.mkdir(parents=True)
    (out / "data").mkdir()
    np.savez_compressed(out / "data/terrain_arrays.npz", **arrays)
    manifest = {"schema": "vkm.paraview/1", "epistemic_status": "DERIVATION", "grid_id": grid_id,
                "surface": surface.relative_to(package_root).as_posix(), "crs_wkt": source_wkt,
                "height_unit": "m", "vertical_datum": "EGM2008 as DSM delivered; Baltic relation UNKNOWN",
                "surface_kind": "DSM_NOT_DTM", "vertical_scale": 1, "quality": quality,
                "triangulation": {"epistemic_status": "MODEL_CHOICE", "method": "two upward planar triangles per four valid raster centres; NE-SW diagonal",
                                  "authority": "source raster values remain authoritative; triangles add no resolution or accuracy",
                                  "display_drape": "bilinear raster sampling; can differ from the piecewise planar mesh between raster centres"},
                "hem_scope": "source random error sigma only; no full uncertainty or resampling error propagation",
                "layers": layers, "omitted_display_objects": omitted, "inputs_sha256": inputs}
    (out / "data/manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1, default=str)+"\n", encoding="utf-8")
    worker = Path(__file__).with_name("paraview_worker.py")
    shutil.copyfile(worker, out / "open_scene.py")
    process = subprocess.run([executable, "--no-mpi", "--force-offscreen-rendering", str(out / "open_scene.py"), "--build"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240,
                             env={**os.environ, "PYTHONUTF8": "1"}, cwd=out)
    (out / "paraview.stdout.log").write_text(process.stdout, encoding="utf-8")
    (out / "paraview.stderr.log").write_text(process.stderr, encoding="utf-8")
    verification = json.loads((out / "verification.json").read_text(encoding="utf-8")) if (out / "verification.json").is_file() else {}
    receipt = {"schema": "vkm.paraview_export_receipt/1", "inputs_sha256": inputs,
               "code_sha256": {"paraview_export.py": _sha(Path(__file__)), "paraview_worker.py": _sha(worker)},
               "command": ["existing-pvpython", "--no-mpi", "--force-offscreen-rendering", "open_scene.py", "--build"],
               "exit_code": process.returncode, "verification": verification,
               "status": "PASS" if process.returncode == 0 and verification.get("status") == "PASS" else "FAIL",
               "outputs_sha256": {p.relative_to(out).as_posix(): _sha(p) for p in sorted(out.rglob("*")) if p.is_file()}}
    (out / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=1)+"\n", encoding="utf-8")
    return receipt

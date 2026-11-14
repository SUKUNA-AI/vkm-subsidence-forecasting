#!/usr/bin/env python3
"""Small synthetic geometry/runtime checks; never opens scientific databases.

Run each mode in its own compatible interpreter. All files stay in --output.
The tolerance (1e-9 px) is fixed before execution. No VKM scientific data used.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata as md
import json
from pathlib import Path
import platform
import sqlite3
import sys
import time

TOLERANCE = 1e-9
FRAME = "SYNTHETIC_PIXEL"


def save(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def fixture():
    return {"frame": FRAME, "unit": "px", "srid": 0,
            "coordinate_tolerance": TOLERANCE, "classification": "SYNTHETIC_RUNTIME_TEST",
            "features": [
                {"id": "point-001", "geometry": {"type": "Point", "coordinates": [10, 20]}},
                {"id": "line-001", "geometry": {"type": "LineString", "coordinates": [[0, 0], [5, 5], [10, 0]]}},
                {"id": "polygon-001", "geometry": {"type": "Polygon", "coordinates": [
                    [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
                    [[2, 2], [2, 4], [4, 4], [4, 2], [2, 2]]]}}]}


def expected_geometries():
    from shapely.geometry import shape
    return {f["id"]: shape(f["geometry"]) for f in fixture()["features"]}


def assert_same(actual, expected):
    assert actual.is_valid and expected.is_valid
    assert actual.geom_type == expected.geom_type, (actual.geom_type, expected.geom_type)
    assert actual.hausdorff_distance(expected) <= TOLERANCE
    assert abs(actual.area - expected.area) <= TOLERANCE
    assert actual.equals(expected), (actual.wkt, expected.wkt)
    if actual.geom_type == "Polygon":
        assert len(actual.interiors) == len(expected.interiors) == 1


def write_dxf(path, geometries):
    import ezdxf
    doc = ezdxf.new("R2013")
    doc.header["$INSUNITS"] = 0  # Pixel units: unitless DXF plus explicit XDATA.
    doc.appids.new("VKM_SYNTHETIC")
    for name in ["POINTS", "LINES", "POLYGON_SHELL", "POLYGON_HOLE"]:
        doc.layers.new(name)
    msp = doc.modelspace()
    def tag(entity, fid, ring):
        entity.set_xdata("VKM_SYNTHETIC", [(1000, fid), (1000, FRAME), (1000, "px"), (1000, ring)])
    for fid, geom in geometries.items():
        if geom.geom_type == "Point":
            tag(msp.add_point((geom.x, geom.y), dxfattribs={"layer": "POINTS"}), fid, "point")
        elif geom.geom_type == "LineString":
            tag(msp.add_lwpolyline(geom.coords, dxfattribs={"layer": "LINES"}), fid, "line")
        elif geom.geom_type == "Polygon":
            tag(msp.add_lwpolyline(geom.exterior.coords, close=True, dxfattribs={"layer": "POLYGON_SHELL"}), fid, "shell")
            for i, hole in enumerate(geom.interiors):
                tag(msp.add_lwpolyline(hole.coords, close=True, dxfattribs={"layer": "POLYGON_HOLE"}), fid, f"hole:{i}")
        else:
            raise ValueError(geom.geom_type)
    doc.saveas(path)


def read_dxf(path):
    import ezdxf
    from shapely.geometry import LineString, Point, Polygon
    doc = ezdxf.readfile(path)
    assert doc.header["$INSUNITS"] == 0
    result, rings, records = {}, {}, []
    for entity in doc.modelspace():
        fid, frame, unit, role = [t.value for t in entity.get_xdata("VKM_SYNTHETIC")]
        assert frame == FRAME and unit == "px"
        records.append({"id": fid, "frame": frame, "unit": unit, "role": role, "layer": entity.dxf.layer})
        if role == "point":
            assert entity.dxf.layer == "POINTS"
            result[fid] = Point(entity.dxf.location.x, entity.dxf.location.y)
        else:
            coords = [(float(x), float(y)) for x, y, *_ in entity.get_points()]
            if role == "line":
                assert not entity.closed and entity.dxf.layer == "LINES"
                result[fid] = LineString(coords)
            else:
                assert entity.closed
                assert entity.dxf.layer == ("POLYGON_SHELL" if role == "shell" else "POLYGON_HOLE")
                rings.setdefault(fid, {})[role] = coords
    for fid, ring in rings.items():
        result[fid] = Polygon(ring["shell"], [v for k, v in sorted(ring.items()) if k.startswith("hole:")])
    return result, records


def windows(out):
    import cv2
    import duckdb
    import numpy as np
    from shapely import from_wkb
    geometries = expected_geometries()
    save(out / "fixture.json", fixture())
    write_dxf(out / "synthetic.dxf", geometries)
    actual, records = read_dxf(out / "synthetic.dxf")
    assert set(actual) == set(geometries)
    for fid, geom in actual.items():
        assert_same(geom, geometries[fid])
    save(out / "dxf_receipt.json", {"status": "PASS", "records": records, "hole_representation": "separate closed shell/hole LWPOLYLINE with common stable ID and XDATA ring role"})

    # Thick horizontal stroke: contour boundary must differ from centerline.
    image = np.zeros((128, 192), np.uint8)
    cv2.line(image, (20, 40), (170, 40), 255, 9)
    cv2.rectangle(image, (30, 80), (80, 110), 255, -1)
    _, mask = cv2.threshold(image, 127, 255, cv2.THRESH_BINARY)
    line_mask = mask[:65]
    contours, _ = cv2.findContours(line_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    assert len(contours) == 1
    # Pixel-column centroid is an explicit centerline method for this fixture only.
    center = []
    for x in range(25, 166):
        ys = np.flatnonzero(line_mask[:, x])
        center.append([x, float(np.mean(ys))])
    assert max(abs(y - 40) for _, y in center) == 0
    contour_y = contours[0][:, 0, 1]
    assert int(contour_y.max() - contour_y.min()) == 10
    assert np.count_nonzero(mask[80:111, 30:81]) == 31 * 51
    overlay = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    cv2.drawContours(overlay, contours, -1, (0, 0, 255), 1)
    cv2.polylines(overlay, [np.asarray(center, np.int32)], False, (0, 255, 0), 1)
    # imwrite in some Windows builds fails for absolute Cyrillic paths.
    # Encode in OpenCV, then persist bytes with Python's Unicode-safe I/O.
    for name, pixels in [("opencv_mask.png", mask), ("opencv_overlay.png", overlay)]:
        ok, encoded = cv2.imencode(".png", pixels)
        assert ok
        (out / name).write_bytes(encoded.tobytes())
        decoded = cv2.imdecode(np.frombuffer((out / name).read_bytes(), np.uint8), cv2.IMREAD_UNCHANGED)
        assert np.array_equal(decoded, pixels)
    save(out / "opencv_receipt.json", {"status": "PASS", "opencv": cv2.__version__, "method": "column centroid on bounded horizontal synthetic stroke", "centerline": center, "stroke_contour": contours[0][:, 0, :].tolist(), "centerline_y": 40, "stroke_boundary_y": [int(contour_y.min()), int(contour_y.max())], "general_map_accuracy": "NOT_EVALUATED"})

    dbpath = out / "synthetic.duckdb"
    con = duckdb.connect(str(dbpath))
    con.execute("INSTALL spatial")
    extension = con.execute("SELECT extension_name, extension_version, installed_from FROM duckdb_extensions() WHERE extension_name='spatial'").fetchall()
    con.close()
    # A genuinely new connection loads the officially signed installed extension.
    con = duckdb.connect(str(dbpath))
    con.execute("LOAD spatial")
    con.execute("CREATE TABLE IF NOT EXISTS features (id VARCHAR PRIMARY KEY, frame VARCHAR, unit VARCHAR, wkb BLOB)")
    assert con.execute("SELECT count(*) FROM features").fetchone()[0] == 0, "Use a fresh output directory"
    for fid, geom in geometries.items():
        con.execute("INSERT INTO features VALUES (?, ?, ?, ?)", [fid, FRAME, "px", geom.wkb])
    con.execute("INSERT INTO features VALUES ('null-001', ?, 'px', NULL)", [FRAME])
    rows = con.execute("SELECT id, frame, unit, ST_AsWKB(ST_GeomFromWKB(wkb)), ST_GeometryType(ST_GeomFromWKB(wkb)), ST_Area(ST_GeomFromWKB(wkb)) FROM features ORDER BY id").fetchall()
    for fid, frame, unit, wkb, kind, area in rows:
        assert frame == FRAME and unit == "px"
        if fid == "null-001":
            assert wkb is None and kind is None and area is None
        else:
            assert_same(from_wkb(wkb), geometries[fid])
    assert con.execute("SELECT ST_NumInteriorRings(ST_GeomFromWKB(wkb)) FROM features WHERE id='polygon-001'").fetchone()[0] == 1
    # Frame compatibility is a caller contract, never silently assign EPSG to pixels.
    def compatible(a, b):
        if a != b:
            raise ValueError("incompatible spatial frames")
    compatible(FRAME, FRAME)
    try:
        compatible(FRAME, "EPSG:4326")
    except ValueError:
        mismatch_rejected = True
    else:
        raise AssertionError("frame mismatch accepted")
    count = con.execute("SELECT count(*) FROM features a JOIN features b ON a.frame=b.frame AND a.unit=b.unit WHERE ST_Intersects(ST_GeomFromWKB(a.wkb), ST_GeomFromWKB(b.wkb))").fetchone()[0]
    assert count == 5
    con.close()
    save(out / "duckdb_receipt.json", {"status": "PASS", "version": duckdb.__version__, "install": "INSTALL spatial", "fresh_connection_load": True, "extension": extension, "rows": [{"id": r[0], "frame": r[1], "unit": r[2], "type": r[4], "area": r[5]} for r in rows], "null_preserved": True, "frame_mismatch_rejected": mismatch_rejected, "intersecting_pairs": count})


def ta1(out):
    from criticalmaas.ta1_geopackage import GeopackageDatabase
    from shapely.geometry import mapping, MultiPolygon, MultiLineString, shape
    geometries = expected_geometries()
    path = out / "synthetic_ta1.gpkg"
    assert not path.exists(), "Use a fresh output directory"
    db = GeopackageDatabase(path, crs="CRITICALMAAS:pixel")
    # The pinned TA1 fixtures omit the SQLite container header pragmas.
    # Adapt only our newly created synthetic package, never upstream files.
    with sqlite3.connect(path) as con:
        upstream_header = {"application_id": con.execute("PRAGMA application_id").fetchone()[0], "user_version": con.execute("PRAGMA user_version").fetchone()[0]}
        con.execute("PRAGMA application_id = 1196444487")  # 0x47504B47 / GPKG
        con.execute("PRAGMA user_version = 10200")  # GPKG 1.2.0, upstream README reference
        assert con.execute("PRAGMA application_id").fetchone()[0] == 0x47504B47
        assert con.execute("PRAGMA user_version").fetchone()[0] == 10200
    db.write_models([db.model.map(id="synthetic-map", name="Synthetic pixels", source_url="urn:synthetic", image_url="urn:synthetic", image_width=192, image_height=128), db.model.point_type(id="point-legend", name="other", description="Synthetic point legend"), db.model.line_type(id="line-legend", name="other", description="Synthetic line legend"), db.model.polygon_type(id="polygon-legend", name="other", color="#80c080", description="Synthetic polygon legend")])
    records = []
    for fid, geom in geometries.items():
        kind = fid.split("-")[0]
        encoded = MultiPolygon([geom]) if kind == "polygon" else MultiLineString([geom]) if kind == "line" else geom
        layer = kind + "_feature"
        properties = {"id": fid, "map_id": "synthetic-map", "type": kind + "-legend", "confidence": None, "provenance": "modelled"}
        if kind == "point":
            properties.update(dip=None, dip_direction=None)
        elif kind == "line":
            properties.update(name="Synthetic line", polarity=0)
        db.write_features(layer, [{"properties": properties, "geometry": mapping(encoded)}])
        db.check_constraints(layer)
        with db.open_layer(layer) as src:
            assert src.crs.to_epsg() is None
            features = list(src)
            assert len(features) == 1
            record = features[0]
            assert record["properties"]["id"] == fid
            assert record["properties"]["type"] == kind + "-legend"
            got = shape(record["geometry"])
            if got.geom_type.startswith("Multi"):
                assert len(got.geoms) == 1
                got = got.geoms[0]
            assert_same(got, geom)
            records.append({"id": fid, "frame": FRAME, "unit": "px", "srid": 0, "wkb_hex": got.wkb_hex, "type": got.geom_type, "holes": len(got.interiors) if got.geom_type == "Polygon" else 0})
    with sqlite3.connect(path) as con:
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
        srs = con.execute("SELECT table_name,srs_id FROM gpkg_geometry_columns").fetchall()
        assert all(r[1] == 0 for r in srs)
        for kind in ["point", "line", "polygon"]:
            assert con.execute(f"SELECT count(*) FROM {kind}_feature f JOIN {kind}_type t ON f.type=t.id").fetchone()[0] == 1
        con.execute("CREATE TABLE synthetic_frame_metadata (frame TEXT, unit TEXT, srid INTEGER, scope TEXT)")
        con.execute("INSERT INTO synthetic_frame_metadata VALUES (?, 'px', 0, 'SYNTHETIC_RUNTIME_TEST')", [FRAME])
    save(out / "gpkg_wkb.json", {"frame": FRAME, "unit": "px", "srid": 0, "tolerance": TOLERANCE, "features": records})
    save(out / "ta1_receipt.json", {"status": "PASS", "package": md.version("criticalmaas.ta1-geopackage"), "crs_argument": "CRITICALMAAS:pixel", "srs_ids": srs, "foreign_keys": "PASS", "legend_references": "PASS", "reader": "TA1 GeopackageDatabase.open_layer (Fiona)", "qgis_reader": "PENDING_ROOT_CHECK", "upstream_container_header": upstream_header, "synthetic_writer_adapter": {"application_id": 1196444487, "user_version": 10200, "source": "https://www.geopackage.org/spec120/#_file_format", "scope": "container header only, not a full OGC conformance certification"}, "features": records})


def roundtrip(out, gpkg_wkb, postgis):
    import duckdb
    from shapely import from_wkb
    source = json.loads(Path(gpkg_wkb).read_text(encoding="utf-8"))
    expected = expected_geometries()
    con = duckdb.connect(str(out / "e19.duckdb"))
    con.execute("LOAD spatial")
    con.execute("CREATE TABLE features(id VARCHAR PRIMARY KEY, frame VARCHAR, unit VARCHAR, wkb BLOB)")
    for item in source["features"]:
        assert item["frame"] == FRAME and item["unit"] == "px" and item["srid"] == 0
        con.execute("INSERT INTO features VALUES (?, ?, ?, ?)", [item["id"], item["frame"], item["unit"], bytes.fromhex(item["wkb_hex"])])
    returned = con.execute("SELECT id, frame, unit, ST_AsWKB(ST_GeomFromWKB(wkb)) FROM features ORDER BY id").fetchall()
    db_geoms = {}
    duckdb_features = []
    for fid, frame, unit, wkb in returned:
        assert frame == FRAME and unit == "px"
        db_geoms[fid] = from_wkb(wkb)
        assert_same(db_geoms[fid], expected[fid])
        duckdb_features.append({"id": fid, "frame": frame, "unit": unit, "srid": 0, "wkb_hex": wkb.hex()})
    save(out / "duckdb_to_postgis.json", {"features": duckdb_features})
    con.close()
    if postgis is None:
        save(out / "e19_receipt.json", {"status": "NOT_RUN_POSTGIS_READBACK", "duckdb": "PASS", "next": "Pass --postgis path to real PostGIS readback"})
        raise RuntimeError("PostGIS readback required for full E19")
    pg = json.loads(Path(postgis).read_text(encoding="utf-8"))
    assert pg["status"] == "PASS"
    assert len(pg["features"]) == len(expected)
    # The isolated SQL worker must bind its proof to this DuckDB export.
    exported_sha = hashlib.sha256((out / "duckdb_to_postgis.json").read_bytes()).hexdigest()
    assert pg["fixture_sha256"] == exported_sha, "PostGIS proof is for a different input"
    final = {}
    for item in pg["features"]:
        assert item["frame"] == FRAME and item["unit"] == "px" and item["srid"] == 0
        geom = from_wkb(bytes.fromhex(item["wkb_hex"]))
        assert_same(geom, expected[item["id"]])
        final[item["id"]] = geom
    assert set(final) == set(expected)
    write_dxf(out / "e19_from_postgis.dxf", final)
    actual, records = read_dxf(out / "e19_from_postgis.dxf")
    assert set(actual) == set(expected)
    for fid, geom in actual.items():
        assert_same(geom, expected[fid])
    save(out / "e19_receipt.json", {"status": "PASS", "chain": "source -> TA1 GPKG -> WKB -> DuckDB spatial -> isolated PostGIS -> DXF -> ezdxf readback", "tolerance_px": TOLERANCE, "features": records, "postgis_input_sha256": hashlib.sha256(Path(postgis).read_bytes()).hexdigest(), "gpkg_wkb_sha256": hashlib.sha256(Path(gpkg_wkb).read_bytes()).hexdigest()})


def krige(out):
    import numpy as np
    from pykrige.ok import OrdinaryKriging
    x = np.array([0., 10., 0., 10., 5.])
    y = np.array([0., 0., 10., 10., 5.])
    z = np.array([1., 2., 3., 4., 2.5])
    model = OrdinaryKriging(x, y, z, variogram_model="linear", variogram_parameters={"slope": 1., "nugget": 0.}, exact_values=True, verbose=False, enable_plotting=False)
    predicted, variance = model.execute("points", x, y)
    assert np.max(np.abs(predicted - z)) < 1e-8
    assert np.max(np.abs(variance)) < 1e-8
    grid, sigma = model.execute("grid", np.linspace(0, 10, 5), np.linspace(0, 10, 5))
    assert grid.shape == (5, 5) and np.isfinite(grid).all() and np.isfinite(sigma).all()
    np.savez(out / "krige_synthetic.npz", grid=grid, variance=sigma)
    save(out / "krige_receipt.json", {"status": "PASS", "pykrige": md.version("PyKrige"), "max_training_error": float(np.max(np.abs(predicted-z))), "shape": list(grid.shape), "scope": "synthetic runtime only"})


def gempy(out):
    import gempy as gp
    import numpy as np
    from gempy_engine.config import AvailableBackends
    # Synthetic horizontal interface via the installed official GemPy API.
    model = gp.create_geomodel(project_name="synthetic_runtime", extent=[0, 10, 0, 10, 0, 10], resolution=[10, 10, 10], refinement=1, structural_frame=gp.data.StructuralFrame.initialize_default_structure())
    gp.add_surface_points(geo_model=model, x=[1., 9., 1., 9.], y=[1., 1., 9., 9.], z=[5., 5., 5., 5.], elements_names="surface1")
    gp.add_orientations(geo_model=model, x=[5.], y=[5.], z=[5.], elements_names=["surface1"], pole_vector=[[0., 0., 1.]])
    gp.set_active_grid(model.grid, [gp.data.Grid.GridTypes.DENSE], reset=True)
    solution = gp.compute_model(model, engine_config=gp.data.GemPyEngineConfig(backend=AvailableBackends.numpy, use_gpu=False))
    lith = np.asarray(solution.raw_arrays.lith_block)
    assert lith.size > 0 and np.isfinite(lith).all()
    assert np.unique(lith).size >= 2
    np.save(out / "gempy_synthetic_lith.npy", lith)
    save(out / "gempy_receipt.json", {"status": "PASS", "gempy": md.version("gempy"), "backend": "numpy/CPU", "example": "synthetic horizontal interface z=5, extent 0..10", "cells": int(lith.size), "lithologies": np.unique(lith).tolist(), "scientific_scope": "synthetic runtime only, not VKM"})


def paraview(out):
    from paraview.simple import XMLUnstructuredGridReader, UpdatePipeline, CreateView, Show, ColorBy, SaveScreenshot
    from paraview import servermanager
    vtk = '''<?xml version="1.0"?>
<VTKFile type="UnstructuredGrid" version="0.1" byte_order="LittleEndian"><UnstructuredGrid><Piece NumberOfPoints="4" NumberOfCells="1"><PointData Scalars="synthetic"><DataArray type="Float32" Name="synthetic" format="ascii">0 1 2 3</DataArray></PointData><Points><DataArray type="Float32" NumberOfComponents="3" format="ascii">0 0 0 1 0 0 0 1 0 0 0 1</DataArray></Points><Cells><DataArray type="Int32" Name="connectivity" format="ascii">0 1 2 3</DataArray><DataArray type="Int32" Name="offsets" format="ascii">4</DataArray><DataArray type="UInt8" Name="types" format="ascii">10</DataArray></Cells></Piece></UnstructuredGrid></VTKFile>'''
    path = out / "synthetic.vtu"
    path.write_text(vtk, encoding="utf-8")
    reader = XMLUnstructuredGridReader(FileName=[str(path)])
    UpdatePipeline(proxy=reader)
    data = servermanager.Fetch(reader)
    assert data.GetNumberOfPoints() == 4 and data.GetNumberOfCells() == 1
    assert data.GetPointData().GetArray("synthetic").GetRange() == (0.0, 3.0)
    view = CreateView("RenderView")
    view.ViewSize = [480, 360]
    display = Show(reader, view)
    ColorBy(display, ("POINTS", "synthetic"))
    view.ResetCamera()
    SaveScreenshot(str(out / "paraview_synthetic.png"), view, ImageResolution=[480, 360])
    assert (out / "paraview_synthetic.png").stat().st_size > 1000
    save(out / "paraview_receipt.json", {"status": "PASS", "version": servermanager.vtkSMProxyManager.GetVersionMajor().__str__() + "." + str(servermanager.vtkSMProxyManager.GetVersionMinor()), "points": 4, "cells": 1, "scalar_range": [0, 3], "offscreen_screenshot": "paraview_synthetic.png"})


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=["windows", "ta1", "roundtrip", "krige", "gempy", "paraview"])
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--gpkg-wkb", type=Path)
    p.add_argument("--postgis", type=Path)
    args = p.parse_args()
    if args.output.exists() and (not args.output.is_dir() or any(args.output.iterdir())):
        p.error("Use a fresh empty output directory; existing results must be preserved")
    args.output.mkdir(parents=True, exist_ok=True)
    start = time.time()
    report = {"mode": args.mode, "python": sys.version, "executable": sys.executable, "platform": platform.platform(), "started_unix": start, "scope": "SYNTHETIC_RUNTIME_TEST", "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "fixture_sha256": hashlib.sha256(json.dumps(fixture(), sort_keys=True).encode()).hexdigest()}
    try:
        if args.mode == "roundtrip":
            roundtrip(args.output, args.gpkg_wkb, args.postgis)
        else:
            globals()[args.mode](args.output)
        report.update(status="PASS", exit_code=0)
    except Exception as exc:
        report.update(status="FAIL", error=f"{type(exc).__name__}: {exc}", exit_code=1)
        raise
    finally:
        report["elapsed_seconds"] = time.time() - start
        save(args.output / (args.mode + "_execution.json"), report)
        print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()

"""GeoPackage of the v6_2 uncertainty ensemble in local SH1 coordinates (no EPSG).

Run in an existing GDAL/QGIS Python runtime. No coordinate transformation is performed; the
custom LOCAL_CS marks the global tie as UNKNOWN. Values are synthetic ensemble statistics
(DERIVATION), never observations. Real data stay in ignored work/ or in PRIVATE.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3

import numpy as np
from osgeo import gdal, ogr, osr

ROOT = Path(__file__).resolve().parents[1]
LOCAL_WKT = ('LOCAL_CS["SH1 local engineering coordinates - global tie UNKNOWN",LOCAL_DATUM["Unknown geodetic tie",32767],'
             'UNIT["metre",1],AXIS["X East",EAST],AXIS["Y North",NORTH]]')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def layer(ds, srs, name, kind, fields, description, metadata):
    lyr = ds.CreateLayer(name, srs=srs, geom_type=kind, options=["SPATIAL_INDEX=YES", "GEOMETRY_NULLABLE=NO", "DESCRIPTION=" + description])
    for key, ftype in fields:
        lyr.CreateField(ogr.FieldDefn(key, ftype))
    lyr.SetMetadata(metadata)
    return lyr


def run(args):
    out = args.output.resolve()
    if out.exists() or not out.is_relative_to((ROOT / "work").resolve()):
        raise ValueError("use a new output directory under ignored work/")
    inputs = {"ensemble_G": args.grid / "ensemble_G.npz", "ensemble_P90": args.grid / "ensemble_P90.npz",
              "sections_G": args.grid / "sections_G.json", "cv_records": args.cv / "cv_records.json",
              "qa": args.model_dir / "FULL_CONTEXT_PARTIAL" / "qa.json", "mapped": args.model_dir / "mapped_boreholes.json",
              "builder": Path(__file__)}
    pins = {k: sha(v) for k, v in inputs.items()}
    out.mkdir(parents=True)
    g, p = np.load(inputs["ensemble_G"]), np.load(inputs["ensemble_P90"])
    contacts = [str(c) for c in g["contacts"]]
    gdal.UseExceptions(); ogr.UseExceptions(); osr.UseExceptions()
    gdal.SetConfigOption("OGR_CURRENT_DATE", args.epoch)
    srs = osr.SpatialReference(); srs.ImportFromWkt(LOCAL_WKT); srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    path = out / "geology_ensemble_v6_2_local_sh1.gpkg"
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(str(path), options=["VERSION=1.4", "METADATA_TABLES=YES"])
    common = {"HORIZONTAL_FRAME": "SH1_LOCAL_ENGINEERING", "LENGTH_UNIT": "m", "GLOBAL_GEODETIC_TIE": "UNKNOWN",
              "NO_EPSG_CLAIM": "Custom LOCAL_CS only; no EPSG identifier is assigned", "TRANSFORMATION_APPLIED": "NONE",
              "STATUS": "SYNTHETIC ENSEMBLE STATISTICS (DERIVATION); not observations; not field validation",
              "GEOMETRY_VERSION": str(g["geometry_version"]), "VERTICAL_FRAME": "DSM_EGM2008_ANCHORED_DEPTH_MODEL (ENGINEERING_ASSUMPTION)"}
    ds.SetMetadata(common)
    records = []
    # nodes
    fields = [("node", ogr.OFTInteger), ("support_mask", ogr.OFTString)]
    for c in contacts:
        fields += [(c + "_p10", ogr.OFTReal), (c + "_p50", ogr.OFTReal), (c + "_p90", ogr.OFTReal), (c + "_w80", ogr.OFTReal),
                   (c + "_w80_plan90", ogr.OFTReal), (c + "_v62", ogr.OFTReal)]
    fields += [("p_zero_PKS", ogr.OFTReal)]
    lyr = layer(ds, srs, "ensemble_nodes", ogr.wkbPoint, fields, "v6_2 grid nodes with ensemble P10/P50/P90 (G) and P10-P90 width with plan registration (P90)",
                {**common, "REALIZATIONS_G": str(int(g["realizations"])), "REALIZATIONS_P90": str(int(p["realizations"]))})
    xy = g["xy"]
    k_pks = contacts.index("PKS")
    for i in range(len(xy)):
        f = ogr.Feature(lyr.GetLayerDefn()); f.SetFID(i + 1)
        pt = ogr.Geometry(ogr.wkbPoint); pt.AddPoint_2D(float(xy[i, 0]), float(xy[i, 1])); f.SetGeometry(pt)
        f.SetField("node", i); f.SetField("support_mask", str(g["support_mask"][i]))
        for k, c in enumerate(contacts):
            q, qp = g[c + "_q"], p[c + "_q"]
            f.SetField(c + "_p10", float(q[1, i])); f.SetField(c + "_p50", float(q[2, i])); f.SetField(c + "_p90", float(q[3, i]))
            f.SetField(c + "_w80", float(q[3, i] - q[1, i])); f.SetField(c + "_w80_plan90", float(qp[3, i] - qp[1, i]))
            f.SetField(c + "_v62", float(g["v62_contacts"][i, k]))
        f.SetField("p_zero_PKS", float(g["p_zero"][k_pks, i]))
        lyr.CreateFeature(f)
    records.append({"layer": "ensemble_nodes", "features": len(xy)})
    # supports with LOO calibration counts (variant G, nested type selection)
    qa, mapped = read(inputs["qa"]), {r["physical_id"]: r for r in read(inputs["mapped"])}
    cv = [r for r in read(inputs["cv_records"])["records"] if r["fold"] == "leave_one_physical_borehole_out" and r["variant"] == "G"
          and r["config"] == "SELECTED" and r["quantity"] in ("depth", "thickness")]
    lyr = layer(ds, srs, "supports", ogr.wkbPoint, [("physical_id", ogr.OFTString), ("complete_column", ogr.OFTInteger),
                ("loo_position", ogr.OFTString), ("loo_readings", ogr.OFTInteger), ("loo_in80", ogr.OFTInteger), ("loo_in90", ogr.OFTInteger),
                ("loo_mean_crps_m", ogr.OFTReal)], "placed supports of v6_2 with leave-one-out coverage counts (variant G)", common)
    for n, pid in enumerate(qa["physical_groups"], 1):
        r = mapped[pid]
        rows = [c for c in cv if c["well"] == pid]
        f = ogr.Feature(lyr.GetLayerDefn()); f.SetFID(n)
        pt = ogr.Geometry(ogr.wkbPoint); pt.AddPoint_2D(float(r["x"]), float(r["y"])); f.SetGeometry(pt)
        f.SetField("physical_id", pid); f.SetField("complete_column", int(all(v is not None for v in r["depths_m"])))
        f.SetField("loo_position", rows[0]["position"] if rows else "NONE"); f.SetField("loo_readings", len(rows))
        f.SetField("loo_in80", sum(c["in80"] for c in rows)); f.SetField("loo_in90", sum(c["in90"] for c in rows))
        if rows:
            f.SetField("loo_mean_crps_m", float(np.mean([c["crps"] for c in rows])))
        lyr.CreateFeature(f)
    records.append({"layer": "supports", "features": len(qa["physical_groups"])})
    # section traces
    sec = read(inputs["sections_G"])["sections"]
    lyr = layer(ds, srs, "section_traces", ogr.wkbLineString, [("name", ogr.OFTString), ("length_m", ogr.OFTReal)],
                "traces of sections I-I, II-II, III-III and profile line 12 (+-50 m variants) used for the bands", common)
    for n, (name, s) in enumerate(sec.items(), 1):
        line = ogr.Geometry(ogr.wkbLineString)
        for x, y in s["xy"]:
            line.AddPoint_2D(float(x), float(y))
        f = ogr.Feature(lyr.GetLayerDefn()); f.SetFID(n); f.SetGeometry(line)
        f.SetField("name", name); f.SetField("length_m", float(s["chainage_m"][-1])); lyr.CreateFeature(f)
    records.append({"layer": "section_traces", "features": len(sec)})
    ds = None
    with sqlite3.connect(path) as db:
        db.execute("UPDATE gpkg_metadata_reference SET timestamp=?", (args.epoch,))
        srs_rows = db.execute("SELECT srs_id,organization,organization_coordsys_id,definition FROM gpkg_spatial_ref_sys WHERE srs_id IN (SELECT srs_id FROM gpkg_contents)").fetchall()
        if any(row[1].upper() == "EPSG" for row in srs_rows):
            raise ValueError("unexpected EPSG assignment")
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("GeoPackage integrity failure")
    # independent re-read
    check = ogr.Open(str(path))
    readback = {check.GetLayer(i).GetName(): check.GetLayer(i).GetFeatureCount() for i in range(check.GetLayerCount())}
    check = None
    if readback != {r["layer"]: r["features"] for r in records}:
        raise RuntimeError("read-back mismatch: %r" % readback)
    if pins != {k: sha(v) for k, v in inputs.items()}:
        raise RuntimeError("input changed during export")
    (out / "receipt.json").write_text(json.dumps({"schema": "vkm.ensemble_local_gpkg/1", "inputs_sha256": pins, "layers": records,
        "readback": readback, "srs_rows": srs_rows, "runtime": {"gdal": gdal.VersionInfo(), "sqlite": sqlite3.sqlite_version},
        "coordinate_transform": "NONE", "outputs_sha256": {path.name: sha(path)}}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({"layers": records, "readback": readback}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for item in ("grid", "cv", "model-dir", "output"):
        parser.add_argument("--" + item, type=Path, required=True)
    parser.add_argument("--epoch", required=True, help="fixed RFC3339 creation timestamp of the package")
    run(parser.parse_args())

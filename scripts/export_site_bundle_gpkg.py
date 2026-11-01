"""Export a pinned local contact TIN and site links with the existing GDAL runtime.

No coordinate operation or geological repair is performed. Historical source
heights remain metadata in their own datum, never solver anchors. This file is
an inspectable spatial representation, not an admitted FEM model.
"""
from __future__ import annotations
import argparse, csv, hashlib, json, sqlite3
from pathlib import Path
import numpy as np
from osgeo import gdal, ogr, osr

ROOT = Path(__file__).resolve().parents[1]
LOCAL_WKT = 'LOCAL_CS["SKRU1_LOCAL_SH1 global tie UNKNOWN",LOCAL_DATUM["UNKNOWN",32767],UNIT["metre",1],AXIS["X",EAST],AXIS["Y",NORTH]]'


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def run(args):
    if args.output.exists() or not args.output.resolve().is_relative_to((ROOT / "work").resolve()):
        raise ValueError("use a new output directory under ignored work/")
    field = np.load(args.field, allow_pickle=False)
    nodes, levels, triangles = field["xy"], field["contacts"], field["triangles"]
    names = field["contact_names"].tolist()
    if nodes.ndim != 2 or nodes.shape[1] != 2 or not np.isfinite(nodes).all():
        raise ValueError("finite planar XY nodes required")
    if triangles.ndim != 2 or triangles.shape[1] != 3 or not np.issubdtype(triangles.dtype, np.integer) or np.any(triangles < 0) or np.any(triangles >= len(nodes)):
        raise ValueError("valid triangle connectivity required")
    if levels.shape != (len(nodes), len(names)) or not np.isfinite(levels).all() or np.any(np.diff(levels, axis=1) > 1e-8):
        raise ValueError("finite ordered contacts matching nodes required")
    version = str(field["geometry_version"])
    sections = json.loads(args.sections.read_text(encoding="utf-8"))
    if any(s["geometry_version"] != version for s in sections.values()):
        raise ValueError("section/TIN geometry version mismatch")
    args.output.mkdir(parents=True)
    gdal.UseExceptions(); ogr.UseExceptions(); osr.UseExceptions()
    gdal.SetConfigOption("OGR_CURRENT_DATE", args.epoch)
    srs = osr.SpatialReference(); srs.ImportFromWkt(LOCAL_WKT)
    path = args.output / "block201_local_sh1.gpkg"
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(str(path), options=["VERSION=1.4", "METADATA_TABLES=YES"])
    meta = {"GEOMETRY_VERSION": version, "VARIANT": str(field["variant"]), "LENGTH_UNIT": "m", "Z_POSITIVE": "UP",
            "GLOBAL_TIE": "UNKNOWN", "HEIGHT_FRAME": str(field["height_status"]),
            "MECHANICAL_ADMISSION": "NOT_ADMITTED; contact field is not a FEM/material/history model"}
    ds.SetMetadata(meta)
    counts = {}

    def layer(name, kind, fields):
        lyr = ds.CreateLayer(name, srs=srs if kind != ogr.wkbNone else None, geom_type=kind)
        for key, dtype in fields:
            lyr.CreateField(ogr.FieldDefn(key, dtype))
        lyr.SetMetadata(meta)
        counts[name] = 0
        return lyr

    def add(lyr, values, geometry=None):
        feat = ogr.Feature(lyr.GetLayerDefn())
        for key, value in values.items():
            if value is not None:
                feat.SetField(key, value)
        if geometry is not None:
            feat.SetGeometry(geometry)
        lyr.CreateFeature(feat)
        counts[lyr.GetName()] += 1

    node_layer = layer("geology_nodes", ogr.wkbPoint25D, [("node_id", ogr.OFTInteger), ("support", ogr.OFTString),
             ("dsm_m", ogr.OFTReal)] + [(str(c) + "_m", ogr.OFTReal) for c in names])
    for i, (xy, z) in enumerate(zip(nodes, levels)):
        geom = ogr.Geometry(ogr.wkbPoint25D); geom.AddPoint(float(xy[0]), float(xy[1]), float(z[0]))
        add(node_layer, {"node_id": i, "support": str(field["support_mask"][i]), "dsm_m": float(field["dsm"][i]),
                        **{str(c) + "_m": float(value) for c, value in zip(names, z)}}, geom)
    tri_layer = layer("tin_connectivity", ogr.wkbPolygon, [("triangle_id", ogr.OFTInteger)] + [("v" + str(i), ogr.OFTInteger) for i in range(3)])
    for i, tri in enumerate(triangles):
        ring = ogr.Geometry(ogr.wkbLinearRing)
        for index in [*tri, tri[0]]:
            ring.AddPoint_2D(float(nodes[index, 0]), float(nodes[index, 1]))
        geom = ogr.Geometry(ogr.wkbPolygon); geom.AddGeometry(ring)
        add(tri_layer, {"triangle_id": i, **{"v" + str(j): int(v) for j, v in enumerate(tri)}}, geom)
    sec_layer = layer("section_contacts", ogr.wkbLineString25D, [("section_id", ogr.OFTString), ("contact", ogr.OFTString)])
    for sid, section in sections.items():
        z = np.asarray(section["levels"])
        for c, name in enumerate(names):
            geom = ogr.Geometry(ogr.wkbLineString25D)
            for xy, value in zip(section["xy"], z[:, c]):
                geom.AddPoint(float(xy[0]), float(xy[1]), float(value))
            add(sec_layer, {"section_id": sid, "contact": name}, geom)
    objects = json.loads(args.site_objects.read_text(encoding="utf-8"))
    obj_layer = layer("source_site_objects", ogr.wkbUnknown, [("object_id", ogr.OFTString), ("source_metadata", ogr.OFTString)])
    for feature in objects["features"]:
        props = feature["properties"]
        add(obj_layer, {"object_id": props["object_id"], "source_metadata": json.dumps(props, ensure_ascii=False)},
            ogr.CreateGeometryFromJson(json.dumps(feature["geometry"])))
    for name, source in (("mining_history", args.history), ("observations", args.observations)):
        lyr = layer(name, ogr.wkbNone, [("row_id", ogr.OFTInteger), ("source_record", ogr.OFTString)])
        with source.open(encoding="utf-8-sig", newline="") as stream:
            for i, row in enumerate(csv.DictReader(stream)):
                add(lyr, {"row_id": i, "source_record": json.dumps(row, ensure_ascii=False)})
    ds = None
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE gpkg_contents SET last_change=?", (args.epoch,))
        conn.execute("UPDATE gpkg_metadata_reference SET timestamp=?", (args.epoch,))
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("GeoPackage integrity check failed")
    reopened = ogr.Open(str(path), 0)
    for name, count in counts.items():
        if reopened.GetLayerByName(name).GetFeatureCount() != count:
            raise RuntimeError("round-trip feature count mismatch")
    xyz = np.array([feat.GetGeometryRef().GetPoint() for feat in reopened.GetLayerByName("geology_nodes")])
    np.testing.assert_allclose(xyz, np.column_stack([nodes, levels[:, 0]]), atol=1e-10, rtol=0)
    stored_tri = np.array([[feat.GetField("v" + str(i)) for i in range(3)] for feat in reopened.GetLayerByName("tin_connectivity")])
    np.testing.assert_array_equal(stored_tri, triangles)
    reopened = None
    receipt = {"schema": "vkm.site_gpkg_export/1", "geometry_version": version, "counts": counts,
        "round_trip": "PASS: node XYZ, connectivity and layer counts", "gdal": gdal.VersionInfo("RELEASE_NAME"),
        "semantic_metadata": meta, "inputs": {key: {"filename": value.name, "sha256": sha(value)}
             for key, value in vars(args).items() if isinstance(value, Path) and key != "output"},
        "code_sha256": sha(Path(__file__)), "outputs": {path.name: sha(path)}}
    (args.output / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("field", "sections", "site-objects", "history", "observations", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--epoch", required=True, help="generation metadata timestamp, not a physical observation epoch")
    run(parser.parse_args())

"""TA1 GeoPackage writer/readback in the already prepared data Python runtime.

Standard TA1 tables retain map/type foreign keys. Project extension tables carry
lossless A/B metadata and explicit source frame/component mappings.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.metadata
import json
from pathlib import Path
import sqlite3

from .transport import (assert_geometry_equal, assert_rows_equal, canonical_json, read_wkb_jsonl,
                        row_digest, validate_rows, write_wkb_jsonl, coordinate_frame)


def _token(value): return hashlib.sha256(canonical_json(value).encode()).hexdigest()[:24]


def _parts(feature):
    geometry = feature["geometry"]; kind = geometry["type"]; coords = geometry["coordinates"]
    fid = feature["source_feature_id"]
    if kind == "MultiPoint":
        return [{"id": f"{fid}:representation:TA1:component:{i:04d}", "path": [i], "parent_source_feature_id": fid,
                 "component_index": i, "layer": "point_feature",
                 "geometry": {"type": "Point", "coordinates": point}} for i, point in enumerate(coords)]
    family = "point" if kind == "Point" else "line" if "LineString" in kind else "polygon"
    physical = copy.deepcopy(geometry)
    if kind in {"LineString", "Polygon"}:
        physical = {"type": "Multi" + kind, "coordinates": [coords]}
    return [{"id": fid, "path": [], "layer": family + "_feature", "geometry": physical}]


def write_ta1(rows: list[dict], path: Path | str) -> dict:
    from criticalmaas.ta1_geopackage import GeopackageDatabase
    validate_rows(rows)
    if not rows: raise ValueError("Empty geometry batch")
    path = Path(path)
    if path.exists(): raise FileExistsError(path)
    # SRID0 is undefined planar, not a declaration that local metres are pixels.
    db = GeopackageDatabase(path, crs="CRITICALMAAS:0")
    maps, types, prepared = {}, {}, []
    for row in rows:
        feature = json.loads(row["feature_json"]); frame = coordinate_frame(feature)
        image_frame = feature.get("image_coordinate_frame")
        if image_frame is None:
            raise ValueError("NOT_APPLICABLE_VECTOR_ONLY: standard TA1 map requires real original image dimensions; use a separate OGC vector annex")
        # Different source images may describe one local engineering frame.
        # Geometry frame alone must never make their image provenance identical.
        map_id = "map-" + _token([row["source_id"], frame, image_frame, feature["source_image_sha256"]])
        if map_id not in maps:
            maps[map_id] = db.model.map(id=map_id, name=frame["id"], source_url="urn:vkm:source:" + row["source_id"],
                image_url="urn:sha256:" + str(feature["source_image_sha256"]), image_width=image_frame["width"], image_height=image_frame["height"])
        parts = _parts(feature)
        for part in parts:
            family = part["layer"].split("_")[0]
            type_id = "type-" + _token([family, row["source_id"], feature["legend_reference"]])
            part.update(map_id=map_id, type_id=type_id)
            if type_id not in types:
                args = dict(id=type_id, name="unknown", description="VKM source legend association; original semantics retained in vkm_transport_rows")
                if family == "polygon": args["color"] = "#808080"
                types[type_id] = getattr(db.model, family + "_type")(**args)
        prepared.append((row, parts))
    db.write_models(list(maps.values()) + list(types.values()))
    for row, parts in prepared:
        for part in parts:
            props = {"id": part["id"], "map_id": part["map_id"], "type": part["type_id"],
                     "confidence": None, "provenance": "modelled"}
            if part["layer"] == "point_feature": props.update(dip=None, dip_direction=None)
            elif part["layer"] == "line_feature": props.update(name=None, polarity=0)
            db.write_features(part["layer"], [{"properties": props, "geometry": part["geometry"]}])
    for layer in ("point_feature", "line_feature", "polygon_feature"):
        db.check_constraints(layer)
    with sqlite3.connect(path) as connection:
        # Only our newly created package: pinned TA1 fixtures omit container header pragmas.
        connection.execute("PRAGMA application_id=1196444487")
        connection.execute("PRAGMA user_version=10200")
        connection.execute("CREATE TABLE vkm_transport_rows(source_feature_id TEXT PRIMARY KEY,row_json TEXT NOT NULL,parts_json TEXT NOT NULL)")
        connection.executemany("INSERT INTO vkm_transport_rows VALUES(?,?,?)", [
            (row["source_feature_id"], canonical_json(row), canonical_json(parts)) for row, parts in prepared])
        connection.execute("CREATE TABLE vkm_representation_metadata(key TEXT PRIMARY KEY,value_json TEXT NOT NULL)")
        connection.execute("INSERT INTO vkm_representation_metadata VALUES('policy',?)", [canonical_json({
            "schema": "vkm.geometry-ta1/1", "ta1_provenance_code": "modelled",
            "provenance_translation": "TA1 coarse enum for an algorithmic adapter output only; NOT a WORLD/MODEL_RESULT and NOT human/expert verification. Exact original epistemic/review/primitive roles remain in full A JSON",
            "default_polygon_color": "DISPLAY_ONLY #808080; not a source legend colour",
            "coordinate_policy": "explicit source geometry frames and units preserved; SRID0 is undefined planar, not pixels/metres/EPSG inferred from a value unit",
            "geometry_policy": "single LineString/Polygon wrapped only in TA1 storage; MultiPoint has explicit derived component IDs",
            "header_adapter": "new output container only, GPKG1.2 header; not a full OGC conformance claim"})])
        if connection.execute("PRAGMA foreign_key_check").fetchall(): raise ValueError("TA1 foreign keys failed")
        srids = connection.execute("SELECT DISTINCT srs_id FROM gpkg_geometry_columns").fetchall()
        if srids != [(0,)]: raise ValueError("Source-coordinate package acquired a geographic SRID")
    actual = read_ta1(path)
    assert_rows_equal(rows, actual)
    return {"status": "PASS", "writer": "criticalmaas.ta1_geopackage.GeopackageDatabase", "version": importlib.metadata.version("criticalmaas-ta1-geopackage"),
            "features": len(rows), "physical_features": sum(len(parts) for _, parts in prepared), "maps": len(maps),
            "foreign_keys": "PASS", "source_frames_kept_distinct_by_map_id": True, "srid": 0, "vertex_tolerance": 1e-9}


def read_ta1(path: Path | str) -> list[dict]:
    from criticalmaas.ta1_geopackage import GeopackageDatabase
    from shapely.geometry import shape, mapping
    from shapely import to_wkb
    path = Path(path)
    db = GeopackageDatabase(path, create=False)
    with sqlite3.connect(path) as connection:
        stored = connection.execute("SELECT row_json,parts_json FROM vkm_transport_rows ORDER BY rowid").fetchall()
        if connection.execute("PRAGMA foreign_key_check").fetchall(): raise ValueError("TA1 foreign keys failed")
    physical = {}
    for layer in ("point_feature", "line_feature", "polygon_feature"):
        with db.open_layer(layer) as source:
            for item in source:
                fid = item["properties"]["id"]
                if fid in physical: raise ValueError("Duplicate TA1 representation ID")
                physical[fid] = (layer, dict(item["properties"]), mapping(shape(item["geometry"])))
    rows, used = [], set()
    for row_json, parts_json in stored:
        row = json.loads(row_json); feature = json.loads(row["feature_json"]); parts = json.loads(parts_json)
        original = feature["geometry"]; kind = original["type"]
        actual_geometry = copy.deepcopy(original)
        if kind == "MultiPoint": actual_geometry["coordinates"] = []
        for part in parts:
            layer, properties, actual = physical[part["id"]]
            if layer != part["layer"] or properties["map_id"] != part["map_id"] or properties["type"] != part["type_id"]:
                raise ValueError("TA1 ID/type/map association changed")
            assert_geometry_equal(part["geometry"], actual)
            used.add(part["id"])
            if kind == "MultiPoint": actual_geometry["coordinates"].append(actual["coordinates"])
            elif kind in {"LineString", "Polygon"}:
                if len(actual["coordinates"]) != 1: raise ValueError("Wrapped TA1 geometry gained components")
                actual_geometry["coordinates"] = actual["coordinates"][0]
            else: actual_geometry = actual
        assert_geometry_equal(original, actual_geometry)
        actual_row = dict(row, wkb_hex=to_wkb(shape(actual_geometry), byte_order=1, include_srid=False).hex())
        actual_row["row_sha256"] = row_digest(actual_row)
        rows.append(actual_row)
    if used != physical.keys(): raise ValueError("Unexpected/missing TA1 geometry IDs")
    return validate_rows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("write", "read"))
    parser.add_argument("--rows", type=Path)
    parser.add_argument("--gpkg", type=Path, required=True)
    parser.add_argument("--readback", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    if args.receipt.exists() or args.readback.exists(): parser.error("Use a fresh attempt")
    result = {"status": "FAIL", "action": args.action}
    try:
        if args.action == "write": result.update(write_ta1(read_wkb_jsonl(args.rows), args.gpkg))
        rows = read_ta1(args.gpkg)
        write_wkb_jsonl(rows, args.readback)
        result.update(status="PASS", readback_features=len(rows), gpkg_sha256=hashlib.sha256(args.gpkg.read_bytes()).hexdigest())
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    args.receipt.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__": raise SystemExit(main())

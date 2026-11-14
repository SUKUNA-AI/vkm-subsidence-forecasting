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
import re
import shutil
import sqlite3
import struct

from .transport import (assert_geometry_equal, assert_rows_equal, canonical_json, read_wkb_jsonl,
                        row_digest, validate_rows, write_wkb_jsonl, coordinate_frame)


def _token(value): return hashlib.sha256(canonical_json(value).encode()).hexdigest()[:24]


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", value):
        raise ValueError("Unexpected GeoPackage table/column identifier")
    return '"' + value + '"'


_GPB_ENVELOPE_BYTES = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}


def _header_srs(blob):
    blob = bytes(blob)
    if len(blob) < 8 or blob[:2] != b"GP" or blob[2] != 0:
        raise ValueError("Expected an actual GeoPackage geometry header")
    flags = blob[3]
    if flags & 0xC0:
        raise ValueError("Reserved GeoPackage geometry header flags must be zero")
    if flags & 0x20:
        raise ValueError("Extended GeoPackage binary types are outside this adapter")
    envelope_code = (flags >> 1) & 7
    if envelope_code not in _GPB_ENVELOPE_BYTES:
        raise ValueError("Unsupported GeoPackage geometry envelope indicator")
    wkb_offset = 8 + _GPB_ENVELOPE_BYTES[envelope_code]
    if len(blob) < wkb_offset + 5:
        raise ValueError("Truncated GeoPackage geometry envelope or WKB payload")
    if blob[wkb_offset] not in (0, 1):
        raise ValueError("Invalid WKB byte-order marker")
    # This checks framing only. Existing Fiona/OGR reads and validates the WKB;
    # its own byte order is independent of the GPB header's byte order.
    return struct.unpack("<i" if flags & 1 else ">i", blob[4:8])[0]


def _cartesian_header(blob, *, allowed_srs=(0, -1)):
    blob = bytes(blob)
    if _header_srs(blob) not in allowed_srs:
        raise ValueError("Only undefined source-coordinate GeoPackage outputs may be normalized")
    changed = blob[:4] + struct.pack("<i" if blob[3] & 1 else ">i", -1) + blob[8:]
    if blob[:4] != changed[:4] or blob[8:] != changed[8:]:
        raise ValueError("SRS-header update changed envelope/WKB bytes")
    return changed


def _set_srs_record(connection, row):
    names = ["srs_name", "srs_id", "organization", "organization_coordsys_id", "definition", "description"]
    if connection.execute("SELECT 1 FROM gpkg_spatial_ref_sys WHERE srs_id=?", [row[1]]).fetchone():
        connection.execute("UPDATE gpkg_spatial_ref_sys SET srs_name=?,organization=?,organization_coordsys_id=?,definition=?,description=? WHERE srs_id=?", [row[0], row[2], row[3], row[4], row[5], row[1]])
    else:
        values = list(row)
        if "definition_12_063" in {r[1] for r in connection.execute("PRAGMA table_info(gpkg_spatial_ref_sys)")}:
            if row[1] == 4326:
                raise ValueError("A missing WKT2 WGS84 registry entry needs the owning GDAL writer")
            names.append("definition_12_063"); values.append("undefined")
        connection.execute("INSERT INTO gpkg_spatial_ref_sys(" + ",".join(names) + ") VALUES(" + ",".join("?" for _ in names) + ")", values)


def normalize_new_cartesian_package(path: Path | str) -> dict:
    """Only a caller-owned NEW output: correct SRS metadata/header, never coordinates.

    GeoPackage Req11 reserves -1 for undefined Cartesian and 0 for undefined
    geographic. PostGIS SRID0 is a separate format-specific convention.
    """
    path = Path(path)
    changed_headers = 0
    payload_digest = hashlib.sha256()
    with sqlite3.connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        geometry_columns = connection.execute("SELECT table_name,column_name,srs_id FROM gpkg_geometry_columns ORDER BY table_name,column_name").fetchall()
        allowed_srs = {0, -1}
        # The existing GDAL/QGIS SOURCE_COORDINATES writer emits this exact
        # undefined placeholder. It is not a defined CRS or an authority claim.
        gdal_undefined = connection.execute("SELECT organization,organization_coordsys_id,definition FROM gpkg_spatial_ref_sys WHERE srs_id=99999").fetchone()
        if gdal_undefined == ('GDAL', 99999, 'LOCAL_CS["Undefined SRS",LOCAL_DATUM["unknown",32767],UNIT["unknown",0],AXIS["Easting",EAST],AXIS["Northing",NORTH]]'):
            allowed_srs.add(99999)
        if any(srs not in allowed_srs for _, _, srs in geometry_columns):
            raise ValueError("Do not relabel an existing defined CRS")
        contents = {table: srs for table, srs in connection.execute("SELECT table_name,srs_id FROM gpkg_contents WHERE data_type='features'")}
        if any(srs is not None and srs not in allowed_srs for srs in contents.values()):
            raise ValueError("Do not relabel an existing defined CRS")
        # OGC Req146: do not silently fix contradictory CRS declarations, even
        # when both numeric IDs are undefined variants. Only coherent outputs
        # of the known writer may receive the explicit Cartesian correction.
        if set(contents) != {table for table, _, _ in geometry_columns} or any(contents.get(table) != srs for table, _, srs in geometry_columns):
            raise ValueError("GeoPackage contents/geometry columns have inconsistent SRS declarations")
        triggers = connection.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name").fetchall()
        # Headers alone change. Preserve existing RTree/feature counts exactly:
        # temporarily suspend triggers in our NEW output, restore their exact SQL.
        for name, sql in triggers:
            connection.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
        for table, column, column_srs in geometry_columns:
            t, c = _identifier(table), _identifier(column)
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", [table]).fetchone():
                raise ValueError("GeoPackage geometry table missing")
            for rowid, blob in connection.execute(f"SELECT rowid,{c} FROM {t} WHERE {c} IS NOT NULL").fetchall():
                original = bytes(blob)
                if _header_srs(original) != column_srs:
                    raise ValueError("GeoPackage binary header/table SRS declarations differ")
                corrected = _cartesian_header(original, allowed_srs=allowed_srs)
                payload_digest.update(canonical_json([table, rowid]).encode() + original[:4] + original[8:])
                if corrected != original:
                    connection.execute(f"UPDATE {t} SET {c}=? WHERE rowid=?", [corrected, rowid])
                    changed_headers += 1
                actual = bytes(connection.execute(f"SELECT {c} FROM {t} WHERE rowid=?", [rowid]).fetchone()[0])
                if actual != corrected:
                    raise ValueError("GeoPackage geometry header write/readback differs")
        for _, sql in triggers:
            connection.execute(sql)
        if connection.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name").fetchall() != triggers:
            raise ValueError("New-output trigger definitions changed")
        for row in [("Undefined Cartesian SRS", -1, "NONE", -1, "undefined", "Undefined Cartesian reference system; actual source frame and units are explicit metadata"),
                    ("Undefined geographic SRS", 0, "NONE", 0, "undefined", "Undefined geographic reference system; not used by these Cartesian features")]:
            _set_srs_record(connection, row)
        if not connection.execute("SELECT 1 FROM gpkg_spatial_ref_sys WHERE srs_id=4326").fetchone():
            # Mandatory GeoPackage registry entry only, NEVER assigned to these
            # source-coordinate features. Standard WGS84 WKT1 definition.
            _set_srs_record(connection, ("WGS 84 geodetic", 4326, "EPSG", 4326,
                'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433],AUTHORITY["EPSG","4326"]]',
                "Mandatory GeoPackage registry entry; not assigned to source geometry"))
        srs_columns = {r[1] for r in connection.execute("PRAGMA table_info(gpkg_spatial_ref_sys)")}
        if "definition_12_063" in srs_columns:
            connection.execute("UPDATE gpkg_spatial_ref_sys SET definition_12_063='undefined' WHERE srs_id IN (-1,0)")
        connection.execute("UPDATE gpkg_geometry_columns SET srs_id=-1")
        connection.execute("UPDATE gpkg_contents SET srs_id=-1 WHERE data_type='features'")
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='geometry_columns'").fetchone():
            connection.execute("UPDATE geometry_columns SET srid=-1")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("GeoPackage foreign keys failed after SRS metadata update")
    standard = assert_cartesian_package(path)
    return {"status": "PASS", "changed_geometry_headers": changed_headers,
            "envelope_WKB_payload_sha256_excluding_SRS": payload_digest.hexdigest(), "standard": standard,
            "coordinate_changes": False, "trigger_definitions_preserved": True}


def assert_cartesian_package(path: Path | str) -> dict:
    with sqlite3.connect("file:" + Path(path).resolve().as_posix() + "?mode=ro", uri=True) as connection:
        srs = {r[1]: r for r in connection.execute("SELECT srs_name,srs_id,organization,organization_coordsys_id,definition,description FROM gpkg_spatial_ref_sys")}
        for identifier in (-1, 0):
            if identifier not in srs or srs[identifier][2:5] != ("NONE", identifier, "undefined"):
                raise ValueError("Nonstandard undefined GeoPackage SRS record")
        if "pixel" in canonical_json([srs[-1], srs[0]]).lower():
            raise ValueError("Unknown Cartesian/geographic SRS must not claim a pixel coordinate system")
        if "definition_12_063" in {r[1] for r in connection.execute("PRAGMA table_info(gpkg_spatial_ref_sys)")}:
            if any(r[0] != "undefined" for r in connection.execute("SELECT definition_12_063 FROM gpkg_spatial_ref_sys WHERE srs_id IN(-1,0)")):
                raise ValueError("Secondary WKT definition disagrees with undefined SRS")
        if 4326 not in srs or str(srs[4326][2]).upper() != "EPSG" or srs[4326][3] != 4326 or srs[4326][4] in ("", "undefined"):
            raise ValueError("Mandatory standard WGS84 registry record missing; it must not be assigned to source geometry")
        geometry_columns = connection.execute("SELECT table_name,column_name,srs_id FROM gpkg_geometry_columns").fetchall()
        if any(r[2] != -1 for r in geometry_columns):
            raise ValueError("Unknown Cartesian geometry must use GeoPackage SRS -1")
        if connection.execute("SELECT count(*) FROM gpkg_contents WHERE data_type='features' AND (srs_id IS NULL OR srs_id<>-1)").fetchone()[0]:
            raise ValueError("GeoPackage contents SRS disagrees with geometry columns")
        checked = 0
        for table, column, _ in geometry_columns:
            for (blob,) in connection.execute(f"SELECT {_identifier(column)} FROM {_identifier(table)} WHERE {_identifier(column)} IS NOT NULL"):
                if _header_srs(blob) != -1:
                    raise ValueError("Actual GeoPackage geometry binary header SRS is not -1")
                checked += 1
    return {"gpkg_srs_id": -1, "organization": "NONE", "definition": "undefined", "actual_geometry_headers_checked": checked,
            "raw_WKB": "no SRS field", "PostGIS_SRID": 0, "EPSG_assigned_to_geometry": False,
            "standard": "OGC GeoPackage Req11: -1 undefined Cartesian; 0 undefined geographic"}


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


def _validate_ta1_parts(feature, parts):
    """Validate the complete source-derived manifest before reading vertices.

    Map IDs remain opaque: older valid packages used a different map-ID recipe.
    Their actual map provenance and physical map/type associations are checked
    separately. No metadata entry can omit a source representation component.
    """
    if not isinstance(parts, list):
        raise ValueError("TA1 part manifest must be a complete list")
    base = []
    for part in parts:
        if not isinstance(part, dict) or any(not isinstance(part.get(k), str) or not part[k] for k in ("map_id", "type_id")):
            raise ValueError("TA1 part manifest lacks map/type identity")
        base.append({k: v for k, v in part.items() if k not in {"map_id", "type_id"}})
    if canonical_json(base) != canonical_json(_parts(feature)):
        raise ValueError("TA1 part manifest differs from complete source geometry/identity")


def write_ta1(rows: list[dict], path: Path | str) -> dict:
    from criticalmaas.ta1_geopackage import GeopackageDatabase
    validate_rows(rows)
    if not rows: raise ValueError("Empty geometry batch")
    path = Path(path)
    if path.exists(): raise FileExistsError(path)
    # Pinned TA1's bootstrap uses a pixel SRS; normalize OUR new package before
    # any geometry write. Standard GPKG -1 and PostGIS0 are distinct conventions.
    db = GeopackageDatabase(path, crs="CRITICALMAAS:0")
    normalize_new_cartesian_package(path)
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
            "coordinate_policy": "explicit source geometry frames/units preserved; GeoPackage -1 undefined Cartesian, raw WKB has no SRS, PostGIS0 is format-specific unknown. No EPSG/pixel/metre inference from value units",
            "geometry_policy": "single LineString/Polygon wrapped only in TA1 storage; MultiPoint has explicit derived component IDs",
            "header_adapter": "new output container only, GPKG1.2 header; not a full OGC conformance claim"})])
        if connection.execute("PRAGMA foreign_key_check").fetchall(): raise ValueError("TA1 foreign keys failed")
    standard = assert_cartesian_package(path)
    actual = read_ta1(path)
    assert_rows_equal(rows, actual)
    return {"status": "PASS", "writer": "criticalmaas.ta1_geopackage.GeopackageDatabase", "version": importlib.metadata.version("criticalmaas-ta1-geopackage"),
            "features": len(rows), "physical_features": sum(len(parts) for _, parts in prepared), "maps": len(maps),
            "foreign_keys": "PASS", "source_frames_kept_distinct_by_map_id": True, "srid": -1,
            "standard_SRS": standard, "vertex_tolerance": 1e-9}


def read_ta1(path: Path | str) -> list[dict]:
    assert_cartesian_package(path)
    return _read_ta1_rows(path)


def _read_ta1_rows(path: Path | str, *, allow_legacy_srs=False) -> list[dict]:
    from criticalmaas.ta1_geopackage import GeopackageDatabase
    from shapely.geometry import shape, mapping
    from shapely import to_wkb
    path = Path(path)
    db = GeopackageDatabase(path, create=False)
    with sqlite3.connect(path) as connection:
        stored = connection.execute("SELECT source_feature_id,row_json,parts_json FROM vkm_transport_rows ORDER BY rowid").fetchall()
        maps = {r[0]: r[1:] for r in connection.execute("SELECT id,name,source_url,image_url,image_width,image_height FROM map")}
        if connection.execute("PRAGMA foreign_key_check").fetchall(): raise ValueError("TA1 foreign keys failed")
    physical = {}
    for layer in ("point_feature", "line_feature", "polygon_feature"):
        with db.open_layer(layer) as source:
            if source.crs.to_epsg() is not None:
                raise ValueError("Fiona assigned an EPSG to source Cartesian geometry")
            if not allow_legacy_srs and "pixel" in (source.crs_wkt or "").lower():
                raise ValueError("Fiona still sees a false pixel SRS")
            for item in source:
                fid = item["properties"]["id"]
                if fid in physical: raise ValueError("Duplicate TA1 representation ID")
                physical[fid] = (layer, dict(item["properties"]), mapping(shape(item["geometry"])))
    rows, used = [], set()
    for stored_fid, row_json, parts_json in stored:
        row = json.loads(row_json)
        if stored_fid != row.get("source_feature_id"):
            raise ValueError("TA1 transport key differs from original source_feature_id")
        feature = json.loads(row["feature_json"]); parts = json.loads(parts_json)
        validate_rows([row])
        _validate_ta1_parts(feature, parts)
        original = feature["geometry"]; kind = original["type"]
        # Reconstruct solely from actual physical feature geometry. Source JSON
        # supplies comparison/identity, never replacement vertices for omissions.
        actual_geometry = {"type": kind, "coordinates": []} if kind == "MultiPoint" else None
        for part in parts:
            layer, properties, actual = physical[part["id"]]
            if layer != part["layer"] or properties["map_id"] != part["map_id"] or properties["type"] != part["type_id"]:
                raise ValueError("TA1 ID/type/map association changed")
            image_frame = feature.get("image_coordinate_frame")
            expected_map = (coordinate_frame(feature)["id"], "urn:vkm:source:" + row["source_id"],
                            "urn:sha256:" + str(feature["source_image_sha256"]), image_frame["width"], image_frame["height"])
            family = layer.split("_")[0]
            expected_type = "type-" + _token([family, row["source_id"], feature["legend_reference"]])
            if maps.get(part["map_id"]) != expected_map or part["type_id"] != expected_type:
                raise ValueError("TA1 map provenance/type no longer agrees with original source")
            assert_geometry_equal(part["geometry"], actual)
            used.add(part["id"])
            if kind == "MultiPoint": actual_geometry["coordinates"].append(actual["coordinates"])
            elif kind in {"LineString", "Polygon"}:
                if len(actual["coordinates"]) != 1: raise ValueError("Wrapped TA1 geometry gained components")
                actual_geometry = {"type": kind, "coordinates": actual["coordinates"][0]}
            else: actual_geometry = actual
        assert_geometry_equal(original, actual_geometry)
        actual_row = dict(row, wkb_hex=to_wkb(shape(actual_geometry), byte_order=1, include_srid=False).hex())
        actual_row["row_sha256"] = row_digest(actual_row)
        rows.append(actual_row)
    if used != physical.keys(): raise ValueError("Unexpected/missing TA1 geometry IDs")
    return validate_rows(rows)


def repair_srs_copy(source: Path, target: Path) -> dict:
    """Metadata/header correction in a NEW immutable derivative; old file untouched."""
    if target.exists(): raise FileExistsError(target)
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    before_rows = _read_ta1_rows(source, allow_legacy_srs=True)
    with sqlite3.connect("file:" + source.resolve().as_posix() + "?mode=ro", uri=True) as connection:
        before_transport = connection.execute("SELECT source_feature_id,row_json,parts_json FROM vkm_transport_rows ORDER BY source_feature_id").fetchall()
        before_srs = connection.execute("SELECT * FROM gpkg_spatial_ref_sys ORDER BY srs_id").fetchall()
        before_maps = connection.execute("SELECT * FROM map ORDER BY id").fetchall()
    with source.open("rb") as src, target.open("xb") as dst:
        shutil.copyfileobj(src, dst)
    normalization = normalize_new_cartesian_package(target)
    with sqlite3.connect(target) as connection:
        policy_json = connection.execute("SELECT value_json FROM vkm_representation_metadata WHERE key='policy'").fetchone()[0]
        policy = json.loads(policy_json)
        policy["coordinate_policy"] = "GeoPackage -1 undefined Cartesian; raw WKB has no SRS; PostGIS0 is format-specific unknown. Actual source frames/units retained; no EPSG inference"
        policy["standard_SRS_correction"] = {"source_gpkg_sha256": source_sha, "new_gpkg_srs_id": -1,
            "geometry_coordinates_changed": False, "old_package_immutable": True}
        connection.execute("UPDATE vkm_representation_metadata SET value_json=? WHERE key='policy'", [canonical_json(policy)])
        if before_transport != connection.execute("SELECT source_feature_id,row_json,parts_json FROM vkm_transport_rows ORDER BY source_feature_id").fetchall():
            raise ValueError("SRS correction changed source A/B rows or TA1 part identity")
        if before_maps != connection.execute("SELECT * FROM map ORDER BY id").fetchall():
            raise ValueError("SRS correction changed original image map metadata")
    after_rows = read_ta1(target)
    assert_rows_equal(before_rows, after_rows)
    if hashlib.sha256(source.read_bytes()).hexdigest() != source_sha:
        raise ValueError("Original package changed during derived-copy correction")
    return {"status": "PASS", "scope": "new derived copy only; standard metadata and geometry SRS header correction",
            "source_gpkg_sha256": source_sha, "corrected_gpkg_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "old_standard_SRS": before_srs, "normalization": normalization, "features": len(after_rows),
            "A_B_rows_exact": True, "image_map_metadata_exact": True, "vertices_types_IDs_exact_1e9": True,
            "original_unchanged": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("write", "read", "repair-srs"))
    parser.add_argument("--rows", type=Path)
    parser.add_argument("--gpkg", type=Path, required=True)
    parser.add_argument("--source-gpkg", type=Path)
    parser.add_argument("--readback", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    if args.receipt.exists() or args.readback.exists(): parser.error("Use a fresh attempt")
    result = {"status": "FAIL", "action": args.action}
    try:
        if args.action == "write": result.update(write_ta1(read_wkb_jsonl(args.rows), args.gpkg))
        elif args.action == "repair-srs": result.update(repair_srs_copy(args.source_gpkg, args.gpkg))
        rows = read_ta1(args.gpkg)
        write_wkb_jsonl(rows, args.readback)
        result.update(status="PASS", readback_features=len(rows), gpkg_sha256=hashlib.sha256(args.gpkg.read_bytes()).hexdigest())
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    args.receipt.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__": raise SystemExit(main())

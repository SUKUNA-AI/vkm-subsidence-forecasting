import sqlite3
import struct

import pytest
from shapely.geometry import Point

from vkm_geometry.ta1_worker import (_cartesian_header, _header_srs,
                                     _parts, _validate_ta1_parts,
                                     assert_cartesian_package, normalize_new_cartesian_package)


@pytest.mark.parametrize("little", [False, True])
def test_binary_header_only_srs_changes(little):
    blob = b"GP\x00" + bytes([int(little)]) + struct.pack("<i" if little else ">i", 0) + Point(3.25, 4.5).wkb
    actual = _cartesian_header(blob)
    assert _header_srs(actual) == -1
    assert blob[:4] == actual[:4] and blob[8:] == actual[8:]


@pytest.mark.parametrize("legacy_srs", [0, 99999])
def test_false_pixel_standard_metadata_rejected_and_corrected_in_new_output(tmp_path, legacy_srs):
    path = tmp_path / "new_synthetic.gpkg"
    blob = b"GP\x00\x01" + struct.pack("<i", legacy_srs) + Point(3.25, 4.5).wkb
    with sqlite3.connect(path) as db:
        db.executescript('''
        CREATE TABLE gpkg_spatial_ref_sys(srs_name TEXT,srs_id INTEGER PRIMARY KEY,organization TEXT,organization_coordsys_id INTEGER,definition TEXT,description TEXT);
        CREATE TABLE gpkg_geometry_columns(table_name TEXT,column_name TEXT,geometry_type_name TEXT,srs_id INTEGER,z INTEGER,m INTEGER);
        CREATE TABLE gpkg_contents(table_name TEXT,data_type TEXT,srs_id INTEGER);
        CREATE TABLE features(id TEXT PRIMARY KEY,geom BLOB,original_attribute TEXT);
        CREATE TRIGGER preserve_trigger AFTER UPDATE OF geom ON features BEGIN SELECT RAISE(ABORT,'geometry trigger preserved'); END;
        ''')
        db.executemany("INSERT INTO gpkg_spatial_ref_sys VALUES(?,?,?,?,?,?)", [
            ("Undefined Cartesian SRS", -1, "NONE", -1, "undefined", "undefined"),
            ("Pixel coordinates", 0, "CRITICALMAAS", 0, 'PIXELCS["Pixel coordinates",ENGRUNITS["m",1]]', "Pixel coordinates"),
            ("WGS84", 4326, "EPSG", 4326, 'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS84",6378137,298.257223563]],PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]', "mandatory standard registry entry, not assigned to features")])
        if legacy_srs == 99999:
            db.execute("INSERT INTO gpkg_spatial_ref_sys VALUES(?,?,?,?,?,?)", ('Undefined SRS', 99999, 'GDAL', 99999, 'LOCAL_CS["Undefined SRS",LOCAL_DATUM["unknown",32767],UNIT["unknown",0],AXIS["Easting",EAST],AXIS["Northing",NORTH]]', 'Custom undefined coordinate reference system'))
        db.execute("INSERT INTO gpkg_geometry_columns VALUES('features','geom','POINT',?,0,0)", [legacy_srs])
        db.execute("INSERT INTO gpkg_contents VALUES('features','features',?)", [legacy_srs])
        db.execute("INSERT INTO features VALUES('synthetic',?,'UTF-8 source literal')", [blob])
        trigger = db.execute("SELECT sql FROM sqlite_master WHERE type='trigger'").fetchone()[0]
    with pytest.raises(ValueError, match="Nonstandard undefined"):
        assert_cartesian_package(path)
    proof = normalize_new_cartesian_package(path)
    assert proof["changed_geometry_headers"] == 1
    assert proof["coordinate_changes"] is False
    assert assert_cartesian_package(path)["gpkg_srs_id"] == -1
    with sqlite3.connect(path) as db:
        got, attribute = db.execute("SELECT geom,original_attribute FROM features").fetchone()
        assert got[:4] == blob[:4] and got[8:] == blob[8:]
        assert attribute == "UTF-8 source literal"
        assert db.execute("SELECT sql FROM sqlite_master WHERE type='trigger'").fetchone()[0] == trigger
        with pytest.raises(sqlite3.IntegrityError, match="trigger preserved"):
            db.execute("UPDATE features SET geom=?", [blob])

    # A real defined CRS must never be silently relabeled by this adapter.
    with sqlite3.connect(path) as db:
        db.execute("UPDATE gpkg_geometry_columns SET srs_id=4326")
    with pytest.raises(ValueError, match="defined CRS"):
        normalize_new_cartesian_package(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT srs_id FROM gpkg_geometry_columns").fetchone()[0] == 4326


@pytest.mark.parametrize("corruption", ["missing", "parent", "child", "typed_index"])
def test_ta1_source_manifest_cannot_hide_missing_or_misidentified_components(corruption):
    from test_transport import synthetic_batch
    feature = synthetic_batch()["features"][1]  # actual MultiPoint components
    parts = _parts(feature)
    for part in parts:
        part.update(map_id="historical-opaque-map", type_id="type-identity")
    _validate_ta1_parts(feature, parts)
    if corruption == "missing": parts.pop()
    elif corruption == "parent": parts[0]["parent_source_feature_id"] = "WRONG:PARENT"
    elif corruption == "child": parts[0]["id"] = "WRONG:CHILD"
    else: parts[0]["component_index"] = False
    with pytest.raises(ValueError, match="part manifest"):
        _validate_ta1_parts(feature, parts)


@pytest.mark.ta1_runtime
@pytest.mark.parametrize("entrypoint", ["read", "repair_copy"])
def test_actual_ta1_sql_key_must_match_embedded_source_id(tmp_path, entrypoint):
    from criticalmaas import ta1_geopackage  # noqa: F401: explicit native prerequisite
    import hashlib
    from test_transport import synthetic_batch
    from vkm_geometry.transport import batch_rows, assert_rows_equal
    from vkm_geometry.ta1_worker import write_ta1, read_ta1, repair_srs_copy
    batch = synthetic_batch(); batch["features"] = batch["features"][:1]; batch["project_matches"] = batch["project_matches"][:1]
    rows = batch_rows(batch); package = tmp_path / "synthetic.gpkg"
    write_ta1(rows, package); assert_rows_equal(rows, read_ta1(package))
    with sqlite3.connect(package) as db:
        original = db.execute("SELECT row_json,parts_json FROM vkm_transport_rows").fetchall()
        geometry = db.execute("SELECT geometry FROM point_feature").fetchall()
        db.execute("UPDATE vkm_transport_rows SET source_feature_id='renamed-key'")
        assert original == db.execute("SELECT row_json,parts_json FROM vkm_transport_rows").fetchall()
        assert geometry == db.execute("SELECT geometry FROM point_feature").fetchall()
    before = hashlib.sha256(package.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="transport key"):
        if entrypoint == "read": read_ta1(package)
        else: repair_srs_copy(package, tmp_path / "repaired.gpkg")
    assert hashlib.sha256(package.read_bytes()).hexdigest() == before
    assert not (tmp_path / "repaired.gpkg").exists()

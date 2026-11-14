"""Bounded GPB tests from the reviewed Hydrogen output, plus native CRS/hole checks.

These check framing/preservation; Fiona/OGR remains the actual WKB reader.
"""
import sqlite3
import struct
import pytest
from vkm_geometry.ta1_worker import (
    _cartesian_header,
    _header_srs,
    assert_cartesian_package,
    normalize_new_cartesian_package,
)


def _point_wkb(little=True, x=3.25, y=4.5):
    order = "<" if little else ">"
    return bytes([int(little)]) + struct.pack(order + "Idd", 1, x, y)


def _gpb(flags, srs=0, payload=b"", envelope=b""):
    order = "<" if flags & 1 else ">"
    return b"GP\x00" + bytes([flags]) + struct.pack(order + "i", srs) + envelope + payload


def _create_gpkg(path, rows):
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE gpkg_spatial_ref_sys(srs_name TEXT,srs_id INTEGER PRIMARY KEY,organization TEXT,organization_coordsys_id INTEGER,definition TEXT,description TEXT);
            CREATE TABLE gpkg_geometry_columns(table_name TEXT,column_name TEXT,geometry_type_name TEXT,srs_id INTEGER,z INTEGER,m INTEGER);
            CREATE TABLE gpkg_contents(table_name TEXT,data_type TEXT,srs_id INTEGER);
            CREATE TABLE features(id TEXT PRIMARY KEY,geom BLOB,original_attribute TEXT);
            CREATE TRIGGER preserve_trigger AFTER UPDATE OF geom ON features BEGIN SELECT RAISE(ABORT,'geometry trigger preserved'); END;
            """
        )
        db.executemany(
            "INSERT INTO gpkg_spatial_ref_sys VALUES(?,?,?,?,?,?)",
            [
                ("Undefined Cartesian SRS", -1, "NONE", -1, "undefined", "undefined"),
                ("Pixel coordinates", 0, "CRITICALMAAS", 0, 'PIXELCS["Pixel coordinates",ENGRUNITS["m",1]]', "Pixel coordinates"),
                ("WGS 84", 4326, "EPSG", 4326, 'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]', "registry only"),
            ],
        )
        db.execute("INSERT INTO gpkg_geometry_columns VALUES('features','geom','POINT',0,0,0)")
        db.execute("INSERT INTO gpkg_contents VALUES('features','features',0)")
        db.executemany("INSERT INTO features VALUES(?,?,?)", rows)
        return db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='preserve_trigger'").fetchone()[0]


def test_valid_gpb_variants_preserve_envelope_endian_and_empty_nan():
    variants = [
        _gpb(1, srs=0, payload=_point_wkb(False)),
        _gpb(0, srs=99999, payload=_point_wkb(True)),
        _gpb(3, srs=0, envelope=struct.pack("<dddd", 3.25, 3.25, 4.5, 4.5), payload=_point_wkb(True)),
        _gpb(17, srs=0, payload=_point_wkb(True, float("nan"), float("nan"))),
    ]
    for blob in variants:
        actual = _cartesian_header(blob, allowed_srs=(0, -1, 99999))
        assert _header_srs(actual) == -1
        assert actual[:4] == blob[:4]
        assert actual[8:] == blob[8:]
    assert _cartesian_header(variants[0])[8] == 0
    assert _cartesian_header(variants[1], allowed_srs=(0, -1, 99999))[8] == 1


@pytest.mark.parametrize(
    "bad_blob",
    [
        _gpb(1),
        _gpb(3, payload=_point_wkb(True)),
        _gpb(129, payload=_point_wkb(True)),
        _gpb(11, payload=_point_wkb(True)),
        b"GP\x01\x01" + struct.pack("<i", 0) + _point_wkb(True),
        _gpb(1, srs=4326, payload=_point_wkb(True)),
    ],
)
def test_invalid_gpb_headers_and_defined_srs_rejected(bad_blob):
    with pytest.raises(ValueError):
        _cartesian_header(bad_blob)


@pytest.mark.parametrize(
    "bad_row_blob",
    [
        _gpb(1, srs=4326, payload=_point_wkb(True)),
        _gpb(3, payload=_point_wkb(True)),
    ],
)
def test_late_row_failure_rolls_back_earlier_writes_and_restores_triggers(tmp_path, bad_row_blob):
    path = tmp_path / "rollback.gpkg"
    first_blob = _gpb(3, srs=0, envelope=struct.pack("<dddd", 3.25, 3.25, 4.5, 4.5), payload=_point_wkb(True))
    trigger_sql = _create_gpkg(
        path,
        [
            ("row1", first_blob, "keep-1"),
            ("row2", bad_row_blob, "keep-2"),
        ],
    )
    with pytest.raises(ValueError):
        normalize_new_cartesian_package(path)
    with sqlite3.connect(path) as db:
        row1_geom = db.execute("SELECT geom FROM features WHERE id='row1'").fetchone()[0]
        assert bytes(row1_geom) == first_blob
        assert db.execute("SELECT srs_id FROM gpkg_geometry_columns").fetchone()[0] == 0
        assert db.execute("SELECT srs_id FROM gpkg_contents").fetchone()[0] == 0
        assert db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='preserve_trigger'").fetchone()[0] == trigger_sql
        with pytest.raises(sqlite3.IntegrityError, match="trigger preserved"):
            db.execute("UPDATE features SET geom=? WHERE id='row1'", [first_blob])


def test_mixed_valid_variants_normalize_and_repeat_idempotently(tmp_path):
    path = tmp_path / "idempotent.gpkg"
    rows = [
        ("le_be", _gpb(1, srs=0, payload=_point_wkb(False)), "a"),
        ("be_le", _gpb(0, srs=0, payload=_point_wkb(True)), "b"),
        ("xy_env", _gpb(3, srs=0, envelope=struct.pack("<dddd", 3.25, 3.25, 4.5, 4.5), payload=_point_wkb(True)), "c"),
        ("empty_nan", _gpb(17, srs=0, payload=_point_wkb(True, float("nan"), float("nan"))), "d"),
    ]
    trigger_sql = _create_gpkg(path, rows)
    first_proof = normalize_new_cartesian_package(path)
    assert first_proof["changed_geometry_headers"] == 4
    assert first_proof["coordinate_changes"] is False
    assert first_proof["trigger_definitions_preserved"] is True
    second_proof = normalize_new_cartesian_package(path)
    assert second_proof["changed_geometry_headers"] == 0
    assert second_proof["envelope_WKB_payload_sha256_excluding_SRS"] == first_proof["envelope_WKB_payload_sha256_excluding_SRS"]
    assert assert_cartesian_package(path)["actual_geometry_headers_checked"] == 4
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='preserve_trigger'").fetchone()[0] == trigger_sql
        for row_id, orig_blob, _ in rows:
            got = bytes(db.execute("SELECT geom FROM features WHERE id=?", [row_id]).fetchone()[0])
            assert _header_srs(got) == -1
            assert got[:4] == orig_blob[:4] and got[8:] == orig_blob[8:]


import sqlite3
import pytest
from vkm_geometry.ta1_worker import normalize_new_cartesian_package

def test_defined_contents_crs_is_not_silently_relabelled(tmp_path):
    path=tmp_path/'contents_defined.gpkg'
    blob=_gpb(1,payload=_point_wkb(True))
    trigger=_create_gpkg(path,[('one',blob,'unchanged')])
    with sqlite3.connect(path) as db:
        db.execute("UPDATE gpkg_contents SET srs_id=4326 WHERE table_name='features'")
    with pytest.raises(ValueError,match='defined CRS'):
        normalize_new_cartesian_package(path)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT srs_id FROM gpkg_contents').fetchone()[0]==4326
        assert db.execute('SELECT srs_id FROM gpkg_geometry_columns').fetchone()[0]==0
        assert bytes(db.execute('SELECT geom FROM features').fetchone()[0])==blob
        assert db.execute("SELECT sql FROM sqlite_master WHERE type='trigger'").fetchone()[0]==trigger


import struct
from shapely import from_wkb
from vkm_geometry.ta1_worker import _cartesian_header

def test_polygon_hole_vertices_and_wkb_endianness_preserved():
    # Native independent domain check: polygon with an inner ring, BE GPB header,
    # LE WKB, XY envelope. No model-generated expected coordinates.
    outer=[(0.,0.),(10.,0.),(10.,10.),(0.,10.),(0.,0.)]
    hole=[(2.,2.),(2.,4.),(4.,4.),(4.,2.),(2.,2.)]
    wkb=b'\x01'+struct.pack('<II',3,2)
    for ring in (outer,hole):
        wkb+=struct.pack('<I',len(ring))+b''.join(struct.pack('<dd',*p) for p in ring)
    envelope=struct.pack('>dddd',0.,10.,0.,10.)
    original=b'GP\x00\x02'+struct.pack('>i',0)+envelope+wkb
    corrected=_cartesian_header(original)
    assert corrected[:4]==original[:4]
    assert corrected[8:]==original[8:]
    assert struct.unpack('>i',corrected[4:8])[0]==-1
    shape=from_wkb(corrected[40:])
    assert list(shape.exterior.coords)==outer
    assert len(shape.interiors)==1 and list(shape.interiors[0].coords)==hole


@pytest.mark.parametrize("flags", [0x41, 0x21, 0x0D, 0x0F])
def test_other_reserved_extended_and_invalid_envelope_codes_rejected(flags):
    with pytest.raises(ValueError):
        _header_srs(_gpb(flags, payload=_point_wkb()))


@pytest.mark.parametrize("payload", [b"\x01\x01\x00\x00", b"\x02\x01\x00\x00\x00", b"\xff\x01\x00\x00\x00"])
def test_truncated_wkb_prefix_or_invalid_byte_order_rejected(payload):
    with pytest.raises(ValueError):
        _header_srs(_gpb(1, payload=payload))


@pytest.mark.parametrize("contradiction", ["undefined_contents", "null_contents", "different_binary_header"])
def test_contradictory_srs_declarations_rejected_without_mutation(tmp_path, contradiction):
    path = tmp_path / "contradictory.gpkg"
    blob = _gpb(1, srs=-1 if contradiction == "different_binary_header" else 0, payload=_point_wkb())
    _create_gpkg(path, [("one", blob, "unchanged")])
    with sqlite3.connect(path) as db:
        if contradiction == "undefined_contents": db.execute("UPDATE gpkg_contents SET srs_id=-1")
        elif contradiction == "null_contents": db.execute("UPDATE gpkg_contents SET srs_id=NULL")
    with sqlite3.connect(path) as db:
        before = list(db.iterdump())
    with pytest.raises(ValueError, match="SRS"):
        normalize_new_cartesian_package(path)
    with sqlite3.connect(path) as db:
        assert list(db.iterdump()) == before


def test_nongeometry_contents_do_not_require_geometry_column(tmp_path):
    path = tmp_path / "attributes.gpkg"
    _create_gpkg(path, [("one", _gpb(1, payload=_point_wkb()), "unchanged")])
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE annotations(id TEXT,label TEXT)")
        db.execute("INSERT INTO gpkg_contents VALUES('annotations','attributes',NULL)")
        db.execute("INSERT INTO annotations VALUES('one','source text')")
    normalize_new_cartesian_package(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT srs_id FROM gpkg_contents WHERE table_name='annotations'").fetchone() == (None,)
        assert db.execute("SELECT * FROM annotations").fetchall() == [("one", "source text")]

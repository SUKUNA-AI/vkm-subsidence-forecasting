import copy
import json
from pathlib import Path

import pytest

from vkm_geometry.transport import (assert_rows_equal, batch_rows, cad_affine, load_batch,
                                    read_dxf, read_wkb_jsonl, validate_batch, write_dxf, write_wkb_jsonl)


def synthetic_batch():
    geometries = [
        {"type": "Point", "coordinates": [1.125, 2.25]},
        {"type": "MultiPoint", "coordinates": [[1, 2], [3, 4]]},
        {"type": "LineString", "coordinates": [[1, 2], [5, 6], [9, 2]]},
        {"type": "MultiLineString", "coordinates": [[[0, 0], [1, 1]], [[3, 2], [4, 8], [9, 10]]]},
        {"type": "Polygon", "coordinates": [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]], [[2, 2], [2, 4], [4, 4], [4, 2], [2, 2]]]},
        {"type": "MultiPolygon", "coordinates": [[[[0, 0], [4, 0], [4, 4], [0, 0]]], [[[10, 10], [12, 10], [12, 14], [10, 10]]]]},
    ]
    features = []
    for i, geometry in enumerate(geometries):
        features.append({"source_feature_id": f"SYNTHETIC:{i}", "source_id": "VKM-SRC-999", "work_id": "VKM-WRK-999",
                         "work_id_basis": "SYNTHETIC", "pdf_page": 1, "printed_page": None, "figure_id": "synthetic",
                         "panel_id": "a", "geometry": geometry,
                         "image_coordinate_frame": {"id": "SYNTHETIC:PX_TL", "origin": "top-left image edge", "x_axis": "right",
                            "y_axis": "down", "unit": "pixel", "width": 100, "height": 100, "pixel_to_pdf_page_pt": None,
                            "crs": None, "registration_status": "NOT_GEOREFERENCED"},
                         "source_label": "Синтетика UTF-8", "geological_meaning": "UNKNOWN", "value": 12,
                         "unit": "m", "legend_reference": {"literal": "Неизвестно", "id": None}, "horizon": None,
                         "source_image": "synthetic.png", "source_image_sha256": "0" * 64,
                         "extraction_status": "SELECTED_OBJECT_ONLY", "epistemic_role": "DERIVATION",
                         "review_status": "AGENT_REVIEW_NOT_HUMAN_ADMITTED", "primitive_role": "OBSERVED_STROKE",
                         "provenance": {"input_sha256": "1" * 64, "extraction_version": "test1", "transformation_history": []},
                         "extra_nested": {"unknown": None, "unicode": "Я" * 20000, "list": [1, "1", False]}})
    return {"batch_id": "SYNTHETIC-TRANSPORT", "extraction_version": "test1", "features": features,
            "project_matches": [{"source_feature_id": f["source_feature_id"], "candidate_project_object_ids": [],
                                  "match_status": "UNKNOWN", "match_evidence": [], "conflicts": [], "limitations": ["synthetic"]} for f in features],
            "provenance": {"synthetic": True}}


def test_six_geometry_types_full_json_wkb_and_dxf(tmp_path):
    batch = synthetic_batch()
    original = copy.deepcopy(batch)
    rows = batch_rows(batch)
    assert batch == original
    assert {r["coordinate_unit"] for r in rows} == {"pixel"}
    write_wkb_jsonl(rows, tmp_path / "rows.jsonl")
    assert_rows_equal(rows, read_wkb_jsonl(tmp_path / "rows.jsonl"))
    info = write_dxf(rows, tmp_path / "source.dxf")
    assert info["entities"] > len(rows)
    assert_rows_equal(rows, read_dxf(tmp_path / "source.dxf"))
    assert cad_affine(batch["features"][0]["image_coordinate_frame"])["forward"] == [[1, 0, 0], [0, -1, 0], [0, 0, 1]]


def test_deleted_native_entity_is_not_hidden_by_metadata(tmp_path):
    import ezdxf
    rows = batch_rows(synthetic_batch())
    path = tmp_path / "damaged.dxf"
    write_dxf(rows, path)
    doc = ezdxf.readfile(path)
    doc.modelspace().delete_entity(list(doc.modelspace())[2])
    doc.saveas(path)
    with pytest.raises(ValueError, match="part count"):
        read_dxf(path)


def test_changed_source_attrs_and_unknown_frame_rejected():
    batch = synthetic_batch()
    batch["features"][0]["image_coordinate_frame"]["crs"] = "EPSG:32640"
    with pytest.raises(ValueError, match="pixels"):
        validate_batch(batch)
    batch = synthetic_batch()
    batch["features"][0]["source_feature_id"] = batch["features"][1]["source_feature_id"]
    with pytest.raises(ValueError, match="unique"):
        validate_batch(batch)


def test_unclosed_ring_is_not_silently_repaired():
    batch = synthetic_batch()
    batch["features"][4]["geometry"]["coordinates"][0].pop()
    with pytest.raises(ValueError, match="closed"):
        batch_rows(batch)


def test_no_overwrite_and_exact_batch_read(tmp_path):
    path = tmp_path / "batch.json"
    batch = synthetic_batch()
    path.write_text(json.dumps(batch, ensure_ascii=False), encoding="utf-8")
    assert load_batch(path) == batch
    rows = batch_rows(batch)
    write_dxf(rows, tmp_path / "one.dxf")
    with pytest.raises(FileExistsError):
        write_dxf(rows, tmp_path / "one.dxf")


@pytest.mark.parametrize("field,value", [("elevation", 2), ("thickness", 1), ("extrusion", (0, 1, 1)), ("elevation", float("nan"))])
def test_dxf_rejects_ocs_elevation_and_nonfinite(tmp_path, field, value):
    import ezdxf
    rows = batch_rows(synthetic_batch())
    path = tmp_path / "not_2d.dxf"
    write_dxf(rows, path)
    doc = ezdxf.readfile(path)
    entity = next(e for e in doc.modelspace() if e.dxftype() == "LWPOLYLINE")
    setattr(entity.dxf, field, value)
    doc.saveas(path)
    with pytest.raises(ValueError): read_dxf(path)


def test_dxf_rejects_point_z_and_affine_tampering(tmp_path):
    import ezdxf
    import hashlib
    from vkm_geometry.transport import APPID, _read_xrecord, _xrecord
    rows = batch_rows(synthetic_batch())
    path = tmp_path / "point_z.dxf"
    write_dxf(rows, path)
    doc = ezdxf.readfile(path)
    next(e for e in doc.modelspace() if e.dxftype() == "POINT").dxf.location = (1.125, -2.25, 1)
    doc.saveas(path)
    with pytest.raises(ValueError, match="point Z"): read_dxf(path)
    path = tmp_path / "affine.dxf"
    write_dxf(rows, path)
    doc = ezdxf.readfile(path)
    dictionary = doc.rootdict[APPID]
    token = hashlib.sha256(rows[0]["source_feature_id"].encode()).hexdigest()
    record = _read_xrecord(dictionary, token)
    record["affine"]["forward"][0][2] = 10
    _xrecord(dictionary, token, record)
    doc.saveas(path)
    with pytest.raises(ValueError, match="affine metadata"): read_dxf(path)


def test_wkb_nonfinite_and_actual_bool_are_rejected():
    from shapely.geometry import Point
    from vkm_geometry.transport import row_digest, validate_rows, assert_geometry_equal
    rows = batch_rows(synthetic_batch())
    rows[0]["wkb_hex"] = Point(float("nan"), 2).wkb_hex
    rows[0]["row_sha256"] = row_digest(rows[0])
    with pytest.raises(ValueError): validate_rows(rows)
    with pytest.raises(ValueError):
        assert_geometry_equal({"type": "Point", "coordinates": [1, 2]}, {"type": "Point", "coordinates": [True, 2]})

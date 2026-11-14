"""A/B source batches, native WKB and reversible DXF representations.

Source JSON remains authoritative for attributes and identity. Every format
readback checks the actual geometry, including multipart order and vertices.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
from pathlib import Path
from typing import Any

TOLERANCE = 1e-9
TYPES = {"Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon"}
A_FIELDS = {"source_feature_id", "source_id", "work_id", "pdf_page", "printed_page", "figure_id", "panel_id",
            "geometry", "image_coordinate_frame", "source_label", "geological_meaning", "value", "unit",
            "legend_reference", "horizon", "source_image", "source_image_sha256", "extraction_status",
            "epistemic_role", "review_status", "primitive_role", "provenance"}
FRAME_FIELDS = {"id", "origin", "x_axis", "y_axis", "unit", "width", "height", "pixel_to_pdf_page_pt", "crs", "registration_status"}
LOCAL_FRAME_FIELDS = {"id", "kind", "origin", "x_axis", "y_axis", "unit", "crs", "registration_status"}
B_FIELDS = {"source_feature_id", "candidate_project_object_ids", "match_status", "match_evidence", "conflicts", "limitations"}
ROW_FIELDS = ("batch_id", "source_feature_id", "source_id", "work_id", "frame_id", "coordinate_unit",
              "geometry_type", "wkb_hex", "feature_json", "matches_json", "batch_provenance_json")
APPID = "VKM_SOURCE_GEOMETRY"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def row_digest(row: dict) -> str:
    return hashlib.sha256(canonical_json({key: row[key] for key in ROW_FIELDS}).encode("utf-8")).hexdigest()


def coordinate_frame(feature: dict) -> dict:
    """Geometry's frame; the optional image frame remains untouched provenance."""
    frame = feature.get("geometry_coordinate_frame")
    if frame is None:
        frame = feature.get("image_coordinate_frame")
    if not isinstance(frame, dict):
        raise ValueError("An explicit geometry/image coordinate frame is required")
    return frame


def _coordinates(geometry: dict) -> None:
    def walk(value):
        if not isinstance(value, (list, tuple)) or not value:
            raise ValueError("Empty or malformed coordinate sequence")
        if isinstance(value[0], (int, float)):
            if len(value) != 2 or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in value):
                raise ValueError("Only finite 2D source coordinates are supported")
        else:
            for child in value:
                walk(child)
    walk(geometry["coordinates"])
    polygons = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"] if geometry["type"] == "MultiPolygon" else []
    for polygon in polygons:
        for ring in polygon:
            if len(ring) < 4 or ring[0] != ring[-1]:
                raise ValueError("Source polygon rings must be explicitly closed; no silent closure")


def geometry_to_wkb(feature: dict) -> bytes:
    from shapely.geometry import shape
    from shapely import to_wkb
    geometry = feature["geometry"]
    if geometry.get("type") not in TYPES:
        raise ValueError("Unsupported source geometry type")
    _coordinates(geometry)
    geom = shape(geometry)
    if geom.is_empty:
        raise ValueError("Empty source geometry is not a zero geometry")
    # Invalid source topology is retained, never repaired here.
    return to_wkb(geom, byte_order=1, include_srid=False, output_dimension=2)


def validate_batch(batch: dict) -> dict:
    required = {"batch_id", "extraction_version", "features", "project_matches", "provenance"}
    if not isinstance(batch, dict) or not required <= batch.keys():
        raise ValueError("Incomplete source batch")
    if not isinstance(batch["features"], list) or not batch["features"]:
        raise ValueError("Batch features must be nonempty")
    if not isinstance(batch["project_matches"], list):
        raise ValueError("project_matches must be a list")
    ids = set()
    frames = {}
    for feature in batch["features"]:
        missing = A_FIELDS - feature.keys()
        if missing:
            raise ValueError(f"Missing source feature fields: {sorted(missing)}")
        fid = feature["source_feature_id"]
        if not isinstance(fid, str) or not fid or fid in ids:
            raise ValueError("Source feature IDs must be unique nonempty strings")
        ids.add(fid)
        if not feature["work_id"] or (feature["work_id"] == feature["source_id"] and not feature.get("work_id_basis")):
            raise ValueError("work_id needs an explicit existing identity/basis")
        if not isinstance(feature["pdf_page"], int) or isinstance(feature["pdf_page"], bool) or feature["pdf_page"] < 1:
            raise ValueError("pdf_page must be 1-based")
        frame = coordinate_frame(feature)
        local = feature.get("geometry_coordinate_frame") is not None
        required_frame = LOCAL_FRAME_FIELDS if local else FRAME_FIELDS
        if not required_frame <= frame.keys() or not frame["id"] or not frame["unit"]:
            raise ValueError("Incomplete explicit geometry coordinate frame")
        if local:
            if frame["kind"] != "LOCAL_ENGINEERING" or frame["unit"] not in {"m", "metre", "meter", "unknown"} or frame["crs"] not in (None, "UNKNOWN"):
                raise ValueError("Local engineering geometry needs declared units and UNKNOWN CRS, no fabricated pixel/metre/EPSG frame")
        else:
            if frame["unit"].lower() not in {"px", "pixel", "pixels"}:
                raise ValueError("Image pixel coordinates cannot be relabelled as metres; use an explicit geometry frame")
        image_frame = feature.get("image_coordinate_frame")
        if image_frame is not None:
            if not isinstance(image_frame, dict) or not FRAME_FIELDS <= image_frame.keys():
                raise ValueError("Incomplete original image provenance frame")
            if image_frame["unit"].lower() not in {"px", "pixel", "pixels"}:
                raise ValueError("Original image provenance dimensions are pixels, never metres")
            if any(isinstance(image_frame[k], bool) or not isinstance(image_frame[k], (int, float)) or not math.isfinite(image_frame[k]) or image_frame[k] <= 0 for k in ("width", "height")):
                raise ValueError("Source image dimensions must be positive pixels")
        encoded = canonical_json(frame)
        if frame["id"] in frames and frames[frame["id"]] != encoded:
            raise ValueError("Conflicting definitions of one coordinate frame")
        frames[frame["id"]] = encoded
        if frame["unit"].lower() in {"px", "pixel", "pixels"} and frame["crs"] not in (None, "UNKNOWN", "CRITICALMAAS:pixel"):
            raise ValueError("Native pixels cannot silently acquire a geographic CRS")
        provenance = feature["provenance"]
        if not isinstance(provenance, dict) or not {"input_sha256", "extraction_version", "transformation_history"} <= provenance.keys():
            raise ValueError("Incomplete feature provenance")
        image, image_sha = feature["source_image"], feature["source_image_sha256"]
        artifact = feature.get("source_artifact")
        if image is None or image_sha is None:
            if image is not None or image_sha is not None or not local or not isinstance(artifact, dict) or artifact.get("kind") != "VECTOR" or not artifact.get("logical_path") or not isinstance(artifact.get("sha256"), str) or len(artifact["sha256"]) != 64:
                raise ValueError("Absent source image requires a pinned VECTOR source_artifact and explicit local geometry frame")
        elif not isinstance(image_sha, str) or len(image_sha) != 64:
            raise ValueError("Source image SHA-256 is required")
        geometry_to_wkb(feature)
    for match in batch["project_matches"]:
        if not isinstance(match, dict) or not B_FIELDS <= match.keys() or match["source_feature_id"] not in ids:
            raise ValueError("B-match must reference a source feature in the batch")
    canonical_json(batch)
    return batch


def load_batch(path: Path | str) -> dict:
    return validate_batch(json.loads(Path(path).read_text(encoding="utf-8-sig")))


def batch_rows(batch: dict) -> list[dict]:
    validate_batch(batch)
    rows = []
    for feature in batch["features"]:
        frame = coordinate_frame(feature)
        row = {"batch_id": batch["batch_id"], "source_feature_id": feature["source_feature_id"],
               "source_id": feature["source_id"], "work_id": feature["work_id"], "frame_id": frame["id"],
               "coordinate_unit": frame["unit"], "geometry_type": feature["geometry"]["type"],
               "wkb_hex": geometry_to_wkb(feature).hex(), "feature_json": canonical_json(feature),
               "matches_json": canonical_json([m for m in batch["project_matches"] if m["source_feature_id"] == feature["source_feature_id"]]),
               "batch_provenance_json": canonical_json({k: v for k, v in batch.items() if k not in {"features", "project_matches"}})}
        row["row_sha256"] = row_digest(row)
        rows.append(row)
    return rows


def _assert_coordinates(left, right, tolerance=TOLERANCE):
    if isinstance(left, (int, float)) and not isinstance(left, bool):
        if not math.isfinite(left) or isinstance(right, bool) or not isinstance(right, (int, float)) or not math.isfinite(right) or abs(left - right) > tolerance:
            raise ValueError("Geometry vertex differs")
    else:
        if not isinstance(right, (list, tuple)) or len(left) != len(right):
            raise ValueError("Geometry vertex/component count differs")
        for a, b in zip(left, right):
            _assert_coordinates(a, b, tolerance)


def assert_geometry_equal(expected: dict, actual: dict, tolerance=TOLERANCE):
    if expected["type"] != actual["type"]:
        raise ValueError("Geometry type changed")
    _assert_coordinates(expected["coordinates"], actual["coordinates"], tolerance)


def validate_rows(rows: list[dict]) -> list[dict]:
    from shapely import from_wkb
    from shapely.geometry import mapping
    seen = set()
    for row in rows:
        if row["source_feature_id"] in seen or row_digest(row) != row.get("row_sha256"):
            raise ValueError("Duplicate or changed transport row")
        seen.add(row["source_feature_id"])
        feature = json.loads(row["feature_json"])
        geometry_to_wkb(feature)
        actual_geometry = mapping(from_wkb(bytes.fromhex(row["wkb_hex"])))
        _coordinates(actual_geometry)
        assert_geometry_equal(feature["geometry"], actual_geometry)
        for key, want in (("source_feature_id", feature["source_feature_id"]), ("source_id", feature["source_id"]),
                          ("work_id", feature["work_id"]), ("frame_id", coordinate_frame(feature)["id"]),
                          ("coordinate_unit", coordinate_frame(feature)["unit"]), ("geometry_type", feature["geometry"]["type"])):
            if row[key] != want:
                raise ValueError(f"Transport metadata differs: {key}")
    return rows


def _strict_2d_entity(entity):
    def zero(value):
        return math.isfinite(float(value)) and float(value) == 0
    extrusion = tuple(entity.dxf.get("extrusion", (0, 0, 1)))
    if len(extrusion) != 3 or not all(math.isfinite(float(x)) for x in extrusion) or extrusion != (0, 0, 1):
        raise ValueError("Non-default DXF extrusion/OCS is outside the 2D contract")
    if not zero(entity.dxf.get("thickness", 0)):
        raise ValueError("Nonzero/nonfinite DXF thickness")
    if entity.dxftype() == "POINT":
        if not zero(entity.dxf.location.z): raise ValueError("Nonzero/nonfinite DXF point Z")
        if not all(math.isfinite(float(x)) for x in entity.dxf.location): raise ValueError("Nonfinite DXF point")
    elif entity.dxftype() == "LWPOLYLINE":
        if not zero(entity.dxf.get("elevation", 0)) or not zero(entity.dxf.get("const_width", 0)):
            raise ValueError("Nonzero/nonfinite DXF elevation or width")
        for point in entity.get_points():
            if not all(math.isfinite(float(x)) for x in point) or any(not zero(point[i]) for i in (2, 3, 4)):
                raise ValueError("Unexpected nonfinite DXF vertex, width or bulge")


def write_wkb_jsonl(rows: list[dict], path: Path | str):
    validate_rows(rows)
    with Path(path).open("x", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(canonical_json(row) + "\n")


def read_wkb_jsonl(path: Path | str) -> list[dict]:
    return validate_rows([json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()])


def assert_rows_equal(expected: list[dict], actual: list[dict], tolerance=TOLERANCE):
    validate_rows(expected); validate_rows(actual)
    a, b = {r["source_feature_id"]: r for r in expected}, {r["source_feature_id"]: r for r in actual}
    if a.keys() != b.keys():
        raise ValueError("Source feature ID set changed")
    for fid, row in a.items():
        got = b[fid]
        for key in ROW_FIELDS:
            if key == "wkb_hex":
                from shapely import from_wkb
                from shapely.geometry import mapping
                assert_geometry_equal(mapping(from_wkb(bytes.fromhex(row[key]))), mapping(from_wkb(bytes.fromhex(got[key]))), tolerance)
            elif row[key] != got[key]:
                raise ValueError(f"Source attribute changed: {fid}/{key}")


def cad_affine(frame: dict) -> dict:
    x, y = str(frame["x_axis"]).lower(), str(frame["y_axis"]).lower()
    if x not in {"right", "x right", "positive right", "east", "x east"} or y not in {"down", "up", "y down", "y up", "north", "y north"}:
        raise ValueError("CAD representation requires explicit right/up-or-down source axes")
    sign = -1 if "down" in y else 1
    return {"forward": [[1, 0, 0], [0, sign, 0], [0, 0, 1]], "inverse": [[1, 0, 0], [0, sign, 0], [0, 0, 1]],
            "convention": "homogeneous column vector [x,y,1]", "role": "REVERSIBLE_REPRESENTATION_ONLY",
            "source_frame_id": frame["id"], "source_origin": frame["origin"], "unit": frame["unit"],
            "registration": "NONE; reflection about source origin, no H/H-1 pixel assumption"}


def geometry_parts(geometry: dict):
    kind, coords = geometry["type"], geometry["coordinates"]
    if kind == "Point": return [((), "point", coords)]
    if kind == "MultiPoint": return [((i,), "point", p) for i, p in enumerate(coords)]
    if kind == "LineString": return [((), "line", coords)]
    if kind == "MultiLineString": return [((i,), "line", p) for i, p in enumerate(coords)]
    polygons = [coords] if kind == "Polygon" else coords
    return [((i, j), "shell" if j == 0 else "hole", ring) for i, poly in enumerate(polygons) for j, ring in enumerate(poly)]


def _xrecord(dictionary, name: str, value):
    encoded = base64.b64encode(canonical_json(value).encode("utf-8")).decode("ascii")
    record = dictionary.doc.objects.add_xrecord()
    dictionary.add(name, record)
    record.reset([(1, encoded[i:i + 240]) for i in range(0, len(encoded), 240)])


def _read_xrecord(dictionary, name: str):
    data = "".join(tag.value for tag in dictionary[name].tags if tag.code == 1)
    return json.loads(base64.b64decode(data, validate=True).decode("utf-8"))


def _expected_dxf_parts(feature: dict) -> list[dict]:
    """Canonical representation manifest; never trust a shortened stored list."""
    fid = feature["source_feature_id"]
    source_parts = geometry_parts(feature["geometry"])
    return [{"path": list(path), "role": role, "component_index": index,
             "source_feature_id": fid if len(source_parts) == 1 else f"{fid}:component:{index:04d}",
             "parent_source_feature_id": fid, "vertex_count": 1 if role == "point" else len(points)}
            for index, (path, role, points) in enumerate(source_parts)]


def _geometry_from_actual_parts(kind: str, parts: list[tuple[dict, list]]) -> dict:
    """Build solely from observed native vertices; source coordinates never fill gaps."""
    if kind in {"Point", "LineString"}:
        coordinates = parts[0][1]
    elif kind in {"MultiPoint", "MultiLineString", "Polygon"}:
        coordinates = [points for _, points in parts]
    elif kind == "MultiPolygon":
        polygons = {}
        for metadata, points in parts:
            polygon, ring = metadata["path"]
            polygons.setdefault(polygon, {})[ring] = points
        coordinates = [[rings[index] for index in sorted(rings)] for _, rings in sorted(polygons.items())]
    else:
        raise ValueError("Unsupported DXF geometry type")
    return {"type": kind, "coordinates": coordinates}


def write_dxf(rows: list[dict], path: Path | str):
    import ezdxf
    validate_rows(rows)
    path = Path(path)
    if path.exists(): raise FileExistsError(path)
    doc = ezdxf.new("R2013")
    doc.header["$INSUNITS"] = 0  # unit preserved explicitly, never claim pixels are metres
    doc.appids.new(APPID)
    metadata = doc.rootdict.get_required_dict(APPID)
    _xrecord(metadata, "MANIFEST", {"schema": "vkm.geometry-dxf/1", "ids": [r["source_feature_id"] for r in rows], "tolerance": TOLERANCE})
    space = doc.modelspace()
    for row in rows:
        fid = row["source_feature_id"]; token = hashlib.sha256(fid.encode()).hexdigest()
        feature = json.loads(row["feature_json"])
        affine = cad_affine(coordinate_frame(feature))
        sign = affine["forward"][1][1]
        parts = _expected_dxf_parts(feature)
        for index, (partpath, role, points) in enumerate(geometry_parts(feature["geometry"])):
            layer = "VKM_" + role.upper()
            if layer not in doc.layers: doc.layers.new(layer)
            if role == "point":
                entity = space.add_point((points[0], sign * points[1]), dxfattribs={"layer": layer})
            else:
                closed = role in {"shell", "hole"}
                vertices = points[:-1] if closed else points
                entity = space.add_lwpolyline([(p[0], sign * p[1]) for p in vertices], close=closed, dxfattribs={"layer": layer})
            entity.set_xdata(APPID, [(1000, token), (1071, index), (1000, role)])
        _xrecord(metadata, token, {"row": row, "affine": affine, "parts": parts})
    doc.saveas(path)
    return {"path": str(path), "features": len(rows), "entities": len(space), "metadata": "named-object XRECORD base64 chunks; lightweight entity XDATA"}


def read_dxf(path: Path | str) -> list[dict]:
    import ezdxf
    from shapely.geometry import shape
    from shapely import to_wkb
    doc = ezdxf.readfile(path)
    if doc.header["$INSUNITS"] != 0: raise ValueError("DXF units changed")
    metadata = doc.rootdict[APPID]
    manifest = _read_xrecord(metadata, "MANIFEST")
    grouped = {}
    for entity in doc.modelspace():
        _strict_2d_entity(entity)
        tags = entity.get_xdata(APPID)
        token, index, role = tags[0].value, tags[1].value, tags[2].value
        if index in grouped.setdefault(token, {}): raise ValueError("Duplicate DXF representation part")
        if role == "point":
            if entity.dxftype() != "POINT": raise ValueError("DXF point type changed")
            points = [entity.dxf.location.x, entity.dxf.location.y]
        else:
            if entity.dxftype() != "LWPOLYLINE" or bool(entity.closed) != (role in {"shell", "hole"}):
                raise ValueError("DXF line/ring type or closure changed")
            if any(abs(p[4]) > 0 for p in entity.get_points()): raise ValueError("Unexpected DXF bulge")
            points = [[float(p[0]), float(p[1])] for p in entity.get_points()]
            if role in {"shell", "hole"}: points.append(points[0][:])
        grouped[token][index] = (role, points)
    rows = []
    for fid in manifest["ids"]:
        token = hashlib.sha256(fid.encode()).hexdigest()
        record = _read_xrecord(metadata, token); row = record["row"]
        feature = json.loads(row["feature_json"])
        if row["source_feature_id"] != fid or canonical_json(record["affine"]) != canonical_json(cad_affine(coordinate_frame(feature))):
            raise ValueError("DXF identity or affine metadata changed")
        parts = record["parts"]
        if canonical_json(parts) != canonical_json(_expected_dxf_parts(feature)):
            raise ValueError("DXF representation part manifest changed; source metadata cannot replace native geometry")
        if set(grouped.get(token, {})) != set(range(len(parts))): raise ValueError("DXF part count changed")
        sign = record["affine"]["inverse"][1][1]
        observed_parts = []
        for index, part in enumerate(parts):
            role, points = grouped[token][index]
            if role != part["role"]: raise ValueError("DXF ring/component role changed")
            if role == "point": native = [points[0], sign * points[1]]
            else: native = [[p[0], sign * p[1]] for p in points]
            if (1 if role == "point" else len(native)) != part["vertex_count"]: raise ValueError("DXF vertex count changed")
            observed_parts.append((part, native))
        geometry = _geometry_from_actual_parts(feature["geometry"]["type"], observed_parts)
        assert_geometry_equal(feature["geometry"], geometry)
        actual = dict(row, wkb_hex=to_wkb(shape(geometry), byte_order=1, include_srid=False).hex())
        actual["row_sha256"] = row_digest(actual)
        rows.append(actual)
    if len(grouped) != len(rows): raise ValueError("Foreign DXF geometry with unknown source identity")
    return validate_rows(rows)

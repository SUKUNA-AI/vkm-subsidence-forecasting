"""Reproducible colour-envelope digitisation in an explicitly separate source-section frame.

Pixels are graphical source controls, not field measurements. Only observed continuous colour segments are
represented; missing samples are never bridged. This module creates no map registration or 3D surface.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

import numpy as np
from PIL import Image, ImageDraw


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for data in iter(lambda: stream.read(1 << 20), b""):
            digest.update(data)
    return digest.hexdigest()


def _local_path(root: Path, relative: Path | str) -> Path:
    relative = Path(relative)
    if relative.is_absolute():
        raise ValueError("Source paths must be relative to resources_root")
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("Source path escapes resources_root")
    return resolved


def continuous_segments(valid):
    """Return half-open sample index ranges without joining across a missing observation."""
    valid = np.asarray(valid, dtype=bool)
    edges = np.diff(np.r_[False, valid, False].astype("int8"))
    return [(int(start), int(stop)) for start, stop in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1))]


def sample_colour_band(rgb, bbox, colour_rgb, *, sample_step_px=10, window_half_width_px=3,
                       rgb_tolerance=18, minimum_class_pixels=4, exclude_bbox_pixels=()):
    """Median of per-column minimum/maximum matching Y; bbox endpoints are inclusive pixel indices.

    A column contributes only with at least minimum_class_pixels colour matches. An X sample with no
    contributing columns is missing. No interpolation, morphological closure or extrapolation is applied.
    """
    rgb = np.asarray(rgb)
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError("Expected an RGB image")
    left, top, right, bottom = map(int, bbox)
    if not (0 <= left < right < rgb.shape[1] and 0 <= top < bottom < rgb.shape[0]):
        raise ValueError("plot_bbox_px must be inside the source image (inclusive endpoints)")
    if sample_step_px <= 0 or window_half_width_px < 0 or minimum_class_pixels < 1 or rgb_tolerance < 0:
        raise ValueError("Invalid colour-sampling parameters")
    colour = np.asarray(colour_rgb, dtype="float64")
    if colour.shape != (3,) or not np.isfinite(colour).all() or (colour < 0).any() or (colour > 255).any():
        raise ValueError("colour_rgb must contain three byte values")
    pixels = rgb[top:bottom+1, left:right+1].astype("float64")
    matched = ((pixels-colour)**2).sum(axis=2) <= float(rgb_tolerance)**2
    matched_before_exclusion = int(matched.sum())
    for box in exclude_bbox_pixels:
        x0, y0, x1, y1 = map(int, box)
        if not (0 <= x0 <= x1 < rgb.shape[1] and 0 <= y0 <= y1 < rgb.shape[0]):
            raise ValueError("An explicit annotation exclusion box is outside the source image")
        x0, x1, y0, y1 = max(left, x0), min(right, x1), max(top, y0), min(bottom, y1)
        if x0 <= x1 and y0 <= y1:
            matched[y0-top:y1-top+1, x0-left:x1-left+1] = False
    xs = np.arange(left, right+1, int(sample_step_px), dtype="int32")
    upper, lower = np.full(len(xs), np.nan), np.full(len(xs), np.nan)
    contributing = np.zeros(len(xs), dtype="int32")
    examined = np.zeros(len(xs), dtype="int32")
    for index, x in enumerate(xs):
        lows, highs = [], []
        first, last = max(left, int(x)-window_half_width_px), min(right, int(x)+window_half_width_px)
        examined[index] = last-first+1
        for column in range(first, last+1):
            ys = np.flatnonzero(matched[:, column-left])
            if len(ys) >= minimum_class_pixels:
                lows.append(int(ys[0])+top)
                highs.append(int(ys[-1])+top)
        if lows:
            upper[index], lower[index] = float(np.median(lows)), float(np.median(highs))
            contributing[index] = len(lows)
    valid = np.isfinite(upper) & np.isfinite(lower)
    segments = continuous_segments(valid)
    gap_lengths = [stop-start for start, stop in continuous_segments(~valid)]
    return {"x_px": xs, "top_px": upper, "bottom_px": lower, "valid": valid,
            "contributing_columns": contributing,
            "qa": {"samples": len(xs), "valid_samples": int(valid.sum()),
                   "valid_fraction": float(valid.mean()), "gap_fraction": float((~valid).mean()),
                   "longest_gap_samples": max(gap_lengths, default=0),
                   "segments": len(segments), "polygon_segments": sum(stop-start >= 2 for start, stop in segments),
                   "singleton_samples": sum(stop-start == 1 for start, stop in segments),
                   "contributing_column_fraction": float(contributing.sum()/examined.sum()),
                   "annotation_exclusion_boxes": [list(box) for box in exclude_bbox_pixels],
                   "excluded_matching_pixels": matched_before_exclusion-int(matched.sum()),
                   "negative_pixel_thickness_samples": int((lower[valid] < upper[valid]).sum())}}


def affine_axis(pixel_values, anchors):
    """An explicit two-anchor graphical scale; no fitted coordinates or inferred datum."""
    (pixel_a, value_a), (pixel_b, value_b) = np.asarray(anchors, dtype="float64")
    if not np.isfinite([pixel_a, value_a, pixel_b, value_b]).all() or pixel_a == pixel_b or value_a == value_b:
        raise ValueError("Axis requires two distinct finite pixel/value anchors")
    return value_a + (np.asarray(pixel_values, dtype="float64")-pixel_a)*(value_b-value_a)/(pixel_b-pixel_a)


def digitise_section(rgb, config):
    """Produce features and QA from a pinned config; all coordinates remain in its printed section frame."""
    if config.get("schema") != "vkm.source_section_config/1":
        raise ValueError("Unsupported source section schema")
    frame = config["frame"]
    if (frame.get("kind") != "SOURCE_SECTION" or not frame.get("id")
            or frame.get("source_x_unit") != "m" or frame.get("source_y_unit") != "m"
            or frame.get("source_y_axis") not in {"UP", "DOWN"}):
        raise ValueError("An explicit metric SOURCE_SECTION frame and vertical direction are required")
    scales = [frame[k] for k in ("x_origin_px", "y_origin_px", "x_m_per_px", "y_m_per_px")]
    if not np.isfinite(scales).all() or not frame["x_m_per_px"] or not frame["y_m_per_px"]:
        raise ValueError("Invalid explicit graphical axis scale")
    bbox = config["plot_bbox_pixels"]
    params = config.get("sampling", {})
    arguments = {"sample_step_px": params.get("step_px", 10),
                 "window_half_width_px": params.get("half_window_px", 3),
                 "rgb_tolerance": params.get("rgb_distance_tolerance", 18),
                 "minimum_class_pixels": params.get("min_color_pixels_per_column", 4)}
    source = config["source"]
    features, bands, preview = [], {}, []

    def metric(points):
        return [[float((x-frame["x_origin_px"])*frame["x_m_per_px"]),
                 float((y-frame["y_origin_px"])*frame["y_m_per_px"])] for x, y in points]

    def add(record_id, layer, geometry_type, points, source_properties, role, colour=None):
        properties = {"record_id": frame["id"]+":"+record_id, "frame_kind": "SOURCE_SECTION", "frame_id": frame["id"],
                      "source_x_unit": "m", "source_y_unit": "m", "source_y_axis": frame["source_y_axis"],
                      "geometry_role": "SOURCE_SECTION_SEPARATE_FRAME", "semantic_role": role,
                      "epistemic_status": "DERIVATION", "verification": "GRAPHICAL_DIGITISATION_NOT_FIELD_VALIDATION",
                      "source_id": source.get("source_id", "UNKNOWN"), "source_path": source["document"],
                      "source_sha256": source["document_sha256"], "unit_id": source_properties.get("id", "UNKNOWN"),
                      "unit_as_printed": source_properties.get("unit_as_printed", source_properties.get("label", "UNKNOWN")),
                      "color_rgb": json.dumps(colour) if colour is not None else "UNKNOWN",
                      "source_properties_json": json.dumps(source_properties, ensure_ascii=False, sort_keys=True),
                      "vertical_datum": "UNKNOWN", "map_trace": "UNKNOWN", "map_vertical_tie": "UNKNOWN"}
        features.append({"layer": layer, "geometry_type": geometry_type, "coordinates": metric(points), "properties": properties})
        preview.append({"geometry_type": geometry_type, "points": points, "colour": colour or [50, 50, 50]})

    identities = [unit["id"] for unit in config["units"]]
    if len(set(identities)) != len(identities):
        raise ValueError("Duplicate colour-unit IDs")
    for unit in config["units"]:
        band = sample_colour_band(rgb, bbox, unit["color_rgb"], **arguments,
                                  exclude_bbox_pixels=unit.get("exclude_bbox_pixels", []))
        bands[unit["id"]] = band
        for segment, (start, stop) in enumerate(continuous_segments(band["valid"])):
            if stop-start < 2:
                continue  # A singleton is an observed sample, not a supported polygon.
            upper = list(zip(band["x_px"][start:stop].tolist(), band["top_px"][start:stop].tolist()))
            lower = list(zip(band["x_px"][start:stop].tolist(), band["bottom_px"][start:stop].tolist()))
            outline = upper+lower[::-1]+[upper[0]]
            prefix = "unit:"+unit["id"]+":segment:"+str(segment)
            add(prefix, "section_units", "Polygon", outline, unit, "SOURCE_COLOUR_ENVELOPE", unit["color_rgb"])
            add(prefix+":top", "section_boundaries", "LineString", upper, unit, "SOURCE_COLOUR_ENVELOPE_TOP", unit["color_rgb"])
            add(prefix+":bottom", "section_boundaries", "LineString", lower, unit, "SOURCE_COLOUR_ENVELOPE_BOTTOM", unit["color_rgb"])
    line_qa = {}
    for line in config.get("lines", []):
        band = sample_colour_band(rgb, line.get("bbox_pixels", bbox), line["color_rgb"], **arguments)
        line_qa[line["id"]] = band["qa"]
        for segment, (start, stop) in enumerate(continuous_segments(band["valid"])):
            if stop-start < 2:
                continue
            centre = (band["top_px"]+band["bottom_px"])/2
            points = list(zip(band["x_px"][start:stop].tolist(), centre[start:stop].tolist()))
            add("line:"+line["id"]+":segment:"+str(segment), "section_lines", "LineString", points, line,
                "SOURCE_GRAPHICAL_BOUNDARY_CENTRE", line["color_rgb"])
    for control in config.get("controls", []):
        points = control["xy_pixels"]
        if not points or not np.isfinite(np.asarray(points, dtype="float64")).all():
            raise ValueError("Control must have finite explicit source-image coordinates")
        if any(not (0 <= x < rgb.shape[1] and 0 <= y < rgb.shape[0]) for x, y in points):
            raise ValueError("Control outside source-image bounds")
        add("control:"+control["id"], "section_controls", "Point" if len(points) == 1 else "LineString", points,
            control, control.get("kind", "SOURCE_GRAPHICAL_CONTROL"))
    order_qa = []
    for upper_id, lower_id in zip(identities, identities[1:]):
        upper, lower = bands[upper_id], bands[lower_id]
        both = upper["valid"] & lower["valid"]
        overlaps = both & (lower["top_px"] < upper["bottom_px"])
        order_qa.append({"upper_id": upper_id, "lower_id": lower_id, "comparable_samples": int(both.sum()),
                         "overlap_samples": int(overlaps.sum()),
                         "overlap_fraction": float(overlaps.sum()/both.sum()) if both.any() else None,
                         "scope": "Colour-envelope diagnostic in config order; no enforced geological stratigraphy"})
    ids = [f["properties"]["record_id"] for f in features]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate derived section IDs")
    qa = {"units": {key: band["qa"] for key, band in bands.items()}, "lines": line_qa, "order": order_qa,
          "samples": {key: [{"x_px": int(x), "top_px": float(top) if valid else None,
                              "bottom_px": float(bottom) if valid else None, "valid": bool(valid),
                              "contributing_columns": int(columns)}
                             for x, top, bottom, valid, columns in zip(band["x_px"], band["top_px"], band["bottom_px"],
                                                                       band["valid"], band["contributing_columns"])]
                      for key, band in bands.items()},
          "feature_count": len(features), "frame": frame,
          "scope": "Graphical DERIVATION; connecting supported samples is a MODEL_CHOICE; missing X samples are never bridged",
          "scientific_admission": "NOT_CHECKED", "field_validation": "NOT_RUN"}
    return features, qa, preview


def _write_gpkg(path, features):
    from osgeo import gdal, ogr, osr
    gdal.UseExceptions()
    ogr.UseExceptions()
    driver = ogr.GetDriverByName("GPKG")
    dataset = driver.CreateDataSource(str(path))
    spatial = osr.SpatialReference()
    spatial.ImportFromWkt('LOCAL_CS["separate_source_section",LOCAL_DATUM["UNKNOWN",0],UNIT["metre",1],AXIS["source distance",EAST],AXIS["source vertical",NORTH]]')
    layers = {}
    field_names = sorted({key for feature in features for key in feature["properties"]})
    for name, geometry_type in (("section_units", ogr.wkbPolygon), ("section_boundaries", ogr.wkbLineString),
                                ("section_lines", ogr.wkbLineString), ("section_controls", ogr.wkbUnknown)):
        layer = dataset.CreateLayer(name, srs=spatial, geom_type=geometry_type)
        for field in field_names:
            layer.CreateField(ogr.FieldDefn(field, ogr.OFTString))
        layers[name] = layer
    invalid = []
    for record in features:
        geometry = ogr.Geometry({"Polygon": ogr.wkbPolygon, "LineString": ogr.wkbLineString, "Point": ogr.wkbPoint}[record["geometry_type"]])
        line = ogr.Geometry(ogr.wkbLinearRing) if record["geometry_type"] == "Polygon" else geometry
        for x, y in record["coordinates"]:
            line.AddPoint_2D(x, y)
        if record["geometry_type"] == "Polygon":
            geometry.AddGeometry(line)
        if not geometry.IsValid():
            invalid.append(record["properties"]["record_id"])
        feature = ogr.Feature(layers[record["layer"]].GetLayerDefn())
        for key, value in record["properties"].items():
            feature.SetField(key, str(value))
        feature.SetGeometry(geometry)
        if layers[record["layer"]].CreateFeature(feature) != 0:
            raise RuntimeError("Failed to write source section feature")
        feature = None
    dataset = None
    # This is generated-container metadata, not a source/observation date.
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE gpkg_contents SET last_change='2000-01-01T00:00:00.000Z'")
    connection.close()
    # Reopen all layers after writer closure, including real geometry/ID checks.
    dataset = ogr.Open(str(path))
    counts, reread_ids = {}, []
    for layer in dataset:
        count = 0
        for feature in layer:
            assert feature.GetGeometryRef() is not None and not feature.GetGeometryRef().IsEmpty()
            reread_ids.append(feature.GetField("record_id"))
            count += 1
        counts[layer.GetName()] = count
    assert sorted(reread_ids) == sorted(f["properties"]["record_id"] for f in features)
    dataset = None
    return {"read_back": "PASS", "layer_counts": counts, "invalid_geometry_ids": invalid}


def build_source_section(resources_root: Path, config_rel: Path, out: Path) -> dict:
    """Read pinned JSON+PNG, then create separate-frame GPKG, derived PNG, full QA and hash receipt.

    The JSON schema is ``vkm.source_section_config/1`` with source/image hashes and explicit affine frame.
    Output is a new directory; no source file or source geometry is changed.
    """
    resources_root, out = Path(resources_root).resolve(), Path(out).resolve()
    config_path = _local_path(resources_root, config_rel)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    image_path = _local_path(resources_root, config["image"])
    document_path = _local_path(resources_root, config["source"]["document"])
    inputs = {Path(config_rel).as_posix(): _sha(config_path), config["image"]: _sha(image_path),
              config["source"]["document"]: _sha(document_path)}
    if inputs[config["image"]] != config["source"]["image_sha256"]:
        raise ValueError("Source image SHA-256 differs from pinned configuration")
    if inputs[config["source"]["document"]] != config["source"]["document_sha256"]:
        raise ValueError("Source document SHA-256 differs from pinned configuration")
    code_hash = _sha(Path(__file__))
    with Image.open(image_path) as image:
        rgb = np.asarray(image.convert("RGB"))
    features, qa, preview = digitise_section(rgb, config)
    if out.exists():
        raise FileExistsError("Source section output must be a new directory")
    out.mkdir(parents=True)
    qa["geometry"] = _write_gpkg(out/"source_section.gpkg", features)
    canvas = Image.new("RGB", (rgb.shape[1], rgb.shape[0]), "white")
    draw = ImageDraw.Draw(canvas)
    for record in preview:
        points = [tuple(point) for point in record["points"]]
        colour = tuple(record["colour"])
        if record["geometry_type"] == "Polygon":
            draw.polygon(points, fill=colour, outline=(50, 50, 50))
    for record in preview:
        points = [tuple(point) for point in record["points"]]
        if record["geometry_type"] == "LineString":
            draw.line(points, fill=tuple(record["colour"]), width=2)
        elif record["geometry_type"] == "Point":
            x, y = points[0]
            draw.ellipse((x-3, y-3, x+3, y+3), fill=(0, 0, 0))
    left, top, right, bottom = config["plot_bbox_pixels"]
    draw.rectangle((left, top, right, bottom), outline=(80, 80, 80), width=1)
    draw.text((left, max(0, top-30)), "SOURCE GRAPH DIGITIZATION / separate frame / white gaps = UNKNOWN", fill=(0, 0, 0))
    canvas.save(out/"source_section_preview.png", compress_level=9)
    payload = {"schema": "vkm.source_section_features/1", "source_config": Path(config_rel).as_posix(),
               "inputs_sha256": inputs, "features": features,
               "coordinate_warning": "Separate metric source frame; no geographic/map or geodetic vertical tie"}
    for name, value in (("source_section_features.json", payload), ("source_section_qa.json", qa)):
        (out/name).write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)+"\n", encoding="utf-8", newline="\n")
    for relative, expected in inputs.items():
        if _sha(_local_path(resources_root, relative)) != expected:
            raise ValueError("Source input changed during digitisation: "+relative)
    if _sha(Path(__file__)) != code_hash:
        raise ValueError("Digitisation code changed during build")
    receipt = {"schema": "vkm.source_section_receipt/1", "status": "PASS" if not qa["geometry"]["invalid_geometry_ids"] else "QA_FAIL",
               "inputs_sha256": inputs, "code_sha256": code_hash,
               "epistemic_status": "DERIVATION", "frame_id": config["frame"]["id"],
               "container_timestamp": "fixed generated metadata for reproducibility; not source time",
               "source_section_gpkg": "source_section.gpkg", "feature_count": len(features), "qa": qa,
               "outputs_sha256": {path.name: _sha(path) for path in sorted(out.iterdir()) if path.is_file()}}
    (out/"receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)+"\n", encoding="utf-8", newline="\n")
    return receipt

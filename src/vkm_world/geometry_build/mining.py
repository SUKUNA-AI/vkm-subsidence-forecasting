"""Reproducible C4 plan/calendar and DMA links, with explicit local frames.

Run with the project's GIS Python runtime. Source coordinates and all scientific
outputs belong in PRIVATE or ignored work directories. This module never infers
an excavated volume from a panel domain and never assigns a vertical datum.
"""
from __future__ import annotations

import argparse
import calendar
import csv
import hashlib
import importlib.util
import io
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path

import numpy as np

CURATED = Path("11_evidence_vnext/canonical/WORLD_PARAMETERS/curated")
MAPS = CURATED / "c4_map"
DMA = Path("08_data_archives/dma_skru1_losev_20261011")
FRAMES = {"skru1": "SKRU1_LOCAL_SH1", "skru2": "SKRU2_LOCAL_SH5", "skru3": "SKRU3_LOCAL_SH1"}
VERSION = "mining_geometry_links_v2"
SCENARIO = "C4_GRATICULE_003_CHECKED_SYMBOLS_V2"


def input_paths(resources_root: Path) -> list[str]:
    """Exact PRIVATE input dependencies, usable before the first build.

    The public formula checker and this module are code dependencies recorded
    separately by SHA-256 in the receipt.
    """
    paths = {Path("11_evidence_vnext/canonical/MINING/spatial_hierarchy.csv"),
             MAPS / "skru1/skru1_boreholes_014_unique.csv",
             MAPS / "skru1/skru1_boreholes_checked.csv",
             MAPS / "skru1/crosswalk_zones.csv",
             MAPS / "skru2/_scripts/map/manual/allotment_003_fig3_17b.csv",
             MAPS / "skru2/_scripts/map/links.py",
             MAPS / "skru2/_scripts/map/manual/graticule_003_fig3_17b.json",
             DMA / "package/data/dma_linked.gpkg", DMA / "package/receipt.json",
             DMA / "original/ДМА_Лосев.csv", DMA / "original/QGIS_DMA2.geojson",
             Path("08_data_archives/osm_controls_20261012/overpass_controls.json")}
    for site in FRAMES:
        paths.update(MAPS / site / f"{site}_{family}.geojson" for family in ("workings", "points", "boundaries"))
        paths.update({CURATED / f"{site}_mining_calendar_20261010.csv", CURATED / f"{site}_calendar_conflicts_20261010.csv"})
        paths.update(p.relative_to(resources_root) for p in (resources_root / MAPS / site / "calibration").glob("*.json"))
    return sorted(p.as_posix() for p in paths)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def semantic_role(layer: str) -> str:
    """The legend's meaning is independent of a filename's year or hatch."""
    if "LAYOUT" in layer:
        return "PANEL_OR_BLOCK_DOMAIN"
    if "REMAINING" in layer:
        return "REMAINING_RESERVES"
    if "PILLAR" in layer:
        return "PROTECTIVE_OR_INTERMINE_PILLAR_DOMAIN"
    if "OFFBALANCE" in layer:
        return "OFFBALANCE_OR_HATCH_OVERLAP_AMBIGUOUS"
    if "DEVELOPMENT_SUSPECT" in layer:
        return "DEVELOPMENT_WORKINGS_CANDIDATE"
    if "WORKED" in layer:
        return "WORKED_AREA_AS_INTERPRETED_BY_SOURCE"
    if "PLANNED" in layer:
        return "PLANNED_AREA"
    if "HATCH" in layer:
        return "HATCH_INTERPRETATION_WITH_UNKNOWN_EPOCH"
    return "UNCLASSIFIED_SOURCE_DOMAIN"


def seam_candidates(text: str | None) -> list[str]:
    """Read explicit seam tokens; ore type alone establishes no seam identity.

    V with its own parenthesized ore qualifier identifies the V variant, while
    generic sylvinite/carnallite descriptions remain unassigned. All raw wording
    is retained by the caller, including named horizons outside this vocabulary.
    """
    text = text or ""
    if not text or text.startswith(("ALL", "UNKNOWN")):
        return []
    seams = []
    if re.search(r"(?<!\w)(?:АБ|AB)(?!\w)", text):
        seams.append("AB")
    if re.search(r"(?<!\w)(?:Кр\s*(?:II|2)|KR2)(?!\w)", text):
        seams.append("KR2")
    if re.search(r"(?<!\w)(?:Кр\s*(?:III|3)|KR3)(?!\w)", text):
        seams.append("KR3")
    qualified_carn = r"(?<!\w)[ВV]\s*\([^)]*карнал[^)]*\)"
    qualified_sylv = r"(?<!\w)[ВV]\s*\([^)]*сильвин[^)]*\)"
    if re.search(r"(?<!\w)(?:Вк|VK)(?!\w)", text) or re.search(qualified_carn, text, re.I):
        seams.append("VK")
    if re.search(r"(?<!\w)(?:Вс|VS)(?!\w)", text) or re.search(qualified_sylv, text, re.I):
        seams.append("VS")
    unqualified = re.sub(qualified_carn, "", text, flags=re.I)
    unqualified = re.sub(qualified_sylv, "", unqualified, flags=re.I)
    # "В (Вс)" is explicitly refined by its own notation, not an alternative V.
    unqualified = re.sub(r"(?<!\w)[ВV]\s*\(\s*(?:Вс|Вк|VS|VK)\s*\)", "", unqualified)
    if re.search(r"(?<!\w)[ВV](?!\w)", unqualified):
        seams.append("V_UNSPECIFIED")
    if re.search(r"(?<!\w)КС(?!\w)", text):
        seams.append("ROCK_SALT_UNSPECIFIED")
    return seams


def source_record_ids(site: str, family: str, features: list[dict]) -> list[str]:
    """Disambiguate repeated source feature IDs without merging observations.

    A content digest is only a source-record discriminator, never evidence that
    two features are one physical object. Exact copies retain occurrence IDs and
    their original source_row. Distinct records keep their IDs after reordering.
    """
    source_ids = [str(f.get("properties", {}).get("id", i)) for i, f in enumerate(features)]
    frequencies = Counter(source_ids)
    seen = Counter()
    result = []
    for source_id, feature in zip(source_ids, features):
        key = f"C4:{site}:{family}:{source_id}"
        if frequencies[source_id] > 1:
            key += ":source:" + hashlib.sha256(_json(feature).encode("utf-8")).hexdigest()
            seen[key] += 1
            if seen[key] > 1:
                key += f":copy:{seen[key]}"
        result.append(key)
    if len(set(result)) != len(result):
        raise ValueError("source observation identifiers collide")
    return result


def mine_identity_relation(record_mine: str | None, event_site: str | None) -> str:
    """Only direct, unambiguous source mine attributes can establish consistency.

    The calendar folder and local coordinate frame are not physical mine labels.
    Shared pillars and unattributed lines remain candidates without being lost.
    """
    exact = {"SKRU1", "SKRU2", "SKRU3"}
    if record_mine in exact and event_site in exact:
        return "CONSISTENT_SOURCE_ATTRIBUTES" if record_mine == event_site else "CONFLICT"
    if not record_mine or not event_site or record_mine == "UNKNOWN" or event_site == "UNKNOWN":
        return "UNKNOWN"
    return "AMBIGUOUS_SOURCE_ATTRIBUTION"


def date_bounds(value: str | None) -> tuple[str | None, str | None]:
    """A partial date is an interval; absent dates remain absent."""
    if not value or not re.fullmatch(r"\d{4}(?:-\d{2})?(?:-\d{2})?", value):
        return None, None
    parts = [int(v) for v in value.split("-")]
    if len(parts) == 1:
        return f"{parts[0]:04}-01-01", f"{parts[0]:04}-12-31"
    if len(parts) == 2:
        last = calendar.monthrange(*parts)[1]
        return f"{value}-01", f"{value}-{last:02}"
    from datetime import date
    date(*parts)
    return value, value


def plan_state(properties: dict) -> dict:
    """Only dates explicitly tied to the plan become dated states."""
    printed = properties.get("state_date_as_printed")
    if printed == "на 01.01.2007":
        return {"state_date": "2007-01-01", "time_status": "SOURCE_STATED_PLAN_SNAPSHOT"}
    if properties.get("date_printed_on_sheet"):
        return {"state_date": "2007-10-01", "time_status": "ENGINEERING_ASSUMPTION_DATE_MEANING"}
    return {"state_date": None, "time_status": "UNKNOWN"}


def affine_inverse(matrix):
    h = np.vstack([np.asarray(matrix, dtype=float), [0, 0, 1]])
    return np.linalg.inv(h)[:2]


def apply_affine(matrix, xy):
    return np.asarray(xy) @ np.asarray(matrix)[:, :2].T + np.asarray(matrix)[:, 2]


def fit_similarity(source, target):
    """Fit four parameters, retain LOO diagnostics and spatial conditioning."""
    source, target = np.asarray(source, dtype=float), np.asarray(target, dtype=float)
    if source.shape != target.shape or source.ndim != 2 or source.shape[1] != 2 or len(source) < 3:
        raise ValueError("At least three paired 2D points required")
    def solve(x, y):
        a = np.zeros((2 * len(x), 4))
        a[0::2] = np.c_[x[:, 0], -x[:, 1], np.ones(len(x)), np.zeros(len(x))]
        a[1::2] = np.c_[x[:, 1], x[:, 0], np.zeros(len(x)), np.ones(len(x))]
        coeff, _, rank, _ = np.linalg.lstsq(a, y.ravel(), rcond=None)
        if rank != 4:
            raise ValueError("Rank deficient control correspondence")
        p, q, tx, ty = coeff
        return np.array([[p, -q, tx], [q, p, ty]])
    matrix = solve(source, target)
    residuals = np.linalg.norm(apply_affine(matrix, source) - target, axis=1)
    loo = []
    for i in range(len(source)):
        keep = np.arange(len(source)) != i
        loo.append(float(np.linalg.norm(apply_affine(solve(source[keep], target[keep]), source[i:i + 1])[0] - target[i])))
    centered = source - source.mean(0)
    return {"M": matrix.tolist(), "n": len(source), "rms_m": float(np.sqrt(np.mean(residuals ** 2))), "residuals_m": residuals.tolist(), "loo_residuals_m": loo, "loo_rms_m": float(np.sqrt(np.mean(np.square(loo)))), "control_extent_m": [source.min(0).tolist(), source.max(0).tolist()], "spatial_rank": int(np.linalg.matrix_rank(centered)), "spatial_condition_number": float(np.linalg.cond(centered)), "independent_test_status": "NOT_AVAILABLE; LOO is internal resampling", "basis": "explicit same-number candidates checked in same plan contexts; source positions are symbol centres"}


def local_to_lonlat(site: str, xy, calibration: dict):
    """Explicit approximate scenario via SKRU-2 and the printed graticule.

    003 uses allotment vertices, never subsidence values. Conversion reproduces
    the source script's pixel->geographic equations with their original factors.
    """
    xy = np.asarray(xy, dtype=float)
    c = calibration["skru2_links"]
    if site == "skru3":
        shift = calibration["skru3_local"]["transform_SKRU3_LOCAL_to_SKRU1_LOCAL"]
        xy = xy + [shift["dx"], shift["dy"]]
    if site != "skru2":
        xy = apply_affine(affine_inverse(c["C_link_skru1"]["fit"]["M"]), xy)
    d = c["D_geo_check_003"]
    en = apply_affine(d["fit"]["M"], xy)
    gr = d["graticule"]
    u = en[..., 0] / d["m_per_px"]["x"]
    v = -en[..., 1] / d["m_per_px"]["y"]
    lon = gr["lon_at_u0"] + (u - gr["u0_px"]) * gr["deg_per_px_lon"]
    lat = gr["lat_at_v0"] - (v - gr["v0_px"]) * gr["deg_per_px_lat"]
    return np.stack([lon, lat], axis=-1)


def build_mining_package(resources_root: Path, out: Path) -> dict:
    from osgeo import gdal, ogr, osr
    from shapely.geometry import shape
    from shapely.strtree import STRtree

    resources_root, out = Path(resources_root).resolve(), Path(out)
    out.mkdir(parents=True, exist_ok=False)
    inventory = {}
    tables = {}
    diagnostics = []

    def read(path, kind="json"):
        full = resources_root / path
        raw = full.read_bytes()
        if raw.startswith(b"version https://git-lfs.github.com/spec/v1"):
            raise ValueError(f"Unloaded LFS input: {path.as_posix()}")
        inventory[path.as_posix()] = {"path": "private:" + path.as_posix(), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        if kind == "bytes":
            return raw
        if kind == "csv":
            return list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline="")))
        return json.loads(raw.decode("utf-8-sig"))

    for dependency in input_paths(resources_root):
        read(Path(dependency), "bytes")

    def dump(name, value):
        (out / name).write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    def table(name, rows):
        tables[name] = rows
        with (out / (name + ".jsonl")).open("w", encoding="utf-8", newline="\n") as f:
            for row in rows:
                f.write(_json(row) + "\n")

    frame_files = {"skru1": "local_system.json", "skru2": "skru2_local_system.json", "skru3": "local_system_skru3.json"}
    frames = []
    all_calibration = []
    for site in FRAMES:
        folder = MAPS / site / "calibration"
        for path in sorted((resources_root / folder).glob("*.json")):
            rel = path.relative_to(resources_root)
            payload = read(rel)
            all_calibration.append({"source_path": "private:" + rel.as_posix(), "source_sha256": inventory[rel.as_posix()]["sha256"], "payload": payload})
        frames.append({"frame_id": FRAMES[site], "mine": site.upper(), "units": "m", "axes": ["east", "north"], "horizontal_crs": None, "vertical_crs": None, "source": "private:" + (folder / frame_files[site]).as_posix(), "status": "DERIVATION", "z_status": "UNKNOWN"})
    calibration = {"skru2_links": read(MAPS / "skru2/calibration/skru2_links_and_checks.json"), "skru3_local": read(MAPS / "skru3/calibration/local_system_skru3.json")}
    unique_path = MAPS / "skru1/skru1_boreholes_014_unique.csv"
    unique = read(unique_path, "csv")
    skru2_points = read(MAPS / "skru2/skru2_points.geojson")["features"]
    old_link = calibration["skru2_links"]["C_link_skru1"]
    pairs = []
    for label, source_name in old_link["sources_skru2"].items():
        match1 = [r for r in unique if r["label_checked"] == label]
        match2 = [f for f in skru2_points if f["properties"].get("label_as_printed") == label and f["properties"].get("source_id") == ("VKM-SRC-064" if source_name == "F57" else "VKM-SRC-012")]
        if len(match1) != 1 or len(match2) != 1:
            raise ValueError(f"Ambiguous corrected control: {label}")
        pairs.append({"label": label, "source_record_skru1": match1[0]["key"], "source_record_skru2": match2[0]["properties"]["id"], "source_xy_m": match2[0]["geometry"]["coordinates"], "target_xy_m": [float(match1[0]["x_local_m"]), float(match1[0]["y_local_m"])]})
    corrected_fit = fit_similarity([p["source_xy_m"] for p in pairs], [p["target_xy_m"] for p in pairs])
    corrected_link = {"fit": corrected_fit, "control_pairs": pairs, "status": "DERIVATION", "interpretation_id": "C4_SYMBOL_LINK_CHECKED_V2", "note": "old link used auto point candidates now known to be text; V2 uses checked symbol overlay; source datum, scale and north uncertainty remain"}
    calibration["skru2_links"]["C_link_skru1_legacy"] = old_link
    calibration["skru2_links"]["C_link_skru1"] = corrected_link
    dump("corrected_skru2_skru1_link.json", {"current": corrected_link, "superseded": old_link})
    # The control table proves the fit uses allotment vertices, not response data.
    controls = read(MAPS / "skru2/_scripts/map/manual/allotment_003_fig3_17b.csv", "csv")
    read(MAPS / "skru2/_scripts/map/links.py", "bytes")
    read(MAPS / "skru2/_scripts/map/manual/graticule_003_fig3_17b.json")
    f57_matrix = read(MAPS / "skru2/calibration/skru2_f57_to_local.json")["M"]
    d = calibration["skru2_links"]["D_geo_check_003"]
    d_source, d_target, d_pairs = [], [], []
    for control in controls:
        ref = control["f57_ref"]
        if ref.startswith(("B", "S")):
            candidates = [f for f in skru2_points if f["properties"]["id"] == "SKRU2-PT-064-" + ref]
            if len(candidates) != 1:
                raise ValueError(f"Missing F57 control: {ref}")
            uv = candidates[0]["properties"]["frame_uv"]
        else:
            uv = list(map(float, ref.split(";")))
        src = apply_affine(f57_matrix, [uv])[0].tolist()
        dst = [float(control["u"]) * d["m_per_px"]["x"], -float(control["v"]) * d["m_per_px"]["y"]]
        d_source.append(src); d_target.append(dst)
        d_pairs.append({"id": ref, "source_xy_m": src, "target_image_en_m": dst, "source_image_uv": uv, "target_image_uv": [float(control["u"]), float(control["v"])]})
    d_check = fit_similarity(d_source, d_target)
    dump("registration_quality.json", {"003_graticule_fit": d_check, "003_controls": d_pairs, "max_matrix_difference_vs_source": float(np.max(np.abs(np.array(d_check["M"]) - np.array(d["fit"]["M"])))), "corrected_crossmine_fit": corrected_fit, "independence": "003 compares allotment-boundary vertices; LOO is internal. OSM shaft test below uses different objects, but underlying map provenance can be dependent.", "vertical_control_status": "UNKNOWN", "datum_status": "graticule datum unnamed; WGS84 assigned only to this approximate scenario as ENGINEERING_ASSUMPTION"})
    table("calibration_source_records", all_calibration)
    dump("frames.json", frames)
    dump("transforms.json", {"version": VERSION, "scenario_id": SCENARIO, "status": "DERIVATION", "horizontal_accuracy": "planimetry approximate; 003 fit RMS69m LOO90m max208m; cross-mine linkage adds uncertainty and extrapolation", "vertical_tie": None, "source_controls": controls, "steps": [{"from": "SKRU3_LOCAL_SH1", "to": "SKRU1_LOCAL_SH1", "kind": "translation_m", "parameters": calibration["skru3_local"]["transform_SKRU3_LOCAL_to_SKRU1_LOCAL"]}, {"from": "SKRU1_LOCAL_SH1", "to": "SKRU2_LOCAL_SH5", "kind": "inverse_affine_m", "matrix": affine_inverse(corrected_fit["M"]).tolist(), "source_forward_fit": corrected_link}, {"from": "SKRU2_LOCAL_SH5", "to": "SRC003_IMAGE_EN_M", "kind": "similarity_m", "fit": calibration["skru2_links"]["D_geo_check_003"]}, {"from": "SRC003_IMAGE_EN_M", "to": "CRS84_SCENARIO", "kind": "inverse_image_scale_then_printed_graticule", "axes": ["longitude_deg", "latitude_deg"]}, {"from": "CRS84_SCENARIO", "to": "EPSG:32640", "kind": "PROJ_WGS84_UTM40N", "axes": ["easting_m", "northing_m"]}], "alternatives": {"skru2_legacy_auto_candidates": old_link, "skru2_via_grid_labels": calibration["skru2_links"]["C2_link_skru1_via_grids"], "skru2_shift_only": old_link["variant_shift_only"]}, "all_other_calibration_and_independent_checks": "calibration_source_records.jsonl"})

    ogr.UseExceptions()
    gpkg = ogr.GetDriverByName("GPKG").CreateDataSource(str(out / "mining.gpkg"))
    geographic = osr.SpatialReference(); geographic.ImportFromEPSG(4326); geographic.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    utm = osr.SpatialReference(); utm.ImportFromEPSG(32640); utm.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    to_utm = osr.CoordinateTransformation(geographic, utm)
    fields = ["record_id", "source_feature_id", "mine", "mine_context", "mine_attribution_status", "frame_id", "semantic_role", "seam", "state_date", "time_status", "interpretation_id", "source_path", "source_sha256", "source_row", "raw_properties_json"]

    def layer(name, srs):
        obj = gpkg.CreateLayer(name, srs, ogr.wkbUnknown)
        for col in fields:
            obj.CreateField(ogr.FieldDefn(col, ogr.OFTString))
        return obj

    def add(layer_, record, geom):
        f = ogr.Feature(layer_.GetLayerDefn())
        for key in fields:
            value = record.get(key)
            if value is not None:
                f.SetField(key, _json(value) if isinstance(value, (dict, list)) else str(value))
        f.SetGeometry(geom)
        layer_.CreateFeature(f)

    def scene_geometry(site, geometry):
        g = geometry.Clone()
        def rec(q):
            if q.GetGeometryCount():
                for i in range(q.GetGeometryCount()): rec(q.GetGeometryRef(i))
            else:
                coords = [(q.GetX(i), q.GetY(i)) for i in range(q.GetPointCount())]
                if not coords: return
                for i, (lon, lat) in enumerate(local_to_lonlat(site, coords, calibration)):
                    x, y, _ = to_utm.TransformPoint(float(lon), float(lat))
                    q.SetPoint_2D(i, x, y)
        rec(g)
        return g

    records, object_links, point_corrections, scenario_panels, shaft_records = [], [], [], [], []
    for site, frame in FRAMES.items():
        local_srs = osr.SpatialReference(); local_srs.SetLocalCS(frame); local_srs.SetLinearUnits("metre", 1)
        for family in ("workings", "points", "boundaries"):
            source_path = MAPS / site / f"{site}_{family}.geojson"
            features = read(source_path)["features"]
            observation_ids = source_record_ids(site, family, features)
            local_layer, scene_layer = layer(f"{site}_{family}_local_source", local_srs), layer(f"{site}_{family}_utm_scenario", utm)
            dated_layer = layer(f"{site}_{family}_source_dated", local_srs) if family == "workings" else None
            for index, feature in enumerate(features):
                p = feature["properties"]
                source_id = p.get("id", str(index))
                auto_bad = site == "skru1" and family == "points" and "-BHA-" in source_id
                role = semantic_role(p.get("layer", "")) if family == "workings" else p.get("kind", family)
                seams = seam_candidates(p.get("seam"))
                record = {"record_id": observation_ids[index], "source_feature_id": source_id, "mine": p.get("mine") or p.get("site_norm") or None, "mine_context": site.upper(), "mine_attribution_status": "SOURCE_ATTRIBUTE" if p.get("mine") or p.get("site_norm") else "UNKNOWN_PLAN_CONTEXT_ONLY", "frame_id": frame, "semantic_role": role, "seam": seams[0] if len(seams) == 1 else None, "seam_candidates": seams, "seam_status": "SOURCE_INTERPRETATION" if len(seams) == 1 else "AMBIGUOUS" if seams else "UNKNOWN", **plan_state(p), "interpretation_id": "SOURCE_LOCAL_V1", "source_path": "private:" + source_path.as_posix(), "source_sha256": inventory[source_path.as_posix()]["sha256"], "source_row": index, "raw_properties_json": p, "geometry_z": None, "geometry_status": "DERIVATION", "review_status": "AUTO_EXTRACTED_UNREVIEWED", "usable_symbol_position": not auto_bad}
                records.append(record)
                geom = ogr.CreateGeometryFromJson(_json(feature["geometry"]))
                source_shape = shape(feature["geometry"])
                record["geometry_valid"] = source_shape.is_valid
                if not source_shape.is_valid:
                    from shapely.validation import explain_validity
                    diagnostics.append({"record_id": record["record_id"], "result": "FAILED_SOURCE_GEOMETRY_VALIDITY", "reason": explain_validity(source_shape), "repair": "NOT_RUN; source preserved, no physical interpretation of repair asserted", "solid_or_volume_use": "BLOCKED"})
                if p.get("kind") == "SHAFT":
                    shaft_records.append((site, record, feature["geometry"]["coordinates"]))
                add(local_layer, record, geom)
                if not auto_bad:
                    scene_geom = scene_geometry(site, geom)
                    add(scene_layer, {**record, "frame_id": "EPSG:32640", "interpretation_id": SCENARIO}, scene_geom)
                    if site == "skru1" and role == "PANEL_OR_BLOCK_DOMAIN":
                        scenario_panels.append((record, shape(json.loads(scene_geom.ExportToJson()))))
                else:
                    point_corrections.append({"source_record_id": record["record_id"], "status": "EXCLUDED_AUTO_SYMBOL_COORDINATE", "reason": "existing source review detects labels/false positives; use checked unique-symbol overlay"})
                if dated_layer is not None and record["state_date"] is not None:
                    add(dated_layer, record, geom)
                for object_id in filter(None, p.get("object_id", "").split(";")):
                    object_links.append({"record_id": record["record_id"], "object_id": object_id, "status": "SOURCE_INTERPRETATION", "basis": "explicit input object_id; not a new name match"})
                for object_id in p.get("related_object_ids", []):
                    object_links.append({"record_id": record["record_id"], "object_id": object_id, "status": "CANDIDATE", "basis": p.get("related_basis")})
    read(MAPS / "skru1/skru1_boreholes_checked.csv", "csv")
    checked_local = osr.SpatialReference(); checked_local.SetLocalCS(FRAMES["skru1"]); checked_local.SetLinearUnits("metre", 1)
    checked = layer("skru1_boreholes_checked_local", checked_local)
    checked_scene = layer("skru1_boreholes_checked_utm_scenario", utm)
    for row in unique:
        point = ogr.Geometry(ogr.wkbPoint); point.AddPoint_2D(float(row["x_local_m"]), float(row["y_local_m"]))
        rec = {"record_id": "C4:skru1:well_symbol:" + row["key"], "source_feature_id": row["key"], "mine": "SKRU1_PLAN_CONTEXT", "frame_id": FRAMES["skru1"], "semantic_role": "CHECKED_WELL_SYMBOL", "source_path": "private:" + unique_path.as_posix(), "source_sha256": inventory[unique_path.as_posix()]["sha256"], "raw_properties_json": row}
        add(checked, rec, point); add(checked_scene, {**rec, "frame_id": "EPSG:32640", "interpretation_id": SCENARIO}, scene_geometry("skru1", point))
    table("checked_well_symbols", unique)
    table("point_exclusions", point_corrections)
    table("plan_records", records)
    table("record_object_links", object_links)
    table("zone_crosswalk_source", read(MAPS / "skru1/crosswalk_zones.csv", "csv"))
    table("entities", read(Path("11_evidence_vnext/canonical/MINING/spatial_hierarchy.csv"), "csv"))
    events, conflicts, event_links = [], [], []
    for site in FRAMES:
        source_path = CURATED / f"{site}_mining_calendar_20261010.csv"
        rows = read(source_path, "csv")
        for index, row in enumerate(rows):
            start, _ = date_bounds(row["date_start_iso"])
            _, end = date_bounds(row["date_end_iso"] or row["date_start_iso"])
            event_id = f"C4EVENT:{site}:{row['row_id']}"
            events.append({"event_id": event_id, "event_type": row["event"], "calendar_mine": site.upper(), "source_site": row["site_norm"], "event_start_min": start, "event_end_max": end, "time_precision": row["precision"], "available_from": None, "available_from_status": "UNKNOWN", "planned_or_actual": row["planned_or_actual"] or "UNKNOWN", "seam_candidates": seam_candidates(row["seam"]), "source_path": "private:" + source_path.as_posix(), "source_sha256": inventory[source_path.as_posix()]["sha256"], "source_row": index, "source_record": row, "status": "DERIVATION", "review_status": "AUTO_EXTRACTED_UNREVIEWED"})
            event_links.extend({"event_id": event_id, "object_id": obj, "status": "SOURCE_INTERPRETATION"} for obj in row["object_id"].split(";") if obj)
        conflicts.extend({"calendar_mine": site.upper(), "source_record": row} for row in read(CURATED / f"{site}_calendar_conflicts_20261010.csv", "csv"))
    table("events", events); table("event_object_links", event_links); table("calendar_conflicts", conflicts)
    event_by_object = {}
    for event_link in event_links:
        event_by_object.setdefault(event_link["object_id"], []).append(event_link["event_id"])
    event_lookup = {event["event_id"]: event for event in events}
    record_lookup = {record["record_id"]: record for record in records}
    record_events = []
    for link in object_links:
        if link["status"] != "SOURCE_INTERPRETATION":
            continue
        for event_id in event_by_object.get(link["object_id"], []):
            record = record_lookup[link["record_id"]]
            event = event_lookup[event_id]
            rs, es = set(record["seam_candidates"]), set(event["seam_candidates"])
            seam_relation = "UNKNOWN" if not rs or not es else "COMPATIBLE_CANDIDATES" if rs & es else "CONFLICT"
            mine_relation = mine_identity_relation(record["mine"], event["source_site"])
            identity_status = "EXPLICIT_SOURCE_OBJECT_ID_JOIN" if mine_relation == "CONSISTENT_SOURCE_ATTRIBUTES" else "SOURCE_OBJECT_ID_CANDIDATE_" + mine_relation
            record_events.append({"record_id": link["record_id"], "event_id": event_id, "object_id": link["object_id"], "identity_status": identity_status, "mine_relation": mine_relation, "record_mine": record["mine"], "event_source_site": event["source_site"], "usable_as_established_identity": False, "seam_relation": seam_relation, "date_start_min": event["event_start_min"], "date_end_max": event["event_end_max"], "planned_or_actual": event["planned_or_actual"], "geometry_role": record["semantic_role"], "excavated_geometry_established": False, "basis": "same explicit object_id in existing source interpretations; mine ambiguity retained; no name or centroid identity matching; source join is not accepted physical identity"})
    table("record_event_links", record_events)
    # Preserve DMA's existing record/FID crosswalk, do not derive time from variants.
    dma_path = DMA / "package/data/dma_linked.gpkg"
    read(dma_path, "bytes")
    con = sqlite3.connect(f"file:{(resources_root / dma_path).as_posix()}?mode=ro", uri=True); con.row_factory = sqlite3.Row
    links = [dict(row) for row in con.execute("select * from links order by fid")]
    table("dma_record_crosswalk", [{**row, "mine": "SKRU1", "object_id": None, "seam": None, "state_date": None, "identity_status": "UNKNOWN", "basis_mining_identity": "input has no mine-object/seam/time fields; multiple values retained"} for row in links])
    for name in ("csv_rows", "value_records", "source_features", "qa_issues"):
        table("dma_" + name, [dict(row) for row in con.execute(f"select * from {name} order by fid")])
    con.close()
    candidate_rows = []
    tree = STRtree([g for _, g in scenario_panels])
    ds = ogr.Open(str(resources_root / dma_path)); contours = ds.GetLayerByName("contours")
    for f in contours:
        poly = shape(json.loads(f.GetGeometryRef().ExportToJson()))
        for idx in tree.query(poly, predicate="intersects"):
            rec, candidate = scenario_panels[int(idx)]
            area = poly.intersection(candidate).area
            if area > 1:
                candidate_rows.append({"contour_id": f.GetField("geom_id"), "plan_record_id": rec["record_id"], "overlap_m2": area, "contour_overlap_fraction": area / poly.area, "identity_status": "CANDIDATE_ONLY", "relation": "SPATIAL_OVERLAP_IN_APPROXIMATE_SCENARIO", "seam": None, "time": None, "interpretation_id": SCENARIO})
    ds = None
    table("dma_plan_spatial_candidates", candidate_rows)
    osm = read(Path("08_data_archives/osm_controls_20261012/overpass_controls.json"))
    shaft_checks = []
    for site, record, xy in shaft_records:
        if site not in ("skru1", "skru3"):
            continue
        lon, lat = local_to_lonlat(site, [xy], calibration)[0]
        x, y, _ = to_utm.TransformPoint(float(lon), float(lat))
        for node in osm["elements"]:
            tags = node.get("tags", {})
            if "lon" not in node or "Шахтный ствол" not in tags.get("name", ""):
                continue
            if (site == "skru1") != ("СКРУ-1" in tags.get("operator", "")):
                continue
            xx, yy, _ = to_utm.TransformPoint(node["lon"], node["lat"])
            name = record["raw_properties_json"].get("label_as_printed", "")
            explicit = site == "skru1" and "2" in name and ("бис" in name.lower() or "bis" in name.lower()) and "2БИС" in tags.get("name", "").upper()
            shaft_checks.append({"plan_record_id": record["record_id"], "plan_label": name, "osm_id": f"{node['type']}/{node['id']}", "osm_name": tags.get("name"), "distance_m": float(np.hypot(x - xx, y - yy)), "identity_status": "EXPLICIT_SHAFT_NAME_CANDIDATE" if explicit else "UNKNOWN_NUMBER_CANDIDATE", "used_as_fit_control": False, "status": "DERIVATION", "interpretation_id": SCENARIO})
    table("shaft_external_checks", shaft_checks)
    # Existing checker is the single implementation of the author-index identities.
    repo = Path(__file__).resolve().parents[3]
    checker = repo / "scripts/check_dma_formulas.py"
    spec = importlib.util.spec_from_file_location("dma_formula_checker", checker)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    csv_path = DMA / "original/ДМА_Лосев.csv"
    read(csv_path, "bytes")
    hdr, dma_rows, _ = mod.load(resources_root / csv_path)
    table("dma_formula_checks", mod.check(dma_rows))
    table("dma_field_dictionary", [dict(zip(mod.DICT_COLS, d)) for d in mod.DICTIONARY])
    dump("dma_formula_semantics.json", {"author_values_unchanged": True, "checker_code_sha256": _sha(checker), "implied_values": mod.implied(dma_rows), "normalization_direction": "Disp decreases with subsidence; source names min/max not relabelled", "physical_min_max_mapping": {"min_Disp": "max_subsidence_if_inverse_is_valid", "max_Disp": "min_subsidence_if_inverse_is_valid"}, "clipping": "values at clip are lower bounds on original subsidence, not recovered values", "mean_warning": "inverse of mean(clipped transformed field) is not mean(original subsidence)", "physical_load_assignment": "PROHIBITED: dimensionless author indices have no pressure/force unit", "dependency": "Disp and Disp*mera depend on subsidence-derived raster; not independent causal inputs", "available_from": None})
    gpkg = None
    with sqlite3.connect(out / "mining.gpkg") as con:
        con.execute("update gpkg_contents set last_change='2000-01-01T00:00:00.000Z'")
        for name, rows in tables.items():
            con.execute(f'create table "{name}" (row_no integer primary key, payload_json text not null)')
            con.executemany(f'insert into "{name}" values (?,?)', [(i, _json(row)) for i, row in enumerate(rows)])
            con.execute("insert into gpkg_contents(table_name,data_type,identifier,last_change,srs_id) values (?,?,?,'2000-01-01T00:00:00.000Z',0)", (name, "attributes", name))
    con.close()  # A sqlite context commits; it does not close the Windows file handle.
    counts = {name: len(rows) for name, rows in tables.items()}
    dump("inventory.json", list(inventory.values()))
    dump("diagnostics.json", {"counts": counts, "plan_roles": dict(Counter(r["semantic_role"] for r in records)), "temporal_states": dict(Counter(r["time_status"] for r in records)), "unknowns": ["vertical tie and all Z", "DMA contour->physical mine object->seam->time", "absolute georeferencing independent accuracy", "excavated/backfill volume geometry", "availability date of calendar knowledge"], "geometry_policy": "source geometry preserved, no repairs or panel extrusion", "diagnostics": diagnostics})
    output_hashes = {p.name: _sha(p) for p in sorted(out.iterdir()) if p.is_file()}
    receipt = {"version": VERSION, "status": "DERIVATION_WITH_EXPLICIT_UNKNOWNS", "scientific_acceptance": "NOT_CHECKED", "code_sha256": _sha(Path(__file__)), "gdal": gdal.VersionInfo(), "counts": counts, "inputs": list(inventory.values()), "outputs_sha256": output_hashes, "steps": [{"operation": name, "exit_code": 0, "records": value} for name, value in counts.items()], "rebuild": "python -m vkm_world.geometry_build.mining --resources-root $VKM_RESOURCES_ROOT --out $VKM_WORK/mining"}
    dump("receipt.json", receipt)
    return receipt


def main():
    import os
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resources-root", type=Path, default=os.environ.get("VKM_RESOURCES_ROOT"), required=not bool(os.environ.get("VKM_RESOURCES_ROOT")))
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(_json(build_mining_package(args.resources_root, args.out)["counts"]))


if __name__ == "__main__":
    main()

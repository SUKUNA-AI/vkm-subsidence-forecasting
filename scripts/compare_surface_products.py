"""Diagnose versioned terrain products at existing height-audit points; never replace a scientific DSM.

Use a PRIVATE/ignored JSON configuration with relative paths and explicit datum/scale policies.
Run with the existing QGIS Python (GDAL + NumPy + pyproj). Inputs and output hashes are recorded.
ATL08 quality, segment support and epoch limitations are inherited, not upgraded by a small residual.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import platform
from pathlib import Path

import numpy as np


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def rooted_path(value: str, roots: dict[str, Path]) -> Path:
    """ROOT:relative/path, never an embedded machine path or a path escaping its declared root."""
    name, sep, relative = value.partition(":")
    if not sep or name not in roots:
        raise ValueError(f"expected a declared ROOT:relative/path: {value}")
    rel = Path(relative)
    if rel.is_absolute() or rel.drive:
        raise ValueError("input paths must be relative to their declared root")
    target = (roots[name] / rel).resolve()
    if not target.is_relative_to(roots[name].resolve()):
        raise ValueError("input path escapes its declared root")
    if not target.is_file():
        raise FileNotFoundError(value)
    return target


def pixel_center_indices(x: float, y: float, gt) -> tuple[float, float]:
    """GDAL geotransforms refer to pixel corners, including PixelIsPoint after GDAL's RFC33 handling."""
    gt = np.asarray(gt, dtype=float)
    if gt.shape != (6,) or not np.all(np.isfinite(gt)) or not np.isfinite([x, y]).all():
        raise ValueError("finite coordinates and six geotransform coefficients are required")
    linear = np.array([[gt[1], gt[2]], [gt[4], gt[5]]])
    if abs(np.linalg.det(linear)) == 0:
        raise ValueError("singular raster geotransform")
    col, row = np.linalg.solve(linear, np.array([x - gt[0], y - gt[3]])) - .5
    return float(col), float(row)


def bilinear_window(values, dx: float, dy: float, nodata=None) -> float | None:
    values = np.asarray(values, dtype=float)
    if values.shape != (2, 2) or not 0 <= dx <= 1 or not 0 <= dy <= 1:
        raise ValueError("bilinear sampling requires a 2x2 window and fractional indices in [0,1]")
    weights = np.array([[(1-dx)*(1-dy), dx*(1-dy)], [(1-dx)*dy, dx*dy]])
    active = weights > 1e-14
    valid = np.isfinite(values)
    if nodata is not None:
        valid &= values != nodata
    if not np.all(valid[active]):
        return None
    return float(np.sum(values[active] * weights[active]))


def physical_value(value: float | None, scale: float, offset: float, policy: str) -> float | None:
    if policy not in ("metadata", "raw_as_m_scenario"):
        raise ValueError(f"unsupported scale policy: {policy}")
    if not np.isfinite([scale, offset]).all() or scale == 0:
        raise ValueError("scale/offset must be finite and scale nonzero")
    if value is None:
        return None
    if not np.isfinite(value):
        raise ValueError("nonfinite raster value")
    return float(value * scale + offset) if policy == "metadata" else float(value)


def residual_stats(values) -> dict:
    a = np.asarray(list(values), dtype=float)
    if not np.all(np.isfinite(a)):
        raise ValueError("residuals must be finite; missing values are excluded explicitly")
    if not len(a):
        return {"n": 0, "bias_m": None, "mae_m": None, "rmse_m": None}
    return {"n": len(a), "bias_m": float(a.mean()), "mae_m": float(np.abs(a).mean()),
            "rmse_m": float(np.sqrt(np.mean(a*a))), "median_m": float(np.median(a)),
            "p05_m": float(np.percentile(a, 5)), "p95_m": float(np.percentile(a, 95))}


class RasterSampler:
    def __init__(self, path: Path, spec: dict):
        from osgeo import gdal, osr
        self.dataset = gdal.Open(str(path), gdal.GA_ReadOnly)
        if self.dataset is None:
            raise ValueError(f"cannot open raster {spec['id']}")
        self.band = self.dataset.GetRasterBand(1)
        self.gt = self.dataset.GetGeoTransform()
        self.width, self.height = self.dataset.RasterXSize, self.dataset.RasterYSize
        if self.width < 2 or self.height < 2:
            raise ValueError("raster must have at least 2x2 pixels")
        self.nodata = self.band.GetNoDataValue()
        self.scale = self.band.GetScale()
        self.scale = 1. if self.scale is None else self.scale
        self.offset = self.band.GetOffset()
        self.offset = 0. if self.offset is None else self.offset
        self.spec = spec
        if spec["scale_policy"] == "raw_as_m_scenario" and not spec.get("scale_override_basis"):
            raise ValueError("raw-as-metres scenario requires an explicit scale_override_basis")
        source = osr.SpatialReference(); source.ImportFromEPSG(4326)
        target = osr.SpatialReference(); target.ImportFromWkt(self.dataset.GetProjection())
        source.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        target.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        self.transform = osr.CoordinateTransformation(source, target)
        self.metadata = {"size": [self.width, self.height], "geotransform": list(self.gt),
                         "projection": self.dataset.GetProjection(), "metadata": self.dataset.GetMetadata(),
                         "band_scale": self.scale, "band_offset": self.offset, "nodata": self.nodata,
                         "scale_policy": spec["scale_policy"], "height_datum": spec["height_datum"],
                         "epoch": spec.get("epoch", "UNKNOWN"), "surface_role": spec["surface_role"],
                         "admission": spec["admission"]}

    def sample(self, lon: float, lat: float, method="bilinear") -> float | None:
        x, y, _ = self.transform.TransformPoint(lon, lat)
        c, r = pixel_center_indices(x, y, self.gt)
        if not (0 <= c <= self.width-1 and 0 <= r <= self.height-1):
            return None
        if method == "nearest":
            a = self.band.ReadAsArray(int(math.floor(c+.5)), int(math.floor(r+.5)), 1, 1)
            raw = float(a[0, 0])
            if not np.isfinite(raw) or (self.nodata is not None and raw == self.nodata):
                return None
        elif method == "bilinear":
            c0, r0 = min(int(math.floor(c)), self.width-2), min(int(math.floor(r)), self.height-2)
            raw = bilinear_window(self.band.ReadAsArray(c0, r0, 2, 2), c-c0, r-r0, self.nodata)
        else:
            raise ValueError(f"unsupported raster sampling method: {method}")
        return physical_value(raw, self.scale, self.offset, self.spec["scale_policy"])


def run(config_path: Path, out: Path, roots: dict[str, Path]) -> dict:
    from osgeo import gdal
    from pyproj import Geod
    gdal.UseExceptions()
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    out.mkdir(parents=True, exist_ok=True)
    if config.get("intended_use") != "DIAGNOSTIC_ONLY":
        raise ValueError("this script does not admit validation or automatic surface replacement")
    points_path = rooted_path(config["points_csv"], roots)
    collars_path = rooted_path(config["collars_json"], roots)
    points = list(csv.DictReader(points_path.open(encoding="utf-8-sig")))
    if not points:
        raise ValueError("height-audit points are empty")
    if len({p["point_id"] for p in points}) != len(points):
        raise ValueError("duplicate observation point_id")
    inputs = {config["points_csv"]: sha256(points_path), config["collars_json"]: sha256(collars_path)}
    for basis in config.get("metadata_bases", []):
        inputs[basis] = sha256(rooted_path(basis, roots))
    samplers = {}
    masks = {}
    for spec in config["products"]:
        if spec["id"] in samplers:
            raise ValueError("duplicate product id")
        path = rooted_path(spec["path"], roots)
        inputs[spec["path"]] = sha256(path)
        samplers[spec["id"]] = RasterSampler(path, spec)
        if spec.get("mask_path"):
            mask_path = rooted_path(spec["mask_path"], roots)
            inputs[spec["mask_path"]] = sha256(mask_path)
            mask_spec = dict(id=spec["id"]+"_MASK", scale_policy="metadata", height_datum="NOT_APPLICABLE",
                             surface_role="MASK", admission="DIAGNOSTIC_MASK")
            masks[spec["id"]] = RasterSampler(mask_path, mask_spec)
    rows = []
    for p in points:
        lon, lat = float(p["lon"]), float(p["lat"])
        reference = float(p["H_egm2008_m"]) if p.get("H_egm2008_m") else None
        if not np.isfinite([lon, lat]).all() or reference is not None and not np.isfinite(reference):
            raise ValueError("audit contains nonfinite coordinates or height")
        for key, sampler in samplers.items():
            value = sampler.sample(lon, lat)
            nearest = sampler.sample(lon, lat, "nearest")
            comparable = sampler.spec["height_datum"] == "EGM2008" and reference is not None
            admitted = p["diagnostic_admission"].startswith("ADMITTED")
            mask = masks[key].sample(lon, lat, "nearest") if key in masks else None
            mask_missing = key in masks and mask is None
            mask_excluded = mask_missing or mask in sampler.spec.get("mask_exclusion_codes", [])
            row = {"point_id": p["point_id"], "product": key, "date": p["date"], "beam": p["beam"],
                   "canopy_class": p["canopy_class"], "slope_deg": p["slope_deg"],
                   "audit_diagnostic_admission": p["diagnostic_admission"], "audit_rejection_reasons": p["rejection_reasons"],
                   "audit_wbm": p.get("wbm"), "audit_wbm_any_r50": p.get("wbm_any_r50"),
                   "product_mask_code": mask, "product_mask_excluded": mask_excluded,
                   "product_mask_missing": mask_missing,
                   "named_rgt": next((s for s in p["point_id"].split("_") if s.startswith("RGT")), "UNKNOWN"),
                   "standing_caveats": p["standing_caveats"], "lon": lon, "lat": lat,
                   "H_reference_EGM2008_m": reference, "bilinear_m": value, "nearest_m": nearest,
                   "nearest_minus_bilinear_m": None if value is None or nearest is None else nearest-value,
                   "residual_product_minus_reference_m": value-reference if comparable and value is not None else None,
                   "score_included": bool(comparable and value is not None and admitted and not mask_excluded),
                   "product_admission": sampler.spec["admission"], "surface_role": sampler.spec["surface_role"],
                   "height_datum": sampler.spec["height_datum"], "scale_policy": sampler.spec["scale_policy"],
                   "comparison_status": "DIAGNOSTIC_NOT_VALIDATION" if comparable else "BLOCKED_VERTICAL_DATUM_OPERATION",
                   "collar_control_admitted": False}
            rows.append(row)
    fieldnames = list(rows[0]) if rows else []
    with (out/"point_comparisons.csv").open("w", newline="", encoding="utf8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames); writer.writeheader(); writer.writerows(rows)
    scores = {}
    for key, sampler in samplers.items():
        eligible = [r for r in rows if r["product"] == key and r["score_included"]]
        def stat(subset):
            return residual_stats(r["residual_product_minus_reference_m"] for r in subset)
        groups = {r["date"]+":"+r["beam"] for r in eligible}
        scores[key] = {"all_inherited_admitted": stat(eligible),
                       "by_canopy": {c: stat([r for r in eligible if r["canopy_class"] == c])
                                     for c in sorted({r["canopy_class"] for r in eligible})},
                       "by_date": {d: stat([r for r in eligible if r["date"] == d])
                                   for d in sorted({r["date"] for r in eligible})},
                       "open_ground_slope_lt5": stat([r for r in eligible if r["canopy_class"] in ("NO_CANOPY_VALUE", "LT2")
                                                      and r["slope_deg"] and float(r["slope_deg"]) < 5]),
                       "campaign_beam_groups": sorted(groups), "n_campaign_beam_groups": len(groups),
                       "named_rgts": sorted({r["named_rgt"] for r in eligible}),
                       "independent_tracks": "NOT_ESTABLISHED; campaign/beam groups are not independent measurements",
                       "admission": sampler.spec["admission"]}
    baseline = config["baseline_product"]
    if baseline not in samplers:
        raise ValueError("baseline product not configured")
    indexed = {(r["product"], r["point_id"]): r for r in rows}
    paired = {}
    for key in samplers:
        common = [(indexed[(baseline, p["point_id"])], indexed[(key, p["point_id"])]) for p in points]
        common = [(b, q) for b, q in common if b["score_included"] and q["score_included"]]
        paired[key] = {"common_point_ids": [b["point_id"] for b, _ in common],
                       "baseline": residual_stats(b["residual_product_minus_reference_m"] for b, _ in common),
                       "alternative": residual_stats(q["residual_product_minus_reference_m"] for _, q in common),
                       "alternative_minus_baseline_height_m": residual_stats(q["bilinear_m"]-b["bilinear_m"] for b, q in common)}
    collar_source = json.loads(collars_path.read_text(encoding="utf-8-sig"))
    collar_points = [r for r in collar_source["supports"] if r.get("physical_id") in config["collar_ids"]]
    collar_points += [r for r in collar_source["independent_references"] if r.get("id") in config["collar_ids"]]
    geod = Geod(ellps="WGS84")
    collars = []
    for p in collar_points:
        pid = p.get("physical_id", p.get("id")); lon, lat = p["lon"], p["lat"]
        ref = p.get("source_value_m")
        for key, sampler in samplers.items():
            center = sampler.sample(lon, lat)
            mask = masks[key].sample(lon, lat, "nearest") if key in masks else None
            rings = {}
            for radius in config["sensitivity_radii_m"]:
                values = []
                for bearing in np.arange(0., 360., 22.5):
                    lo, la, _ = geod.fwd(lon, lat, bearing, radius)
                    value = sampler.sample(lo, la)
                    if value is not None: values.append(value)
                rings[str(radius)] = {"n_scenarios": len(values), "min_m": min(values) if values else None,
                                      "max_m": max(values) if values else None,
                                      "meaning": "fixed radial placement sensitivity; no relocation or probability distribution"}
            collars.append({"physical_id": pid, "product": key, "lon": lon, "lat": lat,
                            "center_bilinear_m": center, "center_nearest_m": sampler.sample(lon, lat, "nearest"),
                            "source_printed_m": ref, "source_height_datum": "UNKNOWN",
                            "product_mask_code": mask, "prior_audit_masks": p.get("masks_at_nearest_source_cell"),
                            "numerical_difference_from_printed_m": None if center is None or ref is None else center-ref,
                            "difference_status": "NUMERICAL_DIAGNOSTIC_ONLY_UNKNOWN_SOURCE_DATUM_EPOCH",
                            "radial_placement_sensitivity": rings, "product_admission": sampler.spec["admission"],
                            "absolute_collar_admitted": False})
    result = {"schema": "vkm.surface_product_diagnostic/1", "intended_use": "DIAGNOSTIC_ONLY",
              "method": "native geotransform pixel-centre bilinear; nearest separately; no reprojection/resampling of tiles",
              "reference": "existing ATL08 audit H=h-N EGM2008; product/statistic/version/QC/segment support unresolved",
              "limitations": ["not subsidence", "not collar validation", "not product accuracy ranking",
                              "not an independent GEDTM validation: GEDTM trained on ICESat-2/GEDI; sample overlap UNKNOWN",
                              "different surface roles/epochs; no epoch correction fabricated",
                              "GEDTM metadata scale conflict retains two explicit diagnostic hypotheses",
                              "AW3D30 EGM96 residual excluded without EGM96-to-EGM2008 operation",
                              "no DSM/model/input replacement performed"],
              "point_count": len(points), "products": {k: s.metadata for k, s in samplers.items()},
              "product_masks": {k: s.metadata for k, s in masks.items()},
              "scores": scores, "paired_with_baseline": paired, "collar_sensitivity": collars,
              "inputs_sha256": inputs, "config_sha256": sha256(config_path),
              "code_sha256": sha256(Path(__file__)),
              "runtime": {"python": platform.python_version(), "numpy": np.__version__, "gdal": gdal.VersionInfo("RELEASE_NAME")}}
    (out/"surface_comparison.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf8")
    receipt = {"inputs_sha256": inputs, "config_sha256": result["config_sha256"], "code_sha256": result["code_sha256"],
               "outputs_sha256": {p.name: sha256(p) for p in (out/"surface_comparison.json", out/"point_comparisons.csv")},
               "status": "COMPLETED_DIAGNOSTIC_ONLY", "runtime": result["runtime"]}
    (out/"receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf8")
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", type=Path, required=True); p.add_argument("--out", type=Path, required=True)
    p.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument("--resources-root", type=Path, default=os.environ.get("VKM_RESOURCES_ROOT"))
    args = p.parse_args()
    if args.resources_root is None:
        p.error("--resources-root or VKM_RESOURCES_ROOT is required")
    result = run(args.config, args.out, {"PUBLIC": args.repo_root, "PRIVATE": args.resources_root})
    print(json.dumps({"status": "COMPLETED_DIAGNOSTIC_ONLY", "point_count": result["point_count"],
                      "all_admitted": {k: v["all_inherited_admitted"] for k, v in result["scores"].items()}}, ensure_ascii=False))


if __name__ == "__main__":
    main()

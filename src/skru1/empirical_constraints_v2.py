"""Versioned evidence registry. No temporal trajectories or legacy labels are read."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from .scenario_simulation import repository_path, sha256_file, verify_file, _write_csv, _write_json


NUMERIC = "ACCEPTED_NUMERIC_CONSTRAINT"
STRUCTURAL = "ACCEPTED_STRUCTURAL_CONSTRAINT"


def checked_inputs(root: Path, config: dict) -> list[Path]:
    paths = []
    for spec in config["inputs"]:
        p = repository_path(root, spec["path"])
        verify_file(p, expected_hash=spec["sha256"], expected_size=spec["size_bytes"])
        paths.append(p)
    return paths


def inventory(root: Path, paths: list[Path]) -> list[dict]:
    paths = [p.resolve() for p in paths]
    return [{"path": p.relative_to(root).as_posix(), "sha256": sha256_file(p),
             "size_bytes": p.stat().st_size} for p in sorted(set(paths))]


def empty_destination(root: Path, output: Path, declared: str) -> None:
    output = output.resolve()
    if output != repository_path(root, declared) and not output.is_relative_to(root / "work/data_foundation_v2"):
        raise ValueError("Output must be its declared new release or work/data_foundation_v2")
    if output.exists() and any(output.iterdir()):
        raise FileExistsError("Immutable destination already contains files; choose a new work directory")


def canonical_digitization(root: Path, config: dict) -> pd.DataFrame:
    frames = []
    for label in ("1", "5", "17", "6"):
        rel = f"artifacts/reconstruction/musikhin_profiles_2011_2016_v1/profile_{label}/points.csv"
        f = pd.read_csv(root / rel, dtype={"profile_label": str})
        f["input_table"] = rel
        f["input_row_number"] = np.arange(len(f)) + 2
        f["input_table_sha256"] = sha256_file(root / rel)
        frames.append(f)
    result = pd.concat(frames, ignore_index=True)
    # The independent detailed re-read is controlling for Line 1 leveling 2011-16.
    rel = "artifacts/reconstruction/musikhin_line1_2011_2016_v1/points.csv"
    detailed = pd.read_csv(root / rel).set_index("figure_order")
    mask = result.profile_label.eq("1") & result.method.eq("leveling") & result.observation_interval_label.eq("2011-2016")
    for idx in result.index[mask]:
        row = detailed.loc[result.loc[idx, "figure_order"]]
        result.loc[idx, "signed_displacement_mm"] = row.signed_subsidence_mm
        result.loc[idx, "digitization_envelope_half_width_mm"] = row.reading_envelope_half_width_mm
        result.loc[idx, "reading_status"] = {
            "clear_marker_center": "digitized_marker_or_visible_curve",
            "partially_occluded_approximation": "partially_occluded_approximation",
            "unresolved_occlusion": "unresolved_occlusion",
        }[row.readability]
        result.loc[idx, "source_pixel_x"] = row.selected_pixel_x
        result.loc[idx, "source_pixel_y"] = row.selected_pixel_y
        result.loc[idx, "input_table"] = rel
        result.loc[idx, "input_table_sha256"] = sha256_file(root / rel)
        result.loc[idx, "input_row_number"] = int(row.name) + 1
    result["origin_class"] = "DIGITIZED"
    result["provenance_hash"] = result.input_table_sha256
    # Old atlas axis-fit diagnostics describe the old reading. Do not attach
    # them to the independent detailed replacement as if recomputed here.
    result = result.drop(columns=["axis_calibrated_signed_mm", "digitization_minus_axis_calibration_mm"])
    result["reading_priority"] = np.where(mask,"accepted_independent_detailed_reread","accepted_atlas")
    assert result.signed_displacement_mm.notna().sum() == 253
    assert result.loc[mask, "signed_displacement_mm"].notna().sum() == 11
    return result


def profile_evidence(points: pd.DataFrame, source: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows, summaries = [], []
    shapes = {"1": "monotone_gradient", "5": "two_lobes", "17": "broad_bowl", "6": "localized_bowl"}
    for (profile, method, period), group in points.groupby(["profile_label", "method", "observation_interval_label"], sort=True):
        finite = group.dropna(subset=["signed_displacement_mm"])
        values = finite.signed_displacement_mm
        uncertainty = finite.digitization_envelope_half_width_mm
        assert uncertainty.notna().all()
        cid = f"MUS-P{profile}-{method.upper()}-{period}-SIGNED-DISP"
        numeric_use = "interval_displacement_envelope" if method == "leveling" else "modality_comparison_only"
        rows.append(dict(
            constraint_id=cid, domain="published_spatial_profile", quantity="signed_displacement_envelope",
            lower_bound=float((values - uncertainty).min()), upper_bound=float((values + uncertainty).max()), unit="mm",
            allowed_values="", basis_type="published_graph_digitization", source_id="SUP01",
            source_file=source["path"], source_page="15", source_location=f"PDF p.15, Line {profile}, {method}, {period}",
            object=f"published_line_{profile}", method=method, period_start=period[:4], period_end=period[-4:],
            sign_convention="negative_down_as_printed", uncertainty_type="conservative_reading_envelope_not_sigma",
            confidence="medium_reading_low_temporal_transfer", status=NUMERIC,
            spatial_semantics="categorical_order_printed_distance_labels_no_georeference",
            temporal_semantics="one_displacement_interval_exact_campaign_dates_and_common_datum_unknown",
            allowed_use=numeric_use, use_scope=numeric_use,
            prohibited_use="cross_period_subtraction;field_sigma;metric_gradient;point_id_mapping;real_temporal_history",
            dependency="method_and_period_separate;nominal_duration_is_explicit_design_assumption",
            notes="Extrema include digitization bounds, not population quantiles or measurement precision.",
            provenance_hash=source["sha256"], current_use="registry_only_not_read_by_v1_formula",
            new_use=numeric_use, reason="published_interval_profile_is_not_a_temporal_trajectory",
        ))
        summaries.append(dict(
            constraint_id=cid, profile_label=profile, method=method, interval=period,
            positions=len(group), readable=len(finite), distinct=int(group.reading_status.eq("digitized_marker_or_visible_curve").sum()),
            approximate=int(group.reading_status.eq("partially_occluded_approximation").sum()), unresolved=int(group.signed_displacement_mm.isna().sum()),
            signed_min_mm=float(values.min()), signed_max_mm=float(values.max()),
            read_bound_min_mm=float(uncertainty.min()), read_bound_max_mm=float(uncertainty.max()),
            magnitude_lower_mm=max(0., -float((values + uncertainty).max())),
            magnitude_upper_mm=max(0., -float((values - uncertainty).min())),
            nominal_interval_years=int(period[-4:]) - int(period[:4]), duration_status="assumed_from_year_labels_not_exact_dates",
            structural_shape=shapes[profile], numeric_calibration=method == "leveling",
        ))
    return pd.DataFrame(rows), pd.DataFrame(summaries)


def modality_comparison(points: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (profile, period), group in points.groupby(["profile_label", "observation_interval_label"], sort=True):
        lvl = group.loc[group.method.eq("leveling")].set_index("figure_order")
        radar = group.loc[group.method.eq("radar")].set_index("figure_order")
        common = lvl.index.intersection(radar.index)
        a, b = lvl.loc[common], radar.loc[common]
        ok = a.signed_displacement_mm.notna() & b.signed_displacement_mm.notna()
        a, b = a.loc[ok], b.loc[ok]
        # Compare printed slots, never interpolate the hidden points.
        assert a.distance_label_as_printed_km.astype(str).equals(b.distance_label_as_printed_km.astype(str))
        d = b.signed_displacement_mm.to_numpy() - a.signed_displacement_mm.to_numpy()
        u = b.digitization_envelope_half_width_mm.to_numpy() + a.digitization_envelope_half_width_mm.to_numpy()
        rows.append(dict(profile_label=profile, interval=period, paired_readable=len(d),
                         signed_radar_minus_leveling_mean_mm=float(d.mean()), median_mm=float(np.median(d)),
                         rms_disagreement_mm=float(np.sqrt(np.mean(d*d))), max_abs_mm=float(np.abs(d).max()),
                         correlation=float(np.corrcoef(a.signed_displacement_mm,b.signed_displacement_mm)[0,1]),
                         fraction_reading_intervals_overlap=float(np.mean(np.abs(d)<=u)),
                         interpretation="plotted_modality_disagreement_not_leveling_error_or_independent_validation"))
    return pd.DataFrame(rows)


def build_constraints(root: Path, config_path: Path, output: Path) -> dict:
    root = root.resolve(); output = output.resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    empty_destination(root, output, config["output_directory"])
    paths = checked_inputs(root, config)
    points = canonical_digitization(root, config)
    source = next(x for x in config["inputs"] if x["path"].endswith("geokniga-02obrabotka.pdf"))
    constraints, profiles = profile_evidence(points, source)
    extra = pd.DataFrame(config["curated_evidence"])
    constraints = pd.concat([constraints, extra], ignore_index=True).fillna("")
    assert constraints.constraint_id.is_unique
    for row in constraints.to_dict("records"):
        if row["status"] in {"UNRESOLVED", "QUALITATIVE_ONLY", "REJECTED"}:
            assert row["new_use"] != "interval_displacement_envelope"
        if row["lower_bound"] != "":
            assert float(row["lower_bound"]) <= float(row["upper_bound"])
    output.mkdir(parents=True, exist_ok=True)
    _write_csv(output / "scenario_constraints.csv", constraints)
    _write_csv(output / "profile_series_inventory.csv", profiles)
    _write_csv(output / "canonical_digitization.csv", points)
    _write_csv(output / "modality_comparison.csv", modality_comparison(points))
    report = dict(status="PASS", constraint_count=len(constraints), profile_series_count=len(profiles),
                  numeric_generation_series=int(profiles.numeric_calibration.sum()), readable=253, unresolved=5,
                  line1_detailed_reading_precedence=True, cross_period_subtraction=False,
                  field_measurement_precision_inferred=False, models_trained=0)
    _write_json(output / "validation_report.json", report)
    _write_json(output / "manifest.json", dict(schema_version=2, artifact_id=config["artifact_id"],
        inputs=inventory(root, paths + [config_path, Path(__file__), root / "scripts/build_scenario_constraints_v2.py"]),
        outputs=[dict(path=p.name,sha256=sha256_file(p),size_bytes=p.stat().st_size,deterministic=True) for p in sorted(output.iterdir()) if p.is_file()],
        provenance_classes=["PUBLISHED","DIGITIZED","DERIVED"], models_trained=0))
    return report

"""Versioned metadata/status repair, without modifying frozen benchmark inputs.

No model fitting, generator truth loading or new holdout target access is performed.
Source observation dates, publication dates and scenario availability are different fields.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

KEY = ["campaign_id", "point_id"]
METADATA = (
    "sample_id", "point_id", "profile_id", "current_campaign_id", "current_date",
    "target_campaign_id", "target_date", "split",
)
HISTORY_PLAN = (
    "n_history", "last_settlement_mm", "last_rate_mm_y", "mean_last_3_rates_mm_y",
    "std_last_3_rates_mm_y", "recent_acceleration_mm_y2", "current_standard_uncertainty_mm",
    "days_since_previous_observation", "forecast_horizon_days", "missing_campaigns_since_previous",
    "profile_mean_settlement_mm", "profile_mean_rate_mm_y", "profile_rate_std_mm_y",
    "profile_n_observed", "current_campaign_type", "target_campaign_type",
)
UNITS = {
    "n_history": "count", "last_settlement_mm": "mm", "last_rate_mm_y": "mm/year",
    "mean_last_3_rates_mm_y": "mm/year", "std_last_3_rates_mm_y": "mm/year",
    "recent_acceleration_mm_y2": "mm/year^2", "current_standard_uncertainty_mm": "mm",
    "days_since_previous_observation": "day", "forecast_horizon_days": "day",
    "missing_campaigns_since_previous": "count", "profile_mean_settlement_mm": "mm",
    "profile_mean_rate_mm_y": "mm/year", "profile_rate_std_mm_y": "mm/year",
    "profile_n_observed": "count", "current_campaign_type": "category", "target_campaign_type": "category",
}
SPATIAL = {
    "settlement_anchor_map_mm": ("Рис. 22 и опубликованные зональные фрагменты", "R", "mm", None, "Чтение палитры, заполнение пропусков и приведение к якорям; не измерение"),
    "kzt": ("Рис. 24а и опубликованные фрагменты", "R", "1", None, "Чтение палитры и пространственная реконструкция коэффициента"),
    "ko": ("Рис. 24б и опубликованные фрагменты", "R", "1", None, "Чтение палитры и пространственная реконструкция коэффициента"),
    "seismic_energy_J_m2": ("Рис. 24в; подпись: Es на 2023 год", "R", "J/m2", 2023, "Числовой прокси цветового класса; 27.5 для >25 не является опубликованным точным значением"),
    "fill_density": ("Рис. 25б", "R", "1", None, "Доля красных пикселей штриховки; не доля заполненного объёма"),
    "fault_distance_m": ("Рис. 24г", "R", "approximate_local_m", None, "Расстояние до оцифрованных линий в приближённой локальной системе"),
    "lithology": ("Рис. 25в", "R", "category", None, "Категория, полученная из цветов и реконструкции; не геологическое опробование"),
    "terrain_TRI_relative": ("Рис. 25а", "R", "relative_colour_proxy", None, "Цветовой контраст растра; не TRI, рассчитанный из высот DEM"),
    "terrain_roughness_relative": ("Рис. 25а", "R", "relative_colour_proxy", None, "Яркостный прокси изображения; не измеренная шероховатость рельефа"),
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bools(series: pd.Series) -> pd.Series:
    if series.isna().any():
        raise ValueError(f"Missing boolean: {series.name}")
    values = series.astype(str).str.lower()
    if not values.isin(["true", "false", "1", "0"]).all():
        raise ValueError(f"Invalid boolean: {series.name}")
    return values.isin(["true", "1"])


def require_unique(frame: pd.DataFrame, keys: list[str], name: str) -> None:
    if frame[keys].isna().any().any() or frame.duplicated(keys).any():
        raise ValueError(f"Missing or duplicate key in {name}: {keys}")


def reconcile_membership(membership: pd.DataFrame, adjusted: pd.DataFrame,
                         runs: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Observed means an accepted finite adjustment exists, not a measurement attempt."""
    require_unique(membership, KEY, "membership")
    require_unique(adjusted, KEY, "adjusted")
    require_unique(runs, ["run_id"], "runs")
    good = (adjusted.qc_status.eq("accepted") &
            np.isfinite(adjusted.observed_settlement_mm) &
            np.isfinite(adjusted.standard_uncertainty_mm) &
            adjusted.standard_uncertainty_mm.gt(0))
    accepted = adjusted.loc[good].copy()
    checked = accepted.merge(membership[KEY + ["date", "targeted"]], on=KEY,
                             how="left", suffixes=("_adjusted", "_membership"), indicator=True)
    if not checked._merge.eq("both").all():
        raise ValueError("Accepted adjustment is absent from membership")
    if not checked.date_adjusted.eq(checked.date_membership).all():
        raise ValueError("Adjustment and membership dates differ")
    if not bools(checked.targeted).all():
        raise ValueError("Accepted adjustment for a non-targeted point")
    out = membership.copy()
    out["legacy_observed"] = bools(out.observed)
    out["legacy_membership_status"] = out.membership_status
    out["legacy_missing_reason"] = out.missing_reason
    index = pd.MultiIndex.from_frame(accepted[KEY])
    out["adjusted_epoch_exists"] = pd.MultiIndex.from_frame(out[KEY]).isin(index)
    out["observed"] = out.adjusted_epoch_exists
    corrections = []
    for i, row in out.loc[out.legacy_observed.ne(out.observed)].iterrows():
        reason = ""
        evidence_runs = []
        if not row.observed:
            attempts = runs.loc[runs.campaign_id.eq(row.campaign_id) & runs.profile_id.eq(row.profile_id)]
            # Only assert failed QC when the run log proves every attempt was rejected.
            if len(attempts) and attempts.qc_status.str.startswith("rejected").all():
                reason = "rejected_after_qc"
                evidence_runs = attempts.run_id.tolist()
            else:
                reason = "missing_accepted_adjustment"
            out.loc[i, "membership_status"] = "missing"
            out.loc[i, "missing_reason"] = reason
        else:
            out.loc[i, "membership_status"] = "observed"
            out.loc[i, "missing_reason"] = ""
        corrections.append({"campaign_id": row.campaign_id, "point_id": row.point_id,
                            "old_observed": bool(row.legacy_observed), "new_observed": bool(row.observed),
                            "reason": reason, "evidence_run_ids": "|".join(evidence_runs)})
    if not out.observed.eq(out.adjusted_epoch_exists).all():
        raise AssertionError("Membership reconciliation failed")
    return out, pd.DataFrame(corrections, columns=["campaign_id", "point_id", "old_observed", "new_observed", "reason", "evidence_run_ids"])


def repair_samples(features: pd.DataFrame, targets: pd.DataFrame,
                   membership: pd.DataFrame, adjusted: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Keep numeric labels and dates fixed, reconcile status and historical missing counts."""
    require_unique(features, ["sample_id"], "features")
    require_unique(targets, ["sample_id"], "targets")
    if set(features.sample_id) != set(targets.sample_id):
        raise ValueError("Feature/target sample IDs differ")
    f = features.copy()
    t = targets.set_index("sample_id").loc[f.sample_id].reset_index()
    for column in [*METADATA, "forecast_horizon_days"]:
        if not f[column].reset_index(drop=True).eq(t[column].reset_index(drop=True)).all():
            raise ValueError(f"Feature/target metadata differs: {column}")
    lookup = membership.set_index(KEY)
    histories = {p: g.sort_values("date") for p, g in adjusted.loc[adjusted.qc_status.eq("accepted")].groupby("point_id")}
    groups = {p: g.sort_values("date") for p, g in membership.groupby("point_id")}
    changes = []
    for i, row in f.iterrows():
        future = groups[row.point_id]
        future = future.loc[bools(future.targeted) & future.date.gt(row.current_date)]
        if future.empty or future.iloc[0].campaign_id != row.target_campaign_id:
            raise ValueError(f"Not the next planned target: {row.sample_id}")
        horizon = (pd.Timestamp(row.target_date) - pd.Timestamp(row.current_date)).days
        if horizon <= 0 or horizon != row.forecast_horizon_days:
            raise ValueError(f"Invalid horizon: {row.sample_id}")
        planned = lookup.loc[(row.target_campaign_id, row.point_id)]
        if bool(planned.observed) != bool(t.loc[i, "target_available"]):
            raise ValueError("Repair would change numeric target availability; explicit target rebuild required")
        if not planned.observed:
            t.loc[i, "label_status"] = "censored_" + str(planned.missing_reason)
            t.loc[i, "missing_reason"] = planned.missing_reason
            if pd.notna(t.loc[i, "observed_rate_mm_y"]):
                raise ValueError("Censored target has a numeric label")
        h = histories[row.point_id]
        h = h.loc[h.date.le(row.current_date)]
        if len(h) < 2 or h.iloc[-1].campaign_id != row.current_campaign_id:
            raise ValueError("Current adjustment is missing")
        between = groups[row.point_id]
        between = between.loc[between.date.gt(h.iloc[-2].date) & between.date.lt(row.current_date)]
        count = int((~bools(between.observed)).sum())
        if count != row.missing_campaigns_since_previous:
            changes.append({"sample_id": row.sample_id, "field": "missing_campaigns_since_previous",
                            "old_value": int(row.missing_campaigns_since_previous), "new_value": count})
            f.loc[i, "missing_campaigns_since_previous"] = count
    return f, t, pd.DataFrame(changes, columns=["sample_id", "field", "old_value", "new_value"])


def field_catalog(contract: pd.DataFrame, fields: list[str]) -> pd.DataFrame:
    rows = []
    for r in contract.to_dict("records"):
        field = r["field"]
        base = field.split("__")[0]
        item = {"field": field, "legacy_role": r["role"], "legacy_allowed": bool(r["allowed"]),
                "present_in_features": field in fields, "source_id": "", "source_locator": "",
                "source_reference_year": None, "source_available_at": None,
                "source_availability_status": "not_applicable_synthetic",
                "provenance_class": "S", "unit": "see_parent_field", "derivation": "",
                "scenario_available_at_rule": "never", "assumption_id": "",
                "history_and_plan_allowed": field in HISTORY_PLAN,
                "reconstruction_assumptions_allowed": bool(r["allowed"]) and field in fields}
        if field in METADATA:
            item.update(derivation="Ключи и метаданные сценария; не вход оценщика", scenario_available_at_rule="metadata_only",
                        unit="ISO_date" if field.endswith("_date") else "identifier_or_category")
        elif base in SPATIAL:
            locator, prov, unit, year, derivation = SPATIAL[base]
            item.update(source_id="SRC01", source_locator=locator, source_reference_year=year,
                        source_availability_status="unknown_not_established_by_publication_year",
                        provenance_class=prov, unit=unit, derivation=derivation,
                        scenario_available_at_rule="scenario_start_if_explicit_assumption",
                        assumption_id="SIM_STATIC_KNOWN")
            if "__" in field:
                item["derivation"] += "; суффикс — метаданные реконструкции, не отдельное измерение"
                if field.endswith("__donor_distance_m"):
                    item["unit"] = "approximate_local_m"
                elif field.endswith("__provenance"):
                    item["unit"] = "category"
        elif field in ["chainage_m", "chainage_normalized_profile"]:
            item.update(provenance_class="R/S", unit="approximate_local_m" if field == "chainage_m" else "1",
                        derivation="Положение в сконструированном профиле; не исходный пикетаж",
                        scenario_available_at_rule="scenario_start_if_explicit_assumption", assumption_id="SIM_STATIC_KNOWN")
        elif field in ["target_campaign_type", "forecast_horizon_days", "current_campaign_type"]:
            item.update(source_locator="tables/survey_campaigns.csv and campaign_point_membership.csv",
                        derivation="План синтетических кампаний; горизонт от current_date до target_date",
                        scenario_available_at_rule="scenario_start", assumption_id="SIM_PLAN_KNOWN")
        elif field in HISTORY_PLAN:
            item.update(source_locator="tables/leveling_adjusted_epochs.csv; campaign_point_membership.csv",
                        derivation="Расчёт по принятым синтетическим наблюдениям не позднее current_date",
                        scenario_available_at_rule="current_date", assumption_id="SIM_ADJUSTMENT_INSTANT")
        elif bool(r["allowed"]) and field in fields:
            raise ValueError(f"No provenance rule for model field: {field}")
        else:
            item.update(derivation="Исключено из признаков: оценочная истина, параметры генератора или координаты",
                        reconstruction_assumptions_allowed=False)
        if field in UNITS:
            item["unit"] = UNITS[field]
        rows.append(item)
    out = pd.DataFrame(rows)
    if set(fields) - set(out.field):
        raise ValueError("Fields without a catalog entry")
    return out


def repair_lineage(lineage: pd.DataFrame) -> pd.DataFrame:
    out = lineage.copy()
    out["legacy_provenance"] = out.provenance
    out["legacy_source_reference_year"] = out.source_reference_year
    out["source_reference_year"] = pd.Series(pd.NA, index=out.index, dtype="Int64")
    out["source_available_at"] = pd.NA
    out["source_availability_status"] = "unknown"
    out["scenario_available_at_rule"] = "scenario_start_if_explicit_assumption"
    out["assumption_id"] = "SIM_STATIC_KNOWN"
    out["uncertainty_interpretation"] = "legacy_heuristic_not_empirical_measurement_sigma"
    for i, row in out.iterrows():
        if row.feature not in SPATIAL:
            raise ValueError(f"Unknown lineage feature: {row.feature}")
        locator, prov, unit, year, derivation = SPATIAL[row.feature]
        is_ref = row.point_type == "REF"
        out.loc[i, "provenance"] = "S" if is_ref else prov
        out.loc[i, "source_id"] = "" if is_ref else "SRC01"
        out.loc[i, "source_locator"] = "synthetic_reference_design" if is_ref else locator
        out.loc[i, "source_reference_year"] = pd.NA if is_ref or year is None else year
        out.loc[i, "derivation"] = "Назначено искусственной опорной точке" if is_ref else derivation
        out.loc[i, "unit"] = unit
        out.loc[i, "source_availability_status"] = "not_applicable_synthetic" if is_ref else "unknown"
        # Old global `model_feature_allowed` meant unconditional availability.
        out.loc[i, "model_feature_allowed"] = False
        out.loc[i, "reconstruction_assumptions_allowed"] = row.feature != "settlement_anchor_map_mm"
    return out


def select_features(features: pd.DataFrame, catalog: pd.DataFrame, *, view: str = "history_and_plan",
                    acknowledge_assumptions: bool = False, scenario_start: str = "2018-01-01") -> pd.DataFrame:
    if view not in ["history_and_plan", "reconstruction_assumptions"]:
        raise ValueError("Unknown feature view")
    if view == "reconstruction_assumptions" and not acknowledge_assumptions:
        raise ValueError("Reconstruction view requires explicit SIM_STATIC_KNOWN acknowledgement")
    allowed = catalog.loc[catalog[f"{view}_allowed"] & catalog.present_in_features]
    # Unknown real availability can never be silently converted into a known historical date.
    if view == "history_and_plan" and allowed.source_availability_status.str.startswith("unknown").any():
        raise ValueError("Unknown source availability in default history/plan view")
    if (pd.to_datetime(features.current_date) < pd.Timestamp(scenario_start)).any():
        raise ValueError("Origin precedes the declared scenario and plan availability")
    for row in allowed.itertuples():
        if pd.notna(row.source_available_at) and str(row.source_available_at).strip():
            if not features.current_date.map(lambda date: known_source_available(row.source_available_at, date)).all():
                raise ValueError(f"Source is unavailable at an origin: {row.field}")
    return features[[*METADATA, *allowed.field.tolist()]].copy()


def known_source_available(source_available_at: str | None, origin: str) -> bool:
    """Fail closed: publication/snapshot year is not an available_at substitute."""
    if source_available_at is None or pd.isna(source_available_at) or not str(source_available_at).strip():
        return False
    return bool(pd.Timestamp(source_available_at) <= pd.Timestamp(origin))


def preserve_unchanged_csv_cells(source: Path, before: pd.DataFrame,
                                 after: pd.DataFrame, key: str = "sample_id") -> pd.DataFrame:
    """Keep unchanged decimal tokens exactly; metadata repair must not round labels."""
    require_unique(before, [key], "original serialization")
    require_unique(after, [key], "repaired serialization")
    if set(before[key]) != set(after[key]) or set(before) != set(after):
        raise ValueError("CSV preservation requires the same keys and columns")
    text_rows = pd.read_csv(source, dtype=str, keep_default_na=False)
    text_rows = text_rows.set_index(key).loc[after[key]].reset_index()
    original = before.set_index(key).loc[after[key]].reset_index()
    updated = after.reset_index(drop=True)
    for column in after:
        unchanged = original[column].eq(updated[column]).fillna(False) | (original[column].isna() & updated[column].isna())
        changed = ~unchanged
        if changed.any():
            text_rows.loc[changed, column] = updated.loc[changed, column].map(
                lambda value: "" if pd.isna(value) else str(value))
    return text_rows[list(after)]


def build_release(root: Path, output_relative: str | None = None) -> dict:
    root = root.resolve()
    config_rel = "configs/reconstruction_research_v1.json"
    config = json.loads((root / config_rel).read_text(encoding="utf-8"))
    output_path = Path(output_relative or config["output_directory"])
    if output_path.is_absolute():
        raise ValueError("Output path must be relative to the repository root")
    output = root / output_path
    if not output.resolve().is_relative_to(root):
        raise ValueError("Output must remain repository-relative")
    raw = Path(config["raw_input_directory"])
    targets_dir = Path(config["parent_target_directory"])
    paths = {
        "membership": raw / "tables/campaign_point_membership.csv",
        "adjusted": raw / "tables/leveling_adjusted_epochs.csv",
        "runs": raw / "tables/leveling_runs_summary.csv",
        "lineage": raw / "tables/point_feature_lineage.csv",
        "features": targets_dir / "next_planned_features.csv",
        "targets": targets_dir / "next_planned_operational_targets.csv",
        "contract": targets_dir / "formal_feature_contract.csv",
    }
    protected_roots = [root / "inputs", root / "SKRU1_ACTUAL_DATA_TABLES_v1", root / "artifacts"]
    if any(output.resolve().is_relative_to(p.resolve()) for p in protected_roots):
        raise ValueError("Refusing to overwrite primary or frozen artifact roots")
    # Verify every bundled source/ZIP before transformation (including supplementary).
    import importlib.util
    spec = importlib.util.spec_from_file_location("verify_skru1_inputs", root / "scripts/verify_inputs.py")
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    verification = []
    for name in ["input_manifest.csv", "source_manifest.csv", "supplementary_source_manifest.csv"]:
        verification.extend(verifier.verify_manifest(root, root / "configs" / name))
    if any(r["status"] != "PASS" for r in verification):
        raise ValueError("Bundled input hash verification failed")
    parent_manifest = pd.read_csv(root / "configs/reconstruction_parent_manifest.csv")
    expected = set(paths.values())
    if expected != set(map(Path, parent_manifest.relative_path)):
        raise ValueError("Parent manifest does not cover exactly the repair inputs")
    for rec in parent_manifest.to_dict("records"):
        p = root / rec["relative_path"]
        if p.stat().st_size != rec["size_bytes"] or sha256(p) != rec["sha256"]:
            raise ValueError(f"Parent input changed: {rec['relative_path']}")
    frames = {name: pd.read_csv(root / path, low_memory=False) for name, path in paths.items()}
    repaired, corrections = reconcile_membership(frames["membership"], frames["adjusted"], frames["runs"])
    repaired["observation_origin"] = "synthetic_monitoring_simulation"
    repaired["source_available_at"] = pd.NA
    repaired["scenario_plan_available_at"] = config["scenario_time_origin"]
    repaired["plan_availability_assumption_id"] = "SIM_PLAN_KNOWN"
    repaired["scenario_observation_available_at"] = repaired.date.where(repaired.observed, pd.NA)
    repaired["observation_availability_assumption_id"] = "SIM_ADJUSTMENT_INSTANT"
    features, targets, feature_changes = repair_samples(frames["features"], frames["targets"], repaired, frames["adjusted"])
    catalog = field_catalog(frames["contract"], list(features.columns))
    lineage = repair_lineage(frames["lineage"])
    # Wide provenance strings must agree with the new point-level provenance.
    for feature, g in lineage.loc[lineage.point_type.eq("WORK")].groupby("feature"):
        column = feature + "__provenance"
        if column in features:
            features[column] = features.point_id.map(g.set_index("point_id").provenance)
    history_view = select_features(features, catalog, scenario_start=config["scenario_time_origin"])
    augmented_view = select_features(features, catalog, view="reconstruction_assumptions", acknowledge_assumptions=True,
                                     scenario_start=config["scenario_time_origin"])
    contract = frames["contract"].copy()
    allowed_map = catalog.set_index("field").history_and_plan_allowed.to_dict()
    contract["allowed"] = contract.field.map(allowed_map).fillna(False)
    contract.loc[contract.field.isin(set(frames["features"]) - set(history_view)), "reason"] = "Excluded by default; reconstructed spatial attributes require an explicit simulation assumption"
    # Validate before writing a release: a failed repair must not replace valid tables.
    original = frames["targets"].set_index("sample_id").loc[targets.sample_id]
    for column in ["target_date", "observed_rate_mm_y", "observed_increment_mm", "target_available"]:
        a = original[column].reset_index(drop=True)
        b = targets[column].reset_index(drop=True)
        if not (a.eq(b) | (a.isna() & b.isna())).all():
            raise AssertionError(f"Unexpected target mutation: {column}")
    output.mkdir(parents=True, exist_ok=True)
    serialized_features = preserve_unchanged_csv_cells(root / paths["features"], frames["features"], features)
    serialized_targets = preserve_unchanged_csv_cells(root / paths["targets"], frames["targets"], targets)
    tables = {"campaign_point_membership.csv": repaired, "membership_corrections.csv": corrections,
              "next_planned_features_all.csv": serialized_features,
              "next_planned_features.csv": serialized_features[list(history_view)],
              "next_planned_features_reconstruction_assumptions.csv": serialized_features[list(augmented_view)],
              "next_planned_operational_targets.csv": serialized_targets, "feature_changes.csv": feature_changes,
              "field_catalog.csv": catalog, "point_feature_lineage.csv": lineage,
              "formal_feature_contract.csv": contract}
    for name, frame in tables.items():
        (output / name).write_text(frame.to_csv(index=False, lineterminator="\n"), encoding="utf-8-sig")
    report = {"dataset_id": config["dataset_id"], "claim_domain": config["research_domain"],
              "bundled_inputs_verified": len(verification), "parent_tables_verified": len(paths),
              "membership_rows": len(repaired), "membership_corrected": len(corrections),
              "membership_conflicts_after": int(repaired.observed.ne(repaired.adjusted_epoch_exists).sum()),
              "correction_reasons": corrections.reason.value_counts().to_dict(),
              "candidate_origins": len(features), "available_targets": int(bools(targets.target_available).sum()),
              "label_status_counts": targets.label_status.value_counts().to_dict(),
              "historical_feature_values_corrected": len(feature_changes),
              "catalog_fields": len(catalog), "history_plan_feature_count": len(history_view.columns) - len(METADATA),
              "augmented_feature_count": len(augmented_view.columns) - len(METADATA),
              "real_source_available_at_values_invented": 0, "models_trained": 0,
              "numeric_targets_unchanged": True, "frozen_benchmarks_modified": False,
              "new_external_holdout_opened": False,
              "remaining_work": ["spatial_reconstruction_validation", "scenario_protocol", "runner_migration", "performance_profile"]}
    (output / "validation_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    input_paths = list(paths.values()) + [Path(config_rel), Path("configs/reconstruction_parent_manifest.csv"),
        Path("configs/input_manifest.csv"), Path("configs/source_manifest.csv"),
        Path("configs/supplementary_source_manifest.csv"), Path("src/skru1/reconstruction_data.py"),
        Path("scripts/repair_reconstruction_data.py"), Path("scripts/verify_inputs.py")]
    manifest = {"dataset_id": config["dataset_id"], "parent_commit": config["parent_commit"],
                "inputs": [{"path": p.as_posix(), "sha256": sha256(root / p)} for p in input_paths],
                "outputs": [{"path": p.name, "sha256": sha256(p), "size_bytes": p.stat().st_size}
                            for p in sorted(output / name for name in [*tables, "validation_report.json"])]}
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report

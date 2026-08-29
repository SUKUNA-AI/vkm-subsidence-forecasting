from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from matplotlib import pyplot as plt
from scipy.stats import kurtosis, skew, spearmanr
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

DAYS_PER_YEAR = 365.25
ACTIVE_STAGES = {"accelerating", "reactivated", "step_transition"}


def ensure_dirs(root: Path) -> dict[str, Path]:
    if root.exists():
        shutil.rmtree(root)
    paths = {
        "root": root,
        "tables": root / "tables",
        "targets": root / "target_tables",
        "meta": root / "metadata",
        "figures": root / "figures",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths


def save_csv(df: pd.DataFrame, path: Path) -> None:
    df.to_csv(path, index=False, encoding="utf-8-sig")


def parse_dates(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    for column in columns:
        if column in frame.columns:
            frame[column] = pd.to_datetime(frame[column], errors="coerce")
    return frame


def safe_corr(x: pd.Series, y: pd.Series, method: str) -> float:
    pair = pd.concat([x, y], axis=1).dropna()
    if len(pair) < 3 or pair.iloc[:, 0].nunique() < 2 or pair.iloc[:, 1].nunique() < 2:
        return float("nan")
    if method == "pearson":
        return float(pair.iloc[:, 0].corr(pair.iloc[:, 1], method="pearson"))
    return float(spearmanr(pair.iloc[:, 0], pair.iloc[:, 1]).statistic)


def describe_series(name: str, series: pd.Series) -> dict[str, Any]:
    values = pd.to_numeric(series, errors="coerce").dropna()
    quantiles = values.quantile([0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99])
    return {
        "variable": name,
        "n": int(values.size),
        "mean": float(values.mean()),
        "std": float(values.std(ddof=1)),
        "min": float(values.min()),
        "p01": float(quantiles.loc[0.01]),
        "p05": float(quantiles.loc[0.05]),
        "p25": float(quantiles.loc[0.25]),
        "median": float(quantiles.loc[0.5]),
        "p75": float(quantiles.loc[0.75]),
        "p95": float(quantiles.loc[0.95]),
        "p99": float(quantiles.loc[0.99]),
        "max": float(values.max()),
        "skew": float(skew(values, bias=False)),
        "excess_kurtosis": float(kurtosis(values, fisher=True, bias=False)),
    }


def split_from_target_date(date: pd.Timestamp) -> str:
    if date.year <= 2023:
        return "train"
    if date.year == 2024:
        return "validation"
    return "test"


def horizon_bin(days: float) -> str:
    if days <= 90:
        return "<=90"
    if days <= 150:
        return "91-150"
    if days <= 220:
        return "151-220"
    if days <= 365:
        return "221-365"
    return ">365"


def make_interpolators(truth: pd.DataFrame) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for point_id, group in truth.groupby("point_id"):
        group = group.sort_values("date")
        time_ns = group["date"].astype("int64").to_numpy(dtype=np.int64)
        result[point_id] = {
            "time": time_ns,
            "settlement": group["true_settlement_mm"].to_numpy(float),
            "velocity": group["true_velocity_mm_y"].to_numpy(float),
            "acceleration": group["true_acceleration_mm_y2"].to_numpy(float),
            "max_date": group["date"].max(),
        }
    return result


def interp_truth(interpolators: dict[str, dict[str, Any]], point_id: str, date: pd.Timestamp, field: str) -> float:
    data = interpolators[point_id]
    x = int(pd.Timestamp(date).value)
    return float(np.interp(x, data["time"], data[field]))


def kalman_predict(history: pd.DataFrame, target_date: pd.Timestamp, q: float = 250.0) -> float:
    history = history.sort_values("date").copy()
    if history.empty:
        return float("nan")
    y0 = float(history.iloc[0]["observed_settlement_mm"])
    x = np.array([y0, 0.0], dtype=float)  # settlement mm, velocity mm/year
    p = np.diag([25.0, 2500.0])
    previous_date = pd.Timestamp(history.iloc[0]["date"])
    for _, row in history.iterrows():
        date = pd.Timestamp(row["date"])
        dt = max((date - previous_date).days / DAYS_PER_YEAR, 0.0)
        if dt > 0:
            f = np.array([[1.0, dt], [0.0, 1.0]])
            g = np.array([[0.5 * dt * dt], [dt]])
            q_matrix = q * (g @ g.T)
            x = f @ x
            p = f @ p @ f.T + q_matrix
        h = np.array([[1.0, 0.0]])
        r = max(float(row.get("standard_uncertainty_mm", 1.0)) ** 2, 1e-6)
        innovation = float(row["observed_settlement_mm"] - (h @ x)[0])
        s = float((h @ p @ h.T)[0, 0] + r)
        k = (p @ h.T) / s
        x = x + k[:, 0] * innovation
        p = (np.eye(2) - k @ h) @ p
        previous_date = date
    dt_future = max((pd.Timestamp(target_date) - previous_date).days / DAYS_PER_YEAR, 0.0)
    return float(x[0] + dt_future * x[1])


def metric_row(split: str, target: str, model: str, actual_increment: np.ndarray, pred_increment: np.ndarray, horizon_days: np.ndarray) -> dict[str, Any]:
    actual_rate = actual_increment / horizon_days * DAYS_PER_YEAR
    pred_rate = pred_increment / horizon_days * DAYS_PER_YEAR
    return {
        "split": split,
        "target": target,
        "model": model,
        "n": int(len(actual_increment)),
        "MAE_increment_mm": float(mean_absolute_error(actual_increment, pred_increment)),
        "RMSE_increment_mm": float(mean_squared_error(actual_increment, pred_increment) ** 0.5),
        "R2_increment": float(r2_score(actual_increment, pred_increment)),
        "Bias_increment_mm": float(np.mean(pred_increment - actual_increment)),
        "MAE_rate_mm_y": float(mean_absolute_error(actual_rate, pred_rate)),
        "RMSE_rate_mm_y": float(mean_squared_error(actual_rate, pred_rate) ** 0.5),
    }


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main(source: Path, output: Path) -> None:
    paths = ensure_dirs(output)
    tdir, odir, mdir, fdir = paths["tables"], paths["targets"], paths["meta"], paths["figures"]

    # Load data.
    points = pd.read_csv(source / "tables/survey_points.csv")
    profiles = pd.read_csv(source / "tables/survey_profiles.csv")
    campaigns = parse_dates(pd.read_csv(source / "tables/survey_campaigns.csv"), ["date"])
    membership = parse_dates(pd.read_csv(source / "tables/campaign_point_membership.csv"), ["date"])
    leveling = parse_dates(pd.read_csv(source / "tables/leveling_adjusted_epochs.csv"), ["date"])
    settlement_rates = parse_dates(pd.read_csv(source / "tables/settlement_rates.csv"), ["date", "previous_date"])
    tilts = parse_dates(pd.read_csv(source / "tables/tilts.csv"), ["date"])
    curvatures = parse_dates(pd.read_csv(source / "tables/curvatures.csv"), ["date"])
    horizontal_strains = parse_dates(pd.read_csv(source / "tables/horizontal_strains.csv"), ["date"])
    profile_kinematics = parse_dates(pd.read_csv(source / "tables/profile_kinematics.csv"), ["date"])
    features = parse_dates(pd.read_csv(source / "model_ready/next_cycle_features.csv"), ["current_date", "target_date"])
    targets = pd.read_csv(source / "evaluation_only/next_cycle_targets.csv")
    truth = parse_dates(pd.read_csv(source / "evaluation_only/truth_survey_points_monthly.csv"), ["date"])
    early_features = parse_dates(pd.read_csv(source / "model_ready/early_acceleration_features.csv"), ["current_date"])
    early_labels_existing = pd.read_csv(source / "evaluation_only/early_acceleration_labels.csv")
    transitions = parse_dates(pd.read_csv(source / "evaluation_only/regime_stage_transitions.csv"), ["transition_date"])
    static_features = pd.read_csv(source / "model_ready/static_point_features.csv")
    lineage = pd.read_csv(source / "model_ready/point_feature_lineage_safe.csv")
    feature_contract_source = pd.read_csv(source / "model_ready/feature_contract.csv")
    process_params = parse_dates(pd.read_csv(source / "private_generation/process_parameters_survey_points.csv"), ["event_onset_date", "second_event_date"])

    interpolators = make_interpolators(truth)

    # Existing target EDA.
    merged = features.merge(targets, on="sample_id", validate="one_to_one")
    merged["observed_next_rate_mm_y"] = merged["observed_next_increment_mm"] / merged["forecast_horizon_days"] * DAYS_PER_YEAR
    merged["target_noise_mm"] = merged["observed_next_increment_mm"] - merged["hidden_true_next_increment_mm"]
    merged["target_sigma_increment_mm"] = np.sqrt(
        merged["current_standard_uncertainty_mm"] ** 2 + merged["next_observation_uncertainty_mm"] ** 2
    )
    merged["target_sigma_rate_mm_y"] = merged["target_sigma_increment_mm"] / merged["forecast_horizon_days"] * DAYS_PER_YEAR
    merged = merged.merge(
        process_params[["point_id", "process_family"]], on="point_id", how="left", validate="many_to_one"
    )

    # Membership/leveling mismatch.
    leveling_keys = set(zip(leveling["campaign_id"], leveling["point_id"]))
    observed_membership = membership[membership["observed"]].copy()
    mismatch = observed_membership[
        ~observed_membership.apply(lambda row: (row["campaign_id"], row["point_id"]) in leveling_keys, axis=1)
    ].copy()
    save_csv(mismatch, tdir / "observed_membership_without_adjusted_leveling.csv")

    # Formal next planned targeted target.
    membership_targeted = membership[membership["targeted"]].sort_values(["point_id", "date"])
    membership_groups = {pid: group for pid, group in membership_targeted.groupby("point_id")}
    level_lookup = leveling.set_index(["point_id", "campaign_id"])
    formal_feature_rows: list[dict[str, Any]] = []
    formal_target_rows: list[dict[str, Any]] = []
    formal_eval_rows: list[dict[str, Any]] = []

    for _, row in features.iterrows():
        point_id = row["point_id"]
        current_date = pd.Timestamp(row["current_date"])
        future = membership_groups[point_id]
        future = future[future["date"] > current_date]
        if future.empty:
            continue
        planned = future.iloc[0]
        planned_date = pd.Timestamp(planned["date"])
        planned_campaign = planned["campaign_id"]
        horizon_days = int((planned_date - current_date).days)
        split = split_from_target_date(planned_date)

        feature_row = row.to_dict()
        feature_row.update({
            "target_campaign_id": planned_campaign,
            "target_date": planned_date.date().isoformat(),
            "split": split,
            "forecast_horizon_days": horizon_days,
            "target_campaign_type": planned["campaign_type"],
        })
        formal_feature_rows.append(feature_row)

        key = (point_id, planned_campaign)
        target_available = key in level_lookup.index
        if target_available:
            level_row = level_lookup.loc[key]
            if isinstance(level_row, pd.DataFrame):
                level_row = level_row.iloc[0]
            target_obs = float(level_row["observed_settlement_mm"])
            target_sigma = float(level_row["standard_uncertainty_mm"])
            increment = target_obs - float(row["last_settlement_mm"])
            rate = increment / horizon_days * DAYS_PER_YEAR
            sigma_increment = math.sqrt(float(row["current_standard_uncertainty_mm"]) ** 2 + target_sigma ** 2)
            sigma_rate = sigma_increment / horizon_days * DAYS_PER_YEAR
            status = "observed"
        else:
            target_obs = np.nan
            target_sigma = np.nan
            increment = np.nan
            rate = np.nan
            sigma_increment = np.nan
            sigma_rate = np.nan
            if bool(planned["observed"]):
                status = "observed_but_no_adjusted_leveling"
            else:
                reason = planned.get("missing_reason")
                if pd.isna(reason) or reason is None:
                    reason = planned.get("membership_status", "missing")
                status = f"censored_{reason}"

        true_current = interp_truth(interpolators, point_id, current_date, "settlement")
        true_target = interp_truth(interpolators, point_id, planned_date, "settlement")
        true_increment = true_target - true_current
        true_rate = true_increment / horizon_days * DAYS_PER_YEAR

        formal_target_rows.append({
            "sample_id": row["sample_id"],
            "point_id": point_id,
            "profile_id": row["profile_id"],
            "current_campaign_id": row["current_campaign_id"],
            "current_date": current_date.date().isoformat(),
            "target_campaign_id": planned_campaign,
            "target_date": planned_date.date().isoformat(),
            "target_campaign_type": planned["campaign_type"],
            "split": split,
            "forecast_horizon_days": horizon_days,
            "label_status": status,
            "target_available": bool(target_available),
            "missing_reason": planned.get("missing_reason"),
            "current_observed_settlement_mm": float(row["last_settlement_mm"]),
            "target_observed_settlement_mm": target_obs,
            "observed_increment_mm": increment,
            "observed_rate_mm_y": rate,
            "current_standard_uncertainty_mm": float(row["current_standard_uncertainty_mm"]),
            "target_standard_uncertainty_mm": target_sigma,
            "sigma_increment_mm": sigma_increment,
            "sigma_rate_mm_y": sigma_rate,
            "training_weight": np.nan,
        })
        formal_eval_rows.append({
            "sample_id": row["sample_id"],
            "point_id": point_id,
            "current_date": current_date.date().isoformat(),
            "target_date": planned_date.date().isoformat(),
            "true_current_settlement_mm": true_current,
            "true_target_settlement_mm": true_target,
            "true_increment_mm": true_increment,
            "true_rate_mm_y": true_rate,
            "process_family": process_params.set_index("point_id").loc[point_id, "process_family"],
            "use_class": "evaluation_only",
        })

    formal_features = pd.DataFrame(formal_feature_rows)
    formal_targets = pd.DataFrame(formal_target_rows)
    formal_eval = pd.DataFrame(formal_eval_rows)
    available_mask = formal_targets["target_available"]
    median_variance = float(np.nanmedian(formal_targets.loc[available_mask, "sigma_rate_mm_y"] ** 2))
    weights = median_variance / (formal_targets.loc[available_mask, "sigma_rate_mm_y"] ** 2)
    weights = weights.clip(0.25, 4.0)
    weights = weights / weights.mean()
    formal_targets.loc[available_mask, "training_weight"] = weights

    save_csv(formal_features, odir / "next_planned_features.csv")
    save_csv(formal_targets, odir / "next_planned_operational_targets.csv")
    save_csv(formal_eval, odir / "next_planned_evaluation_truth.csv")

    # Existing next-available target export for comparison.
    existing_targets_formal = merged[[
        "sample_id", "point_id", "profile_id", "current_campaign_id", "current_date",
        "target_campaign_id", "target_date", "split", "forecast_horizon_days",
        "observed_next_increment_mm", "observed_next_rate_mm_y",
        "hidden_true_next_increment_mm", "hidden_true_next_rate_mm_y",
        "target_sigma_increment_mm", "target_sigma_rate_mm_y",
    ]].copy()
    save_csv(existing_targets_formal, odir / "next_available_targets_formal.csv")

    # Target semantics comparison.
    planned_map = formal_targets.set_index("sample_id")
    alignment = merged["target_campaign_id"].astype(str).to_numpy() == planned_map.loc[merged["sample_id"], "target_campaign_id"].astype(str).to_numpy()
    semantics = pd.DataFrame([
        {
            "target_frame": "Next available observation (existing)",
            "candidate_rows": len(merged),
            "available_labels": len(merged),
            "aligned_rows": int(alignment.sum()),
            "available_or_aligned_fraction": float(alignment.mean()),
            "median_horizon_days": float(merged["forecast_horizon_days"].median()),
            "max_horizon_days": float(merged["forecast_horizon_days"].max()),
            "interpretation": "Auxiliary only; may jump over a failed planned measurement.",
        },
        {
            "target_frame": "Next planned targeted campaign (formal primary)",
            "candidate_rows": len(formal_targets),
            "available_labels": int(formal_targets["target_available"].sum()),
            "aligned_rows": int(alignment.sum()),
            "available_or_aligned_fraction": float(alignment.mean()),
            "median_horizon_days": float(formal_targets["forecast_horizon_days"].median()),
            "max_horizon_days": float(formal_targets["forecast_horizon_days"].max()),
            "interpretation": "Primary operational target; failed measurements are censored, never skipped.",
        },
    ])
    save_csv(semantics, tdir / "target_semantics_comparison.csv")

    # Fixed-horizon synthetic targets.
    fixed_rows: list[dict[str, Any]] = []
    for _, row in early_features.iterrows():
        point_id = row["point_id"]
        current_date = pd.Timestamp(row["current_date"])
        current_settlement = interp_truth(interpolators, point_id, current_date, "settlement")
        record: dict[str, Any] = {
            "sample_id": row["sample_id"],
            "point_id": point_id,
            "profile_id": row["profile_id"],
            "current_date": current_date.date().isoformat(),
            "split": row["split"],
            "use_class": "synthetic_evaluation_only",
        }
        for days in (90, 180, 365):
            target_date = current_date + pd.Timedelta(days=days)
            complete = target_date <= interpolators[point_id]["max_date"]
            record[f"available_{days}d"] = complete
            record[f"target_date_{days}d"] = target_date.date().isoformat()
            if complete:
                target_settlement = interp_truth(interpolators, point_id, target_date, "settlement")
                increment = target_settlement - current_settlement
                record[f"true_increment_{days}d_mm"] = increment
                record[f"true_rate_{days}d_mm_y"] = increment / days * DAYS_PER_YEAR
            else:
                record[f"true_increment_{days}d_mm"] = np.nan
                record[f"true_rate_{days}d_mm_y"] = np.nan
        fixed_rows.append(record)
    fixed_targets = pd.DataFrame(fixed_rows)
    save_csv(fixed_targets, odir / "fixed_horizon_targets_synthetic.csv")

    # Formal early-warning labels.
    transition_groups = {pid: group.sort_values("transition_date") for pid, group in transitions.groupby("point_id")}
    truth_groups = {pid: group.sort_values("date") for pid, group in truth.groupby("point_id")}
    ew_rows: list[dict[str, Any]] = []
    for _, row in early_features.iterrows():
        point_id = row["point_id"]
        current_date = pd.Timestamp(row["current_date"])
        horizon_end = current_date + pd.Timedelta(days=180)
        complete = horizon_end <= interpolators[point_id]["max_date"]
        current_rate = interp_truth(interpolators, point_id, current_date, "velocity")
        future = truth_groups[point_id]
        future = future[(future["date"] > current_date) & (future["date"] <= horizon_end)]
        if complete and not future.empty:
            max_delta_rate = float((future["true_velocity_mm_y"] - current_rate).max())
            max_acceleration = float(future["true_acceleration_mm_y2"].max())
            elevated = (future["true_velocity_mm_y"].to_numpy() >= current_rate + 20.0)
            sustained_two = bool(np.any(elevated[:-1] & elevated[1:])) if len(elevated) >= 2 else False
            activity = bool(max_delta_rate >= 25.0 and max_acceleration >= 15.0 and sustained_two)
            trans = transition_groups.get(point_id, pd.DataFrame())
            if not trans.empty:
                trans = trans[(trans["transition_date"] > current_date) & (trans["transition_date"] <= horizon_end)]
                onset_candidates = trans[trans["to_stage"].isin(ACTIVE_STAGES)]
            else:
                onset_candidates = pd.DataFrame()
            onset = bool(activity and not onset_candidates.empty)
            first_onset_date = onset_candidates["transition_date"].min() if not onset_candidates.empty else pd.NaT
            current_stage_row = truth_groups[point_id].iloc[(truth_groups[point_id]["date"] - current_date).abs().argsort()[:1]]
            current_stage = str(current_stage_row.iloc[0]["regime_stage"])
            ongoing = bool(activity and current_stage in ACTIVE_STAGES)
        else:
            max_delta_rate = np.nan
            max_acceleration = np.nan
            sustained_two = False
            activity = False
            onset = False
            ongoing = False
            first_onset_date = pd.NaT
            current_stage = np.nan
        label_horizon_end = horizon_end
        split = split_from_target_date(label_horizon_end)
        ew_rows.append({
            "sample_id": row["sample_id"],
            "point_id": point_id,
            "profile_id": row["profile_id"],
            "current_date": current_date.date().isoformat(),
            "label_horizon_days": 180,
            "label_horizon_end": label_horizon_end.date().isoformat(),
            "split_by_horizon_end": split,
            "horizon_complete": complete,
            "label_status": "complete" if complete else "right_censored",
            "activity_180d": int(activity) if complete else np.nan,
            "onset_180d": int(onset) if complete else np.nan,
            "ongoing_acceleration_180d": int(ongoing) if complete else np.nan,
            "current_true_rate_mm_y": current_rate,
            "max_delta_rate_next_180d_mm_y": max_delta_rate,
            "max_acceleration_next_180d_mm_y2": max_acceleration,
            "sustained_two_months": sustained_two if complete else np.nan,
            "current_regime_stage": current_stage,
            "first_onset_date": first_onset_date.date().isoformat() if pd.notna(first_onset_date) else None,
            "use_class": "evaluation_only",
        })
    ew_formal = pd.DataFrame(ew_rows)
    save_csv(ew_formal, odir / "early_warning_labels_formal.csv")

    # Profile targets: consecutive full campaigns, coverage >= 0.8 at both ends.
    profile_rows: list[dict[str, Any]] = []
    for profile_id, group in profile_kinematics[profile_kinematics["campaign_type"] == "full"].groupby("profile_id"):
        group = group.sort_values("date").reset_index(drop=True)
        profile_work_n = int((points["profile_id"].eq(profile_id) & points["point_type"].eq("WORK")).sum())
        for index in range(len(group) - 1):
            current = group.iloc[index]
            target = group.iloc[index + 1]
            current_cov = current["n_points_observed"] / max(profile_work_n + 2, 1)
            target_cov = target["n_points_observed"] / max(profile_work_n + 2, 1)
            if current_cov < 0.8 or target_cov < 0.8:
                continue
            profile_rows.append({
                "sample_id": f"{profile_id}::{current['campaign_id']}::{target['campaign_id']}",
                "profile_id": profile_id,
                "current_campaign_id": current["campaign_id"],
                "current_date": current["date"].date().isoformat(),
                "target_campaign_id": target["campaign_id"],
                "target_date": target["date"].date().isoformat(),
                "forecast_horizon_days": int((target["date"] - current["date"]).days),
                "split": split_from_target_date(target["date"]),
                "current_coverage": current_cov,
                "target_coverage": target_cov,
                "target_max_settlement_mm": target["max_settlement_mm"],
                "target_mean_settlement_mm": target["mean_settlement_mm"],
                "target_max_rate_mm_y": target["max_rate_mm_y"],
                "target_max_abs_tilt_mm_m": target["max_abs_tilt_mm_m"],
                "target_max_abs_curvature_mm_m2": target["max_abs_curvature_mm_m2"],
                "target_max_abs_horizontal_strain": target["max_abs_horizontal_strain"],
                "derivation": "derived_from_point_level_forecasts_preferred",
            })
    profile_targets = pd.DataFrame(profile_rows)
    save_csv(profile_targets, odir / "profile_next_full_campaign_targets.csv")

    # Formal feature contract.
    formal_contract = feature_contract_source.copy()
    if "target_campaign_type" not in formal_contract["field"].values:
        formal_contract = pd.concat([
            formal_contract,
            pd.DataFrame([{
                "field": "target_campaign_type",
                "role": "MODEL_FEATURE",
                "allowed": True,
                "reason": "known from the frozen observation plan at prediction time",
            }]),
        ], ignore_index=True)
    save_csv(formal_contract, odir / "formal_feature_contract.csv")

    # Target catalog/contract.
    target_catalog = pd.DataFrame([
        ["T1_RATE_NEXT_PLANNED", "PRIMARY", "point", "regression", "Годовая скорость оседания до следующей плановой targeted-кампании", "365.25*(eta_obs_next-eta_obs_current)/dt_days", "mm/year", "next planned targeted campaign", "leveling_adjusted_epochs.csv", "censor missing planned measurement; never skip forward", "ready"],
        ["T1B_INCREMENT_NEXT_PLANNED", "DERIVED_OUTPUT", "point", "regression-derived", "Приращение оседания", "pred_rate*dt_days/365.25", "mm", "same as T1", "derived from T1", "same as T1", "ready"],
        ["T1C_LEVEL_NEXT_PLANNED", "DERIVED_OUTPUT", "point", "regression-derived", "Накопленное оседание на дату target", "eta_current+pred_increment", "mm positive downward", "same as T1", "derived from T1", "same as T1", "ready"],
        ["T2_RATE_NEXT_AVAILABLE", "AUXILIARY", "point", "regression", "Скорость до следующего фактически успешного измерения", "365.25*delta_eta/dt_days", "mm/year", "next successful observation", "existing next_cycle_targets.csv", "none; may skip failed planned cycle", "available_not_primary"],
        ["T3_RATE_FIXED_180D_TRUE", "EVALUATION_ONLY", "point", "regression", "Синтетическая скорость на фиксированном горизонте 180 суток", "365.25*(eta_true(t+180)-eta_true(t))/180", "mm/year", "180 days", "truth_survey_points_monthly.csv", "right-censor incomplete truth", "synthetic_evaluation_only"],
        ["T4_EW_ACTIVITY_180D", "SECONDARY", "point-origin", "rare-event classification", "Активизация в следующие 180 суток", "max_dv>=25 and max_a>=15 and sustained>=2 months", "binary", "180 days", "future synthetic truth; retrospective rule for real data", "right-censor incomplete window", "ready_with_rare_event_protocol"],
        ["T5_EW_ONSET_180D", "SECONDARY_PRIMARY_WARNING", "point-origin", "rare-event classification", "Начало нового ускоряющего события", "T4=1 and new accelerating/reactivated/step_transition onset", "binary", "180 days", "future synthetic transitions", "right-censor incomplete window", "ready_but_test_small"],
        ["T6_PROFILE_KINEMATICS_NEXT_FULL", "DERIVED_ENGINEERING", "profile", "multi-output derived", "Кинематика профиля на следующем полном цикле", "aggregate point forecasts", "mixed", "next full campaign", "profile_kinematics.csv", "require coverage>=0.8", "ready_as_derived_output"],
    ], columns=["target_id", "priority", "scope", "task_type", "name_ru", "formula", "unit", "horizon", "label_source", "censoring_rule", "status"])
    save_csv(target_catalog, odir / "target_catalog.csv")
    contract_json = {
        "dataset_version": "SKRU1 v3.2",
        "sign_convention": "subsidence positive downward",
        "origin": "after QC and adjustment of the current campaign",
        "primary_target": "T1_RATE_NEXT_PLANNED",
        "split_protocol": {
            "regression": "by target_date",
            "early_warning": "by label_horizon_end",
            "spatial_secondary": ["leave-profile-out", "leave-zone-out"],
            "forbidden": "random row split",
        },
        "uncertainty": {
            "sigma_increment": "sqrt(sigma_current^2 + sigma_target^2) until covariance is available",
            "sigma_rate": "365.25*sigma_increment/horizon_days",
            "weight": "clip(median(sigma_rate^2)/sigma_rate_i^2,0.25,4), normalized to mean 1",
        },
        "metrics": {
            "regression_primary": ["MAE_rate_mm_y", "RMSE_rate_mm_y", "Bias_rate_mm_y"],
            "regression_secondary": ["MAE_increment_mm", "MAE_level_mm", "R2", "coverage_80", "coverage_95", "WIS"],
            "early_warning": ["average_precision", "precision", "recall", "F1", "recall_at_fixed_FPR", "lead_time_days"],
        },
    }
    (odir / "target_contract.json").write_text(json.dumps(contract_json, ensure_ascii=False, indent=2), encoding="utf-8")

    # Dataset/campaign overview.
    overview = pd.DataFrame([
        ["WORK points", int((points["point_type"] == "WORK").sum()), "survey_points.csv"],
        ["Reference points", int((points["point_type"] == "REF").sum()), "survey_points.csv"],
        ["Profiles", profiles["profile_id"].nunique(), "survey_profiles.csv"],
        ["Campaigns", campaigns["campaign_id"].nunique(), "survey_campaigns.csv"],
        ["Full campaigns", int((campaigns["campaign_type"] == "full").sum()), "survey_campaigns.csv"],
        ["Focused campaigns", int((campaigns["campaign_type"] == "focused").sum()), "survey_campaigns.csv"],
        ["Observed membership rows", int(membership["observed"].sum()), "campaign_point_membership.csv"],
        ["Adjusted leveling epochs", len(leveling), "leveling_adjusted_epochs.csv"],
        ["Existing next-cycle samples", len(features), "next_cycle_features.csv"],
        ["Formal primary candidate origins", len(formal_targets), "next_planned_operational_targets.csv"],
        ["Formal primary available labels", int(formal_targets["target_available"].sum()), "next_planned_operational_targets.csv"],
        ["Early-warning complete origins", int(ew_formal["horizon_complete"].sum()), "early_warning_labels_formal.csv"],
        ["Early-warning right-censored origins", int((~ew_formal["horizon_complete"]).sum()), "early_warning_labels_formal.csv"],
        ["Profile target transitions", len(profile_targets), "profile_next_full_campaign_targets.csv"],
    ], columns=["metric", "value", "source_table"])
    save_csv(overview, tdir / "dataset_overview.csv")

    campaign_summary = campaigns[[
        "campaign_id", "date", "campaign_type", "interval_days_from_previous", "long_gap_flag",
        "n_points_targeted", "n_points_observed", "n_points_missing",
        "coverage_fraction_total", "coverage_fraction_targeted",
    ]].copy()
    campaign_summary["date"] = campaign_summary["date"].dt.date.astype(str)
    save_csv(campaign_summary, tdir / "campaign_summary.csv")
    missingness_by_reason = membership[~membership["observed"]].groupby(["membership_status", "missing_reason"], dropna=False).size().reset_index(name="n")
    save_csv(missingness_by_reason, tdir / "campaign_missingness_by_reason.csv")

    # Point observation summary.
    observed_level = leveling[leveling["point_id"].isin(points.loc[points["point_type"] == "WORK", "point_id"])]
    point_obs = observed_level.groupby(["point_id", "profile_id"]).agg(
        n_observations=("campaign_id", "size"),
        first_date=("date", "min"),
        last_date=("date", "max"),
        median_uncertainty_mm=("standard_uncertainty_mm", "median"),
        max_observed_settlement_mm=("observed_settlement_mm", "max"),
    ).reset_index()
    point_obs = point_obs.merge(process_params[["point_id", "process_family"]], on="point_id", how="left")
    point_obs["first_date"] = point_obs["first_date"].dt.date.astype(str)
    point_obs["last_date"] = point_obs["last_date"].dt.date.astype(str)
    save_csv(point_obs, tdir / "point_observation_summary.csv")

    # Target summaries.
    distribution = pd.DataFrame([
        describe_series("observed_next_increment_mm", merged["observed_next_increment_mm"]),
        describe_series("observed_next_rate_mm_y", merged["observed_next_rate_mm_y"]),
        describe_series("hidden_true_next_rate_mm_y", merged["hidden_true_next_rate_mm_y"]),
        describe_series("target_noise_mm", merged["target_noise_mm"]),
        describe_series("current_observed_settlement_mm", merged["last_settlement_mm"]),
    ])
    save_csv(distribution, tdir / "target_distribution_summary.csv")

    z = merged["target_noise_mm"] / merged["target_sigma_increment_mm"]
    noise_calibration = pd.DataFrame([
        ["MAE noise", float(np.mean(np.abs(merged["target_noise_mm"]))), "mm"],
        ["RMSE noise", float(np.sqrt(np.mean(merged["target_noise_mm"] ** 2))), "mm"],
        ["Median target sigma", float(merged["target_sigma_increment_mm"].median()), "mm"],
        ["Coverage ±1 sigma", float((np.abs(z) <= 1.0).mean()), "fraction"],
        ["Coverage ±1.64 sigma", float((np.abs(z) <= 1.64).mean()), "fraction"],
        ["Coverage ±1.96 sigma", float((np.abs(z) <= 1.96).mean()), "fraction"],
        ["Coverage ±3 sigma", float((np.abs(z) <= 3.0).mean()), "fraction"],
        ["Increment vs horizon Pearson", safe_corr(merged["observed_next_increment_mm"], merged["forecast_horizon_days"], "pearson"), "correlation"],
        ["Rate vs horizon Pearson", safe_corr(merged["observed_next_rate_mm_y"], merged["forecast_horizon_days"], "pearson"), "correlation"],
    ], columns=["metric", "value", "unit"])
    save_csv(noise_calibration, tdir / "target_noise_calibration.csv")

    existing_split_summary = merged.groupby("split").agg(
        n=("sample_id", "size"),
        horizon_median_days=("forecast_horizon_days", "median"),
        horizon_max_days=("forecast_horizon_days", "max"),
        increment_median_mm=("observed_next_increment_mm", "median"),
        rate_median_mm_y=("observed_next_rate_mm_y", "median"),
        rate_mean_mm_y=("observed_next_rate_mm_y", "mean"),
        target_noise_mae_mm=("target_noise_mm", lambda s: float(np.mean(np.abs(s)))),
        rate_uncertainty_median_mm_y=("target_sigma_rate_mm_y", "median"),
    ).reset_index()
    save_csv(existing_split_summary, tdir / "existing_next_available_target_summary_by_split.csv")

    formal_available = formal_targets[formal_targets["target_available"]].merge(formal_eval, on=["sample_id", "point_id", "current_date", "target_date"], how="left")
    formal_available["target_noise_mm"] = formal_available["observed_increment_mm"] - formal_available["true_increment_mm"]
    formal_split_summary = formal_available.groupby("split").agg(
        n=("sample_id", "size"),
        horizon_median_days=("forecast_horizon_days", "median"),
        horizon_max_days=("forecast_horizon_days", "max"),
        increment_median_mm=("observed_increment_mm", "median"),
        rate_median_mm_y=("observed_rate_mm_y", "median"),
        rate_mean_mm_y=("observed_rate_mm_y", "mean"),
        target_noise_mae_mm=("target_noise_mm", lambda s: float(np.mean(np.abs(s)))),
        rate_uncertainty_median_mm_y=("sigma_rate_mm_y", "median"),
    ).reset_index()
    save_csv(formal_split_summary, tdir / "formal_primary_target_summary_by_split.csv")

    formal_available["horizon_bin_days"] = formal_available["forecast_horizon_days"].map(horizon_bin)
    formal_horizon_summary = formal_available.groupby(["split", "horizon_bin_days"], observed=False).agg(
        n=("sample_id", "size"),
        rate_median_mm_y=("observed_rate_mm_y", "median"),
        rate_p95_mm_y=("observed_rate_mm_y", lambda s: float(s.quantile(0.95))),
        rate_uncertainty_median_mm_y=("sigma_rate_mm_y", "median"),
        target_noise_mae_mm=("target_noise_mm", lambda s: float(np.mean(np.abs(s)))),
    ).reset_index()
    save_csv(formal_horizon_summary, tdir / "formal_target_summary_by_horizon.csv")

    planned_summary = formal_targets.groupby(["split", "label_status"], dropna=False).size().reset_index(name="n")
    save_csv(planned_summary, tdir / "next_planned_target_summary.csv")

    # Family/stage summaries.
    family_summary = merged.groupby("process_family").agg(
        n_samples=("sample_id", "size"),
        n_points=("point_id", "nunique"),
        observed_rate_median_mm_y=("observed_next_rate_mm_y", "median"),
        observed_rate_mean_mm_y=("observed_next_rate_mm_y", "mean"),
        observed_rate_p95_mm_y=("observed_next_rate_mm_y", lambda s: float(s.quantile(0.95))),
        hidden_rate_median_mm_y=("hidden_true_next_rate_mm_y", "median"),
    ).reset_index().sort_values("observed_rate_median_mm_y")
    save_csv(family_summary, tdir / "existing_target_summary_by_family.csv")

    stage_summary = truth[truth["point_id"].isin(points.loc[points["point_type"] == "WORK", "point_id"])].groupby(["process_family", "regime_stage"]).agg(
        n_months=("date", "size"),
        n_points=("point_id", "nunique"),
        velocity_median_mm_y=("true_velocity_mm_y", "median"),
        velocity_mean_mm_y=("true_velocity_mm_y", "mean"),
        velocity_p95_mm_y=("true_velocity_mm_y", lambda s: float(s.quantile(0.95))),
        acceleration_median_mm_y2=("true_acceleration_mm_y2", "median"),
    ).reset_index().sort_values("velocity_median_mm_y")
    save_csv(stage_summary, tdir / "regime_stage_summary.csv")

    # Feature EDA.
    missingness = pd.DataFrame([
        {
            "field": column,
            "dtype": str(features[column].dtype),
            "missing_n": int(features[column].isna().sum()),
            "missing_fraction": float(features[column].isna().mean()),
            "unique_n": int(features[column].nunique(dropna=True)),
        }
        for column in features.columns
    ]).sort_values(["missing_fraction", "field"], ascending=[False, True])
    save_csv(missingness, tdir / "feature_missingness.csv")

    lineage_rows = []
    for feature_name, group in lineage[lineage["point_type"] == "WORK"].groupby("feature"):
        values = pd.to_numeric(group["value"], errors="coerce")
        uncertainty = pd.to_numeric(group["standard_uncertainty"], errors="coerce")
        distance = pd.to_numeric(group["donor_distance_m"], errors="coerce")
        provenance_distribution = "; ".join(f"{key}:{value}" for key, value in group["provenance"].value_counts(dropna=False).items())
        lineage_rows.append({
            "feature": feature_name,
            "value_missing_fraction": float(values.isna().mean()),
            "uncertainty_missing_fraction": float(uncertainty.isna().mean()),
            "uncertainty_median": float(uncertainty.median()) if uncertainty.notna().any() else np.nan,
            "uncertainty_p95": float(uncertainty.quantile(0.95)) if uncertainty.notna().any() else np.nan,
            "donor_distance_median_m": float(distance.median()) if distance.notna().any() else np.nan,
            "donor_distance_p95_m": float(distance.quantile(0.95)) if distance.notna().any() else np.nan,
            "donor_distance_max_m": float(distance.max()) if distance.notna().any() else np.nan,
            "provenance_distribution": provenance_distribution,
        })
    lineage_summary = pd.DataFrame(lineage_rows)
    save_csv(lineage_summary, tdir / "feature_lineage_summary.csv")

    # Numeric correlations to targets.
    merged_early = merged.merge(early_labels_existing[["sample_id", "early_acceleration_label"]], on="sample_id", how="left")
    numeric_features = [column for column in features.columns if pd.api.types.is_numeric_dtype(features[column])]
    correlation_rows = []
    for column in numeric_features:
        for target_name in ["observed_next_rate_mm_y", "hidden_true_next_rate_mm_y", "early_acceleration_label"]:
            x = merged_early[column]
            y = merged_early[target_name]
            pair = pd.concat([x, y], axis=1).dropna()
            correlation_rows.append({
                "feature": column,
                "target": target_name,
                "n": len(pair),
                "pearson": safe_corr(x, y, "pearson"),
                "spearman": safe_corr(x, y, "spearman"),
            })
    target_correlations = pd.DataFrame(correlation_rows)
    target_correlations["abs_spearman"] = target_correlations["spearman"].abs()
    target_correlations = target_correlations.sort_values(["target", "abs_spearman"], ascending=[True, False])
    save_csv(target_correlations, tdir / "target_correlations.csv")

    merged_early["persistence_residual_observed_mm_y"] = merged_early["observed_next_rate_mm_y"] - merged_early["last_rate_mm_y"]
    merged_early["persistence_residual_true_mm_y"] = merged_early["hidden_true_next_rate_mm_y"] - merged_early["last_rate_mm_y"]
    static_candidates = ["kzt", "ko", "seismic_energy_J_m2", "fill_density", "fault_distance_m", "terrain_TRI_relative", "terrain_roughness_relative", "recent_acceleration_mm_y2", "std_last_3_rates_mm_y", "profile_rate_std_mm_y"]
    persistence_rows = []
    for column in static_candidates:
        for target_name in ["persistence_residual_observed_mm_y", "persistence_residual_true_mm_y"]:
            persistence_rows.append({
                "feature": column,
                "target": target_name,
                "n": int(pd.concat([merged_early[column], merged_early[target_name]], axis=1).dropna().shape[0]),
                "pearson": safe_corr(merged_early[column], merged_early[target_name], "pearson"),
                "spearman": safe_corr(merged_early[column], merged_early[target_name], "spearman"),
            })
    save_csv(pd.DataFrame(persistence_rows), tdir / "persistence_residual_correlations.csv")

    pair_candidates = ["last_rate_mm_y", "mean_last_3_rates_mm_y", "std_last_3_rates_mm_y", "recent_acceleration_mm_y2", "profile_mean_rate_mm_y", "profile_rate_std_mm_y", "chainage_m", "chainage_normalized_profile", "kzt__standard_uncertainty", "kzt__donor_distance_m", "ko__standard_uncertainty", "ko__donor_distance_m", "seismic_energy_J_m2__standard_uncertainty", "seismic_energy_J_m2__donor_distance_m"]
    pair_rows = []
    for i, left in enumerate(pair_candidates):
        for right in pair_candidates[i + 1:]:
            corr = safe_corr(features[left], features[right], "spearman")
            if pd.notna(corr) and abs(corr) >= 0.8:
                pair_rows.append({"feature_a": left, "feature_b": right, "spearman": corr, "abs_spearman": abs(corr)})
    save_csv(pd.DataFrame(pair_rows).sort_values("abs_spearman", ascending=False), tdir / "feature_pair_correlations.csv")

    # Split drift on formal available rows.
    formal_model = formal_features.merge(formal_targets[["sample_id", "target_available"]], on="sample_id", how="left")
    formal_model = formal_model[formal_model["target_available"]]
    numeric_cols = [c for c in formal_model.columns if pd.api.types.is_numeric_dtype(formal_model[c]) and c not in {"target_available"}]
    drift_rows = []
    train = formal_model[formal_model["split"] == "train"]
    for comparison_split in ["validation", "test"]:
        comp = formal_model[formal_model["split"] == comparison_split]
        for column in numeric_cols:
            a = pd.to_numeric(train[column], errors="coerce").dropna()
            b = pd.to_numeric(comp[column], errors="coerce").dropna()
            if len(a) < 2 or len(b) < 2:
                continue
            pooled = math.sqrt((a.var(ddof=1) + b.var(ddof=1)) / 2.0)
            smd = (b.mean() - a.mean()) / pooled if pooled > 0 else np.nan
            drift_rows.append({
                "feature": column,
                "comparison": f"train_vs_{comparison_split}",
                "train_n": len(a),
                "comparison_n": len(b),
                "train_mean": a.mean(),
                "comparison_mean": b.mean(),
                "standardized_mean_difference": smd,
                "abs_smd": abs(smd) if pd.notna(smd) else np.nan,
            })
    numeric_drift = pd.DataFrame(drift_rows).sort_values(["comparison", "abs_smd"], ascending=[True, False])
    save_csv(numeric_drift, tdir / "numeric_split_drift.csv")

    categorical_rows = []
    for column in ["current_campaign_type", "target_campaign_type", "lithology", "kzt__provenance", "ko__provenance", "seismic_energy_J_m2__provenance"]:
        for split, group in formal_model.groupby("split"):
            counts = group[column].fillna("<MISSING>").value_counts(normalize=True)
            for category, fraction in counts.items():
                categorical_rows.append({"feature": column, "split": split, "category": category, "fraction": fraction, "n": len(group)})
    save_csv(pd.DataFrame(categorical_rows), tdir / "categorical_split_drift.csv")

    # Early-warning balance.
    ew_complete = ew_formal[ew_formal["horizon_complete"]]
    ew_balance = []
    for split, group in ew_formal.groupby("split_by_horizon_end"):
        complete_group = group[group["horizon_complete"]]
        ew_balance.append({
            "split": split,
            "origins_total": len(group),
            "complete_n": len(complete_group),
            "right_censored_n": int((~group["horizon_complete"]).sum()),
            "activity_positive_n": int(complete_group["activity_180d"].sum()),
            "activity_positive_fraction": float(complete_group["activity_180d"].mean()) if len(complete_group) else np.nan,
            "onset_positive_n": int(complete_group["onset_180d"].sum()),
            "onset_positive_fraction": float(complete_group["onset_180d"].mean()) if len(complete_group) else np.nan,
            "ongoing_positive_n": int(complete_group["ongoing_acceleration_180d"].sum()),
        })
    save_csv(pd.DataFrame(ew_balance), tdir / "early_warning_formal_balance.csv")

    # Kinematics and reference-only thresholds.
    kinematic_summary = pd.DataFrame([
        describe_series("observed_settlement_mm", leveling[leveling["point_id"].isin(points.loc[points["point_type"] == "WORK", "point_id"])]["observed_settlement_mm"]),
        describe_series("settlement_rate_mm_y", settlement_rates[settlement_rates["point_id"].isin(points.loc[points["point_type"] == "WORK", "point_id"])]["settlement_rate_mm_y"]),
        describe_series("tilt_mm_m", tilts["tilt_mm_m"]),
        describe_series("curvature_mm_m2", curvatures["curvature_mm_m2"]),
        describe_series("horizontal_strain", horizontal_strains["horizontal_strain"]),
        describe_series("horizontal_strain_microstrain", horizontal_strains["horizontal_strain_microstrain"]),
    ])
    save_csv(kinematic_summary, tdir / "kinematic_summary.csv")
    threshold_exceedance = pd.DataFrame([
        ["max_abs_tilt_mm_m > 10", int((profile_kinematics["max_abs_tilt_mm_m"] > 10).sum()), float((profile_kinematics["max_abs_tilt_mm_m"] > 10).mean()), "reference_only_not_enterprise"],
        ["max_abs_tilt_mm_m > 15", int((profile_kinematics["max_abs_tilt_mm_m"] > 15).sum()), float((profile_kinematics["max_abs_tilt_mm_m"] > 15).mean()), "reference_only_not_enterprise"],
    ], columns=["condition", "n_profile_epochs", "fraction", "authority"])
    save_csv(threshold_exceedance, tdir / "profile_reference_threshold_exceedance.csv")

    # Sensor quality.
    level_res = pd.read_csv(source / "evaluation_only/leveling_adjustment_truth_residuals.csv")
    gnss_res = pd.read_csv(source / "evaluation_only/gnss_truth_residuals.csv")
    insar_obs = pd.read_csv(source / "tables/insar_observations_relative.csv")
    insar_truth = pd.read_csv(source / "evaluation_only/insar_truth_relative.csv")
    insar_res = insar_obs.merge(insar_truth[["acquisition_id", "insar_point_id", "true_LOS_relative_mm"]], on=["acquisition_id", "insar_point_id"], validate="one_to_one")
    insar_res["residual_mm"] = insar_res["corrected_LOS_relative_mm"] - insar_res["true_LOS_relative_mm"]
    sensor_rows = []
    for sensor, frame in [("Leveling", level_res), ("GNSS", gnss_res), ("InSAR", insar_res)]:
        residual = pd.to_numeric(frame["residual_mm"], errors="coerce")
        sigma = pd.to_numeric(frame["standard_uncertainty_mm"], errors="coerce")
        mask = residual.notna() & sigma.notna()
        residual = residual[mask]
        sigma = sigma[mask]
        sensor_rows.append({
            "sensor": sensor,
            "n": len(residual),
            "MAE_mm": float(np.mean(np.abs(residual))),
            "RMSE_mm": float(np.sqrt(np.mean(residual ** 2))),
            "Bias_mm": float(np.mean(residual)),
            "coverage_95": float((np.abs(residual) <= 1.96 * sigma).mean()),
            "median_sigma_mm": float(sigma.median()),
        })
    sensor_quality = pd.DataFrame(sensor_rows)
    save_csv(sensor_quality, tdir / "sensor_quality_summary.csv")

    # Formal target sanity baselines.
    history_by_point = {pid: group.sort_values("date") for pid, group in leveling.groupby("point_id")}
    formal_feature_lookup = formal_features.set_index("sample_id")
    prediction_rows = []
    for _, row in formal_available.iterrows():
        feat = formal_feature_lookup.loc[row["sample_id"]]
        horizon_years = row["forecast_horizon_days"] / DAYS_PER_YEAR
        last_pred = float(feat["last_rate_mm_y"]) * horizon_years
        mean3_pred = float(feat["mean_last_3_rates_mm_y"]) * horizon_years
        history = history_by_point[row["point_id"]]
        history = history[history["date"] <= pd.Timestamp(row["current_date"])]
        kalman_level = kalman_predict(history, pd.Timestamp(row["target_date"]), q=250.0)
        kalman_pred = kalman_level - float(feat["last_settlement_mm"])
        prediction_rows.append({
            "sample_id": row["sample_id"],
            "split": row["split"],
            "observed_increment_mm": row["observed_increment_mm"],
            "hidden_true_increment_mm": row["true_increment_mm"],
            "forecast_horizon_days": row["forecast_horizon_days"],
            "Last rate": last_pred,
            "Mean last 3 rates": mean3_pred,
            "Kalman q=250": kalman_pred,
        })
    predictions = pd.DataFrame(prediction_rows)
    save_csv(predictions, tdir / "formal_target_sanity_predictions.csv")
    metric_rows = []
    for split in ["validation", "test"]:
        subset = predictions[predictions["split"] == split]
        horizons = subset["forecast_horizon_days"].to_numpy(float)
        for target_name, column in [("observed", "observed_increment_mm"), ("hidden_truth", "hidden_true_increment_mm")]:
            actual = subset[column].to_numpy(float)
            for model in ["Last rate", "Mean last 3 rates", "Kalman q=250"]:
                metric_rows.append(metric_row(split, target_name, model, actual, subset[model].to_numpy(float), horizons))
    baseline_metrics = pd.DataFrame(metric_rows)
    save_csv(baseline_metrics, tdir / "formal_target_sanity_baselines.csv")

    # Issues.
    issues = pd.DataFrame([
        ["EDA-001", "HIGH", f"{len(mismatch)} membership rows are marked observed but have no adjusted leveling epoch.", "Use adjusted leveling as label authority; patch P-H04/C005 and P-H07/C010 membership statuses in the next dataset revision."],
        ["EDA-002", "HIGH", f"The existing next-cycle target is next successful observation; {int((~alignment).sum())} rows do not point to the next planned targeted campaign.", "Use T1_RATE_NEXT_PLANNED as the primary operational target."],
        ["EDA-003", "HIGH", f"{int((~ew_formal['horizon_complete']).sum())} early-warning origins do not have a complete 180-day truth horizon.", "Exclude as right-censored or extend truth to at least 2026-03 before final early-warning experiments."],
        ["EDA-004", "HIGH", "After strict horizon-end splitting, nominal early-warning test size is very small.", "Extend temporal coverage and keep stress/OOD results separate from nominal results."],
        ["EDA-005", "MEDIUM", "Terrain TRI and roughness are missing in 32.4% of next-cycle feature rows.", "Use explicit missing indicators and report performance by missingness/provenance; never replace unknown uncertainty with zero."],
        ["EDA-006", "MEDIUM", "Lithology uncertainty is absent for all rows.", "Keep provenance as categorical quality information; do not fabricate numeric uncertainty."],
        ["EDA-007", "MEDIUM", "Several uncertainty columns are deterministic functions of donor distance.", "Run ablations and regularize; do not interpret both as independent evidence."],
        ["EDA-008", "HIGH", "1274 rows represent only 98 dependent trajectories in 14 profiles.", "Random row split is prohibited; use temporal and leave-profile/zone-out protocols."],
        ["EDA-009", "MEDIUM", "Static reconstructed features have weak marginal association with rate change after persistence is removed.", "Any geomechanical claim requires improvement over history-only baselines on temporal and spatial holdouts."],
        ["EDA-010", "HIGH", "Enterprise deformation limits are absent.", "Do not convert reference or illustrative thresholds into official risk labels."],
    ], columns=["issue_id", "severity", "finding", "required_action"])
    save_csv(issues, mdir / "eda_issue_register.csv")

    # Formal validation checks.
    forbidden_patterns = ["settlement_anchor_map", "x_local", "y_local", "process_family", "regime_stage", "base_rate", "event_onset", "event_amplitude", "decay_tau", "true_"]
    feature_columns_lower = [c.lower() for c in formal_features.columns]
    formula_error = np.nanmax(np.abs(formal_targets.loc[available_mask, "observed_rate_mm_y"] - formal_targets.loc[available_mask, "observed_increment_mm"] / formal_targets.loc[available_mask, "forecast_horizon_days"] * DAYS_PER_YEAR))
    checks = [
        ["TGT-001", "critical", "PASS" if formal_features["sample_id"].is_unique else "FAIL", formal_features["sample_id"].nunique(), len(formal_features), "Formal feature sample IDs must be unique."],
        ["TGT-002", "critical", "PASS" if formal_targets["sample_id"].is_unique else "FAIL", formal_targets["sample_id"].nunique(), len(formal_targets), "Formal target sample IDs must be unique."],
        ["TGT-003", "critical", "PASS" if set(formal_features["sample_id"]) == set(formal_targets["sample_id"]) else "FAIL", len(set(formal_features["sample_id"]) ^ set(formal_targets["sample_id"])), 0, "Features and operational targets must share the same sample universe."],
        ["TGT-004", "critical", "PASS" if (formal_targets["forecast_horizon_days"] > 0).all() else "FAIL", int((formal_targets["forecast_horizon_days"] <= 0).sum()), 0, "Forecast horizons must be positive."],
        ["TGT-005", "critical", "PASS" if not any(any(pattern in c for pattern in forbidden_patterns) for c in feature_columns_lower) else "FAIL", "forbidden pattern scan", "none", "No latent or terminal-map leakage in formal features."],
        ["TGT-006", "critical", "PASS" if formula_error < 1e-9 else "FAIL", formula_error, 0.0, "Rate target formula consistency."],
        ["TGT-007", "critical", "PASS" if int(formal_targets["target_available"].sum()) == 1216 else "FAIL", int(formal_targets["target_available"].sum()), 1216, "Available primary labels."],
        ["TGT-008", "critical", "PASS" if int((formal_targets["label_status"].str.startswith("censored")).sum()) == 52 else "FAIL", int((formal_targets["label_status"].str.startswith("censored")).sum()), 52, "Planned missing measurements must be censored."],
        ["TGT-009", "critical", "PASS" if int((formal_targets["label_status"] == "observed_but_no_adjusted_leveling").sum()) == 6 else "FAIL", int((formal_targets["label_status"] == "observed_but_no_adjusted_leveling").sum()), 6, "Membership/leveling inconsistencies remain unlabeled."],
        ["TGT-010", "critical", "PASS" if int((~ew_formal["horizon_complete"]).sum()) == 93 else "FAIL", int((~ew_formal["horizon_complete"]).sum()), 93, "Incomplete early-warning horizons must be right-censored."],
        ["TGT-011", "critical", "PASS" if np.isclose((np.abs(z) <= 1.96).mean(), 0.95, atol=0.02) else "FAIL", float((np.abs(z) <= 1.96).mean()), "0.95±0.02", "Synthetic target uncertainty should be calibrated."],
        ["TGT-012", "critical", "PASS" if len(profile_targets) > 0 else "FAIL", len(profile_targets), ">0", "Profile-level derived target table must exist."],
    ]
    validation = pd.DataFrame(checks, columns=["check_id", "severity", "status", "observed", "expected", "note"])
    save_csv(validation, mdir / "target_validation_checks.csv")
    (mdir / "target_validation_report.json").write_text(json.dumps({
        "checks_total": len(validation),
        "checks_passed": int((validation["status"] == "PASS").sum()),
        "checks_failed": int((validation["status"] != "PASS").sum()),
        "overall_status": "PASS" if (validation["status"] == "PASS").all() else "FAIL",
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # Figures. Each figure is separate, no subplots and default colors.
    family_counts = process_params[process_params["point_type"] == "WORK"]["process_family"].value_counts().sort_values(ascending=False)
    plt.figure(figsize=(9, 5)); family_counts.plot(kind="bar"); plt.ylabel("WORK points"); plt.title("Process-family balance"); plt.tight_layout(); plt.savefig(fdir / "01_process_family_balance.png", dpi=180); plt.close()
    plt.figure(figsize=(10, 5)); plt.plot(campaigns["date"], campaigns["coverage_fraction_total"], marker="o"); plt.ylabel("Coverage of all points"); plt.title("Campaign timeline and coverage"); plt.xticks(rotation=45); plt.tight_layout(); plt.savefig(fdir / "02_campaign_timeline_coverage.png", dpi=180); plt.close()
    plt.figure(figsize=(9, 5)); plt.hist(point_obs["n_observations"], bins=range(int(point_obs["n_observations"].min()), int(point_obs["n_observations"].max()) + 2)); plt.xlabel("Observed epochs per WORK point"); plt.ylabel("Points"); plt.title("Observation-count distribution"); plt.tight_layout(); plt.savefig(fdir / "03_observations_per_point.png", dpi=180); plt.close()
    plt.figure(figsize=(9, 5)); merged.boxplot(column="observed_next_rate_mm_y", by="split", grid=False); plt.suptitle(""); plt.title("Observed next-interval rate by split"); plt.ylabel("mm/year"); plt.tight_layout(); plt.savefig(fdir / "04_target_rate_by_split.png", dpi=180); plt.close()
    plt.figure(figsize=(9, 5)); plt.scatter(merged["forecast_horizon_days"], merged["observed_next_increment_mm"], s=10, alpha=0.35); plt.xlabel("Horizon, days"); plt.ylabel("Increment, mm"); plt.title("Increment depends on horizon"); plt.tight_layout(); plt.savefig(fdir / "05_increment_vs_horizon.png", dpi=180); plt.close()
    plt.figure(figsize=(9, 5)); plt.hist(z, bins=40); plt.xlabel("Target noise / target sigma"); plt.ylabel("Rows"); plt.title("Target-noise calibration"); plt.tight_layout(); plt.savefig(fdir / "06_target_noise_calibration.png", dpi=180); plt.close()
    plt.figure(figsize=(10, 5)); order = family_summary["process_family"].tolist(); data = [merged.loc[merged["process_family"] == fam, "observed_next_rate_mm_y"].dropna().to_numpy() for fam in order]; plt.boxplot(data, tick_labels=order, showfliers=False); plt.xticks(rotation=30); plt.ylabel("mm/year"); plt.title("Observed target rate by process family (evaluation only)"); plt.tight_layout(); plt.savefig(fdir / "07_target_rate_by_family.png", dpi=180); plt.close()
    plt.figure(figsize=(9, 5)); ew_complete[["activity_180d", "onset_180d"]].sum().plot(kind="bar"); plt.ylabel("Positive origins"); plt.title("Formal early-warning positives"); plt.tight_layout(); plt.savefig(fdir / "08_early_warning_balance.png", dpi=180); plt.close()
    stage_order = stage_summary["regime_stage"].tolist(); stage_data = [truth.loc[truth["regime_stage"] == stage, "true_velocity_mm_y"].dropna().to_numpy() for stage in stage_order]; plt.figure(figsize=(12, 6)); plt.boxplot(stage_data, tick_labels=stage_order, showfliers=False); plt.xticks(rotation=45, ha="right"); plt.ylabel("mm/year"); plt.title("True velocity by dynamic regime stage"); plt.tight_layout(); plt.savefig(fdir / "09_velocity_by_regime_stage.png", dpi=180); plt.close()
    plt.figure(figsize=(9, 5)); plt.bar(["existing next-available", "formal next-planned"], [len(merged), int(formal_targets["target_available"].sum())]); plt.ylabel("Available labels"); plt.title("Target semantics comparison"); plt.tight_layout(); plt.savefig(fdir / "10_target_semantics_comparison.png", dpi=180); plt.close()
    plt.figure(figsize=(10, 5)); for_plot = formal_horizon_summary[formal_horizon_summary["n"] > 0]; labels = for_plot["split"] + ":" + for_plot["horizon_bin_days"].astype(str); plt.bar(labels, for_plot["rate_median_mm_y"]); plt.xticks(rotation=60, ha="right"); plt.ylabel("Median rate, mm/year"); plt.title("Formal target by horizon bin"); plt.tight_layout(); plt.savefig(fdir / "11_formal_target_by_horizon.png", dpi=180); plt.close()
    for number, family in enumerate(["stable", "uniform_creep", "decaying", "accelerating", "reactivated", "step_change"], start=12):
        candidates = process_params[(process_params["point_type"] == "WORK") & (process_params["process_family"] == family)]["point_id"]
        if candidates.empty:
            continue
        point_id = candidates.iloc[0]
        group = truth[truth["point_id"] == point_id]
        plt.figure(figsize=(9, 5)); plt.plot(group["date"], group["true_velocity_mm_y"]); plt.ylabel("True velocity, mm/year"); plt.title(f"Example history: {family} ({point_id})"); plt.xticks(rotation=45); plt.tight_layout(); plt.savefig(fdir / f"{number:02d}_history_{family}.png", dpi=180); plt.close()
    plt.figure(figsize=(8, 5)); plt.bar(sensor_quality["sensor"], sensor_quality["RMSE_mm"]); plt.ylabel("RMSE, mm"); plt.title("Synthetic sensor residuals"); plt.tight_layout(); plt.savefig(fdir / "18_sensor_quality.png", dpi=180); plt.close()

    # Key numbers and reports.
    key_numbers = {
        "work_points": int((points["point_type"] == "WORK").sum()),
        "reference_points": int((points["point_type"] == "REF").sum()),
        "profiles": profiles["profile_id"].nunique(),
        "campaigns": campaigns["campaign_id"].nunique(),
        "full_campaigns": int((campaigns["campaign_type"] == "full").sum()),
        "focused_campaigns": int((campaigns["campaign_type"] == "focused").sum()),
        "target_rows_existing": len(merged),
        "target_rows_planned": len(formal_targets),
        "target_labels_planned": int(formal_targets["target_available"].sum()),
        "target_censored_planned": int(formal_targets["label_status"].str.startswith("censored").sum()),
        "membership_observed_without_leveling": len(mismatch),
        "formal_origins_observed_but_no_adjustment": int((formal_targets["label_status"] == "observed_but_no_adjusted_leveling").sum()),
        "existing_not_next_planned": int((~alignment).sum()),
        "negative_observed_rates": int((merged["observed_next_rate_mm_y"] < 0).sum()),
        "early_complete": int(ew_formal["horizon_complete"].sum()),
        "early_right_censored": int((~ew_formal["horizon_complete"]).sum()),
        "early_activity_positive": int(ew_complete["activity_180d"].sum()),
        "early_onset_positive": int(ew_complete["onset_180d"].sum()),
        "target_noise_coverage95": float((np.abs(z) <= 1.96).mean()),
        "increment_horizon_pearson": safe_corr(merged["observed_next_increment_mm"], merged["forecast_horizon_days"], "pearson"),
        "rate_horizon_pearson": safe_corr(merged["observed_next_rate_mm_y"], merged["forecast_horizon_days"], "pearson"),
        "profile_target_rows": len(profile_targets),
    }
    (mdir / "eda_key_numbers.json").write_text(json.dumps(key_numbers, ensure_ascii=False, indent=2), encoding="utf-8")

    report = rf"""# EDA SKRU-1 v3.2 и формальная постановка целевых переменных

## 1. Статус

**Данные пригодны для EDA, baseline-экспериментов и разработки алгоритма.** Они не являются производственными журналами СКРУ-1 и не доказывают реальную прогнозную точность.

Главный результат: текущая таблица `next_cycle_targets.csv` описывает **следующее успешное измерение**, а не всегда следующий плановый цикл. Сформирован новый primary target frame: **следующая плановая кампания, в которой конкретный пункт заранее включён в программу наблюдений**. Сорванный цикл цензурируется; target не прыгает вперёд к более позднему измерению.

## 2. Объём и структура

- рабочих реперов: **{key_numbers['work_points']}**;
- опорных реперов: **{key_numbers['reference_points']}**;
- профилей: **{key_numbers['profiles']}**;
- кампаний: **{key_numbers['campaigns']}**, из них {key_numbers['full_campaigns']} полных и {key_numbers['focused_campaigns']} focused;
- медиана наблюдений на рабочий репер: **{point_obs['n_observations'].median():.1f}**;
- интервалы между кампаниями: **{campaigns['interval_days_from_previous'].min():.0f}–{campaigns['interval_days_from_previous'].max():.0f} суток**.

Focused-циклы действительно локальные. Пропуски отделены от `not_targeted`.

## 3. Критическая несогласованность

В `campaign_point_membership.csv` найдено **{len(mismatch)}** строк `observed=True`, для которых нет уравнённой эпохи в `leveling_adjusted_epochs.csv`. Для labels источником истины назначена уравнённая таблица. В формальном target frame это даёт 6 origins со статусом `observed_but_no_adjusted_leveling`; они исключаются из loss.

## 4. Почему primary target — скорость

Существующая выборка содержит {len(merged)} origins. Для каждого интервала:

\[
y_{{\Delta\eta}}=\eta^{{obs}}(t_{{next}})-\eta^{{obs}}(t_k),
\]

\[
y_v=\frac{{365.25}}{{\Delta t_{{days}}}}y_{{\Delta\eta}}.
\]

Приращение коррелирует с длиной горизонта: Pearson \(r={key_numbers['increment_horizon_pearson']:.3f}\). Для годовой скорости связь слабее: \(r={key_numbers['rate_horizon_pearson']:.3f}\). Поэтому основная регрессионная цель — **мм/год**, а приращение и следующий накопленный уровень выводятся из неё.

Наблюдаемые скорости лежат от {merged['observed_next_rate_mm_y'].min():.1f} до {merged['observed_next_rate_mm_y'].max():.1f} мм/год; медиана {merged['observed_next_rate_mm_y'].median():.1f}. Отрицательных labels — {key_numbers['negative_observed_rates']}; их нельзя клиппировать до нуля, иначе появится положительный bias.

## 5. Неопределённость target

До появления ковариационной матрицы:

\[
\sigma_{{\Delta\eta}}=\sqrt{{\sigma_k^2+\sigma_{{k+1}}^2}},
\qquad
\sigma_v=\frac{{365.25}}{{\Delta t_{{days}}}}\sigma_{{\Delta\eta}}.
\]

Покрытие \(\pm1.96\sigma\) относительно synthetic truth равно **{100*key_numbers['target_noise_coverage95']:.1f}%**. После получения ковариаций формула должна учитывать \(-2\operatorname{{Cov}}(k,k+1)\).

## 6. Primary frame T1

Из {len(formal_targets)} origins:

- доступно **{key_numbers['target_labels_planned']}** labels;
- **{key_numbers['target_censored_planned']}** цензурировано фактическим пропуском;
- **{key_numbers['formal_origins_observed_but_no_adjustment']}** имеют ложный `observed` без уравнённой отметки.

Existing next-available цель расходится со следующим плановым targeted-циклом в **{key_numbers['existing_not_next_planned']}** строках. Она остаётся auxiliary.

## 7. Feature EDA

`terrain_TRI_relative` и `terrain_roughness_relative` отсутствуют примерно в 32.4% строк. Для `lithology` количественная неопределённость не задана во всех строках; это не ноль. Нужны missing indicators и срезы качества по provenance/donor distance.

Последняя скорость и средняя трёх скоростей почти дублируют друг друга. После вычитания persistence-baseline статические реконструированные поля слабо связаны с остатком изменения скорости. Claim о влиянии геологии допустим только при устойчивом улучшении temporal и leave-profile-out метрик относительно history-only baseline.

## 8. Split и зависимость наблюдений

{len(features)} строк принадлежат {key_numbers['work_points']} траекториям в {key_numbers['profiles']} профилях. Random row split запрещён. Regression split выполняется по target date, early warning — по концу 180-дневного окна. Обязательны temporal, leave-profile-out и leave-zone-out оценки.

## 9. Early warning

T4 activity положителен, если за 180 суток одновременно:

\[
\max(v(u)-v(t))\ge 25\ \text{{мм/год}},
\]

\[
\max a(u)\ge 15\ \text{{мм/год}}^2,
\]

и скорость выше текущей минимум на 20 мм/год держится не менее двух месяцев.

T5 onset требует нового перехода в `accelerating`, `reactivated` или `step_transition` после origin. Полных окон: **{key_numbers['early_complete']}**, right-censored: **{key_numbers['early_right_censored']}**, activity-positive: **{key_numbers['early_activity_positive']}**, onset-positive: **{key_numbers['early_onset_positive']}**. До финального early-warning теста truth нужно продлить минимум до марта 2026 года либо сдвинуть границу теста.

## 10. Профильные outputs

Создано {key_numbers['profile_target_rows']} перехода между полными кампаниями при coverage не ниже 0.8. Сначала прогнозируются point-level значения, затем рассчитываются максимальные оседание, скорость, наклон, кривизна и горизонтальная деформация.

## 11. Иерархия задач

1. `T1_RATE_NEXT_PLANNED` — primary regression.
2. `T1B/T1C` — derived increment и next level.
3. `T2_RATE_NEXT_AVAILABLE` — auxiliary compatibility target.
4. `T3_RATE_FIXED_180D_TRUE` — synthetic evaluation only.
5. `T4_EW_ACTIVITY_180D` — secondary rare-event task.
6. `T5_EW_ONSET_180D` — собственно раннее предупреждение.
7. `T6_PROFILE_KINEMATICS_NEXT_FULL` — derived engineering outputs.

## 12. Метрики

T1: MAE/RMSE/Bias скорости в мм/год; вторично MAE приращения и уровня в мм, interval coverage и WIS. T4/T5: average precision, precision, recall, F1, recall при фиксированном FPR и lead time. Accuracy на естественном дисбалансе неинформативна.
"""
    (output / "EDA_REPORT_RU.md").write_text(report, encoding="utf-8")

    target_spec = """# Формальная постановка целевых переменных SKRU-1 v3.2

## Единица прогнозирования

Рабочий репер `point_id` после контроля качества и уравнивания текущей кампании. Оседание положительно вниз.

## T1 — скорость до следующей плановой targeted-кампании

`target_campaign` — первая будущая кампания, где данный пункт заранее имеет `targeted=True`.

`y_rate_obs = 365.25 * (eta_obs_target - eta_obs_current) / horizon_days`.

Если target measurement отсутствует, label цензурируется. Перескакивать к следующему успешному измерению запрещено.

## Производные T1B/T1C

`pred_increment_mm = pred_rate_mm_y * horizon_days / 365.25`.

`pred_next_settlement_mm = current_observed_settlement_mm + pred_increment_mm`.

## Неопределённость и веса

`sigma_increment = sqrt(sigma_current^2 + sigma_target^2)` до появления ковариационной матрицы.

`sigma_rate = 365.25 * sigma_increment / horizon_days`.

`w = clip(median(sigma_rate^2)/sigma_rate_i^2, 0.25, 4.0)` с последующей нормировкой до среднего 1.

## T2 — next available observation

Auxiliary target для совместимости. Может пропустить сорванную плановую кампанию и потому не является основной операционной задачей.

## T3 — fixed horizon 180d

Synthetic evaluation only: `365.25*(eta_true(t+180)-eta_true(t))/180`.

## T4 — activity in 180d

Positive при `max delta_v >= 25 mm/y`, `max acceleration >= 15 mm/y^2` и сохранении `v >= v0+20 mm/y` минимум два последовательных месяца.

## T5 — onset in 180d

Positive, если T4=1 и новое событие accelerating/reactivated/step_transition начинается после origin. Неполное окно — right-censored.

## T6 — profile outputs

На следующем полном цикле с coverage >=0.8 рассчитываются max settlement, max rate, max absolute tilt, curvature и horizontal strain. Предпочтительная реализация — агрегация point-level прогнозов.

## Split

Regression: по target date. Early warning: по label horizon end. Headline evaluation: temporal test и leave-profile/leave-zone-out. Random row split запрещён.
"""
    (output / "TARGET_SPECIFICATION_RU.md").write_text(target_spec, encoding="utf-8")

    readme = """# SKRU-1 v3.2 — EDA и target specification v1

Пакет содержит воспроизводимый EDA, formal target frames, feature contract, early-warning labels, профильные outputs, sanity baselines, issue register и validation checks.

Главный файл для обучения primary regression: `target_tables/next_planned_features.csv` + строки `target_available=True` из `target_tables/next_planned_operational_targets.csv`.

`evaluation_only`-таблицы предназначены только для проверки synthetic consistency и не передаются estimator.
"""
    (output / "README.md").write_text(readme, encoding="utf-8")

    # Copy source registries for completeness.
    shutil.copy2(source / "model_ready/feature_contract.csv", mdir / "source_feature_contract_v3_2.csv")
    threshold_candidates = list((source / "metadata").glob("*threshold*"))
    if threshold_candidates:
        shutil.copy2(threshold_candidates[0], mdir / "threshold_registry_v3_2.csv")

    # Artifact manifest, after all files except itself.
    manifest_rows = []
    for path in sorted(output.rglob("*")):
        if path.is_file() and path.name != "artifact_manifest.csv":
            manifest_rows.append({
                "relative_path": str(path.relative_to(output)),
                "bytes": path.stat().st_size,
                "sha256": sha256(path),
            })
    save_csv(pd.DataFrame(manifest_rows), mdir / "artifact_manifest.csv")

    print(json.dumps({
        "output": str(output),
        "formal_primary_candidates": len(formal_targets),
        "formal_primary_labels": int(formal_targets["target_available"].sum()),
        "early_warning_complete": int(ew_formal["horizon_complete"].sum()),
        "early_warning_right_censored": int((~ew_formal["horizon_complete"]).sum()),
        "target_checks_pass": int((validation["status"] == "PASS").sum()),
        "target_checks_total": len(validation),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    main(args.source, args.output)

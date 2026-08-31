"""Aggregation, screening, calibration, and sensitivity logic for Gate B6."""

from __future__ import annotations

import json
from statistics import NormalDist
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from .benchmark_metrics import (
    apply_scaled_conformal,
    fit_scaled_conformal,
    interval_metrics,
    leave_one_cluster_out_jackknife,
    mase_denominator_from_train,
    normal_crps,
    normal_nll,
    paired_cluster_sensitivity,
    point_metrics,
)
from .b6_probabilistic import quantile_crps_approximation
from .data_contracts import ContractViolation


def canonical_prediction_rows(predictions: pd.DataFrame) -> pd.DataFrame:
    """Use the fixed-seed ensemble when present, otherwise the sole fixed seed."""

    frames: list[pd.DataFrame] = []
    for model_id, model in predictions.groupby("model_id", sort=True):
        ensemble = model.loc[model["aggregation"].eq("mean_of_fixed_seeds")]
        selected = ensemble if not ensemble.empty else model.loc[model["aggregation"].eq("single_seed")]
        seeds = selected["seed"].unique()
        if len(seeds) != 1:
            # Multiple individual seeds without their frozen aggregate are an
            # incomplete shard, not an invitation to pick the best seed.
            raise ContractViolation(f"Model {model_id} has no unique canonical seed/ensemble")
        frames.append(selected)
    return pd.concat(frames, ignore_index=True)


def temporal_fold_metrics(
    predictions: pd.DataFrame,
    source: pd.DataFrame,
    outer_assignments: pd.DataFrame,
) -> pd.DataFrame:
    canonical = canonical_prediction_rows(predictions)
    reference = canonical.loc[
        canonical["model_id"].eq("B1_persistence_last_rate"),
        ["fold_id", "sample_id", "y_pred"],
    ].rename(columns={"y_pred": "b1_prediction"})
    canonical = canonical.merge(reference, on=["fold_id", "sample_id"], how="left", validate="many_to_one")
    if canonical["b1_prediction"].isna().any():
        raise ContractViolation("Temporal predictions do not have exact paired B1 rows")
    indexed = source.set_index("sample_id", drop=False)
    rows: list[dict[str, Any]] = []
    for (model_id, family, fold_id), frame in canonical.groupby(
        ["model_id", "family", "fold_id"], sort=True
    ):
        train_ids = tuple(
            outer_assignments.loc[
                outer_assignments["fold_id"].eq(fold_id) & outer_assignments["role"].eq("train"),
                "sample_id",
            ].astype(str)
        )
        training = indexed.loc[list(train_ids)].reset_index(drop=True)
        weights = precision_weights_from_train(training, frame)
        metrics = point_metrics(
            frame["y_true"],
            frame["y_pred"],
            sample_weight=weights,
            b1_prediction=frame["b1_prediction"],
            mase_denominator=mase_denominator_from_train(training),
            last_rate=frame["last_rate_mm_y"],
            neutral_zone=1.96 * pd.to_numeric(frame["sigma_rate_mm_y"], errors="coerce").fillna(0),
        )
        rows.append(
            {
                "model_id": model_id,
                "family": family,
                "fold_id": fold_id,
                "target_date": pd.Timestamp(pd.to_datetime(frame["target_date"]).iloc[0]).date().isoformat(),
                "rows": len(frame),
                "points": int(frame["point_id"].astype(str).nunique()),
                "profiles": int(frame["profile_id"].astype(str).nunique()),
                "zones": int(frame["zone_id"].astype(str).nunique()),
                "fit_seconds": float(frame["fit_seconds"].iloc[0]),
                "inference_seconds": float(frame["inference_seconds"].iloc[0]),
                "peak_ram_mb": float(frame["peak_ram_mb"].iloc[0]),
                "peak_vram_mb": float(frame["peak_vram_mb"].iloc[0]),
                **metrics,
            }
        )
    result = pd.DataFrame(rows)
    b1 = result.loc[result["model_id"].eq("B1_persistence_last_rate"), ["fold_id", "mae"]].rename(
        columns={"mae": "b1_fold_mae"}
    )
    result = result.merge(b1, on="fold_id", how="left", validate="many_to_one")
    result["fold_mae_ratio_vs_b1"] = result["mae"] / result["b1_fold_mae"]
    return result.sort_values(["model_id", "target_date"], kind="mergesort").reset_index(drop=True)


def temporal_aggregate_metrics(predictions: pd.DataFrame, fold_metrics: pd.DataFrame) -> pd.DataFrame:
    canonical = canonical_prediction_rows(predictions)
    reference = canonical.loc[
        canonical["model_id"].eq("B1_persistence_last_rate"), ["sample_id", "y_pred"]
    ].rename(columns={"y_pred": "b1_prediction"})
    canonical = canonical.merge(reference, on="sample_id", how="left", validate="many_to_one")
    rows: list[dict[str, Any]] = []
    for (model_id, family), frame in canonical.groupby(["model_id", "family"], sort=True):
        folds = fold_metrics.loc[fold_metrics["model_id"].eq(model_id)]
        metrics = point_metrics(
            frame["y_true"],
            frame["y_pred"],
            b1_prediction=frame["b1_prediction"],
            last_rate=frame["last_rate_mm_y"],
            neutral_zone=1.96 * pd.to_numeric(frame["sigma_rate_mm_y"], errors="coerce").fillna(0),
        )
        profile_mae = frame.assign(
            absolute_error=np.abs(frame["y_pred"] - frame["y_true"])
        ).groupby("profile_id")["absolute_error"].mean()
        zone_mae = frame.assign(
            absolute_error=np.abs(frame["y_pred"] - frame["y_true"])
        ).groupby("zone_id")["absolute_error"].mean()
        rows.append(
            {
                "model_id": model_id,
                "family": family,
                "rolling_folds": int(folds["fold_id"].nunique()),
                "pooled_rows": len(frame),
                **metrics,
                "median_fold_mae": float(folds["mae"].median()),
                "iqr_fold_mae": float(folds["mae"].quantile(0.75) - folds["mae"].quantile(0.25)),
                "min_fold_mae": float(folds["mae"].min()),
                "max_fold_mae": float(folds["mae"].max()),
                "fold_mae_range": float(folds["mae"].max() - folds["mae"].min()),
                "max_fold_mae_ratio_vs_b1": float(folds["fold_mae_ratio_vs_b1"].max()),
                "equal_profile_macro_mae": float(profile_mae.mean()),
                "equal_zone_macro_mae": float(zone_mae.mean()),
                "worst_profile_mae": float(profile_mae.max()),
                "worst_zone_mae": float(zone_mae.max()),
                "worst_10_percent_points_mae": worst_point_fraction_mae(frame, 0.10),
                "fit_seconds_total": float(folds["fit_seconds"].sum()),
                "inference_seconds_total": float(folds["inference_seconds"].sum()),
                "peak_ram_mb_max": float(folds["peak_ram_mb"].max()),
                "peak_vram_mb_max": float(folds["peak_vram_mb"].max()),
            }
        )
    result = pd.DataFrame(rows)
    b1 = result.loc[result["model_id"].eq("B1_persistence_last_rate")].iloc[0]
    result["pooled_mae_ratio_vs_b1"] = result["mae"] / float(b1["mae"])
    result["median_fold_mae_ratio_vs_b1"] = result["median_fold_mae"] / float(b1["median_fold_mae"])
    return result.sort_values(["mae", "model_id"], kind="mergesort").reset_index(drop=True)


def screening_register(
    aggregate: pd.DataFrame,
    fold_metrics: pd.DataFrame,
    registry: Mapping[str, Any],
    worker_status: Mapping[str, Mapping[str, Any]],
    config: Mapping[str, Any],
    *,
    excluded_models: Mapping[str, Mapping[str, Any]] | None = None,
) -> pd.DataFrame:
    policy = config["temporal_screen"]
    registry_by_id = {item["model_id"]: item for item in registry["models"]}
    excluded_models = excluded_models or {}
    rows: list[dict[str, Any]] = []
    for model_id, spec in sorted(registry_by_id.items()):
        if model_id in excluded_models:
            exclusion = excluded_models[model_id]
            checks = {
                "excluded_before_screen_aggregation": True,
                "predictions_existed_at_exclusion": bool(
                    exclusion.get("predictions_existed_at_exclusion", False)
                ),
                "license_accepted": bool(exclusion.get("license_accepted", False)),
                "weights_downloaded": bool(exclusion.get("weights_downloaded", False)),
                "external_api_used": bool(exclusion.get("external_api_used", False)),
            }
            rows.append(
                {
                    "model_id": model_id,
                    "family": spec["family"],
                    "environment_id": spec["environment_id"],
                    "registry_status": spec["status"],
                    "screen_status": str(exclusion["execution_status"]),
                    "screen_passed": False,
                    "advanced_to_robustness": False,
                    "checks_json": json.dumps(checks, sort_keys=True, separators=(",", ":")),
                    "observed_json": json.dumps(
                        {
                            "scientific_status": exclusion.get("scientific_status"),
                            "selection_evidence_used_for_exclusion": False,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                }
            )
            continue
        metric = aggregate.loc[aggregate["model_id"].eq(model_id)]
        status = worker_status.get(model_id, {})
        worker_state = str(status.get("status", "MISSING"))
        complete_worker = worker_state == "PASS"
        inner_selection_rejections = status.get("inner_selection_rejections", [])
        if metric.empty:
            checks = {
                "all_11_folds_complete": False,
                "finite_exact_predictions": False,
                "pooled_mae_within_10_percent_b1": False,
                "median_mae_within_10_percent_b1": False,
                "no_fold_exceeds_2x_b1": False,
                "no_worker_or_convergence_failure": complete_worker,
                "all_inner_selections_passed_guardrails": not inner_selection_rejections,
            }
            passed = False
            observed: dict[str, Any] = {}
        else:
            row = metric.iloc[0]
            model_folds = fold_metrics.loc[fold_metrics["model_id"].eq(model_id)]
            checks = {
                "all_11_folds_complete": int(row["rolling_folds"]) == int(policy["required_rolling_folds"]),
                "finite_exact_predictions": bool(
                    np.isfinite(model_folds[["mae", "rmse", "bias"]].to_numpy(float)).all()
                ),
                "pooled_mae_within_10_percent_b1": float(row["pooled_mae_ratio_vs_b1"])
                <= float(policy["pooled_mae_ratio_vs_b1_max"]),
                "median_mae_within_10_percent_b1": float(row["median_fold_mae_ratio_vs_b1"])
                <= float(policy["median_fold_mae_ratio_vs_b1_max"]),
                "no_fold_exceeds_2x_b1": float(row["max_fold_mae_ratio_vs_b1"])
                <= float(policy["worst_fold_mae_ratio_vs_b1_max"]),
                "no_worker_or_convergence_failure": complete_worker,
                "all_inner_selections_passed_guardrails": not inner_selection_rejections,
            }
            passed = bool(all(checks.values()))
            observed = {
                "rolling_folds": int(row["rolling_folds"]),
                "pooled_mae": round(float(row["mae"]), 12),
                "pooled_mae_ratio_vs_b1": round(float(row["pooled_mae_ratio_vs_b1"]), 12),
                "median_fold_mae_ratio_vs_b1": round(
                    float(row["median_fold_mae_ratio_vs_b1"]), 12
                ),
                "max_fold_mae_ratio_vs_b1": round(
                    float(row["max_fold_mae_ratio_vs_b1"]), 12
                ),
                "inner_selection_rejected_folds": len(inner_selection_rejections),
            }
        comparator = spec["status"] == "FROZEN_COMPARATOR"
        advanced = bool(complete_worker and (passed or comparator))
        if worker_state == "REJECTED_MODEL_EXECUTION" and not comparator:
            model_status = "REJECTED_TEMPORAL_SCREEN"
        elif not complete_worker:
            model_status = "FAIL_PROTOCOL_INCOMPLETE_WORKER"
        elif passed:
            model_status = "PASSED_TEMPORAL_SCREEN"
        elif comparator:
            model_status = "FROZEN_COMPARATOR_ADVANCED_DESPITE_SCREEN"
        else:
            model_status = str(policy["rejected_status"])
        rows.append(
            {
                "model_id": model_id,
                "family": spec["family"],
                "environment_id": spec["environment_id"],
                "registry_status": spec["status"],
                "screen_status": model_status,
                "screen_passed": passed,
                "advanced_to_robustness": advanced,
                "checks_json": json.dumps(checks, sort_keys=True, separators=(",", ":")),
                "observed_json": json.dumps(observed, sort_keys=True, separators=(",", ":")),
            }
        )
    return pd.DataFrame(rows)


def global_parameter_registry(
    tuning: pd.DataFrame,
    registry: Mapping[str, Any],
) -> dict[str, Any]:
    models: dict[str, Any] = {}
    for spec in registry["models"]:
        model_id = spec["model_id"]
        subset = tuning.loc[tuning["model_id"].eq(model_id) & tuning["status"].eq("COMPLETE")].copy()
        if subset.empty:
            continue
        objective = "inner_crps" if spec["probabilistic_capabilities"] else "inner_mae"
        summary = (
            subset.groupby(["parameter_sha256", "parameter_json"], as_index=False)
            .agg(
                outer_folds=("outer_fold_id", "nunique"),
                objective=(objective, "mean"),
                transition_mae=("transition_mae", "mean"),
                eligibility_rate=("eligible", "mean"),
                effective_iterations=("median_effective_iterations", "median"),
            )
            .sort_values(
                ["eligibility_rate", "objective", "transition_mae", "parameter_json"],
                ascending=[False, True, True, True],
                kind="mergesort",
            )
        )
        best = summary.iloc[0]
        effective = best["effective_iterations"]
        models[model_id] = {
            "parameter_sha256": str(best["parameter_sha256"]),
            "parameters": json.loads(str(best["parameter_json"])),
            "selection_source": "aggregate_inner_results_across_11_rolling_outer_folds",
            "objective": objective,
            "mean_objective": float(best["objective"]),
            "mean_transition_mae": float(best["transition_mae"]),
            "eligibility_rate": float(best["eligibility_rate"]),
            "outer_folds": int(best["outer_folds"]),
            "effective_iterations": None if pd.isna(effective) else int(round(float(effective))),
        }
    return {"schema_version": 1, "models": models, "holdout_used": False}


def robustness_group_metrics(predictions: pd.DataFrame) -> pd.DataFrame:
    canonical = canonical_prediction_rows(predictions).copy()
    canonical["absolute_error"] = np.abs(canonical["y_pred"] - canonical["y_true"])
    rows: list[dict[str, Any]] = []
    for (design, model_id), frame in canonical.groupby(["design", "model_id"], sort=True):
        group_column = "profile_id" if "profile" in design else "zone_id"
        grouped = frame.groupby(group_column, sort=True)["absolute_error"].agg(["mean", "count"])
        rows.append(
            {
                "design": design,
                "model_id": model_id,
                "scope": "pooled_micro",
                "group": "all",
                "rows": len(frame),
                "groups": int(grouped.shape[0]),
                "mae": float(frame["absolute_error"].mean()),
            }
        )
        rows.append(
            {
                "design": design,
                "model_id": model_id,
                "scope": f"equal_{group_column.removesuffix('_id')}_macro",
                "group": "all_equal_weight",
                "rows": len(frame),
                "groups": int(grouped.shape[0]),
                "mae": float(grouped["mean"].mean()),
            }
        )
        worst = str(grouped["mean"].idxmax())
        rows.append(
            {
                "design": design,
                "model_id": model_id,
                "scope": f"worst_{group_column.removesuffix('_id')}",
                "group": worst,
                "rows": int(grouped.loc[worst, "count"]),
                "groups": 1,
                "mae": float(grouped.loc[worst, "mean"]),
            }
        )
        for group, values in grouped.iterrows():
            rows.append(
                {
                    "design": design,
                    "model_id": model_id,
                    "scope": f"by_{group_column.removesuffix('_id')}",
                    "group": str(group),
                    "rows": int(values["count"]),
                    "groups": 1,
                    "mae": float(values["mean"]),
                }
            )
    return pd.DataFrame(rows)


def segmented_metrics(predictions: pd.DataFrame) -> pd.DataFrame:
    canonical = canonical_prediction_rows(predictions).copy()
    canonical["absolute_error"] = np.abs(canonical["y_pred"] - canonical["y_true"])
    horizon = pd.to_numeric(canonical["forecast_horizon_days"], errors="coerce")
    canonical["horizon_bin"] = pd.cut(
        horizon, [-np.inf, 90, 150, np.inf], labels=["le_90", "91_150", "gt_150"]
    ).astype(str)
    missing = pd.to_numeric(canonical["missing_campaigns_since_previous"], errors="coerce").fillna(0)
    canonical["missing_campaign_bin"] = np.select(
        [missing.eq(0), missing.eq(1)], ["0", "1"], default="ge_2"
    )
    history = pd.to_numeric(canonical["n_history"], errors="coerce")
    canonical["history_bin"] = pd.cut(
        history, [2, 5, 9, np.inf], labels=["3_5", "6_9", "ge_10"]
    ).astype(str)
    rows: list[dict[str, Any]] = []
    for (design, model_id), frame in canonical.groupby(["design", "model_id"], sort=True):
        dimensions = [
            ("transition", "transition_segment"),
            ("horizon", "horizon_bin"),
            ("missing_campaigns", "missing_campaign_bin"),
            ("history", "history_bin"),
        ]
        pooled_transition = frame.loc[frame["is_transition"].astype(bool)]
        dimensions.append(("pooled_transition", None))
        for dimension, column in dimensions:
            groups = [("transition", pooled_transition)] if column is None else frame.groupby(column, sort=True)
            for segment, subset in groups:
                if subset.empty:
                    continue
                rows.append(
                    {
                        "design": design,
                        "model_id": model_id,
                        "dimension": dimension,
                        "segment": str(segment),
                        "rows": len(subset),
                        "points": int(subset["point_id"].astype(str).nunique()),
                        "profiles": int(subset["profile_id"].astype(str).nunique()),
                        "zones": int(subset["zone_id"].astype(str).nunique()),
                        "support_status": "ELIGIBLE_SUPPORT"
                        if len(subset) >= 20 and subset["profile_id"].astype(str).nunique() >= 5
                        else "DESCRIPTIVE_LOW_SUPPORT",
                        "mae": float(subset["absolute_error"].mean()),
                        "rmse": float(np.sqrt(np.mean((subset["y_pred"] - subset["y_true"]) ** 2))),
                        "bias": float(np.mean(subset["y_pred"] - subset["y_true"])),
                    }
                )
    return pd.DataFrame(rows)


def calibrate_predictions(
    predictions: pd.DataFrame,
    selected_inner_oof: pd.DataFrame,
    *,
    levels: Sequence[float],
    scale_clip: tuple[float, float],
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    canonical = canonical_prediction_rows(predictions)
    outputs: list[pd.DataFrame] = []
    parameters: list[dict[str, Any]] = []
    for (model_id, fold_id), outer in canonical.groupby(["model_id", "fold_id"], sort=True):
        oof = selected_inner_oof.loc[
            selected_inner_oof["model_id"].eq(model_id)
            & selected_inner_oof["outer_fold_id"].eq(fold_id)
        ].copy()
        if oof.empty:
            raise ContractViolation(f"Missing selected inner OOF rows for {model_id}/{fold_id}")
        if outer["aggregation"].eq("mean_of_fixed_seeds").all():
            group_columns = [
                "outer_fold_id",
                "inner_fold_id",
                "model_id",
                "parameter_sha256",
                "sample_id",
                "point_id",
                "profile_id",
                "zone_id",
                "current_date",
                "target_date",
                "forecast_horizon_days",
                "current_standard_uncertainty_mm",
                "y_true",
                "b1_prediction",
                "transition_segment",
                "is_transition",
                "provenance_role",
            ]
            oof = oof.groupby(group_columns, as_index=False, dropna=False).agg(
                y_pred=("y_pred", "mean"),
                absolute_error=("absolute_error", "mean"),
                b1_absolute_error=("b1_absolute_error", "mean"),
                probabilistic_score=("probabilistic_score", "mean"),
            )
        calibration = fit_scaled_conformal(oof, levels=levels, scale_clip=scale_clip)
        calibrated = apply_scaled_conformal(outer, calibration)
        calibrated["calibration_sample_ids_sha256"] = calibration.calibration_sample_ids_sha256
        outputs.append(calibrated)
        parameters.append(
            {
                "model_id": model_id,
                "fold_id": fold_id,
                **calibration.to_dict(),
            }
        )
    return pd.concat(outputs, ignore_index=True), parameters


def probabilistic_metric_table(calibrated: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for (design, model_id), frame in calibrated.groupby(["design", "model_id"], sort=True):
        scopes: list[tuple[str, str, pd.DataFrame]] = [("overall", "all", frame)]
        for column, dimension in (
            ("transition_segment", "transition"),
            ("profile_id", "profile"),
            ("zone_id", "zone"),
        ):
            scopes.extend((dimension, str(key), subset) for key, subset in frame.groupby(column, sort=True))
        missing = pd.to_numeric(frame["missing_campaigns_since_previous"], errors="coerce").fillna(0)
        gap_frame = frame.assign(gap_bin=np.where(missing.ge(1), "gap", "no_gap"))
        scopes.extend(("gap", str(key), subset) for key, subset in gap_frame.groupby("gap_bin", sort=True))
        for dimension, segment, subset in scopes:
            if dimension != "overall" and len(subset) < 30:
                support = "DESCRIPTIVE_LOW_SUPPORT"
            else:
                support = "ELIGIBLE_SUPPORT"
            common = interval_metrics(subset, prefix="conformal")
            row = {
                "design": design,
                "model_id": model_id,
                "interval_source": "conformalized",
                "dimension": dimension,
                "segment": segment,
                "support_status": support,
                "distribution_family": "distribution_free_conformal",
                **common,
                "crps": np.nan,
                "nll": np.nan,
            }
            rows.append(row)
        for dimension, segment, subset in scopes:
            native = native_probabilistic_metrics(subset)
            if native is None:
                continue
            support = (
                "ELIGIBLE_SUPPORT"
                if dimension == "overall" or len(subset) >= 30
                else "DESCRIPTIVE_LOW_SUPPORT"
            )
            rows.append(
                {
                    "design": design,
                    "model_id": model_id,
                    "interval_source": "native",
                    "dimension": dimension,
                    "segment": segment,
                    "support_status": support,
                    **native,
                }
            )
    return pd.DataFrame(rows)


def add_native_intervals(frame: pd.DataFrame) -> pd.DataFrame:
    output = frame.copy()
    if "predictive_std" in output and output["predictive_std"].notna().all():
        mean = pd.to_numeric(output["y_pred"], errors="raise").to_numpy(float)
        std = pd.to_numeric(output["predictive_std"], errors="coerce").to_numpy(float)
        for coverage in (0.50, 0.80, 0.95):
            z = NormalDist().inv_cdf(0.5 + coverage / 2.0)
            tag = str(int(100 * coverage))
            output[f"native_lower_{tag}"] = mean - z * std
            output[f"native_upper_{tag}"] = mean + z * std
        output["native_median"] = mean
    elif (
        {"q025", "q10", "q25", "q50", "q75", "q90", "q975"}.issubset(output.columns)
        and output[["q025", "q10", "q25", "q50", "q75", "q90", "q975"]].notna().all().all()
    ):
        output["native_lower_50"] = output["q25"]
        output["native_upper_50"] = output["q75"]
        output["native_lower_80"] = output["q10"]
        output["native_upper_80"] = output["q90"]
        output["native_lower_95"] = output["q025"]
        output["native_upper_95"] = output["q975"]
        output["native_median"] = output["q50"]
    return output


def native_probabilistic_metrics(frame: pd.DataFrame) -> dict[str, Any] | None:
    native = add_native_intervals(frame)
    required = {f"native_{bound}_{level}" for level in (50, 80, 95) for bound in ("lower", "upper")}
    if not required.issubset(native.columns):
        return None
    if "predictive_std" in native and native["predictive_std"].notna().all():
        metrics = interval_metrics(native, prefix="native")
        truth = native["y_true"]
        mean = native["y_pred"]
        std = native["predictive_std"]
        metrics["crps"] = float(np.mean(normal_crps(truth, mean, std)))
        metrics["nll"] = float(np.mean(normal_nll(truth, mean, std)))
        metrics["quantile_crossing_rate"] = 0.0
        metrics["native_interval_status"] = "VALID"
    else:
        levels = (0.025, 0.10, 0.25, 0.50, 0.75, 0.90, 0.975)
        columns = ("q025", "q10", "q25", "q50", "q75", "q90", "q975")
        truth = pd.to_numeric(native["y_true"], errors="raise").to_numpy(float)
        quantile_matrix = native.loc[:, columns].to_numpy(float)
        crossing = np.any(np.diff(quantile_matrix, axis=1) < 0.0, axis=1)
        crossing_rate = float(np.mean(crossing))
        if crossing_rate == 0.0:
            metrics = interval_metrics(native, prefix="native")
            metrics["native_interval_status"] = "VALID"
        else:
            metrics = _unavailable_interval_metrics(len(native))
            metrics["native_interval_status"] = "INVALID_QUANTILE_CROSSING"
        metrics["crps"] = float(
            np.mean(quantile_crps_approximation(truth, levels, quantile_matrix))
        )
        metrics["nll"] = float("nan")
        metrics["quantile_crossing_rate"] = crossing_rate
    families = native.get("distribution_family", pd.Series(dtype="string")).dropna().astype(str).unique()
    metrics["distribution_family"] = str(families[0]) if len(families) == 1 else "mixed_or_unspecified"
    return metrics


def _unavailable_interval_metrics(rows: int) -> dict[str, Any]:
    """Return an explicit non-score when native quantiles are not ordered."""

    metrics: dict[str, Any] = {"n": int(rows)}
    for tag in (50, 80, 95):
        metrics[f"coverage_{tag}"] = float("nan")
        metrics[f"mean_width_{tag}"] = float("nan")
        metrics[f"median_width_{tag}"] = float("nan")
        metrics[f"interval_score_{tag}"] = float("nan")
    metrics["weighted_interval_score"] = float("nan")
    return metrics


def paired_sensitivity_tables(
    temporal_predictions: pd.DataFrame,
    model_ids: Sequence[str],
    *,
    replicates: int,
    seed: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    canonical = canonical_prediction_rows(temporal_predictions).copy()
    canonical["absolute_error"] = np.abs(canonical["y_pred"] - canonical["y_true"])
    sensitivity: list[dict[str, Any]] = []
    jackknife: list[pd.DataFrame] = []
    for model_id in model_ids:
        for reference in ("B1_persistence_last_rate", "B7_two_regime_imm"):
            if model_id == reference:
                continue
            for cluster in ("profile_id", "target_date"):
                sensitivity.append(
                    paired_cluster_sensitivity(
                        canonical,
                        model_id=model_id,
                        reference_model_id=reference,
                        cluster_column=cluster,
                        replicates=replicates,
                        seed=seed,
                    )
                )
            jackknife.append(
                leave_one_cluster_out_jackknife(
                    canonical,
                    model_id=model_id,
                    reference_model_id=reference,
                    cluster_column="profile_id",
                )
            )
    return pd.DataFrame(sensitivity), pd.concat(jackknife, ignore_index=True) if jackknife else pd.DataFrame()


def learning_curve_metrics(predictions: pd.DataFrame) -> pd.DataFrame:
    canonical = canonical_prediction_rows(predictions)
    rows: list[dict[str, Any]] = []
    for (model_id, campaigns, training_rows), frame in canonical.groupby(
        ["model_id", "training_campaigns", "training_rows"], sort=True
    ):
        rows.append(
            {
                "model_id": model_id,
                "training_campaigns": int(campaigns),
                "training_rows": int(training_rows),
                "audit_rows": len(frame),
                "audit_target_date": "2023-11-07",
                "hyperparameters_retuned": False,
                **point_metrics(
                    frame["y_true"],
                    frame["y_pred"],
                    last_rate=frame["last_rate_mm_y"],
                    neutral_zone=1.96 * pd.to_numeric(frame["sigma_rate_mm_y"], errors="coerce").fillna(0),
                ),
            }
        )
    return pd.DataFrame(rows)


def precision_weights_from_train(train_frame: pd.DataFrame, evaluation_frame: pd.DataFrame) -> np.ndarray:
    train_sigma = pd.to_numeric(train_frame["sigma_rate_mm_y"], errors="coerce").to_numpy(float)
    finite = train_sigma[np.isfinite(train_sigma) & (train_sigma > 0)]
    if not len(finite):
        return np.ones(len(evaluation_frame), dtype=float)
    reference = float(np.median(finite**2))
    sigma = pd.to_numeric(evaluation_frame["sigma_rate_mm_y"], errors="coerce").to_numpy(float)
    sigma = np.where(np.isfinite(sigma) & (sigma > 0), sigma, np.sqrt(reference))
    weights = np.clip(reference / sigma**2, 0.25, 4.0)
    return weights / float(np.mean(weights))


def worst_point_fraction_mae(frame: pd.DataFrame, fraction: float) -> float:
    values = frame.assign(absolute_error=np.abs(frame["y_pred"] - frame["y_true"])).groupby(
        "point_id"
    )["absolute_error"].mean().sort_values(ascending=False)
    count = max(1, int(np.ceil(fraction * len(values))))
    return float(values.head(count).mean())

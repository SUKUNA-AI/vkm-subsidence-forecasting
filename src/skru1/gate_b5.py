"""Gate B5: freeze and audit the expanded train-only benchmark protocol."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd
import yaml

from .adaptive_kalman import AdaptiveKalmanRate, prepare_kalman_history
from .artifact_io import artifact_inventory, resolve_repo_path, snapshot_paths, write_csv_atomic, write_json_atomic
from .b5_splits import (
    BenchmarkFold,
    assignment_frame,
    build_benchmark_folds,
    freeze_benchmark_manifests,
)
from .baselines import TARGET_COLUMN, build_model
from .benchmark_metrics import mase_denominator_from_train, point_metrics
from .benchmarking import BenchmarkPlan, MetricSuite
from .data_contracts import ContractViolation, discover_project_root, load_canonical_bundle, sha256_file
from .evaluation import causal_feature_history, derived_dataset
from .imm_kalman import TwoRegimeIMMRate
from .robust_imm import RobustInnovationIMMRate
from .splits import load_split_dataset
from .transition_validation import classify_transition_proxy, fit_transition_thresholds


def load_gate_b5_config(root: str | Path | None = None) -> tuple[Path, dict[str, Any]]:
    project_root = discover_project_root(root)
    path = project_root / "configs" / "gate_b5.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ContractViolation("configs/gate_b5.yaml must contain a mapping")
    required = {
        "gate",
        "task",
        "source_split",
        "benchmark_version",
        "data_boundary",
        "resampling",
        "feature_views",
        "frozen_comparators",
        "protected_roots",
        "artifacts",
    }
    missing = required - set(config)
    if missing:
        raise ContractViolation(f"Gate B5 config is missing keys: {sorted(missing)}")
    if config["source_split"] != "t1_v1/train":
        raise ContractViolation("Gate B5 source_split must be t1_v1/train")
    return project_root, config


def run_gate_b5_freeze(root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    before = snapshot_paths(root, config["protected_roots"])
    bundle = load_canonical_bundle(root)
    train = load_split_dataset("t1", "train", root=root)
    source, outer, inner, contracts = build_benchmark_folds(train, bundle, config)
    plan = freeze_benchmark_manifests(root, source, outer, inner, contracts, config)
    paths = _paths(root, config)
    write_json_atomic(
        root,
        paths["protected_snapshot"],
        before,
        work_scope="gate_b5",
    )
    split_files = [
        paths["benchmark_plan"],
        paths["outer_assignments"],
        paths["inner_assignments"],
        paths["fold_contracts"],
        paths["feature_views"],
    ]
    write_csv_atomic(
        root,
        paths["split_inventory"],
        artifact_inventory(root, split_files),
        work_scope="gate_b5",
    )
    after = snapshot_paths(root, config["protected_roots"])
    if before != after:
        raise ContractViolation("Protected B0-B4 artifacts changed during B5 freeze")
    return {
        "phase": "freeze",
        "status": "PASS",
        "benchmark_version": plan.benchmark_version,
        "benchmark_plan_sha256": plan.plan_sha256,
        "outer_folds": len(outer),
        "inner_folds": len(inner),
        "selection_data": ["t1_v1/train"],
        "historical_validation_loaded": False,
        "current_test_loaded": False,
    }


def run_gate_b5_analysis(root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    paths = _paths(root, config)
    if not paths["benchmark_plan"].is_file():
        raise FileNotFoundError("Run Gate B5 freeze before analyze")
    protected_before = json.loads(paths["protected_snapshot"].read_text(encoding="utf-8"))
    if protected_before != snapshot_paths(root, config["protected_roots"]):
        raise ContractViolation("Protected B0-B4 snapshot changed before B5 analysis")
    bundle = load_canonical_bundle(root)
    train = load_split_dataset("t1", "train", root=root)
    source, outer, inner, contracts = build_benchmark_folds(train, bundle, config)
    plan = BenchmarkPlan.from_dict(json.loads(paths["benchmark_plan"].read_text(encoding="utf-8")))
    if plan.source_sample_ids_sha256 != train.provenance.sample_ids_sha256:
        raise ContractViolation("Frozen B5 plan no longer matches t1_v1/train")

    b4_predictions = pd.read_csv(
        root / "artifacts" / "model_selection" / "t1_b4_train_only_v1" / "outer_predictions.csv"
    )
    atlas = build_error_atlas(b4_predictions, source.frame, config)
    dependence = residual_dependence(b4_predictions)
    units = independent_unit_counts(b4_predictions)
    learning_predictions, learning = evaluate_frozen_learning_curves(
        source,
        bundle=bundle,
        config=config,
    )
    cards = method_exclusion_cards(source.frame)
    write_csv_atomic(root, paths["error_atlas"], atlas, work_scope="gate_b5")
    write_csv_atomic(root, paths["residual_dependence"], dependence, work_scope="gate_b5")
    write_csv_atomic(root, paths["independent_units"], units, work_scope="gate_b5")
    write_csv_atomic(
        root,
        paths["learning_curve_predictions"],
        learning_predictions,
        work_scope="gate_b5",
    )
    write_csv_atomic(root, paths["learning_curves"], learning, work_scope="gate_b5")
    write_json_atomic(root, paths["method_cards"], cards, work_scope="gate_b5")
    report = {
        "schema_version": 1,
        "gate": config["gate"],
        "status": "PASS_PROTOCOL_FROZEN",
        "scientific_scope": "train_only_internal_research",
        "claim_boundary": "no_final_model_quality_claim_without_new_future_or_external_holdout",
        "data": {
            "source": "t1_v1/train",
            "rows": len(source.frame),
            "points": int(source.frame["point_id"].astype(str).nunique()),
            "profiles": int(source.frame["profile_id"].astype(str).nunique()),
            "zones": int(source.frame["zone_id"].astype(str).nunique()),
            "target_dates": int(pd.to_datetime(source.frame["target_date"]).nunique()),
            "sample_ids_sha256": source.provenance.sample_ids_sha256,
            "historical_validation_rows_loaded": 0,
            "disclosed_test_rows_loaded": 0,
            "external_holdout_rows_loaded": 0,
        },
        "benchmark": {
            "version": plan.benchmark_version,
            "plan_sha256": plan.plan_sha256,
            "outer_folds": len(outer),
            "inner_folds": len(inner),
            "outer_counts": contracts.loc[contracts["level"].eq("outer")]
            .groupby("design")["fold_id"]
            .nunique()
            .to_dict(),
            "all_forward_only": bool(contracts["forward_only"].all()),
            "all_held_groups_excluded": bool(contracts["held_group_absent_from_train"].all()),
        },
        "evidence": {
            "b4_prediction_rows_audited": len(b4_predictions),
            "error_atlas_rows": len(atlas),
            "residual_dependence_rows": len(dependence),
            "learning_curve_prediction_rows": len(learning_predictions),
            "learning_curve_metric_rows": len(learning),
            "excluded_method_cards": len(cards["methods"]),
        },
        "feature_contract_sha256": bundle.feature_contract.source_sha256,
        "target_contract_sha256": bundle.target_contract.source_sha256,
        "metric_suite": MetricSuite().__dict__,
        "metric_suite_sha256": MetricSuite().suite_sha256,
        "protected_predecessor_snapshot_sha256": protected_before["snapshot_sha256"],
        "protected_predecessors_match": True,
        "historical_validation_loaded": False,
        "current_t1_test_loaded": False,
        "new_holdout_seen": False,
    }
    write_json_atomic(root, paths["gate_report"], report, work_scope="gate_b5")
    if protected_before != snapshot_paths(root, config["protected_roots"]):
        raise ContractViolation("Protected B0-B4 artifacts changed during B5 analysis")
    return {
        "phase": "analyze",
        "status": report["status"],
        "benchmark_plan_sha256": plan.plan_sha256,
        "error_atlas_rows": len(atlas),
        "learning_curve_rows": len(learning),
        "historical_validation_loaded": False,
        "current_test_loaded": False,
    }


def build_error_atlas(
    predictions: pd.DataFrame,
    source: pd.DataFrame,
    config: Mapping[str, Any],
) -> pd.DataFrame:
    required = {
        "design",
        "model_id",
        "sample_id",
        "point_id",
        "profile_id",
        "zone_id",
        "target_date",
        "y_true",
        "y_pred",
        "transition_segment",
    }
    missing = required - set(predictions)
    if missing:
        raise ContractViolation(f"B4 predictions are missing atlas fields: {sorted(missing)}")
    extra_fields = [
        "sample_id",
        "n_history",
        "current_standard_uncertainty_mm",
        "missing_campaigns_since_previous",
        "forecast_horizon_days",
    ]
    enrichment = source.loc[:, extra_fields].drop_duplicates("sample_id")
    enriched = predictions.drop(
        columns=[field for field in extra_fields if field != "sample_id" and field in predictions],
        errors="ignore",
    ).merge(enrichment, on="sample_id", how="left", validate="many_to_one")
    enriched["absolute_error"] = np.abs(
        pd.to_numeric(enriched["y_pred"], errors="raise")
        - pd.to_numeric(enriched["y_true"], errors="raise")
    )
    enriched["horizon_bin"] = pd.cut(
        pd.to_numeric(enriched["forecast_horizon_days"]),
        bins=[-np.inf, 90, 150, np.inf],
        labels=["le_90", "91_150", "gt_150"],
    ).astype("string")
    enriched["history_bin"] = pd.cut(
        pd.to_numeric(enriched["n_history"]),
        bins=[2, 5, 9, np.inf],
        labels=["3_5", "6_9", "ge_10"],
    ).astype("string")
    missing_campaigns = pd.to_numeric(enriched["missing_campaigns_since_previous"], errors="coerce").fillna(0)
    enriched["missing_campaign_bin"] = np.select(
        [missing_campaigns.eq(0), missing_campaigns.eq(1)], ["0", "1"], default="ge_2"
    )
    uncertainty = pd.to_numeric(source["current_standard_uncertainty_mm"], errors="coerce")
    q_low, q_high = uncertainty.quantile([1 / 3, 2 / 3]).tolist()
    enriched["uncertainty_bin"] = pd.cut(
        pd.to_numeric(enriched["current_standard_uncertainty_mm"], errors="coerce"),
        bins=[-np.inf, q_low, q_high, np.inf],
        labels=["low", "middle", "high"],
        include_lowest=True,
    ).astype("string")
    enriched["target_date_label"] = pd.to_datetime(enriched["target_date"]).dt.date.astype(str)

    dimensions = (
        ("target_date", "target_date_label"),
        ("profile", "profile_id"),
        ("zone", "zone_id"),
        ("point", "point_id"),
        ("transition", "transition_segment"),
        ("horizon", "horizon_bin"),
        ("history", "history_bin"),
        ("uncertainty", "uncertainty_bin"),
        ("missing_campaigns", "missing_campaign_bin"),
    )
    rows: list[dict[str, Any]] = []
    for (design, model_id), model_frame in enriched.groupby(["design", "model_id"], sort=True):
        rows.append(_atlas_row(design, model_id, "pooled_micro", "all", model_frame, config))
        for dimension, column in dimensions:
            for label, segment in model_frame.groupby(column, sort=True, dropna=False):
                rows.append(_atlas_row(design, model_id, dimension, str(label), segment, config))
        for group_column, dimension in (("profile_id", "equal_profile_macro"), ("zone_id", "equal_zone_macro")):
            group_metrics = [
                point_metrics(group["y_true"], group["y_pred"])
                for _, group in model_frame.groupby(group_column, sort=True)
            ]
            macro = {}
            for key in group_metrics[0]:
                if key in {"n", "mase_available"}:
                    continue
                values = np.asarray([float(item[key]) for item in group_metrics], dtype=float)
                macro[key] = float(np.mean(values[np.isfinite(values)])) if np.isfinite(values).any() else np.nan
            rows.append(
                {
                    "design": design,
                    "model_id": model_id,
                    "dimension": dimension,
                    "segment": "all_groups_equal_weight",
                    "origins": len(model_frame),
                    "points": int(model_frame["point_id"].astype(str).nunique()),
                    "profiles": int(model_frame["profile_id"].astype(str).nunique()),
                    "zones": int(model_frame["zone_id"].astype(str).nunique()),
                    "support_status": _support_status(model_frame, config),
                    **macro,
                }
            )
        for group_column, dimension in (("profile_id", "worst_profile"), ("zone_id", "worst_zone")):
            group_mae = model_frame.groupby(group_column)["absolute_error"].mean()
            worst = str(group_mae.idxmax())
            rows.append(
                _atlas_row(
                    design,
                    model_id,
                    dimension,
                    worst,
                    model_frame.loc[model_frame[group_column].astype(str).eq(worst)],
                    config,
                )
            )
        point_mae = model_frame.groupby("point_id")["absolute_error"].mean().sort_values(ascending=False)
        worst_count = max(1, int(np.ceil(0.10 * len(point_mae))))
        worst_points = set(point_mae.head(worst_count).index.astype(str))
        rows.append(
            _atlas_row(
                design,
                model_id,
                "worst_10_percent_points",
                "top_mean_absolute_error",
                model_frame.loc[model_frame["point_id"].astype(str).isin(worst_points)],
                config,
            )
        )
    return pd.DataFrame(rows).sort_values(
        ["design", "model_id", "dimension", "segment"], kind="mergesort"
    ).reset_index(drop=True)


def _atlas_row(
    design: str,
    model_id: str,
    dimension: str,
    segment: str,
    frame: pd.DataFrame,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    metrics = point_metrics(frame["y_true"], frame["y_pred"])
    return {
        "design": design,
        "model_id": model_id,
        "dimension": dimension,
        "segment": segment,
        "origins": len(frame),
        "points": int(frame["point_id"].astype(str).nunique()),
        "profiles": int(frame["profile_id"].astype(str).nunique()),
        "zones": int(frame["zone_id"].astype(str).nunique()),
        "support_status": _support_status(frame, config),
        **{key: value for key, value in metrics.items() if key not in {"n", "mase_available"}},
    }


def _support_status(frame: pd.DataFrame, config: Mapping[str, Any]) -> str:
    policy = config["support_policy"]
    if len(frame) < int(policy["minimum_origins_for_selection"]) or frame[
        "profile_id"
    ].astype(str).nunique() < int(policy["minimum_profiles_for_selection"]):
        return str(policy["low_support_status"])
    return str(policy["adequate_support_status"])


def residual_dependence(predictions: pd.DataFrame) -> pd.DataFrame:
    frame = predictions.copy()
    frame["residual"] = pd.to_numeric(frame["y_pred"], errors="raise") - pd.to_numeric(
        frame["y_true"], errors="raise"
    )
    frame["target_date"] = pd.to_datetime(frame["target_date"], errors="raise")
    rows: list[dict[str, Any]] = []
    for (design, model_id), subset in frame.groupby(["design", "model_id"], sort=True):
        for group_column, label in (("profile_id", "within_profile_icc1"), ("target_date", "within_calendar_icc1")):
            rows.append(
                {
                    "design": design,
                    "model_id": model_id,
                    "dependence_measure": label,
                    "groups": int(subset[group_column].nunique()),
                    "rows": len(subset),
                    "residual_correlation": _icc_one_way(subset["residual"], subset[group_column]),
                    "interpretation": "descriptive_cluster_dependence_not_effective_sample_size_proof",
                }
            )
        pairwise = _mean_within_profile_point_correlation(subset)
        rows.append(
            {
                "design": design,
                "model_id": model_id,
                "dependence_measure": "mean_pairwise_point_residual_correlation_within_profile",
                "groups": int(subset["profile_id"].nunique()),
                "rows": len(subset),
                "residual_correlation": pairwise,
                "interpretation": "descriptive_only_requires_repeated_target_dates",
            }
        )
    return pd.DataFrame(rows)


def _icc_one_way(values: pd.Series, groups: pd.Series) -> float:
    data = pd.DataFrame({"value": pd.to_numeric(values, errors="coerce"), "group": groups.astype(str)}).dropna()
    if data["group"].nunique() < 2 or len(data) <= data["group"].nunique():
        return float("nan")
    counts = data.groupby("group").size().to_numpy(float)
    means = data.groupby("group")["value"].mean()
    grand = float(data["value"].mean())
    k = len(counts)
    ss_between = float(sum(counts[index] * (means.iloc[index] - grand) ** 2 for index in range(k)))
    joined = data.join(means.rename("group_mean"), on="group")
    ss_within = float(np.sum((joined["value"] - joined["group_mean"]) ** 2))
    ms_between = ss_between / (k - 1)
    ms_within = ss_within / (len(data) - k)
    n_bar = (len(data) - float(np.sum(counts**2)) / len(data)) / (k - 1)
    denominator = ms_between + (n_bar - 1.0) * ms_within
    return float((ms_between - ms_within) / denominator) if denominator > 0 else float("nan")


def _mean_within_profile_point_correlation(frame: pd.DataFrame) -> float:
    correlations: list[float] = []
    for _, profile in frame.groupby("profile_id", sort=True):
        pivot = profile.pivot_table(index="target_date", columns="point_id", values="residual", aggfunc="mean")
        if pivot.shape[0] < 2 or pivot.shape[1] < 2:
            continue
        matrix = pivot.corr(min_periods=2).to_numpy(float)
        values = matrix[np.triu_indices_from(matrix, k=1)]
        correlations.extend(values[np.isfinite(values)].tolist())
    return float(np.mean(correlations)) if correlations else float("nan")


def independent_unit_counts(predictions: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for (design, model_id), frame in predictions.groupby(["design", "model_id"], sort=True):
        rows.append(
            {
                "design": design,
                "model_id": model_id,
                "origins": len(frame),
                "temporal_units": int(pd.to_datetime(frame["target_date"]).nunique()),
                "profile_units": int(frame["profile_id"].astype(str).nunique()),
                "zone_units": int(frame["zone_id"].astype(str).nunique()),
                "point_trajectories": int(frame["point_id"].astype(str).nunique()),
                "iid_row_count_claimed": False,
            }
        )
    return pd.DataFrame(rows)


def evaluate_frozen_learning_curves(
    source,
    *,
    bundle,
    config: Mapping[str, Any],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    audit_date = pd.Timestamp(config["learning_curves"]["audit_target_date"])
    target_dates = pd.to_datetime(source.frame["target_date"], errors="raise")
    audit_ids = tuple(source.frame.loc[target_dates.eq(audit_date), "sample_id"].astype(str))
    audit = derived_dataset(source, audit_ids, split="validation", label="gate_b5_learning_audit_tail")
    core_dates = sorted(pd.Timestamp(value) for value in target_dates.loc[target_dates.lt(audit_date)].unique())
    raw_history = causal_feature_history(source)
    prepared_history = prepare_kalman_history(raw_history)
    specs = _resolved_comparator_specs(bundle.root, config)
    prediction_rows: list[pd.DataFrame] = []
    expected = {int(key): int(value) for key, value in config["learning_curves"]["trailing_core_campaigns_to_rows"].items()}
    for campaign_count, expected_rows in sorted(expected.items()):
        chosen = set(core_dates[-campaign_count:])
        train_ids = tuple(
            source.frame.loc[target_dates.isin(chosen), "sample_id"].astype(str)
        )
        training = derived_dataset(
            source,
            train_ids,
            split="train",
            label=f"gate_b5_learning_{campaign_count}_campaigns",
        )
        if len(training.frame) != expected_rows:
            raise ContractViolation(
                f"Learning-curve row count changed for {campaign_count} campaigns: {len(training.frame)}"
            )
        thresholds = _transition_thresholds(training.frame, config)
        segments = classify_transition_proxy(audit.frame, thresholds)
        weights = _validation_precision_weights(training.frame, audit.frame)
        mase_scale = mase_denominator_from_train(training.frame)
        for spec in specs:
            model = _build_frozen_comparator(spec, bundle=bundle, random_seed=int(config["random_seed"]))
            model.fit(training)
            if hasattr(model, "predict_distribution"):
                prediction, raw_sigma, _ = model.predict_distribution(audit, history_frame=prepared_history)
            else:
                prediction = model.predict(audit, history_frame=raw_history)
                raw_sigma = np.full(len(audit.frame), np.nan)
            if not np.isfinite(prediction).all():
                raise RuntimeError(f"Learning curve model produced non-finite values: {model.model_id}")
            output = audit.frame.loc[
                :, [
                    "sample_id",
                    "point_id",
                    "profile_id",
                    "zone_id",
                    "current_date",
                    "target_date",
                    "forecast_horizon_days",
                    "last_rate_mm_y",
                    "sigma_rate_mm_y",
                ]
            ].copy()
            output.insert(0, "training_campaigns", campaign_count)
            output.insert(1, "training_rows", len(training.frame))
            output.insert(2, "train_sample_ids_sha256", training.provenance.sample_ids_sha256)
            output.insert(3, "model_id", model.model_id)
            output.insert(4, "family", model.family)
            output["y_true"] = pd.to_numeric(audit.frame[TARGET_COLUMN], errors="raise").to_numpy(float)
            output["y_pred"] = prediction
            output["error"] = prediction - output["y_true"].to_numpy(float)
            output["absolute_error"] = np.abs(output["error"])
            output["raw_sigma"] = raw_sigma
            output["precision_weight"] = weights
            output["mase_denominator"] = mase_scale
            for column in segments:
                output[column] = segments[column].to_numpy()
            prediction_rows.append(output)
    predictions = pd.concat(prediction_rows, ignore_index=True)
    metrics_rows: list[dict[str, Any]] = []
    for (campaigns, training_rows, model_id, family), frame in predictions.groupby(
        ["training_campaigns", "training_rows", "model_id", "family"], sort=True
    ):
        reference = predictions.loc[
            predictions["training_campaigns"].eq(campaigns)
            & predictions["model_id"].eq("B1_persistence_last_rate")
        ].set_index("sample_id").loc[frame["sample_id"].astype(str), "y_pred"]
        metrics_rows.append(
            {
                "training_campaigns": int(campaigns),
                "training_rows": int(training_rows),
                "model_id": model_id,
                "family": family,
                "audit_target_date": audit_date.date().isoformat(),
                "audit_rows": len(frame),
                "hyperparameters_retuned": False,
                **point_metrics(
                    frame["y_true"],
                    frame["y_pred"],
                    sample_weight=frame["precision_weight"],
                    b1_prediction=reference,
                    mase_denominator=float(frame["mase_denominator"].iloc[0]),
                    last_rate=frame["last_rate_mm_y"],
                    neutral_zone=1.96 * pd.to_numeric(frame["sigma_rate_mm_y"], errors="coerce").fillna(0),
                ),
            }
        )
    return predictions, pd.DataFrame(metrics_rows)


def _resolved_comparator_specs(root: Path, config: Mapping[str, Any]) -> list[dict[str, Any]]:
    b4 = yaml.safe_load((root / "configs" / "gate_b4.yaml").read_text(encoding="utf-8"))
    b4_by_id = {str(spec["model_id"]): dict(spec) for spec in b4["frozen_comparators"]}
    candidate = json.loads(
        (root / "artifacts" / "model_selection" / "t1_b4_train_only_v1" / "research_candidate.json").read_text(
            encoding="utf-8"
        )
    )
    result: list[dict[str, Any]] = []
    for raw in config["frozen_comparators"]:
        spec = dict(raw)
        model_id = str(spec["model_id"])
        if model_id in {"B6_adaptive_kalman", "B7_two_regime_imm"}:
            spec = b4_by_id[model_id]
        elif model_id == "B8_student_t_robust_imm":
            spec = {
                "model_id": model_id,
                "family": "imm_student_t_robust_observation",
                "parameters": dict(candidate["selected_parameters"]),
            }
        result.append(spec)
    return result


def _build_frozen_comparator(spec: Mapping[str, Any], *, bundle, random_seed: int):
    family = str(spec["family"])
    if family in {"persistence", "profile_robust_trend", "fixed_kalman", "ridge", "extra_trees"}:
        return build_model(
            spec,
            contract=bundle.feature_contract,
            random_seed=random_seed,
            weight_clip=(0.25, 4.0),
        )
    if family == "adaptive_kalman":
        return AdaptiveKalmanRate(model_id=str(spec["model_id"]), parameters=dict(spec["parameters"]))
    if family == "imm_damped_acceleration":
        return TwoRegimeIMMRate(model_id=str(spec["model_id"]), parameters=dict(spec["parameters"]))
    if family == "imm_student_t_robust_observation":
        return RobustInnovationIMMRate(
            model_id=str(spec["model_id"]), parameters=dict(spec["parameters"])
        )
    raise KeyError(f"Unknown frozen comparator family: {family}")


def _transition_thresholds(frame: pd.DataFrame, config: Mapping[str, Any]):
    policy = config["transition_validation"]
    return fit_transition_thresholds(
        frame,
        acceleration_quantile=float(policy["acceleration_absolute_quantile"]),
        volatility_quantile=float(policy["volatility_quantile"]),
        missing_campaigns_threshold=int(policy["missing_campaigns_threshold"]),
    )


def _validation_precision_weights(train_frame: pd.DataFrame, validation_frame: pd.DataFrame) -> np.ndarray:
    train_sigma = pd.to_numeric(train_frame["sigma_rate_mm_y"], errors="coerce").to_numpy(float)
    finite_train = train_sigma[np.isfinite(train_sigma) & (train_sigma > 0)]
    if not len(finite_train):
        return np.ones(len(validation_frame), dtype=float)
    reference_variance = float(np.median(finite_train**2))
    sigma = pd.to_numeric(validation_frame["sigma_rate_mm_y"], errors="coerce").to_numpy(float)
    sigma = np.where(np.isfinite(sigma) & (sigma > 0), sigma, np.sqrt(reference_variance))
    weights = np.clip(reference_variance / sigma**2, 0.25, 4.0)
    return weights / float(np.mean(weights))


def method_exclusion_cards(source: pd.DataFrame) -> dict[str, Any]:
    origins_per_point = source.groupby("point_id").size()
    n_history = pd.to_numeric(source["n_history"], errors="coerce")
    gaps = pd.to_numeric(source["days_since_previous_observation"], errors="coerce")
    common = {
        "status": "NOT_ELIGIBLE_DATA_GEOMETRY",
        "decision_stage": "B5_pre_screening",
        "not_a_missing_implementation": True,
        "observed_geometry": {
            "model_origins_per_point_min": int(origins_per_point.min()),
            "model_origins_per_point_max": int(origins_per_point.max()),
            "available_history_min": int(n_history.min()),
            "available_history_max": int(n_history.max()),
            "campaign_interval_days_min": int(gaps.min()),
            "campaign_interval_days_max": int(gaps.max()),
            "profiles": int(source["profile_id"].astype(str).nunique()),
            "missing_campaigns_present": bool(
                pd.to_numeric(source["missing_campaigns_since_previous"], errors="coerce").gt(0).any()
            ),
        },
    }
    return {
        "schema_version": 1,
        "scope": "t1_v1/train_only",
        "methods": [
            {
                "method_id": "ETS",
                **common,
                "reason": "Short, irregular and gapped point histories would require unvalidated regular-grid interpolation.",
            },
            {
                "method_id": "ARIMA_ARIMAX",
                **common,
                "reason": "Only 3-16 observations are available at issue time and campaign intervals are irregular; ARIMA identification is not supported without speculative interpolation.",
            },
            {
                "method_id": "PROFILE_VAR",
                **common,
                "reason": "Fourteen profiles and incomplete synchronous campaigns make a profile VAR non-identifiable or extremely unstable at this sample size.",
            },
        ],
    }


def run_gate_b5_validation(root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    paths = _paths(root, config)
    checks: list[dict[str, Any]] = []

    def check(name: str, condition: bool, observed: Any = None) -> None:
        checks.append({"check": name, "passed": bool(condition), "observed": observed})

    bundle = load_canonical_bundle(root)
    train = load_split_dataset("t1", "train", root=root)
    source, outer, inner, contracts = build_benchmark_folds(train, bundle, config)
    saved_plan_payload = json.loads(paths["benchmark_plan"].read_text(encoding="utf-8"))
    saved_plan = BenchmarkPlan.from_dict(saved_plan_payload)
    check("benchmark_source_is_train_only", saved_plan.source_split == "t1_v1/train")
    check("outer_fold_count_is_65", len(outer) == 65, len(outer))
    check("inner_fold_count_is_195", len(inner) == 195, len(inner))
    check("all_folds_forward_only", bool(contracts["forward_only"].all()))
    check("all_held_groups_absent_from_train", bool(contracts["held_group_absent_from_train"].all()))
    check(
        "saved_contracts_equal_rebuild",
        _frames_equal(pd.read_csv(paths["fold_contracts"], keep_default_na=False), contracts),
    )
    check(
        "saved_outer_assignments_equal_rebuild",
        _frames_equal(
            pd.read_csv(paths["outer_assignments"], keep_default_na=False),
            assignment_frame(outer),
        ),
    )
    check(
        "saved_inner_assignments_equal_rebuild",
        _frames_equal(
            pd.read_csv(paths["inner_assignments"], keep_default_na=False),
            assignment_frame(inner),
        ),
    )
    check(
        "protected_predecessors_unchanged",
        json.loads(paths["protected_snapshot"].read_text(encoding="utf-8"))
        == snapshot_paths(root, config["protected_roots"]),
    )
    feature_views = json.loads(paths["feature_views"].read_text(encoding="utf-8"))
    check("three_feature_views_frozen", set(feature_views["views"]) == {"SAFE_ALL", "DYNAMIC_CORE_17", "NATIVE_CATEGORICAL"})
    check(
        "no_identifiers_in_feature_views",
        all(not view["identifiers_in_X"] for view in feature_views["views"].values()),
    )
    cards = json.loads(paths["method_cards"].read_text(encoding="utf-8"))
    check(
        "three_ineligible_method_cards",
        len(cards["methods"]) == 3
        and all(method["status"] == "NOT_ELIGIBLE_DATA_GEOMETRY" for method in cards["methods"]),
    )
    report = json.loads(paths["gate_report"].read_text(encoding="utf-8"))
    check("validation_not_loaded", report["historical_validation_loaded"] is False)
    check("test_not_loaded", report["current_t1_test_loaded"] is False)
    check("new_holdout_not_seen", report["new_holdout_seen"] is False)
    learning_predictions = pd.read_csv(paths["learning_curve_predictions"])
    learning_metrics = pd.read_csv(paths["learning_curves"])
    check("learning_curve_exact_sizes", set(learning_predictions["training_rows"].unique()) == {217, 423, 708, 823})
    check("learning_curve_audit_rows", learning_predictions.groupby(["training_rows", "model_id"]).size().eq(88).all())
    check("learning_curve_hyperparameters_not_retuned", not learning_metrics["hyperparameters_retuned"].astype(bool).any())
    b4_predictions = pd.read_csv(
        root / "artifacts" / "model_selection" / "t1_b4_train_only_v1" / "outer_predictions.csv"
    )
    check(
        "error_atlas_recomputes",
        _frames_equal(pd.read_csv(paths["error_atlas"]), build_error_atlas(b4_predictions, source.frame, config)),
    )
    check(
        "independent_units_recompute",
        _frames_equal(pd.read_csv(paths["independent_units"]), independent_unit_counts(b4_predictions)),
    )
    failed = [row for row in checks if not row["passed"]]
    validation = {
        "schema_version": 1,
        "gate": config["gate"],
        "status": "PASS" if not failed else "FAIL_PROTOCOL",
        "summary": {"checks": len(checks), "passed": len(checks) - len(failed), "failed": len(failed)},
        "checks": checks,
        "validator_data_access": ["t1_v1/train", "saved_machine_artifacts", "immutable_B4_predictions"],
        "historical_validation_loaded": False,
        "current_t1_test_loaded": False,
    }
    write_json_atomic(root, paths["validation_report"], validation, work_scope="gate_b5")
    evidence_paths = [
        paths[key]
        for key in (
            "protected_snapshot",
            "error_atlas",
            "residual_dependence",
            "independent_units",
            "learning_curve_predictions",
            "learning_curves",
            "method_cards",
            "gate_report",
            "validation_report",
        )
    ]
    write_csv_atomic(
        root,
        paths["artifact_inventory"],
        artifact_inventory(root, evidence_paths),
        work_scope="gate_b5",
    )
    if failed:
        raise ContractViolation(f"Gate B5 validation failed: {[row['check'] for row in failed]}")
    return {"phase": "validate", "status": "PASS", "checks": len(checks), "failed": 0}


def _frames_equal(left: pd.DataFrame, right: pd.DataFrame) -> bool:
    if list(left.columns) != list(right.columns) or left.shape != right.shape:
        return False
    try:
        pd.testing.assert_frame_equal(
            left.reset_index(drop=True),
            right.reset_index(drop=True),
            check_dtype=False,
            check_exact=False,
            rtol=1e-10,
            atol=1e-10,
        )
    except AssertionError:
        return False
    return True


def _paths(root: Path, config: Mapping[str, Any]) -> dict[str, Path]:
    return {key: resolve_repo_path(root, value) for key, value in config["artifacts"].items()}

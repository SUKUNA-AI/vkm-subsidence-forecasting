"""Gate B4: nested train-only research for a robust IMM observation model."""

from __future__ import annotations

import ast
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from uuid import uuid4

import numpy as np
import pandas as pd
import yaml

from .adaptive_kalman import AdaptiveKalmanRate, PreparedKalmanHistory, prepare_kalman_history
from .baselines import TARGET_COLUMN, build_model
from .data_contracts import (
    CanonicalBundle,
    ContractViolation,
    discover_project_root,
    load_canonical_bundle,
    sha256_file,
)
from .evaluation import EvaluationFold, causal_feature_history, derived_dataset
from .imm_kalman import TwoRegimeIMMRate
from .metrics import regression_metrics
from .robust_imm import RobustInnovationIMMRate
from .splits import ManifestDataset, load_split_dataset, rolling_origin_assignments
from .train_research_splits import build_train_only_folds, freeze_train_only_manifests
from .transition_validation import (
    classify_transition_proxy,
    fit_transition_thresholds,
    transition_metrics,
)


MODEL_IDS = (
    "B1_persistence_last_rate",
    "B5_fixed_kalman",
    "B6_adaptive_kalman",
    "B7_two_regime_imm",
    "B8_student_t_robust_imm",
)


def load_gate_b4_config(
    root: str | Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    project_root = discover_project_root(root)
    path = project_root / "configs" / "gate_b4.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ContractViolation("configs/gate_b4.yaml must contain a mapping")
    required = {
        "gate",
        "task",
        "source_split",
        "data_boundary",
        "protected_predecessors",
        "resampling",
        "frozen_comparators",
        "robust_model",
        "selection",
        "transition_validation",
        "acceptance",
        "artifacts",
        "source_files",
    }
    missing = required - set(config)
    if missing:
        raise ContractViolation(f"Gate B4 config is missing keys: {sorted(missing)}")
    if config["source_split"] != "t1_v1/train":
        raise ContractViolation("Gate B4 source split must be t1_v1/train")
    allowed = list(config["data_boundary"]["allowed_model_selection_data"])
    if allowed != ["t1_v1/train"]:
        raise ContractViolation("Gate B4 may select models only on t1_v1/train")
    weights = config["selection"]["objective"]
    if not np.isclose(
        float(weights["overall_normalized_mae_weight"])
        + float(weights["volatile_or_gap_normalized_mae_weight"]),
        1.0,
    ):
        raise ContractViolation("Gate B4 tuning weights must sum to one")
    if len(config["robust_model"]["student_t_df_grid"]) != 4:
        raise ContractViolation("Gate B4 requires the frozen four-value Student-t grid")
    for relative in [
        *config["artifacts"].values(),
        *config["protected_predecessors"].keys(),
        *config["source_files"],
    ]:
        resolve_repo_path(project_root, str(relative))
    return project_root, config


def resolve_repo_path(root: Path, relative: str | Path) -> Path:
    path = Path(relative)
    if path.is_absolute():
        raise ContractViolation(f"Gate B4 path must be repository-relative: {path}")
    resolved = (root / path).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ContractViolation(f"Gate B4 path escapes repository root: {path}") from exc
    return resolved


def capture_protected_predecessor_snapshot(
    root: Path,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for relative, expected_hash in config["protected_predecessors"].items():
        path = resolve_repo_path(root, relative)
        if not path.is_file():
            raise FileNotFoundError(f"Protected Gate B4 predecessor is missing: {relative}")
        observed_hash = sha256_file(path)
        if observed_hash.lower() != str(expected_hash).lower():
            raise ContractViolation(f"Protected predecessor changed: {relative}")
        rows.append(
            {
                "relative_path": Path(relative).as_posix(),
                "size_bytes": path.stat().st_size,
                "expected_sha256": str(expected_hash).lower(),
                "observed_sha256": observed_hash,
                "matches": True,
            }
        )
    return {
        "schema_version": 1,
        "policy": "hash_only_no_validation_or_test_loader",
        "all_match": True,
        "protected_files": rows,
    }


def robust_parameters(
    config: Mapping[str, Any],
    student_t_df: float,
) -> dict[str, Any]:
    b7 = next(
        spec
        for spec in config["frozen_comparators"]
        if spec["model_id"] == "B7_two_regime_imm"
    )
    parameters = dict(b7["parameters"])
    parameters.update(config["robust_model"]["fixed_parameters"])
    parameters["student_t_df"] = float(student_t_df)
    return parameters


def robust_parameter_grid(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        robust_parameters(config, float(value))
        for value in config["robust_model"]["student_t_df_grid"]
    ]


def tune_robust_df(
    training: ManifestDataset,
    *,
    prepared_history: PreparedKalmanHistory,
    config: Mapping[str, Any],
    context: str,
) -> tuple[dict[str, Any], pd.DataFrame]:
    """Select Student-t degrees of freedom on nested rolling train folds."""

    policy = config["resampling"]
    assignments = rolling_origin_assignments(
        [training],
        minimum_train_dates=int(policy["inner_minimum_train_dates"]),
        maximum_folds=int(policy["inner_rolling_origin_folds"]),
    )
    transition_policy = config["transition_validation"]
    candidate_buffers: dict[float, dict[str, list[Any]]] = {
        float(value): {
            "truth": [],
            "b7": [],
            "b8": [],
            "segment": [],
        }
        for value in config["robust_model"]["student_t_df_grid"]
    }
    contract_rows: list[dict[str, Any]] = []
    b7_spec = next(
        spec
        for spec in config["frozen_comparators"]
        if spec["model_id"] == "B7_two_regime_imm"
    )
    for fold_id, assignment in assignments.groupby("fold_id", sort=True):
        train_ids = tuple(
            assignment.loc[assignment["role"].eq("train"), "sample_id"].astype(str)
        )
        validation_ids = tuple(
            assignment.loc[
                assignment["role"].eq("validation"), "sample_id"
            ].astype(str)
        )
        inner_train = derived_dataset(
            training,
            train_ids,
            split="train",
            label=f"gate_b4_{context}_{fold_id}_train",
        )
        inner_validation = derived_dataset(
            training,
            validation_ids,
            split="validation",
            label=f"gate_b4_{context}_{fold_id}_validation",
        )
        train_max = pd.Timestamp(pd.to_datetime(inner_train.frame["target_date"]).max())
        validation_min = pd.Timestamp(
            pd.to_datetime(inner_validation.frame["target_date"]).min()
        )
        if train_max >= validation_min:
            raise ContractViolation(f"Inner Gate B4 fold is not forward-only: {context}/{fold_id}")
        thresholds = fit_transition_thresholds(
            inner_train.frame,
            acceleration_quantile=float(
                transition_policy["acceleration_absolute_quantile"]
            ),
            volatility_quantile=float(transition_policy["volatility_quantile"]),
            missing_campaigns_threshold=int(
                transition_policy["missing_campaigns_threshold"]
            ),
        )
        segments = classify_transition_proxy(inner_validation.frame, thresholds)[
            "transition_segment"
        ].astype(str)
        truth = pd.to_numeric(
            inner_validation.frame[TARGET_COLUMN], errors="raise"
        ).to_numpy(float)
        b7_model = TwoRegimeIMMRate(
            model_id=str(b7_spec["model_id"]),
            parameters=dict(b7_spec["parameters"]),
        ).fit(inner_train)
        b7_prediction = b7_model.predict(
            inner_validation,
            history_frame=prepared_history,
        )
        contract_rows.append(
            {
                "fold_id": str(fold_id),
                "train_rows": len(inner_train.frame),
                "validation_rows": len(inner_validation.frame),
                "train_target_date_max": train_max.date().isoformat(),
                "validation_target_date_min": validation_min.date().isoformat(),
                "train_sample_ids_sha256": inner_train.provenance.sample_ids_sha256,
                "validation_sample_ids_sha256": inner_validation.provenance.sample_ids_sha256,
            }
        )
        for student_t_df in candidate_buffers:
            model = RobustInnovationIMMRate(
                model_id=str(config["robust_model"]["model_id"]),
                parameters=robust_parameters(config, student_t_df),
            ).fit(inner_train)
            prediction = model.predict(
                inner_validation,
                history_frame=prepared_history,
            )
            if not np.isfinite(prediction).all():
                raise RuntimeError(
                    f"Robust IMM produced invalid tuning predictions: {context}/{fold_id}/{student_t_df}"
                )
            buffer = candidate_buffers[student_t_df]
            buffer["truth"].extend(truth.tolist())
            buffer["b7"].extend(b7_prediction.tolist())
            buffer["b8"].extend(prediction.tolist())
            buffer["segment"].extend(segments.tolist())

    contract_hash = sha256(
        json.dumps(contract_rows, sort_keys=True).encode("utf-8")
    ).hexdigest()
    objective = config["selection"]["objective"]
    segment_name = str(config["selection"]["segment"])
    rows: list[dict[str, Any]] = []
    for student_t_df, buffer in candidate_buffers.items():
        truth = np.asarray(buffer["truth"], dtype=float)
        b7_prediction = np.asarray(buffer["b7"], dtype=float)
        b8_prediction = np.asarray(buffer["b8"], dtype=float)
        segment = np.asarray(buffer["segment"], dtype=str)
        segment_mask = segment == segment_name
        segment_rows = int(segment_mask.sum())
        if segment_rows < int(config["selection"]["minimum_segment_rows"]):
            raise ContractViolation(
                f"Gate B4 tuning context {context} has only {segment_rows} {segment_name} rows"
            )
        overall_mae = float(np.mean(np.abs(truth - b8_prediction)))
        reference_overall_mae = float(np.mean(np.abs(truth - b7_prediction)))
        segment_mae = float(
            np.mean(np.abs(truth[segment_mask] - b8_prediction[segment_mask]))
        )
        reference_segment_mae = float(
            np.mean(np.abs(truth[segment_mask] - b7_prediction[segment_mask]))
        )
        normalized_overall = overall_mae / reference_overall_mae
        normalized_segment = segment_mae / reference_segment_mae
        tuning_score = (
            float(objective["overall_normalized_mae_weight"])
            * normalized_overall
            + float(objective["volatile_or_gap_normalized_mae_weight"])
            * normalized_segment
        )
        rows.append(
            {
                "tuning_context": context,
                "student_t_df": student_t_df,
                "inner_folds": len(contract_rows),
                "validation_rows": len(truth),
                "volatile_or_gap_rows": segment_rows,
                "overall_mae": overall_mae,
                "reference_b7_overall_mae": reference_overall_mae,
                "overall_normalized_mae": normalized_overall,
                "volatile_or_gap_mae": segment_mae,
                "reference_b7_volatile_or_gap_mae": reference_segment_mae,
                "volatile_or_gap_normalized_mae": normalized_segment,
                "tuning_score": tuning_score,
                "inner_fold_contract_sha256": contract_hash,
                "all_inner_folds_forward_only": True,
            }
        )
    tuning = pd.DataFrame(rows).sort_values(
        [
            "tuning_score",
            "volatile_or_gap_normalized_mae",
            "overall_normalized_mae",
            "student_t_df",
        ],
        kind="mergesort",
    )
    tuning["selected"] = False
    tuning.loc[tuning.index[0], "selected"] = True
    tuning = tuning.sort_values("student_t_df", kind="mergesort").reset_index(drop=True)
    selected_df = float(tuning.loc[tuning["selected"], "student_t_df"].iloc[0])
    return robust_parameters(config, selected_df), tuning


def evaluate_outer_folds(
    source: ManifestDataset,
    folds: Sequence[EvaluationFold],
    *,
    bundle: CanonicalBundle,
    prepared_history: PreparedKalmanHistory,
    config: Mapping[str, Any],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    prediction_frames: list[pd.DataFrame] = []
    tuning_frames: list[pd.DataFrame] = []
    transition_policy = config["transition_validation"]
    for fold in folds:
        train_fold = derived_dataset(
            source,
            fold.train_sample_ids,
            split="train",
            label=f"gate_b4_{fold.fold_id}_train",
        )
        validation_fold = derived_dataset(
            source,
            fold.validation_sample_ids,
            split="validation",
            label=f"gate_b4_{fold.fold_id}_validation",
        )
        selected_parameters, tuning = tune_robust_df(
            train_fold,
            prepared_history=prepared_history,
            config=config,
            context=f"outer::{fold.fold_id}",
        )
        tuning_frames.append(tuning)
        thresholds = fit_transition_thresholds(
            train_fold.frame,
            acceleration_quantile=float(
                transition_policy["acceleration_absolute_quantile"]
            ),
            volatility_quantile=float(transition_policy["volatility_quantile"]),
            missing_campaigns_threshold=int(
                transition_policy["missing_campaigns_threshold"]
            ),
        )
        segments = classify_transition_proxy(validation_fold.frame, thresholds)
        truth = pd.to_numeric(
            validation_fold.frame[TARGET_COLUMN], errors="raise"
        ).to_numpy(float)
        model_specs = [dict(spec) for spec in config["frozen_comparators"]]
        model_specs.append(
            {
                "model_id": config["robust_model"]["model_id"],
                "family": config["robust_model"]["family"],
                "parameters": selected_parameters,
            }
        )
        for spec in model_specs:
            model = _build_gate_b4_model(spec, bundle=bundle, config=config)
            model.fit(train_fold)
            diagnostics: pd.DataFrame | None = None
            if hasattr(model, "predict_distribution"):
                prediction, raw_sigma, diagnostics = model.predict_distribution(
                    validation_fold,
                    history_frame=prepared_history,
                )
            else:
                prediction = model.predict(
                    validation_fold,
                    history_frame=causal_feature_history(source),
                )
                raw_sigma = np.full(len(validation_fold.frame), np.nan)
            if prediction.shape != truth.shape or not np.isfinite(prediction).all():
                raise RuntimeError(f"{model.model_id}/{fold.fold_id} produced invalid predictions")
            metadata = [
                "sample_id",
                "point_id",
                "profile_id",
                "zone_id",
                "current_date",
                "target_date",
                "forecast_horizon_days",
            ]
            frame = validation_fold.frame.loc[:, metadata].copy()
            frame.insert(0, "design", fold.design)
            frame.insert(1, "fold_id", fold.fold_id)
            frame.insert(2, "held_out_group", fold.held_out_group)
            frame.insert(3, "model_id", model.model_id)
            frame.insert(4, "family", model.family)
            frame["y_true"] = truth
            frame["y_pred"] = prediction
            frame["error"] = prediction - truth
            frame["absolute_error"] = np.abs(prediction - truth)
            frame["raw_sigma"] = raw_sigma
            for column in segments:
                frame[column] = segments[column].to_numpy()
            if isinstance(model, RobustInnovationIMMRate) and diagnostics is not None:
                frame["selected_student_t_df"] = float(
                    selected_parameters["student_t_df"]
                )
                for column in diagnostics:
                    frame[column] = diagnostics[column].to_numpy()
            prediction_frames.append(frame)
    return (
        pd.concat(prediction_frames, ignore_index=True),
        pd.concat(tuning_frames, ignore_index=True),
    )


def _build_gate_b4_model(
    spec: Mapping[str, Any],
    *,
    bundle: CanonicalBundle,
    config: Mapping[str, Any],
) -> Any:
    family = str(spec["family"])
    if family in {"persistence", "fixed_kalman"}:
        return build_model(
            spec,
            contract=bundle.feature_contract,
            random_seed=int(config["random_seed"]),
            weight_clip=(0.25, 4.0),
        )
    if family == "adaptive_kalman":
        return AdaptiveKalmanRate(
            model_id=str(spec["model_id"]),
            parameters=dict(spec["parameters"]),
        )
    if family == "imm_damped_acceleration":
        return TwoRegimeIMMRate(
            model_id=str(spec["model_id"]),
            parameters=dict(spec["parameters"]),
        )
    if family == "imm_student_t_robust_observation":
        return RobustInnovationIMMRate(
            model_id=str(spec["model_id"]),
            parameters=dict(spec["parameters"]),
        )
    raise KeyError(f"Unknown Gate B4 model family: {family}")


def fold_metrics(predictions: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for (design, fold_id, held_out, model_id, family), frame in predictions.groupby(
        ["design", "fold_id", "held_out_group", "model_id", "family"],
        sort=True,
        dropna=False,
    ):
        rows.append(
            {
                "design": design,
                "fold_id": fold_id,
                "held_out_group": "" if pd.isna(held_out) else held_out,
                "model_id": model_id,
                "family": family,
                "rows": len(frame),
                **regression_metrics(frame["y_true"], frame["y_pred"]),
            }
        )
    return pd.DataFrame(rows).sort_values(
        ["design", "fold_id", "mae", "model_id"], kind="mergesort"
    ).reset_index(drop=True)


def aggregate_metrics(predictions: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for (design, model_id, family), frame in predictions.groupby(
        ["design", "model_id", "family"], sort=True
    ):
        metrics = regression_metrics(frame["y_true"], frame["y_pred"])
        rows.append(
            {
                "design": design,
                "model_id": model_id,
                "family": family,
                "folds": int(frame["fold_id"].nunique()),
                **metrics,
            }
        )
    result = pd.DataFrame(rows)
    lookup = result.set_index(["design", "model_id"])["mae"]
    result["reference_b7_mae"] = [
        float(lookup.loc[(row.design, "B7_two_regime_imm")])
        for row in result.itertuples(index=False)
    ]
    result["improvement_vs_b7_percent"] = 100.0 * (
        result["reference_b7_mae"] - result["mae"]
    ) / result["reference_b7_mae"]
    return result.sort_values(
        ["design", "mae", "model_id"], kind="mergesort"
    ).reset_index(drop=True)


def robustness_diagnostics(predictions: pd.DataFrame) -> pd.DataFrame:
    robust = predictions.loc[
        predictions["model_id"].eq("B8_student_t_robust_imm")
    ].copy()
    rows: list[dict[str, Any]] = []
    for (design, fold_id, held_out), frame in robust.groupby(
        ["design", "fold_id", "held_out_group"], sort=True, dropna=False
    ):
        update_count = pd.to_numeric(frame["robust_update_count"], errors="raise")
        downweighted = pd.to_numeric(
            frame["robust_downweighted_update_count"], errors="raise"
        )
        weights = pd.to_numeric(frame["robust_weight_mean"], errors="raise")
        total_updates = int(update_count.sum())
        rows.append(
            {
                "design": design,
                "fold_id": fold_id,
                "held_out_group": "" if pd.isna(held_out) else held_out,
                "rows": len(frame),
                "student_t_df": float(frame["selected_student_t_df"].iloc[0]),
                "robust_updates": total_updates,
                "downweighted_updates": int(downweighted.sum()),
                "downweighted_update_rate": float(downweighted.sum() / total_updates)
                if total_updates
                else 0.0,
                "weighted_mean_influence_weight": float(
                    np.average(weights, weights=np.maximum(update_count, 1))
                ),
                "minimum_influence_weight": float(
                    pd.to_numeric(frame["robust_weight_min"], errors="raise").min()
                ),
                "maximum_standardized_innovation_squared": float(
                    pd.to_numeric(
                        frame["robust_innovation_z2_max"], errors="raise"
                    ).max()
                ),
            }
        )
    return pd.DataFrame(rows).sort_values(
        ["design", "fold_id"], kind="mergesort"
    ).reset_index(drop=True)


def screening_assessment(
    aggregate: pd.DataFrame,
    transition: pd.DataFrame,
    diagnostics: pd.DataFrame,
    *,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    acceptance = config["acceptance"]
    lookup = aggregate.set_index(["design", "model_id"])["mae"]
    temporal_b8 = float(
        lookup.loc[("internal_temporal", "B8_student_t_robust_imm")]
    )
    temporal_b7 = float(
        lookup.loc[("internal_temporal", "B7_two_regime_imm")]
    )
    profile_b8 = float(
        lookup.loc[("train_leave_profile_out", "B8_student_t_robust_imm")]
    )
    zone_b8 = float(
        lookup.loc[("train_leave_zone_out", "B8_student_t_robust_imm")]
    )
    zone_b7 = float(
        lookup.loc[("train_leave_zone_out", "B7_two_regime_imm")]
    )
    mechanisms = transition.loc[
        transition["design"].eq("internal_temporal")
        & transition["scope"].eq("mechanism")
    ].set_index(["segment", "model_id"])["mae"]
    volatile_b8 = float(
        mechanisms.loc[("volatile_or_gap", "B8_student_t_robust_imm")]
    )
    volatile_b7 = float(
        mechanisms.loc[("volatile_or_gap", "B7_two_regime_imm")]
    )
    accelerating_b8 = float(
        mechanisms.loc[("accelerating", "B8_student_t_robust_imm")]
    )
    accelerating_b7 = float(
        mechanisms.loc[("accelerating", "B7_two_regime_imm")]
    )
    downweighted_updates = int(diagnostics["downweighted_updates"].sum())
    observed = {
        "internal_temporal_b8_mae": temporal_b8,
        "internal_temporal_b7_mae": temporal_b7,
        "internal_temporal_mae_ratio_vs_b7": temporal_b8 / temporal_b7,
        "volatile_or_gap_b8_mae": volatile_b8,
        "volatile_or_gap_b7_mae": volatile_b7,
        "volatile_or_gap_improvement_vs_b7_percent": 100.0
        * (volatile_b7 - volatile_b8)
        / volatile_b7,
        "accelerating_b8_mae": accelerating_b8,
        "accelerating_b7_mae": accelerating_b7,
        "accelerating_mae_ratio_vs_b7": accelerating_b8 / accelerating_b7,
        "leave_profile_b8_mae": profile_b8,
        "leave_profile_degradation_vs_internal_temporal_percent": 100.0
        * (profile_b8 - temporal_b8)
        / temporal_b8,
        "leave_zone_b8_mae": zone_b8,
        "leave_zone_b7_mae": zone_b7,
        "leave_zone_degradation_vs_internal_temporal_percent": 100.0
        * (zone_b8 - temporal_b8)
        / temporal_b8,
        "leave_zone_mae_ratio_vs_b7": zone_b8 / zone_b7,
        "robust_downweighted_updates": downweighted_updates,
    }
    checks = {
        "internal_temporal_vs_b7": observed[
            "internal_temporal_mae_ratio_vs_b7"
        ]
        <= float(acceptance["internal_temporal_mae_ratio_vs_b7_max"]),
        "volatile_or_gap_vs_b7": observed[
            "volatile_or_gap_improvement_vs_b7_percent"
        ]
        >= float(acceptance["volatile_or_gap_improvement_vs_b7_percent_min"]),
        "accelerating_vs_b7": observed["accelerating_mae_ratio_vs_b7"]
        <= float(acceptance["accelerating_mae_ratio_vs_b7_max"]),
        "leave_profile_stability": observed[
            "leave_profile_degradation_vs_internal_temporal_percent"
        ]
        <= float(
            acceptance[
                "leave_profile_degradation_vs_internal_temporal_percent_max"
            ]
        ),
        "leave_zone_stability": observed[
            "leave_zone_degradation_vs_internal_temporal_percent"
        ]
        <= float(
            acceptance[
                "leave_zone_degradation_vs_internal_temporal_percent_max"
            ]
        ),
        "leave_zone_vs_b7": observed["leave_zone_mae_ratio_vs_b7"]
        <= float(acceptance["leave_zone_mae_ratio_vs_b7_max"]),
        "robust_mechanism_active": downweighted_updates
        >= int(acceptance["robust_downweighted_updates_min"]),
    }
    return {
        "scope": "t1_v1_train_nested_resampling_only",
        "observed": observed,
        "criteria": dict(acceptance),
        "checks": {key: bool(value) for key, value in checks.items()},
        "all_pass": bool(all(checks.values())),
    }


def run_gate_b4_development(root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    before_snapshot = capture_protected_predecessor_snapshot(root, config)
    bundle = load_canonical_bundle(root)
    train = load_split_dataset("t1", "train", root=root)
    if train.provenance.version != "t1_v1":
        raise ContractViolation("Gate B4 requires the frozen t1_v1 train manifest")
    source, folds, contracts = build_train_only_folds(
        train,
        bundle,
        rolling_folds=int(config["resampling"]["rolling_origin_folds"]),
    )
    split_root = resolve_repo_path(root, config["artifacts"]["split_root"])
    split_record = freeze_train_only_manifests(
        root,
        source,
        folds,
        contracts,
        split_root=split_root,
    )
    prepared_history = prepare_kalman_history(causal_feature_history(source))
    predictions, outer_tuning = evaluate_outer_folds(
        source,
        folds,
        bundle=bundle,
        prepared_history=prepared_history,
        config=config,
    )
    selected_parameters, final_tuning = tune_robust_df(
        train,
        prepared_history=prepared_history,
        config=config,
        context="final_spec::full_train",
    )
    tuning = pd.concat([outer_tuning, final_tuning], ignore_index=True)
    fold_metric_frame = fold_metrics(predictions)
    aggregate = aggregate_metrics(predictions)
    transition = transition_metrics(predictions)
    diagnostics = robustness_diagnostics(predictions)
    screening = screening_assessment(
        aggregate,
        transition,
        diagnostics,
        config=config,
    )
    robust_model = RobustInnovationIMMRate(
        model_id=str(config["robust_model"]["model_id"]),
        parameters=selected_parameters,
    ).fit(train)
    source_hashes = {
        str(relative): sha256_file(resolve_repo_path(root, relative))
        for relative in config["source_files"]
    }
    candidate_digest_payload = {
        "model_id": robust_model.model_id,
        "parameters": selected_parameters,
        "train_sample_ids_sha256": train.provenance.sample_ids_sha256,
        "source_hashes": source_hashes,
    }
    candidate_digest = sha256(
        json.dumps(candidate_digest_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()
    candidate_id = f"t1-b4-train-v1-{candidate_digest[:12]}"
    candidate_status = (
        config["acceptance"]["status_if_pass"]
        if screening["all_pass"]
        else config["acceptance"]["status_if_fail"]
    )
    candidate = {
        "schema_version": 1,
        "candidate_id": candidate_id,
        "status": candidate_status,
        "candidate_scope": "gate_b4_t1_train_nested_resampling_only",
        "selected_model": robust_model.model_id,
        "selected_parameters": selected_parameters,
        "model_state": robust_model.state_dict(),
        "selection_data": ["t1_v1/train"],
        "historical_validation_used": False,
        "current_t1_test_used": False,
        "eligible_for_final_claim": False,
        "screening_passed": screening["all_pass"],
        "screening": screening,
        "train_manifest_hash": train.provenance.sample_ids_sha256,
        "feature_contract_sha256": bundle.feature_contract.source_sha256,
        "target_contract_sha256": bundle.target_contract.source_sha256,
        "source_hashes": source_hashes,
        "final_evaluation_status": "PENDING_NEW_HOLDOUT",
    }
    primary_model_id = (
        robust_model.model_id
        if screening["all_pass"]
        else "B7_two_regime_imm"
    )
    suite_models = [dict(spec) for spec in config["frozen_comparators"]]
    suite_models.append(
        {
            "model_id": robust_model.model_id,
            "family": robust_model.family,
            "parameters": selected_parameters,
            "gate_b4_candidate_id": candidate_id,
        }
    )
    candidate_suite = {
        "schema_version": 1,
        "suite_id": f"t1-final-suite-v3-{candidate_digest[:12]}",
        "status": "frozen_before_new_holdout_labels",
        "primary_model_id": primary_model_id,
        "primary_selection_rule": (
            "B8 only if every predeclared train-only Gate B4 screening check passes; otherwise frozen B7"
        ),
        "primary_selected_from_holdout": False,
        "models": suite_models,
        "comparator_results_role": "context_only_no_post_holdout_selection",
        "training_data": ["t1_v1/train"],
        "train_sample_ids_sha256": train.provenance.sample_ids_sha256,
        "feature_contract_sha256": bundle.feature_contract.source_sha256,
        "target_contract_sha256": bundle.target_contract.source_sha256,
        "source_hashes": source_hashes,
        "historical_validation_loaded_by_gate_b4": False,
        "current_t1_test_loaded_by_gate_b4": False,
        "new_holdout_seen": False,
    }
    report = {
        "schema_version": 1,
        "gate": config["gate"],
        "status": candidate_status,
        "question": (
            "Does a predeclared Student-t robust observation channel improve volatile/gap behaviour under nested train-only resampling?"
        ),
        "data": {
            "source": "t1_v1/train",
            "rows": len(train.frame),
            "target_date_min": pd.Timestamp(
                pd.to_datetime(train.frame["target_date"]).min()
            ).date().isoformat(),
            "target_date_max": pd.Timestamp(
                pd.to_datetime(train.frame["target_date"]).max()
            ).date().isoformat(),
            "points": int(train.frame["point_id"].astype(str).nunique()),
            "profiles": int(train.frame["profile_id"].astype(str).nunique()),
            "train_sample_ids_sha256": train.provenance.sample_ids_sha256,
            "internal_audit_tail_rows": split_record["audit_tail_rows"],
            "historical_validation_rows_loaded": 0,
            "current_test_rows_loaded": 0,
        },
        "model": {
            "model_id": robust_model.model_id,
            "family": robust_model.family,
            "selected_student_t_df": selected_parameters["student_t_df"],
            "df_grid": list(config["robust_model"]["student_t_df_grid"]),
            "minimum_robust_weight": selected_parameters[
                "minimum_robust_weight"
            ],
            "frozen_dynamics_source": "B7_two_regime_imm",
        },
        "resampling": split_record,
        "screening": screening,
        "candidate_id": candidate_id,
        "candidate_suite_id": candidate_suite["suite_id"],
        "predeclared_primary_model_id": primary_model_id,
        "historical_validation_loaded": False,
        "current_t1_test_loaded": False,
        "new_final_holdout_loaded": False,
        "final_holdout_status": "PENDING_DATA",
        "protected_predecessors_match": before_snapshot["all_match"],
    }

    paths = {
        key: resolve_repo_path(root, value)
        for key, value in config["artifacts"].items()
        if key not in {"root", "split_root"}
    }
    write_json_atomic(root, paths["protected_predecessor_snapshot"], before_snapshot)
    write_json_atomic(root, paths["split_record"], split_record)
    write_csv_atomic(root, paths["fold_contracts"], contracts)
    write_csv_atomic(root, paths["robust_tuning"], tuning)
    write_csv_atomic(root, paths["outer_predictions"], predictions)
    write_csv_atomic(root, paths["fold_metrics"], fold_metric_frame)
    write_csv_atomic(root, paths["aggregate_metrics"], aggregate)
    write_csv_atomic(root, paths["transition_metrics"], transition)
    write_csv_atomic(root, paths["robustness_diagnostics"], diagnostics)
    write_json_atomic(root, paths["research_candidate"], candidate)
    write_json_atomic(root, paths["candidate_suite"], candidate_suite)
    write_json_atomic(root, paths["gate_report"], report)

    after_snapshot = capture_protected_predecessor_snapshot(root, config)
    if before_snapshot != after_snapshot:
        raise ContractViolation("Protected predecessors changed during Gate B4")
    return {
        "phase": "develop",
        "status": candidate_status,
        "candidate_id": candidate_id,
        "primary_model_id": primary_model_id,
        "selected_student_t_df": selected_parameters["student_t_df"],
        "screening_passed": screening["all_pass"],
        "historical_validation_loaded": False,
        "current_t1_test_loaded": False,
        "final_holdout_status": "PENDING_DATA",
    }


def run_gate_b4_validation(
    root: Path,
    config: Mapping[str, Any],
    *,
    write_outputs: bool = True,
) -> dict[str, Any]:
    """Independently recompute published Gate B4 contracts and key metrics."""

    checks: list[dict[str, Any]] = []

    def add(name: str, passed: bool, observed: Any, expected: Any) -> None:
        checks.append(
            {
                "check": name,
                "passed": bool(passed),
                "observed": _plain_value(observed),
                "expected": _plain_value(expected),
            }
        )

    paths = {
        key: resolve_repo_path(root, value)
        for key, value in config["artifacts"].items()
        if key not in {"root", "split_root"}
    }
    required = [
        "protected_predecessor_snapshot",
        "split_record",
        "fold_contracts",
        "robust_tuning",
        "outer_predictions",
        "fold_metrics",
        "aggregate_metrics",
        "transition_metrics",
        "robustness_diagnostics",
        "research_candidate",
        "candidate_suite",
        "gate_report",
    ]
    for key in required:
        add(f"artifact_exists::{key}", paths[key].is_file(), paths[key].is_file(), True)
    if any(not paths[key].is_file() for key in required):
        raise FileNotFoundError("Gate B4 validation requires completed development artifacts")

    snapshot = json.loads(
        paths["protected_predecessor_snapshot"].read_text(encoding="utf-8")
    )
    add(
        "protected_snapshot_recomputed",
        snapshot == capture_protected_predecessor_snapshot(root, config),
        snapshot,
        capture_protected_predecessor_snapshot(root, config),
    )
    bundle = load_canonical_bundle(root)
    train = load_split_dataset("t1", "train", root=root)
    source, folds, expected_contracts = build_train_only_folds(
        train,
        bundle,
        rolling_folds=int(config["resampling"]["rolling_origin_folds"]),
    )
    stored_contracts = pd.read_csv(paths["fold_contracts"])
    add(
        "fold_contracts_recomputed",
        frames_equivalent(stored_contracts, expected_contracts),
        len(stored_contracts),
        len(expected_contracts),
    )
    design_counts = stored_contracts.groupby("design")["fold_id"].nunique().to_dict()
    expected_design_counts = {
        "internal_temporal": 1,
        "train_rolling_origin": 5,
        "train_leave_profile_out": 14,
        "train_leave_zone_out": 4,
    }
    add("design_counts", design_counts == expected_design_counts, design_counts, expected_design_counts)
    add(
        "all_outer_folds_forward_only",
        bool(
            (
                pd.to_datetime(stored_contracts["train_target_date_max"])
                < pd.to_datetime(stored_contracts["validation_target_date_min"])
            ).all()
        ),
        True,
        True,
    )

    predictions = pd.read_csv(paths["outer_predictions"])
    source_ids = set(source.frame["sample_id"].astype(str))
    prediction_ids = set(predictions["sample_id"].astype(str))
    add("prediction_ids_subset_train", prediction_ids <= source_ids, len(prediction_ids - source_ids), 0)
    add(
        "model_set_exact",
        set(predictions["model_id"].astype(str)) == set(MODEL_IDS),
        sorted(predictions["model_id"].astype(str).unique()),
        list(MODEL_IDS),
    )
    add(
        "model_rows_aligned",
        predictions.groupby(["design", "fold_id"])["model_id"].nunique().eq(5).all(),
        predictions.groupby(["design", "fold_id"])["model_id"].nunique().min(),
        5,
    )
    add(
        "predictions_finite",
        bool(
            np.isfinite(pd.to_numeric(predictions["y_true"], errors="coerce")).all()
            and np.isfinite(pd.to_numeric(predictions["y_pred"], errors="coerce")).all()
        ),
        True,
        True,
    )
    recomputed_fold = fold_metrics(predictions)
    stored_fold = pd.read_csv(paths["fold_metrics"])
    add("fold_metrics_recomputed", frames_equivalent(stored_fold, recomputed_fold), len(stored_fold), len(recomputed_fold))
    recomputed_aggregate = aggregate_metrics(predictions)
    stored_aggregate = pd.read_csv(paths["aggregate_metrics"])
    add(
        "aggregate_metrics_recomputed",
        frames_equivalent(stored_aggregate, recomputed_aggregate),
        len(stored_aggregate),
        len(recomputed_aggregate),
    )
    recomputed_transition = transition_metrics(predictions)
    stored_transition = pd.read_csv(paths["transition_metrics"])
    add(
        "transition_metrics_recomputed",
        frames_equivalent(stored_transition, recomputed_transition),
        len(stored_transition),
        len(recomputed_transition),
    )
    recomputed_diagnostics = robustness_diagnostics(predictions)
    stored_diagnostics = pd.read_csv(paths["robustness_diagnostics"])
    add(
        "robustness_diagnostics_recomputed",
        frames_equivalent(stored_diagnostics, recomputed_diagnostics),
        len(stored_diagnostics),
        len(recomputed_diagnostics),
    )

    tuning = pd.read_csv(paths["robust_tuning"])
    per_context_candidates = tuning.groupby("tuning_context")["student_t_df"].nunique()
    add(
        "four_candidates_per_tuning_context",
        per_context_candidates.eq(4).all(),
        per_context_candidates.to_dict(),
        "all 4",
    )
    selected_counts = tuning.groupby("tuning_context")["selected"].sum()
    add(
        "one_selected_per_tuning_context",
        selected_counts.eq(1).all(),
        selected_counts.to_dict(),
        "all 1",
    )
    selected_minimum = True
    for _, frame in tuning.groupby("tuning_context"):
        selected_score = float(frame.loc[frame["selected"].astype(bool), "tuning_score"].iloc[0])
        selected_minimum &= np.isclose(selected_score, float(frame["tuning_score"].min()))
    add("selected_candidate_minimizes_score", selected_minimum, selected_minimum, True)
    add(
        "all_inner_folds_forward_only",
        tuning["all_inner_folds_forward_only"].astype(bool).all(),
        tuning["all_inner_folds_forward_only"].astype(bool).all(),
        True,
    )

    report = json.loads(paths["gate_report"].read_text(encoding="utf-8"))
    candidate = json.loads(paths["research_candidate"].read_text(encoding="utf-8"))
    suite = json.loads(paths["candidate_suite"].read_text(encoding="utf-8"))
    screening = screening_assessment(
        recomputed_aggregate,
        recomputed_transition,
        recomputed_diagnostics,
        config=config,
    )
    add(
        "screening_recomputed",
        mappings_close(screening, report["screening"]),
        screening,
        report["screening"],
    )
    full_selected = tuning.loc[
        tuning["tuning_context"].eq("final_spec::full_train")
        & tuning["selected"].astype(bool),
        "student_t_df",
    ]
    add(
        "candidate_df_matches_full_train_tuning",
        len(full_selected) == 1
        and np.isclose(
            float(candidate["selected_parameters"]["student_t_df"]),
            float(full_selected.iloc[0]),
        ),
        candidate["selected_parameters"]["student_t_df"],
        float(full_selected.iloc[0]),
    )
    expected_primary = (
        "B8_student_t_robust_imm"
        if screening["all_pass"]
        else "B7_two_regime_imm"
    )
    add("suite_primary_predeclared_rule", suite["primary_model_id"] == expected_primary, suite["primary_model_id"], expected_primary)
    add(
        "suite_never_saw_holdout",
        suite["new_holdout_seen"] is False
        and suite["primary_selected_from_holdout"] is False,
        {
            "new_holdout_seen": suite["new_holdout_seen"],
            "primary_selected_from_holdout": suite["primary_selected_from_holdout"],
        },
        {"new_holdout_seen": False, "primary_selected_from_holdout": False},
    )
    add(
        "candidate_boundary",
        candidate["selection_data"] == ["t1_v1/train"]
        and candidate["historical_validation_used"] is False
        and candidate["current_t1_test_used"] is False
        and candidate["eligible_for_final_claim"] is False,
        {
            "selection_data": candidate["selection_data"],
            "historical_validation_used": candidate["historical_validation_used"],
            "current_t1_test_used": candidate["current_t1_test_used"],
        },
        "train only; no validation/test; not final",
    )
    for relative, expected_hash in candidate["source_hashes"].items():
        observed_hash = sha256_file(resolve_repo_path(root, relative))
        governed_successor = _governed_suite_v4_source_hash_matches(
            root,
            relative,
            observed_hash,
        )
        add(
            f"candidate_source_hash::{relative}",
            observed_hash == expected_hash or governed_successor,
            {
                "sha256": observed_hash,
                "acceptance": (
                    "historical_exact"
                    if observed_hash == expected_hash
                    else "governed_suite_v4_successor"
                ),
            },
            {
                "historical_sha256": expected_hash,
                "governed_successor_allowed": True,
            },
        )
    scan_paths = [resolve_repo_path(root, relative) for relative in config["source_files"] if str(relative).endswith(".py")]
    findings = find_forbidden_split_loader_calls(scan_paths)
    add("no_validation_or_test_loader_call", findings == [], findings, [])
    holdout_config = yaml.safe_load(
        (root / "configs" / "final_holdout_v3.yaml").read_text(encoding="utf-8")
    )
    add("new_holdout_pending", holdout_config["status"] == "PENDING_DATA", holdout_config["status"], "PENDING_DATA")

    failed = [check for check in checks if not check["passed"]]
    validation_report = {
        "schema_version": 1,
        "gate": config["gate"],
        "status": "PASS" if not failed else "FAIL",
        "overall_assessment": "Share with caveats" if not failed else "Needs revision",
        "question": (
            "Are Gate B4 train-only boundaries, nested selection, frozen comparators, metrics, diagnostics, and candidate-suite rule reproducible?"
        ),
        "summary": {"checks": len(checks), "failed": len(failed)},
        "checks": checks,
        "methodology_review": {
            "data_boundary": "every scored sample ID independently shown to belong to t1_v1/train; AST scan rejects validation/test loader calls",
            "resampling": "24 forward-only outer contracts rebuilt from train and all tuning contexts assert three forward inner folds",
            "metrics": "fold, aggregate, transition, and robust influence summaries recomputed from prediction rows",
            "selection": "four predeclared degrees-of-freedom candidates per context and deterministic minimum-score selection",
            "holdout": "candidate suite names its primary before any new holdout exists",
        },
        "remaining_caveats": [
            "The internal audit tail is part of the original train era and is not an external-validity estimate.",
            "Profile and zone folds reuse the same internal tail, so fold scores are dependent.",
            "The Student-t mechanism changes all scalar observation channels with one shared degrees of freedom.",
            "No eligible future/external final holdout is currently available.",
        ],
    }
    artifact_root = resolve_repo_path(root, config["artifacts"]["root"])
    inventory_path = paths["artifact_inventory"]
    if write_outputs:
        write_json_atomic(root, paths["validation_report"], validation_report)
        sources = sorted(
            [
                path
                for path in artifact_root.rglob("*")
                if path.is_file() and path != inventory_path
            ],
            key=lambda path: path.relative_to(root).as_posix(),
        )
        inventory = pd.DataFrame(
            [
                {
                    "relative_path": path.relative_to(root).as_posix(),
                    "size_bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
                for path in sources
            ]
        )
        write_csv_atomic(root, inventory_path, inventory)
    return {
        "phase": "validate",
        "status": validation_report["status"],
        "overall_assessment": validation_report["overall_assessment"],
        "checks": len(checks),
        "failed": len(failed),
        "artifact_inventory": inventory_path.relative_to(root).as_posix(),
        "outputs_written": write_outputs,
    }


def find_forbidden_split_loader_calls(paths: Iterable[Path]) -> list[str]:
    findings: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = ""
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            if name != "load_split_dataset":
                continue
            split_value: str | None = None
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                split_value = str(node.args[1].value)
            for keyword in node.keywords:
                if keyword.arg == "split" and isinstance(keyword.value, ast.Constant):
                    split_value = str(keyword.value.value)
            if split_value is not None and split_value.lower() != "train":
                findings.append(f"{path.as_posix()}:{node.lineno}:{split_value}")
    return findings


def frames_equivalent(left: pd.DataFrame, right: pd.DataFrame) -> bool:
    if set(left.columns) != set(right.columns) or len(left) != len(right):
        return False
    columns = list(right.columns)
    sort_columns = [
        column
        for column in (
            "design",
            "fold_id",
            "held_out_group",
            "model_id",
            "scope",
            "segment",
            "sample_id",
        )
        if column in columns
    ]
    left_ordered = left.loc[:, columns].copy()
    right_ordered = right.loc[:, columns].copy()
    for frame in (left_ordered, right_ordered):
        if "held_out_group" in frame:
            frame["held_out_group"] = frame["held_out_group"].fillna("").astype(str)
    if sort_columns:
        left_ordered = left_ordered.sort_values(sort_columns, kind="mergesort")
        right_ordered = right_ordered.sort_values(sort_columns, kind="mergesort")
    try:
        pd.testing.assert_frame_equal(
            left_ordered.reset_index(drop=True),
            right_ordered.reset_index(drop=True),
            check_dtype=False,
            check_exact=False,
            rtol=1e-10,
            atol=1e-10,
        )
    except AssertionError:
        return False
    return True


def _governed_suite_v4_source_hash_matches(
    root: Path,
    relative: str,
    observed_hash: str | None,
) -> bool:
    """Accept a changed historical source only when frozen suite v4 owns its hash."""

    if observed_hash is None:
        return False
    suite_path = root / "artifacts" / "governance" / "final_candidate_suite_v4.json"
    if not suite_path.is_file():
        return False
    suite = json.loads(suite_path.read_text(encoding="utf-8"))
    predecessor = suite.get("predecessor_suite", {})
    predecessor_relative = predecessor.get("relative_path")
    if (
        suite.get("new_holdout_seen") is not False
        or suite.get("primary_selected_from_holdout") is not False
        or predecessor.get("immutable") is not True
        or predecessor_relative
        != "artifacts/governance/final_candidate_suite_v3.json"
    ):
        return False
    predecessor_path = resolve_repo_path(root, predecessor_relative)
    return bool(
        predecessor_path.is_file()
        and sha256_file(predecessor_path) == predecessor.get("sha256")
        and suite.get("source_hashes", {}).get(relative) == observed_hash
    )


def mappings_close(left: Any, right: Any) -> bool:
    """Compare nested machine reports while tolerating CSV round-trip floats."""

    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return set(left) == set(right) and all(
            mappings_close(left[key], right[key]) for key in left
        )
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(
            mappings_close(a, b) for a, b in zip(left, right, strict=True)
        )
    if isinstance(left, (float, np.floating)) or isinstance(
        right, (float, np.floating)
    ):
        try:
            return bool(np.isclose(float(left), float(right), rtol=1e-10, atol=1e-10))
        except (TypeError, ValueError):
            return False
    return left == right


def write_json_atomic(root: Path, path: Path, payload: Mapping[str, Any]) -> None:
    text = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        default=_json_default,
    )
    _write_text_atomic(root, path, text + "\n")


def write_csv_atomic(root: Path, path: Path, frame: pd.DataFrame) -> None:
    _write_text_atomic(root, path, frame.to_csv(index=False, lineterminator="\n"))


def _write_text_atomic(root: Path, path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    work_dir = root / "work" / "gate_b4"
    work_dir.mkdir(parents=True, exist_ok=True)
    temporary = work_dir / f"{path.name}.{uuid4().hex}.tmp"
    temporary.write_text(text, encoding="utf-8", newline="\n")
    temporary.replace(path)


def _plain_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _plain_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_value(item) for item in value]
    if hasattr(value, "item"):
        return value.item()
    return value


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(f"Cannot serialize {type(value).__name__}")

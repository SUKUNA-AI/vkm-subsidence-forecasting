"""Governed train/validation-only workflow for the Gate B3 two-regime IMM."""

from __future__ import annotations

import ast
from collections import Counter
from hashlib import sha256
import importlib.metadata
from itertools import product
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from uuid import uuid4

import numpy as np
import pandas as pd
import yaml

from .adaptive_kalman import PreparedKalmanHistory, prepare_kalman_history
from .baselines import TARGET_COLUMN
from .data_contracts import (
    CanonicalBundle,
    ContractViolation,
    discover_project_root,
    load_canonical_bundle,
    sha256_file,
)
from .evaluation import (
    EvaluationFold,
    build_gate_b0_b1_folds,
    causal_feature_history,
    derived_dataset,
)
from .imm_kalman import TwoRegimeIMMRate
from .metrics import regression_metrics
from .splits import (
    ManifestDataset,
    load_split_dataset,
    read_manifest,
    rolling_origin_assignments,
)
from .transition_validation import (
    classify_transition_proxy,
    fit_transition_thresholds,
    transition_metrics,
)
from .uncertainty import (
    apply_scaled_conformal_intervals,
    calibrate_scaled_conformal,
    finite_sample_conformal_quantile,
    interval_metrics,
)


def load_gate_b3_config(
    root: str | Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    project_root = discover_project_root(root)
    path = project_root / "configs" / "gate_b3.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ContractViolation("configs/gate_b3.yaml must contain a mapping")
    required = {
        "task",
        "split_version",
        "test_policy",
        "comparator_policy",
        "development_policy",
        "imm_model",
        "interval_calibration",
        "transition_validation",
        "acceptance",
        "artifacts",
    }
    missing = required - set(config)
    if missing:
        raise ContractViolation(f"Gate B3 config is missing keys: {sorted(missing)}")
    for value in config["artifacts"].values():
        resolve_repo_path(project_root, str(value))
    for section in ("test_policy", "comparator_policy"):
        for value in config[section]["protected_files"]:
            resolve_repo_path(project_root, str(value))
    if config["test_policy"]["model_selection_access"] != "prohibited":
        raise ContractViolation("Gate B3 must prohibit current T1 test selection access")
    if config["comparator_policy"]["mode"] != "frozen_predictions_only_no_refit":
        raise ContractViolation("Gate B3 comparators must be reused without refit")
    objective = config["development_policy"]["tuning_objective"]
    weight_sum = float(objective["overall_normalized_mae_weight"]) + float(
        objective["problem_transition_normalized_mae_weight"]
    )
    if not np.isclose(weight_sum, 1.0):
        raise ContractViolation("Gate B3 tuning weights must sum to one")
    return project_root, config


def resolve_repo_path(root: Path, relative: str | Path) -> Path:
    path = Path(relative)
    if path.is_absolute():
        raise ContractViolation(f"Gate B3 path must be repository-relative: {path}")
    resolved = (root / path).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ContractViolation(f"Gate B3 path escapes repository root: {path}") from exc
    return resolved


def capture_protected_predecessor_snapshot(
    root: Path,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """Verify disclosed-test and frozen Gate B2 artifacts by hash only."""

    rows: list[dict[str, Any]] = []
    for role, section in (
        ("disclosed_test", "test_policy"),
        ("frozen_comparator", "comparator_policy"),
    ):
        for relative, expected_hash in config[section]["protected_files"].items():
            path = resolve_repo_path(root, relative)
            if not path.is_file():
                raise FileNotFoundError(f"Protected predecessor artifact is missing: {relative}")
            observed = sha256_file(path)
            if observed.lower() != str(expected_hash).lower():
                raise ContractViolation(
                    f"Protected predecessor artifact changed before Gate B3: {relative}"
                )
            rows.append(
                {
                    "role": role,
                    "relative_path": Path(relative).as_posix(),
                    "size_bytes": path.stat().st_size,
                    "expected_sha256": str(expected_hash).lower(),
                    "observed_sha256": observed,
                    "matches": True,
                }
            )
    return {
        "schema_version": 1,
        "policy": "hash_only_no_test_loader_frozen_b2_predictions",
        "protected_files": rows,
        "all_match": True,
    }


def imm_parameters(
    config: Mapping[str, Any],
    *,
    q_stable: float,
    q_transition: float,
    p_stable_stay: float,
    p_transition_stay: float,
) -> dict[str, Any]:
    parameters = dict(config["imm_model"]["parameters"])
    parameters.update(
        {
            "q_stable": float(q_stable),
            "q_transition": float(q_transition),
            "p_stable_stay": float(p_stable_stay),
            "p_transition_stay": float(p_transition_stay),
        }
    )
    return parameters


def imm_parameter_grid(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    model = config["imm_model"]
    return [
        imm_parameters(
            config,
            q_stable=float(q_stable),
            q_transition=float(q_transition),
            p_stable_stay=float(p_stable_stay),
            p_transition_stay=float(p_transition_stay),
        )
        for q_stable, q_transition, p_stable_stay, p_transition_stay in product(
            model["q_stable_grid"],
            model["q_transition_grid"],
            model["p_stable_stay_grid"],
            model["p_transition_stay_grid"],
        )
    ]


def tune_imm_parameters(
    training: ManifestDataset,
    *,
    prepared_history: PreparedKalmanHistory,
    config: Mapping[str, Any],
    context: str,
) -> tuple[dict[str, Any], pd.DataFrame]:
    """Select one predeclared IMM variant using inner train-only folds."""

    policy = config["development_policy"]
    assignments = rolling_origin_assignments(
        [training],
        minimum_train_dates=int(policy["inner_minimum_train_dates"]),
        maximum_folds=int(policy["inner_rolling_origin_folds"]),
    )
    transition_policy = config["transition_validation"]
    problem_segments = set(
        map(str, policy["tuning_objective"]["problem_transition_segments"])
    )
    inner_contexts: list[dict[str, Any]] = []
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
            label=f"gate_b3_{context}_{fold_id}_train",
        )
        inner_validation = derived_dataset(
            training,
            validation_ids,
            split="validation",
            label=f"gate_b3_{context}_{fold_id}_validation",
        )
        thresholds = fit_transition_thresholds(
            inner_train.frame,
            acceleration_quantile=float(
                transition_policy["inner_acceleration_absolute_quantile"]
            ),
            volatility_quantile=float(
                transition_policy["inner_volatility_quantile"]
            ),
            missing_campaigns_threshold=int(
                transition_policy["inner_missing_campaigns_threshold"]
            ),
        )
        segments = classify_transition_proxy(inner_validation.frame, thresholds)[
            "transition_segment"
        ].astype(str)
        truth = pd.to_numeric(
            inner_validation.frame[TARGET_COLUMN], errors="raise"
        ).to_numpy(float)
        fallback = float(
            pd.to_numeric(
                inner_train.frame[TARGET_COLUMN], errors="coerce"
            ).dropna().median()
        )
        last_rate = pd.to_numeric(
            inner_validation.frame["last_rate_mm_y"], errors="coerce"
        ).to_numpy(float)
        b1_prediction = np.where(np.isfinite(last_rate), last_rate, fallback)
        inner_contexts.append(
            {
                "fold_id": str(fold_id),
                "train": inner_train,
                "validation": inner_validation,
                "truth": truth,
                "b1_prediction": b1_prediction,
                "problem_mask": segments.isin(problem_segments).to_numpy(bool),
                "thresholds": thresholds,
            }
        )

    objective = policy["tuning_objective"]
    candidate_summaries: list[dict[str, Any]] = []
    candidate_fold_rows: list[list[dict[str, Any]]] = []
    for parameters in imm_parameter_grid(config):
        candidate_key = _candidate_key(parameters)
        fold_rows: list[dict[str, Any]] = []
        all_truth: list[np.ndarray] = []
        all_prediction: list[np.ndarray] = []
        all_b1: list[np.ndarray] = []
        problem_truth: list[np.ndarray] = []
        problem_prediction: list[np.ndarray] = []
        problem_b1: list[np.ndarray] = []
        for inner in inner_contexts:
            model = TwoRegimeIMMRate(
                model_id=str(config["imm_model"]["model_id"]),
                parameters=parameters,
            ).fit(inner["train"])
            prediction, _, _ = model.predict_distribution(
                inner["validation"], history_frame=prepared_history
            )
            truth = inner["truth"]
            b1_prediction = inner["b1_prediction"]
            problem_mask = inner["problem_mask"]
            overall = regression_metrics(truth, prediction)
            b1_overall = regression_metrics(truth, b1_prediction)
            if problem_mask.any():
                problem = regression_metrics(truth[problem_mask], prediction[problem_mask])
                b1_problem = regression_metrics(
                    truth[problem_mask], b1_prediction[problem_mask]
                )
                problem_mae = float(problem["mae"])
                b1_problem_mae = float(b1_problem["mae"])
            else:
                problem_mae = np.nan
                b1_problem_mae = np.nan
            train_dates = pd.to_datetime(inner["train"].frame["target_date"])
            validation_dates = pd.to_datetime(
                inner["validation"].frame["target_date"]
            )
            thresholds = inner["thresholds"]
            fold_rows.append(
                {
                    "tuning_context": context,
                    "candidate_key": candidate_key,
                    "inner_fold_id": inner["fold_id"],
                    "train_rows": len(inner["train"].frame),
                    "validation_rows": len(inner["validation"].frame),
                    "problem_transition_rows": int(problem_mask.sum()),
                    "train_target_date_max": train_dates.max().date().isoformat(),
                    "validation_target_date_min": validation_dates.min().date().isoformat(),
                    "validation_target_date_max": validation_dates.max().date().isoformat(),
                    "train_sample_ids_sha256": inner["train"].provenance.sample_ids_sha256,
                    "validation_sample_ids_sha256": inner[
                        "validation"
                    ].provenance.sample_ids_sha256,
                    "inner_acceleration_threshold": thresholds.acceleration_absolute,
                    "inner_volatility_threshold": thresholds.volatility,
                    "overall_mae": float(overall["mae"]),
                    "b1_overall_mae": float(b1_overall["mae"]),
                    "problem_transition_mae": problem_mae,
                    "b1_problem_transition_mae": b1_problem_mae,
                    **_grid_values(parameters),
                }
            )
            all_truth.append(truth)
            all_prediction.append(prediction)
            all_b1.append(b1_prediction)
            if problem_mask.any():
                problem_truth.append(truth[problem_mask])
                problem_prediction.append(prediction[problem_mask])
                problem_b1.append(b1_prediction[problem_mask])

        pooled_truth = np.concatenate(all_truth)
        pooled_prediction = np.concatenate(all_prediction)
        pooled_b1 = np.concatenate(all_b1)
        pooled_problem_truth = np.concatenate(problem_truth)
        pooled_problem_prediction = np.concatenate(problem_prediction)
        pooled_problem_b1 = np.concatenate(problem_b1)
        minimum_problem = int(objective["minimum_problem_transition_rows"])
        if len(pooled_problem_truth) < minimum_problem:
            raise ContractViolation(
                f"Tuning context {context} has only {len(pooled_problem_truth)} "
                f"problem-transition rows; minimum is {minimum_problem}"
            )
        overall_mae = float(regression_metrics(pooled_truth, pooled_prediction)["mae"])
        b1_overall_mae = float(regression_metrics(pooled_truth, pooled_b1)["mae"])
        problem_mae = float(
            regression_metrics(pooled_problem_truth, pooled_problem_prediction)["mae"]
        )
        b1_problem_mae = float(
            regression_metrics(pooled_problem_truth, pooled_problem_b1)["mae"]
        )
        overall_ratio = overall_mae / b1_overall_mae
        problem_ratio = problem_mae / b1_problem_mae
        score = (
            float(objective["overall_normalized_mae_weight"]) * overall_ratio
            + float(objective["problem_transition_normalized_mae_weight"])
            * problem_ratio
        )
        candidate_summaries.append(
            {
                "candidate_key": candidate_key,
                "candidate_overall_mae": overall_mae,
                "candidate_b1_overall_mae": b1_overall_mae,
                "candidate_overall_normalized_mae": overall_ratio,
                "candidate_problem_transition_rows": len(pooled_problem_truth),
                "candidate_problem_transition_mae": problem_mae,
                "candidate_b1_problem_transition_mae": b1_problem_mae,
                "candidate_problem_transition_normalized_mae": problem_ratio,
                "candidate_tuning_score": score,
                **_grid_values(parameters),
            }
        )
        candidate_fold_rows.append(fold_rows)

    ranked = sorted(
        candidate_summaries,
        key=lambda row: (
            row["candidate_tuning_score"],
            row["candidate_problem_transition_normalized_mae"],
            row["candidate_overall_normalized_mae"],
            row["q_transition"],
            row["p_transition_stay"],
            row["q_stable"],
            row["p_stable_stay"],
        ),
    )
    selected_summary = ranked[0]
    selected_key = str(selected_summary["candidate_key"])
    summary_lookup = {str(row["candidate_key"]): row for row in candidate_summaries}
    output_rows: list[dict[str, Any]] = []
    for fold_rows in candidate_fold_rows:
        for row in fold_rows:
            summary = summary_lookup[str(row["candidate_key"])]
            output_rows.append(
                {
                    **row,
                    **{
                        key: value
                        for key, value in summary.items()
                        if key.startswith("candidate_")
                    },
                    "selected": row["candidate_key"] == selected_key,
                }
            )
    selected = imm_parameters(
        config,
        q_stable=float(selected_summary["q_stable"]),
        q_transition=float(selected_summary["q_transition"]),
        p_stable_stay=float(selected_summary["p_stable_stay"]),
        p_transition_stay=float(selected_summary["p_transition_stay"]),
    )
    return selected, pd.DataFrame(output_rows)


def _grid_values(parameters: Mapping[str, Any]) -> dict[str, float]:
    return {
        "q_stable": float(parameters["q_stable"]),
        "q_transition": float(parameters["q_transition"]),
        "p_stable_stay": float(parameters["p_stable_stay"]),
        "p_transition_stay": float(parameters["p_transition_stay"]),
    }


def _candidate_key(parameters: Mapping[str, Any]) -> str:
    values = _grid_values(parameters)
    return (
        f"qs={values['q_stable']:g}|qt={values['q_transition']:g}|"
        f"ps={values['p_stable_stay']:g}|pt={values['p_transition_stay']:g}"
    )


def load_frozen_comparator_predictions(
    root: Path,
    config: Mapping[str, Any],
) -> pd.DataFrame:
    relative = "artifacts/model_selection/t1_b2_v1/outer_fold_predictions.csv"
    expected_hash = config["comparator_policy"]["protected_files"][relative]
    path = resolve_repo_path(root, relative)
    if sha256_file(path) != str(expected_hash).lower():
        raise ContractViolation("Frozen Gate B2 comparator predictions changed")
    frame = pd.read_csv(path)
    required = {
        "design",
        "fold_id",
        "model_id",
        "family",
        "sample_id",
        "point_id",
        "profile_id",
        "y_true",
        "y_pred",
        "transition_segment",
        "is_transition",
    }
    missing = required - set(frame)
    if missing:
        raise ContractViolation(
            f"Frozen comparator predictions are missing columns: {sorted(missing)}"
        )
    expected_models = set(map(str, config["comparator_policy"]["expected_models"]))
    observed_models = set(frame["model_id"].astype(str))
    if observed_models != expected_models:
        raise ContractViolation(
            f"Frozen comparator model set changed: {observed_models} != {expected_models}"
        )
    keys = ["design", "fold_id", "model_id", "sample_id"]
    if frame.duplicated(keys).any():
        raise ContractViolation("Frozen comparator predictions contain duplicate keys")
    base_keys: set[tuple[str, str, str]] | None = None
    for model_id, subset in frame.groupby("model_id", sort=True):
        current = set(
            map(
                tuple,
                subset[["design", "fold_id", "sample_id"]]
                .astype(str)
                .itertuples(index=False, name=None),
            )
        )
        if base_keys is None:
            base_keys = current
        elif current != base_keys:
            raise ContractViolation(
                f"Frozen comparator rows do not align for model {model_id}"
            )
    return frame


def validate_rebuilt_fold_contracts(
    rebuilt: pd.DataFrame,
    frozen: pd.DataFrame,
) -> None:
    if list(rebuilt.columns) != list(frozen.columns):
        raise ContractViolation("Rebuilt and frozen fold-contract schemas differ")
    left = rebuilt.fillna("").astype(str).reset_index(drop=True)
    right = frozen.fillna("").astype(str).reset_index(drop=True)
    if not left.equals(right):
        difference = int((left.ne(right)).any(axis=1).sum())
        raise ContractViolation(
            f"Rebuilt fold contracts differ from frozen Gate B2 contracts ({difference} rows)"
        )


def evaluate_imm_outer_folds(
    development: ManifestDataset,
    folds: Sequence[EvaluationFold],
    *,
    config: Mapping[str, Any],
    prepared_history: PreparedKalmanHistory,
    comparators: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    prediction_frames: list[pd.DataFrame] = []
    tuning_frames: list[pd.DataFrame] = []
    model_id = str(config["imm_model"]["model_id"])
    for fold in folds:
        train_fold = derived_dataset(
            development,
            fold.train_sample_ids,
            split="train",
            label=f"gate_b3_{fold.fold_id}_train",
        )
        validation_fold = derived_dataset(
            development,
            fold.validation_sample_ids,
            split="validation",
            label=f"gate_b3_{fold.fold_id}_validation",
        )
        selected, tuning = tune_imm_parameters(
            train_fold,
            prepared_history=prepared_history,
            config=config,
            context=f"outer::{fold.fold_id}",
        )
        tuning_frames.append(tuning)
        model = TwoRegimeIMMRate(model_id=model_id, parameters=selected).fit(train_fold)
        prediction, raw_sigma, diagnostics = model.predict_distribution(
            validation_fold,
            history_frame=prepared_history,
        )
        truth = pd.to_numeric(
            validation_fold.frame[TARGET_COLUMN], errors="raise"
        ).to_numpy(float)
        if (
            prediction.shape != truth.shape
            or not np.isfinite(prediction).all()
            or not np.isfinite(raw_sigma).all()
            or (raw_sigma <= 0).any()
        ):
            raise RuntimeError(f"{model_id}/{fold.fold_id} produced invalid predictions")

        comparator_fold = comparators.loc[
            comparators["fold_id"].astype(str).eq(fold.fold_id)
            & comparators["model_id"].eq("B1_persistence_last_rate")
        ].copy()
        comparator_fold = comparator_fold.set_index("sample_id", drop=False)
        expected_ids = tuple(validation_fold.frame["sample_id"].astype(str))
        if set(comparator_fold.index.astype(str)) != set(expected_ids):
            raise ContractViolation(
                f"IMM and frozen comparator sample IDs differ for {fold.fold_id}"
            )
        comparator_fold = comparator_fold.loc[list(expected_ids)].reset_index(drop=True)
        comparator_truth = pd.to_numeric(
            comparator_fold["y_true"], errors="raise"
        ).to_numpy(float)
        if not np.allclose(truth, comparator_truth, rtol=0.0, atol=1e-12):
            raise ContractViolation(
                f"IMM and frozen comparator truth differ for {fold.fold_id}"
            )

        metadata = [
            "sample_id",
            "point_id",
            "profile_id",
            "current_date",
            "target_date",
            "forecast_horizon_days",
        ]
        frame = validation_fold.frame.loc[:, metadata].copy()
        frame.insert(0, "design", fold.design)
        frame.insert(1, "fold_id", fold.fold_id)
        frame.insert(2, "held_out_group", fold.held_out_group)
        frame.insert(3, "model_id", model_id)
        frame.insert(4, "family", model.family)
        frame["y_true"] = truth
        frame["y_pred"] = prediction
        frame["error"] = prediction - truth
        frame["absolute_error"] = np.abs(prediction - truth)
        for key, value in _grid_values(selected).items():
            frame[f"selected_{key}"] = value
        frame["raw_sigma"] = raw_sigma
        for column in diagnostics:
            frame[column] = diagnostics[column].to_numpy()
        transition_columns = [
            "transition_segment",
            "is_transition",
            "transition_acceleration_flag",
            "transition_deceleration_flag",
            "transition_volatility_or_gap_flag",
        ]
        for column in transition_columns:
            frame[column] = comparator_fold[column].to_numpy()
        prediction_frames.append(frame)
    return (
        pd.concat(prediction_frames, ignore_index=True),
        pd.concat(tuning_frames, ignore_index=True),
    )


def nested_imm_calibration_predictions(
    train: ManifestDataset,
    *,
    config: Mapping[str, Any],
    prepared_history: PreparedKalmanHistory,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Create five nested train-OOF residual sets for B7 interval calibration."""

    policy = config["development_policy"]
    assignments = rolling_origin_assignments(
        [train],
        minimum_train_dates=int(policy["inner_minimum_train_dates"]),
        maximum_folds=int(policy["calibration_rolling_origin_folds"]),
    )
    prediction_frames: list[pd.DataFrame] = []
    tuning_frames: list[pd.DataFrame] = []
    transition_policy = config["transition_validation"]
    for fold_id, assignment in assignments.groupby("fold_id", sort=True):
        train_ids = tuple(
            assignment.loc[assignment["role"].eq("train"), "sample_id"].astype(str)
        )
        validation_ids = tuple(
            assignment.loc[
                assignment["role"].eq("validation"), "sample_id"
            ].astype(str)
        )
        calibration_train = derived_dataset(
            train,
            train_ids,
            split="train",
            label=f"gate_b3_calibration_{fold_id}_train",
        )
        calibration_validation = derived_dataset(
            train,
            validation_ids,
            split="validation",
            label=f"gate_b3_calibration_{fold_id}_validation",
        )
        selected, tuning = tune_imm_parameters(
            calibration_train,
            prepared_history=prepared_history,
            config=config,
            context=f"calibration::{fold_id}",
        )
        tuning_frames.append(tuning)
        model = TwoRegimeIMMRate(
            model_id=str(config["imm_model"]["model_id"]),
            parameters=selected,
        ).fit(calibration_train)
        mean, raw_sigma, diagnostics = model.predict_distribution(
            calibration_validation,
            history_frame=prepared_history,
        )
        thresholds = fit_transition_thresholds(
            calibration_train.frame,
            acceleration_quantile=float(
                transition_policy["inner_acceleration_absolute_quantile"]
            ),
            volatility_quantile=float(
                transition_policy["inner_volatility_quantile"]
            ),
            missing_campaigns_threshold=int(
                transition_policy["inner_missing_campaigns_threshold"]
            ),
        )
        segments = classify_transition_proxy(calibration_validation.frame, thresholds)
        frame = calibration_validation.frame.loc[
            :,
            [
                "sample_id",
                "point_id",
                "profile_id",
                "current_date",
                "target_date",
                "forecast_horizon_days",
            ],
        ].copy()
        frame.insert(0, "calibration_fold_id", str(fold_id))
        frame["train_rows"] = len(calibration_train.frame)
        frame["train_target_date_max"] = (
            pd.to_datetime(calibration_train.frame["target_date"])
            .max()
            .date()
            .isoformat()
        )
        frame["validation_target_date_min"] = (
            pd.to_datetime(calibration_validation.frame["target_date"])
            .min()
            .date()
            .isoformat()
        )
        frame["train_sample_ids_sha256"] = (
            calibration_train.provenance.sample_ids_sha256
        )
        for key, value in _grid_values(selected).items():
            frame[f"selected_{key}"] = value
        frame["y_true"] = pd.to_numeric(
            calibration_validation.frame[TARGET_COLUMN], errors="raise"
        ).to_numpy(float)
        frame["y_pred"] = mean
        frame["raw_sigma"] = raw_sigma
        for column in diagnostics:
            frame[column] = diagnostics[column].to_numpy()
        for column in segments:
            frame[column] = segments[column].to_numpy()
        prediction_frames.append(frame)
    return (
        pd.concat(prediction_frames, ignore_index=True),
        pd.concat(tuning_frames, ignore_index=True),
    )


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
        rows.append(
            {
                "design": design,
                "model_id": model_id,
                "family": family,
                "folds": int(frame["fold_id"].nunique()),
                **regression_metrics(frame["y_true"], frame["y_pred"]),
            }
        )
    return pd.DataFrame(rows).sort_values(
        ["design", "mae", "model_id"], kind="mergesort"
    ).reset_index(drop=True)


def compare_models(aggregate: pd.DataFrame) -> pd.DataFrame:
    lookup = aggregate.set_index(["design", "model_id"])["mae"]
    rows: list[dict[str, Any]] = []
    for row in aggregate.itertuples(index=False):
        references = {
            "b1": float(lookup.loc[(row.design, "B1_persistence_last_rate")]),
            "b5": float(lookup.loc[(row.design, "B5_fixed_kalman")]),
            "b6": float(lookup.loc[(row.design, "B6_adaptive_kalman")]),
        }
        output: dict[str, Any] = {
            "design": row.design,
            "model_id": row.model_id,
            "mae": float(row.mae),
        }
        for label, reference in references.items():
            output[f"reference_{label}_mae"] = reference
            output[f"improvement_vs_{label}_percent"] = (
                100.0 * (reference - float(row.mae)) / reference
            )
        rows.append(output)
    return pd.DataFrame(rows).sort_values(
        ["design", "mae", "model_id"], kind="mergesort"
    ).reset_index(drop=True)


def problem_transition_metrics(
    predictions: pd.DataFrame,
    *,
    problem_segments: Sequence[str],
) -> pd.DataFrame:
    problem = predictions.loc[
        predictions["transition_segment"].astype(str).isin(set(map(str, problem_segments)))
    ]
    rows: list[dict[str, Any]] = []
    for (design, model_id), frame in problem.groupby(
        ["design", "model_id"], sort=True
    ):
        rows.append(
            {
                "design": design,
                "model_id": model_id,
                "scope": "accelerating_plus_volatile_or_gap",
                "rows": len(frame),
                "points": frame["point_id"].astype(str).nunique(),
                "profiles": frame["profile_id"].astype(str).nunique(),
                **regression_metrics(frame["y_true"], frame["y_pred"]),
            }
        )
    result = pd.DataFrame(rows)
    lookup = result.set_index(["design", "model_id"])["mae"]
    enriched: list[dict[str, Any]] = []
    for row in result.to_dict(orient="records"):
        b1 = float(lookup.loc[(row["design"], "B1_persistence_last_rate")])
        b6 = float(lookup.loc[(row["design"], "B6_adaptive_kalman")])
        row["reference_b1_mae"] = b1
        row["improvement_vs_b1_percent"] = 100.0 * (b1 - row["mae"]) / b1
        row["reference_b6_mae"] = b6
        row["improvement_vs_b6_percent"] = 100.0 * (b6 - row["mae"]) / b6
        enriched.append(row)
    return pd.DataFrame(enriched).sort_values(
        ["design", "mae", "model_id"], kind="mergesort"
    ).reset_index(drop=True)


def regime_summary(imm_predictions: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for (design, segment), frame in imm_predictions.groupby(
        ["design", "transition_segment"], sort=True
    ):
        probability = pd.to_numeric(
            frame["transition_probability"], errors="raise"
        ).to_numpy(float)
        rows.append(
            {
                "design": design,
                "transition_segment": segment,
                "rows": len(frame),
                "points": frame["point_id"].astype(str).nunique(),
                "profiles": frame["profile_id"].astype(str).nunique(),
                "transition_probability_mean": float(np.mean(probability)),
                "transition_probability_median": float(np.median(probability)),
                "transition_probability_q90": float(np.quantile(probability, 0.90)),
                "transition_probability_ge_0_5_rate": float(np.mean(probability >= 0.5)),
                "regime_entropy_mean": float(
                    pd.to_numeric(frame["regime_entropy"], errors="raise").mean()
                ),
                "mae": float(
                    regression_metrics(frame["y_true"], frame["y_pred"])["mae"]
                ),
            }
        )
    return pd.DataFrame(rows).sort_values(
        ["design", "transition_segment"], kind="mergesort"
    ).reset_index(drop=True)


def screening_assessment(
    aggregate: pd.DataFrame,
    transition: pd.DataFrame,
    problem: pd.DataFrame,
    intervals: pd.DataFrame,
    *,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    acceptance = config["acceptance"]
    model_id = str(config["imm_model"]["model_id"])
    aggregate_lookup = aggregate.set_index(["design", "model_id"])["mae"]
    temporal = float(aggregate_lookup.loc[("temporal_holdout", model_id)])
    temporal_b6 = float(
        aggregate_lookup.loc[("temporal_holdout", "B6_adaptive_kalman")]
    )
    profile = float(aggregate_lookup.loc[("leave_profile_out", model_id)])
    zone = float(aggregate_lookup.loc[("leave_zone_out", model_id)])
    zone_b1 = float(
        aggregate_lookup.loc[("leave_zone_out", "B1_persistence_last_rate")]
    )

    problem_row = problem.loc[
        problem["design"].eq("temporal_holdout")
        & problem["model_id"].eq(model_id)
    ].iloc[0]
    temporal_mechanisms = transition.loc[
        transition["design"].eq("temporal_holdout")
        & transition["scope"].eq("mechanism")
    ].set_index(["model_id", "segment"])["mae"]
    accelerating = float(temporal_mechanisms.loc[(model_id, "accelerating")])
    accelerating_b1 = float(
        temporal_mechanisms.loc[("B1_persistence_last_rate", "accelerating")]
    )
    volatile = float(temporal_mechanisms.loc[(model_id, "volatile_or_gap")])
    volatile_b1 = float(
        temporal_mechanisms.loc[("B1_persistence_last_rate", "volatile_or_gap")]
    )
    interval_95 = intervals.loc[
        np.isclose(intervals["coverage_nominal"], 0.95)
    ].iloc[0]
    observed = {
        "temporal_mae": temporal,
        "temporal_b6_mae": temporal_b6,
        "temporal_mae_ratio_vs_b6": temporal / temporal_b6,
        "problem_transition_rows": int(problem_row["rows"]),
        "problem_transition_mae": float(problem_row["mae"]),
        "problem_transition_b1_mae": float(problem_row["reference_b1_mae"]),
        "problem_transition_b6_mae": float(problem_row["reference_b6_mae"]),
        "problem_transition_improvement_vs_b1_percent": float(
            problem_row["improvement_vs_b1_percent"]
        ),
        "problem_transition_improvement_vs_b6_percent": float(
            problem_row["improvement_vs_b6_percent"]
        ),
        "accelerating_mae": accelerating,
        "accelerating_b1_mae": accelerating_b1,
        "accelerating_mae_ratio_vs_b1": accelerating / accelerating_b1,
        "volatile_or_gap_mae": volatile,
        "volatile_or_gap_b1_mae": volatile_b1,
        "volatile_or_gap_mae_ratio_vs_b1": volatile / volatile_b1,
        "leave_profile_mae": profile,
        "leave_profile_mae_degradation_vs_temporal_percent": 100.0
        * (profile - temporal)
        / temporal,
        "leave_zone_mae": zone,
        "leave_zone_b1_mae": zone_b1,
        "leave_zone_mae_degradation_vs_temporal_percent": 100.0
        * (zone - temporal)
        / temporal,
        "leave_zone_mae_ratio_vs_b1": zone / zone_b1,
        "coverage_95_empirical": float(interval_95["coverage_empirical"]),
        "coverage_95_mean_width_mm_y": float(interval_95["mean_width_mm_y"]),
    }
    checks = {
        "temporal_vs_b6": observed["temporal_mae_ratio_vs_b6"]
        <= float(acceptance["temporal_mae_ratio_vs_b6_max"]),
        "problem_transition_vs_b1": observed[
            "problem_transition_improvement_vs_b1_percent"
        ]
        >= float(
            acceptance["problem_transition_improvement_vs_b1_percent_min"]
        ),
        "problem_transition_vs_b6": observed[
            "problem_transition_improvement_vs_b6_percent"
        ]
        >= float(
            acceptance["problem_transition_improvement_vs_b6_percent_min"]
        ),
        "accelerating_vs_b1": observed["accelerating_mae_ratio_vs_b1"]
        <= float(acceptance["accelerating_mae_ratio_vs_b1_max"]),
        "volatile_or_gap_vs_b1": observed["volatile_or_gap_mae_ratio_vs_b1"]
        <= float(acceptance["volatile_or_gap_mae_ratio_vs_b1_max"]),
        "leave_profile_degradation": observed[
            "leave_profile_mae_degradation_vs_temporal_percent"
        ]
        <= float(
            acceptance["leave_profile_mae_degradation_vs_temporal_percent_max"]
        ),
        "leave_zone_degradation": observed[
            "leave_zone_mae_degradation_vs_temporal_percent"
        ]
        <= float(
            acceptance["leave_zone_mae_degradation_vs_temporal_percent_max"]
        ),
        "leave_zone_vs_b1": observed["leave_zone_mae_ratio_vs_b1"]
        <= float(acceptance["leave_zone_mae_ratio_vs_b1_max"]),
        "coverage_95": float(acceptance["coverage_95_min"])
        <= observed["coverage_95_empirical"]
        <= float(acceptance["coverage_95_max"]),
    }
    return {
        "observed": observed,
        "criteria": dict(acceptance),
        "checks": {key: bool(value) for key, value in checks.items()},
        "all_pass": all(checks.values()),
        "scope": "development_screening_only_no_final_holdout",
    }


def run_gate_b3_development(root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    """Run the predeclared Gate B3 once without a current-test code path."""

    before_snapshot = capture_protected_predecessor_snapshot(root, config)
    bundle = load_canonical_bundle(root)
    train = load_split_dataset("t1", "train", root=root)
    validation = load_split_dataset("t1", "validation", root=root)
    if train.provenance.version != config["split_version"]:
        raise ContractViolation("Gate B3 split version differs from frozen T1 manifests")
    development, folds, rebuilt_fold_contracts = build_gate_b0_b1_folds(
        train,
        validation,
        bundle,
        rolling_folds=int(config["development_policy"]["outer_designs"]["rolling_origin"]),
    )
    frozen_fold_path = resolve_repo_path(
        root, "artifacts/model_selection/t1_b2_v1/fold_contracts.csv"
    )
    frozen_fold_contracts = pd.read_csv(frozen_fold_path)
    validate_rebuilt_fold_contracts(rebuilt_fold_contracts, frozen_fold_contracts)
    comparators = load_frozen_comparator_predictions(root, config)
    history_frame = causal_feature_history(development)
    prepared_history = prepare_kalman_history(history_frame)

    imm_predictions, outer_tuning = evaluate_imm_outer_folds(
        development,
        folds,
        config=config,
        prepared_history=prepared_history,
        comparators=comparators,
    )
    comparison_predictions = pd.concat(
        [comparators.copy(), imm_predictions.copy()],
        ignore_index=True,
        sort=False,
    )
    fold_summary = fold_metrics(comparison_predictions)
    aggregate = aggregate_metrics(comparison_predictions)
    comparison = compare_models(aggregate)
    transition = transition_metrics(comparison_predictions)
    problem = problem_transition_metrics(
        comparison_predictions,
        problem_segments=config["transition_validation"][
            "problem_transition_segments"
        ],
    )
    regimes = regime_summary(imm_predictions)

    calibration_raw, calibration_tuning = nested_imm_calibration_predictions(
        train,
        config=config,
        prepared_history=prepared_history,
    )
    interval_config = config["interval_calibration"]
    calibration_predictions, calibration_summary = calibrate_scaled_conformal(
        calibration_raw,
        coverage_levels=interval_config["coverage_levels"],
        sigma_floor=float(interval_config["sigma_floor_mm_y"]),
    )
    temporal_imm = imm_predictions.loc[
        imm_predictions["design"].eq("temporal_holdout")
    ].copy()
    validation_intervals = apply_scaled_conformal_intervals(
        temporal_imm,
        calibration_summary,
        sigma_floor=float(interval_config["sigma_floor_mm_y"]),
    )
    interval_summary = interval_metrics(validation_intervals, calibration_summary)
    tuning = pd.concat([outer_tuning, calibration_tuning], ignore_index=True)
    tuning = tuning.sort_values(
        [
            "tuning_context",
            "candidate_tuning_score",
            "candidate_problem_transition_normalized_mae",
            "candidate_overall_normalized_mae",
            "q_transition",
            "p_transition_stay",
            "q_stable",
            "p_stable_stay",
            "inner_fold_id",
        ],
        kind="mergesort",
    ).reset_index(drop=True)

    paths = {
        name: resolve_repo_path(root, value)
        for name, value in config["artifacts"].items()
        if name != "root"
    }
    write_csv_atomic(root, paths["fold_contracts"], frozen_fold_contracts)
    write_csv_atomic(root, paths["imm_tuning"], tuning)
    write_csv_atomic(root, paths["imm_outer_predictions"], imm_predictions)
    write_csv_atomic(root, paths["comparison_predictions"], comparison_predictions)
    write_csv_atomic(root, paths["fold_metrics"], fold_summary)
    write_csv_atomic(root, paths["aggregate_metrics"], aggregate)
    write_csv_atomic(root, paths["model_comparison"], comparison)
    write_csv_atomic(root, paths["calibration_predictions"], calibration_predictions)
    write_csv_atomic(root, paths["interval_calibration"], calibration_summary)
    write_csv_atomic(root, paths["validation_intervals"], validation_intervals)
    write_csv_atomic(root, paths["interval_metrics"], interval_summary)
    write_csv_atomic(root, paths["transition_metrics"], transition)
    write_csv_atomic(root, paths["problem_transition_metrics"], problem)
    write_csv_atomic(root, paths["regime_summary"], regimes)

    screening = screening_assessment(
        aggregate,
        transition,
        problem,
        interval_summary,
        config=config,
    )
    temporal_tuning = tuning.loc[
        tuning["tuning_context"].eq("outer::temporal_validation_2024")
        & tuning["selected"].astype(bool)
    ]
    if temporal_tuning.empty:
        raise RuntimeError("Temporal IMM tuning did not select a candidate")
    first_selected = temporal_tuning.iloc[0]
    selected_parameters = imm_parameters(
        config,
        q_stable=float(first_selected["q_stable"]),
        q_transition=float(first_selected["q_transition"]),
        p_stable_stay=float(first_selected["p_stable_stay"]),
        p_transition_stay=float(first_selected["p_transition_stay"]),
    )
    main_model = TwoRegimeIMMRate(
        model_id=str(config["imm_model"]["model_id"]),
        parameters=selected_parameters,
    ).fit(train)
    comparator_hashes = {
        relative: str(expected).lower()
        for relative, expected in config["comparator_policy"][
            "protected_files"
        ].items()
    }
    candidate_base = {
        "schema_version": 1,
        "candidate_scope": "gate_b3_train_validation_only",
        "status": (
            "validation_frozen_pending_new_holdout"
            if screening["all_pass"]
            else "validation_recorded"
        ),
        "task": "t1",
        "split_version": config["split_version"],
        "selected_model": config["imm_model"]["model_id"],
        "selected_parameters": selected_parameters,
        "model_state": main_model.state_dict(),
        "feature_contract_sha256": bundle.feature_contract.source_sha256,
        "target_contract_sha256": bundle.target_contract.source_sha256,
        "manifest_hashes": {
            "train": train.provenance.sample_ids_sha256,
            "validation": validation.provenance.sample_ids_sha256,
        },
        "comparator_hashes": comparator_hashes,
        "selection_data": ["t1_v1/train", "t1_v1/validation"],
        "current_t1_test_used": False,
        "current_t1_test_authorized": False,
        "screening_passed": bool(screening["all_pass"]),
        "eligible_for_final_claim": False,
        "final_evaluation_status": "PENDING_NEW_HOLDOUT_OR_GOVERNANCE_DECISION",
    }
    candidate_digest = sha256(
        json.dumps(candidate_base, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    candidate_id = f"t1-b3-v1-{candidate_digest[:12]}"
    fold_counts = Counter(fold.design for fold in folds)
    report = {
        "schema_version": 1,
        "gate": config["gate"],
        "status": (
            "VALIDATION_FROZEN_PENDING_NEW_HOLDOUT"
            if screening["all_pass"]
            else "VALIDATION_RECORDED_SCREENING_CRITERIA_NOT_MET"
        ),
        "candidate_id": candidate_id,
        "test_data_loaded": False,
        "test_phase_available": False,
        "hypothesis": (
            "A predeclared two-regime IMM reduces accelerating/volatile transition "
            "error and leave-zone instability without materially degrading B6 temporal MAE."
        ),
        "data": {
            "task": "T1_RATE_NEXT_PLANNED",
            "target": TARGET_COLUMN,
            "unit": "mm/year",
            "train_rows": len(train.frame),
            "validation_rows": len(validation.frame),
            "train_target_date_max": pd.to_datetime(train.frame["target_date"])
            .max()
            .date()
            .isoformat(),
            "validation_target_date_max": pd.to_datetime(
                validation.frame["target_date"]
            )
            .max()
            .date()
            .isoformat(),
            "train_sample_ids_sha256": train.provenance.sample_ids_sha256,
            "validation_sample_ids_sha256": validation.provenance.sample_ids_sha256,
        },
        "model": {
            "model_id": config["imm_model"]["model_id"],
            "family": config["imm_model"]["family"],
            "selected_parameters": selected_parameters,
            "state": ["settlement_mm", "velocity_mm_y", "acceleration_mm_y2"],
            "regimes": list(config["imm_model"]["regimes"]),
            "candidate_grid_size": len(imm_parameter_grid(config)),
        },
        "validation_design": {
            "fold_counts": dict(sorted(fold_counts.items())),
            "outer_forward_only": True,
            "inner_tuning_forward_only": True,
            "tuning_objective": config["development_policy"]["tuning_objective"],
            "calibration": "five nested rolling train-only OOF folds",
        },
        "comparators": {
            "models": list(config["comparator_policy"]["expected_models"]),
            "mode": config["comparator_policy"]["mode"],
            "source": "artifacts/model_selection/t1_b2_v1/outer_fold_predictions.csv",
            "source_sha256": comparator_hashes[
                "artifacts/model_selection/t1_b2_v1/outer_fold_predictions.csv"
            ],
        },
        "screening": screening,
        "intervals": interval_summary.to_dict(orient="records"),
        "regime_diagnostics": regimes.to_dict(orient="records"),
        "governance": {
            "protocol": "docs/governance/GATE_B3_PROTOCOL.md",
            "protocol_frozen_before_outer_run": True,
            "current_t1_test_role": config["test_policy"]["current_t1_test_role"],
            "current_t1_test_used": False,
            "new_holdout_policy": config["test_policy"]["replacement_holdout_policy"],
            "final_claim_allowed_now": False,
        },
        "environment": _package_versions(),
        "caveats": [
            "Gate B3 is train/validation evidence and not an unseen final estimate.",
            "The two regimes are statistical filters, not identified physical process states.",
            "Transition categories remain origin-only proxies rather than adjudicated events.",
            "Rows repeat points and profiles; row counts are not independent trajectories.",
            "The same validation period motivated the narrow hypothesis, so successful screening still requires a new holdout.",
        ],
    }
    write_json_atomic(root, paths["gate_report"], report)

    source_paths = [
        root / "configs" / "gate_b3.yaml",
        root / "configs" / "final_holdout_v2.yaml",
        root / "docs" / "governance" / "GATE_B3_PROTOCOL.md",
        root / "src" / "skru1" / "adaptive_kalman.py",
        root / "src" / "skru1" / "imm_kalman.py",
        root / "src" / "skru1" / "transition_validation.py",
        root / "src" / "skru1" / "uncertainty.py",
        root / "src" / "skru1" / "gate_b3.py",
        root / "scripts" / "run_gate_b3.py",
    ]
    candidate = {
        **candidate_base,
        "candidate_id": candidate_id,
        "gate_report": paths["gate_report"].relative_to(root).as_posix(),
        "gate_report_sha256": sha256_file(paths["gate_report"]),
        "source_hashes": {
            path.relative_to(root).as_posix(): sha256_file(path) for path in source_paths
        },
        "test_access_policy": "no_test_phase_current_holdout_ineligible",
    }
    write_json_atomic(root, paths["development_candidate"], candidate)

    after_snapshot = capture_protected_predecessor_snapshot(root, config)
    protected_snapshot = {
        **before_snapshot,
        "verified_after_generation": True,
        "after_generation_sha256": {
            row["relative_path"]: row["observed_sha256"]
            for row in after_snapshot["protected_files"]
        },
        "unchanged_during_gate_b3": before_snapshot == after_snapshot,
    }
    write_json_atomic(
        root,
        paths["protected_predecessor_snapshot"],
        protected_snapshot,
    )
    return {
        "phase": "develop",
        "status": report["status"],
        "candidate_id": candidate_id,
        "selected_parameters": _grid_values(selected_parameters),
        "screening_all_pass": bool(screening["all_pass"]),
        "test_data_loaded": False,
        "comparators_refit": False,
        "outer_folds": len(folds),
        "calibration_rows": len(calibration_predictions),
    }


def run_gate_b3_validation(
    root: Path,
    config: Mapping[str, Any],
    *,
    write_outputs: bool = True,
) -> dict[str, Any]:
    """Independently audit B7 metrics, boundaries, and frozen comparators."""

    paths = {
        name: resolve_repo_path(root, value)
        for name, value in config["artifacts"].items()
        if name != "root"
    }
    required_names = [
        "protected_predecessor_snapshot",
        "fold_contracts",
        "imm_tuning",
        "imm_outer_predictions",
        "comparison_predictions",
        "fold_metrics",
        "aggregate_metrics",
        "model_comparison",
        "calibration_predictions",
        "interval_calibration",
        "validation_intervals",
        "interval_metrics",
        "transition_metrics",
        "problem_transition_metrics",
        "regime_summary",
        "development_candidate",
        "gate_report",
    ]
    missing = [paths[name] for name in required_names if not paths[name].is_file()]
    if missing:
        raise FileNotFoundError(f"Gate B3 artifacts are missing: {missing}")

    protected = json.loads(
        paths["protected_predecessor_snapshot"].read_text(encoding="utf-8")
    )
    fold_contracts = pd.read_csv(paths["fold_contracts"])
    tuning = pd.read_csv(paths["imm_tuning"])
    imm_predictions = pd.read_csv(paths["imm_outer_predictions"])
    comparison_predictions = pd.read_csv(paths["comparison_predictions"])
    saved_fold_metrics = pd.read_csv(paths["fold_metrics"])
    saved_aggregate = pd.read_csv(paths["aggregate_metrics"])
    saved_comparison = pd.read_csv(paths["model_comparison"])
    calibration_predictions = pd.read_csv(paths["calibration_predictions"])
    calibration = pd.read_csv(paths["interval_calibration"])
    validation_intervals = pd.read_csv(paths["validation_intervals"])
    saved_interval_metrics = pd.read_csv(paths["interval_metrics"])
    saved_transition = pd.read_csv(paths["transition_metrics"])
    saved_problem = pd.read_csv(paths["problem_transition_metrics"])
    saved_regimes = pd.read_csv(paths["regime_summary"])
    candidate = json.loads(paths["development_candidate"].read_text(encoding="utf-8"))
    report = json.loads(paths["gate_report"].read_text(encoding="utf-8"))
    checks: list[dict[str, Any]] = []

    def add(check_id: str, passed: bool, observed: Any, expected: Any) -> None:
        checks.append(
            {
                "check_id": check_id,
                "passed": bool(passed),
                "observed": _plain_value(observed),
                "expected": _plain_value(expected),
            }
        )

    current_snapshot = capture_protected_predecessor_snapshot(root, config)
    add(
        "protected_predecessors_unchanged",
        bool(protected.get("unchanged_during_gate_b3"))
        and current_snapshot["protected_files"] == protected["protected_files"],
        protected.get("unchanged_during_gate_b3"),
        True,
    )
    source_paths = [
        root / "src" / "skru1" / "imm_kalman.py",
        root / "src" / "skru1" / "gate_b3.py",
        root / "scripts" / "run_gate_b3.py",
    ]
    loader_calls = find_test_loader_calls(source_paths)
    add("gate_b3_has_no_test_loader_call", not loader_calls, loader_calls, [])

    frozen_fold_contracts = pd.read_csv(
        root / "artifacts" / "model_selection" / "t1_b2_v1" / "fold_contracts.csv"
    )
    add(
        "fold_contracts_equal_frozen_b2",
        _frames_equivalent(fold_contracts, frozen_fold_contracts),
        len(fold_contracts),
        len(frozen_fold_contracts),
    )
    expected_fold_counts = {
        str(key): int(value)
        for key, value in config["development_policy"]["outer_designs"].items()
    }
    observed_fold_counts = (
        fold_contracts.groupby("design")["fold_id"].nunique().astype(int).to_dict()
    )
    add(
        "outer_fold_counts",
        observed_fold_counts == expected_fold_counts,
        observed_fold_counts,
        expected_fold_counts,
    )
    forward_outer = pd.to_datetime(fold_contracts["train_target_date_max"]).lt(
        pd.to_datetime(fold_contracts["validation_target_date_min"])
    )
    add("outer_folds_forward_only", bool(forward_outer.all()), int((~forward_outer).sum()), 0)

    forward_tuning = pd.to_datetime(tuning["train_target_date_max"]).lt(
        pd.to_datetime(tuning["validation_target_date_min"])
    )
    add(
        "nested_tuning_forward_only",
        bool(forward_tuning.all()),
        int((~forward_tuning).sum()),
        0,
    )
    grid_size = len(imm_parameter_grid(config))
    observed_grid_sizes = tuning.groupby("tuning_context")[
        "candidate_key"
    ].nunique()
    add(
        "predeclared_grid_size_every_context",
        bool(observed_grid_sizes.eq(grid_size).all()),
        observed_grid_sizes.to_dict(),
        grid_size,
    )
    selected_keys = (
        tuning.loc[tuning["selected"].astype(bool)]
        .groupby("tuning_context")["candidate_key"]
        .nunique()
    )
    add(
        "one_selected_candidate_per_context",
        len(selected_keys) == tuning["tuning_context"].nunique()
        and bool(selected_keys.eq(1).all()),
        selected_keys.to_dict(),
        "exactly one candidate key per context",
    )
    score_min = tuning.groupby("tuning_context")["candidate_tuning_score"].min()
    selected_score = (
        tuning.loc[tuning["selected"].astype(bool)]
        .groupby("tuning_context")["candidate_tuning_score"]
        .first()
    )
    add(
        "selected_candidate_minimizes_predeclared_score",
        bool(np.allclose(selected_score.sort_index(), score_min.sort_index())),
        selected_score.to_dict(),
        score_min.to_dict(),
    )

    frozen_comparators = load_frozen_comparator_predictions(root, config)
    comparator_models = set(map(str, config["comparator_policy"]["expected_models"]))
    persisted_comparators = comparison_predictions.loc[
        comparison_predictions["model_id"].astype(str).isin(comparator_models),
        frozen_comparators.columns,
    ]
    add(
        "comparators_reused_unchanged",
        _frames_equivalent(persisted_comparators, frozen_comparators),
        len(persisted_comparators),
        len(frozen_comparators),
    )
    b1_keys = set(
        map(
            tuple,
            frozen_comparators.loc[
                frozen_comparators["model_id"].eq("B1_persistence_last_rate"),
                ["design", "fold_id", "sample_id"],
            ]
            .astype(str)
            .itertuples(index=False, name=None),
        )
    )
    imm_keys = set(
        map(
            tuple,
            imm_predictions[["design", "fold_id", "sample_id"]]
            .astype(str)
            .itertuples(index=False, name=None),
        )
    )
    add("imm_keys_match_frozen_comparators", imm_keys == b1_keys, len(imm_keys), len(b1_keys))

    train_ids = set(
        read_manifest(root / "artifacts" / "splits" / "t1_v1" / "train.csv")[
            "sample_id"
        ].astype(str)
    )
    validation_ids = set(
        read_manifest(
            root / "artifacts" / "splits" / "t1_v1" / "validation.csv"
        )["sample_id"].astype(str)
    )
    calibration_ids = set(calibration_predictions["sample_id"].astype(str))
    outer_ids = set(imm_predictions["sample_id"].astype(str))
    add(
        "calibration_ids_train_only",
        calibration_ids <= train_ids and not (calibration_ids & validation_ids),
        {
            "rows": len(calibration_ids),
            "validation_overlap": len(calibration_ids & validation_ids),
        },
        {"subset": "train", "validation_overlap": 0},
    )
    add(
        "outer_ids_development_only",
        outer_ids <= (train_ids | validation_ids),
        len(outer_ids - (train_ids | validation_ids)),
        0,
    )
    calibration_order = pd.to_datetime(
        calibration_predictions["train_target_date_max"]
    ).lt(pd.to_datetime(calibration_predictions["validation_target_date_min"]))
    add(
        "calibration_oof_forward_only",
        bool(calibration_order.all()),
        int((~calibration_order).sum()),
        0,
    )

    independent_fold = fold_metrics(comparison_predictions)
    add(
        "fold_metrics_recomputed",
        _frames_equivalent(saved_fold_metrics, independent_fold),
        len(saved_fold_metrics),
        len(independent_fold),
    )
    independent_aggregate = aggregate_metrics(comparison_predictions)
    add(
        "aggregate_metrics_recomputed",
        _frames_equivalent(saved_aggregate, independent_aggregate),
        len(saved_aggregate),
        len(independent_aggregate),
    )
    independent_comparison = compare_models(independent_aggregate)
    add(
        "model_comparison_recomputed",
        _frames_equivalent(saved_comparison, independent_comparison),
        len(saved_comparison),
        len(independent_comparison),
    )
    independent_transition = transition_metrics(comparison_predictions)
    add(
        "transition_metrics_recomputed",
        _frames_equivalent(saved_transition, independent_transition),
        len(saved_transition),
        len(independent_transition),
    )
    independent_problem = problem_transition_metrics(
        comparison_predictions,
        problem_segments=config["transition_validation"][
            "problem_transition_segments"
        ],
    )
    add(
        "problem_transition_metrics_recomputed",
        _frames_equivalent(saved_problem, independent_problem),
        len(saved_problem),
        len(independent_problem),
    )
    independent_regimes = regime_summary(imm_predictions)
    add(
        "regime_summary_recomputed",
        _frames_equivalent(saved_regimes, independent_regimes),
        len(saved_regimes),
        len(independent_regimes),
    )

    probability_sum = pd.to_numeric(
        imm_predictions["stable_probability"], errors="coerce"
    ) + pd.to_numeric(imm_predictions["transition_probability"], errors="coerce")
    probabilities_valid = (
        pd.to_numeric(imm_predictions["stable_probability"], errors="coerce")
        .between(0.0, 1.0)
        .all()
        and pd.to_numeric(
            imm_predictions["transition_probability"], errors="coerce"
        )
        .between(0.0, 1.0)
        .all()
        and np.allclose(probability_sum, 1.0, atol=1e-12)
    )
    add(
        "imm_probabilities_valid",
        bool(probabilities_valid),
        float(np.max(np.abs(probability_sum - 1.0))),
        0.0,
    )
    numerical_fallbacks = int(
        imm_predictions["numerical_fallback_used"].astype(bool).sum()
    )
    add("no_numerical_fallbacks", numerical_fallbacks == 0, numerical_fallbacks, 0)

    score = calibration_predictions["nonconformity_score"].to_numpy(float)
    for row in calibration.itertuples(index=False):
        qhat, probability = finite_sample_conformal_quantile(
            score, coverage=float(row.coverage)
        )
        add(
            f"conformal_qhat::{row.coverage}",
            np.isclose(float(row.qhat), qhat, rtol=1e-12, atol=1e-12)
            and np.isclose(
                float(row.quantile_probability),
                probability,
                rtol=1e-12,
                atol=1e-12,
            ),
            {"qhat": float(row.qhat), "probability": float(row.quantile_probability)},
            {"qhat": qhat, "probability": probability},
        )
    independent_interval = interval_metrics(validation_intervals, calibration)
    add(
        "interval_metrics_recomputed",
        _frames_equivalent(saved_interval_metrics, independent_interval),
        len(saved_interval_metrics),
        len(independent_interval),
    )

    screening = screening_assessment(
        independent_aggregate,
        independent_transition,
        independent_problem,
        independent_interval,
        config=config,
    )
    add(
        "screening_checks_recomputed",
        screening["checks"] == report["screening"]["checks"]
        and screening["all_pass"] == report["screening"]["all_pass"],
        screening["checks"],
        report["screening"]["checks"],
    )
    for key, expected in screening["observed"].items():
        observed = report["screening"]["observed"][key]
        passed = (
            int(observed) == int(expected)
            if key.endswith("_rows")
            else np.isclose(float(observed), float(expected), rtol=1e-10, atol=1e-10)
        )
        add(f"screening_observed::{key}", passed, observed, expected)

    temporal_selected = tuning.loc[
        tuning["tuning_context"].eq("outer::temporal_validation_2024")
        & tuning["selected"].astype(bool)
    ].iloc[0]
    selected_grid = {
        key: float(candidate["selected_parameters"][key])
        for key in ("q_stable", "q_transition", "p_stable_stay", "p_transition_stay")
    }
    expected_grid = {
        key: float(temporal_selected[key])
        for key in ("q_stable", "q_transition", "p_stable_stay", "p_transition_stay")
    }
    add(
        "candidate_parameters_match_temporal_train_tuning",
        selected_grid == expected_grid,
        selected_grid,
        expected_grid,
    )
    add(
        "candidate_prohibits_current_test",
        candidate.get("current_t1_test_used") is False
        and candidate.get("current_t1_test_authorized") is False
        and candidate.get("eligible_for_final_claim") is False
        and candidate.get("test_access_policy")
        == "no_test_phase_current_holdout_ineligible",
        {
            "used": candidate.get("current_t1_test_used"),
            "authorized": candidate.get("current_t1_test_authorized"),
            "eligible_for_final_claim": candidate.get("eligible_for_final_claim"),
        },
        {"used": False, "authorized": False, "eligible_for_final_claim": False},
    )
    for relative, expected_hash in candidate.get("source_hashes", {}).items():
        source_path = resolve_repo_path(root, relative)
        observed_hash = sha256_file(source_path) if source_path.is_file() else None
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
    add(
        "gate_report_hash_frozen",
        sha256_file(paths["gate_report"]) == candidate["gate_report_sha256"],
        sha256_file(paths["gate_report"]),
        candidate["gate_report_sha256"],
    )
    add(
        "report_confirms_frozen_comparators",
        report["comparators"]["mode"] == "frozen_predictions_only_no_refit"
        and report["test_data_loaded"] is False
        and report["test_phase_available"] is False,
        {
            "mode": report["comparators"]["mode"],
            "test_data_loaded": report["test_data_loaded"],
        },
        {"mode": "frozen_predictions_only_no_refit", "test_data_loaded": False},
    )
    holdout_policy = yaml.safe_load(
        (root / "configs" / "final_holdout_v2.yaml").read_text(encoding="utf-8")
    )
    add(
        "final_holdout_pending",
        holdout_policy.get("status") == "PENDING_DATA"
        and holdout_policy["excluded_evaluation_sets"][0]["split"] == "t1_v1/test",
        {
            "status": holdout_policy.get("status"),
            "excluded": holdout_policy["excluded_evaluation_sets"][0]["split"],
        },
        {"status": "PENDING_DATA", "excluded": "t1_v1/test"},
    )

    failed = [check for check in checks if not check["passed"]]
    validation_report = {
        "schema_version": 1,
        "gate": config["gate"],
        "status": "PASS" if not failed else "FAIL",
        "overall_assessment": "Share with caveats" if not failed else "Needs revision",
        "question": (
            "Are the predeclared IMM experiment, frozen B1/B5/B6 comparison, "
            "transition/leave-zone claims, train-only calibration, and no-test boundary reproducible?"
        ),
        "summary": {"checks": len(checks), "failed": len(failed)},
        "checks": checks,
        "methodology_review": {
            "comparator_reuse": "frozen Gate B2 prediction rows compared to the B3 copy",
            "metric_recomputation": "fold, aggregate, transition, problem-transition, and regime summaries recomputed from rows",
            "interval_recomputation": "finite-sample qhat and interval metrics recomputed",
            "data_boundary": "calibration IDs are train-only; outer IDs are train/validation only; AST scan found no test loader call",
            "tuning_audit": "all contexts use the fixed 16-candidate grid and choose the minimum predeclared score",
        },
        "remaining_caveats": [
            "No new unseen final holdout exists yet.",
            "The hypothesis was motivated by known validation segment failures, even though the B7 protocol was fixed before B7 execution.",
            "Transition labels are origin-feature proxies and IMM regimes are statistical, not physical-state labels.",
            "Validation precision is limited by repeated points/profiles and 130 temporal validation origins.",
        ],
    }
    inventory_path = paths["artifact_inventory"]
    if write_outputs:
        write_json_atomic(root, paths["validation_report"], validation_report)
        artifact_root = resolve_repo_path(root, config["artifacts"]["root"])
        inventory_sources = sorted(
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
                for path in inventory_sources
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


def find_test_loader_calls(paths: Iterable[Path]) -> list[str]:
    findings: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            else:
                name = ""
            if name != "load_split_dataset":
                continue
            split_value: str | None = None
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                split_value = str(node.args[1].value)
            for keyword in node.keywords:
                if keyword.arg == "split" and isinstance(keyword.value, ast.Constant):
                    split_value = str(keyword.value.value)
            if split_value is not None and split_value.lower().startswith("test"):
                findings.append(f"{path.as_posix()}:{node.lineno}:{split_value}")
    return findings


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
    work_dir = root / "work" / "gate_b3"
    work_dir.mkdir(parents=True, exist_ok=True)
    temporary = work_dir / f"{path.name}.{uuid4().hex}.tmp"
    temporary.write_text(text, encoding="utf-8", newline="\n")
    temporary.replace(path)


def _frames_equivalent(left: pd.DataFrame, right: pd.DataFrame) -> bool:
    if set(left.columns) != set(right.columns) or len(left) != len(right):
        return False
    columns = list(right.columns)
    sort_columns = [
        column
        for column in ("design", "fold_id", "model_id", "scope", "segment", "sample_id")
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
    left_ordered = left_ordered.reset_index(drop=True)
    right_ordered = right_ordered.reset_index(drop=True)
    try:
        pd.testing.assert_frame_equal(
            left_ordered,
            right_ordered,
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


def _package_versions() -> dict[str, str]:
    names = ["numpy", "pandas", "scipy", "scikit-learn", "PyYAML"]
    return {name: importlib.metadata.version(name) for name in names}


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

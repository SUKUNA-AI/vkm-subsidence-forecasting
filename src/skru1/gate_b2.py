"""Controlled train/validation-only workflow for T1 Gate B2."""

from __future__ import annotations

import ast
from collections import Counter
from dataclasses import asdict
from hashlib import sha256
import importlib.metadata
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from uuid import uuid4

import numpy as np
import pandas as pd
import yaml

from .adaptive_kalman import (
    AdaptiveKalmanRate,
    PreparedKalmanHistory,
    prepare_kalman_history,
)
from .baselines import TARGET_COLUMN, build_model
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
from .metrics import regression_metrics
from .splits import (
    ManifestDataset,
    load_split_dataset,
    read_manifest,
    rolling_origin_assignments,
    sample_id_list_sha256,
)
from .transition_validation import (
    TransitionThresholds,
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


def load_gate_b2_config(
    root: str | Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    project_root = discover_project_root(root)
    path = project_root / "configs" / "gate_b2.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ContractViolation("configs/gate_b2.yaml must contain a mapping")
    required = {
        "task",
        "split_version",
        "test_policy",
        "development_policy",
        "reference_models",
        "adaptive_model",
        "interval_calibration",
        "transition_validation",
        "acceptance",
        "artifacts",
    }
    missing = required - set(config)
    if missing:
        raise ContractViolation(f"Gate B2 config is missing keys: {sorted(missing)}")
    for value in config["artifacts"].values():
        resolve_repo_path(project_root, str(value))
    for value in config["test_policy"]["protected_files"]:
        resolve_repo_path(project_root, str(value))
    if config["test_policy"]["model_selection_access"] != "prohibited":
        raise ContractViolation("Gate B2 must prohibit current T1 test model-selection access")
    return project_root, config


def resolve_repo_path(root: Path, relative: str | Path) -> Path:
    path = Path(relative)
    if path.is_absolute():
        raise ContractViolation(f"Gate B2 path must be repository-relative: {path}")
    resolved = (root / path).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ContractViolation(f"Gate B2 path escapes repository root: {path}") from exc
    return resolved


def capture_protected_test_snapshot(
    root: Path,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """Hash already disclosed outputs without loading any test model frame."""

    files: list[dict[str, Any]] = []
    for relative, expected_hash in config["test_policy"]["protected_files"].items():
        path = resolve_repo_path(root, relative)
        if not path.is_file():
            raise FileNotFoundError(f"Protected T1 test artifact is missing: {relative}")
        observed_hash = sha256_file(path)
        if observed_hash.lower() != str(expected_hash).lower():
            raise ContractViolation(
                f"Protected T1 test artifact changed before Gate B2: {relative}"
            )
        files.append(
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
        "policy": "hash_only_no_test_loader",
        "protected_files": files,
        "all_match": True,
    }


def adaptive_parameters(
    config: Mapping[str, Any],
    *,
    q_base: float,
    acceleration_gain: float,
) -> dict[str, Any]:
    parameters = dict(config["adaptive_model"]["parameters"])
    parameters["q_base"] = float(q_base)
    parameters["acceleration_gain"] = float(acceleration_gain)
    return parameters


def tune_adaptive_parameters(
    training: ManifestDataset,
    *,
    prepared_history: PreparedKalmanHistory,
    config: Mapping[str, Any],
    context: str,
) -> tuple[dict[str, Any], pd.DataFrame]:
    """Nested expanding-window selection entirely within one outer train fold."""

    policy = config["development_policy"]
    assignments = rolling_origin_assignments(
        [training],
        minimum_train_dates=int(policy["inner_minimum_train_dates"]),
        maximum_folds=int(policy["inner_rolling_origin_folds"]),
    )
    candidate_results: list[dict[str, Any]] = []
    candidate_fold_rows: list[list[dict[str, Any]]] = []
    for q_base in config["adaptive_model"]["q_base_grid"]:
        for acceleration_gain in config["adaptive_model"]["acceleration_gain_grid"]:
            parameters = adaptive_parameters(
                config,
                q_base=float(q_base),
                acceleration_gain=float(acceleration_gain),
            )
            fold_rows: list[dict[str, Any]] = []
            all_truth: list[np.ndarray] = []
            all_prediction: list[np.ndarray] = []
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
                    label=f"{context}_{fold_id}_train",
                )
                inner_validation = derived_dataset(
                    training,
                    validation_ids,
                    split="validation",
                    label=f"{context}_{fold_id}_validation",
                )
                model = AdaptiveKalmanRate(
                    model_id=str(config["adaptive_model"]["model_id"]),
                    parameters=parameters,
                ).fit(inner_train)
                prediction, _, _ = model.predict_distribution(
                    inner_validation, history_frame=prepared_history
                )
                truth = pd.to_numeric(
                    inner_validation.frame[TARGET_COLUMN], errors="raise"
                ).to_numpy(float)
                metrics = regression_metrics(truth, prediction)
                train_dates = pd.to_datetime(inner_train.frame["target_date"])
                validation_dates = pd.to_datetime(inner_validation.frame["target_date"])
                fold_rows.append(
                    {
                        "tuning_context": context,
                        "candidate_key": _candidate_key(float(q_base), float(acceleration_gain)),
                        "q_base": float(q_base),
                        "acceleration_gain": float(acceleration_gain),
                        "inner_fold_id": str(fold_id),
                        "train_rows": len(inner_train.frame),
                        "validation_rows": len(inner_validation.frame),
                        "train_target_date_max": train_dates.max().date().isoformat(),
                        "validation_target_date_min": validation_dates.min().date().isoformat(),
                        "validation_target_date_max": validation_dates.max().date().isoformat(),
                        "train_sample_ids_sha256": inner_train.provenance.sample_ids_sha256,
                        "validation_sample_ids_sha256": inner_validation.provenance.sample_ids_sha256,
                        **metrics,
                    }
                )
                all_truth.append(truth)
                all_prediction.append(prediction)
            aggregate = regression_metrics(
                np.concatenate(all_truth), np.concatenate(all_prediction)
            )
            candidate_results.append(
                {
                    "q_base": float(q_base),
                    "acceleration_gain": float(acceleration_gain),
                    "aggregate_mae": aggregate["mae"],
                    "aggregate_rmse": aggregate["rmse"],
                }
            )
            candidate_fold_rows.append(fold_rows)
    ranking = sorted(
        candidate_results,
        key=lambda row: (
            row["aggregate_mae"],
            row["acceleration_gain"],
            row["q_base"],
        ),
    )
    selected = ranking[0]
    selected_key = _candidate_key(selected["q_base"], selected["acceleration_gain"])
    aggregate_lookup = {
        _candidate_key(row["q_base"], row["acceleration_gain"]): row
        for row in candidate_results
    }
    output_rows: list[dict[str, Any]] = []
    for fold_rows in candidate_fold_rows:
        for row in fold_rows:
            summary = aggregate_lookup[row["candidate_key"]]
            output_rows.append(
                {
                    **row,
                    "candidate_aggregate_mae": summary["aggregate_mae"],
                    "candidate_aggregate_rmse": summary["aggregate_rmse"],
                    "selected": row["candidate_key"] == selected_key,
                }
            )
    parameters = adaptive_parameters(
        config,
        q_base=selected["q_base"],
        acceleration_gain=selected["acceleration_gain"],
    )
    return parameters, pd.DataFrame(output_rows)


def evaluate_outer_folds(
    development: ManifestDataset,
    folds: Sequence[EvaluationFold],
    *,
    bundle: CanonicalBundle,
    config: Mapping[str, Any],
    history_frame: pd.DataFrame,
    prepared_history: PreparedKalmanHistory,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    prediction_frames: list[pd.DataFrame] = []
    metric_rows: list[dict[str, Any]] = []
    tuning_frames: list[pd.DataFrame] = []
    threshold_rows: list[dict[str, Any]] = []
    transition_config = config["transition_validation"]
    for fold in folds:
        train_fold = derived_dataset(
            development,
            fold.train_sample_ids,
            split="train",
            label=f"gate_b2_{fold.fold_id}_train",
        )
        validation_fold = derived_dataset(
            development,
            fold.validation_sample_ids,
            split="validation",
            label=f"gate_b2_{fold.fold_id}_validation",
        )
        selected_parameters, tuning = tune_adaptive_parameters(
            train_fold,
            prepared_history=prepared_history,
            config=config,
            context=f"outer::{fold.fold_id}",
        )
        tuning_frames.append(tuning)
        thresholds = fit_transition_thresholds(
            train_fold.frame,
            acceleration_quantile=float(
                transition_config["acceleration_absolute_quantile"]
            ),
            volatility_quantile=float(transition_config["volatility_quantile"]),
            missing_campaigns_threshold=int(
                transition_config["missing_campaigns_threshold"]
            ),
        )
        threshold_rows.append(
            {
                "design": fold.design,
                "fold_id": fold.fold_id,
                "held_out_group": fold.held_out_group,
                "train_rows": len(train_fold.frame),
                "train_sample_ids_sha256": train_fold.provenance.sample_ids_sha256,
                **thresholds.to_dict(),
            }
        )
        transition = classify_transition_proxy(validation_fold.frame, thresholds)
        truth = pd.to_numeric(
            validation_fold.frame[TARGET_COLUMN], errors="raise"
        ).to_numpy(float)

        models: list[tuple[Any, np.ndarray | None, pd.DataFrame | None]] = []
        for reference_spec in config["reference_models"]:
            model = build_model(
                reference_spec,
                contract=bundle.feature_contract,
                random_seed=int(config["random_seed"]),
                weight_clip=(0.25, 4.0),
            ).fit(train_fold)
            prediction = model.predict(validation_fold, history_frame=history_frame)
            models.append((model, prediction, None))
        adaptive = AdaptiveKalmanRate(
            model_id=str(config["adaptive_model"]["model_id"]),
            parameters=selected_parameters,
        ).fit(train_fold)
        adaptive_prediction, adaptive_sigma, adaptive_diagnostics = (
            adaptive.predict_distribution(validation_fold, history_frame=prepared_history)
        )
        models.append((adaptive, adaptive_prediction, adaptive_diagnostics))

        for model, prediction, diagnostics in models:
            prediction = np.asarray(prediction, dtype=float)
            if prediction.shape != truth.shape or not np.isfinite(prediction).all():
                raise RuntimeError(
                    f"{model.model_id}/{fold.fold_id} produced invalid predictions"
                )
            metrics = regression_metrics(truth, prediction)
            metric_rows.append(
                {
                    "design": fold.design,
                    "fold_id": fold.fold_id,
                    "held_out_group": fold.held_out_group,
                    "model_id": model.model_id,
                    "family": model.family,
                    "train_rows": len(train_fold.frame),
                    "validation_rows": len(validation_fold.frame),
                    "selected_q_base": selected_parameters["q_base"]
                    if model is adaptive
                    else np.nan,
                    "selected_acceleration_gain": selected_parameters[
                        "acceleration_gain"
                    ]
                    if model is adaptive
                    else np.nan,
                    **metrics,
                }
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
            frame.insert(3, "model_id", model.model_id)
            frame.insert(4, "family", model.family)
            frame["y_true"] = truth
            frame["y_pred"] = prediction
            frame["error"] = prediction - truth
            frame["absolute_error"] = np.abs(prediction - truth)
            frame["selected_q_base"] = (
                selected_parameters["q_base"] if model is adaptive else np.nan
            )
            frame["selected_acceleration_gain"] = (
                selected_parameters["acceleration_gain"] if model is adaptive else np.nan
            )
            frame["raw_sigma"] = adaptive_sigma if model is adaptive else np.nan
            if diagnostics is not None:
                for column in diagnostics:
                    frame[column] = diagnostics[column].to_numpy()
            else:
                frame["process_scale"] = np.nan
                frame["adaptive_q"] = np.nan
                frame["causal_history_rows"] = np.nan
            for column in transition:
                frame[column] = transition[column].to_numpy()
            prediction_frames.append(frame)
    return (
        pd.concat(prediction_frames, ignore_index=True),
        pd.DataFrame(metric_rows),
        pd.concat(tuning_frames, ignore_index=True),
        pd.DataFrame(threshold_rows),
    )


def nested_calibration_predictions(
    train: ManifestDataset,
    *,
    config: Mapping[str, Any],
    prepared_history: PreparedKalmanHistory,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Create nested rolling OOF residuals without using validation labels."""

    policy = config["development_policy"]
    assignments = rolling_origin_assignments(
        [train],
        minimum_train_dates=int(policy["inner_minimum_train_dates"]),
        maximum_folds=int(policy["calibration_rolling_origin_folds"]),
    )
    predictions: list[pd.DataFrame] = []
    tuning_frames: list[pd.DataFrame] = []
    for fold_id, assignment in assignments.groupby("fold_id", sort=True):
        train_ids = tuple(
            assignment.loc[assignment["role"].eq("train"), "sample_id"].astype(str)
        )
        validation_ids = tuple(
            assignment.loc[assignment["role"].eq("validation"), "sample_id"].astype(str)
        )
        calibration_train = derived_dataset(
            train,
            train_ids,
            split="train",
            label=f"calibration_{fold_id}_train",
        )
        calibration_validation = derived_dataset(
            train,
            validation_ids,
            split="validation",
            label=f"calibration_{fold_id}_validation",
        )
        selected, tuning = tune_adaptive_parameters(
            calibration_train,
            prepared_history=prepared_history,
            config=config,
            context=f"calibration::{fold_id}",
        )
        tuning_frames.append(tuning)
        model = AdaptiveKalmanRate(
            model_id=str(config["adaptive_model"]["model_id"]),
            parameters=selected,
        ).fit(calibration_train)
        mean, sigma, diagnostics = model.predict_distribution(
            calibration_validation, history_frame=prepared_history
        )
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
        frame["train_target_date_max"] = pd.to_datetime(
            calibration_train.frame["target_date"]
        ).max().date().isoformat()
        frame["validation_target_date_min"] = pd.to_datetime(
            calibration_validation.frame["target_date"]
        ).min().date().isoformat()
        frame["train_sample_ids_sha256"] = calibration_train.provenance.sample_ids_sha256
        frame["selected_q_base"] = selected["q_base"]
        frame["selected_acceleration_gain"] = selected["acceleration_gain"]
        frame["y_true"] = pd.to_numeric(
            calibration_validation.frame[TARGET_COLUMN], errors="raise"
        ).to_numpy(float)
        frame["y_pred"] = mean
        frame["raw_sigma"] = sigma
        for column in diagnostics:
            frame[column] = diagnostics[column].to_numpy()
        predictions.append(frame)
    return (
        pd.concat(predictions, ignore_index=True),
        pd.concat(tuning_frames, ignore_index=True),
    )


def aggregate_outer_metrics(predictions: pd.DataFrame) -> pd.DataFrame:
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
    return pd.DataFrame(rows).sort_values(
        ["design", "mae", "model_id"], kind="mergesort"
    ).reset_index(drop=True)


def compare_outer_models(aggregate: pd.DataFrame) -> pd.DataFrame:
    lookup = aggregate.set_index(["design", "model_id"])["mae"]
    rows: list[dict[str, Any]] = []
    for row in aggregate.itertuples(index=False):
        b1 = float(lookup.loc[(row.design, "B1_persistence_last_rate")])
        b5 = float(lookup.loc[(row.design, "B5_fixed_kalman")])
        rows.append(
            {
                "design": row.design,
                "model_id": row.model_id,
                "mae": float(row.mae),
                "reference_b1_mae": b1,
                "improvement_vs_b1_percent": 100.0 * (b1 - float(row.mae)) / b1,
                "reference_b5_mae": b5,
                "improvement_vs_b5_percent": 100.0 * (b5 - float(row.mae)) / b5,
            }
        )
    return pd.DataFrame(rows).sort_values(
        ["design", "mae", "model_id"], kind="mergesort"
    ).reset_index(drop=True)


def run_gate_b2_development(root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    """Run the complete B2 development stage without a test-loading code path."""

    before_snapshot = capture_protected_test_snapshot(root, config)
    bundle = load_canonical_bundle(root)
    train = load_split_dataset("t1", "train", root=root)
    validation = load_split_dataset("t1", "validation", root=root)
    if train.provenance.version != config["split_version"]:
        raise ContractViolation("Gate B2 split version differs from the frozen T1 manifest")
    development, folds, fold_contracts = build_gate_b0_b1_folds(
        train,
        validation,
        bundle,
        rolling_folds=int(config["development_policy"]["outer_designs"]["rolling_origin"]),
    )
    history_frame = causal_feature_history(development)
    prepared_history = prepare_kalman_history(history_frame)
    outer_predictions, outer_fold_metrics, outer_tuning, thresholds = evaluate_outer_folds(
        development,
        folds,
        bundle=bundle,
        config=config,
        history_frame=history_frame,
        prepared_history=prepared_history,
    )
    aggregate = aggregate_outer_metrics(outer_predictions)
    comparison = compare_outer_models(aggregate)
    transition = transition_metrics(outer_predictions)

    calibration_raw, calibration_tuning = nested_calibration_predictions(
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
    temporal_adaptive = outer_predictions.loc[
        outer_predictions["design"].eq("temporal_holdout")
        & outer_predictions["model_id"].eq(config["adaptive_model"]["model_id"])
    ].copy()
    validation_intervals = apply_scaled_conformal_intervals(
        temporal_adaptive,
        calibration_summary,
        sigma_floor=float(interval_config["sigma_floor_mm_y"]),
    )
    interval_summary = interval_metrics(validation_intervals, calibration_summary)

    q_tuning = pd.concat([outer_tuning, calibration_tuning], ignore_index=True).sort_values(
        [
            "tuning_context",
            "candidate_aggregate_mae",
            "acceleration_gain",
            "q_base",
            "inner_fold_id",
        ],
        kind="mergesort",
    ).reset_index(drop=True)
    paths = {
        name: resolve_repo_path(root, value)
        for name, value in config["artifacts"].items()
        if name != "root"
    }
    write_csv_atomic(root, paths["fold_contracts"], fold_contracts)
    write_csv_atomic(root, paths["q_tuning"], q_tuning)
    write_csv_atomic(root, paths["outer_fold_predictions"], outer_predictions)
    write_csv_atomic(root, paths["outer_fold_metrics"], outer_fold_metrics)
    write_csv_atomic(root, paths["aggregate_metrics"], aggregate)
    write_csv_atomic(root, paths["model_comparison"], comparison)
    write_csv_atomic(root, paths["calibration_predictions"], calibration_predictions)
    write_csv_atomic(root, paths["interval_calibration"], calibration_summary)
    write_csv_atomic(root, paths["validation_intervals"], validation_intervals)
    write_csv_atomic(root, paths["interval_metrics"], interval_summary)
    write_csv_atomic(root, paths["transition_thresholds"], thresholds)
    write_csv_atomic(root, paths["transition_metrics"], transition)

    screening = screening_assessment(
        aggregate,
        transition,
        interval_summary,
        acceptance=config["acceptance"],
    )
    temporal_tuning = q_tuning.loc[
        q_tuning["tuning_context"].eq("outer::temporal_validation_2024")
        & q_tuning["selected"].astype(bool)
    ]
    selected_q = float(temporal_tuning["q_base"].iloc[0])
    selected_gain = float(temporal_tuning["acceleration_gain"].iloc[0])
    selected_parameters = adaptive_parameters(
        config, q_base=selected_q, acceleration_gain=selected_gain
    )
    main_model = AdaptiveKalmanRate(
        model_id=str(config["adaptive_model"]["model_id"]),
        parameters=selected_parameters,
    ).fit(train)
    candidate_base = {
        "schema_version": 1,
        "candidate_scope": "gate_b2_train_validation_only",
        "status": "validation_frozen" if screening["all_pass"] else "validation_recorded",
        "task": "t1",
        "split_version": config["split_version"],
        "selected_model": config["adaptive_model"]["model_id"],
        "selected_parameters": selected_parameters,
        "model_state": main_model.state_dict(),
        "feature_contract_sha256": bundle.feature_contract.source_sha256,
        "target_contract_sha256": bundle.target_contract.source_sha256,
        "manifest_hashes": {
            "train": train.provenance.sample_ids_sha256,
            "validation": validation.provenance.sample_ids_sha256,
        },
        "selection_data": ["t1_v1/train", "t1_v1/validation"],
        "current_t1_test_used": False,
        "current_t1_test_authorized": False,
        "eligible_for_final_claim": bool(screening["all_pass"]),
        "final_evaluation_status": "PENDING_NEW_HOLDOUT_OR_GOVERNANCE_DECISION",
    }
    candidate_digest = sha256(
        json.dumps(candidate_base, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    candidate_id = f"t1-b2-v1-{candidate_digest[:12]}"

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
            "validation_target_date_max": pd.to_datetime(validation.frame["target_date"])
            .max()
            .date()
            .isoformat(),
            "train_sample_ids_sha256": train.provenance.sample_ids_sha256,
            "validation_sample_ids_sha256": validation.provenance.sample_ids_sha256,
        },
        "model": {
            "model_id": config["adaptive_model"]["model_id"],
            "selected_q_base": selected_q,
            "selected_acceleration_gain": selected_gain,
            "adaptation": "train-fold quantile scales for acceleration, volatility, and campaign gaps",
            "rate_anchor": "origin-known last_rate with uncertainty-derived variance",
        },
        "validation_design": {
            "fold_counts": dict(sorted(fold_counts.items())),
            "outer_forward_only": True,
            "inner_tuning_forward_only": True,
            "calibration": "five nested rolling train-only OOF folds",
        },
        "screening": screening,
        "intervals": interval_summary.to_dict(orient="records"),
        "transition_proxy": {
            "definition": (
                "accelerating/decelerating at train-fold q80 absolute acceleration; "
                "otherwise volatility at train-fold q80 or at least two missing campaigns"
            ),
            "future_or_private_fields_used": [],
        },
        "governance": {
            "current_t1_test_role": config["test_policy"]["current_t1_test_role"],
            "current_t1_test_used": False,
            "new_holdout_policy": config["test_policy"]["replacement_holdout_policy"],
            "final_claim_allowed_now": False,
        },
        "environment": _package_versions(),
        "caveats": [
            "Gate B2 is train/validation evidence and is not an unseen final estimate.",
            "Transition categories are origin-only proxies, not private event labels or causal regimes.",
            "Rows repeat points and profiles; row counts are not independent trajectory counts.",
            "Conformal coverage is assessed on 130 validation origins and is conditional on the frozen temporal split.",
            "A new future or external holdout, or an explicit governance decision, is required for final claims.",
        ],
    }
    write_json_atomic(root, paths["gate_report"], report)

    source_paths = [
        root / "configs" / "gate_b2.yaml",
        root / "configs" / "final_holdout_v2.yaml",
        root / "src" / "skru1" / "adaptive_kalman.py",
        root / "src" / "skru1" / "uncertainty.py",
        root / "src" / "skru1" / "transition_validation.py",
        root / "src" / "skru1" / "gate_b2.py",
        root / "scripts" / "run_gate_b2.py",
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

    holdout_policy_path = root / "configs" / "final_holdout_v2.yaml"
    holdout_doc_path = root / "docs" / "governance" / "FINAL_EVALUATION_POLICY_V2.md"
    holdout_status = {
        "schema_version": 1,
        "policy_id": "T1_FINAL_HOLDOUT_V2",
        "status": "PENDING_DATA",
        "current_t1_test_eligible": False,
        "current_t1_test_role": "historical_stage_candidate_diagnostic_only",
        "candidate_id": candidate_id,
        "candidate_eligible_if_screening_passed": bool(screening["all_pass"]),
        "final_claim_allowed": False,
        "required_next_event": "freeze_new_future_or_external_holdout_manifest",
        "policy_config": holdout_policy_path.relative_to(root).as_posix(),
        "policy_config_sha256": sha256_file(holdout_policy_path),
        "policy_document": holdout_doc_path.relative_to(root).as_posix(),
        "policy_document_sha256": sha256_file(holdout_doc_path),
    }
    write_json_atomic(root, paths["final_holdout_status"], holdout_status)

    after_snapshot = capture_protected_test_snapshot(root, config)
    protected_snapshot = {
        **before_snapshot,
        "verified_after_generation": True,
        "after_generation_sha256": {
            row["relative_path"]: row["observed_sha256"]
            for row in after_snapshot["protected_files"]
        },
        "unchanged_during_gate_b2": before_snapshot == after_snapshot,
    }
    write_json_atomic(root, paths["protected_test_snapshot"], protected_snapshot)
    return {
        "phase": "develop",
        "status": report["status"],
        "candidate_id": candidate_id,
        "selected_q_base": selected_q,
        "selected_acceleration_gain": selected_gain,
        "screening_all_pass": bool(screening["all_pass"]),
        "test_data_loaded": False,
        "outer_folds": len(folds),
        "calibration_rows": len(calibration_predictions),
    }


def screening_assessment(
    aggregate: pd.DataFrame,
    transition: pd.DataFrame,
    intervals: pd.DataFrame,
    *,
    acceptance: Mapping[str, Any],
) -> dict[str, Any]:
    lookup = aggregate.set_index(["design", "model_id"])["mae"]
    adaptive_id = "B6_adaptive_kalman"
    temporal = float(lookup.loc[("temporal_holdout", adaptive_id)])
    temporal_b1 = float(lookup.loc[("temporal_holdout", "B1_persistence_last_rate")])
    profile = float(lookup.loc[("leave_profile_out", adaptive_id)])
    zone = float(lookup.loc[("leave_zone_out", adaptive_id)])
    transition_row = transition.loc[
        transition["design"].eq("temporal_holdout")
        & transition["model_id"].eq(adaptive_id)
        & transition["scope"].eq("stable_vs_transition")
        & transition["segment"].eq("transition")
    ].iloc[0]
    interval_95 = intervals.loc[np.isclose(intervals["coverage_nominal"], 0.95)].iloc[0]
    observed = {
        "temporal_mae": temporal,
        "temporal_b1_mae": temporal_b1,
        "temporal_mae_ratio_vs_b1": temporal / temporal_b1,
        "transition_rows": int(transition_row["rows"]),
        "transition_mae": float(transition_row["mae"]),
        "transition_b1_mae": float(transition_row["reference_b1_mae"]),
        "transition_mae_improvement_vs_b1_percent": float(
            transition_row["improvement_vs_b1_percent"]
        ),
        "leave_profile_mae": profile,
        "leave_profile_mae_degradation_vs_temporal_percent": 100.0
        * (profile - temporal)
        / temporal,
        "leave_zone_mae": zone,
        "leave_zone_mae_degradation_vs_temporal_percent": 100.0
        * (zone - temporal)
        / temporal,
        "coverage_95_empirical": float(interval_95["coverage_empirical"]),
        "coverage_95_mean_width_mm_y": float(interval_95["mean_width_mm_y"]),
    }
    checks = {
        "temporal_vs_b1": observed["temporal_mae_ratio_vs_b1"]
        <= float(acceptance["temporal_mae_ratio_vs_b1_max"]),
        "transition_improvement": observed[
            "transition_mae_improvement_vs_b1_percent"
        ]
        >= float(acceptance["transition_mae_improvement_vs_b1_percent_min"]),
        "leave_profile_degradation": observed[
            "leave_profile_mae_degradation_vs_temporal_percent"
        ]
        <= float(acceptance["leave_profile_mae_degradation_vs_temporal_percent_max"]),
        "leave_zone_degradation": observed[
            "leave_zone_mae_degradation_vs_temporal_percent"
        ]
        <= float(acceptance["leave_zone_mae_degradation_vs_temporal_percent_max"]),
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


def run_gate_b2_validation(root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    """Independently recompute the high-impact claims saved by development."""

    paths = {
        name: resolve_repo_path(root, value)
        for name, value in config["artifacts"].items()
        if name != "root"
    }
    required_paths = [
        paths["protected_test_snapshot"],
        paths["fold_contracts"],
        paths["q_tuning"],
        paths["outer_fold_predictions"],
        paths["aggregate_metrics"],
        paths["calibration_predictions"],
        paths["interval_calibration"],
        paths["validation_intervals"],
        paths["interval_metrics"],
        paths["transition_metrics"],
        paths["development_candidate"],
        paths["gate_report"],
        paths["final_holdout_status"],
    ]
    missing = [path for path in required_paths if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Gate B2 artifacts are missing: {missing}")
    protected = json.loads(paths["protected_test_snapshot"].read_text(encoding="utf-8"))
    fold_contracts = pd.read_csv(paths["fold_contracts"])
    q_tuning = pd.read_csv(paths["q_tuning"])
    predictions = pd.read_csv(paths["outer_fold_predictions"])
    aggregate = pd.read_csv(paths["aggregate_metrics"])
    calibration_predictions = pd.read_csv(paths["calibration_predictions"])
    calibration = pd.read_csv(paths["interval_calibration"])
    validation_intervals = pd.read_csv(paths["validation_intervals"])
    saved_interval_metrics = pd.read_csv(paths["interval_metrics"])
    saved_transition_metrics = pd.read_csv(paths["transition_metrics"])
    candidate = json.loads(paths["development_candidate"].read_text(encoding="utf-8"))
    report = json.loads(paths["gate_report"].read_text(encoding="utf-8"))
    holdout_status = json.loads(paths["final_holdout_status"].read_text(encoding="utf-8"))
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

    current_snapshot = capture_protected_test_snapshot(root, config)
    add(
        "protected_test_files_unchanged",
        bool(protected.get("unchanged_during_gate_b2"))
        and current_snapshot["protected_files"] == protected["protected_files"],
        protected.get("unchanged_during_gate_b2"),
        True,
    )
    source_paths = [
        root / "src" / "skru1" / "adaptive_kalman.py",
        root / "src" / "skru1" / "uncertainty.py",
        root / "src" / "skru1" / "transition_validation.py",
        root / "src" / "skru1" / "gate_b2.py",
        root / "scripts" / "run_gate_b2.py",
    ]
    test_loader_calls = find_test_loader_calls(source_paths)
    add("gate_b2_has_no_test_loader_call", not test_loader_calls, test_loader_calls, [])

    expected_fold_counts = {
        key: int(value)
        for key, value in config["development_policy"]["outer_designs"].items()
    }
    observed_fold_counts = (
        fold_contracts.groupby("design")["fold_id"].nunique().astype(int).to_dict()
    )
    add("outer_fold_counts", observed_fold_counts == expected_fold_counts, observed_fold_counts, expected_fold_counts)
    fold_order = pd.to_datetime(fold_contracts["train_target_date_max"]).lt(
        pd.to_datetime(fold_contracts["validation_target_date_min"])
    )
    add("outer_folds_forward_only", bool(fold_order.all()), int((~fold_order).sum()), 0)

    tuning_order = pd.to_datetime(q_tuning["train_target_date_max"]).lt(
        pd.to_datetime(q_tuning["validation_target_date_min"])
    )
    add("nested_tuning_forward_only", bool(tuning_order.all()), int((~tuning_order).sum()), 0)
    selected_candidates_per_context = (
        q_tuning.loc[q_tuning["selected"].astype(bool)]
        .groupby("tuning_context")["candidate_key"]
        .nunique()
    )
    add(
        "one_selected_tuning_candidate_per_context",
        len(selected_candidates_per_context) == q_tuning["tuning_context"].nunique()
        and bool(selected_candidates_per_context.eq(1).all()),
        selected_candidates_per_context.to_dict(),
        "exactly one candidate key per context",
    )

    for row in aggregate.itertuples(index=False):
        subset = predictions.loc[
            predictions["design"].eq(row.design)
            & predictions["model_id"].eq(row.model_id)
        ]
        independent = _independent_regression_metrics(subset["y_true"], subset["y_pred"])
        for metric in ("mae", "rmse", "bias", "r2"):
            observed = float(getattr(row, metric))
            expected = float(independent[metric])
            add(
                f"aggregate::{row.design}::{row.model_id}::{metric}",
                np.isclose(observed, expected, rtol=1e-10, atol=1e-10),
                observed,
                expected,
            )

    train_ids = set(
        read_manifest(root / "artifacts" / "splits" / "t1_v1" / "train.csv")[
            "sample_id"
        ].astype(str)
    )
    validation_ids = set(
        read_manifest(root / "artifacts" / "splits" / "t1_v1" / "validation.csv")[
            "sample_id"
        ].astype(str)
    )
    calibration_ids = set(calibration_predictions["sample_id"].astype(str))
    prediction_ids = set(predictions["sample_id"].astype(str))
    add(
        "calibration_ids_train_only",
        calibration_ids <= train_ids and not (calibration_ids & validation_ids),
        {"rows": len(calibration_ids), "validation_overlap": len(calibration_ids & validation_ids)},
        {"subset": "train", "validation_overlap": 0},
    )
    add(
        "outer_prediction_ids_development_only",
        prediction_ids <= (train_ids | validation_ids),
        len(prediction_ids - (train_ids | validation_ids)),
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

    score = calibration_predictions["nonconformity_score"].to_numpy(float)
    for row in calibration.itertuples(index=False):
        qhat, probability = finite_sample_conformal_quantile(
            score, coverage=float(row.coverage)
        )
        add(
            f"conformal_qhat::{row.coverage}",
            np.isclose(float(row.qhat), qhat, rtol=1e-12, atol=1e-12)
            and np.isclose(
                float(row.quantile_probability), probability, rtol=1e-12, atol=1e-12
            ),
            {"qhat": float(row.qhat), "probability": float(row.quantile_probability)},
            {"qhat": qhat, "probability": probability},
        )
    independent_interval_metrics = _independent_interval_metrics(
        validation_intervals, calibration
    )
    for row in saved_interval_metrics.itertuples(index=False):
        independent = independent_interval_metrics.loc[
            np.isclose(
                independent_interval_metrics["coverage_nominal"],
                float(row.coverage_nominal),
            )
        ].iloc[0]
        for metric in (
            "coverage_empirical",
            "mean_width_mm_y",
            "median_width_mm_y",
            "mean_interval_score",
        ):
            observed = float(getattr(row, metric))
            expected = float(independent[metric])
            add(
                f"interval::{row.coverage_nominal}::{metric}",
                np.isclose(observed, expected, rtol=1e-10, atol=1e-10),
                observed,
                expected,
            )

    valid_segments = {"stable", "accelerating", "decelerating", "volatile_or_gap"}
    add(
        "transition_segments_exhaustive",
        set(predictions["transition_segment"].astype(str)) <= valid_segments
        and predictions["transition_segment"].notna().all(),
        sorted(set(predictions["transition_segment"].astype(str))),
        sorted(valid_segments),
    )
    for row in saved_transition_metrics.itertuples(index=False):
        subset = predictions.loc[
            predictions["design"].eq(row.design)
            & predictions["model_id"].eq(row.model_id)
        ]
        if row.scope == "stable_vs_transition":
            subset = subset.loc[
                subset["is_transition"].astype(bool).eq(row.segment == "transition")
            ]
        elif row.scope == "mechanism":
            subset = subset.loc[subset["transition_segment"].eq(row.segment)]
        independent = float(np.mean(np.abs(subset["y_pred"] - subset["y_true"])))
        add(
            f"transition::{row.design}::{row.model_id}::{row.scope}::{row.segment}",
            len(subset) == int(row.rows)
            and np.isclose(float(row.mae), independent, rtol=1e-10, atol=1e-10),
            {"rows": len(subset), "mae": float(row.mae)},
            {"rows": int(row.rows), "mae": independent},
        )

    add(
        "candidate_prohibits_current_test",
        candidate.get("current_t1_test_used") is False
        and candidate.get("current_t1_test_authorized") is False
        and candidate.get("test_access_policy")
        == "no_test_phase_current_holdout_ineligible",
        {
            "used": candidate.get("current_t1_test_used"),
            "authorized": candidate.get("current_t1_test_authorized"),
            "policy": candidate.get("test_access_policy"),
        },
        {"used": False, "authorized": False},
    )
    for relative, expected_hash in candidate.get("source_hashes", {}).items():
        source_path = resolve_repo_path(root, relative)
        observed_hash = sha256_file(source_path) if source_path.is_file() else None
        add(
            f"candidate_source_hash::{relative}",
            observed_hash == expected_hash,
            observed_hash,
            expected_hash,
        )
    temporal_selected = q_tuning.loc[
        q_tuning["tuning_context"].eq("outer::temporal_validation_2024")
        & q_tuning["selected"].astype(bool)
    ].iloc[0]
    add(
        "candidate_parameters_match_temporal_train_tuning",
        np.isclose(
            float(candidate["selected_parameters"]["q_base"]),
            float(temporal_selected["q_base"]),
        )
        and np.isclose(
            float(candidate["selected_parameters"]["acceleration_gain"]),
            float(temporal_selected["acceleration_gain"]),
        ),
        {
            "q_base": candidate["selected_parameters"]["q_base"],
            "acceleration_gain": candidate["selected_parameters"]["acceleration_gain"],
        },
        {
            "q_base": float(temporal_selected["q_base"]),
            "acceleration_gain": float(temporal_selected["acceleration_gain"]),
        },
    )
    add(
        "gate_report_hash_frozen",
        sha256_file(paths["gate_report"]) == candidate["gate_report_sha256"],
        sha256_file(paths["gate_report"]),
        candidate["gate_report_sha256"],
    )
    add(
        "report_confirms_no_test_load",
        report.get("test_data_loaded") is False
        and report.get("test_phase_available") is False,
        {
            "test_data_loaded": report.get("test_data_loaded"),
            "test_phase_available": report.get("test_phase_available"),
        },
        {"test_data_loaded": False, "test_phase_available": False},
    )
    add(
        "final_holdout_pending",
        holdout_status.get("status") == "PENDING_DATA"
        and holdout_status.get("current_t1_test_eligible") is False
        and holdout_status.get("final_claim_allowed") is False,
        {
            "status": holdout_status.get("status"),
            "current_t1_test_eligible": holdout_status.get("current_t1_test_eligible"),
            "final_claim_allowed": holdout_status.get("final_claim_allowed"),
        },
        {
            "status": "PENDING_DATA",
            "current_t1_test_eligible": False,
            "final_claim_allowed": False,
        },
    )

    failed = [check for check in checks if not check["passed"]]
    validation_report = {
        "schema_version": 1,
        "gate": config["gate"],
        "status": "PASS" if not failed else "FAIL",
        "overall_assessment": "Share with caveats" if not failed else "Needs revision",
        "question": (
            "Are adaptive B6 tuning, train-only interval calibration, transition validation, "
            "and the no-current-test boundary internally reproducible?"
        ),
        "summary": {"checks": len(checks), "failed": len(failed)},
        "checks": checks,
        "methodology_review": {
            "metric_recomputation": "all aggregate MAE/RMSE/bias/R2 values recomputed from prediction rows",
            "interval_recomputation": "coverage, width, interval score, and finite-sample qhat independently recomputed",
            "transition_recomputation": "each saved transition MAE and row count recomputed",
            "data_boundary": "calibration IDs are train-only; outer IDs are train/validation only; AST scan found no test loader call",
        },
        "remaining_caveats": [
            "No new unseen final holdout exists yet.",
            "Transition labels are origin-feature proxies rather than independently adjudicated regime events.",
            "Validation precision is limited by repeated points/profiles and 130 temporal validation origins.",
        ],
    }
    write_json_atomic(root, paths["validation_report"], validation_report)
    inventory_path = paths["artifact_inventory"]
    artifact_root = resolve_repo_path(root, config["artifacts"]["root"])
    inventory_sources = sorted(
        [path for path in artifact_root.rglob("*") if path.is_file() and path != inventory_path]
        + [paths["final_holdout_status"]],
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
    }


def find_test_loader_calls(paths: Iterable[Path]) -> list[str]:
    """Find literal attempts to load a test split in Gate B2 source files."""

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
    work_dir = root / "work" / "gate_b2"
    work_dir.mkdir(parents=True, exist_ok=True)
    temporary = work_dir / f"{path.name}.{uuid4().hex}.tmp"
    temporary.write_text(text, encoding="utf-8", newline="\n")
    temporary.replace(path)


def _candidate_key(q_base: float, acceleration_gain: float) -> str:
    return f"q={q_base:g}|gain={acceleration_gain:g}"


def _independent_regression_metrics(
    y_true: Sequence[float], y_pred: Sequence[float]
) -> dict[str, float]:
    truth = np.asarray(y_true, dtype=float)
    prediction = np.asarray(y_pred, dtype=float)
    error = prediction - truth
    denominator = float(np.sum(np.square(truth - float(np.mean(truth)))))
    return {
        "mae": float(np.mean(np.abs(error))),
        "rmse": float(np.sqrt(np.mean(np.square(error)))),
        "bias": float(np.mean(error)),
        "r2": float(1.0 - np.sum(np.square(error)) / denominator),
    }


def _independent_interval_metrics(
    predictions: pd.DataFrame,
    calibration: pd.DataFrame,
) -> pd.DataFrame:
    truth = predictions["y_true"].to_numpy(float)
    rows: list[dict[str, float]] = []
    for row in calibration.itertuples(index=False):
        coverage = float(row.coverage)
        suffix = str(int(round(coverage * 100)))
        lower = predictions[f"lower_{suffix}"].to_numpy(float)
        upper = predictions[f"upper_{suffix}"].to_numpy(float)
        width = upper - lower
        covered = (truth >= lower) & (truth <= upper)
        alpha = 1.0 - coverage
        score = width.copy()
        below = truth < lower
        above = truth > upper
        score[below] += 2.0 / alpha * (lower[below] - truth[below])
        score[above] += 2.0 / alpha * (truth[above] - upper[above])
        rows.append(
            {
                "coverage_nominal": coverage,
                "coverage_empirical": float(np.mean(covered)),
                "mean_width_mm_y": float(np.mean(width)),
                "median_width_mm_y": float(np.median(width)),
                "mean_interval_score": float(np.mean(score)),
            }
        )
    return pd.DataFrame(rows)


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

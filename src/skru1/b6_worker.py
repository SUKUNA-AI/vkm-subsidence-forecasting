"""Isolated train-only execution worker for frozen Gate B6 jobs."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sys
from time import perf_counter
import traceback
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd
import yaml

from .adaptive_kalman import prepare_kalman_history
from .artifact_io import resolve_repo_path, write_csv_atomic, write_json_atomic
from .baselines import TARGET_COLUMN
from .benchmark_metrics import normal_crps
from .b6_probabilistic import quantile_crps_approximation
from .benchmarking import (
    PredictionBundle,
    assert_train_only_worker_job,
    canonical_json_sha256,
)
from .b6_models import AdapterPrediction, FrozenAdapterEnsemble, create_adapter
from .b6_governance import excluded_model_records
from .b6_registry import model_spec_from_registry
from .data_contracts import ContractViolation, load_canonical_bundle, sha256_file
from .evaluation import causal_feature_history, derived_dataset
from .leakage import LeakageViolation
from .splits import attach_spatial_zones, build_spatial_zone_map, load_split_dataset
from .transition_validation import classify_transition_proxy, fit_transition_thresholds


@dataclass(frozen=True)
class ParameterSelection:
    parameters: Mapping[str, Any]
    parameter_sha256: str
    effective_iterations: int | None
    tuning_rows: pd.DataFrame
    selected_inner_oof: pd.DataFrame
    selection_status: str
    eligible_under_preregistered_guardrails: bool


def run_environment_worker(
    root: Path,
    config: Mapping[str, Any],
    *,
    environment_id: str,
    phase: str,
    model_id: str,
) -> dict[str, Any]:
    if phase not in {"screen", "robustness"}:
        raise ValueError("B6 worker phase must be screen or robustness")
    artifact_root = resolve_repo_path(root, config["artifacts"]["root"])
    registry_path = resolve_repo_path(root, config["artifacts"]["model_registry"])
    job_path = resolve_repo_path(root, config["artifacts"]["frozen_job_manifest"])
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    job_manifest = json.loads(job_path.read_text(encoding="utf-8"))
    assert_train_only_worker_job(job_manifest)
    if model_id in excluded_model_records(root):
        raise ContractViolation(
            f"Model {model_id} is disabled by frozen governance amendment B6-GOV-001"
        )
    _assert_environment_ready(root, config, environment_id)
    spec = model_spec_from_registry(registry, model_id)
    if spec.environment_id != environment_id:
        raise ContractViolation(
            f"Model {model_id} requires {spec.environment_id}, not {environment_id}"
        )
    jobs = [
        job
        for job in job_manifest["jobs"]
        if job["phase"] == phase
        and job["environment_id"] == environment_id
        and job["model_id"] == model_id
    ]
    if not jobs:
        raise ContractViolation(f"No frozen {phase} jobs for {environment_id}/{model_id}")
    if phase == "robustness" and spec.status != "FROZEN_COMPARATOR":
        screen_path = resolve_repo_path(root, config["artifacts"]["screening_register"])
        if not screen_path.is_file():
            raise ContractViolation("Temporal screening register is required before robustness")
        screening = pd.read_csv(screen_path)
        match = screening.loc[screening["model_id"].eq(model_id)]
        if match.empty or not bool(match["advanced_to_robustness"].iloc[0]):
            raise ContractViolation(f"Model did not advance to robustness: {model_id}")

    shard_dir = artifact_root / "prediction_shards" / phase / environment_id
    tuning_dir = artifact_root / "tuning_shards" / phase / environment_id
    status_dir = artifact_root / "worker_status" / phase / environment_id
    predictions_path = shard_dir / f"{model_id}.csv"
    tuning_path = tuning_dir / f"{model_id}.csv"
    oof_path = tuning_dir / f"{model_id}__selected_inner_oof.csv"
    status_path = status_dir / f"{model_id}.json"
    learning_path = artifact_root / "learning_curve_shards" / environment_id / f"{model_id}.csv"
    status_provenance = {
        "registry_sha256": registry.get("registry_sha256"),
        "job_manifest_sha256": job_manifest.get("job_manifest_sha256"),
        "benchmark_plan_sha256": job_manifest.get("benchmark_plan_sha256"),
        "model_spec_sha256": spec.spec_sha256,
        "worker_source_sha256": sha256_file(Path(__file__)),
    }
    write_json_atomic(
        root,
        status_path,
        {
            "schema_version": 1,
            "status": "RUNNING",
            "phase": phase,
            "environment_id": environment_id,
            "model_id": model_id,
            "expected_folds": len(jobs),
            **status_provenance,
            "source_split": "t1_v1/train",
            "historical_validation_loaded": False,
            "current_test_loaded": False,
        },
        work_scope="gate_b6_worker",
    )

    bundle = load_canonical_bundle(root)
    train = load_split_dataset("t1", "train", root=root)
    zone_map, _ = build_spatial_zone_map(bundle)
    source = attach_spatial_zones(train, zone_map)
    outer_assignments = pd.read_csv(
        root / "artifacts" / "splits" / "t1_train_benchmark_v1" / "outer_assignments.csv",
        keep_default_na=False,
    )
    inner_assignments = pd.read_csv(
        root / "artifacts" / "splits" / "t1_train_benchmark_v1" / "inner_assignments.csv",
        keep_default_na=False,
    )
    contracts = pd.read_csv(
        root / "artifacts" / "splits" / "t1_train_benchmark_v1" / "fold_contracts.csv",
        keep_default_na=False,
    )
    raw_history = causal_feature_history(source)
    prepared_history = prepare_kalman_history(raw_history)
    prediction_frames: list[pd.DataFrame] = []
    tuning_frames: list[pd.DataFrame] = []
    inner_oof_frames: list[pd.DataFrame] = []
    failures: list[dict[str, Any]] = []
    inner_selection_rejections: list[dict[str, str]] = []
    for job_index, job in enumerate(jobs, start=1):
        fold_id = str(job["outer_fold_id"])
        print(f"[{model_id}] {phase} fold {job_index}/{len(jobs)}: {fold_id}", flush=True)
        try:
            train_ids, validation_ids = _role_ids(outer_assignments, fold_id)
            outer_train = derived_dataset(
                source,
                train_ids,
                split="train",
                label=f"gate_b6_{model_id}_{fold_id}_train",
            )
            outer_validation = derived_dataset(
                source,
                validation_ids,
                split="validation",
                label=f"gate_b6_{model_id}_{fold_id}_validation",
            )
            selection = select_parameters(
                source,
                outer_train,
                spec=spec,
                outer_fold_id=fold_id,
                inner_fold_ids=job["inner_fold_ids"],
                inner_assignments=inner_assignments,
                config=config,
                contract=bundle.feature_contract,
                raw_history=raw_history,
                prepared_history=prepared_history,
            )
            tuning_frames.append(selection.tuning_rows)
            inner_oof_frames.append(selection.selected_inner_oof)
            if not selection.eligible_under_preregistered_guardrails:
                inner_selection_rejections.append(
                    {"fold_id": fold_id, "selection_status": selection.selection_status}
                )
            thresholds = _thresholds(outer_train.frame, config)
            segments = classify_transition_proxy(outer_validation.frame, thresholds)
            seed_frames: list[pd.DataFrame] = []
            for seed in map(int, spec.seed_policy["seeds"]):
                parameters = dict(selection.parameters)
                if selection.effective_iterations is not None:
                    if spec.family in {
                        "xgboost",
                        "lightgbm",
                        "catboost",
                        "hist_gradient_boosting",
                        "quantile_hist_gradient_boosting",
                    }:
                        parameters["frozen_iterations"] = selection.effective_iterations
                    if spec.family in {"residual_mlp", "protocol_safe_enfs_replica"}:
                        parameters["frozen_epochs"] = selection.effective_iterations
                adapter = create_adapter(
                    spec,
                    parameters,
                    contract=bundle.feature_contract,
                    seed=seed,
                    raw_history=raw_history,
                    prepared_history=prepared_history,
                )
                _reset_peak_vram()
                memory_before = _resident_memory_mb()
                fit_started = perf_counter()
                adapter.fit(outer_train)
                fit_seconds = perf_counter() - fit_started
                inference_started = perf_counter()
                prediction = adapter.predict(outer_validation)
                inference_seconds = perf_counter() - inference_started
                frame = prediction_frame(
                    outer_validation,
                    prediction,
                    spec=spec,
                    seed=seed,
                    design=str(job["outer_design"]),
                    fold_id=fold_id,
                    benchmark_plan_sha256=str(job["benchmark_plan_sha256"]),
                    fold_manifest_sha256=str(job["outer_validation_sample_ids_sha256"]),
                    selected_parameter_sha256=selection.parameter_sha256,
                    selected_parameters=parameters,
                    segments=segments,
                    fit_seconds=fit_seconds,
                    inference_seconds=inference_seconds,
                    peak_ram_mb=max(_resident_memory_mb(), memory_before),
                    effective_iterations=adapter.effective_iterations_,
                    adapter=adapter,
                )
                PredictionBundle.validate(
                    frame,
                    expected_sample_ids=validation_ids,
                    expected_environment_id=environment_id,
                    expected_model_id=model_id,
                )
                seed_frames.append(frame)
            prediction_frames.extend(seed_frames)
            if len(seed_frames) > 1:
                prediction_frames.append(ensemble_prediction_frame(seed_frames))
        except Exception as exc:  # worker records a protocol-visible failure and continues
            failures.append(
                {
                    "model_id": model_id,
                    "environment_id": environment_id,
                    "phase": phase,
                    "fold_id": fold_id,
                    "exception_type": type(exc).__name__,
                    "failure_class": _failure_class(exc),
                    "message": _sanitize_failure_message(str(exc), root),
                    "traceback_frames": _traceback_frames(exc, root),
                }
            )
            print(f"[{model_id}] FAILED {fold_id}: {type(exc).__name__}: {exc}", flush=True)

    learning_predictions = pd.DataFrame()
    if phase == "robustness" and not failures:
        try:
            learning_predictions = evaluate_learning_curves(
                root,
                config,
                source,
                spec=spec,
                contract=bundle.feature_contract,
                raw_history=raw_history,
                prepared_history=prepared_history,
                benchmark_plan_sha256=str(job_manifest["benchmark_plan_sha256"]),
            )
        except Exception as exc:
            failures.append(
                {
                    "model_id": model_id,
                    "environment_id": environment_id,
                    "phase": phase,
                    "fold_id": "diagnostic_learning_curves",
                    "exception_type": type(exc).__name__,
                    "failure_class": _failure_class(exc),
                    "message": _sanitize_failure_message(str(exc), root),
                    "traceback_frames": _traceback_frames(exc, root),
                }
            )

    predictions = pd.concat(prediction_frames, ignore_index=True) if prediction_frames else pd.DataFrame()
    tuning = pd.concat(tuning_frames, ignore_index=True) if tuning_frames else pd.DataFrame()
    selected_oof = pd.concat(inner_oof_frames, ignore_index=True) if inner_oof_frames else pd.DataFrame()
    if not predictions.empty:
        write_csv_atomic(root, predictions_path, predictions, work_scope="gate_b6_worker")
    if not tuning.empty:
        write_csv_atomic(root, tuning_path, tuning, work_scope="gate_b6_worker")
    if not selected_oof.empty:
        write_csv_atomic(root, oof_path, selected_oof, work_scope="gate_b6_worker")
    if not learning_predictions.empty:
        write_csv_atomic(root, learning_path, learning_predictions, work_scope="gate_b6_worker")
    completed_folds = int(predictions["fold_id"].nunique()) if not predictions.empty else 0
    if not failures and completed_folds == len(jobs):
        worker_status = "PASS"
    elif any(item["failure_class"] == "PROTOCOL" for item in failures):
        worker_status = "FAIL_PROTOCOL"
    else:
        worker_status = "REJECTED_MODEL_EXECUTION"
    status = {
        "schema_version": 1,
        "status": worker_status,
        "phase": phase,
        "environment_id": environment_id,
        "model_id": model_id,
        "expected_folds": len(jobs),
        "completed_folds": completed_folds,
        "prediction_rows": len(predictions),
        "tuning_rows": len(tuning),
        "selected_inner_oof_rows": len(selected_oof),
        "learning_curve_prediction_rows": len(learning_predictions),
        "failures": failures,
        "inner_selection_rejections": inner_selection_rejections,
        **status_provenance,
        "source_split": "t1_v1/train",
        "historical_validation_loaded": False,
        "current_test_loaded": False,
    }
    write_json_atomic(root, status_path, status, work_scope="gate_b6_worker")
    return status


def _failure_class(exc: Exception) -> str:
    """Separate protocol integrity failures from ordinary model rejection.

    A numerical/convergence/library failure rejects that model scientifically;
    it does not invalidate otherwise intact benchmark evidence.  Data-boundary,
    manifest, leakage, and environment-contract failures remain Gate failures.
    """

    if isinstance(exc, (ContractViolation, LeakageViolation)):
        return "PROTOCOL"
    message = str(exc).lower()
    protocol_markers = (
        "environment is not ready",
        "historical validation",
        "current test",
        "sample hash",
        "manifest",
        "feature contract",
        "network access is prohibited",
    )
    return "PROTOCOL" if any(marker in message for marker in protocol_markers) else "MODEL_EXECUTION"


def _sanitize_failure_message(message: str, root: Path) -> str:
    return message.replace(str(root), "<repo>").replace(root.as_posix(), "<repo>")


def _traceback_frames(exc: Exception, root: Path) -> list[dict[str, Any]]:
    """Preserve actionable frames without committing machine-local paths."""

    frames: list[dict[str, Any]] = []
    root_resolved = root.resolve()
    for frame in traceback.extract_tb(exc.__traceback__, limit=10):
        path = Path(frame.filename)
        try:
            display = path.resolve().relative_to(root_resolved).as_posix()
            origin = "repository"
        except ValueError:
            display = path.name
            origin = "external_dependency"
        frames.append(
            {
                "file": display,
                "origin": origin,
                "line": int(frame.lineno),
                "function": frame.name,
            }
        )
    return frames


def freeze_full_train_model(
    root: Path,
    config: Mapping[str, Any],
    *,
    model_id: str,
) -> dict[str, Any]:
    """Fit and serialize the selected model inside its declared environment."""

    registry = json.loads(
        resolve_repo_path(root, config["artifacts"]["model_registry"]).read_text(encoding="utf-8")
    )
    job_manifest = json.loads(
        resolve_repo_path(root, config["artifacts"]["frozen_job_manifest"]).read_text(encoding="utf-8")
    )
    assert_train_only_worker_job(job_manifest)
    if model_id in excluded_model_records(root):
        raise ContractViolation(
            f"Model {model_id} is disabled by frozen governance amendment B6-GOV-001"
        )
    spec = model_spec_from_registry(registry, model_id)
    _assert_environment_ready(root, config, spec.environment_id)
    selected = json.loads(
        resolve_repo_path(root, config["artifacts"]["selected_parameter_registry"]).read_text(
            encoding="utf-8"
        )
    )
    if model_id not in selected.get("models", {}):
        raise ContractViolation(f"No frozen global parameters for full-train model: {model_id}")
    record = selected["models"][model_id]
    parameters = _parameters_with_frozen_iterations(spec, record)
    bundle = load_canonical_bundle(root)
    train = load_split_dataset("t1", "train", root=root)
    zone_map, _ = build_spatial_zone_map(bundle)
    source = attach_spatial_zones(train, zone_map)
    raw_history = causal_feature_history(source)
    prepared_history = prepare_kalman_history(raw_history)
    adapters = []
    for seed in map(int, spec.seed_policy["seeds"]):
        adapter = create_adapter(
            spec,
            parameters,
            contract=bundle.feature_contract,
            seed=seed,
            raw_history=raw_history,
            prepared_history=prepared_history,
        )
        adapter.fit(source)
        adapters.append(adapter)
    artifact = FrozenAdapterEnsemble(
        model_id=model_id,
        environment_id=spec.environment_id,
        adapters=tuple(adapters),
        seeds=tuple(map(int, spec.seed_policy["seeds"])),
    )
    try:
        import joblib
    except ImportError as exc:
        raise RuntimeError("joblib is required to freeze the full-train primary") from exc
    output_path = resolve_repo_path(root, config["artifacts"]["full_train_model"])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = root / "work" / "gate_b6_full_train" / f"{output_path.name}.tmp"
    temporary.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, temporary)
    temporary.replace(output_path)
    from .data_contracts import sha256_file

    manifest = {
        "schema_version": 1,
        "model_id": model_id,
        "environment_id": spec.environment_id,
        "relative_path": output_path.relative_to(root).as_posix(),
        "bytes": output_path.stat().st_size,
        "sha256": sha256_file(output_path),
        "train_sample_ids_sha256": source.provenance.sample_ids_sha256,
        "selected_parameter_sha256": str(record["parameter_sha256"]),
        "model_spec_sha256": spec.spec_sha256,
        "seeds": list(map(int, spec.seed_policy["seeds"])),
        "external_weight_sha256": None,
        "source_split": "t1_v1/train",
        "historical_validation_loaded": False,
        "current_test_loaded": False,
    }
    manifest_path = output_path.with_suffix(output_path.suffix + ".manifest.json")
    write_json_atomic(root, manifest_path, manifest, work_scope="gate_b6_full_train")
    return manifest


def select_parameters(
    source,
    outer_train,
    *,
    spec,
    outer_fold_id: str,
    inner_fold_ids: Sequence[str],
    inner_assignments: pd.DataFrame,
    config: Mapping[str, Any],
    contract,
    raw_history: pd.DataFrame,
    prepared_history: Any,
) -> ParameterSelection:
    candidates = tuple(spec.parameter_grid) if spec.parameter_grid else (dict(spec.fixed_parameters),)
    candidate_rows: list[dict[str, Any]] = []
    candidate_oof: dict[str, pd.DataFrame] = {}
    candidate_iterations: dict[str, list[int]] = {}
    for candidate_index, parameters in enumerate(candidates):
        parameter_json = json.dumps(parameters, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        parameter_sha = canonical_json_sha256(parameters)
        oof_frames: list[pd.DataFrame] = []
        iterations: list[int] = []
        failure = ""
        for inner_fold_id in inner_fold_ids:
            try:
                inner_train_ids, inner_validation_ids = _role_ids(inner_assignments, inner_fold_id)
                if not set(inner_train_ids).issubset(set(outer_train.sample_ids)) or not set(
                    inner_validation_ids
                ).issubset(set(outer_train.sample_ids)):
                    raise ContractViolation("Inner tuning fold escapes outer training role")
                inner_train = derived_dataset(
                    source,
                    inner_train_ids,
                    split="train",
                    label=f"gate_b6_{spec.model_id}_{outer_fold_id}_{inner_fold_id}_train",
                )
                inner_validation = derived_dataset(
                    source,
                    inner_validation_ids,
                    split="validation",
                    label=f"gate_b6_{spec.model_id}_{outer_fold_id}_{inner_fold_id}_validation",
                )
                thresholds = _thresholds(inner_train.frame, config)
                segments = classify_transition_proxy(inner_validation.frame, thresholds)
                for seed in map(int, spec.seed_policy["seeds"]):
                    adapter = create_adapter(
                        spec,
                        parameters,
                        contract=contract,
                        seed=seed,
                        raw_history=raw_history,
                        prepared_history=prepared_history,
                    )
                    adapter.fit(inner_train, validation=inner_validation)
                    prediction = adapter.predict(inner_validation)
                    if adapter.effective_iterations_ is not None:
                        iterations.append(int(adapter.effective_iterations_))
                    truth = pd.to_numeric(inner_validation.frame[TARGET_COLUMN], errors="raise").to_numpy(float)
                    b1 = _b1_prediction(inner_train.frame, inner_validation.frame)
                    oof = inner_validation.frame.loc[
                        :, [
                            "sample_id",
                            "point_id",
                            "profile_id",
                            "zone_id",
                            "current_date",
                            "target_date",
                            "forecast_horizon_days",
                            "current_standard_uncertainty_mm",
                        ]
                    ].copy()
                    oof.insert(0, "outer_fold_id", outer_fold_id)
                    oof.insert(1, "inner_fold_id", inner_fold_id)
                    oof.insert(2, "model_id", spec.model_id)
                    oof.insert(3, "seed", seed)
                    oof.insert(4, "parameter_sha256", parameter_sha)
                    oof["y_true"] = truth
                    oof["y_pred"] = prediction.mean
                    oof["b1_prediction"] = b1
                    oof["absolute_error"] = np.abs(prediction.mean - truth)
                    oof["b1_absolute_error"] = np.abs(b1 - truth)
                    oof["transition_segment"] = segments["transition_segment"].to_numpy()
                    oof["is_transition"] = segments["is_transition"].to_numpy()
                    oof["probabilistic_score"] = _probabilistic_score(truth, prediction)
                    oof["provenance_role"] = "inner_validation"
                    oof_frames.append(oof)
            except Exception as exc:
                failure = f"{type(exc).__name__}: {exc}"
                break
        if failure:
            candidate_rows.append(
                {
                    "outer_fold_id": outer_fold_id,
                    "model_id": spec.model_id,
                    "candidate_index": candidate_index,
                    "parameter_sha256": parameter_sha,
                    "parameter_json": parameter_json,
                    "status": "FAILED_FIT_OR_PREDICT",
                    "failure": failure,
                    "inner_folds": len(oof_frames),
                    "inner_rows": 0,
                    "inner_mae": np.nan,
                    "inner_crps": np.nan,
                    "inner_b1_mae": np.nan,
                    "transition_rows": 0,
                    "transition_mae": np.nan,
                    "transition_b1_mae": np.nan,
                    "transition_guardrail_status": "UNAVAILABLE_FAILED_CANDIDATE",
                    "eligible": False,
                    "selected": False,
                    "median_effective_iterations": np.nan,
                }
            )
            continue
        oof = pd.concat(oof_frames, ignore_index=True)
        inner_mae = float(oof["absolute_error"].mean())
        b1_mae = float(oof["b1_absolute_error"].mean())
        transition = oof.loc[oof["is_transition"].astype(bool)]
        transition_rows = int(len(transition))
        transition_mae = float(transition["absolute_error"].mean()) if transition_rows else float("nan")
        transition_b1 = float(transition["b1_absolute_error"].mean()) if transition_rows else float("nan")
        minimum_support = int(config["selection"]["transition_guardrail"]["minimum_inner_origins"])
        if transition_rows < minimum_support:
            guard_status = str(config["selection"]["transition_guardrail"]["unavailable_status"])
            guard_pass = True
        else:
            guard_pass = bool(
                transition_mae
                <= float(config["selection"]["transition_guardrail"]["maximum_mae_ratio_vs_b1"])
                * transition_b1
            )
            guard_status = "PASS" if guard_pass else "FAIL_TRANSITION_GUARDRAIL"
        has_probabilistic_objective = bool(spec.probabilistic_capabilities)
        inner_crps = float(oof["probabilistic_score"].mean()) if has_probabilistic_objective else float("nan")
        probabilistic_mae_pass = (
            not has_probabilistic_objective
            or inner_mae <= float(config["selection"]["probabilistic_mae_ratio_vs_b1_max"]) * b1_mae
        )
        probabilistic_score_available = bool(
            not has_probabilistic_objective or np.isfinite(inner_crps)
        )
        eligible = bool(guard_pass and probabilistic_mae_pass and probabilistic_score_available)
        candidate_rows.append(
            {
                "outer_fold_id": outer_fold_id,
                "model_id": spec.model_id,
                "candidate_index": candidate_index,
                "parameter_sha256": parameter_sha,
                "parameter_json": parameter_json,
                "status": "COMPLETE",
                "failure": "",
                "inner_folds": len(inner_fold_ids),
                "inner_rows": len(oof),
                "inner_mae": inner_mae,
                "inner_crps": inner_crps,
                "inner_b1_mae": b1_mae,
                "transition_rows": transition_rows,
                "transition_mae": transition_mae,
                "transition_b1_mae": transition_b1,
                "transition_guardrail_status": guard_status,
                "probabilistic_mae_guardrail_passed": probabilistic_mae_pass,
                "probabilistic_score_available": probabilistic_score_available,
                "eligible": eligible,
                "selected": False,
                "selection_status": "NOT_SELECTED",
                "median_effective_iterations": int(np.median(iterations)) if iterations else np.nan,
            }
        )
        candidate_oof[parameter_sha] = oof
        candidate_iterations[parameter_sha] = iterations
    tuning = pd.DataFrame(candidate_rows)
    objective = "inner_crps" if spec.probabilistic_capabilities else "inner_mae"
    eligible = tuning.loc[tuning["eligible"].astype(bool) & np.isfinite(tuning[objective])].copy()
    selection_status = "SELECTED_PREREGISTERED_GUARDRAILS_PASS"
    eligible_under_guardrails = not eligible.empty
    if eligible.empty:
        complete = tuning.loc[
            tuning["status"].eq("COMPLETE") & np.isfinite(tuning[objective])
        ].copy()
        if spec.status == "FROZEN_COMPARATOR":
            eligible = complete
            selection_status = "FROZEN_COMPARATOR_FIXED_SPEC"
        elif not complete.empty:
            # Bad inner performance is a scientific rejection, not a software
            # failure. Select the objective-best candidate only to complete a
            # diagnostic outer prediction; the worker records the failed
            # guardrail and the temporal screen cannot advance this model.
            eligible = complete
            selection_status = "DIAGNOSTIC_FALLBACK_NO_ELIGIBLE_CANDIDATE"
        else:
            selection_status = "NO_FINITE_OBJECTIVE"
    if eligible.empty:
        failures = tuning.loc[tuning["status"].ne("COMPLETE"), "failure"].tolist()
        raise RuntimeError(f"No eligible parameter candidate for {spec.model_id}: {failures[:3]}")
    eligible = eligible.sort_values(
        [objective, "transition_mae", "parameter_json"], kind="mergesort", na_position="last"
    )
    selected_index = int(eligible.index[0])
    tuning.loc[selected_index, "selected"] = True
    tuning.loc[selected_index, "selection_status"] = selection_status
    selected_sha = str(tuning.loc[selected_index, "parameter_sha256"])
    parameters = json.loads(str(tuning.loc[selected_index, "parameter_json"]))
    iterations = candidate_iterations.get(selected_sha, [])
    effective_iterations = int(np.median(iterations)) if iterations else None
    selected_oof = candidate_oof[selected_sha].copy()
    selected_oof["selected_effective_iterations"] = effective_iterations
    return ParameterSelection(
        parameters=parameters,
        parameter_sha256=selected_sha,
        effective_iterations=effective_iterations,
        tuning_rows=tuning,
        selected_inner_oof=selected_oof,
        selection_status=selection_status,
        eligible_under_preregistered_guardrails=eligible_under_guardrails,
    )


def prediction_frame(
    validation,
    prediction: AdapterPrediction,
    *,
    spec,
    seed: int,
    design: str,
    fold_id: str,
    benchmark_plan_sha256: str,
    fold_manifest_sha256: str,
    selected_parameter_sha256: str,
    selected_parameters: Mapping[str, Any],
    segments: pd.DataFrame,
    fit_seconds: float,
    inference_seconds: float,
    peak_ram_mb: float,
    effective_iterations: int | None,
    adapter,
    provenance_role: str = "outer_validation",
) -> pd.DataFrame:
    prediction.validate(len(validation.frame))
    metadata = [
        "sample_id",
        "point_id",
        "profile_id",
        "zone_id",
        "current_date",
        "target_date",
        "forecast_horizon_days",
        "last_rate_mm_y",
        "current_standard_uncertainty_mm",
        "sigma_rate_mm_y",
        "n_history",
        "missing_campaigns_since_previous",
    ]
    output = validation.frame.loc[:, metadata].copy()
    output.insert(0, "model_id", spec.model_id)
    output.insert(1, "family", spec.family)
    output.insert(2, "environment_id", spec.environment_id)
    output.insert(3, "feature_view", spec.feature_view)
    output.insert(4, "model_spec_sha256", spec.spec_sha256)
    output.insert(5, "benchmark_plan_sha256", benchmark_plan_sha256)
    output.insert(6, "fold_manifest_sha256", fold_manifest_sha256)
    output.insert(7, "seed", int(seed))
    output.insert(8, "design", design)
    output.insert(9, "fold_id", fold_id)
    output["transition_segment"] = segments["transition_segment"].to_numpy()
    output["is_transition"] = segments["is_transition"].to_numpy()
    output["y_true"] = pd.to_numeric(validation.frame[TARGET_COLUMN], errors="raise").to_numpy(float)
    output["y_pred"] = prediction.mean
    if prediction.predictive_std is not None:
        output["predictive_std"] = prediction.predictive_std
    if prediction.distribution_family is not None:
        output["distribution_family"] = prediction.distribution_family
    if "loc" in prediction.distribution_parameters:
        output["distribution_loc"] = prediction.distribution_parameters["loc"]
    if "scale" in prediction.distribution_parameters:
        output["distribution_scale"] = prediction.distribution_parameters["scale"]
    quantile_columns = {0.025: "q025", 0.10: "q10", 0.25: "q25", 0.50: "q50", 0.75: "q75", 0.90: "q90", 0.975: "q975"}
    for level, column in quantile_columns.items():
        if level in prediction.quantiles:
            output[column] = prediction.quantiles[level]
    state = adapter.state_dict()
    output["fit_seconds"] = float(fit_seconds)
    output["inference_seconds"] = float(inference_seconds)
    output["peak_ram_mb"] = float(peak_ram_mb)
    output["peak_vram_mb"] = _peak_vram_mb()
    output["artifact_size_bytes"] = 0
    output["parameter_count"] = _parameter_count(adapter, state)
    output["tree_count"] = _tree_count(adapter, state)
    output["rule_count"] = int(selected_parameters.get("fuzzy_rules", 0))
    output["selected_parameter_sha256"] = selected_parameter_sha256
    output["selected_parameter_json"] = json.dumps(
        selected_parameters, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    output["provenance_role"] = provenance_role
    output["effective_iterations"] = effective_iterations
    output["ensemble_member_count"] = 1
    output["aggregation"] = "single_seed"
    return output.loc[:, _prediction_column_order(output)]


def ensemble_prediction_frame(seed_frames: Sequence[pd.DataFrame]) -> pd.DataFrame:
    first = seed_frames[0].copy()
    if any(tuple(frame["sample_id"].astype(str)) != tuple(first["sample_id"].astype(str)) for frame in seed_frames[1:]):
        raise ContractViolation("Seed prediction rows are not aligned for ensemble aggregation")
    numeric_average = [
        "y_pred",
        "predictive_std",
        "distribution_loc",
        "distribution_scale",
        "q025",
        "q10",
        "q25",
        "q50",
        "q75",
        "q90",
        "q975",
    ]
    for column in numeric_average:
        available = [pd.to_numeric(frame[column], errors="coerce").to_numpy(float) for frame in seed_frames if column in frame]
        if available:
            first[column] = np.nanmean(np.vstack(available), axis=0)
    first["fit_seconds"] = sum(float(frame["fit_seconds"].iloc[0]) for frame in seed_frames)
    first["inference_seconds"] = sum(
        float(frame["inference_seconds"].iloc[0]) for frame in seed_frames
    )
    first["peak_ram_mb"] = max(float(frame["peak_ram_mb"].iloc[0]) for frame in seed_frames)
    first["peak_vram_mb"] = max(float(frame["peak_vram_mb"].iloc[0]) for frame in seed_frames)
    first["artifact_size_bytes"] = sum(
        int(frame["artifact_size_bytes"].iloc[0]) for frame in seed_frames
    )
    first["seed"] = -1
    first["ensemble_member_count"] = len(seed_frames)
    first["aggregation"] = "mean_of_fixed_seeds"
    return first


def evaluate_learning_curves(
    root: Path,
    config: Mapping[str, Any],
    source,
    *,
    spec,
    contract,
    raw_history: pd.DataFrame,
    prepared_history: Any,
    benchmark_plan_sha256: str,
) -> pd.DataFrame:
    """Run the four preregistered audit-tail curves without retuning."""

    selected_path = resolve_repo_path(root, config["artifacts"]["selected_parameter_registry"])
    if not selected_path.is_file():
        raise ContractViolation("Selected parameter registry is required for learning curves")
    selected = json.loads(selected_path.read_text(encoding="utf-8"))
    record = selected["models"].get(spec.model_id)
    if record is None:
        raise ContractViolation(f"No global selected parameters for {spec.model_id}")
    parameters = dict(record["parameters"])
    effective = record.get("effective_iterations")
    if effective is not None:
        if spec.family in {
            "xgboost",
            "lightgbm",
            "catboost",
            "hist_gradient_boosting",
            "quantile_hist_gradient_boosting",
        }:
            parameters["frozen_iterations"] = int(effective)
        if spec.family in {"residual_mlp", "protocol_safe_enfs_replica"}:
            parameters["frozen_epochs"] = int(effective)
    audit_date = pd.Timestamp("2023-11-07")
    dates = pd.to_datetime(source.frame["target_date"], errors="raise")
    audit_ids = tuple(source.frame.loc[dates.eq(audit_date), "sample_id"].astype(str))
    audit = derived_dataset(source, audit_ids, split="validation", label=f"b6_learning_{spec.model_id}_audit")
    core_dates = sorted(pd.Timestamp(value) for value in dates.loc[dates.lt(audit_date)].unique())
    expected = {5: 217, 9: 423, 14: 708, 18: 823}
    frames: list[pd.DataFrame] = []
    for campaigns, expected_rows in expected.items():
        chosen = set(core_dates[-campaigns:])
        train_ids = tuple(source.frame.loc[dates.isin(chosen), "sample_id"].astype(str))
        training = derived_dataset(
            source,
            train_ids,
            split="train",
            label=f"b6_learning_{spec.model_id}_{campaigns}",
        )
        if len(training.frame) != expected_rows:
            raise ContractViolation("Learning-curve train size changed")
        segments = classify_transition_proxy(audit.frame, _thresholds(training.frame, config))
        seed_frames: list[pd.DataFrame] = []
        for seed in map(int, spec.seed_policy["seeds"]):
            adapter = create_adapter(
                spec,
                parameters,
                contract=contract,
                seed=seed,
                raw_history=raw_history,
                prepared_history=prepared_history,
            )
            _reset_peak_vram()
            started = perf_counter()
            adapter.fit(training)
            fit_seconds = perf_counter() - started
            inference_started = perf_counter()
            prediction = adapter.predict(audit)
            inference_seconds = perf_counter() - inference_started
            frame = prediction_frame(
                audit,
                prediction,
                spec=spec,
                seed=seed,
                design="diagnostic_learning_curve",
                fold_id=f"learning_{campaigns}_campaigns",
                benchmark_plan_sha256=benchmark_plan_sha256,
                fold_manifest_sha256=canonical_json_sha256(list(audit_ids)),
                selected_parameter_sha256=str(record["parameter_sha256"]),
                selected_parameters=parameters,
                segments=segments,
                fit_seconds=fit_seconds,
                inference_seconds=inference_seconds,
                peak_ram_mb=_resident_memory_mb(),
                effective_iterations=effective,
                adapter=adapter,
                provenance_role="diagnostic_audit_tail",
            )
            frame["training_campaigns"] = campaigns
            frame["training_rows"] = expected_rows
            frame["hyperparameters_retuned"] = False
            seed_frames.append(frame)
        frames.extend(seed_frames)
        if len(seed_frames) > 1:
            ensemble = ensemble_prediction_frame(seed_frames)
            ensemble["training_campaigns"] = campaigns
            ensemble["training_rows"] = expected_rows
            ensemble["hyperparameters_retuned"] = False
            frames.append(ensemble)
    return pd.concat(frames, ignore_index=True)


def _prediction_column_order(frame: pd.DataFrame) -> list[str]:
    from .benchmarking import PREDICTION_OPTIONAL_COLUMNS, PREDICTION_REQUIRED_COLUMNS

    return [*PREDICTION_REQUIRED_COLUMNS, *[column for column in PREDICTION_OPTIONAL_COLUMNS if column in frame]]


def _probabilistic_score(truth: np.ndarray, prediction: AdapterPrediction) -> np.ndarray:
    if prediction.predictive_std is not None:
        return normal_crps(truth, prediction.mean, prediction.predictive_std)
    if prediction.quantiles:
        ordered = sorted(prediction.quantiles.items())
        levels = [float(level) for level, _ in ordered]
        values = np.column_stack([np.asarray(value, float) for _, value in ordered])
        return quantile_crps_approximation(truth, levels, values)
    return np.full(len(truth), np.nan)


def _b1_prediction(train_frame: pd.DataFrame, validation_frame: pd.DataFrame) -> np.ndarray:
    fallback = float(pd.to_numeric(train_frame[TARGET_COLUMN], errors="raise").median())
    values = pd.to_numeric(validation_frame["last_rate_mm_y"], errors="coerce").to_numpy(float)
    return np.where(np.isfinite(values), values, fallback)


def _thresholds(frame: pd.DataFrame, config: Mapping[str, Any]):
    policy = config["transition_validation"]
    return fit_transition_thresholds(
        frame,
        acceleration_quantile=float(policy["acceleration_absolute_quantile"]),
        volatility_quantile=float(policy["volatility_quantile"]),
        missing_campaigns_threshold=int(policy["missing_campaigns_threshold"]),
    )


def _role_ids(assignments: pd.DataFrame, fold_id: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    subset = assignments.loc[assignments["fold_id"].astype(str).eq(fold_id)]
    train_ids = tuple(subset.loc[subset["role"].eq("train"), "sample_id"].astype(str))
    validation_ids = tuple(subset.loc[subset["role"].eq("validation"), "sample_id"].astype(str))
    if not train_ids or not validation_ids or set(train_ids) & set(validation_ids):
        raise ContractViolation(f"Invalid frozen fold assignment: {fold_id}")
    return train_ids, validation_ids


def _parameters_with_frozen_iterations(spec, record: Mapping[str, Any]) -> dict[str, Any]:
    parameters = dict(record["parameters"])
    effective = record.get("effective_iterations")
    if effective is None:
        return parameters
    if spec.family in {
        "xgboost",
        "lightgbm",
        "catboost",
        "hist_gradient_boosting",
        "quantile_hist_gradient_boosting",
    }:
        parameters["frozen_iterations"] = int(effective)
    if spec.family in {"residual_mlp", "protocol_safe_enfs_replica"}:
        parameters["frozen_epochs"] = int(effective)
    return parameters


def _assert_environment_ready(root: Path, config: Mapping[str, Any], environment_id: str) -> None:
    manifest_path = resolve_repo_path(root, config["artifacts"]["environment_manifest"])
    if not manifest_path.is_file():
        raise ContractViolation("Gate B6 environment manifest is missing; run preflight")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    record = manifest.get("environments", {}).get(environment_id)
    if record is None or record.get("status") != "READY":
        raise ContractViolation(f"Environment is not READY: {environment_id}")
    expected_python = resolve_repo_path(root, record["python_executable"])
    if Path(sys.executable).resolve() != expected_python.resolve():
        raise ContractViolation(
            f"Worker interpreter mismatch: expected {record['python_executable']}, got {Path(sys.executable).name}"
        )


def _resident_memory_mb() -> float:
    try:
        import psutil

        memory = psutil.Process().memory_info()
        # Windows exposes the lifetime process peak directly.  Other
        # platforms fall back to the current RSS, which is explicitly captured
        # after fit while the estimator remains resident.
        observed = getattr(memory, "peak_wset", memory.rss)
        return float(observed / (1024**2))
    except Exception:
        return float("nan")


def _reset_peak_vram() -> None:
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats()
    except Exception:
        return


def _peak_vram_mb() -> float:
    try:
        import torch

        return float(torch.cuda.max_memory_allocated() / (1024**2)) if torch.cuda.is_available() else 0.0
    except Exception:
        return 0.0


def _parameter_count(adapter: Any, state: Mapping[str, Any]) -> int:
    if getattr(adapter, "network", None) is not None:
        return int(sum(parameter.numel() for parameter in adapter.network.parameters()))
    model_state = state.get("model_state", {})
    return int(model_state.get("parameter_count", 0) or 0)


def _tree_count(adapter: Any, state: Mapping[str, Any]) -> int:
    estimator = getattr(adapter, "estimator", None)
    for attribute in ("n_estimators_", "n_estimators", "tree_count_"):
        value = getattr(estimator, attribute, None)
        if isinstance(value, (int, np.integer)):
            return int(value)
    return int(state.get("model_state", {}).get("tree_count", 0) or 0)

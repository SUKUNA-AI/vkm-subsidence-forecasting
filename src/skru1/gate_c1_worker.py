"""Isolated CUDA worker for the Gate C1 compact sequence screen.

The worker can read only frozen sequence inputs, fold assignments, a staged
outer-train target file, and its frozen job.  It never imports the canonical
split loader and cannot see outer-validation target values.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import random
import socket
from time import perf_counter
from typing import Any, Iterator, Mapping, Sequence
import urllib.request
from uuid import uuid4

import numpy as np
import pandas as pd
import psutil
import torch

from .data_contracts import ContractViolation, sha256_file
from .gate_c1_checkpoints import TopKCheckpointManager, validate_checkpoint_manifest
from .gate_c1_interfaces import (
    C1FitContext,
    C1SequencePreprocessor,
    C1_SEEDS,
    SequenceModelSpec,
    SequencePredictionBundle,
    SequenceTargetScaler,
    assert_sha256,
    assert_train_only_c1_job,
    canonical_json_sha256,
    ordered_sample_hash,
    target_values_sha256,
)
from .gate_c1_models import (
    ModelOutput,
    create_sequence_model,
    model_parameter_count,
    point_huber_loss,
    student_t_nll_loss,
)
from .gate_c1_probabilistic import quantile_grid_crps, student_t_nll, student_t_quantiles


TARGET_COLUMN = "observed_rate_mm_y"


def configure_determinism(seed: int) -> None:
    os.environ["CUBLAS_WORKSPACE_CONFIG"] = ":4096:8"
    random.seed(int(seed))
    np.random.seed(int(seed))
    torch.manual_seed(int(seed))
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(int(seed))
    torch.use_deterministic_algorithms(True)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False


@contextmanager
def runtime_network_guard() -> Iterator[None]:
    """Fail closed on socket/URL access after environment staging."""

    original_connect = socket.socket.connect
    original_create_connection = socket.create_connection
    original_urlopen = urllib.request.urlopen

    def blocked(*_: Any, **__: Any) -> Any:
        raise ContractViolation("Gate C1 runtime network access is prohibited")

    socket.socket.connect = blocked
    socket.create_connection = blocked
    urllib.request.urlopen = blocked
    try:
        yield
    finally:
        socket.socket.connect = original_connect
        socket.create_connection = original_create_connection
        urllib.request.urlopen = original_urlopen


def run_outer_job(root: Path, runtime_payload: Mapping[str, Any]) -> dict[str, Any]:
    """Tune one architecture/fold and atomically emit one unlabeled shard."""

    root = root.resolve()
    assert_train_only_c1_job(runtime_payload)
    job = dict(runtime_payload["job"])
    spec = SequenceModelSpec.from_dict(runtime_payload["model_spec"])
    if job["model_id"] != spec.model_id or job["model_spec_sha256"] != spec.spec_sha256:
        raise ContractViolation("Gate C1 runtime job/model spec mismatch")
    for name in ("config_sha256", "code_sha256", "environment_sha256"):
        assert_sha256(str(runtime_payload[name]), name)
    if runtime_payload["environment_id"] != "gate_c_torch":
        raise ContractViolation("Gate C1 worker environment mismatch")
    if not torch.cuda.is_available() or torch.cuda.get_device_name(0) != "NVIDIA GeForce RTX 5070 Ti":
        raise ContractViolation("Gate C1 authoritative screen requires the frozen RTX 5070 Ti CUDA device")

    sequence_rows = pd.read_csv(root / runtime_payload["sequence_rows"])
    sequence_manifest = pd.read_csv(root / runtime_payload["sequence_manifest"])
    outer_assignments = pd.read_csv(root / runtime_payload["outer_assignments"])
    inner_assignments = pd.read_csv(root / runtime_payload["inner_assignments"])
    fold_contracts = pd.read_csv(root / runtime_payload["fold_contracts"])
    staged_target_path = _resolve_work_path(root, runtime_payload["staged_train_targets"])
    targets = pd.read_csv(staged_target_path)
    if list(targets.columns) != ["sample_id", TARGET_COLUMN]:
        raise ContractViolation("Staged Gate C1 targets must contain exactly sample_id and target")
    if targets["sample_id"].isna().any() or targets["sample_id"].duplicated().any():
        raise ContractViolation("Staged Gate C1 targets contain invalid sample IDs")
    if not np.isfinite(pd.to_numeric(targets[TARGET_COLUMN], errors="coerce")).all():
        raise ContractViolation("Staged Gate C1 targets contain non-finite values")

    fold_id = str(job["outer_fold_id"])
    outer_train_ids, outer_validation_ids = _role_ids(outer_assignments, fold_id)
    if ordered_sample_hash(outer_train_ids) != job["outer_train_sample_ids_sha256"]:
        raise ContractViolation("Gate C1 outer-train manifest hash mismatch")
    if ordered_sample_hash(outer_validation_ids) != job["outer_validation_sample_ids_sha256"]:
        raise ContractViolation("Gate C1 outer-validation manifest hash mismatch")
    if tuple(targets["sample_id"].astype(str)) != outer_train_ids:
        raise ContractViolation("Worker target staging exposes rows outside exact outer train")
    target_map = targets.set_index("sample_id")[TARGET_COLUMN]
    manifest_index = sequence_manifest.set_index("sample_id", drop=False)
    if (set(outer_train_ids) | set(outer_validation_ids)) - set(manifest_index.index.astype(str)):
        raise ContractViolation("Gate C1 job refers to unknown C0 sequences")

    tuning_records: list[dict[str, Any]] = []
    prediction_cache: dict[tuple[str, int], pd.DataFrame] = {}
    device = torch.device("cuda:0")
    with runtime_network_guard():
        for parameter_index, parameters in enumerate(spec.parameter_grid):
            parameter_json = _canonical_parameter_json(parameters)
            parameter_sha = canonical_json_sha256(parameters)
            for inner_fold_id in job["inner_fold_ids"]:
                inner_train_ids, inner_validation_ids = _role_ids(inner_assignments, str(inner_fold_id))
                if not set(inner_train_ids).issubset(outer_train_ids) or not set(
                    inner_validation_ids
                ).issubset(outer_train_ids):
                    raise ContractViolation("Gate C1 inner fold escapes outer train")
                if set(inner_train_ids) & set(inner_validation_ids):
                    raise ContractViolation("Gate C1 inner train/validation overlap")
                contract = _fold_contract(fold_contracts, str(inner_fold_id), level="inner")
                if not pd.Timestamp(contract["train_target_date_max"]) < pd.Timestamp(
                    contract["validation_target_date_min"]
                ):
                    raise ContractViolation("Gate C1 inner fold is not strict forward-only")
                train_values = _targets_for(target_map, inner_train_ids)
                validation_values = _targets_for(target_map, inner_validation_ids)
                train_target_sha = target_values_sha256(inner_train_ids, train_values)
                validation_target_sha = target_values_sha256(inner_validation_ids, validation_values)
                for seed in C1_SEEDS:
                    cache_key = _fit_cache_key(
                        spec=spec,
                        parameters=parameters,
                        seed=seed,
                        train_ids=inner_train_ids,
                        validation_ids=inner_validation_ids,
                        contract=contract,
                        train_target_sha=train_target_sha,
                        validation_target_sha=validation_target_sha,
                        runtime_payload=runtime_payload,
                    )
                    cached = _read_cache(root, cache_key)
                    physical_reused = cached is not None
                    if cached is None:
                        result, predictions = _fit_inner(
                            root=root,
                            fit_id=cache_key,
                            spec=spec,
                            parameters=parameters,
                            seed=seed,
                            train_ids=inner_train_ids,
                            validation_ids=inner_validation_ids,
                            train_values=train_values,
                            validation_values=validation_values,
                            sequence_rows=sequence_rows,
                            contract=contract,
                            runtime_payload=runtime_payload,
                            device=device,
                        )
                        result["source_fit_id"] = cache_key
                        _write_cache(root, cache_key, result, predictions)
                    else:
                        result, predictions = cached
                    logical = {
                        "model_id": spec.model_id,
                        "family": spec.family,
                        "outer_fold_id": fold_id,
                        "inner_fold_id": str(inner_fold_id),
                        "parameter_index": parameter_index,
                        "parameter_sha256": parameter_sha,
                        "parameter_json": parameter_json,
                        "seed": int(seed),
                        "train_rows": len(inner_train_ids),
                        "validation_rows": len(inner_validation_ids),
                        "train_sample_ids_sha256": ordered_sample_hash(inner_train_ids),
                        "validation_sample_ids_sha256": ordered_sample_hash(inner_validation_ids),
                        "train_sequence_pairs_sha256": str(contract["train_sequence_pairs_sha256"]),
                        "validation_sequence_pairs_sha256": str(contract["validation_sequence_pairs_sha256"]),
                        "train_target_sha256": train_target_sha,
                        "validation_target_sha256": validation_target_sha,
                        "fit_cache_key": cache_key,
                        "physical_fit_reused": bool(physical_reused),
                        **result,
                    }
                    tuning_records.append(logical)
                    prediction_cache[(cache_key, seed)] = predictions

        tuning = pd.DataFrame(tuning_records)
        selected_parameters, selected_sha, selected_index = _select_parameters(spec, tuning)
        selected_tuning = tuning.loc[tuning["parameter_sha256"].eq(selected_sha)].copy()
        if len(selected_tuning) != 15:
            raise ContractViolation("Gate C1 selected configuration must have exactly 15 inner fits")
        outer_epochs = max(1, int(np.median(selected_tuning["best_epoch"].to_numpy(int))))
        selected_oof = _selected_inner_oof(
            selected_tuning,
            prediction_cache,
            target_map=target_map,
            spec=spec,
            outer_fold_id=fold_id,
        )

        outer_train_values = _targets_for(target_map, outer_train_ids)
        outer_contract = _fold_contract(fold_contracts, fold_id, level="outer")
        outer_frames = []
        outer_checkpoint_manifests = []
        for seed in C1_SEEDS:
            outer_fit_id = canonical_json_sha256(
                {
                    "role": "outer",
                    "model_spec_sha256": spec.spec_sha256,
                    "selected_parameter_sha256": selected_sha,
                    "seed": int(seed),
                    "fold_id": fold_id,
                    "epochs": int(outer_epochs),
                    "train_sample_ids_sha256": ordered_sample_hash(outer_train_ids),
                    "train_sequence_pairs_sha256": str(
                        outer_contract["train_sequence_pairs_sha256"]
                    ),
                    "code_sha256": runtime_payload["code_sha256"],
                    "config_sha256": runtime_payload["config_sha256"],
                    "environment_sha256": runtime_payload["environment_sha256"],
                }
            )
            fit_result = _fit_outer(
                root=root,
                fit_id=outer_fit_id,
                spec=spec,
                parameters=selected_parameters,
                seed=seed,
                epochs=outer_epochs,
                train_ids=outer_train_ids,
                validation_ids=outer_validation_ids,
                train_values=outer_train_values,
                sequence_rows=sequence_rows,
                contract=outer_contract,
                runtime_payload=runtime_payload,
                device=device,
            )
            outer_checkpoint_manifests.append(
                {
                    "seed": int(seed),
                    "fit_id": outer_fit_id,
                    "path": fit_result["checkpoint_manifest"],
                    "sha256": fit_result["checkpoint_manifest_sha256"],
                    "selected_checkpoint_epoch": int(
                        fit_result["selected_checkpoint_epoch"]
                    ),
                    "retained_checkpoint_count": int(
                        fit_result["retained_checkpoint_count"]
                    ),
                }
            )
            outer_frames.append(
                _outer_prediction_frame(
                    spec=spec,
                    fold_id=fold_id,
                    seed=seed,
                    validation_ids=outer_validation_ids,
                    predictions=fit_result["predictions"],
                    selected_parameters=selected_parameters,
                    selected_parameter_sha256=selected_sha,
                    epochs=outer_epochs,
                    parameter_count=int(fit_result["parameter_count"]),
                    fit_seconds=float(fit_result["fit_seconds"]),
                    inference_seconds=float(fit_result["inference_seconds"]),
                    peak_ram_mb=float(fit_result["peak_ram_mb"]),
                    peak_vram_mb=float(fit_result["peak_vram_mb"]),
                    runtime_payload=runtime_payload,
                )
            )
    shard = pd.concat(outer_frames, ignore_index=True)
    SequencePredictionBundle.validate(
        shard,
        expected_sample_ids=outer_validation_ids,
        expected_model_id=spec.model_id,
        expected_fold_id=fold_id,
    )
    output_root = root / "artifacts" / "model_selection" / "t1_gate_c1_compact_screen_v1"
    safe_fold = fold_id.replace(":", "_")
    shard_path = output_root / "prediction_shards" / spec.model_id / f"{safe_fold}.csv"
    tuning_path = output_root / "tuning_shards" / spec.model_id / f"{safe_fold}.csv"
    oof_path = output_root / "selected_inner_oof_shards" / spec.model_id / f"{safe_fold}.csv"
    _write_csv_atomic(root, shard_path, shard)
    _write_csv_atomic(root, tuning_path, tuning)
    _write_csv_atomic(root, oof_path, selected_oof)
    status = {
        "schema_version": 1,
        "status": "COMPLETED",
        "job_id": str(job["job_id"]),
        "model_id": spec.model_id,
        "fold_id": fold_id,
        "seeds": list(C1_SEEDS),
        "selected_parameter_sha256": selected_sha,
        "selected_parameter_index": int(selected_index),
        "selected_parameter_json": _canonical_parameter_json(selected_parameters),
        "outer_epoch_count": outer_epochs,
        "logical_inner_evaluations": len(tuning),
        "physical_inner_fits_executed": int((~tuning["physical_fit_reused"].astype(bool)).sum()),
        "physical_inner_fits_reused": int(tuning["physical_fit_reused"].astype(bool).sum()),
        "outer_refits": 5,
        "outer_prediction_rows": len(shard),
        "checkpoint_policy": "top_5_inner_objective_and_outer_fixed_final",
        "checkpoint_persistence_scope": "work_only",
        "outer_checkpoint_manifests": outer_checkpoint_manifests,
        "outer_checkpoint_manifest_count": len(outer_checkpoint_manifests),
        "outer_labels_used_for_checkpoint_selection": False,
        "unlabeled_prediction_shard": shard_path.relative_to(root).as_posix(),
        "unlabeled_prediction_sha256": sha256_file(shard_path),
        "tuning_shard_sha256": sha256_file(tuning_path),
        "selected_inner_oof_sha256": sha256_file(oof_path),
        "historical_validation_loaded": False,
        "current_test_loaded": False,
        "new_holdout_seen": False,
        "outer_validation_labels_loaded_by_worker": False,
        "runtime_network_allowed": False,
    }
    status_path = output_root / "worker_status" / spec.model_id / f"{safe_fold}.json"
    _write_json_atomic(root, status_path, status)
    return status


def _fit_inner(
    *,
    root: Path,
    fit_id: str,
    spec: SequenceModelSpec,
    parameters: Mapping[str, Any],
    seed: int,
    train_ids: tuple[str, ...],
    validation_ids: tuple[str, ...],
    train_values: np.ndarray,
    validation_values: np.ndarray,
    sequence_rows: pd.DataFrame,
    contract: Mapping[str, Any],
    runtime_payload: Mapping[str, Any],
    device: torch.device,
) -> tuple[dict[str, Any], pd.DataFrame]:
    configure_determinism(seed)
    context = C1FitContext(
        fold_id=str(contract["fold_id"]),
        role="train",
        source_split="t1_v1/train",
        sample_ids_sha256=ordered_sample_hash(train_ids),
        sequence_pairs_sha256=str(contract["train_sequence_pairs_sha256"]),
        target_sha256=target_values_sha256(train_ids, train_values),
        seed=seed,
    )
    preprocessor = C1SequencePreprocessor().fit(sequence_rows, sample_ids=train_ids, context=context)
    train_batch = preprocessor.transform(sequence_rows, sample_ids=train_ids)
    validation_batch = preprocessor.transform(sequence_rows, sample_ids=validation_ids)
    target_scaler = SequenceTargetScaler().fit(train_values, context=context)
    target_train = target_scaler.transform(train_values)
    target_validation = target_scaler.transform(validation_values)
    model = create_sequence_model(spec.model_id, parameters, input_size=train_batch.x.shape[2]).to(device)
    parameter_count = model_parameter_count(model)
    if parameter_count > spec.parameter_count_limit:
        raise ContractViolation(f"Gate C1 parameter-count limit exceeded: {parameter_count}")
    checkpoint_manager = TopKCheckpointManager(
        root=root,
        fit_id=fit_id,
        role="inner",
        keep_top_k=int(runtime_payload["checkpointing"]["keep_top_k"]),
        stage_interval_epochs=int(
            runtime_payload["checkpointing"]["stage_interval_epochs"]
        ),
        provenance={
            "model_id": spec.model_id,
            "fold_id": str(contract["fold_id"]),
            "seed": int(seed),
            "code_sha256": runtime_payload["code_sha256"],
            "config_sha256": runtime_payload["config_sha256"],
            "environment_sha256": runtime_payload["environment_sha256"],
            "training_role": "inner_train_with_forward_validation",
        },
    )
    result = _train_with_early_stopping(
        model=model,
        train_batch=train_batch,
        validation_batch=validation_batch,
        train_targets=target_train,
        validation_targets=target_validation,
        target_scaler=target_scaler,
        probabilistic=spec.probabilistic,
        weight_decay=float(parameters["weight_decay"]),
        seed=seed,
        device=device,
        max_epochs=int(runtime_payload["training"]["max_epochs"]),
        patience=int(runtime_payload["training"]["patience"]),
        batch_size=int(runtime_payload["training"]["batch_size"]),
        learning_rate=float(runtime_payload["training"]["learning_rate"]),
        gradient_clip_norm=float(runtime_payload["training"]["gradient_clip_norm"]),
        checkpoint_manager=checkpoint_manager,
        fused_optimizer=runtime_payload["training"]["optimizer_backend"]
        == "fused_adamw_cuda",
    )
    prediction = result.pop("prediction")
    prediction_frame = pd.DataFrame({"sample_id": validation_ids, **prediction})
    absolute_error = np.abs(prediction_frame["y_pred"].to_numpy(float) - validation_values)
    crps = (
        quantile_grid_crps(
            validation_values,
            prediction_frame["distribution_loc"].to_numpy(float),
            prediction_frame["distribution_scale"].to_numpy(float),
            prediction_frame["distribution_df"].to_numpy(float),
        )
        if spec.probabilistic
        else np.full(len(validation_values), np.nan)
    )
    nll = (
        student_t_nll(
            validation_values,
            prediction_frame["distribution_loc"].to_numpy(float),
            prediction_frame["distribution_scale"].to_numpy(float),
            prediction_frame["distribution_df"].to_numpy(float),
        )
        if spec.probabilistic
        else np.full(len(validation_values), np.nan)
    )
    prediction_frame["absolute_error"] = absolute_error
    prediction_frame["crps"] = crps
    prediction_frame["nll"] = nll
    result.update(
        {
            "status": "COMPLETED",
            "rows": len(validation_values),
            "absolute_error_sum": float(np.sum(absolute_error)),
            "mae": float(np.mean(absolute_error)),
            "crps_sum": float(np.nansum(crps)) if spec.probabilistic else np.nan,
            "crps": float(np.mean(crps)) if spec.probabilistic else np.nan,
            "nll_sum": float(np.nansum(nll)) if spec.probabilistic else np.nan,
            "nll": float(np.mean(nll)) if spec.probabilistic else np.nan,
            "parameter_count": parameter_count,
            "preprocessing_state_sha256": preprocessor.state_sha256,
            "target_scaler_state_sha256": target_scaler.state_sha256,
        }
    )
    return result, prediction_frame


def _fit_outer(
    *,
    root: Path,
    fit_id: str,
    spec: SequenceModelSpec,
    parameters: Mapping[str, Any],
    seed: int,
    epochs: int,
    train_ids: tuple[str, ...],
    validation_ids: tuple[str, ...],
    train_values: np.ndarray,
    sequence_rows: pd.DataFrame,
    contract: Mapping[str, Any],
    runtime_payload: Mapping[str, Any],
    device: torch.device,
) -> dict[str, Any]:
    configure_determinism(seed)
    context = C1FitContext(
        fold_id=str(contract["fold_id"]),
        role="train",
        source_split="t1_v1/train",
        sample_ids_sha256=ordered_sample_hash(train_ids),
        sequence_pairs_sha256=str(contract["train_sequence_pairs_sha256"]),
        target_sha256=target_values_sha256(train_ids, train_values),
        seed=seed,
    )
    preprocessor = C1SequencePreprocessor().fit(sequence_rows, sample_ids=train_ids, context=context)
    train_batch = preprocessor.transform(sequence_rows, sample_ids=train_ids)
    validation_batch = preprocessor.transform(sequence_rows, sample_ids=validation_ids)
    target_scaler = SequenceTargetScaler().fit(train_values, context=context)
    target_train = target_scaler.transform(train_values)
    model = create_sequence_model(spec.model_id, parameters, input_size=train_batch.x.shape[2]).to(device)
    parameter_count = model_parameter_count(model)
    if parameter_count > spec.parameter_count_limit:
        raise ContractViolation(f"Gate C1 parameter-count limit exceeded: {parameter_count}")
    started = perf_counter()
    _reset_peak_vram(device)
    checkpoint_manager = TopKCheckpointManager(
        root=root,
        fit_id=fit_id,
        role="outer",
        keep_top_k=int(runtime_payload["checkpointing"]["keep_top_k"]),
        stage_interval_epochs=int(
            runtime_payload["checkpointing"]["stage_interval_epochs"]
        ),
        provenance={
            "model_id": spec.model_id,
            "fold_id": str(contract["fold_id"]),
            "seed": int(seed),
            "fixed_epochs": int(epochs),
            "code_sha256": runtime_payload["code_sha256"],
            "config_sha256": runtime_payload["config_sha256"],
            "environment_sha256": runtime_payload["environment_sha256"],
            "training_role": "outer_train_fixed_epoch_refit",
        },
    )
    checkpoint_summary = _train_fixed_epochs(
        model=model,
        batch=train_batch,
        targets=target_train,
        probabilistic=spec.probabilistic,
        weight_decay=float(parameters["weight_decay"]),
        seed=seed,
        device=device,
        epochs=epochs,
        batch_size=int(runtime_payload["training"]["batch_size"]),
        learning_rate=float(runtime_payload["training"]["learning_rate"]),
        gradient_clip_norm=float(runtime_payload["training"]["gradient_clip_norm"]),
        checkpoint_manager=checkpoint_manager,
        fused_optimizer=runtime_payload["training"]["optimizer_backend"]
        == "fused_adamw_cuda",
    )
    fit_seconds = perf_counter() - started
    prediction_started = perf_counter()
    prediction = _predict(model, validation_batch, target_scaler, spec.probabilistic, device)
    inference_seconds = perf_counter() - prediction_started
    return {
        "predictions": prediction,
        "fit_seconds": fit_seconds,
        "inference_seconds": inference_seconds,
        "peak_ram_mb": _resident_memory_mb(),
        "peak_vram_mb": _peak_vram_mb(device),
        "parameter_count": parameter_count,
        **checkpoint_summary,
    }


def _train_with_early_stopping(
    *,
    model: torch.nn.Module,
    train_batch,
    validation_batch,
    train_targets: np.ndarray,
    validation_targets: np.ndarray,
    target_scaler: SequenceTargetScaler,
    probabilistic: bool,
    weight_decay: float,
    seed: int,
    device: torch.device,
    max_epochs: int,
    patience: int,
    batch_size: int,
    learning_rate: float,
    gradient_clip_norm: float,
    checkpoint_manager: TopKCheckpointManager,
    fused_optimizer: bool,
) -> dict[str, Any]:
    optimizer = _create_optimizer(
        model,
        learning_rate=learning_rate,
        weight_decay=weight_decay,
        device=device,
        fused_optimizer=fused_optimizer,
    )
    tensors = _torch_batch(train_batch, device)
    target_tensor = torch.as_tensor(train_targets, dtype=torch.float32, device=device)
    validation_tensors = _torch_batch(validation_batch, device)
    validation_target = torch.as_tensor(validation_targets, dtype=torch.float32, device=device)
    generator = torch.Generator(device=device).manual_seed(int(seed))
    best_metric = float("inf")
    best_epoch = 0
    stale = 0
    started = perf_counter()
    _reset_peak_vram(device)
    recovered = checkpoint_manager.resume(
        model=model,
        optimizer=optimizer,
        shuffle_generator=generator,
        device=device,
    )
    start_epoch = 1
    terminal_recovery = False
    if recovered is not None:
        start_epoch = int(recovered["epoch"]) + 1
        stale = int(recovered["stale_epochs"])
        terminal_recovery = bool(recovered["terminal"])
        if checkpoint_manager.ranked:
            best_metric = float(checkpoint_manager.ranked[0]["metric"])
            best_epoch = int(checkpoint_manager.ranked[0]["epoch"])
    epoch = start_epoch - 1
    if terminal_recovery:
        epoch = int(recovered["epoch"])
    for epoch in range(start_epoch, max_epochs + 1) if not terminal_recovery else ():
        _training_epoch(
            model,
            optimizer,
            tensors,
            target_tensor,
            probabilistic=probabilistic,
            batch_size=batch_size,
            generator=generator,
            gradient_clip_norm=gradient_clip_norm,
        )
        model.eval()
        with torch.inference_mode():
            output = model(**validation_tensors)
            if probabilistic:
                metric_tensor = student_t_nll_loss(output, validation_target)
            else:
                metric_tensor = torch.mean(torch.abs(output.point - validation_target)) * float(
                    target_scaler.scale_
                )
            metric = float(metric_tensor.item())
        if not np.isfinite(metric):
            raise FloatingPointError("Non-finite Gate C1 early-stopping metric")
        if metric < best_metric:
            best_metric = metric
            best_epoch = epoch
            stale = 0
        else:
            stale += 1
        terminal = stale >= patience or epoch == max_epochs
        checkpoint_manager.observe(
            epoch=epoch,
            metric=metric,
            model=model,
            optimizer=optimizer,
            shuffle_generator=generator,
            stale_epochs=stale,
            device=device,
            terminal=terminal,
        )
        if terminal:
            break
    if not checkpoint_manager.ranked or best_epoch <= 0:
        raise FloatingPointError("Gate C1 training did not produce a finite checkpoint")
    checkpoint_summary = checkpoint_manager.finalize(model=model)
    if int(checkpoint_summary["selected_checkpoint_epoch"]) != int(best_epoch):
        raise ContractViolation("Gate C1 best epoch and selected checkpoint diverged")
    prediction_started = perf_counter()
    prediction = _predict(model, validation_batch, target_scaler, probabilistic, device)
    inference_seconds = perf_counter() - prediction_started
    return {
        "best_epoch": int(best_epoch),
        "epochs_completed": int(epoch),
        "early_stopping_metric": float(best_metric),
        "fit_seconds": perf_counter() - started,
        "inference_seconds": inference_seconds,
        "peak_ram_mb": _resident_memory_mb(),
        "peak_vram_mb": _peak_vram_mb(device),
        "prediction": prediction,
        **checkpoint_summary,
    }


def _train_fixed_epochs(
    *,
    model: torch.nn.Module,
    batch,
    targets: np.ndarray,
    probabilistic: bool,
    weight_decay: float,
    seed: int,
    device: torch.device,
    epochs: int,
    batch_size: int,
    learning_rate: float,
    gradient_clip_norm: float,
    checkpoint_manager: TopKCheckpointManager,
    fused_optimizer: bool,
) -> dict[str, Any]:
    optimizer = _create_optimizer(
        model,
        learning_rate=learning_rate,
        weight_decay=weight_decay,
        device=device,
        fused_optimizer=fused_optimizer,
    )
    tensors = _torch_batch(batch, device)
    target_tensor = torch.as_tensor(targets, dtype=torch.float32, device=device)
    generator = torch.Generator(device=device).manual_seed(int(seed))
    recovered = checkpoint_manager.resume(
        model=model,
        optimizer=optimizer,
        shuffle_generator=generator,
        device=device,
    )
    start_epoch = 1 if recovered is None else int(recovered["epoch"]) + 1
    terminal_recovery = bool(recovered["terminal"]) if recovered is not None else False
    for epoch in range(start_epoch, int(epochs) + 1) if not terminal_recovery else ():
        _training_epoch(
            model,
            optimizer,
            tensors,
            target_tensor,
            probabilistic=probabilistic,
            batch_size=batch_size,
            generator=generator,
            gradient_clip_norm=gradient_clip_norm,
        )
        checkpoint_manager.observe(
            epoch=epoch,
            metric=float(epoch),
            model=model,
            optimizer=optimizer,
            shuffle_generator=generator,
            stale_epochs=0,
            device=device,
            terminal=epoch == int(epochs),
        )
    summary = checkpoint_manager.finalize(model=model)
    if int(summary["selected_checkpoint_epoch"]) != int(epochs):
        raise ContractViolation("Outer checkpoint selection changed the fixed epoch rule")
    return summary


def _training_epoch(
    model: torch.nn.Module,
    optimizer: torch.optim.Optimizer,
    tensors: Mapping[str, torch.Tensor],
    targets: torch.Tensor,
    *,
    probabilistic: bool,
    batch_size: int,
    generator: torch.Generator,
    gradient_clip_norm: float,
) -> None:
    model.train()
    order = torch.randperm(len(targets), generator=generator, device=targets.device)
    for start in range(0, len(targets), int(batch_size)):
        indices = order[start : start + int(batch_size)]
        optimizer.zero_grad(set_to_none=True)
        output = model(**{key: value[indices] for key, value in tensors.items()})
        loss = student_t_nll_loss(output, targets[indices]) if probabilistic else point_huber_loss(
            output, targets[indices]
        )
        if not torch.isfinite(loss):
            raise FloatingPointError("Non-finite Gate C1 training loss")
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), float(gradient_clip_norm))
        optimizer.step()


def _predict(
    model: torch.nn.Module,
    batch,
    scaler: SequenceTargetScaler,
    probabilistic: bool,
    device: torch.device,
) -> dict[str, np.ndarray]:
    model.eval()
    with torch.inference_mode():
        output: ModelOutput = model(**_torch_batch(batch, device))
    point = scaler.inverse_transform(output.point.detach().cpu().numpy())
    result: dict[str, np.ndarray] = {"y_pred": point}
    if probabilistic:
        loc = scaler.inverse_transform(output.loc.detach().cpu().numpy())
        scale = scaler.inverse_scale(output.scale.detach().cpu().numpy())
        df = output.df.detach().cpu().numpy().astype(float)
        levels = np.asarray([0.025, 0.10, 0.25, 0.50, 0.75, 0.90, 0.975])
        quantiles = student_t_quantiles(loc, scale, df, levels)
        result.update(
            {
                "distribution_family": np.repeat("student_t", len(point)),
                "distribution_loc": loc,
                "distribution_scale": scale,
                "distribution_df": df,
                "q025": quantiles[:, 0],
                "q10": quantiles[:, 1],
                "q25": quantiles[:, 2],
                "q50": quantiles[:, 3],
                "q75": quantiles[:, 4],
                "q90": quantiles[:, 5],
                "q975": quantiles[:, 6],
            }
        )
    return result


def _torch_batch(batch, device: torch.device) -> dict[str, torch.Tensor]:
    return {
        "x": torch.as_tensor(batch.x, dtype=torch.float32, device=device),
        "lengths": torch.as_tensor(batch.lengths, dtype=torch.int64, device=device),
        "padding_mask": torch.as_tensor(batch.padding_mask, dtype=torch.float32, device=device),
        "observation_mask": torch.as_tensor(batch.observation_mask, dtype=torch.float32, device=device),
        "missing_campaign_mask": torch.as_tensor(
            batch.missing_campaign_mask, dtype=torch.float32, device=device
        ),
    }


def _create_optimizer(
    model: torch.nn.Module,
    *,
    learning_rate: float,
    weight_decay: float,
    device: torch.device,
    fused_optimizer: bool,
) -> torch.optim.Optimizer:
    if fused_optimizer and device.type != "cuda":
        raise ContractViolation("Fused Gate C1 optimizer is authorized only on CUDA")
    return torch.optim.AdamW(
        model.parameters(),
        lr=float(learning_rate),
        weight_decay=float(weight_decay),
        fused=bool(fused_optimizer),
    )


def _select_parameters(
    spec: SequenceModelSpec, tuning: pd.DataFrame
) -> tuple[Mapping[str, Any], str, int]:
    aggregates = []
    for parameter_sha, frame in tuning.groupby("parameter_sha256", sort=True):
        if len(frame) != 15 or set(frame["seed"].astype(int)) != set(C1_SEEDS):
            raise ContractViolation("Gate C1 parameter evaluation is incomplete")
        rows = int(frame["rows"].sum())
        mae = float(frame["absolute_error_sum"].sum() / rows)
        crps = float(frame["crps_sum"].sum() / rows) if spec.probabilistic else np.nan
        aggregates.append(
            {
                "parameter_sha256": parameter_sha,
                "parameter_index": int(frame["parameter_index"].iloc[0]),
                "parameter_json": str(frame["parameter_json"].iloc[0]),
                "inner_mae": mae,
                "inner_crps": crps,
                "parameter_count": int(frame["parameter_count"].max()),
            }
        )
    ranked = pd.DataFrame(aggregates)
    columns = ["inner_crps", "inner_mae", "parameter_count", "parameter_json"] if spec.probabilistic else [
        "inner_mae",
        "parameter_count",
        "parameter_json",
    ]
    ranked = ranked.sort_values(columns, kind="mergesort").reset_index(drop=True)
    selected = ranked.iloc[0]
    parameters = json.loads(str(selected["parameter_json"]))
    return parameters, str(selected["parameter_sha256"]), int(selected["parameter_index"])


def _selected_inner_oof(
    selected_tuning: pd.DataFrame,
    prediction_cache: Mapping[tuple[str, int], pd.DataFrame],
    *,
    target_map: pd.Series,
    spec: SequenceModelSpec,
    outer_fold_id: str,
) -> pd.DataFrame:
    frames = []
    for record in selected_tuning.to_dict("records"):
        prediction = prediction_cache[(str(record["fit_cache_key"]), int(record["seed"]))].copy()
        prediction.insert(0, "outer_fold_id", outer_fold_id)
        prediction.insert(1, "inner_fold_id", str(record["inner_fold_id"]))
        prediction.insert(2, "model_id", spec.model_id)
        prediction.insert(3, "seed", int(record["seed"]))
        prediction.insert(4, "parameter_sha256", str(record["parameter_sha256"]))
        prediction["y_true"] = target_map.loc[prediction["sample_id"].astype(str)].to_numpy(float)
        prediction["provenance_role"] = "inner_validation_within_outer_train"
        frames.append(prediction)
    return pd.concat(frames, ignore_index=True)


def _outer_prediction_frame(
    *,
    spec: SequenceModelSpec,
    fold_id: str,
    seed: int,
    validation_ids: tuple[str, ...],
    predictions: Mapping[str, np.ndarray],
    selected_parameters: Mapping[str, Any],
    selected_parameter_sha256: str,
    epochs: int,
    parameter_count: int,
    fit_seconds: float,
    inference_seconds: float,
    peak_ram_mb: float,
    peak_vram_mb: float,
    runtime_payload: Mapping[str, Any],
) -> pd.DataFrame:
    rows = pd.DataFrame({"sample_id": validation_ids, **predictions})
    rows.insert(0, "model_id", spec.model_id)
    rows.insert(1, "family", spec.family)
    rows.insert(2, "fold_id", fold_id)
    rows.insert(3, "seed", int(seed))
    rows["environment_id"] = "gate_c_torch"
    rows["model_spec_sha256"] = spec.spec_sha256
    rows["config_sha256"] = runtime_payload["config_sha256"]
    rows["code_sha256"] = runtime_payload["code_sha256"]
    rows["environment_sha256"] = runtime_payload["environment_sha256"]
    rows["expected_sample_ids_sha256"] = ordered_sample_hash(validation_ids)
    rows["selected_parameter_sha256"] = selected_parameter_sha256
    rows["selected_parameter_json"] = _canonical_parameter_json(selected_parameters)
    rows["epoch_count"] = int(epochs)
    rows["parameter_count"] = int(parameter_count)
    rows["fit_seconds"] = fit_seconds
    rows["inference_seconds"] = inference_seconds
    rows["peak_ram_mb"] = peak_ram_mb
    rows["peak_vram_mb"] = peak_vram_mb
    rows["aggregation"] = "single_seed"
    required = list(runtime_payload["prediction_required_columns"])
    optional = [name for name in runtime_payload["prediction_optional_columns"] if name in rows]
    return rows.loc[:, required + optional]


def _fit_cache_key(
    *,
    spec: SequenceModelSpec,
    parameters: Mapping[str, Any],
    seed: int,
    train_ids: tuple[str, ...],
    validation_ids: tuple[str, ...],
    contract: Mapping[str, Any],
    train_target_sha: str,
    validation_target_sha: str,
    runtime_payload: Mapping[str, Any],
) -> str:
    payload = {
        "model_spec_sha256": spec.spec_sha256,
        "parameter_sha256": canonical_json_sha256(parameters),
        "seed": int(seed),
        "train_sample_ids_sha256": ordered_sample_hash(train_ids),
        "validation_sample_ids_sha256": ordered_sample_hash(validation_ids),
        "train_sequence_pairs_sha256": str(contract["train_sequence_pairs_sha256"]),
        "validation_sequence_pairs_sha256": str(contract["validation_sequence_pairs_sha256"]),
        "train_target_sha256": train_target_sha,
        "validation_target_sha256": validation_target_sha,
        "preprocessing_contract_sha256": runtime_payload["preprocessing_contract_sha256"],
        "code_sha256": runtime_payload["code_sha256"],
        "environment_sha256": runtime_payload["environment_sha256"],
    }
    return canonical_json_sha256(payload)


def _read_cache(root: Path, cache_key: str) -> tuple[dict[str, Any], pd.DataFrame] | None:
    cache_root = root / "work" / "gate_c1" / "inner_cache"
    metadata_path = cache_root / f"{cache_key}.json"
    prediction_path = cache_root / f"{cache_key}.csv"
    if not metadata_path.exists() and not prediction_path.exists():
        return None
    if not metadata_path.is_file() or not prediction_path.is_file():
        raise ContractViolation("Gate C1 cache record is partial")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata.get("fit_cache_key") != cache_key:
        raise ContractViolation("Gate C1 cache key mismatch")
    if metadata.get("prediction_sha256") != sha256_file(prediction_path):
        raise ContractViolation("Gate C1 cached prediction hash mismatch")
    prediction = pd.read_csv(prediction_path)
    result = dict(metadata["fit_result"])
    validate_checkpoint_manifest(
        root,
        str(result.get("checkpoint_manifest", "")),
        str(result.get("checkpoint_manifest_sha256", "")),
        expected_role="inner",
    )
    return result, prediction


def _write_cache(root: Path, cache_key: str, result: Mapping[str, Any], prediction: pd.DataFrame) -> None:
    cache_root = root / "work" / "gate_c1" / "inner_cache"
    cache_root.mkdir(parents=True, exist_ok=True)
    prediction_path = cache_root / f"{cache_key}.csv"
    metadata_path = cache_root / f"{cache_key}.json"
    _write_csv_atomic(root, prediction_path, prediction)
    payload = {
        "schema_version": 1,
        "fit_cache_key": cache_key,
        "source_fit_id": cache_key,
        "prediction_sha256": sha256_file(prediction_path),
        "fit_result": dict(result),
    }
    _write_json_atomic(root, metadata_path, payload)


def _role_ids(assignments: pd.DataFrame, fold_id: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    fold = assignments.loc[assignments["fold_id"].astype(str).eq(str(fold_id))]
    train = tuple(fold.loc[fold["role"].eq("train"), "sample_id"].astype(str))
    validation = tuple(fold.loc[fold["role"].eq("validation"), "sample_id"].astype(str))
    if not train or not validation or set(train) & set(validation):
        raise ContractViolation(f"Invalid Gate C1 frozen fold: {fold_id}")
    return train, validation


def _fold_contract(fold_contracts: pd.DataFrame, fold_id: str, *, level: str) -> dict[str, Any]:
    row = fold_contracts.loc[
        fold_contracts["fold_id"].astype(str).eq(str(fold_id))
        & fold_contracts["level"].astype(str).eq(level)
    ]
    if len(row) != 1 or str(row.iloc[0]["design"]) != "rolling_origin":
        raise ContractViolation(f"Missing Gate C1 rolling {level} contract: {fold_id}")
    return row.iloc[0].to_dict()


def _targets_for(target_map: pd.Series, sample_ids: Sequence[str]) -> np.ndarray:
    missing = set(sample_ids) - set(target_map.index.astype(str))
    if missing:
        raise ContractViolation("Worker attempted to access targets outside staged outer train")
    values = pd.to_numeric(target_map.loc[list(sample_ids)], errors="raise").to_numpy(float)
    if not np.isfinite(values).all():
        raise ContractViolation("Gate C1 target subset is non-finite")
    return values


def _resolve_work_path(root: Path, relative: str) -> Path:
    path = (root / Path(relative)).resolve()
    work_root = (root / "work").resolve()
    try:
        path.relative_to(work_root)
    except ValueError as exc:
        raise ContractViolation("Gate C1 staged worker input must stay under work/") from exc
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def _canonical_parameter_json(parameters: Mapping[str, Any]) -> str:
    return json.dumps(parameters, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _resident_memory_mb() -> float:
    return float(psutil.Process().memory_info().rss / (1024**2))


def _reset_peak_vram(device: torch.device) -> None:
    torch.cuda.reset_peak_memory_stats(device)


def _peak_vram_mb(device: torch.device) -> float:
    torch.cuda.synchronize(device)
    return float(torch.cuda.max_memory_allocated(device) / (1024**2))


def _write_csv_atomic(root: Path, path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = root / "work" / "gate_c1" / "atomic" / f"{path.name}.{uuid4().hex}.tmp"
    temporary.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(temporary, index=False, lineterminator="\n")
    temporary.replace(path)


def _write_json_atomic(root: Path, path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = root / "work" / "gate_c1" / "atomic" / f"{path.name}.{uuid4().hex}.tmp"
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=_json_default) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _json_default(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Path):
        return value.as_posix()
    raise TypeError(type(value).__name__)

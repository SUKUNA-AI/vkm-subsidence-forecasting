#!/usr/bin/env python
"""CUDA import/fit/predict/serialization and determinism checks for Gate C1."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from time import perf_counter

import numpy as np
import pandas as pd
import psutil
import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from skru1.gate_c1_interfaces import C1_REQUIRED_MODELS, C1_SEEDS, SequenceModelSpec
from skru1.gate_c1_checkpoints import TopKCheckpointManager, validate_checkpoint_manifest
from skru1.gate_c1_models import (
    create_sequence_model,
    model_parameter_count,
    point_huber_loss,
    student_t_nll_loss,
)
from skru1.gate_c1_worker import configure_determinism, runtime_network_guard
from skru1.gate_c1_worker import _fit_inner
from skru1.splits import load_split_dataset


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = Path(args.output_root).resolve()
    output.mkdir(parents=True, exist_ok=True)
    registry = json.loads(
        (
            root
            / "artifacts"
            / "model_selection"
            / "t1_gate_c1_compact_screen_v1"
            / "model_registry.json"
        ).read_text(encoding="utf-8")
    )
    specs = [SequenceModelSpec.from_dict(item) for item in registry["models"]]
    if tuple(spec.model_id for spec in specs) != C1_REQUIRED_MODELS:
        raise RuntimeError("Gate C1 smoke registry differs from frozen four-model set")
    sequence_rows = pd.read_csv(
        root / "artifacts" / "splits" / "t1_train_gate_c_v1" / "sequence_rows.csv"
    )
    categories = int(
        sequence_rows.loc[sequence_rows["padding_mask"].eq(0), "current_campaign_type"]
        .astype("string")
        .nunique()
    )
    input_size = 5 + categories + 1
    hardware = hardware_report()
    if not hardware["cuda_available"]:
        raise RuntimeError("Gate C1 CUDA smoke requires an available GPU")
    if hardware["torch_version"] != "2.13.0+cu130" or hardware["torch_cuda_version"] != "13.0":
        raise RuntimeError("Gate C1 torch/CUDA version mismatch")
    if hardware["gpu_name"] != "NVIDIA GeForce RTX 5070 Ti":
        raise RuntimeError("Gate C1 GPU identity mismatch")
    device = torch.device("cuda:0")
    batch = synthetic_batch(input_size)
    parameter_rows = []
    smoke_rows = []
    determinism_rows = []
    with runtime_network_guard():
        for spec in specs:
            for parameter_index, parameters in enumerate(spec.parameter_grid):
                configure_determinism(C1_SEEDS[0])
                model = create_sequence_model(
                    spec.model_id, parameters, input_size=input_size
                ).to(device)
                count = model_parameter_count(model)
                parameter_rows.append(
                    {
                        "model_id": spec.model_id,
                        "parameter_index": parameter_index,
                        "parameter_count": count,
                        "within_limit": count <= spec.parameter_count_limit,
                    }
                )
            first_parameters = spec.parameter_grid[0]
            started = perf_counter()
            first_prediction, first_state = smoke_fit(
                spec, first_parameters, batch, device=device, seed=C1_SEEDS[0]
            )
            state_path = output / f"{spec.model_id}.smoke_state.pt"
            torch.save(first_state, state_path)
            restored = create_sequence_model(spec.model_id, first_parameters, input_size=input_size).to(
                device
            )
            restored.load_state_dict(torch.load(state_path, map_location=device, weights_only=True))
            restored.eval()
            with torch.inference_mode():
                roundtrip = restored(**batch_inputs(batch, device)).point.detach().cpu().numpy()
            state_path.unlink()
            second_prediction, _ = smoke_fit(
                spec, first_parameters, batch, device=device, seed=C1_SEEDS[0]
            )
            roundtrip_delta = float(np.max(np.abs(first_prediction - roundtrip)))
            determinism_delta = float(np.max(np.abs(first_prediction - second_prediction)))
            smoke_rows.append(
                {
                    "model_id": spec.model_id,
                    "status": "PASS"
                    if np.isfinite(first_prediction).all() and roundtrip_delta <= 1.0e-6
                    else "FAIL",
                    "finite_predictions": bool(np.isfinite(first_prediction).all()),
                    "serialization_roundtrip": bool(roundtrip_delta <= 1.0e-6),
                    "serialization_max_abs_delta": roundtrip_delta,
                    "elapsed_seconds": perf_counter() - started,
                }
            )
            determinism_rows.append(
                {
                    "model_id": spec.model_id,
                    "status": "PASS" if determinism_delta <= 1.0e-6 else "FAIL",
                    "maximum_absolute_delta": determinism_delta,
                    "tolerance": 1.0e-6,
                    "runs": 2,
                }
            )
    parameter_frame = pd.DataFrame(parameter_rows)
    checkpoint_roundtrip = checkpoint_smoke(specs[0], batch, device=device, parent=root / "work")
    real_train_only_inner_flow = real_train_only_inner_smoke(
        root,
        specs[0],
        sequence_rows,
        device=device,
    )
    smoke = {
        "schema_version": 1,
        "status": "PASS"
        if all(row["status"] == "PASS" for row in smoke_rows)
        and parameter_frame["within_limit"].all()
        else "FAIL",
        "environment_id": "gate_c_torch",
        "input_channels": input_size,
        "grid_configurations_checked": len(parameter_frame),
        "all_parameter_counts_lte_100000": bool(parameter_frame["within_limit"].all()),
        "maximum_parameter_count": int(parameter_frame["parameter_count"].max()),
        "models": smoke_rows,
        "external_pretrained_dependencies": False,
        "runtime_network_allowed": False,
        "fused_adamw_cuda": True,
        "recurrent_execution": "vectorized_right_padding_dense_cuda",
        "validation_metric_device": "cuda",
        "torch_compile": False,
        "checkpoint_roundtrip": checkpoint_roundtrip,
        "real_train_only_inner_flow": real_train_only_inner_flow,
    }
    determinism = {
        "schema_version": 1,
        "status": "PASS" if all(row["status"] == "PASS" for row in determinism_rows) else "FAIL",
        "environment_id": "gate_c_torch",
        "tolerance": 1.0e-6,
        "models": determinism_rows,
        "deterministic_algorithms": True,
        "cublas_workspace_config": os.environ.get("CUBLAS_WORKSPACE_CONFIG"),
        "tf32": False,
        "mixed_precision": False,
    }
    write_json(output / "hardware_report.json", hardware)
    write_json(output / "smoke_report.json", smoke)
    write_json(output / "determinism_report.json", determinism)
    print(json.dumps({"smoke": smoke["status"], "determinism": determinism["status"]}))
    return 0 if smoke["status"] == determinism["status"] == "PASS" else 2


def synthetic_batch(input_size: int) -> dict[str, np.ndarray]:
    generator = np.random.default_rng(42117)
    rows = 24
    length = 16
    lengths = np.asarray([3 + index % 14 for index in range(rows)], dtype=np.int64)
    x = generator.normal(size=(rows, length, input_size)).astype(np.float32)
    padding = np.ones((rows, length), dtype=np.float32)
    observation = np.zeros((rows, length), dtype=np.float32)
    missing = np.zeros((rows, length), dtype=np.float32)
    for index, valid in enumerate(lengths):
        padding[index, -valid:] = 0
        observation[index, -valid:] = 1
        if index % 3 == 0:
            missing[index, -1] = 1
        x[index, : length - valid] = 0
    targets = generator.normal(size=rows).astype(np.float32)
    return {
        "x": x,
        "lengths": lengths,
        "padding_mask": padding,
        "observation_mask": observation,
        "missing_campaign_mask": missing,
        "targets": targets,
    }


def batch_inputs(batch: dict[str, np.ndarray], device: torch.device) -> dict[str, torch.Tensor]:
    return {
        "x": torch.as_tensor(batch["x"], dtype=torch.float32, device=device),
        "lengths": torch.as_tensor(batch["lengths"], dtype=torch.int64, device=device),
        "padding_mask": torch.as_tensor(batch["padding_mask"], dtype=torch.float32, device=device),
        "observation_mask": torch.as_tensor(batch["observation_mask"], dtype=torch.float32, device=device),
        "missing_campaign_mask": torch.as_tensor(
            batch["missing_campaign_mask"], dtype=torch.float32, device=device
        ),
    }


def smoke_fit(
    spec: SequenceModelSpec,
    parameters: dict,
    batch: dict[str, np.ndarray],
    *,
    device: torch.device,
    seed: int,
) -> tuple[np.ndarray, dict[str, torch.Tensor]]:
    configure_determinism(seed)
    model = create_sequence_model(spec.model_id, parameters, input_size=batch["x"].shape[2]).to(device)
    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=0.001,
        weight_decay=float(parameters["weight_decay"]),
        fused=True,
    )
    inputs = batch_inputs(batch, device)
    targets = torch.as_tensor(batch["targets"], dtype=torch.float32, device=device)
    for _ in range(3):
        optimizer.zero_grad(set_to_none=True)
        output = model(**inputs)
        loss = student_t_nll_loss(output, targets) if spec.probabilistic else point_huber_loss(
            output, targets
        )
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        optimizer.step()
    model.eval()
    with torch.inference_mode():
        prediction = model(**inputs).point.detach().cpu().numpy()
    state = {name: value.detach().cpu().clone() for name, value in model.state_dict().items()}
    return prediction, state


def checkpoint_smoke(
    spec: SequenceModelSpec,
    batch: dict[str, np.ndarray],
    *,
    device: torch.device,
    parent: Path,
) -> bool:
    """Exercise full-state top-five persistence and terminal restoration."""

    fake_root = parent / "gate_c1_checkpoint_smoke_root"
    if fake_root.exists():
        shutil.rmtree(fake_root)
    fake_root.mkdir(parents=True)
    try:
        configure_determinism(C1_SEEDS[0])
        parameters = spec.parameter_grid[0]
        model = create_sequence_model(spec.model_id, parameters, input_size=batch["x"].shape[2]).to(
            device
        )
        optimizer = torch.optim.AdamW(
            model.parameters(),
            lr=0.001,
            weight_decay=float(parameters["weight_decay"]),
            fused=True,
        )
        generator = torch.Generator(device=device).manual_seed(C1_SEEDS[0])
        manager = TopKCheckpointManager(
            root=fake_root,
            fit_id="a" * 64,
            role="inner",
            keep_top_k=5,
            stage_interval_epochs=2,
            provenance={"model_id": spec.model_id, "fold_id": "smoke", "seed": C1_SEEDS[0]},
        )
        inputs = batch_inputs(batch, device)
        targets = torch.as_tensor(batch["targets"], dtype=torch.float32, device=device)
        for epoch in range(1, 7):
            optimizer.zero_grad(set_to_none=True)
            output = model(**inputs)
            loss = point_huber_loss(output, targets)
            loss.backward()
            optimizer.step()
            manager.observe(
                epoch=epoch,
                metric=float(7 - epoch),
                model=model,
                optimizer=optimizer,
                shuffle_generator=generator,
                stale_epochs=0,
                device=device,
                terminal=epoch == 6,
            )
        summary = manager.finalize(model=model)
        manifest = validate_checkpoint_manifest(
            fake_root,
            summary["checkpoint_manifest"],
            summary["checkpoint_manifest_sha256"],
            expected_role="inner",
        )
        return bool(
            manifest["retained_checkpoint_count"] == 5
            and manifest["selected_epoch"] == 6
            and manifest["latest_stage"]["terminal"] is True
        )
    finally:
        shutil.rmtree(fake_root)


def real_train_only_inner_smoke(
    root: Path,
    spec: SequenceModelSpec,
    sequence_rows: pd.DataFrame,
    *,
    device: torch.device,
) -> bool:
    """Run a six-epoch real inner-fold flow without any outer validation label."""

    fake_root = root / "work" / "gate_c1_real_inner_smoke_root"
    if fake_root.exists():
        shutil.rmtree(fake_root)
    fake_root.mkdir(parents=True)
    try:
        contracts = pd.read_csv(
            root / "artifacts" / "splits" / "t1_train_gate_c_v1" / "fold_sequence_contracts.csv"
        )
        contract_row = contracts.loc[
            contracts["level"].eq("inner") & contracts["design"].eq("rolling_origin")
        ].sort_values("validation_target_date", kind="mergesort").iloc[0]
        fold_id = str(contract_row["fold_id"])
        assignments = pd.read_csv(
            root / "artifacts" / "splits" / "t1_train_benchmark_v1" / "inner_assignments.csv"
        )
        fold = assignments.loc[assignments["fold_id"].astype(str).eq(fold_id)]
        train_ids = tuple(fold.loc[fold["role"].eq("train"), "sample_id"].astype(str))
        validation_ids = tuple(
            fold.loc[fold["role"].eq("validation"), "sample_id"].astype(str)
        )
        source = load_split_dataset("t1", "train", root=root).frame.set_index("sample_id")
        train_values = source.loc[list(train_ids), "observed_rate_mm_y"].to_numpy(float)
        validation_values = source.loc[
            list(validation_ids), "observed_rate_mm_y"
        ].to_numpy(float)
        runtime_payload = {
            "code_sha256": "a" * 64,
            "config_sha256": "b" * 64,
            "environment_sha256": "c" * 64,
            "training": {
                "max_epochs": 6,
                "patience": 5,
                "batch_size": 32,
                "learning_rate": 0.001,
                "gradient_clip_norm": 1.0,
                "optimizer_backend": "fused_adamw_cuda",
            },
            "checkpointing": {"keep_top_k": 5, "stage_interval_epochs": 2},
        }
        with runtime_network_guard():
            result, predictions = _fit_inner(
                root=fake_root,
                fit_id="d" * 64,
                spec=spec,
                parameters=spec.parameter_grid[0],
                seed=C1_SEEDS[0],
                train_ids=train_ids,
                validation_ids=validation_ids,
                train_values=train_values,
                validation_values=validation_values,
                sequence_rows=sequence_rows,
                contract=contract_row.to_dict(),
                runtime_payload=runtime_payload,
                device=device,
            )
        return bool(
            result["status"] == "COMPLETED"
            and result["retained_checkpoint_count"] == 5
            and len(predictions) == len(validation_ids)
            and np.isfinite(predictions["y_pred"].to_numpy(float)).all()
        )
    finally:
        shutil.rmtree(fake_root)


def hardware_report() -> dict:
    query = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=name,driver_version,memory.total,compute_cap",
            "--format=csv,noheader,nounits",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    fields = [value.strip() for value in query.stdout.strip().split(",")] if query.returncode == 0 else []
    return {
        "schema_version": 1,
        "environment_id": "gate_c_torch",
        "python_version": platform.python_version(),
        "os": platform.platform(),
        "cpu": platform.processor(),
        "logical_cpu_count": psutil.cpu_count(logical=True),
        "physical_cpu_count": psutil.cpu_count(logical=False),
        "ram_bytes": int(psutil.virtual_memory().total),
        "torch_version": torch.__version__,
        "torch_cuda_version": torch.version.cuda,
        "cuda_available": bool(torch.cuda.is_available()),
        "gpu_name": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        "gpu_driver_version": fields[1] if len(fields) >= 2 else None,
        "gpu_memory_mib": float(fields[2]) if len(fields) >= 3 else None,
        "gpu_compute_capability": fields[3] if len(fields) >= 4 else None,
        "deterministic_algorithms": True,
        "cudnn_benchmark": False,
        "tf32": False,
        "mixed_precision": False,
    }


def write_json(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    raise SystemExit(main())

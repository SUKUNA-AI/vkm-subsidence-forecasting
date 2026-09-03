"""Gate C1 freeze, preflight, execution, aggregation, and validation runner."""

from __future__ import annotations

from hashlib import sha256
from itertools import product
import json
from pathlib import Path
import subprocess
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd
import yaml

from .artifact_io import artifact_inventory, snapshot_paths, write_csv_atomic, write_json_atomic
from .data_contracts import ContractViolation, discover_project_root, sha256_file
from .gate_c1_evaluation import aggregate_and_score
from .gate_c1_interfaces import (
    C1BenchmarkPlan,
    C1_CATEGORICAL_CHANNELS,
    C1_MASKS,
    C1_NUMERIC_CHANNELS,
    C1_REQUIRED_MODELS,
    C1_SEEDS,
    SequencePredictionBundle,
    SequenceModelSpec,
    assert_train_only_c1_job,
    canonical_json_sha256,
    ordered_sample_hash,
)
from .splits import load_split_dataset


CONFIG_RELATIVE = Path("configs/gate_c1.yaml")
WINDOWS_CUDA_POST_COMMIT_EXIT_CODE = 0xC0000409


def load_gate_c1_config(root: str | Path | None = None) -> tuple[Path, dict[str, Any]]:
    project_root = discover_project_root(root)
    config_path = project_root / CONFIG_RELATIVE
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(config, dict) or config.get("gate") != "C1_COMPACT_SEQUENCE_TEMPORAL_SCREEN":
        raise ContractViolation("configs/gate_c1.yaml is not a Gate C1 mapping")
    _validate_config(project_root, config)
    return project_root, config


def run_gate_c1_freeze(root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    _validate_predecessor_hashes(root, config)
    code_hashes = _code_source_hashes(root, config)
    code_sha = canonical_json_sha256(code_hashes)
    config_sha = sha256_file(root / CONFIG_RELATIVE)
    registry = _model_registry(root, config, code_sha=code_sha, config_sha=config_sha)
    jobs, benchmark = _build_jobs(root, config, registry)
    registry_payload = {
        "schema_version": 1,
        "gate": config["gate"],
        "scientific_scope": "train_only_internal_research",
        "source_split": "t1_v1/train",
        "config_sha256": config_sha,
        "code_sha256": code_sha,
        "models": [spec.to_dict() for spec in registry],
        "model_count": len(registry),
        "configuration_count": sum(len(spec.parameter_grid) for spec in registry),
        "seeds": list(C1_SEEDS),
    }
    registry_payload["registry_sha256"] = canonical_json_sha256(registry_payload)
    job_payload = {
        "schema_version": 1,
        "gate": config["gate"],
        "source_split": "t1_v1/train",
        "execution_boundary": "temporal_screen_only",
        "model_registry_sha256": registry_payload["registry_sha256"],
        "benchmark_plan": benchmark.to_dict(),
        "jobs": jobs,
        "job_count": len(jobs),
        "logical_inner_fits": int(config["cache"]["logical_inner_fits"]),
        "expected_physical_inner_fits": int(config["cache"]["expected_physical_inner_fits"]),
        "worker_cli_arguments": ["runtime_job"],
        "worker_cli_accepts_validation_test_or_holdout_manifest": False,
        "outer_validation_labels_exposed": False,
        "historical_validation_loaded": False,
        "current_test_loaded": False,
        "new_holdout_seen": False,
        "checkpoint_policy_sha256": canonical_json_sha256(config["checkpointing"]),
        "checkpoint_binaries_in_artifacts": False,
    }
    job_payload["job_manifest_sha256"] = canonical_json_sha256(job_payload)
    protected = snapshot_paths(root, config["protected_roots"])
    protocol = {
        "schema_version": 1,
        "status": "PROTOCOL_FROZEN_ENVIRONMENT_PENDING",
        "gate": config["gate"],
        "scientific_scope": "train_only_internal_research",
        "config_sha256": config_sha,
        "code_source_hashes": code_hashes,
        "code_sha256": code_sha,
        "model_registry_sha256": registry_payload["registry_sha256"],
        "job_manifest_sha256": job_payload["job_manifest_sha256"],
        "benchmark_plan_sha256": benchmark.plan_sha256,
        "protected_predecessor_snapshot_sha256": protected["snapshot_sha256"],
        "environment_contract_sha256": canonical_json_sha256(config["environment"]),
        "checkpoint_contract_sha256": canonical_json_sha256(config["checkpointing"]),
        "cuda_execution_contract_sha256": canonical_json_sha256(
            {
                name: config["training"][name]
                for name in (
                    "optimizer_backend",
                    "validation_metric_device",
                    "recurrent_execution",
                    "inference_mode",
                    "torch_compile",
                )
            }
        ),
        "environment_preflight_authority": "artifacts/model_selection/t1_gate_c1_compact_screen_v1/environment/execution_authority.json",
        "predictions_exist_at_freeze": False,
        "historical_validation_loaded": False,
        "current_test_loaded": False,
        "new_holdout_seen": False,
        "suite_v4_mutated": False,
    }
    protocol["protocol_freeze_sha256"] = canonical_json_sha256(protocol)
    paths = config["artifacts"]
    write_json_atomic(root, root / paths["model_registry"], registry_payload, work_scope="gate_c1")
    write_json_atomic(root, root / paths["job_manifest"], job_payload, work_scope="gate_c1")
    write_json_atomic(root, root / paths["protected_snapshot"], protected, work_scope="gate_c1")
    write_json_atomic(root, root / paths["protocol_freeze"], protocol, work_scope="gate_c1")
    return {
        "phase": "freeze",
        "status": protocol["status"],
        "models": len(registry),
        "configurations": registry_payload["configuration_count"],
        "outer_jobs": len(jobs),
        "logical_inner_fits": job_payload["logical_inner_fits"],
        "physical_inner_fits": job_payload["expected_physical_inner_fits"],
        "unique_inner_manifest_pairs": 13,
        "code_sha256": code_sha,
        "config_sha256": config_sha,
    }


def run_gate_c1_preflight(root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    _require_freeze(root, config)
    environment_root = root / config["artifacts"]["environment_root"]
    required = (
        "pip_freeze.txt",
        "pip_install_report.json",
        "wheel_manifest.csv",
        "hardware_report.json",
        "smoke_report.json",
        "determinism_report.json",
        "environment_manifest.json",
    )
    missing = [name for name in required if not (environment_root / name).is_file()]
    if missing:
        raise FileNotFoundError(
            "Stage the fresh Gate C1 environment first with scripts/stage_gate_c1_environment.py: "
            + ", ".join(missing)
        )
    manifest = json.loads((environment_root / "environment_manifest.json").read_text(encoding="utf-8"))
    hardware = json.loads((environment_root / "hardware_report.json").read_text(encoding="utf-8"))
    smoke = json.loads((environment_root / "smoke_report.json").read_text(encoding="utf-8"))
    determinism = json.loads((environment_root / "determinism_report.json").read_text(encoding="utf-8"))
    checks = {
        "environment_manifest_pass": manifest.get("status") == "PASS",
        "python_313": str(hardware.get("python_version", "")).startswith("3.13."),
        "torch_213_cu130": hardware.get("torch_version") == "2.13.0+cu130",
        "cuda_130": hardware.get("torch_cuda_version") == "13.0",
        "rtx_5070_ti": hardware.get("gpu_name") == "NVIDIA GeForce RTX 5070 Ti",
        "cuda_available": hardware.get("cuda_available") is True,
        "all_adapter_smokes_pass": smoke.get("status") == "PASS",
        "all_grid_parameter_counts_pass": smoke.get("all_parameter_counts_lte_100000") is True,
        "determinism_pass": determinism.get("status") == "PASS",
        "determinism_tolerance": float(determinism.get("tolerance", np.inf)) == 1.0e-6,
        "external_pretrained_dependencies_absent": manifest.get("external_pretrained_models") is False,
        "fused_adamw_cuda": smoke.get("fused_adamw_cuda") is True,
        "vectorized_dense_recurrent": smoke.get("recurrent_execution")
        == "vectorized_right_padding_dense_cuda",
        "checkpoint_roundtrip": smoke.get("checkpoint_roundtrip") is True,
        "real_train_only_inner_flow": smoke.get("real_train_only_inner_flow") is True,
    }
    if not all(checks.values()):
        raise ContractViolation(f"Gate C1 environment preflight failed: {checks}")
    protocol = json.loads((root / config["artifacts"]["protocol_freeze"]).read_text(encoding="utf-8"))
    evidence_hashes = {
        name: sha256_file(environment_root / name)
        for name in required
    }
    environment_sha = canonical_json_sha256(evidence_hashes)
    authority = {
        "schema_version": 1,
        "status": "PASS",
        "environment_id": "gate_c_torch",
        "config_sha256": protocol["config_sha256"],
        "code_sha256": protocol["code_sha256"],
        "protocol_freeze_sha256": protocol["protocol_freeze_sha256"],
        "environment_evidence_hashes": evidence_hashes,
        "environment_sha256": environment_sha,
        "checks": checks,
        "preflight_status": "PASS",
        "network_access_during_runtime": False,
    }
    authority["execution_authority_sha256"] = canonical_json_sha256(authority)
    write_json_atomic(
        root, environment_root / "execution_authority.json", authority, work_scope="gate_c1_preflight"
    )
    return {
        "phase": "preflight",
        "status": "PASS",
        "environment_sha256": environment_sha,
        "checks": len(checks),
    }


def run_gate_c1_screen(root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    registry, job_manifest, protocol, authority = _load_execution_authority(root, config)
    _assert_no_predictions_after_code_change(root, config, protocol)
    source = load_split_dataset("t1", "train", root=root)
    source_index = source.frame.set_index("sample_id", drop=False)
    outer_assignments = pd.read_csv(root / config["resampling"]["outer_assignments"])
    env_python = root / config["environment"]["path"] / "Scripts" / "python.exe"
    if not env_python.is_file():
        raise FileNotFoundError(f"Fresh Gate C1 environment is missing: {env_python}")
    registry_by_id = {item["model_id"]: item for item in registry["models"]}
    exit_ledger_path = root / config["artifacts"]["worker_exit_ledger"]
    if exit_ledger_path.is_file():
        exit_ledger = pd.read_csv(exit_ledger_path)
        if exit_ledger["job_id"].duplicated().any():
            raise ContractViolation("Gate C1 worker process-exit ledger contains duplicates")
    else:
        exit_ledger = pd.DataFrame()
    completed = 0
    resumed = 0
    for index, job in enumerate(job_manifest["jobs"], start=1):
        model_id = str(job["model_id"])
        fold_id = str(job["outer_fold_id"])
        safe_fold = fold_id.replace(":", "_")
        status_path = (
            root
            / config["artifacts"]["root"]
            / "worker_status"
            / model_id
            / f"{safe_fold}.json"
        )
        if status_path.is_file():
            validation_ids = _role_ids(outer_assignments, fold_id, "validation")
            _validate_completed_worker_artifacts(
                root=root,
                config=config,
                job=job,
                validation_ids=validation_ids,
                protocol=protocol,
                authority=authority,
                registry_by_id=registry_by_id,
            )
            prior_exit = exit_ledger.loc[
                exit_ledger["job_id"].astype(str).eq(str(job["job_id"]))
            ]
            if len(prior_exit) != 1 or not bool(
                prior_exit.iloc[0]["artifacts_validated_after_exit"]
            ):
                raise ContractViolation(
                    f"Gate C1 resume lacks one validated process-exit record: {job['job_id']}"
                )
            resumed += 1
            print(f"[Gate C1] resume {index}/44 {model_id} {fold_id}", flush=True)
            continue
        train_ids = tuple(
            outer_assignments.loc[
                outer_assignments["fold_id"].astype(str).eq(fold_id)
                & outer_assignments["role"].eq("train"),
                "sample_id",
            ].astype(str)
        )
        if ordered_sample_hash(train_ids) != job["outer_train_sample_ids_sha256"]:
            raise ContractViolation("Gate C1 launcher outer-train hash mismatch")
        targets = source_index.loc[list(train_ids), ["sample_id", "observed_rate_mm_y"]].reset_index(
            drop=True
        )
        staged_path = root / "work" / "gate_c1" / "staged_targets" / f"{model_id}__{safe_fold}.csv"
        write_csv_atomic(root, staged_path, targets, work_scope="gate_c1_stage_targets")
        runtime_payload = {
            "schema_version": 1,
            "source_split": "t1_v1/train",
            "job": job,
            "model_spec": registry_by_id[model_id],
            "config_sha256": protocol["config_sha256"],
            "code_sha256": protocol["code_sha256"],
            "environment_id": authority["environment_id"],
            "environment_sha256": authority["environment_sha256"],
            "sequence_rows": config["sequence"]["sequence_rows"],
            "sequence_manifest": config["sequence"]["sequence_manifest"],
            "outer_assignments": config["resampling"]["outer_assignments"],
            "inner_assignments": config["resampling"]["inner_assignments"],
            "fold_contracts": config["sequence"]["fold_contracts"],
            "staged_train_targets": staged_path.relative_to(root).as_posix(),
            "preprocessing_contract_sha256": canonical_json_sha256(
                {
                    "numeric_channels": config["sequence"]["numeric_channels"],
                    "categorical_channels": config["sequence"]["categorical_channels"],
                    "masks": config["sequence"]["masks"],
                    "fit_role": "train",
                    "padding_excluded": True,
                    "unknown_bucket": 0,
                }
            ),
            "training": config["training"],
            "checkpointing": config["checkpointing"],
            "prediction_required_columns": config["prediction_schema"]["required_columns"],
            "prediction_optional_columns": config["prediction_schema"][
                "probabilistic_optional_columns"
            ],
        }
        assert_train_only_c1_job(runtime_payload)
        runtime_path = root / "work" / "gate_c1" / "runtime_jobs" / f"{model_id}__{safe_fold}.json"
        write_json_atomic(root, runtime_path, runtime_payload, work_scope="gate_c1_runtime_jobs")
        print(f"[Gate C1] run {index}/44 {model_id} {fold_id}", flush=True)
        completed_process = subprocess.run(
            [
                str(env_python),
                str(root / "scripts" / "run_gate_c1_worker.py"),
                "--runtime-job",
                str(runtime_path),
            ],
            cwd=root,
            text=True,
            check=False,
        )
        validation_ids = _role_ids(outer_assignments, fold_id, "validation")
        _validate_completed_worker_artifacts(
            root=root,
            config=config,
            job=job,
            validation_ids=validation_ids,
            protocol=protocol,
            authority=authority,
            registry_by_id=registry_by_id,
        )
        exit_code = int(completed_process.returncode)
        teardown_anomaly = exit_code == WINDOWS_CUDA_POST_COMMIT_EXIT_CODE
        if exit_code != 0 and not teardown_anomaly:
            raise RuntimeError(
                f"Gate C1 worker failed for {job['job_id']} with code {exit_code}"
            )
        exit_row = pd.DataFrame(
            [
                {
                    "job_id": str(job["job_id"]),
                    "model_id": model_id,
                    "fold_id": fold_id,
                    "worker_returncode": exit_code,
                    "post_commit_teardown_anomaly": teardown_anomaly,
                    "accepted_returncode": True,
                    "artifacts_validated_after_exit": True,
                    "outer_validation_labels_loaded_by_worker": False,
                    "historical_validation_loaded": False,
                    "current_test_loaded": False,
                    "new_holdout_seen": False,
                    "code_sha256": protocol["code_sha256"],
                    "config_sha256": protocol["config_sha256"],
                    "environment_sha256": authority["environment_sha256"],
                }
            ]
        )
        exit_ledger = pd.concat((exit_ledger, exit_row), ignore_index=True)
        if exit_ledger["job_id"].duplicated().any():
            raise ContractViolation("Gate C1 attempted to duplicate a worker process-exit record")
        write_csv_atomic(root, exit_ledger_path, exit_ledger, work_scope="gate_c1_exit_ledger")
        completed += 1
    return {
        "phase": "screen",
        "status": "PASS_WORKERS_COMPLETE",
        "outer_jobs": len(job_manifest["jobs"]),
        "executed_jobs": completed,
        "resumed_jobs": resumed,
        "outer_refits": 220,
        "post_commit_teardown_anomalies": int(
            exit_ledger["post_commit_teardown_anomaly"].astype(bool).sum()
        ),
        "historical_validation_loaded": False,
        "current_test_loaded": False,
    }


def run_gate_c1_aggregate(root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    registry, job_manifest, _, authority = _load_execution_authority(root, config)
    return aggregate_and_score(root, config, registry, job_manifest, authority)


def run_gate_c1_validation(root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    registry, jobs, protocol, authority = _load_execution_authority(root, config)
    _validate_predecessor_hashes(root, config)
    frozen_snapshot = json.loads(
        (root / config["artifacts"]["protected_snapshot"]).read_text(encoding="utf-8")
    )
    current_snapshot = snapshot_paths(root, config["protected_roots"])
    paths = config["artifacts"]
    required_artifacts = (
        "tuning_inventory",
        "selected_inner_oof",
        "worker_status",
        "worker_exit_ledger",
        "execution_incident",
        "shard_inventory",
        "scored_predictions",
        "fold_metrics",
        "aggregate_metrics",
        "seed_stability",
        "native_metrics",
        "screening_register",
        "rejection_register",
        "admission_manifest",
        "label_access_ledger",
        "compute_inventory",
        "checkpoint_inventory",
    )
    missing = [name for name in required_artifacts if not (root / paths[name]).is_file()]
    if missing:
        raise FileNotFoundError(f"Gate C1 aggregate artifacts are missing: {missing}")
    tuning = pd.read_csv(root / paths["tuning_inventory"])
    oof = pd.read_csv(root / paths["selected_inner_oof"])
    status = pd.read_csv(root / paths["worker_status"])
    worker_exit = pd.read_csv(root / paths["worker_exit_ledger"])
    execution_incident = json.loads(
        (root / paths["execution_incident"]).read_text(encoding="utf-8")
    )
    shards = pd.read_csv(root / paths["shard_inventory"])
    scored = pd.read_csv(root / paths["scored_predictions"])
    folds = pd.read_csv(root / paths["fold_metrics"])
    aggregate = pd.read_csv(root / paths["aggregate_metrics"])
    screening = pd.read_csv(root / paths["screening_register"])
    checkpoints = pd.read_csv(root / paths["checkpoint_inventory"])
    admission = json.loads((root / paths["admission_manifest"]).read_text(encoding="utf-8"))
    ledger = json.loads((root / paths["label_access_ledger"]).read_text(encoding="utf-8"))
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append({"name": name, "status": "PASS" if passed else "FAIL", "detail": detail})

    check("protected_predecessors", frozen_snapshot == current_snapshot, "C0/B5/B6/suite-v4/holdout-v3 hashes")
    check("registry", len(registry["models"]) == 4 and registry["configuration_count"] == 56, "4 models, 56 grids")
    check("outer_jobs", len(jobs["jobs"]) == 44, "4 x 11 frozen jobs")
    check("logical_inner_fits", len(tuning) == 9240, "all logical evaluations present")
    check("physical_cache_keys", tuning["fit_cache_key"].nunique() == 3640, "13 pairs x 56 grids x 5 seeds")
    check("outer_refits", len(status) == 44 and status["outer_refits"].sum() == 220, "five seeds per job")
    check(
        "checkpoint_inventory",
        len(checkpoints) == 3860
        and int(checkpoints["fit_id"].nunique()) == 3860
        and int(checkpoints["role"].eq("inner").sum()) == 3640
        and int(checkpoints["role"].eq("outer").sum()) == 220
        and checkpoints["keep_top_k"].eq(5).all()
        and not checkpoints["outer_labels_used_for_ranking"].astype(bool).any(),
        "3640 inner plus 220 outer work-only top-five manifests",
    )
    check("unlabeled_shards", len(shards) == 44 and not shards["contains_y_true"].astype(bool).any(), "44 label-free shards")
    check("scored_rows", len(scored) == 16065, "11900 + 2380 + 1785")
    check(
        "deep_single_seed_rows",
        len(scored.loc[scored["model_id"].isin(C1_REQUIRED_MODELS) & scored["aggregation"].eq("single_seed")])
        == 11900,
        "four models x five seeds x 595 origins",
    )
    check(
        "deep_ensemble_rows",
        len(scored.loc[scored["model_id"].isin(C1_REQUIRED_MODELS) & scored["aggregation"].eq("mean_of_fixed_seeds")])
        == 2380,
        "four canonical ensembles x 595 origins",
    )
    check(
        "comparator_rows",
        len(scored.loc[scored["model_id"].isin(config["comparators"]["model_ids"])]) == 1785,
        "B1/B7/B8 exact matched origins",
    )
    check("fold_metrics", folds["fold_id"].nunique() == 11, "eleven target dates")
    check("finite_predictions", np.isfinite(scored[["y_true", "y_pred"]].to_numpy(float)).all(), "all scores finite")
    check(
        "ensemble_exact_mean",
        _validate_ensemble_means(scored),
        "canonical ensemble equals arithmetic mean of all five seeds",
    )
    c04_ensemble = scored.loc[
        scored["model_id"].eq("C04_probabilistic_gru_student_t")
        & scored["aggregation"].eq("mean_of_fixed_seeds")
    ]
    check(
        "no_pseudo_student_t_ensemble",
        c04_ensemble[["distribution_loc", "distribution_scale", "distribution_df"]].isna().all().all(),
        "ensemble publishes point mean only",
    )
    check(
        "worker_status",
        len(status) == 44
        and status["status"].eq("COMPLETED").all()
        and not status["outer_validation_labels_loaded_by_worker"].astype(bool).any(),
        "all workers completed without outer labels",
    )
    check(
        "worker_process_exit_ledger",
        len(worker_exit) == 44
        and worker_exit["job_id"].nunique() == 44
        and worker_exit["accepted_returncode"].astype(bool).all()
        and worker_exit["artifacts_validated_after_exit"].astype(bool).all()
        and set(worker_exit["worker_returncode"].astype(int)).issubset(
            {0, WINDOWS_CUDA_POST_COMMIT_EXIT_CODE}
        )
        and not worker_exit["outer_validation_labels_loaded_by_worker"].astype(bool).any(),
        "44 isolated process exits accepted only after artifact validation",
    )
    incident_rows = execution_incident.get("incidents", [])
    check(
        "execution_incident_register",
        bool(incident_rows)
        and all(item.get("outer_label_scoring_started") is False for item in incident_rows)
        and all(int(item.get("outer_label_access_events", -1)) == 0 for item in incident_rows)
        and all(item.get("historical_validation_loaded") is False for item in incident_rows)
        and all(item.get("current_test_loaded") is False for item in incident_rows)
        and all(item.get("new_holdout_seen") is False for item in incident_rows)
        and all(item.get("invalidated_results_used_for_selection") is False for item in incident_rows),
        "invalidated diagnostics are registered and excluded before label scoring",
    )
    check(
        "label_access_ledger",
        ledger.get("access_event") == 1
        and ledger.get("all_shards_hash_frozen_before_access") is True
        and ledger.get("worker_outer_validation_labels_loaded") is False,
        "single scorer-only access after shard freeze",
    )
    check(
        "admission_reproducible",
        sorted(admission["admitted_model_ids"])
        == sorted(screening.loc[screening["status"].eq("PASSED_TEMPORAL_SCREEN"), "model_id"].astype(str)),
        "manifest follows screening register",
    )
    check(
        "terminal_model_statuses",
        len(screening) == 4
        and screening["status"].isin(
            ["PASSED_TEMPORAL_SCREEN", "REJECTED_TEMPORAL_SCREEN", "REJECTED_MODEL_EXECUTION"]
        ).all(),
        "all four models have scientific terminal status",
    )
    check(
        "boundary_flags",
        admission.get("historical_validation_loaded") is False
        and admission.get("current_test_loaded") is False
        and admission.get("new_holdout_seen") is False
        and admission.get("profile_zone_transition_audit_executed") is False
        and admission.get("suite_v5_created") is False,
        "C1 remains temporal train-only research",
    )
    check("config_hash", protocol["config_sha256"] == sha256_file(root / CONFIG_RELATIVE), "frozen config unchanged")
    check("code_hash", protocol["code_sha256"] == canonical_json_sha256(_code_source_hashes(root, config)), "execution code unchanged")
    check("environment_authority", authority["status"] == "PASS", "fresh gate_c_torch authority")
    failed = [item for item in checks if item["status"] != "PASS"]
    validation = {
        "schema_version": 1,
        "gate": config["gate"],
        "status": "PASS_C1_TEMPORAL_SCREEN" if not failed else "FAIL_PROTOCOL",
        "scientific_scope": "train_only_internal_research",
        "checks": checks,
        "check_count": len(checks),
        "failed_checks": len(failed),
        "models": 4,
        "admitted_models": admission["admitted_model_ids"],
        "scored_prediction_rows": len(scored),
        "historical_validation_loaded": False,
        "current_test_loaded": False,
        "new_holdout_seen": False,
        "profile_zone_transition_audit_executed": False,
        "suite_v5_created": False,
    }
    write_json_atomic(root, root / paths["validation_report"], validation, work_scope="gate_c1_validate")
    if failed:
        raise ContractViolation(f"Gate C1 independent validation failed: {failed}")
    inventory_paths = [
        root / paths[name]
        for name in (
            "model_registry",
            "job_manifest",
            "protocol_freeze",
            "protected_snapshot",
            *required_artifacts,
            "validation_report",
        )
    ]
    environment_root = root / paths["environment_root"]
    inventory_paths.extend(sorted(path for path in environment_root.rglob("*") if path.is_file()))
    inventory_paths.extend(
        sorted(
            path
            for subdir in ("prediction_shards", "tuning_shards", "selected_inner_oof_shards", "worker_status")
            for path in (root / paths["root"] / subdir).rglob("*")
            if path.is_file()
        )
    )
    write_csv_atomic(
        root,
        root / paths["artifact_inventory"],
        artifact_inventory(root, inventory_paths),
        work_scope="gate_c1_validate",
    )
    return {
        "phase": "validate",
        "status": validation["status"],
        "checks": len(checks),
        "failed_checks": 0,
        "admitted_models": admission["admitted_model_ids"],
    }


def _model_registry(
    root: Path, config: Mapping[str, Any], *, code_sha: str, config_sha: str
) -> list[SequenceModelSpec]:
    gate_c = yaml.safe_load((root / config["architecture_source"]["config"]).read_text(encoding="utf-8"))
    selected = [
        item
        for item in gate_c["architecture_registry"]
        if item.get("status") == config["architecture_source"]["include_status"]
    ]
    if tuple(item["model_id"] for item in selected) != C1_REQUIRED_MODELS:
        raise ContractViolation("Frozen Gate C0 required compact registry changed")
    specs = []
    for item in selected:
        grid = _expand_grid(item["grid"])
        expected = int(config["architecture_source"]["expected_configuration_counts"][item["model_id"]])
        if len(grid) != expected:
            raise ContractViolation(f"Gate C1 grid count changed for {item['model_id']}")
        probabilistic = bool(item.get("probabilistic"))
        specs.append(
            SequenceModelSpec(
                model_id=str(item["model_id"]),
                family=str(item["family"]),
                probabilistic=probabilistic,
                parameter_grid=grid,
                seeds=C1_SEEDS,
                numeric_channels=C1_NUMERIC_CHANNELS,
                categorical_channels=C1_CATEGORICAL_CHANNELS,
                masks=C1_MASKS,
                training_objective="student_t_nll" if probabilistic else "huber_delta_1_standardized",
                selection_objective="pooled_inner_crps" if probabilistic else "pooled_inner_mae",
                parameter_count_limit=100_000,
                environment_id="gate_c_torch",
            )
        )
    if sum(len(spec.parameter_grid) for spec in specs) != 56:
        raise ContractViolation("Gate C1 must freeze exactly 56 configurations")
    return specs


def _build_jobs(
    root: Path, config: Mapping[str, Any], registry: Sequence[SequenceModelSpec]
) -> tuple[list[dict[str, Any]], C1BenchmarkPlan]:
    outer_assignments = pd.read_csv(root / config["resampling"]["outer_assignments"])
    inner_assignments = pd.read_csv(root / config["resampling"]["inner_assignments"])
    fold_sequences = pd.read_csv(root / config["sequence"]["fold_contracts"])
    rolling_outer = fold_sequences.loc[
        fold_sequences["level"].eq("outer") & fold_sequences["design"].eq("rolling_origin")
    ].sort_values("validation_target_date", kind="mergesort")
    rolling_inner = fold_sequences.loc[
        fold_sequences["level"].eq("inner") & fold_sequences["design"].eq("rolling_origin")
    ]
    if len(rolling_outer) != 11:
        raise ContractViolation("Gate C1 requires exactly 11 rolling outer folds")
    jobs = []
    inner_pairs = set()
    for spec in registry:
        for outer in rolling_outer.to_dict("records"):
            fold_id = str(outer["fold_id"])
            inner = rolling_inner.loc[rolling_inner["parent_fold_id"].astype(str).eq(fold_id)].sort_values(
                "validation_target_date", kind="mergesort"
            )
            if len(inner) != 3:
                raise ContractViolation(f"Gate C1 requires three inner folds for {fold_id}")
            train_ids = _role_ids(outer_assignments, fold_id, "train")
            validation_ids = _role_ids(outer_assignments, fold_id, "validation")
            inner_contexts = []
            for record in inner.to_dict("records"):
                inner_fold_id = str(record["fold_id"])
                inner_train = _role_ids(inner_assignments, inner_fold_id, "train")
                inner_validation = _role_ids(inner_assignments, inner_fold_id, "validation")
                if not set(inner_train).issubset(train_ids) or not set(inner_validation).issubset(train_ids):
                    raise ContractViolation("Gate C1 inner context escapes outer train")
                pair = (
                    ordered_sample_hash(inner_train),
                    ordered_sample_hash(inner_validation),
                    str(record["train_sequence_pairs_sha256"]),
                    str(record["validation_sequence_pairs_sha256"]),
                )
                inner_pairs.add(pair)
                inner_contexts.append(
                    {
                        "fold_id": inner_fold_id,
                        "train_sample_ids_sha256": pair[0],
                        "validation_sample_ids_sha256": pair[1],
                        "train_sequence_pairs_sha256": pair[2],
                        "validation_sequence_pairs_sha256": pair[3],
                        "forward_only": bool(record["forward_only"]),
                    }
                )
            jobs.append(
                {
                    "job_id": f"{spec.model_id}::{fold_id}",
                    "model_id": spec.model_id,
                    "model_spec_sha256": spec.spec_sha256,
                    "environment_id": "gate_c_torch",
                    "source_split": "t1_v1/train",
                    "outer_fold_id": fold_id,
                    "outer_design": "rolling_origin",
                    "outer_target_date": str(outer["validation_target_date"]),
                    "outer_train_sample_ids_sha256": ordered_sample_hash(train_ids),
                    "outer_validation_sample_ids_sha256": ordered_sample_hash(validation_ids),
                    "outer_train_sequence_pairs_sha256": str(outer["train_sequence_pairs_sha256"]),
                    "outer_validation_sequence_pairs_sha256": str(
                        outer["validation_sequence_pairs_sha256"]
                    ),
                    "inner_fold_ids": [item["fold_id"] for item in inner_contexts],
                    "inner_contexts": inner_contexts,
                    "outer_labels_exposed_to_worker": False,
                    "model_data_inputs": ["t1_v1/train_sequences", "staged_outer_train_targets"],
                }
            )
    if len(jobs) != 44 or len(inner_pairs) != 13:
        raise ContractViolation(f"Gate C1 job geometry changed: jobs={len(jobs)}, pairs={len(inner_pairs)}")
    benchmark_payload = json.loads((root / config["resampling"]["benchmark_plan"]).read_text(encoding="utf-8"))
    sequence_contract = json.loads((root / config["sequence"]["sequence_contract"]).read_text(encoding="utf-8"))
    content_hash = sequence_contract.pop("contract_sha256")
    benchmark = C1BenchmarkPlan(
        source_split="t1_v1/train",
        benchmark_plan_sha256=str(benchmark_payload["plan_sha256"]),
        sequence_contract_content_sha256=str(content_hash),
        sequence_contract_file_sha256=sha256_file(root / config["sequence"]["sequence_contract"]),
        outer_jobs=tuple(jobs),
        expected_outer_folds=11,
        inner_folds_per_outer=3,
        logical_inner_fits=9240,
        physical_inner_fits=3640,
    )
    return jobs, benchmark


def _expand_grid(grid: Mapping[str, Sequence[Any]]) -> tuple[dict[str, Any], ...]:
    names = tuple(grid)
    rows = [dict(zip(names, values, strict=True)) for values in product(*(grid[name] for name in names))]
    rows.sort(key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")))
    return tuple(rows)


def _validate_config(root: Path, config: Mapping[str, Any]) -> None:
    if config.get("source_split") != "t1_v1/train" or config.get("execution_boundary") != "temporal_screen_only":
        raise ContractViolation("Gate C1 data/execution boundary changed")
    if tuple(config["training"]["seeds"]) != C1_SEEDS:
        raise ContractViolation("Gate C1 seed policy changed")
    if tuple(config["architecture_source"]["required_model_ids"]) != C1_REQUIRED_MODELS:
        raise ContractViolation("Gate C1 required model IDs changed")
    if int(config["expected_counts"]["outer_jobs"]) != 44:
        raise ContractViolation("Gate C1 expected outer job count changed")
    checkpointing = config["checkpointing"]
    if (
        checkpointing.get("root") != "work/gate_c1/checkpoints"
        or int(checkpointing.get("keep_top_k", -1)) != 5
        or int(checkpointing.get("stage_interval_epochs", -1)) != 50
        or checkpointing.get("inner_ranking") != "metric_ascending_then_epoch"
        or checkpointing.get("outer_ranking") != "latest_epoch_fixed_final"
        or checkpointing.get("outer_selection") != "fixed_final_epoch"
        or checkpointing.get("persistence_scope") != "work_only"
        or checkpointing.get("outer_labels_allowed") is not False
    ):
        raise ContractViolation("Gate C1 checkpoint policy changed")
    training = config["training"]
    if (
        training.get("optimizer_backend") != "fused_adamw_cuda"
        or training.get("validation_metric_device") != "cuda"
        or training.get("recurrent_execution") != "vectorized_right_padding_dense_cuda"
        or training.get("inference_mode") is not True
        or training.get("torch_compile") is not False
    ):
        raise ContractViolation("Gate C1 CUDA execution policy changed")
    candidates = [
        *config["frozen_predecessors"]["files"].keys(),
        *config["protected_roots"],
        *config["code_sources"],
        *config["sequence"].values(),
        *config["resampling"].values(),
        config["environment"]["path"],
        config["environment"]["lock"],
        config["checkpointing"]["root"],
        config["comparators"]["source"],
        *config["artifacts"].values(),
    ]
    for value in candidates:
        if not isinstance(value, str) or value in {"rolling_origin"}:
            continue
        path = Path(value)
        if path.is_absolute():
            raise ContractViolation(f"Gate C1 config path must be repository-relative: {value}")
        resolved = (root / path).resolve()
        try:
            resolved.relative_to(root.resolve())
        except ValueError as exc:
            raise ContractViolation(f"Gate C1 path escapes repository root: {value}") from exc


def _validate_predecessor_hashes(root: Path, config: Mapping[str, Any]) -> None:
    mismatches = []
    for relative, expected in config["frozen_predecessors"]["files"].items():
        path = root / relative
        actual = sha256_file(path) if path.is_file() else "MISSING"
        if actual != expected:
            mismatches.append({"path": relative, "expected": expected, "actual": actual})
    if mismatches:
        raise ContractViolation(f"Gate C1 protected predecessor mismatch: {mismatches}")
    contract = json.loads((root / config["sequence"]["sequence_contract"]).read_text(encoding="utf-8"))
    supplied = contract.pop("contract_sha256")
    recomputed = canonical_json_sha256(contract)
    expected_content = config["frozen_predecessors"]["gate_c0_contract_content_sha256"]
    if supplied != recomputed or supplied != expected_content:
        raise ContractViolation("Gate C0 sequence contract content hash changed")


def _code_source_hashes(root: Path, config: Mapping[str, Any]) -> dict[str, str]:
    missing = [relative for relative in config["code_sources"] if not (root / relative).is_file()]
    if missing:
        raise FileNotFoundError(f"Gate C1 code sources are incomplete: {missing}")
    return {relative: sha256_file(root / relative) for relative in config["code_sources"]}


def _require_freeze(root: Path, config: Mapping[str, Any]) -> None:
    for key in ("model_registry", "job_manifest", "protocol_freeze", "protected_snapshot"):
        if not (root / config["artifacts"][key]).is_file():
            raise FileNotFoundError(f"Run Gate C1 freeze first: {key}")


def _load_execution_authority(
    root: Path, config: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    _require_freeze(root, config)
    paths = config["artifacts"]
    registry = json.loads((root / paths["model_registry"]).read_text(encoding="utf-8"))
    jobs = json.loads((root / paths["job_manifest"]).read_text(encoding="utf-8"))
    protocol = json.loads((root / paths["protocol_freeze"]).read_text(encoding="utf-8"))
    authority_path = root / paths["environment_root"] / "execution_authority.json"
    if not authority_path.is_file():
        raise FileNotFoundError("Run Gate C1 environment preflight before model execution")
    authority = json.loads(authority_path.read_text(encoding="utf-8"))
    if authority.get("status") != "PASS":
        raise ContractViolation("Gate C1 execution authority did not pass")
    if authority["config_sha256"] != protocol["config_sha256"] or authority["code_sha256"] != protocol[
        "code_sha256"
    ]:
        raise ContractViolation("Gate C1 execution authority is stale")
    return registry, jobs, protocol, authority


def _assert_no_predictions_after_code_change(
    root: Path, config: Mapping[str, Any], protocol: Mapping[str, Any]
) -> None:
    current_config = sha256_file(root / CONFIG_RELATIVE)
    current_code = canonical_json_sha256(_code_source_hashes(root, config))
    if current_config == protocol["config_sha256"] and current_code == protocol["code_sha256"]:
        return
    prediction_root = root / config["artifacts"]["root"] / "prediction_shards"
    count = sum(1 for path in prediction_root.rglob("*.csv")) if prediction_root.exists() else 0
    message = "Gate C1 config/code changed after protocol freeze"
    if count:
        message += f" and invalidates {count} prediction shards; create a new freeze and rerun"
    raise ContractViolation(message)


def _role_ids(assignments: pd.DataFrame, fold_id: str, role: str) -> tuple[str, ...]:
    values = tuple(
        assignments.loc[
            assignments["fold_id"].astype(str).eq(fold_id) & assignments["role"].eq(role),
            "sample_id",
        ].astype(str)
    )
    if not values or len(values) != len(set(values)):
        raise ContractViolation(f"Gate C1 invalid {role} manifest: {fold_id}")
    return values


def _validate_completed_worker_artifacts(
    *,
    root: Path,
    config: Mapping[str, Any],
    job: Mapping[str, Any],
    validation_ids: tuple[str, ...],
    protocol: Mapping[str, Any],
    authority: Mapping[str, Any],
    registry_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Fail closed before accepting any isolated worker process exit."""

    model_id = str(job["model_id"])
    fold_id = str(job["outer_fold_id"])
    safe_fold = fold_id.replace(":", "_")
    artifact_root = root / config["artifacts"]["root"]
    status_path = artifact_root / "worker_status" / model_id / f"{safe_fold}.json"
    shard_path = artifact_root / "prediction_shards" / model_id / f"{safe_fold}.csv"
    tuning_path = artifact_root / "tuning_shards" / model_id / f"{safe_fold}.csv"
    oof_path = artifact_root / "selected_inner_oof_shards" / model_id / f"{safe_fold}.csv"
    expected_paths = (status_path, shard_path, tuning_path, oof_path)
    missing = [
        path.relative_to(root).as_posix() for path in expected_paths if not path.is_file()
    ]
    if missing:
        raise ContractViolation(f"Gate C1 worker exited before complete atomic artifacts: {missing}")
    expected_logical = (
        len(job["inner_fold_ids"])
        * len(registry_by_id[model_id]["parameter_grid"])
        * len(C1_SEEDS)
    )
    status = json.loads(status_path.read_text(encoding="utf-8"))
    identity_ok = (
        status.get("status") == "COMPLETED"
        and status.get("job_id") == job["job_id"]
        and status.get("model_id") == model_id
        and status.get("fold_id") == fold_id
        and tuple(status.get("seeds", ())) == C1_SEEDS
        and int(status.get("logical_inner_evaluations", -1)) == expected_logical
        and int(status.get("outer_refits", -1)) == len(C1_SEEDS)
        and int(status.get("outer_prediction_rows", -1))
        == len(validation_ids) * len(C1_SEEDS)
        and status.get("outer_validation_labels_loaded_by_worker") is False
        and status.get("historical_validation_loaded") is False
        and status.get("current_test_loaded") is False
        and status.get("new_holdout_seen") is False
        and int(status.get("outer_checkpoint_manifest_count", -1)) == len(C1_SEEDS)
        and status.get("checkpoint_persistence_scope") == "work_only"
        and status.get("outer_labels_used_for_checkpoint_selection") is False
    )
    if not identity_ok:
        raise ContractViolation(f"Gate C1 worker terminal identity/status invalid: {job['job_id']}")
    expected_hashes = {
        shard_path: status.get("unlabeled_prediction_sha256"),
        tuning_path: status.get("tuning_shard_sha256"),
        oof_path: status.get("selected_inner_oof_sha256"),
    }
    mismatches = [
        path.relative_to(root).as_posix()
        for path, expected in expected_hashes.items()
        if not isinstance(expected, str) or sha256_file(path) != expected
    ]
    if mismatches:
        raise ContractViolation(f"Gate C1 post-exit artifact hash mismatch: {mismatches}")
    shard = pd.read_csv(shard_path)
    SequencePredictionBundle.validate(
        shard,
        expected_sample_ids=validation_ids,
        expected_model_id=model_id,
        expected_fold_id=fold_id,
    )
    forbidden = set(config["prediction_schema"]["worker_forbidden_columns"])
    if forbidden & set(shard.columns):
        raise ContractViolation("Gate C1 post-exit worker shard contains forbidden target columns")
    expected_provenance = {
        "code_sha256": protocol["code_sha256"],
        "config_sha256": protocol["config_sha256"],
        "environment_sha256": authority["environment_sha256"],
        "environment_id": authority["environment_id"],
    }
    for column, expected in expected_provenance.items():
        if set(shard[column].astype(str)) != {str(expected)}:
            raise ContractViolation(f"Gate C1 post-exit shard provenance mismatch: {column}")
    tuning = pd.read_csv(tuning_path)
    if (
        len(tuning) != expected_logical
        or tuning.duplicated(["parameter_sha256", "inner_fold_id", "seed"]).any()
        or not np.isfinite(tuning["mae"].to_numpy(float)).all()
        or not {
            "checkpoint_manifest",
            "checkpoint_manifest_sha256",
            "retained_checkpoint_count",
            "selected_checkpoint_epoch",
        }.issubset(tuning.columns)
    ):
        raise ContractViolation("Gate C1 post-exit tuning inventory is incomplete or non-finite")
    for record in tuning[
        ["checkpoint_manifest", "checkpoint_manifest_sha256"]
    ].drop_duplicates().to_dict("records"):
        _validate_work_checkpoint_manifest(
            root,
            relative_path=str(record["checkpoint_manifest"]),
            expected_sha256=str(record["checkpoint_manifest_sha256"]),
            expected_role="inner",
        )
    outer_records = status.get("outer_checkpoint_manifests", [])
    if len(outer_records) != len(C1_SEEDS):
        raise ContractViolation("Gate C1 outer checkpoint manifest set is incomplete")
    for record in outer_records:
        manifest = _validate_work_checkpoint_manifest(
            root,
            relative_path=str(record["path"]),
            expected_sha256=str(record["sha256"]),
            expected_role="outer",
        )
        if int(manifest["selected_epoch"]) != int(status["outer_epoch_count"]):
            raise ContractViolation("Outer checkpoint did not select the fixed final epoch")
    return status


def _validate_work_checkpoint_manifest(
    root: Path,
    *,
    relative_path: str,
    expected_sha256: str,
    expected_role: str,
) -> dict[str, Any]:
    """Torch-free parent validation of a work-only checkpoint manifest."""

    path = (root / relative_path).resolve()
    checkpoint_root = (root / "work" / "gate_c1" / "checkpoints").resolve()
    try:
        path.relative_to(checkpoint_root)
    except ValueError as exc:
        raise ContractViolation("Gate C1 checkpoint manifest escapes work/") from exc
    if not path.is_file() or sha256_file(path) != expected_sha256:
        raise ContractViolation("Gate C1 checkpoint manifest is missing or changed")
    payload = json.loads(path.read_text(encoding="utf-8"))
    supplied = payload.pop("manifest_content_sha256", None)
    if supplied != canonical_json_sha256(payload):
        raise ContractViolation("Gate C1 checkpoint manifest content hash mismatch")
    payload["manifest_content_sha256"] = supplied
    if (
        payload.get("status") != "COMPLETE"
        or payload.get("role") != expected_role
        or int(payload.get("keep_top_k", -1)) != 5
        or payload.get("persistence_scope") != "work_only"
        or payload.get("outer_labels_used_for_ranking") is not False
    ):
        raise ContractViolation("Gate C1 checkpoint manifest policy mismatch")
    checkpoints = payload.get("checkpoints", [])
    if not checkpoints or len(checkpoints) != int(payload.get("retained_checkpoint_count", -1)):
        raise ContractViolation("Gate C1 ranked checkpoint count mismatch")
    for record in checkpoints:
        checkpoint_path = (root / str(record["path"])).resolve()
        try:
            checkpoint_path.relative_to(checkpoint_root)
        except ValueError as exc:
            raise ContractViolation("Gate C1 ranked checkpoint escapes work/") from exc
        if not checkpoint_path.is_file() or sha256_file(checkpoint_path) != record.get("sha256"):
            raise ContractViolation("Gate C1 ranked checkpoint hash mismatch")
    return payload


def _validate_ensemble_means(scored: pd.DataFrame) -> bool:
    for model_id in C1_REQUIRED_MODELS:
        single = scored.loc[
            scored["model_id"].eq(model_id) & scored["aggregation"].eq("single_seed")
        ]
        expected = single.groupby(["fold_id", "sample_id"], sort=True)["y_pred"].mean().rename("expected")
        ensemble = scored.loc[
            scored["model_id"].eq(model_id) & scored["aggregation"].eq("mean_of_fixed_seeds")
        ].set_index(["fold_id", "sample_id"])["y_pred"]
        aligned = expected.to_frame().join(ensemble.rename("actual"), how="outer")
        if aligned.isna().any().any() or not np.allclose(
            aligned["expected"], aligned["actual"], atol=1.0e-12, rtol=0
        ):
            return False
    return True

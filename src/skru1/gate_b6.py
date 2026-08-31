"""Gate B6 orchestration and dependency-free artifact aggregation."""

from __future__ import annotations

import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd
import yaml

from .artifact_io import artifact_inventory, resolve_repo_path, snapshot_paths, write_csv_atomic, write_json_atomic
from .b6_evaluation import (
    calibrate_predictions,
    canonical_prediction_rows,
    global_parameter_registry,
    learning_curve_metrics,
    paired_sensitivity_tables,
    probabilistic_metric_table,
    robustness_group_metrics,
    screening_register,
    segmented_metrics,
    temporal_aggregate_metrics,
    temporal_fold_metrics,
)
from .b6_governance import (
    AMENDMENT_RELATIVE_PATH,
    effective_environment_settings,
    excluded_model_records,
    executable_model_ids,
    protocol_amendment_payload,
)
from .b6_registry import build_job_manifest, model_spec_from_registry, registry_payload
from .benchmarking import BenchmarkPlan, PredictionBundle, assert_train_only_worker_job, canonical_json_sha256
from .data_contracts import ContractViolation, discover_project_root, load_canonical_bundle, sha256_file
from .splits import attach_spatial_zones, build_spatial_zone_map, load_split_dataset, sample_id_list_sha256


def load_gate_b6_config(root: str | Path | None = None) -> tuple[Path, dict[str, Any]]:
    project_root = discover_project_root(root)
    path = project_root / "configs" / "gate_b6.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ContractViolation("configs/gate_b6.yaml must contain a mapping")
    required = {
        "gate",
        "source_split",
        "benchmark_version",
        "data_boundary",
        "selection",
        "temporal_screen",
        "suite_v4_eligibility",
        "environments",
        "frozen_comparators",
        "models",
        "artifacts",
    }
    missing = required - set(config)
    if missing:
        raise ContractViolation(f"Gate B6 config is missing keys: {sorted(missing)}")
    if config["source_split"] != "t1_v1/train":
        raise ContractViolation("Gate B6 source split must be exactly t1_v1/train")
    return project_root, config


def run_gate_b6_preflight(root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    paths = _paths(root, config)
    b5_validation_path = root / "artifacts" / "model_selection" / "t1_b5_evidence_v1" / "validation_report.json"
    if not b5_validation_path.is_file():
        raise ContractViolation("Gate B5 independent validation must run before B6 preflight")
    b5_validation = json.loads(b5_validation_path.read_text(encoding="utf-8"))
    if b5_validation.get("status") != "PASS":
        raise ContractViolation("Gate B5 validation did not pass")
    benchmark_payload = json.loads(
        (root / "artifacts" / "splits" / "t1_train_benchmark_v1" / "benchmark_plan.json").read_text(
            encoding="utf-8"
        )
    )
    benchmark = BenchmarkPlan.from_dict(benchmark_payload)
    if benchmark.preregistered_source_hashes.get("configs/gate_b6.yaml") != sha256_file(
        root / "configs" / "gate_b6.yaml"
    ):
        raise ContractViolation("Gate B6 config changed after B5 freeze")
    contracts = pd.read_csv(
        root / "artifacts" / "splits" / "t1_train_benchmark_v1" / "fold_contracts.csv",
        keep_default_na=False,
    )
    outer = contracts.loc[contracts["level"].eq("outer")]
    inner = contracts.loc[contracts["level"].eq("inner")]
    registry = registry_payload(root, config, benchmark)
    jobs = build_job_manifest(registry, benchmark, outer, inner)
    assert_train_only_worker_job(jobs)
    amendment = protocol_amendment_payload(root, registry)
    executable = executable_model_ids(root, registry)
    environments = environment_manifest(root, config)
    external = external_model_manifest(root, config)
    errata = protocol_errata(config, registry, root=root)
    write_json_atomic(root, paths["model_registry"], registry, work_scope="gate_b6")
    write_json_atomic(root, paths["frozen_job_manifest"], jobs, work_scope="gate_b6")
    write_json_atomic(root, paths["environment_manifest"], environments, work_scope="gate_b6")
    write_json_atomic(root, paths["external_model_manifest"], external, work_scope="gate_b6")
    write_json_atomic(
        root,
        paths["root"] / "protocol_amendment.json",
        amendment,
        work_scope="gate_b6",
    )
    write_json_atomic(
        root,
        paths["root"] / "protocol_errata.json",
        errata,
        work_scope="gate_b6",
    )
    smoke = collect_environment_smoke_reports(root, config, environments)
    determinism = collect_determinism_reports(root, config, environments)
    write_json_atomic(root, paths["environment_smoke_report"], smoke, work_scope="gate_b6")
    write_json_atomic(root, paths["determinism_report"], determinism, work_scope="gate_b6")
    ready = all(record["status"] == "READY" for record in environments["environments"].values())
    ready = ready and external["status"] == "EXCLUDED_GOVERNANCE_USER_WITHDRAWAL"
    return {
        "phase": "preflight",
        "status": "PASS" if ready else "PENDING_ENVIRONMENTS",
        "registry_sha256": registry["registry_sha256"],
        "job_manifest_sha256": jobs["job_manifest_sha256"],
        "models": len(executable),
        "registry_models": registry["model_count"],
        "jobs": sum(str(job["model_id"]) in executable for job in jobs["jobs"]),
        "historical_frozen_jobs": jobs["job_count"],
        "excluded_models": amendment["excluded_model_ids"],
        "environment_status": {
            key: value["status"] for key, value in environments["environments"].items()
        },
        "external_model_status": external["status"],
        "historical_validation_loaded": False,
        "current_test_loaded": False,
    }


def dispatch_gate_b6_workers(
    root: Path, config: Mapping[str, Any], *, phase: str
) -> dict[str, Any]:
    """Run pending model shards in their declared isolated interpreters.

    The parent process never imports environment-specific libraries.  A worker
    is considered safely complete only when its durable status is PASS or a
    preregistered candidate has a scientific MODEL_EXECUTION rejection.  A
    missing status, protocol failure, or frozen-comparator failure aborts the
    orchestration.
    """

    if phase not in {"screen", "robustness"}:
        raise ValueError(f"Unsupported worker phase: {phase}")
    registry = _load_json(root, config["artifacts"]["model_registry"])
    environment = _load_json(root, config["artifacts"]["environment_manifest"])
    executable = executable_model_ids(root, registry)
    registry_status = {str(item["model_id"]): str(item["status"]) for item in registry["models"]}
    if phase == "screen":
        model_ids = sorted(executable)
    else:
        screening_path = resolve_repo_path(root, config["artifacts"]["screening_register"])
        if not screening_path.is_file():
            raise ContractViolation("Temporal screening register is required before robustness workers")
        screening = pd.read_csv(screening_path)
        model_ids = sorted(
            screening.loc[screening["advanced_to_robustness"].astype(bool), "model_id"].astype(str)
        )
    results: list[dict[str, Any]] = []
    for model_id in model_ids:
        spec = model_spec_from_registry(registry, model_id)
        environment_record = environment["environments"].get(spec.environment_id, {})
        if environment_record.get("status") != "READY":
            raise ContractViolation(f"Environment is not READY for {model_id}: {spec.environment_id}")
        artifact_root = resolve_repo_path(root, config["artifacts"]["root"])
        status_path = (
            artifact_root
            / "worker_status"
            / phase
            / spec.environment_id
            / f"{model_id}.json"
        )
        existing = (
            json.loads(status_path.read_text(encoding="utf-8")) if status_path.is_file() else {}
        )
        existing_status = str(existing.get("status", "MISSING"))
        reusable = existing_status == "PASS" or (
            existing_status == "REJECTED_MODEL_EXECUTION"
            and registry_status[model_id] == "PREREGISTERED_CANDIDATE"
        )
        if reusable:
            results.append({"model_id": model_id, "status": existing_status, "action": "REUSED"})
            continue
        python_path = resolve_repo_path(root, environment_record["python_executable"])
        command = [
            str(python_path),
            str(root / "scripts" / "run_gate_b6_worker.py"),
            "--environment-id",
            spec.environment_id,
            "--phase",
            phase,
            "--model-id",
            model_id,
        ]
        completed = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=172800,
            check=False,
        )
        status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.is_file() else {}
        worker_status = str(status.get("status", "MISSING"))
        allowed_rejection = (
            worker_status == "REJECTED_MODEL_EXECUTION"
            and registry_status[model_id] == "PREREGISTERED_CANDIDATE"
        )
        if worker_status != "PASS" and not allowed_rejection:
            raise ContractViolation(
                f"Worker {model_id}/{phase} failed with {worker_status}; "
                f"stderr tail: {completed.stderr[-1000:]}"
            )
        results.append(
            {
                "model_id": model_id,
                "status": worker_status,
                "action": "EXECUTED",
                "returncode": completed.returncode,
            }
        )
    return {
        "phase": f"{phase}_workers",
        "status": "PASS",
        "models": len(model_ids),
        "executed": sum(item["action"] == "EXECUTED" for item in results),
        "reused": sum(item["action"] == "REUSED" for item in results),
        "results": results,
        "historical_validation_loaded": False,
        "current_test_loaded": False,
    }


def protocol_errata(
    config: Mapping[str, Any], registry: Mapping[str, Any], *, root: Path | None = None
) -> dict[str, Any]:
    declared = sorted(map(str, config["environments"]["b6_torch"].get("accelerator_models", [])))
    actual = sorted(
        str(model["model_id"])
        for model in registry["models"]
        if model["environment_id"] == "b6_torch"
    )
    records: list[dict[str, Any]] = []
    if declared != actual:
        records.append(
            {
                "erratum_id": "B6-ERRATUM-001",
                "status": "RECORDED_NON_OPERATIVE_METADATA",
                "detected_stage": "after_B5_freeze_before_B6_screen_aggregation",
                "field": "environments.b6_torch.accelerator_models",
                "declared_values": declared,
                "authoritative_values": actual,
                "runtime_authority": "models[].environment_id expanded into model_registry.json",
                "scientific_protocol_changed": False,
                "grids_changed": False,
                "data_boundary_changed": False,
                "selection_rule_changed": False,
                "impact": "none; the non-authoritative convenience list is not consumed by workers",
            }
        )
    tabpfn = next(
        (model for model in registry["models"] if model["model_id"] == "Z15_tabpfn_v2_6"),
        None,
    )
    if tabpfn is not None and tabpfn["feature_view"] == "SAFE_ALL":
        records.append(
            {
                "erratum_id": "B6-ERRATUM-003",
                "status": "RECORDED_AMBIGUITY_RESOLUTION",
                "detected_stage": "B6_implementation_review",
                "field": "Z15_tabpfn_v2_6.feature_view",
                "ambiguous_text": "feature-view narrative mentioned native categorical handling while the explicit model-zoo row specified SAFE_ALL",
                "authoritative_value": "SAFE_ALL",
                "runtime_authority": "frozen configs/gate_b6.yaml models[].feature_view and model_registry.json",
                "categorical_handling": "train-fitted SAFE_ALL numeric preprocessing; identifiers remain excluded",
                "superseded_for_execution_by": "B6-GOV-001",
                "runtime_relevance_after_amendment": "none; model excluded before license, weights, predictions, or scoring",
                "scientific_protocol_changed": False,
                "grids_changed": False,
                "data_boundary_changed": False,
                "impact": "none; the historical per-model specification resolves the frozen record and B6-GOV-001 removes runtime use",
            }
        )
    if root is not None:
        method_cards_path = (
            root / "artifacts" / "model_selection" / "t1_b5_evidence_v1" / "method_cards.json"
        )
        if method_cards_path.is_file():
            methods = json.loads(method_cards_path.read_text(encoding="utf-8"))["methods"]
            observed_maxima = {
                int(item["observed_geometry"]["campaign_interval_days_max"])
                for item in methods
            }
            train = load_split_dataset("t1", "train", root=root)
            horizons = pd.to_numeric(train.frame["forecast_horizon_days"], errors="raise")
            if observed_maxima == {560} and int(horizons.max()) == 210:
                records.append(
                    {
                        "erratum_id": "B6-ERRATUM-002",
                        "status": "RECORDED_SEMANTIC_CLARIFICATION",
                        "detected_stage": "B6_report_preparation",
                        "field": "B5 method_cards.methods[].observed_geometry.campaign_interval_days_max",
                        "stored_value": 560,
                        "stored_value_meaning": "maximum point-level inter-observation gap after skipped campaigns",
                        "one_step_forecast_horizon_days_min": int(horizons.min()),
                        "one_step_forecast_horizon_days_max": int(horizons.max()),
                        "scientific_protocol_changed": False,
                        "eligibility_decision_changed": False,
                        "impact": "none; the longer observed gap strengthens rather than weakens the exclusion rationale",
                    }
                )
    return {
        "schema_version": 1,
        "status": "RECORDED" if records else "NO_ERRATA",
        "records": records,
    }


def environment_manifest(root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    records: dict[str, Any] = {}
    for environment_id in config["environments"]:
        settings = effective_environment_settings(root, config, environment_id)
        lock_path = resolve_repo_path(root, settings["lock"])
        expected = _requirements_from_lock(lock_path)
        environment_python = root / "work" / "environments" / environment_id / "Scripts" / "python.exe"
        capture_root = root / "work" / "environment_staging" / environment_id
        durable_capture_root = (
            root
            / "artifacts"
            / "model_selection"
            / "t1_b6_expanded_v1"
            / "environments"
            / environment_id
        )
        freeze_path = durable_capture_root / "pip_freeze.txt"
        wheel_report_path = durable_capture_root / "pip_wheel_report.json"
        installed = _installed_versions(environment_python, expected) if environment_python.is_file() else {}
        runtime = _environment_runtime_capture(environment_python) if environment_python.is_file() else {}
        exact = bool(installed) and all(
            _installed_version_matches(
                name,
                installed.get(name.lower()),
                version,
                cuda_wheel_index=settings.get("cuda_wheel_index"),
            )
            for name, version in expected.items()
        )
        python_match = str(runtime.get("python", "")).startswith(f"{settings['python']}.")
        wheel_records = _wheel_records(wheel_report_path)
        wheel_hashes_complete = bool(wheel_records) and all(record.get("sha256") for record in wheel_records)
        cuda_contract = True
        if settings.get("cuda_wheel_index"):
            cuda_contract = bool(
                str(runtime.get("torch_cuda", "")).startswith("13.0")
                and runtime.get("torch_cuda_available") is True
                and any(
                    str(item.get("package", "")).lower() == "torch"
                    and "/cu130/" in str(item.get("url", ""))
                    for item in wheel_records
                )
            )
        if (
            environment_python.is_file()
            and exact
            and python_match
            and cuda_contract
            and freeze_path.is_file()
            and wheel_hashes_complete
        ):
            status = "READY"
        elif environment_python.is_file():
            status = "ENVIRONMENT_PRESENT_BUT_CONTRACT_INCOMPLETE"
        else:
            status = "NOT_STAGED"
        records[environment_id] = {
            "environment_id": environment_id,
            "status": status,
            "python_version_required": str(settings["python"]),
            "python_executable": environment_python.relative_to(root).as_posix(),
            "lock_path": lock_path.relative_to(root).as_posix(),
            "lock_sha256": sha256_file(lock_path),
            "expected_packages": expected,
            "installed_packages": installed,
            "exact_required_versions_match": exact,
            "python_runtime_match": python_match,
            "runtime": runtime,
            "pip_freeze_path": freeze_path.relative_to(root).as_posix(),
            "pip_freeze_sha256": sha256_file(freeze_path) if freeze_path.is_file() else None,
            "wheel_records": wheel_records,
            "wheel_hashes_complete": wheel_hashes_complete,
            "cpu_only": bool(settings.get("cpu_only", False)),
            "cuda_wheel_index": settings.get("cuda_wheel_index"),
            "cuda_contract_match": cuda_contract,
        }
    return {
        "schema_version": 1,
        "hardware_contract": _hardware_capture(root),
        "environments": records,
    }


def external_model_manifest(root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    """Prove that the governance-excluded TabPFN model has no local weights."""

    policy = config["external_model_policy"]["tabpfn"]
    cache_root = resolve_repo_path(root, policy["cache_root"])
    entries = (
        sorted(
            path.relative_to(cache_root).as_posix()
            for path in cache_root.rglob("*")
            if path.is_file() and path.name != ".gitkeep"
        )
        if cache_root.is_dir()
        else []
    )
    status = (
        "EXCLUDED_GOVERNANCE_USER_WITHDRAWAL"
        if not entries
        else "FAIL_FORBIDDEN_EXTERNAL_MODEL_FILES_PRESENT"
    )
    return {
        "schema_version": 1,
        "model_id": "Z15_tabpfn_v2_6",
        "family": "local_tabpfn",
        "model_version": "v2.6",
        "status": status,
        "scientific_status": "NOT_EVALUATED",
        "license_status": "NOT_ACCEPTED_USER_WITHDREW_MODEL",
        "license_marker_present": "LICENSE_ACCEPTED" in entries,
        "weights_downloaded": any(path.lower().endswith(".ckpt") for path in entries),
        "cache_entries": entries,
        "relative_path": None,
        "size_bytes": None,
        "sha256": None,
        "execution_allowed": False,
        "staging_allowed": False,
        "runtime_network_access": "PROHIBITED",
        "api_mode": "PROHIBITED",
        "governance_amendment": AMENDMENT_RELATIVE_PATH.as_posix(),
    }


def run_gate_b6_screen(root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    registry = _load_json(root, config["artifacts"]["model_registry"])
    executable = executable_model_ids(root, registry)
    excluded = excluded_model_records(root)
    predictions, tuning, oof, statuses, inventory = collect_worker_outputs(
        root, config, registry, phase="screen", allowed_model_ids=executable
    )
    paths = _paths(root, config)
    bundle = load_canonical_bundle(root)
    train = load_split_dataset("t1", "train", root=root)
    zone_map, _ = build_spatial_zone_map(bundle)
    source = attach_spatial_zones(train, zone_map)
    assignments = pd.read_csv(
        root / "artifacts" / "splits" / "t1_train_benchmark_v1" / "outer_assignments.csv",
        keep_default_na=False,
    )
    fold = temporal_fold_metrics(predictions, source.frame, assignments)
    aggregate = temporal_aggregate_metrics(predictions, fold)
    screen = screening_register(
        aggregate,
        fold,
        registry,
        statuses,
        config,
        excluded_models=excluded,
    )
    selected = global_parameter_registry(tuning, registry)
    selected["model_registry_sha256"] = registry["registry_sha256"]
    selected["selection_data"] = ["t1_v1/train/inner_forward_folds"]
    selected["historical_validation_used"] = False
    selected["current_test_used"] = False
    rejection = screen.loc[~screen["screen_passed"].astype(bool)].copy()
    write_csv_atomic(root, paths["temporal_predictions"], predictions, work_scope="gate_b6")
    write_csv_atomic(root, paths["temporal_fold_metrics"], fold, work_scope="gate_b6")
    write_csv_atomic(root, paths["temporal_aggregate_metrics"], aggregate, work_scope="gate_b6")
    write_csv_atomic(root, paths["tuning_results"], tuning, work_scope="gate_b6")
    write_json_atomic(root, paths["selected_parameter_registry"], selected, work_scope="gate_b6")
    write_csv_atomic(root, paths["screening_register"], screen, work_scope="gate_b6")
    write_csv_atomic(root, paths["rejection_register"], rejection, work_scope="gate_b6")
    write_csv_atomic(root, paths["prediction_shard_inventory"], inventory, work_scope="gate_b6")
    complete = bool(~screen["screen_status"].astype(str).str.startswith("FAIL_PROTOCOL").any())
    return {
        "phase": "screen",
        "status": "PASS" if complete else "FAIL_PROTOCOL",
        "models": len(screen),
        "models_executed": len(executable),
        "models_excluded_governance": len(excluded),
        "advanced_to_robustness": int(screen["advanced_to_robustness"].sum()),
        "rejected_temporal_screen": int(screen["screen_status"].eq("REJECTED_TEMPORAL_SCREEN").sum()),
        "prediction_rows": len(predictions),
        "historical_validation_loaded": False,
        "current_test_loaded": False,
    }


def run_gate_b6_robustness(root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    paths = _paths(root, config)
    registry = _load_json(root, config["artifacts"]["model_registry"])
    screen = pd.read_csv(paths["screening_register"])
    allowed = set(screen.loc[screen["advanced_to_robustness"].astype(bool), "model_id"].astype(str))
    predictions, tuning, oof, statuses, inventory = collect_worker_outputs(
        root, config, registry, phase="robustness", allowed_model_ids=allowed
    )
    successful = {model_id for model_id, status in statuses.items() if status.get("status") == "PASS"}
    group = robustness_group_metrics(predictions)
    temporal = pd.read_csv(paths["temporal_predictions"])
    transition = segmented_metrics(pd.concat([temporal, predictions], ignore_index=True, sort=False))
    learning_shards = collect_learning_curve_shards(root, config, registry, successful)
    learning = learning_curve_metrics(learning_shards)
    write_csv_atomic(root, paths["robustness_predictions"], predictions, work_scope="gate_b6")
    write_csv_atomic(root, paths["group_metrics"], group, work_scope="gate_b6")
    write_csv_atomic(root, paths["transition_metrics"], transition, work_scope="gate_b6")
    write_csv_atomic(root, paths["learning_curves"], learning, work_scope="gate_b6")
    existing_tuning = pd.read_csv(paths["tuning_results"])
    write_csv_atomic(
        root,
        paths["tuning_results"],
        pd.concat([existing_tuning, tuning], ignore_index=True)
        .drop_duplicates()
        .reset_index(drop=True),
        work_scope="gate_b6",
    )
    # Preserve the screen inventory and add the robustness shards.
    current_inventory = pd.read_csv(paths["prediction_shard_inventory"])
    write_csv_atomic(
        root,
        paths["prediction_shard_inventory"],
        pd.concat([current_inventory, inventory], ignore_index=True)
        .drop_duplicates(["phase", "model_id", "environment_id", "kind", "path"], keep="last")
        .sort_values(["phase", "model_id", "kind"], kind="mergesort")
        .reset_index(drop=True),
        work_scope="gate_b6",
    )
    registry_status = {item["model_id"]: item["status"] for item in registry["models"]}
    protocol_failure = any(
        status.get("status") in {"FAIL_PROTOCOL", "MISSING"}
        or (
            registry_status.get(model_id) == "FROZEN_COMPARATOR"
            and status.get("status") != "PASS"
        )
        for model_id, status in statuses.items()
    )
    complete = set(statuses) == allowed and not protocol_failure
    spatial_rejections = [
        {
            "model_id": model_id,
            "screen_status": "REJECTED_SPATIAL_MODEL_EXECUTION",
            "reason": "one_or_more_preregistered_spatial_folds_failed_model_execution",
        }
        for model_id, status in sorted(statuses.items())
        if status.get("status") == "REJECTED_MODEL_EXECUTION"
    ]
    spatial_guardrail_rejections = [
        {
            "model_id": model_id,
            "screen_status": "REJECTED_SUITE_ELIGIBILITY_SPATIAL_INNER_GUARDRAIL",
            "reason": "one_or_more_spatial_inner_folds_used_diagnostic_no_eligible_candidate_fallback",
        }
        for model_id, status in sorted(statuses.items())
        if registry_status.get(model_id) == "PREREGISTERED_CANDIDATE"
        and any(
            str(item.get("selection_status", "")).startswith("DIAGNOSTIC_FALLBACK")
            for item in status.get("inner_selection_rejections", [])
        )
    ]
    if spatial_rejections or spatial_guardrail_rejections:
        rejection = pd.read_csv(paths["rejection_register"])
        write_csv_atomic(
            root,
            paths["rejection_register"],
            pd.concat(
                [
                    rejection,
                    pd.DataFrame(spatial_rejections + spatial_guardrail_rejections),
                ],
                ignore_index=True,
                sort=False,
            ).drop_duplicates(["model_id", "screen_status", "reason"], keep="last"),
            work_scope="gate_b6",
        )
    return {
        "phase": "robustness",
        "status": "PASS" if complete else "FAIL_PROTOCOL",
        "models": len(allowed),
        "models_completed_spatial": len(successful),
        "models_rejected_spatial_execution": len(spatial_rejections),
        "models_disqualified_by_spatial_inner_guardrail": len(spatial_guardrail_rejections),
        "prediction_rows": len(predictions),
        "group_metric_rows": len(group),
        "transition_metric_rows": len(transition),
        "learning_curve_rows": len(learning),
        "historical_validation_loaded": False,
        "current_test_loaded": False,
    }


def run_gate_b6_calibration(root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    paths = _paths(root, config)
    temporal = pd.read_csv(paths["temporal_predictions"])
    robustness = pd.read_csv(paths["robustness_predictions"])
    predictions = pd.concat([temporal, robustness], ignore_index=True, sort=False)
    oof = collect_selected_inner_oof(root, config)
    calibrated, parameters = calibrate_predictions(
        predictions,
        oof,
        levels=tuple(map(float, config["conformal"]["levels"])),
        scale_clip=tuple(map(float, config["conformal"]["scale_clip"])),
    )
    probabilistic = probabilistic_metric_table(calibrated)
    screen = pd.read_csv(paths["screening_register"])
    advanced = list(screen.loc[screen["advanced_to_robustness"].astype(bool), "model_id"].astype(str))
    sensitivity, jackknife = paired_sensitivity_tables(
        temporal,
        advanced,
        replicates=int(config["paired_sensitivity"]["replicates"]),
        seed=int(config["paired_sensitivity"]["seed"]),
    )
    write_csv_atomic(root, paths["calibration_predictions"], calibrated, work_scope="gate_b6")
    write_json_atomic(
        root,
        paths["calibration_parameters"],
        {"schema_version": 1, "calibrations": parameters, "outer_labels_used_for_calibration": False},
        work_scope="gate_b6",
    )
    write_csv_atomic(root, paths["probabilistic_metrics"], probabilistic, work_scope="gate_b6")
    write_csv_atomic(root, paths["paired_sensitivity"], sensitivity, work_scope="gate_b6")
    write_csv_atomic(root, paths["jackknife"], jackknife, work_scope="gate_b6")
    return {
        "phase": "calibrate",
        "status": "PASS",
        "prediction_rows": len(calibrated),
        "calibration_records": len(parameters),
        "probabilistic_metric_rows": len(probabilistic),
        "outer_labels_used_for_calibration": False,
    }


def run_gate_b6_freeze(root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    paths = _paths(root, config)
    registry = _load_json(root, config["artifacts"]["model_registry"])
    screen = pd.read_csv(paths["screening_register"])
    temporal_aggregate = pd.read_csv(paths["temporal_aggregate_metrics"])
    temporal_fold = pd.read_csv(paths["temporal_fold_metrics"])
    group = pd.read_csv(paths["group_metrics"])
    transition = pd.read_csv(paths["transition_metrics"])
    probabilistic = pd.read_csv(paths["probabilistic_metrics"])
    learning = pd.read_csv(paths["learning_curves"])
    amendment = protocol_amendment_payload(root, registry)
    advanced = set(
        screen.loc[screen["advanced_to_robustness"].astype(bool), "model_id"].astype(str)
    )
    robustness_guardrail_failures = _robustness_guardrail_failures(
        root, config, registry, advanced
    )
    eligibility = suite_v4_eligibility(
        screen,
        temporal_aggregate,
        temporal_fold,
        group,
        transition,
        probabilistic,
        learning,
        config,
        robustness_guardrail_failures=robustness_guardrail_failures,
    )
    eligible = eligibility.loc[eligibility["eligible"].astype(bool)].copy()
    if eligible.empty:
        primary = str(config["suite_v4_eligibility"]["fallback_primary"])
        status = str(config["suite_v4_eligibility"]["pass_fallback_status"])
    else:
        order = [
            "rolling_mae",
            "transition_mae",
            "worst_zone_mae",
            "conformal_95_weighted_interval_score",
            "fit_time",
            "model_id",
        ]
        primary = str(eligible.sort_values(order, kind="mergesort").iloc[0]["model_id"])
        status = str(config["suite_v4_eligibility"]["pass_new_status"])
    selected_parameters = _load_json(root, config["artifacts"]["selected_parameter_registry"])
    bundle = load_canonical_bundle(root)
    train = load_split_dataset("t1", "train", root=root)
    source_hashes = _suite_source_hashes(root, config, paths)
    predecessor_relative_path = "artifacts/governance/final_candidate_suite_v3.json"
    predecessor_path = resolve_repo_path(root, predecessor_relative_path)
    predecessor_payload = _load_json(root, predecessor_relative_path)
    predecessor_suite = {
        "relative_path": predecessor_relative_path,
        "sha256": sha256_file(predecessor_path),
        "suite_id": predecessor_payload["suite_id"],
        "primary_model_id": predecessor_payload["primary_model_id"],
        "immutable": True,
    }
    shortlist = build_internal_shortlist(
        primary,
        status,
        eligibility,
        temporal_aggregate,
        probabilistic,
    )
    write_json_atomic(root, paths["internal_shortlist"], shortlist, work_scope="gate_b6")
    suite_models = []
    for model_id in (
        "B1_persistence_last_rate",
        "B5_fixed_kalman",
        "B6_adaptive_kalman",
        "B7_two_regime_imm",
        "B8_student_t_robust_imm",
    ):
        record = next(item for item in registry["models"] if item["model_id"] == model_id)
        suite_models.append(
            {
                "model_id": model_id,
                "role": "primary" if model_id == primary else "context_only_comparator",
                "model_spec_sha256": record["spec_sha256"],
                "parameters": selected_parameters["models"][model_id]["parameters"],
            }
        )
    for model_id in shortlist.get("context_only_models", []):
        if model_id in {item["model_id"] for item in suite_models}:
            continue
        record = next(item for item in registry["models"] if item["model_id"] == model_id)
        suite_models.append(
            {
                "model_id": model_id,
                "role": "primary" if model_id == primary else "context_only",
                "model_spec_sha256": record["spec_sha256"],
                "parameters": selected_parameters["models"][model_id]["parameters"],
            }
        )
    if primary not in {item["model_id"] for item in suite_models}:
        record = next(item for item in registry["models"] if item["model_id"] == primary)
        suite_models.append(
            {
                "model_id": primary,
                "role": "primary",
                "model_spec_sha256": record["spec_sha256"],
                "parameters": selected_parameters["models"][primary]["parameters"],
            }
        )
    for item in suite_models:
        item["role"] = "primary" if item["model_id"] == primary else item["role"]
    suite = {
        "schema_version": 1,
        "status": status,
        "scientific_scope": "train_only_internal_research",
        "primary_model_id": primary,
        "primary_count": sum(item["role"] == "primary" for item in suite_models),
        "primary_selection_rule": "preregistered hard eligibility gates then lexicographic ordering; B7 fallback",
        "primary_selected_from_holdout": False,
        "new_holdout_seen": False,
        "context_results_can_change_primary_after_holdout": False,
        "models": suite_models,
        "training_data": ["t1_v1/train"],
        "train_sample_ids_sha256": train.provenance.sample_ids_sha256,
        "feature_contract_sha256": bundle.feature_contract.source_sha256,
        "target_contract_sha256": bundle.target_contract.source_sha256,
        "predecessor_suite": predecessor_suite,
        "source_hashes": source_hashes,
        "eligibility_records": eligibility.to_dict(orient="records"),
        "governance_amendment": {
            "amendment_id": amendment["amendment"]["amendment_id"],
            "source": amendment["amendment_source"],
            "source_sha256": amendment["amendment_source_sha256"],
            "excluded_model_ids": amendment["excluded_model_ids"],
            "selection_evidence_used_for_exclusion": False,
        },
    }
    full_train = freeze_full_train_primary(root, config, suite, registry, selected_parameters)
    suite_digest = canonical_json_sha256(
        {
            "primary": primary,
            "models": suite_models,
            "sources": source_hashes,
            "predecessor_suite": predecessor_suite,
            "train": train.provenance.sample_ids_sha256,
            "full_train_primary": full_train,
        }
    )
    suite["suite_id"] = f"t1-final-suite-v4-{suite_digest[:12]}"
    suite["full_train_primary"] = full_train
    write_json_atomic(root, paths["suite_v4"], suite, work_scope="gate_b6")
    report = {
        "schema_version": 1,
        "gate": config["gate"],
        "status": status,
        "scientific_scope": "train_only_internal_research",
        "final_quality_claim_allowed": False,
        "primary_model_id": primary,
        "suite_id": suite["suite_id"],
        "new_models_eligible": int(eligibility["eligible"].sum()),
        "registry_models": len(screen),
        "models_screened": int(
            (~screen["screen_status"].astype(str).str.startswith("EXCLUDED_GOVERNANCE")).sum()
        ),
        "models_excluded_governance": int(
            screen["screen_status"].astype(str).str.startswith("EXCLUDED_GOVERNANCE").sum()
        ),
        "excluded_model_ids": amendment["excluded_model_ids"],
        "models_advanced": int(screen["advanced_to_robustness"].sum()),
        "historical_validation_loaded": False,
        "current_t1_test_loaded": False,
        "new_holdout_seen": False,
        "full_train_primary": full_train,
    }
    write_json_atomic(root, paths["gate_report"], report, work_scope="gate_b6")
    return {"phase": "freeze", "status": status, "primary_model_id": primary, "suite_id": suite["suite_id"]}


def run_gate_b6_validation(root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    paths = _paths(root, config)
    checks: list[dict[str, Any]] = []

    def check(name: str, condition: bool, observed: Any = None) -> None:
        checks.append({"check": name, "passed": bool(condition), "observed": observed})

    suite = _load_json(root, config["artifacts"]["suite_v4"])
    report = _load_json(root, config["artifacts"]["gate_report"])
    registry = _load_json(root, config["artifacts"]["model_registry"])
    executable = executable_model_ids(root, registry)
    excluded = excluded_model_records(root)
    amendment_current = protocol_amendment_payload(root, registry)
    predecessor_suite = suite.get("predecessor_suite", {})
    predecessor_relative_path = predecessor_suite.get("relative_path")
    predecessor_path = (
        resolve_repo_path(root, predecessor_relative_path)
        if isinstance(predecessor_relative_path, str)
        else None
    )
    check(
        "suite_v4_links_immutable_suite_v3",
        predecessor_suite.get("immutable") is True
        and predecessor_relative_path
        == "artifacts/governance/final_candidate_suite_v3.json"
        and predecessor_path is not None
        and predecessor_path.is_file()
        and sha256_file(predecessor_path) == predecessor_suite.get("sha256"),
        predecessor_suite,
    )
    job_manifest = _load_json(root, config["artifacts"]["frozen_job_manifest"])
    assert_train_only_worker_job(job_manifest)
    screen_temporary, screen_tuning, _, screen_statuses, _ = collect_worker_outputs(
        root, config, registry, phase="screen", allowed_model_ids=executable
    )
    screen_saved = pd.read_csv(paths["screening_register"])
    advanced = set(
        screen_saved.loc[screen_saved["advanced_to_robustness"].astype(bool), "model_id"].astype(str)
    )
    robustness_temporary, _, _, robustness_statuses, _ = collect_worker_outputs(
        root, config, registry, phase="robustness", allowed_model_ids=advanced
    )
    temporal = pd.read_csv(paths["temporal_predictions"])
    robustness = pd.read_csv(paths["robustness_predictions"])
    calibrated = pd.read_csv(paths["calibration_predictions"])
    PredictionBundle.validate(temporal)
    PredictionBundle.validate(robustness)
    expected_models = {str(item["model_id"]) for item in registry["models"]}
    check(
        "screen_status_exact_executable_catalog",
        set(screen_statuses) == executable,
        sorted(screen_statuses),
    )
    check(
        "screen_register_preserves_full_historical_catalog",
        set(screen_saved["model_id"].astype(str)) == expected_models,
        sorted(screen_saved["model_id"].astype(str)),
    )
    excluded_rows = screen_saved.loc[screen_saved["model_id"].isin(excluded)]
    check(
        "governance_exclusion_is_not_scored_or_advanced",
        len(excluded_rows) == len(excluded)
        and excluded_rows["screen_status"].eq("EXCLUDED_GOVERNANCE_USER_WITHDRAWAL").all()
        and not excluded_rows["screen_passed"].astype(bool).any()
        and not excluded_rows["advanced_to_robustness"].astype(bool).any(),
        excluded_rows.to_dict(orient="records"),
    )
    registry_status = {item["model_id"]: item["status"] for item in registry["models"]}
    screen_protocol_clean = all(
        record.get("status") == "PASS"
        or (
            registry_status.get(model_id) == "PREREGISTERED_CANDIDATE"
            and record.get("status") == "REJECTED_MODEL_EXECUTION"
        )
        for model_id, record in screen_statuses.items()
    )
    check(
        "screen_has_no_protocol_worker_failure",
        screen_protocol_clean,
        {key: value.get("status") for key, value in screen_statuses.items()},
    )
    worker_source_sha256 = sha256_file(root / "src" / "skru1" / "b6_worker.py")
    check(
        "screen_workers_match_current_source_sha256",
        all(
            record.get("worker_source_sha256") == worker_source_sha256
            for record in screen_statuses.values()
        ),
        {
            model_id: record.get("worker_source_sha256")
            for model_id, record in screen_statuses.items()
        },
    )
    check(
        "robustness_worker_set_exact",
        set(robustness_statuses) == advanced,
        {"expected": sorted(advanced), "observed": sorted(robustness_statuses)},
    )
    robustness_protocol_clean = all(
        record.get("status") not in {"FAIL_PROTOCOL", "MISSING"}
        and not (
            registry_status.get(model_id) == "FROZEN_COMPARATOR"
            and record.get("status") != "PASS"
        )
        for model_id, record in robustness_statuses.items()
    )
    check(
        "robustness_workers_protocol_clean",
        robustness_protocol_clean,
        {key: value.get("status") for key, value in robustness_statuses.items()},
    )
    check(
        "robustness_workers_match_current_source_sha256",
        all(
            record.get("worker_source_sha256") == worker_source_sha256
            for record in robustness_statuses.values()
        ),
        {
            model_id: record.get("worker_source_sha256")
            for model_id, record in robustness_statuses.items()
        },
    )
    check(
        "saved_temporal_predictions_match_validated_shards",
        _dataframes_equivalent(
            temporal,
            screen_temporary,
            keys=("model_id", "fold_id", "aggregation", "seed", "sample_id"),
        ),
        {"saved_rows": len(temporal), "shard_rows": len(screen_temporary)},
    )
    check(
        "saved_robustness_predictions_match_validated_shards",
        _dataframes_equivalent(
            robustness,
            robustness_temporary,
            keys=("model_id", "design", "fold_id", "aggregation", "seed", "sample_id"),
        ),
        {"saved_rows": len(robustness), "shard_rows": len(robustness_temporary)},
    )
    check("suite_has_exactly_one_primary", suite["primary_count"] == 1)
    check("suite_new_holdout_not_seen", suite["new_holdout_seen"] is False)
    check("suite_primary_not_selected_from_holdout", suite["primary_selected_from_holdout"] is False)
    check("report_validation_not_loaded", report["historical_validation_loaded"] is False)
    check("report_test_not_loaded", report["current_t1_test_loaded"] is False)
    check("temporal_predictions_no_duplicates", not temporal.duplicated(["model_id", "seed", "fold_id", "sample_id"]).any())
    check("robustness_predictions_no_duplicates", not robustness.duplicated(["model_id", "seed", "fold_id", "sample_id"]).any())
    check("all_prediction_values_finite", np.isfinite(pd.concat([temporal["y_pred"], robustness["y_pred"]])).all())
    check("conformal_calibration_hash_present", calibrated["calibration_sample_ids_sha256"].astype(str).str.len().eq(64).all())

    bundle = load_canonical_bundle(root)
    train = load_split_dataset("t1", "train", root=root)
    zone_map, _ = build_spatial_zone_map(bundle)
    source = attach_spatial_zones(train, zone_map)
    assignments = pd.read_csv(
        root / "artifacts" / "splits" / "t1_train_benchmark_v1" / "outer_assignments.csv",
        keep_default_na=False,
    )
    fold_recomputed = temporal_fold_metrics(temporal, source.frame, assignments)
    aggregate_recomputed = temporal_aggregate_metrics(temporal, fold_recomputed)
    group_recomputed = robustness_group_metrics(robustness)
    transition_recomputed = segmented_metrics(pd.concat([temporal, robustness], ignore_index=True, sort=False))
    robustness_successful = {
        model_id for model_id, status in robustness_statuses.items() if status.get("status") == "PASS"
    }
    learning_shards = collect_learning_curve_shards(root, config, registry, robustness_successful)
    learning_recomputed = learning_curve_metrics(learning_shards)
    oof = collect_selected_inner_oof(root, config)
    calibrated_recomputed, calibration_records = calibrate_predictions(
        pd.concat([temporal, robustness], ignore_index=True, sort=False),
        oof,
        levels=tuple(map(float, config["conformal"]["levels"])),
        scale_clip=tuple(map(float, config["conformal"]["scale_clip"])),
    )
    probabilistic_recomputed = probabilistic_metric_table(calibrated_recomputed)
    sensitivity_recomputed, jackknife_recomputed = paired_sensitivity_tables(
        temporal,
        sorted(advanced),
        replicates=int(config["paired_sensitivity"]["replicates"]),
        seed=int(config["paired_sensitivity"]["seed"]),
    )
    saved_fold = pd.read_csv(paths["temporal_fold_metrics"])
    saved_aggregate = pd.read_csv(paths["temporal_aggregate_metrics"])
    saved_group = pd.read_csv(paths["group_metrics"])
    saved_transition = pd.read_csv(paths["transition_metrics"])
    saved_learning = pd.read_csv(paths["learning_curves"])
    saved_probabilistic = pd.read_csv(paths["probabilistic_metrics"])
    saved_sensitivity = pd.read_csv(paths["paired_sensitivity"])
    saved_jackknife = pd.read_csv(paths["jackknife"])
    for name, saved, recomputed, keys in (
        ("temporal_fold_metrics", saved_fold, fold_recomputed, ("model_id", "fold_id")),
        ("temporal_aggregate_metrics", saved_aggregate, aggregate_recomputed, ("model_id",)),
        ("group_metrics", saved_group, group_recomputed, ("design", "model_id", "scope", "group")),
        (
            "transition_metrics",
            saved_transition,
            transition_recomputed,
            ("design", "model_id", "dimension", "segment"),
        ),
        ("learning_curves", saved_learning, learning_recomputed, ("model_id", "training_rows")),
        (
            "probabilistic_metrics",
            saved_probabilistic,
            probabilistic_recomputed,
            ("design", "model_id", "interval_source", "dimension", "segment"),
        ),
        (
            "paired_sensitivity",
            saved_sensitivity,
            sensitivity_recomputed,
            ("model_id", "reference_model_id", "cluster_column"),
        ),
        (
            "leave_one_profile_out_jackknife",
            saved_jackknife,
            jackknife_recomputed,
            ("model_id", "reference_model_id", "left_out_cluster"),
        ),
    ):
        check(
            f"{name}_recomputed_from_prediction_rows",
            _dataframes_equivalent(saved, recomputed, keys=keys),
            {"saved_rows": len(saved), "recomputed_rows": len(recomputed)},
        )
    check(
        "calibrated_predictions_recomputed_from_inner_oof",
        _dataframes_equivalent(
            calibrated,
            calibrated_recomputed,
            keys=("model_id", "design", "fold_id", "sample_id"),
        ),
        {"saved_rows": len(calibrated), "recomputed_rows": len(calibrated_recomputed)},
    )
    saved_calibration = _load_json(root, config["artifacts"]["calibration_parameters"])
    check(
        "calibration_parameters_recomputed_from_inner_oof",
        canonical_json_sha256(saved_calibration["calibrations"])
        == canonical_json_sha256(calibration_records),
        len(calibration_records),
    )

    screen_recomputed = screening_register(
        aggregate_recomputed,
        fold_recomputed,
        registry,
        screen_statuses,
        config,
        excluded_models=excluded,
    )
    check(
        "temporal_screen_reproduced",
        _dataframes_equivalent(screen_saved, screen_recomputed, keys=("model_id",)),
        len(screen_recomputed),
    )
    selected_saved = _load_json(root, config["artifacts"]["selected_parameter_registry"])
    selected_recomputed = global_parameter_registry(screen_tuning, registry)
    check(
        "global_parameters_reproduced_from_screen_inner_results",
        canonical_json_sha256(selected_saved["models"])
        == canonical_json_sha256(selected_recomputed["models"]),
        len(selected_recomputed["models"]),
    )

    eligibility_recomputed = suite_v4_eligibility(
        screen_recomputed,
        aggregate_recomputed,
        fold_recomputed,
        group_recomputed,
        transition_recomputed,
        probabilistic_recomputed,
        learning_recomputed,
        config,
        robustness_guardrail_failures=_robustness_guardrail_failures(
            root, config, registry, advanced
        ),
    )
    eligible = eligibility_recomputed.loc[eligibility_recomputed["eligible"].astype(bool)]
    if eligible.empty:
        reproduced_primary = str(config["suite_v4_eligibility"]["fallback_primary"])
        reproduced_status = str(config["suite_v4_eligibility"]["pass_fallback_status"])
    else:
        reproduced_primary = str(
            eligible.sort_values(
                [
                    "rolling_mae",
                    "transition_mae",
                    "worst_zone_mae",
                    "conformal_95_weighted_interval_score",
                    "fit_time",
                    "model_id",
                ],
                kind="mergesort",
            ).iloc[0]["model_id"]
        )
        reproduced_status = str(config["suite_v4_eligibility"]["pass_new_status"])
    check("suite_v4_primary_rule_reproduced", suite["primary_model_id"] == reproduced_primary, reproduced_primary)
    check("suite_v4_status_reproduced", suite["status"] == reproduced_status, reproduced_status)
    check(
        "suite_v4_eligibility_records_reproduced",
        _dataframes_equivalent(
            pd.DataFrame(suite["eligibility_records"]),
            eligibility_recomputed,
            keys=("model_id",),
        ),
        len(eligibility_recomputed),
    )

    environment_saved = _load_json(root, config["artifacts"]["environment_manifest"])
    environment_current = environment_manifest(root, config)
    check(
        "all_environments_ready",
        all(item.get("status") == "READY" for item in environment_current["environments"].values()),
        {key: value.get("status") for key, value in environment_current["environments"].items()},
    )
    check(
        "environment_contracts_unchanged",
        canonical_json_sha256(environment_saved["environments"])
        == canonical_json_sha256(environment_current["environments"]),
    )
    torch_environment = environment_current["environments"]["b6_torch"]
    torch_freeze_path = resolve_repo_path(root, torch_environment["pip_freeze_path"])
    torch_wheel_path = (
        paths["root"] / "environments" / "b6_torch" / "pip_wheel_report.json"
    )
    torch_freeze_text = (
        torch_freeze_path.read_text(encoding="utf-8").lower()
        if torch_freeze_path.is_file()
        else "missing"
    )
    torch_wheel_text = (
        torch_wheel_path.read_text(encoding="utf-8").lower()
        if torch_wheel_path.is_file()
        else "missing"
    )
    check(
        "torch_runtime_uses_governance_lock_without_tabpfn",
        torch_environment["lock_path"] == "requirements/b6_torch_runtime.lock.txt"
        and "tabpfn" not in torch_freeze_text
        and "tabpfn" not in torch_wheel_text,
        {
            "lock_path": torch_environment["lock_path"],
            "pip_freeze_contains_tabpfn": "tabpfn" in torch_freeze_text,
            "wheel_report_contains_tabpfn": "tabpfn" in torch_wheel_text,
        },
    )
    smoke = _load_json(root, config["artifacts"]["environment_smoke_report"])
    determinism = _load_json(root, config["artifacts"]["determinism_report"])
    check(
        "environment_smoke_reports_pass",
        all(
            item.get("status") == "PASS"
            and item.get("models")
            and all(model.get("status") == "PASS" for model in item["models"])
            for item in smoke["environments"].values()
        ),
        {key: value.get("status") for key, value in smoke["environments"].items()},
    )
    check(
        "two_run_determinism_reports_pass",
        all(
            item.get("status") == "PASS"
            and item.get("models")
            and all(model.get("status") == "PASS" for model in item["models"])
            for item in determinism["environments"].values()
        ),
        {key: value.get("status") for key, value in determinism["environments"].items()},
    )
    expected_by_environment = {
        environment_id: sorted(
            str(item["model_id"])
            for item in registry["models"]
            if item["environment_id"] == environment_id
            and str(item["model_id"]) in executable
        )
        for environment_id in environment_saved["environments"]
    }
    check(
        "environment_smoke_exact_executable_model_sets",
        all(
            sorted(str(item["model_id"]) for item in smoke["environments"][environment_id]["models"])
            == model_ids
            for environment_id, model_ids in expected_by_environment.items()
        ),
        expected_by_environment,
    )
    external_saved = _load_json(root, config["artifacts"]["external_model_manifest"])
    external_current = external_model_manifest(root, config)
    check(
        "tabpfn_excluded_without_license_or_weights",
        external_current["status"] == "EXCLUDED_GOVERNANCE_USER_WITHDRAWAL"
        and external_current["license_marker_present"] is False
        and external_current["weights_downloaded"] is False
        and external_current["cache_entries"] == []
        and external_current["execution_allowed"] is False
        and external_current["staging_allowed"] is False,
        external_current,
    )
    check(
        "external_model_manifest_unchanged",
        canonical_json_sha256(external_saved) == canonical_json_sha256(external_current),
    )
    amendment_saved = json.loads(
        (paths["root"] / "protocol_amendment.json").read_text(encoding="utf-8")
    )
    check(
        "governance_amendment_unchanged",
        canonical_json_sha256(amendment_saved) == canonical_json_sha256(amendment_current),
    )
    forbidden_tabpfn_shards = sorted(
        path.relative_to(paths["root"]).as_posix()
        for subtree in (
            "prediction_shards",
            "tuning_shards",
            "worker_status",
            "learning_curve_shards",
        )
        for path in (paths["root"] / subtree).rglob("*Z15_tabpfn*")
        if path.is_file()
    )
    check("tabpfn_has_no_prediction_or_worker_shards", not forbidden_tabpfn_shards, forbidden_tabpfn_shards)

    for relative, digest in suite["source_hashes"].items():
        source_path = resolve_repo_path(root, relative)
        check(
            f"suite_source_hash_current::{relative}",
            source_path.is_file() and sha256_file(source_path) == digest,
        )
    full_train = suite["full_train_primary"]
    full_train_path = resolve_repo_path(root, full_train["relative_path"])
    check(
        "full_train_primary_artifact_hash_valid",
        full_train_path.is_file() and sha256_file(full_train_path) == full_train["sha256"],
        full_train.get("relative_path"),
    )
    check(
        "full_train_primary_train_hash_valid",
        full_train.get("train_sample_ids_sha256") == train.provenance.sample_ids_sha256,
    )
    source_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            root / "src" / "skru1" / "b6_worker.py",
            root / "scripts" / "run_gate_b6_worker.py",
            root / "scripts" / "run_gate_b6_full_train_worker.py",
        )
    )
    check("worker_has_no_validation_loader", 'load_split_dataset("t1", "validation"' not in source_text)
    check("worker_has_no_test_loader", 'load_split_dataset("t1", "test"' not in source_text)
    privacy_violations = _durable_text_privacy_violations(paths["root"])
    check(
        "durable_artifacts_have_no_absolute_paths_or_secret_tokens",
        not privacy_violations,
        privacy_violations[:20],
    )
    b5_two_run_path = paths["root"] / "b5_two_run_determinism_report.json"
    b5_two_run = (
        json.loads(b5_two_run_path.read_text(encoding="utf-8"))
        if b5_two_run_path.is_file()
        else {}
    )
    check(
        "b5_two_run_outputs_byte_identical",
        b5_two_run.get("status") == "PASS"
        and b5_two_run.get("runs") == 2
        and b5_two_run.get("byte_identical_sha256_maps") is True,
        b5_two_run.get("changed_or_missing_files"),
    )
    predecessor = _load_json(
        root, "artifacts/model_selection/t1_b5_evidence_v1/protected_predecessor_snapshot.json"
    )
    b5_config = yaml.safe_load((root / "configs" / "gate_b5.yaml").read_text(encoding="utf-8"))
    check("B0_B4_predecessors_unchanged", predecessor == snapshot_paths(root, b5_config["protected_roots"]))
    failed = [item for item in checks if not item["passed"]]
    validation = {
        "schema_version": 1,
        "gate": config["gate"],
        "status": "PASS" if not failed else "FAIL_PROTOCOL",
        "summary": {"checks": len(checks), "passed": len(checks) - len(failed), "failed": len(failed)},
        "checks": checks,
        "validator_recomputed_from_prediction_rows": True,
        "recomputed_tables": [
            "temporal_fold_metrics",
            "temporal_aggregate_metrics",
            "group_metrics",
            "transition_metrics",
            "learning_curves",
            "calibrated_predictions",
            "probabilistic_metrics",
            "paired_sensitivity",
            "leave_one_profile_out_jackknife",
            "screening_register",
            "suite_v4_eligibility",
        ],
        "historical_validation_loaded": False,
        "current_test_loaded": False,
    }
    write_json_atomic(root, paths["validation_report"], validation, work_scope="gate_b6")
    artifact_root = paths["root"]
    files = sorted(
        path
        for path in artifact_root.rglob("*")
        if path.is_file() and path != paths["artifact_inventory"]
    )
    if paths["suite_v4"].is_file():
        files.append(paths["suite_v4"])
    write_csv_atomic(root, paths["artifact_inventory"], artifact_inventory(root, files), work_scope="gate_b6")
    if failed:
        raise ContractViolation(f"Gate B6 validation failed: {[item['check'] for item in failed]}")
    return {"phase": "validate", "status": "PASS", "checks": len(checks), "failed": 0}


def collect_worker_outputs(
    root: Path,
    config: Mapping[str, Any],
    registry: Mapping[str, Any],
    *,
    phase: str,
    allowed_model_ids: set[str] | None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, Any], pd.DataFrame]:
    artifact_root = resolve_repo_path(root, config["artifacts"]["root"])
    job_manifest = _load_json(root, config["artifacts"]["frozen_job_manifest"])
    expected_models = {
        item["model_id"]
        for item in registry["models"]
        if allowed_model_ids is None or item["model_id"] in allowed_model_ids
    }
    predictions: list[pd.DataFrame] = []
    tuning: list[pd.DataFrame] = []
    oof: list[pd.DataFrame] = []
    statuses: dict[str, Any] = {}
    inventory_rows: list[dict[str, Any]] = []
    for model_id in sorted(expected_models):
        spec = model_spec_from_registry(registry, model_id)
        base = artifact_root / "prediction_shards" / phase / spec.environment_id / f"{model_id}.csv"
        tune = artifact_root / "tuning_shards" / phase / spec.environment_id / f"{model_id}.csv"
        inner = artifact_root / "tuning_shards" / phase / spec.environment_id / f"{model_id}__selected_inner_oof.csv"
        status_path = artifact_root / "worker_status" / phase / spec.environment_id / f"{model_id}.json"
        if not status_path.is_file():
            statuses[model_id] = {"status": "MISSING", "model_id": model_id}
            continue
        status = json.loads(status_path.read_text(encoding="utf-8"))
        statuses[model_id] = status
        for kind, path in (("predictions", base), ("tuning", tune), ("inner_oof", inner), ("status", status_path)):
            if path.is_file():
                inventory_rows.append(
                    {
                        "phase": phase,
                        "model_id": model_id,
                        "environment_id": spec.environment_id,
                        "kind": kind,
                        "path": path.relative_to(root).as_posix(),
                        "bytes": path.stat().st_size,
                        "sha256": sha256_file(path),
                        "worker_status": status.get("status"),
                    }
                )
        if status.get("status") != "PASS" or not base.is_file():
            continue
        frame = pd.read_csv(base)
        PredictionBundle.validate(
            frame,
            expected_environment_id=spec.environment_id,
            expected_model_id=model_id,
        )
        _validate_fold_completeness(frame, model_id, spec, phase, job_manifest)
        predictions.append(frame)
        if tune.is_file():
            tuning.append(pd.read_csv(tune))
        if inner.is_file():
            oof.append(pd.read_csv(inner))
    if not predictions:
        raise ContractViolation(f"No Gate B6 {phase} prediction shards are available")
    return (
        pd.concat(predictions, ignore_index=True, sort=False),
        pd.concat(tuning, ignore_index=True, sort=False) if tuning else pd.DataFrame(),
        pd.concat(oof, ignore_index=True, sort=False) if oof else pd.DataFrame(),
        statuses,
        pd.DataFrame(inventory_rows),
    )


def collect_selected_inner_oof(root: Path, config: Mapping[str, Any]) -> pd.DataFrame:
    artifact_root = resolve_repo_path(root, config["artifacts"]["root"])
    frames = [pd.read_csv(path) for path in sorted(artifact_root.glob("tuning_shards/*/*/*__selected_inner_oof.csv"))]
    if not frames:
        raise ContractViolation("No selected inner OOF shards are available for conformal calibration")
    return pd.concat(frames, ignore_index=True, sort=False)


def collect_learning_curve_shards(
    root: Path,
    config: Mapping[str, Any],
    registry: Mapping[str, Any],
    allowed: set[str],
) -> pd.DataFrame:
    artifact_root = resolve_repo_path(root, config["artifacts"]["root"])
    frames: list[pd.DataFrame] = []
    for model_id in sorted(allowed):
        spec = model_spec_from_registry(registry, model_id)
        path = artifact_root / "learning_curve_shards" / spec.environment_id / f"{model_id}.csv"
        if not path.is_file():
            raise ContractViolation(f"Missing learning-curve shard for {model_id}")
        frames.append(pd.read_csv(path))
    return pd.concat(frames, ignore_index=True, sort=False)


def suite_v4_eligibility(
    screen: pd.DataFrame,
    aggregate: pd.DataFrame,
    fold: pd.DataFrame,
    group: pd.DataFrame,
    transition: pd.DataFrame,
    probabilistic: pd.DataFrame,
    learning: pd.DataFrame,
    config: Mapping[str, Any],
    *,
    robustness_guardrail_failures: set[str] | None = None,
) -> pd.DataFrame:
    policy = config["suite_v4_eligibility"]
    robustness_guardrail_failures = robustness_guardrail_failures or set()
    b7_aggregate = aggregate.loc[aggregate["model_id"].eq("B7_two_regime_imm")].iloc[0]
    b7_audit = fold.loc[
        fold["model_id"].eq("B7_two_regime_imm") & fold["target_date"].eq("2023-11-07"), "mae"
    ].iloc[0]
    b1_transition = _metric_value(
        transition,
        model_id="B1_persistence_last_rate",
        design="rolling_origin",
        dimension="pooled_transition",
        segment="transition",
    )
    b1_volatile = _metric_value(
        transition,
        model_id="B1_persistence_last_rate",
        design="rolling_origin",
        dimension="transition",
        segment="volatile_or_gap",
    )
    b7_volatile = _metric_value(
        transition,
        model_id="B7_two_regime_imm",
        design="rolling_origin",
        dimension="transition",
        segment="volatile_or_gap",
    )
    b7_profile = _metric_value(
        group,
        model_id="B7_two_regime_imm",
        design="spatiotemporal_leave_profile_out",
        scope="equal_profile_macro",
        value_column="mae",
    )
    b7_zone = _metric_value(
        group,
        model_id="B7_two_regime_imm",
        design="spatiotemporal_leave_zone_out",
        scope="equal_zone_macro",
        value_column="mae",
    )
    b7_worst_zone = _metric_value(
        group,
        model_id="B7_two_regime_imm",
        design="spatiotemporal_leave_zone_out",
        scope="worst_zone",
        value_column="mae",
    )
    rows: list[dict[str, Any]] = []
    candidates = screen.loc[screen["registry_status"].eq("PREREGISTERED_CANDIDATE")]
    for model_id in candidates["model_id"].astype(str):
        aggregate_row = aggregate.loc[aggregate["model_id"].eq(model_id)]
        if aggregate_row.empty:
            continue
        row = aggregate_row.iloc[0]
        audit_values = fold.loc[fold["model_id"].eq(model_id) & fold["target_date"].eq("2023-11-07"), "mae"]
        transition_mae = _metric_value(
            transition,
            model_id=model_id,
            design="rolling_origin",
            dimension="pooled_transition",
            segment="transition",
        )
        volatile = _metric_value(
            transition,
            model_id=model_id,
            design="rolling_origin",
            dimension="transition",
            segment="volatile_or_gap",
        )
        profile = _metric_value(
            group,
            model_id=model_id,
            design="spatiotemporal_leave_profile_out",
            scope="equal_profile_macro",
            value_column="mae",
        )
        zone = _metric_value(
            group,
            model_id=model_id,
            design="spatiotemporal_leave_zone_out",
            scope="equal_zone_macro",
            value_column="mae",
        )
        worst_zone = _metric_value(
            group,
            model_id=model_id,
            design="spatiotemporal_leave_zone_out",
            scope="worst_zone",
            value_column="mae",
        )
        conformal = probabilistic.loc[
            probabilistic["model_id"].eq(model_id)
            & probabilistic["design"].eq("rolling_origin")
            & probabilistic["interval_source"].eq("conformalized")
            & probabilistic["dimension"].eq("overall")
        ]
        coverage = float(conformal["coverage_95"].iloc[0]) if not conformal.empty else float("nan")
        wis = float(conformal["weighted_interval_score"].iloc[0]) if not conformal.empty else float("nan")
        date_wide = fold.loc[fold["model_id"].isin([model_id, "B7_two_regime_imm"])].pivot(
            index="target_date", columns="model_id", values="mae"
        )
        date_signs = int((date_wide[model_id] < date_wide["B7_two_regime_imm"]).sum()) if model_id in date_wide else 0
        profile_values = group.loc[
            group["model_id"].isin([model_id, "B7_two_regime_imm"])
            & group["design"].eq("spatiotemporal_leave_profile_out")
            & group["scope"].eq("by_profile")
        ].pivot(index="group", columns="model_id", values="mae")
        profile_signs = int((profile_values[model_id] < profile_values["B7_two_regime_imm"]).sum()) if model_id in profile_values else 0
        learning_value = learning.loc[
            learning["model_id"].eq(model_id) & learning["training_rows"].eq(823), "mae"
        ]
        checks = {
            "rolling_mae": float(row["mae"]) <= float(policy["rolling_mae_ratio_vs_b7_max"]) * float(b7_aggregate["mae"]),
            "audit_tail_mae": not audit_values.empty
            and float(audit_values.iloc[0]) <= float(policy["audit_tail_mae_ratio_vs_b7_max"]) * float(b7_audit),
            "transition_improvement": np.isfinite(transition_mae)
            and transition_mae <= (1.0 - float(policy["transition_improvement_vs_b1_min"])) * b1_transition,
            "volatile_or_gap": np.isfinite(volatile) and volatile <= min(b1_volatile, b7_volatile),
            "leave_profile_macro": np.isfinite(profile)
            and profile <= (1.0 + float(policy["leave_profile_macro_degradation_max"])) * b7_profile,
            "leave_zone_macro": np.isfinite(zone)
            and zone <= (1.0 + float(policy["leave_zone_macro_degradation_max"])) * b7_zone,
            "worst_zone": np.isfinite(worst_zone)
            and worst_zone <= (1.0 + float(policy["worst_zone_degradation_max"])) * b7_worst_zone,
            "conformal_95_coverage": np.isfinite(coverage)
            and float(policy["conformal_95_coverage_min"]) <= coverage <= float(policy["conformal_95_coverage_max"]),
            "date_sign_consistency": date_signs >= int(policy["rolling_date_improvement_sign_min"]),
            "profile_sign_consistency": profile_signs >= int(policy["profile_improvement_sign_min"]),
            "learning_curve_available": not learning_value.empty,
            "spatial_inner_guardrails": model_id not in robustness_guardrail_failures,
            "screen_and_protocol": bool(
                screen.loc[screen["model_id"].eq(model_id), "advanced_to_robustness"].iloc[0]
            ),
        }
        checks = {name: bool(value) for name, value in checks.items()}
        rows.append(
            {
                "model_id": model_id,
                "eligible": bool(all(checks.values())),
                "checks_json": json.dumps(checks, sort_keys=True, separators=(",", ":")),
                "rolling_mae": float(row["mae"]),
                "audit_tail_mae": float(audit_values.iloc[0]) if not audit_values.empty else np.nan,
                "transition_mae": transition_mae,
                "volatile_or_gap_mae": volatile,
                "leave_profile_macro_mae": profile,
                "leave_zone_macro_mae": zone,
                "worst_zone_mae": worst_zone,
                "conformal_95_coverage": coverage,
                "conformal_95_weighted_interval_score": wis,
                "rolling_date_improvement_signs": date_signs,
                "profile_improvement_signs": profile_signs,
                "fit_time": float(row["fit_seconds_total"]),
            }
        )
    return pd.DataFrame(rows)


def _robustness_guardrail_failures(
    root: Path,
    config: Mapping[str, Any],
    registry: Mapping[str, Any],
    model_ids: set[str],
) -> set[str]:
    artifact_root = resolve_repo_path(root, config["artifacts"]["root"])
    failures: set[str] = set()
    for model_id in sorted(model_ids):
        spec = model_spec_from_registry(registry, model_id)
        status_path = (
            artifact_root
            / "worker_status"
            / "robustness"
            / spec.environment_id
            / f"{model_id}.json"
        )
        if not status_path.is_file():
            failures.add(model_id)
            continue
        status = json.loads(status_path.read_text(encoding="utf-8"))
        if any(
            str(item.get("selection_status", "")).startswith("DIAGNOSTIC_FALLBACK")
            for item in status.get("inner_selection_rejections", [])
        ):
            failures.add(model_id)
    return failures


def build_internal_shortlist(
    primary: str,
    status: str,
    eligibility: pd.DataFrame,
    aggregate: pd.DataFrame,
    probabilistic: pd.DataFrame,
) -> dict[str, Any]:
    interpretable_ids = {"Z01_elastic_net", "Z02_huber", "Z05_gaussian_gee", "Z11_ebm"}
    probabilistic_ids = {
        "Z04_gaussian_process",
        "Z05_gaussian_gee",
        "Z07_quantile_hist_gradient_boosting",
        "Z12_ngboost",
    }
    audited_ids = set(
        eligibility.loc[
            eligibility[["leave_profile_macro_mae", "leave_zone_macro_mae"]]
            .apply(pd.to_numeric, errors="coerce")
            .notna()
            .all(axis=1),
            "model_id",
        ].astype(str)
    )
    interpretable = aggregate.loc[
        aggregate["model_id"].isin(interpretable_ids & audited_ids)
    ].sort_values(["mae", "model_id"])
    probabilistic_table = probabilistic.loc[
        probabilistic["model_id"].isin(probabilistic_ids & audited_ids)
        & probabilistic["design"].eq("rolling_origin")
        & probabilistic["dimension"].eq("overall")
    ].sort_values(["weighted_interval_score", "model_id"])
    context = []
    if not interpretable.empty:
        context.append(str(interpretable.iloc[0]["model_id"]))
    if not probabilistic_table.empty:
        context.append(str(probabilistic_table.iloc[0]["model_id"]))
    return {
        "schema_version": 1,
        "status": status,
        "primary_model_id": primary,
        "context_only_models": list(dict.fromkeys(context)),
        "eligibility": eligibility.to_dict(orient="records"),
        "new_holdout_seen": False,
        "claim_scope": "train_only_internal_research",
    }


def freeze_full_train_primary(
    root: Path,
    config: Mapping[str, Any],
    suite: Mapping[str, Any],
    registry: Mapping[str, Any],
    selected_parameters: Mapping[str, Any],
) -> dict[str, Any]:
    primary = str(suite["primary_model_id"])
    spec = model_spec_from_registry(registry, primary)
    if primary not in selected_parameters.get("models", {}):
        raise ContractViolation(f"Selected primary has no frozen parameters: {primary}")
    environment = _load_json(root, config["artifacts"]["environment_manifest"])
    record = environment["environments"].get(spec.environment_id)
    if record is None or record.get("status") != "READY":
        raise ContractViolation(f"Primary environment is not READY: {spec.environment_id}")
    python_path = resolve_repo_path(root, record["python_executable"])
    command = [
        str(python_path),
        str(root / "scripts" / "run_gate_b6_full_train_worker.py"),
        "--model-id",
        primary,
    ]
    completed = None
    for attempt in range(3):
        completed = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=86400,
            check=False,
        )
        if completed.returncode == 0:
            break
        if attempt < 2 and "PermissionError: [WinError 5]" in completed.stderr:
            time.sleep(0.5)
            continue
        break
    assert completed is not None
    if completed.returncode != 0:
        raise RuntimeError(
            f"Full-train worker failed in {spec.environment_id}: {completed.stderr[-4000:]}"
        )
    model_path = resolve_repo_path(root, config["artifacts"]["full_train_model"])
    manifest_path = model_path.with_suffix(model_path.suffix + ".manifest.json")
    if not model_path.is_file() or not manifest_path.is_file():
        raise ContractViolation("Full-train worker did not produce model and manifest")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("model_id") != primary or manifest.get("environment_id") != spec.environment_id:
        raise ContractViolation("Full-train manifest provenance mismatch")
    if manifest.get("sha256") != sha256_file(model_path):
        raise ContractViolation("Full-train artifact hash mismatch")
    return manifest


def _validate_fold_completeness(
    frame: pd.DataFrame,
    model_id: str,
    spec,
    phase: str,
    job_manifest: Mapping[str, Any],
) -> None:
    jobs = [job for job in job_manifest["jobs"] if job["model_id"] == model_id and job["phase"] == phase]
    expected = {job["outer_fold_id"]: job for job in jobs}
    if set(frame["fold_id"].astype(str)) != set(expected):
        raise ContractViolation(f"{model_id}/{phase} fold IDs do not match the frozen job manifest")
    expected_seeds = set(map(int, spec.seed_policy["seeds"]))
    for fold_id, subset in frame.groupby("fold_id", sort=True):
        individual = subset.loc[subset["aggregation"].eq("single_seed")]
        if set(individual["seed"].astype(int)) != expected_seeds:
            raise ContractViolation(f"{model_id}/{fold_id} seed coverage is incomplete")
        for seed, seed_frame in individual.groupby("seed", sort=True):
            if sample_id_list_sha256(tuple(seed_frame["sample_id"].astype(str))) != expected[str(fold_id)][
                "outer_validation_sample_ids_sha256"
            ]:
                raise ContractViolation(f"{model_id}/{fold_id}/{seed} sample hash mismatch")
        if len(expected_seeds) > 1:
            ensemble = subset.loc[subset["aggregation"].eq("mean_of_fixed_seeds")]
            if ensemble.empty:
                raise ContractViolation(f"{model_id}/{fold_id} fixed-seed ensemble is missing")


def _requirements_from_lock(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("-"):
            continue
        if "==" in stripped:
            name, version = stripped.split("==", 1)
            result[name.lower()] = version
    return result


def _installed_versions(python: Path, expected: Mapping[str, str]) -> dict[str, str]:
    script = (
        "import importlib.metadata,json; names="
        + repr(list(expected))
        + "; print(json.dumps({n:importlib.metadata.version(n) for n in names}))"
    )
    completed = subprocess.run(
        [str(python), "-c", script],
        cwd=python.parents[3],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        return {}
    return {str(key).lower(): str(value) for key, value in json.loads(completed.stdout).items()}


def _installed_version_matches(
    package: str,
    installed: str | None,
    required: str,
    *,
    cuda_wheel_index: str | None,
) -> bool:
    if installed == required:
        return True
    if package.lower() == "torch" and cuda_wheel_index and installed:
        return installed == f"{required}+cu130"
    return False


def _environment_runtime_capture(python: Path) -> dict[str, Any]:
    script = (
        "import json,platform; d={'python':platform.python_version()}; "
        "\ntry:\n import torch; d.update(torch_version=torch.__version__,torch_cuda=torch.version.cuda,"
        "torch_cuda_available=torch.cuda.is_available(),torch_device=(torch.cuda.get_device_name(0) if torch.cuda.is_available() else None))"
        "\nexcept ImportError:\n pass"
        "\nprint(json.dumps(d))"
    )
    completed = subprocess.run(
        [str(python), "-c", script],
        cwd=python.parents[3],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        return {"capture_error": completed.stderr[-1000:]}
    return json.loads(completed.stdout)


def _wheel_records(report_path: Path) -> list[dict[str, Any]]:
    if not report_path.is_file():
        return []
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    for item in payload.get("install", []):
        info = item.get("download_info", {})
        url = str(info.get("url", ""))
        if not url.startswith(("https://", "http://")):
            # The editable repository itself is source, not a downloaded wheel.
            continue
        hashes = info.get("archive_info", {}).get("hashes", {})
        rows.append(
            {
                "package": item.get("metadata", {}).get("name"),
                "version": item.get("metadata", {}).get("version"),
                "url": url,
                "sha256": hashes.get("sha256"),
            }
        )
    return rows


def _hardware_capture(root: Path) -> dict[str, Any]:
    nvidia = subprocess.run(
        ["nvidia-smi", "--query-gpu=name,driver_version,memory.total,compute_cap", "--format=csv,noheader,nounits"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    nvidia_version = subprocess.run(
        ["nvidia-smi", "--version"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    return {
        "os": platform.platform(),
        "python": platform.python_version(),
        "cpu_model": _cpu_model(),
        "cpu_logical_count": os.cpu_count(),
        "ram_bytes": _total_memory_bytes(),
        "nvidia_smi": nvidia.stdout.strip() if nvidia.returncode == 0 else None,
        "nvidia_versions": _parse_colon_lines(nvidia_version.stdout)
        if nvidia_version.returncode == 0
        else None,
    }


def _cpu_model() -> str | None:
    """Return a privacy-safe CPU model without shelling out to a host profiler."""

    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            ) as key:
                value, _ = winreg.QueryValueEx(key, "ProcessorNameString")
            normalized = " ".join(str(value).split())
            return normalized or None
        except (OSError, ImportError):
            pass
    normalized = " ".join(platform.processor().split())
    return normalized or None


def _parse_colon_lines(text: str) -> dict[str, str]:
    """Parse privacy-safe ``nvidia-smi --version`` key/value output."""

    values: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        normalized = "_".join(key.strip().lower().replace("-", "_").split())
        values[normalized] = value.strip()
    return values


def _durable_text_privacy_violations(root: Path) -> list[str]:
    """Detect machine-local paths and token-shaped secrets in durable output."""

    windows_path = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\\\/]")
    secret = re.compile(r"(?:hf_|sk-)[A-Za-z0-9_-]{20,}")
    violations: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".json", ".csv", ".txt", ".md"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for label, pattern in (("absolute_windows_path", windows_path), ("secret_token", secret)):
            match = pattern.search(text)
            if match:
                violations.append(f"{path.relative_to(root).as_posix()}::{label}::{match.group(0)[:24]}")
    return violations


def _total_memory_bytes() -> int | None:
    try:
        import psutil

        return int(psutil.virtual_memory().total)
    except Exception:
        return None


def collect_environment_smoke_reports(
    root: Path, config: Mapping[str, Any], manifest: Mapping[str, Any]
) -> dict[str, Any]:
    reports = {}
    for environment_id, record in manifest["environments"].items():
        path = root / "work" / "environment_staging" / environment_id / "smoke_report.json"
        reports[environment_id] = (
            json.loads(path.read_text(encoding="utf-8"))
            if path.is_file()
            else {"status": "PENDING", "environment_status": record["status"]}
        )
    return {"schema_version": 1, "environments": reports}


def collect_determinism_reports(
    root: Path, config: Mapping[str, Any], manifest: Mapping[str, Any]
) -> dict[str, Any]:
    reports = {}
    for environment_id, record in manifest["environments"].items():
        path = root / "work" / "environment_staging" / environment_id / "determinism_report.json"
        reports[environment_id] = (
            json.loads(path.read_text(encoding="utf-8"))
            if path.is_file()
            else {"status": "PENDING", "environment_status": record["status"]}
        )
    return {"schema_version": 1, "environments": reports}


def _metric_value(frame: pd.DataFrame, *, value_column: str = "mae", **filters: str) -> float:
    subset = frame
    for column, value in filters.items():
        subset = subset.loc[subset[column].astype(str).eq(str(value))]
    return float(subset[value_column].iloc[0]) if not subset.empty else float("nan")


def _suite_source_hashes(root: Path, config: Mapping[str, Any], paths: Mapping[str, Path]) -> dict[str, str]:
    sources = [
        root / "configs" / "gate_b5.yaml",
        root / "configs" / "gate_b6.yaml",
        root / "configs" / "final_holdout_v3.yaml",
        root / AMENDMENT_RELATIVE_PATH,
        root / "requirements" / "b6_cpu.lock.txt",
        root / "requirements" / "b6_ngboost.lock.txt",
        root / "requirements" / "b6_torch.lock.txt",
        root / "requirements" / "b6_torch_runtime.lock.txt",
        root / "artifacts" / "splits" / "t1_train_benchmark_v1" / "benchmark_plan.json",
        paths["model_registry"],
        paths["selected_parameter_registry"],
        paths["environment_manifest"],
        paths["external_model_manifest"],
        paths["root"] / "protocol_amendment.json",
    ]
    errata_path = paths["root"] / "protocol_errata.json"
    if errata_path.is_file():
        sources.append(errata_path)
    sources.extend(sorted((root / "src" / "skru1").glob("*.py")))
    sources.extend(
        root / "scripts" / name
        for name in (
            "run_gate_b5.py",
            "run_gate_b6.py",
            "run_gate_b6_worker.py",
            "run_gate_b6_full_train_worker.py",
            "run_b6_environment_smoke.py",
            "stage_b6_environment.py",
        )
    )
    return {path.relative_to(root).as_posix(): sha256_file(path) for path in sources}


def _dataframes_equivalent(
    saved: pd.DataFrame,
    recomputed: pd.DataFrame,
    *,
    keys: Sequence[str],
    atol: float = 1e-9,
) -> bool:
    """Compare persisted and independently recomputed tables deterministically."""

    if set(saved.columns) != set(recomputed.columns) or len(saved) != len(recomputed):
        return False
    missing_keys = [key for key in keys if key not in saved.columns]
    if missing_keys:
        return False
    left = saved.loc[:, sorted(saved.columns)].sort_values(list(keys), kind="mergesort").reset_index(drop=True)
    right = (
        recomputed.loc[:, sorted(recomputed.columns)]
        .sort_values(list(keys), kind="mergesort")
        .reset_index(drop=True)
    )
    for column in left.columns:
        left_column = left[column]
        right_column = right[column]
        if pd.api.types.is_numeric_dtype(left_column) and pd.api.types.is_numeric_dtype(right_column):
            if not np.allclose(
                pd.to_numeric(left_column, errors="coerce").to_numpy(float),
                pd.to_numeric(right_column, errors="coerce").to_numpy(float),
                rtol=1e-9,
                atol=atol,
                equal_nan=True,
            ):
                return False
        else:
            left_values = left_column.astype("string").fillna("<NA>").to_numpy(str)
            right_values = right_column.astype("string").fillna("<NA>").to_numpy(str)
            if not np.array_equal(left_values, right_values):
                return False
    return True


def _load_json(root: Path, relative: str | Path) -> dict[str, Any]:
    path = resolve_repo_path(root, relative)
    return json.loads(path.read_text(encoding="utf-8"))


def _paths(root: Path, config: Mapping[str, Any]) -> dict[str, Path]:
    return {key: resolve_repo_path(root, value) for key, value in config["artifacts"].items()}

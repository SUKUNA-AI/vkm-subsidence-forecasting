"""Sealed intake and one-shot evaluation for a new T1 final holdout."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from typing import Any, Mapping
from uuid import uuid4

import numpy as np
import pandas as pd
import yaml

from .data_contracts import (
    ContractViolation,
    discover_project_root,
    load_canonical_bundle,
    sha256_file,
)
from .evaluation import causal_feature_history
from .gate_b4 import _build_gate_b4_model
from .metrics import regression_metrics
from .splits import (
    FrozenManifestError,
    ManifestDataset,
    SplitProvenance,
    read_manifest,
    sample_id_list_sha256,
)


def load_holdout_v3_config(
    root: str | Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    project_root = discover_project_root(root)
    path = project_root / "configs" / "final_holdout_v3.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ContractViolation("configs/final_holdout_v3.yaml must contain a mapping")
    required = {
        "policy_id",
        "status",
        "candidate_package",
        "eligible_options",
        "minimum_scope",
        "freeze_protocol",
        "artifacts",
    }
    missing = required - set(config)
    if missing:
        raise ContractViolation(f"Final holdout v3 config is missing: {sorted(missing)}")
    if config["status"] != "PENDING_DATA":
        raise ContractViolation("Repository policy status must remain PENDING_DATA until freeze")
    for value in [
        config["candidate_package"]["directory"],
        *config["artifacts"].values(),
        config["freeze_protocol"]["candidate_suite"],
    ]:
        resolve_repo_path(project_root, str(value))
    return project_root, config


def resolve_repo_path(root: Path, relative: str | Path) -> Path:
    path = Path(relative)
    if path.is_absolute():
        raise ContractViolation(f"Holdout path must be repository-relative: {path}")
    resolved = (root / path).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ContractViolation(f"Holdout path escapes repository root: {path}") from exc
    return resolved


def inspect_holdout_candidate(
    root: Path,
    config: Mapping[str, Any],
    *,
    write_status: bool = True,
) -> tuple[dict[str, Any], pd.DataFrame, pd.DataFrame | None]:
    """Inspect unlabelled origins and target bytes without parsing target values."""

    package = config["candidate_package"]
    directory = resolve_repo_path(root, package["directory"])
    expected = {
        "package_manifest": directory / package["package_manifest"],
        "origins": directory / package["origins_file"],
        "sealed_targets": directory / package["sealed_targets_file"],
    }
    inventory = pd.DataFrame(
        [
            {
                "role": role,
                "relative_path": path.relative_to(root).as_posix(),
                "exists": path.is_file(),
                "size_bytes": path.stat().st_size if path.is_file() else pd.NA,
                "sha256": sha256_file(path) if path.is_file() else pd.NA,
                "content_parsed_before_freeze": role != "sealed_targets" and path.is_file(),
            }
            for role, path in expected.items()
        ]
    )
    present_count = int(inventory["exists"].sum())
    if present_count == 0:
        status = {
            "schema_version": 1,
            "policy_id": config["policy_id"],
            "status": "PENDING_DATA",
            "eligible": False,
            "reason": "no_future_or_external_candidate_package_present",
            "candidate_directory": Path(package["directory"]).as_posix(),
            "sealed_target_values_read": False,
            "next_action": "place an authorized package matching FINAL_HOLDOUT_INTAKE_V3.md",
        }
        if write_status:
            _write_status_outputs(root, config, status, inventory)
        return status, inventory, None
    if present_count != len(expected):
        status = {
            "schema_version": 1,
            "policy_id": config["policy_id"],
            "status": "INVALID_PACKAGE",
            "eligible": False,
            "reason": "candidate_package_is_partial",
            "sealed_target_values_read": False,
            "missing_roles": inventory.loc[~inventory["exists"], "role"].tolist(),
        }
        if write_status:
            _write_status_outputs(root, config, status, inventory)
        return status, inventory, None

    manifest = json.loads(expected["package_manifest"].read_text(encoding="utf-8"))
    required_manifest = {
        "schema_version",
        "package_id",
        "holdout_type",
        "target_definition",
        "origins_file",
        "origins_sha256",
        "sealed_targets_file",
        "sealed_targets_sha256",
        "source_package_is_new",
        "labels_unseen_before_candidate_freeze",
    }
    missing = required_manifest - set(manifest)
    if missing:
        raise ContractViolation(f"Holdout package manifest is missing: {sorted(missing)}")
    if manifest["origins_file"] != package["origins_file"] or manifest[
        "sealed_targets_file"
    ] != package["sealed_targets_file"]:
        raise ContractViolation("Holdout package filenames differ from the frozen contract")
    if sha256_file(expected["origins"]) != str(manifest["origins_sha256"]).lower():
        raise ContractViolation("Holdout origins SHA-256 mismatch")
    if sha256_file(expected["sealed_targets"]) != str(
        manifest["sealed_targets_sha256"]
    ).lower():
        raise ContractViolation("Sealed target SHA-256 mismatch")
    if manifest["target_definition"] != package["target_definition"]:
        raise ContractViolation("Holdout target definition differs from T1 contract")
    if manifest["source_package_is_new"] is not True:
        raise ContractViolation("Holdout package must declare a new source package")
    if manifest["labels_unseen_before_candidate_freeze"] is not True:
        raise ContractViolation("Holdout package must declare labels unseen before freeze")

    bundle = load_canonical_bundle(root)
    origins = pd.read_csv(expected["origins"])
    expected_columns = list(bundle.features.columns)
    if list(origins.columns) != expected_columns:
        raise ContractViolation(
            "Holdout origin schema must exactly match the canonical feature table order"
        )
    if origins["sample_id"].isna().any() or origins["sample_id"].duplicated().any():
        raise ContractViolation("Holdout origin sample IDs must be non-null and unique")
    for column in ("current_date", "target_date"):
        origins[column] = pd.to_datetime(origins[column], errors="coerce")
        if origins[column].isna().any():
            raise ContractViolation(f"Holdout origins contain invalid {column}")
    horizon = pd.to_numeric(origins["forecast_horizon_days"], errors="coerce")
    if horizon.isna().any() or (horizon <= 0).any():
        raise ContractViolation("Holdout forecast_horizon_days must be positive")

    development_ids: set[str] = set()
    for split in ("train", "validation", "test"):
        path = root / "artifacts" / "splits" / "t1_v1" / f"{split}.csv"
        development_ids.update(read_manifest(path)["sample_id"].astype(str))
    overlap = set(origins["sample_id"].astype(str)) & development_ids
    if overlap:
        raise ContractViolation(f"Holdout sample IDs overlap development data: {len(overlap)}")

    scope = {
        "observed_origins": len(origins),
        "unique_points": int(origins["point_id"].astype(str).nunique()),
        "unique_profiles": int(origins["profile_id"].astype(str).nunique()),
        "distinct_target_campaign_dates": int(origins["target_date"].nunique()),
    }
    scope_failures = {
        key: {"observed": scope[key], "minimum": int(minimum)}
        for key, minimum in config["minimum_scope"].items()
        if scope[key] < int(minimum)
    }
    if scope_failures:
        raise ContractViolation(f"Holdout scope is below minimum: {scope_failures}")

    holdout_type = str(manifest["holdout_type"])
    if holdout_type == "future_temporal_holdout":
        policy = config["eligible_options"][holdout_type]
        lower = pd.Timestamp(policy["target_date_not_before"])
        strict = pd.Timestamp(policy["target_date_strictly_after"])
        if pd.Timestamp(origins["target_date"].min()) < lower:
            raise ContractViolation("Future holdout begins before the frozen lower bound")
        if not origins["target_date"].gt(strict).all():
            raise ContractViolation("Future holdout is not strictly beyond the old boundary")
    elif holdout_type == "external_holdout":
        required_true = (
            "independent_site_or_campaign_package",
            "schema_mapping_frozen_before_labels",
            "target_definition_equivalent",
        )
        if any(manifest.get(key) is not True for key in required_true):
            raise ContractViolation("External holdout independence declarations are incomplete")
        development_points = set(bundle.features["point_id"].astype(str))
        point_overlap = set(origins["point_id"].astype(str)) & development_points
        if point_overlap:
            raise ContractViolation(f"External holdout point IDs overlap development: {len(point_overlap)}")
    else:
        raise ContractViolation(f"Unknown holdout_type: {holdout_type}")

    status = {
        "schema_version": 1,
        "policy_id": config["policy_id"],
        "status": "READY_TO_FREEZE",
        "eligible": True,
        "package_id": str(manifest["package_id"]),
        "holdout_type": holdout_type,
        "scope": scope,
        "target_date_min": origins["target_date"].min().date().isoformat(),
        "target_date_max": origins["target_date"].max().date().isoformat(),
        "origin_sha256": sha256_file(expected["origins"]),
        "sealed_target_sha256": sha256_file(expected["sealed_targets"]),
        "sealed_target_values_read": False,
    }
    if write_status:
        _write_status_outputs(root, config, status, inventory)
    return status, inventory, origins


def freeze_holdout(root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    status, _, origins = inspect_holdout_candidate(root, config, write_status=True)
    if status["status"] != "READY_TO_FREEZE" or origins is None:
        raise ContractViolation(f"Holdout cannot be frozen from status {status['status']}")
    suite_path = resolve_repo_path(root, config["freeze_protocol"]["candidate_suite"])
    if not suite_path.is_file():
        raise FileNotFoundError("Run Gate B4 to freeze final_candidate_suite_v3.json first")
    suite = json.loads(suite_path.read_text(encoding="utf-8"))
    if suite.get("status") != "frozen_before_new_holdout_labels":
        raise ContractViolation("Candidate suite is not frozen before holdout labels")
    _verify_suite_source_hashes(root, suite)
    _verify_suite_contracts(root, suite)
    commit_sha = _git_head(root)

    ordered = origins.sort_values(
        ["target_date", "current_date", "profile_id", "point_id", "sample_id"],
        kind="mergesort",
    )[["sample_id"]].reset_index(drop=True)
    sample_path = resolve_repo_path(root, config["artifacts"]["sample_manifest"])
    _write_frozen_csv(sample_path, ordered)
    package = config["candidate_package"]
    directory = resolve_repo_path(root, package["directory"])
    frozen_record = {
        "schema_version": 1,
        "policy_id": config["policy_id"],
        "status": "FROZEN_UNOPENED",
        "package_id": status["package_id"],
        "holdout_type": status["holdout_type"],
        "candidate_commit_sha": commit_sha,
        "candidate_suite_path": Path(config["freeze_protocol"]["candidate_suite"]).as_posix(),
        "candidate_suite_sha256": sha256_file(suite_path),
        "primary_model_id": suite["primary_model_id"],
        "origins_sha256": sha256_file(directory / package["origins_file"]),
        "sealed_targets_sha256": sha256_file(directory / package["sealed_targets_file"]),
        "sample_manifest_sha256": sha256_file(sample_path),
        "sample_ids_sha256": sample_id_list_sha256(ordered["sample_id"].astype(str)),
        "rows": len(ordered),
        "target_values_read": False,
        "one_access_remaining": 1,
    }
    frozen_path = resolve_repo_path(root, config["artifacts"]["frozen_record"])
    if frozen_path.exists():
        existing = json.loads(frozen_path.read_text(encoding="utf-8"))
        if existing != frozen_record:
            raise FrozenManifestError("Refusing to replace a different frozen holdout record")
    else:
        _write_json_atomic(root, frozen_path, frozen_record)
    ledger_path = resolve_repo_path(root, config["artifacts"]["access_ledger"])
    initial_ledger = {
        "schema_version": 1,
        "policy_id": config["policy_id"],
        "state": "FROZEN_UNOPENED",
        "attempt_count": 0,
        "target_values_read": False,
        "failed_access_is_consumed": True,
    }
    if ledger_path.exists():
        existing_ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        if existing_ledger != initial_ledger:
            raise PermissionError("Holdout access ledger already exists in a non-initial state")
    else:
        _write_json_atomic(root, ledger_path, initial_ledger)
    return frozen_record


def evaluate_holdout_once(root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    paths = {
        key: resolve_repo_path(root, value)
        for key, value in config["artifacts"].items()
    }
    if not paths["frozen_record"].is_file() or not paths["access_ledger"].is_file():
        raise PermissionError("Freeze the eligible holdout before evaluation")
    frozen = json.loads(paths["frozen_record"].read_text(encoding="utf-8"))
    ledger = json.loads(paths["access_ledger"].read_text(encoding="utf-8"))
    if frozen.get("status") != "FROZEN_UNOPENED" or ledger.get("state") != "FROZEN_UNOPENED":
        raise PermissionError("The one-shot holdout access has already been consumed")
    suite_path = resolve_repo_path(root, frozen["candidate_suite_path"])
    if sha256_file(suite_path) != frozen["candidate_suite_sha256"]:
        raise ContractViolation("Frozen candidate suite hash changed")
    suite = json.loads(suite_path.read_text(encoding="utf-8"))
    _verify_suite_source_hashes(root, suite)
    _verify_suite_contracts(root, suite)
    package = config["candidate_package"]
    directory = resolve_repo_path(root, package["directory"])
    origin_path = directory / package["origins_file"]
    target_path = directory / package["sealed_targets_file"]
    if sha256_file(origin_path) != frozen["origins_sha256"]:
        raise ContractViolation("Frozen holdout origin file changed")
    if sha256_file(target_path) != frozen["sealed_targets_sha256"]:
        raise ContractViolation("Frozen sealed target file changed")
    if sha256_file(paths["sample_manifest"]) != frozen["sample_manifest_sha256"]:
        raise ContractViolation("Frozen holdout sample manifest changed")

    attempt_id = uuid4().hex
    consumed = {
        "schema_version": 1,
        "policy_id": config["policy_id"],
        "state": "CONSUMED_IN_PROGRESS",
        "attempt_count": 1,
        "attempt_id": attempt_id,
        "access_started_utc": datetime.now(timezone.utc).isoformat(),
        "target_values_read": False,
        "failed_access_is_consumed": True,
    }
    _write_json_atomic(root, paths["access_ledger"], consumed)
    try:
        targets = pd.read_csv(target_path)
        consumed["target_values_read"] = True
        required_target_columns = list(package["required_target_columns"])
        if list(targets.columns) != required_target_columns:
            raise ContractViolation("Holdout target schema differs from the frozen contract")
        if targets["sample_id"].isna().any() or targets["sample_id"].duplicated().any():
            raise ContractViolation("Holdout target sample IDs must be non-null and unique")
        manifest_ids = read_manifest(paths["sample_manifest"])["sample_id"].astype(str)
        if set(targets["sample_id"].astype(str)) != set(manifest_ids):
            raise ContractViolation("Holdout target IDs differ from the frozen manifest")
        targets["observed_rate_mm_y"] = pd.to_numeric(
            targets["observed_rate_mm_y"], errors="coerce"
        )
        if targets["observed_rate_mm_y"].isna().any() or not np.isfinite(
            targets["observed_rate_mm_y"]
        ).all():
            raise ContractViolation("Holdout targets must be finite numeric values")

        origins = pd.read_csv(origin_path)
        for column in ("current_date", "target_date"):
            origins[column] = pd.to_datetime(origins[column], errors="raise")
        frame = origins.merge(targets, on="sample_id", how="inner", validate="one_to_one")
        frame = frame.set_index("sample_id", drop=False).loc[list(manifest_ids)].reset_index(drop=True)
        provenance = SplitProvenance(
            task="t1",
            split="final_holdout_v3",
            version="t1_final_holdout_v3",
            manifest_path=paths["sample_manifest"],
            manifest_file_sha256=frozen["sample_manifest_sha256"],
            sample_ids_sha256=frozen["sample_ids_sha256"],
            row_count=len(frame),
            test_authorized=True,
            candidate_id=str(suite["suite_id"]),
        )
        bundle = load_canonical_bundle(root)
        if bundle.feature_contract.source_sha256 != suite["feature_contract_sha256"]:
            raise ContractViolation("Frozen suite feature contract changed before scoring")
        if bundle.target_contract.source_sha256 != suite["target_contract_sha256"]:
            raise ContractViolation("Frozen suite target contract changed before scoring")
        holdout = ManifestDataset(
            frame=frame,
            feature_columns=bundle.feature_contract.allowed_features,
            provenance=provenance,
        )
        train = _load_frozen_train(root)
        history = causal_feature_history(train, holdout)
        gate_b4_config = yaml.safe_load(
            (root / "configs" / "gate_b4.yaml").read_text(encoding="utf-8")
        )
        prediction_frames: list[pd.DataFrame] = []
        metric_rows: list[dict[str, Any]] = []
        truth = frame["observed_rate_mm_y"].to_numpy(float)
        for spec in suite["models"]:
            model = _build_gate_b4_model(spec, bundle=bundle, config=gate_b4_config)
            model.fit(train)
            prediction = model.predict(holdout, history_frame=history)
            if prediction.shape != truth.shape or not np.isfinite(prediction).all():
                raise RuntimeError(f"Frozen model produced invalid holdout predictions: {model.model_id}")
            metrics = regression_metrics(truth, prediction)
            metric_rows.append(
                {
                    "model_id": model.model_id,
                    "family": model.family,
                    "role": "primary" if model.model_id == suite["primary_model_id"] else "context_comparator",
                    **metrics,
                }
            )
            output = frame[
                [
                    "sample_id",
                    "point_id",
                    "profile_id",
                    "current_date",
                    "target_date",
                    "forecast_horizon_days",
                ]
            ].copy()
            output.insert(0, "model_id", model.model_id)
            output.insert(1, "family", model.family)
            output.insert(
                2,
                "role",
                "primary" if model.model_id == suite["primary_model_id"] else "context_comparator",
            )
            output["y_true"] = truth
            output["y_pred"] = prediction
            output["error"] = prediction - truth
            output["absolute_error"] = np.abs(prediction - truth)
            prediction_frames.append(output)
        predictions = pd.concat(prediction_frames, ignore_index=True)
        metrics = pd.DataFrame(metric_rows).sort_values("mae", kind="mergesort")
        final_report = {
            "schema_version": 1,
            "policy_id": config["policy_id"],
            "status": "ONE_SHOT_EVALUATION_COMPLETE",
            "attempt_id": attempt_id,
            "suite_id": suite["suite_id"],
            "primary_model_id": suite["primary_model_id"],
            "primary_selected_before_holdout": True,
            "comparator_results_used_for_selection": False,
            "rows": len(frame),
            "sample_ids_sha256": frozen["sample_ids_sha256"],
            "metrics": metrics.to_dict(orient="records"),
            "post_access_tuning_permitted": False,
        }
        _write_csv_atomic(root, paths["predictions"], predictions)
        _write_csv_atomic(root, paths["metrics"], metrics)
        _write_json_atomic(root, paths["final_report"], final_report)
        consumed.update(
            {
                "state": "CONSUMED_SUCCESS",
                "access_finished_utc": datetime.now(timezone.utc).isoformat(),
                "target_values_read": True,
                "final_report_sha256": sha256_file(paths["final_report"]),
            }
        )
        _write_json_atomic(root, paths["access_ledger"], consumed)
        return final_report
    except Exception as exc:
        consumed.update(
            {
                "state": "CONSUMED_FAILED",
                "access_finished_utc": datetime.now(timezone.utc).isoformat(),
                "failure_type": type(exc).__name__,
                "failure_message": str(exc),
            }
        )
        _write_json_atomic(root, paths["access_ledger"], consumed)
        raise


def _load_frozen_train(root: Path) -> ManifestDataset:
    from .splits import load_split_dataset

    return load_split_dataset("t1", "train", root=root)


def _verify_suite_source_hashes(root: Path, suite: Mapping[str, Any]) -> None:
    for relative, expected_hash in suite.get("source_hashes", {}).items():
        path = resolve_repo_path(root, relative)
        if not path.is_file() or sha256_file(path) != expected_hash:
            raise ContractViolation(f"Frozen candidate source changed: {relative}")


def _verify_suite_contracts(root: Path, suite: Mapping[str, Any]) -> None:
    bundle = load_canonical_bundle(root)
    if bundle.feature_contract.source_sha256 != suite.get("feature_contract_sha256"):
        raise ContractViolation("Frozen candidate feature contract hash changed")
    if bundle.target_contract.source_sha256 != suite.get("target_contract_sha256"):
        raise ContractViolation("Frozen candidate target contract hash changed")
    train_manifest = read_manifest(
        root / "artifacts" / "splits" / "t1_v1" / "train.csv"
    )
    observed_train_hash = sample_id_list_sha256(
        train_manifest["sample_id"].astype(str)
    )
    if observed_train_hash != suite.get("train_sample_ids_sha256"):
        raise ContractViolation("Frozen candidate train manifest hash changed")


def _git_head(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    value = result.stdout.strip()
    if len(value) != 40:
        raise ContractViolation("Could not capture a full candidate commit SHA")
    return value


def _write_status_outputs(
    root: Path,
    config: Mapping[str, Any],
    status: Mapping[str, Any],
    inventory: pd.DataFrame,
) -> None:
    _write_json_atomic(
        root,
        resolve_repo_path(root, config["artifacts"]["status"]),
        status,
    )
    _write_csv_atomic(
        root,
        resolve_repo_path(root, config["artifacts"]["intake_inventory"]),
        inventory,
    )


def _write_frozen_csv(path: Path, frame: pd.DataFrame) -> None:
    text = frame.to_csv(index=False, lineterminator="\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_text(encoding="utf-8") != text:
            raise FrozenManifestError(f"Refusing to mutate frozen holdout manifest: {path}")
        return
    path.write_text(text, encoding="utf-8", newline="\n")


def _write_json_atomic(root: Path, path: Path, payload: Mapping[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    _write_text_atomic(root, path, text + "\n")


def _write_csv_atomic(root: Path, path: Path, frame: pd.DataFrame) -> None:
    _write_text_atomic(root, path, frame.to_csv(index=False, lineterminator="\n"))


def _write_text_atomic(root: Path, path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    work_dir = root / "work" / "holdout_v3"
    work_dir.mkdir(parents=True, exist_ok=True)
    temporary = work_dir / f"{path.name}.{uuid4().hex}.tmp"
    temporary.write_text(text, encoding="utf-8", newline="\n")
    temporary.replace(path)

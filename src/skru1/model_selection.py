"""Immutable candidate and one-time T1 test-access records."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

import joblib
import pandas as pd
import yaml

from .data_contracts import CanonicalBundle, ContractViolation, discover_project_root, sha256_file
from .splits import ManifestDataset, read_manifest, sample_id_list_sha256


class CandidateFreezeError(ContractViolation):
    """Raised when an existing frozen candidate would be changed."""


class RepeatedTestAccessError(PermissionError):
    """Raised when a candidate attempts a second T1 test access."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_gate_b_config(
    root: str | Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    project_root = discover_project_root(root)
    path = project_root / "configs" / "gate_b0_b1.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ContractViolation("configs/gate_b0_b1.yaml must contain a mapping")
    required = {"task", "split_version", "models", "selection", "artifacts"}
    missing = required - set(config)
    if missing:
        raise ContractViolation(f"Gate B0/B1 config is missing keys: {sorted(missing)}")
    for value in config["artifacts"].values():
        resolve_repo_path(project_root, str(value))
    return project_root, config


def resolve_repo_path(root: Path, relative: str | Path) -> Path:
    path = Path(relative)
    if path.is_absolute():
        raise ContractViolation(f"Gate B0/B1 path must be repository-relative: {path}")
    resolved = (root / path).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ContractViolation(f"Gate B0/B1 path escapes repository root: {path}") from exc
    return resolved


def write_json_atomic(root: Path, path: Path, payload: Mapping[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=_json_default)
    _write_text_atomic(root, path, text + "\n")


def write_csv_atomic(root: Path, path: Path, frame: pd.DataFrame) -> None:
    text = frame.to_csv(index=False, lineterminator="\n")
    _write_text_atomic(root, path, text)


def persist_model_atomic(root: Path, path: Path, model: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    work_dir = root / "work" / "gate_b0_b1"
    work_dir.mkdir(parents=True, exist_ok=True)
    temporary = work_dir / f"{path.name}.{uuid4().hex}.tmp"
    joblib.dump(model, temporary, compress=3)
    temporary.replace(path)


def freeze_candidate(
    *,
    root: Path,
    config: Mapping[str, Any],
    bundle: CanonicalBundle,
    train: ManifestDataset,
    validation: ManifestDataset,
    selected_ranking: Mapping[str, Any],
    selected_model_spec: Mapping[str, Any],
    model_state: Mapping[str, Any],
    model: Any,
    development_report_path: Path,
    source_paths: list[Path],
) -> dict[str, Any]:
    artifacts = config["artifacts"]
    candidate_config_path = resolve_repo_path(root, artifacts["candidate_config"])
    model_path = resolve_repo_path(root, artifacts["model_artifact"])
    record_path = resolve_repo_path(root, artifacts["candidate_record"])
    if record_path.exists():
        existing = json.loads(record_path.read_text(encoding="utf-8"))
        same_identity = (
            existing.get("selected_model") == selected_model_spec["model_id"]
            and existing.get("feature_contract_sha256") == bundle.feature_contract.source_sha256
            and existing.get("manifest_hashes", {}).get("train")
            == train.provenance.sample_ids_sha256
            and existing.get("manifest_hashes", {}).get("validation")
            == validation.provenance.sample_ids_sha256
        )
        if not same_identity:
            raise CandidateFreezeError(
                "A different frozen candidate already exists; create a new governed candidate version"
            )
        for artifact_field, hash_field in (
            ("candidate_config", "candidate_config_sha256"),
            ("model_artifact", "model_artifact_sha256"),
            ("development_report", "development_report_sha256"),
        ):
            frozen_path = resolve_repo_path(root, existing[artifact_field])
            if not frozen_path.is_file() or sha256_file(frozen_path) != existing[hash_field]:
                raise CandidateFreezeError(
                    f"Frozen candidate artifact is missing or changed: {artifact_field}"
                )
        return existing
    candidate_config = {
        "schema_version": 1,
        "gate": config["gate"],
        "candidate_scope": config["selection"]["candidate_scope"],
        "task": config["task"],
        "split_version": config["split_version"],
        "random_seed": int(config["random_seed"]),
        "selected_model_spec": dict(selected_model_spec),
        "selection_result": dict(selected_ranking),
        "selection_policy": dict(config["selection"]),
        "training_weight_policy": dict(
            config["development_policy"]["training_weights"]
        ),
        "feature_contract_sha256": bundle.feature_contract.source_sha256,
        "model_state": dict(model_state),
    }
    write_json_atomic(root, candidate_config_path, candidate_config)
    persist_model_atomic(root, model_path, model)

    immutable = {
        "schema_version": 1,
        "candidate_scope": config["selection"]["candidate_scope"],
        "status": "frozen",
        "task": str(config["task"]).lower(),
        "split_version": config["split_version"],
        "selected_model": selected_model_spec["model_id"],
        "feature_contract_sha256": bundle.feature_contract.source_sha256,
        "manifest_hashes": {
            "train": train.provenance.sample_ids_sha256,
            "validation": validation.provenance.sample_ids_sha256,
        },
        "manifest_file_hashes": {
            "train": train.provenance.manifest_file_sha256,
            "validation": validation.provenance.manifest_file_sha256,
        },
        "candidate_config": candidate_config_path.relative_to(root).as_posix(),
        "candidate_config_sha256": sha256_file(candidate_config_path),
        "model_artifact": model_path.relative_to(root).as_posix(),
        "model_artifact_sha256": sha256_file(model_path),
        "development_report": development_report_path.relative_to(root).as_posix(),
        "development_report_sha256": sha256_file(development_report_path),
        "source_hashes": {
            path.relative_to(root).as_posix(): sha256_file(path) for path in source_paths
        },
        "test_access_policy": "candidate_record_once",
        "test_access_ledger": str(artifacts["test_access_ledger"]),
    }
    candidate_digest = sha256(_canonical_json(immutable).encode("utf-8")).hexdigest()
    candidate_id = f"t1-b0b1-v1-{candidate_digest[:12]}"
    record = {
        **immutable,
        "candidate_id": candidate_id,
        "frozen_at_utc": utc_now(),
    }
    write_json_atomic(root, record_path, record)
    return record


def claim_test_access(
    *,
    root: Path,
    config: Mapping[str, Any],
    candidate_record: Mapping[str, Any],
) -> dict[str, Any]:
    """Irreversibly claim the one permitted test access before loading labels."""

    ledger_path = resolve_repo_path(root, config["artifacts"]["test_access_ledger"])
    if ledger_path.exists():
        existing = json.loads(ledger_path.read_text(encoding="utf-8"))
        raise RepeatedTestAccessError(
            "T1 test access has already been claimed for candidate "
            f"{existing.get('candidate_id', '<unknown>')} with status={existing.get('status')}"
        )
    candidate_path = resolve_repo_path(root, config["artifacts"]["candidate_record"])
    if not candidate_path.is_file():
        raise CandidateFreezeError("Frozen candidate record is missing")
    on_disk = json.loads(candidate_path.read_text(encoding="utf-8"))
    if on_disk != dict(candidate_record):
        raise CandidateFreezeError("In-memory candidate record differs from the frozen on-disk record")
    if candidate_record.get("status") != "frozen":
        raise CandidateFreezeError("Only status=frozen may claim test access")

    test_manifest = root / "artifacts" / "splits" / "t1_v1" / "test.csv"
    ids = tuple(read_manifest(test_manifest)["sample_id"].astype(str))
    ledger = {
        "schema_version": 1,
        "access_event_id": uuid4().hex,
        "candidate_id": candidate_record["candidate_id"],
        "candidate_record": candidate_path.relative_to(root).as_posix(),
        "candidate_record_sha256": sha256_file(candidate_path),
        "claimed_at_utc": utc_now(),
        "status": "opening",
        "test_manifest": test_manifest.relative_to(root).as_posix(),
        "test_manifest_file_sha256": sha256_file(test_manifest),
        "test_sample_ids_sha256": sample_id_list_sha256(ids),
        "test_rows": len(ids),
    }
    write_json_atomic(root, ledger_path, ledger)
    return ledger


def finalize_test_access(
    *,
    root: Path,
    config: Mapping[str, Any],
    status: str,
    outputs: Mapping[str, Path] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    ledger_path = resolve_repo_path(root, config["artifacts"]["test_access_ledger"])
    if not ledger_path.is_file():
        raise RepeatedTestAccessError("Cannot finalize a test access that was never claimed")
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    if ledger.get("status") != "opening":
        raise RepeatedTestAccessError(
            f"Test access ledger is already terminal: {ledger.get('status')}"
        )
    if status not in {"consumed", "failed_after_claim"}:
        raise ValueError("Test ledger terminal status must be consumed or failed_after_claim")
    ledger["status"] = status
    ledger["finalized_at_utc"] = utc_now()
    if outputs:
        ledger["output_hashes"] = {
            name: {
                "path": path.relative_to(root).as_posix(),
                "sha256": sha256_file(path),
            }
            for name, path in outputs.items()
        }
    if error:
        ledger["error"] = error
    write_json_atomic(root, ledger_path, ledger)
    return ledger


def load_frozen_model(root: Path, candidate_record: Mapping[str, Any]) -> Any:
    model_path = resolve_repo_path(root, candidate_record["model_artifact"])
    if sha256_file(model_path) != candidate_record["model_artifact_sha256"]:
        raise CandidateFreezeError("Frozen model artifact hash mismatch")
    return joblib.load(model_path)


def _write_text_atomic(root: Path, path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    work_dir = root / "work" / "gate_b0_b1"
    work_dir.mkdir(parents=True, exist_ok=True)
    temporary = work_dir / f"{path.name}.{uuid4().hex}.tmp"
    temporary.write_text(text, encoding="utf-8", newline="\n")
    temporary.replace(path)


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=_json_default)


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return value.as_posix()
    if hasattr(value, "item"):
        return value.item()
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    raise TypeError(f"Cannot serialize {type(value).__name__}")

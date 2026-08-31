"""Executable governance amendments for Gate B6.

The original Gate B6 config and the B5 benchmark plan remain immutable.  This
module applies separately hashed execution amendments without rewriting that
historical preregistration record.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import yaml

from .benchmarking import canonical_json_sha256
from .data_contracts import ContractViolation, sha256_file


AMENDMENT_RELATIVE_PATH = Path("configs/gate_b6_amendment_no_tabpfn.yaml")


def load_b6_execution_amendment(root: Path) -> dict[str, Any]:
    """Load and strictly validate the frozen no-TabPFN execution amendment."""

    path = root / AMENDMENT_RELATIVE_PATH
    if not path.is_file():
        raise ContractViolation(f"Missing Gate B6 execution amendment: {AMENDMENT_RELATIVE_PATH.as_posix()}")
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ContractViolation("Gate B6 execution amendment must contain a mapping")
    required = {
        "schema_version",
        "amendment_id",
        "status",
        "scope",
        "excluded_models",
        "execution_catalog",
        "runtime_policy",
        "claim_scope",
    }
    missing = required - set(payload)
    if missing:
        raise ContractViolation(f"Gate B6 execution amendment is missing keys: {sorted(missing)}")
    excluded = payload["excluded_models"]
    if not isinstance(excluded, list) or len(excluded) != 1:
        raise ContractViolation("Gate B6 execution amendment must exclude exactly one historical model")
    record = excluded[0]
    if record.get("model_id") != "Z15_tabpfn_v2_6":
        raise ContractViolation("Only the historical Z15 TabPFN model may be excluded by B6-GOV-001")
    if record.get("execution_status") != "EXCLUDED_GOVERNANCE_USER_WITHDRAWAL":
        raise ContractViolation("Unexpected Gate B6 exclusion status")
    if any(
        bool(record.get(field))
        for field in ("predictions_existed_at_exclusion", "license_accepted", "weights_downloaded", "external_api_used")
    ):
        raise ContractViolation("B6-GOV-001 requires proof that TabPFN was never evaluated or staged")
    catalog = payload["execution_catalog"]
    if int(catalog.get("original_registry_model_count", -1)) != 23:
        raise ContractViolation("B6-GOV-001 original registry count must remain 23")
    if int(catalog.get("executable_model_count", -1)) != 22:
        raise ContractViolation("B6-GOV-001 executable model count must be 22")
    runtime = payload["runtime_policy"]
    if any(bool(value) for value in runtime.values()):
        raise ContractViolation("All TabPFN runtime capabilities must be prohibited")
    overrides = payload.get("environment_overrides", {})
    torch_override = overrides.get("b6_torch", {})
    if torch_override.get("historical_frozen_lock") != "requirements/b6_torch.lock.txt":
        raise ContractViolation("B6-GOV-001 must preserve the historical B5 torch lock")
    if torch_override.get("effective_runtime_lock") != "requirements/b6_torch_runtime.lock.txt":
        raise ContractViolation("B6-GOV-001 must declare the no-TabPFN runtime lock")
    runtime_lock = root / str(torch_override["effective_runtime_lock"])
    if not runtime_lock.is_file():
        raise ContractViolation("B6-GOV-001 runtime lock is missing")
    if "tabpfn" in runtime_lock.read_text(encoding="utf-8").lower():
        raise ContractViolation("B6-GOV-001 runtime lock must not contain TabPFN")
    return payload


def effective_environment_settings(
    root: Path,
    config: Mapping[str, Any],
    environment_id: str,
) -> dict[str, Any]:
    """Apply the separately frozen execution amendment to environment settings."""

    if environment_id not in config["environments"]:
        raise KeyError(environment_id)
    settings = dict(config["environments"][environment_id])
    override = load_b6_execution_amendment(root).get("environment_overrides", {}).get(
        environment_id, {}
    )
    if "effective_runtime_lock" in override:
        settings["lock"] = str(override["effective_runtime_lock"])
        settings["historical_frozen_lock"] = str(override["historical_frozen_lock"])
        settings["governance_amendment"] = "B6-GOV-001"
    return settings


def excluded_model_records(root: Path) -> dict[str, dict[str, Any]]:
    amendment = load_b6_execution_amendment(root)
    return {str(record["model_id"]): dict(record) for record in amendment["excluded_models"]}


def executable_model_ids(root: Path, registry: Mapping[str, Any]) -> set[str]:
    excluded = set(excluded_model_records(root))
    registered = {str(record["model_id"]) for record in registry["models"]}
    if not excluded <= registered:
        raise ContractViolation(f"Execution amendment references unknown models: {sorted(excluded - registered)}")
    executable = registered - excluded
    expected = int(load_b6_execution_amendment(root)["execution_catalog"]["executable_model_count"])
    if len(executable) != expected:
        raise ContractViolation(
            f"Gate B6 executable catalog count mismatch: expected {expected}, observed {len(executable)}"
        )
    return executable


def protocol_amendment_payload(root: Path, registry: Mapping[str, Any]) -> dict[str, Any]:
    amendment = load_b6_execution_amendment(root)
    excluded = excluded_model_records(root)
    executable = executable_model_ids(root, registry)
    registry_models = {str(record["model_id"]) for record in registry["models"]}
    return {
        "schema_version": 1,
        "amendment": amendment,
        "amendment_source": AMENDMENT_RELATIVE_PATH.as_posix(),
        "amendment_source_sha256": sha256_file(root / AMENDMENT_RELATIVE_PATH),
        "amendment_payload_sha256": canonical_json_sha256(amendment),
        "original_registry_model_count": len(registry_models),
        "executable_model_count": len(executable),
        "excluded_model_ids": sorted(excluded),
        "executable_model_ids": sorted(executable),
        "historical_registry_rewritten": False,
        "historical_job_manifest_rewritten": False,
        "selection_evidence_used_for_exclusion": False,
    }

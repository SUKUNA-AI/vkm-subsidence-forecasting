"""Frozen candidate record and one-time test access (stdlib only).

Order (``docs/governance/VALIDATION_POLICY_RU.md``):

1. contracts, metrics and acceptance criteria are fixed (their hashes enter the record);
2. the candidate is frozen: an immutable record binding dataset, contract, manifest, evaluation-spec,
   artifact and environment hashes, code commit and seed; its id is the hash of that content;
3. test access is claimed once in a ledger (exclusive file creation) before any label is read;
4. the access is finalised as ``consumed`` or ``failed_after_claim``.

Any claim counts as spent, including one that crashed after the claim. A second claim raises
``RepeatedTestAccessError``; a changed candidate needs a new record (new evaluation version).
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from ..core.io import resolve_repo_path, sha256_file, sha256_json, write_json_atomic
from .splits import SealedTestError

SCHEMA_VERSION = 1
TERMINAL_STATUSES = frozenset({"consumed", "failed_after_claim"})
_UNHASHED = ("candidate_id", "frozen_at_utc")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{7,40}$")


class CandidateFreezeError(ValueError):
    """The candidate record is incomplete, altered, or conflicts with an existing frozen record."""


class RepeatedTestAccessError(PermissionError):
    """Test access was already claimed (or finalised) for this ledger."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _require_sha(name: str, value: Any) -> str:
    if not isinstance(value, str) or not _SHA256.match(value):
        raise CandidateFreezeError(f"{name} must be a lowercase sha256 hex digest")
    return value


def _require_sha_map(name: str, mapping: Mapping[str, str], *, required: tuple[str, ...] = ()) -> dict[str, str]:
    out = {str(k): _require_sha(f"{name}[{k}]", v) for k, v in mapping.items()}
    missing = [k for k in required if k not in out]
    if not out or missing:
        raise CandidateFreezeError(f"{name} is empty or misses {missing}")
    return dict(sorted(out.items()))


# ---------------------------------------------------------------- candidate record
def build_candidate_record(*, task: str, split_version: str, model_id: str, dataset_sha256: str,
                           contract_hashes: Mapping[str, str], manifest_hashes: Mapping[str, str],
                           evaluation_spec_sha256: str, code_commit: str, random_seed: int,
                           environment_sha256: str, artifact_hashes: Mapping[str, str] | None = None,
                           notes: str | None = None) -> dict[str, Any]:
    """Immutable record; ``candidate_id`` is derived from the content (timestamp excluded)."""
    for name, value in (("task", task), ("split_version", split_version), ("model_id", model_id)):
        if not str(value).strip():
            raise CandidateFreezeError(f"{name} must be non-empty")
    if not _COMMIT.match(str(code_commit)):
        raise CandidateFreezeError("code_commit must be a git commit sha (7-40 hex)")
    if isinstance(random_seed, bool) or not isinstance(random_seed, int):
        raise CandidateFreezeError("random_seed must be an integer")
    body: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "frozen",
        "task": str(task),
        "split_version": str(split_version),
        "model_id": str(model_id),
        "dataset_sha256": _require_sha("dataset_sha256", dataset_sha256),
        "contract_hashes": _require_sha_map("contract_hashes", contract_hashes),
        "manifest_hashes": _require_sha_map("manifest_hashes", manifest_hashes, required=("train",)),
        "evaluation_spec_sha256": _require_sha("evaluation_spec_sha256", evaluation_spec_sha256),
        "code_commit": str(code_commit),
        "random_seed": random_seed,
        "environment_sha256": _require_sha("environment_sha256", environment_sha256),
        "artifact_hashes": _require_sha_map("artifact_hashes", artifact_hashes) if artifact_hashes else {},
        "test_access_policy": "one_time_ledger",
    }
    if notes:
        body["notes"] = notes
    return {**body, "candidate_id": f"cand-{candidate_digest(body)[:16]}"}


def candidate_digest(record: Mapping[str, Any]) -> str:
    return sha256_json({k: v for k, v in record.items() if k not in _UNHASHED})


def verify_candidate_record(record: Mapping[str, Any]) -> None:
    """Status is frozen and the id matches the content (detects edits after freezing)."""
    if record.get("status") != "frozen":
        raise CandidateFreezeError("only a record with status=frozen is a candidate")
    cid = str(record.get("candidate_id", ""))
    if cid != f"cand-{candidate_digest(record)[:16]}":
        raise CandidateFreezeError(f"candidate record {cid or '<no id>'} does not match its content")


def freeze_candidate(root: str | Path, target: str | Path, record: Mapping[str, Any]) -> dict[str, Any]:
    """Write the record once. Re-freezing identical content is a no-op; different content is refused."""
    verify_candidate_record(record)
    path = resolve_repo_path(root, target)
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing.get("candidate_id") == record["candidate_id"] and \
                candidate_digest(existing) == candidate_digest(record):
            return existing
        raise CandidateFreezeError(f"a different frozen candidate already exists at {target}; "
                                   "create a new candidate version")
    frozen = {**record, "frozen_at_utc": utc_now()}
    write_json_atomic(root, target, frozen, work_scope="validation")
    return frozen


def verify_candidate_artifacts(root: str | Path, record: Mapping[str, Any]) -> list[str]:
    """Errors for frozen artifacts that are missing or changed on disk (empty list = intact)."""
    errors = []
    for rel, expected in record.get("artifact_hashes", {}).items():
        p = resolve_repo_path(root, rel)
        if not p.is_file():
            errors.append(f"{rel}: missing")
        elif sha256_file(p) != expected:
            errors.append(f"{rel}: sha256 changed after freezing")
    return errors


def authorize_test_access(record: Mapping[str, Any] | None, *, task: str, split_version: str,
                          contract_hashes: Mapping[str, str], manifest_hashes: Mapping[str, str]) -> str:
    """Return the candidate id if ``record`` is a valid frozen candidate for this exact setup, else SealedTestError."""
    if record is None:
        raise SealedTestError("test is sealed: freeze a candidate first")
    try:
        verify_candidate_record(record)
    except CandidateFreezeError as exc:
        raise SealedTestError(f"test is sealed: {exc}") from exc
    if record["task"] != task or record["split_version"] != split_version:
        raise SealedTestError("candidate was frozen for a different task or split version")
    if record["contract_hashes"] != dict(sorted(contract_hashes.items())):
        raise SealedTestError("candidate contract hashes differ from the current contracts")
    for split, sha in manifest_hashes.items():
        if record["manifest_hashes"].get(split) != sha:
            raise SealedTestError(f"candidate {split} manifest hash differs")
    return str(record["candidate_id"])


# ---------------------------------------------------------------- one-time test-access ledger
def claim_test_access(root: str | Path, ledger_target: str | Path, record: Mapping[str, Any], *,
                      test_sample_ids_sha256: str, test_rows: int) -> dict[str, Any]:
    """Irreversibly claim the single test access before any test label is loaded."""
    path = resolve_repo_path(root, ledger_target)
    if path.exists():
        raise RepeatedTestAccessError(f"test access already claimed: {ledger_target}")
    verify_candidate_record(record)
    ledger = {
        "schema_version": SCHEMA_VERSION,
        "access_event_id": uuid4().hex,
        "candidate_id": record["candidate_id"],
        "candidate_content_sha256": candidate_digest(record),
        "test_sample_ids_sha256": _require_sha("test_sample_ids_sha256", test_sample_ids_sha256),
        "test_rows": int(test_rows),
        "claimed_at_utc": utc_now(),
        "status": "opening",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    try:  # exclusive creation: the file's existence is the claim, even if the process dies later
        with open(path, "x", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(ledger, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except FileExistsError as exc:
        raise RepeatedTestAccessError(f"test access already claimed: {ledger_target}") from exc
    return ledger


def finalize_test_access(root: str | Path, ledger_target: str | Path, *, status: str,
                         output_hashes: Mapping[str, str] | None = None, error: str | None = None) -> dict[str, Any]:
    path = resolve_repo_path(root, ledger_target)
    if not path.is_file():
        raise RepeatedTestAccessError("cannot finalise a test access that was never claimed")
    ledger = json.loads(path.read_text(encoding="utf-8"))
    if ledger.get("status") != "opening":
        raise RepeatedTestAccessError(f"test access ledger is already terminal: {ledger.get('status')}")
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"terminal status must be one of {sorted(TERMINAL_STATUSES)}")
    ledger.update(status=status, finalized_at_utc=utc_now())
    if output_hashes:
        ledger["output_hashes"] = {k: _require_sha(f"output_hashes[{k}]", v) for k, v in sorted(output_hashes.items())}
    if error:
        ledger["error"] = error
    write_json_atomic(root, ledger_target, ledger, work_scope="validation")
    return ledger


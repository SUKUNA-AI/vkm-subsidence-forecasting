"""Frozen candidate record and one-time test access (stdlib only).

Order (``docs/governance/VALIDATION_POLICY_RU.md``):

1. contracts, metrics and acceptance criteria are fixed (their hashes enter the record);
2. the candidate is frozen: an immutable record binding dataset, contract, manifest, evaluation-spec,
   artifact and environment hashes, code commit and seed; its id is the hash of that content;
3. authorization verifies the persisted candidate and current identities; test access is then
   claimed once in a ledger (exclusive file creation) before any label is read;
4. the access is finalised as ``consumed`` or ``failed_after_claim``.

Any claim counts as spent, including one that crashed after the claim. A second claim raises
``RepeatedTestAccessError``; a changed candidate needs a new record (new evaluation version).
"""
from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterator, Mapping
from uuid import uuid4

from ..core.io import PathPolicyError, resolve_repo_path, sha256_file, sha256_json, write_json_atomic
from .splits import SealedTestError

SCHEMA_VERSION = 1
TERMINAL_STATUSES = frozenset({"consumed", "failed_after_claim"})
_UNHASHED = ("candidate_id", "frozen_at_utc")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{7,40}$")
_REQUIRED_FIELDS = frozenset({"schema_version", "status", "task", "split_version", "model_id",
    "dataset_sha256", "contract_hashes", "manifest_hashes", "evaluation_spec_sha256", "code_commit",
    "random_seed", "environment_sha256", "artifact_hashes", "test_access_policy", "candidate_id"})


class CandidateFreezeError(ValueError):
    """The candidate record is incomplete, altered, or conflicts with an existing frozen record."""


class RepeatedTestAccessError(PermissionError):
    """Test access was already claimed (or finalised) for this ledger."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _require_sha(name: str, value: Any) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise CandidateFreezeError(f"{name} must be a lowercase sha256 hex digest")
    return value


def _require_sha_map(name: str, mapping: Mapping[str, str], *, required: tuple[str, ...] = ()) -> dict[str, str]:
    if not isinstance(mapping, Mapping):
        raise CandidateFreezeError(f"{name} must be a mapping")
    if any(not isinstance(k, str) or not k.strip() for k in mapping):
        raise CandidateFreezeError(f"{name} keys must be non-empty strings")
    out = {k: _require_sha(f"{name}[{k}]", v) for k, v in mapping.items()}
    missing = [k for k in required if k not in out]
    if not out or missing:
        raise CandidateFreezeError(f"{name} is empty or misses {missing}")
    return dict(sorted(out.items()))


def _artifact_hashes(mapping: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(mapping, Mapping):
        raise CandidateFreezeError("artifact_hashes must be a mapping")
    out = _require_sha_map("artifact_hashes", mapping) if mapping else {}
    for rel in out:
        win = PureWindowsPath(rel)
        if PurePosixPath(rel).is_absolute() or win.drive or win.root or "\\" in rel or \
                ".." in PurePosixPath(rel).parts or rel == ".":
            raise CandidateFreezeError("artifact_hashes keys must be repository-relative file paths")
    return out


# ---------------------------------------------------------------- candidate record
def build_candidate_record(*, task: str, split_version: str, model_id: str, dataset_sha256: str,
                           contract_hashes: Mapping[str, str], manifest_hashes: Mapping[str, str],
                           evaluation_spec_sha256: str, code_commit: str, random_seed: int,
                           environment_sha256: str, artifact_hashes: Mapping[str, str] | None = None,
                           notes: str | None = None) -> dict[str, Any]:
    """Immutable record; ``candidate_id`` is derived from the content (timestamp excluded)."""
    for name, value in (("task", task), ("split_version", split_version), ("model_id", model_id)):
        if not isinstance(value, str) or not value.strip():
            raise CandidateFreezeError(f"{name} must be a non-empty string")
    if not isinstance(code_commit, str) or not _COMMIT.fullmatch(code_commit):
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
        "artifact_hashes": _artifact_hashes(artifact_hashes if artifact_hashes is not None else {}),
        "test_access_policy": "one_time_ledger",
    }
    if notes is not None and (not isinstance(notes, str) or not notes.strip()):
        raise CandidateFreezeError("notes must be a non-empty string when supplied")
    if notes is not None:
        body["notes"] = notes
    return {**body, "candidate_id": f"cand-{candidate_digest(body)[:16]}"}


def candidate_digest(record: Mapping[str, Any]) -> str:
    return sha256_json({k: v for k, v in record.items() if k not in _UNHASHED})


def verify_candidate_record(record: Mapping[str, Any]) -> None:
    """Validate the complete schema and content identity; a self-hash alone is insufficient."""
    if not isinstance(record, Mapping):
        raise CandidateFreezeError("candidate record must be a mapping")
    if any(not isinstance(key, str) for key in record):
        raise CandidateFreezeError("candidate record keys must be strings")
    missing = _REQUIRED_FIELDS - record.keys()
    extra = record.keys() - (_REQUIRED_FIELDS | {"notes", "frozen_at_utc"})
    if missing or extra:
        raise CandidateFreezeError(f"candidate record has missing fields {sorted(missing)} or unknown fields {sorted(extra)}")
    if type(record["schema_version"]) is not int or record["schema_version"] != SCHEMA_VERSION:
        raise CandidateFreezeError("candidate schema_version is unsupported")
    if record.get("status") != "frozen":
        raise CandidateFreezeError("only a record with status=frozen is a candidate")
    if record["test_access_policy"] != "one_time_ledger":
        raise CandidateFreezeError("candidate test_access_policy must be one_time_ledger")
    if "notes" in record and (not isinstance(record["notes"], str) or not record["notes"].strip()):
        raise CandidateFreezeError("notes must be a non-empty string when supplied")
    build_candidate_record(**{key: record[key] for key in (
        "task", "split_version", "model_id", "dataset_sha256", "contract_hashes", "manifest_hashes",
        "evaluation_spec_sha256", "code_commit", "random_seed", "environment_sha256", "artifact_hashes")},
        notes=record.get("notes"))
    if "frozen_at_utc" in record:
        try:
            timestamp = datetime.fromisoformat(record["frozen_at_utc"])
            if timestamp.utcoffset() != timezone.utc.utcoffset(timestamp):
                raise ValueError("timestamp must be UTC")
        except (TypeError, ValueError) as exc:
            raise CandidateFreezeError("frozen_at_utc must be a UTC timestamp") from exc
    cid = str(record.get("candidate_id", ""))
    if cid != f"cand-{candidate_digest(record)[:16]}":
        raise CandidateFreezeError(f"candidate record {cid or '<no id>'} does not match its content")


def freeze_candidate(root: str | Path, target: str | Path, record: Mapping[str, Any]) -> dict[str, Any]:
    """Write the record once. Re-freezing identical content is a no-op; different content is refused."""
    verify_candidate_record(record)
    path = resolve_repo_path(root, target)
    frozen = {**record, "frozen_at_utc": utc_now()}
    tmp_dir = resolve_repo_path(root, "work/validation")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / f"{path.name}.{uuid4().hex}.tmp"
    try:
        with open(tmp, "x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(frozen, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # Publish a complete record without replacing an existing name. Unsupported hardlinks
            # or different filesystems fail closed; never fall back to an overwriting rename.
            os.link(tmp, path)
        except FileExistsError:
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                verify_candidate_record(existing)
            except (OSError, ValueError) as exc:
                raise CandidateFreezeError(f"existing frozen candidate is invalid at {target}") from exc
            if "frozen_at_utc" in existing and existing["candidate_id"] == record["candidate_id"] and \
                    candidate_digest(existing) == candidate_digest(record):
                return existing
            raise CandidateFreezeError(f"a different frozen candidate already exists at {target}; "
                                       "create a new candidate version")
        return frozen
    finally:
        tmp.unlink(missing_ok=True)


def verify_candidate_artifacts(root: str | Path, record: Mapping[str, Any]) -> list[str]:
    """Errors for frozen artifacts that are missing or changed on disk (empty list = intact)."""
    verify_candidate_record(record)
    errors = []
    for rel, expected in record.get("artifact_hashes", {}).items():
        p = resolve_repo_path(root, rel)
        if not p.is_file():
            errors.append(f"{rel}: missing")
        elif sha256_file(p) != expected:
            errors.append(f"{rel}: sha256 changed after freezing")
    return errors


def authorize_test_access(record: Mapping[str, Any] | None, *, root: str | Path, candidate_target: str | Path,
                          task: str, split_version: str, contract_hashes: Mapping[str, str],
                          manifest_hashes: Mapping[str, str], dataset_sha256: str,
                          evaluation_spec_sha256: str, environment_sha256: str,
                          artifact_hashes: Mapping[str, str]) -> str:
    """Return the candidate id if ``record`` is a valid frozen candidate for this exact setup, else SealedTestError."""
    if record is None:
        raise SealedTestError("test is sealed: freeze a candidate first")
    try:
        verify_candidate_record(record)
        persisted = json.loads(resolve_repo_path(root, candidate_target).read_text(encoding="utf-8"))
        verify_candidate_record(persisted)
        if "frozen_at_utc" not in persisted or candidate_digest(persisted) != candidate_digest(record):
            raise CandidateFreezeError("candidate does not match the persisted frozen record")
        current = {
            "dataset_sha256": _require_sha("dataset_sha256", dataset_sha256),
            "evaluation_spec_sha256": _require_sha("evaluation_spec_sha256", evaluation_spec_sha256),
            "environment_sha256": _require_sha("environment_sha256", environment_sha256),
            "contract_hashes": _require_sha_map("contract_hashes", contract_hashes),
            "manifest_hashes": _require_sha_map("manifest_hashes", manifest_hashes, required=("train",)),
            "artifact_hashes": _artifact_hashes(artifact_hashes),
        }
        if not current["artifact_hashes"]:
            raise CandidateFreezeError("test access requires non-empty frozen artifact_hashes")
    except (CandidateFreezeError, OSError, PathPolicyError, json.JSONDecodeError) as exc:
        raise SealedTestError(f"test is sealed: {exc}") from exc
    if record["task"] != task or record["split_version"] != split_version:
        raise SealedTestError("candidate was frozen for a different task or split version")
    for name, expected in current.items():
        if record[name] != expected:
            raise SealedTestError(f"candidate {name} differs from the current setup")
    try:
        artifact_errors = verify_candidate_artifacts(root, persisted)
    except (OSError, PathPolicyError) as exc:
        raise SealedTestError(f"test is sealed: cannot verify frozen artifacts: {exc}") from exc
    if artifact_errors:
        raise SealedTestError(f"test is sealed: frozen artifacts differ: {'; '.join(artifact_errors)}")
    return str(record["candidate_id"])


# ---------------------------------------------------------------- one-time test-access ledger
@contextmanager
def _terminal_ledger_lock(path: Path) -> Iterator[None]:
    """Native, nonblocking process lock. Keep its inode; process death releases ownership."""
    lock_path = resolve_repo_path(path.parent, path.name + ".finalize.lock")
    with open(lock_path, "a+b") as lock:
        if os.name == "nt":
            import msvcrt
            if os.fstat(lock.fileno()).st_size == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            acquire = lambda: msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            release = lambda: (lock.seek(0), msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1))
        else:
            import fcntl
            acquire = lambda: fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = lambda: fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        try:
            acquire()
        except OSError as exc:
            raise RepeatedTestAccessError("test access finalization is locked or unsupported") from exc
        try:
            yield
        finally:
            release()


def claim_test_access(root: str | Path, ledger_target: str | Path, record: Mapping[str, Any], *,
                      test_sample_ids_sha256: str, test_rows: int) -> dict[str, Any]:
    """Irreversibly spend access. Authorization must separately succeed before reading labels."""
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
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"terminal status must be one of {sorted(TERMINAL_STATUSES)}")
    outputs = {k: _require_sha(f"output_hashes[{k}]", v) for k, v in sorted(output_hashes.items())} \
        if output_hashes else {}
    with _terminal_ledger_lock(path):
        ledger = json.loads(path.read_text(encoding="utf-8"))
        if ledger.get("status") != "opening":
            raise RepeatedTestAccessError(f"test access ledger is already terminal: {ledger.get('status')}")
        ledger.update(status=status, finalized_at_utc=utc_now())
        if outputs:
            ledger["output_hashes"] = outputs
        if error:
            ledger["error"] = error
        write_json_atomic(root, ledger_target, ledger, work_scope="validation")
        return ledger

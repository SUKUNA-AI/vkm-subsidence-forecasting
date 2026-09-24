"""Pinned v2 identity and executable Python model-worker file boundary."""
from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import threading

DATASET = "SKRU1_SCENARIO_SIMULATION_V2_1"
DATA_DIRECTORY = "data/scenario_simulation_v2_1"
DATA_SHA = "a268cd7800e1320a1cc61172f95cf458c7ac7434237b32b288f23a83a97a2c95"
CONSTRAINTS_SHA = "96b88b61cf766062c1f831e32b50bdd82441da63c50ce41569a5ffad297de6ef"
MODEL_FILES = frozenset({
    "manifest.json", "feature_contract.json", "model_features.csv.gz",
    "causal_history.csv.gz", "sequence_windows.csv.gz", "split_assignments.csv.gz",
    "targets/train_observed.csv.gz",
})
_SCOPES: list[Path] = []
_LOCK = threading.RLock()


def file_sha256(path: Path) -> str:
    h = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def json_sha256(value) -> str:
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _audit(event, args):
    if not _SCOPES:
        return
    if event in {"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn"}:
        raise PermissionError("Model worker cannot launch a process outside its file boundary")
    if event != "open" or not isinstance(args[0], (str, bytes, os.PathLike)):
        return
    path = Path(os.fsdecode(args[0])).resolve()
    for root in tuple(_SCOPES):
        data = root / DATA_DIRECTORY
        if path.is_relative_to(data):
            relative = path.relative_to(data).as_posix()
            if relative not in MODEL_FILES:
                raise PermissionError(f"Model worker denied release payload: {relative}")
            mode = args[1] or ""
            flags = args[2] or 0
            if any(c in mode for c in "wa+") or flags & (os.O_WRONLY | os.O_RDWR):
                raise PermissionError("Model worker cannot write frozen data")
        elif any(path.is_relative_to(root / prefix) for prefix in (
            "data", "SKRU1_ACTUAL_DATA_TABLES_v1", "inputs/holdout_candidates",
            "artifacts/splits/t1_v1", "artifacts/splits/t5_v1",
        )):
            raise PermissionError("Model worker denied historical/other dataset access")


sys.addaudithook(_audit)


@contextmanager
def model_worker_scope(root: str | Path):
    """Deny non-model payloads across Python threads; scorer runs outside this scope.

    The guard does not claim to sandbox arbitrary native extensions. Do not pass
    already-open truth descriptors or objects into a worker.
    """
    root = Path(root).resolve()
    with _LOCK:
        _SCOPES.append(root)
    try:
        yield
    finally:
        with _LOCK:
            _SCOPES.remove(root)


def verified_manifest(root: Path) -> dict:
    path = root / DATA_DIRECTORY / "manifest.json"
    if file_sha256(path) != DATA_SHA:
        raise ValueError("Frozen v2 manifest identity mismatch")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest["dataset_id"] != DATASET:
        raise ValueError("Wrong dataset identity")
    return manifest


def verified_payload(root: Path, manifest: dict, relative: str) -> Path:
    directory = (root / DATA_DIRECTORY).resolve()
    path = (directory / relative).resolve()
    if not path.is_relative_to(directory):
        raise ValueError("Payload path escapes frozen release")
    records = {row["path"]: row for row in manifest["outputs"]}
    record = records[relative]
    if path.stat().st_size != record["size_bytes"] or file_sha256(path) != record["sha256"]:
        raise ValueError(f"Frozen payload mismatch: {relative}")
    return path

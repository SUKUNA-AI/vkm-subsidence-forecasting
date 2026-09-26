"""Deterministic, repository-relative artifact I/O (stdlib only).

Rules (``docs/governance/DATA_AND_PATH_POLICY_RU.md``):

* working paths are relative to the repository root; absolute paths (POSIX or Windows, e.g. ``E:\\...``)
  and paths escaping the root (``..``, symlinks) are rejected;
* temporary files live only under ``<root>/work/`` (git-ignored); the final file appears atomically
  via ``os.replace``, so a reader never sees a half-written artifact;
* text is UTF-8 with ``\\n`` line endings; JSON is sorted and indented, hashes use canonical JSON;
* generated artifacts are deterministic: inventories and snapshots are sorted and carry no timestamps.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from datetime import date, datetime
from enum import Enum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable, Mapping, Sequence
from uuid import uuid4

WORK_DIR = "work"
_CHUNK = 1 << 20


class PathPolicyError(ValueError):
    """A path violates the repository path policy (absolute, Windows-style or escaping the root)."""


# ---------------------------------------------------------------- hashing
def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_json(payload: Any) -> str:
    """Compact, key-sorted JSON used for hashing (independent of indentation and key order)."""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=_json_default)


def sha256_json(payload: Any) -> str:
    return sha256_bytes(canonical_json(payload).encode("utf-8"))


# ---------------------------------------------------------------- paths
def resolve_repo_path(root: str | Path, relative: str | Path) -> Path:
    """Resolve a repository-relative path; reject absolute paths and escapes from ``root``."""
    text = str(relative)
    if not text.strip():
        raise PathPolicyError("empty artifact path")
    win = PureWindowsPath(text)
    if Path(text).is_absolute() or PurePosixPath(text).is_absolute() or win.drive or win.root:
        raise PathPolicyError(f"artifact path must be repository-relative, got absolute path: {text}")
    if "\\" in text:
        raise PathPolicyError(f"artifact path must use POSIX separators: {text}")
    base = Path(root).resolve()
    resolved = (base / text).resolve()
    if not resolved.is_relative_to(base):
        raise PathPolicyError(f"artifact path escapes repository root: {text}")
    return resolved


def _target(root: Path, target: str | Path) -> Path:
    """Relative → ``resolve_repo_path``; absolute → must already lie inside ``root``."""
    if Path(target).is_absolute():
        resolved = Path(target).resolve()
        if not resolved.is_relative_to(root.resolve()):
            raise PathPolicyError(f"artifact path is outside repository root: {target}")
        return resolved
    return resolve_repo_path(root, target)


def repo_relative(root: str | Path, path: str | Path) -> str:
    """POSIX path of ``path`` relative to ``root`` (error if outside)."""
    return _target(Path(root), path).relative_to(Path(root).resolve()).as_posix()


# ---------------------------------------------------------------- atomic writers
def write_text_atomic(root: str | Path, target: str | Path, text: str, *, work_scope: str = "io") -> Path:
    """Write UTF-8 text with ``\\n`` newlines via a temporary file in ``<root>/work/<work_scope>/``."""
    root = Path(root)
    path = _target(root, target)
    tmp_dir = resolve_repo_path(root, f"{WORK_DIR}/{work_scope}")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / f"{path.name}.{uuid4().hex}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def write_json_atomic(root: str | Path, target: str | Path, payload: Any, *, work_scope: str = "io") -> Path:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=_json_default)
    return write_text_atomic(root, target, text + "\n", work_scope=work_scope)


def write_csv_atomic(root: str | Path, target: str | Path, rows: Iterable[Mapping[str, Any]],
                     fieldnames: Sequence[str], *, work_scope: str = "io") -> Path:
    """Write rows in the given column order. Unknown keys are an error; missing keys become empty cells."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(fieldnames), extrasaction="raise", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: ("" if v is None else v) for k, v in row.items()})
    return write_text_atomic(root, target, buf.getvalue(), work_scope=work_scope)


# ---------------------------------------------------------------- inventories
def _file_row(root: Path, path: Path) -> dict[str, Any]:
    return {"path": path.relative_to(root).as_posix(), "bytes": path.stat().st_size, "sha256": sha256_file(path)}


def artifact_inventory(root: str | Path, paths: Iterable[str | Path]) -> list[dict[str, Any]]:
    """Sorted ``path, bytes, sha256`` rows for existing files inside ``root``."""
    base = Path(root).resolve()
    resolved = {_target(base, p) for p in paths}
    missing = sorted(str(p) for p in resolved if not p.is_file())
    if missing:
        raise FileNotFoundError(f"inventory files are missing: {missing}")
    return sorted((_file_row(base, p) for p in resolved), key=lambda r: r["path"])


def snapshot_paths(root: str | Path, relative_roots: Iterable[str | Path]) -> dict[str, Any]:
    """Content-hash snapshot of files/directories (recursively), to prove later that they were not mutated."""
    base = Path(root).resolve()
    files: set[Path] = set()
    for rel in relative_roots:
        p = resolve_repo_path(base, rel)
        if p.is_file():
            files.add(p)
        elif p.is_dir():
            files.update(x for x in p.rglob("*") if x.is_file())
        else:
            raise FileNotFoundError(f"snapshot root is missing: {rel}")
    rows = sorted((_file_row(base, p) for p in files), key=lambda r: r["path"])
    return {
        "schema_version": 1,
        "policy": "content_hash_snapshot_no_mutation",
        "file_count": len(rows),
        "snapshot_sha256": sha256_json(rows),
        "files": rows,
    }


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if hasattr(value, "model_dump"):  # pydantic
        return value.model_dump(mode="json", exclude_none=True)
    if hasattr(value, "item"):  # numpy scalar
        return value.item()
    raise TypeError(f"cannot serialize {type(value).__name__}")

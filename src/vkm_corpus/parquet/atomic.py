"""Atomic, write-once file creation inside a data root.

Protocol: write to ``<root>/tmp/<uuid>.<name>`` → flush → fsync (Windows needs a descriptor opened ``r+b``) →
``os.replace`` onto the final path. The final path must not exist (immutability) unless it already holds exactly the
same bytes (idempotent retry). Directories are fsynced where the OS allows it.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any, Callable


class ImmutableFileError(FileExistsError):
    """The target exists with different content: canonical files are never overwritten."""


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fsync_file(path: Path) -> None:
    with open(path, "r+b") as f:
        f.flush()
        os.fsync(f.fileno())


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def tmp_path(tmp_dir: Path, name: str) -> Path:
    tmp_dir.mkdir(parents=True, exist_ok=True)
    return tmp_dir / f".{uuid.uuid4().hex}.{Path(name).name}.tmp"


def publish(tmp: Path, target: Path, *, overwrite: bool = False) -> bool:
    """Move a finished temporary file to ``target``. Returns False (and removes tmp) when ``target`` already holds the
    same bytes; raises ImmutableFileError if it holds different bytes and ``overwrite`` is False."""
    fsync_file(tmp)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not overwrite:
        same = sha256_of(target) == sha256_of(tmp)
        tmp.unlink(missing_ok=True)
        if same:
            return False
        raise ImmutableFileError(f"refusing to overwrite an immutable file: {target.name}")
    os.replace(tmp, target)
    _fsync_dir(target.parent)
    return True


def write_bytes(tmp_dir: Path, target: Path, data: bytes, *, overwrite: bool = False) -> bool:
    tmp = tmp_path(tmp_dir, target.name)
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        return publish(tmp, target, overwrite=overwrite)
    finally:
        tmp.unlink(missing_ok=True)


def dump_json(obj: Any) -> str:
    """Deterministic JSON text of markers and manifests: sorted keys, indent 1, LF, UTF-8."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=1) + "\n"


def write_json(tmp_dir: Path, target: Path, obj: Any, *, overwrite: bool = False) -> bool:
    return write_bytes(tmp_dir, target, dump_json(obj).encode("utf-8"), overwrite=overwrite)


def write_with(tmp_dir: Path, target: Path, writer: Callable[[Path], None], *, overwrite: bool = False) -> bool:
    """Let ``writer(tmp_path)`` produce the file (e.g. ``pyarrow.parquet.write_table``), then publish atomically."""
    tmp = tmp_path(tmp_dir, target.name)
    try:
        writer(tmp)
        return publish(tmp, target, overwrite=overwrite)
    finally:
        tmp.unlink(missing_ok=True)


def create_exclusive(path: Path, text: str) -> bool:
    """Create ``path`` only if it does not exist (O_CREAT|O_EXCL). Returns False if it exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    return True

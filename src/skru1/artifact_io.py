"""Deterministic repository-relative artifact I/O helpers."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import uuid4

import pandas as pd

from .data_contracts import ContractViolation, sha256_file


def resolve_repo_path(root: Path, relative: str | Path) -> Path:
    raw = Path(relative)
    if raw.is_absolute():
        raise ContractViolation(f"Artifact path must be repository-relative: {relative}")
    resolved = (root / raw).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ContractViolation(f"Artifact path escapes repository root: {relative}") from exc
    return resolved


def write_text_atomic(root: Path, path: Path, text: str, *, work_scope: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    work_dir = root / "work" / work_scope
    work_dir.mkdir(parents=True, exist_ok=True)
    temporary = work_dir / f"{path.name}.{uuid4().hex}.tmp"
    temporary.write_text(text, encoding="utf-8", newline="\n")
    temporary.replace(path)


def write_json_atomic(
    root: Path,
    path: Path,
    payload: Mapping[str, Any] | list[Any],
    *,
    work_scope: str,
) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=_json_default)
    write_text_atomic(root, path, text + "\n", work_scope=work_scope)


def write_csv_atomic(
    root: Path,
    path: Path,
    frame: pd.DataFrame,
    *,
    work_scope: str,
) -> None:
    write_text_atomic(
        root,
        path,
        frame.to_csv(index=False, lineterminator="\n"),
        work_scope=work_scope,
    )


def snapshot_paths(root: Path, relative_roots: Iterable[str | Path]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for relative in relative_roots:
        path = resolve_repo_path(root, relative)
        if path.is_file():
            files = [path]
        elif path.is_dir():
            files = sorted(item for item in path.rglob("*") if item.is_file())
        else:
            raise FileNotFoundError(f"Protected predecessor is missing: {path}")
        for file_path in files:
            rows.append(
                {
                    "path": file_path.relative_to(root).as_posix(),
                    "bytes": file_path.stat().st_size,
                    "sha256": sha256_file(file_path),
                }
            )
    rows.sort(key=lambda row: row["path"])
    digest = sha256(
        json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": 1,
        "policy": "content_hash_snapshot_no_mutation",
        "file_count": len(rows),
        "snapshot_sha256": digest,
        "files": rows,
    }


def artifact_inventory(root: Path, paths: Iterable[Path]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for path in sorted(set(paths)):
        if not path.is_file():
            raise FileNotFoundError(path)
        rows.append(
            {
                "path": path.relative_to(root).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return pd.DataFrame(rows)


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(f"Cannot serialize {type(value).__name__}")

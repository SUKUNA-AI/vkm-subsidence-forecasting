"""Receipts of pipeline transformations: inputs, command, exit code, output hashes, counts.

A receipt must be public-safe: numbers, hashes, versions, logical paths. ``check_public_safe`` rejects forbidden
field names (CP-04), absolute host paths and private IPv4 addresses before a receipt is written; receipts that go to
PUBLIC ``docs/corpus_platform/receipts/`` are additionally whitelisted by the coordinator's serializer (H-40).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

FORBIDDEN_KEYS = frozenset({"quote", "verbatim_quote", "ocr_text", "page_text", "full_text"})
_HOST_PATH = re.compile(r"(?:^|[\s\"'=(])(?:[A-Za-z]:[\\/]|/home/|/mnt/[a-z]/|/Users/|/root/)")
_PRIVATE_IPV4 = re.compile(r"(?<![\d.])(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|"
                           r"172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})(?![\d.])")


def _walk(obj: Any, path: str, problems: list[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in FORBIDDEN_KEYS:
                problems.append(f"{path}/{k}: forbidden key")
            _walk(v, f"{path}/{k}", problems)
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _walk(v, f"{path}/{i}", problems)
    elif isinstance(obj, str):
        if _HOST_PATH.search(obj):
            problems.append(f"{path}: absolute host path")
        if _PRIVATE_IPV4.search(obj):
            problems.append(f"{path}: private IPv4 address")


def check_public_safe(payload: Any) -> list[str]:
    problems: list[str] = []
    _walk(payload, "", problems)
    return problems


def write_receipt(path: Path, payload: dict[str, Any], *, strict: bool = True) -> Path:
    """Write a receipt as deterministic, pretty JSON (atomic replace)."""
    problems = check_public_safe(payload)
    if problems and strict:
        raise ValueError("receipt is not public-safe: " + "; ".join(problems[:10]))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True, default=str) + "\n",
                   encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    return path

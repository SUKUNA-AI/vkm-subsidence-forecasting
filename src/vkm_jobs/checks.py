"""Expected-result checks evaluated by the runner after the process ends (plan §2.3).

``file_exists`` (path, glob allowed), ``json_value`` (JSON Pointer equals ``expected``; numbers compared with
``rtol``/``atol`` when given), ``number_close`` (JSON Pointer, |actual − expected| ≤ atol + rtol·|expected|),
``text_contains`` / ``text_absent`` (substring in a text file), ``exit_code``. A check never raises: a missing file or
pointer is a failed check with a message.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from vkm_jobs.errors import ToolFailure
from vkm_jobs.roots import resolve_inside

MAX_TEXT_BYTES = 64 * 1024 * 1024
_MISSING = object()


def json_pointer(doc: Any, pointer: str) -> Any:
    """RFC 6901 lookup; returns the module-private ``_MISSING`` sentinel when the pointer does not resolve."""
    if pointer in ("", None):
        return doc
    cur = doc
    for raw in pointer.lstrip("/").split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, dict):
            if token not in cur:
                return _MISSING
            cur = cur[token]
        elif isinstance(cur, list):
            if not token.isdigit() or int(token) >= len(cur):
                return _MISSING
            cur = cur[int(token)]
        else:
            return _MISSING
    return cur


def _close(actual: Any, expected: Any, rtol: float, atol: float) -> bool:
    """Structural comparison; numbers within ``atol + rtol·|expected|`` (``0, 0`` = exact); bools never equal ints."""
    if isinstance(actual, bool) or isinstance(expected, bool):
        return actual is expected
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        if not (math.isfinite(actual) and math.isfinite(expected)):
            return actual == expected
        return abs(actual - expected) <= atol + rtol * abs(expected)
    if isinstance(actual, list) and isinstance(expected, list) and len(actual) == len(expected):
        return all(_close(a, e, rtol, atol) for a, e in zip(actual, expected))
    if isinstance(actual, dict) and isinstance(expected, dict) and actual.keys() == expected.keys():
        return all(_close(actual[k], expected[k], rtol, atol) for k in actual)
    return actual == expected


def _file(job_dir: Path, rel: str) -> Path:
    return resolve_inside(job_dir, rel, what="check path")


def _short(value: Any, limit: int = 300) -> Any:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return value if len(text) <= limit else text[:limit] + "…"


def run_check(job_dir: Path, check: dict[str, Any], exit_code: int | None) -> dict[str, Any]:
    out = {k: check.get(k) for k in ("name", "kind", "path", "pointer", "expected", "rtol", "atol")}
    out = {k: v for k, v in out.items() if v is not None}
    kind = check["kind"]
    try:
        if kind == "exit_code":
            out.update(actual=exit_code, passed=exit_code == check["expected"])
            return out
        rel = check["path"]
        if kind == "file_exists":
            if any(ch in rel for ch in "*?["):
                hits = sorted(p for p in job_dir.glob(rel) if p.is_file())
                out.update(actual=len(hits), passed=bool(hits))
            else:
                out.update(actual=_file(job_dir, rel).is_file(), passed=_file(job_dir, rel).is_file())
            return out
        path = _file(job_dir, rel)
        if not path.is_file():
            out.update(actual=None, passed=False, message="file not found")
            return out
        if path.stat().st_size > MAX_TEXT_BYTES:
            out.update(actual=None, passed=False, message="file too large for a check")
            return out
        if kind in ("json_value", "number_close"):
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                out.update(actual=None, passed=False, message=f"not JSON: {exc}")
                return out
            actual = json_pointer(doc, check.get("pointer") or "")
            if actual is _MISSING:
                out.update(actual=None, passed=False, message="pointer does not resolve")
                return out
            if kind == "number_close":
                ok = (isinstance(actual, (int, float)) and not isinstance(actual, bool)
                      and _close(float(actual), float(check["expected"]), check["rtol"], check["atol"]))
            elif check.get("rtol") is not None or check.get("atol") is not None:
                ok = _close(actual, check["expected"], check.get("rtol") or 0.0, check.get("atol") or 0.0)
            else:
                ok = _close(actual, check["expected"], 0.0, 0.0)
            out.update(actual=_short(actual), passed=bool(ok))
            return out
        text = path.read_text(encoding="utf-8", errors="replace")
        found = check["expected"] in text
        out.update(actual=found, passed=found if kind == "text_contains" else not found)
        return out
    except ToolFailure as exc:
        out.update(actual=None, passed=False, message=f"{exc.code}: {exc.message}")
        return out
    except OSError as exc:
        out.update(actual=None, passed=False, message=f"{type(exc).__name__}: {exc.strerror or exc}")
        return out
    except Exception as exc:  # noqa: BLE001 - a malformed check is a failed check, never a runner crash
        out.update(actual=None, passed=False, message=f"{type(exc).__name__}: {exc}"[:300])
        return out


def run_checks(job_dir: Path, checks: list[dict[str, Any]], exit_code: int | None) -> list[dict[str, Any]]:
    return [run_check(Path(job_dir), c, exit_code) for c in checks]

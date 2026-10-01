"""Deprecated compatibility API for expected-result checks; new callers should use ``vkm_jobs``.

``validate`` / ``evaluate`` / ``evaluate_all`` remain available. Legacy text checks deliberately use regular
expressions (shared job checks use literal substrings); ``file_exists`` honours ``expected=False``; numeric
tolerances default to zero, ``json_value`` remains exact, and ``exit_code`` defaults to zero. JSON/numeric
evaluation delegates to the shared finite/type/pointer checks; finite numeric error fields remain available.
The historical ``pointer='/'`` whole-document alias is retained. No solver or licence is started here.
"""
from __future__ import annotations

import re
from fractions import Fraction
from pathlib import Path
from typing import Any

from vkm_jobs.checks import _MISSING, _finite_error, _metadata, _numeric_difference, json_pointer, run_check
from vkm_jobs.errors import ToolFailure
from vkm_jobs.roots import resolve_inside
from vkm_jobs.spec import CHECK_KINDS, validate_check

KINDS = frozenset(CHECK_KINDS)


def _normalised(check: dict[str, Any]) -> dict[str, Any]:
    """Validate with the shared contract while retaining intentional legacy defaults."""
    normalised = validate_check(check)
    if normalised["kind"] == "number_close":
        normalised["rtol"] = check.get("rtol") or 0.0
        normalised["atol"] = check.get("atol") or 0.0
    elif normalised["kind"] == "json_value":
        normalised["rtol"] = normalised["atol"] = None
    return normalised


def validate(check: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(check, dict) or check.get("kind") not in KINDS:
        raise ValueError(f"check kind must be one of {sorted(KINDS)}")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", str(check.get("name", ""))):
        raise ValueError("check name: 1-80 of letters, digits, '_', '.', ':', '-'")
    path = check.get("path")
    if check["kind"] != "exit_code":
        if not isinstance(path, str) or not path or path.startswith(("/", "\\")) or ".." in Path(path).parts \
                or re.match(r"^[A-Za-z]:", path):
            raise ValueError("check path must be relative to the job directory, without '..'")
    try:
        _normalised({"expected": 0, **check} if check["kind"] == "exit_code" else check)
    except ToolFailure as exc:
        raise ValueError(exc.message) from exc
    return check


def _pointer(doc: Any, pointer: str) -> Any:
    result = json_pointer(doc, "" if pointer == "/" else pointer)
    if result is _MISSING:
        raise KeyError(pointer)
    return result


def evaluate(check: dict[str, Any], job_dir: Path, exit_code: int | None = None) -> dict[str, Any]:
    out = {}
    try:
        out = _metadata(check, keep_none=True)
        raw = {"expected": 0, **check} if check.get("kind") == "exit_code" else check
        normalised = _normalised(raw)
        kind = normalised["kind"]
        if kind == "file_exists":
            actual = resolve_inside(job_dir, normalised["path"], what="check path").is_file()
            out.update(actual=actual, passed=actual is bool(check.get("expected", True)))
        elif kind in ("text_contains", "text_absent"):
            path = resolve_inside(job_dir, normalised["path"], what="check path")
            found = re.search(normalised["expected"], path.read_text(encoding="utf-8", errors="replace")) is not None
            out.update(actual=found, passed=found if kind == "text_contains" else not found)
        else:
            result = run_check(job_dir, normalised, exit_code, short_actual=False)
            out.update(actual=result["actual"], passed=result["passed"])
            if "message" in result:
                out["error"] = result["message"]
            if kind == "number_close" and isinstance(result["actual"], (int, float)) \
                    and not isinstance(result["actual"], bool):
                actual, expected = result["actual"], normalised["expected"]
                error = _numeric_difference(actual, expected)
                relative = error / abs(Fraction(expected)) if expected else None
                out["abs_error"] = _finite_error(error, integer=isinstance(actual, int) and isinstance(expected, int))
                out["rel_error"] = _finite_error(relative) if relative is not None else None
                if out["abs_error"] is None or relative is not None and out["rel_error"] is None:
                    out["diagnostic"] = "numeric error cannot be represented as a finite number"
    except Exception as exc:  # noqa: BLE001 - a check that cannot be evaluated fails
        out.update({"actual": None, "passed": False, "error": f"{type(exc).__name__}: {exc}"[:300]})
    return out


def evaluate_all(checks: list[dict[str, Any]], job_dir: Path, exit_code: int | None = None) -> list[dict[str, Any]]:
    return [evaluate(c, job_dir, exit_code) for c in checks]

"""Expected-result checks of a job (plan §2.3): ``{name, kind, path, pointer, expected, rtol, atol}``.

Kinds: ``file_exists``, ``json_value``, ``number_close``, ``text_contains``, ``text_absent``, ``exit_code``. Paths are
relative to the job directory and may not leave it. The shared job layer evaluates the same records; this module lets
the Ansys wrapper and the tests evaluate them without it.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

KINDS = frozenset({"file_exists", "json_value", "number_close", "text_contains", "text_absent", "exit_code"})


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
    return check


def _pointer(doc: Any, pointer: str) -> Any:
    if pointer in ("", "/"):
        return doc
    cur = doc
    for raw in pointer.lstrip("/").split("/"):
        key = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, list):
            cur = cur[int(key)]
        elif isinstance(cur, dict):
            cur = cur[key]
        else:
            raise KeyError(pointer)
    return cur


def evaluate(check: dict[str, Any], job_dir: Path, exit_code: int | None = None) -> dict[str, Any]:
    kind = check["kind"]
    out = {"name": check["name"], "kind": kind, "expected": check.get("expected"), "rtol": check.get("rtol"),
           "atol": check.get("atol"), "path": check.get("path"), "pointer": check.get("pointer")}
    try:
        if kind == "exit_code":
            actual = exit_code
            ok = actual == int(check.get("expected", 0))
        else:
            path = (job_dir / check["path"]).resolve()
            if not path.is_relative_to(job_dir.resolve()):
                raise ValueError("path outside the job directory")
            if kind == "file_exists":
                actual = path.is_file()
                ok = actual is bool(check.get("expected", True))
            elif kind in ("text_contains", "text_absent"):
                text = path.read_text(encoding="utf-8", errors="replace")
                found = re.search(check["expected"], text) is not None
                actual = found
                ok = found if kind == "text_contains" else not found
            else:
                doc = json.loads(path.read_text(encoding="utf-8"))
                actual = _pointer(doc, check.get("pointer", ""))
                if kind == "json_value":
                    ok = actual == check.get("expected")
                else:
                    exp = float(check["expected"])
                    act = float(actual)
                    rtol = float(check.get("rtol") or 0.0)
                    atol = float(check.get("atol") or 0.0)
                    ok = math.isfinite(act) and abs(act - exp) <= atol + rtol * abs(exp)
                    out["abs_error"] = abs(act - exp)
                    out["rel_error"] = abs(act - exp) / abs(exp) if exp else None
        out.update({"actual": actual, "passed": bool(ok)})
    except Exception as exc:  # noqa: BLE001 - a check that cannot be evaluated fails
        out.update({"actual": None, "passed": False, "error": f"{type(exc).__name__}: {exc}"[:300]})
    return out


def evaluate_all(checks: list[dict[str, Any]], job_dir: Path, exit_code: int | None = None) -> list[dict[str, Any]]:
    return [evaluate(c, job_dir, exit_code) for c in checks]

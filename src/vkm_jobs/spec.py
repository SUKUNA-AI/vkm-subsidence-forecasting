"""Job specification ``vkm.sim_job/1``, job status ``vkm.sim_job_status/1``, checks, parameters and timeouts.

A server builds a :class:`JobSpec` (what to run, how long, which checks, which science metadata) and the detached
runner executes it. ``job.json`` holds real machine paths (it never leaves ``VKM_SIM_ROOT``); everything the runner
publishes — ``status.json``, ``receipt.json`` — carries logical paths only.
"""
from __future__ import annotations

import json
import math
import os
import re
import secrets
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from vkm_jobs.errors import ToolFailure

JOB_SCHEMA = "vkm.sim_job/1"
STATUS_SCHEMA = "vkm.sim_job_status/1"
RECEIPT_SCHEMA = "vkm.sim_receipt/1"
APPS = ("MATLAB", "MAPDL", "MECH", "WB", "DPF", "OSL", "PY")
STATUSES = ("QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CHECK_FAILED", "TIMED_OUT", "CANCELLED",
            "LICENSE_UNAVAILABLE", "LOST")
ACTIVE = frozenset({"QUEUED", "RUNNING"})
TERMINAL = frozenset(STATUSES) - ACTIVE
JOB_ID_PATTERN = r"^(?:MATLAB|MAPDL|MECH|WB|DPF|OSL|PY)-\d{8}T\d{6}Z-[0-9a-f]{8}$"
JOB_ID = re.compile(JOB_ID_PATTERN)
CHECK_KINDS = ("file_exists", "json_value", "number_close", "text_contains", "text_absent", "exit_code")
EPISTEMIC_STATUSES = ("FACT", "DERIVATION", "INTERPOLATION", "MODEL_CHOICE", "ENGINEERING_ASSUMPTION", "ANALOGUE",
                      "UNKNOWN")
# kind -> (default, ceiling) in seconds (engineering-tools plan §2.2)
TIMEOUTS: dict[str, tuple[int, int]] = {
    "matlab": (1800, 24 * 3600),
    "mapdl": (7200, 48 * 3600),
    "ladder": (900, 2 * 3600),
    "dpf": (1800, 6 * 3600),
    "mech": (3600, 24 * 3600),
    "wb": (3600, 24 * 3600),
    "osl": (24 * 3600, 72 * 3600),
    "py": (3600, 24 * 3600),
    "discovery": (300, 3600),
}
QUEUE_TIMEOUT_S = 24 * 3600
DEFAULT_ERROR_PATTERNS = (r"(?i)^\s*\*{0,3}\s*error\b", r"(?i)\berror\s*:", r"(?i)^Error using\b", r"(?i)^Error in\b",
                          r"(?i)traceback \(most recent call last\)")
DEFAULT_WARNING_PATTERNS = (r"(?i)^\s*\*{0,3}\s*warning\b", r"(?i)\bwarning\s*:")
DEFAULT_LICENSE_PATTERNS = (r"(?i)licen[sc]e (?:checkout|manager) (?:failed|error)",
                            r"(?i)no licen[sc]e (?:is )?available", r"(?i)licen[sc]e (?:is )?not available",
                            r"(?i)flexnet licensing error", r"(?i)unable to (?:check out|obtain) (?:a )?licen[sc]e")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_job_id(app: str, now: datetime | None = None) -> str:
    if app not in APPS:
        raise ValueError(f"unknown app {app}")
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return f"{app}-{stamp}-{secrets.token_hex(4)}"


def check_job_id(job_id: str) -> str:
    if not isinstance(job_id, str) or not JOB_ID.match(job_id):
        raise ToolFailure("INVALID_ARGUMENT", "job_id has the form <APP>-YYYYMMDDTHHMMSSZ-xxxxxxxx "
                                              f"(APP ∈ {', '.join(APPS)})")
    return job_id


def clamp_timeout(kind: str, value: int | float | None) -> int:
    default, ceiling = TIMEOUTS.get(kind, TIMEOUTS["py"])
    if value is None:
        return default
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 1:
        raise ToolFailure("INVALID_ARGUMENT", "timeout_s must be a positive number of seconds")
    if value > ceiling:
        raise ToolFailure("INVALID_ARGUMENT", f"timeout_s {value:g} exceeds the ceiling {ceiling} s for {kind} jobs")
    return int(math.ceil(value))


# --------------------------------------------------------------------------------------------------- checks
def _has_nonfinite_numbers(value: Any) -> bool:
    """JSON numbers must be finite, including numbers nested inside expected/actual values."""
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, Mapping):
        return any(_has_nonfinite_numbers(k) or _has_nonfinite_numbers(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_has_nonfinite_numbers(v) for v in value)
    return False


def validate_check(raw: Mapping[str, Any], *, default_path: str | None = None) -> dict[str, Any]:
    """``Check = {name, kind, path, pointer, expected, rtol, atol}`` (plan §2.3) → normalised dict."""
    if not isinstance(raw, Mapping):
        raise ToolFailure("INVALID_ARGUMENT", "a check is an object {name, kind, path, pointer, expected, rtol, atol}")
    kind = raw.get("kind")
    if kind not in CHECK_KINDS:
        raise ToolFailure("INVALID_ARGUMENT", f"check kind must be one of {', '.join(CHECK_KINDS)}")
    name = str(raw.get("name") or kind)[:120]
    path = raw.get("path") or (default_path if kind in ("json_value", "number_close") else None)
    if kind != "exit_code" and not path:
        raise ToolFailure("INVALID_ARGUMENT", f"check '{name}' ({kind}) needs a path relative to the job directory")
    if path is not None:
        from vkm_jobs.roots import clean_relpath

        path = clean_relpath(str(path), what=f"check '{name}' path")
    pointer = raw.get("pointer")
    if kind in ("json_value", "number_close"):
        # Historical whole-document alias. An empty token elsewhere is retained (e.g. //value).
        pointer = "" if pointer in (None, "/") else str(pointer)
        if pointer and (not pointer.startswith("/") or re.search(r"~(?![01])", pointer)):
            raise ToolFailure("INVALID_ARGUMENT", f"check '{name}': pointer is a JSON Pointer such as /s or /a/0")
    expected = raw.get("expected")
    if _has_nonfinite_numbers(expected):
        raise ToolFailure("INVALID_ARGUMENT", f"check '{name}': expected numbers must be finite")
    if kind in ("text_contains", "text_absent") and not isinstance(expected, str):
        raise ToolFailure("INVALID_ARGUMENT", f"check '{name}': expected must be a string for {kind}")
    if kind == "number_close" and (not isinstance(expected, (int, float)) or isinstance(expected, bool)):
        raise ToolFailure("INVALID_ARGUMENT", f"check '{name}': expected must be a number for number_close")
    if kind == "exit_code" and (not isinstance(expected, int) or isinstance(expected, bool)):
        raise ToolFailure("INVALID_ARGUMENT", f"check '{name}': expected must be an integer exit code")
    out = {"name": name, "kind": kind, "path": path, "pointer": pointer, "expected": expected}
    for tol in ("rtol", "atol"):
        value = raw.get(tol)
        if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool)
                                  or _has_nonfinite_numbers(value) or value < 0):
            raise ToolFailure("INVALID_ARGUMENT", f"check '{name}': {tol} must be a finite non-negative number")
        out[tol] = value
    if kind == "number_close":
        out["rtol"] = 1e-9 if out["rtol"] is None else out["rtol"]
        out["atol"] = 0.0 if out["atol"] is None else out["atol"]
    return out


def validate_param(raw: Mapping[str, Any]) -> dict[str, Any]:
    """A numeric parameter of a typed tool: ``{name, value, unit?, status, source_ref, scope?}``.

    Numbers without an epistemic status are refused (plan §2.5): UNKNOWN stays UNKNOWN and needs no value."""
    if not isinstance(raw, Mapping):
        raise ToolFailure("INVALID_ARGUMENT", "a parameter is an object {name, value, unit, status, source_ref}")
    name = raw.get("name")
    status = raw.get("status")
    if not isinstance(name, str) or not re.match(r"^[A-Za-z][A-Za-z0-9_]{0,63}$", name):
        raise ToolFailure("INVALID_ARGUMENT", "parameter name must be an identifier (letters, digits, '_')")
    if status not in EPISTEMIC_STATUSES:
        raise ToolFailure("INVALID_ARGUMENT", f"parameter '{name}' needs status ∈ {', '.join(EPISTEMIC_STATUSES)}")
    value = raw.get("value")
    if status == "UNKNOWN":
        if value is not None:
            raise ToolFailure("INVALID_ARGUMENT", f"parameter '{name}' is UNKNOWN and cannot carry a value")
    elif not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise ToolFailure("INVALID_ARGUMENT", f"parameter '{name}' needs a finite numeric value")
    source_ref = raw.get("source_ref")
    if status != "UNKNOWN" and (not isinstance(source_ref, str) or len(source_ref.strip()) < 3):
        raise ToolFailure("INVALID_ARGUMENT", f"parameter '{name}' needs source_ref (a source locator or the "
                                              "rationale of the assumption)")
    out = {"name": name, "value": value, "status": status, "source_ref": source_ref,
           "unit": raw.get("unit"), "scope": raw.get("scope")}
    return {k: v for k, v in out.items() if v is not None or k in ("value", "source_ref")}


# --------------------------------------------------------------------------------------------------- the spec
@dataclass
class JobSpec:
    job_id: str
    app: str
    kind: str                                   # tool / job kind, e.g. matlab_run, mapdl_deck
    pool: str                                   # licence pool (vkm_jobs.pools)
    argv: list[str]                             # real argv (machine paths; job.json stays in VKM_SIM_ROOT)
    timeout_s: int
    cwd: str = "work"                           # relative to the job directory
    env: dict[str, str] = field(default_factory=dict)          # extra environment (receipt lists names only)
    queue_timeout_s: int = QUEUE_TIMEOUT_S
    label: str | None = None
    checks: list[dict[str, Any]] = field(default_factory=list)
    outputs: list[str] = field(default_factory=lambda: ["out/**"])      # globs hashed into the receipt
    hash_exclude: list[str] = field(default_factory=list)              # globs never hashed (solver scratch)
    cleanup_on_success: list[str] = field(default_factory=list)        # globs deleted after SUCCEEDED (recorded)
    inputs: list[dict[str, Any]] = field(default_factory=list)         # {path, bytes, sha256, source}
    app_info: dict[str, Any] = field(default_factory=dict)             # {name, version, build}
    git: dict[str, Any] = field(default_factory=dict)                  # {commit, dirty, scope}
    params: list[dict[str, Any]] = field(default_factory=list)
    model_choices: list[dict[str, Any]] = field(default_factory=list)
    success_exit_codes: list[int] = field(default_factory=lambda: [0])
    log_files: list[str] = field(default_factory=lambda: ["logs/stdout.log", "logs/stderr.log"])
    error_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_ERROR_PATTERNS))
    warning_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_WARNING_PATTERNS))
    license_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_LICENSE_PATTERNS))
    progress: dict[str, Any] | None = None     # {"module", "function", "args"}: hook called by the runner
    gate: dict[str, Any] | None = None         # {"pid_files": [glob below VKM_SIM_ROOT], "image": name}: wait while live
    tree_grace_s: float = 30.0                 # after the main process exits, wait this long for its children
    logical_roots: dict[str, str] = field(default_factory=dict)        # {"<MATLAB_ROOT>": real path, ...}
    meta: dict[str, Any] = field(default_factory=dict)                 # server data (tool arguments, notes)
    result_status: str = "MODEL_RESULT"
    created_at: str = field(default_factory=utc_now)
    schema: str = JOB_SCHEMA

    def validate(self) -> "JobSpec":
        check_job_id(self.job_id)
        if not self.job_id.startswith(self.app + "-"):
            raise ValueError("job_id prefix must equal app")
        if not self.argv or not all(isinstance(a, str) for a in self.argv):
            raise ValueError("argv must be a non-empty list of strings")
        if not re.match(r"^[a-z][a-z0-9_]{0,31}$", self.pool):
            raise ValueError("pool must be a lower-case identifier")
        if not self.timeout_s or self.timeout_s < 1:
            raise ValueError("timeout_s must be positive")
        return self

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "JobSpec":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})


# --------------------------------------------------------------------------------------------------- json files
def atomic_write_json(path: Path, obj: Any, *, attempts: int = 40) -> None:
    """Write JSON through a temporary file and ``os.replace`` (retried: Windows refuses to replace an open file)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode("utf-8")
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
    tmp.write_bytes(data)
    for attempt in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == attempts - 1:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.025 * (attempt + 1))


def read_json(path: Path, *, attempts: int = 20, default: Any = None) -> Any:
    """Read a JSON file written by :func:`atomic_write_json` (retries transient sharing violations)."""
    for attempt in range(attempts):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        except (PermissionError, json.JSONDecodeError):
            if attempt == attempts - 1:
                raise
            time.sleep(0.025 * (attempt + 1))
    return default

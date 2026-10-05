"""Bounded source-owned startup diagnostics for native model owners.

Only closed vocabularies, bounded integers and our own hashes are recorded:
stage, startup-proof fence step, outcome, allowlisted exception class, HTTP
status of the owned witness endpoint and the owned child's exit code. Exception
text, paths, argv, environment, credentials, native logs and model payloads are
never read into a projection. A primary failure is kept separate from secondary
cleanup failures, so cleanup cannot mask the cause.

The public projection is safe for container logs and PUBLIC receipts. The
private receipt adds bounded diagnosis detail (library basenames outside the
pre-spawn inventory, classified placement groups) and is written only into an
explicit operator-owned directory. Neither is a model, GPU or scientific proof.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

SCHEMA = "vkm-owner-startup-diagnostic/1"
PRIVATE_SCHEMA = "vkm-owner-startup-diagnostic-private/1"
KINDS = ("visual",)
# Fixed log markers; the first word of a diagnostic line is never free text.
MARKERS = ("visual_gpu_owner_startup_unavailable", "visual_gpu_owner_ready",
           "visual_owner_startup_unavailable", "visual_owner_ready")

STAGES = (
    "GPU_RECIPE", "IMPLEMENTATION_FENCE", "CUDA_PREFLIGHT",
    "RECIPE_IDENTITY", "WATCHES", "DEPENDENCY_INVENTORY", "CREDENTIAL_READ", "CLIENT_SETUP",
    "FILE_INVENTORY", "SPAWN", "NATIVE_LOAD_PROOF", "PROOF_IDENTITY", "BRIDGE_CHECK",
    "PLACEMENT_PROFILE", "LISTENER", "SERVING")
FENCE_STEPS = (
    "CHILD_PROCESS", "CHILD_EXECUTABLE", "LISTENER", "MAPPED_IMPLEMENTATION", "MAPPED_RESOURCES",
    "WITNESS_HTTP", "WITNESS_STATUS", "WITNESS_BODY", "WITNESS_SCHEMA", "WITNESS_BINDING",
    "PLACEMENT_PRESENT", "WITNESS_LIFETIME", "PROCESS_STABILITY", "FILE_LEASE", "CLIENT_FENCE", "UNSET")
OUTCOMES = ("IN_PROGRESS", "READY", "FAILED", "LOAD_PROOF_DEADLINE", "CHILD_EXITED_DURING_LOAD")
SECONDARY = ("OWNER_CLOSE", "BRIDGE_CLOSE", "CLIENT_CLOSE", "WATCH_CLOSE", "IMPLEMENTATION_FENCE_CLOSE",
             "PRIVATE_RECEIPT_WRITE")
# Most specific first: the first allowlisted name in the exception MRO is used.
EXCEPTION_CLASSES = (
    "ValidationError", "ConnectError", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout",
    "ReadError", "WriteError", "RemoteProtocolError", "LocalProtocolError", "DecodingError",
    "TimeoutException", "TransportError", "HTTPStatusError", "HTTPError", "JSONDecodeError",
    "UnicodeDecodeError", "UnicodeError", "FileNotFoundError", "PermissionError", "ProcessLookupError",
    "ChildProcessError", "IsADirectoryError", "NotADirectoryError", "FileExistsError", "InterruptedError",
    "BlockingIOError", "ConnectionRefusedError", "ConnectionResetError", "ConnectionAbortedError",
    "BrokenPipeError", "ConnectionError", "TimeoutError", "OSError", "ValueError", "TypeError", "KeyError",
    "IndexError", "AttributeError", "AssertionError", "NotImplementedError", "RecursionError",
    "RuntimeError", "MemoryError", "ModuleNotFoundError", "ImportError", "SystemExit", "KeyboardInterrupt")
OTHER = "OTHER"
INVALID = "INVALID"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_BASENAME = re.compile(r"^[A-Za-z0-9._+-]{1,128}$")
_GROUP = re.compile(r"^(other|output|blk\.(0|[1-9][0-9]{0,3}))$")
MAX_STATUS_KINDS = 16
MAX_SECONDARY = 8
MAX_UNEXPECTED = 16
MAX_GROUPS = 1100
MAX_COUNT = 10**9
PRIVATE_LIMIT = 256 * 1024


def classify(exc: BaseException | type | None) -> str | None:
    """Allowlisted class label only; the exception message is never read."""
    if exc is None:
        return None
    klass = exc if isinstance(exc, type) else type(exc)
    for item in getattr(klass, "__mro__", ()):
        name = getattr(item, "__name__", None)
        if name in EXCEPTION_CLASSES:
            return name
    return OTHER


def _closed(value, vocabulary):
    return value if value in vocabulary else INVALID


def _ms(seconds: float) -> int:
    return max(0, min(int(seconds * 1000), MAX_COUNT))


class StartupDiagnostics:
    """One bounded record per owner startup. Methods never raise."""

    def __init__(self, *, kind: str = "visual", identity: dict | None = None, clock=time.monotonic):
        self._lock = threading.Lock()
        self._clock = clock
        self._began = clock()
        self.kind = kind if kind in KINDS else INVALID
        self.diagnostic_id = secrets.token_hex(16)
        self.identity = {key: value for key, value in (identity or {}).items()
                         if key in {"recipe_sha256", "code_sha256"} and isinstance(value, str)
                         and _HEX64.fullmatch(value)}
        self.stage = None
        self.stages: list[list] = []
        self.outcome = "IN_PROGRESS"
        self.primary: dict | None = None
        self.fences: dict[tuple[str, str], list[int]] = {}
        self.last_fence: tuple[str, str] | None = None
        self.witness_status: dict[int, int] = {}
        self.child = {"spawned": False, "alive_at_terminal": None, "exit_code": None}
        self.secondary: list[list[str]] = []
        self.unexpected_mapped: list[str] = []
        self.placement_groups: dict[str, str] | None = None

    def _now(self) -> int:
        try:
            return _ms(self._clock() - self._began)
        except Exception:
            return 0

    def enter(self, stage: str) -> None:
        with self._lock:
            stage = _closed(stage, STAGES)
            self.stage = stage
            if len(self.stages) < len(STAGES) + 4:
                self.stages.append([stage, self._now()])

    def spawned(self) -> None:
        with self._lock:
            self.child["spawned"] = True

    def fence_failure(self, step: str | None, exc: BaseException | str | None, *, http_status=None) -> None:
        with self._lock:
            label = exc if isinstance(exc, str) and exc in EXCEPTION_CLASSES + (OTHER,) else classify(exc)
            key = (_closed(step or "UNSET", FENCE_STEPS), label or OTHER)
            now = self._now()
            entry = self.fences.get(key)
            if entry is None:
                self.fences[key] = [1, now, now]
            else:
                entry[0] = min(entry[0] + 1, MAX_COUNT)
                entry[2] = now
            self.last_fence = key
            if (type(http_status) is int and 100 <= http_status <= 599
                    and (http_status in self.witness_status or len(self.witness_status) < MAX_STATUS_KINDS)):
                self.witness_status[http_status] = min(self.witness_status.get(http_status, 0) + 1, MAX_COUNT)

    def unexpected_mapping(self, basenames) -> None:
        with self._lock:
            values = sorted({name if isinstance(name, str) and _BASENAME.fullmatch(name) else INVALID
                             for name in basenames})
            self.unexpected_mapped = values[:MAX_UNEXPECTED]

    def placement(self, groups) -> None:
        with self._lock:
            try:
                items = sorted(groups.items())[:MAX_GROUPS]
                self.placement_groups = {g if isinstance(g, str) and _GROUP.fullmatch(g) else INVALID:
                                         d if d in {"GPU", "HOST", "MIXED"} else INVALID for g, d in items}
            except Exception:
                self.placement_groups = {INVALID: INVALID}

    def child_exit(self, code) -> None:
        with self._lock:
            self.child["alive_at_terminal"] = False
            self.child["exit_code"] = code if type(code) is int and -64 <= code <= 255 else None
            if self.primary is None:
                self.outcome = "CHILD_EXITED_DURING_LOAD"

    def deadline(self, *, child_alive, exit_code=None) -> None:
        with self._lock:
            self.child["alive_at_terminal"] = child_alive if type(child_alive) is bool else None
            if type(exit_code) is int and -64 <= exit_code <= 255:
                self.child["exit_code"] = exit_code  # died within the last poll interval
            if self.primary is None:
                self.outcome = "LOAD_PROOF_DEADLINE"

    def fail(self, exc: BaseException) -> None:
        """Record the first primary failure only; later calls never replace it."""
        with self._lock:
            if self.primary is not None:
                return
            if self.outcome not in {"LOAD_PROOF_DEADLINE", "CHILD_EXITED_DURING_LOAD"}:
                self.outcome = "FAILED"
            step = self.last_fence[0] if self.stage == "NATIVE_LOAD_PROOF" and self.last_fence else None
            self.primary = {"stage": self.stage, "exception_class": classify(exc), "fence_step": step,
                            "at_ms": self._now()}

    def cleanup_failure(self, component: str, exc: BaseException) -> None:
        with self._lock:
            if len(self.secondary) < MAX_SECONDARY:
                self.secondary.append([_closed(component, SECONDARY), classify(exc) or OTHER])

    def ready(self) -> None:
        with self._lock:
            if self.primary is None:
                self.outcome = "READY"

    def public(self) -> dict:
        with self._lock:
            primary = self.primary or {}
            stage = primary.get("stage", self.stage)
            fence_step = primary.get("fence_step")
            exc_class = primary.get("exception_class")
            cause = "/".join(str(part) if part is not None else "-"
                             for part in (stage, self.outcome, fence_step, exc_class))
            value = {
                "schema_version": SCHEMA, "kind": self.kind, "diagnostic_id": self.diagnostic_id,
                "identity": dict(sorted(self.identity.items())),
                "terminal_stage": stage, "outcome": self.outcome, "fence_step": fence_step,
                "exception_class": exc_class, "cause_code": cause, "elapsed_ms": self._now(),
                "stages": [list(item) for item in self.stages],
                "fence_failures": [[step, label, *counts] for (step, label), counts in sorted(self.fences.items())],
                "witness_http_status": [[status, count] for status, count in sorted(self.witness_status.items())],
                "child": dict(self.child),
                "secondary": [list(item) for item in self.secondary],
                "unexpected_mapped_count": len(self.unexpected_mapped),
                "payload": "NOT_RECORDED",
            }
        return value if validate_public(value) else _invalid_projection(self.diagnostic_id)

    def private(self) -> dict:
        public = self.public()
        with self._lock:
            return {"schema_version": PRIVATE_SCHEMA, "public": public,
                    "unexpected_mapped_basenames": list(self.unexpected_mapped),
                    "placement_groups": dict(self.placement_groups) if self.placement_groups is not None else None}

    def line(self, marker: str) -> str:
        marker = marker if marker in MARKERS else "owner_startup_diagnostic"
        return marker + " " + json.dumps(self.public(), sort_keys=True, separators=(",", ":"),
                                         ensure_ascii=True)

    def write_private(self, directory) -> str | None:
        """Exclusive new 0600 file in an explicit absolute directory; never raises."""
        if directory is None:
            return None
        try:
            root = Path(directory)
            if (not root.is_absolute() or root.is_symlink() or not root.is_dir()
                    or any(parent.is_symlink() for parent in root.parents)):
                raise ValueError("explicit direct diagnostics directory required")
            raw = (json.dumps(self.private(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
                   + "\n").encode("ascii")
            if len(raw) > PRIVATE_LIMIT:
                raise ValueError("private diagnostic exceeds bound")
            path = root / ("owner-startup-diagnostic-" + self.diagnostic_id + ".json")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                view = memoryview(raw)
                while view:
                    view = view[os.write(fd, view):]
                os.fsync(fd)
            finally:
                os.close(fd)
            return path.name
        except Exception as exc:
            self.cleanup_failure("PRIVATE_RECEIPT_WRITE", exc)
            return None


class NullDiagnostics:
    """Default for callers without a diagnostic owner; identical control flow."""

    def enter(self, stage): pass
    def spawned(self): pass
    def fence_failure(self, step, exc, *, http_status=None): pass
    def unexpected_mapping(self, basenames): pass
    def placement(self, groups): pass
    def child_exit(self, code): pass
    def deadline(self, *, child_alive, exit_code=None): pass
    def fail(self, exc): pass
    def cleanup_failure(self, component, exc): pass
    def ready(self): pass


NULL = NullDiagnostics()


def _invalid_projection(diagnostic_id):
    return {"schema_version": SCHEMA, "kind": INVALID,
            "diagnostic_id": diagnostic_id if _HEX32.fullmatch(str(diagnostic_id)) else "0" * 32,
            "identity": {}, "terminal_stage": None, "outcome": "FAILED", "fence_step": None,
            "exception_class": None, "cause_code": "DIAGNOSTIC_PROJECTION_INVALID", "elapsed_ms": 0,
            "stages": [], "fence_failures": [], "witness_http_status": [],
            "child": {"spawned": False, "alive_at_terminal": None, "exit_code": None},
            "secondary": [], "unexpected_mapped_count": 0, "payload": "NOT_RECORDED"}


_KEYS = {"schema_version", "kind", "diagnostic_id", "identity", "terminal_stage", "outcome", "fence_step",
         "exception_class", "cause_code", "elapsed_ms", "stages", "fence_failures", "witness_http_status",
         "child", "secondary", "unexpected_mapped_count", "payload"}
_LABELS = set(EXCEPTION_CLASSES) | {OTHER, INVALID}


def _int(value, low=0, high=MAX_COUNT):
    return type(value) is int and low <= value <= high


def validate_public(value) -> bool:
    """Strict closed-schema check used before any projection leaves the process."""
    try:
        if not isinstance(value, dict) or set(value) != _KEYS or value["schema_version"] != SCHEMA:
            return False
        if value["kind"] not in (*KINDS, INVALID) or not _HEX32.fullmatch(value["diagnostic_id"]):
            return False
        identity = value["identity"]
        if (not isinstance(identity, dict) or not set(identity) <= {"recipe_sha256", "code_sha256"}
                or any(not isinstance(v, str) or not _HEX64.fullmatch(v) for v in identity.values())):
            return False
        if value["terminal_stage"] not in (*STAGES, INVALID, None) or value["outcome"] not in OUTCOMES:
            return False
        if value["fence_step"] not in (*FENCE_STEPS, INVALID, None):
            return False
        if value["exception_class"] not in _LABELS | {None}:
            return False
        parts = value["cause_code"].split("/")
        if value["cause_code"] != "DIAGNOSTIC_PROJECTION_INVALID" and (
                len(parts) != 4 or parts[0] not in {*STAGES, INVALID, "-"} or parts[1] not in OUTCOMES
                or parts[2] not in {*FENCE_STEPS, INVALID, "-"} or parts[3] not in _LABELS | {"-"}):
            return False
        if not _int(value["elapsed_ms"]):
            return False
        stages = value["stages"]
        if (not isinstance(stages, list) or len(stages) > len(STAGES) + 4
                or any(not isinstance(s, list) or len(s) != 2 or s[0] not in (*STAGES, INVALID) or not _int(s[1])
                       for s in stages)):
            return False
        fences = value["fence_failures"]
        if (not isinstance(fences, list) or len(fences) > len(FENCE_STEPS) * len(_LABELS)
                or any(not isinstance(f, list) or len(f) != 5 or f[0] not in (*FENCE_STEPS, INVALID)
                       or f[1] not in _LABELS or not all(_int(x) for x in f[2:]) for f in fences)):
            return False
        statuses = value["witness_http_status"]
        if (not isinstance(statuses, list) or len(statuses) > MAX_STATUS_KINDS
                or any(not isinstance(s, list) or len(s) != 2 or not _int(s[0], 100, 599) or not _int(s[1])
                       for s in statuses)):
            return False
        child = value["child"]
        if (not isinstance(child, dict) or set(child) != {"spawned", "alive_at_terminal", "exit_code"}
                or type(child["spawned"]) is not bool or child["alive_at_terminal"] not in (True, False, None)
                or not (child["exit_code"] is None or _int(child["exit_code"], -64, 255))):
            return False
        secondary = value["secondary"]
        if (not isinstance(secondary, list) or len(secondary) > MAX_SECONDARY
                or any(not isinstance(s, list) or len(s) != 2 or s[0] not in (*SECONDARY, INVALID)
                       or s[1] not in _LABELS for s in secondary)):
            return False
        return _int(value["unexpected_mapped_count"], 0, MAX_UNEXPECTED) and value["payload"] == "NOT_RECORDED"
    except Exception:
        return False

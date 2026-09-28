"""Shared helpers of the vkm-ansys product modules: Mechanical, Workbench, optiSLang (agent ANS2).

* :class:`ProductJob` — a job request built by a product tool: the command (with placeholders for the job directories
  and the job interpreter), files staged into ``in/`` (entry scripts, requests, user scripts — sha256 in the receipt),
  inputs copied from user paths or from earlier jobs (``job:<job_id>/<path>``), checks, output globs and the science
  metadata of the receipt (``params`` with status, ``model_choices``; results are MODEL_RESULT);
* :func:`submit` — hands a :class:`ProductJob` to the shared job layer ``vkm_jobs`` (pool ``ansys``: one licensed
  Ansys process at a time, shared with MAPDL and DPF jobs of the core);
* read-only discovery: installation build lines, product processes with listening addresses, and the licence features
  the licence server reports (``lmutil lmstat -a``: feature names and seat counts only — never user names, host names
  or the licence string; nothing is checked out).

Product entry scripts (``*/entry_*.py``, ``workbench/wrapper.wbjn``) are standalone files without vkm imports: a job
keeps a byte-exact snapshot of the code that ran.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

APPS = ("MECH", "WB", "OSL")
JOB_ID = r"^[A-Z]+-\d{8}T\d{6}Z-[0-9a-f]{8}$"
_JOB_ID = re.compile(JOB_ID)
# placeholders of ProductJob.argv / env values, filled by submit() once the job directory exists
PLACEHOLDERS = ("{JOB_DIR}", "{IN}", "{WORK}", "{OUT}", "{LOGS}", "{PYTHON}", "{ANSYS_ROOT}")
# marker in staged text files, replaced by the absolute job directory (ASCII: VKM_SIM_ROOT is ASCII by contract)
JOB_DIR_MARKER = "@@VKM_JOB_DIR@@"
CHECK_KINDS = ("file_exists", "json_value", "number_close", "text_contains", "text_absent", "exit_code")
STATUSES = ("FACT", "DERIVATION", "INTERPOLATION", "MODEL_CHOICE", "ENGINEERING_ASSUMPTION", "ANALOGUE", "UNKNOWN")
MAX_SCRIPT_BYTES = 1_000_000
ERROR_CODES = frozenset({"INVALID_ARGUMENT", "NOT_FOUND", "APP_UNAVAILABLE", "LICENSE_UNAVAILABLE", "POOL_BUSY",
                         "TIMEOUT", "PATH_OUTSIDE_ROOT", "WOULD_OVERWRITE", "GATE_CLOSED", "INTERNAL",
                         "SIM_ROOT_UNAVAILABLE", "JOB_LAYER_UNAVAILABLE", "JOB_NOT_FINISHED", "RESULT_NOT_FOUND",
                         "PAYLOAD_TOO_LARGE"})


class ProductFailure(Exception):
    """Machine-readable failure (``code`` + ``as_dict()``, the protocol of ``AnsysContext.call``)."""

    def __init__(self, code: str, message: str, *, retryable: bool = False, **details: Any):
        if code not in ERROR_CODES:
            raise ValueError(f"unknown error code {code}")
        super().__init__(message)
        self.code, self.message, self.retryable, self.details = code, message, retryable, details

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable, "details": self.details}


def fail(code: str, message: str, **details: Any) -> ProductFailure:
    return ProductFailure(code, message, **details)


# ---------------------------------------------------------------------------------------------------- job request
@dataclass
class ProductJob:
    """What a product tool asks the job layer to run (one licensed process tree, pool ``ansys``)."""

    app: str                                   # MECH | WB | OSL
    kind: str                                  # e.g. mechanical.script, workbench.journal, optislang.run
    argv: list[str]                            # command; tokens may contain PLACEHOLDERS
    staged: dict[str, bytes] = field(default_factory=dict)       # name under in/ -> bytes (JOB_DIR_MARKER allowed)
    inputs: list[tuple[Path, str]] = field(default_factory=list)  # (source file or directory, name under in/)
    env: dict[str, str] = field(default_factory=dict)            # extra environment (names go to the receipt)
    timeout_s: int = 3600
    checks: list[dict[str, Any]] = field(default_factory=list)
    outputs: list[str] = field(default_factory=lambda: ["out/**"])  # globs relative to the job directory
    label: str | None = None
    params: list[dict[str, Any]] = field(default_factory=list)
    model_choices: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    pool: str = "ansys"
    uses_license: bool = True
    cwd: str = "work"

    def __post_init__(self) -> None:
        if self.app not in APPS:
            raise ValueError(f"unknown app {self.app}")
        for name in [*self.staged, *(n for _p, n in self.inputs)]:
            safe_relative(name, what="staged file name")

    def rendered(self, job_dir: Path) -> dict[str, bytes]:
        """Staged files with the job-directory marker filled in (only text files carry the marker)."""
        marker = JOB_DIR_MARKER.encode()
        text = str(job_dir).encode("ascii", errors="strict") if str(job_dir).isascii() else None
        out: dict[str, bytes] = {}
        for name, data in self.staged.items():
            if marker in data:
                if text is None:
                    raise fail("SIM_ROOT_UNAVAILABLE", "the job directory must be an ASCII path")
                data = data.replace(marker, text.replace(b"\\", b"/"))
            out[name] = data
        return out

    def expand(self, job_dir: Path, python: str, ansys_root: str | None) -> tuple[list[str], dict[str, str]]:
        values = {"{JOB_DIR}": str(job_dir), "{IN}": str(job_dir / "in"), "{WORK}": str(job_dir / "work"),
                  "{OUT}": str(job_dir / "out"), "{LOGS}": str(job_dir / "logs"), "{PYTHON}": python,
                  "{ANSYS_ROOT}": ansys_root or ""}

        def sub(token: str) -> str:
            for key, value in values.items():
                token = token.replace(key, value)
            return token
        return [sub(t) for t in self.argv], {k: sub(v) for k, v in self.env.items()}

    def describe(self) -> dict[str, Any]:
        """Dry-run view (no paths of the machine): what would be staged and run."""
        return {"app": self.app, "kind": self.kind, "argv": list(self.argv), "pool": self.pool,
                "timeout_s": self.timeout_s, "uses_license": self.uses_license,
                "staged": {n: {"bytes": len(b), "sha256": hashlib.sha256(b).hexdigest()} for n, b in
                           sorted(self.staged.items())},
                "inputs": [n for _p, n in self.inputs], "env": sorted(self.env), "checks": self.checks,
                "outputs": self.outputs, "label": self.label, "params": self.params,
                "model_choices": self.model_choices, "meta": self.meta}


def safe_relative(name: str, *, what: str = "path") -> str:
    """A relative POSIX path without '..', drive, UNC or absolute parts."""
    if not name or len(name) > 240 or "\\" in name or ":" in name or name.startswith("/") or "\x00" in name:
        raise fail("PATH_OUTSIDE_ROOT", f"{what} must be a relative POSIX path: {name!r}")
    if any(part in ("", ".", "..") for part in name.split("/")):
        raise fail("PATH_OUTSIDE_ROOT", f"{what} may not contain empty, '.' or '..' parts: {name!r}")
    return name


def job_dir_of(sim_root: Path, job_id: str) -> Path:
    if not _JOB_ID.match(job_id or ""):
        raise fail("INVALID_ARGUMENT", f"not a job id: {job_id!r}")
    return sim_root / "jobs" / job_id


def resolve_input(ref: str, sim_root: Path | None, *, kinds: tuple[str, ...] = ("file", "dir")) -> Path:
    """``job:<job_id>/<relative path>`` (inside that job directory) or an absolute path to an existing file/dir."""
    if ref.startswith("job:"):
        if sim_root is None:
            raise fail("SIM_ROOT_UNAVAILABLE", "job: references need VKM_SIM_ROOT")
        body = ref[4:]
        job_id, _, rel = body.partition("/")
        base = job_dir_of(sim_root, job_id)
        path = (base / safe_relative(rel, what="job reference path")).resolve()
        if not path.is_relative_to(base.resolve()):
            raise fail("PATH_OUTSIDE_ROOT", "job reference escapes the job directory", ref=ref)
    else:
        path = Path(ref)
        if not path.is_absolute():
            raise fail("INVALID_ARGUMENT", "inputs are absolute paths or job:<job_id>/<path> references", ref=ref)
    if not path.exists():
        raise fail("NOT_FOUND", "input not found", ref=ref if ref.startswith("job:") else path.name)
    if path.is_file() and "file" not in kinds or path.is_dir() and "dir" not in kinds:
        raise fail("INVALID_ARGUMENT", f"input must be one of {kinds}", ref=path.name)
    return path


def companion_dir(project_file: Path) -> Path | None:
    """The data folder next to a project file (``x.mechdb`` → ``x_Mech_Files``, ``x.wbpj`` → ``x_files``)."""
    if project_file.suffix.lower() == ".mechdb":
        cand = project_file.with_name(project_file.stem + "_Mech_Files")
    elif project_file.suffix.lower() == ".wbpj":
        cand = project_file.with_name(project_file.stem + "_files")
    elif project_file.suffix.lower() == ".opf":
        cand = project_file.with_name(project_file.stem + ".opd")
    else:
        return None
    return cand if cand.is_dir() else None


def script_bytes(text: str | None, path: str | None, sim_root: Path | None, *, what: str = "script") -> tuple[bytes, str]:
    """Exactly one of inline text or a file reference → (bytes, origin label without machine paths)."""
    if (text is None) == (path is None):
        raise fail("INVALID_ARGUMENT", f"give exactly one of the {what} text or the {what} file")
    if text is not None:
        data = text.encode("utf-8")
        origin = "inline"
    else:
        src = resolve_input(path, sim_root, kinds=("file",))
        data = src.read_bytes()
        origin = path if path.startswith("job:") else f"file:{src.name}"
    if len(data) > MAX_SCRIPT_BYTES:
        raise fail("PAYLOAD_TOO_LARGE", f"{what} larger than {MAX_SCRIPT_BYTES} bytes")
    return data, origin


def validate_checks(checks: Iterable[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """Plan §2.3 ``Check = {name, kind, path, pointer, expected, rtol, atol}``."""
    out: list[dict[str, Any]] = []
    for c in checks or []:
        c = dict(c)
        if c.get("kind") not in CHECK_KINDS:
            raise fail("INVALID_ARGUMENT", f"check kind must be one of {CHECK_KINDS}", check=c.get("name"))
        if not isinstance(c.get("name"), str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", c["name"]):
            raise fail("INVALID_ARGUMENT", "check name: 1-80 chars [A-Za-z0-9_.:-]")
        if c["kind"] != "exit_code":
            safe_relative(str(c.get("path", "")), what="check path")
        out.append(c)
    return out


def validate_params(params: Iterable[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """Receipt ``params[]``: numbers carry an epistemic status and a source reference (plan §2.5, D-19)."""
    out = []
    for p in params or []:
        p = dict(p)
        if not isinstance(p.get("name"), str) or not p["name"]:
            raise fail("INVALID_ARGUMENT", "param needs a name")
        if p.get("status") not in STATUSES:
            raise fail("INVALID_ARGUMENT", f"param {p['name']}: status must be one of {STATUSES}")
        if not p.get("source_ref"):
            raise fail("INVALID_ARGUMENT", f"param {p['name']}: source_ref is required (e.g. 'TOY' for tool tests)")
        out.append(p)
    return out


# ---------------------------------------------------------------------------------------------------- submission
def job_python(env: Mapping[str, str]) -> str:
    """Interpreter of the job entries: ``VKM_ANSYS_PYTHON`` or this interpreter (the server runs in venv-ansys)."""
    value = (env.get("VKM_ANSYS_PYTHON") or "").strip()
    return value if value and "${" not in value else sys.executable


def submit(ctx: Any, job: ProductJob, wait_s: int = 0) -> dict[str, Any]:
    """Create the job through the shared job layer and return its ``JobRef`` (plan §2.2)."""
    from vkm_ansys import product_submit

    return product_submit.submit(ctx, job, wait_s)


# ---------------------------------------------------------------------------------------------------- discovery
def read_build_lines(ansys_root: Path) -> str:
    try:
        return (ansys_root / "builddate.txt").read_bytes()[:400_000].decode("utf-8", errors="replace")
    except OSError:
        return ""


def build_section(text: str, product: str) -> list[str]:
    """Lines of one product block of ``builddate.txt`` (blocks start with the product name on its own line)."""
    lines = text.splitlines()
    out: list[str] = []
    inside = False
    for line in lines:
        if line.strip() == product:
            inside = True
            continue
        if inside:
            if not line.strip():
                break
            out.append(line.strip().lstrip("﻿ï»¿"))
    return out[:12]


def product_processes(names: Iterable[str]) -> dict[str, Any]:
    """Running processes of the given image names (lower case) with listening TCP ports and a loopback flag."""
    wanted = {n.lower() for n in names}
    try:
        import psutil
    except ImportError:
        return {"available": False, "reason": "psutil not importable"}
    found: dict[int, str] = {}
    for proc in psutil.process_iter(["pid", "name"]):
        name = (proc.info.get("name") or "").lower()
        if name in wanted:
            found[proc.info["pid"]] = proc.info["name"]
    listening: dict[int, list[dict[str, Any]]] = {}
    if found:
        try:
            for conn in psutil.net_connections(kind="tcp"):
                if conn.status == psutil.CONN_LISTEN and conn.pid in found and conn.laddr:
                    listening.setdefault(conn.pid, []).append({"port": conn.laddr.port,
                                                               "loopback": is_loopback(conn.laddr.ip)})
        except (psutil.AccessDenied, OSError):
            pass
    return {"available": True,
            "running": [{"name": n, "pid": p, "listening": listening.get(p, [])} for p, n in sorted(found.items())]}


def is_loopback(host: str) -> bool:
    import ipaddress

    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]").split("%")[0]).is_loopback
    except ValueError:
        return False


_USERS_OF = re.compile(r"^Users of ([A-Za-z0-9_.\-]+):\s*\(Total of (\d+) licenses? issued;\s*Total of (\d+) "
                       r"licenses? in use\)")
_LICENCE_CACHE: dict[str, tuple[float, dict[str, tuple[int, int]]]] = {}


def parse_lmstat(text: str) -> dict[str, tuple[int, int]]:
    """``Users of <feature>: (Total of N licenses issued; Total of M licenses in use)`` → {feature: (N, M)}.

    Every other line (user names, hosts, displays, server lines) is ignored."""
    out: dict[str, tuple[int, int]] = {}
    for line in text.splitlines():
        m = _USERS_OF.match(line.strip())
        if m:
            out[m.group(1)] = (int(m.group(2)), int(m.group(3)))
    return out


def licence_features(env: Mapping[str, str], ansys_root: Path | None, patterns: Mapping[str, str], *,
                     timeout_s: float = 60.0, max_age_s: float = 300.0) -> dict[str, Any]:
    """Read-only licence query: features whose names match a product pattern, with issued / in-use seat counts.

    ``lmutil lmstat -a -c $ANSYSLMD_LICENSE_FILE`` — a status query of the licence server: nothing is checked out and
    no setting changes. The licence string, host and user names are not returned."""
    import time

    source = (env.get("ANSYSLMD_LICENSE_FILE") or "").strip()
    if not source or "${" in source:
        return {"queried": False, "reason": "ANSYSLMD_LICENSE_FILE not set"}
    if ansys_root is None:
        return {"queried": False, "reason": "Ansys root not found"}
    lmutil = ansys_root / "licensingclient" / "winx64" / "lmutil.exe"
    if not lmutil.is_file():
        return {"queried": False, "reason": "lmutil not found under <ANSYS_ROOT>/licensingclient/winx64"}
    now = time.time()
    cached = _LICENCE_CACHE.get(source)
    if cached and now - cached[0] < max_age_s:
        features = cached[1]
    else:
        try:
            proc = subprocess.run([str(lmutil), "lmstat", "-a", "-c", source], capture_output=True, text=True,
                                  timeout=timeout_s, errors="replace",
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"queried": False, "reason": f"lmstat failed: {type(exc).__name__}"}
        features = parse_lmstat(proc.stdout)
        if not features:
            return {"queried": True, "features_total": 0, "reason": "no feature lines in the lmstat output"}
        _LICENCE_CACHE[source] = (now, features)
    by_product: dict[str, list[dict[str, Any]]] = {}
    for product, pattern in patterns.items():
        rx = re.compile(pattern, re.IGNORECASE)
        by_product[product] = [{"feature": f, "issued": n, "in_use": m} for f, (n, m) in sorted(features.items())
                               if rx.search(f)]
    return {"queried": True, "method": "lmutil lmstat -a (read-only)", "features_total": len(features),
            "matched_by_name_pattern": by_product,
            "note": "product attribution is a name-pattern heuristic; which feature a run takes shows in its receipt"}


def pkg_version(name: str) -> str | None:
    import importlib.metadata as md

    try:
        return md.version(name)
    except md.PackageNotFoundError:
        return None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def json_bytes(obj: Any) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def progress(job_dir: Path, **_: Any) -> dict[str, Any]:
    """Progress hook of the job runner: the phase an entry reported in ``work/vkm_progress.json``."""
    try:
        data = json.loads((Path(job_dir) / "work" / "vkm_progress.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if k in ("phase", "since", "mode", "helper", "node", "step")
            and isinstance(v, (str, int, float))}


def env_flag(env: Mapping[str, str], name: str) -> bool:
    return (env.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def windows() -> bool:
    return os.name == "nt"

"""Receipt ``vkm.sim_receipt/1`` of a job (plan §2.3): what ran and with what, how it went, inputs and outputs with
SHA-256, checks, and the science metadata (``params``, ``model_choices``, ``result_status = MODEL_RESULT``,
``review_status = AUTO_UNREVIEWED``). All paths are logical (``<VKM_SIM_ROOT>``, ``<MATLAB_ROOT>``, ``<ANSYS_ROOT>``,
``<PUBLIC>``); environment variables are listed by name only; no user, host or licence strings.
"""
from __future__ import annotations

import fnmatch
import hashlib
import platform
import re
import sys
from pathlib import Path
from typing import Any, Iterable

from vkm_jobs import __version__
from vkm_jobs.redact import Redactor
from vkm_jobs.spec import RECEIPT_SCHEMA, JobSpec

HASH_CHUNK = 1024 * 1024
MAX_LOG_SCAN_BYTES = 256 * 1024 * 1024
MAX_FILES = 20000
INTERNAL_FILES = {"job.json", "status.json", "receipt.json", "runner.lock", "cancel.request"}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(HASH_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _match(rel: str, globs: Iterable[str]) -> bool:
    for g in globs:
        if fnmatch.fnmatchcase(rel, g) or (g.endswith("/**") and (rel + "/").startswith(g[:-2])):
            return True
    return False


def job_files(job_dir: Path, globs: Iterable[str]) -> list[str]:
    """Relative POSIX paths of regular files below ``job_dir`` matching any glob (``**`` crosses directories)."""
    globs = list(globs)
    out = []
    for path in sorted(Path(job_dir).rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(job_dir).as_posix()
        if rel in INTERNAL_FILES or rel.startswith(".") or "/." in rel:
            continue
        if _match(rel, globs):
            out.append(rel)
            if len(out) >= MAX_FILES:
                break
    return out


def hash_files(job_dir: Path, rels: Iterable[str]) -> list[dict[str, Any]]:
    out = []
    for rel in rels:
        path = Path(job_dir) / rel
        try:
            out.append({"path": rel, "bytes": path.stat().st_size, "sha256": sha256_file(path)})
        except OSError as exc:
            out.append({"path": rel, "error": type(exc).__name__})
    return out


def log_summary(job_dir: Path, spec: JobSpec, redactor: Redactor) -> dict[str, Any]:
    """Counts of error and warning lines in the job's logs, the first error, and licence-failure evidence."""
    errors = [re.compile(p) for p in spec.error_patterns]
    warnings = [re.compile(p) for p in spec.warning_patterns]
    licence = [re.compile(p) for p in spec.license_patterns]
    n_err = n_warn = 0
    first_error = first_license = None
    scanned = []
    for rel in spec.log_files:
        path = Path(job_dir) / rel
        if not path.is_file():
            continue
        scanned.append(rel)
        with open(path, "rb") as fh:
            data = fh.read(MAX_LOG_SCAN_BYTES)
        for line in data.decode("utf-8", errors="replace").splitlines():
            if any(p.search(line) for p in errors):
                n_err += 1
                first_error = first_error or redactor.text(line.strip())[:500]
            elif any(p.search(line) for p in warnings):
                n_warn += 1
            if first_license is None and any(p.search(line) for p in licence):
                first_license = redactor.text(line.strip())[:300]
    return {"files": scanned, "errors": n_err, "warnings": n_warn, "first_error": first_error,
            "license_failure": first_license}


def build_receipt(spec: JobSpec, status: dict[str, Any], *, job_dir: Path, redactor: Redactor,
                  outputs: list[dict[str, Any]], checks: list[dict[str, Any]], logs: dict[str, Any],
                  runner: dict[str, Any], tree: dict[str, Any], scratch: dict[str, Any]) -> dict[str, Any]:
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "job_id": spec.job_id,
        "label": spec.label,
        "app": spec.app_info or {"name": spec.app},
        "kind": spec.kind,
        "pool": spec.pool,
        "command": [redactor.text(a) for a in spec.argv],
        "cwd": spec.cwd,
        "env_names": sorted(spec.env),
        "git_commit": spec.git.get("commit"),
        "git_dirty": spec.git.get("dirty"),
        "git_scope": spec.git.get("scope"),
        "queued_at": status.get("queued_at"),
        "started_at": status.get("started_at"),
        "ended_at": status.get("ended_at"),
        "queue_wait_s": status.get("queue_wait_s"),
        "duration_s": status.get("duration_s"),
        "timeout_s": spec.timeout_s,
        "exit_code": status.get("exit_code"),
        "status": status.get("status"),
        "status_reason": status.get("reason"),
        "log_summary": logs,
        "inputs": spec.inputs,
        "outputs": outputs,
        "scratch": scratch,
        "checks": checks,
        "checks_passed": all(c.get("passed") for c in checks) if checks else None,
        "params": spec.params,
        "model_choices": spec.model_choices,
        "result_status": spec.result_status,
        "review_status": "AUTO_UNREVIEWED",
        "runner": {"vkm_jobs": __version__, "python": platform.python_version(), "platform": sys.platform,
                   **runner},
        "process_tree": tree,
        "job_dir": f"<VKM_SIM_ROOT>/jobs/{spec.job_id}",
        "meta": spec.meta,
    }
    return redactor.obj(receipt)

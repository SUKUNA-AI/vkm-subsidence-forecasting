"""Submission of product jobs (Mechanical, Workbench, optiSLang) to the shared job layer ``vkm_jobs``.

``JobsService.draft(app)`` → staged files and inputs (SHA-256 recorded) → ``draft.spec(...)`` → ``submit`` (detached
runner, pool ``ansys``). Inside the job the product entry additionally takes the machine-wide licence lock of
:mod:`vkm_ansys.licence_lock` before it starts the product, exactly like the MAPDL wrapper of the core: one licensed
Ansys process at a time across all servers, sessions and tests of the machine.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from vkm_ansys import product_jobs as pj

LOG_FILES = ["logs/stdout.log", "logs/stderr.log", "logs/product.log"]
EXTRA_LICENSE_PATTERNS = [r"VKM_STATUS=LICENSE_UNAVAILABLE"]
_SERVICES: dict[int, Any] = {}


def jobs_service(ctx: Any) -> Any:
    """The core's shared ``JobsService`` when the context offers one, else one built from the context's environment
    (logical roots ``<VKM_SIM_ROOT>``, ``<PUBLIC>``, ``<ANSYS_ROOT>``)."""
    shared = getattr(ctx, "jobs_service", None)
    if callable(shared):
        return shared()
    key = id(ctx)
    if key not in _SERVICES:
        ctx.jobs                                   # raises JOB_LAYER_UNAVAILABLE when vkm_jobs is missing
        from vkm_jobs.service import JobsService

        roots: dict[str, str] = {}
        try:
            roots["<ANSYS_ROOT>"] = str(ctx.ansys_root()[0])
        except Exception:  # noqa: BLE001 - redaction then covers the other roots only
            pass
        _SERVICES[key] = JobsService.from_env(ctx.env, logical_roots=roots)
    return _SERVICES[key]


def build_spec_fields(ctx: Any, job: pj.ProductJob, job_dir: Path) -> dict[str, Any]:
    root = None
    try:
        root = str(ctx.ansys_root()[0])
    except Exception:  # noqa: BLE001
        pass
    argv, env = job.expand(job_dir, pj.job_python(ctx.env), root)
    from vkm_jobs.service import git_info
    from vkm_jobs.spec import DEFAULT_LICENSE_PATTERNS
    from vkm_jobs.roots import repo_root

    return {
        "kind": job.kind, "pool": job.pool, "argv": argv, "timeout_s": int(job.timeout_s), "cwd": job.cwd,
        "env": env, "label": job.label, "checks": job.checks, "outputs": job.outputs, "params": job.params,
        "model_choices": [c if isinstance(c, dict) else {"choice": str(c), "status": "MODEL_CHOICE"}
                          for c in job.model_choices],
        "app_info": job.meta.get("app_info") or {"name": job.meta.get("product", job.app)},
        "git": git_info(repo_root(ctx.env), ["src/vkm_ansys"]),
        "meta": {k: v for k, v in job.meta.items() if k != "app_info"},
        "log_files": LOG_FILES,
        "license_patterns": list(DEFAULT_LICENSE_PATTERNS) + EXTRA_LICENSE_PATTERNS,
        "progress": {"module": "vkm_ansys.product_jobs", "function": "progress", "args": {}},
        "success_exit_codes": [0],
    }


def submit(ctx: Any, job: pj.ProductJob, wait_s: int = 0) -> dict[str, Any]:
    if not 0 <= int(wait_s) <= 600:
        raise pj.fail("INVALID_ARGUMENT", "wait_s must be 0..600")
    svc = jobs_service(ctx)
    draft = svc.draft(job.app)
    try:
        for name, data in job.rendered(Path(draft.dir)).items():
            draft.write_input(name, data, source=f"vkm-ansys:{job.kind}")
        for src, name in job.inputs:
            draft.copy_input(src, name)
        spec = draft.spec(**build_spec_fields(ctx, job, Path(draft.dir)))
    except BaseException:
        draft.discard()
        raise
    return svc.submit(draft, spec, wait_s=wait_s)

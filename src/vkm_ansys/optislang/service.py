"""Ansys optiSLang tools of ``vkm-ansys`` (agent ANS2): job builders, discovery and result reading.

Runs are jobs of the shared job layer (pool ``ansys``): the job interpreter runs the staged entry
``in/vkm_osl_entry.py`` (:mod:`vkm_ansys.optislang.entry`), which drives optiSLang through PyOptiSLang in batch mode.
Gate (CLAUDE.md): sensitivity analysis, robustness and Monte Carlo only by explicit task — a run needs
``authorized_by`` naming that task; it is recorded in the receipt. Results are MODEL_RESULT; the linear fit of the
design tables is a DERIVATION of them.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from vkm_ansys import product_jobs as pj

HERE = Path(__file__).resolve().parent
ENTRY = HERE / "entry.py"
KIT = HERE.parent / "entry_kit.py"
EXE_REL = "optiSLang/optislang.com"
PROCESS_NAMES = ("optislang.com", "optislang.exe", "oslpp.exe", "optislang-python.exe")
LICENCE_PATTERN = r"^(osl_[a-z0-9_]+|dynardo_[a-z0-9_]+|optishpc(_pack)?|liboptislang|icliboptislang)$"
SAVE_OPF = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}\.opf$"
LOCK_TIMEOUT_S = 1800
MAX_TIMEOUT_S = 72 * 3600
MAX_ROWS = 2000


def _checks(save_as: str | None) -> list[dict[str, Any]]:
    checks = [{"name": "entry_ok", "kind": "json_value", "path": "out/entry_status.json", "pointer": "/ok",
               "expected": True},
              {"name": "loopback_only", "kind": "json_value", "path": "out/entry_status.json",
               "pointer": "/non_loopback_listeners", "expected": 0}]
    if save_as:
        checks.append({"name": "project_saved", "kind": "file_exists", "path": f"out/{save_as}"})
    return checks


class OptislangService:
    def __init__(self, ctx: Any):
        self.ctx = ctx

    # ------------------------------------------------------------------------------------------------ discovery
    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {"product": "Ansys optiSLang", "pyoptislang": pj.pkg_version("ansys-optislang-core"),
                               "windows": pj.windows()}
        root = ver = None
        try:
            root, ver = self.ctx.ansys_root()
        except Exception as exc:  # noqa: BLE001
            out.update({"installed": False, "reason": getattr(exc, "message", str(exc))})
        if root is not None:
            exe = root / EXE_REL
            text = pj.read_build_lines(root)
            out.update({"installed": exe.is_file(), "version": ver,
                        "executable": {"path": f"<ANSYS_ROOT>/{EXE_REL}", "exists": exe.is_file()},
                        "build": pj.build_section(text, "optiSLang")[:3],
                        "channel": "PyOptiSLang batch (local-domain server of a child process; no TCP port of "
                                   "ours)"})
        out["processes"] = pj.product_processes(PROCESS_NAMES)
        out["licence"] = pj.licence_features(self.ctx.env, root, {"optislang": LICENCE_PATTERN})
        out["gate"] = "sensitivity / robustness / Monte Carlo runs need authorized_by (an explicit task, CLAUDE.md)"
        return out

    @staticmethod
    def node_types(filter_text: str | None = None, limit: int = 200) -> dict[str, Any]:
        """Node types known to PyOptiSLang (static module; optiSLang is not started)."""
        try:
            from ansys.optislang.core import node_types as nt
        except ImportError as exc:
            raise pj.fail("APP_UNAVAILABLE", "ansys-optislang-core is not installed in this interpreter",
                          reason=str(exc)) from exc
        rows = []
        for name in sorted(dir(nt)):
            obj = getattr(nt, name)
            if type(obj).__name__ != "NodeType":
                continue
            row = {"name": name, "id": obj.id, "subtype": str(getattr(obj.subtype, "name", obj.subtype)),
                   "class": str(getattr(obj.osl_class_type, "name", obj.osl_class_type))}
            if filter_text and filter_text.lower() not in f"{name} {row['id']} {row['subtype']}".lower():
                continue
            rows.append(row)
        return {"count": len(rows), "node_types": rows[:limit], "truncated": len(rows) > limit}

    # ------------------------------------------------------------------------------------------------ jobs
    def _sim_root(self) -> Path | None:
        try:
            return self.ctx.sim_root()
        except Exception:  # noqa: BLE001
            return None

    def _base(self, kind: str, request: dict[str, Any], *, timeout_s: int, label: str | None, save_as: str | None,
              checks: list[dict[str, Any]] | None = None, params: list[dict[str, Any]] | None = None,
              model_choices: list[str] | None = None,
              license_probe: bool = False) -> tuple[pj.ProductJob, dict[str, Any]]:
        if not 1 <= int(timeout_s) <= MAX_TIMEOUT_S:
            raise pj.fail("INVALID_ARGUMENT", f"timeout_s must be 1..{MAX_TIMEOUT_S}")
        if save_as is not None and not re.fullmatch(SAVE_OPF, save_as):
            raise pj.fail("INVALID_ARGUMENT", "save_as must be a file name like project.opf (it lands in out/)")
        root, ver = self.ctx.ansys_root()
        exe = root / EXE_REL
        if not exe.is_file():
            raise pj.fail("APP_UNAVAILABLE", "optiSLang executable not found", path=f"<ANSYS_ROOT>/{EXE_REL}")
        request = {"schema": "vkm.osl_request/1", "version": ver, "executable": str(exe), "machine_lock": True,
                   "lock_timeout_s": min(LOCK_TIMEOUT_S, int(timeout_s)), "export_designs": True, **request}
        if save_as:
            request["save_as"] = f"out/{save_as}"
        if license_probe:
            request["license_probe"] = {"lmutil": str(root / "licensingclient" / "winx64" / "lmutil.exe"),
                                        "pattern": LICENCE_PATTERN, "delay_s": 10}
        job = pj.ProductJob(app="OSL", kind=kind, argv=["{PYTHON}", "{IN}/vkm_osl_entry.py", "{JOB_DIR}"],
                            timeout_s=int(timeout_s), label=label,
                            checks=_checks(save_as) + pj.validate_checks(checks),
                            params=pj.validate_params(params), model_choices=list(model_choices or []),
                            meta={"product": "optislang", "mode": request.get("mode"), "run": request.get("run"),
                                  "authorized_by": request.get("authorized_by"), "result_status": "MODEL_RESULT",
                                  "app_info": {"name": "Ansys optiSLang", "version": f"{ver}"}})
        job.staged["vkm_osl_entry.py"] = ENTRY.read_bytes()
        job.staged["vkm_entry_kit.py"] = KIT.read_bytes()
        return job, request

    @staticmethod
    def _finish(job: pj.ProductJob, request: dict[str, Any]) -> pj.ProductJob:
        job.staged["osl_request.json"] = pj.json_bytes(request)
        job.meta["request"] = {k: v for k, v in request.items() if k not in ("executable", "license_probe")}
        return job

    def _stage_project(self, ref: str, job: pj.ProductJob, request: dict[str, Any]) -> None:
        src = pj.resolve_input(ref, self._sim_root(), kinds=("file",))
        if src.suffix.lower() != ".opf":
            raise pj.fail("INVALID_ARGUMENT", "project must be an optiSLang .opf project")
        job.inputs.append((src, f"project/{src.name}"))
        data = pj.companion_dir(src)
        if data is not None:
            job.inputs.append((data, f"project/{data.name}"))
        request["project_input"] = f"in/project/{src.name}"

    def build_run(self, *, mode: str = "script", script: str | None = None, script_path: str | None = None,
                  project: str | None = None, run: bool = True, authorized_by: str | None = None,
                  export_designs: bool = True, save_as: str | None = "project.opf",
                  params: list[dict[str, Any]] | None = None, model_choices: list[str] | None = None,
                  checks: list[dict[str, Any]] | None = None, timeout_s: int = 86400, label: str | None = None,
                  license_probe: bool = False) -> pj.ProductJob:
        if mode not in ("script", "run_project"):
            raise pj.fail("INVALID_ARGUMENT", "mode must be script or run_project")
        if mode == "run_project" and (not project or script is not None or script_path is not None):
            raise pj.fail("INVALID_ARGUMENT", "run_project needs project and no script")
        if run and (not authorized_by or len(authorized_by.strip()) < 10):
            raise pj.fail("GATE_CLOSED", "optiSLang runs (sensitivity, robustness, Monte Carlo, optimisation) need "
                                         "authorized_by: the explicit task that allows them (CLAUDE.md)")
        job, request = self._base("optislang.run", {"mode": mode, "run": bool(run),
                                                    "authorized_by": (authorized_by or "").strip()[:300] or None,
                                                    "export_designs": bool(export_designs)},
                                  timeout_s=timeout_s, label=label, save_as=save_as, checks=checks, params=params,
                                  model_choices=model_choices, license_probe=license_probe)
        if mode == "script":
            data, origin = pj.script_bytes(script, script_path, self._sim_root())
            job.staged["script.py"] = data
            job.meta["script_origin"] = origin
            request["script"] = "in/script.py"
        if project:
            self._stage_project(project, job, request)
        return self._finish(job, request)

    def build_summary(self, *, project: str, timeout_s: int = 900, label: str | None = None) -> pj.ProductJob:
        job, request = self._base("optislang.summary", {"mode": "summary", "run": False, "export_designs": True},
                                  timeout_s=timeout_s, label=label, save_as=None)
        self._stage_project(project, job, request)
        return self._finish(job, request)

    # ------------------------------------------------------------------------------------------------ results
    def results(self, job_id: str, system: str | None = None, state: str | None = None,
                max_rows: int = 200) -> dict[str, Any]:
        """Summary and design tables of a finished optiSLang job (files of the job; nothing is started)."""
        from vkm_ansys.product_submit import jobs_service

        if not 1 <= max_rows <= MAX_ROWS:
            raise pj.fail("INVALID_ARGUMENT", f"max_rows must be 1..{MAX_ROWS}")
        svc = jobs_service(self.ctx)
        job_dir = Path(svc.job_dir(job_id))
        st = svc.status(job_id)
        if st.get("status") in ("QUEUED", "RUNNING"):
            raise pj.fail("JOB_NOT_FINISHED", f"job {job_id} is {st.get('status')}")
        summary_path = job_dir / "out" / "result.json"
        if not summary_path.is_file():
            raise pj.fail("RESULT_NOT_FOUND", f"job {job_id} has no out/result.json")
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        out: dict[str, Any] = {"job_id": job_id, "status": st.get("status"), "result_status": "MODEL_RESULT",
                               "project_status": summary.get("project_status"), "systems": {}}
        for name, info in (summary.get("systems") or {}).items():
            if system and name != system:
                continue
            states = {}
            for hid, sdata in (info.get("states") or {}).items():
                if state and hid != state:
                    continue
                entry = {k: v for k, v in sdata.items() if k != "table"}
                table = job_dir / "out" / "designs" / f"{re.sub(r'[^A-Za-z0-9_.-]+', '_', name)[:80]}__{hid}.json"
                if table.is_file():
                    rows = json.loads(table.read_text(encoding="utf-8")).get("designs", [])
                    entry["rows"] = rows[:max_rows]
                    entry["rows_truncated"] = len(rows) > max_rows
                states[hid] = entry
            out["systems"][name] = {**{k: v for k, v in info.items() if k != "states"}, "states": states}
        return out

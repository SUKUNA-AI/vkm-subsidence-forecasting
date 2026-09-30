"""Ansys Workbench tools of ``vkm-ansys`` (agent ANS2): job builders and discovery.

Runs are jobs of the shared job layer (pool ``ansys``): the job interpreter runs the staged entry
``in/vkm_wb_entry.py`` (:mod:`vkm_ansys.workbench.entry`), which starts ``RunWB2.exe -B -R`` on the staged journal
wrapper (:mod:`vkm_ansys.workbench.journal_wrapper`, IronPython inside Workbench). Projects travel between jobs as
``job:<job_id>/out/<name>.wbpj`` (with its ``_files`` folder) or ``.wbpz`` archives.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from vkm_ansys import product_jobs as pj

HERE = Path(__file__).resolve().parent
ENTRY = HERE / "entry.py"
WRAPPER = HERE / "journal_wrapper.py"
KIT = HERE.parent / "entry_kit.py"
EXE_REL = "Framework/bin/Win64/RunWB2.exe"
PROCESS_NAMES = ("RunWB2.exe", "AnsysFWW.exe", "AnsysFW.exe", "AnsysFWH.exe", "Ansys.Framework.exe")
# Workbench itself takes no feature; systems take their products' features (geometry, Mechanical, solvers)
LICENCE_PATTERN = r"^(a_geometry|a_spaceclaim_dirmod|discovery_geom|disco_level\d|preppost|ansys|mech_[a-z0-9_]+)$"
SAVE_WBPJ = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}\.wbpj$"
SAVE_WBPZ = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}\.wbpz$"
LOCK_TIMEOUT_S = 1800
MAX_TIMEOUT_S = 86400


def _checks(save_as: str | None, archive_as: str | None) -> list[dict[str, Any]]:
    checks = [{"name": "entry_ok", "kind": "json_value", "path": "out/entry_status.json", "pointer": "/ok",
               "expected": True},
              {"name": "loopback_only", "kind": "json_value", "path": "out/entry_status.json",
               "pointer": "/non_loopback_listeners", "expected": 0}]
    if save_as:
        checks.append({"name": "project_saved", "kind": "file_exists", "path": f"out/{save_as}"})
    if archive_as:
        checks.append({"name": "archive_written", "kind": "file_exists", "path": f"out/{archive_as}"})
    return checks


class WorkbenchService:
    def __init__(self, ctx: Any):
        self.ctx = ctx

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {"product": "Ansys Workbench", "pyworkbench": pj.pkg_version("ansys-workbench-core"),
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
                        "build": {"workbench": pj.build_section(text, "WB")[:3],
                                  "framework": pj.build_section(text, "framewrk")[:1]},
                        "channels": {"batch_journal": "AVAILABLE" if exe.is_file() else "UNAVAILABLE",
                                     "server_session": "NOT_EXPOSED (PyWorkbench launches through WMI outside the "
                                                       "job tree; batch journals are used instead)"}})
        out["processes"] = pj.product_processes(PROCESS_NAMES)
        out["licence"] = pj.licence_features(self.ctx.env, root, {"workbench_systems": LICENCE_PATTERN})
        return out

    # ------------------------------------------------------------------------------------------------ jobs
    def _sim_root(self) -> Path | None:
        try:
            return self.ctx.sim_root()
        except Exception:  # noqa: BLE001
            return None

    def _base(self, kind: str, request: dict[str, Any], *, timeout_s: int, label: str | None,
              save_as: str | None, archive_as: str | None, checks: list[dict[str, Any]] | None = None,
              params: list[dict[str, Any]] | None = None, model_choices: list[str] | None = None,
              license_probe: bool = False) -> tuple[pj.ProductJob, dict[str, Any]]:
        if not 1 <= int(timeout_s) <= MAX_TIMEOUT_S:
            raise pj.fail("INVALID_ARGUMENT", f"timeout_s must be 1..{MAX_TIMEOUT_S}")
        if save_as is not None and not re.fullmatch(SAVE_WBPJ, save_as):
            raise pj.fail("INVALID_ARGUMENT", "save_as must be a file name like project.wbpj (it lands in out/)")
        if archive_as is not None and not re.fullmatch(SAVE_WBPZ, archive_as):
            raise pj.fail("INVALID_ARGUMENT", "archive_as must be a file name like project.wbpz (it lands in out/)")
        root, ver = self.ctx.ansys_root()
        exe = root / EXE_REL
        if not exe.is_file():
            raise pj.fail("APP_UNAVAILABLE", "Workbench executable not found", path=f"<ANSYS_ROOT>/{EXE_REL}")
        request = {"schema": "vkm.wb_request/1", "version": ver, "exe": str(exe), "machine_lock": True,
                   "lock_timeout_s": min(LOCK_TIMEOUT_S, int(timeout_s)), **request}
        if save_as:
            request["save_as"] = f"out/{save_as}"
        if archive_as:
            request["archive_as"] = f"out/{archive_as}"
        if license_probe:
            request["license_probe"] = {"lmutil": str(root / "licensingclient" / "winx64" / "lmutil.exe"),
                                        "pattern": LICENCE_PATTERN, "delay_s": 30}
        job = pj.ProductJob(app="WB", kind=kind, argv=["{PYTHON}", "{IN}/vkm_wb_entry.py", "{JOB_DIR}"],
                            timeout_s=int(timeout_s), label=label,
                            checks=_checks(save_as, archive_as) + pj.validate_checks(checks),
                            params=pj.validate_params(params), model_choices=list(model_choices or []),
                            meta={"product": "workbench", "mode": request.get("mode"),
                                  "helper": request.get("helper"), "result_status": "MODEL_RESULT",
                                  "app_info": {"name": "Ansys Workbench", "version": f"{ver}"}})
        job.staged["vkm_wb_entry.py"] = ENTRY.read_bytes()
        job.staged["vkm_entry_kit.py"] = KIT.read_bytes()
        job.staged["vkm_wb_wrapper.wbjn"] = WRAPPER.read_bytes().replace(b'"@@VKM_JOB_DIR@@"',
                                                                          b'"' + pj.JOB_DIR_MARKER.encode() + b'"')
        return job, request

    @staticmethod
    def _finish(job: pj.ProductJob, request: dict[str, Any]) -> pj.ProductJob:
        job.staged["wb_request.json"] = pj.json_bytes(request)
        job.meta["request"] = {k: v for k, v in request.items() if k not in ("exe", "license_probe")}
        return job

    def _stage_project(self, ref: str, job: pj.ProductJob, request: dict[str, Any]) -> None:
        src = pj.resolve_input(ref, self._sim_root(), kinds=("file",))
        suffix = src.suffix.lower()
        if suffix not in (".wbpj", ".wbpz"):
            raise pj.fail("INVALID_ARGUMENT", "project must be a .wbpj project or a .wbpz archive")
        job.inputs.append((src, f"project/{src.name}"))
        if suffix == ".wbpj":
            comp = pj.companion_dir(src)
            if comp is not None:
                job.inputs.append((comp, f"project/{comp.name}"))
            request["project_input"] = f"in/project/{src.name}"
            request["project"] = f"work/project/{src.name}"
        else:
            request["project"] = f"in/project/{src.name}"

    def build_run_journal(self, *, journal: str | None, journal_path: str | None, project: str | None = None,
                          inputs: list[str] | None = None, save_as: str | None = None,
                          archive_as: str | None = None, archive_include_results: bool = True,
                          params: list[dict[str, Any]] | None = None, model_choices: list[str] | None = None,
                          checks: list[dict[str, Any]] | None = None, timeout_s: int = 3600,
                          label: str | None = None, license_probe: bool = False) -> pj.ProductJob:
        data, origin = pj.script_bytes(journal, journal_path, self._sim_root(), what="journal")
        job, request = self._base("workbench.journal", {"mode": "journal", "journal": "in/journal.wbjn",
                                                        "archive_include_results": bool(archive_include_results)},
                                  timeout_s=timeout_s, label=label, save_as=save_as, archive_as=archive_as,
                                  checks=checks, params=params, model_choices=model_choices,
                                  license_probe=license_probe)
        job.staged["journal.wbjn"] = data
        job.meta["journal_origin"] = origin
        if project:
            self._stage_project(project, job, request)
        names: set[str] = set()
        for ref in inputs or []:
            src = pj.resolve_input(ref, self._sim_root())
            if src.name in names:
                raise pj.fail("INVALID_ARGUMENT", f"two inputs share the name {src.name}")
            names.add(src.name)
            job.inputs.append((src, f"inputs/{src.name}"))
        return self._finish(job, request)

    def build_summary(self, *, project: str, timeout_s: int = 900, label: str | None = None) -> pj.ProductJob:
        job, request = self._base("workbench.summary", {"mode": "helper", "helper": "summary"}, timeout_s=timeout_s,
                                  label=label, save_as=None, archive_as=None)
        self._stage_project(project, job, request)
        return self._finish(job, request)

    def build_update(self, *, project: str, systems: list[str] | None = None, save_as: str | None = "project.wbpj",
                     archive_as: str | None = None, timeout_s: int = 3600, label: str | None = None,
                     license_probe: bool = False) -> pj.ProductJob:
        job, request = self._base("workbench.update", {"mode": "helper", "helper": "update",
                                                       "args": {"systems": list(systems or [])}},
                                  timeout_s=timeout_s, label=label, save_as=save_as, archive_as=archive_as,
                                  license_probe=license_probe)
        self._stage_project(project, job, request)
        return self._finish(job, request)

    def build_archive(self, *, project: str, archive_as: str = "project.wbpz", include_results: bool = True,
                      timeout_s: int = 1800, label: str | None = None) -> pj.ProductJob:
        job, request = self._base("workbench.archive", {"mode": "helper", "helper": "archive",
                                                        "archive_include_results": bool(include_results)},
                                  timeout_s=timeout_s, label=label, save_as=None, archive_as=archive_as)
        self._stage_project(project, job, request)
        return self._finish(job, request)

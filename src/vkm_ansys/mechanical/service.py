"""Ansys Mechanical tools of ``vkm-ansys`` (agent ANS2): job builders, discovery and the offline API search.

Every run is a job of the shared job layer (pool ``ansys``): the job interpreter runs the staged entry
``in/vkm_mech_entry.py`` (:mod:`vkm_ansys.mechanical.entry`), which either embeds Mechanical through PyMechanical
(``engine="embedded"``, no port) or starts ``AnsysWBU.exe`` in batch mode on the staged wrapper
(``engine="batch"``, Mechanical's own scripting engine: IronPython, or CPython with ``-engineType cpython``).
Projects travel between jobs as ``job:<job_id>/out/<name>.mechdb`` references (the ``_Mech_Files`` folder goes along).
Solver results are MODEL_RESULT; the typed helpers write no evidence.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from vkm_ansys import product_jobs as pj
from vkm_ansys.mechanical import api_index, step_box

HERE = Path(__file__).resolve().parent
ENTRY = HERE / "entry.py"
WRAPPER = HERE / "batch_wrapper.py"
KIT = HERE.parent / "entry_kit.py"
LOCK_TIMEOUT_S = 1800
EXE_REL = "aisol/bin/winx64/AnsysWBU.exe"
PROCESS_NAMES = ("AnsysWBU.exe", "ANSYS261.exe", "AnsysFWW.exe", "Ans.Rsm.Launcher.exe")
# licence feature names attributed to Mechanical / its solver by name (a heuristic; the receipt shows the real use)
LICENCE_PATTERN = r"^(ansys|ansys[a-z]{2}|meba|mebach|mech_[a-z0-9_]+|mechanical|mechhpc|preppost|prf[a-z0-9]*|struct\d?)$"
SAVE_NAME = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}\.mechdb$"
ANALYSES = ("static_structural", "transient_structural", "modal", "steady_state_thermal", "transient_thermal",
            "eigenvalue_buckling")
RESULT_TYPES = ("total_deformation", "directional_deformation", "equivalent_stress", "normal_stress", "shear_stress",
                "maximum_principal_stress", "minimum_principal_stress", "equivalent_elastic_strain",
                "normal_elastic_strain", "force_reaction")
UNIT_SYSTEMS = ("StandardMKS", "StandardCGS", "StandardNMM", "StandardBFT", "StandardBIN", "StandardUMKS")
DEFAULT_TIMEOUT_S, MAX_TIMEOUT_S = 3600, 86400
MAX_INPUTS = 50


def _ok_checks(save_as: str | None) -> list[dict[str, Any]]:
    checks = [{"name": "entry_ok", "kind": "json_value", "path": "out/entry_status.json", "pointer": "/ok",
               "expected": True},
              {"name": "loopback_only", "kind": "json_value", "path": "out/entry_status.json",
               "pointer": "/non_loopback_listeners", "expected": 0}]
    if save_as:
        checks.append({"name": "project_saved", "kind": "file_exists", "path": f"out/{save_as}"})
    return checks


class MechanicalService:
    def __init__(self, ctx: Any):
        self.ctx = ctx

    # ------------------------------------------------------------------------------------------------ discovery
    def status(self) -> dict[str, Any]:
        """Installation, versions, engines, processes and licence features seen — Mechanical is not started."""
        env = self.ctx.env
        out: dict[str, Any] = {"product": "Ansys Mechanical", "pymechanical": pj.pkg_version("ansys-mechanical-core"),
                               "stubs": pj.pkg_version("ansys-mechanical-stubs"),
                               "pythonnet": pj.pkg_version("ansys-pythonnet"), "windows": pj.windows()}
        root = ver = None
        try:
            root, ver = self.ctx.ansys_root()
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            out["installed"] = False
            out["reason"] = getattr(exc, "message", str(exc))
        if root is not None:
            exe = root / EXE_REL
            out.update({"installed": exe.is_file(), "version": ver,
                        "executable": {"path": f"<ANSYS_ROOT>/{EXE_REL}", "exists": exe.is_file()},
                        "build": {"workbench": pj.build_section(pj.read_build_lines(root), "WB")[:3],
                                  "framework": pj.build_section(pj.read_build_lines(root), "framewrk")[:1],
                                  "solver": pj.build_section(pj.read_build_lines(root), "solver")[:1]}})
            v = int(ver) if ver and ver.isdigit() else 0
            out["engines"] = {
                "embedded": "AVAILABLE" if out["pymechanical"] and pj.windows() and v >= 232 else
                            "UNAVAILABLE:needs Windows, PyMechanical and Mechanical 2023 R2+",
                "batch": "AVAILABLE" if exe.is_file() else "UNAVAILABLE:AnsysWBU.exe not found",
                "batch_cpython": "AVAILABLE" if exe.is_file() and v >= 261 else "UNAVAILABLE:needs 2026 R1+"}
        out["processes"] = pj.product_processes(PROCESS_NAMES)
        out["licence"] = pj.licence_features(env, root, {"mechanical": LICENCE_PATTERN})
        out["api_index"] = {"stubs_releases": api_index.releases()}
        return out

    def api_search(self, query: str, limit: int = 20, kinds: list[str] | None = None,
                   release: str | None = None) -> dict[str, Any]:
        if release is None:
            try:
                release = self.ctx.ansys_root()[1] or "261"
            except Exception:  # noqa: BLE001
                release = "261"
        return api_index.search(query, release=release, limit=limit, kinds=kinds)

    # ------------------------------------------------------------------------------------------------ jobs
    def _sim_root(self) -> Path | None:
        try:
            return self.ctx.sim_root()
        except Exception:  # noqa: BLE001 - job: references then fail with SIM_ROOT_UNAVAILABLE
            return None

    def _stage_project(self, ref: str, job: pj.ProductJob) -> str:
        src = pj.resolve_input(ref, self._sim_root(), kinds=("file",))
        if src.suffix.lower() not in (".mechdb", ".mechdat"):
            raise pj.fail("INVALID_ARGUMENT", "db_file must be a .mechdb (or .mechdat) project")
        job.inputs.append((src, f"model/{src.name}"))
        comp = pj.companion_dir(src)
        if comp is not None:
            job.inputs.append((comp, f"model/{comp.name}"))
        return f"in/model/{src.name}"

    def _base(self, kind: str, request: dict[str, Any], *, timeout_s: int, label: str | None,
              save_as: str | None, checks: list[dict[str, Any]] | None = None,
              params: list[dict[str, Any]] | None = None, model_choices: list[str] | None = None,
              license_probe: bool = False) -> tuple[pj.ProductJob, dict[str, Any]]:
        if not 1 <= int(timeout_s) <= MAX_TIMEOUT_S:
            raise pj.fail("INVALID_ARGUMENT", f"timeout_s must be 1..{MAX_TIMEOUT_S}")
        if save_as is not None and not re.fullmatch(SAVE_NAME, save_as):
            raise pj.fail("INVALID_ARGUMENT", "save_as must be a file name like model.mechdb (it lands in out/)")
        root, ver = self.ctx.ansys_root()
        request = {"schema": "vkm.mech_request/1", "version": int(ver or 261), "unit_system": None,
                   "machine_lock": True, "lock_timeout_s": min(LOCK_TIMEOUT_S, int(timeout_s)), **request}
        if save_as:
            request["save_as"] = f"out/{save_as}"
        if license_probe:
            request["license_probe"] = {"lmutil": str(root / "licensingclient" / "winx64" / "lmutil.exe"),
                                        "pattern": LICENCE_PATTERN, "delay_s": 20}
        if request.get("engine") == "batch":
            exe = root / EXE_REL
            if not exe.is_file():
                raise pj.fail("APP_UNAVAILABLE", "Mechanical executable not found", path=f"<ANSYS_ROOT>/{EXE_REL}")
            request["exe"] = str(exe)
        job = pj.ProductJob(app="MECH", kind=kind, argv=["{PYTHON}", "{IN}/vkm_mech_entry.py", "{JOB_DIR}"],
                            timeout_s=int(timeout_s), label=label,
                            checks=_ok_checks(save_as) + pj.validate_checks(checks),
                            params=pj.validate_params(params), model_choices=list(model_choices or []),
                            meta={"product": "mechanical", "engine": request.get("engine", "embedded"),
                                  "mode": request.get("mode"), "helper": request.get("helper"),
                                  "result_status": "MODEL_RESULT"},
                            uses_license=not request.get("readonly", False))
        job.staged["vkm_mech_entry.py"] = ENTRY.read_bytes()
        job.staged["vkm_entry_kit.py"] = KIT.read_bytes()
        job.meta["app_info"] = {"name": "Ansys Mechanical", "version": f"{ver}",
                                "engine": request.get("engine", "embedded")}
        if request.get("engine") == "batch":
            job.staged["vkm_mech_batch.py"] = WRAPPER.read_bytes().replace(b'"@@VKM_JOB_DIR@@"',
                                                                            b'"' + pj.JOB_DIR_MARKER.encode() + b'"')
        return job, request

    @staticmethod
    def _finish(job: pj.ProductJob, request: dict[str, Any]) -> pj.ProductJob:
        job.staged["mech_request.json"] = pj.json_bytes(request)
        job.meta["request"] = {k: v for k, v in request.items() if k not in ("exe", "license_probe")}
        return job

    def build_run_script(self, *, script: str | None, script_path: str | None, engine: str = "embedded",
                         batch_engine_type: str = "ironpython", db_file: str | None = None,
                         inputs: list[str] | None = None, save_as: str | None = None, readonly: bool = False,
                         unit_system: str | None = None, params: list[dict[str, Any]] | None = None,
                         model_choices: list[str] | None = None, checks: list[dict[str, Any]] | None = None,
                         timeout_s: int = DEFAULT_TIMEOUT_S, label: str | None = None,
                         license_probe: bool = False) -> pj.ProductJob:
        if engine not in ("embedded", "batch"):
            raise pj.fail("INVALID_ARGUMENT", "engine must be embedded or batch")
        if engine == "batch" and readonly:
            raise pj.fail("INVALID_ARGUMENT", "readonly is an embedded-engine option")
        if unit_system is not None and unit_system not in UNIT_SYSTEMS:
            raise pj.fail("INVALID_ARGUMENT", f"unit_system must be one of {UNIT_SYSTEMS}")
        data, origin = pj.script_bytes(script, script_path, self._sim_root())
        request: dict[str, Any] = {"engine": engine, "mode": "script", "script": "in/script.py",
                                   "readonly": bool(readonly), "unit_system": unit_system}
        if engine == "batch" and batch_engine_type == "cpython":
            request["engine_type"] = "cpython"
        job, request = self._base("mechanical.script", request, timeout_s=timeout_s, label=label,
                                  save_as=save_as, checks=checks, params=params, model_choices=model_choices,
                                  license_probe=license_probe)
        job.staged["script.py"] = data
        job.meta["script_origin"] = origin
        if db_file:
            request["db_file"] = self._stage_project(db_file, job)
        self._stage_inputs(inputs, job)
        return self._finish(job, request)

    def _stage_inputs(self, inputs: list[str] | None, job: pj.ProductJob) -> None:
        names: set[str] = set()
        for ref in (inputs or [])[:MAX_INPUTS + 1]:
            if len(names) >= MAX_INPUTS:
                raise pj.fail("INVALID_ARGUMENT", f"at most {MAX_INPUTS} inputs")
            src = pj.resolve_input(ref, self._sim_root())
            if src.name in names:
                raise pj.fail("INVALID_ARGUMENT", f"two inputs share the name {src.name}")
            names.add(src.name)
            job.inputs.append((src, f"inputs/{src.name}"))

    def build_import_geometry(self, *, geometry: str | None = None, box_m: list[float] | None = None,
                              analysis: str | None = "static_structural", named_selections: bool = False,
                              unit_system: str = "StandardMKS", save_as: str = "model.mechdb",
                              timeout_s: int = 1800, label: str | None = None,
                              license_probe: bool = False) -> pj.ProductJob:
        if (geometry is None) == (box_m is None):
            raise pj.fail("INVALID_ARGUMENT", "give exactly one of geometry (a CAD file) or box_m ([lx, ly, lz])")
        if analysis is not None and analysis not in ANALYSES:
            raise pj.fail("INVALID_ARGUMENT", f"analysis must be one of {ANALYSES} or null")
        args: dict[str, Any] = {"analysis": analysis, "named_selections": bool(named_selections)}
        job, request = self._base("mechanical.import_geometry",
                                  {"engine": "embedded", "mode": "helper", "helper": "import_geometry", "args": args,
                                   "unit_system": unit_system},
                                  timeout_s=timeout_s, label=label, save_as=save_as, license_probe=license_probe,
                                  model_choices=[f"analysis={analysis}"])
        if box_m is not None:
            if len(box_m) != 3 or any(not (0 < float(v) <= 1e5) for v in box_m):
                raise pj.fail("INVALID_ARGUMENT", "box_m = [lx, ly, lz] in metres, each in (0, 1e5]")
            job.staged["geometry/box.step"] = step_box.box_step(*[float(v) for v in box_m], name="VKM_TOY_BOX")
            args["file"] = "in/geometry/box.step"
            job.params.extend({"name": f"box_{a}", "value": float(v), "unit": "m",
                               "status": "ENGINEERING_ASSUMPTION", "source_ref": "TOY", "scope": "TOY"}
                              for a, v in zip("xyz", box_m))
        else:
            src = pj.resolve_input(geometry, self._sim_root(), kinds=("file",))
            job.inputs.append((src, f"geometry/{src.name}"))
            args["file"] = f"in/geometry/{src.name}"
        return self._finish(job, request)

    def build_mesh(self, *, db_file: str, element_size_m: float | None = None, element_order: str = "quadratic",
                   method: str | None = None, save_as: str = "model.mechdb", timeout_s: int = 3600,
                   label: str | None = None, license_probe: bool = False) -> pj.ProductJob:
        if element_order not in ("quadratic", "linear", "program_controlled"):
            raise pj.fail("INVALID_ARGUMENT", "element_order must be quadratic, linear or program_controlled")
        if method is not None and method not in ("tetrahedrons", "hex_dominant", "sweep", "multizone"):
            raise pj.fail("INVALID_ARGUMENT", "method must be tetrahedrons, hex_dominant, sweep or multizone")
        if element_size_m is not None and not 0 < float(element_size_m) <= 1e4:
            raise pj.fail("INVALID_ARGUMENT", "element_size_m must be in (0, 1e4]")
        args = {"element_size_m": element_size_m, "element_order": element_order, "method": method}
        choices = [f"mesh.element_order={element_order}"] + ([f"mesh.element_size_m={element_size_m}"]
                                                             if element_size_m else []) + \
                  ([f"mesh.method={method}"] if method else [])
        job, request = self._base("mechanical.mesh",
                                  {"engine": "embedded", "mode": "helper", "helper": "mesh", "args": args},
                                  timeout_s=timeout_s, label=label, save_as=save_as, model_choices=choices,
                                  license_probe=license_probe)
        request["db_file"] = self._stage_project(db_file, job)
        return self._finish(job, request)

    def build_solve(self, *, db_file: str, analysis: int | str = 0, save_as: str = "model.mechdb",
                    timeout_s: int = DEFAULT_TIMEOUT_S, label: str | None = None,
                    license_probe: bool = False) -> pj.ProductJob:
        job, request = self._base("mechanical.solve",
                                  {"engine": "embedded", "mode": "helper", "helper": "solve",
                                   "args": {"analysis": analysis}},
                                  timeout_s=timeout_s, label=label, save_as=save_as, license_probe=license_probe)
        request["db_file"] = self._stage_project(db_file, job)
        return self._finish(job, request)

    def build_results(self, *, db_file: str, quantities: list[dict[str, Any]], analysis: int | str = 0,
                      export_tables: bool = True, save_as: str | None = None, timeout_s: int = 1800,
                      label: str | None = None, license_probe: bool = False) -> pj.ProductJob:
        if not 1 <= len(quantities) <= 30:
            raise pj.fail("INVALID_ARGUMENT", "1..30 result quantities")
        clean = []
        for q in quantities:
            q = dict(q)
            if q.get("type") not in RESULT_TYPES:
                raise pj.fail("INVALID_ARGUMENT", f"result type must be one of {RESULT_TYPES}")
            if q["type"] == "force_reaction" and not q.get("boundary_condition"):
                raise pj.fail("INVALID_ARGUMENT", "force_reaction needs boundary_condition (the support's name)")
            if q.get("axis") is not None and str(q["axis"]).lower() not in ("x", "y", "z"):
                raise pj.fail("INVALID_ARGUMENT", "axis must be x, y or z")
            clean.append(q)
        job, request = self._base("mechanical.results",
                                  {"engine": "embedded", "mode": "helper", "helper": "results",
                                   "args": {"analysis": analysis, "quantities": clean,
                                            "export_tables": bool(export_tables)}},
                                  timeout_s=timeout_s, label=label, save_as=save_as, license_probe=license_probe)
        request["db_file"] = self._stage_project(db_file, job)
        return self._finish(job, request)

    def build_summary(self, *, db_file: str | None = None, max_depth: int = 6, max_nodes: int = 2000,
                      readonly: bool = True, timeout_s: int = 900, label: str | None = None) -> pj.ProductJob:
        job, request = self._base("mechanical.summary",
                                  {"engine": "embedded", "mode": "helper", "helper": "summary",
                                   "readonly": bool(readonly),
                                   "args": {"max_depth": int(max_depth), "max_nodes": int(max_nodes),
                                            "license_list": True}},
                                  timeout_s=timeout_s, label=label, save_as=None)
        if db_file:
            request["db_file"] = self._stage_project(db_file, job)
        return self._finish(job, request)


def describe_request(job: pj.ProductJob) -> dict[str, Any]:
    """The request as staged (for dry runs and tests)."""
    return json.loads(job.staged["mech_request.json"].decode("utf-8"))

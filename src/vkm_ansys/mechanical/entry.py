"""vkm-ansys Mechanical job entry (CPython, venv-ansys) — staged as ``in/vkm_mech_entry.py``.

Run by the job layer::

    <venv-ansys python> in/vkm_mech_entry.py <job_dir>

Standalone apart from ``in/vkm_entry_kit.py`` (staged next to it): the job keeps a byte-exact snapshot of the code
that ran. The request is ``in/mech_request.json`` (``vkm.mech_request/1``). Engines:

* ``embedded`` (default) — PyMechanical ``ansys.mechanical.core.App`` inside this process (no port is opened). Modes:
  ``script`` runs a user script with Mechanical's scripting globals (``app``, ``Model``, ``ExtAPI``, ``DataModel``,
  ``Tree``, ``Graphics``, ``Quantity``, enums …) plus ``JOB_DIR``, ``IN_DIR``, ``WORK_DIR``, ``OUT_DIR``, ``REQUEST``
  and a dict ``result`` that is written to ``out/result.json``; ``helper`` runs a built-in operation
  (``import_geometry``, ``mesh``, ``solve``, ``results``, ``summary``, ``license_info``);
* ``batch`` — Mechanical itself (``AnsysWBU.exe -DSApplet -AppModeMech -b -script in/vkm_mech_batch.py -x``, optionally
  ``-engineType cpython``) runs the user script in its own scripting engine; its console goes to ``logs/product.log``
  and the wrapper reports through ``out/batch_status.json``.

Before Mechanical starts, the entry takes the machine-wide licence lock of the ``ansys`` pool and holds it until
Mechanical and its helper processes have exited. An input project is copied from ``in/`` to ``work/`` before it is
opened (``in/`` stays the input snapshot) and saved to ``save_as`` (under ``out/``) when asked.
``out/entry_status.json`` records the outcome, the Mechanical messages, the child processes seen, every listening TCP
socket of the process tree and the licence features in use. Exit codes: 0 ok, 1 script or helper error, 2 bad
request, 3 licence unavailable, 4 application failure, 5 licence lock busy.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vkm_entry_kit as kit  # noqa: E402 - staged next to this file

TAG = "vkm_mech_entry"
ENTRY_VERSION = "2"
ANALYSES = {"static_structural": "AddStaticStructuralAnalysis", "transient_structural": "AddTransientStructuralAnalysis",
            "modal": "AddModalAnalysis", "steady_state_thermal": "AddSteadyStateThermalAnalysis",
            "transient_thermal": "AddTransientThermalAnalysis", "eigenvalue_buckling": "AddEigenvalueBucklingAnalysis"}
RESULTS = {"total_deformation": "AddTotalDeformation", "directional_deformation": "AddDirectionalDeformation",
           "equivalent_stress": "AddEquivalentStress", "normal_stress": "AddNormalStress",
           "shear_stress": "AddShearStress", "maximum_principal_stress": "AddMaximumPrincipalStress",
           "minimum_principal_stress": "AddMinimumPrincipalStress",
           "equivalent_elastic_strain": "AddEquivalentElasticStrain", "normal_elastic_strain": "AddNormalElasticStrain",
           "force_reaction": "AddForceReaction"}
AXIS_RESULTS = {"directional_deformation", "normal_stress", "normal_elastic_strain"}
safe = kit.safe


def log(msg: str) -> None:
    kit.log(TAG, msg)


def qty(q):
    """Ansys Quantity → {value, unit}; plain numbers pass through."""
    if q is None:
        return None
    try:
        return {"value": float(q.Value), "unit": str(q.Unit)}
    except Exception:  # noqa: BLE001
        try:
            return float(q)
        except Exception:  # noqa: BLE001
            return str(q)


def messages(ns: dict, limit: int = 200) -> list[dict]:
    out = []
    for msg in safe(lambda: list(ns["ExtAPI"].Application.Messages), []) or []:
        out.append({"severity": str(safe(lambda m=msg: m.Severity, "")).upper(),
                    "text": str(safe(lambda m=msg: m.DisplayString, ""))[:2000]})
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------------------------------- reports
def bodies_report(ns: dict) -> list[dict]:
    Model, cat = ns["Model"], ns["DataModelObjectCategory"]
    out = []
    for body in safe(lambda: list(Model.Geometry.GetChildren(cat.Body, True)), []) or []:
        geo = safe(lambda b=body: b.GetGeoBody())
        out.append({"name": safe(lambda b=body: b.Name), "material": safe(lambda b=body: b.Material),
                    "volume": qty(safe(lambda b=body: b.Volume)), "suppressed": safe(lambda b=body: b.Suppressed),
                    "faces": safe(lambda g=geo: g.Faces.Count), "edges": safe(lambda g=geo: g.Edges.Count),
                    "nodes": safe(lambda b=body: b.Nodes), "elements": safe(lambda b=body: b.Elements)})
    return out


def analyses_report(ns: dict) -> list[dict]:
    out = []
    for i, an in enumerate(safe(lambda: list(ns["Model"].Analyses), []) or []):
        out.append({"index": i, "name": safe(lambda a=an: a.Name), "type": str(safe(lambda a=an: a.AnalysisType, "")),
                    "physics": str(safe(lambda a=an: a.PhysicsType, "")),
                    "solution_state": str(safe(lambda a=an: a.Solution.ObjectState, "")),
                    "solution_status": str(safe(lambda a=an: a.Solution.Status, ""))})
    return out


def mesh_report(ns: dict) -> dict:
    mesh = ns["Model"].Mesh
    rep = {"nodes": safe(lambda: mesh.Nodes), "elements": safe(lambda: mesh.Elements),
           "state": str(safe(lambda: mesh.ObjectState, "")), "element_order": str(safe(lambda: mesh.ElementOrder, "")),
           "element_size": qty(safe(lambda: mesh.ElementSize))}
    if rep["elements"]:
        try:
            mesh.MeshMetric = ns["MeshMetricType"].ElementQuality
            rep["element_quality"] = {"min": safe(lambda: float(mesh.Minimum)),
                                      "max": safe(lambda: float(mesh.Maximum)),
                                      "average": safe(lambda: float(mesh.Average))}
        except Exception:  # noqa: BLE001 - metric not available (read-only app, no mesh)
            pass
    return rep


def tree_report(obj, depth: int, max_depth: int, budget: list) -> dict:
    budget[0] -= 1
    node = {"name": safe(lambda: obj.Name), "category": str(safe(lambda: obj.DataModelObjectCategory, "")),
            "state": str(safe(lambda: obj.ObjectState, ""))}
    if ".Results" in str(safe(lambda: obj.GetType().Namespace, "")) and hasattr(obj, "Minimum"):
        node["minimum"], node["maximum"] = qty(safe(lambda: obj.Minimum)), qty(safe(lambda: obj.Maximum))
    children = safe(lambda: list(obj.Children), []) or []
    if children and depth < max_depth and budget[0] > 0:
        node["children"] = [tree_report(c, depth + 1, max_depth, budget) for c in children if budget[0] > 0]
    elif children:
        node["children_truncated"] = len(children)
    return node


def pick_analysis(ns: dict, which):
    analyses = list(ns["Model"].Analyses)
    if not analyses:
        raise ValueError("the model has no analysis")
    if isinstance(which, int):
        return analyses[which]
    for an in analyses:
        if an.Name == which:
            return an
    raise ValueError(f"analysis not found: {which!r}")


def find_object(ns: dict, name: str):
    for obj in ns["DataModel"].GetObjectsByName(name) or []:
        return obj
    raise ValueError(f"object not found by name: {name!r}")


# ---------------------------------------------------------------------------------------------------- helpers
def h_import_geometry(ns: dict, args: dict, jd: Path) -> dict:
    path = (jd / args["file"]).resolve()
    gi = ns["app"].helpers.import_geometry(str(path), process_named_selections=bool(args.get("named_selections")),
                                           named_selection_key=args.get("named_selection_key", "NS"))
    added = None
    if args.get("analysis"):
        added = getattr(ns["Model"], ANALYSES[args["analysis"]])().Name
    return {"geometry_import_state": str(safe(lambda: gi.ObjectState, "")), "bodies": bodies_report(ns),
            "analysis_added": added, "analyses": analyses_report(ns)}


def h_mesh(ns: dict, args: dict, jd: Path) -> dict:
    mesh = ns["Model"].Mesh
    if args.get("element_size_m"):
        mesh.ElementSize = ns["Quantity"](f"{float(args['element_size_m'])!r} [m]")
    order = args.get("element_order")
    if order:
        mesh.ElementOrder = {"quadratic": ns["ElementOrder"].Quadratic, "linear": ns["ElementOrder"].Linear,
                             "program_controlled": ns["ElementOrder"].ProgramControlled}[order]
    method = args.get("method")
    if method:
        ctrl = mesh.AddAutomaticMethod()
        sel = ns["ExtAPI"].SelectionManager.CreateSelectionInfo(ns["SelectionTypeEnum"].GeometryEntities)
        sel.Ids = [b.GetGeoBody().Id for b in ns["Model"].Geometry.GetChildren(ns["DataModelObjectCategory"].Body,
                                                                                True)]
        ctrl.Location = sel
        ctrl.Method = {"tetrahedrons": ns["MethodType"].AllTriAllTet, "hex_dominant": ns["MethodType"].HexDominant,
                       "sweep": ns["MethodType"].Sweep, "multizone": ns["MethodType"].MultiZone}[method]
    t0 = time.time()
    mesh.GenerateMesh()
    return {"seconds": round(time.time() - t0, 3), "mesh": mesh_report(ns), "bodies": bodies_report(ns)}


def h_solve(ns: dict, args: dict, jd: Path) -> dict:
    an = pick_analysis(ns, args.get("analysis", 0))
    t0 = time.time()
    an.Solve(True)
    sol = an.Solution
    rep = {"analysis": an.Name, "seconds": round(time.time() - t0, 3),
           "solution_state": str(safe(lambda: sol.ObjectState, "")),
           "solution_status": str(safe(lambda: sol.Status, ""))}
    wd = safe(lambda: an.WorkingDir)
    if wd and Path(wd).is_dir():
        files = sorted(p for p in Path(wd).iterdir() if p.is_file())
        rep["solver_files"] = [{"name": p.name, "bytes": p.stat().st_size} for p in files][:200]
        target = jd / "out" / "solver"
        target.mkdir(parents=True, exist_ok=True)
        for name in ("solve.out", "file.err", "ds.dat", "MatML.xml"):
            src = Path(wd) / name
            if src.is_file() and src.stat().st_size < 50_000_000:
                shutil.copy2(src, target / name)
        text = (Path(wd) / "solve.out").read_text(errors="replace") if (Path(wd) / "solve.out").is_file() else ""
        rep["solve_out"] = {"errors": text.count("*** ERROR ***"), "warnings": text.count("*** WARNING ***"),
                            "copied_to": "out/solver/"}
    return rep


def h_results(ns: dict, args: dict, jd: Path) -> dict:
    an = pick_analysis(ns, args.get("analysis", 0))
    sol = an.Solution
    created = []
    for i, spec in enumerate(args.get("quantities") or []):
        kind = spec["type"]
        obj = getattr(sol, RESULTS[kind])()
        if spec.get("name"):
            obj.Name = spec["name"]
        if kind in AXIS_RESULTS:
            obj.NormalOrientation = getattr(ns["NormalOrientationType"], f"{(spec.get('axis') or 'z').upper()}Axis")
        if kind == "force_reaction":
            obj.BoundaryConditionSelection = find_object(ns, spec["boundary_condition"])
        elif spec.get("scope"):
            obj.Location = find_object(ns, spec["scope"])
        if spec.get("time") is not None:
            obj.DisplayTime = ns["Quantity"](f"{float(spec['time'])!r} [s]")
        created.append((i, kind, spec, obj))
    sol.EvaluateAllResults()
    rows = []
    for i, kind, spec, obj in created:
        row = {"index": i, "type": kind, "name": safe(lambda o=obj: o.Name), "axis": spec.get("axis"),
               "scope": spec.get("scope") or spec.get("boundary_condition"),
               "state": str(safe(lambda o=obj: o.ObjectState, ""))}
        if kind == "force_reaction":
            for axis, prop in (("x", "XAxis"), ("y", "YAxis"), ("z", "ZAxis"), ("total", "Total")):
                row[axis] = qty(safe(lambda o=obj, p=prop: getattr(o, p)))
        else:
            row.update({"minimum": qty(safe(lambda o=obj: o.Minimum)), "maximum": qty(safe(lambda o=obj: o.Maximum)),
                        "average": qty(safe(lambda o=obj: o.Average))})
            if args.get("export_tables", True):
                target = jd / "out" / "results" / f"{i:02d}_{kind}.txt"
                target.parent.mkdir(parents=True, exist_ok=True)
                if safe(lambda o=obj, t=target: (o.ExportToTextFile(str(t)), True)[1], False):
                    row["table"] = target.relative_to(jd).as_posix()
        rows.append(row)
    return {"analysis": an.Name, "results": rows}


def h_license_info(ns: dict, args: dict, jd: Path) -> dict:
    """Licence preference list as Mechanical sees it (read-only: nothing is reordered, enabled or reset)."""
    app = ns["app"]
    lm = safe(lambda: app.license_manager)
    names = safe(lambda: list(lm.get_all_licenses()), []) or []
    return {"readonly": safe(lambda: app.readonly), "licenses": [
        {"name": str(n), "status": str(safe(lambda n=n: lm.get_license_status(n), ""))} for n in names]}


def h_summary(ns: dict, args: dict, jd: Path) -> dict:
    budget = [int(args.get("max_nodes", 2000))]
    rep = {"unit_system": str(safe(lambda: ns["ExtAPI"].Application.ActiveUnitSystem, "")),
           "bodies": bodies_report(ns), "mesh": mesh_report(ns), "analyses": analyses_report(ns),
           "tree": tree_report(ns["Model"], 0, int(args.get("max_depth", 6)), budget)}
    if args.get("license_list"):
        rep["mechanical_licences"] = h_license_info(ns, args, jd)
    return rep


HELPERS = {"import_geometry": h_import_geometry, "mesh": h_mesh, "solve": h_solve, "results": h_results,
           "summary": h_summary, "license_info": h_license_info}


# ---------------------------------------------------------------------------------------------------- engines
def run_embedded(req: dict, jd: Path, db_file: Path | None, status: dict) -> int:
    out = jd / "out"
    result: dict = {}
    kit.progress(jd, "starting_mechanical")
    t0 = time.time()
    try:
        from ansys.mechanical.core import App

        kwargs = {"version": int(req.get("version") or 261), "readonly": bool(req.get("readonly"))}
        if req.get("private_appdata"):
            kwargs["private_appdata"] = True
        app = App(db_file=str(db_file) if db_file else None, **kwargs)
    except Exception as exc:  # noqa: BLE001
        text = f"{type(exc).__name__}: {exc}"
        lic = bool(kit.LICENCE_RX.search(text))
        status["error"] = {"code": "LICENSE_UNAVAILABLE" if lic else "APP_START_FAILED", "type": type(exc).__name__,
                           "message": str(exc)[:4000], "traceback": traceback.format_exc()[-6000:]}
        kit.status_line("LICENSE_UNAVAILABLE" if lic else "APP_START_FAILED", text)
        return kit.EXIT_LICENSE if lic else kit.EXIT_APP
    status["app"] = {"product": safe(lambda: repr(app)), "version": safe(lambda: app.version),
                     "readonly": safe(lambda: app.readonly)}
    status["app_start_s"] = round(time.time() - t0, 3)
    log(f"Mechanical started in {status['app_start_s']} s (readonly={status['app']['readonly']})")
    status["licence_after_start"] = kit.licence_probe(req.get("license_probe"))
    ns: dict = {"__name__": "__vkm_mech_script__", "__builtins__": __builtins__, "app": app, "JOB_DIR": str(jd),
                "IN_DIR": str(jd / "in"), "WORK_DIR": str(jd / "work"), "OUT_DIR": str(out), "REQUEST": req,
                "result": result, "vkm_log": log}
    code = kit.EXIT_OK
    try:
        app.update_globals(ns)
        if req.get("unit_system"):
            units = ns.get("MechanicalUnitSystem") or ns.get("MechanicalUnitSystemEnum")
            ns["ExtAPI"].Application.ActiveUnitSystem = getattr(units, req["unit_system"])
        kit.progress(jd, "running", mode=req["mode"], helper=req.get("helper"))
        t1 = time.time()
        if req["mode"] == "script":
            script = jd / req["script"]
            ns["__file__"] = str(script)
            exec(compile(script.read_text(encoding="utf-8"), str(script), "exec"), ns)  # noqa: S102 - the channel
            result = ns.get("result", result)
        else:
            result.update(HELPERS[req["helper"]](ns, req.get("args") or {}, jd))
        status["run_s"] = round(time.time() - t1, 3)
        if req.get("save_as"):
            kit.progress(jd, "saving")
            target = (jd / req["save_as"]).resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            app.save_as(str(target), overwrite=True)
            status["saved_as"] = target.relative_to(jd).as_posix()
        status["ok"] = True
    except Exception as exc:  # noqa: BLE001 - reported in entry_status.json, exit code 1 or 3
        text = f"{type(exc).__name__}: {exc}"
        lic = bool(kit.LICENCE_RX.search(text))
        code = kit.EXIT_LICENSE if lic else kit.EXIT_SCRIPT
        status["error"] = {"code": "LICENSE_UNAVAILABLE" if lic else "SCRIPT_ERROR", "type": type(exc).__name__,
                           "message": str(exc)[:4000], "traceback": traceback.format_exc()[-8000:]}
        kit.status_line("LICENSE_UNAVAILABLE" if lic else "SCRIPT_ERROR", text)
    status["messages"] = messages(ns)
    status["licence_before_exit"] = kit.licence_probe(req.get("license_probe"))
    try:
        kit.write_json(out / "result.json", result if isinstance(result, dict) else {"value": result})
    except Exception as exc:  # noqa: BLE001
        status.setdefault("error", {"code": "RESULT_NOT_SERIALIZABLE", "message": str(exc)[:1000]})
        status["ok"] = False
        code = code or kit.EXIT_SCRIPT
    kit.progress(jd, "closing")
    safe(lambda: app.close())          # new empty project: drops the project lock file
    safe(lambda: app._dispose())       # release Mechanical (and its licence) before the machine lock goes
    return code


def run_batch(req: dict, jd: Path, status: dict) -> int:
    """Mechanical in batch mode on the staged wrapper; the wrapper writes out/batch_status.json."""
    out = jd / "out"
    exe = req.get("exe")
    if not exe or not Path(exe).is_file():
        status["error"] = {"code": "APP_UNAVAILABLE", "message": "Mechanical executable not found"}
        return kit.EXIT_APP
    cmd = [exe, "-DSApplet", "-AppModeMech", "-b", "-script", str(jd / "in" / "vkm_mech_batch.py"), "-x"]
    if req.get("engine_type"):
        cmd += ["-engineType", req["engine_type"]]
    status["command"] = ["<ANSYS_ROOT>/aisol/bin/winx64/AnsysWBU.exe", *cmd[1:5], "in/vkm_mech_batch.py", *cmd[6:]]
    kit.progress(jd, "running_mechanical_batch")
    rc, seen = kit.run_product(cmd, jd / "work", jd / "logs" / "product.log", probe=req.get("license_probe"))
    status["licence_during_run"] = seen
    text = (jd / "logs" / "product.log").read_text(encoding="utf-8", errors="replace")[-400_000:]
    inner = kit.read_json(out / "batch_status.json")
    status.update({"product_exit_code": rc, "batch": inner})
    ok = bool(inner and inner.get("ok"))
    lic = not ok and bool(kit.LICENCE_RX.search(text + json.dumps((inner or {}).get("error") or {})))
    status["ok"] = ok
    status["messages"] = (inner or {}).get("messages", [])
    if not (out / "result.json").is_file():
        kit.write_json(out / "result.json", {})
    if ok:
        return kit.EXIT_OK
    status["error"] = {"code": "LICENSE_UNAVAILABLE" if lic else ("SCRIPT_ERROR" if inner else "APP_FAILED"),
                       "message": ((inner or {}).get("error") or {}).get("message")
                       or f"Mechanical exited with {rc} without a batch status"}
    kit.status_line(status["error"]["code"], status["error"]["message"] or "")
    return kit.EXIT_LICENSE if lic else (kit.EXIT_SCRIPT if inner else kit.EXIT_APP)


# ---------------------------------------------------------------------------------------------------- main
def main(argv: list[str]) -> int:
    if len(argv) != 2:
        log("usage: vkm_mech_entry.py <job_dir>")
        return kit.EXIT_REQUEST
    jd = Path(argv[1]).resolve()
    out = jd / "out"
    out.mkdir(parents=True, exist_ok=True)
    (jd / "work").mkdir(exist_ok=True)
    status: dict = {"schema": "vkm.mech_entry_status/1", "entry": TAG, "entry_version": ENTRY_VERSION, "ok": False,
                    "started_at": kit.utc(), "job_id": jd.name}
    t0 = time.time()
    try:
        req = json.loads((jd / "in" / "mech_request.json").read_text(encoding="utf-8"))
        mode, engine = req.get("mode"), req.get("engine", "embedded")
        status.update({"mode": mode, "helper": req.get("helper"), "version": req.get("version"), "engine": engine})
        if mode not in ("script", "helper") or (mode == "helper" and req.get("helper") not in HELPERS):
            raise ValueError(f"bad request mode/helper: {mode!r}/{req.get('helper')!r}")
        if engine not in ("embedded", "batch") or (engine == "batch" and mode != "script"):
            raise ValueError(f"bad engine {engine!r} for mode {mode!r}")
    except Exception as exc:  # noqa: BLE001
        status["error"] = {"code": "INVALID_ARGUMENT", "type": type(exc).__name__, "message": str(exc)[:2000]}
        kit.write_json(out / "entry_status.json", status)
        kit.status_line("INVALID_ARGUMENT", str(exc))
        return kit.EXIT_REQUEST
    os.chdir(jd / "work")
    db_file = None
    if req.get("db_file"):
        src = jd / req["db_file"]
        db_file = jd / "work" / src.name
        shutil.copy2(src, db_file)
        comp = src.with_name(src.stem + "_Mech_Files")
        if comp.is_dir():
            shutil.copytree(comp, db_file.with_name(db_file.stem + "_Mech_Files"), dirs_exist_ok=True)
    lock = kit.MachineLock(label=f"mechanical {req.get('helper') or mode}", job_id=jd.name)
    kit.progress(jd, "waiting_for_licence_lock")
    if req.get("machine_lock", True) and not lock.acquire(float(req.get("lock_timeout_s", 1800))):
        status["licence_lock"] = lock.report()
        status["error"] = {"code": "POOL_BUSY", "message": "the machine-wide ansys licence lock stayed busy"}
        kit.write_json(out / "entry_status.json", status)
        kit.status_line("POOL_BUSY", "machine-wide ansys licence lock busy")
        return kit.EXIT_LOCK
    watch = kit.Watch()
    watch.start()
    try:
        code = run_batch(req, jd, status) if engine == "batch" else run_embedded(req, jd, db_file, status)
    finally:
        status.update(watch.report())
        status["leftover_children"] = kit.wait_tree_gone(60.0 if engine == "batch" else 10.0,
                                                         pids=set(watch.pids))
        lock.release()
        status["licence_lock"] = lock.report()
        status["duration_s"] = round(time.time() - t0, 3)
        kit.write_json(out / "entry_status.json", status)
    log(f"done: ok={status.get('ok')} exit={code}")
    return code


if __name__ == "__main__":
    code = main(sys.argv)
    sys.stdout.flush()
    sys.stderr.flush()
    sys.exit(code)

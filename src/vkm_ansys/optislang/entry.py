"""vkm-ansys optiSLang job entry (CPython, venv-ansys, PyOptiSLang) — staged as ``in/vkm_osl_entry.py``.

Run by the job layer::

    <venv-ansys python> in/vkm_osl_entry.py <job_dir>

Reads ``in/osl_request.json`` (``vkm.osl_request/1``), takes the machine-wide licence lock of the ``ansys`` pool and
starts optiSLang in batch mode through PyOptiSLang (``Optislang(project_path=…, batch=True)``: a child process of this
entry, local-domain channel — no TCP port of ours). Modes:

* ``script`` — new project (or the input project) + optiSLang's own Python API script (``actors``, ``add_actor``,
  ``connect``, parameter managers …) run by ``project.run_python_file``; optionally ``project.start()``;
* ``run_project`` — open the input project and ``project.start()``;
* ``summary`` — open the input project, change nothing.

Afterwards it walks the node tree (name, type, status, states) and, for every parametric system, writes the design
table of each state to ``out/designs/<system>__<state>.json`` and ``.csv`` and a least-squares linear fit of each
response on the parameters (``linear_fit``: intercept, coefficients, R²) — a DERIVATION of MODEL_RESULT values.
``out/result.json`` holds the summary; ``out/entry_status.json`` the outcome, processes and listening sockets. Exit
codes: 0 ok, 1 script / run error, 2 bad request, 3 licence unavailable, 4 optiSLang start failure, 5 licence lock
busy.
"""
from __future__ import annotations

import csv
import json
import os
import re
import shutil
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vkm_entry_kit as kit  # noqa: E402 - staged next to this file

TAG = "vkm_osl_entry"
ENTRY_VERSION = "1"
safe = kit.safe


def log(msg: str) -> None:
    kit.log(TAG, msg)


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text)[:80] or "node"


def value(v):
    if isinstance(v, (int, float, str, bool)) or v is None:
        return v
    try:
        return float(v)
    except Exception:  # noqa: BLE001
        return str(v)


def linear_fit(rows: list[dict], params: list[str], responses: list[str]) -> dict:
    """Least squares y = b0 + Σ b_i x_i for each response over the succeeded designs (numpy)."""
    try:
        import numpy as np
    except ImportError:
        return {"available": False}
    out = {}
    use = [r for r in rows if all(isinstance(r["parameters"].get(p), (int, float)) for p in params)]
    for resp in responses:
        pts = [r for r in use if isinstance(r["responses"].get(resp), (int, float))]
        if len(pts) <= len(params) + 1:
            out[resp] = {"n": len(pts), "reason": "too few designs"}
            continue
        x = np.array([[1.0] + [float(r["parameters"][p]) for p in params] for r in pts])
        y = np.array([float(r["responses"][resp]) for r in pts])
        coef, *_ = np.linalg.lstsq(x, y, rcond=None)
        pred = x @ coef
        ss_res = float(((y - pred) ** 2).sum())
        ss_tot = float(((y - y.mean()) ** 2).sum())
        out[resp] = {"n": len(pts), "intercept": float(coef[0]),
                     "coef": {p: float(c) for p, c in zip(params, coef[1:])},
                     "r2": 1.0 - ss_res / ss_tot if ss_tot > 0 else None, "max_abs_residual": float(abs(y - pred).max())}
    return out


def design_rows(designs) -> list[dict]:
    rows = []
    for d in designs:
        rows.append({"id": value(safe(lambda d=d: d.id)), "status": str(safe(lambda d=d: d.status.name,
                                                                              safe(lambda d=d: d.status, ""))),
                     "feasible": safe(lambda d=d: d.feasibility),
                     "parameters": {v.name: value(v.value) for v in safe(lambda d=d: d.parameters, ()) or ()},
                     "responses": {v.name: value(v.value) for v in safe(lambda d=d: d.responses, ()) or ()},
                     "objectives": {v.name: value(v.value) for v in safe(lambda d=d: d.objectives, ()) or ()}})
    return rows


def walk(node, depth: int, jd: Path, export: bool, summary: dict) -> dict:
    info = {"name": safe(lambda: node.get_name()), "type": str(safe(lambda: node.type.id, safe(lambda: node.type))),
            "status": safe(lambda: node.get_status()), "uid": safe(lambda: node.uid)}
    if hasattr(node, "design_manager"):
        pm = safe(lambda: node.parameter_manager)
        rm = safe(lambda: node.response_manager)
        params = list(safe(lambda: pm.get_parameters_names(), ()) or ())
        responses = list(safe(lambda: rm.get_responses_names(), ()) or ())
        info.update({"parametric": True, "parameters": params, "responses": responses,
                     "criteria": list(safe(lambda: node.criteria_manager.get_criteria_names(), ()) or ())})
        states = {}
        for hid in safe(lambda: list(node.get_states_ids()), []) or []:
            rows = design_rows(safe(lambda h=hid: node.design_manager.get_designs(hid=h), ()) or ())
            state = {"designs": len(rows), "succeeded": sum(1 for r in rows if "SUCCEEDED" in r["status"].upper()),
                     "linear_fit": linear_fit(rows, params, responses)}
            if export and rows:
                base = jd / "out" / "designs" / f"{slug(info['name'] or 'system')}__{slug(str(hid))}"
                base.parent.mkdir(parents=True, exist_ok=True)
                kit.write_json(base.with_suffix(".json"), {"system": info["name"], "state": hid, "designs": rows})
                with open(base.with_suffix(".csv"), "w", newline="", encoding="utf-8") as fh:
                    w = csv.writer(fh)
                    w.writerow(["id", "status", "feasible", *params, *responses])
                    for r in rows:
                        w.writerow([r["id"], r["status"], r["feasible"], *[r["parameters"].get(p) for p in params],
                                    *[r["responses"].get(q) for q in responses]])
                state["table"] = base.with_suffix(".csv").relative_to(jd).as_posix()
            states[str(hid)] = state
        info["states"] = states
        summary["systems"][info["name"] or info["uid"]] = {k: info[k] for k in ("type", "status", "parameters",
                                                                                "responses", "states")}
    children = safe(lambda: list(node.get_nodes()), []) if hasattr(node, "get_nodes") else []
    if children and depth < 8:
        info["children"] = [walk(c, depth + 1, jd, export, summary) for c in children]
    return info


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        log("usage: vkm_osl_entry.py <job_dir>")
        return kit.EXIT_REQUEST
    jd = Path(argv[1]).resolve()
    out = jd / "out"
    out.mkdir(parents=True, exist_ok=True)
    (jd / "work" / "project").mkdir(parents=True, exist_ok=True)
    status: dict = {"schema": "vkm.osl_entry_status/1", "entry": TAG, "entry_version": ENTRY_VERSION, "ok": False,
                    "started_at": kit.utc(), "job_id": jd.name}
    t0 = time.time()
    try:
        req = json.loads((jd / "in" / "osl_request.json").read_text(encoding="utf-8"))
        status.update({"mode": req.get("mode"), "run": bool(req.get("run"))})
        if req.get("mode") not in ("script", "run_project", "summary"):
            raise ValueError(f"bad mode {req.get('mode')!r}")
        if req["mode"] in ("run_project", "summary") and not req.get("project_input"):
            raise ValueError(f"mode {req['mode']} needs an input project")
    except Exception as exc:  # noqa: BLE001
        status["error"] = {"code": "INVALID_ARGUMENT", "type": type(exc).__name__, "message": str(exc)[:2000]}
        kit.write_json(out / "entry_status.json", status)
        kit.status_line("INVALID_ARGUMENT", str(exc))
        return kit.EXIT_REQUEST
    os.chdir(jd / "work")
    if req.get("project_input"):
        src = jd / req["project_input"]
        project = jd / "work" / "project" / src.name
        shutil.copy2(src, project)
        data = src.with_suffix(".opd")
        if data.is_dir():
            shutil.copytree(data, project.with_suffix(".opd"), dirs_exist_ok=True)
    else:
        project = jd / "work" / "project" / "project.opf"
    lock = kit.MachineLock(label=f"optislang {req['mode']}", job_id=jd.name)
    kit.progress(jd, "waiting_for_licence_lock")
    if req.get("machine_lock", True) and not lock.acquire(float(req.get("lock_timeout_s", 1800))):
        status["licence_lock"] = lock.report()
        status["error"] = {"code": "POOL_BUSY", "message": "the machine-wide ansys licence lock stayed busy"}
        kit.write_json(out / "entry_status.json", status)
        kit.status_line("POOL_BUSY", "machine-wide ansys licence lock busy")
        return kit.EXIT_LOCK
    watch = kit.Watch()
    watch.start()
    code = kit.EXIT_OK
    result: dict = {"systems": {}}
    osl = None
    try:
        kit.progress(jd, "starting_optislang")
        try:
            from ansys.optislang.core import Optislang

            osl = Optislang(executable=req.get("executable"), project_path=str(project), batch=True,
                            ini_timeout=float(req.get("ini_timeout", 180)), loglevel="WARNING",
                            log_process_stdout=False, log_process_stderr=False)
        except Exception as exc:  # noqa: BLE001
            text = f"{type(exc).__name__}: {exc}"
            lic = bool(kit.LICENCE_RX.search(text))
            status["error"] = {"code": "LICENSE_UNAVAILABLE" if lic else "APP_START_FAILED",
                               "type": type(exc).__name__, "message": str(exc)[:4000],
                               "traceback": traceback.format_exc()[-6000:]}
            kit.status_line("LICENSE_UNAVAILABLE" if lic else "APP_START_FAILED", text)
            code = kit.EXIT_LICENSE if lic else kit.EXIT_APP
            return code
        status["optislang"] = {"version": safe(lambda: osl.osl_version_string),
                               "pyoptislang": safe(lambda: __import__("importlib.metadata").metadata.version(
                                   "ansys-optislang-core"))}
        status["licence_after_start"] = kit.licence_probe(req.get("license_probe"))
        try:
            proj = osl.application.project
            if req["mode"] == "script":
                kit.progress(jd, "running_script")
                stdout, stderr = proj.run_python_file(file_path=str(jd / req["script"]))
                (jd / "logs").mkdir(exist_ok=True)
                (jd / "logs" / "optislang_script.log").write_text(f"{stdout or ''}\n--- stderr ---\n{stderr or ''}",
                                                                  encoding="utf-8")
                status["script_output"] = {"stdout_chars": len(stdout or ""), "stderr_chars": len(stderr or ""),
                                           "log": "logs/optislang_script.log"}
            if req.get("run") and req["mode"] in ("script", "run_project"):
                kit.progress(jd, "running_project")
                t1 = time.time()
                proj.start(wait_for_started=True, wait_for_finished=True)
                status["run_s"] = round(time.time() - t1, 3)
            status["licence_during_run"] = kit.licence_probe(req.get("license_probe"))
            kit.progress(jd, "reading_results")
            result["project_status"] = safe(lambda: proj.get_status())
            result["tree"] = walk(proj.root_system, 0, jd, bool(req.get("export_designs", True)), result)
            if req.get("save_as"):
                target = (jd / req["save_as"]).resolve()
                target.parent.mkdir(parents=True, exist_ok=True)
                osl.application.save_copy(target)
                status["saved_as"] = target.relative_to(jd).as_posix()
            else:
                safe(lambda: osl.application.save())
            status["ok"] = True
        except Exception as exc:  # noqa: BLE001
            text = f"{type(exc).__name__}: {exc}"
            lic = bool(kit.LICENCE_RX.search(text))
            code = kit.EXIT_LICENSE if lic else kit.EXIT_SCRIPT
            status["error"] = {"code": "LICENSE_UNAVAILABLE" if lic else "SCRIPT_ERROR", "type": type(exc).__name__,
                               "message": str(exc)[:4000], "traceback": traceback.format_exc()[-8000:]}
            kit.status_line(status["error"]["code"], text)
    finally:
        if osl is not None:
            kit.progress(jd, "closing")
            safe(lambda: osl.dispose())
        kit.write_json(out / "result.json", result)
        status.update(watch.report())
        status["leftover_children"] = kit.wait_tree_gone(60.0, pids=set(watch.pids))
        lock.release()
        status["licence_lock"] = lock.report()
        status["duration_s"] = round(time.time() - t0, 3)
        kit.write_json(out / "entry_status.json", status)
    log(f"done: ok={status.get('ok')} exit={code}")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))

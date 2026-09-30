# encoding: utf-8
"""vkm-ansys Workbench journal wrapper: runs INSIDE Workbench (``RunWB2.exe -B -R in/vkm_wb_wrapper.wbjn``).

Staged as ``in/vkm_wb_wrapper.wbjn`` by the Workbench tools (the job directory is filled in at staging) and started by
the job entry ``in/vkm_wb_entry.py``. IronPython 2.7 (Workbench scripting): no f-strings, no pathlib. It opens the
input project (``.wbpj`` copied to ``work/project/``, or a ``.wbpz`` archive unpacked there), runs the user journal
with the Workbench scripting globals (``GetTemplate``, ``GetAllSystems``, ``Open``, ``Save``, ``Update``,
``Archive``, ``Parameters`` …) plus ``JOB_DIR``, ``IN_DIR``, ``WORK_DIR``, ``OUT_DIR``, ``REQUEST`` and a dict
``result``, or a built-in helper (``summary``, ``update``, ``archive``), then saves / archives into ``out/`` when
asked. Reports through ``out/result.json`` and ``out/wb_status.json``.
"""
import json
import os
import sys
import time
import traceback

JOB_DIR = "@@VKM_JOB_DIR@@"
IN_DIR = os.path.join(JOB_DIR, "in")
WORK_DIR = os.path.join(JOB_DIR, "work")
OUT_DIR = os.path.join(JOB_DIR, "out")


def _write(name, obj):
    data = json.dumps(obj, ensure_ascii=True, indent=2, sort_keys=True, default=str)
    if not isinstance(data, bytes):
        data = data.encode("ascii")
    handle = open(os.path.join(OUT_DIR, name), "wb")
    try:
        handle.write(data)
    finally:
        handle.close()


def _read_json(path):
    handle = open(path, "rb")
    try:
        return json.loads(handle.read().decode("utf-8"))
    finally:
        handle.close()


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:  # noqa: BLE001 - optional report fields
        return default


def _state(component):
    for attr in ("State", "Status", "ComponentState"):
        value = _safe(lambda: getattr(component, attr))
        if value is not None:
            return str(value)
    return None


def _summary():
    systems = []
    for system in _safe(lambda: list(GetAllSystems()), []) or []:  # noqa: F821 - Workbench global
        components = []
        for comp in _safe(lambda: list(system.Components), []) or []:
            components.append({"name": _safe(lambda: comp.Name), "display": _safe(lambda: comp.DisplayText),
                               "state": _state(comp)})
        systems.append({"name": _safe(lambda: system.Name), "display": _safe(lambda: system.DisplayText),
                        "type": _safe(lambda: str(system.SystemType)), "components": components})
    params = []
    for p in _safe(lambda: list(Parameters.GetAllParameters()), []) or []:  # noqa: F821
        params.append({"name": _safe(lambda: p.Name), "display": _safe(lambda: p.DisplayText),
                       "usage": _safe(lambda: str(p.Usage)), "value": _safe(lambda: str(p.Value))})
    points = []
    for dp in _safe(lambda: list(Parameters.GetAllDesignPoints()), []) or []:  # noqa: F821
        points.append({"name": _safe(lambda: dp.Name), "retained": _safe(lambda: bool(dp.Retained))})
    project = _safe(lambda: GetProjectFile(), "")  # noqa: F821
    return {"framework_version": _safe(lambda: str(GetFrameworkVersion())),  # noqa: F821
            "project_file": os.path.basename(project or ""), "systems": systems, "parameters": params,
            "design_points": points}


def _open_project(req):
    path = req.get("project")
    if not path:
        return None
    path = os.path.join(JOB_DIR, path)
    if path.lower().endswith(".wbpz"):
        target = os.path.join(WORK_DIR, "project", "project.wbpj")
        Unarchive(ArchivePath=path, ProjectPath=target, Overwrite=True)  # noqa: F821
        return target
    Open(FilePath=path)  # noqa: F821
    return path


def _main():
    status = {"schema": "vkm.wb_journal_status/1", "ok": False, "python": sys.version.split()[0],
              "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    result = {}
    t0 = time.time()
    try:
        _safe(lambda: SetScriptVersion(Version="26.1"))  # noqa: F821
        req = _read_json(os.path.join(IN_DIR, "wb_request.json"))
        status["framework_version"] = _safe(lambda: str(GetFrameworkVersion()))  # noqa: F821
        opened = _open_project(req)
        status["project_opened"] = os.path.basename(opened) if opened else None
        mode, helper = req.get("mode"), req.get("helper")
        if mode == "journal":
            script = os.path.join(JOB_DIR, req["journal"])
            handle = open(script, "rb")
            try:
                source = handle.read().decode("utf-8")
            finally:
                handle.close()
            ns = dict(globals())
            ns.update({"__name__": "__vkm_wb_journal__", "__file__": script, "JOB_DIR": JOB_DIR, "IN_DIR": IN_DIR,
                       "WORK_DIR": WORK_DIR, "OUT_DIR": OUT_DIR, "REQUEST": req, "result": result})
            exec(compile(source, script, "exec"), ns)  # noqa: S102 - the universal channel
            result = ns.get("result", result)
        elif helper == "summary":
            result = _summary()
        elif helper == "update":
            names = (req.get("args") or {}).get("systems") or []
            if names:
                for name in names:
                    GetSystem(Name=name).Update(AllDependencies=True)  # noqa: F821
            else:
                Update()  # noqa: F821
            result = _summary()
        elif helper == "archive":
            result = _summary()
        else:
            raise ValueError("unknown mode/helper: %r/%r" % (mode, helper))
        if req.get("save_as"):
            Save(FilePath=os.path.join(JOB_DIR, req["save_as"]), Overwrite=True)  # noqa: F821
            status["saved_as"] = req["save_as"]
        if req.get("archive_as"):
            if not req.get("save_as"):
                current = os.path.basename(_safe(lambda: GetProjectFile(), "") or "")  # noqa: F821
                if current.startswith("wbnew.") or not current:
                    Save(FilePath=os.path.join(WORK_DIR, "project", "project.wbpj"), Overwrite=True)  # noqa: F821
                else:
                    Save(Overwrite=True)  # noqa: F821
            Archive(FilePath=os.path.join(JOB_DIR, req["archive_as"]),  # noqa: F821
                    IncludeSkippedFiles=bool(req.get("archive_include_results", True)))
            status["archived_as"] = req["archive_as"]
        status["ok"] = True
    except Exception as exc:  # noqa: BLE001 - reported to the entry
        status["error"] = {"type": type(exc).__name__, "message": str(exc)[:4000],
                           "traceback": traceback.format_exc()[-8000:]}
    status["run_s"] = round(time.time() - t0, 3)
    try:
        _write("result.json", result if isinstance(result, dict) else {"value": result})
    except Exception as exc:  # noqa: BLE001
        status["ok"] = False
        status.setdefault("error", {"type": "RESULT_NOT_SERIALIZABLE", "message": str(exc)[:1000]})
    _write("wb_status.json", status)


_main()

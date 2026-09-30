# -*- coding: utf-8 -*-
"""vkm-ansys Mechanical batch wrapper: runs INSIDE Mechanical's own scripting engine.

    AnsysWBU.exe -DSApplet -AppModeMech -b -script in/vkm_mech_batch.py -x [-engineType cpython]

Staged into the job's ``in/`` by the Mechanical tools (the job directory is filled in at staging) and started by the
job entry ``in/vkm_mech_entry.py``. Python 2.7 (IronPython, Mechanical's default engine) and Python 3 compatible: no
f-strings, no pathlib. Runs the user script with Mechanical's globals (``ExtAPI``, ``DataModel``, ``Model``, ``Tree``,
``Graphics``, ``Ansys``, enums) plus ``JOB_DIR``, ``IN_DIR``, ``WORK_DIR``, ``OUT_DIR``, ``REQUEST`` and a dict
``result``; writes ``out/result.json`` and ``out/batch_status.json``. The entry turns them into the job status.
"""
from __future__ import print_function

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


def _product():
    try:
        import clr
        clr.AddReference("Ansys.Mechanical.Application")
        import Ansys as _A
        return str(_A.Mechanical.Application.ProductInfo.ProductInfoAsString)
    except Exception:  # noqa: BLE001
        return None


def _messages(limit=200):
    out = []
    for msg in _safe(lambda: list(ExtAPI.Application.Messages), []) or []:  # noqa: F821 - Mechanical global
        out.append({"severity": str(_safe(lambda: msg.Severity, "")).upper(),
                    "text": str(_safe(lambda: msg.DisplayString, ""))[:2000]})
        if len(out) >= limit:
            break
    return out


def _main():
    status = {"schema": "vkm.mech_batch_status/1", "ok": False, "engine": "batch",
              "python": sys.version.split()[0], "implementation": _safe(lambda: sys.implementation.name,
                                                                        _safe(lambda: sys.subversion[0], "")),
              "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    result = {}
    t0 = time.time()
    try:
        req = _read_json(os.path.join(IN_DIR, "mech_request.json"))
        status["product"] = _product()
        if req.get("db_file"):
            path = os.path.join(WORK_DIR, os.path.basename(req["db_file"]))
            ExtAPI.DataModel.Project.Open(path)  # noqa: F821
        if req.get("unit_system"):
            ExtAPI.Application.ActiveUnitSystem = getattr(MechanicalUnitSystem, req["unit_system"])  # noqa: F821
        script = os.path.join(JOB_DIR, req["script"])
        handle = open(script, "rb")
        try:
            source = handle.read().decode("utf-8")
        finally:
            handle.close()
        ns = dict(globals())
        ns.update({"__name__": "__vkm_mech_script__", "__file__": script, "JOB_DIR": JOB_DIR, "IN_DIR": IN_DIR,
                   "WORK_DIR": WORK_DIR, "OUT_DIR": OUT_DIR, "REQUEST": req, "result": result})
        exec(compile(source, script, "exec"), ns)  # noqa: S102 - the universal channel
        result = ns.get("result", result)
        if req.get("save_as"):
            target = os.path.join(JOB_DIR, req["save_as"])
            if not os.path.isdir(os.path.dirname(target)):
                os.makedirs(os.path.dirname(target))
            ExtAPI.DataModel.Project.SaveAs(target)  # noqa: F821
            status["saved_as"] = req["save_as"]
        status["ok"] = True
    except Exception as exc:  # noqa: BLE001 - reported to the entry
        status["error"] = {"type": type(exc).__name__, "message": str(exc)[:4000],
                           "traceback": traceback.format_exc()[-8000:]}
    status["messages"] = _messages()
    status["run_s"] = round(time.time() - t0, 3)
    try:
        _write("result.json", result if isinstance(result, dict) else {"value": result})
    except Exception as exc:  # noqa: BLE001
        status["ok"] = False
        status.setdefault("error", {"type": "RESULT_NOT_SERIALIZABLE", "message": str(exc)[:1000]})
    _write("batch_status.json", status)


_main()

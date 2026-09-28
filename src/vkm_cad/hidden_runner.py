"""Separate process for ``cad_exec(kind="python_com")``: a hidden AutoCAD / Civil 3D started by the bridge.

Run as ``python -m vkm_cad.hidden_runner <request.json>`` by :mod:`vkm_cad.hidden` (never imported by the MCP server
for COM work). Steps:

1. refuse when any ``acad.exe`` is already running (never touch a user session);
2. start ``acad.exe /Automation /product <ACAD|C3D> /language <locale> /nologo`` — hidden, like a COM-started server;
3. find **this** process's Application object in the Running Object Table: only entries of the AutoCAD CLSID whose
   window belongs to the started PID are accepted (no ``GetActiveObject``, no COM activation of a new server);
4. open the job's drawing copy (or a new document) — never a user document;
5. ``exec`` the user code with ``app``, ``doc``, ``civil()`` (the Civil 3D COM application ``AeccXUiLand``),
   ``run_dir``, ``out_dir``, ``result`` (a dict returned as JSON) and ``log(msg)``;
6. save to the run directory, close, quit; the parent kills the process tree on timeout.

The user code is trusted local code with the user's rights — this is a guard rail, not a sandbox.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable

ROT_TIMEOUT_S = 240


def _acad_pids() -> set[int]:
    from vkm_cad import winproc

    return {p.pid for p in winproc.snapshot() if p.image == "acad.exe"}


def find_in_rot(pid: int, clsid: str, timeout: float = ROT_TIMEOUT_S, sleep: Callable[[float], None] = time.sleep,
                clock: Callable[[], float] = time.monotonic) -> Any:
    """The Application object of the AutoCAD process ``pid`` (ROT entry of ``clsid`` whose window is in ``pid``)."""
    import pythoncom  # type: ignore[import-not-found]
    import win32com.client.dynamic  # type: ignore[import-not-found]
    import win32process  # type: ignore[import-not-found]

    deadline = clock() + timeout
    while clock() < deadline:
        rot = pythoncom.GetRunningObjectTable()
        ctx = pythoncom.CreateBindCtx(0)
        for moniker in rot.EnumRunning():
            try:
                if clsid.lower() not in moniker.GetDisplayName(ctx, None).lower():
                    continue
                disp = rot.GetObject(moniker).QueryInterface(pythoncom.IID_IDispatch)
                app = win32com.client.dynamic.Dispatch(disp)
                _tid, owner = win32process.GetWindowThreadProcessId(app.HWND)
            except pythoncom.com_error:
                continue
            if owner == pid:
                return app
        sleep(1.0)
    raise TimeoutError("the started AutoCAD did not register in the Running Object Table")


def main(request_path: str) -> int:
    req = json.loads(Path(request_path).read_text(encoding="utf-8"))
    result_path = Path(req["result_file"])
    out: dict[str, Any] = {"schema": "vkm-cad.hidden_result/1", "ok": False}
    started = time.monotonic()
    proc = None
    app = None
    try:
        if _acad_pids():
            out["error"] = {"code": "USER_SESSION_RUNNING", "message": "an AutoCAD is running; refused"}
            return 3
        import pythoncom  # type: ignore[import-not-found]

        pythoncom.CoInitialize()
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        cmd = [req["acad_exe"], "/Automation", "/product", req["product"], "/nologo"]
        if req.get("language"):
            cmd += ["/language", req["language"]]
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, creationflags=flags)
        out["acad_pid_started"] = True
        app = find_in_rot(proc.pid, req["clsid"])
        out["attach_s"] = round(time.monotonic() - started, 1)
        if req.get("input_drawing"):
            doc = app.Documents.Open(req["input_drawing"])
        else:
            doc = app.Documents.Add()
        civil_app: dict[str, Any] = {}

        def civil() -> Any:
            if "app" not in civil_app:
                civil_app["app"] = app.GetInterfaceObject(req["civil_progid"])
            return civil_app["app"]

        logs: list[str] = []
        namespace: dict[str, Any] = {"app": app, "doc": doc, "civil": civil, "run_dir": req["run_dir"],
                                     "out_dir": req["out_dir"], "result": {}, "log": logs.append,
                                     "__name__": "vkm_user_job"}
        code = Path(req["code_file"]).read_text(encoding="utf-8")
        exec(compile(code, "user_code.py", "exec"), namespace)  # noqa: S102 - the user's own job code
        out["result"] = _jsonable(namespace.get("result"))
        out["log"] = logs[-200:]
        if req.get("save_to"):
            doc.SaveAs(req["save_to"])
            out["saved"] = Path(req["save_to"]).is_file()
        doc.Close(False)
        out["ok"] = True
        return 0
    except Exception as exc:  # noqa: BLE001 - reported to the parent as JSON
        out["error"] = {"code": "PYTHON_COM_FAILED", "message": f"{type(exc).__name__}: {exc}"[:2000],
                        "traceback": traceback.format_exc()[-4000:]}
        return 2
    finally:
        if app is not None:
            try:
                app.Quit()
            except Exception:  # noqa: BLE001 - the parent kills the process tree anyway
                pass
        if proc is not None:
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                out["killed_after_quit"] = True
        out["duration_s"] = round(time.monotonic() - started, 1)
        result_path.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return json.loads(json.dumps(value, default=str))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))

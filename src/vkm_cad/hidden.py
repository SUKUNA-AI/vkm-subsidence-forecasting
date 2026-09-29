"""The hidden full instance (``cad_exec(kind="python_com")``): gated, audited, one at a time.

Why gated: a full ``acad.exe`` — even hidden and for seconds — writes the user's AutoCAD profile (window placement,
profile values, ``CurVer``/``LastLaunchedProduct``, workspace ``*.aws`` files) and lets plug-in autoloaders modify the
user's CUIX. Exploration on 28.09.2026 showed exactly that (AGENT_CAD_V1.md §5). Headless runs (``accoreconsole
/isolate``) do not. Therefore python_com runs only with ``VKM_CAD_ALLOW_HIDDEN_INSTANCE=1`` (the user's decision),
never while a user AutoCAD runs, and every run records the registry/profile audit in the receipt.

The COM work happens in a child process (:mod:`vkm_cad.hidden_runner`); this module prepares the request, watches the
process tree (timeout, dialogs) and kills it when needed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from vkm_cad import winproc
from vkm_cad.errors import ToolFailure

GATE = "VKM_CAD_ALLOW_HIDDEN_INSTANCE"


def allowed(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return (env.get(GATE) or "").strip() == "1"


def run(*, acad_exe: Path, product: str, language: str | None, clsid: str, civil_progid: str, run_dir: Path,
        out_dir: Path, code: str, input_drawing: Path | None, save_to: Path | None, timeout_s: float,
        env: Mapping[str, str] | None = None, procs: Any = winproc, popen: Callable[..., Any] = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    if not allowed(env):
        raise ToolFailure("HIDDEN_INSTANCE_NOT_ALLOWED", "python_com starts a hidden full AutoCAD, which writes the "
                          "user's AutoCAD profile; it needs the user's decision: VKM_CAD_ALLOW_HIDDEN_INSTANCE=1. "
                          "Headless kinds (scr, lisp, csharp) need no gate.")
    if any(p.image == "acad.exe" for p in procs.snapshot()):
        raise ToolFailure("USER_SESSION_RUNNING", "an AutoCAD is running; the hidden instance is refused so that "
                          "no user session is touched", retryable=True)
    code_file = run_dir / "user_code.py"
    code_file.write_text(code, encoding="utf-8")
    result_file = run_dir / "hidden_result.json"
    request = {"acad_exe": str(acad_exe), "product": product, "language": language, "clsid": clsid,
               "civil_progid": civil_progid, "run_dir": str(run_dir), "out_dir": str(out_dir),
               "code_file": str(code_file), "result_file": str(result_file),
               "input_drawing": str(input_drawing) if input_drawing else None,
               "save_to": str(save_to) if save_to else None}
    request_file = run_dir / "hidden_request.json"
    request_file.write_text(json.dumps(request, ensure_ascii=False, indent=1), encoding="utf-8")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    started = clock()
    timed_out = dialog = False
    windows: list[str] = []
    killed: list[str] = []
    with open(run_dir / "hidden_runner.log", "wb") as log:
        before = procs.filetime_now()
        proc = popen([sys.executable, "-m", "vkm_cad.hidden_runner", str(request_file)], stdout=log,
                     stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, cwd=str(run_dir), creationflags=flags)
        # only processes created after the runner belong to it (PID reuse: see vkm_cad.winproc)
        root_created = procs.creation_time(proc.pid) or (before - 20_000_000 if before else None)
        while proc.poll() is None:
            if clock() - started > timeout_s:
                timed_out = True
                break
            snap = procs.snapshot()
            members = procs.tree(proc.pid, snap, not_before=root_created)
            acad = {p.pid for p in snap if p.pid in members and p.image == "acad.exe"}
            seen = [(pid, title) for pid, title in procs.visible_windows(acad) if title]
            if seen:
                dialog = True
                windows = [title for _pid, title in seen]
                break
            sleep(1.0)
        if timed_out or dialog:
            snap = procs.snapshot()
            members = procs.tree(proc.pid, snap, not_before=root_created)
            names = {p.pid: p.image for p in snap}
            killed = [names.get(p, "?") for p in procs.kill(sorted(members, key=lambda p: p == proc.pid),
                                                              {p.pid: p.created for p in snap})]
        try:
            code_rc = proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            if procs.kill([proc.pid]):
                killed.append("python.exe")
            code_rc = None
    outcome: dict[str, Any] = {}
    if result_file.is_file():
        outcome = json.loads(result_file.read_text(encoding="utf-8"))
    record = {"exit_code": code_rc, "timed_out": timed_out, "dialog_blocked": dialog, "windows_seen": windows,
              "killed_processes": killed, "duration_s": round(clock() - started, 2), "channel": "HIDDEN_INSTANCE_COM"}
    if dialog:
        raise ToolFailure("CAD_DIALOG_BLOCKED", "the hidden AutoCAD showed a window; its processes were killed",
                          details={**record, "windows": windows})
    if timed_out:
        raise ToolFailure("CAD_RUN_TIMEOUT", "the hidden AutoCAD job did not finish in time; killed", retryable=True,
                          details=record)
    if not outcome.get("ok"):
        err = outcome.get("error") or {}
        if err.get("code") == "USER_SESSION_RUNNING":
            raise ToolFailure("USER_SESSION_RUNNING", err.get("message", "a user AutoCAD is running"), retryable=True)
        raise ToolFailure("CAD_SCRIPT_FAILED", err.get("message") or "the python_com job failed",
                          details={**record, "traceback": (err.get("traceback") or "")[-1500:]})
    return {**record, "result": outcome.get("result"), "log": outcome.get("log", []), "saved": outcome.get("saved"),
            "attach_s": outcome.get("attach_s")}

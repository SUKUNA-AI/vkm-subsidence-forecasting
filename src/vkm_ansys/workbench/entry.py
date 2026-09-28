"""vkm-ansys Workbench job entry (CPython, venv-ansys) — staged as ``in/vkm_wb_entry.py``.

Run by the job layer::

    <venv-ansys python> in/vkm_wb_entry.py <job_dir>

Reads ``in/wb_request.json`` (``vkm.wb_request/1``), copies an input project (``.wbpj`` with its ``_files`` folder, or
a ``.wbpz`` archive) to ``work/project/``, takes the machine-wide licence lock of the ``ansys`` pool, runs Workbench in
batch mode on the staged journal wrapper::

    RunWB2.exe -B -R in/vkm_wb_wrapper.wbjn

(console → ``logs/product.log``), waits until every Workbench process of the tree has exited, releases the lock and
turns ``out/wb_status.json`` (written by the wrapper) into ``out/entry_status.json`` and the exit code: 0 ok,
1 journal error, 2 bad request, 3 licence unavailable, 4 application failure, 5 licence lock busy. Workbench is not
started as a server here (no port of ours); the listening sockets of the tree are recorded all the same.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vkm_entry_kit as kit  # noqa: E402 - staged next to this file

TAG = "vkm_wb_entry"
ENTRY_VERSION = "1"


def log(msg: str) -> None:
    kit.log(TAG, msg)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        log("usage: vkm_wb_entry.py <job_dir>")
        return kit.EXIT_REQUEST
    jd = Path(argv[1]).resolve()
    out = jd / "out"
    out.mkdir(parents=True, exist_ok=True)
    (jd / "work" / "project").mkdir(parents=True, exist_ok=True)
    status: dict = {"schema": "vkm.wb_entry_status/1", "entry": TAG, "entry_version": ENTRY_VERSION, "ok": False,
                    "started_at": kit.utc(), "job_id": jd.name}
    t0 = time.time()
    try:
        req = json.loads((jd / "in" / "wb_request.json").read_text(encoding="utf-8"))
        status.update({"mode": req.get("mode"), "helper": req.get("helper")})
        if req.get("mode") not in ("journal", "helper"):
            raise ValueError(f"bad mode {req.get('mode')!r}")
        exe = req.get("exe")
        if not exe or not Path(exe).is_file():
            raise FileNotFoundError("RunWB2.exe not found")
    except Exception as exc:  # noqa: BLE001
        status["error"] = {"code": "INVALID_ARGUMENT", "type": type(exc).__name__, "message": str(exc)[:2000]}
        kit.write_json(out / "entry_status.json", status)
        kit.status_line("INVALID_ARGUMENT", str(exc))
        return kit.EXIT_REQUEST
    os.chdir(jd / "work")
    if req.get("project_input"):
        src = jd / req["project_input"]
        target = jd / "work" / "project" / src.name
        shutil.copy2(src, target)
        comp = src.with_name(src.stem + "_files")
        if comp.is_dir():
            shutil.copytree(comp, target.with_name(target.stem + "_files"), dirs_exist_ok=True)
    lock = kit.MachineLock(label=f"workbench {req.get('helper') or req.get('mode')}", job_id=jd.name)
    kit.progress(jd, "waiting_for_licence_lock")
    if req.get("machine_lock", True) and not lock.acquire(float(req.get("lock_timeout_s", 1800))):
        status["licence_lock"] = lock.report()
        status["error"] = {"code": "POOL_BUSY", "message": "the machine-wide ansys licence lock stayed busy"}
        kit.write_json(out / "entry_status.json", status)
        kit.status_line("POOL_BUSY", "machine-wide ansys licence lock busy")
        return kit.EXIT_LOCK
    watch = kit.Watch()
    watch.start()
    code = kit.EXIT_APP
    try:
        cmd = [req["exe"], "-B", "-R", str(jd / "in" / "vkm_wb_wrapper.wbjn")]
        status["command"] = ["<ANSYS_ROOT>/Framework/bin/Win64/RunWB2.exe", "-B", "-R", "in/vkm_wb_wrapper.wbjn"]
        kit.progress(jd, "running_workbench_batch")
        rc, seen = kit.run_product(cmd, jd / "work", jd / "logs" / "product.log", probe=req.get("license_probe"))
        status["product_exit_code"] = rc
        status["licence_during_run"] = seen
        watch.sample()
        # RunWB2 is a launcher: the framework process may still be finishing the journal after it returns
        status["processes_still_running_after_launcher"] = kit.wait_tree_gone(float(req.get("tree_wait_s", 600)),
                                                                              pids=set(watch.pids))
        inner = kit.read_json(out / "wb_status.json")
        status["journal"] = inner
        ok = bool(inner and inner.get("ok"))
        text = (jd / "logs" / "product.log").read_text(encoding="utf-8", errors="replace")[-400_000:]
        lic = not ok and bool(kit.LICENCE_RX.search(text + json.dumps((inner or {}).get("error") or {})))
        status["ok"] = ok
        if not (out / "result.json").is_file():
            kit.write_json(out / "result.json", {})
        if ok:
            code = kit.EXIT_OK
        else:
            status["error"] = {"code": "LICENSE_UNAVAILABLE" if lic else ("SCRIPT_ERROR" if inner else "APP_FAILED"),
                               "message": ((inner or {}).get("error") or {}).get("message")
                               or f"Workbench exited with {rc} without a journal status"}
            kit.status_line(status["error"]["code"], status["error"]["message"] or "")
            code = kit.EXIT_LICENSE if lic else (kit.EXIT_SCRIPT if inner else kit.EXIT_APP)
    finally:
        status.update(watch.report())
        status["leftover_children"] = kit.wait_tree_gone(30.0, pids=set(watch.pids))
        lock.release()
        status["licence_lock"] = lock.report()
        status["duration_s"] = round(time.time() - t0, 3)
        kit.write_json(out / "entry_status.json", status)
    log(f"done: ok={status.get('ok')} exit={code}")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))

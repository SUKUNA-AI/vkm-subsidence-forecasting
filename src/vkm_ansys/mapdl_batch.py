"""MAPDL batch runs: deck policy, command line, and the job-process wrapper ``python -m vkm_ansys.mapdl_batch <job>``.

The wrapper is the "application process" of a MAPDL job. It reads ``in/mapdl_spec.json``, takes the machine-wide
licence lock (pool ``ansys``, :mod:`vkm_ansys.licence_lock`) and holds it until MAPDL has exited, runs
``ANSYS<ver>.exe -b`` in ``work/`` (ASCII path, SMP by default, ``-np ≤ 4``), kills the whole process tree on its own
timeout, parses ``.out``/``.err`` into ``out/mapdl_summary.json``, runs the optional post-step (ladder results, DPF
extraction) and removes scratch files by a white list after success. Exit codes of the wrapper:

====  =======================================================================
0     MAPDL finished, no errors in the output, post-step done
1-69  MAPDL's own exit code (Operations Guide: 8 error/end of run, 12 /STOP, 15 fatal, 16 disk, 17 file ...)
70    post-step failed (results could not be extracted)
71    MAPDL exit code 0 but the output reports errors
73    licence pool still busy after ``lock_timeout_s``
75    licence checkout refused (text of ``.out``/``.err``) → status LICENSE_UNAVAILABLE
124   wrapper timeout (tree killed) → status TIMED_OUT
====  =======================================================================
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vkm_ansys.errors import ToolFailure

SPEC_NAME = "mapdl_spec.json"
SUMMARY_NAME = "mapdl_summary.json"
MAX_DECK_BYTES = 1_000_000
MAX_NP = 4                                            # 4 cores without HPC licences (Parallel Processing Guide)
DEFAULT_KEEP = ("rst", "db", "out", "err", "log", "mntr")
# scratch files deleted after a successful run (never anything outside work/, never a kept extension)
SCRATCH_EXT = re.compile(r"\.(full|esav|emat|osav|page|ldhi|rdb|r\d{3}|pvts|dsp|bcs|pcs|stat|lock|xml|dbb|ist|"
                         r"ldbk|elem|node|mlv|pc|pvt)$", re.IGNORECASE)
EXIT = {"ok": 0, "post_failed": 70, "errors_in_output": 71, "pool_busy": 73, "licence": 75, "timeout": 124}
_SYS = re.compile(r"(?:^|\$)\s*/SYS(?:T(?:E(?:M)?)?)?\s*(?:,|$)", re.IGNORECASE)
_ABS = re.compile(r"(?<![\w%])(?:[A-Za-z]:[\\/]|\\\\[\w.$-]+\\)")
_INPUT = re.compile(r"(?:^|\$)\s*(?:/INP(?:UT)?|\*USE|/INPUT)\s*,\s*([^,\s$]+)(?:\s*,\s*([^,\s$]*))?", re.IGNORECASE)


@dataclass
class MapdlSpec:
    deck: str = "input.inp"                          # file name in in/ (copied to work/)
    includes: list[str] = field(default_factory=list)  # further files in in/ (macros, /INPUT files)
    jobname: str = "file"
    np: int = 4
    parallel: str = "smp"                            # smp | dmp
    ram_mb: int | None = None
    license_type: str | None = None                  # -p <product feature>, default: MAPDL's own choice
    keep: list[str] = field(default_factory=lambda: list(DEFAULT_KEEP))
    allow_sys: bool = False
    timeout_s: float = 7200.0
    lock_timeout_s: float | None = None              # default: timeout_s
    post: dict[str, Any] | None = None               # {"kind": "ladder", "step": "S02", "params": {...}} | {"kind": "dpf", ...}
    label: str = ""
    job_id: str | None = None

    @classmethod
    def load(cls, path: Path) -> "MapdlSpec":
        data = json.loads(path.read_text(encoding="utf-8"))
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def dump(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                        encoding="utf-8")

    def validate(self) -> None:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,31}", self.jobname):
            raise ToolFailure("INVALID_ARGUMENT", "jobname: ASCII letter, then letters, digits, '_' (≤ 32)")
        if not 1 <= self.np <= MAX_NP:
            raise ToolFailure("INVALID_ARGUMENT", f"np must be 1..{MAX_NP} (no HPC licences are assumed)")
        if self.parallel not in ("smp", "dmp"):
            raise ToolFailure("INVALID_ARGUMENT", "parallel is 'smp' or 'dmp'")
        if self.license_type is not None and not re.fullmatch(r"[A-Za-z0-9_]{1,32}", self.license_type):
            raise ToolFailure("INVALID_ARGUMENT", "license_type is a product feature name")
        if self.ram_mb is not None and not 64 <= self.ram_mb <= 65536:
            raise ToolFailure("INVALID_ARGUMENT", "ram_mb must be 64..65536")
        for name in [self.deck, *self.includes]:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", name) or name in (".", ".."):
                raise ToolFailure("INVALID_ARGUMENT", f"file name {name!r}: ASCII letters, digits, '_', '-', '.'")
        bad_keep = [k for k in self.keep if not re.fullmatch(r"[a-z0-9]{1,8}", k)]
        if bad_keep:
            raise ToolFailure("INVALID_ARGUMENT", f"keep: file extensions only ({bad_keep})")


def scan_deck(text: str) -> dict[str, Any]:
    """Policy scan of APDL text: ``/SYS`` lines, absolute paths, ``/INPUT``/``*USE`` references (line numbers)."""
    sys_lines, abs_lines, inputs = [], [], []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("!", 1)[0]                   # APDL comment
        if _SYS.search(line):
            sys_lines.append(n)
        if _ABS.search(line):
            abs_lines.append(n)
        for m in _INPUT.finditer(line):
            inputs.append({"line": n, "file": m.group(1), "ext": m.group(2) or None})
    return {"sys_lines": sys_lines, "absolute_path_lines": abs_lines, "input_refs": inputs}


def check_deck_policy(texts: dict[str, str], allow_sys: bool) -> dict[str, Any]:
    """Refuse ``/SYS`` unless ``allow_sys``; report absolute paths as warnings (the job dir is a workspace)."""
    report = {name: scan_deck(text) for name, text in texts.items()}
    with_sys = {name: r["sys_lines"] for name, r in report.items() if r["sys_lines"]}
    if with_sys and not allow_sys:
        raise ToolFailure("INVALID_ARGUMENT", "the deck runs operating-system commands (/SYS); pass allow_sys=true "
                          "if this is intended", details={"sys_lines": with_sys})
    warnings = []
    for name, r in report.items():
        if r["absolute_path_lines"]:
            warnings.append(f"{name}: absolute paths on lines {r['absolute_path_lines'][:10]} — files outside the job "
                            "directory are not tracked in the receipt")
    return {"files": report, "warnings": warnings}


def build_argv(exe: Path, spec: MapdlSpec, work: Path) -> list[str]:
    argv = [str(exe), "-b", "-dir", str(work), "-j", spec.jobname, "-s", "noread", "-l", "en-us",
            "-np", str(spec.np), "-smp" if spec.parallel == "smp" else "-dis"]
    if spec.ram_mb:
        argv += ["-m", str(spec.ram_mb)]
    if spec.license_type:
        argv += ["-p", spec.license_type]
    argv += ["-i", spec.deck, "-o", f"{spec.jobname}.out"]
    return argv


def logical_argv(argv: list[str], exe_root: Path | None, job_dir: Path) -> list[str]:
    out = []
    for a in argv:
        a2 = a
        if exe_root is not None and a.startswith(str(exe_root)):
            a2 = "<ANSYS_ROOT>" + a[len(str(exe_root)):]
        if a.startswith(str(job_dir)):
            a2 = "<JOB_DIR>" + a[len(str(job_dir)):]
        out.append(a2.replace("\\", "/"))
    return out


# ------------------------------------------------------------------------------------------------ process tree
def _job_object():
    """A Windows Job Object that kills its processes when the wrapper's handle closes (pywin32), or None."""
    if sys.platform != "win32":
        return None
    try:
        import win32job
    except ImportError:
        return None
    job = win32job.CreateJobObject(None, "")
    info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
    info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
    return job


def _assign(job, proc: subprocess.Popen) -> bool:
    if job is None:
        return False
    try:
        import win32api
        import win32con
        import win32job
        handle = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, proc.pid)
        win32job.AssignProcessToJobObject(job, handle)
        return True
    except Exception:  # noqa: BLE001 - fall back to psutil tree kill
        return False


def kill_tree(pid: int, job=None) -> list[int]:
    killed: list[int] = []
    try:
        import psutil
        parent = psutil.Process(pid)
        procs = parent.children(recursive=True) + [parent]
        for p in procs:
            try:
                p.kill()
                killed.append(p.pid)
            except psutil.Error:
                pass
        psutil.wait_procs(procs, timeout=30)
    except Exception:  # noqa: BLE001
        pass
    if job is not None:
        try:
            import win32job
            win32job.TerminateJobObject(job, 1)
        except Exception:  # noqa: BLE001
            pass
    return killed


# ------------------------------------------------------------------------------------------------ wrapper
def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def cleanup_scratch(work: Path, keep: list[str]) -> list[dict[str, Any]]:
    removed = []
    keep_set = {k.lower() for k in keep}
    for p in sorted(work.iterdir()):
        if not p.is_file() or p.suffix.lower().lstrip(".") in keep_set or not SCRATCH_EXT.search(p.name):
            continue
        size = p.stat().st_size
        try:
            p.unlink()
            removed.append({"file": p.name, "bytes": size})
        except OSError:
            pass
    return removed


def run_job(job_dir: Path, env: dict[str, str] | None = None) -> int:
    """Run the MAPDL job in ``job_dir`` (``in/mapdl_spec.json``); returns the wrapper exit code."""
    from vkm_ansys.detect import exe_path, find_root
    from vkm_ansys.licence_lock import LicenceLock
    from vkm_ansys.mapdl_out import parse_out

    env = dict(os.environ if env is None else env)
    job_dir = job_dir.resolve()
    ind, work, out, logs = (job_dir / d for d in ("in", "work", "out", "logs"))
    for d in (work, out, logs):
        d.mkdir(parents=True, exist_ok=True)
    spec = MapdlSpec.load(ind / SPEC_NAME)
    spec.validate()
    summary: dict[str, Any] = {"schema": "vkm-ansys.mapdl_summary/1", "job_id": spec.job_id, "jobname": spec.jobname,
                               "label": spec.label, "np": spec.np, "parallel": spec.parallel}

    def finish(code: int, status_hint: str) -> int:
        summary["wrapper_exit_code"] = code
        summary["status_hint"] = status_hint
        summary["ended_at"] = _utc()
        (out / SUMMARY_NAME).write_text(json.dumps(summary, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                                        encoding="utf-8")
        return code

    texts = {name: _read(ind / name) for name in [spec.deck, *spec.includes]}
    summary["policy"] = check_deck_policy(texts, spec.allow_sys)
    for name in [spec.deck, *spec.includes]:
        shutil.copyfile(ind / name, work / name)
    root, ver, _how = find_root(env)
    if root is None:
        raise ToolFailure("APP_UNAVAILABLE", "Ansys installation not found")
    exe = exe_path(root, ver, "mapdl")
    argv = build_argv(exe, spec, work)
    summary["command"] = logical_argv(argv, root, job_dir)
    lock = LicenceLock("ansys", label=spec.label or f"mapdl {spec.jobname}", job_id=spec.job_id, env=env)
    t_lock = time.monotonic()
    summary["lock_requested_at"] = _utc()
    if not lock.acquire(spec.lock_timeout_s if spec.lock_timeout_s is not None else spec.timeout_s):
        summary["lock_holder"] = lock.holder()
        return finish(EXIT["pool_busy"], "POOL_BUSY")
    summary["lock_wait_s"] = round(time.monotonic() - t_lock, 3)
    job = _job_object()
    timed_out = False
    try:
        summary["started_at"] = _utc()
        t0 = time.monotonic()
        with open(logs / "mapdl.stdout.txt", "wb") as so, open(logs / "mapdl.stderr.txt", "wb") as se:
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            proc = subprocess.Popen(argv, cwd=work, stdin=subprocess.DEVNULL, stdout=so, stderr=se, env=env,
                                    creationflags=flags)
            summary["in_job_object"] = _assign(job, proc)
            try:
                rc = proc.wait(timeout=spec.timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                summary["killed_pids"] = len(kill_tree(proc.pid, job))
                rc = proc.wait(timeout=60)
        summary["duration_s"] = round(time.monotonic() - t0, 3)
    finally:
        if job is not None:
            try:
                import win32api
                win32api.CloseHandle(job)
            except Exception:  # noqa: BLE001
                pass
        lock.release()
    summary["mapdl_exit_code"] = rc
    out_text = _read(work / f"{spec.jobname}.out")
    err_text = _read(work / f"{spec.jobname}.err")
    summary["out"] = parse_out(out_text)
    summary["err"] = parse_out(err_text)
    if timed_out:
        return finish(EXIT["timeout"], "TIMED_OUT")
    licence = summary["out"]["licence_failure"] or summary["err"]["licence_failure"]
    if licence and (rc != 0 or summary["out"]["errors"]):
        summary["licence_message"] = licence
        return finish(EXIT["licence"], "LICENSE_UNAVAILABLE")
    if rc != 0:
        return finish(rc, "FAILED")
    if summary["out"]["errors"] or summary["err"]["errors"]:
        return finish(EXIT["errors_in_output"], "FAILED")
    if spec.post:
        try:
            summary["post"] = run_post(spec, job_dir, env)
        except Exception as exc:  # noqa: BLE001 - reported in the summary
            summary["post_error"] = f"{type(exc).__name__}: {exc}"[:1000]
            return finish(EXIT["post_failed"], "FAILED")
    summary["scratch_removed"] = cleanup_scratch(work, spec.keep)
    summary["outputs"] = sorted(p.relative_to(job_dir).as_posix() for p in work.iterdir() if p.is_file())
    return finish(EXIT["ok"], "SUCCEEDED")


def run_post(spec: MapdlSpec, job_dir: Path, env: dict[str, str]) -> dict[str, Any]:
    kind = (spec.post or {}).get("kind")
    if kind == "ladder":
        from vkm_ansys.ladder import post_process
        return post_process(spec.post["step"], job_dir, spec.post.get("params") or {}, env=env)
    if kind == "dpf":
        from vkm_ansys.dpf_job import run_extract
        return run_extract(job_dir, spec.post, env=env)
    raise ValueError(f"unknown post kind {kind!r}")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m vkm_ansys.mapdl_batch <job_dir>", file=sys.stderr)
        return 2
    return run_job(Path(args[0]))


if __name__ == "__main__":
    sys.exit(main())

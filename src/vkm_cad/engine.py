"""Headless AutoCAD / Civil 3D runs with ``accoreconsole.exe`` (Core Console), crash- and dialog-aware.

Verified on the workstation (28.09.2026; AutoCAD 2026 25.1.164, Civil 3D 2026 13.8, ru-RU, see AGENT_CAD_V1.md):

* ``/i <dwg|dxf>`` opens a drawing (without it: a new drawing from the profile template), ``/s <script>``,
  ``/product ACAD|C3D`` (C3D loads the AECC object enablers; Civil objects are reached through the .NET host),
  ``/l ru-RU``, ``/isolate <id> <folder>`` (sysvars and profile live in <folder>: the user profile is not written);
* a missing or unreadable script makes the console wait for keyboard input → stdin is always DEVNULL (it exits);
* an invalid ``(command …)`` input can crash the core (0xC0000005 in accore.dll). Crash detection relies on the exit
  code, the timeout and the run's own markers: a non-zero exit, a crash-reporter child process or a visible window of
  the job's process tree → every process of the tree is killed (orphans included), the run is CRASHED / DIALOG and
  nothing is promoted. Crash reports are never sent and CER settings are never changed.
* the job's process tree holds only processes created after the console itself (its creation time is read through
  the handle ``Popen`` keeps, so the PID cannot be reused meanwhile): Windows reuses PIDs and keeps a dead parent's
  PID in its children, and on 29.09.2026 a user's Discord — children of a long-gone process with the console's PID —
  was taken for the job's tree and killed (:mod:`vkm_cad.winproc`).

One process per run; the caller holds the engine lock (:class:`vkm_cad.jobs.EngineLock`).
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from vkm_cad import lisp, winproc
from vkm_cad.errors import ToolFailure

CRASH_REPORTERS = ("senddmp.exe", "cer_dialog.exe", "werfault.exe", "werfaultsecure.exe")
CAD_IMAGES = ("acad.exe", "accoreconsole.exe")
POLL_S = 0.25
WATCH_EVERY_S = 1.0
CLOCK_MARGIN_FT = 20_000_000             # 2 s in FILETIME units: fallback lower bound when the console's time is unread
ISOLATE_ID = "vkm-bridge"
SWITCHES = frozenset({"/i", "/s", "/product", "/l", "/isolate"})


def decode_console(data: bytes) -> str:
    """The console writes UTF-16LE when redirected (any ASCII character then carries a NUL byte)."""
    if not data:
        return ""
    if data[:2] == b"\xff\xfe" or b"\x00" in data:
        return data.decode("utf-16-le", errors="replace").lstrip("﻿")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1251", errors="replace")


@dataclass
class ConsoleRun:
    product: str                         # ACAD | C3D
    body: str                            # LISP/script body between prelude and epilogue
    run_id: str
    run_dir: Path
    input_drawing: Path | None = None    # /i (a copy inside the run directory)
    save_to: Path | None = None          # SAVEAS target inside the run directory (None: read-only run)
    netload: list[Path] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    timeout_s: float = 300.0


@dataclass
class ConsoleResult:
    exit_code: int | None
    timed_out: bool
    crashed: bool
    dialog: bool
    duration_s: float
    script_read: bool
    completed: bool                      # END marker reached
    ok: bool                             # END … OK and a clean exit
    markers: list[str]
    failures: list[str]
    console_tail: list[str]
    killed: list[str]
    windows: list[str]
    foreign_cad_processes: dict[str, int]
    command: list[str]

    def as_record(self) -> dict[str, Any]:
        return {"exit_code": self.exit_code, "timed_out": self.timed_out, "crashed": self.crashed,
                "dialog_blocked": self.dialog, "duration_s": round(self.duration_s, 2),
                "script_read": self.script_read, "completed": self.completed, "ok": self.ok,
                "markers": self.markers[:200], "failures": self.failures[:50], "killed_processes": self.killed,
                "windows_seen": self.windows[:20], "foreign_cad_processes": self.foreign_cad_processes,
                "engine_command": self.command}

    def raise_for_status(self) -> None:
        details = {"exit_code": self.exit_code, "markers": self.markers[-10:], "failures": self.failures[:10],
                   "console_tail": self.console_tail[-15:]}
        if self.crashed:
            raise ToolFailure("CAD_ENGINE_CRASHED", f"AutoCAD exited abnormally (exit code {self.exit_code}); the "
                              "job's processes were killed and nothing was promoted",
                              details={**details, "killed": self.killed})
        if self.dialog:
            raise ToolFailure("CAD_DIALOG_BLOCKED", "a window of the job's processes appeared (headless runs have no "
                              "UI); the processes were killed", details={**details, "windows": self.windows})
        if self.timed_out:
            raise ToolFailure("CAD_RUN_TIMEOUT", "the AutoCAD process did not finish in time and was killed",
                              retryable=True, details=details)
        if not self.script_read:
            raise ToolFailure("CAD_SCRIPT_NOT_READ", "the engine did not run the job script (nothing ran)",
                              details=details)
        if not self.ok:
            raise ToolFailure("CAD_SCRIPT_FAILED", "the run did not complete: " + (
                "; ".join(self.failures[:3]) if self.failures else "the script stopped before its end marker (a "
                "command left waiting for input or an aborted LISP)"), details=details)


class CoreConsole:
    """Runs one ``accoreconsole.exe`` process per :class:`ConsoleRun`."""

    def __init__(self, exe: Path, isolate_root: Path, *, locale: str | None = None,
                 popen: Callable[..., Any] = subprocess.Popen, procs: Any = winproc,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 env: dict[str, str] | None = None) -> None:
        self.exe = exe
        self.isolate_root = isolate_root
        self.locale = locale
        self._popen = popen
        self._procs = procs
        self._clock = clock
        self._sleep = sleep
        self._env = dict(os.environ if env is None else env)

    def command(self, spec: ConsoleRun, script_path: Path) -> list[str]:
        cmd = [str(self.exe)]
        if spec.input_drawing is not None:
            cmd += ["/i", str(spec.input_drawing)]
        cmd += ["/s", str(script_path), "/product", spec.product]
        if self.locale:
            cmd += ["/l", self.locale]
        isolate = self.isolate_root / spec.product.lower()
        isolate.mkdir(parents=True, exist_ok=True)
        cmd += ["/isolate", ISOLATE_ID, str(isolate)]
        return cmd

    def run(self, spec: ConsoleRun) -> ConsoleResult:
        if spec.product not in ("ACAD", "C3D"):
            raise ToolFailure("INVALID_ARGUMENT", "product is ACAD or C3D")
        if not self.exe.is_file():
            raise ToolFailure("CAD_ENGINE_UNAVAILABLE", "accoreconsole.exe was not found")
        script_path = spec.run_dir / "job.scr"
        script_path.write_bytes(lisp.encode(lisp.script(spec.run_id, spec.run_dir, spec.body, spec.save_to,
                                                        spec.netload)))
        cmd = self.command(spec, script_path)
        console_path = spec.run_dir / "console.bin"
        env = {**self._env, **spec.env}
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        started = self._clock()
        timed_out = reporter = dialog = False
        windows: list[str] = []
        foreign: dict[str, int] = {}
        killed: list[str] = []
        members: set[int] = set()
        with open(console_path, "wb") as console:
            before = self._procs.filetime_now()
            proc = self._popen(cmd, stdout=console, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                               cwd=str(spec.run_dir), env=env, creationflags=flags)
            # descendants are created after the console; its own time is exact, the clock before Popen a fallback
            root_created = self._procs.creation_time(proc.pid) or (before - CLOCK_MARGIN_FT if before else None)
            members = {proc.pid}
            last_watch = started - WATCH_EVERY_S
            while proc.poll() is None:
                now = self._clock()
                if now - started > spec.timeout_s:
                    timed_out = True
                    killed += self._kill(proc, members, root_created)
                    break
                if now - last_watch >= WATCH_EVERY_S:
                    last_watch = now
                    snap = self._procs.snapshot()
                    members |= self._procs.tree(proc.pid, snap, not_before=root_created)
                    names = {p.pid: p.image for p in snap}
                    for image in CAD_IMAGES:
                        count = sum(1 for p in snap if p.image == image and p.pid not in members)
                        if count:
                            foreign[image] = max(foreign.get(image, 0), count)
                    if any(names.get(pid) in CRASH_REPORTERS for pid in members if pid != proc.pid):
                        reporter = True
                        killed += self._kill(proc, members, root_created, snap)
                        break
                    seen = self._procs.visible_windows(members)
                    if seen:
                        dialog = True
                        windows += [f"{names.get(pid, '?')}: {title}" for pid, title in seen]
                        killed += self._kill(proc, members, root_created, snap)
                        break
                self._sleep(POLL_S)
            try:
                code = proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                killed += self._kill(proc, members, root_created)
                code = proc.wait(timeout=30)
        duration = self._clock() - started
        # orphans of the job (a child that outlived the console, e.g. a crash reporter)
        snap = self._procs.snapshot()
        family = self._procs.tree(proc.pid, snap, not_before=root_created)
        leftovers = [p for p in snap if p.pid != proc.pid and p.pid in family]
        if leftovers:
            reporter = reporter or any(p.image in CRASH_REPORTERS for p in leftovers)
            done = set(self._procs.kill([p.pid for p in leftovers], {p.pid: p.created for p in leftovers}))
            killed += [p.image for p in leftovers if p.pid in done]
        crashed = reporter or (code not in (0, None) and not timed_out and not dialog)
        text = decode_console(console_path.read_bytes())
        (spec.run_dir / "console.txt").write_text(text, encoding="utf-8")
        console_path.unlink(missing_ok=True)
        markers = read_markers(spec.run_dir / "markers.txt")
        failures = [m[5:] for m in markers if m.startswith("FAIL ")]
        failed_flag = spec.run_dir / "FAILED"
        if failed_flag.is_file():
            failures += [line for line in failed_flag.read_text(encoding="utf-8-sig", errors="replace").splitlines()
                         if line and line not in failures]
        end = [m for m in markers if m.startswith(f"END {spec.run_id}")]
        completed = bool(end)
        ok = completed and end[-1].endswith(" OK") and not crashed and not timed_out and not dialog
        tail = [self._redact(line, spec) for line in text.splitlines() if line.strip()][-40:]
        return ConsoleResult(exit_code=code, timed_out=timed_out, crashed=crashed, dialog=dialog,
                             duration_s=duration, script_read=any(m.startswith("BEGIN") for m in markers),
                             completed=completed, ok=ok, markers=markers, failures=failures, console_tail=tail,
                             killed=killed, windows=windows, foreign_cad_processes=foreign,
                             command=self.redacted(cmd, spec))

    def _redact(self, line: str, spec: ConsoleRun) -> str:
        """Console lines echo full paths: replace the run directory, the isolate root and the jobs root."""
        for base, label in ((spec.run_dir, "run:"), (self.isolate_root, "isolate:"),
                            (self.isolate_root.parent, "jobs:")):
            for form in (str(base), str(base).replace("\\", "/")):
                line = line.replace(form, label)
        return line

    def redacted(self, cmd: list[str], spec: ConsoleRun) -> list[str]:
        """The command line with logical names instead of machine paths (for receipts)."""
        out = []
        for i, arg in enumerate(cmd):
            path = Path(arg)
            if i == 0:
                out.append(path.name)
            elif arg in SWITCHES:
                out.append(arg)
            elif path.is_absolute():
                for base, label in ((spec.run_dir, "run"), (self.isolate_root, "isolate")):
                    try:
                        out.append(f"{label}/{path.relative_to(base).as_posix()}")
                        break
                    except ValueError:
                        continue
                else:
                    out.append(path.name)
            else:
                out.append(arg)
        return out

    def _kill(self, proc: Any, members: set[int], not_before: int | None,
              snap: list[Any] | None = None) -> list[str]:
        snap = snap if snap is not None else self._procs.snapshot()
        members = members | self._procs.tree(proc.pid, snap, not_before=not_before)
        names = {p.pid: p.image for p in snap}
        created = {p.pid: p.created for p in snap}
        done = list(self._procs.kill(sorted(members, key=lambda p: p == proc.pid), created))  # children first
        if proc.pid not in done and proc.poll() is None:              # the console through its own handle
            try:
                proc.kill()
                done.append(proc.pid)
            except OSError:
                pass
        return [names.get(p, "?") for p in done]


def read_markers(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
            if line.strip()]

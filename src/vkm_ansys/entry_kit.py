"""Shared machinery of the product job entries (Mechanical, Workbench, optiSLang) — staged as ``in/vkm_entry_kit.py``.

Standalone (standard library + optional ``psutil``); a job keeps a byte-exact snapshot of it next to the product entry.

* :class:`Watch` — samples the entry's process tree: child process names and listening TCP sockets classed by bind
  address (``loopback`` / ``any`` / ``other``; the address itself is not kept). Sockets of the Ansys licensing client
  are reported apart: they belong to the licensing installation, which jobs never reconfigure;
* :class:`MachineLock` — the machine-wide licence lock of the ``ansys`` pool (:mod:`vkm_ansys.licence_lock` of the
  core when importable — the same lock the MAPDL wrapper and MAPDL sessions hold): taken before a product starts and
  released after its process tree has gone;
* :func:`licence_probe` — read-only ``lmutil lmstat -a``: product features in use during the run (names and counts);
* :func:`run_product` — starts a product executable with its console output in ``logs/product.log``;
* exit codes shared by all entries and ``VKM_STATUS=<code>`` marker lines on stderr for the job layer.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

EXIT_OK, EXIT_SCRIPT, EXIT_REQUEST, EXIT_LICENSE, EXIT_APP, EXIT_LOCK = 0, 1, 2, 3, 4, 5
LICENCE_RX = re.compile(r"licen[cs]e\s+(checkout|request)\s+(failed|denied)|no\s+licen[cs]e|unable to (check ?out|obtain)"
                        r"( a)?\s+licen[cs]e|flexnet licensing error|licensing error|licen[cs]e server .*(down|not "
                        r"respond|unavailable)|could not (check ?out|obtain) (a )?licen[cs]e|"
                        r"OslServerLicensingError", re.IGNORECASE)
LICENSING_PROCESSES = {"ansyscl.exe", "ansysli_client.exe", "ansysli_server.exe", "lmgrd.exe", "ansyslmd.exe"}


def log(tag: str, msg: str) -> None:
    sys.stderr.write(f"[{tag}] {msg}\n")
    sys.stderr.flush()


def status_line(code: str, message: str = "") -> None:
    """Marker for the job layer; licence failures also read as 'licence checkout failed' for its patterns."""
    extra = " (licence checkout failed)" if code == "LICENSE_UNAVAILABLE" else ""
    sys.stderr.write(f"VKM_STATUS={code}{extra} {message[:500]}\n")
    sys.stderr.flush()


def utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def write_json(path: Path, obj) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    for attempt in range(40):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.025 * (attempt + 1))
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def progress(job_dir: Path, phase: str, **extra) -> None:
    """``work/vkm_progress.json`` — read by the runner's progress hook (``vkm_ansys.product_jobs.progress``)."""
    try:
        write_json(Path(job_dir) / "work" / "vkm_progress.json", {"phase": phase, "since": utc(), **extra})
    except OSError:
        pass


class Watch(threading.Thread):
    def __init__(self, every_s: float = 2.0):
        super().__init__(daemon=True)
        self.every_s, self.halt = every_s, threading.Event()
        self.listeners: set[tuple[str, int, str]] = set()
        self.processes: set[str] = set()
        self.pids: set[int] = set()          # every descendant seen (orphans of a launcher stay tracked)
        self.error: str | None = None

    def sample(self) -> None:
        try:
            import ipaddress

            import psutil
        except ImportError as exc:
            self.error = f"psutil unavailable: {exc}"
            return
        try:
            me = psutil.Process()
            tree = {me.pid: me.name()}
            for child in me.children(recursive=True):
                try:
                    tree[child.pid] = child.name()
                except psutil.Error:
                    continue
            self.processes.update(tree.values())
            self.pids.update(p for p in tree if p != me.pid)
            for conn in psutil.net_connections(kind="tcp"):
                if conn.status == psutil.CONN_LISTEN and conn.pid in tree and conn.laddr:
                    ip = conn.laddr.ip.split("%")[0]
                    try:
                        addr = ipaddress.ip_address(ip)
                        bind = "loopback" if addr.is_loopback else ("any" if addr.is_unspecified else "other")
                    except ValueError:
                        bind = "loopback" if ip.lower() == "localhost" else "other"
                    self.listeners.add((tree[conn.pid], conn.laddr.port, bind))
        except Exception as exc:  # noqa: BLE001 - the watch never breaks a job
            self.error = f"{type(exc).__name__}: {exc}"

    def run(self) -> None:
        while not self.halt.wait(self.every_s):
            self.sample()

    def report(self) -> dict:
        self.halt.set()
        self.sample()
        rows = [{"process": p, "port": port, "bind": bind, "loopback": bind == "loopback",
                 "licensing": p.lower() in LICENSING_PROCESSES} for p, port, bind in sorted(self.listeners)]
        return {"listeners": rows,
                "non_loopback_listeners": sum(1 for r in rows if not r["loopback"] and not r["licensing"]),
                "non_loopback_licensing_listeners": sum(1 for r in rows if not r["loopback"] and r["licensing"]),
                "processes_seen": sorted(self.processes), "watch_error": self.error}


class MachineLock:
    """The machine-wide licence lock (``vkm_ansys.licence_lock.LicenceLock``, pool ``ansys``)."""

    def __init__(self, label: str, job_id: str | None):
        self.label, self.job_id, self.lock, self.state = label, job_id, None, "NOT_TAKEN"

    def acquire(self, timeout_s: float) -> bool:
        try:
            from vkm_ansys.licence_lock import LicenceLock
        except Exception as exc:  # noqa: BLE001 - the job layer's pool still serialises jobs
            self.state = f"UNAVAILABLE:{type(exc).__name__}"
            return True
        self.lock = LicenceLock("ansys", label=self.label, job_id=self.job_id)
        t0 = time.monotonic()
        ok = self.lock.acquire(timeout_s=timeout_s)
        self.state = "HELD" if ok else "BUSY"
        self.waited_s = round(time.monotonic() - t0, 3)
        return ok

    def release(self) -> None:
        if self.lock is not None and self.state == "HELD":
            self.lock.release()
            self.state = "RELEASED"

    def report(self) -> dict:
        return {"state": self.state, "waited_s": getattr(self, "waited_s", None)}


def wait_tree_gone(timeout_s: float = 60.0, pids: set[int] | None = None) -> int:
    """Wait until this process has no children left and none of ``pids`` (descendants seen earlier, possibly
    orphaned by a launcher that exited first) is alive. Returns how many are still alive at the deadline."""
    try:
        import psutil
    except ImportError:
        return 0
    deadline = time.monotonic() + timeout_s
    while True:
        kids = [c for c in psutil.Process().children(recursive=True) if c.is_running()]
        kids = [c for c in kids if safe(lambda c=c: c.status()) != psutil.STATUS_ZOMBIE]
        alive = {c.pid for c in kids} | {p for p in (pids or set()) if psutil.pid_exists(p)}
        if not alive or time.monotonic() >= deadline:
            return len(alive)
        time.sleep(0.5)


def safe(fn, default=None):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return default


def licence_probe(cfg: dict | None) -> dict | None:
    """Read-only ``lmutil lmstat -a``: features matching ``cfg['pattern']`` that are in use now (names, counts)."""
    if not cfg:
        return None
    source = os.environ.get("ANSYSLMD_LICENSE_FILE", "").strip()
    lmutil = cfg.get("lmutil")
    if not source or not lmutil or not Path(lmutil).is_file():
        return {"queried": False}
    try:
        out = subprocess.run([lmutil, "lmstat", "-a", "-c", source], capture_output=True, text=True, timeout=60,
                             errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"queried": False, "reason": type(exc).__name__}
    rx = re.compile(cfg.get("pattern") or ".", re.IGNORECASE)
    used = []
    for line in out.splitlines():
        m = re.match(r"^Users of ([A-Za-z0-9_.\-]+):\s*\(Total of (\d+) licenses? issued;\s*Total of (\d+) licenses? "
                     r"in use\)", line.strip())
        if m and int(m.group(3)) > 0 and rx.search(m.group(1)):
            used.append({"feature": m.group(1), "in_use": int(m.group(3))})
    return {"queried": True, "at": utc(), "in_use": used}


def run_product(cmd: list[str], cwd: Path, log_path: Path, *, probe: dict | None = None,
                env: dict | None = None) -> tuple[int, dict | None]:
    """Run a product executable to completion; console output → ``log_path``. Returns (exit code, licence probe)."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    seen = None
    with open(log_path, "ab") as fh:
        proc = subprocess.Popen(cmd, cwd=str(cwd), stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        probe_at = time.monotonic() + float((probe or {}).get("delay_s", 20))
        while proc.poll() is None:
            time.sleep(0.5)
            if probe and seen is None and time.monotonic() > probe_at:
                seen = licence_probe(probe)
    return proc.returncode, seen

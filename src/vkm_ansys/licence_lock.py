"""Machine-wide licence lock of an application pool (``ansys`` by default), shared by every VKM process.

One licensed Ansys process at a time (plan §2.2, pool ``ansys`` = 1): MAPDL batch jobs and the ladder, MAPDL sessions,
Mechanical, Workbench and optiSLang runs of the product modules — in this server, in another agent's server and in
live tests — all take the same lock before they start a product and hold it until the product process has exited.

The lock is an OS byte-range lock on ``<lock dir>/<pool>.lock`` (``msvcrt.locking`` on Windows, ``fcntl.flock``
elsewhere). The operating system drops it when the holding process dies, so there are no stale locks to clean up.
The lock directory does not depend on ``VKM_SIM_ROOT`` (two servers with different simulation roots still exclude each
other): ``VKM_LOCK_DIR`` if set, else ``%LOCALAPPDATA%/vkm/locks`` (Windows) or ``~/.cache/vkm/locks``. The holder
writes ``<pool>.holder.json`` (pid, label, job or session id, since) for status reports; it is informational only.

Usage::

    with LicenceLock("ansys", label="mapdl ladder S02", job_id=job_id).hold(timeout_s=3600):
        run_mapdl()
"""
from __future__ import annotations

import json
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from vkm_ansys.detect import env_value
from vkm_ansys.errors import ToolFailure

POOLS = frozenset({"ansys", "matlab"})


def lock_dir(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    explicit = env_value(env, "VKM_LOCK_DIR")
    if explicit:
        return Path(explicit)
    if sys.platform == "win32" and env_value(env, "LOCALAPPDATA"):
        return Path(env_value(env, "LOCALAPPDATA")) / "vkm" / "locks"
    return Path.home() / ".cache" / "vkm" / "locks"


def _pid_alive(pid: int) -> bool:
    try:
        import psutil
        return psutil.pid_exists(pid)
    except ImportError:
        if sys.platform == "win32":
            return True                                      # unknown: report as alive
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


class LicenceLock:
    def __init__(self, pool: str = "ansys", *, label: str = "", job_id: str | None = None,
                 session_id: str | None = None, env: Mapping[str, str] | None = None):
        if pool not in POOLS:
            raise ValueError(f"unknown pool {pool}")
        self.pool = pool
        self.label = label
        self.job_id = job_id
        self.session_id = session_id
        self.dir = lock_dir(env)
        self.path = self.dir / f"{pool}.lock"
        self.holder_path = self.dir / f"{pool}.holder.json"
        self._fd: int | None = None

    # ---------------------------------------------------------------------------------------------- primitives
    def _try_lock(self, fd: int) -> bool:
        if sys.platform == "win32":
            import msvcrt
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False
        import fcntl
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(self, fd: int) -> None:
        if sys.platform == "win32":
            import msvcrt
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self, timeout_s: float | None = 0.0, poll_s: float = 0.5, *, record: bool = True) -> bool:
        """Take the lock; wait up to ``timeout_s`` (``None`` = forever, ``0`` = one try). True when held."""
        if self._fd is not None:
            return True
        self.dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        deadline = None if timeout_s is None else time.monotonic() + max(0.0, timeout_s)
        while True:
            if self._try_lock(fd):
                self._fd = fd
                if record:
                    self._write_holder()
                return True
            if deadline is not None and time.monotonic() >= deadline:
                os.close(fd)
                return False
            time.sleep(poll_s if deadline is None else max(0.01, min(poll_s, deadline - time.monotonic())))

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            try:
                self.holder_path.unlink(missing_ok=True)
            except OSError:
                pass
            self._unlock(self._fd)
        finally:
            os.close(self._fd)
            self._fd = None

    @contextmanager
    def hold(self, timeout_s: float | None = 0.0) -> Iterator["LicenceLock"]:
        if not self.acquire(timeout_s):
            holder = self.holder()
            raise ToolFailure("POOL_BUSY", f"licence pool '{self.pool}' is held by another process",
                              retryable=True, details={"pool": self.pool, "holder": holder})
        try:
            yield self
        finally:
            self.release()

    # ---------------------------------------------------------------------------------------------- status
    def _write_holder(self) -> None:
        info = {"pool": self.pool, "pid": os.getpid(), "label": self.label[:200], "job_id": self.job_id,
                "session_id": self.session_id, "since": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        tmp = self.holder_path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.holder_path)

    def holder(self) -> dict[str, Any] | None:
        """Who holds the pool now (``None`` when free). Reads the holder file and probes the lock itself."""
        if self._fd is not None:
            return {"pool": self.pool, "pid": os.getpid(), "label": self.label, "self": True}
        probe = LicenceLock(self.pool, env={"VKM_LOCK_DIR": str(self.dir)})
        if probe.acquire(0, record=False):
            probe._fd_release_quiet()
            return None
        try:
            info = json.loads(self.holder_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            info = {"pool": self.pool, "pid": None, "label": "unknown holder"}
        if isinstance(info.get("pid"), int):
            info["pid_alive"] = _pid_alive(info["pid"])
        return info

    def _fd_release_quiet(self) -> None:
        # a status probe: release without touching the holder file of the real holder
        if self._fd is not None:
            self._unlock(self._fd)
            os.close(self._fd)
            self._fd = None


def status(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"lock_dir_from": "VKM_LOCK_DIR" if env_value(os.environ if env is None else env,
                                                                         "VKM_LOCK_DIR") else "default"}
    for pool in sorted(POOLS):
        holder = LicenceLock(pool, env=env).holder()
        out[pool] = {"held": holder is not None, "holder": holder}
    return out

"""Licence pools: OS file locks below ``<VKM_SIM_ROOT>/locks`` (one lock file per slot) and a FIFO queue.

A slot is held with an OS byte-range lock (``msvcrt.locking`` on Windows, ``flock`` elsewhere) for the lifetime of
the runner that owns it, so a dead runner can never leave a stale slot behind: the OS releases the lock with the
process. ``<pool>.<k>.holder.json`` beside each slot names the holder (job_id, runner pid) for status reports.

Queue: each waiting runner puts a marker ``locks/queue/<pool>/<queued_ns>_<job_id>`` and tries a slot only while
fewer than *capacity* live markers are ahead of it. Markers whose runner lock (``jobs/<id>/runner.lock``) is free
belong to dead runners and are pruned.

Capacities (plan §2.2): ``ansys`` 1 (MAPDL, Mechanical, Workbench, optiSLang), ``matlab`` 1, ``dpf`` 2, ``py`` 2;
``VKM_POOL_CAPACITY_<POOL>`` overrides (all servers and runners must see the same value).
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from vkm_jobs.spec import atomic_write_json, read_json, utc_now

POOL_CAPACITY = {"ansys": 1, "matlab": 1, "dpf": 2, "py": 2}
DEFAULT_CAPACITY = 1

if os.name == "nt":
    import msvcrt
else:
    import fcntl


def pool_capacity(name: str, env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    raw = env.get(f"VKM_POOL_CAPACITY_{name.upper()}")
    if raw and raw.strip().isdigit() and int(raw) >= 1:
        return int(raw)
    return POOL_CAPACITY.get(name, DEFAULT_CAPACITY)


class FileLock:
    """Non-blocking exclusive OS lock on a lock file (released by the OS when the process dies)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fh = None

    @property
    def held(self) -> bool:
        return self._fh is not None

    def try_acquire(self) -> bool:
        if self._fh is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")          # noqa: SIM115 - kept open while the lock is held
        try:
            if os.name == "nt":
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def acquire(self, timeout_s: float, poll_s: float = 0.05) -> bool:
        deadline = time.monotonic() + timeout_s
        while True:
            if self.try_acquire():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(poll_s)

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            if os.name == "nt":
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            fh.close()


def lock_is_held(path: Path) -> bool:
    """True when another handle holds the lock (the probe takes and drops it at once when it is free)."""
    if not Path(path).exists():
        return False
    probe = FileLock(path)
    if probe.try_acquire():
        probe.release()
        return False
    return True


@dataclass
class Slot:
    pool: str
    index: int
    lock: FileLock
    holder_file: Path

    def release(self) -> None:
        self.holder_file.unlink(missing_ok=True)
        self.lock.release()


class Pool:
    def __init__(self, locks_dir: Path, jobs_dir: Path, name: str, capacity: int | None = None) -> None:
        self.locks_dir = Path(locks_dir)
        self.jobs_dir = Path(jobs_dir)
        self.name = name
        self.capacity = capacity if capacity is not None else pool_capacity(name)

    # ---------------------------------------------------------------------------------------------- slots
    def _slot_lock(self, k: int) -> Path:
        return self.locks_dir / f"{self.name}.{k}.lock"

    def _holder(self, k: int) -> Path:
        return self.locks_dir / f"{self.name}.{k}.holder.json"

    def try_acquire(self, job_id: str) -> Slot | None:
        for k in range(self.capacity):
            lock = FileLock(self._slot_lock(k))
            if lock.try_acquire():
                atomic_write_json(self._holder(k), {"job_id": job_id, "runner_pid": os.getpid(), "since": utc_now()})
                return Slot(self.name, k, lock, self._holder(k))
        return None

    def holders(self) -> list[dict[str, Any]]:
        out = []
        for k in range(self.capacity):
            if lock_is_held(self._slot_lock(k)):
                info = read_json(self._holder(k), default=None) or {}
                out.append({"slot": k, "job_id": info.get("job_id"), "since": info.get("since")})
        return out

    # ---------------------------------------------------------------------------------------------- queue
    @property
    def queue_dir(self) -> Path:
        return self.locks_dir / "queue" / self.name

    def enqueue(self, job_id: str, queued_ns: int) -> Path:
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        marker = self.queue_dir / f"{queued_ns:020d}_{job_id}"
        marker.touch()
        return marker

    def dequeue(self, job_id: str) -> None:
        if self.queue_dir.is_dir():
            for marker in self.queue_dir.glob(f"*_{job_id}"):
                marker.unlink(missing_ok=True)

    def queue(self) -> list[str]:
        """Job ids waiting for this pool, oldest first (dead runners' markers are pruned)."""
        if not self.queue_dir.is_dir():
            return []
        alive = []
        for marker in sorted(self.queue_dir.iterdir()):
            job_id = marker.name.split("_", 1)[-1]
            if lock_is_held(self.jobs_dir / job_id / "runner.lock"):
                alive.append(job_id)
            else:
                marker.unlink(missing_ok=True)
        return alive

    def position(self, job_id: str) -> int | None:
        """Number of live waiting jobs ahead of ``job_id`` (``None`` when it is not queued)."""
        queue = self.queue()
        return queue.index(job_id) if job_id in queue else None

    def describe(self) -> dict[str, Any]:
        holders = self.holders()
        return {"pool": self.name, "capacity": self.capacity, "busy": len(holders), "holders": holders,
                "queued": self.queue()}

"""Jobs of the CAD bridge v1: ``$VKM_CAD_JOBS`` or ``$VKM_WORK/cad_jobs`` → ``<job_id>/``.

Layout of a job::

    <job_id>/
      job.json          state and provenance (runs, inputs, outputs, surfaces) — the receipt is rendered from it
      receipt.json      ``vkm-cad.job_receipt/1``, rewritten after every change (logical paths only)
      in/               copies of the inputs (SHA-256 of the original before and after the copy)
      runs/R001/        one directory per run: script, request/result, console log, markers, drawing in/out
      out/              outputs (DWG, DXF, PDF, CSV, JSON), each with SHA-256 and a derivation record
      work/drawing.dwg  the job drawing — promoted from a run only when the run reached its end marker
      fallback/         pure-Python TINs (vertices + triangles) of surfaces built without Civil 3D

Rules: every file the bridge writes lies inside the job directory; originals are only read (copied with SHA-256 before
and after); one AutoCAD process at a time for the whole jobs root (``_engine.lock``; a lock whose process is gone is
stale and is taken over) — the licence is used by one job only. Receipts name files logically (``job:<id>/…``,
``$VKM_WORK/…``, ``<PUBLIC>/…``); machine paths never enter them.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from vkm_cad import __version__
from vkm_cad.errors import ToolFailure
from vkm_cad.scratch import COPY_CHUNK, _repo_root, env_value, sha256_file, utc_now

JOB_ID = re.compile(r"^CADJ-\d{8}T\d{6}Z-[0-9a-f]{8}$")
RUN_ID = re.compile(r"^R\d{3,4}$")
NAME = re.compile(r"^[A-Za-z0-9А-Яа-яЁё][A-Za-z0-9А-Яа-яЁё_.() -]{0,99}$")
RECEIPT_SCHEMA = "vkm-cad.job_receipt/1"
STATE_SCHEMA = "vkm-cad.job_state/1"
PRODUCTS = ("ACAD", "C3D")
LOCK_NAME = "_engine.lock"
MEDIA = {".dwg": "image/vnd.dwg", ".dxf": "image/vnd.dxf", ".pdf": "application/pdf", ".csv": "text/csv",
         ".json": "application/json", ".jsonl": "application/x-ndjson", ".txt": "text/plain", ".dwt": "image/vnd.dwg",
         ".scr": "text/plain", ".lsp": "text/plain", ".cs": "text/plain"}


def job_logical(job_id: str, rel: str) -> str:
    return f"job:{job_id}/{rel}"


def logical_source(path: Path, env: Mapping[str, str] | None = None) -> str:
    """A machine-independent name of an input file (never a drive letter or a home directory)."""
    env = os.environ if env is None else env
    resolved = path.resolve()
    for var in ("VKM_RESOURCES_ROOT", "VKM_DATA_ROOT", "VKM_WORK"):
        root = env_value(env, var)
        if root:
            try:
                return f"${var}/{resolved.relative_to(Path(root).resolve()).as_posix()}"
            except ValueError:
                pass
    repo = _repo_root()
    if repo is not None:
        try:
            return f"<PUBLIC>/{resolved.relative_to(repo).as_posix()}"
        except ValueError:
            pass
    return f"<external>/{resolved.name}"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259                               # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class EngineLock:
    """``<jobs root>/_engine.lock`` held while an AutoCAD process of the bridge runs (one licensed process)."""

    def __init__(self, root: Path, holder: dict[str, Any], pid_alive=_pid_alive) -> None:
        self.path = root / LOCK_NAME
        self.holder = holder
        self._pid_alive = pid_alive
        self._held = False

    def acquire(self) -> "EngineLock":
        body = json.dumps({**self.holder, "pid": os.getpid(), "since": utc_now()}, sort_keys=True).encode("utf-8")
        for _attempt in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    current = json.loads(self.path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    current = {}
                pid = int(current.get("pid") or 0)
                if pid and self._pid_alive(pid):
                    raise ToolFailure("CAD_ENGINE_BUSY", "another bridge job is running AutoCAD (one licensed process "
                                      "at a time); try again when it finishes", retryable=True,
                                      details={"job_id": current.get("job_id"), "run_id": current.get("run_id"),
                                               "since": current.get("since")})
                self.path.unlink(missing_ok=True)                  # stale: its process is gone
                continue
            with os.fdopen(fd, "wb") as fh:
                fh.write(body)
            self._held = True
            return self
        raise ToolFailure("CAD_ENGINE_BUSY", "could not take the engine lock", retryable=True)

    def release(self) -> None:
        if self._held:
            self.path.unlink(missing_ok=True)
            self._held = False

    def __enter__(self) -> "EngineLock":
        return self.acquire()

    def __exit__(self, *exc: Any) -> None:
        self.release()


class Job:
    """One job directory. State changes go through :meth:`update` (thread lock + atomic write + receipt)."""

    _locks: dict[str, threading.Lock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, store: "JobStore", job_id: str) -> None:
        self.store = store
        self.job_id = job_id
        self.path = store.require() / job_id
        with Job._locks_guard:
            self._lock = Job._locks.setdefault(job_id, threading.Lock())

    # ------------------------------------------------------------------------------------------------ paths
    def dir(self, *parts: str) -> Path:
        return self.path.joinpath(*parts)

    def logical(self, rel: str) -> str:
        return job_logical(self.job_id, rel)

    @property
    def drawing(self) -> Path:
        return self.dir("work", "drawing.dwg")

    def has_drawing(self) -> bool:
        return self.drawing.is_file()

    # ------------------------------------------------------------------------------------------------ state
    def state(self) -> dict[str, Any]:
        path = self.dir("job.json")
        if not path.is_file():
            raise ToolFailure("CAD_JOB_NOT_FOUND", f"job {self.job_id} has no state file")
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_json(self, rel: str, obj: dict[str, Any]) -> None:
        path = self.dir(rel)
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        tmp.write_bytes((json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode("utf-8"))
        os.replace(tmp, path)

    def update(self, fn) -> dict[str, Any]:
        with self._lock:
            state = self.state()
            fn(state)
            state["updated_at"] = utc_now()
            self._write_json("job.json", state)
            self._write_json("receipt.json", render_receipt(state))
            return state

    # ------------------------------------------------------------------------------------------------ runs
    def new_run(self, kind: str) -> tuple[str, Path]:
        holder: dict[str, str] = {}

        def bump(state: dict[str, Any]) -> None:
            state["run_counter"] = int(state.get("run_counter", 0)) + 1
            holder["run_id"] = f"R{state['run_counter']:03d}"
            state.setdefault("runs", []).append({"run_id": holder["run_id"], "kind": kind, "status": "STARTED",
                                                 "started_at": utc_now()})

        self.update(bump)
        run_dir = self.dir("runs", holder["run_id"])
        run_dir.mkdir(parents=True, exist_ok=False)
        return holder["run_id"], run_dir

    def finish_run(self, run_id: str, record: dict[str, Any]) -> dict[str, Any]:
        def put(state: dict[str, Any]) -> None:
            for run in state.get("runs", []):
                if run["run_id"] == run_id:
                    run.update(record)
                    run["finished_at"] = utc_now()
                    return
            raise ToolFailure("INTERNAL", f"run {run_id} not found in job state")

        return self.update(put)

    # ------------------------------------------------------------------------------------------------ files
    def copy_input(self, source: Path, name: str | None = None, env: Mapping[str, str] | None = None) -> dict[str, Any]:
        """Copy ``source`` into ``in/`` (the original is only read; SHA-256 before and after the copy)."""
        source = Path(source)
        if not source.is_file():
            raise ToolFailure("INPUT_NOT_FOUND", f"input file not found: {source.name}")
        target_name = safe_name(name or source.name)
        target = self.dir("in", target_name)
        if target.exists():
            stem, suffix = os.path.splitext(target_name)
            target = self.dir("in", f"{stem}.{secrets.token_hex(3)}{suffix}")
        before = sha256_file(source)
        with open(source, "rb") as src, open(target, "xb") as dst:
            shutil.copyfileobj(src, dst, COPY_CHUNK)
        after = sha256_file(source)
        copied = sha256_file(target)
        if not before == after == copied:
            target.unlink(missing_ok=True)
            raise ToolFailure("SOURCE_CHANGED_DURING_COPY", "the original changed while it was copied; try again",
                              retryable=True)
        entry = {"path": f"in/{target.name}", "sha256": copied, "bytes": target.stat().st_size,
                 "source": logical_source(source, env), "original_unchanged": True, "copied_at": utc_now()}
        self.update(lambda s: s.setdefault("inputs", []).append(entry))
        return entry

    def write_input_bytes(self, name: str, data: bytes, source: str) -> dict[str, Any]:
        target = self.dir("in", safe_name(name))
        if target.exists():
            stem, suffix = os.path.splitext(target.name)
            target = self.dir("in", f"{stem}.{secrets.token_hex(3)}{suffix}")
        target.write_bytes(data)
        entry = {"path": f"in/{target.name}", "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
                 "source": source, "copied_at": utc_now()}
        self.update(lambda s: s.setdefault("inputs", []).append(entry))
        return entry

    def out_path(self, name: str, overwrite: bool = False) -> Path:
        target = self.dir("out", safe_name(name))
        if target.exists() and not overwrite:
            raise ToolFailure("WOULD_OVERWRITE", f"out/{target.name} exists in {self.job_id}; pass overwrite=true")
        return target

    def register_output(self, rel: str, kind: str, derivation: dict[str, Any] | None = None,
                        run_id: str | None = None) -> dict[str, Any]:
        path = self.dir(rel)
        entry = {"path": rel, "kind": kind, "media_type": MEDIA.get(path.suffix.lower(), "application/octet-stream"),
                 "sha256": sha256_file(path), "bytes": path.stat().st_size, "run_id": run_id,
                 "created_at": utc_now(), "derivation": derivation}

        def put(state: dict[str, Any]) -> None:
            outs = [o for o in state.setdefault("outputs", []) if o["path"] != rel]
            outs.append(entry)
            state["outputs"] = outs

        self.update(put)
        return {**entry, "logical": self.logical(rel)}

    def promote_drawing(self, produced: Path, run_id: str) -> dict[str, Any]:
        """Make a run's saved drawing the job drawing (atomic replace inside the job)."""
        if not produced.is_file() or produced.stat().st_size == 0:
            raise ToolFailure("CAD_SCRIPT_FAILED", "the run did not save its drawing")
        tmp = self.dir("work", f".drawing.{secrets.token_hex(4)}.tmp")
        shutil.copyfile(produced, tmp)
        os.replace(tmp, self.drawing)
        digest = sha256_file(self.drawing)

        def put(state: dict[str, Any]) -> None:
            state["drawing"] = {"path": "work/drawing.dwg", "sha256": digest, "bytes": self.drawing.stat().st_size,
                                "from_run": run_id}

        self.update(put)
        return {"path": "work/drawing.dwg", "sha256": digest}

    def set_surface(self, name: str, info: dict[str, Any]) -> None:
        self.update(lambda s: s.setdefault("surfaces", {}).__setitem__(name, info))

    def surface(self, name: str) -> dict[str, Any]:
        found = self.state().get("surfaces", {}).get(name)
        if found is None:
            raise ToolFailure("SURFACE_NOT_FOUND", f"surface {name!r} is not known in job {self.job_id}",
                              details={"known": sorted(self.state().get("surfaces", {}))})
        return found

    def summary(self) -> dict[str, Any]:
        state = self.state()
        return {"job_id": self.job_id, "label": state.get("label"), "product": state.get("product"),
                "created_at": state.get("created_at"), "updated_at": state.get("updated_at"),
                "drawing": state.get("drawing"), "runs": [{k: r.get(k) for k in ("run_id", "kind", "status",
                                                                                    "duration_s", "error_code")}
                                                           for r in state.get("runs", [])],
                "surfaces": sorted(state.get("surfaces", {})),
                "outputs": [{"path": o["path"], "kind": o["kind"], "sha256": o["sha256"], "bytes": o["bytes"],
                             "logical": self.logical(o["path"])} for o in state.get("outputs", [])],
                "inputs": [{"path": i["path"], "sha256": i["sha256"], "source": i.get("source")}
                           for i in state.get("inputs", [])],
                "receipt": self.logical("receipt.json")}


def safe_name(name: str) -> str:
    name = name.strip()
    if not NAME.match(name) or name in (".", "..") or ":" in name or "/" in name or "\\" in name:
        raise ToolFailure("INVALID_ARGUMENT", "file names are letters, digits, '_', '-', '.', '(', ')' and spaces "
                                              "(≤ 100 characters, no path separators)")
    return name


def render_receipt(state: dict[str, Any]) -> dict[str, Any]:
    """The public face of the job state: logical paths, hashes, runs, derivations (no machine paths)."""
    return {"schema": RECEIPT_SCHEMA, "bridge_version": __version__, "job_id": state["job_id"],
            "label": state.get("label"), "product": state.get("product"), "template": state.get("template"),
            "created_at": state.get("created_at"), "updated_at": state.get("updated_at"),
            "host_role": "WORKSTATION", "engine": state.get("engine"), "drawing": state.get("drawing"),
            "inputs": state.get("inputs", []), "runs": state.get("runs", []), "outputs": state.get("outputs", []),
            "surfaces": state.get("surfaces", {}),
            "rules": {"review_status": "AUTO_EXTRACTED_UNREVIEWED", "never_input_of_extraction": True,
                      "crs_default": "UNKNOWN_CRS", "epsg": None,
                      "documents": "new documents in the job directory only; user documents are never opened"}}


@dataclass
class JobStore:
    root: Path | None
    reason: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "JobStore":
        env = os.environ if env is None else env
        explicit = env_value(env, "VKM_CAD_JOBS")
        work = env_value(env, "VKM_WORK")
        if not explicit and not work:
            return cls(None, "set VKM_CAD_JOBS or VKM_WORK")
        root = (Path(explicit) if explicit else Path(work) / "cad_jobs").expanduser().resolve()
        resources = env_value(env, "VKM_RESOURCES_ROOT")
        data = env_value(env, "VKM_DATA_ROOT")
        repo = _repo_root()
        if resources and root.is_relative_to(Path(resources).resolve()):
            return cls(None, "the jobs root lies inside VKM_RESOURCES_ROOT")
        if data and root.is_relative_to(Path(data).resolve() / "canonical"):
            return cls(None, "the jobs root lies inside the canonical data root")
        if repo and root.is_relative_to(repo) and not root.is_relative_to(repo / "work"):
            return cls(None, "the jobs root lies inside the repository outside the git-ignored work/")
        return cls(root)

    def require(self) -> Path:
        if self.root is None:
            raise ToolFailure("JOBS_UNAVAILABLE", f"jobs root unavailable: {self.reason}")
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    def create(self, *, label: str | None, product: str, template: str | None = None,
               engine: dict[str, Any] | None = None) -> Job:
        if product not in PRODUCTS:
            raise ToolFailure("INVALID_ARGUMENT", f"product is one of {PRODUCTS}")
        root = self.require()
        job_id = f"CADJ-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"
        path = root / job_id
        for sub in ("in", "runs", "out", "work", "fallback", "logs"):
            (path / sub).mkdir(parents=True, exist_ok=False)
        state = {"schema": STATE_SCHEMA, "job_id": job_id, "label": label, "product": product,
                 "template": template or "default", "created_at": utc_now(), "updated_at": utc_now(),
                 "run_counter": 0, "runs": [], "inputs": [], "outputs": [], "surfaces": {}, "drawing": None,
                 "engine": engine or {}}
        job = Job(self, job_id)
        job._write_json("job.json", state)
        job._write_json("receipt.json", render_receipt(state))
        return job

    def get(self, job_id: str) -> Job:
        if not JOB_ID.match(job_id or ""):
            raise ToolFailure("INVALID_ARGUMENT", "job_id has the form CADJ-YYYYMMDDTHHMMSSZ-xxxxxxxx")
        path = self.require() / job_id
        if not path.is_dir() or path.is_symlink() or not (path / "job.json").is_file():
            raise ToolFailure("CAD_JOB_NOT_FOUND", f"job {job_id} does not exist")
        return Job(self, job_id)

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        root = self.require()
        ids = sorted((p.name for p in root.iterdir() if p.is_dir() and JOB_ID.match(p.name)), reverse=True)
        out = []
        for job_id in ids[:limit]:
            try:
                state = Job(self, job_id).state()
            except (ToolFailure, OSError, ValueError):
                continue
            out.append({"job_id": job_id, "label": state.get("label"), "product": state.get("product"),
                        "created_at": state.get("created_at"), "runs": len(state.get("runs", [])),
                        "outputs": len(state.get("outputs", [])), "has_drawing": bool(state.get("drawing"))})
        return out

    def engine_lock(self, job_id: str, run_id: str) -> EngineLock:
        return EngineLock(self.require(), {"job_id": job_id, "run_id": run_id})

    def shared_dir(self, *parts: str) -> Path:
        """``_isolated/…`` (AutoCAD user profiles of the bridge) and ``_plugins/…`` (compiled .NET hosts)."""
        path = self.require().joinpath(*parts)
        path.mkdir(parents=True, exist_ok=True)
        return path

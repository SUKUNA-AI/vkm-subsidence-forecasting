"""Server-side job API shared by ``vkm-matlab`` and ``vkm-ansys`` (plain Python; the MCP layer only wraps it).

Typical use by an application server::

    jobs = JobsService.from_env(logical_roots={"<MATLAB_ROOT>": matlab_root})
    draft = jobs.draft("MATLAB")                       # job id + jobs/<id>/{in,work,out,logs}
    draft.copy_input(path_to_csv)                      # → in/<name>, SHA-256 recorded
    draft.write_input("payload.json", text)            # generated input, SHA-256 recorded
    spec = draft.spec(kind="matlab_run", pool="matlab", argv=[...], timeout_s=1800, checks=[...])
    ref = jobs.submit(draft, spec, wait_s=0)           # detached runner started → JobRef

Queries: :meth:`status` (detects ``LOST`` runners), :meth:`list`, :meth:`wait`, :meth:`read` (jail + grep),
:meth:`receipt`, :meth:`cancel` (``cancel.request``; the runner kills the tree), :meth:`publish_receipt` (sanitised
copy into the PUBLIC tree after the hygiene and leakage checks).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from vkm_jobs.errors import ToolFailure
from vkm_jobs.pools import Pool, lock_is_held, pool_capacity
from vkm_jobs.procs import spawn_detached
from vkm_jobs.receipt import build_receipt, hash_files, job_files, log_summary, sha256_file
from vkm_jobs.redact import Redactor, public_text_problems
from vkm_jobs.roots import SimRoot, clean_relpath, repo_root, resolve_inside
from vkm_jobs.spec import (ACTIVE, JOB_ID, STATUS_SCHEMA, TERMINAL, JobSpec, atomic_write_json, check_job_id,
                           new_job_id, read_json, utc_now, validate_check, validate_param)

PUBLISH_DIR = "docs/engineering_tools/receipts"
PUBLISH_NAME = re.compile(r"^[a-z0-9_]{1,80}$")
SPAWN_GRACE_S = 60.0          # a runner that has not taken its lock this long after the spawn is LOST
MAX_READ_CHARS = 20000
MAX_REPLY_CHARS = 20000
MAX_INPUT_FILES = 10000
LIST_LIMIT = 200
JOB_SUBDIRS = ("in", "work", "out", "logs")


def _age_s(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()
    except ValueError:
        return None


def git_info(repo: Path | None, scope: list[str]) -> dict[str, Any]:
    """``{commit, dirty, scope}`` of the PUBLIC clone; ``dirty`` is limited to the snapshot paths in ``scope``."""
    out: dict[str, Any] = {"commit": None, "dirty": None, "scope": scope}
    if repo is None or not (Path(repo) / ".git").exists():
        return out
    try:
        commit = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True,
                                timeout=20, check=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--", *scope], capture_output=True,
                               text=True, timeout=30, check=True).stdout.strip()
        out.update(commit=commit or None, dirty=bool(dirty))
    except (OSError, subprocess.SubprocessError):
        pass
    return out


class JobDraft:
    """A job directory being prepared: inputs are copied into ``in/`` with their SHA-256."""

    def __init__(self, service: "JobsService", app: str) -> None:
        self.service = service
        self.app = app
        self.job_id = new_job_id(app)
        self.dir = service.jobs_dir / self.job_id
        for sub in JOB_SUBDIRS:
            (self.dir / sub).mkdir(parents=True, exist_ok=False)
        self.inputs: list[dict[str, Any]] = []

    def _record(self, rel: str, source: str) -> dict[str, Any]:
        path = self.dir / rel
        entry = {"path": rel, "bytes": path.stat().st_size, "sha256": sha256_file(path), "source": source}
        self.inputs.append(entry)
        return entry

    def write_input(self, rel: str, data: str | bytes, *, source: str = "generated") -> dict[str, Any]:
        rel = "in/" + clean_relpath(rel, what="input name")
        path = self.dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
        return self._record(rel, source)

    def copy_input(self, source: Path, name: str | None = None) -> list[dict[str, Any]]:
        """Copy a file or a directory tree into ``in/<name>`` (hash of the original before and after for files)."""
        source = Path(source)
        if not source.exists():
            raise ToolFailure("NOT_FOUND", "input not found", details={"input": self.service.redactor.text(str(source))})
        rel = "in/" + clean_relpath(name or source.name, what="input name")
        logical = self.service.redactor.text(str(source.resolve()))
        target = self.dir / rel
        if source.is_dir():
            files = [p for p in sorted(source.rglob("*")) if p.is_file() and not p.is_symlink()
                     and "__pycache__" not in p.parts and not any(part.startswith(".") for part in
                                                                   p.relative_to(source).parts)]
            if len(files) > MAX_INPUT_FILES:
                raise ToolFailure("INVALID_ARGUMENT", f"input directory has more than {MAX_INPUT_FILES} files")
            out = []
            for f in files:
                sub = f.relative_to(source).as_posix()
                (target / sub).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(f, target / sub)
                out.append(self._record(f"{rel}/{sub}", f"{logical}/{sub}".replace("\\", "/")))
            return out
        before = sha256_file(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        if sha256_file(source) != before or sha256_file(target) != before:
            target.unlink(missing_ok=True)
            raise ToolFailure("INVALID_ARGUMENT", "the input changed while it was copied; try again", retryable=True)
        return [self._record(rel, logical.replace("\\", "/"))]

    def spec(self, **fields: Any) -> JobSpec:
        """The job spec; checks and params are validated here (``INVALID_ARGUMENT`` before anything runs)."""
        fields.setdefault("logical_roots", self.service.logical_roots)
        fields.setdefault("inputs", self.inputs)
        fields["checks"] = [validate_check(c) for c in fields.get("checks") or []]
        fields["params"] = [validate_param(p) for p in fields.get("params") or []]
        return JobSpec(job_id=self.job_id, app=self.app, **fields).validate()

    def discard(self) -> None:
        """Remove a draft that will not be submitted (argument errors found after the directory was made)."""
        shutil.rmtree(self.dir, ignore_errors=True)


class JobsService:
    def __init__(self, root: SimRoot, *, env: Mapping[str, str] | None = None, python: str | None = None,
                 public_root: Path | None = None, publish_dir: str = PUBLISH_DIR,
                 logical_roots: Mapping[str, str] | None = None,
                 spawner: Callable[[list[str], Path, dict[str, str], Path], tuple[int, str]] | None = None) -> None:
        self.root = root
        self.env = dict(os.environ if env is None else env)
        self.python = python or sys.executable
        self.public_root = public_root if public_root is not None else repo_root(self.env)
        self.publish_dir = publish_dir
        roots: dict[str, str] = {}
        if root.path is not None:
            roots["<VKM_SIM_ROOT>"] = str(root.path)
        if self.public_root is not None:
            roots["<PUBLIC>"] = str(self.public_root)
        roots.update({k: str(v) for k, v in (logical_roots or {}).items() if v})
        self.logical_roots = roots
        self.redactor = Redactor(roots, env=self.env)
        self._spawner = spawner or (lambda argv, cwd, env, log: spawn_detached(argv, cwd=cwd, env=env,
                                                                                log_path=log))

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **kw: Any) -> "JobsService":
        return cls(SimRoot.from_env(env), env=env, **kw)

    # ------------------------------------------------------------------------------------------- creation
    @property
    def jobs_dir(self) -> Path:
        return self.root.jobs

    def draft(self, app: str) -> JobDraft:
        return JobDraft(self, app)

    def runner_env(self) -> dict[str, str]:
        env = dict(self.env)
        pkg_parent = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = os.pathsep.join([pkg_parent] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
        env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        return env

    def submit(self, draft: JobDraft, spec: JobSpec, *, wait_s: float = 0) -> dict[str, Any]:
        spec.validate()
        atomic_write_json(draft.dir / "job.json", spec.to_dict())
        atomic_write_json(draft.dir / "status.json", {
            "schema": STATUS_SCHEMA, "job_id": spec.job_id, "status": "QUEUED", "app": spec.app, "kind": spec.kind,
            "label": spec.label, "pool": spec.pool, "queued_at": utc_now(), "updated_at": utc_now(),
            "queued_ns": time.time_ns()})                       # FIFO order = submission order, not runner start
        argv = [self.python, "-m", "vkm_jobs.runner", str(draft.dir)]
        pid, mode = self._spawner(argv, draft.dir, self.runner_env(), draft.dir / "logs" / "runner.log")
        atomic_write_json(draft.dir / "spawn.json", {"spawned_at": utc_now(), "detached": mode, "spawn_pid": pid})
        if wait_s and wait_s > 0:
            self.wait(spec.job_id, wait_s)
        return self.ref(spec.job_id)

    # ------------------------------------------------------------------------------------------- queries
    def job_dir(self, job_id: str) -> Path:
        check_job_id(job_id)
        path = self.jobs_dir / job_id
        if not (path / "job.json").is_file():
            raise ToolFailure("NOT_FOUND", f"job {job_id} does not exist")
        return path

    def status(self, job_id: str) -> dict[str, Any]:
        path = self.job_dir(job_id)
        st = read_json(path / "status.json", default={}) or {}
        if st.get("status") in ACTIVE and not lock_is_held(path / "runner.lock"):
            spawn = read_json(path / "spawn.json", default={}) or {}
            started = bool((st.get("runner") or {}).get("pid"))
            age = _age_s(spawn.get("spawned_at") or st.get("queued_at"))
            if started or age is None or age > SPAWN_GRACE_S:
                st = read_json(path / "status.json", default={}) or {}
                if st.get("status") in ACTIVE and not lock_is_held(path / "runner.lock"):
                    st = self._mark_lost(path, st)
        if st.get("status") == "QUEUED":
            st["queue_position"] = Pool(self.root.locks, self.jobs_dir, st.get("pool") or "py").position(job_id)
        return st

    def _mark_lost(self, path: Path, st: dict[str, Any]) -> dict[str, Any]:
        st.update(status="LOST", reason="the runner process vanished (no runner lock); see logs/runner.log",
                  ended_at=utc_now(), updated_at=utc_now())
        spec = JobSpec.from_dict(read_json(path / "job.json"))
        Pool(self.root.locks, self.jobs_dir, spec.pool).dequeue(spec.job_id)
        if not (path / "receipt.json").exists():
            redactor = Redactor(spec.logical_roots, env=self.env)
            outputs = hash_files(path, job_files(path, spec.outputs))
            receipt = build_receipt(spec, st, job_dir=path, redactor=redactor, outputs=outputs, checks=[],
                                    logs=log_summary(path, spec, redactor), runner={"detached": None},
                                    tree={}, scratch={})
            atomic_write_json(path / "receipt.json", receipt)
            st["receipt"] = "receipt.json"
        atomic_write_json(path / "status.json", st)
        return st

    def ref(self, job_id: str, st: dict[str, Any] | None = None) -> dict[str, Any]:
        st = st or self.status(job_id)
        out = {"job_id": job_id, "status": st.get("status"), "app": st.get("app"), "kind": st.get("kind"),
               "label": st.get("label"), "pool": st.get("pool"), "job_dir": f"<VKM_SIM_ROOT>/jobs/{job_id}"}
        for key in ("queue_position", "gated", "exit_code", "reason", "queued_at", "started_at", "ended_at",
                    "duration_s", "progress", "checks_passed"):
            if st.get(key) is not None:
                out[key] = st[key]
        if st.get("status") in TERMINAL:
            receipt = read_json(self.jobs_dir / job_id / "receipt.json", default=None)
            if receipt:
                out["receipt"] = compact_receipt(receipt)
        return self.redactor.obj(out)

    def list(self, app: str | None = None, status: str | None = None, limit: int = 50,
             cursor: str | None = None) -> dict[str, Any]:
        if not 1 <= limit <= LIST_LIMIT:
            raise ToolFailure("INVALID_ARGUMENT", f"limit must be 1…{LIST_LIMIT}")
        if cursor is not None:
            check_job_id(cursor)
        names = [p.name for p in self.jobs_dir.iterdir() if p.is_dir() and JOB_ID.match(p.name)]
        names.sort(key=lambda n: (n.split("-")[1], n), reverse=True)            # newest first
        if cursor is not None:
            key = (cursor.split("-")[1], cursor)
            names = [n for n in names if (n.split("-")[1], n) < key]
        items: list[dict[str, Any]] = []
        next_cursor = None
        for i, name in enumerate(names):
            if app and not name.startswith(app + "-"):
                continue
            try:
                st = self.status(name)
            except ToolFailure:
                continue
            if status and st.get("status") != status:
                continue
            items.append({k: st.get(k) for k in ("job_id", "status", "app", "kind", "label", "pool", "queued_at",
                                                 "ended_at", "exit_code") if st.get(k) is not None})
            if len(items) >= limit:
                next_cursor = name if i + 1 < len(names) else None
                break
        return {"jobs": self.redactor.obj(items), "count": len(items), "next_cursor": next_cursor}

    def wait(self, job_id: str, timeout_s: float, on_progress: Callable[[dict[str, Any]], None] | None = None,
             poll_s: float = 0.5, progress_every_s: float = 5.0) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.0, timeout_s)
        last = 0.0
        while True:
            st = self.status(job_id)
            if st.get("status") in TERMINAL or time.monotonic() >= deadline:
                return self.ref(job_id, st)
            if on_progress and time.monotonic() - last >= progress_every_s:
                last = time.monotonic()
                on_progress(st)
            time.sleep(min(poll_s, max(0.01, deadline - time.monotonic())))

    def receipt(self, job_id: str) -> dict[str, Any]:
        path = self.job_dir(job_id)
        st = self.status(job_id)
        receipt = read_json(path / "receipt.json", default=None)
        if receipt is None:
            raise ToolFailure("NOT_FOUND", f"job {job_id} has no receipt yet (status {st.get('status')})",
                              retryable=st.get("status") in ACTIVE)
        return receipt

    def read(self, job_id: str, path: str, offset: int = 0, max_chars: int = MAX_READ_CHARS,
             grep: str | None = None) -> dict[str, Any]:
        base = self.job_dir(job_id)
        rel = clean_relpath(path, what="path")
        if rel in ("job.json", "spawn.json"):
            raise ToolFailure("INVALID_ARGUMENT", f"{rel} is internal to the runner; read status or receipt instead")
        target = resolve_inside(base, rel, what="path")
        if not target.is_file():
            raise ToolFailure("NOT_FOUND", f"{rel} is not a file of job {job_id}")
        if not 1 <= max_chars <= MAX_READ_CHARS:
            raise ToolFailure("INVALID_ARGUMENT", f"max_chars must be 1…{MAX_READ_CHARS}")
        size = target.stat().st_size
        if offset < 0 or offset > size:
            raise ToolFailure("INVALID_ARGUMENT", f"offset must be 0…{size} (bytes)")
        out: dict[str, Any] = {"job_id": job_id, "path": rel, "bytes": size, "offset": offset}
        with open(target, "rb") as fh:
            head = fh.read(8192)
            if b"\x00" in head:
                out.update(binary=True, sha256=sha256_file(target))
                return out
            fh.seek(offset)
            if grep is None:
                data = _trim_utf8(fh.read(max_chars))
                out.update(text=self.redactor.text(data.decode("utf-8", errors="replace")),
                           next_offset=offset + len(data), eof=offset + len(data) >= size)
                return out
            try:
                rx = re.compile(grep)
            except re.error as exc:
                raise ToolFailure("INVALID_ARGUMENT", f"grep is not a valid regular expression: {exc}") from exc
            matches, used, pos, truncated = [], 0, offset, False
            for raw in fh:
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if rx.search(line):
                    text = self.redactor.text(line)[:2000]
                    if used + len(text) > max_chars:
                        truncated = True
                        break
                    matches.append({"offset": pos, "text": text})
                    used += len(text)
                pos += len(raw)
            out.update(grep=grep, matches=matches, count=len(matches), next_offset=pos,
                       eof=not truncated and pos >= size)
            return out

    def cancel(self, job_id: str, reason: str, wait_s: float = 20.0) -> dict[str, Any]:
        if not isinstance(reason, str) or len(reason.strip()) < 10:
            raise ToolFailure("INVALID_ARGUMENT", "reason must have at least 10 characters")
        path = self.job_dir(job_id)
        st = self.status(job_id)
        if st.get("status") in TERMINAL:
            return {**self.ref(job_id, st), "already_terminal": True}
        atomic_write_json(path / "cancel.request", {"reason": reason.strip()[:500], "requested_at": utc_now()})
        return {**self.wait(job_id, wait_s, poll_s=0.2), "cancel_requested": True}

    # ------------------------------------------------------------------------------------------- publishing
    def publish_receipt(self, job_id: str, name: str, overwrite: bool = False) -> dict[str, Any]:
        if not isinstance(name, str) or not PUBLISH_NAME.match(name):
            raise ToolFailure("INVALID_ARGUMENT", "name is [a-z0-9_]+ (≤ 80 characters)")
        receipt = self.receipt(job_id)
        if self.public_root is None:
            raise ToolFailure("PUBLISH_BLOCKED", "the PUBLIC clone was not found (set VKM_PUBLIC_ROOT)")
        rel = f"{self.publish_dir}/{name}.json"
        target = Path(self.public_root) / rel
        if target.exists() and not overwrite:
            raise ToolFailure("WOULD_OVERWRITE", f"{rel} exists; pass overwrite=true")
        data = self.redactor.obj(receipt)
        text = json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
        problems = public_text_problems(text)
        if problems:
            raise ToolFailure("PUBLISH_BLOCKED", "the receipt fails the public hygiene checks",
                              details={"problems": problems[:20]})
        target.parent.mkdir(parents=True, exist_ok=True)
        previous = target.read_bytes() if target.exists() else None
        target.write_bytes(text.encode("utf-8"))
        from vkm_world.governance.leakage import scan

        leaks = scan(self.public_root, files=[target])
        if leaks:
            if previous is None:
                target.unlink(missing_ok=True)
            else:
                target.write_bytes(previous)
            raise ToolFailure("PUBLISH_BLOCKED", "the receipt fails the leakage scan", details={"problems": leaks})
        return {"job_id": job_id, "path": rel, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "bytes": len(text.encode("utf-8")), "leakage_scan": "PASS", "overwritten": previous is not None}

    def pools(self) -> list[dict[str, Any]]:
        names = sorted({p.name.split(".")[0] for p in self.root.locks.glob("*.lock")} | {"matlab", "ansys", "dpf"})
        return [Pool(self.root.locks, self.jobs_dir, n, pool_capacity(n, self.env)).describe() for n in names]


def _trim_utf8(data: bytes) -> bytes:
    """Drop a trailing incomplete UTF-8 sequence (a chunk boundary must not split a character)."""
    for back in range(1, min(4, len(data)) + 1):
        byte = data[-back]
        if byte < 0x80:
            return data
        if byte >= 0xC0:                          # lead byte: complete only if the sequence fits
            need = 2 if byte < 0xE0 else 3 if byte < 0xF0 else 4
            return data if back >= need else data[:-back]
    return data


def compact_receipt(receipt: dict[str, Any], limit: int = MAX_REPLY_CHARS // 2) -> dict[str, Any]:
    """The receipt for a tool reply: full when small, otherwise with long lists cut (``job_receipt`` has it all)."""
    if len(json.dumps(receipt, ensure_ascii=False)) <= limit:
        return receipt
    out = dict(receipt)
    for key in ("outputs", "inputs"):
        items = out.get(key) or []
        if len(items) > 20:
            out[key] = items[:20] + [{"truncated": len(items) - 20}]
    out.pop("meta", None)
    return out

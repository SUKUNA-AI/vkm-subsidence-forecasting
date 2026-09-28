"""Detached job runner: ``python -m vkm_jobs.runner <job_dir>``.

One runner per job, started by :meth:`vkm_jobs.service.JobsService.submit` outside the MCP server's process tree. It
holds ``runner.lock`` for its whole life (the server detects a dead runner — ``LOST`` — by that lock), waits in the
pool queue, starts the application in a :class:`~vkm_jobs.procs.ProcessTree` (Windows Job Object), enforces the
timeout and ``cancel.request``, writes ``status.json`` on every transition and a heartbeat, evaluates the checks,
hashes the outputs and writes ``receipt.json``.

Statuses: ``QUEUED → RUNNING → SUCCEEDED | FAILED | CHECK_FAILED | TIMED_OUT | CANCELLED | LICENSE_UNAVAILABLE``
(``LOST`` is set by the server when the runner vanished).
"""
from __future__ import annotations

import glob
import importlib
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from vkm_jobs.checks import run_checks
from vkm_jobs.pools import FileLock, Pool
from vkm_jobs.procs import ProcessTree, pid_alive, process_image
from vkm_jobs.receipt import build_receipt, hash_files, job_files, log_summary
from vkm_jobs.redact import Redactor
from vkm_jobs.spec import STATUS_SCHEMA, TERMINAL, JobSpec, atomic_write_json, read_json, utc_now

POLL_S = 0.25
QUEUE_POLL_S = 1.0
HEARTBEAT_S = 5.0
KILL_WAIT_S = 20.0


class Runner:
    def __init__(self, job_dir: Path) -> None:
        self.job_dir = Path(job_dir).resolve()
        self.root = self.job_dir.parent.parent
        self.spec = JobSpec.from_dict(read_json(self.job_dir / "job.json"))
        self.redactor = Redactor(self.spec.logical_roots or {"<VKM_SIM_ROOT>": str(self.root)})
        self.status: dict[str, Any] = read_json(self.job_dir / "status.json", default={}) or {}
        self.lock = FileLock(self.job_dir / "runner.lock")
        self.tree: ProcessTree | None = None

    # ----------------------------------------------------------------------------------------- bookkeeping
    def _write(self, **changes: Any) -> None:
        self.status.update(changes)
        self.status.update(schema=STATUS_SCHEMA, job_id=self.spec.job_id, updated_at=utc_now())
        atomic_write_json(self.job_dir / "status.json", self.status)

    def _cancel_reason(self) -> str | None:
        req = self.job_dir / "cancel.request"
        if not req.exists():
            return None
        data = read_json(req, default={}) or {}
        return str(data.get("reason") or "cancel requested")[:500]

    def _gate_blocked(self) -> bool:
        gate = self.spec.gate or {}
        for pattern in gate.get("pid_files", []):
            for path in glob.glob(str(self.root / pattern)):
                info = read_json(Path(path), default={}) or {}
                pid = info.get("pid")
                if isinstance(pid, int) and pid_alive(pid):
                    image = process_image(pid) or ""
                    if not gate.get("image") or image.lower() == str(gate["image"]).lower():
                        return True
        return False

    def _progress(self) -> dict[str, Any] | None:
        hook = self.spec.progress
        if not hook:
            return None
        try:
            fn = getattr(importlib.import_module(hook["module"]), hook["function"])
            return fn(self.job_dir, **(hook.get("args") or {}))
        except Exception as exc:  # noqa: BLE001 - a progress hook never breaks the job
            return {"error": f"{type(exc).__name__}: {exc}"[:200]}

    # ----------------------------------------------------------------------------------------- main
    def run(self) -> int:
        if not self.lock.acquire(timeout_s=5.0):
            return 3                                    # another runner owns this job
        try:
            if self.status.get("status") in TERMINAL:
                return 0
            return self._run()
        except Exception as exc:  # noqa: BLE001 - the job must end with a status and a receipt
            self._finish("FAILED", reason=f"runner error: {type(exc).__name__}: {exc}"[:500], exit_code=None,
                         runner_error=traceback.format_exc()[-4000:])
            return 1
        finally:
            if self.tree is not None:
                self.tree.close()
            self.lock.release()

    def _run(self) -> int:
        spec = self.spec
        queued_ns = time.time_ns()
        self._write(status="QUEUED", app=spec.app, kind=spec.kind, label=spec.label, pool=spec.pool,
                    runner={**(self.status.get("runner") or {}), "pid": os.getpid()},
                    queued_at=self.status.get("queued_at") or utc_now())
        pool = Pool(self.root / "locks", self.root / "jobs", spec.pool)
        pool.enqueue(spec.job_id, queued_ns)
        started_wait = time.monotonic()
        slot = None
        position = None
        try:
            while slot is None:
                reason = self._cancel_reason()
                if reason:
                    return self._finish("CANCELLED", reason=reason, exit_code=None)
                if time.monotonic() - started_wait > spec.queue_timeout_s:
                    return self._finish("TIMED_OUT", reason=f"waited more than {spec.queue_timeout_s} s for the "
                                                            f"'{spec.pool}' pool", exit_code=None, timeout_kind="QUEUE")
                gated = self._gate_blocked()
                pos = pool.position(spec.job_id)
                if not gated and pos is not None and pos < pool.capacity:
                    slot = pool.try_acquire(spec.job_id)
                if slot is None:
                    if pos != position or gated != self.status.get("gated"):
                        position = pos
                        self._write(queue_position=pos, gated=gated)
                    time.sleep(QUEUE_POLL_S)
        finally:
            pool.dequeue(spec.job_id)
        try:
            return self._execute(time.monotonic() - started_wait)
        finally:
            slot.release()

    def _execute(self, queue_wait_s: float) -> int:
        spec = self.spec
        cwd = self.job_dir / spec.cwd
        cwd.mkdir(parents=True, exist_ok=True)
        (self.job_dir / "logs").mkdir(exist_ok=True)
        env = {**os.environ, **spec.env}
        self.tree = ProcessTree()
        t0 = time.monotonic()
        with open(self.job_dir / "logs" / "stdout.log", "ab") as out, \
                open(self.job_dir / "logs" / "stderr.log", "ab") as err:
            proc = self.tree.start(spec.argv, cwd=cwd, env=env, stdout=out, stderr=err)
            self._write(status="RUNNING", started_at=utc_now(), queue_wait_s=round(queue_wait_s, 3),
                        queue_position=None, app_pid=proc.pid, isolation=self.tree.isolation)
            outcome = reason = None
            last_beat = time.monotonic()
            while True:
                try:
                    proc.wait(timeout=POLL_S)
                    break
                except subprocess.TimeoutExpired:
                    pass
                cancel = self._cancel_reason()
                if cancel:
                    outcome, reason = "CANCELLED", cancel
                    self.tree.kill()
                    break
                if time.monotonic() - t0 > spec.timeout_s:
                    outcome, reason = "TIMED_OUT", f"exceeded timeout_s = {spec.timeout_s}"
                    self.tree.kill()
                    break
                if time.monotonic() - last_beat >= HEARTBEAT_S:
                    last_beat = time.monotonic()
                    progress = {"elapsed_s": round(time.monotonic() - t0, 1),
                                "stdout_bytes": (self.job_dir / "logs" / "stdout.log").stat().st_size}
                    extra = self._progress()
                    if extra:
                        progress.update(extra)
                    self._write(progress=progress)
            # the main process ended (or the tree was killed): give its children a grace period, then kill
            leftover = 0
            if outcome is None and not self.tree.wait_empty(spec.tree_grace_s):
                leftover = self.tree.active_count()
                self.tree.kill()
            if self.tree.killed:
                self.tree.wait_empty(KILL_WAIT_S)
            proc.wait()
        duration = round(time.monotonic() - t0, 3)
        exit_code = proc.returncode
        tree = {"isolation": self.tree.isolation, "killed_by_runner": self.tree.killed,
                "leftover_processes_killed": leftover, "active_after": self.tree.active_count(),
                **self.tree.accounting()}
        return self._finish(outcome, reason=reason, exit_code=exit_code, duration_s=duration, tree=tree)

    # ----------------------------------------------------------------------------------------- the end
    def _finish(self, outcome: str | None, *, reason: str | None, exit_code: int | None,
                duration_s: float | None = None,
                tree: dict[str, Any] | None = None, timeout_kind: str | None = None,
                runner_error: str | None = None) -> int:
        spec = self.spec
        logs = log_summary(self.job_dir, spec, self.redactor)
        ran = exit_code is not None
        checks = run_checks(self.job_dir, spec.checks, exit_code) if ran else []
        if outcome is None:
            if logs.get("license_failure") and exit_code not in spec.success_exit_codes:
                outcome, reason = "LICENSE_UNAVAILABLE", logs["license_failure"]
            elif exit_code not in spec.success_exit_codes:
                outcome, reason = "FAILED", f"exit code {exit_code}"
            elif checks and not all(c["passed"] for c in checks):
                outcome = "CHECK_FAILED"
                reason = "failed checks: " + ", ".join(c["name"] for c in checks if not c["passed"])
            else:
                outcome = "SUCCEEDED"
        deleted: list[str] = []
        if outcome == "SUCCEEDED" and spec.cleanup_on_success:
            for rel in job_files(self.job_dir, spec.cleanup_on_success):
                if not rel.startswith(("in/", "logs/")):
                    (self.job_dir / rel).unlink(missing_ok=True)
                    deleted.append(rel)
        rels = [r for r in job_files(self.job_dir, spec.outputs) if r not in deleted]
        excluded = job_files(self.job_dir, spec.hash_exclude) if spec.hash_exclude else []
        outputs = hash_files(self.job_dir, [r for r in rels if r not in set(excluded)])
        log_files = hash_files(self.job_dir, [r for r in job_files(self.job_dir, ["logs/**"]) if r != "logs/runner.log"])
        ended = utc_now()
        final = {"status": outcome, "reason": reason, "exit_code": exit_code, "ended_at": ended,
                 "duration_s": duration_s, "timeout_kind": timeout_kind}
        self.status.update({k: v for k, v in final.items() if v is not None or k in ("exit_code", "reason")})
        runner = {"detached": (read_json(self.job_dir / "spawn.json", default={}) or {}).get("detached")}
        if runner_error:
            runner["error"] = self.redactor.text(runner_error)
        receipt = build_receipt(spec, self.status, job_dir=self.job_dir, redactor=self.redactor,
                                outputs=outputs + [dict(o, kind="log") for o in log_files], checks=checks, logs=logs,
                                runner=runner, tree=tree or {},
                                scratch={"not_hashed": excluded, "deleted_after_success": deleted})
        atomic_write_json(self.job_dir / "receipt.json", receipt)
        self._write(progress=None, receipt="receipt.json", checks_passed=receipt["checks_passed"])
        return 0 if outcome == "SUCCEEDED" else 2


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m vkm_jobs.runner <job_dir>", file=sys.stderr)
        return 64
    job_dir = Path(args[0])
    if not (job_dir / "job.json").is_file():
        print(json.dumps({"error": "job.json not found"}), file=sys.stderr)
        return 66
    return Runner(job_dir).run()


if __name__ == "__main__":
    sys.exit(main())

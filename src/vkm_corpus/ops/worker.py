"""Plan-first job worker on the WORKSTATION (H-12): the only planner and executor of reprocessing jobs.

One job at a time, taken from the control plane with a lease::

    PLAN_REQUESTED → `vkm-corpus run plan`     → PLANNED (plan + hash stored, lease released)
    CONFIRMED      → re-plan; another hash drops the confirmation (back to PLANNED)
                   → RUNNING → `run extract` → `core publish` → PUBLISHED
                   → reconcile on CORE (``VKM_RECONCILE_CMD``) → ADMITTED | REJECTED | FAILED

Stages are the regular CLI in subprocesses (same interpreter and environment): a crash of the pipeline never takes
the worker down, and every run keeps its usual journal and receipts. While a stage runs, a heartbeat thread with its
own connection extends the lease. API/MCP only request and confirm; they never execute (CP-19).
"""
from __future__ import annotations

import json
import logging
import shlex
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from vkm_corpus import ids
from vkm_corpus.config import ConfigError, Settings
from vkm_corpus.ops import jobs
from vkm_corpus.versions import PIPELINE_VERSION

log = logging.getLogger("vkm.ops.worker")

SUPPORTED_KINDS = ("REPROCESS_SOURCE", "REPROCESS_PAGE")
ALLOWED_OPTIONS = ("force", "recall_model", "no_ocr")
PLAN_SCHEMA = "vkm.ops_plan/1"


class JobRequestError(ValueError):
    """The job request cannot be turned into a pipeline selection."""


# ---------------------------------------------------------------------------------------------------- selection
@dataclass(frozen=True)
class Selection:
    source_id: str
    page_range: tuple[int, int] | None = None
    options: tuple[str, ...] = ()

    def cli_args(self) -> list[str]:
        out = ["--source", self.source_id]
        if self.page_range:
            out += ["--page", f"{self.page_range[0]}-{self.page_range[1]}"]
        out += ["--" + o.replace("_", "-") for o in self.options]
        return out

    def to_dict(self) -> dict[str, Any]:
        return {"source_id": self.source_id, "page_range": list(self.page_range) if self.page_range else None,
                "options": list(self.options)}


def selection_for(job: dict[str, Any]) -> Selection:
    kind = job["kind"]
    if kind not in SUPPORTED_KINDS:
        raise JobRequestError(f"job kind {kind} is not executed by the v0 worker")
    request = job.get("request") or {}
    options = request.get("options") or {}
    if not isinstance(options, dict) or set(options) - set(ALLOWED_OPTIONS):
        raise JobRequestError(f"options must be a mapping over {ALLOWED_OPTIONS}")
    chosen = tuple(o for o in ALLOWED_OPTIONS if options.get(o) is True)
    if kind == "REPROCESS_SOURCE":
        sid = job.get("source_id") or ""
        if not ids.matches("source", sid):
            raise JobRequestError(f"REPROCESS_SOURCE needs a valid source_id, got {sid!r}")
        return Selection(sid, None, chosen)
    pid = job.get("page_id") or ""
    if not ids.matches("page", pid):
        raise JobRequestError(f"REPROCESS_PAGE needs a valid page_id, got {pid!r}")
    ref = ids.parse_page_id(pid)
    if job.get("source_id") and job["source_id"] != ref.source_id:
        raise JobRequestError("source_id and page_id disagree")
    return Selection(ref.source_id, (ref.index, ref.index), chosen)


def stable_plan(pipeline_plan: dict[str, Any], sel: Selection) -> dict[str, Any]:
    """What a human confirms: the pipeline plan without run-specific fields (run id, plan artifact id)."""
    return {"schema": PLAN_SCHEMA, "selection": sel.to_dict(), "pipeline_plan_sha256": pipeline_plan["plan_sha256"],
            "totals": pipeline_plan.get("totals"), "sources": pipeline_plan.get("sources")}


def decide(receipt: dict[str, Any], commit_ids: list[str]) -> tuple[str, str, str | None]:
    """Job outcome from a reconcile receipt: (state, note, snapshot_id)."""
    rejected = {r.get("commit_id") for r in receipt.get("rejected") or []}
    mine = sorted(set(commit_ids) & rejected)
    snap = receipt.get("snapshot") or {}
    if mine:
        return "REJECTED", f"commits rejected at admission: {', '.join(mine)}", None
    if snap.get("status") == "PASS" and receipt.get("result") == "RECONCILED":
        return "ADMITTED", "admitted; projections rebuilt", snap.get("snapshot_id")
    failed = [s.get("step") for s in receipt.get("steps") or [] if s.get("status") == "FAILED"]
    if snap.get("status") == "PASS" and failed:
        return "ADMITTED", f"admitted; projection steps failed: {', '.join(map(str, failed))}", snap.get("snapshot_id")
    return "FAILED", f"reconcile result {receipt.get('result')!r}; snapshot {snap.get('status')!r}", None


# ---------------------------------------------------------------------------------------------------- commands
def parse_json_tail(text: str) -> dict[str, Any]:
    """The last top-level JSON object a CLI printed (the CLIs print one pretty JSON document at the end)."""
    text = text.strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    lines = text.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith("{"):
            try:
                obj = json.loads("\n".join(lines[i:]))
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                return obj
    raise ValueError("no JSON document in the command output")


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class Runner(Protocol):
    def __call__(self, argv: list[str], timeout: float | None) -> CommandResult: ...


def subprocess_runner(argv: list[str], timeout: float | None) -> CommandResult:
    proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)


def vkm(*args: str) -> list[str]:
    return [sys.executable, "-m", "vkm_corpus.cli", *args]


def code_revision() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], capture_output=True, text=True,
                             cwd=Path(__file__).resolve().parent, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


# ---------------------------------------------------------------------------------------------------- worker
@dataclass
class WorkerConfig:
    worker_id: str
    publish_target: str | None
    reconcile_cmd: str | None
    poll_seconds: float = 10.0
    lease_seconds: int = 900
    heartbeat_seconds: float = 60.0
    stage_timeout: float | None = None
    only_requested_by: str | None = None   # dedicated workers and tests


@dataclass
class Worker:
    settings: Settings
    config: WorkerConfig
    runner: Runner = subprocess_runner
    connect: Callable[[Settings], Any] = jobs.connect
    _stop: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        if self.settings.data_role != "producer":
            raise ConfigError("the job worker runs only on the producer (VKM_DATA_ROLE=producer)")
        self.conn = self.connect(self.settings)
        jobs.register_worker(self.conn, self.config.worker_id, "WORKSTATION", "PIPELINE", code_revision(),
                             PIPELINE_VERSION)

    # -------------------------------------------------------------- loop
    def run_forever(self) -> None:
        while not self._stop.is_set():
            if not self.run_once():
                jobs.heartbeat(self.conn, self.config.worker_id, state="IDLE")
                self._stop.wait(self.config.poll_seconds)

    def stop(self) -> None:
        self._stop.set()

    def run_once(self) -> bool:
        job = jobs.claim_next(self.conn, self.config.worker_id, lease_seconds=self.config.lease_seconds,
                              requested_by=self.config.only_requested_by)
        if job is None:
            return False
        try:
            sel = selection_for(job)
        except JobRequestError as exc:
            self._fail(job, "E_BAD_REQUEST", str(exc), stage="plan")
            return True
        with self._heartbeat(job):
            try:
                if job["state"] == "PLAN_REQUESTED":
                    self._plan(job, sel)
                elif job["state"] == "CONFIRMED":
                    self._execute(job, sel)
            except Exception as exc:  # noqa: BLE001 - every failure ends the job as FAILED with an error row
                log.exception("job %s failed", job["job_id"])
                self._fail(job, "E_WORKER", f"{type(exc).__name__}: {exc}", stage=job["state"].lower())
        return True

    # -------------------------------------------------------------- stages
    def _pipeline_plan(self, sel: Selection) -> dict[str, Any]:
        res = self.runner(vkm("run", "plan", *sel.cli_args()), self.config.stage_timeout)
        if res.returncode != 0:
            raise RuntimeError(f"run plan exited {res.returncode}: {res.stderr.strip()[-400:]}")
        return parse_json_tail(res.stdout)

    def _plan(self, job: dict[str, Any], sel: Selection) -> None:
        plan = stable_plan(self._pipeline_plan(sel), sel)
        digest = jobs.record_plan(self.conn, job["job_id"], plan, planned_by=self.config.worker_id)
        log.info("job %s planned (%s)", job["job_id"], digest[:12])

    def _execute(self, job: dict[str, Any], sel: Selection) -> None:
        job_id = job["job_id"]
        fresh = stable_plan(self._pipeline_plan(sel), sel)
        try:
            jobs.verify_confirmed_plan(self.conn, job_id, fresh)
        except jobs.JobStateError:
            jobs.record_plan(self.conn, job_id, fresh, planned_by=self.config.worker_id)
            log.info("job %s: plan changed since confirmation; re-planned, new confirmation needed", job_id)
            return
        if not self.config.publish_target or not self.config.reconcile_cmd:
            raise ConfigError("publish target and reconcile command are required to execute jobs")
        jobs.set_state(self.conn, job_id, "RUNNING")
        argv = vkm("run", "extract", *sel.cli_args())
        if "recall_model" in sel.options:
            argv += ["--confirm-plan", fresh["pipeline_plan_sha256"]]
        res = self.runner(argv, self.config.stage_timeout)
        summary = parse_json_tail(res.stdout) if res.stdout.strip() else {}
        run_id = summary.get("run_id")
        if res.returncode != 0 or summary.get("status") not in ("SUCCEEDED", "PARTIAL"):
            raise RuntimeError(f"run extract {run_id or ''} ended {summary.get('status')!r} (exit {res.returncode})")
        commit_ids = sorted({c["commit_id"] for c in (summary.get("commits") or {}).values() if c.get("commit_id")})
        pub = self.runner(vkm("core", "publish", "--target", self.config.publish_target), self.config.stage_timeout)
        if pub.returncode != 0:
            raise RuntimeError(f"core publish exited {pub.returncode}: {pub.stderr.strip()[-400:]}")
        jobs.set_state(self.conn, job_id, "PUBLISHED", run_id=run_id, commit_ids=commit_ids,
                       note=f"extraction {summary.get('status')}")
        reconcile_run = ids.new_run_id()
        cmd = [part.replace("{run_id}", reconcile_run) for part in shlex.split(self.config.reconcile_cmd)]
        rec = self.runner(cmd, self.config.stage_timeout)
        receipt = parse_json_tail(rec.stdout) if rec.stdout.strip() else {"result": f"exit {rec.returncode}"}
        state, note, snapshot = decide(receipt, commit_ids)
        fields: dict[str, Any] = {"note": f"{note} (reconcile {reconcile_run})"}
        if snapshot:
            fields["snapshot_id"] = snapshot
        jobs.set_state(self.conn, job_id, state, **fields)
        log.info("job %s → %s", job_id, state)

    def _fail(self, job: dict[str, Any], code: str, message: str, *, stage: str) -> None:
        error_id = jobs.record_error(self.conn, code=code, message=message[:2000], stage=stage, tool="ops.worker",
                                     worker_id=self.config.worker_id, job_id=job["job_id"],
                                     source_id=job.get("source_id"), page_id=job.get("page_id"))
        try:
            jobs.set_state(self.conn, job["job_id"], "FAILED", last_error_id=error_id, note=message[:500])
        except jobs.JobStateError:
            log.exception("could not mark job %s FAILED", job["job_id"])

    # -------------------------------------------------------------- heartbeat + lease extension
    def _heartbeat(self, job: dict[str, Any]):
        worker = self

        class _Beat:
            def __enter__(self_inner):
                self_inner.stop = threading.Event()
                self_inner.thread = threading.Thread(target=self_inner.loop, daemon=True)
                self_inner.thread.start()
                return self_inner

            def loop(self_inner) -> None:
                try:
                    conn = worker.connect(worker.settings)
                except Exception:  # noqa: BLE001 - heartbeats are best effort
                    log.exception("heartbeat connection failed")
                    return
                try:
                    while not self_inner.stop.wait(worker.config.heartbeat_seconds):
                        jobs.heartbeat(conn, worker.config.worker_id, job_id=job["job_id"], stage=job["state"])
                        jobs.extend_lease(conn, job["job_id"], worker.config.worker_id, worker.config.lease_seconds)
                except Exception:  # noqa: BLE001
                    log.exception("heartbeat failed")
                finally:
                    conn.close()

            def __exit__(self_inner, *exc) -> None:
                self_inner.stop.set()
                self_inner.thread.join(timeout=10)

        return _Beat()


def default_worker_id() -> str:
    """Stable per host: role plus a short hash of the host name (no host names in the control plane)."""
    import hashlib

    return "workstation-" + hashlib.sha256(socket.gethostname().encode("utf-8")).hexdigest()[:8]

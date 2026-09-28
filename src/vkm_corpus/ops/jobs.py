"""Plan-first jobs on the control plane (H-12).

Lifecycle::

    PLAN_REQUESTED → PLANNED → CONFIRMED → RUNNING → PUBLISHED → ADMITTED | REJECTED
    (any active state) → FAILED | CANCELLED;  FAILED → PLAN_REQUESTED (retry)

* API/MCP only *request* a plan; the worker is the only planner (it knows code revision, config hash, model revisions)
  and stores the plan with ``plan_sha256``;
* a human confirms exactly that hash; before executing, the worker re-plans and refuses on a different hash;
* claims use ``FOR UPDATE SKIP LOCKED`` with a lease, so two workers never take the same job.

All functions take an open psycopg connection and commit their own transaction.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from vkm_corpus.config import ConfigError, Settings

JOB_KINDS = ("REPROCESS_SOURCE", "REPROCESS_PAGE", "PIPELINE_RUN", "PUBLISH", "RECONCILE")
ACTIVE = ("PLAN_REQUESTED", "PLANNED", "CONFIRMED", "RUNNING", "PUBLISHED")
TERMINAL = ("ADMITTED", "REJECTED", "FAILED", "CANCELLED")
TRANSITIONS: dict[str, frozenset[str]] = {
    "PLAN_REQUESTED": frozenset({"PLANNED", "FAILED", "CANCELLED"}),
    "PLANNED": frozenset({"CONFIRMED", "PLAN_REQUESTED", "FAILED", "CANCELLED"}),
    "CONFIRMED": frozenset({"RUNNING", "PLAN_REQUESTED", "FAILED", "CANCELLED"}),
    "RUNNING": frozenset({"PUBLISHED", "FAILED"}),
    "PUBLISHED": frozenset({"ADMITTED", "REJECTED", "FAILED"}),
    "FAILED": frozenset({"PLAN_REQUESTED"}),
    "ADMITTED": frozenset(),
    "REJECTED": frozenset({"PLAN_REQUESTED"}),
    "CANCELLED": frozenset(),
}


class JobStateError(RuntimeError):
    """Illegal transition, stale plan hash or missing job."""


def plan_sha256(plan: dict[str, Any]) -> str:
    """Hash of a plan in canonical JSON (sorted keys, compact) — what a human confirms."""
    text = json.dumps(plan, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def connect(settings: Settings):
    """psycopg connection from ``VKM_PG_DSN``/``VKM_PG_DSN_FILE`` (imported lazily; the pipeline never requires it)."""
    if not settings.pg_dsn:
        raise ConfigError("VKM_PG_DSN (or VKM_PG_DSN_FILE) is not set; the control plane is optional for the pipeline")
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(settings.pg_dsn, row_factory=dict_row, application_name="vkm-corpus")


def init_schema(conn) -> str:
    from importlib.resources import files

    sql = files("vkm_corpus.ops").joinpath("schema.sql").read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql)
        cur.execute("SELECT max(version) AS v FROM ops.schema_version")
        version = cur.fetchone()["v"]
    conn.commit()
    return version


# ---------------------------------------------------------------- requests (API / MCP admin)
def request_plan(conn, kind: str, requested_by: str, *, source_id: str | None = None, page_id: str | None = None,
                 request: dict[str, Any] | None = None) -> int:
    if kind not in JOB_KINDS:
        raise JobStateError(f"unknown job kind {kind!r}")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ops.job (kind, state, source_id, page_id, request, requested_by) "
            "VALUES (%s, 'PLAN_REQUESTED', %s, %s, %s::jsonb, %s) RETURNING job_id",
            (kind, source_id, page_id, json.dumps(request or {}), requested_by))
        job_id = cur.fetchone()["job_id"]
    conn.commit()
    return job_id


def confirm(conn, job_id: int, confirmed_plan_sha256: str, confirmed_by: str) -> dict[str, Any]:
    """Human confirmation of exactly the stored plan hash (stale or foreign hash → JobStateError)."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM ops.job WHERE job_id = %s FOR UPDATE", (job_id,))
        job = cur.fetchone()
        if job is None:
            raise JobStateError(f"job {job_id} not found")
        if job["state"] != "PLANNED":
            raise JobStateError(f"job {job_id} is {job['state']}, only PLANNED can be confirmed")
        if job["plan_sha256"] != confirmed_plan_sha256:
            raise JobStateError(f"job {job_id}: plan changed (confirmed hash does not match the current plan)")
        cur.execute(
            "UPDATE ops.job SET state = 'CONFIRMED', confirmed_plan_sha256 = %s, confirmed_by = %s, "
            "confirmed_at = now(), updated_at = now() WHERE job_id = %s RETURNING *",
            (confirmed_plan_sha256, confirmed_by, job_id))
        row = cur.fetchone()
    conn.commit()
    return row


def cancel(conn, job_id: int, by: str, note: str = "") -> None:
    set_state(conn, job_id, "CANCELLED", note=f"cancelled by {by}: {note}".strip())


# ---------------------------------------------------------------- worker side
def claim_next(conn, worker_id: str, states: tuple[str, ...] = ("PLAN_REQUESTED", "CONFIRMED"),
               lease_seconds: int = 900, requested_by: str | None = None) -> dict[str, Any] | None:
    """Take the oldest job in ``states`` that is not leased by a live worker (optionally of one requester)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT job_id FROM ops.job WHERE state = ANY(%s) AND (lease_until IS NULL OR lease_until < now()) "
            "AND (%s::text IS NULL OR requested_by = %s::text) "
            "ORDER BY job_id FOR UPDATE SKIP LOCKED LIMIT 1", (list(states), requested_by, requested_by))
        found = cur.fetchone()
        if found is None:
            conn.commit()
            return None
        cur.execute(
            "UPDATE ops.job SET locked_by = %s, lease_until = now() + make_interval(secs => %s), "
            "attempts = attempts + 1, updated_at = now() WHERE job_id = %s RETURNING *",
            (worker_id, lease_seconds, found["job_id"]))
        job = cur.fetchone()
    conn.commit()
    return job


def extend_lease(conn, job_id: int, worker_id: str, lease_seconds: int = 900) -> bool:
    """Keep the lease of a job this worker still holds (a no-op once the job has left the worker)."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE ops.job SET lease_until = now() + make_interval(secs => %s) "
            "WHERE job_id = %s AND locked_by = %s AND lease_until IS NOT NULL", (lease_seconds, job_id, worker_id))
        extended = cur.rowcount == 1
    conn.commit()
    return extended


def record_plan(conn, job_id: int, plan: dict[str, Any], planned_by: str) -> str:
    """Store the worker's plan; returns its hash. A re-plan of a CONFIRMED job with another hash drops confirmation."""
    digest = plan_sha256(plan)
    with conn.cursor() as cur:
        cur.execute("SELECT state, plan_sha256 FROM ops.job WHERE job_id = %s FOR UPDATE", (job_id,))
        job = cur.fetchone()
        if job is None:
            raise JobStateError(f"job {job_id} not found")
        if job["state"] not in ("PLAN_REQUESTED", "PLANNED", "CONFIRMED"):
            raise JobStateError(f"job {job_id} is {job['state']}, cannot be (re)planned")
        cur.execute(
            "UPDATE ops.job SET state = 'PLANNED', plan = %s::jsonb, plan_sha256 = %s, planned_by = %s, "
            "planned_at = now(), confirmed_plan_sha256 = NULL, confirmed_by = NULL, confirmed_at = NULL, "
            "locked_by = NULL, lease_until = NULL, updated_at = now() WHERE job_id = %s",
            (json.dumps(plan, sort_keys=True), digest, planned_by, job_id))
    conn.commit()
    return digest


def verify_confirmed_plan(conn, job_id: int, fresh_plan: dict[str, Any]) -> None:
    """Before execution: the freshly recomputed plan must equal the confirmed one (H-12)."""
    with conn.cursor() as cur:
        cur.execute("SELECT state, confirmed_plan_sha256 FROM ops.job WHERE job_id = %s", (job_id,))
        job = cur.fetchone()
    conn.commit()
    if job is None or job["state"] != "CONFIRMED":
        raise JobStateError(f"job {job_id} is not CONFIRMED")
    if plan_sha256(fresh_plan) != job["confirmed_plan_sha256"]:
        raise JobStateError(f"job {job_id}: plan changed since confirmation; a new confirmation is required")


def set_state(conn, job_id: int, new_state: str, **fields: Any) -> dict[str, Any]:
    allowed_fields = {"run_id", "commit_ids", "snapshot_id", "last_error_id", "note"}
    unknown = set(fields) - allowed_fields
    if unknown:
        raise JobStateError(f"unknown job fields {sorted(unknown)}")
    with conn.cursor() as cur:
        cur.execute("SELECT state FROM ops.job WHERE job_id = %s FOR UPDATE", (job_id,))
        job = cur.fetchone()
        if job is None:
            raise JobStateError(f"job {job_id} not found")
        if new_state not in TRANSITIONS.get(job["state"], frozenset()) and not (
                new_state == "CANCELLED" and job["state"] in ACTIVE[:3]):
            raise JobStateError(f"job {job_id}: illegal transition {job['state']} → {new_state}")
        sets = ["state = %s", "updated_at = now()"]
        values: list[Any] = [new_state]
        for name, value in fields.items():
            sets.append(f"{name} = %s" + ("::jsonb" if name == "commit_ids" else ""))
            values.append(json.dumps(value) if name == "commit_ids" else value)
        if new_state in TERMINAL or new_state in ("PLANNED", "PUBLISHED"):
            sets += ["locked_by = NULL", "lease_until = NULL"]
        cur.execute(f"UPDATE ops.job SET {', '.join(sets)} WHERE job_id = %s RETURNING *", (*values, job_id))
        row = cur.fetchone()
    conn.commit()
    return row


# ---------------------------------------------------------------- workers, heartbeats, errors, services
def register_worker(conn, worker_id: str, host_role: str, kind: str, code_revision: str | None,
                    pipeline_version: str | None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ops.worker (worker_id, host_role, kind, code_revision, pipeline_version) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (worker_id) DO UPDATE SET state = 'IDLE', "
            "last_heartbeat_at = now()", (worker_id, host_role, kind, code_revision, pipeline_version))
    conn.commit()


def heartbeat(conn, worker_id: str, *, state: str = "BUSY", run_id: str | None = None, job_id: int | None = None,
              stage: str | None = None, detail: dict[str, Any] | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE ops.worker SET state = %s, last_heartbeat_at = now() WHERE worker_id = %s",
                    (state, worker_id))
        cur.execute("INSERT INTO ops.heartbeat (worker_id, run_id, job_id, stage, detail) VALUES (%s, %s, %s, %s, "
                    "%s::jsonb)", (worker_id, run_id, job_id, stage, json.dumps(detail or {})))
    conn.commit()


def record_error(conn, *, code: str, message: str, stage: str | None = None, tool: str | None = None,
                 retryable: bool = False, worker_id: str | None = None, job_id: int | None = None,
                 run_id: str | None = None, source_id: str | None = None, page_id: str | None = None,
                 log_ref: str | None = None) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ops.error (worker_id, job_id, run_id, code, stage, tool, message, retryable, source_id, "
            "page_id, log_ref) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING error_id",
            (worker_id, job_id, run_id, code, stage, tool, message, retryable, source_id, page_id, log_ref))
        error_id = cur.fetchone()["error_id"]
    conn.commit()
    return error_id


def upsert_run(conn, run_id: str, kind: str, host_role: str, *, state: str = "RUNNING",
               pipeline_version: str | None = None, code_revision: str | None = None,
               params: dict[str, Any] | None = None, summary: dict[str, Any] | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ops.ingest_run (run_id, kind, host_role, state, pipeline_version, code_revision, params, "
            "summary) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb) ON CONFLICT (run_id) DO UPDATE SET "
            "state = EXCLUDED.state, summary = EXCLUDED.summary, finished_at = CASE WHEN EXCLUDED.state <> "
            "'RUNNING' THEN now() END",
            (run_id, kind, host_role, state, pipeline_version, code_revision, json.dumps(params or {}),
             json.dumps(summary or {})))
    conn.commit()


def set_service_state(conn, service: str, host_role: str, state: str, *, version: str | None = None,
                      detail: dict[str, Any] | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ops.service_state (service, host_role, version, state, detail) VALUES (%s, %s, %s, %s, "
            "%s::jsonb) ON CONFLICT (service) DO UPDATE SET host_role = EXCLUDED.host_role, version = "
            "EXCLUDED.version, state = EXCLUDED.state, detail = EXCLUDED.detail, updated_at = now()",
            (service, host_role, version, state, json.dumps(detail or {})))
    conn.commit()


def status(conn) -> dict[str, Any]:
    """Counts for /status and `vkm-corpus ops status` (roles only, no addresses)."""
    out: dict[str, Any] = {}
    with conn.cursor() as cur:
        cur.execute("SELECT state, count(*) AS n FROM ops.job GROUP BY state ORDER BY state")
        out["jobs"] = {r["state"]: r["n"] for r in cur.fetchall()}
        cur.execute("SELECT worker_id, host_role, kind, state, last_heartbeat_at FROM ops.worker "
                    "ORDER BY last_heartbeat_at DESC LIMIT 20")
        out["workers"] = [dict(r, last_heartbeat_at=r["last_heartbeat_at"].isoformat()) for r in cur.fetchall()]
        cur.execute("SELECT service, host_role, version, state, updated_at FROM ops.service_state ORDER BY service")
        out["services"] = [dict(r, updated_at=r["updated_at"].isoformat()) for r in cur.fetchall()]
        cur.execute("SELECT count(*) AS n FROM ops.error WHERE at > now() - interval '24 hours'")
        out["errors_24h"] = cur.fetchone()["n"]
        cur.execute("SELECT max(version) AS v FROM ops.schema_version")
        out["schema_version"] = cur.fetchone()["v"]
    conn.commit()
    return out

"""Control plane (H-12, H-42): plan hashing, lifecycle graph, live lifecycle against PostgreSQL (services)."""
from __future__ import annotations

import os

import pytest

from vkm_corpus.config import load_settings
from vkm_corpus.ops import jobs


def test_plan_hash_is_canonical():
    a = {"pages": ["VKM-SRC-001:p0001"], "model_calls": 0}
    b = {"model_calls": 0, "pages": ["VKM-SRC-001:p0001"]}
    assert jobs.plan_sha256(a) == jobs.plan_sha256(b)
    assert jobs.plan_sha256(a) != jobs.plan_sha256({**a, "model_calls": 1})


def test_lifecycle_graph_is_closed_and_plan_first():
    states = set(jobs.TRANSITIONS)
    assert all(targets <= states for targets in jobs.TRANSITIONS.values())
    assert "RUNNING" not in jobs.TRANSITIONS["PLAN_REQUESTED"]      # nothing runs without a plan
    assert "RUNNING" not in jobs.TRANSITIONS["PLANNED"]             # nothing runs without confirmation
    assert jobs.TRANSITIONS["RUNNING"] == {"PUBLISHED", "FAILED"}
    assert not jobs.TRANSITIONS["ADMITTED"] and not jobs.TRANSITIONS["CANCELLED"]
    assert set(jobs.ACTIVE).isdisjoint(jobs.TERMINAL)


def test_schema_is_packaged_and_idempotent_text():
    from importlib.resources import files

    sql = files("vkm_corpus.ops").joinpath("schema.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS ops.job" in sql and "job_plan_confirm" in sql
    for statement in ("CREATE TABLE", "CREATE INDEX"):
        assert all("IF NOT EXISTS" in line for line in sql.splitlines() if line.startswith(statement))


@pytest.mark.services
def test_live_plan_first_lifecycle():
    pytest.importorskip("psycopg")
    settings = load_settings()
    if not settings.pg_dsn:
        pytest.skip("VKM_PG_DSN(_FILE) not configured: NOT_RUN")
    worker = f"TEST:{os.getpid()}"
    with jobs.connect(settings) as conn:
        jobs.init_schema(conn)
        jobs.register_worker(conn, worker, "WORKSTATION", "PIPELINE", "test", "0")
        job_id = jobs.request_plan(conn, "REPROCESS_PAGE", "pytest", source_id="VKM-SRC-001",
                                   page_id="VKM-SRC-001:p0001")
        try:
            plan = {"pages": ["VKM-SRC-001:p0001"], "model_calls": 0}
            claimed = jobs.claim_next(conn, worker, states=("PLAN_REQUESTED",))
            assert claimed is not None
            digest = jobs.record_plan(conn, job_id, plan, worker)
            with pytest.raises(jobs.JobStateError):
                jobs.confirm(conn, job_id, "0" * 64, "pytest")
            jobs.confirm(conn, job_id, digest, "pytest")
            with pytest.raises(jobs.JobStateError):
                jobs.verify_confirmed_plan(conn, job_id, {**plan, "model_calls": 1})
            jobs.verify_confirmed_plan(conn, job_id, plan)
            with pytest.raises(jobs.JobStateError):
                jobs.set_state(conn, job_id, "ADMITTED")
        finally:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM ops.job WHERE job_id = %s", (job_id,))
                cur.execute("DELETE FROM ops.heartbeat WHERE worker_id = %s", (worker,))
                cur.execute("DELETE FROM ops.worker WHERE worker_id = %s", (worker,))
            conn.commit()

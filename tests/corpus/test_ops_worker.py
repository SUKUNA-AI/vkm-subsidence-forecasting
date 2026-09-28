"""Plan-first job worker (H-12): selection, stable plan, outcome decision; live lifecycle with a fake command runner."""
from __future__ import annotations

import dataclasses
import json
import os
import secrets

import pytest

from vkm_corpus.config import load_settings
from vkm_corpus.ops import jobs
from vkm_corpus.ops.worker import (CommandResult, JobRequestError, Selection, Worker, WorkerConfig, decide,
                                   parse_json_tail, selection_for, stable_plan)


def test_selection_for_source_and_page():
    sel = selection_for({"kind": "REPROCESS_SOURCE", "source_id": "VKM-SRC-044", "request": {}})
    assert sel == Selection("VKM-SRC-044") and sel.cli_args() == ["--source", "VKM-SRC-044"]
    sel = selection_for({"kind": "REPROCESS_PAGE", "source_id": "VKM-SRC-044", "page_id": "VKM-SRC-044:p0012",
                         "request": {"options": {"force": True, "no_ocr": False}}})
    assert sel.cli_args() == ["--source", "VKM-SRC-044", "--page", "12-12", "--force"]


@pytest.mark.parametrize("job", [
    {"kind": "PIPELINE_RUN", "source_id": "VKM-SRC-044"},
    {"kind": "REPROCESS_SOURCE", "source_id": "VKM-SRC-44"},
    {"kind": "REPROCESS_PAGE", "page_id": "VKM-SRC-044:x0001"},
    {"kind": "REPROCESS_PAGE", "source_id": "VKM-SRC-045", "page_id": "VKM-SRC-044:p0001"},
    {"kind": "REPROCESS_SOURCE", "source_id": "VKM-SRC-044", "request": {"options": {"delete_everything": True}}},
])
def test_selection_rejects_bad_requests(job):
    with pytest.raises(JobRequestError):
        selection_for(job)


def test_stable_plan_ignores_run_specific_fields():
    sel = Selection("VKM-SRC-044")
    a = {"run_id": "RUN-20260928T010000Z-0000aaaa", "plan_sha256": "ab" * 32, "plan_artifact_id": "sha256:" + "1" * 64,
         "totals": {"to_process": 1}, "sources": [{"source_id": "VKM-SRC-044", "action": "PROCESS"}]}
    b = {**a, "run_id": "RUN-20260928T020000Z-0000bbbb", "plan_artifact_id": "sha256:" + "2" * 64}
    assert jobs.plan_sha256(stable_plan(a, sel)) == jobs.plan_sha256(stable_plan(b, sel))
    assert "run_id" not in stable_plan(a, sel)


def test_decide_outcomes():
    ok = {"result": "RECONCILED", "snapshot": {"status": "PASS", "snapshot_id": "SNAP-1"}, "rejected": []}
    assert decide(ok, ["c1"]) == ("ADMITTED", "admitted; projections rebuilt", "SNAP-1")
    rej = {**ok, "rejected": [{"commit_id": "c1", "key": "source=VKM-SRC-044", "problems": []}]}
    assert decide(rej, ["c1"])[0] == "REJECTED"
    assert decide(rej, ["c2"])[0] == "ADMITTED"                       # someone else's commit was rejected
    partial = {"result": "FAILED", "snapshot": {"status": "PASS", "snapshot_id": "SNAP-2"},
               "steps": [{"step": "graph", "status": "FAILED"}]}
    assert decide(partial, ["c1"])[:1] == ("ADMITTED",) and "graph" in decide(partial, ["c1"])[1]
    not_passed = {"result": "SNAPSHOT_NOT_PASSED_PROJECTIONS_UNCHANGED", "snapshot": {"status": "FAIL"}}
    assert decide(not_passed, ["c1"])[0] == "FAILED"


def test_parse_json_tail_skips_log_lines():
    text = 'progress 1/3\n{"a": 1}\nmore log\n{\n "run_id": "RUN-x",\n "status": "SUCCEEDED"\n}\n'
    assert parse_json_tail(text) == {"run_id": "RUN-x", "status": "SUCCEEDED"}
    with pytest.raises(ValueError):
        parse_json_tail("no json here")


class FakeRunner:
    """Stands in for the CLI: `run plan`, `run extract`, `core publish` and the reconcile command."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.plan_totals = {"to_process": 1}

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        if argv[-2:] == ["--source", "VKM-SRC-001"] and "plan" in argv:
            body = {"run_id": f"RUN-{secrets.token_hex(4)}", "plan_sha256": "cd" * 32, "totals": self.plan_totals,
                    "sources": [{"source_id": "VKM-SRC-001", "action": "PROCESS"}]}
            return CommandResult(0, "log line\n" + json.dumps(body, indent=1), "")
        if "extract" in argv:
            body = {"run_id": "RUN-20260928T030000Z-0000cccc", "status": "SUCCEEDED",
                    "commits": {"VKM-SRC-001": {"status": "COMPLETE", "commit_id": "commit-1"}}}
            return CommandResult(0, json.dumps(body, indent=1), "")
        if "publish" in argv:
            return CommandResult(0, json.dumps({"steps": []}), "")
        if argv[0] == "reconcile-on-core":
            body = {"result": "RECONCILED", "snapshot": {"status": "PASS", "snapshot_id": "SNAP-TEST"}, "rejected": []}
            return CommandResult(0, json.dumps(body), "")
        return CommandResult(2, "", f"unexpected command {argv}")


@pytest.mark.services
def test_live_worker_plans_then_executes_confirmed_job(monkeypatch):
    pytest.importorskip("psycopg")
    settings = load_settings()
    if not settings.pg_dsn:
        pytest.skip("VKM_PG_DSN(_FILE) not configured: NOT_RUN")
    monkeypatch.setattr("vkm_corpus.ops.worker.code_revision", lambda: "test")
    requester = f"pytest-worker-{os.getpid()}-{secrets.token_hex(3)}"
    cfg = WorkerConfig(worker_id=requester, publish_target="core:/canonical", reconcile_cmd="reconcile-on-core {run_id}",
                       heartbeat_seconds=0.2, only_requested_by=requester)
    runner = FakeRunner()
    worker = Worker(dataclasses.replace(settings, data_role="producer"), cfg, runner=runner)
    conn = worker.conn
    job_id = jobs.request_plan(conn, "REPROCESS_SOURCE", requester, source_id="VKM-SRC-001")
    try:
        assert worker.run_once()                                        # PLAN_REQUESTED → PLANNED
        job = _job(conn, job_id)
        assert job["state"] == "PLANNED" and job["plan"]["pipeline_plan_sha256"] == "cd" * 32
        jobs.confirm(conn, job_id, job["plan_sha256"], "pytest")
        runner.plan_totals = {"to_process": 2}                          # the world changed after confirmation
        assert worker.run_once()
        assert _job(conn, job_id)["state"] == "PLANNED"                 # confirmation dropped, nothing executed
        assert not any("extract" in c for c in runner.calls)
        jobs.confirm(conn, job_id, _job(conn, job_id)["plan_sha256"], "pytest")
        assert worker.run_once()                                        # CONFIRMED → … → ADMITTED
        job = _job(conn, job_id)
        assert job["state"] == "ADMITTED" and job["snapshot_id"] == "SNAP-TEST"
        assert job["run_id"] == "RUN-20260928T030000Z-0000cccc" and job["commit_ids"] == ["commit-1"]
        reconcile = [c for c in runner.calls if c[0] == "reconcile-on-core"]
        assert len(reconcile) == 1 and reconcile[0][1].startswith("RUN-")
        assert not worker.run_once()                                    # nothing left for this requester
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM ops.job WHERE job_id = %s", (job_id,))
            cur.execute("DELETE FROM ops.error WHERE worker_id = %s", (requester,))
            cur.execute("DELETE FROM ops.heartbeat WHERE worker_id = %s", (requester,))
            cur.execute("DELETE FROM ops.worker WHERE worker_id = %s", (requester,))
        conn.commit()
        conn.close()


def _job(conn, job_id):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM ops.job WHERE job_id = %s", (job_id,))
        row = cur.fetchone()
    conn.commit()
    return row

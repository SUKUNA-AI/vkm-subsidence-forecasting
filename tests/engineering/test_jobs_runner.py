"""State machine of the detached job runner on a fake application (``python -c``): success with checks, failure,
failed checks, licence failure, timeout and cancel with the whole process tree killed, leftover children, FIFO pool
queue, gate, a lost runner, and receipts without machine paths (plan §2, §7)."""
from __future__ import annotations

import json
import os
import signal
import time

import pytest

from vkm_jobs.errors import ToolFailure
from vkm_jobs.procs import pid_alive
from vkm_jobs.redact import public_text_problems
from vkm_jobs.receipt import sha256_file
from vkm_jobs.spec import read_json

from jobs_fakes import OK_APP, ORPHAN_APP, PY, TREE_APP, read_pids, service, submit, wait_for

CHECK_S = {"name": "sum", "kind": "json_value", "path": "out/result.json", "pointer": "/s", "expected": 136}


def test_success_receipt_inputs_outputs_and_checks(tmp_path):
    jobs = service(tmp_path)
    draft = jobs.draft("PY")
    src = tmp_path / "input.csv"
    src.write_text("t,y\n0,0\n1,1\n", encoding="utf-8")
    [copied] = draft.copy_input(src)
    spec = draft.spec(kind="fake", pool="py", argv=[PY, "-c", OK_APP], timeout_s=60, label="smoke",
                      checks=[CHECK_S, {"name": "vec", "kind": "number_close", "path": "out/result.json",
                                        "pointer": "/v/1", "expected": 2.0000001, "rtol": 1e-6},
                              {"name": "exit", "kind": "exit_code", "expected": 0},
                              {"name": "log", "kind": "text_contains", "path": "logs/stdout.log",
                               "expected": "line three"}],
                      params=[{"name": "E", "value": 1.0, "status": "ENGINEERING_ASSUMPTION",
                               "source_ref": "toy value for the job-layer test", "unit": "Pa"}])
    ref = jobs.submit(draft, spec, wait_s=60)
    assert ref["status"] == "SUCCEEDED" and ref["exit_code"] == 0 and ref["checks_passed"] is True
    receipt = jobs.receipt(ref["job_id"])
    assert receipt["schema"] == "vkm.sim_receipt/1" and receipt["result_status"] == "MODEL_RESULT"
    assert receipt["review_status"] == "AUTO_UNREVIEWED" and receipt["label"] == "smoke"
    assert copied["sha256"] == sha256_file(src) == receipt["inputs"][0]["sha256"]
    assert receipt["inputs"][0]["path"] == "in/input.csv"
    out = {o["path"]: o for o in receipt["outputs"]}
    assert out["out/result.json"]["sha256"] == sha256_file(draft.dir / "out" / "result.json")
    assert "logs/stdout.log" in out and out["logs/stdout.log"]["kind"] == "log"
    assert [c["passed"] for c in receipt["checks"]] == [True, True, True, True]
    assert receipt["log_summary"]["errors"] == 1 and "something to grep" in receipt["log_summary"]["first_error"]
    assert receipt["params"][0]["status"] == "ENGINEERING_ASSUMPTION"
    assert receipt["process_tree"]["active_after"] == 0 and receipt["queue_wait_s"] >= 0
    text = json.dumps(receipt, ensure_ascii=False)
    assert str(tmp_path) not in text and "<VKM_SIM_ROOT>" in text
    assert public_text_problems(text) == []
    assert receipt["command"][1:] == ["-c", OK_APP]


def test_failed_check_failed_and_licence(tmp_path):
    jobs = service(tmp_path)
    failed, _ = submit(jobs, "import sys; print('ERROR: boom', file=sys.stderr); sys.exit(3)", wait_s=60)
    assert failed["status"] == "FAILED" and failed["exit_code"] == 3
    assert "boom" in jobs.receipt(failed["job_id"])["log_summary"]["first_error"]
    wrong = dict(CHECK_S, expected=137)
    check_failed, _ = submit(jobs, OK_APP, checks=[wrong], wait_s=60)
    assert check_failed["status"] == "CHECK_FAILED" and "sum" in check_failed["reason"]
    [check] = jobs.receipt(check_failed["job_id"])["checks"]
    assert check["actual"] == 136 and check["passed"] is False
    lic, _ = submit(jobs, "import sys; print('License checkout failed for feature X'); sys.exit(1)", wait_s=60)
    assert lic["status"] == "LICENSE_UNAVAILABLE"


def test_timeout_kills_the_whole_tree(tmp_path):
    jobs = service(tmp_path)
    ref, draft = submit(jobs, TREE_APP, timeout_s=4)
    pids = read_pids(draft.dir)
    assert all(pid_alive(p) for p in pids)
    final = jobs.wait(ref["job_id"], 60)
    assert final["status"] == "TIMED_OUT"
    receipt = jobs.receipt(ref["job_id"])
    assert receipt["process_tree"]["killed_by_runner"] is True and receipt["process_tree"]["active_after"] == 0
    wait_for(lambda: not any(pid_alive(p) for p in pids), 10)


def test_cancel_running_job_kills_children(tmp_path):
    jobs = service(tmp_path)
    ref, draft = submit(jobs, TREE_APP, timeout_s=300)
    pids = read_pids(draft.dir)
    with pytest.raises(ToolFailure) as short:
        jobs.cancel(ref["job_id"], "short")
    assert short.value.code == "INVALID_ARGUMENT"
    out = jobs.cancel(ref["job_id"], "cancel test: the fake app runs too long")
    assert out["status"] == "CANCELLED" and out["cancel_requested"] is True
    wait_for(lambda: not any(pid_alive(p) for p in pids), 10)
    again = jobs.cancel(ref["job_id"], "cancel test: second request is a no-op")
    assert again["already_terminal"] is True and again["status"] == "CANCELLED"


def test_leftover_children_are_killed_after_grace(tmp_path):
    jobs = service(tmp_path)
    ref, draft = submit(jobs, ORPHAN_APP, tree_grace_s=1.0, wait_s=60)
    assert ref["status"] == "SUCCEEDED"
    [child] = read_pids(draft.dir)
    receipt = jobs.receipt(ref["job_id"])
    assert receipt["process_tree"]["leftover_processes_killed"] >= 1
    wait_for(lambda: not pid_alive(child), 10)


def test_pool_queue_is_fifo_and_exclusive(tmp_path):
    jobs = service(tmp_path)
    first, first_draft = submit(jobs, "import time; time.sleep(10)", pool="qtest")    # long enough on slow disks
    wait_for(lambda: jobs.status(first["job_id"])["status"] == "RUNNING")
    second, _ = submit(jobs, "print('second')", pool="qtest")
    third, _ = submit(jobs, "print('third')", pool="qtest")
    wait_for(lambda: jobs.status(third["job_id"]).get("queue_position") == 1)
    assert jobs.status(second["job_id"])["status"] == "QUEUED"
    assert jobs.status(second["job_id"])["queue_position"] == 0
    pool = next(p for p in jobs.pools() if p["pool"] == "qtest")
    assert pool["busy"] == 1 and pool["holders"][0]["job_id"] == first["job_id"]
    assert pool["queued"] == [second["job_id"], third["job_id"]]
    done = [jobs.wait(j["job_id"], 60) for j in (first, second, third)]
    assert [d["status"] for d in done] == ["SUCCEEDED"] * 3
    r1, r2 = jobs.receipt(first["job_id"]), jobs.receipt(second["job_id"])
    assert r2["started_at"] >= r1["ended_at"] and r2["queue_wait_s"] > 1.0


def test_cancel_while_queued(tmp_path):
    jobs = service(tmp_path)
    first, _ = submit(jobs, "import time; time.sleep(10)", pool="qtest")
    wait_for(lambda: jobs.status(first["job_id"])["status"] == "RUNNING")
    queued, _ = submit(jobs, "print('never')", pool="qtest")
    wait_for(lambda: jobs.status(queued["job_id"]).get("queue_position") == 0)
    out = jobs.cancel(queued["job_id"], "cancel test: leave the queue")
    assert out["status"] == "CANCELLED"
    assert jobs.receipt(queued["job_id"])["exit_code"] is None
    assert jobs.wait(first["job_id"], 60)["status"] == "SUCCEEDED"


def test_gate_waits_for_a_live_session(tmp_path):
    import subprocess

    jobs = service(tmp_path)
    holder = subprocess.Popen([PY, "-c", "import time; time.sleep(60)"])
    try:
        pid_dir = jobs.root.path / "matlab" / "session" / "active"
        pid_dir.mkdir(parents=True)
        (pid_dir / "session.json").write_text(json.dumps({"pid": holder.pid}), encoding="utf-8")
        ref, _ = submit(jobs, "print('after the session')", gate={"pid_files": ["matlab/session/active/*.json"]})
        wait_for(lambda: jobs.status(ref["job_id"]).get("gated") is True)
        assert jobs.status(ref["job_id"])["status"] == "QUEUED"
    finally:
        holder.kill()
        holder.wait()
    assert jobs.wait(ref["job_id"], 60)["status"] == "SUCCEEDED"


def test_lost_runner_is_detected(tmp_path):
    jobs = service(tmp_path)
    ref, draft = submit(jobs, TREE_APP, timeout_s=300)
    pids = read_pids(draft.dir)
    runner_pid = wait_for(lambda: (read_json(draft.dir / "status.json") or {}).get("runner", {}).get("pid"))
    os.kill(runner_pid, signal.SIGTERM if os.name == "nt" else signal.SIGKILL)
    status = wait_for(lambda: (lambda s: s if s["status"] == "LOST" else None)(jobs.status(ref["job_id"])), 30)
    assert "runner" in status["reason"]
    assert jobs.receipt(ref["job_id"])["status"] == "LOST"
    if os.name == "nt":                                   # KILL_ON_JOB_CLOSE took the application with the runner
        wait_for(lambda: not any(pid_alive(p) for p in pids), 10)
    else:
        for p in pids:
            try:
                os.kill(p, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_spawn_that_never_starts_becomes_lost(tmp_path, monkeypatch):
    import vkm_jobs.service as svc

    jobs = service(tmp_path)
    jobs._spawner = lambda argv, cwd, env, log: (0, "TEST_NOT_STARTED")       # the runner never runs
    ref, _ = submit(jobs, "print(1)")
    assert jobs.status(ref["job_id"])["status"] == "QUEUED"                   # within the spawn grace period
    monkeypatch.setattr(svc, "SPAWN_GRACE_S", -1.0)
    assert jobs.status(ref["job_id"])["status"] == "LOST"


def test_timing_of_a_quick_job_is_reasonable(tmp_path):
    jobs = service(tmp_path)
    t0 = time.monotonic()
    ref, _ = submit(jobs, "print(1)", wait_s=60)
    assert ref["status"] == "SUCCEEDED" and time.monotonic() - t0 < 20

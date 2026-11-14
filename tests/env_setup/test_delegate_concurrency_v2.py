"""No API/key access: real spawned processes exercise one synthetic shared ledger."""
from contextlib import contextmanager
from decimal import Decimal
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = Path(os.environ.get("VKM_DELEGATE_CONCURRENCY_SCRIPT", ROOT / ".agents/skills/openrouter-delegate/scripts/openrouter_call.py"))


def module(path=SCRIPT):
    spec = importlib.util.spec_from_file_location("concurrency_delegate", path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def worker(script, ledger, gate, pipe, amount, profile, budget, exit_mode="hold"):
    d = module(script)
    gate.wait(15)
    try:
        token = d.reserve(ledger, budget, "same-key", "100", amount, profile=profile)
        pipe.send({"status": "RESERVED", "token": token, "pid": os.getpid()})
        if exit_mode == "crash":
            os._exit(7)
        if exit_mode == "clean_exit":
            return
        if pipe.poll(25):
            pipe.recv()
    except d.DelegateError as error:
        pipe.send({"status": error.code, "pid": os.getpid()})
    finally:
        pipe.close()


@contextmanager
def workers(ledger, requests, budget="20", exit_mode="hold"):
    context = multiprocessing.get_context("spawn")
    gate = context.Event()
    processes, pipes = [], []
    for amount, profile in requests:
        parent, child = context.Pipe(duplex=True)
        process = context.Process(target=worker, args=(str(SCRIPT), str(ledger), gate, child, amount, profile, budget, exit_mode))
        process.start()
        child.close()
        processes.append(process)
        pipes.append(parent)
    gate.set()
    try:
        results = []
        for pipe in pipes:
            assert pipe.poll(20), "worker failed to report"
            results.append(pipe.recv())
        yield results, processes
    finally:
        for process, pipe in zip(processes, pipes):
            if process.is_alive():
                try:
                    pipe.send("release")
                except (OSError, BrokenPipeError):
                    pass
            process.join(15)
            if process.is_alive():
                process.terminate()
                process.join(5)
        for pipe in pipes:
            pipe.close()


def read(path):
    return json.loads(path.read_text())


def test_two_confirmed_live_processes_can_hold_reservations(tmp_path):
    d, ledger = module(), tmp_path / "parallel.json"
    with workers(ledger, [(".1", "cheap_code"), (".3", "vision_documents")]) as (results, processes):
        assert [r["status"] for r in results] == ["RESERVED", "RESERVED"]
        assert all(process.is_alive() for process in processes)
        state = read(ledger)
        assert len(state["reservations"]) == len(state["reservation_leases"]) == 2
        assert all(isinstance(value, str) for value in state["reservations"].values())
        assert all(d._lease_is_confirmed_active(lease) for lease in state["reservation_leases"].values())
        assert sum(map(Decimal, state["reservations"].values())) == Decimal(".4")


def test_contested_last_money_has_one_winner_under_mutex(tmp_path):
    ledger = tmp_path / "last.json"
    with workers(ledger, [("1", "engineering_research"), ("1", "engineering_research")], budget="1") as (results, _):
        assert sorted(r["status"] for r in results) == ["BUDGET_EXCEEDED", "RESERVED"]
        assert len(read(ledger)["reservations"]) == 1


def test_three_profiles_share_the_twenty_dollar_ceiling(tmp_path):
    d, ledger = module(), tmp_path / "profiles.json"
    # Synthetic settled history: count and spent are never reset for the V2 batch.
    for amount in ["1"] * 18 + [".6"]:
        token = d.reserve(ledger, "20", "same-key", "100", amount)
        d.settle(ledger, token, amount)
    with workers(ledger, [(".1", "cheap_code"), (".3", "vision_documents"), ("1", "engineering_research")]) as (results, _):
        assert all(result["status"] == "RESERVED" for result in results)
        state = read(ledger)
        assert Decimal(state["spent_usd"]) + sum(map(Decimal, state["reservations"].values())) == Decimal("20")
        assert state["reservation_count"] == 22
        with pytest.raises(d.DelegateError, match="BUDGET_EXCEEDED"):
            d.reserve(ledger, "20", "same-key", "100", ".001", profile="cheap_code")


@pytest.mark.parametrize("exit_mode", ["clean_exit", "crash", "terminate"])
def test_dead_or_crashed_owner_never_frees_pending_money(tmp_path, exit_mode):
    d, ledger = module(), tmp_path / "dead.json"
    with workers(ledger, [(".1", "cheap_code")], exit_mode="hold" if exit_mode == "terminate" else exit_mode) as (results, processes):
        assert results[0]["status"] == "RESERVED"
        if exit_mode == "terminate":
            processes[0].terminate()
        processes[0].join(10)
        assert not processes[0].is_alive()
        before = ledger.read_bytes()
        with pytest.raises(d.DelegateError, match="PENDING_EXECUTION_COST"):
            d.reserve(ledger, "20", "same-key", "100", ".01")
        assert ledger.read_bytes() == before  # no automatic recovery/removal


@pytest.mark.parametrize("corruption", ["reused_pid_birth", "expired", "legacy", "unknown", "foreign_host"])
def test_unconfirmed_active_lease_cannot_enable_new_expenses(tmp_path, corruption):
    d, ledger = module(), tmp_path / "unconfirmed.json"
    token = d.reserve(ledger, "20", "same-key", "100", ".1")
    state = read(ledger)
    lease = state["reservation_leases"][token]
    if corruption == "reused_pid_birth":
        assert lease["pid"] == os.getpid() and d._process_birth_identity(os.getpid())
        lease["process_birth"] += ":old-incarnation"
    elif corruption == "expired":
        lease.update(created_unix=time.time()-10, deadline_unix=time.time()-1,
                     created_monotonic=time.monotonic()-10, deadline_monotonic=time.monotonic()-1)
    elif corruption == "legacy":
        del state["reservation_leases"]
    elif corruption == "unknown":
        lease["state"] = "UNKNOWN"
    else:
        lease["host"] = "unconfirmed-other-host"
    d.write_json(ledger, state)
    before = ledger.read_bytes()
    with pytest.raises(d.DelegateError, match="PENDING_EXECUTION_COST"):
        d.reserve(ledger, "20", "same-key", "100", ".01")
    assert ledger.read_bytes() == before


def test_unknown_cost_blocks_and_known_cost_cannot_forgive_another_token(tmp_path):
    d, ledger = module(), tmp_path / "unknown.json"
    unknown = d.reserve(ledger, "20", "same-key", "100", ".1")
    known = d.reserve(ledger, "20", "same-key", "100", ".2")
    d.settle(ledger, unknown, None)
    d.settle(ledger, known, ".02")
    state = read(ledger)
    assert state["blocked"] and state["reservation_leases"][unknown]["state"] == "UNKNOWN"
    assert set(state["reservations"]) == {unknown}
    assert Decimal(state["reservations"][unknown]) == Decimal(".1")
    assert known not in state["reservation_leases"]
    assert Decimal(state["spent_usd"]) == Decimal(".02") and state["reservation_count"] == 2
    with pytest.raises(d.DelegateError, match="UNKNOWN_EXECUTION_COST"):
        d.reserve(ledger, "20", "same-key", "100", ".01")


def test_over_reserve_cost_and_invalid_cost_are_sticky_blockers(tmp_path):
    d, ledger = module(), tmp_path / "over.json"
    token = d.reserve(ledger, "20", "same-key", "100", ".1")
    d.settle(ledger, token, ".11")
    state = read(ledger)
    assert state["blocked"] and not state["reservations"] and not state["reservation_leases"]
    assert Decimal(state["spent_usd"]) == Decimal(".11")
    invalid = tmp_path / "invalid.json"
    token = d.reserve(invalid, "20", "same-key", "100", ".1")
    with pytest.raises(d.DelegateError, match="INVALID_COST"):
        d.settle(invalid, token, "NaN")
    state = read(invalid)
    assert state["blocked"] and state["reservation_leases"][token]["state"] == "UNKNOWN"
    assert token in state["reservations"]


def test_same_key_budget_contract_and_verified_remaining(tmp_path):
    d, ledger = module(), tmp_path / "contract.json"
    d.reserve(ledger, "20", "same-key", "100", ".1")
    before = ledger.read_bytes()
    for budget, key in [("19", "same-key"), ("20", "other-key")]:
        with pytest.raises(d.DelegateError, match="LEDGER_CONTRACT_MISMATCH"):
            d.reserve(ledger, budget, key, "100", ".01")
    with pytest.raises(d.DelegateError, match="BUDGET_EXCEEDED"):
        d.reserve(ledger, "20", "same-key", ".11", ".02")
    assert ledger.read_bytes() == before


@pytest.mark.parametrize("remaining", [None, "0", "NaN", "Infinity", "-1", True])
def test_normal_mode_requires_positive_finite_observed_key_remaining(tmp_path, remaining):
    d = module()
    with pytest.raises(d.DelegateError, match="LIVE_BLOCKED_KEY_LIMIT"):
        d.reserve(tmp_path / "never.json", "20", "same-key", remaining, ".01")
    assert not (tmp_path / "never.json").exists()


@pytest.mark.parametrize("amount", ["0", "-1", "NaN", "Infinity", True])
def test_reservation_amount_must_be_positive_finite(tmp_path, amount):
    d = module()
    with pytest.raises(d.DelegateError):
        d.reserve(tmp_path / "never.json", "20", "same-key", "100", amount)
    assert not (tmp_path / "never.json").exists()


def test_profile_caps_and_global_budget_cannot_expand(tmp_path):
    d = module()
    for profile, amount in [("cheap_code", ".1000001"), ("vision_documents", ".300001"), ("engineering_research", "1.00001")]:
        with pytest.raises(d.DelegateError, match="PER_CALL_CAP_EXCEEDED"):
            d.reserve(tmp_path / "never.json", "20", "same-key", "100", amount, profile=profile)
    with pytest.raises(d.DelegateError, match="LIVE_BLOCKED_BUDGET"):
        d.reserve(tmp_path / "never.json", "20.00001", "same-key", "100", ".01")


def test_empty_legacy_migration_preserves_spent_and_lifetime_count(tmp_path):
    d, ledger = module(), tmp_path / "legacy.json"
    d.write_json(ledger, {"key_id":"same-key","budget_usd":"20","spent_usd":".25","reservations":{},"blocked":False,"reservation_count":12})
    token = d.reserve(ledger, "20", "same-key", "100", ".1")
    state = read(ledger)
    assert state["spent_usd"] == ".25" and state["reservation_count"] == 13
    assert state["reservation_leases"][token]["state"] == "ACTIVE"


def test_missing_lifetime_counter_is_unknown_not_a_reset_to_zero(tmp_path):
    d, ledger = module(), tmp_path / "missing-count.json"
    d.write_json(ledger, {"key_id":"same-key","budget_usd":"20","spent_usd":".25","reservations":{},"blocked":False})
    before = ledger.read_bytes()
    with pytest.raises(d.DelegateError, match="LEDGER_CONTRACT_MISMATCH"):
        d.reserve(ledger, "20", "same-key", "100", ".1")
    assert ledger.read_bytes() == before


def test_legacy_three_smokes_remain_serial_and_lifetime_bounded(tmp_path):
    d, ledger = module(), tmp_path / "smokes.json"
    token = d.reserve(ledger, "1", "same-key", None, ".1", owner_local_cap_only=True)
    with pytest.raises(d.DelegateError, match="PENDING_EXECUTION_COST"):
        d.reserve(ledger, "1", "same-key", None, ".1", owner_local_cap_only=True)
    d.settle(ledger, token, ".01")
    for _ in range(2):
        token = d.reserve(ledger, "1", "same-key", None, ".1", owner_local_cap_only=True)
        d.settle(ledger, token, ".01")
    with pytest.raises(d.DelegateError, match="OWNER_SMOKE_LIMIT_REACHED"):
        d.reserve(ledger, "1", "same-key", None, ".1", owner_local_cap_only=True)
    assert read(ledger)["reservation_count"] == 3


def test_unsupported_process_identity_fails_closed(tmp_path, monkeypatch):
    d = module()
    monkeypatch.setattr(d, "_process_birth_identity", lambda pid: None)
    with pytest.raises(d.DelegateError, match="PROCESS_IDENTITY_UNCONFIRMED"):
        d.reserve(tmp_path / "never.json", "20", "same-key", "100", ".01")
    assert not (tmp_path / "never.json").exists()


def test_stale_and_replaced_locks_are_never_removed(tmp_path):
    d, ledger = module(), tmp_path / "lock.json"
    lock = Path(str(ledger) + ".lock")
    lock.write_text("foreign-stale-lock")
    with pytest.raises(d.DelegateError, match="LEDGER_LOCKED"):
        d.reserve(ledger, "20", "same-key", "100", ".01")
    assert lock.read_text() == "foreign-stale-lock"
    # Only remove this test's own fixture, never a production lock.
    lock.unlink()
    with pytest.raises(d.DelegateError, match="LEDGER_LOCK_OWNERSHIP_LOST"):
        with d.locked(ledger):
            lock.write_text("foreign-replacement")
    assert lock.read_text() == "foreign-replacement"


def test_precise_held_sum_never_rounds_away_last_fraction(tmp_path):
    d, ledger = module(), tmp_path / "precise.json"
    d.reserve(ledger, "1", "same-key", "100", ".999999999999999999999999999999999999")
    with pytest.raises(d.DelegateError, match="BUDGET_EXCEEDED"):
        d.reserve(ledger, "1", "same-key", "100", ".000000000000000000000000000000000002")

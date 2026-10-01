import copy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from vkm_corpus.parquet.atomic import write_bytes
from vkm_corpus.update.barrier import BarrierUnavailable, ReceiverBarrier
from vkm_corpus.update.contracts import ComponentIdentity, GenerationManifest
from vkm_corpus.update.deployment import (DeploymentProfile, DurableDeployment,
                                         ReceiverControl, SelectorAdapter)
from vkm_corpus.update.generation import GenerationUnavailable
from vkm_evidence.contracts import canonical_bytes, record_hash


def manifest(epoch):
    return GenerationManifest(code_commit="a" * 40, policy_sha256="b" * 64, acceptance_sha256="c" * 64,
        components=(ComponentIdentity(component="DOCUMENT", revision=epoch, manifest_sha256="d" * 64,
            policy_sha256="b" * 64, built_from={}),
            ComponentIdentity(component="DUCKDB", revision=epoch, manifest_sha256="e" * 64,
                policy_sha256="b" * 64, built_from={"DOCUMENT": epoch})))


def rig(tmp_path, *, fault=None, restore_failure=False, scope="SYNTHETIC"):
    previous, candidate = manifest("old"), manifest("new")
    root = tmp_path / "served"
    root.mkdir()
    write_bytes(root / "tmp", root / (previous.sha256 + ".json"), canonical_bytes(previous))
    write_bytes(root / "tmp", root / "CURRENT", (previous.sha256 + "\n").encode())
    state = {c.component: c.model_dump(mode="json") for c in previous.components}
    calls = []
    barrier = ReceiverBarrier("receiver")
    profile = DeploymentProfile(scope=scope, code_commit="a" * 40, code_tree_sha256="a" * 64,
        dependencies_sha256="b" * 64, access_config_sha256="c" * 64, policy_sha256="b" * 64,
        deployment_profile_sha256="d" * 64, receiver_ids=("receiver",), adapter_ids=("document", "duckdb"))
    def adapter(name, component):
        def capture():
            return copy.deepcopy(state[component])
        def apply(selected, request):
            calls.append(("apply", name, request))
            state[component] = next(c.model_dump(mode="json") for c in selected.components if c.component == component)
        def restore(binding, request):
            calls.append(("restore", name, request))
            if restore_failure:
                raise RuntimeError("synthetic unavailable restore")
            state[component] = copy.deepcopy(binding)
        return SelectorAdapter(name, capture, apply, restore)
    def rebind(selected):
        assert barrier.status()["paused"]
        assert not (root / "MAINTENANCE").exists()
        assert (root / "CURRENT").read_text().strip() == selected.sha256
        calls.append(("rebind", selected.sha256))
    def qualify(selected):
        assert selected.acceptance_sha256 == "c" * 64
    def probe(selected):
        assert (root / "MAINTENANCE").exists() and barrier.status()["paused"]
        assert state == {c.component: c.model_dump(mode="json") for c in selected.components}
    controller = DurableDeployment(root, adapters=(adapter("document", "DOCUMENT"), adapter("duckdb", "DUCKDB")),
        receivers=(ReceiverControl(barrier, rebind),), observer=lambda: copy.deepcopy(state),
        identity_provider=lambda: profile, qualification=qualify, candidate_probe=probe,
        drain_timeout_seconds=.01, fault=fault)
    return controller, previous, candidate, state, calls, barrier


def test_transaction_persists_bindings_drains_and_retries_lost_ack_without_mutation(tmp_path):
    controller, old, new, state, calls, barrier = rig(tmp_path)
    plan = controller.plan(new, "update")
    result = controller.switch(new, "update", plan["plan_sha256"])
    assert result["status"] == "PASS" and result["scope"] == "SYNTHETIC"
    assert (controller.root / "CURRENT").read_text().strip() == new.sha256
    assert not (controller.root / "MAINTENANCE").exists() and not barrier.status()["paused"]
    history = controller.history("update")
    assert history[0]["phase"] == "PREPARED" and history[-1]["phase"] == "FINISHED"
    count = len(calls)
    assert controller.switch(new, "update", plan["plan_sha256"]) == result
    assert len(calls) == count
    with pytest.raises(GenerationUnavailable, match="different deployment"):
        controller.plan(old, "update")


@pytest.mark.parametrize("phase", ["DRAINED", "APPLIED:document", "APPLIED:duckdb", "CURRENT_WRITTEN"])
def test_process_interruption_restores_exact_previous_selectors_from_intent(tmp_path, phase):
    def crash(current):
        if current == phase:
            raise KeyboardInterrupt("simulated process interruption")
    controller, old, new, state, calls, barrier = rig(tmp_path, fault=crash)
    plan = controller.plan(new, "update")
    with pytest.raises(KeyboardInterrupt):
        controller.switch(new, "update", plan["plan_sha256"])
    assert (controller.root / "MAINTENANCE").exists()
    with pytest.raises(GenerationUnavailable, match="explicit recovery"):
        controller.switch(new, "update", plan["plan_sha256"])
    controller.fault = None
    recovery = controller.recovery_plan("update", mode="RESTORE_PREVIOUS")
    restored = controller.recover("update", recovery["recovery_sha256"])
    assert restored["status"] == "RESTORED" and restored["generation_sha256"] == old.sha256
    assert state == {c.component: c.model_dump(mode="json") for c in old.components}
    assert (controller.root / "CURRENT").read_text().strip() == old.sha256
    assert not barrier.status()["paused"]


def test_resume_candidate_after_crash_requires_fresh_native_and_qualified_candidate(tmp_path):
    def crash(phase):
        if phase == "CURRENT_WRITTEN":
            raise KeyboardInterrupt()
    controller, old, new, *_ = rig(tmp_path, fault=crash)
    plan = controller.plan(new, "update")
    with pytest.raises(KeyboardInterrupt):
        controller.switch(new, "update", plan["plan_sha256"])
    controller.fault = None
    recovery = controller.recovery_plan("update", mode="COMPLETE_CANDIDATE")
    result = controller.recover("update", recovery["recovery_sha256"], mode="COMPLETE_CANDIDATE")
    assert result["status"] == "PASS" and (controller.root / "CURRENT").read_text().strip() == new.sha256


def test_drain_timeout_does_not_mutate_selectors_and_leaves_service_closed(tmp_path):
    controller, old, new, state, calls, barrier = rig(tmp_path)
    lease = barrier.acquire()
    plan = controller.plan(new, "update")
    with pytest.raises(RuntimeError, match="drain"):
        controller.switch(new, "update", plan["plan_sha256"])
    assert not any(c[0] in {"apply", "restore"} for c in calls)
    assert (controller.root / "MAINTENANCE").exists()
    assert state == {c.component: c.model_dump(mode="json") for c in old.components}
    lease.release()


def test_failed_restore_never_reopens_receivers(tmp_path):
    controller, _, new, state, _, barrier = rig(tmp_path, restore_failure=True,
        fault=lambda phase: (_ for _ in ()).throw(RuntimeError()) if phase == "APPLIED:document" else None)
    plan = controller.plan(new, "update")
    with pytest.raises(RuntimeError, match="unavailable restore"):
        controller.switch(new, "update", plan["plan_sha256"])
    assert barrier.status()["paused"] and (controller.root / "MAINTENANCE").exists()


def test_fresh_confirmation_and_live_writer_lease_required(tmp_path):
    controller, old, new, _, calls, _ = rig(tmp_path)
    with pytest.raises(GenerationUnavailable, match="confirmation"):
        controller.switch(new, "update", "f" * 64)
    assert calls == [] and not (controller.root / "MAINTENANCE").exists()
    with pytest.raises(GenerationUnavailable, match="live exclusive writer"):
        controller.writer_fence("update")


def test_intent_and_event_tampering_cannot_change_rollback_target(tmp_path):
    controller, old, new, *_ = rig(tmp_path, fault=lambda phase:
        (_ for _ in ()).throw(KeyboardInterrupt()) if phase == "DRAINED" else None)
    plan = controller.plan(new, "update")
    with pytest.raises(KeyboardInterrupt):
        controller.switch(new, "update", plan["plan_sha256"])
    key = controller._request_key("update")
    path = controller.root / "requests" / (key + ".json")
    body = json.loads(path.read_bytes())
    body["previous"] = new.model_dump(mode="json")
    path.write_bytes(canonical_bytes(body))
    with pytest.raises(GenerationUnavailable, match="intent bytes"):
        controller.recovery_plan("update", mode="RESTORE_PREVIOUS")
    path.write_bytes(canonical_bytes(plan))
    event = controller.history("update")[-1]
    (controller.root / "journals" / (event["journal_sha256"] + ".json")).write_text("{}")
    with pytest.raises(GenerationUnavailable, match="ownership or bytes"):
        controller.history("update")


def test_actual_partial_apply_drill_restores_and_preserves_synthetic_scope(tmp_path):
    controller, old, new, _, calls, barrier = rig(tmp_path)
    proof = controller.rehearse_failure(new, "drill", fail_after_adapter="document")
    assert proof["schema"] == "vkm-deployment-drill/1" and proof["scope"] == "SYNTHETIC"
    assert set(proof["checks"].values()) == {"PASS"}
    assert proof["native_before_sha256"] == proof["native_after_restore_sha256"]
    assert any(c[0] == "apply" for c in calls) and any(c[0] == "restore" for c in calls)
    assert not barrier.status()["paused"]


def test_production_selectors_never_allow_fault_injection(tmp_path):
    controller, old, new, *_ = rig(tmp_path, scope="PRODUCTION_SWITCH")
    refusal = "isolated selectors" if os.name == "posix" else "not qualified on this OS"
    with pytest.raises(GenerationUnavailable, match=refusal):
        controller.rehearse_failure(new, "drill", fail_after_adapter="document")


def interrupt_before_prepared(controller, candidate, request_id, monkeypatch):
    plan = controller.plan(candidate, request_id)
    event = controller._event
    def crash(key, phase, **kwargs):
        if phase == "PREPARED":
            raise KeyboardInterrupt("request persisted; PREPARED not acknowledged")
        return event(key, phase, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(controller, "_event", crash)
        with pytest.raises(KeyboardInterrupt):
            controller.switch(candidate, request_id, plan["plan_sha256"])
    assert (controller.root / "requests" / (controller._request_key(request_id) + ".json")).is_file()
    assert controller.history(request_id) == []
    return plan


def test_orphan_intent_cannot_rollback_a_later_successful_deployment(tmp_path, monkeypatch):
    controller, old, new, state, calls, _ = rig(tmp_path)
    orphan = interrupt_before_prepared(controller, new, "request-a", monkeypatch)
    third = manifest("third")
    plan_b = controller.plan(third, "request-b")
    assert controller.switch(third, "request-b", plan_b["plan_sha256"])["status"] == "PASS"
    after_b = copy.deepcopy(calls)
    controller.fault = lambda phase: (_ for _ in ()).throw(RuntimeError("would trigger stale rollback")) if phase == "APPLIED:document" else None
    with pytest.raises(GenerationUnavailable, match="stale previous"):
        controller.switch(new, "request-a", orphan["plan_sha256"])
    assert calls == after_b
    assert controller._current().sha256 == third.sha256
    assert state == {c.component: c.model_dump(mode="json") for c in third.components}
    assert not (controller.root / "MAINTENANCE").exists()


def test_orphan_intent_same_exact_previous_state_can_resume(tmp_path, monkeypatch):
    controller, old, new, state, calls, _ = rig(tmp_path)
    orphan = interrupt_before_prepared(controller, new, "request-a", monkeypatch)
    assert calls == [] and controller._current().sha256 == old.sha256
    assert controller.switch(new, "request-a", orphan["plan_sha256"])["status"] == "PASS"
    assert controller._current().sha256 == new.sha256
    assert sum(c[0] == "apply" for c in calls) == 2


def test_rehashed_mutable_rollback_intent_is_rejected_by_original_journal_pin(tmp_path):
    controller, old, new, state, calls, _ = rig(tmp_path, fault=lambda phase:
        (_ for _ in ()).throw(KeyboardInterrupt()) if phase == "DRAINED" else None)
    plan = controller.plan(new, "update")
    with pytest.raises(KeyboardInterrupt):
        controller.switch(new, "update", plan["plan_sha256"])
    original_history = controller.history("update")
    assert original_history[0]["detail"]["plan_sha256"] == plan["plan_sha256"]
    path = controller.root / "requests" / (controller._request_key("update") + ".json")
    forged = json.loads(path.read_bytes())
    forged["previous"] = new.model_dump(mode="json")
    forged["bindings"] = {name: next(c.model_dump(mode="json") for c in new.components if c.component == component)
        for name, component in (("document", "DOCUMENT"), ("duckdb", "DUCKDB"))}
    forged["native_before_sha256"] = record_hash({"components": {c.component: c.model_dump(mode="json") for c in new.components}, "services": {}})
    forged["plan_sha256"] = record_hash({k: v for k, v in forged.items() if k != "plan_sha256"})
    path.write_bytes(canonical_bytes(forged))
    controller.fault = None
    with pytest.raises(GenerationUnavailable, match="original rollback intent"):
        controller.recovery_plan("update", mode="RESTORE_PREVIOUS")
    assert controller.history("update") == original_history
    assert calls == [] and controller._current().sha256 == old.sha256
    assert (controller.root / "MAINTENANCE").exists()


@pytest.mark.parametrize("delivery_failure", ["ACK", "RESULT_READ"])
def test_postcommit_delivery_failure_replays_without_rollback(tmp_path, monkeypatch, delivery_failure):
    controller, old, new, state, calls, barrier = rig(tmp_path)
    plan = controller.plan(new, "update")
    with monkeypatch.context() as patch:
        if delivery_failure == "ACK":
            patch.setattr(controller, "fault", lambda phase: (_ for _ in ()).throw(ConnectionError("lost ACK")) if phase == "ACK" else None)
        else:
            patch.setattr(controller, "_result", lambda *args: (_ for _ in ()).throw(OSError("receipt read unavailable")))
        with pytest.raises((ConnectionError, OSError)):
            controller.switch(new, "update", plan["plan_sha256"])
    assert controller.history("update")[-1]["phase"] == "FINISHED"
    assert controller._current().sha256 == new.sha256
    assert not any(c[0] == "restore" for c in calls)
    assert not barrier.status()["paused"] and not (controller.root / "MAINTENANCE").exists()
    completed_calls = copy.deepcopy(calls)
    result = controller.switch(new, "update", plan["plan_sha256"])
    assert result["status"] == "PASS" and calls == completed_calls


@pytest.mark.skipif(os.name != "posix", reason="NOT_RUN: POSIX shared admission gate")
@pytest.mark.parametrize("receiver_id", ["receiver", "late-receiver"])
def test_late_or_replacement_receiver_stays_closed_through_failed_rebind_and_restore(tmp_path, receiver_id):
    controller, old, new, state, calls, barrier = rig(tmp_path)
    late = ReceiverBarrier(receiver_id, gate_path=controller.root / "admission.lock")
    initial_rebind = controller.receivers["receiver"].rebind
    checks = []
    def ensure_closed(label):
        with pytest.raises(BarrierUnavailable):
            with late.acquire():
                pytest.fail("new receiver admitted during selector transaction")
        checks.append(label)
    def rebind(selected):
        # CURRENT is readable for strict rebind, but kernel admission stays shut.
        assert controller.coordinator.status(lambda: state)["status"] == "READY"
        ensure_closed("rebind")
        if selected.sha256 == new.sha256:
            raise RuntimeError("synthetic receiver restart failure")
        initial_rebind(selected)
    controller.receivers["receiver"] = ReceiverControl(barrier, rebind)
    for name, adapter in tuple(controller.adapters.items()):
        def restore(binding, request, adapter=adapter):
            ensure_closed("restore")
            return adapter.restore(binding, request)
        controller.adapters[name] = SelectorAdapter(adapter.adapter_id, adapter.capture, adapter.apply, restore)
    plan = controller.plan(new, "update")
    with pytest.raises(RuntimeError, match="restart failure"):
        controller.switch(new, "update", plan["plan_sha256"])
    assert checks.count("restore") == 2 and checks.count("rebind") == 2
    assert controller._current().sha256 == old.sha256
    with late.acquire():
        assert late.status()["active_requests"] == 1


@pytest.mark.skipif(os.name != "posix", reason="NOT_RUN: POSIX shared admission gate")
def test_unregistered_process_request_must_drain_before_any_selector_mutation(tmp_path):
    controller, old, new, state, calls, _ = rig(tmp_path)
    child = """from pathlib import Path
import sys
from vkm_corpus.update.barrier import ReceiverBarrier
barrier = ReceiverBarrier('separate-process', gate_path=Path(sys.argv[1]))
with barrier.acquire():
    print('HELD', flush=True)
    sys.stdin.readline()
"""
    env = dict(os.environ)
    env.update(PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"),
               PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
    process = subprocess.Popen([sys.executable, "-c", child, str(controller.root / "admission.lock")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        ready = process.stdout.readline().strip()
        if ready != "HELD":
            _, errors = process.communicate(timeout=10)
            pytest.fail("synthetic receiver did not start: " + errors[-1000:])
        plan = controller.plan(new, "update")
        with pytest.raises(GenerationUnavailable, match="requests did not drain"):
            controller.switch(new, "update", plan["plan_sha256"])
        assert not any(c[0] in {"apply", "restore"} for c in calls)
        assert controller._current().sha256 == old.sha256 and (controller.root / "MAINTENANCE").exists()
        process.communicate("release\n", timeout=10)
        assert process.returncode == 0
        recovery = controller.recovery_plan("update", mode="RESTORE_PREVIOUS")
        assert controller.recover("update", recovery["recovery_sha256"])["status"] == "RESTORED"
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)


@pytest.mark.skipif(os.name != "posix", reason="NOT_RUN: POSIX shared admission gate")
def test_replaced_admission_inode_invalidates_writer_fence_before_mutation(tmp_path):
    controller, old, new, state, calls, _ = rig(tmp_path)
    checks = []
    def replace_gate(phase):
        if phase != "DRAINED":
            return
        gate = controller.root / "admission.lock"
        gate.rename(controller.root / "replaced-admission.lock")
        gate.write_bytes(b"")
        with pytest.raises(GenerationUnavailable, match="gate|admission"):
            controller.writer_fence("update")
        checks.append("inode replacement rejected")
        raise KeyboardInterrupt("stop synthetic transaction before selectors")
    controller.fault = replace_gate
    plan = controller.plan(new, "update")
    with pytest.raises(KeyboardInterrupt):
        controller.switch(new, "update", plan["plan_sha256"])
    assert checks and not any(c[0] in {"apply", "restore"} for c in calls)
    assert controller._current().sha256 == old.sha256


@pytest.mark.skipif(os.name != "posix", reason="NOT_RUN: POSIX shared admission gate")
@pytest.mark.parametrize("replace_after", ["duckdb", "document"])
def test_rollback_rechecks_admission_before_each_selector_restore(tmp_path, monkeypatch, replace_after):
    controller, old, new, state, calls, _ = rig(tmp_path, fault=lambda phase:
        (_ for _ in ()).throw(RuntimeError("trigger rollback")) if phase == "APPLIED:duckdb" else None)
    adapter = controller.adapters[replace_after]
    selections = []
    select = controller._select_current
    def selected(manifest):
        selections.append(manifest.sha256)
        return select(manifest)
    monkeypatch.setattr(controller, "_select_current", selected)
    def restore_then_replace_gate(binding, request):
        adapter.restore(binding, request)
        gate = controller.root / "admission.lock"
        gate.rename(controller.root / "old-admission.lock")
        gate.write_bytes(b"")
    controller.adapters[replace_after] = SelectorAdapter(adapter.adapter_id, adapter.capture, adapter.apply, restore_then_replace_gate)
    plan = controller.plan(new, "update")
    with pytest.raises(GenerationUnavailable, match="gate|admission"):
        controller.switch(new, "update", plan["plan_sha256"])
    assert ("restore", "duckdb", "update") in calls
    if replace_after == "duckdb":
        assert ("restore", "document", "update") not in calls
        assert state["DOCUMENT"]["revision"] == "new"
    else:
        assert ("restore", "document", "update") in calls
        assert state["DOCUMENT"]["revision"] == "old"
    assert selections == []  # CURRENT is also a selector protected by the fence.
    assert (controller.root / "MAINTENANCE").exists()

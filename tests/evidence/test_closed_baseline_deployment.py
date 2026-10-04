"""Closed previous is restored as CLOSED, never silently promoted to serving."""
import json

import pytest

from test_deployment_lifecycle import rig
from test_durable_admission import replacement
from vkm_corpus.update.admission import AdmissionState, read_state
from vkm_corpus.update.barrier import BarrierUnavailable
from vkm_corpus.update.deployment import CLOSED_DRILL_CHECKS, PreviousAdmission, ReceiverControl
from vkm_corpus.update.generation import GenerationUnavailable
from vkm_evidence.contracts import canonical_bytes, record_hash


def closed_rig(tmp_path, *, fault=None):
    controller, old, new, state, calls, barrier = rig(tmp_path, fault=fault)
    policy = PreviousAdmission(mode="CLOSED_BASELINE", qualification_sha256="6" * 64,
                               closed_owner_key=record_hash("bootstrap-original-owner"))
    controller.previous_admission = lambda selected: policy if selected == old else PreviousAdmission()
    controller._publish_admission(AdmissionState(status="CLOSED", request_key=policy.closed_owner_key))
    original = controller.receivers["receiver"].rebind
    def rebind(selected):
        original(selected)
        if selected == old:
            assert read_state(controller.root).request_key == policy.closed_owner_key
        return {"synthetic_native": selected.sha256}
    controller.receivers["receiver"] = ReceiverControl(barrier, rebind)
    return controller, old, new, state, calls, barrier, policy


def assert_closed(controller, old, policy):
    assert controller._current() == old
    assert read_state(controller.root) == AdmissionState(status="CLOSED", request_key=policy.closed_owner_key)
    assert not (controller.root / "MAINTENANCE").exists()
    with pytest.raises(BarrierUnavailable):
        replacement(controller.root).acquire()


def test_closed_previous_can_deploy_a_qualified_candidate_without_opening_previous(tmp_path):
    controller, old, new, state, calls, barrier, policy = closed_rig(tmp_path)
    plan = controller.plan(new, "update")
    assert plan["previous_admission"] == policy.model_dump(mode="json")
    result = controller.switch(new, "update", plan["plan_sha256"])
    assert result["status"] == "PASS" and controller._current() == new
    with replacement(controller.root).acquire():
        pass


def test_partial_failure_restores_closed_owner_before_native_startup(tmp_path):
    controller, old, new, _, calls, barrier, policy = closed_rig(tmp_path, fault=lambda phase:
        (_ for _ in ()).throw(RuntimeError("partial apply")) if phase == "APPLIED:document" else None)
    plan = controller.plan(new, "update")
    with pytest.raises(RuntimeError, match="partial apply"):
        controller.switch(new, "update", plan["plan_sha256"])
    assert_closed(controller, old, policy)
    assert not barrier.status()["admission_open"] and not barrier.status()["paused"]
    assert controller._committed_closed(controller._request_key("update"), old, policy)
    assert controller.history("update")[-1]["phase"] == "FAILED_RESTORED"
    assert any(c[0] == "rebind" and c[1] == old.sha256 for c in calls)


@pytest.mark.parametrize("phase", ["APPLIED:document", "CURRENT_WRITTEN", "CLOSED_OWNER_RESTORED",
    "CLOSED_MAINTENANCE_REMOVED", "CLOSED_REBOUND:receiver", "CLOSED_RECEIVERS_VERIFIED"])
def test_interrupted_closed_restore_resumes_without_candidate_artifact(tmp_path, phase):
    def fault(at):
        if at == phase:
            raise KeyboardInterrupt("crash")
        if phase.startswith("CLOSED_") and at == "APPLIED:document":
            raise RuntimeError("partial apply")
    controller, old, new, _, calls, _, policy = closed_rig(tmp_path, fault=fault)
    plan = controller.plan(new, "update")
    with pytest.raises(KeyboardInterrupt):
        controller.switch(new, "update", plan["plan_sha256"])
    (controller.root / (new.sha256 + ".json")).write_bytes(b"corrupt candidate")
    controller.fault = None
    recover = controller.recovery_plan("update", mode="RESTORE_PREVIOUS")
    result = controller.recover("update", recover["recovery_sha256"])
    assert result["status"] == "RESTORED_CLOSED" and result["generation_sha256"] == old.sha256
    assert_closed(controller, old, policy)


@pytest.mark.parametrize("phase", ["CLOSED_RESTORE_COMMITTED", "RECOVERY_ACK"])
def test_closed_commit_lost_ack_does_not_restart_or_restore_twice(tmp_path, phase):
    def crash(at):
        if at == "APPLIED:document":
            raise RuntimeError("partial apply") if phase == "CLOSED_RESTORE_COMMITTED" else KeyboardInterrupt()
        if at == phase:
            raise KeyboardInterrupt("lost ack")
    controller, old, new, _, calls, _, policy = closed_rig(tmp_path, fault=crash)
    plan = controller.plan(new, "update")
    with pytest.raises(KeyboardInterrupt):
        controller.switch(new, "update", plan["plan_sha256"])
    controller.fault = None
    recover = controller.recovery_plan("update", mode="RESTORE_PREVIOUS")
    if phase == "RECOVERY_ACK":
        controller.fault = lambda at: (_ for _ in ()).throw(ConnectionError()) if at == phase else None
        with pytest.raises(ConnectionError):
            controller.recover("update", recover["recovery_sha256"])
    count = len(calls)
    result = controller.recover("update", recover["recovery_sha256"])
    assert result["status"] == "RESTORED_CLOSED" and len(calls) == count
    assert controller.recover("update", recover["recovery_sha256"]) == result
    assert_closed(controller, old, policy)
    with pytest.raises(GenerationUnavailable):
        controller.recover("update", "f" * 64)


def test_closed_drill_never_claims_active_drain_or_reopened_previous(tmp_path):
    controller, old, new, _, _, barrier, policy = closed_rig(tmp_path)
    proof = controller.rehearse_failure(new, "closed-drill", fail_after_adapter="document")
    assert proof["schema"] == "vkm-closed-baseline-drill/1"
    assert proof["previous_admission"] == policy.model_dump(mode="json")
    assert proof["checks"] == dict.fromkeys(CLOSED_DRILL_CHECKS, "PASS")
    assert "drained" not in proof["checks"] and "rollback_admission" not in proof["checks"]
    assert not barrier.status()["admission_open"]
    assert_closed(controller, old, policy)


@pytest.mark.parametrize("boundary", ["before_switch", "during_rebind", "before_commit"])
def test_frozen_previous_qualification_cannot_be_replaced(tmp_path, boundary):
    controller, old, new, _, calls, _, policy = closed_rig(tmp_path)
    plan = controller.plan(new, "update")
    def change():
        controller.previous_admission = lambda _: policy.model_copy(update={"qualification_sha256": "9" * 64})
    if boundary == "before_switch":
        # Persist the concrete reviewable plan; no mutation has happened yet.
        path = controller.root / "requests" / (controller._request_key("update") + ".json")
        path.parent.mkdir()
        path.write_bytes(canonical_bytes(plan))
        change()
    else:
        def fault(at):
            if at == "APPLIED:document":
                raise RuntimeError("partial apply")
            if at == ("CLOSED_REBOUND:receiver" if boundary == "during_rebind" else "CLOSED_RECEIVERS_VERIFIED"):
                change()
        controller.fault = fault
    with pytest.raises(GenerationUnavailable, match="policy|profile"):
        controller.switch(new, "update", plan["plan_sha256"])
    assert read_state(controller.root).status == "CLOSED"
    assert not replacement(controller.root).status()["admission_open"]
    if boundary == "before_switch":
        assert calls == []


@pytest.mark.parametrize("damage", ["marker", "event", "state", "current", "qualification"])
def test_closed_commit_is_not_a_bare_success_marker(tmp_path, damage):
    def fault(at):
        if at == "APPLIED:document":
            raise RuntimeError()
        if at == "CLOSED_RESTORE_COMMITTED":
            raise KeyboardInterrupt()
    controller, old, new, _, _, _, policy = closed_rig(tmp_path, fault=fault)
    plan = controller.plan(new, "update")
    with pytest.raises(KeyboardInterrupt):
        controller.switch(new, "update", plan["plan_sha256"])
    marker_path = controller.root / "CLOSED_BASELINE.json"
    marker = json.loads(marker_path.read_bytes())
    if damage == "marker":
        marker["request_key"] = "0" * 64
        marker_path.write_bytes(canonical_bytes(marker))
    elif damage == "event":
        (controller.root / "journals" / (marker["verified_event_sha256"] + ".json")).write_bytes(b"{}")
    elif damage == "state":
        controller._publish_admission(AdmissionState(status="CLOSED", request_key="0" * 64))
    elif damage == "current":
        controller._select_current(new)
    else:
        controller.previous_admission = lambda _: policy.model_copy(update={"qualification_sha256": "0" * 64})
    assert not controller._committed_closed(controller._request_key("update"), old, policy)


def acceptance_drill(tmp_path):
    from vkm_corpus.update import acceptance as A
    controller, old, new, _, _, _, policy = closed_rig(tmp_path)
    proof = controller.rehearse_failure(new, "closed-drill", fail_after_adapter="document")
    receipt_root = tmp_path / "receipts"
    receipt_root.mkdir()
    registrar = A.AcceptanceRegistrar(receipt_root, approved_plan_sha256="0" * 64)
    for event in controller.history("closed-drill"):
        registrar.store({k: v for k, v in event.items() if k != "journal_sha256"})
    pin = A.CandidatePin(components=new.components, code_commit=new.code_commit,
        code_tree_sha256=proof["code_tree_sha256"], dependencies_sha256=proof["dependencies_sha256"],
        access_config_sha256=proof["access_config_sha256"], policy_sha256=new.policy_sha256,
        duckdb_file_sha256="a" * 64)
    plan = A.AcceptancePlan(candidate=pin, scope="SYNTHETIC", source_id="VKM-SRC-001",
        tools={name: A.ToolProbe(arguments={}, expected_object_ids=() if name == "get_corpus_status" else ("VKM-SRC-001",))
               for name in A.read_tool_names()}, reader_principal="reader", denied_principal="denied",
        deployment_profile_sha256=proof["deployment_profile_sha256"], drill_receipt_sha256=registrar.store(proof))
    registrar.approved_plan_sha256 = plan.sha256
    return registrar, plan, proof, policy


def test_registrar_accepts_exact_typed_closed_drill_without_reopened_claim(tmp_path):
    registrar, plan, proof, policy = acceptance_drill(tmp_path)
    assert registrar.require_drill(plan) == proof
    observed = []
    def verify(value):
        observed.append(value)
        return policy
    registrar.closed_baseline_verifier = verify
    assert registrar.require_drill(plan) == proof
    assert registrar.require_drill(plan) == proof and observed == [proof, proof]


@pytest.mark.parametrize("fault", ["owner", "marker_hash", "generation", "native", "ordinary_schema"])
def test_closed_drill_binding_forgery_cannot_qualify_acceptance(tmp_path, fault):
    from vkm_corpus.update.acceptance import AcceptanceError
    registrar, plan, proof, _ = acceptance_drill(tmp_path)
    proof = json.loads(canonical_bytes(proof))
    if fault == "owner":
        proof["previous_admission"]["closed_owner_key"] = "0" * 64
    elif fault == "marker_hash":
        proof["closed_restore_commit_sha256"] = "0" * 64
    elif fault == "generation":
        proof["previous_generation_sha256"] = "0" * 64
    elif fault == "native":
        proof["native_before_sha256"] = proof["native_after_restore_sha256"] = "0" * 64
    else:
        from vkm_corpus.update.acceptance import DRILL_CHECKS
        proof["schema"] = "vkm-deployment-drill/1"
        proof["checks"] = dict.fromkeys(DRILL_CHECKS, "PASS")
    changed = plan.model_copy(update={"drill_receipt_sha256": registrar.store(proof)})
    with pytest.raises(AcceptanceError):
        registrar.require_drill(changed)


def test_production_closed_drill_requires_independent_fresh_baseline_validator(tmp_path):
    from vkm_corpus.update.acceptance import AcceptanceError
    registrar, plan, proof, policy = acceptance_drill(tmp_path)
    # Deliberately relabel synthetic bytes: missing/wrong independent verifier
    # must fail; this test never registers a production receipt.
    proof = {**proof, "scope": "SHADOW_PRODUCTION", "isolation_attestation_sha256": "1" * 64}
    claimed = plan.model_copy(update={"scope": "SHADOW_PRODUCTION", "isolation_attestation_sha256": "1" * 64,
                                     "drill_receipt_sha256": registrar.store(proof)})
    with pytest.raises(AcceptanceError, match="independent baseline"):
        registrar.require_drill(claimed)
    registrar.closed_baseline_verifier = lambda _: PreviousAdmission()
    with pytest.raises(AcceptanceError, match="differs"):
        registrar.require_drill(claimed)
    registrar.closed_baseline_verifier = lambda _: policy.model_copy(update={"qualification_sha256": "0" * 64})
    with pytest.raises(AcceptanceError, match="differs"):
        registrar.require_drill(claimed)

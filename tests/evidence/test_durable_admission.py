"""Synthetic selector crashes; no Docker, production or scientific admission."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from vkm_corpus.update.admission import (AdmissionState, AdmissionUnavailable, STATE_FILE,
                                          read_state, require_open)
from vkm_corpus.update.barrier import BarrierUnavailable, ReceiverBarrier
from vkm_corpus.update.deployment import ReceiverControl
from vkm_corpus.update.generation import GenerationUnavailable
from vkm_evidence.contracts import canonical_bytes
from test_deployment_lifecycle import rig


def replacement(root, *, required=True):
    barrier = ReceiverBarrier("replacement", require_durable=required)
    if os.name == "posix":
        barrier.bind_gate(root / "admission.lock")
    else:
        barrier.admission_root = root  # portable state tests; no native flock claim
    return barrier


def admitted(tmp_path):
    controller, old, new, state, calls, barrier = rig(tmp_path)
    plan = controller.plan(new, "update")
    controller.switch(new, "update", plan["plan_sha256"])
    return controller, old, new, plan, calls, barrier


@pytest.mark.parametrize("phase", ["MAINTENANCE_REMOVED", "REBOUND:receiver", "RECEIVERS_VERIFIED"])
def test_controller_interruption_after_maintenance_removal_keeps_replacement_closed(tmp_path, phase):
    controller, old, new, state, calls, barrier = rig(tmp_path, fault=lambda current:
        (_ for _ in ()).throw(KeyboardInterrupt()) if current == phase else None)
    plan = controller.plan(new, "update")
    with pytest.raises(KeyboardInterrupt):
        controller.switch(new, "update", plan["plan_sha256"])
    assert not (controller.root / "MAINTENANCE").exists()
    late = replacement(controller.root)
    assert not late.status()["admission_open"]
    with pytest.raises(BarrierUnavailable):
        late.acquire()
    controller.fault = None
    recovery = controller.recovery_plan("update", mode="RESTORE_PREVIOUS")
    assert controller.recover("update", recovery["recovery_sha256"])["status"] == "RESTORED"
    assert require_open(controller.root).generation_sha256 == old.sha256
    with late.acquire():
        pass


@pytest.mark.parametrize("exception", [KeyboardInterrupt, RuntimeError])
def test_crash_after_durable_open_reconciles_ack_without_apply_or_restart(tmp_path, exception):
    controller, old, new, state, calls, barrier = rig(tmp_path, fault=lambda phase:
        (_ for _ in ()).throw(exception()) if phase == "ADMISSION_OPEN" else None)
    plan = controller.plan(new, "update")
    with pytest.raises(exception):
        controller.switch(new, "update", plan["plan_sha256"])
    assert require_open(controller.root).generation_sha256 == new.sha256
    with replacement(controller.root).acquire():
        pass
    assert not any(c[0] == "restore" for c in calls)
    with pytest.raises(GenerationUnavailable, match="already admitted"):
        controller.recovery_plan("update", mode="RESTORE_PREVIOUS")
    count = len(calls)
    result = controller.switch(new, "update", plan["plan_sha256"])
    assert result["status"] == "PASS" and len(calls) == count
    assert not barrier.status()["paused"]
    assert controller.switch(new, "update", plan["plan_sha256"]) == result


def test_lost_ack_after_previous_reopened_reconciles_restore_without_second_restart(tmp_path):
    def crash(phase):
        if phase == "APPLIED:document":
            raise RuntimeError("partial apply")
        if phase == "ADMISSION_OPEN":
            raise KeyboardInterrupt("restore committed, answer lost")
    controller, old, new, state, calls, barrier = rig(tmp_path, fault=crash)
    plan = controller.plan(new, "update")
    with pytest.raises(KeyboardInterrupt):
        controller.switch(new, "update", plan["plan_sha256"])
    assert require_open(controller.root).generation_sha256 == old.sha256
    count = len(calls)
    recovery = controller.recovery_plan("update", mode="RESTORE_PREVIOUS")
    result = controller.recover("update", recovery["recovery_sha256"])
    assert result["status"] == "RESTORED" and len(calls) == count
    assert not barrier.status()["paused"]


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_restore_previous_never_requires_candidate_generation_artifact(tmp_path, damage):
    controller, old, new, _, _, _ = rig(tmp_path, fault=lambda phase:
        (_ for _ in ()).throw(KeyboardInterrupt()) if phase == "APPLIED:document" else None)
    plan = controller.plan(new, "update")
    with pytest.raises(KeyboardInterrupt):
        controller.switch(new, "update", plan["plan_sha256"])
    path = controller.root / (new.sha256 + ".json")
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"corrupt candidate")
    controller.fault = None
    recovery = controller.recovery_plan("update", mode="RESTORE_PREVIOUS")
    assert controller.recover("update", recovery["recovery_sha256"])["status"] == "RESTORED"
    assert require_open(controller.root).generation_sha256 == old.sha256


@pytest.mark.parametrize("boundary", ["rebind", "verified"])
def test_pre_open_fence_rejects_profile_change_after_receiver_rebind(tmp_path, boundary):
    controller, _, new, _, _, _ = rig(tmp_path)
    profile = controller.identity_provider()
    def change():
        controller.identity_provider = lambda: profile.model_copy(update={"dependencies_sha256": "f" * 64})
    original = controller.receivers["receiver"].rebind
    if boundary == "rebind":
        def rebind(manifest):
            original(manifest)
            change()
        controller.receivers["receiver"] = ReceiverControl(controller.receivers["receiver"].barrier, rebind)
    else:
        controller.fault = lambda phase: change() if phase == "RECEIVERS_VERIFIED" else None
    plan = controller.plan(new, "update")
    with pytest.raises(GenerationUnavailable, match="profile"):
        controller.switch(new, "update", plan["plan_sha256"])
    assert read_state(controller.root).status == "CLOSED"
    assert not replacement(controller.root).status()["admission_open"]


@pytest.mark.parametrize("mode", ["RESTORE_PREVIOUS", "COMPLETE_CANDIDATE"])
def test_recovery_terminal_ack_retry_returns_identical_receipt_without_mutation(tmp_path, mode):
    controller, old, new, _, calls, _ = rig(tmp_path, fault=lambda phase:
        (_ for _ in ()).throw(KeyboardInterrupt()) if phase == "CURRENT_WRITTEN" else None)
    plan = controller.plan(new, "update")
    with pytest.raises(KeyboardInterrupt):
        controller.switch(new, "update", plan["plan_sha256"])
    recovery = controller.recovery_plan("update", mode=mode)
    controller.fault = lambda phase: (_ for _ in ()).throw(ConnectionError("lost recovery ACK")) if phase == "RECOVERY_ACK" else None
    with pytest.raises(ConnectionError):
        controller.recover("update", recovery["recovery_sha256"], mode=mode)
    count = len(calls)
    result = controller.recover("update", recovery["recovery_sha256"], mode=mode)
    assert len(calls) == count
    assert result["generation_sha256"] == (old.sha256 if mode == "RESTORE_PREVIOUS" else new.sha256)
    assert controller.recover("update", recovery["recovery_sha256"], mode=mode) == result
    with pytest.raises(GenerationUnavailable):
        controller.recover("update", "f" * 64, mode=mode)


@pytest.mark.parametrize("fault", ["missing", "closed", "current", "event_bytes", "event_phase",
                                  "event_request", "event_generation", "event_receivers", "duplicate", "unowned", "directory"])
def test_open_requires_fresh_owned_record_current_and_verified_event(tmp_path, fault):
    controller, _, new, _, _, _ = admitted(tmp_path)
    root = controller.root
    state = read_state(root)
    path = root / STATE_FILE
    event_path = root / "journals" / (state.verified_event_sha256 + ".json")
    if fault == "missing":
        path.unlink()
    elif fault == "closed":
        path.write_bytes(canonical_bytes(AdmissionState(status="CLOSED", request_key=state.request_key)))
    elif fault == "current":
        (root / "CURRENT").write_text("a" * 64)
    elif fault == "event_bytes":
        event_path.write_bytes(event_path.read_bytes() + b" ")
    elif fault.startswith("event_"):
        event = json.loads(event_path.read_bytes())
        if fault == "event_phase":
            event["phase"] = "VERIFIED"  # pre-restart proof is insufficient
        elif fault == "event_request":
            event["request_key"] = "a" * 64
        elif fault == "event_receivers":
            event["detail"]["receiver_proofs"] = {}
        else:
            event["detail"]["generation_sha256"] = "a" * 64
        from hashlib import sha256
        raw = canonical_bytes(event)
        sha = sha256(raw).hexdigest()
        (root / "journals" / (sha + ".json")).write_bytes(raw)
        path.write_bytes(canonical_bytes(state.model_copy(update={"verified_event_sha256": sha})))
    elif fault == "duplicate":
        path.write_bytes(b'{"status":"OPEN","status":"CLOSED"}')
    elif fault == "unowned":
        path.write_bytes(canonical_bytes(state.model_copy(update={"request_key": "a" * 64})))
    else:
        path.unlink()
        path.mkdir()
    late = replacement(root)
    assert not late.status()["admission_open"]
    with pytest.raises((AdmissionUnavailable, OSError)):
        require_open(root)
    with pytest.raises(BarrierUnavailable):
        late.acquire()
    # A content request denied here never retained a kernel/request lease.
    assert late.status()["active_requests"] == 0


def test_missing_record_only_allows_explicit_legacy_barrier_not_qualified_receiver(tmp_path):
    root = tmp_path / "legacy"
    root.mkdir()
    with replacement(root, required=False).acquire():
        pass
    with pytest.raises(BarrierUnavailable):
        replacement(root).acquire()
    (root / "MAINTENANCE").write_text("unresolved")
    with pytest.raises(BarrierUnavailable):
        replacement(root, required=False).acquire()


def test_open_record_cannot_carry_scientific_admission_or_omit_verified_event():
    with pytest.raises(ValueError):
        AdmissionState(status="OPEN", request_key="a" * 64, generation_sha256="b" * 64)
    with pytest.raises(ValueError):
        AdmissionState(status="OPEN", request_key="a" * 64, generation_sha256="b" * 64,
                       verified_event_sha256="c" * 64, scientific_admission="FACT")


@pytest.mark.skipif(os.name != "posix", reason="NOT_RUN: POSIX shared admission gate")
def test_native_rebind_cannot_publish_open_without_returned_proof(tmp_path):
    controller, old, new, state, calls, barrier = rig(tmp_path)
    # Change only the profile classification of this synthetic control fixture.
    profile = controller.identity_provider().model_copy(update={
        "scope": "SHADOW_PRODUCTION", "isolation_attestation_sha256": "f" * 64})
    controller.identity_provider = lambda: profile
    plan = controller.plan(new, "update")
    with pytest.raises(GenerationUnavailable, match="omitted its proof"):
        controller.switch(new, "update", plan["plan_sha256"])
    assert read_state(controller.root).status == "CLOSED"
    assert not replacement(controller.root).status()["admission_open"]


@pytest.mark.skipif(os.name != "posix", reason="NOT_RUN: POSIX shared admission gate")
def test_native_lock_watch_survives_owned_read_only_fences_and_repeated_writer(tmp_path):
    controller, old, new, state, calls, barrier = rig(tmp_path)
    profile = controller.identity_provider().model_copy(update={
        "scope": "SHADOW_PRODUCTION", "isolation_attestation_sha256": "f" * 64})
    controller.identity_provider = lambda: profile
    original = controller.receivers["receiver"].rebind
    def rebind(manifest):
        original(manifest)
        return {"synthetic_generation": manifest.sha256}  # CPU fixture only
    controller.receivers["receiver"] = ReceiverControl(barrier, rebind)
    for request, candidate in (("update", new), ("next", old)):
        plan = controller.plan(candidate, request)
        assert controller.switch(candidate, request, plan["plan_sha256"])["status"] == "PASS"
        controller._lock_watch.check()
    # Actual external lock-file mutation still permanently invalidates the lease.
    (controller.root / "writer.lock").write_bytes(b"mutation")
    with pytest.raises(ValueError, match="changed"):
        controller._lock_watch.check()


@pytest.mark.skipif(os.name != "posix", reason="NOT_RUN: POSIX shared admission gate")
@pytest.mark.parametrize("phase", ["MAINTENANCE_REMOVED", "REBOUND:receiver", "RECEIVERS_VERIFIED", "ADMISSION_OPEN"])
def test_actual_writer_process_exit_does_not_reopen_unverified_replacement(tmp_path, phase):
    child = """from pathlib import Path
import os, sys
from test_deployment_lifecycle import rig
c, _, new, _, _, _ = rig(Path(sys.argv[1]), fault=lambda phase:
    os._exit(73) if phase == sys.argv[2] else None)
p = c.plan(new, 'update')
c.switch(new, 'update', p['plan_sha256'])
"""
    env = dict(os.environ)
    root = Path(__file__).resolve().parents[2]
    env.update(PYTHONPATH=os.pathsep.join((str(root / "src"), str(root / "tests/evidence"))),
               PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1")
    result = subprocess.run([sys.executable, "-c", child, str(tmp_path), phase], env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 73, result.stderr[-2000:]
    served = tmp_path / "served"
    assert not (served / "MAINTENANCE").exists()
    late = replacement(served)
    if phase == "ADMISSION_OPEN":
        with late.acquire():
            assert require_open(served).status == "OPEN"
    else:
        assert read_state(served).status == "CLOSED"
        with pytest.raises(BarrierUnavailable):
            late.acquire()

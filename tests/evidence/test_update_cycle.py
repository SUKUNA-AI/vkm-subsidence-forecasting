from __future__ import annotations

import hashlib
import json

import pytest

from vkm_corpus.update.contracts import CampaignManifest, ComponentIdentity, GenerationManifest
from vkm_corpus.update.cycle import UpdateCycle
from vkm_corpus.update.generation import GenerationCoordinator, GenerationUnavailable
from vkm_evidence.contracts import canonical_bytes, record_hash

SHA = "a" * 64
CODE = "b" * 40
IDENTITY = {"commit": CODE, "dirty": False, "profile": "production"}


def campaign(**overrides):
    return CampaignManifest(**{"campaign_id": "synthetic", "code_commit": CODE,
        "producer_identity_sha256": record_hash(IDENTITY), "policy_sha256": SHA, "inputs": (),
        "stages": ({"stage_id": "first", "operation": "VERIFY_ORIGINALS", "input_sha256": SHA,
                    "config_sha256": SHA, "memory_gib": 0.1, "min_free_disk_gib": 0,
                    "timeout_seconds": 5, "compute": "CPU", "attempts": 2},),
        "gates": ({"gate_id": "qualified", "status": "PASS", "evidence_sha256": SHA},), **overrides})


def engine(tmp_path, adapter, **kwargs):
    return UpdateCycle(tmp_path / "runtime", tmp_path / "originals", producer_guard=lambda: IDENTITY,
        adapters={"VERIFY_ORIGINALS": adapter}, memory_available=lambda: 1, gate_verifier=lambda s: s == SHA, **kwargs)


def test_resume_reuses_pass_and_invalidated_output_does_not_fake_pass(tmp_path):
    calls = []
    def adapter(stage, request, root):
        calls.append(request)
        path = root / "output.json"
        path.write_bytes(b"synthetic")
        return {"status": "PASS", "outputs": [{"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}]}
    e, c = engine(tmp_path, adapter), campaign()
    plan = e.plan(c)
    assert e.execute(c, plan["plan_sha256"])["status"] == "PASS"
    assert e.execute(c, plan["plan_sha256"])["status"] == "PASS" and len(calls) == 1
    (e.root / "output.json").write_bytes(b"changed")
    assert e.status(c)["stages"]["first"]["status"] == "INVALIDATED"
    assert e.execute(c, plan["plan_sha256"])["status"] == "PASS" and len(calls) == 2 and calls[0] == calls[1]


def test_failed_attempt_bounded_and_blocked_not_pass(tmp_path):
    e = engine(tmp_path, lambda *_: {"status": "BLOCKED", "reason": "NO_ADAPTER", "outputs": []})
    c = campaign()
    result = e.execute(c, e.plan(c)["plan_sha256"])
    assert result["status"] == "BLOCKED" and result["stages"]["first"]["status"] == "BLOCKED"
    assert e.status(c)["status"] == "BLOCKED"


def test_input_changes_and_notrun_gate_block_plan(tmp_path):
    originals = tmp_path / "originals"
    originals.mkdir()
    (originals / "input.bin").write_bytes(b"original")
    c = campaign(inputs=({"source_id": "source-1", "source_sha256": hashlib.sha256(b"original").hexdigest(),
                            "logical_path": "input.bin", "size_bytes": 8, "lifecycle": "ACTIVE"},))
    e = engine(tmp_path, lambda *_: {"status": "PASS", "outputs": []})
    first = e.plan(c)
    (originals / "input.bin").write_bytes(b"replaced")
    assert e.plan(c)["status"] == "BLOCKED"
    with pytest.raises(ValueError, match="qualified plan"):
        e.execute(c, first["plan_sha256"])
    blocked = campaign(gates=({"gate_id": "gold", "status": "NOT_RUN"},))
    assert e.plan(blocked)["status"] == "BLOCKED"


def test_memory_guard_runs_before_adapter(tmp_path):
    called = []
    e = engine(tmp_path, lambda *_: called.append(True))
    e.memory_available = lambda: 0
    c = campaign()
    with pytest.raises(ValueError, match="memory guard"):
        e.execute(c, e.plan(c)["plan_sha256"])
    assert called == []


def generation(revision="doc-1"):
    components = (ComponentIdentity(component="DOCUMENT", revision=revision, manifest_sha256=SHA,
        policy_sha256=SHA, built_from={}), ComponentIdentity(component="NAV", revision="nav-" + revision,
        manifest_sha256=SHA, policy_sha256=SHA, built_from={"DOCUMENT": revision}))
    return GenerationManifest(components=components, code_commit=CODE, policy_sha256=SHA, acceptance_sha256=SHA)


def test_shadow_switch_failed_acceptance_restores_all_selectors(tmp_path):
    coordinator = GenerationCoordinator(tmp_path)
    observed = {}
    def apply(g):
        observed.clear()
        observed.update({c.component: c.model_dump(mode="json") for c in g.components})
        assert coordinator.status(lambda: observed)["status"] == "UNAVAILABLE"
    first, second = generation(), generation("doc-2")
    coordinator.switch(first, lambda: observed, apply, apply, acceptance=lambda _: True)
    assert coordinator.status(lambda: observed)["status"] == "READY"
    with pytest.raises(GenerationUnavailable, match="acceptance"):
        coordinator.switch(second, lambda: observed, apply, apply, acceptance=lambda _: False)
    assert coordinator.manifest() == first
    assert coordinator.status(lambda: observed)["status"] == "READY"


def test_failed_rollback_keeps_maintenance(tmp_path):
    coordinator = GenerationCoordinator(tmp_path)
    observed = {}
    def apply(g):
        observed.update({c.component: c.model_dump(mode="json") for c in g.components})
    coordinator.switch(generation(), lambda: observed, apply, apply, acceptance=lambda _: True)
    def failed_restore(_):
        raise RuntimeError("rollback failed")
    with pytest.raises(RuntimeError, match="rollback failed"):
        coordinator.switch(generation("doc-2"), lambda: observed, apply, failed_restore, acceptance=lambda _: False)
    assert (tmp_path / "MAINTENANCE").exists()
    assert coordinator.status(lambda: observed)["status"] == "UNAVAILABLE"


def test_mixed_policy_or_projection_rejected():
    g = generation()
    wrong = g.model_dump()
    wrong["components"][1]["built_from"]["DOCUMENT"] = "other"
    with pytest.raises(ValueError, match="mixed component"):
        GenerationManifest.model_validate(wrong)


def _read_state(engine, campaign):
    return json.loads((engine.root / (campaign.sha256 + ".state.json")).read_bytes())


def _write_state(engine, campaign, state):
    (engine.root / (campaign.sha256 + ".state.json")).write_bytes(canonical_bytes(state))


def _read_receipt(engine, entry):
    return json.loads((engine.root / (entry["receipt_sha256"] + ".receipt.json")).read_bytes())


def _new_receipt(engine, receipt):
    data = canonical_bytes(receipt)
    digest = hashlib.sha256(data).hexdigest()
    (engine.root / (digest + ".receipt.json")).write_bytes(data)
    return digest


def test_selector_cannot_promote_blocked_receipt_to_pass(tmp_path):
    e, c = engine(tmp_path, lambda *_: {"status": "BLOCKED", "outputs": []}), campaign()
    e.execute(c, e.plan(c)["plan_sha256"])
    state = _read_state(e, c)
    state["status"] = state["stages"]["first"]["status"] = "PASS"
    _write_state(e, c, state)
    with pytest.raises(ValueError, match="contradicts receipt"):
        e.status(c)
    with pytest.raises(ValueError, match="contradicts receipt"):
        e.execute(c, e.plan(c)["plan_sha256"])


@pytest.mark.parametrize("field,value", [("campaign_sha256", "d" * 64), ("stage_id", "other"),
    ("request_id", "e" * 64), ("attempt", 2), ("attempt", True)])
def test_hash_valid_receipt_with_wrong_owner_rejected(tmp_path, field, value):
    e, c = engine(tmp_path, lambda *_: {"status": "PASS", "outputs": []}), campaign()
    e.execute(c, e.plan(c)["plan_sha256"])
    state = _read_state(e, c)
    entry = state["stages"]["first"]
    receipt = _read_receipt(e, entry)
    receipt[field] = value
    entry["receipt_sha256"] = _new_receipt(e, receipt)
    _write_state(e, c, state)
    with pytest.raises(ValueError, match="owner identity"):
        e.status(c)


@pytest.mark.parametrize("field,value", [("campaign_sha256", "d" * 64), ("stage_id", "other"),
    ("request_id", "e" * 64), ("attempt", 3), ("dependency_receipts", {})])
def test_adapter_cannot_override_receipt_owner(tmp_path, field, value):
    e, c = engine(tmp_path, lambda *_: {"status": "PASS", "outputs": [], field: value}), campaign()
    result = e.execute(c, e.plan(c)["plan_sha256"])
    assert result["status"] == "FAIL"
    receipt = _read_receipt(e, result["stages"]["first"])
    assert receipt["campaign_sha256"] == c.sha256 and receipt["stage_id"] == "first"
    assert receipt["request_id"] == e._request(c, c.stages[0]) and receipt["attempt"] == 1


def test_durable_running_intent_binds_campaign_attempt_and_recovers_same_request(tmp_path):
    calls = []
    def adapter(stage, request, root):
        calls.append(request)
        if len(calls) == 1:
            raise KeyboardInterrupt("synthetic crash after durable intent")
        return {"status": "PASS", "outputs": []}
    e, c = engine(tmp_path, adapter), campaign()
    with pytest.raises(KeyboardInterrupt):
        e.execute(c, e.plan(c)["plan_sha256"])
    state = e.status(c)
    assert state["status"] == state["stages"]["first"]["status"] == "RUNNING"
    receipt = _read_receipt(e, state["stages"]["first"])
    assert receipt == {"campaign_sha256": c.sha256, "stage_id": "first", "request_id": calls[0],
                       "attempt": 1, "dependency_receipts": {}, "outputs": [], "status": "RUNNING"}
    result = e.execute(c, e.plan(c)["plan_sha256"])
    assert result["status"] == "PASS" and result["stages"]["first"]["attempts"] == 1
    assert calls[0] == calls[1]


def test_crash_after_upstream_retry_cannot_reuse_stale_downstream_receipt(tmp_path):
    c = campaign()
    second = c.stages[0].model_copy(update={"stage_id": "second", "depends_on": ("first",)})
    c = c.model_copy(update={"stages": (c.stages[0], second)})
    calls = []
    def adapter(stage, request, root):
        calls.append(stage.stage_id)
        output = root / (stage.stage_id + ".out")
        output.write_bytes(b"stable result")
        return {"status": "PASS", "outputs": [{"path": output.name, "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}]}
    e = engine(tmp_path, adapter)
    e.execute(c, e.plan(c)["plan_sha256"])
    (e.root / "first.out").write_bytes(b"corrupt")
    original_save = e._save
    def crash_after_upstream(campaign, state):
        original_save(campaign, state)
        if state["stages"]["first"]["attempts"] == 2 and state["stages"]["first"]["status"] == "PASS":
            raise KeyboardInterrupt("crash before downstream retry")
    e._save = crash_after_upstream
    with pytest.raises(KeyboardInterrupt):
        e.execute(c, e.plan(c)["plan_sha256"])
    state = e.status(c)
    assert state["stages"]["first"]["status"] == "PASS"
    assert state["stages"]["second"]["status"] == "INVALIDATED" and state["status"] == "INVALIDATED"
    e._save = original_save
    assert e.execute(c, e.plan(c)["plan_sha256"])["status"] == "PASS"
    assert calls == ["first", "second", "first", "second"]


@pytest.mark.parametrize("damage", ["wrong_hash", "empty", "malformed", "missing", "observed_mismatch"])
def test_invalid_previous_generation_never_applies_candidate_and_stays_closed(tmp_path, damage):
    coordinator = GenerationCoordinator(tmp_path)
    previous, candidate = generation(), generation("doc-2")
    (tmp_path / "CURRENT").write_text(previous.sha256)
    (tmp_path / (previous.sha256 + ".json")).write_bytes(canonical_bytes(previous))
    if damage == "wrong_hash":
        (tmp_path / (previous.sha256 + ".json")).write_bytes(canonical_bytes(candidate))
    elif damage == "empty":
        (tmp_path / "CURRENT").write_text("")
    elif damage == "malformed":
        (tmp_path / "CURRENT").write_text("../other")
    elif damage == "missing":
        (tmp_path / (previous.sha256 + ".json")).unlink()
    observed = {c.component: c.model_dump(mode="json") for c in previous.components}
    if damage == "observed_mismatch":
        observed = {}
    calls = []
    with pytest.raises((GenerationUnavailable, OSError)):
        coordinator.switch(candidate, lambda: observed, lambda _: calls.append("apply"),
                           lambda _: calls.append("restore"), acceptance=lambda _: True)
    assert calls == [] and (tmp_path / "MAINTENANCE").exists()
    assert not (tmp_path / (candidate.sha256 + ".json")).exists()
    assert coordinator.status(lambda: observed)["status"] == "UNAVAILABLE"

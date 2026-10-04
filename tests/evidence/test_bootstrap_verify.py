"""Immutable SYNTHETIC baseline-chain consumers; no real receiver qualification."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from test_bootstrap_native import cold_case, shadow_setup, bound
from vkm_corpus.update import acceptance as A
from vkm_corpus.update import bootstrap_probes as P
from vkm_corpus.update.bootstrap import (BaselineQualification, BaselineRegistration,
    BootstrapBoundaryReceipt, BootstrapPreparation)
from vkm_corpus.update.bootstrap_verify import require_closed_baseline
from vkm_corpus.update.deployment import components_sha256, services_sha256
from vkm_corpus.update.generation import GenerationUnavailable
from vkm_corpus.update.receiver import ReceiverIdentity
from vkm_corpus.update.runtime import BoundFile
from vkm_evidence.contracts import record_hash


def store(root, value):
    return bound(root / (record_hash(value) + ".json"), value)


def private_report(root, plan):
    """Format-only transcript fixture, only permitted for explicit SYNTHETIC."""
    assert plan.scope == "SYNTHETIC"
    receipts = []
    cases = [("MCP", "tools/list", None)]
    cases += [("MCP", name, probe.arguments) for name, probe in plan.probes.tools.items()]
    cases += [("API", method + " " + route, {"principal": principal, "body": body})
        for principal, method, route, body, _, _ in A._api_probes(plan.probes)]
    for kind, operation, arguments in cases:
        receipt = {"schema": P.PROBE_SCHEMA, "plan_sha256": plan.sha256, "status": "PASS",
            "kind": kind, "operation": operation, "arguments_sha256": record_hash(arguments),
            "response_sha256": record_hash({"synthetic_operation": operation, "arguments": arguments}),
            "response_bytes": 42, "native_components_sha256": record_hash(plan.probes.candidate.component_map),
            "native_services_sha256": record_hash(plan.probes.candidate.service_map)}
        receipts.append(store(root, receipt).sha256)
    return {**P._bindings(plan), "status": P.VERIFIED, "tools": dict.fromkeys(A.read_tool_names(), "PASS"),
        "checks": dict.fromkeys(P.BOOTSTRAP_CHECKS, "PASS"), "closed_authority_sha256": "a" * 64,
        "raw_probe_receipts": receipts}


def receiver(manifest, release, *, number=1):
    return ReceiverIdentity(nonce=str(number) * 64, instance=str(number + 1) * 64,
        process_pid=100 + number, process_start_ticks=1000 + number, pid_namespace_inode=321,
        generation_sha256=manifest.sha256, runtime_config_sha256=release.runtime_config_sha256,
        code_sha256=release.code_sha256, dependencies_sha256=release.dependencies_sha256,
        access_sha256=release.access_sha256, components_sha256=components_sha256(manifest),
        services_sha256=services_sha256(manifest), gate_device=0, gate_inode=123, admission_open=False,
        read_contract_sha256=record_hash(sorted(A.read_tool_names())))


class Registered:
    def __init__(self, c):
        self.c, self.root, self.qroot = c, c.root, Path(c.config.qualification_root)
        self.empty, self.legacy = c.bootstrap._empty(), c.bootstrap._legacy()
        c.bootstrap._event("BOOTSTRAP_ATTEMPT", intent_sha256=c.intent.sha256,
            plan_sha256=store(self.qroot, c.bootstrap.plan_run()).sha256)
        self.first, first_release, _ = c.bootstrap._prepare()
        self.first_proof = store(self.qroot, receiver(c.bootstrap.manifest, first_release))
        c.bootstrap._event("CLOSED_NATIVE_STARTED", receiver_release_sha256=first_release.sha256,
            native_proof_sha256=self.first_proof.sha256)
        c.bootstrap._event("FIRST_PRIVATE_PROBES_VERIFIED",
            private_probe_sha256=store(self.qroot, private_report(self.qroot, c.bootstrap.plan)).sha256)
        self.restore_event()
        self.second, second_release, _ = self.prepare("3", "4")
        self.partial = {"schema": "vkm-bootstrap-partial-start/1", "scope": "SYNTHETIC",
            "intent_sha256": c.intent.sha256, "admission": "CLOSED", "units": {
                unit: {"container_id": self.second.native_container_ids[unit], "running": unit == "api",
                    **second_release.units[unit].model_dump(mode="json")} for unit in ("api", "mcp")}}
        partial_ref = store(self.qroot, self.partial)
        c.bootstrap._event("PARTIAL_NATIVE_START", native_sha256=partial_ref.sha256)
        self.restore_event()
        self.boundary = BootstrapBoundaryReceipt(scope="SYNTHETIC", intent_sha256=c.intent.sha256,
            startup_authority_sha256=c.startup.sha256, preparation_sha256=record_hash(self.second),
            legacy_before_sha256=self.legacy, legacy_after_sha256=self.legacy,
            empty_before_sha256=self.empty, empty_after_sha256=self.empty,
            failed_native_start_sha256=partial_ref.sha256, journal_receipts=c.bootstrap._history_receipts())
        self.final, final_release, _ = self.prepare("5", "6")
        self.report = private_report(self.qroot, c.bootstrap.plan)
        self.native = receiver(c.bootstrap.manifest, final_release, number=3)
        self.reg = BaselineRegistration(scope="SYNTHETIC", intent=c.config.intent,
            probes=store(self.qroot, c.bootstrap.plan), preparation=store(self.qroot, self.final),
            failed_preparation=store(self.qroot, self.second), private_probe=store(self.qroot, self.report),
            boundary=store(self.qroot, self.boundary), native_start=store(self.qroot, self.native),
            qualification=BoundFile(path=str(self.qroot / "pending.json"), sha256="0" * 64))
        self.seal()

    def prepare(self, api_id, mcp_id):
        self.c.commands.objects.clear()
        self.c.commands.cold["api"]["Id"] = api_id * 64
        self.c.commands.cold["mcp"]["Id"] = mcp_id * 64
        return self.c.bootstrap._prepare()

    def restore_event(self):
        self.c.bootstrap._event("ADMISSION_CLOSED")
        self.c.bootstrap._event("DRAINED")
        self.c.bootstrap._event("EMPTY_RESTORED", empty_sha256=self.empty, legacy_sha256=self.legacy)

    def seal(self, **changes):
        self.reg = self.reg.model_copy(update={key: store(self.qroot, value) for key, value in changes.items()})
        q = BaselineQualification(scope=self.reg.scope, intent_sha256=self.c.intent.sha256,
            startup_authority_sha256=self.c.startup.sha256, preparation_sha256=self.reg.preparation.sha256,
            baseline_generation_sha256=self.c.bootstrap.manifest.sha256,
            private_probe_receipt_sha256=self.reg.private_probe.sha256, boundary_receipt_sha256=self.reg.boundary.sha256,
            repeated_native_start_sha256=self.reg.native_start.sha256, legacy_topology_sha256=self.c.intent.legacy.sha256,
            closed_owner_key=self.c.startup.request_key)
        self.reg = self.reg.model_copy(update={"qualification": store(self.qroot, q)})
        self.ref = store(self.qroot, self.reg)
        return self.ref

    def journals(self, edit):
        events = [json.loads((self.qroot / (digest + ".json")).read_bytes()) for digest in self.boundary.journal_receipts]
        edit(events)
        parent, hashes = None, []
        for event in events:
            event["parent_sha256"] = parent
            parent = store(self.qroot, event).sha256
            hashes.append(parent)
        self.boundary = self.boundary.model_copy(update={"journal_receipts": tuple(hashes)})
        self.seal(boundary=self.boundary)

    def verify(self):
        return require_closed_baseline(self.ref, self.c.bootstrap.manifest, production=False)


@pytest.fixture
def registered(cold_case):
    return Registered(cold_case)


def test_full_chain_returns_closed_baseline_without_reading_current_or_later_owner(registered):
    c = registered
    (c.root / "CURRENT").write_bytes(b"corrupt later candidate selector")
    (c.root / "ADMISSION.json").write_bytes(b"later transaction has another closed owner")
    for path in (c.root / "journals").glob("*.json"):
        path.write_bytes(b"mutable runtime journal unavailable")
    q = c.verify()
    assert q.status == "CLOSED_BASELINE_QUALIFIED" and q.admission == "CLOSED_BASELINE"
    assert q.scientific_admission is False and q.closed_owner_key == c.c.startup.request_key
    assert c.reg.preparation != c.reg.failed_preparation


def test_synthetic_registration_is_never_a_production_previous(registered):
    with pytest.raises(GenerationUnavailable, match="synthetic baseline"):
        require_closed_baseline(registered.ref, registered.c.bootstrap.manifest)


@pytest.mark.parametrize("field", ["intent", "probes", "preparation", "failed_preparation", "private_probe", "boundary",
                                  "native_start", "qualification"])
def test_any_referenced_receipt_byte_change_blocks_restore(registered, field):
    Path(getattr(registered.reg, field).path).write_bytes(b"changed")
    with pytest.raises(GenerationUnavailable): registered.verify()


@pytest.mark.parametrize("field", ["runtime", "environment", "startup_authority", "independent_backup"])
def test_approved_original_inputs_are_fresh_not_saved_hashes(registered, field):
    Path(getattr(registered.c.intent, field).path).write_bytes(b"changed")
    with pytest.raises(GenerationUnavailable): registered.verify()


@pytest.mark.parametrize("change", ["missing_tool", "missing_raw", "duplicate_raw", "fake_status", "open", "checks",
                                     "wrong_plan", "policy_case", "missing_raw_file"])
def test_private_pass_summary_cannot_replace_full_tool_and_policy_receipts(registered, change):
    c, report = registered, copy.deepcopy(registered.report)
    if change == "missing_tool": report["tools"].pop(next(iter(report["tools"])))
    if change == "missing_raw": report["raw_probe_receipts"].pop()
    if change == "duplicate_raw": report["raw_probe_receipts"].append(report["raw_probe_receipts"][0])
    if change == "fake_status": report["status"] = "READY"
    if change == "open": report["serving_admission"] = True
    if change == "checks": report["checks"]["closed_authority"] = "NOT_RUN"
    if change == "wrong_plan": report["plan_sha256"] = "0" * 64
    if change == "policy_case":
        digest = report["raw_probe_receipts"][-1]
        raw = json.loads((c.qroot / (digest + ".json")).read_bytes())
        raw["arguments_sha256"] = "0" * 64
        report["raw_probe_receipts"][-1] = store(c.qroot, raw).sha256
    if change == "missing_raw_file": (c.qroot / (report["raw_probe_receipts"][0] + ".json")).unlink()
    c.seal(private_probe=report)
    with pytest.raises(GenerationUnavailable): c.verify()


@pytest.mark.parametrize("change", ["owner", "missing_drain", "missing_first_restore", "only_metadata",
                                     "partial_hash", "prepare_hash", "open_event", "attempt_intent", "attempt_plan",
                                     "missing_first_private", "first_private_hash", "first_private_tools"])
def test_resealed_journal_still_requires_actual_two_closed_fallbacks(registered, change):
    def edit(events):
        if change == "owner": events[0]["request_key"] = "0" * 64
        if change == "missing_drain": events.pop(next(i for i, e in enumerate(events) if e["phase"] == "DRAINED"))
        if change == "missing_first_restore": events.pop(next(i for i, e in enumerate(events) if e["phase"] == "EMPTY_RESTORED"))
        if change == "only_metadata": events[:] = [events[-1]]
        if change == "partial_hash": next(e for e in events if e["phase"] == "PARTIAL_NATIVE_START")["detail"]["native_sha256"] = "0" * 64
        if change == "prepare_hash": next(e for e in events if e["phase"] == "COLD_PREPARED")["detail"]["preparation_sha256"] = "0" * 64
        if change == "attempt_intent": events[0]["detail"]["intent_sha256"] = "0" * 64
        if change == "attempt_plan": events[0]["detail"]["plan_sha256"] = "0" * 64
        if change == "missing_first_private": events.pop(next(i for i, e in enumerate(events) if e["phase"] == "FIRST_PRIVATE_PROBES_VERIFIED"))
        if change == "first_private_hash": next(e for e in events if e["phase"] == "FIRST_PRIVATE_PROBES_VERIFIED")["detail"]["private_probe_sha256"] = "0" * 64
        if change == "first_private_tools":
            receipt = next(e for e in events if e["phase"] == "FIRST_PRIVATE_PROBES_VERIFIED")
            value = json.loads((registered.qroot / (receipt["detail"]["private_probe_sha256"] + ".json")).read_bytes())
            value["raw_probe_receipts"].pop()
            receipt["detail"]["private_probe_sha256"] = store(registered.qroot, value).sha256
        if change == "open_event": events.insert(-1, {"schema": "vkm-deployment-event/1", "request_key": events[0]["request_key"],
            "parent_sha256": None, "phase": "ADMISSION_OPEN", "detail": {}})
    registered.journals(edit)
    with pytest.raises(GenerationUnavailable): registered.verify()


def test_older_synthetic_chain_is_format_compatible_without_claiming_a_production_attempt(registered):
    registered.journals(lambda events: events.pop(0))
    assert registered.verify().scope == "SYNTHETIC"


@pytest.mark.parametrize("change", ["mcp_running", "api_stopped", "native_id", "config", "open", "scope", "saved_ready"])
def test_failed_start_requires_exact_actual_api_only_native_objects(registered, change):
    c, partial = registered, copy.deepcopy(registered.partial)
    if change == "mcp_running": partial["units"]["mcp"]["running"] = True
    if change == "api_stopped": partial["units"]["api"]["running"] = False
    if change == "native_id": partial["units"]["api"]["container_id"] = "0" * 64
    if change == "config": partial["units"]["mcp"]["config_sha256"] = "0" * 64
    if change == "open": partial["admission"] = "OPEN"
    if change == "scope": partial["scope"] = "BOOTSTRAP_SHADOW_PRODUCTION"
    if change == "saved_ready": partial = {"status": "READY", "saved_digest": "0" * 64}
    digest = store(c.qroot, partial).sha256
    c.boundary = c.boundary.model_copy(update={"failed_native_start_sha256": digest})
    c.journals(lambda events: next(e for e in events if e["phase"] == "PARTIAL_NATIVE_START")["detail"].update(native_sha256=digest))
    with pytest.raises(GenerationUnavailable): c.verify()


@pytest.mark.parametrize("change", ["open", "generation", "runtime", "contract", "instance", "nonce", "process", "config_zero"])
def test_repeated_native_start_must_bind_new_closed_serving_process_and_recipe(registered, change):
    c = registered
    edits = {"open": {"admission_open": True}, "generation": {"generation_sha256": "0" * 64},
        "runtime": {"runtime_config_sha256": "0" * 64}, "contract": {"read_contract_sha256": "0" * 64},
        "instance": {"instance": "2" * 64}, "nonce": {"nonce": "1" * 64},
        "process": {"process_pid": 101, "process_start_ticks": 1001, "pid_namespace_inode": 321}}
    if change == "config_zero":
        prep = c.final.model_dump(mode="json")
        prep["receiver_release"]["units"]["api"]["config_sha256"] = "0" * 64
        c.seal(preparation=BootstrapPreparation.model_validate(prep))
    else: c.seal(native_start=c.native.model_copy(update=edits[change]))
    with pytest.raises(GenerationUnavailable): c.verify()


def test_exact_generation_and_original_owner_cannot_be_rebound(registered):
    c = registered
    wrong = c.c.bootstrap.manifest.model_copy(update={"acceptance_sha256": "0" * 64})
    with pytest.raises(GenerationUnavailable): require_closed_baseline(c.ref, wrong, production=False)
    q = BaselineQualification.model_validate(json.loads(Path(c.reg.qualification.path).read_bytes()))
    c.reg = c.reg.model_copy(update={"qualification": store(c.qroot, q.model_copy(update={"closed_owner_key": "0" * 64}))})
    c.ref = store(c.qroot, c.reg)
    with pytest.raises(GenerationUnavailable): c.verify()


def test_shared_hardlink_baseline_receipt_is_never_an_immutable_authority(registered):
    c = registered
    os.link(c.reg.qualification.path, c.qroot / "mutable-alias.json")
    with pytest.raises(GenerationUnavailable): c.verify()


def test_native_read_boundary_failure_is_reported_as_generation_unavailable(registered, monkeypatch):
    from vkm_corpus.update import bootstrap_verify as V
    from vkm_corpus.update.admission import AdmissionUnavailable
    def denied(*args):
        raise AdmissionUnavailable("synthetic native boundary refused")
    monkeypatch.setattr(V, "_ordinary_bytes", denied)
    with pytest.raises(GenerationUnavailable, match="closed baseline evidence is unavailable"):
        registered.verify()

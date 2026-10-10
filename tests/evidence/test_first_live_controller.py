"""SYNTHETIC lifecycle with real durable files and full private probe verifier.

The fake implements no Docker/network/model operation. These tests deliberately
do not count as native, recovery-capsule, firewall or reboot qualification.
"""
import asyncio
import hashlib
from pathlib import Path

import pytest

from test_shadow_acceptance import setup
from test_first_live_frontdoor import FakeNft
from vkm_corpus.update import acceptance as A
from vkm_corpus.update import first_live_native as N
from vkm_corpus.update.admission import read_state, require_open
from vkm_corpus.update.contracts import GenerationManifest
from vkm_corpus.update.first_live import (FirstLiveIntent, LiveAdmissionAuthority, CandidateDocuments,
    LegacyRecoveryBoundary, LegacyFile, verify_legacy_files, verify_candidate_documents, FirstLiveError)
from vkm_corpus.update.frontdoor import FrontdoorProfile, NativeFrontdoor
from vkm_corpus.update.operator import OperatorRelease
from vkm_corpus.update.operator_units import ComposeRelease, UnitPin, UnitControlConfig, bound_json
from vkm_corpus.update.runtime import BoundFile
from vkm_evidence.contracts import canonical_bytes, record_hash


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = canonical_bytes(value)
    path.write_bytes(data)
    path.chmod(0o600)
    return BoundFile(path=str(path), sha256=hashlib.sha256(data).hexdigest())


class Crash(BaseException):
    pass


@pytest.fixture
def rig(setup, tmp_path, monkeypatch):
    authority = tmp_path / "authority"
    root = tmp_path / "runtime/served"
    root.mkdir(parents=True)
    (root / "admission.lock").touch(mode=0o600)
    token = authority / "token"
    docs = {k: write(authority / (k + ".json"), {"logical": k}) for k in
            ("compose", "environment", "runtime", "native", "policy", "context")}
    effective = {u: write(authority / (u + ".effective.json"), {"unit": u}) for u in ("api", "mcp")}
    bundle = CandidateDocuments(documents=docs, effective=effective)
    units = {u: UnitPin(image_id="sha256:" + "a" * 64, config_sha256=record_hash({"unit": u})) for u in ("api", "mcp")}
    release = ComposeRelease(compose=docs["compose"], project="vkm-core", units=units,
        runtime_config_sha256="a" * 64, code_sha256=setup.plan.candidate.code_tree_sha256,
        dependencies_sha256=setup.plan.candidate.dependencies_sha256, access_sha256=setup.plan.candidate.access_config_sha256,
        mcp_read_principal_sha256="a" * 64)
    manifest = GenerationManifest(code_commit=setup.plan.candidate.code_commit, policy_sha256=setup.plan.candidate.policy_sha256,
        components=setup.plan.candidate.components, services=setup.plan.candidate.services, acceptance_sha256="a" * 64)
    candidate_ref = write(authority / "generation.json", manifest)
    plan_ref = write(authority / "plan.json", setup.plan)
    legacy_compose = write(authority / "legacy-compose.json", {"legacy": True})
    restored = (LegacyFile(file=legacy_compose, bytes=Path(legacy_compose.path).stat().st_size),)
    inventory = record_hash([{"sha256": r.file.sha256, "bytes": r.bytes} for r in restored])
    recovery = {"schema": "vkm-legacy-control-recovery/1", "status": "AUTHENTICATED_BYTES_VERIFIED",
        "inventory_sha256": inventory, "source_failure_domain": "source", "backup_failure_domain": "independent",
        "files": 1, "bytes": restored[0].bytes}
    legacy = LegacyRecoveryBoundary(restore_compose=legacy_compose, units=units,
        original_container_ids={u: "a" * 64 for u in units}, selectors=(docs["policy"],), restored_files=restored,
        independent_copy=write(authority / "copy.json", {**recovery, "kind": "COPY"}),
        independent_restore=write(authority / "restore.json", {**recovery, "kind": "RESTORE"}),
        source_failure_domain="source", backup_failure_domain="independent", shared_services_sha256=record_hash([]))
    control = UnitControlConfig(scope="SYNTHETIC", docker=BoundFile(path=str(authority / "docker"), sha256="a" * 64),
        compose_binary=BoundFile(path=str(authority / "docker-compose"), sha256="a" * 64),
        docker_socket=str(tmp_path / "docker.sock"), control_root=str(root), receiver_url="http://127.0.0.1:18000",
        mcp_receiver_url="http://127.0.0.1:18765", operator_token_file=str(token), releases=(release,))
    front = FrontdoorProfile(scope="SYNTHETIC", nft=BoundFile(path=str(authority / "nft"), sha256="a" * 64),
        tcp_ports=(8000, 8765, 8766, 18000, 18765))
    intent = FirstLiveIntent(scope="SYNTHETIC", request_id="first-live-test", candidate=candidate_ref,
        shadow_operator=docs["runtime"], shadow_acceptance=docs["context"], shadow_plan=plan_ref,
        shadow_documents=bundle, live_documents=bundle, mappings=(), legacy=legacy,
        candidate_release=release, release=OperatorRelease(receiver_release_sha256=release.sha256,
            environment=docs["environment"], runtime=docs["runtime"], generation=candidate_ref, acceptance_plan=plan_ref),
        units=control, frontdoor=front, authority_root=str(authority), qualification_root=str(setup.reg.root),
        protected_roots=(str(tmp_path / "originals"),), operator_code_sha256="a" * 64,
        operator_dependencies_sha256="b" * 64, operator_commit="c" * 40)
    intent_ref = write(authority / "intent.json", intent)
    grant = LiveAdmissionAuthority(intent=intent_ref, request_id=intent.request_id, control_root=str(root),
        candidate_generation_sha256=manifest.sha256)
    grant_ref = write(authority / "grant.json", grant)
    def source(ref, *, recovery_only=False):
        assert ref == grant_ref
        actual = LiveAdmissionAuthority.model_validate(bound_json(ref))
        current = FirstLiveIntent.model_validate(bound_json(actual.intent))
        verify_legacy_files(current.legacy)
        if actual != grant or current != intent:
            raise FirstLiveError("synthetic authority drift")
        if not recovery_only:
            GenerationManifest.model_validate(bound_json(current.candidate))
        return actual, current, None if recovery_only else manifest, None if recovery_only else setup.plan
    monkeypatch.setattr(N, "verify_authority", source)
    class Adapters:
        def __init__(self):
            self.frontdoor = NativeFrontdoor(front, _commands=FakeNft())
            self.restores, self.starts, self.epoch = 0, 0, 0
            self.selected = "legacy"
            self.fail = None
            self.event = None
        def fence(self, *, recovery_only=False):
            if self.fail == "identity": raise FirstLiveError("controller identity drift")
        def legacy_observe(self, *, original=False):
            if self.selected != "legacy": raise FirstLiveError("not legacy")
            return {"legacy_instance": self.epoch, "status": "UNQUALIFIED"}
        def legacy_restore(self):
            self.restores += 1
            self.epoch += 1
            self.selected = "legacy"
            return self.legacy_observe()
        def candidate_start(self, *, partial=False):
            self.starts += 1
            self.selected = "candidate"
            self.epoch += 1
            if self.fail == "restart": raise FirstLiveError("partial restart failure")
            return {"started": {"api": str(self.epoch)}, "stopped": {"mcp": str(self.epoch)}} if partial else {}
        def candidate_proof(self, gate, *, admission_open=False):
            if self.selected != "candidate": raise FirstLiveError("not candidate")
            return {"instance": self.epoch, "admission_open": admission_open, "nonce": "synthetic"}
        def private49(self, boundary, store):
            tools, checks, receipts = {}, {}, []
            if self.fail == "probe": raise FirstLiveError("private probe failed")
            asyncio.run(A._execute_private_probes(setup.plan, transport=setup.transport, fence=setup.fence,
                store=store, tools=tools, checks=checks, receipts=receipts,
                receipt_schema="vkm-first-live-raw-probe/1", receipt_plan_sha256=intent.sha256,
                boundary_check=boundary))
            return {"tools": tools, "checks": checks, "raw_probe_receipts": receipts}
        def close(self): pass
    adapters = Adapters()
    c = N.FirstLiveController(grant_ref, _synthetic_adapters=adapters)
    from types import SimpleNamespace
    yield SimpleNamespace(c=c, adapters=adapters, intent=intent, ref=grant_ref, root=root, legacy=legacy,
                         setup=setup, candidate_ref=candidate_ref)
    c.close()


def test_real_durable_drill_private49_open_and_lost_ack_resume(rig):
    r = rig
    out = r.c.rehearse(r.intent.sha256)
    assert out["status"] == "LEGACY_RESTORED_UNQUALIFIED_CLOSED" and not (r.root / "CURRENT").exists()
    assert read_state(r.root).status == "CLOSED"
    out = r.c.activate(r.intent.sha256)
    assert out["status"] == "FIRST_LIVE_ACTIVATED" and require_open(r.root).status == "OPEN"
    starts = r.adapters.starts
    assert r.c.resume(r.intent.sha256)["status"] == "FIRST_LIVE_ACTIVATED"
    assert r.adapters.starts == starts


@pytest.mark.parametrize("failure", ["restart", "probe"])
def test_partial_failure_restores_legacy_closed_never_qualified_previous(rig, failure):
    r = rig
    r.c.rehearse(r.intent.sha256)
    r.adapters.fail = failure
    with pytest.raises(FirstLiveError): r.c.activate(r.intent.sha256)
    assert r.adapters.selected == "legacy" and read_state(r.root).status == "CLOSED"
    assert not (r.root / "CURRENT").exists()
    r.adapters.frontdoor.observe(closed=True)


@pytest.mark.parametrize("phase", ["FIRST_CANDIDATE_SELECTED", "FIRST_LIVE_PRIVATE49_VERIFIED", "RECEIVERS_VERIFIED",
    "FIRST_LIVE_DURABLE_OPEN", "FIRST_LIVE_KERNEL_GATE_RELEASED", "FIRST_LIVE_OPEN_RECEIVERS_VERIFIED",
    "FIRST_LIVE_OPEN_PREPARED", "FIRST_LIVE_HOST_UNSEALED", "FIRST_LIVE_ACTIVATED", "RECEIPT_PUBLISHED"])
def test_crash_recovery_uses_retained_closure_when_candidate_disappears(rig, phase):
    r = rig
    r.c.rehearse(r.intent.sha256)
    r.c.fault = lambda value: (_ for _ in ()).throw(Crash()) if value == phase else None
    with pytest.raises(Crash): r.c.activate(r.intent.sha256)
    r.c.fault = None
    Path(r.candidate_ref.path).unlink()
    out = r.c.recover(r.intent.sha256)
    assert out["status"] == "LEGACY_RESTORED_UNQUALIFIED_CLOSED"
    restored = r.adapters.restores
    assert r.c.recover(r.intent.sha256)["receipt_sha256"] == out["receipt_sha256"]
    assert r.adapters.restores == restored and read_state(r.root).status == "CLOSED"


@pytest.mark.parametrize("phase", ["FIRST_LIVE_DURABLE_OPEN", "FIRST_LIVE_KERNEL_GATE_RELEASED",
    "FIRST_LIVE_OPEN_RECEIVERS_VERIFIED", "FIRST_LIVE_HOST_UNSEALED", "FIRST_LIVE_ACTIVATED", "RECEIPT_PUBLISHED"])
def test_lost_ack_can_complete_exact_still_running_candidate_without_restart(rig, phase):
    r = rig
    r.c.rehearse(r.intent.sha256)
    r.c.fault = lambda value: (_ for _ in ()).throw(Crash()) if value == phase else None
    with pytest.raises(Crash): r.c.activate(r.intent.sha256)
    r.c.fault = None
    starts = r.adapters.starts
    assert r.c.resume(r.intent.sha256)["status"] == "FIRST_LIVE_ACTIVATED"
    assert r.adapters.starts == starts


def test_resume_does_not_requalify_restarted_candidate(rig):
    r = rig
    r.c.rehearse(r.intent.sha256)
    r.c.activate(r.intent.sha256)
    r.adapters.epoch += 1
    with pytest.raises(FirstLiveError, match="process changed"):
        r.c.resume(r.intent.sha256)
    assert read_state(r.root).status == "CLOSED"
    r.adapters.frontdoor.observe(closed=True)


def test_existing_current_rejects_before_firewall_or_restart(rig):
    (rig.root / "CURRENT").write_text("foreign")
    with pytest.raises(FirstLiveError): rig.c.rehearse(rig.intent.sha256)
    assert rig.adapters.frontdoor.commands.calls == [] and rig.adapters.starts == 0


def test_drifted_authority_rejected_before_open(rig):
    rig.c.rehearse(rig.intent.sha256)
    Path(rig.ref.path).write_bytes(b"{}")
    with pytest.raises((FirstLiveError, ValueError)): rig.c.activate(rig.intent.sha256)
    assert rig.adapters.starts == 1 and read_state(rig.root).status == "CLOSED"


def test_recovery_verifies_bytes_not_only_pass_or_size(rig):
    verify_legacy_files(rig.legacy)
    path = Path(rig.legacy.restore_compose.path)
    raw = path.read_bytes()
    path.write_bytes(b"x" * len(raw))
    with pytest.raises(FirstLiveError): verify_legacy_files(rig.legacy)


def test_candidate_control_diff_rejects_unmapped_semantics(rig):
    verify_candidate_documents(rig.intent)
    current = rig.intent.live_documents
    ref = write(Path(rig.intent.authority_root) / "changed-native.json", {"logical": "other"})
    changed = current.model_copy(update={"documents": {**current.documents, "native": ref}})
    with pytest.raises(FirstLiveError, match="unmapped"):
        verify_candidate_documents(rig.intent.model_copy(update={"live_documents": changed}))


def test_effective_native_configuration_is_part_of_semantic_diff(rig):
    current = rig.intent.live_documents
    ref = write(Path(rig.intent.authority_root) / "changed-effective.json", {"unit": "api", "unreviewed": True})
    changed = current.model_copy(update={"effective": {**current.effective, "api": ref}})
    with pytest.raises(FirstLiveError, match="unmapped"):
        verify_candidate_documents(rig.intent.model_copy(update={"live_documents": changed}))


def test_project_mapping_cannot_change_arbitrary_named_native_semantics(rig):
    from vkm_corpus.update.first_live import FirstLiveMapping
    root = Path(rig.intent.authority_root)
    before = write(root / "native-before.json", {"name": "vkm-core-shadow"})
    after = write(root / "native-after.json", {"name": "vkm-core"})
    shadow = rig.intent.shadow_documents.model_copy(update={"documents": {**rig.intent.shadow_documents.documents, "native": before}})
    live = rig.intent.live_documents.model_copy(update={"documents": {**rig.intent.live_documents.documents, "native": after}})
    mapping = FirstLiveMapping(document="native", pointer="/name", before="vkm-core-shadow", after="vkm-core", kind="PROJECT")
    with pytest.raises(FirstLiveError, match="unapproved"):
        verify_candidate_documents(rig.intent.model_copy(update={"shadow_documents": shadow, "live_documents": live, "mappings": (mapping,)}))


def test_synthetic_copy_restore_wrappers_cannot_be_promoted_to_production(rig):
    value = rig.legacy.model_dump(mode="json")
    value["scope"] = "RECEIVER_CONTROL_ONLY"
    with pytest.raises(ValueError, match="owner-approved"):
        LegacyRecoveryBoundary.model_validate(value)


@pytest.mark.parametrize("drift", [None, "source-path", "restored-path", "version", "duplicate-source"])
def test_each_legacy_selector_has_an_exact_original_to_restored_link(rig, drift):
    from vkm_corpus.update.first_live import RetainedSharedObserver, RestoredLegacySelector
    from vkm_corpus.update.contracts import ServiceIdentity
    base = rig.legacy.restore_compose
    service = ServiceIdentity(service="CONTROL", **dict.fromkeys(("instance_sha256", "code_sha256", "dependencies_sha256",
        "config_sha256", "endpoint_sha256", "runtime_sha256"), "c" * 64))
    observer = RetainedSharedObserver(environment=base, runtime=base, native=base, duckdb=base, nav=base,
        max_local_identity_bytes=1000, components=rig.setup.plan.candidate.components, services=(service,))
    original = base.model_copy(update={"path": str(Path(base.path).parent / "original-selector.json")})
    restored = base
    source = original
    if drift == "source-path": source = original.model_copy(update={"path": original.path + ".unregistered"})
    elif drift == "restored-path": restored = base.model_copy(update={"path": base.path + ".uncopied"})
    elif drift == "version": restored = base.model_copy(update={"sha256": "f" * 64})
    value = {**rig.legacy.model_dump(mode="json"), "scope": "RECEIVER_CONTROL_ONLY", "observer": observer.model_dump(mode="json"),
        "owner_approval": base.model_dump(mode="json"), "selectors": [original.model_dump(mode="json")],
        "selector_restores": [{"source": source.model_dump(mode="json"), "restored": restored.model_dump(mode="json")} ]}
    if drift == "duplicate-source": value["selector_restores"] *= 2
    if drift is None:
        assert LegacyRecoveryBoundary.model_validate(value).selector_restores == (RestoredLegacySelector(source=original, restored=base),)
    else:
        with pytest.raises(ValueError): LegacyRecoveryBoundary.model_validate(value)


def test_boot_gate_cli_status_is_explicit_and_does_not_print_exception_payload(monkeypatch, capsys):
    from vkm_corpus.update.first_live import BootQualificationUnavailable
    monkeypatch.setattr(N, "FirstLiveController", lambda *a, **kw: (_ for _ in ()).throw(BootQualificationUnavailable("secret not emitted")))
    assert N.main(["status", "--authority", "irrelevant", "--sha256", "a" * 64]) == 2
    assert capsys.readouterr().out.strip() == '{"status": "BLOCKED_BY_ACTUAL_BOOT_QUALIFICATION"}'


@pytest.mark.parametrize("recovery_only", [False, True])
def test_production_controller_blocks_before_paths_barrier_or_adapters(monkeypatch, tmp_path, recovery_only):
    from types import SimpleNamespace
    from vkm_corpus.update.first_live import FirstLiveNotReady
    untouched = tmp_path / "must-not-exist"
    monkeypatch.setattr(N, "verify_authority", lambda *a, **kw:
        (object(), SimpleNamespace(scope="FIRST_LIVE_PRODUCTION"), None, None))
    def forbidden(*args, **kwargs):
        pytest.fail("no production path, barrier, journal, registry or adapter construction")
    for name in ("Path", "NativeFirstLiveAdapters", "ReceiverBarrier", "DurableDeployment", "AcceptanceRegistrar"):
        monkeypatch.setattr(N, name, forbidden)
    with pytest.raises(FirstLiveNotReady):
        N.FirstLiveController(object(), recovery_only=recovery_only)
    assert not untouched.exists()


@pytest.mark.parametrize("action", ["status", "rehearse", "activate", "restore", "resume"])
def test_not_ready_cli_is_explicit_and_does_not_print_exception_payload(monkeypatch, capsys, action):
    from vkm_corpus.update.first_live import FirstLiveNotReady
    monkeypatch.setattr(N, "FirstLiveController", lambda *a, **kw:
        (_ for _ in ()).throw(FirstLiveNotReady("secret container/config path must not be printed")))
    assert N.main([action, "--authority", "irrelevant", "--sha256", "a" * 64]) == 2
    assert capsys.readouterr().out.strip() == (
        '{"status": "FIRST_LIVE_NOT_READY", '
        '"reason": "UNQUALIFIED_NATIVE_CREATE_CAN_DELETE_RETAINED_LEGACY"}')


def test_shared_drift_after_legacy_recreation_keeps_closed_without_success_receipt(rig):
    original = rig.adapters.fence
    def fence(**kw):
        original(**kw)
        if rig.adapters.restores:
            raise FirstLiveError("shared native resources changed")
    rig.adapters.fence = fence
    with pytest.raises(FirstLiveError, match="shared native"):
        rig.c.rehearse(rig.intent.sha256)
    assert rig.adapters.restores == 1
    assert read_state(rig.root).status == "CLOSED"
    rig.adapters.frontdoor.observe(closed=True)
    assert not (rig.root / "FIRST_LIVE_RESULT").exists()
    assert not any(e["phase"] == "FIRST_LIVE_FALLBACK_DRILL_VERIFIED" for e in rig.c.journal.history(rig.intent.request_id))

"""Synthetic full-contract probes and actual local Linux lease negatives.

No fixture below is a production corpus, Docker receiver or qualified baseline.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import platform
from types import SimpleNamespace

import pytest

from test_shadow_acceptance import setup as shadow_setup
from vkm_corpus.update import acceptance as A
from vkm_corpus.update import bootstrap_probes as B
from vkm_corpus.update.admission import AdmissionState
from vkm_corpus.update.bootstrap import (BootstrapIntent, BootstrapProbePlan, BootstrapRecipe,
                                        BootstrapStartupAuthority, LegacyTopology)
from vkm_corpus.update.contracts import GenerationManifest
from vkm_corpus.update.runtime import BoundFile
from vkm_evidence.contracts import canonical_bytes, record_hash


def bound(path, value):
    path.write_bytes(canonical_bytes(value))
    return BoundFile(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def control_case(s, tmp_path, *, production=False):
    root = tmp_path / "control"
    root.mkdir()
    trusted = tmp_path / "authority"
    trusted.mkdir()
    dummy = bound(trusted / "input.json", {"scope": "SYNTHETIC", "data": "test-only"})
    legacy = LegacyTopology(compose=dummy, selectors=(dummy,), units={name: {
        "image_id": "sha256:" + "a" * 64, "config_sha256": "b" * 64,
        "container_id": letter * 64, "started_at": "synthetic"}
        for name, letter in (("api", "c"), ("mcp", "d"))})
    probes = s.plan.model_copy(update={"scope": "SHADOW_PRODUCTION" if production else "SYNTHETIC",
        "drill_receipt_sha256": None, "isolation_attestation_sha256": "1" * 64})
    startup = BootstrapStartupAuthority(bootstrap_id="synthetic-bootstrap", request_id="synthetic-request",
        candidate=probes.candidate, control_root=str(root), isolation_attestation_sha256="1" * 64,
        legacy_topology_sha256=legacy.sha256, independent_backup_sha256=dummy.sha256)
    startup_ref = bound(trusted / "startup.json", startup)
    scope = "BOOTSTRAP_SHADOW_PRODUCTION" if production else "SYNTHETIC"
    intent = BootstrapIntent(scope=scope, bootstrap_id=startup.bootstrap_id, request_id=startup.request_id,
        startup_authority=startup_ref, probe_spec_sha256=record_hash(probes),
        deployment_profile_sha256=probes.deployment_profile_sha256, environment=dummy, runtime=dummy,
        independent_backup=dummy, legacy=legacy, recipe=BootstrapRecipe(compose=dummy,
            images={name: "sha256:" + "e" * 64 for name in ("api", "mcp")}, networks=({
                "name": "synthetic-isolated", "network_id": "f" * 64, "config_sha256": "a" * 64},)))
    intent_ref = bound(trusted / "intent.json", intent)
    plan = BootstrapProbePlan(scope=scope, intent_sha256=intent.sha256, startup_authority_sha256=startup.sha256,
        legacy_topology_sha256=legacy.sha256, independent_backup_sha256=dummy.sha256, probes=probes)
    pin = startup.candidate
    generation = GenerationManifest(components=pin.components, services=pin.services, code_commit=pin.code_commit,
                                   policy_sha256=pin.policy_sha256, acceptance_sha256=startup.sha256)
    (root / "CURRENT").write_bytes((generation.sha256 + "\n").encode("ascii"))
    (root / (generation.sha256 + ".json")).write_bytes(canonical_bytes(generation))
    (root / "ADMISSION.json").write_bytes(canonical_bytes(AdmissionState(status="CLOSED", request_key=startup.request_key)))
    if not production:
        (root / "MAINTENANCE").write_text(startup.request_key, encoding="ascii")
    for name in ("writer.lock", "admission.lock"):
        (root / name).write_bytes(b"")
    return SimpleNamespace(s=s, root=root, startup=startup, startup_ref=startup_ref, intent=intent,
        intent_ref=intent_ref, plan=plan, generation=generation,
        registrar=B.BootstrapProbeRegistrar(s.reg.root, approved_plan_sha256=plan.sha256))


@pytest.fixture()
def bootstrap_case(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "serving_dependencies_identity", lambda: "d" * 64)
    s = shadow_setup.__wrapped__(tmp_path, monkeypatch)
    value = control_case(s, tmp_path)
    value.authority = B.ClosedBootstrapAuthority(value.startup_ref, value.plan, intent=value.intent_ref)
    yield value
    value.authority.close()
    s.fence.close()


def run(case, *, transport="default"):
    return asyncio.run(B.qualify_closed_bootstrap(case.plan,
        transport=case.s.transport if transport == "default" else transport,
        fence=case.s.fence, registrar=case.registrar, authority=case.authority))


def report(case, out):
    return json.loads(case.registrar.read(out["receipt_sha256"]))


def test_full_49_private_probes_are_separate_from_serving_and_baseline(bootstrap_case):
    c = bootstrap_case
    out = run(c)
    assert out["status"] == "PRIVATE_PROBES_VERIFIED", out
    assert len(out["tools"]) == 49 and set(out["tools"]) == A.read_tool_names()
    assert out["checks"] == dict.fromkeys(B.BOOTSTRAP_CHECKS, "PASS")
    assert out["admission"] == "CLOSED" and out["serving_admission"] is False
    assert out["scientific_admission"] is False and out["scope"] == "SYNTHETIC"
    assert "failed_switch" not in out["checks"] and "rollback" not in out["checks"]
    assert b"secret-reader" not in c.registrar.read(out["receipt_sha256"])
    ordinary = A.AcceptanceRegistrar(c.s.reg.root, approved_plan_sha256=c.plan.probes.sha256)
    with pytest.raises(A.AcceptanceError):
        ordinary.register(report(c, out), c.plan.probes)
    # Removing the ordinary drill still prevents ordinary qualification.
    ordinary_out = asyncio.run(A.qualify_shadow(c.plan.probes, transport=c.s.transport,
        fence=c.s.fence, registrar=ordinary))
    assert ordinary_out["status"] == "NOT_RUN"


def test_missing_transport_is_not_run_without_success_receipt(bootstrap_case):
    out = run(bootstrap_case, transport=None)
    assert out["status"] == "NOT_RUN" and not out["tools"] and "receipt_sha256" not in out


def test_absent_maintenance_mode_is_fixed_for_every_private_probe(bootstrap_case):
    c = bootstrap_case
    c.authority.close()
    (c.root / "MAINTENANCE").unlink()
    c.authority = B.ClosedBootstrapAuthority(c.startup_ref, c.plan, intent=c.intent_ref)
    assert c.authority.check()["maintenance_present"] is False
    def restore_flag():
        (c.root / "MAINTENANCE").write_text(c.startup.request_key, encoding="ascii")
    c.s.transport.on_call = restore_flag
    out = run(c)
    assert out["status"] == "FAIL" and "receipt_sha256" not in out


@pytest.mark.parametrize("fault", ["startup", "intent", "intent_input", "open", "owner", "maintenance", "current",
                                      "generation", "closed_lease", "policy", "native"])
def test_mid_probe_state_or_authority_drift_never_acknowledges(bootstrap_case, fault):
    c = bootstrap_case
    def drift():
        if fault in {"startup", "intent"}:
            Path(getattr(c, fault + "_ref").path).write_bytes(b"{}")
        elif fault == "intent_input":
            Path(c.intent.environment.path).write_bytes(b"{}")
        elif fault == "open":
            (c.root / "ADMISSION.json").write_bytes(canonical_bytes(AdmissionState(status="OPEN",
                request_key=c.startup.request_key, generation_sha256=c.generation.sha256,
                verified_event_sha256="a" * 64)))
        elif fault == "owner":
            (c.root / "ADMISSION.json").write_bytes(canonical_bytes(AdmissionState(status="CLOSED", request_key="0" * 64)))
        elif fault == "maintenance":
            (c.root / "MAINTENANCE").unlink(missing_ok=True)
        elif fault == "current":
            (c.root / "CURRENT").write_bytes(("0" * 64 + "\n").encode("ascii"))
        elif fault == "generation":
            (c.root / (c.generation.sha256 + ".json")).write_bytes(b"{}")
        elif fault == "closed_lease":
            c.authority.close()
        elif fault == "policy":
            c.s.fence.policy_path.write_bytes(b"changed")
        else:
            c.s.observed["DUCKDB"]["revision"] = "changed"
    c.s.transport.on_call = drift
    out = run(c)
    assert out["status"] == "FAIL" and "receipt_sha256" not in out


@pytest.mark.parametrize("fault", ["missing_tool", "unexpected_tool", "missing_image", "wrong_snapshot", "policy_allows"])
def test_full_private_contract_is_not_weakened_for_bootstrap(bootstrap_case, fault):
    c = bootstrap_case
    if fault == "missing_tool":
        c.s.transport.names.pop()
    elif fault == "unexpected_tool":
        c.s.transport.names.append("dangerous_write")
    elif fault == "policy_allows":
        from test_shadow_acceptance import body
        async def allow(*_):
            return {"http_status": 200, "body": body()}
        c.s.transport.api = allow
    else:
        def edit(name, value):
            if fault == "missing_image" and name == "get_page_image":
                value["content"] = []
            elif fault == "wrong_snapshot" and name != "get_corpus_status":
                value["structuredContent"]["meta"]["canonical_snapshot_id"] = "old"
            return value
        c.s.transport.edit = edit
    out = run(c)
    assert out["status"] == "FAIL" and "receipt_sha256" not in out


@pytest.mark.parametrize("fault", ["omitted_receipt", "forged_probe", "wrong_intent", "other_schema", "ready", "ordinary_probe"])
def test_registration_rechecks_all_bound_evidence(bootstrap_case, fault):
    c = bootstrap_case
    out = run(c)
    assert out["status"] == B.VERIFIED, out
    proof = report(c, out)
    if fault == "omitted_receipt":
        proof["raw_probe_receipts"].pop()
    elif fault in {"forged_probe", "ordinary_probe"}:
        original = json.loads(c.registrar.read(proof["raw_probe_receipts"][0]))
        original["status" if fault == "forged_probe" else "schema"] = "NOT_RUN" if fault == "forged_probe" else "vkm-shadow-probe/1"
        proof["raw_probe_receipts"][0] = c.registrar.store(original)
    elif fault == "wrong_intent":
        proof["intent_sha256"] = "0" * 64
    elif fault == "other_schema":
        proof["schema"] = "vkm-serving-acceptance/1"
    else:
        proof["status"] = "READY"
    with pytest.raises(A.AcceptanceError):
        c.registrar.register(proof, c.plan, authority=c.authority)


def test_changed_intent_with_valid_hash_still_must_match_approved_plan(bootstrap_case):
    c = bootstrap_case
    changed = c.intent.model_copy(update={"probe_spec_sha256": "0" * 64})
    ref = bound(Path(c.intent_ref.path), changed)
    altered_plan = c.plan.model_copy(update={"intent_sha256": changed.sha256})
    with pytest.raises(ValueError, match="differ"):
        B.ClosedBootstrapAuthority(c.startup_ref, altered_plan, intent=ref)


def test_lost_ack_requires_fresh_closed_state_on_idempotent_retry(bootstrap_case, monkeypatch):
    from vkm_evidence import cli
    c = bootstrap_case
    out = run(c)
    assert out["status"] == B.VERIFIED, out
    proof = report(c, out)
    original = cli._qualification_sync_directory
    monkeypatch.setattr(cli, "_qualification_sync_directory", lambda _: (_ for _ in ()).throw(OSError("lost ACK")))
    with pytest.raises(OSError):
        c.registrar.register(proof, c.plan, authority=c.authority)
    monkeypatch.setattr(cli, "_qualification_sync_directory", original)
    assert c.registrar.register(proof, c.plan, authority=c.authority) == out["receipt_sha256"]
    (c.root / "CURRENT").write_bytes(("0" * 64 + "\n").encode("ascii"))
    with pytest.raises(A.AcceptanceError):
        c.registrar.register(proof, c.plan, authority=c.authority)


def test_exception_payload_never_leaks_into_receipts(bootstrap_case):
    c = bootstrap_case
    def fail():
        raise ValueError("PRIVATE SECRET TOKEN AND SOURCE VALUE")
    c.s.transport.on_call = fail
    out = run(c)
    raw = c.registrar.read(out["attempt_receipt_sha256"])
    assert out["status"] == "FAIL" and b"PRIVATE SECRET" not in raw and out["error_type"] == "ValueError"


def test_registration_cannot_acknowledge_closure_lost_during_durable_write(bootstrap_case, monkeypatch):
    c = bootstrap_case
    original = c.registrar.store
    def store(value):
        digest = original(value)
        if value.get("status") == B.VERIFIED:
            (c.root / "MAINTENANCE").unlink()
        return digest
    monkeypatch.setattr(c.registrar, "store", store)
    out = run(c)
    assert out["status"] == "FAIL" and "receipt_sha256" not in out


LINUX_REASON = "NOT_RUN: Linux native bootstrap flock and inotify qualification"


@pytest.fixture()
def native_case(tmp_path, monkeypatch):
    import fcntl
    monkeypatch.setattr(A, "serving_dependencies_identity", lambda: "d" * 64)
    s = shadow_setup.__wrapped__(tmp_path, monkeypatch)
    c = control_case(s, tmp_path, production=True)
    fds = [os.open(c.root / name, os.O_RDONLY) for name in ("writer.lock", "admission.lock")]
    for fd in fds:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    c.fds = fds
    c.authority = B.ClosedBootstrapAuthority(c.startup_ref, c.plan, intent=c.intent_ref,
                                            writer_fd=fds[0], gate_fd=fds[1])
    yield c
    c.authority.close()
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass


@pytest.mark.skipif(platform.system() != "Linux", reason=LINUX_REASON)
@pytest.mark.parametrize("fault", ["unlock_writer", "unlock_gate", "shared", "closed_fd", "replace_gate", "mutate_restore"])
def test_actual_native_lease_loss_is_not_an_owned_writer(native_case, fault):
    import fcntl
    c = native_case
    assert set(c.authority.check()["native_exclusive_leases"]) == {"writer", "gate"}
    if fault.startswith("unlock"):
        fcntl.flock(c.fds[0 if fault == "unlock_writer" else 1], fcntl.LOCK_UN)
    elif fault == "shared":
        fcntl.flock(c.fds[1], fcntl.LOCK_SH)
    elif fault == "closed_fd":
        os.close(c.fds[0])
    elif fault == "replace_gate":
        path = c.root / "admission.lock"
        path.unlink()
        path.write_bytes(b"")
    else:
        path = c.root / "CURRENT"
        old = path.read_bytes()
        path.write_bytes(b"0" * 64)
        path.write_bytes(old)
    with pytest.raises((ValueError, OSError)):
        c.authority.check()


@pytest.mark.skipif(platform.system() != "Linux", reason=LINUX_REASON)
def test_native_fds_are_borrowed_and_fake_transport_never_qualifies_production(native_case):
    import fcntl
    c = native_case
    with pytest.raises(A.AcceptanceError, match="real private"):
        run(c)
    c.authority.close()
    for fd in c.fds:
        assert os.fstat(fd)
    other = os.open(c.root / "admission.lock", os.O_RDONLY)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(other)

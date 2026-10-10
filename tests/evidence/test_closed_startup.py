"""Closed bootstrap is a metadata capability, never serving acceptance.

All artifacts and tiny DuckDB files here are synthetic. The local
receiver tests exercise the real binder and HTTP authorization; they do not
qualify a remote store, shadow deployment, model or a bootstrap baseline.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from vkm_corpus.api.app import create_app
from vkm_corpus.api import production
from vkm_corpus.api.service import ApiService
from vkm_corpus.contracts.access import AccessContext
from vkm_corpus.update.acceptance import CandidatePin
from vkm_corpus.update.admission import AdmissionState, STATE_FILE, require_open
from vkm_corpus.update.bootstrap import BootstrapStartupAuthority, require_closed_startup
from vkm_corpus.update.contracts import GenerationManifest, ServiceIdentity
from vkm_corpus.update.generation import GenerationUnavailable
from vkm_corpus.update.receiver import (RECEIVER_ROUTE, ReceiverChallenge, verify_identity,
                                       read_credential_binding)
from vkm_corpus.update.runtime import RuntimeConfig
from vkm_evidence.contracts import canonical_bytes, record_hash

from test_production_serving import setup, write, requires_linux_receiver


TOKEN = "synthetic-bootstrap-operator-token-" + "x" * 32


@pytest.fixture
def closed(tmp_path):
    deps, runtime, cfg, root = setup(tmp_path)
    cfg = replace(cfg, deployment_token=TOKEN)
    served = runtime.root / "served"
    previous = GenerationManifest.model_validate_json(
        (served / ((served / "CURRENT").read_text().strip() + ".json")).read_bytes())
    proof = json.loads((Path(runtime.config.qualification_root) /
                        (previous.acceptance_sha256 + ".json")).read_bytes())
    candidate = CandidatePin(components=previous.components, services=previous.services,
        **{key: proof[key] for key in ("code_commit", "code_tree_sha256", "dependencies_sha256",
                                      "access_config_sha256", "policy_sha256", "duckdb_file_sha256")})
    authority = BootstrapStartupAuthority(bootstrap_id="synthetic-bootstrap", request_id="synthetic-startup",
        candidate=candidate, control_root=str(served), isolation_attestation_sha256="a" * 64,
        legacy_topology_sha256="b" * 64, independent_backup_sha256="c" * 64)
    ref = write(tmp_path / "operator" / "startup.json", authority)
    # RuntimeConfig requires an independently pinned native profile. These
    # tests deliberately bind local-only dependencies, not the async remote
    # factory; no placeholder is treated as an observed remote identity.
    native = write(tmp_path / "operator" / "native.json", {"scope": "SYNTHETIC", "status": "NOT_RUN"})
    runtime.config = RuntimeConfig.model_validate({**runtime.config.model_dump(mode="json"),
        "bootstrap_startup": ref.model_dump(mode="json"), "native_serving": native.model_dump(mode="json")})
    manifest = previous.model_copy(update={"acceptance_sha256": authority.sha256})
    write(served / (manifest.sha256 + ".json"), manifest)
    (served / "CURRENT").write_bytes((manifest.sha256 + "\n").encode("ascii"))
    write(served / STATE_FILE, AdmissionState(status="CLOSED", request_key=authority.request_key))
    value = SimpleNamespace(deps=deps, runtime=runtime, cfg=cfg, root=root, served=served,
                            authority=authority, manifest=manifest, ref=ref)
    try:
        yield value
    finally:
        if lease := getattr(deps, "serving_file_lease", None):
            lease.close()


def test_exact_closed_authority_returns_only_native_byte_pins(closed):
    result = require_closed_startup(closed.runtime, closed.manifest, closed.cfg)
    assert result == {"duckdb_file_sha256": closed.authority.candidate.duckdb_file_sha256,
                      "nav_file_sha256": None, "mode": "SHADOW_CLOSED_METADATA"}
    assert not ({"status", "receipt", "serving_admission", "scientific_admission"} & result.keys())


def test_startup_authority_cannot_be_relabelled_ordinary_serving_acceptance(closed):
    target = Path(closed.runtime.config.qualification_root) / (closed.authority.sha256 + ".json")
    target.write_bytes(Path(closed.ref.path).read_bytes())
    with pytest.raises(GenerationUnavailable, match="unqualified"):
        production.require_serving_acceptance(closed.runtime, closed.manifest, closed.cfg)
    # Removing the explicit startup mode cannot elevate the same immutable
    # authority to an ordinary receipt, even when its bytes are in that store.
    closed.runtime.config = closed.runtime.config.model_copy(update={"bootstrap_startup": None})
    with pytest.raises(GenerationUnavailable, match="unqualified"):
        production._require_startup_proof(closed.runtime, closed.manifest, closed.cfg)


@pytest.mark.parametrize("fault", ["code", "dependencies", "access", "missing_token", "missing_authority",
    "authority_bytes", "wrong_owner", "runtime_commit", "runtime_policy", "acceptance", "generation_code",
    "component_identity", "service_identity"])
def test_startup_rejects_unqualified_identity_and_authority(closed, monkeypatch, fault):
    runtime, cfg, manifest = closed.runtime, closed.cfg, closed.manifest
    if fault in {"code", "dependencies"}:
        function = "serving_code_identity" if fault == "code" else "serving_dependencies_identity"
        monkeypatch.setattr(production, function, lambda: "9" * 64)
    elif fault == "access":
        cfg = replace(cfg, access_contexts={"reader": AccessContext(principal="other", execution="LOCAL")})
    elif fault == "missing_token":
        cfg = replace(cfg, deployment_token=None)
    elif fault == "missing_authority":
        runtime.config = runtime.config.model_copy(update={"bootstrap_startup": None})
    elif fault == "authority_bytes":
        Path(closed.ref.path).write_bytes(canonical_bytes(closed.authority.model_copy(update={"request_id": "other"})))
    elif fault == "wrong_owner":
        write(closed.served / STATE_FILE, AdmissionState(status="CLOSED", request_key="9" * 64))
    elif fault == "runtime_commit":
        runtime.config = runtime.config.model_copy(update={"expected_commit": "9" * 40})
    elif fault == "runtime_policy":
        runtime.config = runtime.config.model_copy(update={"policy": runtime.config.policy.model_copy(update={"sha256": "9" * 64})})
    elif fault == "acceptance":
        manifest = manifest.model_copy(update={"acceptance_sha256": "9" * 64})
    elif fault == "generation_code":
        manifest = manifest.model_copy(update={"code_commit": "9" * 40})
    elif fault == "component_identity":
        manifest = manifest.model_copy(update={"components": tuple(
            c.model_copy(update={"manifest_sha256": "9" * 64}) if c.component == "DUCKDB" else c
            for c in manifest.components)})
    else:
        service = ServiceIdentity(service="CONTROL", instance_sha256="9" * 64, code_sha256="9" * 64,
            dependencies_sha256="9" * 64, config_sha256="9" * 64, endpoint_sha256="9" * 64,
            runtime_sha256="9" * 64)
        manifest = manifest.model_copy(update={"services": (service,)})
    with pytest.raises((GenerationUnavailable, ValueError)):
        require_closed_startup(runtime, manifest, cfg)


def _forge_open(closed):
    # Complete syntactic OPEN proof: the test must reach the capability guard,
    # not merely fail because OPEN lacks its journal or generation binding.
    event = {"schema": "vkm-deployment-event/1", "request_key": closed.authority.request_key,
             "parent_sha256": None, "phase": "RECEIVERS_VERIFIED", "detail": {
                 "generation_sha256": closed.manifest.sha256, "native_sha256": "d" * 64,
                 "receiver_proofs": {"synthetic-api": "e" * 64}}}
    digest = record_hash(event)
    write(closed.served / "journals" / (digest + ".json"), event)
    write(closed.served / STATE_FILE, AdmissionState(status="OPEN", request_key=closed.authority.request_key,
        generation_sha256=closed.manifest.sha256, verified_event_sha256=digest))
    assert require_open(closed.served).generation_sha256 == closed.manifest.sha256


def test_even_complete_open_proof_is_not_closed_startup_authority(closed):
    _forge_open(closed)
    with pytest.raises(GenerationUnavailable, match="context differs"):
        require_closed_startup(closed.runtime, closed.manifest, closed.cfg)


@requires_linux_receiver
def test_actual_closed_receiver_proves_metadata_but_never_serves_content(closed):
    guard = production.bind_generation_guard(closed.deps, closed.runtime, closed.cfg, closed.root)
    assert guard() == {"status": "UNAVAILABLE"}
    service = ApiService(closed.deps)
    app = create_app(service, closed.cfg)

    async def probe():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://synthetic") as client:
            for token in (None, "read", "wrong-operator-token"):
                headers = {} if token is None else {"Authorization": "Bearer " + token}
                result = await client.post(RECEIVER_ROUTE, json={"nonce": "1" * 64}, headers=headers)
                assert result.status_code == 401 and result.json() == {"status": "UNAUTHORIZED"}
            result = await client.post(RECEIVER_ROUTE, json={"nonce": "1" * 64}, headers={
                "Authorization": "Bearer " + TOKEN, "X-VKM-Read-Authorization": "Bearer read"})
            assert result.status_code == 200
            pin = closed.authority.candidate
            proof = verify_identity(result.content, token=TOKEN, challenge=ReceiverChallenge(nonce="1" * 64),
                manifest=closed.manifest, runtime_sha256=record_hash(closed.runtime.config),
                code_sha256=pin.code_tree_sha256, dependencies_sha256=pin.dependencies_sha256,
                access_sha256=pin.access_config_sha256, gate=closed.deps.admission_barrier._gate_identity,
                admission_open=False)
            assert proof.read_principal_sha256 == record_hash(closed.cfg.access_contexts["reader"])
            assert proof.read_credential_sha256 == read_credential_binding(TOKEN, "Bearer read")
            content = await client.get("/v1/source/VKM-SRC-001", headers={"Authorization": "Bearer read"})
            assert content.status_code == 503
            assert not closed.deps.admission_barrier.status()["active_requests"]
            _forge_open(closed)
            assert closed.deps.admission_barrier.status()["admission_open"]
            assert guard() == {"status": "UNAVAILABLE"}
            content = await client.get("/v1/source/VKM-SRC-001", headers={"Authorization": "Bearer read"})
            assert content.status_code == 503
            # Rebinding a native proof also refuses OPEN: metadata must not
            # attest that the bootstrap authority admitted the generation.
            result = await client.post(RECEIVER_ROUTE, json={"nonce": "2" * 64},
                headers={"Authorization": "Bearer " + TOKEN})
            assert result.status_code == 503 and result.json() == {"status": "UNAVAILABLE"}
            assert guard() == {"status": "UNAVAILABLE"}
    asyncio.run(probe())


@requires_linux_receiver
@pytest.mark.parametrize("fault", ["policy_bytes", "canonical_bytes", "current_generation", "authority_owner"])
def test_closed_receiver_metadata_rechecks_actual_bindings(closed, fault):
    guard = production.bind_generation_guard(closed.deps, closed.runtime, closed.cfg, closed.root)
    assert not asyncio.run(closed.deps.receiver_identity("1" * 64)).admission_open
    if fault == "policy_bytes":
        Path(closed.runtime.config.policy.path).write_bytes(b'{"changed":true}')
    elif fault == "canonical_bytes":
        from test_production_serving import edit_database_copy
        edit_database_copy(closed.deps.canon.path, lambda con: con.execute("INSERT INTO sources VALUES ('VKM-SRC-001')"))
    elif fault == "current_generation":
        changed = closed.manifest.model_copy(update={"code_commit": "9" * 40})
        write(closed.served / (changed.sha256 + ".json"), changed)
        (closed.served / "CURRENT").write_bytes((changed.sha256 + "\n").encode("ascii"))
    else:
        write(closed.served / STATE_FILE, AdmissionState(status="CLOSED", request_key="9" * 64))
    with pytest.raises((ValueError, GenerationUnavailable)):
        asyncio.run(closed.deps.receiver_identity("2" * 64))
    assert guard() == {"status": "UNAVAILABLE"}

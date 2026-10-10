"""Authentication, actual MCP upstream identity and complete response draining."""
import asyncio
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from vkm_corpus.api.app import ApiConfig, create_app
from vkm_corpus.update.barrier import AdmissionBarrierMiddleware, ReceiverBarrier
from vkm_corpus.update.generation import GenerationUnavailable
from vkm_corpus.update.receiver import (RECEIVER_ROUTE, MAX_PROOF_BYTES, ReceiverChallenge,
    ReceiverIdentityProvider, McpReceiverProofMiddleware, SignedReceiverIdentity, SignedMcpReceiverIdentity,
    sign_identity, verify_identity, operator_token)
from vkm_corpus.update.receiver import read_credential_binding
from vkm_corpus.contracts.access import AccessContext
from vkm_evidence.contracts import canonical_bytes, record_hash
from test_core_operator import TOKEN, GATE, generation, signed_proof


def release():
    return SimpleNamespace(runtime_config_sha256="a" * 64, code_sha256="b" * 64,
        dependencies_sha256="c" * 64, access_sha256="d" * 64)


def verify(raw, nonce="1" * 64, **kwargs):
    r = release()
    return verify_identity(raw, token=TOKEN, challenge=ReceiverChallenge(nonce=nonce), manifest=generation("old"),
        runtime_sha256=r.runtime_config_sha256, code_sha256=r.code_sha256,
        dependencies_sha256=r.dependencies_sha256, access_sha256=r.access_sha256, gate=GATE, **kwargs)


@pytest.mark.parametrize("fault", ["nonce", "signature", "code", "runtime", "gate", "generation", "body", "duplicate"])
def test_signed_identity_rejects_replay_tamper_and_different_native_bindings(fault):
    raw = signed_proof(generation("old"), release(), "1" * 64)
    assert verify(raw).instance == "1" * 64
    signed = SignedReceiverIdentity.model_validate_json(raw)
    if fault == "nonce":
        with pytest.raises(ValueError):
            verify(raw, nonce="2" * 64)
        return
    elif fault == "signature":
        signed = signed.model_copy(update={"hmac_sha256": "0" * 64})
    elif fault in {"code", "runtime", "gate", "generation"}:
        key = {"code": "code_sha256", "runtime": "runtime_config_sha256", "gate": "gate_inode",
               "generation": "generation_sha256"}[fault]
        changed = signed.identity.model_copy(update={key: 99 if fault == "gate" else "9" * 64})
        signed = sign_identity(changed, TOKEN)  # even authentic, another binding must fail
    elif fault == "body":
        with pytest.raises(ValueError, match="bound"):
            verify(b" " * (MAX_PROOF_BYTES + 1))
        return
    else:
        raw = b'{"hmac_sha256":"' + b"0" * 64 + b'","hmac_sha256":"' + b"0" * 64 + b'"}'
        with pytest.raises(ValueError, match="duplicate"):
            verify(raw)
        return
    with pytest.raises(ValueError):
        verify(canonical_bytes(signed))


def test_metadata_route_is_separately_authorized_while_all_content_stays_closed(monkeypatch):
    async def run():
        barrier = ReceiverBarrier("test-receiver")
        barrier.pause("synthetic-switch")
        provider = object.__new__(ReceiverIdentityProvider)
        async def prove(_self, nonce):
            return SignedReceiverIdentity.model_validate_json(signed_proof(generation("old"), release(), nonce)).identity
        monkeypatch.setattr(ReceiverIdentityProvider, "__call__", prove)
        deps = SimpleNamespace(receiver_identity=provider, admission_barrier=barrier,
            generation_guard=lambda: {"status": "UNAVAILABLE"}, evidence=None, evidence_publisher=None)
        contexts = {"reader": AccessContext(principal="reader", execution="CLOUD"),
            "other": AccessContext(principal="other", execution="LOCAL")}
        app = create_app(SimpleNamespace(deps=deps), ApiConfig(read_tokens={"read": "reader", "other": "other", "dual": "reader"},
            write_tokens={"write": "writer", "dual": "writer"},
            access_contexts=contexts, deployment_token=TOKEN))
        assert RECEIVER_ROUTE not in app.openapi()["paths"]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for token in (None, "read", "bad"):
                headers = {} if token is None else {"Authorization": "Bearer " + token}
                assert (await client.post(RECEIVER_ROUTE, json={"nonce": "1" * 64}, headers=headers)).status_code == 401
            valid = await client.post(RECEIVER_ROUTE, json={"nonce": "1" * 64}, headers={"Authorization": "Bearer " + TOKEN})
            assert valid.status_code == 200 and verify(valid.content).generation_sha256 == generation("old").sha256
            headers = {"Authorization": "Bearer " + TOKEN, "X-VKM-Read-Authorization": "Bearer read"}
            qualified = await client.post(RECEIVER_ROUTE, json={"nonce": "1" * 64}, headers=headers)
            proof = verify(qualified.content)
            assert proof.read_credential_sha256 == read_credential_binding(TOKEN, "Bearer read")
            assert proof.read_principal_sha256 == record_hash(contexts["reader"])
            other = await client.post(RECEIVER_ROUTE, json={"nonce": "1" * 64},
                headers={**headers, "X-VKM-Read-Authorization": "Bearer other"})
            assert verify(other.content).read_principal_sha256 == record_hash(contexts["other"])
            assert verify(other.content).read_principal_sha256 != proof.read_principal_sha256
            for bad in ("wrong-read-token", "write", "dual"):
                failed = await client.post(RECEIVER_ROUTE, json={"nonce": "1" * 64},
                    headers={**headers, "X-VKM-Read-Authorization": "Bearer " + bad})
                assert failed.status_code == 503
            data = await client.get("/v1/source/VKM-SRC-001", headers={"Authorization": "Bearer read"})
            assert data.status_code == 503 and not barrier.status()["active_requests"]
            duplicate = await client.post(RECEIVER_ROUTE, content='{"nonce":"' + "1" * 64 + '","nonce":"' + "2" * 64 + '"}',
                headers={"Authorization": "Bearer " + TOKEN})
            assert duplicate.status_code == 503 and duplicate.json() == {"status": "UNAVAILABLE"}
            oversized = await client.post(RECEIVER_ROUTE, content=b" " * 1100, headers={"Authorization": "Bearer " + TOKEN})
            assert oversized.status_code == 503
    asyncio.run(run())


def test_operator_secret_is_bounded_separate_and_never_printable_in_config(tmp_path):
    token = tmp_path / "secret"
    token.write_text(TOKEN)
    token.chmod(0o600)
    assert operator_token(token) == TOKEN
    assert TOKEN not in repr(ApiConfig(deployment_token=TOKEN))
    with pytest.raises(ValueError, match="separate"):
        ApiConfig(read_tokens={TOKEN: "read"}, deployment_token=TOKEN)
    token.write_text("too short")
    with pytest.raises(ValueError):
        operator_token(token)


def test_actual_read_mcp_proof_uses_its_own_api_client_and_actual_inventory(tmp_path, monkeypatch):
    import vkm_corpus.update.receiver as module
    from vkm_corpus.mcp.api_client import ApiClient
    from vkm_corpus.mcp.http import McpHttpConfig
    from vkm_corpus.mcp.servers import build_read_server, build_admin_server
    from vkm_corpus.api.production import serving_code_identity, serving_dependencies_identity
    gate = tmp_path / "gate"
    gate.touch()
    info = gate.stat()
    barrier = SimpleNamespace(gate_path=gate, _gate_identity=(info.st_dev, info.st_ino), status=lambda: {"admission_open": False})
    requests = []
    def upstream(request):
        requests.append(request)
        challenge = ReceiverChallenge.model_validate_json(request.content)
        read_header = request.headers.get("X-VKM-Read-Authorization", "")
        if read_header != "Bearer read":
            return httpx.Response(503)
        signed = SignedReceiverIdentity.model_validate_json(signed_proof(generation("old"), release(), challenge.nonce))
        signed = sign_identity(signed.identity.model_copy(update={
            "read_credential_sha256": read_credential_binding(TOKEN, read_header), "read_principal_sha256": "9" * 64}), TOKEN)
        return httpx.Response(200, content=canonical_bytes(signed))
    monkeypatch.setattr(module, "linux_process_identity", lambda: {"process_pid": 7, "process_start_ticks": 100, "pid_namespace_inode": 1234})
    async def run():
        api = ApiClient("http://api:8000", "read", transport=httpx.MockTransport(upstream))
        try:
            cfg = McpHttpConfig(kind="read", api_url="http://api:8000", api_token="read", deployment_token=TOKEN)
            read = build_read_server(api)
            async def unreachable(*_):
                pytest.fail("metadata request entered the corpus MCP transport")
            app = McpReceiverProofMiddleware(unreachable, read, api, cfg, barrier)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mcp") as client:
                assert (await client.post(RECEIVER_ROUTE, json={"nonce": "1" * 64})).status_code == 401
                result = await client.post(RECEIVER_ROUTE, json={"nonce": "1" * 64}, headers={"Authorization": "Bearer " + TOKEN})
                assert result.status_code == 200
                proof = SignedMcpReceiverIdentity.model_validate_json(result.content)
                assert proof.identity.code_sha256 == serving_code_identity()
                assert proof.identity.dependencies_sha256 == serving_dependencies_identity()
                assert proof.identity.api_url == "http://api:8000"
                assert (proof.identity.gate_device, proof.identity.gate_inode) == barrier._gate_identity
                assert not proof.identity.admission_open
                assert requests[-1].url == "http://api:8000" + RECEIVER_ROUTE
                assert requests[-1].headers["authorization"] == "Bearer " + TOKEN
                assert requests[-1].headers["X-VKM-Read-Authorization"] == "Bearer read"
                api._http.headers["Authorization"] = "Bearer wrong-read-token"
                failed_read = await client.post(RECEIVER_ROUTE, json={"nonce": "2" * 64}, headers={"Authorization": "Bearer " + TOKEN})
                assert failed_read.status_code == 503
                api._http.headers["Authorization"] = "Bearer read"
                app.server = build_admin_server(api)
                blocked = await client.post(RECEIVER_ROUTE, json={"nonce": "2" * 64}, headers={"Authorization": "Bearer " + TOKEN})
                assert blocked.status_code == 503
        finally:
            await api.aclose()
    asyncio.run(run())


def test_native_provider_unavailable_and_errors_are_sanitized(monkeypatch):
    from fastapi import FastAPI
    from vkm_corpus.update.receiver import mount_receiver_route
    async def run():
        app = FastAPI()
        mount_receiver_route(app, SimpleNamespace(deps=SimpleNamespace(receiver_identity=lambda _: "private secret")),
            ApiConfig(deployment_token=TOKEN))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            result = await client.post(RECEIVER_ROUTE, json={"nonce": "1" * 64}, headers={"Authorization": "Bearer " + TOKEN})
            assert result.status_code == 503 and result.json() == {"status": "UNAVAILABLE"}
    asyncio.run(run())


@pytest.mark.skipif(os.name != "posix", reason="NOT_RUN: POSIX shared admission gate")
def test_mcp_client_response_holds_native_gate_after_upstream_has_finished(tmp_path):
    import fcntl
    async def run():
        gate = tmp_path / "admission.lock"
        barrier = ReceiverBarrier("read-mcp", gate_path=gate)
        upstream_finished, send_allowed = asyncio.Event(), asyncio.Event()
        async def app(scope, receive, send):
            # API response already arrived; client response has not finished.
            upstream_finished.set()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send_allowed.wait()
            await send({"type": "http.response.body", "body": b"synthetic-old-payload"})
        messages = []
        async def receive():
            return {"type": "http.request", "body": b""}
        async def send(item):
            messages.append(item)
        task = asyncio.create_task(AdmissionBarrierMiddleware(app, provider=lambda: barrier)(
            {"type": "http", "path": "/mcp"}, receive, send))
        await upstream_finished.wait()
        with gate.open("rb") as writer:
            with pytest.raises(BlockingIOError):
                fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
            send_allowed.set()
            await task
            fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(writer, fcntl.LOCK_UN)
        assert messages[-1]["body"] == b"synthetic-old-payload" and not barrier.status()["active_requests"]
    asyncio.run(run())

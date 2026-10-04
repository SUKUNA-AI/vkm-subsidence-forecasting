"""Authenticated, metadata-only proof from the process that serves requests.

The route is intentionally outside the request drain: it returns no corpus
objects, never changes a selector, and requires a separate operator credential.
Health/status and a controller-created sidecar cannot substitute for this proof.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import os
from pathlib import Path
import secrets
import stat
from typing import Literal

from pydantic import Field

from vkm_corpus.update.contracts import GenerationManifest
from vkm_evidence.contracts import Sha256, StrictModel, canonical_bytes, record_hash

RECEIVER_ROUTE = "/v1/internal/deployment/receiver"
MAX_PROOF_BYTES = 256 * 1024


class ReceiverChallenge(StrictModel):
    nonce: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReceiverIdentity(StrictModel):
    schema_version: Literal["vkm-receiver-identity/1"] = "vkm-receiver-identity/1"
    nonce: str = Field(pattern=r"^[0-9a-f]{64}$")
    instance: str = Field(pattern=r"^[0-9a-f]{64}$")
    process_pid: int = Field(gt=0)
    process_start_ticks: int = Field(gt=0)
    pid_namespace_inode: int = Field(gt=0)
    generation_sha256: Sha256
    runtime_config_sha256: Sha256
    code_sha256: Sha256
    dependencies_sha256: Sha256
    access_sha256: Sha256
    components_sha256: Sha256
    services_sha256: Sha256
    gate_device: int = Field(ge=0)
    gate_inode: int = Field(gt=0)
    admission_open: bool
    read_contract_sha256: Sha256
    read_credential_sha256: Sha256 | None = None
    read_principal_sha256: Sha256 | None = None


class SignedReceiverIdentity(StrictModel):
    identity: ReceiverIdentity
    hmac_sha256: Sha256


class McpReceiverIdentity(StrictModel):
    schema_version: Literal["vkm-mcp-receiver-identity/1"] = "vkm-mcp-receiver-identity/1"
    nonce: str = Field(pattern=r"^[0-9a-f]{64}$")
    instance: str = Field(pattern=r"^[0-9a-f]{64}$")
    process_pid: int = Field(gt=0)
    process_start_ticks: int = Field(gt=0)
    pid_namespace_inode: int = Field(gt=0)
    code_sha256: Sha256
    dependencies_sha256: Sha256
    read_contract_sha256: Sha256
    api_url: str
    read_credential_sha256: Sha256
    gate_device: int = Field(ge=0)
    gate_inode: int = Field(gt=0)
    admission_open: bool
    upstream: SignedReceiverIdentity


class SignedMcpReceiverIdentity(StrictModel):
    identity: McpReceiverIdentity
    hmac_sha256: Sha256


def operator_token(path: Path) -> str:
    """No inherited paths, unbounded secrets, or shared API/operator tokens."""
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
        raise ValueError("operator credential must be an ordinary local file")
    info = path.stat()
    if (info.st_size > 512 or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or (os.name == "posix" and info.st_mode & 0o077)):
        raise ValueError("operator credential exceeds bound")
    token = path.read_text(encoding="ascii").strip()
    if not 32 <= len(token) <= 256 or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in token):
        raise ValueError("operator credential has invalid shape")
    return token


def process_start_ticks(raw: str, *, pid: int) -> int:
    # comm can contain spaces and parentheses; starttime is field 22.
    head, separator, tail = raw.rpartition(")")
    if not separator or head.split(" (", 1)[0] != str(pid):
        raise ValueError("native process stat identity differs")
    fields = tail.split()
    if len(fields) < 20 or int(fields[19]) <= 0:
        raise ValueError("native process start identity unavailable")
    return int(fields[19])


def linux_process_identity() -> dict:
    if os.name != "posix" or not Path("/proc/self/stat").is_file():
        raise ValueError("qualified native process identity requires Linux procfs")
    pid = os.getpid()
    raw = Path("/proc/self/stat").read_text(encoding="ascii")
    if len(raw) > 4096:
        raise ValueError("native process stat exceeds bound")
    return {"process_pid": pid, "process_start_ticks": process_start_ticks(raw, pid=pid),
            "pid_namespace_inode": Path("/proc/self/ns/pid").stat().st_ino}


def sign_identity(identity: ReceiverIdentity, token: str) -> SignedReceiverIdentity:
    return SignedReceiverIdentity(identity=identity,
        hmac_sha256=hmac.new(token.encode("ascii"), canonical_bytes(identity), hashlib.sha256).hexdigest())


def sign_mcp_identity(identity: McpReceiverIdentity, token: str) -> SignedMcpReceiverIdentity:
    return SignedMcpReceiverIdentity(identity=identity,
        hmac_sha256=hmac.new(token.encode("ascii"), canonical_bytes(identity), hashlib.sha256).hexdigest())


def read_credential_binding(operator_secret: str, authorization: str) -> str:
    return hmac.new(operator_secret.encode("ascii"),
        b"vkm-read-credential/1\x00" + authorization.encode("utf-8"), hashlib.sha256).hexdigest()


class McpReceiverProofMiddleware:
    """Metadata proof from the actual read server and its actual upstream client.

    It does not expose a new MCP tool or corpus bytes. Legacy/read/admin apps
    without the separate operator credential preserve their previous routes.
    """
    def __init__(self, app, server, api, config, barrier):
        self.app, self.server, self.api, self.config = app, server, api, config
        self.barrier = barrier
        self.pid, self.instance = os.getpid(), secrets.token_hex(32)

    async def _proof(self, nonce):
        from mcp.server.mcpserver import MCPServer
        from vkm_corpus.mcp.api_client import ApiClient
        from vkm_corpus.mcp.servers import READ_NAME
        from vkm_corpus.api.production import read_tool_names, serving_code_identity, serving_dependencies_identity
        if (self.pid != os.getpid() or self.config.kind != "read" or type(self.api) is not ApiClient
                or type(self.server) is not MCPServer or self.server.name != READ_NAME
                or str(self.api._http.base_url).rstrip("/") != self.config.api_url.rstrip("/")):
            raise ValueError("actual read MCP/upstream identity differs")
        names = sorted(t.name for t in await self.server.list_tools())
        if names != sorted(read_tool_names()):
            raise ValueError("actual read MCP contract differs")
        read_header = self.api._http.headers.get("Authorization", "")
        if not read_header.startswith("Bearer "):
            raise ValueError("actual MCP read credential unavailable")
        gate = self.barrier.gate_path.stat(follow_symlinks=False)
        if (gate.st_dev, gate.st_ino) != self.barrier._gate_identity:
            raise ValueError("read MCP shared admission gate changed")
        async with self.api._http.stream("POST", RECEIVER_ROUTE,
                headers={"Authorization": "Bearer " + self.config.deployment_token,
                         "X-VKM-Read-Authorization": read_header},
                json={"nonce": nonce}, timeout=15) as response:
            if response.status_code != 200:
                raise ValueError("actual MCP upstream proof is unavailable")
            if response.headers.get("content-encoding") not in {None, "identity"}:
                raise ValueError("compressed identity proofs are forbidden")
            raw = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                raw.extend(chunk)
                if len(raw) > MAX_PROOF_BYTES:
                    raise ValueError("MCP upstream proof exceeds bound")
        from vkm_corpus.update.operator_units import strict_json
        upstream = SignedReceiverIdentity.model_validate(strict_json(bytes(raw)))
        if (upstream.identity.nonce != nonce or not hmac.compare_digest(upstream.hmac_sha256,
                sign_identity(upstream.identity, self.config.deployment_token).hmac_sha256)):
            raise ValueError("actual MCP upstream proof is unauthenticated")
        read_binding = read_credential_binding(self.config.deployment_token, read_header)
        if (upstream.identity.read_credential_sha256 != read_binding or not upstream.identity.read_principal_sha256
                or self.api._http.headers.get("Authorization", "") != read_header):
            raise ValueError("actual MCP read authorization was not qualified")
        return McpReceiverIdentity(nonce=nonce, instance=self.instance, **linux_process_identity(),
            code_sha256=serving_code_identity(), dependencies_sha256=serving_dependencies_identity(),
            read_contract_sha256=record_hash(names), api_url=self.config.api_url, upstream=upstream,
            read_credential_sha256=read_binding,
            gate_device=gate.st_dev, gate_inode=gate.st_ino,
            admission_open=self.barrier.status()["admission_open"])

    async def __call__(self, scope, receive, send):
        if (scope.get("type") != "http" or scope.get("path") != RECEIVER_ROUTE
                or self.config.deployment_token is None):
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        token = self.config.deployment_token
        status, body = 401, {"status": "UNAUTHORIZED"}
        if hmac.compare_digest(headers.get(b"authorization", b""), ("Bearer " + token).encode("ascii")):
            try:
                if scope.get("method") != "POST" or scope.get("query_string") or b"content-encoding" in headers:
                    raise ValueError("invalid metadata request")
                from vkm_corpus.update.operator_units import strict_json
                async def bounded():
                    raw = bytearray()
                    while True:
                        item = await receive()
                        if item["type"] != "http.request":
                            raise ValueError("disconnected metadata request")
                        raw.extend(item.get("body", b""))
                        if len(raw) > 1024:
                            raise ValueError("metadata request exceeds bound")
                        if not item.get("more_body", False):
                            return ReceiverChallenge.model_validate(strict_json(bytes(raw)))
                challenge = await asyncio.wait_for(bounded(), timeout=5)
                proof = await asyncio.wait_for(self._proof(challenge.nonce), timeout=30)
                status, body = 200, sign_mcp_identity(proof, token).model_dump(mode="json")
            except Exception:
                status, body = 503, {"status": "UNAVAILABLE"}
        await send({"type": "http.response.start", "status": status,
            "headers": [(b"content-type", b"application/json"), (b"connection", b"close")]})
        await send({"type": "http.response.body", "body": canonical_bytes(body)})


def verify_identity(raw: bytes, *, token: str, challenge: ReceiverChallenge,
                    manifest: GenerationManifest, runtime_sha256: str, code_sha256: str,
                    dependencies_sha256: str, access_sha256: str, gate: tuple[int, int],
                    admission_open: bool | None = None) -> ReceiverIdentity:
    if len(raw) > MAX_PROOF_BYTES:
        raise ValueError("receiver proof exceeds bound")
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate receiver proof field")
            value[key] = item
        return value
    proof = SignedReceiverIdentity.model_validate(json.loads(raw, object_pairs_hook=unique))
    wanted = sign_identity(proof.identity, token).hmac_sha256
    if not hmac.compare_digest(proof.hmac_sha256, wanted):
        raise ValueError("receiver proof is not authenticated")
    from vkm_corpus.api.production import read_tool_names
    from vkm_corpus.update.deployment import components_sha256, services_sha256
    item = proof.identity
    if (item.nonce != challenge.nonce or item.generation_sha256 != manifest.sha256
            or item.runtime_config_sha256 != runtime_sha256 or item.code_sha256 != code_sha256
            or item.dependencies_sha256 != dependencies_sha256 or item.access_sha256 != access_sha256
            or item.components_sha256 != components_sha256(manifest)
            or item.services_sha256 != services_sha256(manifest)
            or (item.gate_device, item.gate_inode) != gate
            or item.read_contract_sha256 != record_hash(sorted(read_tool_names()))
            or (admission_open is not None and item.admission_open is not admission_open)):
        raise ValueError("receiver serves another code/config/generation/gate")
    return item


class ReceiverIdentityProvider:
    """Installed by the native startup binder around its actual observers."""
    def __init__(self, deps, runtime, api_config, observer, service_observer=None):
        self.deps, self.runtime, self.api_config = deps, runtime, api_config
        self.observer, self.service_observer = observer, service_observer
        self.pid, self.instance = os.getpid(), secrets.token_hex(32)

    async def __call__(self, nonce: str) -> ReceiverIdentity:
        from vkm_corpus.api.production import (read_tool_names, serving_access_identity,
            serving_code_identity, serving_dependencies_identity)
        from vkm_corpus.update.deployment import components_sha256, services_sha256
        from vkm_corpus.update.generation import GenerationCoordinator
        ReceiverChallenge(nonce=nonce)
        if self.pid != os.getpid() or self.deps.serving_profile != "production":
            raise ValueError("receiver proof is not bound to this qualified process")
        barrier = self.deps.admission_barrier
        if barrier is None or barrier.gate_path is None:
            raise ValueError("receiver shared admission gate is unavailable")
        self.deps.serving_file_lease.check()
        coordinator = GenerationCoordinator(Path(self.runtime.config.runtime_root) / "served")
        manifest = coordinator.manifest()
        observed = await asyncio.to_thread(self.observer)
        services = self.service_observer() if self.service_observer is not None else {}
        if inspect.isawaitable(services):
            services = await services
        coordinator.verify(manifest, observed, services)
        gate = barrier.gate_path.stat(follow_symlinks=False)
        if (gate.st_dev, gate.st_ino) != barrier._gate_identity:
            raise ValueError("receiver admission gate was replaced")
        identity = ReceiverIdentity(nonce=nonce, instance=self.instance,
            **linux_process_identity(),
            generation_sha256=manifest.sha256, runtime_config_sha256=record_hash(self.runtime.config),
            code_sha256=serving_code_identity(), dependencies_sha256=serving_dependencies_identity(),
            access_sha256=serving_access_identity(self.api_config),
            components_sha256=components_sha256(manifest), services_sha256=services_sha256(manifest),
            gate_device=gate.st_dev, gate_inode=gate.st_ino,
            admission_open=barrier.status()["admission_open"],
            read_contract_sha256=record_hash(sorted(read_tool_names())))
        self.deps.serving_file_lease.check()
        if coordinator.manifest().sha256 != manifest.sha256:
            raise ValueError("receiver generation changed during proof")
        if linux_process_identity() != {k: getattr(identity, k) for k in
                ("process_pid", "process_start_ticks", "pid_namespace_inode")}:
            raise ValueError("receiver process changed during proof")
        return identity


def mount_receiver_route(app, service, config):
    from fastapi.responses import JSONResponse
    # Avoid postponed-annotation resolution of locally imported Request.
    from starlette.requests import Request as RequestType

    async def identity(request):
        token = config.deployment_token
        supplied = request.headers.get("authorization", "")
        if token is None:
            return JSONResponse({"status": "NOT_CONFIGURED"}, status_code=404)
        if not hmac.compare_digest(supplied.encode("utf-8"), ("Bearer " + token).encode("ascii")):
            return JSONResponse({"status": "UNAUTHORIZED"}, status_code=401)
        if request.query_params or request.headers.get("content-encoding"):
            return JSONResponse({"status": "INVALID_ARGUMENT"}, status_code=400)
        try:
            async def body():
                raw = bytearray()
                async for chunk in request.stream():
                    raw.extend(chunk)
                    if len(raw) > 1024:
                        raise ValueError("receiver challenge exceeds bound")
                return raw
            raw = await asyncio.wait_for(body(), timeout=5)
            def unique(pairs):
                value = {}
                for key, item in pairs:
                    if key in value:
                        raise ValueError("duplicate challenge field")
                    value[key] = item
                return value
            challenge = ReceiverChallenge.model_validate(json.loads(raw, object_pairs_hook=unique))
            provider = service.deps.receiver_identity
            if type(provider) is not ReceiverIdentityProvider:
                raise ValueError("native receiver identity unavailable")
            proof = await asyncio.wait_for(provider(challenge.nonce), timeout=30)
            if read_authorization := request.headers.get("x-vkm-read-authorization"):
                label = None
                for candidate, candidate_label in config.read_tokens.items():
                    if (candidate not in config.write_tokens and hmac.compare_digest(
                            read_authorization.encode("utf-8"), ("Bearer " + candidate).encode("utf-8"))):
                        label = candidate_label
                if label is None or label not in config.access_contexts:
                    raise ValueError("actual client is not a qualified read principal")
                proof = proof.model_copy(update={
                    "read_credential_sha256": read_credential_binding(token, read_authorization),
                    "read_principal_sha256": record_hash(config.access_contexts[label])})
            signed = sign_identity(proof, token)
            return JSONResponse(signed.model_dump(mode="json"))
        except Exception:
            # No paths, source values, nonce, credential or exception messages.
            return JSONResponse({"status": "UNAVAILABLE"}, status_code=503)
    identity.__annotations__["request"] = RequestType
    app.add_api_route(RECEIVER_ROUTE, identity, methods=["POST"], include_in_schema=False)

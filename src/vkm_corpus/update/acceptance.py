"""Private candidate qualification, independent of production CURRENT/MAINTENANCE.

This module never opens a listening socket or switches a selector. A production
receipt additionally needs an independently registered deployment fault drill.
Operator-owned inputs are trusted configuration, never corpus instructions.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import platform
from typing import Any, Callable, Literal, Protocol

from pydantic import Field, model_validator

from vkm_corpus.api.envelope import ApiResponse, API_VERSION
from vkm_corpus.api.production import (SERVING_CHECKS, read_tool_names, serving_access_identity,
    serving_code_identity, serving_dependencies_identity, _duckdb_file_signature, _qualify_duckdb_file)
from vkm_corpus.update.contracts import ComponentIdentity, GenerationManifest, ServiceIdentity
from vkm_corpus.update.generation import GenerationUnavailable
from vkm_corpus.update.remote_control import ControlSpec
from vkm_evidence.contracts import Identifier, Sha256, StrictModel, canonical_bytes, record_hash


class AcceptanceError(ValueError):
    """No acceptance may be registered for this attempt."""


class CandidatePin(StrictModel):
    code_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    code_tree_sha256: Sha256
    dependencies_sha256: Sha256
    access_config_sha256: Sha256
    policy_sha256: Sha256
    duckdb_file_sha256: Sha256
    nav_file_sha256: Sha256 | None = None
    components: tuple[ComponentIdentity, ...]
    services: tuple[ServiceIdentity, ...] = ()

    @model_validator(mode="after")
    def _coherent(self):
        GenerationManifest(components=self.components, services=self.services, code_commit=self.code_commit,
            policy_sha256=self.policy_sha256, acceptance_sha256="0" * 64)
        if not {"DOCUMENT", "DUCKDB"} <= {c.component for c in self.components}:
            raise ValueError("candidate must bind DOCUMENT and DUCKDB")
        if any(not c.required for c in self.components):
            raise ValueError("acceptance components must all be required")
        if ("NAV" in {c.component for c in self.components}) != (self.nav_file_sha256 is not None):
            raise ValueError("NAV acceptance requires exact packed database bytes")
        return self

    @property
    def component_map(self):
        return {c.component: c.model_dump(mode="json") for c in self.components}

    @property
    def components_sha256(self):
        return record_hash([c.model_dump(mode="json") for c in sorted(self.components, key=lambda c: c.component)])

    @property
    def service_map(self):
        return {s.service: s.model_dump(mode="json") for s in self.services}

    @property
    def services_sha256(self):
        return record_hash([s.model_dump(mode="json") for s in sorted(self.services, key=lambda s: s.service)])

    @property
    def snapshot(self):
        return next(c.revision for c in self.components if c.component == "DOCUMENT")


class ToolProbe(StrictModel):
    arguments: dict[str, Any]
    # A trusted probe inventory supplies an actual expected object. This prevents
    # a generic {ok:true} or empty response from qualifying an unavailable tool.
    expected_object_ids: tuple[str, ...] = ()
    require_image: bool = False

    @model_validator(mode="after")
    def _objects(self):
        if len(set(self.expected_object_ids)) != len(self.expected_object_ids):
            raise ValueError("duplicate expected object")
        if any(not isinstance(v, str) or not v or len(v) > 200 for v in self.expected_object_ids):
            raise ValueError("invalid expected object id")
        return self


class AcceptancePlan(StrictModel):
    schema_version: Literal["vkm-shadow-plan/1"] = "vkm-shadow-plan/1"
    candidate: CandidatePin
    scope: Literal["SYNTHETIC", "SHADOW_PRODUCTION"]
    tools: dict[str, ToolProbe]
    source_id: str = Field(pattern=r"^VKM-SRC-[0-9]{3}$")
    reader_principal: Identifier
    denied_principal: Identifier
    call_timeout_seconds: float = Field(default=30, gt=0, le=600)
    total_timeout_seconds: float = Field(default=900, gt=0, le=7200)
    max_response_bytes: int = Field(default=2_000_000, gt=0, le=16_000_000)
    max_total_response_bytes: int = Field(default=64_000_000, gt=0, le=256_000_000)
    drill_receipt_sha256: Sha256 | None = None
    deployment_profile_sha256: Sha256
    isolation_attestation_sha256: Sha256 | None = None
    control_spec: ControlSpec | None = None

    @model_validator(mode="after")
    def _complete(self):
        if set(self.tools) != read_tool_names():
            raise ValueError("probe inventory must equal the complete installed READ contract")
        if any((name == "get_corpus_status") != (not probe.expected_object_ids)
               for name, probe in self.tools.items()):
            raise ValueError("only corpus status has no object envelope; other probes require actual object ids")
        if self.reader_principal == self.denied_principal:
            raise ValueError("policy challenge needs distinct configured principals")
        if self.scope == "SHADOW_PRODUCTION" and self.isolation_attestation_sha256 is None:
            raise ValueError("production shadow requires pinned isolation attestation")
        if ("CONTROL" in self.candidate.service_map) != (self.control_spec is not None):
            raise ValueError("CONTROL identity needs the exact approved worker/schema qualification spec")
        if len(canonical_bytes(self.model_dump(mode="json")["tools"])) > 1_000_000:
            raise ValueError("probe inventory exceeds bound")
        return self

    @property
    def sha256(self):
        return record_hash(self)


class ProbeTransport(Protocol):
    async def list_tools(self, cursor: str | None = None) -> dict: ...
    async def call_tool(self, name: str, arguments: dict) -> dict: ...
    async def api(self, principal: str | None, method: str, path: str, body: dict | None = None) -> dict: ...
    async def observe_services(self) -> dict: ...


class CandidateFence:
    """Verify the candidate directly; no production selectors or acceptance read."""

    def __init__(self, pin: CandidatePin, *, duckdb_path: Path, policy_path: Path,
                 api_config, observer: Callable[[], dict], production: bool, max_duckdb_bytes: int = 8 * 1024**3,
                 nav_duckdb_path: Path | None = None):
        self.pin, self.duckdb_path, self.policy_path = pin, Path(duckdb_path).absolute(), Path(policy_path).absolute()
        self.api_config, self.observer, self.production = api_config, observer, production
        if isinstance(max_duckdb_bytes, bool) or not isinstance(max_duckdb_bytes, int) or max_duckdb_bytes <= 0:
            raise AcceptanceError("positive native hashing budget required")
        self.max_duckdb_bytes = max_duckdb_bytes
        self.signature = None
        self.nav_path = Path(nav_duckdb_path).absolute() if nav_duckdb_path is not None else None
        if (self.nav_path is not None) != (pin.nav_file_sha256 is not None):
            raise AcceptanceError("actual packed NAV path and its byte pin are required together")
        self.nav_signature = None
        self._watch = None
        self._closed = False

    def check(self, *, full: bool = False):
        from vkm_evidence.cli import _qualification_bytes

        if self.production and platform.system() != "Linux":
            raise AcceptanceError("production shadow filesystem qualification requires Linux")
        if self._closed:
            raise AcceptanceError("candidate qualification lease is closed")
        if self.production and self._watch is None:
            if not full:
                raise AcceptanceError("candidate native file lease is not installed")
            # Stat timestamps can coalesce even on Linux. Capture mutation events
            # before hashing so repeated size/mtime/ctime cannot hide a write.
            from vkm_corpus.update.native_files import NativeFileWatch
            self._watch = NativeFileWatch([self.duckdb_path, self.policy_path,
                                          *([self.nav_path] if self.nav_path is not None else [])])
        if self._watch is not None:
            self._watch.check()
        _qualification_bytes(self.policy_path, self.pin.policy_sha256)
        if serving_access_identity(self.api_config) != self.pin.access_config_sha256:
            raise AcceptanceError("candidate authorization identity changed")
        observed = self.observer()
        if not isinstance(observed, dict) or observed != self.pin.component_map:
            raise AcceptanceError("native candidate identities differ")
        if full:
            if _duckdb_file_signature(self.duckdb_path)[2] > self.max_duckdb_bytes:
                raise AcceptanceError("candidate DuckDB exceeds hashing budget")
            signature, _ = _qualify_duckdb_file(self.duckdb_path, self.pin.duckdb_file_sha256)
            if self.signature is not None and self.signature != signature:
                raise AcceptanceError("candidate DuckDB replaced during probes")
            self.signature = signature
            if self.nav_path is not None:
                if _duckdb_file_signature(self.nav_path)[2] > self.max_duckdb_bytes:
                    raise AcceptanceError("candidate NAV exceeds hashing budget")
                nav_signature, _ = _qualify_duckdb_file(self.nav_path, self.pin.nav_file_sha256)
                if self.nav_signature is not None and nav_signature != self.nav_signature:
                    raise AcceptanceError("candidate NAV replaced during probes")
                self.nav_signature = nav_signature
            if (serving_code_identity() != self.pin.code_tree_sha256 or
                    serving_dependencies_identity() != self.pin.dependencies_sha256):
                raise AcceptanceError("actual serving code or dependencies differ")
            if self.production:
                from vkm_corpus.pipeline.context import code_revision
                commit, dirty = code_revision(strict=True)
                if dirty or commit != self.pin.code_commit:
                    raise AcceptanceError("production shadow requires pinned clean source checkout")
        elif self.signature is None or _duckdb_file_signature(self.duckdb_path) != self.signature:
            raise AcceptanceError("candidate DuckDB identity changed")
        if (self.nav_path is not None and (self.nav_signature is None
                or _duckdb_file_signature(self.nav_path) != self.nav_signature)):
            raise AcceptanceError("candidate NAV identity changed")
        if self._watch is not None:
            self._watch.check()
        return record_hash(observed)

    def close(self):
        self._closed = True
        if self._watch is not None:
            self._watch.close()

    def guard(self):
        try:
            self.check()
            return {"status": "READY", "generation": record_hash(self.pin)}
        except (OSError, ValueError, GenerationUnavailable):
            return {"status": "UNAVAILABLE"}


class PrivateASGIProbeTransport:
    """Actual MCP -> ApiClient -> FastAPI route, entirely in-process.

    Pass a fresh candidate service with explicitly pinned, isolated backends.
    Its native observer is operator code, not a caller-supplied PASS predicate.
    Public production entry points do not use this constructor.
    """

    def __init__(self, service, api_config, fence: CandidateFence, plan: AcceptancePlan, *, synthetic_service_observer=None):
        from vkm_corpus.api.app import create_app
        from vkm_corpus.api.service import ApiService

        if fence.pin != plan.candidate or fence.api_config is not api_config:
            raise AcceptanceError("transport and plan must share exact candidate/config")
        if fence.production != (plan.scope == "SHADOW_PRODUCTION"):
            raise AcceptanceError("transport scope mismatch")
        if Path(service.canon.path).absolute() != fence.duckdb_path:
            raise AcceptanceError("transport does not select the pinned DuckDB")
        policy = service.deps.access_policy
        if policy is None or Path(policy.path).absolute() != fence.policy_path:
            raise AcceptanceError("transport does not select the pinned policy")
        required = {"DOCUMENT", "DUCKDB"}
        for attr, components in {"nav": {"NAV"}, "evidence": {"EVIDENCE"}, "graph": {"GRAPH"},
                                 "search": {"SEARCH"}, "hybrid": {"DENSE", "LATE", "VISUAL"}}.items():
            if getattr(service.deps, attr) is not None:
                required.update(components)
        if not required <= set(plan.candidate.component_map):
            raise AcceptanceError("selected dependency is absent from native candidate")
        service_names = {name for attr, name in (("rerank", "RERANK"), ("control", "CONTROL"), ("hybrid", "RETRIEVAL"))
                         if getattr(service.deps, attr) is not None}
        if service_names != set(plan.candidate.service_map):
            raise AcceptanceError("configured runtime services differ from complete candidate service set")
        if fence.production and synthetic_service_observer is not None:
            raise AcceptanceError("production cannot use a synthetic service observer")
        self._service_observer = synthetic_service_observer
        self._tokens = {}
        for principal in (plan.reader_principal, plan.denied_principal):
            tokens = [t for t, label in api_config.read_tokens.items() if label == principal]
            if not tokens or principal not in api_config.access_contexts:
                raise AcceptanceError("configured read principal unavailable")
            if any(t in api_config.write_tokens for t in tokens):
                raise AcceptanceError("probe read token must not grant writes")
            self._tokens[principal] = tokens[0]
        self.fence, self.plan = fence, plan
        self._service = ApiService(replace(service.deps, generation_guard=fence.guard,
                                           admission_barrier=None, serving_profile="shadow"))
        self._api_app = create_app(self._service, api_config)
        self._app = self._buffered_app
        self._client = self._api = self._http = None

    async def __aenter__(self):
        import httpx
        from mcp import Client
        from vkm_corpus.mcp.api_client import ApiClient
        from vkm_corpus.mcp.servers import build_read_server

        try:
            self.fence.check(full=True)
            if self._service_observer is None and self.plan.candidate.services:
                from vkm_corpus.update.remote_services import NativeServiceObserver
                self._service_observer = await asyncio.wait_for(NativeServiceObserver.bind(
                    self._service.deps, control_spec=self.plan.control_spec), self.plan.call_timeout_seconds)
            await self.observe_services()
            wire = httpx.ASGITransport(app=self._app, raise_app_exceptions=False)
            self._http = httpx.AsyncClient(transport=wire, base_url="http://private-candidate.invalid", trust_env=False)
            self._api = ApiClient("http://private-candidate.invalid", self._tokens[self.plan.reader_principal], transport=wire)
            self._client = Client(build_read_server(self._api))
            await self._client.__aenter__()
        except BaseException:
            try:
                if self._api is not None:
                    await self._api.aclose()
                if self._http is not None:
                    await self._http.aclose()
            finally:
                self.fence.close()
            raise
        return self

    async def observe_services(self):
        value = {} if self._service_observer is None else await asyncio.wait_for(
            self._service_observer.observe(), self.plan.call_timeout_seconds)
        if not isinstance(value, dict):
            raise AcceptanceError("native runtime service observation unavailable")
        observed = {name: identity.model_dump(mode="json") if isinstance(identity, ServiceIdentity) else identity
                    for name, identity in value.items()}
        if observed != self.plan.candidate.service_map:
            raise AcceptanceError("native runtime service identity changed")
        return observed

    async def _buffered_app(self, scope, receive, send):
        if scope["type"] != "http":
            return await self._api_app(scope, receive, send)
        messages, size = [], 0
        async def buffer(message):
            nonlocal size
            size += len(message.get("body", b""))
            if size > self.plan.max_response_bytes or len(messages) >= 4096:
                raise AcceptanceError("private ASGI response exceeds buffer budget")
            messages.append(message)
        try:
            self.fence.check()
            await self.observe_services()
            await self._api_app(scope, receive, buffer)
            await self.observe_services()
            self.fence.check()
        except Exception:
            # No candidate response bytes have been emitted. Denial contains no
            # backend exception text, object identifier, token or old payload.
            raw = canonical_bytes({"ok": False, "meta": {"request_id": "shadow-unavailable", "api_version": API_VERSION},
                "error": {"code": "DEPENDENCY_UNAVAILABLE", "message": "candidate identity unavailable"}})
            await send({"type": "http.response.start", "status": 503,
                        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(raw)).encode("ascii"))]})
            await send({"type": "http.response.body", "body": raw})
            return
        for message in messages:
            await send(message)

    async def __aexit__(self, *exc):
        try:
            await self._client.__aexit__(*exc)
        finally:
            try:
                await self._api.aclose()
                await self._http.aclose()
            finally:
                self.fence.close()

    async def list_tools(self, cursor=None):
        result = await self._client.list_tools(cursor=cursor) if cursor else await self._client.list_tools()
        return {"tools": [{"name": t.name, "input_schema": t.input_schema,
                            "read_only": bool(t.annotations and t.annotations.read_only_hint)} for t in result.tools],
                "next_cursor": getattr(result, "next_cursor", None)}

    async def call_tool(self, name, arguments):
        result = await self._client.call_tool(name, arguments)
        return result.model_dump(mode="json", by_alias=True)

    async def api(self, principal, method, path, body=None):
        headers = {} if principal is None else {"Authorization": "Bearer " + self._tokens[principal]}
        response = await self._http.request(method, path, headers=headers, json=body)
        return {"http_status": response.status_code, "body": response.json()}


DRILL_CHECKS = {"admission_closed", "drained", "partial_apply_failed", "restored_exact_previous", "rollback_admission"}
DRILL_PHASES = ("PREPARED", "ADMISSION_CLOSED", "DRAINED", "APPLYING", "FAULT_INJECTED",
                "ADMISSION_CLOSED", "DRAINED", "RESTORING", "RESTORED", "FAILED_RESTORED")


def validate_drill(raw: bytes, plan: AcceptancePlan):
    if hashlib.sha256(raw).hexdigest() != plan.drill_receipt_sha256:
        raise AcceptanceError("deployment drill receipt bytes differ")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise AcceptanceError("duplicate deployment receipt key")
            result[key] = value
        return result
    proof = json.loads(raw, object_pairs_hook=unique)
    expected = {"schema": "vkm-deployment-drill/1", "scope": plan.scope, "status": "PASS",
        "candidate_components_sha256": plan.candidate.components_sha256,
        "candidate_services_sha256": plan.candidate.services_sha256,
        "code_commit": plan.candidate.code_commit, "policy_sha256": plan.candidate.policy_sha256,
        "code_tree_sha256": plan.candidate.code_tree_sha256,
        "dependencies_sha256": plan.candidate.dependencies_sha256,
        "access_config_sha256": plan.candidate.access_config_sha256,
        "deployment_profile_sha256": plan.deployment_profile_sha256,
        "checks": dict.fromkeys(DRILL_CHECKS, "PASS")}
    if any(proof.get(key) != value for key, value in expected.items()):
        raise AcceptanceError("deployment drill scope/context/checks differ")
    if plan.scope == "SHADOW_PRODUCTION" and proof.get("isolation_attestation_sha256") != plan.isolation_attestation_sha256:
        raise AcceptanceError("deployment isolation attestation differs")
    from pydantic import TypeAdapter
    digest = TypeAdapter(Sha256)
    for key in ("native_before_sha256", "native_after_restore_sha256", "previous_components_sha256", "previous_services_sha256",
                "bindings_before_sha256", "bindings_after_restore_sha256"):
        digest.validate_python(proof.get(key))
    transitions = proof.get("transitions")
    if not isinstance(transitions, list) or any(not isinstance(t, dict) or set(t) != {"phase", "journal_sha256"}
            or not isinstance(t["phase"], str) or not t["phase"] for t in transitions):
        raise AcceptanceError("malformed deployment transitions")
    hashes = [digest.validate_python(t["journal_sha256"]) for t in transitions]
    if proof.get("journal_receipts") != hashes or len(set(hashes)) != len(hashes):
        raise AcceptanceError("deployment transition journal binding differs")
    if (not proof.get("native_before_sha256") or
            proof.get("native_before_sha256") != proof.get("native_after_restore_sha256") or
            proof.get("bindings_before_sha256") != proof.get("bindings_after_restore_sha256") or
            not proof.get("previous_components_sha256") or not proof.get("transitions") or
            not isinstance(proof.get("adapter_ids"), list) or not proof["adapter_ids"] or
            any(not isinstance(v, str) or not v for v in proof["adapter_ids"]) or not proof.get("journal_receipts")):
        raise AcceptanceError("deployment drill lacks native transition/restore evidence")
    return proof


class AcceptanceRegistrar:
    """Operator-owned durable receipt directory and separately approved plan pin.

    Publication is content-addressed. Retrying after a lost ACK must submit the
    exact same bytes; it never reuses a receipt as evidence for a changed plan.
    Directory access/ownership is an operator responsibility, not a signature.
    """

    def __init__(self, root: Path, *, approved_plan_sha256: str):
        from vkm_evidence.cli import _qualification_path
        self.root = _qualification_path(Path(root))
        if not self.root.is_dir():
            raise AcceptanceError("operator receipt directory must already exist")
        self.approved_plan_sha256 = approved_plan_sha256

    def store(self, value: dict) -> str:
        from vkm_evidence.cli import _qualification_write_report
        raw = canonical_bytes(value)
        digest = hashlib.sha256(raw).hexdigest()
        _qualification_write_report(self.root / (digest + ".json"), raw)
        return digest

    def read(self, digest: str) -> bytes:
        from vkm_evidence.cli import _qualification_bytes
        from pydantic import TypeAdapter
        TypeAdapter(Sha256).validate_python(digest)
        return _qualification_bytes(self.root / (digest + ".json"), digest)

    def register(self, report: dict, plan: AcceptancePlan) -> str:
        if self.approved_plan_sha256 != plan.sha256 or report.get("plan_sha256") != plan.sha256:
            raise AcceptanceError("operator-approved plan differs")
        if report.get("status") != "PASS" or report.get("scope") != plan.scope:
            raise AcceptanceError("non-PASS report cannot become serving acceptance")
        bindings = {"schema": "vkm-serving-acceptance/1", **plan.candidate.model_dump(mode="json", exclude={"components", "services"}),
                    "components_sha256": plan.candidate.components_sha256,
                    "services_sha256": plan.candidate.services_sha256,
                    "deployment_drill_sha256": plan.drill_receipt_sha256}
        if set(report) != set(bindings) | {"scope", "plan_sha256", "status", "tools", "checks", "raw_probe_receipts"}:
            raise AcceptanceError("unexpected acceptance report fields")
        if any(report.get(key) != value for key, value in bindings.items()):
            raise AcceptanceError("acceptance report differs from pinned candidate")
        if plan.scope == "SHADOW_PRODUCTION" and platform.system() != "Linux":
            raise AcceptanceError("durable production registration requires Linux")
        self.require_drill(plan)
        if (report.get("checks") != dict.fromkeys(SERVING_CHECKS, "PASS") or
                report.get("tools") != dict.fromkeys(read_tool_names(), "PASS")):
            raise AcceptanceError("incomplete serving contract")
        seen_tools, api_cases, listings = set(), set(), 0
        hashes = report.get("raw_probe_receipts", [])
        if not isinstance(hashes, list) or len(set(hashes)) != len(hashes):
            raise AcceptanceError("duplicate or invalid raw probe receipts")
        for digest in hashes:
            receipt = json.loads(self.read(digest))
            if (receipt.get("schema") != "vkm-shadow-probe/1" or receipt.get("plan_sha256") != plan.sha256
                    or receipt.get("status") != "PASS" or
                    receipt.get("native_components_sha256") != record_hash(plan.candidate.component_map) or
                    receipt.get("native_services_sha256") != record_hash(plan.candidate.service_map)):
                raise AcceptanceError("probe receipt mismatch")
            from pydantic import TypeAdapter
            TypeAdapter(Sha256).validate_python(receipt.get("response_sha256"))
            if not isinstance(receipt.get("response_bytes"), int) or not 0 < receipt["response_bytes"] <= plan.max_response_bytes:
                raise AcceptanceError("invalid raw response budget")
            if receipt.get("kind") == "MCP" and receipt.get("operation") == "tools/list":
                listings += 1
            elif receipt.get("kind") == "MCP":
                name = receipt.get("operation")
                if (name not in plan.tools or name in seen_tools or
                        receipt.get("arguments_sha256") != record_hash(plan.tools[name].arguments)):
                    raise AcceptanceError("tool receipt inventory mismatch")
                seen_tools.add(name)
            elif receipt.get("kind") == "API":
                api_cases.add((receipt.get("operation"), receipt.get("arguments_sha256")))
            else:
                raise AcceptanceError("unknown probe receipt kind")
        wanted_api = {(method + " " + route, record_hash({"principal": principal, "body": body}))
                      for principal, method, route, body, _, _ in _api_probes(plan)}
        if seen_tools != read_tool_names() or api_cases != wanted_api or not 1 <= listings <= 20:
            raise AcceptanceError("probe receipts are incomplete")
        return self.store(report)

    def require_drill(self, plan):
        proof = validate_drill(self.read(plan.drill_receipt_sha256), plan)
        parent, request_key, phases, partial = None, None, [], []
        for transition in proof["transitions"]:
            digest = transition["journal_sha256"]
            event = json.loads(self.read(digest))
            if (event.get("schema") != "vkm-deployment-event/1" or
                    event.get("parent_sha256") != parent or event.get("phase") != transition["phase"]):
                raise AcceptanceError("deployment event chain differs")
            from pydantic import TypeAdapter
            key = TypeAdapter(Sha256).validate_python(event.get("request_key"))
            request_key = key if request_key is None else request_key
            if key != request_key:
                raise AcceptanceError("mixed deployment requests")
            parent = digest
            phases.append(event["phase"])
            if event["phase"] == "FAULT_INJECTED":
                detail = event.get("detail", {})
                native = TypeAdapter(Sha256).validate_python(detail.get("native_partial_sha256"))
                bindings = TypeAdapter(Sha256).validate_python(detail.get("bindings_partial_sha256"))
                partial.append(native != proof["native_before_sha256"] or bindings != proof["bindings_before_sha256"])
        position = -1
        for phase in DRILL_PHASES:
            try:
                position = phases.index(phase, position + 1)
            except ValueError:
                raise AcceptanceError("deployment failure/restore phases are incomplete or reordered") from None
        if phases[-1] != "FAILED_RESTORED" or not partial or not all(partial):
            raise AcceptanceError("deployment drill is not terminal restored")
        return proof


def _positive(body: dict, candidate: CandidatePin, expected_ids: tuple[str, ...]):
    if not isinstance(body, dict) or body.get("ok") is not True:
        raise AcceptanceError("positive response must carry a literal boolean success")
    response = ApiResponse.model_validate(body)
    if (response.ok is not True or response.error is not None or response.meta.warnings or
            response.meta.api_version != API_VERSION or response.meta.canonical_snapshot_id != candidate.snapshot):
        raise AcceptanceError("positive response is incomplete, stale or degraded")
    items = ([response.item] if response.item else []) + (response.items or [])
    if not items or not set(expected_ids) <= {item.envelope.object_id for item in items}:
        raise AcceptanceError("expected objects absent from actual response")
    if any(item.envelope.canonical_snapshot_id not in (None, candidate.snapshot) for item in items):
        raise AcceptanceError("mixed canonical snapshot in response")


def _corpus_status(body: dict, candidate: CandidatePin):
    # /v1/status is intentionally the existing status contract, not ApiResponse's
    # object envelope. Never reinterpret its top-level ok as dependency health.
    if (not isinstance(body, dict) or set(body) != {"ok", "meta", "status"} or body["ok"] is not True
            or body["meta"].get("api_version") != API_VERSION or not body["meta"].get("request_id")
            or body["meta"].get("warnings")):
        raise AcceptanceError("invalid corpus status response")
    status = body["status"]
    controls = status.get("production_controls", {})
    if (status.get("api_version") != API_VERSION or status.get("canonical", {}).get("snapshot_id") != candidate.snapshot
            or controls.get("profile") != "shadow" or controls.get("source_policy") != "ENFORCED"
            or controls.get("generation") != {"status": "READY", "generation": record_hash(candidate)}):
        raise AcceptanceError("corpus status lacks pinned shadow admission")
    def healthy(value):
        if isinstance(value, dict):
            if value.get("available") is False or value.get("error") or value.get("matches_canonical_snapshot") is False:
                raise AcceptanceError("corpus status reports unavailable or inconsistent dependency")
            for key in ("status", "state"):
                if str(value.get(key, "")).upper() in {"FAIL", "NOT_RUN", "SKIP", "UNAVAILABLE", "BLOCKED", "ERROR", "DEGRADED"}:
                    raise AcceptanceError("corpus dependency status is not qualified")
            for nested in value.values():
                healthy(nested)
        elif isinstance(value, list):
            for nested in value:
                healthy(nested)
    healthy(status.get("canonical"))
    healthy(status.get("dependencies"))


def _tool_response(name, probe, answer, candidate):
    if not isinstance(answer, dict) or answer.get("isError") is not False:
        raise AcceptanceError("MCP tool returned an error")
    structured = answer.get("structuredContent")
    if name == "get_corpus_status":
        _corpus_status(structured, candidate)
        return
    wants_image = probe.require_image or name == "get_page_image" or (
        name == "get_figure" and probe.arguments.get("include_image", False))
    if name in {"get_page_image", "get_figure"} and isinstance(structured, dict):
        image_info = structured.get("image")
        if isinstance(image_info, dict) and image_info.get("error"):
            raise AcceptanceError("MCP image endpoint returned an error")
        structured = {k: v for k, v in structured.items() if k != "image"}
    _positive(structured, candidate, probe.expected_object_ids)
    if wants_image:
        import base64
        import io
        from PIL import Image
        images = [c for c in answer.get("content", []) if c.get("type") == "image"]
        if not images:
            raise AcceptanceError("required image unavailable")
        for content in images:
            raw = base64.b64decode(content["data"], validate=True)
            with Image.open(io.BytesIO(raw)) as image:
                if image.format not in {"PNG", "JPEG"} or max(image.size) > 2048:
                    raise AcceptanceError("probe image exceeds contract")
                image.verify()


def _api_probes(plan):
    path = "/v1/source/" + plan.source_id
    return ((None, "GET", path, None, 401, "UNAUTHORIZED"),
        (plan.denied_principal, "GET", path, None, 403, "FORBIDDEN"),
        (plan.reader_principal, "GET", path, None, 200, None),
        (plan.reader_principal, "POST", "/v1/reprocess/source", {"target_id": plan.source_id,
         "reason": "isolated read-token rejection probe"}, 403, "FORBIDDEN"))


async def qualify_shadow(plan: AcceptancePlan, *, transport: ProbeTransport | None,
                         fence: CandidateFence, registrar: AcceptanceRegistrar) -> dict:
    """Execute actual fixed read probes. Failed attempts never publish acceptance.

    Missing transport/drill returns NOT_RUN; transport failures return FAIL.
    Raw payloads/token values are neither logged nor copied into public receipts.
    """
    if registrar.approved_plan_sha256 != plan.sha256 or fence.pin != plan.candidate:
        raise AcceptanceError("candidate/approved plan binding mismatch")
    if plan.scope == "SHADOW_PRODUCTION" and (type(transport) is not PrivateASGIProbeTransport or
            transport.fence is not fence or transport.plan != plan or not fence.production):
        if transport is not None:
            raise AcceptanceError("production requires the real private ASGI/MCP transport")
    base = {"schema": "vkm-serving-acceptance/1", "scope": plan.scope, "plan_sha256": plan.sha256,
            **plan.candidate.model_dump(mode="json", exclude={"components", "services"}),
            "components_sha256": plan.candidate.components_sha256, "services_sha256": plan.candidate.services_sha256}
    tools, checks, receipts = {}, {}, []
    def attempt(status, **detail):
        report = {**base, "status": status, **detail, "tools": tools, "checks": checks,
                  "raw_probe_receipts": receipts}
        return {**report, "attempt_receipt_sha256": registrar.store(report)}
    if transport is None or plan.drill_receipt_sha256 is None:
        return attempt("NOT_RUN", reason="TRANSPORT_OR_DEPLOYMENT_DRILL_UNAVAILABLE")
    try:
        drill_raw = registrar.read(plan.drill_receipt_sha256)
    except FileNotFoundError:
        return attempt("NOT_RUN", reason="DEPLOYMENT_DRILL_NOT_REGISTERED")
    deadline = asyncio.get_running_loop().time() + plan.total_timeout_seconds
    total_bytes = 0

    async def services():
        timeout = min(plan.call_timeout_seconds, deadline - asyncio.get_running_loop().time())
        if timeout <= 0:
            raise TimeoutError("qualification deadline")
        value = await asyncio.wait_for(transport.observe_services(), timeout)
        if value != plan.candidate.service_map:
            raise AcceptanceError("candidate service identity differs")
        return record_hash(value)

    async def request(kind, operation, arguments, call, validate):
        nonlocal total_bytes
        fence.check()
        await services()
        timeout = min(plan.call_timeout_seconds, deadline - asyncio.get_running_loop().time())
        if timeout <= 0:
            raise TimeoutError("qualification deadline")
        answer = await asyncio.wait_for(call(), timeout)
        raw = canonical_bytes(answer)
        total_bytes += len(raw)
        if len(raw) > plan.max_response_bytes or total_bytes > plan.max_total_response_bytes:
            raise AcceptanceError("probe response budget exceeded")
        validate(answer)
        native_services = await services()
        native = fence.check()
        receipt = {"schema": "vkm-shadow-probe/1", "plan_sha256": plan.sha256, "status": "PASS",
            "kind": kind, "operation": operation, "arguments_sha256": record_hash(arguments),
            "response_sha256": hashlib.sha256(raw).hexdigest(), "response_bytes": len(raw),
            "native_components_sha256": native, "native_services_sha256": native_services}
        receipts.append(registrar.store(receipt))
        return answer

    try:
        fence.check(full=True)
        await services()
        # Re-read the same immutable hash while validating every journal link.
        registrar.require_drill(plan)
        checks.update(failed_switch="PASS", rollback="PASS", native_identity="PASS")
        discovered, cursors, cursor = {}, set(), None
        for _ in range(20):
            def validate_listing(answer):
                if not isinstance(answer, dict) or not isinstance(answer.get("tools"), list):
                    raise AcceptanceError("invalid tools/list response")
                for tool in answer["tools"]:
                    if not isinstance(tool, dict):
                        raise AcceptanceError("invalid MCP tool description")
                    name = tool.get("name")
                    if name in discovered or name not in read_tool_names() or tool.get("read_only") is not True:
                        raise AcceptanceError("unexpected, duplicate or non-read-only MCP tool")
                    if not isinstance(tool.get("input_schema"), dict) or tool["input_schema"].get("type") != "object":
                        raise AcceptanceError("MCP input schema unavailable")
                    discovered[name] = tool
            answer = await request("MCP", "tools/list", {"cursor": cursor},
                lambda: transport.list_tools(cursor), validate_listing)
            cursor = answer.get("next_cursor")
            if not cursor:
                break
            if not isinstance(cursor, str) or cursor in cursors:
                raise AcceptanceError("tools/list cursor loop")
            cursors.add(cursor)
        if cursor or set(discovered) != read_tool_names():
            raise AcceptanceError("complete READ tool contract unavailable")
        for name, probe in sorted(plan.tools.items()):
            def validate_tool(answer):
                _tool_response(name, probe, answer, plan.candidate)
            await request("MCP", name, probe.arguments, lambda: transport.call_tool(name, probe.arguments), validate_tool)
            tools[name] = "PASS"
        checks["full_mcp"] = "PASS"
        for principal, method, route, body, status, error in _api_probes(plan):
            def validate_api(answer):
                if answer.get("http_status") != status:
                    raise AcceptanceError("API policy challenge HTTP status differs")
                value = ApiResponse.model_validate(answer.get("body"))
                if error:
                    if (answer["body"].get("ok") is not False or value.ok or not value.error
                            or value.error.code != error or value.item or value.items):
                        raise AcceptanceError("API policy challenge did not deny cleanly")
                else:
                    _positive(answer["body"], plan.candidate, (plan.source_id,))
            await request("API", method + " " + route, {"principal": principal, "body": body},
                lambda: transport.api(principal, method, route, body), validate_api)
        checks.update(policy_enforcement="PASS", generation_consistency="PASS", shadow_acceptance="PASS")
        fence.check(full=True)
        await services()
        report = {**base, "status": "PASS", "tools": tools, "checks": checks,
                  "deployment_drill_sha256": plan.drill_receipt_sha256, "raw_probe_receipts": receipts}
        digest = registrar.register(report, plan)
        return {**report, "receipt_sha256": digest}
    except Exception as exc:
        # Exception messages can contain query/source/token values; persist only type.
        return attempt("FAIL", error_type=type(exc).__name__)

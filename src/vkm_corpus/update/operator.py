"""Closed CORE release factory and CLI; host configuration is operator authority.

The factory constructs actual existing API/native observers and the private MCP
acceptance transport. It cannot load arbitrary Python callbacks, shell commands,
SQL/Cypher, or a model-selected adapter from an incoming campaign.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import inspect
import os
from pathlib import Path
import platform
import threading
from typing import Literal

from pydantic import Field, model_validator

from vkm_corpus.update.acceptance import (AcceptancePlan, AcceptanceRegistrar, CandidateFence,
    PrivateASGIProbeTransport, qualify_shadow)
from vkm_corpus.update.barrier import ReceiverBarrier
from vkm_corpus.update.contracts import GenerationManifest
from vkm_corpus.update.deployment import DeploymentProfile, DurableDeployment, ReceiverControl, SelectorAdapter
from vkm_corpus.update.generation import GenerationUnavailable
from vkm_corpus.update.operator_units import CoreUnitControl, UnitControlConfig, bound_json
from vkm_corpus.update.runtime import BoundFile, RuntimeConfig, observe_components, read_bound
from vkm_evidence.contracts import Sha256, StrictModel, canonical_bytes, record_hash


class OperatorRelease(StrictModel):
    receiver_release_sha256: Sha256
    environment: BoundFile
    runtime: BoundFile
    generation: BoundFile
    acceptance_plan: BoundFile


def host_path(value):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("operator paths must be absolute and normalized without traversal")
    return path.absolute()


def qualified_read_context(config, plan, release):
    """Bind the live MCP principal to the exact context used by acceptance."""
    context = config.access_contexts.get(plan.reader_principal)
    return context is not None and record_hash(context) == release.mcp_read_principal_sha256


class CoreOperatorConfig(StrictModel):
    schema_version: Literal["vkm-core-operator/1"] = "vkm-core-operator/1"
    factory: Literal["CORE_RELEASE_V1"] = "CORE_RELEASE_V1"
    units: UnitControlConfig
    releases: tuple[OperatorRelease, ...] = Field(min_length=1, max_length=16)
    candidate_release_sha256: Sha256
    expected_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    expected_code_sha256: Sha256
    expected_dependencies_sha256: Sha256
    access_config_sha256: Sha256
    policy: BoundFile
    authority_root: str
    protected_roots: tuple[str, ...] = Field(min_length=1)
    qualification_root: str
    isolation_attestation_sha256: Sha256 | None = None
    drain_timeout_seconds: float = Field(default=30, gt=0, le=300)
    max_duckdb_bytes: int = Field(default=8 * 1024**3, gt=0, le=128 * 1024**3)

    @model_validator(mode="after")
    def _complete(self):
        keys = {r.receiver_release_sha256 for r in self.releases}
        if (len(keys) != len(self.releases) or keys != {r.sha256 for r in self.units.releases}
                or self.candidate_release_sha256 not in keys):
            raise ValueError("operator/native receiver release inventories differ")
        if self.units.scope == "SHADOW_PRODUCTION" and self.isolation_attestation_sha256 is None:
            raise ValueError("shadow isolation attestation required")
        if self.units.scope == "SYNTHETIC":
            raise ValueError("the public operator factory cannot qualify synthetic receivers")
        authority, control, qualification = (host_path(p) for p in
            (self.authority_root, self.units.control_root, self.qualification_root))
        roots = tuple(host_path(p) for p in self.protected_roots)
        def overlap(a, b):
            return a.is_relative_to(b) or b.is_relative_to(a)
        if (any(overlap(authority, p) for p in (control, qualification, *roots))
                or any(overlap(write, p) for p in roots for write in (control, qualification))):
            raise ValueError("operator authority and control writes must be disjoint from protected data")
        refs = [self.policy, *(r.compose for r in self.units.releases)]
        for r in self.releases:
            refs.extend((r.environment, r.runtime, r.generation, r.acceptance_plan))
        if (any(not host_path(r.path).is_relative_to(authority) for r in refs)
                or not host_path(self.units.operator_token_file).is_relative_to(authority)):
            raise ValueError("all release control inputs must belong to the independent authority root")
        return self

    @property
    def deployment_profile_sha256(self):
        # Exclude acceptance/generation files: their hashes contain the result
        # of the drill and acceptance. Including them creates a hash cycle.
        body = self.model_dump(mode="json", exclude={"candidate_release_sha256"})
        for release in body["releases"]:
            release.pop("generation")
            release.pop("acceptance_plan")
        return record_hash(body)


class NativePortal:
    """One owned loop for actual async backend clients, also during sync control."""
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="vkm-operator-native", daemon=True)
        self.thread.start()

    def call(self, awaitable, *, timeout=900):
        future = asyncio.run_coroutine_threadsafe(awaitable, self.loop)
        try:
            return future.result(timeout=timeout)
        except BaseException:
            future.cancel()
            raise

    def close(self):
        async def shutdown():
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        try:
            self.call(shutdown(), timeout=10)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=10)
            if not self.thread.is_alive():
                self.loop.close()


@dataclass
class NativeRelease:
    binding: OperatorRelease
    app: object
    runtime: object
    native: object
    plan: AcceptancePlan
    manifest: GenerationManifest

    def components(self):
        deps = self.app.state.service.deps
        observed = observe_components(self.runtime.config.observations)
        observed.update(self.native.observe_components())
        # Check the actual readers, not just a sidecar database path.
        canon = deps.canon.snapshot()
        document = observed["DOCUMENT"]
        if (canon.snapshot_id, canon.manifest_sha256) != (document["revision"], document["manifest_sha256"]):
            raise GenerationUnavailable("operator selected canon reader differs from native source")
        if deps.evidence is not None:
            deps.evidence.journal.commits(deps.evidence.journal.revision)
            if observed.get("EVIDENCE", {}).get("revision") != deps.evidence.journal.revision:
                raise GenerationUnavailable("operator selected evidence reader differs")
        self.native.verify_document(document)
        if set(observed) != {c.component for c in self.manifest.components}:
            raise GenerationUnavailable("operator generation omits a configured component")
        return observed

    async def close(self):
        # Own only clients created by this private factory; close on their loop.
        deps = self.app.state.service.deps
        seen = set()
        for obj in (getattr(deps.search, "_client", None), getattr(deps.graph, "_driver", None),
                    getattr(deps.rerank, "_client", None), getattr(deps.hybrid, "_embed", None),
                    deps.nav, getattr(deps.canon, "_con", None)):
            if obj is None or id(obj) in seen:
                continue
            seen.add(id(obj))
            method = getattr(obj, "aclose", None) or getattr(obj, "close", None)
            if method:
                result = method()
                if inspect.isawaitable(result):
                    await result


def operator_environment(ref: BoundFile) -> dict[str, str]:
    raw = bound_json(ref)
    # These are data for existing load_settings/ApiConfig, never shell env.
    allowed = {"VKM_DATA_ROOT", "VKM_DATA_ROLE", "VKM_WORK", "VKM_SOURCE_POLICY_FILE",
        "VKM_ACCESS_CONTEXT_FILE", "VKM_UPDATE_RUNTIME_FILE", "VKM_DEPLOYMENT_TOKEN_FILE",
        "VKM_NEO4J_URI", "VKM_NEO4J_USER", "VKM_NEO4J_DATABASE", "VKM_NEO4J_PASSWORD_FILE",
        "VKM_OPENSEARCH_URL", "VKM_OPENSEARCH_INDEX_PREFIX", "VKM_RERANK_URL", "VKM_RERANK_TOKEN_FILE",
        "VKM_PG_DSN_FILE", "VKM_API_TOKEN_FILE", "VKM_API_WRITE_TOKEN_FILE", "VKM_EMBED_URL",
        "VKM_EMBED_TOKEN_FILE", "VKM_EVIDENCE_REVIEWERS_FILE", "VKM_API_PROFILE",
        "VKM_HYBRID_LATE_DEFAULT", "VKM_HYBRID_VISUAL_ROUTE", "VKM_HYBRID_VISUAL_SEARCH",
        "VKM_HYBRID_VISUAL_EF_SEARCH", "VKM_HYBRID_GRAPH"}
    if (not isinstance(raw, dict) or not set(raw) <= allowed or raw.get("VKM_API_PROFILE") != "production"
            or raw.get("VKM_DATA_ROLE") != "canonical"
            or any(not isinstance(v, str) or len(v) > 8192 or "\x00" in v for v in raw.values())):
        raise GenerationUnavailable("operator environment contains an unregistered capability")
    return raw


class CoreOperator:
    def __init__(self, config: CoreOperatorConfig, *, config_ref: BoundFile):
        if platform.system() != "Linux":
            raise GenerationUnavailable("CORE operator requires qualified Linux")
        self.config, self.config_ref = config, config_ref
        self.root = Path(config.units.control_root).absolute()
        self.bindings = {r.receiver_release_sha256: r for r in config.releases}
        self._fence_config()
        self.units = CoreUnitControl(config.units)
        self.loaded = {}
        self._drilling = False
        self.barrier = ReceiverBarrier("core-api-and-read-mcp", gate_path=self.root / "admission.lock")
        self.portal = NativePortal()
        try:
            self.controller = DurableDeployment(self.root,
                adapters=(SelectorAdapter("core-receiver-release", self.units.selection_binding,
                    self._apply, self._restore),), receivers=(ReceiverControl(self.barrier, self._rebind),),
                observer=lambda: self._selected().components(),
                service_observer=lambda: self.portal.call(self._selected().native.observe_services()),
                identity_provider=self._identity, qualification=self._qualify,
                candidate_probe=self._probe, drain_timeout_seconds=config.drain_timeout_seconds)
        except BaseException:
            self.portal.close()
            raise

    def _fence_config(self):
        authority = host_path(self.config.authority_root)
        if (not host_path(self.config_ref.path).is_relative_to(authority)
                or any(p.is_symlink() for p in (authority, *authority.parents))):
            raise GenerationUnavailable("operator configuration is outside its authority root")
        if CoreOperatorConfig.model_validate(bound_json(self.config_ref)) != self.config:
            raise GenerationUnavailable("operator authority changed")
        bound_json(self.config.policy)
        if os.name == "posix":
            for path in (authority, Path(self.config_ref.path), Path(self.config.policy.path)):
                if path.stat().st_mode & 0o022:
                    raise GenerationUnavailable("operator authority is writable by other principals")

    async def _load(self, key):
        from vkm_corpus.api.app import build_from_settings
        from vkm_corpus.update.serving import NativeServingBindings
        binding = self.bindings[key]
        env = operator_environment(binding.environment)
        runtime = RuntimeConfig.model_validate(bound_json(binding.runtime))
        if (Path(env.get("VKM_UPDATE_RUNTIME_FILE", "")).absolute() != Path(binding.runtime.path).absolute()
                or Path(env.get("VKM_SOURCE_POLICY_FILE", "")).absolute() != Path(runtime.policy.path).absolute()):
            raise GenerationUnavailable("operator environment selects another runtime/policy")
        release = self.units.releases[key]
        if (host_path(runtime.runtime_root) / "served" != self.root or runtime.policy != self.config.policy
                or record_hash(runtime) != release.runtime_config_sha256
                or any(not any(host_path(p).is_relative_to(host_path(root))
                    for root in self.config.protected_roots)
                    for p in (runtime.originals_root, runtime.canonical_root, runtime.evidence_root) if p)
                or self.units._compose(release)["services"]["api"].get("environment") != env):
            raise GenerationUnavailable("release runtime/roots/environment differ from operator authority")
        app = build_from_settings(environ=env, _defer_generation_binding=True)
        if app.state.update_runtime.config != runtime or runtime.native_serving is None:
            raise GenerationUnavailable("CORE operator requires actual complete native serving")
        plan = AcceptancePlan.model_validate(bound_json(binding.acceptance_plan))
        manifest = GenerationManifest.model_validate(bound_json(binding.generation))
        pin = plan.candidate
        if (manifest.code_commit != pin.code_commit or manifest.policy_sha256 != pin.policy_sha256
                or {c.component: c.model_dump(mode="json") for c in manifest.components} != pin.component_map
                or {s.service: s.model_dump(mode="json") for s in manifest.services} != pin.service_map
                or (key == self.config.candidate_release_sha256 and
                    plan.deployment_profile_sha256 != self.config.deployment_profile_sha256)
                or release.code_sha256 != pin.code_tree_sha256
                or release.dependencies_sha256 != pin.dependencies_sha256
                or release.access_sha256 != pin.access_config_sha256
                or not qualified_read_context(app.state.config, plan, release)
                or plan.scope != "SHADOW_PRODUCTION"):
            raise GenerationUnavailable("approved generation/acceptance/profile differ")
        native = await NativeServingBindings.bind(app.state.service.deps, runtime.native_serving)
        value = NativeRelease(binding, app, app.state.update_runtime, native, plan, manifest)
        self.controller.coordinator.verify(manifest, value.components(), await native.observe_services())
        return value

    def _release(self, key):
        self._fence_config()
        binding = self.bindings[key]
        for ref in (binding.environment, binding.runtime, binding.generation, binding.acceptance_plan):
            bound_json(ref)
        if key not in self.loaded:
            self.loaded[key] = self.portal.call(self._load(key), timeout=300)
        return self.loaded[key]

    def _selected(self):
        return self._release(self.units.selected().sha256)

    def _candidate(self):
        return self._release(self.config.candidate_release_sha256)

    def _identity(self):
        from vkm_corpus.api.production import serving_code_identity, serving_dependencies_identity
        from vkm_corpus.pipeline.context import code_revision
        self._fence_config()
        commit, dirty = code_revision(strict=True)
        if dirty or commit != self.config.expected_commit:
            raise GenerationUnavailable("operator requires exact clean source commit")
        code, dependencies = serving_code_identity(), serving_dependencies_identity()
        if code != self.config.expected_code_sha256 or dependencies != self.config.expected_dependencies_sha256:
            raise GenerationUnavailable("operator code/dependencies differ from its approved control identity")
        return DeploymentProfile(scope=self.config.units.scope, code_commit=commit,
            code_tree_sha256=code, dependencies_sha256=dependencies,
            access_config_sha256=self.config.access_config_sha256,
            policy_sha256=self.config.policy.sha256,
            deployment_profile_sha256=self.config.deployment_profile_sha256,
            receiver_ids=(self.barrier.receiver_id,), adapter_ids=("core-receiver-release",),
            isolation_attestation_sha256=self.config.isolation_attestation_sha256)

    def _qualify(self, manifest):
        candidate = self._candidate()
        if manifest != candidate.manifest:
            raise GenerationUnavailable("generation is outside operator approval")
        profile = self._identity()
        if (candidate.plan.candidate.code_tree_sha256 != profile.code_tree_sha256
                or candidate.plan.candidate.dependencies_sha256 != profile.dependencies_sha256
                or candidate.plan.candidate.access_config_sha256 != profile.access_config_sha256):
            raise GenerationUnavailable("candidate does not match approved control identity")
        candidate.components()
        if self._drilling:
            return  # isolated failure happens before opening any candidate receiver
        from vkm_corpus.api.production import require_serving_acceptance
        require_serving_acceptance(candidate.runtime, manifest, candidate.app.state.config)
        registrar = AcceptanceRegistrar(Path(self.config.qualification_root), approved_plan_sha256=candidate.plan.sha256)
        registrar.register(json.loads(registrar.read(manifest.acceptance_sha256)), candidate.plan)

    async def _accept(self, candidate):
        deps, plan = candidate.app.state.service.deps, candidate.plan
        fence = CandidateFence(plan.candidate, duckdb_path=deps.canon.path,
            policy_path=deps.access_policy.path, api_config=candidate.app.state.config,
            observer=candidate.components, production=True, max_duckdb_bytes=self.config.max_duckdb_bytes,
            nav_duckdb_path=deps.nav._paths()[1] if deps.nav is not None else None)
        registrar = AcceptanceRegistrar(Path(self.config.qualification_root), approved_plan_sha256=plan.sha256)
        try:
            async with PrivateASGIProbeTransport(candidate.app.state.service, candidate.app.state.config, fence, plan) as transport:
                return await qualify_shadow(plan, transport=transport, fence=fence, registrar=registrar)
        finally:
            fence.close()

    def _probe(self, manifest):
        value = self._candidate()
        if manifest != value.manifest or self._drilling:
            raise GenerationUnavailable("isolated fault drill must fail before candidate admission")
        result = self.portal.call(self._accept(value), timeout=value.plan.total_timeout_seconds + 60)
        if result.get("status") != "PASS":
            raise GenerationUnavailable("complete native MCP/API candidate acceptance failed")

    def _apply(self, candidate, request_id):
        self.units.select(self.config.candidate_release_sha256, fence=lambda: self.controller.writer_fence(request_id))

    def _restore(self, binding, request_id):
        self.units.restore(binding, fence=lambda: self.controller.writer_fence(request_id))

    def _rebind(self, manifest):
        selected = self._selected()
        if selected.manifest != manifest:
            raise GenerationUnavailable("receiver selected another approved generation")
        self.units.rebind(manifest, gate=self.barrier._gate_identity)

    def plan(self, request_id):
        previous = self.controller._current()
        if previous is None or previous != self._selected().manifest:
            raise GenerationUnavailable("previous qualified generation must already be registered")
        self.units.prove_current(previous, gate=self.barrier._gate_identity)
        return self.controller.plan(self._candidate().manifest, request_id)

    def accept(self, confirmation):
        self._identity()
        candidate = self._candidate()
        if confirmation != candidate.plan.sha256:
            raise GenerationUnavailable("exact approved shadow plan confirmation required")
        return self.portal.call(self._accept(candidate), timeout=candidate.plan.total_timeout_seconds + 60)

    def switch(self, request_id, confirmation):
        if not self.controller.history(request_id):
            self.plan(request_id)  # independently prove the actual old receivers
        result = self.controller.switch(self._candidate().manifest, request_id, confirmation)
        self.units.prove_current(self.controller._current(), gate=self.barrier._gate_identity)
        return result

    def recovery_plan(self, request_id):
        return self.controller.recovery_plan(request_id, mode="RESTORE_PREVIOUS")

    def recover(self, request_id, confirmation):
        result = self.controller.recover(request_id, confirmation, mode="RESTORE_PREVIOUS")
        self.units.prove_current(self.controller._current(), gate=self.barrier._gate_identity)
        return result

    def drill_plan(self, request_id):
        if self.config.units.scope != "SHADOW_PRODUCTION":
            raise GenerationUnavailable("failure drill is forbidden for live CORE")
        self._drilling = True
        try:
            return self.plan(request_id)
        finally:
            self._drilling = False

    def drill(self, request_id, confirmation):
        if self.config.units.scope != "SHADOW_PRODUCTION":
            raise GenerationUnavailable("failure drill is forbidden for live CORE")
        self._drilling = True
        try:
            plan = self.plan(request_id)
            if confirmation != plan["plan_sha256"]:
                raise GenerationUnavailable("exact isolated drill plan confirmation required")
            proof = self.controller.rehearse_failure(self._candidate().manifest, request_id,
                fail_after_adapter="core-receiver-release")
            registrar = AcceptanceRegistrar(Path(self.config.qualification_root), approved_plan_sha256=self._candidate().plan.sha256)
            for event in self.controller.history(request_id):
                registrar.store({k: v for k, v in event.items() if k != "journal_sha256"})
            return {"status": "PASS", "scope": "SHADOW_PRODUCTION", "drill_receipt_sha256": registrar.store(proof)}
        finally:
            self._drilling = False

    def status(self, request_id=None):
        try:
            selected = self._selected()
            self.units.prove_current(selected.manifest, gate=self.barrier._gate_identity)
            self.controller._verify(selected.manifest)
            state = "READY" if not (self.root / "MAINTENANCE").exists() else "MAINTENANCE"
        except (OSError, ValueError, GenerationUnavailable):
            state = "UNAVAILABLE"
        return {"status": state, "scope": self.config.units.scope,
            "history": self.controller.history(request_id) if request_id else []}

    def close(self):
        try:
            async def close():
                await asyncio.gather(*(r.close() for r in self.loaded.values()), return_exceptions=True)
            self.portal.call(close(), timeout=15)
        finally:
            self.portal.close()


def command(args):
    operator = None
    try:
        ref = BoundFile(path=args.operator_config, sha256=args.config_sha256)
        config = CoreOperatorConfig.model_validate(bound_json(ref))
        operator = CoreOperator(config, config_ref=ref)
        action = args.deployment_command
        if action == "status":
            result = operator.status(args.request_id)
        elif action == "plan":
            result = {"status": "READY", **operator.plan(args.request_id)}
        elif action == "drill-plan":
            result = {"status": "READY", **operator.drill_plan(args.request_id)}
        elif action == "accept":
            result = operator.accept(args.confirm_plan)
        elif action == "switch":
            result = operator.switch(args.request_id, args.confirm_plan)
        elif action == "recovery-plan":
            result = {"status": "READY", **operator.recovery_plan(args.request_id)}
        elif action == "recover":
            result = operator.recover(args.request_id, args.confirm_plan)
        else:
            result = operator.drill(args.request_id, args.confirm_plan)
        # Deployment intent includes local paths in adapter specs? Our unit
        # bindings are hashes only. Do not print approved environments/configs.
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0 if result.get("status") in {"PASS", "READY", "RESTORED"} else 2
    except Exception as exc:
        print(json.dumps({"status": "BLOCKED", "reason": type(exc).__name__}))
        return 2
    finally:
        if operator is not None:
            operator.close()


def register(subparsers):
    parser = subparsers.add_parser("deployment", help="fixed CORE receiver plan/accept/switch/restore")
    sub = parser.add_subparsers(dest="deployment_command", required=True)
    for action in ("status", "plan", "drill-plan", "accept", "switch", "recovery-plan", "recover", "drill"):
        p = sub.add_parser(action)
        p.add_argument("--operator-config", required=True, help="operator-owned local configuration, never a campaign payload")
        p.add_argument("--config-sha256", required=True)
        if action != "accept":
            p.add_argument("--request-id", required=action != "status")
        if action in {"accept", "switch", "recover", "drill"}:
            p.add_argument("--confirm-plan", required=True)
        p.set_defaults(func=command)

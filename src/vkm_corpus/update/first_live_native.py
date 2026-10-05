"""Fixed first-LIVE transaction. Importing this module performs no operations.

The only test seam is explicit SYNTHETIC authority. Production is disabled before
native adapter construction: same-project Compose creation can delete parked
legacy containers by labels, so candidates are created only by the fixed
Engine-create adapter (``engine_create``) and removed only by exact journal-owned
IDs. That adapter has no actual Engine lifecycle qualification yet (Gate L1
NOT_RUN). No runtime preservation claim is made by this code.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import platform
import re
import time

from vkm_corpus.parquet.atomic import write_bytes, write_with, _fsync_dir
from vkm_corpus.update.acceptance import (AcceptanceRegistrar, CandidateFence, PrivateASGIProbeTransport,
    _execute_private_probes, _verify_probe_receipts, read_tool_names)
from vkm_corpus.update.admission import AdmissionState, ReceiverVerification, read_state, require_open
from vkm_corpus.update.barrier import ReceiverBarrier
from vkm_corpus.update.contracts import GenerationManifest
from vkm_corpus.update.deployment import DurableDeployment, DeploymentProfile, ReceiverControl, SelectorAdapter
from vkm_corpus.update.first_live import (FirstLiveError, FirstLiveIntent, LiveAdmissionAuthority,
    FirstLiveReceipt, FirstLiveNotReady, BootQualificationUnavailable, verify_authority, verify_legacy_files)
from vkm_corpus.update.frontdoor import FrontdoorProfile, NativeFrontdoor
from vkm_corpus.update.generation import GenerationUnavailable
from vkm_corpus.update.operator import NativePortal, NativeRelease, operator_environment, qualified_read_context
from vkm_corpus.update.operator_units import CoreUnitControl, bound_json, strict_json, container_fingerprint
from vkm_corpus.update.runtime import BoundFile, RuntimeConfig, read_bound
from vkm_evidence.contracts import canonical_bytes, record_hash


def operator_identity():
    # Intentionally separate from the candidate pin. Private API execution still
    # has to pass its existing full serving_code_identity; no identity bypass.
    from vkm_corpus.api.production import serving_code_identity, serving_dependencies_identity
    return serving_code_identity(), serving_dependencies_identity()


def require_sealed_legacy_ingress(native, sealed_ports):
    """Legacy admin/alternative ports must also fit the exact host seal."""
    host = native.get("HostConfig", {})
    if host.get("NetworkMode") in {"host", "none"}:
        raise FirstLiveError("legacy network mode is outside the sealed bridge path")
    bindings = host.get("PortBindings")
    if not isinstance(bindings, dict) or not bindings:
        raise FirstLiveError("legacy native ingress inventory is unavailable")
    for container, addresses in bindings.items():
        match = re.fullmatch(r"([0-9]{1,5})/tcp", container)
        if (match is None or int(match.group(1)) not in sealed_ports
                or not isinstance(addresses, list) or not addresses):
            raise FirstLiveError("legacy target port or protocol is not sealed")
        for address in addresses:
            if (set(address) != {"HostIp", "HostPort"} or not str(address["HostPort"]).isdigit()
                    or int(address["HostPort"]) not in sealed_ports):
                raise FirstLiveError("legacy published port is not sealed")
    return True


class NativeFirstLiveAdapters:
    def __init__(self, intent: FirstLiveIntent):
        # Do not create clients, threads, paths, firewall rules or containers.
        # The fixed Engine-create adapter is implemented and synthetically tested
        # only; an actual isolated Engine lifecycle qualification (Gate L1) is
        # still required before any native construction is allowed.
        raise FirstLiveNotReady(FirstLiveNotReady.reason)

    def _require_synthetic_mutation_test(self):
        # Only object.__new__ fake-adapter tests can reach these mechanics.
        # The public constructor deliberately accepts no native authority yet.
        if getattr(getattr(self, "intent", None), "scope", None) != "SYNTHETIC":
            raise FirstLiveNotReady(FirstLiveNotReady.reason)

    def _engine(self):
        """The fixed Engine-create adapter; absent means NOT_READY, never Compose."""
        self._require_synthetic_mutation_test()
        engine = getattr(self, "engine", None)
        if engine is None:
            raise FirstLiveNotReady(FirstLiveNotReady.reason)
        return engine

    def _bound_engine(self, *, daemon=True):
        """The adapter must carry exactly this intent's plan, journal, socket and preserved IDs.

        With ``daemon`` the fixed legacy CLI (park/restore) and the Engine adapter must
        also address the same pinned daemon: same socket path and daemon ID from both.
        """
        from vkm_corpus.update.engine_create import engine_journal_path
        engine = self._engine()
        units = self.intent.units
        if (engine.plan != self.intent.engine_create or engine.intent_sha256 != self.intent.sha256
                or engine.preserved != frozenset(self.intent.legacy.original_container_ids.values())
                or engine.socket_path != units.docker_socket
                or engine.journal.path != engine_journal_path(units.control_root, self.intent.sha256)):
            raise FirstLiveError("Engine-create adapter is not bound to this exact intent")
        if not daemon:
            return engine
        cli_daemon = strict_json(self.units.commands.run("docker", ("info", "--format", "{{json .ID}}")))
        if cli_daemon != engine.plan.daemon.daemon_id:
            raise FirstLiveError("legacy CLI and Engine adapter address different daemons")
        return engine

    def _name_occupant(self, name):
        payload = self.units.commands.run("docker", ("container", "ls", "--all", "--filter",
            "name=^/" + name + "$", "--format", "{{json .ID}}", "--no-trunc"))
        return strict_json(payload) if payload.strip() else None

    def fence(self, *, recovery_only=False):
        if operator_identity() != (self.intent.operator_code_sha256, self.intent.operator_dependencies_sha256):
            raise FirstLiveError("actual installed controller code/dependencies differ")
        from vkm_corpus.pipeline.context import code_revision
        commit, dirty = code_revision(strict=True)
        if dirty or commit != self.intent.operator_commit:
            raise FirstLiveError("controller requires the exact clean source revision")
        from vkm_corpus.update.frontdoor import BootSealRegistration, verify_boot_ordering
        registration = BootSealRegistration.model_validate(bound_json(self.intent.boot_registration))
        if (FrontdoorProfile.model_validate(bound_json(registration.profile)) != self.intent.frontdoor
                or registration.code_sha256 != self.intent.operator_code_sha256
                or registration.dependencies_sha256 != self.intent.operator_dependencies_sha256):
            raise FirstLiveError("boot service belongs to another installed controller/seal")
        try:
            verify_boot_ordering(self.intent.boot_registration, self.intent.boot_checkpoint)
        except (OSError, ValueError, RuntimeError) as exc:
            raise BootQualificationUnavailable("BLOCKED_BY_ACTUAL_BOOT_QUALIFICATION") from exc
        verify_legacy_files(self.intent.legacy)
        for ref in self.intent.legacy.selectors:
            read_bound(ref)
        # Receiver-only rollback never deletes or rebuilds these images or
        # shared networks. Availability is checked BEFORE stopping any unit.
        pins = tuple(self.intent.legacy.units.values())
        if not recovery_only:
            pins += tuple(self.intent.candidate_release.units.values())
        for image_id in sorted({p.image_id for p in pins}):
            raw = strict_json(self.units.commands.run("docker", ("image", "inspect", image_id)))
            if not isinstance(raw, list) or len(raw) != 1 or raw[0].get("Id") != image_id:
                raise FirstLiveError("a retained native receiver image is unavailable")
        self._networks()
        self.retained_shared()

    async def _load_retained(self):
        """Use restored control, never candidate G/R or a copied CURRENT.

        This reads stores/identity endpoints only. It does not start query model
        owners or execute inference. Hash I/O has an explicit approved budget.
        """
        from types import SimpleNamespace
        from vkm_corpus.api.app import build_from_settings
        from vkm_corpus.api.production import _qualify_duckdb_file, _native_canon_binding, _native_nav_binding
        from vkm_corpus.update.serving import NativeServingBindings
        observer = self.intent.legacy.observer
        runtime = RuntimeConfig.model_validate(bound_json(observer.runtime))
        env = operator_environment(observer.environment)
        if (runtime.bootstrap_startup is not None or runtime.native_serving != observer.native
                or env.get("VKM_UPDATE_RUNTIME_FILE") != observer.runtime.path):
            raise FirstLiveError("retained native observation control differs")
        if sum(Path(r.path).stat().st_size for r in (observer.duckdb, observer.nav)) > observer.max_local_identity_bytes:
            raise FirstLiveError("retained native file hashing exceeds approved I/O budget")
        app = build_from_settings(environ=env, _defer_generation_binding=True)
        cleanup = NativeRelease(None, app, app.state.update_runtime, None, None, None)
        try:
            deps = app.state.service.deps
            if (app.state.update_runtime.config != runtime or Path(deps.canon.path).absolute() != Path(observer.duckdb.path)
                    or deps.nav is None or Path(deps.nav._paths()[1]).absolute() != Path(observer.nav.path)):
                raise FirstLiveError("retained factory selects another physical reader")
            for ref in (observer.duckdb, observer.nav):
                _, _, lease = await asyncio.to_thread(_qualify_duckdb_file, Path(ref.path), ref.sha256, capture_watch=True)
                self.retained_leases.append(lease)
            _native_canon_binding(deps.canon, Path(observer.duckdb.path))
            _native_nav_binding(deps.nav, Path(observer.duckdb.path), Path(observer.nav.path))
            native = await NativeServingBindings.bind(deps, observer.native, api_service=app.state.service)
            # NativeRelease's existing actual-reader checks are reused, without
            # manufacturing a GenerationManifest or granting serving acceptance.
            return NativeRelease(None, app, app.state.update_runtime, native, None,
                SimpleNamespace(components=observer.components))
        except BaseException:
            await cleanup.close()
            for lease in self.retained_leases:
                lease.close()
            self.retained_leases.clear()
            raise

    def retained_shared(self):
        observer = self.intent.legacy.observer
        if observer is None:
            raise FirstLiveError("independent retained native observer is required")
        if self.retained is None:
            self.retained = self.portal.call(self._load_retained())
        for lease in self.retained_leases:
            lease.check()
        actual = self.retained.components()
        expected = {c.component: c.model_dump(mode="json") for c in observer.components}
        services = self.portal.call(self.retained.native.observe_services())
        if (actual != expected or {k: v.model_dump(mode="json") for k, v in services.items()} !=
                {s.service: s.model_dump(mode="json") for s in observer.services}
                or record_hash([s.model_dump(mode="json") for s in sorted(observer.services, key=lambda s: s.service)])
                    != self.intent.legacy.shared_services_sha256):
            raise FirstLiveError("shared native state changed outside receiver-only rollback scope")
        for lease in self.retained_leases:
            lease.check()
        return record_hash({"components": actual, "services": {k: v.model_dump(mode="json") for k, v in services.items()}})

    def _networks(self):
        from vkm_corpus.update.bootstrap_native import network_fingerprint
        bridges = []
        for pin in self.intent.networks:
            raw = strict_json(self.units.commands.run("docker", ("network", "inspect", pin.name)))
            if (not isinstance(raw, list) or len(raw) != 1 or raw[0].get("Id") != pin.network_id
                    or raw[0].get("Driver") != "bridge" or raw[0].get("Scope") != "local"
                    or raw[0].get("Ingress") is not False
                    or network_fingerprint(raw[0]) != pin.config_sha256):
                raise FirstLiveError("retained native external network differs")
            bridges.append((raw[0].get("Options") or {}).get("com.docker.network.bridge.name") or "br-" + pin.network_id[:12])
        if tuple(sorted(bridges)) != self.intent.frontdoor.trusted_bridges:
            raise FirstLiveError("seal exceptions differ from actual pinned Docker bridge interfaces")

    async def _load(self):
        from vkm_corpus.api.app import build_from_settings
        from vkm_corpus.update.serving import NativeServingBindings
        i = self.intent
        env = operator_environment(i.release.environment)
        runtime = RuntimeConfig.model_validate(bound_json(i.release.runtime))
        if (runtime.bootstrap_startup is not None or runtime.native_serving is None
                or Path(runtime.runtime_root) / "served" != Path(i.units.control_root)
                or record_hash(runtime) != i.candidate_release.runtime_config_sha256
                or runtime.policy != i.live_documents.documents["policy"]
                or i.release.environment != i.live_documents.documents["environment"]
                or i.release.runtime != i.live_documents.documents["runtime"]
                or i.candidate_release.compose != i.live_documents.documents["compose"]
                or runtime.native_serving != i.live_documents.documents["native"]
                or env.get("VKM_UPDATE_RUNTIME_FILE") != i.release.runtime.path
                or env.get("VKM_SOURCE_POLICY_FILE") != runtime.policy.path
                or self.units._compose(i.candidate_release)["services"]["api"].get("environment") != env):
            raise FirstLiveError("actual first-LIVE runtime/compose selects another candidate")
        app = build_from_settings(environ=env, _defer_generation_binding=True)
        from vkm_corpus.update.acceptance import AcceptancePlan
        plan = AcceptancePlan.model_validate(bound_json(i.shadow_plan))
        manifest = GenerationManifest.model_validate(bound_json(i.candidate))
        release = i.candidate_release
        if (app.state.update_runtime.config != runtime or not qualified_read_context(app.state.config, plan, release)
                or (release.code_sha256, release.dependencies_sha256, release.access_sha256) !=
                    (plan.candidate.code_tree_sha256, plan.candidate.dependencies_sha256, plan.candidate.access_config_sha256)):
            raise FirstLiveError("actual live API/read principal differs from shadow candidate")
        native = await NativeServingBindings.bind(app.state.service.deps, runtime.native_serving,
            api_service=app.state.service)
        value = NativeRelease(i.release, app, app.state.update_runtime, native, plan, manifest)
        from vkm_corpus.update.generation import GenerationCoordinator
        GenerationCoordinator.verify(manifest, value.components(), await native.observe_services())
        return value

    def candidate(self):
        if self.loaded is None:
            self.loaded = self.portal.call(self._load())
        return self.loaded

    def shared_services(self):
        services = self.portal.call(self.candidate().native.observe_services())
        expected = self.intent.legacy.shared_services_sha256
        if record_hash([services[k].model_dump(mode="json") for k in sorted(services)]) != expected:
            raise FirstLiveError("shared final EDGE/control/retrieval identities changed")
        return expected

    def _inspect(self, unit, *, stopped=False, reference=None):
        engine = getattr(self, "engine", None)
        if reference is None and engine is not None and unit in ("api", "mcp") and engine.current(unit) is not None:
            # A journal-owned candidate is observed only by its exact ID through
            # the fixed Engine adapter; its pin is the normalized contract hash.
            return engine.observe(unit, running=not stopped)
        reference = reference or "vkm-core-" + unit + "-1"
        raw = strict_json(self.units.commands.run("docker", ("inspect", "--type", "container", reference)))
        if not isinstance(raw, list) or len(raw) != 1:
            raise FirstLiveError("legacy/candidate native receiver inventory unavailable")
        item = raw[0]
        # Same fixed fingerprint algorithm supports the separately retained
        # legacy admin only here; it never extends candidate receiver UNITS.
        if unit == "mcp-admin":
            import copy
            transformed = copy.deepcopy(item)
            if transformed["Config"].get("Labels", {}).get("com.docker.compose.service") != unit:
                raise FirstLiveError("legacy admin unit ownership differs")
            transformed["Config"]["Labels"]["com.docker.compose.service"] = "mcp"
            pin = container_fingerprint(transformed, project="vkm-core", unit="mcp", _stopped_prepare=stopped)
            # Keep the original exact label in the fingerprint input.
            pin["config_sha256"] = record_hash({"legacy_admin_fingerprint": pin["config_sha256"], "unit": unit})
        else:
            pin = container_fingerprint(item, project="vkm-core", unit=unit, _stopped_prepare=stopped)
        if not re.fullmatch(r"[0-9a-f]{64}", item.get("Id", "")):
            raise FirstLiveError("invalid actual receiver container id")
        return item, pin

    def legacy_observe(self, *, original=False):
        self.fence(recovery_only=True)
        result = {}
        for unit, expected in self.intent.legacy.units.items():
            raw, pin = self._inspect(unit)
            if pin != expected.model_dump(mode="json") or raw["Id"] != self.intent.legacy.original_container_ids[unit]:
                raise FirstLiveError("actual retained legacy topology differs")
            require_sealed_legacy_ingress(raw, self.intent.frontdoor.tcp_ports)
            result[unit] = {**pin, "container_id": raw["Id"], "started_at": raw["State"].get("StartedAt")}
        return result

    def _retained_name(self, unit):
        return "vkm-first-live-retained-" + record_hash(self.intent.request_id)[:20] + "-" + unit

    def _legacy_id(self, unit):
        identity = self.intent.legacy.original_container_ids[unit]
        try:
            raw, pin = self._inspect(unit, reference=identity)
        except (ValueError, GenerationUnavailable):
            raw, pin = self._inspect(unit, stopped=True, reference=identity)
        if raw["Id"] != identity or pin != self.intent.legacy.units[unit].model_dump(mode="json"):
            raise FirstLiveError("preserved original legacy container differs or is unavailable")
        return raw

    def _park_legacy(self):
        """Preserve exact original IDs and writable layers; never docker rm them."""
        self._require_synthetic_mutation_test()
        for unit in self.intent.legacy.units:
            original = "vkm-core-" + unit + "-1"
            retained = self._retained_name(unit)
            raw = self._legacy_id(unit)
            if raw.get("Name") not in {"/" + original, "/" + retained}:
                raise FirstLiveError("preserved legacy name is outside this exact request")
            if raw["Name"] == "/" + original:
                occupied = self.units.commands.run("docker", ("container", "ls", "--all", "--filter",
                    "name=^/" + retained + "$", "--format", "{{json .ID}}", "--no-trunc"))
                if occupied.strip():
                    raise FirstLiveError("retained legacy namespace is already occupied")
            if raw["State"]["Running"]:
                self.units.commands.run("docker", ("stop", "--time", "30", raw["Id"]))
            if raw["Name"] == "/" + original:
                self.units.commands.run("docker", ("rename", raw["Id"], retained))
            verified = self._legacy_id(unit)
            if verified["State"]["Running"] or verified.get("Name") != "/" + retained:
                raise FirstLiveError("original legacy was not preserved stopped under its owned name")
            self.event("LEGACY_ORIGINAL_PARKED", unit=unit, container_id=raw["Id"])

    def _remove_owned(self):
        """Remove only exact IDs journaled as created by this intent's attempts.

        Never by name, name prefix or labels: renamed originals keep their old
        Compose labels and are never candidates. Preserved original IDs are refused.
        """
        self._require_synthetic_mutation_test()
        engine = self._bound_engine(daemon=False)
        if not engine.has_effects():
            # Write-ahead journal is empty: no Engine create was ever requested. Daemon
            # identity drift must not strand a pure legacy fallback; the read-only check
            # still refuses a lost journal, and _name_occupant refuses any occupant.
            engine.assert_no_unjournaled_candidate()
            return
        self._bound_engine().remove_owned()

    def _create(self, ref, units):
        # Rename preserves Compose project/service labels, so Compose create/up is
        # never used here. Only the fixed Engine adapter creates exact profiles,
        # without compose labels, and verifies them natively before any start.
        engine = self._bound_engine()
        release = self.intent.candidate_release
        if (ref != release.compose or units != release.units
                or any(engine.plan.profiles[u].image != units[u].image_id for u in ("api", "mcp"))):
            raise FirstLiveError("Engine create request is not the intent's candidate release")
        return {unit: engine.create(unit) for unit in ("api", "mcp")}

    def candidate_start(self, *, partial=False):
        # Bind and check daemon identity, images, networks and mount sources BEFORE
        # any original is stopped or renamed: drift must fail without parking.
        engine = self._bound_engine()
        engine.preflight()
        self.units._compose(self.intent.candidate_release)
        self._park_legacy()
        self._remove_owned()
        ids = self._create(self.intent.candidate_release.compose, self.intent.candidate_release.units)
        engine.start("api")
        if partial:
            for unit in ("api", "mcp"):
                raw, pin = self._inspect(unit, stopped=unit == "mcp")
                if raw["Id"] != ids[unit] or pin != engine.expected_pin(unit):
                    raise FirstLiveError("actual partial native start differs from owned prepared receivers")
            return {"started": {"api": ids["api"]}, "stopped": {"mcp": ids["mcp"]}}
        engine.start("mcp")
        return ids

    def candidate_proof(self, gate, *, admission_open=False):
        self.shared_services()
        deadline = time.monotonic() + self.intent.units.restart_timeout_seconds
        while True:
            try:
                return self.units._joined_proof(self.candidate().manifest, gate=gate,
                    admission_open=admission_open).model_dump(mode="json")
            except (OSError, ValueError, GenerationUnavailable):
                if time.monotonic() >= deadline:
                    raise FirstLiveError("candidate native joined proof deadline exceeded") from None
                time.sleep(.05)

    def legacy_restore(self):
        self._require_synthetic_mutation_test()
        self.fence(recovery_only=True)
        self._remove_owned()
        for unit in self.intent.legacy.units:
            original = "vkm-core-" + unit + "-1"
            raw = self._legacy_id(unit)
            if raw.get("Name") == "/" + self._retained_name(unit):
                if self._name_occupant(original) is not None:
                    # Only journal-owned candidates were removed; anything else at
                    # the original name is neither displaced nor removed.
                    raise FirstLiveError("refusing to displace an unknown native container at the original name")
                if raw["State"]["Running"]:
                    self.units.commands.run("docker", ("stop", "--time", "30", raw["Id"]))
                self.units.commands.run("docker", ("rename", raw["Id"], original))
                raw = self._legacy_id(unit)
            if raw.get("Name") != "/" + original:
                raise FirstLiveError("cannot restore an original from an unregistered namespace")
            if not raw["State"]["Running"]:
                self.units.commands.run("docker", ("start", raw["Id"]))
        return self.legacy_observe()

    async def _private49(self, boundary, store):
        value = self.candidate()
        deps, plan = value.app.state.service.deps, value.plan
        fence = CandidateFence(plan.candidate, duckdb_path=deps.canon.path, policy_path=deps.access_policy.path,
            api_config=value.app.state.config, observer=value.components, production=True,
            nav_duckdb_path=deps.nav._paths()[1] if deps.nav is not None else None)
        tools, checks, receipts = {}, {}, []
        try:
            async with PrivateASGIProbeTransport(value.app.state.service, value.app.state.config, fence, plan) as transport:
                await _execute_private_probes(plan, transport=transport, fence=fence, store=store,
                    tools=tools, checks=checks, receipts=receipts, receipt_schema="vkm-first-live-raw-probe/1",
                    receipt_plan_sha256=self.intent.sha256, boundary_check=boundary)
            return {"tools": tools, "checks": checks, "raw_probe_receipts": receipts}
        finally:
            fence.close()

    def private49(self, boundary, store):
        return self.portal.call(self._private49(boundary, store), timeout=self.candidate().plan.total_timeout_seconds + 60)

    def close(self):
        try:
            if self.loaded is not None:
                self.portal.call(self.loaded.close(), timeout=30)
            if self.retained is not None:
                self.portal.call(self.retained.close(), timeout=30)
        finally:
            for lease in self.retained_leases:
                lease.close()
            if getattr(self, "engine", None) is not None:
                self.engine.close()
            self.portal.close()


class FirstLiveController:
    """Durable one-time state machine; never treats legacy as normal previous."""
    def __init__(self, authority: BoundFile, *, recovery_only=False, _synthetic_adapters=None, _fault=None):
        self.ref = authority
        self.authority, self.intent, self.manifest, self.plan = verify_authority(authority, recovery_only=recovery_only)
        self.synthetic = self.intent.scope == "SYNTHETIC"
        if not self.synthetic:
            raise FirstLiveNotReady(FirstLiveNotReady.reason)
        if (_synthetic_adapters is not None or _fault is not None) and not self.synthetic:
            raise FirstLiveError("production controller cannot inject adapters or faults")
        self.root = Path(self.intent.units.control_root)
        self.key = self.authority.request_key
        self.adapters = _synthetic_adapters or NativeFirstLiveAdapters(self.intent)
        self.adapters.event = self._event
        self.fault = _fault
        self.barrier = ReceiverBarrier("first-live-api-and-mcp",
            gate_path=self.root / "admission.lock" if os.name == "posix" else None, require_durable=True)
        self.journal = DurableDeployment(self.root,
            adapters=(SelectorAdapter("first-live-boundary", lambda: {}, lambda *_: None, lambda *_: None),),
            receivers=(ReceiverControl(self.barrier, lambda _: None),), observer=lambda: {},
            identity_provider=self._profile, qualification=lambda _: None, candidate_probe=lambda _: None)
        self.store = AcceptanceRegistrar(Path(self.intent.qualification_root), approved_plan_sha256=self.intent.sha256)

    def _profile(self):
        return DeploymentProfile(scope="SYNTHETIC" if self.synthetic else "PRODUCTION_SWITCH",
            code_commit=self.intent.operator_commit, code_tree_sha256=self.intent.operator_code_sha256,
            dependencies_sha256=self.intent.operator_dependencies_sha256,
            access_config_sha256=self.intent.candidate_release.access_sha256,
            policy_sha256=self.intent.live_documents.documents["policy"].sha256, deployment_profile_sha256=self.intent.sha256,
            receiver_ids=(self.barrier.receiver_id,), adapter_ids=("first-live-boundary",))

    def _event(self, phase, **detail):
        digest = self.journal._event(self.key, phase, detail=detail)
        if self.fault:
            self.fault(phase)
        return digest

    # Controller-private files; receiver-readable selectors keep the shared modes.
    PRIVATE_FILES = frozenset({"FIRST_LIVE_INTENT.json", "FIRST_LIVE_RESULT"})

    def _write(self, name, data, *, overwrite=False):
        if name not in self.PRIVATE_FILES:
            write_bytes(self.root / "tmp", self.journal._path(name), data, overwrite=overwrite)
            return

        def private(tmp):
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
                         | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.chmod(tmp, 0o600)
        write_with(self.root / "tmp", self.journal._path(name), private, overwrite=overwrite)

    def _admission(self, state):
        self._write("ADMISSION.json", canonical_bytes(state), overwrite=True)

    def _fence(self, *, recovery_only=False):
        actual, intent, _, _ = verify_authority(self.ref, recovery_only=recovery_only)
        if actual != self.authority or intent != self.intent:
            raise FirstLiveError("first-LIVE owner/intent changed")
        self.adapters.fence(recovery_only=recovery_only)
        if self.journal._read("FIRST_LIVE_INTENT.json") != self.intent.model_dump(mode="json"):
            raise FirstLiveError("durable first-LIVE intent changed")
        self.journal._require_gate()
        if self.journal._writer_fd is None:
            raise FirstLiveError("first-LIVE writer absent")
        if not self.synthetic:
            from vkm_corpus.update.bootstrap_probes import require_exclusive_native_lease
            require_exclusive_native_lease(self.journal._writer_fd, self.root / "writer.lock")
            require_exclusive_native_lease(self.journal._gate_fd, self.root / "admission.lock")

    @contextmanager
    def _closed(self, *, recovery=False):
        with self.journal._writer():
            self.adapters.fence(recovery_only=recovery)
            marker = self.root / "FIRST_LIVE_INTENT.json"
            if not marker.exists():
                if recovery or any((self.root / name).exists() for name in ("CURRENT", "ADMISSION.json")):
                    raise FirstLiveError("initialization requires an empty new control boundary")
                self._write("FIRST_LIVE_INTENT.json", canonical_bytes(self.intent))
                self._event("FIRST_LIVE_PREPARED", intent_sha256=self.intent.sha256, authority_sha256=self.ref.sha256)
            elif self.journal._read("FIRST_LIVE_INTENT.json") != self.intent.model_dump(mode="json"):
                raise FirstLiveError("another first-LIVE request owns this boundary")
            if (self.root / "ADMISSION.json").exists() and read_state(self.root).request_key != self.key:
                raise FirstLiveError("another request owns durable admission")
            self.adapters.frontdoor.close()
            self.journal._pause(self.key)
            flag = self.root / "MAINTENANCE"
            if flag.read_text(encoding="ascii") != self.key:
                raise FirstLiveError("first-LIVE maintenance ownership changed")
            flag.unlink()
            _fsync_dir(self.root)
            self._fence(recovery_only=recovery)
            yield

    def _boundary(self):
        self._fence()
        state = read_state(self.root)
        if (state.status != "CLOSED" or state.request_key != self.key
                or (self.root / "MAINTENANCE").exists()
                or self.journal.coordinator.manifest() != self.manifest):
            raise FirstLiveError("first-LIVE closed candidate boundary changed")
        return self.adapters.frontdoor.observe(closed=True)

    def _receipt(self, status, native, seal):
        history = self.journal.history(self.intent.request_id)
        result = FirstLiveReceipt(scope=self.intent.scope, status=status, intent_sha256=self.intent.sha256,
            authority_sha256=self.ref.sha256, candidate_generation_sha256=self.authority.candidate_generation_sha256,
            journal_head_sha256=history[-1]["journal_sha256"], native_sha256=record_hash(native),
            frontdoor_sha256=record_hash(seal))
        sha = self.store.store(result.model_dump(mode="json"))
        self._write("FIRST_LIVE_RESULT", (sha + "\n").encode(), overwrite=True)
        if self.fault:
            self.fault("RECEIPT_PUBLISHED")
        return {**result.model_dump(mode="json"), "receipt_sha256": sha}

    def _initialize(self):
        if self.manifest is None:
            raise FirstLiveError("recovery capability cannot initialize candidate")
        history = self.journal.history(self.intent.request_id)
        if not history:
            raise FirstLiveError("first-LIVE prepared intent missing")
        self._write(self.manifest.sha256 + ".json", canonical_bytes(self.manifest))
        self._write("CURRENT", (self.manifest.sha256 + "\n").encode(), overwrite=True)
        self._write("receiver-release.CURRENT", (self.intent.candidate_release.sha256 + "\n").encode(), overwrite=True)
        self._event("FIRST_CANDIDATE_SELECTED", generation_sha256=self.manifest.sha256)

    def _restore(self):
        if self.journal._gate_fd is None and os.name == "posix":
            self.journal._pause(self.key)
            flag = self.root / "MAINTENANCE"
            if flag.read_text(encoding="ascii") != self.key:
                raise FirstLiveError("restore lost maintenance ownership")
            flag.unlink()
            _fsync_dir(self.root)
        self._fence(recovery_only=True)
        self.adapters.frontdoor.observe(closed=True)
        self._admission(AdmissionState(status="CLOSED", request_key=self.key))
        native = self.adapters.legacy_restore()
        for name in ("CURRENT", "receiver-release.CURRENT"):
            path = self.journal._path(name)
            if path.exists():
                path.unlink()
        _fsync_dir(self.root)
        self._fence(recovery_only=True)
        seal = self.adapters.frontdoor.observe(closed=True)
        self._event("LEGACY_RESTORED_UNQUALIFIED_CLOSED", native_sha256=record_hash(native))
        return self._receipt("LEGACY_RESTORED_UNQUALIFIED_CLOSED", native, seal)

    def rehearse(self, confirmation):
        if confirmation != self.intent.sha256:
            raise FirstLiveError("exact first-LIVE intent confirmation required")
        # Reject an existing live generation before any firewall/container write.
        if (self.root / "CURRENT").exists() or self.journal.history(self.intent.request_id):
            raise FirstLiveError("one-time boundary already initialized; explicit recovery required")
        self.adapters.legacy_observe(original=True)
        with self._closed():
            try:
                self._initialize()
                partial = self.adapters.candidate_start(partial=True)
                if not partial.get("started") or not partial.get("stopped"):
                    raise FirstLiveError("fault drill did not reach a real partial native start")
                self._event("FIRST_LIVE_PARTIAL_START_OBSERVED", native_sha256=record_hash(partial))
            except Exception:
                # Process death is reconciled from the immutable intent, not a
                # forged success. Ordinary exceptions restore under the seal.
                self._restore()
                raise
            result = self._restore()
            self._event("FIRST_LIVE_FALLBACK_DRILL_VERIFIED", restore_receipt_sha256=result["receipt_sha256"])
            return result

    def activate(self, confirmation):
        if confirmation != self.intent.sha256 or self.manifest is None:
            raise FirstLiveError("exact candidate activation authority required")
        history = self.journal.history(self.intent.request_id)
        if (not history or history[0]["detail"].get("intent_sha256") != self.intent.sha256
                or not any(e["phase"] == "FIRST_LIVE_FALLBACK_DRILL_VERIFIED" for e in history)
                or any(e["phase"] == "FIRST_LIVE_ACTIVATED" for e in history)):
            raise FirstLiveError("actual one-time legacy fallback drill is required")
        with self._closed():
            try:
                self._initialize()
                self.adapters.candidate_start()
                gate = self.barrier._gate_identity
                before = self.adapters.candidate_proof(gate)
                probes = self.adapters.private49(self._boundary, self.store.store)
                if (probes.get("tools") != dict.fromkeys(read_tool_names(), "PASS")
                        or probes.get("checks") != dict.fromkeys({"native_identity", "full_mcp", "policy_enforcement", "generation_consistency"}, "PASS")):
                    raise FirstLiveError("first-LIVE private contract incomplete")
                _verify_probe_receipts(probes, self.plan, self.store.read,
                    schema="vkm-first-live-raw-probe/1", plan_sha256=self.intent.sha256)
                self._boundary()
                after = self.adapters.candidate_proof(gate)
                if {k: v for k, v in before.items() if k != "nonce"} != {k: v for k, v in after.items() if k != "nonce"}:
                    raise FirstLiveError("actual candidate process changed during private probes")
                probe_sha = self.store.store({"schema": "vkm-first-live-private49/1", "scope": self.intent.scope,
                    "intent_sha256": self.intent.sha256, "receiver_before": record_hash(before),
                    "receiver_after": record_hash(after), **probes})
                self._event("FIRST_LIVE_PRIVATE49_VERIFIED", receipt_sha256=probe_sha)
                self._boundary()
                proof_sha = self.store.store(after)
                verification = ReceiverVerification(generation_sha256=self.manifest.sha256,
                    native_sha256=record_hash(after), receiver_proofs={"api-and-mcp": proof_sha})
                verified = self._event("RECEIVERS_VERIFIED", **verification.model_dump(mode="json"))
                self._boundary()
                self._admission(AdmissionState(status="OPEN", request_key=self.key,
                    generation_sha256=self.manifest.sha256, verified_event_sha256=verified))
                self._event("FIRST_LIVE_DURABLE_OPEN", verified_event_sha256=verified)
                require_open(self.root)
                # The external packet seal remains CLOSED. Actual receiver
                # metadata can report OPEN only after this exclusive flock is
                # released; retaining it would make the transition impossible.
                self.barrier.resume(self.key)
                if self.journal._gate_fd is not None:
                    os.close(self.journal._gate_fd)
                    self.journal._gate_fd = None
                self._event("FIRST_LIVE_KERNEL_GATE_RELEASED")
                self.adapters.candidate_proof(gate, admission_open=True)
                self._event("FIRST_LIVE_OPEN_RECEIVERS_VERIFIED")
                verify_authority(self.ref)
                self.adapters.fence()
                require_open(self.root)
                self.adapters.frontdoor.observe(closed=True)
                self._event("FIRST_LIVE_OPEN_PREPARED", verified_event_sha256=verified)
                seal = self.adapters.frontdoor.open()
                self._event("FIRST_LIVE_HOST_UNSEALED", frontdoor_sha256=record_hash(seal))
                self._event("FIRST_LIVE_ACTIVATED", private_probe_sha256=probe_sha)
                return self._receipt("FIRST_LIVE_ACTIVATED", after, seal)
            except Exception:
                self.adapters.frontdoor.close()
                self._restore()
                raise

    def recover(self, confirmation):
        if confirmation != self.intent.sha256:
            raise FirstLiveError("exact recovery intent confirmation required")
        # Recovery never needs candidate artifacts. A missing/corrupt candidate
        # is not a reason to abandon the separately retained legacy boundary.
        with self._closed(recovery=True):
            result = self.root / "FIRST_LIVE_RESULT"
            if result.exists():
                from vkm_corpus.update.admission import _ordinary_bytes
                digest = _ordinary_bytes(result, 65).decode("ascii").strip()
                prior = FirstLiveReceipt.model_validate_json(self.store.read(digest))
                if (prior.intent_sha256 != self.intent.sha256 or prior.authority_sha256 != self.ref.sha256):
                    raise FirstLiveError("terminal recovery receipt belongs to another owner")
                if prior.status == "LEGACY_RESTORED_UNQUALIFIED_CLOSED" and not (self.root / "CURRENT").exists():
                    try:
                        native = self.adapters.legacy_observe()
                    except (OSError, ValueError, GenerationUnavailable):
                        native = None
                    if native is not None and record_hash(native) == prior.native_sha256:
                        return {**prior.model_dump(mode="json"), "receipt_sha256": digest}
            return self._restore()

    def resume(self, confirmation):
        """Reconcile a lost activation ACK without recreating healthy receivers.

        If OPEN is not fully evidenced, explicit restore remains the only
        operation. This method never interprets a partial phase as acceptance.
        """
        if confirmation != self.intent.sha256 or self.manifest is None:
            raise FirstLiveError("candidate resume requires the complete original authority")
        with self.journal._writer():
            try:
                verify_authority(self.ref)
                self.adapters.fence()
                state = require_open(self.root)
                if (state.request_key != self.key or state.generation_sha256 != self.manifest.sha256
                        or self.journal.coordinator.manifest() != self.manifest):
                    raise FirstLiveError("durable OPEN belongs to another candidate")
                events = self.journal.history(self.intent.request_id)
                if not events or events[0]["detail"].get("intent_sha256") != self.intent.sha256:
                    raise FirstLiveError("resume lacks the original durable intent")
                verified = [e for e in events if e["journal_sha256"] == state.verified_event_sha256]
                probes = [e for e in events if e["phase"] == "FIRST_LIVE_PRIVATE49_VERIFIED"]
                if (len(verified) != 1 or verified[0]["phase"] != "RECEIVERS_VERIFIED" or len(probes) != 1
                        or not any(e["phase"] == "FIRST_LIVE_FALLBACK_DRILL_VERIFIED" for e in events)):
                    raise FirstLiveError("resume lacks full first-LIVE qualification")
                raw = strict_json(self.store.read(probes[0]["detail"]["receipt_sha256"]))
                if raw.get("scope") != self.intent.scope or raw.get("intent_sha256") != self.intent.sha256:
                    raise FirstLiveError("resume private probes belong to another intent")
                _verify_probe_receipts(raw, self.plan, self.store.read,
                    schema="vkm-first-live-raw-probe/1", plan_sha256=self.intent.sha256)
                detail = ReceiverVerification.model_validate(verified[0]["detail"])
                old = strict_json(self.store.read(detail.receiver_proofs["api-and-mcp"]))
                current = self.adapters.candidate_proof(self.barrier._gate_identity, admission_open=True)
                stable = lambda proof: {k: v for k, v in proof.items() if k not in {"nonce", "admission_open"}}
                if stable(old) != stable(current) or current.get("admission_open") is not True:
                    raise FirstLiveError("actual candidate process changed after qualification")
                from vkm_corpus.update.frontdoor import seal_objects
                actual = self.adapters.frontdoor.inventory()
                if actual == seal_objects(self.intent.frontdoor, closed=True):
                    verify_authority(self.ref)
                    self.adapters.fence()
                    require_open(self.root)
                    seal = self.adapters.frontdoor.open()
                else:
                    seal = self.adapters.frontdoor.observe(closed=False)
                if not any(e["phase"] == "FIRST_LIVE_ACTIVATED" for e in events):
                    self._event("FIRST_LIVE_ACTIVATED", recovered_verified_event_sha256=state.verified_event_sha256)
                return self._receipt("FIRST_LIVE_ACTIVATED", current, seal)
            except Exception:
                # A receiver restarted since qualification is not allowed to
                # continue on an old OPEN authority. Close packet ingress first;
                # this never blesses or automatically recreates that process.
                self.adapters.frontdoor.close()
                state = read_state(self.root)
                if state.request_key == self.key:
                    self._admission(AdmissionState(status="CLOSED", request_key=self.key))
                raise

    def status(self):
        """Operational metadata only, without reading data or starting services."""
        events = self.journal.history(self.intent.request_id)
        state = read_state(self.root) if (self.root / "ADMISSION.json").exists() else None
        return {"schema": "vkm-first-live-status/1", "scope": self.intent.scope,
            "intent_sha256": self.intent.sha256, "last_phase": events[-1]["phase"] if events else "NOT_STARTED",
            "admission": state.status if state else "ABSENT", "qualified_runtime": "NOT_INFERRED_FROM_STATUS",
            "scientific_admission": False, "reboot_safety": "NOT_QUALIFIED",
            "boot_qualification": "NOT_RUN_BY_STATUS", "rollback_scope": "RECEIVER_CONTROL_ONLY_SHARED_STATE_UNCHANGED"}

    def close(self):
        self.adapters.close()


def main(argv=None):
    import argparse
    import json
    parser = argparse.ArgumentParser(description="Fixed first-LIVE authority controller")
    parser.add_argument("action", choices=("rehearse", "activate", "restore", "resume", "status"))
    parser.add_argument("--authority", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--confirm")
    args = parser.parse_args(argv)
    control = None
    try:
        control = FirstLiveController(BoundFile(path=args.authority, sha256=args.sha256),
            recovery_only=args.action in {"restore", "status"})
        result = control.status() if args.action == "status" else getattr(control,
            "recover" if args.action == "restore" else args.action)(args.confirm)
        print(json.dumps(result, sort_keys=True))
        return 0
    except FirstLiveNotReady:
        print(json.dumps({"status": "FIRST_LIVE_NOT_READY", "reason": FirstLiveNotReady.reason}))
        return 2
    except BootQualificationUnavailable:
        print(json.dumps({"status": "BLOCKED_BY_ACTUAL_BOOT_QUALIFICATION"}))
        return 2
    except (OSError, ValueError, RuntimeError):
        # No exception text: Docker/credential/capsule paths are not public logs.
        print(json.dumps({"status": "BLOCKED", "error": "FIRST_LIVE_AUTHORITY_OR_NATIVE_GATE_FAILED"}))
        return 2
    finally:
        if control is not None:
            control.close()


if __name__ == "__main__":
    raise SystemExit(main())

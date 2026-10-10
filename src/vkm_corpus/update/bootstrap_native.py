"""Fixed isolated cold bootstrap; legacy CORE is inspected, never controlled.

The only managed objects are the two labelled shadow receivers. Networks are
pre-existing, independently pinned and external. No build/pull/down/prune or
legacy restart command can be produced by this factory.
"""
from __future__ import annotations

from pathlib import Path
import hashlib
import json
import os
import platform
import time
from typing import Literal

from pydantic import Field, model_validator

from vkm_corpus.parquet.atomic import write_bytes, _fsync_dir
from vkm_corpus.update.admission import AdmissionState, read_state
from vkm_corpus.update.bootstrap import (BootstrapIntent, BootstrapProbePlan, BootstrapStartupAuthority,
    BootstrapPreparation, BootstrapBoundaryReceipt, BaselineQualification, BaselineRegistration)
from vkm_corpus.update.barrier import ReceiverBarrier
from vkm_corpus.update.contracts import GenerationManifest
from vkm_corpus.update.deployment import DurableDeployment, DeploymentProfile, ReceiverControl, SelectorAdapter
from vkm_corpus.update.generation import GenerationUnavailable
from vkm_corpus.update.operator import NativePortal, NativeRelease, operator_environment
from vkm_corpus.update.operator_units import (CoreUnitControl, UnitControlConfig, ComposeRelease, UnitPin,
    FixedDockerCommands, bound_json, strict_json, container_fingerprint)
from vkm_corpus.update.runtime import BoundFile, RuntimeConfig
from vkm_evidence.contracts import Sha256, StrictModel, record_hash, canonical_bytes


class BootstrapControlConfig(StrictModel):
    schema_version: Literal["vkm-bootstrap-control/1"] = "vkm-bootstrap-control/1"
    factory: Literal["ISOLATED_EMPTY_BASELINE_V1"] = "ISOLATED_EMPTY_BASELINE_V1"
    intent: BoundFile
    probe_spec: BoundFile
    docker: BoundFile
    compose_binary: BoundFile
    docker_socket: str
    control_root: str
    authority_root: str
    qualification_root: str
    protected_roots: tuple[str, ...] = Field(min_length=1)
    receiver_url: str
    mcp_receiver_url: str
    operator_token_file: str
    api_container_port: int = Field(default=8000, ge=1, le=65535)
    mcp_container_port: int = Field(default=8765, ge=1, le=65535)
    command_timeout_seconds: float = Field(default=90, gt=0, le=300)
    restart_timeout_seconds: float = Field(default=120, gt=0, le=600)
    drain_timeout_seconds: float = Field(default=30, gt=0, le=300)
    max_duckdb_bytes: int = Field(default=8 * 1024**3, gt=0, le=128 * 1024**3)

    @model_validator(mode="after")
    def _roots(self):
        roots = [Path(p) for p in (self.control_root, self.authority_root,
            self.qualification_root, self.docker_socket, self.operator_token_file, *self.protected_roots)]
        if any(not p.is_absolute() or ".." in p.parts for p in roots):
            raise ValueError("bootstrap needs normalized absolute host-local roots")
        authority, control, qualification = map(Path, (self.authority_root, self.control_root, self.qualification_root))
        overlap = lambda a, b: a.is_relative_to(b) or b.is_relative_to(a)
        if (overlap(authority, control) or overlap(authority, qualification) or overlap(control, qualification)
                or any(overlap(write, Path(read)) for write in (authority, control, qualification)
                       for read in self.protected_roots)):
            raise ValueError("bootstrap authority/control/receipts must be disjoint from original data")
        if not all(Path(ref.path).is_relative_to(authority) for ref in (self.intent, self.probe_spec)):
            raise ValueError("bootstrap intent/probes must be independently operator-owned")
        return self

    @property
    def profile_sha256(self):
        # One-directional DAG: the approved intent names this control recipe;
        # its future hashes cannot also be part of that recipe's identity.
        return record_hash(self.model_dump(mode="json", exclude={"intent", "probe_spec"}))


def network_fingerprint(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("Id"), str):
        raise GenerationUnavailable("native network inventory unavailable")
    fields = ("Name", "Driver", "Scope", "Internal", "Attachable", "Ingress", "EnableIPv6",
              "IPAM", "Options", "Labels", "ConfigOnly", "ConfigFrom")
    return record_hash({k: raw.get(k) for k in fields})


class IsolatedBootstrap:
    def __init__(self, config: BootstrapControlConfig, *, config_ref: BoundFile,
                 _commands=None, _http=None, _process_probe=None, _fault=None, _recovery_only=False):
        # Test injection is restricted to explicit SYNTHETIC intent below.
        self.config, self.config_ref, self.recovery_only = config, config_ref, _recovery_only
        if not Path(config_ref.path).is_relative_to(Path(config.authority_root)):
            raise GenerationUnavailable("bootstrap control must belong to the operator authority")
        self.root = Path(config.control_root).absolute()
        self.intent = BootstrapIntent.model_validate(bound_json(config.intent))
        if _recovery_only:
            if self.intent.retained_startup is None:
                raise GenerationUnavailable("recovery requires the independently retained startup identity")
            self.authority = self.intent.retained_startup
        else:
            self.authority = BootstrapStartupAuthority.model_validate(bound_json(self.intent.startup_authority))
        if (self.authority.sha256 != self.intent.startup_authority.sha256
                or self.authority.request_id != self.intent.request_id or self.authority.bootstrap_id != self.intent.bootstrap_id
                or self.authority.legacy_topology_sha256 != self.intent.legacy.sha256
                or self.authority.independent_backup_sha256 != self.intent.independent_backup.sha256
                or self.intent.scope != "SYNTHETIC" and self.intent.retained_startup != self.authority):
            raise GenerationUnavailable("retained bootstrap identity differs from approved intent")
        from vkm_corpus.update.acceptance import AcceptancePlan
        self.probes = None if _recovery_only else AcceptancePlan.model_validate(bound_json(config.probe_spec))
        self.plan = None if _recovery_only else BootstrapProbePlan(scope=self.intent.scope, intent_sha256=self.intent.sha256,
            startup_authority_sha256=self.authority.sha256,
            legacy_topology_sha256=self.intent.legacy.sha256,
            independent_backup_sha256=self.intent.independent_backup.sha256, probes=self.probes)
        if self.plan is not None:
            self.intent.require_plan(self.plan, self.authority)
        if (config.profile_sha256 != self.intent.deployment_profile_sha256
                or Path(self.authority.control_root).absolute() != self.root
                or any(x is not None for x in (_commands, _http, _process_probe, _fault))
                   and self.intent.scope != "SYNTHETIC"):
            raise GenerationUnavailable("bootstrap control/injection differs from approved scope")
        if self.intent.scope != "SYNTHETIC" and platform.system() != "Linux":
            raise GenerationUnavailable("native cold bootstrap requires Linux")
        if not self.root.is_dir():
            raise GenerationUnavailable("independently prepared control root is required")
        self._fault_hook, self._http, self._process_probe = _fault, _http, _process_probe
        self._unit_kwargs = {k: v for k, v in {"http": _http, "process_probe": _process_probe}.items() if v is not None}
        # Shape-only prototype: the zero config pins are NEVER used to inspect
        # or qualify a receiver. Actual pins are captured after CLOSED create.
        self.prototype = ComposeRelease(compose=self.intent.recipe.compose, project="vkm-core-shadow",
            units={u: UnitPin(image_id=image, config_sha256="0" * 64)
                   for u, image in self.intent.recipe.images.items()},
            runtime_config_sha256="0" * 64 if _recovery_only else record_hash(RuntimeConfig.model_validate(bound_json(self.intent.runtime))),
            code_sha256=self.authority.candidate.code_tree_sha256,
            dependencies_sha256=self.authority.candidate.dependencies_sha256,
            access_sha256=self.authority.candidate.access_config_sha256,
            mcp_read_principal_sha256="0" * 64 if _recovery_only else self._principal_hash())
        self.unit_config = self._unit_config(self.prototype)
        self.commands = _commands or FixedDockerCommands(self.unit_config)
        self.template = CoreUnitControl(self.unit_config, commands=self.commands, **self._unit_kwargs)
        # Validate all approved inputs before bind_gate can create a lock or a
        # private portal starts its thread. The mutation path rechecks them.
        self._fence(fallback_only=_recovery_only)
        self.barrier = ReceiverBarrier("bootstrap-shadow", gate_path=self.root / "admission.lock", require_durable=True)
        self.portal, self.loaded = NativePortal(), None
        def no_switch(*args):
            raise GenerationUnavailable("ordinary switch forbidden in bootstrap")
        self.controller = DurableDeployment(self.root,
            adapters=(SelectorAdapter("bootstrap-empty-shadow", lambda: {}, no_switch, no_switch),),
            receivers=(ReceiverControl(self.barrier, lambda manifest: None),),
            observer=lambda: {}, identity_provider=self._profile,
            qualification=lambda manifest: (_ for _ in ()).throw(GenerationUnavailable("ordinary switch forbidden in bootstrap")),
            candidate_probe=lambda manifest: None, drain_timeout_seconds=config.drain_timeout_seconds)
        self.manifest = GenerationManifest(components=self.authority.candidate.components,
            services=self.authority.candidate.services, code_commit=self.authority.candidate.code_commit,
            policy_sha256=self.authority.candidate.policy_sha256, acceptance_sha256=self.authority.sha256)
        # Bootstrap has its own approved intent, not an ordinary deployment
        # plan with an already-qualified previous generation.
        self.controller.writer_fence = self._writer_fence
        try:
            self._fence(fallback_only=_recovery_only)
        except BaseException:
            self.close()
            raise

    def _writer_fence(self, request_id, *, maintenance=True):
        if request_id != self.intent.request_id or self.controller._writer_fd is None:
            raise GenerationUnavailable("bootstrap requires its live exclusive writer")
        self._profile()
        self.controller._require_gate()
        if self.controller._lock_watch is not None:
            self.controller._lock_watch.check()
        fd = self.controller._writer_fd
        got, current = os.fstat(fd), (self.root / "writer.lock").stat(follow_symlinks=False)
        if (got.st_dev, got.st_ino) != (current.st_dev, current.st_ino):
            raise GenerationUnavailable("bootstrap writer was replaced")
        if self.intent.scope != "SYNTHETIC":
            from vkm_corpus.update.bootstrap_probes import require_exclusive_native_lease
            require_exclusive_native_lease(fd, self.root / "writer.lock")
            require_exclusive_native_lease(self.controller._gate_fd, self.root / "admission.lock")
        state = read_state(self.root)
        flag = self.root / "MAINTENANCE"
        if (state.status != "CLOSED" or state.request_key != self.authority.request_key
                or flag.is_symlink() or (maintenance and flag.read_bytes() != self.authority.request_key.encode("ascii"))
                or (not maintenance and flag.exists())):
            raise GenerationUnavailable("bootstrap closure changed")

    def _principal_hash(self):
        from vkm_corpus.api.app import ApiConfig
        from vkm_corpus.config import load_settings
        env = operator_environment(self.intent.environment)
        context = ApiConfig.from_settings(load_settings(env), environ=env).access_contexts.get(self.probes.reader_principal)
        if context is None:
            raise GenerationUnavailable("bootstrap READ principal is unavailable")
        return record_hash(context)

    def _unit_config(self, release):
        c = self.config
        return UnitControlConfig(scope="SHADOW_PRODUCTION" if self.intent.scope != "SYNTHETIC" else "SYNTHETIC",
            docker=c.docker, compose_binary=c.compose_binary, docker_socket=c.docker_socket,
            control_root=c.control_root, receiver_url=c.receiver_url, mcp_receiver_url=c.mcp_receiver_url,
            operator_token_file=c.operator_token_file, api_container_port=c.api_container_port,
            mcp_container_port=c.mcp_container_port, command_timeout_seconds=c.command_timeout_seconds,
            restart_timeout_seconds=c.restart_timeout_seconds, releases=(release,))

    def _profile(self):
        self._fence(fallback_only=self.recovery_only)
        pin = self.authority.candidate
        if self.intent.scope != "SYNTHETIC":
            from vkm_corpus.api.production import serving_code_identity, serving_dependencies_identity
            from vkm_corpus.pipeline.context import code_revision
            commit, dirty = code_revision(strict=True)
            if (dirty or commit != pin.code_commit or serving_code_identity() != pin.code_tree_sha256
                    or serving_dependencies_identity() != pin.dependencies_sha256):
                raise GenerationUnavailable("bootstrap requires exact clean native source/dependencies")
        return DeploymentProfile(scope="SYNTHETIC" if self.intent.scope == "SYNTHETIC" else "SHADOW_PRODUCTION",
            code_commit=pin.code_commit, code_tree_sha256=pin.code_tree_sha256,
            dependencies_sha256=pin.dependencies_sha256, access_config_sha256=pin.access_config_sha256,
            policy_sha256=pin.policy_sha256, deployment_profile_sha256=self.config.profile_sha256,
            receiver_ids=(self.barrier.receiver_id,), adapter_ids=("bootstrap-empty-shadow",),
            isolation_attestation_sha256=self.authority.isolation_attestation_sha256)

    def _fence(self, *, fallback_only=False):
        if BootstrapControlConfig.model_validate(bound_json(self.config_ref)) != self.config:
            raise GenerationUnavailable("bootstrap control authority changed")
        if BootstrapIntent.model_validate(bound_json(self.config.intent)) != self.intent:
            raise GenerationUnavailable("approved bootstrap intent changed")
        refs = [self.intent.legacy.compose, *self.intent.legacy.selectors, self.intent.independent_backup]
        if not fallback_only:
            refs += [self.intent.startup_authority, self.intent.environment, self.intent.runtime,
                     self.intent.recipe.compose, self.config.probe_spec]
        for ref in refs:
            # Binary original/model data are not bootstrap control inputs.
            path = Path(ref.path).absolute()
            if any(p.is_symlink() for p in (path, *path.parents)) or path.stat().st_size > 2 * 1024 * 1024:
                raise GenerationUnavailable("bootstrap control input is indirect or oversized")
            from vkm_corpus.update.runtime import read_bound
            read_bound(ref)
        from vkm_corpus.update.bootstrap import require_independent_bootstrap_backup
        require_independent_bootstrap_backup(self.intent)

    def _fault(self, phase):
        if self._fault_hook:
            self._fault_hook(phase)

    def _event(self, phase, **detail):
        return self.controller._event(self.authority.request_key, phase, detail=detail)

    def _store(self, value):
        from vkm_corpus.update.acceptance import AcceptanceRegistrar
        return AcceptanceRegistrar(Path(self.config.qualification_root),
            approved_plan_sha256=self.plan.sha256 if self.plan else self.intent.sha256).store(value)

    def _legacy(self):
        self._fence(fallback_only=True)
        result = {}
        for unit, pin in self.intent.legacy.units.items():
            native = strict_json(self.commands.run("docker", ("inspect", "--type", "container", "vkm-core-" + unit + "-1")))
            if not isinstance(native, list) or len(native) != 1:
                raise GenerationUnavailable("retained legacy container unavailable")
            raw = native[0]
            got = {**container_fingerprint(raw, project="vkm-core", unit=unit),
                   "container_id": raw.get("Id"), "started_at": raw["State"].get("StartedAt")}
            if got != pin.model_dump(mode="json"):
                raise GenerationUnavailable("legacy topology changed; bootstrap cannot restore or restart legacy")
            result[unit] = got
        return record_hash({"topology": self.intent.legacy.sha256, "native": result})

    def _networks(self, *, empty=False):
        result = {}
        for pin in self.intent.recipe.networks:
            native = strict_json(self.commands.run("docker", ("network", "inspect", pin.name)))
            if (not isinstance(native, list) or len(native) != 1 or native[0].get("Id") != pin.network_id
                    or network_fingerprint(native[0]) != pin.config_sha256
                    or empty and native[0].get("Containers")):
                raise GenerationUnavailable("pre-existing isolated external network differs")
            result[pin.name] = {"id": pin.network_id, "config_sha256": pin.config_sha256}
        return result

    def _project(self):
        raw = self.commands.run("docker", ("container", "ls", "--all", "--filter",
            "label=com.docker.compose.project=vkm-core-shadow", "--format", "{{.ID}}", "--no-trunc"))
        values = raw.decode("ascii").splitlines()
        import re
        if any(not re.fullmatch(r"[0-9a-f]{64}", v) for v in values) or len(set(values)) != len(values) or len(values) > 2:
            raise GenerationUnavailable("shadow project includes unknown objects")
        return values

    def _empty(self):
        if self._project() or any((self.root / name).exists() or (self.root / name).is_symlink()
                                  for name in ("CURRENT", "receiver-release.CURRENT")):
            raise GenerationUnavailable("first-baseline fallback is not EMPTY")
        return record_hash({"project": "vkm-core-shadow", "containers": [], "selectors": [],
                            "networks": self._networks(empty=True)})

    def _compose(self):
        raw = self.template._compose(self.prototype)
        wanted = {n.name: {"external": True, "name": n.name} for n in self.intent.recipe.networks}
        if raw.get("networks") != wanted or any(k in raw for k in ("volumes", "secrets", "configs")):
            raise GenerationUnavailable("cold bootstrap cannot create infrastructure or named storage")
        env = operator_environment(self.intent.environment)
        runtime = RuntimeConfig.model_validate(bound_json(self.intent.runtime))
        if (runtime.bootstrap_startup != self.intent.startup_authority
                or Path(runtime.runtime_root).absolute() / "served" != self.root
                or raw["services"]["api"].get("environment") != env
                or Path(env.get("VKM_UPDATE_RUNTIME_FILE", "")).absolute() != Path(self.intent.runtime.path).absolute()):
            raise GenerationUnavailable("cold recipe selects a different startup authority/runtime")
        for unit, spec in raw["services"].items():
            if (spec.get("labels", {}).get("io.vkm.bootstrap.owner") != self.intent.bootstrap_id
                    or spec.get("networks") != sorted(wanted)):
                raise GenerationUnavailable("cold receivers need exact owner and external network inventory")
            image = strict_json(self.commands.run("docker", ("image", "inspect", spec["image"])))
            if not isinstance(image, list) or len(image) != 1 or image[0].get("Id") != self.intent.recipe.images[unit]:
                raise GenerationUnavailable("cold recipe image is not locally available")
            if image[0].get("Config", {}).get("Volumes"):
                raise GenerationUnavailable("cold image cannot create anonymous storage")
        return raw

    def _match_recipe(self, item, unit, spec):
        image = strict_json(self.commands.run("docker", ("image", "inspect", spec["image"])))[0]
        base = image.get("Config", {})
        def env_dict(values):
            out = {}
            for value in values or []:
                if not isinstance(value, str) or "=" not in value or value.split("=", 1)[0] in out:
                    raise GenerationUnavailable("ambiguous native receiver environment")
                k, v = value.split("=", 1)
                out[k] = v
            return out
        wanted_env = {**env_dict(base.get("Env")), **spec.get("environment", {})}
        cfg, host = item["Config"], item["HostConfig"]
        mounts = lambda values: sorted((m["Type"], m["Source"], m["Destination"], m["RW"]) for m in values)
        expected_mounts = sorted((m["type"], m["source"], m["target"], not m["read_only"])
                                  for m in spec.get("volumes", []))
        if (env_dict(cfg.get("Env")) != wanted_env or cfg.get("Cmd") != spec["command"]
                or cfg.get("Entrypoint") != base.get("Entrypoint")
                or cfg.get("User", "") != spec.get("user", base.get("User", ""))
                or host.get("ReadonlyRootfs") is not True or host.get("CapDrop") != ["ALL"]
                or host.get("Memory") != spec["mem_limit"]
                or host.get("NanoCpus") != int(spec["cpus"] * 1_000_000_000)
                or host.get("Privileged", False) or host.get("Devices") or host.get("CapAdd")
                or host.get("PidMode", "") or host.get("NetworkMode") == "host"
                or (host.get("SecurityOpt") or []) != spec.get("security_opt", [])
                or mounts(item["Mounts"]) != expected_mounts
                or set(item["NetworkSettings"]["Networks"]) != set(spec["networks"])):
            raise GenerationUnavailable("native receiver differs from approved effective recipe")
        # Port mappings must be explicit loopback bindings. No extra native
        # host publication is permitted, including image-exposed ports.
        expected_ports = {}
        for port in spec.get("ports", []):
            if not isinstance(port, dict) or port.get("host_ip") != "127.0.0.1":
                raise GenerationUnavailable("bootstrap requires explicit loopback ports")
            key = str(port["target"]) + "/" + port.get("protocol", "tcp")
            expected_ports.setdefault(key, []).append({"HostIp": "127.0.0.1", "HostPort": str(port["published"])})
        if (host.get("PortBindings") or {}) != expected_ports:
            raise GenerationUnavailable("native receiver port bindings differ")

    def _compose_command(self, action, *extra):
        recipe = self.intent.recipe
        return self.commands.run("compose", ("--env-file", "/dev/null", "--project-name", recipe.project,
            "--project-directory", str(Path(recipe.compose.path).parent), "--file", recipe.compose.path,
            action, *extra, "api", "mcp"))

    def _native_pair(self, *, stopped):
        recipe = self._compose()
        result, pins = {}, {}
        for unit in ("api", "mcp"):
            raw = strict_json(self.commands.run("docker", ("inspect", "--type", "container", "vkm-core-shadow-" + unit + "-1")))
            if not isinstance(raw, list) or len(raw) != 1:
                raise GenerationUnavailable("cold prepare did not create exactly one receiver")
            item = raw[0]
            if (item["Config"].get("Labels", {}).get("io.vkm.bootstrap.owner") != self.intent.bootstrap_id
                    or item.get("Image") != self.intent.recipe.images[unit]):
                raise GenerationUnavailable("native cold receiver is not owned by this intent")
            self._match_recipe(item, unit, recipe["services"][unit])
            pins[unit] = container_fingerprint(item, project="vkm-core-shadow", unit=unit, _stopped_prepare=stopped)
            result[unit] = item
        if set(self._project()) != {raw["Id"] for raw in result.values()}:
            raise GenerationUnavailable("shadow project contains an unapproved receiver")
        return result, pins

    def _prepare(self):
        if self.recovery_only:
            raise GenerationUnavailable("recovery-only bootstrap cannot create receivers")
        self.controller.writer_fence(self.intent.request_id)
        self._empty()
        self._compose()
        self._fence()
        self._legacy()
        self._networks(empty=True)
        self.controller.writer_fence(self.intent.request_id)
        self._compose_command("create", "--no-build", "--pull", "never")
        self._fault("COLD_CREATED")
        raw, pins = self._native_pair(stopped=True)
        release = self.prototype.model_copy(update={"units": {u: UnitPin(**pin) for u, pin in pins.items()}})
        preparation = BootstrapPreparation(intent_sha256=self.intent.sha256,
            startup_authority_sha256=self.authority.sha256, recipe_sha256=record_hash(self.intent.recipe),
            runtime_sha256=self.prototype.runtime_config_sha256, receiver_release=release.model_dump(mode="json"),
            native_container_ids={u: v["Id"] for u, v in raw.items()})
        digest = self._store(preparation.model_dump(mode="json"))
        self._event("COLD_PREPARED", preparation_sha256=digest, receiver_release_sha256=release.sha256)
        self._fault("COLD_PREPARED")
        return preparation, release, digest

    def _start(self, release):
        self.controller.writer_fence(self.intent.request_id)
        write_bytes(self.root / "tmp", self.root / "receiver-release.CURRENT", (release.sha256 + "\n").encode("ascii"), overwrite=True)
        self.controller._select_current(self.manifest)
        (self.root / "MAINTENANCE").unlink()
        _fsync_dir(self.root)
        self._writer_fence(self.intent.request_id, maintenance=False)
        self._fault("BOOTSTRAP_CURRENT")
        units = CoreUnitControl(self._unit_config(release), commands=self.commands, **self._unit_kwargs)
        proof = units.rebind(self.manifest, gate=self.barrier._gate_identity)
        self._writer_fence(self.intent.request_id, maintenance=False)
        self._legacy()
        self._event("CLOSED_NATIVE_STARTED", receiver_release_sha256=release.sha256,
                    native_proof_sha256=self._store(proof.model_dump(mode="json")))
        self._fault("CLOSED_NATIVE_STARTED")
        return units, proof

    def _restore_empty(self):
        previous = self.recovery_only
        self.recovery_only = True
        try:
            return self._restore_empty_owned()
        finally:
            self.recovery_only = previous

    def _restore_empty_owned(self):
        self._fence(fallback_only=True)
        self.controller._pause(self.authority.request_key)
        self.controller.writer_fence(self.intent.request_id)
        # Delete ONLY exact native IDs with this intent's owner and image pins.
        # A substituted object is BLOCKED; broad project down/prune is forbidden.
        owned = []
        for identifier in self._project():
            raw = strict_json(self.commands.run("docker", ("inspect", "--type", "container", identifier)))
            if not isinstance(raw, list) or len(raw) != 1:
                raise GenerationUnavailable("fallback receiver native identity unavailable")
            item = raw[0]
            labels = item["Config"].get("Labels", {})
            unit = labels.get("com.docker.compose.service")
            if (unit not in {"api", "mcp"} or item.get("Id") != identifier
                    or labels.get("com.docker.compose.project") != "vkm-core-shadow"
                    or labels.get("io.vkm.bootstrap.owner") != self.intent.bootstrap_id
                    or item.get("Image") != self.intent.recipe.images[unit]):
                raise GenerationUnavailable("fallback cannot remove an unowned/substituted object")
            owned.append((identifier, unit, record_hash(item)))
        # Inspect the complete set before the first deletion. A foreign second
        # object must not permit a partial mutation of the first one.
        for identifier, unit, digest in owned:
            raw = strict_json(self.commands.run("docker", ("inspect", "--type", "container", identifier)))
            if len(raw) != 1 or record_hash(raw[0]) != digest:
                raise GenerationUnavailable("fallback receiver changed before removal")
            self.controller.writer_fence(self.intent.request_id)
            self.commands.run("docker", ("container", "rm", "--force", identifier))
            self._fault("REMOVED:" + unit)
        self.controller.writer_fence(self.intent.request_id)
        for name in ("CURRENT", "receiver-release.CURRENT"):
            path = self.root / name
            if path.is_symlink():
                raise GenerationUnavailable("fallback selector was replaced")
            path.unlink(missing_ok=True)
        _fsync_dir(self.root)
        empty, legacy = self._empty(), self._legacy()
        self._event("EMPTY_RESTORED", empty_sha256=empty, legacy_sha256=legacy)
        self._fault("EMPTY_RESTORED")
        return empty, legacy

    def _ref(self, digest):
        return BoundFile(path=str(Path(self.config.qualification_root) / (digest + ".json")), sha256=digest)

    def plan_run(self):
        if self.recovery_only:
            raise GenerationUnavailable("recovery-only bootstrap cannot plan startup")
        self._profile()
        legacy, empty = self._legacy(), self._empty()
        return {"schema": "vkm-bootstrap-execution-plan/1", "status": "PLANNED_CLOSED",
            "scope": self.intent.scope, "intent_sha256": self.intent.sha256,
            "control_sha256": self.config_ref.sha256, "private_probe_plan_sha256": self.plan.sha256,
            "legacy_sha256": legacy, "empty_sha256": empty, "serving_admission": False,
            "scientific_admission": False}

    async def _load_private(self, release):
        from vkm_corpus.api.app import build_from_settings
        from vkm_corpus.update.serving import NativeServingBindings
        from vkm_corpus.update.operator import qualified_read_context
        env = operator_environment(self.intent.environment)
        runtime = RuntimeConfig.model_validate(bound_json(self.intent.runtime))
        app = build_from_settings(environ=env, _defer_generation_binding=True)
        if (app.state.update_runtime.config != runtime or runtime.native_serving is None
                or not qualified_read_context(app.state.config, self.probes, release)):
            raise GenerationUnavailable("bootstrap private factory differs from pinned readers")
        native = await NativeServingBindings.bind(
            app.state.service.deps, runtime.native_serving, api_service=app.state.service)
        result = NativeRelease(None, app, app.state.update_runtime, native, self.probes, self.manifest)
        self.controller.coordinator.verify(self.manifest, result.components(), await native.observe_services())
        return result

    async def _private_probes(self, release):
        from vkm_corpus.update.acceptance import CandidateFence, PrivateASGIProbeTransport
        from vkm_corpus.update.bootstrap_probes import (ClosedBootstrapAuthority,
            BootstrapProbeRegistrar, qualify_closed_bootstrap)
        if self.loaded is not None:
            await self.loaded.close()
        self.loaded = await self._load_private(release)
        value, plan = self.loaded, self.probes
        deps = value.app.state.service.deps
        fence = CandidateFence(plan.candidate, duckdb_path=deps.canon.path,
            policy_path=deps.access_policy.path, api_config=value.app.state.config,
            observer=value.components, production=self.intent.scope != "SYNTHETIC",
            max_duckdb_bytes=self.config.max_duckdb_bytes,
            nav_duckdb_path=deps.nav._paths()[1] if deps.nav is not None else None)
        registrar = BootstrapProbeRegistrar(Path(self.config.qualification_root), approved_plan_sha256=self.plan.sha256)
        try:
            with ClosedBootstrapAuthority(self.intent.startup_authority, self.plan, intent=self.config.intent,
                    writer_fd=self.controller._writer_fd, gate_fd=self.controller._gate_fd) as authority:
                async with PrivateASGIProbeTransport(value.app.state.service, value.app.state.config, fence, plan) as transport:
                    result = await qualify_closed_bootstrap(self.plan, transport=transport, fence=fence,
                        registrar=registrar, authority=authority)
            if result.get("status") != "PRIVATE_PROBES_VERIFIED":
                raise GenerationUnavailable("complete closed private probes did not qualify")
            return result["receipt_sha256"]
        finally:
            fence.close()

    def _partial_start(self, release):
        """A bounded native failure: only API starts; MCP remains stopped."""
        self.controller.writer_fence(self.intent.request_id)
        write_bytes(self.root / "tmp", self.root / "receiver-release.CURRENT", (release.sha256 + "\n").encode("ascii"), overwrite=True)
        self.controller._select_current(self.manifest)
        (self.root / "MAINTENANCE").unlink()
        _fsync_dir(self.root)
        self._writer_fence(self.intent.request_id, maintenance=False)
        recipe = self.intent.recipe
        self.commands.run("compose", ("--env-file", "/dev/null", "--project-name", recipe.project,
            "--project-directory", str(Path(recipe.compose.path).parent), "--file", recipe.compose.path,
            "start", "api"))
        # Inspect BOTH actual objects. A simulated exception alone is no drill.
        observed = {}
        for unit in ("api", "mcp"):
            native = strict_json(self.commands.run("docker", ("inspect", "--type", "container", "vkm-core-shadow-" + unit + "-1")))
            if len(native) != 1:
                raise GenerationUnavailable("partial startup boundary missing")
            raw = native[0]
            pin = container_fingerprint(raw, project="vkm-core-shadow", unit=unit, _stopped_prepare=unit == "mcp")
            if pin != release.units[unit].model_dump(mode="json"):
                raise GenerationUnavailable("partial startup native recipe changed")
            observed[unit] = {"container_id": raw["Id"], "running": raw["State"]["Running"], **pin}
        self._writer_fence(self.intent.request_id, maintenance=False)
        digest = self._store({"schema": "vkm-bootstrap-partial-start/1", "scope": self.intent.scope,
            "intent_sha256": self.intent.sha256, "admission": "CLOSED", "units": observed})
        self._event("PARTIAL_NATIVE_START", native_sha256=digest)
        self._fault("PARTIAL_NATIVE_START")
        return digest

    def _history_receipts(self):
        return tuple(self._store({k: v for k, v in event.items() if k != "journal_sha256"})
                     for event in self.controller.history(self.intent.request_id))

    def execute(self, confirmation):
        if self.recovery_only:
            raise GenerationUnavailable("recovery-only bootstrap cannot qualify receivers")
        # No command is generated until the exact immutable plan is approved.
        marker = self.root / "bootstrap.CLOSED_BASELINE.json"
        with self.controller._writer():
            if marker.exists():
                registration = BoundFile.model_validate(json.loads(marker.read_bytes()))
                from vkm_corpus.update.bootstrap import require_closed_baseline
                proof = require_closed_baseline(registration, self.manifest, production=self.intent.scope != "SYNTHETIC")
                registered = BaselineRegistration.model_validate(bound_json(registration))
                if (registered.intent != self.config.intent or registered.probes.sha256 != self.plan.sha256
                        or proof.intent_sha256 != self.intent.sha256):
                    raise GenerationUnavailable("registered baseline belongs to another approved intent")
                if confirmation != self.intent.sha256:
                    raise GenerationUnavailable("bootstrap intent confirmation differs")
                self.controller._pause(self.authority.request_key)
                (self.root / "MAINTENANCE").unlink()
                _fsync_dir(self.root)
                self._writer_fence(self.intent.request_id, maintenance=False)
                preparation = BootstrapPreparation.model_validate(bound_json(
                    registered.preparation))
                units = CoreUnitControl(self._unit_config(ComposeRelease.model_validate(preparation.receiver_release)),
                    commands=self.commands, **self._unit_kwargs)
                units._joined_proof(self.manifest, gate=self.barrier._gate_identity, admission_open=False)
                self._legacy()
                return {"status": proof.status, "scope": proof.scope, "registration": registration.model_dump(mode="json"),
                        "serving_admission": False, "scientific_admission": False}
            plan = self.plan_run()
            if confirmation != self.intent.sha256:
                raise GenerationUnavailable("exact bootstrap intent confirmation required")
            self._event("BOOTSTRAP_ATTEMPT", intent_sha256=self.intent.sha256,
                plan_sha256=self._store(plan))
            self.controller._pause(self.authority.request_key)
            try:
                prep, release, prep_sha = self._prepare()
                self._start(release)
                first_private = self.portal.call(self._private_probes(release), timeout=self.probes.total_timeout_seconds + 60)
                self._event("FIRST_PRIVATE_PROBES_VERIFIED", private_probe_sha256=first_private)
                self._restore_empty()
                prep, release, prep_sha = self._prepare()
                failed = self._partial_start(release)
                empty_after, legacy_after = self._restore_empty()
                boundary = BootstrapBoundaryReceipt(scope=self.intent.scope, intent_sha256=self.intent.sha256,
                    startup_authority_sha256=self.authority.sha256, preparation_sha256=prep_sha,
                    legacy_before_sha256=plan["legacy_sha256"], legacy_after_sha256=legacy_after,
                    empty_before_sha256=plan["empty_sha256"], empty_after_sha256=empty_after,
                    failed_native_start_sha256=failed, journal_receipts=self._history_receipts())
                boundary_sha = self._store(boundary.model_dump(mode="json"))
                prep, release, prep_sha = self._prepare()
                units, native = self._start(release)
                private_sha = self.portal.call(self._private_probes(release), timeout=self.probes.total_timeout_seconds + 60)
                self._writer_fence(self.intent.request_id, maintenance=False)
                repeated = units._joined_proof(self.manifest, gate=self.barrier._gate_identity, admission_open=False)
                native_sha = self._store(repeated.model_dump(mode="json"))
                if self._legacy() != plan["legacy_sha256"]:
                    raise GenerationUnavailable("retained legacy changed")
                qualification = BaselineQualification(scope=self.intent.scope, intent_sha256=self.intent.sha256,
                    startup_authority_sha256=self.authority.sha256, preparation_sha256=prep_sha,
                    baseline_generation_sha256=self.manifest.sha256, private_probe_receipt_sha256=private_sha,
                    boundary_receipt_sha256=boundary_sha, repeated_native_start_sha256=native_sha,
                    legacy_topology_sha256=self.intent.legacy.sha256, closed_owner_key=self.authority.request_key)
                qualification_sha = self._store(qualification.model_dump(mode="json"))
                registration = BaselineRegistration(scope=self.intent.scope, intent=self.config.intent,
                    failed_preparation=self._ref(boundary.preparation_sha256),
                    probes=self._ref(self._store(self.plan.model_dump(mode="json"))), preparation=self._ref(prep_sha),
                    private_probe=self._ref(private_sha), boundary=self._ref(boundary_sha),
                    native_start=self._ref(native_sha), qualification=self._ref(qualification_sha))
                ref = self._ref(self._store(registration.model_dump(mode="json")))
                from vkm_corpus.update.bootstrap import require_closed_baseline
                require_closed_baseline(ref, self.manifest, production=self.intent.scope != "SYNTHETIC")
                self._writer_fence(self.intent.request_id, maintenance=False)
                write_bytes(self.root / "tmp", marker, canonical_bytes(ref), overwrite=False)
                self._event("CLOSED_BASELINE_REGISTERED", registration_sha256=ref.sha256)
                self._fault("CLOSED_BASELINE_REGISTERED")
                return {"status": qualification.status, "scope": self.intent.scope,
                    "registration": ref.model_dump(mode="json"), "serving_admission": False, "scientific_admission": False}
            except Exception:
                # An acknowledged durable registration survives an ACK loss.
                # Never erase it merely because response delivery failed.
                if not marker.exists():
                    self._restore_empty()
                raise

    def recover(self, confirmation):
        if confirmation != self.intent.sha256:
            raise GenerationUnavailable("exact retained intent confirmation required")
        with self.controller._writer():
            if (self.root / "bootstrap.CLOSED_BASELINE.json").exists():
                raise GenerationUnavailable("qualified baseline requires coherent previous rollback, not EMPTY cleanup")
            empty, legacy = self._restore_empty()
            return {"status": "EMPTY_RESTORED", "scope": self.intent.scope, "empty_sha256": empty,
                "legacy_sha256": legacy, "serving_admission": False, "scientific_admission": False}

    def close(self):
        try:
            if self.loaded is not None:
                self.portal.call(self.loaded.close(), timeout=15)
        finally:
            self.portal.close()


def command(args):
    operator = None
    try:
        ref = BoundFile(path=args.operator_config, sha256=args.config_sha256)
        config = BootstrapControlConfig.model_validate(bound_json(ref))
        operator = IsolatedBootstrap(config, config_ref=ref, _recovery_only=args.bootstrap_command == "recover")
        if args.bootstrap_command == "plan":
            result = operator.plan_run()
        elif args.bootstrap_command == "recover":
            result = operator.recover(args.confirm_intent)
        else:
            result = operator.execute(args.confirm_intent)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0 if result.get("status") in {"PLANNED_CLOSED", "CLOSED_BASELINE_QUALIFIED", "EMPTY_RESTORED"} else 2
    except Exception as exc:
        print(json.dumps({"status": "BLOCKED", "reason": type(exc).__name__}))
        return 2
    finally:
        if operator is not None:
            operator.close()


def register(subparsers):
    parser = subparsers.add_parser("bootstrap", help="isolated first baseline; never serving/scientific READY")
    sub = parser.add_subparsers(dest="bootstrap_command", required=True)
    for action in ("plan", "execute", "recover"):
        p = sub.add_parser(action)
        p.add_argument("--operator-config", required=True)
        p.add_argument("--config-sha256", required=True)
        if action != "plan":
            p.add_argument("--confirm-intent", required=True)
        p.set_defaults(func=command)

"""SYNTHETIC fake-Docker cold bootstrap boundaries, never actual runtime proof."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_shadow_acceptance import setup as shadow_setup
from vkm_corpus.update import bootstrap_native as B
from vkm_corpus.update.admission import AdmissionState, read_state
from vkm_corpus.update.bootstrap import (BootstrapIntent, BootstrapNetwork, BootstrapRecipe,
    BootstrapStartupAuthority, LegacyTopology)
from vkm_corpus.update.generation import GenerationUnavailable
from vkm_corpus.update.operator_units import container_fingerprint
from vkm_corpus.update.runtime import BoundFile, RuntimeConfig
from vkm_evidence.contracts import canonical_bytes, record_hash


def bound(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = canonical_bytes(value)
    path.write_bytes(data)
    path.chmod(0o600)
    return BoundFile(path=str(path), sha256=hashlib.sha256(data).hexdigest())


class FakeDocker:
    def __init__(self, legacy, cold, network):
        self.legacy, self.cold, self.network = legacy, cold, network
        self.objects, self.calls = {}, []
        self.on_create = lambda: None
        self.on_image = lambda: None
        self.on_up = lambda: None
        self.starts = 0

    def run(self, kind, args):
        self.calls.append((kind, args))
        if kind == "compose":
            assert args[:2] == ("--env-file", "/dev/null")
            assert args[3] == "vkm-core-shadow"
            action = args[8]
            assert args[-2:] == ("api", "mcp") if action != "start" else args[9:] == ("api",)
            if action == "create":
                assert args[9:-2] == ("--no-build", "--pull", "never")
                self.objects = {v["Id"]: copy.deepcopy(v) for v in self.cold.values()}
                self.on_create()
            elif action == "up":
                assert args[9:-2] == ("--detach", "--no-deps", "--no-build", "--pull", "never", "--force-recreate")
                self.starts += 1
                replaced = {}
                for raw in self.objects.values():
                    raw["Id"] = hashlib.sha256((raw["Id"] + str(self.starts)).encode("ascii")).hexdigest()
                    raw["State"].update(Running=True, Pid=100, StartedAt="synthetic-start")
                    replaced[raw["Id"]] = raw
                self.objects = replaced
                self.on_up()
            elif action == "start":
                for raw in self.objects.values():
                    if raw["Config"]["Labels"]["com.docker.compose.service"] == "api":
                        raw["State"].update(Running=True, Pid=101, StartedAt="synthetic-partial-start")
            else:
                raise AssertionError("unapproved synthetic Compose action: " + action)
            return b""
        if args[:2] == ("image", "inspect"):
            self.on_image()
            return canonical_bytes([{"Id": "sha256:" + args[2].split("@sha256:")[-1]}])
        if args[:2] == ("network", "inspect"):
            assert args[2] == self.network["Name"]
            native = copy.deepcopy(self.network)
            native["Containers"] = {key: {} for key in self.objects}
            return canonical_bytes([native])
        if args[:3] == ("container", "ls", "--all"):
            assert args[3:5] == ("--filter", "label=com.docker.compose.project=vkm-core-shadow")
            return ("\n".join(self.objects) + ("\n" if self.objects else "")).encode("ascii")
        if args[:3] == ("inspect", "--type", "container"):
            key = args[3]
            if key.startswith("vkm-core-shadow-"):
                unit = key.split("-")[-2]
                matching = [raw for raw in self.objects.values()
                    if raw["Config"]["Labels"].get("com.docker.compose.service") == unit]
                return canonical_bytes(matching)
            if key.startswith("vkm-core-"):
                return canonical_bytes([self.legacy[key.split("-")[-2]]])
            return canonical_bytes([self.objects[key]] if key in self.objects else [])
        if args[:3] == ("container", "rm", "--force"):
            assert args[3] in self.objects
            del self.objects[args[3]]
            return b""
        raise AssertionError("unapproved fake-Docker command: " + repr(args))


def native_container(unit, *, project, image, number, spec=None):
    spec = spec or {}
    running = project == "vkm-core"
    labels = {"com.docker.compose.project": project, "com.docker.compose.service": unit,
              "com.docker.compose.container-number": "1", **spec.get("labels", {})}
    return {"Id": number * 64, "Image": image,
        "Config": {"Labels": labels, "Env": [k + "=" + v for k, v in spec.get("environment", {}).items()],
                   "Cmd": spec.get("command", ["retained-legacy"]), "Hostname": "generated", "User": spec.get("user", "")},
        "State": {"Running": running, "Paused": False, "Pid": 123 if running else 0,
                  "StartedAt": "legacy-start" if running else "0001-01-01T00:00:00Z"},
        "HostConfig": {"PidMode": "", "ReadonlyRootfs": spec.get("read_only", True),
                       "CapDrop": spec.get("cap_drop", ["ALL"]), "Memory": spec.get("mem_limit", 2 * 1024**3),
                       "NanoCpus": int(spec.get("cpus", 2) * 1_000_000_000)},
        "Mounts": [{"Type": m["type"], "Source": m["source"], "Destination": m["target"],
                    "Mode": "ro" if m["read_only"] else "rw", "RW": not m["read_only"], "Propagation": "rprivate"}
                   for m in spec.get("volumes", [])],
        "NetworkSettings": {"Networks": {name: {} for name in spec.get("networks", ["legacy-net"])}, "Ports": {}}}


@pytest.fixture
def cold_case(tmp_path, monkeypatch, shadow_setup):
    authority = tmp_path / "authority"
    root = tmp_path / "runtime/served"
    qual = tmp_path / "bootstrap-receipts"
    protected = tmp_path / "originals"
    for path in (authority, root, qual, protected): path.mkdir(parents=True)
    (root / "admission.lock").touch()
    dummy = bound(authority / "dummy.json", {"scope": "SYNTHETIC"})
    legacy_raw = {unit: native_container(unit, project="vkm-core", image="sha256:" + letter * 64, number=letter)
                  for unit, letter in (("api", "a"), ("mcp", "b"))}
    legacy = LegacyTopology(compose=bound(authority / "legacy-compose.json", {"retained": "synthetic"}),
        selectors=(bound(authority / "legacy-current.json", {"retained": "legacy"}),),
        units={unit: {**container_fingerprint(raw, project="vkm-core", unit=unit),
            "container_id": raw["Id"], "started_at": raw["State"]["StartedAt"]} for unit, raw in legacy_raw.items()})
    backup = bound(authority / "backup.json", {"scope": "SYNTHETIC", "independent": True})
    network = {"Id": "f" * 64, "Name": "synthetic-shadow-net", "Driver": "bridge", "Scope": "local",
               "Internal": True, "Attachable": False, "Ingress": False, "EnableIPv6": False,
               "IPAM": {}, "Options": {}, "Labels": {"test-scope": "SYNTHETIC"}, "ConfigOnly": False,
               "ConfigFrom": {}, "Containers": {}}
    config = B.BootstrapControlConfig(intent=dummy, probe_spec=dummy, docker=BoundFile(
        path=str(authority / "docker"), sha256="d" * 64), compose_binary=BoundFile(
        path=str(authority / "docker-compose"), sha256="e" * 64), docker_socket=str(tmp_path / "docker.sock"),
        control_root=str(root), authority_root=str(authority), qualification_root=str(qual),
        protected_roots=(str(protected),), receiver_url="http://127.0.0.1:18000",
        mcp_receiver_url="http://127.0.0.1:18765", operator_token_file=str(authority / "token"))
    probes = shadow_setup.plan.model_copy(update={"scope": "SYNTHETIC", "drill_receipt_sha256": None,
        "deployment_profile_sha256": config.profile_sha256, "isolation_attestation_sha256": "1" * 64})
    startup = BootstrapStartupAuthority(bootstrap_id="synthetic-cold", request_id="synthetic-request",
        candidate=probes.candidate, control_root=str(root), isolation_attestation_sha256="1" * 64,
        legacy_topology_sha256=legacy.sha256, independent_backup_sha256=backup.sha256)
    startup_ref = bound(authority / "startup.json", startup)
    runtime = RuntimeConfig(runtime_root=str(root.parent), originals_root=str(protected),
        policy=BoundFile(path=str(shadow_setup.fence.policy_path), sha256=probes.candidate.policy_sha256),
        qualification_root=str(qual), expected_commit=probes.candidate.code_commit,
        memory_budget_gib=4, memory_reserve_gib=1, worker_memory_gib=1, min_free_disk_gib=1,
        native_serving=dummy, bootstrap_startup=startup_ref)
    runtime_ref = bound(authority / "runtime.json", runtime)
    env = {"VKM_API_PROFILE": "production", "VKM_DATA_ROLE": "canonical", "VKM_UPDATE_RUNTIME_FILE": runtime_ref.path}
    env_ref = bound(authority / "environment.json", env)
    compose = {"name": "vkm-core-shadow", "networks": {network["Name"]: {"external": True, "name": network["Name"]}},
        "services": {"api": {"image": "synthetic-api@sha256:" + "c" * 64,
            "command": ["api", "serve", "--host", "0.0.0.0", "--port", "8000"], "environment": env},
            "mcp": {"image": "synthetic-mcp@sha256:" + "d" * 64,
                "command": ["mcp", "serve", "--kind", "read", "--host", "0.0.0.0", "--port", "8765"],
                "environment": {"VKM_API_URL": "http://api:8000", "VKM_API_TOKEN_FILE": "/run/read",
                    "VKM_MCP_TOKEN_FILE": "/run/mcp", "VKM_DEPLOYMENT_TOKEN_FILE": "/run/operator",
                    "VKM_DEPLOYMENT_GATE_FILE": str(root / "admission.lock")}}}}
    for spec in compose["services"].values():
        spec.update(read_only=True, cap_drop=["ALL"], mem_limit=2 * 1024**3, cpus=2,
            labels={"io.vkm.bootstrap.owner": startup.bootstrap_id}, networks=[network["Name"]], volumes=[
                {"type": "bind", "source": str(root), "target": str(root), "read_only": True,
                 "bind": {"create_host_path": False}},
                {"type": "bind", "source": str(root / "admission.lock"), "target": str(root / "admission.lock"),
                 "read_only": False, "bind": {"create_host_path": False}}])
    compose_ref = bound(authority / "cold-compose.json", compose)
    intent = BootstrapIntent(scope="SYNTHETIC", bootstrap_id=startup.bootstrap_id, request_id=startup.request_id,
        startup_authority=startup_ref, retained_startup=startup, probe_spec_sha256=probes.sha256, deployment_profile_sha256=config.profile_sha256,
        environment=env_ref, runtime=runtime_ref, legacy=legacy, independent_backup=backup,
        recipe=BootstrapRecipe(compose=compose_ref, images={"api": "sha256:" + "c" * 64, "mcp": "sha256:" + "d" * 64},
            networks=(BootstrapNetwork(name=network["Name"], network_id=network["Id"],
                config_sha256=B.network_fingerprint(network)),)))
    config = config.model_copy(update={"intent": bound(authority / "intent.json", intent),
                                      "probe_spec": bound(authority / "probes.json", probes)})
    config_ref = bound(authority / "control.json", config)
    native = {unit: native_container(unit, project="vkm-core-shadow", image=intent.recipe.images[unit],
              number=letter, spec=compose["services"][unit]) for unit, letter in (("api", "1"), ("mcp", "2"))}
    commands = FakeDocker(legacy_raw, native, network)
    monkeypatch.setattr(B.IsolatedBootstrap, "_principal_hash", lambda self: "9" * 64)
    if os.name != "posix":
        # Fake-Docker shape checks are portable. Kernel flock itself is NOT
        # exercised by this method fixture and is separately Linux-qualified.
        from vkm_corpus.update.barrier import ReceiverBarrier
        monkeypatch.setattr(B, "ReceiverBarrier", lambda receiver_id, **kwargs:
            ReceiverBarrier(receiver_id, require_durable=True))
    bootstrap = B.IsolatedBootstrap(config, config_ref=config_ref, _commands=commands)
    # These method tests deliberately isolate native inspection from the
    # separately tested real writer lease. They never grant production scope.
    monkeypatch.setattr(bootstrap.controller, "writer_fence", lambda request:
        None if request == intent.request_id else (_ for _ in ()).throw(GenerationUnavailable("wrong request")))
    yield SimpleNamespace(bootstrap=bootstrap, commands=commands, root=root, intent=intent, config=config,
        config_ref=config_ref, authority=authority, startup=startup, native=native, shadow=shadow_setup)
    bootstrap.close()


def mutations(commands):
    return [(kind, args) for kind, args in commands.calls if kind == "compose" or args[:2] == ("container", "rm")]


def test_cold_native_prepare_captures_real_stopped_pins_without_qualification(cold_case):
    c = cold_case
    preparation, release, digest = c.bootstrap._prepare()
    assert preparation.status == "PREPARED_CLOSED_NOT_QUALIFIED"
    assert preparation.native_container_ids == {u: raw["Id"] for u, raw in c.native.items()}
    for unit in ("api", "mcp"):
        assert release.units[unit].model_dump() == container_fingerprint(c.native[unit],
            project="vkm-core-shadow", unit=unit, _stopped_prepare=True)
        assert release.units[unit].config_sha256 != "0" * 64
    calls = mutations(c.commands)
    assert len(calls) == 1 and calls[0][0] == "compose"
    assert calls[0][1][-6:] == ("create", "--no-build", "--pull", "never", "api", "mcp")
    assert all(not any(arg in {"build", "pull", "down", "prune", "network-create"} for arg in args)
               for _, args in c.commands.calls)


@pytest.mark.parametrize("change", ["owner", "image", "running", "pid", "project", "paused"])
def test_cold_native_capture_rejects_wrong_owned_object(cold_case, change):
    c = cold_case
    raw = c.commands.cold["api"]
    if change == "owner": raw["Config"]["Labels"]["io.vkm.bootstrap.owner"] = "foreign"
    if change == "image": raw["Image"] = "sha256:" + "0" * 64
    if change == "running": raw["State"]["Running"] = True
    if change == "pid": raw["State"]["Pid"] = 1
    if change == "project": raw["Config"]["Labels"]["com.docker.compose.project"] = "vkm-core"
    if change == "paused": raw["State"]["Paused"] = True
    with pytest.raises(GenerationUnavailable): c.bootstrap._prepare()


@pytest.mark.parametrize("change", ["env", "command", "readonly", "memory", "cpu", "mount", "network"])
def test_cold_native_capture_must_match_approved_effective_recipe(cold_case, change):
    c = cold_case
    raw = c.commands.cold["api"]
    if change == "env": raw["Config"]["Env"] = ["VKM_API_PROFILE=compatibility"]
    if change == "command": raw["Config"]["Cmd"] = ["arbitrary-command"]
    if change == "readonly": raw["HostConfig"]["ReadonlyRootfs"] = False
    if change == "memory": raw["HostConfig"]["Memory"] = 0
    if change == "cpu": raw["HostConfig"]["NanoCpus"] = 0
    if change == "mount": raw["Mounts"][0]["RW"] = True
    if change == "network": raw["NetworkSettings"]["Networks"] = {"legacy-net": {}}
    with pytest.raises(GenerationUnavailable): c.bootstrap._prepare()


@pytest.mark.parametrize("change", ["id", "config", "occupied"])
def test_empty_fallback_requires_exact_preexisting_isolated_network(cold_case, change):
    c = cold_case
    if change == "id": c.commands.network["Id"] = "0" * 64
    if change == "config": c.commands.network["Internal"] = False
    if change == "occupied": c.commands.objects["e" * 64] = {}
    with pytest.raises(GenerationUnavailable): c.bootstrap._empty()
    assert mutations(c.commands) == []


@pytest.mark.parametrize("change", ["id", "started", "image", "config"])
def test_legacy_is_inspected_and_never_restarted_or_reconfigured(cold_case, change):
    c = cold_case
    assert c.bootstrap._legacy()
    raw = c.commands.legacy["api"]
    if change == "id": raw["Id"] = "e" * 64
    if change == "started": raw["State"]["StartedAt"] = "replacement-start"
    if change == "image": raw["Image"] = "sha256:" + "e" * 64
    if change == "config": raw["Config"]["Env"] = ["OTHER=value"]
    with pytest.raises(GenerationUnavailable, match="legacy topology changed"):
        c.bootstrap._legacy()
    assert mutations(c.commands) == []


def test_empty_restore_survives_corrupt_candidate_files_without_touching_legacy(cold_case):
    c = cold_case
    empty_before, legacy_before = c.bootstrap._empty(), c.bootstrap._legacy()
    c.bootstrap._prepare()
    (c.root / "CURRENT").write_text("0" * 64)
    (c.root / "receiver-release.CURRENT").write_text("1" * 64)
    for ref in (c.intent.runtime, c.intent.environment, c.intent.startup_authority, c.intent.recipe.compose):
        Path(ref.path).write_bytes(b"CORRUPTED-CANDIDATE")
    empty_after, legacy_after = c.bootstrap._restore_empty()
    assert (empty_after, legacy_after) == (empty_before, legacy_before)
    assert read_state(c.root).status == "CLOSED"
    assert not c.commands.objects and not (c.root / "CURRENT").exists()
    removed = [args[3] for kind, args in c.commands.calls if args[:3] == ("container", "rm", "--force")]
    assert set(removed) == {raw["Id"] for raw in c.native.values()}
    assert all("vkm-core-api-1" not in args and "vkm-core-mcp-1" not in args
               for kind, args in mutations(c.commands))


@pytest.mark.parametrize("change", ["owner", "image", "project", "foreign_second"])
def test_empty_restore_unknown_objects_block_before_any_removal(cold_case, change):
    c = cold_case
    c.bootstrap._prepare()
    raw = c.commands.objects["2" * 64 if change == "foreign_second" else "1" * 64]
    if change in {"owner", "foreign_second"}: raw["Config"]["Labels"]["io.vkm.bootstrap.owner"] = "foreign"
    if change == "image": raw["Image"] = "sha256:" + "0" * 64
    if change == "project": raw["Config"]["Labels"]["com.docker.compose.project"] = "vkm-core"
    before = copy.deepcopy(c.commands.objects)
    with pytest.raises(GenerationUnavailable): c.bootstrap._restore_empty()
    assert c.commands.objects == before
    assert not any(args[:2] == ("container", "rm") for _, args in c.commands.calls)
    assert read_state(c.root).status == "CLOSED"


def test_changed_authority_or_backup_blocks_cold_prepare_before_mutation(cold_case):
    c = cold_case
    Path(c.intent.independent_backup.path).write_bytes(b"INVALID-BACKUP")
    with pytest.raises((GenerationUnavailable, ValueError)):
        c.bootstrap._prepare()
    assert mutations(c.commands) == []


@pytest.fixture
def lifecycle(cold_case, monkeypatch):
    """Source-owned lifecycle + full synthetic probes; native joins stay fake."""
    from test_bootstrap_verify import receiver
    from vkm_corpus.update import bootstrap_probes as P
    c = cold_case
    assert c.intent.scope == "SYNTHETIC"
    c.bootstrap.controller.writer_fence = c.bootstrap._writer_fence

    class SyntheticUnits:
        def __init__(self, config, *, commands, **kwargs):
            assert config.scope == "SYNTHETIC"
            self.config, self.commands, self.release = config, commands, config.releases[0]

        def rebind(self, manifest, *, gate):
            assert not (c.root / "MAINTENANCE").exists()
            assert read_state(c.root).status == "CLOSED"
            release = self.release
            self.commands.run("compose", ("--env-file", "/dev/null", "--project-name", release.project,
                "--project-directory", str(Path(release.compose.path).parent), "--file", release.compose.path,
                "up", "--detach", "--no-deps", "--no-build", "--pull", "never", "--force-recreate", "api", "mcp"))
            return self._joined_proof(manifest, gate=gate, admission_open=False)

        def _joined_proof(self, manifest, *, gate, admission_open):
            assert admission_open is False and not (c.root / "MAINTENANCE").exists()
            assert read_state(c.root).status == "CLOSED"
            assert len(c.commands.objects) == 2 and all(raw["State"]["Running"] for raw in c.commands.objects.values())
            return receiver(manifest, self.release, number=2 * c.commands.starts - 1)

    monkeypatch.setattr(B, "CoreUnitControl", SyntheticUnits)

    async def private(release):
        registrar = P.BootstrapProbeRegistrar(Path(c.config.qualification_root),
            approved_plan_sha256=c.bootstrap.plan.sha256)
        with P.ClosedBootstrapAuthority(c.intent.startup_authority, c.bootstrap.plan,
                intent=c.config.intent, writer_fd=c.bootstrap.controller._writer_fd,
                gate_fd=c.bootstrap.controller._gate_fd) as authority:
            result = await P.qualify_closed_bootstrap(c.bootstrap.plan, transport=c.shadow.transport,
                fence=c.shadow.fence, registrar=registrar, authority=authority)
        assert result["status"] == P.VERIFIED, result
        return result["receipt_sha256"]

    monkeypatch.setattr(c.bootstrap, "_private_probes", private)
    return c


def test_lifecycle_executes_both_fallbacks_then_registers_closed_only_and_idempotent_retry(lifecycle):
    c = lifecycle
    out = c.bootstrap.execute(c.intent.sha256)
    assert out["status"] == "CLOSED_BASELINE_QUALIFIED" and out["scope"] == "SYNTHETIC"
    assert out["serving_admission"] is False and out["scientific_admission"] is False
    assert read_state(c.root).status == "CLOSED" and not (c.root / "MAINTENANCE").exists()
    assert (c.root / "CURRENT").read_bytes() == (c.bootstrap.manifest.sha256 + "\n").encode("ascii")
    assert sum(kind == "compose" and args[8:] == ("start", "api") for kind, args in c.commands.calls) == 1
    assert sum(args[:3] == ("container", "rm", "--force") for _, args in c.commands.calls) == 4
    before = list(mutations(c.commands))
    repeated = c.bootstrap.execute(c.intent.sha256)
    assert repeated == out and mutations(c.commands) == before
    with pytest.raises(GenerationUnavailable, match="qualified baseline"):
        c.bootstrap.recover(c.intent.sha256)
    assert mutations(c.commands) == before


def test_lifecycle_plan_and_bad_confirmation_never_mutate_native_state(lifecycle):
    c = lifecycle
    planned = c.bootstrap.plan_run()
    assert planned["status"] == "PLANNED_CLOSED" and planned["serving_admission"] is False
    with pytest.raises(GenerationUnavailable): c.bootstrap.execute("0" * 64)
    assert mutations(c.commands) == [] and not (c.root / "bootstrap.CLOSED_BASELINE.json").exists()


@pytest.mark.parametrize("phase", ["COLD_CREATED", "COLD_PREPARED", "BOOTSTRAP_CURRENT", "CLOSED_NATIVE_STARTED",
                                  "PARTIAL_NATIVE_START"])
def test_lifecycle_interruption_returns_empty_keeps_legacy_and_never_creates_ready(lifecycle, phase):
    c = lifecycle
    def once(actual):
        if actual == phase:
            c.bootstrap._fault_hook = None
            raise RuntimeError("synthetic interrupted " + phase)
    c.bootstrap._fault_hook = once
    with pytest.raises(RuntimeError, match="synthetic interrupted"):
        c.bootstrap.execute(c.intent.sha256)
    assert not c.commands.objects and not (c.root / "CURRENT").exists()
    assert not (c.root / "bootstrap.CLOSED_BASELINE.json").exists()
    assert read_state(c.root).status == "CLOSED"
    assert c.bootstrap._legacy()
    assert all("vkm-core-api-1" not in args and "vkm-core-mcp-1" not in args for _, args in mutations(c.commands))


def test_ack_loss_after_registration_preserves_qualified_closed_pair(lifecycle):
    c = lifecycle
    def lost(phase):
        if phase == "CLOSED_BASELINE_REGISTERED":
            c.bootstrap._fault_hook = None
            raise RuntimeError("synthetic ACK lost")
    c.bootstrap._fault_hook = lost
    with pytest.raises(RuntimeError, match="ACK lost"): c.bootstrap.execute(c.intent.sha256)
    assert (c.root / "bootstrap.CLOSED_BASELINE.json").exists() and len(c.commands.objects) == 2
    assert all(raw["State"]["Running"] for raw in c.commands.objects.values())
    assert read_state(c.root).status == "CLOSED"
    mutations_before = mutations(c.commands)
    assert c.bootstrap.execute(c.intent.sha256)["status"] == "CLOSED_BASELINE_QUALIFIED"
    assert mutations(c.commands) == mutations_before


def test_fresh_recovery_uses_retained_authority_when_candidate_files_are_corrupt(cold_case):
    c = cold_case
    c.bootstrap._prepare()
    for ref in (c.intent.runtime, c.intent.environment, c.intent.startup_authority,
                c.intent.recipe.compose, c.config.probe_spec):
        Path(ref.path).write_bytes(b"CORRUPTED-CANDIDATE")
    recovered = B.IsolatedBootstrap(c.config, config_ref=c.config_ref,
        _commands=c.commands, _recovery_only=True)
    try:
        out = recovered.recover(c.intent.sha256)
        assert out["status"] == "EMPTY_RESTORED" and out["serving_admission"] is False
        assert not c.commands.objects and not (c.root / "CURRENT").exists()
        assert read_state(c.root).status == "CLOSED"
    finally:
        recovered.close()


def test_recovery_then_retry_preserves_prior_hash_chain_and_qualifies_only_new_complete_attempt(lifecycle):
    c = lifecycle
    def once(phase):
        if phase == "COLD_PREPARED":
            c.bootstrap._fault_hook = None
            raise RuntimeError("synthetic first attempt interrupted")
    c.bootstrap._fault_hook = once
    with pytest.raises(RuntimeError): c.bootstrap.execute(c.intent.sha256)
    assert c.bootstrap.recover(c.intent.sha256)["status"] == "EMPTY_RESTORED"
    old_events = c.bootstrap.controller.history(c.intent.request_id)
    old_hashes = [event["journal_sha256"] for event in old_events]
    assert c.bootstrap.execute(c.intent.sha256)["status"] == "CLOSED_BASELINE_QUALIFIED"
    new_events = c.bootstrap.controller.history(c.intent.request_id)
    assert [e["journal_sha256"] for e in new_events[:len(old_events)]] == old_hashes
    assert sum(e["phase"] == "BOOTSTRAP_ATTEMPT" for e in new_events) == 2

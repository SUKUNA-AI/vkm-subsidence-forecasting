"""CPU-only qualification of operator authority and restart failure boundaries.

No Docker command, remote endpoint, actual deployment or corpus input is used.
Native procfs observations are separately tested and never implied by the fake.
"""
import asyncio
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from vkm_corpus.update.contracts import GenerationManifest
from vkm_corpus.update.generation import GenerationUnavailable
from vkm_corpus.update.operator import (CoreOperator, CoreOperatorConfig, NativePortal, OperatorRelease,
    operator_environment, qualified_read_context)
from vkm_corpus.update.operator_units import (ComposeRelease, CoreUnitControl, UnitControlConfig,
    UnitPin, container_fingerprint, verify_native_process)
from vkm_corpus.update.receiver import (ReceiverChallenge, ReceiverIdentity, process_start_ticks,
    sign_identity, verify_identity, RECEIVER_ROUTE, McpReceiverIdentity, sign_mcp_identity, SignedReceiverIdentity)
from vkm_corpus.update.runtime import BoundFile
from vkm_evidence.contracts import canonical_bytes, record_hash

TOKEN = "operator-test-credential-" + "x" * 32
GATE = (10, 20)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = canonical_bytes(value)
    path.write_bytes(raw)
    return BoundFile(path=str(path), sha256=hashlib.sha256(raw).hexdigest())


def generation(label):
    return GenerationManifest(code_commit="a" * 40, policy_sha256="b" * 64,
        acceptance_sha256=record_hash(label), components=({"component": "DOCUMENT", "revision": label,
        "manifest_sha256": record_hash(label), "policy_sha256": "b" * 64, "built_from": {}},))


def signed_proof(manifest, release, nonce, *, instance="1" * 64, admission=True):
    from vkm_corpus.api.production import read_tool_names
    from vkm_corpus.update.deployment import components_sha256, services_sha256
    proof = ReceiverIdentity(nonce=nonce, instance=instance, process_pid=7, process_start_ticks=100,
        pid_namespace_inode=1234, generation_sha256=manifest.sha256,
        runtime_config_sha256=release.runtime_config_sha256, code_sha256=release.code_sha256,
        dependencies_sha256=release.dependencies_sha256, access_sha256=release.access_sha256,
        components_sha256=components_sha256(manifest), services_sha256=services_sha256(manifest),
        gate_device=GATE[0], gate_inode=GATE[1], admission_open=admission,
        read_contract_sha256=record_hash(sorted(read_tool_names())))
    return canonical_bytes(sign_identity(proof, TOKEN))


class FakeCommands:
    def __init__(self, containers):
        self.containers = containers
        self.calls = []
        self.after_restart = None

    def run(self, kind, argv):
        self.calls.append((kind, argv))
        if kind == "compose":
            if self.after_restart:
                self.after_restart()
            return b""
        if argv[:2] == ("image", "inspect"):
            image = "sha256:" + argv[2].split("@sha256:")[-1]
            return canonical_bytes([{"Id": image}])
        assert argv[:3] == ("inspect", "--type", "container")
        return canonical_bytes([self.containers[argv[3].split("-")[-2]]])


@pytest.fixture
def rig(tmp_path):
    authority = tmp_path / "authority"
    root = tmp_path / "runtime/served"
    root.mkdir(parents=True)
    (root / "admission.lock").touch()
    token = authority / "token"
    token.parent.mkdir()
    token.write_text(TOKEN, encoding="ascii")
    token.chmod(0o600)
    containers = {}
    for n, unit in enumerate(("api", "mcp")):
        containers[unit] = {"Id": str(n + 1) * 64, "Image": "sha256:" + str(n + 1) * 64,
            "Config": {"Labels": {"com.docker.compose.project": "vkm-core-shadow",
                "com.docker.compose.service": unit, "com.docker.compose.container-number": "1"},
                "Env": ["SYNTHETIC=true"], "Hostname": "generated"},
            "State": {"Running": True, "Paused": False, "Pid": 111 + n, "StartedAt": "synthetic-start"},
            "HostConfig": {"PidMode": ""}, "Mounts": [],
            "NetworkSettings": {"Networks": {"isolated": {}},
                "Ports": {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18000"}]}}}
        if unit == "mcp":
            containers[unit]["NetworkSettings"]["Ports"] = {"8765/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18765"}]}
    def release(label):
        env = {"VKM_API_PROFILE": "production", "VKM_UPDATE_RUNTIME_FILE": str(authority / "runtime.json")}
        compose = {"name": "vkm-core-shadow", "services": {
            "api": {"image": "synthetic-api@sha256:" + "1" * 64, "read_only": True, "cap_drop": ["ALL"],
                    "command": ["api", "serve", "--host", "0.0.0.0", "--port", "8000"], "environment": env},
                "mcp": {"image": "synthetic-mcp@sha256:" + "2" * 64, "read_only": True, "cap_drop": ["ALL"],
                    "command": ["mcp", "serve", "--kind", "read", "--host", "0.0.0.0", "--port", "8765"],
                    "environment": {"VKM_API_URL": "http://api:8000", "VKM_API_TOKEN_FILE": "/run/read",
                        "VKM_MCP_TOKEN_FILE": "/run/mcp", "VKM_DEPLOYMENT_TOKEN_FILE": "/run/deployment",
                        "VKM_DEPLOYMENT_GATE_FILE": str(root / "admission.lock")}}}}
        for spec in compose["services"].values():
            spec.update(mem_limit=2 * 1024**3, cpus=2, volumes=[
                {"type": "bind", "source": str(root), "target": str(root), "read_only": True,
                 "bind": {"create_host_path": False}},
                {"type": "bind", "source": str(root / "admission.lock"), "target": str(root / "admission.lock"),
                 "read_only": False, "bind": {"create_host_path": False}}])
        ref = write(authority / (label + ".json"), compose)
        return ComposeRelease(compose=ref, project="vkm-core-shadow", units={unit: UnitPin(**container_fingerprint(
            containers[unit], project="vkm-core-shadow", unit=unit)) for unit in containers},
            runtime_config_sha256=record_hash("runtime-" + label), code_sha256="c" * 64,
            dependencies_sha256="d" * 64, access_sha256="e" * 64, mcp_read_principal_sha256="8" * 64)
    old, new = release("old"), release("new")
    config = UnitControlConfig(scope="SYNTHETIC", docker=BoundFile(path=str(authority / "docker"), sha256="a" * 64),
        compose_binary=BoundFile(path=str(authority / "docker-compose"), sha256="a" * 64),
        docker_socket=str(tmp_path / "docker.sock"), control_root=str(root), receiver_url="http://127.0.0.1:18000",
        mcp_receiver_url="http://127.0.0.1:18765",
        operator_token_file=str(token), releases=(old, new), restart_timeout_seconds=.01)
    (root / "receiver-release.CURRENT").write_text(old.sha256)
    state = {"manifest": generation("old"), "instance": "1" * 64, "admission": True,
        "read_principal": "8" * 64}
    commands = FakeCommands(containers)
    nonces = []
    def endpoint(request):
        assert request.headers["Authorization"] == "Bearer " + TOKEN
        assert request.url.path == RECEIVER_ROUTE
        challenge = ReceiverChallenge.model_validate_json(request.content)
        nonces.append(challenge.nonce)
        release = old if state["manifest"].components[0].revision == "old" else new
        body = signed_proof(state["manifest"], release, challenge.nonce,
            instance=state["instance"], admission=state["admission"])
        if request.url.port == 18765:
            upstream = SignedReceiverIdentity.model_validate_json(body)
            upstream = sign_identity(upstream.identity.model_copy(update={
                "read_credential_sha256": "7" * 64, "read_principal_sha256": state["read_principal"]}), TOKEN)
            proof = McpReceiverIdentity(nonce=challenge.nonce, instance="3" * 64, process_pid=9,
                process_start_ticks=200, pid_namespace_inode=1234, code_sha256=release.code_sha256,
                dependencies_sha256=release.dependencies_sha256,
                read_contract_sha256=upstream.identity.read_contract_sha256, api_url="http://api:8000",
                read_credential_sha256="7" * 64, upstream=upstream,
                gate_device=GATE[0], gate_inode=GATE[1], admission_open=state["admission"])
            body = canonical_bytes(sign_mcp_identity(proof, TOKEN))
        return httpx.Response(200, content=body)
    client = httpx.Client(transport=httpx.MockTransport(endpoint))
    process_calls = []
    def process_probe(container, proof):
        process_calls.append((container["State"]["Pid"], proof.process_pid))
        if proof.pid_namespace_inode != 1234:
            raise GenerationUnavailable("synthetic wrong namespace")
    control = CoreUnitControl(config, commands=commands, http=client, process_probe=process_probe)
    yield SimpleNamespace(control=control, commands=commands, containers=containers, config=config,
        old=old, new=new, state=state, nonces=nonces, process_calls=process_calls, authority=authority)
    client.close()


def test_native_configuration_pin_retains_secrets_user_argv_limits_and_mounts(rig):
    raw = rig.containers["api"]
    for key, value in (("Env", ["CHANGED=true"]), ("User", "other"), ("Cmd", ["other"])):
        changed = copy.deepcopy(raw)
        changed["Config"][key] = value
        assert container_fingerprint(changed, project="vkm-core-shadow", unit="api") != rig.old.units["api"].model_dump()
    rig.control.prove_current(rig.state["manifest"], gate=GATE)
    rig.control.prove_current(rig.state["manifest"], gate=GATE)
    assert len(set(rig.nonces)) == 4 and len(rig.process_calls) == 6


def test_fixed_restart_has_no_build_pull_cleanup_other_service_or_implicit_env(rig):
    rig.control.prove_current(rig.state["manifest"], gate=GATE)
    rig.control.select(rig.new.sha256, fence=lambda: None)
    rig.state.update(manifest=generation("new"), admission=False)
    def restarted():
        rig.state["instance"] = "2" * 64
        for raw in rig.containers.values():
            raw["Id"] = "f" * 64
            raw["State"]["Pid"] += 10
            raw["State"]["StartedAt"] = "new-synthetic-start"
    rig.commands.after_restart = restarted
    proof = rig.control.rebind(rig.state["manifest"], gate=GATE)
    assert proof.instance == "2" * 64
    argv = next(argv for kind, argv in rig.commands.calls if kind == "compose")
    assert argv[-2:] == ("api", "mcp")
    assert argv[:2] == ("--env-file", "/dev/null")
    assert ("--no-build", "--pull", "never", "--force-recreate") == argv[-6:-2]
    assert "--no-deps" in argv and not any(x in argv for x in ("build", "prune", "neo4j", "opensearch"))


def test_wrong_endpoint_is_rejected_before_http_proof(rig):
    rig.containers["api"]["NetworkSettings"]["Ports"]["8000/tcp"][0]["HostPort"] = "28000"
    with pytest.raises(GenerationUnavailable, match="published binding"):
        rig.control.prove_current(rig.state["manifest"], gate=GATE)
    assert rig.nonces == []


def test_correct_endpoint_but_other_native_process_is_rejected(rig):
    rig.control.process_probe = lambda *_: (_ for _ in ()).throw(GenerationUnavailable("different native process"))
    with pytest.raises(GenerationUnavailable, match="different native process"):
        rig.control.prove_current(rig.state["manifest"], gate=GATE)


def test_container_replacement_during_proof_is_rejected(rig):
    rig.control.process_probe = lambda *_: rig.containers["api"]["State"].update(Pid=999)
    with pytest.raises(GenerationUnavailable, match="changed during"):
        rig.control.prove_current(rig.state["manifest"], gate=GATE)


@pytest.mark.parametrize("change", ["bare-env", "braced-env", "host-network", "build", "compatibility", "admin",
    "duplicate-kind-admin", "wrong-upstream", "write-token", "wrong-mcp-gate", "writable-state",
    "missing-gate-mount", "short-mount", "unbounded-memory", "unbounded-cpu"])
def test_unqualified_compose_is_rejected_before_side_effect(rig, change):
    raw = json.loads(Path(rig.new.compose.path).read_bytes())
    api = raw["services"]["api"]
    if change == "bare-env":
        api["environment"]["OTHER"] = "$PROFILE"
    elif change == "braced-env":
        api["environment"]["OTHER"] = "${PROFILE}"
    elif change == "host-network":
        api["network_mode"] = "host"
    elif change == "build":
        api["build"] = "."
    elif change == "compatibility":
        api["environment"]["VKM_API_PROFILE"] = "compatibility"
    elif change == "admin":
        raw["services"]["mcp"]["command"][3] = "admin"
    elif change == "duplicate-kind-admin":
        raw["services"]["mcp"]["command"].extend(["--kind", "admin"])
    elif change == "wrong-upstream":
        raw["services"]["mcp"]["environment"]["VKM_API_URL"] = "http://other-api:8000"
    elif change == "write-token":
        raw["services"]["mcp"]["environment"]["VKM_API_WRITE_TOKEN_FILE"] = "/run/write"
    elif change == "wrong-mcp-gate":
        raw["services"]["mcp"]["environment"]["VKM_DEPLOYMENT_GATE_FILE"] = "/other/gate"
    elif change == "writable-state":
        api["volumes"][0]["read_only"] = False
    elif change == "missing-gate-mount":
        api["volumes"].pop()
    elif change == "short-mount":
        api["volumes"].append("/host:/data")
    elif change == "unbounded-memory":
        api.pop("mem_limit")
    else:
        api.pop("cpus")
    unsafe = rig.new.model_copy(update={"compose": write(rig.authority / "unsafe.json", raw)})
    with pytest.raises(GenerationUnavailable):
        rig.control._compose(unsafe)
    assert rig.commands.calls == []


def test_unhealthy_corrupt_candidate_does_not_block_restoring_previous_selection(rig):
    binding = rig.control.selection_binding()
    rig.control.select(rig.new.sha256, fence=lambda: None)
    Path(rig.new.compose.path).write_text("corrupt candidate")
    rig.containers["api"]["State"]["Running"] = False
    assert rig.control.selection_binding()["release_sha256"] == rig.new.sha256
    rig.control.restore(binding, fence=lambda: None)
    assert rig.control.selection_binding() == binding
    assert rig.commands.calls == []


def test_same_old_instance_after_recreate_cannot_be_ready(rig):
    rig.control.prove_current(rig.state["manifest"], gate=GATE)
    rig.state["admission"] = False
    with pytest.raises(GenerationUnavailable, match="deadline"):
        rig.control.rebind(rig.state["manifest"], gate=GATE)


def test_current_ready_requires_open_admission_on_both_actual_receivers(rig):
    rig.state["admission"] = False
    with pytest.raises(ValueError, match="gate"):
        rig.control.prove_current(rig.state["manifest"], gate=GATE)


def test_authentic_but_different_read_context_cannot_prove_current_ready(rig):
    # HMAC, native container/PID, inventory and upstream remain valid. A second
    # legitimate API read context must not substitute for the accepted one.
    rig.state["read_principal"] = "9" * 64
    with pytest.raises(GenerationUnavailable, match="code/contract/upstream differs"):
        rig.control.prove_current(rig.state["manifest"], gate=GATE)


def test_release_read_context_must_be_the_exact_acceptance_reader_context():
    from vkm_corpus.contracts.access import AccessContext
    accepted = AccessContext(principal="reader", execution="CLOUD")
    other = AccessContext(principal="other", execution="LOCAL")
    config = SimpleNamespace(access_contexts={"reader": accepted, "other": other})
    plan = SimpleNamespace(reader_principal="reader")
    release = SimpleNamespace(mcp_read_principal_sha256=record_hash(accepted))
    assert qualified_read_context(config, plan, release)
    assert not qualified_read_context(config, plan,
        SimpleNamespace(mcp_read_principal_sha256=record_hash(other)))
    assert not qualified_read_context(config, SimpleNamespace(reader_principal="missing"), release)
    changed = accepted.model_copy(update={"execution": "LOCAL"})
    assert not qualified_read_context(SimpleNamespace(access_contexts={"reader": changed}), plan, release)


def operator_config(rig):
    units = rig.config.model_copy(update={"scope": "SHADOW_PRODUCTION"})
    empty = write(rig.authority / "placeholder.json", {})
    return CoreOperatorConfig(units=units, releases=tuple(OperatorRelease(receiver_release_sha256=r.sha256,
        environment=empty, runtime=empty, generation=empty, acceptance_plan=empty) for r in units.releases),
        candidate_release_sha256=rig.new.sha256, expected_commit="a" * 40,
        expected_code_sha256="c" * 64, expected_dependencies_sha256="d" * 64,
        access_config_sha256="e" * 64, policy=write(rig.authority / "policy.json", {}),
        authority_root=str(rig.authority), protected_roots=(str(rig.authority.parent / "originals"),),
        qualification_root=str(rig.authority.parent / "qualifications"), isolation_attestation_sha256="f" * 64)


def test_control_identity_never_loads_candidate_or_previous_artifacts(rig, monkeypatch):
    import vkm_corpus.api.production as production
    import vkm_corpus.pipeline.context as context
    operator = object.__new__(CoreOperator)
    operator.config = operator_config(rig)
    operator.barrier = SimpleNamespace(receiver_id="core-api-and-read-mcp")
    operator._fence_config = lambda: None
    operator._candidate = lambda: pytest.fail("recovery identity touched corrupt candidate")
    operator._selected = lambda: pytest.fail("recovery identity touched a runtime")
    monkeypatch.setattr(context, "code_revision", lambda **_: ("a" * 40, False))
    monkeypatch.setattr(production, "serving_code_identity", lambda: "c" * 64)
    monkeypatch.setattr(production, "serving_dependencies_identity", lambda: "d" * 64)
    profile = operator._identity()
    assert profile.policy_sha256 == operator.config.policy.sha256
    monkeypatch.setattr(production, "serving_dependencies_identity", lambda: "0" * 64)
    with pytest.raises(GenerationUnavailable, match="control identity"):
        operator._identity()


def test_durable_recovery_uses_real_control_identity_after_candidate_corruption(rig, monkeypatch, tmp_path):
    from test_deployment_lifecycle import rig as deployment_rig
    import vkm_corpus.api.production as production
    import vkm_corpus.pipeline.context as context
    (tmp_path / "durable").mkdir()
    controller, old, new, state, calls, barrier = deployment_rig(tmp_path / "durable", fault=lambda phase:
        (_ for _ in ()).throw(KeyboardInterrupt()) if phase == "APPLIED:document" else None)
    operator = object.__new__(CoreOperator)
    config = operator_config(rig)
    operator.config = config.model_copy(update={"units": config.units.model_copy(update={"scope": "SYNTHETIC"}),
        "policy": config.policy.model_copy(update={"sha256": "b" * 64})})
    operator.barrier = barrier
    operator._fence_config = lambda: None
    operator._candidate = lambda: (_ for _ in ()).throw(GenerationUnavailable("candidate is corrupt"))
    monkeypatch.setattr(context, "code_revision", lambda **_: ("a" * 40, False))
    monkeypatch.setattr(production, "serving_code_identity", lambda: "c" * 64)
    monkeypatch.setattr(production, "serving_dependencies_identity", lambda: "d" * 64)
    # Existing durable selector fixture remains explicitly SYNTHETIC. Only the
    # actual operator control-identity path is reused, with those fixture IDs.
    controller.identity_provider = lambda: operator._identity().model_copy(update={"adapter_ids": tuple(controller.adapters)})
    plan = controller.plan(new, "crashed")
    with pytest.raises(KeyboardInterrupt):
        controller.switch(new, "crashed", plan["plan_sha256"])
    controller.fault = None
    recovery = controller.recovery_plan("crashed", mode="RESTORE_PREVIOUS")
    result = controller.recover("crashed", recovery["recovery_sha256"])
    assert result["status"] == "RESTORED" and result["generation_sha256"] == old.sha256
    assert state == {c.component: c.model_dump(mode="json") for c in old.components}
    assert any(call[0] == "restore" for call in calls)


def test_wrong_local_image_resolution_blocks_before_compose_up(rig):
    original = rig.commands.run
    def wrong_image(kind, argv):
        if argv[:2] == ("image", "inspect"):
            return canonical_bytes([{"Id": "sha256:" + "f" * 64}])
        return original(kind, argv)
    rig.commands.run = wrong_image
    with pytest.raises(GenerationUnavailable, match="locally available"):
        rig.control.rebind(rig.state["manifest"], gate=GATE)
    assert not any(kind == "compose" for kind, _ in rig.commands.calls)


def test_operator_authority_is_disjoint_and_rejects_unknown_callbacks(rig):
    raw = operator_config(rig).model_dump()
    for key, value in (("authority_root", rig.config.control_root), ("callback", "arbitrary.module.run")):
        changed = {**raw, key: value}
        with pytest.raises(ValueError):
            CoreOperatorConfig.model_validate(changed)


def test_authority_parent_traversal_and_backup_writes_in_originals_are_rejected(rig):
    raw = operator_config(rig).model_dump()
    raw["policy"]["path"] = str(rig.authority / ".." / "originals" / "policy.json")
    with pytest.raises(ValueError, match="traversal"):
        CoreOperatorConfig.model_validate(raw)
    raw = operator_config(rig).model_dump()
    raw["qualification_root"] = raw["protected_roots"][0]
    with pytest.raises(ValueError, match="disjoint"):
        CoreOperatorConfig.model_validate(raw)


def test_profile_excludes_generation_acceptance_hash_cycle_but_pins_control(rig):
    config = operator_config(rig)
    changed = config.model_copy(update={"releases": tuple(r.model_copy(update={
        "generation": r.generation.model_copy(update={"sha256": "9" * 64}),
        "acceptance_plan": r.acceptance_plan.model_copy(update={"sha256": "8" * 64})}) for r in config.releases)})
    assert changed.deployment_profile_sha256 == config.deployment_profile_sha256
    assert config.model_copy(update={"access_config_sha256": "7" * 64}).deployment_profile_sha256 != config.deployment_profile_sha256


def test_untrusted_environment_cannot_set_shell_code_python_path_or_plain_secret(rig):
    for key in ("PATH", "PYTHONPATH", "HOME", "VKM_API_TOKEN", "CALLBACK"):
        ref = write(rig.authority / "env.json", {"VKM_API_PROFILE": "production", "VKM_DATA_ROLE": "canonical", key: "secret"})
        with pytest.raises(GenerationUnavailable):
            operator_environment(ref)


def test_private_candidate_does_not_inherit_operator_process_retrieval_flags(monkeypatch):
    from vkm_corpus.api.backends import HybridBackend
    monkeypatch.setenv("VKM_HYBRID_LATE_DEFAULT", "1")
    monkeypatch.setenv("VKM_HYBRID_VISUAL_ROUTE", "1")
    monkeypatch.setenv("VKM_HYBRID_GRAPH", "invalid-operator-flag")
    selected = HybridBackend(SimpleNamespace(), search=SimpleNamespace(), embed=SimpleNamespace(),
        environ={"VKM_HYBRID_LATE_DEFAULT": "0", "VKM_HYBRID_VISUAL_ROUTE": "0"})
    assert selected.late_default is False and selected.visual.enabled is False and selected.graph_error is None


def test_native_portal_uses_one_owned_loop_and_cancels_on_timeout():
    portal = NativePortal()
    stopped = []
    async def first():
        return id(asyncio.get_running_loop())
    async def hanging():
        try:
            await asyncio.sleep(60)
        finally:
            stopped.append(True)
    try:
        assert portal.call(first()) == portal.call(first()) == id(portal.loop)
        with pytest.raises(TimeoutError):
            portal.call(hanging(), timeout=.02)
    finally:
        portal.close()
    assert stopped and not portal.thread.is_alive() and portal.loop.is_closed()


def test_proc_stat_parser_preserves_complex_comm_and_rejects_wrong_identity():
    raw = "7 (space (and) parentheses) S " + "0 " * 18 + "123 " + "0 " * 10
    assert process_start_ticks(raw, pid=7) == 123
    with pytest.raises(ValueError):
        process_start_ticks(raw, pid=8)


def test_native_process_join_rejects_shared_host_namespace_even_without_proc_access(rig):
    raw = copy.deepcopy(rig.containers["api"])
    raw["HostConfig"]["PidMode"] = "host"
    with pytest.raises(GenerationUnavailable, match="isolated native"):
        verify_native_process(raw, SimpleNamespace(pid_namespace_inode=1234))


def test_cli_is_closed_and_errors_never_echo_paths_secrets_or_commands(capsys):
    from vkm_corpus.cli import build_parser
    args = build_parser(["deployment", "status"]).parse_args(["deployment", "status",
        "--operator-config", "sensitive-private-secret-path", "--config-sha256", "a" * 64])
    assert args.func(args) == 2
    output = capsys.readouterr().out
    assert json.loads(output)["status"] == "BLOCKED" and "sensitive" not in output
    with pytest.raises(SystemExit):
        build_parser(["deployment", "shell"]).parse_args(["deployment", "shell"])


def test_closed_drill_callback_binds_original_startup_authority(rig):
    from vkm_corpus.update.deployment import PreviousAdmission
    previous = generation("synthetic-closed-previous")
    path = rig.authority / "previous-generation.json"
    raw = canonical_bytes(previous)
    path.write_bytes(raw)
    path.chmod(0o600)
    ref = BoundFile(path=str(path), sha256=hashlib.sha256(raw).hexdigest())
    operator = object.__new__(CoreOperator)
    operator.config = SimpleNamespace(releases=(SimpleNamespace(generation=ref),))
    policy = PreviousAdmission(mode="CLOSED_BASELINE", qualification_sha256="1" * 64,
                              closed_owner_key="2" * 64)
    operator._previous_admission = lambda manifest: policy if manifest == previous else None
    proof = {"previous_generation_sha256": ref.sha256, "startup_authority_sha256": previous.acceptance_sha256}
    assert operator._closed_drill_previous(proof) == policy
    proof["startup_authority_sha256"] = "3" * 64
    with pytest.raises(GenerationUnavailable, match="another startup authority"):
        operator._closed_drill_previous(proof)

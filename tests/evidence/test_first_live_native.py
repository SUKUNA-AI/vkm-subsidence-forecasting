"""Adversarial native adapter seams; no Docker, network or model is executed."""
import asyncio
from types import SimpleNamespace as NS

import pytest

from vkm_corpus.update.first_live import FirstLiveError, BootQualificationUnavailable
from vkm_corpus.update.first_live_native import NativeFirstLiveAdapters, require_sealed_legacy_ingress
from vkm_corpus.update.contracts import ComponentIdentity, ServiceIdentity
from vkm_evidence.contracts import record_hash


@pytest.mark.parametrize("scope", ["FIRST_LIVE_PRODUCTION", "SYNTHETIC", None])
def test_native_constructor_blocks_before_clients_threads_or_host_effects(monkeypatch, scope):
    from vkm_corpus.update import first_live_native as N
    from vkm_corpus.update.first_live import FirstLiveNotReady
    def forbidden(*args, **kwargs):
        pytest.fail("unqualified production must not construct native dependencies")
    for name in ("CoreUnitControl", "NativeFrontdoor", "NativePortal"):
        monkeypatch.setattr(N, name, forbidden)
    with pytest.raises(FirstLiveNotReady):
        N.NativeFirstLiveAdapters(NS(scope=scope))


@pytest.mark.parametrize("method", ["_park_legacy", "_remove_owned", "candidate_start", "legacy_restore"])
@pytest.mark.parametrize("scope", ["FIRST_LIVE_PRODUCTION", None])
def test_every_native_container_mutator_blocks_before_read_or_effect(method, scope):
    from vkm_corpus.update.first_live import FirstLiveNotReady
    adapter = object.__new__(NativeFirstLiveAdapters)
    adapter.intent = NS(scope=scope)
    # No units, commands, files or network exist: even reading them is a failure.
    with pytest.raises(FirstLiveNotReady):
        getattr(adapter, method)()


def test_native_create_has_no_compose_convergence_even_for_synthetic_adapter():
    from vkm_corpus.update.first_live import FirstLiveNotReady
    adapter = object.__new__(NativeFirstLiveAdapters)
    with pytest.raises(FirstLiveNotReady):
        adapter._create(object(), object())


@pytest.mark.parametrize("bindings", [
    {"8766/tcp": [{"HostIp": "0.0.0.0", "HostPort": "18766"}]},
    {"9999/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8766"}]},
    {"8766/udp": [{"HostIp": "::", "HostPort": "8766"}]}, {}, None])
def test_unsealed_actual_legacy_admin_or_protocol_blocks_before_restart(bindings):
    with pytest.raises(FirstLiveError):
        require_sealed_legacy_ingress({"HostConfig": {"NetworkMode": "vkm_net", "PortBindings": bindings}}, (8000, 8765, 8766))


def test_legacy_both_family_wildcard_is_covered_only_by_complete_port_seal():
    value = {"HostConfig": {"NetworkMode": "vkm_net", "PortBindings": {
        "8766/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8766"}, {"HostIp": "::", "HostPort": "8766"}]}}}
    assert require_sealed_legacy_ingress(value, (8000, 8765, 8766))


def shared_fixture():
    component = ComponentIdentity(component="DOCUMENT", revision="snap", manifest_sha256="a" * 64,
        policy_sha256="b" * 64, built_from={})
    service = ServiceIdentity(service="CONTROL", **dict.fromkeys(("instance_sha256", "code_sha256", "dependencies_sha256",
        "config_sha256", "endpoint_sha256", "runtime_sha256"), "c" * 64))
    actual, services = {"DOCUMENT": component.model_dump(mode="json")}, {"CONTROL": service}
    class Lease:
        changed = False
        def check(self):
            if self.changed: raise FirstLiveError("native bytes changed")
    class Native:
        async def observe_services(self): return services
    adapter = object.__new__(NativeFirstLiveAdapters)
    adapter.intent = NS(legacy=NS(observer=NS(components=(component,), services=(service,)),
        shared_services_sha256=record_hash([service.model_dump(mode="json")])))
    adapter.retained = NS(components=lambda: actual, native=Native())
    adapter.retained_leases = [Lease()]
    adapter.portal = NS(call=asyncio.run)
    adapter.candidate = lambda: (_ for _ in ()).throw(AssertionError("candidate must not be loaded"))
    return adapter, actual, services


def test_retained_native_observer_does_not_read_candidate_to_verify_shared_state():
    adapter, _, _ = shared_fixture()
    assert len(adapter.retained_shared()) == 64


@pytest.mark.parametrize("drift", ["component", "service", "file", "incomplete"])
def test_retained_shared_drift_is_never_a_successful_receiver_only_restore(drift):
    adapter, actual, services = shared_fixture()
    if drift == "component": actual["DOCUMENT"]["manifest_sha256"] = "d" * 64
    elif drift == "service": services["CONTROL"] = services["CONTROL"].model_copy(update={"instance_sha256": "d" * 64})
    elif drift == "file": adapter.retained_leases[0].changed = True
    else: services.clear()
    with pytest.raises(FirstLiveError): adapter.retained_shared()


def test_native_boot_gate_is_called_before_docker_or_shared_observer(monkeypatch):
    from test_first_live_frontdoor import boot_fixture
    from vkm_corpus.update import first_live_native as N, frontdoor as F
    from vkm_corpus.pipeline import context
    reg, _, _, _ = boot_fixture()
    profile = NS()
    adapter = object.__new__(NativeFirstLiveAdapters)
    adapter.intent = NS(operator_code_sha256=reg.code_sha256, operator_dependencies_sha256=reg.dependencies_sha256,
        operator_commit="e" * 40, boot_registration=NS(path="reg"), boot_checkpoint=NS(path="checkpoint"), frontdoor=profile)
    monkeypatch.setattr(N, "operator_identity", lambda: (reg.code_sha256, reg.dependencies_sha256))
    monkeypatch.setattr(context, "code_revision", lambda **kw: ("e" * 40, False))
    monkeypatch.setattr(N, "bound_json", lambda ref: reg.model_dump(mode="json") if ref.path == "reg" else {})
    monkeypatch.setattr(N.FrontdoorProfile, "model_validate", lambda value: profile)
    calls = []
    def native_gate(*args):
        calls.append(args)
        raise ValueError("no actual reboot")
    monkeypatch.setattr(F, "verify_boot_ordering", native_gate)
    with pytest.raises(BootQualificationUnavailable): adapter.fence()
    assert len(calls) == 1  # missing units/legacy fields could not be touched


def test_recovery_native_fence_does_not_require_candidate_image(monkeypatch):
    from test_first_live_frontdoor import boot_fixture
    from vkm_corpus.update import first_live_native as N, frontdoor as F
    from vkm_corpus.pipeline import context
    from vkm_corpus.update.operator_units import UnitPin
    from vkm_evidence.contracts import canonical_bytes
    reg, _, _, _ = boot_fixture()
    profile, calls = NS(), []
    adapter = object.__new__(NativeFirstLiveAdapters)
    legacy = UnitPin(image_id="sha256:" + "a" * 64, config_sha256="b" * 64)
    candidate = UnitPin(image_id="sha256:" + "d" * 64, config_sha256="b" * 64)
    adapter.intent = NS(operator_code_sha256=reg.code_sha256, operator_dependencies_sha256=reg.dependencies_sha256,
        operator_commit="e" * 40, boot_registration=NS(path="reg"), boot_checkpoint=NS(path="checkpoint"), frontdoor=profile,
        legacy=NS(selectors=(), units={"api": legacy, "mcp": legacy}),
        candidate_release=NS(units={"api": candidate, "mcp": candidate}))
    monkeypatch.setattr(N, "operator_identity", lambda: (reg.code_sha256, reg.dependencies_sha256))
    monkeypatch.setattr(context, "code_revision", lambda **kw: ("e" * 40, False))
    monkeypatch.setattr(N, "bound_json", lambda ref: reg.model_dump(mode="json") if ref.path == "reg" else {})
    monkeypatch.setattr(N.FrontdoorProfile, "model_validate", lambda value: profile)
    monkeypatch.setattr(F, "verify_boot_ordering", lambda *a: None)
    monkeypatch.setattr(N, "verify_legacy_files", lambda *a: None)
    def run(tool, argv):
        calls.append(argv)
        if argv[-1] == candidate.image_id: raise FirstLiveError("candidate image unavailable")
        return canonical_bytes([{"Id": legacy.image_id}])
    adapter.units = NS(commands=NS(run=run))
    adapter._networks = lambda: None
    adapter.retained_shared = lambda: None
    adapter.fence(recovery_only=True)
    assert calls == [("image", "inspect", legacy.image_id)]
    with pytest.raises(FirstLiveError, match="candidate image"):
        adapter.fence()


@pytest.mark.parametrize("wrong", [False, True])
def test_partial_start_is_observed_natively_instead_of_echoing_start_command(wrong):
    from vkm_corpus.update.operator_units import UnitPin
    pin = UnitPin(image_id="sha256:" + "a" * 64, config_sha256="b" * 64)
    adapter = object.__new__(NativeFirstLiveAdapters)
    adapter.intent = NS(scope="SYNTHETIC", candidate_release=NS(compose="bound", units={"api": pin, "mcp": pin}))
    adapter.units = NS(_compose=lambda _: None, commands=NS(run=lambda *a: b""))
    calls, started, order = [], [], []
    adapter._remove_owned = lambda: None
    adapter._park_legacy = lambda: order.append("park")
    adapter._create = lambda *a: {"api": "api-id", "mcp": "mcp-id"}
    # Fixed Engine-create seam: bound and preflighted before parking; start by owned unit.
    adapter.engine = NS(start=started.append, expected_pin=lambda unit: pin.model_dump(mode="json"),
                        preflight=lambda: order.append("preflight"))
    adapter._bound_engine = lambda: adapter.engine
    def inspect(unit, *, stopped=False):
        calls.append((unit, stopped))
        return {"Id": "wrong" if wrong else unit + "-id"}, pin.model_dump(mode="json")
    adapter._inspect = inspect
    if wrong:
        with pytest.raises(FirstLiveError, match="partial native"):
            adapter.candidate_start(partial=True)
    else:
        assert adapter.candidate_start(partial=True) == {"started": {"api": "api-id"}, "stopped": {"mcp": "mcp-id"}}
        assert calls == [("api", False), ("mcp", True)]
        assert started == ["api"] and order == ["preflight", "park"]


@pytest.mark.parametrize("outcome", ["success", "overflow", "failure", "timeout", "interruption"])
def test_native_command_is_bounded_and_keeps_the_held_executable_fd(monkeypatch, tmp_path, outcome):
    from test_first_live_frontdoor import profile
    from vkm_corpus.update import frontdoor as F
    from vkm_corpus.update.generation import GenerationUnavailable
    killed, calls = [], []
    class Process:
        pid = 19876
        count = 0
        def wait(self, timeout):
            self.count += 1
            if self.count == 1:
                if outcome == "timeout": raise F.subprocess.TimeoutExpired("hidden", timeout)
                if outcome == "interruption": raise KeyboardInterrupt()
            return 1 if outcome == "failure" else 0
    def spawn(argv, **kwargs):
        calls.append((argv, kwargs))
        assert kwargs["pass_fds"] == (17,) and argv[-2:] == ["/proc/self/fd/17", "--fixed"]
        kwargs["stdout"].write(b"x" * (F.MAX_NATIVE + 1 if outcome == "overflow" else 4))
        return Process()
    monkeypatch.setattr(F.subprocess, "Popen", spawn)
    monkeypatch.setattr(F.os, "killpg", lambda pid, sig: killed.append(pid), raising=False)
    monkeypatch.setattr(F.signal, "SIGKILL", 9, raising=False)  # fake Linux transport on Windows too
    watch = NS(check=lambda: None)
    call = lambda: F.FixedNft(profile(tmp_path))._execute("read", None, ["/proc/self/fd/17", "--fixed"], "fixed-child", 17, watch)
    if outcome == "success": assert call() == b"xxxx"
    elif outcome == "interruption":
        with pytest.raises(KeyboardInterrupt): call()
    else:
        with pytest.raises(GenerationUnavailable): call()
    assert killed == ([19876] if outcome in {"timeout", "interruption"} else [])
    assert len(calls) == 1


def parked_fixture(tmp_path):
    import copy
    from test_engine_create import Clock, FakeDaemon, INTENT_SHA, make_plan
    from vkm_corpus.update.engine_create import ENGINE_SOCKET, EngineCreateAdapter, engine_journal_path
    from vkm_corpus.update.operator_units import UnitPin
    from vkm_corpus.update.generation import GenerationUnavailable
    from vkm_evidence.contracts import canonical_bytes
    legacy_pin = UnitPin(image_id="sha256:" + "a" * 64, config_sha256="b" * 64)
    candidate_pin = legacy_pin.model_copy(update={"config_sha256": "c" * 64})
    ids = {"api": "1" * 64, "mcp": "2" * 64}
    objects = {identity: {"Id": identity, "Name": "/vkm-core-" + unit + "-1", "State": {"Running": True},
        "pin": legacy_pin, "unique_layer": "DO_NOT_LOSE_" + unit} for unit, identity in ids.items()}
    calls, events = [], []
    state = NS(fault=None)
    # Candidates exist only through the fixed Engine adapter (a fake daemon over the
    # same native objects) and are owned only through its durable effect journal.
    root = tmp_path / "runtime" / "served"
    root.mkdir(parents=True)
    (root / "admission.lock").touch()
    (root.parent / "data").mkdir()
    daemon, plan = FakeDaemon(), make_plan(root)
    daemon.containers = objects
    adapter = object.__new__(NativeFirstLiveAdapters)
    adapter.intent = NS(scope="SYNTHETIC", request_id="preserve-legacy", legacy=NS(units=dict.fromkeys(ids, legacy_pin), original_container_ids=ids),
        candidate_release=NS(units=dict.fromkeys(ids, candidate_pin)), sha256=INTENT_SHA, engine_create=plan,
        units=NS(docker_socket=ENGINE_SOCKET, control_root=str(root)))
    adapter.engine = EngineCreateAdapter(plan, journal_path=engine_journal_path(str(root), INTENT_SHA),
        intent_sha256=INTENT_SHA, preserved_ids=ids.values(), transport=daemon.transport(), clock=Clock())
    def inspect(unit, *, stopped=False, reference=None):
        selected = reference or "/vkm-core-" + unit + "-1"
        obj = next((v for k, v in objects.items() if k == selected or v["Name"] == selected), None)
        if obj is None or obj["State"]["Running"] is stopped:
            raise GenerationUnavailable("not the required native state")
        return copy.deepcopy(obj), obj["pin"].model_dump(mode="json")
    def run(tool, argv):
        calls.append(argv)
        if argv[0] == "info":  # the same pinned daemon as the Engine adapter
            return canonical_bytes(daemon.info["ID"])
        if argv[:2] == ("container", "ls"):
            name = argv[4].removeprefix("name=^").removesuffix("$")
            found = [v["Id"] for v in objects.values() if v["Name"] == name]
            return b"" if not found else canonical_bytes(found[0])
        if argv[0] == "stop": objects[argv[-1]]["State"]["Running"] = False
        elif argv[0] == "start": objects[argv[-1]]["State"]["Running"] = True
        elif argv[0] == "rename":
            if any(v["Name"] == "/" + argv[2] for v in objects.values()): raise FirstLiveError("occupied")
            objects[argv[1]]["Name"] = "/" + argv[2]
        elif argv[0] == "rm":
            assert argv[1] not in ids.values(), "original writable layer must never be removed"
            del objects[argv[1]]
        else: raise AssertionError(argv)
        if state.fault == argv[0]:
            raise KeyboardInterrupt()  # process death after actual synthetic effect
        return b""
    adapter._inspect = inspect
    adapter.units = NS(commands=NS(run=run))
    adapter.fence = lambda **kw: None
    adapter.event = lambda phase, **detail: events.append((phase, detail))
    adapter.legacy_observe = lambda **kw: {u: adapter._legacy_id(u)["Id"] for u in ids}
    adapter._create = lambda *a: (_ for _ in ()).throw(AssertionError("legacy must not be recreated"))
    return adapter, objects, ids, calls, state, candidate_pin, daemon


def test_same_original_container_ids_and_unique_writable_layers_survive_fallback(tmp_path):
    a, objects, ids, calls, _, candidate_pin, daemon = parked_fixture(tmp_path)
    a._park_legacy()
    assert all(not objects[i]["State"]["Running"] for i in ids.values())
    candidates = {unit: a.engine.create(unit) for unit in ids}
    for unit in ids:
        a.engine.start(unit)
    assert all(objects[i]["Name"] == "/vkm-core-" + u + "-1" and objects[i]["State"]["Running"]
               for u, i in candidates.items())
    assert a.legacy_restore() == ids
    assert set(objects) == set(ids.values())
    assert all(objects[i]["State"]["Running"] and objects[i]["unique_layer"] == "DO_NOT_LOSE_" + u for u, i in ids.items())
    assert {cmd[1] for cmd in calls if cmd[0] == "rm"}.isdisjoint(ids.values())
    assert sorted(daemon.deleted) == sorted(candidates.values()) and set(daemon.deleted).isdisjoint(ids.values())


@pytest.mark.parametrize("fault", ["stop", "rename"])
def test_interrupted_parking_restores_originals_without_candidate_artifacts(tmp_path, fault):
    a, objects, ids, calls, state, _, daemon = parked_fixture(tmp_path)
    state.fault = fault
    with pytest.raises(KeyboardInterrupt): a._park_legacy()
    state.fault = None
    assert a.legacy_restore() == ids
    assert all(objects[i]["State"]["Running"] for i in ids.values())
    assert not any(cmd[0] == "rm" for cmd in calls) and daemon.mutations() == []


def test_foreign_original_name_is_not_overwritten_or_removed_for_fallback(tmp_path):
    a, objects, ids, calls, _, pin, daemon = parked_fixture(tmp_path)
    a._park_legacy()
    objects["9" * 64] = {"Id": "9" * 64, "Name": "/vkm-core-api-1", "State": {"Running": True},
        "pin": pin.model_copy(update={"config_sha256": "f" * 64})}
    with pytest.raises(FirstLiveError, match="unknown native"):
        a.legacy_restore()
    assert all(i in objects for i in ids.values()) and not any(cmd[0] == "rm" for cmd in calls)
    assert "9" * 64 in objects and daemon.mutations() == []


def test_missing_original_layer_blocks_instead_of_silently_recreating_from_image(tmp_path):
    a, objects, ids, calls, _, _, daemon = parked_fixture(tmp_path)
    a._park_legacy()
    del objects[ids["api"]]
    with pytest.raises((ValueError, RuntimeError)): a.legacy_restore()
    assert not any(cmd[0] in {"rm", "start"} for cmd in calls) and daemon.mutations() == []

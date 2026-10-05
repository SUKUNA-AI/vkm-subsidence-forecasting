"""Fixed Docker Engine-create adapter for the first-LIVE receivers.

Importing this module performs no operation. Only a code-built allowlist of
Engine API (method, path-template) pairs exists: version/info, pinned image and
network inspect, create of an owned receiver name, container inspect, and
start/stop/remove of journal-owned container IDs. There is no pull, build,
exec, Compose call or generic request executor, and nothing is read from an
incoming manifest except the typed, operator-approved ``EngineCreatePlan``.

Ownership of a candidate container comes only from the attempt's durable
write-ahead effect journal (exact container IDs). Labels never grant
ownership by themselves: an occupant of an owned name is attributed to this
journal only when a recorded CREATE_INTENT of the same attempt/unit/profile
precedes it. Created containers never carry ``com.docker.compose.*`` labels,
so Compose cannot adopt or converge them.

Env values (which may be secrets) are never part of a profile, intent, journal,
receipt or exception: a profile carries Env NAMES and an authority-root
``BoundFile``; values are read and SHA-checked only at create time, and the
native contract compares their SHA-256 digests.

Synthetic fake transports exercise this code in tests. No actual Engine
lifecycle qualification is claimed by this module (Gate L1 is NOT_RUN).
"""
from __future__ import annotations

from datetime import datetime
import hashlib
from ipaddress import ip_address
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import time
from typing import Annotated, Callable, Literal
from urllib.parse import urlencode

from pydantic import ConfigDict, Field, model_validator

from vkm_corpus.parquet.atomic import _fsync_dir
from vkm_corpus.update.runtime import BoundFile
from vkm_evidence.contracts import Sha256, StrictModel, canonical_bytes, record_hash

ENGINE_SOCKET = "/var/run/docker.sock"
UNITS = ("api", "mcp")
LABEL_PREFIX = "org.vkm.first-live."
LABEL_ATTEMPT = LABEL_PREFIX + "attempt"
LABEL_PROFILE = LABEL_PREFIX + "profile-sha256"
LABEL_UNIT = LABEL_PREFIX + "unit"
COMPOSE_LABEL_PREFIX = "com.docker.compose."
JOURNAL_SCHEMA = "vkm-first-live-engine-effect/1"
MAX_JOURNAL_BYTES = 4 * 1024 * 1024
MAX_JOURNAL_RECORDS = 4096
MAX_CREATE_INTENTS = 3
CREATED_TOLERANCE_SECONDS = 1.0
IMAGE_DEFAULT = "IMAGE_DEFAULT"
DIFFERS_FROM_IMAGE = "DIFFERS_FROM_IMAGE"
DEFAULT_OR_STRICTER = "DEFAULT_OR_STRICTER"
WEAKENED = "WEAKENED"
# Documented Engine defaults; inspect lists may only be supersets of these.
BASELINE_MASKED_PATHS = frozenset({"/proc/asound", "/proc/acpi", "/proc/kcore", "/proc/keys", "/proc/latency_stats",
    "/proc/timer_list", "/proc/timer_stats", "/proc/sched_debug", "/proc/scsi", "/sys/firmware"})
BASELINE_READONLY_PATHS = frozenset({"/proc/bus", "/proc/fs", "/proc/irq", "/proc/sys", "/proc/sysrq-trigger"})
# Receivers bind unprivileged ports; added capabilities are refused unless reviewed here.
APPROVED_CAP_ADD = frozenset({"NET_BIND_SERVICE"})
# Host paths a receiver must never see (the Engine API itself and kernel/system trees).
FORBIDDEN_HOST_TREES = ("/proc", "/sys", "/dev", "/boot", "/run/docker", "/var/run/docker", "/run/containerd",
                        "/var/run/containerd", "/var/lib/containerd")
ENGINE_SOCKETS = ("/var/run/docker.sock", "/run/docker.sock", "/run/containerd/containerd.sock")
FORBIDDEN_TARGET_TREES = ("/proc", "/sys", "/dev")
# Reviewed security options only (an allowlist): anything else could weaken daemon defaults.
APPROVED_SECURITY_OPT = frozenset({"no-new-privileges", "no-new-privileges:true", "no-new-privileges=true",
                                   "apparmor=docker-default"})

HEX64 = re.compile(r"[0-9a-f]{64}")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
OWNED_NAME = re.compile(r"vkm-core-(?:api|mcp)-1")
API_VERSION = r"1\.(?:4[1-9]|5[0-9])"


class EngineCreateError(ValueError):
    """Fail closed. Messages are fixed text without profile values or daemon payloads."""


class EngineRequestRefused(EngineCreateError):
    """A request outside the fixed allowlist or with an invalid parameter."""


class EngineUnavailable(EngineCreateError):
    """Daemon incompatible or unpinned, unreachable for a read, or an unexpected answer."""


class EngineOutcomeUnknown(EngineCreateError):
    """Transport lost or deadline exceeded: the effect of a mutating request is unknown."""


class EngineContractDrift(EngineCreateError):
    """Native state differs from the approved profile."""


class EngineOwnershipError(EngineCreateError):
    """The object is not attributable to this journal attempt; it is never touched."""


class EngineCreateAmbiguous(EngineCreateError):
    """A create outcome is unresolved; never recreate blindly."""


class EngineJournalError(EngineCreateError):
    """The durable effect journal is absent, foreign, inconsistent or torn."""


def _posix_absolute(value):
    path = PurePosixPath(value)
    return (isinstance(value, str) and value.startswith("/") and path.as_posix() == value
            and ".." not in path.parts and "\x00" not in value)


def _posix_under(path: str, tree: str) -> bool:
    return PurePosixPath(path) == PurePosixPath(tree) or PurePosixPath(path).is_relative_to(PurePosixPath(tree))


def native_overlap(left, right) -> bool:
    """True when one native path equals or contains the other."""
    a, b = Path(left), Path(right)
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def mount_source_problem(source: str) -> str | None:
    """Native check of an approved bind source on the Engine host (not lexical only).

    The source must exist, contain no symlink component, resolve to itself on
    POSIX, and must not be a socket, device or FIFO; its resolved path must not
    be a forbidden tree. Returns a fixed reason or None.
    """
    path = Path(source)
    try:
        if any(p.is_symlink() for p in (path, *path.parents)):
            return "indirect"
        if os.name == "posix" and os.path.realpath(source) != source:
            return "indirect"
        mode = os.stat(source).st_mode
    except OSError:
        return "unavailable"
    if stat.S_ISSOCK(mode) or stat.S_ISCHR(mode) or stat.S_ISBLK(mode) or stat.S_ISFIFO(mode):
        return "special"
    if os.name == "posix" and forbidden_host_path(os.path.realpath(source)):
        return "forbidden"
    return None


def forbidden_host_path(source: str) -> bool:
    """Engine sockets (or a tree containing them), '/', kernel/system trees and any socket file."""
    if not source.startswith("/"):
        return False
    if source == "/" or PurePosixPath(source).name.endswith(".sock"):
        return True
    return (any(_posix_under(source, tree) for tree in FORBIDDEN_HOST_TREES)
            or any(_posix_under(sock, source) for sock in ENGINE_SOCKETS))


Arg = Annotated[str, Field(max_length=4096, pattern=r"^[^\x00]*$")]
EnvName = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")]
LabelKey = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}$")]
LabelValue = Annotated[str, Field(max_length=4096, pattern=r"^[^\x00]*$")]
TmpfsTarget = Annotated[str, Field(min_length=2, max_length=4096)]
TmpfsOptions = Annotated[str, Field(pattern=r"^[a-z0-9=,_.-]{0,256}$")]
Capability = Annotated[str, Field(pattern=r"^(?:ALL|[A-Z][A-Z0-9_]{1,40})$")]
LogKey = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,63}$")]
LogValue = Annotated[str, Field(max_length=256, pattern=r"^[^\x00]*$")]


class EngineMount(StrictModel):
    type: Literal["bind"] = "bind"
    source: str = Field(min_length=1, max_length=4096)
    target: str = Field(min_length=2, max_length=4096)
    read_only: bool

    @model_validator(mode="after")
    def _exact(self):
        # Host paths follow the codebase's native-path rule; container targets are Linux paths.
        if (not Path(self.source).is_absolute() or ".." in Path(self.source).parts or "\x00" in self.source
                or not _posix_absolute(self.target) or self.target == "/"):
            raise ValueError("bind mounts require normalized absolute host and container paths")
        if forbidden_host_path(self.source) or any(_posix_under(self.target, t) for t in FORBIDDEN_TARGET_TREES):
            raise ValueError("bind mount exposes the Engine socket or a kernel/system tree")
        return self


class EnginePortBinding(StrictModel):
    container_port: int = Field(ge=1, le=65535)
    protocol: Literal["tcp"] = "tcp"
    host_ip: str = Field(min_length=2, max_length=45)
    host_port: int = Field(ge=1, le=65535)
    unspecified_host_ip_approved: bool = False

    @model_validator(mode="after")
    def _explicit(self):
        value = ip_address(self.host_ip)
        if str(value) != self.host_ip or "%" in self.host_ip:
            raise ValueError("explicit canonical host IP required")
        if value.is_unspecified != self.unspecified_host_ip_approved:
            raise ValueError("an unspecified host IP requires the explicit approval field, and only it")
        return self


class EngineRestartPolicy(StrictModel):
    # Receivers are never restarted by the daemon: a restarted process is not the
    # qualified one, and a crash-looping candidate must stay stoppable for rollback.
    name: Literal["no"] = "no"
    maximum_retry_count: Literal[0] = 0


class EngineLogConfig(StrictModel):
    type: Literal["json-file", "local", "journald", "none"]
    config: dict[LogKey, LogValue] = Field(default_factory=dict, max_length=16)


class EngineNetworkAttachment(StrictModel):
    """An existing external network; it is never created or removed here."""
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    network_id: Sha256
    config_sha256: Sha256
    aliases: tuple[Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,62}$")], ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def _unique(self):
        if len(set(self.aliases)) != len(self.aliases):
            raise ValueError("duplicate network alias")
        return self


class EngineCreateProfile(StrictModel):
    """Exact approved Config / HostConfig / NetworkingConfig subset of one receiver.

    The create body is built from this profile only, never from an inspect
    document. Env values are NOT part of the profile: ``env_names`` plus the
    SHA-pinned authority-root ``environment`` file are resolved at create time.
    """
    model_config = ConfigDict(hide_input_in_errors=True)

    schema_version: Literal["vkm-engine-create-profile/2"] = "vkm-engine-create-profile/2"
    unit: Literal["api", "mcp"]
    name: str = Field(pattern=r"^vkm-core-(?:api|mcp)-1$")
    image: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    cmd: tuple[Arg, ...] = Field(min_length=1, max_length=64)
    entrypoint: tuple[Arg, ...] | None = Field(default=None, max_length=16)
    env_names: tuple[EnvName, ...] = Field(default=(), max_length=128)
    environment: BoundFile | None = None
    # Numeric, non-root uid:gid; never the image default (which may be root).
    user: str = Field(pattern=r"^[1-9][0-9]{0,9}:[1-9][0-9]{0,9}$")
    working_dir: str | None = Field(default=None, max_length=4096)
    labels: dict[LabelKey, LabelValue] = Field(default_factory=dict, max_length=64)
    exposed_ports: tuple[Annotated[str, Field(pattern=r"^[0-9]{1,5}/tcp$")], ...] = Field(max_length=16)
    stop_signal: str | None = Field(default=None, pattern=r"^SIG[A-Z0-9]{1,12}$")
    stop_timeout: int | None = Field(default=None, ge=0, le=300)
    mounts: tuple[EngineMount, ...] = Field(max_length=32)
    tmpfs: dict[TmpfsTarget, TmpfsOptions] = Field(default_factory=dict, max_length=8)
    port_bindings: tuple[EnginePortBinding, ...] = Field(max_length=16)
    network: EngineNetworkAttachment
    restart_policy: EngineRestartPolicy = EngineRestartPolicy()
    readonly_rootfs: Literal[True] = True
    cap_drop: tuple[Capability, ...] = ("ALL",)
    cap_add: tuple[Capability, ...] = ()
    security_opt: tuple[Annotated[str, Field(pattern=r"^[a-z][a-z-]{1,40}(?:[:=][A-Za-z0-9_./:-]{1,256})?$")], ...] = Field(default=(), max_length=8)
    privileged: Literal[False] = False
    memory_bytes: int = Field(gt=0, le=64 * 1024**3)
    memory_swap_bytes: int | None = Field(default=None, gt=0, le=128 * 1024**3)
    nano_cpus: int = Field(gt=0, le=64_000_000_000)
    pids_limit: int | None = Field(default=None, gt=0, le=1_000_000)
    init: bool | None = None
    log_config: EngineLogConfig

    @model_validator(mode="after")
    def _approved(self):
        if self.name != "vkm-core-" + self.unit + "-1":
            raise ValueError("owned receiver name must be fixed by its unit")
        if len(set(self.env_names)) != len(self.env_names) or bool(self.env_names) != (self.environment is not None):
            raise ValueError("unique Env names require exactly one SHA-pinned environment file")
        if any(k.startswith(COMPOSE_LABEL_PREFIX) or k.startswith(LABEL_PREFIX) for k in self.labels):
            raise ValueError("Compose and reserved ownership labels are never approved profile labels")
        if self.working_dir is not None and not _posix_absolute(self.working_dir):
            raise ValueError("working directory must be a normalized absolute container path")
        if len(set(self.exposed_ports)) != len(self.exposed_ports) or any(
                not 1 <= int(p.split("/")[0]) <= 65535 for p in self.exposed_ports):
            raise ValueError("exposed ports must be unique tcp ports")
        bindings = {(b.host_ip, b.host_port) for b in self.port_bindings}
        if (len(bindings) != len(self.port_bindings)
                or any(str(b.container_port) + "/tcp" not in self.exposed_ports for b in self.port_bindings)):
            raise ValueError("port bindings must be unique and exposed")
        targets = [m.target for m in self.mounts]
        if (len(set(targets)) != len(targets) or set(targets) & set(self.tmpfs)
                or any(not _posix_absolute(t) or t == "/" or any(_posix_under(t, f) for f in FORBIDDEN_TARGET_TREES)
                       for t in self.tmpfs)):
            raise ValueError("mount and tmpfs targets must be unique normalized container paths")
        if ("ALL" not in self.cap_drop or set(self.cap_add) - APPROVED_CAP_ADD or set(self.cap_add) & set(self.cap_drop)
                or len(set(self.cap_drop)) != len(self.cap_drop) or len(set(self.cap_add)) != len(self.cap_add)):
            raise ValueError("capabilities must drop ALL and add only reviewed capabilities")
        if len(set(self.security_opt)) != len(self.security_opt) or set(self.security_opt) - APPROVED_SECURITY_OPT:
            raise ValueError("security options must be unique reviewed hardening options")
        if self.memory_swap_bytes is not None and self.memory_swap_bytes < self.memory_bytes:
            raise ValueError("memory swap limit cannot be below the memory limit")
        return self

    @property
    def sha256(self):
        return record_hash(self)


class EngineComponentPin(StrictModel):
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9 _.-]{0,63}$")
    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.+:~-]{0,127}$")


class EngineDaemonPin(StrictModel):
    """Exact daemon identity; the actual values come from the L1 qualification."""
    daemon_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9:_.-]{7,127}$")
    engine_version: str = Field(pattern=r"^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}(?:[-+][A-Za-z0-9.+-]{1,64})?$")
    components: tuple[EngineComponentPin, ...] = Field(min_length=1, max_length=8)
    default_runtime: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,63}$")
    security_options: tuple[Annotated[str, Field(pattern=r"^name=[a-z0-9]+(?:,[a-z]+=[A-Za-z0-9_./-]+)*$")], ...] = Field(max_length=16)
    cgroup_driver: Literal["systemd", "cgroupfs"]
    cgroup_version: Literal["1", "2"]
    docker_root_dir: str = Field(min_length=2, max_length=4096)

    @model_validator(mode="after")
    def _exact(self):
        if (not _posix_absolute(self.docker_root_dir) or len({c.name for c in self.components}) != len(self.components)
                or len(set(self.security_options)) != len(self.security_options)):
            raise ValueError("exact unique daemon identity required")
        return self

    @property
    def apparmor_profile(self):
        return "docker-default" if any(o.startswith("name=apparmor") for o in self.security_options) else ""


class EngineCreatePlan(StrictModel):
    """Operator-approved fixed create of both first-LIVE receivers."""
    model_config = ConfigDict(hide_input_in_errors=True)

    schema_version: Literal["vkm-engine-create-plan/2"] = "vkm-engine-create-plan/2"
    api_version: str = Field(pattern=r"^1\.(?:4[1-9]|5[0-9])$")
    daemon: EngineDaemonPin
    request_timeout_seconds: float = Field(default=15, gt=0, le=60)
    create_timeout_seconds: float = Field(default=60, gt=0, le=180)
    stop_timeout_seconds: int = Field(default=30, ge=0, le=120)
    max_response_bytes: int = Field(default=1024 * 1024, ge=64 * 1024, le=8 * 1024 * 1024)
    profiles: dict[Literal["api", "mcp"], EngineCreateProfile]

    @model_validator(mode="after")
    def _both(self):
        if set(self.profiles) != set(UNITS) or any(p.unit != u for u, p in self.profiles.items()):
            raise ValueError("plan must contain exactly the API and read-MCP profiles")
        api, mcp = self.profiles["api"], self.profiles["mcp"]
        if (api.network.name, api.network.network_id, api.network.config_sha256) != (
                mcp.network.name, mcp.network.network_id, mcp.network.config_sha256):
            raise ValueError("both receivers must join the same pinned external network")
        bound = [(b.host_ip, b.host_port) for p in (api, mcp) for b in p.port_bindings]
        if len(set(bound)) != len(bound):
            raise ValueError("host bindings must be unique across receivers")
        root = self.daemon.docker_root_dir
        if any(m.source.startswith("/") and (_posix_under(m.source, root) or _posix_under(root, m.source))
               for p in (api, mcp) for m in p.mounts):
            raise ValueError("bind mounts must not expose the Engine data root")
        return self

    @property
    def sha256(self):
        return record_hash(self)


def ownership_labels(attempt_id: str, unit: str, profile_sha256: str) -> dict:
    return {LABEL_ATTEMPT: attempt_id, LABEL_PROFILE: profile_sha256, LABEL_UNIT: unit}


def env_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()


def _bindings(profile):
    result = {}
    for b in profile.port_bindings:
        result.setdefault(str(b.container_port) + "/tcp", []).append([b.host_ip, str(b.host_port)])
    return {k: sorted(v) for k, v in sorted(result.items())}


def _log(profile):
    return {"Type": profile.log_config.type, "Config": dict(sorted(profile.log_config.config.items()))}


def create_body(profile: EngineCreateProfile, ownership: dict, env: dict) -> dict:
    """The only create request body: built from the approved profile and its resolved Env."""
    config = {"Image": profile.image, "Cmd": list(profile.cmd),
              "Env": [k + "=" + env[k] for k in sorted(profile.env_names)],
              "Labels": dict(sorted({**profile.labels, **ownership}.items())),
              "ExposedPorts": {p: {} for p in sorted(profile.exposed_ports)}, "User": profile.user,
              "AttachStdin": False, "AttachStdout": False, "AttachStderr": False,
              "Tty": False, "OpenStdin": False, "StdinOnce": False}
    for key, value in (("Entrypoint", profile.entrypoint), ("WorkingDir", profile.working_dir),
                       ("StopSignal", profile.stop_signal), ("StopTimeout", profile.stop_timeout)):
        if value is not None:
            config[key] = list(value) if isinstance(value, tuple) else value
    host = {"Mounts": [{"Type": "bind", "Source": m.source, "Target": m.target, "ReadOnly": m.read_only}
                       for m in profile.mounts],
            "Tmpfs": dict(sorted(profile.tmpfs.items())),
            "PortBindings": {k: [{"HostIp": ip, "HostPort": port} for ip, port in v]
                             for k, v in _bindings(profile).items()},
            "NetworkMode": profile.network.name,
            "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
            "ReadonlyRootfs": True, "CapDrop": list(profile.cap_drop), "CapAdd": list(profile.cap_add),
            "SecurityOpt": list(profile.security_opt), "Privileged": False, "PublishAllPorts": False,
            "AutoRemove": False, "IpcMode": "private", "CgroupnsMode": "private",
            "Memory": profile.memory_bytes, "NanoCpus": profile.nano_cpus, "LogConfig": _log(profile)}
    for key, value in (("MemorySwap", profile.memory_swap_bytes), ("PidsLimit", profile.pids_limit),
                       ("Init", profile.init)):
        if value is not None:
            host[key] = value
    networking = {"EndpointsConfig": {profile.network.name: {"Aliases": list(profile.network.aliases)}}}
    return {**config, "HostConfig": host, "NetworkingConfig": networking}


def _caps(values):
    if values is None:
        return []
    if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
        raise TypeError("capabilities")
    return sorted({v.upper() if v.upper() == "ALL" else v.upper().removeprefix("CAP_") for v in values})


def _baseline(values, baseline):
    """A daemon default (null) or a superset of the documented default; [] is a weakening."""
    if values is None:
        return DEFAULT_OR_STRICTER
    if not isinstance(values, list):
        raise TypeError("path list")
    return DEFAULT_OR_STRICTER if baseline <= set(values) else WEAKENED


def _empty(value):
    return [] if value in (None, [], {}) else value


_FIXED_HOST = {"PublishAllPorts": False, "AutoRemove": False, "PidMode": "", "UsernsMode": "", "UTSMode": "",
               "IpcMode": "private", "CgroupnsMode": "private", "Cgroup": "", "CgroupParent": "", "VolumeDriver": "",
               "Devices": [], "DeviceRequests": [], "DeviceCgroupRules": [], "GroupAdd": [], "VolumesFrom": [],
               "Links": [], "ExtraHosts": [], "Dns": [], "DnsOptions": [], "DnsSearch": [], "Ulimits": [],
               "Sysctls": [], "StorageOpt": [], "Annotations": [], "OomKillDisable": False, "OomScoreAdj": 0,
               "MaskedPaths": DEFAULT_OR_STRICTER, "ReadonlyPaths": DEFAULT_OR_STRICTER}
_FIXED_CONFIG = {"Tty": False, "OpenStdin": False, "StdinOnce": False, "AttachStdin": False, "Healthcheck": IMAGE_DEFAULT}


def expected_contract(profile: EngineCreateProfile, ownership: dict, env_digests: dict, daemon: EngineDaemonPin) -> dict:
    """Normalized approved contract; order-insensitive wherever Docker is. Env as digests only."""
    config = {"Image": profile.image, "Cmd": list(profile.cmd),
              "Entrypoint": list(profile.entrypoint) if profile.entrypoint is not None else IMAGE_DEFAULT,
              "Env": dict(sorted(env_digests.items())), "User": profile.user,
              "WorkingDir": profile.working_dir if profile.working_dir is not None else IMAGE_DEFAULT,
              "Labels": dict(sorted({**profile.labels, **ownership}.items())),
              "ExposedPorts": sorted(profile.exposed_ports), **_FIXED_CONFIG}
    if profile.stop_signal is not None:
        config["StopSignal"] = profile.stop_signal
    if profile.stop_timeout is not None:
        config["StopTimeout"] = profile.stop_timeout
    host = {"Mounts": sorted(({"Type": "bind", "Source": m.source, "Target": m.target, "ReadOnly": m.read_only,
                               "Options": []} for m in profile.mounts), key=lambda m: (m["Target"], m["Source"])),
            "Binds": [], "Tmpfs": dict(sorted(profile.tmpfs.items())), "PortBindings": _bindings(profile),
            "NetworkMode": profile.network.name, "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
            "ReadonlyRootfs": True, "CapDrop": _caps(list(profile.cap_drop)), "CapAdd": _caps(list(profile.cap_add)),
            "SecurityOpt": sorted(profile.security_opt), "Privileged": False, "Runtime": daemon.default_runtime,
            "Memory": profile.memory_bytes, "NanoCpus": profile.nano_cpus, "LogConfig": _log(profile), **_FIXED_HOST}
    for key, value in (("MemorySwap", profile.memory_swap_bytes), ("PidsLimit", profile.pids_limit),
                       ("Init", profile.init)):
        if value is not None:
            host[key] = value
    # Docker builds inspect Mounts from a Go map: compare as a set ordered by destination.
    mounts = sorted(({"Type": "bind", "Source": m.source, "Destination": m.target, "RW": not m.read_only}
                     for m in profile.mounts), key=lambda m: (m["Destination"], m["Source"]))
    return {"name": "/" + profile.name, "image": profile.image, "compose_labels": False,
            "apparmor_profile": daemon.apparmor_profile, "config": config, "host": host, "mounts": mounts,
            "networks": {profile.network.name: {"NetworkID": profile.network.network_id,
                "Aliases": sorted(profile.network.aliases), "IPAMConfig": [], "Links": [], "DriverOpts": []}}}


def _env_map(values):
    if values is None:
        return {}
    if not isinstance(values, list) or any(not isinstance(v, str) or "=" not in v for v in values):
        raise TypeError("environment")
    result = {}
    for item in values:
        key, value = item.split("=", 1)
        if key in result:
            raise TypeError("duplicate environment name")
        result[key] = value
    return result


def _image_field(actual, image, pinned):
    if pinned is not None:
        return actual
    return IMAGE_DEFAULT if (actual or None) == (image or None) else DIFFERS_FROM_IMAGE


def observed_contract(item: dict, image_config: dict, profile: EngineCreateProfile, daemon: EngineDaemonPin) -> dict:
    """Normalize an inspect document to the approved contract's shape.

    Image-inherited Env/Labels/ExposedPorts are removed only when they equal the
    pinned image's own values; Env values are reduced to SHA-256 digests.
    Daemon-filled defaults are compared only where the profile pins them or a
    fixed hardened value (runtime, namespaces, masks, devices, DNS...) is required.
    """
    try:
        cfg, host = item.get("Config") or {}, item.get("HostConfig") or {}
        image_config = image_config or {}
        labels = cfg.get("Labels") or {}
        if not isinstance(labels, dict):
            raise TypeError("labels")
        image_labels = image_config.get("Labels") or {}
        expected_keys = set(profile.labels) | {LABEL_ATTEMPT, LABEL_PROFILE, LABEL_UNIT}
        env, image_env = _env_map(cfg.get("Env")), _env_map(image_config.get("Env"))
        exposed = set(cfg.get("ExposedPorts") or {})
        image_exposed = set(image_config.get("ExposedPorts") or {})
        config = {"Image": cfg.get("Image"), "Cmd": cfg.get("Cmd"),
                  "Entrypoint": _image_field(cfg.get("Entrypoint"), image_config.get("Entrypoint"), profile.entrypoint),
                  "Env": dict(sorted((k, env_digest(v)) for k, v in env.items()
                                     if k in profile.env_names or image_env.get(k) != v)),
                  "User": cfg.get("User"),
                  "WorkingDir": _image_field(cfg.get("WorkingDir"), image_config.get("WorkingDir"), profile.working_dir),
                  "Labels": dict(sorted((k, v) for k, v in labels.items() if k in expected_keys or image_labels.get(k) != v)),
                  "ExposedPorts": sorted(p for p in exposed if p in profile.exposed_ports or p not in image_exposed),
                  "Tty": cfg.get("Tty") or False, "OpenStdin": cfg.get("OpenStdin") or False,
                  "StdinOnce": cfg.get("StdinOnce") or False, "AttachStdin": cfg.get("AttachStdin") or False,
                  "Healthcheck": _image_field(cfg.get("Healthcheck"), image_config.get("Healthcheck"), None)}
        if profile.stop_signal is not None:
            config["StopSignal"] = cfg.get("StopSignal")
        if profile.stop_timeout is not None:
            config["StopTimeout"] = cfg.get("StopTimeout")
        mounts = []
        for m in host.get("Mounts") or []:
            mounts.append({"Type": m.get("Type"), "Source": m.get("Source"), "Target": m.get("Target"),
                           "ReadOnly": bool(m.get("ReadOnly") or False),
                           "Options": sorted(k for k, v in m.items() if k not in {"Type", "Source", "Target", "ReadOnly"}
                                             and v not in (None, "", {}, [], False))})
        bindings = {}
        for port, rows in (host.get("PortBindings") or {}).items():
            bindings[port] = sorted([r.get("HostIp"), r.get("HostPort")] for r in rows or [])
        restart = host.get("RestartPolicy") or {}
        log = host.get("LogConfig") or {}
        observed_host = {"Mounts": sorted(mounts, key=lambda m: (str(m["Target"]), str(m["Source"]))),
                         "Binds": _empty(host.get("Binds")), "Tmpfs": dict(sorted((host.get("Tmpfs") or {}).items())),
                         "PortBindings": dict(sorted(bindings.items())), "NetworkMode": host.get("NetworkMode"),
                         "RestartPolicy": {"Name": restart.get("Name"), "MaximumRetryCount": restart.get("MaximumRetryCount")},
                         "ReadonlyRootfs": host.get("ReadonlyRootfs"), "CapDrop": _caps(host.get("CapDrop")),
                         "CapAdd": _caps(host.get("CapAdd")), "SecurityOpt": sorted(host.get("SecurityOpt") or []),
                         "Privileged": host.get("Privileged"), "Runtime": host.get("Runtime") or daemon.default_runtime,
                         "Memory": host.get("Memory"), "NanoCpus": host.get("NanoCpus"),
                         "LogConfig": {"Type": log.get("Type"), "Config": dict(sorted((log.get("Config") or {}).items()))},
                         "PublishAllPorts": host.get("PublishAllPorts") or False, "AutoRemove": host.get("AutoRemove") or False,
                         "PidMode": host.get("PidMode") or "", "UsernsMode": host.get("UsernsMode") or "",
                         "UTSMode": host.get("UTSMode") or "", "IpcMode": host.get("IpcMode"),
                         "CgroupnsMode": host.get("CgroupnsMode"), "Cgroup": host.get("Cgroup") or "",
                         "CgroupParent": host.get("CgroupParent") or "", "VolumeDriver": host.get("VolumeDriver") or "",
                         "OomKillDisable": host.get("OomKillDisable") or False, "OomScoreAdj": host.get("OomScoreAdj") or 0,
                         "MaskedPaths": _baseline(host.get("MaskedPaths"), BASELINE_MASKED_PATHS),
                         "ReadonlyPaths": _baseline(host.get("ReadonlyPaths"), BASELINE_READONLY_PATHS)}
        for key in ("Devices", "DeviceRequests", "DeviceCgroupRules", "GroupAdd", "VolumesFrom", "Links", "ExtraHosts",
                    "Dns", "DnsOptions", "DnsSearch", "Ulimits", "Sysctls", "StorageOpt", "Annotations"):
            observed_host[key] = _empty(host.get(key))
        for key, value in (("MemorySwap", profile.memory_swap_bytes), ("PidsLimit", profile.pids_limit),
                           ("Init", profile.init)):
            if value is not None:
                observed_host[key] = host.get(key)
        native_mounts = sorted(({"Type": m.get("Type"), "Source": m.get("Source"), "Destination": m.get("Destination"),
                                 "RW": m.get("RW")} for m in item.get("Mounts") or []
                                if not (m.get("Type") == "tmpfs" and m.get("Destination") in profile.tmpfs)),
                               key=lambda m: (str(m["Destination"]), str(m["Source"])))
        short = str(item.get("Id") or "")[:12]
        running = (item.get("State") or {}).get("Running") is True

        def network_id(name, value):
            # Some daemons assign the endpoint NetworkID only when it connects (start).
            # Before start an EMPTY id of the pinned network name is accepted (the
            # name->id pin is checked natively right before create and start);
            # once running the exact pinned id is required.
            if not running and value in ("", None) and name == profile.network.name:
                return profile.network.network_id
            return value
        networks = {name: {"NetworkID": network_id(name, n.get("NetworkID")),
                           "Aliases": sorted(set(n.get("Aliases") or []) - {short}),
                           "IPAMConfig": _empty({k: v for k, v in (n.get("IPAMConfig") or {}).items() if v}),
                           "Links": _empty(n.get("Links")), "DriverOpts": _empty(n.get("DriverOpts"))}
                    for name, n in sorted(((item.get("NetworkSettings") or {}).get("Networks") or {}).items())}
        return {"name": item.get("Name"), "image": item.get("Image"),
                "compose_labels": any(str(k).startswith(COMPOSE_LABEL_PREFIX) for k in labels),
                "apparmor_profile": item.get("AppArmorProfile") or "",
                "config": config, "host": observed_host, "mounts": native_mounts, "networks": networks}
    except (AttributeError, TypeError, ValueError):
        raise EngineContractDrift("native inspect document is malformed") from None


def drift_fields(expected: dict, observed: dict) -> list[str]:
    """Names of differing contract fields only; never their values."""
    fields = []
    for key in sorted(set(expected) | set(observed)):
        left, right = expected.get(key), observed.get(key)
        if isinstance(left, dict) and isinstance(right, dict) and key in {"config", "host"}:
            fields += [key + "." + k for k in sorted(set(left) | set(right)) if left.get(k) != right.get(k)]
        elif left != right:
            fields.append(key)
    return fields


def _version_tuple(value, parts):
    match = re.match(r"^([0-9]{1,4})\.([0-9]{1,4})" + (r"\.([0-9]{1,4})" if parts == 3 else ""), str(value or ""))
    if match is None:
        raise EngineUnavailable("Engine version is not parseable")
    return tuple(int(x) for x in match.groups())


def created_unix(value) -> float | None:
    """Engine ``Created`` (RFC 3339, nanoseconds) as Unix seconds; None if unparseable."""
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})", str(value or ""))
    if match is None:
        return None
    base = datetime.fromisoformat(match.group(1) + ("+00:00" if match.group(3) == "Z" else match.group(3)))
    return base.timestamp() + (int(match.group(2).ljust(9, "0")) / 1e9 if match.group(2) else 0.0)


class EngineTransport:
    """Fixed Engine API client over the local unix socket.

    Only the operations below exist. Each is a (method, path-template) pair
    built by code; path parameters are validated before formatting and the
    final path is matched again. No redirects, no environment proxies, bounded
    per-read timeouts plus a monotonic TOTAL deadline per request, and bounded
    response sizes. Tests inject a fake ``transport``.
    """
    _OPERATIONS = {
        "version": ("GET", "/v{api}/version", (), ()),
        "info": ("GET", "/v{api}/info", (), ()),
        "image": ("GET", "/v{api}/images/{image_id}/json", ("image_id",), ()),
        "network": ("GET", "/v{api}/networks/{network_id}", ("network_id",), ()),
        "create": ("POST", "/v{api}/containers/create", (), ("name",)),
        "inspect": ("GET", "/v{api}/containers/{ref}/json", ("ref",), ()),
        "start": ("POST", "/v{api}/containers/{container_id}/start", ("container_id",), ()),
        "stop": ("POST", "/v{api}/containers/{container_id}/stop", ("container_id",), ("t",)),
        "remove": ("DELETE", "/v{api}/containers/{container_id}", ("container_id",), ("force", "v")),
    }
    _PARAMS = {"image_id": IMAGE_ID, "network_id": HEX64, "container_id": HEX64,
               "ref": re.compile(r"[0-9a-f]{64}|vkm-core-(?:api|mcp)-1")}
    _QUERY = {"name": OWNED_NAME, "t": re.compile(r"[0-9]{1,3}"), "force": re.compile(r"false"), "v": re.compile(r"false")}

    def __init__(self, api_version: str, *, transport=None, socket_path: str = ENGINE_SOCKET,
                 timeout_seconds: float = 15, max_response_bytes: int = 1024 * 1024,
                 _monotonic: Callable[[], float] = time.monotonic):
        import httpx
        if not re.fullmatch(API_VERSION, api_version or ""):
            raise EngineRequestRefused("Engine API version is not pinned")
        if not _posix_absolute(socket_path or "") or not socket_path.endswith(".sock"):
            raise EngineRequestRefused("fixed Engine socket path required")
        self.api_version = api_version
        self.socket_path = socket_path
        self.timeout_seconds = float(timeout_seconds)
        self.max_response_bytes = int(max_response_bytes)
        self._monotonic = _monotonic
        api = re.escape(api_version)
        self._paths = {
            "version": re.compile("/v" + api + "/version"),
            "info": re.compile("/v" + api + "/info"),
            "image": re.compile("/v" + api + "/images/sha256:[0-9a-f]{64}/json"),
            "network": re.compile("/v" + api + "/networks/[0-9a-f]{64}"),
            "create": re.compile("/v" + api + "/containers/create"),
            "inspect": re.compile("/v" + api + "/containers/(?:[0-9a-f]{64}|vkm-core-(?:api|mcp)-1)/json"),
            "start": re.compile("/v" + api + "/containers/[0-9a-f]{64}/start"),
            "stop": re.compile("/v" + api + "/containers/[0-9a-f]{64}/stop"),
            "remove": re.compile("/v" + api + "/containers/[0-9a-f]{64}"),
        }
        self._client = httpx.Client(transport=transport if transport is not None else self._native_transport(socket_path),
            base_url="http://localhost", follow_redirects=False, trust_env=False,
            timeout=httpx.Timeout(self.timeout_seconds))

    @staticmethod
    def _native_transport(socket_path):
        import httpx
        if sys.platform != "linux":
            raise EngineUnavailable("native Engine socket requires qualified Linux")
        try:
            info = os.stat(socket_path)
        except OSError:
            raise EngineUnavailable("fixed Engine socket is unavailable") from None
        if not stat.S_ISSOCK(info.st_mode):
            raise EngineUnavailable("fixed Engine socket is not a socket")
        return httpx.HTTPTransport(uds=socket_path, retries=0)

    def _request(self, operation, *, params=None, query=(), body=None, timeout=None):
        import httpx
        try:
            method, template, names, query_names = self._OPERATIONS[operation]
        except (KeyError, TypeError):
            raise EngineRequestRefused("operation is outside the fixed Engine allowlist") from None
        params = dict(params or {})
        if tuple(sorted(params)) != tuple(sorted(names)) or any(
                not isinstance(v, str) or not self._PARAMS[k].fullmatch(v) for k, v in params.items()):
            raise EngineRequestRefused("invalid Engine path parameter")
        query = tuple(query)
        if tuple(k for k, _ in query) != query_names or any(
                not isinstance(v, str) or not self._QUERY[k].fullmatch(v) for k, v in query):
            raise EngineRequestRefused("invalid Engine query parameter")
        if (body is not None) != (operation == "create"):
            raise EngineRequestRefused("only the fixed create request carries a body")
        path = template.format(api=self.api_version, **params)
        if not self._paths[operation].fullmatch(path):
            raise EngineRequestRefused("Engine path is outside the fixed allowlist")
        url = path + ("?" + urlencode(query) if query else "")
        data = canonical_bytes(body) if body is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        budget = self.timeout_seconds if timeout is None else float(timeout)
        deadline = self._monotonic() + budget
        try:
            with self._client.stream(method, url, content=data, headers=headers, timeout=budget) as response:
                status = response.status_code
                if response.headers.get("content-encoding", "identity") != "identity":
                    raise EngineUnavailable("encoded Engine responses are refused")
                if 300 <= status < 400:
                    raise EngineUnavailable("Engine redirects are refused")
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > self.max_response_bytes:
                        raise EngineUnavailable("Engine response exceeds its bound")
                    if self._monotonic() > deadline:
                        raise EngineOutcomeUnknown("Engine response exceeded its total deadline")
                if self._monotonic() > deadline:
                    raise EngineOutcomeUnknown("Engine response exceeded its total deadline")
        except httpx.HTTPError:
            # Timeout, dropped connection or protocol failure: the effect is unknown.
            raise EngineOutcomeUnknown("Engine transport lost the request outcome") from None
        return status, bytes(raw)

    def _json(self, raw):
        from vkm_corpus.update.operator_units import strict_json
        try:
            value = strict_json(raw)
        except (ValueError, UnicodeDecodeError):
            raise EngineUnavailable("Engine response is not strict JSON") from None
        if not isinstance(value, dict):
            raise EngineUnavailable("Engine response is not an object")
        return value

    def _read(self, operation, params=None):
        status, raw = self._request(operation, params=params)
        if status == 404 and operation not in {"version", "info"}:
            return None
        if status != 200:
            raise EngineUnavailable("unexpected Engine read status")
        return self._json(raw)

    def version(self):
        return self._read("version")

    def info(self):
        return self._read("info")

    def image(self, image_id):
        return self._read("image", {"image_id": image_id})

    def network(self, network_id):
        return self._read("network", {"network_id": network_id})

    def inspect(self, ref):
        return self._read("inspect", {"ref": ref})

    def create(self, name, body, *, timeout):
        status, raw = self._request("create", query=(("name", name),), body=body, timeout=timeout)
        return status, (self._json(raw) if status == 201 else None)

    def start(self, container_id):
        status, _ = self._request("start", params={"container_id": container_id})
        if status not in {204, 304}:
            raise EngineUnavailable("Engine refused the fixed start")
        return status

    def stop(self, container_id, seconds):
        status, _ = self._request("stop", params={"container_id": container_id}, query=(("t", str(int(seconds))),),
                                  timeout=self.timeout_seconds + int(seconds))
        if status not in {204, 304}:
            raise EngineUnavailable("Engine refused the fixed stop")
        return status

    def remove(self, container_id):
        status, _ = self._request("remove", params={"container_id": container_id},
                                  query=(("force", "false"), ("v", "false")))
        if status not in {204, 404}:
            raise EngineUnavailable("Engine refused the fixed non-forced remove")
        return status

    def close(self):
        self._client.close()


_OWNERSHIP = {"attempt", "unit", "container_id", "profile_sha256", "reason"}
_DETAIL = {
    "JOURNAL_OPEN": {"journal_id", "intent_sha256", "plan_sha256", "preserved_ids"},
    "ATTEMPT_OPEN": {"attempt"},
    "CREATE_INTENT": {"attempt", "unit", "owned_name", "profile_sha256", "preserved_ids", "deadline_unix", "ordinal"},
    "CREATE_ACK": {"attempt", "unit", "container_id", "profile_sha256"},
    "CREATE_ADOPTED": _OWNERSHIP,
    "CREATE_PARTIAL": _OWNERSHIP,
    # A late occupant of the owned name attributed to an earlier recorded create
    # intent of this journal: owned for removal only, never startable.
    "OCCUPANT_OWNED": _OWNERSHIP,
    "CREATE_ABSENT": {"attempt", "unit", "reason"},
    "POST_CREATE_VERIFIED": {"attempt", "unit", "container_id", "contract_sha256"},
    "VERIFY_FAILED": {"attempt", "unit", "container_id", "fields"},
    "START_INTENT": {"attempt", "unit", "container_id", "contract_sha256", "deadline_unix"},
    "STARTED": {"attempt", "unit", "container_id"},
    "STOP_INTENT": {"container_id", "deadline_unix"},
    "STOPPED": {"container_id"},
    "REMOVE_INTENT": {"container_id", "deadline_unix"},
    "REMOVED": {"container_id", "observed"},
    "ATTEMPT_CLOSED": {"attempt"},
}


class EngineEffectJournal:
    """Append-only, hash-chained JSONL write-ahead journal of Engine effects.

    The file is created 0600 with O_EXCL (directory 0700), appended with
    O_APPEND after an exact device/inode check, and every record is fsynced
    together with its directory. Any torn, reordered, foreign or edited record
    fails closed; a torn tail is never auto-repaired (operator procedure).
    """

    def __init__(self, path: Path, header: dict, *, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        if set(header) != _DETAIL["JOURNAL_OPEN"]:
            raise EngineJournalError("journal header is incomplete")
        self.header = dict(header)
        self.clock = clock
        self._identity = None

    def _flags(self, flags):
        return flags | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)

    def _parent(self):
        parent = self.path.parent
        if any(p.is_symlink() for p in (parent, *parent.parents)):
            raise EngineJournalError("journal directory is indirect")
        if not parent.parent.is_dir():
            raise EngineJournalError("the control root must already exist")
        parent.mkdir(mode=0o700, exist_ok=True)
        if parent.is_symlink() or not parent.is_dir():
            raise EngineJournalError("journal directory is indirect")
        return parent

    def _check_file(self, fd=None):
        try:
            info = os.lstat(self.path)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_JOURNAL_BYTES:
            raise EngineJournalError("journal is not an exclusively owned bounded regular file")
        identity = (info.st_dev, info.st_ino)
        if fd is not None:
            held = os.fstat(fd)
            if (held.st_dev, held.st_ino) != identity or held.st_nlink != 1:
                raise EngineJournalError("journal inode changed")
        if self._identity is not None and identity != self._identity:
            raise EngineJournalError("journal inode changed")
        return identity

    def records(self) -> list[dict]:
        identity = self._check_file()
        if identity is None:
            if self._identity is not None:
                raise EngineJournalError("journal disappeared")
            return []
        fd = os.open(self.path, self._flags(os.O_RDONLY))
        try:
            self._check_file(fd)
            chunks, total = [], 0
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_JOURNAL_BYTES:
                    raise EngineJournalError("journal exceeds its bound")
                chunks.append(chunk)
        finally:
            os.close(fd)
        data = b"".join(chunks)
        if not data.endswith(b"\n"):
            raise EngineJournalError("journal has a torn record")
        lines = data[:-1].split(b"\n")
        if len(lines) > MAX_JOURNAL_RECORDS:
            raise EngineJournalError("journal exceeds its record bound")
        from vkm_corpus.update.operator_units import strict_json
        result, previous = [], "0" * 64
        for seq, line in enumerate(lines):
            try:
                record = strict_json(line)
            except (ValueError, UnicodeDecodeError):
                raise EngineJournalError("journal record is not strict JSON") from None
            if (not isinstance(record, dict) or canonical_bytes(record) != line
                    or set(record) != {"schema", "seq", "prev_sha256", "recorded_unix", "phase", "detail", "sha256"}
                    or record["schema"] != JOURNAL_SCHEMA or record["seq"] != seq
                    or record["prev_sha256"] != previous or record["phase"] not in _DETAIL
                    or not isinstance(record["detail"], dict)
                    or set(record["detail"]) != _DETAIL[record["phase"]]
                    or record_hash({k: v for k, v in record.items() if k != "sha256"}) != record["sha256"]
                    or (seq == 0) != (record["phase"] == "JOURNAL_OPEN")):
                raise EngineJournalError("journal record chain is inconsistent")
            previous = record["sha256"]
            result.append(record)
        if result[0]["detail"] != self.header:
            raise EngineJournalError("journal belongs to another intent, plan or preserved set")
        self._identity = identity
        return result

    def append(self, phase: str, **detail) -> str:
        if phase not in _DETAIL or phase == "JOURNAL_OPEN" or set(detail) != _DETAIL[phase]:
            raise EngineJournalError("unregistered journal record")
        records = self.records()
        if not records:
            self._write_new()
            records = self.records()
        record = {"schema": JOURNAL_SCHEMA, "seq": len(records), "prev_sha256": records[-1]["sha256"],
                  "recorded_unix": float(self.clock()), "phase": phase, "detail": detail}
        record["sha256"] = record_hash(record)
        fd = os.open(self.path, self._flags(os.O_WRONLY | os.O_APPEND))
        try:
            self._check_file(fd)
            self._write_all(fd, canonical_bytes(record) + b"\n")
        finally:
            os.close(fd)
        _fsync_dir(self.path.parent)
        return record["sha256"]

    def _write_new(self):
        parent = self._parent()
        record = {"schema": JOURNAL_SCHEMA, "seq": 0, "prev_sha256": "0" * 64,
                  "recorded_unix": float(self.clock()), "phase": "JOURNAL_OPEN", "detail": self.header}
        record["sha256"] = record_hash(record)
        try:
            fd = os.open(self.path, self._flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL), 0o600)
        except FileExistsError:
            raise EngineJournalError("journal appeared concurrently") from None
        try:
            held = os.fstat(fd)
            self._identity = (held.st_dev, held.st_ino)
            self._write_all(fd, canonical_bytes(record) + b"\n")
        finally:
            os.close(fd)
        _fsync_dir(parent)

    @staticmethod
    def _write_all(fd, data):
        if os.write(fd, data) != len(data):
            raise EngineJournalError("journal append was short")
        os.fsync(fd)


def engine_journal_path(control_root: str, intent_sha256: str) -> Path:
    """Fixed per-intent journal location inside the first-LIVE control root."""
    if not HEX64.fullmatch(intent_sha256 or ""):
        raise EngineJournalError("invalid intent identity")
    return Path(control_root) / "first-live-engine" / (intent_sha256 + ".jsonl")


class EngineCreateAdapter:
    """Create, verify, start and roll back only journal-owned receiver containers.

    The adapter never calls Compose and never touches a preserved original ID.
    A lost create ACK is resolved by inspecting the exact owned name and
    adopting only this attempt's exact candidate. Occupants of the owned names
    that carry this journal's labels are owned for removal only when a recorded
    create intent precedes them. Production construction is gated by the
    caller (first-LIVE remains FIRST_LIVE_NOT_READY until Gate L1).
    """

    def __init__(self, plan: EngineCreatePlan, *, journal_path, intent_sha256: str, preserved_ids,
                 transport=None, socket_path: str = ENGINE_SOCKET, clock: Callable[[], float] = time.time,
                 _fault=None, _monotonic: Callable[[], float] = time.monotonic):
        if not isinstance(plan, EngineCreatePlan) or not HEX64.fullmatch(intent_sha256 or ""):
            raise EngineCreateError("approved Engine-create plan and intent identity required")
        preserved = tuple(sorted(set(preserved_ids)))
        if not preserved or any(not isinstance(i, str) or not HEX64.fullmatch(i) for i in preserved):
            raise EngineCreateError("exact preserved original container IDs are required")
        self.plan = plan
        self.intent_sha256 = intent_sha256
        self.preserved = frozenset(preserved)
        self.journal_id = record_hash({"intent_sha256": intent_sha256, "plan_sha256": plan.sha256})[:32]
        self.clock = clock
        self._fault = _fault
        self.journal = EngineEffectJournal(Path(journal_path), {"journal_id": self.journal_id,
            "intent_sha256": intent_sha256, "plan_sha256": plan.sha256, "preserved_ids": list(preserved)}, clock=clock)
        self.engine = EngineTransport(plan.api_version, transport=transport, socket_path=socket_path,
            timeout_seconds=plan.request_timeout_seconds, max_response_bytes=plan.max_response_bytes,
            _monotonic=_monotonic)
        self.socket_path = socket_path
        self._images = {}

    # -- journal state -----------------------------------------------------
    def _append(self, phase, **detail):
        digest = self.journal.append(phase, **detail)
        if self._fault is not None:
            self._fault(phase)
        return digest

    def attempt_id(self, attempt: int) -> str:
        return self.journal_id + "." + str(attempt)

    def _state(self):
        attempts, owned, current = {}, {}, None
        try:
            for record in self.journal.records()[1:]:
                phase, d = record["phase"], record["detail"]
                if phase == "ATTEMPT_OPEN":
                    if d["attempt"] != len(attempts) + 1 or (current is not None and not attempts[current]["closed"]):
                        raise KeyError(phase)
                    current = d["attempt"]
                    attempts[current] = {"closed": False, "units": {u: self._blank() for u in UNITS}}
                    continue
                if phase in {"STOP_INTENT", "STOPPED", "REMOVE_INTENT", "REMOVED"}:
                    item = owned[d["container_id"]]
                    if item["removed"]:
                        raise KeyError(phase)
                    item["removing"] = True
                    if phase == "STOPPED":
                        item["stopped"] = True
                    if phase == "REMOVED":
                        item["removed"] = True
                    continue
                if phase == "OCCUPANT_OWNED":
                    unit = attempts[d["attempt"]]["units"][d["unit"]]
                    if (d["container_id"] in owned or not unit["intent_times"]
                            or d["profile_sha256"] not in unit["intent_profiles"]
                            or d["attempt"] == current and not attempts[current]["closed"]
                            and (unit["pending"] is not None or unit["container_id"] is not None)):
                        raise KeyError(phase)
                    if d["attempt"] == current and not attempts[current]["closed"]:
                        unit.update(container_id=d["container_id"], profile_sha256=d["profile_sha256"], exact=False)
                    owned[d["container_id"]] = {"attempt": d["attempt"], "unit": d["unit"], "profile_sha256": d["profile_sha256"],
                                                "removed": False, "removing": False, "stopped": False}
                    continue
                if d["attempt"] != current or attempts[current]["closed"]:
                    raise KeyError(phase)
                if phase == "ATTEMPT_CLOSED":
                    if any(not o["removed"] for o in owned.values() if o["attempt"] == current) or any(
                            u["pending"] for u in attempts[current]["units"].values()):
                        raise KeyError(phase)
                    attempts[current]["closed"] = True
                    continue
                unit = attempts[current]["units"][d["unit"]]
                if phase == "CREATE_INTENT":
                    if unit["pending"] is not None or unit["container_id"] is not None:
                        raise KeyError(phase)
                    unit["pending"] = {**d, "recorded_unix": record["recorded_unix"]}
                    unit["intents"] += 1
                    unit["intent_times"].append(record["recorded_unix"])
                    unit["intent_profiles"].add(d["profile_sha256"])
                elif phase in {"CREATE_ACK", "CREATE_ADOPTED", "CREATE_PARTIAL"}:
                    if (unit["pending"] is None or d["container_id"] in owned
                            or d["profile_sha256"] != unit["pending"]["profile_sha256"]):
                        raise KeyError(phase)
                    unit.update(pending=None, container_id=d["container_id"], profile_sha256=d["profile_sha256"],
                                exact=phase != "CREATE_PARTIAL")
                    owned[d["container_id"]] = {"attempt": current, "unit": d["unit"], "profile_sha256": d["profile_sha256"],
                                                "removed": False, "removing": False, "stopped": False}
                elif phase == "CREATE_ABSENT":
                    if unit["pending"] is None:
                        raise KeyError(phase)
                    unit["pending"] = None
                elif d["container_id"] != unit["container_id"]:
                    raise KeyError(phase)
                elif phase == "POST_CREATE_VERIFIED":
                    unit["verified"] = d["contract_sha256"]
                elif phase == "VERIFY_FAILED":
                    unit["verified"], unit["exact"] = None, False
                elif phase == "START_INTENT":
                    if unit["verified"] != d["contract_sha256"]:
                        raise KeyError(phase)
                    unit["start_intent"] = True
                elif phase == "STARTED":
                    if not unit["start_intent"]:
                        raise KeyError(phase)
                    unit["started"] = True
        except (KeyError, TypeError):
            raise EngineJournalError("journal effect sequence is inconsistent") from None
        return attempts, owned, current

    @staticmethod
    def _blank():
        return {"pending": None, "intents": 0, "intent_times": [], "intent_profiles": set(), "container_id": None,
                "profile_sha256": None, "exact": False, "verified": None, "start_intent": False, "started": False}

    def _unit(self, unit):
        attempts, owned, current = self._state()
        if current is None or attempts[current]["closed"]:
            return None, None, owned
        return current, attempts[current]["units"][unit], owned

    def current(self, unit) -> str | None:
        """Journal-owned, exact and not yet removed candidate of the open attempt."""
        attempt, state, owned = self._unit(unit)
        identity = state and state["container_id"]
        if not identity or not state["exact"] or owned[identity]["removing"]:
            return None
        return identity

    # -- native checks -----------------------------------------------------
    def _check_daemon(self):
        """Exact pinned daemon: version, components, ID, runtime, security options, cgroups, data root."""
        pin = self.plan.daemon
        version, info = self.engine.version(), self.engine.info()
        components = version.get("Components")
        try:
            components = sorted((c["Name"], c["Version"]) for c in components)
        except (KeyError, TypeError):
            components = None
        options = info.get("SecurityOptions")
        if (version.get("Os") != "linux" or version.get("Version") != pin.engine_version
                or _version_tuple(version.get("ApiVersion"), 2) < _version_tuple(self.plan.api_version, 2)
                or _version_tuple(version.get("MinAPIVersion"), 2) > _version_tuple(self.plan.api_version, 2)
                or components != sorted((c.name, c.version) for c in pin.components)
                or info.get("ID") != pin.daemon_id or info.get("DefaultRuntime") != pin.default_runtime
                or not isinstance(options, list) or sorted(options) != sorted(pin.security_options)
                or info.get("CgroupDriver") != pin.cgroup_driver or str(info.get("CgroupVersion")) != pin.cgroup_version
                or info.get("DockerRootDir") != pin.docker_root_dir):
            raise EngineUnavailable("Engine daemon differs from its pinned identity")

    def preflight(self):
        """Pinned daemon, image and external network checks before any create/start."""
        from vkm_corpus.update.bootstrap_native import network_fingerprint
        self._check_daemon()
        for profile in self.plan.profiles.values():
            if any(mount_source_problem(m.source) for m in profile.mounts):
                raise EngineContractDrift("bind mount source is indirect, special, unavailable or forbidden")
            image = self.engine.image(profile.image)
            if image is None or image.get("Id") != profile.image:
                raise EngineContractDrift("pinned receiver image is unavailable or differs")
            config = image.get("Config") or {}
            volumes = set(config.get("Volumes") or {})
            if volumes - {m.target for m in profile.mounts} - set(profile.tmpfs):
                raise EngineContractDrift("image declares anonymous volumes outside the approved mounts")
            self._images[profile.image] = config
            net = self.engine.network(profile.network.network_id)
            try:
                same = network_fingerprint(net) == profile.network.config_sha256
            except (RuntimeError, ValueError):
                same = False
            if (net is None or not same or net.get("Id") != profile.network.network_id
                    or net.get("Name") != profile.network.name or net.get("Driver") != "bridge"
                    or net.get("Scope") != "local" or net.get("Ingress") not in (False, None)):
                raise EngineContractDrift("pinned external network is unavailable or differs")

    def _environment(self, profile) -> dict:
        """Read the SHA-pinned authority-root Env file; values never leave this call path."""
        if profile.environment is None:
            return {}
        from vkm_corpus.update.operator_units import bound_json
        try:
            values = bound_json(profile.environment)
        except (OSError, ValueError, RuntimeError):
            raise EngineContractDrift("approved receiver environment is unavailable or changed") from None
        if (not isinstance(values, dict) or set(values) != set(profile.env_names)
                or any(not isinstance(v, str) or len(v) > 8192 or "\x00" in v for v in values.values())):
            raise EngineContractDrift("approved receiver environment differs from its profile")
        return values

    def _contract(self, profile, attempt):
        digests = {k: env_digest(v) for k, v in self._environment(profile).items()}
        return expected_contract(profile, ownership_labels(self.attempt_id(attempt), profile.unit, profile.sha256),
                                 digests, self.plan.daemon)

    def expected_pin(self, unit) -> dict:
        attempt, _, _ = self._unit(unit)
        if attempt is None:
            raise EngineOwnershipError("no open create attempt")
        profile = self.plan.profiles[unit]
        return {"image_id": profile.image, "config_sha256": record_hash(self._contract(profile, attempt))}

    def _observe(self, identity, profile, attempt):
        doc = self.engine.inspect(identity)
        if doc is None:
            raise EngineOwnershipError("journal-owned candidate is absent")
        if doc.get("Id") != identity:
            raise EngineOwnershipError("Engine answered for another container")
        if profile.image not in self._images:
            self.preflight()
        observed = observed_contract(doc, self._images[profile.image], profile, self.plan.daemon)
        return doc, observed, drift_fields(self._contract(profile, attempt), observed)

    def observe(self, unit, *, running: bool):
        """Native observation of the exact journal-owned candidate: (inspect, pin)."""
        attempt, state, owned = self._unit(unit)
        identity = self.current(unit)
        if identity is None:
            raise EngineOwnershipError("no exact journal-owned candidate for this unit")
        doc, observed, _ = self._observe(identity, self.plan.profiles[unit], attempt)
        status = doc.get("State") or {}
        if (status.get("Running") is not running or status.get("Paused") is not False
                or (not running and status.get("Pid") != 0)):
            raise EngineContractDrift("native candidate state differs")
        return doc, {"image_id": doc.get("Image"), "config_sha256": record_hash(observed)}

    def _ours(self, doc, attempt, unit, profile_sha256):
        labels = (doc.get("Config") or {}).get("Labels") or {}
        identity = doc.get("Id")
        return (isinstance(labels, dict) and isinstance(identity, str) and HEX64.fullmatch(identity) is not None
                and identity not in self.preserved
                and not any(str(k).startswith(COMPOSE_LABEL_PREFIX) for k in labels)
                and labels.get(LABEL_ATTEMPT) == self.attempt_id(attempt) and labels.get(LABEL_UNIT) == unit
                and labels.get(LABEL_PROFILE) == profile_sha256)

    def _classify(self, unit, doc, attempts):
        """Occupant of an owned name: 'FOREIGN' or the attempt number of a recorded intent.

        A container that carries this journal's attempt label but cannot be tied
        to a recorded create intent (unit, profile, creation time) is refused.
        """
        labels = (doc.get("Config") or {}).get("Labels") or {}
        label = labels.get(LABEL_ATTEMPT) if isinstance(labels, dict) else None
        if not isinstance(label, str) or not label.startswith(self.journal_id + "."):
            return "FOREIGN"
        suffix = label[len(self.journal_id) + 1:]
        attempt = int(suffix) if suffix.isdigit() and not suffix.startswith("0") else None
        state = attempts.get(attempt, {}).get("units", {}).get(unit) if attempt else None
        profile = self.plan.profiles[unit]
        created = created_unix(doc.get("Created"))
        if (state is None or not state["intent_times"] or profile.sha256 not in state["intent_profiles"]
                or not self._ours(doc, attempt, unit, profile.sha256) or doc.get("Name") != "/" + profile.name
                or created is None or created + CREATED_TOLERANCE_SECONDS < min(state["intent_times"])):
            raise EngineOwnershipError("a container labelled for this journal is not attributable to a recorded create")
        return attempt

    # -- create ------------------------------------------------------------
    def _resolve(self, unit, attempt, pending, *, reason, wait_deadline=True, adopt=True):
        """Resolve a create whose outcome is unknown by the exact owned name only."""
        profile = self.plan.profiles[unit]
        doc = self.engine.inspect(pending["owned_name"])
        if doc is None:
            if wait_deadline and self.clock() < pending["deadline_unix"]:
                raise EngineCreateAmbiguous("create may still be in flight until its recorded deadline")
            self._append("CREATE_ABSENT", attempt=attempt, unit=unit, reason=reason + "_ABSENT")
            return None
        attempts, owned, _ = self._state()
        owner = self._classify(unit, doc, attempts)
        if owner == "FOREIGN":
            raise EngineOwnershipError("a container this attempt does not own occupies the owned name")
        identity = doc["Id"]
        if owner != attempt:
            # A late candidate of an earlier attempt holds the name: this create cannot
            # have succeeded. Own the earlier one for removal only.
            self._append("CREATE_ABSENT", attempt=attempt, unit=unit, reason=reason + "_OCCUPIED")
            if identity not in owned:
                self._append("OCCUPANT_OWNED", attempt=owner, unit=unit, container_id=identity,
                             profile_sha256=profile.sha256, reason=reason)
            raise EngineOwnershipError("an earlier attempt-owned candidate occupies the owned name")
        exact = False
        if adopt and pending["profile_sha256"] == profile.sha256:
            state = doc.get("State") or {}
            _, _, fields = self._observe(identity, profile, attempt)
            exact = not fields and state.get("Running") is False and state.get("Status") == "created"
        self._append("CREATE_ADOPTED" if exact else "CREATE_PARTIAL", attempt=attempt, unit=unit,
                     container_id=identity, profile_sha256=pending["profile_sha256"], reason=reason)
        if not exact:
            raise EngineContractDrift("attempt-owned candidate is not the exact approved creation")
        return identity

    def _open_attempt(self):
        attempts, owned, current = self._state()
        if current is not None and not attempts[current]["closed"]:
            return current
        if any(not o["removed"] for o in owned.values()):
            raise EngineOwnershipError("owned candidates of a previous attempt must be removed first")
        self._append("ATTEMPT_OPEN", attempt=len(attempts) + 1)
        return len(attempts) + 1

    def _verified(self, unit, attempt, identity):
        profile = self.plan.profiles[unit]
        _, _, fields = self._observe(identity, profile, attempt)
        if fields:
            self._append("VERIFY_FAILED", attempt=attempt, unit=unit, container_id=identity, fields=fields)
            raise EngineContractDrift("created candidate differs from the approved profile")
        contract = record_hash(self._contract(profile, attempt))
        _, state, _ = self._unit(unit)
        if state["verified"] != contract:
            self._append("POST_CREATE_VERIFIED", attempt=attempt, unit=unit, container_id=identity,
                         contract_sha256=contract)
        return identity

    def create(self, unit) -> str:
        """Create (or resume) the exact approved receiver; it is verified and NOT started."""
        profile = self.plan.profiles[unit]
        self.preflight()
        env = self._environment(profile)
        attempt = self._open_attempt()
        _, state, owned = self._unit(unit)
        if any(o["removing"] for o in owned.values() if o["attempt"] == attempt):
            raise EngineOwnershipError("attempt is being rolled back")
        if state["container_id"] is not None:
            if not state["exact"]:
                raise EngineOwnershipError("owned partial candidate requires rollback")
            if state["profile_sha256"] != profile.sha256:
                raise EngineOwnershipError("owned candidate belongs to a stale profile")
            return self._verified(unit, attempt, state["container_id"])
        if state["pending"] is not None:
            identity = self._resolve(unit, attempt, state["pending"], reason="RESUMED_PENDING_CREATE")
            if identity is not None:
                return self._verified(unit, attempt, identity)
            _, state, _ = self._unit(unit)
        if state["intents"] >= MAX_CREATE_INTENTS:
            raise EngineCreateAmbiguous("create intent budget exhausted")
        occupant = self.engine.inspect(profile.name)
        if occupant is not None:
            attempts, owned, _ = self._state()
            owner = self._classify(unit, occupant, attempts)
            if owner != "FOREIGN" and occupant["Id"] not in owned:
                self._append("OCCUPANT_OWNED", attempt=owner, unit=unit, container_id=occupant["Id"],
                             profile_sha256=profile.sha256, reason="LATE_OCCUPANT_BEFORE_CREATE")
            raise EngineOwnershipError("the owned name is occupied; only rollback may follow")
        pending = {"attempt": attempt, "unit": unit, "owned_name": profile.name, "profile_sha256": profile.sha256,
                   "preserved_ids": sorted(self.preserved),
                   "deadline_unix": float(self.clock()) + self.plan.create_timeout_seconds,
                   "ordinal": state["intents"] + 1}
        self._append("CREATE_INTENT", **pending)
        body = create_body(profile, ownership_labels(self.attempt_id(attempt), unit, profile.sha256), env)
        try:
            status, answer = self.engine.create(profile.name, body, timeout=self.plan.create_timeout_seconds)
        except EngineOutcomeUnknown:
            identity = self._resolve(unit, attempt, pending, reason="LOST_ACK_INSPECTED")
            if identity is None:
                raise EngineCreateAmbiguous("create outcome was lost and no candidate is observable") from None
            return self._verified(unit, attempt, identity)
        if status == 201:
            identity = answer.get("Id")
            if not isinstance(identity, str) or not HEX64.fullmatch(identity) or identity in self.preserved:
                raise EngineOwnershipError("Engine create answered with an invalid or preserved identity")
            self._append("CREATE_ACK", attempt=attempt, unit=unit, container_id=identity, profile_sha256=profile.sha256)
            return self._verified(unit, attempt, identity)
        if status == 409:
            doc = self.engine.inspect(profile.name)
            if doc is None:
                # The name may be reserved by a create still in flight: stay pending.
                raise EngineCreateAmbiguous("name conflict without a visible occupant; the create stays pending")
            attempts, _, _ = self._state()
            if self._classify(unit, doc, attempts) == "FOREIGN":
                self._append("CREATE_ABSENT", attempt=attempt, unit=unit, reason="NAME_CONFLICT_FOREIGN")
                raise EngineOwnershipError("a container this attempt does not own occupies the owned name")
            identity = self._resolve(unit, attempt, pending, reason="NAME_CONFLICT_INSPECTED", wait_deadline=False)
            if identity is None:
                raise EngineCreateAmbiguous("name conflict without an observable candidate")
            return self._verified(unit, attempt, identity)
        # Any other answer: the daemon finished this request but may have created
        # partially. Own a visible attempt candidate for rollback only.
        self._resolve(unit, attempt, pending, reason="DAEMON_ERROR_INSPECTED", wait_deadline=False, adopt=False)
        raise EngineCreateError("Engine refused the fixed create request")

    # -- start -------------------------------------------------------------
    def start(self, unit) -> str:
        """Start one exact verified candidate after re-verifying image, network and contract."""
        profile = self.plan.profiles[unit]
        self.preflight()
        attempt, state, owned = self._unit(unit)
        identity = state and state["container_id"]
        if (not identity or not state["exact"] or state["verified"] is None or owned[identity]["removing"]
                or state["profile_sha256"] != profile.sha256):
            raise EngineOwnershipError("no verified journal-owned candidate of the current profile")
        doc, observed, fields = self._observe(identity, profile, attempt)
        contract = record_hash(self._contract(profile, attempt))
        if fields or state["verified"] != contract:
            self._append("VERIFY_FAILED", attempt=attempt, unit=unit, container_id=identity, fields=fields or ["contract"])
            raise EngineContractDrift("candidate drifted before start")
        status = doc.get("State") or {}
        if status.get("Running") is True:
            if state["started"]:
                return identity
            if not state["start_intent"]:
                raise EngineOwnershipError("candidate was started outside this journal")
            self._append("STARTED", attempt=attempt, unit=unit, container_id=identity)
            return identity
        if state["started"] or status.get("Status") != "created" or status.get("Paused"):
            raise EngineOwnershipError("candidate is not in its pristine created state")
        if not state["start_intent"]:
            self._append("START_INTENT", attempt=attempt, unit=unit, container_id=identity, contract_sha256=contract,
                         deadline_unix=float(self.clock()) + self.plan.request_timeout_seconds)
        self.engine.start(identity)
        doc, _, fields = self._observe(identity, profile, attempt)
        if fields or (doc.get("State") or {}).get("Running") is not True:
            self._append("VERIFY_FAILED", attempt=attempt, unit=unit, container_id=identity,
                         fields=fields or ["state"])
            raise EngineContractDrift("started candidate differs from its verified contract")
        self._append("STARTED", attempt=attempt, unit=unit, container_id=identity)
        return identity

    # -- rollback ----------------------------------------------------------
    def has_effects(self) -> bool:
        """Whether this journal ever recorded anything (write-ahead before any create)."""
        return bool(self.journal.records())

    def assert_no_unjournaled_candidate(self):
        """With an empty journal: refuse if an owned name carries this journal's labels.

        Write-ahead means no CREATE_INTENT, no create request. A candidate labelled
        for this journal nevertheless means the journal was lost. This read-only
        check does not require the daemon pin, so identity drift alone cannot
        strand a pure legacy fallback; it never removes or displaces anything.
        """
        if self.journal.records():
            raise EngineJournalError("journal already holds effects")
        for profile in self.plan.profiles.values():
            doc = self.engine.inspect(profile.name)
            labels = ((doc or {}).get("Config") or {}).get("Labels") or {}
            if isinstance(labels, dict) and str(labels.get(LABEL_ATTEMPT, "")).startswith(self.journal_id + "."):
                raise EngineJournalError("the effect journal is missing but its candidate exists")

    def remove_owned(self) -> list[str]:
        """Remove only journal-owned IDs whose labels confirm their recorded attempt.

        Pending creates are resolved first, then the two fixed owned names are
        inspected for late occupants of recorded intents (owned for removal
        only). Preserved original IDs, renamed originals and every container
        that is not attributable to this journal are never stopped or removed.
        The attempt is not closed while anything attributable remains.
        """
        if not self.journal.records():
            self._check_daemon()
            self.assert_no_unjournaled_candidate()
            return []
        attempts, owned, current = self._state()
        if any(i in self.preserved for i in owned):
            raise EngineOwnershipError("journal claims a preserved original container")
        self._check_daemon()
        unresolved = False
        if current is not None and not attempts[current]["closed"]:
            for unit, state in attempts[current]["units"].items():
                if state["pending"] is not None:
                    try:
                        self._resolve(unit, current, state["pending"], reason="ROLLBACK_INSPECTED", adopt=False)
                    except EngineContractDrift:
                        pass  # recorded CREATE_PARTIAL: now journal-owned and removable
                    except (EngineCreateAmbiguous, EngineOwnershipError):
                        unresolved = True
            attempts, owned, current = self._state()
        for unit, profile in self.plan.profiles.items():
            doc = self.engine.inspect(profile.name)
            if doc is None or doc.get("Id") in owned:
                continue
            try:
                owner = self._classify(unit, doc, attempts)
            except EngineOwnershipError:
                unresolved = True
                continue
            if owner == "FOREIGN":
                continue  # never ours; the caller refuses to displace it
            state = attempts[owner]["units"][unit]
            if owner == current and not attempts[current]["closed"] and (
                    state["pending"] is not None or state["container_id"] is not None):
                unresolved = True
                continue
            self._append("OCCUPANT_OWNED", attempt=owner, unit=unit, container_id=doc["Id"],
                         profile_sha256=profile.sha256, reason="OWNED_NAME_SWEEP")
            attempts, owned, current = self._state()
        removed = []
        for identity, item in owned.items():
            if item["removed"]:
                continue
            doc = self.engine.inspect(identity)
            if doc is None:
                self._append("REMOVED", container_id=identity, observed="ABSENT")
                continue
            if doc.get("Id") != identity or not self._ours(doc, item["attempt"], item["unit"], item["profile_sha256"]):
                raise EngineOwnershipError("journal-owned ID no longer carries its attempt ownership")
            status = doc.get("State") or {}
            if status.get("Paused"):
                raise EngineOwnershipError("owned candidate is paused; an operator must decide")
            if status.get("Running") or status.get("Restarting"):
                self._append("STOP_INTENT", container_id=identity,
                             deadline_unix=float(self.clock()) + self.plan.stop_timeout_seconds + self.plan.request_timeout_seconds)
                self.engine.stop(identity, self.plan.stop_timeout_seconds)
                after = self.engine.inspect(identity)
                if after is not None and ((after.get("State") or {}).get("Running")
                                          or (after.get("State") or {}).get("Restarting")):
                    raise EngineOwnershipError("owned candidate did not stop")
                self._append("STOPPED", container_id=identity)
            self._append("REMOVE_INTENT", container_id=identity,
                         deadline_unix=float(self.clock()) + self.plan.request_timeout_seconds)
            answer = self.engine.remove(identity)
            self._append("REMOVED", container_id=identity, observed="DELETED" if answer == 204 else "ABSENT")
            removed.append(identity)
        if unresolved:
            raise EngineCreateAmbiguous("rollback left an unresolved create or occupant; nothing foreign was touched")
        attempts, owned, current = self._state()
        if current is not None and not attempts[current]["closed"]:
            self._append("ATTEMPT_CLOSED", attempt=current)
        return removed

    def close(self):
        self.engine.close()

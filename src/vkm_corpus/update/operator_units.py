"""Fixed CORE API/read-MCP control; no commands are deserialized from a campaign.

Releases are operator-owned, fully rendered Compose documents. Only the two
registered receivers may be recreated, from already available pinned images.
The durable controller owns the shared writer/drain fence and rollback intent.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from vkm_corpus.parquet.atomic import write_bytes
from vkm_corpus.update.contracts import GenerationManifest
from vkm_corpus.update.deployment import ReceiverControl, SelectorAdapter
from vkm_corpus.update.generation import GenerationUnavailable
from vkm_corpus.update.receiver import (MAX_PROOF_BYTES, RECEIVER_ROUTE, ReceiverChallenge,
    operator_token, verify_identity, process_start_ticks, SignedMcpReceiverIdentity, sign_mcp_identity)
from vkm_corpus.update.runtime import BoundFile, read_bound
from vkm_evidence.contracts import Sha256, StrictModel, canonical_bytes, record_hash

UNITS = ("api", "mcp")
MAX_CONFIG_BYTES = 2 * 1024 * 1024
MAX_COMMAND_BYTES = 8 * 1024 * 1024


def strict_json(raw: bytes):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate operator field")
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=unique)


def bound_json(ref: BoundFile):
    path = read_bound(ref).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)) or path.stat().st_size > MAX_CONFIG_BYTES:
        raise ValueError("operator input is indirect or exceeds bound")
    info = path.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or (os.name == "posix" and info.st_mode & 0o022)):
        raise ValueError("operator input is not independently owned and immutable")
    raw = path.read_bytes()
    if len(raw) > MAX_CONFIG_BYTES or hashlib.sha256(raw).hexdigest() != ref.sha256:
        raise ValueError("operator input changed")
    return strict_json(raw)


class UnitPin(StrictModel):
    image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    # Captured from a qualified container with the SAME effective configuration.
    config_sha256: Sha256


class ComposeRelease(StrictModel):
    compose: BoundFile
    project: Literal["vkm-core", "vkm-core-shadow"]
    units: dict[Literal["api", "mcp"], UnitPin]
    runtime_config_sha256: Sha256
    code_sha256: Sha256
    dependencies_sha256: Sha256
    access_sha256: Sha256
    mcp_read_principal_sha256: Sha256

    @model_validator(mode="after")
    def _complete(self):
        if set(self.units) != set(UNITS):
            raise ValueError("CORE receiver release must include API and read MCP")
        return self

    @property
    def sha256(self):
        return record_hash(self)


class UnitControlConfig(StrictModel):
    schema_version: Literal["vkm-core-unit-control/1"] = "vkm-core-unit-control/1"
    scope: Literal["SYNTHETIC", "SHADOW_PRODUCTION", "PRODUCTION_SWITCH"]
    docker: BoundFile
    compose_binary: BoundFile
    docker_socket: str
    control_root: str
    selected_release: str = "receiver-release.CURRENT"
    receiver_url: str
    api_container_port: int = Field(default=8000, ge=1, le=65535)
    mcp_receiver_url: str
    mcp_container_port: int = Field(default=8765, ge=1, le=65535)
    operator_token_file: str
    command_timeout_seconds: float = Field(default=90, gt=0, le=300)
    restart_timeout_seconds: float = Field(default=120, gt=0, le=600)
    releases: tuple[ComposeRelease, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def _closed(self):
        if Path(self.docker.path).name not in {"docker", "docker.exe"} or Path(self.compose_binary.path).name not in {"docker-compose", "docker-compose.exe"}:
            raise ValueError("only pinned Docker executables are registered")
        if (not Path(self.docker_socket).is_absolute() or self.docker_socket.startswith("-")
                or self.selected_release != "receiver-release.CURRENT"):
            raise ValueError("fixed local Docker socket/selector required")
        if len({r.sha256 for r in self.releases}) != len(self.releases):
            raise ValueError("duplicate receiver release")
        if len({r.project for r in self.releases}) != 1:
            raise ValueError("all releases must control the same isolated Compose project")
        if self.scope == "SHADOW_PRODUCTION" and self.releases[0].project != "vkm-core-shadow":
            raise ValueError("shadow control cannot target live CORE")
        if self.scope == "PRODUCTION_SWITCH" and self.releases[0].project != "vkm-core":
            raise ValueError("live profile must name live CORE explicitly")
        url = urlsplit(self.receiver_url)
        if (url.scheme not in {"http", "https"} or url.hostname != "127.0.0.1" or url.username or url.password
                or url.query or url.fragment or url.path not in {"", "/"}):
            raise ValueError("receiver endpoint must be a fixed origin without credentials")
        mcp_url = urlsplit(self.mcp_receiver_url)
        if (mcp_url.scheme not in {"http", "https"} or mcp_url.hostname != "127.0.0.1" or mcp_url.username
                or mcp_url.password or mcp_url.query or mcp_url.fragment or mcp_url.path not in {"", "/"}
                or self.mcp_receiver_url == self.receiver_url):
            raise ValueError("read MCP requires its own fixed local origin")
        for value in (self.control_root, self.docker_socket, self.operator_token_file,
                      self.docker.path, self.compose_binary.path, *(r.compose.path for r in self.releases)):
            if not Path(value).is_absolute() or ".." in Path(value).parts:
                raise ValueError("unit control paths must be absolute and normalized")
        return self


def container_fingerprint(raw: dict, *, project: str, unit: str) -> dict:
    """Hash native effective settings; never persist environment/secret values."""
    labels = raw["Config"].get("Labels") or {}
    if (unit not in UNITS or labels.get("com.docker.compose.project") != project
            or labels.get("com.docker.compose.service") != unit
            or labels.get("com.docker.compose.container-number") != "1"
            or raw["State"].get("Running") is not True or raw["State"].get("Paused") is not False):
        raise GenerationUnavailable("actual container is not the registered running receiver")
    config = dict(raw["Config"])
    # Docker generates hostname and Compose generates these bookkeeping labels.
    # All other labels, env, argv, user and native host limits stay in the pin.
    config.pop("Hostname", None)
    config.pop("Image", None)
    config["Labels"] = {k: v for k, v in labels.items() if k not in {
        "com.docker.compose.config-hash", "com.docker.compose.version",
        "com.docker.compose.image", "com.docker.compose.project.config_files",
        "com.docker.compose.project.working_dir"}}
    host = dict(raw["HostConfig"])
    mounts = [{k: m.get(k) for k in ("Type", "Source", "Destination", "Mode", "RW", "Propagation")}
              for m in raw.get("Mounts", [])]
    config_hash = record_hash({"config": config, "host_config": host,
                              "mounts": sorted(mounts, key=lambda m: (m["Destination"], m["Source"] or "")),
                              "networks": sorted((raw.get("NetworkSettings", {}).get("Networks") or {}).keys())})
    return {"image_id": raw["Image"], "config_sha256": config_hash}


def verify_native_process(container: dict, proof):
    """Join the signed HTTP process to the inspected Docker PID namespace.

    These fixed procfs paths are native observations, never incoming paths. A
    separate healthy endpoint with identical manifests cannot pass this join.
    Rootless/restricted procfs is BLOCKED until independently qualified.
    """
    pid = container["State"].get("Pid")
    if (sys.platform != "linux" or type(pid) is not int or pid <= 0
            or container["HostConfig"].get("PidMode", "") not in {"", "private"}):
        raise GenerationUnavailable("isolated native Docker process identity unavailable")
    proc = Path("/proc") / str(pid)
    if (proc / "ns/pid").stat().st_ino != proof.pid_namespace_inode:
        raise GenerationUnavailable("HTTP receiver is outside the inspected container")
    process = proc / "root/proc" / str(proof.process_pid)
    raw = (process / "stat").read_text(encoding="ascii")
    if (len(raw) > 4096 or process_start_ticks(raw, pid=proof.process_pid) != proof.process_start_ticks
            or (process / "ns/pid").stat().st_ino != proof.pid_namespace_inode):
        raise GenerationUnavailable("inspected receiver process start identity differs")


class FixedDockerCommands:
    """A bounded native command transport. Tests inject a CPU-only fake instead."""
    def __init__(self, config: UnitControlConfig):
        self.config = config

    def run(self, kind: Literal["docker", "compose"], argv: tuple[str, ...]) -> bytes:
        if sys.platform != "linux":
            raise GenerationUnavailable("native Docker control requires qualified Linux")
        ref = self.config.docker if kind == "docker" else self.config.compose_binary
        executable = read_bound(ref).absolute()
        if any(p.is_symlink() for p in (executable, *executable.parents)):
            raise GenerationUnavailable("Docker executable must be directly pinned")
        env = {"PATH": "/usr/bin:/bin", "HOME": str(Path(self.config.control_root).absolute()),
               "DOCKER_HOST": "unix://" + self.config.docker_socket, "LANG": "C.UTF-8",
               "COMPOSE_INTERACTIVE_NO_CLI": "1", "COMPOSE_MENU": "0"}
        # A separate process installs limits; no unsafe threaded preexec_fn.
        wrapper = ("import os,resource,sys;resource.setrlimit(resource.RLIMIT_FSIZE,(8388608,8388608));"
                   "resource.setrlimit(resource.RLIMIT_AS,(1073741824,1073741824));"
                   "os.execv(sys.argv[1],sys.argv[1:])")
        command = [sys.executable, "-I", "-c", wrapper, str(executable), *argv]
        with tempfile.TemporaryFile() as output:
            proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output,
                stderr=subprocess.DEVNULL, env=env, cwd=self.config.control_root, start_new_session=True)
            try:
                code = proc.wait(timeout=self.config.command_timeout_seconds)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
                raise GenerationUnavailable("Docker control exceeded its deadline") from None
            if code != 0 or output.tell() > MAX_COMMAND_BYTES:
                raise GenerationUnavailable("Docker control failed or exceeded output budget")
            output.seek(0)
            value = output.read(MAX_COMMAND_BYTES + 1)
        read_bound(ref)
        if len(value) > MAX_COMMAND_BYTES:
            raise GenerationUnavailable("Docker control output exceeds bound")
        return value


class CoreUnitControl:
    def __init__(self, config: UnitControlConfig, *, commands=None, http=None, process_probe=None):
        self.config = config
        self.root = Path(config.control_root).absolute()
        if not self.root.is_dir() or any(p.is_symlink() for p in (self.root, *self.root.parents)):
            raise GenerationUnavailable("operator control root must already exist and be direct")
        self.commands = commands or FixedDockerCommands(config)
        self.http = http
        self.process_probe = process_probe or verify_native_process
        self.releases = {r.sha256: r for r in config.releases}
        self.selector = self.root / config.selected_release
        self._expected_instances = {}

    def selected(self) -> ComposeRelease:
        if self.selector.is_symlink() or self.selector.stat().st_size > 128:
            raise GenerationUnavailable("receiver release selector is unsafe")
        key = self.selector.read_text(encoding="ascii").strip()
        if key not in self.releases:
            raise GenerationUnavailable("receiver release is not operator-approved")
        return self.releases[key]

    def _compose(self, release: ComposeRelease):
        raw = bound_json(release.compose)
        if (not isinstance(raw, dict) or set(raw.get("services", {})) != set(UNITS)
                or raw.get("name", release.project) != release.project
                or set(raw) - {"name", "services", "networks", "volumes", "secrets", "configs"}):
            raise GenerationUnavailable("release must contain exactly the registered receiver services")
        for unit, spec in raw["services"].items():
            allowed = {"image", "user", "read_only", "tmpfs", "cap_drop", "security_opt", "networks",
                "logging", "restart", "command", "environment", "volumes", "ports", "mem_limit", "labels",
                "stop_grace_period", "init", "cpus"}
            if (not isinstance(spec, dict) or set(spec) & {"build", "develop", "extends", "env_file", "pre_start", "post_start", "pre_stop", "post_stop"}
                    or set(spec) - allowed
                    or spec.get("depends_on") or spec.get("privileged") or spec.get("network_mode")
                    or spec.get("pid") or spec.get("entrypoint")
                    or not isinstance(spec.get("image"), str)
                    or not re.fullmatch(r"[^\s$]+@sha256:[0-9a-f]{64}", spec["image"])
                    or type(spec.get("mem_limit")) is not int or not 0 < spec["mem_limit"] <= 16 * 1024**3
                    or type(spec.get("cpus")) not in {int, float} or not 0 < spec["cpus"] <= 4
                    or spec.get("read_only") is not True or spec.get("cap_drop") != ["ALL"]):
                raise GenerationUnavailable("receiver release has an unregistered execution capability")
            mounts = spec.get("volumes", [])
            seen = set()
            required = {str(self.root): True, str(self.root / "admission.lock"): False}
            for mount in mounts:
                if not isinstance(mount, dict):
                    raise GenerationUnavailable("receiver mounts require explicit long syntax")
                source, target = mount.get("source"), mount.get("target")
                if (not isinstance(mount, dict) or set(mount) - {"type", "source", "target", "read_only", "bind"}
                        or mount.get("type") != "bind" or not isinstance(source, str) or source != target
                        or not Path(source).is_absolute() or ".." in Path(source).parts
                        or mount.get("bind") != {"create_host_path": False} or target in seen
                        or mount.get("read_only") is not (target != str(self.root / "admission.lock"))
                        or not Path(source).exists() or any(p.is_symlink() for p in (Path(source), *Path(source).parents))):
                    raise GenerationUnavailable("receiver mount is indirect, mutable or outside the fixed host mapping")
                seen.add(target)
            if not set(required) <= seen:
                raise GenerationUnavailable("receivers require read-only control state and the shared gate file")
        api = raw["services"]["api"]
        mcp = raw["services"]["mcp"]
        mcp_env = mcp.get("environment", {})
        if (api.get("command") != ["api", "serve", "--host", "0.0.0.0", "--port", str(self.config.api_container_port)]
                or api.get("environment", {}).get("VKM_API_PROFILE") != "production"
                or not api.get("environment", {}).get("VKM_UPDATE_RUNTIME_FILE")
                or mcp.get("command") != ["mcp", "serve", "--kind", "read", "--host", "0.0.0.0", "--port", str(self.config.mcp_container_port)]
                or not isinstance(mcp_env, dict) or set(mcp_env) - {"VKM_API_URL", "VKM_API_TOKEN_FILE",
                    "VKM_MCP_TOKEN_FILE", "VKM_DEPLOYMENT_TOKEN_FILE", "VKM_DEPLOYMENT_GATE_FILE",
                    "VKM_MCP_ALLOWED_HOSTS", "VKM_LOG_LEVEL"}
                or mcp_env.get("VKM_API_URL") != "http://api:" + str(self.config.api_container_port)
                or any(not mcp_env.get(k) for k in ("VKM_API_TOKEN_FILE", "VKM_MCP_TOKEN_FILE", "VKM_DEPLOYMENT_TOKEN_FILE"))):
            raise GenerationUnavailable("only the qualified production API and read MCP are allowed")
        if mcp_env.get("VKM_DEPLOYMENT_GATE_FILE") != str(self.root / "admission.lock"):
            raise GenerationUnavailable("read MCP must bind the registered shared admission gate")
        if b"$" in canonical_bytes(raw):
            raise GenerationUnavailable("receiver release must be fully rendered without interpolation")
        return raw

    def _inspect(self, release: ComposeRelease, *, native=False) -> dict:
        result = {}
        for unit in UNITS:
            name = release.project + "-" + unit + "-1"
            raw = strict_json(self.commands.run("docker", ("inspect", "--type", "container", name)))
            if not isinstance(raw, list) or len(raw) != 1:
                raise GenerationUnavailable("one native container per receiver is required")
            fingerprint = container_fingerprint(raw[0], project=release.project, unit=unit)
            if fingerprint != release.units[unit].model_dump(mode="json"):
                raise GenerationUnavailable("actual image/config differs from the qualified release")
            result[unit] = raw[0] if native else fingerprint
        return result

    def _process_before_restart(self, release):
        result = {}
        for unit in UNITS:
            try:
                raw = strict_json(self.commands.run("docker", ("inspect", "--type", "container", release.project + "-" + unit + "-1")))
            except GenerationUnavailable:
                continue  # a failed candidate may have no surviving container
            if (not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0].get("Id"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", raw[0]["Id"])):
                raise GenerationUnavailable("native restart boundary is invalid")
            result[unit] = raw[0]["Id"]
        return result

    def _joined_proof(self, manifest, *, gate, admission_open=None):
        release = self.selected()
        before = self._inspect(release, native=True)
        api = before["api"]
        for unit, url, port in (("api", self.config.receiver_url, self.config.api_container_port),
                              ("mcp", self.config.mcp_receiver_url, self.config.mcp_container_port)):
            host_port = urlsplit(url).port or (443 if url.startswith("https:") else 80)
            endpoints = before[unit].get("NetworkSettings", {}).get("Ports", {}).get(str(port) + "/tcp")
            if endpoints != [{"HostIp": "127.0.0.1", "HostPort": str(host_port)}]:
                raise GenerationUnavailable("receiver endpoint differs from the native published binding")
        proof = self._proof(manifest, gate=gate, admission_open=admission_open)
        self.process_probe(api, proof)
        mcp = self._mcp_proof(manifest, gate=gate, admission_open=admission_open)
        if mcp.upstream.identity.instance != proof.instance:
            raise GenerationUnavailable("read MCP is connected to another API process")
        self.process_probe(api, mcp.upstream.identity)
        self.process_probe(before["mcp"], mcp)
        after = self._inspect(release, native=True)
        for unit in UNITS:
            identity = lambda raw: (raw.get("Id"), raw["State"].get("Pid"), raw["State"].get("StartedAt"))
            if not all(identity(before[unit])) or identity(before[unit]) != identity(after[unit]):
                raise GenerationUnavailable("native receiver changed during identity proof")
        self._last_native = after
        return proof

    def _mcp_proof(self, manifest, *, gate, admission_open):
        import hmac
        import httpx
        import secrets
        from vkm_corpus.api.production import read_tool_names
        release = self.selected()
        token = operator_token(Path(self.config.operator_token_file))
        challenge = ReceiverChallenge(nonce=secrets.token_hex(32))
        client = self.http or httpx.Client(timeout=35, follow_redirects=False, trust_env=False)
        try:
            with client.stream("POST", self.config.mcp_receiver_url.rstrip("/") + RECEIVER_ROUTE,
                    headers={"Authorization": "Bearer " + token}, json=challenge.model_dump()) as response:
                if response.status_code != 200:
                    raise GenerationUnavailable("actual read MCP receiver proof unavailable")
                if response.headers.get("content-encoding") not in {None, "identity"}:
                    raise GenerationUnavailable("compressed identity proofs are forbidden")
                raw = bytearray()
                for chunk in response.iter_bytes(chunk_size=64 * 1024):
                    raw.extend(chunk)
                    if len(raw) > MAX_PROOF_BYTES:
                        raise GenerationUnavailable("MCP receiver proof exceeds bound")
            signed = SignedMcpReceiverIdentity.model_validate(strict_json(bytes(raw)))
            item = signed.identity
            if (not hmac.compare_digest(signed.hmac_sha256, sign_mcp_identity(item, token).hmac_sha256)
                    or item.nonce != challenge.nonce or item.code_sha256 != release.code_sha256
                    or item.dependencies_sha256 != release.dependencies_sha256
                    or item.read_contract_sha256 != record_hash(sorted(read_tool_names()))
                    or item.api_url != "http://api:" + str(self.config.api_container_port)
                    or item.upstream.identity.read_principal_sha256 != release.mcp_read_principal_sha256
                    or item.read_credential_sha256 != item.upstream.identity.read_credential_sha256
                    or (item.gate_device, item.gate_inode) != gate
                    or (admission_open is not None and item.admission_open is not admission_open)):
                raise GenerationUnavailable("read MCP receiver code/contract/upstream differs")
            verify_identity(canonical_bytes(item.upstream), token=token, challenge=challenge, manifest=manifest,
                runtime_sha256=release.runtime_config_sha256, code_sha256=release.code_sha256,
                dependencies_sha256=release.dependencies_sha256, access_sha256=release.access_sha256,
                gate=gate, admission_open=admission_open)
            return item
        finally:
            if self.http is None:
                client.close()

    def capture(self):
        release = self.selected()
        self._compose(release)
        return {"release_sha256": release.sha256, "units": self._inspect(release)}

    def selection_binding(self):
        # The durable intent records the immutable desired configuration. Native
        # containers are independently inspected before plan and after rebind.
        # Reading a partial intent must not require the failed candidate to run.
        release = self.selected()
        return {"release_sha256": release.sha256,
                "units": {k: p.model_dump(mode="json") for k, p in release.units.items()}}

    def prove_current(self, manifest, *, gate):
        self.capture()
        proof = self._joined_proof(manifest, gate=gate, admission_open=True)
        self._expected_instances["api"] = proof.instance
        return proof

    def select(self, release_sha256: str, *, fence):
        fence()
        if release_sha256 not in self.releases:
            raise GenerationUnavailable("unapproved receiver release")
        target = self.releases[release_sha256]
        self._compose(target)
        write_bytes(self.root / "tmp", self.selector, (target.sha256 + "\n").encode("ascii"), overwrite=True)
        fence()

    def restore(self, binding: dict, *, fence):
        if (set(binding) != {"release_sha256", "units"} or binding["release_sha256"] not in self.releases
                or binding["units"] != {k: p.model_dump(mode="json") for k, p in self.releases[binding["release_sha256"]].units.items()}):
            raise GenerationUnavailable("restore binding is not the exact approved previous release")
        self.select(binding["release_sha256"], fence=fence)

    def _proof(self, manifest: GenerationManifest, *, gate, admission_open=None):
        import httpx
        import secrets
        release = self.selected()
        token = operator_token(Path(self.config.operator_token_file))
        challenge = ReceiverChallenge(nonce=secrets.token_hex(32))
        # A persistent caller-owned client is used by fixtures; production owns
        # this verified, nonredirecting, environment-independent short-lived one.
        client = self.http or httpx.Client(timeout=10, follow_redirects=False, trust_env=False)
        try:
            with client.stream("POST", self.config.receiver_url.rstrip("/") + RECEIVER_ROUTE,
                    headers={"Authorization": "Bearer " + token}, json=challenge.model_dump()) as response:
                if response.status_code != 200:
                    raise GenerationUnavailable("actual receiver identity is unavailable")
                if response.headers.get("content-encoding") not in {None, "identity"}:
                    raise GenerationUnavailable("compressed identity proofs are forbidden")
                data = bytearray()
                for chunk in response.iter_bytes(chunk_size=64 * 1024):
                    data.extend(chunk)
                    if len(data) > MAX_PROOF_BYTES:
                        raise GenerationUnavailable("receiver identity exceeds bound")
            return verify_identity(bytes(data), token=token, challenge=challenge, manifest=manifest,
                runtime_sha256=release.runtime_config_sha256, code_sha256=release.code_sha256,
                dependencies_sha256=release.dependencies_sha256, access_sha256=release.access_sha256,
                gate=gate, admission_open=admission_open)
        finally:
            if self.http is None:
                client.close()

    def rebind(self, manifest: GenerationManifest, *, gate: tuple[int, int]):
        release = self.selected()
        compose = self._compose(release)
        # Images must already exist, no pull/build and no other services/cleanup.
        for unit, pin in release.units.items():
            raw = strict_json(self.commands.run("docker", ("image", "inspect", compose["services"][unit]["image"])))
            if not isinstance(raw, list) or len(raw) != 1 or raw[0].get("Id") != pin.image_id:
                raise GenerationUnavailable("qualified receiver image is not locally available")
        before = self._process_before_restart(release)
        self.commands.run("compose", ("--env-file", "/dev/null", "--project-name", release.project, "--project-directory", str(Path(release.compose.path).parent),
            "--file", str(read_bound(release.compose)), "up", "--detach", "--no-deps", "--no-build", "--pull", "never", "--force-recreate", *UNITS))
        deadline = time.monotonic() + self.config.restart_timeout_seconds
        while True:
            try:
                proof = self._joined_proof(manifest, gate=gate, admission_open=False)
                if any(self._last_native[unit]["Id"] == identity for unit, identity in before.items()):
                    raise GenerationUnavailable("receiver recreation returned a previous container")
                if proof.instance == self._expected_instances.get("api"):
                    raise GenerationUnavailable("receiver restart returned the previous process")
                self._expected_instances["api"] = proof.instance
                return proof
            except (OSError, ValueError, GenerationUnavailable):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise GenerationUnavailable("new receiver did not qualify before deadline") from None
                time.sleep(min(.05, remaining))

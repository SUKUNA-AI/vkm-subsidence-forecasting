"""Fixed nft inet TCP seal for the one-time legacy boundary.

No rule, command, hook or expression is accepted from a manifest. The seal is
deliberately wider than one DNAT address: the approved host/container TCP ports
are blocked on every non-loopback interface, in both directions and families.
This catches existing streams as well as new connections. Software nft hooks
cannot attest hardware/flow offload; such a host is refused. Runtime/reboot
qualification is separate from CPU tests and from observing a present table.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import platform
import re
import signal
import stat
import subprocess
import tempfile
from typing import Literal

from pydantic import Field, model_validator

from vkm_corpus.update.generation import GenerationUnavailable
from vkm_corpus.update.operator_units import strict_json
from vkm_corpus.update.runtime import BoundFile, read_bound
from vkm_evidence.contracts import Sha256, StrictModel, canonical_bytes, record_hash

TABLE = "vkm_first_live_seal"
CHAINS = {"prerouting": -150, "input": -150, "forward": -150, "output": -150}
MAX_NATIVE = 2 * 1024 * 1024


class FrontdoorProfile(StrictModel):
    schema_version: Literal["vkm-first-live-frontdoor/1"] = "vkm-first-live-frontdoor/1"
    scope: Literal["SYNTHETIC", "FIRST_LIVE_PRODUCTION"]
    nft: BoundFile
    ip: BoundFile | None = None
    tc: BoundFile | None = None
    tcp_ports: tuple[int, ...] = Field(min_length=1, max_length=16)
    trusted_bridges: tuple[str, ...] = Field(default=(), max_length=4)
    timeout_seconds: float = Field(default=10, gt=0, le=30)
    # This is a limitation, not a configurable claim of successful reboot.
    reboot_safety: Literal["NOT_QUALIFIED"] = "NOT_QUALIFIED"

    @model_validator(mode="after")
    def _fixed(self):
        if (Path(self.nft.path).name != "nft" or not Path(self.nft.path).is_absolute()
                or ".." in Path(self.nft.path).parts
                or tuple(sorted(set(self.tcp_ports))) != self.tcp_ports
                or any(type(p) is not int or not 1024 <= p <= 65535 for p in self.tcp_ports)):
            raise ValueError("fixed nft executable and sorted unique non-system ports required")
        if self.scope == "FIRST_LIVE_PRODUCTION" and (self.ip is None or self.tc is None):
            raise ValueError("native host qualification requires pinned ip and tc observers")
        if (tuple(sorted(set(self.trusted_bridges))) != self.trusted_bridges
                or any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", name) or name == "lo" for name in self.trusted_bridges)
                or self.scope == "FIRST_LIVE_PRODUCTION" and not self.trusted_bridges):
            raise ValueError("exact retained Docker bridge interfaces are required")
        for name in ("ip", "tc"):
            ref = getattr(self, name)
            if ref is not None and (Path(ref.path).name != name or not Path(ref.path).is_absolute() or ".." in Path(ref.path).parts):
                raise ValueError("fixed native network observer executable required")
        return self


def _match(left, right, op="=="):
    return {"match": {"op": op, "left": left, "right": right}}


def seal_objects(profile: FrontdoorProfile, *, closed: bool):
    """Canonical nft objects; order is also part of the native observation."""
    result = [{"table": {"family": "inet", "name": TABLE}}]
    for chain, priority in CHAINS.items():
        result.append({"chain": {"family": "inet", "table": TABLE, "name": chain,
            "type": "filter", "hook": chain, "prio": priority, "policy": "accept"}})
        if not closed:
            continue
        # The native read MCP must reach API through its retained bridge while
        # ingress is sealed. Forward checks BOTH sides, so a reply arriving from
        # that bridge cannot escape through a physical interface on an existing
        # connection. No address/CIDR supplied by a client is trusted here.
        interfaces = ("iifname", "oifname") if chain == "forward" else ("oifname",) if chain == "output" else ("iifname",)
        for interface in interfaces:
            for field in ("sport", "dport"):
                result.append({"rule": {"family": "inet", "table": TABLE, "chain": chain,
                    "expr": [_match({"meta": {"key": interface}}, {"set": ["lo", *profile.trusted_bridges]}, "!="),
                        _match({"meta": {"key": "l4proto"}}, "tcp"),
                        _match({"payload": {"protocol": "tcp", "field": field}},
                               {"set": list(profile.tcp_ports)}), {"drop": None}]}})
    return result


class FixedNft:
    """Bounded privileged local executable. Never uses a shell or sudo."""
    def __init__(self, profile):
        self.profile = profile

    def run(self, operation, body=None):
        if platform.system() != "Linux" or operation not in {"read", "check", "apply", "links", "routes4", "routes6", "qdiscs", "filters"}:
            raise GenerationUnavailable("unsupported native frontdoor operation")
        status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
        if not int(status["CapEff"].strip(), 16) & (1 << 12):
            raise GenerationUnavailable("frontdoor requires explicit CAP_NET_ADMIN runner")
        ref = self.profile.ip if operation in {"links", "routes4", "routes6"} else self.profile.tc if operation in {"qdiscs", "filters"} else self.profile.nft
        if ref is None:
            raise GenerationUnavailable("native network observer is unregistered")
        executable = Path(ref.path)
        if any(p.is_symlink() for p in (executable, *executable.parents)):
            raise GenerationUnavailable("indirect nft executable")
        args = ["-j"]
        if operation == "read": args += ["list", "ruleset"]
        elif operation in {"check", "apply"}: args += (["-c"] if operation == "check" else []) + ["-f", "-"]
        elif operation == "links": args += ["-d", "link", "show"]
        elif operation in {"routes4", "routes6"}: args += ["-4" if operation == "routes4" else "-6", "-d", "route", "show", "table", "all"]
        elif operation == "qdiscs": args += ["qdisc", "show"]
        else:
            if (not isinstance(body, tuple) or len(body) != 2 or not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", body[0])
                    or body[1] not in {"root", "ingress", "egress"}):
                raise GenerationUnavailable("fixed native interface observer required")
            args += ["filter", "show", "dev", body[0], body[1]]
            body = None
        if body is not None and (not isinstance(body, bytes) or len(body) > 65536):
            raise GenerationUnavailable("frontdoor transaction exceeds bound")
        wrapper = ("import os,resource,sys;resource.setrlimit(resource.RLIMIT_FSIZE,(2097153,2097153));"
                   "resource.setrlimit(resource.RLIMIT_AS,(268435456,268435456));os.execv(sys.argv[1],sys.argv[1:])")
        import sys
        # Capture before hash; exec the held inode, not a pathname that can be
        # replaced between read_bound and exec. Operator-owned immutable native
        # code is assumed; a privileged in-place kernel/RAM adversary is outside
        # this operational guarantee.
        from vkm_corpus.update.native_files import NativeFileWatch
        watch = NativeFileWatch((executable,))
        fd = None
        try:
            fd = os.open(executable, os.O_RDONLY | os.O_NOFOLLOW)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o022:
                raise GenerationUnavailable("native executable is not independently immutable")
            with os.fdopen(os.dup(fd), "rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != ref.sha256:
                raise GenerationUnavailable("held native executable bytes differ")
            watch.check()
            return self._execute(operation, body, ["/proc/self/fd/" + str(fd), *args], wrapper, fd, watch)
        finally:
            if fd is not None:
                os.close(fd)
            watch.close()

    def _execute(self, operation, body, args, wrapper, fd, watch):
        import sys
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as source:
            if body:
                source.write(body)
            source.seek(0)
            proc = subprocess.Popen([sys.executable, "-I", "-c", wrapper, *args], stdin=source,
                stdout=output, stderr=subprocess.DEVNULL, start_new_session=True,
                pass_fds=(fd,),
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"})
            try:
                code = proc.wait(timeout=self.profile.timeout_seconds)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    raise GenerationUnavailable("frontdoor killed process could not be reaped") from None
                raise GenerationUnavailable("frontdoor command deadline exceeded") from None
            except BaseException:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=5)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    pass
                raise
            if code or output.tell() > MAX_NATIVE:
                raise GenerationUnavailable("frontdoor native command failed")
            output.seek(0)
            result = output.read(MAX_NATIVE + 1)
        watch.check()
        if len(result) > MAX_NATIVE:
            raise GenerationUnavailable("frontdoor native output exceeds bound")
        return result


def _without_handles(value):
    # Only nft-assigned object handles are non-semantic. Do not strip rules,
    # counters, comments or unknown fields to manufacture expected equality.
    return {kind: {k: v for k, v in data.items() if k != "handle"}
            for kind, data in value.items()}


class NativeFrontdoor:
    def __init__(self, profile: FrontdoorProfile, *, _commands=None):
        if _commands is not None and profile.scope != "SYNTHETIC":
            raise GenerationUnavailable("synthetic nft transport cannot seal production")
        self.profile = profile
        self.commands = _commands or FixedNft(profile)
        self.pid = os.getpid()

    def inventory(self):
        if self.pid != os.getpid():
            raise GenerationUnavailable("frontdoor ownership crossed process")
        self.host_guard()
        raw = self.commands.run("read")
        if len(raw) > MAX_NATIVE:
            raise GenerationUnavailable("frontdoor ruleset exceeds bound")
        value = strict_json(raw)
        if set(value) != {"nftables"} or not isinstance(value["nftables"], list) or len(value["nftables"]) > 10000:
            raise GenerationUnavailable("unqualified nft ruleset inventory")
        selected = []
        for item in value["nftables"]:
            if not isinstance(item, dict) or len(item) != 1:
                raise GenerationUnavailable("unknown nft object")
            kind, obj = next(iter(item.items()))
            if kind == "metainfo":
                if obj.get("json_schema_version") != 1:
                    raise GenerationUnavailable("unsupported nft JSON version")
                continue
            # A flowtable or hardware-offloaded chain can bypass software hooks.
            if kind == "flowtable" or kind not in {"table", "chain", "rule", "set", "map", "counter", "quota", "limit", "ct helper", "ct timeout", "ct expectation"}:
                raise GenerationUnavailable("unqualified nft offload/object kind")
            if (not isinstance(obj, dict) or obj.get("flags")
                    or "flow" in canonical_bytes(obj).decode("utf-8").lower()):
                raise GenerationUnavailable("nft flow/offload semantics are unsupported")
            if kind == "chain" and "hook" in obj:
                if obj.get("family") not in {"ip", "ip6", "inet", "arp", "bridge"} or obj["hook"] not in {
                        "prerouting", "input", "forward", "output", "postrouting"}:
                    raise GenerationUnavailable("unknown native network hook")
                if obj.get("type") == "nat" and obj["hook"] in {"prerouting", "output"} and (
                        type(obj.get("prio")) is not int or obj["prio"] <= -150):
                    raise GenerationUnavailable("unqualified translation before the fixed packet seal")
            if kind == "rule":
                encoded = canonical_bytes(obj).decode("utf-8").lower()
                if any('"' + key + '"' in encoded for key in ("tproxy", "fwd", "dup", "queue", "notrack", "mangle", "synproxy")):
                    raise GenerationUnavailable("unqualified packet rewrite or alternate delivery path")
            if (kind == "table" and obj.get("name") == TABLE) or obj.get("table") == TABLE:
                if obj.get("family") != "inet" or kind not in {"table", "chain", "rule"}:
                    raise GenerationUnavailable("seal namespace belongs to another native object")
                selected.append(_without_handles(item))
        return selected

    def host_guard(self):
        """No XDP, tc classifiers, route/flow offload or unknown qdisc path."""
        if self.profile.scope == "SYNTHETIC" and self.profile.ip is None:
            return  # explicit test-only nft renderer, never a production proof
        def read(kind, body=None):
            raw = self.commands.run(kind, body)
            if len(raw) > MAX_NATIVE:
                raise GenerationUnavailable("host network inventory exceeds bound")
            value = strict_json(raw)
            if not isinstance(value, list) or len(value) > 4096:
                raise GenerationUnavailable("invalid native host network inventory")
            return value
        links = read("links")
        names = [item.get("ifname") for item in links if isinstance(item, dict)]
        if (len(names) != len(links) or "lo" not in names or len(names) < 2 or len(set(names)) != len(names)
                or any(not isinstance(n, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", n) for n in names)
                or any("xdp" in item for item in links)
                or any(item.get("master") and item.get("linkinfo", {}).get("info_kind") != "veth" for item in links)):
            raise GenerationUnavailable("unsupported or incomplete native link/XDP inventory")
        if any(item["ifname"] in self.profile.trusted_bridges and item.get("linkinfo", {}).get("info_kind") != "bridge" for item in links):
            raise GenerationUnavailable("trusted interface is not an actual native bridge")
        for kind in ("routes4", "routes6"):
            for route in read(kind):
                if not isinstance(route, dict) or any(x in canonical_bytes(route).decode().lower() for x in ("offload", "trap", "bpf", "encap")):
                    raise GenerationUnavailable("unsupported route/offload path")
        for qdisc in read("qdiscs"):
            if (not isinstance(qdisc, dict) or qdisc.get("kind") not in {"noqueue", "mq", "fq_codel", "fq", "pfifo_fast"}
                    or qdisc.get("dev") not in names or qdisc.get("offload") or qdisc.get("hw_offload")):
                raise GenerationUnavailable("unknown tc execution/offload path")
        for name in names:
            for hook in ("root", "ingress", "egress"):
                if read("filters", (name, hook)):
                    raise GenerationUnavailable("native tc filters bypass the qualified host profile")

    def observe(self, *, closed):
        selected = self.inventory()
        if selected != seal_objects(self.profile, closed=closed):
            raise GenerationUnavailable("actual frontdoor rules differ from exact fixed seal")
        return {"schema": "vkm-native-frontdoor-observation/1", "profile_sha256": record_hash(self.profile),
            "rules_sha256": record_hash(selected), "state": "CLOSED" if closed else "OPEN",
            "families": ["IPv4", "IPv6"], "existing_flows": "BLOCKED_BY_PACKET_RULES" if closed else "NOT_BLOCKED",
            "reboot_safety": "NOT_QUALIFIED"}

    def set_closed(self, closed: bool):
        if type(closed) is not bool:
            raise GenerationUnavailable("literal frontdoor state required")
        current = self.inventory()
        if current not in ([], seal_objects(self.profile, closed=True), seal_objects(self.profile, closed=False)):
            raise GenerationUnavailable("unowned or changed seal namespace")
        # The kernel commits the complete JSON batch atomically. Check never
        # publishes a partial rule. Unknown old rules are never deleted.
        commands = ([{"delete": {"table": {"family": "inet", "name": TABLE}}}] if current else [])
        commands += [{"add": obj} for obj in seal_objects(self.profile, closed=closed)]
        body = canonical_bytes({"nftables": commands})
        self.commands.run("check", body)
        if self.inventory() != current:
            raise GenerationUnavailable("frontdoor changed between check and commit")
        self.commands.run("apply", body)
        return self.observe(closed=closed)

    def close(self):
        return self.set_closed(True)

    def open(self):
        # Only the source-owned controller calls this after verified admission.
        return self.set_closed(False)


class BootSealRegistration(StrictModel):
    """Fixed boot ordering recipe; installation/reboot are external actions."""
    schema_version: Literal["vkm-frontdoor-boot-registration/1"] = "vkm-frontdoor-boot-registration/1"
    profile: BoundFile
    python: BoundFile
    systemctl: BoundFile
    service_file: BoundFile
    docker_dropin: BoundFile
    code_sha256: Sha256
    dependencies_sha256: Sha256

    @model_validator(mode="after")
    def _paths(self):
        for ref in (self.profile, self.python, self.systemctl, self.service_file, self.docker_dropin):
            if not re.fullmatch(r"/[A-Za-z0-9_./-]+", ref.path) or ".." in Path(ref.path).parts:
                raise ValueError("fixed ASCII host paths without shell/systemd expansion required")
        if (Path(self.systemctl.path).name != "systemctl"
                or self.service_file.path != "/etc/systemd/system/vkm-first-live-seal.service"
                or self.docker_dropin.path != "/etc/systemd/system/docker.service.d/vkm-first-live-seal.conf"):
            raise ValueError("only fixed host boot service and Docker dependency are registered")
        return self

    def argv(self):
        return [self.python.path, "-I", "-m", "vkm_corpus.update.frontdoor", "boot-close",
            "--profile", self.profile.path, "--sha256", self.profile.sha256,
            "--code", self.code_sha256, "--dependencies", self.dependencies_sha256]


def boot_files(registration):
    """Exact unit bytes. No supplied ExecStart, shell, hooks or substitutions."""
    # Do not order a default-dependency service before docker.socket: sockets
    # precede basic.target whereas services follow it, creating a boot cycle.
    # The socket may listen; only Docker.service can restore/start containers.
    unit = ("[Unit]\nDescription=VKM fixed first-LIVE ingress seal\nBefore=docker.service\n"
        "[Service]\nType=oneshot\nRemainAfterExit=yes\nUser=root\nNoNewPrivileges=yes\n"
        "CapabilityBoundingSet=CAP_NET_ADMIN\nAmbientCapabilities=CAP_NET_ADMIN\n"
        "ExecStart=" + " ".join(registration.argv()) + "\n[Install]\nWantedBy=multi-user.target\n")
    dropin = "[Unit]\nRequires=vkm-first-live-seal.service\nAfter=vkm-first-live-seal.service\n"
    return unit.encode("ascii"), dropin.encode("ascii")


def boot_checkpoint(registration_ref):
    """Read-only pre-reboot checkpoint; operator persists it as immutable JSON."""
    from vkm_corpus.update.operator_units import bound_json
    registration = BootSealRegistration.model_validate(bound_json(registration_ref))
    if platform.system() != "Linux":
        raise GenerationUnavailable("host boot drill requires Linux")
    return {"schema": "vkm-frontdoor-preboot/1", "registration_sha256": registration_ref.sha256,
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()}


def _validate_boot_state(registration, checkpoint, *, registration_sha256, boot_id, seal, docker):
    if (set(checkpoint) != {"schema", "registration_sha256", "boot_id"}
            or checkpoint["schema"] != "vkm-frontdoor-preboot/1"
            or checkpoint["registration_sha256"] != registration_sha256
            or not re.fullmatch(r"[0-9a-f-]{36}", checkpoint["boot_id"])
            or not re.fullmatch(r"[0-9a-f-]{36}", boot_id) or checkpoint["boot_id"] == boot_id):
        raise GenerationUnavailable("a different observed host boot is required")
    if (seal.get("FragmentPath") != registration.service_file.path or seal.get("DropInPaths")
            or seal.get("NeedDaemonReload") != "no" or seal.get("ActiveState") != "active"
            or seal.get("SubState") != "exited" or seal.get("Result") != "success"
            or seal.get("ExecMainStatus") != "0" or docker.get("NeedDaemonReload") != "no"
            or docker.get("ActiveState") != "active"
            or docker.get("DropInPaths") != registration.docker_dropin.path
            or "vkm-first-live-seal.service" not in docker.get("Requires", "").split()
            or "vkm-first-live-seal.service" not in docker.get("After", "").split()):
        raise GenerationUnavailable("actual boot dependency/unit state is not qualified")
    command = seal.get("ExecStart", "")
    match = re.search(r"argv\[\]=(.*?) ; ignore_errors=no", command)
    if match is None or match.group(1).split() != registration.argv():
        raise GenerationUnavailable("actually loaded boot command differs")
    try:
        completed = int(seal["ExecMainExitTimestampMonotonic"])
        started = int(docker["ExecMainStartTimestampMonotonic"])
    except (ValueError, KeyError):
        raise GenerationUnavailable("boot activation timestamps unavailable") from None
    if not 0 < completed < started:
        raise GenerationUnavailable("Docker started before the seal completed")
    return {"schema": "vkm-native-boot-ordering/1", "registration_sha256": registration_sha256,
        "boot_id": boot_id, "seal_completed_monotonic": completed, "docker_started_monotonic": started,
        "status": "ORDERING_OBSERVED_AFTER_REBOOT"}


def verify_boot_ordering(registration_ref, checkpoint_ref):
    """Fresh systemd/kernel proof; neither table presence nor JSON PASS suffices."""
    from vkm_corpus.update.operator_units import bound_json
    from vkm_corpus.update.admission import _ordinary_bytes
    if platform.system() != "Linux":
        raise GenerationUnavailable("native boot ordering requires Linux")
    reg = BootSealRegistration.model_validate(bound_json(registration_ref))
    for ref, expected in zip((reg.service_file, reg.docker_dropin), boot_files(reg)):
        data = _ordinary_bytes(Path(ref.path), 8192)
        if data != expected or hashlib.sha256(data).hexdigest() != ref.sha256:
            raise GenerationUnavailable("boot seal source/ordering files differ")
    read_bound(reg.python)
    profile = FrontdoorProfile.model_validate(bound_json(reg.profile))
    props = ("FragmentPath,DropInPaths,NeedDaemonReload,ActiveState,SubState,Result,ExecMainStatus,"
        "Requires,After,ExecStart,ExecMainExitTimestampMonotonic,ExecMainStartTimestampMonotonic")
    def observe(unit):
        # Read-only exact unit names. Output is bounded and never includes Env.
        from vkm_corpus.update.native_files import NativeFileWatch
        tool = Path(reg.systemctl.path)
        if any(p.is_symlink() for p in (tool, *tool.parents)):
            raise GenerationUnavailable("indirect boot observer executable")
        watch = NativeFileWatch((tool,))
        fd = None
        try:
            fd = os.open(tool, os.O_RDONLY | os.O_NOFOLLOW)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o022:
                raise GenerationUnavailable("boot observer is not independently immutable")
            with os.fdopen(os.dup(fd), "rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != reg.systemctl.sha256:
                    raise GenerationUnavailable("boot observer held bytes differ")
            watch.check()
            wrapper = ("import os,resource,sys;resource.setrlimit(resource.RLIMIT_FSIZE,(16385,16385));"
                       "resource.setrlimit(resource.RLIMIT_AS,(268435456,268435456));os.execv(sys.argv[1],sys.argv[1:])")
            raw = FixedNft(profile)._execute("boot-observe", None,
                ["/proc/self/fd/" + str(fd), "show", "--no-pager", "--property=" + props, unit], wrapper, fd, watch)
        finally:
            if fd is not None:
                os.close(fd)
            watch.close()
        if len(raw) > 16384:
            raise GenerationUnavailable("boot observer failed or exceeded bound")
        values = {}
        for line in raw.decode("utf-8").splitlines():
            key, sep, value = line.partition("=")
            if not sep or key in values:
                raise GenerationUnavailable("invalid systemd observer response")
            values[key] = value
        return values
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    proof = _validate_boot_state(reg, bound_json(checkpoint_ref), registration_sha256=registration_ref.sha256,
        boot_id=boot_id, seal=observe("vkm-first-live-seal.service"), docker=observe("docker.service"))
    if Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip() != boot_id:
        raise GenerationUnavailable("host boot changed during inspection")
    read_bound(reg.systemctl)
    return proof


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Fixed early boot CLOSED host seal")
    parser.add_argument("action", choices=("boot-close",))
    for name in ("profile", "sha256", "code", "dependencies"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args(argv)
    from vkm_corpus.update.first_live_native import operator_identity
    from vkm_corpus.update.operator_units import bound_json
    if operator_identity() != (args.code, args.dependencies):
        raise GenerationUnavailable("boot seal installed code/dependencies changed")
    profile = FrontdoorProfile.model_validate(bound_json(BoundFile(path=args.profile, sha256=args.sha256)))
    if profile.scope != "FIRST_LIVE_PRODUCTION":
        raise GenerationUnavailable("synthetic seal cannot protect a native boot")
    NativeFrontdoor(profile).close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

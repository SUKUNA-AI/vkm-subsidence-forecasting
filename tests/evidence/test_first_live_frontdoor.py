"""Pure nft JSON/state adversaries; no firewall command is executed."""
import copy
from pathlib import Path

import pytest

from vkm_corpus.update.frontdoor import FrontdoorProfile, NativeFrontdoor, TABLE, seal_objects
from vkm_corpus.update.generation import GenerationUnavailable
from vkm_corpus.update.operator_units import PublicReceiverBinding
from vkm_corpus.update.runtime import BoundFile
from vkm_evidence.contracts import canonical_bytes


class FakeNft:
    def __init__(self):
        self.objects = []
        self.calls = []
        self.on_check = None

    def run(self, operation, body=None):
        import json
        self.calls.append((operation, body))
        if operation == "read":
            return canonical_bytes({"nftables": [{"metainfo": {"json_schema_version": 1}}, *self.objects]})
        if operation == "check":
            if self.on_check:
                self.on_check()
            return b""
        assert operation == "apply"
        commands = json.loads(body)["nftables"]
        self.objects = [item["add"] for item in commands if "add" in item]
        return b""


def profile(tmp_path):
    return FrontdoorProfile(scope="SYNTHETIC", nft=BoundFile(path=str(tmp_path / "nft"), sha256="a" * 64),
        tcp_ports=(8000, 8765, 8766, 18000, 18765))


def test_exact_inet_seal_covers_existing_replies_before_dnat_and_keeps_reboot_unqualified(tmp_path):
    p, commands = profile(tmp_path), FakeNft()
    gate = NativeFrontdoor(p, _commands=commands)
    proof = gate.close()
    assert proof["families"] == ["IPv4", "IPv6"] and proof["reboot_safety"] == "NOT_QUALIFIED"
    objects = commands.objects
    chains = [r["chain"] for r in objects if "chain" in r]
    assert {c["hook"] for c in chains} == {"prerouting", "input", "forward", "output"}
    assert all(c["prio"] == -150 and c["family"] == "inet" for c in chains)
    rules = [r["rule"] for r in objects if "rule" in r]
    assert len(rules) == 10 and all(r["expr"][-1] == {"drop": None} for r in rules)
    assert {r["expr"][2]["match"]["left"]["payload"]["field"] for r in rules} == {"sport", "dport"}
    assert all("ct state" not in str(r) for r in rules)  # no established bypass
    assert gate.open()["state"] == "OPEN" and not any("rule" in x for x in commands.objects)
    assert [x[0] for x in commands.calls].count("apply") == 2


@pytest.mark.parametrize("kind", ["flowtable", "unknown", "netdev", "offload", "flow-expression", "early-dnat", "packet-rewrite", "queue", "wrong-family", "extra-rule"])
def test_unsupported_or_unowned_rules_never_mutated(tmp_path, kind):
    p, commands = profile(tmp_path), FakeNft()
    gate = NativeFrontdoor(p, _commands=commands)
    if kind == "flowtable": commands.objects = [{"flowtable": {"family": "inet", "name": "fast"}}]
    elif kind == "unknown": commands.objects = [{"opaque": {}}]
    elif kind == "netdev": commands.objects = [{"chain": {"family": "netdev", "hook": "ingress"}}]
    elif kind == "offload": commands.objects = [{"chain": {"family": "inet", "flags": ["offload"]}}]
    elif kind == "flow-expression": commands.objects = [{"rule": {"expr": [{"flow": {"op": "add"}}]}}]
    elif kind == "early-dnat": commands.objects = [{"chain": {"family": "inet", "type": "nat", "hook": "prerouting", "prio": -200}}]
    elif kind == "packet-rewrite": commands.objects = [{"rule": {"expr": [{"mangle": {"key": "tcp.dport", "value": 9999}}]}}]
    elif kind == "queue": commands.objects = [{"rule": {"expr": [{"queue": {"num": 1}}]}}]
    elif kind == "wrong-family": commands.objects = [{"table": {"family": "ip", "name": TABLE}}]
    else:
        commands.objects = seal_objects(p, closed=True)
        commands.objects.append({"rule": {"family": "inet", "table": TABLE, "chain": "input", "expr": [{"accept": None}]}})
    with pytest.raises(GenerationUnavailable): gate.close()
    assert not any(c[0] in {"check", "apply"} for c in commands.calls)


def test_ruleset_change_after_check_prevents_commit(tmp_path):
    commands = FakeNft()
    gate = NativeFrontdoor(profile(tmp_path), _commands=commands)
    commands.on_check = lambda: commands.objects.append({"table": {"family": "inet", "name": TABLE}})
    with pytest.raises(GenerationUnavailable, match="between check"):
        gate.close()
    assert not any(c[0] == "apply" for c in commands.calls)


def test_native_json_handles_are_only_ignored_field(tmp_path):
    commands = FakeNft()
    p = profile(tmp_path)
    commands.objects = copy.deepcopy(seal_objects(p, closed=True))
    for n, item in enumerate(commands.objects): next(iter(item.values()))["handle"] = n
    gate = NativeFrontdoor(p, _commands=commands)
    gate.observe(closed=True)
    commands.objects[-1]["rule"]["comment"] = "foreign mutation"
    with pytest.raises(GenerationUnavailable): gate.observe(closed=True)


@pytest.mark.parametrize("chain,iif,oif,sport,dport,dropped", [
    ("prerouting", "eth0", None, 40000, 18765, True),
    ("prerouting", "br-owned", None, 40000, 8000, False),
    ("forward", "br-owned", "br-owned", 40000, 8000, False),
    ("forward", "eth0", "br-owned", 40000, 8000, True),
    ("forward", "br-owned", "eth0", 8000, 40000, True),
    ("output", None, "eth0", 8765, 40000, True),
    ("output", None, "lo", 40000, 18765, False),
    ("forward", "br-unapproved", "br-owned", 40000, 8000, True),
])
def test_directional_packet_contract_preserves_private_mcp_but_blocks_external_existing_replies(
        tmp_path, chain, iif, oif, sport, dport, dropped):
    p = profile(tmp_path).model_copy(update={"trusted_bridges": ("br-owned",)})
    packet = {"iifname": iif, "oifname": oif, "l4proto": "tcp", "sport": sport, "dport": dport}
    def matches(expr):
        m = expr["match"]
        left = m["left"]
        key = left["meta"]["key"] if "meta" in left else left["payload"]["field"]
        right = m["right"]
        equal = packet[key] in right["set"] if isinstance(right, dict) else packet[key] == right
        return not equal if m["op"] == "!=" else equal
    rules = [r["rule"] for r in seal_objects(p, closed=True) if "rule" in r and r["rule"]["chain"] == chain]
    assert any(all(matches(x) for x in rule["expr"][:-1]) for rule in rules) is dropped


def test_production_cannot_inject_firewall_or_claim_reboot(tmp_path):
    p = profile(tmp_path)
    with pytest.raises(GenerationUnavailable):
        NativeFrontdoor(p.model_copy(update={"scope": "FIRST_LIVE_PRODUCTION"}), _commands=FakeNft())
    with pytest.raises(ValueError):
        FrontdoorProfile.model_validate({**p.model_dump(), "reboot_safety": "PASS"})


@pytest.mark.parametrize("address", ["0.0.0.0", "::", "127.0.0.1", "::1", "224.0.0.1", "fe80::1", "localhost", "10.0.0.1%eth0"])
def test_public_receiver_binding_never_accepts_wildcards_loopback_or_hostnames(address):
    with pytest.raises(ValueError): PublicReceiverBinding(unit="mcp", host_ip=address, host_port=8765)


def test_concrete_ipv4_ipv6_public_bindings_are_typed():
    assert PublicReceiverBinding(unit="api", host_ip="192.0.2.5", host_port=8000).host_ip == "192.0.2.5"
    assert PublicReceiverBinding(unit="mcp", host_ip="2001:db8::5", host_port=8765).unit == "mcp"


class HostNft(FakeNft):
    def __init__(self):
        super().__init__()
        self.native = {"links": [{"ifname": "lo"}, {"ifname": "eth0"}], "routes4": [], "routes6": [],
            "qdiscs": [{"dev": "lo", "kind": "noqueue"}, {"dev": "eth0", "kind": "fq_codel"}], "filters": []}

    def run(self, operation, body=None):
        if operation in self.native:
            self.calls.append((operation, body))
            return canonical_bytes(self.native[operation])
        return super().run(operation, body)


@pytest.mark.parametrize("hazard", ["xdp", "tc", "offload", "route", "missing-loopback", "duplicate-link"])
def test_host_paths_that_can_bypass_packet_hooks_block_before_mutation(tmp_path, hazard):
    p = profile(tmp_path).model_copy(update={"ip": BoundFile(path=str(tmp_path / "ip"), sha256="b" * 64),
        "tc": BoundFile(path=str(tmp_path / "tc"), sha256="c" * 64)})
    native = HostNft()
    if hazard == "xdp": native.native["links"][1]["xdp"] = {"id": 1}
    elif hazard == "tc": native.native["filters"] = [{"kind": "bpf"}]
    elif hazard == "offload": native.native["qdiscs"][1]["offload"] = True
    elif hazard == "route": native.native["routes6"] = [{"flags": ["offload"]}]
    elif hazard == "missing-loopback": native.native["links"] = [{"ifname": "eth0"}]
    else: native.native["links"].append({"ifname": "eth0"})
    with pytest.raises(GenerationUnavailable): NativeFrontdoor(p, _commands=native).close()
    assert not any(c[0] in {"check", "apply"} for c in native.calls)


def boot_fixture():
    from vkm_corpus.update.frontdoor import BootSealRegistration
    ref = lambda path: BoundFile(path=path, sha256="a" * 64)
    reg = BootSealRegistration(profile=ref("/opt/vkm/profile.json"), python=ref("/opt/vkm/bin/python3"),
        systemctl=ref("/usr/bin/systemctl"), service_file=ref("/etc/systemd/system/vkm-first-live-seal.service"),
        docker_dropin=ref("/etc/systemd/system/docker.service.d/vkm-first-live-seal.conf"),
        code_sha256="b" * 64, dependencies_sha256="c" * 64)
    checkpoint = {"schema": "vkm-frontdoor-preboot/1", "registration_sha256": "d" * 64,
        "boot_id": "11111111-1111-4111-a111-111111111111"}
    seal = {"FragmentPath": reg.service_file.path, "DropInPaths": "", "NeedDaemonReload": "no", "ActiveState": "active",
        "SubState": "exited", "Result": "success", "ExecMainStatus": "0", "ExecMainExitTimestampMonotonic": "100",
        "ExecStart": "{ path=" + reg.python.path + " ; argv[]=" + " ".join(reg.argv()) + " ; ignore_errors=no ; }"}
    docker = {"DropInPaths": reg.docker_dropin.path, "NeedDaemonReload": "no", "ActiveState": "active",
        "Requires": "vkm-first-live-seal.service", "After": "vkm-first-live-seal.service", "ExecMainStartTimestampMonotonic": "200"}
    return reg, checkpoint, seal, docker


def test_fixed_boot_recipe_has_no_socket_dependency_cycle_or_shell():
    from vkm_corpus.update.frontdoor import boot_files, _validate_boot_state
    reg, checkpoint, seal, docker = boot_fixture()
    unit, dropin = boot_files(reg)
    assert b"Before=docker.service\n" in unit and b"docker.socket" not in unit
    assert b"/bin/sh" not in unit and b"Requires=vkm-first-live-seal.service" in dropin
    result = _validate_boot_state(reg, checkpoint, registration_sha256="d" * 64,
        boot_id="22222222-2222-4222-a222-222222222222", seal=seal, docker=docker)
    assert result["status"] == "ORDERING_OBSERVED_AFTER_REBOOT"


@pytest.mark.parametrize("hazard", ["same-boot", "wrong-registration", "ignored-errors", "wrong-code", "reloaded",
    "extra-dropin", "no-dependency", "wrong-order", "not-exited", "failed-status"])
def test_boot_ordering_requires_actual_loaded_unit_and_native_order(hazard):
    from vkm_corpus.update.frontdoor import _validate_boot_state
    reg, checkpoint, seal, docker = boot_fixture()
    boot_id = "22222222-2222-4222-a222-222222222222"
    if hazard == "same-boot": boot_id = checkpoint["boot_id"]
    elif hazard == "wrong-registration": checkpoint["registration_sha256"] = "e" * 64
    elif hazard == "ignored-errors": seal["ExecStart"] = seal["ExecStart"].replace("ignore_errors=no", "ignore_errors=yes")
    elif hazard == "wrong-code": seal["ExecStart"] = seal["ExecStart"].replace("b" * 64, "e" * 64)
    elif hazard == "reloaded": docker["NeedDaemonReload"] = "yes"
    elif hazard == "extra-dropin": docker["DropInPaths"] += " /tmp/injection.conf"
    elif hazard == "no-dependency": docker["Requires"] = ""
    elif hazard == "wrong-order": docker["ExecMainStartTimestampMonotonic"] = "90"
    elif hazard == "not-exited": seal["SubState"] = "running"
    else: seal["ExecMainStatus"] = "1"
    with pytest.raises(GenerationUnavailable):
        _validate_boot_state(reg, checkpoint, registration_sha256="d" * 64, boot_id=boot_id, seal=seal, docker=docker)

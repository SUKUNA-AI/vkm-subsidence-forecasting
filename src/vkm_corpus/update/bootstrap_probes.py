"""Full private bootstrap probes under a live CLOSED writer capability.

These receipts attest private read qualification only. They never authorize
public serving, replace an EMPTY fallback drill, or grant scientific admission.
All configuration is operator-owned; no callable is loaded from configuration.
"""
from __future__ import annotations

import os
import hashlib
from pathlib import Path
import platform
import stat

from vkm_corpus.update.acceptance import (
    AcceptanceError, AcceptanceRegistrar, CandidateFence,
    _execute_private_probes, _require_private_transport, _verify_probe_receipts, read_tool_names,
)
from vkm_corpus.update.admission import _ordinary_bytes, read_state
from vkm_corpus.update.bootstrap import BootstrapProbePlan, BootstrapStartupAuthority
from vkm_corpus.update.contracts import GenerationManifest
from vkm_corpus.update.operator_units import bound_json
from vkm_corpus.update.runtime import BoundFile
from vkm_evidence.contracts import canonical_bytes, record_hash


BOOTSTRAP_CHECKS = {"native_identity", "full_mcp", "policy_enforcement",
                    "generation_consistency", "closed_authority"}
PROBE_SCHEMA = "vkm-bootstrap-private-probe/1"
REPORT_SCHEMA = "vkm-bootstrap-private-probes/1"
VERIFIED = "PRIVATE_PROBES_VERIFIED"


def require_exclusive_native_lease(fd: int, path: Path, pid: int | None = None):
    """Observe an already-owned Linux flock; never acquire or modify a lease."""
    pid = os.getpid() if pid is None else pid
    path = Path(path)
    if platform.system() != "Linux" or pid != os.getpid():
        raise AcceptanceError("native bootstrap leases require the current Linux process")
    if type(fd) is not int or fd < 0:
        raise AcceptanceError("closed bootstrap requires both live native exclusive leases")
    if not path.is_absolute() or ".." in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
        raise AcceptanceError("indirect bootstrap lease")
    owned, current = os.fstat(fd), path.stat(follow_symlinks=False)
    identity = lambda value: (value.st_dev, value.st_ino)
    if (not stat.S_ISREG(owned.st_mode) or owned.st_nlink != 1 or owned.st_mode & 0o022
            or identity(owned) != identity(current)):
        raise AcceptanceError("bootstrap lease descriptor does not bind its owned path")
    locks = [line.split() for line in Path(f"/proc/self/fdinfo/{fd}").read_text().splitlines()
             if line.startswith("lock:")]
    if len(locks) != 1:
        raise AcceptanceError("bootstrap native lease is absent or ambiguous")
    entry = locks[0]
    if (len(entry) != 9 or entry[2:5] != ["FLOCK", "ADVISORY", "WRITE"]
            or entry[5] != str(pid) or entry[7:] != ["0", "EOF"]):
        raise AcceptanceError("bootstrap descriptor does not own a native exclusive lease")
    major, minor, inode = entry[6].split(":")
    if (int(major, 16), int(minor, 16), int(inode)) != (
            os.major(owned.st_dev), os.minor(owned.st_dev), owned.st_ino):
        raise AcceptanceError("bootstrap kernel lease belongs to another inode")
    return list(identity(owned))


class ClosedBootstrapAuthority:
    """Borrow, never acquire or unlock, the controller's two exclusive leases.

    Construction and every check re-read the pinned authority and actual
    CLOSED/CURRENT bytes. Linux fdinfo proves that the supplied descriptors
    currently own WRITE flock leases; merely possessing an open file descriptor
    or discovering that a lock is available is not ownership. The mutation watch
    rejects mutate-and-restore as well as replacement during the entire run.

    SYNTHETIC plans use the same state binding without claiming native leases.
    Such a capability can never be consumed by a production probe plan.
    """

    def __init__(self, startup: BoundFile, plan: BootstrapProbePlan, *, intent: BoundFile,
                 writer_fd: int | None = None, gate_fd: int | None = None):
        self.startup, self.plan, self.intent = startup, plan, intent
        self.writer_fd, self.gate_fd = writer_fd, gate_fd
        self.pid, self._closed, self._watch = os.getpid(), False, None
        self.production = plan.scope == "BOOTSTRAP_SHADOW_PRODUCTION"
        self.authority = BootstrapStartupAuthority.model_validate(bound_json(startup))
        from vkm_corpus.update.bootstrap import BootstrapIntent
        self._intent = BootstrapIntent.model_validate(bound_json(intent))
        self._refs = (startup, intent, self._intent.environment, self._intent.runtime,
            self._intent.recipe.compose, self._intent.legacy.compose,
            *self._intent.legacy.selectors, self._intent.independent_backup)
        self.root = Path(self.authority.control_root).absolute()
        maintenance = self.root / "MAINTENANCE"
        self._maintenance_present = maintenance.exists() or maintenance.is_symlink()
        if self.production and self._maintenance_present:
            raise AcceptanceError("native bootstrap metadata startup requires absent MAINTENANCE")
        pin = self.authority.candidate
        self.manifest = GenerationManifest(components=pin.components, services=pin.services,
            code_commit=pin.code_commit, policy_sha256=pin.policy_sha256, acceptance_sha256=self.authority.sha256)
        self._paths = (*dict.fromkeys(Path(ref.path) for ref in self._refs), self.root / "CURRENT",
            self.root / (self.manifest.sha256 + ".json"), self.root / "ADMISSION.json",
            *((maintenance,) if self._maintenance_present else ()),
            self.root / "writer.lock", self.root / "admission.lock")
        try:
            if self.production:
                if platform.system() != "Linux":
                    raise AcceptanceError("native closed bootstrap qualification requires Linux")
                from vkm_corpus.update.native_files import NativeFileWatch
                self._watch = NativeFileWatch(self._paths)
            self.check()
        except BaseException:
            self.close()
            raise

    def _lock(self, fd, path):
        # fdinfo is kernel-owned, not an operator/corpus JSON assertion. Do not
        # call flock here: that would silently acquire a previously free lease.
        return require_exclusive_native_lease(fd, path, self.pid)

    def check(self):
        if self._closed or self.pid != os.getpid():
            raise AcceptanceError("closed bootstrap capability expired or crossed a process")
        if self._watch is not None:
            self._watch.check()
        actual = BootstrapStartupAuthority.model_validate(bound_json(self.startup))
        plan = BootstrapProbePlan.model_validate(self.plan.model_dump(mode="json"))
        if (actual != self.authority or self.startup.sha256 != actual.sha256
                or plan.startup_authority_sha256 != actual.sha256
                or plan.probes.candidate != actual.candidate
                or plan.legacy_topology_sha256 != actual.legacy_topology_sha256
                or plan.independent_backup_sha256 != actual.independent_backup_sha256
                or plan.probes.isolation_attestation_sha256 != actual.isolation_attestation_sha256):
            raise AcceptanceError("bootstrap probes do not bind the exact startup authority")
        # The typed intent validator is source-owned and verifies the relation;
        # an arbitrary caller-provided SHA is not a substitute for its bytes.
        from vkm_corpus.update.bootstrap import BootstrapIntent
        intent = BootstrapIntent.model_validate(bound_json(self.intent))
        if (self.intent.sha256 != plan.intent_sha256 or record_hash(intent) != plan.intent_sha256
                or intent.startup_authority != self.startup or intent != self._intent):
            raise AcceptanceError("bootstrap probes do not bind the approved intent")
        intent.require_plan(plan, actual)
        for path in (self.root, *self._paths):
            if any(p.is_symlink() for p in (path, *path.parents)):
                raise AcceptanceError("indirect bootstrap authority or control state")
        for ref in self._refs:
            path = Path(ref.path)
            if not path.is_absolute() or ".." in path.parts or path.is_relative_to(self.root):
                raise AcceptanceError("bootstrap authority is indirect or belongs to mutable control state")
            if hashlib.sha256(_ordinary_bytes(path, 2 * 1024 * 1024)).hexdigest() != ref.sha256:
                raise AcceptanceError("bootstrap intent input bytes changed")
        state = read_state(self.root)
        maintenance = self.root / "MAINTENANCE"
        maintenance_matches = (_ordinary_bytes(maintenance, 64) == actual.request_key.encode("ascii")
            if self._maintenance_present else not maintenance.exists() and not maintenance.is_symlink())
        if (state.status != "CLOSED" or state.request_key != actual.request_key
                or not maintenance_matches
                or _ordinary_bytes(self.root / "CURRENT", 65) != (self.manifest.sha256 + "\n").encode("ascii")
                or _ordinary_bytes(self.root / (self.manifest.sha256 + ".json"), 1_000_000)
                != canonical_bytes(self.manifest)):
            raise AcceptanceError("closed bootstrap owner or CURRENT differs")
        identities = {}
        if self.production:
            if platform.system() != "Linux" or plan.scope != "BOOTSTRAP_SHADOW_PRODUCTION":
                raise AcceptanceError("native bootstrap scope differs")
            identities = {"writer": self._lock(self.writer_fd, self.root / "writer.lock"),
                          "gate": self._lock(self.gate_fd, self.root / "admission.lock")}
        elif plan.scope != "SYNTHETIC":
            raise AcceptanceError("synthetic capability cannot qualify production")
        if self._watch is not None:
            self._watch.check()
        if not self._maintenance_present and (maintenance.exists() or maintenance.is_symlink()):
            raise AcceptanceError("bootstrap maintenance absence changed during inspection")
        return {"startup_authority_sha256": actual.sha256, "intent_sha256": plan.intent_sha256,
            "generation_sha256": self.manifest.sha256, "request_key": actual.request_key,
            "control_root_sha256": record_hash(str(self.root)), "scope": plan.scope,
            "native_exclusive_leases": identities, "maintenance_present": self._maintenance_present}

    def close(self):
        self._closed = True
        if self._watch is not None:
            self._watch.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _bindings(plan):
    pin = plan.probes.candidate
    return {"schema": REPORT_SCHEMA, "scope": plan.scope, "plan_sha256": plan.sha256,
        "intent_sha256": plan.intent_sha256, "startup_authority_sha256": plan.startup_authority_sha256,
        "legacy_topology_sha256": plan.legacy_topology_sha256,
        "independent_backup_sha256": plan.independent_backup_sha256,
        "candidate_sha256": record_hash(pin), "components_sha256": pin.components_sha256,
        "services_sha256": pin.services_sha256, "admission": "CLOSED",
        "serving_admission": False, "scientific_admission": False}


class BootstrapProbeRegistrar(AcceptanceRegistrar):
    """Separate immutable private-probe receipts, never serving certificates."""

    def register(self, report, plan: BootstrapProbePlan, *, authority: ClosedBootstrapAuthority):
        if type(authority) is not ClosedBootstrapAuthority or authority.plan != plan:
            raise AcceptanceError("source-owned closed bootstrap authority required")
        if self.approved_plan_sha256 != plan.sha256:
            raise AcceptanceError("operator-approved bootstrap probe plan differs")
        proof = authority.check()
        expected = {**_bindings(plan), "status": VERIFIED, "closed_authority_sha256": record_hash(proof),
                    "tools": dict.fromkeys(read_tool_names(), "PASS"),
                    "checks": dict.fromkeys(BOOTSTRAP_CHECKS, "PASS")}
        if set(report) != set(expected) | {"raw_probe_receipts"} or any(report.get(k) != v for k, v in expected.items()):
            raise AcceptanceError("bootstrap private probe report is incomplete or differently bound")
        _verify_probe_receipts(report, plan.probes, self.read, schema=PROBE_SCHEMA, plan_sha256=plan.sha256)
        if authority.check() != proof:
            raise AcceptanceError("closed authority changed before registration")
        digest = self.store(report)
        if authority.check() != proof:
            raise AcceptanceError("closed authority changed before registration acknowledgement")
        return digest


async def qualify_closed_bootstrap(plan: BootstrapProbePlan, *, transport,
        fence: CandidateFence, registrar: BootstrapProbeRegistrar,
        authority: ClosedBootstrapAuthority) -> dict:
    """Run the complete private READ contract without opening public admission."""
    if (type(registrar) is not BootstrapProbeRegistrar or type(authority) is not ClosedBootstrapAuthority
            or registrar.approved_plan_sha256 != plan.sha256 or authority.plan != plan):
        raise AcceptanceError("approved bootstrap plan and source-owned authority required")
    _require_private_transport(plan.probes, transport, fence)
    if plan.scope == "BOOTSTRAP_SHADOW_PRODUCTION" and (not authority.production or not fence.production):
        raise AcceptanceError("production bootstrap cannot use synthetic capabilities")
    tools, checks, receipts = {}, {}, []
    def attempt(status, **detail):
        report = {**_bindings(plan), "status": status, **detail,
                  "tools": tools, "checks": checks, "raw_probe_receipts": receipts}
        return {**report, "attempt_receipt_sha256": registrar.store(report)}
    if transport is None:
        return attempt("NOT_RUN", reason="PRIVATE_TRANSPORT_UNAVAILABLE")
    try:
        initial = authority.check()
        await _execute_private_probes(plan.probes, transport=transport, fence=fence, store=registrar.store,
            tools=tools, checks=checks, receipts=receipts, receipt_schema=PROBE_SCHEMA,
            receipt_plan_sha256=plan.sha256, boundary_check=authority.check)
        if authority.check() != initial:
            raise AcceptanceError("closed bootstrap capability changed during private probes")
        checks["closed_authority"] = "PASS"
        report = {**_bindings(plan), "status": VERIFIED, "tools": tools, "checks": checks,
            "closed_authority_sha256": record_hash(initial), "raw_probe_receipts": receipts}
        digest = registrar.register(report, plan, authority=authority)
        return {**report, "receipt_sha256": digest}
    except Exception as exc:
        # Probe exceptions may contain source/token values. Persist only type.
        return attempt("FAIL", error_type=type(exc).__name__)

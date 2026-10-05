"""One-time legacy -> qualified LIVE authority, distinct from promotion.

The candidate retains its original shadow serving receipt. Legacy is only a
recovery boundary and can never become a qualified GenerationManifest. No
future activation receipt occurs in the runtime/manifest hash input.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import stat
from typing import Literal

from pydantic import Field, model_validator

from vkm_corpus.update.acceptance import AcceptancePlan, AcceptanceRegistrar
from vkm_corpus.update.contracts import GenerationManifest, ComponentIdentity, ServiceIdentity
from vkm_corpus.update.frontdoor import FrontdoorProfile
from vkm_corpus.update.bootstrap import BootstrapNetwork
from vkm_corpus.update.operator import CoreOperatorConfig, OperatorRelease
from vkm_corpus.update.operator_units import ComposeRelease, UnitControlConfig, UnitPin, bound_json
from vkm_corpus.update.runtime import BoundFile
from vkm_evidence.contracts import Identifier, Sha256, StrictModel, record_hash


class FirstLiveError(ValueError):
    pass


class FirstLiveNotReady(FirstLiveError):
    """Native creation is unqualified; block before any host/container effect."""

    reason = "UNQUALIFIED_NATIVE_CREATE_CAN_DELETE_RETAINED_LEGACY"


class BootQualificationUnavailable(FirstLiveError):
    """No source-owned host ordering proof; never a generic code-test failure."""


class LegacyFile(StrictModel):
    file: BoundFile
    bytes: int = Field(ge=0, le=64 * 1024**2)


class RetainedSharedObserver(StrictModel):
    """Read-only observation recipe, independent of candidate G/R/CURRENT."""
    environment: BoundFile
    runtime: BoundFile
    native: BoundFile
    duckdb: BoundFile
    nav: BoundFile
    max_local_identity_bytes: int = Field(gt=0, le=128 * 1024**3)
    components: tuple[ComponentIdentity, ...] = Field(min_length=1)
    services: tuple[ServiceIdentity, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self):
        if (len({c.component for c in self.components}) != len(self.components)
                or len({s.service for s in self.services}) != len(self.services)):
            raise ValueError("retained actual resource/service set must be unique")
        return self


class RestoredLegacySelector(StrictModel):
    source: BoundFile
    restored: BoundFile

    @model_validator(mode="after")
    def _same_version(self):
        if self.source.sha256 != self.restored.sha256:
            raise ValueError("selector restore must retain the exact original byte version")
        return self


class LegacyRecoveryBoundary(StrictModel):
    schema_version: Literal["vkm-first-live-legacy-boundary/1"] = "vkm-first-live-legacy-boundary/1"
    status: Literal["UNQUALIFIED_LEGACY"] = "UNQUALIFIED_LEGACY"
    scope: Literal["SYNTHETIC", "RECEIVER_CONTROL_ONLY"] = "SYNTHETIC"
    restore_compose: BoundFile
    units: dict[Literal["api", "mcp", "mcp-admin"], UnitPin]
    original_container_ids: dict[Literal["api", "mcp", "mcp-admin"], Sha256]
    selectors: tuple[BoundFile, ...] = Field(min_length=1, max_length=64)
    selector_restores: tuple[RestoredLegacySelector, ...] = Field(default=(), max_length=64)
    # These are independently retrieved/decrypted ordinary control files. The
    # files are verified again; a PASS/count in a receipt is insufficient.
    restored_files: tuple[LegacyFile, ...] = Field(min_length=1, max_length=128)
    independent_copy: BoundFile
    independent_restore: BoundFile
    source_failure_domain: Identifier
    backup_failure_domain: Identifier
    shared_services_sha256: Sha256
    owner_approval: BoundFile | None = None
    observer: RetainedSharedObserver | None = None
    max_control_bytes: int = Field(default=64 * 1024**2, gt=0, le=256 * 1024**2)

    @model_validator(mode="after")
    def _complete(self):
        if (not {"api", "mcp"} <= set(self.units) or set(self.units) != set(self.original_container_ids)
                or self.source_failure_domain == self.backup_failure_domain
                or self.independent_copy == self.independent_restore
                or len({r.file.path for r in self.restored_files}) != len(self.restored_files)
                or len({r.path for r in self.selectors}) != len(self.selectors)
                or sum(r.bytes for r in self.restored_files) > self.max_control_bytes
                or self.restore_compose not in [r.file for r in self.restored_files]):
            raise ValueError("complete independently restored legacy control closure required")
        if self.scope == "RECEIVER_CONTROL_ONLY" and (self.owner_approval is None or self.observer is None
                or len(self.selector_restores) != len(self.selectors)
                or {r.source for r in self.selector_restores} != set(self.selectors)
                or not {r.restored for r in self.selector_restores} <= {r.file for r in self.restored_files}
                or not {self.observer.environment, self.observer.runtime, self.observer.native}
                    <= {r.file for r in self.restored_files}):
            raise ValueError("owner-approved recovery and every selector byte version are required")
        return self


class LegacyRecoveryApproval(StrictModel):
    """Owner approval capability, not a producer's assertion of independence.

    Created only after reviewing the exact executed opaque recovery packet.
    The controller verifies its closure; it cannot independently prove a remote
    failure domain from JSON. No existing corpus receipt qualifies this scope.
    """
    schema_version: Literal["vkm-owner-approved-control-recovery/1"] = "vkm-owner-approved-control-recovery/1"
    scope: Literal["RECEIVER_CONTROL_ONLY"] = "RECEIVER_CONTROL_ONLY"
    decision_id: Identifier
    frozen_executor: BoundFile
    frozen_capture: BoundFile
    procedure: BoundFile
    copy_receipt: BoundFile
    restore_receipt: BoundFile
    restored_inventory_sha256: Sha256
    source_failure_domain: Identifier
    backup_failure_domain: Identifier
    source_machine_sha256: Sha256
    backup_machine_sha256: Sha256
    authenticated_host_key_sha256: Sha256

    @model_validator(mode="after")
    def _different(self):
        if (self.source_machine_sha256 == self.backup_machine_sha256
                or self.source_failure_domain == self.backup_failure_domain
                or self.copy_receipt == self.restore_receipt):
            raise ValueError("independent exact host/restore observations required")
        return self


class CandidateDocuments(StrictModel):
    """Named complete control graph; names replace BoundFile locations in diff."""
    documents: dict[Identifier, BoundFile] = Field(min_length=6, max_length=128)
    effective: dict[Literal["api", "mcp"], BoundFile]
    opaque: dict[Identifier, BoundFile] = Field(default_factory=dict, max_length=64)

    @model_validator(mode="after")
    def _required(self):
        if (not {"compose", "environment", "runtime", "native", "policy", "context"} <= set(self.documents)
                or set(self.effective) != {"api", "mcp"}
                or set(self.documents) & set(self.opaque)
                or any(k.startswith("effective-") for k in self.documents)
                or len({(r.path, r.sha256) for r in self.documents.values()}) != len(self.documents)):
            raise ValueError("complete uniquely named candidate control graph required")
        return self


class FirstLiveMapping(StrictModel):
    document: Identifier
    pointer: str = Field(pattern=r"^/(?:[^~]|~[01])+$", max_length=2048)
    before: str = Field(min_length=1, max_length=8192)
    after: str = Field(min_length=1, max_length=8192)
    kind: Literal["PROJECT", "AUTHORITY_ROOT", "CONTROL_ROOT", "QUALIFICATION_ROOT"]


class FirstLiveIntent(StrictModel):
    schema_version: Literal["vkm-first-live-intent/1"] = "vkm-first-live-intent/1"
    scope: Literal["SYNTHETIC", "FIRST_LIVE_PRODUCTION"]
    request_id: Identifier
    candidate: BoundFile
    shadow_operator: BoundFile
    shadow_acceptance: BoundFile
    shadow_plan: BoundFile
    shadow_documents: CandidateDocuments
    live_documents: CandidateDocuments
    mappings: tuple[FirstLiveMapping, ...] = Field(max_length=256)
    legacy: LegacyRecoveryBoundary
    candidate_release: ComposeRelease
    release: OperatorRelease
    units: UnitControlConfig
    frontdoor: FrontdoorProfile
    networks: tuple[BootstrapNetwork, ...] = Field(default=(), max_length=4)
    authority_root: str
    qualification_root: str
    protected_roots: tuple[str, ...] = Field(min_length=1, max_length=16)
    operator_code_sha256: Sha256
    operator_dependencies_sha256: Sha256
    operator_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    boot_registration: BoundFile | None = None
    boot_checkpoint: BoundFile | None = None
    # Host restart is explicitly outside this transaction until a separate
    # persistence/ordering drill exists; this field cannot claim PASS.
    reboot_safety: Literal["NOT_QUALIFIED"] = "NOT_QUALIFIED"

    @model_validator(mode="after")
    def _bound(self):
        if (self.units.releases != (self.candidate_release,) or self.candidate_release.project != "vkm-core"
                or self.release.receiver_release_sha256 != self.candidate_release.sha256
                or self.release.baseline_registration is not None or self.release.generation != self.candidate
                or self.release.acceptance_plan != self.shadow_plan
                or self.frontdoor.scope != self.scope
                or self.units.scope != ("SYNTHETIC" if self.scope == "SYNTHETIC" else "PRODUCTION_SWITCH")
                or len({(m.document, m.pointer) for m in self.mappings}) != len(self.mappings)):
            raise ValueError("first LIVE must preserve the normal candidate and source plan")
        from urllib.parse import urlsplit
        required_ports = {self.units.api_container_port, self.units.mcp_container_port,
            urlsplit(self.units.receiver_url).port or 80, urlsplit(self.units.mcp_receiver_url).port or 80,
            *(b.host_port for b in self.units.public_bindings)}
        if not required_ports <= set(self.frontdoor.tcp_ports):
            raise ValueError("frontdoor omits an actual candidate ingress/return port")
        if ((self.boot_registration is None) != (self.boot_checkpoint is None)
                or self.scope == "FIRST_LIVE_PRODUCTION" and self.boot_registration is None):
            raise ValueError("production first LIVE requires a separately exercised boot-ordering boundary")
        if (len({n.name for n in self.networks}) != len(self.networks)
                or self.scope == "FIRST_LIVE_PRODUCTION" and
                    (not self.networks or self.legacy.scope != "RECEIVER_CONTROL_ONLY")):
            raise ValueError("production requires retained external networks and approved control-only recovery")
        roots = [Path(x) for x in (self.authority_root, self.units.control_root, self.qualification_root, *self.protected_roots)]
        if any(not p.is_absolute() or ".." in p.parts for p in roots):
            raise ValueError("normalized absolute first-LIVE roots required")
        overlap = lambda a, b: a.is_relative_to(b) or b.is_relative_to(a)
        if any(overlap(a, b) for i, a in enumerate(roots[:3]) for b in roots[i + 1:]):
            raise ValueError("first-LIVE authority/control/receipts/data roots overlap")
        if self.scope == "FIRST_LIVE_PRODUCTION":
            refs = [*self.live_documents.documents.values(), *self.live_documents.opaque.values(),
                *self.live_documents.effective.values(), *(r.file for r in self.legacy.restored_files),
                self.legacy.independent_copy, self.legacy.independent_restore, self.legacy.owner_approval,
                self.boot_registration, self.boot_checkpoint]
            if any(not Path(r.path).is_absolute() or ".." in Path(r.path).parts
                    or not Path(r.path).is_relative_to(roots[0]) for r in refs):
                raise ValueError("live inputs and retained control closure require an independent authority root")
        return self

    @property
    def sha256(self):
        return record_hash(self)


class LiveAdmissionAuthority(StrictModel):
    schema_version: Literal["vkm-first-live-authority/1"] = "vkm-first-live-authority/1"
    capability: Literal["INITIALIZE_NORMAL_GENERATION_ONCE"] = "INITIALIZE_NORMAL_GENERATION_ONCE"
    intent: BoundFile
    request_id: Identifier
    control_root: str
    # Final receipts never appear here or in the candidate runtime.
    candidate_generation_sha256: Sha256
    fallback: Literal["LEGACY_RESTORED_UNQUALIFIED_CLOSED"] = "LEGACY_RESTORED_UNQUALIFIED_CLOSED"

    @property
    def request_key(self):
        return record_hash(self.request_id)


class FirstLiveReceipt(StrictModel):
    schema_version: Literal["vkm-first-live-result/1"] = "vkm-first-live-result/1"
    scope: Literal["SYNTHETIC", "FIRST_LIVE_PRODUCTION"]
    status: Literal["PRIVATE_49_VERIFIED_CLOSED", "LEGACY_RESTORED_UNQUALIFIED_CLOSED", "FIRST_LIVE_ACTIVATED"]
    intent_sha256: Sha256
    authority_sha256: Sha256
    candidate_generation_sha256: Sha256
    journal_head_sha256: Sha256
    native_sha256: Sha256
    frontdoor_sha256: Sha256
    scientific_admission: Literal[False] = False
    rollback_scope: Literal["RECEIVER_CONTROL_ONLY_SHARED_STATE_UNCHANGED"] = "RECEIVER_CONTROL_ONLY_SHARED_STATE_UNCHANGED"
    reboot_safety: Literal["NOT_QUALIFIED"] = "NOT_QUALIFIED"


def verify_legacy_files(boundary: LegacyRecoveryBoundary):
    """Verify actually restored bytes; never decode/render secret payloads."""
    expected = [{"sha256": r.file.sha256, "bytes": r.bytes} for r in boundary.restored_files]
    inventory = record_hash(expected)
    if boundary.scope == "RECEIVER_CONTROL_ONLY":
        approval = LegacyRecoveryApproval.model_validate(bound_json(boundary.owner_approval))
        if (approval.copy_receipt != boundary.independent_copy or approval.restore_receipt != boundary.independent_restore
                or approval.restored_inventory_sha256 != inventory
                or approval.source_failure_domain != boundary.source_failure_domain
                or approval.backup_failure_domain != boundary.backup_failure_domain):
            raise FirstLiveError("owner recovery approval names another exact executed packet")
        from vkm_corpus.update.runtime import read_bound
        for ref in (approval.frozen_executor, approval.frozen_capture, approval.procedure):
            read_bound(ref)
    for kind, ref in (("COPY", boundary.independent_copy), ("RESTORE", boundary.independent_restore)):
        data = bound_json(ref)
        if (not isinstance(data, dict) or type(data.get("files")) is not int or type(data.get("bytes")) is not int
                or boundary.scope == "RECEIVER_CONTROL_ONLY" and data.get("source_untouched") is not True):
            raise FirstLiveError("recovery observations require exact integer counts and boolean source fence")
        wanted = {"schema": "vkm-legacy-control-recovery/1", "kind": kind,
            "status": "AUTHENTICATED_BYTES_VERIFIED", "inventory_sha256": inventory,
            "source_failure_domain": boundary.source_failure_domain,
            "backup_failure_domain": boundary.backup_failure_domain,
            "files": len(expected), "bytes": sum(r.bytes for r in boundary.restored_files)}
        if boundary.scope == "RECEIVER_CONTROL_ONLY":
            wanted.update({"scope": "RECEIVER_CONTROL_ONLY", "source_untouched": True,
                "frozen_executor_sha256": approval.frozen_executor.sha256,
                "frozen_capture_sha256": approval.frozen_capture.sha256,
                "procedure_sha256": approval.procedure.sha256,
                "source_machine_sha256": approval.source_machine_sha256,
                "backup_machine_sha256": approval.backup_machine_sha256,
                "authenticated_host_key_sha256": approval.authenticated_host_key_sha256,
                "transport": "PINNED_SSH_SECOND_SESSION", "encryption": "AGE_AUTHENTICATED",
                "operation": "COPY_COMMITTED" if kind == "COPY" else "INDEPENDENT_FETCH_DECRYPT_RESTORE"})
        if data != wanted:
            raise FirstLiveError("independent legacy recovery receipt has another exact closure")
    for item in boundary.restored_files:
        path = Path(item.file.path)
        if not path.is_absolute() or ".." in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
            raise FirstLiveError("indirect restored legacy file")
        from vkm_corpus.update.admission import _ordinary_bytes
        raw = _ordinary_bytes(path, item.bytes)
        if len(raw) != item.bytes or hashlib.sha256(raw).hexdigest() != item.file.sha256:
            raise FirstLiveError("actual restored legacy bytes differ")
    return inventory


def _symbolic(value, refs):
    if isinstance(value, dict):
        if set(value) == {"path", "sha256"}:
            key = (value["path"], value["sha256"])
            if key not in refs:
                raise FirstLiveError("candidate control graph omits a BoundFile")
            return {"$document": refs[key]}
        return {k: _symbolic(v, refs) for k, v in value.items()}
    if isinstance(value, list):
        return [_symbolic(v, refs) for v in value]
    return value


def _normalize_ingress(documents, config):
    """Verify the complete actual port lists before replacing their projection.

    The source-owned binding contract independently retains every IP and port.
    A new endpoint in a caller mapping cannot be admitted by this function.
    """
    from urllib.parse import urlsplit
    for unit in ("api", "mcp"):
        endpoint = config.receiver_url if unit == "api" else config.mcp_receiver_url
        target = config.api_container_port if unit == "api" else config.mcp_container_port
        expected = [("127.0.0.1", urlsplit(endpoint).port or 80)]
        expected += [(b.host_ip, b.host_port) for b in config.public_bindings if b.unit == unit]
        spec = documents["compose"]["services"][unit]
        actual = []
        for row in spec.get("ports", []):
            if (not isinstance(row, dict) or set(row) - {"host_ip", "published", "target", "protocol", "mode"}
                    or row.get("target") != target or row.get("protocol", "tcp") != "tcp"
                    or row.get("mode", "ingress") != "ingress" or not str(row.get("published", "")).isdigit()):
                raise FirstLiveError("unregistered compose ingress binding")
            actual.append((row.get("host_ip"), int(row["published"])))
        native = documents["effective-" + unit]["host_config"].get("PortBindings")
        if not isinstance(native, dict) or set(native) != {str(target) + "/tcp"}:
            raise FirstLiveError("unregistered effective native ingress binding")
        observed = []
        for row in native[str(target) + "/tcp"]:
            if set(row) != {"HostIp", "HostPort"} or not str(row["HostPort"]).isdigit():
                raise FirstLiveError("unregistered effective native ingress binding")
            observed.append((row["HostIp"], int(row["HostPort"])))
        if sorted(actual) != sorted(expected) or sorted(observed) != sorted(expected):
            raise FirstLiveError("ingress is outside the exact typed endpoint inventory")
        spec["ports"] = "EXACT_TYPED_RECEIVER_BINDINGS"
        documents["effective-" + unit]["host_config"]["PortBindings"] = "EXACT_TYPED_RECEIVER_BINDINGS"


def _structured_effective(documents):
    for unit in ("api", "mcp"):
        effective = documents["effective-" + unit]
        env = effective["config"].get("Env", [])
        if not isinstance(env, list) or any(not isinstance(x, str) or "=" not in x for x in env):
            raise FirstLiveError("effective native environment is not complete")
        pairs = [x.split("=", 1) for x in env]
        if len({x[0] for x in pairs}) != len(pairs):
            raise FirstLiveError("duplicate effective native environment key")
        effective["config"]["Env"] = dict(pairs)
        binds = effective["host_config"].get("Binds")
        if binds is not None:
            if not isinstance(binds, list) or any(not isinstance(x, str) or len(x.split(":")) != 3 for x in binds):
                raise FirstLiveError("ambiguous effective native mount syntax")
            effective["host_config"]["Binds"] = [{"Source": x.split(":")[0], "Destination": x.split(":")[1],
                "mode": x.split(":")[2]} for x in binds]


def verify_candidate_documents(intent):
    """No arbitrary ignored hashes; every nested BoundFile is represented."""
    shadow, live = intent.shadow_documents, intent.live_documents
    if (set(shadow.documents) != set(live.documents) or set(shadow.opaque) != set(live.opaque)):
        raise FirstLiveError("shadow/live control inventories differ")
    from vkm_corpus.update.runtime import read_bound
    for name in shadow.opaque:
        read_bound(shadow.opaque[name]); read_bound(live.opaque[name])
        if shadow.opaque[name].sha256 != live.opaque[name].sha256:
            raise FirstLiveError("opaque credential/control bytes cannot change during promotion")
    def load(bundle):
        names = {(r.path, r.sha256): k for k, r in {**bundle.documents, **bundle.opaque}.items()}
        return {k: _symbolic(bound_json(r), names) for k, r in {**bundle.documents,
            **{"effective-" + u: r for u, r in bundle.effective.items()}}.items()}
    left, right = load(shadow), load(live)
    config = CoreOperatorConfig.model_validate(bound_json(intent.shadow_operator)) if intent.scope != "SYNTHETIC" else None
    if config is not None:
        sr = next((r for r in config.releases if r.generation.sha256 == intent.candidate.sha256), None)
        su = next((r for r in config.units.releases if sr is not None and r.sha256 == sr.receiver_release_sha256), None)
        if (sr is None or su is None or sr.runtime != shadow.documents["runtime"]
                or sr.environment != shadow.documents["environment"] or su.compose != shadow.documents["compose"]):
            raise FirstLiveError("shadow document graph does not select the qualified candidate")
        for label, bundle, unit_config, unit_release in (("shadow", shadow, config.units, su),
                ("live", live, intent.units, intent.candidate_release)):
            for unit in ("api", "mcp"):
                value = bound_json(bundle.effective[unit])
                if (set(value) != {"config", "host_config", "mounts", "networks"}
                        or record_hash(value) != unit_release.units[unit].config_sha256
                        or value["config"].get("Labels", {}).get("com.docker.compose.project") != unit_release.project
                        or value["config"].get("Labels", {}).get("com.docker.compose.service") != unit):
                    raise FirstLiveError("full effective receiver configuration differs")
            _normalize_ingress(left if label == "shadow" else right, unit_config)
        # Native Env/Binds are structured here so an allowed path mapping never
        # becomes a blanket string replacement inside arbitrary model values.
        for collection in (left, right):
            _structured_effective(collection)
    allowed_paths = {"path", "authority_root", "control_root", "qualification_root", "runtime_root", "source", "target",
        "Source", "Destination", "VKM_UPDATE_RUNTIME_FILE", "VKM_SOURCE_POLICY_FILE", "VKM_ACCESS_CONTEXT_FILE",
        "VKM_DEPLOYMENT_TOKEN_FILE", "VKM_API_TOKEN_FILE", "VKM_MCP_TOKEN_FILE", "VKM_DEPLOYMENT_GATE_FILE",
        "VKM_API_READ_CREDENTIALS_FILE", "VKM_API_WRITE_TOKEN_FILE"}
    for mapping in intent.mappings:
        if mapping.document not in left:
            raise FirstLiveError("mapping names an unknown document")
        parts = [p.replace("~1", "/").replace("~0", "~") for p in mapping.pointer[1:].split("/")]
        valid = False
        if mapping.kind == "PROJECT":
            valid = ((mapping.document == "compose" and parts == ["name"] or mapping.document.startswith("effective-")
                and parts == ["config", "Labels", "com.docker.compose.project"])
                and (mapping.before, mapping.after) == ("vkm-core-shadow", "vkm-core"))
        elif mapping.kind in {"AUTHORITY_ROOT", "CONTROL_ROOT", "QUALIFICATION_ROOT"} and config is not None:
            pairs = {"AUTHORITY_ROOT": (config.authority_root, intent.authority_root),
                "CONTROL_ROOT": (str(Path(config.units.control_root).parent), str(Path(intent.units.control_root).parent)),
                "QUALIFICATION_ROOT": (config.qualification_root, intent.qualification_root)}
            aroot, broot = pairs[mapping.kind]
            valid = (parts[-1] in allowed_paths and
                (mapping.before == aroot or mapping.before.startswith(aroot.rstrip("/\\") + "/"))
                and mapping.after == broot + mapping.before[len(aroot):])
        if not valid or mapping.before == mapping.after:
            raise FirstLiveError("mapping attempts an unapproved semantic change")
        a, b = left[mapping.document], right[mapping.document]
        try:
            for part in parts[:-1]:
                a = a[int(part)] if isinstance(a, list) else a[part]
                b = b[int(part)] if isinstance(b, list) else b[part]
            key_a = int(parts[-1]) if isinstance(a, list) else parts[-1]
            key_b = int(parts[-1]) if isinstance(b, list) else parts[-1]
            if a[key_a] != mapping.before or b[key_b] != mapping.after:
                raise FirstLiveError("mapping does not describe the exact scalar")
            a[key_a] = mapping.after
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise FirstLiveError("invalid candidate mapping") from exc
    if left != right:
        raise FirstLiveError("unmapped candidate configuration drift")
    for unit in ("api", "mcp"):
        effective = bound_json(live.effective[unit])
        if record_hash(effective) != intent.candidate_release.units[unit].config_sha256:
            raise FirstLiveError("effective candidate native configuration differs")
    return record_hash({"shadow": shadow.model_dump(mode="json"), "live": live.model_dump(mode="json"),
                        "mappings": [m.model_dump(mode="json") for m in intent.mappings]})


def verify_authority(ref: BoundFile, *, recovery_only=False):
    authority = LiveAdmissionAuthority.model_validate(bound_json(ref))
    intent = FirstLiveIntent.model_validate(bound_json(authority.intent))
    if (authority.intent.sha256 != intent.sha256 or authority.request_id != intent.request_id
            or authority.control_root != intent.units.control_root
            or authority.candidate_generation_sha256 != intent.candidate.sha256
            or not Path(ref.path).is_relative_to(Path(intent.authority_root))
            or not Path(authority.intent.path).is_relative_to(Path(intent.authority_root))):
        raise FirstLiveError("first-LIVE authority and intent differ")
    verify_legacy_files(intent.legacy)
    if recovery_only:
        return authority, intent, None, None
    verify_candidate_documents(intent)
    manifest = GenerationManifest.model_validate(bound_json(intent.candidate))
    plan = AcceptancePlan.model_validate(bound_json(intent.shadow_plan))
    if (manifest.sha256 != intent.candidate.sha256 or manifest.acceptance_sha256 != intent.shadow_acceptance.sha256
            or plan.sha256 != intent.shadow_plan.sha256 or plan.candidate.components != manifest.components
            or plan.candidate.services != manifest.services or plan.candidate.code_commit != manifest.code_commit
            or plan.candidate.policy_sha256 != manifest.policy_sha256
            or record_hash([s.model_dump(mode="json") for s in sorted(manifest.services, key=lambda s: s.service)]) != intent.legacy.shared_services_sha256
            or plan.scope != ("SYNTHETIC" if intent.scope == "SYNTHETIC" else "SHADOW_PRODUCTION")):
        raise FirstLiveError("normal candidate differs from exact shadow qualification")
    if intent.legacy.observer is not None and (
            sorted(intent.legacy.observer.components, key=lambda c: c.component) != sorted(manifest.components, key=lambda c: c.component)
            or sorted(intent.legacy.observer.services, key=lambda s: s.service) != sorted(manifest.services, key=lambda s: s.service)):
        raise FirstLiveError("receiver-only transition would change shared stores or services")
    # Validate the full old registrar contract, including original closed
    # baseline chain. Never grant acceptance just because a receipt says PASS.
    shadow = CoreOperatorConfig.model_validate(bound_json(intent.shadow_operator))
    if (shadow.units.scope != "SHADOW_PRODUCTION" or shadow.deployment_profile_sha256 != plan.deployment_profile_sha256):
        raise FirstLiveError("original shadow deployment profile differs")
    from vkm_corpus.update.operator import CoreOperator
    verifier = object.__new__(CoreOperator)
    verifier.config = shadow
    registrar = AcceptanceRegistrar(Path(shadow.qualification_root), approved_plan_sha256=plan.sha256,
        closed_baseline_verifier=verifier._closed_drill_previous)
    report = bound_json(intent.shadow_acceptance)
    # register is content-addressed and would write; verification must not do so.
    captured = []
    registrar.store = lambda value: captured.append(record_hash(value)) or record_hash(value)
    if registrar.register(report, plan) != intent.shadow_acceptance.sha256 or captured != [intent.shadow_acceptance.sha256]:
        raise FirstLiveError("original shadow acceptance closure differs")
    return authority, intent, manifest, plan

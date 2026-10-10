"""Closed first-baseline capabilities; never ordinary serving acceptance.

The authority permits native startup/metadata in an isolated shadow only. It
does not permit public reads, scientific admission or any legacy mutation.
The final baseline registration is downstream of private probes and an EMPTY
fallback drill; none of those future receipts is an input to startup authority.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from vkm_corpus.update.acceptance import AcceptancePlan, CandidatePin
from vkm_corpus.update.runtime import BoundFile
from vkm_evidence.contracts import Identifier, Sha256, StrictModel, record_hash


class BootstrapStartupAuthority(StrictModel):
    schema_version: Literal["vkm-bootstrap-startup/1"] = "vkm-bootstrap-startup/1"
    mode: Literal["SHADOW_CLOSED_METADATA"] = "SHADOW_CLOSED_METADATA"
    bootstrap_id: Identifier
    request_id: Identifier
    candidate: CandidatePin
    control_root: str = Field(min_length=1)
    isolation_attestation_sha256: Sha256
    legacy_topology_sha256: Sha256
    independent_backup_sha256: Sha256

    @model_validator(mode="after")
    def _direct_root(self):
        path = Path(self.control_root)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("bootstrap control root must be absolute without traversal")
        return self

    @property
    def sha256(self):
        return record_hash(self)

    @property
    def request_key(self):
        return record_hash(self.request_id)


class BootstrapProbePlan(StrictModel):
    schema_version: Literal["vkm-bootstrap-probe-plan/1"] = "vkm-bootstrap-probe-plan/1"
    scope: Literal["SYNTHETIC", "BOOTSTRAP_SHADOW_PRODUCTION"]
    intent_sha256: Sha256
    startup_authority_sha256: Sha256
    legacy_topology_sha256: Sha256
    independent_backup_sha256: Sha256
    # Reuse the complete probe specification, never its ordinary drill/receipt.
    probes: AcceptancePlan

    @model_validator(mode="after")
    def _separate(self):
        wanted = "SYNTHETIC" if self.scope == "SYNTHETIC" else "SHADOW_PRODUCTION"
        if self.probes.scope != wanted or self.probes.drill_receipt_sha256 is not None:
            raise ValueError("bootstrap probes cannot borrow ordinary serving/drill qualification")
        return self

    @property
    def sha256(self):
        return record_hash(self)


class LegacyUnitPin(StrictModel):
    image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    config_sha256: Sha256
    container_id: Sha256
    started_at: str = Field(min_length=1, max_length=100)


class LegacyTopology(StrictModel):
    schema_version: Literal["vkm-retained-legacy-topology/1"] = "vkm-retained-legacy-topology/1"
    project: Literal["vkm-core"] = "vkm-core"
    compose: BoundFile
    units: dict[Literal["api", "mcp"], LegacyUnitPin]
    selectors: tuple[BoundFile, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _whole(self):
        if (set(self.units) != {"api", "mcp"}
                or len({p.path for p in self.selectors}) != len(self.selectors)):
            raise ValueError("legacy topology must retain both receivers and unique native selectors")
        return self

    @property
    def sha256(self):
        return record_hash(self)


class BootstrapNetwork(StrictModel):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    network_id: Sha256
    config_sha256: Sha256


class BootstrapRecipe(StrictModel):
    project: Literal["vkm-core-shadow"] = "vkm-core-shadow"
    compose: BoundFile
    images: dict[Literal["api", "mcp"], str]
    # Pre-existing isolated EXTERNAL networks; bootstrap creates/deletes none.
    networks: tuple[BootstrapNetwork, ...] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def _bounded(self):
        import re
        if (set(self.images) != {"api", "mcp"}
                or any(not re.fullmatch(r"sha256:[0-9a-f]{64}", v) for v in self.images.values())
                or len({n.name for n in self.networks}) != len(self.networks)
                or len({n.network_id for n in self.networks}) != len(self.networks)):
            raise ValueError("bootstrap needs exact native images and unique existing networks")
        return self


class BootstrapIntent(StrictModel):
    schema_version: Literal["vkm-bootstrap-intent/1"] = "vkm-bootstrap-intent/1"
    scope: Literal["SYNTHETIC", "BOOTSTRAP_SHADOW_PRODUCTION"]
    bootstrap_id: Identifier
    request_id: Identifier
    startup_authority: BoundFile
    retained_startup: BootstrapStartupAuthority | None = None
    probe_spec_sha256: Sha256
    deployment_profile_sha256: Sha256
    environment: BoundFile
    runtime: BoundFile
    recipe: BootstrapRecipe
    legacy: LegacyTopology
    independent_backup: BoundFile

    @property
    def sha256(self):
        return record_hash(self)

    def require_plan(self, plan: BootstrapProbePlan, authority: BootstrapStartupAuthority):
        if (self.retained_startup is not None and self.retained_startup != authority
                or plan.intent_sha256 != self.sha256 or plan.scope != self.scope
                or plan.startup_authority_sha256 != self.startup_authority.sha256
                or authority.sha256 != self.startup_authority.sha256
                or authority.bootstrap_id != self.bootstrap_id or authority.request_id != self.request_id
                or authority.candidate != plan.probes.candidate
                or record_hash(plan.probes) != self.probe_spec_sha256
                or plan.probes.deployment_profile_sha256 != self.deployment_profile_sha256
                or authority.isolation_attestation_sha256 != plan.probes.isolation_attestation_sha256
                or plan.legacy_topology_sha256 != self.legacy.sha256
                or authority.legacy_topology_sha256 != self.legacy.sha256
                or plan.independent_backup_sha256 != self.independent_backup.sha256
                or authority.independent_backup_sha256 != self.independent_backup.sha256):
            raise ValueError("bootstrap intent/authority/probes/native boundaries differ")
        return self


class BootstrapBoundaryReceipt(StrictModel):
    schema_version: Literal["vkm-bootstrap-boundary/1"] = "vkm-bootstrap-boundary/1"
    scope: Literal["SYNTHETIC", "BOOTSTRAP_SHADOW_PRODUCTION"]
    status: Literal["EMPTY_FALLBACK_VERIFIED"] = "EMPTY_FALLBACK_VERIFIED"
    intent_sha256: Sha256
    startup_authority_sha256: Sha256
    preparation_sha256: Sha256
    legacy_before_sha256: Sha256
    legacy_after_sha256: Sha256
    empty_before_sha256: Sha256
    empty_after_sha256: Sha256
    failed_native_start_sha256: Sha256
    journal_receipts: tuple[Sha256, ...] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def _exact_fallback(self):
        if (self.legacy_before_sha256 != self.legacy_after_sha256
                or self.empty_before_sha256 != self.empty_after_sha256
                or len(set(self.journal_receipts)) != len(self.journal_receipts)):
            raise ValueError("bootstrap did not restore exact EMPTY while preserving legacy")
        return self


class BaselineQualification(StrictModel):
    schema_version: Literal["vkm-closed-baseline/1"] = "vkm-closed-baseline/1"
    scope: Literal["SYNTHETIC", "BOOTSTRAP_SHADOW_PRODUCTION"]
    status: Literal["CLOSED_BASELINE_QUALIFIED"] = "CLOSED_BASELINE_QUALIFIED"
    admission: Literal["CLOSED_BASELINE"] = "CLOSED_BASELINE"
    scientific_admission: Literal[False] = False
    intent_sha256: Sha256
    startup_authority_sha256: Sha256
    preparation_sha256: Sha256
    baseline_generation_sha256: Sha256
    private_probe_receipt_sha256: Sha256
    boundary_receipt_sha256: Sha256
    repeated_native_start_sha256: Sha256
    legacy_topology_sha256: Sha256
    closed_owner_key: Sha256


class BaselineRegistration(StrictModel):
    schema_version: Literal["vkm-baseline-registration/1"] = "vkm-baseline-registration/1"
    scope: Literal["SYNTHETIC", "BOOTSTRAP_SHADOW_PRODUCTION"]
    intent: BoundFile
    probes: BoundFile
    failed_preparation: BoundFile
    preparation: BoundFile
    private_probe: BoundFile
    boundary: BoundFile
    native_start: BoundFile
    qualification: BoundFile


class IndependentBootstrapBackup(StrictModel):
    schema_version: Literal["vkm-bootstrap-backup/1"] = "vkm-bootstrap-backup/1"
    status: Literal["INDEPENDENT_COPY_AND_RESTORE_VERIFIED"] = "INDEPENDENT_COPY_AND_RESTORE_VERIFIED"
    scope: Literal["BOOTSTRAP_SHADOW_PRODUCTION"] = "BOOTSTRAP_SHADOW_PRODUCTION"
    candidate_sha256: Sha256
    legacy_topology_sha256: Sha256
    inventory_sha256: Sha256
    source_failure_domain: Identifier
    independent_failure_domain: Identifier
    verified_files: int = Field(gt=0, le=10_000_000)
    verified_bytes: int = Field(gt=0)
    copy_verification: BoundFile
    restore_verification: BoundFile

    @model_validator(mode="after")
    def _independent(self):
        if self.source_failure_domain == self.independent_failure_domain:
            raise ValueError("copy in the source failure domain is not independent backup")
        if self.copy_verification == self.restore_verification:
            raise ValueError("copy verification cannot replace an actual restore receipt")
        return self


def require_independent_bootstrap_backup(intent):
    """Consume frozen operational copy/restore evidence, never a generic PASS.

    The separately qualified backup verifier produces these reports after actual
    copying and restore. This consumer performs no copying or full-corpus reads.
    """
    from vkm_corpus.update.operator_units import bound_json
    from vkm_corpus.update.generation import GenerationUnavailable
    if intent.scope == "SYNTHETIC":
        return None
    proof = IndependentBootstrapBackup.model_validate(bound_json(intent.independent_backup))
    candidate = intent.retained_startup.candidate if intent.retained_startup else None
    if (candidate is None or proof.candidate_sha256 != record_hash(candidate)
            or proof.legacy_topology_sha256 != intent.legacy.sha256):
        raise GenerationUnavailable("backup evidence belongs to another baseline")
    common = {"inventory_sha256": proof.inventory_sha256, "candidate_sha256": proof.candidate_sha256,
        "legacy_topology_sha256": proof.legacy_topology_sha256, "expected_files": proof.verified_files,
        "verified_files": proof.verified_files, "verified_bytes": proof.verified_bytes,
        "missing_files": 0, "corrupt_files": 0, "status": "VERIFIED"}
    copy = bound_json(proof.copy_verification)
    restore = bound_json(proof.restore_verification)
    if (not isinstance(copy, dict) or not isinstance(restore, dict)
            or type(restore.get("source_untouched")) is not bool
            or any(type(report.get(key)) is not int for report in (copy, restore)
                   for key in ("expected_files", "verified_files", "verified_bytes", "missing_files", "corrupt_files"))
            or copy != {"schema": "vkm-bootstrap-copy-verification/1", **common,
                "source_failure_domain": proof.source_failure_domain,
                "target_failure_domain": proof.independent_failure_domain}
            or restore != {"schema": "vkm-bootstrap-restore-verification/1", **common,
                "backup_failure_domain": proof.independent_failure_domain,
                "source_untouched": True}):
        raise GenerationUnavailable("actual copy and restore verification evidence is incomplete")
    return proof


def require_closed_baseline(registration: BoundFile, manifest, *, production=True):
    from vkm_corpus.update.bootstrap_verify import require_closed_baseline as verify
    return verify(registration, manifest, production=production)


class BootstrapPreparation(StrictModel):
    schema_version: Literal["vkm-bootstrap-preparation/1"] = "vkm-bootstrap-preparation/1"
    status: Literal["PREPARED_CLOSED_NOT_QUALIFIED"] = "PREPARED_CLOSED_NOT_QUALIFIED"
    intent_sha256: Sha256
    startup_authority_sha256: Sha256
    recipe_sha256: Sha256
    runtime_sha256: Sha256
    # Captured from actual stopped containers, never invented expected hashes.
    receiver_release: dict
    native_container_ids: dict[Literal["api", "mcp"], Sha256]

    @model_validator(mode="after")
    def _captured(self):
        from vkm_corpus.update.operator_units import ComposeRelease
        release = ComposeRelease.model_validate(self.receiver_release)
        if release.project != "vkm-core-shadow" or set(self.native_container_ids) != {"api", "mcp"}:
            raise ValueError("cold prepare must capture the isolated receiver pair")
        return self


def require_closed_startup(runtime, manifest, api_config):
    """Fresh startup authority with a permanently closed public capability.

    This function intentionally returns neither READY nor a serving receipt.
    Native binders use the byte pins; public guards must remain UNAVAILABLE even
    if another process writes an OPEN admission record.
    """
    from vkm_corpus.api.production import (serving_access_identity,
        serving_code_identity, serving_dependencies_identity, require_navigation_runtime)
    from vkm_corpus.update.admission import read_state
    from vkm_corpus.update.generation import GenerationUnavailable
    from vkm_corpus.update.operator_units import bound_json

    ref = runtime.config.bootstrap_startup
    if ref is None or api_config.deployment_token is None:
        raise GenerationUnavailable("closed bootstrap requires explicit operator authority")
    authority = BootstrapStartupAuthority.model_validate(bound_json(ref))
    root = Path(runtime.config.runtime_root).absolute() / "served"
    state = read_state(root)
    pin = authority.candidate
    if (authority.sha256 != ref.sha256 or Path(authority.control_root).absolute() != root
            or state.status != "CLOSED" or state.request_key != authority.request_key
            or manifest.acceptance_sha256 != authority.sha256
            or manifest.code_commit != pin.code_commit or manifest.policy_sha256 != pin.policy_sha256
            or {c.component: c.model_dump(mode="json") for c in manifest.components} != pin.component_map
            or {s.service: s.model_dump(mode="json") for s in manifest.services} != pin.service_map
            or serving_code_identity() != pin.code_tree_sha256
            or serving_dependencies_identity() != pin.dependencies_sha256
            or serving_access_identity(api_config) != pin.access_config_sha256
            or runtime.config.expected_commit != pin.code_commit
            or runtime.config.policy.sha256 != pin.policy_sha256):
        raise GenerationUnavailable("closed bootstrap authority/native context differs")
    # The checked file must also be outside both mutable and protected roots.
    # RuntimeConfig enforces this before startup, bound_json rejects indirection.
    require_navigation_runtime()
    return {"duckdb_file_sha256": pin.duckdb_file_sha256,
            "nav_file_sha256": pin.nav_file_sha256, "mode": authority.mode}

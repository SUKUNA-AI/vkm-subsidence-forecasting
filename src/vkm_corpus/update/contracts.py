from __future__ import annotations

from typing import Literal, Any

from pydantic import Field, model_validator

from vkm_evidence.contracts import Identifier, Sha256, StrictModel, record_hash
from vkm_datasets.contracts import CampaignDatasetInput, DATASET_OPTIONS


class CampaignInput(StrictModel):
    source_id: Identifier
    source_sha256: Sha256
    logical_path: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    lifecycle: Literal["ACTIVE", "RETIRED", "MISSING", "EXCLUDED"]
    reason: str | None = None
    dataset_version_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def _reason(self):
        if self.dataset_version_sha256 is not None:
            raise ValueError("dataset input requires CampaignDatasetInput; document adapters cannot verify dataset versions")
        if self.lifecycle != "ACTIVE" and not self.reason:
            raise ValueError("excluded inputs need an explicit reason")
        if self.logical_path.startswith(("/", "\\")) or ":" in self.logical_path or ".." in self.logical_path.replace("\\", "/").split("/"):
            raise ValueError("input path must be relative to the protected original root")
        return self


class Stage(StrictModel):
    stage_id: Identifier
    operation: Literal["VERIFY_ORIGINALS", "EXTRACT", "COVERAGE", "EVIDENCE_VALIDATE", "EVIDENCE_PROJECT",
                       "CANON_VALIDATE", "BUILD_SHADOW", "ACCEPT_SHADOW", "SWITCH", "VERIFY_SWITCH",
                       "DATASET_VERIFY", "DATASET_INSPECT", "DATASET_CONVERT", "DATASET_REGISTER"]
    depends_on: tuple[Identifier, ...] = ()
    input_sha256: Sha256
    config_sha256: Sha256
    memory_gib: float = Field(gt=0)
    min_free_disk_gib: float = Field(ge=0)
    timeout_seconds: int = Field(gt=0)
    attempts: int = Field(1, ge=1, le=3)
    compute: Literal["CPU", "GPU", "WINDOWS_DESKTOP", "CORE_EDGE"]
    required_gates: tuple[Identifier, ...] = ()
    options: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _dataset_options(self):
        if self.operation in DATASET_OPTIONS:
            DATASET_OPTIONS[self.operation].model_validate(self.options)
        return self


class QualificationGate(StrictModel):
    gate_id: Identifier
    status: Literal["PASS", "FAIL", "NOT_RUN", "BLOCKED"]
    evidence_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def _receipt(self):
        if self.status == "PASS" and self.evidence_sha256 is None:
            raise ValueError("PASS requires qualification evidence")
        return self


class CampaignManifest(StrictModel):
    schema_version: Literal["vkm-production-campaign/1"] = "vkm-production-campaign/1"
    campaign_id: Identifier
    code_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    producer_identity_sha256: Sha256
    policy_sha256: Sha256
    inputs: tuple[CampaignInput, ...]
    dataset_inputs: tuple[CampaignDatasetInput, ...] = ()
    stages: tuple[Stage, ...]
    gates: tuple[QualificationGate, ...]
    scientific_use: str | None = None

    @model_validator(mode="after")
    def _dag(self):
        if len({i.source_id for i in self.inputs}) != len(self.inputs):
            raise ValueError("duplicate campaign input")
        dataset_ids = {i.dataset_id for i in self.dataset_inputs}
        if len(dataset_ids) != len(self.dataset_inputs):
            raise ValueError("duplicate campaign dataset")
        selected = set()
        for stage in self.stages:
            if stage.operation in DATASET_OPTIONS:
                opts = DATASET_OPTIONS[stage.operation].model_validate(stage.options)
                targets = set(opts.dataset_ids) if stage.operation == "DATASET_REGISTER" else {opts.dataset_id}
                if not targets <= dataset_ids:
                    raise ValueError("dataset stage references undeclared input")
                selected.update(targets)
        if selected != dataset_ids:
            raise ValueError("campaign dataset has no typed operation")
        ids = {s.stage_id for s in self.stages}
        if len(ids) != len(self.stages) or len({g.gate_id for g in self.gates}) != len(self.gates):
            raise ValueError("duplicate campaign stage/gate")
        gates = {g.gate_id for g in self.gates}
        done = set()
        for s in self.stages:
            if not set(s.depends_on).issubset(done):
                raise ValueError("stages must be in dependency order, no cycles/dangling references")
            if not set(s.required_gates).issubset(gates):
                raise ValueError("unknown qualification gate")
            done.add(s.stage_id)
        return self

    @property
    def sha256(self) -> str:
        return record_hash(self)


class ComponentIdentity(StrictModel):
    component: Literal["DOCUMENT", "DATASET", "EVIDENCE", "DUCKDB", "NAV", "GRAPH", "SEARCH", "DENSE", "LATE", "VISUAL"]
    revision: str = Field(min_length=1)
    manifest_sha256: Sha256
    policy_sha256: Sha256
    built_from: dict[str, str]
    required: bool = True


class ServiceIdentity(StrictModel):
    service: Literal["RETRIEVAL", "RERANK", "CONTROL"]
    instance_sha256: Sha256
    code_sha256: Sha256
    dependencies_sha256: Sha256
    config_sha256: Sha256
    endpoint_sha256: Sha256
    runtime_sha256: Sha256
    resources: dict[Identifier, Sha256] = Field(default_factory=dict)
    capabilities: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def _unique_capabilities(self):
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("duplicate service capability")
        return self


class GenerationManifest(StrictModel):
    schema_version: Literal["vkm-served-generation/1"] = "vkm-served-generation/1"
    components: tuple[ComponentIdentity, ...]
    services: tuple[ServiceIdentity, ...] = ()
    code_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    policy_sha256: Sha256
    acceptance_sha256: Sha256

    @model_validator(mode="after")
    def _coherent(self):
        mapping = {c.component: c for c in self.components}
        if len({s.service for s in self.services}) != len(self.services):
            raise ValueError("duplicate service identity")
        if len(mapping) != len(self.components) or "DOCUMENT" not in mapping:
            raise ValueError("duplicate components or missing DOCUMENT identity")
        for c in self.components:
            if c.policy_sha256 != self.policy_sha256:
                raise ValueError("mixed access-policy generations")
            for parent, revision in c.built_from.items():
                if parent not in mapping or mapping[parent].revision != revision:
                    raise ValueError("mixed component generations")
        return self

    @property
    def sha256(self):
        return record_hash(self)

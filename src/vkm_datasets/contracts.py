"""Typed update/catalogue contracts layered over immutable DatasetVersion v1."""
from typing import Literal

from pydantic import Field, model_validator

from vkm_corpus.contracts.access import ResourcePolicy
from vkm_evidence.contracts import Identifier, Sha256, StrictModel, record_hash
from .manifest import ID, relative_name


class SourceVersionLink(StrictModel):
    source_id: str = Field(pattern=r"^VKM-SRC-[0-9]{3}$")
    source_sha256: Sha256
    relation: Literal["DESCRIBED_BY", "DERIVED_FROM", "DISTRIBUTED_WITH", "PRIMARY_SOURCE"]


class CampaignDatasetInput(StrictModel):
    dataset_id: Identifier
    manifest_artifact: Identifier
    version_sha256: Sha256
    originals_prefix: str
    source_register_artifact: Identifier | None = None
    source_links: tuple[SourceVersionLink, ...] = ()
    lifecycle: Literal["ACTIVE", "RETIRED", "EXCLUDED"] = "ACTIVE"
    reason: str | None = None

    @model_validator(mode="after")
    def _valid(self):
        if not ID.fullmatch(self.dataset_id):
            raise ValueError("invalid logical dataset ID")
        relative_name(self.originals_prefix)
        if self.lifecycle != "ACTIVE" and not self.reason:
            raise ValueError("inactive dataset requires reason")
        if bool(self.source_links) != bool(self.source_register_artifact):
            raise ValueError("source links require a pinned SOURCE_REGISTER artifact")
        if len({x.source_id for x in self.source_links}) != len(self.source_links):
            raise ValueError("duplicate dataset source link")
        return self


class DatasetVerifyOptions(StrictModel):
    dataset_id: Identifier
    context_artifact: Identifier


class DatasetInspectOptions(DatasetVerifyOptions):
    entrypoint: str
    include_values: bool = False
    workbook_limits: dict[str, int] = Field(default_factory=dict)
    vector_limits: dict[str, int] = Field(default_factory=dict)
    keys: dict[str, tuple[str, ...]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _limits(self):
        from .workbooks import Limits
        from .gis import VectorLimits
        relative_name(self.entrypoint)
        for values in (self.workbook_limits, self.vector_limits):
            if any(type(v) is not int or v <= 0 for v in values.values()):
                raise ValueError("dataset limits must be positive integers")
        try:
            Limits(**self.workbook_limits)
            VectorLimits(**self.vector_limits)
        except TypeError as exc:
            raise ValueError("unknown dataset limit") from exc
        if any(not k or not v or len(set(v)) != len(v) or any(not c for c in v) for k, v in self.keys.items()):
            raise ValueError("invalid dataset keys")
        return self


class DatasetConvertOptions(DatasetInspectOptions):
    include_values: Literal[False] = False


class DatasetRegisterOptions(StrictModel):
    dataset_ids: tuple[Identifier, ...] = Field(min_length=1)
    context_artifact: Identifier
    previous_snapshot_artifact: Identifier | None = None

    @model_validator(mode="after")
    def _unique(self):
        if len(set(self.dataset_ids)) != len(self.dataset_ids):
            raise ValueError("duplicate dataset selection")
        return self


DATASET_OPTIONS = {"DATASET_VERIFY": DatasetVerifyOptions, "DATASET_INSPECT": DatasetInspectOptions,
                   "DATASET_CONVERT": DatasetConvertOptions, "DATASET_REGISTER": DatasetRegisterOptions}


class DatasetArtifact(StrictModel):
    path: str
    sha256: Sha256

    @model_validator(mode="after")
    def _path(self):
        relative_name(self.path)
        return self


class DatasetSelection(StrictModel):
    dataset_id: Identifier
    version_sha256: Sha256
    manifest: DatasetArtifact
    lifecycle: Literal["ACTIVE", "RETIRED", "EXCLUDED"]
    reason: str | None = None
    policy: ResourcePolicy
    source_links: tuple[SourceVersionLink, ...]
    inherited_source_links: tuple[SourceVersionLink, ...] = ()
    source_register_sha256: Sha256 | None
    inspections: dict[str, DatasetArtifact]
    conversions: dict[str, DatasetArtifact]

    @model_validator(mode="after")
    def _selection(self):
        if not ID.fullmatch(self.dataset_id):
            raise ValueError("invalid logical dataset ID")
        if self.lifecycle != "ACTIVE" and not self.reason:
            raise ValueError("inactive dataset requires reason")
        if len({x.source_id for x in self.source_links}) != len(self.source_links):
            raise ValueError("duplicate dataset source link")
        lineage = [(x.source_id, x.source_sha256, x.relation) for x in self.inherited_source_links]
        if lineage != sorted(set(lineage)):
            raise ValueError("inherited source links must be unique and ordered")
        if bool(self.source_links) != bool(self.source_register_sha256):
            raise ValueError("source links require SOURCE_REGISTER identity")
        for entrypoint in (*self.inspections, *self.conversions):
            relative_name(entrypoint)
        if self.lifecycle != "ACTIVE" and (self.inspections or self.conversions):
            raise ValueError("inactive selection cannot serve native outputs")
        return self


class DatasetCatalogSnapshot(StrictModel):
    schema_version: Literal["vkm-dataset-catalogue/1"] = "vkm-dataset-catalogue/1"
    policy_sha256: Sha256
    producer_identity_sha256: Sha256
    previous_snapshot_sha256: Sha256 | None = None
    entries: tuple[DatasetSelection, ...]
    scientific_admission: Literal["NOT_ESTABLISHED"] = "NOT_ESTABLISHED"

    @model_validator(mode="after")
    def _unique(self):
        if len({e.dataset_id for e in self.entries}) != len(self.entries):
            raise ValueError("duplicate logical dataset selection")
        if tuple(e.dataset_id for e in self.entries) != tuple(sorted(e.dataset_id for e in self.entries)):
            raise ValueError("catalogue entries must be in identity order")
        return self

    @property
    def sha256(self):
        return record_hash(self)

"""Base model configuration and nested structs shared by the dataset rows."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from vkm_corpus.contracts.fieldtypes import ArtifactId, Int16, Int32, Sha256Hex
from vkm_corpus.contracts.vocab import (
    AuthorRole,
    ExternalIdRelation,
    ExternalIdScheme,
    MetadataBasis,
    ModelRole,
    RawArtifactRole,
    VectorFormat,
)


class ContractModel(BaseModel):
    """Strict, immutable base: unknown keys are errors, enum fields hold plain string values."""

    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True, validate_default=True,
                              protected_namespaces=())


# ---------------------------------------------------------------- nested structs (Arrow struct / list<struct>)
class ModelRef(ContractModel):
    """A model that produced the region (LAYOUT) or the content (RECOGNITION) of an object (H-03)."""

    role: ModelRole
    model_id: str = Field(min_length=1)
    model_revision: str = Field(min_length=1)


class RawArtifactRef(ContractModel):
    """A raw artifact behind an object, with its role (H-24): e.g. find_tables JSON and the OCR response."""

    role: RawArtifactRole
    artifact_id: ArtifactId


class VectorArtifactRef(ContractModel):
    """Native vector representation of a figure region (H-24): paths JSON and SVG, in PAGE_PT_TL."""

    format: VectorFormat
    artifact_id: ArtifactId


class TableCell(ContractModel):
    row: Int32 = Field(ge=0)
    col: Int32 = Field(ge=0)
    row_span: Int16 = Field(1, ge=1)
    col_span: Int16 = Field(1, ge=1)
    is_header: bool = False
    text: str


class ArtifactRecipe(ContractModel):
    """How to reproduce an artifact that is not stored (H-23) or how it was produced."""

    tool: str = Field(min_length=1)
    tool_version: str = Field(min_length=1)
    profile: str = Field(min_length=1, description="render/extraction profile id, e.g. r200g")
    source_sha256: Sha256Hex | None = None
    page_id: str | None = None
    params_json: str = Field("{}", description="canonical JSON of the remaining parameters")


class ExternalId(ContractModel):
    scheme: ExternalIdScheme
    value: str = Field(min_length=1)
    relation: ExternalIdRelation


class WorkMetadataBasis(ContractModel):
    """Basis of each group of bibliographic fields of a Work."""

    title: MetadataBasis
    authors: MetadataBasis
    year: MetadataBasis
    venue: MetadataBasis
    identifiers: MetadataBasis


class RunModelInfo(ContractModel):
    """A model used by a processing run: revision and weights hash identify it; backend info is recorded but is not
    part of any cache signature (H-05)."""

    role: ModelRole
    model_id: str = Field(min_length=1)
    model_revision: str = Field(min_length=1)
    weights_sha256: Sha256Hex | None = None
    backend: str | None = None
    backend_version: str | None = None
    quantization: str | None = None
    device: str | None = None


class CuratedAuthor(ContractModel):
    """Parsed author entry of a curated Work row (loader helper, not a dataset)."""

    ordinal: Int16 = Field(ge=1)
    name_as_listed: str = Field(min_length=1)
    role: AuthorRole = AuthorRole.AUTHOR

    @field_validator("name_as_listed")
    @classmethod
    def _strip(cls, v: str) -> str:
        if v != v.strip():
            raise ValueError("name_as_listed must be stripped")
        return v


__all__ = ["ContractModel", "ModelRef", "RawArtifactRef", "VectorArtifactRef", "TableCell", "ArtifactRecipe",
           "ExternalId", "WorkMetadataBasis", "RunModelInfo", "CuratedAuthor"]

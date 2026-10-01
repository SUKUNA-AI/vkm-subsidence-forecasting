"""Strict evidence-layer contracts. Original occurrences are never overwritten."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator, model_serializer

from vkm_corpus.contracts.access import ResourcePolicy
from vkm_world.core.provenance import Provenance, Quantity, TemporalSupport, Scope, Scale, Transfer

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,199}$")]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def canonical_bytes(value) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def record_hash(value) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True, allow_inf_nan=False)


class ReviewState(StrEnum):
    UNREVIEWED = "UNREVIEWED"
    VERIFIED_TRANSCRIPTION = "VERIFIED_TRANSCRIPTION"
    SEMANTIC_REVIEWED = "SEMANTIC_REVIEWED"
    CONFLICT = "CONFLICT"
    REJECTED = "REJECTED"


class VersionRef(StrictModel):
    record_id: Identifier
    record_sha256: Sha256


class ObjectRef(StrictModel):
    source_id: Identifier
    source_sha256: Sha256
    snapshot_id: Identifier
    object_id: Identifier
    object_version: str = Field(min_length=1, max_length=256)
    content_sha256: Sha256
    locator: str = Field(min_length=1, max_length=2000)
    extraction_generation: str = Field(min_length=1, max_length=200)
    # Bounded spans are verified against the authoritative original object.
    char_start: int | None = Field(None, ge=0)
    char_end: int | None = Field(None, ge=0)
    fragment_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def _span(self):
        if (self.char_start is None) != (self.char_end is None):
            raise ValueError("both span boundaries are required")
        if self.char_start is not None and self.char_end <= self.char_start:
            raise ValueError("empty or reversed span")
        if self.char_start is not None and self.fragment_sha256 is None:
            raise ValueError("a text span requires its fragment hash")
        return self


class OriginSlice(StrictModel):
    """A reviewed primary origin and explicit subset; empty members means whole set."""
    origin_id: Identifier
    members: tuple[Identifier, ...] = ()
    independence_basis: str = Field(min_length=1)
    verified: bool = False

    @field_validator("members")
    @classmethod
    def _unique(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("duplicate origin member")
        return values


class RecordBase(StrictModel):
    schema_version: Literal["vkm-evidence/1"] = "vkm-evidence/1"
    record_id: Identifier
    revision: int = Field(1, ge=1)
    supersedes: VersionRef | None = None
    recorded_at: datetime
    actor: str = Field(min_length=1, max_length=200)
    policy: ResourcePolicy
    review_state: ReviewState = ReviewState.UNREVIEWED
    supports: tuple[ObjectRef, ...] = ()
    depends_on: tuple[VersionRef, ...] = ()
    references: tuple[VersionRef, ...] = ()  # pinned citation/association edges, allowed to be cyclic
    time: TemporalSupport = Field(default_factory=TemporalSupport)

    @field_validator("recorded_at")
    @classmethod
    def _utc(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("recorded_at must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _version(self):
        if (self.revision > 1) != (self.supersedes is not None):
            raise ValueError("a correction requires the previous exact version")
        if self.supersedes and self.supersedes.record_id != self.record_id:
            raise ValueError("supersedes must refer to the same logical record")
        if len({r.record_id for r in self.depends_on}) != len(self.depends_on):
            raise ValueError("duplicate dependency")
        return self

    @property
    def version_ref(self) -> VersionRef:
        return VersionRef(record_id=self.record_id, record_sha256=record_hash(self))


class Mention(RecordBase):
    kind: Literal["MENTION"] = "MENTION"
    literal: str = Field(min_length=1)
    entity_candidates: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def _support(self):
        if not self.supports:
            raise ValueError("mention requires an original occurrence")
        return self


class Claim(RecordBase):
    kind: Literal["CLAIM"] = "CLAIM"
    proposition: str = Field(min_length=1)
    attribution: str = Field(min_length=1)
    polarity: Literal["AFFIRMED", "NEGATED", "UNCERTAIN"]
    modality: Literal["OBSERVED", "REPORTED", "HYPOTHESIZED", "PLANNED", "NORMATIVE", "UNKNOWN"]
    qualifiers: tuple[str, ...] = ()
    provenance: Provenance

    @model_validator(mode="after")
    def _support(self):
        if not self.supports:
            raise ValueError("claim requires exact source support")
        return self


class Observation(RecordBase):
    kind: Literal["OBSERVATION"] = "OBSERVATION"
    original_value: str
    value_state: Literal["VALUE", "EMPTY", "MISSING", "UNREADABLE", "NOT_APPLICABLE"]
    quantity: Quantity | None = None
    entity_id: Identifier | None = None
    method: str | None = None
    origins: tuple[OriginSlice, ...] = ()
    time: TemporalSupport = Field(default_factory=TemporalSupport)

    @model_validator(mode="after")
    def _value(self):
        if not self.supports:
            raise ValueError("observation requires original support")
        if self.value_state != "VALUE" and self.quantity is not None:
            raise ValueError("non-value cell cannot carry a physical quantity")
        return self


class ObservationSet(RecordBase):
    kind: Literal["OBSERVATION_SET"] = "OBSERVATION_SET"
    observation_ids: tuple[Identifier, ...] = ()
    origins: tuple[OriginSlice, ...] = ()
    campaign_id: Identifier | None = None
    dataset_version_id: Identifier | None = None
    lineage_state: Literal["VERIFIED", "POSSIBLE_OVERLAP", "UNKNOWN"] = "UNKNOWN"

    @model_validator(mode="after")
    def _origins(self):
        if len(set(self.observation_ids)) != len(self.observation_ids):
            raise ValueError("duplicate observation")
        if self.lineage_state == "VERIFIED" and (not self.origins or not all(x.verified for x in self.origins)):
            raise ValueError("verified lineage requires verified primary origins")
        return self


class Entity(RecordBase):
    kind: Literal["ENTITY"] = "ENTITY"
    entity_type: str = Field(min_length=1)
    label: str = Field(min_length=1)
    site_scope: str = "UNSTATED"
    aliases: tuple[str, ...] = ()
    identity_state: Literal["CANDIDATE", "RESOLVED", "AMBIGUOUS"] = "CANDIDATE"


class EntityResolutionDecision(RecordBase):
    kind: Literal["ENTITY_RESOLUTION"] = "ENTITY_RESOLUTION"
    participants: tuple[Identifier, ...] = Field(min_length=2)
    decision: Literal["SAME_ENTITY", "DISTINCT", "SPLIT", "AMBIGUOUS"]
    rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def _review(self):
        if len(set(self.participants)) != len(self.participants):
            raise ValueError("duplicate entity participant")
        if self.decision != "AMBIGUOUS" and self.review_state != ReviewState.SEMANTIC_REVIEWED:
            raise ValueError("identity resolution requires semantic review")
        return self


class SymbolBinding(StrictModel):
    symbol: str = Field(min_length=1)
    definition: str = Field(min_length=1)
    scope: str = Field(min_length=1)
    unit: str | None = None
    role: Literal["LHS", "RHS", "UNKNOWN"] = "UNKNOWN"
    supports: tuple[ObjectRef, ...] = Field(min_length=1)


class FormulaInterpretation(RecordBase):
    kind: Literal["FORMULA_INTERPRETATION"] = "FORMULA_INTERPRETATION"
    original_form: str = Field(min_length=1)
    representation: Literal["LATEX", "OMML", "MATHML", "TEXT", "IMAGE_ONLY"]
    normalized_form: str | None = None
    parse_state: Literal["PARSED", "UNPARSED", "IMAGE_ONLY", "CONFLICT"] = "UNPARSED"
    symbols: tuple[SymbolBinding, ...] = ()
    assumptions: tuple[str, ...] = ()
    domain: str | None = None
    origin: Literal["AUTHOR", "CITED", "MODIFIED", "UNKNOWN"] = "UNKNOWN"
    conflicting_versions: tuple[Identifier, ...] = ()
    time: TemporalSupport = Field(default_factory=TemporalSupport)

    @model_validator(mode="after")
    def _source(self):
        if not self.supports:
            raise ValueError("formula interpretation requires original support")
        if self.parse_state == "PARSED" and not self.normalized_form:
            raise ValueError("PARSED needs a representation")
        return self


class EventAssertion(RecordBase):
    kind: Literal["EVENT_ASSERTION"] = "EVENT_ASSERTION"
    event_type: str = Field(min_length=1)
    event_class: Literal["PHYSICAL", "INFORMATION"]
    state: Literal["ACTUAL", "PLANNED", "HYPOTHETICAL", "UNKNOWN"]
    participants: tuple[Identifier, ...] = ()
    temporal_expression: str
    time: TemporalSupport
    revealed_by: tuple[Identifier, ...] = ()
    provenance: Provenance

    @model_validator(mode="after")
    def _source(self):
        if not self.supports:
            raise ValueError("event assertion requires exact source support")
        return self


class EvidenceRelation(RecordBase):
    kind: Literal["EVIDENCE_RELATION"] = "EVIDENCE_RELATION"
    subject: Identifier
    predicate: Literal["REPORTS", "USES_DATA_FROM", "DERIVED_FROM", "REPRINTS", "CORRECTS", "SUPPORTS",
                       "CONTRADICTS", "POSSIBLE_SAME_ENTITY", "SAME_OCCURRENCE", "SPLIT", "MERGED", "REMOVED", "AMBIGUOUS"]
    object: Identifier
    rationale: str = Field(min_length=1)


class ReviewDecision(RecordBase):
    kind: Literal["REVIEW_DECISION"] = "REVIEW_DECISION"
    target: VersionRef
    decision: Literal["VERIFIED_TRANSCRIPTION", "SEMANTIC_REVIEWED", "CONFLICT", "REJECTED", "REVOKED"]
    rationale: str = Field(min_length=1)
    source_verified: bool = False
    checks: tuple[str, ...] = ()
    reviewer_authority: str = Field(min_length=1)

    @model_validator(mode="after")
    def _review(self):
        if self.decision in {"VERIFIED_TRANSCRIPTION", "SEMANTIC_REVIEWED"}:
            if not self.source_verified or not self.supports:
                raise ValueError("verification requires original-source inspection")
        return self


class EvidenceTransfer(RecordBase):
    """Reviewed application of an explicit WorldSpec transfer to an exact value."""
    kind: Literal["EVIDENCE_TRANSFER"] = "EVIDENCE_TRANSFER"
    target: VersionRef
    transfer: Transfer

    @model_validator(mode="after")
    def _support(self):
        if not self.supports:
            raise ValueError("transfer assertion requires original support")
        return self


class ScientificTransferBinding(StrictModel):
    target: VersionRef
    transfer_record: VersionRef
    transfer_sha256: Sha256


class ScientificUseContext(StrictModel):
    use: Literal["SOURCE_INTERPRETATION", "IDENTITY", "GEOMETRY_INPUT", "MATERIAL_PARAMETER",
                 "CALIBRATION_INPUT", "VALIDATION_OBSERVATION", "PREDICTION_INPUT", "BENCHMARK_INPUT", "SCENARIO_INPUT"]
    site: Scope
    scale: Scale
    transfers: tuple[ScientificTransferBinding, ...] = ()

    @model_validator(mode="after")
    def _known(self):
        if self.site == Scope.UNSTATED or self.scale == Scale.UNSTATED:
            raise ValueError("scientific use needs an explicit site and scale")
        if len({b.target.record_id for b in self.transfers}) != len(self.transfers):
            raise ValueError("duplicate transfer target")
        return self


class ScientificUseAdmission(RecordBase):
    kind: Literal["SCIENTIFIC_USE_ADMISSION"] = "SCIENTIFIC_USE_ADMISSION"
    purpose: str = Field(min_length=1)
    origin: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    targets: tuple[VersionRef, ...] = Field(min_length=1)
    dependency_versions: tuple[VersionRef, ...]
    review_versions: tuple[VersionRef, ...]
    status: Literal["READY", "NOT_READY", "REVOKED"]
    reasons: tuple[str, ...] = ()
    policy_sha256: Sha256
    # Archival v1 records remain readable; an absent context cannot be READY.
    use_context: ScientificUseContext | None = None

    @model_serializer(mode="wrap")
    def _archive_bytes(self, handler):
        result = handler(self)
        if self.use_context is None:
            result.pop("use_context", None)
        return result

    @model_validator(mode="after")
    def _receipt(self):
        from datetime import date
        date.fromisoformat(self.origin)
        if self.status == "READY" and (not self.review_versions or self.reasons):
            raise ValueError("READY requires reviews and no blocking reasons")
        return self


EvidenceRecord = Annotated[Union[Mention, Claim, Observation, ObservationSet, Entity, EntityResolutionDecision,
                                  FormulaInterpretation, EventAssertion, EvidenceRelation, ReviewDecision,
                                  ScientificUseAdmission, EvidenceTransfer], Field(discriminator="kind")]


class EvidenceBatch(StrictModel):
    schema_version: Literal["vkm-evidence/1"] = "vkm-evidence/1"
    records: tuple[EvidenceRecord, ...]

    @model_validator(mode="after")
    def _unique(self):
        if len({r.record_id for r in self.records}) != len(self.records):
            raise ValueError("duplicate logical record in batch")
        return self

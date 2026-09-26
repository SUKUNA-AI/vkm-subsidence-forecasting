"""Provenance, epistemic status, uncertainty and support of every WorldSpec datum.

The WorldSpec never stores a bare number. Every quantity carries

* where it comes from (``SourceRef`` list: registered source + locator + evidence ids),
* what kind of knowledge it is (``EpistemicStatus``: FACT / DERIVATION / INTERPOLATION /
  MODEL_CHOICE / ENGINEERING_ASSUMPTION / ANALOGUE / UNKNOWN),
* the site scope and the physical scale it was obtained at (``Scope``, ``Scale``),
* where and when it is valid (``SpatialSupport``, ``TemporalSupport``),
* how uncertain it is (``Uncertainty``).

Scientific rules enforced here (see ``validate_quantity``):

* UNKNOWN stays UNKNOWN: an UNKNOWN quantity has no value.
* no data is not zero, no range is not a point: ranges are kept as ranges.
* FACT and DERIVATION need at least one source reference.
* ANALOGUE needs an explicit scope different from the target site.
* INTERPOLATION needs the method and the inputs it was built from.
* MODEL_CHOICE / ENGINEERING_ASSUMPTION need a written rationale.
* a LAB-scale value is never silently used as a MASSIF value (``transfer`` record required).
"""
from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EpistemicStatus(str, Enum):
    FACT = "FACT"
    DERIVATION = "DERIVATION"
    INTERPOLATION = "INTERPOLATION"
    MODEL_CHOICE = "MODEL_CHOICE"
    ENGINEERING_ASSUMPTION = "ENGINEERING_ASSUMPTION"
    ANALOGUE = "ANALOGUE"
    UNKNOWN = "UNKNOWN"


class EvidenceType(str, Enum):
    MEASURED = "MEASURED"
    FIELD_OBSERVATION = "FIELD_OBSERVATION"
    LAB_TEST = "LAB_TEST"
    NORMATIVE = "NORMATIVE"
    DESIGN_VALUE = "DESIGN_VALUE"
    CALCULATED_BY_AUTHOR = "CALCULATED_BY_AUTHOR"
    MODEL_CALIBRATED = "MODEL_CALIBRATED"
    TEACHING_EXAMPLE = "TEACHING_EXAMPLE"
    LITERATURE_CITED = "LITERATURE_CITED"
    INTERPRETATION = "INTERPRETATION"
    DESCRIPTIVE = "DESCRIPTIVE"
    COMPUTED_THIS_PROJECT = "COMPUTED_THIS_PROJECT"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class Scope(str, Enum):
    """Site scope of a datum. Only SKRU1 (and SKRU1_* joint scopes) are site-specific for SKRU-1."""

    SKRU1 = "SKRU1"
    SKRU1_SKRU2 = "SKRU1_SKRU2"
    SKRU2 = "SKRU2"
    SKRU3 = "SKRU3"
    BKPRU1 = "BKPRU1"
    BKPRU2 = "BKPRU2"
    BKPRU3 = "BKPRU3"
    BKPRU4 = "BKPRU4"
    USOLSKY = "USOLSKY"
    OTHER_VKM_SITE = "OTHER_VKM_SITE"
    VKM_REGIONAL = "VKM_REGIONAL"
    OTHER_POTASH_SITE = "OTHER_POTASH_SITE"
    NON_VKM = "NON_VKM"
    GENERAL_METHOD = "GENERAL_METHOD"
    PROJECT = "PROJECT"  # created by this project (derivation / model choice)
    UNSTATED = "UNSTATED"


SKRU1_SCOPES = frozenset({Scope.SKRU1, Scope.SKRU1_SKRU2})


class Scale(str, Enum):
    LAB = "LAB"
    MASSIF = "MASSIF"
    CALIBRATED_EFFECTIVE_MODEL = "CALIBRATED_EFFECTIVE_MODEL"
    FIELD = "FIELD"
    DESIGN = "DESIGN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class SpatialLevel(str, Enum):
    """Spatial hierarchy of the VKM / SKRU-1 world, coarse → fine."""

    DEPOSIT = "deposit"                 # ВКМКС
    DISTRICT = "district"               # район Соликамска
    MINE = "mine"                       # СКРУ-1
    NEIGHBOUR_MINE = "neighbour_mine"   # СКРУ-2, СКРУ-3 ...
    INTERMINE_PILLAR = "intermine_pillar"  # межрудничные / защитные целики
    EXPLORATION_AREA = "exploration_area"   # участок детальной разведки (e.g. Соликамский, Ново-Соликамский)
    DEPOSIT_PART = "deposit_part"           # северная/центральная/южная часть месторождения
    MINE_FIELD = "mine_field"
    SHAFT = "shaft"                         # ствол
    MINE_HORIZON = "mine_horizon"           # горизонт рудника (e.g. −113/−143/−240 m)
    PANEL = "panel"
    BLOCK = "block"
    SITE = "site"                       # участок
    WORKING = "working"                 # выработка
    CHAMBER = "chamber"
    PILLAR = "pillar"
    SEAM = "seam"
    INTERSEAM = "interseam"
    GEOLOGICAL_ELEMENT = "geological_element"
    BOREHOLE = "borehole"
    BOREHOLE_INTERVAL = "borehole_interval"
    GPR_PROFILE = "gpr_profile"
    SURVEY_LINE = "survey_line"
    BENCHMARK = "benchmark"
    SPECIMEN = "specimen"
    POINT = "point"
    UNSTATED = "unstated"


SPATIAL_LEVEL_ORDER: tuple[SpatialLevel, ...] = tuple(SpatialLevel)


class SourceRef(BaseModel):
    """Pointer into the registered private corpus (or an external source)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(..., description="VKM-SRC-xxx, EXT-SRC-xxx or EXTWEB-xxx")
    pdf_page: int | None = None
    printed_page: str | None = None
    locator: str | None = Field(None, description="table/figure/equation/section/rendered page")
    evidence_ids: tuple[str, ...] = ()
    extraction_method: str | None = None  # TEXT_LAYER / OCR / OCR_VISUALLY_CONFIRMED / VISUAL_READ / ...


class SpatialSupport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: SpatialLevel = SpatialLevel.UNSTATED
    name: str | None = None
    entity_id: str | None = Field(None, description="id of a node in the WorldSpec spatial hierarchy")


class TemporalSupport(BaseModel):
    """Physical time vs information time.

    ``event_date``        when the physical event happened (e.g. chamber mined);
    ``measurement_date``  when it was measured;
    ``processing_date``   when the measurement was processed/interpreted;
    ``publication_date``  when it was published;
    ``available_from``    the earliest date a forecaster could have used it (information availability).
    A forecast issued at origin t0 may only use data with ``available_from <= t0``.
    """

    model_config = ConfigDict(extra="forbid")

    event_date: date | None = None
    event_date_end: date | None = None
    measurement_date: date | None = None
    processing_date: date | None = None
    publication_date: date | None = None
    available_from: date | None = None
    precision: str = Field("unknown", description="day|month|year|decade|unknown")
    notes: str | None = None

    @model_validator(mode="after")
    def _ordered(self) -> "TemporalSupport":
        if self.event_date and self.event_date_end and self.event_date_end < self.event_date:
            raise ValueError("event_date_end precedes event_date")
        if self.measurement_date and self.processing_date and self.processing_date < self.measurement_date:
            raise ValueError("processing_date precedes measurement_date")
        if self.available_from:
            for name in ("measurement_date", "processing_date"):
                d = getattr(self, name)
                if d and self.available_from < d:
                    raise ValueError(f"available_from precedes {name}: information cannot be available before it exists")
        return self

    def usable_at(self, origin: date) -> bool | None:
        """True/False if availability is known, None if it is not (then the datum must not be used)."""
        if self.available_from is None:
            return None
        return self.available_from <= origin


class UncertaintyKind(str, Enum):
    NONE_STATED = "NONE_STATED"   # source gives a single number without uncertainty
    INTERVAL = "INTERVAL"         # [low, high] bounds, no distribution implied
    NORMAL = "NORMAL"
    LOGNORMAL = "LOGNORMAL"
    UNIFORM = "UNIFORM"
    TRIANGULAR = "TRIANGULAR"
    DISCRETE_SET = "DISCRETE_SET"  # competing hypotheses (e.g. K0 = 0.6 | 0.71 | 1.0)
    UNKNOWN = "UNKNOWN"


class UncertaintyComponent(str, Enum):
    MEASUREMENT = "measurement"
    SPATIAL = "spatial"
    TEMPORAL = "temporal"
    SOURCE_CONFLICT = "source_conflict"
    INTERPOLATION = "interpolation"
    MODEL_FORM = "model_form"
    PARAMETER = "parameter"
    TRANSFERABILITY = "transferability"
    ENGINEERING_ASSUMPTION = "engineering_assumption"


class Uncertainty(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: UncertaintyKind = UncertaintyKind.UNKNOWN
    params: dict[str, float] = Field(default_factory=dict)
    values: tuple[float, ...] = ()  # for DISCRETE_SET
    components: tuple[UncertaintyComponent, ...] = ()
    notes: str | None = None


class Transfer(BaseModel):
    """Explicit record of moving a value across scale or site (LAB→MASSIF, analogue→SKRU-1)."""

    model_config = ConfigDict(extra="forbid")

    from_scale: Scale | None = None
    to_scale: Scale | None = None
    from_scope: Scope | None = None
    to_scope: Scope | None = None
    method: str = Field(..., description="e.g. scale-effect factor from VKM-SRC-025 p.31, or ENGINEERING_ASSUMPTION")
    status: EpistemicStatus
    rationale: str


class Provenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: EpistemicStatus
    sources: tuple[SourceRef, ...] = ()
    evidence_type: EvidenceType = EvidenceType.NOT_APPLICABLE
    scope: Scope = Scope.UNSTATED
    scale: Scale = Scale.NOT_APPLICABLE
    spatial: SpatialSupport = Field(default_factory=SpatialSupport)
    temporal: TemporalSupport = Field(default_factory=TemporalSupport)
    method: str | None = Field(None, description="derivation / interpolation method")
    inputs: tuple[str, ...] = Field((), description="ids of WorldSpec items this was derived/interpolated from")
    rationale: str | None = Field(None, description="mandatory for MODEL_CHOICE / ENGINEERING_ASSUMPTION")
    transfer: Transfer | None = None
    confidence: str = "UNSTATED"
    notes: str | None = None

    @model_validator(mode="after")
    def _rules(self) -> "Provenance":
        errs = provenance_errors(self)
        if errs:
            raise ValueError("; ".join(errs))
        return self


def provenance_errors(p: Provenance) -> list[str]:
    errs: list[str] = []
    s = p.status
    if s in (EpistemicStatus.FACT, EpistemicStatus.DERIVATION) and not p.sources and not p.inputs:
        errs.append(f"{s.value} requires at least one source reference or derivation inputs")
    if s is EpistemicStatus.FACT and p.evidence_type in (EvidenceType.TEACHING_EXAMPLE,):
        errs.append("a TEACHING_EXAMPLE value can never be a FACT about the site")
    if s is EpistemicStatus.DERIVATION and not p.method:
        errs.append("DERIVATION requires the derivation method")
    if s is EpistemicStatus.INTERPOLATION and (not p.method or not p.inputs):
        errs.append("INTERPOLATION requires method and inputs")
    if s in (EpistemicStatus.MODEL_CHOICE, EpistemicStatus.ENGINEERING_ASSUMPTION) and not p.rationale:
        errs.append(f"{s.value} requires a written rationale")
    if s is EpistemicStatus.ANALOGUE and p.scope in SKRU1_SCOPES:
        errs.append("ANALOGUE cannot have an SKRU-1 scope")
    if s is EpistemicStatus.ANALOGUE and not p.sources:
        errs.append("ANALOGUE requires the analogue source")
    return errs


class Quantity(BaseModel):
    """A physical quantity with provenance. Point OR range OR discrete set OR nothing (UNKNOWN)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    unit: str = Field(..., description="SI or explicit unit string understood by vkm_world.core.units")
    value: float | None = None
    low: float | None = None
    high: float | None = None
    provenance: Provenance
    uncertainty: Uncertainty = Field(default_factory=Uncertainty)
    original: str | None = Field(None, description="value/unit exactly as printed in the source")

    @model_validator(mode="after")
    def _rules(self) -> "Quantity":
        errs = quantity_errors(self)
        if errs:
            raise ValueError(f"{self.name}: " + "; ".join(errs))
        return self

    @property
    def is_range(self) -> bool:
        return self.low is not None or self.high is not None


def quantity_errors(q: Quantity) -> list[str]:
    errs: list[str] = []
    has_any = q.value is not None or q.low is not None or q.high is not None or bool(q.uncertainty.values)
    if q.provenance.status is EpistemicStatus.UNKNOWN and has_any:
        errs.append("UNKNOWN stays UNKNOWN: an UNKNOWN quantity must not carry a value")
    if q.provenance.status is not EpistemicStatus.UNKNOWN and not has_any:
        errs.append("non-UNKNOWN quantity without any value/range/set (use status UNKNOWN)")
    if q.low is not None and q.high is not None and q.low > q.high:
        errs.append("low > high")
    if q.value is not None and q.low is not None and q.high is not None and not (q.low <= q.value <= q.high):
        errs.append("point value outside its own range")
    return errs


def check_scale_use(q: Quantity, required: Scale) -> list[str]:
    """Return errors if ``q`` is used at a scale it was not obtained at without an explicit Transfer."""
    p = q.provenance
    if p.scale in (required, Scale.NOT_APPLICABLE):
        return []
    if p.transfer and p.transfer.to_scale == required:
        return []
    return [f"{q.name}: {p.scale.value} value used as {required.value} without an explicit Transfer record"]


def check_site_use(q: Quantity, target: Scope = Scope.SKRU1) -> list[str]:
    """Return errors if an off-site value is used as a target-site value without ANALOGUE/Transfer."""
    p = q.provenance
    if p.scope in SKRU1_SCOPES or p.scope in (Scope.GENERAL_METHOD, Scope.PROJECT):
        return []
    if p.status in (EpistemicStatus.ANALOGUE, EpistemicStatus.MODEL_CHOICE, EpistemicStatus.ENGINEERING_ASSUMPTION):
        return []
    if p.transfer and p.transfer.to_scope == target:
        return []
    return [f"{q.name}: scope {p.scope.value} used for {target.value} with status {p.status.value} (needs ANALOGUE or Transfer)"]


def as_dict(obj: BaseModel) -> dict[str, Any]:
    return obj.model_dump(mode="json", exclude_none=True)

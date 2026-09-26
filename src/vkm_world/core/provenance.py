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

import calendar
import re
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
    """Site scope of a datum. Only SKRU1 is field-wide site-specific for SKRU-1.

    The SKRU-1/SKRU-2 joint labels are kept apart (review finding TRANSFER-023):
    ``SKRU1_SKRU2_PILLAR`` — the intermine pillar zone on the SKRU-1/SKRU-2 boundary (usable for SKRU-1 only locally,
    at the pillar, or through an explicit Transfer); ``SKRU1_OR_SKRU2_UNATTRIBUTED`` — the source does not say which
    of the two mines; ``SOLIKAMSK_GROUP`` — pooled over SKRU-1, SKRU-2 and SKRU-3. ``SKRU1_SKRU2`` is the legacy
    ambiguous label of the first Phase-1 catalogues; it is valid but never counts as SKRU-1 data.
    """

    SKRU1 = "SKRU1"
    SKRU1_SKRU2_PILLAR = "SKRU1_SKRU2_PILLAR"
    SKRU1_OR_SKRU2_UNATTRIBUTED = "SKRU1_OR_SKRU2_UNATTRIBUTED"
    SOLIKAMSK_GROUP = "SOLIKAMSK_GROUP"
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


# field-wide SKRU-1 data; the related scopes touch SKRU-1 at least partly (for filtering, never for silent use)
SKRU1_SCOPES = frozenset({Scope.SKRU1})
SKRU1_RELATED_SCOPES = frozenset({Scope.SKRU1, Scope.SKRU1_SKRU2_PILLAR, Scope.SKRU1_OR_SKRU2_UNATTRIBUTED,
                                  Scope.SOLIKAMSK_GROUP, Scope.SKRU1_SKRU2})
# scopes an ANALOGUE value can never carry (it would claim to be SKRU-1 data)
SITE_SPECIFIC_SKRU1_SCOPES = frozenset({Scope.SKRU1, Scope.SKRU1_SKRU2_PILLAR})


class Scale(str, Enum):
    LAB = "LAB"
    MASSIF = "MASSIF"
    CALIBRATED_EFFECTIVE_MODEL = "CALIBRATED_EFFECTIVE_MODEL"
    FIELD = "FIELD"
    DESIGN = "DESIGN"
    NOT_APPLICABLE = "NOT_APPLICABLE"   # the quantity has no material scale (a date, a geometry, a dimensionless code)
    UNSTATED = "UNSTATED"               # default: scale not recorded yet — an error wherever a scale is required


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


DATE_PRECISIONS = ("day", "month", "year", "decade", "unknown")
_PRECISION_RANK = {p: i for i, p in enumerate(DATE_PRECISIONS)}


def date_bounds(d: date, precision: str | None) -> tuple[date, date]:
    """Earliest and latest calendar day a date of the given precision can mean.

    ``unknown`` is treated like ``year`` (conservative for availability: a date of unknown precision is not
    assumed to be exact). Review finding CHRONOLOGY-014.
    """
    precision = precision or "unknown"
    if precision == "day":
        return d, d
    if precision == "month":
        return date(d.year, d.month, 1), date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])
    if precision == "decade":
        y0 = d.year - d.year % 10
        return date(y0, 1, 1), date(y0 + 9, 12, 31)
    return date(d.year, 1, 1), date(d.year, 12, 31)


_PARTIAL = re.compile(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?$|^(\d{3})(?:0s|x)$")


def parse_partial_date(text: str) -> tuple[date, str]:
    """Parse '1930', '1930-05', '1930-05-17', '1930s' / '193x' into (first day, precision)."""
    m = _PARTIAL.match(text.strip())
    if not m:
        raise ValueError(f"not a partial ISO date: {text!r}")
    if m.group(4):
        return date(int(m.group(4)) * 10, 1, 1), "decade"
    y, mo, d = m.group(1), m.group(2), m.group(3)
    if d:
        return date(int(y), int(mo), int(d)), "day"
    if mo:
        return date(int(y), int(mo), 1), "month"
    return date(int(y), 1, 1), "year"


def finer(p1: str | None, p2: str | None) -> bool:
    """True if precision p1 is strictly finer than p2."""
    return _PRECISION_RANK.get(p1 or "unknown", 4) < _PRECISION_RANK.get(p2 or "unknown", 4)


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
    precision: str = Field("unknown", description="precision of the event/measurement dates: day|month|year|decade|unknown")
    available_from_precision: str | None = Field(
        None, description="precision of available_from if it differs from ``precision`` (e.g. a publication year)")
    notes: str | None = None

    @model_validator(mode="after")
    def _ordered(self) -> "TemporalSupport":
        for name in ("precision", "available_from_precision"):
            v = getattr(self, name)
            if v is not None and v not in DATE_PRECISIONS:
                raise ValueError(f"{name} must be one of {DATE_PRECISIONS}, got {v!r}")
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

    def available_latest(self) -> date | None:
        """Last day the information may have become available (a year-precision date means 31 December)."""
        if self.available_from is None:
            return None
        return date_bounds(self.available_from, self.available_from_precision or self.precision)[1]

    def event_bounds(self) -> tuple[date, date] | None:
        """(earliest start, latest end) of the physical event, honouring ``precision``."""
        start = self.event_date or self.measurement_date
        if start is None:
            return None
        end = self.event_date_end or start
        return date_bounds(start, self.precision)[0], date_bounds(end, self.precision)[1]

    def usable_at(self, origin: date) -> bool | None:
        """True/False if availability is known, None if it is not (then the datum must not be used).

        Imprecise availability is resolved to its latest possible day: a book of «1999» is usable from 1999-12-31
        on, never from 1 January (review finding CHRONOLOGY-014, decision D-03)."""
        latest = self.available_latest()
        if latest is None:
            return None
        return latest <= origin


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
    scale: Scale = Scale.UNSTATED
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
    if s is EpistemicStatus.ANALOGUE and p.scope in SITE_SPECIFIC_SKRU1_SCOPES:
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
    """Return errors if ``q`` is used at a scale it was not obtained at without an explicit Transfer.

    UNSTATED is an error (review finding TRANSFER-024: the old default NOT_APPLICABLE let unscaled material values
    pass as MASSIF). NOT_APPLICABLE passes: it is reserved for quantities without a material scale; material
    parameters must state their scale (``MaterialParameter`` enforces it)."""
    p = q.provenance
    if p.scale is Scale.UNSTATED and required is not Scale.UNSTATED:
        return [f"{q.name}: scale not stated; record LAB/MASSIF/FIELD/DESIGN/CALIBRATED_EFFECTIVE_MODEL before using it "
                f"as {required.value}"]
    if p.scale in (required, Scale.NOT_APPLICABLE):
        return []
    if p.transfer and p.transfer.to_scale == required:
        return []
    return [f"{q.name}: {p.scale.value} value used as {required.value} without an explicit Transfer record"]


def check_site_use(q: Quantity, target: Scope = Scope.SKRU1, local_to_pillar: bool = False) -> list[str]:
    """Return errors if an off-site value is used as a target-site value without ANALOGUE/Transfer.

    * only the target scope itself passes silently;
    * ``SKRU1_SKRU2_PILLAR`` passes for SKRU-1 only when the use is local to the intermine pillar zone
      (``local_to_pillar=True``); field-wide use needs a Transfer (review findings ATTRIBUTION-007, TRANSFER-023);
    * unattributed (SKRU-1 or SKRU-2), pooled (SKRU-1+2+3), legacy SKRU1_SKRU2 and GENERAL_METHOD values need
      ANALOGUE / MODEL_CHOICE / ENGINEERING_ASSUMPTION status or a Transfer (TRANSFER-024);
    * PROJECT scope passes for this project's own DERIVATION / INTERPOLATION."""
    p = q.provenance
    if p.scope == target:
        return []
    if p.status in (EpistemicStatus.ANALOGUE, EpistemicStatus.MODEL_CHOICE, EpistemicStatus.ENGINEERING_ASSUMPTION):
        return []
    if p.transfer and p.transfer.to_scope == target:
        return []
    if target is Scope.SKRU1 and p.scope is Scope.SKRU1_SKRU2_PILLAR and local_to_pillar:
        return []
    if p.scope is Scope.PROJECT and p.status in (EpistemicStatus.DERIVATION, EpistemicStatus.INTERPOLATION):
        return []
    return [f"{q.name}: scope {p.scope.value} used for {target.value} with status {p.status.value} (needs ANALOGUE or Transfer)"]


def as_dict(obj: BaseModel) -> dict[str, Any]:
    return obj.model_dump(mode="json", exclude_none=True)

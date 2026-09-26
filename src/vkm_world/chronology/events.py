"""Explicit chronology of the world: physical events and information events.

Physical events change the massif (excavation, backfill, flooding ...). Information events create
knowledge about it (a survey campaign, a publication). Both live on one time axis but are typed
separately, because a forecaster at origin t0 may only use information whose ``available_from <= t0``.

Ordering rules checked by ``chronology_errors``:
* an event's end is not before its start (TemporalSupport);
* backfill of an object does not start before extraction of that object (or its ancestor) started;
* extraction/backfill inside a mine does not precede that mine's commissioning, if dated;
* extraction does not end before it starts across paired START/END events;
* every event references existing objects.
Dates as precise as the source gives them; imprecise dates carry ``precision``.
"""
from __future__ import annotations

from datetime import date
from enum import Enum

from pydantic import Field

from ..core.base import WorldObject, duplicate_ids
from ..core.provenance import TemporalSupport


class EventClass(str, Enum):
    PHYSICAL = "PHYSICAL"
    INFORMATION = "INFORMATION"


class EventType(str, Enum):
    # physical
    GEOLOGICAL = "GEOLOGICAL"                       # formation/tectonic/dissolution (geological time)
    EXPLORATION_DRILLING = "EXPLORATION_DRILLING"
    MINE_CONSTRUCTION = "MINE_CONSTRUCTION"
    SHAFT_SINKING = "SHAFT_SINKING"
    MINE_COMMISSIONING = "MINE_COMMISSIONING"       # ввод в эксплуатацию
    DEVELOPMENT = "DEVELOPMENT"                     # проходка подготовительных выработок
    EXTRACTION_START = "EXTRACTION_START"
    EXTRACTION_END = "EXTRACTION_END"
    EXTRACTION_PERIOD = "EXTRACTION_PERIOD"
    BACKFILL_START = "BACKFILL_START"
    BACKFILL_END = "BACKFILL_END"
    BACKFILL_PERIOD = "BACKFILL_PERIOD"
    BACKFILL_STATE_CHANGE = "BACKFILL_STATE_CHANGE"
    TECHNOLOGY_CHANGE = "TECHNOLOGY_CHANGE"         # e.g. switch drill-and-blast → combine, start of hydraulic backfill
    INTERSEAM_FAILURE = "INTERSEAM_FAILURE"         # разрушение междупластья (e.g. block 129, 1984–85)
    ROOF_FALL = "ROOF_FALL"
    PILLAR_FAILURE = "PILLAR_FAILURE"
    COLLAPSE = "COLLAPSE"
    DYNAMIC_EVENT = "DYNAMIC_EVENT"                 # горный удар / ГДЯ / seismic event
    WATER_INFLOW = "WATER_INFLOW"
    FLOODING = "FLOODING"
    HYDRO_REGIME_CHANGE = "HYDRO_REGIME_CHANGE"
    MINE_CLOSURE = "MINE_CLOSURE"
    # information
    MONITORING_CAMPAIGN = "MONITORING_CAMPAIGN"
    SURVEY_OF_WORKINGS = "SURVEY_OF_WORKINGS"       # маркшейдерская съёмка выработок
    LAB_TESTING = "LAB_TESTING"
    GEOPHYSICAL_SURVEY = "GEOPHYSICAL_SURVEY"
    GEOLOGICAL_MODEL_UPDATE = "GEOLOGICAL_MODEL_UPDATE"
    DATA_PROCESSING = "DATA_PROCESSING"
    PUBLICATION = "PUBLICATION"
    NORMATIVE_IN_FORCE = "NORMATIVE_IN_FORCE"
    DESIGN_DOCUMENT = "DESIGN_DOCUMENT"             # проект / регламент / паспорт issued
    DATA_AVAILABILITY = "DATA_AVAILABILITY"         # e.g. satellite mission start, archive release
    OTHER = "OTHER"


INFORMATION_TYPES = frozenset({EventType.MONITORING_CAMPAIGN, EventType.SURVEY_OF_WORKINGS, EventType.LAB_TESTING,
                               EventType.GEOPHYSICAL_SURVEY, EventType.GEOLOGICAL_MODEL_UPDATE,
                               EventType.DATA_PROCESSING, EventType.PUBLICATION, EventType.NORMATIVE_IN_FORCE,
                               EventType.DESIGN_DOCUMENT, EventType.DATA_AVAILABILITY})
EXTRACTION_TYPES = frozenset({EventType.EXTRACTION_START, EventType.EXTRACTION_PERIOD})
BACKFILL_TYPES = frozenset({EventType.BACKFILL_START, EventType.BACKFILL_PERIOD})


class Event(WorldObject):
    event_type: EventType
    objects: tuple[str, ...] = Field((), description="ids of mining objects / spatial nodes / datasets affected")
    time: TemporalSupport
    description: str | None = None
    event_class_override: EventClass | None = Field(None, description="explicit class, required for OTHER events")

    @property
    def event_class(self) -> EventClass:
        if self.event_class_override is not None:
            return self.event_class_override
        return EventClass.INFORMATION if self.event_type in INFORMATION_TYPES else EventClass.PHYSICAL

    @property
    def start(self) -> date | None:
        return self.time.event_date or self.time.measurement_date

    @property
    def end(self) -> date | None:
        return self.time.event_date_end or self.start


def chronology_errors(events: list[Event], known_ids: set[str] | None = None,
                      parent_of: dict[str, str | None] | None = None,
                      mine_of: dict[str, str | None] | None = None) -> list[str]:
    """Consistency of the event list. ``parent_of`` maps object → parent object (for backfill-of-child),
    ``mine_of`` maps object → mine spatial id (for commissioning checks)."""
    errs = [f"duplicate event id {d}" for d in duplicate_ids(events)]
    parent_of = parent_of or {}
    mine_of = mine_of or {}
    if known_ids is not None:
        for e in events:
            for o in e.objects:
                if o not in known_ids:
                    errs.append(f"event {e.id}: unknown object '{o}'")

    def lineage(o: str) -> list[str]:
        out, cur = [o], parent_of.get(o)
        while cur and cur not in out:
            out.append(cur)
            cur = parent_of.get(cur)
        return out

    for e in events:
        if e.event_type is EventType.OTHER and e.event_class_override is None:
            errs.append(f"event {e.id}: OTHER event must state event_class_override (PHYSICAL or INFORMATION)")
    first_extraction: dict[str, date] = {}
    for e in events:
        if e.event_type in EXTRACTION_TYPES and e.start:
            for o in e.objects:
                if o not in first_extraction or e.start < first_extraction[o]:
                    first_extraction[o] = e.start
    commissioning: dict[str, date] = {}
    for e in events:
        if e.event_type is EventType.MINE_COMMISSIONING and e.start:
            for o in e.objects:
                commissioning[o] = min(e.start, commissioning.get(o, e.start))
    for e in events:
        if e.event_type in BACKFILL_TYPES and e.start:
            for o in e.objects:
                ext = [first_extraction[x] for x in lineage(o) if x in first_extraction]
                if ext and e.start < min(ext):
                    errs.append(f"event {e.id}: backfill of {o} starts {e.start} before its extraction {min(ext)}")
        if e.event_type in EXTRACTION_TYPES | BACKFILL_TYPES and e.start:
            for o in e.objects:
                m = mine_of.get(o)
                if m and m in commissioning and e.start < commissioning[m]:
                    errs.append(f"event {e.id}: {e.event_type.value} of {o} on {e.start} before commissioning of {m} "
                                f"({commissioning[m]})")
    ends: dict[str, date] = {}
    for e in events:
        if e.event_type is EventType.EXTRACTION_END and e.start:
            for o in e.objects:
                ends[o] = e.start
    for o, end in ends.items():
        if o in first_extraction and end < first_extraction[o]:
            errs.append(f"object {o}: extraction ends {end} before it starts {first_extraction[o]}")
    return errs


def known_at(events: list[Event], origin: date) -> list[Event]:
    """Information events usable by a forecaster at ``origin`` (availability must be known and <= origin)."""
    return [e for e in events if e.event_class is EventClass.INFORMATION and e.time.usable_at(origin) is True]

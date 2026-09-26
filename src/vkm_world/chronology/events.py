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
Dates as precise as the source gives them; imprecise dates carry ``precision`` and are compared as intervals
(a year means the whole year): an order error is reported only when it is certain, e.g. the LATEST possible
start of a backfill precedes the EARLIEST possible start of the extraction. For a mine commissioned on several
dated rows the most precise date is used, not the earliest (review finding CHRONOLOGY-014).

Two availability views (review finding CHRONOLOGY-023): ``known_at`` — information events usable at t0;
``known_physical_at`` — physical events a forecaster at t0 knows about: their own ``available_from`` (or that of an
information event listed in ``revealed_by``) is known and not later than t0. Unknown availability fails closed.
"""
from __future__ import annotations

from datetime import date
from enum import Enum

from pydantic import Field

from ..core.base import WorldObject, duplicate_ids
from ..core.provenance import TemporalSupport, date_bounds, finer


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
    revealed_by: tuple[str, ...] = Field((), description="ids of INFORMATION events that recorded this physical event "
                                                         "(survey, GIS snapshot, publication)")

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

    @property
    def bounds(self) -> tuple[date, date] | None:
        """(earliest start, latest end) honouring the date precision."""
        return self.time.event_bounds()


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
    # earliest possible start of the first extraction of each object
    first_extraction: dict[str, date] = {}
    for e in events:
        if e.event_type in EXTRACTION_TYPES and e.bounds:
            for o in e.objects:
                lo = e.bounds[0]
                if o not in first_extraction or lo < first_extraction[o]:
                    first_extraction[o] = lo
    # commissioning: the most precise dated row per mine (ties → earlier), compared by its earliest day
    commissioning: dict[str, tuple[date, str]] = {}
    for e in events:
        if e.event_type is EventType.MINE_COMMISSIONING and e.bounds:
            for o in e.objects:
                cur = commissioning.get(o)
                cand = (e.bounds[0], e.time.precision)
                if cur is None or finer(cand[1], cur[1]) or (cand[1] == cur[1] and cand[0] < cur[0]):
                    commissioning[o] = cand
    for e in events:
        if not e.bounds:
            continue
        s_lo, _ = e.bounds
        s_hi = date_bounds(e.start, e.time.precision)[1] if e.start else s_lo
        if e.event_type in BACKFILL_TYPES:
            for o in e.objects:
                ext = [first_extraction[x] for x in lineage(o) if x in first_extraction]
                if ext and s_hi < min(ext):
                    errs.append(f"event {e.id}: backfill of {o} starts {e.start} before its extraction {min(ext)}")
        if e.event_type in EXTRACTION_TYPES | BACKFILL_TYPES:
            for o in e.objects:
                m = mine_of.get(o)
                if m and m in commissioning and s_hi < commissioning[m][0]:
                    errs.append(f"event {e.id}: {e.event_type.value} of {o} on {e.start} before commissioning of {m} "
                                f"({commissioning[m][0]})")
    ends: dict[str, date] = {}
    for e in events:
        if e.event_type is EventType.EXTRACTION_END and e.bounds:
            for o in e.objects:
                ends[o] = e.bounds[1]          # latest possible end
    for o, end_hi in ends.items():
        if o in first_extraction and end_hi < first_extraction[o]:
            errs.append(f"object {o}: extraction ends {end_hi} before it starts {first_extraction[o]}")
    return errs


def known_at(events: list[Event], origin: date) -> list[Event]:
    """Information events usable by a forecaster at ``origin`` (availability must be known and <= origin)."""
    return [e for e in events if e.event_class is EventClass.INFORMATION and e.time.usable_at(origin) is True]


def known_physical_at(events: list[Event], origin: date) -> list[Event]:
    """Physical events a forecaster at ``origin`` knows about.

    A physical event is known when its own ``available_from`` is known and <= origin, or when one of the information
    events in ``revealed_by`` is known at origin. Physical events with unknown availability are NOT returned (fail
    closed): the world state at t0 is what was known at t0, not every event with event_date <= t0."""
    info_known = {e.id for e in known_at(events, origin)}
    return [e for e in events if e.event_class is EventClass.PHYSICAL
            and (e.time.usable_at(origin) is True or any(r in info_known for r in e.revealed_by))]

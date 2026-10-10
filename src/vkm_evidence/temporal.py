"""Conservative compatibility of evidence time and WorldSpec provenance time.

An empty default is absent metadata; no registration date is inferred. Explicit
conflicting dates remain in their original records and close known-at admission.
"""
from datetime import date, datetime, timezone
from typing import Literal

from pydantic import field_validator

from vkm_world.core.provenance import date_bounds
from vkm_evidence.contracts import Sha256, StrictModel, record_hash


class HistoricalReadContext(StrictModel):
    """Exact transaction revision plus separately stated information/record times.

    recorded_at is the record's asserted timestamp, not a reconstructed server
    commit time. Only the exact retained journal revision proves transaction order.
    """
    schema_version: Literal["vkm-evidence-history/1"] = "vkm-evidence-history/1"
    journal_revision: Sha256
    as_of: date
    recorded_at: datetime

    @field_validator("recorded_at")
    @classmethod
    def _aware(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("historical recorded_at must be timezone-aware")
        return value.astimezone(timezone.utc)

    @property
    def sha256(self):
        return record_hash(self)


def records_at(records, context: HistoricalReadContext):
    """Latest retained version satisfying both independent temporal cutoffs."""
    selected = {}
    for record in records:
        if record.recorded_at <= context.recorded_at and usable_at(record, context.as_of) is True:
            selected.pop(record.record_id, None)
            selected[record.record_id] = record
    return selected

DATE_FIELDS = ("event_date", "event_date_end", "measurement_date", "processing_date", "publication_date", "available_from")


def temporal_views(record):
    views = [record.time]
    if hasattr(record, "provenance"):
        views.append(record.provenance.temporal)
    quantity = getattr(record, "quantity", None)
    if quantity is not None:
        views.append(quantity.provenance.temporal)
    return views


def temporal_conflicts(record):
    views = temporal_views(record)
    conflicts = []
    for name in DATE_FIELDS:
        intervals = [date_bounds(getattr(t, name),
            (t.available_from_precision or t.precision) if name == "available_from" else t.precision)
            for t in views if getattr(t, name) is not None]
        # Date anchors are serialization, not exact-day assertions. A stated
        # year and a day within it are compatible. Never narrow availability
        # silently: available_latest still takes the most conservative bound.
        if intervals and max(a for a, _ in intervals) > min(b for _, b in intervals):
            conflicts.append("CONFLICTING_" + name.upper())
    availability = [t.available_latest() for t in views if t.available_from is not None]
    if availability:
        latest = max(availability)
        for t in views:
            for name in ("measurement_date", "processing_date"):
                value = getattr(t, name)
                if value is not None and latest < date_bounds(value, t.precision)[1]:
                    conflicts.append("AVAILABILITY_PRECEDES_" + name.upper())
    return sorted(set(conflicts))


def available_latest(record):
    if temporal_conflicts(record):
        return None
    stated = [t.available_latest() for t in temporal_views(record) if t.available_from is not None]
    return max(stated) if stated else None


def usable_at(record, origin):
    latest = available_latest(record)
    return latest <= origin if latest is not None else None


def unique_date(record, field):
    values = {getattr(t, field) for t in temporal_views(record) if getattr(t, field) is not None}
    return next(iter(values)) if len(values) == 1 else None

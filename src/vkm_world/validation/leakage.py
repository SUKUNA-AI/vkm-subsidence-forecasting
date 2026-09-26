"""Leakage guards for forecast validation (data-free, stdlib only).

A forecast issued at origin ``t0`` for a target epoch ``t1 > t0`` may use only information whose
availability is known and ``available_from <= t0`` (``TemporalSupport.usable_at``). The guards below
fail closed:

* estimator fields: raw identifiers, post-outcome/label metadata, ``true_/hidden_/generator_/private_``
  fields and names with ``future/next/target`` tokens are rejected (planned-at-origin exceptions only);
* time: every sample has ``origin < target`` and a positive horizon consistent with the dates;
* availability: unknown or future availability makes an input unusable;
* targets: the target is the planned epoch; a missed planned epoch is censored, never replaced by the
  next successful observation (that would select on the outcome);
* samples: train/validation/test id sets are disjoint;
* code: no ``train_test_split``/plain K-fold/``shuffle=True`` calls in model-facing sources.

This is distinct from ``vkm_world.governance.leakage`` (PRIVATE → PUBLIC repository guard).
"""
from __future__ import annotations

import ast
import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Collection, Iterable, Mapping, Sequence

from ..core.provenance import TemporalSupport


class LeakageViolation(ValueError):
    """A feature, availability, time-order or split rule is violated."""


# ---------------------------------------------------------------- estimator fields
FORBIDDEN_PREFIXES = ("true_", "hidden_", "generator_", "private_")
FUTURE_NAME_PATTERN = re.compile(r"(?:^|_)(?:future|next|target)(?:_|$)", re.IGNORECASE)
IDENTIFIER_FIELDS = frozenset({
    "sample_id", "point_id", "benchmark_id", "profile_id", "line_id", "borehole_id",
    "campaign_id", "scenario_id", "run_id",
})
POST_OUTCOME_FIELDS = frozenset({"label_status", "target_available", "missing_reason", "censoring_reason"})
# information fixed by the monitoring plan and known at the origin
PLANNED_INFORMATION_FIELDS = frozenset({"target_campaign_type", "forecast_horizon_days"})


def forbidden_field_reason(field: str, *, planned: Collection[str] = PLANNED_INFORMATION_FIELDS) -> str | None:
    """Why ``field`` must not be an estimator input, or None if the name is acceptable."""
    name = field.strip().lower()
    if name in IDENTIFIER_FIELDS or name.endswith("_campaign_id"):
        return "raw identifier (memorisation of points/campaigns)"
    if name in POST_OUTCOME_FIELDS:
        return "label / post-outcome metadata"
    if name.startswith(FORBIDDEN_PREFIXES):
        return "true/hidden/generator/private field"
    if FUTURE_NAME_PATTERN.search(name) and name not in {x.lower() for x in planned}:
        return "future/next/target information outside the planned-information exception"
    return None


def assert_feature_fields_safe(fields: Iterable[str], *, planned: Collection[str] = PLANNED_INFORMATION_FIELDS,
                               extra_forbidden: Collection[str] = ()) -> None:
    extra = {f.lower() for f in extra_forbidden}
    bad: dict[str, str] = {}
    for f in fields:
        if f.lower() in extra:
            bad[f] = "explicitly forbidden by the experiment contract"
        elif reason := forbidden_field_reason(f, planned=planned):
            bad[f] = reason
    if bad:
        raise LeakageViolation(f"forbidden estimator fields: {bad}")


# ---------------------------------------------------------------- time alignment
def as_date(value: date | datetime | str | None) -> date | None:
    """``date`` from a date/datetime/ISO string; None/''/NaN → None (unknown)."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    return datetime.fromisoformat(text).date() if text else None


def assert_positive_horizon(horizons_days: Iterable[float | int | None], name: str = "samples") -> None:
    bad = [i for i, h in enumerate(horizons_days)
           if h is None or not math.isfinite(float(h)) or float(h) <= 0]
    if bad:
        raise LeakageViolation(f"{name}: {len(bad)} missing or non-positive horizons (first rows {bad[:5]})")


def assert_time_alignment(origins: Sequence, targets: Sequence, horizons_days: Sequence | None = None,
                          *, name: str = "samples") -> None:
    """Every sample has known dates with ``origin < target``; stored horizons equal ``target - origin`` days."""
    if len(origins) != len(targets) or (horizons_days is not None and len(horizons_days) != len(origins)):
        raise LeakageViolation(f"{name}: origin/target/horizon sequences differ in length")
    bad_dates, bad_horizon = [], []
    for i, (o, t) in enumerate(zip(origins, targets)):
        o, t = as_date(o), as_date(t)
        if o is None or t is None or t <= o:
            bad_dates.append(i)
        elif horizons_days is not None and horizons_days[i] != (t - o).days:
            bad_horizon.append(i)
    if bad_dates or bad_horizon:
        raise LeakageViolation(f"{name}: time alignment failed: bad_dates={bad_dates[:5]} "
                               f"({len(bad_dates)}), horizon_mismatches={bad_horizon[:5]} ({len(bad_horizon)})")


# ---------------------------------------------------------------- availability at origin
def availability_reason(support: TemporalSupport | None, origin: date) -> str | None:
    """Why an input is unusable at ``origin`` (None if usable). Unknown availability is unusable."""
    origin = as_date(origin)
    if origin is None:
        return "origin date is unknown"
    if support is None:
        return "no temporal support"
    usable = support.usable_at(origin)
    if usable is None:
        return "availability unknown (available_from not set)"
    if not usable:
        return f"available only from {support.available_from}"
    return None


def assert_available_at(inputs: Mapping[str, TemporalSupport | None], origin: date) -> None:
    bad = {k: r for k, s in inputs.items() if (r := availability_reason(s, origin))}
    if bad:
        raise LeakageViolation(f"inputs unusable at origin {origin}: {bad}")


# ---------------------------------------------------------------- planned targets
@dataclass(frozen=True)
class PlannedTarget:
    campaign_id: str
    date: date
    observed: bool

    @property
    def censored(self) -> bool:
        return not self.observed


def _schedule(schedule: Iterable[tuple[str, date]]) -> list[tuple[date, str]]:
    items = [(as_date(d), str(c)) for c, d in schedule]
    ids = [c for _, c in items]
    dates = [d for d, _ in items]
    if None in dates:
        raise LeakageViolation("planned schedule contains an undated campaign")
    if len(set(ids)) != len(ids) or len(set(dates)) != len(dates):
        raise LeakageViolation("planned schedule has duplicate campaign ids or dates")
    return sorted(items)


def planned_target(origin: date, schedule: Iterable[tuple[str, date]], observed: Collection[str],
                   *, steps: int = 1) -> PlannedTarget | None:
    """The ``steps``-th planned epoch strictly after ``origin`` (None if the plan ends earlier).

    If that epoch was not observed (missed, rejected after QC) the target is censored; the next
    successful observation never takes its place.
    """
    if steps < 1:
        raise LeakageViolation("steps must be >= 1")
    origin = as_date(origin)
    if origin is None:
        raise LeakageViolation("origin date is unknown")
    future = [(d, c) for d, c in _schedule(schedule) if d > origin]
    if len(future) < steps:
        return None
    d, c = future[steps - 1]
    return PlannedTarget(campaign_id=c, date=d, observed=c in set(observed))


def assert_planned_target(origin: date, target_campaign_id: str, schedule: Iterable[tuple[str, date]],
                          *, steps: int = 1) -> None:
    expected = planned_target(origin, schedule, (), steps=steps)
    if expected is None or expected.campaign_id != target_campaign_id:
        raise LeakageViolation(f"target {target_campaign_id} is not the planned epoch {steps} after {origin} "
                               f"(expected {expected.campaign_id if expected else None})")


# ---------------------------------------------------------------- sample sets
def assert_disjoint_sample_sets(split_to_ids: Mapping[str, Iterable[str]]) -> None:
    sets = {name: set(map(str, ids)) for name, ids in split_to_ids.items()}
    names = list(sets)
    overlaps = [f"{a}<->{b}:{len(sets[a] & sets[b])}"
                for i, a in enumerate(names) for b in names[i + 1:] if sets[a] & sets[b]]
    if overlaps:
        raise LeakageViolation("split sample ids overlap: " + ", ".join(overlaps))


# ---------------------------------------------------------------- source scanner
FORBIDDEN_SPLIT_CALLS = frozenset({
    "train_test_split", "KFold", "StratifiedKFold", "RepeatedKFold", "RepeatedStratifiedKFold",
    "ShuffleSplit", "StratifiedShuffleSplit",
})


@dataclass(frozen=True)
class SplitApiFinding:
    path: str
    line: int
    api: str


def find_forbidden_split_api_usage(paths: Iterable[str | Path]) -> list[SplitApiFinding]:
    """Calls of row-random splitters and ``shuffle=True`` in Python sources (AST: strings/comments ignored).

    An unparseable file is reported (fail closed).
    """
    findings: list[SplitApiFinding] = []
    for p in map(Path, paths):
        if not p.is_file() or p.suffix != ".py":
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        except (SyntaxError, UnicodeDecodeError):
            findings.append(SplitApiFinding(p.as_posix(), 0, "unparseable"))
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
            if name in FORBIDDEN_SPLIT_CALLS:
                findings.append(SplitApiFinding(p.as_posix(), node.lineno, name))
            for kw in node.keywords:
                if kw.arg == "shuffle" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                    findings.append(SplitApiFinding(p.as_posix(), node.lineno, "shuffle=True"))
    return sorted(findings, key=lambda x: (x.path, x.line, x.api))


def assert_no_forbidden_split_api_usage(paths: Iterable[str | Path]) -> None:
    findings = find_forbidden_split_api_usage(paths)
    if findings:
        raise LeakageViolation(f"forbidden random-split API usage: {findings}")

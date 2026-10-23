"""Uncertainty budget kept per class: swings of quantities of interest, never one combined error (FW §5, AG §44).

The objects here only ever group records of ONE class. A world envelope and a representation error are two
different whiskers; there is deliberately no function that returns a total over classes
(``total_over_classes`` raises ``MixedClassesError``).

* ``Swing`` — one-at-a-time (OAT) effect of one element on one quantity of interest: the value at the low and high
  level relative to the central world, inside one representation.
* ``ClassBudget`` — the swings of one class for one qoi, ranked by the magnitude of the log-ratio, with the
  OAT envelope (min, max over single-element changes; NOT a combined error) and, optionally, a log-additive
  envelope that assumes no interactions (an explicit assumption; check it with ``interaction_index``).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .registry import UncertaintyRegistry
from .taxonomy import UncertaintyClass


class MixedClassesError(ValueError):
    """Raised when records of different uncertainty classes would be combined into one number."""


@dataclass(frozen=True)
class Swing:
    element_id: str
    uncertainty_class: UncertaintyClass
    qoi: str
    unit: str
    central: float
    low: float | None
    high: float | None
    time_yr: float | None = None
    label_low: str = ""
    label_high: str = ""
    representation_context: str = ""

    def ratios(self) -> tuple[float | None, float | None]:
        def r(v):
            return None if v is None or self.central == 0 else v / self.central
        return r(self.low), r(self.high)

    def magnitude(self) -> float:
        """max |ln(value / central)| over the two levels (0 when undefined)."""
        out = 0.0
        for q in self.ratios():
            if q is not None and q > 0:
                out = max(out, abs(math.log(q)))
        return out


def assert_single_class(records: Iterable[Swing]) -> UncertaintyClass:
    classes = {r.uncertainty_class for r in records}
    if len(classes) != 1:
        raise MixedClassesError(f"records of several classes cannot be combined: {sorted(c.value for c in classes)}")
    return classes.pop()


@dataclass
class ClassBudget:
    uncertainty_class: UncertaintyClass
    qoi: str
    unit: str
    central: float
    swings: list[Swing] = field(default_factory=list)
    representation_context: str = ""

    def ranked(self) -> list[Swing]:
        return sorted(self.swings, key=lambda s: s.magnitude(), reverse=True)

    def oat_envelope(self) -> tuple[float, float]:
        """min / max over single-element changes (each element moved alone; not a combined error)."""
        vals = [self.central] + [v for s in self.swings for v in (s.low, s.high) if v is not None]
        return min(vals), max(vals)

    def log_additive_envelope(self) -> tuple[float, float]:
        """ASSUMPTION: effects multiply independently (no interactions). Within ONE class only."""
        lo = hi = 0.0
        for s in self.swings:
            logs = [math.log(q) for q in s.ratios() if q is not None and q > 0]
            if logs:
                lo += min(0.0, min(logs))
                hi += max(0.0, max(logs))
        return self.central * math.exp(lo), self.central * math.exp(hi)


BudgetKey = tuple[UncertaintyClass, str, "float | None", str]


def build_class_budgets(swings: Sequence[Swing]) -> dict[BudgetKey, ClassBudget]:
    """Group swings by (class, qoi, time, representation_context); the central value must be the same inside a group.

    A world effect is meaningful only inside the representation it was computed in, so effects of different
    representation contexts are never merged into one ranking or envelope."""
    out: dict[BudgetKey, ClassBudget] = {}
    for s in swings:
        key = (s.uncertainty_class, s.qoi, s.time_yr, s.representation_context)
        b = out.get(key)
        if b is None:
            out[key] = ClassBudget(s.uncertainty_class, s.qoi, s.unit, s.central, [s], s.representation_context)
        else:
            if not math.isclose(b.central, s.central, rel_tol=1e-9, abs_tol=1e-12) or b.unit != s.unit:
                raise ValueError(f"{s.element_id}: central/unit differ inside {key}")
            b.swings.append(s)
    return out


def total_over_classes(*_args, **_kwargs):
    """There is no total: world uncertainty, representation error, method error, rules and observation are separate."""
    raise MixedClassesError("no total over uncertainty classes (FW §5, AG §44, MS §30): report each class separately")


def interaction_index(f00: float, f10: float, f01: float, f11: float) -> float:
    """ln(f11 f00 / (f10 f01)) for a 2x2 factorial: 0 = log-additive (no interaction)."""
    return math.log(f11 * f00 / (f10 * f01))


def swings_from_registry(reg: UncertaintyRegistry, qoi: str, time_yr: float | None = None) -> list[Swing]:
    """Swings stored in the registry (effects with a central value), one per measurement effect."""
    out = []
    for e in reg.elements:
        for ms in e.measurements:
            for ef in ms.effects:
                if ef.qoi != qoi or ef.central is None:
                    continue
                if time_yr is not None and (ef.time_yr is None or not math.isclose(ef.time_yr, time_yr)):
                    continue
                out.append(Swing(e.element_id, e.uncertainty_class, ef.qoi, ef.unit, ef.central, ef.low, ef.high,
                                 ef.time_yr, representation_context=ms.representation_context))
    return out

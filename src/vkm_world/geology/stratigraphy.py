"""Stratigraphic column of the ВКМ section (units, ranks, ordering). Metadata, not geometry."""
from __future__ import annotations

from enum import Enum

from pydantic import Field

from ..core.base import WorldObject, duplicate_ids
from ..core.provenance import Quantity


class UnitRank(str, Enum):
    COMPLEX = "complex"           # e.g. overburden (надсолевая толща), salt formation
    FORMATION = "formation"       # свита / толща: ТКТ, СМТ, ПКС, ПДКС, ...
    ZONE = "zone"                 # карналлитовая / сильвинитовая зона
    SEAM = "seam"                 # пласт: КрII, АБ, В, Г, Д, Е, ...
    INTERSEAM = "interseam"       # межпластье / междупластье
    MARKER = "marker"             # маркирующая глина, прослой
    INTERLAYER = "interlayer"     # глинистый/ангидритовый прослой
    PROTECTIVE = "protective"     # ВЗТ / водозащитная толща (functional unit, may span formations)
    OTHER = "other"


class StratigraphicUnit(WorldObject):
    abbrev: str | None = None
    rank: UnitRank
    parent_unit: str | None = None
    order_index: int | None = Field(None, description="top→bottom order within its parent (0 = uppermost)")
    lithology: str | None = None
    age: str | None = None
    functional: bool = Field(False, description="True for engineering units (ВЗТ) defined across formations")
    thickness_ranges: tuple[Quantity, ...] = Field((), description="as reported by sources, per site; never averaged here")
    aliases: tuple[str, ...] = ()


def column_errors(units: list[StratigraphicUnit]) -> list[str]:
    errs = [f"duplicate stratigraphic unit {d}" for d in duplicate_ids(units)]
    by = {u.id: u for u in units}
    for u in units:
        if u.parent_unit and u.parent_unit not in by:
            errs.append(f"{u.id}: parent unit '{u.parent_unit}' missing")
    groups: dict[str | None, list[StratigraphicUnit]] = {}
    for u in units:
        if not u.functional:
            groups.setdefault(u.parent_unit, []).append(u)
    for parent, us in groups.items():
        idx = [u.order_index for u in us if u.order_index is not None]
        if len(idx) != len(set(idx)):
            errs.append(f"duplicate order_index among children of {parent}")
    return errs


def ordered(units: list[StratigraphicUnit], parent: str | None = None) -> list[StratigraphicUnit]:
    return sorted([u for u in units if u.parent_unit == parent and u.order_index is not None],
                  key=lambda u: u.order_index)

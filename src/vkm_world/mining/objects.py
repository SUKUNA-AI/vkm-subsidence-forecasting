"""Mining objects (design and actual), as metadata with source-backed dimensions."""
from __future__ import annotations

from enum import Enum

from pydantic import Field

from ..core.base import WorldObject, duplicate_ids
from ..core.provenance import Quantity


class MiningObjectKind(str, Enum):
    SHAFT = "shaft"
    CAPITAL_WORKING = "capital_working"        # капитальные выработки
    DEVELOPMENT_WORKING = "development_working"  # подготовительные выработки
    PANEL = "panel"
    BLOCK = "block"
    CHAMBER = "chamber"
    PILLAR = "pillar"                           # междукамерный / ленточный целик
    BARRIER_PILLAR = "barrier_pillar"           # барьерный / охранный целик
    INTERMINE_PILLAR = "intermine_pillar"       # межрудничный целик
    PROTECTIVE_PILLAR = "protective_pillar"     # предохранительный целик под объектами
    MINED_AREA = "mined_area"                   # отработанное пространство (aggregate)
    BACKFILLED_VOLUME = "backfilled_volume"
    OTHER = "other"


class DesignOrActual(str, Enum):
    DESIGN = "DESIGN"           # проект / норматив
    ACTUAL = "ACTUAL"           # фактически отработано (маркшейдерская съёмка)
    TEACHING = "TEACHING"       # учебный пример — never a site fact
    UNKNOWN = "UNKNOWN"


class MiningObject(WorldObject):
    kind: MiningObjectKind
    design_or_actual: DesignOrActual = DesignOrActual.UNKNOWN
    mine: str | None = Field(None, description="spatial node id of the mine")
    parent_object: str | None = None
    spatial_node: str | None = None
    seams: tuple[str, ...] = Field((), description="stratigraphic unit ids mined / affected")
    mining_system: str | None = Field(None, description="e.g. камерная система с ленточными целиками, as printed")
    width: Quantity | None = None
    height: Quantity | None = None
    length: Quantity | None = None
    extraction_ratio: Quantity | None = None
    loading_degree: Quantity | None = None
    depth: Quantity | None = None
    geometry_ref: str | None = None


def mining_object_errors(objs: list[MiningObject], spatial_ids: set[str] | None = None,
                         unit_ids: set[str] | None = None) -> list[str]:
    errs = [f"duplicate mining object id {d}" for d in duplicate_ids(objs)]
    ids = {o.id for o in objs}
    for o in objs:
        if o.parent_object and o.parent_object not in ids:
            errs.append(f"{o.id}: parent object '{o.parent_object}' missing")
        if spatial_ids is not None:
            for ref in (o.mine, o.spatial_node):
                if ref and ref not in spatial_ids:
                    errs.append(f"{o.id}: spatial reference '{ref}' missing")
        if unit_ids is not None:
            for s in o.seams:
                if s not in unit_ids:
                    errs.append(f"{o.id}: seam '{s}' not in stratigraphic column")
        if o.design_or_actual is DesignOrActual.TEACHING and o.provenance.status.value == "FACT":
            errs.append(f"{o.id}: teaching example stored as FACT")
    return errs

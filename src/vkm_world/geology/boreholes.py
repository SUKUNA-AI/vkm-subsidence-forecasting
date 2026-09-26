"""Boreholes: ORIGINAL observations kept separate from interpretation.

* ``BoreholeRecord`` — identity, type, location as printed, provenance per source mention.
* ``PickObservation`` — a depth/elevation/thickness exactly as printed in one source (FACT or
  DERIVATION by the source author); several sources may report different picks for the same hole.
* ``PickInterpretation`` — a project decision reconciling observations (MODEL_CHOICE/DERIVATION);
  never overwrites observations.

No representative borehole is ever chosen here; ``usable_for_3d`` is a data-quality verdict only.
"""
from __future__ import annotations

from enum import Enum

from pydantic import Field, model_validator

from ..core.base import WorldObject, duplicate_ids
from ..core.provenance import Provenance, Quantity


class BoreholeType(str, Enum):
    EXPLORATION = "exploration"             # разведочная
    HYDROGEOLOGICAL = "hydrogeological"
    OIL = "oil"                             # нефтяная (Соликамская впадина)
    UNDERGROUND = "underground"             # подземная (из выработок)
    GEOTECHNICAL = "geotechnical"
    OBSERVATION = "observation"
    STRUCTURAL = "structural"
    UNKNOWN = "unknown"


class Usability3D(str, Enum):
    YES = "YES"             # located + picks
    PARTIAL = "PARTIAL"     # picks without location, or location without picks
    NO = "NO"
    UNKNOWN = "UNKNOWN"


class Location(WorldObject):
    """Collar location exactly as available; coordinates may be UNKNOWN (None) with a map reference."""

    crs_id: str | None = None
    x: Quantity | None = None
    y: Quantity | None = None
    collar_elevation: Quantity | None = None
    map_reference: str | None = Field(None, description="figure/map where the hole is plotted, if coordinates are not printed")
    description: str | None = None


class BoreholeRecord(WorldObject):
    borehole_type: BoreholeType = BoreholeType.UNKNOWN
    aliases: tuple[str, ...] = ()
    site_scope: str | None = None
    spatial_node: str | None = None
    location: Location | None = None
    total_depth: Quantity | None = None
    drilled: str | None = Field(None, description="date/period as printed")
    has_core: bool | None = None
    logging: tuple[str, ...] = Field((), description="log types reported: gamma, acoustic, Vp, resistivity, ...")
    lab_tests: tuple[str, ...] = ()
    usable_for_3d: Usability3D = Usability3D.UNKNOWN
    usability_reason: str | None = None
    open_questions: tuple[str, ...] = ()


class DepthReference(str, Enum):
    DEPTH_BELOW_COLLAR = "depth_below_collar"
    ABSOLUTE_ELEVATION = "absolute_elevation"
    THICKNESS_ONLY = "thickness_only"
    UNKNOWN = "unknown"


class PickObservation(WorldObject):
    borehole_id: str
    unit_id: str = Field(..., description="stratigraphic unit (id in the column) or 'UNRESOLVED:<as printed>'")
    unit_as_printed: str
    reference: DepthReference
    top: Quantity | None = None
    bottom: Quantity | None = None
    thickness: Quantity | None = None

    @model_validator(mode="after")
    def _geometry(self) -> "PickObservation":
        if self.top is None and self.bottom is None and self.thickness is None:
            raise ValueError(f"{self.id}: pick without top, bottom or thickness")
        if (self.reference is DepthReference.DEPTH_BELOW_COLLAR and self.top and self.bottom
                and self.top.value is not None and self.bottom.value is not None and self.top.value > self.bottom.value):
            raise ValueError(f"{self.id}: depth top {self.top.value} below bottom {self.bottom.value}")
        if (self.reference is DepthReference.ABSOLUTE_ELEVATION and self.top and self.bottom
                and self.top.value is not None and self.bottom.value is not None and self.top.value < self.bottom.value):
            raise ValueError(f"{self.id}: elevation top {self.top.value} below bottom {self.bottom.value}")
        return self


class PickInterpretation(WorldObject):
    borehole_id: str
    unit_id: str
    from_observations: tuple[str, ...]
    top: Quantity | None = None
    bottom: Quantity | None = None
    reference: DepthReference = DepthReference.UNKNOWN
    rationale: str

    @model_validator(mode="after")
    def _needs_obs(self) -> "PickInterpretation":
        if not self.from_observations:
            raise ValueError(f"{self.id}: interpretation must cite the observations it reconciles")
        return self


def borehole_errors(holes: list[BoreholeRecord], picks: list[PickObservation],
                    interps: list[PickInterpretation] = (), unit_ids: set[str] | None = None,
                    column_order: dict[str, int] | None = None) -> list[str]:
    errs = [f"duplicate borehole id {d}" for d in duplicate_ids(holes)]
    alias_owner: dict[str, str] = {}
    for h in holes:
        for a in (h.id, *h.aliases):
            if a in alias_owner and alias_owner[a] != h.id:
                errs.append(f"borehole alias '{a}' claimed by both {alias_owner[a]} and {h.id} (contradictory ids)")
            alias_owner.setdefault(a, h.id)
        if h.usable_for_3d is Usability3D.YES and (h.location is None or h.location.x is None or h.location.y is None):
            errs.append(f"{h.id}: usable_for_3d=YES but no located collar")
    ids = {h.id for h in holes}
    errs += [f"duplicate pick id {d}" for d in duplicate_ids(picks)]
    for p in picks:
        if p.borehole_id not in ids:
            errs.append(f"pick {p.id}: unknown borehole '{p.borehole_id}'")
        if unit_ids is not None and not p.unit_id.startswith("UNRESOLVED:") and p.unit_id not in unit_ids:
            errs.append(f"pick {p.id}: unknown stratigraphic unit '{p.unit_id}'")
    pick_ids = {p.id for p in picks}
    for i in interps:
        for o in i.from_observations:
            if o not in pick_ids:
                errs.append(f"interpretation {i.id}: unknown observation '{o}'")
    if column_order:
        errs += ordering_errors(picks, column_order)
    return errs


def ordering_errors(picks: list[PickObservation], column_order: dict[str, int]) -> list[str]:
    """Within one borehole/source/reference, deeper units (larger order) must not have shallower tops."""
    errs = []
    groups: dict[tuple, list[PickObservation]] = {}
    for p in picks:
        src = p.provenance.sources[0].source_id if p.provenance.sources else "?"
        groups.setdefault((p.borehole_id, src, p.reference), []).append(p)
    for (bh, src, ref), ps in groups.items():
        if ref not in (DepthReference.DEPTH_BELOW_COLLAR, DepthReference.ABSOLUTE_ELEVATION):
            continue
        known = [p for p in ps if p.unit_id in column_order and p.top is not None and p.top.value is not None]
        known.sort(key=lambda p: column_order[p.unit_id])
        for a, b in zip(known, known[1:]):
            deeper_is_shallower = (b.top.value < a.top.value) if ref is DepthReference.DEPTH_BELOW_COLLAR \
                else (b.top.value > a.top.value)
            if deeper_is_shallower:
                errs.append(f"{bh} ({src}): unit {b.unit_id} top {b.top.value} is above overlying unit "
                            f"{a.unit_id} top {a.top.value} — inverted order (check source/OCR, do not delete)")
    return errs


__all__ = ["BoreholeType", "Usability3D", "Location", "BoreholeRecord", "DepthReference", "PickObservation",
           "PickInterpretation", "borehole_errors", "ordering_errors", "Provenance"]

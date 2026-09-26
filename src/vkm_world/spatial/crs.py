"""Coordinate / height reference METADATA (no transformation engine in Phase 1).

What the sources use is often a local mine grid ("условная система рудника"), a state system
(СК-42 / СК-63 / МСК-59 zones), or unspecified map coordinates. Vertical: Балтийская система высот 1977,
absolute elevations ("абсолютные отметки") or depths below collar. Unknown offsets stay UNKNOWN.
"""
from __future__ import annotations

from enum import Enum

from pydantic import Field

from ..core.base import WorldObject


class CRSKind(str, Enum):
    STATE_GEODETIC = "STATE_GEODETIC"        # СК-42, СК-95, ГСК-2011 (geodetic lat/lon)
    STATE_PROJECTED = "STATE_PROJECTED"      # Gauss–Krüger zones of СК-42/СК-63, МСК-59
    LOCAL_MINE_GRID = "LOCAL_MINE_GRID"      # условная система рудника / шахтного поля
    MAP_ONLY = "MAP_ONLY"                    # coordinates readable only from a map sheet, system unstated
    PROJECT_ENGINEERING = "PROJECT_ENGINEERING"  # frame chosen by this project (MODEL_CHOICE)
    UNKNOWN = "UNKNOWN"


class VerticalKind(str, Enum):
    BALTIC_1977 = "BALTIC_1977"
    ABSOLUTE_UNSPECIFIED = "ABSOLUTE_UNSPECIFIED"   # "абс. отм." without system name
    DEPTH_BELOW_COLLAR = "DEPTH_BELOW_COLLAR"
    DEPTH_BELOW_SURFACE = "DEPTH_BELOW_SURFACE"
    RELATIVE_LOCAL = "RELATIVE_LOCAL"
    UNKNOWN = "UNKNOWN"


class AxisConvention(str, Enum):
    ENU = "x_east_y_north_z_up"
    GEODETIC_NE = "x_north_y_east"            # Russian geodetic convention (X north, Y east)
    UNKNOWN = "unknown"


class CoordinateSystem(WorldObject):
    kind: CRSKind
    epsg: int | None = None
    vertical: VerticalKind = VerticalKind.UNKNOWN
    axes: AxisConvention = AxisConvention.UNKNOWN
    units: str = "m"
    description: str | None = None
    used_by_sources: tuple[str, ...] = ()


class TransformKind(str, Enum):
    HELMERT_7 = "HELMERT_7"
    AFFINE_2D = "AFFINE_2D"
    SIMILARITY_2D = "SIMILARITY_2D"
    VERTICAL_OFFSET = "VERTICAL_OFFSET"
    GEOREFERENCED_MAP = "GEOREFERENCED_MAP"   # control points picked on a map
    UNKNOWN = "UNKNOWN"


class CoordinateTransform(WorldObject):
    """A KNOWN relation between two systems. Parameters stay None (UNKNOWN) unless source-backed."""

    from_crs: str
    to_crs: str
    kind: TransformKind = TransformKind.UNKNOWN
    parameters: dict[str, float] = Field(default_factory=dict)
    control_points: tuple[str, ...] = ()
    residual_rms_m: float | None = None


def crs_reference_errors(systems: list[CoordinateSystem], transforms: list[CoordinateTransform]) -> list[str]:
    ids = {s.id for s in systems}
    errs = []
    for t in transforms:
        for end in (t.from_crs, t.to_crs):
            if end not in ids:
                errs.append(f"transform {t.id}: unknown CRS '{end}'")
        if t.kind is not TransformKind.UNKNOWN and not t.parameters and not t.control_points:
            errs.append(f"transform {t.id}: kind {t.kind.value} without parameters or control points")
    for s in systems:
        if s.kind in (CRSKind.STATE_PROJECTED, CRSKind.STATE_GEODETIC) and s.epsg is None and not s.description:
            errs.append(f"CRS {s.id}: state system without EPSG code or description")
    return errs

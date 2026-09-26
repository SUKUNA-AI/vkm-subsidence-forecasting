"""Horizons (unit boundaries), contacts and structural features — representation METADATA only.

A horizon never "is" a surface by itself: it has one or more representations, each with its own
status. Phase 1 creates PICKS_ONLY / SOURCE_MAP representations; INTERPOLATED_SURFACE entries are
placeholders for the local computational phase (method + inputs must be declared when created).
"""
from __future__ import annotations

from enum import Enum

from pydantic import Field, model_validator

from ..core.base import WorldObject
from ..core.provenance import EpistemicStatus


class HorizonRepresentationKind(str, Enum):
    PICKS_ONLY = "PICKS_ONLY"                   # set of borehole pick observations
    SOURCE_MAP = "SOURCE_MAP"                   # contour/structure map published in a source (figure locator)
    SOURCE_SECTION = "SOURCE_SECTION"           # published cross-section
    INTERPOLATED_SURFACE = "INTERPOLATED_SURFACE"  # future: method + inputs + CV metrics (local phase)
    GPR_TRACED = "GPR_TRACED"                   # local reflector geometry from GPR near workings
    SEISMIC_TRACED = "SEISMIC_TRACED"


class HorizonRepresentation(WorldObject):
    kind: HorizonRepresentationKind
    inputs: tuple[str, ...] = Field((), description="pick ids / figure locators / profile ids")
    method: str | None = None
    extent: str | None = Field(None, description="spatial node id(s) covered")
    artifact_path: str | None = Field(None, description="future grid/mesh file; None in Phase 1")

    @model_validator(mode="after")
    def _status(self) -> "HorizonRepresentation":
        if self.kind is HorizonRepresentationKind.INTERPOLATED_SURFACE:
            if self.provenance.status is not EpistemicStatus.INTERPOLATION:
                raise ValueError(f"{self.id}: interpolated surface must have status INTERPOLATION")
            if not self.method or not self.inputs:
                raise ValueError(f"{self.id}: interpolated surface needs method and inputs")
        return self


class Horizon(WorldObject):
    upper_unit: str | None = None
    lower_unit: str | None = None
    representations: tuple[HorizonRepresentation, ...] = ()


class ContactKind(str, Enum):
    CONFORMABLE = "conformable"
    CLAY_INTERLAYER = "clay_interlayer"
    WEAK_CONTACT = "weak_contact"
    UNCONFORMITY = "unconformity"
    UNKNOWN = "unknown"


class Contact(WorldObject):
    horizon_id: str | None = None
    kind: ContactKind = ContactKind.UNKNOWN
    mechanical_relevance: str | None = None


class StructureKind(str, Enum):
    FOLD = "fold"
    FLEXURE = "flexure"
    FAULT = "fault"
    DISTURBED_ZONE = "disturbed_zone"
    GEODYNAMIC_ZONE = "geodynamic_zone"
    SALT_DISSOLUTION_ZONE = "salt_dissolution_zone"
    SUBSIDENCE_BASIN = "subsidence_basin"
    BOUDINAGE = "boudinage"
    WASHOUT = "washout"              # размыв / замещение
    ANOMALY = "anomaly"
    OTHER = "other"


class StructuralFeature(WorldObject):
    kind: StructureKind
    affected_units: tuple[str, ...] = ()
    location: str | None = Field(None, description="spatial node / map locator")
    orientation: str | None = Field(None, description="dip/strike/axis as printed")
    scale: str | None = None

"""Backfill records: technology, material and state of fill of mined voids (evidence metadata)."""
from __future__ import annotations

from enum import Enum

from pydantic import Field

from ..core.base import WorldObject
from ..core.provenance import Quantity


class BackfillMethod(str, Enum):
    HYDRAULIC = "hydraulic"            # гидравлическая закладка
    DRY = "dry"                        # сухая (самотёчная/механическая)
    PASTE = "paste"
    PNEUMATIC = "pneumatic"
    SELF_FILL_COLLAPSE = "self_fill"   # обрушение кровли / самозакладка
    NONE = "none"
    UNKNOWN = "unknown"


class BackfillRecord(WorldObject):
    target_objects: tuple[str, ...] = Field(..., description="mining objects (chambers/blocks/panels) filled")
    method: BackfillMethod = BackfillMethod.UNKNOWN
    material: str | None = Field(None, description="e.g. галитовые отходы, глинисто-солевые шламы — as printed")
    fill_ratio: Quantity | None = None               # доля заполнения пустоты
    delay_after_extraction: Quantity | None = None   # задержка закладки
    density: Quantity | None = None
    porosity: Quantity | None = None
    water_content: Quantity | None = None
    strength: Quantity | None = None
    deformation_modulus: Quantity | None = None
    compaction: str | None = None
    roof_contact: str | None = Field(None, description="контакт с кровлей / недозаклад, as printed")
    events: tuple[str, ...] = Field((), description="chronology event ids (BACKFILL_START/END/PERIOD)")
    convergence_relation: str | None = None

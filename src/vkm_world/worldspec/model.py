"""The WorldSpec document: 3D + time + provenance + uncertainty of the ВКМ / СКРУ-1 world.

It describes WHAT EXISTS and WHAT IS KNOWN (with status), not how a solver discretises it.
Solver representations (2D sections, local 3D models, meshes) are future DERIVED VIEWS with their own
MODEL_CHOICE list; they never overwrite the world.
"""
from __future__ import annotations

from datetime import date
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from ..chronology.events import Event, chronology_errors
from ..core.base import WorldObject, duplicate_ids
from ..core.provenance import Provenance, Scope
from ..geology.boreholes import BoreholeRecord, PickInterpretation, PickObservation, borehole_errors
from ..geology.horizons import Contact, Horizon, StructuralFeature
from ..geology.stratigraphy import StratigraphicUnit, column_errors
from ..materials.parameters import MaterialParameter
from ..mathmeta.models import MathModelRecord
from ..mining.backfill import BackfillRecord
from ..mining.objects import MiningObject, mining_object_errors
from ..observations.catalog import ObservationDataset, ObservationOperatorSpec, ObservationSystem
from ..physics.processes import ProcessDefinition
from ..spatial.crs import CoordinateSystem, CoordinateTransform, crs_reference_errors
from ..spatial.hierarchy import SpatialNode, hierarchy_errors

SCHEMA_VERSION = "worldspec-vnext/0.1"


class WorldStatus(str, Enum):
    DESIGN = "DESIGN"          # schema + example content (Phase 1)
    EVIDENCE_POPULATED = "EVIDENCE_POPULATED"
    DIAGNOSTIC = "DIAGNOSTIC"  # with interpolated representations (local phase)
    FROZEN = "FROZEN"


class WorldMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    world_id: str
    schema_version: str = SCHEMA_VERSION
    title: str
    status: WorldStatus = WorldStatus.DESIGN
    created: date | None = None
    evidence_snapshot: str | None = Field(None, description="PRIVATE resources commit the evidence was taken from")
    target_scope: Scope = Scope.SKRU1
    description: str | None = None


class UnknownItem(WorldObject):
    """An explicit gap. UNKNOWN is a first-class citizen of the world."""

    what: str
    why_it_matters: str
    blocks: tuple[str, ...] = Field((), description="process / object ids blocked by this gap")
    resolution_path: str | None = None


class WorldSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meta: WorldMeta
    coordinate_systems: list[CoordinateSystem] = Field(default_factory=list)
    transforms: list[CoordinateTransform] = Field(default_factory=list)
    spatial_nodes: list[SpatialNode] = Field(default_factory=list)
    stratigraphy: list[StratigraphicUnit] = Field(default_factory=list)
    boreholes: list[BoreholeRecord] = Field(default_factory=list)
    picks: list[PickObservation] = Field(default_factory=list)
    pick_interpretations: list[PickInterpretation] = Field(default_factory=list)
    horizons: list[Horizon] = Field(default_factory=list)
    contacts: list[Contact] = Field(default_factory=list)
    structures: list[StructuralFeature] = Field(default_factory=list)
    mining_objects: list[MiningObject] = Field(default_factory=list)
    backfill: list[BackfillRecord] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    materials: list[MaterialParameter] = Field(default_factory=list)
    processes: list[ProcessDefinition] = Field(default_factory=list)
    math_models: list[MathModelRecord] = Field(default_factory=list)
    observation_systems: list[ObservationSystem] = Field(default_factory=list)
    observation_datasets: list[ObservationDataset] = Field(default_factory=list)
    observation_operators: list[ObservationOperatorSpec] = Field(default_factory=list)
    unknowns: list[UnknownItem] = Field(default_factory=list)

    # ------------------------------------------------------------------ validation
    def all_object_ids(self) -> set[str]:
        ids: set[str] = set()
        for coll in (self.coordinate_systems, self.transforms, self.spatial_nodes, self.stratigraphy, self.boreholes,
                     self.picks, self.pick_interpretations, self.horizons, self.contacts, self.structures,
                     self.mining_objects, self.backfill, self.events, self.materials, self.processes,
                     self.observation_systems, self.observation_datasets, self.observation_operators, self.unknowns):
            ids |= {o.id for o in coll}
        return ids

    def _provenances(self):
        for coll in (self.coordinate_systems, self.transforms, self.spatial_nodes, self.stratigraphy, self.boreholes,
                     self.picks, self.pick_interpretations, self.horizons, self.contacts, self.structures,
                     self.mining_objects, self.backfill, self.events, self.materials, self.processes,
                     self.observation_systems, self.observation_datasets, self.observation_operators, self.unknowns):
            for o in coll:
                yield o.id, o.provenance

    def validate_world(self, registered_sources: set[str] | None = None) -> list[str]:
        e: list[str] = []
        spatial_ids = {n.id for n in self.spatial_nodes}
        unit_ids = {u.id for u in self.stratigraphy}
        mining_ids = {m.id for m in self.mining_objects}
        order = {u.id: i for i, u in enumerate(sorted([u for u in self.stratigraphy if u.order_index is not None
                                                        and u.rank.value in ("formation", "zone", "seam", "interseam", "marker", "interlayer")],
                                                       key=lambda u: (u.order_index,)))}
        e += crs_reference_errors(self.coordinate_systems, self.transforms)
        e += hierarchy_errors(self.spatial_nodes)
        e += column_errors(self.stratigraphy)
        e += borehole_errors(self.boreholes, self.picks, self.pick_interpretations, unit_ids or None, None)
        e += mining_object_errors(self.mining_objects, spatial_ids or None, unit_ids or None)
        parent_of = {m.id: m.parent_object for m in self.mining_objects}
        mine_of = {m.id: m.mine for m in self.mining_objects}
        known = self.all_object_ids()
        e += chronology_errors(self.events, known, parent_of, mine_of)
        for b in self.backfill:
            for t in b.target_objects:
                if t not in mining_ids:
                    e.append(f"backfill {b.id}: unknown target object '{t}'")
            for ev in b.events:
                if ev not in {x.id for x in self.events}:
                    e.append(f"backfill {b.id}: unknown event '{ev}'")
        for h in self.horizons:
            for u in (h.upper_unit, h.lower_unit):
                if u and u not in unit_ids:
                    e.append(f"horizon {h.id}: unknown unit '{u}'")
        for m in self.materials:
            if m.unit_id and m.unit_id not in unit_ids:
                e.append(f"material {m.id}: unknown unit '{m.unit_id}'")
        sys_ids = {s.id for s in self.observation_systems}
        for d in self.observation_datasets:
            if d.system_id not in sys_ids:
                e.append(f"dataset {d.id}: unknown observation system '{d.system_id}'")
            for n in d.spatial_nodes:
                if n not in spatial_ids:
                    e.append(f"dataset {d.id}: unknown spatial node '{n}'")
        model_ids = {m.model_id for m in self.math_models}
        for p in self.processes:
            for mid in p.math_models:
                if model_ids and mid not in model_ids:
                    e.append(f"process {p.id}: unknown math model '{mid}'")
        for op in self.observation_operators:
            for mid in op.math_models:
                if model_ids and mid not in model_ids:
                    e.append(f"operator {op.id}: unknown math model '{mid}'")
        e += [f"duplicate math model id {d}" for d in {m.model_id for m in self.math_models
                                                      if [x.model_id for x in self.math_models].count(m.model_id) > 1}]
        for oid, prov in self._provenances():
            ent = prov.spatial.entity_id
            if ent and ent not in spatial_ids:
                e.append(f"{oid}: spatial support entity '{ent}' not in hierarchy")
            if registered_sources is not None:
                for s in prov.sources:
                    if s.source_id.startswith("VKM-SRC-") and s.source_id not in registered_sources:
                        e.append(f"{oid}: source '{s.source_id}' is not registered")
        all_ids = []
        for coll in (self.spatial_nodes, self.boreholes, self.mining_objects, self.events):
            all_ids += [o.id for o in coll]
        return e


def empty_world(world_id: str, title: str) -> WorldSpec:
    return WorldSpec(meta=WorldMeta(world_id=world_id, title=title))


__all__ = ["WorldSpec", "WorldMeta", "WorldStatus", "UnknownItem", "empty_world", "SCHEMA_VERSION", "Provenance",
           "duplicate_ids"]

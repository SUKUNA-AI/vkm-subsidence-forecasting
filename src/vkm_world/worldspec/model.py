"""The WorldSpec document: 3D + time + provenance + uncertainty of the ВКМ / СКРУ-1 world.

It describes WHAT EXISTS and WHAT IS KNOWN (with status), not how a solver discretises it.
Solver representations (2D sections, local 3D models, meshes) are future DERIVED VIEWS with their own
MODEL_CHOICE list; they never overwrite the world.
"""
from __future__ import annotations

from datetime import date
from enum import Enum
from collections import Counter

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..chronology.events import Event, EventClass, EventType, chronology_errors
from ..core.base import SOURCE_ID_RE, WorldObject, duplicate_ids
from ..core.provenance import Provenance, Scope, SourceRef
from ..geology.boreholes import BoreholeRecord, PickInterpretation, PickObservation, borehole_errors
from ..geology.horizons import Contact, Horizon, HorizonRepresentationKind, StructuralFeature
from ..geology.stratigraphy import StratigraphicUnit, column_errors
from ..materials.parameters import MaterialParameter
from ..mathmeta.models import MathModelRecord
from ..mining.backfill import BackfillRecord
from ..mining.objects import MiningObject, NomenclatureCrosswalk, mining_object_errors
from ..observations.catalog import ObservationDataset, ObservationOperatorSpec, ObservationSystem
from ..physics.processes import ProcessDefinition
from ..spatial.crs import CoordinateSystem, CoordinateTransform, crs_reference_errors
from ..spatial.hierarchy import SpatialNode, hierarchy_errors

SCHEMA_VERSION = "worldspec-vnext/0.1"


def _nested_models(value, path="world"):
    """Visit typed records, including locations, quantities and horizon representations."""
    if isinstance(value, BaseModel):
        yield path, value
        for name in type(value).model_fields:
            yield from _nested_models(getattr(value, name), f"{path}.{name}")
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _nested_models(child, f"{path}.{key}")
    elif isinstance(value, (tuple, list)):
        for index, child in enumerate(value):
            yield from _nested_models(child, f"{path}[{index}]")


def _metadata_ids(value):
    """Catalogue metadata uses exact semicolon-separated identifiers, not inferred aliases."""
    if isinstance(value, str):
        return [item.strip() for item in value.split(";") if item.strip()]
    return list(value or ())


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
    crosswalks: list[NomenclatureCrosswalk] = Field(default_factory=list)
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
        """WorldObject ids; mathematical metadata has a separate model_id namespace."""
        return {obj.id for _, obj in _nested_models(self) if isinstance(obj, WorldObject)}

    def _provenances(self):
        for path, obj in _nested_models(self):
            if isinstance(obj, Provenance):
                yield path, obj

    def revalidated(self) -> "WorldSpec":
        """Validate current contents, including nested mutations, without freezing authoring models."""
        return type(self).model_validate(self.model_dump(mode="python", round_trip=True))

    def validate_world(self, registered_sources: set[str] | None = None, *,
                       registered_evidence: set[str] | None = None,
                       registered_laws: set[str] | None = None) -> list[str]:
        """Structural errors in current contents, independent of scientific-use admission.

        Omitted collections are valid in DESIGN. Explicit object/model/law references must resolve.
        Source/evidence closure is checked when the caller supplies the corresponding authoritative index.
        A law-catalogue id resolves only by exact membership; it is never converted to a math model id.
        """
        try:
            fresh = self.revalidated()
        except ValidationError as exc:
            return [f"{'.'.join(map(str, error['loc'])) or 'world'}: {error['msg']}"
                    for error in exc.errors(include_url=False)]
        return fresh._reference_errors(registered_sources, registered_evidence, registered_laws)

    def _reference_errors(self, registered_sources, registered_evidence, registered_laws) -> list[str]:
        e: list[str] = []
        models = list(_nested_models(self))
        counts = Counter(obj.id for _, obj in models if isinstance(obj, WorldObject))
        e += [f"duplicate WorldObject id {oid}" for oid, count in sorted(counts.items()) if count > 1]
        spatial_ids = {n.id for n in self.spatial_nodes}
        unit_ids = {u.id for u in self.stratigraphy}
        mining_ids = {m.id for m in self.mining_objects}
        order = {u.id: i for i, u in enumerate(sorted([u for u in self.stratigraphy if u.order_index is not None
                                                        and u.rank.value in ("formation", "zone", "seam", "interseam", "marker", "interlayer")],
                                                       key=lambda u: (u.order_index,)))}
        e += crs_reference_errors(self.coordinate_systems, self.transforms)
        e += hierarchy_errors(self.spatial_nodes)
        e += column_errors(self.stratigraphy)
        # order_index is local to a parent; the flattened order above is not a safe geological order.
        e += borehole_errors(self.boreholes, self.picks, self.pick_interpretations, unit_ids, None)
        e += mining_object_errors(self.mining_objects, spatial_ids, unit_ids)
        parent_of = {m.id: m.parent_object for m in self.mining_objects}
        mine_of = {m.id: m.mine for m in self.mining_objects}
        known = self.all_object_ids()
        e += chronology_errors(self.events, known, parent_of, mine_of)
        event_by_id = {event.id: event for event in self.events}
        for event in self.events:
            for ref in event.revealed_by:
                if ref not in event_by_id:
                    e.append(f"event {event.id}: unknown information event '{ref}' in revealed_by")
                elif event_by_id[ref].event_class is not EventClass.INFORMATION:
                    e.append(f"event {event.id}: revealed_by '{ref}' is not an INFORMATION event")
        crs_ids = {s.id for s in self.coordinate_systems}
        for hole in self.boreholes:
            if hole.spatial_node and hole.spatial_node not in spatial_ids:
                e.append(f"borehole {hole.id}: unknown spatial node '{hole.spatial_node}'")
            if hole.location and hole.location.crs_id and hole.location.crs_id not in crs_ids:
                e.append(f"location {hole.location.id}: unknown CRS '{hole.location.crs_id}'")
        for interp in self.pick_interpretations:
            if interp.borehole_id not in {h.id for h in self.boreholes}:
                e.append(f"interpretation {interp.id}: unknown borehole '{interp.borehole_id}'")
            if not interp.unit_id.startswith("UNRESOLVED:") and interp.unit_id not in unit_ids:
                e.append(f"interpretation {interp.id}: unknown stratigraphic unit '{interp.unit_id}'")
        for b in self.backfill:
            for t in b.target_objects:
                if t not in mining_ids:
                    e.append(f"backfill {b.id}: unknown target object '{t}'")
            for ev in b.events:
                if ev not in event_by_id:
                    e.append(f"backfill {b.id}: unknown event '{ev}'")
                elif event_by_id[ev].event_type not in (EventType.BACKFILL_START, EventType.BACKFILL_END,
                                                        EventType.BACKFILL_PERIOD):
                    e.append(f"backfill {b.id}: event '{ev}' is not BACKFILL_START/END/PERIOD")
        for h in self.horizons:
            for u in (h.upper_unit, h.lower_unit):
                if u and u not in unit_ids:
                    e.append(f"horizon {h.id}: unknown unit '{u}'")
            for rep in h.representations:
                if rep.kind is HorizonRepresentationKind.PICKS_ONLY:
                    for ref in rep.inputs:
                        if ref not in {pick.id for pick in self.picks}:
                            e.append(f"representation {rep.id}: unknown pick input '{ref}'")
                # SOURCE_MAP/SECTION inputs are source locators, not WorldObject ids.
        for contact in self.contacts:
            if contact.horizon_id and contact.horizon_id not in {h.id for h in self.horizons}:
                e.append(f"contact {contact.id}: unknown horizon '{contact.horizon_id}'")
        for structure in self.structures:
            for unit in structure.affected_units:
                if unit not in unit_ids:
                    e.append(f"structure {structure.id}: unknown unit '{unit}'")
        model_ids = {m.model_id for m in self.math_models}
        law_ids = model_ids | set(registered_laws or ())
        for m in self.materials:
            if m.unit_id and m.unit_id not in unit_ids:
                e.append(f"material {m.id}: unknown unit '{m.unit_id}'")
            if m.law_id and m.law_id not in law_ids:
                e.append(f"material {m.id}: unknown law '{m.law_id}'")
        sys_ids = {s.id for s in self.observation_systems}
        for system in self.observation_systems:
            if system.crs_id and system.crs_id not in crs_ids:
                e.append(f"observation system {system.id}: unknown CRS '{system.crs_id}'")
        for d in self.observation_datasets:
            if d.system_id not in sys_ids:
                e.append(f"dataset {d.id}: unknown observation system '{d.system_id}'")
            for n in d.spatial_nodes:
                if n not in spatial_ids:
                    e.append(f"dataset {d.id}: unknown spatial node '{n}'")
        for p in self.processes:
            for mid in p.math_models:
                if mid not in model_ids:
                    e.append(f"process {p.id}: unknown math model '{mid}'")
            for ref in p.evidence:
                if SOURCE_ID_RE.fullmatch(ref):
                    if registered_sources is not None and ref not in registered_sources:
                        e.append(f"process {p.id}: source '{ref}' is not registered")
                elif registered_evidence is not None and ref not in registered_evidence:
                    e.append(f"process {p.id}: evidence '{ref}' is not registered")
        for op in self.observation_operators:
            for mid in op.math_models:
                if mid not in model_ids:
                    e.append(f"operator {op.id}: unknown math model '{mid}'")
            for ref in op.world_inputs:
                if ref not in known:
                    e.append(f"operator {op.id}: unknown world input '{ref}'")
        model_counts = Counter(m.model_id for m in self.math_models)
        e += [f"duplicate math model id {mid}" for mid, count in sorted(model_counts.items()) if count > 1]
        for oid, prov in self._provenances():
            ent = prov.spatial.entity_id
            if ent and ent not in spatial_ids:
                e.append(f"{oid}: spatial support entity '{ent}' not in hierarchy")
            for ref in prov.inputs:
                if ref not in known | model_ids:
                    e.append(f"{oid}: unknown provenance input '{ref}'")
        for path, obj in models:
            if isinstance(obj, SourceRef):
                if registered_sources is not None and obj.source_id not in registered_sources:
                    e.append(f"{path}: source '{obj.source_id}' is not registered")
                if registered_evidence is not None:
                    e += [f"{path}: evidence '{ref}' is not registered" for ref in obj.evidence_ids
                          if ref not in registered_evidence]
            elif isinstance(obj, MathModelRecord):
                if registered_sources is not None:
                    e += [f"math model {obj.model_id}: source '{ref}' is not registered"
                          for ref in _metadata_ids(obj.source_ids) if ref not in registered_sources]
                if registered_evidence is not None:
                    e += [f"math model {obj.model_id}: evidence '{ref}' is not registered"
                          for ref in _metadata_ids(getattr(obj, "vn_ids", ())) if ref not in registered_evidence]
        if registered_sources is not None:
            for system in self.coordinate_systems:
                e += [f"CRS {system.id}: source '{ref}' is not registered" for ref in system.used_by_sources
                      if ref not in registered_sources]
        for node in self.spatial_nodes:
            for ref in node.also_within:
                if ref not in spatial_ids:
                    e.append(f"{node.id}: also_within '{ref}' does not exist")
        for unknown in self.unknowns:
            for ref in unknown.blocks:
                if ref not in known:
                    e.append(f"unknown {unknown.id}: unknown blocked object '{ref}'")
        return e


def empty_world(world_id: str, title: str) -> WorldSpec:
    return WorldSpec(meta=WorldMeta(world_id=world_id, title=title))


__all__ = ["WorldSpec", "WorldMeta", "WorldStatus", "UnknownItem", "empty_world", "SCHEMA_VERSION", "Provenance",
           "duplicate_ids"]

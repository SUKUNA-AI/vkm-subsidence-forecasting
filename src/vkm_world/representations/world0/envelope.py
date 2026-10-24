"""Microkernel instance envelope K_i = {G_i, Θ_i, x_i, m_i, P_i, B_i, U_i} (MS §6, §41; CT §18–21).

An instance belongs to a WorldRepresentation (``representation_id`` is mandatory, CT §18) and maps to physical
entities through typed relations (REPRESENTS / REPRESENTS_PART_OF / AGGREGATES). Its state and memory must match the
active physics families of the kernel type exactly: no extra variables, no missing ones (MS §27, §46). Changing the
state layout requires a new ``state_schema_version`` (MS §43).
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .catalogue import KERNEL_TYPES, RELATIONS


class EntityRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    entity_id: str
    relation: str = Field(..., description="REPRESENTS | REPRESENTS_PART_OF | AGGREGATES")

    @model_validator(mode="after")
    def _rel(self) -> "EntityRef":
        if self.relation not in RELATIONS:
            raise ValueError(f"relation must be one of {RELATIONS}")
        return self


class KernelEvent(BaseModel):
    """Local event history B_i (mining, backfilling, flooding, ...), times in seconds of the world clock."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    t: float
    event: str
    note: str | None = None


class Microkernel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    kernel_id: str
    representation_id: str
    type_id: str
    physical_entity_refs: tuple[EntityRef, ...] = Field(..., min_length=1)
    geometry_ref: str
    state_schema_version: str
    active_families: tuple[str, ...] = ("ELASTIC",)
    state: dict[str, float]
    memory: dict[str, float] = Field(default_factory=dict)
    parameter_set_ref: str
    active_ports: tuple[str, ...] = ()
    history: tuple[KernelEvent, ...] = ()
    uncertainty_ref: str | None = None
    provenance: str = Field(..., description="how the instance was built (partition rule, inputs)")
    task: str
    horizon_s: float
    tolerance: float

    @model_validator(mode="after")
    def _schema(self) -> "Microkernel":
        kt = KERNEL_TYPES.get(self.type_id)
        if kt is None:
            raise ValueError(f"unknown kernel type {self.type_id}")
        bad = [f for f in self.active_families if f not in kt.families]
        if bad:
            raise ValueError(f"families {bad} are not available for {self.type_id}")
        want_state = {v.symbol for v in kt.active_variables(self.active_families, "STATE")}
        want_mem = {v.symbol for v in kt.active_variables(self.active_families, "MEMORY")}
        if set(self.state) != want_state:
            raise ValueError(f"state {sorted(self.state)} ≠ active state {sorted(want_state)} of {self.type_id}")
        if set(self.memory) != want_mem:
            raise ValueError(f"memory {sorted(self.memory)} ≠ active memory {sorted(want_mem)} of {self.type_id}")
        derived = {v.symbol for v in kt.variables if v.kind == "DERIVED"}
        if derived & (set(self.state) | set(self.memory)):
            raise ValueError("derived quantities are computed, never stored as state (FW §9)")
        bounds = {v.symbol: v.bounds for v in kt.variables}
        for name, val in {**self.state, **self.memory}.items():
            lo, hi = bounds[name]
            if (lo is not None and val < lo) or (hi is not None and val > hi):
                raise ValueError(f"{name} = {val} outside admissible bounds {bounds[name]} (MS §40)")
        port_names = {p.name for p in kt.ports}
        unknown = [p for p in self.active_ports if p not in port_names]
        if unknown:
            raise ValueError(f"ports {unknown} are not defined for {self.type_id}")
        if self.horizon_s <= 0 or self.tolerance <= 0:
            raise ValueError("task horizon and tolerance must be positive (MS §4)")
        times = [e.t for e in self.history]
        if times != sorted(times):
            raise ValueError("event history must be ordered in time")
        return self

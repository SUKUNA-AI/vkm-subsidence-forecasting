"""Typed physical ports of a microkernel (theory v0.2: AG §7–8, MS §27).

A port is an interface of the representation, not a new physical law (AG §8). Each port kind has a pair of
conjugate variables whose product is the power exchanged through the port:

* MECHANICAL — displacement u [m] and traction t = σ·n [Pa]; power density t·u̇ [W/m²];
* HYDRAULIC  — pressure p [Pa] and normal Darcy flux q_n [m/s]; power density p·q_n [W/m²];
* THERMAL    — temperature T [K] and normal heat flux q_T [W/m²] (entropy flux q_T/T);
* CONTACT    — normal opening δ_n, slip δ_s [m], contact traction p_c [Pa], hydraulic aperture a [m].

Sign convention used throughout World-0: compression and convergence are positive (mining-mechanics
convention); vertical displacement is positive downward (subsidence).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PortKind(str, Enum):
    MECHANICAL = "MECHANICAL"
    HYDRAULIC = "HYDRAULIC"
    THERMAL = "THERMAL"
    CONTACT = "CONTACT"


# (symbol, SI unit, meaning); the first item is the "effort"-like, the second the "flow"-like variable.
PORT_VARIABLES: dict[PortKind, tuple[tuple[str, str, str], ...]] = {
    PortKind.MECHANICAL: (("u", "m", "mean normal displacement of the port face"),
                          ("t", "Pa", "mean normal traction t = σ·n on the port face (compression +)")),
    PortKind.HYDRAULIC: (("p", "Pa", "fluid (pore) pressure"),
                         ("q_n", "m/s", "normal Darcy flux through the port face")),
    PortKind.THERMAL: (("T", "K", "temperature"),
                       ("q_T", "W/m2", "normal heat flux")),
    PortKind.CONTACT: (("delta_n", "m", "normal opening (gap ≥ 0)"),
                       ("delta_s", "m", "tangential slip"),
                       ("p_c", "Pa", "contact normal traction (compression +)"),
                       ("a", "m", "hydraulic aperture")),
}


@dataclass(frozen=True)
class Port:
    """One typed port of a kernel type: kind + the face it lives on + whether the hypothesis activates it."""

    name: str
    kind: PortKind
    face: str
    active: bool = True
    note: str = ""

    @property
    def variables(self) -> tuple[str, ...]:
        return tuple(v[0] for v in PORT_VARIABLES[self.kind])


def port_power_density(kind: PortKind, effort: float, flow_rate: float) -> float:
    """Power density exchanged through a port [W/m²].

    MECHANICAL: t·u̇ (traction × displacement rate); HYDRAULIC: p·q_n; THERMAL: the heat flux itself
    (q_T is already a power density; ``effort`` is ignored). CONTACT has no single conjugate pair
    (normal and tangential parts are separate) and raises.
    """
    if kind is PortKind.MECHANICAL or kind is PortKind.HYDRAULIC:
        return float(effort) * float(flow_rate)
    if kind is PortKind.THERMAL:
        return float(flow_rate)
    raise ValueError("CONTACT power is split into normal (p_c·δ̇_n) and tangential parts; use them explicitly")

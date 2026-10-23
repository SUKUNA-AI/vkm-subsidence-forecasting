"""Shared set-up of the World-0 identification runs (23.10.2026): the representative column and material scenarios.

Column: v6_2 slice (geometry_version 887a7f33…), chainage 1459 m = centre of the conditional 600 m panel of the
solver_next_stage runs; thicknesses are a DERIVATION of the v6_2 INTERPOLATION (not observations). KrII roof depth
321.3 m; KrII thickness 5.5 m and «roof = SZ top − 10 m» are MODEL_CHOICE of that stage.

Materials: step-3 table of the solver_next_stage runs (PRIVATE 12_work_exports/geology_misis_20261023/solver_next_stage/
step3_materials.json): modulus scenarios M10/M05/M03 = 1.0/0.5/0.3 × LAB means (VKM-SRC-025 табл. 2.2) and NORM
(«Указания», cited by VKM-SRC-012 табл. 1.1); ν, ρ normative; Q — ENGINEERING_ASSUMPTION; ТКТ/СМТ — normative «мергель»
transferred by assumption. Norton viscosities: Transfer TR-CREEP-ERMASHOV2015-SKRU1-SLICE-V6_2 (VKM-SRC-045, n = 5):
FIELD sylvinite 7.58e7, salt 3.33e8, carnallite 3.70e7 MPa·yr; LAB sylvinite 1.85e7, salt 8.21e7 MPa·yr.
All values are scenario inputs of a PhysicalWorld hypothesis, not SKRU-1 facts.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from vkm_world.representations.world0.mapdl_inputs import Layer, Material  # noqa: E402

COLUMN_M = [  # bottom → top: (name, thickness m)
    ("PDKS", 315.0), ("PDKS_TOP", 20.0), ("SZ_BELOW", 6.1), ("KRII", 5.5), ("SZ_ABOVE", 10.0), ("SKZ", 75.3),
    ("PKS", 31.3), ("SMT", 90.3), ("TKT", 99.6), ("Q", 14.8),
]
ROWS = {"PDKS": 12, "PDKS_TOP": 6, "SZ_BELOW": 3, "KRII": 3, "SZ_ABOVE": 5, "SKZ": 8, "PKS": 4, "SMT": 8, "TKT": 9,
        "Q": 2}

E_SCEN = {  # Pa: (M10, M05, M03, NORM, C044)
    "Q": (0.1e9, 0.1e9, 0.1e9, 0.1e9, 0.1e9),
    "TKT": (0.6e9, 0.6e9, 0.6e9, 0.6e9, 0.7e9),
    "SMT": (0.6e9, 0.6e9, 0.6e9, 0.6e9, 0.6e9),
    "PKS": (8.05e9, 4.025e9, 2.415e9, 1.5e9, 1.5e9),
    "SKZ": (3.14e9, 1.57e9, 0.942e9, 0.5e9, 0.6e9),
    "SYLV": (13.79e9, 6.895e9, 4.137e9, 0.8e9, 0.9e9),
    "PDKS": (12.96e9, 6.48e9, 3.888e9, 1.5e9, 1.5e9),
}
# C044: one compatible parameter set (CT §45) — the retrospective SKRU-1 model of profile line V, VKM-SRC-044 табл. 28
# p.177 (CALIBRATED_EFFECTIVE_MODEL of that source, not measurements); СКЗ = 0.6 GPa / ν 0.4 is a MODEL_CHOICE between
# its carnallite (0.5–0.7) and interseam-salt (0.6–1.2) rows.
SCEN_INDEX = {"M10": 0, "M05": 1, "M03": 2, "NORM": 3, "C044": 4}
NU = {"Q": 0.3, "TKT": 0.3, "SMT": 0.3, "PKS": 0.3, "SKZ": 0.4, "SYLV": 0.2, "PDKS": 0.3}
NU_C044 = {"Q": 0.4, "TKT": 0.3, "SMT": 0.3, "PKS": 0.3, "SKZ": 0.4, "SYLV": 0.3, "PDKS": 0.3}
RHO = {"Q": 1900.0, "TKT": 2242.6, "SMT": 2242.6, "PKS": 2242.6, "SKZ": 2038.7, "SYLV": 2140.7, "KRII": 2090.0,
       "PDKS": 2242.6}
ETA = {"FIELD": {"SYLV": 7.58e7, "PDKS": 3.33e8, "PKS": 3.33e8, "SKZ": 3.70e7},
       "LAB": {"SYLV": 1.85e7, "PDKS": 8.21e7, "PKS": 8.21e7, "SKZ": 3.70e7}}
LAYER_ROCK = {"PDKS": "PDKS", "PDKS_TOP": "PDKS", "SZ_BELOW": "SYLV", "KRII": "SYLV", "SZ_ABOVE": "SYLV", "SKZ": "SKZ",
              "PKS": "PKS", "SMT": "SMT", "TKT": "TKT", "Q": "Q"}


def material(rock: str, scen: str = "M05", creep: str | None = None, name: str | None = None,
             E_override: float | None = None) -> Material:
    E = E_override if E_override is not None else E_SCEN[rock][SCEN_INDEX[scen]]
    eta = ETA[creep].get(rock) if creep else None
    rho = RHO.get(name or rock, RHO.get(rock, 2200.0))
    nu = NU_C044[rock] if scen == "C044" else NU[rock]
    return Material(name or rock, E, nu, rho, eta)


def column_layers(scen: str = "M05", q_E: float | None = None) -> list[Layer]:
    out = []
    for name, h in COLUMN_M:
        rock = LAYER_ROCK[name]
        mat = material(rock, scen, name=name, E_override=q_E if name == "Q" else None)
        out.append(Layer(name, h, mat, ROWS[name]))
    return out


def sigma_v_at(name_top_of: str = "KRII") -> float:
    """Lithostatic σ_v at the top of the given layer (role BODY_FORCE), g = 9.80665."""
    s = 0.0
    above = False
    for name, h in COLUMN_M:
        if above:
            s += RHO.get(name, RHO[LAYER_ROCK[name]]) * 9.80665 * h
        if name == name_top_of:
            above = True
    return s

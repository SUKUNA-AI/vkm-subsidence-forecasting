"""Design of the GPU ensemble: elements of world uncertainty, their ranges with status/source, Saltelli sampling.

Every range below is either taken from an evidence record of the project (with its locator) or is an explicitly
marked scenario range (UNKNOWN -> SCENARIO / MODEL_CHOICE). Distributions inside the ranges are MODEL_CHOICE
(uniform or log-uniform) — the ensemble explores the ranges, it is not a probability statement about SKRU-1.

Sobol' indices by groups (Saltelli 2010 / Jansen estimators): one group = one element of uncertainty; the AB_i
matrices swap a whole group, so runs = N·(g + 2). With g = 8 groups and N = 1024: 10 240 runs.
"""
from __future__ import annotations

import math
from dataclasses import asdict

import numpy as np

from vkm_world.representations.gpu_plane_strain import MeshSpec

OUT_TIMES = [0.0, 1.0, 2.0, 5.0, 10.0, 15.0, 20.0, 25.0, 30.0, 35.0, 40.0]

MESH_SPECS = {
    # rows: Q, TKT, SMT, PKS, SKZ, SZ above KrII, KrII upper, KrII lower, SZ below, PdKS
    "seamA2": MeshSpec(rows=(2, 6, 5, 4, 6, 8, 3, 3, 6, 10), chamber_elems=8, pillar_h=1.3, outer_h0=1.3,
                       outer_growth=1.2, outer_hmax=20.0),
    "seamC": MeshSpec(rows=(2, 10, 8, 6, 10, 8, 3, 3, 6, 18), chamber_elems=8, pillar_h=1.3, outer_h0=1.3,
                      outer_growth=1.2, outer_hmax=20.0),
    "ref_like": MeshSpec(rows=(2, 10, 8, 6, 10, 8, 3, 3, 6, 18), chamber_elems=8, pillar_h=1.3, outer_h0=1.3,
                         outer_growth=1.15, outer_hmax=5.0),
}


def spec_dict(spec: MeshSpec) -> dict:
    return asdict(spec)


# ------------------------------------------------------------------------------------------- elements (groups)
# each parameter: (name, kind, lo, hi, status / source)
GROUPS = [
    ("moduli", [("E_factor", "uniform", 0.3, 1.0,
                 "MODEL_CHOICE scenario E_massif = LAB × f, f in [0.3, 1.0] (step 3: M03…M10; LAB ≠ MASSIF; "
                 "LAB means VKM-SRC-025 табл. 2.2) — applied to all LAB-based zones (ПКС…ПдКС, КрII)")]),
    ("creep_viscosity", [
        ("eta_sylv", "loguniform", 1.848e7, 7.69e7,
         "TR-CREEP-ERMASHOV2015: sylvinite ηs LAB 1.85e7 (VKM-SRC-045 табл. 2.3.2) … FIELD 7.58e7 (табл. 3.2, целики "
         "СКРУ-1 12 ЗП) / FIELD_N 7.69e7 (табл. 4.2.2); MPa·yr, n = 5 fixed (FACT of the source)"),
        ("eta_salt", "loguniform", 8.21e7, 4.0e8,
         "rock salt ηs LAB 8.21e7 (табл. 2.3.2) … FIELD 3.33e8 (табл. 4.3.2, ANALOGUE СКРУ-2) / FIELD_N 4.0e8"),
        ("eta_carn", "loguniform", 3.70e7, 7.69e7,
         "carnallite ηs FIELD 3.70e7 (табл. 4.3.2, ANALOGUE СКРУ-2) … FIELD_N 7.69e7; LAB UNKNOWN in the source"),
    ]),
    ("extraction", [("L_axis", "uniform", 20.0, 27.0,
                     "axis spacing of chambers 20…27 m (VKM-SRC-096 p0005 табл. 2, СКРУ-1 опытный участок): "
                     "k = 10.2/L = 0.378…0.51; chamber width 10.2 m fixed (2 passes × 5.1 m)")]),
    ("backfill", [
        ("fill_u", "uniform", 0.0, 1.0,
         "presence: UNKNOWN for the place (calendar of backfill at SKRU-1 not established) -> u < 0.5 no backfill "
         "(MODEL_CHOICE 50/50); u >= 0.5 backfill of the lower part of each chamber, filled fraction "
         "0.6 + 0.6·(u − 0.5) ∈ [0.6, 0.9] — scenario range around the degree of filling of KrII chambers "
         "A = 0.75…0.85 (VKM-SRC-044 p0180, VKM regional, AB 0.63…0.70); in 2D the fill occupies the lower part of "
         "the chamber, placed right after excavation, stress-free, bonded to floor and pillar walls, no roof contact"),
        ("E_fill", "loguniform", 7.5e7, 5.0e8,
         "deformation modulus of the backfill mass 75…500 MPa: values used in the models of VKM-SRC-266 p0146 "
         "(E(10 yr) = 75 MPa, 200, 300, 500 MPa; MODEL values of the source, VKM regional) -> scenario range; "
         "elastic, ν = 0.3 (MODEL_CHOICE), own weight not applied"),
    ]),
    ("seam_depth", [("d_roof", "uniform", 7.5, 16.0,
                     "KrII roof below the SZ contact 7.5…16 m (scenario of A_corpus seam_scenarios; FACT 7.5/12.8 m "
                     "only at SKRU-2 wells 130/139)")]),
    ("seam_thickness", [("h_seam", "uniform", 5.10, 6.30,
                         "KrII thickness 5.10…6.30 m (СКРУ-1, VKM-SRC-252 табл. 1.2); chamber height = seam thickness")]),
    ("geology", [("geo_u", "uniform", 0.0, 1.0,
                  "conditional realizations of the v6_2 geology ensemble (variant G, 50 realizations; session "
                  "«ансамбль неопределённости геологии v6_2 на GPU»): index = floor(50·u); synthetic DERIVATION")]),
    ("initial_stress", [("lam_u", "uniform", 0.0, 1.0,
                         "hypotheses λ = 1 (u < 0.5) / λ = ν/(1−ν) (u ≥ 0.5); initial field of СКРУ-1 not measured "
                         "(UNKNOWN; MR-CONF-009 0.45…1.5). With creep, K0 also drives background creep of the intact "
                         "slice; the effect is taken as the difference to the reference run")]),
]

N_REAL = 50


def param_list():
    return [p for _, ps in GROUPS for p in ps]


def transform(U: np.ndarray) -> dict:
    """U (n, 11) in [0, 1) -> physical parameters for ensemble_core.run_batch."""
    out = {}
    for j, (name, kind, lo, hi, _) in enumerate(param_list()):
        u = U[:, j]
        if kind == "uniform":
            out[name] = lo + (hi - lo) * u
        else:
            out[name] = np.exp(math.log(lo) + (math.log(hi) - math.log(lo)) * u)
    fill_on = out["fill_u"] >= 0.5
    out["fill_on"] = fill_on.astype(int)
    out["fill_frac"] = np.where(fill_on, 0.6 + 0.6 * (out["fill_u"] - 0.5), 0.5)
    out["geo"] = np.minimum((out["geo_u"] * N_REAL).astype(int), N_REAL - 1)
    out["k0"] = (out["lam_u"] >= 0.5).astype(int)
    out["norm"] = np.zeros(len(U), int)
    return out


def saltelli(N: int, seed: int):
    """Saltelli design by groups: returns (U (N·(g+2), d), labels) with blocks A, B, AB_1..AB_g."""
    from scipy.stats import qmc
    d = len(param_list())
    g = len(GROUPS)
    base = qmc.Sobol(d=2 * d, scramble=True, seed=seed).random(N)
    A, Bm = base[:, :d], base[:, d:]
    blocks = [A, Bm]
    labels = [("A", -1)] * N + [("B", -1)] * N
    col = 0
    for gi, (gname, ps) in enumerate(GROUPS):
        AB = A.copy()
        AB[:, col:col + len(ps)] = Bm[:, col:col + len(ps)]
        col += len(ps)
        blocks.append(AB)
        labels += [("AB", gi)] * N
    return np.vstack(blocks), labels


def n_chambers(L_axis: np.ndarray, a: float = 10.2, panel: float = 600.0) -> np.ndarray:
    return np.floor((panel + (L_axis - a)) / L_axis + 1e-9).astype(int)

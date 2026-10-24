"""Analytic kernel checks and pair interactions of World-0 (no solver needed):

H1  hereditary (Abel-kernel) pillar memory → Prony modes: memory dimension vs tolerance (MS §14, W0-L-04a);
A1  aquifer kernel (ТКТ) consolidation: closure test 1 mode vs N modes (MS §29) and drawdown settlement scale;
V1  VZT ↔ aquifer: series conductivity, controlling layer, leakage for intact / fractured sublayers;
T1  thermal: Arrhenius factor of creep for the temperature uncertainty (prescribed field, thermal port inactive);
G1  gravity, role BODY_FORCE: lithostatic σ_v at the seam; role POTENTIAL_FIELD: Δg of chamber, panel, backfill,
    void closure and the free-air term of subsidence (observation operator only).
All parameter values are labelled scenarios with their evidence rows (PRIVATE WORLD0_MICROKERNELS); none is a site fact.
Usage: python scripts/world0/analytic_pairs.py --out work/world0_20261023/analysis
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import identification_setup as setup  # noqa: E402

from vkm_world.representations.world0 import gravity as grav  # noqa: E402
from vkm_world.representations.world0 import laws  # noqa: E402
from vkm_world.representations.world0.closure import state_closure_test  # noqa: E402
from vkm_world.representations.world0.kernels import AquiferKernel, VZTKernel  # noqa: E402

DAY = 86400.0


def h1_hereditary() -> dict:
    """Abel kernel α = 0.7, δ_c = 0.18 … 0.35 h^−0.3 (ML-09: SKRU-1 KrII pillars, VKM-SRC-047, natural estimate)."""
    out = {}
    for delta in (0.18, 0.35):
        res = {}
        for label, t0 in (("from_1h", 1.0), ("from_1month", 730.0)):
            t = np.geomspace(t0, 40 * 8766.0, 400)          # hours
            J = laws.abel_creep_compliance(t, 1.0, delta, 0.7)        # E·J(t)
            modes = {}
            for N in (1, 2, 3, 4, 6, 8, 10, 12):
                taus = np.geomspace(t0 / 10, 40 * 8766.0 * 10, N) if N > 1 else np.array([8766.0])
                w = laws.prony_fit_compliance(t, J, taus, 1.0)
                modes[N] = float(np.max(np.abs(laws.prony_compliance(t, 1.0, w, taus) - J) / J))
            res[label] = {"EJ_end": float(J[-1]), "max_rel_err_by_modes": modes,
                          "min_modes_1pct": next((n for n, e in modes.items() if e <= 0.01), None),
                          "min_modes_5pct": next((n for n, e in modes.items() if e <= 0.05), None)}
        res["EJ_1yr"] = float(laws.abel_creep_compliance(8766.0, 1.0, delta, 0.7))
        out[f"delta_{delta}"] = res
    out["note"] = ("δ in h^(α−1) as printed for the SKRU-1 natural estimate (ML-09); time unit of δ conflicts between "
                   "sources (C-04) — kept as printed; the modes are memory state variables of the pillar kernel, not "
                   "material constants (W0-L-04a)")
    return out


def a1_aquifer() -> dict:
    """ТКТ thickness 99.6 m (v6_2 column), drained at the top, impermeable base (MODEL_CHOICE)."""
    Hd = 99.6
    E, nu = 0.6e9, 0.3                                 # NORMATIVE «мергель» transferred (step-3 table)
    M = laws.oedometric_modulus(E, nu)
    out = {"M_oed_Pa": M}
    # conductivity: «единицы–сотни м/сут» (MP-0121, VKM_REGIONAL FIELD) → scenario 1…100 m/d; S_s = 1/(γ_w⁻¹M) +fluid
    S_s = 1000 * 9.80665 * (1 / M + 0.45e-9 * 0.1)     # specific storage: skeleton + water (porosity 0.1, MODEL_CHOICE)
    out["S_s_per_m"] = S_s
    for K_md in (1.0, 100.0):
        K = K_md / DAY
        cv = laws.consolidation_coefficient(K, S_s)
        out[f"K_{K_md:g}md"] = {"c_v_m2_s": cv, "t90_days": float(0.848 * Hd ** 2 / cv / DAY)}
    # closure: a uniform excess-pressure profile and a first-mode profile with the SAME mean pressure
    zeta = np.linspace(0, 1, 2001)
    k1 = AquiferKernel(Hd, 1.0, n_modes=1)
    kN = AquiferKernel(Hd, 1.0, n_modes=60)
    p_uni = np.ones_like(zeta)
    p_mode = np.sin(math.pi / 2 * zeta)
    p_mode *= np.trapezoid(p_uni, zeta) / np.trapezoid(p_mode, zeta)       # same mean
    Tv = np.array([0.01, 0.05, 0.1, 0.2, 0.5, 1.0])
    t = Tv * Hd ** 2 / 1.0

    def future(profile):
        return kN.mean_pressure(kN.evolve(kN.project(zeta, profile), t))

    r1 = state_closure_test([p_uni, p_mode], reduce=lambda p: [np.trapezoid(p, zeta)], future_response=future,
                            tolerance=0.02, reduced_atol=1e-6)
    rN = state_closure_test([p_uni, p_uni.copy()], reduce=lambda p: kN.project(zeta, p), future_response=future,
                            tolerance=0.02)
    out["closure_mean_pressure_only"] = {"verdict": r1.verdict, "spread": r1.spread, "Tv": Tv.tolist()}
    out["closure_60_modes"] = {"verdict": rN.verdict, "spread": rN.spread}
    exact = laws.terzaghi_degree(Tv)
    one = 1 - (8 / math.pi ** 2) * np.exp(-math.pi ** 2 * Tv / 4)
    out["U_exact_vs_1mode"] = {"Tv": Tv.tolist(), "exact": exact.tolist(), "one_mode": one.tolist()}
    # drawdown settlement scale (only if the VZT is breached and the aquifer drains): Δp = 1 MPa (≈100 m head)
    out["settlement_per_MPa_drawdown_m"] = float(Hd / M * 1e6)
    return out


def v1_vzt() -> dict:
    """Two compositions of the barrier (MODEL_CHOICE, both kept):
    KRII_ONLY  — blocks where КрII is the upper mined seam: СЗ 10 | СКЗ 75.3 | ПКС 31.3 | СМТ 90.3 m (v6_2 column);
    SRC252_V   — SKRU-1 plan 2013 (VKM-SRC-252, RA-039/040): ВЗТ 100–110 m above the roof of seam В →
                 СКЗ above В 58.7 | ПКС 31.3 | ПП / lower СМТ 15 m (split of the 105 m is a MODEL_CHOICE)."""
    comps = {"KRII_ONLY": (("SZ_ABOVE", "SKZ", "PKS", "SMT"), (10.0, 75.3, 31.3, 90.3)),
             "SRC252_V": (("SKZ_ABOVE_V", "PKS", "PP_SMT_LOW"), (58.7, 31.3, 15.0))}
    # salt: LAB permeability ~1e-5 mD under isotropic loading, «×десятки» at a 1 MPa deviator (MP-0127, VKM_REGIONAL,
    # cited) → K = kρg/μ with water (MODEL_CHOICE); the «≤ 10⁻⁸ м/сут» row MP-0125 is UNSUPPORTED (re-check B6)
    k_iso = 1e-5 * 9.869233e-16                             # m²
    salt_K = {"iso": laws.permeability_to_conductivity(k_iso), "deviatoric_x100": 100 * laws.permeability_to_conductivity(k_iso)}
    smt_K = 0.1 / DAY                       # «доли–21 м/сут» (MP-0123) → scenario 0.1 m/d (lower part)
    frac = 1.0 / DAY                        # fractured conductivity scenario 1 m/d (MODEL_CHOICE)
    head = 250.0                            # head difference ТКТ → workings, scenario (rassol horizon head 120–150 m)
    cases = {}
    for cname, (names, h) in comps.items():
        n = len(h)
        Kf = (frac,) * n
        salt_idx = [i for i, nm in enumerate(names) if not nm.startswith(("SMT", "PP"))]
        for sk, Ks in salt_K.items():
            Ki = tuple(Ks if i in salt_idx else smt_K for i in range(n))
            scen = {"intact": [0] * n, "marl_fractured": [0 if i in salt_idx else 1 for i in range(n)],
                    "salt_except_PKS": [0 if names[i] == "PKS" else 1 for i in range(n)], "all_fractured": [1] * n}
            for label, d in scen.items():
                k = VZTKernel(h, Ki, Kf, list(d))
                q = k.leakage(head)
                cases[f"{cname}:{sk}:{label}"] = {"total_m": float(sum(h)), "K_salt_m_per_day": Ks * DAY,
                                                  "K_eff_m_per_day": k.K_eff() * DAY,
                                                  "q_m3_per_day_per_km2": q * DAY * 1e6,
                                                  "controlling_layer": names[k.controlling_layer()]}
    return {"compositions": {k: dict(zip(*v)) for k, v in comps.items()}, "head_drop_m": head, "cases": cases,
            "note": "salt K from MP-0127 (LAB, cited) for isotropic and ×100 deviatoric loading; SMT, fractured K and "
                    "head are labelled scenarios; SKRU-1 values UNKNOWN (HYD-146)"}


def t1_thermal() -> dict:
    # rock temperature: 7–9 °C «калийные рудники Верхнекамья» (RA-002, SRC-050) vs +9…+10 °C salt at BKPRU-3 (MP-0129)
    # — conflict CF-RA-02 kept; SKRU-1 UNKNOWN → T_ref = 9 °C with ±2 K covering both
    T_ref = 273.15 + 9.0
    out = {"T_ref_K": T_ref, "T_range_C": [7.0, 10.0]}
    for Q in (24.5e3, 42e3, 54e3):            # VKM-SRC-193, NON_VKM (ML-04, ML-05): Transfer required
        out[f"Q_{Q / 1e3:g}kJ"] = {f"dT_{dT:+g}K": laws.arrhenius_factor(T_ref + dT, T_ref, Q) for dT in (-2, -1, 1, 2)}
    kappa = 6.65 / (2242.6 * 854.0)         # λ 6.65 W/(m·K) (RA-007, NBS), c 854 J/(kg·K) (RA-004), ρ normative salt —
    #                                          GENERAL values found in the corpus (re-check A4), not VKM measurements
    out["diffusivity_m2_s"] = kappa
    out["conduction_time_5m_yr"] = laws.diffusion_time(5.0, kappa) / laws.YEAR_S
    out["conduction_time_100m_yr"] = laws.diffusion_time(100.0, kappa) / laws.YEAR_S
    return out


def g1_gravity() -> dict:
    out = {}
    # role BODY_FORCE: σ_v at the КрII roof from the column (W0-L-16)
    names = [n for n, _ in setup.COLUMN_M][::-1]
    th = [h for _, h in setup.COLUMN_M][::-1]
    rho = [setup.RHO.get(n, setup.RHO[setup.LAYER_ROCK[n]]) for n in names]
    depth, sv = grav.lithostatic_profile(th, rho)
    i_roof = names.index("SZ_ABOVE")
    out["BODY_FORCE"] = {"sigma_v_KrII_roof_MPa": float(sv[i_roof] / 1e6), "depth_KrII_roof_m": float(depth[i_roof]),
                         "consistent_with_identification_setup": abs(sv[i_roof] - setup.sigma_v_at("KRII")) < 1.0}
    # role POTENTIAL_FIELD (W0-L-17): probe only
    z_roof = float(depth[i_roof])
    rho_ore = 2090.0
    x = np.linspace(-1500, 1500, 601)
    ch = [(-5.1, z_roof), (5.1, z_roof), (5.1, z_roof + 5.5), (-5.1, z_roof + 5.5)]
    g_ch = grav.talwani_gz(x, 0.0, ch, -rho_ore)
    out["single_chamber_2D_uGal"] = float(g_ch.min() / grav.UGAL)
    a, wc, n = 27.0, 10.2, 22
    centres = -n * a / 2 + a / 2 + a * np.arange(n)
    g_panel = sum(grav.talwani_gz(x, 0.0, [(c - wc / 2, z_roof), (c + wc / 2, z_roof), (c + wc / 2, z_roof + 5.5),
                                           (c - wc / 2, z_roof + 5.5)], -rho_ore) for c in centres)
    out["panel_A27_2D_open_uGal"] = float(g_panel.min() / grav.UGAL)
    # 3D: same panel, chambers 600 m long (MODEL_CHOICE)
    g3 = sum(grav.prism_gz(np.array([0.0]), 0.0, 0.0, (c - wc / 2, c + wc / 2, -300, 300, z_roof, z_roof + 5.5),
                           -rho_ore) for c in centres)
    out["panel_A27_3D_600m_open_uGal"] = float(g3[0] / grav.UGAL)
    # backfilled to A = 0.8 with mechanical fill 1.375 t/m³ (MP-0091, MP-0094): fill Δρ = 1375 − 2090, void above
    hf = 0.8 * 5.5
    g_bf = sum(grav.talwani_gz(np.array([0.0]), 0.0, [(c - wc / 2, z_roof + 5.5 - hf), (c + wc / 2, z_roof + 5.5 - hf),
                                                      (c + wc / 2, z_roof + 5.5), (c - wc / 2, z_roof + 5.5)],
                               1375.0 - rho_ore) +
               grav.talwani_gz(np.array([0.0]), 0.0, [(c - wc / 2, z_roof), (c + wc / 2, z_roof),
                                                      (c + wc / 2, z_roof + 5.5 - hf), (c - wc / 2, z_roof + 5.5 - hf)],
                               -rho_ore) for c in centres)
    out["panel_A27_2D_backfilled_A08_uGal"] = float(g_bf[0] / grav.UGAL)
    # time-lapse terms per metre of subsidence (infinite-panel limit): free air + closure of the void at depth
    out["free_air_uGal_per_mm"] = float(grav.free_air_change(1e-3) / grav.UGAL)
    out["void_closure_slab_uGal_per_mm"] = float(grav.bouguer_slab_gz(rho_ore, 1e-3) / grav.UGAL)
    out["note"] = ("probe only: Δg never enters the mechanics; field gravimetry in the corpus exists only for other "
                   "VKM sites (ML-27: SKRU-2 2014 sinkhole −0.1 mGal; Berezniki −0.05…−0.10 mGal)")
    np.savez(Path(OUT) / "gravity_profiles.npz", x=x, g_chamber=g_ch, g_panel=g_panel)
    return out


OUT = "."


def main():
    global OUT
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    OUT = str(a.out)
    res = {"H1_hereditary": h1_hereditary(), "A1_aquifer": a1_aquifer(), "V1_vzt": v1_vzt(),
           "T1_thermal": t1_thermal(), "G1_gravity": g1_gravity()}
    (a.out / "analytic_pairs.json").write_text(json.dumps(res, indent=1, ensure_ascii=False, default=float),
                                               encoding="utf-8")
    print(json.dumps(res, indent=1, ensure_ascii=False, default=float))


if __name__ == "__main__":
    main()

"""Post-processing of the compliance runs: K = C⁻¹ sign structure (G5, RC-T1), G = S·C⁻¹, Knothe comparator,
aggregation of cells (cell width dependence), volume transfer, fine-vs-coarse equivalence of the mined cell (AG §31).

Usage: python scripts/world0/analyze_influence.py --root work/world0_20261023/compliance --out work/world0_20261023/analysis
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

from vkm_world.representations.world0 import influence as I  # noqa: E402
from vkm_world.representations.world0 import laws  # noqa: E402
from vkm_world.representations.world0.closure import external_equivalence  # noqa: E402


def aggregate(C: np.ndarray, S: np.ndarray, m: int):
    """Merge m adjacent cells: unit traction on a coarse cell = on its m fine cells; convergence averaged."""
    n = C.shape[0] // m
    P = np.zeros((C.shape[0], n))
    for J in range(n):
        P[J * m:(J + 1) * m, J] = 1.0
    return P.T @ C @ P / m, S @ P


def summarize(case: str, d) -> dict:
    C, S, x = d["C"], d["S"], d["x"]
    axisym = bool(d["axisym"])
    depth = float(d["depth_roof"])
    edges = d["cell_edges"]
    a = float(edges[1] - edges[0])
    out = {"case": case, "axisym": axisym, "cell_width_m": a, "n_cells": int(C.shape[0]), "depth_roof_m": depth}
    if not axisym:
        K, asym = I.stiffness_from_compliance(C)
        out["betti_asymmetry"] = asym
    if not axisym:
        ss = I.sign_structure(K)
        out["K"] = {k: v for k, v in ss.items() if k != "lag_profile"}
        out["K"]["lag_profile_0_8"] = [float(v) for v in ss["lag_profile"][:9]]
        out["K_diag_Pa_per_m_mid"] = float(K[len(K) // 2, len(K) // 2])
        agg = {}
        for m in (2, 3, 5, 10):
            if C.shape[0] % m == 0 or C.shape[0] // m >= 3:
                nn = (C.shape[0] // m) * m
                Cc, Sc = aggregate(C[:nn, :nn], S[:, :nn], m)
                Kc, _ = I.stiffness_from_compliance(Cc)
                sc = I.sign_structure(Kc)
                agg[f"{a * m:.0f}m"] = {"z_matrix": sc["z_matrix"], "first_positive_lag": sc["first_positive_lag"],
                                        "lag_profile_0_4": [float(v) for v in sc["lag_profile"][:5]],
                                        "max_positive_offdiag_rel": sc["max_positive_offdiag_rel"]}
        out["K_aggregated"] = agg
        G = I.surface_operator(S, C)
        j = C.shape[0] // 2
        fit = I.knothe_fit(x, G[:, j], depth=depth)
        out["G_mid"] = {k: float(v) for k, v in fit.items()}
        out["G_mid"]["volume_ratio_surface_to_convergence"] = float(np.trapezoid(G[:, j], x) / a)
        out["G_mid"]["width_10pct_m"] = I.width_at_fraction(x, G[:, j], 0.1)
        out["G_mid"]["min_value_rel"] = float(G[:, j].min() / G[:, j].max())
        # free influence of a unit deficit (no neighbours held): S column normalised by its own convergence
        Sf = S[:, j] / C[j, j]
        out["S_free_mid"] = {k: float(v) for k, v in I.knothe_fit(x, Sf, depth=depth).items()}
        # compliance decay
        out["C_row_mid_rel_lag"] = [float(v) for v in (C[j, j:j + 10] / C[j, j])]
    else:
        # axisymmetric: unit convergence of the central disc (others held) → 3D point-source influence
        G = I.surface_operator(S, C)
        r = x
        g = G[:, 0]
        area0 = math.pi * edges[1] ** 2
        vol_s = float(np.trapezoid(2 * math.pi * r * g, r))
        out["G_disc"] = {"disc_radius_m": float(edges[1]), "volume_ratio_surface_to_convergence": vol_s / area0,
                         "s_max_per_unit_conv": float(g.max())}
        # 3D Knothe g(r) = V/r_k² · exp(−π r²/r_k²): fit r_k by least squares
        best = None
        for rk in np.geomspace(50, 2000, 800):
            f = np.exp(-math.pi * r ** 2 / rk ** 2) / rk ** 2
            A = float(f @ g / (f @ f))
            res = float(np.sqrt(np.mean((g - A * f) ** 2)))
            if best is None or res < best[0]:
                best = (res, rk, A)
        out["G_disc"].update({"r_knothe_3d_m": best[1], "tan_beta_3d": depth / best[1],
                              "rel_rms": best[0] / float(g.max())})
        A = np.pi * (edges[1:] ** 2 - edges[:-1] ** 2)
        Aw = np.diag(A) @ C                       # reciprocity: A_i·C_ij symmetric
        out["betti_asymmetry_area_weighted"] = float(np.max(np.abs(Aw - Aw.T)) / np.max(np.abs(Aw)))
        ss = I.sign_structure(np.linalg.inv(C))   # no symmetrisation of C in axisymmetry
        out["K_rings"] = {"z_matrix": ss["z_matrix"], "first_positive_lag": ss["first_positive_lag"],
                          "lag_profile_0_4": [float(v) for v in ss["lag_profile"][:5]]}
    return out


def fine_vs_coarse(root: Path, coarse_case: str = "PS_M05_A27", fine_case: str = "PS_M05_FINE27",
                   cell_cases=("CELL_A27_B_EL", "CELL_A27_F_EL")) -> dict:
    """AG §31: is MK-MINED-CELL (pillar + chamber merged) externally equivalent to the explicit pillars/chambers?"""
    dc = np.load(root / coarse_case / "compliance.npz")
    df = np.load(root / fine_case / "fine.npz")
    C, S, x = dc["C"], dc["S"], dc["x"]
    a, k, wp, h = 27.0, 10.2 / 27.0, 16.8, 5.5
    sig0 = float(df["sigma0"])
    E = setup.E_SCEN["SYLV"][setup.SCEN_INDEX["M05"]]
    Ep = laws.plane_strain_modulus(E, setup.NU["SYLV"])
    K = np.linalg.inv(0.5 * (C + C.T))
    n = K.shape[0]
    out = {}
    variants = [("tributary_plane_strain_free", (1 - k) * Ep / h)]
    for cell_case in cell_cases:
        hp = root.parent / "cells" / cell_case / "history.npz"
        if hp.exists():
            hc = np.load(hp)
            variants.append((f"unit_cell_{cell_case}", k * sig0 / float(hc["cell_convergence"][-1])))
    for label, Ecell in variants:
        # (1−k)(σ0 + E'v/h) = σ0 − K v  →  (K + (1−k)E'/h) v = k σ0
        v = np.linalg.solve(K + Ecell * np.eye(n), k * sig0 * np.ones(n))
        s_coarse = S @ np.linalg.solve(0.5 * (C + C.T), v)     # s = G v = S C⁻¹ v
        conv = df["floor_uy"] - df["roof_uy"]
        edges = dc["cell_edges"]
        v_fine = np.array([np.trapezoid(conv[(x >= edges[i] - 1e-9) & (x <= edges[i + 1] + 1e-9)],
                                        x[(x >= edges[i] - 1e-9) & (x <= edges[i + 1] + 1e-9)]) / a for i in range(n)])
        s_fine = -df["top_uy"]
        out[label] = {
            "sigma0_Pa": sig0, "k": k, "k_cell_Pa_per_m": float(Ecell),
            "v_mid_fine_mm": float(v_fine[n // 2] * 1e3), "v_mid_coarse_mm": float(v[n // 2] * 1e3),
            "s_max_fine_mm": float(s_fine.max() * 1e3), "s_max_coarse_mm": float(s_coarse.max() * 1e3),
            "E_ext_surface_rel": external_equivalence(s_fine, s_coarse, "sup", relative_to=float(s_fine.max())),
            "E_ext_cells_rel": external_equivalence(v_fine, v, "sup", relative_to=float(v_fine.max())),
        }
        np.savez(root.parent / "analysis" / f"fine_vs_coarse_{fine_case}_{label}.npz", x=x, s_fine=s_fine,
                 s_coarse=s_coarse,
                 v_fine=v_fine, v_coarse=v, edges=edges)
        out[label]["surface_area_ratio_coarse_to_fine"] = float(np.trapezoid(s_coarse, x) / np.trapezoid(s_fine, x))
    return out


def vzt_coupling(cd: Path) -> dict | None:
    """Horizontal strain on the tops of the VZT layers per unit convergence (W0-L-15 criterion [ε] = 0.002)."""
    pf = cd / "probes.npz"
    if not pf.exists():
        return None
    d = np.load(cd / "compliance.npz")
    p = np.load(pf)
    C, x, ut = d["C"], d["x"], float(d["unit_traction"])
    Ci = np.linalg.inv(0.5 * (C + C.T))
    out = {}
    v = np.ones(C.shape[0])                     # uniform unit convergence of every cell (others' tractions adjust)
    for q, name in enumerate(p["layers"]):
        Ux = p["P"][:, q, :, 0].T / ut          # (nx, cells) per Pa of deficit
        eps = np.gradient(Ux @ Ci @ v, x)
        k_ext = float(eps.max())
        out[str(name)] = {"depth_m": float(p["depth_top"][q]), "max_extension_per_m_conv": k_ext,
                          "max_compression_per_m_conv": float(-eps.min()), "x_of_max_extension_m": float(x[np.argmax(eps)]),
                          "uniform_convergence_for_normative_limit_m": 0.002 / k_ext}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    res = {}
    for cd in sorted(a.root.iterdir()):
        f = cd / "compliance.npz"
        if f.exists():
            res[cd.name] = summarize(cd.name, np.load(f))
    if (a.root / "PS_M05_FINE27" / "fine.npz").exists() and (a.root / "PS_M05_A27" / "compliance.npz").exists():
        res["FINE_VS_COARSE_A27"] = fine_vs_coarse(a.root)
    if (a.root / "PS_M05_FINE27F" / "fine.npz").exists() and (a.root / "PS_M05_A27F" / "compliance.npz").exists():
        res["FINE_VS_COARSE_A27F"] = fine_vs_coarse(a.root, "PS_M05_A27F", "PS_M05_FINE27F", ("CELL_A27_F_EL",))
    if (a.root / "PS_M05_FINE27N" / "fine.npz").exists():
        res["FINE_VS_COARSE_A27_noabut"] = fine_vs_coarse(a.root, "PS_M05_A27", "PS_M05_FINE27N", ("CELL_A27_B_EL",))
    if (a.root / "PS_M05_FINE27FN" / "fine.npz").exists():
        res["FINE_VS_COARSE_A27F_noabut"] = fine_vs_coarse(a.root, "PS_M05_A27F", "PS_M05_FINE27FN", ("CELL_A27_F_EL",))
    res["VZT_COUPLING"] = {cd.name: vzt_coupling(cd) for cd in sorted(a.root.iterdir()) if (cd / "probes.npz").exists()}
    (a.out / "influence_summary.json").write_text(json.dumps(res, indent=1, ensure_ascii=False, default=float),
                                                  encoding="utf-8")
    for k, v in res.items():
        if k.startswith("FINE") or k.startswith("VZT"):
            print(k, json.dumps(v, ensure_ascii=False))
            continue
        if v["axisym"]:
            print(k, "AX", json.dumps(v["G_disc"]), v["K_rings"])
            continue
        print(f"{k:14s} Z={v['K']['z_matrix']!s:5} lag1..3={np.round(v['K']['lag_profile_0_8'][1:4], 3)} "
              f"r={v['G_mid']['r']:.0f} tanb={v['G_mid']['tan_beta']:.2f} rrms={v['G_mid']['rel_rms']:.3f} "
              f"vol={v['G_mid']['volume_ratio_surface_to_convergence']:.3f} min={v['G_mid']['min_value_rel']:.3f} "
              f"agg={ {kk: (vv['z_matrix'], vv['first_positive_lag']) for kk, vv in v['K_aggregated'].items()} }")


if __name__ == "__main__":
    main()

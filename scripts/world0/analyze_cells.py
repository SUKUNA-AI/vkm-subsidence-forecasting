"""Post-processing of the unit-cell runs: reduced pillar/cell laws, closure test (MS §29), pair coefficients.

Usage: python scripts/world0/analyze_cells.py --root work/world0_20261023/cells --out work/world0_20261023/analysis
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import identification_setup as setup  # noqa: E402

from vkm_world.representations.world0 import laws  # noqa: E402
from vkm_world.representations.world0.closure import state_closure_test  # noqa: E402

H = 5.5


def load(root: Path, case: str):
    p = root / case / "history.npz"
    return dict(np.load(p)) if p.exists() else None


def creep_part(d, key):
    """Quantity minus its value right after excavation (t = 2e-7): the creep-driven increment."""
    t = d["t"]
    i0 = int(np.argmin(np.abs(t - 2e-7)))
    return t[i0:], d[key][i0:] - d[key][i0], d[key][i0]


def late_rate(t, y, t_from=20.0):
    k = t >= t_from
    return float(np.polyfit(t[k], y[k], 1)[0])


def identify(root: Path) -> dict:
    out = {}
    for case in ("CELL_A27_B_FIELD", "CELL_A27_B_FIELD_ELSLAB", "CELL_A27_F_FIELD", "CELL_A27_B_LAB",
                 "CELL_B20_B_FIELD", "CELL_B20_B_LAB"):
        d = load(root, case)
        if d is None:
            continue
        rheo = case.split("_")[3]
        eta_c = setup.ETA[rheo]["SYLV"] * 1e6
        sp = float(np.median(d["sigma_pillar"][d["t"] > 1e-3]))
        rate_uni = float(laws.norton_rate(sp, eta_c, 5.0))       # 1/yr (η in Pa·yr)
        t, dvp, vp0 = creep_part(d, "pillar_shortening")
        _, dvc, vc0 = creep_part(d, "cell_convergence")
        _, dch, ch0 = creep_part(d, "chamber_convergence")
        r_p = late_rate(t, dvp) / H
        r_c = late_rate(t, dvc) / H
        out[case] = {
            "sigma_pillar_MPa": sp / 1e6,
            "elastic_pillar_shortening_mm": vp0 * 1e3, "elastic_cell_convergence_mm": vc0 * 1e3,
            "elastic_chamber_convergence_mm": ch0 * 1e3,
            "creep_pillar_shortening_40yr_mm": float(dvp[-1] * 1e3),
            "creep_cell_convergence_40yr_mm": float(dvc[-1] * 1e3),
            "creep_chamber_convergence_40yr_mm": float(dch[-1] * 1e3),
            "uniaxial_norton_rate_per_yr": rate_uni,
            "c_shape_pillar": r_p / rate_uni,
            "c_shape_cell": r_c / rate_uni,
            "plane_strain_free_factor": laws.plane_strain_free_factor(5.0),
            "early_to_late_rate_ratio_pillar": float((dvp[np.searchsorted(t, 1.0)] / t[np.searchsorted(t, 1.0)])
                                                     / late_rate(t, dvp)),
        }
    return out


def closure(root: Path) -> dict:
    A = load(root, "CELL_A27_B_FIELD")
    B = load(root, "CELL_A27_B_FIELD_HIST")
    if A is None or B is None:
        return {}
    res = {}
    for key in ("pillar_shortening", "cell_convergence", "chamber_convergence"):
        tB = B["t"]
        iB = int(np.argmin(np.abs(tB - 2.001)))
        vB0 = B[key][iB]
        tA, vA = A["t"], A[key]
        k = tA > 1e-3
        tstar = float(np.interp(vB0, vA[k], tA[k]))          # A reaches B's reduced state at t*
        horizon = min(40.0 - tstar, 40.0 - 2.001)
        taus = np.linspace(0, horizon, 41)
        incA = np.interp(tstar + taus, tA[k], vA[k]) - vB0
        incB = np.interp(2.001 + taus, tB, B[key]) - vB0
        ref = float(np.max(np.abs(incA)))

        def fut(X):
            return X[1]

        r = state_closure_test([(vB0, incA), (vB0, incB)], reduce=lambda X: X[0], future_response=fut,
                               tolerance=0.05, relative_to=ref)
        by_h = {}
        for hz in (1.0, 2.0, 5.0, 10.0, 20.0):
            if hz <= horizon:
                m = taus <= hz + 1e-9
                tt = np.linspace(0, hz, 21)
                a_ = np.interp(tstar + tt, tA[k], vA[k]) - vB0
                b_ = np.interp(2.001 + tt, tB, B[key]) - vB0
                by_h[f"{hz:g}yr"] = float(np.max(np.abs(a_ - b_)) / np.max(np.abs(a_)))
        res[key] = {"reduced_state_mm": float(vB0 * 1e3), "t_star_A_yr": tstar, "horizon_yr": float(horizon),
                    "spread_rel_by_horizon": by_h,
                    "future_increment_A_mm": float(incA[-1] * 1e3), "future_increment_B_mm": float(incB[-1] * 1e3),
                    "spread_rel": r.spread, "verdict_tol5pct": r.verdict,
                    "early_rate_ratio_B_over_A": float((incB[1] / taus[1]) / (incA[1] / taus[1]))}
    return res


def backfill(root: Path) -> dict:
    out = {}
    for case in sorted(p.name for p in root.iterdir() if "_BF_" in p.name):
        d = load(root, case)
        ref = load(root, case.split("_BF_")[0] + "_B_" + case.split("_BF_")[1].split("_")[0])
        if d is None:
            continue
        t = d["t"]
        sf = d["sigma_fill"]
        engaged = t[np.argmax(sf > 0.01e6)] if np.any(sf > 0.01e6) else None
        o = {"t_contact_yr": float(engaged) if engaged is not None else None,
             "sigma_fill_40yr_MPa": float(sf[-1] / 1e6), "sigma_fill_x_40yr_MPa": float(d["sigma_fill_x"][-1] / 1e6),
             "sigma_pillar_40yr_MPa": float(d["sigma_pillar"][-1] / 1e6),
             "chamber_convergence_40yr_mm": float(d["chamber_convergence"][-1] * 1e3),
             "cell_convergence_40yr_mm": float(d["cell_convergence"][-1] * 1e3)}
        if ref is not None:
            o["no_fill_chamber_convergence_40yr_mm"] = float(ref["chamber_convergence"][-1] * 1e3)
            o["no_fill_cell_convergence_40yr_mm"] = float(ref["cell_convergence"][-1] * 1e3)
            o["convergence_reduction_rel"] = 1 - o["cell_convergence_40yr_mm"] / o["no_fill_cell_convergence_40yr_mm"]
        out[case] = o
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    res = {"identification": identify(a.root), "closure": closure(a.root), "backfill": backfill(a.root)}
    (a.out / "cells_summary.json").write_text(json.dumps(res, indent=1, ensure_ascii=False, default=float),
                                              encoding="utf-8")
    print(json.dumps(res, indent=1, ensure_ascii=False, default=float))


if __name__ == "__main__":
    main()

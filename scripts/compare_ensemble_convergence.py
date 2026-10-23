"""Monte Carlo convergence of the geology ensemble: 5 000 vs 10 000 realizations.

Compares (a) two independent grid runs (different seeds and counts), (b) the nested first
N realizations of the larger run against all of it, for P10/P50/P90 of the SZ and PDKS roofs
and P(zero PKS thickness); and LOO coverage of runs with different draws per fold.
Differences are Monte Carlo noise of synthetic statistics, not uncertainty of geology.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

KEY = ("SZ", "PDKS")
PROBS = {1: "P10", 2: "P50", 3: "P90"}


def stats(diff, mask):
    d = np.abs(diff[mask])
    return {"median_m": float(np.median(d)), "p99_m": float(np.percentile(d, 99)), "max_m": float(d.max())}


def grid_compare(small, large, variant):
    a, b = np.load(small / ("ensemble_%s.npz" % variant)), np.load(large / ("ensemble_%s.npz" % variant))
    mask = b["support_mask"]
    regions = {"INTERPOLATION": mask == "INTERPOLATION", "EXTRAPOLATION": mask == "EXTRAPOLATION"}
    contacts = list(b["contacts"])
    k = contacts.index("PKS")
    out = {"realizations": {"independent_run": int(a["realizations"]), "large_run": int(b["realizations"]),
                            "nested_subset": int(b["subset_size"]) if "subset_size" in b.files else 0}}
    for c in KEY:
        for i, name in PROBS.items():
            for label, ref in (("independent_vs_large", a[c + "_q"][i]),
                               ("nested_vs_large", b[c + "_q_subset"][i] if c + "_q_subset" in b.files else None)):
                if ref is None:
                    continue
                out.setdefault(c, {}).setdefault(name, {})[label] = {r: stats(ref - b[c + "_q"][i], m) for r, m in regions.items()}
    pz = b["p_zero"][k]
    out["p_zero_PKS"] = {"independent_vs_large": {r: {"median": float(np.median(np.abs(a["p_zero"][k] - pz)[m])),
                                                      "max": float(np.abs(a["p_zero"][k] - pz)[m].max())} for r, m in regions.items()}}
    if "p_zero_subset" in b.files:
        out["p_zero_PKS"]["nested_vs_large"] = {r: {"median": float(np.median(np.abs(b["p_zero_subset"][k] - pz)[m])),
                                                    "max": float(np.abs(b["p_zero_subset"][k] - pz)[m].max())} for r, m in regions.items()}
    # binomial Monte Carlo standard error of P(zero) at the largest count, for scale
    out["p_zero_PKS"]["mc_standard_error_max"] = float(np.sqrt(0.25 / int(b["realizations"])))
    return out


def cv_compare(runs):
    table = {}
    for label, path in runs.items():
        summary = json.loads((path / "cv_summary.json").read_text(encoding="utf-8"))["summary"]
        for pos in ("INTERPOLATION", "EXTRAPOLATION"):
            for qty in ("depth", "thickness"):
                key = "/".join(("leave_one_physical_borehole_out", pos, qty, "G", "SELECTED"))
                if key in summary:
                    b = summary[key]["pooled"]
                    table.setdefault(pos + "/" + qty, {})[label] = {"n": b["n_readings"], "coverage80": b["coverage80"],
                                                                     "coverage90": b["coverage90"], "crps_m": b["crps_mean_m"],
                                                                     "mae_p50_m": b["mae_p50_m"]}
    return table


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--grid-small", type=Path, required=True)
    p.add_argument("--grid-large", type=Path, required=True)
    p.add_argument("--cv", action="append", default=[], help="label=path of a cv output directory")
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    result = {"status": "Monte Carlo noise of synthetic statistics; not geological uncertainty",
              "grid": {v: grid_compare(args.grid_small, args.grid_large, v) for v in ("G", "P90")},
              "loo_coverage": cv_compare({s.split("=", 1)[0]: Path(s.split("=", 1)[1]) for s in args.cv})}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(result["loo_coverage"], ensure_ascii=False))


if __name__ == "__main__":
    main()

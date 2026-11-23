"""Mesh convergence of the creep response on the v6_2 slice (GPU): a family of meshes from coarse to 2× finer than the
MAPDL/OGS reference mesh near the seam, same physics, same time schedule. Answers how far the reference mesh itself
is from a mesh-converged solution (part of the representation error of every solver on that mesh)."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import torch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "src"))

from vkm_world.representations.gpu_plane_strain import MeshSpec, time_steps  # noqa: E402
import ensemble_core as EC  # noqa: E402
import ensemble_design as ED  # noqa: E402
from world_inputs import Tables  # noqa: E402

FAMILY = {
    "coarse": MeshSpec(),
    "seamA2": ED.MESH_SPECS["seamA2"],
    "ref_like": ED.MESH_SPECS["ref_like"],
    "fine2": MeshSpec(rows=(2, 10, 8, 6, 10, 16, 6, 6, 12, 18), chamber_elems=16, pillar_h=0.65, outer_h0=0.65,
                      outer_growth=1.15, outer_hmax=10.0),
    "fine3": MeshSpec(rows=(2, 10, 8, 6, 10, 24, 9, 9, 18, 18), chamber_elems=24, pillar_h=0.433, outer_h0=0.433,
                      outer_growth=1.12, outer_hmax=10.0),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profiles", type=Path, required=True)
    ap.add_argument("--materials", type=Path, required=True)
    ap.add_argument("--transfer", type=Path, required=True)
    ap.add_argument("--section-json", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--sched", default="0.01,1.25,0.5")
    ap.add_argument("--mem-gb", type=float, default=3.5)
    ap.add_argument("--only", nargs="*", help="subset of mesh names; results are merged into --out")
    ap.add_argument("--method", default="cr", choices=["cr", "thomas"])
    a = ap.parse_args()
    free, total = torch.cuda.mem_get_info()
    torch.cuda.set_per_process_memory_fraction(min(a.mem_gb * 1e9, free - 1.0e9) / total)
    tab = Tables(a.materials, a.transfer)
    z = np.load(a.profiles)
    sec = json.loads(a.section_json.read_text(encoding="utf-8"))
    prof = {k: z[k] for k in ("chain", "dsm", "v62", "realizations")}
    prof["base_z"] = sec["model_choices"]["base_elevation_m"][0]
    sched = tuple(float(v) for v in a.sched.split(","))
    steps = time_steps(40.0, *sched, marks=ED.OUT_TIMES[1:])
    EC.SOLVER_KW = dict(factor_dtype=torch.float32, keep_factor_between_steps=True, reuse_ratio=0.4, method=a.method)
    res = {"schema": "vkm.gpu_ensemble.mesh_convergence/1", "schedule": a.sched, "n_steps": len(steps), "times": ED.OUT_TIMES,
           "cases": {}}
    if a.only and a.out.exists():
        res = json.loads(a.out.read_text(encoding="utf-8"))
    for var, L, visc in (("A27", 27.0, "FIELD"), ("B20", 20.0, "LAB")):
        for name, spec in FAMILY.items():
            if a.only and name not in a.only:
                continue
            P = {"E_factor": [0.5], "eta_sylv": [tab.eta(visc, "sylvinite")], "eta_salt": [tab.eta(visc, "rock_salt")],
                 "eta_carn": [tab.eta(visc, "carnallite")], "L_axis": [L], "d_roof": [10.0], "h_seam": [5.5], "fill_on": [0],
                 "fill_frac": [0.5], "E_fill": [2e8], "k0": [0], "geo": [-1], "norm": [0]}
            t0 = time.time()
            out = EC.run_batch(tab, prof, P, spec, steps, ED.OUT_TIMES, device="cuda", with_ref=True)
            res["cases"].setdefault(f"M05_{visc}_{var}", {})[name] = {
                "nx": out["nx"], "nz": out["nz"], "s_max_mm": (out["s"][0].max(axis=1) * 1e3).tolist(),
                "conv_central_m": out["conv_central_m"][0].tolist(), "time_s": round(time.time() - t0, 1), "stats": out["stats"]}
            print(var, name, out["nx"], out["nz"], np.round(out["s"][0].max(axis=1) * 1e3, 3).tolist(), flush=True)
            a.out.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()

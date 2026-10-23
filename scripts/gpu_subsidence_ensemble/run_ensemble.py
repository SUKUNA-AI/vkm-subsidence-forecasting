"""Run the GPU ensemble over elements of world uncertainty (Saltelli design by groups), in resumable batches.

    python scripts/gpu_subsidence_ensemble/run_ensemble.py --profiles profiles.npz --materials step3_materials.json \
        --transfer creep_transfer.json --section-json section.json --gate ladder_receipts_gpu.json --out <dir>

The run refuses to start unless the ladder gate (L1–L4) in ``--gate`` is PASS. Design, seeds, versions, input hashes
and per-batch GPU time/memory go to receipts; results per batch go to ``batches/`` and are merged by
``analyze_ensemble.py``. All outputs are MODEL results (synthetic worlds), never observations.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
import time
from pathlib import Path

import numpy as np
import torch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "src"))

from vkm_world.representations.gpu_plane_strain import time_steps  # noqa: E402
import ensemble_core as EC  # noqa: E402
import ensemble_design as ED  # noqa: E402
from world_inputs import Tables  # noqa: E402

GRID_DX = 5.0  # m, common chainage grid of stored profiles


def sha(p) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def make_design(N: int, seed: int):
    U, labels = ED.saltelli(N, seed)
    P = ED.transform(U)
    P["run_id"] = np.arange(len(U))
    P["block"] = np.array([b for b, _ in labels])
    P["group"] = np.array([g for _, g in labels])
    P["n_ch"] = ED.n_chambers(P["L_axis"])
    return U, P


def batches(P: dict, B: int, rng: np.random.Generator):
    keys = sorted(set(zip(P["n_ch"].tolist(), P["k0"].tolist())))
    out = []
    for nch, k0 in keys:
        ids = np.where((P["n_ch"] == nch) & (P["k0"] == k0))[0]
        rng.shuffle(ids)
        for i in range(0, len(ids), B):
            out.append((int(nch), int(k0), np.sort(ids[i:i + B])))
    order = rng.permutation(len(out))  # random order of batches: partial results are a random subset of the design
    return [out[k] for k in order]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profiles", type=Path, required=True)
    ap.add_argument("--materials", type=Path, required=True)
    ap.add_argument("--transfer", type=Path, required=True)
    ap.add_argument("--section-json", type=Path, required=True)
    ap.add_argument("--gate", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--N", type=int, default=1024)
    ap.add_argument("--seed", type=int, default=20261023)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--spec", default="seamA2")
    ap.add_argument("--sched", default="0.02,1.3,1.0")
    ap.add_argument("--mem-gb", type=float, default=11.0, help="upper bound of GPU memory for this process")
    ap.add_argument("--max-batches", type=int, default=0)
    ap.add_argument("--only", type=int, nargs="*", help="run only these run ids (retry pass)")
    ap.add_argument("--tag", default="main")
    a = ap.parse_args()
    gate = json.loads(a.gate.read_text(encoding="utf-8"))
    st = {k: gate["steps"].get(k, {}).get("status") for k in ("L1_synthetic", "L2_equilibrium_LS1", "L3_elastic", "L4_creep")}
    if not all(v == "PASS" for v in st.values()):
        raise SystemExit(f"gate not passed: {st}")
    dev = "cuda"
    free, total = torch.cuda.mem_get_info()
    cap = min(a.mem_gb * 1e9, free - 1.5e9)
    torch.cuda.set_per_process_memory_fraction(max(0.05, cap / total))
    a.out.mkdir(parents=True, exist_ok=True)
    bdir = a.out / "batches" / a.tag
    bdir.mkdir(parents=True, exist_ok=True)
    U, P = make_design(a.N, a.seed)
    design_path = a.out / "design.npz"
    if not design_path.exists():
        np.savez_compressed(design_path, U=U, **{k: np.asarray(v) for k, v in P.items()},
                            param_names=np.array([p[0] for p in ED.param_list()]),
                            groups=np.array([g for g, _ in ED.GROUPS]))
    tab = Tables(a.materials, a.transfer)
    z = np.load(a.profiles)
    sec = json.loads(a.section_json.read_text(encoding="utf-8"))
    prof = {k: z[k] for k in ("chain", "dsm", "v62", "realizations")}
    prof["realizations"] = prof["realizations"][:ED.N_REAL]  # variant G (first 50)
    names = z["realization_names"][:ED.N_REAL]
    assert all(str(n).startswith("realizations_subset_G#") for n in names), names[:3]
    prof["base_z"] = sec["model_choices"]["base_elevation_m"][0]
    spec = ED.MESH_SPECS[a.spec]
    sched = tuple(float(v) for v in a.sched.split(","))
    steps = time_steps(40.0, *sched, marks=ED.OUT_TIMES[1:])
    EC.SOLVER_KW = dict(factor_dtype=torch.float32, keep_factor_between_steps=True, reuse_ratio=0.4, strict=False)
    grid = np.arange(0.0, float(prof["chain"][-1]) + 1e-6, GRID_DX)
    receipt = {
        "schema": "vkm.gpu_ensemble.run/1", "tag": a.tag, "N": a.N, "seed": a.seed, "runs": int(len(U)),
        "groups": [g for g, _ in ED.GROUPS], "params": [{"name": n, "kind": k, "lo": lo, "hi": hi, "status": s}
                                                         for n, k, lo, hi, s in ED.param_list()],
        "mesh_spec": a.spec, "mesh_spec_values": ED.spec_dict(spec), "schedule": a.sched, "n_steps": len(steps),
        "solver": {"factorization": "block cyclic reduction, float32 with float64 residuals", "newton_tol_m": 1e-9,
                   "reuse_ratio": 0.4, "keep_factor_between_steps": True, "return_iterations": 8},
        "env": {"python": platform.python_version(), "torch": torch.__version__, "cuda": torch.version.cuda,
                "gpu": torch.cuda.get_device_name(0), "numpy": np.__version__, "platform": platform.platform(),
                "memory_cap_gb": round(cap / 1e9, 2)},
        "inputs": {"profiles.npz": sha(a.profiles), "step3_materials.json": sha(a.materials),
                   "creep_transfer.json": sha(a.transfer), "section.json": sha(a.section_json),
                   "ladder_receipts_gpu.json": sha(a.gate), "design.npz": sha(design_path)},
        "gate": st, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    (a.out / f"receipt_run_{a.tag}.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=1), encoding="utf-8")
    rng = np.random.default_rng(a.seed)
    if a.only:
        sel = {k: np.asarray(v)[a.only] for k, v in P.items()}
        sel["run_id"] = np.asarray(a.only)
        Pw = sel
    else:
        Pw = P
    plan = batches(Pw, a.batch, rng)
    log = open(a.out / f"progress_{a.tag}.log", "a", encoding="utf-8")
    t_all = time.time()
    done_runs = 0
    for bi, (nch, k0, idx) in enumerate(plan):
        if a.max_batches and bi >= a.max_batches:
            break
        f = bdir / f"b{bi:05d}_n{nch}_k{k0}_r{int(idx[0]):05d}.npz"
        if f.exists():
            done_runs += len(idx)
            continue
        Pb = {k: np.asarray(v)[idx] for k, v in Pw.items()}
        torch.cuda.reset_peak_memory_stats()
        t0 = time.time()
        try:
            out = EC.run_batch(tab, prof, Pb, spec, steps, ED.OUT_TIMES, device=dev, with_ref=True)
        except torch.OutOfMemoryError:
            torch.cuda.empty_cache()
            print(f"batch {bi}: OOM, skipped (rerun with a smaller --batch)", file=log, flush=True)
            continue
        dtb = time.time() - t0
        s_grid = np.stack([[np.interp(grid, out["xs"][m], out["s"][m, j]) for j in range(len(ED.OUT_TIMES))]
                           for m in range(len(idx))]).astype(np.float32)
        met = {k: np.zeros((len(idx), len(ED.OUT_TIMES))) for k in
               ("s_max_mm", "x_at_max_m", "width_10mm_m", "tilt_max_mm_per_m", "ux_max_mm", "eps_h_max_mm_per_m")}
        for m in range(len(idx)):
            for j in range(len(ED.OUT_TIMES)):
                r = EC.surface_metrics(out["xs"][m], out["s"][m, j].astype(float), out["ux"][m, j].astype(float))
                for k in met:
                    met[k][m, j] = r[k]
        np.savez_compressed(f, run_id=Pb["run_id"], t=np.asarray(ED.OUT_TIMES), grid=grid, s_grid_m=s_grid,
                            conv_central_m=out["conv_central_m"], background_mm=out["background_mm"],
                            pillar_creep_eq_mean_final=out["pillar_creep_eq_mean_final"],
                            seam_roof_depth_centre_m=out["seam_roof_depth_centre_m"], adjust_max_m=out["adjust_max_m"],
                            failed=out["failed"], nx=out["nx"], nz=out["nz"], n_chambers=nch,
                            gpu_time_s=dtb, peak_mem_gb=torch.cuda.max_memory_allocated() / 1e9,
                            factorizations=out["stats"]["factorizations"], solves=out["stats"]["solves"], **met)
        done_runs += len(idx)
        el = time.time() - t_all
        msg = (f"{time.strftime('%H:%M:%S')} batch {bi + 1}/{len(plan)} n_ch={nch} k0={k0} B={len(idx)} "
               f"{dtb:.1f}s peak {torch.cuda.max_memory_allocated() / 1e9:.1f}GB fact {out['stats']['factorizations']} "
               f"solves {out['stats']['solves']} failed {int(out['failed'].sum())} | done {done_runs}/{len(Pw['run_id'])}")
        print(msg, file=log, flush=True)
        print(msg, flush=True)
    receipt["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    receipt["wall_time_s"] = round(time.time() - t_all, 1)
    (a.out / f"receipt_run_{a.tag}.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()

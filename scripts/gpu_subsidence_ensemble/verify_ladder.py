"""Solver ladder of the GPU representation against analytic truths and MAPDL/OGS reference runs (gate before the
ensemble). Criteria are fixed in ``gate_criteria.json`` (written before the formal comparison).

L1 synthetic tests (pytest); L2 LS1 equilibrium vs MAPDL; L3 elastic excavation (explicit chambers A27/B20) and step-6
equivalent-layer cases (K0 / λ=1, modulus change in 3 stages); L4 creep on the reference meshes vs MAPDL fine step,
OGS and the main MAPDL runs; L5 representation error of the ensemble configuration (mesh spec, schedule, float32).
All references are MODEL results of other solvers — this is verification of an implementation, not validation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import torch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "src"))

from vkm_world.representations.gpu_plane_strain import (Materials, MeshSpec, PlaneStrainCreepSolver,  # noqa: E402
                                                        column_vertical_stress, mesh_from_npz, stress_from_lambda,
                                                        time_steps)
import ensemble_core as EC  # noqa: E402
from world_inputs import G_ACC, Tables, base_zone  # noqa: E402

TIMES = [0.25, 1, 2, 5, 10, 15, 20, 25, 30, 35, 40]
T_ALL = [0.0] + TIMES
CASES = [("A27", "M05", "FIELD"), ("A27", "M05", "LAB"), ("B20", "M05", "FIELD"), ("B20", "M05", "LAB"),
         ("A27", "M05", "FIELD_N"), ("A27", "NORM", "FIELD")]
SCHED_CONVERGED = (0.002, 1.1, 0.1)


def sha(p) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def env_info() -> dict:
    d = {"python": platform.python_version(), "torch": torch.__version__, "numpy": np.__version__,
         "cuda": torch.version.cuda, "platform": platform.platform()}
    if torch.cuda.is_available():
        d["gpu"] = torch.cuda.get_device_name(0)
    return d


def solver_kw(a) -> dict:
    return dict(factor_dtype=torch.float32 if a.factor == "fp32" else torch.float64, keep_factor_between_steps=True,
                reuse_ratio=0.4)


def zone_materials(tab: Tables, zone: np.ndarray, scen: str, visc: str | None, dev):
    E = np.zeros(zone.shape)
    nu = np.zeros(zone.shape)
    rho = np.zeros(zone.shape)
    A = np.zeros(zone.shape)
    for z in np.unique(zone):
        bz = base_zone(int(z)) if int(z) not in (11, 12, 13) else 6
        r = tab.zrow[bz]
        m = zone == z
        E[m] = r["E_Pa"][scen]
        nu[m] = r["nu"]
        rho[m] = tab.rho(bz)
        if visc is not None:
            lith = tab.transfer["zones"].get(str(int(z)), {}).get("lithology")
            if lith:
                A[m] = 1.0 / tab.eta(visc, lith)
    T = lambda x: torch.as_tensor(x, dtype=torch.float64, device=dev)[None]
    return T(E), T(nu), T(rho), T(A)


def ref_mesh(raw: Path, var: str, dev):
    m = np.load(raw / f"mesh_{var}_mesh.npz")
    return m, mesh_from_npz(m["nodes"], m["zone"], m["xs"], int(m["nz"]), device=dev)


def run_reference_case(tab, raw, var, scen, visc, sched, kw, dev, with_ref=True):
    m, mesh = ref_mesh(raw, var, dev)
    nz = int(m["nz"])
    zone = m["zone"].reshape(len(m["xs"]) - 1, nz)
    E, nu, rho, A = zone_materials(tab, zone, scen, visc, dev)
    mat = Materials(E, nu, rho, A, torch.ones_like(E))
    sig0 = stress_from_lambda(column_vertical_stress(mesh, rho * G_ACC), torch.ones_like(E))
    t0 = time.time()
    S = PlaneStrainCreepSolver(mesh, mat, sig0, n=tab.n, p0=tab.p0, **kw)
    st1 = S.initial_state()
    exc = mat.clone()
    exc.alive = (mesh.zone != 15).to(torch.float64)
    st2 = S.elastic_stage(st1, exc, "EXC")
    steps = time_steps(40.0, *sched, marks=TIMES)
    grab = lambda t, st: st.u[0, :, 0, :].cpu().numpy().copy()
    ch = m["chambers"]
    cols = [int(np.argmin(np.abs(m["xs"] - 0.5 * (c0 + c1)))) for c0, c1 in ch]
    c = cols[len(cols) // 2]
    jr = int(sum(m["rows_per"][:6]))
    jf = jr + int(m["rows_per"][6])
    grab_c = lambda t, st: (st.u[0, :, 0, :].cpu().numpy().copy(), float(st.u[0, c, jf, 1] - st.u[0, c, jr, 1]))
    rE = S.run_creep(st2, exc, steps, T_ALL, grab_c)
    rR = S.run_creep(st1, mat, steps, T_ALL, grab_c) if with_ref else None
    u1 = st1.u[0, :, 0, :].cpu().numpy()
    out = {"t": T_ALL, "s_mm": [], "conv_m": [], "background_mm": []}
    for t in T_ALL:
        ue, ce = rE["outputs"][round(t, 9)]
        ur, cr = rR["outputs"][round(t, 9)] if rR else (u1, 0.0)
        out["s_mm"].append((-(ue - ur)[:, 1] * 1e3).tolist())
        out["conv_m"].append(ce - cr)
        out["background_mm"].append(float(np.abs(-(ur - u1)[:, 1]).max() * 1e3))
    out["u1_full"] = st1.u[0].cpu().numpy().reshape(-1, 2)
    out["stats"] = dict(S.stats)
    out["n_steps"] = len(steps)
    out["time_s"] = time.time() - t0
    return out


def mapdl_profiles(raw, var, scen, visc, kind=""):
    v = visc.replace("_", "")
    E = np.load(raw / f"{scen}_{v}_{var}_EXC{kind}.npz")["u_surface_all_ls"]
    R = np.load(raw / f"{scen}_{v}_{var}_REF.npz")["u_surface_all_ls"]
    return np.array([-(E[k + 1, :, 1] - R[k + 1, :, 1]) * 1e3 for k in range(len(T_ALL))], dtype=float)


def ogs_profiles(raw, var):
    m = np.load(raw / f"mesh_{var}_mesh.npz")
    nz = int(m["nz"])
    top = np.arange(len(m["xs"])) * (nz + 1)
    nmap = np.load(raw / f"mesh_{var}_exc_node_map.npy")
    E = np.load(raw / "ogs_M05_FIELD_A27_EXC.npz")
    R = np.load(raw / "ogs_M05_FIELD_A27_REF.npz")
    prof = []
    for t in T_ALL:
        key = "u_t0.000001" if t == 0 else f"u_t{t:.6f}"
        ur = R[key]
        full = ur.copy()
        full[nmap] = E[key]
        prof.append(-(full - ur)[top, 1] * 1e3)
    return np.array(prof, dtype=float)


def compare(gpu: np.ndarray, ref: np.ndarray, times) -> list:
    rows = []
    for k, t in enumerate(times):
        g, r = np.asarray(gpu[k]), np.asarray(ref[k])
        rows.append({"t_yr": t, "gpu_s_max_mm": float(g.max()), "ref_s_max_mm": float(r.max()),
                     "rel_diff_pct": float(100 * (g.max() - r.max()) / r.max()),
                     "profile_max_abs_diff_mm": float(np.abs(g - r).max()),
                     "profile_max_abs_diff_pct_of_smax": float(100 * np.abs(g - r).max() / r.max())})
    return rows


def step6_case(tab, gsolver: Path, case: str, kw, dev):
    scen, lam, variant = case.split("_")
    m = np.load(gsolver / "step10_slice/mesh.npz")
    nz = int(m["nz"])
    mesh = mesh_from_npz(m["nodes"], m["zone"], m["xs"], nz, device=dev)
    zone = m["zone"].reshape(len(m["xs"]) - 1, nz)
    E, nu, rho, A = zone_materials(tab, zone, scen, None, dev)
    lamf = nu / (1 - nu) if lam == "K0" else torch.ones_like(nu)
    sig0 = stress_from_lambda(column_vertical_stress(mesh, rho * G_ACC), lamf)
    mat = Materials(E, nu, rho, A, torch.ones_like(E))
    S = PlaneStrainCreepSolver(mesh, mat, sig0, **kw)
    st = S.initial_state()
    u1 = st.u[0, :, 0, :].cpu().numpy()
    Eeq = tab.materials["seam_Eeq_Pa"][scen][variant]
    cur = mat
    for zst in (11, 12, 13):
        cur = cur.clone()
        cur.E = torch.where(mesh.zone == zst, torch.full_like(cur.E, Eeq), cur.E)
        st = S.elastic_stage(st, cur, f"stage{zst}")
    g = -(st.u[0, :, 0, :].cpu().numpy() - u1)[:, 1] * 1e3
    d = gsolver / "step6_cases" / case
    top = np.arange(len(m["xs"])) * (nz + 1)
    l1 = np.loadtxt(d / "u_ls1.txt")
    l4 = np.loadtxt(d / "u_ls4.txt")
    r = -(l4 - l1)[top, 1] * 1e3
    return compare([g], [r], [0.0])[0], {"u_ls1.txt": sha(d / "u_ls1.txt"), "u_ls4.txt": sha(d / "u_ls4.txt")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", type=Path, required=True, help="extracted raw_creep_tables (…/G_creep/_raw_pack)")
    ap.add_argument("--transfer", type=Path, required=True)
    ap.add_argument("--gsolver", type=Path, required=True, help="G_solver dir (step3_materials.json, step10_slice, step6_cases)")
    ap.add_argument("--profiles", type=Path, required=True)
    ap.add_argument("--criteria", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--factor", default="fp32", choices=["fp32", "fp64"])
    ap.add_argument("--steps", nargs="*", default=["L1", "L2", "L3", "L4", "L5"])
    ap.add_argument("--ens-spec", default="seamA2")
    ap.add_argument("--ens-sched", default="0.02,1.3,1.0")
    a = ap.parse_args()
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    if dev == "cuda":
        torch.cuda.set_per_process_memory_fraction(min(1.0, 11.0e9 / torch.cuda.get_device_properties(0).total_memory))
    a.out.mkdir(parents=True, exist_ok=True)
    tab = Tables(a.gsolver / "step3_materials.json", a.transfer)
    kw = solver_kw(a)
    rec_path = a.out / "ladder_receipts_gpu.json"
    rec = json.loads(rec_path.read_text(encoding="utf-8")) if rec_path.exists() else {}
    rec.update({"schema": "vkm.gpu_ensemble.ladder/1", "env": env_info(), "criteria": {"file": a.criteria.name, "sha256": sha(a.criteria)},
                "solver_options": {"factor_dtype": a.factor, "factorization": "block cyclic reduction",
                                   "keep_factor_between_steps": True, "reuse_ratio": 0.4, "newton_tol_m": 1e-9},
                "inputs": {"step3_materials.json": sha(a.gsolver / "step3_materials.json"), "creep_transfer.json": sha(a.transfer),
                           "profiles.npz": sha(a.profiles)}})
    rec.setdefault("steps", {})

    def save():
        rec_path.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")

    if "L1" in a.steps:
        t0 = time.time()
        cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/world/test_gpu_plane_strain.py"]
        p = subprocess.run(cmd, cwd=HERE.parents[1], capture_output=True, text=True)
        tail = p.stdout.strip().splitlines()[-1] if p.stdout.strip() else p.stderr[-300:]
        rec["steps"]["L1_synthetic"] = {"command": "python -m pytest -q tests/world/test_gpu_plane_strain.py",
                                        "exit_code": p.returncode, "summary": tail, "time_s": round(time.time() - t0, 1),
                                        "status": "PASS" if p.returncode == 0 else "FAIL"}
        save()
        print("L1", rec["steps"]["L1_synthetic"], flush=True)

    ref_runs = {}
    if "L2" in a.steps or "L3" in a.steps or "L4" in a.steps:
        for var, scen, visc in CASES:
            tag = f"{scen}_{visc}_{var}"
            r = run_reference_case(tab, a.raw, var, scen, visc, SCHED_CONVERGED, kw, dev)
            ref_runs[tag] = r
            np.savez_compressed(a.out / f"gpu_refmesh_{tag}.npz", t=np.array(T_ALL), s_mm=np.array(r["s_mm"]),
                                conv_m=np.array(r["conv_m"]), background_mm=np.array(r["background_mm"]))
            print(tag, "done", round(r["time_s"], 1), "s", r["stats"], flush=True)

    if "L2" in a.steps:
        r = ref_runs["M05_FIELD_A27"]
        um = np.load(a.raw / "M05_FIELD_A27_EXC.npz")["u_full_ls1"].astype(float)
        dmax = float(np.abs(r["u1_full"] - um).max())
        rec["steps"]["L2_equilibrium_LS1"] = {"case": "M05_FIELD_A27 reference mesh A27 (70 500 nodes)",
                                              "max_abs_diff_m": dmax, "max_abs_u_m": float(np.abs(um).max()),
                                              "reference": "MAPDL 2026 R1 u_full_ls1 (raw_creep_tables)",
                                              "inputs": {"M05_FIELD_A27_EXC.npz": sha(a.raw / "M05_FIELD_A27_EXC.npz")},
                                              "status": "PASS" if dmax <= 1e-5 else "FAIL"}
        save()
        print("L2", rec["steps"]["L2_equilibrium_LS1"], flush=True)

    if "L3" in a.steps:
        res = {}
        ok = True
        for var in ("A27", "B20"):
            g = np.array(ref_runs[f"M05_FIELD_{var}"]["s_mm"])
            c = compare(g[:1], mapdl_profiles(a.raw, var, "M05", "FIELD")[:1], [0.0])[0]
            res[f"explicit_chambers_{var}_vs_MAPDL"] = c
            ok &= abs(c["rel_diff_pct"]) <= 0.5 and c["profile_max_abs_diff_pct_of_smax"] <= 1.0
        c = compare(np.array(ref_runs["M05_FIELD_A27"]["s_mm"])[:1], ogs_profiles(a.raw, "A27")[:1], [0.0])[0]
        res["explicit_chambers_A27_vs_OGS"] = c
        ok &= abs(c["rel_diff_pct"]) <= 0.5 and c["profile_max_abs_diff_pct_of_smax"] <= 1.0
        hashes = {}
        for case in ("M05_K0_A", "M05_L1_D", "NORM_K0_D"):
            c, h = step6_case(tab, a.gsolver, case, kw, dev)
            res[f"step6_{case}_vs_MAPDL"] = c
            hashes[case] = h
            ok &= abs(c["rel_diff_pct"]) <= 0.5 and c["profile_max_abs_diff_pct_of_smax"] <= 1.0
        rec["steps"]["L3_elastic"] = {"results": res, "inputs": hashes, "status": "PASS" if ok else "FAIL"}
        save()
        print("L3", json.dumps(rec["steps"]["L3_elastic"], ensure_ascii=False)[:2000], flush=True)

    if "L4" in a.steps:
        res, ok = {}, True
        idx = [T_ALL.index(t) for t in (10, 20, 40)]
        for tag, kind, label in (("M05_FIELD_A27", "_CRP01", "MAPDL_fine"), ("M05_LAB_B20", "_CRP01", "MAPDL_fine")):
            var = tag.split("_")[-1]
            visc = tag.split("_")[1]
            ref = mapdl_profiles(a.raw, var, "M05", visc, kind)
            c = compare(np.array(ref_runs[tag]["s_mm"]), ref, T_ALL)
            res[f"{tag}_vs_{label}"] = c
            ok &= all(abs(c[i]["rel_diff_pct"]) <= 1.0 and c[i]["profile_max_abs_diff_pct_of_smax"] <= 2.0 for i in idx)
        c = compare(np.array(ref_runs["M05_FIELD_A27"]["s_mm"]), ogs_profiles(a.raw, "A27"), T_ALL)
        res["M05_FIELD_A27_vs_OGS"] = c
        ok &= all(abs(c[i]["rel_diff_pct"]) <= 1.0 and c[i]["profile_max_abs_diff_pct_of_smax"] <= 2.0 for i in idx)
        for var, scen, visc in CASES:
            tag = f"{scen}_{visc}_{var}"
            c = compare(np.array(ref_runs[tag]["s_mm"]), mapdl_profiles(a.raw, var, scen, visc), T_ALL)
            res[f"{tag}_vs_MAPDL_main"] = c
            ok &= all(abs(c[i]["rel_diff_pct"]) <= 2.5 for i in idx)
        res["background_REF_surface_max_abs_mm_t40"] = {k: v["background_mm"][-1] for k, v in ref_runs.items()}
        res["gpu_runs"] = {k: {"n_steps": v["n_steps"], "stats": v["stats"], "time_s": round(v["time_s"], 1)} for k, v in ref_runs.items()}
        rec["steps"]["L4_creep"] = {"schedule": {"dt0": SCHED_CONVERGED[0], "growth": SCHED_CONVERGED[1], "dt_max": SCHED_CONVERGED[2]},
                                    "results": res, "inputs": {p.name: sha(p) for p in sorted(a.raw.glob("*.npz"))},
                                    "status": "PASS" if ok else "FAIL"}
        save()
        print("L4", rec["steps"]["L4_creep"]["status"], flush=True)

    if "L5" in a.steps:
        gate = all(rec["steps"].get(k, {}).get("status") == "PASS" for k in ("L1_synthetic", "L2_equilibrium_LS1", "L3_elastic", "L4_creep"))
        z = np.load(a.profiles)
        sec = json.loads((a.gsolver / "step10_slice/section.json").read_text(encoding="utf-8"))
        prof = {k: z[k] for k in ("chain", "dsm", "v62", "realizations")}
        prof["base_z"] = sec["model_choices"]["base_elevation_m"][0]
        import ensemble_design as ED
        spec = ED.MESH_SPECS[a.ens_spec]
        sched = tuple(float(v) for v in a.ens_sched.split(","))
        steps = time_steps(40.0, *sched, marks=ED.OUT_TIMES[1:])
        EC.SOLVER_KW = kw
        res = {}
        for var, L in (("A27", 27.0), ("B20", 20.0)):
            sel = [(s, v) for (vv, s, v) in CASES if vv == var]
            P = {k: [] for k in ("E_factor", "eta_sylv", "eta_salt", "eta_carn", "L_axis", "d_roof", "h_seam", "fill_on",
                                 "fill_frac", "E_fill", "k0", "geo", "norm")}
            for scen, visc in sel:
                P["E_factor"].append(0.5)
                P["norm"].append(1 if scen == "NORM" else 0)
                for k, lith in (("eta_sylv", "sylvinite"), ("eta_salt", "rock_salt"), ("eta_carn", "carnallite")):
                    P[k].append(tab.eta(visc, lith))
                P["L_axis"].append(L); P["d_roof"].append(10.0); P["h_seam"].append(5.5); P["fill_on"].append(0)
                P["fill_frac"].append(0.5); P["E_fill"].append(0.2e9); P["k0"].append(0); P["geo"].append(-1)
            t0 = time.time()
            out = EC.run_batch(tab, prof, P, spec, steps, ED.OUT_TIMES, device=dev, with_ref=True)
            for k, (scen, visc) in enumerate(sel):
                tag = f"{scen}_{visc}_{var}"
                gref = np.array(ref_runs[tag]["s_mm"]) if tag in ref_runs else np.load(a.out / f"gpu_refmesh_{tag}.npz")["s_mm"]
                xs_ref = np.load(a.raw / f"mesh_{var}_mesh.npz")["xs"]
                ti = [T_ALL.index(t) for t in ED.OUT_TIMES]
                ens = np.array([np.interp(xs_ref, out["xs"][k], out["s"][k, j] * 1e3) for j in range(len(ED.OUT_TIMES))])
                row = {"vs_gpu_reference_mesh_converged": compare(ens, gref[ti], ED.OUT_TIMES)}
                try:
                    row["vs_MAPDL_main"] = compare(ens, mapdl_profiles(a.raw, var, scen, visc)[ti], ED.OUT_TIMES)
                except FileNotFoundError:
                    pass
                if tag in ("M05_FIELD_A27", "M05_LAB_B20"):
                    row["vs_MAPDL_fine"] = compare(ens, mapdl_profiles(a.raw, var, "M05", visc, "_CRP01")[ti], ED.OUT_TIMES)
                if tag == "M05_FIELD_A27":
                    row["vs_OGS"] = compare(ens, ogs_profiles(a.raw, var)[ti], ED.OUT_TIMES)
                res[tag] = row
            res[f"batch_{var}"] = {"time_s": round(time.time() - t0, 1), "stats": out["stats"], "nx": out["nx"], "nz": out["nz"]}
        rec["steps"]["L5_representation"] = {"gate_L1_L4": "PASS" if gate else "FAIL", "mesh_spec": a.ens_spec,
                                             "mesh_spec_values": ED.spec_dict(spec), "schedule": a.ens_sched, "n_steps": len(steps),
                                             "results": res}
        save()
        print("L5 done", flush=True)


if __name__ == "__main__":
    main()

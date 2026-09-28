"""OGS quick checks S7 (toy plane-strain section) and S8 (small factorial of hypotheses).

VERIFICATION TOY: a derived 2D representation for a verification task (D-18). Not SKRU-1,
not a forecast, not calibrated, not validated; results are MODEL results, never observations.

Usage:  python run_toy.py S7        (base case + numerical checks)
        python run_toy.py S8        (4 lambda x 3 creep x 3 backfill + 12 no-mining backgrounds)
        python run_toy.py S8post    (post-processing only)
"""
from __future__ import annotations

import json
import math
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

import oqc_common as C
import oqc_toy as T

OUT_TIMES_YR = [1.0, 2.0, 5.0, 10.0, 11.0, 15.0, 20.0, 30.0, 40.0, 50.0]
T_FIRST = 1.0  # s, instantaneous (elastic) excavation response
FIXED_TIMES = [T_FIRST] + [t * C.YEAR for t in OUT_TIMES_YR]
N_WORKERS = 1  # one OGS run at a time on the shared workstation


# ----------------------------------------------------------------------------- prj
def toy_prj(case: str, mesh: str, gml: str, lam: float, creep_f: float, backfill: str, with_rooms: bool,
            io: int = 3, max_dt: float = 0.5 * C.YEAR, t_end: float = T.T_END,
            outvars: tuple[str, ...] = ("displacement", "sigma")) -> str:
    params = [C.xml_param_constant("zero", 0.0), C.xml_param_constant("u0", [0.0, 0.0]),
              C.xml_param_constant("s0", 1e6), C.xml_param_constant("Q", 0.0),
              C.xml_param_constant("T_ref", T.T_REF)]
    for lith in ("marl", "rock_salt", "sylvinite"):
        params += [C.xml_param_constant(f"E_{lith}", T.E[lith]), C.xml_param_constant(f"nu_{lith}", T.NU[lith])]
    for lith in ("rock_salt", "sylvinite"):
        params += [C.xml_param_constant(f"A_{lith}", creep_f * T.CREEP_K[lith] / 86400.0),
                   C.xml_param_constant(f"n_{lith}", T.CREEP_N[lith])]
    params.append(C.xml_param_function("sigma0", T.initial_stress_expressions(lam, with_rooms=with_rooms)))
    cons = [C.xml_linear_elastic(T.M_OB, "E_marl", "nu_marl")]
    for mid in (T.M_SALT_UP, T.M_KP, T.M_PDKS):
        cons.append(C.xml_creep_bgra(mid, "E_rock_salt", "nu_rock_salt", "A_rock_salt", "n_rock_salt", "s0", "Q"))
    cons.append(C.xml_creep_bgra(T.M_SYLV, "E_sylvinite", "nu_sylvinite", "A_sylvinite", "n_sylvinite", "s0", "Q"))
    dens = {T.M_OB: T.RHO["marl"], T.M_SALT_UP: T.RHO["rock_salt"], T.M_KP: T.RHO["rock_salt"],
            T.M_SYLV: T.RHO["sylvinite"], T.M_PDKS: T.RHO["rock_salt"]}
    if with_rooms:
        bf = T.BACKFILL[backfill]
        tsw = T.T_BACKFILL + 1.0
        if bf is None:
            params += [C.xml_param_constant("E_room", T.E_VOID), C.xml_param_constant("nu_room", T.NU_VOID)]
        else:
            params += [C.xml_param_function("E_room", [f"if(t < {tsw!r}, {T.E_VOID!r}, {bf['E']!r})"]),
                       C.xml_param_function("nu_room", [f"if(t < {tsw!r}, {T.NU_VOID!r}, {bf['nu']!r})"])]
        cons.append(C.xml_linear_elastic(T.M_ROOM, "E_room", "nu_room"))
        dens[T.M_ROOM] = 0.0
    bcs = [C.xml_bc("toy", "LEFT", "Dirichlet", 0, "zero"), C.xml_bc("toy", "RIGHT", "Dirichlet", 0, "zero"),
           C.xml_bc("toy", "BOTTOM", "Dirichlet", 1, "zero")]
    ts = C.xml_iter_timestepping(0.0, t_end, T_FIRST, 1e-3, max_dt, iters=(2, 4, 6, 10, 15),
                                 mults=(5.0, 2.0, 1.2, 0.8, 0.5))
    fixed = [t for t in FIXED_TIMES if t <= t_end + 1e-6]
    out = C.xml_output(case, list(outvars), fixed_times=fixed)
    return C.build_prj(mesh=mesh, geometry=gml, dim=2, integration_order=io, constitutive=cons,
                       body_force=[0.0, -C.G], parameters=params, densities=dens, pv_order=2, ic_param="u0",
                       bcs=bcs, time_stepping=ts, output=out, initial_stress="sigma0",
                       reference_temperature="T_ref", max_iter=25, conv_abstol=1e-7, conv_reltol=1e-9,
                       secondary=("sigma", "epsilon") if "sigma" in outvars else ())


def write_meshes(wd: Path, refine: int = 1) -> dict:
    tag = "" if refine == 1 else f"_r{refine}"
    shas = {}
    for with_rooms in (True, False):
        pts, conn, mids, xs, ys = T.toy_mesh(with_rooms=with_rooms, refine=refine)
        name = f"toy{'_rooms' if with_rooms else '_norooms'}{tag}.vtu"
        shas[name] = C.write_mesh(wd / name, pts, "quad8", conn, mids)
        shas[f"_{name}_counts"] = {"elements": int(len(conn)), "nodes": int(len(pts))}
    shas["toy.gml"] = C.rect_gml_2d(wd / "toy.gml", "toy", 0.0, T.X_RIGHT, T.Y_BOTTOM, 0.0)
    return shas


def job(args):
    prj, outdir, log = args
    return C.run_ogs(Path(prj), Path(outdir), Path(log), timeout=3 * 3600)


# ----------------------------------------------------------------------------- extraction
def extract(outdir: Path, prefix: str, with_rooms: bool = True) -> dict:
    """Surface profiles u_y(x,t) and seam quantities at the fixed output times."""
    items = C.read_pvd(outdir / f"{prefix}.pvd")
    res = {"t_s": [], "x_surf": None, "uy_surf": [], "uy_roof_x": None, "conv_room0_m": [],
           "pillar1_strain": [], "pillar1_sigma_yy_mean_Pa": [], "seam_closure_area_m2": [],
           "trough_area_m2": [], "pillar1_sigma_yy_centre_Pa": []}
    for t, f in items:
        if t <= 0.0:
            continue
        m = C.read_vtu(f)
        x, y = m.points[:, 0], m.points[:, 1]
        u = m.point_data["displacement"]
        sig = m.point_data.get("sigma", np.full((len(x), 4), np.nan))
        top = np.isclose(y, 0.0)
        o = np.argsort(x[top])
        xs, uy = x[top][o], u[top, 1][o]
        roof = np.isclose(y, T.Y_ROOF)
        floor = np.isclose(y, T.Y_FLOOR)
        xr = x[roof]
        orr = np.argsort(xr)
        xf = x[floor]
        off = np.argsort(xf)
        clos = u[roof, 1][orr] - u[floor, 1][off]  # negative = closure (roof down / floor up)
        if res["x_surf"] is None:
            res["x_surf"] = xs.tolist()
            res["uy_roof_x"] = xr[orr].tolist()
        res["t_s"].append(t)
        res["uy_surf"].append(uy.tolist())
        c0 = clos[np.isclose(xr[orr], 0.0)][0]
        res["conv_room0_m"].append(float(-c0))
        pc = 8.0 + T.PILLAR_W / 2.0
        p1 = float(clos[np.isclose(xr[orr], pc)][0])
        res["pillar1_strain"].append(-p1 / T.SEAM_H)
        mid = np.isclose(y, 0.5 * (T.Y_ROOF + T.Y_FLOOR))
        inside = mid & (x > 8.0 + 1e-6) & (x < 8.0 + T.PILLAR_W - 1e-6)
        res["pillar1_sigma_yy_mean_Pa"].append(float(sig[inside, 1].mean()))
        cen = mid & np.isclose(x, pc)
        res["pillar1_sigma_yy_centre_Pa"].append(float(sig[cen, 1].mean()))
        res["seam_closure_area_m2"].append(float(-np.trapezoid(clos, xr[orr])))
        res["trough_area_m2"].append(float(-np.trapezoid(uy, xs)))
    return res


def trough_metrics(x: np.ndarray, s: np.ndarray) -> dict:
    """s >= 0 subsidence profile on x >= 0 (symmetric half)."""
    x = np.asarray(x, float)
    s = np.asarray(s, float)
    smax = float(s.max())
    out = {"s_max_m": smax, "x_at_s_max_m": float(x[np.argmax(s)])}
    if smax <= 0:
        return out

    def cross(frac):
        lev = frac * smax
        i0 = int(np.argmax(s))
        for i in range(i0, len(s) - 1):
            if s[i] >= lev > s[i + 1]:
                return float(x[i] + (lev - s[i]) * (x[i + 1] - x[i]) / (s[i + 1] - s[i]))
        return float("nan")

    slope = np.gradient(s, x)
    k = int(np.argmax(np.abs(slope)))
    out.update(x50_m=cross(0.5), x10_m=cross(0.1), x_inflection_m=float(x[k]),
               max_tilt_mm_per_m=float(abs(slope[k]) * 1e3))
    depth = -0.5 * (T.Y_ROOF + T.Y_FLOOR)
    if not math.isnan(out["x10_m"]):
        out["angle_x10_from_panel_edge_deg"] = float(math.degrees(math.atan2(out["x10_m"] - T.X_PANEL_EDGE, depth)))
    return out


def mining_induced(r_mine: dict, r_bg: dict | None) -> list[np.ndarray]:
    prof = []
    for k in range(len(r_mine["t_s"])):
        uy = np.array(r_mine["uy_surf"][k])
        if r_bg is not None:
            uy = uy - np.array(r_bg["uy_surf"][k])
        prof.append(-uy)
    return prof


# ============================================================================= S7
S7_CASE = (1.0, 1.0, "none")  # lambda, creep factor, backfill of the S7 base case


def s7_variants() -> dict:
    return {
        "base": dict(mesh="toy_rooms.vtu", io=3, max_dt=0.5 * C.YEAR, t_end=T.T_END),
        # 2x2 reduced integration of QUAD8 (locking check); nodal stress extrapolation is impossible with
        # 4 integration points for 8 nodes, so this variant writes displacement only
        "io2_reduced_integration": dict(mesh="toy_rooms.vtu", io=2, max_dt=0.5 * C.YEAR, t_end=T.T_END,
                                        outvars=("displacement",)),
        # time-step check: maximum step 0.125 yr instead of 0.5 yr, to 10 years (cost); compared at 10 years
        "dt_max_quarter_to10yr": dict(mesh="toy_rooms.vtu", io=3, max_dt=0.125 * C.YEAR, t_end=10.0 * C.YEAR),
        # refined mesh (x2 in both directions) only to 10 years (cost); compared at 10 years
        "mesh_refined_x2_to10yr": dict(mesh="toy_rooms_r2.vtu", io=3, max_dt=0.5 * C.YEAR, t_end=10.0 * C.YEAR),
    }


def s7_one(name: str) -> dict:
    """Run a single S7 variant (identical input text to s7()), used to add a variant without re-running others."""
    wd = C.RUN_ROOT / "S7"
    v = s7_variants()[name]
    lam, cf, bf = S7_CASE
    case = f"s7_{name}"
    txt = toy_prj(case, v["mesh"], "toy.gml", lam, cf, bf, True, io=v["io"], max_dt=v["max_dt"],
                  t_end=v["t_end"], outvars=v.get("outvars", ("displacement", "sigma")))
    p = wd / f"S7_{name}.prj"
    p.write_text(txt)
    C.copy_prj_to_repo(p, "S7")
    r = C.run_ogs(p, wd / f"out_{case}", wd / f"ogs_{case}.log")
    print(r)
    return r


def s7(run: bool = True) -> dict:
    """run=False: post-process only; variants that are still running are reported as pending."""
    wd = C.RUN_ROOT / "S7"
    wd.mkdir(parents=True, exist_ok=True)
    shas = write_meshes(wd, 1)
    shas.update(write_meshes(wd, 2))
    lam, cf, bf = S7_CASE
    variants = s7_variants()
    jobs, prjs = [], {}
    for name, v in variants.items():
        case = f"s7_{name}"
        txt = toy_prj(case, v["mesh"], "toy.gml", lam, cf, bf, True, io=v["io"], max_dt=v["max_dt"],
                      t_end=v["t_end"], outvars=v.get("outvars", ("displacement", "sigma")))
        p = wd / f"S7_{name}.prj"
        log = wd / f"ogs_{case}.log"
        if p.exists() and p.read_text() == txt and log.exists() and "OGS completed" in log.read_text(errors="replace"):
            prjs[name] = p  # identical input already run to completion: reuse (re-run of the post-processing)
            jobs.append(None)
            continue
        if not run:
            prjs[name] = p
            jobs.append("pending")
            continue
        p.write_text(txt)
        C.copy_prj_to_repo(p, "S7")
        prjs[name] = p
        jobs.append((str(p), str(wd / f"out_{case}"), str(log)))
    todo = [j for j in jobs if j not in (None, "pending")]
    done = iter([job(j) for j in todo])  # sequential: one OGS run at a time
    runs = []
    for (name, v), j in zip(variants.items(), jobs):
        if j == "pending":
            runs.append({"exit_code": None, "note": "still running when this receipt was written"})
        elif j is not None:
            runs.append(next(done))
        else:
            case = f"s7_{name}"
            log = wd / f"ogs_{case}.log"
            txtlog = log.read_text(errors="replace")
            import re as _re  # noqa: PLC0415
            mt = _re.findall(r"Execution took ([0-9.eE+-]+) s", txtlog)
            runs.append({"command": C.logical(f"{C.OGS_BIN} -o {wd / ('out_' + case)} {prjs[name]}"),
                         "exit_code": 0, "wall_time_s": float(mt[-1]) if mt else None,
                         "log": C.logical(str(log)), "log_sha256": C.sha256_file(log), "reused_completed_run": True})
    rec = C.receipt_base("S7", "Toy plane-strain section: overburden, salt series, a panel of 13 rooms in KrII; "
                               "surface subsidence vs time (base case) and numerical sensitivity checks")
    rec["case"] = {"lambda": lam, "creep_factor": cf, "backfill": bf}
    rec["meshes_sha256"] = {k: v for k, v in shas.items() if not k.startswith("_")}
    rec["mesh_counts"] = {k.strip("_").replace("_counts", ""): v for k, v in shas.items() if k.startswith("_")}
    rec["parameter_table"] = T.parameter_table()
    rec["runs"] = {}
    results = {}
    for (name, v), r in zip(variants.items(), runs):
        r["prj_sha256"] = C.sha256_file(prjs[name]) if prjs[name].exists() else None
        rec["runs"][name] = {**v, "run": r}
        if r["exit_code"] == 0:
            results[name] = extract(wd / f"out_s7_{name}", f"s7_{name}")
    json.dump(results, open(wd / "s7_results.json", "w"))
    if "base" not in results:
        rec["pass"] = False
        C.write_json(C.RECEIPTS / "S7.json", rec)
        print("S7 base failed")
        return rec
    b = results["base"]
    x = np.array(b["x_surf"])
    prof = mining_induced(b, None)
    metrics = {f"{t / C.YEAR:.6g}yr" if t > 10 else "0+ (1 s)": trough_metrics(x, s) for t, s in zip(b["t_s"], prof)}
    sv_seam = float(-T.sigma_v(np.array([0.5 * (T.Y_ROOF + T.Y_FLOOR)]))[0])
    trib = sv_seam * T.PITCH / T.PILLAR_W
    checks = {
        "pillar1_mean_sigma_yy_t0_Pa": b["pillar1_sigma_yy_mean_Pa"][0],
        "tributary_area_estimate_Pa": -trib,
        "ratio_fe_to_tributary_t0": abs(b["pillar1_sigma_yy_mean_Pa"][0]) / trib,
        "volume_ratio_trough_to_seam_closure_50yr": b["trough_area_m2"][-1] / b["seam_closure_area_m2"][-1],
        "surface_uy_at_right_boundary_over_s_max_50yr": float(abs(b["uy_surf"][-1][-1]) / max(prof[-1])),
    }
    sens = {}
    tb = [t / C.YEAR for t in b["t_s"]]
    for name in ("io2_reduced_integration", "dt_max_quarter_to10yr", "mesh_refined_x2_to10yr"):
        if name in results:
            r = results[name]
            t_cmp = r["t_s"][-1] / C.YEAR
            kb = int(np.argmin(np.abs(np.array(tb) - t_cmp)))
            s_alt = -np.array(r["uy_surf"][-1])
            x_alt = np.array(r["x_surf"])
            s_b = prof[kb]
            sens[name] = {
                "compared_at_yr": t_cmp,
                "s_max_alt_m": float(s_alt.max()), "s_max_base_m": float(s_b.max()),
                "rel_diff_s_max_vs_base": float((s_alt.max() - s_b.max()) / s_b.max()),
                "rel_diff_x50_vs_base": float((trough_metrics(x_alt, s_alt)["x50_m"] - trough_metrics(x, s_b)["x50_m"]) /
                                              trough_metrics(x, s_b)["x50_m"]),
                "conv_room0_alt_m": r["conv_room0_m"][-1], "conv_room0_base_m": b["conv_room0_m"][kb],
            }
        else:
            sens[name] = {"pending": True} if rec["runs"][name]["run"].get("exit_code") is None else {"failed": True}
    rec["checks"] = checks
    rec["trough_metrics_base"] = metrics
    rec["time_series_base"] = {
        "t_yr": [t / C.YEAR for t in b["t_s"]], "s_max_m": [float(p.max()) for p in prof],
        "conv_room0_m": b["conv_room0_m"], "pillar1_strain": b["pillar1_strain"],
        "pillar1_sigma_yy_mean_Pa": b["pillar1_sigma_yy_mean_Pa"],
    }
    rec["numerical_sensitivity"] = sens
    keep = {}
    for k, (t, pr) in enumerate(zip(b["t_s"], prof)):
        ty = t / C.YEAR
        if k == 0 or any(abs(ty - w) < 1e-6 for w in (1.0, 5.0, 10.0, 20.0, 50.0)):
            keep["0+ (1 s)" if k == 0 else f"{ty:g} yr"] = {"x_m": x.tolist(), "s_m": [float(v) for v in pr]}
    rec["_profiles_base"] = keep
    tol = 0.05
    rec["numerical_tolerance_rel"] = tol
    done_sens = {k: v for k, v in sens.items() if not v.get("pending")}
    rec["pending_variants"] = [k for k, v in sens.items() if v.get("pending")]
    rec["pass"] = bool(all(not v.get("failed") and abs(v["rel_diff_s_max_vs_base"]) < tol for v in done_sens.values())
                       and checks["surface_uy_at_right_boundary_over_s_max_50yr"] < 0.05)
    C.write_json(C.RECEIPTS / "S7.json", rec)
    print(json.dumps({"checks": checks, "sens": sens, "s_max": rec["time_series_base"]["s_max_m"]}, indent=1))
    return rec


# ============================================================================= S8
def s8_cases():
    cases = []
    for lam in sorted(T.LAMBDAS):
        for cf in T.CREEP_FACTORS:
            for bf in ("none", "soft", "stiff"):
                cases.append(dict(name=f"L{lam:g}_C{cf:g}_B{bf}", lam=lam, cf=cf, bf=bf, rooms=True))
            cases.append(dict(name=f"L{lam:g}_C{cf:g}_background", lam=lam, cf=cf, bf="none", rooms=False))
    return cases


def s8(run: bool = True) -> dict:
    wd = C.RUN_ROOT / "S8"
    wd.mkdir(parents=True, exist_ok=True)
    shas = write_meshes(wd, 1)
    cases = s8_cases()
    jobs, prj_sha = [], {}
    for c in cases:
        txt = toy_prj(c["name"], "toy_rooms.vtu" if c["rooms"] else "toy_norooms.vtu", "toy.gml",
                      c["lam"], c["cf"], c["bf"], c["rooms"])
        p = wd / f"S8_{c['name']}.prj"
        p.write_text(txt)
        C.copy_prj_to_repo(p, "S8")
        prj_sha[c["name"]] = C.sha256_file(p)
        jobs.append((str(p), str(wd / f"out_{c['name']}"), str(wd / f"ogs_{c['name']}.log")))
    runs = {}
    if run:
        for c, j in zip(cases, jobs):  # sequential: one OGS run at a time
            r = job(j)
            runs[c["name"]] = r
            print(f"S8 {c['name']}: exit={r['exit_code']} wall={r['wall_time_s']}s", flush=True)
        json.dump(runs, open(wd / "s8_runs.json", "w"))
    else:
        runs = json.load(open(wd / "s8_runs.json"))
    return s8_post(wd, cases, runs, prj_sha, shas)


def s8_post(wd: Path, cases, runs, prj_sha, shas) -> dict:
    data = {}
    for c in cases:
        if runs.get(c["name"], {}).get("exit_code") == 0:
            try:
                data[c["name"]] = extract(wd / f"out_{c['name']}", c["name"])
            except Exception as e:  # noqa: BLE001
                data[c["name"]] = {"error": repr(e)}
    json.dump(data, open(wd / "s8_extracted.json", "w"))
    table = []
    for c in cases:
        if not c["rooms"]:
            continue
        row = {"case": c["name"], "lambda": c["lam"], "creep_factor": c["cf"], "backfill": c["bf"],
               "exit_code": runs.get(c["name"], {}).get("exit_code"),
               "wall_time_s": runs.get(c["name"], {}).get("wall_time_s"), "prj_sha256": prj_sha[c["name"]]}
        bgname = f"L{c['lam']:g}_C{c['cf']:g}_background"
        d, bg = data.get(c["name"]), data.get(bgname)
        if d is None or "error" in d or bg is None or "error" in bg:
            row["status"] = "FAILED_OR_MISSING"
            table.append(row)
            continue
        x = np.array(d["x_surf"])
        prof = mining_induced(d, bg)
        tyr = [t / C.YEAR for t in d["t_s"]]
        row["t_yr"] = tyr
        row["s_max_series_m"] = [float(p.max()) for p in prof]
        row["background_s_max_series_m"] = [float(-np.array(u).min()) for u in bg["uy_surf"]]
        row["total_s_max_series_m"] = [float(-np.array(u).min()) for u in d["uy_surf"]]
        row["conv_room0_series_m"] = d["conv_room0_m"]
        row["pillar1_strain_series"] = d["pillar1_strain"]
        for tag, k in (("0+", 0), ("10yr", tyr.index(10.0) + 1 if 10.0 in tyr else None),
                       ("50yr", len(tyr) - 1)):
            pass
        idx = {"0+": 0, "10yr": int(np.argmin(np.abs(np.array(tyr) - 10.0))),
               "50yr": int(np.argmin(np.abs(np.array(tyr) - 50.0)))}
        for tag, k in idx.items():
            m = trough_metrics(x, prof[k])
            for kk, vv in m.items():
                row[f"{kk}@{tag}"] = vv
        row["pillar1_strain@50yr"] = d["pillar1_strain"][idx["50yr"]]
        row["conv_room0_m@50yr"] = d["conv_room0_m"][idx["50yr"]]
        row["validity_flags"] = []
        if row["pillar1_strain@50yr"] > 0.05:
            row["validity_flags"].append("pillar strain > 5 % (critical ~5 %, N28): tertiary creep/failure not modelled")
        if row["conv_room0_m@50yr"] > 0.5 * T.SEAM_H:
            row["validity_flags"].append("room closure > 50 % of height: small-strain/no-contact model invalid")
        row["status"] = "OK" if not row["validity_flags"] else "OUTSIDE_VALIDITY"
        table.append(row)
    sens = sensitivity(table)
    rec = C.receipt_base("S8", "Hypothesis matrix on the toy: lambda x creep-rate scenario x backfill scenario")
    rec["design"] = {"lambda": {str(k): v for k, v in T.LAMBDAS.items()}, "creep_factor_on_CL15": T.CREEP_FACTORS,
                     "backfill": {k: v for k, v in T.BACKFILL.items()}, "t_backfill_yr": T.T_BACKFILL / C.YEAR,
                     "t_end_yr": T.T_END / C.YEAR,
                     "mining_induced_definition": "s = -(u_y(mined run) - u_y(no-mining background run, same lambda "
                                                  "and creep)) at the surface; background removes creep driven by "
                                                  "the non-hydrostatic initial stress itself"}
    rec["meshes_sha256"] = {k: v for k, v in shas.items() if not k.startswith("_")}
    rec["background_runs"] = {c["name"]: {"exit_code": runs.get(c["name"], {}).get("exit_code"),
                                          "prj_sha256": prj_sha[c["name"]],
                                          "s_max_50yr_m": (float(-np.array(data[c["name"]]["uy_surf"][-1]).min())
                                                           if c["name"] in data and "uy_surf" in data[c["name"]] else None)}
                              for c in cases if not c["rooms"]}
    rec["runs"] = {c["name"]: runs.get(c["name"]) for c in cases}
    rec["table"] = table
    rec["sensitivity"] = sens
    rec["pass"] = bool(all(r.get("status") in ("OK", "OUTSIDE_VALIDITY") for r in table))
    C.write_json(C.RECEIPTS / "S8.json", rec)
    write_csv(table)
    print(json.dumps(sens, indent=1))
    return rec


# ----------------------------------------------------------------------------- S8 with dossier scenarios
FULL_FACTORIAL_SCENARIOS = ["IS-SC-A", "IS-SC-B/0.6", "IS-SC-B/0.8", "IS-SC-D", "IS-SC-E", "NORMATIVE-0.45"]
# IS-P-21 (0.71) lies inside IS-SC-B; after the dossier (IS-SC) replaced the catalogue list it is not run.
PRIORITY = ["IS-SC-A", "IS-SC-B/0.6", "IS-SC-E", "IS-SC-B/0.8", "IS-SC-D", "NORMATIVE-0.45",
            "IS-SC-A/OB0.7", "IS-SC-C"]
ENSEMBLE = ["IS-SC-A", "IS-SC-B/0.6", "IS-SC-B/0.8", "IS-SC-D", "IS-SC-E"]
SINGLE_CHECK_SCENARIOS = ["IS-SC-A/OB0.7", "IS-SC-C"]  # creep x1, no backfill only


def all_cases():
    cases = []
    for sid, (spec, prefix, label, role) in T.STRESS_SCENARIOS.items():
        if sid not in FULL_FACTORIAL_SCENARIOS and sid not in SINGLE_CHECK_SCENARIOS:
            continue
        if sid in FULL_FACTORIAL_SCENARIOS:
            combos = [(cf, bf) for cf in T.CREEP_FACTORS for bf in ("none", "soft", "stiff")]
            bgs = list(T.CREEP_FACTORS)
        else:
            combos = [(1.0, "none")]
            bgs = [1.0]
        for cf, bf in combos:
            cases.append(dict(name=f"{prefix}_C{cf:g}_B{bf}", sid=sid, spec=spec, cf=cf, bf=bf, rooms=True))
        for cf in bgs:
            cases.append(dict(name=f"{prefix}_C{cf:g}_background", sid=sid, spec=spec, cf=cf, bf="none", rooms=False))
    return cases


def s8b(run: bool = True, workers: int = 8) -> None:
    """Run the dossier scenarios that the first S8 batch (uniform lambda 0.45/0.6/0.71/1.0) did not cover."""
    wd = C.RUN_ROOT / "S8"
    todo = []
    first_batch = {"IS-SC-A", "IS-SC-B/0.6", "NORMATIVE-0.45", "IS-P-21-0.71"}  # uniform lambda, run by s8()
    for c in all_cases():
        log = wd / f"ogs_{c['name']}.log"
        if c["sid"] in first_batch or log.exists():
            continue  # run by the first batch (or already done)
        txt = toy_prj(c["name"], "toy_rooms.vtu" if c["rooms"] else "toy_norooms.vtu", "toy.gml",
                      c["spec"], c["cf"], c["bf"], c["rooms"])
        p = wd / f"S8_{c['name']}.prj"
        p.write_text(txt)
        C.copy_prj_to_repo(p, "S8")
        todo.append((str(p), str(wd / f"out_{c['name']}"), str(log)))
    print(f"S8b: {len(todo)} new runs", flush=True)
    if run and todo:
        for j in todo:  # sequential: one OGS run at a time
            r = job(j)
            print(f"S8b {Path(j[0]).stem}: exit={r['exit_code']} wall={r['wall_time_s']}s", flush=True)


def runall(workers: int = 1) -> None:
    """Prioritised restart of all S7 variants and S8 cases whose log does not say 'OGS completed'
    (used after the runs were killed by an out-of-memory event of the shared workstation)."""
    todo = []
    wd7 = C.RUN_ROOT / "S7"
    lam, cf, bf = S7_CASE
    for name, v in s7_variants().items():
        if name == "base":
            continue
        log = wd7 / f"ogs_s7_{name}.log"
        if log.exists() and "OGS completed" in log.read_text(errors="replace"):
            continue
        p = wd7 / f"S7_{name}.prj"
        p.write_text(toy_prj(f"s7_{name}", v["mesh"], "toy.gml", lam, cf, bf, True, io=v["io"], max_dt=v["max_dt"],
                             t_end=v["t_end"], outvars=v.get("outvars", ("displacement", "sigma"))))
        C.copy_prj_to_repo(p, "S7")
        todo.append((str(p), str(wd7 / f"out_s7_{name}"), str(log)))
    wd = C.RUN_ROOT / "S8"
    cases = sorted(all_cases(), key=lambda c: (PRIORITY.index(c["sid"]), 0 if c["rooms"] else 1))
    for c in cases:
        log = wd / f"ogs_{c['name']}.log"
        if log.exists() and "OGS completed" in log.read_text(errors="replace"):
            continue
        p = wd / f"S8_{c['name']}.prj"
        txt = toy_prj(c["name"], "toy_rooms.vtu" if c["rooms"] else "toy_norooms.vtu", "toy.gml",
                      c["spec"], c["cf"], c["bf"], c["rooms"])
        if not p.exists() or p.read_text() != txt:
            p.write_text(txt)
        C.copy_prj_to_repo(p, "S8")
        todo.append((str(p), str(wd / f"out_{c['name']}"), str(log)))
    # strictly sequential: one OGS run at a time (shared workstation; the parallel batch of 28.09 exhausted RAM)
    print(f"runall: {len(todo)} runs, sequential", flush=True)
    for j in todo:
        r = job(j)
        print(f"{Path(j[0]).stem}: exit={r['exit_code']} wall={r['wall_time_s']}s", flush=True)
    print("runall done", flush=True)


def s8_all_post() -> dict:
    """Post-process all S8 runs (both batches) into the receipt, the CSV matrix and the sensitivity."""
    import re as _re  # noqa: PLC0415

    wd = C.RUN_ROOT / "S8"
    cases = all_cases()
    shas = {n: C.sha256_file(wd / n) for n in ("toy_rooms.vtu", "toy_norooms.vtu", "toy.gml")}
    runs, prj_sha, data = {}, {}, {}
    for c in cases:
        log = wd / f"ogs_{c['name']}.log"
        prj = wd / f"S8_{c['name']}.prj"
        if not log.exists():
            runs[c["name"]] = {"exit_code": None, "note": "not run"}
            continue
        txt = log.read_text(errors="replace")
        ok = "OGS completed" in txt
        mt = _re.findall(r"Execution took ([0-9.eE+-]+) s", txt)
        outdir = wd / ("out_" + c["name"])
        runs[c["name"]] = {"command": C.logical(f"{C.OGS_BIN} -o {outdir} {prj}"),
                           "exit_code": 0 if ok else 1, "wall_time_s": float(mt[-1]) if mt else None,
                           "log": C.logical(str(log)), "log_sha256": C.sha256_file(log)}
        prj_sha[c["name"]] = C.sha256_file(prj) if prj.exists() else None
        if ok:
            try:
                data[c["name"]] = extract(outdir, c["name"])
            except Exception as e:  # noqa: BLE001
                data[c["name"]] = {"error": repr(e)}
    json.dump(data, open(wd / "s8_extracted_all.json", "w"))
    table = []
    for c in cases:
        if not c["rooms"]:
            continue
        spec, prefix, label, role = T.STRESS_SCENARIOS[c["sid"]]
        row = {"case": c["name"], "scenario": c["sid"], "scenario_role": role,
               "salt_lambda_in_plane": spec["salt"][0], "salt_lambda_out_of_plane": spec["salt"][1],
               "ob_lambda_in_plane": spec["ob"][0], "creep_factor": c["cf"], "backfill": c["bf"],
               "exit_code": runs[c["name"]].get("exit_code"), "wall_time_s": runs[c["name"]].get("wall_time_s"),
               "prj_sha256": prj_sha.get(c["name"])}
        bgname = f"{prefix}_C{c['cf']:g}_background"
        d, bg = data.get(c["name"]), data.get(bgname)
        if d is None or "error" in d or bg is None or "error" in bg:
            row["status"] = "FAILED_OR_MISSING"
            table.append(row)
            continue
        x = np.array(d["x_surf"])
        prof = mining_induced(d, bg)
        tyr = [t / C.YEAR for t in d["t_s"]]
        row.update(t_yr=tyr, s_max_series_m=[float(pp.max()) for pp in prof],
                   background_s_max_series_m=[float(-np.array(u).min()) for u in bg["uy_surf"]],
                   background_uplift_max_series_m=[float(np.array(u).max()) for u in bg["uy_surf"]],
                   total_s_max_series_m=[float(-np.array(u).min()) for u in d["uy_surf"]],
                   conv_room0_series_m=d["conv_room0_m"], pillar1_strain_series=d["pillar1_strain"],
                   pillar1_sigma_yy_mean_series_Pa=d["pillar1_sigma_yy_mean_Pa"])
        idx = {"0+": 0, "10yr": int(np.argmin(np.abs(np.array(tyr) - 10.0))),
               "50yr": int(np.argmin(np.abs(np.array(tyr) - 50.0)))}
        for tag, k in idx.items():
            for kk, vv in trough_metrics(x, prof[k]).items():
                row[f"{kk}@{tag}"] = vv
        row["profile_50yr_x_m"] = x.tolist()
        row["profile_50yr_s_m"] = prof[idx["50yr"]].tolist()
        row["pillar1_strain@50yr"] = d["pillar1_strain"][idx["50yr"]]
        row["conv_room0_m@50yr"] = d["conv_room0_m"][idx["50yr"]]
        row["validity_flags"] = []
        if row["pillar1_strain@50yr"] > 0.05:
            row["validity_flags"].append("pillar strain > 5 % (critical ~5 %, N28): tertiary creep/failure not modelled")
        if row["conv_room0_m@50yr"] > 0.5 * T.SEAM_H:
            row["validity_flags"].append("room closure > 50 % of height: small-strain/no-contact model invalid")
        row["status"] = "OK" if not row["validity_flags"] else "OUTSIDE_VALIDITY"
        table.append(row)
    ens = [r for r in table if r["scenario"] in ENSEMBLE]
    sens = sensitivity(ens, factors=("scenario", "creep_factor", "backfill"))
    rec = C.receipt_base("S8", "Hypothesis matrix on the toy: initial-stress scenario (IS-SC) x creep-rate scenario "
                               "x backfill scenario")
    rec["design"] = {
        "stress_scenarios": {sid: {"spec": v[0], "label": v[2], "role": v[3]} for sid, v in T.STRESS_SCENARIOS.items()},
        "stress_scenarios_source": "docs/science/topic_dossiers/INITIAL_STRESS_RU.md section 9, table IS-SC "
                                   "(coordinator commit 792cd69); scenarios are not averaged",
        "ensemble_for_sensitivity": ENSEMBLE,
        "creep_factor_on_CL15": T.CREEP_FACTORS, "backfill": T.BACKFILL,
        "t_backfill_yr": T.T_BACKFILL / C.YEAR, "t_end_yr": T.T_END / C.YEAR,
        "mining_induced_definition": "s = -(u_y(mined run) - u_y(no-mining background run, same stress scenario and "
                                     "creep)) at the surface; the background removes deformation driven by the "
                                     "non-hydrostatic initial stress itself (relaxation of salt)",
    }
    rec["meshes_sha256"] = shas
    rec["runs"] = runs
    rec["prj_sha256"] = prj_sha
    rec["table"] = table
    rec["sensitivity_ensemble"] = sens
    rec["n_runs"] = {"total": len(runs), "completed": sum(1 for r in runs.values() if r.get("exit_code") == 0)}
    rec["pass"] = bool(all(r.get("status") in ("OK", "OUTSIDE_VALIDITY") for r in table))
    C.write_json(C.RECEIPTS / "S8.json", rec)
    write_csv(table)
    print(json.dumps({k: v.get("ranking") for k, v in sens.items() if isinstance(v, dict)}, indent=1))
    return rec


def write_csv(table):
    import csv  # noqa: PLC0415

    cols = ["case", "scenario", "scenario_role", "salt_lambda_in_plane", "salt_lambda_out_of_plane",
            "ob_lambda_in_plane", "creep_factor", "backfill", "status", "exit_code", "wall_time_s",
            "s_max_m@0+", "s_max_m@10yr", "s_max_m@50yr", "x50_m@50yr", "x_inflection_m@50yr",
            "x10_m@50yr", "angle_x10_from_panel_edge_deg@50yr", "max_tilt_mm_per_m@50yr",
            "conv_room0_m@50yr", "pillar1_strain@50yr"]
    path = C.RECEIPTS / "S8_hypothesis_matrix.csv"
    with open(path, "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(cols + ["background_s_max_m@50yr", "validity_flags"])
        for r in table:
            bgs = r.get("background_s_max_series_m")
            w.writerow([_fmt(r.get(c)) for c in cols] + [_fmt(bgs[-1] if bgs else None),
                                                        "; ".join(r.get("validity_flags", []))])


def _fmt(v):
    if isinstance(v, float):
        return f"{v:.6g}"
    return "" if v is None else str(v)


def sensitivity(table, factors=("lambda", "creep_factor", "backfill")) -> dict:
    """Main effects: for each factor, mean response per level and range of level means; share of the
    total sum of squares explained by each main effect (balanced full factorial -> exact decomposition)."""
    ok = [r for r in table if r.get("status") in ("OK", "OUTSIDE_VALIDITY")]
    out = {"n_runs_used": len(ok), "n_runs_total": len(table)}
    if not ok:
        return out
    for resp in ("s_max_m@50yr", "s_max_m@10yr", "s_max_m@0+", "x50_m@50yr", "x_inflection_m@50yr",
                 "max_tilt_mm_per_m@50yr"):
        vals = np.array([r[resp] for r in ok], float)
        if np.any(~np.isfinite(vals)):
            continue
        for use_log in (False, True):
            if use_log and np.any(vals <= 0):
                continue
            yv = np.log10(vals) if use_log else vals
            mu = yv.mean()
            sst = float(((yv - mu) ** 2).sum())
            eff = {}
            for fk in factors:
                levels = sorted(set(r[fk] for r in ok), key=str)
                means = {str(lv): float(np.mean([yy for yy, r in zip(yv, ok) if r[fk] == lv])) for lv in levels}
                ss = float(sum(sum(1 for r in ok if r[fk] == lv) * (means[str(lv)] - mu) ** 2 for lv in levels))
                eff[fk] = {"level_means": means, "range_of_level_means": max(means.values()) - min(means.values()),
                           "share_of_total_SS": ss / sst if sst > 0 else 0.0}
            rank = sorted(eff, key=lambda k: -eff[k]["share_of_total_SS"])
            key = f"{resp}{' (log10)' if use_log else ''}"
            out[key] = {"effects": eff, "ranking": rank, "total_SS": sst}
    return out


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "S7"
    if arg == "S7":
        s7()
    elif arg == "S7post":
        s7(run=False)
    elif arg == "S8":
        s8(run=True)
    elif arg == "S7one":
        s7_one(sys.argv[2])
    elif arg == "S8b":
        s8b(run=True, workers=int(sys.argv[2]) if len(sys.argv) > 2 else 8)
    elif arg == "runall":
        runall(1)
    elif arg == "S8all":
        s8_all_post()
    elif arg == "S8post":
        s8(run=False)
    elif arg == "smoke":  # quick syntax/timing test: base case to 2 years (not a ladder step)
        wd = C.RUN_ROOT / "smoke"
        wd.mkdir(parents=True, exist_ok=True)
        write_meshes(wd, 1)
        p = wd / "smoke.prj"
        p.write_text(toy_prj("smoke", "toy_rooms.vtu", "toy.gml", float(sys.argv[2]) if len(sys.argv) > 2 else 1.0,
                             float(sys.argv[3]) if len(sys.argv) > 3 else 1.0, "soft", True, t_end=11.0 * C.YEAR))
        r = C.run_ogs(p, wd / "out", wd / "ogs.log")
        print(r)
        if r["exit_code"] == 0:
            d = extract(wd / "out", "smoke")
            print("t_yr", [round(t / C.YEAR, 4) for t in d["t_s"]])
            print("s_max", [round(-min(u), 5) for u in d["uy_surf"]])
            print("conv0", [round(v, 5) for v in d["conv_room0_m"]])
            print("pillar1 strain", [round(v, 5) for v in d["pillar1_strain"]])
            print("pillar1 sig mean", [round(v / 1e6, 3) for v in d["pillar1_sigma_yy_mean_Pa"]])

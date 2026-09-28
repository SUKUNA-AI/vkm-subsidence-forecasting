"""OGS quick checks, ladder S1-S6 (analytic / reference checks).

Usage (inside the workstation Linux environment with the OGS source build):
    python run_ladder.py S1 S2 S3 S4 S5 S6
Runs are written to <RUN_ROOT>/<step>/ (outside git); the .prj inputs are copied to
inputs/prj/<step>/ and a receipt to receipts/<step>.json. Test values in S2, S5 are
TEST_VALUE numbers (not rock properties); S3/S4/S6 use the toy's labelled values (oqc_toy.py).
"""
from __future__ import annotations

import math
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

import oqc_common as C
import oqc_toy as T

R_GAS = 8.3144621  # J/(mol K): value of MaterialLib::PhysicalConstant::IdealGasConstant in OGS 6.5.9


def step_dir(step: str) -> Path:
    d = C.RUN_ROOT / step
    d.mkdir(parents=True, exist_ok=True)
    return d


def last_output(outdir: Path, prefix: str):
    items = C.read_pvd(outdir / f"{prefix}.pvd")
    return items


# ============================================================================= S1
S1_BENCH = [
    ("disc_with_hole", "Mechanics/Linear", "disc_with_hole.prj",
     "SMALL_DEFORMATION, LinearElasticIsotropic, plate with a hole under traction"),
    ("square_with_deactivated_hole", "Mechanics/Linear", "square_with_deactivated_hole.prj",
     "SMALL_DEFORMATION, deactivated subdomain (material id 1)"),
    ("arehs_salt_creep_gravity", "Mechanics/CreepWithHeterogeneousReferenceTemperature",
     "arehs-salt-M_gravity_only_element_refT.prj",
     "SMALL_DEFORMATION, CreepBGRa rock salt + elastic layers under gravity, 1000 years"),
]


def s1() -> dict:
    rec = C.receipt_base("S1", "Version and shipped OGS small-deformation benchmarks vs their references")
    rec["method"] = ("Benchmark folders copied from <OGS_SRC>/Tests/Data; OGS run with '-r <copy>' which "
                     "executes the <test_definition> vtkdiff comparisons of the .prj against the shipped "
                     "reference .vtu files with the tolerances defined by OGS developers.")
    rec["benchmarks"] = []
    ok_all = True
    for name, rel, prj, desc in S1_BENCH:
        src = C.OGS_SRC / "Tests" / "Data" / rel
        wd = step_dir("S1") / name
        if wd.exists():
            shutil.rmtree(wd)
        ref = wd / "ref"
        shutil.copytree(src, ref)
        root = ET.parse(ref / prj).getroot()
        tests = []
        for vd in root.iter("vtkdiff"):
            tests.append({k: (vd.find(k).text.strip() if vd.find(k) is not None else None)
                          for k in ("file", "field", "absolute_tolerance", "relative_tolerance")})
        inputs = {prj: C.sha256_file(ref / prj)}
        for tag in ("mesh", "geometry"):
            for el in root.iter(tag):
                if el.text and (ref / el.text.strip()).exists():
                    inputs[el.text.strip()] = C.sha256_file(ref / el.text.strip())
        refs = {t["file"]: C.sha256_file(ref / t["file"]) for t in tests if (ref / t["file"]).exists()}
        res = C.run_ogs(ref / prj, wd / "out", wd / "ogs.log", extra=["-r", str(ref)])
        log = (wd / "ogs.log").read_text(errors="replace")
        # vtkdiff blocks: parse abs/rel maximum norms and re-check them against the tolerances
        blocks, cur = [], None
        for ln in log.splitlines():
            if "vtkdiff begin" in ln:
                cur = {}
            elif "vtkdiff end" in ln and cur is not None:
                blocks.append(cur)
                cur = None
            elif cur is not None and "maximum norm" in ln:
                key = "abs_max_norm" if ln.strip().startswith("abs") else "rel_max_norm"
                cur[key] = [float(v) for v in ln.split("[", 1)[1].rstrip("]").split(",")]
        recheck = []
        for t, b in zip(tests, blocks):
            at, rt = float(t["absolute_tolerance"]), float(t["relative_tolerance"])
            comp_ok = [(a <= at) or (r <= rt) for a, r in zip(b.get("abs_max_norm", []), b.get("rel_max_norm", []))]
            recheck.append({"file": t["file"], "field": t["field"], "abs_tol": at, "rel_tol": rt, **b,
                            "all_components_within_tolerance": bool(comp_ok and all(comp_ok))})
        ok = res["exit_code"] == 0 and len(recheck) == len(tests) and all(r["all_components_within_tolerance"] for r in recheck)
        ok_all &= ok
        rec["benchmarks"].append({
            "name": name, "benchmark": f"<OGS_SRC>/Tests/Data/{rel}/{prj}", "description": desc,
            "inputs_sha256": inputs, "references_sha256": refs, "test_definition": tests,
            "run": res, "vtkdiff_results": recheck,
            "pass": ok,
        })
        print(f"S1 {name}: exit={res['exit_code']} wall={res['wall_time_s']}s")
    rec["pass"] = ok_all
    rec["check_summary"] = "all shipped comparisons passed" if ok_all else "at least one comparison FAILED"
    C.write_json(C.RECEIPTS / "S1.json", rec)
    return rec


# ============================================================================= S2
def s2() -> dict:
    wd = step_dir("S2")
    E, nu, s = 10e9, 0.25, -10e6  # TEST_VALUE
    pts, conn, _ = C.quad8_rectilinear([0.0, 1.0], [0.0, 1.0])
    mesh_sha = C.write_mesh(wd / "s2_square_quad8.vtu", pts, "quad8", conn, np.zeros(1))
    gml_sha = C.rect_gml_2d(wd / "s2_square.gml", "sq", 0.0, 1.0, 0.0, 1.0)
    ts, _ = C.xml_fixed_timestepping(0.0, [(1, 1.0)])
    params = [C.xml_param_constant("E", E), C.xml_param_constant("nu", nu),
              C.xml_param_constant("zero", 0.0), C.xml_param_constant("u0", [0.0, 0.0]),
              C.xml_param_constant("traction", s)]
    bcs = [C.xml_bc("sq", "LEFT", "Dirichlet", 0, "zero"), C.xml_bc("sq", "BOTTOM", "Dirichlet", 1, "zero"),
           C.xml_bc("sq", "TOP", "Neumann", 1, "traction")]
    prj = C.build_prj(mesh="s2_square_quad8.vtu", geometry="s2_square.gml", dim=2, integration_order=3,
                      constitutive=[C.xml_linear_elastic(None, "E", "nu")], body_force=[0.0, 0.0],
                      parameters=params, densities={None: 2000.0}, pv_order=2, ic_param="u0", bcs=bcs,
                      time_stepping=ts, output=C.xml_output("s2", ["displacement", "sigma", "epsilon"], pairs=[(1, 1)]))
    p = wd / "S2_single_element_hooke.prj"
    p.write_text(prj)
    C.copy_prj_to_repo(p, "S2")
    res = C.run_ogs(p, wd / "out", wd / "ogs.log")
    rec = C.receipt_base("S2", "Single QUAD8 element, plane strain, linear elastic vs Hooke's law")
    rec.update(inputs_sha256={p.name: C.sha256_file(p), "s2_square_quad8.vtu": mesh_sha, "s2_square.gml": gml_sha},
               run=res, test_values={"E_Pa": E, "nu": nu, "sigma_yy_Pa": s},
               note="E, nu, load are TEST_VALUE numbers, not rock properties")
    if res["exit_code"] != 0:
        rec["pass"] = False
        C.write_json(C.RECEIPTS / "S2.json", rec)
        return rec
    t, f = last_output(wd / "out", "s2")[-1]
    m = C.read_vtu(f)
    sig, eps, u = m.point_data["sigma"], m.point_data["epsilon"], m.point_data["displacement"]
    exp_eyy = (1 - nu ** 2) * s / E
    exp_exx = -nu * (1 + nu) * s / E
    exp_szz = nu * s
    top = np.isclose(m.points[:, 1], 1.0)
    right = np.isclose(m.points[:, 0], 1.0)
    checks = {
        "sigma_yy": {"ogs": float(sig[:, 1].mean()), "analytic": s,
                     "max_rel_err": float(np.max(C.rel_err(sig[:, 1], s)))},
        "sigma_xx": {"ogs_max_abs_Pa": float(np.max(np.abs(sig[:, 0]))), "analytic": 0.0},
        "sigma_zz": {"ogs": float(sig[:, 2].mean()), "analytic": exp_szz,
                     "max_rel_err": float(np.max(C.rel_err(sig[:, 2], exp_szz)))},
        "eps_yy": {"ogs": float(eps[:, 1].mean()), "analytic": exp_eyy,
                   "max_rel_err": float(np.max(C.rel_err(eps[:, 1], exp_eyy)))},
        "eps_xx": {"ogs": float(eps[:, 0].mean()), "analytic": exp_exx,
                   "max_rel_err": float(np.max(C.rel_err(eps[:, 0], exp_exx)))},
        "u_y_top": {"ogs": float(u[top, 1].mean()), "analytic": exp_eyy,
                    "max_rel_err": float(np.max(C.rel_err(u[top, 1], exp_eyy)))},
        "u_x_right": {"ogs": float(u[right, 0].mean()), "analytic": exp_exx,
                      "max_rel_err": float(np.max(C.rel_err(u[right, 0], exp_exx)))},
    }
    worst = max(v.get("max_rel_err", 0.0) for v in checks.values())
    tol = 1e-8
    rec.update(checks=checks, tolerance_rel=tol, worst_rel_err=worst,
               pass_=bool(worst < tol and checks["sigma_xx"]["ogs_max_abs_Pa"] < 1e-6 * abs(s)))
    rec["pass"] = rec.pop("pass_")
    C.write_json(C.RECEIPTS / "S2.json", rec)
    print(f"S2: worst rel err {worst:.3e} pass={rec['pass']}")
    return rec


# ============================================================================= S3 / S4 helpers
def toy_elastic_constitutive(mids) -> list[str]:
    return [C.xml_linear_elastic(mid, f"E_{T.mat_lith(mid)}", f"nu_{T.mat_lith(mid)}") for mid in mids]


def toy_material_params() -> list[str]:
    out = []
    for lith in ("marl", "rock_salt", "sylvinite"):
        out.append(C.xml_param_constant(f"E_{lith}", T.E[lith]))
        out.append(C.xml_param_constant(f"nu_{lith}", T.NU[lith]))
    return out


def analytic_uy_column(yq: np.ndarray) -> np.ndarray:
    """u_y(y) of a laterally confined layered column under self-weight: int_{bottom}^{y} sigma_v/M dy."""
    grid = np.unique(np.concatenate([np.array(T.INTERFACES), yq]))
    grid.sort()
    # integrand sigma_v(y)/M(y) is linear inside each layer; evaluate one-sided at nodes per segment
    u = {grid[0]: 0.0}
    acc = 0.0
    for ya, yb in zip(grid[:-1], grid[1:]):
        ym = 0.5 * (ya + yb)
        lith = T.mat_lith(T.material_at_depth(ym))
        Ey, nuy = T.E[lith], T.NU[lith]
        M = Ey * (1 - nuy) / ((1 + nuy) * (1 - 2 * nuy))
        acc += 0.5 * (T.sigma_v(ya) + T.sigma_v(yb)) / M * (yb - ya)
        u[yb] = acc
    return np.array([u[y] for y in yq])


def s3() -> dict:
    wd = step_dir("S3")
    pts, conn, mids, xs, ys = T.column_mesh(10.0)
    mesh_sha = C.write_mesh(wd / "s3_column.vtu", pts, "quad8", conn, mids)
    gml_sha = C.rect_gml_2d(wd / "s3_column.gml", "col", 0.0, 10.0, T.Y_BOTTOM, 0.0)
    present = sorted(set(int(m) for m in mids))
    ts, _ = C.xml_fixed_timestepping(0.0, [(1, 1.0)])
    params = toy_material_params() + [C.xml_param_constant("zero", 0.0), C.xml_param_constant("u0", [0.0, 0.0])]
    bcs = [C.xml_bc("col", "LEFT", "Dirichlet", 0, "zero"), C.xml_bc("col", "RIGHT", "Dirichlet", 0, "zero"),
           C.xml_bc("col", "BOTTOM", "Dirichlet", 1, "zero")]
    dens = {m: T.RHO[T.mat_lith(m)] for m in present}
    prj = C.build_prj(mesh="s3_column.vtu", geometry="s3_column.gml", dim=2, integration_order=3,
                      constitutive=toy_elastic_constitutive(present), body_force=[0.0, -C.G],
                      parameters=params, densities=dens, pv_order=2, ic_param="u0", bcs=bcs, time_stepping=ts,
                      output=C.xml_output("s3", ["displacement", "sigma", "epsilon"], pairs=[(1, 1)]))
    p = wd / "S3_column_gravity.prj"
    p.write_text(prj)
    C.copy_prj_to_repo(p, "S3")
    res = C.run_ogs(p, wd / "out", wd / "ogs.log")
    rec = C.receipt_base("S3", "Layered column under gravity: sigma_v(z) = int rho g dz, K0 and settlement")
    rec.update(inputs_sha256={p.name: C.sha256_file(p), "s3_column.vtu": mesh_sha, "s3_column.gml": gml_sha},
               run=res, mesh={"elements": int(len(conn)), "nodes": int(len(pts)), "type": "QUAD8"})
    if res["exit_code"] != 0:
        rec["pass"] = False
        C.write_json(C.RECEIPTS / "S3.json", rec)
        return rec
    _, f = last_output(wd / "out", "s3")[-1]
    m = C.read_vtu(f)
    y = m.points[:, 1]
    sig, u = m.point_data["sigma"], m.point_data["displacement"]
    sv = T.sigma_v(y)
    sv_bottom = abs(T.sigma_v(np.array([T.Y_BOTTOM]))[0])
    err_sv = float(np.max(np.abs(sig[:, 1] - sv)) / sv_bottom)
    on_iface = np.zeros_like(y, dtype=bool)
    for yi in T.INTERFACES:
        on_iface |= np.isclose(y, yi, atol=1e-6)
    k0 = np.array([T.NU[T.mat_lith(T.material_at_depth(yy))] for yy in y])
    k0 = k0 / (1 - k0)
    inner = ~on_iface & (y < -1e-6)
    err_k0 = float(np.max(np.abs(sig[inner, 0] - k0[inner] * sv[inner])) / sv_bottom)
    uy_an = analytic_uy_column(y)
    err_u = float(np.max(np.abs(u[:, 1] - uy_an)) / abs(uy_an).max())
    tol = 1e-6
    rec.update(checks={
        "sigma_v_bottom_Pa": -sv_bottom,
        "max_abs_err_sigma_yy_over_sigma_v_bottom": err_sv,
        "max_abs_err_sigma_xx_vs_K0_over_sigma_v_bottom_(non-interface nodes)": err_k0,
        "surface_settlement_ogs_m": float(u[np.isclose(y, 0.0), 1].mean()),
        "surface_settlement_analytic_m": float(analytic_uy_column(np.array([0.0]))[0]),
        "max_abs_err_u_y_over_max_u": err_u,
    }, tolerance_rel=tol)
    rec["pass"] = bool(max(err_sv, err_k0, err_u) < tol)
    C.write_json(C.RECEIPTS / "S3.json", rec)
    print(f"S3: err_sv={err_sv:.2e} err_k0={err_k0:.2e} err_u={err_u:.2e} pass={rec['pass']}")
    return rec


def s4() -> dict:
    wd = step_dir("S4")
    rec = C.receipt_base("S4", "Initial stress sigma_h = lambda*sigma_v as initial condition -> equilibrium, no displacement")
    rec["cases"] = []
    ok_all = True
    # column, 4 lambda hypotheses (PC-03), elastic
    pts, conn, mids, xs, ys = T.column_mesh(10.0)
    mesh_sha = C.write_mesh(wd / "s4_column.vtu", pts, "quad8", conn, mids)
    gml_sha = C.rect_gml_2d(wd / "s4_column.gml", "col", 0.0, 10.0, T.Y_BOTTOM, 0.0)
    present = sorted(set(int(m) for m in mids))
    ref_settlement = abs(float(analytic_uy_column(np.array([0.0]))[0]))
    for lam in sorted(T.LAMBDAS):
        case = f"column_lambda_{lam:g}"
        ts, _ = C.xml_fixed_timestepping(0.0, [(1, 1.0)])
        params = toy_material_params() + [
            C.xml_param_constant("zero", 0.0), C.xml_param_constant("u0", [0.0, 0.0]),
            C.xml_param_function("sigma0", T.initial_stress_expressions(lam, with_rooms=False))]
        bcs = [C.xml_bc("col", "LEFT", "Dirichlet", 0, "zero"), C.xml_bc("col", "RIGHT", "Dirichlet", 0, "zero"),
               C.xml_bc("col", "BOTTOM", "Dirichlet", 1, "zero")]
        dens = {m: T.RHO[T.mat_lith(m)] for m in present}
        prj = C.build_prj(mesh="s4_column.vtu", geometry="s4_column.gml", dim=2, integration_order=3,
                          constitutive=toy_elastic_constitutive(present), body_force=[0.0, -C.G],
                          parameters=params, densities=dens, pv_order=2, ic_param="u0", bcs=bcs,
                          time_stepping=ts, initial_stress="sigma0",
                          output=C.xml_output(case, ["displacement", "sigma"], pairs=[(1, 1)]))
        p = wd / f"S4_{case}.prj"
        p.write_text(prj)
        C.copy_prj_to_repo(p, "S4")
        res = C.run_ogs(p, wd / f"out_{case}", wd / f"ogs_{case}.log")
        c = {"case": case, "lambda": lam, "lambda_status": T.LAMBDAS[lam], "run": res,
             "inputs_sha256": {p.name: C.sha256_file(p), "s4_column.vtu": mesh_sha, "s4_column.gml": gml_sha}}
        if res["exit_code"] == 0:
            _, f = last_output(wd / f"out_{case}", case)[-1]
            m = C.read_vtu(f)
            y = m.points[:, 1]
            sig, u = m.point_data["sigma"], m.point_data["displacement"]
            sv = T.sigma_v(y)
            svb = abs(T.sigma_v(np.array([T.Y_BOTTOM]))[0])
            c["max_abs_u_m"] = float(np.max(np.abs(u)))
            c["max_abs_u_over_gravity_settlement"] = c["max_abs_u_m"] / ref_settlement
            c["max_err_sigma_yy_over_sigma_v_bottom"] = float(np.max(np.abs(sig[:, 1] - sv)) / svb)
            c["max_err_sigma_xx_over_sigma_v_bottom"] = float(np.max(np.abs(sig[:, 0] - lam * sv)) / svb)
            c["pass"] = bool(c["max_abs_u_over_gravity_settlement"] < 1e-6 and
                             c["max_err_sigma_yy_over_sigma_v_bottom"] < 1e-6 and
                             c["max_err_sigma_xx_over_sigma_v_bottom"] < 1e-6)
        else:
            c["pass"] = False
        ok_all &= c["pass"]
        rec["cases"].append(c)
        print(f"S4 {case}: exit={res['exit_code']} max|u|={c.get('max_abs_u_m')} pass={c['pass']}")
    # toy 2D block without rooms, all elastic, lambda = 0.45 (checks the toy sigma0 expression)
    case = "toy_block_no_rooms_lambda_0.45"
    pts, conn, mids, xs, ys = T.toy_mesh(with_rooms=False)
    msha = C.write_mesh(wd / "s4_toy_block.vtu", pts, "quad8", conn, mids)
    gsha = C.rect_gml_2d(wd / "s4_toy_block.gml", "toy", 0.0, T.X_RIGHT, T.Y_BOTTOM, 0.0)
    present = sorted(set(int(m) for m in mids))
    ts, _ = C.xml_fixed_timestepping(0.0, [(1, 1.0)])
    params = toy_material_params() + [
        C.xml_param_constant("zero", 0.0), C.xml_param_constant("u0", [0.0, 0.0]),
        C.xml_param_function("sigma0", T.initial_stress_expressions(0.45, with_rooms=False))]
    bcs = [C.xml_bc("toy", "LEFT", "Dirichlet", 0, "zero"), C.xml_bc("toy", "RIGHT", "Dirichlet", 0, "zero"),
           C.xml_bc("toy", "BOTTOM", "Dirichlet", 1, "zero")]
    dens = {m: T.RHO[T.mat_lith(m)] for m in present}
    prj = C.build_prj(mesh="s4_toy_block.vtu", geometry="s4_toy_block.gml", dim=2, integration_order=3,
                      constitutive=toy_elastic_constitutive(present), body_force=[0.0, -C.G],
                      parameters=params, densities=dens, pv_order=2, ic_param="u0", bcs=bcs,
                      time_stepping=ts, initial_stress="sigma0",
                      output=C.xml_output("s4toy", ["displacement", "sigma"], pairs=[(1, 1)]))
    p = wd / f"S4_{case}.prj"
    p.write_text(prj)
    C.copy_prj_to_repo(p, "S4")
    res = C.run_ogs(p, wd / "out_toy", wd / "ogs_toy.log")
    c = {"case": case, "lambda": 0.45, "run": res,
         "mesh": {"elements": int(len(conn)), "nodes": int(len(pts))},
         "inputs_sha256": {p.name: C.sha256_file(p), "s4_toy_block.vtu": msha, "s4_toy_block.gml": gsha}}
    if res["exit_code"] == 0:
        _, f = last_output(wd / "out_toy", "s4toy")[-1]
        m = C.read_vtu(f)
        c["max_abs_u_m"] = float(np.max(np.abs(m.point_data["displacement"])))
        c["max_abs_u_over_gravity_settlement"] = c["max_abs_u_m"] / ref_settlement
        c["pass"] = bool(c["max_abs_u_over_gravity_settlement"] < 1e-6)
    else:
        c["pass"] = False
    ok_all &= c["pass"]
    rec["cases"].append(c)
    print(f"S4 {case}: exit={res['exit_code']} max|u|={c.get('max_abs_u_m')} pass={c['pass']}")
    rec["reference_scale"] = {"gravity_settlement_of_column_m": ref_settlement,
                              "note": "S3 settlement without initial stress, used to normalise |u|"}
    rec["pass"] = ok_all
    C.write_json(C.RECEIPTS / "S4.json", rec)
    return rec


# ============================================================================= S5 Kirsch
def kirsch_mesh(a=1.0, L=20.0, n_theta=24, n_r=30, n_ring=6, growth=1.13, core=0.5):
    """Quarter plate [0,L]^2 with a circular hole of radius a meshed inside (material 1).
    QUAD8, structured O-grid: plate rays from the circle to the square boundary, ring rays from a
    core square (side core*a) to the circle, core square as a tensor grid."""
    dth = 0.5 * math.pi / n_theta
    s = core * a

    def sq(theta, half):
        if theta <= math.pi / 4 + 1e-15:
            return np.array([half, half * math.tan(theta)])
        return np.array([half / math.tan(theta), half])

    def circ(theta):
        return np.array([a * math.cos(theta), a * math.sin(theta)])

    def f_geo(i):
        return (growth ** i - 1.0) / (growth ** n_r - 1.0)

    nodes: dict = {}
    pts: list = []

    def node(p):
        key = (round(float(p[0]), 9), round(float(p[1]), 9))
        if key not in nodes:
            nodes[key] = len(pts)
            pts.append((float(p[0]), float(p[1])))
        return nodes[key]

    conn, mids = [], []

    def add_elem(P, mat):  # P(i, j) with half-integer args
        c = [node(P(0, 0)), node(P(1, 0)), node(P(1, 1)), node(P(0, 1)),
             node(P(0.5, 0)), node(P(1, 0.5)), node(P(0.5, 1)), node(P(0, 0.5))]
        conn.append(c)
        mids.append(mat)

    # plate
    for j in range(n_theta):
        for i in range(n_r):
            def P(di, dj, i=i, j=j):
                th = (j + dj) * dth
                pin, pout = circ(th), sq(th, L)
                return pin + f_geo(i + di) * (pout - pin)
            add_elem(P, 0)
    # ring (core square boundary -> circle)
    for j in range(n_theta):
        for i in range(n_ring):
            def P(di, dj, i=i, j=j):
                th = (j + dj) * dth
                pin, pout = sq(th, s), circ(th)
                return pin + ((i + di) / n_ring) * (pout - pin)
            add_elem(P, 1)
    # core square
    nc = n_theta // 2
    for l in range(nc):
        for k in range(nc):
            def P(dk, dl, k=k, l=l):
                return np.array([s * math.tan((k + dk) * dth), s * math.tan((l + dl) * dth)])
            add_elem(P, 1)
    return np.array(pts), np.array(conn, dtype=np.int64), np.array(mids, dtype=np.int32)


def kirsch_analytic(r, th, a, Sx, Sy, G, nu):
    P, Q = 0.5 * (Sx + Sy), 0.5 * (Sx - Sy)
    ar2 = (a / r) ** 2
    ar4 = ar2 ** 2
    srr = P * (1 - ar2) + Q * (1 - 4 * ar2 + 3 * ar4) * np.cos(2 * th)
    stt = P * (1 + ar2) - Q * (1 + 3 * ar4) * np.cos(2 * th)
    ur = a ** 2 / (2 * G * r) * (P + Q * (4 * (1 - nu) - ar2) * np.cos(2 * th))
    return srr, stt, ur


def s5() -> dict:
    wd = step_dir("S5")
    a, L, p, lam, E, nu = 1.0, 20.0, 10e6, 0.5, 10e9, 0.25  # TEST_VALUE
    Sx, Sy = -lam * p, -p
    G = E / (2 * (1 + nu))
    pts, conn, mids = kirsch_mesh(a=a, L=L)
    msha = C.write_mesh(wd / "s5_kirsch.vtu", pts, "quad8", conn, mids)
    gsha = C.rect_gml_2d(wd / "s5_kirsch.gml", "k", 0.0, L, 0.0, L)
    rec = C.receipt_base("S5", "Circular opening in an elastic plate (plane strain) vs Kirsch")
    rec.update(test_values={"a_m": a, "L_m": L, "p_Pa": p, "lambda": lam, "E_Pa": E, "nu": nu,
                            "Sx_Pa": Sx, "Sy_Pa": Sy},
               mesh={"elements": int(len(conn)), "nodes": int(len(pts)), "type": "QUAD8 O-grid, hole meshed (mat 1)"},
               note="TEST_VALUE numbers; quarter model, symmetry rollers, far-field traction on x=L, y=L")
    rec["variants"] = []
    ok_all = True
    common_params = [C.xml_param_constant("E", E), C.xml_param_constant("nu", nu),
                     C.xml_param_constant("zero", 0.0), C.xml_param_constant("u0", [0.0, 0.0]),
                     C.xml_param_constant("tx", Sx), C.xml_param_constant("ty", Sy)]
    bcs = [C.xml_bc("k", "LEFT", "Dirichlet", 0, "zero"), C.xml_bc("k", "BOTTOM", "Dirichlet", 1, "zero"),
           C.xml_bc("k", "RIGHT", "Neumann", 0, "tx"), C.xml_bc("k", "TOP", "Neumann", 1, "ty")]
    for variant in ("deactivated_subdomain", "void_material_zero_initial_stress"):
        if variant == "deactivated_subdomain":
            ts, _ = C.xml_fixed_timestepping(0.0, [(2, 1.0)])
            params = common_params + [C.xml_param_constant("sigma0", [Sx, Sy, Sx, 0.0])]
            cons = [C.xml_linear_elastic("*", "E", "nu")]
            dens = {"*": 0.0}
            deact = ("<deactivated_subdomains><deactivated_subdomain>"
                     "<time_interval><start>1.5</start><end>3.0</end></time_interval>"
                     "<material_ids>1</material_ids></deactivated_subdomain></deactivated_subdomains>")
            pairs = [(2, 1)]
        else:
            ts, _ = C.xml_fixed_timestepping(0.0, [(1, 1.0)])
            rc = f"sqrt(x^2 + y^2) < {a!r}"
            params = common_params + [
                C.xml_param_constant("E_void", E * 1e-5), C.xml_param_constant("nu_void", 0.0),
                C.xml_param_function("sigma0", [f"if({rc}, 0.0, {Sx!r})", f"if({rc}, 0.0, {Sy!r})",
                                                f"if({rc}, 0.0, {Sx!r})", "0.0"])]
            cons = [C.xml_linear_elastic(0, "E", "nu"), C.xml_linear_elastic(1, "E_void", "nu_void")]
            dens = {0: 0.0, 1: 0.0}
            deact = ""
            pairs = [(1, 1)]
        prj = C.build_prj(mesh="s5_kirsch.vtu", geometry="s5_kirsch.gml", dim=2, integration_order=3,
                          constitutive=cons, body_force=[0.0, 0.0], parameters=params, densities=dens,
                          pv_order=2, ic_param="u0", bcs=bcs, time_stepping=ts, initial_stress="sigma0",
                          deactivated=deact, output=C.xml_output(f"s5_{variant}", ["displacement", "sigma"], pairs=pairs))
        pth = wd / f"S5_kirsch_{variant}.prj"
        pth.write_text(prj)
        C.copy_prj_to_repo(pth, "S5")
        res = C.run_ogs(pth, wd / f"out_{variant}", wd / f"ogs_{variant}.log")
        v = {"variant": variant, "run": res,
             "inputs_sha256": {pth.name: C.sha256_file(pth), "s5_kirsch.vtu": msha, "s5_kirsch.gml": gsha}}
        if res["exit_code"] != 0:
            v["pass"] = False
            ok_all = False
            rec["variants"].append(v)
            print(f"S5 {variant}: exit={res['exit_code']} FAILED")
            continue
        outs = last_output(wd / f"out_{variant}", f"s5_{variant}")
        if variant == "deactivated_subdomain":
            # step 1 (t=1): all elements active -> equilibrium, no displacement
            t1 = [f for t, f in outs if abs(t - 1.0) < 1e-9]
            m1 = C.read_vtu(t1[0])
            v["step1_all_active_max_abs_u_m"] = float(np.max(np.abs(m1.point_data["displacement"])))
        _, f = outs[-1]
        m = C.read_vtu(f)
        x, y = m.points[:, 0], m.points[:, 1]
        sig, u = m.point_data["sigma"], m.point_data["displacement"]
        prof = {}
        for axis, th in (("x_axis_theta0", 0.0), ("y_axis_theta90", math.pi / 2)):
            if th == 0.0:
                sel = np.isclose(y, 0.0) & (x >= a - 1e-9) & (x <= 5 * a + 1e-9)
                r = x[sel]
                s_tt, s_rr, ur = sig[sel, 1], sig[sel, 0], u[sel, 0]
            else:
                sel = np.isclose(x, 0.0) & (y >= a - 1e-9) & (y <= 5 * a + 1e-9)
                r = y[sel]
                s_tt, s_rr, ur = sig[sel, 0], sig[sel, 1], u[sel, 1]
            o = np.argsort(r)
            r, s_tt, s_rr, ur = r[o], s_tt[o], s_rr[o], ur[o]
            a_rr, a_tt, a_ur = kirsch_analytic(r, th, a, Sx, Sy, G, nu)
            # nodal stresses are element averages: the wall node mixes plate and hole elements, so the
            # wall value is taken by quadratic extrapolation from the first 3 plate-only nodes (r > a)
            inner = r > a + 1e-9
            coef = np.polyfit(r[inner][:3], s_tt[inner][:3], 2)
            s_wall = float(np.polyval(coef, a))
            prof[axis] = {
                "wall_sigma_tt_ogs_extrapolated_Pa": s_wall, "wall_sigma_tt_kirsch_Pa": float(a_tt[0]),
                "wall_sigma_tt_rel_err": float(abs(s_wall - a_tt[0]) / abs(a_tt[0])),
                "wall_node_sigma_tt_raw_average_Pa": float(s_tt[0]),
                "wall_u_r_ogs_m": float(ur[0]), "wall_u_r_kirsch_m": float(a_ur[0]),
                "wall_u_r_rel_err": float(abs(ur[0] - a_ur[0]) / abs(a_ur[0])),
                "profile_r_gt_a_to_5a_max_abs_err_sigma_tt_over_p": float(np.max(np.abs(s_tt[inner] - a_tt[inner])) / p),
                "profile_max_abs_err_sigma_rr_over_p": float(np.max(np.abs(s_rr[inner] - a_rr[inner])) / p),
                "profile_max_rel_err_u_r": float(np.max(np.abs(ur - a_ur) / np.abs(a_ur))),
                "n_points": int(len(r)),
                "_r": r.tolist(), "_stt": s_tt.tolist(), "_stt_an": a_tt.tolist(),
                "_ur": ur.tolist(), "_ur_an": a_ur.tolist(),
            }
        v["profiles"] = prof
        worst_wall_s = max(prof[k]["wall_sigma_tt_rel_err"] for k in prof)
        worst_wall_u = max(prof[k]["wall_u_r_rel_err"] for k in prof)
        worst_prof = max(prof[k]["profile_r_gt_a_to_5a_max_abs_err_sigma_tt_over_p"] for k in prof)
        tol_s, tol_u, tol_p = 0.03, 0.03, 0.05
        v.update(worst_wall_sigma_tt_rel_err=worst_wall_s, worst_wall_u_r_rel_err=worst_wall_u,
                 worst_profile_sigma_tt_err_over_p=worst_prof,
                 tolerances={"wall_sigma_tt_rel": tol_s, "wall_u_r_rel": tol_u, "profile_sigma_over_p": tol_p})
        v["pass"] = bool(worst_wall_s < tol_s and worst_wall_u < tol_u and worst_prof < tol_p and
                         v.get("step1_all_active_max_abs_u_m", 0.0) < 1e-12)
        ok_all &= v["pass"]
        rec["variants"].append(v)
        print(f"S5 {variant}: wall sigma err {worst_wall_s:.3%}, wall u err {worst_wall_u:.3%}, "
              f"profile {worst_prof:.3%} pass={v['pass']}")
    rec["pass"] = ok_all
    C.write_json(C.RECEIPTS / "S5.json", rec)
    return rec


# ============================================================================= S6 creep
def ode_plane_strain_creep(t_out, sig, E, nu, A_eff, n, s0):
    """Reference: single material point, plane strain, sigma_xx = 0, sigma_yy = -sig (const)."""
    from scipy.integrate import solve_ivp  # noqa: PLC0415

    sxx, syy = 0.0, -sig

    def rates(szz):
        p = (sxx + syy + szz) / 3.0
        sv = np.array([sxx - p, syy - p, szz - p])
        ns = math.sqrt(float(sv @ sv))
        if ns == 0.0:
            return np.zeros(3)
        seff = math.sqrt(1.5) * ns
        return math.sqrt(1.5) * A_eff * (seff / s0) ** n * sv / ns

    def rhs(t, yv):
        r = rates(yv[0])
        return [-E * r[2], r[1]]

    szz0 = nu * (sxx + syy)
    sol = solve_ivp(rhs, (0.0, t_out[-1]), [szz0, 0.0], t_eval=t_out, method="Radau", rtol=1e-10, atol=[1e-3, 1e-16])
    szz, ecr = sol.y
    eyy = (syy - nu * (sxx + szz)) / E + ecr
    return eyy, szz


def s6() -> dict:
    wd = step_dir("S6")
    rec = C.receipt_base("S6", "Constant-stress creep of one element (CreepBGRa native, MFront PowerLawLinearCreep) "
                               "vs closed form / ODE reference")
    lith = "sylvinite"
    E, nu = T.E[lith], T.NU[lith]
    A = T.CREEP_K[lith] / 86400.0
    n = T.CREEP_N[lith]
    s0 = 1e6
    sig = 15e6  # TEST load level of the order of toy pillar stresses
    t_end = 10 * C.YEAR
    rec["parameters"] = {"lithology_values_from": "oqc_toy (CL-15 sylvinite mapped to BGRa)", "E_Pa": E, "nu": nu,
                         "A_1_per_s": A, "n": n, "sigma0_Pa": s0, "axial_stress_Pa": -sig, "t_end_s": t_end}
    rec["cases"] = []
    ok_all = True
    # --- 3D single hex, uniaxial: files shipped with OGS (cube_1x1x1.gml, cube_1x1x1_hex_1e0.vtu)
    src = C.OGS_SRC / "Tests" / "Data" / "Mechanics" / "Linear"
    for fn in ("cube_1x1x1.gml", "cube_1x1x1_hex_1e0.vtu"):
        shutil.copy2(src / fn, wd / fn)
    cases3d = [
        ("3D_native_CreepBGRa_Q0", "native", 0.0, 293.15, A),
        ("3D_native_CreepBGRa_Q54kJ_T300K", "native", 54000.0, 300.0, A * math.exp(54000.0 / (R_GAS * 300.0))),
        ("3D_MFront_PowerLawLinearCreep_Q0", "mfront", 0.0, 293.15, A),
    ]
    nsteps = 40
    for name, kind, Q, Tk, Aval in cases3d:
        ts, _ = C.xml_fixed_timestepping(0.0, [(nsteps, t_end / nsteps)])
        params = [C.xml_param_constant("E", E), C.xml_param_constant("nu", nu), C.xml_param_constant("A", Aval),
                  C.xml_param_constant("n", n), C.xml_param_constant("s0", s0), C.xml_param_constant("Q", Q),
                  C.xml_param_constant("T_ref", Tk), C.xml_param_constant("zero", 0.0),
                  C.xml_param_constant("u0", [0.0, 0.0, 0.0]), C.xml_param_constant("load", -sig),
                  C.xml_param_constant("A2", 0.0), C.xml_param_constant("Q2", 0.0), C.xml_param_constant("D", 1.0)]
        if kind == "native":
            cons = [C.xml_creep_bgra(None, "E", "nu", "A", "n", "s0", "Q")]
        else:
            cons = [C.xml_mfront_pllc(None, "E", "nu", "A", "Q", "n", "s0", "A2", "Q2", "D")]
        gs = "cube_1x1x1_geometry"
        bcs = [C.xml_bc(gs, "left", "Dirichlet", 0, "zero"), C.xml_bc(gs, "front", "Dirichlet", 1, "zero"),
               C.xml_bc(gs, "bottom", "Dirichlet", 2, "zero"), C.xml_bc(gs, "top", "Neumann", 2, "load")]
        prj = C.build_prj(mesh="cube_1x1x1_hex_1e0.vtu", geometry="cube_1x1x1.gml", dim=3, integration_order=2,
                          constitutive=cons, body_force=[0.0, 0.0, 0.0], parameters=params,
                          densities={None: T.RHO[lith]}, pv_order=1, ic_param="u0", bcs=bcs, time_stepping=ts,
                          reference_temperature="T_ref", conv_abstol=1e-14, conv_reltol=1e-12,
                          output=C.xml_output(name, ["displacement", "sigma", "epsilon"], pairs=[(nsteps, 1)]))
        pth = wd / f"S6_{name}.prj"
        pth.write_text(prj)
        C.copy_prj_to_repo(pth, "S6")
        res = C.run_ogs(pth, wd / f"out_{name}", wd / f"ogs_{name}.log")
        c = {"case": name, "Q_J_per_mol": Q, "T_K": Tk, "A_input_1_per_s": Aval, "run": res,
             "inputs_sha256": {pth.name: C.sha256_file(pth), "cube_1x1x1.gml": C.sha256_file(wd / "cube_1x1x1.gml"),
                               "cube_1x1x1_hex_1e0.vtu": C.sha256_file(wd / "cube_1x1x1_hex_1e0.vtu")},
             "mesh_origin": "<OGS_SRC>/Tests/Data/Mechanics/Linear (shipped with OGS)"}
        if res["exit_code"] == 0:
            outs = last_output(wd / f"out_{name}", name)
            rate = Aval * math.exp(-Q / (R_GAS * Tk)) * (sig / s0) ** n
            tt, ezz, exx = [], [], []
            for t, f in outs:
                if t <= 0:
                    continue
                m = C.read_vtu(f)
                tt.append(t)
                ezz.append(float(m.point_data["epsilon"][:, 2].mean()))
                exx.append(float(m.point_data["epsilon"][:, 0].mean()))
            tt, ezz, exx = np.array(tt), np.array(ezz), np.array(exx)
            an_zz = -sig / E - rate * tt
            an_xx = nu * sig / E + 0.5 * rate * tt
            c.update(closed_form="eps_zz = -sigma/E - A exp(-Q/RT) (sigma/sigma0)^n t;  eps_xx = nu sigma/E + 0.5 rate t",
                     creep_rate_closed_form_1_per_s=rate,
                     creep_rate_ogs_1_per_s=float((ezz[-1] - ezz[0]) / (tt[-1] - tt[0])) * -1.0,
                     eps_zz_end_ogs=float(ezz[-1]), eps_zz_end_closed=float(an_zz[-1]),
                     max_rel_err_eps_zz=float(np.max(C.rel_err(ezz, an_zz))),
                     max_rel_err_eps_xx=float(np.max(C.rel_err(exx, an_xx))),
                     _t=tt.tolist(), _ezz=ezz.tolist(), _ezz_an=an_zz.tolist())
            c["pass"] = bool(c["max_rel_err_eps_zz"] < 1e-5 and c["max_rel_err_eps_xx"] < 1e-5)
        else:
            c["pass"] = False
        ok_all &= c["pass"]
        rec["cases"].append(c)
        print(f"S6 {name}: exit={res['exit_code']} err={c.get('max_rel_err_eps_zz')} pass={c['pass']}")
    # --- 2D plane strain single QUAD8, native CreepBGRa vs ODE reference (sigma_zz transient)
    name = "2D_plane_strain_native_CreepBGRa_Q0"
    pts, conn, _ = C.quad8_rectilinear([0.0, 1.0], [0.0, 1.0])
    msha = C.write_mesh(wd / "s6_square_quad8.vtu", pts, "quad8", conn, np.zeros(1))
    gsha = C.rect_gml_2d(wd / "s6_square.gml", "sq", 0.0, 1.0, 0.0, 1.0)
    nst = 400
    ts, _ = C.xml_fixed_timestepping(0.0, [(nst, t_end / nst)])
    params = [C.xml_param_constant("E", E), C.xml_param_constant("nu", nu), C.xml_param_constant("A", A),
              C.xml_param_constant("n", n), C.xml_param_constant("s0", s0), C.xml_param_constant("Q", 0.0),
              C.xml_param_constant("T_ref", 293.15), C.xml_param_constant("zero", 0.0),
              C.xml_param_constant("u0", [0.0, 0.0]), C.xml_param_constant("load", -sig)]
    bcs = [C.xml_bc("sq", "LEFT", "Dirichlet", 0, "zero"), C.xml_bc("sq", "BOTTOM", "Dirichlet", 1, "zero"),
           C.xml_bc("sq", "TOP", "Neumann", 1, "load")]
    prj = C.build_prj(mesh="s6_square_quad8.vtu", geometry="s6_square.gml", dim=2, integration_order=3,
                      constitutive=[C.xml_creep_bgra(None, "E", "nu", "A", "n", "s0", "Q")], body_force=[0.0, 0.0],
                      parameters=params, densities={None: T.RHO[lith]}, pv_order=2, ic_param="u0", bcs=bcs,
                      time_stepping=ts, reference_temperature="T_ref", conv_abstol=1e-14, conv_reltol=1e-12,
                      output=C.xml_output(name, ["displacement", "sigma", "epsilon"], pairs=[(nst // 10, 10)]))
    pth = wd / f"S6_{name}.prj"
    pth.write_text(prj)
    C.copy_prj_to_repo(pth, "S6")
    res = C.run_ogs(pth, wd / f"out_{name}", wd / f"ogs_{name}.log")
    c = {"case": name, "run": res, "inputs_sha256": {pth.name: C.sha256_file(pth), "s6_square_quad8.vtu": msha,
                                                     "s6_square.gml": gsha}}
    if res["exit_code"] == 0:
        outs = [(t, f) for t, f in last_output(wd / f"out_{name}", name) if t > 0]
        tt = np.array([t for t, _ in outs])
        eyy = np.array([float(C.read_vtu(f).point_data["epsilon"][:, 1].mean()) for _, f in outs])
        szz = np.array([float(C.read_vtu(f).point_data["sigma"][:, 2].mean()) for _, f in outs])
        ref_eyy, ref_szz = ode_plane_strain_creep(tt, sig, E, nu, A, n, s0)
        steady = (math.sqrt(3.0) / 2.0) ** (n + 1) * A * (sig / s0) ** n
        rate_ogs = -(eyy[-1] - eyy[-2]) / (tt[-1] - tt[-2])
        c.update(reference="ODE (scipy Radau, rtol 1e-10) for sigma_zz(t) and eps_yy(t) of one material point",
                 steady_rate_closed_form_1_per_s=steady, steady_rate_ogs_1_per_s=float(rate_ogs),
                 steady_rate_rel_err=float(abs(rate_ogs - steady) / steady),
                 sigma_zz_end_ogs_Pa=float(szz[-1]), sigma_zz_steady_closed_Pa=-sig / 2.0,
                 max_rel_err_eps_yy_vs_ode=float(np.max(C.rel_err(eyy, ref_eyy))),
                 _t=tt.tolist(), _eyy=eyy.tolist(), _eyy_ode=ref_eyy.tolist())
        c["pass"] = bool(c["max_rel_err_eps_yy_vs_ode"] < 5e-3 and c["steady_rate_rel_err"] < 5e-3)
    else:
        c["pass"] = False
    ok_all &= c["pass"]
    rec["cases"].append(c)
    print(f"S6 {name}: exit={res['exit_code']} err={c.get('max_rel_err_eps_yy_vs_ode')} "
          f"steady={c.get('steady_rate_rel_err')} pass={c['pass']}")
    rec["pass"] = ok_all
    C.write_json(C.RECEIPTS / "S6.json", rec)
    return rec


STEPS = {"S1": s1, "S2": s2, "S3": s3, "S4": s4, "S5": s5, "S6": s6}

if __name__ == "__main__":
    todo = sys.argv[1:] or list(STEPS)
    for st in todo:
        STEPS[st]()

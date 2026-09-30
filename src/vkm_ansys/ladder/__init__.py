"""Solver ladder S01…S09 of ``vkm-ansys`` (CLAUDE.md, plan §4.4): TOY problems with closed-form answers.

Every MAPDL step is an APDL deck body (``ladder/Sxx.inp``) behind a generated parameter header, a post-processing
function that turns the files written by the deck (``*VWRITE``) into ``out/results.json``, and checks whose expected
values come from the analytic solution. S08 is a DPF step: it re-reads the result files of an S02 and an S04 job and
compares DPF with the deck's own ``*VGET``/``PRNSOL`` values. Parameters are TOY values with status
ENGINEERING_ASSUMPTION (scope TOY): not SKRU-1 or salt parameters, never evidence. Results are MODEL_RESULT.

S01 install/version · S02 minimal mechanics · S03 material zones · S04 gravity · S05 initial state (INISTATE) ·
S06 time steps · S07 Norton creep · S08 result extraction (DPF vs *GET) · S09 integrated toy case (layers, excavation by
EKILL, creep, surface trough). S10 (WorldSpec adapter) is a separate task.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Mapping

from vkm_ansys.errors import ToolFailure
from vkm_ansys.mapdl_out import parse_kv, parse_mntr, parse_out, parse_table

RESULTS_NAME = "results.json"
PARAM_STATUS = "ENGINEERING_ASSUMPTION"
PARAM_SCOPE = "TOY"
PARAM_SOURCE = "vkm_ansys.ladder (toy verification problem; not a SKRU-1 value)"
REVISION = 26.1


@dataclass(frozen=True)
class Param:
    default: float
    unit: str
    note: str
    lo: float = -math.inf
    hi: float = math.inf
    integer: bool = False


@dataclass
class Step:
    step: str
    title: str
    kind: str                                 # "mapdl" | "dpf"
    params: dict[str, Param]
    checks: Callable[[dict[str, float]], list[dict[str, Any]]]
    post: Callable[[Path, dict[str, float]], dict[str, Any]] | None
    checks_text: str
    model_choices: list[str] = field(default_factory=list)
    default_timeout_s: float = 900.0
    inputs: tuple[str, ...] = ()              # job references a DPF step reads (S08: s02_job, s04_job)


def _p(default, unit, note, lo=-math.inf, hi=math.inf, integer=False) -> Param:
    return Param(float(default), unit, note, lo, hi, integer)


def _close(name: str, pointer: str, expected: float, rtol: float = 0.0, atol: float = 0.0,
           note: str = "") -> dict[str, Any]:
    return {"name": name, "kind": "number_close", "path": f"out/{RESULTS_NAME}", "pointer": pointer,
            "expected": float(expected), "rtol": rtol, "atol": atol, "note": note}


def _value(name: str, pointer: str, expected: Any, note: str = "") -> dict[str, Any]:
    return {"name": name, "kind": "json_value", "path": f"out/{RESULTS_NAME}", "pointer": pointer,
            "expected": expected, "note": note}


def _file(work: Path, name: str) -> str:
    path = work / name
    if not path.is_file():
        raise FileNotFoundError(f"{name} was not written by the deck")
    return path.read_text(encoding="utf-8", errors="replace")


def _kv(work: Path, name: str = "vkm_results.txt") -> dict[str, float]:
    return parse_kv(_file(work, name))


def _table(work: Path, name: str, columns: list[str]) -> list[dict[str, float]]:
    return parse_table(_file(work, name), columns)


def _join(a: list[dict[str, float]], b: list[dict[str, float]], key: str = "id") -> dict[int, dict[str, float]]:
    out = {int(r[key]): dict(r) for r in a}
    for r in b:
        out.setdefault(int(r[key]), {}).update(r)
    return out


def _abs_max(*values: float) -> float:
    return max(abs(v) for v in values)


def _constrained_modulus(e: float, nu: float) -> float:
    return e * (1 - nu) / ((1 + nu) * (1 - 2 * nu))


def _solution_summary(work: Path) -> dict[str, Any]:
    """Errors, completed steps and substeps (``.out``), bisections and iterations (``.mntr`` attempts)."""
    s = parse_out(_file(work, "file.out"))
    mntr_file = work / "file.mntr"
    mntr = parse_mntr(mntr_file.read_text(encoding="utf-8", errors="replace")) if mntr_file.is_file() else None
    return {"errors": s["errors"], "warnings": s["warnings"], "load_steps_completed": s["load_steps_completed"],
            "substeps_completed": s["substeps_completed"], "not_converged_messages": s["not_converged_messages"],
            "bisections": mntr["bisected_substeps"] if mntr else s["bisection_messages"],
            "max_iterations_per_substep": mntr["max_iterations"] if mntr else None,
            "equilibrium_iterations": s["equilibrium_iterations"][:200]}


# ================================================================================================ S01
def _s01_checks(p):
    return [_close("revision", "/rev", REVISION, atol=1e-6, note="Mechanical APDL 2026 R1 reports revision 26.1")]


def _s01_post(work, p):
    kv = _kv(work)
    return {"rev": kv.get("rev"), "processors_reported": kv.get("nproc")}


# ================================================================================================ S02
S02_PARAMS = {
    "E_MOD": _p(1e9, "Pa", "Young's modulus", 1e3, 1e13),
    "NU": _p(0.25, "-", "Poisson's ratio", 0.0, 0.49),
    "P_TOP": _p(1e6, "Pa", "pressure on the top face", 1.0, 1e10),
    "L_COL": _p(10.0, "m", "column height", 0.1, 1e4),
    "A_SIDE": _p(1.0, "m", "column width", 0.01, 1e4),
    "N_XY": _p(2, "-", "elements across", 1, 20, True),
    "N_Z": _p(10, "-", "elements along z", 1, 200, True),
}


def _s02_checks(p):
    uz = -p["P_TOP"] * p["L_COL"] / p["E_MOD"]
    return [
        _close("uz_top_min", "/uz_top_min", uz, rtol=1e-6, note="uz(top) = -pL/E"),
        _close("uz_top_max", "/uz_top_max", uz, rtol=1e-6, note="uniform over the top face"),
        _close("sz_min", "/sz_min", -p["P_TOP"], rtol=1e-6, note="SZ = -p"),
        _close("sz_max", "/sz_max", -p["P_TOP"], rtol=1e-6),
        _close("lateral_stress", "/sxy_abs_max", 0.0, atol=1e-6 * p["P_TOP"], note="SX = SY = 0"),
        _close("n_elem", "/n_elem", p["N_XY"] ** 2 * p["N_Z"], atol=0.5),
    ]


def _s02_post(work, p):
    kv = _kv(work)
    nodes = _join(_table(work, "vkm_nodes_xyz.txt", ["id", "x", "y", "z"]),
                  _table(work, "vkm_nodes_u.txt", ["id", "ux", "uy", "uz"]))
    return {**{k: kv[k] for k in ("uz_top_min", "uz_top_max", "sz_min", "sz_max", "n_node", "n_elem")},
            "sxy_abs_max": _abs_max(kv["sx_min"], kv["sx_max"], kv["sy_min"], kv["sy_max"]),
            "node_table_rows": len(nodes)}


# ================================================================================================ S03
S03_PARAMS = {
    "E1": _p(1e9, "Pa", "Young's modulus of the lower layer (MAT 1)", 1e3, 1e13),
    "E2": _p(4e9, "Pa", "Young's modulus of the upper layer (MAT 2)", 1e3, 1e13),
    "NU": _p(0.25, "-", "Poisson's ratio of both layers", 0.0, 0.49),
    "P_TOP": _p(1e6, "Pa", "pressure on the top face", 1.0, 1e10),
    "L1": _p(6.0, "m", "lower layer thickness", 0.1, 1e4),
    "L2": _p(4.0, "m", "upper layer thickness", 0.1, 1e4),
    "A_SIDE": _p(1.0, "m", "column width", 0.01, 1e4),
    "N_XY": _p(2, "-", "elements across", 1, 20, True),
    "N_Z1": _p(6, "-", "elements along z in the lower layer", 1, 200, True),
    "N_Z2": _p(4, "-", "elements along z in the upper layer", 1, 200, True),
}


def _s03_checks(p):
    m1 = _constrained_modulus(p["E1"], p["NU"])
    m2 = _constrained_modulus(p["E2"], p["NU"])
    ui = -p["P_TOP"] * p["L1"] / m1
    ut = -p["P_TOP"] * (p["L1"] / m1 + p["L2"] / m2)
    sx = p["NU"] / (1 - p["NU"]) * -p["P_TOP"]
    return [
        _close("uz_top_min", "/uz_top_min", ut, rtol=1e-6, note="uz(top) = -p(L1/M1 + L2/M2); nu = 0: -p(L1/E1 + L2/E2)"),
        _close("uz_top_max", "/uz_top_max", ut, rtol=1e-6),
        _close("uz_interface_min", "/uz_interface_min", ui, rtol=1e-6, note="uz(L1) = -p L1/M1"),
        _close("uz_interface_max", "/uz_interface_max", ui, rtol=1e-6),
        _close("sz_layer1_min", "/sz1_min", -p["P_TOP"], rtol=1e-6),
        _close("sz_layer1_max", "/sz1_max", -p["P_TOP"], rtol=1e-6),
        _close("sz_layer2_min", "/sz2_min", -p["P_TOP"], rtol=1e-6),
        _close("sz_layer2_max", "/sz2_max", -p["P_TOP"], rtol=1e-6),
        _close("sx_layers", "/sx_max_abs_err", 0.0, atol=1e-6 * p["P_TOP"], note="SX = nu/(1-nu) SZ in both zones"),
        _close("n_elem_mat2", "/n_elem_mat2", p["N_XY"] ** 2 * p["N_Z2"], atol=0.5,
               note="zone assignment by element centroid"),
    ]


def _s03_post(work, p):
    kv = _kv(work)
    keys = ("uz_top_min", "uz_top_max", "uz_interface_min", "uz_interface_max", "sz1_min", "sz1_max", "sz2_min",
            "sz2_max", "sx1_min", "sx1_max", "sx2_min", "sx2_max", "n_elem_mat2")
    sx = p["NU"] / (1 - p["NU"]) * -p["P_TOP"]
    err = _abs_max(*(kv[k] - sx for k in ("sx1_min", "sx1_max", "sx2_min", "sx2_max")))
    return {**{k: kv[k] for k in keys}, "sx_max_abs_err": err}


# ================================================================================================ S04 / S05
S04_PARAMS = {
    "E_MOD": _p(1e9, "Pa", "Young's modulus", 1e3, 1e13),
    "NU": _p(0.25, "-", "Poisson's ratio", 0.0, 0.49),
    "RHO": _p(2000.0, "kg/m3", "density", 1.0, 1e5),
    "G": _p(9.81, "m/s2", "gravity", 0.1, 100.0),
    "H_COL": _p(100.0, "m", "column height", 1.0, 1e4),
    "A_SIDE": _p(10.0, "m", "column width", 0.01, 1e4),
    "N_XY": _p(1, "-", "elements across", 1, 20, True),
    "N_Z": _p(20, "-", "elements along z", 1, 400, True),
}


def _s04_exact(p):
    m = _constrained_modulus(p["E_MOD"], p["NU"])
    rg = p["RHO"] * p["G"]
    return m, rg


def _s04_checks(p):
    m, rg = _s04_exact(p)
    ut = -rg * p["H_COL"] ** 2 / (2 * m)
    return [
        _close("uz_top_min", "/uz_top_min", ut, rtol=1e-6, note="uz(H) = -rho g H^2 (1+nu)(1-2nu) / (2E(1-nu))"),
        _close("uz_top_max", "/uz_top_max", ut, rtol=1e-6),
        _close("uz_profile", "/node_uz_max_rel_err", 0.0, atol=1e-6,
               note="uz(z) = -(rho g/M)(Hz - z^2/2) at every node, relative to |uz(H)|"),
        _close("sz_profile", "/elem_sz_max_rel_err", 0.0, atol=1e-6,
               note="SZ(zc) = -rho g (H - zc) at element centroids, relative to rho g H"),
        _close("k0_ratio", "/elem_k0_max_abs_err", 0.0, atol=1e-6, note="SX/SZ = SY/SZ = nu/(1-nu)"),
    ]


def _column_errors(work, p, with_nodes=True):
    m, rg = _s04_exact(p)
    h = p["H_COL"]
    k0 = p["NU"] / (1 - p["NU"])
    out: dict[str, Any] = {}
    elems = _table(work, "vkm_elems_zs.txt", ["id", "zc", "sx", "sz"])
    sy_rows = {}
    try:
        sy_rows = {int(r["id"]): r["sy"] for r in _table(work, "vkm_elems_sy.txt", ["id", "sy"])}
    except FileNotFoundError:
        pass
    sz_err = k0_err = 0.0
    for r in elems:
        exact = -rg * (h - r["zc"])
        sz_err = max(sz_err, abs(r["sz"] - exact) / (rg * h))
        if abs(r["sz"]) > 0:
            k0_err = max(k0_err, abs(r["sx"] / r["sz"] - k0))
            if int(r["id"]) in sy_rows:
                k0_err = max(k0_err, abs(sy_rows[int(r["id"])] / r["sz"] - k0))
    out.update({"elem_rows": len(elems), "elem_sz_max_rel_err": sz_err, "elem_k0_max_abs_err": k0_err})
    if with_nodes:
        nodes = _join(_table(work, "vkm_nodes_xyz.txt", ["id", "x", "y", "z"]),
                      _table(work, "vkm_nodes_u.txt", ["id", "ux", "uy", "uz"]))
        ut = abs(rg * h ** 2 / (2 * m))
        err = 0.0
        for r in nodes.values():
            if "z" in r and "uz" in r:
                exact = -(rg / m) * (h * r["z"] - r["z"] ** 2 / 2)
                err = max(err, abs(r["uz"] - exact) / ut)
        out.update({"node_rows": len(nodes), "node_uz_max_rel_err": err})
    return out


def _s04_post(work, p):
    kv = _kv(work)
    return {"uz_top_min": kv["uz_top_min"], "uz_top_max": kv["uz_top_max"], "n_node": kv["n_node"],
            "n_elem": kv["n_elem"], **_column_errors(work, p)}


def _s05_checks(p):
    m, rg = _s04_exact(p)
    u_ref = rg * p["H_COL"] ** 2 / (2 * m)
    return [
        _close("equilibrium", "/usum_max", 0.0, atol=1e-8 * u_ref,
               note="initial state equilibrates gravity: max |u| <= 1e-8 |uz(H)| of S04"),
        _close("sz_kept", "/elem_sz_max_rel_err", 0.0, atol=1e-6, note="SZ(zc) stays -rho g (H - zc)"),
        _close("k0_kept", "/elem_k0_max_abs_err", 0.0, atol=1e-6, note="SX/SZ stays nu/(1-nu)"),
    ]


def _s05_post(work, p):
    kv = _kv(work)
    return {"usum_max": kv["usum_max"], "uz_min": kv["uz_min"], "uz_max": kv["uz_max"],
            **_column_errors(work, p, with_nodes=False)}


# ================================================================================================ S06
S06_PARAMS = {
    "E_MOD": _p(1e9, "Pa", "Young's modulus", 1e3, 1e13),
    "NU": _p(0.25, "-", "Poisson's ratio", 0.0, 0.49),
    "L_COL": _p(10.0, "m", "column height", 0.1, 1e4),
    "A_SIDE": _p(1.0, "m", "column width", 0.01, 1e4),
    "N_XY": _p(2, "-", "elements across", 1, 20, True),
    "N_Z": _p(10, "-", "elements along z", 1, 200, True),
    "P1": _p(5e5, "Pa", "pressure at the end of load step 1", 1.0, 1e10),
    "P2": _p(1.5e6, "Pa", "pressure at the end of load step 2", 1.0, 1e10),
    "T1": _p(1.0, "s", "TIME at the end of load step 1", 1e-6, 1e9),
    "T2": _p(3.0, "s", "TIME at the end of load step 2 (> T1)", 1e-6, 1e9),
    "NS1": _p(2, "-", "substeps of load step 1", 1, 100, True),
    "NS2": _p(4, "-", "substeps of load step 2", 1, 100, True),
}


def _s06_pressure(p, t):
    if t <= p["T1"]:
        return p["P1"] * t / p["T1"]
    return p["P1"] + (p["P2"] - p["P1"]) * (t - p["T1"]) / (p["T2"] - p["T1"])


def _s06_checks(p):
    if p["T2"] <= p["T1"]:
        raise ToolFailure("INVALID_ARGUMENT", "S06 needs T2 > T1")
    return [
        _close("n_sets", "/n_set", p["NS1"] + p["NS2"], atol=0.5, note="one result set per substep"),
        _close("history", "/history_max_rel_err", 0.0, atol=1e-6, note="uz(top, t) = -p(t) L/E, p linear per step"),
        _close("times", "/time_max_abs_err", 0.0, atol=1e-9 * p["T2"], note="substep times T1/NS1, ..., T2"),
        _close("final", "/uz_top_final", -p["P2"] * p["L_COL"] / p["E_MOD"], rtol=1e-6),
    ]


def _s06_times(p):
    times = [p["T1"] * k / p["NS1"] for k in range(1, int(p["NS1"]) + 1)]
    times += [p["T1"] + (p["T2"] - p["T1"]) * k / p["NS2"] for k in range(1, int(p["NS2"]) + 1)]
    return times


def _s06_post(work, p):
    kv = _kv(work)
    rows = _table(work, "vkm_history.txt", ["set", "ls", "sb", "t", "umin", "umax"])
    expected_t = _s06_times(p)
    u_ref = abs(p["P2"] * p["L_COL"] / p["E_MOD"])
    hist_err = t_err = 0.0
    for i, r in enumerate(rows):
        exact = -_s06_pressure(p, r["t"]) * p["L_COL"] / p["E_MOD"]
        hist_err = max(hist_err, abs(r["umin"] - exact) / u_ref, abs(r["umax"] - exact) / u_ref)
        if i < len(expected_t):
            t_err = max(t_err, abs(r["t"] - expected_t[i]))
    return {"n_set": kv["n_set"], "history": rows, "history_max_rel_err": hist_err,
            "time_max_abs_err": t_err if len(rows) == len(expected_t) else math.inf,
            "uz_top_final": rows[-1]["umin"] if rows else None}


# ================================================================================================ S07
S07_PARAMS = {
    "E_MOD": _p(1e9, "Pa", "Young's modulus", 1e3, 1e13),
    "NU": _p(0.25, "-", "Poisson's ratio", 0.0, 0.49),
    "P_TOP": _p(1e6, "Pa", "constant pressure on the top face", 1.0, 1e10),
    "L_COL": _p(10.0, "m", "column height", 0.1, 1e4),
    "A_SIDE": _p(1.0, "m", "column width", 0.01, 1e4),
    "N_XY": _p(2, "-", "elements across", 1, 20, True),
    "N_Z": _p(10, "-", "elements along z", 1, 200, True),
    "C1": _p(1e-24, "Pa^-C2/s", "Norton coefficient (TOY)", 0.0, 1.0),
    "C2": _p(3.0, "-", "Norton stress exponent (TOY)", 1.0, 10.0),
    "T_EL": _p(1e-8, "s", "TIME of the elastic load step", 1e-12, 1.0),
    "T_END": _p(1000.0, "s", "TIME at the end of the creep step", 1e-3, 1e12),
    "NSUB": _p(10, "-", "substeps of the creep step", 1, 1000, True),
}


def _s07_rate(p):
    return p["C1"] * p["P_TOP"] ** p["C2"]


def _s07_checks(p):
    ecz = -_s07_rate(p) * (p["T_END"] - p["T_EL"])
    uz = -p["P_TOP"] * p["L_COL"] / p["E_MOD"] + ecz * p["L_COL"]
    return [
        _close("n_sets", "/n_set", 1 + p["NSUB"], atol=0.5),
        _close("creep_final", "/epcr_z_final", ecz, rtol=1e-3, note="EPCR_Z(T_END) = -C1 p^C2 (T_END - T_EL)"),
        _close("creep_history", "/epcr_max_rel_err", 0.0, atol=1e-3, note="linear growth in time at constant stress"),
        _close("incompressible", "/epcr_x_ratio_err", 0.0, atol=1e-3, note="EPCR_X = -EPCR_Z/2"),
        _close("uz_final", "/uz_top_final", uz, rtol=1e-3, note="uz(top) = -pL/E + EPCR_Z L"),
        _close("uz_history", "/uz_max_rel_err", 0.0, atol=1e-3),
    ]


def _s07_post(work, p):
    kv = _kv(work)
    rows = _table(work, "vkm_history.txt", ["set", "code", "a", "b", "c"])
    sets: dict[int, dict[str, float]] = {}
    for r in rows:
        d = sets.setdefault(int(r["set"]), {"set": int(r["set"])})
        if int(r["code"]) == 1:
            d.update({"t": r["a"], "epcr_z_min": r["b"], "epcr_z_max": r["c"]})
        else:
            d.update({"epcr_x_min": r["a"], "uz_min": r["b"], "uz_max": r["c"]})
    hist = [sets[k] for k in sorted(sets)]
    rate = _s07_rate(p)
    ecz_end = rate * (p["T_END"] - p["T_EL"])
    u_el = -p["P_TOP"] * p["L_COL"] / p["E_MOD"]
    u_end = abs(u_el - ecz_end * p["L_COL"])
    e_err = u_err = x_err = 0.0
    for d in hist:
        exact = -rate * max(0.0, d["t"] - p["T_EL"])
        e_err = max(e_err, abs(d["epcr_z_min"] - exact) / ecz_end, abs(d["epcr_z_max"] - exact) / ecz_end)
        u_exact = u_el + exact * p["L_COL"]
        u_err = max(u_err, abs(d["uz_min"] - u_exact) / u_end, abs(d["uz_max"] - u_exact) / u_end)
        if abs(d["epcr_z_min"]) > 1e-3 * ecz_end:
            x_err = max(x_err, abs(d["epcr_x_min"] / d["epcr_z_min"] + 0.5))
    last = hist[-1] if hist else {}
    return {"n_set": kv["n_set"], "history": hist, "epcr_z_final": last.get("epcr_z_min"),
            "epcr_max_rel_err": e_err, "uz_max_rel_err": u_err, "epcr_x_ratio_err": x_err,
            "uz_top_final": last.get("uz_min"), "solution": _solution_summary(work)}


# ================================================================================================ S08 (DPF)
def _s08_checks(p):
    return [
        _close("s02_dpf_vs_vget", "/s02/u_max_rel_diff", 0.0, atol=1e-9, note="DPF U = *VGET U at every node"),
        _close("s04_dpf_vs_vget", "/s04/u_max_rel_diff", 0.0, atol=1e-9),
        _close("s02_dpf_vs_prnsol", "/s02/prnsol_uz_max_rel_diff", 0.0, atol=1e-4,
               note="PRNSOL prints 5 significant digits"),
        _close("s04_dpf_vs_prnsol", "/s04/prnsol_uz_max_rel_diff", 0.0, atol=1e-4),
        _value("s02_same_nodes", "/s02/same_node_set", True),
        _value("s04_same_nodes", "/s04/same_node_set", True),
        _value("entry_context", "/dpf/context", "entry", note="DPF Entry context: no licence checkout"),
    ]


# ================================================================================================ S09
S09_PARAMS = {
    "W_HALF": _p(60.0, "m", "half-width of the block in x and y", 10.0, 1e4),
    "H_TOT": _p(60.0, "m", "block height (surface at z = H_TOT)", 10.0, 1e4),
    "H_EL": _p(5.0, "m", "element size (divides every dimension below)", 0.1, 100.0),
    "Z_SALT_BOT": _p(20.0, "m", "bottom of the creeping layer", 0.0, 1e4),
    "Z_SALT_TOP": _p(35.0, "m", "top of the creeping layer", 0.0, 1e4),
    "CH_HALF": _p(10.0, "m", "chamber half-width in x and y", 0.1, 1e4),
    "CH_Z1": _p(25.0, "m", "chamber floor", 0.0, 1e4),
    "CH_Z2": _p(30.0, "m", "chamber roof", 0.0, 1e4),
    "E_BASE": _p(5e9, "Pa", "base layer Young's modulus", 1e3, 1e13),
    "NU_BASE": _p(0.25, "-", "base layer Poisson's ratio", 0.0, 0.49),
    "RHO_BASE": _p(2500.0, "kg/m3", "base layer density", 1.0, 1e5),
    "E_SALT": _p(2e9, "Pa", "creeping layer Young's modulus", 1e3, 1e13),
    "NU_SALT": _p(0.3, "-", "creeping layer Poisson's ratio", 0.0, 0.49),
    "RHO_SALT": _p(2200.0, "kg/m3", "creeping layer density", 1.0, 1e5),
    "C1": _p(1e-24, "Pa^-C2/s", "Norton coefficient of the creeping layer (TOY)", 0.0, 1.0),
    "C2": _p(3.0, "-", "Norton stress exponent (TOY)", 1.0, 10.0),
    "E_COVER": _p(1e9, "Pa", "cover Young's modulus", 1e3, 1e13),
    "NU_COVER": _p(0.25, "-", "cover Poisson's ratio", 0.0, 0.49),
    "RHO_COVER": _p(2000.0, "kg/m3", "cover density", 1.0, 1e5),
    "G": _p(9.81, "m/s2", "gravity", 0.1, 100.0),
    "T_GEO": _p(1.0, "s", "TIME of the geostatic step", 1e-6, 1e9),
    "T_EXC": _p(2.0, "s", "TIME of the excavation step (> T_GEO)", 1e-6, 1e9),
    "T_END": _p(1002.0, "s", "TIME at the end of the creep step (> T_EXC)", 1e-6, 1e12),
    "NSUB": _p(10, "-", "substeps of the creep step", 2, 1000, True),
}
S09_MODEL_CHOICES = [
    "lithostatic initial stress K0 = 1 in every layer (INISTATE at element centroids)",
    "excavation as instantaneous EKILL in one load step (ESTIF default)",
    "Norton creep only in the middle layer; cover and base linear elastic",
    "rollers on the lateral faces, fixed bottom; no pore pressure, no temperature effect",
]


def _s09_geometry(p):
    h = p["H_EL"]
    for name in ("W_HALF", "H_TOT", "Z_SALT_BOT", "Z_SALT_TOP", "CH_HALF", "CH_Z1", "CH_Z2"):
        k = p[name] / h
        if abs(k - round(k)) > 1e-9:
            raise ToolFailure("INVALID_ARGUMENT", f"S09: {name} must be a multiple of H_EL")
    if not (0 < p["Z_SALT_BOT"] <= p["CH_Z1"] < p["CH_Z2"] <= p["Z_SALT_TOP"] < p["H_TOT"]):
        raise ToolFailure("INVALID_ARGUMENT", "S09 needs 0 < Z_SALT_BOT <= CH_Z1 < CH_Z2 <= Z_SALT_TOP < H_TOT")
    if not p["CH_HALF"] < p["W_HALF"] or not p["T_GEO"] < p["T_EXC"] < p["T_END"]:
        raise ToolFailure("INVALID_ARGUMENT", "S09 needs CH_HALF < W_HALF and T_GEO < T_EXC < T_END")
    n_ch = round(2 * p["CH_HALF"] / h) ** 2 * round((p["CH_Z2"] - p["CH_Z1"]) / h)
    n_el = round(2 * p["W_HALF"] / h) ** 2 * round(p["H_TOT"] / h)
    return n_ch, n_el


def _s09_checks(p):
    n_ch, _n_el = _s09_geometry(p)
    return [
        _value("no_errors", "/solution/errors", 0),
        _value("load_steps", "/solution/load_steps_completed", 3),
        _value("substeps", "/solution/substeps_completed", int(p["NSUB"]) + 2),
        _value("no_bisection", "/solution/bisections", 0),
        _value("no_divergence", "/solution/not_converged_messages", 0),
        _close("n_chamber", "/n_chamber", n_ch, atol=0.5, note="chamber elements selected by centroid"),
        _close("equilibrium_ls1", "/equilibrium_usum_max_ls1", 0.0, atol=1e-6,
               note="lithostatic initial state + gravity: max |u| after LS1 below 1 micrometre"),
        _close("symmetry", "/symmetry_rel_err", 0.0, atol=1e-6, note="w(x,y) = w(-x,y) = w(x,-y) = w(y,x)"),
        _value("centre_is_max", "/centre_is_max", True, note="the trough maximum is above the chamber centre"),
        _value("subsidence_down", "/subsidence_positive", True),
        _value("monotonic", "/monotonic_growth", True, note="the maximum grows with every creep substep"),
    ]


def _s09_post(work, p):
    kv = _kv(work)
    rows = _table(work, "vkm_history.txt", ["set", "code", "a", "b", "c"])
    sets: dict[int, dict[str, float]] = {}
    for r in rows:
        d = sets.setdefault(int(r["set"]), {"set": int(r["set"])})
        if int(r["code"]) == 1:
            d.update({"load_step": int(r["a"]), "t": r["b"], "uz_centre": r["c"]})
        else:
            d.update({"usum_max": r["a"], "uz_surface_min": r["b"], "uz_surface_max": r["c"]})
    hist = [sets[k] for k in sorted(sets)]
    for d in hist:
        d["subsidence_max"] = -d["uz_surface_min"]
    creep = [d["subsidence_max"] for d in hist if d["load_step"] == 3]
    exc = [d["subsidence_max"] for d in hist if d["load_step"] == 2]
    series = exc[-1:] + creep
    monotonic = len(creep) >= 2 and all(b > a for a, b in zip(series, series[1:]))
    ls1 = [d for d in hist if d["load_step"] == 1]
    nodes = _join(_table(work, "vkm_nodes_xyz.txt", ["id", "x", "y", "z"]),
                  _table(work, "vkm_nodes_uz_last.txt", ["id", "uz"]))
    top = {(round(r["x"], 6), round(r["y"], 6)): r["uz"] for r in nodes.values()
           if "z" in r and "uz" in r and abs(r["z"] - p["H_TOT"]) < 1e-6}
    wmax = max((abs(v) for v in top.values()), default=0.0)
    sym = 0.0
    for (x, y), uz in top.items():
        for twin in ((-x, y), (x, -y), (y, x)):
            key = (round(twin[0] + 0.0, 6), round(twin[1] + 0.0, 6))
            if key in top:
                sym = max(sym, abs(uz - top[key]))
            else:
                sym = math.inf
    centre = top.get((0.0, 0.0))
    last = hist[-1] if hist else {}
    return {
        "n_set": kv["n_set"], "n_chamber": kv["n_chamber"], "history": hist, "solution": _solution_summary(work),
        "equilibrium_usum_max_ls1": ls1[-1]["usum_max"] if ls1 else math.inf,
        "surface_nodes": len(top), "subsidence_final_m": -last.get("uz_surface_min", 0.0),
        "subsidence_after_excavation_m": exc[-1] if exc else None,
        "subsidence_positive": bool(last) and -last["uz_surface_min"] > 0,
        "symmetry_rel_err": (sym / wmax) if wmax > 0 else math.inf,
        "centre_is_max": centre is not None and wmax > 0 and abs(abs(centre) - wmax) <= 1e-12 * wmax,
        "monotonic_growth": monotonic,
        "model_choices": S09_MODEL_CHOICES,
    }


STEPS: dict[str, Step] = {
    "S01": Step("S01", "installation and version", "mapdl", {}, _s01_checks, _s01_post,
                "*GET ACTIVE REV = 26.1, exit 0", default_timeout_s=300.0),
    "S02": Step("S02", "minimal mechanics: uniaxial stress column", "mapdl", S02_PARAMS, _s02_checks, _s02_post,
                "uz(top) = -pL/E, SZ = -p, SX = SY = 0 (rtol 1e-6)"),
    "S03": Step("S03", "material zones: two-layer column", "mapdl", S03_PARAMS, _s03_checks, _s03_post,
                "uniaxial strain: uz(top) = -p(L1/M1 + L2/M2) (nu = 0: -p(L1/E1 + L2/E2)), uz(L1) = -pL1/M1, "
                "SZ = -p, SX = nu/(1-nu) SZ in both zones (rtol 1e-6)"),
    "S04": Step("S04", "gravity: uniaxial strain column", "mapdl", S04_PARAMS, _s04_checks, _s04_post,
                "SZ(z) = -rho g (H - z), SX = nu/(1-nu) SZ, uz(z) = -(rho g/M)(Hz - z^2/2) (1e-6)"),
    "S05": Step("S05", "initial state (INISTATE) plus gravity", "mapdl", S04_PARAMS, _s05_checks, _s05_post,
                "equilibrium: max |u| <= 1e-8 |uz(H)| of S04; stresses keep the initial field (1e-6)"),
    "S06": Step("S06", "load steps and substeps with TIME", "mapdl", S06_PARAMS, _s06_checks, _s06_post,
                "uz(top, t) = -p(t) L/E for every substep of two ramped load steps (1e-6)"),
    "S07": Step("S07", "rheology: Norton creep (TB,CREEP TBOPT 10)", "mapdl", S07_PARAMS, _s07_checks, _s07_post,
                "EPCR_Z(t) = -C1 p^C2 (t - T_EL), EPCR_X = -EPCR_Z/2, uz(top, t) (rtol 1e-3)"),
    "S08": Step("S08", "result extraction: DPF vs *VGET and PRNSOL on S02 and S04", "dpf", {}, _s08_checks, None,
                "DPF nodal U equals *VGET to 1e-9 and PRNSOL to print precision; Entry context (no licence)",
                default_timeout_s=900.0, inputs=("s02_job", "s04_job")),
    "S09": Step("S09", "integrated toy case: layers, excavation (EKILL), creep, surface trough", "mapdl",
                S09_PARAMS, _s09_checks, _s09_post,
                "zero errors, converged without bisection, equilibrium after LS1, symmetric trough with the maximum "
                "above the chamber, monotonic growth in time", model_choices=S09_MODEL_CHOICES,
                default_timeout_s=1800.0),
}


# ================================================================================================ API
def list_steps() -> list[dict[str, Any]]:
    out = []
    for s in STEPS.values():
        out.append({"step": s.step, "title": s.title, "kind": s.kind, "checks": s.checks_text,
                    "deck": f"ladder/{s.step}.inp" if s.kind == "mapdl" else None, "inputs": list(s.inputs),
                    "default_timeout_s": s.default_timeout_s, "model_choices": s.model_choices,
                    "params": {k: {"default": v.default, "unit": v.unit, "note": v.note,
                                   "status": PARAM_STATUS, "scope": PARAM_SCOPE,
                                   **({"min": v.lo} if v.lo > -math.inf else {}),
                                   **({"max": v.hi} if v.hi < math.inf else {}),
                                   **({"integer": True} if v.integer else {})}
                               for k, v in s.params.items()}})
    return out


def get_step(step: str) -> Step:
    if step not in STEPS:
        raise ToolFailure("INVALID_ARGUMENT", f"unknown ladder step {step!r}; steps: {sorted(STEPS)}")
    return STEPS[step]


def resolve_params(step: str, overrides: Mapping[str, Any] | None) -> dict[str, float]:
    s = get_step(step)
    overrides = {k: v for k, v in dict(overrides or {}).items() if k not in s.inputs}
    unknown = sorted(set(overrides) - set(s.params))
    if unknown:
        raise ToolFailure("INVALID_ARGUMENT", f"unknown parameters for {step}: {unknown}",
                          details={"allowed": sorted(s.params) + list(s.inputs)})
    out: dict[str, float] = {}
    for name, spec in s.params.items():
        value = overrides.get(name, spec.default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ToolFailure("INVALID_ARGUMENT", f"{step}.{name} must be a finite number")
        value = float(value)
        if spec.integer and value != int(value):
            raise ToolFailure("INVALID_ARGUMENT", f"{step}.{name} must be an integer")
        if not spec.lo <= value <= spec.hi:
            raise ToolFailure("INVALID_ARGUMENT", f"{step}.{name} = {value} outside [{spec.lo}, {spec.hi}]")
        out[name] = value
    s.checks(out)                                        # step-specific consistency (raises INVALID_ARGUMENT)
    return out


def param_records(step: str, params: Mapping[str, float]) -> list[dict[str, Any]]:
    s = get_step(step)
    return [{"name": k, "value": params[k], "unit": s.params[k].unit, "status": PARAM_STATUS, "scope": PARAM_SCOPE,
             "source_ref": PARAM_SOURCE, "note": s.params[k].note} for k in s.params]


def _fmt(value: float, integer: bool) -> str:
    return str(int(value)) if integer else repr(float(value))


def deck_text(step: str, params: Mapping[str, float]) -> str:
    s = get_step(step)
    if s.kind != "mapdl":
        raise ToolFailure("INVALID_ARGUMENT", f"{step} is a {s.kind} step and has no deck")
    body = resources.files("vkm_ansys.ladder").joinpath(f"{step}.inp").read_text(encoding="utf-8")
    head = [f"! VKM solver ladder {step}: {s.title}",
            "! generated by vkm_ansys.ladder; parameters are TOY values (status ENGINEERING_ASSUMPTION, scope TOY),",
            "! not SKRU-1 or salt parameters. Results are MODEL_RESULT."]
    for name, spec in s.params.items():
        head.append(f"{name} = {_fmt(params[name], spec.integer)}   ! {spec.unit}; {spec.note}")
    text = "\n".join(head) + "\n" + body
    if not text.isascii():
        raise ToolFailure("INTERNAL", f"deck of {step} is not ASCII")
    return text


def expected_checks(step: str, params: Mapping[str, float]) -> list[dict[str, Any]]:
    return get_step(step).checks(dict(params))


def write_results(job_dir: Path, step: str, results: dict[str, Any]) -> dict[str, Any]:
    results = {"schema": "vkm-ansys.ladder_results/1", "step": step, "result_status": "MODEL_RESULT", **results}
    out = Path(job_dir) / "out"
    out.mkdir(parents=True, exist_ok=True)
    text = json.dumps(results, ensure_ascii=False, indent=1, sort_keys=True, default=_json_default)
    (out / RESULTS_NAME).write_text(text + "\n", encoding="utf-8")
    return {"results": f"out/{RESULTS_NAME}", "keys": sorted(k for k in results if k != "schema")[:60]}


def _json_default(obj: Any) -> Any:
    return str(obj)


def post_process(step: str, job_dir: Path, params: Mapping[str, Any], env: Mapping[str, str] | None = None
                 ) -> dict[str, Any]:
    """Read the deck's files in ``work/`` and write ``out/results.json``; returns a short summary."""
    s = get_step(step)
    if s.post is None:
        raise ToolFailure("INVALID_ARGUMENT", f"{step} has no deck post-processing")
    resolved = resolve_params(step, params)
    results = s.post(Path(job_dir) / "work", resolved)
    results = {k: (None if isinstance(v, float) and not math.isfinite(v) else v) for k, v in results.items()} | {
        "nonfinite": sorted(k for k, v in results.items() if isinstance(v, float) and not math.isfinite(v))}
    return write_results(job_dir, step, results)

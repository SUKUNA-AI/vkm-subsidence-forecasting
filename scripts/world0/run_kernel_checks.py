"""Per-kernel checks of World-0 local laws against MAPDL (small sub-models, analytic/semi-analytic references).

KC1  one plane-strain element, free lateral face, Norton creep: MAPDL vs ``laws.plane_strain_norton_element`` and the
     steady factor (√3/2)^(n+1) of the pillar kernel (W0-L-02a).
KC2  one laterally confined (oedometric) element, Norton creep: deviator relaxation and vertical strain vs the closed
     form W0-L-12 (salt layer kernel; isochoric creep locked under confinement).
KC3  long circular opening (thick cylinder a/R = 1/30), Norton creep: steady closure rate vs W0-L-13 (void kernel law).
Units: Pa, m, years. Synthetic loads; parameters = Transfer scenarios (VKM-SRC-045), not site facts.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import identification_setup  # noqa: E402,F401  (sets sys.path to src)

from vkm_world.representations.mapdl_batch import run_deck  # noqa: E402
from vkm_world.representations.world0 import laws  # noqa: E402
from vkm_world.representations.world0.mapdl_inputs import Material  # noqa: E402

SYLV_FIELD = Material("SYLV", 6.895e9, 0.2, 2140.7, 7.58e7, 5.0)
SYLV_LAB = Material("SYLV", 6.895e9, 0.2, 2140.7, 1.85e7, 5.0)
SALT_LAB = Material("PKS", 4.025e9, 0.3, 2242.6, 8.21e7, 5.0)
TIMES = [0.25, 0.5, 1, 2, 3, 5, 7.5, 10, 15, 20, 25, 30, 40, 50]


def _post_series(node: int, comp: str, extra: list[str] | None = None) -> list[str]:
    return ["FINISH", "/POST1", "*GET,NSETS,ACTIVE,0,SET,NSET", "*DIM,RES,ARRAY,NSETS,4", "*DO,K,1,NSETS",
            "SET,,,,,,,K", "*GET,TT,ACTIVE,0,SET,TIME", "RES(K,1)=TT", f"RES(K,2)=U{comp}({node})"] + (extra or []) + \
           ["*ENDDO", "*CFOPEN,series,txt", "*VWRITE,RES(1,1),RES(1,2),RES(1,3),RES(1,4)", "(4E20.11)", "*CFCLOS",
            "FINISH", "/EXIT,NOSAVE"]


def _creep_steps(times) -> list[str]:
    out = ["RATE,ON", "AUTOTS,ON", "CUTCONTROL,CRPLIMIT,0.05,1", "NEQIT,40"]
    prev = 1e-6
    for t in times:
        n = max(10, int(math.ceil((t - prev) / 0.05)))
        out += [f"TIME,{t}", f"NSUBST,{n},{1000 * n},{max(2, n // 4)}", "SOLVE"]
        prev = t
    return out


def deck_kc1(sigma=10e6, mat=SYLV_FIELD) -> str:
    L = ["/BATCH", "/PREP7", "ET,1,PLANE182", "KEYOPT,1,1,0", "KEYOPT,1,3,2"] + mat.apdl(1) + \
        ["N,1,0,0", "N,2,1,0", "N,3,1,1", "N,4,0,1", "MAT,1", "E,1,2,3,4", "D,1,ALL,0", "D,2,UY,0", "D,4,UX,0",
         f"F,3,FY,{-sigma / 2:.6e}", f"F,4,FY,{-sigma / 2:.6e}", "FINISH", "/SOLU", "ANTYPE,STATIC", "KBC,1",
         "OUTRES,ALL,ALL", "RATE,OFF", "TIME,1e-6", "NSUBST,1", "SOLVE"] + _creep_steps(TIMES)
    return "\n".join(L + _post_series(3, "Y")) + "\n"


def deck_kc2(sigma=7e6, mat=SALT_LAB) -> str:
    L = ["/BATCH", "/PREP7", "ET,1,PLANE182", "KEYOPT,1,1,0", "KEYOPT,1,3,2"] + mat.apdl(1) + \
        ["N,1,0,0", "N,2,1,0", "N,3,1,1", "N,4,0,1", "MAT,1", "E,1,2,3,4", "D,ALL,UX,0", "D,1,UY,0", "D,2,UY,0",
         f"F,3,FY,{-sigma / 2:.6e}", f"F,4,FY,{-sigma / 2:.6e}", "FINISH", "/SOLU", "ANTYPE,STATIC", "KBC,1",
         "OUTRES,ALL,ALL", "RATE,OFF", "TIME,1e-6", "NSUBST,1", "SOLVE"] + _creep_steps(TIMES)
    extra = ["ESEL,ALL", "*GET,SXV,ELEM,1,SMISC,1", "ETABLE,SXE,S,X", "ETABLE,SYE,S,Y", "*GET,SXX,ELEM,1,ETAB,SXE",
             "*GET,SYY,ELEM,1,ETAB,SYE", "RES(K,3)=SXX", "RES(K,4)=SYY"]
    return "\n".join(L + _post_series(3, "Y", extra)) + "\n"


def deck_kc3(p=15e6, a=2.0, R=60.0, mat=SYLV_LAB, nr=40, nt=18) -> tuple[str, dict]:
    rs = a * (R / a) ** (np.arange(nr + 1) / nr)
    th = np.linspace(0, math.pi / 2, nt + 1)
    L = ["/BATCH", "/PREP7", "ET,1,PLANE182", "KEYOPT,1,1,0", "KEYOPT,1,3,2"] + mat.apdl(1)
    nid = {}
    k = 0
    for j, t in enumerate(th):
        for i, r in enumerate(rs):
            k += 1
            nid[(i, j)] = k
            L.append(f"N,{k},{r * math.cos(t):.8f},{r * math.sin(t):.8f}")
    L.append("MAT,1")
    for j in range(nt):
        for i in range(nr):
            L.append(f"E,{nid[(i, j)]},{nid[(i + 1, j)]},{nid[(i + 1, j + 1)]},{nid[(i, j + 1)]}")
    L += ["NSEL,S,LOC,Y,-1e-6,1e-6", "D,ALL,UY,0", "NSEL,S,LOC,X,-1e-6,1e-6", "D,ALL,UX,0", "ALLSEL",
          "CSYS,1", f"NSEL,S,LOC,X,{R - 1e-4},{R + 1e-4}", "ESLN,S", f"SF,ALL,PRES,{p:.6e}", "ALLSEL", "CSYS,0",
          "FINISH", "/SOLU", "ANTYPE,STATIC", "KBC,1", "OUTRES,ALL,ALL", "RATE,OFF", "TIME,1e-6", "NSUBST,1", "SOLVE"]
    L += _creep_steps([0.001, 0.003, 0.01, 0.03, 0.1] + TIMES + [75, 100, 150, 200, 300, 400])
    return "\n".join(L + _post_series(nid[(0, 0)], "X")) + "\n", {"a": a, "R": R, "p": p}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--only", nargs="*")
    a = ap.parse_args()
    js = a.out / "kernel_checks.json"
    summary = json.loads(js.read_text(encoding="utf-8")) if js.exists() else {}
    names = [n for n in ("KC1", "KC2", "KC3") if not a.only or n in a.only]
    for name in names:
        cd = a.out / name
        cd.mkdir(parents=True, exist_ok=True)
        info = {}
        if name == "KC1":
            deck = deck_kc1()
        elif name == "KC2":
            deck = deck_kc2()
        else:
            deck, info = deck_kc3()
        (cd / "case.inp").write_text(deck, encoding="ascii")
        rec = run_deck(cd, n_proc=1)
        res = np.loadtxt(cd / "series.txt")
        t = res[:, 0]
        if name == "KC1":
            ref = laws.plane_strain_norton_element(t, 10e6, SYLV_FIELD.E, SYLV_FIELD.nu, SYLV_FIELD.eta_MPa_yr * 1e6, 5)
            m = res[:, 1]
            # late-time rate vs steady factor
            k = t > 20
            rate_fe = np.polyfit(t[k], m[k], 1)[0]
            rate_ss = -laws.plane_strain_free_factor(5) * float(laws.norton_rate(10e6, SYLV_FIELD.eta_MPa_yr * 1e6, 5))
            summary[name] = {"max_rel_err_vs_reference": float(np.max(np.abs(m - ref)) / np.max(np.abs(ref))),
                             "late_rate_fe_per_yr": rate_fe, "steady_rate_formula_per_yr": rate_ss,
                             "rate_ratio": rate_fe / rate_ss, "eps_yy_50yr_fe": float(m[-1]), "eps_yy_50yr_ref": float(ref[-1])}
        elif name == "KC2":
            q = -res[:, 3] + res[:, 2]            # σ_v − σ_h (compression +) = −SY + SX
            mat = SALT_LAB
            q_ref = laws.oedometric_norton_q(t - t[0], q[0], mat.E, mat.nu, mat.eta_MPa_yr * 1e6, 5)
            ev = -(res[:, 1] - res[0, 1])
            ev_ref = laws.oedometric_norton_vertical_strain(t - t[0], q[0], mat.E, mat.nu, mat.eta_MPa_yr * 1e6, 5)
            summary[name] = {"q0_Pa": float(q[0]), "q0_expected_Pa": 7e6 * (1 - 2 * mat.nu) / (1 - mat.nu),
                             "q_50yr_fe": float(q[-1]), "q_50yr_closed_form": float(q_ref[-1]),
                             "q_max_rel_err": float(np.max(np.abs(q - q_ref)) / q[0]),
                             "dev_50yr_fe": float(ev[-1]), "dev_50yr_closed_form": float(ev_ref[-1]),
                             "dev_rel_err": float(abs(ev[-1] - ev_ref[-1]) / abs(ev_ref[-1]))}
        else:
            u = -res[:, 1]
            k = t >= 300
            rate_fe = np.polyfit(t[k], u[k], 1)[0] / info["a"]
            k50 = (t >= 30) & (t <= 50)
            rate_fe_50 = np.polyfit(t[k50], u[k50], 1)[0] / info["a"]
            rate_an = laws.circular_opening_closure_rate(info["p"], SYLV_LAB.eta_MPa_yr * 1e6, 5,
                                                         a_over_R=info["a"] / info["R"])
            rate_inf = laws.circular_opening_closure_rate(info["p"], SYLV_LAB.eta_MPa_yr * 1e6, 5)
            summary[name] = {"late_closure_rate_fe_per_yr": rate_fe, "closure_rate_30_50yr_fe": rate_fe_50, "steady_rate_thick_cylinder_per_yr": rate_an,
                             "ratio": rate_fe / rate_an, "infinite_medium_rate_per_yr": rate_inf,
                             "u_wall_end_m": float(u[-1]), "t_end_yr": float(t[-1])}
        summary[name]["receipt"] = rec
        print(name, json.dumps({k: v for k, v in summary[name].items() if k != "receipt"}, ensure_ascii=False))
    (a.out / "kernel_checks.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()

"""World-0 identification: compliance C and surface operator S of the overburden/underburden (MAPDL, linear).

Usage: VKM_MAPDL_EXE=... python scripts/world0/run_compliance.py --out work/world0_20261023/compliance --cases PS_M05_B ...
Each case: deck → MAPDL batch (-np 4) → per-load-step tables → C, S, Sx (npz) + receipt (json).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from identification_setup import column_layers, material, sigma_v_at  # noqa: E402

from vkm_world.representations.mapdl_batch import run_deck  # noqa: E402
from vkm_world.representations.world0.mapdl_inputs import Layer, LayeredSlotModel, Material  # noqa: E402

ABOVE_IFACES = ("SZ_ABOVE|SKZ", "SKZ|PKS", "PKS|SMT", "SMT|TKT", "TKT|Q")
PROBES = ("SZ_ABOVE", "SKZ", "PKS", "SMT")      # tops of the water-protective strata layers and base of ТКТ


def a27_panel_nodes(n_cells: int = 22, a: float = 27.0, wp: float = 16.8, nch: int = 3, npil: int = 8):
    """Panel nodes for cells [half chamber | pillar | half chamber], exact 5.1/16.8/5.1 m (VKM-SRC-096 табл. 2)."""
    wc = (a - wp) / 2
    seg = np.concatenate([np.linspace(0, wc, nch + 1)[:-1], wc + np.linspace(0, wp, npil + 1)[:-1],
                          wc + wp + np.linspace(0, wc, nch + 1)[:-1]])
    x0 = -n_cells * a / 2
    xs = np.concatenate([x0 + j * a + seg for j in range(n_cells)] + [np.array([x0 + n_cells * a])])
    pillars = tuple((x0 + j * a + wc, x0 + j * a + wc + wp) for j in range(n_cells))
    return xs, pillars


def case_model(case: str) -> LayeredSlotModel:
    geo, scen, var = case.split("_")[:3]
    q_E = 0.02e9 if var == "QS" else None
    layers = column_layers(scen, q_E=q_E)
    if var == "HOMO":   # homogeneous reference: every layer = rock salt M05 (isolates the effect of layering)
        layers = [Layer(L.name, L.thickness, Material("HOMO", 4.025e9, 0.3, 2200.0), L.rows) for L in layers]
    fric = ()
    if var == "F":
        fric = ABOVE_IFACES
    elif var == "F1":
        fric = ("PKS|SMT",)
    if geo == "PS":
        if var in ("A27", "FINE27", "A27F", "FINE27F", "FINE27N", "FINE27FN"):
            xs, pillars = a27_panel_nodes()
            fine = var.startswith("FINE")
            seam_fric = ("SZ_BELOW|KRII", "KRII|SZ_ABOVE") if var.rstrip("N").endswith("F") else ()
            return LayeredSlotModel(layers, "KRII", panel=594.0, n_cells=22, half_width=1500.0, dx_max=20.0,
                                    x_panel=xs, pillar_intervals=pillars if fine else (),
                                    sigma0=sigma_v_at("KRII") if fine else 0.0, frictionless_interfaces=seam_fric,
                                    abutment_wall_loads=not var.endswith("N"))
        return LayeredSlotModel(layers, "KRII", panel=600.0, n_cells=30, half_width=1500.0, dx_in=2.5, dx_max=20.0,
                                frictionless_interfaces=fric, probe_layers=PROBES)
    if geo == "AX":
        return LayeredSlotModel(layers, "KRII", panel=300.0, n_cells=15, half_width=2000.0, dx_in=2.5, dx_max=20.0,
                                axisym=True, frictionless_interfaces=fric)
    raise ValueError(case)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--cases", nargs="+", required=True)
    ap.add_argument("--np", type=int, default=4)
    a = ap.parse_args()
    for case in a.cases:
        cd = a.out / case
        cd.mkdir(parents=True, exist_ok=True)
        model = case_model(case)
        (cd / "case.inp").write_text(model.deck(case), encoding="ascii")
        rec = run_deck(cd, n_proc=a.np)
        files = sorted(cd.glob("ls*.txt"))
        per_ls = [np.loadtxt(f) for f in files]
        if model.probe_layers and len(per_ls) == model.n_cells:
            P = np.stack([np.stack([np.loadtxt(cd / f"pr{j + 1:03d}_{q}.txt") for q in range(len(model.probe_layers))])
                          for j in range(model.n_cells)])        # (cells, probes, nx, 2)
            np.savez(cd / "probes.npz", P=P, layers=np.array(model.probe_layers),
                     depth_top=np.array([-model.rows_y[model.row_top[n]] for n in model.probe_layers]))
        rec.update(case=case, n_nodes=int(len(model.nodes)), n_elems=int(len(model.elems)),
                   n_load_steps=len(per_ls), depth_roof_m=float(model.depth_roof))
        if model.pillar_intervals and per_ls:
            arr = per_ls[0]
            np.savez(cd / "fine.npz", x=model.x, roof_uy=arr[:, 0], floor_uy=arr[:, 1], top_uy=arr[:, 2],
                     top_ux=arr[:, 3], cell_edges=model.cell_edges, sigma0=model.sigma0)
        elif len(per_ls) == model.n_cells:
            res = model.assemble(per_ls)
            np.savez(cd / "compliance.npz", **res, axisym=model.axisym, depth_roof=model.depth_roof,
                     unit_traction=model.unit_traction)
        (cd / "receipt.json").write_text(json.dumps(rec, indent=1, ensure_ascii=False), encoding="utf-8")
        print(case, rec["exit_code"], rec["n_errors"], rec["runtime_s"], "s", len(per_ls), "LS")


if __name__ == "__main__":
    main()

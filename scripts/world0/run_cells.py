"""World-0 pair experiments in the periodic unit cell (MAPDL): pillar–roof/floor, pillar–chamber–backfill, closure test.

Case names: CELL_<A27|B20>_<B|F|BF>_<EL|FIELD|LAB>[_<suffix>]
  B  — pillar bonded to roof/floor; F — frictionless pillar contacts (no separation); BF — bonded + backfill
  EL — elastic (instantaneous response only); FIELD/LAB — Norton viscosity scenario (Transfer VKM-SRC-045)
  suffix ELSLAB — roof/floor slabs elastic (creep only in the pillar); HIST — 1.5σ0 for 2 years, then σ0;
         E<MPa>G<mm> — backfill modulus and initial gap (e.g. E200G165 = 0.2 GPa, 0.165 m).
Usage: VKM_MAPDL_EXE=... python scripts/world0/run_cells.py --out work/world0_20261023/cells --cases ...
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from identification_setup import material, sigma_v_at  # noqa: E402

from vkm_world.representations.mapdl_batch import run_deck  # noqa: E402
from vkm_world.representations.world0.mapdl_inputs import Material, PillarCellModel  # noqa: E402

GEOM = {"A27": (27.0, 16.8), "B20": (20.0, 9.8)}     # spacing a, pillar width w (VKM-SRC-096 табл. 2: chamber 10.2 m)
H_SEAM = 5.5
TIMES = [0.1, 0.25, 0.5, 1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10, 12.5, 15, 17.5, 20, 25, 30, 35, 40]


def build(case: str) -> PillarCellModel:
    parts = case.split("_")
    _, geo, cont, rheo = parts[:4]
    suffix = parts[4] if len(parts) > 4 else ""
    a, w = GEOM[geo]
    creep = None if rheo == "EL" else rheo
    pillar = material("SYLV", "M05", creep, name="KRII")
    slab_creep = None if suffix == "ELSLAB" else creep
    roof = material("SYLV", "M05", slab_creep, name="SZ_ABOVE")
    floor = material("SYLV", "M05", slab_creep, name="SZ_BELOW")
    s0 = sigma_v_at("KRII")
    if creep is None:
        hist = []
    elif suffix == "HIST":
        hist = [(t, 1.5 * s0, max(8, int(t * 20))) for t in (0.1, 0.25, 0.5, 1.0, 1.5, 2.0)]
        hist += [(2.001, s0, 4)] + [(t, s0, 20) for t in TIMES if t > 2.0]
    else:
        hist = [(t, s0, 20) for t in TIMES]
    fill = None
    fill_h = None
    m = re.match(r"E(\d+)G(\d+)", suffix)
    if cont == "BF":
        if not m:
            raise ValueError("backfill case needs E<MPa>G<mm>")
        E_b = float(m.group(1)) * 1e6
        gap = float(m.group(2)) / 1000.0
        fill = Material("FILL", E_b, 0.25, 1700.0)
        fill_h = H_SEAM - gap
    soft = case.endswith("_SOFTC")          # retry option: softer contact penalty, finer sub-steps
    return PillarCellModel(a=a, w=w, h=H_SEAM, t_roof=40.0, t_floor=40.0, pillar=pillar, roof=roof, floor=floor,
                           sigma0=s0, history=hist, roof_contact="frictionless" if cont == "F" else "bonded",
                           fill=fill, fill_height=fill_h, contact_fkn=0.1 if soft else None,
                           max_substep_factor=400 if soft else 50, crplimit=0.1 if soft else 0.2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--cases", nargs="+", required=True)
    ap.add_argument("--np", type=int, default=4)
    a = ap.parse_args()
    for case in a.cases:
        cd = a.out / case
        cd.mkdir(parents=True, exist_ok=True)
        model = build(case)
        (cd / "case.inp").write_text(model.deck(case), encoding="ascii")
        rec = run_deck(cd, n_proc=a.np, timeout_s=3600)
        rec.update(case=case, n_nodes=int(len(model.nodes)), n_elems=int(len(model.elems) + len(model.fill_elems)),
                   sigma0_Pa=model.sigma0, a=model.a, w=model.w, h=model.h)
        hp = cd / "history.txt"
        if hp.exists():
            res = PillarCellModel.parse_history(hp.read_text())
            np.savez(cd / "history.npz", **res)
            rec["n_sets"] = int(len(res["t"]))
            rec["last"] = {k: float(v[-1]) for k, v in res.items()}
        (cd / "receipt.json").write_text(json.dumps(rec, indent=1, ensure_ascii=False), encoding="utf-8")
        print(case, rec["exit_code"], rec["n_errors"], rec["runtime_s"], "s", rec.get("last"), flush=True)


if __name__ == "__main__":
    main()

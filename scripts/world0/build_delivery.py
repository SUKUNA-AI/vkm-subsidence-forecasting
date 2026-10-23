"""Collect receipts, summaries, figures and raw tables of the World-0 microkernel runs into the PRIVATE delivery.

Usage: VKM_RESOURCES_ROOT=<PRIVATE> [VKM_7Z=<7z executable>] python scripts/world0/build_delivery.py \
           --work work/world0_20261023 --name microkernels_20261023
Writes 12_work_exports/<name>/ : RESULT_MICROKERNELS_RU.md, results/*.json, figures/*.png,
ladder_receipts_world0.json, raw_world0_tables.7z, MANIFEST.json. Paths in the receipts are relative.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import identification_setup as setup  # noqa: E402


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def mapdl_version(out: Path) -> str | None:
    if not out.exists():
        return None
    m = re.search(r"Ansys MAPDL\s+(\S+\s+\S+)\s+Build\s+(\S+)", out.read_text(errors="ignore"))
    return f"{m.group(1)} build {m.group(2)}" if m else None


def receipts(work: Path) -> list[dict]:
    out = []
    for cd in sorted((work / "compliance").iterdir()):
        r = json.loads((cd / "receipt.json").read_text(encoding="utf-8")) if (cd / "receipt.json").exists() else None
        if r is None:
            continue
        chk = {}
        if (cd / "compliance.npz").exists():
            dz = np.load(cd / "compliance.npz")
            C = dz["C"]
            if bool(dz["axisym"]):          # reciprocity holds for A_i·C_ij (ring areas), not for C itself
                e = dz["cell_edges"]
                C = np.diag(np.pi * (e[1:] ** 2 - e[:-1] ** 2)) @ C
            chk["betti_asymmetry"] = float(np.max(np.abs(C - C.T)) / np.max(np.abs(C)))
            # plane strain: loads and averages use the same weights → machine precision; axisymmetry: consistent ring
            # loads (2r_a + r_b)/6 vs r-weighted trapezoid averaging differ at O(Δr/r) → discretisation-level limit
            lim = 1e-3 if bool(dz["axisym"]) else 1e-8
            chk["betti_limit"] = lim
            chk["betti_ok"] = chk["betti_asymmetry"] < lim
        chk["outputs_present"] = (cd / "compliance.npz").exists() or (cd / "fine.npz").exists()
        steps = [3, 4, 5, 8, 10] + ([9] if "FINE" in cd.name else [])
        out.append({"run": f"compliance/{cd.name}", "ladder_steps": steps, "mapdl": mapdl_version(cd / "case.out"),
                    "input": "case.inp", "input_sha256": r.get("input_sha256"), "command": r.get("command"),
                    "exit_code": r.get("exit_code"), "n_errors": r.get("n_errors"), "runtime_s": r.get("runtime_s"),
                    "check": chk, "status": "PASS" if r.get("exit_code") == 0 and r.get("n_errors") == 0
                    and chk["outputs_present"] and chk.get("betti_ok", True) else "FAIL",
                    "result_files_sha256": r.get("result_files_sha256", {})})
    for cd in sorted((work / "cells").iterdir()):
        rp = cd / "receipt.json"
        if not rp.exists():
            continue
        r = json.loads(rp.read_text(encoding="utf-8"))
        chk = {"history_present": (cd / "history.npz").exists()}
        if chk["history_present"] and "_BF_" not in cd.name:
            h = np.load(cd / "history.npz")
            k = (r["a"] - r["w"]) / r["a"]
            trib = r["sigma0_Pa"] / (1 - k)
            sp = h["sigma_pillar"][h["t"] > 1e-3]
            sp = sp if len(sp) else h["sigma_pillar"][-1:]
            chk["tributary_rel_err"] = float(abs(np.median(sp) - trib) / trib)
            chk["tributary_ok"] = chk["tributary_rel_err"] < 1e-3
        ok = r.get("exit_code") == 0 and r.get("n_errors") == 0 and chk["history_present"] and chk.get("tributary_ok",
                                                                                                         True)
        out.append({"run": f"cells/{cd.name}", "ladder_steps": [3, 5, 6, 7, 8, 9], "mapdl": mapdl_version(cd / "case.out"),
                    "input": "case.inp", "input_sha256": r.get("input_sha256"), "command": r.get("command"),
                    "exit_code": r.get("exit_code"), "n_errors": r.get("n_errors"), "runtime_s": r.get("runtime_s"),
                    "check": chk, "status": "PASS" if ok else "NOT_CONVERGED" if r.get("not_converged") else "FAIL",
                    "result_files_sha256": r.get("result_files_sha256", {})})
    kc = json.loads((work / "kernel_checks" / "kernel_checks.json").read_text(encoding="utf-8"))
    tol = {"KC1": ("max_rel_err_vs_reference", 1e-3), "KC2": ("q_max_rel_err", 1e-3), "KC3": ("ratio", None)}
    for name, v in kc.items():
        r = v["receipt"]
        key, lim = tol[name]
        val = v[key]
        ok = (val <= lim) if lim is not None else abs(val - 1) < 0.05
        out.append({"run": f"kernel_checks/{name}", "ladder_steps": [2, 7],
                    "mapdl": mapdl_version(work / "kernel_checks" / name / "case.out"),
                    "input": "case.inp", "input_sha256": r.get("input_sha256"), "command": r.get("command"),
                    "exit_code": r.get("exit_code"), "n_errors": r.get("n_errors"), "runtime_s": r.get("runtime_s"),
                    "check": {key: val, "limit": lim if lim is not None else "|ratio−1| < 0.05"},
                    "status": "PASS" if ok and r.get("exit_code") == 0 else "FAIL",
                    "result_files_sha256": r.get("result_files_sha256", {})})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", type=Path, required=True)
    ap.add_argument("--name", default="microkernels_20261023")
    a = ap.parse_args()
    res = os.environ.get("VKM_RESOURCES_ROOT")
    if not res:
        print("set VKM_RESOURCES_ROOT", file=sys.stderr)
        return 2
    dst = Path(res) / "12_work_exports" / a.name
    (dst / "results").mkdir(parents=True, exist_ok=True)
    (dst / "figures").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "docs" / "worldspec" / "RESULT_MICROKERNELS_RU.md", dst / "RESULT_MICROKERNELS_RU.md")
    for f in ("influence_summary.json", "cells_summary.json", "analytic_pairs.json"):
        shutil.copyfile(a.work / "analysis" / f, dst / "results" / f)
    shutil.copyfile(a.work / "kernel_checks" / "kernel_checks.json", dst / "results" / "kernel_checks.json")
    for p in sorted((a.work / "figures").glob("*.png")):
        shutil.copyfile(p, dst / "figures" / p.name)
    rec = receipts(a.work)
    head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    ladder = {"schema": "vkm.world0.ladder_receipts/1", "date": "2026-10-23",
              "solver": "Ansys MAPDL 2026 R1 (batch, -np 4 / -np 1)", "public_code": {"commit": head,
              "paths": ["src/vkm_world/representations/", "scripts/world0/"]},
              "column": {"source": "v6_2 slice section.json (geometry_version 887a7f33…), chainage 1459 m",
                         "layers_bottom_to_top_m": setup.COLUMN_M, "status": "DERIVATION of INTERPOLATION"},
              "sigma_v_KrII_roof_Pa": setup.sigma_v_at("KRII"),
              "ladder_step_names": {"1": "installation & version (MAPDL header in every case.out)",
                                    "2": "minimal mechanics vs analytic", "3": "material zones", "4": "loads",
                                    "5": "boundary & initial conditions", "6": "time steps", "7": "rheology",
                                    "8": "result extraction", "9": "integrated toy case", "10": "adapter to a WorldSpec slice"},
              "runs": rec,
              "summary": {s: sum(1 for r in rec if r["status"] == s) for s in ("PASS", "FAIL", "NOT_CONVERGED")}}
    (dst / "ladder_receipts_world0.json").write_text(json.dumps(ladder, indent=1, ensure_ascii=False) + "\n",
                                                     encoding="utf-8")
    sevenz = os.environ.get("VKM_7Z", "7z")
    arc = dst / "raw_world0_tables.7z"
    if arc.exists():
        arc.unlink()
    lst = []
    for sub in ("compliance", "cells", "kernel_checks"):
        for p in sorted((a.work / sub).rglob("*")):
            if p.is_file() and p.suffix in (".inp", ".txt", ".npz", ".json", ".out", ".log", ".mntr"):
                lst.append(str(p.relative_to(a.work)))
    (a.work / "raw_list.txt").write_text("\n".join(lst) + "\n", encoding="utf-8")
    subprocess.run([sevenz, "a", "-t7z", "-mx=7", str(arc), "@raw_list.txt"], cwd=a.work, check=True,
                   stdout=subprocess.DEVNULL)
    files = [{"path": str(p.relative_to(dst)).replace(os.sep, "/"), "sha256": sha(p), "bytes": p.stat().st_size}
             for p in sorted(dst.rglob("*")) if p.is_file() and p.name != "MANIFEST.json"]
    (dst / "MANIFEST.json").write_text(json.dumps({"delivery": a.name, "date": "2026-10-23", "files": files},
                                                  indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(dst.name, len(files), "files;", ladder["summary"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

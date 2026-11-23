"""Exact P1 profiles along the reference trace (74–75–23–30) for the GPU ensemble.

Inputs: accepted v6_2 contact TIN (``contact_fields.npz``), the DSM TIN of the linked world (``scene.js``), the trace
of the reference slice (``section.json`` of G_solver/step10_slice) and, optionally, conditional realizations of the
geology ensemble (``realizations_subset_*.npz``: levels (R, 6, nodes) on the same TIN). Output ``profiles.npz``:
chainage at the union of all breakpoints (both TINs), DSM, v6_2 contacts and realization contacts there — between
breakpoints every field is linear, so linear interpolation of these samples is the exact P1 section anywhere.

Needs ``vkm_world.geometry_build.tin_section`` (PUBLIC branch with the geology build; pass ``--geometry-src``).
Status of the outputs: DERIVATION of INTERPOLATION geometry (v6_2) / of synthetic realizations; not observations.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from pathlib import Path

import numpy as np


def sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def decode(spec):
    return np.frombuffer(base64.b64decode(spec["data"]), dtype="<" + spec["dtype"]).reshape(spec["shape"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--contact-fields", type=Path, required=True)
    ap.add_argument("--scene", type=Path, required=True)
    ap.add_argument("--section-json", type=Path, required=True)
    ap.add_argument("--realizations", type=Path, nargs="*", default=[], help="realizations_subset_<variant>.npz")
    ap.add_argument("--geometry-src", type=Path, help="src/ of a PUBLIC checkout with geometry_build/tin_section.py")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    if a.geometry_src:
        sys.path.insert(0, str(a.geometry_src))
    from vkm_world.geometry_build.tin_section import section_through_tin

    z = np.load(a.contact_fields)
    gv = str(z["geometry_version"])
    sec_ref = json.loads(a.section_json.read_text(encoding="utf-8"))
    assert sec_ref["geometry_version"] == gv, (sec_ref["geometry_version"], gv)
    line = np.asarray(sec_ref["trace"]["polyline_sh1"], float)
    cols = [z["contacts"]]
    names = ["v6_2"]
    reals = []
    for p in a.realizations:
        r = np.load(p)
        lev = r["levels"]  # (R, 6, nodes)
        assert lev.shape[1] == 6 and lev.shape[2] == len(z["xy"])
        reals.append((p, lev))
        for k in range(lev.shape[0]):
            cols.append(lev[k].T)
            names.append(f"{p.stem}#{k}")
    V = np.concatenate(cols, axis=1).astype(float)
    sec = section_through_tin(z["xy"], z["triangles"], V, line)
    assert sec["gaps"] == 0 and np.isfinite(sec["values"]).all(), "trace leaves the geology TIN"
    txt = a.scene.read_text(encoding="utf-8")
    sc = json.loads(txt.split("=", 1)[1].strip().removesuffix(";"))
    del txt
    txy = decode(sc["terrain_xy"]).reshape(-1, 2).astype(float)
    tz = decode(sc["terrain_raw_z"]).ravel().astype(float)
    ttri = decode(sc["terrain_triangles"]).reshape(-1, 3)
    del sc
    dsec = section_through_tin(txy, ttri, tz, line)
    assert dsec["gaps"] == 0 and np.isfinite(dsec["values"]).all(), "trace leaves the DSM"
    chain = np.unique(np.round(np.concatenate([sec["chainage_m"], dsec["chainage_m"]]), 9))
    dsm = np.interp(chain, dsec["chainage_m"], dsec["values"][:, 0])
    vals = np.column_stack([np.interp(chain, sec["chainage_m"], sec["values"][:, k]) for k in range(V.shape[1])])
    v62 = vals[:, :6]
    R = (V.shape[1] - 6) // 6
    real = vals[:, 6:].reshape(len(chain), R, 6).transpose(1, 0, 2) if R else np.zeros((0, len(chain), 6))
    a.out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(a.out, chain=chain, dsm=dsm, v62=v62, realizations=real.astype(np.float64),
                        realization_names=np.array(names[1:]), contacts=np.array(["TKT", "SMT", "PKS", "SKZ", "SZ", "PDKS"]),
                        geometry_version=gv, polyline_sh1=line)
    receipt = {
        "schema": "vkm.gpu_ensemble.profiles/1", "geometry_version": gv, "operator": sec["operator"],
        "trace_length_m": float(chain[-1]), "breakpoints": {"geology": int(sec["breakpoints"]), "dsm": int(dsec["breakpoints"]),
                                                            "union": int(len(chain))},
        "inputs": {"contact_fields.npz": sha(a.contact_fields), "scene.js": sha(a.scene), "section.json": sha(a.section_json),
                   **{p.name: sha(p) for p, _ in reals}},
        "realizations": int(R), "output": {"profiles.npz": sha(a.out)},
        "status": "DERIVATION (exact P1 section of v6_2 INTERPOLATION geometry and of synthetic realizations); not observations",
    }
    a.out.with_suffix(".receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()

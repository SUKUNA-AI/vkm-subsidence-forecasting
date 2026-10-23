"""Export the World-0 kernel-type catalogue and law registry into the PRIVATE stream WORLD0_MICROKERNELS.

Both files carry no quotes (public-safe); they enter PUBLIC only through scripts/build_public_catalogues.py.
Usage: VKM_RESOURCES_ROOT=<PRIVATE clone> python scripts/world0/export_world0_catalogue.py
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from vkm_world.representations.world0.catalogue import catalogue_csv, catalogue_problems  # noqa: E402
from vkm_world.representations.world0.laws import LAW_SPECS  # noqa: E402


def registry_csv() -> str:
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["law_id", "name", "family", "status", "role", "equation", "evidence_ids", "note"])
    for s in LAW_SPECS.values():
        w.writerow([s.law_id, s.name, s.family, s.status, s.role, s.equation, ";".join(s.evidence_ids), s.note])
    return buf.getvalue()


def main() -> int:
    res = os.environ.get("VKM_RESOURCES_ROOT")
    if not res:
        print("set VKM_RESOURCES_ROOT", file=sys.stderr)
        return 2
    if catalogue_problems():
        print("catalogue problems:", catalogue_problems(), file=sys.stderr)
        return 1
    stream = Path(res) / "11_evidence_vnext" / "canonical" / "WORLD0_MICROKERNELS"
    stream.mkdir(parents=True, exist_ok=True)
    out = {"microkernel_types.csv": catalogue_csv(), "microkernel_law_registry.csv": registry_csv()}
    for name, text in out.items():
        (stream / name).write_bytes(text.encode("utf-8"))
        print(name, hashlib.sha256(text.encode("utf-8")).hexdigest()[:16])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

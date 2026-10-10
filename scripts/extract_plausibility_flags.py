#!/usr/bin/env python3
"""Plausibility sweep of the passport (10.10.2026): numbers from page extraction that set the ranges of the worlds and
have not been reviewed yet are flagged for a page-image review when

- ``OUT_OF_BOUNDS`` — the SI value lies outside physical bounds of its quantity (table ``BOUNDS``);
- ``FAR_FROM_MEDIAN`` — for C2–C4 the value is more than 10× from the median of its group (parameter × target, ≥ 5
  values); C1 thicknesses are not compared (a group mixes thin layers and thick units);
- ``HEADER_MULTIPLIER`` — a table-header multiplier was printed and not applied (the SI value is missing).

Flags are review requests, not verdicts. Output: one JSON line per flagged record.

Usage:  python scripts/extract_plausibility_flags.py --passport <WORLD_PASSPORT dir> --out <flags.jsonl>
        [--producers SOL_WAVE2_HIGH]
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

csv.field_size_limit(sys.maxsize)
BOUNDS = {"RHO": (1200, 3500), "UNIT_WEIGHT": (1200, 3500), "E": (5e6, 1.2e11), "EDEF": (1e6, 1.2e11),
          "NU": (0.0, 0.5), "UCS": (1e5, 3e8), "UTS": (1e4, 3e7), "COH": (1e3, 5e7), "PHI": (3, 65), "LTS": (1e5, 2e8),
          "LTS_RATIO": (0.05, 1.0), "LAMBDA": (0.1, 12), "SIGMA_V": (0, 1e8), "SIGMA_H": (0, 2e8),
          "THICKNESS": (0.01, 800), "DEPTH": (0, 2000), "CHAMBER_WIDTH": (1, 40), "PILLAR_WIDTH": (0.5, 80),
          "CHAMBER_HEIGHT": (0.5, 30), "EXTRACTION_RATIO": (0, 1), "BACKFILL_RATIO": (0, 1),
          "LOADING_DEGREE": (0, 1.5), "TEMPERATURE": (-10, 80)}
PRODUCERS = ("SOL_HIGH_FULL", "CLAUDE_DOUBLE_ENTRY")


def flags(ev: list[dict], producers=PRODUCERS) -> list[dict]:
    todo = [x for x in ev if x["producer"] in producers and not x["verification"].startswith("ADJUDICATED")
            and x["module"] in ("C1", "C2", "C3", "C4")]
    num = [x for x in todo if x["si_min"]]
    groups = defaultdict(list)
    for x in num:
        if x["module"] != "C1":
            groups[(x["parameter"], x["target"])].append(float(x["si_min"]))
    med = {k: statistics.median(v) for k, v in groups.items() if len(v) >= 5}
    out = []
    for x in num:
        p, lo, hi = x["parameter"], float(x["si_min"]), float(x["si_max"])
        why = []
        if p in BOUNDS and (lo < BOUNDS[p][0] or hi > BOUNDS[p][1]):
            why.append("OUT_OF_BOUNDS")
        m = med.get((p, x["target"]))
        if x["module"] != "C1" and m and m > 0 and lo > 0 and (lo / m > 10 or m / lo > 10):
            why.append("FAR_FROM_MEDIAN")
        if why:
            out.append({"why": why, "producer_record_id": x["producer_record_id"], "page_id": x["page_id"],
                        "parameter": p, "target": x["target"], "si_min": x["si_min"], "si_max": x["si_max"]})
    for x in todo:
        if x["conversion"] == "HEADER_MULTIPLIER_NOT_APPLIED" and x["module"] in ("C2", "C3"):
            out.append({"why": ["HEADER_MULTIPLIER"], "producer_record_id": x["producer_record_id"],
                        "page_id": x["page_id"], "parameter": x["parameter"], "target": x["target"],
                        "si_min": "", "si_max": ""})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--passport", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--producers", nargs="+", default=list(PRODUCERS),
                    help="producers whose unreviewed numbers are swept (e.g. SOL_WAVE2_HIGH for the second wave)")
    a = ap.parse_args()
    ev = list(csv.DictReader(open(a.passport / "passport_evidence.csv", encoding="utf-8")))
    fl = flags(ev, tuple(a.producers))
    with open(a.out, "w", encoding="utf-8", newline="\n") as f:
        for x in fl:
            f.write(json.dumps(x, ensure_ascii=False) + "\n")
    print(json.dumps({"flagged": len(fl), "by_reason": dict(Counter(w for x in fl for w in x["why"])),
                      "pages": len({x["page_id"] for x in fl})}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

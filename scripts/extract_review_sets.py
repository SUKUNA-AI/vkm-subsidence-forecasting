#!/usr/bin/env python3
"""World passport review (10.10.2026): the records an independent reviewer checks against the page before they set
the ranges of the worlds, grouped by page.

Item kinds:
- ``DISAGREEMENT`` — a number found by both the producer (Sol) and the double entry with a hard attribution difference
  (rock / layer, scale, site, parameter code); both records go to the reviewer;
- ``FACT_CHECK`` — producer or double-entry evidence behind a registry row with status FACT;
- ``SKRU1_RANGE`` — producer numbers attributed to SKRU-1 for world parameters, on pages without double entry;
- ``VKM_BRANCH_B`` — producer mechanics / initial-state numbers at massif, field or model scale (they set branch B),
  on pages without double entry.

Writes ``<out>/review_items.jsonl`` (one line per page: packet, items with the records) and ``<out>/batches/*.jsonl``.

Usage:  python scripts/extract_review_sets.py --work <extraction work dir> --passport <PRIVATE WORLD_PASSPORT>
        --out <dir> [--batches 8]
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vkm_world.extraction import compare as C          # noqa: E402

csv.field_size_limit(sys.maxsize)
HARD = ("material", "scale", "site_norm", "parameter_code")
WORLD_CODES = {"RHO", "UNIT_WEIGHT", "E", "EDEF", "NU", "UCS", "UTS", "COH", "PHI", "LTS", "LTS_RATIO", "EPS_UCS",
               "CREEP_RATE", "CREEP_STRAIN", "CREEP_LAW_PARAM", "VISCOSITY", "DAMAGE_PARAM", "LAMBDA", "SIGMA_V",
               "SIGMA_H", "TEMPERATURE", "PORE_PRESSURE", "THICKNESS", "DEPTH", "PROTECTIVE_LAYER", "CHAMBER_WIDTH",
               "CHAMBER_HEIGHT", "CHAMBER_LENGTH", "PILLAR_WIDTH", "AXIS_SPACING", "PANEL_WIDTH", "PANEL_LENGTH",
               "EXTRACTION_RATIO", "LOADING_DEGREE", "BACKFILL_RATIO", "BACKFILL_DELAY", "BACKFILL_DENSITY",
               "BACKFILL_STIFFNESS", "BACKFILL_COMPACTION", "MINING_DATE", "BACKFILL_DATE"}
MECH_STATE = {"E", "EDEF", "NU", "UCS", "UTS", "COH", "PHI", "LTS", "LTS_RATIO", "CREEP_RATE", "CREEP_LAW_PARAM",
              "VISCOSITY", "LAMBDA", "SIGMA_V", "SIGMA_H"}
KEEP = ("record_id", "kind", "parameter_code", "parameter", "symbol", "value_as_printed", "value_min", "value_max",
        "multiplier_as_printed", "unit_as_printed", "material_as_printed", "scale", "site_as_printed", "site_norm",
        "conditions", "method", "n_samples", "time_as_printed", "locator", "quote", "origin", "cited_ref",
        "entity_name", "notes")


def _jsonl(p: Path) -> list[dict]:
    return [json.loads(x) for x in open(p, encoding="utf-8")] if p.is_file() else []


def slim(r: dict) -> dict:
    return {k: r[k] for k in KEEP if r.get(k) not in (None, "")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--work", required=True, type=Path)
    ap.add_argument("--passport", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--batches", type=int, default=8)
    a = ap.parse_args()
    sol = _jsonl(a.work / "accept" / "high_full" / "accepted.jsonl")
    dbl = _jsonl(a.work / "accept" / "double" / "accepted.jsonl")
    pkt_of_page = {}
    for line in open(a.work / "packets.jsonl", encoding="utf-8"):
        m = json.loads(line)
        for pg in m["page_ids"]:
            if not m.get("split_of"):
                pkt_of_page.setdefault(pg, m["packet_id"])
    dbl_pages = {r["page_id"] for r in dbl} | {pg for pid in json.load(open(a.work / "double_entry_packets.json"))
                                                 for pg in json.load(open(a.work / "packets" / f"{pid}.json",
                                                                          encoding="utf-8"))["manifest"]["page_ids"]}
    items = defaultdict(list)                     # page → items
    seen = set()

    def add(kind, recs, why):
        key = (kind, tuple(r["record_id"] for r in recs))
        if key in seen:
            return
        seen.add(key)
        items[recs[0]["page_id"]].append({"kind": kind, "why": why, "records": [slim(r) for r in recs]})

    common = {r["page_id"] for r in sol} & dbl_pages
    pairs, _, _ = C.match([r for r in dbl if r["page_id"] in common], [r for r in sol if r["page_id"] in common])
    for d, s in pairs:
        hard = [f for f in C.attribution_diff(d, s) if f in HARD]
        if hard:
            add("DISAGREEMENT", [s, d], ";".join(hard))
    by_id = {r["record_id"]: r for r in sol + dbl}
    reg = list(csv.DictReader(open(a.passport / "world_passport.csv", encoding="utf-8")))
    ev = {r["ev_id"]: r for r in csv.DictReader(open(a.passport / "passport_evidence.csv", encoding="utf-8"))}
    for row in reg:
        if row["world_status"] != "FACT":
            continue
        for e in row["evidence_ids"].split(";"):
            x = ev.get(e)
            if x and x["producer_record_id"] in by_id:
                add("FACT_CHECK", [by_id[x["producer_record_id"]]], f"{row['passport_id']} {row['parameter']}")
    for r in sol:
        if r["page_id"] in dbl_pages or r.get("value_min") is None:
            continue
        code = r.get("parameter_code")
        if r.get("site_norm") == "SKRU1" and code in WORLD_CODES:
            add("SKRU1_RANGE", [r], code)
        elif code in MECH_STATE and r.get("scale") in ("MASSIF", "FIELD", "MODEL"):
            add("VKM_BRANCH_B", [r], code)
    pages = sorted(items, key=lambda p: (p.split(":")[0], p))
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "batches").mkdir(exist_ok=True)
    rows = [{"page_id": p, "packet_id": pkt_of_page.get(p, ""), "items": items[p]} for p in pages]
    with open(a.out / "review_items.jsonl", "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    total = sum(len(r["items"]) for r in rows)
    target = total / a.batches
    batches, cur, n = [], [], 0
    for r in rows:                                 # a source stays in one batch while the batch is not full
        if cur and n >= target and r["page_id"].split(":")[0] != cur[-1]["page_id"].split(":")[0]:
            batches.append(cur)
            cur, n = [], 0
        cur.append(r)
        n += len(r["items"])
    if cur:
        batches.append(cur)
    for i, b in enumerate(batches, 1):
        with open(a.out / "batches" / f"batch_{i:02d}.jsonl", "w", encoding="utf-8", newline="\n") as f:
            for r in b:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    kinds = defaultdict(int)
    for r in rows:
        for it in r["items"]:
            kinds[it["kind"]] += 1
    print(json.dumps({"pages": len(rows), "items": total, "by_kind": dict(kinds),
                      "batches": [sum(len(r["items"]) for r in b) for b in batches]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

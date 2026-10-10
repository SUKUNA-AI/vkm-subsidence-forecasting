#!/usr/bin/env python3
"""Self-check of a review (adjudication) file: every corrected record is re-verified against the page text of its
packet (numbers and unit printed on the page, quote on the page), the verdict vocabulary and the field names are
checked. Prints the failures; exit code 1 if there are any.

Review line: ``{"record_id", "page_id", "verdict": CORRECT|CORRECTED|REJECT|NOT_WORLD_PARAMETER, "corrected": {...},
"material_class", "value_scale_factor", "checked_on": TEXT|IMAGE, "basis", "confidence": HIGH|MEDIUM|LOW,
"preferred"}``.

Usage:  python scripts/extract_review_check.py --work <extraction work dir> --items <batch.jsonl> --review <out.jsonl>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vkm_world.extraction import schema as S             # noqa: E402
from vkm_world.extraction import verify as V             # noqa: E402

VERDICTS = {"CORRECT", "CORRECTED", "REJECT", "NOT_WORLD_PARAMETER"}
CLASSES = {"ROCKSALT", "SYLVINITE", "CARNALLITE", "CLAY_CONTACT", "TRANSITION_OVERBURDEN", "COVER", "BACKFILL",
           "SALT_GENERAL", "COLUMN", "UNCLASSIFIED"}
FIELDS = {"parameter_code", "parameter", "value_as_printed", "value_min", "value_max", "unit_as_printed",
          "multiplier_as_printed", "material_as_printed", "scale", "site_as_printed", "site_norm", "conditions",
          "method", "n_samples", "time_as_printed", "locator", "quote", "origin", "cited_ref"}
ENUMS = {"parameter_code": set(S.PARAMETER_CODES), "scale": set(S.SCALES), "site_norm": set(S.SITES),
         "origin": set(S.ORIGINS)}


def check(work: Path, items: Path, review: Path) -> list[str]:
    recs, page_packet = {}, {}
    for line in open(items, encoding="utf-8"):
        row = json.loads(line)
        for it in row["items"]:
            for r in it["records"]:
                recs[r["record_id"]] = dict(r, page_id=row["page_id"])
                page_packet[row["page_id"]] = row["packet_id"]
    texts = {}
    problems, seen = [], set()
    for n, line in enumerate(open(review, encoding="utf-8"), 1):
        try:
            v = json.loads(line)
        except ValueError as e:
            problems.append(f"line {n}: not JSON ({e})")
            continue
        rid = v.get("record_id")
        if rid not in recs:
            problems.append(f"line {n}: unknown record_id {rid}")
            continue
        seen.add(rid)
        if v.get("verdict") not in VERDICTS:
            problems.append(f"{rid}: verdict {v.get('verdict')!r}")
        if v.get("material_class") not in (None, "") and v["material_class"] not in CLASSES:
            problems.append(f"{rid}: material_class {v['material_class']!r}")
        corr = v.get("corrected") or {}
        bad = set(corr) - FIELDS
        if bad:
            problems.append(f"{rid}: unknown corrected fields {sorted(bad)}")
        for f, allowed in ENUMS.items():
            if f in corr and corr[f] not in allowed:
                problems.append(f"{rid}: {f} {corr[f]!r} not in the schema")
        if v.get("verdict") == "CORRECTED" and not corr and v.get("material_class") in (None, "") \
                and v.get("value_scale_factor") in (None, ""):
            problems.append(f"{rid}: CORRECTED without corrections")
        if v.get("verdict") in ("CORRECT", "CORRECTED"):
            pid = recs[rid]["page_id"]
            pkt = page_packet.get(pid)
            if pkt not in texts:
                texts[pkt] = json.loads((work / "packets" / f"{pkt}.json").read_text(encoding="utf-8"))["page_texts"]
            pages = {p: V.PageText(p, t) for p, t in texts[pkt].items()}
            merged = dict(recs[rid], **corr)
            why = V.check_record(merged, pages)
            if why:
                problems.append(f"{rid}: after corrections {why}")
    missing = sorted(set(recs) - seen)
    if missing:
        problems.append(f"{len(missing)} records without a verdict, e.g. {missing[:5]}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--work", required=True, type=Path)
    ap.add_argument("--items", required=True, type=Path)
    ap.add_argument("--review", required=True, type=Path)
    a = ap.parse_args()
    probs = check(a.work, a.items, a.review)
    print(json.dumps({"problems": len(probs), "details": probs[:80]}, ensure_ascii=False, indent=1))
    return 1 if probs else 0


if __name__ == "__main__":
    sys.exit(main())

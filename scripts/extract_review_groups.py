#!/usr/bin/env python3
"""Groups of extraction records for a page-image review workflow (10.10.2026): records named in a flags file
(``extract_plausibility_flags.py`` output, or any JSON lines with ``producer_record_id`` / ``record_id`` and ``why``)
are looked up in ``WORLD_EXTRACTION/<run>/accepted.jsonl``, joined with their passport row (SI value, target) and laid
out by source and page: at most ``--max-records`` records and ``--max-pages`` pages per group, pages of one source
together.

Output: ``<out>/group_NNN.json`` (``group``, ``pages`` → ``page_id``, ``packet_id``, ``items`` → ``record_id``, ``why``,
``record``, ``passport_si``, ``passport_target``) and ``<out>/index.json`` (``group``, ``source``, ``pages``, ``n``) —
the ``args`` of the review workflow.

Usage:  VKM_RESOURCES_ROOT=<PRIVATE> python scripts/extract_review_groups.py --flags <flags.jsonl> --runs high_full
        --out <dir> [--max-records 20] [--max-pages 10]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

csv.field_size_limit(sys.maxsize)


def _jsonl(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.is_file() else []


def layout(flags: list[dict], records: dict[str, dict], passport: dict[str, dict], max_records: int,
           max_pages: int) -> list[dict]:
    by_page: dict[str, list[dict]] = defaultdict(list)
    for f in flags:
        rid = f.get("producer_record_id") or f.get("record_id")
        rec = records.get(rid)
        if not rec:
            continue
        pv = passport.get(rid, {})
        by_page[rec["page_id"]].append({"record_id": rid, "why": f.get("why", []), "record": rec,
                                        "passport_si": [pv.get("si_min", ""), pv.get("si_max", ""), pv.get("si_unit", "")],
                                        "passport_target": pv.get("target", "")})
    groups, cur, cur_src = [], [], None

    def close():
        if cur:
            groups.append({"group": len(groups) + 1, "pages": list(cur)})
            cur.clear()

    def key(pid: str):
        src, _, p = pid.rpartition(":")
        return src, p
    for pid in sorted(by_page, key=key):
        src = key(pid)[0]
        items = sorted(by_page[pid], key=lambda x: x["record_id"])
        n_cur = sum(len(p["items"]) for p in cur)
        if cur and (src != cur_src or len(cur) >= max_pages or n_cur + len(items) > max_records):
            close()
        cur_src = src
        cur.append({"page_id": pid, "packet_id": items[0]["record"].get("packet_id", ""), "items": items})
    close()
    return groups


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--flags", required=True, type=Path)
    ap.add_argument("--runs", nargs="+", required=True)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--max-records", type=int, default=20)
    ap.add_argument("--max-pages", type=int, default=10)
    a = ap.parse_args()
    res = os.environ.get("VKM_RESOURCES_ROOT")
    if not res:
        print("set VKM_RESOURCES_ROOT", file=sys.stderr)
        return 2
    canon = Path(res) / "11_evidence_vnext" / "canonical"
    records = {r["record_id"]: r for run in a.runs for r in _jsonl(canon / "WORLD_EXTRACTION" / run / "accepted.jsonl")}
    passport = {r["producer_record_id"]: r for r in csv.DictReader(
        open(canon / "WORLD_PASSPORT" / "passport_evidence.csv", encoding="utf-8"))}
    groups = layout(_jsonl(a.flags), records, passport, a.max_records, a.max_pages)
    a.out.mkdir(parents=True, exist_ok=True)
    index = []
    for g in groups:
        (a.out / f"group_{g['group']:03d}.json").write_text(json.dumps(g, ensure_ascii=False, indent=1) + "\n",
                                                             encoding="utf-8", newline="\n")
        index.append({"group": g["group"], "source": g["pages"][0]["page_id"].rpartition(":")[0],
                      "pages": len(g["pages"]), "n": sum(len(p["items"]) for p in g["pages"])})
    (a.out / "index.json").write_text(json.dumps(index, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"groups": len(index), "records": sum(x["n"] for x in index)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

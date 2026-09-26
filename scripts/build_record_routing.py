"""Record routing index: which PUBLIC catalogues cite each sweep record (review finding COVERAGE_DUPLICATES-016).

The formula stream keeps a record → model map with MAPPED/EXCLUDED per record; the other streams do not. This index
is the mechanical part of that map for every kind: for each of the 13 572 records of ``record_index.csv`` it lists the
PUBLIC catalogues whose text cites the record's ``vn_id``.

routing:
  DOMAIN    cited by at least one domain catalogue (anything outside evidence/qa/);
  QA_ONLY   cited only by QA tables (OCR/visual ledger, corrections, review-fix logs);
  UNROUTED  cited by no PUBLIC catalogue. The record stays in PRIVATE sweep_raw with its quote; it did not reach a
            catalogue. Why (duplicate under another id, outside the stream's scope, not transferred) is NOT classified
            here — that needs a reading of each record (Phase-2 backlog).

Inputs: PUBLIC ``evidence/sources/record_index.csv`` and every CSV/JSON target of ``scripts/public_catalogue_map.json``.
Deterministic; no PRIVATE access.
Usage:  python scripts/build_record_routing.py [--check]
"""
from __future__ import annotations

import collections
import csv
import io
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = "evidence/sources/record_index.csv"
OUT = "evidence/sources/record_routing.csv"
VN_ID = re.compile(r"EV-VN-S\d{3}-\d{4}")
QA_PREFIX = "evidence/qa/"
COLUMNS = ["vn_id", "source_id", "kind", "pdf_page", "routing", "n_domain_catalogues", "domain_catalogues",
           "qa_catalogues"]


def build(root: Path = ROOT) -> str:
    mapping = json.loads((root / "scripts" / "public_catalogue_map.json").read_text(encoding="utf-8"))
    targets = sorted(t for t in set(mapping.values()) if t != INDEX)
    cited: dict[str, dict[str, set[str]]] = collections.defaultdict(lambda: {"domain": set(), "qa": set()})
    for t in targets:
        side = "qa" if t.startswith(QA_PREFIX) else "domain"
        for vn in set(VN_ID.findall((root / t).read_text(encoding="utf-8"))):
            cited[vn][side].add(t)
    with (root / INDEX).open(encoding="utf-8", newline="") as stream:
        index = list(csv.DictReader(stream))
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\n")
    w.writeheader()
    for r in index:
        c = cited.get(r["vn_id"], {"domain": set(), "qa": set()})
        routing = "DOMAIN" if c["domain"] else "QA_ONLY" if c["qa"] else "UNROUTED"
        w.writerow({"vn_id": r["vn_id"], "source_id": r["source_id"], "kind": r["kind"], "pdf_page": r["pdf_page"],
                    "routing": routing, "n_domain_catalogues": len(c["domain"]),
                    "domain_catalogues": ";".join(sorted(c["domain"])), "qa_catalogues": ";".join(sorted(c["qa"]))})
    return buf.getvalue()


def main() -> int:
    text = build()
    out = ROOT / OUT
    if "--check" in sys.argv[1:]:
        ok = out.exists() and out.read_text(encoding="utf-8") == text
        print(f"{OUT}: {'up to date' if ok else 'STALE — rerun scripts/build_record_routing.py'}")
        return 0 if ok else 1
    out.write_text(text, encoding="utf-8", newline="")
    rows = list(csv.DictReader(io.StringIO(text)))
    by = collections.Counter(r["routing"] for r in rows)
    print(f"{OUT}: {len(rows)} records; " + ", ".join(f"{k} {v}" for k, v in sorted(by.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())

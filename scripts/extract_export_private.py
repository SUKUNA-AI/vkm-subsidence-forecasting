#!/usr/bin/env python3
"""World passport extraction, step 2d: move the results of a run from the work directory into PRIVATE.

``11_evidence_vnext/canonical/WORLD_EXTRACTION/``: the page selection, packet manifests, task and schema, receipts;
per run ``<run>/accepted.jsonl``, ``rejected.jsonl`` (with reasons), ``summary.json``, ``run_meta.jsonl`` (one line
per packet: producer, sha256 of packet / task / schema / prompt, times, exit code, token usage); comparisons.
``12_work_exports/extraction_20261009/<run>_raw.zip``: the raw archive (prompt, ``--json`` stream, stderr, last
message, meta) of every packet, byte for byte.

Records keep their quotes, so this goes to PRIVATE only; the public projection is built by
``scripts/build_public_catalogues.py``.

Usage:  VKM_RESOURCES_ROOT=<PRIVATE> python scripts/extract_export_private.py --work <dir> --runs high_full double
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path

FIXED = ("pages.csv", "pages_receipt.json", "packets.jsonl", "packets_receipt.json", "task.txt", "schema.json",
         "double_entry_packets.json")


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--work", required=True, type=Path)
    ap.add_argument("--runs", nargs="+", required=True)
    ap.add_argument("--compare", nargs="*", default=[], help="compare/<A>__<B> directories to copy")
    a = ap.parse_args()
    res = os.environ.get("VKM_RESOURCES_ROOT")
    if not res:
        print("set VKM_RESOURCES_ROOT", file=sys.stderr)
        return 2
    canon = Path(res) / "11_evidence_vnext" / "canonical" / "WORLD_EXTRACTION"
    raw_dir = Path(res) / "12_work_exports" / "extraction_20261009"
    canon.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    receipt = {"rule_version": "extract_export_private_v1", "fixed": {}, "runs": {}, "compare": {}}
    for name in FIXED:
        src = a.work / name
        if src.is_file():
            shutil.copyfile(src, canon / name)
            receipt["fixed"][name] = sha(src)
    for run in a.runs:
        acc = a.work / "accept" / run
        dst = canon / run
        dst.mkdir(exist_ok=True)
        for name in ("accepted.jsonl", "rejected.jsonl", "summary.json"):
            shutil.copyfile(acc / name, dst / name)
        entry = {k: sha(dst / k) for k in ("accepted.jsonl", "rejected.jsonl", "summary.json")}
        if run != "double":
            root = a.work / "sol" / run
            metas = sorted(root.glob("*/meta.json"))
            with open(dst / "run_meta.jsonl", "w", encoding="utf-8", newline="\n") as f:
                for m in metas:
                    f.write(json.dumps(json.loads(m.read_text(encoding="utf-8")), ensure_ascii=False) + "\n")
            entry["run_meta.jsonl"] = sha(dst / "run_meta.jsonl")
            z = raw_dir / f"{run}_raw.zip"
            with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in sorted(root.rglob("*")):
                    if p.is_file():
                        zf.write(p, p.relative_to(root).as_posix())
            entry["raw_zip"] = {"path": f"12_work_exports/extraction_20261009/{z.name}", "sha256": sha(z),
                                "files": sum(1 for p in root.rglob("*") if p.is_file())}
        else:
            z = raw_dir / "double_raw.zip"
            with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in sorted((a.work / "double").glob("*.json")):
                    zf.write(p, p.name)
            entry["raw_zip"] = {"path": f"12_work_exports/extraction_20261009/{z.name}", "sha256": sha(z)}
        receipt["runs"][run] = entry
    for c in a.compare:
        src = a.work / "compare" / c
        dst = canon / "compare" / c
        dst.mkdir(parents=True, exist_ok=True)
        for p in src.iterdir():
            shutil.copyfile(p, dst / p.name)
        receipt["compare"][c] = {p.name: sha(dst / p.name) for p in sorted(dst.iterdir())}
    (canon / f"export_receipt_{'_'.join(a.runs)}.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

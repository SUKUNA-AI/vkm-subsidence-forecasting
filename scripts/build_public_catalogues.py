"""Build PUBLIC-safe catalogues from PRIVATE canonical catalogues.

PRIVATE canonical CSVs (with verbatim quotes) live in
``$VKM_RESOURCES_ROOT/11_evidence_vnext/canonical/``. This script copies the catalogues listed in
``scripts/public_catalogue_map.json`` into the PUBLIC tree, dropping forbidden text columns
(quote, verbatim_quote, ocr_text, page_text, full_text), rewrites machine-specific absolute paths to
logical names (``sanitize_paths``), and writes a manifest with SHA-256 of every
input and output. It never copies binaries. Deterministic: rows keep their canonical order.

Usage:  VKM_RESOURCES_ROOT=/path/to/resources python scripts/build_public_catalogues.py
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from vkm_world.governance.leakage import (  # noqa: E402
    BIBLIO_COLUMN_HINTS, FORBIDDEN_COLUMNS, VERBATIM_LIMIT_WORDS, longest_shared_run, quote_shingles, sanitize_paths, scan,
    words)

csv.field_size_limit(sys.maxsize)


def strip_forbidden_keys(obj, removed: list[str]):
    """Copy of a JSON value without keys named like verbatim/OCR text columns (at any depth)."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and k.strip().lower() in FORBIDDEN_COLUMNS:
                removed.append(k)
                continue
            out[k] = strip_forbidden_keys(v, removed)
        return out
    if isinstance(obj, list):
        return [strip_forbidden_keys(v, removed) for v in obj]
    return obj


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> int:
    res = os.environ.get("VKM_RESOURCES_ROOT")
    if not res:
        print("set VKM_RESOURCES_ROOT to the private resources repository", file=sys.stderr)
        return 2
    canon = Path(res) / "11_evidence_vnext" / "canonical"
    mapping = json.loads((ROOT / "scripts" / "public_catalogue_map.json").read_text(encoding="utf-8"))
    manifest = {"generator": "scripts/build_public_catalogues.py", "private_root_rel": "11_evidence_vnext/canonical",
                "dropped_columns": sorted(FORBIDDEN_COLUMNS), "files": []}
    outs = []
    # verbatim guard (review finding DOCS_LEAKAGE-023): shingles of every PRIVATE quote column of the mapped catalogues
    shingles: set[str] = set()
    for src_rel in sorted(mapping):
        src = canon / src_rel
        if src.suffix.lower() == ".csv" and src.exists():
            with open(src, encoding="utf-8", newline="") as f:
                rd = csv.reader(f)
                header = next(rd, [])
                qi = [i for i, h in enumerate(header) if h.strip().lower() in FORBIDDEN_COLUMNS]
                shingles |= quote_shingles(row[i] for row in rd for i in qi if i < len(row))
    manifest["verbatim_guard"] = {"rule": f">= {VERBATIM_LIMIT_WORDS} consecutive alphabetic words of a PRIVATE quote "
                                          "are cut to their first 20 words in PUBLIC", "cells_shortened": []}
    for src_rel, dst_rel in sorted(mapping.items()):
        src, dst = canon / src_rel, ROOT / dst_rel
        if not src.exists():
            print(f"MISSING {src_rel}", file=sys.stderr)
            manifest["files"].append({"source": src_rel, "target": dst_rel, "status": "MISSING"})
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.suffix.lower() == ".csv":
            with open(src, encoding="utf-8", newline="") as f:
                rows = list(csv.reader(f))
            header = rows[0]
            keep = [i for i, h in enumerate(header) if h.strip().lower() not in FORBIDDEN_COLUMNS]
            with open(dst, "w", encoding="utf-8", newline="") as f:
                w = csv.writer(f, lineterminator="\n")
                for n, r in enumerate(rows):
                    cells = [sanitize_paths(r[i], data=True) if i < len(r) else "" for i in keep]
                    if n:
                        for j, i in enumerate(keep):
                            cell = cells[j]
                            if len(cell) <= 80 or any(x in header[i].lower() for x in BIBLIO_COLUMN_HINTS):
                                continue
                            alpha, _, _ = longest_shared_run(words(cell), shingles)
                            if alpha >= VERBATIM_LIMIT_WORDS:
                                cells[j] = " ".join(cell.split()[:20]) + " … [сокращено: дословный текст источника — только в PRIVATE]"
                                manifest["verbatim_guard"]["cells_shortened"].append(
                                    {"target": dst_rel, "row": n + 1, "column": header[i], "copied_words": alpha})
                    w.writerow(cells)
            dropped = [header[i] for i in range(len(header)) if i not in keep]
        else:
            text = src.read_text(encoding="utf-8")
            dropped = []
            if src.suffix.lower() == ".json":
                data = json.loads(text)
                removed: list[str] = []
                clean = strip_forbidden_keys(data, removed)
                if removed:              # review finding DOCS_LEAKAGE-023: quote keys never reach PUBLIC JSON
                    text = json.dumps(clean, ensure_ascii=False, indent=1) + "\n"
                    dropped = sorted(set(removed))
            dst.write_text(sanitize_paths(text, data=True), encoding="utf-8", newline="\n")
        outs.append(dst)
        manifest["files"].append({"source": src_rel, "source_sha256": sha(src), "target": dst_rel,
                                  "target_sha256": sha(dst), "dropped_columns": dropped, "status": "OK"})
    problems = scan(ROOT, files=outs)
    manifest["leakage_problems"] = problems
    (ROOT / "evidence" / "PUBLIC_CATALOGUE_MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{sum(1 for f in manifest['files'] if f['status'] == 'OK')} catalogues written; leakage problems: {len(problems)}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())

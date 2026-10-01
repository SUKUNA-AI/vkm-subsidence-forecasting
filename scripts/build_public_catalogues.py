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
import io
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from vkm_world.governance.leakage import (  # noqa: E402
    BIBLIO_COLUMN_HINTS, FORBIDDEN_COLUMNS, VERBATIM_LIMIT_WORDS, longest_shared_run, quote_prefix, quote_shingles, sanitize_paths,
    words)
from vkm_world.governance.publication import (  # noqa: E402
    contained_path, load_catalogue_map, public_json_texts, publish_batch, strict_json)

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


def main() -> int:
    res = os.environ.get("VKM_RESOURCES_ROOT")
    if not res:
        print("set VKM_RESOURCES_ROOT to the private resources repository", file=sys.stderr)
        return 2
    try:
        return build(Path(res) / "11_evidence_vnext" / "canonical")
    except (OSError, ValueError, csv.Error, UnicodeError) as exc:
        print(f"catalogue publication aborted: {type(exc).__name__}", file=sys.stderr)
        return 1


def build(canon: Path) -> int:
    mapping = load_catalogue_map(ROOT / "scripts" / "public_catalogue_map.json")
    # Preflight the entire required set before creating or replacing any PUBLIC output.
    inputs = {}
    for source, target in sorted(mapping.items()):
        src = contained_path(canon, source)
        contained_path(ROOT, target)
        if not src.is_file():
            raise ValueError('required canonical catalogue is missing')
        data = src.read_bytes()
        text = data.decode('utf8')
        if src.suffix == '.csv':
            rows = list(csv.reader(io.StringIO(text, newline=''), strict=True))
            if (not rows or not rows[0] or any(not h.strip() for h in rows[0])
                    or len(set(rows[0])) != len(rows[0]) or any(len(row) != len(rows[0]) for row in rows[1:])):
                raise ValueError('canonical CSV has an invalid header or row width')
            inputs[source] = (data, rows)
        else:
            inputs[source] = (data, strict_json(text))
    manifest = {"generator": "scripts/build_public_catalogues.py", "private_root_rel": "11_evidence_vnext/canonical",
                "dropped_columns": sorted(FORBIDDEN_COLUMNS), "files": []}
    outputs = {}
    # verbatim guard (review finding DOCS_LEAKAGE-023): shingles of every PRIVATE quote column of the mapped catalogues
    shingles: set[str] = set()
    for src_rel in sorted(mapping):
        if Path(src_rel).suffix == ".csv":
            rows = inputs[src_rel][1]
            header = rows[0]
            qi = [i for i, h in enumerate(header) if h.strip().lower() in FORBIDDEN_COLUMNS]
            shingles |= quote_shingles(row[i] for row in rows[1:] for i in qi)
    manifest["verbatim_guard"] = {"rule": f">= {VERBATIM_LIMIT_WORDS} consecutive alphabetic words of a PRIVATE quote "
                                          "are cut to their first 20 words in PUBLIC", "cells_shortened": []}
    for src_rel, dst_rel in sorted(mapping.items()):
        source_bytes, value = inputs[src_rel]
        if Path(src_rel).suffix == ".csv":
            rows = value
            header = rows[0]
            keep = [i for i, h in enumerate(header) if h.strip().lower() not in FORBIDDEN_COLUMNS]
            if not keep:
                raise ValueError('canonical CSV contains no public columns')
            with io.StringIO(newline='') as f:
                w = csv.writer(f, lineterminator="\n")
                for n, r in enumerate(rows):
                    cells = [sanitize_paths(r[i], data=True) if i < len(r) else "" for i in keep]
                    if n:
                        for j, i in enumerate(keep):
                            cell = cells[j]
                            if any(x in header[i].lower() for x in BIBLIO_COLUMN_HINTS):
                                continue
                            alpha, _, _ = longest_shared_run(words(cell), shingles)
                            if alpha >= VERBATIM_LIMIT_WORDS:
                                cells[j] = quote_prefix(cell) + " … [сокращено: дословный текст источника — только в PRIVATE]"
                                manifest["verbatim_guard"]["cells_shortened"].append(
                                    {"target": dst_rel, "row": n + 1, "column": header[i], "copied_words": alpha})
                            if longest_shared_run(words(cells[j]), shingles)[0] >= VERBATIM_LIMIT_WORDS:
                                raise ValueError('generated public CSV cell still repeats a canonical quote')
                    w.writerow(cells)
                text = f.getvalue()
            dropped = [header[i] for i in range(len(header)) if i not in keep]
        else:
            dropped = []
            removed: list[str] = []
            clean = strip_forbidden_keys(value, removed)
            # Keep unchanged formatting when no keys were removed (existing deterministic consumer format).
            text = (json.dumps(clean, ensure_ascii=False, indent=1, allow_nan=False) + '\n'
                    if removed else source_bytes.decode('utf8').replace('\r\n', '\n').replace('\r', '\n'))
            dropped = sorted(set(removed))
            text = sanitize_paths(text, data=True)
            if any(longest_shared_run(words(value), shingles)[0] >= VERBATIM_LIMIT_WORDS
                   for value in public_json_texts(strict_json(text))):
                raise ValueError('public JSON repeats a canonical quote')
        target_bytes = text.encode('utf8')
        outputs[dst_rel] = target_bytes
        manifest["files"].append({"source": src_rel, "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
                                  "target": dst_rel, "target_sha256": hashlib.sha256(target_bytes).hexdigest(),
                                  "dropped_columns": dropped, "status": "OK"})
    manifest["leakage_problems"] = []
    outputs['evidence/PUBLIC_CATALOGUE_MANIFEST.json'] = (
        json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True) + '\n').encode('utf8')
    publish_batch(ROOT, outputs)
    print(f"{len(mapping)} catalogues written; leakage problems: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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
from vkm_world.governance.leakage import FORBIDDEN_COLUMNS, sanitize_paths, scan  # noqa: E402

csv.field_size_limit(sys.maxsize)


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
                for r in rows:
                    w.writerow([sanitize_paths(r[i]) if i < len(r) else "" for i in keep])
            dropped = [header[i] for i in range(len(header)) if i not in keep]
        else:
            dst.write_text(sanitize_paths(src.read_text(encoding="utf-8")), encoding="utf-8", newline="\n")
            dropped = []
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

"""sha256 of the frozen benchmark inputs (images, truth, manual transcription) → hashes_v1.json (IDs and hashes only).
Usage: hash_inputs.py <work dir> <registry> <out json>"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> None:
    work, reg, out = Path(sys.argv[1]), json.loads(Path(sys.argv[2]).read_text(encoding="utf-8")), Path(sys.argv[3])
    fam = {f["key"]: f for f in reg["figures"]}
    rows = {}
    for k in reg["evaluation_set"]:
        f = fam[k]
        rows[k] = {"id": f.get("figure_id") or f"{f['source_id']} p.{f['page']} (manual region)",
                   "input_png_sha256": sha(work / "inputs" / f"{k}.png"),
                   "truth_json_sha256": sha(work / "truth" / f"{k}.json")}
    doc = {"benchmark": "CHART_MODELS_V1", "manual_truth_v1_json_sha256": sha(work / "manual_truth_v1.json"),
           "figures": rows}
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(doc, ensure_ascii=False)[:400])


if __name__ == "__main__":
    main()

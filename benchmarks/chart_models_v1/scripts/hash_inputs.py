"""sha256 of the frozen benchmark inputs (images, truth, manual transcription) → hashes_v1.json (IDs and hashes only).
Usage: hash_inputs.py <work dir> <registry> <out json>
       hash_inputs.py <work dir> <registry> <out json> --glm-crops   (label crops of the GLM-OCR addendum)"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> None:
    work, reg, out = Path(sys.argv[1]), json.loads(Path(sys.argv[2]).read_text(encoding="utf-8")), Path(sys.argv[3])
    if "--glm-crops" in sys.argv:
        rows = {}
        for k in reg["evaluation_set"]:
            man = work / "glm_crops" / k / "manifest.json"
            m = json.loads(man.read_text(encoding="utf-8"))
            rows[k] = {"n_crops": m["n_crops"], "manifest_sha256": sha(man),
                       "crops_sha256": [c["sha256"] for c in m["crops"]]}
        doc = {"benchmark": "CHART_MODELS_V1", "addendum": "GLM-OCR", "what": "route R label crops as Tesseract "
               "receives them (8-bit grey PNG), captured by scripts/capture_label_crops.py", "figures": rows,
               "n_crops": sum(r["n_crops"] for r in rows.values())}
        out.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(doc["n_crops"], "crops")
        return
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

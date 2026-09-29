"""CHART_MODELS_V1 — public receipt (IDs, numbers, hashes; no values, no text, no paths) from results_v1.json.
Usage: make_receipt.py <results_v1.json> <exploratory.json> <run_meta.json> <out receipt json>"""
from __future__ import annotations

import json
import sys


def main() -> None:
    res = json.loads(open(sys.argv[1], encoding="utf-8").read())
    exp = json.loads(open(sys.argv[2], encoding="utf-8").read())
    meta = json.loads(open(sys.argv[3], encoding="utf-8").read())
    a, per = res["aggregate"], res["per_figure"]
    t2keys = [k for k in per if "T2" in per[k]]
    receipt = {
        "receipt": "chart_models_v1",
        "agent": "CH",
        "date": "2026-09-29",
        "benchmark": "CHART_MODELS_V1",
        "question": "does a 2025-2026 chart-understanding model beat the figure-digitization helpers of agent FD "
                    "(Tesseract tick labels, raster route R geometry) on the same images",
        "status": "DERIVATION",
        "review_status": "AUTO_EXTRACTED_UNREVIEWED",
        "status_note": "digitized numbers are derived test outputs, never observations; nothing written to evidence, "
                       "the canon, CORE or EDGE; images, truth values and raw answers stay in the git-ignored work dir",
        **meta,
        "results": {
            "T1_tick_labels": {arm: {k: v for k, v in a["T1"][arm].items()} for arm in ("qwen", "granite", "tesseract")},
            "T1_differences": {k: v for k, v in a["T1"].items() if k.endswith("_recall")},
            "T2_values": {arm: a["T2"][arm] for arm in ("qwen", "granite", "G", "H")},
            "T2_differences": {k: v for k, v in a["T2"].items() if k.endswith("hit@2%")},
            "T2_per_figure_hit@2%": {k: {arm: per[k]["T2"][arm].get("hit@2%", 0.0) for arm in ("qwen", "granite", "G", "H")}
                                     for k in t2keys},
            "T2_qualitative": {k: per[k]["T2_qualitative"] for k in per if "T2_qualitative" in per[k]},
            "E1_thinking": {k: {m: per[k]["T2"]["qwen_think"].get(m) for m in ("hit@2%", "completion_tokens", "wall_s", "parsed")}
                            for k in t2keys if "qwen_think" in per[k]["T2"]},
            "exploratory_not_preregistered": exp["aggregate"],
        },
    }
    with open(sys.argv[4], "w", encoding="utf-8", newline="\n") as fh:
        json.dump(receipt, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    print("ok", len(json.dumps(receipt)))


if __name__ == "__main__":
    main()

"""CHART_MODELS_V1 — exploratory analyses, NOT preregistered (RESULTS.md §6 marks them as such).

X1  H without the legend-only rule for route R traces: a traced curve is drawn by definition; in the preregistered
    scoring a trace that took the label of a legend entry without a drawn curve (S3) was dropped.
X1b H with the model's colour-matched series labels ignored in matching (distance matching only, as for G): shows
    how much of H − G comes from wrong labels (labels decide the matching first).
X2  Fallback F: route R (G) where it calibrates both axes and traces at least one series, otherwise the model's
    values (the combination suggested by the per-figure pattern — chosen after seeing the results).
X3  F with the hybrid H instead of G.
Usage: exploratory.py <work> <registry> <results_v1.json> <out json>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import evaluate as E  # noqa: E402


def main() -> None:
    work, reg = Path(sys.argv[1]), json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    res = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))["per_figure"]
    keys = [k for k in reg["evaluation_set"] if "T2" in res[k]]
    rows = {}
    for k in keys:
        truth = json.loads((work / "truth" / f"{k}.json").read_text(encoding="utf-8"))
        hrec = json.loads((work / "routeR" / "H" / f"{k}.json").read_text(encoding="utf-8"))
        t_no_lo = dict(truth, legend_only=[])
        x1 = E.eval_values(t_no_lo, E.route_series(hrec, truth))
        # X1b: the same traces with the model's colour-matched labels removed (pure distance matching, as for G)
        unl = [dict(s, label=None) for s in E.route_series(hrec, truth)]
        x1b = E.eval_values(truth, unl)
        t2 = res[k]["T2"]
        g_ok = t2["G"].get("axis_status") == "OK" and t2["G"]["n_pred_series"] > 0
        h_ok = t2["H"].get("axis_status") == "OK" and t2["H"]["n_pred_series"] > 0
        rows[k] = {"H_no_legend_only_rule": x1["hit@2%"], "H_labels_ignored": x1b["hit@2%"],
                   "F_G_else_qwen": (t2["G"] if g_ok else t2["qwen"])["hit@2%"],
                   "F_source": "G" if g_ok else "qwen",
                   "F_H_else_qwen": (x1 if h_ok else t2["qwen"])["hit@2%"],
                   "G": t2["G"]["hit@2%"], "H": t2["H"]["hit@2%"], "qwen": t2["qwen"]["hit@2%"],
                   "granite": t2["granite"]["hit@2%"] if "hit@2%" in t2["granite"] else 0.0}
    agg = {}
    for col in ("G", "H", "H_no_legend_only_rule", "H_labels_ignored", "qwen", "granite", "F_G_else_qwen",
                "F_H_else_qwen"):
        agg[col] = float(np.mean([rows[k][col] for k in keys]))
    agg["X1_H_vs_G"] = E.boot_diff([rows[k]["H_no_legend_only_rule"] for k in keys], [rows[k]["G"] for k in keys])
    agg["X1b_H_labels_ignored_vs_G"] = E.boot_diff([rows[k]["H_labels_ignored"] for k in keys],
                                                   [rows[k]["G"] for k in keys])
    agg["X2_F_vs_G"] = E.boot_diff([rows[k]["F_G_else_qwen"] for k in keys], [rows[k]["G"] for k in keys])
    agg["X2_F_vs_qwen"] = E.boot_diff([rows[k]["F_G_else_qwen"] for k in keys], [rows[k]["qwen"] for k in keys])
    agg["F_sources"] = {s: sum(1 for k in keys if rows[k]["F_source"] == s) for s in ("G", "qwen")}
    # X3 (descriptive): figures where route R (G) produced matched series vs the rest, paired with Qwen
    works = [k for k in keys if res[k]["T2"]["G"].get("median_rel_dist") is not None]
    rest = [k for k in keys if k not in works]
    mm = [k for k in works if k[0] in "SR" or k in ("X05", "X25")]       # y axis in mm
    agg["X3_paired_where_G_works"] = {
        "figures": works,
        "median_rel_dist": {a: float(np.median([res[k]["T2"][a]["median_rel_dist"] for k in works]))
                            for a in ("G", "qwen")},
        "median_abs_err_mm": {a: float(np.median([res[k]["T2"][a]["median_abs_err"] for k in mm])) for a in ("G", "qwen")},
        "mm_figures": mm,
        "mean_hit@2%": {a: float(np.mean([res[k]["T2"][a]["hit@2%"] for k in works])) for a in ("G", "qwen")},
        "qwen_mean_hit@2%_where_G_gives_nothing": float(np.mean([res[k]["T2"]["qwen"]["hit@2%"] for k in rest])),
        "figures_where_G_gives_nothing": rest,
    }
    # X4 (sensitivity of T1): Tesseract with ALL words it read, not only numbers/dates (route R itself uses only
    # numbers/dates for calibration; category labels such as 'Rp2.2' are read by its sparse OCR but not used)
    hits = n = 0
    for k in reg["evaluation_set"]:
        truth = json.loads((work / "truth" / f"{k}.json").read_text(encoding="utf-8"))
        g = json.loads((work / "routeR" / "G" / f"{k}.json").read_text(encoding="utf-8"))
        allt = (truth["ticks"]["x"] or []) + (truth["ticks"]["y"] or [])
        hits += E.multiset_hits(allt, g.get("tesseract_words") or [])
        n += len(allt)
    agg["X4_tesseract_recall_all_words"] = hits / n
    Path(sys.argv[4]).write_text(json.dumps({"note": "exploratory, not preregistered", "per_figure": rows,
                                              "aggregate": agg}, indent=1), encoding="utf-8")
    print(json.dumps(agg, indent=1))


if __name__ == "__main__":
    main()

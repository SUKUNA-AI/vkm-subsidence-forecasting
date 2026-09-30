"""CHART_MODELS_V1, GLM-OCR addendum — metrics of PREREGISTRATION_ADDENDUM_GLM.md.

Task 1 (tick labels, 268 labels on 19 figures) exactly as in evaluate.py (multiset match after ``evaluate.norm``),
for the GLM-OCR variants W (whole image, "Text Recognition:"), IE (whole image, information-extraction schema) and
C (route R's label crops), next to Tesseract (pooled words of arm G, and its readings of the same crops), Qwen3.5-9B
and Granite 4.1 4B (T1). Secondary: route R with another reader (arms RC, RIE, RQ; axis calibration and hit@2 %).
Writes <out> (IDs and numbers only). WSL, PYTHONPATH=<PUBLIC>/src.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import evaluate as E  # noqa: E402
from run_route_r_readers import ie_labels, parse_ie  # noqa: E402

STRIP = " \t,;:()[]{}\"'*#$`"


def tokens(text: str | None) -> list[str]:
    """Whitespace / table-pipe tokens of an unstructured OCR answer, punctuation trimmed at both ends
    (a trailing full stop too); LaTeX math markers removed."""
    if not text:
        return []
    t = re.sub(r"\\[()\[\]]", " ", text).replace("$", " ")
    out = []
    for tok in re.split(r"[\s|]+", t):
        tok = tok.strip(STRIP).rstrip(".")
        if tok:
            out.append(tok)
    return out


def labelish(ts: list[str]) -> list[str]:
    return [t for t in ts if E.is_labelish(t)]


def found_in_text(needle: str | None, text: str | None) -> bool | None:
    if needle is None:
        return None
    n = re.sub(r"\s+", "", E.norm(needle))
    h = re.sub(r"\s+", "", E.norm(text or ""))
    return bool(n) and n in h


def ticks_pooled(truth: dict, pred: list[str]) -> dict:
    return E.eval_ticks(truth, [], [], pooled_pred=pred)


def legend_fixed(obj: dict | None) -> list[str]:
    """Deviation G1 (RESULTS.md): legend objects of the form {"label": v} carry the text in the value; the
    preregistered rule took the keys (the smoke answer had {text: marker})."""
    out = []
    for item in (obj or {}).get("legend") or []:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            v = item.get("label", item.get("name"))
            out += [str(v)] if isinstance(v, (str, int, float)) else [str(k) for k in item.keys()]
    return out


def ie_lenient(txt: str | None) -> tuple[list[str], list[str]]:
    """Exploratory G2 (not preregistered): when the IE answer is not valid JSON, the "tick_labels" arrays of the
    x_axis and y_axis blocks are read with a regular expression (the answer is kept, only the parse is forgiving)."""
    out = {"x": [], "y": []}
    for ax in ("x", "y"):
        m = re.search(r'"%s_axis"\s*:\s*\{(.*?)\}' % ax, txt or "", flags=re.S)
        if not m:
            continue
        t = re.search(r'"tick_labels"\s*:\s*\[(.*?)\]', m.group(1), flags=re.S)
        if t:
            out[ax] = [v.strip().strip('"').strip() for v in t.group(1).split(",") if v.strip().strip('"').strip()]
    return out["x"], out["y"]


def summarize(rows: list[dict], key: str = "hits") -> dict:
    nt = sum(r["n_truth"] for r in rows)
    npred = sum(r["n_pred"] for r in rows)
    return {"micro_recall": sum(r["hits"] for r in rows) / nt if nt else None,
            "micro_precision": sum(r["hits"] for r in rows) / npred if npred else None,
            "mean_recall": float(np.mean([r["recall"] for r in rows if r["recall"] is not None])),
            "n_labels": nt, "n_pred": npred}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--results-v1", required=True, help="results_v1.json of the main benchmark (Qwen, Granite, G)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--crop-truth", default="", help="exploratory G3: manual reading of every crop (JSON)")
    args = ap.parse_args()
    reg = json.loads(Path(args.registry).read_text(encoding="utf-8"))
    work = Path(args.work)
    v1 = json.loads(Path(args.results_v1).read_text(encoding="utf-8"))["per_figure"]
    keys = reg["evaluation_set"]
    per = {}
    for key in keys:
        truth = json.loads((work / "truth" / f"{key}.json").read_text(encoding="utf-8"))
        rec: dict = {}
        # ---- W
        w = json.loads((work / "raw" / "glm" / "W" / f"{key}.json").read_text(encoding="utf-8"))
        toks = tokens(w.get("content"))
        rec["W"] = {**ticks_pooled(truth, labelish(toks)), "wall_s": w.get("wall_s"),
                    "peak_card_mib": w.get("peak_card_mib"), "finish": w.get("finish_reason")}
        rec["W_all_tokens"] = ticks_pooled(truth, toks)
        tf = {}
        for ax in ("x", "y"):
            t = (truth.get("titles") or {}).get(ax)
            if t is not None:
                tf[f"{ax}_title_found"] = found_in_text(t, w.get("content"))
        leg = truth.get("legend") or []
        if leg:
            tf["legend_found"] = sum(bool(found_in_text(v, w.get("content"))) for v in leg) / len(leg)
        rec["W"].update(tf)
        # ---- IE
        ie = json.loads((work / "raw" / "glm" / "IE" / f"{key}.json").read_text(encoding="utf-8"))
        obj = parse_ie(ie.get("content"))
        px, py = ie_labels(obj, "x"), ie_labels(obj, "y")
        ans = obj if isinstance(obj, dict) else {}
        norm_ans = {"x_axis": {k: (v or None) if isinstance(v, str) else v
                               for k, v in (ans.get("x_axis") or {}).items()},
                    "y_axis": {k: (v or None) if isinstance(v, str) else v
                               for k, v in (ans.get("y_axis") or {}).items()},
                    "legend": []}
        for item in ans.get("legend") or []:
            if isinstance(item, str):
                norm_ans["legend"].append(item)
            elif isinstance(item, dict):
                norm_ans["legend"] += [str(k) for k in item.keys()]
        rec["IE"] = {**E.eval_ticks(truth, px, py), **E.eval_titles(truth, norm_ans), "parsed": obj is not None,
                     "wall_s": ie.get("wall_s"), "peak_card_mib": ie.get("peak_card_mib"),
                     "finish": ie.get("finish_reason")}
        if leg:
            lf = legend_fixed(obj)
            rec["IE"]["legend_recall_fixed"] = E.multiset_hits(leg, lf) / len(leg)
        lx, ly = (px, py) if obj is not None else ie_lenient(ie.get("content"))
        rec["IE_lenient"] = E.eval_ticks(truth, lx, ly)
        # ---- C (same crops as Tesseract)
        c = json.loads((work / "raw" / "glm" / "C" / f"{key}.json").read_text(encoding="utf-8"))
        glm_reads = [re.sub(r"\s+", "", cr.get("content") or "") for cr in c["crops"]]
        tes_reads = [re.sub(r"\s+", "", cr.get("tesseract_text") or "") for cr in c["crops"]]
        rec["C"] = {**ticks_pooled(truth, labelish([r for r in glm_reads if r])), "n_crops": c["n_crops"],
                    "wall_s": c.get("wall_s"), "peak_card_mib": c.get("peak_card_mib")}
        rec["C_all"] = ticks_pooled(truth, [r for r in glm_reads if r])
        rec["C_tesseract"] = ticks_pooled(truth, labelish([r for r in tes_reads if r]))
        rec["C_agreement"] = (sum(E.norm(a) == E.norm(b) for a, b in zip(glm_reads, tes_reads)) / len(glm_reads)
                              if glm_reads else None)
        # ---- references from the main benchmark
        for arm in ("qwen", "granite", "tesseract"):
            rec[f"ref_{arm}"] = {k: v1[key]["T1"][arm].get(k) for k in ("n_truth", "n_pred", "hits", "recall",
                                                                         "precision", "axis_hits")}
        # ---- secondary: route R with another reader
        for arm in ("G", "H", "RC", "RIE", "RQ"):
            p = work / "routeR" / arm / f"{key}.json"
            if not p.exists():
                continue
            r = json.loads(p.read_text(encoding="utf-8"))
            s = {"axis_status": r.get("axis_status")}
            if key not in E.VALUE_KEYS_SKIP:
                v = E.eval_values(truth, E.route_series(r, truth))
                s["hit@2%"] = v.get("hit@2%", 0.0)
            rec[f"route_{arm}"] = s
        per[key] = rec

    agg: dict = {"T1": {}, "route_r": {}, "speed": {}}
    for name, getter in (("glm_W", lambda k: per[k]["W"]), ("glm_W_all_tokens", lambda k: per[k]["W_all_tokens"]),
                         ("glm_IE", lambda k: per[k]["IE"]), ("tesseract_pooled", lambda k: per[k]["ref_tesseract"]),
                         ("qwen", lambda k: per[k]["ref_qwen"]), ("granite", lambda k: per[k]["ref_granite"])):
        agg["T1"][name] = summarize([getter(k) for k in keys])
    agg["T1"]["glm_IE"]["micro_axis_recall"] = sum(per[k]["IE"]["axis_hits"] for k in keys) / agg["T1"]["glm_IE"]["n_labels"]
    for fld in ("x_title_ok", "y_title_ok", "x_unit_ok", "y_unit_ok"):
        v = [per[k]["IE"][fld] for k in keys if fld in per[k]["IE"]]
        agg["T1"]["glm_IE"][fld] = (sum(v) / len(v), len(v)) if v else None
    v = [per[k]["IE"]["legend_recall"] for k in keys if "legend_recall" in per[k]["IE"]]
    agg["T1"]["glm_IE"]["legend_recall_mean"] = (float(np.mean(v)), len(v)) if v else None
    v = [per[k]["IE"]["legend_recall_fixed"] for k in keys if "legend_recall_fixed" in per[k]["IE"]]
    agg["T1"]["glm_IE"]["legend_recall_mean_G1"] = (float(np.mean(v)), len(v)) if v else None
    agg["T1"]["glm_IE"]["parsed"] = sum(bool(per[k]["IE"]["parsed"]) for k in keys)
    agg["T1"]["glm_IE_lenient_G2"] = summarize([per[k]["IE_lenient"] for k in keys])
    for fld in ("x_title_found", "y_title_found"):
        v = [per[k]["W"][fld] for k in keys if per[k]["W"].get(fld) is not None]
        agg["T1"]["glm_W"][fld] = (sum(v) / len(v), len(v)) if v else None
    v = [per[k]["W"]["legend_found"] for k in keys if "legend_found" in per[k]["W"]]
    agg["T1"]["glm_W"]["legend_found_mean"] = (float(np.mean(v)), len(v)) if v else None
    ck = [k for k in keys if per[k]["C"]["n_crops"] > 0]
    agg["T1"]["C_figures"] = ck
    agg["T1"]["glm_C"] = summarize([per[k]["C"] for k in ck])
    agg["T1"]["glm_C_all"] = summarize([per[k]["C_all"] for k in ck])
    agg["T1"]["tesseract_C"] = summarize([per[k]["C_tesseract"] for k in ck])
    agg["T1"]["C_agreement_mean"] = float(np.mean([per[k]["C_agreement"] for k in ck]))
    agg["T1"]["n_crops"] = sum(per[k]["C"]["n_crops"] for k in keys)
    rec_of = lambda k, name: per[k][name]["recall"] or 0.0      # noqa: E731
    agg["T1"]["A1_glmW_vs_tesseract_pooled"] = E.boot_diff([rec_of(k, "W") for k in keys],
                                                           [rec_of(k, "ref_tesseract") for k in keys])
    agg["T1"]["A2_glmC_vs_tesseractC"] = E.boot_diff([rec_of(k, "C") for k in ck], [rec_of(k, "C_tesseract") for k in ck])
    agg["T1"]["B1_glmIE_vs_qwen"] = E.boot_diff([rec_of(k, "IE") for k in keys], [rec_of(k, "ref_qwen") for k in keys])
    agg["T1"]["B2_glmW_all_vs_qwen"] = E.boot_diff([rec_of(k, "W_all_tokens") for k in keys],
                                                   [rec_of(k, "ref_qwen") for k in keys])
    agg["T1"]["B3_glmIE_vs_granite"] = E.boot_diff([rec_of(k, "IE") for k in keys],
                                                   [rec_of(k, "ref_granite") for k in keys])
    agg["T1"]["glmIE_vs_tesseract_pooled"] = E.boot_diff([rec_of(k, "IE") for k in keys],
                                                         [rec_of(k, "ref_tesseract") for k in keys])
    vk = [k for k in keys if k not in E.VALUE_KEYS_SKIP]
    for arm in ("G", "H", "RC", "RIE", "RQ"):
        have = [k for k in keys if f"route_{arm}" in per[k]]
        if not have:
            continue
        agg["route_r"][arm] = {
            "both_axes_calibrated": sum(per[k][f"route_{arm}"]["axis_status"] == "OK" for k in have),
            "of": len(have),
            "mean_hit@2%": float(np.mean([per[k][f"route_{arm}"].get("hit@2%", 0.0) for k in vk if k in have])),
        }
    for arm in ("RC", "RIE", "RQ"):
        if arm in agg["route_r"]:
            agg["route_r"][f"{arm}_vs_G_hit@2%"] = E.boot_diff([per[k][f"route_{arm}"].get("hit@2%", 0.0) for k in vk],
                                                               [per[k]["route_G"].get("hit@2%", 0.0) for k in vk])
    if args.crop_truth:        # exploratory G3 (not preregistered): per-crop accuracy against a manual reading
        ct = json.loads(Path(args.crop_truth).read_text(encoding="utf-8"))["labels"]
        lab = {"glm": [0, 0], "tesseract": [0, 0]}          # [read exactly, label crops]
        spurious = {"glm": [0, 0], "tesseract": [0, 0]}     # [label-like reading, non-label crops]
        both_wrong = glm_only = tes_only = 0
        for k in keys:
            c = json.loads((work / "raw" / "glm" / "C" / f"{k}.json").read_text(encoding="utf-8"))
            truth_k = ct.get(k, {})
            for cr in c["crops"]:
                g = re.sub(r"\s+", "", cr.get("content") or "")
                t = re.sub(r"\s+", "", cr.get("tesseract_text") or "")
                tv = truth_k.get(str(cr["i"]))
                if tv is not None:
                    og, ot = E.norm(g) == E.norm(tv), E.norm(t) == E.norm(tv)
                    lab["glm"][0] += og
                    lab["tesseract"][0] += ot
                    lab["glm"][1] += 1
                    lab["tesseract"][1] += 1
                    both_wrong += (not og and not ot)
                    glm_only += og and not ot
                    tes_only += ot and not og
                else:
                    for name, r in (("glm", g), ("tesseract", t)):
                        spurious[name][0] += bool(r) and E.is_labelish(r)
                        spurious[name][1] += 1
        agg["T1"]["G3_crops_manual"] = {
            "label_crops": lab["glm"][1],
            "read_exactly": {n: lab[n][0] / lab[n][1] for n in lab},
            "only_glm_right": glm_only, "only_tesseract_right": tes_only, "both_wrong": both_wrong,
            "non_label_crops": spurious["glm"][1],
            "spurious_label_like_reading": {n: spurious[n][0] / spurious[n][1] for n in spurious},
        }
    for v in ("W", "IE", "C"):
        ws = [per[k][v]["wall_s"] for k in keys if per[k][v].get("wall_s") is not None]
        pk = [per[k][v]["peak_card_mib"] for k in keys if per[k][v].get("peak_card_mib")]
        agg["speed"][v] = {"median_s": statistics.median(ws), "max_s": max(ws), "sum_s": round(sum(ws), 2),
                           "peak_card_mib": max(pk) if pk else None}
    Path(args.out).write_text(json.dumps({"per_figure": per, "aggregate": agg}, ensure_ascii=False, indent=1,
                                         default=lambda o: o.item() if hasattr(o, "item") else str(o)),
                              encoding="utf-8")
    print(json.dumps(agg, ensure_ascii=False, indent=1, default=str)[:8000])


if __name__ == "__main__":
    main()

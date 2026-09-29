"""CHART_MODELS_V1 — metrics (PREREGISTRATION.md §4) from the truth files, the raw model answers and route R outputs.

Runs in the WSL venv of agent FD (numpy, scipy) with ``PYTHONPATH=<PUBLIC>/src`` (label parsers of the figures
package). Writes ``<out>`` (JSON: per-figure metrics per arm, aggregates, paired bootstrap) — only IDs and numbers.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
from pathlib import Path

import numpy as np
from scipy.optimize import linear_sum_assignment

from vkm_corpus.figures.primitives import decimal_year, parse_date, parse_number

SEED = 20260929
N_BOOT = 10000
HIT_LEVELS = (0.01, 0.02, 0.05)
MATCH_MAX_COST = 0.25
HALLU_Y = 0.05
HALLU_X = 0.02

VALUE_KEYS_SKIP = {"R1", "R5"}          # no numeric truth


# --------------------------------------------------------------------------------------------- parsing
def norm(s) -> str:
    s = "" if s is None else str(s)
    s = re.sub(r"\s+", "", s.strip().lower())
    return s.replace("−", "-").replace("–", "-").replace("—", "-").replace(",", ".")


def parse_json_answer(txt: str):
    if not txt:
        return None
    if "</think>" in txt:                     # thinking mode (E1): the answer follows the reasoning
        txt = txt.split("</think>")[-1]
    s = re.sub(r"^```(?:json|csv)?|```$", "", txt.strip(), flags=re.M).strip()
    starts = [i for i in (s.find("{"), s.find("[")) if i >= 0]
    if not starts:
        return None
    s = s[min(starts):]
    try:
        return json.loads(s)
    except Exception:
        pass
    # a long answer cut by max_tokens: close what is open after the last complete point
    cut = s.rfind("]")
    while cut > 0:
        frag = s[:cut + 1]
        opens = []
        for ch in frag:
            if ch in "[{":
                opens.append(ch)
            elif ch in "]}" and opens:
                opens.pop()
        tail = "".join("]" if o == "[" else "}" for o in reversed(opens))
        try:
            return json.loads(frag + tail)
        except Exception:
            cut = s.rfind("]", 0, cut)
    return None


def answer_text(path: Path) -> tuple[str | None, dict]:
    if not path.exists():
        return None, {}
    d = json.loads(path.read_text(encoding="utf-8"))
    r = d.get("response") or {}
    ch = (r.get("choices") or [{}])[0]
    return (ch.get("message") or {}).get("content"), d


def to_number(v) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    if isinstance(v, str):
        n = parse_number(v)
        if n is not None:
            return n
        try:
            return float(v.replace(",", "."))
        except ValueError:
            return None
    return None


def to_x(v, truth: dict, order_index: int, n_points: int):
    """x of a predicted point in the truth frame: number, date (decimal year), or category index."""
    if truth.get("x_categories"):
        cats = [norm(c) for c in truth["x_categories"]]
        if isinstance(v, str) and norm(v) in cats:
            return float(cats.index(norm(v)))
        if n_points == len(cats):
            return float(order_index)        # categories in order
        n = to_number(v)
        return n if n is not None and 0 <= n <= len(cats) - 1 and float(n).is_integer() else None
    if truth.get("x_positions"):             # R4: printed km labels → category positions
        n = to_number(v)
        if n is None:
            return None
        lab = sorted((float(k), p) for k, p in truth["x_positions"].items())
        return float(np.interp(n, [a for a, _ in lab], [b for _, b in lab]))
    if isinstance(v, str):
        d = parse_date(v)
        if d is None:
            m = re.match(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})", v)
            if m:
                import datetime as dt

                try:
                    d = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                except ValueError:
                    d = None
        if d is not None:
            return decimal_year(d)
    return to_number(v)


def model_series(content: str | None, truth: dict) -> tuple[list[dict], dict]:
    obj = parse_json_answer(content or "")
    info = {"parsed": obj is not None}
    if not isinstance(obj, dict):
        return [], info
    out = []
    for s in obj.get("series") or []:
        if not isinstance(s, dict):
            continue
        pts = [p for p in (s.get("points") or []) if isinstance(p, (list, tuple)) and len(p) >= 2]
        xs, ys = [], []
        for i, p in enumerate(pts):
            x = to_x(p[0], truth, i, len(pts))
            y = to_number(p[1])
            if x is not None and y is not None:
                xs.append(x)
                ys.append(y)
        out.append({"label": s.get("label"), "color": s.get("color"), "x": xs, "y": ys, "n_raw": len(pts)})
    info["axes"] = {"x": obj.get("x_axis"), "y": obj.get("y_axis")}
    return out, info


def granite_series(content: str | None, truth: dict) -> tuple[list[dict], dict]:
    """<chart2csv> answer: header row (x name, series names), one row per x."""
    info = {"parsed": False}
    if not content:
        return [], info
    txt = re.sub(r"^```(?:csv)?|```$", "", content.strip(), flags=re.M).strip()
    lines = [ln for ln in txt.splitlines() if ln.strip()]
    if len(lines) < 2:
        return [], info
    delim = max([",", ";", "\t", "|"], key=lambda d: lines[0].count(d))
    rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delim))
    header, body = rows[0], [r for r in rows[1:] if len(r) >= 2]
    info["parsed"] = True
    info["header"] = header
    out = []
    for j in range(1, len(header)):
        xs, ys = [], []
        for i, r in enumerate(body):
            if j >= len(r):
                continue
            x = to_x(r[0].strip(), truth, i, len(body))
            y = to_number(r[j].strip())
            if x is not None and y is not None:
                xs.append(x)
                ys.append(y)
        out.append({"label": header[j].strip(), "color": None, "x": xs, "y": ys, "n_raw": len(body)})
    return out, info


def route_series(rec: dict, truth: dict) -> list[dict]:
    out = []
    for s in rec.get("series") or []:
        pts = s.get("points") or []
        xs, ys = [], []
        for i, p in enumerate(pts):
            if p.get("y") is None:
                continue
            x = p.get("x")
            if truth.get("x_categories"):
                # category axis (route R leaves x uncalibrated): the vertex takes the category whose column centre
                # (printed table under the chart) is nearest in pixels, within half a column
                cpx = truth.get("x_category_px") or []
                x = None
                if cpx and p.get("px") is not None:
                    k = int(np.argmin([abs(p["px"] - c) for c in cpx]))
                    half = 0.5 * (cpx[-1] - cpx[0]) / max(1, len(cpx) - 1)
                    x = float(k) if abs(p["px"] - cpx[k]) <= half else None
            elif truth.get("x_positions") and x is not None:
                lab = sorted((float(k), q) for k, q in truth["x_positions"].items())
                x = float(np.interp(x, [a for a, _ in lab], [b for _, b in lab]))
            if x is None:
                continue
            xs.append(float(x))
            ys.append(float(p["y"]))
        lab = s.get("label_model") if s.get("label_model") is not None else s.get("label_raw")
        out.append({"label": lab, "color": s.get("color"), "x": xs, "y": ys, "n_raw": len(pts)})
    return out


# --------------------------------------------------------------------------------------------- truth helpers
def tick_values(labels) -> list[float]:
    vals = []
    for t in labels or []:
        v = to_number(t)
        if v is None:
            d = parse_date(str(t))
            v = decimal_year(d) if d else None
        if v is not None:
            vals.append(v)
    return vals


def ranges(truth: dict) -> tuple[float, float]:
    yv = tick_values(truth["ticks"]["y"])
    yr = (max(yv) - min(yv)) if len(yv) >= 2 else None
    if truth.get("x_categories"):
        xr = float(len(truth["x_categories"]) - 1)
    elif truth.get("x_positions"):
        xr = 30.0
    else:
        xv = tick_values(truth["ticks"]["x"])
        xr = (max(xv) - min(xv)) if len(xv) >= 2 else None
    return xr, yr


def truth_series(truth: dict) -> list[dict]:
    """Truth vertices in drawing order (route A path order; category/position order for R figures)."""
    out = []
    for s in truth.get("series") or []:
        pts = [(p["x"], p["y"]) for p in s["points"] if p.get("x") is not None and p.get("y") is not None]
        out.append({"label": s.get("label"), "x": [a for a, _ in pts], "y": [b for _, b in pts]})
    return out


def polyline(xs, ys):
    """sorted by x, duplicate x averaged (for the vertical error in data units only)"""
    if not xs:
        return np.array([]), np.array([])
    order = np.argsort(xs, kind="stable")
    x = np.asarray(xs, float)[order]
    y = np.asarray(ys, float)[order]
    ux, inv = np.unique(x, return_inverse=True)
    uy = np.bincount(inv, weights=y) / np.bincount(inv)
    return ux, uy


def dist_to_polyline(qx, qy, lx, ly, xr: float, yr: float) -> np.ndarray:
    """Distance of each query point to the polyline (points joined in the given order), in axis-range units:
    sqrt((dx / x range)^2 + (dy / y range)^2)."""
    q = np.stack([np.asarray(qx, float) / xr, np.asarray(qy, float) / yr], 1)
    p = np.stack([np.asarray(lx, float) / xr, np.asarray(ly, float) / yr], 1)
    if len(p) == 0:
        return np.full(len(q), np.inf)
    if len(p) == 1:
        return np.linalg.norm(q - p[0], axis=1)
    a, b = p[:-1], p[1:]
    ab = b - a
    l2 = np.maximum((ab ** 2).sum(1), 1e-18)
    out = np.empty(len(q))
    for s in range(0, len(q), 256):
        qq = q[s:s + 256, None, :]
        t = np.clip(((qq - a) * ab).sum(2) / l2, 0.0, 1.0)
        proj = a + t[..., None] * ab
        out[s:s + 256] = np.sqrt(((qq - proj) ** 2).sum(2)).min(1)
    return out


def eval_pair(t: dict, p: dict, xr: float, yr: float) -> dict:
    """Truth → prediction: normalised 2-D distance of every truth vertex to the predicted polyline (points in the
    order the arm gave them). Prediction → truth: distance of every predicted point to the truth polyline.
    Vertical error in data units at truth vertices inside the x span of the prediction (for mm)."""
    tx, ty = np.asarray(t["x"], float), np.asarray(t["y"], float)
    if not p["x"]:
        return {"d_truth": np.full(len(tx), np.inf), "d_pred": np.array([]), "abs": np.array([]), "cov": 0.0,
                "vert": np.full(len(tx), np.inf)}
    d_t = dist_to_polyline(tx, ty, p["x"], p["y"], xr, yr)
    d_p = dist_to_polyline(p["x"], p["y"], tx, ty, xr, yr)
    ux, uy = polyline(p["x"], p["y"])
    inside = (tx >= ux[0]) & (tx <= ux[-1]) if len(ux) > 1 else np.abs(tx - ux[0]) <= 0.5 * HALLU_X * xr
    est = np.interp(tx, ux, uy) if len(ux) > 1 else np.full(len(tx), uy[0])
    vert = np.where(inside, np.abs(est - ty) / yr, np.inf)      # vertical error at the truth x (inf: not covered)
    return {"d_truth": d_t, "d_pred": d_p, "abs": np.abs(est - ty)[inside], "cov": float(inside.mean()),
            "vert": vert}


def eval_values(truth: dict, pred: list[dict]) -> dict:
    xr, yr = ranges(truth)
    ts = truth_series(truth)
    legend_only = {norm(v) for v in truth.get("legend_only") or []}
    pred = [p for p in pred if norm(p.get("label")) not in legend_only]   # legend entries without a drawn curve
    n_t, n_p = len(ts), len(pred)
    res = {"n_truth_series": n_t, "n_pred_series": n_p, "series_count_ok": n_t == n_p,
           "n_truth_points": int(sum(len(t["x"]) for t in ts)), "n_pred_points": int(sum(len(p["x"]) for p in pred))}
    if n_t == 0:
        return res
    pairs = {(i, j): eval_pair(t, p, xr, yr) for i, t in enumerate(ts) for j, p in enumerate(pred)}
    # 1) a predicted series whose label equals a truth label is that series (labels decide first)
    matched, used_i, used_j = [], set(), set()
    tl = {norm(t["label"]): i for i, t in enumerate(ts) if t["label"] not in (None, "")}
    for j, p in enumerate(pred):
        i = tl.get(norm(p.get("label")))
        if i is not None and i not in used_i and j not in used_j and p.get("label") not in (None, ""):
            matched.append((i, j))
            used_i.add(i)
            used_j.add(j)
    # 2) the rest by curve distance (median truth→prediction distance), one-to-one
    ri = [i for i in range(n_t) if i not in used_i]
    rj = [j for j in range(n_p) if j not in used_j]
    if ri and rj:
        cost = np.array([[float(np.median(pairs[i, j]["d_truth"])) for j in rj] for i in ri])
        cost = np.where(np.isfinite(cost), cost, 10.0)
        rows, cols = linear_sum_assignment(cost)
        matched += [(ri[a], rj[b]) for a, b in zip(rows, cols) if cost[a, b] <= MATCH_MAX_COST]
    hits = {lv: 0 for lv in HIT_LEVELS}
    vhits = {lv: 0 for lv in HIT_LEVELS}
    d_all, absd, bad_pred = [], [], 0
    lab_ok = lab_n = 0
    mi = {i for i, _ in matched}
    mj = {j for _, j in matched}
    for i, j in matched:
        e = pairs[i, j]
        d_all += list(e["d_truth"])
        absd += list(e["abs"])
        for lv in HIT_LEVELS:
            hits[lv] += int((e["d_truth"] <= lv).sum())
            vhits[lv] += int((e["vert"] <= lv).sum())
        bad_pred += int((e["d_pred"] > HALLU_Y).sum())
        if ts[i]["label"] not in (None, ""):
            lab_n += 1
            lab_ok += int(norm(ts[i]["label"]) == norm(pred[j].get("label")))
    for i, t in enumerate(ts):
        if i not in mi and t["label"] not in (None, ""):
            lab_n += 1
    for j, p in enumerate(pred):
        if j not in mj:
            bad_pred += len(p["x"])
    ntp = res["n_truth_points"]
    res.update({
        "n_matched": len(matched), "y_range": yr, "x_range": xr,
        **{f"hit@{int(lv * 100)}%": hits[lv] / ntp for lv in HIT_LEVELS},
        **{f"vhit@{int(lv * 100)}%": vhits[lv] / ntp for lv in HIT_LEVELS},
        "median_rel_dist": float(np.median(d_all)) if d_all else None,
        "p90_rel_dist": float(np.percentile(d_all, 90)) if d_all else None,
        "median_abs_err": float(np.median(absd)) if absd else None,
        "p90_abs_err": float(np.percentile(absd, 90)) if absd else None,
        "missing_share": 1.0 - hits[HALLU_Y] / ntp,
        "hallucinated_share": (bad_pred / res["n_pred_points"]) if res["n_pred_points"] else None,
        "label_acc": (lab_ok / lab_n) if lab_n else None, "labels_scored": lab_n,
        "coverage_mean": float(np.mean([pairs[i, j]["cov"] for i, j in matched])) if matched else 0.0,
    })
    return res


# --------------------------------------------------------------------------------------------- task 1
def multiset_hits(truth: list[str], pred: list[str]) -> int:
    pool = [norm(p) for p in pred]
    n = 0
    for t in truth:
        k = norm(t)
        if k in pool:
            pool.remove(k)
            n += 1
    return n


def is_labelish(s: str) -> bool:
    return to_number(s) is not None or parse_date(str(s)) is not None


def eval_ticks(truth: dict, px: list, py: list, pooled_pred: list | None = None) -> dict:
    tx, ty = truth["ticks"]["x"] or [], truth["ticks"]["y"] or []
    allt = tx + ty
    pool = pooled_pred if pooled_pred is not None else (px + py)
    hit = multiset_hits(allt, pool)
    return {"n_truth": len(allt), "n_pred": len(pool), "hits": hit,
            "recall": hit / len(allt) if allt else None, "precision": hit / len(pool) if pool else None,
            "axis_hits": multiset_hits(tx, px) + multiset_hits(ty, py)}


def eval_titles(truth: dict, ans: dict) -> dict:
    out = {}
    for ax in ("x", "y"):
        t = (truth.get("titles") or {}).get(ax)
        p = ((ans or {}).get(f"{ax}_axis") or {}).get("title")
        if t is None and p is None:
            continue
        out[f"{ax}_title_ok"] = norm(t) == norm(p)
        tu = (truth.get("units") or {}).get(ax)
        pu = ((ans or {}).get(f"{ax}_axis") or {}).get("unit")
        if tu is not None or pu is not None:
            out[f"{ax}_unit_ok"] = norm(tu) == norm(pu)
    leg = truth.get("legend") or []
    if leg:
        pl = [str(v) for v in (ans or {}).get("legend") or []]
        out["legend_recall"] = multiset_hits(leg, pl) / len(leg)
        out["legend_precision"] = multiset_hits(leg, pl) / len(pl) if pl else None
    return out


# --------------------------------------------------------------------------------------------- aggregate
def boot_diff(a: list[float], b: list[float]) -> dict:
    a, b = np.asarray(a, float), np.asarray(b, float)
    d = a - b
    rng = np.random.default_rng(SEED)
    idx = rng.integers(0, len(d), size=(N_BOOT, len(d)))
    bs = d[idx].mean(1)
    return {"mean_diff": float(d.mean()), "ci95": [float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))],
            "n": int(len(d))}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    reg = json.loads(Path(args.registry).read_text(encoding="utf-8"))
    work = Path(args.work)
    keys = reg["evaluation_set"]
    per = {}
    for key in keys:
        truth = json.loads((work / "truth" / f"{key}.json").read_text(encoding="utf-8"))
        rec = {"family": key[0]}
        # ---------- task 1
        t1 = {}
        for arm in ("qwen", "granite"):
            content, meta = answer_text(work / "raw" / arm / "T1" / f"{key}.json")
            ans = parse_json_answer(content or "")
            ans = ans if isinstance(ans, dict) else {}
            px = [str(v) for v in (ans.get("x_axis") or {}).get("tick_labels") or []]
            py = [str(v) for v in (ans.get("y_axis") or {}).get("tick_labels") or []]
            t1[arm] = {**eval_ticks(truth, px, py), **eval_titles(truth, ans), "parsed": bool(ans),
                       "wall_s": meta.get("wall_s"), "peak_card_mib": meta.get("peak_card_mib")}
        g = json.loads((work / "routeR" / "G" / f"{key}.json").read_text(encoding="utf-8"))
        tw = [w for w in (g.get("tesseract_words") or []) if is_labelish(w)]
        used_x = [l["text"] for l in ((g.get("x_axis") or {}).get("labels") or [])]
        used_y = [l["text"] for l in ((g.get("y_axis") or {}).get("labels") or [])]
        t1["tesseract"] = {**eval_ticks(truth, used_x, used_y, pooled_pred=tw), "elapsed_s": g.get("elapsed_s"),
                           "axis_status": g.get("axis_status")}
        rec["T1"] = t1
        # ---------- task 2
        if key not in VALUE_KEYS_SKIP:
            t2 = {}
            content, meta = answer_text(work / "raw" / "qwen" / "T2" / f"{key}.json")
            ser, info = model_series(content, truth)
            t2["qwen"] = {**eval_values(truth, ser), "parsed": info["parsed"], "wall_s": meta.get("wall_s"),
                          "peak_card_mib": meta.get("peak_card_mib"),
                          "finish": ((meta.get("response") or {}).get("choices") or [{}])[0].get("finish_reason")}
            content, meta = answer_text(work / "raw" / "granite" / "T2_granite" / f"{key}.json")
            ser, info = granite_series(content, truth)
            t2["granite"] = {**eval_values(truth, ser), "parsed": info["parsed"], "wall_s": meta.get("wall_s"),
                             "peak_card_mib": meta.get("peak_card_mib")}
            p_e1 = work / "raw" / "qwen_think" / "T2" / f"{key}.json"          # E1, exploratory
            if p_e1.exists():
                content, meta = answer_text(p_e1)
                ser, info = model_series(content, truth)
                t2["qwen_think"] = {**eval_values(truth, ser), "parsed": info["parsed"], "wall_s": meta.get("wall_s"),
                                    "completion_tokens": ((meta.get("response") or {}).get("usage") or {})
                                    .get("completion_tokens")}
            for arm in ("G", "H"):
                p = work / "routeR" / arm / f"{key}.json"
                r = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
                t2[arm] = {**eval_values(truth, route_series(r, truth)), "axis_status": r.get("axis_status"),
                           "elapsed_s": r.get("elapsed_s")}
            rec["T2"] = t2
        else:          # R1, R5: series count and legend only (qualitative)
            q = {}
            content, _ = answer_text(work / "raw" / "qwen" / "T2" / f"{key}.json")
            ser, info = model_series(content, truth)
            q["qwen"] = {"n_series": len(ser), "labels": [s["label"] for s in ser], "parsed": info["parsed"]}
            content, _ = answer_text(work / "raw" / "granite" / "T2_granite" / f"{key}.json")
            ser, info = granite_series(content, truth)
            q["granite"] = {"n_series": len(ser), "labels": [s["label"] for s in ser], "parsed": info["parsed"]}
            for arm in ("G", "H"):
                p = work / "routeR" / arm / f"{key}.json"
                r = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
                q[arm] = {"n_series": len(r.get("series") or []), "axis_status": r.get("axis_status"),
                          "labels": [s.get("label_model") or s.get("label_raw") for s in r.get("series") or []]}
            q["truth_series_count"] = truth.get("series_count")
            q["truth_legend"] = truth.get("legend")
            rec["T2_qualitative"] = q
        per[key] = rec

    # ---------- aggregates
    agg = {"T1": {}, "T2": {}}
    for arm in ("qwen", "granite", "tesseract"):
        rows = [per[k]["T1"][arm] for k in keys]
        nt = sum(r["n_truth"] for r in rows)
        agg["T1"][arm] = {"micro_recall": sum(r["hits"] for r in rows) / nt,
                          "micro_axis_recall": sum(r["axis_hits"] for r in rows) / nt,
                          "micro_precision": (sum(r["hits"] for r in rows) / max(1, sum(r["n_pred"] for r in rows))),
                          "mean_recall": float(np.mean([r["recall"] for r in rows])), "n_labels": nt}
        if arm != "tesseract":
            for fld in ("x_title_ok", "y_title_ok", "x_unit_ok", "y_unit_ok"):
                v = [r[fld] for r in rows if fld in r]
                agg["T1"][arm][fld] = (sum(v) / len(v), len(v)) if v else None
            v = [r["legend_recall"] for r in rows if "legend_recall" in r]
            agg["T1"][arm]["legend_recall_mean"] = (float(np.mean(v)), len(v)) if v else None
    for arm in ("qwen", "granite"):
        agg["T1"][f"{arm}_vs_tesseract_recall"] = boot_diff([per[k]["T1"][arm]["recall"] for k in keys],
                                                              [per[k]["T1"]["tesseract"]["recall"] for k in keys])
    vkeys = [k for k in keys if "T2" in per[k]]
    for arm in ("qwen", "granite", "G", "H"):
        rows = [per[k]["T2"][arm] for k in vkeys]
        a = {"n_figures": len(rows)}
        for m in ("hit@1%", "hit@2%", "hit@5%", "missing_share"):
            a[f"mean_{m}"] = float(np.mean([r.get(m, 0.0 if m != "missing_share" else 1.0) for r in rows]))
        hs = [r["hallucinated_share"] for r in rows if r.get("hallucinated_share") is not None]
        a["mean_hallucinated_share"] = float(np.mean(hs)) if hs else None
        med = [r["median_rel_dist"] for r in rows if r.get("median_rel_dist") is not None]
        a["median_of_figure_median_rel_dist"] = float(np.median(med)) if med else None
        a["figures_with_matched_series"] = len(med)
        a["series_count_ok"] = sum(bool(r.get("series_count_ok")) for r in rows)
        la = [(r["label_acc"], r["labels_scored"]) for r in rows if r.get("label_acc") is not None]
        a["label_acc_micro"] = (sum(x * n for x, n in la) / sum(n for _, n in la)) if la else None
        mm = [r["median_abs_err"] for k, r in zip(vkeys, rows)
              if r.get("median_abs_err") is not None and k in ("S1", "S2", "S3", "S4", "S5", "R2", "R3", "R4")]
        a["median_abs_err_mm_figs"] = float(np.median(mm)) if mm else None
        agg["T2"][arm] = a
    for a1, a2 in (("qwen", "G"), ("granite", "G"), ("H", "G"), ("qwen", "granite")):
        agg["T2"][f"{a1}_vs_{a2}_hit@2%"] = boot_diff([per[k]["T2"][a1]["hit@2%"] if "hit@2%" in per[k]["T2"][a1] else 0.0
                                                        for k in vkeys],
                                                       [per[k]["T2"][a2]["hit@2%"] if "hit@2%" in per[k]["T2"][a2] else 0.0
                                                        for k in vkeys])
    Path(args.out).write_text(json.dumps({"per_figure": per, "aggregate": agg}, ensure_ascii=False, indent=1,
                                         default=lambda o: o.item() if hasattr(o, "item") else str(o)),
                              encoding="utf-8")
    print(json.dumps(agg, ensure_ascii=False, indent=1, default=str)[:6000])


if __name__ == "__main__":
    main()

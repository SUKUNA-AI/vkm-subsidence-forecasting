"""CHART_MODELS_V1 — our geometry on the same images the models see.

Arm G: route R of agent FD exactly as the sweep runs it (``digitize_raster(fig, OcrEngine(), sample_markers=True)``,
Tesseract 5.5.3 in single-line mode with the digit whitelist for tick labels), code of the PUBLIC commit under test,
nothing changed.

Arm H (hybrid, needs the model answers of T1 and T2): route R geometry, but
* tick-label TEXT comes from the model (T1): route R's label blobs of the four axis strips (positions, same code as
  ``raster.ocr_axis_strips``) keep their positions and take the model's labels by order (x strips left→right,
  y strips bottom→top; monotone alignment with an edit-distance score when the counts differ); the result is passed
  to ``digitize_raster`` as ``corpus_words`` (so Tesseract is not used for calibration);
* the NUMBER of series comes from the model (T2): extra traces beyond that number are dropped, shortest first;
* series LABELS come from the model (T2): each trace takes the label of the model series with the nearest colour
  (ΔE*ab ≤ 35), one label per series.

Runs in WSL (opencv, tesseract) with ``PYTHONPATH=<PUBLIC>/src``; outputs ``<work>/routeR/<arm>/<key>.json``.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import time
from pathlib import Path

import numpy as np

from vkm_corpus.figures import raster as R
from vkm_corpus.figures.labels_ocr import OcrEngine
from vkm_corpus.figures.primitives import Text, label_value
from vkm_corpus.figures.raster_digitize import digitize_raster

BASIC = {  # colour names a model may answer with → sRGB
    "black": (0, 0, 0), "white": (255, 255, 255), "red": (220, 30, 30), "darkred": (139, 0, 0),
    "green": (0, 160, 0), "darkgreen": (0, 100, 0), "lightgreen": (144, 238, 144), "olive": (128, 128, 0),
    "blue": (30, 60, 220), "darkblue": (0, 0, 139), "lightblue": (135, 206, 235), "navy": (0, 0, 128),
    "cyan": (0, 200, 220), "teal": (0, 128, 128), "magenta": (230, 0, 230), "pink": (255, 150, 180),
    "purple": (128, 50, 160), "violet": (148, 0, 211), "orange": (250, 140, 30), "yellow": (240, 220, 0),
    "brown": (140, 70, 30), "gray": (128, 128, 128), "grey": (128, 128, 128), "lightgray": (200, 200, 200),
    "maroon": (128, 0, 0), "gold": (230, 180, 0), "salmon": (250, 128, 114), "turquoise": (64, 224, 208),
}


def to_rgb(c) -> tuple | None:
    if not isinstance(c, str):
        return None
    s = c.strip().lower().replace(" ", "")
    m = re.match(r"^#?([0-9a-f]{6})$", s)
    if m:
        h = m.group(1)
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    for k in sorted(BASIC, key=len, reverse=True):
        if k in s:
            return BASIC[k]
    return None


def lab_of(rgb) -> np.ndarray:
    import cv2

    return cv2.cvtColor(np.uint8([[rgb]]), cv2.COLOR_RGB2LAB)[0, 0].astype(float)


def parse_json_answer(txt: str):
    if not txt:
        return None
    s = re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M).strip()
    for opener in ("{", "["):
        i = s.find(opener)
        if i >= 0:
            try:
                return json.loads(s[i:])
            except Exception:
                pass
    return None


def model_answer(work: Path, arm: str, task: str, key: str):
    p = work / "raw" / arm / task / f"{key}.json"
    if not p.exists():
        return None
    d = json.loads(p.read_text(encoding="utf-8"))
    r = d.get("response") or {}
    ch = (r.get("choices") or [{}])[0]
    return parse_json_answer((ch.get("message") or {}).get("content") or "")


def norm(s: str) -> str:
    return re.sub(r"\s+", "", str(s)).replace("−", "-").replace("–", "-").replace("—", "-").replace(",", ".").lower()


def sim(a: str, b: str) -> float:
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = i
    for j in range(len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (a[i - 1] != b[j - 1]))
    return 1.0 - d[-1][-1] / max(len(a), len(b))


def align(blobs: list[Text], labels: list[str]) -> list[tuple[Text, str]]:
    """Monotone alignment of blobs (in reading order) with model labels (same order)."""
    if not blobs or not labels:
        return []
    if len(blobs) == len(labels):
        return list(zip(blobs, labels))
    n, m = len(blobs), len(labels)
    best = np.full((n + 1, m + 1), -1e9)
    best[0, :] = 0.0
    best[:, 0] = 0.0
    move = {}
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            opts = [(best[i - 1, j], "up"), (best[i, j - 1], "left"),
                    (best[i - 1, j - 1] + sim(blobs[i - 1].text, labels[j - 1]), "diag")]
            best[i, j], move[i, j] = max(opts, key=lambda o: o[0])
    out, i, j = [], n, m
    while i > 0 and j > 0:
        mv = move[i, j]
        if mv == "diag":
            if sim(blobs[i - 1].text, labels[j - 1]) >= 0.34:
                out.append((blobs[i - 1], labels[j - 1]))
            i, j = i - 1, j - 1
        elif mv == "up":
            i -= 1
        else:
            j -= 1
    return out[::-1]


def axis_strip_blobs(rgb, coloured, hl, vl, engine, text_h) -> dict[str, list[Text]]:
    """Same strips and blob reader as raster.ocr_axis_strips, kept per strip."""
    import cv2

    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    gray = np.where(coloured, 255, gray).astype(np.uint8)
    h, w = gray.shape
    th = max(12.0, text_h)
    wl = "0123456789.,-"
    gap = max(6.0, 0.4 * th)
    xs0 = [s[0] for s in hl] + [s[2] for s in vl]
    xs1 = [s[1] for s in hl] + [s[2] for s in vl]
    ys0 = [s[2] for s in hl] + [s[0] for s in vl]
    ys1 = [s[2] for s in hl] + [s[1] for s in vl]
    if not xs0:
        return {}
    left, right, top, bottom = min(xs0), max(xs1), min(ys0), max(ys1)
    strips = {"y_left": (left - 7 * th, top - th, left - gap, bottom + th),
              "y_right": (right + gap, top - th, right + 7 * th, bottom + th),
              "x_top": (left - 2 * th, top - 3.2 * th, right + 2 * th, top - gap),
              "x_bottom": (left - 2 * th, bottom + gap, right + 2 * th, bottom + 3.2 * th)}
    out = {}
    for name, (a, b, c, d) in strips.items():
        c0, r0, c1, r1 = int(max(0, a)), int(max(0, b)), int(min(w, c)), int(min(h, d))
        if c1 - c0 < 8 or r1 - r0 < 8:
            out[name] = []
            continue
        out[name] = R.ocr_blobs(np.ascontiguousarray(gray[r0:r1, c0:c1]), (c0, r0), engine, wl)
    return out


def hybrid_words(rgb, engine, t1: dict) -> tuple[list[Text], dict]:
    dark, grey, coloured, bg, chroma, lab = R.masks(rgb)
    hl, vl = R.long_lines(dark | grey, 0.3)
    hl, vl = R.extend_and_merge(dark | grey, hl, vl)
    words = R.ocr_words(rgb, engine)
    if words:
        hmed_all = float(np.median([t.h for t in words]))
        words = [t for t in words if t.h <= 2.5 * hmed_all and (t.x1 - t.x0) <= 0.25 * rgb.shape[1]]
    num_h = [t.h for t in words if label_value(t.text)[0] is not None]
    text_h = float(np.median(num_h)) if num_h else 0.012 * rgb.shape[0] * 2
    strips = axis_strip_blobs(rgb, coloured, hl, vl, engine, text_h)
    xl = [str(v) for v in ((t1 or {}).get("x_axis") or {}).get("tick_labels") or []]
    yl = [str(v) for v in ((t1 or {}).get("y_axis") or {}).get("tick_labels") or []]
    out, info = [], {}
    for name, blobs in strips.items():
        if name.startswith("x"):
            order = sorted(blobs, key=lambda t: t.xc)
            pairs = align(order, xl)
        else:
            order = sorted(blobs, key=lambda t: -t.yc)          # bottom → top
            pairs = align(order, yl)
        info[name] = {"blobs": len(blobs), "aligned": len(pairs),
                      "tesseract": [t.text for t in order], "model": [lb for _, lb in pairs]}
        for t, lb in pairs:
            out.append(Text(lb, t.x0, t.x1, t.yc, t.h, 0.0, "MODEL_TEXT_AT_BLOB", "MODEL_T1"))
    # words with letters (inline curve labels, legend) keep route R's sparse OCR; numbers far from the strips too
    used = [(t.xc, t.yc) for t in out]
    for t in words:
        if not any(abs(t.xc - x) < t.h and abs(t.yc - y) < t.h for x, y in used):
            out.append(t)
    return out, info


def to_json_axis(ax):
    return None if ax is None else ax.to_json()


def run(key: str, png: Path, arm: str, work: Path) -> dict:
    import cv2

    rgb = cv2.cvtColor(cv2.imread(str(png)), cv2.COLOR_BGR2RGB)
    fig = R.RasterFigure(rgb, 1.0, (0.0, 0.0), None, {"image": png.name})
    engine = OcrEngine()
    t0 = time.perf_counter()
    info = {}
    if arm == "G":
        res = digitize_raster(fig, engine, sample_markers=True)
    else:
        t1 = model_answer(work, "qwen", "T1", key)
        t2 = model_answer(work, "qwen", "T2", key)
        words, info = hybrid_words(rgb, engine, t1)
        res = digitize_raster(fig, engine, corpus_words=words, sample_markers=True)
        mser = [s for s in ((t2 or {}).get("series") or []) if isinstance(s, dict)]
        n_model = len(mser)
        series = res["series"]
        if n_model and len(series) > n_model:
            keep = sorted(range(len(series)), key=lambda i: -series[i]["n_columns"])[:n_model]
            series = [series[i] for i in sorted(keep)]
        # labels by colour
        cands = []
        for mi, ms in enumerate(mser):
            rgbm = to_rgb(ms.get("color"))
            if rgbm is None:
                continue
            for si, s in enumerate(series):
                h = s["color"].lstrip("#")
                rgbs = tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
                d = float(np.linalg.norm((lab_of(rgbm) - lab_of(rgbs)) * np.array([100 / 255, 1, 1])))
                cands.append((d, si, mi))
        taken_s, taken_m = set(), set()
        for d, si, mi in sorted(cands):
            if d > 35 or si in taken_s or mi in taken_m:
                continue
            series[si]["label_model"] = mser[mi].get("label")
            taken_s.add(si)
            taken_m.add(mi)
        res["series"] = series
        info["n_model_series"] = n_model
    elapsed = time.perf_counter() - t0
    return {
        "key": key, "arm": arm, "elapsed_s": round(elapsed, 3), "axis_status": res["axis_status"],
        "label_source": res["label_source"], "x_axis": to_json_axis(res["x_axis"]), "y_axis": to_json_axis(res["y_axis"]),
        "tesseract_words": [t.text for t in res["words"]] if arm == "G" else None,
        "hybrid": info or None, "ocr_calls": engine.calls,
        "series": [{"color": s["color"], "label_raw": s.get("label_raw"), "label_model": s.get("label_model"),
                    "sampling": s["sampling"], "flags": s["flags"], "n_columns": s["n_columns"],
                    "points": [{"x": p["x"], "y": p["y"], "px": p["x_drawing"], "py": p["y_drawing"]}
                               for p in s["points"]]} for s in res["series"]],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--arm", required=True, choices=["G", "H"])
    ap.add_argument("--keys", default="")
    args = ap.parse_args()
    reg = json.loads(Path(args.registry).read_text(encoding="utf-8"))
    work = Path(args.work)
    keys = [k for k in (args.keys.split(",") if args.keys else reg["evaluation_set"]) if k]
    for key in keys:
        out = work / "routeR" / args.arm / f"{key}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            rec = run(key, work / "inputs" / f"{key}.png", args.arm, work)
        except Exception as e:          # a crash is a result of the arm
            rec = {"key": key, "arm": args.arm, "error": f"{type(e).__name__}: {e}", "series": [], "axis_status": "ERROR"}
        out.write_text(json.dumps(rec, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
        print(key, args.arm, rec.get("axis_status"), len(rec.get("series") or []), rec.get("elapsed_s"), rec.get("error"),
              flush=True)


if __name__ == "__main__":
    main()

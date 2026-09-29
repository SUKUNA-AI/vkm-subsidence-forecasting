"""CHART_MODELS_V1 — draw every arm's series over the input image (visual check; images stay in the work dir).

Pixel mapping per figure: S/X — least squares from the truth vertices (value ↔ pixel); R1–R4 — the axis calibration
of route R (G) and, for categories, the column centres of the printed table; R5 — ``--r5-map`` (tick pixels read off
the image by hand: x0,xv0,x1,xv1,y0,yv0,y1,yv1). Usage: overlays.py <work> <registry> [--r5-map ...]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import evaluate as E  # noqa: E402

COLS = {"truth": (0, 0, 0), "qwen": (0, 0, 255), "granite": (0, 160, 0), "G": (255, 0, 255), "H": (255, 140, 0),
        "qwen_think": (0, 200, 255)}


def linmap(vals, pix):
    a, b = np.polyfit(np.asarray(vals, float), np.asarray(pix, float), 1)
    return lambda v: a * v + b


def mapping(key, truth, g, r5):
    if truth.get("series") and truth["series"][0]["points"] and "px" in truth["series"][0]["points"][0]:
        pts = [p for s in truth["series"] for p in s["points"]]
        return linmap([p["x"] for p in pts], [p["px"] for p in pts]), linmap([p["y"] for p in pts], [p["py"] for p in pts])
    if key == "R5" and r5:
        x0, xv0, x1, xv1, y0, yv0, y1, yv1 = r5
        return linmap([xv0, xv1], [x0, x1]), linmap([yv0, yv1], [y0, y1])
    ya = (g or {}).get("y_axis")
    fy = linmap([l["value"] for l in ya["labels"]], [l["pos"] for l in ya["labels"]]) if ya else None
    if truth.get("x_category_px"):
        cpx = truth["x_category_px"]
        fx = lambda v: np.interp(v, np.arange(len(cpx)), cpx)   # noqa: E731
    else:
        xa = (g or {}).get("x_axis")
        if xa and truth.get("x_positions"):
            lab = sorted((float(k), q) for k, q in truth["x_positions"].items())
            vals = [float(np.interp(l["value"], [a for a, _ in lab], [b for _, b in lab])) for l in xa["labels"]]
            fx = linmap(vals, [l["pos"] for l in xa["labels"]])
        else:
            fx = linmap([l["value"] for l in xa["labels"]], [l["pos"] for l in xa["labels"]]) if xa else None
    return fx, fy


def main() -> None:
    import cv2

    ap = argparse.ArgumentParser()
    ap.add_argument("work")
    ap.add_argument("registry")
    ap.add_argument("--r5-map", default="")
    args = ap.parse_args()
    work = Path(args.work)
    reg = json.loads(Path(args.registry).read_text(encoding="utf-8"))
    r5 = [float(v) for v in args.r5_map.split(",")] if args.r5_map else None
    out = work / "overlays"
    out.mkdir(exist_ok=True)
    for key in reg["evaluation_set"]:
        truth = json.loads((work / "truth" / f"{key}.json").read_text(encoding="utf-8"))
        gp = work / "routeR" / "G" / f"{key}.json"
        g = json.loads(gp.read_text(encoding="utf-8")) if gp.exists() else {}
        fx, fy = mapping(key, truth, g, r5)
        if fx is None or fy is None:
            print(key, "no pixel mapping")
            continue
        arms = {}
        c, _ = E.answer_text(work / "raw" / "qwen" / "T2" / f"{key}.json")
        arms["qwen"] = E.model_series(c, truth)[0]
        c, _ = E.answer_text(work / "raw" / "granite" / "T2_granite" / f"{key}.json")
        arms["granite"] = E.granite_series(c, truth)[0]
        for a in ("G", "H"):
            p = work / "routeR" / a / f"{key}.json"
            arms[a] = E.route_series(json.loads(p.read_text(encoding="utf-8")), truth) if p.exists() else []
        p = work / "raw" / "qwen_think" / "T2" / f"{key}.json"
        if p.exists():
            c, _ = E.answer_text(p)
            arms["qwen_think"] = E.model_series(c, truth)[0]
        base = cv2.imread(str(work / "inputs" / f"{key}.png"))
        for arm, series in arms.items():
            img = (base.astype(float) * 0.45 + 255 * 0.55).astype(np.uint8)
            for s in E.truth_series(truth):
                pts = [(int(fx(x)), int(fy(y))) for x, y in zip(s["x"], s["y"])]
                for q in pts:
                    cv2.circle(img, q, 2, COLS["truth"], -1)
            for s in series:
                pts = [(int(round(fx(x))), int(round(fy(y)))) for x, y in zip(s["x"], s["y"])
                       if np.isfinite(fx(x)) and np.isfinite(fy(y))]
                for q0, q1 in zip(pts, pts[1:]):
                    cv2.line(img, q0, q1, COLS[arm], 2)
                for q in pts:
                    cv2.circle(img, q, 4, COLS[arm], 1)
            cv2.putText(img, f"{key} {arm}: {len(series)} series", (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.7, COLS[arm], 2)
            cv2.imwrite(str(out / f"{key}_{arm}.png"), img)
        print(key, {a: len(s) for a, s in arms.items()})


if __name__ == "__main__":
    main()

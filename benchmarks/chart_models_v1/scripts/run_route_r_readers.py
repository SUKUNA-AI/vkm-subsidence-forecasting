"""CHART_MODELS_V1, GLM-OCR addendum — route R with another label reader (secondary metrics S1/S2 of the addendum).

Arms (route R code of agent FD unchanged; only the reader of the tick labels changes):
  RC   GLM-OCR as the drop-in crop reader: ``OcrEngine.read_line`` returns GLM-OCR's answer (variant C) for the crop
       with the same PNG sha256; the whole-image sparse pass stays Tesseract (as in route R)
  RIE  labels of GLM-OCR variant IE placed on route R's label blobs (mechanism of arm H, labels only: no series
       count, no series labels)
  RQ   the same with Qwen3.5-9B T1 labels (labels only) — the like-for-like reference for RIE
Outputs <work>/routeR/<arm>/<key>.json in the format of run_route_r.py. WSL, PYTHONPATH=<PUBLIC>/src.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from pathlib import Path

import numpy as np

from vkm_corpus.figures import raster as R
from vkm_corpus.figures.labels_ocr import OcrEngine
from vkm_corpus.figures.raster_digitize import digitize_raster

import run_route_r as RR   # noqa: E402  (same folder: hybrid_words, model_answer, to_json_axis)

_ORIG = OcrEngine.read_line


def parse_ie(txt: str | None) -> dict | None:
    """GLM-OCR IE answer → T1-shaped dict (number literals kept as printed)."""
    if not txt:
        return None
    s = re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M).strip()
    i = s.find("{")
    if i < 0:
        return None
    try:
        obj = json.loads(s[i:], parse_float=str, parse_int=str)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def ie_labels(obj: dict | None, axis: str) -> list[str]:
    v = ((obj or {}).get(f"{axis}_axis") or {}).get("tick_labels")
    if isinstance(v, list):
        out = []
        for x in v:
            if isinstance(x, (str, int, float)) and str(x).strip():
                out.append(str(x).strip())
        return out
    if isinstance(v, str):
        return [t for t in re.split(r"[\s;]+", v) if t]
    return []


def run(key: str, png: Path, arm: str, work: Path) -> dict:
    import cv2

    rgb = cv2.cvtColor(cv2.imread(str(png)), cv2.COLOR_BGR2RGB)
    fig = R.RasterFigure(rgb, 1.0, (0.0, 0.0), None, {"image": png.name})
    engine = OcrEngine()
    info: dict = {}
    t0 = time.perf_counter()
    if arm == "RC":
        glm = json.loads((work / "raw" / "glm" / "C" / f"{key}.json").read_text(encoding="utf-8"))
        by_sha = {c["sha256"]: (c.get("content") or "") for c in glm["crops"]}
        miss = []

        def glm_read_line(self, gray: np.ndarray) -> str:
            ok, buf = cv2.imencode(".png", np.ascontiguousarray(gray))
            sha = hashlib.sha256(buf.tobytes()).hexdigest()
            if sha in by_sha:
                return by_sha[sha].strip()
            miss.append(sha)
            return _ORIG(self, gray)

        OcrEngine.read_line = glm_read_line
        try:
            res = digitize_raster(fig, engine, sample_markers=True)
        finally:
            OcrEngine.read_line = _ORIG
        info = {"crops_answered_by_glm": len(by_sha), "crops_not_found": len(miss)}
    else:
        if arm == "RIE":
            t1 = parse_ie(json.loads((work / "raw" / "glm" / "IE" / f"{key}.json").read_text(encoding="utf-8"))
                          .get("content"))
            t1 = {"x_axis": {"tick_labels": ie_labels(t1, "x")}, "y_axis": {"tick_labels": ie_labels(t1, "y")}}
        else:
            t1 = RR.model_answer(work, "qwen", "T1", key)
        words, info = RR.hybrid_words(rgb, engine, t1)
        res = digitize_raster(fig, engine, corpus_words=words, sample_markers=True)
    return {"key": key, "arm": arm, "elapsed_s": round(time.perf_counter() - t0, 3), "axis_status": res["axis_status"],
            "label_source": res["label_source"], "x_axis": RR.to_json_axis(res["x_axis"]),
            "y_axis": RR.to_json_axis(res["y_axis"]), "reader": info,
            "series": [{"color": s["color"], "label_raw": s.get("label_raw"), "label_model": None,
                        "sampling": s["sampling"], "flags": s["flags"], "n_columns": s["n_columns"],
                        "points": [{"x": p["x"], "y": p["y"], "px": p["x_drawing"], "py": p["y_drawing"]}
                                   for p in s["points"]]} for s in res["series"]]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--arm", required=True, choices=["RC", "RIE", "RQ"])
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
            rec = {"key": key, "arm": args.arm, "error": f"{type(e).__name__}: {e}", "series": [],
                   "axis_status": "ERROR"}
        out.write_text(json.dumps(rec, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
        print(key, args.arm, rec.get("axis_status"), len(rec.get("series") or []), rec.get("reader"), flush=True)


if __name__ == "__main__":
    main()

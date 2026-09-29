"""CHART_MODELS_V1, GLM-OCR addendum — the label crops that route R gives Tesseract, captured byte for byte.

Route R (agent FD, code of the PUBLIC commit under test, unchanged) reads every tick-label blob of the four axis
strips with ``OcrEngine.read_line`` (Tesseract 5.5.3, psm 7, digit whitelist) on a crop that it cuts, upscales ×3
and pads (``raster.ocr_blobs``). This script wraps ``OcrEngine.read_line`` to keep each crop and Tesseract's reading,
runs ``digitize_raster(fig, OcrEngine(), sample_markers=True)`` exactly as arm G did, and writes

    <work>/glm_crops/<key>/NNN.png          the crop as Tesseract received it (8-bit grey, lossless PNG)
    <work>/glm_crops/<key>/manifest.json    per crop: index, size, sha256, Tesseract text; the calibration words

Check 0: the words of this run must equal the words arm G recorded (``routeR/G/<key>.json``); a difference stops the
script. Runs in the WSL venv of agent FD with ``PYTHONPATH=<PUBLIC>/src``.
Usage: capture_label_crops.py --registry figures_v1.json --work <work> [--keys ...] [--image <png> --key SMOKE]
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np

from vkm_corpus.figures import raster as R
from vkm_corpus.figures.labels_ocr import OcrEngine
from vkm_corpus.figures.raster_digitize import digitize_raster

_CAPTURED: list[dict] = []
_ORIG = OcrEngine.read_line


def _capturing_read_line(self, gray: np.ndarray) -> str:
    text = _ORIG(self, gray)
    _CAPTURED.append({"gray": np.ascontiguousarray(gray).copy(), "text": text, "whitelist": self.whitelist,
                      "psm": self.psm})
    return text


def capture(key: str, png: Path, out_dir: Path) -> dict:
    import cv2

    _CAPTURED.clear()
    OcrEngine.read_line = _capturing_read_line
    try:
        rgb = cv2.cvtColor(cv2.imread(str(png)), cv2.COLOR_BGR2RGB)
        fig = R.RasterFigure(rgb, 1.0, (0.0, 0.0), None, {"image": png.name})
        res = digitize_raster(fig, OcrEngine(), sample_markers=True)
    finally:
        OcrEngine.read_line = _ORIG
    out_dir.mkdir(parents=True, exist_ok=True)
    crops = []
    for i, c in enumerate(_CAPTURED):
        f = out_dir / f"{i:03d}.png"
        ok, buf = cv2.imencode(".png", c["gray"])
        f.write_bytes(buf.tobytes())
        crops.append({"i": i, "file": f.name, "h": int(c["gray"].shape[0]), "w": int(c["gray"].shape[1]),
                      "sha256": hashlib.sha256(buf.tobytes()).hexdigest(), "tesseract_text": c["text"],
                      "tesseract_psm": c["psm"], "tesseract_whitelist": c["whitelist"]})
    man = {"key": key, "image": png.name, "n_crops": len(crops), "crops": crops,
           "route_r_words": [t.text for t in res["words"]], "axis_status": res["axis_status"]}
    (out_dir / "manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")
    return man


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--keys", default="")
    ap.add_argument("--image", default="")
    ap.add_argument("--key", default="SMOKE")
    args = ap.parse_args()
    work = Path(args.work)
    if args.image:
        man = capture(args.key, Path(args.image), work / "glm_crops_smoke" / args.key)
        print(args.key, man["n_crops"], "crops", man["axis_status"])
        return
    reg = json.loads(Path(args.registry).read_text(encoding="utf-8"))
    keys = [k for k in (args.keys.split(",") if args.keys else reg["evaluation_set"]) if k]
    bad = 0
    for key in keys:
        man = capture(key, work / "inputs" / f"{key}.png", work / "glm_crops" / key)
        g = json.loads((work / "routeR" / "G" / f"{key}.json").read_text(encoding="utf-8"))
        same = man["route_r_words"] == g.get("tesseract_words")
        bad += not same
        print(key, man["n_crops"], "crops,", "check 0 (words equal to arm G):", same, flush=True)
    if bad:
        raise SystemExit(f"check 0 failed on {bad} figures")


if __name__ == "__main__":
    main()

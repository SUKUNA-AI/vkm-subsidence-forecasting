"""CHART_MODELS_V1, GLM-OCR addendum — requests to the GLM-OCR server exactly as the corpus pipeline builds them.

The request body is ``vkm_corpus.ocr.client.request_body`` with the pipeline's ``DEFAULT_SAMPLING`` (temperature 0,
top_p 1e-5, top_k 1, repetition_penalty 1.1, seed 0) and the pipeline's text cap (max_tokens 4096); images are sent
as lossless 8-bit grey PNG (the pipeline's crop mode "L"). The URL must pass the pipeline's loopback allowlist.

Variants (PREREGISTRATION_ADDENDUM_GLM.md):
  W   whole figure image, the pipeline's text prompt "Text Recognition:"
  IE  whole figure image, the model's documented information-extraction prompt (fixed JSON schema of T1)
  C   each label crop route R gives Tesseract (glm_crops/<key>/NNN.png), "Text Recognition:"

One request at a time; per request: wall time, usage, peak GPU memory of the card (nvidia-smi every 0.25 s).
Raw answers: <work>/raw/glm/<variant>/<key>.json (C: one file per figure with all crops).
Usage: run_glm.py --url <loopback URL> --variants W,IE,C --registry figures_v1.json --work <work>
       [--keys ...] [--image <png> --crops-dir <dir>]   (smoke: one image outside the benchmark set)
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import threading
import time
import urllib.request
from dataclasses import replace
from pathlib import Path

from vkm_corpus.ocr.client import OcrRequest, check_url, request_body
from vkm_corpus.ocr.prompts import DEFAULT_MODEL, DEFAULT_SAMPLING, prompt_for

TEXT_CAP = 4096          # PipelineConfig.max_tokens_by_task["text"]
IE_PROMPT = (
    "请按下列JSON格式输出图中信息:\n"
    "{\n"
    "    \"x_axis\": {\n"
    "        \"title\": \"\",\n"
    "        \"unit\": \"\",\n"
    "        \"tick_labels\": []\n"
    "    },\n"
    "    \"y_axis\": {\n"
    "        \"title\": \"\",\n"
    "        \"unit\": \"\",\n"
    "        \"tick_labels\": []\n"
    "    },\n"
    "    \"legend\": []\n"
    "}"
)


class VramSampler:
    def __init__(self, period: float = 0.25):
        self.period, self.peak, self._stop = period, 0, threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.is_set():
            try:
                out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                                     capture_output=True, text=True, timeout=5).stdout.strip().splitlines()
                self.peak = max(self.peak, int(out[0]))
            except Exception:
                pass
            self._stop.wait(self.period)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *a):
        self._stop.set()
        self._t.join()


def grey_png(path_or_bytes) -> tuple[bytes, int, int, str]:
    """8-bit grey lossless PNG (pipeline mode "L") and the sha256 of its pixels."""
    from PIL import Image

    im = Image.open(path_or_bytes if isinstance(path_or_bytes, (str, Path)) else io.BytesIO(path_or_bytes))
    im = im.convert("L")
    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=False)
    return buf.getvalue(), im.width, im.height, hashlib.sha256(im.tobytes()).hexdigest()


def call(url: str, png: bytes, w: int, h: int, pix_sha: str, prompt: str) -> tuple[dict | None, str | None, float]:
    req = OcrRequest(task="text", prompt=prompt, png=png, png_sha256=hashlib.sha256(png).hexdigest(),
                     pixel_sha256=pix_sha, width=w, height=h, mode="L",
                     sampling=replace(DEFAULT_SAMPLING, max_tokens=TEXT_CAP))
    body = request_body(req, DEFAULT_MODEL.served_model_name)
    http = urllib.request.Request(url + "/v1/chat/completions", data=json.dumps(body).encode(),
                                  headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(http, timeout=900) as r:
            resp, err = json.loads(r.read().decode()), None
    except Exception as e:           # the failure is a result, kept as such
        resp, err = None, f"{type(e).__name__}: {e}"
    return resp, err, time.perf_counter() - t0


def content_of(resp: dict | None) -> tuple[str | None, str | None, dict | None]:
    if not resp:
        return None, None, None
    ch = (resp.get("choices") or [{}])[0]
    return (ch.get("message") or {}).get("content"), ch.get("finish_reason"), resp.get("usage")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--variants", required=True)
    ap.add_argument("--registry", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--keys", default="")
    ap.add_argument("--image", default="")
    ap.add_argument("--crops-dir", default="")
    ap.add_argument("--baseline-mib", type=int, default=0)
    args = ap.parse_args()
    url = check_url(args.url)
    work = Path(args.work)
    reg = json.loads(Path(args.registry).read_text(encoding="utf-8"))
    if args.image:
        jobs = [("SMOKE", Path(args.image), Path(args.crops_dir) if args.crops_dir else None)]
        out_root = work / "smoke" / "glm"
    else:
        keys = [k for k in (args.keys.split(",") if args.keys else reg["evaluation_set"]) if k]
        jobs = [(k, work / "inputs" / f"{k}.png", work / "glm_crops" / k) for k in keys]
        out_root = work / "raw" / "glm"
    for variant in args.variants.split(","):
        for key, png_path, crops_dir in jobs:
            out = out_root / variant / f"{key}.json"
            if out.exists() and not args.image:
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            rec = {"key": key, "variant": variant, "model": DEFAULT_MODEL.as_dict(),
                   "sampling": replace(DEFAULT_SAMPLING, max_tokens=TEXT_CAP).as_dict(), "baseline_mib": args.baseline_mib}
            with VramSampler() as vs:
                if variant in ("W", "IE"):
                    png, w, h, pix = grey_png(png_path)
                    prompt = prompt_for("text") if variant == "W" else IE_PROMPT
                    resp, err, wall = call(url, png, w, h, pix, prompt)
                    c, fin, use = content_of(resp)
                    rec.update({"prompt": prompt, "input": {"w": w, "h": h, "pixel_sha256": pix, "mode": "L"},
                                "wall_s": round(wall, 3), "error": err, "content": c, "finish_reason": fin,
                                "usage": use})
                elif variant == "C":
                    man = json.loads((crops_dir / "manifest.json").read_text(encoding="utf-8"))
                    crops, total = [], 0.0
                    for cr in man["crops"]:
                        png, w, h, pix = grey_png(crops_dir / cr["file"])
                        resp, err, wall = call(url, png, w, h, pix, prompt_for("text"))
                        c, fin, use = content_of(resp)
                        total += wall
                        crops.append({"i": cr["i"], "sha256": cr["sha256"], "w": w, "h": h, "wall_s": round(wall, 3),
                                      "error": err, "content": c, "finish_reason": fin, "usage": use,
                                      "tesseract_text": cr["tesseract_text"]})
                    rec.update({"prompt": prompt_for("text"), "n_crops": len(crops), "crops": crops,
                                "wall_s": round(total, 3)})
                else:
                    raise ValueError(variant)
            rec["peak_card_mib"] = vs.peak
            out.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"glm {variant} {key}: {rec['wall_s']:.1f} s, peak {vs.peak} MiB", flush=True)


if __name__ == "__main__":
    main()

"""CHART_MODELS_V1 — send the benchmark images to one OpenAI-compatible model server and keep every raw answer.

One request at a time (the server runs one slot); for every (figure, task): wall time, token usage and server timings,
peak GPU memory of the whole card sampled with nvidia-smi every 0.25 s (the desktop holds ~3.7 GB of it, recorded
before the server started as ``--baseline-mib``). Raw answers go to ``<out>/raw/<arm>/<task>/<key>.json``; nothing is
parsed here. All machine paths and the server URL are arguments.

    python run_models.py --url <server URL> --model qwen35-9b-q8 --arm qwen --tasks T1,T2 \
        --prompts prompts_v1.json --registry figures_v1.json --work <work dir> [--keys S1,R2] [--image <png>]
        [--thinking]  (E1: Qwen with enable_thinking=true; answers go to <arm>_think)
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import threading
import time
import urllib.request
from pathlib import Path


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


def request(url: str, model: str, png: Path, prompt: str, max_tokens: int, extra: dict) -> dict:
    b64 = base64.b64encode(png.read_bytes()).decode()
    body = {"model": model, "temperature": 0.0, "top_p": 1.0, "seed": 0, "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                {"type": "text", "text": prompt}]}]}
    body.update(extra)
    req = urllib.request.Request(url.rstrip("/") + "/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=1800) as r:
        return json.loads(r.read().decode())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--arm", required=True, help="qwen | granite")
    ap.add_argument("--tasks", required=True)
    ap.add_argument("--prompts", required=True)
    ap.add_argument("--registry", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--keys", default="")
    ap.add_argument("--image", default="", help="smoke test: one image outside the benchmark set")
    ap.add_argument("--baseline-mib", type=int, default=0)
    ap.add_argument("--out-subdir", default="raw")
    ap.add_argument("--thinking", action="store_true")
    args = ap.parse_args()
    prompts = json.loads(Path(args.prompts).read_text(encoding="utf-8"))
    reg = json.loads(Path(args.registry).read_text(encoding="utf-8"))
    work = Path(args.work)
    keys = [k for k in (args.keys.split(",") if args.keys else reg["evaluation_set"]) if k]
    jobs = [("SMOKE", Path(args.image))] if args.image else [(k, work / "inputs" / f"{k}.png") for k in keys]
    extra = {"cache_prompt": False}
    arm_dir = args.arm
    if args.arm == "qwen":
        extra["chat_template_kwargs"] = dict(prompts["decoding"]["qwen_chat_template_kwargs"])
        if args.thinking:
            extra["chat_template_kwargs"]["enable_thinking"] = True
            arm_dir = "qwen_think"
    for task in args.tasks.split(","):
        ptxt = prompts[task]
        mt = prompts["decoding"]["max_tokens"][task]
        for key, png in jobs:
            out = work / args.out_subdir / arm_dir / task / f"{key}.json"
            if out.exists() and not args.image:
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            t0 = time.perf_counter()
            err = None
            with VramSampler() as vs:
                try:
                    resp = request(args.url, args.model, png, ptxt, mt, extra)
                except Exception as e:          # the failure is a result, kept as such
                    resp, err = None, f"{type(e).__name__}: {e}"
            wall = time.perf_counter() - t0
            rec = {"key": key, "arm": arm_dir, "task": task, "model": args.model, "image": png.name,
                   "wall_s": round(wall, 3), "peak_card_mib": vs.peak, "baseline_mib": args.baseline_mib,
                   "error": err, "response": resp}
            out.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
            usage = (resp or {}).get("usage") or {}
            print(f"{arm_dir} {task} {key}: {wall:.1f} s, peak {vs.peak} MiB, "
                  f"tokens {usage.get('prompt_tokens')}/{usage.get('completion_tokens')}, err={err}", flush=True)


if __name__ == "__main__":
    main()

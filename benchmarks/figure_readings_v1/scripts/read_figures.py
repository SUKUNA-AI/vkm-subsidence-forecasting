"""FIGURE_READINGS_V1 — send figure images to the local VLM servers and keep every raw answer.

Subcommands (all machine paths and server URLs are arguments):

  jobs   --work <dir> --manifests m1.json,m2.json [--out jobs.json]
         one job per image occurrence: key, source_id, locator, the PNG actually sent (``sent/<key>.png``: the
         original decoded and flattened on white, lossless; EMF/WMF: the rendered drawing), sha256 of the original
         part and of the sent PNG.
  qwen   --work <dir> --url <loopback URL> [--keys ...] [--stage classify|extract|both]
         Qwen3.5-9B through agent CH's request code (benchmarks/chart_models_v1/scripts/run_models.py: same body,
         temperature 0, top_p 1, seed 0, non-thinking, no prompt cache); first the classification prompt, then the
         extraction prompt of the class. Raw: raw/qwen/<CLASSIFY|EXTRACT>/<key>.json.
  glm    --work <dir> --url <loopback URL> [--keys ...]
         GLM-OCR second reader of the images classified TABLE_SCREENSHOT: the corpus pipeline's request body
         (vkm_corpus.ocr.client.request_body, DEFAULT_SAMPLING, table cap 6144, grey 8-bit PNG) with the pipeline's
         table prompt. Raw: raw/glm/TABLE/<key>.json.

One request at a time; per request wall time, usage, server timings and the peak memory of the whole card
(nvidia-smi every 0.25 s; the desktop's share is the baseline recorded before the server started).
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import sys
import time
import urllib.request
from dataclasses import replace
from pathlib import Path

HERE = Path(__file__).resolve().parent
BENCH = HERE.parent
REPO = BENCH.parent.parent
CH_SCRIPTS = REPO / "benchmarks" / "chart_models_v1" / "scripts"
QWEN_MODEL = {"model_id": "Qwen/Qwen3.5-9B", "weights_repo": "unsloth/Qwen3.5-9B-GGUF",
              "revision": "3885219b6810b007914f3a7950a8d1b469d598a5",
              "files": {"Qwen3.5-9B-Q8_0.gguf": "809626574d0cb43d4becfa56169980da2bb448f2299270f7be443cb89d0a6ae4",
                        "mmproj-F16.gguf": "f70dc3509053962b0d0d3ee8a7eacebf5d60aa560cad78254ae8698516ae029f"},
              "served_as": "qwen35-9b-q8",
              "server": "llama.cpp llama-server b11243-fc07d781e (CUDA 12), "
                        "ghcr.io/ggml-org/llama.cpp@sha256:1c568d229561bbd4577698f1f38d3ed9bf3f5ab343e04f7a508e11ef63f4ced1",
              "server_settings": "compose benchmarks/chart_models_v1/serving/compose.yml profile qwen: n-gpu-layers 99, "
                                 "ctx 32768, 1 slot, flash attention, image tokens 1024-4096, cache-ram 0, seed 0"}
TYPES = ["TABLE_SCREENSHOT", "MAP", "GEOLOGICAL_SECTION", "CHART", "SCHEME_DRAWING", "PHOTO", "OTHER"]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def prompts() -> dict:
    return json.loads((BENCH / "prompts_v1.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------------------------------------- jobs
def cmd_jobs(a) -> None:
    from PIL import Image

    work = Path(a.work)
    (work / "sent").mkdir(parents=True, exist_ok=True)
    jobs = []
    for m in [p for p in a.manifests.split(",") if p]:
        man = json.loads(Path(m).read_text(encoding="utf-8"))
        for r in man["images"]:
            if "key" not in r:
                continue
            src = work / (r.get("rendered") or {}).get("file", r["file"])
            im = Image.open(src)
            im.load()
            if im.mode in ("RGBA", "LA", "P", "PA") or "transparency" in im.info:
                im = im.convert("RGBA")
                bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
                bg.alpha_composite(im)
                im = bg
            im = im.convert("RGB")
            buf = io.BytesIO()
            im.save(buf, format="PNG")
            sent = work / "sent" / f"{r['key']}.png"
            sent.write_bytes(buf.getvalue())
            jobs.append({
                "key": r["key"], "source_id": r["source_id"], "container": r.get("container"),
                "locator": r.get("locator") or {"media_part": r.get("media_part"), "caption": r.get("caption"),
                                                 "caption_kind": r.get("caption_kind"), "paragraph": r.get("paragraph")},
                "original_file": r["file"], "original_sha256": r["sha256"], "original_format": r.get("format"),
                "rendered": r.get("rendered"), "sent_file": f"sent/{sent.name}",
                "sent_sha256": hashlib.sha256(buf.getvalue()).hexdigest(), "width": im.width, "height": im.height,
                "caption": r.get("caption"), "context_paragraph": r.get("context_paragraph", ""),
            })
    out = Path(a.out) if a.out else work / "jobs.json"
    out.write_text(json.dumps(jobs, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(jobs)} jobs -> {out.name}")


def select(jobs: list[dict], keys: str) -> list[dict]:
    if not keys:
        return jobs
    want = [k for k in keys.split(",") if k]
    return [j for j in jobs if j["key"] in want or any(j["key"].startswith(w.rstrip("*")) for w in want if w.endswith("*"))]


def is_loop(text: str | None, window: int = 40, max_unique: int = 6) -> bool:
    """A greedy repetition loop: the last `window` non-empty lines hold at most `max_unique` distinct lines, or they
    count up in a constant step ("380", "381", "382" … / "1485", "1490", …: a runaway enumeration, not labels)."""
    tail = [l.strip() for l in (text or "").splitlines() if l.strip()][-window:]
    if len(tail) >= window // 2 and len(set(tail)) <= max_unique:
        return True
    nums = []
    for l in tail:
        s = l.strip('",[]{} ')
        if s.lstrip("-").isdigit():
            nums.append(int(s))
    if len(nums) >= 20:
        steps = [b - a for a, b in zip(nums, nums[1:])]
        top = max(set(steps), key=steps.count)
        if top != 0 and steps.count(top) >= 0.9 * len(steps):
            return True
    return False


def parse_type(text: str | None) -> str | None:
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t[t.find("{"):] if "{" in t else t
    try:
        obj = json.loads(t[t.find("{"): t.rfind("}") + 1])
        ty = str(obj.get("type", "")).strip().upper()
    except Exception:
        ty = next((x for x in TYPES if x in text.upper()), "")
    return ty if ty in TYPES else None


# ---------------------------------------------------------------------------------------------------------- qwen
def cmd_qwen(a) -> None:
    ch = _load("ch_run_models", CH_SCRIPTS / "run_models.py")
    work = Path(a.work)
    jobs = select(json.loads((work / "jobs.json").read_text(encoding="utf-8")), a.keys)
    P = prompts()
    dec = P["decoding"]
    extra = {"cache_prompt": False, "chat_template_kwargs": dict(dec["qwen_chat_template_kwargs"])}
    stages = ["CLASSIFY", "EXTRACT"] if a.stage == "both" else [a.stage.upper()]
    for stage in stages:
        for j in jobs:
            out = work / "raw" / "qwen" / stage / f"{j['key']}.json"
            if out.exists() and not a.force:
                continue
            if stage == "CLASSIFY":
                task = "CLASSIFY"
            else:
                cls = work / "raw" / "qwen" / "CLASSIFY" / f"{j['key']}.json"
                if not cls.exists():
                    print("no classification for", j["key"]); continue
                crec = json.loads(cls.read_text(encoding="utf-8"))
                task = a.force_type or crec.get("type") or "OTHER"
            req_extra = dict(extra)
            retry_kind = None
            if stage == "EXTRACT_RETRY":
                # only answers of the first pass that ran into the token cap are asked once more, same prompt:
                # a greedy repetition loop (the tail repeats a few lines) -> same decoding plus presence_penalty;
                # a long answer that is not a loop (dense maps with hundreds of labels) -> same decoding, larger cap
                prev = work / "raw" / "qwen" / "EXTRACT" / f"{j['key']}.json"
                if not prev.exists():
                    continue
                prec = json.loads(prev.read_text(encoding="utf-8"))
                if prec.get("finish_reason") != "length" and not a.keys:
                    continue
                task = prec["task"]
                retry_kind = "loop_presence_penalty" if is_loop(prec.get("content")) else "long_answer_larger_cap"
                if retry_kind == "long_answer_larger_cap" and dec["max_tokens"][task] >= dec["LONG_RETRY_MAX_TOKENS"]:
                    print("no larger cap for", j["key"], "(identical request would repeat); left TRUNCATED_AT_CAP")
                    continue
                if retry_kind == "loop_presence_penalty":
                    req_extra["presence_penalty"] = dec["RETRY_PRESENCE_PENALTY"]
            if stage == "CLASSIFY":
                ptxt, mt = P[task], dec["max_tokens"][task]
            else:
                ptxt, mt = P[task] + "\n" + P["COMMON_SUFFIX"], dec["max_tokens"][task]
            if retry_kind == "long_answer_larger_cap":
                mt = dec["LONG_RETRY_MAX_TOKENS"]
            out.parent.mkdir(parents=True, exist_ok=True)
            t0 = time.perf_counter()
            err = None
            with ch.VramSampler() as vs:
                try:
                    resp = ch.request(a.url, QWEN_MODEL["served_as"], work / j["sent_file"], ptxt, mt, req_extra)
                except Exception as e:          # the failure is a result, kept as such
                    resp, err = None, f"{type(e).__name__}: {e}"
            wall = time.perf_counter() - t0
            ch0 = ((resp or {}).get("choices") or [{}])[0]
            content = (ch0.get("message") or {}).get("content")
            rec = {"key": j["key"], "reader": "qwen", "stage": stage, "task": task, "model": QWEN_MODEL,
                   "prompt_sha256": sha256_text(ptxt), "sampling": {"temperature": dec["temperature"],
                   "top_p": dec["top_p"], "seed": dec["seed"], "max_tokens": mt, **req_extra},
                   "sent_sha256": j["sent_sha256"], "wall_s": round(wall, 3), "peak_card_mib": vs.peak,
                   "baseline_mib": a.baseline_mib, "error": err, "content": content,
                   "finish_reason": ch0.get("finish_reason"), "usage": (resp or {}).get("usage"),
                   "timings": (resp or {}).get("timings"), "response": resp}
            if stage == "CLASSIFY":
                rec["type"] = parse_type(content)
            if retry_kind:
                rec["retry_kind"] = retry_kind
            out.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
            u = rec["usage"] or {}
            print(f"qwen {stage} {j['key']} {task}{' -> ' + str(rec.get('type')) if stage == 'CLASSIFY' else ''}: "
                  f"{wall:.1f} s, peak {vs.peak} MiB, tokens {u.get('prompt_tokens')}/{u.get('completion_tokens')}, "
                  f"finish {rec['finish_reason']}, err={err}", flush=True)


# ---------------------------------------------------------------------------------------------------------- glm
def cmd_glm(a) -> None:
    sys.path.insert(0, str(REPO / "src"))
    glm = _load("ch_run_glm", CH_SCRIPTS / "run_glm.py")
    from vkm_corpus.ocr.client import OcrRequest, check_url, request_body
    from vkm_corpus.ocr.prompts import DEFAULT_MODEL, DEFAULT_SAMPLING, prompt_for

    url = check_url(a.url)
    P = prompts()
    cap = P["decoding"]["glm_table"]["max_tokens"]
    sampling = replace(DEFAULT_SAMPLING, max_tokens=cap)
    work = Path(a.work)
    jobs = select(json.loads((work / "jobs.json").read_text(encoding="utf-8")), a.keys)
    if not a.keys:     # default: every image Qwen classified as a table
        def is_table(j):
            f = work / "raw" / "qwen" / "CLASSIFY" / f"{j['key']}.json"
            return f.exists() and json.loads(f.read_text(encoding="utf-8")).get("type") == "TABLE_SCREENSHOT"
        jobs = [j for j in jobs if is_table(j)]
    prompt = prompt_for("table")
    for j in jobs:
        out = work / "raw" / "glm" / "TABLE" / f"{j['key']}.json"
        if out.exists() and not a.force:
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        png, w, h, pix = glm.grey_png(work / j["sent_file"])
        req = OcrRequest(task="table", prompt=prompt, png=png, png_sha256=hashlib.sha256(png).hexdigest(),
                         pixel_sha256=pix, width=w, height=h, mode="L", sampling=sampling)
        body = request_body(req, DEFAULT_MODEL.served_model_name)
        http = urllib.request.Request(url + "/v1/chat/completions", data=json.dumps(body).encode(),
                                      headers={"Content-Type": "application/json"})
        t0 = time.perf_counter()
        with glm.VramSampler() as vs:
            try:
                with urllib.request.urlopen(http, timeout=900) as r:
                    resp, err = json.loads(r.read().decode()), None
            except Exception as e:           # the failure is a result, kept as such
                resp, err = None, f"{type(e).__name__}: {e}"
        wall = time.perf_counter() - t0
        c, fin, use = glm.content_of(resp)
        rec = {"key": j["key"], "reader": "glm-ocr", "stage": "TABLE", "model": DEFAULT_MODEL.as_dict(),
               "server": "vLLM v0.30.0, vllm/vllm-openai@sha256:8a69ffad015f138d7170c4ddc429e230a3bc1c1719f67e14324749df200a4b90 "
                         "(compose benchmarks/chart_models_v1/serving/compose.yml profile glm)",
               "prompt": prompt, "prompt_sha256": sha256_text(prompt), "sampling": sampling.as_dict(),
               "input": {"w": w, "h": h, "pixel_sha256": pix, "mode": "L", "png_sha256": req.png_sha256},
               "sent_sha256": j["sent_sha256"], "wall_s": round(wall, 3), "peak_card_mib": vs.peak,
               "baseline_mib": a.baseline_mib, "error": err, "content": c, "finish_reason": fin, "usage": use}
        out.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"glm TABLE {j['key']}: {wall:.1f} s, peak {vs.peak} MiB, tokens {(use or {}).get('completion_tokens')}, "
              f"finish {fin}, err={err}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("jobs"); s.add_argument("--work", required=True); s.add_argument("--manifests", required=True)
    s.add_argument("--out", default="")
    s = sub.add_parser("qwen"); s.add_argument("--work", required=True); s.add_argument("--url", required=True)
    s.add_argument("--keys", default=""); s.add_argument("--stage", default="both")
    s.add_argument("--force", action="store_true"); s.add_argument("--force-type", default="")
    s.add_argument("--baseline-mib", type=int, default=0)
    s = sub.add_parser("glm"); s.add_argument("--work", required=True); s.add_argument("--url", required=True)
    s.add_argument("--keys", default=""); s.add_argument("--force", action="store_true")
    s.add_argument("--baseline-mib", type=int, default=0)
    a = ap.parse_args()
    {"jobs": cmd_jobs, "qwen": cmd_qwen, "glm": cmd_glm}[a.cmd](a)


if __name__ == "__main__":
    main()

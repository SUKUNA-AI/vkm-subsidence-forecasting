"""V2 serving feasibility (PREREGISTRATION §7): query vectors from the same Q8_0 GGUF the RX580 service would load,
produced exactly as the service does (``vkm_corpus.embeddings.tokenize`` ids → ``llama-server /embedding`` →
``vkm_corpus.embeddings.postprocess.finalize_dense``), on a local llama.cpp CPU build as a stand-in for the Vulkan
backend (Vulkan ↔ reference parity is agent K's gate). Also: the text tower of a visual model vs a text model.

    q8 <key>[,<key>…]   — 189 queries → ``$V2_WORK/vec/<key>/queries_q8.f32.npy`` + parity (cos to the GPU bf16 query
                          vectors) + CPU latency; ``run_v2.py`` then adds ``E:<key>~q8`` / ``B:<key>~q8``
    tower <vis> <text>  — cos between the query vectors of a visual model and a text model (same text tower?)
    feasibility         — ``$V2_WORK/out/serving_v2.json``: per model GGUF / gate / RX580 VRAM and latency (agent K's
                          matrix, Q8_0), Q8_0 parity of the ranking (from ``results_v2_full.json`` when present),
                          re-encode cost on the 5070 Ti (measured throughput × units of the CURRENT snapshot)

Environment: ``V2_WORK``, ``VKM_MODELS_DIR``, ``V2_LLAMA_BIN`` (dir with ``llama-server``), ``V2_GGUF_ROOT``
(``<root>/<rx580_key>/<rx580_key>-Q8_0.gguf`` + ``SHA256SUMS``).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
from vkm_corpus.embeddings import specs as ES  # noqa: E402
from vkm_corpus.embeddings.llama import LlamaServerClient  # noqa: E402
from vkm_corpus.embeddings.postprocess import finalize_dense, l2_normalize  # noqa: E402
from vkm_corpus.embeddings.tokenize import SpecTokenizer  # noqa: E402
from vkm_corpus.retrieval_lab import bench as B  # noqa: E402

WORK = Path(os.environ["V2_WORK"])
CFG = json.loads((REPO / "benchmarks/retrieval_v2/configs/models_v2.json").read_text(encoding="utf-8"))
QUERY_MAX = int(CFG["query_max_tokens"])
UNITS_CURRENT = 205457
# agent K's RX580 matrix (docs/implementation_work/AGENT_K_RX580_MODEL_MATRIX.md §2–3, Q8_0, Vulkan RADV b11223):
# process VRAM after warm-up (MiB), query latency p50 / p95 (ms), gate verdict
K_MATRIX = {
    "jina-v5-nano-retrieval": (211.1, 9.7, 11.7, "PASS"), "qwen3-emb-0.6b": (1186.3, 28.0, 32.9, "PASS"),
    "bge-m3": (388.6, 32.0, 36.2, "PASS (dense)"), "jina-v5-small-retrieval": (1199.9, 28.0, 33.7, "PASS"),
    "giga-emb-480m": (610.6, 25.3, 29.7, "PASS"), "pplx-embed-0.6b": (735.9, 27.0, 32.1, "PASS"),
    "pplx-embed-context-0.6b": (739.0, 27.2, 32.5, "PASS"), "granite-311m-r2": (172.5, 11.6, 14.0, "PASS"),
    "granite-97m-r2": (100.9, 6.4, 6.8, "PASS"), "mdenseon": (172.0, 11.6, 14.6, "PASS"),
    "mlateon": (192.0, 11.9, 15.3, "PASS"),
}
RX580_VRAM_MIB = 8192
RX580_BUDGET_MIB = 7 * 1024


def model_cfg(key: str) -> dict:
    for m in CFG["text"] + CFG["visual"]:
        if m["key"] == key:
            return m
    raise SystemExit(f"unknown key {key}")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def gguf_of(rx_key: str) -> tuple[Path, str]:
    d = Path(os.environ["V2_GGUF_ROOT"]) / rx_key
    path = d / f"{rx_key}-Q8_0.gguf"
    want = None
    sums = d / "SHA256SUMS"
    if sums.is_file():
        for line in sums.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].lstrip("*").endswith(path.name):
                want = parts[0]
    got = sha256_file(path)
    if want and got != want:
        raise SystemExit(f"{path.name}: sha256 {got} != SHA256SUMS {want}")
    return path, got


def q8(key: str) -> dict:
    m = model_cfg(key)
    spec = ES.get(m["rx580_key"])
    if spec.pooling == "none":
        raise SystemExit(f"{key}: contextual/late models are not handled here")
    gguf, gsha = gguf_of(m["rx580_key"])
    hf = Path(os.environ["VKM_MODELS_DIR"]) / spec.model_id.replace("/", "__") / spec.model_revision
    tok = SpecTokenizer.from_dir(spec, hf)
    bench = B.load_benchmark(REPO / "benchmarks/retrieval_v0")
    qs = [(q.query_id, q.text) for q in bench.queries]
    port = 18411
    cmd = [str(Path(os.environ["V2_LLAMA_BIN"]) / "llama-server"), "-m", str(gguf), "--embeddings",
           "--pooling", spec.pooling, "-c", "4096", "-b", "4096", "-ub", "4096", "-np", "1", "-t", "16",
           "--host", "127.0.0.1", "--port", str(port), "--no-webui"]
    logf = open(WORK / "logs" / f"llama_{key}.log", "w", encoding="utf-8")
    proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT)
    client = LlamaServerClient(f"http://127.0.0.1:{port}", pooled=True, b64=False)
    try:
        t0 = time.time()
        while time.time() - t0 < 300:
            try:
                if client.health().get("http_status") == 200:
                    break
            except Exception:  # noqa: BLE001 - server still loading
                pass
            if proc.poll() is not None:
                raise SystemExit(f"llama-server exited {proc.returncode}; see logs/llama_{key}.log")
            time.sleep(0.5)
        vecs, lat, ntok = [], [], []
        for _qid, text in qs:
            enc = tok.encode(text, "query", max_len=QUERY_MAX)
            t1 = time.perf_counter()
            res = client.embed_ids([list(enc.ids)])
            lat.append((time.perf_counter() - t1) * 1000)
            ntok.append(len(enc.ids))
            out = finalize_dense(spec, res.vectors[0])
            # pplx: the model's own output is the int8 code (the GPU run used the model's int8 quantizer module)
            vecs.append(out.int8.astype(np.float32) if spec.output_transform == "tanh_int8" else out.vector)
    finally:
        client.close()
        proc.terminate()
        proc.wait(timeout=30)
        logf.close()
    q8v = l2_normalize(np.stack(vecs).astype(np.float32))
    out = WORK / "vec" / key
    np.save(out / "queries_q8.f32.npy", q8v)
    gpu = np.load(out / "queries.f32.npy")
    cos = (q8v * gpu).sum(axis=1)
    rep = {"key": key, "rx580_key": m["rx580_key"], "gguf": gguf.name, "gguf_sha256": gsha, "pooling": spec.pooling,
           "query_prefix": spec.query_prefix, "tokenizer_sha256": tok.tokenizer_sha256, "queries": len(qs),
           "cos_q8_vs_gpu_bf16": {"mean": float(cos.mean()), "min": float(cos.min()),
                                   "p01": float(np.percentile(cos, 1))},
           "tokens_per_query": {"mean": float(np.mean(ntok)), "max": int(np.max(ntok))},
           "cpu_latency_ms": {"p50": float(np.percentile(lat, 50)), "p95": float(np.percentile(lat, 95)),
                              "note": "llama.cpp CPU (16 threads), stand-in only; RX580 latency is K's matrix"},
           "llama_cpp": "local CPU build of the RX580 source tree (commit 4da6337767f9)"}
    (out / "serving_q8.json").write_text(json.dumps(rep, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(rep, ensure_ascii=False))
    return rep


def qvl(gguf_path: str) -> dict:
    """Qwen3-VL-Embedding-2B text tower as a llama.cpp GGUF (``qwen3vl`` arch, converted with the pinned llama.cpp):
    query ids = the model's chat template (system = the V2 query instruction, user = the query, generation prompt) +
    ``<|endoftext|>``, exactly the ids sentence-transformers fed the GPU run (checked by decoding them); last-token
    pooling; cos to the GPU bf16 query vectors of ``vis/qwen3-vl-2b``."""
    from transformers import AutoTokenizer

    m = model_cfg("qwen3-vl-2b")
    hf = Path(os.environ["VKM_MODELS_DIR"]) / "Qwen__Qwen3-VL-Embedding-2B" / "9f2f7e710d6d81056aa5c0a4f04764fec6bb7bda"
    tok = AutoTokenizer.from_pretrained(str(hf), local_files_only=True)
    eot = tok.convert_tokens_to_ids("<|endoftext|>")
    bench = B.load_benchmark(REPO / "benchmarks/retrieval_v0")
    qs = [(q.query_id, q.text) for q in bench.queries]
    gguf = Path(gguf_path)
    port = 18412
    cmd = [str(Path(os.environ["V2_LLAMA_BIN"]) / "llama-server"), "-m", str(gguf), "--embeddings", "--pooling",
           "last", "-c", "4096", "-b", "4096", "-ub", "4096", "-np", "1", "-t", "16", "--host", "127.0.0.1",
           "--port", str(port)]
    logf = open(WORK / "logs" / "llama_qwen3-vl-2b.log", "w", encoding="utf-8")
    proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT)
    client = LlamaServerClient(f"http://127.0.0.1:{port}", pooled=True, b64=False)
    try:
        t0 = time.time()
        while time.time() - t0 < 300:
            try:
                if client.health().get("http_status") == 200:
                    break
            except Exception:  # noqa: BLE001
                pass
            if proc.poll() is not None:
                raise SystemExit(f"llama-server exited {proc.returncode}")
            time.sleep(0.5)
        vecs, lat, ntok = [], [], []
        for _qid, text in qs:
            conv = [{"role": "system", "content": [{"type": "text", "text": m["query_prompt"]}]},
                    {"role": "user", "content": [{"type": "text", "text": text}]}]
            ids = tok.apply_chat_template(conv, add_generation_prompt=True, tokenize=True)
            if not isinstance(ids, list):     # transformers 5: a BatchEncoding (mapping), not a dict
                ids = ids["input_ids"]
            if ids and isinstance(ids[0], list):
                ids = ids[0]
            # sentence-transformers (the GPU run) ends the sequence with <|endoftext|> and pools that last token
            ids = list(ids) + [eot]
            t1 = time.perf_counter()
            res = client.embed_ids([list(ids)])
            lat.append((time.perf_counter() - t1) * 1000)
            ntok.append(len(ids))
            vecs.append(res.vectors[0])
    finally:
        client.close()
        proc.terminate()
        proc.wait(timeout=30)
        logf.close()
    q8v = l2_normalize(np.stack(vecs).astype(np.float32))
    out = WORK / "vis" / "qwen3-vl-2b"
    np.save(out / "queries_q8.f32.npy", q8v)
    gpu = np.load(out / "queries.f32.npy")
    cos = (q8v * gpu).sum(axis=1)
    rep = {"key": "qwen3-vl-2b", "gguf": gguf.name, "gguf_sha256": sha256_file(gguf), "queries": len(qs),
           "cos_q8_vs_gpu_bf16": {"mean": float(cos.mean()), "min": float(cos.min())},
           "tokens_per_query": {"mean": float(np.mean(ntok)), "max": int(np.max(ntok))},
           "cpu_latency_ms": {"p50": float(np.percentile(lat, 50)), "p95": float(np.percentile(lat, 95))},
           "conversion": "convert_hf_to_gguf.py (llama.cpp 4da6337767f9, text model, arch qwen3vl) → llama-quantize Q8_0"}
    (out / "serving_q8.json").write_text(json.dumps(rep, indent=1), encoding="utf-8")
    print(json.dumps(rep))
    return rep


def tower(vis: str, text: str) -> None:
    a = np.load(WORK / "vis" / vis / "queries.f32.npy")
    b = np.load(WORK / "vec" / text / "queries.f32.npy")
    cos = (a * b).sum(axis=1)
    rep = {"visual": vis, "text": text, "cos_mean": float(cos.mean()), "cos_min": float(cos.min()),
           "identical_within_1e-3": bool(cos.min() > 0.999)}
    (WORK / "vis" / vis / f"tower_vs_{text}.json").write_text(json.dumps(rep, indent=1), encoding="utf-8")
    print(json.dumps(rep))


def feasibility() -> None:
    res_path = WORK / "out" / "results_v2_full.json"
    results = json.load(open(res_path, encoding="utf-8")) if res_path.is_file() else {}
    resident = K_MATRIX["jina-v5-nano-retrieval"][0] + K_MATRIX["mlateon"][0]
    out = {}
    for m in CFG["text"]:
        key, rx = m["key"], m.get("rx580_key")
        meta_p = WORK / "vec" / key / "meta.json"
        meta = json.load(open(meta_p, encoding="utf-8")) if meta_p.is_file() else {}
        k = K_MATRIX.get(rx) if rx else None
        f = {"license": m["license"], "gguf_q8": bool(k), "gate": k[3] if k else "no GGUF on the pin",
             "rx580_vram_mib": k[0] if k else None, "rx580_query_ms_p50": k[1] if k else None,
             "rx580_query_ms_p95": k[2] if k else None}
        if k:
            # the dense slot is replaced (nano leaves), mLateOn stays resident
            f["rx580_resident_mib_after_switch"] = round(k[0] + K_MATRIX["mlateon"][0], 1)
            f["fits_rx580"] = f["rx580_resident_mib_after_switch"] <= RX580_BUDGET_MIB
        if meta.get("units_per_s"):
            f["reencode_current_snapshot_min_5070ti"] = round(UNITS_CURRENT / meta["units_per_s"] / 60, 1)
            f["units_per_s_5070ti"] = meta["units_per_s"]
        sq = WORK / "vec" / key / "serving_q8.json"
        if sq.is_file():
            s = json.load(open(sq, encoding="utf-8"))
            f["q8_cos_mean"] = s["cos_q8_vs_gpu_bf16"]["mean"]
            f["q8_cos_min"] = s["cos_q8_vs_gpu_bf16"]["min"]
        par = {}
        for label in ("verified", "verified+pooled"):
            p = (((results.get("sets") or {}).get(label) or {}).get("text") or {}).get("pairs", {})
            d = (p.get(f"E:{key}~q8 ~ E:{key}") or {}).get("ndcg@10")
            if d:
                par[label] = d["delta"]
        f["q8_delta_ndcg10"] = par or None
        f["q8_parity_ok"] = (bool(par) and all(abs(v) <= 0.005 for v in par.values())
                             and f.get("q8_cos_mean", 0) >= 0.999) if par else None
        f["feasible"] = bool(k and f.get("fits_rx580") and f["q8_parity_ok"])
        out[key] = f
    for m in CFG["visual"]:
        key = m["key"]
        tv = WORK / "vis" / key / "tower_vs_jina-small.json"
        f = {"license": m["license"]}
        if key == "omni-small" and tv.is_file():
            t = json.load(open(tv, encoding="utf-8"))
            f["text_tower"] = "jina-v5-small (Q8_0 GGUF, K gate PASS)" if t["identical_within_1e-3"] else "differs"
            f["text_tower_cos_min"] = t["cos_min"]
            js = out.get("jina-small", {})
            f["rx580_vram_mib"] = K_MATRIX["jina-v5-small-retrieval"][0]
            f["rx580_query_ms_p50"], f["rx580_query_ms_p95"] = K_MATRIX["jina-v5-small-retrieval"][1:3]
            f["feasible"] = bool(t["identical_within_1e-3"] and js.get("q8_parity_ok"))
        elif (WORK / "vis" / key / "serving_q8.json").is_file():
            s = json.load(open(WORK / "vis" / key / "serving_q8.json", encoding="utf-8"))
            f["text_tower"] = "own GGUF: " + s["conversion"]
            f["q8_cos_mean"] = s["cos_q8_vs_gpu_bf16"]["mean"]
            f["q8_cos_min"] = s["cos_q8_vs_gpu_bf16"]["min"]
            par = {}
            for label in ("verified", "verified+pooled"):
                for track in ("visual", "text"):
                    p = (((results.get("sets") or {}).get(label) or {}).get(track) or {}).get("pairs", {})
                    d = (p.get(f"E+VIS:{key}~q8 ~ E+VIS:{key}") or {}).get("ndcg@10")
                    if d:
                        par[f"{label}/{track}"] = d["delta"]
            f["q8_delta_ndcg10"] = par or None
            f["q8_parity_ok"] = (bool(par) and all(abs(v) <= 0.005 for v in par.values())
                                 and f["q8_cos_mean"] >= 0.999) if par else None
            f["q8_cpu_latency_ms"] = s.get("cpu_latency_ms")
            # diagnostic outside the rule: the F16 GGUF of the same text tower (queries_f16_diag.f32.npy)
            fd = WORK / "vis" / key / "serving_f16_diag.json"
            if fd.is_file():
                s16 = json.load(open(fd, encoding="utf-8"))
                d16 = {}
                for label in ("verified", "verified+pooled"):
                    for track in ("visual", "text"):
                        p = (((results.get("sets") or {}).get(label) or {}).get(track) or {}).get("pairs", {})
                        d = (p.get(f"E+VIS:{key}~f16 ~ E+VIS:{key}") or {}).get("ndcg@10")
                        if d:
                            d16[f"{label}/{track}"] = d["delta"]
                f["f16_diag"] = {"cos_mean": s16["cos_q8_vs_gpu_bf16"]["mean"],
                                 "cos_min": s16["cos_q8_vs_gpu_bf16"]["min"],
                                 "cpu_latency_ms": s16.get("cpu_latency_ms"), "delta_ndcg10": d16 or None,
                                 "parity_ok": (bool(d16) and all(abs(v) <= 0.005 for v in d16.values())
                                               and s16["cos_q8_vs_gpu_bf16"]["mean"] >= 0.999) if d16 else None}
            gdir = WORK / "gguf" / key
            if gdir.is_dir():
                f["gguf_file_mib"] = {g.name: round(g.stat().st_size / 2**20, 1) for g in sorted(gdir.glob("*.gguf"))}
            f["rx580_gate"] = "NOT_RUN: agent K's Vulkan parity gate on the RX580 is required before deployment"
            f["feasible"] = None
        else:
            f["text_tower"] = "no GGUF of the text tower on the pin (llama.cpp conversion not attempted)"
            f["feasible"] = None
        meta_p = WORK / "vis" / key / "meta.json"
        if meta_p.is_file():
            meta = json.load(open(meta_p, encoding="utf-8"))
            f["pages_per_s_5070ti"] = meta.get("pages_per_s")
            f["reencode_pages_min_5070ti"] = round(meta["n_pages"] / meta["pages_per_s"] / 60, 1)
        out[key] = f
    rep = {"rx580_budget_mib": RX580_BUDGET_MIB, "rx580_resident_now_mib": resident, "edge": "GTX 1650: 186 MiB free "
           "after the rerankers (MODEL_SERVICES.md §3) — not a target", "feasibility": out,
           "k_matrix_source": "docs/implementation_work/AGENT_K_RX580_MODEL_MATRIX.md (Q8_0, vk-mesa25)"}
    (WORK / "out").mkdir(exist_ok=True)
    (WORK / "out" / "serving_v2.json").write_text(json.dumps(rep, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(rep, indent=1, ensure_ascii=False)[:6000])


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "q8":
        for k in sys.argv[2].split(","):
            q8(k)
    elif cmd == "tower":
        tower(sys.argv[2], sys.argv[3])
    elif cmd == "qvl":
        qvl(sys.argv[2])
    elif cmd == "feasibility":
        feasibility()
    else:
        raise SystemExit(cmd)

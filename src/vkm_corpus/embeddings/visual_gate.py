"""RX580 gate of a visual query tower (agent VIS): the text tower of Qwen3-VL-Embedding-2B as a GGUF on llama.cpp
Vulkan, against the WORKSTATION reference, before the visual slot joins the resident models of the service.

Runs on CORE next to the GPU in the RX580 image (standard library + numpy + tokenizers; K's harness pieces:
:mod:`vkm_corpus.embeddings.bench` servers, fdinfo VRAM, latency statistics)::

    python -m vkm_corpus.embeddings.visual_gate run --key qwen3-vl-emb-2b --gguf <F16.gguf> --tokenizer-dir <hf dir> \
        --ref <reference dir> --out <receipt.json> [--ctx 2048 --ubatch 512 --parallel 2 --threads 2] \
        [--extra-arg=-ot --extra-arg='token_embd\\.weight=CPU']

The reference directory (``infra/workstation/visual_route/gate_reference.py``) holds the pre-registered probe: the
189 benchmark queries (``benchmarks/retrieval_v0/queries.jsonl``, sha256 pinned by V2's pre-registration), their token
ids (``tokens_query.jsonl``), the fp32 reference vectors of the official path (``q_dense.npy``, sentence-transformers
fp32 on the WORKSTATION GPU) and a fixed sample of page vectors (``d_dense.npy``) for the ranking agreement — documents
are images and are never encoded on the RX580, so the page side is the same matrix for both.

Checks (thresholds fixed here before the first RX580 run, MODEL_CHOICE):

* tokenization parity — the service tokenizer (:class:`SpecTokenizer`, pinned ``tokenizer.json``) rebuilds exactly the
  reference ids;
* vectors — cosine of RX580 vs reference query vectors: mean ≥ 0.999 (V2 §7), min ≥ 0.995;
* ranking over the page sample — top-10 and top-50 overlap ≥ 0.90, Spearman (top-100 union) ≥ 0.95 (K's gate);
* budget — device VRAM with the tower loaded (dense + late of the running service included) ≤ 7168 MiB;
* latency — sequential query p95 ≤ 1000 ms (the route adds this to the hybrid answer).

The receipt records the per-process VRAM (DRM fdinfo), the llama.cpp buffer sizes, GTT, host RSS, p50/p95 and the
verdict; ``expected_vram_mib`` of the service slot is taken from it.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import numpy as np

GATE_VISUAL = {"query_cos_mean_min": 0.999, "query_cos_min_min": 0.995, "top10_overlap_min": 0.90,
               "top50_overlap_min": 0.90, "spearman_min": 0.95, "device_vram_max_mib": 7168.0,
               "query_p95_max_ms": 1000.0}


def load_visual_reference(ref_dir: Path) -> dict[str, Any]:
    ids = []
    with open(ref_dir / "tokens_query.jsonl", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                row = json.loads(line)
                ids.append({"query_id": row.get("query_id"), "text": row.get("text"), "ids": list(row["ids"])})
    q = np.load(ref_dir / "q_dense.npy").astype(np.float32)
    d = np.load(ref_dir / "d_dense.npy").astype(np.float32)
    if len(ids) != q.shape[0] or q.shape[1] != d.shape[1]:
        raise ValueError("reference: token rows, query vectors and page vectors disagree")
    return {"queries": ids, "q": q, "d": d, "meta": json.loads((ref_dir / "meta.json").read_text(encoding="utf-8"))}


def evaluate(ref_q: np.ndarray, rx_q: np.ndarray, pages: np.ndarray) -> dict[str, Any]:
    """Vector and ranking agreement of the RX580 query vectors with the reference over a fixed page matrix."""
    from vkm_corpus.embeddings import parity
    from vkm_corpus.embeddings.postprocess import l2_normalize

    vec = parity.vector_agreement(ref_q, rx_q)
    P = l2_normalize(pages)
    ranking = parity.ranking_agreement(l2_normalize(ref_q) @ P.T, l2_normalize(rx_q) @ P.T).as_dict()
    return {"query_vectors": vec, "ranking": ranking}


def verdict(result: dict[str, Any], thresholds: dict[str, float] | None = None) -> dict[str, Any]:
    th = {**GATE_VISUAL, **(thresholds or {})}
    qv = (result.get("parity") or {}).get("query_vectors") or {}
    rk = (result.get("parity") or {}).get("ranking") or {}
    lat = result.get("query_latency_sequential") or {}
    dev = ((result.get("resident_after_load") or {}).get("device") or {}).get("vram_used_mib")
    checks = {
        "tokenization": bool(result.get("tokenization", {}).get("equal")),
        "query_cos_mean": qv.get("cos_mean", 0.0) >= th["query_cos_mean_min"],
        "query_cos_min": qv.get("cos_min", 0.0) >= th["query_cos_min_min"],
        "top10_overlap": rk.get("top10_overlap", 0.0) >= th["top10_overlap_min"],
        "top50_overlap": rk.get("top50_overlap", 0.0) >= th["top50_overlap_min"],
        "spearman": rk.get("spearman_top100", 0.0) >= th["spearman_min"],
        "device_vram": dev is not None and float(dev) <= th["device_vram_max_mib"],
        "latency_p95": lat.get("p95_ms") is not None and float(lat["p95_ms"]) <= th["query_p95_max_ms"],
    }
    return {"verdict": "PASS" if all(checks.values()) else "FAIL", "checks": checks, "thresholds": th}


def cmd_run(a: argparse.Namespace) -> int:
    from vkm_corpus.embeddings import bench, gpu
    from vkm_corpus.embeddings import postprocess as pp
    from vkm_corpus.embeddings.specs import get
    from vkm_corpus.embeddings.tokenize import SpecTokenizer

    spec = get(a.key)
    ref = load_visual_reference(Path(a.ref))
    tok = SpecTokenizer.from_dir(spec, Path(a.tokenizer_dir))
    mine = [list(tok.encode(q["text"], "query", max_len=a.query_max_len).ids) for q in ref["queries"]]
    ref_ids = [q["ids"] for q in ref["queries"]]
    tokenization = {"equal": mine == ref_ids, "n": len(mine),
                    "mismatch": [q["query_id"] for q, m in zip(ref["queries"], mine) if m != q["ids"]][:10],
                    "tokenizer_sha256": tok.tokenizer_sha256}
    cfg = bench.ServerConfig(a.key, a.gguf, a.port, ctx=a.ctx, batch=a.ubatch, ubatch=a.ubatch, parallel=a.parallel,
                             threads=a.threads, flash_attn=a.fa, extra=list(a.extra_arg or []))
    log_dir = Path(a.out).parent / "logs"
    srv = bench.Server(cfg, log_dir)
    base = gpu.device_memory()
    result: dict[str, Any] = {"key": a.key, "quant": a.quant, "gguf": Path(a.gguf).name, "config": cfg.__dict__,
                              "argv": cfg.argv(spec)[1:], "vk_env": bench.VK_ENV,
                              "llama_server_bin": bench.binary_info(), "baseline": bench.snapshot([]),
                              "baseline_vram_mib": bench._mib(base.vram_used), "reference": ref["meta"],
                              "probe": {"queries": len(ref_ids), "pages": int(ref["d"].shape[0])},
                              "tokenization": tokenization, "started_at": time.strftime("%FT%T%z")}

    def encode(batch_ids: list[list[int]]) -> list[np.ndarray]:
        res = srv.client.embed_ids(batch_ids)
        return [pp.finalize_dense(spec, v).vector for v in res.vectors]

    try:
        srv.start()
        result["load_s"] = round(srv.load_s or 0, 2)
        encode(ref_ids[:4])                                     # warm-up: pipelines, buffers resident
        result["resident_after_warmup"] = bench.snapshot([srv])
        result["buffers_mib"] = srv.buffers_from_log()
        rx = []
        for i in range(0, len(ref_ids), 8):
            rx += encode(ref_ids[i:i + 8])
        rx_q = np.stack(rx).astype(np.float32)
        result["parity"] = evaluate(ref["q"], rx_q, ref["d"])
        lat = []
        with bench.Sampler() as smp:
            for _ in range(a.repeats):
                for ids in ref_ids:
                    t0 = time.perf_counter()
                    encode([ids])
                    lat.append(time.perf_counter() - t0)
        result["query_latency_sequential"] = {**bench.latency_stats(lat), **smp.summary()}
        result["resident_after_load"] = bench.snapshot([srv])
        if a.save_vectors:
            np.save(Path(a.out).with_suffix(".queries.npy"), rx_q)
    finally:
        srv.stop()
    result["gate"] = verdict(result)
    result["finished_at"] = time.strftime("%FT%T%z")
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(result, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    print(json.dumps({"gate": result["gate"], "parity": result["parity"],
                      "latency": result["query_latency_sequential"],
                      "vram": result["resident_after_load"]}, ensure_ascii=False, default=str))
    return 0 if result["gate"]["verdict"] == "PASS" else 3


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m vkm_corpus.embeddings.visual_gate")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="RX580 parity, VRAM and latency of a visual query tower against the reference")
    r.add_argument("--key", required=True)
    r.add_argument("--gguf", required=True)
    r.add_argument("--quant", default="F16")
    r.add_argument("--tokenizer-dir", required=True)
    r.add_argument("--ref", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--ctx", type=int, default=2048)
    r.add_argument("--ubatch", type=int, default=512)
    r.add_argument("--parallel", type=int, default=2)
    r.add_argument("--threads", type=int, default=2)
    r.add_argument("--fa", default="auto")
    r.add_argument("--port", type=int, default=18230)
    r.add_argument("--query-max-len", type=int, default=512)
    r.add_argument("--repeats", type=int, default=3)
    r.add_argument("--extra-arg", action="append", help="extra llama-server argument (repeat), e.g. -ot")
    r.add_argument("--save-vectors", action="store_true")
    a = ap.parse_args(argv)
    return cmd_run(a)


if __name__ == "__main__":
    raise SystemExit(main())

"""Reference directory of the RX580 gate of the visual query tower (``python -m vkm_corpus.embeddings.visual_gate``).

WORKSTATION, retrieval-lab venv (torch, sentence-transformers 6.1): the pre-registered probe = the 189 benchmark
queries (``benchmarks/retrieval_v0/queries.jsonl``, sha256 pinned by V2's pre-registration). For each query:

* ``tokens_query.jsonl`` — the service's token ids (``SpecTokenizer`` with the pinned ``tokenizer.json`` and the chat
  template of the spec), checked against the model's own chat template + ``<|endoftext|>`` (the ids
  sentence-transformers feeds the model) — the run stops on any difference;
* ``q_dense.npy`` — the official path in **fp32** (sentence-transformers ``encode(query, prompt=instruction)``,
  last-token pooling, L2), CPU by default (K's reference precision); ``q_bf16_v2.npy`` — V2's GPU bf16 vectors for
  comparison when present;
* ``d_dense.npy`` / ``d_ids.json`` — the page vectors of the visual artifact (float16), the fixed page side of the
  ranking agreement;
* ``meta.json`` — model, revision, weights and tokenizer sha256, instruction, template sha256, library versions, probe
  sha256, source of the page vectors, cos(fp32, bf16).

    python gate_reference.py --artifact <visual artifact config dir> --out <dir> [--device cpu|cuda]
    # env: VKM_MODELS_DIR, V2_WORK (optional: V2's bf16 query vectors for the comparison)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
from vkm_corpus.embeddings.artifacts import current_rows, read_table  # noqa: E402
from vkm_corpus.embeddings.specs import get  # noqa: E402
from vkm_corpus.embeddings.tokenize import SpecTokenizer, file_sha256  # noqa: E402


def versions() -> dict:
    import importlib.metadata as md

    out = {"python": platform.python_version()}
    for pkg in ("torch", "transformers", "sentence-transformers", "tokenizers", "numpy"):
        try:
            out[pkg] = md.version(pkg)
        except md.PackageNotFoundError:
            out[pkg] = None
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifact", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--device", default="cpu")
    a = ap.parse_args()
    import torch
    from sentence_transformers import SentenceTransformer
    from transformers import AutoTokenizer

    spec = get("qwen3-vl-emb-2b")
    hf = Path(os.environ["VKM_MODELS_DIR"]) / spec.model_id.replace("/", "__") / spec.model_revision
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    qpath = REPO / "benchmarks/retrieval_v0/queries.jsonl"
    queries = [json.loads(line) for line in qpath.read_text(encoding="utf-8").splitlines() if line.strip()]
    st = SpecTokenizer.from_dir(spec, hf)
    tok = AutoTokenizer.from_pretrained(str(hf), local_files_only=True)
    eot = tok.convert_tokens_to_ids("<|endoftext|>")
    rows, bad = [], []
    for q in queries:
        mine = list(st.encode(q["text"], "query", max_len=512).ids)
        conv = [{"role": "system", "content": [{"type": "text", "text": spec.query_prefix}]},
                {"role": "user", "content": [{"type": "text", "text": q["text"]}]}]
        ref = tok.apply_chat_template(conv, add_generation_prompt=True, tokenize=True)
        ref = ref["input_ids"] if not isinstance(ref, list) else ref
        ref = ref[0] if ref and isinstance(ref[0], list) else ref
        if mine != list(ref) + [eot]:
            bad.append(q["query_id"])
        rows.append({"query_id": q["query_id"], "text": q["text"], "ids": mine})
    if bad:
        raise SystemExit(f"service ids differ from the chat template for {bad[:5]}")
    with open(out / "tokens_query.jsonl", "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    t0 = time.time()
    model = SentenceTransformer(str(hf), device=a.device, local_files_only=True,
                                model_kwargs={"dtype": torch.float32, "attn_implementation": "sdpa"})
    load_s = time.time() - t0
    t0 = time.time()
    qv = model.encode([q["text"] for q in queries], prompt=spec.query_prefix, batch_size=8, convert_to_numpy=True,
                      normalize_embeddings=True)
    enc_s = time.time() - t0
    qv = np.asarray(qv, dtype=np.float32)
    np.save(out / "q_dense.npy", qv)
    cmp_bf16 = None
    v2 = Path(os.environ.get("V2_WORK", "")) / "vis" / "qwen3-vl-2b" / "queries.f32.npy"
    if v2.is_file():
        b = np.load(v2)
        np.save(out / "q_bf16_v2.npy", b)
        c = (qv * b).sum(1)
        cmp_bf16 = {"mean": float(c.mean()), "min": float(c.min())}
    art = Path(a.artifact)
    table = read_table(art)
    cur = current_rows(table)
    ids = sorted(cur)
    D = np.stack([np.asarray(cur[i]["vector"], dtype=np.float32) for i in ids]).astype(np.float16)
    np.save(out / "d_dense.npy", D)
    (out / "d_ids.json").write_text(json.dumps(ids), encoding="utf-8")
    cfg = json.loads((art / "config.json").read_text(encoding="utf-8"))
    meta = {"schema": "vkm.visual_gate_reference/1", "key": spec.key, "model_id": spec.model_id,
            "model_revision": spec.model_revision, "weights_sha256": file_sha256(hf / "model.safetensors"),
            "tokenizer_sha256": st.tokenizer_sha256, "instruction": spec.query_prefix,
            "template_sha256": hashlib.sha256(spec.query_template.encode("utf-8")).hexdigest(),
            "reference": f"sentence-transformers fp32 on {a.device} (official path), L2", "queries": len(rows),
            "probe": "benchmarks/retrieval_v0/queries.jsonl", "probe_sha256": file_sha256(qpath),
            "ids_equal_chat_template": True, "pages": len(ids), "pages_dtype": "float16",
            "page_vectors_config_signature": cfg.get("config_signature"), "cos_fp32_vs_v2_bf16": cmp_bf16,
            "load_s": round(load_s, 1), "encode_s": round(enc_s, 1), "env": versions(),
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    (out / "meta.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    main()

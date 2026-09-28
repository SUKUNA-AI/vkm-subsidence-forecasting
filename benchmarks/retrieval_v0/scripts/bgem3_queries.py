"""BGE-M3 (BAAI/bge-m3 @ 5617a9f6) query encodings for all 189 benchmark queries, fp32 CPU, FlagEmbedding semantics:
dense = normalised CLS; sparse = relu(sparse_linear) per token, max over repeats, special tokens dropped;
colbert = normalised colbert_linear(h[1:]) over the attention mask (CLS dropped, EOS kept)."""
import json
import os
import time
from pathlib import Path

import numpy as np

from vkm_corpus.retrieval_lab.encoders import BGEM3Encoder, load_specs

V0 = Path(os.environ["J_V0"])
spec = load_specs("benchmarks/retrieval_v0/configs/models.json")["D9"]
queries = [json.loads(l) for l in open("benchmarks/retrieval_v0/queries.jsonl", encoding="utf-8") if l.strip()]
t0 = time.time()
enc = BGEM3Encoder(spec, os.environ["VKM_MODELS_DIR"], device="cpu", precision="fp32", batch_size=16, threads=8)
t_load = time.time() - t0
t0 = time.time()
out = enc.encode_all_queries([q["text"] for q in queries])
dt = time.time() - t0
with open(V0 / "rx" / "q" / "q_bgem3.jsonl", "w", encoding="utf-8") as f:
    for q, d, s, c in zip(queries, out["dense"], out["sparse"], out["multivector"]):
        f.write(json.dumps({"query_id": q["query_id"], "dense": [round(float(x), 7) for x in d], "sparse": s,
                            "colbert": np.round(c, 7).tolist()}) + "\n")
print(json.dumps({"n": len(queries), "load_s": round(t_load, 1), "encode_s": round(dt, 1),
                  "ms_per_query": round(dt * 1000 / len(queries), 1), "max_query_tokens": spec.max_query_tokens,
                  "colbert_tokens_p50": int(np.median([len(c) for c in out["multivector"]])),
                  "sparse_terms_p50": int(np.median([len(s) for s in out["sparse"]]))}))

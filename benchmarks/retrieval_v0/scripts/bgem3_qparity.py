"""Parity of the lab's BGE-M3 query encodings vs K's FlagEmbedding query encodings (npz)."""
import json
import os
import sys
from pathlib import Path

import numpy as np

V0 = Path(os.environ["J_V0"])
npz = Path(sys.argv[1]) if len(sys.argv) > 1 else V0 / "rx" / "bgem3_smoke" / "queries.npz"
z = np.load(npz, allow_pickle=False)
mine = {json.loads(l)["query_id"]: json.loads(l) for l in open(V0 / "rx" / "q" / "q_bgem3.jsonl", encoding="utf-8")}
dc, mc, ntok_eq, sp_l1 = [], [], 0, []
for i, qid in enumerate(z["query_ids"]):
    m = mine[str(qid)]
    a, b = z["dense"][i], np.asarray(m["dense"], dtype=np.float32)
    dc.append(float(a @ b / np.linalg.norm(a) / np.linalg.norm(b)))
    kv = z["mv"][z["mv_offsets"][i]:z["mv_offsets"][i + 1]]
    mv = np.asarray(m["colbert"], dtype=np.float32)
    if len(kv) == len(mv):
        ntok_eq += 1
        mc.append(float(np.min(np.sum(kv * mv, axis=1) / np.linalg.norm(kv, axis=1) / np.linalg.norm(mv, axis=1))))
    ks = dict(zip(z["sparse_ids"][z["sparse_offsets"][i]:z["sparse_offsets"][i + 1]].tolist(),
                  z["sparse_weights"][z["sparse_offsets"][i]:z["sparse_offsets"][i + 1]].tolist()))
    ms = {int(k): v for k, v in m["sparse"].items()}
    keys = set(ks) | set(ms)
    sp_l1.append(sum(abs(ks.get(k, 0.0) - ms.get(k, 0.0)) for k in keys))
print(json.dumps({"n": len(z["query_ids"]), "dense_cos_min": round(min(dc), 6), "dense_cos_mean": round(float(np.mean(dc)), 6),
                  "mv_same_token_count": ntok_eq, "mv_token_cos_min": round(min(mc), 6) if mc else None,
                  "sparse_l1_max": round(max(sp_l1), 5), "sparse_l1_mean": round(float(np.mean(sp_l1)), 5)}))

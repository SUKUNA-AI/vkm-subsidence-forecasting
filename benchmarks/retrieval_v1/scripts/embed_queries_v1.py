"""V1: encode the benchmark queries with the resident RX580 service (dense jina-v5-nano + late mLateOn, Q8_0).

Runs inside the ``rx580-retrieval`` container of the CORE compose (``docker exec -i <container> python -c ...``): the
service's own token file (``VKM_RX580_TOKEN_FILE``) and loopback port; read-only (``POST /embed/query``). Input: JSONL
``{"query_id", "text"}`` on stdin; output: one JSONL record per query on stdout (vectors, token counts, service
``encode_ms`` per role and the wall time of the call). Sequential calls, no bursts.

    mode ``both``   — one call per query with role ``both`` (dense and late concurrently, as in the service)
    mode ``probe``  — latency probe: role ``dense`` then role ``late`` per query, no vectors
"""
import json
import os
import sys
import time
import urllib.request

URL = os.environ.get("J_EMBED_URL", "http://127.0.0.1:8790")
MODE = sys.argv[1] if len(sys.argv) > 1 else "both"
TOKEN = open(os.environ["VKM_RX580_TOKEN_FILE"], encoding="utf-8").read().strip()


def call(text: str, role: str, vectors: bool) -> tuple[dict, float]:
    body = json.dumps({"text": text, "role": role, "include_vectors": vectors}).encode("utf-8")
    req = urllib.request.Request(URL + "/embed/query", data=body,
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + TOKEN})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=120) as r:
        resp = json.loads(r.read().decode("utf-8"))
    return resp, (time.perf_counter() - t0) * 1000


KEEP = ("model", "signature", "n_tokens", "encode_ms", "dimension", "token_vectors", "vector", "vectors")
for line in sys.stdin:
    if not line.strip():
        continue
    q = json.loads(line)
    if MODE == "both":
        resp, wall = call(q["text"], "both", True)
        rec = {"query_id": q["query_id"], "wall_ms": round(wall, 1)}
        for role in ("dense", "late"):
            rec[role] = {k: v for k, v in resp[role].items() if k in KEEP}
    else:
        rec = {"query_id": q["query_id"]}
        for role in ("dense", "late"):
            resp, wall = call(q["text"], role, False)
            rec[role] = {"wall_ms": round(wall, 1), "encode_ms": resp[role].get("encode_ms"),
                         "n_tokens": resp[role].get("n_tokens")}
    print(json.dumps(rec), flush=True)

"""Run on CORE (host network): encode benchmark queries through a retrieval service /embed/query (role both or one)."""
import json
import sys
import time
import urllib.request

port, role, out_path = int(sys.argv[1]), sys.argv[2], sys.argv[3]
queries = [json.loads(l) for l in open("/work/q/queries.jsonl", encoding="utf-8") if l.strip()]
t0 = time.time()
with open(out_path, "w", encoding="utf-8") as out:
    for q in queries:
        body = json.dumps({"text": q["text"], "role": role, "include_vectors": True}).encode("utf-8")
        req = urllib.request.Request(f"http://127.0.0.1:{port}/embed/query", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.loads(r.read().decode("utf-8"))
        rec = {"query_id": q["query_id"]}
        for k in ("dense", "late"):
            if k in resp:
                rec[k] = {x: resp[k][x] for x in resp[k] if x in ("model", "signature", "n_tokens", "encode_ms",
                                                                   "dimension", "vector", "vectors")}
        out.write(json.dumps(rec) + "\n")
print(json.dumps({"n": len(queries), "seconds": round(time.time() - t0, 1), "port": port, "role": role}))

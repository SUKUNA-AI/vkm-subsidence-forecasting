"""Query-encode latency on an idle RX580 service: N sequential /embed/query calls per (port, role)."""
import json
import statistics
import sys
import time
import urllib.request

port, role, n = int(sys.argv[1]), sys.argv[2], int(sys.argv[3])
qs = [json.loads(l)["text"] for l in open("/work/q/queries.jsonl", encoding="utf-8")][:n]
wall, svc = [], []
for text in qs:
    body = json.dumps({"text": text, "role": role, "include_vectors": False}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/embed/query", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.loads(r.read())
    wall.append((time.perf_counter() - t0) * 1000)
    part = d.get(role) or {}
    if "encode_ms" in part:
        svc.append(part["encode_ms"])
q = lambda xs, p: sorted(xs)[min(len(xs) - 1, int(p * len(xs)))] if xs else None
print(json.dumps({"port": port, "role": role, "n": len(wall), "wall_ms_p50": round(statistics.median(wall), 1),
                  "wall_ms_p95": round(q(wall, 0.95), 1),
                  "service_ms_p50": round(statistics.median(svc), 1) if svc else None}))

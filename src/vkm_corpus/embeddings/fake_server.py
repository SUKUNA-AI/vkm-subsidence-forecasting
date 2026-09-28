"""A stand-in for ``llama-server`` in process-management tests (no model, no GPU): accepts the llama-server command
line, serves ``GET /health`` and ``POST /embedding`` (token-id lists) with deterministic vectors.

``python -m vkm_corpus.embeddings.fake_server -m x.gguf --embeddings --pooling cls … --port 18999 [--dim 16]
[--die-after N]`` — ``--die-after`` makes the process exit after N embedding requests (restart tests).
"""
from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from vkm_corpus.embeddings.fakes import _vec


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--pooling", default="cls")
    ap.add_argument("--dim", type=int, default=16)
    ap.add_argument("--die-after", type=int, default=0)
    args, _unknown = ap.parse_known_args(argv)
    served = {"n": 0}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # quiet
            pass

        def _send(self, code: int, body: object) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/health":
                self._send(200, {"status": "ok"})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            n = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(n) or b"{}")
            seqs = body.get("content", [])
            b64 = body.get("encoding_format") == "base64"
            out = []
            for i, s in enumerate(seqs):
                if args.pooling == "none":
                    rows = [_vec(f"{j}|{t}", args.dim) for j, t in enumerate(s)]
                    if b64:   # the VKM patch 0003 answer shape
                        import base64

                        import numpy as np
                        raw = np.stack(rows).astype("<f4").tobytes()
                        out.append({"index": i, "embedding_b64": base64.b64encode(raw).decode(), "n_rows": len(rows),
                                    "n_cols": args.dim})
                        continue
                    emb = [r.tolist() for r in rows]
                else:
                    emb = [_vec(f"{list(s)}", args.dim).tolist()]
                out.append({"index": i, "embedding": emb})
            self._send(200, out)
            served["n"] += 1
            if args.die_after and served["n"] >= args.die_after:
                self.wfile.flush()
                sys.stdout.flush()
                import os
                os._exit(3)

    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""``vkm-corpus retrieval …`` — the RX580 retrieval service (agent K).

* ``retrieval serve [--host H] [--port P]`` — resident encoders + API (``VKM_RX580_CONFIG``);
* ``retrieval health --url URL`` — print the ``/health`` of a running service.
"""
from __future__ import annotations

import argparse
import json


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from vkm_corpus.logs import configure
    from vkm_corpus.retrieval_service import SERVICE_NAME
    from vkm_corpus.retrieval_service.app import build_production_app

    configure(SERVICE_NAME)
    uvicorn.run(build_production_app(), host=args.host, port=args.port, log_level="warning", access_log=False)
    return 0


def _health(args: argparse.Namespace) -> int:
    import urllib.request

    with urllib.request.urlopen(args.url.rstrip("/") + "/health", timeout=10) as r:  # noqa: S310 (operator URL)
        body = json.loads(r.read())
    print(json.dumps(body, indent=1, ensure_ascii=False))
    return 0 if body.get("status") == "ok" else 1


def register(subparsers) -> None:
    p = subparsers.add_parser("retrieval", help="RX580 retrieval service (resident dense + late encoders)")
    sub = p.add_subparsers(dest="retrieval_cmd", required=True)
    s = sub.add_parser("serve", help="run the service")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8790)
    s.set_defaults(func=_serve)
    s = sub.add_parser("health", help="query /health")
    s.add_argument("--url", required=True)
    s.set_defaults(func=_health)

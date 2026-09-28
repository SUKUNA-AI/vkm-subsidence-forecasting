"""``vkm-corpus api`` — serve the VKM API, export its OpenAPI document, check the canonical store.

* ``serve [--host H] [--port P]`` — uvicorn over :func:`vkm_corpus.api.app.build_from_settings` (CANONICAL root only);
* ``openapi [--out FILE]`` — the OpenAPI JSON of the current code (built over a synthetic canon; no data needed);
* ``check`` — open the canonical DuckDB read-only and print snapshot vs ``CURRENT`` (H-48).
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from vkm_corpus.api.app import build_from_settings
    from vkm_corpus.logs import configure

    configure("vkm-api", log_dir=Path(args.log_dir) if args.log_dir else None)
    uvicorn.run(build_from_settings(), host=args.host, port=args.port, log_config=None, access_log=False,
                proxy_headers=False)
    return 0


def _openapi(args: argparse.Namespace) -> int:
    from vkm_corpus.api.app import ApiConfig, create_app
    from vkm_corpus.api.fixtures import synthetic_service

    with tempfile.TemporaryDirectory(prefix="vkm-openapi-") as tmp:
        service, _canon, _fakes = synthetic_service(Path(tmp))
        spec = create_app(service, ApiConfig(read_tokens={"x": "read"})).openapi()
    text = json.dumps(spec, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8", newline="\n")
    else:
        sys.stdout.write(text)
    return 0


def _check(_args: argparse.Namespace) -> int:
    from vkm_corpus.api.canon import CanonStore
    from vkm_corpus.config import load_settings

    store = CanonStore.from_data_root(load_settings().require_data_root())
    status = store.status()
    print(json.dumps(status, ensure_ascii=False, indent=1, default=str))
    return 0 if status["up_to_date"] else 2


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("api", help="VKM API (FastAPI /v1)")
    sub = parser.add_subparsers(dest="api_command", required=True)
    p = sub.add_parser("serve", help="serve the API (CANONICAL data root, read-only)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--log-dir", default=None, help="optional directory of the rotating JSON log")
    p.set_defaults(func=_serve)
    p = sub.add_parser("openapi", help="write the OpenAPI JSON of the API")
    p.add_argument("--out", default=None)
    p.set_defaults(func=_openapi)
    p = sub.add_parser("check", help="canonical DuckDB snapshot vs CURRENT")
    p.set_defaults(func=_check)

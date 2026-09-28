"""``vkm-corpus mcp`` — serve an MCP server over streamable HTTP, run the acceptance scenario §60.

* ``serve --kind read|admin [--host H] [--port P]`` — ``vkm-corpus`` (read) or ``vkm-corpus-admin`` (plan-first);
* ``acceptance [--url URL | --dry-run] [--query Q] [--out FILE]`` — scripted MCP client, JSON receipt.
"""
from __future__ import annotations

import argparse


def _serve(args: argparse.Namespace) -> int:
    from vkm_corpus.mcp.http import serve

    serve(args.kind, args.host, args.port)
    return 0


def _acceptance(args: argparse.Namespace) -> int:
    from vkm_corpus.mcp.acceptance import main

    argv = (["--dry-run"] if args.dry_run else ["--url", args.url or ""]) + ["--query", args.query]
    if args.out:
        argv += ["--out", args.out]
    return main(argv)


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("mcp", help="VKM Corpus MCP servers (read / admin)")
    sub = parser.add_subparsers(dest="mcp_command", required=True)
    p = sub.add_parser("serve", help="serve one MCP server over streamable HTTP")
    p.add_argument("--kind", choices=["read", "admin"], required=True)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.set_defaults(func=_serve)
    p = sub.add_parser("acceptance", help="scripted MCP client of acceptance §60")
    p.add_argument("--url", default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--query", default="оседание земной поверхности мульда сдвижения")
    p.add_argument("--out", default=None)
    p.set_defaults(func=_acceptance)

"""Command line for people and scripts: ``python -m vkm_drawio.cli <command>`` (the same service as the MCP tools).

Commands: ``status``; ``create --root R --path P --spec SPEC.json [--overwrite]``; ``read --root R --path P``;
``export --root R --path P --format svg|png|pdf|jpg [--page N] [--all-pages] [--out-path P] [--overwrite]``;
``canonicalize --root R --path P``; ``list --root R [--pattern G]``. Output: one JSON document on stdout; exit code 0
on success, 1 on a tool failure (the JSON then has ``ok: false`` and the error).

``register(subparsers)`` adds the same commands as a group of another CLI.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from vkm_drawio.errors import ToolFailure
from vkm_drawio.model import DiagramSpec
from vkm_drawio.ops import Canonicalize
from vkm_drawio.service import DrawioService
from vkm_drawio.workspace import Workspace


def _service() -> DrawioService:
    return DrawioService(Workspace.from_env())


def _run(args: argparse.Namespace) -> dict[str, Any]:
    svc = _service()
    if args.command == "status":
        return svc.status()
    if args.command == "create":
        spec = DiagramSpec.model_validate_json(Path(args.spec).read_text(encoding="utf-8"))
        return svc.create(args.root, args.path, spec, args.overwrite)
    if args.command == "read":
        return svc.read(args.root, args.path, not args.no_geometry)
    if args.command == "export":
        return svc.export(args.root, args.path, args.format, args.page, args.all_pages, args.scale, args.border,
                          args.transparent, None if args.embed is None else args.embed == "yes", args.out_root,
                          args.out_path, args.overwrite, args.embed_fonts)
    if args.command == "canonicalize":
        current = svc.ws.resolve(args.root, args.path).read_bytes()
        return svc.update(args.root, args.path, hashlib.sha256(current).hexdigest(), [Canonicalize(op="canonicalize")])
    if args.command == "list":
        return svc.list(args.root, args.pattern)
    raise SystemExit(f"unknown command {args.command}")


def _add_commands(sub: argparse._SubParsersAction) -> None:
    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--root", choices=["public", "work"], required=True)
        p.add_argument("--path", required=True, help="path relative to the root")

    sub.add_parser("status", help="draw.io availability and roots")
    p = sub.add_parser("create", help="create a .drawio file from a JSON spec")
    common(p)
    p.add_argument("--spec", required=True, help="DiagramSpec JSON file")
    p.add_argument("--overwrite", action="store_true")
    p = sub.add_parser("read", help="read a diagram")
    common(p)
    p.add_argument("--no-geometry", action="store_true")
    p = sub.add_parser("export", help="export with the draw.io CLI")
    common(p)
    p.add_argument("--format", choices=["png", "svg", "pdf", "jpg"], required=True)
    p.add_argument("--page", type=int, default=1)
    p.add_argument("--all-pages", action="store_true")
    p.add_argument("--scale", type=float)
    p.add_argument("--border", type=int, default=10)
    p.add_argument("--transparent", action="store_true")
    p.add_argument("--embed", choices=["yes", "no"])
    p.add_argument("--out-root", choices=["public", "work"])
    p.add_argument("--out-path")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--embed-fonts", action="store_true", help="svg: embed fonts")
    p = sub.add_parser("canonicalize", help="rewrite a .drawio file in the canonical form")
    common(p)
    p = sub.add_parser("list", help="list diagrams of a root")
    p.add_argument("--root", choices=["public", "work"], required=True)
    p.add_argument("--pattern", default="**/*.drawio")


def _main_with(args: argparse.Namespace) -> int:
    try:
        out: dict[str, Any] = {"ok": True, "result": _run(args)}
        code = 0
    except ToolFailure as exc:
        out, code = {"ok": False, "error": exc.as_dict()}, 1
    sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return code


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("drawio", help="deterministic draw.io diagrams (vkm-drawio)")
    _add_commands(parser.add_subparsers(dest="command", required=True))
    parser.set_defaults(func=_main_with)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vkm_drawio.cli", description=__doc__.splitlines()[0])
    _add_commands(parser.add_subparsers(dest="command", required=True))
    return _main_with(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())

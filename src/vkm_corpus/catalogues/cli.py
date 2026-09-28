"""``vkm-corpus catalogues …`` — the PUBLIC evidence catalogues as a served DuckDB pack.

* ``catalogues pack --repo PUBLIC_ROOT --out DIR [--commit SHA] [--allow-dirty]`` — CSV under ``catalogues/`` and
  ``evidence/`` → ``DIR/catalogues.duckdb`` + ``DIR/manifest.json`` (git commit, sha256 and rows per file);
* ``catalogues publish --pack DIR [--data-root ROOT]`` — copy to ``ROOT/derived/catalogues/<pack_id>/`` and point
  ``derived/catalogues/CURRENT`` at it (atomic replace);
* ``catalogues status [--data-root ROOT]`` — the served pack (CURRENT, commit, counts).

The data root defaults to ``$VKM_DATA_ROOT``. Output is JSON on stdout (no corpus text; paths only as given).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def _data_root(args: argparse.Namespace) -> Path:
    if args.data_root:
        return Path(args.data_root)
    from vkm_corpus.config import load_settings

    return load_settings().require_data_root()


def cmd_pack(args: argparse.Namespace) -> int:
    from vkm_corpus.catalogues.pack import PackError, pack

    try:
        manifest = pack(args.repo, args.out, commit=args.commit, allow_dirty=args.allow_dirty,
                        roots=tuple(args.root) if args.root else ("catalogues", "evidence"))
    except PackError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, **{k: manifest[k] for k in ("pack_id", "git_commit", "working_tree", "n_files",
                                                               "n_rows", "content_sha256")},
                      "db": manifest["db"], "tables": {f["table"]: f["rows"] for f in manifest["files"]}},
                     ensure_ascii=False, indent=1))
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    from vkm_corpus.catalogues.pack import PackError, publish

    try:
        info = publish(_data_root(args), args.pack)
    except PackError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, **info}, ensure_ascii=False, indent=1))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from vkm_corpus.catalogues.store import CatalogueStore, CataloguesUnavailable

    store = CatalogueStore(_data_root(args))
    try:
        meta = store.meta()
        tables = store.tables()
    except CataloguesUnavailable as exc:
        print(json.dumps({"ok": False, "published": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, "published": True, **{k: meta.get(k) for k in (
        "pack_id", "git_commit", "working_tree", "content_sha256", "packed_at", "n_files", "n_rows")},
        "n_tables": len(tables)}, ensure_ascii=False, indent=1))
    return 0


def register(subparsers) -> None:
    p = subparsers.add_parser("catalogues", help="PUBLIC evidence catalogues as a served DuckDB pack")
    sub = p.add_subparsers(dest="catalogues_cmd", metavar="<command>")
    a = sub.add_parser("pack", help="CSV catalogues of a PUBLIC checkout → catalogues.duckdb + manifest.json")
    a.add_argument("--repo", required=True, help="PUBLIC checkout (root with catalogues/ and evidence/)")
    a.add_argument("--out", required=True, help="output directory (outside the repository)")
    a.add_argument("--commit", default=None, help="PUBLIC commit of the files when git cannot read the checkout")
    a.add_argument("--allow-dirty", action="store_true", help="pack uncommitted catalogue files (pack id -dirty-)")
    a.add_argument("--root", action="append", default=None, help="directory to pack (repeatable; default "
                                                                  "catalogues and evidence)")
    a.set_defaults(func=cmd_pack)
    b = sub.add_parser("publish", help="publish a pack under derived/catalogues/<pack_id>/ and point CURRENT at it")
    b.add_argument("--pack", required=True, help="directory written by `catalogues pack`")
    b.add_argument("--data-root", default=None, help="data root (default $VKM_DATA_ROOT)")
    b.set_defaults(func=cmd_publish)
    c = sub.add_parser("status", help="the served pack")
    c.add_argument("--data-root", default=None, help="data root (default $VKM_DATA_ROOT)")
    c.set_defaults(func=cmd_status)
    p.set_defaults(func=lambda args: (p.print_help(), 2)[1])

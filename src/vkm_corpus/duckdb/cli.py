"""``vkm-corpus duckdb`` — rebuild the DuckDB query layer from a snapshot (CANONICAL root only), show its status."""
from __future__ import annotations

import json

from vkm_corpus.config import load_settings


def _layout():
    from vkm_corpus.parquet.layout import CanonLayout

    return CanonLayout(load_settings().require_data_root())


def cmd_build(args) -> int:
    from vkm_corpus.duckdb.build import build_duckdb

    print(json.dumps(build_duckdb(_layout(), args.snapshot), ensure_ascii=False, indent=1, sort_keys=True))
    return 0


def cmd_status(args) -> int:
    from vkm_corpus.duckdb.build import duckdb_status

    print(json.dumps(duckdb_status(_layout()), ensure_ascii=False, indent=1, sort_keys=True, default=str))
    return 0


def register(subparsers) -> None:
    p = subparsers.add_parser("duckdb", help="DuckDB query layer (rebuilt from a snapshot)")
    sub = p.add_subparsers(dest="duckdb_cmd", metavar="<command>")
    s = sub.add_parser("build", help="build duckdb/vkm_corpus.duckdb from CURRENT (fingerprints verified)")
    s.add_argument("--snapshot", default=None)
    s.set_defaults(func=cmd_build)
    sub.add_parser("status", help="snapshot of the DuckDB file versus CURRENT").set_defaults(func=cmd_status)
    p.set_defaults(func=lambda args: (p.print_help(), 2)[1])

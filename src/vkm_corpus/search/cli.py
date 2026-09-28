"""``vkm-corpus search …``: build, status, query, smoke and rollback of the OpenSearch projection."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from vkm_corpus.config import load_settings
from vkm_corpus.graph.common import LOGS_DIR, ProjectionError, failures, normalize_value


def _print(obj: Any) -> None:
    print(json.dumps(normalize_value(obj), indent=2, ensure_ascii=False, sort_keys=True))


def _fail(code: str, message: str, details: Any = None) -> int:
    print(json.dumps({"status": "FAILED", "error": {"code": code, "message": message, "details": details}},
                     indent=2, ensure_ascii=False, default=str), file=sys.stderr)
    return 1


def _client(settings: Any) -> Any:
    from vkm_corpus.search.client import connect

    return connect(settings)


def _prefix(settings: Any, args: argparse.Namespace) -> str:
    from vkm_corpus.search.indexer import check_prefix

    return check_prefix(getattr(args, "prefix", None) or settings.opensearch_index_prefix)


def _cmd_build(args: argparse.Namespace) -> int:
    from vkm_corpus.logs import configure
    from vkm_corpus.search.indexer import BuildOptions, build

    settings = load_settings()
    log_dir = None
    try:
        log_dir = settings.require_data_root() / LOGS_DIR if settings.data_root else None
    except Exception:
        log_dir = None
    configure("vkm-search-indexer", log_dir=log_dir, level=settings.log_level)
    options = BuildOptions(snapshot_id=args.snapshot, prefix=args.prefix, plan_only=args.plan_only,
                           keep_failed=args.keep_failed, prune=args.prune, smoke=args.smoke,
                           hash_files=not args.skip_file_hash)
    try:
        receipt = build(settings, options)
    except ProjectionError as exc:
        return _fail(exc.code, exc.message, exc.details)
    keys = ("status", "build_id", "indices", "counts", "expected_counts", "doc_stream_sha256", "receipt_ref",
            "timings_s")
    _print({k: receipt.get(k) for k in keys if k in receipt})
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    from vkm_corpus.search.indexer import status

    settings = load_settings()
    try:
        _print(status(_client(settings), _prefix(settings, args)))
    except ProjectionError as exc:
        return _fail(exc.code, exc.message)
    return 0


def _parse_filters(items: list[str]) -> dict[str, Any]:
    """``name=value`` (repeatable; ``a,b`` → list); ranges as ``year>=2000`` / ``year<=2010``."""
    filters: dict[str, Any] = {}
    for item in items:
        for op, key in ((">=", "gte"), ("<=", "lte")):
            if op in item:
                name, value = item.split(op, 1)
                filters.setdefault(name.strip(), {})[key] = int(value) if value.strip().lstrip("-").isdigit() else value
                break
        else:
            name, _, value = item.partition("=")
            name = name.strip()
            if name in ("has_preview",):
                filters[name] = value.strip().lower() in ("1", "true", "yes")
            elif name in ("available_until", "unknown_policy"):
                filters[name] = value.strip()
            else:
                filters.setdefault(name, []).extend(v.strip() for v in value.split(",") if v.strip())
    return filters


def _cmd_query(args: argparse.Namespace) -> int:
    from vkm_corpus.search.query import SearchRequest, SearchRequestError, rerank_candidates, search

    settings = load_settings()
    try:
        client = _client(settings)
        request = SearchRequest(query=args.text, kinds=tuple(args.kind or ["PAGE"]),
                                filters=_parse_filters(args.filter or []), size=args.size,
                                include_secondary_layers=args.all_layers, include_duplicates=args.duplicates)
        response = search(client, request, _prefix(settings, args))
    except SearchRequestError as exc:
        return _fail(exc.code, exc.message)
    except ProjectionError as exc:
        return _fail(exc.code, exc.message)
    out = response.as_dict()
    if args.rerank:
        out["rerank_candidates"] = rerank_candidates(response, mode=args.rerank)
    _print(out)
    return 0


def _cmd_smoke(args: argparse.Namespace) -> int:
    from vkm_corpus.search.smoke import run_smoke

    settings = load_settings()
    try:
        results = run_smoke(_client(settings), _prefix(settings, args), t0=args.t0, min_total=args.min_total)
    except ProjectionError as exc:
        return _fail(exc.code, exc.message)
    _print([r.as_dict() for r in results])
    return 1 if failures(results) else 0


def _cmd_rollback(args: argparse.Namespace) -> int:
    from vkm_corpus.search.indexer import rollback

    settings = load_settings()
    try:
        _print(rollback(_client(settings), _prefix(settings, args), tuple(args.types)))
    except ProjectionError as exc:
        return _fail(exc.code, exc.message)
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    from vkm_corpus.search.mappings import INDEX_TYPES
    from vkm_corpus.search.query import KINDS

    parser = subparsers.add_parser("search", help="OpenSearch retrieval projection (agent E)")
    sub = parser.add_subparsers(dest="search_cmd", metavar="<command>")

    b = sub.add_parser("build", help="build all indices from the CURRENT canonical snapshot and swap aliases")
    b.add_argument("--snapshot", help="snapshot id (default: canonical/CURRENT)")
    b.add_argument("--prefix", help="index/alias prefix (default VKM_OPENSEARCH_INDEX_PREFIX)")
    b.add_argument("--plan-only", action="store_true", help="preflight and expected counts only")
    b.add_argument("--keep-failed", action="store_true", help="keep indices of a failed build for diagnosis")
    b.add_argument("--prune", action="store_true", help="delete builds older than the previous COMPLETE one")
    b.add_argument("--smoke", action="store_true", help="run the Russian smoke on the new indices before the swap")
    b.add_argument("--skip-file-hash", action="store_true", help="check manifest file sizes but not sha256")
    b.set_defaults(func=_cmd_build)

    s = sub.add_parser("status", help="aliases, builds, snapshot identity and counts")
    s.add_argument("--prefix")
    s.set_defaults(func=_cmd_status)

    q = sub.add_parser("query", help="BM25 query with filters; prints hits (IDs, scores, highlights)")
    q.add_argument("text")
    q.add_argument("--kind", action="append", choices=KINDS, help="PAGE (default), BLOCK, FIGURE, TABLE, FORMULA")
    q.add_argument("--filter", action="append", metavar="NAME=VALUE",
                   help="e.g. source_id=VKM-SRC-014, source_scope=SKRU1, year>=2000, available_until=2010-12-31, "
                        "unknown_policy=EXCLUDE")
    q.add_argument("--size", type=int, default=20)
    q.add_argument("--all-layers", action="store_true", help="include secondary text layers (H-30)")
    q.add_argument("--duplicates", action="store_true", help="do not collapse duplicate pages")
    q.add_argument("--rerank", choices=["text", "visual"], help="also print rerank candidate references")
    q.add_argument("--prefix")
    q.set_defaults(func=_cmd_query)

    sm = sub.add_parser("smoke", help="Russian search smoke (acceptance §59) on the aliased indices")
    sm.add_argument("--t0", default="2009-12-31", help="availability date for the filter check")
    sm.add_argument("--min-total", type=int, default=10, help="minimum hits for «оседание земной поверхности»")
    sm.add_argument("--prefix")
    sm.set_defaults(func=_cmd_smoke)

    r = sub.add_parser("rollback", help="move aliases back to the previous COMPLETE build")
    r.add_argument("--types", nargs="+", default=list(INDEX_TYPES), choices=INDEX_TYPES)
    r.add_argument("--prefix")
    r.set_defaults(func=_cmd_rollback)

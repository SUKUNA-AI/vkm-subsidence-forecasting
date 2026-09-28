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
        only = tuple(x.strip().upper() for x in args.only.split(",") if x.strip()) if args.only else None
        results = run_smoke(_client(settings), _prefix(settings, args), t0=args.t0, min_total=args.min_total,
                            only=only)
    except ProjectionError as exc:
        return _fail(exc.code, exc.message)
    _print([r.as_dict() for r in results])
    return 1 if failures(results) else 0


def _cmd_export_units(args: argparse.Namespace) -> int:
    from pathlib import Path

    from vkm_corpus.search.vectors import export_units

    settings = load_settings()
    try:
        man = export_units(settings, snapshot_id=args.snapshot, variant=args.variant,
                           out_dir=Path(args.out) if args.out else None, force=args.force)
    except ProjectionError as exc:
        return _fail(exc.code, exc.message, exc.details)
    _print(man)
    return 0


def _cmd_build_vectors(args: argparse.Namespace) -> int:
    from vkm_corpus.logs import configure
    from vkm_corpus.search.vectors import VectorBuildOptions, build_vectors

    settings = load_settings()
    configure("vkm-search-vectors", level=settings.log_level)
    options = VectorBuildOptions(embeddings=args.embeddings, snapshot_id=args.snapshot, units=args.units,
                                 prefix=args.prefix, plan_only=args.plan_only, keep_failed=args.keep_failed,
                                 force_merge=args.force_merge, verify_checksums=not args.skip_checksums,
                                 skip_if_current=args.skip_if_current)
    try:
        receipt = build_vectors(settings, options)
    except ProjectionError as exc:
        return _fail(exc.code, exc.message, exc.details)
    keys = ("status", "build_id", "index", "current_snapshot_id", "embeddings", "units", "checks_64", "checks",
            "alias_actions", "pruned", "receipt_ref", "timings_s", "vectors")
    _print({k: receipt.get(k) for k in keys if k in receipt})
    return 0


def _cmd_hybrid(args: argparse.Namespace) -> int:
    from vkm_corpus.search.hybrid import EmbedClient, HybridError, HybridRequest, hybrid_search
    from vkm_corpus.search.query import SearchRequestError

    settings = load_settings()
    embed = EmbedClient(args.embed_url or settings.embed_url, settings.embed_token)
    try:
        out = hybrid_search(_client(settings), embed, HybridRequest(
            query=args.text, kinds=tuple(args.kind or ["PAGE"]), filters=_parse_filters(args.filter or []),
            size=args.size, candidates=args.candidates, include_duplicates=args.duplicates, late=args.late,
            late_candidates=args.late_candidates, bib_route=args.bib_route), _prefix(settings, args))
    except (SearchRequestError, HybridError, ProjectionError) as exc:
        return _fail(exc.code, exc.message)
    finally:
        embed.close()
    _print(out)
    return 0


def _cmd_hybrid_smoke(args: argparse.Namespace) -> int:
    """Hybrid search through the VKM API (read token from VKM_API_TOKEN_FILE): each query needs ≥ 1 hit whose
    trace has a fused rank and at least one stage rank (BM25, dense or the bibliographic channel); with ``--late``
    also the late stage (a stage record, every hit with a late status, ≥ 1 hit with a late rank); with
    ``--expect-route`` the route the server chose (``bibliographic`` / ``default``). Latency per query (client and
    server timings) and p50/p95 are reported; the exit code is the verdict."""
    import time

    import httpx

    settings = load_settings()
    url = (args.api_url or settings.api_url or "").rstrip("/")
    if not url or not settings.api_token:
        return _fail("E_NO_SERVICE", "VKM_API_URL (or --api-url) and VKM_API_TOKEN_FILE are required")
    results, ok_all = [], True
    with httpx.Client(base_url=url, headers={"Authorization": f"Bearer {settings.api_token}"}, timeout=60.0,
                      trust_env=False) as http:
        for text in args.query or SMOKE_QUERIES:
            entry: dict[str, Any] = {"query": text}
            req: dict[str, Any] = {"query": text, "kinds": ["PAGE"], "limit": args.limit}
            if args.late is not None:
                req.update({"late": args.late, "late_candidates": args.late_candidates})
            t0 = time.perf_counter()
            try:
                r = http.post("/v1/search/hybrid", json=req)
                body = r.json()
            except (httpx.HTTPError, ValueError) as exc:
                body, r = {"ok": False, "error": {"code": type(exc).__name__}}, None
            entry["elapsed_ms"] = round((time.perf_counter() - t0) * 1e3, 1)
            items = body.get("items") or []
            traces = [(it.get("record") or {}).get("trace") or {} for it in items]
            good = [t for t in traces if t.get("fused_rank") and
                    (t.get("bm25_rank") or t.get("dense_rank") or t.get("bib_rank"))]
            entry.update({"http_status": r.status_code if r is not None else None, "ok": bool(body.get("ok")),
                          "hits": len(items), "hits_with_trace": len(good),
                          "both_stages": sum(1 for t in good if t.get("bm25_rank") and t.get("dense_rank")),
                          "top": [{"id": (it.get("envelope") or {}).get("object_id"),
                                   "trace": {k: t.get(k) for k in ("fused_rank", "bm25_rank", "dense_rank",
                                                                   "bib_rank", "late_rank") if k in t}}
                                  for it, t in list(zip(items, traces))[:3]],
                          "error": (body.get("error") or {}).get("code")})
            record = (body.get("item") or {}).get("record") or {}
            entry["server_ms"] = {k: v for k, v in (record.get("timings_ms") or {}).items()
                                  if k in ("total", "embed", "late", "bm25_page", "dense_page", "bib_scan",
                                           "bib_page_filter")}
            bib = (record.get("stages") or {}).get("bib_route")
            entry["route"] = record.get("route")
            if isinstance(bib, dict):
                entry["bib_route"] = {k: bib.get(k) for k in ("status", "cues", "weak_cues", "units_scanned",
                                                              "pages_kept") if k in bib}
            entry["pass"] = entry["ok"] and entry["hits"] >= 1 and entry["hits_with_trace"] == entry["hits"]
            if args.expect_route:
                entry["pass"] = entry["pass"] and entry["route"] == args.expect_route
            if args.late:
                late = (record.get("stages") or {}).get("late")
                entry.update({"late_stage": isinstance(late, dict),
                              "hits_with_late_rank": sum(1 for t in traces if t.get("late_rank")),
                              "hits_with_late_status": sum(1 for t in traces if t.get("late_status")),
                              "late_scored": late.get("scored") if isinstance(late, dict) else None,
                              "late_unscored": late.get("unscored") if isinstance(late, dict) else None,
                              "late_pack": ((late.get("store") or {}).get("pack_id")
                                            if isinstance(late, dict) else None)})
                entry["pass"] = entry["pass"] and entry["late_stage"] and entry["hits_with_late_rank"] >= 1 and \
                    entry["hits_with_late_status"] == entry["hits"]
            ok_all = ok_all and entry["pass"]
            results.append(entry)
    lat = sorted(e["elapsed_ms"] for e in results)
    summary = {"n": len(lat), "p50_ms": lat[(len(lat) - 1) // 2] if lat else None,
               "p95_ms": lat[min(len(lat) - 1, int(round(0.95 * (len(lat) - 1))))] if lat else None,
               "max_ms": lat[-1] if lat else None}
    _print({"status": "PASS" if ok_all else "FAIL", "late": args.late, "latency": summary, "queries": results})
    return 0 if ok_all else 1


SMOKE_QUERIES = ("оседание земной поверхности", "ползучесть каменной соли", "мульда сдвижения")


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
    sm.add_argument("--only", help="comma-separated check ids to run (e.g. Q5); default: all")
    sm.set_defaults(func=_cmd_smoke)

    eu = sub.add_parser("export-units", help="embedding units (vkm-units-v1) of the CURRENT snapshot → "
                                             "derived/embeddings/units/<snapshot>/ (input of `embed encode`)")
    eu.add_argument("--snapshot", help="snapshot id (default: canonical/CURRENT)")
    eu.add_argument("--variant", default="A", choices=["A", "B", "C", "D"], help="context variant (J §4)")
    eu.add_argument("--out", help="output directory (default under the data root)")
    eu.add_argument("--force", action="store_true", help="rewrite even if an identical export exists")
    eu.set_defaults(func=_cmd_export_units)

    bv = sub.add_parser("build-vectors", help="§64 checks + versioned k-NN index of dense embeddings + alias swap")
    bv.add_argument("--embeddings", required=True,
                    help="derived artifact config dir, or JSON: `embed encode` report / {\"artifact_dir\": …}")
    bv.add_argument("--snapshot", help="assert this is the CURRENT snapshot (refuse otherwise)")
    bv.add_argument("--units", help="units export directory (default: that of CURRENT and the embedding text rule)")
    bv.add_argument("--prefix")
    bv.add_argument("--plan-only", action="store_true", help="checks only, no index")
    bv.add_argument("--keep-failed", action="store_true", help="keep the index of a failed build for diagnosis")
    bv.add_argument("--force-merge", action="store_true", help="force-merge to one segment (slow for large indices)")
    bv.add_argument("--skip-checksums", action="store_true", help="do not re-hash the Parquet parts")
    bv.add_argument("--skip-if-current", action="store_true",
                    help="no-op when the alias already serves a COMPLETE build of this snapshot, signature and units")
    bv.set_defaults(func=_cmd_build_vectors)

    hy = sub.add_parser("hybrid", help="hybrid query (BM25 + dense via the RX580 service, RRF) with stage trace")
    hy.add_argument("text")
    hy.add_argument("--kind", action="append", choices=["PAGE", "FIGURE", "TABLE", "FORMULA"])
    hy.add_argument("--filter", action="append", metavar="NAME=VALUE")
    hy.add_argument("--size", type=int, default=10)
    hy.add_argument("--candidates", type=int, default=100)
    hy.add_argument("--duplicates", action="store_true")
    hy.add_argument("--embed-url", help="RX580 retrieval service (default VKM_EMBED_URL)")
    hy.add_argument("--prefix")
    hy.add_argument("--late", dest="late", action="store_true", default=None,
                    help="late interaction stage (MaxSim on the RX580) over the RRF top --late-candidates")
    hy.add_argument("--no-late", dest="late", action="store_false")
    hy.add_argument("--late-candidates", type=int, default=100)
    hy.add_argument("--bib-route", dest="bib_route", action="store_true", default=None,
                    help="force the bibliographic route (BIB_ENTRY channel); default: the query's cues decide")
    hy.add_argument("--no-bib-route", dest="bib_route", action="store_false")
    hy.set_defaults(func=_cmd_hybrid)

    hs = sub.add_parser("hybrid-smoke", help="3 Russian hybrid queries through the VKM API; exit code = verdict")
    hs.add_argument("--api-url", help="VKM API base URL (default VKM_API_URL)")
    hs.add_argument("--query", action="append", help="override the smoke queries")
    hs.add_argument("--limit", type=int, default=10)
    hs.add_argument("--late", dest="late", action="store_true", default=None,
                    help="request the late stage and check its trace (default: the server's default)")
    hs.add_argument("--no-late", dest="late", action="store_false")
    hs.add_argument("--late-candidates", type=int, default=100)
    hs.add_argument("--expect-route", choices=("bibliographic", "default"),
                    help="every query must take this route (bibliographic: the BIB_ENTRY channel was applied)")
    hs.set_defaults(func=_cmd_hybrid_smoke)

    r = sub.add_parser("rollback", help="move aliases back to the previous COMPLETE build")
    r.add_argument("--types", nargs="+", default=list(INDEX_TYPES), choices=INDEX_TYPES)
    r.add_argument("--prefix")
    r.set_defaults(func=_cmd_rollback)

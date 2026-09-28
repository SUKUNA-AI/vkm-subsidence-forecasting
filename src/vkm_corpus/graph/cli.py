"""``vkm-corpus graph …``: DDL, rebuild (``--mode wipe``), verify and status of the DOCUMENT graph."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from vkm_corpus.config import load_settings
from vkm_corpus.graph.common import LOGS_DIR, ProjectionError, normalize_value


def _print(obj: Any) -> None:
    print(json.dumps(normalize_value(obj), indent=2, ensure_ascii=False, sort_keys=True))


def _configure_logs(settings: Any) -> None:
    from vkm_corpus.logs import configure

    log_dir = None
    if settings.data_root is not None:
        try:
            log_dir = settings.require_data_root() / LOGS_DIR
        except Exception:
            log_dir = None
    configure("vkm-graph-projector", log_dir=log_dir, level=settings.log_level)


def _fail(exc: ProjectionError) -> int:
    print(json.dumps({"status": "FAILED", "error": exc.as_dict()}, indent=2, ensure_ascii=False, default=str),
          file=sys.stderr)
    return 1


def _cmd_ddl(args: argparse.Namespace) -> int:
    from vkm_corpus.graph import schema as S

    if args.print:
        sys.stdout.write(S.ddl_script())
        return 0
    from vkm_corpus.graph import client
    from vkm_corpus.graph.loader import apply_ddl

    settings = load_settings()
    try:
        driver = client.connect(settings)
    except ProjectionError as exc:
        return _fail(exc)
    try:
        _print(apply_ddl(driver, settings.neo4j_database, S.Namespace()))
    finally:
        driver.close()
    return 0


def _cmd_rebuild(args: argparse.Namespace) -> int:
    from vkm_corpus.graph.loader import RebuildOptions, rebuild

    settings = load_settings()
    _configure_logs(settings)
    options = RebuildOptions(mode=args.mode, snapshot_id=args.snapshot, batch_size=args.batch_size,
                             plan_only=args.plan_only, hash_files=not args.skip_file_hash,
                             sample_per_label=args.sample, command=" ".join(["graph", "rebuild", *sys.argv[3:]]))
    try:
        receipt = rebuild(settings, options)
    except ProjectionError as exc:
        return _fail(exc)
    keys = ("status", "run_id", "receipt_ref", "expected_counts", "checks_summary", "timings_s")
    _print({k: receipt.get(k) for k in keys if k in receipt}
           | {"content_digest": (receipt.get("content_digest") or {}).get("digest")})
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    from vkm_corpus.graph.common import failures
    from vkm_corpus.graph.loader import verify_current

    settings = load_settings()
    try:
        results = verify_current(settings, sample_per_label=args.sample)
    except ProjectionError as exc:
        return _fail(exc)
    _print([r.as_dict() for r in results])
    return 1 if failures(results) else 0


def _cmd_status(args: argparse.Namespace) -> int:
    from vkm_corpus.graph import client, runs

    settings = load_settings()
    try:
        driver = client.connect(settings)
    except ProjectionError as exc:
        return _fail(exc)
    try:
        _print({"server": client.server_info(driver, settings.neo4j_database),
                "state": runs.graph_state(driver, settings.neo4j_database),
                "runs": runs.runs(driver, settings.neo4j_database, limit=args.limit)})
    finally:
        driver.close()
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("graph", help="Neo4j DOCUMENT graph projection (agent E)")
    sub = parser.add_subparsers(dest="graph_cmd", metavar="<command>")

    ddl = sub.add_parser("ddl", help="apply the idempotent DDL (constraints, indexes) and confirm it")
    ddl.add_argument("--print", action="store_true", help="print the DDL script instead of applying it")
    ddl.set_defaults(func=_cmd_ddl)

    rb = sub.add_parser("rebuild", help="rebuild the DOCUMENT layer from the CURRENT canonical snapshot")
    rb.add_argument("--mode", choices=["wipe"], default="wipe", help="v0: only wipe (H-43)")
    rb.add_argument("--snapshot", help="snapshot id (default: canonical/CURRENT)")
    rb.add_argument("--batch-size", type=int, default=5000, help="UNWIND batch size (1000-20000)")
    rb.add_argument("--plan-only", action="store_true", help="preflight and expected counts only; no writes")
    rb.add_argument("--skip-file-hash", action="store_true", help="check manifest file sizes but not sha256")
    rb.add_argument("--sample", type=int, default=200, help="nodes per label in the trace check C10")
    rb.set_defaults(func=_cmd_rebuild)

    vf = sub.add_parser("verify", help="checks C1-C16 and digests of the current graph, without writing")
    vf.add_argument("--sample", type=int, default=200)
    vf.set_defaults(func=_cmd_verify)

    st = sub.add_parser("status", help="server version, readiness (503 gate) and recent ProjectionRuns")
    st.add_argument("--limit", type=int, default=10)
    st.set_defaults(func=_cmd_status)

"""``vkm-corpus core …`` — publish staging to CORE, reconcile on CORE, back up CORE to the workstation archive."""
from __future__ import annotations

import argparse
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path

from vkm_corpus.config import ConfigError, load_settings


def _run_id() -> str:
    return f"RUN-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def _publish(args: argparse.Namespace) -> int:
    from vkm_corpus.publish.transfer import publish

    settings = load_settings()
    staging = Path(args.staging) if args.staging else settings.require_data_root()
    if not args.target:
        raise ConfigError("--target host:/path of the CANONICAL root is required")
    _print(publish(staging, args.target, dry_run=args.dry_run).to_dict())
    return 0


def _reconcile(args: argparse.Namespace) -> int:
    from vkm_corpus.publish.reconcile import reconcile

    receipt = reconcile(load_settings(), run_id=args.run_id or _run_id(), graph=not args.no_graph,
                        search=not args.no_search, smoke=not args.no_smoke)
    _print(receipt)
    return 0 if receipt.get("result") == "RECONCILED" else 1


def _backup(args: argparse.Namespace) -> int:
    from vkm_corpus.publish.transfer import backup, sha_manifest

    archive = Path(args.archive)
    receipt = backup(args.source, archive, dry_run=args.dry_run).to_dict()
    if not args.dry_run and not args.no_manifest:
        manifest = sha_manifest(archive)
        (archive / "SHA256_MANIFEST.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n",
                                                      encoding="utf-8", newline="\n")
        receipt["manifest"] = {"files": manifest["files"], "digest": manifest["digest"]}
    _print(receipt)
    return 0


def register(sub) -> None:
    parser = sub.add_parser("core", help="publish staging to CORE, reconcile projections, back up CORE")
    core = parser.add_subparsers(dest="core_cmd", metavar="<cmd>")
    p = core.add_parser("publish", help="rsync a STAGING root to the CANONICAL root (immutable files, markers last)")
    p.add_argument("--staging", help="STAGING root (default: $VKM_DATA_ROOT)")
    p.add_argument("--target", required=True, help="host:/path of the CANONICAL root")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=_publish)
    r = core.add_parser("reconcile", help="on CORE: admit → snapshot → DuckDB → Neo4j → OpenSearch")
    r.add_argument("--run-id")
    r.add_argument("--no-graph", action="store_true")
    r.add_argument("--no-search", action="store_true")
    r.add_argument("--no-smoke", action="store_true")
    r.set_defaults(func=_reconcile)
    b = core.add_parser("backup", help="reverse copy of canonical/ and artifacts/ from CORE into an archive dir")
    b.add_argument("--source", required=True, help="host:/path of the CANONICAL root")
    b.add_argument("--archive", required=True, help="local archive directory")
    b.add_argument("--dry-run", action="store_true")
    b.add_argument("--no-manifest", action="store_true")
    b.set_defaults(func=_backup)

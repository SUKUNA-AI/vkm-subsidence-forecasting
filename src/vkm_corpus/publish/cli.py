"""``vkm-corpus core …`` — publish staging to CORE, reconcile on CORE, back up CORE to the workstation archive."""
from __future__ import annotations

import argparse
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path

from vkm_corpus.config import ConfigError, load_settings


def _pinned_json(path, digest):
    from vkm_corpus.coverage.publication import _json
    from vkm_corpus.coverage.accounting import _path
    from vkm_corpus.parquet.atomic import sha256_of
    p = Path(path).absolute()
    _path(p.parent, p.name)
    if not p.is_file() or sha256_of(p) != digest:
        raise ConfigError("PUBLICATION_OPERATOR_INPUT_CHANGED")
    return _json(p)


def _approval(args):
    from vkm_corpus.coverage.publication import PublicationApproval
    values = [getattr(args, k, None) for k in ("approval", "approval_sha256", "policy")]
    if not any(values):
        return None
    if not all(values):
        raise ConfigError("--approval, --approval-sha256 and --policy are required together")
    return PublicationApproval.model_validate(_pinned_json(args.approval, args.approval_sha256))


def _publication_call(fn):
    def run(args):
        try:
            return fn(args)
        except (OSError, ValueError, PermissionError, KeyError, RuntimeError):
            _print({"status": "BLOCKED", "reason": "PUBLICATION_INPUT_OR_OPERATION_NOT_VERIFIED"})
            return 1
    return run


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
    _print(publish(staging, args.target, dry_run=args.dry_run,
                   publication_approval=_approval(args), policy_path=args.policy).to_dict())
    return 0


def _reconcile(args: argparse.Namespace) -> int:
    from vkm_corpus.publish.reconcile import reconcile

    receipt = reconcile(load_settings(), run_id=args.run_id or _run_id(), graph=not args.no_graph,
                        search=not args.no_search, smoke=not args.no_smoke,
                        publication_approval=_approval(args), policy_path=args.policy)
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
    for parser_ in (p,):
        _approval_args(parser_)
    p.set_defaults(func=_publication_call(_publish))
    r = core.add_parser("reconcile", help="on CORE: admit → snapshot → DuckDB → Neo4j → OpenSearch")
    r.add_argument("--run-id")
    r.add_argument("--no-graph", action="store_true")
    r.add_argument("--no-search", action="store_true")
    r.add_argument("--no-smoke", action="store_true")
    _approval_args(r)
    r.set_defaults(func=_publication_call(_reconcile))
    b = core.add_parser("backup", help="reverse copy of canonical/ and artifacts/ from CORE into an archive dir")
    b.add_argument("--source", required=True, help="host:/path of the CANONICAL root")
    b.add_argument("--archive", required=True, help="local archive directory")
    b.add_argument("--dry-run", action="store_true")
    b.add_argument("--no-manifest", action="store_true")
    b.set_defaults(func=_backup)
    f = core.add_parser("publication-freeze", help="freeze a pinned accounting request; no transfer/admission")
    f.add_argument("--staging", required=True)
    f.add_argument("--request", required=True)
    f.add_argument("--request-sha256", required=True)
    f.add_argument("--context", required=True, help="operator-owned AccessContext JSON")
    f.add_argument("--context-sha256", required=True)
    f.add_argument("--policy", required=True)
    f.set_defaults(func=_publication_call(_freeze))
    v = core.add_parser("accounting-verify", help="verify CURRENT accounting closure after receiving or restore")
    v.add_argument("--root", required=True)
    _approval_args(v)
    v.set_defaults(func=_publication_call(_verify_accounting))


def _approval_args(parser):
    parser.add_argument("--approval", help="operator-owned PublicationApproval JSON; incoming descriptor is not approval")
    parser.add_argument("--approval-sha256", help="pin of the operator approval file")
    parser.add_argument("--policy", help="current operator source-policy inventory")


def _freeze(args):
    from vkm_corpus.coverage.publication import PublicationRequest, freeze_publication
    from vkm_corpus.contracts.access import AccessContext
    request = PublicationRequest.model_validate(_pinned_json(args.request, args.request_sha256))
    context = AccessContext.model_validate(_pinned_json(args.context, args.context_sha256))
    ref = freeze_publication(Path(args.staging), request, policy_path=args.policy, context=context)
    _print({"status": "FROZEN", "descriptor": ref.model_dump(), "publication": "NOT_RUN"})
    return 0


def _verify_accounting(args):
    from vkm_corpus.coverage.publication import snapshot_accounting
    from vkm_corpus.parquet.layout import open_root
    from vkm_corpus.parquet.reader import load_manifest
    approval = _approval(args)
    try:
        layout = open_root(Path(args.root), "CANONICAL")
        result = snapshot_accounting(layout.root, load_manifest(layout), approval=approval, policy_path=args.policy)
    except (OSError, ValueError, PermissionError, KeyError):
        result = {"status": "BLOCKED", "reason": "ACCOUNTING_CLOSURE_NOT_VERIFIED"}
    _print(result)
    return 0 if result["status"] == "VERIFIED" else 1

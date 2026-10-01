"""Runtime manifests → read-only plan/status or explicitly confirmed bounded execution."""
import json
from pathlib import Path

from vkm_corpus.update.contracts import CampaignManifest
from vkm_corpus.update.runtime import RuntimeConfig, UpdateRuntime, read_bound, nav_compatibility, late_pack_compatibility


def command(args):
    try:
        runtime = UpdateRuntime(RuntimeConfig.model_validate_json(Path(args.config).read_bytes()))
        if args.update_command == "compatibility":
            document_ref = runtime.config.artifacts[args.document_artifact]
            document = json.loads(read_bound(document_ref).read_bytes())
            nav = nav_compatibility(read_bound(runtime.config.artifacts[args.nav_artifact]),
                canonical_snapshot=document["snapshot_id"], canonical_manifest_sha256=document_ref.sha256,
                packed_path=read_bound(runtime.config.artifacts[args.packed_artifact]) if args.packed_artifact else None,
                required_datasets=tuple(args.required_dataset))
            result = {"status": nav["status"], "nav": nav}
            if args.late_artifact:
                if not args.encoder_artifact:
                    raise ValueError("query encoder artifact required")
                late = late_pack_compatibility(read_bound(runtime.config.artifacts[args.late_artifact]),
                    canonical_snapshot=document["snapshot_id"],
                    encoder=json.loads(read_bound(runtime.config.artifacts[args.encoder_artifact]).read_bytes()))
                result["late"] = late
                if late["status"] != "PASS":
                    result["status"] = "BLOCKED"
        elif args.update_command == "rollback":
            result = runtime.rollback()
        else:
            campaign = CampaignManifest.model_validate_json(Path(args.campaign).read_bytes())
            if args.update_command in {"plan", "dry-run"}:
                result = runtime.plan(campaign)
            elif args.update_command == "status":
                result = runtime.status(campaign)
            else:
                result = runtime.execute(campaign, args.confirm_plan, allow_gpu=args.allow_gpu)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0 if result.get("status", result.get("campaign", {}).get("status")) in {"READY", "PASS", "NOT_RUN"} else 2
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError) as exc:
        # Validation messages may embed paths or source values; don't copy them to delivery logs.
        print(json.dumps({"status": "BLOCKED", "reason": type(exc).__name__}))
        return 2


def register(subparsers):
    parser = subparsers.add_parser("update", help="qualified campaign plan/dry-run/execute/resume/status/rollback")
    sub = parser.add_subparsers(dest="update_command", required=True)
    for name in ("plan", "dry-run", "execute", "resume", "status", "rollback"):
        command_parser = sub.add_parser(name)
        command_parser.add_argument("--config", required=True, help="local runtime JSON; do not commit machine paths")
        if name != "rollback":
            command_parser.add_argument("--campaign", required=True)
        if name in {"execute", "resume"}:
            command_parser.add_argument("--confirm-plan", required=True)
            command_parser.add_argument("--allow-gpu", action="store_true",
                                        help="necessary but insufficient: GPU adapter and qualification also required")
        command_parser.set_defaults(func=command)
    compatibility = sub.add_parser("compatibility", help="read-only NAV/late-pack byte and origin rehearsal")
    compatibility.add_argument("--config", required=True)
    compatibility.add_argument("--document-artifact", required=True)
    compatibility.add_argument("--nav-artifact", required=True)
    compatibility.add_argument("--packed-artifact")
    compatibility.add_argument("--late-artifact")
    compatibility.add_argument("--encoder-artifact")
    compatibility.add_argument("--required-dataset", action="append", default=[])
    compatibility.set_defaults(func=command)

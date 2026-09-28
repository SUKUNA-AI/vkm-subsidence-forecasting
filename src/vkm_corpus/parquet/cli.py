"""``vkm-corpus canon`` — data-root marker, admission, snapshots, validation, status, GC, schema export."""
from __future__ import annotations

import json
from typing import Any

from vkm_corpus.config import load_settings


def _layout(kind: str | None = None):
    from vkm_corpus.parquet.layout import CanonLayout

    layout = CanonLayout(load_settings().require_data_root())
    if kind:
        layout.require(kind)
    return layout


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True, default=str))


def _summary(report: dict[str, Any]) -> dict[str, Any]:
    return {"status": report["status"], "blocking_failures": report["blocking_failures"],
            "warnings": report["warnings"], "counts": report.get("counts"),
            "not_pass": [{k: c[k] for k in ("check_id", "status", "violations", "examples")}
                         for c in report["checks"] if c["status"] != "PASS"]}


def cmd_init(args) -> int:
    from vkm_corpus.parquet.layout import init_root

    layout = init_root(load_settings().require_data_root(), args.kind)
    _print({"root_kind": layout.kind().value})
    return 0


def cmd_status(args) -> int:
    from vkm_corpus.parquet.admit import load_admissions
    from vkm_corpus.parquet.commits import list_markers
    from vkm_corpus.parquet.reader import current_snapshot_id
    from vkm_corpus.parquet.runs import list_runs

    layout = _layout()
    markers = list_markers(layout)
    runs = list_runs(layout)
    adm = load_admissions(layout)
    leases = sorted(p.stem for p in (layout.canonical / "_leases").glob("*.lease")) \
        if (layout.canonical / "_leases").is_dir() else []
    _print({"root_kind": layout.kind().value, "current": current_snapshot_id(layout), "commits": len(markers),
            "commits_pending_admission": sorted(m["commit_id"] for m in markers if m["commit_id"] not in adm),
            "commits_rejected": sorted(c for c, r in adm.items() if r["status"] == "REJECTED"),
            "runs": len(runs), "runs_without_end": sorted(r for r, i in runs.items() if i.crashed),
            "leases": leases})
    return 0


def cmd_admit(args) -> int:
    from vkm_corpus.parquet.admit import admit

    res = admit(_layout("CANONICAL"))
    _print(res)
    return 1 if res["rejected"] else 0


def _options(args):
    from vkm_corpus.parquet.validator import ValidationOptions

    return ValidationOptions(deep=args.deep, acceptance=args.acceptance, expected_sources=args.expected_sources)


def cmd_snapshot(args) -> int:
    from vkm_corpus.parquet.snapshot import build_snapshot

    res = build_snapshot(_layout("CANONICAL"), options=_options(args))
    _print({"snapshot_id": res["snapshot_id"], "current_moved": res["current_moved"],
            "manifest_path": res["manifest_path"], **_summary(res["report"])})
    return 0 if res["current_moved"] else 1


def cmd_validate(args) -> int:
    from vkm_corpus.parquet.reader import load_manifest
    from vkm_corpus.parquet.validator import validate

    layout = _layout("CANONICAL")
    manifest = load_manifest(layout, args.snapshot, candidate=args.candidate)
    report = validate(layout, manifest, _options(args))
    _print(_summary(report))
    return 0 if report["status"] == "PASS" else 1


def cmd_gc(args) -> int:
    from vkm_corpus.parquet.gc import collect_garbage

    _print(collect_garbage(_layout(), delete=args.delete, min_age_hours=args.min_age_hours))
    return 0


def cmd_lease_break(args) -> int:
    from vkm_corpus.parquet.commits import break_lease

    _print({"key": args.key, "released": break_lease(_layout("STAGING"), args.key)})
    return 0


def cmd_unlock(args) -> int:
    layout = _layout()
    lock = layout.canonical / layout.LOCK
    existed = lock.is_file()
    lock.unlink(missing_ok=True)
    _print({"lock_removed": existed})
    return 0


def cmd_schemas(args) -> int:
    from vkm_corpus.contracts import export

    return export.main(["--check"] if args.action == "check" else [])


def register(subparsers) -> None:
    p = subparsers.add_parser("canon", help="canonical Parquet store: init, admit, snapshot, validate, status, gc")
    sub = p.add_subparsers(dest="canon_cmd", metavar="<command>")
    s = sub.add_parser("init", help="create the data root marker (STAGING on producers, CANONICAL on CORE)")
    s.add_argument("--kind", required=True, choices=["STAGING", "CANONICAL"])
    s.set_defaults(func=cmd_init)
    sub.add_parser("status", help="root kind, CURRENT, pending/rejected commits, crashed runs, leases").set_defaults(
        func=cmd_status)
    sub.add_parser("admit", help="admit published commits (CANONICAL)").set_defaults(func=cmd_admit)
    for name, fn, hlp in (("snapshot", cmd_snapshot, "build + validate a snapshot; CURRENT moves only on PASS"),
                          ("validate", cmd_validate, "validate a snapshot manifest (default CURRENT)")):
        s = sub.add_parser(name, help=hlp)
        s.add_argument("--deep", action="store_true", help="hash every stored blob")
        s.add_argument("--acceptance", action="store_true", help="acceptance mode (F02 blocking)")
        s.add_argument("--expected-sources", type=int, default=None, help="e.g. 251")
        if name == "validate":
            s.add_argument("--snapshot", default=None)
            s.add_argument("--candidate", action="store_true")
        s.set_defaults(func=fn)
    s = sub.add_parser("gc", help="list (or --delete) orphan partitions and stale tmp files older than 24 h")
    s.add_argument("--delete", action="store_true")
    s.add_argument("--min-age-hours", type=float, default=24.0)
    s.set_defaults(func=cmd_gc)
    s = sub.add_parser("lease-break", help="remove the lease of a dead writer (STAGING)")
    s.add_argument("key")
    s.set_defaults(func=cmd_lease_break)
    sub.add_parser("unlock", help="remove the admit/snapshot lock of a dead process").set_defaults(func=cmd_unlock)
    s = sub.add_parser("schemas", help="export or check schemas/corpus/")
    s.add_argument("action", choices=["export", "check"])
    s.set_defaults(func=cmd_schemas)
    p.set_defaults(func=lambda args: (p.print_help(), 2)[1])



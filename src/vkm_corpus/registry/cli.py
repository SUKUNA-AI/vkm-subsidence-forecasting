"""``vkm-corpus registry`` — SOURCE_REGISTER + curated Work register → REGISTRY commit; file checks; Work seed."""
from __future__ import annotations

import json
from pathlib import Path

from vkm_corpus.config import load_settings

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_COVERAGE = REPO_ROOT / "evidence" / "sources" / "SOURCE_COVERAGE_MASTER.csv"


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True, default=str))


def cmd_import(args) -> int:
    from vkm_corpus.parquet.layout import CanonLayout
    from vkm_corpus.registry.importer import import_registry, work_registry_dir

    settings = load_settings()
    resources = settings.require_resources_root()
    layout = CanonLayout(settings.require_data_root()).require("STAGING")
    coverage = Path(args.coverage) if args.coverage else (DEFAULT_COVERAGE if DEFAULT_COVERAGE.is_file() else None)
    res = import_registry(layout, resources, work_registry=Path(args.work_registry) if args.work_registry else
                          work_registry_dir(resources), coverage_path=coverage, workers=args.workers,
                          cli_command="vkm-corpus registry import")
    _print(res)
    return 0


def cmd_verify(args) -> int:
    from vkm_corpus.registry.sources import REGISTER_REL, load_register, verify_files

    settings = load_settings()
    resources = settings.require_resources_root()
    rows, sha = load_register(resources / REGISTER_REL)
    cache = None
    if settings.data_root is not None:
        cache = settings.require_data_root() / "cache" / "source_sha256_cache.json"
    checks = verify_files(resources, rows, cache_path=cache, workers=args.workers)
    counts: dict[str, int] = {}
    problems = []
    for r in rows:
        c = checks[r["resource_id"]]
        counts[c.status.value] = counts.get(c.status.value, 0) + 1
        if c.status.value != "PRESENT_VERIFIED":
            problems.append({"source_id": r["resource_id"], "status": c.status.value,
                             "migration_status": r["migration_status"]})
    _print({"register_sha256": sha, "rows": len(rows), "file_status": counts, "not_verified": problems})
    return 0


def cmd_propose_works(args) -> int:
    from vkm_corpus.registry.work_seed import propose_work_registry, write_seed

    settings = load_settings()
    resources = settings.require_resources_root()
    coverage = Path(args.coverage) if args.coverage else DEFAULT_COVERAGE
    res = propose_work_registry(resources, coverage)
    receipt = write_seed(res, Path(args.out), command="vkm-corpus registry propose-works")
    _print(receipt)
    return 0


def register(subparsers) -> None:
    p = subparsers.add_parser("registry", help="SOURCE_REGISTER and the curated Work register")
    sub = p.add_subparsers(dest="registry_cmd", metavar="<command>")
    s = sub.add_parser("import", help="build the registry datasets and commit them (STAGING root)")
    s.add_argument("--work-registry", default=None, help="default: $VKM_WORK_REGISTRY_DIR or "
                   "$VKM_RESOURCES_ROOT/00_registry/work_registry")
    s.add_argument("--coverage", default=None, help="Phase-1 coverage master (default: PUBLIC evidence/sources)")
    s.add_argument("--workers", type=int, default=8)
    s.set_defaults(func=cmd_import)
    s = sub.add_parser("verify", help="check presence, size and sha256 of every registered file")
    s.add_argument("--workers", type=int, default=8)
    s.set_defaults(func=cmd_verify)
    s = sub.add_parser("propose-works", help="bootstrap WORK_REGISTER/WORK_LINKS proposals for curation")
    s.add_argument("--out", required=True, help="output directory (outside PRIVATE; curated copy goes to PRIVATE)")
    s.add_argument("--coverage", default=None)
    s.set_defaults(func=cmd_propose_works)
    p.set_defaults(func=lambda args: (p.print_help(), 2)[1])


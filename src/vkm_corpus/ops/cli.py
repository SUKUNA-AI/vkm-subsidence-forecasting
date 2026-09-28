"""``vkm-corpus ops …`` — control-plane administration (schema, status, jobs)."""
from __future__ import annotations

import argparse
import json

from vkm_corpus.config import load_settings


def _conn():
    from vkm_corpus.ops import jobs

    return jobs.connect(load_settings())


def _init(args: argparse.Namespace) -> int:
    from vkm_corpus.ops import jobs

    with _conn() as conn:
        print(json.dumps({"schema_version": jobs.init_schema(conn)}))
    return 0


def _status(args: argparse.Namespace) -> int:
    from vkm_corpus.ops import jobs

    with _conn() as conn:
        print(json.dumps(jobs.status(conn), indent=2, ensure_ascii=False, default=str))
    return 0


def _jobs(args: argparse.Namespace) -> int:
    with _conn() as conn, conn.cursor() as cur:
        where, params = ("WHERE state = %s", (args.state,)) if args.state else ("", ())
        cur.execute(f"SELECT job_id, kind, state, source_id, page_id, requested_by, plan_sha256, run_id, snapshot_id, "
                    f"updated_at FROM ops.job {where} ORDER BY job_id DESC LIMIT %s", (*params, args.limit))
        rows = cur.fetchall()
    print(json.dumps(rows, indent=2, ensure_ascii=False, default=str))
    return 0


def register(sub) -> None:
    parser = sub.add_parser("ops", help="PostgreSQL control plane: schema, status, jobs")
    ops = parser.add_subparsers(dest="ops_cmd", metavar="<cmd>")
    ops.add_parser("init", help="create or upgrade the ops schema (idempotent)").set_defaults(func=_init)
    ops.add_parser("status", help="jobs by state, workers, services, recent errors").set_defaults(func=_status)
    jobs_p = ops.add_parser("jobs", help="list recent jobs")
    jobs_p.add_argument("--state")
    jobs_p.add_argument("--limit", type=int, default=50)
    jobs_p.set_defaults(func=_jobs)

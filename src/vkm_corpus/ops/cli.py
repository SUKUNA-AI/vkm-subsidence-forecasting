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


def _request(args: argparse.Namespace) -> int:
    from vkm_corpus.ops import jobs

    options = {o: True for o in args.option or []}
    with _conn() as conn:
        job_id = jobs.request_plan(conn, args.kind, args.by, source_id=args.source, page_id=args.page,
                                   request={"options": options} if options else {})
    print(json.dumps({"job_id": job_id, "state": "PLAN_REQUESTED"}))
    return 0


def _show(args: argparse.Namespace) -> int:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM ops.job WHERE job_id = %s", (args.job_id,))
        row = cur.fetchone()
    if row is None:
        print(json.dumps({"error": f"job {args.job_id} not found"}))
        return 1
    print(json.dumps(row, indent=2, ensure_ascii=False, default=str))
    return 0


def _confirm(args: argparse.Namespace) -> int:
    from vkm_corpus.ops import jobs

    with _conn() as conn:
        row = jobs.confirm(conn, args.job_id, args.plan_sha256, args.by)
    print(json.dumps({"job_id": row["job_id"], "state": row["state"]}))
    return 0


def _cancel(args: argparse.Namespace) -> int:
    from vkm_corpus.ops import jobs

    with _conn() as conn:
        jobs.cancel(conn, args.job_id, args.by, args.note or "")
    print(json.dumps({"job_id": args.job_id, "state": "CANCELLED"}))
    return 0


def _worker(args: argparse.Namespace) -> int:
    import os

    from vkm_corpus.logs import configure
    from vkm_corpus.ops.worker import Worker, WorkerConfig, default_worker_id

    settings = load_settings()
    configure("ops-worker", log_dir=settings.require_data_root() / "logs", level=settings.log_level)
    cfg = WorkerConfig(worker_id=args.worker_id or default_worker_id(),
                       publish_target=args.target or os.environ.get("VKM_PUBLISH_TARGET") or None,
                       reconcile_cmd=args.reconcile_cmd or os.environ.get("VKM_RECONCILE_CMD") or None,
                       poll_seconds=args.poll)
    worker = Worker(settings, cfg)
    if args.once:
        worker.run_once()
        return 0
    try:
        worker.run_forever()
    except KeyboardInterrupt:
        worker.stop()
    return 0


def register(sub) -> None:
    parser = sub.add_parser("ops", help="PostgreSQL control plane: schema, status, jobs, worker")
    ops = parser.add_subparsers(dest="ops_cmd", metavar="<cmd>")
    ops.add_parser("init", help="create or upgrade the ops schema (idempotent)").set_defaults(func=_init)
    ops.add_parser("status", help="jobs by state, workers, services, recent errors").set_defaults(func=_status)
    jobs_p = ops.add_parser("jobs", help="list recent jobs")
    jobs_p.add_argument("--state")
    jobs_p.add_argument("--limit", type=int, default=50)
    jobs_p.set_defaults(func=_jobs)
    req = ops.add_parser("request", help="request a plan for a reprocessing job (what API/MCP admin do)")
    req.add_argument("--kind", required=True, choices=["REPROCESS_SOURCE", "REPROCESS_PAGE"])
    req.add_argument("--source")
    req.add_argument("--page", help="page id, e.g. VKM-SRC-044:p0012")
    req.add_argument("--option", action="append", choices=["force", "recall_model", "no_ocr"])
    req.add_argument("--by", default="operator")
    req.set_defaults(func=_request)
    show = ops.add_parser("show", help="one job with its plan")
    show.add_argument("job_id", type=int)
    show.set_defaults(func=_show)
    conf = ops.add_parser("confirm", help="confirm exactly the stored plan hash of a PLANNED job")
    conf.add_argument("job_id", type=int)
    conf.add_argument("--plan-sha256", required=True)
    conf.add_argument("--by", default="operator")
    conf.set_defaults(func=_confirm)
    canc = ops.add_parser("cancel", help="cancel a job that is not running yet")
    canc.add_argument("job_id", type=int)
    canc.add_argument("--by", default="operator")
    canc.add_argument("--note")
    canc.set_defaults(func=_cancel)
    wk = ops.add_parser("worker", help="plan-first job worker (WORKSTATION producer only)")
    wk.add_argument("--worker-id")
    wk.add_argument("--target", help="CANONICAL root host:/path (default $VKM_PUBLISH_TARGET)")
    wk.add_argument("--reconcile-cmd", help="command that runs reconcile on CORE, with {run_id} "
                    "(default $VKM_RECONCILE_CMD)")
    wk.add_argument("--poll", type=float, default=10.0)
    wk.add_argument("--once", action="store_true", help="take at most one job and exit")
    wk.set_defaults(func=_worker)

"""``vkm-corpus run``: the document pipeline.

    run plan     [--source ...] [--canary] [--page A-B] [--force] [--failed-only] [--recall-model]
    run extract  [--source ...] [--canary] [--page A-B] [--resume [RUN]] [--force] [--failed-only] [--plan-only]
                 [--max-model-calls N] [--recall-model --confirm-plan SHA] [--no-ocr] [--concurrency N] [--workers N]
    run render-docx --source VKM-SRC-023      (on the Docker host: pinned LibreOffice render → cached artifact)
    run import-layer --ocr-root DIR --run-tag TAG --manifest FILE [--source ...] [--dry-run]
                 (stage IMPORTED_LAYER: OCR v2 pages chosen NEW by CHOICE_V1 → OCR_RAW records + stage cache; no
                 model call, no commit — the next ``run extract`` of those sources assembles them as primary layer)
    run status   [--run RUN]

One meaning of ``--force``: rebuild rows (prepare, visual summary, commit) and use the model caches. A model is called
again for a cached call signature only with ``--recall-model``, and only through a confirmed plan
(``run plan --recall-model`` prints ``plan_sha256``; ``run extract --recall-model --confirm-plan <sha>``).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

# CP-23 canary: 7 sources of agent C + 023, 025, 064, 033, 042 + 005, 031, 202 (H-27)
CANARY = ["VKM-SRC-197", "VKM-SRC-044", "VKM-SRC-052", "VKM-SRC-045", "VKM-SRC-014", "VKM-SRC-037", "VKM-SRC-249",
          "VKM-SRC-023", "VKM-SRC-025", "VKM-SRC-064", "VKM-SRC-033", "VKM-SRC-042", "VKM-SRC-005", "VKM-SRC-031",
          "VKM-SRC-202"]


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--source", action="append", default=[], help="source id(s), repeatable or comma-separated")
    p.add_argument("--canary", action="store_true", help="the CP-23 canary set")
    p.add_argument("--page", default=None, help="page range A-B for layout/OCR (with a single --source)")
    p.add_argument("--force", action="store_true", help="rebuild rows; model caches are used")
    p.add_argument("--failed-only", action="store_true", help="only sources whose head is not COMPLETE")
    p.add_argument("--recall-model", action="store_true", help="call the model again for cached signatures")
    p.add_argument("--max-model-calls", type=int, default=None)
    p.add_argument("--workers", type=int, default=None)
    p.add_argument("--concurrency", type=int, default=None, help="OCR requests in flight")
    p.add_argument("--no-ocr", action="store_true", help="skip the OCR phase (pages stay OCR_REQUIRED)")
    p.add_argument("--no-scenario-b", action="store_true")
    p.add_argument("--scan-dpi-max", type=int, default=None, help="upper dpi of scan crops for OCR")
    p.add_argument("--profile", choices=("exploratory", "production"), default="exploratory")
    p.add_argument("--expected-commit", default=None, help="exact full production checkout commit")
    p.add_argument("--dependency-lock", action="append", default=[], help="additional checkout-relative exact lock")
    p.add_argument("--memory-budget-gb", type=float, default=None, help="aggregate worker reservation, GiB")
    p.add_argument("--memory-reserve-gb", type=float, default=None, help="coordinator reservation, GiB")
    p.add_argument("--source-memory-gb", type=float, default=None, help="each worker RLIMIT_AS, GiB")
    p.add_argument("--min-free-disk-gb", type=float, default=None, help="minimum staging free space, GiB")


def register(subparsers: Any) -> None:
    run = subparsers.add_parser("run", help="document pipeline (plan, extract, render-docx, status)")
    sub = run.add_subparsers(dest="run_cmd", metavar="<command>")
    p_plan = sub.add_parser("plan", help="plan only: what would run and how many model calls")
    _common(p_plan)
    p_plan.set_defaults(func=cmd_plan)
    p_ext = sub.add_parser("extract", help="run the pipeline over the selection")
    _common(p_ext)
    p_ext.add_argument("--plan-only", action="store_true")
    p_ext.add_argument("--resume", nargs="?", const="LATEST", default=None, help="resume a crashed run (id or LATEST)")
    p_ext.add_argument("--confirm-plan", default=None, help="plan_sha256 confirmed for --recall-model")
    p_ext.set_defaults(func=cmd_extract)
    p_docx = sub.add_parser("render-docx", help="pinned LibreOffice render of DOCX sources (Docker host)")
    p_docx.add_argument("--source", action="append", default=[])
    p_docx.add_argument("--image", default=None)
    p_docx.set_defaults(func=cmd_render_docx)
    p_imp = sub.add_parser("import-layer", help="import an external OCR layer (OCR v2, CHOICE_V1 NEW pages)")
    p_imp.add_argument("--ocr-root", required=True, help="OCR v2 run root (img/, runs/<tag>/, pp/<tag>/, choice/)")
    p_imp.add_argument("--run-tag", required=True, help="run tag of the OCR v2 run, e.g. full_v2")
    p_imp.add_argument("--manifest", required=True, help="page manifest of the OCR v2 kit (source_sha256, page size)")
    p_imp.add_argument("--source", action="append", default=[], help="source id(s); default: all of the run")
    p_imp.add_argument("--dry-run", action="store_true", help="check and count only; nothing is stored")
    p_imp.set_defaults(func=cmd_import_layer)
    p_st = sub.add_parser("status", help="summary of a run (default: the latest)")
    p_st.add_argument("--run", default=None)
    p_st.set_defaults(func=cmd_status)
    run.set_defaults(func=lambda a: (run.print_help(), 2)[1])


# ---------------------------------------------------------------------------------------------------- helpers
def _ids(args: argparse.Namespace) -> list[str]:
    out = []
    for x in args.source or []:
        out += [y.strip() for y in x.split(",") if y.strip()]
    if getattr(args, "canary", False):
        out += CANARY
    return list(dict.fromkeys(out))


def _cfg(args: argparse.Namespace):
    from vkm_corpus.pipeline.config import load_pipeline_config

    cfg = load_pipeline_config(workers=args.workers, ocr_concurrency=args.concurrency,
                               max_model_calls=args.max_model_calls,
                               **{key: getattr(args, key, None) for key in (
                                   "profile", "expected_commit", "memory_budget_gb", "memory_reserve_gb",
                                   "source_memory_gb", "min_free_disk_gb")},
                               dependency_locks=tuple(getattr(args, "dependency_lock", [])))
    if getattr(args, "no_scenario_b", False):
        cfg.scenario_b.enabled = False
    if getattr(args, "scan_dpi_max", None):
        cfg.ocr_crop.scan_dpi_max = args.scan_dpi_max
    return cfg


def _logger(cfg: Any, run_id: str | None = None):
    import logging

    from vkm_corpus.logs import JsonFormatter, configure, get_logger

    configure("pipeline", log_dir=Path(cfg.data_root) / "logs")
    log = get_logger("pipeline")
    path = None
    if run_id:
        path = Path(cfg.data_root) / "logs" / "runs" / f"{run_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        h = logging.FileHandler(path, encoding="utf-8")
        h.setFormatter(JsonFormatter("pipeline"))
        logging.getLogger("vkm").addHandler(h)
    return log, path


def _flags(args: argparse.Namespace) -> list[str]:
    out = []
    for name in ("resume", "force", "failed_only", "plan_only", "recall_model", "no_ocr", "canary", "no_scenario_b"):
        if getattr(args, name, None):
            out.append("--" + name.replace("_", "-"))
    if getattr(args, "source", None):
        out.append("--source")
    if getattr(args, "page", None):
        out.append("--page")
    if getattr(args, "max_model_calls", None) is not None:
        out.append("--max-model-calls")
    return out


# ---------------------------------------------------------------------------------------------------- commands
def cmd_plan(args: argparse.Namespace) -> int:
    args.plan_only = True
    args.resume = None
    args.confirm_plan = None
    return cmd_extract(args)


def cmd_extract(args: argparse.Namespace) -> int:
    from vkm_corpus.parquet.blobs import put_blob
    from vkm_corpus.pipeline import commit as cm
    from vkm_corpus.pipeline.context import (open_cache, open_store, write_json_atomic, producer_identity,
                                             resource_guard, ProducerGuardError)
    from vkm_corpus.pipeline.plan import build_plan
    from vkm_corpus.pipeline.runner import Orchestrator
    from vkm_corpus.pipeline.sources import load_sources, select

    cfg = _cfg(args)
    try:
        producer_identity(cfg)
        resource_guard(cfg)
    except ProducerGuardError as exc:
        print(f"producer preflight refused: {exc}", file=sys.stderr)
        return 4
    ids_ = _ids(args)
    sources = select(load_sources(cfg.resources_root), ids_ or None)
    if args.page and len(sources) != 1:
        print("--page needs exactly one --source", file=sys.stderr)
        return 2
    log, _ = _logger(cfg)
    from vkm_corpus.pipeline.imported_layer import imported_run_models

    orch = Orchestrator(cfg, argv=["vkm-corpus", "run", "extract", *sys.argv[3:]], flags=_flags(args), log=log,
                        run_kind="PLAN" if args.plan_only else "EXTRACTION", plan_only=bool(args.plan_only),
                        extra_models=imported_run_models(cfg, [s.source_id for s in sources]))
    log, log_path = _logger(cfg, orch.run_id)
    orch.log = log
    t0 = time.time()
    status = "SUCCEEDED"
    report: dict[str, Any] = {"run_id": orch.run_id, "selection": [s.source_id for s in sources]}
    try:
        cache = open_cache(cfg, orch.run_id, "plan")
        store = open_store(cfg, orch.run_id, "plan")
        plan = build_plan(cfg, sources, cache, store, force=args.force, failed_only=args.failed_only,
                          recall=args.recall_model, page_range=args.page, no_ocr=args.no_ocr)
        store.close()
        cache.close()
        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        prow = put_blob(orch.layout, "RUN_PLAN", json.dumps(plan, ensure_ascii=False, sort_keys=True).encode("utf-8"),
                        "application/json", run_id=orch.run_id, created_at=now)
        orch.recorder.add_artifacts([prow])
        report["plan"] = {"plan_sha256": plan["plan_sha256"], "totals": plan["totals"],
                          "plan_artifact_id": prow["artifact_id"]}
        if args.plan_only:
            write_json_atomic(orch.dir / "plan.json", plan)
            print(json.dumps(report["plan"] | {"run_id": orch.run_id,
                                              "sources": [{k: v for k, v in e.items() if k in (
                                                  "source_id", "action", "reasons", "prepare", "layout_pending_pages",
                                                  "ocr_tasks_known", "ocr_cached_known", "ocr_calls_known",
                                                  "imported_layer", "commit")} for e in plan["sources"]]},
                             ensure_ascii=False, indent=1))
            return 0
        if (args.recall_model or cfg.profile == "production") and args.confirm_plan != plan["plan_sha256"]:
            print("refused: production or --recall-model needs --confirm-plan with the sha256 of this exact plan "
                  f"(current plan_sha256={plan['plan_sha256']})", file=sys.stderr)
            status = "ABORTED"
            return 4
        if cfg.profile == "production":
            write_json_atomic(orch.dir / "approved_plan.json", plan)
        skipped = [s for s in sources if s.lifecycle != "ACTIVE"]
        orch.record_skips(skipped)
        todo_ids = {e["source_id"] for e in plan["sources"] if e["action"] == "PROCESS"}
        todo = [s for s in sources if s.source_id in todo_ids]
        report["skipped_by_register"] = [s.source_id for s in skipped]
        report["up_to_date"] = [e["source_id"] for e in plan["sources"] if e["action"] == "UP_TO_DATE"]
        if args.resume:
            report["resume"] = _resume(orch, args.resume)
        if todo:
            t = time.time()
            orch.prepare(todo, force=args.force)
            report["t_prepare_s"] = round(time.time() - t, 1)
            t = time.time()
            orch.visual(todo, page_range=args.page, max_layout_calls=args.max_model_calls)
            report["t_visual_s"] = round(time.time() - t, 1)
            if not args.no_ocr:
                t = time.time()
                left = None if args.max_model_calls is None else max(0, args.max_model_calls -
                                                                        orch.counters["layout_calls"])
                report["ocr"] = orch.ocr(todo, max_calls=left, recall=args.recall_model, page_range=args.page)
                report["t_ocr_s"] = round(time.time() - t, 1)
            t = time.time()
            orch.commit(todo)
            report["t_commit_s"] = round(time.time() - t, 1)
        report["commits"] = orch.results
        report["crashes"] = orch.crashed
        report["counters"] = orch.counters
        if orch.crashed or any(r.get("status") not in ("COMPLETE",) for r in orch.results.values()):
            status = "PARTIAL"
        stops = [p.get("stopped") for p in (report.get("ocr") or {}).get("passes", []) if p.get("stopped")]
        if stops:
            status = "PARTIAL"
            orch.crashed.append({"source_id": None, "phase": "ocr", "code": "OCR_QUALITY_STOP",
                                 "message": stops[0][:400]})
        return int(cfg.profile == "production" and status != "SUCCEEDED")
    except Exception as exc:  # noqa: BLE001 - the run is closed as FAILED with its log
        status = "FAILED"
        log.exception("run failed")
        report["error"] = f"{type(exc).__name__}: {exc}"
        return 1
    finally:
        report["wall_s"] = round(time.time() - t0, 1)
        try:
            orch.record_crashes()
        except Exception:  # noqa: BLE001
            log.exception("could not record crashes")
        write_json_atomic(orch.dir / "report.json", report)
        orch.finish(status, log_path)
        summary = {k: report.get(k) for k in ("run_id", "wall_s", "t_prepare_s", "t_visual_s", "t_ocr_s", "t_commit_s",
                                              "counters", "error")}
        summary["status"] = status
        summary["commits"] = {sid: {k: r.get(k) for k in ("status", "commit_id", "noop", "page_count", "n_errors")}
                              for sid, r in (report.get("commits") or {}).items()}
        print(json.dumps(summary, ensure_ascii=False, indent=1))


def _resume(orch: Any, which: str) -> dict[str, Any]:
    """Sources a crashed run started but did not commit → WORKER_CRASHED in this run's journal (H-11)."""
    from vkm_corpus.parquet.runs import list_runs

    runs = list_runs(orch.layout)
    crashed = [r for r in runs.values() if r.crashed and r.run_id != orch.run_id]
    target = None
    if which == "LATEST":
        target = max(crashed, key=lambda r: r.run_id) if crashed else None
    else:
        target = runs.get(which)
    if target is None:
        return {"resumed_run": None}
    prev_dir = Path(orch.cfg.data_root) / "tmp" / f"run={target.run_id}"
    started = set()
    for phase in ("prepare", "visual", "commit"):
        d = prev_dir / phase
        if d.is_dir():
            started |= {p.stem.split(".")[0] for p in d.iterdir()}
    committed = set()
    cd = prev_dir / "commit"
    if cd.is_dir():
        for p in cd.glob("*.json"):
            try:
                if json.loads(p.read_text(encoding="utf-8")).get("commit_id"):
                    committed.add(p.stem)
            except json.JSONDecodeError:
                pass
    lost = sorted(started - committed)
    for sid in lost:
        orch.crashed.append({"source_id": sid, "phase": "commit", "code": "WORKER_CRASHED",
                             "message": f"run {target.run_id} ended without END and without a commit of this source"})
    return {"resumed_run": target.run_id, "sources_without_commit": lost}


def cmd_render_docx(args: argparse.Namespace) -> int:
    """Render DOCX sources with the pinned LibreOffice container and cache the PDF (no heavy imports)."""
    from vkm_corpus.config import load_settings
    from vkm_corpus.extract.docx_render import DEFAULT_IMAGE, render_docx
    from vkm_corpus.pipeline.cache import StageCache
    from vkm_corpus.pipeline.config import PipelineConfig
    from vkm_corpus.pipeline.context import open_store
    from vkm_corpus.pipeline.prepare import docx_render_signature
    from vkm_corpus.pipeline.sources import load_sources, select

    s = load_settings()
    cfg = PipelineConfig(data_root=s.require_data_root(), resources_root=s.require_resources_root())
    image = args.image or DEFAULT_IMAGE
    cfg.docx_render_image = image
    run_tag = "RUN-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-d0c0d0c0"
    store = open_store(cfg, run_tag, "render-docx")
    cache = StageCache(Path(cfg.data_root), run_tag, writer_name="render-docx")
    out = []
    try:
        for src in select(load_sources(cfg.resources_root), args.source or None):
            if not src.canonical_path.lower().endswith(".docx"):
                continue
            sig = docx_render_signature(src, image)
            if cache.get_stage(sig):
                out.append({"source_id": src.source_id, "status": "CACHED"})
                continue
            res = render_docx(Path(cfg.resources_root) / src.canonical_path, image=image)
            rec = store.put_bytes(res.pdf_bytes, "DOCX_RENDERED_PDF", "application/pdf", source_id=src.source_id,
                                  recipe={"renderer": "libreoffice", "renderer_version":
                                          res.profile.get("libreoffice_version"), "profile": res.profile_string,
                                          "source_sha256": src.sha256, "image_id": res.profile.get("image_id")})
            cache.add_stage(stage_signature=sig, stage="DOCX_RENDER", source_id=src.source_id, page_index=None,
                            outputs={"pdf_artifact_id": rec.artifact_id, "profile": res.profile,
                                     "profile_string": res.profile_string})
            out.append({"source_id": src.source_id, "status": "RENDERED", "pdf_artifact_id": rec.artifact_id,
                        "profile": res.profile_string})
    finally:
        store.close()
        cache.close()
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


def cmd_import_layer(args: argparse.Namespace) -> int:
    """Stage IMPORTED_LAYER (no model, no commit): see ``vkm_corpus.pipeline.imported_layer``."""
    from vkm_corpus import ids
    from vkm_corpus.pipeline.config import load_pipeline_config
    from vkm_corpus.pipeline.context import REPO_ROOT, open_cache, open_store, run_dir, write_json_atomic
    from vkm_corpus.pipeline.imported_layer import ImportRefused, OcrRunInputs, import_layer
    from vkm_corpus.pipeline.sources import load_sources, select

    cfg = load_pipeline_config()
    inputs = OcrRunInputs(ocr_root=Path(args.ocr_root).expanduser(), run_tag=args.run_tag,
                          kit_manifest=Path(args.manifest).expanduser())
    for path in (inputs.pages, inputs.choice, inputs.choice_receipt, inputs.prep_manifest, inputs.kit_manifest,
                 inputs.postprocess_receipt, inputs.prep_receipt):
        if not path.is_file():
            print(f"import-layer: missing input {path.name}", file=sys.stderr)
            return 2
    ids_ = _ids(args)
    if not ids_:
        with open(inputs.kit_manifest, encoding="utf-8") as fh:
            ids_ = sorted({json.loads(line)["source_id"] for line in fh if line.strip()})
    sources = select(load_sources(cfg.resources_root), ids_)
    run_id = ids.new_run_id()
    log, _ = _logger(cfg)
    store = open_store(cfg, run_id, "import-layer")
    cache = open_cache(cfg, run_id, "import-layer")
    try:
        receipt = import_layer(cfg, store, cache, sources, inputs, repo_root=REPO_ROOT, run_id=run_id,
                               dry_run=args.dry_run, log=log)
    except ImportRefused as exc:
        print(json.dumps({"status": "REFUSED", "code": exc.code, "detail": exc.detail}, ensure_ascii=False))
        return 3
    finally:
        store.close()
        cache.close()
    write_json_atomic(run_dir(cfg, run_id) / "import_layer.json", receipt)
    print(json.dumps({k: receipt[k] for k in ("run_id", "config_hash", "provenance_artifact_id", "totals", "wall_s",
                                              "dry_run")} | {"refused": {sid: r["refused"] for sid, r in
                                                                         receipt["sources"].items() if r["refused"]}},
                     ensure_ascii=False, indent=1))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from vkm_corpus.pipeline.config import load_pipeline_config

    cfg = load_pipeline_config()
    base = Path(cfg.data_root) / "tmp"
    runs = sorted(p.name[4:] for p in base.glob("run=*")) if base.exists() else []
    run_id = args.run or (runs[-1] if runs else None)
    if run_id is None:
        print("no runs")
        return 1
    rep = base / f"run={run_id}" / "report.json"
    print(rep.read_text(encoding="utf-8") if rep.exists() else json.dumps({"run_id": run_id, "report": None}))
    return 0

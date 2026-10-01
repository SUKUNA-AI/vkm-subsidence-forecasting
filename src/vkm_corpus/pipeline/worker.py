"""Subprocess entry of the pipeline (source isolation, H-11): ``python -m vkm_corpus.pipeline.worker <phase> ...``.

Phases: ``prepare`` (one source), ``visual`` (several sources, one GPU model load), ``commit`` (one source). Each
worker writes its result as JSON into ``<data_root>/tmp/run=<RUN>/<phase>/<SID>.json`` and exits 0; a crash, a
timeout or an out-of-memory kill leaves no result file, and the orchestrator records WORKER_CRASHED.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

from vkm_corpus.pipeline.context import (config_from_json, open_cache, open_store, run_dir, write_json_atomic,
                                         verify_approved_sources)
from vkm_corpus.pipeline.sources import load_sources, select


def _log():
    from vkm_corpus.logs import configure, get_logger

    configure("pipeline-worker")
    return get_logger("pipeline.worker")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="vkm_corpus.pipeline.worker")
    ap.add_argument("phase", choices=["prepare", "visual", "commit"])
    ap.add_argument("--config", required=True)
    ap.add_argument("--run", required=True)
    ap.add_argument("--sources", required=True, help="comma-separated source ids")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--part-base", type=int, default=0)
    ap.add_argument("--max-layout-calls", type=int, default=None)
    ap.add_argument("--pages", default=None, help="page range for visual/layout (single source)")
    a = ap.parse_args(argv)
    cfg = config_from_json(json.loads(Path(a.config).read_text(encoding="utf-8")))
    srcs = select(load_sources(cfg.resources_root), a.sources.split(","))
    if cfg.profile == "production":
        approved = Path(cfg.data_root) / "tmp" / f"run={a.run}" / "approved_plan.json"
        verify_approved_sources(cfg, srcs, json.loads(approved.read_text(encoding="utf-8")))
    out_dir = run_dir(cfg, a.run) / a.phase
    name = f"{a.phase}-{srcs[0].source_id if len(srcs) == 1 else 'multi'}-{int(time.time())}"
    store = open_store(cfg, a.run, name)
    cache = open_cache(cfg, a.run, name)
    log = _log()
    try:
        if a.phase == "prepare":
            from vkm_corpus.pipeline.prepare import prepare_source

            for s in srcs:
                t0 = time.perf_counter()
                prep = prepare_source(cfg, store, cache, s, force=a.force)
                write_json_atomic(out_dir / f"{s.source_id}.json", {
                    "source_id": s.source_id, "status": prep.get("status"), "pages": len(prep.get("pages", [])),
                    "errors": len(prep.get("errors", [])), "reused": prep.get("run_id") != a.run,
                    "t_s": round(time.perf_counter() - t0, 2)})
        elif a.phase == "visual":
            from vkm_corpus.pipeline.commit import load_state
            from vkm_corpus.pipeline.visual import run_visual

            model = None
            counters: dict[str, int] = {}
            for s in srcs:
                t0 = time.perf_counter()
                (out_dir / f"{s.source_id}.started").parent.mkdir(parents=True, exist_ok=True)
                (out_dir / f"{s.source_id}.started").write_text(str(time.time()), encoding="utf-8")
                prep, _ = load_state(cfg, s)
                if prep is None or prep.get("status") != "PREPARED":
                    write_json_atomic(out_dir / f"{s.source_id}.json", {"source_id": s.source_id,
                                                                         "status": "SKIPPED_NOT_PREPARED"})
                    continue
                needs_model = cfg.use_gpu_layout and prep["inspect"]["file_format"] in ("PDF", "DJVU")
                from vkm_corpus.pipeline.visual import load_visual, visual_signature

                cached_vis = load_visual(cfg.data_root, s.source_id, visual_signature(cfg, prep))
                if cached_vis is not None and cached_vis.get("complete"):
                    needs_model = False  # everything cached: do not touch the GPU
                if needs_model and model is None and cfg.models_root is not None:
                    from vkm_corpus.layout.ppdoclayout import LayoutModel

                    model = LayoutModel(cfg.models_root, config=cfg.layout)
                keep = None
                if a.pages:
                    lo, _, hi = a.pages.partition("-")
                    keep = set(range(int(lo), int(hi or lo) + 1))
                before = dict(counters)
                summary = run_visual(cfg, store, cache, prep, model if needs_model else None, log=log,
                                     max_layout_calls=a.max_layout_calls, counters=counters, only_pages=keep)
                write_json_atomic(out_dir / f"{s.source_id}.json", {
                    "source_id": s.source_id, "status": "COMPLETE" if summary["complete"] else "INCOMPLETE",
                    "pages": len(summary["pages"]),
                    "layout_calls": counters.get("layout_calls", 0) - before.get("layout_calls", 0),
                    "layout_cached": counters.get("layout_cached", 0) - before.get("layout_cached", 0),
                    "t_s": round(time.perf_counter() - t0, 2)})
        elif a.phase == "commit":
            from vkm_corpus.pipeline.commit import safe_commit
            from vkm_corpus.pipeline.context import code_revision

            rev, dirty = code_revision()
            for s in srcs:
                res = safe_commit(cfg, a.run, s, store=store, cache=cache, part_base=a.part_base,
                                  code_revision=rev + ("+dirty" if dirty else ""), log=log)
                write_json_atomic(out_dir / f"{s.source_id}.json", res)
    except Exception:  # noqa: BLE001 - the traceback goes to stderr; no result file = crash for the orchestrator
        traceback.print_exc()
        return 1
    finally:
        store.close()
        cache.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

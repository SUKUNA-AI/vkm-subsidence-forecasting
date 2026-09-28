"""Orchestrator of ``vkm-corpus run``: plan, prepare → visual/layout → OCR (+ scenario B) → commit.

The orchestrator owns the run record (START/END markers, RUN_LOG) and the GPU client of GLM-OCR; CPU stages and the
layout model run in subprocess workers (``pipeline.worker``) with time and memory budgets. A worker that dies leaves
no result file: the source gets a ``WORKER_CRASHED`` error in the run journal, its pages keep their rows from the
caches at the next commit, and ``--resume`` reports the sources a crashed run left without a commit (H-11).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vkm_corpus.extract.model import SourceInput
from vkm_corpus.pipeline import commit as cm
from vkm_corpus.pipeline.config import PipelineConfig
from vkm_corpus.pipeline.context import (code_revision, config_to_json, logical_argv, open_cache, open_store,
                                         run_dir, write_json_atomic)
from vkm_corpus.pipeline.sources import load_sources, select
from vkm_corpus.versions import PIPELINE_VERSION

HOST_ROLE = "WORKSTATION"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _mem_limit(gb: float):
    def fn() -> None:
        try:
            import resource

            lim = int(gb * 1024 ** 3)
            resource.setrlimit(resource.RLIMIT_AS, (lim, lim))
        except (ImportError, ValueError, OSError):
            pass
    return fn


class Orchestrator:
    def __init__(self, cfg: PipelineConfig, *, argv: list[str], flags: list[str], log: Any, run_kind: str = "EXTRACTION",
                 plan_only: bool = False, parent_run_id: str | None = None):
        from vkm_corpus.parquet.layout import init_root
        from vkm_corpus.parquet.runs import RunRecorder

        self.cfg = cfg
        self.log = log
        self.layout = init_root(cfg.data_root, "STAGING")
        rev, dirty = code_revision()
        self.code_rev = rev + ("+dirty" if dirty else "")
        models = [{"role": "LAYOUT", "model_id": "PaddlePaddle/PP-DocLayoutV3_safetensors",
                   "model_revision": "97d101e6db2642e162a1d05392d1b0231c91033e",
                   "weights_sha256": "5ea422c6cc5fe759a47e1357c35639b58173508e025a3131cbe4b6ac59e2b85e",
                   "backend": "transformers", "device": "cuda"},
                  {"role": "RECOGNITION", "model_id": cfg.model.model_id, "model_revision": cfg.model.model_revision,
                   "weights_sha256": cfg.model.weights_sha256, "backend": "vllm"}]
        self.recorder = RunRecorder(self.layout, run_kind=run_kind, cli_command=logical_argv(argv, cfg),
                                    host_role=HOST_ROLE, config=config_to_json_public(cfg),
                                    code_revision=rev, code_dirty=dirty, models=models, cli_flags=flags,
                                    extra={"plan_only": plan_only, "parent_run_id": parent_run_id})
        self.recorder.start()
        self.run_id = self.recorder.run_id
        self.dir = run_dir(cfg, self.run_id)
        self.cfg_path = self.dir / "config.json"
        write_json_atomic(self.cfg_path, config_to_json(cfg))
        self.counters: dict[str, Any] = {"model_calls": 0, "layout_calls": 0, "worker_crashes": 0}
        self.crashed: list[dict[str, Any]] = []
        self.results: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ workers
    def _worker(self, phase: str, sources: list[str], *, timeout: float, extra: list[str] | None = None,
                mem_gb: float | None = None) -> int:
        cmd = [sys.executable, "-m", "vkm_corpus.pipeline.worker", phase, "--config", str(self.cfg_path),
               "--run", self.run_id, "--sources", ",".join(sources), *(extra or [])]
        env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout, env=env,
                                  preexec_fn=_mem_limit(mem_gb) if (mem_gb and os.name == "posix") else None)
        except subprocess.TimeoutExpired:
            self.log.error("worker timeout", extra={"vkm": {"stage": phase, "status": ",".join(sources)[:200]}})
            return -9
        if proc.returncode != 0:
            tail = proc.stderr.decode("utf-8", "replace")[-2000:]
            self.log.error("worker failed", extra={"vkm": {"stage": phase, "status": f"exit {proc.returncode}",
                                                           "detail": tail}})
        return proc.returncode

    def _crash(self, src: SourceInput, phase: str, rc: int, message: str) -> None:
        self.counters["worker_crashes"] += 1
        code = "TIMEOUT" if rc == -9 else "WORKER_CRASHED"
        self.crashed.append({"source_id": src.source_id, "phase": phase, "code": code, "message": message})

    def _result(self, phase: str, sid: str) -> dict[str, Any] | None:
        p = self.dir / phase / f"{sid}.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    # ------------------------------------------------------------------ phases
    def prepare(self, sources: list[SourceInput], *, force: bool) -> None:
        def one(s: SourceInput) -> None:
            extra = ["--force"] if force else []
            rc = self._worker("prepare", [s.source_id], timeout=self.cfg.source_timeout_s, extra=extra,
                              mem_gb=self.cfg.source_memory_gb)
            if self._result("prepare", s.source_id) is None:
                # one retry in light mode (no path counting), then a recorded crash
                env_before = os.environ.get("VKM_PREP_LIGHT")
                os.environ["VKM_PREP_LIGHT"] = "1"
                try:
                    rc = self._worker("prepare", [s.source_id], timeout=self.cfg.source_timeout_s,
                                      extra=["--force"], mem_gb=self.cfg.source_memory_gb)
                finally:
                    if env_before is None:
                        os.environ.pop("VKM_PREP_LIGHT", None)
                    else:
                        os.environ["VKM_PREP_LIGHT"] = env_before
                if self._result("prepare", s.source_id) is None:
                    self._crash(s, "prepare", rc, "prepare worker produced no result")

        with ThreadPoolExecutor(max_workers=max(1, self.cfg.workers)) as ex:
            list(ex.map(one, sources))

    def visual(self, sources: list[SourceInput], *, page_range: str | None = None,
               max_layout_calls: int | None = None) -> None:
        todo = [s.source_id for s in sources]
        while todo:
            extra = []
            if page_range:
                extra += ["--pages", page_range]
            if max_layout_calls is not None:
                extra += ["--max-layout-calls", str(max(0, max_layout_calls - self.counters["layout_calls"]))]
            rc = self._worker("visual", todo, timeout=self.cfg.source_timeout_s * max(1, len(todo)), extra=extra)
            done = []
            for sid in todo:
                r = self._result("visual", sid)
                if r is not None:
                    done.append(sid)
                    self.counters["layout_calls"] += int(r.get("layout_calls") or 0)
            rest = [sid for sid in todo if sid not in done]
            if not rest:
                break
            # the first unfinished source is the one that crashed the worker: record it and continue after it
            started = [sid for sid in rest if (self.dir / "visual" / f"{sid}.started").exists()]
            culprit = started[0] if started else rest[0]
            self._crash(next(s for s in sources if s.source_id == culprit), "visual", rc,
                        "visual/layout worker produced no result")
            todo = [sid for sid in rest if sid != culprit]

    def ocr(self, sources: list[SourceInput], *, max_calls: int | None, recall: bool,
            page_range: str | None = None) -> dict[str, Any]:
        from vkm_corpus.pipeline import ocr_stage

        store = open_store(self.cfg, self.run_id, "ocr")
        cache = open_cache(self.cfg, self.run_id, "ocr")
        backend = asyncio.run(self._backend_info())
        self.backend = backend
        report: dict[str, Any] = {"backend": backend, "passes": []}
        try:
            # pass 1: required OCR + formula/table regions + scenario-B samples
            work = self._ocr_work(sources, cache, store, reocr_sources=set(), page_range=page_range)
            stats = asyncio.run(ocr_stage.run_ocr(self.cfg, store, cache, work, backend=backend, max_calls=max_calls,
                                                  recall=recall, log=self.log))
            self.counters["model_calls"] += stats.called
            report["passes"].append({"pass": "main", **ocr_stage.summarize(stats)})
            # scenario B: decisions from the samples, then re-OCR of the chosen sources within the budget
            decisions = {}
            for s in sources:
                prep, visual = cm.load_state(self.cfg, s)
                if prep is None or not cm.embedded_pages(prep) or not self.cfg.scenario_b.enabled:
                    continue
                cache.reload()
                d = cm.decide_scenario_b(self.cfg, store, cache, s, prep, visual)
                if d is not None:
                    decisions[s.source_id] = {k: v for k, v in d.items() if k != "rows"} | {"rows": d["rows"]}
            report["scenario_b"] = decisions
            reocr = {sid for sid, d in decisions.items() if d.get("decision") == "REOCR_SOURCE"}
            if reocr and self.cfg.scenario_b.reocr and not stats.stopped:
                left = None if max_calls is None else max(0, max_calls - stats.called)
                work_b = self._ocr_work([s for s in sources if s.source_id in reocr], cache, store,
                                        reocr_sources=reocr, page_range=page_range)
                stats_b = asyncio.run(ocr_stage.run_ocr(self.cfg, store, cache, work_b, backend=backend,
                                                        max_calls=left, recall=False, log=self.log,
                                                        budget_s=self.cfg.scenario_b.budget_gpu_hours * 3600))
                self.counters["model_calls"] += stats_b.called
                report["passes"].append({"pass": "scenario_b_reocr", "sources": sorted(reocr),
                                         **ocr_stage.summarize(stats_b)})
        finally:
            store.close()
            cache.close()
        return report

    async def _backend_info(self) -> dict[str, Any]:
        from vkm_corpus.ocr.client import GlmOcrClient

        if not self.cfg.ocr_url:
            return {"engine": "vllm", "available": False}
        async with GlmOcrClient(self.cfg.ocr_url, model=self.cfg.model.served_model_name) as c:
            info = await c.server_info()
        models = info.get("models") or {}
        root = None
        try:
            root = models["data"][0].get("root")
        except (KeyError, IndexError, TypeError):
            pass
        revision_ok = bool(root) and str(root).rstrip("/").endswith(self.cfg.model.model_revision)
        if not revision_ok:
            raise RuntimeError("MODEL_UNAVAILABLE: the OCR server does not serve the pinned GLM-OCR revision")
        return {"engine": "vllm", "version": (info.get("version") or {}).get("version"),
                "served_model_root_revision_ok": revision_ok, "health_status": info.get("health_status"),
                "image_digest": os.environ.get("VKM_GLM_OCR_IMAGE_DIGEST") or "unrecorded"}

    def _ocr_work(self, sources: list[SourceInput], cache: Any, store: Any, *, reocr_sources: set[str],
                  page_range: str | None) -> list[Any]:
        from vkm_corpus.pipeline import ocr_stage

        work = []
        keep = None
        if page_range:
            lo, _, hi = page_range.partition("-")
            keep = set(range(int(lo), int(hi or lo) + 1))
        for s in sources:
            prep, visual = cm.load_state(self.cfg, s)
            if prep is None or prep.get("status") != "PREPARED":
                continue
            fmt = prep["inspect"]["file_format"]
            path = str(Path(self.cfg.resources_root) / s.canonical_path)
            sample = cm.sample_for(self.cfg, prep)
            vis_rows = {r["page_index"]: r for r in (visual or {}).get("pages", [])}
            for row in prep["pages"]:
                idx = row["page_index"]
                if keep is not None and idx not in keep:
                    continue
                if fmt == "EPUB":
                    unit = store.read_json(row["native_raw_artifact_id"]) if row.get("native_raw_artifact_id") else {}
                    specs = ocr_stage.plan_epub_tasks(s.source_id, unit) if unit else []
                elif fmt in ("PDF", "DJVU"):
                    vis = vis_rows.get(idx)
                    try:
                        _, regions = ocr_stage.load_regions(store, (vis or {}).get("layout_raw_artifact_id"),
                                                            row.get("width_pt") or 0, row.get("height_pt") or 0,
                                                            self.cfg.region_thresholds)
                    except Exception:  # noqa: BLE001
                        regions = []
                    specs = ocr_stage.plan_page_tasks(self.cfg, fmt, s.source_id, row, vis, regions,
                                                      sample_b=idx in sample,
                                                      reocr=s.source_id in reocr_sources)
                    if s.source_id in reocr_sources:
                        specs = [x for x in specs if x.role == "REOCR"]
                else:
                    specs = []
                if specs:
                    work.append(ocr_stage.PageWork(s.source_id, fmt, path, idx, specs, s.sha256))
        return work

    def commit(self, sources: list[SourceInput]) -> None:
        def one(s: SourceInput) -> None:
            base = int(s.number) * 100
            rc = self._worker("commit", [s.source_id], timeout=self.cfg.source_timeout_s,
                              extra=["--part-base", str(base)], mem_gb=self.cfg.source_memory_gb)
            r = self._result("commit", s.source_id)
            if r is None:
                self._crash(s, "commit", rc, "commit worker produced no result")
                return
            self.results[s.source_id] = r
            if r.get("commit_id") and not r.get("noop"):
                self.recorder.note_commit(r["commit_id"])
            from vkm_corpus.parquet.runs import describe_file

            for rel in r.get("journal_parts", []):
                name = rel.split("/", 1)[0]
                self.recorder.files.append(describe_file(self.layout, name, rel))

        with ThreadPoolExecutor(max_workers=max(1, self.cfg.workers)) as ex:
            list(ex.map(one, sources))

    # ------------------------------------------------------------------ journal entries of the orchestrator
    def record_skips(self, skipped: list[SourceInput]) -> None:
        if not skipped:
            return
        now = _now()
        from vkm_corpus import ids

        rows = []
        for s in skipped:
            rows.append({"schema_version": "0.1.0", "step_id": ids.step_id(self.run_id, s.source_id, None, "INSPECT", 1),
                         "processing_run_id": self.run_id, "source_id": s.source_id, "page_id": None,
                         "page_index": None, "stage": "INSPECT", "attempt": 1, "outcome": "SKIPPED_BY_POLICY",
                         "status": "SKIPPED_BY_REGISTER", "reason_code": s.skip_reason,
                         "stage_signature": hashlib.sha256(f"skip|{s.source_id}|{s.sha256}".encode()).hexdigest(),
                         "call_signature": None, "source_sha256": s.sha256 or None,
                         "pipeline_version": PIPELINE_VERSION, "extractor_id": "vkm-pipeline",
                         "extractor_version": "0.1.0", "extraction_generation": 1,
                         "config_hash": hashlib.sha256(b"register-lifecycle-v1").hexdigest(), "model_id": None,
                         "model_revision": None, "models": [], "input_artifact_ids": [], "output_artifact_ids": [],
                         "n_objects_out": 0, "started_at": now, "finished_at": now, "duration_ms": 0,
                         "commit_id": None, "log_ref": None, "host_role": HOST_ROLE})
        self.recorder.add_steps(rows)

    def record_crashes(self, extra: list[dict[str, Any]] | None = None) -> None:
        items = self.crashed + (extra or [])
        if not items:
            return
        from vkm_corpus import ids

        now = _now()
        rows = []
        for i, c in enumerate(items):
            rows.append({"schema_version": "0.1.0", "error_id": ids.error_id(self.run_id, None, c["code"], 10000 + i),
                         "processing_run_id": self.run_id, "step_id": None, "source_id": c["source_id"],
                         "page_id": None, "page_index": None,
                         "stage": {"prepare": "PAGINATE", "visual": "LAYOUT", "commit": "COMMIT"}.get(c["phase"],
                                                                                                      "COMMIT"),
                         "code": c["code"], "tool": "vkm-pipeline-worker", "tool_version": PIPELINE_VERSION,
                         "message": c["message"][:500], "retryable": True, "severity": "ERROR", "attempt": None,
                         "log_ref": None, "exception_type": None, "created_at": now})
        self.recorder.add_errors(rows)

    def finish(self, status: str, log_path: Path | None) -> str:
        log_bytes = log_path.read_bytes() if log_path and log_path.exists() else None
        counters = {"n_model_calls": int(self.counters.get("model_calls", 0)) + int(self.counters.get("layout_calls", 0)),
                    "n_sources_planned": len(self.results) + len(self.crashed)}
        return self.recorder.end(status, log_bytes=log_bytes, counters=counters)


def config_to_json_public(cfg: PipelineConfig) -> dict[str, Any]:
    """Run configuration without machine paths (it becomes the RUN_CONFIG artifact)."""
    d = config_to_json(cfg)
    for k in ("data_root", "resources_root", "models_root"):
        d[k] = {"data_root": "DATA:", "resources_root": "PRIVATE:", "models_root": "MODELS:"}[k] if d.get(k) else None
    if d.get("ocr_url"):
        d["ocr_url"] = "loopback"
    return d

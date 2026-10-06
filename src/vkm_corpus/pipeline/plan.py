"""Planning (``run plan`` / ``run extract --plan-only``): what each source needs, and how many model calls.

Per source: register lifecycle, prepare (cached / new / signature changed), visual+layout (pages without cached
layout), OCR tasks (known from cached layouts; cached results counted by task key), commit (up to date when the
commit signature equals the staging ledger and the source is COMPLETE). ``plan_sha256`` binds a confirmation to the
exact plan: ``--recall-model`` (new model calls for cached signatures) runs only with ``--confirm-plan <sha>`` of an
identical, freshly recomputed plan (H-05, H-12).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from vkm_corpus.extract.model import SourceInput
from vkm_corpus.pipeline import commit as cm
from vkm_corpus.pipeline import ocr_stage
from vkm_corpus.pipeline.config import PipelineConfig
from vkm_corpus.pipeline.prepare import docx_grid_cache_current, load_prep, source_format_admitted
from vkm_corpus.pipeline.context import producer_identity
from vkm_corpus.registry.sources import fresh_source_identity


def _range(page_range: str | None) -> set[int] | None:
    if not page_range:
        return None
    lo, _, hi = page_range.partition("-")
    return set(range(int(lo), int(hi or lo) + 1))


def build_plan(cfg: PipelineConfig, sources: list[SourceInput], cache: Any, store: Any, *, force: bool = False,
               failed_only: bool = False, recall: bool = False, page_range: str | None = None,
               no_ocr: bool = False) -> dict[str, Any]:
    identity = producer_identity(cfg)
    # Verify BEFORE reading stage caches: size+mtime caches cannot attest the current source bytes.
    fresh = {s.source_id: fresh_source_identity(cfg.resources_root, s.canonical_path, s.sha256, s.size_bytes)
             for s in sources if s.lifecycle == "ACTIVE"} if cfg.profile == "production" else {}
    if len({s.source_id for s in sources}) != len(sources):
        raise ValueError("duplicate source IDs in plan selection")
    ledger = cm.load_ledger(cfg)
    by_task: dict[str, bool] = {}
    for entries in cache.calls.values():
        for r in entries:
            if r.get("kind") == "OCR" and r.get("task_key"):
                by_task[r["task_key"]] = by_task.get(r["task_key"], False) or r.get("status") == "OK"
    keep = _range(page_range)
    out = []
    totals = {"sources": 0, "skipped_by_register": 0, "up_to_date": 0, "to_process": 0, "prepare_new": 0,
              "layout_pages_pending": 0, "ocr_tasks_known": 0, "ocr_cached_known": 0, "ocr_calls_known": 0,
              "pages_without_layout": 0}
    for s in sources:
        totals["sources"] += 1
        e: dict[str, Any] = {"source_id": s.source_id, "reasons": []}
        if s.source_id in fresh:
            e["source_identity"] = fresh[s.source_id]
        if s.lifecycle != "ACTIVE":
            e.update(action="SKIPPED_BY_REGISTER", reason_code=s.skip_reason)
            totals["skipped_by_register"] += 1
            out.append(e)
            continue
        sig = cm.prep_signature_for(cfg, s)
        prep = load_prep(cfg.data_root, s.source_id, sig)
        admitted = {"_fresh_source_identity": fresh[s.source_id]} if s.source_id in fresh else {}
        if prep is not None and not source_format_admitted(cfg, s, prep, **admitted):
            prep = None
            e["reasons"].append("SOURCE_FORMAT_CACHE_MISMATCH")
        if prep is not None and (prep.get("inspect") or {}).get("file_format") == "DOCX" \
                and not docx_grid_cache_current(prep, s, store=store):
            prep = None
            e["reasons"].append("DOCX_NATIVE_GRID_STALE")
        any_prep = (Path(cfg.data_root) / "cache" / "prep" / s.source_id).exists()
        if prep is None:
            e["prepare"] = "SIGNATURE_CHANGED" if any_prep else "NEW"
            e["reasons"].append(e["prepare"])
            totals["prepare_new"] += 1
        else:
            e["prepare"] = "FORCED" if force else "CACHED"
        visual = None
        if prep is not None:
            prep.setdefault("canonical_path", s.canonical_path)
            _, visual = cm.load_state(cfg, s, **admitted)
            fmt = prep.get("inspect", {}).get("file_format")
            e["pages"] = len(prep.get("pages", []))
            e["format"] = fmt
            vis_rows = {r["page_index"]: r for r in (visual or {}).get("pages", [])}
            layout_pages = [p for p in prep["pages"] if fmt in ("PDF", "DJVU") and p.get("route") != "FAILED"
                            and (keep is None or p["page_index"] in keep)]
            pending = [p for p in layout_pages if not (vis_rows.get(p["page_index"]) or {}).get("layout_raw_artifact_id")]
            e["layout_pending_pages"] = len(pending)
            totals["layout_pages_pending"] += len(pending)
            if pending:
                e["reasons"].append("LAYOUT_PENDING")
            tasks = cached = 0
            sample = cm.sample_for(cfg, prep)
            decision = cm.load_decision(cfg, cache, prep)
            reocr = (decision or {}).get("decision") == "REOCR_SOURCE"
            for row in prep["pages"]:
                idx = row["page_index"]
                if keep is not None and idx not in keep:
                    continue
                if fmt == "EPUB":
                    unit = store.read_json(row["native_raw_artifact_id"]) if row.get("native_raw_artifact_id") else {}
                    specs = ocr_stage.plan_epub_tasks(s.source_id, unit) if unit else []
                elif fmt in ("PDF", "DJVU"):
                    vis = vis_rows.get(idx)
                    if not (vis or {}).get("layout_raw_artifact_id"):
                        totals["pages_without_layout"] += 1
                        continue
                    _, regions = ocr_stage.load_regions(store, vis["layout_raw_artifact_id"], row.get("width_pt") or 0,
                                                        row.get("height_pt") or 0, cfg.region_thresholds)
                    specs = ocr_stage.plan_page_tasks(cfg, fmt, s.source_id, row, vis, regions,
                                                      sample_b=idx in sample, reocr=reocr)
                else:
                    specs = []
                hits = [bool(by_task.get(ocr_stage.task_key(cfg, sp))) for sp in specs]
                if specs and not all(hits):
                    # the task key moves with the crop/model configuration; the authoritative cache key is the
                    # pixel-based call signature, known from the page's crop manifests without rendering
                    crops = ocr_stage.manifest_crops(cfg, cache, fmt, s.sha256, specs)
                    if crops is not None:
                        done = ocr_stage.cached_specs(cfg, cache, crops)
                        hits = [h or i in done for i, h in enumerate(hits)]
                tasks += len(specs)
                cached += sum(hits)
            e["ocr_tasks_known"] = tasks
            e["ocr_cached_known"] = cached
            e["ocr_calls_known"] = tasks if recall else tasks - cached
            totals["ocr_tasks_known"] += tasks
            totals["ocr_cached_known"] += cached
            totals["ocr_calls_known"] += e["ocr_calls_known"]
            if e["ocr_calls_known"]:
                e["reasons"].append("OCR_PENDING")
        if prep is not None and (prep.get("inspect") or {}).get("file_format") in ("PDF", "DJVU"):
            from vkm_corpus.pipeline.imported_layer import load_source_import

            imp = load_source_import(cache, s.source_id, s.sha256)
            if imp is not None:
                e["imported_layer"] = {"pages": len(imp.pages), "refused": len(imp.refused), "stale": len(imp.stale)}
                totals["imported_pages"] = totals.get("imported_pages", 0) + len(imp.pages)
        csig = cm.commit_signature(cfg, cache, s, prep, visual, **admitted) if prep is not None else None
        led = ledger.get(s.source_id)
        e["commit_signature"] = csig
        e["head_status"] = (led or {}).get("document_processing_status")
        if led is None:
            e["commit"] = "NEW"
        elif csig is not None and led.get("commit_signature") == csig:
            e["commit"] = "UP_TO_DATE"
        else:
            e["commit"] = "SIGNATURE_CHANGED"
        if failed_only and led is not None and led.get("document_processing_status") == "COMPLETE":
            e["action"] = "SKIPPED_NOT_FAILED"
        elif force:
            e["action"] = "PROCESS"
            e["reasons"].append("FORCED")
        elif e["commit"] == "UP_TO_DATE" and not e["reasons"]:
            e["action"] = "UP_TO_DATE"
            totals["up_to_date"] += 1
        else:
            e["action"] = "PROCESS"
            if e["commit"] != "UP_TO_DATE":
                e["reasons"].append("COMMIT_" + e["commit"])
        if e["action"] == "PROCESS":
            totals["to_process"] += 1
        out.append(e)
    body = {"schema": "vkm.run_plan/1", "flags": {"force": force, "failed_only": failed_only, "recall_model": recall,
                                                  "page_range": page_range, "no_ocr": no_ocr},
            "sources": out, "totals": totals, "producer_identity": identity}
    body["plan_sha256"] = hashlib.sha256(json.dumps(
        {"flags": body["flags"], "producer_identity": identity,
         "sources": [{k: v for k, v in x.items() if k != "head_status"} for x in out]},
        sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    return body

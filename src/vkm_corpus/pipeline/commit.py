"""Phase 4 helpers: scenario-B sample/decision, commit signature, and the commit of one source.

A commit rebuilds the source entirely from caches (``Assembler``), maps it to canonical rows (``extract.to_canon``),
writes its processing steps and errors as run-journal parts, and calls the canonical writer (``commit_source``) under
the source lease. A commit with unchanged content is a no-op (fingerprints equal the parent). The staging commit
ledger (``cache/commits``) remembers the commit signature, so an unchanged source is planned as up to date without
rebuilding it (K-06: repeated run → 0 model calls, 0 new commits).
"""
from __future__ import annotations

import json
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vkm_corpus.extract.model import ErrorRec, SourceInput, StepRec
from vkm_corpus.pipeline import scenario_b as sb
from vkm_corpus.pipeline.assemble import Assembler
from vkm_corpus.pipeline.cache import JsonlAppender, _iter_lines
from vkm_corpus.pipeline.config import PipelineConfig
from vkm_corpus.pipeline.prepare import docx_grid_cache_current, load_prep, prepare_signature, source_format_admitted
from vkm_corpus.pipeline.visual import load_visual, visual_signature
from vkm_corpus.versions import PIPELINE_VERSION


def _cfg_hash(d: dict[str, Any]) -> str:
    from vkm_corpus.contracts.signatures import config_hash

    return config_hash(d)


def prep_signature_for(cfg: PipelineConfig, src: SourceInput) -> str:
    return prepare_signature(cfg, src, {"docx_image": cfg.docx_render_image}
                             if src.canonical_path.lower().endswith(".docx") else None)


def load_state(cfg: PipelineConfig, src: SourceInput, *, _fresh_source_identity: dict[str, Any] | None = None
               ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    prep = load_prep(cfg.data_root, src.source_id, prep_signature_for(cfg, src))
    if prep is None:
        return None, None
    if not source_format_admitted(cfg, src, prep, _fresh_source_identity=_fresh_source_identity):
        return None, None
    if prep.get("inspect", {}).get("file_format") == "DOCX" and not docx_grid_cache_current(
            prep, src, data_root=cfg.data_root):
        return None, None  # old native DOCX prep requires fresh adapter execution, even without .docx suffix
    prep.setdefault("canonical_path", src.canonical_path)
    visual = load_visual(cfg.data_root, src.source_id, visual_signature(cfg, prep)) \
        if prep.get("inspect", {}).get("file_format") in ("PDF", "DJVU", "DOCX") else None
    return prep, visual


# ---------------------------------------------------------------------------------------------------- scenario B
def embedded_pages(prep: dict[str, Any]) -> list[int]:
    return [p["page_index"] for p in prep.get("pages", []) if p.get("route") == "OCR_OPTIONAL"]


def sample_for(cfg: PipelineConfig, prep: dict[str, Any]) -> set[int]:
    if not cfg.scenario_b.enabled:
        return set()
    return set(sb.sample_pages(embedded_pages(prep), cfg.scenario_b.share, cfg.scenario_b.min_pages))


def scenario_signature(cfg: PipelineConfig, prep: dict[str, Any]) -> str:
    from vkm_corpus.contracts.signatures import stage_signature

    return stage_signature(source_sha256=prep["source_sha256"], stage="SCENARIO_B",
                           pipeline_version=PIPELINE_VERSION, extractor_id="vkm-pipeline", extractor_version="0.1.0",
                           stage_config_hash=_cfg_hash({"b": cfg.stage_config("SCENARIO_B"),
                                                        "ocr": cfg.stage_config("OCR"),
                                                        "regions": cfg.stage_config("REGIONS"),
                                                        "prep": prep["prepare_signature"]}))


def decide_scenario_b(cfg: PipelineConfig, store: Any, cache: Any, src: SourceInput, prep: dict[str, Any],
                      visual: dict[str, Any] | None) -> dict[str, Any] | None:
    """CER of the embedded layer on the sampled pages → decision; cached by the scenario signature."""
    sample = sample_for(cfg, prep)
    if not sample:
        return None
    asm = Assembler(cfg, store, cache, src, prep, visual, sample_pages=sample, only_pages=sample)
    res = asm.build()
    rows = []
    for p in res.pages:
        sbx = p.extra.get("scenario_b")
        if sbx:
            rows.append({"page_index": p.page_index, "cer": p.native_ocr_cer, **sbx})
        else:
            rows.append({"page_index": p.page_index, "cer": None, "distance": 0, "ref_len": 0,
                         "layer_letter_share": None, "missing_glm": True})
    decision = sb.decide(rows, cfg.scenario_b.cer_threshold, cfg.scenario_b.letter_share_min)
    decision.update({"source_id": src.source_id, "embedded_pages": len(embedded_pages(prep)),
                     "sample_page_indices": sorted(sample), "rows": rows,
                     "complete": all(not r.get("missing_glm") for r in rows)})
    if decision["complete"]:
        cache.add_stage(stage_signature=scenario_signature(cfg, prep), stage="SCENARIO_B", source_id=src.source_id,
                        page_index=None, outputs=decision)
    return decision


def load_decision(cfg: PipelineConfig, cache: Any, prep: dict[str, Any]) -> dict[str, Any] | None:
    hit = cache.get_stage(scenario_signature(cfg, prep))
    return hit["outputs"] if hit else None


# ---------------------------------------------------------------------------------------------------- commit ledger
def ledger_dir(cfg: PipelineConfig) -> Path:
    return Path(cfg.data_root) / "cache" / "commits"


def load_ledger(cfg: PipelineConfig) -> dict[str, dict[str, Any]]:
    """source_id → latest ledger entry."""
    out: dict[str, dict[str, Any]] = {}
    for rec in _iter_lines(ledger_dir(cfg)):
        prev = out.get(rec["source_id"])
        if prev is None or rec.get("recorded_at", "") >= prev.get("recorded_at", ""):
            out[rec["source_id"]] = rec
    from vkm_corpus.coverage.accounting import verify_binding

    for rec in out.values():
        try:
            rec["accounting_state"] = verify_binding(cfg.data_root, rec["accounting_receipt"],
                commit_id=rec["commit_id"], source_id=rec["source_id"], require_head=True)
        except (OSError, ValueError, KeyError):
            rec["accounting_state"] = "MISSING_OR_INVALID"
            rec["commit_signature"] = None
            rec["canonical_document_processing_status"] = rec.get("document_processing_status")
            rec["document_processing_status"] = "PARTIAL"
    return out


def commit_signature(cfg: PipelineConfig, cache: Any, src: SourceInput, prep: dict[str, Any] | None,
                     visual: dict[str, Any] | None, *, accounting_events: list[dict] | None = None,
                     _fresh_source_identity: dict[str, Any] | None = None) -> str | None:
    """Signature of everything a commit is built from (configs, prep/visual summaries, cached model results)."""
    if prep is None:
        return None
    if not source_format_admitted(cfg, src, prep, _fresh_source_identity=_fresh_source_identity):
        return None
    docx = (prep.get("inspect") or {}).get("file_format") == "DOCX"
    if docx and not docx_grid_cache_current(prep, src, data_root=cfg.data_root):
        return None
    from vkm_corpus.extract.bibliography import CONFIG_HASH as BIBLIOGRAPHY_RULES
    from vkm_corpus.coverage.accounting import ocr_events
    from vkm_evidence.contracts import record_hash

    if accounting_events is None:
        accounting_events = [ref for ref, _ in ocr_events(cfg.data_root, src.source_id, src.sha256,
                                                        record_hash(cfg.stage_config("OCR")))]

    calls = sorted(r["raw_artifact_id"] for entries in cache.calls.values() for r in entries
                   if r.get("source_id") == src.source_id and r.get("status") == "OK" and r.get("kind") == "OCR")
    decision = load_decision(cfg, cache, prep)
    inputs = {"pipeline": PIPELINE_VERSION, "prep": prep.get("prepare_signature"),
                      "visual": (visual or {}).get("visual_signature"),
                      "visual_complete": (visual or {}).get("complete"),
                      "configs": {k: cfg.stage_config(k) for k in ("REGIONS", "OCR", "NORMALIZE", "SCENARIO_B")},
                      "ocr_results": _cfg_hash({"ids": calls}), "decision": (decision or {}).get("decision"),
                      "to_canon": "to_canon_v3", "bibliography": BIBLIOGRAPHY_RULES,
                      "accounting": "pipeline-object-accounting/1",
                      "accounting_events": sorted(ref["sha256"] for ref in accounting_events)}
    if docx:
        inputs["docx_native_grid"] = {"signature": prep["docx_grid_signature"],
                                      "native_artifact_id": prep["document_raw_artifact_id"]}
    return _cfg_hash(inputs)


# ---------------------------------------------------------------------------------------------------- commit
def commit_source_once(cfg: PipelineConfig, run_id: str, src: SourceInput, *, store: Any, cache: Any,
                       part_base: int, host_role: str = "WORKSTATION", code_revision: str = "unknown",
                       log: Any = None) -> dict[str, Any]:
    from vkm_corpus.artifacts.store import load_index
    from vkm_corpus.extract import to_canon
    from vkm_corpus.parquet.commits import acquire_lease, commit_source
    from vkm_corpus.parquet.layout import open_root
    from vkm_corpus.parquet.writer import write_partition

    started = datetime.now(timezone.utc)
    layout = open_root(cfg.data_root, "STAGING")
    prep, visual = load_state(cfg, src)
    if prep is None:
        return {"source_id": src.source_id, "status": "NOT_PROCESSED", "error": "prep summary missing"}
    decision = load_decision(cfg, cache, prep)
    asm = Assembler(cfg, store, cache, src, prep, visual, sample_pages=sample_for(cfg, prep),
                    scenario_decision=decision)
    result = asm.build()
    result.metrics["scenario_b"] = decision
    unit = {"EPUB": "s", "DOCX": "r"}.get(prep.get("inspect", {}).get("file_format"), "p")
    hashes = {"objects": _cfg_hash(cfg.stage_config("NORMALIZE")),
              "page": _cfg_hash({"normalize": cfg.stage_config("NORMALIZE"), "regions": cfg.stage_config("REGIONS")}),
              "document": _cfg_hash({"classify": cfg.stage_config("CLASSIFY"), "native": cfg.stage_config(
                  "NATIVE_TEXT")})}
    now = datetime.now(timezone.utc)
    mapper = to_canon.CanonMapper(result, run_id=run_id, config_hashes=hashes, created_at=now, host_role=host_role)
    rows = mapper.build()
    from vkm_corpus.coverage.accounting import publish_report, bind_commit

    accounting = asm.accounting.finish(result, rows, cache, code_revision=code_revision)
    if rows.document_status == "COMPLETE" and accounting["report"]["status"] != "ACCOUNTED":
        raise ValueError("ACCOUNTING_INCOMPLETE_FOR_COMPLETE_SOURCE")
    accounting_report = publish_report(cfg.data_root, accounting)
    index = load_index(Path(cfg.data_root) / "cache" / "artifacts")
    # OCR inputs are KEEP_RAW: index them with the source even though only the raw records reference them
    extra_ids = set()
    for s in result.steps:
        extra_ids.update(s.input_artifact_ids)
        extra_ids.update(s.output_artifact_ids)
    art_rows, missing = to_canon.artifact_rows(index, rows.artifact_ids | extra_ids, run_id=run_id, created_at=now)
    for aid in missing:
        result.errors.append(ErrorRec(code="ARTIFACT_MISSING", stage="COMMIT",
                                      message=f"artifact {aid} not in the staging index"))
    # Pin exactly the attempts included in this report; a concurrent later event
    # must invalidate plan eligibility, not enter a signature without accounting.
    commit_sig = commit_signature(cfg, cache, src, prep, visual, accounting_events=accounting["ocr_events"])
    lease = acquire_lease(layout, src.source_id, run_id, host_role)
    try:
        res = commit_source(layout, source_id=src.source_id, source_sha256=src.sha256, run_id=run_id,
                            tables=rows.tables, artifact_rows=art_rows, document_processing_status=rows.document_status,
                            page_count=rows.page_count, host_role=host_role, code_revision=code_revision,
                            accounting_required=True)
        accounting_receipt = bind_commit(cfg.data_root, accounting_report, commit_id=res.commit_id,
                                        source_id=src.source_id, source_sha256=src.sha256)
    finally:
        lease.release()
    finished = datetime.now(timezone.utc)
    result.steps.append(StepRec(stage="COMMIT", page_index=None, outcome="SKIPPED_UP_TO_DATE" if res.noop else
                                "EXECUTED", status="NATIVE_OK" if rows.document_status == "COMPLETE" else "PARTIAL",
                                stage_signature=commit_sig or "0" * 64, extractor_id="vkm-pipeline",
                                extractor_version="0.1.0", config_hash=hashes["objects"], started_at=started,
                                finished_at=finished, n_objects_out=sum(rows.counts.values())))
    step_dicts = to_canon.step_rows(result, run_id=run_id, host_role=host_role, started_at=started,
                                    finished_at=finished, unit=unit)
    for d in step_dicts:
        if d["stage"] == "COMMIT":
            d["commit_id"] = res.commit_id
    err_dicts = to_canon.error_rows(result, run_id=run_id, created_at=finished, unit=unit)
    parts = []
    if step_dicts:
        e = write_partition(layout, "processing_steps", step_dicts, run_id=run_id, part=part_base)
        parts.append(e.path)
    if err_dicts:
        e = write_partition(layout, "errors", err_dicts, run_id=run_id, part=part_base)
        parts.append(e.path)
    rec = {"source_id": src.source_id, "commit_signature": commit_sig, "commit_id": res.commit_id, "noop": res.noop,
           "run_id": run_id, "document_processing_status": rows.document_status, "page_count": rows.page_count,
           "page_status_counts": _count([p.page_status for p in result.pages]), "counts": rows.counts,
           "accounting_receipt": accounting_receipt, "accounting_state": accounting["report"]["status"],
           "recorded_at": finished.isoformat()}
    w = JsonlAppender(ledger_dir(cfg) / f"run={run_id}" / f"{src.source_id}.jsonl")
    w.append(rec)
    w.close()
    return {**rec, "status": rows.document_status, "journal_parts": parts, "n_errors": len(err_dicts),
            "n_steps": len(step_dicts), "missing_artifacts": len(missing), "metrics": _metrics(result)}


def _count(values: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out


def _metrics(result: Any) -> dict[str, Any]:
    pages = result.pages
    return {"pages": len(pages), "blocks": len(result.blocks), "figures": len(result.figures),
            "tables": len(result.tables), "formulas": len(result.formulas),
            "origin_counts": _count([p.primary_text_origin for p in pages]),
            "embedded_pages": sum(1 for p in pages if p.route == "OCR_OPTIONAL"),
            "embedded_marked_native": sum(1 for p in pages if p.route == "OCR_OPTIONAL" and
                                          p.primary_text_origin == "NATIVE"),
            "plain_text_sha256": {str(p.page_index): p.plain_text_sha256 for p in pages if p.plain_text_sha256},
            "scenario_b": result.metrics.get("scenario_b")}


def safe_commit(cfg: PipelineConfig, run_id: str, src: SourceInput, **kw: Any) -> dict[str, Any]:
    try:
        return commit_source_once(cfg, run_id, src, **kw)
    except Exception as exc:  # noqa: BLE001 - reported to the orchestrator as a failed commit
        return {"source_id": src.source_id, "status": "COMMIT_FAILED", "error": f"{type(exc).__name__}: {exc}"[:500],
                "traceback": traceback.format_exc()[-3000:]}

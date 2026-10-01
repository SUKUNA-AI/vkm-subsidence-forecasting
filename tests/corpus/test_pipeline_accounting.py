"""Real producer hooks, synthetic sources and mock models only (no GPU/OCR server)."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import hashlib
import io

import pytest

from test_pipeline_e2e import env, _run_all, RUN
from vkm_corpus.coverage import accounting
from vkm_corpus.layout.regions import regions_from_raw
from vkm_corpus.pipeline import commit as cm
from vkm_corpus.pipeline import ocr_stage
from vkm_corpus.pipeline.context import open_cache, open_store
from vkm_corpus.pipeline.sources import load_sources


@pytest.fixture
def completed(env):
    sources, stats, receipts = _run_all(env, RUN)
    return env, sources, stats, receipts


def report_for(cfg, receipt):
    binding = json.loads((cfg.data_root / receipt["accounting_receipt"]["path"]).read_bytes())
    report = json.loads((cfg.data_root / binding["report"]["path"]).read_bytes())
    return binding, report


def detection(query, label, score, box, i):
    return {"query": query, "label_id": label, "label": "text", "score": score,
            "box_pt": box, "order_rank": i}


def test_trace_preserves_every_raw_detection_and_exact_suppression_reason():
    raw = {"detections": [
        detection(1, 22, .9, [1, 1, 40, 40], 0),
        detection(1, 17, .8, [1, 1, 40, 40], 1),
        detection(2, 22, .2, [1, 40, 40, 80], 2),
        detection(3, 22, .8, [1, 1, 40, 40], 3),
        detection(4, 22, .8, [110, 110, 120, 120], 4),
    ]}
    trace = []
    ordinary = regions_from_raw(raw, 100, 100)
    traced = regions_from_raw(raw, 100, 100, trace=trace)
    assert ordinary == traced and len(traced) == 1
    assert [d["det_index"] for d in trace] == [0, 1, 2, 3, 4]
    assert [d["reason"] for d in trace] == [None, "BEST_QUERY_CLASS", "CONFIDENCE_THRESHOLD", "NMS_OVERLAP", "CLIPPED_BOX_TOO_SMALL"]
    assert trace[1]["duplicate_of"] == trace[3]["duplicate_of"] == 0


def test_denominator_never_comes_from_surviving_output_rows():
    prep = {"source_id": "VKM-SRC-001", "source_sha256": "a" * 64,
        "pagination": {"count": 4, "unit": "p", "basis": "PDF_PAGE_TREE", "check_count": 5},
        "pages": [{"page_index": n} for n in (1, 1, 2, 6)]}
    summary = accounting.expected_summary(prep)
    assert summary["expected_count"] == 4 and summary["missing_indices"] == [3, 4]
    assert summary["duplicate_indices"] == [1] and summary["extra_indices"] == [6]
    assert not summary["pagination_agrees"]
    prep["pagination"] = None
    assert accounting.expected_summary(prep)["denominator_state"] == "UNKNOWN"


def test_actual_hooks_bind_verified_mapper_outputs_and_private_original_hashes(completed):
    cfg, sources, _, receipts = completed
    for source in sources[:2]:
        receipt = receipts[source.source_id]
        binding, report = report_for(cfg, receipt)
        assert report["source_sha256"] == source.sha256
        assert report["expected"]["expected_count"] == (6 if source.source_id.endswith("901") else 3)
        assert report["report"]["status"] == "ACCOUNTED"
        assert report["ledger"]["campaign_sha256"]
        assert report["output_objects"]
        linked = {oid: sha for a in report["ledger"]["attempts"] for oid, sha in a["object_outputs"].items()}
        assert linked == report["output_objects"]
        assert report["unmatched_output_ids"] == []
        assert all(x["raw_artifact_id"].startswith("sha256:") and len(x["input_sha256"]) == 64
                   for x in report["occurrences"])
        assert accounting.verify_binding(cfg.data_root, receipt["accounting_receipt"],
            commit_id=receipt["commit_id"], source_id=source.source_id, require_head=True) == "ACCOUNTED"
        assert binding["physical_durability"] == "NOT_QUALIFIED"
        assert report["detector_recall"] == report["scientific_admission"] == "NOT_ESTABLISHED"
        assert "Синтетический распознанный текст" not in json.dumps(report, ensure_ascii=False)


def test_actual_failed_page_stays_in_expected_denominator(env, monkeypatch):
    from vkm_corpus.extract import pdf_native
    original = pdf_native.extract_page
    def fail_one(doc, index, *args, **kw):
        if index == 1:
            raise RuntimeError("synthetic native extraction failure")
        return original(doc, index, *args, **kw)
    monkeypatch.setattr(pdf_native, "extract_page", fail_one)
    _, _, receipts = _run_all(env, RUN)
    _, report = report_for(env, receipts["VKM-SRC-901"])
    assert report["expected"]["expected_count"] == 6
    assert any(a["stage"] == "PAGE_ASSEMBLY" and a["state"] == "FAILED" for a in report["ledger"]["attempts"])
    assert len([u for u in report["ledger"]["units"] if u["unit_kind"] == "PAGE"]) == 6


def test_missing_accounting_receipt_blocks_up_to_date_even_with_old_complete_cache(completed):
    cfg, _, _, receipts = completed
    sid = "VKM-SRC-902"
    path = cfg.data_root / receipts[sid]["accounting_receipt"]["path"]
    path.write_bytes(b"synthetic corrupt receipt")
    ledger = cm.load_ledger(cfg)
    assert ledger[sid]["accounting_state"] == "MISSING_OR_INVALID"
    assert ledger[sid]["commit_signature"] is None and ledger[sid]["document_processing_status"] == "PARTIAL"


def test_final_binding_failure_never_returns_complete_and_retry_recovers_existing_commit(env, monkeypatch):
    original = accounting.bind_commit
    def fail(*args, **kw):
        raise OSError("synthetic receipt fsync failure")
    monkeypatch.setattr(accounting, "bind_commit", fail)
    with pytest.raises(OSError, match="synthetic receipt"):
        _run_all(env, RUN)
    assert cm.load_ledger(env) == {}
    monkeypatch.setattr(accounting, "bind_commit", original)
    _, _, receipts = _run_all(env, "RUN-20260928T000001Z-0000c0df")
    assert receipts["VKM-SRC-901"]["noop"]
    assert all(r["accounting_state"] == "ACCOUNTED" for r in receipts.values())


def test_canonical_partition_tamper_cannot_receive_new_binding(completed):
    cfg, _, _, receipts = completed
    sid = "VKM-SRC-901"
    binding, _ = report_for(cfg, receipts[sid])
    marker = accounting._commit_marker(cfg.data_root, sid, receipts[sid]["commit_id"])
    path = cfg.data_root / "canonical" / marker["datasets"]["blocks"]["path"]
    path.write_bytes(path.read_bytes() + b"corrupt")
    with pytest.raises(ValueError, match="COMMITTED_PARTITION_CHANGED"):
        accounting.bind_commit(cfg.data_root, binding["report"], commit_id=receipts[sid]["commit_id"],
                               source_id=sid, source_sha256=binding["source_sha256"])


def test_ocr_budget_branch_records_every_task_without_model_calls(env):
    # First run populates synthetic raw/crop fixtures via a mock, never a server.
    _, _, receipts = _run_all(env, RUN)
    sid = "VKM-SRC-901"
    sources = load_sources(env.resources_root)
    source = next(s for s in sources if s.source_id == sid)
    run = "RUN-20260928T000002Z-0000c0aa"
    store, cache = open_store(env, run, "budget"), open_cache(env, run, "budget")
    try:
        prep, visual = cm.load_state(env, source)
        page = next(r for r in prep["pages"] if r["route"] == "OCR_REQUIRED")
        vis = next(r for r in visual["pages"] if r["page_index"] == page["page_index"])
        _, regions = ocr_stage.load_regions(store, vis["layout_raw_artifact_id"], page["width_pt"], page["height_pt"], env.region_thresholds)
        specs = ocr_stage.plan_page_tasks(env, "PDF", sid, page, vis, regions)
        work = [ocr_stage.PageWork(sid, "PDF", str(env.resources_root / source.canonical_path), page["page_index"], specs, source.sha256)]
        import httpx
        def no_model(request):
            pytest.fail("budget-zero must not invoke a model")
        stats = asyncio.run(ocr_stage.run_ocr(env, store, cache, work, backend={"engine": "synthetic"},
            max_calls=0, recall=True, crop_workers=1, transport=httpx.MockTransport(no_model)))
        assert stats.called == 0 and stats.skipped_budget > 0
        events = [e for _, e in accounting.ocr_events(env.data_root, sid, source.sha256) if e["run_id"] == run]
        assert len([e for e in events if e["phase"] == "PLANNED"]) == len(specs)
        assert any(e["reason"] == "OCR_CALL_BUDGET_EXHAUSTED" for e in events)
        assert not any(e["phase"] == "SUCCEEDED" for e in events)
    finally:
        cache.close()
        store.close()


def test_native_docx_empty_paragraph_is_explicitly_suppressed_without_fake_page(env):
    from vkm_corpus.artifacts.store import ArtifactStore
    from vkm_corpus.extract.model import SourceResult, SourceInput
    from vkm_corpus.extract.to_canon import CanonRows
    prep = {"source_id": "VKM-SRC-901", "source_sha256": "a" * 64, "pages": [], "pagination": None}
    store = ArtifactStore(env.data_root / "artifacts")
    raw = {"schema": "vkm.native_raw.docx_document/1", "paragraphs": [{"path": "footnotes/p[1]", "text": ""}]}
    aid = store.put_json(raw, "NATIVE_RAW", source_id=prep["source_id"]).artifact_id
    acc = accounting.SourceAccounting(env, prep, store)
    acc.native(raw, aid, docx=True)
    result = SourceResult(source=SourceInput(prep["source_id"], "synthetic.docx", prep["source_sha256"], 0))
    report = acc.finish(result, CanonRows(), None, code_revision="synthetic")
    assert report["ledger"]["units"][0]["unit_kind"] == "NATIVE_OBJECT"
    assert report["ledger"]["units"][0]["locator"] == "footnotes/p[1]"
    assert report["ledger"]["candidates"][0]["reason"] == "EMPTY_NATIVE_PARAGRAPH"
    assert report["report"]["status"] == "INCOMPLETE"
    assert report["expected"]["denominator_state"] == "UNKNOWN"
    store.close()


def test_mismatched_source_version_or_output_count_is_not_accounted(completed):
    from vkm_corpus.pipeline.assemble import Assembler
    from vkm_corpus.extract.to_canon import CanonMapper
    cfg, sources, _, _ = completed
    source = sources[0]
    store, cache = open_store(cfg, RUN, "negative"), open_cache(cfg, RUN, "negative")
    try:
        prep, visual = cm.load_state(cfg, source)
        assembler = Assembler(cfg, store, cache, source, prep, visual)
        result = assembler.build()
        rows = CanonMapper(result, run_id=RUN, config_hashes={"objects": "a" * 64}).build()
        rows.tables["blocks"][0] = rows.tables["blocks"][0].model_copy(update={"source_sha256": "b" * 64})
        with pytest.raises(ValueError, match="OUTPUT_SOURCE_OR_LOCATOR_MISMATCH"):
            assembler.accounting.finish(result, rows, cache, code_revision="synthetic")
        rows.tables["blocks"] = []
        with pytest.raises(ValueError, match="ASSEMBLY_MAPPER_COUNT_MISMATCH"):
            assembler.accounting.finish(result, rows, cache, code_revision="synthetic")
    finally:
        cache.close()
        store.close()


@pytest.mark.parametrize("case", ["token_limit", "size_guard", "worker_failure", "missing_crop", "missing_band", "unexpected_crop"])
def test_actual_ocr_failure_branches_persist_reasons_without_real_ocr(env, monkeypatch, case):
    import httpx
    from PIL import Image
    run = "RUN-20260928T000009Z-0000c0ff"
    spec = ocr_stage.OcrTaskSpec("VKM-SRC-901", 1, "VKM-SRC-901:p0001", "text", "PRIMARY", 200)
    buffer = io.BytesIO()
    Image.new("L", (16, 16), 127).save(buffer, format="PNG")
    png = buffer.getvalue()
    digest = hashlib.sha256(png).hexdigest()
    def crop_job(args):
        if case == "worker_failure":
            raise RuntimeError("synthetic worker failure")
        if case == "missing_crop":
            return []
        crop = ocr_stage.CropOut(spec, png, digest, digest, 16, 16, "L", .5, [0, 0, 16, 16],
            error="synthetic image size too large" if case == "size_guard" else None,
            band_count=2 if case == "missing_band" else 1)
        crop_spec = asdict(spec)
        if case == "unexpected_crop":
            crop_spec["page_index"] = 99
            crop_spec["page_id"] = "VKM-SRC-901:p0099"
        return [{**crop.__dict__, "spec": crop_spec}]
    monkeypatch.setattr(ocr_stage, "_crop_job", crop_job)
    monkeypatch.setattr(ocr_stage, "ProcessPoolExecutor", lambda max_workers, mp_context: ThreadPoolExecutor(max_workers=max_workers))
    calls = []
    def transport(request):
        calls.append(request)
        assert case == "token_limit"
        return httpx.Response(200, json={"choices": [{"message": {"content": "synthetic partial"}, "finish_reason": "length"}]})
    env.stop_window = 1
    work = [ocr_stage.PageWork(spec.source_id, "PDF", "synthetic-unused", 1, [spec], "a" * 64)]
    store, cache = open_store(env, run, "failure"), open_cache(env, run, "failure")
    try:
        call = ocr_stage.run_ocr(env, store, cache, work, backend={"engine": "synthetic"}, crop_workers=1,
                                  transport=httpx.MockTransport(transport))
        if case in {"worker_failure", "missing_crop", "missing_band", "unexpected_crop"}:
            with pytest.raises((RuntimeError, ValueError)):
                asyncio.run(call)
        else:
            stats = asyncio.run(call)
            assert stats.called == (1 if case == "token_limit" else 0)
        events = [e for _, e in accounting.ocr_events(env.data_root, spec.source_id, "a" * 64)]
        expected = {"token_limit": "OCR_TOKEN_LIMIT", "size_guard": "OCR_CROP_SIZE_GUARD",
            "worker_failure": "OCR_CROP_WORKER_FAILED", "missing_crop": "OCR_CROP_WORKER_OUTPUT_MISSING",
            "missing_band": "OCR_CROP_BANDS_INVALID", "unexpected_crop": "OCR_CROP_WORKER_UNEXPECTED_OUTPUT"}[case]
        assert any(e["reason"] == expected for e in events)
        assert len(calls) == (1 if case == "token_limit" else 0)
        if case == "token_limit":
            assert any(e["phase"] == "RUN_STOPPED" for e in events)
        if case == "size_guard":
            assert any(e["error_sha256"] for e in events)
    finally:
        cache.close()
        store.close()


def test_new_failed_attempt_invalidates_old_commit_signature(completed):
    cfg, sources, _, receipts = completed
    source = sources[0]
    store, cache = open_store(cfg, RUN, "signature"), open_cache(cfg, RUN, "signature")
    try:
        prep, visual = cm.load_state(cfg, source)
        before = cm.commit_signature(cfg, cache, source, prep, visual)
        spec = ocr_stage.OcrTaskSpec(source.source_id, 1, source.source_id + ":p0001", "text", "PRIMARY", 200)
        accounting.record_ocr_event(cfg, cache, spec, source.sha256,
            phase="FAILED", reason="SYNTHETIC_POST_COMMIT_FAILURE")
        assert cm.commit_signature(cfg, cache, source, prep, visual) != before
        _, report = report_for(cfg, receipts[source.source_id])
        assert cm.commit_signature(cfg, cache, source, prep, visual,
            accounting_events=report["ocr_events"]) == before
    finally:
        cache.close()
        store.close()


def test_new_canonical_head_without_binding_invalidates_previous_complete_ledger(completed):
    from vkm_corpus.parquet.commits import commit_source, DOCUMENT_DATASETS
    from vkm_corpus.parquet.layout import open_root
    cfg, sources, _, receipts = completed
    source = sources[1]
    old = receipts[source.source_id]
    assert old["document_processing_status"] == "COMPLETE"
    newer = commit_source(open_root(cfg.data_root, "STAGING"), source_id=source.source_id,
        source_sha256=source.sha256, run_id="RUN-20260928T000011Z-0000c001",
        tables={name: [] for name in DOCUMENT_DATASETS},
        document_processing_status="COMPLETE", page_count=3, require_lease=False)
    assert newer.commit_id != old["commit_id"]
    ledger = cm.load_ledger(cfg)[source.source_id]
    assert ledger["accounting_state"] == "MISSING_OR_INVALID"
    assert ledger["commit_signature"] is None and ledger["document_processing_status"] == "PARTIAL"

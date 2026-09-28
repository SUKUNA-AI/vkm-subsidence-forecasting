"""End-to-end pipeline on a synthetic resources root (agent C), without GPU and without the OCR server:

* a fake layout model produces LAYOUT_RAW; an ``httpx.MockTransport`` plays GLM-OCR;
* every page of every source has exactly one page row (no silent page loss), with a status and a text layer that
  pass the contract models; register-skipped sources are SKIPPED_BY_REGISTER (never UNSUPPORTED);
* a second OCR pass makes 0 model calls (call cache) and a second commit is a no-op (K-06);
* a page whose native extraction fails keeps its row as FAILED with an error.
"""
from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("pymupdf")
pytest.importorskip("pyarrow")
httpx = pytest.importorskip("httpx")

from vkm_corpus.extract.synthetic import make_epub, make_pdf, make_register, register_row  # noqa: E402

RUN = "RUN-20260928T000000Z-0000c0de"


class FakeLayout:
    """Stand-in for PP-DocLayoutV3: one text region per page half, one display formula."""

    runtime = {"device": "cpu", "fake": True}
    id2label = {22: "text", 5: "formula", 21: "table"}

    def detect(self, images):
        out = []
        for im in images:
            w, h = im.width, im.height
            out.append({"n_queries": 300, "n_classes": 25, "detections": [
                {"query": 1, "label_id": 22, "label": "text", "score": 0.9, "box_px": [0.1 * w, 0.05 * h, 0.9 * w,
                                                                                       0.45 * h], "order_rank": 1},
                {"query": 2, "label_id": 5, "label": "formula", "score": 0.8, "box_px": [0.3 * w, 0.5 * h, 0.7 * w,
                                                                                         0.55 * h], "order_rank": 2},
                {"query": 3, "label_id": 21, "label": "table", "score": 0.8, "box_px": [0.1 * w, 0.6 * h, 0.9 * w,
                                                                                        0.8 * h], "order_rank": 3}]})
        return out


def fake_ocr(request):
    body = json.loads(request.content)
    prompt = body["messages"][0]["content"][1]["text"]
    content = {"Text Recognition:": "Синтетический распознанный текст страницы.",
               "Formula Recognition:": "$$E = m c^{2} \\tag{1.1}$$",
               "Table Recognition:": "<table><tr><td>a</td><td>b</td></tr><tr><td>1</td><td>2</td></tr></table>"}[prompt]
    return httpx.Response(200, json={"choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                                     "usage": {"prompt_tokens": 50, "completion_tokens": 10}})


@pytest.fixture()
def env(tmp_path):
    from vkm_corpus.parquet.layout import init_root
    from vkm_corpus.pipeline.config import PipelineConfig

    res = tmp_path / "resources"
    (res / "docs").mkdir(parents=True)
    make_pdf(res / "docs" / "a.pdf")
    make_epub(res / "docs" / "b.epub")
    rows = [register_row("VKM-SRC-901", "docs/a.pdf", res), register_row("VKM-SRC-902", "docs/b.epub", res),
            register_row("VKM-SRC-903", "docs/gone.zip", res,
                         migration_status="ORIGINAL_ARCHIVE_DELETED_AFTER_PDF_ASSEMBLY", source_class="book_archive"),
            register_row("VKM-SRC-904", "docs/retired.zip", res, evidence_scope="LEGACY_RETIRED",
                         migration_status="RETIRED_FROM_CURRENT_RESEARCH", source_class="retired_legacy_archive")]
    make_register(res, rows)
    data = tmp_path / "staging"
    init_root(data, "STAGING")
    cfg = PipelineConfig(data_root=data, resources_root=res, ocr_url="http://127.0.0.1:8080", workers=1)
    cfg.scenario_b.enabled = False
    return cfg


def _run_all(cfg, run_id, *, fail_page=None, monkeypatch=None):
    from vkm_corpus.pipeline import commit as cm
    from vkm_corpus.pipeline import ocr_stage
    from vkm_corpus.pipeline.context import open_cache, open_store
    from vkm_corpus.pipeline.prepare import prepare_source
    from vkm_corpus.pipeline.sources import load_sources
    from vkm_corpus.pipeline.visual import run_visual

    sources = load_sources(cfg.resources_root)
    active = [s for s in sources if s.lifecycle == "ACTIVE"]
    store = open_store(cfg, run_id, "t")
    cache = open_cache(cfg, run_id, "t")
    for s in active:
        prep = prepare_source(cfg, store, cache, s)
        prep.setdefault("canonical_path", s.canonical_path)
        if prep["inspect"]["file_format"] == "PDF":
            run_visual(cfg, store, cache, prep, FakeLayout())
    work = []
    for s in active:
        prep, visual = cm.load_state(cfg, s)
        fmt = prep["inspect"]["file_format"]
        path = str(cfg.resources_root / s.canonical_path)
        vis = {r["page_index"]: r for r in (visual or {}).get("pages", [])}
        for row in prep["pages"]:
            if fmt == "EPUB":
                specs = ocr_stage.plan_epub_tasks(s.source_id, store.read_json(row["native_raw_artifact_id"]))
            else:
                _, regions = ocr_stage.load_regions(store, (vis.get(row["page_index"]) or {}).get(
                    "layout_raw_artifact_id"), row.get("width_pt") or 0, row.get("height_pt") or 0,
                    cfg.region_thresholds)
                specs = ocr_stage.plan_page_tasks(cfg, fmt, s.source_id, row, vis.get(row["page_index"]), regions)
            if specs:
                work.append(ocr_stage.PageWork(s.source_id, fmt, path, row["page_index"], specs, s.sha256))
    stats = asyncio.run(ocr_stage.run_ocr(cfg, store, cache, work, backend={"engine": "mock"},
                                          transport=httpx.MockTransport(fake_ocr), crop_workers=1))
    results = {}
    for s in active:
        results[s.source_id] = cm.commit_source_once(cfg, run_id, s, store=store, cache=cache,
                                                     part_base=int(s.number) * 100 % 100000)
    store.close()
    cache.close()
    return sources, stats, results


def _rows(cfg, dataset, sid):
    import pyarrow.parquet as pq

    from vkm_corpus.parquet.commits import chain_head, list_markers
    from vkm_corpus.parquet.layout import open_root

    layout = open_root(cfg.data_root)
    markers = list_markers(layout)
    head, _ = chain_head(markers, sid)
    m = next(x for x in markers if x["commit_id"] == head)
    return pq.read_table(layout.path(m["datasets"][dataset]["path"])).to_pylist()


def test_end_to_end_no_page_loss_and_idempotency(env):
    sources, stats, results = _run_all(env, RUN)
    assert stats.called > 0 and stats.failed == 0
    assert results["VKM-SRC-901"]["page_count"] == 6 and results["VKM-SRC-902"]["page_count"] == 3
    pages = _rows(env, "pages", "VKM-SRC-901")
    assert [p["page_index"] for p in pages] == [1, 2, 3, 4, 5, 6]
    by = {p["page_index"]: p for p in pages}
    assert by[1]["page_status"] == "NATIVE_OK" and by[1]["primary_text_origin"] == "NATIVE"
    assert by[3]["page_class"] == "RASTER_SCAN" and by[3]["page_status"] == "OCR_OK"
    assert by[3]["primary_text_origin"] == "OCR" and by[3]["normalized_text"]
    assert by[5]["page_route"] == "NATIVE_REPAIR" and "ENCODING_REPAIRED" not in by[5]["quality_flags"]
    blocks = _rows(env, "blocks", "VKM-SRC-901")
    assert any(b["origin"] == "OCR" and b["region_origin"] == "LAYOUT_MODEL" for b in blocks)
    assert any("ENCODING_REPAIRED" in b["quality_flags"] for b in blocks)
    formulas = _rows(env, "formulas", "VKM-SRC-901")
    assert formulas and all(f["origin"] == "OCR" and f["equation_label"] == "(1.1)" for f in formulas)
    tables = _rows(env, "tables", "VKM-SRC-901")
    assert tables and all(t["n_rows"] == 2 for t in tables)
    epub_pages = _rows(env, "pages", "VKM-SRC-902")
    assert [p["page_id"] for p in epub_pages] == ["VKM-SRC-902:s0001", "VKM-SRC-902:s0002", "VKM-SRC-902:s0003"]
    assert epub_pages[0]["printed_label_origin"] == "EPUB_PAGE_ANCHOR"
    epub_formulas = _rows(env, "formulas", "VKM-SRC-902")
    assert len(epub_formulas) == 2 and {f["recognition_method"] for f in epub_formulas} == {"EPUB_IMAGE_OCR"}
    # validator B04: every object id recomputes from the row (DOCX path / bbox / reading order + text of a block)
    from vkm_corpus import ids

    for sid in ("VKM-SRC-901", "VKM-SRC-902"):
        for ds in ("blocks", "figures", "tables", "formulas"):
            for r in _rows(env, ds, sid):
                if r["region_origin"] == "DOCX_ELEMENT":
                    scope, anchor = ids.document_id(sid), ids.xml_anchor(r["docx_paragraph_path"])
                elif r["bbox_space"] == "PAGE_PT_TL":
                    scope, anchor = r["page_id"], ids.bbox_anchor(r["bbox_x0"], r["bbox_y0"], r["bbox_x1"],
                                                                  r["bbox_y1"])
                elif ds == "blocks":
                    scope, anchor = r["page_id"], ids.ordinal_anchor(r["reading_order"], r["text"] or "")
                else:
                    continue
                pk = ids.producer_key(r["extractor_id"], r["extraction_generation"], r["raw_config_hash"],
                                      r["models"] or [])
                dups = 8 if "DUPLICATE_DETECTION_DISAMBIGUATED" in (r["quality_flags"] or []) else 0
                assert any(ids.object_id(scope, r["object_kind"], r["origin"], r["region_origin"], anchor, pk, d)
                           == r["object_id"] for d in range(dups + 1)), (ds, r["object_id"])
    # validator E11 (H-02): no NATIVE block on a raster-scan page
    raster = {p["page_id"] for p in pages if p["page_class"] == "RASTER_SCAN"}
    assert not [b for b in blocks if b["page_id"] in raster and b["origin"] == "NATIVE"]
    # K-06: second pass → no model calls, no new commit
    _, stats2, results2 = _run_all(env, "RUN-20260928T000001Z-0000c0df")
    assert stats2.called == 0 and stats2.cached == stats.called + stats.cached
    assert all(r["noop"] for r in results2.values())


def test_register_skips_are_never_unsupported(env):
    from vkm_corpus.pipeline.sources import load_sources

    by = {s.source_id: s for s in load_sources(env.resources_root)}
    assert (by["VKM-SRC-903"].lifecycle, by["VKM-SRC-903"].skip_reason) == ("ABSENT_BY_REGISTER",
                                                                           "ARCHIVE_DELETED_AFTER_ASSEMBLY")
    assert (by["VKM-SRC-904"].lifecycle, by["VKM-SRC-904"].skip_reason) == ("RETIRED", "RETIRED_NOT_EVIDENCE")
    from vkm_corpus.pipeline.plan import build_plan
    from vkm_corpus.pipeline.context import open_cache, open_store

    plan = build_plan(env, list(by.values()), open_cache(env, RUN, "p"), open_store(env, RUN, "p"))
    actions = {e["source_id"]: e["action"] for e in plan["sources"]}
    assert actions["VKM-SRC-903"] == actions["VKM-SRC-904"] == "SKIPPED_BY_REGISTER"
    assert plan["totals"]["skipped_by_register"] == 2 and len(plan["plan_sha256"]) == 64


def test_failed_page_keeps_its_row(env, monkeypatch):
    from vkm_corpus.extract import pdf_native

    real = pdf_native.extract_page

    def flaky(doc, index0, *a, **kw):
        if index0 == 1:
            raise RuntimeError("synthetic extraction failure")
        return real(doc, index0, *a, **kw)

    monkeypatch.setattr(pdf_native, "extract_page", flaky)
    _run_all(env, RUN)
    pages = _rows(env, "pages", "VKM-SRC-901")
    assert len(pages) == 6
    failed = [p for p in pages if p["page_status"] == "FAILED"]
    assert [p["page_index"] for p in failed] == [2]

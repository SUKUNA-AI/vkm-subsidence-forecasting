"""Imported OCR v2 layer (stage IMPORTED_LAYER, text layer PADDLEOCR_VL) on synthetic data only.

* geometry rule ``ocrv2_geometry_v1``: the deskew is undone with PIL's own matrix (checked against a real PIL rotation),
  part offsets of split spreads, raster → page points, clip, rotated (/Rotate 90) pages, size refusals;
* split spreads: an object found in both halves inside the overlap band is kept once (part a), order a then b;
* end to end (synthetic producer, fake layout, mock GLM-OCR, a synthetic OCR v2 run): the imported layer becomes the
  primary layer of the NEW pages, the old GLM/native objects stay as the secondary layer with unchanged ids, tables and
  formulas carry ``is_primary_layer``, accounting has no gaps, refused pages keep their layer with an error, and the
  commit signature changes only for the source with an import;
* readers that take tables/formulas wholesale skip ``is_primary_layer = FALSE`` and keep NULL (historical rows);
* historical tables 0.1.2 / formulas 0.1.1 files stay readable with their original hashes.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import pytest

from vkm_corpus.pipeline import imported_layer as il

pytest.importorskip("PIL")


# ============================================================================================ geometry
@pytest.mark.parametrize("w,h,angle", [(1264, 1946, 1.0), (2481, 3508, -0.8), (900, 400, 4.6), (3508, 2479, 0.35)])
def test_deskew_inverse_matches_pil_rotation(w, h, angle):
    from PIL import Image

    m, size = il.pil_rotate_matrix(w, h, -angle)
    img = Image.new("L", (w, h), 255)
    x, y = int(w * 0.71), int(h * 0.23)
    img.putpixel((x, y), 0)
    out = img.rotate(-angle, resample=Image.NEAREST, expand=True, fillcolor=255)   # what prep_images did
    assert out.size == size
    px = out.load()
    dark = [(i, j) for j in range(out.size[1]) for i in range(out.size[0]) if px[i, j] < 128]
    assert dark
    cx = sum(p[0] for p in dark) / len(dark) + 0.5
    cy = sum(p[1] for p in dark) / len(dark) + 0.5
    bx, by = il._apply(m, cx, cy)
    assert abs(bx - (x + 0.5)) < 1.5 and abs(by - (y + 0.5)) < 1.5
    box, approx = il.undo_deskew((cx - 5, cy - 5, cx + 5, cy + 5), {"applied": True, "angle": angle,
                                                                   "size_before": [w, h], "size_after": list(size)})
    assert approx and box[0] <= x + 0.5 <= box[2] and box[1] <= y + 0.5 <= box[3]
    # the envelope of a rotated 10x10 box grows by at most |sin a| * 10 on each axis
    assert box[2] - box[0] <= 10 * (abs(math.cos(math.radians(angle))) + abs(math.sin(math.radians(angle)))) + 1e-6


def test_deskew_not_applied_is_identity_and_size_mismatch_refuses():
    assert il.undo_deskew((1, 2, 3, 4), {"applied": False, "angle": 0.1}) == ((1.0, 2.0, 3.0, 4.0), False)
    assert il.undo_deskew((1, 2, 3, 4), None) == ((1.0, 2.0, 3.0, 4.0), False)
    with pytest.raises(il.ImportRefused) as exc:
        il.undo_deskew((1, 2, 3, 4), {"applied": True, "angle": 1.0, "size_before": [100, 100], "size_after": [99, 99]})
    assert exc.value.code == "DESKEW_SIZE_MISMATCH"


def test_split_offset_scale_and_clip():
    # part b of a spread starts at x = 1824 of the 3088 px page raster; page 741.25 x 466.9 pt
    px = il.to_page_px((10.0, 20.0, 110.0, 70.0), [1824, 0, 3088, 1946])
    assert px == (1834.0, 20.0, 1934.0, 70.0)
    pt, clipped = il.to_page_pt(px, [3088, 1946], 741.25, 466.9)
    s = 741.25 / 3088
    assert not clipped and pt == tuple(round(v, 3) for v in (1834 * s, 20 * 466.9 / 1946, 1934 * s, 70 * 466.9 / 1946))
    pt, clipped = il.to_page_pt((-5.0, 10.0, 3100.0, 1950.0), [3088, 1946], 741.25, 466.9)
    assert clipped and pt == (0.0, round(10 * 466.9 / 1946, 3), 741.25, 466.9)


def test_page_geometry_checks_rotation_raster_and_djvu():
    prep = {"method": "pdfium_render", "page_size_pt": [595.0, 842.0], "page_size_px": [3508, 2479]}
    # /Rotate 90: pdfium reports the unrotated box, the render (and the canon) is the displayed page
    g = il.check_page_geometry(prep, 842.0, 595.0, 90, "PDF")
    assert g["prep_size_basis"] == "TRANSPOSED_FOR_ROTATE" and not g["bbox_approx"]
    with pytest.raises(il.ImportRefused, match="PAGE_SIZE_MISMATCH"):
        il.check_page_geometry(prep, 842.0, 595.0, 0, "PDF")
    with pytest.raises(il.ImportRefused, match="PAGE_ASPECT_MISMATCH"):
        il.check_page_geometry({**prep, "page_size_px": [3508, 2000]}, 842.0, 595.0, 90, "PDF")
    raster = {"method": "pdfimages_original_raster", "page_size_pt": [595.0, 842.0], "page_size_px": [1240, 1754],
              "pdfimages_rows": [{"type": "image", "w": 1240, "h": 1754, "xppi": 150.0, "yppi": 150.0}]}
    assert not il.check_page_geometry(raster, 595.0, 842.0, 0, "PDF")["bbox_approx"]
    loose = {**raster, "pdfimages_rows": [{"type": "image", "w": 1240, "h": 1754, "xppi": 148.0, "yppi": 148.0}]}
    assert il.check_page_geometry(loose, 595.0, 842.0, 0, "PDF")["bbox_approx"]
    djvu = {"method": "ddjvu_native", "page_size_px": [2481, 3508]}
    assert not il.check_page_geometry(djvu, 595.44, 841.92, 0, "DJVU")["bbox_approx"]
    assert il.check_djvu_grid(djvu, (4962, 7016))["raster_scale_vs_info"] == 0.5
    with pytest.raises(il.ImportRefused, match="PAGE_SIZE_MISMATCH"):
        il.check_djvu_grid(djvu, (4962, 6016))
    with pytest.raises(il.ImportRefused, match="PAGE_SIZE_MISMATCH"):
        il.check_djvu_grid(djvu, (2000, 2829))          # an upscaled raster is not the page's grid


# ============================================================================================ synthetic OCR v2 run
def _raw_block(i, label, bbox, content="", group=None):
    return {"block_label": label, "block_content": content, "block_bbox": list(bbox), "block_id": i,
            "block_order": None, "group_id": i if group is None else group}


def _pp_item(part, i, label, bbox, kind, text=None, **kw):
    it = {"part": part, "block_id": i, "label": label, "bbox": list(bbox), "order": i, "group_id": kw.pop("group", i),
          "flags": kw.pop("flags", []), "kept": kw.pop("kept", True), "source": "original", "kind": kind}
    if text is not None:
        it["text"] = text
    it.update(kw)
    return it


TABLE_HTML = "<table><tr><td>a</td><td>b</td></tr><tr><td>1</td><td>2</td></tr></table>"
# an absolute machine path as the OCR v2 kit writes it (built, so this public file carries no host path literal)
FAKE_HOST_ROOT = "/".join(["", "home", "someone", "vkm-ocr"])


def page3_blocks():
    """Raster page (1240x1754 px at 150 ppi on 595x842 pt): every kind of block of the postprocess."""
    raw = [_raw_block(0, "header", (100, 40, 1100, 80), "Колонтитул главы"),
           _raw_block(1, "paragraph_title", (100, 120, 1100, 170), "## Глава 1. Импорт"),
           _raw_block(2, "text", (100, 200, 1100, 600), "Импортированный текст страницы с формулой $x^2$."),
           _raw_block(3, "table", (100, 650, 1100, 900), TABLE_HTML),
           _raw_block(4, "display_formula", (300, 950, 900, 1020), "$$E = m c^{2}$$"),
           _raw_block(5, "formula_number", (950, 960, 1050, 1010), "(1.1)"),
           _raw_block(6, "number", (580, 1680, 660, 1720), "12"),
           _raw_block(7, "image", (100, 1100, 1100, 1600)),
           _raw_block(8, "text", (100, 610, 1100, 640), "", group=2)]
    pp = [_pp_item("", 0, "header", raw[0]["block_bbox"], "header", "Колонтитул главы"),
          _pp_item("", 1, "paragraph_title", raw[1]["block_bbox"], "text", "Глава 1. Импорт"),
          _pp_item("", 2, "text", raw[2]["block_bbox"], "text", "Импортированный текст страницы с формулой $x^2$."),
          _pp_item("", 3, "table", raw[3]["block_bbox"], "table", "a | b\n1 | 2", fragments=[]),
          _pp_item("", 4, "display_formula", raw[4]["block_bbox"], "formula", "$$ E = m c^{2} $$ (1.1)",
                   latex="E = m c^{2}", number="(1.1)", number_block=5),
          _pp_item("", 5, "formula_number", raw[5]["block_bbox"], "formula_number", "", attached=True, kept=False),
          _pp_item("", 6, "number", raw[6]["block_bbox"], "page_number", "12"),
          _pp_item("", 7, "image", raw[7]["block_bbox"], "figure"),
          _pp_item("", 8, "text", raw[8]["block_bbox"], "merged_follower", kept=False, group=2)]
    return raw, pp, [{"part": "", "block_id": 3, "html": TABLE_HTML, "flat": "a | b\n1 | 2", "fragments": 1,
                      "source": "original"}]


def write_ocr_run(root: Path, src, pages: dict[int, dict], *, tag="t1") -> il.OcrRunInputs:
    """A complete OCR v2 run directory for ``pages`` {index: {choice, canon (w, h, rot), prep, raw, pp, tables}}."""
    from PIL import Image  # noqa: F401  (the kit is PIL-based; geometry needs nothing else)

    for d in ("img", f"runs/{tag}/parts", f"runs/{tag}/receipts", f"pp/{tag}", "choice"):
        (root / d).mkdir(parents=True, exist_ok=True)
    pp_rows, prep_rows, choice_rows, manifest = [], [], [], []
    for idx, spec in sorted(pages.items()):
        pid = f"{src.source_id}:p{idx:04d}"
        w, h, rot = spec["canon"]
        parts = []
        for part in spec.get("parts", [{"part": "", "box": [0, 0, *spec["prep"]["page_size_px"]]}]):
            name = part["part"]
            stem = f"{src.source_id}_p{idx:04d}" + (f"_{name}" if name else "")
            iw, ih = part.get("size") or spec["prep"]["page_size_px"]
            sha = hashlib.sha256(stem.encode()).hexdigest()
            q = {"part": name, "part_id": pid + (f"#{name}" if name else ""), "box_in_page": part["box"],
                 "status": "OK", "image": f"png/{stem}.png", "width": iw, "height": ih, "png_sha256": sha,
                 "deskew": part.get("deskew") or {"applied": False, "angle": 0.0}}
            parts.append(q)
            raw = {"schema": "vkm.ocr_v2.part/1", "part_id": q["part_id"], "page_id": pid, "part": name, "status": "OK",
                   "image": {"path": q["image"], "png_sha256": sha, "width": iw, "height": ih},
                   "res": {"input_path": "/".join([FAKE_HOST_ROOT, "img", "png", f"{stem}.png"]), "width": iw,
                           "height": ih,
                           "parsing_res_list": part.get("raw", spec.get("raw", [])),
                           "layout_det_res": {"boxes": [{"label": b["block_label"], "score": 0.9,
                                                         "coordinate": b["block_bbox"]}
                                                        for b in part.get("raw", spec.get("raw", []))]}},
                   "vlm": [], "retries": []}
            (root / f"runs/{tag}/parts/{stem}.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        prep_rows.append({"page_id": pid, "source_id": src.source_id, "page_index": idx, "format": "PDF",
                          "status": "OK", "parts": parts, **spec["prep"]})
        pp_rows.append({"page_id": pid, "parts": [q["part_id"] for q in parts], "verdict": spec.get("verdict", "OK"),
                        "text": "", "blocks": spec.get("pp", []), "tables": spec.get("tables", []),
                        "source_id": src.source_id})
        choice_rows.append({"page_id": pid, "old_layer": "GLM_OCR", "verdict": spec.get("verdict", "OK"),
                            "choice": spec["choice"], "reason": "NEW_NOT_WORSE" if spec["choice"] == "NEW" else
                            "NEW_LOWER_DICT_RATE", "new": {}, "old": {}})
        manifest.append({"page_id": pid, "source_id": src.source_id, "page_index": idx, "format": "PDF",
                         "width_pt": w, "height_pt": h, "rotation_deg": rot, "source_sha256": src.sha256})

    def jsonl(path, rows):
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")

    jsonl(root / f"pp/{tag}/pages.jsonl", pp_rows)
    jsonl(root / "img/prep_manifest.jsonl", prep_rows)
    jsonl(root / "choice/layer_choice.jsonl", choice_rows)
    jsonl(root / "kit_manifest.jsonl", manifest)
    pages_sha = hashlib.sha256((root / f"pp/{tag}/pages.jsonl").read_bytes()).hexdigest()
    (root / "choice/layer_choice_receipt.json").write_text(json.dumps({
        "rule": "CHOICE_V1", "params": {"min_words": 30}, "inputs": {"pages_sha256": pages_sha},
        "summary": {"pages": len(choice_rows)}, "code_sha256": {"choose_layer.py": "0" * 64}}), encoding="utf-8")
    (root / f"pp/{tag}/postprocess_receipt.json").write_text(json.dumps({
        "status": "AUTO_EXTRACTED_UNREVIEWED", "code_sha256": {"postprocess.py": "1" * 64}, "render_ctx": {},
        "verdicts": {"OK": len(pp_rows)}, "versions": {"pymorphy3": "2.0.6"}}), encoding="utf-8")
    (root / "img/prep_receipt.json").write_text(json.dumps({"config": {"deskew": True}, "n_pages": len(prep_rows),
                                                            "out": FAKE_HOST_ROOT + "/img"}), encoding="utf-8")
    receipt = {"run_tag": tag, "status": "COMPLETED", "config_sha256": "2" * 64, "shard": "0/1",
               "config": {"server_url": "http://127.0.0.1:8118/v1", "predict": {"temperature": 0.0},
                          "retry": {"enabled": True}, "run": {"max_attempts": 2}},
               "server": {"models": {"data": [{"id": "PaddleOCR-VL-1.6-0.9B", "max_model_len": 16384}]}},
               "pipeline": {"init_kwargs": {"pipeline_version": "v1.6", "vl_rec_server_url": "http://127.0.0.1:8118/v1",
                                            "use_doc_unwarping": False}, "paddlex_config_overrides": {}},
               "predict_kwargs": {"temperature": 0.0, "max_new_tokens": 10000}, "code_sha256": {"run.py": "3" * 64},
               "versions": {"paddleocr": "3.7.0", "paddlex": "3.7.2", "paddle": "3.4.0", "python": "3.12.14"},
               "units_todo": len(prep_rows), "done": len(prep_rows), "failed": 0, "finished_at": "2026-10-06T09:40:49Z"}
    (root / f"runs/{tag}/receipts/run_shard-0.json").write_text(json.dumps(receipt), encoding="utf-8")
    return il.OcrRunInputs(ocr_root=root, run_tag=tag, kit_manifest=root / "kit_manifest.jsonl")


def write_pins(root: Path) -> Path:
    pins = {"schema": "vkm.model_pins/1", "models": [
        {"key": "paddleocr-vl-1.6", "repo_id": il.RECOGNITION_MODEL.model_id,
         "revision": il.RECOGNITION_MODEL.model_revision, "served_by": f"vllm image {il.SERVER_IMAGE_DIGEST}",
         "weights_sha256": {"model.safetensors": "4" * 64}},
        {"key": "pp-doclayout-v3-paddle", "repo_id": il.LAYOUT_MODEL.model_id,
         "revision": il.LAYOUT_MODEL.model_revision,
         "weights_sha256": {"inference.pdiparams": "5" * 64}}]}
    (root / "infra/models").mkdir(parents=True, exist_ok=True)
    (root / il.PINS_RELPATH).write_text(json.dumps(pins), encoding="utf-8")
    return root


def _rotated_spec():
    """/Rotate 90 page (canon 842 x 595 pt): rendered upright at 300 dpi, deskewed by 1.0 degree."""
    w, h = 3508, 2479
    _, size = il.pil_rotate_matrix(w, h, -1.0)
    raw = [_raw_block(0, "text", (400, 300, 3000, 700), "Текст повернутой страницы.")]
    return {"choice": "NEW", "canon": (842.0, 595.0, 90),
            "prep": {"method": "pdfium_render", "page_size_pt": [595.0, 842.0], "page_size_px": [w, h]},
            "parts": [{"part": "", "box": [0, 0, w, h], "size": list(size), "raw": raw,
                       "deskew": {"applied": True, "angle": 1.0, "size_before": [w, h], "size_after": list(size)}}],
            "pp": [_pp_item("", 0, "text", raw[0]["block_bbox"], "text", "Текст повернутой страницы.")]}


def test_page_objects_spread_overlap_dedup_and_order():
    # spread 2000 px wide split at 1000 with a 20 px overlap: a = [0, 1020), b = [980, 2000)
    raw_a = [_raw_block(0, "text", (50, 100, 900, 300), "Левая страница."),
             _raw_block(1, "number", (985, 1500, 1015, 1530), "7")]
    raw_b = [_raw_block(0, "number", (5, 1500, 35, 1530), "7"),
             _raw_block(1, "text", (100, 100, 950, 300), "Правая страница.")]
    rec = {"schema": il.RECORD_SCHEMA, "geometry": {"bbox_approx": False},
           "prep": {"page_size_px": [2000, 1600], "parts": [
               {"part": "a", "part_id": "X#a", "box_in_page": [0, 0, 1020, 1600], "deskew": {"applied": False}},
               {"part": "b", "part_id": "X#b", "box_in_page": [980, 0, 2000, 1600], "deskew": {"applied": False}}]},
           "postprocess": {"blocks": [_pp_item("a", 0, "text", raw_a[0]["block_bbox"], "text", "Левая страница."),
                                      _pp_item("a", 1, "number", raw_a[1]["block_bbox"], "page_number", "7"),
                                      _pp_item("b", 0, "number", raw_b[0]["block_bbox"], "page_number", "7"),
                                      _pp_item("b", 1, "text", raw_b[1]["block_bbox"], "text", "Правая страница.")]},
           "parts": [{"part": "a", "raw": {"res": {"parsing_res_list": raw_a}}},
                     {"part": "b", "raw": {"res": {"parsing_res_list": raw_b}}}]}
    pi = il.page_objects(rec, 1000.0, 800.0)
    assert [o.text for o in pi.objects] == ["Левая страница.", "7", "Правая страница."]
    assert [o.order for o in pi.objects] == [1, 2, 3]
    assert [(s.locator, s.reason, s.duplicate_of) for s in pi.suppressed] == [
        ("/parts/1/raw/res/parsing_res_list/0", "SPREAD_OVERLAP_DUPLICATE", "/parts/0/raw/res/parsing_res_list/1")]
    right = pi.objects[2]
    assert right.bbox == (540.0, 50.0, 965.0, 150.0)          # (980 + 100) / 2 ... page px → pt (scale 0.5)
    with pytest.raises(il.ImportRefused, match="POSTPROCESS_RAW_MISMATCH"):
        broken = json.loads(json.dumps(rec))
        broken["postprocess"]["blocks"].pop()
        il.page_objects(broken, 1000.0, 800.0)


# ============================================================================================ end to end
def _rows(cfg, dataset, sid):
    from test_pipeline_e2e import _rows as rows

    return rows(cfg, dataset, sid)


def test_imported_layer_becomes_primary_end_to_end(env, tmp_path):
    from test_pipeline_e2e import RUN, _run_all
    from vkm_corpus.pipeline import commit as cm
    from vkm_corpus.pipeline.context import open_cache, open_store
    from vkm_corpus.pipeline.sources import load_sources

    _run_all(env, RUN)
    src = {s.source_id: s for s in load_sources(env.resources_root)}
    s901, s902 = src["VKM-SRC-901"], src["VKM-SRC-902"]
    base_blocks = {b["object_id"]: b for b in _rows(env, "blocks", "VKM-SRC-901")}
    base_tables = {t["object_id"] for t in _rows(env, "tables", "VKM-SRC-901")}
    assert all(t["is_primary_layer"] is None for t in _rows(env, "tables", "VKM-SRC-901"))   # no choice recorded

    def signatures():
        cache = open_cache(env, "RUN-20261006T000000Z-0000aaaa", "sig")
        out = {}
        for s in (s901, s902):
            prep, visual = cm.load_state(env, s)
            out[s.source_id] = cm.commit_signature(env, cache, s, prep, visual)
        return out

    before = signatures()
    raw3, pp3, tables3 = page3_blocks()
    pages = {
        3: {"choice": "NEW", "canon": (595.0, 842.0, 0), "raw": raw3, "pp": pp3, "tables": tables3,
            "prep": {"method": "pdfimages_original_raster", "page_size_pt": [595.0, 842.0],
                     "page_size_px": [1240, 1754],
                     "pdfimages_rows": [{"type": "image", "w": 1240, "h": 1754, "xppi": 150.0, "yppi": 150.0}]}},
        6: _rotated_spec(),
        # a figure-only page: no imported text block, yet the page's primary layer is the imported one
        4: {"choice": "NEW", "canon": (595.0, 842.0, 0), "raw": [_raw_block(0, "image", (100, 100, 2000, 3000))],
            "pp": [_pp_item("", 0, "image", (100, 100, 2000, 3000), "figure")],
            "prep": {"method": "pdfium_render", "page_size_pt": [595.0, 842.0], "page_size_px": [2479, 3508]}},
        1: {"choice": "OLD", "canon": (595.0, 842.0, 0), "raw": [], "pp": [],
            "prep": {"method": "pdfium_render", "page_size_pt": [595.0, 842.0], "page_size_px": [2479, 3508]}},
        2: {"choice": "NEW", "canon": (595.0, 842.0, 0), "raw": [], "pp": [],    # pdfium says 600 pt wide: refused
            "prep": {"method": "pdfium_render", "page_size_pt": [600.0, 842.0], "page_size_px": [2479, 3508]}},
    }
    inputs = write_ocr_run(tmp_path / "ocr", s901, pages)
    run2 = "RUN-20261006T010000Z-0000bbbb"
    store, cache = open_store(env, run2, "imp"), open_cache(env, run2, "imp")
    receipt = il.import_layer(env, store, cache, [s901, s902], inputs, repo_root=write_pins(tmp_path / "repo"),
                              run_id=run2)
    assert receipt["totals"]["pages_new"] == 4 and receipt["totals"]["pages_imported"] == 3
    assert [r["code"] for r in receipt["sources"]["VKM-SRC-901"]["refused"]] == ["PAGE_SIZE_MISMATCH"]
    # idempotent: a second import of the same inputs adds nothing
    again = il.import_layer(env, store, cache, [s901], inputs, repo_root=tmp_path / "repo", run_id=run2)
    assert again["totals"]["pages_cached"] == 3 and again["totals"]["pages_imported"] == 0
    # the raw record keeps the PaddleX JSON but no machine path
    entry = il.load_source_import(cache, "VKM-SRC-901", s901.sha256)
    record = store.read_json(entry.pages[3]["raw_artifact_id"])
    assert record["parts"][0]["raw"]["res"]["input_path"] == "OCRV2_IMG:png/VKM-SRC-901_p0003.png"
    assert "/home/" not in json.dumps(record)
    store.close()
    cache.close()

    after = signatures()
    assert after["VKM-SRC-902"] == before["VKM-SRC-902"]          # no import: the signature is untouched
    assert after["VKM-SRC-901"] != before["VKM-SRC-901"]

    run3 = "RUN-20261006T020000Z-0000cccc"
    store, cache = open_store(env, run3, "c"), open_cache(env, run3, "c")
    res = cm.commit_source_once(env, run3, s901, store=store, cache=cache, part_base=901)
    store.close()
    cache.close()
    assert not res["noop"] and res["accounting_state"] == "ACCOUNTED", res
    report = json.loads((Path(env.data_root) / res["accounting_receipt"]["path"]).read_text(encoding="utf-8"))
    report = json.loads((Path(env.data_root) / report["report"]["path"]).read_text(encoding="utf-8"))
    assert report["gaps"] == [] and report["unmatched_output_ids"] == []
    states = {o["locator"]: o["state"] for o in report["occurrences"] if o["details"].get("layer") == "PADDLEOCR_VL"}
    assert states["/parts/0/raw/res/parsing_res_list/7"] == "SUPPRESSED"          # figure: pipeline layout
    assert states["/parts/0/raw/res/parsing_res_list/8"] == "SUPPRESSED"          # merged follower
    assert states["/parts/0/raw/res/parsing_res_list/2"] is None                  # extracted (disposition later)

    pages_rows = {p["page_index"]: p for p in _rows(env, "pages", "VKM-SRC-901")}
    p3 = pages_rows[3]
    assert (p3["primary_text_layer"], p3["primary_text_origin"], p3["page_status"], p3["origin"]) == (
        "PADDLEOCR_VL", "OCR", "OCR_OK", "OCR")
    assert p3["model_id"] == il.RECOGNITION_MODEL.model_id
    assert p3["ocr_raw_artifact_id"] == entry.pages[3]["raw_artifact_id"]
    # page text from the derived rule: primary blocks, header/page number excluded, markdown removed
    assert p3["normalized_text"] == "Глава 1. Импорт\n\nИмпортированный текст страницы с формулой $x^2$."
    assert p3["printed_page_labels"] == ["12"] and p3["printed_label_origin"] == "RUNNING_HEAD_OCR"
    assert pages_rows[6]["primary_text_layer"] == "PADDLEOCR_VL" and "SKEW_CORRECTED" in pages_rows[6]["quality_flags"]
    p4 = pages_rows[4]
    assert (p4["primary_text_layer"], p4["page_status"], p4["normalized_text"]) == ("PADDLEOCR_VL", "OCR_OK", None)
    assert "EMPTY_PAGE" in p4["quality_flags"] and p4["model_id"] == il.RECOGNITION_MODEL.model_id
    assert pages_rows[1]["primary_text_layer"] == "PDF_TEXT_LAYER"               # OLD: untouched
    assert pages_rows[2]["primary_text_layer"] != "PADDLEOCR_VL"                 # refused: old layer kept

    blocks = _rows(env, "blocks", "VKM-SRC-901")
    imported = [b for b in blocks if b["text_layer"] == "PADDLEOCR_VL"]
    assert {b["page_id"] for b in imported} == {"VKM-SRC-901:p0003", "VKM-SRC-901:p0006"}
    assert all(b["is_primary_layer"] and b["origin"] == "OCR" and b["region_origin"] == "LAYOUT_MODEL" and
               b["review_status"] == "AUTO_EXTRACTED_UNREVIEWED" and b["extractor_id"] == il.EXTRACTOR_ID and
               {m["role"] for m in b["models"]} == {"LAYOUT", "RECOGNITION"} for b in imported)
    p3_types = [b["block_type"] for b in sorted(imported, key=lambda b: b["reading_order"])
                if b["page_id"] == "VKM-SRC-901:p0003"]
    assert p3_types == ["PAGE_HEADER", "HEADING", "TEXT", "PAGE_NUMBER"]
    rot = next(b for b in imported if b["page_id"] == "VKM-SRC-901:p0006")
    assert 0 <= rot["bbox_x0"] < rot["bbox_x1"] <= 842 and 0 <= rot["bbox_y0"] < rot["bbox_y1"] <= 595
    assert "BBOX_APPROX" in rot["quality_flags"]
    old_p3 = [b for b in blocks if b["page_id"] == "VKM-SRC-901:p0003" and b["text_layer"] != "PADDLEOCR_VL"]
    assert old_p3 and not any(b["is_primary_layer"] for b in old_p3)
    # untouched objects keep their ids (secondary blocks are the same objects, only the flag changed)
    assert {b["object_id"] for b in blocks if b["text_layer"] != "PADDLEOCR_VL"} == set(base_blocks)
    tables = _rows(env, "tables", "VKM-SRC-901")
    assert base_tables <= {t["object_id"] for t in tables}
    by_page = {}
    for t in tables:
        by_page.setdefault(t["page_id"], []).append(t)
    p3t = by_page["VKM-SRC-901:p0003"]
    assert sorted((t["text_layer"], t["is_primary_layer"]) for t in p3t) == [("GLM_OCR", False), ("PADDLEOCR_VL", True)]
    new_t = next(t for t in p3t if t["text_layer"] == "PADDLEOCR_VL")
    assert new_t["recognition_method"] == "OCR_PADDLEOCR_VL" and new_t["n_rows"] == 2 and new_t["n_cols"] == 2
    imported_pages = {"VKM-SRC-901:p0003", "VKM-SRC-901:p0004", "VKM-SRC-901:p0006"}
    assert all(t["is_primary_layer"] is None for p, ts in by_page.items() if p not in imported_pages for t in ts)
    assert all(t["is_primary_layer"] is False for t in by_page.get("VKM-SRC-901:p0006", []))   # no imported table
    formulas = [f for f in _rows(env, "formulas", "VKM-SRC-901") if f["page_id"] == "VKM-SRC-901:p0003"]
    assert sorted((f["text_layer"], f["is_primary_layer"]) for f in formulas) == [
        ("GLM_OCR", False), ("PADDLEOCR_VL", True)]
    new_f = next(f for f in formulas if f["text_layer"] == "PADDLEOCR_VL")
    assert (new_f["equation_label"], new_f["normalized_latex"], new_f["formula_kind"]) == ("(1.1)", "E = m c^{2}",
                                                                                         "DISPLAY")
    steps, errs = _journal(env, run3)                 # steps and errors are run-journal partitions
    assert sum(1 for s in steps if s["stage"] == "IMPORTED_LAYER") == 3
    assert [(e["code"], e["page_index"]) for e in errs if e["stage"] == "IMPORTED_LAYER"] == [
        ("IMPORTED_LAYER_REFUSED", 2)]


def _journal(cfg, run_id):
    import pyarrow.parquet as pq

    base = Path(cfg.data_root) / "canonical"
    out = []
    for name in ("processing_steps", "errors"):
        rows = []
        for p in (base / name / f"run={run_id}").glob("**/*.parquet"):
            rows += pq.read_table(p).to_pylist()
        out.append(rows)
    return out


@pytest.fixture()
def env(tmp_path):
    from test_pipeline_e2e import env as e2e_env

    return e2e_env.__wrapped__(tmp_path)


# ============================================================================================ readers
def _duck_with(rows_by_table):
    duckdb = pytest.importorskip("duckdb")
    from vkm_corpus.contracts import arrow as ca

    con = duckdb.connect()
    con.execute("CREATE SCHEMA canonical")
    for name, rows in rows_by_table.items():
        con.register(f"_{name}", ca.rows_to_table(name, rows))
        con.execute(f'CREATE TABLE canonical."{name}" AS SELECT * FROM _{name}')
    return con


def test_wholesale_readers_skip_secondary_tables_and_formulas():
    from vkm_corpus.contracts.primary_layer import is_primary, primary_clause
    from vkm_corpus.navigation import object_duplicates as od
    from vkm_corpus.retrieval_lab.canon import CanonReader
    from vkm_corpus.testing import rows as R

    tables = [R.make_table(page_index=1, is_primary_layer=None), R.make_table(page_index=2, is_primary_layer=True),
              R.make_table(page_index=3, is_primary_layer=False)]
    formulas = [R.make_formula(page_index=1, is_primary_layer=False), R.make_formula(page_index=2)]
    con = _duck_with({"tables": tables, "formulas": formulas, "pages": [], "blocks": []})
    reader = CanonReader(con, "t")
    assert {t["page_id"][-5:] for t in reader.tables()} == {"p0001", "p0002"}
    assert {f["page_id"][-5:] for f in reader.formulas()} == {"p0002"}
    assert len(od.load_tables(con)) == 2 and len(od.load_formulas(con)) == 1
    assert "is_primary_layer" not in od.load_tables(con)[0]
    assert primary_clause(con, "tables", "t") == "t.is_primary_layer IS NOT FALSE"
    con.execute("CREATE TABLE canonical.old_tables (object_id VARCHAR)")
    assert primary_clause(con, "old_tables") == "TRUE"           # projection without the column: no filter
    assert is_primary({}) and is_primary({"is_primary_layer": None}) and not is_primary({"is_primary_layer": False})
    from vkm_corpus.navigation.formulas import _load

    fml, _ = _load(con, None)
    assert len(fml) == 1
    con.close()


def test_rerank_and_object_views_carry_the_flag():
    pytest.importorskip("duckdb")
    from vkm_corpus.testing import rows as R

    tables = [R.make_table(page_index=1, is_primary_layer=False), R.make_table(page_index=2)]
    con = _duck_with({"tables": tables})
    con.execute("CREATE VIEW t AS SELECT object_id, coalesce(is_primary_layer, true) AS p FROM canonical.tables")
    assert sorted(r[0] for r in con.execute("SELECT p FROM t").fetchall()) == [False, True]
    sql = (Path(__file__).resolve().parents[2] / "src/vkm_corpus/duckdb/sql/30_access.sql").read_text(encoding="utf-8")
    assert "NULL::BOOLEAN AS is_primary_layer,\n         pipeline_version, processing_run_id, extractor_id, " \
           "extractor_version, extraction_generation, model_id,\n         model_revision, models, config_hash, " \
           "raw_config_hash, extraction_signature, raw_content_sha256,\n         content_sha256, raw_artifact_id, " \
           "raw_artifacts, created_at, review_status, quality_flags, schema_version,\n         bbox_x0, bbox_y0, " \
           "bbox_x1, bbox_y1, bbox_space\n  FROM canonical.\"tables\"" not in sql
    assert sql.count("coalesce(is_primary_layer, true)") == 2
    con.close()


# ============================================================================================ historical schemas
@pytest.mark.parametrize("name,factory", [("tables", "make_table"), ("formulas", "make_formula")])
def test_pre_primary_flag_files_stay_readable_with_original_hashes(tmp_path, name, factory):
    pytest.importorskip("duckdb")
    from test_locator_schema_compat import _old_file
    from vkm_corpus.contracts import arrow as ca
    from vkm_corpus.contracts.datasets import HISTORICAL_PRIMARY_FLAG_SCHEMAS
    from vkm_corpus.duckdb.build import attach_manifest, verify_fingerprints
    from vkm_corpus.parquet.layout import init_root
    from vkm_corpus.testing import rows as R
    import duckdb

    layout = init_root(tmp_path / "canon", "CANONICAL")
    historical = HISTORICAL_PRIMARY_FLAG_SCHEMAS[name]
    entry, digest, table_fp = _old_file(layout, name, getattr(R, factory)(raw_locator="/synthetic/x[1]"), historical)
    manifest = {"datasets": {name: {"files": [entry], "schema_version": historical[0],
                                    "schema_fingerprint": historical[1], "table_fingerprint": table_fp}}}
    con = duckdb.connect()
    attach_manifest(con, layout, manifest)
    assert con.execute(f'SELECT is_primary_layer FROM canonical."{name}"').fetchone() == (None,)
    assert verify_fingerprints(con, manifest) == {}
    row = con.execute(f'SELECT * FROM canonical."{name}"').to_arrow_table().to_pylist()[0]
    assert ca.digest_rows(name, [row]) == digest
    row["is_primary_layer"] = False
    with pytest.raises(ValueError, match="historical"):
        ca.digest_rows(name, [row])
    assert ca.readable_schema(name, *historical)
    con.close()


@pytest.mark.parametrize("name,factory,which", [("tables", "make_table", "HISTORICAL_LOCATOR_SCHEMAS"),
                                                ("tables", "make_table", "HISTORICAL_PRIMARY_FLAG_SCHEMAS"),
                                                ("formulas", "make_formula", "HISTORICAL_PRIMARY_FLAG_SCHEMAS")])
def test_publication_accepts_unchanged_historical_partitions_only_exactly(tmp_path, name, factory, which):
    """A base source that is not re-extracted keeps its immutable partitions (tables 0.1.0 … 0.1.2): the publication
    closure must accept exactly the allowlisted historical column sets and nothing else."""
    pq = pytest.importorskip("pyarrow.parquet")
    pytest.importorskip("duckdb")
    from test_locator_schema_compat import _old_file
    from vkm_corpus.contracts import datasets as D
    from vkm_corpus.contracts.access import AccessContext
    from vkm_corpus.coverage import publication as P
    from vkm_corpus.parquet.layout import init_root
    from vkm_corpus.testing import rows as R

    layout = init_root(tmp_path / "canon", "CANONICAL")
    entry, _, _ = _old_file(layout, name, getattr(R, factory)(), getattr(D, which)[name])
    path = layout.root / "canonical" / entry["path"]
    policies = {"VKM-SRC-001": {"access_class": "PRIVATE_CLOUD_ALLOWED", "experimental_role": "INPUT",
                                "policy_version": "t1", "authority": "test"}}
    context = AccessContext(principal="t", execution="LOCAL", granted_classes={"PRIVATE_CLOUD_ALLOWED"})
    P._authorize_partition(path, name, policies, context)               # exact historical schema: accepted
    table = pq.read_table(path)
    forged = table.append_column("extra_column", table.column(0))      # same metadata, other column set
    pq.write_table(forged.replace_schema_metadata(table.schema.metadata), path)
    with pytest.raises(P.PublicationBlocked, match="SCHEMA_INVALID"):
        P._authorize_partition(path, name, policies, context)


def test_bibliography_entry_continued_on_another_text_layer_gets_its_own_id(monkeypatch):
    """B07: an entry whose first fragment stays but whose continuation page switched to the imported layer must not
    keep its id with other content; single-layer entries keep their ids."""
    from datetime import datetime, timezone

    from vkm_corpus.extract import bibliography as bib
    from vkm_corpus.extract import to_canon
    from vkm_corpus.extract.model import SourceInput, SourceResult
    from vkm_corpus.testing import rows as R

    first = R.make_block(page_index=1, order=1, text="12. Иванов И. И. Синтетическая статья",
                         block_type="REFERENCE_LIST")

    def entry_id(cont_layer: str) -> str:
        cont = R.make_block(page_index=2, order=1, text="// Вестник синтетики. 2020. С. 1–2.",
                            block_type="REFERENCE_LIST").model_copy(update={"text_layer": cont_layer})
        text = f"{first.text} {cont.text}"
        entry = bib.Entry(label="12", ordinal=1, blocks=[first, cont], text=text, normalized_text=text,
                          continues_on_page_id=cont.page_id, parsed=bib.parse_entry(text), numbered=True)
        monkeypatch.setattr(bib, "extract_entries", lambda rows: ([entry], None))
        src = SourceInput("VKM-SRC-001", "x.pdf", R.SOURCE_SHA, 1, evidence_scope="VKM_regional")
        mapper = to_canon.CanonMapper(SourceResult(source=src),
                                      run_id=R.RUN_ID, config_hashes={}, created_at=datetime.now(timezone.utc))
        (row,) = mapper.bibliography([first, cont])
        return row.object_id

    same = entry_id("PDF_TEXT_LAYER")
    assert same == entry_id("PDF_TEXT_LAYER")                         # stable for an unchanged single layer
    assert entry_id("PADDLEOCR_VL") != same                           # continuation now on the imported layer

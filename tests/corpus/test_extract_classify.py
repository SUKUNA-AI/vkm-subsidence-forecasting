"""Page classification rule c1, routes, document classes and embedded-layer quality (agent C; pure Python)."""
from __future__ import annotations

from vkm_corpus.extract import classify as c


def feats(**kw):
    base = {"chars": 1000, "cyr": 800, "lat": 50, "digits": 50, "punct": 80, "latext": 0, "greek": 0, "ctrl": 0,
            "repl": 0, "pua": 0, "other": 20, "chars_invisible": 0, "word_tokens": 150, "func_hits": 30,
            "img_cov": 0.0, "n_images": 0, "n_paths": 10}
    base.update(kw)
    return base


def test_golden_table():
    assert c.classify_pdf_page(feats()) == ("NATIVE_TEXT", [])
    assert c.classify_pdf_page(feats(chars=0, img_cov=0.99, n_images=1))[0] == "RASTER_SCAN"
    assert c.classify_pdf_page(feats(chars=5, n_paths=900))[1][-1] == "VECTOR_NO_TEXT"
    assert c.classify_pdf_page(feats(chars=0, img_cov=0.2, n_images=1))[1][-1] == "IMAGE_NO_TEXT"
    assert c.classify_pdf_page(feats(chars=0))[0] == "EMPTY"
    assert c.classify_pdf_page(feats(repl=300)) == ("BROKEN_TEXT_LAYER", ["GARBAGE_GLYPHS"])
    scan = c.classify_pdf_page(feats(img_cov=0.95, n_images=1, chars_invisible=1000))
    assert scan[0] == "RASTER_SCAN" and "HIDDEN_TEXT_LAYER" in scan[1] and "EMBEDDED_TEXT_LAYER" in scan[1]
    visible = c.classify_pdf_page(feats(img_cov=0.95, n_images=1))
    assert "VISIBLE_TEXT_OVER_IMAGE" in visible[1]
    assert c.classify_pdf_page(feats(img_cov=0.4, n_images=2))[0] == "MIXED"
    assert c.classify_pdf_page(feats(n_paths=800))[0] == "VECTOR"
    moj = c.classify_pdf_page(feats(cyr=0, latext=800, remap_cyr_share=0.9, remap_words=100, remap_func_rate=0.2))
    assert "REPAIRABLE_CP1251_REMAP" in moj[1]
    bad = c.classify_pdf_page(feats(cyr=0, latext=800, remap_cyr_share=0.1, remap_words=100, remap_func_rate=0.0))
    assert bad == ("BROKEN_TEXT_LAYER", ["MOJIBAKE_NOT_REPAIRABLE"])
    assert "LOW_FUNCTION_WORD_RATE" in c.classify_pdf_page(feats(func_hits=0))[1]


def test_routes():
    assert c.route_for("RASTER_SCAN", ["EMBEDDED_TEXT_LAYER"]) == ("OCR_OPTIONAL", "EMBEDDED_OCR_LAYER")
    assert c.route_for("RASTER_SCAN", ["NO_TEXT_LAYER"])[0] == "OCR_REQUIRED"
    assert c.route_for("BROKEN_TEXT_LAYER", ["GARBAGE_GLYPHS"]) == ("OCR_REQUIRED", "BROKEN_GARBAGE_GLYPHS")
    assert c.route_for("VECTOR", ["VECTOR_NO_TEXT"])[0] == "OCR_REQUIRED"
    assert c.route_for("NATIVE_TEXT", ["REPAIRABLE_CP1251_REMAP"])[0] == "NATIVE_REPAIR"
    assert c.route_for("EMPTY", [])[0] == "EMPTY"
    assert c.route_for("MIXED", [])[0] == "NATIVE"


def test_djvu_layer_quality():
    assert c.classify_djvu_page(False, None) == ("RASTER_SCAN", ["DJVU", "NO_TEXT_LAYER"])
    ok = c.classify_djvu_page(True, "Синтетический текст слоя DjVu с достаточным числом букв для проверки")
    assert ok[0] == "RASTER_SCAN" and "EMBEDDED_TEXT_LAYER" in ok[1]
    assert c.classify_djvu_page(True, "�" * 50)[0] == "BROKEN_TEXT_LAYER"
    assert "EMPTY_TEXT_CHUNK" in c.classify_djvu_page(True, "  ")[1]


def test_document_classes_use_contract_values():
    from vkm_corpus.contracts.vocab import DocumentClass

    allowed = {d.value for d in DocumentClass}
    cases = [([("NATIVE_TEXT", [])] * 10, "PDF"), ([("RASTER_SCAN", ["NO_TEXT_LAYER"])] * 10, "DJVU"),
             ([("RASTER_SCAN", ["EMBEDDED_TEXT_LAYER"])] * 10, "PDF"), ([("BROKEN_TEXT_LAYER", [])] * 10, "PDF"),
             ([], "EPUB"), ([], "DOCX"), ([("RASTER_SCAN", ["EMBEDDED_TEXT_LAYER"])] * 5 +
                                          [("RASTER_SCAN", ["NO_TEXT_LAYER"])] * 5, "PDF")]
    got = [c.document_class(p, f) for p, f in cases]
    assert got == ["NATIVE", "SCANNED_NO_TEXT", "SCANNED_WITH_TEXT_LAYER", "BROKEN_TEXT_LAYER", "REFLOWABLE_EPUB",
                   "WORD_DOCX", "SCANNED_PARTIAL_TEXT_LAYER"]
    assert set(got) <= allowed


def test_thresholds_enter_the_config():
    cfg = c.DEFAULT_THRESHOLDS.as_config()
    assert cfg["classifier_version"] == c.CLASSIFIER_VERSION and cfg["min_text"] == 20

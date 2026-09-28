"""Native PDF extraction on synthetic PDFs (agent C): pagination by two methods, classification, geometry, vectors,
images, BOM before the header, page labels, CP1251 repair, Phase-1-compatible plain-text hash."""
from __future__ import annotations

import hashlib

import pytest

pymupdf = pytest.importorskip("pymupdf")
pytest.importorskip("pypdfium2")

from vkm_corpus.extract import classify, pdf_native  # noqa: E402
from vkm_corpus.extract.detect import inspect_file, sniff  # noqa: E402
from vkm_corpus.extract.pages import paginate_pdf  # noqa: E402
from vkm_corpus.extract.synthetic import make_pdf  # noqa: E402
from vkm_corpus.extract.text_repair import remap_cp1251, repair_plausible  # noqa: E402


@pytest.fixture(scope="module")
def pdf(tmp_path_factory):
    return make_pdf(tmp_path_factory.mktemp("pdf") / "synthetic.pdf")


def _classes(path):
    doc = pdf_native.open_pdf(path)
    out = []
    try:
        for i in range(doc.page_count):
            np_ = pdf_native.extract_page(doc, i, {})
            c, flags = classify.classify_pdf_page(np_.features)
            out.append((c, flags, np_))
    finally:
        doc.close()
    return out


def test_two_page_counters_agree(pdf):
    p = paginate_pdf(pdf)
    assert p.count == 6 and p.check_count == 6 and p.agree
    assert (p.unit, p.page_kind, p.basis) == ("p", "PDF_PAGE", "PDF_PAGE_TREE")


def test_page_classes_and_routes(pdf):
    got = [(c, classify.route_for(c, f)[0]) for c, f, _ in _classes(pdf)]
    assert got[0] == ("NATIVE_TEXT", "NATIVE")
    assert got[1][0] == "VECTOR" and got[1][1] == "NATIVE"
    assert got[2] == ("RASTER_SCAN", "OCR_REQUIRED")
    assert got[3] == ("EMPTY", "EMPTY")
    assert got[4][1] == "NATIVE_REPAIR"
    assert got[5] == ("NATIVE_TEXT", "NATIVE")


def test_rotated_page_geometry_is_page_pt_tl(pdf):
    *_, (_, _, rotated) = _classes(pdf)
    raw = rotated.raw
    assert raw["rotation"] == 90
    assert raw["width_pt"] == pytest.approx(842) and raw["height_pt"] == pytest.approx(595)
    for b in raw["blocks"]:
        x0, y0, x1, y1 = b["bbox"]
        assert 0 <= x0 < x1 <= 842.5 and 0 <= y0 < y1 <= 595.5


def test_blocks_have_order_bbox_and_plain_text_hash(pdf):
    c, f, np_ = _classes(pdf)[0]
    raw = np_.raw
    assert raw["blocks"], "text blocks expected"
    assert [b["n"] for b in raw["blocks"]] == sorted(b["n"] for b in raw["blocks"])
    assert all(len(b["bbox"]) == 4 for b in raw["blocks"])
    assert pdf_native.block_text(raw["blocks"][0]).strip()
    assert raw["plain_text_sha256"] == hashlib.sha256(np_.plain_text.encode("utf-8")).hexdigest()


def test_images_without_decoding_and_vectors(pdf):
    classes = _classes(pdf)
    raster = classes[2][2].raw
    assert raster["images"] and raster["features"]["img_cov"] >= 0.85
    vector = classes[1][2].raw
    assert vector["features"]["n_paths"] >= 500
    doc = pdf_native.open_pdf(pdf)
    try:
        paths = pdf_native.drawings_json(doc[1])
        assert paths["n_paths"] >= 500 and paths["bbox_space"] == "PAGE_PT_TL"
        assert pdf_native.page_svg(doc[1]).startswith(b"<svg")
    finally:
        doc.close()


def test_embedded_image_stream_for_a_figure_region(tmp_path):
    path = make_pdf(tmp_path / "fig.pdf", pages=("figure",))
    doc = pdf_native.open_pdf(path)
    try:
        raw = pdf_native.extract_page(doc, 0, {}).raw
        emb = pdf_native.embedded_image_for(doc, doc[0], raw, (150, 250, 450, 475))
        assert emb is not None and not emb.get("ambiguous")
        assert emb["bytes"] and emb["width"] == 600 and emb["height"] == 450
        assert emb["transcoded"] in (True, False) and "original_filter" in emb
    finally:
        doc.close()


def test_bom_before_header_is_flagged_not_repaired(tmp_path):
    path = make_pdf(tmp_path / "bom.pdf", pages=("text",), bom=True)
    info = inspect_file(path)
    assert info.file_format == "PDF" and info.leading_bytes == 3
    assert "SIGNATURE_AFTER_BOM" in info.flags
    assert paginate_pdf(path).count == 1


def test_sniff_formats():
    assert sniff(b"%PDF-1.7\n")[0] == "PDF"
    assert sniff(b"AT&TFORM\x00\x00\x00\x10DJVM")[0] == "DJVU"
    assert sniff(b"version https://git-lfs.github.com/spec/v1\n")[2] == "LFS_POINTER"
    assert sniff(b"\x89PNG\r\n\x1a\n")[0] == "IMAGE"
    assert sniff(b"garbage")[0] == "UNKNOWN"


def test_page_labels_are_read(tmp_path):
    path = make_pdf(tmp_path / "labels.pdf", pages=("text", "text"), labels=True)
    doc = pdf_native.open_pdf(path)
    try:
        assert [pdf_native.extract_page(doc, i, {}).raw["label"] for i in range(2)] == ["i", "ii"]
    finally:
        doc.close()


def test_cp1251_repair_roundtrip():
    original = "Оценка несущей способности целиков"
    broken = original.encode("cp1251").decode("latin-1")
    assert remap_cp1251(broken) == original
    ok, stats = repair_plausible(broken)
    assert ok and stats["cyr_share"] > 0.8


def test_broken_text_layer_goes_to_ocr(tmp_path):
    path = make_pdf(tmp_path / "broken.pdf", pages=("broken",))
    (c, flags, np_), = _classes(path)
    assert c == "BROKEN_TEXT_LAYER" and "GARBAGE_GLYPHS" in flags
    assert classify.route_for(c, flags) == ("OCR_REQUIRED", "BROKEN_GARBAGE_GLYPHS")

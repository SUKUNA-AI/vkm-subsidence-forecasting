"""Route A (native PDF vectors) and the single-figure crop on synthetic PDFs made with PyMuPDF; glyph-outline tick
labels read by the local OCR helper when Tesseract is installed."""
from __future__ import annotations

import math
import shutil

import numpy as np
import pytest

pymupdf = pytest.importorskip("pymupdf")

from vkm_corpus.figures import core, labels_ocr, pdf_route  # noqa: E402
from vkm_corpus.figures.crop import crop_figure_pdf  # noqa: E402

# plot area on the page: x 100…400 pt ↔ 0…30, y 300 (0) … 100 (-60)
X0, X1, Y0, Y1 = 100.0, 400.0, 300.0, 100.0


def _xy(x, y):
    return X0 + (X1 - X0) * x / 30.0, Y0 + (Y1 - Y0) * y / -60.0


def _chart_pdf(path, *, offpage_text=True, clipped_series=True, date_glyphs=False):
    doc = pymupdf.open()
    page = doc.new_page(width=500, height=420)
    sh = page.new_shape()
    sh.draw_line((X0, Y0), (X1, Y0))
    sh.draw_line((X0, Y0), (X0, Y1))
    for i in range(4):
        x = X0 + (X1 - X0) * i / 3
        sh.draw_line((x, Y0), (x, Y0 + 4))
    for i in range(4):
        y = Y0 + (Y1 - Y0) * i / 3
        sh.draw_line((X0 - 4, y), (X0, y))
    sh.finish(color=(0, 0, 0), width=0.8)
    sh.commit()
    if not date_glyphs:
        for i, v in enumerate((0, 10, 20, 30)):
            x = X0 + (X1 - X0) * i / 3
            page.insert_text((x - 5, Y0 + 16), str(v), fontsize=9)
    for i, v in enumerate((0, -20, -40, -60)):
        y = Y0 + (Y1 - Y0) * i / 3
        page.insert_text((X0 - 30, y + 3), str(v), fontsize=9)
    series = {(1, 0, 0): [(0, 0), (10, -12), (20, -40), (30, -45)],
              (0, 0, 1): [(0, 0), (10, -5), (20, -18), (30, -22)]}
    for colour, pts in series.items():
        sh = page.new_shape()
        p = [_xy(*q) for q in pts]
        for a, b in zip(p[:-1], p[1:]):
            c1 = (a[0] + (b[0] - a[0]) / 3, a[1] + (b[1] - a[1]) / 3)
            c2 = (a[0] + 2 * (b[0] - a[0]) / 3, a[1] + 2 * (b[1] - a[1]) / 3)
            sh.draw_bezier(a, c1, c2, b)
        sh.finish(color=colour, width=1.2, closePath=False)
        sh.commit()
    if offpage_text:
        page.insert_text((50, -60), "17* 0,00011 offpage", fontsize=9)
    if date_glyphs:
        _glyph_dates(page)
    doc.save(path)
    return series


def _glyph_dates(page):
    """Rotated date labels drawn as filled glyph outlines (no text), as some chart exporters write them."""
    mpl = pytest.importorskip("matplotlib")
    from matplotlib.font_manager import FontProperties
    from matplotlib.textpath import TextPath

    del mpl
    for i, label in enumerate(("04.01.2010", "04.01.2012", "03.01.2014", "02.01.2016")):
        x = X0 + (X1 - X0) * i / 3
        tp = TextPath((0, 0), label, size=9, prop=FontProperties(family="DejaVu Sans"))
        a = math.radians(-45)            # descending to the right from the tick, below the axis
        for poly in tp.to_polygons():
            pts = []
            for px, py in poly:
                rx, ry = px * math.cos(a) - py * math.sin(a), px * math.sin(a) + py * math.cos(a)
                pts.append((x + rx, Y0 + 12 - ry))
            sh = page.new_shape()
            sh.draw_polyline(pts)
            sh.finish(fill=(0, 0, 0), color=None, even_odd=True, closePath=True)
            sh.commit()


def test_route_a_recovers_series(tmp_path):
    pdf = tmp_path / "chart.pdf"
    series = _chart_pdf(pdf)
    page = pymupdf.open(pdf)[0]
    region = (60, 80, 440, 330)
    texts, paths = pdf_route.load_page(page, region)
    res = core.digitize(texts, paths, region, quantum=0.05)
    assert res["axis_status"] == "OK"
    got = {s["color"]: sorted((p["x"], p["y"]) for p in s["points"]) for s in res["series"]}
    assert np.allclose(got["#ff0000"], sorted(series[(1, 0, 0)]), atol=1e-2)
    assert np.allclose(got["#0000ff"], sorted(series[(0, 0, 1)]), atol=1e-2)


def test_crop_sets_cropbox_and_keeps_content(tmp_path):
    pdf = tmp_path / "chart.pdf"
    _chart_pdf(pdf)
    out = tmp_path / "crop.pdf"
    info = crop_figure_pdf(pdf, 1, (80, 90, 420, 330), out, margin=6)
    page = pymupdf.open(out)[0]
    assert info.mode == "cropbox"
    assert abs(page.rect.width - 352) < 1e-6 and abs(page.rect.height - 252) < 1e-6
    assert info.chars_outside_after > 0          # the off-page text is still in the stream …
    words = " ".join(w[4] for w in page.get_text("words"))
    assert "offpage" not in words                 # … but not in the visible text layer used to confirm MTEXT
    # the DXF → page transform maps the CropBox origin to the box corner
    assert info.dxf_to_page(0.0, 0.0) == (74.0, 336.0)


def test_crop_with_text_redaction_removes_offpage_text(tmp_path):
    pdf = tmp_path / "chart.pdf"
    _chart_pdf(pdf)
    info = crop_figure_pdf(pdf, 1, (80, 90, 420, 330), tmp_path / "crop.pdf", margin=6, redact_text=True)
    assert info.mode == "redact_text+cropbox" and info.chars_outside_after == 0


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="local OCR helper (tesseract) not installed")
def test_glyph_outline_date_labels_are_read(tmp_path):
    pdf = tmp_path / "chart.pdf"
    _chart_pdf(pdf, date_glyphs=True, offpage_text=False)
    page = pymupdf.open(pdf)[0]
    region = (60, 60, 460, 400)
    texts, paths = pdf_route.load_page(page, region)
    eng = labels_ocr.OcrEngine()
    reader = labels_ocr.make_glyph_reader(page, lambda X, Y: (X, Y), eng, max_size=18.0, default_h=6.0,
                                          y_down=True)
    res = core.digitize(texts, paths, region, quantum=0.05, glyph_reader=reader)
    assert res["x_axis"] is not None and res["x_axis"].kind == "DATE"
    assert res["x_axis"].label_source == "LOCAL_OCR" and res["x_axis"].ocr["engine"] == "tesseract"
    read = sorted(lab.text for lab in reader.labels)
    assert "04.01.2010" in read and "02.01.2016" in read

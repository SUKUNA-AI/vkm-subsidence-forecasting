"""Route R on a synthetic raster chart (OpenCV drawing): axes, ticks, two coloured curves, a benchmark-marker axis.
Tick labels are handed in as corpus text, so no OCR is needed; the OCR blob reader runs only with Tesseract."""
from __future__ import annotations

import shutil

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from vkm_corpus.figures import raster as R  # noqa: E402
from vkm_corpus.figures.labels_ocr import OcrEngine  # noqa: E402
from vkm_corpus.figures.primitives import Text  # noqa: E402
from vkm_corpus.figures.raster_digitize import digitize_raster  # noqa: E402

W, H = 900, 600
PX0, PX1, PY0, PY1 = 100, 800, 500, 100          # plot: x 0…70 over 100…800 px, y 0…-40 over 500…100 px


def _px(x, y):
    return PX0 + (PX1 - PX0) * x / 70.0, PY0 + (PY1 - PY0) * y / -40.0


def _image():
    img = np.full((H, W, 3), 250, np.uint8)
    cv2.line(img, (PX0, PY0), (PX1, PY0), (0, 0, 0), 2)
    cv2.line(img, (PX0, PY0), (PX0, PY1), (0, 0, 0), 2)
    for i in range(8):
        x = int(PX0 + (PX1 - PX0) * i / 7)
        cv2.line(img, (x, PY0), (x, PY0 + 8), (0, 0, 0), 2)
    for i in range(5):
        y = int(PY0 + (PY1 - PY0) * i / 4)
        cv2.line(img, (PX0 - 8, y), (PX0, y), (0, 0, 0), 2)
    curves = {(220, 40, 40): [(0, -2), (10, -5), (20, -15), (30, -30), (40, -32), (50, -20), (60, -10), (70, -6)],
              (40, 60, 200): [(0, -1), (10, -2), (20, -6), (30, -12), (40, -14), (50, -9), (60, -4), (70, -3)]}
    for colour, pts in curves.items():
        p = np.array([_px(x, y) for x, y in pts], np.int32)
        cv2.polylines(img, [p], False, colour, 3, lineType=cv2.LINE_AA)
    words = [Text(str(v), PX0 + (PX1 - PX0) * i / 7 - 8, PX0 + (PX1 - PX0) * i / 7 + 8, PY0 + 25, 14.0,
                  source="CORPUS_OCR") for i, v in enumerate(range(0, 80, 10))]
    words += [Text(str(v), PX0 - 55, PX0 - 15, PY0 + (PY1 - PY0) * i / 4, 14.0, source="CORPUS_OCR")
              for i, v in enumerate((0, -10, -20, -30, -40))]
    return img, curves, words


def test_raster_route_recovers_curves_with_corpus_labels():
    img, curves, words = _image()
    fig = R.RasterFigure(img, 1.0, (0.0, 0.0), native_px_per_pt=1.0)
    res = digitize_raster(fig, OcrEngine(exe="/nonexistent"), corpus_words=words, sample_markers=False)
    assert res["axis_status"] == "OK" and res["label_source"] == "CORPUS_TEXT"
    assert len(res["series"]) == 2
    for s in res["series"]:
        xs = np.array([p["x"] for p in s["points"]])
        ys = np.array([p["y"] for p in s["points"]])
        colour = min(curves, key=lambda c: np.abs(np.array(c) - np.array(
            [int(s["color"][1:3], 16), int(s["color"][3:5], 16), int(s["color"][5:7], 16)])).sum())
        truth = np.array(curves[colour])
        want = np.interp(xs, truth[:, 0], truth[:, 1])
        assert np.median(np.abs(ys - want)) < 0.3 and np.max(np.abs(ys - want)) < 1.0
        assert all(p["y_err"] > 0 for p in s["points"])
        assert "RASTER" in s["flags"]
    assert res["qc"]["ink_recall"] > 0.9


def test_marker_axis_counts_benchmarks_despite_irregular_spacing():
    markers = [10, 30, 45, 70, 80, 105, 120, 150]            # benchmarks 46…53 at their chainage
    words = [Text("46", 5, 15, 0, 8), Text("48", 40, 50, 0, 8), Text("50", 75, 85, 0, 8), Text("52", 115, 125, 0, 8)]
    ax = R.marker_axis("x", markers, words)
    assert ax is not None and ax.kind == "MARKERS"
    assert list(ax.values) == [46, 47, 48, 49, 50, 51, 52, 53]
    assert float(ax.value(45)) == 48.0


def test_piecewise_axis_drops_a_misread_label():
    rows = [["46", 46.0, 10.0, False, "LOCAL_OCR"], ["48", 48.0, 30.0, False, "LOCAL_OCR"],
            ["5", 5.0, 50.0, False, "LOCAL_OCR"], ["52", 52.0, 70.0, False, "LOCAL_OCR"],
            ["54", 54.0, 95.0, False, "LOCAL_OCR"]]
    ax = R._piecewise("x", rows)
    assert ax is not None and ax.dropped == ["5"] and float(ax.value(30.0)) == 48.0


def test_polyline_vertices_of_a_line_chart():
    xs = np.arange(0, 301, dtype=float)
    corners = [(0, 50.0), (100, 10.0), (200, 80.0), (300, 40.0)]
    ys = np.interp(xs, [c[0] for c in corners], [c[1] for c in corners])
    idx = R.polyline_vertices(xs, ys, 2.0, 300.0)
    assert idx is not None and list(xs[idx]) == [0, 100, 200, 300]


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="local OCR helper (tesseract) not installed")
def test_ocr_blobs_reads_crowded_labels():
    img = np.full((60, 400), 255, np.uint8)
    for i, t in enumerate(("46", "48", "50", "0.3", "52")):
        cv2.putText(img, t, (10 + 75 * i, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.9, 0, 2, cv2.LINE_AA)
    out = R.ocr_blobs(img, (0, 0), OcrEngine(), "0123456789.,-")
    assert [t.text for t in sorted(out, key=lambda t: t.x0)] == ["46", "48", "50", "0.3", "52"]

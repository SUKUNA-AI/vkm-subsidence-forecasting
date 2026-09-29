"""Figure digitization core (agent FD): label parsing, axis calibration, curve chaining and extraction on synthetic
vector plots. No corpus data."""
from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from vkm_corpus.figures import core
from vkm_corpus.figures.calibrate import detect_axes, fit_axis, plot_box, structure_lines
from vkm_corpus.figures.primitives import (Path, Text, decimal_year, from_decimal_year, label_value, parse_date,
                                           parse_number)
from vkm_corpus.figures.series import chain, extract_series, legend_text_ids


# ------------------------------------------------------------------------------------------------ parsing
@pytest.mark.parametrize("s,v", [("0", 0.0), ("–60", -60.0), ("−1 150", -1150.0), ("0,5", 0.5), ("1.25", 1.25),
                                 ("-200", -200.0), ("2014 ", 2014.0), ("12%", 12.0)])
def test_parse_number(s, v):
    assert parse_number(s) == pytest.approx(v)


@pytest.mark.parametrize("s", ["Rp2.2", "мм", "04.01.2010", "187 188 189", ""])
def test_parse_number_rejects(s):
    assert parse_number(s) is None


def test_parse_date_and_decimal_year_roundtrip():
    assert parse_date("04.01.2010") == dt.date(2010, 1, 4)
    assert parse_date("26/10/22") == dt.date(2022, 10, 26)
    assert parse_date("12.2013") == dt.date(2013, 12, 1)
    assert parse_date("31.02.2010") is None
    d = dt.date(2019, 10, 17)
    assert from_decimal_year(decimal_year(d)) == d
    v, k = label_value("15.07.1990")
    assert k == "DATE" and int(v) == 1990


# ------------------------------------------------------------------------------------------------ helpers
def _label(text, xc, yc, h=0.08):
    w = 0.06 * len(text)
    return Text(text, xc - w / 2, xc + w / 2, yc, h)


def _chart(y_up: bool = True):
    """A 4 x 3 unit plot: x 0…100 over 0…4, y 0…-60 over 3…0 (y up) with ticks, two series and a legend."""
    sy = 1 if y_up else -1
    texts = [_label(str(v), v / 25.0, (-0.15) * sy) for v in (0, 25, 50, 75, 100)]
    texts += [Text(str(v), -0.45, -0.1, (3 + v / 20.0) * sy, 0.08) for v in (0, -20, -40, -60)]
    paths = [Path(np.array([[0, 0], [4, 0]]), None, 0.01, "LINE"),
             Path(np.array([[0, 0], [0, 3 * sy]]), None, 0.01, "LINE")]
    for v in (0, 25, 50, 75, 100):
        paths.append(Path(np.array([[v / 25.0, 0], [v / 25.0, -0.05 * sy]]), None, 0.01, "LINE"))
    for v in (0, -20, -40, -60):
        y = (3 + v / 20.0) * sy
        paths.append(Path(np.array([[-0.05, y], [0, y]]), None, 0.01, "LINE"))
    truth = {"A": [(0, 0.0), (25, -10.0), (50, -30.0), (75, -35.0), (100, -50.0)],
             "B": [(0, 0.0), (25, -5.0), (50, -12.5), (75, -20.0), (100, -22.5)]}
    colours = {"A": 0xFF0000, "B": 0x0000FF}
    for name, pts in truth.items():
        xy = np.array([[x / 25.0, (3 + y / 20.0) * sy] for x, y in pts])
        # pieces as a PDF import writes them: one Bezier piece per segment, some reversed, shuffled
        pieces = [xy[i:i + 2] if i % 2 else xy[i:i + 2][::-1] for i in range(len(xy) - 1)]
        for k in (2, 0, 3, 1):
            paths.append(Path(pieces[k], colours[name], 0.02, "BEZ"))
    # legend outside the plot: sample stroke + label
    for i, name in enumerate(("A", "B")):
        y = (-0.6 - 0.2 * i) * sy
        paths.append(Path(np.array([[1.0, y], [1.3, y]]), colours[name], 0.02, "LINE"))
        texts.append(Text(name, 1.35, 1.45, y, 0.08))
    return texts, paths, truth


@pytest.mark.parametrize("y_up", [True, False])
def test_digitize_recovers_series_exactly(y_up):
    texts, paths, truth = _chart(y_up)
    region = (-0.8, -1.2, 4.3, 3.3) if y_up else (-0.8, -3.3, 4.3, 1.2)
    res = core.digitize(texts, paths, region, quantum=1e-4)
    assert res["axis_status"] == "OK"
    assert res["x_axis"].method == "SNAPPED_TO_TICKS" and res["y_axis"].method == "SNAPPED_TO_TICKS"
    got = {s["label_raw"]: s for s in res["series"]}
    assert set(got) == {"A", "B"}
    for name, pts in truth.items():
        xs = sorted((p["x"], p["y"]) for p in got[name]["points"])
        assert len(xs) == len(pts)
        for (x, y), (tx, ty) in zip(xs, pts):
            assert x == pytest.approx(tx, abs=1e-6) and y == pytest.approx(ty, abs=1e-6)
        assert all(p["y_err"] is not None and p["y_err"] >= 0 for p in got[name]["points"])


def test_axis_titles_take_the_whole_pdf_text_line():
    """Route A gives words: a title word brings the rest of its PDF text line, in reading order (rotated y title
    read bottom-to-top in a y-down frame)."""
    texts, paths, _ = _chart(y_up=False)
    texts += [Text("Время,", 1.5, 1.9, 0.35, 0.08, 0.0, "PDF_WORD:5.0"),
              Text("сут", 1.95, 2.15, 0.35, 0.08, 0.0, "PDF_WORD:5.0"),
              Text("Оседание,", -0.75, -0.65, -1.0, 0.08, 90.0, "PDF_WORD:6.0"),
              Text("мм", -0.75, -0.65, -1.8, 0.08, 90.0, "PDF_WORD:6.0")]
    res = core.digitize(texts, paths, (-0.8, -3.3, 4.3, 1.2), quantum=1e-4)
    assert res["axis_status"] == "OK"
    assert res["x_title_raw"] == "Время, сут" and res["y_title_raw"] == "Оседание, мм"
    assert core.split_title(res["y_title_raw"]) == ("Оседание", "мм")


def test_chain_joins_reversed_and_shuffled_pieces():
    pts = np.array([[0, 0], [1, 1], [2, 0.5], [3, 2]], float)
    pieces = [pts[2:4][::-1], pts[0:2], pts[1:3]]
    out = chain(pieces, 1e-6)
    assert len(out) == 1
    got = out[0] if out[0][0, 0] == 0 else out[0][::-1]
    assert np.allclose(got, pts)


def test_axis_fit_drops_an_outlier_and_detects_log_scale():
    rows = [[str(v), float(v), p, False, "NATIVE"] for v, p in ((0, 0.0), (10, 1.0), (20, 2.0), (30, 3.0),
                                                                  (7, 3.6))]
    ax = fit_axis("x", rows, "NUM", 0.1, 3, max_drop=1)
    assert ax is not None and ax.kind == "LINEAR" and ax.dropped == ["7"]
    rows = [[str(v), float(v), p, False, "NATIVE"] for v, p in ((1, 0.0), (10, 1.0), (100, 2.0), (1000, 3.0))]
    ax = fit_axis("y", rows, "NUM", 0.1, 3)
    assert ax.kind == "LOG10" and float(ax.value(1.5)) == pytest.approx(10 ** 1.5)


def test_date_axis_and_reversed_values():
    texts = [_label(d, x, -0.2) for d, x in (("04.01.2010", 0.0), ("04.01.2011", 1.0), ("04.01.2012", 2.0))]
    texts += [Text(str(v), -0.5, -0.1, y, 0.08) for v, y in ((0, 3.0), (100, 2.0), (200, 1.0), (300, 0.0))]
    xa, ya, _ = detect_axes(texts, [], [])
    assert xa.kind == "DATE" and from_decimal_year(float(xa.value(1.0))) == dt.date(2011, 1, 4)
    assert ya.b < 0 and float(ya.value(0.0)) == pytest.approx(300.0)


def test_legend_texts_are_not_tick_labels():
    # a legend numbered 1…6 is a perfectly linear column: it must not become the y axis
    texts, paths, _ = _chart()
    for i in range(6):
        y = 2.5 - 0.1 * i
        paths.append(Path(np.array([[3.0, y], [3.2, y]]), 0x00AA00 + i, 0.02, "LINE"))
        texts.append(Text(str(i + 1), 3.25, 3.3, y, 0.06))
    hs, vs = structure_lines(paths)
    leg = legend_text_ids(paths, texts, (-0.8, -1.2, 4.3, 3.3))
    xa, ya, _ = detect_axes(texts, hs, vs, exclude=leg)
    assert ya is not None and {lab[0] for lab in ya.labels} == {"0", "-20", "-40", "-60"}


def test_plot_box_keeps_the_legend_and_chart_border_out():
    texts, paths, _ = _chart()
    paths.append(Path(np.array([[-0.8, -1.1], [4.2, -1.1], [4.2, 3.2], [-0.8, 3.2], [-0.8, -1.1]]), 0xD9D9D9,
                      0.01, "POLY", closed=True))
    hs, vs = structure_lines(paths)
    xa, ya, _ = detect_axes(texts, hs, vs)
    box = plot_box(xa, ya, paths, (-0.8, -1.2, 4.3, 3.3), texts)
    assert box[1] > -0.3          # the legend (y < -0.5) stays outside
    series, legend, missing = extract_series(paths, texts, box)
    assert sorted(s["label_raw"] for s in series) == ["A", "B"] and missing == []


def test_marker_series_and_monochrome_legend():
    texts, paths, _ = _chart()
    # a scatter of 5 filled dots and a dashed curve drawn as closed dash outlines (not data points)
    dots = [(0.5, 2.0), (1.5, 1.6), (2.5, 1.0), (3.2, 0.8), (3.8, 0.5)]
    for cx, cy in dots:
        ang = np.linspace(0, 2 * np.pi, 13)
        paths.append(Path(np.stack([cx + 0.03 * np.cos(ang), cy + 0.03 * np.sin(ang)], 1), 0x00AA00, 0.0, "FILL",
                          True))
    for k in range(6):
        x = 0.2 + 0.5 * k
        paths.append(Path(np.array([[x, 2.7], [x + 0.2, 2.7], [x + 0.2, 2.72], [x, 2.72], [x, 2.7]]), 0x7F7F7F, 0.01,
                          "POLY", True))
    hs, vs = structure_lines(paths)
    xa, ya, _ = detect_axes(texts, hs, vs)
    box = plot_box(xa, ya, paths, (-0.8, -1.2, 4.3, 3.3), texts)
    series, _, _ = extract_series(paths, texts, box)
    marker = [s for s in series if s.get("sampling") == "MARKER_CENTRES"]
    assert len(marker) == 1 and len(marker[0]["pts"]) == 5
    assert np.allclose(marker[0]["pts"], sorted(dots), atol=1e-6)
    assert not any(s["color"] == "#7f7f7f" for s in series)


def test_split_title():
    assert core.split_title("Оседание, мм") == ("Оседание", "мм")
    assert core.split_title("Время, сутки") == ("Время", "сутки")
    assert core.split_title("Годы") == ("Годы", None)
    assert core.split_title("η (мм)") == ("η", "мм")

"""Regression tests of the digitizer fixes after the visual review of 08.10 (fd-0.1.5): one synthetic case per failure
mode of the reviewed figures — no corpus data, no network, no models (the OCR cases run only with Tesseract)."""
from __future__ import annotations

import math
import shutil

import numpy as np
import pytest

from vkm_corpus.figures import core
from vkm_corpus.figures.calibrate import corner_gaps, detect_axes, pair_axes, structure_lines, structure_segments
from vkm_corpus.figures.primitives import Path, Text, label_value, parse_number
from vkm_corpus.figures.series import extract_series, marker_legend, same_shape

H = 6.0                                    # label cap height (pt)


def _ylabel(v: str, y: float, x1: float = 103.0) -> Text:
    return Text(v, x1 - 2.2 * len(v) - 2, x1, y, H, origin=f"PDF_WORD:y{v}")


def _xlabel(v: str, x: float, y: float) -> Text:
    return Text(v, x - 1.2 * len(v) - 1, x + 1.2 * len(v) + 1, y, H, origin=f"PDF_WORD:x{v}")


def _line(a, b, colour=None, lw=0.8) -> Path:
    return Path(np.array([a, b], float), colour, lw, "LINE")


# ------------------------------------------------------------------------------------------------ mode 5: snapping
def test_a_tick_label_snaps_to_its_tick_not_to_a_legend_stroke_at_its_height():
    """VKM-SRC-017 p.5: «−20» snapped to an orange legend sample 0.8 pt off its tick — the scale shrank by 5 %."""
    ticks = {"40": 100.0, "20": 150.0, "0": 200.0, "–20": 250.0}
    texts = [_ylabel(v, y + (1.0 if v == "–20" else 0.3)) for v, y in ticks.items()]
    paths = [_line((110, 90), (110, 300))] + [_line((105, y), (110, y)) for y in ticks.values()]
    paths.append(_line((112, 251.4), (122, 251.4), colour=0xFFA500))      # legend sample beside the axis, inside
    hs, vs = structure_lines(paths)
    _, old, _ = detect_axes(texts, hs, vs)
    _, new, _ = detect_axes(texts, hs, vs, segments=structure_segments(paths))
    assert {lab[0]: lab[2] for lab in old.labels}["–20"] == pytest.approx(251.4)        # the old rule
    assert {lab[0]: lab[2] for lab in new.labels}["–20"] == pytest.approx(250.0)
    assert new.residual_rms == pytest.approx(0.0, abs=1e-9) and float(new.value(250.0)) == pytest.approx(-20.0)


def test_a_label_does_not_snap_to_another_panels_axis_line():
    """VKM-SRC-097 p.2 (а): the corner «0» snapped to the x axis line of the panel to the right (200 pt away)."""
    ys = {"0,01": 228.4, "0,02": 203.4, "0,03": 178.9, "0,04": 154.1}
    texts = [_ylabel(v, y) for v, y in ys.items()] + [_ylabel("0", 255.0)]
    paths = [_line((105, y), (110, y)) for y in ys.values()] + [_line((315, 256.4), (442, 256.4))]
    _, ya, _ = detect_axes(texts, *structure_lines(paths), segments=structure_segments(paths))
    assert "0" not in [lab[0] for lab in ya.labels if lab[3]]


def test_a_corner_zero_off_its_tick_snaps_where_its_neighbours_put_it():
    """VKM-SRC-097 p.2 (б), VKM-SRC-202 p.155: the «0» of the time axis stands left of the y axis line; unsnapped, it
    shifted every curve by 2 days."""
    texts = [_xlabel("0", 104.5, 312.0), _xlabel("20", 210.0, 312.0), _xlabel("40", 310.0, 312.0)]
    paths = [_line((110, 100), (110, 300)), _line((110, 300), (410, 300)),
             _line((210, 300), (210, 304)), _line((310, 300), (310, 304))]
    hs, vs = structure_lines(paths)
    old, _, _ = detect_axes(texts, hs, vs)
    new, _, _ = detect_axes(texts, hs, vs, segments=structure_segments(paths))
    assert abs(float(old.value(110.0))) > 0.5                                  # the shift of the old rule
    assert new.method == "SNAPPED_TO_TICKS" and float(new.value(110.0)) == pytest.approx(0.0, abs=1e-9)


# ------------------------------------------------------------------------------------------------ mode 1: panels
def _panel(x0: float, y0: float, xs: list[str], ys: list[str], dx: float = 75.0, dy: float = 25.0):
    """A chart corner at (x0, y0 = bottom): axis lines, ticks and labels; returns texts, paths."""
    texts, paths = [], [_line((x0, y0 - dy * (len(ys) - 1)), (x0, y0)), _line((x0, y0), (x0 + dx * (len(xs) - 1), y0))]
    for i, v in enumerate(xs):
        paths.append(_line((x0 + dx * i, y0), (x0 + dx * i, y0 + 4)))
        texts.append(_xlabel(v, x0 + dx * i, y0 + 12))
    for i, v in enumerate(ys):
        paths.append(_line((x0 - 5, y0 - dy * i), (x0, y0 - dy * i)))
        texts.append(_ylabel(v, y0 - dy * i, x1=x0 - 7))
    return texts, paths


def test_the_axes_are_the_corner_of_one_chart_not_of_two_panels():
    """VKM-SRC-141 p.3 г/д: the x labels of the panel above (0…8) won over the panel's own (0…40) — x read 5× too
    small. The region holds the lower panel and the x labels of the upper one."""
    texts, paths = _panel(110, 340, ["0", "10", "20", "30", "40"], ["0", "20", "40", "60", "80"])
    paths = [_line((335.3, 340), (335.3, 344)) if p.pts[0, 0] == 335 and p.pts[0, 1] == 340 else p for p in paths]
    for i, v in enumerate(["0", "2", "4", "6", "8"]):                    # the upper panel's x labels and ticks
        texts.append(_xlabel(v, 111 + 75 * i, 208.0))
        paths.append(_line((111 + 75 * i, 197), (111 + 75 * i, 201)))
    paths.append(_line((111, 197), (411, 197)))
    seg = structure_segments(paths)
    xa, ya, cands = detect_axes(texts, *structure_lines(paths), segments=seg)
    assert [lab[0] for lab in xa.labels] == ["0", "2", "4", "6", "8"]   # by label count the row above wins …
    assert corner_gaps(xa, ya)[0] > 4.0
    x2, y2, replaced = pair_axes(xa, ya, cands)
    assert replaced and [lab[0] for lab in x2.labels] == ["0", "10", "20", "30", "40"] and y2 is ya
    res = core.digitize(texts, paths, (60, 180, 430, 370), quantum=0.05)
    assert [lab[0] for lab in res["x_axis"].labels] == ["0", "10", "20", "30", "40"]
    assert any("corner of one chart" in n for n in res["notes"]) and not res["other_panels"]
    # a sound single chart is never re-paired
    t1, p1 = _panel(110, 340, ["0", "10", "20", "30", "40"], ["0", "20", "40", "60", "80"])
    xa1, ya1, c1 = detect_axes(t1, *structure_lines(p1), segments=structure_segments(p1))
    assert pair_axes(xa1, ya1, c1) == (xa1, ya1, False)


def test_a_box_over_several_panels_digitizes_one_of_them_and_says_so():
    """VKM-SRC-135 p.2: seven panels in one figure box; x from one panel, y from another, curves mixed."""
    t1, p1 = _panel(110, 340, ["0", "10", "20", "30", "40"], ["0", "20", "40", "60", "80"])
    t2, p2 = _panel(500, 322, ["0", "5", "10", "15", "20", "25"], ["0", "0.1", "0.2", "0.3", "0.4"], dx=60)
    texts, paths = t1 + t2, p1 + p2
    res = core.digitize(texts, paths, (60, 200, 860, 370), quantum=0.05)
    gx, gy = corner_gaps(res["x_axis"], res["y_axis"])
    assert gx <= 4.0 and gy <= 6.0 and res["other_panels"] is True


# ------------------------------------------------------------------------------------------------ mode 2: scales
def test_panels_sharing_the_x_axis_read_each_series_on_its_own_scale():
    """VKM-SRC-203 p.192: σy/γH (−1.2 … −0.8) under η (0 … −0.4) — the σ curve was read on the η scale."""
    texts = [_xlabel(v, 110 + 150 * i, 92.0) for i, v in enumerate(("0", "1000", "2000"))]
    paths = [_line((110, 100), (410, 100)), _line((110, 100), (110, 215))]
    paths += [_line((110 + 150 * i, 96), (110 + 150 * i, 100)) for i in range(3)]
    for i, v in enumerate(("0", "-0.1", "-0.2", "-0.3", "-0.4")):         # the η scale of the upper panel
        texts.append(_ylabel(v, 100 + 20 * i))
        paths.append(_line((105, 100 + 20 * i), (110, 100 + 20 * i)))
    for v, y in (("-1.2", 200.0), ("-1", 210.0), ("-0.8", 220.0)):         # the σ scale of the lower panel
        texts.append(_ylabel(v, y, x1=97.0))
        paths.append(_line((105, y), (110, y)))
    eta = [(110 + 30 * i, 100 + 20 * abs(math.sin(i / 3)) * 4) for i in range(11)]
    sig = [(110 + 30 * i, 205 + 8 * abs(math.cos(i / 2))) for i in range(11)]
    for pts, colour in ((eta, 0x00AA00), (sig, 0xDD0000)):
        paths.append(Path(np.array(pts, float), colour, 1.0, "POLY"))
    res = core.digitize(texts, paths, (60, 80, 430, 235), quantum=0.05)
    by = {s["color"]: s for s in res["series"]}
    lower, upper = by["#dd0000"], by["#00aa00"]
    ly = [p["y"] for p in lower["points"]]
    assert "Y_AXIS_PER_SERIES" in lower["flags"] and lower["y_axis"].span == (200.0, 220.0)
    assert min(ly) >= -1.2 - 1e-6 and max(ly) <= -0.8 + 1e-6 and lower["y_title_raw"] is None
    assert "Y_AXIS_PER_SERIES" not in upper["flags"] and "y_axis" not in upper
    assert all(-0.4 - 1e-6 <= p["y"] <= 1e-6 for p in upper["points"])


def test_a_second_y_scale_beside_the_first_is_flagged():
    """VKM-SRC-202 p.49: «S/S₀, %» 0…80 and «ΔV» −2…8 printed side by side left of one chart; ΔV was read on the
    S/S₀ scale. FD's legend rule hid the inner column (its labels stand right of the outer column's ticks)."""
    from vkm_corpus.figures.calibrate import Axis
    from vkm_corpus.navigation import figure_series as FS

    def column(x1, values, positions):
        labels = [[str(v), float(v), float(p), True, "NATIVE"] for v, p in zip(values, positions)]
        texts = [Text(str(v), x1 - 10, x1, float(p), 6.0) for v, p in zip(values, positions)]
        vals, pos = np.array(values, float), np.array(positions, float)
        b, a = np.polyfit(pos, vals, 1)
        return Axis("y", "LINEAR", float(a), float(b), labels=labels, method="SNAPPED_TO_TICKS"), texts

    box = (100.0, 100.0, 300.0, 300.0)
    outer, t_outer = column(70, (0, 20, 40, 60, 80), (300, 250, 200, 150, 100))
    inner, t_inner = column(92, (-2, 0, 2, 4, 6, 8), (350, 300, 250, 200, 150, 100))
    mirror, t_mirror = column(92, (0, 20, 40, 60, 80), (300, 250, 200, 150, 100))
    stacked, t_stacked = column(92, (-1.2, -1.0, -0.8), (320, 340, 360))
    assert FS.beside_axis(outer, [outer, inner], t_outer + t_inner, box)
    assert not FS.beside_axis(outer, [outer, mirror], t_outer + t_mirror, box)       # the same scale twice
    assert not FS.beside_axis(outer, [outer, stacked], t_outer + t_stacked, box)     # a panel below: per series
    far, t_far = column(30, (-2, 0, 2, 4, 6, 8), (350, 300, 250, 200, 150, 100))
    assert not FS.beside_axis(outer, [outer, far], t_outer + t_far, box)             # a neighbour panel's labels


# ------------------------------------------------------------------------------------------------ mode 7: legends
def _diamond(cx: float, cy: float, r: float = 3.0, colour: int = 0x4F81BD) -> Path:
    return Path(np.array([[cx, cy - r], [cx + r, cy], [cx, cy + r], [cx - r, cy]], float), colour, 0.0, "FILL",
                closed=True)


def test_the_legend_marker_is_not_a_data_point_and_names_the_series():
    """VKM-SRC-045: the «◆ Расчет МКЭ» sample of the legend was a point of the series (e.g. 0.223; 30.4)."""
    data = [(130, 280), (170, 240), (210, 200), (250, 170), (290, 150)]
    paths = [_diamond(x, y) for x, y in data] + [_diamond(330, 250), _diamond(370, 120)]
    texts = [Text("Расчет", 337, 357, 250, 6.0), Text("МКЭ", 360, 372, 250, 6.0),
             Text("рп.354", 377, 395, 120, 6.0)]                 # a data label next to a point is not a legend
    series, _, _ = extract_series(paths, texts, (100, 100, 400, 300))
    (s,) = series
    assert s["label_raw"] == "Расчет МКЭ" and s["legend_markers_excluded"] == 1
    assert sorted(map(tuple, s["pts"].tolist())) == sorted(data + [(370, 120)])
    assert marker_legend([_diamond(370, 120)], texts, 8.5) == {}


def test_a_legend_outside_the_plot_names_its_marker_series():
    """VKM-SRC-116 p.4: «● Н.О.=8,5%» under the chart — the series stayed unlabelled."""
    paths = [_diamond(130 + 40 * i, 200 - 10 * i) for i in range(5)] + [_diamond(150, 330)]
    texts = [Text("Н.О.=8,5%", 157, 190, 330, 6.0)]
    (s,) = extract_series(paths, texts, (100, 100, 400, 300))[0]
    assert s["label_raw"] == "Н.О.=8,5%" and len(s["pts"]) == 5 and s["legend_markers_excluded"] == 0


def test_identical_markers_close_together_are_all_data_and_curve_pieces_are_not_markers():
    """VKM-SRC-045 p.44: 18 of ~45 markers (close markers fell to the «letters shoulder to shoulder» rule); a thick
    curve drawn as filled pieces sharing their end vertices is not a marker series."""
    dense = [_diamond(120 + 5.0 * i, 200 + (i % 3)) for i in range(12)]         # spaced 0.83 marker sizes
    assert same_shape(dense, 6.0 * math.sqrt(2)) is not None
    (s,) = extract_series(dense, [], (100, 100, 400, 300))[0]
    assert len(s["pts"]) == 12
    # markers of another shape in the same colour group are kept as the old rule kept them (spaced out)
    triangles = [Path(np.array([[x - 3, 253], [x + 3, 253], [x, 247]], float), 0x4F81BD, 0.0, "FILL", closed=True)
                 for x in (200.0, 260.0, 320.0)]
    (s2,) = extract_series(dense + triangles, [], (100, 100, 400, 300))[0]
    assert len(s2["pts"]) == 15
    pieces = []
    for i in range(30):                                                       # a thick red curve as quads
        x, y, x2, y2 = 110 + 8 * i, 200 + 4 * math.sin(i / 4), 118 + 8 * i, 200 + 4 * math.sin((i + 1) / 4)
        pieces.append(Path(np.array([[x, y - 1], [x2, y2 - 1], [x2, y2 + 1], [x, y + 1]], float), 0xBE4B48, 0.0,
                           "FILL", closed=True))
    assert same_shape(pieces, 8.5) is None
    assert not [s for s in extract_series(pieces, [], (100, 100, 400, 300))[0] if s.get("sampling") == "MARKER_CENTRES"]


def test_a_tick_label_in_scientific_notation_is_a_number_not_an_axis_title():
    """VKM-SRC-045 p.90/92: the zero tick «-5E-19» became the x axis title."""
    assert parse_number("-5E-19") == pytest.approx(-5e-19) and parse_number("1E+05") == 1e5
    assert parse_number("2,5E-3") == pytest.approx(0.0025) and label_value("5E-18")[1] == "NUM"
    assert parse_number("E5") is None and parse_number("Rp2.2") is None
    texts = [_xlabel(v, 110 + 100 * i, 312.0) for i, v in enumerate(("0", "400", "800", "1200"))]
    texts += [_ylabel(v, y) for v, y in (("0.0004", 100.0), ("0.0002", 150.0), ("-5E-19", 200.0), ("-0.0002", 250.0))]
    texts.append(Text("Расстояние,", 230, 270, 330, 6.0, origin="PDF_WORD:t"))
    paths = [_line((110, 90), (110, 300)), _line((110, 300), (410, 300))]
    paths += [_line((110 + 100 * i, 300), (110 + 100 * i, 304)) for i in range(4)]
    paths += [_line((105, y), (110, y)) for y in (100.0, 150.0, 200.0, 250.0)]
    res = core.digitize(texts, paths, (60, 80, 430, 345), quantum=0.05)
    assert "-5E-19" in [lab[0] for lab in res["y_axis"].labels] and res["x_title_raw"] == "Расстояние,"


# ------------------------------------------------------------------------------------------------ raster (no OCR)
def test_raster_axes_from_foreign_numbers_are_withdrawn():
    """VKM-SRC-012 p.52: «1», «7», «33» of a table cell 36 px apart made a LOG10 x axis of the whole chart."""
    R = pytest.importorskip("vkm_corpus.figures.raster")
    from vkm_corpus.figures.calibrate import fit_axis

    rows = [["1", 1.0, 928.5, False, "LOCAL_OCR"], ["7", 7.0, 953.0, False, "LOCAL_OCR"],
            ["33", 33.0, 964.5, False, "LOCAL_OCR"]]
    ax = fit_axis("x", rows, "NUM", 10.0, 3, max_drop=1)
    assert ax is not None and ax.kind == "LOG10"                      # what the old rule kept
    assert not R.plausible_axis(ax, 2000.0)
    words = [Text(t, p - 5, p + 5, 1230.0, 10.0, source="LOCAL_OCR") for t, _, p, _, _ in rows]
    assert R.calibrate(words, [], [], None, None, extent=(2000.0, 1260.0)) == (None, None)
    assert R._regular_values([0, 500, 1000, 2500]) and not R._regular_values([1, 3, 80])
    assert R._log_mantissas([10, 100, 1000]) and not R._log_mantissas([1, 7, 33])


def test_raster_axis_keeps_the_labels_on_one_line_when_some_are_misread():
    """VKM-SRC-012 p.105: «−200», «−400», «−500» read without their minus — no y axis, a degenerate plot box."""
    R = pytest.importorskip("vkm_corpus.figures.raster")
    vals = [("0", 39), ("-100", 154), ("200", 269), ("-300", 384), ("400", 497), ("500", 612), ("-600", 726),
            ("-700", 841)]
    words = [Text(t, 80, 140, float(y), 16.0, source="LOCAL_OCR") for t, y in vals]
    _, ya = R.calibrate(words, [], [], None, None, extent=(2000.0, 950.0))
    assert ya is not None and sorted(ya.dropped) == ["200", "400", "500"]
    assert float(ya.value(841.0)) == pytest.approx(-700.0, abs=2.0)
    # a column of legend numbers with misread entries never displaces a column that fits as a whole
    legend = [Text(str(n), 1900, 1915, 300.0 + 40 * i, 16.0, source="LOCAL_OCR") for i, n in enumerate(range(13, 31))]
    legend[2].text, legend[5].text = "2", "77"
    good = [Text(str(v), 80, 140, 800.0 - 100 * i, 16.0, source="LOCAL_OCR") for i, v in
            enumerate((0, 200, 400, 600, 800, 1000))]
    _, ya2 = R.calibrate(good + legend, [], [], None, None, extent=(2000.0, 950.0))
    assert [lab[0] for lab in ya2.labels] == ["0", "200", "400", "600", "800", "1000"]


def test_raster_plot_box_stops_at_the_axis_line_not_at_a_zone_boundary():
    """VKM-SRC-012 p.49/50: the box walked inwards over a zone line at x = 20 (the chart's 0…20 lost); labels 5 and
    10 unread put its left edge at 15 although the axis line is at 0."""
    rd = pytest.importorskip("vkm_corpus.figures.raster_digitize")
    from vkm_corpus.figures.calibrate import Axis

    xa = Axis("x", "LINEAR", 0.0, 0.1, labels=[[str(v), float(v), 150.0 + 12 * v, False, "LOCAL_OCR"]
                                               for v in range(0, 130, 10)])
    ya = Axis("y", "LINEAR", -50.0, 0.3, labels=[[str(v), float(v), 50.0 + (v + 50) / 0.3, False, "LOCAL_OCR"]
                                                 for v in (-50, 50, 100, 250)])
    vl = [(46.0, 1110.0, 150.0), (46.0, 1102.0, 270.0), (46.0, 1107.0, 388.0)]      # axis, zone edge, zone line
    box = rd._box_from(xa, ya, [], vl, (1154, 1896))
    assert box[0] == pytest.approx(150.0)
    # the x labels read start at 15 (at 593 px); the y axis line at 113 px, right of the y label column
    xa15 = Axis("x", "LINEAR", 0.0, 1.0, labels=[[str(v), float(v), 593.0 + 32 * (v - 15), False, "LOCAL_OCR"]
                                                 for v in range(15, 50, 5)])
    ya2 = Axis("y", "LINEAR", 0.0, 1.0, labels=[[str(v), float(v), 186.0 + 0.2784 * v, False, "LOCAL_OCR"]
                                                for v in (0, 500, 1500, 2000)])
    words = [Text(str(v), 60, 96, 186.0 + 0.2784 * v, 13.0, source="LOCAL_OCR") for v in (0, 500, 1500, 2000)]
    box = rd._box_from(xa15, ya2, [], [(42.0, 1020.0, 113.0), (47.0, 1020.0, 433.0)], (1052, 2085), words)
    assert box[0] == pytest.approx(113.0)


def test_join_tracks_takes_the_cheapest_continuation():
    R = pytest.importorskip("vkm_corpus.figures.raster")
    t = lambda x0, y0, n, c: {"xs": np.arange(x0, x0 + n, dtype=float), "ys": np.full(n, float(y0)),  # noqa: E731
                              "width": 3.0, "colour": np.array(c, float)}
    tracks = [t(0, 100, 20, (50, 150, 150)), t(25, 100.5, 20, (50, 150, 150)), t(25, 130, 20, (50, 150, 150)),
              t(25, 101, 20, (200, 100, 100))]
    out = R.join_tracks(tracks, max_gap_x=12.0, max_dy=8.0)
    first = next(o for o in out if o["xs"][0] == 0)
    assert len(first["xs"]) == 40 and first["gaps"] == [(19.0, 25.0)] and np.allclose(first["ys"][20:], 100.5)


def test_raster_label_boxes_are_not_traced_as_curves():
    """VKM-SRC-012 p.46: the pale yellow boxes of the zone names («2ЮВ», «СКРУ-2») were traced as three series at
    −90…−110 mm. A filled box of one colour is not a curve; a curve of another colour beside it stays whole."""
    cv2 = pytest.importorskip("cv2")
    R = pytest.importorskip("vkm_corpus.figures.raster")

    h, w = 400, 900
    img = np.full((h, w, 3), 255, np.uint8)
    xs = np.arange(40, 860)
    ys = (150 + 60 * np.sin(xs / 90.0)).astype(np.int32)
    cv2.polylines(img, [np.stack([xs, ys], 1).astype(np.int32)], False, (220, 40, 40), 5)        # a red curve
    for bx in (100, 300, 500):                                                      # pale yellow boxes with text
        cv2.rectangle(img, (bx, 290), (bx + 120, 370), (250, 250, 160), -1)
        cv2.putText(img, "2UV", (bx + 15, 345), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 0), 3)
    for cx in (700, 790):                                    # big filled data markers of a thin-line chart, no text
        cv2.circle(img, (cx, 330), 15, (40, 90, 200), -1)
        cv2.circle(img, (cx, 330), 15, (0, 0, 0), 2)         # with a dark outline
    dark, grey, coloured, bg, chroma, lab = R.masks(img)
    fill = R.filled_areas(coloured, lab, stroke=3.0, dark=dark)
    assert not fill[315:346, 685:806].any()                   # markers are not label boxes
    assert R.filled_areas(coloured, lab, stroke=3.0)[325:336, 695:706].all()   # without the text rule they would be
    fill = R.filled_areas(coloured, lab, dark=dark)
    box = np.zeros((h, w), bool)
    box[290:371, 100:621] = True
    assert fill[290:371, 100:221].mean() > 0.95 and fill[290:371, 300:421].mean() > 0.95
    curve = coloured & ~box
    assert not (fill & curve).any()                                             # the curve is not a filled area
    def in_boxes(t):
        return bool(np.any((t["xs"] <= 640) & (t["ys"] > 280)))
    tracks = [t for t in R.track_curves(coloured & ~fill, lab, 0, w - 1, max_jump=12, max_run=100)
              if len(t["xs"]) >= 8]
    assert any(float(np.max(t["ys"])) < 230 and len(t["xs"]) > 600 for t in tracks)   # the red curve
    assert not any(in_boxes(t) for t in tracks)                                         # nothing in the boxes
    # without the rule the boxes' rows are traced as well
    assert any(in_boxes(t) for t in R.track_curves(coloured, lab, 0, w - 1, max_jump=12, max_run=100)
               if len(t["xs"]) >= 30)


def test_raster_minus_read_as_a_dash_still_calibrates_the_axis():
    """VKM-SRC-004 p.13 (InSAR): the y labels «−10 … −60» of a small raster were read as «=10», «—40», «—-60»: the
    column did not parse and the chart stayed NO_AXES."""
    R = pytest.importorskip("vkm_corpus.figures.raster")

    assert [R.ocr_minus(s) for s in ("=10", "—40", "—-60", "-10", "10", "a=10", "==", "–5")] == \
        ["-10", "-40", "-60", "-10", "10", "a=10", "==", "-5"]
    read = [("0", 142.0), ("=10", 201.5), ("=30", 319.0), ("—40", 378.5), ("=50", 436.5), ("—-60", 495.5)]
    raw = [Text(s, 98.0, 161.0, y, 22.0, source="LOCAL_OCR") for s, y in read]
    _, ya_raw = R.calibrate(raw, [], [], None, None, extent=(637.0, 616.0))
    assert ya_raw is None
    words = [Text(R.ocr_minus(s), 98.0, 161.0, y, 22.0, source="LOCAL_OCR") for s, y in read]
    _, ya = R.calibrate(words, [], [], None, None, extent=(637.0, 616.0))
    assert ya is not None and float(ya.value(142.0)) == pytest.approx(0.0, abs=0.8)
    assert float(ya.value(495.5)) == pytest.approx(-60.0, abs=0.8)


def test_raster_ocr_garbage_and_tick_rows_do_not_name_series():
    """VKM-SRC-012 p.46/p.51: «\\m», «|ho», «I~», «/N» and the numbers of a top axis inside the plot named series."""
    rd = pytest.importorskip("vkm_corpus.figures.raster_digitize")

    assert not any(rd.label_word(s) for s in ("\\m", "|ho", "I~", "/N", "it", "L", ""))
    assert all(rd.label_word(s) for s in ("Прогноз", "СКРУ-2", "нивелирование", "Репер"))
    row = [Text(str(v), 100 + 200 * i, 130 + 200 * i, 50.0, 12.0) for i, v in enumerate((10, 20, 30, 40))]
    lone = Text("2", 500, 510, 300.0, 12.0)
    pair = [Text("27.12.91", 800, 860, 400.0, 12.0), Text("04.12.92", 900, 960, 400.0, 12.0)]   # legend columns
    ids = rd.numeric_rows(row + [lone] + pair)
    assert ids == {id(t) for t in row}


# ------------------------------------------------------------------------------------------------ raster with OCR
needs_ocr = pytest.mark.skipif(shutil.which("tesseract") is None, reason="local OCR helper (tesseract) not installed")


@needs_ocr
def test_ocr_reads_a_minus_sign_and_ignores_the_tick_after_a_label():
    cv2 = pytest.importorskip("cv2")
    from vkm_corpus.figures import raster as R
    from vkm_corpus.figures.labels_ocr import OcrEngine

    img = np.full((60, 220), 255, np.uint8)
    cv2.putText(img, "-100", (10, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.9, 0, 2, cv2.LINE_AA)
    cv2.line(img, (100, 30), (112, 30), 0, 2)                       # the tick mark right of the label
    out = R.ocr_blobs(img, (0, 0), OcrEngine(), "0123456789.,-")
    assert [t.text for t in out] == ["-100"]


@needs_ocr
def test_rotated_date_labels_calibrate_the_time_axis():
    """VKM-SRC-012 p.103/104: dates printed rotated under the chart were not read (X_UNCALIBRATED)."""
    cv2 = pytest.importorskip("cv2")
    from vkm_corpus.figures import raster as R
    from vkm_corpus.figures.labels_ocr import OcrEngine

    w, h = 1400, 700
    img = np.full((h, w, 3), 255, np.uint8)
    for i, year in enumerate(range(1980, 2040, 5)):
        lab = np.full((40, 260), 255, np.uint8)
        cv2.putText(lab, f"01.01.{year}", (5, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.9, 0, 2, cv2.LINE_AA)
        rot = cv2.rotate(lab, cv2.ROTATE_90_COUNTERCLOCKWISE)          # read bottom to top
        x = 100 + 100 * i
        img[400:660, x - 20:x + 20] = np.minimum(img[400:660, x - 20:x + 20], rot[:, :, None])
    coloured = np.zeros((h, w), bool)
    ax = R.rotated_date_axis(img, coloured, OcrEngine(), (0, 380, w, h), [])
    assert ax is not None and ax.kind == "DATE" and ax.label_source == "LOCAL_OCR"
    assert float(ax.value(100.0)) == pytest.approx(1980.0, abs=0.3) and float(ax.value(1200.0)) == pytest.approx(
        2035.0, abs=0.3)

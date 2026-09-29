"""Route B (AutoCAD -PDFIMPORT DXF) on a synthetic drawing that reproduces the import's quirks: a row of tick labels
merged into one MTEXT, BYLAYER colour of the first object on a layer, Bezier pieces as SPLINEs, off-page text."""
from __future__ import annotations

import numpy as np
import pytest

ezdxf = pytest.importorskip("ezdxf")

from vkm_corpus.figures import core, dxf_route  # noqa: E402
from vkm_corpus.figures.primitives import Text  # noqa: E402

BLUE = 0x4F81BD
RED = 0xC0504D


def _mtext(msp, text, x, y, h=0.08):
    m = msp.add_mtext(text, dxfattribs={"layer": "PDF _Текст", "char_height": h, "attachment_point": 4})
    m.dxf.insert = (x, y)
    return m


def _bez(msp, a, b, **attrs):
    (x0, y0), (x1, y1) = a, b
    cps = [(x0, y0), (x0 + (x1 - x0) / 3, y0 + (y1 - y0) / 3), (x0 + 2 * (x1 - x0) / 3, y0 + 2 * (y1 - y0) / 3),
           (x1, y1)]
    s = msp.add_spline(dxfattribs={"layer": "PDF _Геометрия", **attrs})
    s.apply_construction_tool(ezdxf.math.BSpline(cps, order=4))
    return s


@pytest.fixture()
def drawing(tmp_path):
    doc = ezdxf.new("R2018")
    geo = doc.layers.add("PDF _Геометрия")
    geo.rgb = ((BLUE >> 16) & 255, (BLUE >> 8) & 255, BLUE & 255)   # layer colour = first object's colour
    doc.layers.add("PDF _Текст")
    msp = doc.modelspace()
    # axes with ticks; x labels 0…40 merged into one MTEXT, y labels separate
    msp.add_lwpolyline([(1, 1), (5, 1)], dxfattribs={"layer": "PDF _Геометрия", "color": 7})
    msp.add_lwpolyline([(1, 1), (1, 4)], dxfattribs={"layer": "PDF _Геометрия", "color": 7})
    for i in range(5):
        msp.add_lwpolyline([(1 + i, 1), (1 + i, 0.95)], dxfattribs={"layer": "PDF _Геометрия", "color": 7})
    _mtext(msp, "0 10 20 30 40", 0.97, 0.85)
    for v, y in ((0, 4.0), (100, 3.0), (200, 2.0), (300, 1.0)):
        msp.add_lwpolyline([(0.95, y), (1, y)], dxfattribs={"layer": "PDF _Геометрия", "color": 7})
        _mtext(msp, str(v), 0.6, y)
    # series: BLUE drawn BYLAYER (its colour is the layer's), RED with an explicit true colour
    blue = [(1, 4.0), (2, 3.5), (3, 2.0), (4, 1.8), (5, 1.6)]
    red = [(1, 3.9), (2, 3.8), (3, 3.2), (4, 3.0), (5, 2.9)]
    for pts, attrs in ((blue, {"color": 256}), (red, {"true_color": RED})):
        for a, b in zip(pts[:-1], pts[1:]):
            _bez(msp, a, b, **attrs)
    # off-page text that PDFIMPORT drops onto the plot
    _mtext(msp, "17* 0,00011", 2.5, 2.5)
    path = tmp_path / "import.dxf"
    doc.saveas(path)
    words = [Text(str(v), 1 + v / 10 - 0.05, 1 + v / 10 + 0.05, 0.85, 0.08) for v in (0, 10, 20, 30, 40)]
    words += [Text(str(v), 0.6, 0.6 + 0.06 * len(str(v)), y, 0.08) for v, y in ((0, 4.0), (100, 3.0), (200, 2.0),
                                                                                  (300, 1.0))]
    return path, words, blue, red


def test_multi_segment_spline_keeps_its_joints(tmp_path):
    doc = ezdxf.new("R2018")
    msp = doc.modelspace()
    joints = [(0, 0), (1, 2), (2, 1), (3, 3)]
    cps = [joints[0]]
    for a, b in zip(joints[:-1], joints[1:]):
        cps += [(a[0] + (b[0] - a[0]) / 3, a[1] + (b[1] - a[1]) / 3),
                (a[0] + 2 * (b[0] - a[0]) / 3, a[1] + 2 * (b[1] - a[1]) / 3), b]
    knots = [0, 0, 0, 0, 1, 1, 1, 2, 2, 2, 3, 3, 3, 3]
    s = msp.add_open_spline(cps, degree=3, knots=knots)
    s.dxf.true_color = RED
    path = tmp_path / "s.dxf"
    doc.saveas(path)
    _, paths = dxf_route.load_dxf(path)
    assert np.allclose(paths[0].pts, joints)


def test_refit_spline_keeps_only_its_ends_and_marks_the_series(tmp_path):
    """A cubic with a double interior knot (PDFIMPORT's refit of a chain): its knot point need not be a vertex of
    the PDF path, and samples of the curve are not plotted data — only the ends are kept, the series is flagged."""
    doc = ezdxf.new("R2018")
    msp = doc.modelspace()
    cps = [(1.0, 1.0), (1.5, 2.0), (2.0, 2.5), (3.0, 2.5), (3.5, 2.0), (4.0, 1.2)]
    s = msp.add_open_spline(cps, degree=3, knots=[0, 0, 0, 0, 1, 1, 2, 2, 2, 2])
    s.dxf.true_color = RED
    _bez(msp, (4.0, 1.2), (4.5, 1.6), true_color=RED)       # a one-piece spline continues the curve
    path = tmp_path / "refit.dxf"
    doc.saveas(path)
    _, paths = dxf_route.load_dxf(path)
    refit = [p for p in paths if p.extra.get("spline_refit")]
    assert len(refit) == 1 and np.allclose(refit[0].pts, [(1.0, 1.0), (4.0, 1.2)])
    assert refit[0].dense is not None and len(refit[0].dense) > 2      # the drawn shape stays for rendering
    from vkm_corpus.figures.series import extract_series

    series, *_ = extract_series(paths, [], (0.5, 0.5, 5.0, 3.0))
    assert len(series) == 1 and series[0]["spline_refit_pieces"] == 1
    assert np.allclose(series[0]["pts"], [(1.0, 1.0), (4.0, 1.2), (4.5, 1.6)])
    res = core.digitize([], paths, (0.5, 0.5, 5.0, 3.0), quantum=0.05 / 72)
    assert "SPLINE_INTERIOR_NOT_RECOVERED" in res["series"][0]["flags"]


def test_closed_spline_is_a_marker_outline(tmp_path):
    """A closed spline (a marker drawn as a stroke) keeps its outline, so the marker series finds its centre."""
    doc = ezdxf.new("R2018")
    msp = doc.modelspace()
    centres = [(1.5, 1.2), (2.5, 2.0), (3.5, 1.6)]
    r = 0.03
    for cx, cy in centres:
        loop = [(cx - r, cy - r), (cx + r, cy - r), (cx + r, cy + r), (cx - r, cy + r), (cx - r, cy - r)]
        s = msp.add_open_spline(loop, degree=3, knots=[0, 0, 0, 0, 1, 2, 2, 2, 2])
        s.dxf.true_color = BLUE
    path = tmp_path / "markers.dxf"
    doc.saveas(path)
    _, paths = dxf_route.load_dxf(path)
    assert len(paths) == 3 and all(p.closed and len(p.pts) > 4 and not p.extra for p in paths)
    from vkm_corpus.figures.series import extract_series

    series, *_ = extract_series(paths, [], (0.5, 0.5, 5.0, 3.0))
    marks = [s for s in series if s.get("sampling") == "MARKER_CENTRES"]
    assert len(marks) == 1 and len(marks[0]["pts"]) == 3
    for (x, y), (cx, cy) in zip(marks[0]["pts"], centres):
        assert abs(x - cx) <= r and abs(y - cy) <= r


def test_bylayer_colour_is_resolved(drawing):
    path, *_ = drawing
    texts, paths = dxf_route.load_dxf(path)
    colours = {p.color for p in paths if p.kind == "BEZ"}
    assert colours == {BLUE, RED}


def test_merged_row_is_split_and_offpage_text_dropped(drawing):
    path, words, *_ = drawing
    texts, _ = dxf_route.load_dxf(path)
    refined, stats = dxf_route.refine_texts(texts, words)
    got = sorted(t.text for t in refined)
    assert "0 10 20 30 40" not in got and all(v in got for v in ("10", "20", "30", "40"))
    assert not any("0,00011" in t.text for t in refined)
    assert stats["split"] == 1 and stats["dropped_unconfirmed"] == 1


def test_route_b_digitizes_the_synthetic_import(drawing):
    path, words, blue, red = drawing
    texts, paths = dxf_route.load_dxf(path)
    texts, _ = dxf_route.refine_texts(texts, words)
    res = core.digitize(texts, paths, (0.3, 0.5, 5.3, 4.3), quantum=0.05 / 72)
    assert res["axis_status"] == "OK"
    by_colour = {s["color"]: s for s in res["series"]}
    for colour, pts in (("#4f81bd", blue), ("#c0504d", red)):
        got = sorted((p["x"], p["y"]) for p in by_colour[colour]["points"])
        want = sorted(((x - 1) * 10.0, (4.0 - y) * 100.0) for x, y in pts)
        assert np.allclose(got, want, atol=1e-6)

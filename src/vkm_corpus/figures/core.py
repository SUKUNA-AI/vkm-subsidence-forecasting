"""Route-independent digitization of one figure: axes → plot area → series → points with per-point errors."""
from __future__ import annotations

import re
from typing import Callable

import numpy as np

from vkm_corpus.figures.calibrate import (Axis, detect_axes, labels_disjoint, other_panels, pair_axes, plot_box,
                                          structure_lines, structure_segments)
from vkm_corpus.figures.primitives import Path, Text, from_decimal_year, label_value
from vkm_corpus.figures.series import extract_series, legend_text_ids, point_errors

# glyph_reader(orient, texts, paths, box_guess) -> Axis | None
GlyphReader = Callable[[str, list, list, tuple], "Axis | None"]


def split_title(title: str | None) -> tuple[str | None, str | None]:
    """'Оседание, мм' → ('Оседание', 'мм'); 'Время, сутки' → ('Время', 'сутки'); 'Годы' → ('Годы', None)."""
    if not title:
        return None, None
    t = title.strip()
    m = re.match(r"^(.*?)[,;]\s*([^,;]{1,15})$", t)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    m = re.match(r"^(.*?)\s*\(([^()]{1,15})\)$", t)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return t, None


def _line_of(t: Text, texts: list[Text]) -> list[Text]:
    """The words of the PDF text line of ``t`` in reading order (route A gives words; an MTEXT is already whole)."""
    if not t.origin.startswith("PDF_WORD:"):
        return [t]
    same = [u for u in texts if u.origin == t.origin]
    if abs(t.rot) >= 45:           # rotated line: reading order along y (y down: bottom-to-top when rot > 0)
        same.sort(key=lambda u: u.yc if t.rot < 0 else -u.yc)
    else:
        same.sort(key=lambda u: u.x0)
    return same


def axis_titles(texts: list[Text], xa: Axis | None, ya: Axis | None, box) -> tuple[str | None, str | None]:
    """Non-numeric texts closest to the x tick row and to the y tick column (outside the plot area); a word of a PDF
    text line brings the whole line."""
    def is_title(t):
        s = t.text.strip()
        return label_value(s)[0] is None and len(s) > 1 and any(ch.isalpha() for ch in s)
    cands = [t for t in texts if is_title(t)]
    x0, y0, x1, y1 = box
    xt = yt = None
    y_pick: Text | None = None
    y_line: list[Text] = []
    xrow = [t.yc for t in texts if xa and t.text.strip() in {lab[0] for lab in xa.labels}]
    ycol = [t.xc for t in texts if ya and t.text.strip() in {lab[0] for lab in ya.labels}]
    outside = [t for t in cands if not (x0 < t.xc < x1 and y0 < t.yc < y1)]
    if ycol and outside:       # the y title first: it sits beside the tick column, often rotated
        col_x, mid = float(np.mean(ycol)), (y0 + y1) / 2
        side = [t for t in outside if abs(t.xc - col_x) < abs(t.xc - (x0 + x1) / 2)]
        if side:
            y_pick = min(side, key=lambda t: abs(t.xc - col_x) + abs(t.yc - mid))
            y_line = _line_of(y_pick, texts)
            yt = " ".join(t.text for t in y_line)
    if xrow and outside:       # labels read by OCR have no text row: no x title then
        row_y, mid = float(np.mean(xrow)), (x0 + x1) / 2
        taken = {id(t) for t in y_line}
        pool = [t for t in outside if id(t) not in taken and (y_pick is None or t.text != y_pick.text)]
        if pool:
            xt = " ".join(t.text for t in _line_of(
                min(pool, key=lambda t: 3 * abs(t.yc - row_y) + 0.5 * abs(t.xc - mid)), texts))
    return xt, yt


def _regular(values) -> bool:
    """Tick values stepping by multiples of their smallest step (missing ticks allowed)."""
    vals = sorted({float(v) for v in values})
    if len(vals) < 3:
        return False
    steps = [b - a for a, b in zip(vals, vals[1:])]
    s0 = min(steps)
    return s0 > 0 and all(abs(s / s0 - round(s / s0)) <= 0.02 * s / s0 for s in steps)


def stacked_axes(ya: Axis | None, cands: list[Axis], box) -> list[Axis]:
    """Other y scales of a chart whose panels share the x axis (fd-0.1.5): a column of labels snapped to ticks (≥ 3,
    regularly stepped, other labels than the chosen axis), within 6 label heights of the chosen column and outside the
    plot box, whose label span does not overlap the chosen one and that gives another scale — the scale of the panel
    above or below (σ/γH under η)."""
    if ya is None or len(ya.texts) < 2:
        return []
    m_lo, m_hi = ya.span
    col = float(np.median([t.xc for t in ya.texts]))
    h = float(np.median([t.h for t in ya.texts])) or 1.0
    x0, _, x1, _ = box
    out: list[Axis] = []
    for c in cands:
        if c is ya or c.orient != "y" or len(c.labels) < 3 or len(c.texts) < 3 or c.method == "TEXT_CENTRE":
            continue
        if not labels_disjoint(c, ya) or not _regular([lab[1] for lab in c.labels]):
            continue
        c_lo, c_hi = c.span
        if not (c_lo > m_hi or c_hi < m_lo):
            continue                                   # overlapping spans: two scales of one panel, not stacked
        if abs(float(np.median([t.xc for t in c.texts])) - col) > 6.0 * h:
            continue
        if min(t.x1 for t in c.texts) > x0 + 0.5 * h and max(t.x0 for t in c.texts) < x1 - 0.5 * h:
            continue                                   # inside the plot: curve labels, not a scale
        span = abs(float(ya.value(m_hi)) - float(ya.value(m_lo))) or 1.0
        if max(abs(float(c.value(p)) - float(ya.value(p))) for p in (m_lo, m_hi)) <= 0.01 * span:
            continue
        if any(not labels_disjoint(c, o) for o in out):
            continue                                   # the same column found by another clustering
        out.append(c)
    return out


def _band_axis(ys_page: np.ndarray, ya: Axis, others: list[Axis]) -> Axis | None:
    """The stacked scale whose band holds every vertex of a series: each vertex nearer to its label span than to the
    chosen axis' span, and within half that span beyond it."""
    def dist(y, lo, hi):
        return np.maximum(0.0, np.maximum(lo - y, y - hi))
    m_lo, m_hi = ya.span
    for c in others:
        c_lo, c_hi = c.span
        reach = 0.5 * (c_hi - c_lo)
        dc, dm = dist(ys_page, c_lo, c_hi), dist(ys_page, m_lo, m_hi)
        if len(ys_page) and bool(np.all(dc < dm)) and bool(np.all(dc <= reach)):
            return c
    return None


def digitize(texts: list[Text], paths: list[Path], region, quantum: float,
             glyph_reader: GlyphReader | None = None, corpus_labels: list[Text] | None = None) -> dict:
    """``region``: figure box in drawing units; ``quantum``: coordinate precision in drawing units (error floor)."""
    snap_h, snap_v = structure_lines(paths)
    segments = structure_segments(paths)
    leg = legend_text_ids(paths, texts, region)
    xa, ya, cands = detect_axes(texts, snap_h, snap_v, exclude=leg, segments=segments)
    notes: list[str] = []
    xa, ya, paired = pair_axes(xa, ya, cands)
    if paired:
        notes.append("x and y axes chosen as the corner of one chart (the labels of another panel stand nearer)")
    if (xa is None or ya is None) and corpus_labels:
        xa2, ya2, _ = detect_axes(texts + corpus_labels, snap_h, snap_v, exclude=leg, segments=segments)
        if xa is None and xa2 is not None:
            xa = xa2
            notes.append("x axis from corpus text of the region")
        if ya is None and ya2 is not None:
            ya = ya2
            notes.append("y axis from corpus text of the region")
    if (xa is None or ya is None) and glyph_reader is not None:
        guess = plot_box(xa, ya, paths, region, texts)
        if xa is None:
            xa = glyph_reader("x", texts, paths, guess)
            if xa is not None:
                notes.append("x axis from glyph-outline labels (local OCR helper)")
        if ya is None:
            ya = glyph_reader("y", texts, paths, guess)
            if ya is not None:
                notes.append("y axis from glyph-outline labels (local OCR helper)")
    box = plot_box(xa, ya, paths, region, texts)
    series, legend, legend_without_curve = extract_series(paths, texts, box)
    xt, yt = axis_titles(texts, xa, ya, box)
    others = stacked_axes(ya, cands, box) if ya is not None and ya.label_source == "NATIVE" else []
    out_series = []
    for s in series:
        pts = s["pts"]
        if len(pts) < 2:
            continue
        sa = _band_axis(pts[:, 1], ya, others) if others else None   # the scale of the panel the series is in
        yax = sa or ya
        xs = xa.value(pts[:, 0]) if xa else None
        ys = yax.value(pts[:, 1]) if yax else None
        ex = point_errors(xa, pts[:, 0], quantum)
        ey = point_errors(yax, pts[:, 1], quantum)
        points = []
        for i, p in enumerate(pts):
            row = {"i": i, "x": None if xs is None else float(xs[i]), "y": None if ys is None else float(ys[i]),
                   "x_err": None if ex is None else float(ex[i]), "y_err": None if ey is None else float(ey[i]),
                   "x_drawing": float(p[0]), "y_drawing": float(p[1])}
            if xa is not None and xa.kind == "DATE":
                row["x_date"] = from_decimal_year(float(xs[i])).isoformat()
            points.append(row)
        flags = []
        if xa is None:
            flags.append("X_UNCALIBRATED")
        if ya is None:
            flags.append("Y_UNCALIBRATED")
        if not s["label_raw"]:
            flags.append("LEGEND_UNMATCHED")
        if s["n_chains_in_style"] > 1:
            flags.append("MULTI_CHAIN_STYLE")
        if s.get("spline_refit_pieces"):
            flags.append("SPLINE_INTERIOR_NOT_RECOVERED")
        if s.get("legend_markers_excluded"):
            flags.append("LEGEND_MARKER_EXCLUDED")       # the legend's sample marker is not a data point
        for ax in (xa, ya):
            if ax is not None and ax.label_source == "LOCAL_OCR":
                flags.append("LOCAL_OCR_CALIBRATION")
        extra = {}
        if sa is not None:
            # values on the scale of the series' own panel; the title of that scale is not looked for (a neighbour
            # panel's title or unit would be worse than none)
            flags.append("Y_AXIS_PER_SERIES")
            extra = {"y_axis": sa, "y_title_raw": None}
        out_series.append({**{k: v for k, v in s.items() if k != "pts"}, "points": points, "flags": flags,
                           **extra})
    if any(s.get("y_axis") is not None for s in out_series):
        notes.append("panels sharing the x axis: series on another y scale are read on that scale (Y_AXIS_PER_SERIES)")
    return {"x_axis": xa, "y_axis": ya, "plot_box": [float(v) for v in box], "series": out_series,
            "legend": {str(k): v for k, v in legend.items()}, "legend_without_curve": legend_without_curve,
            "x_title_raw": xt, "y_title_raw": yt, "notes": notes, "other_panels": other_panels(xa, ya, cands),
            "axis_status": ("OK" if xa and ya else "X_UNCALIBRATED" if ya else "Y_UNCALIBRATED" if xa
                            else "NO_AXES")}

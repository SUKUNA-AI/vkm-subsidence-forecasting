"""Route-independent digitization of one figure: axes → plot area → series → points with per-point errors."""
from __future__ import annotations

import re
from typing import Callable

import numpy as np

from vkm_corpus.figures.calibrate import Axis, detect_axes, plot_box, structure_lines
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


def axis_titles(texts: list[Text], xa: Axis | None, ya: Axis | None, box) -> tuple[str | None, str | None]:
    """Non-numeric texts closest to the x tick row and to the y tick column (outside the plot area)."""
    def is_title(t):
        s = t.text.strip()
        return label_value(s)[0] is None and len(s) > 1 and any(ch.isalpha() for ch in s)
    cands = [t for t in texts if is_title(t)]
    x0, y0, x1, y1 = box
    xt = yt = None
    xrow = [t.yc for t in texts if xa and t.text.strip() in {lab[0] for lab in xa.labels}]
    ycol = [t.xc for t in texts if ya and t.text.strip() in {lab[0] for lab in ya.labels}]
    outside = [t for t in cands if not (x0 < t.xc < x1 and y0 < t.yc < y1)]
    if ycol and outside:       # the y title first: it sits beside the tick column, often rotated
        col_x, mid = float(np.mean(ycol)), (y0 + y1) / 2
        side = [t for t in outside if abs(t.xc - col_x) < abs(t.xc - (x0 + x1) / 2)]
        if side:
            yt = min(side, key=lambda t: abs(t.xc - col_x) + abs(t.yc - mid)).text
    if xrow and outside:       # labels read by OCR have no text row: no x title then
        row_y, mid = float(np.mean(xrow)), (x0 + x1) / 2
        pool = [t for t in outside if t.text != yt]
        if pool:
            xt = min(pool, key=lambda t: 3 * abs(t.yc - row_y) + 0.5 * abs(t.xc - mid)).text
    return xt, yt


def digitize(texts: list[Text], paths: list[Path], region, quantum: float,
             glyph_reader: GlyphReader | None = None, corpus_labels: list[Text] | None = None) -> dict:
    """``region``: figure box in drawing units; ``quantum``: coordinate precision in drawing units (error floor)."""
    snap_h, snap_v = structure_lines(paths)
    leg = legend_text_ids(paths, texts, region)
    xa, ya, _ = detect_axes(texts, snap_h, snap_v, exclude=leg)
    notes: list[str] = []
    if (xa is None or ya is None) and corpus_labels:
        xa2, ya2, _ = detect_axes(texts + corpus_labels, snap_h, snap_v, exclude=leg)
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
    out_series = []
    for s in series:
        pts = s["pts"]
        if len(pts) < 2:
            continue
        xs = xa.value(pts[:, 0]) if xa else None
        ys = ya.value(pts[:, 1]) if ya else None
        ex = point_errors(xa, pts[:, 0], quantum)
        ey = point_errors(ya, pts[:, 1], quantum)
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
        for ax in (xa, ya):
            if ax is not None and ax.label_source == "LOCAL_OCR":
                flags.append("LOCAL_OCR_CALIBRATION")
        out_series.append({**{k: v for k, v in s.items() if k != "pts"}, "points": points, "flags": flags})
    return {"x_axis": xa, "y_axis": ya, "plot_box": [float(v) for v in box], "series": out_series,
            "legend": {str(k): v for k, v in legend.items()}, "legend_without_curve": legend_without_curve,
            "x_title_raw": xt, "y_title_raw": yt, "notes": notes,
            "axis_status": ("OK" if xa and ya else "X_UNCALIBRATED" if ya else "Y_UNCALIBRATED" if xa
                            else "NO_AXES")}

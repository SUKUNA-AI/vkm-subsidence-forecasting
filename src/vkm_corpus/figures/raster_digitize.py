"""Route R driver: one raster figure → series (same result shape as :func:`vkm_corpus.figures.core.digitize`)."""
from __future__ import annotations

import math

import numpy as np

from vkm_corpus.figures import raster as R
from vkm_corpus.figures.labels_ocr import OcrEngine
from vkm_corpus.figures.primitives import label_value


def markers_on_line(dark: np.ndarray, hline, band: int = 5) -> list[float]:
    """Dark blobs sitting on a horizontal axis line (benchmark dots of a profile line)."""
    x0, x1, y = hline
    yi = int(round(y))
    sl = dark[max(0, yi - band):yi + band + 1, int(x0):int(x1) + 1]
    prof = sl.sum(0).astype(float)
    base = np.median(prof)
    idx = np.where(prof >= base + 2)[0]
    return R._runs_centres(idx, int(x0))


# Series labels of route R (fd-0.1.5, MODEL_CHOICE): the OCR of a chart reads curve marks and broken glyphs as words
# («\m», «|ho», «I~», «/N» named the series of VKM-SRC-012 p.46). A worded label has at least LABEL_MIN_LETTERS
# letters and at most LABEL_MAX_OTHER of its characters other than letters, digits and the punctuation of labels; a
# number standing in a row of three or more numbers on one line is a tick label wherever it is.
LABEL_MIN_LETTERS, LABEL_MAX_OTHER = 3, 0.2
_LABEL_PUNCT = frozenset(" .,-–−%/()№=+:")


def label_word(text: str) -> bool:
    """A word that may label a series (:data:`LABEL_MIN_LETTERS`)."""
    s = text.strip()
    if not s:
        return False
    letters = sum(ch.isalpha() for ch in s)
    other = sum(not (ch.isalnum() or ch in _LABEL_PUNCT) for ch in s)
    return letters >= LABEL_MIN_LETTERS and other <= LABEL_MAX_OTHER * len(s)


def numeric_rows(words) -> set:
    """ids of the numeric words that stand in a row of three or more numbers (same line within half a height)."""
    nums = [t for t in words if label_value(t.text)[0] is not None]
    out: set = set()
    for t in nums:
        row = [u for u in nums if abs(u.yc - t.yc) <= 0.5 * max(t.h, u.h)]
        if len(row) >= 3:
            out.add(id(t))
    return out


def _box_from(xa, ya, hl, vl, shape, words=()):
    """Plot area of a raster chart: the label spans, extended by axis/frame lines that overlap them, bounded by the
    value axis line. fd-0.1.5: the value axis line is the line *nearest* the first (last) labelled tick — not the last
    of several lines met while walking inwards (a zone boundary at x = 20 cut off the chart's 0…20) — and the x span
    reaches the y axis line left of the first label read (labels 0…10 unread: the chart starts at the axis, not at
    15); a line with tick labels between it and the span is not that axis. Without such a line the span stays at the
    first label read (extending it by whole tick steps changed the length thresholds of the tracing, which are shares
    of the box width, and lost a usable fragment of VKM-SRC-012 p.47 — not done)."""
    h, w = shape
    xs = list(xa.span) if xa is not None else [0.0, float(w - 1)]
    ys = list(ya.span) if ya is not None else [0.0, float(h - 1)]
    # extend by axis/frame lines that overlap the label spans (within half a span beyond them)
    wx, wy = xs[1] - xs[0], ys[1] - ys[0]
    for x0, x1, y in hl:
        if x1 - x0 >= 0.4 * wx and ys[0] - 0.05 * wy <= y <= ys[1] + 0.05 * wy \
                and x0 >= xs[0] - 0.5 * wx and x1 <= xs[1] + 0.5 * wx:
            xs = [min(xs[0], x0), max(xs[1], x1)]
    for y0, y1, x in vl:
        if y1 - y0 >= 0.4 * wy and xs[0] - 0.05 * wx <= x <= xs[1] + 0.05 * wx \
                and y0 >= ys[0] - 0.5 * wy and y1 <= ys[1] + 0.5 * wy:
            ys = [min(ys[0], y0), max(ys[1], y1)]
    longv = [x for y0, y1, x in vl if y1 - y0 >= 0.5 * (ys[1] - ys[0])]
    if xa is not None and ya is not None:
        # the y axis line left of the first x label read (the labels between it and the span were not read): the
        # leftmost long line right of the y label column, within half the x span (rightmost for a right-hand axis)
        col = [t for t in words for lab in ya.labels
               if t.text == lab[0] and abs(t.yc - float(lab[2])) <= max(t.h, 3.0)]
        if col:
            c0, c1 = min(t.x0 for t in col), max(t.x1 for t in col)
            half = 0.5 * (xs[1] - xs[0])
            if c1 < xs[0]:
                left = [x for x in longv if c1 < x < xs[0] and x >= xs[0] - half]
                if left:
                    xs[0] = min(left)
            elif c0 > xs[1]:
                right = [x for x in longv if xs[1] < x < c0 and x <= xs[1] + half]
                if right:
                    xs[1] = max(right)
    # the value axis line bounds the plot: nothing left of a left axis (or right of a right one) is data — the line
    # nearest the edge within 15 % of the span
    span = xs[1] - xs[0]
    inner_left = [x for x in longv if xs[0] <= x <= xs[0] + 0.15 * span]
    inner_right = [x for x in longv if xs[1] - 0.15 * span <= x <= xs[1]]
    x0n = min(inner_left) if inner_left else xs[0]
    x1n = max(inner_right) if inner_right else xs[1]
    return x0n, ys[0], x1n, ys[1]


def digitize_raster(fig: R.RasterFigure, engine: OcrEngine, corpus_words=None, sample_markers: bool = True) -> dict:
    """``corpus_words``: tick/curve labels the corpus already has for the region (pixel frame); without them the
    local OCR helper reads the figure (its provenance is recorded on the axes)."""
    import cv2

    rgb = fig.rgb
    dark, grey, coloured, bg, chroma, lab = R.masks(rgb)
    hl, vl = R.long_lines(dark | grey, 0.3)
    hl, vl = R.extend_and_merge(dark | grey, hl, vl)
    words = list(corpus_words or [])
    label_source = "CORPUS_TEXT" if words else "LOCAL_OCR"
    if not words:
        words = R.ocr_words(rgb, engine)
    if words:   # sparse OCR sometimes returns a "word" spanning half the plot: implausible boxes are dropped
        hmed_all = float(np.median([t.h for t in words]))
        words = [t for t in words if t.h <= 2.5 * hmed_all and (t.x1 - t.x0) <= 0.25 * rgb.shape[1]]
    num_h = [t.h for t in words if label_value(t.text)[0] is not None]
    text_h = float(np.median(num_h)) if num_h else 0.012 * rgb.shape[0] * 2
    axis_words = R.ocr_axis_strips(rgb, coloured, hl, vl, engine, text_h) if label_source == "LOCAL_OCR" else []
    # snapping: tick marks along the axis lines and grid lines
    snap_x = [x for _, _, x in vl]
    snap_y = [y for _, _, y in hl]
    for line in hl:
        snap_x += R.tick_positions(dark, line, True)
    for line in vl:
        snap_y += R.tick_positions(dark, line, False)
    cal_words = axis_words + [t for t in words if not any(abs(t.xc - a.xc) < a.h and abs(t.yc - a.yc) < a.h
                                                          for a in axis_words)]
    extent = (float(rgb.shape[1]), float(rgb.shape[0]))
    xa, ya = R.calibrate(cal_words, snap_x, snap_y, None, engine if label_source == "LOCAL_OCR" else None,
                         extent=extent)
    if axis_words:
        # the strips' readings replace the sparse reading at the same place; a strip that reads fragments (blurred
        # small labels) must not displace good words — the axis from the sparse words alone, or from the sparse words
        # completed by the strips, wins when it keeps more labels
        alt = words + [a for a in axis_words if not any(abs(t.xc - a.xc) < a.h and abs(t.yc - a.yc) < a.h
                                                         for t in words)]
        def rank(ax):     # a straight scale before a piecewise one, then more labels
            return (ax.kind not in ("PIECEWISE", "MARKERS"), len(ax.labels))
        for variant in (words, alt):
            xb, yb = R.calibrate(variant, snap_x, snap_y, None, engine, extent=extent)
            xa = xb if xb is not None and (xa is None or rank(xb) > rank(xa)) else xa
            ya = yb if yb is not None and (ya is None or rank(yb) > rank(ya)) else ya
    if xa is None and label_source == "LOCAL_OCR":
        # date labels printed rotated under the plot (fd-0.1.5): the band below the lowest y label, else the lower
        # 40 % of the figure
        tops = ([max(ya.span) + 0.5 * text_h] if ya is not None else []) + [0.6 * rgb.shape[0]]
        for top in tops:
            xa = R.rotated_date_axis(rgb, coloured, engine, (0, top, rgb.shape[1], rgb.shape[0]), snap_x)
            if xa is not None:
                break
    # profile-line axis: benchmark markers on a horizontal axis line, numbered by the printed labels next to it
    marker_line, markers = None, []
    if sample_markers:
        for ln in hl:
            mk = [m for m in markers_on_line(dark, ln) if ln[0] - 2 <= m <= ln[1] + 2]
            near = [t for t in cal_words if abs(t.yc - ln[2]) <= 3.5 * text_h]
            ax = R.marker_axis("x", mk, near)
            if ax is not None and (marker_line is None or len(mk) > len(markers)):
                marker_line, markers, xm = ln, mk, ax
        if marker_line is not None:
            xa = xm
            if label_source == "LOCAL_OCR":
                xa.ocr = engine.provenance()
    box = _box_from(xa, ya, hl, vl, dark.shape, cal_words)
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    inset = 3
    plot = np.zeros(dark.shape, bool)
    plot[y0 + inset:y1 - inset + 1, x0 + inset:x1 - inset + 1] = True
    textmask = np.zeros(dark.shape, np.uint8)
    for t in words:
        cv2.rectangle(textmask, (int(t.x0) - 2, int(t.yc - t.h * 0.7) - 2), (int(t.x1) + 2, int(t.yc + t.h * 0.7) + 2),
                      255, -1)
    # filled label boxes (one colour, text inside) are not curves (fd-0.1.5)
    fill = R.filled_areas(coloured & plot, lab, dark=dark)
    curve_mask = coloured & plot & (textmask == 0) & ~fill
    # remove tiny specks
    n, cc, stats, _ = cv2.connectedComponentsWithStats(curve_mask.astype(np.uint8), 8)
    keep = np.zeros(n, bool)
    keep[1:] = stats[1:, cv2.CC_STAT_AREA] >= 12
    curve_mask = keep[cc]
    # inline / legend labels by colour of the word's ink (tick labels excluded)
    word_colour = []
    in_rows = numeric_rows(words)
    for t in words:
        v, k = label_value(t.text)
        if v is None and not label_word(t.text):
            continue          # OCR garbage («\m», «|ho»), not a label (fd-0.1.5)
        if v is not None and id(t) in in_rows:
            continue          # a number in a row of numbers is a tick label, also inside the plot
        if not (x0 <= t.xc <= x1 and y0 <= t.yc <= y1) and v is not None and not ("." in t.text and k == "DATE"):
            continue          # numbers outside the plot are tick labels, not curve labels (legend dates stay)
        xa0, xa1 = int(max(0, t.x0)), int(min(dark.shape[1] - 1, t.x1))
        ya0, ya1 = int(max(0, t.yc - t.h * 0.7)), int(min(dark.shape[0] - 1, t.yc + t.h * 0.7))
        sub = coloured[ya0:ya1 + 1, xa0:xa1 + 1] & ~fill[ya0:ya1 + 1, xa0:xa1 + 1]   # a box's fill is not ink
        if sub.sum() < 5:
            continue
        mean = lab[ya0:ya1 + 1, xa0:xa1 + 1][sub].mean(0)
        word_colour.append((t, mean))
    markers = sorted(markers) if marker_line is not None else []
    native =fig.native_px_per_pt / fig.px_per_pt if fig.native_px_per_pt else 1.0   # native px per analysed px
    half_native = 0.5 / native if native else 0.5
    # multi-curve tracking over all curve pixels (colour is a secondary cost), then fragments are joined
    jump = max(6.0, 0.03 * (y1 - y0))
    tracks = R.track_curves(curve_mask, lab, x0, x1, max_jump=jump, max_run=0.25 * (y1 - y0))
    tracks = [t for t in tracks if len(t["xs"]) >= 8]
    bridges = [(t.x0 - 2, t.x1 + 2, t.yc - t.h, t.yc + t.h) for t in words if x0 <= t.xc <= x1 and y0 <= t.yc <= y1]
    tracks = R.join_tracks(tracks, max_gap_x=max(12.0, 0.03 * (x1 - x0)), max_dy=max(8.0, 0.02 * (y1 - y0)),
                           bridges=bridges)
    traced = []
    for t in tracks:
        if len(t["xs"]) < 0.1 * (x1 - x0):
            continue
        traced.append({"centre": t["colour"], "xs": t["xs"], "ys": t["ys"], "width": t["width"],
                       "n": int(len(t["xs"])), "gaps": t.get("gaps", [])})
    # fragments of one series (split by data markers or crossings) share the ink colour and do not overlap in x
    merged_any = True
    while merged_any:
        merged_any = False
        for i in range(len(traced)):
            for j in range(i + 1, len(traced)):
                a, b = traced[i], traced[j]
                if R.de_lab(a["centre"], b["centre"]) > 10:
                    continue
                lo, hi = max(a["xs"].min(), b["xs"].min()), min(a["xs"].max(), b["xs"].max())
                if hi - lo > 0.1 * min(np.ptp(a["xs"]), np.ptp(b["xs"])):
                    continue
                order = np.argsort(np.concatenate([a["xs"], b["xs"]]), kind="stable")
                a["xs"] = np.concatenate([a["xs"], b["xs"]])[order]
                a["ys"] = np.concatenate([a["ys"], b["ys"]])[order]
                a["gaps"] = a["gaps"] + b["gaps"] + [(float(min(a["xs"].max(), b["xs"].max())),
                                                      float(max(a["xs"].min(), b["xs"].min())))]
                a["n"] += b["n"]
                traced.pop(j)
                merged_any = True
                break
            if merged_any:
                break
    traced = [t for t in traced if len(t["xs"]) >= 0.15 * (x1 - x0)]
    if traced:
        # a trace three times thicker than the typical stroke (and over 12 px) runs along a filled label box or a
        # shaded band, not a curve (fd-0.1.5: the yellow site labels of a profile were "series")
        wmed = float(np.median([t["width"] for t in traced]))
        traced = [t for t in traced if not (t["width"] > 3.0 * wmed and t["width"] > 12.0)]

    # labels: an inline label sits on (or right next to) its curve; the geometric distance from the label centre to
    # the curve decides, the ink colour breaks ties
    def label_cost(t: dict, word, wc) -> float:
        xs, ys = t["xs"], t["ys"]
        de = R.de_lab(wc, t["centre"])
        if word.x0 - 3 * word.h <= xs.max() and word.x1 + 3 * word.h >= xs.min():
            cx = min(max(word.xc, xs.min()), xs.max())
            gy = abs(float(np.interp(cx, xs, ys)) - word.yc)
            if gy <= 1.5 * word.h:
                return gy / max(1.0, word.h) + de / 100.0
        return 10.0 + de / 10.0
    pairs = sorted(((label_cost(t, w, wc), wi, ti)
                    for wi, (w, wc) in enumerate(word_colour) for ti, t in enumerate(traced)), key=lambda p: p[0])
    label_of: dict[int, str] = {}
    used_words: set[int] = set()
    for cost, wi, ti in pairs:
        if cost > 12.0 or wi in used_words or ti in label_of:
            continue
        v, k = label_value(word_colour[wi][0].text)
        if cost >= 10.0 and v is not None and k != "DATE":
            continue          # a number names only the curve it stands beside; by colour alone, only a legend date
        label_of[ti] = word_colour[wi][0].text
        used_words.add(wi)
    series = []
    # re-plot QC: all traced curves redrawn with their stroke width against the curve-pixel mask
    fig_qc = R.overlay_qc_all(curve_mask, [(t["xs"], t["ys"], t["width"]) for t in traced])
    for ti, tr in enumerate(traced):
        c, xs, ys, width = tr["centre"], tr["xs"], tr["ys"], tr["width"]
        qc = R.track_qc(curve_mask, xs, ys, width)
        lbl = label_of.get(ti)
        # sampling: at benchmark markers when present (only where the curve has ink within 2 px — a gap is not
        # filled), else the centreline thinned to ~1 point per 2 native px
        n_gap_markers = 0
        if markers:
            inside = [mk for mk in markers if xs.min() <= mk <= xs.max()]
            px = np.array([mk for mk in inside if np.min(np.abs(xs - mk)) <= 2.0])
            n_gap_markers = len(inside) - len(px)
            py = np.interp(px, xs, ys) if len(px) else np.array([])
            sampling = "AT_AXIS_MARKERS"
        else:
            mk = R.series_markers(curve_mask, lab, c, xs, ys, width)
            vidx = R.polyline_vertices(xs, ys, width, x1 - x0)
            if len(mk) >= 5 and (max(m[0] for m in mk) - min(m[0] for m in mk)) >= 0.5 * np.ptp(xs):
                px = np.array([m[0] for m in mk])
                py = np.array([m[1] for m in mk])
                sampling = "AT_SERIES_MARKERS"
            elif vidx is not None:
                px, py = xs[vidx], ys[vidx]
                sampling = "POLYLINE_VERTICES"
            else:
                step = max(1, int(round(2 / native))) if native else 2
                px, py = xs[::step], ys[::step]
                sampling = "CENTRELINE"
        xv = xa.value(px) if xa is not None else None
        yv = ya.value(py) if ya is not None else None
        points = []
        for i in range(len(px)):
            ex = None if xa is None else float(xa.value_per_pos(px[i]) * math.hypot(xa.residual_rms, half_native))
            ey = None if ya is None else float(ya.value_per_pos(py[i]) *
                                               math.hypot(ya.residual_rms, half_native, 0.5 * width))
            points.append({"i": i, "x": None if xv is None or np.isnan(xv[i]) else float(xv[i]),
                           "y": None if yv is None else float(yv[i]), "x_err": ex, "y_err": ey,
                           "x_drawing": float(px[i]), "y_drawing": float(py[i])})
        rgbc = cv2.cvtColor(np.uint8([[c]]), cv2.COLOR_LAB2RGB)[0, 0]
        flags = ["RASTER"] + (["LOCAL_OCR_CALIBRATION"] if label_source == "LOCAL_OCR" else []) + \
                ([] if lbl else ["LEGEND_UNMATCHED"]) + (["X_UNCALIBRATED"] if xa is None else []) + \
                (["Y_UNCALIBRATED"] if ya is None else []) + (["TRACE_GAP_NOT_FILLED"] if n_gap_markers or
                                                              tr["gaps"] else [])
        series.append({"color": "#%02x%02x%02x" % tuple(int(v) for v in rgbc), "label_raw": lbl,
                       "stroke_width_px": width, "sampling": sampling, "qc": qc, "n_columns": int(len(xs)),
                       "gaps_px": tr["gaps"], "markers_in_gaps": n_gap_markers,
                       "points": points, "flags": flags, "trace_px": (xs, ys)})
    return {"x_axis": xa, "y_axis": ya, "plot_box": [float(v) for v in box], "series": series, "qc": fig_qc,
            "words": cal_words, "markers": markers, "label_source": label_source,
            "native_px_per_analysed_px": native,
            "axis_status": "OK" if xa is not None and ya is not None else
            "X_UNCALIBRATED" if ya is not None else "Y_UNCALIBRATED" if xa is not None else "NO_AXES",
            "lines": {"h": hl, "v": vl}}

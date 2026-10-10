"""Axis calibration from printed tick labels: rows of numeric/date labels → x axis, columns → y axis; linear, log10 or
date scales; label positions snapped to tick marks / grid lines when they are drawn; residuals are reported.

fd-0.1.5 (review of 08.10): a label snaps to a tick or grid line *beside it* (within ``SNAP_ACROSS_H`` label heights
across the axis, the nearest across first) — not to a legend sample or another panel's axis that only shares its
coordinate; a label left unsnapped is snapped once more at the position its snapped neighbours predict (the «0» of
a corner set off its tick); and the x and y axes are chosen as the corner of one chart when the best-by-count pair
belongs to two panels (:func:`pair_axes`)."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from vkm_corpus.figures.primitives import Path, Text, is_black, is_greyish, label_value


@dataclass
class Axis:
    orient: str                 # "x": values along the drawing x; "y": along the drawing y
    kind: str                   # LINEAR | LOG10 | DATE (decimal years)
    a: float                    # value = a + b·pos  (LOG10: log10(value) = a + b·pos)
    b: float
    labels: list = field(default_factory=list)      # [text, value, pos, snapped, source]
    residual_rms: float = 0.0   # drawing units
    residual_max: float = 0.0
    method: str = ""            # SNAPPED_TO_TICKS | PARTLY_SNAPPED | TEXT_CENTRE | OCR_...
    dropped: list = field(default_factory=list)     # labels removed as outliers
    label_source: str = "NATIVE"
    ocr: dict | None = None
    # the text items of ``labels`` (same order; empty for labels read from glyph outlines) — geometry only, never
    # serialised
    texts: list = field(default_factory=list, repr=False, compare=False)

    def value(self, pos):
        v = self.a + self.b * np.asarray(pos, dtype=float)
        return np.power(10.0, v) if self.kind == "LOG10" else v

    def pos(self, value):
        v = np.log10(value) if self.kind == "LOG10" else np.asarray(value, dtype=float)
        return (v - self.a) / self.b

    def value_per_pos(self, pos) -> float:
        """|d value / d pos| at ``pos`` — converts drawing-unit errors into value units."""
        if self.kind == "LOG10":
            return abs(self.b * math.log(10.0) * float(self.value(pos)))
        return abs(self.b)

    @property
    def span(self) -> tuple[float, float]:
        ps = [lab[2] for lab in self.labels]
        return min(ps), max(ps)

    def to_json(self) -> dict:
        return {"orient": self.orient, "kind": self.kind, "a": self.a, "b": self.b, "method": self.method,
                "label_source": self.label_source, "ocr": self.ocr, "n_labels": len(self.labels),
                "residual_rms_drawing_units": self.residual_rms, "residual_max_drawing_units": self.residual_max,
                "labels": [{"text": t, "value": v, "pos": p, "snapped": s, "source": src}
                           for t, v, p, s, src in self.labels],
                "dropped_labels": self.dropped}


# ------------------------------------------------------------------------------------------------ structure lines
# A segment is horizontal (vertical) when its minor extent is at most SLOPE_TOL of its major extent: scale-free,
# so the same rule holds for DXF inches and PDF points (PDF writers round tick marks to 0.1 pt: 376.1 → 376.2).
SLOPE_TOL = 0.03


def _seg_dir(a, b) -> str | None:
    dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
    if dx > 1e-9 and dy <= SLOPE_TOL * dx:
        return "H"
    if dy > 1e-9 and dx <= SLOPE_TOL * dy:
        return "V"
    return None


def is_axis_aligned_segment(p: Path) -> str | None:
    if len(p.pts) != 2:
        return None
    return _seg_dir(p.pts[0], p.pts[1])


def is_manhattan(p: Path) -> bool:
    """All segments horizontal or vertical: axis lines with tick marks, grid lines as polylines, frames."""
    if len(p.pts) < 2 or p.kind not in ("POLY", "LINE"):
        return False
    for a, b in zip(p.pts[:-1], p.pts[1:]):
        if (abs(b[0] - a[0]) > 1e-9 or abs(b[1] - a[1]) > 1e-9) and _seg_dir(a, b) is None:
            return False
    return True


def is_rect(p: Path) -> bool:
    if len(p.pts) not in (4, 5):
        return False
    span = float(np.max(np.ptp(p.pts, 0))) or 1.0
    tol = 0.01 * span
    ux = np.unique(np.round(p.pts[:, 0] / tol))
    uy = np.unique(np.round(p.pts[:, 1] / tol))
    return len(ux) <= 2 and len(uy) <= 2


def manhattan_segments(p: Path):
    """Horizontal (x0, x1, y) and vertical (y0, y1, x) segments of a path (y/x at the segment's middle)."""
    hs, vs = [], []
    for a, b in zip(p.pts[:-1], p.pts[1:]):
        d = _seg_dir(a, b)
        if d == "H":
            hs.append((float(min(a[0], b[0])), float(max(a[0], b[0])), float((a[1] + b[1]) / 2)))
        elif d == "V":
            vs.append((float(min(a[1], b[1])), float(max(a[1], b[1])), float((a[0] + b[0]) / 2)))
    return hs, vs


def structure_segments(paths: list[Path], neutral_only: bool = False):
    hs, vs = [], []
    for p in paths:
        if p.kind == "FILL":
            continue
        if neutral_only and not (is_black(p.color) or is_greyish(p.color)):
            continue
        if is_manhattan(p) or is_rect(p):
            h, v = manhattan_segments(p)
            hs += h
            vs += v
    return hs, vs


def structure_lines(paths: list[Path]):
    """y of horizontal and x of vertical straight pieces — candidates for snapping tick labels."""
    hs, vs = structure_segments(paths)
    return [s[2] for s in hs], [s[2] for s in vs]


# ------------------------------------------------------------------------------------------------ fitting
def _fit(values, pos, kind):
    v = np.log10(values) if kind == "LOG10" else np.asarray(values, float)
    p = np.asarray(pos, float)
    A = np.vstack([np.ones_like(p), p]).T
    (a, b), *_ = np.linalg.lstsq(A, v, rcond=None)
    if b == 0 or not np.isfinite(b):
        return a, b, np.full_like(p, np.inf)
    return a, b, p - (v - a) / b


# A log10 scale is tried only when the positive labels span at least this ratio (fd-0.1.4): over a narrow range far
# from zero (years 1998…2003, 77 900…78 020) log and linear fits are indistinguishable and the log fit won by chance.
LOG_MIN_RATIO = 3.0


def fit_axis(orient: str, labels: list, kind0: str, hmed: float, min_labels: int = 3, max_drop: int = 2,
             method_hint: str | None = None, label_source: str = "NATIVE") -> Axis | None:
    """``labels``: [text, value, pos, snapped, source]. Linear first, then log10 (positive values spanning at least
    ``LOG_MIN_RATIO``); up to ``max_drop`` outliers are dropped (a label of another element that happens to sit in the
    row)."""
    labels = list(labels)
    dropped = []
    lo, hi = min(lab[1] for lab in labels), max(lab[1] for lab in labels)
    trials = ["LINEAR"] if kind0 == "DATE" or lo <= 0 or hi / lo < LOG_MIN_RATIO else ["LINEAR", "LOG10"]
    while len(labels) >= min_labels and len({lab[1] for lab in labels}) >= min_labels:
        best = None
        for kind in trials:
            vals = [lab[1] for lab in labels]
            pos = [lab[2] for lab in labels]
            a, b, res = _fit(vals, pos, kind)
            if not np.all(np.isfinite(res)):
                continue
            spacing = float(np.median(np.diff(sorted(pos)))) if len(pos) > 1 else 1.0
            tol = max(0.3 * hmed, 0.08 * abs(spacing))
            mx = float(np.max(np.abs(res)))
            cand = (mx <= tol, -mx, kind, a, b, res)
            if best is None or cand[:2] > best[:2]:
                best = cand
        if best is None:
            return None
        ok, _, kind, a, b, res = best
        if ok:
            snapped = [lab[3] for lab in labels]
            method = method_hint or ("SNAPPED_TO_TICKS" if all(snapped) else "PARTLY_SNAPPED" if any(snapped)
                                     else "TEXT_CENTRE")
            return Axis(orient, "DATE" if kind0 == "DATE" else kind, float(a), float(b), labels,
                        float(np.sqrt(np.mean(res ** 2))), float(np.max(np.abs(res))), method, dropped,
                        label_source)
        if len(dropped) >= max_drop or len(labels) - 1 < min_labels:
            return None
        worst = int(np.argmax(np.abs(res)))
        dropped.append(labels.pop(worst)[0])
    return None


def _clusters(items, key, tol):
    items = sorted(items, key=key)
    out, cur = [], []
    for it in items:
        if cur and abs(key(it) - key(cur[-1])) > tol:
            out.append(cur)
            cur = []
        cur.append(it)
    if cur:
        out.append(cur)
    return out


def _snap(p: float, lines: list[float], h: float) -> float | None:
    if not lines:
        return None
    arr = np.asarray(lines)
    j = int(np.argmin(np.abs(arr - p)))
    return float(arr[j]) if abs(arr[j] - p) <= 0.45 * h else None


# Snapping beside the label (fd-0.1.5, MODEL_CHOICE): a tick mark, an axis or a grid line runs up to the label, so
# the line a label snaps to must come within SNAP_ACROSS_H label heights of the label's box across the axis; of the
# lines within 0.45 label heights along it, the nearest across wins (lines up to SNAP_TIE_PT farther count as
# equally near), then the nearest along. A legend sample or a curve piece inside the plot, or the axis of a
# neighbouring panel, that only shares the label's coordinate is not its tick. After the fit, a label with several
# such lines takes the one its neighbours predict (a tick and a grid line drawn 0.3 pt apart). A label left unsnapped
# is tried once more at the position its snapped neighbours predict when its text centre is within RESNAP_H label
# heights of it (the «0» of a chart corner set off its tick, under the other axis).
SNAP_ACROSS_H, SNAP_TIE_PT, RESNAP_H, RESNAP_ALONG_H = 4.0, 1.0, 1.5, 0.3


def _segment_arrays(segments):
    """(horizontal, vertical) structure segments → two float arrays (n × 3: from, to, at) or None."""
    if segments is None:
        return None
    hs, vs = segments
    return (np.asarray(hs, float).reshape(-1, 3), np.asarray(vs, float).reshape(-1, 3))


def _label_box_across(t: Text, orient: str) -> tuple[float, float]:
    """The extent of a label across its axis: top and bottom of an x label, left and right of a y label."""
    if orient == "x":
        half = t.h / 1.4                         # Text.h is 0.7 of the word box height
        return t.yc - half, t.yc + half
    return t.x0, t.x1


def snap_options(t: Text, orient: str, segs: np.ndarray, at: float | None = None,
                 along_h: float = 0.45) -> np.ndarray:
    """Lines a label may snap to, best first: segments (from, to, at) perpendicular to the axis within ``along_h``
    label heights of the label position (or of ``at``) that come within ``SNAP_ACROSS_H`` label heights of the label
    box across the axis, ordered by distance across (ties within ``SNAP_TIE_PT``), then along. Their coordinates."""
    if segs is None or not len(segs):
        return np.zeros(0)
    p = (t.xc if orient == "x" else t.yc) if at is None else at
    d_along = np.abs(segs[:, 2] - p)
    lo, hi = _label_box_across(t, orient)
    across = np.maximum(0.0, np.maximum(segs[:, 0] - hi, lo - segs[:, 1]))
    ok = (d_along <= along_h * t.h) & (across <= SNAP_ACROSS_H * t.h)
    if not ok.any():
        return np.zeros(0)
    tie = max(SNAP_TIE_PT, 0.15 * t.h)
    m = float(across[ok].min())
    idx = np.where(ok)[0]
    out: list[float] = []
    for j in sorted(idx, key=lambda j: (bool(across[j] > m + tie), float(d_along[j]), float(across[j]))):
        c = float(segs[j, 2])
        if not any(abs(c - o) <= 1e-6 for o in out):
            out.append(c)
    return np.array(out)


def snap_beside(t: Text, orient: str, segs: np.ndarray, at: float | None = None,
                along_h: float = 0.45) -> float | None:
    """The tick/grid line of label ``t`` (:func:`snap_options`, the first); None: no such line."""
    opts = snap_options(t, orient, segs, at, along_h)
    return float(opts[0]) if len(opts) else None


def detect_axes(texts: list[Text], snap_h: list[float], snap_v: list[float], min_labels: int = 3,
                exclude: set | frozenset = frozenset(), segments=None):
    """Best x axis (a row of numeric or date labels) and y axis (a column), with all candidates. ``segments``
    (horizontal, vertical structure segments with their extents, :func:`structure_segments`): labels snap only to
    lines beside them (:func:`snap_beside`); without them, to any line at their coordinate (``snap_h``/``snap_v``)."""
    lab = []
    for t in texts:
        if id(t) in exclude or abs(t.rot) >= 1:
            continue
        v, k = label_value(t.text)
        if v is not None:
            lab.append((t, v, k))
    if not lab:
        return None, None, []
    hmed = float(np.median([t.h for t, _, _ in lab]))
    segs = _segment_arrays(segments)
    cands: list[Axis] = []
    for row in _clusters(lab, key=lambda r: r[0].yc, tol=0.35 * hmed):
        cands += _candidates(row, "x", snap_v, hmed, min_labels, None if segs is None else segs[1])
    seen = set()
    for keyf in (lambda r: r[0].x1, lambda r: r[0].xc, lambda r: r[0].x0):
        for col in _clusters(lab, key=keyf, tol=0.6 * hmed):
            ids = tuple(sorted(id(r[0]) for r in col))
            if ids not in seen:
                seen.add(ids)
                cands += _candidates(col, "y", snap_h, hmed, min_labels, None if segs is None else segs[0])
    xs = [c for c in cands if c.orient == "x"]
    ys = [c for c in cands if c.orient == "y"]

    def best(cs):
        return max(cs, key=lambda c: (len(c.labels), -c.residual_rms)) if cs else None
    return best(xs), best(ys), cands


def _candidates(group, orient, snap_lines, hmed, min_labels, segs=None):
    kinds = {k for _, _, k in group}
    if len(group) < min_labels or len(kinds) != 1:
        return []
    kind0 = kinds.pop()
    labels, text_of = [], {}
    for t, v, _ in group:
        p = t.xc if orient == "x" else t.yc
        q = _snap(p, snap_lines, t.h) if segs is None else snap_beside(t, orient, segs)
        row = [t.text.strip(), v, q if q is not None else p, q is not None, t.source]
        labels.append(row)
        text_of[id(row)] = t
    ax = fit_axis(orient, labels, kind0, hmed, min_labels, label_source=group[0][0].source)
    if ax is not None and segs is not None:
        ax = _consistent(ax, labels, text_of, orient, kind0, hmed, min_labels, segs, group[0][0].source)
        ax = _resnap(ax, labels, text_of, orient, kind0, hmed, min_labels, segs, group[0][0].source)
    if ax is None:
        return []
    ax.texts = [text_of[id(r)] for r in ax.labels]
    return [ax]


def _predict(rows: list, kind: str):
    """Position predicted for a value by the line through ``rows`` (None for fewer than two distinct values)."""
    if len({r[1] for r in rows}) < 2 or (kind == "LOG10" and any(r[1] <= 0 for r in rows)):
        return None
    a, b, _ = _fit([r[1] for r in rows], [r[2] for r in rows], kind)
    if not b or not np.isfinite(b):
        return None
    return lambda v: ((math.log10(v) if kind == "LOG10" else v) - a) / b


def _consistent(ax: Axis, labels: list, text_of: dict, orient: str, kind0: str, hmed: float, min_labels: int,
                segs: np.ndarray, source: str) -> Axis:
    """A snapped label with several lines beside it takes the line nearest to the position the other labels
    predict; the refit replaces the axis when it keeps the labels and its residual does not grow."""
    if len(ax.labels) < 3:
        return ax
    kind = "LOG10" if ax.kind == "LOG10" else "LINEAR"
    old = {id(r): (r[2], r[3]) for r in labels}
    changed = False
    for r in [r for r in ax.labels if r[3]]:
        opts = snap_options(text_of[id(r)], orient, segs)
        if len(opts) < 2:
            continue
        pred = _predict([o for o in ax.labels if o is not r], kind)
        if pred is None or (kind == "LOG10" and r[1] <= 0):
            continue
        c = float(opts[int(np.argmin(np.abs(opts - pred(r[1]))))])
        if abs(c - r[2]) > 1e-9:
            r[2] = c
            changed = True
    if not changed:
        return ax
    ax2 = fit_axis(orient, labels, kind0, hmed, min_labels, label_source=source)
    if ax2 is not None and len(ax2.labels) >= len(ax.labels) and ax2.residual_rms <= ax.residual_rms + 1e-9:
        return ax2
    for r in labels:                                    # keep the first snapping
        r[2], r[3] = old[id(r)]
    return ax


def _resnap(ax: Axis, labels: list, text_of: dict, orient: str, kind0: str, hmed: float, min_labels: int,
            segs: np.ndarray, source: str) -> Axis:
    """Snap the unsnapped labels (kept or dropped by the fit) at the positions the snapped ones predict: a line
    beside the label within ``RESNAP_ALONG_H`` label heights of the prediction, the text centre within ``RESNAP_H``
    label heights of it. The refit replaces the axis when it keeps at least as many labels."""
    snapped = [r for r in ax.labels if r[3]]
    loose = [r for r in labels if not r[3]]
    kind = "LOG10" if ax.kind == "LOG10" else "LINEAR"
    pred = _predict(snapped, kind) if len(snapped) >= 2 and loose else None
    if pred is None:
        return ax
    changed = False
    for r in loose:
        if kind == "LOG10" and r[1] <= 0:
            continue
        q = pred(r[1])
        t = text_of[id(r)]
        if abs(q - r[2]) > RESNAP_H * t.h:
            continue
        c = snap_beside(t, orient, segs, at=q, along_h=RESNAP_ALONG_H)
        if c is not None:
            r[2], r[3] = c, True
            changed = True
    if not changed:
        return ax
    ax2 = fit_axis(orient, labels, kind0, hmed, min_labels, label_source=source)
    return ax2 if ax2 is not None and len(ax2.labels) >= len(ax.labels) else ax


# ------------------------------------------------------------------------------------------------ one chart's corner
# The x and y axes of one chart meet at its corner (fd-0.1.5, MODEL_CHOICE): the x label row stands within
# CORNER_ACROSS_H label heights beyond the end of the y labels' span (below or above it) and the y label column within
# CORNER_ALONG_H label heights beside the x labels' span. When the pair chosen by label count is farther apart (two
# panels: the x labels of the panel above, the y labels of the panel beside), the pair is replaced by the best pair
# that forms a corner, among candidates with at most one label fewer. A label row inside the y span (an x axis
# through the middle of the chart) is never judged.
CORNER_ACROSS_H, CORNER_ALONG_H = 4.0, 6.0


def corner_gaps(xa: Axis | None, ya: Axis | None) -> tuple[float, float] | None:
    """(gap of the x label row beyond the y labels' span, gap of the y label column beside the x labels' span), in
    label heights; negative: inside the span. None without the label texts."""
    if xa is None or ya is None or len(xa.texts) < 2 or len(ya.texts) < 2:
        return None
    h = float(np.median([t.h for t in xa.texts + ya.texts])) or 1.0
    row = float(np.median([t.yc for t in xa.texts]))
    ylo, yhi = min(float(lab[2]) for lab in ya.labels), max(float(lab[2]) for lab in ya.labels)
    gy = row - yhi if row > yhi else ylo - row if row < ylo else -min(row - ylo, yhi - row)
    xlo, xhi = min(float(lab[2]) for lab in xa.labels), max(float(lab[2]) for lab in xa.labels)
    c0, c1 = min(t.x0 for t in ya.texts), max(t.x1 for t in ya.texts)
    gx = xlo - c1 if c1 < xlo else c0 - xhi if c0 > xhi else -min(c1 - xlo, xhi - c0)
    return gy / h, gx / h


def is_corner(g: tuple[float, float] | None) -> bool:
    return g is not None and -0.5 <= g[0] <= CORNER_ACROSS_H and -0.5 <= g[1] <= CORNER_ALONG_H


def _label_keys(ax: Axis) -> set:
    return {(lab[0], round(float(lab[2]), 1)) for lab in ax.labels}


def labels_disjoint(a: Axis, b: Axis) -> bool:
    """Fewer than half of the labels (text and position) shared: another axis, not the same labels clustered
    another way."""
    ka, kb = _label_keys(a), _label_keys(b)
    return len(ka & kb) < 0.5 * min(len(ka), len(kb))


def pair_axes(xa: Axis | None, ya: Axis | None, cands: list[Axis]) -> tuple[Axis | None, Axis | None, bool]:
    """The x and y axes as the corner of one chart: the best-by-count pair unless its label row and column stand
    farther apart than a chart corner allows and another pair (at most one label fewer on either axis) forms one;
    → (x axis, y axis, replaced)."""
    g = corner_gaps(xa, ya)
    if g is None or (g[0] <= CORNER_ACROSS_H and g[1] <= CORNER_ALONG_H):
        return xa, ya, False
    xs = [c for c in cands if c.orient == "x" and len(c.labels) >= len(xa.labels) - 1]
    ys = [c for c in cands if c.orient == "y" and len(c.labels) >= len(ya.labels) - 1]
    pairs = [(X, Y) for X in xs for Y in ys if is_corner(corner_gaps(X, Y))]
    if not pairs:
        return xa, ya, False
    X, Y = max(pairs, key=lambda p: (len(p[0].labels) + len(p[1].labels), -(p[0].residual_rms + p[1].residual_rms)))
    return X, Y, True


def other_panels(xa: Axis | None, ya: Axis | None, cands: list[Axis]) -> bool:
    """The region holds the corner of another chart: an x row and a y column, both with labels other than the chosen
    axes', that form a corner (:func:`corner_gaps`) — a figure box over several panels, of which one is digitized."""
    if xa is None or ya is None:
        return False
    xs = [c for c in cands if c.orient == "x" and len(c.labels) >= 3 and labels_disjoint(c, xa)]
    ys = [c for c in cands if c.orient == "y" and len(c.labels) >= 3 and labels_disjoint(c, ya)]
    return any(is_corner(corner_gaps(X, Y)) for X in xs for Y in ys)


# ------------------------------------------------------------------------------------------------ plot area
def plot_box(xa: Axis | None, ya: Axis | None, paths: list[Path], region, texts=(), max_overhang: float = 0.5):
    """Plot area: tick ranges extended by neutral axis/grid/frame lines that overlap them, overhang by at most
    ``max_overhang`` of the range and do not swallow non-tick text (the chart border and the legend stay out)."""
    hsg, vsg = structure_segments(paths, neutral_only=True)
    tick_texts = set()
    for ax in (xa, ya):
        if ax:
            tick_texts |= {lab[0] for lab in ax.labels}
    other = [t for t in texts if t.text.strip() not in tick_texts]

    def swallows(x0, y0, x1, y1):
        return any(x0 < t.xc < x1 and y0 < t.yc < y1 for t in other)

    bx0, bx1 = xa.span if xa else (region[0], region[2])
    by0, by1 = ya.span if ya else (region[1], region[3])
    if xa is None:
        # no x labels: the plot spans the grid lines drawn at the y ticks (or the longest horizontal lines)
        ticks = [lab[2] for lab in ya.labels] if ya else []
        tol = 0.01 * (by1 - by0 if ya else region[3] - region[1])
        at_ticks = [s for s in hsg if any(abs(s[2] - t) <= tol for t in ticks)]
        long_h = at_ticks or [s for s in hsg if s[1] - s[0] > 0.3 * (region[2] - region[0])]
        if long_h:
            bx0, bx1 = min(s[0] for s in long_h), max(s[1] for s in long_h)
    if ya is None:
        ticks = [lab[2] for lab in xa.labels] if xa else []
        tol = 0.01 * (bx1 - bx0)
        at_ticks = [s for s in vsg if any(abs(s[2] - t) <= tol for t in ticks)]
        long_v = at_ticks or [s for s in vsg if s[1] - s[0] > 0.3 * (region[3] - region[1])]
        if long_v:
            by0, by1 = min(s[0] for s in long_v), max(s[1] for s in long_v)
    wx, wy = bx1 - bx0, by1 - by0
    ex0, ex1, ey0, ey1 = bx0, bx1, by0, by1
    # extension lines must lie within the plot's span across them (an axis at the edge, a grid line inside);
    # the chart border lies beyond the tick labels and is not taken
    for x0, x1, y in hsg:
        if x1 - x0 >= 0.4 * wx and by0 - 0.02 * wy <= y <= by1 + 0.02 * wy \
                and x0 >= bx0 - max_overhang * wx and x1 <= bx1 + max_overhang * wx:
            nx0, nx1 = min(ex0, x0), max(ex1, x1)
            if not swallows(nx0, by0, ex0, by1) and not swallows(ex1, by0, nx1, by1):
                ex0, ex1 = nx0, nx1
    for y0, y1, x in vsg:
        if y1 - y0 >= 0.4 * wy and bx0 - 0.02 * wx <= x <= bx1 + 0.02 * wx \
                and y0 >= by0 - max_overhang * wy and y1 <= by1 + max_overhang * wy:
            ny0, ny1 = min(ey0, y0), max(ey1, y1)
            if not swallows(bx0, ny0, bx1, ey0) and not swallows(bx0, ey1, bx1, ny1):
                ey0, ey1 = ny0, ny1
    return ex0, ey0, ex1, ey1

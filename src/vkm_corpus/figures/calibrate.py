"""Axis calibration from printed tick labels: rows of numeric/date labels → x axis, columns → y axis; linear, log10 or
date scales; label positions snapped to tick marks / grid lines when they are drawn; residuals are reported."""
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


def fit_axis(orient: str, labels: list, kind0: str, hmed: float, min_labels: int = 3, max_drop: int = 2,
             method_hint: str | None = None, label_source: str = "NATIVE") -> Axis | None:
    """``labels``: [text, value, pos, snapped, source]. Linear first, then log10 (positive values); up to
    ``max_drop`` outliers are dropped (a label of another element that happens to sit in the row)."""
    labels = list(labels)
    dropped = []
    trials = ["LINEAR"] if kind0 == "DATE" or min(lab[1] for lab in labels) <= 0 else ["LINEAR", "LOG10"]
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


def detect_axes(texts: list[Text], snap_h: list[float], snap_v: list[float], min_labels: int = 3,
                exclude: set | frozenset = frozenset()):
    """Best x axis (a row of numeric or date labels) and y axis (a column), with all candidates."""
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
    cands: list[Axis] = []
    for row in _clusters(lab, key=lambda r: r[0].yc, tol=0.35 * hmed):
        cands += _candidates(row, "x", snap_v, hmed, min_labels)
    seen = set()
    for keyf in (lambda r: r[0].x1, lambda r: r[0].xc, lambda r: r[0].x0):
        for col in _clusters(lab, key=keyf, tol=0.6 * hmed):
            ids = tuple(sorted(id(r[0]) for r in col))
            if ids not in seen:
                seen.add(ids)
                cands += _candidates(col, "y", snap_h, hmed, min_labels)
    xs = [c for c in cands if c.orient == "x"]
    ys = [c for c in cands if c.orient == "y"]

    def best(cs):
        return max(cs, key=lambda c: (len(c.labels), -c.residual_rms)) if cs else None
    return best(xs), best(ys), cands


def _candidates(group, orient, snap_lines, hmed, min_labels):
    kinds = {k for _, _, k in group}
    if len(group) < min_labels or len(kinds) != 1:
        return []
    kind0 = kinds.pop()
    labels = []
    for t, v, _ in group:
        p = t.xc if orient == "x" else t.yc
        q = _snap(p, snap_lines, t.h)
        labels.append([t.text.strip(), v, q if q is not None else p, q is not None, t.source])
    ax = fit_axis(orient, labels, kind0, hmed, min_labels, label_source=group[0][0].source)
    return [ax] if ax else []


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

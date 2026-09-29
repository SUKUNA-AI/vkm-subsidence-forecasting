"""Curve extraction from vector paths: legend samples, style groups (colour, width, dash), chaining of pieces,
data-bearing vertices (polyline vertices and Bezier joints are the plotted points of chart software)."""
from __future__ import annotations

import math

import numpy as np

from vkm_corpus.figures.calibrate import Axis, is_axis_aligned_segment, is_manhattan, is_rect
from vkm_corpus.figures.primitives import Path, Text, color_name, is_black, is_greyish


def _legend_pairs(paths: list[Path], texts: list[Text], max_len: float):
    """(sample stroke, label words) pairs: a short horizontal stroke and the words right of it on its line — the
    words continue while the gap stays under 1.2 cap heights and no other sample starts."""
    samples = []
    for p in paths:
        if p.kind == "FILL" or is_axis_aligned_segment(p) != "H":
            continue
        if float(p.pts[:, 0].max() - p.pts[:, 0].min()) <= max_len:
            samples.append(p)
    starts = [(float(q.pts[:, 0].min()), float(q.pts[:, 1].mean())) for q in samples]
    out = []
    for p in samples:
        sx1, sy = float(p.pts[:, 0].max()), float(p.pts[:, 1].mean())
        line = sorted((t for t in texts if abs(t.yc - sy) <= 0.7 * t.h and t.x0 - sx1 >= -0.3 * t.h),
                      key=lambda t: t.x0)
        if not line or line[0].x0 - sx1 > 3.0 * line[0].h:
            continue
        words = [line[0]]
        for t in line[1:]:
            nxt = min((x for x, y in starts if abs(y - sy) <= 0.7 * t.h and x > words[-1].x1), default=None)
            if t.x0 - words[-1].x1 > 1.2 * t.h or (nxt is not None and t.x0 > nxt):
                break
            words.append(t)
        out.append((p, words))
    return out


def legend_text_ids(paths: list[Path], texts: list[Text], region) -> set:
    """ids of texts that label a legend sample (a short horizontal stroke with the label right of it)."""
    return {id(t) for _, ws in _legend_pairs(paths, texts, 0.2 * (region[2] - region[0])) for t in ws}


def style_key(p: Path):
    return (p.color if p.color is not None else -1, round(p.lw, 4), p.dashed)


def chain(pieces: list[np.ndarray], tol: float) -> list[np.ndarray]:
    """Join pieces whose end points coincide (either direction) into polylines."""
    pieces = [p for p in pieces if len(p) >= 2]
    used = [False] * len(pieces)
    heads = np.array([p[0] for p in pieces]) if pieces else np.zeros((0, 2))
    tails = np.array([p[-1] for p in pieces]) if pieces else np.zeros((0, 2))
    out = []
    for i in range(len(pieces)):
        if used[i]:
            continue
        used[i] = True
        cur = [pieces[i]]
        for forward in (True, False):
            while True:
                end = cur[-1][-1] if forward else cur[0][0]
                free = [j for j in range(len(pieces)) if not used[j]]
                if not free:
                    break
                fr = np.array(free)
                dh = np.hypot(*(heads[fr] - end).T)
                dt = np.hypot(*(tails[fr] - end).T)
                jh, jt = int(np.argmin(dh)), int(np.argmin(dt))
                if forward:
                    if dh[jh] <= tol and dh[jh] <= dt[jt]:
                        j = fr[jh]
                        cur.append(pieces[j][1:])
                    elif dt[jt] <= tol:
                        j = fr[jt]
                        cur.append(pieces[j][::-1][1:])
                    else:
                        break
                else:
                    if dt[jt] <= tol and dt[jt] <= dh[jh]:
                        j = fr[jt]
                        cur.insert(0, pieces[j][:-1])
                    elif dh[jh] <= tol:
                        j = fr[jh]
                        cur.insert(0, pieces[j][::-1][:-1])
                    else:
                        break
                used[j] = True
        out.append(np.vstack([c for c in cur if len(c)]))
    return out


def dedupe_consecutive(pts: np.ndarray, tol: float) -> np.ndarray:
    keep = [0]
    for i in range(1, len(pts)):
        if math.hypot(*(pts[i] - pts[keep[-1]])) > tol:
            keep.append(i)
    return pts[keep]


def extract_series(paths: list[Path], texts: list[Text], box, min_chain_frac: float = 0.04):
    """Series inside the plot ``box``: pieces grouped by style and chained; legend samples give the labels."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    tol = 1e-3 * max(w, h)
    margin = 0.02 * max(w, h)
    diag = math.hypot(w, h)
    legend: dict = {}
    legend_ids = set()
    for p, ws in _legend_pairs(paths, texts, 0.2 * w):
        legend.setdefault(style_key(p), []).append(" ".join(t.text.strip() for t in ws))
        legend_ids.add(id(p))
    groups: dict = {}
    marks: dict = {}
    for p in paths:
        if id(p) in legend_ids or is_rect(p):
            continue
        o = is_axis_aligned_segment(p)
        pin = ((p.pts[:, 0] >= x0 - margin) & (p.pts[:, 0] <= x1 + margin) & (p.pts[:, 1] >= y0 - margin)
               & (p.pts[:, 1] <= y1 + margin))
        if not (bool(pin.all()) or (bool(pin.any()) and o is None)):
            continue
        bx0_, by0_, bx1_, by1_ = p.bbox
        size = math.hypot(bx1_ - bx0_, by1_ - by0_)
        closed = len(p.pts) >= 3 and math.hypot(*(p.pts[0] - p.pts[-1])) <= tol * 10
        if p.kind in ("FILL", "MARK") or closed:
            # small filled or closed shapes are data markers (dots, squares, triangles), grouped by colour
            if size <= 0.04 * diag and not (is_greyish(p.color) and (p.color >> 16) >= 190):
                marks.setdefault(p.color if p.color is not None else -1, []).append(p)
            continue
        if is_greyish(p.color) and (o is not None or is_manhattan(p) or (p.color >> 16) & 255 >= 190):
            continue                     # grey grid lines, axes with ticks, frames (mid-grey curves are data)
        if is_black(p.color):
            if o is not None:
                length = abs(p.pts[1, 0] - p.pts[0, 0]) + abs(p.pts[1, 1] - p.pts[0, 1])
                bx0_, by0_, bx1_, by1_ = p.bbox
                near_edge = (min(abs(bx0_ - x0), abs(bx1_ - x1), abs(by0_ - y0), abs(by1_ - y1))
                             <= 0.03 * min(w, h))
                if length > 0.5 * min(w, h) or (length < 0.03 * min(w, h) and near_edge):
                    continue             # axes, grid lines, tick marks (short marks sit on the frame)
            elif len(p.pts) > 2 and is_manhattan(p):
                continue                 # axis polylines with tick marks
        groups.setdefault(style_key(p), []).append(p)
    series = []
    for key, ps in groups.items():
        chains = [c for c in chain([p.pts for p in ps], tol)
                  if math.hypot(*(c.max(0) - c.min(0))) >= min_chain_frac * diag
                  and not (len(c) >= 3 and math.hypot(*(c[0] - c[-1])) <= tol * 10
                           and math.hypot(*(c.max(0) - c.min(0))) < 0.1 * diag)]   # dash outlines, symbols
        labels = legend.get(key, [])
        if not labels:   # legend sample drawn with another stroke width: same colour and dash
            labels = [lab for k2, labs in legend.items() if k2[0] == key[0] and k2[2] == key[2] for lab in labs]
        # one legend entry names one curve: several chains of one style (a monochrome drawing) stay unlabelled
        label = labels[0] if len(set(labels)) == 1 and len(chains) == 1 else None
        for ci, c in enumerate(sorted(chains, key=lambda c: -len(c))):
            series.append({"style": key, "color": color_name(key[0] if key[0] != -1 else None), "lw": key[1],
                           "dashed": key[2], "label_raw": label, "legend_labels_same_style": sorted(set(labels)),
                           "chain_index": ci, "n_chains_in_style": len(chains),
                           "pts": dedupe_consecutive(c, tol * 0.1), "kinds": sorted({p.kind for p in ps})})
    # marker series: ≥ 3 same-colour small shapes of similar size; the point is the shape's centre
    for colour, ms in marks.items():
        sizes = np.array([math.hypot(p.bbox[2] - p.bbox[0], p.bbox[3] - p.bbox[1]) for p in ms])
        med = float(np.median(sizes)) if len(sizes) else 0.0
        keep = [p for p, s in zip(ms, sizes) if 0.6 * med <= s <= 1.6 * med]
        if len(keep) >= 3:
            # letters drawn as filled outlines sit shoulder to shoulder; data markers are spaced out
            c = np.array([((p.bbox[0] + p.bbox[2]) / 2, (p.bbox[1] + p.bbox[3]) / 2) for p in keep])
            d = np.sqrt(((c[:, None, :] - c[None, :, :]) ** 2).sum(-1))
            np.fill_diagonal(d, np.inf)
            keep = [p for p, nn in zip(keep, d.min(1)) if nn >= 1.2 * med]
        if len(keep) < 3:
            continue
        cents = np.array(sorted(((p.bbox[0] + p.bbox[2]) / 2, (p.bbox[1] + p.bbox[3]) / 2) for p in keep))
        cents = dedupe_consecutive(cents, 0.2 * med)
        labels = [lab for k2, labs in legend.items() if k2[0] == colour for lab in labs]
        series.append({"style": (colour, 0.0, False), "color": color_name(colour if colour != -1 else None),
                       "lw": 0.0, "dashed": False, "label_raw": labels[0] if len(set(labels)) == 1 else None,
                       "legend_labels_same_style": sorted(set(labels)), "chain_index": 0, "n_chains_in_style": 1,
                       "pts": cents, "kinds": ["MARKERS"], "sampling": "MARKER_CENTRES",
                       "marker_size": med})
    used_labels = {s["label_raw"] for s in series if s["label_raw"]}
    legend_without_curve = sorted({lab for labs in legend.values() for lab in labs} - used_labels)
    return series, legend, legend_without_curve


def point_errors(axis: Axis | None, pts_axis: np.ndarray, quantum: float) -> np.ndarray | None:
    """Half-width error per point in value units: label-fit rms of the axis ⊕ coordinate quantisation."""
    if axis is None:
        return None
    e = math.hypot(axis.residual_rms, quantum)
    return np.array([e * axis.value_per_pos(p) for p in pts_axis])

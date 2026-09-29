"""Curve extraction from vector paths: legend samples, style groups (colour, width, dash), chaining of pieces,
data-bearing vertices (polyline vertices and Bezier joints are the plotted points of chart software)."""
from __future__ import annotations

import math

import numpy as np

from vkm_corpus.figures.calibrate import Axis, is_axis_aligned_segment, is_manhattan, is_rect
from vkm_corpus.figures.primitives import Path, Text, color_name, is_black, is_greyish


def _legend_pairs(paths: list[Path], texts: list[Text], max_len: float):
    out = []
    for p in paths:
        if p.kind == "FILL" or is_axis_aligned_segment(p) != "H":
            continue
        length = float(p.pts[:, 0].max() - p.pts[:, 0].min())
        if length > max_len:
            continue
        sx1, sy = float(p.pts[:, 0].max()), float(p.pts[:, 1].mean())
        best = None
        for t in texts:
            d = t.x0 - sx1
            if abs(t.yc - sy) <= 0.7 * t.h and -0.3 * t.h <= d <= 3.0 * t.h and (best is None or d < best[0]):
                best = (d, t)
        if best:
            out.append((p, best[1]))
    return out


def legend_text_ids(paths: list[Path], texts: list[Text], region) -> set:
    """ids of texts that label a legend sample (a short horizontal stroke with the label right of it)."""
    return {id(t) for _, t in _legend_pairs(paths, texts, 0.2 * (region[2] - region[0]))}


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
    for p, t in _legend_pairs(paths, texts, 0.2 * w):
        legend.setdefault(style_key(p), []).append(t.text.strip())
        legend_ids.add(id(p))
    groups: dict = {}
    for p in paths:
        if p.kind == "FILL" or is_rect(p) or is_greyish(p.color) or id(p) in legend_ids:
            continue
        o = is_axis_aligned_segment(p)
        pin = ((p.pts[:, 0] >= x0 - margin) & (p.pts[:, 0] <= x1 + margin) & (p.pts[:, 1] >= y0 - margin)
               & (p.pts[:, 1] <= y1 + margin))
        if not (bool(pin.all()) or (bool(pin.any()) and o is None)):
            continue
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
        chains = [c for c in chain([p.pts for p in ps], tol) if math.hypot(*(c.max(0) - c.min(0))) >= min_chain_frac * diag]
        labels = legend.get(key, [])
        if not labels:   # legend sample drawn with another stroke width: same colour and dash
            labels = [lab for k2, labs in legend.items() if k2[0] == key[0] and k2[2] == key[2] for lab in labs]
        label = labels[0] if len(set(labels)) == 1 else None
        for ci, c in enumerate(sorted(chains, key=lambda c: -len(c))):
            series.append({"style": key, "color": color_name(key[0] if key[0] != -1 else None), "lw": key[1],
                           "dashed": key[2], "label_raw": label, "legend_labels_same_style": sorted(set(labels)),
                           "chain_index": ci, "n_chains_in_style": len(chains),
                           "pts": dedupe_consecutive(c, tol * 0.1), "kinds": sorted({p.kind for p in ps})})
    used_labels = {s["label_raw"] for s in series if s["label_raw"]}
    legend_without_curve = sorted({lab for labs in legend.values() for lab in labs} - used_labels)
    return series, legend, legend_without_curve


def point_errors(axis: Axis | None, pts_axis: np.ndarray, quantum: float) -> np.ndarray | None:
    """Half-width error per point in value units: label-fit rms of the axis ⊕ coordinate quantisation."""
    if axis is None:
        return None
    e = math.hypot(axis.residual_rms, quantum)
    return np.array([e * axis.value_per_pos(p) for p in pts_axis])

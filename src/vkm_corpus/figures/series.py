"""Curve extraction from vector paths: legend samples, style groups (colour, width, dash), chaining of pieces,
data-bearing vertices (polyline vertices and Bezier joints are the plotted points of chart software)."""
from __future__ import annotations

import math
import re

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


# Markers (fd-0.1.5, MODEL_CHOICE). A marker shape repeated by most of a colour group (≥ MARKER_SAME_SHARE of it,
# vertices equal up to translation within MARKER_SHAPE_TOL of the marker size) are data markers however close they
# stand — dense data no longer lose markers to the «letters shoulder to shoulder» rule, which stays for groups of
# varied shapes (glyph outlines). A marker followed on its line, within MARKER_LEGEND_GAP_H text heights, by words
# with a word of ≥ 3 letters is the legend's sample: it is not a data point and its words label the series (a data
# label next to a point — «рп.354», «-80.82», «2» — has no such word). A group whose shapes share vertices
# (≥ MARKER_CHAIN_SHARE of them) is a curve drawn as filled pieces, not markers.
MARKER_SAME_SHARE, MARKER_SHAPE_TOL, MARKER_LEGEND_GAP_H, MARKER_CHAIN_SHARE = 0.6, 0.15, 3.0, 0.3
_LETTERS = re.compile(r"[A-Za-zА-Яа-яЁё]")


def _outline(p: Path, size: float) -> np.ndarray:
    """The vertices of a shape about its box centre, a closing vertex that repeats the first one dropped (a marker
    drawn as a fill and as a closed outline gives the same vertices twice)."""
    pts = p.pts
    if len(pts) >= 3 and float(np.hypot(*(pts[0] - pts[-1]))) <= 1e-3 * size:
        pts = pts[:-1]
    return pts - [(p.bbox[0] + p.bbox[2]) / 2, (p.bbox[1] + p.bbox[3]) / 2]


def same_shape(shapes: list[Path], size: float) -> list[Path] | None:
    """The shapes of the group's most repeated marker shape (same vertex count, vertices about the box centre equal
    within ``MARKER_SHAPE_TOL`` × size) when they are at least ``MARKER_SAME_SHARE`` of the group (and ≥ 3), else
    None. A group whose shapes share vertices with shapes centred elsewhere (pieces of a thick curve drawn as filled
    outlines) is not a marker group: None."""
    if len(shapes) < 3 or size <= 0:
        return None
    q = 0.002 * size
    cents = np.array([((p.bbox[0] + p.bbox[2]) / 2, (p.bbox[1] + p.bbox[3]) / 2) for p in shapes])
    owner: dict[tuple, int] = {}
    touching: set[int] = set()
    for i, p in enumerate(shapes):
        for v in {(round(float(x) / q), round(float(y) / q)) for x, y in p.pts}:
            j = owner.setdefault(v, i)
            if j != i and float(np.hypot(*(cents[i] - cents[j]))) > 0.25 * size:
                touching.update((i, j))         # a shared vertex of two shapes centred apart: a chain of pieces
    if len(touching) >= MARKER_CHAIN_SHARE * len(shapes):
        return None
    outlines = [_outline(p, size) for p in shapes]
    counts: dict[int, list[int]] = {}
    for i, o in enumerate(outlines):
        counts.setdefault(len(o), []).append(i)
    idx = max(counts.values(), key=len)
    if len(idx) < max(3, MARKER_SAME_SHARE * len(shapes)):
        return None
    norm = np.stack([outlines[i] for i in idx])
    ref = np.median(norm, axis=0)                      # the typical shape of the group, vertex by vertex
    match = np.abs(norm - ref).max(axis=(1, 2)) <= MARKER_SHAPE_TOL * size
    members = [idx[j] for j in np.where(match)[0]]
    if len(members) < max(3, MARKER_SAME_SHARE * len(shapes)):
        return None
    return [shapes[i] for i in sorted(members)]


def marker_legend(markers: list[Path], texts: list[Text], size: float, min_letters: int = 3) -> dict[int, str]:
    """{id(marker): legend words} of the markers that are legend samples: words on the marker's line starting within
    ``MARKER_LEGEND_GAP_H`` text heights right of it (gaps between words under 1.2 heights), one word with at least
    ``min_letters`` letters (3 inside the plot, where data labels stand; 1 for a legend outside it)."""
    out: dict[int, str] = {}
    for p in markers:
        x0, y0, x1, y1 = p.bbox
        cy = (y0 + y1) / 2
        line = sorted((t for t in texts if abs(t.yc - cy) <= 0.7 * t.h and t.x0 - x1 >= -0.3 * t.h),
                      key=lambda t: t.x0)
        if not line or line[0].x0 - x1 > max(MARKER_LEGEND_GAP_H * line[0].h, 2.5 * size):
            continue
        words = [line[0]]
        for t in line[1:]:
            if t.x0 - words[-1].x1 > 1.2 * t.h:
                break
            words.append(t)
        if any(len(_LETTERS.findall(w.text)) >= min_letters for w in words):
            out[id(p)] = " ".join(w.text.strip() for w in words)
    return out


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
    outside: dict = {}               # small shapes outside the plot: candidate legend samples of marker series
    for p in paths:
        if id(p) in legend_ids or is_rect(p):
            continue
        o = is_axis_aligned_segment(p)
        pin = ((p.pts[:, 0] >= x0 - margin) & (p.pts[:, 0] <= x1 + margin) & (p.pts[:, 1] >= y0 - margin)
               & (p.pts[:, 1] <= y1 + margin))
        bx0_, by0_, bx1_, by1_ = p.bbox
        size = math.hypot(bx1_ - bx0_, by1_ - by0_)
        closed = len(p.pts) >= 3 and math.hypot(*(p.pts[0] - p.pts[-1])) <= tol * 10
        if not (bool(pin.all()) or (bool(pin.any()) and o is None)):
            if (p.kind in ("FILL", "MARK") or closed) and size <= 0.04 * diag:
                outside.setdefault(p.color if p.color is not None else -1, []).append(p)   # a legend outside?
            continue
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
        refits = [p.pts[[0, -1]] for p in ps if p.extra.get("spline_refit")]
        for ci, c in enumerate(sorted(chains, key=lambda c: -len(c))):
            # pieces whose interior vertices the source did not keep (route B refit splines) that end on this chain
            n_refit = sum(1 for ends in refits
                          if min(np.hypot(*(c - ends[0]).T).min(), np.hypot(*(c - ends[1]).T).min()) <= tol)
            series.append({"style": key, "color": color_name(key[0] if key[0] != -1 else None), "lw": key[1],
                           "dashed": key[2], "label_raw": label, "legend_labels_same_style": sorted(set(labels)),
                           "chain_index": ci, "n_chains_in_style": len(chains),
                           "pts": dedupe_consecutive(c, tol * 0.1), "kinds": sorted({p.kind for p in ps}),
                           "spline_refit_pieces": n_refit})
    # marker series: ≥ 3 same-colour small shapes of similar size; the point is the shape's centre
    for colour, ms in marks.items():
        sizes = np.array([math.hypot(p.bbox[2] - p.bbox[0], p.bbox[3] - p.bbox[1]) for p in ms])
        med = float(np.median(sizes)) if len(sizes) else 0.0
        keep = [p for p, s in zip(ms, sizes) if 0.6 * med <= s <= 1.6 * med]
        if len(keep) >= 3:
            same = same_shape(keep, med)
            # letters drawn as filled outlines sit shoulder to shoulder; data markers are spaced out
            c = np.array([((p.bbox[0] + p.bbox[2]) / 2, (p.bbox[1] + p.bbox[3]) / 2) for p in keep])
            d = np.sqrt(((c[:, None, :] - c[None, :, :]) ** 2).sum(-1))
            np.fill_diagonal(d, np.inf)
            spaced = [p for p, nn in zip(keep, d.min(1)) if nn >= 1.2 * med]
            if same is not None:
                # one marker shape repeated: data markers, however close they stand; markers of other shapes in the
                # group stay as before (spaced out)
                ids = {id(p) for p in same}
                keep = same + [p for p in spaced if id(p) not in ids]
            else:
                keep = spaced
        # the legend's sample of the series: a marker with the legend words right of it — not a data point
        samples = marker_legend(keep, texts, med)
        keep = [p for p in keep if id(p) not in samples]
        if len(keep) < 3:
            continue
        cents = np.array(sorted(((p.bbox[0] + p.bbox[2]) / 2, (p.bbox[1] + p.bbox[3]) / 2) for p in keep))
        cents = dedupe_consecutive(cents, 0.2 * med)
        # a black stroke beside a number is a tick or a leader far more often than a legend sample: black markers
        # take only worded stroke-legend labels (fd-0.1.5: benchmark number «84» named a series)
        labels = list(samples.values()) + [lab for k2, labs in legend.items() if k2[0] == colour for lab in labs
                                           if colour != -1 or _LETTERS.search(lab)]
        if not labels:               # the legend outside the plot: a sample of the same colour and size
            like = [q for q in outside.get(colour, [])
                    if 0.6 * med <= math.hypot(q.bbox[2] - q.bbox[0], q.bbox[3] - q.bbox[1]) <= 1.6 * med]
            labels = list(marker_legend(like, texts, med, min_letters=1).values())
        series.append({"style": (colour, 0.0, False), "color": color_name(colour if colour != -1 else None),
                       "lw": 0.0, "dashed": False, "label_raw": labels[0] if len(set(labels)) == 1 else None,
                       "legend_labels_same_style": sorted(set(labels)), "chain_index": 0, "n_chains_in_style": 1,
                       "pts": cents, "kinds": ["MARKERS"], "sampling": "MARKER_CENTRES",
                       "marker_size": med, "legend_markers_excluded": len(samples)})
    used_labels = {s["label_raw"] for s in series if s["label_raw"]}
    legend_without_curve = sorted({lab for labs in legend.values() for lab in labs} - used_labels)
    return series, legend, legend_without_curve


def point_errors(axis: Axis | None, pts_axis: np.ndarray, quantum: float) -> np.ndarray | None:
    """Half-width error per point in value units: label-fit rms of the axis ⊕ coordinate quantisation."""
    if axis is None:
        return None
    e = math.hypot(axis.residual_rms, quantum)
    return np.array([e * axis.value_per_pos(p) for p in pts_axis])

"""Route A: native PDF vectors (PyMuPDF ``get_drawings``, the same source as the pipeline's ``VECTOR_PATHS_JSON``)
and the PDF text layer → primitives in PAGE_PT_TL (points, y down).

Clipping: a chart clips its plot area; a path the viewer never shows (outside every active clip) is dropped, as
AutoCAD ``-PDFIMPORT`` does — PyMuPDF itself lists clipped-out paths.
"""
from __future__ import annotations

import math

import numpy as np

from vkm_corpus.figures.primitives import Path, Text, rgb_int


def _pt(p) -> tuple[float, float]:
    return float(p.x), float(p.y)


def _subpaths(items) -> list[list[tuple[float, float]]]:
    """Consecutive items joined into polylines of data-bearing points (item end points; Bezier joints)."""
    out: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] = []
    for it in items:
        op = it[0]
        if op in ("l", "c"):
            a, b = _pt(it[1]), _pt(it[-1])
            if cur and math.hypot(cur[-1][0] - a[0], cur[-1][1] - a[1]) <= 1e-3:
                cur.append(b)
            else:
                if len(cur) >= 2:
                    out.append(cur)
                cur = [a, b]
        elif op == "re":
            r = it[1]
            if len(cur) >= 2:
                out.append(cur)
            cur = []
            out.append([(r.x0, r.y0), (r.x1, r.y0), (r.x1, r.y1), (r.x0, r.y1), (r.x0, r.y0)])
        elif op == "qu":
            q = it[1]
            if len(cur) >= 2:
                out.append(cur)
            cur = []
            out.append([_pt(q.ul), _pt(q.ur), _pt(q.lr), _pt(q.ll), _pt(q.ul)])
    if len(cur) >= 2:
        out.append(cur)
    return out


def load_page(page, clip_rect=None) -> tuple[list[Text], list[Path]]:
    """Texts (words; rotation from the line direction) and paths of one page, optionally within ``clip_rect``
    (PAGE_PT_TL of the displayed page)."""
    import pymupdf

    clip = pymupdf.Rect(clip_rect) if clip_rect is not None else None
    texts: list[Text] = []
    d = page.get_text("dict", clip=clip)
    line_dir = {}
    for b in d.get("blocks", []):
        for li, ln in enumerate(b.get("lines", [])):
            line_dir[(b["number"], li)] = ln.get("dir", (1, 0))
    for x0, y0, x1, y1, word, bno, lno, _ in page.get_text("words", clip=clip):
        dx, dy = line_dir.get((bno, lno), (1, 0))
        rot = -math.degrees(math.atan2(dy, dx))       # y down → counter-clockwise positive as on the page
        h = (y1 - y0) if abs(rot) < 1 else (x1 - x0)
        texts.append(Text(word, float(x0), float(x1), float((y0 + y1) / 2), float(h) * 0.7, rot,
                          f"PDF_WORD:{bno}.{lno}", "NATIVE"))
    paths: list[Path] = []
    clips: list[tuple[int, object]] = []      # (level, scissor rect) of active clips
    for dr in page.get_drawings(extended=True):
        kind = dr.get("type")
        level = dr.get("level", 0)
        clips = [(lv, sc) for lv, sc in clips if lv < level]
        if kind == "clip":
            clips.append((level, dr.get("scissor") or dr.get("rect")))
            continue
        if kind == "group":
            continue
        rect = dr.get("rect")
        if clip is not None and rect is not None and not rect.intersects(clip) and not rect.is_empty:
            continue
        visible = True
        for _, sc in clips:
            if sc is not None and rect is not None:
                grown = pymupdf.Rect(sc) + (-0.5, -0.5, 0.5, 0.5)
                if not (grown.intersects(rect) or grown.contains(rect.tl)):
                    visible = False
                    break
        if not visible:
            continue
        stroke = dr.get("color")
        fill = dr.get("fill")
        width = float(dr.get("width") or 0.0)
        dashed = bool(dr.get("dashes")) and str(dr.get("dashes")).strip() not in ("[] 0", "[] 0.0", "")
        is_fill = kind == "f" or (kind == "fs" and stroke is None)
        col = fill if is_fill else stroke
        colour = None if col is None else rgb_int(*col[:3])
        if colour is not None and colour == 0:
            colour = None
        for sp in _subpaths(dr.get("items", [])):
            pts = np.array(sp, float)
            if is_fill:
                line = _thin_rect_as_line(pts)
                if line is not None:       # a line drawn as a thin filled rectangle (axes, ticks, legend keys)
                    a, b, thick = line
                    paths.append(Path(np.array([a, b]), colour, thick, "LINE", origin=f"PDF_RECT:{dr.get('seqno')}"))
                    continue
            paths.append(Path(pts, colour, width, "FILL" if is_fill else ("POLY" if len(pts) > 2 else "LINE"),
                              closed=bool(dr.get("closePath")), dashed=dashed, origin=f"PDF_PATH:{dr.get('seqno')}"))
    return texts, paths


def _thin_rect_as_line(pts: np.ndarray, max_thick: float = 1.6, min_len: float = 2.0):
    """An axis-aligned rectangle thinner than ``max_thick`` points → its centre line and thickness."""
    if len(pts) not in (4, 5):
        return None
    x0, y0 = pts.min(0)
    x1, y1 = pts.max(0)
    ux = np.unique(np.round(pts[:, 0], 2))
    uy = np.unique(np.round(pts[:, 1], 2))
    if len(ux) > 2 or len(uy) > 2:
        return None
    w, h = x1 - x0, y1 - y0
    if h <= max_thick and w >= min_len and w > 2 * h:
        ym = (y0 + y1) / 2
        return (float(x0), float(ym)), (float(x1), float(ym)), float(h)
    if w <= max_thick and h >= min_len and h > 2 * w:
        xm = (x0 + x1) / 2
        return (float(xm), float(y0)), (float(xm), float(y1)), float(w)
    return None

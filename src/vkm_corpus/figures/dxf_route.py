"""Route B: a DXF written by AutoCAD ``-PDFIMPORT`` (through ``vkm-cad``) → primitives (inches, y up)."""
from __future__ import annotations

import math

import numpy as np

from vkm_corpus.figures.primitives import Path, Text, text_width

INCH_PER_LINEWEIGHT = 1 / 100 / 25.4       # DXF lineweight is 1/100 mm
DEFAULT_LINEWEIGHT = 25


def _aci_rgb(ci) -> int | None:
    if ci is None or ci in (0, 7, 256) or ci < 0:
        return None
    from ezdxf.colors import aci2rgb

    r, g, b = aci2rgb(ci)
    return (r << 16) | (g << 8) | b


def _color(e, layer_colors: dict) -> int | None:
    """Entity colour; BYLAYER resolves through the layer — PDFIMPORT gives its layers the colour of the first object
    it puts on them and stores objects of that colour as BYLAYER."""
    tc = e.dxf.get("true_color")
    if tc is not None:
        return int(tc)
    ci = e.dxf.get("color", 256)
    if ci in (None, 256):
        return layer_colors.get(e.dxf.get("layer", "0"))
    return _aci_rgb(ci)


def _layer_colors(doc) -> dict:
    out = {}
    for layer in doc.layers:
        tc = layer.dxf.get("true_color")
        out[layer.dxf.name] = int(tc) if tc is not None else _aci_rgb(abs(layer.dxf.get("color", 7)))
    return out


def _lw(e, layer_lw: dict) -> float:
    lw = e.dxf.get("lineweight", -1)
    if lw is None or lw == -1:                 # BYLAYER
        lw = layer_lw.get(e.dxf.get("layer", "0"), DEFAULT_LINEWEIGHT)
    return (DEFAULT_LINEWEIGHT if lw is None or lw < 0 else lw) * INCH_PER_LINEWEIGHT


def _dense(e, tol=0.002):
    from ezdxf import path as ezpath

    try:
        p = ezpath.make_path(e)
        return np.array([(q.x, q.y) for q in p.flattening(tol)])
    except Exception:  # noqa: BLE001
        return None


def _spline_vertices(e, cp: np.ndarray) -> tuple[np.ndarray, bool, bool]:
    """Data-bearing points of a SPLINE, whether the spline is a refit, whether it is closed.

    PDFIMPORT writes one PDF curve segment as a 4-control-point spline; its end points are vertices of the PDF path
    (sweep of 320 figures: 99.96 % within 0.15 pt of a PDF vertex). A cubic in Bezier form (interior knots of
    multiplicity 3) passes through every third control point, the segment joints (not seen in the sweep). Any other
    spline refits a chain of PDF segments: a cubic with double knots whose knot points fall on a PDF vertex in only
    49 % of the cases (3,139 knots in 49 figures; the others subdivide one segment). Only its end points are kept
    (95.9 % on a PDF vertex): the interior vertices cannot be told apart in the DXF, and the path is marked as a
    refit so that its series says so. Samples of the drawn curve are never returned as vertices of an open curve.
    A closed spline is a symbol outline (a marker drawn as a stroke): its flattened outline is returned, the marker
    centre is taken from it later."""
    deg = int(e.dxf.get("degree", 3) or 3)
    n = len(cp)
    if deg == 3 and n == 4:
        return np.array([cp[0], cp[-1]]), False, False
    dense = _dense(e)
    ends = (dense[0], dense[-1]) if dense is not None and len(dense) >= 2 else (cp[0], cp[-1])
    size = float(np.hypot(*(cp.max(0) - cp.min(0))))
    if dense is not None and len(dense) >= 4 and size > 0 and float(np.hypot(*(ends[1] - ends[0]))) <= 1e-3 * size:
        return dense, False, True
    if deg == 3 and n > 4 and (n - 1) % 3 == 0:
        knots = list(e.knots)
        interior = knots[4:-4] if len(knots) == n + 4 else []
        if interior and all(interior[i] == interior[i + 1] == interior[i + 2] for i in range(0, len(interior), 3)):
            return cp[::3].copy(), False, False
    return np.array([ends[0], ends[1]]), True, False


def load_dxf(dxf_path) -> tuple[list[Text], list[Path]]:
    """MTEXT/TEXT → :class:`Text` (centre from the attachment point and an estimated width); LWPOLYLINE, LINE,
    SPLINE (see :func:`_spline_vertices`), ARC (end points; PDFIMPORT writes nearly straight Bezier pieces as arcs of
    huge radius), CIRCLE (markers), SOLID/HATCH (fills) → :class:`Path`."""
    import ezdxf
    from ezdxf import path as ezpath

    doc = ezdxf.readfile(str(dxf_path))
    layer_colors = _layer_colors(doc)
    layer_lw = {layer.dxf.name: layer.dxf.get("lineweight", DEFAULT_LINEWEIGHT) for layer in doc.layers}
    texts: list[Text] = []
    paths: list[Path] = []
    for e in doc.modelspace():
        t = e.dxftype()
        handle = e.dxf.handle
        if t in ("MTEXT", "TEXT"):
            s = (e.plain_text() if t == "MTEXT" else e.dxf.text).replace("\n", " ").strip()
            if not s:
                continue
            ins = e.dxf.insert
            if t == "MTEXT":
                ch = float(e.dxf.get("char_height", 0.1) or 0.1)
                att = int(e.dxf.get("attachment_point", 1) or 1)
                rot = float(e.dxf.get("rotation", 0) or 0)
                if e.dxf.hasattr("text_direction"):
                    d = e.dxf.text_direction
                    rot = math.degrees(math.atan2(d.y, d.x))
            else:
                ch = float(e.dxf.get("height", 0.1) or 0.1)
                att = 7
                rot = float(e.dxf.get("rotation", 0) or 0)
            w = text_width(s, ch)
            col, row = (att - 1) % 3, (att - 1) // 3
            x0 = ins.x - (0.0 if col == 0 else w / 2 if col == 1 else w)
            yc = ins.y - (ch / 2 if row == 0 else 0.0 if row == 1 else -ch / 2)
            texts.append(Text(s, x0, x0 + w, yc, ch, rot, f"{t}#{handle}", "NATIVE"))
            continue
        col = _color(e, layer_colors)
        lw = _lw(e, layer_lw)
        try:
            if t == "LWPOLYLINE":
                pts = np.array([(x, y) for x, y in e.get_points("xy")], dtype=float)
                if len(pts) < 2:
                    continue
                closed = bool(e.closed)
                if closed and not np.allclose(pts[0], pts[-1]):
                    pts = np.vstack([pts, pts[:1]])
                paths.append(Path(pts, col, lw, "POLY", closed, origin=f"LWPOLYLINE#{handle}"))
            elif t == "LINE":
                s0, s1 = e.dxf.start, e.dxf.end
                paths.append(Path(np.array([[s0.x, s0.y], [s1.x, s1.y]]), col, lw, "LINE", origin=f"LINE#{handle}"))
            elif t == "SPLINE":
                cp = np.array([p[:2] for p in e.control_points], dtype=float)
                if len(cp) >= 2:
                    pts, refit, closed = _spline_vertices(e, cp)
                    paths.append(Path(pts, col, lw, "BEZ", closed, dense=_dense(e), origin=f"SPLINE#{handle}",
                                      extra={"spline_refit": True} if refit else {}))
            elif t == "ARC":
                s0, s1 = e.start_point, e.end_point
                dense = None if e.dxf.radius > 50 else _dense(e)
                paths.append(Path(np.array([[s0.x, s0.y], [s1.x, s1.y]]), col, lw, "ARC", dense=dense,
                                  origin=f"ARC#{handle}"))
            elif t == "CIRCLE":           # data markers (dots) of a scatter or a line with markers
                c0, r = e.dxf.center, float(e.dxf.radius)
                ang = np.linspace(0, 2 * np.pi, 17)
                ring = np.stack([c0.x + r * np.cos(ang), c0.y + r * np.sin(ang)], 1)
                paths.append(Path(ring, col, lw, "MARK", True, origin=f"CIRCLE#{handle}"))
            elif t == "SOLID":
                v = [e.dxf.vtx0, e.dxf.vtx1, e.dxf.vtx3, e.dxf.vtx2]
                paths.append(Path(np.array([(q.x, q.y) for q in v]), col, lw, "FILL", True, origin=f"SOLID#{handle}"))
            elif t == "HATCH":
                for bp in ezpath.from_hatch(e):
                    v = np.array([(q.x, q.y) for q in bp.flattening(0.002)])
                    if len(v) >= 3:
                        paths.append(Path(v, col, lw, "FILL", True, origin=f"HATCH#{handle}"))
        except Exception:  # noqa: BLE001 — a malformed entity is skipped, never guessed
            continue
    return texts, paths


def pdf_words(cropped_pdf, page_index: int = 0) -> list[Text]:
    """Words of the PDF text layer of the single-figure page in the drawing frame of its import (inches, origin =
    lower-left of the CropBox, y up). PyMuPDF reports them relative to the CropBox top-left."""
    import pymupdf

    page = pymupdf.open(str(cropped_pdf))[page_index]
    h = float(page.rect.height)
    out = []
    for x0, y0, x1, y1, word, *_ in page.get_text("words"):
        out.append(Text(word, x0 / 72.0, x1 / 72.0, (h - (y0 + y1) / 2) / 72.0, (y1 - y0) / 72.0 * 0.7, 0.0,
                        "PDF_WORD", "NATIVE"))
    return out


def _norm(s: str) -> str:
    return "".join(s.split()).replace("−", "-").replace("–", "-")


def refine_texts(texts: list[Text], words: list[Text], drop_unconfirmed: bool = True) -> tuple[list[Text], dict]:
    """PDFIMPORT merges a row of tick labels into one MTEXT ('187 188 189 …'), gives no text widths and imports text
    that the page never shows (placed outside the MediaBox). Each MTEXT is matched to the words of the visible PDF
    text layer of the same single-figure PDF on its line (tokens in order): a match replaces its estimated extent by
    the exact word boxes and splits a merged row into one text per word. MTEXT without any visible counterpart
    nearby is dropped (``drop_unconfirmed``); a rotated or otherwise unmatched but confirmed MTEXT is kept as is."""
    stats = {"mtext": len(texts), "refined": 0, "split": 0, "kept_unrefined": 0, "dropped_unconfirmed": 0}
    used: set[int] = set()
    out: list[Text] = []
    word_text = [(_norm(w.text), w) for w in words]

    def confirmed(t: Text) -> bool:
        s = _norm(t.text)
        if not s:
            return False
        r = 2.5 * max(t.h, 0.05) + (t.x1 - t.x0)
        return any((ws and (ws in s or s in ws)) and abs(w.xc - t.xc) <= r and abs(w.yc - t.yc) <= r
                   for ws, w in word_text)

    for t in texts:
        tokens = t.text.split()
        if not tokens or abs(t.rot) >= 1:
            if drop_unconfirmed and words and not confirmed(t):
                stats["dropped_unconfirmed"] += 1
            else:
                out.append(t)
                stats["kept_unrefined"] += 1
            continue
        line = sorted((w for w in words if id(w) not in used and abs(w.yc - t.yc) <= 0.6 * t.h),
                      key=lambda w: w.x0)
        start = [w for w in line if w.text == tokens[0] and abs(w.x0 - t.x0) <= 1.5 * t.h]
        matched = None
        if start:
            seq = [start[0]]
            rest = [w for w in line if w.x0 > start[0].x0]
            k = 1
            for w in rest:
                if k >= len(tokens):
                    break
                if w.text == tokens[k]:
                    seq.append(w)
                    k += 1
                elif w.x0 - seq[-1].x1 > 40 * t.h:
                    break
            if k == len(tokens):
                matched = seq
        if matched is None:
            if drop_unconfirmed and words and not confirmed(t):
                stats["dropped_unconfirmed"] += 1
            else:
                out.append(t)
                stats["kept_unrefined"] += 1
            continue
        for w in matched:
            used.add(id(w))
            out.append(Text(w.text, w.x0, w.x1, t.yc, t.h, t.rot, t.origin + "|PDF_WORD", t.source))
        stats["refined"] += 1
        stats["split"] += int(len(matched) > 1)
    return out, stats


def clip(texts: list[Text], paths: list[Path], box, margin: float = 0.0, mostly_inside: float = 0.5):
    """Texts with their centre in the box; paths with at least ``mostly_inside`` of their vertices in it."""
    x0, y0, x1, y1 = box[0] - margin, box[1] - margin, box[2] + margin, box[3] + margin
    tt = [t for t in texts if x0 <= t.xc <= x1 and y0 <= t.yc <= y1]
    pp = []
    for p in paths:
        inside = (p.pts[:, 0] >= x0) & (p.pts[:, 0] <= x1) & (p.pts[:, 1] >= y0) & (p.pts[:, 1] <= y1)
        if inside.mean() >= mostly_inside:
            pp.append(p)
    return tt, pp

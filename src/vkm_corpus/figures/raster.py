"""Route R: raster plots (embedded images, scans).

Pixel frame: x right, y down, units = pixels of the analysed image (rendered from the page at ``dpi``); the native
resolution of the embedded raster bounds the accuracy and enters the error estimate.

Steps: background colour → dark-neutral mask (axes, grid, text) → long horizontal/vertical lines (morphological
opening) → tick labels (corpus text of the region first, else the local OCR helper in sparse-text mode) → axis
calibration (linear / log / date; a monotone piecewise axis when labels sit at irregular positions; a marker axis
when the benchmarks of a profile line are dots on the axis line, numbered by counting) → curve pixels = coloured
pixels inside the plot area minus text boxes → column-by-column multi-curve tracking with the ink colour as a
secondary cost (k-means colour clustering was tried and dropped: anti-aliased halos and JPEG split one curve into
several clusters) → fragments joined → series; labels from inline labels (geometry) or legends (colour). Gaps are
never filled. QC: all traced curves are re-drawn and compared with the curve-pixel mask.
"""
from __future__ import annotations

import math
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path as FsPath

import numpy as np

from vkm_corpus.figures.calibrate import fit_axis
from vkm_corpus.figures.labels_ocr import OcrEngine
from vkm_corpus.figures.primitives import Text, label_value


# ------------------------------------------------------------------------------------------------ image
@dataclass
class RasterFigure:
    rgb: np.ndarray                       # H x W x 3 uint8
    px_per_pt: float                      # analysed pixels per page point
    origin_pt: tuple[float, float]        # page point (PAGE_PT_TL) of pixel (0, 0)
    native_px_per_pt: float | None = None  # resolution of the embedded raster, if known
    source: dict = field(default_factory=dict)

    def to_page(self, x, y):
        return self.origin_pt[0] + np.asarray(x) / self.px_per_pt, self.origin_pt[1] + np.asarray(y) / self.px_per_pt


def load_region(pdf_path, page_no: int, bbox_pt, dpi: int = 300, margin: float = 6.0) -> RasterFigure:
    import pymupdf

    doc = pymupdf.open(str(pdf_path))
    page = doc[page_no - 1]
    r = (pymupdf.Rect(bbox_pt) + (-margin, -margin, margin, margin)) & page.rect
    pix = page.get_pixmap(dpi=dpi, clip=r, alpha=False, colorspace=pymupdf.csRGB)
    rgb = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3).copy()
    native = None
    for im in page.get_image_info():
        b = pymupdf.Rect(im["bbox"])
        if b.intersects(r) and im.get("width") and b.width > 0:
            native = max(native or 0.0, float(im["width"]) / b.width)
    return RasterFigure(rgb, dpi / 72.0, (float(r.x0), float(r.y0)), native,
                        {"render_dpi": dpi, "clip_pt": [float(v) for v in r]})


def de_lab(c1, c2) -> float:
    """ΔE*ab between two OpenCV 8-bit Lab colours (L stored ×2.55, a/b offset 128)."""
    d = np.asarray(c1, float) - np.asarray(c2, float)
    return float(math.sqrt((d[0] * 100 / 255) ** 2 + d[1] ** 2 + d[2] ** 2))


def _lab(rgb: np.ndarray) -> np.ndarray:
    import cv2

    return cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype(np.float32)


def background_lab(lab: np.ndarray) -> np.ndarray:
    """Most frequent colour (quantised) — white, beige or grey paper."""
    q = (lab // 4).reshape(-1, 3).astype(np.int32)
    keys, counts = np.unique(q[:, 0] * 10000 + q[:, 1] * 100 + q[:, 2], return_counts=True)
    k = keys[np.argmax(counts)]
    return np.array([k // 10000, (k // 100) % 100, k % 100], dtype=np.float32) * 4 + 2


def colour_distance(rgb: np.ndarray) -> np.ndarray:
    """ΔE-like distance of each pixel from the paper colour (L in 0…100, a/b in their units)."""
    lab = _lab(rgb)
    bg = background_lab(lab)
    return np.sqrt(((lab[..., 0] - bg[0]) * 100 / 255) ** 2 + (lab[..., 1] - bg[1]) ** 2 + (lab[..., 2] - bg[2]) ** 2)


def masks(rgb: np.ndarray):
    """(dark-neutral mask: axes, grid, text; coloured mask: curves, markers; background Lab; chroma image)."""
    lab = _lab(rgb)
    bg = background_lab(lab)
    L = lab[..., 0] * 100 / 255
    a = lab[..., 1] - 128
    b = lab[..., 2] - 128
    chroma = np.hypot(a, b)
    bga, bgb = bg[1] - 128, bg[2] - 128
    dist = np.sqrt((lab[..., 0] - bg[0]) ** 2 * (100 / 255) ** 2 + (a - bga) ** 2 + (b - bgb) ** 2)
    # neutral ink (axes, grid, text) vs coloured ink; printed muted colours (teal, brown) keep a chroma of 10-20
    # after JPEG and anti-aliasing, so only chroma < 10 counts as neutral
    dark = (L < 55) & (chroma < 10)
    grey = (dist > 12) & (chroma < 8) & ~dark
    coloured = (chroma >= 10) & (dist > 25) & ~dark
    return dark, grey, coloured, bg, chroma, lab


# ------------------------------------------------------------------------------------------------ lines
def long_lines(mask: np.ndarray, min_frac: float = 0.3):
    """Horizontal (x0, x1, y) and vertical (y0, y1, x) lines of ``mask`` at least ``min_frac`` of the image long."""
    import cv2

    h, w = mask.shape
    m = mask.astype(np.uint8) * 255
    out_h, out_v = [], []
    kh = cv2.getStructuringElement(cv2.MORPH_RECT, (max(3, int(w * min_frac)), 1))
    kv = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(3, int(h * min_frac))))
    for kernel, horizontal in ((kh, True), (kv, False)):
        o = cv2.morphologyEx(m, cv2.MORPH_OPEN, kernel)
        n, lab, stats, _ = cv2.connectedComponentsWithStats(o, 8)
        for i in range(1, n):
            x, y, ww, hh, area = stats[i]
            if horizontal:
                out_h.append((float(x), float(x + ww - 1), float(y + (hh - 1) / 2)))
            else:
                out_v.append((float(y), float(y + hh - 1), float(x + (ww - 1) / 2)))
    return out_h, out_v


def extend_and_merge(mask: np.ndarray, hl, vl, max_gap: int = 40, tol: float = 2.5):
    """Lines broken by coloured marks (a red circle on the axis) are extended along their row/column across gaps of
    at most ``max_gap`` pixels and collinear pieces are merged."""
    h, w = mask.shape

    def ext_h(x0, x1, y):
        yi = int(round(y))
        band = mask[max(0, yi - 2):yi + 3].any(0)
        a, gap = int(x0), 0
        while a - 1 >= 0 and gap <= max_gap:
            a -= 1
            gap = 0 if band[a] else gap + 1
        b, gap = int(x1), 0
        while b + 1 < w and gap <= max_gap:
            b += 1
            gap = 0 if band[b] else gap + 1
        idx = np.where(band[a:b + 1])[0]
        return (float(a + idx.min()), float(a + idx.max()), y) if len(idx) else (x0, x1, y)

    def ext_v(y0, y1, x):
        xi = int(round(x))
        band = mask[:, max(0, xi - 2):xi + 3].any(1)
        a, gap = int(y0), 0
        while a - 1 >= 0 and gap <= max_gap:
            a -= 1
            gap = 0 if band[a] else gap + 1
        b, gap = int(y1), 0
        while b + 1 < h and gap <= max_gap:
            b += 1
            gap = 0 if band[b] else gap + 1
        idx = np.where(band[a:b + 1])[0]
        return (float(a + idx.min()), float(a + idx.max()), x) if len(idx) else (y0, y1, x)

    def merge(lines):
        out = []
        for s in sorted(lines, key=lambda s: (s[2], s[0])):
            if out and abs(out[-1][2] - s[2]) <= tol and s[0] <= out[-1][1] + max_gap:
                p = out[-1]
                out[-1] = (min(p[0], s[0]), max(p[1], s[1]), (p[2] + s[2]) / 2)
            else:
                out.append(s)
        return out
    return merge([ext_h(*s) for s in hl]), merge([ext_v(*s) for s in vl])


def tick_positions(dark: np.ndarray, line, horizontal: bool, band: int = 10, min_len: int = 3) -> list[float]:
    """Short perpendicular marks along an axis line (tick marks), as positions along the line."""
    h, w = dark.shape
    if horizontal:
        x0, x1, y = line
        yi = int(round(y))
        cols = []
        for side in (-1, 1):
            ys = slice(max(0, yi - band), yi - 2) if side < 0 else slice(yi + 3, min(h, yi + band + 1))
            prof = dark[ys, int(x0):int(x1) + 1].sum(0)
            cols.append(prof)
        prof = np.maximum(cols[0], cols[1])
        idx = np.where(prof >= min_len)[0]
        return _runs_centres(idx, int(x0))
    y0, y1, x = line
    xi = int(round(x))
    rows = []
    for side in (-1, 1):
        xs = slice(max(0, xi - band), xi - 2) if side < 0 else slice(xi + 3, min(w, xi + band + 1))
        prof = dark[int(y0):int(y1) + 1, xs].sum(1)
        rows.append(prof)
    prof = np.maximum(rows[0], rows[1])
    idx = np.where(prof >= min_len)[0]
    return _runs_centres(idx, int(y0))


def _runs_centres(idx: np.ndarray, offset: int) -> list[float]:
    if len(idx) == 0:
        return []
    out, start, prev = [], idx[0], idx[0]
    for i in idx[1:]:
        if i != prev + 1:
            out.append(offset + (start + prev) / 2)
            start = i
        prev = i
    out.append(offset + (start + prev) / 2)
    return [float(v) for v in out]


# ------------------------------------------------------------------------------------------------ OCR words
def ocr_words(rgb: np.ndarray, engine: OcrEngine, psm: int = 11, whitelist: str | None = None,
              min_conf: float = 30.0, scale: int = 2, offset=(0, 0), gray: np.ndarray | None = None) -> list[Text]:
    """Tesseract words with boxes (pixel frame of the full image: ``offset`` = position of this crop)."""
    import cv2

    if not engine.available():
        return []
    if gray is None:
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    big = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    with tempfile.TemporaryDirectory() as tmp:
        f = FsPath(tmp) / "fig.png"
        cv2.imwrite(str(f), big)
        cmd = [engine.exe, str(f), "stdout", "--psm", str(psm), "-l", engine.lang, "tsv"]
        if whitelist:
            cmd[5:5] = ["-c", f"tessedit_char_whitelist={whitelist}"]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    engine.calls += 1
    words = []
    for line in (out.stdout or "").splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) < 12 or not parts[11].strip():
            continue
        try:
            conf = float(parts[10])
        except ValueError:
            continue
        if conf < min_conf:
            continue
        left, top, width, height = (int(parts[i]) / scale for i in (6, 7, 8, 9))
        left += offset[0]
        top += offset[1]
        words.append(Text(parts[11].strip(), left, left + width, top + height / 2, height * 0.8, 0.0,
                          f"OCR:{conf:.0f}", "LOCAL_OCR"))
    return words


def ocr_blobs(gray: np.ndarray, offset, engine: OcrEngine, whitelist: str, min_h: float = 5.0) -> list[Text]:
    """Tick labels crowded along an axis are read one by one: ink components are grouped into labels by horizontal
    gaps (< 0.6 × glyph height, same row), each label is cropped, upscaled and read in single-line mode; the label
    box gives the position (Tesseract would otherwise merge '46 48 50 …' into one word)."""
    import cv2

    if gray.size == 0 or not engine.available():
        return []
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    n, cc, stats, _ = cv2.connectedComponentsWithStats(bw, 8)
    comps = [tuple(stats[i][:4]) for i in range(1, n) if stats[i][4] >= 4]
    big = [c for c in comps if c[3] >= min_h]
    if not big:
        return []
    hmed = float(np.median([c[3] for c in big]))
    comps = [c for c in comps if 0.5 * hmed <= c[3] <= 1.6 * hmed or (c[3] < 0.5 * hmed and c[2] < hmed)]
    comps.sort(key=lambda c: (c[1] // max(1, int(hmed)), c[0]))
    groups: list[list] = []
    for c in sorted(comps, key=lambda c: c[0]):
        placed = False
        small = c[3] < 0.5 * hmed           # decimal point, comma, minus sign
        for g in groups:
            gx1 = max(k[0] + k[2] for k in g)
            gy0, gy1 = min(k[1] for k in g), max(k[1] + k[3] for k in g)
            overlap = min(gy1, c[1] + c[3]) - max(gy0, c[1])
            inside = gy0 - 0.2 * hmed <= c[1] + c[3] / 2 <= gy1 + 0.2 * hmed
            last_small = g[-1][3] < 0.5 * hmed
            max_gap = 0.9 * hmed if (small or last_small) else 0.6 * hmed
            # a leading minus sign (a group of small marks so far) belongs to the digits that follow when it sits at
            # their mid-height (fd-0.1.5: «−10 … −110» were read as «10», «30»)
            lead = all(k[3] < 0.5 * hmed for k in g) and c[1] - 0.2 * hmed <= (gy0 + gy1) / 2 <= \
                c[1] + c[3] + 0.2 * hmed
            if (overlap > 0.3 * hmed or (small and inside) or lead) and -0.1 * hmed <= c[0] - gx1 <= max_gap:
                g.append(c)
                placed = True
                break
        if not placed:
            groups.append([c])
    out = []
    for g in groups:
        x0 = min(k[0] for k in g)
        y0 = min(k[1] for k in g)
        x1 = max(k[0] + k[2] for k in g)
        y1 = max(k[1] + k[3] for k in g)
        if y1 - y0 < 0.6 * hmed:
            continue                  # dots, dashes
        pad = int(0.4 * hmed) + 2
        crop = gray[max(0, y0 - pad):y1 + pad, max(0, x0 - pad):x1 + pad]
        big = cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
        big = cv2.copyMakeBorder(big, 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=255)
        txt = re.sub(r"\s+", "", engine.read_line(big) if whitelist == engine.whitelist else
                     OcrEngine(exe=engine.exe, whitelist=whitelist, version=engine.version).read_line(big))
        engine.calls += 0 if whitelist == engine.whitelist else 1
        # a tick mark next to the label is read as a trailing «-» (fd-0.1.5: «-100-»): no number ends with one
        txt = re.sub(r"(?<=\d)[-.,]+$", "", txt)
        if txt:
            out.append(Text(txt, offset[0] + x0, offset[0] + x1, offset[1] + (y0 + y1) / 2, (y1 - y0) * 0.95, 0.0,
                            "OCR_BLOB", "LOCAL_OCR"))
    return out


def _anchors(values: list[float], min_gap: float) -> list[float]:
    """Distinct strip anchors: values more than ``min_gap`` apart, in the order given."""
    out: list[float] = []
    for v in values:
        if all(abs(v - o) > min_gap for o in out):
            out.append(float(v))
    return out


def ocr_axis_strips(rgb: np.ndarray, coloured: np.ndarray, hl, vl, engine: OcrEngine, text_h: float) -> list[Text]:
    """Tick labels read in four strips around the frame spanned by the long lines (axes, grid): left and right of
    it (y labels at the grid-line heights), above and below it (x labels); digits whitelist, coloured marks whitened.
    Sparse OCR of the whole figure misses small labels crowded along an axis."""
    import cv2

    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    gray = np.where(coloured, 255, gray).astype(np.uint8)
    h, w = gray.shape
    th = max(12.0, text_h)
    out: list[Text] = []
    wl = "0123456789.,-"
    gap = max(6.0, 0.4 * th)      # keep the axis line and the markers sitting on it out of the strip
    xs0 = [s[0] for s in hl] + [s[2] for s in vl]
    xs1 = [s[1] for s in hl] + [s[2] for s in vl]
    ys0 = [s[2] for s in hl] + [s[0] for s in vl]
    ys1 = [s[2] for s in hl] + [s[1] for s in vl]
    if not xs0:
        return []
    left, right, top, bottom = min(xs0), max(xs1), min(ys0), max(ys1)
    # fd-0.1.5: the frame's extreme is not always the axis — an x axis line or the legend's line that runs past the y
    # axis put the strip of the y labels left of them (the «−100 … −700» of a chart read as «.5.», «2.»). The outermost
    # vertical lines (horizontal for x labels) anchor strips of their own.
    lefts = _anchors([left] + ([min(s[2] for s in vl)] if vl else []), th)
    rights = _anchors([right] + ([max(s[2] for s in vl)] if vl else []), th)
    tops = _anchors([top] + ([min(s[2] for s in hl)] if hl else []), th)
    bottoms = _anchors([bottom] + ([max(s[2] for s in hl)] if hl else []), th)
    strips = [(lx - 7 * th, top - th, lx - gap, bottom + th) for lx in lefts]           # y labels, left
    strips += [(rx + gap, top - th, rx + 7 * th, bottom + th) for rx in rights]         # y labels, right (second axis)
    strips += [(left - 2 * th, ty - 3.2 * th, right + 2 * th, ty - gap) for ty in tops]        # x labels, above
    strips += [(left - 2 * th, by + gap, right + 2 * th, by + 3.2 * th) for by in bottoms]     # x labels, below
    for a, b, c, d in strips:
        c0, r0, c1, r1 = int(max(0, a)), int(max(0, b)), int(min(w, c)), int(min(h, d))
        if c1 - c0 < 8 or r1 - r0 < 8:
            continue
        out += ocr_blobs(np.ascontiguousarray(gray[r0:r1, c0:c1]), (c0, r0), engine, wl)
    # the same label read from two overlapping strips: keep one
    uniq: list[Text] = []
    for t in out:
        if not any(u.text == t.text and abs(u.xc - t.xc) < t.h and abs(u.yc - t.yc) < t.h for u in uniq):
            uniq.append(t)
    return uniq


def rotated_date_axis(rgb: np.ndarray, coloured: np.ndarray, engine: OcrEngine, band, snap_x: list[float]):
    """x axis from date labels printed rotated by 90° (read bottom to top) in ``band`` = (x0, y0, x1, y1) below the
    plot (fd-0.1.5; «01.01.1980 … 01.01.2035» of a time chart were not read, the chart stayed X_UNCALIBRATED): the
    band is turned clockwise, read in sparse mode with a digits-and-dots whitelist, the dates mapped back to their
    columns (the label's thickness is its tick position) and fitted as a DATE axis — only labels that parse as dates
    count, misread ones are dropped (:func:`_subset_axis` logic); None when fewer than three dates line up."""
    import cv2

    from vkm_corpus.figures.calibrate import _snap

    h, w = coloured.shape
    x0, y0, x1, y1 = (int(round(v)) for v in band)
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
    if x1 - x0 < 20 or y1 - y0 < 20 or not engine.available():
        return None
    gray = cv2.cvtColor(np.ascontiguousarray(rgb[y0:y1, x0:x1]), cv2.COLOR_RGB2GRAY)
    gray = np.where(coloured[y0:y1, x0:x1], 255, gray).astype(np.uint8)
    rot = cv2.rotate(gray, cv2.ROTATE_90_CLOCKWISE)          # dst(x, y) = src(y, H − 1 − x)
    hb = gray.shape[0]
    found = ocr_words(rot, engine, psm=11, whitelist="0123456789.", gray=rot)
    labels = []
    for t in found:
        v, k = label_value(t.text)
        if v is None or k != "DATE":
            continue
        top, bottom = t.yc - t.h / 1.6, t.yc + t.h / 1.6     # the text's thickness in the turned image
        xc = x0 + (top + bottom) / 2
        yc = y0 + (hb - 1) - (t.x0 + t.x1) / 2
        labels.append(Text(t.text, xc - (bottom - top) / 2, xc + (bottom - top) / 2, yc, bottom - top, 90.0,
                           "OCR_ROTATED", "LOCAL_OCR"))
    if len(labels) < 3:
        return None
    hmed = float(np.median([t.h for t in labels]))
    rows = []
    for t in sorted(labels, key=lambda t: t.xc):
        q = _snap(t.xc, snap_x, t.h)
        rows.append([t.text, label_value(t.text)[0], q if q is not None else t.xc, q is not None, "LOCAL_OCR"])
    ax = fit_axis("x", rows, "DATE", hmed, 3, max_drop=max(1, len(rows) // 4), method_hint="OCR_ROTATED_LABELS",
                  label_source="LOCAL_OCR")
    if ax is None or not plausible_axis(ax, float(w)):
        return None
    ax.ocr = engine.provenance()
    return ax


# ------------------------------------------------------------------------------------------------ calibration
@dataclass
class PiecewiseAxis:
    """Monotone piecewise-linear axis through labelled positions (labels at irregular positions)."""

    orient: str
    positions: np.ndarray
    values: np.ndarray
    labels: list
    kind: str = "PIECEWISE"
    method: str = "TEXT_CENTRE"
    label_source: str = "LOCAL_OCR"
    residual_rms: float = 0.0
    residual_max: float = 0.0
    ocr: dict | None = None
    dropped: list = field(default_factory=list)

    def value(self, pos):
        return np.interp(np.asarray(pos, float), self.positions, self.values, left=np.nan, right=np.nan)

    def value_per_pos(self, pos) -> float:
        i = int(np.clip(np.searchsorted(self.positions, pos), 1, len(self.positions) - 1))
        return abs((self.values[i] - self.values[i - 1]) / (self.positions[i] - self.positions[i - 1]))

    @property
    def span(self):
        return float(self.positions.min()), float(self.positions.max())

    def to_json(self) -> dict:
        return {"orient": self.orient, "kind": self.kind, "method": self.method, "label_source": self.label_source,
                "ocr": self.ocr, "n_labels": len(self.labels), "residual_rms_drawing_units": self.residual_rms,
                "residual_max_drawing_units": self.residual_max,
                "labels": [{"text": t, "value": v, "pos": p, "snapped": s, "source": src}
                           for t, v, p, s, src in self.labels], "dropped_labels": self.dropped}


# Plausibility of an axis read by OCR (fd-0.1.5, MODEL_CHOICE): its labels span at least RASTER_MIN_SPAN of the
# analysed image along the axis (three numbers of a table cell 36 px apart made a log10 axis of a whole chart); a log10
# axis carries the mantissas of a printed log axis; an axis kept on at most four labels steps regularly. A row or
# column with misread labels keeps its largest subset on one line when that subset is at least RASTER_SUBSET_SHARE of
# it, at least three labels, regularly stepped (dropped labels are recorded); otherwise no axis — «не знаю» rather
# than a wrong scale.
RASTER_MIN_SPAN, RASTER_SUBSET_SHARE = 0.12, 0.5
_LOG_MANTISSAS = (1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0)


def _regular_values(values) -> bool:
    """Printed tick values: every step a whole multiple of the smallest (to 2 %), at most five steps skipped."""
    vals = sorted({float(v) for v in values})
    if len(vals) < 3:
        return False
    steps = [b - a for a, b in zip(vals, vals[1:])]
    s0 = min(steps)
    return s0 > 0 and all(abs(s / s0 - round(s / s0)) <= 0.02 and s / s0 <= 6.0 for s in steps)


def _log_mantissas(values) -> bool:
    for v in values:
        if v <= 0:
            return False
        m = v / 10 ** math.floor(math.log10(v) + 1e-9)
        if not (any(abs(m - q) <= 0.02 * q for q in _LOG_MANTISSAS) or abs(m - 10.0) <= 0.2):
            return False
    return True


def plausible_axis(ax, size: float | None) -> bool:
    """An OCR axis that may calibrate a chart (:data:`RASTER_MIN_SPAN`, regular steps, log mantissas)."""
    if ax is None:
        return False
    ps = [float(lab[2]) for lab in ax.labels]
    if size and (max(ps) - min(ps)) < RASTER_MIN_SPAN * size:
        return False
    vals = [float(lab[1]) for lab in ax.labels]
    if ax.kind == "LOG10":
        return _log_mantissas(vals)
    if ax.kind == "LINEAR" and len(vals) <= 4:
        return _regular_values(vals)
    return True


def _subset_axis(orient: str, rows: list, kind0: str, hmed: float, source: str):
    """The largest subset of labels on one straight line (every pair of labels proposes one), at least
    ``RASTER_SUBSET_SHARE`` of the row and three labels, regularly stepped; fitted on that subset, the others dropped."""
    n = len(rows)
    if n < 4 or kind0 == "DATE":
        return None
    pos = np.array([float(r[2]) for r in rows])
    val = np.array([float(r[1]) for r in rows])
    spacing = float(np.median(np.diff(np.sort(pos)))) if n > 1 else 1.0
    tol = max(0.3 * hmed, 0.08 * abs(spacing))
    best: list[int] = []
    for i in range(n):
        for j in range(i + 1, n):
            if pos[i] == pos[j] or val[i] == val[j]:
                continue
            b = (val[j] - val[i]) / (pos[j] - pos[i])
            inl = np.where(np.abs(pos - pos[i] - (val - val[i]) / b) <= tol)[0]
            if len(inl) > len(best):
                best = [int(k) for k in inl]
    if len(best) < 3 or len(best) < RASTER_SUBSET_SHARE * n or not _regular_values(val[best]):
        return None
    ax = fit_axis(orient, [rows[k] for k in best], kind0, hmed, 3, max_drop=0, label_source=source)
    if ax is not None:
        ax.dropped = [rows[k][0] for k in range(n) if k not in best]
    return ax


def calibrate(words: list[Text], snap_x: list[float], snap_y: list[float], plot_hint, engine: OcrEngine | None,
              extent: tuple[float, float] | None = None):
    """x axis from a row of numeric/date labels, y axis from a column; piecewise when the row is monotone but not
    linear (irregular positions). ``extent`` (width, height of the analysed image): an axis must be plausible
    (:func:`plausible_axis`) — misread labels are dropped (:func:`_subset_axis`) or the axis stays uncalibrated."""
    lab = []
    for t in words:
        v, k = label_value(t.text)
        if v is not None:
            lab.append((t, v, k))
    if not lab:
        return None, None
    hmed = float(np.median([t.h for t, _, _ in lab]))
    from vkm_corpus.figures.calibrate import _clusters, _snap

    wx, wy = extent if extent is not None else (None, None)
    best_x = best_y = sub_x = sub_y = None
    for row in _clusters(lab, key=lambda r: r[0].yc, tol=0.5 * hmed):
        if len(row) < 3 or len({k for *_, k in row}) != 1:
            continue
        kind0 = row[0][2]
        rows = []
        for t, v, _ in row:
            q = _snap(t.xc, snap_x, t.h)
            rows.append([t.text, v, q if q is not None else t.xc, q is not None, t.source])
        ax = fit_axis("x", rows, kind0, hmed, 3, max_drop=1, label_source=row[0][0].source)
        if ax is not None and not plausible_axis(ax, wx):
            ax = None
        if ax is None:
            sub = _subset_axis("x", rows, kind0, hmed, row[0][0].source)
            if sub is not None and plausible_axis(sub, wx) and (sub_x is None or len(sub.labels) > len(sub_x.labels)):
                sub_x = sub
            ax = _piecewise("x", rows)
            if ax is not None:
                ax.label_source = row[0][0].source
                if not plausible_axis(ax, wx):
                    ax = None
        if ax is not None and (best_x is None or len(ax.labels) > len(best_x.labels)):
            best_x = ax
    for key in (lambda r: r[0].x1, lambda r: r[0].xc):
        for col in _clusters(lab, key=key, tol=0.8 * hmed):
            if len(col) < 3 or len({k for *_, k in col}) != 1:
                continue
            rows = []
            for t, v, _ in col:
                q = _snap(t.yc, snap_y, t.h)
                rows.append([t.text, v, q if q is not None else t.yc, q is not None, t.source])
            ax = fit_axis("y", rows, col[0][2], hmed, 3, max_drop=1, label_source=col[0][0].source)
            if ax is not None and not plausible_axis(ax, wy):
                ax = None
            if ax is None:
                sub = _subset_axis("y", rows, col[0][2], hmed, col[0][0].source)
                if sub is not None and plausible_axis(sub, wy) and (sub_y is None or len(sub.labels) > len(sub_y.labels)):
                    sub_y = sub
            if ax is not None and (best_y is None or len(ax.labels) > len(best_y.labels)):
                best_y = ax
    # an axis on a subset of misread labels only where no row (column) fits as a whole: a column of legend numbers
    # with a few misread entries must not displace the axis
    best_x = best_x or sub_x
    best_y = best_y or sub_y
    for ax in (best_x, best_y):
        if ax is not None and engine is not None:
            ax.ocr = engine.provenance()
    return best_x, best_y


def _longest_monotone(rows: list, increasing: bool) -> list:
    """Longest strictly monotone subsequence of label values ordered by position (drops misread labels)."""
    n = len(rows)
    best = [1] * n
    prev = [-1] * n
    for i in range(n):
        for j in range(i):
            ok = rows[j][1] < rows[i][1] if increasing else rows[j][1] > rows[i][1]
            if ok and best[j] + 1 > best[i]:
                best[i], prev[i] = best[j] + 1, j
    if not n:
        return []
    i = int(np.argmax(best))
    out = []
    while i >= 0:
        out.append(rows[i])
        i = prev[i]
    return out[::-1]


def _piecewise(orient: str, rows: list, min_labels: int = 4) -> PiecewiseAxis | None:
    rows = sorted(rows, key=lambda r: r[2])
    cands = [_longest_monotone(rows, True), _longest_monotone(rows, False)]
    keep = max(cands, key=len)
    if len(keep) < min_labels or len(keep) < 0.5 * len(rows):
        return None
    pos = np.array([r[2] for r in keep], float)
    if np.any(np.diff(pos) <= 0):
        return None
    vals = np.array([r[1] for r in keep], float)
    dropped = [r[0] for r in rows if r not in keep]
    return PiecewiseAxis(orient, pos, vals, keep, dropped=dropped,
                         method="PIECEWISE_SNAPPED" if all(r[3] for r in keep) else "PIECEWISE_TEXT_CENTRE")


@dataclass
class MarkerAxis(PiecewiseAxis):
    """Profile-line axis: one marker per benchmark on the axis line; printed numbers identify the markers, the
    others are numbered by counting (benchmarks are consecutive) — robust to irregular spacing and unread labels."""

    kind: str = "MARKERS"


def marker_axis(orient: str, markers: list[float], words: list[Text], min_agree: int = 3) -> MarkerAxis | None:
    if len(markers) < 5:
        return None
    mk = np.asarray(sorted(markers), float)
    spacing = float(np.median(np.diff(mk)))
    offs = []
    rows = []
    for t in words:
        v, k = label_value(t.text)
        if v is None or k != "NUM" or v != int(v):
            continue
        p = t.xc if orient == "x" else t.yc
        j = int(np.argmin(np.abs(mk - p)))
        if abs(mk[j] - p) <= 0.5 * spacing:
            offs.append(int(v) - j)
            rows.append([t.text, float(v), float(mk[j]), True, t.source, j])
    if not offs:
        return None
    vals, counts = np.unique(offs, return_counts=True)
    off = int(vals[np.argmax(counts)])
    agree = [r for r in rows if int(r[1]) - r[5] == off]
    if len(agree) < min_agree or len(agree) < 0.6 * len(rows):
        return None
    numbers = np.arange(len(mk)) + off
    dropped = [r[0] for r in rows if int(r[1]) - r[5] != off]
    return MarkerAxis(orient, mk, numbers.astype(float), [r[:5] for r in agree], method="MARKERS_COUNTED",
                      dropped=dropped)


# ------------------------------------------------------------------------------------------------ curves


def track_curves(mask: np.ndarray, lab: np.ndarray, x0: int, x1: int, max_jump: float, max_run: float,
                 gap: int = 12, colour_weight: float = 0.4) -> list[dict]:
    """Column-by-column multi-curve tracking: every run of curve pixels in a column is matched to the track whose
    predicted position (last point + recent slope) is nearest, with the run's ink colour as a secondary cost; a run
    that matches no track starts one. Anti-aliased halos stay inside their run, so a curve is not split by colour."""
    h, w = mask.shape
    tracks: list[dict] = []
    active: list[int] = []
    for x in range(max(0, x0), min(w, x1 + 1)):
        col = np.where(mask[:, x])[0]
        runs = []
        if len(col):
            start = p = col[0]
            for v in list(col[1:]) + [None]:
                if v is None or v != p + 1:
                    if p - start + 1 <= max_run:
                        seg = lab[start:p + 1, x].reshape(-1, 3)
                        runs.append(((start + p) / 2, p - start + 1, seg.mean(0)))
                    if v is not None:
                        start = v
                if v is not None:
                    p = v
        # predictions
        preds = {}
        for ti in active:
            t = tracks[ti]
            if len(t["xs"]) >= 3:
                k = min(6, len(t["xs"]) - 1)
                slope = (t["ys"][-1] - t["ys"][-1 - k]) / max(1, t["xs"][-1] - t["xs"][-1 - k])
            else:
                slope = 0.0
            preds[ti] = (t["ys"][-1] + slope * (x - t["xs"][-1]), abs(slope))
        cand = []
        for ri, (c, wd, colr) in enumerate(runs):
            for ti, (py, s) in preds.items():
                dy = abs(c - py)
                if dy <= max_jump + 2 * s:
                    cand.append((dy + colour_weight * de_lab(colr, tracks[ti]["colour"]), ri, ti))
        cand.sort()
        used_r, used_t = set(), set()
        for cost, ri, ti in cand:
            if ri in used_r or ti in used_t:
                continue
            c, wd, colr = runs[ri]
            t = tracks[ti]
            t["xs"].append(x)
            t["ys"].append(c)
            t["ws"].append(wd)
            n = len(t["xs"])
            t["colour"] = t["colour"] + (colr - t["colour"]) / min(n, 20)
            used_r.add(ri)
            used_t.add(ti)
        for ri, (c, wd, colr) in enumerate(runs):
            if ri not in used_r:
                tracks.append({"xs": [x], "ys": [c], "ws": [wd], "colour": colr.astype(float)})
                active.append(len(tracks) - 1)
        active = [ti for ti in active if x - tracks[ti]["xs"][-1] <= gap]
    for t in tracks:
        t["xs"] = np.array(t["xs"], float)
        t["ys"] = np.array(t["ys"], float)
        t["width"] = float(np.median(t["ws"]))
    return tracks


def _end_slope(t: dict, at_end: bool, k: int = 8) -> float:
    xs, ys = t["xs"], t["ys"]
    if len(xs) < 3:
        return 0.0
    k = min(k, len(xs) - 1)
    if at_end:
        return float((ys[-1] - ys[-1 - k]) / max(1.0, xs[-1] - xs[-1 - k]))
    return float((ys[k] - ys[0]) / max(1.0, xs[k] - xs[0]))


def join_tracks(tracks: list[dict], max_gap_x: float, max_dy: float, max_de: float = 30.0,
                bridges: list[tuple[float, float, float, float]] = ()) -> list[dict]:
    """Join fragments of one curve, cheapest pair first: the next fragment starts after the previous ends, where
    the previous one's end slope predicts it, with a similar ink colour. ``bridges`` (x0, x1, y0, y1): text boxes an
    inline label cuts out of its curve — a gap covered by one may be as long as the box. The pair costs are computed
    as arrays (fd-0.1.5: the same choices as the pairwise loop, hundreds of fragments in seconds, not minutes)."""
    tracks = [dict(t) for t in tracks]
    while len(tracks) > 1:
        n = len(tracks)
        ex = np.array([float(t["xs"][-1]) for t in tracks])
        ey = np.array([float(t["ys"][-1]) for t in tracks])
        sx = np.array([float(t["xs"][0]) for t in tracks])
        sy = np.array([float(t["ys"][0]) for t in tracks])
        sa = np.array([_end_slope(t, True) for t in tracks])
        col = np.array([np.asarray(t["colour"], float) for t in tracks])
        dx = sx[None, :] - ex[:, None]                       # [i, j]: gap from the end of i to the start of j
        ya, yb = ey[:, None], sy[None, :]
        allowed = np.full((n, n), float(max_gap_x))
        for bx0, bx1, by0, by1 in bridges:
            hit = ((bx0 - max_gap_x <= ex[:, None]) & (ex[:, None] <= bx1) & (bx0 <= sx[None, :])
                   & (sx[None, :] <= bx1 + max_gap_x) & (by0 - max_dy <= (ya + yb) / 2) & ((ya + yb) / 2 <= by1 + max_dy))
            allowed = np.where(hit, np.maximum(allowed, (bx1 - bx0) + 2 * max_gap_x), allowed)
        d = col[:, None, :] - col[None, :, :]
        de = np.sqrt((d[..., 0] * 100 / 255) ** 2 + d[..., 1] ** 2 + d[..., 2] ** 2)
        # a matching ink colour tolerates a longer gap and a larger jump (a faint stretch at the bottom of a trough
        # breaks the tracking); the gap itself is never filled with values
        limit = np.where(de <= 12, np.maximum(allowed, 3 * max_gap_x), allowed)
        pred = ya + sa[:, None] * dx
        dy = np.minimum(np.abs(yb - pred), np.abs(yb - ya))
        slack = np.where(de <= 12, 0.4 * dx, 0.05 * dx)
        ok = (dx > 0) & (dx <= limit) & (dy <= max_dy + slack) & (de <= max_de)
        np.fill_diagonal(ok, False)
        if not ok.any():
            return tracks
        cost = np.where(ok, dy + 0.05 * dx + 0.3 * de, np.inf)
        i, j = (int(v) for v in np.unravel_index(int(np.argmin(cost)), cost.shape))
        a, b = tracks[i], tracks[j]
        a.setdefault("gaps", []).append((float(a["xs"][-1]), float(b["xs"][0])))
        a["gaps"] += b.get("gaps", [])
        a["xs"] = np.concatenate([a["xs"], b["xs"]])
        a["ys"] = np.concatenate([a["ys"], b["ys"]])
        a["width"] = float(np.median([a["width"], b["width"]]))
        na, nb = len(a["xs"]) - len(b["xs"]), len(b["xs"])
        a["colour"] = (a["colour"] * na + b["colour"] * nb) / (na + nb)
        tracks.pop(j)
    return tracks


def series_markers(curve_mask: np.ndarray, lab: np.ndarray, colour, xs: np.ndarray, ys: np.ndarray, width: float,
                   max_de: float = 18.0) -> list[tuple[float, float]]:
    """Data markers of a series (diamonds, triangles, squares): pixels of the series colour near its trace, opened
    with a disk wider than the line (the line disappears, markers stay); centroids of the remaining blobs."""
    import cv2

    h, w = curve_mask.shape
    near = np.zeros((h, w), np.uint8)
    pts = np.stack([xs, ys], 1).round().astype(np.int32)
    reach = int(max(6, 4 * width))
    cv2.polylines(near, [pts], False, 255, 2 * reach + 1)
    d = lab.astype(np.float32) - np.asarray(colour, np.float32)
    de = np.sqrt((d[..., 0] * 100 / 255) ** 2 + d[..., 1] ** 2 + d[..., 2] ** 2)
    m = (curve_mask & (near > 0) & (de <= max_de)).astype(np.uint8) * 255
    k = int(max(3, round(2.2 * width))) | 1
    opened = cv2.morphologyEx(m, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    n, cc, stats, cents = cv2.connectedComponentsWithStats(opened, 8)
    if n <= 1:
        return []
    areas = stats[1:, cv2.CC_STAT_AREA]
    med = float(np.median(areas))
    out = [(float(cents[i][0]), float(cents[i][1])) for i in range(1, n)
           if 0.35 * med <= stats[i, cv2.CC_STAT_AREA] <= 3.0 * med]
    return sorted(out)


def simplify(xs: np.ndarray, ys: np.ndarray, eps: float) -> np.ndarray:
    """Douglas–Peucker indices of a polyline."""
    pts = np.stack([xs, ys], 1)
    keep = np.zeros(len(pts), bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        a, b = pts[i], pts[j]
        d = b - a
        n = math.hypot(*d) or 1.0
        dist = np.abs(d[0] * (pts[i + 1:j, 1] - a[1]) - d[1] * (pts[i + 1:j, 0] - a[0])) / n
        k = int(np.argmax(dist))
        if dist[k] > eps:
            m = i + 1 + k
            keep[m] = True
            stack += [(i, m), (m, j)]
    return np.where(keep)[0]


def polyline_vertices(xs: np.ndarray, ys: np.ndarray, width: float, plot_w: float, max_vertices: int = 80):
    """Vertices of a chart drawn with straight segments (a line chart: vertices = plotted data points), or None
    for a smooth curve (short segments everywhere)."""
    if len(xs) < 10:
        return None
    idx = simplify(xs, ys, max(1.0, 0.6 * width))
    if len(idx) > max_vertices:
        return None
    seg = np.diff(xs[idx])
    if len(seg) == 0 or np.median(seg) < 0.03 * plot_w:
        return None
    # a rounded or thick corner can give two vertices a few pixels apart: keep the sharper one
    med = float(np.median(seg))
    keep = list(idx)
    changed = True
    while changed and len(keep) > 2:
        changed = False
        for k in range(1, len(keep) - 1):
            if xs[keep[k + 1]] - xs[keep[k]] < 0.35 * med and k + 1 < len(keep) - 1:
                def turn(j):
                    a, b, c = keep[j - 1], keep[j], keep[j + 1]
                    v1 = np.array([xs[b] - xs[a], ys[b] - ys[a]])
                    v2 = np.array([xs[c] - xs[b], ys[c] - ys[b]])
                    return abs(math.atan2(v1[0] * v2[1] - v1[1] * v2[0], float(v1 @ v2)))
                drop = k if turn(k) < turn(k + 1) else k + 1
                keep.pop(drop)
                changed = True
                break
    return np.array(keep)


def overlay_qc_all(mask: np.ndarray, curves) -> dict:
    """Figure-level re-plot check: recall = share of curve ink within the redrawn curves (stroke width + 2 px);
    precision = share of redrawn pixels that lie on ink."""
    import cv2

    draw = np.zeros(mask.shape, np.uint8)
    for xs, ys, width in curves:
        if len(xs) >= 2:
            pts = np.stack([xs, ys], 1).round().astype(np.int32)
            cv2.polylines(draw, [pts], False, 255, max(1, int(round(width))))
    near = cv2.dilate(draw, np.ones((5, 5), np.uint8), iterations=1) > 0
    m = mask.astype(bool)
    ink_near = cv2.dilate(m.astype(np.uint8) * 255, np.ones((3, 3), np.uint8), iterations=1) > 0
    return {"ink_recall": round(float((m & near).sum() / max(1, m.sum())), 3),
            "redraw_precision": round(float(((draw > 0) & ink_near).sum() / max(1, (draw > 0).sum())), 3)}


def track_qc(mask: np.ndarray, xs: np.ndarray, ys: np.ndarray, width: float) -> dict:
    """Per-curve check: share of traced columns with ink within ±1 px of the centre; column coverage of its span."""
    if len(xs) == 0:
        return {"ink_support": 0.0, "coverage": 0.0}
    h = mask.shape[0]
    ok = 0
    for x, y in zip(xs.astype(int), ys):
        a, b = int(max(0, round(y) - 1)), int(min(h - 1, round(y) + 1))
        ok += bool(mask[a:b + 1, x].any())
    span = xs.max() - xs.min() + 1
    return {"ink_support": round(ok / len(xs), 3), "coverage": round(float(len(np.unique(xs)) / span), 3)}

"""Tick labels that are not text: rotated labels exported as glyph outlines (vector figures) and labels of raster
figures.

Order (project rule «OCR только через конвейер» for corpus text): (1) text the corpus already has for the region
(canonical blocks, OCR_RAW), passed in by the caller; (2) a local OCR helper used *only* to calibrate this derived
object — Tesseract (CPU, deterministic, single-line mode with a character whitelist). The helper's engine, version and
settings are written into the calibration provenance; its output never enters the canon.

Glyph outlines: small black pieces outside the plot area are clustered into labels (single linkage); each label's
direction comes from PCA of its points; the label is rendered from the PDF at high resolution, masked to its own
oriented rectangle (neighbouring rotated labels overlap in an axis-aligned box), rotated to horizontal and read. The
label is anchored at its end nearest to the plot area and snapped to the nearest tick or grid line.
"""
from __future__ import annotations

import math
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path as FsPath

import numpy as np

from vkm_corpus.figures.calibrate import Axis, fit_axis
from vkm_corpus.figures.primitives import Path, is_black, is_greyish, label_value

WHITELIST = "0123456789.,-/"


@dataclass
class OcrEngine:
    name: str = "tesseract"
    exe: str | None = None
    lang: str = "eng"
    psm: int = 7
    whitelist: str = WHITELIST
    dpi: int = 600
    version: str | None = None
    calls: int = 0

    def available(self) -> bool:
        self.exe = self.exe or shutil.which("tesseract")
        if self.exe and self.version is None:
            try:
                out = subprocess.run([self.exe, "--version"], capture_output=True, text=True, timeout=20)
                self.version = (out.stdout or out.stderr).splitlines()[0].strip()
            except Exception:  # noqa: BLE001
                self.version = "unknown"
        return bool(self.exe)

    def provenance(self) -> dict:
        return {"engine": self.name, "version": self.version, "lang": self.lang, "psm": self.psm,
                "whitelist": self.whitelist, "render_dpi": self.dpi, "calls": self.calls,
                "use": "calibration of a derived object only; not corpus text, not canon"}

    def read_line(self, gray: np.ndarray) -> str:
        """OCR of one text line (uint8 grayscale, dark text on white)."""
        import cv2

        if not self.available():
            raise RuntimeError("tesseract not found")
        with tempfile.TemporaryDirectory() as tmp:
            f = FsPath(tmp) / "line.png"
            cv2.imwrite(str(f), gray)
            cmd = [self.exe, str(f), "stdout", "--psm", str(self.psm), "-l", self.lang,
                   "-c", f"tessedit_char_whitelist={self.whitelist}"]
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        self.calls += 1
        return (out.stdout or "").strip()


# ------------------------------------------------------------------------------------------------ glyph labels
@dataclass
class GlyphLabel:
    points: np.ndarray            # all vertices of the label's pieces (drawing units)
    angle_deg: float              # direction of the baseline (drawing frame, y up)
    center: np.ndarray
    length: float
    height: float
    anchor: np.ndarray            # end nearest to the plot area
    text: str = ""
    value: float | None = None
    kind: str | None = None
    extra: dict = field(default_factory=dict)


def glyph_pieces(paths: list[Path], box, max_size: float) -> list[Path]:
    """Small dark pieces outside the plot box: glyph fills and their boundary segments (``-PDFIMPORT`` turns a
    filled glyph into a fill plus boundary splines on the geometry layer, which carry the layer's colour)."""
    x0, y0, x1, y1 = box
    out = []
    for p in paths:
        if not (is_black(p.color) or is_greyish(p.color)):
            continue
        bx0, by0, bx1, by1 = p.bbox
        if max(bx1 - bx0, by1 - by0) > max_size:
            continue
        cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            continue
        out.append(p)
    return out


def cluster_labels(pieces: list[Path], eps: float, box) -> list[GlyphLabel]:
    """Single-linkage clusters of piece centres. ``eps`` ≈ 1.1 × the cap height of the figure's text: glyphs of a
    label are closer than that, neighbouring rotated labels are several cap heights apart."""
    from scipy.spatial import cKDTree

    if not pieces:
        return []
    cents = np.array([[(p.bbox[0] + p.bbox[2]) / 2, (p.bbox[1] + p.bbox[3]) / 2] for p in pieces])
    parent = list(range(len(pieces)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i, j in cKDTree(cents).query_pairs(eps):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj
    groups: dict[int, list[int]] = {}
    for i in range(len(pieces)):
        groups.setdefault(find(i), []).append(i)
    x0, y0, x1, y1 = box
    labels = []
    for idx in groups.values():
        pts = np.vstack([pieces[i].dense if pieces[i].dense is not None and len(pieces[i].dense) else pieces[i].pts
                         for i in idx])
        if len(idx) < 2:
            continue
        c = pts.mean(0)
        u, s, vt = np.linalg.svd(pts - c, full_matrices=False)
        d = vt[0]
        if d[0] < 0:
            d = -d
        proj = (pts - c) @ d
        perp = (pts - c) @ np.array([-d[1], d[0]])
        ends = [c + d * proj.min(), c + d * proj.max()]

        def dist_to_box(q):
            dx = max(x0 - q[0], 0.0, q[0] - x1)
            dy = max(y0 - q[1], 0.0, q[1] - y1)
            return math.hypot(dx, dy)
        anchor = min(ends, key=dist_to_box)
        labels.append(GlyphLabel(pts, math.degrees(math.atan2(d[1], d[0])), c, float(proj.max() - proj.min()),
                                 float(perp.max() - perp.min()), anchor))
    return labels


def render_label(pdf_page, to_pdf, label: GlyphLabel, dpi: int, pad: float, y_down: bool = False) -> np.ndarray:
    """Render the label's axis-aligned box from the PDF page, blank everything outside its oriented rectangle,
    rotate the text to horizontal and crop. ``to_pdf(X, Y)`` maps drawing units to PyMuPDF page coordinates;
    ``y_down``: the drawing frame is the page frame (route A) rather than a y-up drawing (route B)."""
    import cv2
    import pymupdf

    xs, ys = to_pdf(label.points[:, 0], label.points[:, 1])
    r = pymupdf.Rect(float(np.min(xs)) - pad, float(np.min(ys)) - pad, float(np.max(xs)) + pad, float(np.max(ys)) + pad)
    pix = pdf_page.get_pixmap(dpi=dpi, clip=r, colorspace=pymupdf.csGRAY, alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).copy()
    sc = dpi / 72.0
    px = (np.asarray(xs) - r.x0) * sc
    py = (np.asarray(ys) - r.y0) * sc
    # oriented rectangle of the label in pixel space (y down)
    pts = np.stack([px, py], 1).astype(np.float32)
    rect = cv2.minAreaRect(pts)
    (cx, cy), (w, h), ang = rect
    grow = pad * sc
    box = cv2.boxPoints(((cx, cy), (w + 2 * grow, h + 2 * grow), ang)).astype(np.int32)
    mask = np.zeros_like(img)
    cv2.fillPoly(mask, [box], 255)
    img = np.where(mask > 0, img, 255).astype(np.uint8)
    # rotate so that the baseline is horizontal: the label is tilted counter-clockwise by angle_deg as seen on the
    # page (drawing frame y up); OpenCV's positive angle is counter-clockwise, so undo it with -angle_deg
    theta = -label.angle_deg if y_down else label.angle_deg
    hh, ww = img.shape
    M = cv2.getRotationMatrix2D((ww / 2, hh / 2), -theta, 1.0)
    cos, sin = abs(M[0, 0]), abs(M[0, 1])
    nw, nh = int(hh * sin + ww * cos) + 2, int(hh * cos + ww * sin) + 2
    M[0, 2] += nw / 2 - ww / 2
    M[1, 2] += nh / 2 - hh / 2
    rot = cv2.warpAffine(img, M, (nw, nh), flags=cv2.INTER_CUBIC, borderValue=255)
    ink = np.argwhere(rot < 128)
    if len(ink):
        (r0, c0), (r1, c1) = ink.min(0), ink.max(0)
        rot = rot[max(0, r0 - 2):r1 + 3, max(0, c0 - 2):c1 + 3]
    return cv2.copyMakeBorder(rot, 12, 12, 12, 12, cv2.BORDER_CONSTANT, value=255)


def read_glyph_labels(labels: list[GlyphLabel], pdf_page, to_pdf, engine: OcrEngine, pad_pt: float = 1.5,
                      y_down: bool = False):
    for lab in labels:
        img = render_label(pdf_page, to_pdf, lab, engine.dpi, pad_pt, y_down)
        txt = engine.read_line(img)
        lab.text = re.sub(r"\s+", "", txt)
        v, k = label_value(lab.text)
        # a date read upside down or mirrored does not parse; try the opposite direction once
        if v is None:
            img2 = np.rot90(img, 2).copy()
            txt2 = re.sub(r"\s+", "", engine.read_line(img2))
            v2, k2 = label_value(txt2)
            if v2 is not None:
                lab.text, v, k = txt2, v2, k2
                lab.extra["flipped"] = True
        lab.value, lab.kind = v, k
    return labels


def make_glyph_reader(pdf_page, to_pdf, engine: OcrEngine, max_size: float, default_h: float,
                      min_len_factor: float = 1.0, max_len_factor: float = 25.0, y_down: bool = False):
    """A ``glyph_reader(orient, texts, paths, box)`` for :func:`core.digitize`: labels drawn as glyph outlines
    next to the missing axis, read by the local OCR helper. ``to_pdf`` maps drawing units to page points of
    ``pdf_page``; sizes are in drawing units."""
    from vkm_corpus.figures.calibrate import structure_lines

    def reader(orient, texts, paths, box):
        if not engine.available():
            return None
        hmed = float(np.median([t.h for t in texts])) if texts else default_h
        pieces = glyph_pieces(paths, box, max_size=max_size)
        labels = cluster_labels(pieces, eps=1.1 * hmed, box=box)
        x0, y0, x1, y1 = box
        tol = 0.25 * hmed
        if orient == "x":
            labels = [lb for lb in labels if lb.anchor[1] > y1 - tol or lb.anchor[1] < y0 + tol]
        else:
            labels = [lb for lb in labels if lb.anchor[0] < x0 + tol or lb.anchor[0] > x1 - tol]
        labels = [lb for lb in labels if min_len_factor * hmed <= lb.length <= max_len_factor * hmed]
        if len(labels) < 3:
            return None
        read_glyph_labels(labels, pdf_page, to_pdf, engine, y_down=y_down)
        hs, vs = structure_lines(paths)
        ax = axis_from_glyph_labels(orient, labels, vs if orient == "x" else hs, engine)
        reader.labels = labels
        return ax
    reader.labels = []
    return reader


def axis_from_glyph_labels(orient: str, labels: list[GlyphLabel], snap: list[float], engine: OcrEngine,
                           min_labels: int = 3) -> Axis | None:
    """Anchor each read label to the nearest tick/grid line (orientation ``x``: vertical lines) and fit."""
    good = [lab for lab in labels if lab.value is not None]
    if len(good) < min_labels:
        return None
    kinds = {lab.kind for lab in good}
    kind0 = "DATE" if "DATE" in kinds else "NUM"
    good = [lab for lab in good if lab.kind == kind0]
    hmed = float(np.median([lab.height for lab in good]))
    arr = np.asarray(snap) if snap else None
    rows = []
    for lab in good:
        p = float(lab.anchor[0] if orient == "x" else lab.anchor[1])
        q = None
        if arr is not None and len(arr):
            j = int(np.argmin(np.abs(arr - p)))
            if abs(arr[j] - p) <= 1.5 * hmed:
                q = float(arr[j])
        rows.append([lab.text, lab.value, q if q is not None else p, q is not None, "LOCAL_OCR"])
    ax = fit_axis(orient, rows, kind0, hmed, min_labels, max_drop=max(2, len(rows) // 4),
                  method_hint="OCR_GLYPH_LABELS", label_source="LOCAL_OCR")
    if ax is not None:
        ax.ocr = engine.provenance()
    return ax

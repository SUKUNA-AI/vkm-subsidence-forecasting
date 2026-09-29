"""Single-figure PDF for AutoCAD ``-PDFIMPORT``.

A PDF page can carry text the viewer never shows (text placed outside the MediaBox, left over by layout software):
``-PDFIMPORT`` imports it onto the drawing, where it can fall into a figure. The figure is therefore cut out first:
one page, CropBox = figure box. ``-PDFIMPORT`` takes the CropBox as the drawing origin (lower-left corner, 1 unit =
1 inch) but still imports everything of the page, so after the import paths are clipped to the box
(``dxf_route.clip``) and MTEXT is kept only where the visible PDF text layer confirms it (``dxf_route.refine_texts``:
off-page text has no visible counterpart and is dropped).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CropInfo:
    box_unrotated_pt: tuple[float, float, float, float]   # PyMuPDF coordinates (top-left origin), unrotated page
    page_rotation: int
    page_height_pt: float
    chars_before: int
    chars_after: int
    chars_outside_after: int
    mode: str = "cropbox"

    def dxf_to_page(self, X, Y):
        """DXF inches (origin = lower-left of the CropBox, y up) → PAGE_PT_TL of the unrotated page."""
        x0, y0, x1, y1 = self.box_unrotated_pt
        return x0 + X * 72.0, y1 - Y * 72.0

    def to_json(self) -> dict:
        return {"box_unrotated_pt": list(self.box_unrotated_pt), "page_rotation": self.page_rotation,
                "page_height_pt": self.page_height_pt, "chars_before": self.chars_before,
                "chars_after": self.chars_after, "chars_outside_after": self.chars_outside_after,
                "mode": self.mode,
                "method": ("single page copied unchanged, CropBox = figure box" if self.mode == "cropbox" else
                           "text outside the box redacted (incl. off-page text), CropBox = figure box")}


def crop_figure_pdf(src_pdf, page_no: int, bbox_pt, out_pdf, margin: float = 12.0,
                    redact_text: bool = False) -> CropInfo:
    """``bbox_pt``: figure box in PAGE_PT_TL of the displayed page (canon coordinates); ``page_no`` from 1.

    Default: the page is copied unchanged and only its CropBox is set. ``redact_text=True`` also removes the text
    outside the box, but MuPDF's redaction rewrites the whole content stream, and ``-PDFIMPORT`` of the rewritten
    page lost the colour of a stroke in a test — so off-page text is dropped after the import instead
    (``dxf_route.refine_texts`` keeps only MTEXT that the visible PDF text layer confirms)."""
    import pymupdf

    doc = pymupdf.open(src_pdf)
    out = pymupdf.open()
    out.insert_pdf(doc, from_page=page_no - 1, to_page=page_no - 1)
    page = out[0]
    shown = pymupdf.Rect(page.rect)
    r = (pymupdf.Rect(bbox_pt) + (-margin, -margin, margin, margin)) & shown
    if page.rotation:
        r = (r * page.derotation_matrix).normalize()
    before = sum(len(s["chars"]) for s in page.get_texttrace())
    if redact_text:
        big = 1.0e4
        for rr in (pymupdf.Rect(-big, -big, big, r.y0), pymupdf.Rect(-big, r.y1, big, big),
                   pymupdf.Rect(-big, r.y0, r.x0, r.y1), pymupdf.Rect(r.x1, r.y0, big, r.y1)):
            page.add_redact_annot(rr)
        # line art is left alone: MuPDF treats a zero-height path (a flat series, an axis line) as covered by any
        # redaction rectangle and would delete it
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                              graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                              text=pymupdf.PDF_REDACT_TEXT_REMOVE)
    trace = page.get_texttrace()
    after = sum(len(s["chars"]) for s in trace)
    outside = sum(1 for s in trace for c in s["chars"] if not pymupdf.Rect(c[3]).intersects(r))
    height = float(page.mediabox.height)
    # page coordinates are relative to the page's own CropBox; set_cropbox takes MediaBox-relative coordinates
    off = page.cropbox_position
    page.set_cropbox((r + (off.x, off.y, off.x, off.y)) & page.mediabox)
    out.save(out_pdf, garbage=1)
    return CropInfo((float(r.x0), float(r.y0), float(r.x1), float(r.y1)), int(page.rotation), height, before, after,
                    outside, "redact_text+cropbox" if redact_text else "cropbox")

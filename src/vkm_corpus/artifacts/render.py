"""Deterministic rendering helpers: PDF pages (PyMuPDF), DjVu pages (DjVuLibre ``ddjvu``), crops, previews.

Cache keys use the *pixel* hash (decoded buffer + size + mode), not the bytes of an encoded file, so a change of the
PNG encoder does not invalidate model caches (H-05). Heavy libraries are imported lazily.
"""
from __future__ import annotations

import hashlib
import io
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RENDER_PROFILES: dict[str, dict[str, Any]] = {
    # layout input and full-page OCR (the dpi GLM-OCR's SDK renders PDF pages with)
    "r200c": {"dpi": 200, "mode": "RGB"},
    # OCR crops of born-digital pages and 600-dpi bitonal scans (downsampled)
    "r300g": {"dpi": 300, "mode": "L"},
    # OCR crops of low-resolution scans: native resolution, no upsampling (profile resolved per page)
    "rnatg": {"dpi": None, "mode": "L"},
    # preview for visual rerank and the API (long side 1024 px, JPEG q85)
    "prev1024": {"long_side": 1024, "format": "JPEG", "quality": 85},
}


@dataclass
class Raster:
    """A decoded image with its identity."""

    image: Any  # PIL.Image.Image
    dpi: float
    profile: str

    @property
    def width(self) -> int:
        return self.image.width

    @property
    def height(self) -> int:
        return self.image.height

    @property
    def mode(self) -> str:
        return self.image.mode

    @property
    def pixel_sha256(self) -> str:
        return pixel_sha256(self.image)


def pixel_sha256(image: Any) -> str:
    """sha256 of the decoded pixel buffer with its size and mode (encoder-independent cache key)."""
    h = hashlib.sha256()
    h.update(f"vkm-pix-v1|{image.mode}|{image.width}|{image.height}|".encode("ascii"))
    h.update(image.tobytes())
    return h.hexdigest()


def png_bytes(image: Any) -> bytes:
    """Lossless PNG with fixed encoder settings (no metadata chunks)."""
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=False, compress_level=6)
    return buf.getvalue()


def preview_jpeg(image: Any, long_side: int = 1024, quality: int = 85) -> tuple[bytes, int, int]:
    """Downscaled JPEG preview (visual rerank, API page image). Returns (bytes, width, height)."""
    from PIL import Image

    img = image
    if img.mode not in ("L", "RGB"):
        img = img.convert("RGB")
    scale = long_side / max(img.width, img.height)
    if scale < 1.0:
        size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
        img = img.resize(size, Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=False, progressive=False, subsampling=2)
    return buf.getvalue(), img.width, img.height


def ink_ratio(image: Any, threshold: int = 200, sample_step: int = 2) -> float:
    """Share of 'ink' (dark) pixels on a subsampled grey version; used for EMPTY and empty-on-ink checks."""
    import numpy as np

    g = image.convert("L")
    a = np.asarray(g)[::sample_step, ::sample_step]
    if a.size == 0:
        return 0.0
    return float((a < threshold).mean())


# ---------------------------------------------------------------------------------------------------- PDF
def render_pdf_page(page: Any, dpi: float, mode: str = "RGB", profile: str = "") -> Raster:
    """Render a PyMuPDF page (rotation applied, CropBox) to a PIL image."""
    import pymupdf
    from PIL import Image

    cs = pymupdf.csGRAY if mode == "L" else pymupdf.csRGB
    pix = page.get_pixmap(dpi=int(round(dpi)), colorspace=cs, alpha=False, annots=True)
    img = Image.frombytes("L" if pix.n == 1 else "RGB", (pix.width, pix.height), pix.samples)
    return Raster(image=img, dpi=float(int(round(dpi))), profile=profile)


# ---------------------------------------------------------------------------------------------------- DjVu
def render_djvu_page(path: Path, page_index: int, dpi: float, mode: str = "L", profile: str = "",
                     timeout: float = 120.0, ddjvu: str = "ddjvu") -> Raster:
    """Render DjVu page ``page_index`` (1-based) with ``ddjvu`` at ``dpi`` (the page's own dpi scales to this)."""
    from PIL import Image

    fmt = "pgm" if mode == "L" else "ppm"
    with tempfile.TemporaryDirectory(prefix="vkm-ddjvu-") as td:
        out = Path(td) / f"p.{fmt}"
        cmd = [ddjvu, f"-format={fmt}", f"-page={page_index}", f"-scale={int(round(dpi))}", str(path), str(out)]
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if proc.returncode != 0 or not out.exists():
            raise RuntimeError(f"RENDER_FAILED: ddjvu exit {proc.returncode}: "
                               f"{proc.stderr.decode('utf-8', 'replace')[:300]}")
        img = Image.open(out)
        img.load()
        if img.mode != mode:
            img = img.convert(mode)
    return Raster(image=img, dpi=float(int(round(dpi))), profile=profile)


# ---------------------------------------------------------------------------------------------------- crops
def crop_pt(raster: Raster, bbox_pt: tuple[float, float, float, float], pad_pt: float = 2.0) -> Any:
    """Crop a PAGE_PT_TL box (points) from a page raster, with a small padding, clamped to the image."""
    s = raster.dpi / 72.0
    x0, y0, x1, y1 = bbox_pt
    left = max(0, int((x0 - pad_pt) * s))
    top = max(0, int((y0 - pad_pt) * s))
    right = min(raster.width, int(round((x1 + pad_pt) * s)) + 1)
    bottom = min(raster.height, int(round((y1 + pad_pt) * s)) + 1)
    if right <= left or bottom <= top:
        raise ValueError(f"empty crop for bbox {bbox_pt}")
    return raster.image.crop((left, top, right, bottom))


def crop_box_px(raster: Raster, bbox_pt: tuple[float, float, float, float], pad_pt: float = 2.0) -> list[int]:
    s = raster.dpi / 72.0
    x0, y0, x1, y1 = bbox_pt
    return [max(0, int((x0 - pad_pt) * s)), max(0, int((y0 - pad_pt) * s)),
            min(raster.width, int(round((x1 + pad_pt) * s)) + 1),
            min(raster.height, int(round((y1 + pad_pt) * s)) + 1)]

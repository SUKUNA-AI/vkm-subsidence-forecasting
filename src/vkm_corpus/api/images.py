"""Images served by the API and sent to the visual reranker: size limits (H-45: long side 1024 by default) and the
pixel → page transform, so a bbox in PAGE_PT_TL can be drawn on the served image.

``pt_x = x0 + px_x * sx`` and ``pt_y = y0 + px_y * sy`` (points, top-left origin of the upright page).
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

from vkm_corpus.api.errors import ApiFailure

DEFAULT_MAX_SIDE = 1024
MIN_MAX_SIDE, MAX_MAX_SIDE = 128, 2048
MAX_ENCODED_BYTES = 1_500_000
JPEG_QUALITY = 85


@dataclass(frozen=True)
class PreparedImage:
    data: bytes
    media_type: str
    size: tuple[int, int]
    original_size: tuple[int, int]
    scale: float                     # served px / original px


def image_size(data: bytes) -> tuple[int, int]:
    image = _open(data)
    return image.size


def _open(data: bytes) -> Any:
    try:
        from PIL import Image
    except ImportError as exc:
        raise ApiFailure("DEPENDENCY_UNAVAILABLE", "Pillow is not installed on the API host", stage="image",
                         tool="pillow") from exc
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as exc:  # noqa: BLE001 - decoding errors, decompression bombs
        raise ApiFailure("ARTIFACT_NOT_DECODABLE", f"stored image cannot be decoded ({type(exc).__name__})",
                         stage="image", tool="pillow") from exc
    return image


def prepare(data: bytes, media_type: str, max_side: int = DEFAULT_MAX_SIDE, fmt: str = "auto") -> PreparedImage:
    """Downscale to ``max_side`` (never upscale); PNG stays PNG when small enough, otherwise JPEG q85."""
    if not MIN_MAX_SIDE <= max_side <= MAX_MAX_SIDE:
        raise ApiFailure("INVALID_ARGUMENT", f"max_side must be {MIN_MAX_SIDE}…{MAX_MAX_SIDE}")
    if fmt not in ("auto", "png", "jpeg"):
        raise ApiFailure("INVALID_ARGUMENT", "format is auto, png or jpeg")
    image = _open(data)
    original = image.size
    scale = min(1.0, max_side / max(original))
    unchanged = scale >= 1.0 and (fmt == "auto" or media_type == f"image/{fmt}") and \
        media_type in ("image/png", "image/jpeg") and len(data) <= MAX_ENCODED_BYTES
    if unchanged:
        return PreparedImage(data, media_type, original, original, 1.0)
    from PIL import Image

    if scale < 1.0:
        image = image.resize((max(1, round(original[0] * scale)), max(1, round(original[1] * scale))),
                             Image.Resampling.LANCZOS)
    size = image.size
    want_png = fmt == "png" or (fmt == "auto" and media_type == "image/png")
    if want_png:
        out = io.BytesIO()
        image.save(out, format="PNG", optimize=True)
        if fmt == "png" or out.tell() <= MAX_ENCODED_BYTES:
            return PreparedImage(out.getvalue(), "image/png", size, original, size[0] / original[0])
    out = io.BytesIO()
    image.convert("RGB").save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return PreparedImage(out.getvalue(), "image/jpeg", size, original, size[0] / original[0])


def pixel_to_page(served: tuple[int, int],
                  region_pt: tuple[float, float, float, float] | None) -> dict[str, float] | None:
    """Affine transform of the served image onto PAGE_PT_TL; ``region_pt`` is the page box (0, 0, w, h) for a page
    render or the object's bbox for a crop."""
    if region_pt is None or served[0] <= 0 or served[1] <= 0:
        return None
    x0, y0, x1, y1 = region_pt
    if x1 <= x0 or y1 <= y0:
        return None
    return {"x0": round(x0, 4), "y0": round(y0, 4), "sx": round((x1 - x0) / served[0], 6),
            "sy": round((y1 - y0) / served[1], 6)}

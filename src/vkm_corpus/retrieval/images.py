"""Image intake of the visual reranker: strict decoding, validation and normalisation (decision H-45).

One function, :func:`normalize_image`, is used by the gateway, the parity harness and the acceptance fixtures, so the
pixels that the transformers reference and llama.cpp see are identical. Steps:

1. strict base64 → bytes (≤ ``MAX_IMAGE_BYTES``), format by magic bytes (PNG, JPEG, WebP only);
2. decompression-bomb guard on the declared size (≤ ``MAX_IMAGE_SOURCE_PIXELS``) before decoding;
3. EXIF orientation, first frame, RGB (transparency composited on white, like ``transformers.convert_to_rgb``);
4. target size: the long side capped at ``max_side`` (no upscaling), then Qwen2-VL ``smart_resize`` (multiples of 28,
   ``M0_MIN_PIXELS`` ≤ w·h ≤ ``M0_MAX_PIXELS``) — one bicubic resampling from the source to the final size;
5. PNG encoding and three hashes: source bytes, PNG bytes, raw RGB pixels.

Because the result already lies on the 28-pixel grid inside the pixel bounds, neither the HF processor nor
llama.cpp resamples it again. Pillow is imported lazily.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import math
from dataclasses import dataclass

from vkm_corpus.retrieval.models import (
    IMAGE_MAX_SIDE_PX,
    M0_MAX_PIXELS,
    M0_MIN_PIXELS,
    M0_PATCH_FACTOR,
    MAX_IMAGE_BYTES,
    MAX_IMAGE_SOURCE_PIXELS,
    RerankLimitError,
)

_MAX_ASPECT = 200.0


class ImageDecodeError(ValueError):
    """The payload is not a decodable PNG/JPEG/WebP image (→ HTTP 422 ``RERANK_IMAGE_DECODE_FAILED``)."""


@dataclass(frozen=True)
class NormalizedImage:
    png: bytes
    sha256: str                 # of ``png``
    pixel_sha256: str           # of the RGB pixel buffer with a size header
    width: int
    height: int
    source_sha256: str
    source_media_type: str
    source_width: int
    source_height: int
    resized: bool

    @property
    def image_tokens(self) -> int:
        """Visual tokens of Qwen2-VL after the 2×2 merge."""
        return (self.width // M0_PATCH_FACTOR) * (self.height // M0_PATCH_FACTOR)

    def png_base64(self) -> str:
        return base64.b64encode(self.png).decode("ascii")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def decode_base64(payload: str, *, max_bytes: int = MAX_IMAGE_BYTES) -> bytes:
    """Strict standard base64 (no ``data:`` prefix, no line breaks)."""
    if payload.startswith("data:"):
        raise ImageDecodeError("data: URIs are not accepted; send plain base64")
    # base64 inflates by 4/3: refuse early without decoding a huge string
    if len(payload) > (max_bytes * 4) // 3 + 8:
        raise RerankLimitError("image bytes", max_bytes, (len(payload) * 3) // 4)
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImageDecodeError(f"invalid base64: {exc}") from exc
    if len(data) > max_bytes:
        raise RerankLimitError("image bytes", max_bytes, len(data))
    if not data:
        raise ImageDecodeError("empty image")
    return data


def sniff_media_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def smart_resize(height: int, width: int, factor: int = M0_PATCH_FACTOR, min_pixels: int = M0_MIN_PIXELS,
                 max_pixels: int = M0_MAX_PIXELS) -> tuple[int, int]:
    """Qwen2-VL ``smart_resize`` (transformers ``image_processing_qwen2_vl``): returns ``(height, width)``."""
    if height < 1 or width < 1:
        raise ImageDecodeError("image has an empty side")
    if max(height, width) / min(height, width) > _MAX_ASPECT:
        raise ImageDecodeError(f"aspect ratio above {_MAX_ASPECT:.0f}")
    h_bar = max(factor, round(height / factor) * factor)
    w_bar = max(factor, round(width / factor) * factor)
    if h_bar * w_bar > max_pixels:
        beta = math.sqrt((height * width) / max_pixels)
        h_bar = max(factor, math.floor(height / beta / factor) * factor)
        w_bar = max(factor, math.floor(width / beta / factor) * factor)
    elif h_bar * w_bar < min_pixels:
        beta = math.sqrt(min_pixels / (height * width))
        h_bar = math.ceil(height * beta / factor) * factor
        w_bar = math.ceil(width * beta / factor) * factor
    return h_bar, w_bar


def target_size(width: int, height: int, max_side: int = IMAGE_MAX_SIDE_PX) -> tuple[int, int]:
    """Final ``(width, height)``: cap the long side (no upscaling), then ``smart_resize``."""
    scale = min(1.0, max_side / max(width, height)) if max_side > 0 else 1.0
    capped_w = max(1, round(width * scale))
    capped_h = max(1, round(height * scale))
    h, w = smart_resize(capped_h, capped_w)
    return w, h


def _to_rgb(img):
    from PIL import Image

    if img.mode == "RGB":
        return img
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(background, rgba).convert("RGB")
    return img.convert("RGB")


def normalize_image(data: bytes, *, max_side: int = IMAGE_MAX_SIDE_PX,
                    max_source_pixels: int = MAX_IMAGE_SOURCE_PIXELS) -> NormalizedImage:
    """Decode, validate and normalise one image (see the module docstring)."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    media_type = sniff_media_type(data)
    if media_type is None:
        raise ImageDecodeError("unsupported image format (PNG, JPEG or WebP expected)")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            src_w, src_h = probe.size
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageDecodeError(f"cannot read image header: {exc}") from exc
    if src_w * src_h > max_source_pixels:
        raise RerankLimitError("image source pixels", max_source_pixels, src_w * src_h)
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.seek(0)
            img.load()
            img = ImageOps.exif_transpose(img)
            rgb = _to_rgb(img)
            rgb.load()
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        raise ImageDecodeError(f"cannot decode image: {exc}") from exc
    width, height = rgb.size
    new_w, new_h = target_size(width, height, max_side=max_side)
    resized = (new_w, new_h) != (width, height)
    if resized:
        rgb = rgb.resize((new_w, new_h), resample=Image.Resampling.BICUBIC)
    out = io.BytesIO()
    rgb.save(out, format="PNG", optimize=False, compress_level=6)
    png = out.getvalue()
    pixels = rgb.tobytes()
    pixel_sha = hashlib.sha256(f"RGB8:{new_w}x{new_h}:".encode("ascii") + pixels).hexdigest()
    return NormalizedImage(png=png, sha256=sha256_hex(png), pixel_sha256=pixel_sha, width=new_w, height=new_h,
                           source_sha256=sha256_hex(data), source_media_type=media_type, source_width=src_w,
                           source_height=src_h, resized=resized)

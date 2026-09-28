"""Image intake of the visual reranker (agent F; H-45): strict base64, formats, limits, normalisation grid."""
from __future__ import annotations

import base64
import io

import pytest

from vkm_corpus.retrieval import images as I
from vkm_corpus.retrieval.models import M0_MAX_PIXELS, M0_MIN_PIXELS, RerankLimitError


def test_smart_resize_matches_qwen2vl():
    # A4 landscape capped at 1024 px: 1024×724 → 896×644 (736 visual tokens), the example of project F §2.1
    assert I.smart_resize(724, 1024) == (644, 896)
    assert I.smart_resize(28, 28) == (56, 56)                 # upscaled to min_pixels
    h, w = I.smart_resize(3000, 2000)
    assert h % 28 == 0 and w % 28 == 0 and h * w <= M0_MAX_PIXELS
    with pytest.raises(I.ImageDecodeError):
        I.smart_resize(10, 5000)                              # aspect ratio > 200


@pytest.mark.parametrize("w,h", [(1400, 1000), (1024, 724), (300, 200), (5000, 3000), (29, 1000)])
def test_target_size_is_on_grid(w, h):
    tw, th = I.target_size(w, h)
    assert tw % 28 == 0 and th % 28 == 0 and M0_MIN_PIXELS <= tw * th <= M0_MAX_PIXELS


def test_target_size_caps_long_side_without_upscaling():
    assert I.target_size(1400, 1000) == (896, 644)
    assert I.target_size(560, 280) == (560, 280)              # small grid-aligned image stays as is


def test_decode_base64_is_strict():
    raw = b"\x89PNG\r\n\x1a\n" + b"x" * 10
    assert I.decode_base64(base64.b64encode(raw).decode()) == raw
    with pytest.raises(I.ImageDecodeError):
        I.decode_base64("data:image/png;base64," + base64.b64encode(raw).decode())
    with pytest.raises(I.ImageDecodeError):
        I.decode_base64("@@not base64@@")
    with pytest.raises(RerankLimitError):
        I.decode_base64(base64.b64encode(b"\x00" * 2048).decode(), max_bytes=1024)


def test_sniff_media_type():
    assert I.sniff_media_type(b"\x89PNG\r\n\x1a\n....") == "image/png"
    assert I.sniff_media_type(b"\xff\xd8\xff\xe0....") == "image/jpeg"
    assert I.sniff_media_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert I.sniff_media_type(b"GIF89a") is None


def _png(size=(300, 200), mode="RGB", color=(10, 20, 30)) -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new(mode, size, color).save(out, format="PNG")
    return out.getvalue()


def test_normalize_image_grid_hashes_and_idempotence():
    pytest.importorskip("PIL")
    n = I.normalize_image(_png((1400, 1000)))
    assert (n.width, n.height) == (896, 644) and n.image_tokens == 32 * 23 and n.resized
    assert n.source_media_type == "image/png" and (n.source_width, n.source_height) == (1400, 1000)
    again = I.normalize_image(n.png)                 # already on the grid: no second resampling
    assert (again.width, again.height) == (896, 644) and not again.resized and again.pixel_sha256 == n.pixel_sha256


def test_normalize_image_transparency_on_white():
    pytest.importorskip("PIL")
    from PIL import Image

    n = I.normalize_image(_png((280, 280), mode="RGBA", color=(0, 0, 0, 0)))
    img = Image.open(io.BytesIO(n.png))
    assert img.mode == "RGB" and img.getpixel((10, 10)) == (255, 255, 255)


def test_normalize_image_rejects_garbage_and_bombs():
    pytest.importorskip("PIL")
    with pytest.raises(I.ImageDecodeError):
        I.normalize_image(b"plain text")
    with pytest.raises(I.ImageDecodeError):
        I.normalize_image(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)       # PNG magic, broken body
    with pytest.raises(RerankLimitError):
        I.normalize_image(_png((1000, 1000)), max_source_pixels=500_000)


def test_normalize_image_exif_orientation():
    pytest.importorskip("PIL")
    from PIL import Image

    img = Image.new("RGB", (560, 280), (200, 0, 0))
    exif = Image.Exif()
    exif[0x0112] = 6                                   # rotate 90° on display
    out = io.BytesIO()
    img.save(out, format="JPEG", exif=exif.tobytes())
    n = I.normalize_image(out.getvalue())
    assert (n.width, n.height) == (280, 560) and n.source_media_type == "image/jpeg"

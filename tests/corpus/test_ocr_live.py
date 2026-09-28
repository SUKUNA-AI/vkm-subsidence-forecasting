"""Live checks (marker ``gpu``): the GLM-OCR server serves the pinned revision and recognises synthetic images;
PP-DocLayoutV3 runs offline on the GPU and is deterministic within a process. Without the service/GPU → skipped
(NOT_RUN)."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.gpu


def _server() -> str:
    url = os.environ.get("VKM_OCR_URL", "").strip()
    if not url:
        pytest.skip("VKM_OCR_URL not set (NOT_RUN)")
    httpx = pytest.importorskip("httpx")
    try:
        if httpx.get(url.rstrip("/") + "/health", timeout=5).status_code != 200:
            pytest.skip("GLM-OCR server not healthy (NOT_RUN)")
    except httpx.HTTPError:
        pytest.skip("GLM-OCR server not reachable (NOT_RUN)")
    return url


def test_server_serves_pinned_revision_and_recognises_synthetic_images():
    url = _server()
    from vkm_corpus.artifacts.render import png_bytes
    from vkm_corpus.artifacts.store import sha256_hex
    from vkm_corpus.contracts.signatures import pixel_sha256
    from vkm_corpus.ocr.cli import synthetic_images
    from vkm_corpus.ocr.client import GlmOcrClient, OcrRequest
    from vkm_corpus.ocr.prompts import DEFAULT_MODEL

    async def go():
        async with GlmOcrClient(url, concurrency=3) as c:
            info = await c.server_info()
            reqs = []
            for task, prompt, img in synthetic_images():
                png = png_bytes(img)
                reqs.append(OcrRequest(task=task, prompt=prompt, png=png, png_sha256=sha256_hex(png),
                                       pixel_sha256=pixel_sha256(img.mode, img.width, img.height, img.tobytes()),
                                       width=img.width, height=img.height, mode=img.mode))
            return info, await asyncio.gather(*(c.recognize(r) for r in reqs))

    info, res = asyncio.run(go())
    assert str(info["models"]["data"][0]["root"]).rstrip("/").endswith(DEFAULT_MODEL.model_revision)
    assert all(r.ok and r.finish_reason == "stop" and (r.content or "").strip() for r in res)
    table = next(r for r in res if r.request.task == "table")
    assert "<table" in table.content.lower()


def test_layout_model_offline_deterministic():
    models = os.environ.get("VKM_MODELS_DIR", "").strip()
    torch = pytest.importorskip("torch")
    pytest.importorskip("transformers")
    if not models or not torch.cuda.is_available():
        pytest.skip("models dir or CUDA not available (NOT_RUN)")
    pymupdf = pytest.importorskip("pymupdf")
    from vkm_corpus.artifacts.render import render_pdf_page
    from vkm_corpus.extract.synthetic import make_pdf
    from vkm_corpus.layout.ppdoclayout import LayoutModel
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        pdf = make_pdf(Path(td) / "x.pdf", pages=("text", "figure"))
        doc = pymupdf.open(pdf)
        imgs = [render_pdf_page(doc[i], 200, "RGB").image for i in range(2)]
        doc.close()
    lm = LayoutModel(Path(models))
    a = lm.detect(imgs)
    b = lm.detect(imgs)
    assert a == b
    assert any(d["score"] >= 0.3 for d in a[0]["detections"])

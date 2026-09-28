"""Thin GLM-OCR client (agent C): loopback allowlist, retries, timeouts, raw body kept, no pixels in the raw record,
and no ``glmocr`` SDK anywhere in ``src/`` (H-06)."""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

import pytest

from vkm_corpus.ocr.client import HostNotAllowed, check_url

SRC = Path(__file__).resolve().parents[2] / "src"


def test_allowlist():
    assert check_url("http://127.0.0.1:8080") == "http://127.0.0.1:8080"
    assert check_url("http://localhost:8080/") == "http://localhost:8080"
    assert check_url("http://glm-ocr:8000")
    for bad in ("http://example.com", "https://api.z.ai/v1", "http://192.0.2.10:8080", "ftp://127.0.0.1", "127.0.0.1"):
        with pytest.raises(HostNotAllowed):
            check_url(bad)


def test_no_glmocr_import_in_src():
    import re

    offenders = []
    for py in SRC.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text)
        except SyntaxError:  # a file that does not parse is another test's failure; the guard falls back to text
            if re.search(r"^\s*(import\s+glmocr\b|from\s+glmocr\b)", text, re.M):
                offenders.append(str(py.relative_to(SRC)))
            continue
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(n == "glmocr" or n.startswith("glmocr.") for n in names):
                offenders.append(str(py.relative_to(SRC)))
    assert offenders == []


def _request():
    from vkm_corpus.ocr.client import OcrRequest

    png = b"\x89PNG\r\n\x1a\nfake"
    return OcrRequest(task="text", prompt="Text Recognition:", png=png, png_sha256="a" * 64, pixel_sha256="b" * 64,
                      width=10, height=10, mode="L")


def _run(handler, **kw):
    httpx = pytest.importorskip("httpx")
    from vkm_corpus.ocr.client import GlmOcrClient

    async def go():
        async with GlmOcrClient("http://127.0.0.1:8080", transport=httpx.MockTransport(handler), max_retries=2,
                                **kw) as c:
            return await c.recognize(_request())

    return asyncio.run(go())


def _ok_body(content="синтетика", finish="stop"):
    return {"choices": [{"message": {"content": content}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3}}


def test_success_keeps_raw_body():
    httpx = pytest.importorskip("httpx")
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_ok_body())

    r = _run(handler)
    assert r.ok and r.content == "синтетика" and r.finish_reason == "stop"
    assert json.loads(r.body_text) == _ok_body()
    msg = seen["body"]["messages"][0]["content"]
    assert msg[0]["image_url"]["url"].startswith("data:image/png;base64,") and msg[1]["text"] == "Text Recognition:"
    assert seen["body"]["temperature"] == 0 and seen["body"]["top_k"] == 1


def test_retry_on_503_then_success(monkeypatch):
    httpx = pytest.importorskip("httpx")
    import vkm_corpus.ocr.client as cl

    async def fast_sleep(_):
        return None

    monkeypatch.setattr(cl.asyncio, "sleep", fast_sleep)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503, text="busy") if calls["n"] == 1 else httpx.Response(200, json=_ok_body())

    r = _run(handler)
    assert r.ok and calls["n"] == 2 and r.tries[0]["http_status"] == 503


def test_timeout_is_an_error_not_an_exception(monkeypatch):
    httpx = pytest.importorskip("httpx")
    import vkm_corpus.ocr.client as cl

    async def fast_sleep(_):
        return None

    monkeypatch.setattr(cl.asyncio, "sleep", fast_sleep)

    def handler(request):
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    r = _run(handler)
    assert not r.ok and r.status == "TIMEOUT" and len(r.tries) == 3


def test_raw_record_has_no_pixels_and_no_forbidden_keys():
    from vkm_corpus.artifacts.receipts import check_public_safe
    from vkm_corpus.ocr.client import OcrResponse
    from vkm_corpus.ocr.prompts import DEFAULT_MODEL
    from vkm_corpus.ocr.raw import build_record

    resp = OcrResponse(request=_request(), status="OK", http_status=200, body_text=json.dumps(_ok_body()),
                       content="x", finish_reason="stop", usage={}, queued_at="t", started_at="t", finished_at="t",
                       latency_ms=1)
    rec = build_record(resp, call_signature="c" * 64, attempt=1, model=DEFAULT_MODEL, backend={"engine": "vllm"},
                       run_id=None, source_id=None, page_id=None, input_ref={}, region_ref=None)
    text = json.dumps(rec)
    assert "base64" not in text and "sha256=" in rec["request"]["messages"][0]["content"][0]["image_url"]["url"]
    assert rec["model"]["weights_sha256"] == DEFAULT_MODEL.weights_sha256
    assert check_public_safe(rec) == []

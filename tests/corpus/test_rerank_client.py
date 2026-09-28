"""Client of the rerank gateway for the VKM API (agent F → G): request building, token header, errors (mocked HTTP)."""
from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

httpx = pytest.importorskip("httpx")
pytest.importorskip("pydantic")

from vkm_corpus.retrieval import models as M  # noqa: E402
from vkm_corpus.retrieval.client import (  # noqa: E402
    RerankClient,
    RerankClientError,
    SyncRerankClient,
    build_visual_request,
    encode_image,
)

SHA = hashlib.sha256(b"x").hexdigest()


def _ok_response(ids):
    return {"kind": "text", "request_id": "r", "model_id": "m", "model_revision": "v", "quant": "fp16",
            "placement": "GPU", "backend": "b", "backend_version": "bv", "model_config_sha256": SHA,
            "score_semantics": "s", "license": "CC-BY-NC-4.0", "candidate_ids": ids,
            "scores": [1.0 - i * 0.1 for i in range(len(ids))],
            "results": [{"id": c, "rank": i + 1, "score": 1.0 - i * 0.1, "input_index": i} for i, c in enumerate(ids)],
            "n_candidates": len(ids), "top_n": len(ids), "query_sha256": SHA, "input_sha256": SHA,
            "latency_ms": {"total": 1, "preprocess": 0, "queue": 0, "backend": 1}, "gateway_version": "0.1.0",
            "created_at": "2026-09-28T00:00:00Z"}


def test_sync_client_sends_token_and_parses():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["token"] = request.headers.get(M.HEADER_TOKEN)
        seen["path"] = request.url.path
        body = json.loads(request.content)
        seen["body"] = body
        return httpx.Response(200, json=_ok_response([c["id"] for c in body["candidates"]][::-1]))

    with SyncRerankClient("http://gateway.test:18084", "tok", transport=httpx.MockTransport(handler)) as c:
        resp = c.rerank_text("оседание", [("a", "текст а"), {"id": "b", "text": "текст б"}], top_n=2,
                             request_id="req-1")
    assert seen["token"] == "tok" and seen["path"] == "/v1/rerank/text"
    assert seen["body"] == {"query": "оседание", "candidates": [{"id": "a", "text": "текст а"},
                                                                {"id": "b", "text": "текст б"}],
                            "top_n": 2, "request_id": "req-1"}
    assert resp.candidate_ids == ["b", "a"]


def test_client_checks_limits_before_sending():
    calls = []
    transport = httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(500))
    with SyncRerankClient("http://gateway.test", "t", transport=transport) as c:
        with pytest.raises(M.RerankLimitError):
            c.rerank_text("q", [(f"c{i}", "t") for i in range(25)])
        with pytest.raises(M.RerankLimitError):
            c.rerank_visual("q", [(f"i{i}", b"\x89PNG....") for i in range(9)])
    assert calls == []


def test_client_maps_contract_errors():
    err = {"contract": "vkm.rerank/1", "error": {"code": M.E_BACKEND_BUSY, "message": "queue full", "retryable": True}}
    transport = httpx.MockTransport(lambda r: httpx.Response(503, json=err))
    with SyncRerankClient("http://gateway.test", "t", transport=transport) as c:
        with pytest.raises(RerankClientError) as exc:
            c.rerank_text("q", [("a", "t")])
    assert exc.value.status == 503 and exc.value.error.code == M.E_BACKEND_BUSY and exc.value.retryable


def test_async_client_visual():
    def handler(request):
        body = json.loads(request.content)
        assert body["candidates"][0]["image_base64"] == encode_image(b"\x89PNGdata")
        resp = _ok_response(["img-1"])
        resp["kind"] = "visual"
        return httpx.Response(200, json=resp)

    async def go():
        async with RerankClient("http://gateway.test", "t", transport=httpx.MockTransport(handler)) as c:
            return await c.rerank_visual("разрез", [("img-1", b"\x89PNGdata")])

    assert asyncio.run(go()).kind == "visual"


def test_build_visual_request_accepts_models_and_dicts():
    req = build_visual_request("q", [M.VisualCandidate(id="a", image_base64="AAAA"),
                                     {"id": "b", "image_base64": "BBBB"}])
    assert [c.id for c in req.candidates] == ["a", "b"]


def test_from_settings_needs_url(monkeypatch):
    from vkm_corpus.config import ConfigError

    monkeypatch.delenv("VKM_RERANK_URL", raising=False)
    with pytest.raises(ConfigError):
        SyncRerankClient.from_settings()

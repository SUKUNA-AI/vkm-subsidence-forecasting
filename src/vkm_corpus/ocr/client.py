"""Thin asynchronous client of the GLM-OCR server (OpenAI-compatible ``/v1/chat/completions``, vLLM).

* Only loopback hosts or the compose service name are allowed (``ALLOWED_HOSTS``): the client refuses any other URL
  before a socket is opened (H-06).
* Images are sent as lossless PNG data URLs; one image and one prompt per request.
* Concurrency is bounded by a semaphore; 429/5xx/connection errors are retried with backoff; the complete HTTP
  response body is returned as text, unchanged, together with timings and the list of failed tries.
* The request that is kept for provenance has the image replaced by its hashes (no pixels in the raw record).
"""
from __future__ import annotations

import asyncio
import base64
import json
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from vkm_corpus.ocr.prompts import DEFAULT_SAMPLING, SERVED_MODEL_NAME, Sampling

ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "glm-ocr"})
CLIENT_ID = "vkm-glm-ocr-client"
CLIENT_VERSION = "0.1.0"
RETRY_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


class HostNotAllowed(ValueError):
    """The OCR server URL points outside the loopback allowlist."""


def check_url(base_url: str) -> str:
    u = urlparse(base_url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise HostNotAllowed(f"invalid OCR server URL: {base_url!r}")
    if u.hostname.lower() not in ALLOWED_HOSTS:
        raise HostNotAllowed(f"OCR server host {u.hostname!r} is not in the allowlist {sorted(ALLOWED_HOSTS)}")
    return base_url.rstrip("/")


@dataclass
class OcrRequest:
    task: str
    prompt: str
    png: bytes
    png_sha256: str
    pixel_sha256: str
    width: int
    height: int
    mode: str
    sampling: Sampling = DEFAULT_SAMPLING
    tag: Any = None  # caller's key (returned unchanged)


@dataclass
class OcrResponse:
    request: OcrRequest
    status: str                     # OK | HTTP_ERROR | TIMEOUT | CONNECT_ERROR | PROTOCOL_ERROR
    http_status: int | None
    body_text: str | None           # complete HTTP body as received
    content: str | None
    finish_reason: str | None
    usage: dict[str, Any] | None
    queued_at: str
    started_at: str | None
    finished_at: str
    latency_ms: int
    tries: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "OK"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def request_body(req: OcrRequest, model: str = SERVED_MODEL_NAME, *, redact_image: bool = False) -> dict[str, Any]:
    if redact_image:
        url = f"data:image/png;sha256={req.png_sha256};pixel_sha256={req.pixel_sha256};bytes={len(req.png)}"
    else:
        url = "data:image/png;base64," + base64.b64encode(req.png).decode("ascii")
    return {"model": model,
            "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}},
                                                      {"type": "text", "text": req.prompt}]}],
            **req.sampling.as_dict()}


class GlmOcrClient:
    """``async with GlmOcrClient(url) as c: resp = await c.recognize(req)``."""

    def __init__(self, base_url: str, *, model: str = SERVED_MODEL_NAME, concurrency: int = 32,
                 timeout_s: float = 600.0, max_retries: int = 3, transport: Any = None):
        self.base_url = check_url(base_url)
        self.model = model
        self.concurrency = max(1, int(concurrency))
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self._transport = transport
        self._sem = asyncio.Semaphore(self.concurrency)
        self._client = None

    async def __aenter__(self) -> "GlmOcrClient":
        import httpx

        limits = httpx.Limits(max_connections=self.concurrency + 4, max_keepalive_connections=self.concurrency + 4)
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=httpx.Timeout(self.timeout_s, connect=10.0),
                                         limits=limits, transport=self._transport, trust_env=False,
                                         follow_redirects=False)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ server identity
    async def server_info(self) -> dict[str, Any]:
        """``/health``, ``/version`` and ``/v1/models`` as returned (for the receipt and the raw records)."""
        info: dict[str, Any] = {"base_url_host": urlparse(self.base_url).hostname}
        for key, path in (("health_status", "/health"), ("version", "/version"), ("models", "/v1/models")):
            try:
                r = await self._client.get(path, timeout=10.0)
                if key == "health_status":
                    info[key] = r.status_code
                else:
                    info[key] = r.json() if r.status_code == 200 else {"http_status": r.status_code}
            except Exception as exc:  # noqa: BLE001 - reported, the caller decides
                info[key] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
        return info

    # ------------------------------------------------------------------ recognition
    async def recognize(self, req: OcrRequest) -> OcrResponse:
        import httpx

        queued = _now()
        t_queue = time.perf_counter()
        async with self._sem:
            started = _now()
            t0 = time.perf_counter()
            body = request_body(req, self.model)
            tries: list[dict[str, Any]] = []
            last_error = None
            for attempt in range(1, self.max_retries + 2):
                try:
                    r = await self._client.post("/v1/chat/completions", json=body)
                except httpx.TimeoutException as exc:
                    last_error = ("TIMEOUT", None, f"{type(exc).__name__}: {exc}"[:300])
                except httpx.TransportError as exc:
                    last_error = ("CONNECT_ERROR", None, f"{type(exc).__name__}: {exc}"[:300])
                else:
                    text = r.text
                    if r.status_code == 200:
                        return self._parse(req, text, r.status_code, queued, started, t0, tries)
                    last_error = ("HTTP_ERROR", r.status_code, text[:2000])
                    if r.status_code not in RETRY_STATUS:
                        break
                tries.append({"try": attempt, "status": last_error[0], "http_status": last_error[1],
                              "error": last_error[2][:300], "at": _now()})
                if attempt <= self.max_retries:
                    await asyncio.sleep(min(30.0, 1.5 ** attempt + random.random()))
            status, http_status, message = last_error or ("PROTOCOL_ERROR", None, "no response")
            return OcrResponse(request=req, status=status, http_status=http_status,
                               body_text=message if status == "HTTP_ERROR" else None, content=None,
                               finish_reason=None, usage=None, queued_at=queued, started_at=started,
                               finished_at=_now(), latency_ms=int((time.perf_counter() - t0) * 1000),
                               tries=tries, error=message)

    def _parse(self, req: OcrRequest, text: str, http_status: int, queued: str, started: str, t0: float,
               tries: list[dict[str, Any]]) -> OcrResponse:
        latency = int((time.perf_counter() - t0) * 1000)
        try:
            data = json.loads(text)
            choice = data["choices"][0]
            content = choice["message"].get("content")
            finish = choice.get("finish_reason")
            usage = data.get("usage")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            return OcrResponse(request=req, status="PROTOCOL_ERROR", http_status=http_status, body_text=text,
                               content=None, finish_reason=None, usage=None, queued_at=queued, started_at=started,
                               finished_at=_now(), latency_ms=latency, tries=tries,
                               error=f"unparseable response: {type(exc).__name__}")
        return OcrResponse(request=req, status="OK", http_status=http_status, body_text=text, content=content,
                           finish_reason=finish, usage=usage, queued_at=queued, started_at=started,
                           finished_at=_now(), latency_ms=latency, tries=tries)

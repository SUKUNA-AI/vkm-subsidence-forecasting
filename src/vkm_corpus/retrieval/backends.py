"""Backends of the rerank gateway on EDGE and per-backend admission control (CP-18, H-41).

* :class:`TextBackend` — the existing text service ``careerops-reranker`` (jina-reranker-v3.5, transformers fp16,
  loopback port 18082), used exactly as it is: ``GET /readyz`` gives the runtime identity, ``POST /v1/rerank`` takes
  ``{query, documents, top_n, token_budget, expected_runtime}`` and answers ``{runtime, usage.total_tokens,
  results: [{index, relevance_score}]}``. The service handles HTTP in threads (``ThreadingHTTPServer``) but runs every
  inference under one ``threading.Lock`` (``JinaRerankerRuntime.rerank``), so its VRAM peak does not grow with
  concurrent requests; the gateway therefore does not limit concurrency towards it (H-41: evidence in the receipt).
* :class:`VisualBackend` — pinned ``llama-server`` with jina-reranker-m0 (Q6_K + mmproj Q8_0, loopback port 18083),
  ``POST /embedding`` with multimodal prompts, last-token pooling without normalisation; the score head is applied by
  the gateway.

Errors are mapped to the contract codes of :mod:`vkm_corpus.retrieval.models`. httpx is imported lazily.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import Any

from vkm_corpus.retrieval.models import (
    E_BACKEND_BUSY,
    E_BACKEND_ERROR,
    E_BACKEND_TIMEOUT,
    E_BACKEND_UNAVAILABLE,
    E_PAYLOAD_TOO_LARGE,
    ERROR_STATUS,
    RETRYABLE,
    TEXT_TIMEOUT_S,
    VISUAL_TIMEOUT_S,
)

# llama-server replaces a per-instance random media marker (GET /props → media_marker) by the image embeddings, wrapped
# by mtmd in <|vision_start|> … <|vision_end|>; <|box_end|> is the score token (id 100 swapped in the GGUF vocab).
M0_PROMPT_TEMPLATE = "**Document**:\n{marker}\n**Query**:\n{query}<|box_end|>"


class RerankError(Exception):
    """Error with a contract code; the gateway turns it into an HTTP error response."""

    def __init__(self, code: str, message: str, *, stage: str | None = None,
                 details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code, self.message, self.stage, self.details = code, message, stage, details

    @property
    def status(self) -> int:
        return ERROR_STATUS.get(self.code, 500)

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE


class BackendGate:
    """Admission control of ONE backend: ``max_inflight`` requests in work (0 = unlimited) and at most ``max_queue``
    waiting; beyond that ``RERANK_BACKEND_BUSY``. There is no lock shared between backends."""

    def __init__(self, name: str, max_inflight: int = 0, max_queue: int = 0) -> None:
        self.name, self.max_inflight, self.max_queue = name, max_inflight, max_queue
        self._sem = asyncio.Semaphore(max_inflight) if max_inflight > 0 else None
        self.waiting = 0
        self.inflight = 0

    @contextlib.asynccontextmanager
    async def slot(self):
        """Yields the seconds spent waiting for the slot."""
        t0 = time.perf_counter()
        if self._sem is None:
            self.inflight += 1
            try:
                yield 0.0
            finally:
                self.inflight -= 1
            return
        if self._sem.locked() and self.waiting >= self.max_queue:
            raise RerankError(E_BACKEND_BUSY, f"{self.name} backend queue is full", stage=self.name,
                              details={"max_inflight": self.max_inflight, "max_queue": self.max_queue})
        self.waiting += 1
        try:
            await self._sem.acquire()
        finally:
            self.waiting -= 1
        self.inflight += 1
        try:
            yield time.perf_counter() - t0
        finally:
            self.inflight -= 1
            self._sem.release()


def _client(base_url: str, timeout_s: float):
    import httpx

    return httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(timeout_s, connect=5.0),
                             limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
                             trust_env=False)


async def _request(client, method: str, path: str, stage: str, **kwargs):
    import httpx

    try:
        return await client.request(method, path, **kwargs)
    except httpx.TimeoutException as exc:
        raise RerankError(E_BACKEND_TIMEOUT, f"{stage} backend timed out", stage=stage) from exc
    except httpx.TransportError as exc:
        raise RerankError(E_BACKEND_UNAVAILABLE, f"{stage} backend is unreachable: {type(exc).__name__}",
                          stage=stage) from exc


@dataclass
class TextBackend:
    """Client of the existing v3.5 service (never modified by VKM)."""

    base_url: str
    timeout_s: float = TEXT_TIMEOUT_S
    _identity: dict[str, str] | None = None
    _client: Any = None

    def client(self):
        if self._client is None:
            self._client = _client(self.base_url, self.timeout_s)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def identity(self, refresh: bool = False) -> dict[str, str]:
        if self._identity is None or refresh:
            resp = await _request(self.client(), "GET", "/readyz", "text", timeout=5.0)
            if resp.status_code != 200:
                raise RerankError(E_BACKEND_UNAVAILABLE, f"text backend not ready (HTTP {resp.status_code})",
                                  stage="text")
            body = resp.json()
            runtime = body.get("runtime")
            if body.get("status") != "ready" or not isinstance(runtime, dict):
                raise RerankError(E_BACKEND_UNAVAILABLE, "text backend is not ready", stage="text")
            self._identity = {str(k): str(v) for k, v in runtime.items()}
        return self._identity

    async def health(self) -> str:
        try:
            await self.identity(refresh=True)
            return "ready"
        except RerankError:
            return "unavailable"

    async def rerank(self, query: str, documents: list[str], top_n: int, token_budget: int) -> dict[str, Any]:
        """``{"results": [(index, score)], "total_tokens": int, "runtime": {...}}``; one retry on identity change."""
        for attempt in (0, 1):
            identity = await self.identity(refresh=attempt > 0)
            payload = {"query": query, "documents": documents, "top_n": top_n, "token_budget": token_budget,
                       "expected_runtime": identity}
            resp = await _request(self.client(), "POST", "/v1/rerank", "text", json=payload)
            if resp.status_code == 409 and attempt == 0:
                continue
            break
        body = _json_or_none(resp)
        if resp.status_code == 413:
            raise RerankError(E_PAYLOAD_TOO_LARGE, "text backend rejected the token budget", stage="text",
                              details={"backend_error": (body or {}).get("error"),
                                       "backend_message": (body or {}).get("message")})
        if resp.status_code == 503:
            raise RerankError(E_BACKEND_ERROR, "text backend inference failed (see service log)", stage="text",
                              details={"backend_status": 503})
        if resp.status_code != 200 or not isinstance(body, dict):
            raise RerankError(E_BACKEND_ERROR, f"text backend answered HTTP {resp.status_code}", stage="text",
                              details={"backend_status": resp.status_code, "backend_error": (body or {}).get("error")
                                       if isinstance(body, dict) else None})
        try:
            results = [(int(r["index"]), float(r["relevance_score"])) for r in body["results"]]
            total = int(body["usage"]["total_tokens"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RerankError(E_BACKEND_ERROR, "text backend returned an unexpected body", stage="text") from exc
        return {"results": results, "total_tokens": total, "runtime": body.get("runtime") or identity}


def _json_or_none(resp) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


@dataclass
class VisualBackend:
    """Client of the pinned llama-server with jina-reranker-m0."""

    base_url: str
    timeout_s: float = VISUAL_TIMEOUT_S
    _client: Any = None
    props_cache: dict[str, Any] = field(default_factory=dict)

    def client(self):
        if self._client is None:
            self._client = _client(self.base_url, self.timeout_s)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def health(self) -> str:
        try:
            resp = await _request(self.client(), "GET", "/health", "visual", timeout=5.0)
        except RerankError:
            return "unavailable"
        if resp.status_code == 200:
            return "ready"
        return "loading" if resp.status_code == 503 else "unavailable"

    async def props(self, refresh: bool = False) -> dict[str, Any]:
        if self.props_cache and not refresh:
            return self.props_cache
        resp = await _request(self.client(), "GET", "/props", "visual", timeout=5.0)
        body = _json_or_none(resp)
        if resp.status_code != 200 or not isinstance(body, dict) or not body.get("media_marker"):
            raise RerankError(E_BACKEND_UNAVAILABLE, f"visual backend props unavailable (HTTP {resp.status_code})",
                              stage="visual")
        self.props_cache = body
        return body

    async def media_marker(self) -> str:
        return str((await self.props())["media_marker"])

    async def embed_images(self, query: str, images_b64: list[str]) -> list[list[float]]:
        """Hidden states of the score token for ``(query, image)`` pairs, in input order.

        The media marker is random per llama-server instance; after a backend restart the cached marker is stale and
        the prompt tokenisation fails — then the props are re-read once."""
        for attempt in (0, 1):
            marker = (await self.props(refresh=attempt > 0))["media_marker"]
            try:
                return await self.embed([(m0_prompt(query, marker), b64) for b64 in images_b64])
            except RerankError as exc:
                stale = (exc.details or {}).get("backend_status") == 500 and "tokenize" in str(
                    (exc.details or {}).get("backend_message") or "").lower()
                if attempt == 0 and stale:
                    continue
                raise
        raise AssertionError("unreachable")

    async def embed(self, prompts: list[tuple[str, str]]) -> list[list[float]]:
        """``prompts``: ``(prompt_string, png_base64)`` pairs → last-token hidden states, in input order."""
        content = [{"prompt_string": p, "multimodal_data": [b64]} for p, b64 in prompts]
        resp = await _request(self.client(), "POST", "/embedding", "visual",
                              json={"content": content, "embd_normalize": -1})
        body = _json_or_none(resp)
        if resp.status_code == 503:
            raise RerankError(E_BACKEND_UNAVAILABLE, "visual backend is loading or busy", stage="visual")
        if resp.status_code != 200 or not isinstance(body, list):
            message = None
            if isinstance(body, dict):
                message = (body.get("error") or {}).get("message") if isinstance(body.get("error"), dict) else None
            raise RerankError(E_BACKEND_ERROR, f"visual backend answered HTTP {resp.status_code}", stage="visual",
                              details={"backend_status": resp.status_code, "backend_message": message})
        by_index: dict[int, list[float]] = {}
        for item in body:
            emb = item.get("embedding")
            if isinstance(emb, list) and emb and isinstance(emb[0], list):
                emb = emb[-1]            # pooled output is a list with one vector
            by_index[int(item.get("index", len(by_index)))] = emb
        if sorted(by_index) != list(range(len(prompts))):
            raise RerankError(E_BACKEND_ERROR, "visual backend returned an incomplete result", stage="visual")
        return [by_index[i] for i in range(len(prompts))]


def m0_prompt(query: str, marker: str) -> str:
    """Prompt of one (query, image) pair, as ``formatting_prompts_func(doc_type='image')`` + score token."""
    return M0_PROMPT_TEMPLATE.format(marker=marker, query=query)

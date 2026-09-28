"""Client of ``vkm-rerank-gateway`` for the VKM API / MCP on CORE (agent G) and for the CLI.

``RerankClient`` (async, for FastAPI) and ``SyncRerankClient`` share request building and error mapping. Limits of
the contract are checked before sending (:class:`models.RerankLimitError`); gateway errors come back as
:class:`RerankClientError` with the contract error body. The base URL and the token come from
``load_settings()`` (``VKM_RERANK_URL``, ``VKM_RERANK_TOKEN_FILE``). httpx is imported lazily.
"""
from __future__ import annotations

import base64
from typing import Any, Iterable

from vkm_corpus.retrieval import models as M


class RerankClientError(RuntimeError):
    def __init__(self, status: int, error: M.ErrorBody | None, message: str) -> None:
        super().__init__(message)
        self.status, self.error = status, error

    @property
    def retryable(self) -> bool:
        return bool(self.error and self.error.retryable) or self.status in (502, 503, 504)


def encode_image(data: bytes) -> str:
    """Raw image bytes (PNG/JPEG/WebP) → the ``image_base64`` field."""
    return base64.b64encode(data).decode("ascii")


def build_text_request(query: str, candidates: Iterable[M.TextCandidate | dict[str, Any] | tuple[str, str]],
                       top_n: int | None = None, request_id: str | None = None,
                       truncate_to_tokens: int | None = None) -> M.TextRerankRequest:
    items = [c if isinstance(c, M.TextCandidate) else
             M.TextCandidate(id=c[0], text=c[1]) if isinstance(c, tuple) else M.TextCandidate(**c) for c in candidates]
    req = M.TextRerankRequest(query=query, candidates=items, top_n=top_n, request_id=request_id,
                              truncate_to_tokens=truncate_to_tokens)
    req.check_limits()
    return req


def build_visual_request(query: str, candidates: Iterable[M.VisualCandidate | dict[str, Any] | tuple[str, bytes]],
                         top_n: int | None = None, request_id: str | None = None) -> M.VisualRerankRequest:
    items = []
    for c in candidates:
        if isinstance(c, M.VisualCandidate):
            items.append(c)
        elif isinstance(c, tuple):
            items.append(M.VisualCandidate(id=c[0], image_base64=encode_image(c[1])))
        else:
            items.append(M.VisualCandidate(**c))
    req = M.VisualRerankRequest(query=query, candidates=items, top_n=top_n, request_id=request_id)
    req.check_limits()
    return req


def _raise_for(resp) -> None:
    if resp.status_code == 200:
        return
    error = None
    try:
        error = M.ErrorResponse.model_validate(resp.json()).error
    except Exception:  # noqa: BLE001 - not a contract error body (proxy, crash)
        pass
    message = f"rerank gateway HTTP {resp.status_code}" + (f": {error.code} {error.message}" if error else "")
    raise RerankClientError(resp.status_code, error, message)


def _headers(token: str | None, request_id: str | None) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if token:
        headers[M.HEADER_TOKEN] = token
    if request_id:
        headers[M.HEADER_REQUEST_ID] = request_id
    return headers


class RerankClient:
    """Async client; one instance per process (connection pool)."""

    def __init__(self, base_url: str, token: str | None, *, timeout_text_s: float = M.CLIENT_TIMEOUT_TEXT_S,
                 timeout_visual_s: float = M.CLIENT_TIMEOUT_VISUAL_S, transport: Any = None) -> None:
        import httpx

        self.base_url, self.token = base_url.rstrip("/"), token
        self.timeout_text_s, self.timeout_visual_s = timeout_text_s, timeout_visual_s
        self._http = httpx.AsyncClient(base_url=self.base_url, transport=transport, trust_env=False,
                                       timeout=httpx.Timeout(timeout_text_s, connect=5.0))

    @classmethod
    def from_settings(cls, settings=None, **kwargs: Any) -> "RerankClient":
        from vkm_corpus.config import ConfigError, load_settings

        settings = settings or load_settings()
        if not settings.rerank_url:
            raise ConfigError("VKM_RERANK_URL is not set (base URL of vkm-rerank-gateway)")
        return cls(settings.rerank_url, settings.rerank_token, **kwargs)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "RerankClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def rerank_text(self, query: str, candidates, top_n: int | None = None, request_id: str | None = None,
                          truncate_to_tokens: int | None = None) -> M.RerankResponse:
        req = build_text_request(query, candidates, top_n, request_id, truncate_to_tokens)
        resp = await self._http.post("/v1/rerank/text", content=req.model_dump_json(exclude_none=True),
                                     headers={**_headers(self.token, request_id), "Content-Type": "application/json"},
                                     timeout=self.timeout_text_s)
        _raise_for(resp)
        return M.RerankResponse.model_validate(resp.json())

    async def rerank_visual(self, query: str, candidates, top_n: int | None = None,
                            request_id: str | None = None) -> M.RerankResponse:
        req = build_visual_request(query, candidates, top_n, request_id)
        resp = await self._http.post("/v1/rerank/visual", content=req.model_dump_json(exclude_none=True),
                                     headers={**_headers(self.token, request_id), "Content-Type": "application/json"},
                                     timeout=self.timeout_visual_s)
        _raise_for(resp)
        return M.RerankResponse.model_validate(resp.json())

    async def status(self) -> M.StatusResponse:
        resp = await self._http.get("/status", headers=_headers(self.token, None), timeout=15.0)
        _raise_for(resp)
        return M.StatusResponse.model_validate(resp.json())

    async def health(self) -> M.HealthResponse:
        resp = await self._http.get("/health", timeout=10.0)
        _raise_for(resp)
        return M.HealthResponse.model_validate(resp.json())


class SyncRerankClient:
    """Blocking twin of :class:`RerankClient` (CLI, scripts)."""

    def __init__(self, base_url: str, token: str | None, *, timeout_text_s: float = M.CLIENT_TIMEOUT_TEXT_S,
                 timeout_visual_s: float = M.CLIENT_TIMEOUT_VISUAL_S, transport: Any = None) -> None:
        import httpx

        self.base_url, self.token = base_url.rstrip("/"), token
        self.timeout_text_s, self.timeout_visual_s = timeout_text_s, timeout_visual_s
        self._http = httpx.Client(base_url=self.base_url, transport=transport, trust_env=False,
                                  timeout=httpx.Timeout(timeout_text_s, connect=5.0))

    @classmethod
    def from_settings(cls, settings=None, **kwargs: Any) -> "SyncRerankClient":
        from vkm_corpus.config import ConfigError, load_settings

        settings = settings or load_settings()
        if not settings.rerank_url:
            raise ConfigError("VKM_RERANK_URL is not set (base URL of vkm-rerank-gateway)")
        return cls(settings.rerank_url, settings.rerank_token, **kwargs)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "SyncRerankClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def rerank_text(self, query: str, candidates, top_n: int | None = None, request_id: str | None = None,
                    truncate_to_tokens: int | None = None) -> M.RerankResponse:
        req = build_text_request(query, candidates, top_n, request_id, truncate_to_tokens)
        resp = self._http.post("/v1/rerank/text", content=req.model_dump_json(exclude_none=True),
                               headers={**_headers(self.token, request_id), "Content-Type": "application/json"},
                               timeout=self.timeout_text_s)
        _raise_for(resp)
        return M.RerankResponse.model_validate(resp.json())

    def rerank_visual(self, query: str, candidates, top_n: int | None = None,
                      request_id: str | None = None) -> M.RerankResponse:
        req = build_visual_request(query, candidates, top_n, request_id)
        resp = self._http.post("/v1/rerank/visual", content=req.model_dump_json(exclude_none=True),
                               headers={**_headers(self.token, request_id), "Content-Type": "application/json"},
                               timeout=self.timeout_visual_s)
        _raise_for(resp)
        return M.RerankResponse.model_validate(resp.json())

    def status(self) -> M.StatusResponse:
        resp = self._http.get("/status", headers=_headers(self.token, None), timeout=15.0)
        _raise_for(resp)
        return M.StatusResponse.model_validate(resp.json())

    def health(self) -> M.HealthResponse:
        resp = self._http.get("/health", timeout=10.0)
        _raise_for(resp)
        return M.HealthResponse.model_validate(resp.json())

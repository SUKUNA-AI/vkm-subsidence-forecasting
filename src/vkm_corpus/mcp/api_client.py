"""HTTP client of the MCP servers towards the VKM API (httpx, async; one per server process).

Transport failures become an ``ApiResponse``-shaped error body (``DEPENDENCY_UNAVAILABLE`` / ``DEPENDENCY_TIMEOUT``),
so every tool answer has the same shape. The token is sent as ``Authorization: Bearer`` and never logged.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

VISUAL_TIMEOUT_S = 330.0     # H-13: the visual reranker may take minutes for 8 images
DEFAULT_TIMEOUT_S = 60.0


def _error_body(code: str, message: str, retryable: bool) -> dict[str, Any]:
    rid = uuid.uuid4().hex[:16]
    return {"ok": False, "meta": {"request_id": rid, "api_version": None, "warnings": []},
            "error": {"code": code, "message": message, "retryable": retryable, "log_ref": rid, "details": {}}}


@dataclass
class BinaryResult:
    ok: bool
    data: bytes = b""
    media_type: str | None = None
    meta: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


class ApiClient:
    def __init__(self, base_url: str, token: str | None, *, transport: Any = None,
                 timeout: float = DEFAULT_TIMEOUT_S) -> None:
        import httpx

        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), headers=headers, transport=transport,
                                       timeout=httpx.Timeout(timeout, connect=5.0), trust_env=False)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                       body: Any = None, timeout: float | None = None) -> Any:
        import httpx

        kwargs: dict[str, Any] = {"params": {k: v for k, v in (params or {}).items() if v is not None}}
        if body is not None:
            kwargs["json"] = body
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            return await self._http.request(method, path, **kwargs)
        except httpx.TimeoutException:
            return _error_body("DEPENDENCY_TIMEOUT", "the VKM API did not answer in time", True)
        except httpx.HTTPError as exc:
            return _error_body("DEPENDENCY_UNAVAILABLE", f"the VKM API is not reachable ({type(exc).__name__})",
                               True)

    async def call(self, method: str, path: str, *, params: dict[str, Any] | None = None, body: Any = None,
                   timeout: float | None = None) -> dict[str, Any]:
        response = await self._request(method, path, params=params, body=body, timeout=timeout)
        if isinstance(response, dict):
            return response
        try:
            data = response.json()
        except ValueError:
            return _error_body("DEPENDENCY_ERROR", f"the VKM API answered HTTP {response.status_code} without JSON",
                               response.status_code >= 500)
        if isinstance(data, dict):
            data.setdefault("ok", response.status_code < 400)
            return data
        return _error_body("DEPENDENCY_ERROR", "unexpected API answer", False)

    async def binary(self, path: str, *, params: dict[str, Any] | None = None) -> BinaryResult:
        response = await self._request("GET", path, params=params)
        if isinstance(response, dict):
            return BinaryResult(ok=False, error=response["error"])
        if response.status_code != 200:
            try:
                return BinaryResult(ok=False, error=response.json().get("error"))
            except ValueError:
                return BinaryResult(ok=False, error={"code": "DEPENDENCY_ERROR", "message":
                                                     f"HTTP {response.status_code}", "retryable": True})
        meta = response.headers.get("X-VKM-Image-Meta")
        return BinaryResult(ok=True, data=response.content, media_type=response.headers.get("content-type"),
                            meta=json.loads(meta) if meta else None)

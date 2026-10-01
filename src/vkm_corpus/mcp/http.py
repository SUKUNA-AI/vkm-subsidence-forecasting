"""Streamable HTTP hosting of the MCP servers on CORE (stateless, JSON responses; CP-19, H-26).

* A client token (``VKM_MCP_TOKEN[_FILE]`` for ``vkm-corpus``, ``VKM_MCP_ADMIN_TOKEN[_FILE]`` for
  ``vkm-corpus-admin``) is required on every request except ``GET /healthz``; comparison is constant time.
* DNS-rebinding protection of the SDK: only ``Host`` values in ``VKM_MCP_ALLOWED_HOSTS`` (comma list, ``host:port`` or
  ``host:*``) are served; others get 421.
* The read server talks to the API with the API *read* token only (``VKM_API_TOKEN``); it never falls back to the
  write token. The admin server uses ``VKM_API_WRITE_TOKEN``.
* Every response carries ``Connection: close``. uvicorn drops an idle keep-alive connection after 5 s, and a client
  behind a local connection proxy (the Windows workstation, 29.09) never sees that close: its next request on the pooled
  connection hangs until the client times out. A new connection per request costs about a millisecond on the LAN.
"""
from __future__ import annotations

import contextlib
import asyncio
import hmac
import json
import os
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

from vkm_corpus.config import ConfigError, load_settings

DEFAULT_ALLOWED_HOSTS = ["127.0.0.1:*", "localhost:*"]
HEALTH_PATH = "/healthz"
REJECTED_BODY_LIMIT = 64 * 1024
REJECTED_BODY_TIMEOUT = 0.25


def _secret(env: Mapping[str, str], name: str) -> str | None:
    """``NAME_FILE`` (file content) wins over ``NAME``; unexpanded ``${…}`` literals count as unset."""
    from vkm_corpus import config as cfg

    value = cfg._secret(env, name)  # noqa: SLF001 - the one secret reader of the platform
    return None if value is None or value.startswith("${") else value


@dataclass
class McpHttpConfig:
    kind: Literal["read", "admin"]
    api_url: str
    api_token: str
    client_tokens: dict[str, str] = field(default_factory=dict)
    allowed_hosts: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOWED_HOSTS))
    path: str = "/mcp"

    @classmethod
    def from_env(cls, kind: Literal["read", "admin"], env: Mapping[str, str] | None = None) -> "McpHttpConfig":
        env = os.environ if env is None else env
        settings = load_settings(env)
        if not settings.api_url:
            raise ConfigError("VKM_API_URL is not set (base URL of the VKM API as seen from the MCP server)")
        if kind == "read":
            api_token = settings.api_token
            if not api_token:
                raise ConfigError("the read MCP server needs the API read token (VKM_API_TOKEN_FILE); it never uses "
                                  "the write token")
            client = _secret(env, "VKM_MCP_TOKEN")
        else:
            api_token = settings.api_write_token
            if not api_token:
                raise ConfigError("the admin MCP server needs VKM_API_WRITE_TOKEN_FILE")
            client = _secret(env, "VKM_MCP_ADMIN_TOKEN")
        if not client:
            raise ConfigError(f"no client token for the {kind} MCP server "
                              f"({'VKM_MCP_TOKEN' if kind == 'read' else 'VKM_MCP_ADMIN_TOKEN'}[_FILE])")
        hosts = [h.strip() for h in env.get("VKM_MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
        return cls(kind=kind, api_url=settings.api_url, api_token=api_token,
                   client_tokens={client: f"mcp-{kind}"}, allowed_hosts=hosts or list(DEFAULT_ALLOWED_HOSTS))


class BearerMiddleware:
    """Pure ASGI middleware (keeps streaming intact): 401 without a known bearer token; ``/healthz`` is open; every
    response gets ``Connection: close`` (module docstring)."""

    def __init__(self, app: Any, tokens: dict[str, str]) -> None:
        self.app = app
        self.tokens = {t.encode("utf-8"): label for t, label in tokens.items()}

    def _label(self, header: bytes) -> str | None:
        if not header.lower().startswith(b"bearer "):
            return None
        token = header[7:].strip()
        found = None
        for known, label in self.tokens.items():
            if hmac.compare_digest(token, known):
                found = label
        return found

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        body = _RequestBody(scope, receive)
        receive = body.receive
        send = _closing(send, body)
        if scope.get("path") == HEALTH_PATH and scope.get("method") == "GET":
            body = json.dumps({"status": "ok"}).encode()
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})
            return
        header = dict(scope.get("headers") or []).get(b"authorization", b"")
        if self._label(header) is None:
            body = json.dumps({"error": {"code": "UNAUTHORIZED", "message": "missing or invalid bearer token"}})
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")]})
            await send({"type": "http.response.body", "body": body.encode()})
            return
        await self.app(scope, receive, send)


class _RequestBody:
    """Discard a small unread request before closing its socket, without parsing.

    Windows can abort a connection closed with unread POST bytes before the
    client receives the 401/421 response. Reading has a byte/deadline bound and
    never dispatches to the application or initiates a 100-continue upload.
    """
    def __init__(self, scope, receive):
        self._receive = receive
        headers = dict(scope.get("headers") or [])
        self.done = (scope.get("method") in {"GET", "HEAD", "OPTIONS"}
                     and not headers.get(b"content-length") and not headers.get(b"transfer-encoding"))
        self.expect_continue = headers.get(b"expect", b"").lower() == b"100-continue"
        self.bytes_seen = 0

    async def receive(self):
        message = await self._receive()
        if message["type"] == "http.request":
            self.bytes_seen += len(message.get("body", b""))
            self.done = not message.get("more_body", False)
        elif message["type"] == "http.disconnect":
            self.done = True
        return message

    async def discard(self):
        if self.done or self.expect_continue or self.bytes_seen >= REJECTED_BODY_LIMIT:
            return

        async def bounded():
            while not self.done and self.bytes_seen < REJECTED_BODY_LIMIT:
                await self.receive()
        try:
            await asyncio.wait_for(bounded(), timeout=REJECTED_BODY_TIMEOUT)
        except (TimeoutError, OSError):
            pass  # oversized/slow/disconnected uploads do not delay rejection


def _closing(send: Any, body: _RequestBody | None = None) -> Any:
    """``send`` that marks every response ``Connection: close``, replacing a ``Connection`` header set upstream."""

    async def wrapped(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            headers = [(k, v) for k, v in message.get("headers") or [] if k.lower() != b"connection"]
            message = {**message, "headers": [*headers, (b"connection", b"close")]}
        elif message["type"] == "http.response.body" and not message.get("more_body", False) and body:
            await body.discard()
        await send(message)

    return wrapped


def build_http_app(server: Any, config: McpHttpConfig) -> Any:
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.applications import Starlette
    from starlette.routing import Mount

    security = TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=config.allowed_hosts,
                                         allowed_origins=[])
    inner = server.streamable_http_app(streamable_http_path=config.path, stateless_http=True, json_response=True,
                                       transport_security=security, host="0.0.0.0")

    @contextlib.asynccontextmanager
    async def lifespan(_app: Any):
        async with server.session_manager.run():
            yield

    app = Starlette(routes=[Mount("/", app=inner)], lifespan=lifespan)
    return BearerMiddleware(app, config.client_tokens)


def build(kind: Literal["read", "admin"], config: McpHttpConfig | None = None, *, transport: Any = None) -> Any:
    """ASGI app of one MCP server (read or admin) talking to the API at ``config.api_url``."""
    from vkm_corpus.mcp.api_client import ApiClient
    from vkm_corpus.mcp.servers import build_admin_server, build_read_server

    config = config or McpHttpConfig.from_env(kind)
    api = ApiClient(config.api_url, config.api_token, transport=transport)
    server = build_read_server(api) if kind == "read" else build_admin_server(api)
    return build_http_app(server, config)


def serve(kind: Literal["read", "admin"], host: str, port: int) -> None:
    import uvicorn

    from vkm_corpus.logs import configure

    configure(f"vkm-mcp{'' if kind == 'read' else '-admin'}", log_dir=None)
    uvicorn.run(build(kind), host=host, port=port, log_config=None, access_log=False, proxy_headers=False)

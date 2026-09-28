"""Client of a pinned ``llama-server`` (llama.cpp, Vulkan/RADV on the RX580) serving one encoder.

Token ids in, vectors out: the service tokenizes (``vkm_corpus.embeddings.tokenize``) and sends ``content`` as lists
of token ids to ``POST /embedding``; llama-server does not add special tokens to id lists. With a pooled GGUF
(``--pooling cls|mean|last``) the answer is one vector per sequence (normalisation is requested off, ``embd_normalize
= -1``: the post-processing normalises); with ``--pooling none`` it is the token matrix. Standard library only
(``http.client``), one keep-alive connection per thread, so the client works in the stdlib-only harness on CORE and
in the service (which calls it from worker threads). No lock is shared between servers: two resident models are two
independent processes and two independent clients.
"""
from __future__ import annotations

import base64
import http.client
import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Sequence
from urllib.parse import urlsplit

import numpy as np


class LlamaError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status, self.retryable = status, retryable


@dataclass(frozen=True)
class EmbedResult:
    vectors: list[np.ndarray]      # per sequence: [d] (pooled) or [n_tokens, d] (pooling none)
    seconds: float
    n_tokens: int


class LlamaServerClient:
    def __init__(self, url: str, *, timeout_s: float = 120.0, pooled: bool = True, b64: bool = True) -> None:
        parts = urlsplit(url)
        if parts.scheme != "http" or not parts.hostname:
            raise ValueError("llama-server URL must be http://host:port")
        self.host, self.port = parts.hostname, parts.port or 80
        self.timeout_s = timeout_s
        self.pooled = pooled
        self.b64 = b64          # ask for base64 float32 rows (VKM patch 0003); plain JSON answers are still accepted
        self._local = threading.local()

    # ----------------------------------------------------------------------------------------------------- transport
    def _conn(self) -> http.client.HTTPConnection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout_s)
            self._local.conn = conn
        return conn

    def _request(self, method: str, path: str, body: Any | None = None) -> tuple[int, bytes]:
        payload = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        for attempt in (0, 1):
            conn = self._conn()
            try:
                conn.request(method, path, body=payload, headers=headers)
                resp = conn.getresponse()
                data = resp.read()
                return resp.status, data
            except (ConnectionError, http.client.HTTPException, OSError) as exc:
                conn.close()
                self._local.conn = None
                if attempt == 1:
                    raise LlamaError(f"llama-server {self.host}:{self.port} unreachable: {exc}", retryable=True) from exc
        raise AssertionError("unreachable")

    # ------------------------------------------------------------------------------------------------------- queries
    def health(self) -> dict[str, Any]:
        status, data = self._request("GET", "/health")
        try:
            body = json.loads(data or b"{}")
        except ValueError:
            body = {}
        return {"http_status": status, **(body if isinstance(body, dict) else {})}

    def props(self) -> dict[str, Any]:
        status, data = self._request("GET", "/props")
        if status != 200:
            raise LlamaError(f"/props → HTTP {status}", status=status)
        return json.loads(data)

    def embed_ids(self, sequences: Sequence[Sequence[int]]) -> EmbedResult:
        if not sequences:
            return EmbedResult([], 0.0, 0)
        body: dict[str, Any] = {"content": [list(map(int, s)) for s in sequences], "embd_normalize": -1}
        if self.b64 and not self.pooled:
            body["encoding_format"] = "base64"
        t0 = time.perf_counter()
        status, data = self._request("POST", "/embedding", body)
        if status != 200:
            msg = data[:300].decode("utf-8", "replace")
            raise LlamaError(f"/embedding → HTTP {status}: {msg}", status=status, retryable=status in (429, 503))
        items = json.loads(data)
        dt = time.perf_counter() - t0
        items = sorted(items, key=lambda it: it.get("index", 0))
        if len(items) != len(sequences):
            raise LlamaError(f"/embedding returned {len(items)} results for {len(sequences)} sequences")
        vectors: list[np.ndarray] = []
        for it, seq in zip(items, sequences):
            if "embedding_b64" in it:
                emb = np.frombuffer(base64.b64decode(it["embedding_b64"]), dtype="<f4").reshape(
                    int(it["n_rows"]), int(it["n_cols"]))
            else:
                emb = np.asarray(it["embedding"], dtype=np.float32)
            if self.pooled:
                vectors.append(emb.reshape(-1))
            else:
                if emb.ndim != 2 or emb.shape[0] != len(seq):
                    raise LlamaError(f"token output shape {emb.shape} does not match {len(seq)} input ids")
                vectors.append(emb)
        return EmbedResult(vectors, dt, int(sum(len(s) for s in sequences)))

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

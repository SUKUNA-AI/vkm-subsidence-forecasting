"""EDGE reranker stage of the lab pipelines (G, H, I): agent F's gateway client, never a model of its own.

Limits of the contract (H-13, ``vkm_corpus.retrieval.models``): at most 24 text candidates per call, no splicing of
several calls (listwise scores of different calls are not comparable), the whole listwise prompt ≤ 4096 tokens — so
each candidate is cut to ``truncate_to_tokens`` (default 150 at 24 candidates). Without ``VKM_RERANK_URL`` or with the
gateway down the stage is ``NOT_RUN`` (never scored as zero).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

MAX_CANDIDATES = 24
DEFAULT_TRUNCATE_TOKENS = 150
NOT_RUN = "NOT_RUN"


class RerankUnavailable(RuntimeError):
    """The reranker stage cannot run (no configuration or gateway unreachable)."""


@dataclass
class EdgeReranker:
    client: Any
    truncate_to_tokens: int = DEFAULT_TRUNCATE_TOKENS
    model: dict[str, Any] | None = None
    last_truncated: int = 0

    @classmethod
    def from_settings(cls, settings: Any = None, **kw: Any) -> "EdgeReranker":
        from vkm_corpus.config import load_settings
        from vkm_corpus.retrieval.client import SyncRerankClient

        settings = settings or load_settings()
        if not settings.rerank_url:
            raise RerankUnavailable("VKM_RERANK_URL is not set")
        client = SyncRerankClient(settings.rerank_url, settings.rerank_token)
        try:
            client.health()
        except Exception as exc:  # noqa: BLE001 - any transport/HTTP failure means the stage is not available
            client.close()
            raise RerankUnavailable(f"rerank gateway not reachable: {type(exc).__name__}") from exc
        return cls(client, **kw)

    def rerank(self, query: str, candidates: Sequence[tuple[str, str]], top_n: int | None = None,
               request_id: str | None = None) -> list[tuple[str, float]]:
        """``candidates``: (unit_id, text) in current rank order, at most 24; returns (unit_id, score) best first."""
        cands = [(cid, text) for cid, text in candidates if text and text.strip()][:MAX_CANDIDATES]
        if not cands:
            return []
        resp = self.client.rerank_text(query, cands, top_n=top_n, request_id=request_id,
                                       truncate_to_tokens=self.truncate_to_tokens)
        self.model = {k: getattr(resp, k, None) for k in ("model_id", "model_revision", "quant", "placement",
                                                           "backend", "backend_version", "model_config_sha256",
                                                           "score_semantics", "license")}
        self.last_truncated = sum(1 for r in resp.results if r.truncated)
        ranked = sorted(resp.results, key=lambda r: r.rank)
        return [(r.id, float(r.score)) for r in ranked]

    def close(self) -> None:
        self.client.close()

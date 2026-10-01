"""Query encoders of the service: text → token ids (pinned tokenizer) → resident llama-server → vectors.

A :class:`QueryEncoder` is bound to one model slot; it owns no lock. The backend is anything with
``embed_ids(list[list[int]]) -> EmbedResult`` (the llama-server client in production, a fake in tests).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from vkm_corpus.embeddings import postprocess as pp
from vkm_corpus.embeddings.signature import QueryConfig, query_config
from vkm_corpus.embeddings.specs import EncoderSpec, get
from vkm_corpus.embeddings.tokenize import SpecTokenizer
from vkm_corpus.retrieval_service.config import ModelSlot


@dataclass
class QueryVectors:
    role: str
    key: str
    vector: np.ndarray | None          # dense / visual (the text tower of a page-image model)
    vectors: np.ndarray | None         # late: [tokens, dim]
    n_tokens: int
    signature: str
    encode_ms: float


class QueryEncoder:
    def __init__(self, slot: ModelSlot, spec: EncoderSpec, tokenizer: SpecTokenizer, backend: Any, *,
                 heads: dict[str, np.ndarray] | None = None) -> None:
        self.slot, self.spec, self.tok, self.backend = slot, spec, tokenizer, backend
        self.heads = heads or {}
        self.late = slot.role == "late"
        self.qconfig: QueryConfig = query_config(
            spec, weights_sha256=slot.gguf_sha256, quantization=slot.quant, tokenizer_sha256=tokenizer.tokenizer_sha256
            or slot.tokenizer_sha256, dimension=slot.dimension, max_len=slot.query_max_len, late=self.late,
            heads_sha256=slot.heads_sha256, query_instruction=slot.query_instruction)
        self.signature = self.qconfig.signature()

    @classmethod
    def from_slot(cls, slot: ModelSlot, backend: Any) -> "QueryEncoder":
        spec = get(slot.key)
        from vkm_corpus.update.remote_retrieval import capture_load_files
        paths = [Path(slot.tokenizer_dir) / spec.tokenizer_file]
        cfg = Path(slot.tokenizer_dir) / "tokenizer_config.json"
        if cfg.is_file():
            paths.append(cfg)
        if slot.heads:
            paths.append(Path(slot.heads))
        before = capture_load_files(paths)
        from vkm_corpus.update.native_files import optional_watch
        watch = optional_watch(paths)
        tok = SpecTokenizer.from_dir(spec, Path(slot.tokenizer_dir),
                                     expected_sha256=slot.tokenizer_sha256 or None)
        heads = dict(np.load(slot.heads)) if slot.heads else None
        result = cls(slot, spec, tok, backend, heads=heads)
        result._load_files = before if capture_load_files(paths) == before else None
        result._load_watch = watch
        return result

    @property
    def pooling(self) -> str:
        """llama-server pooling for this slot."""
        return "none" if (self.late or self.spec.pooling == "none") else self.spec.pooling

    def keepalive_ids(self) -> list[int]:
        return list(self.tok.encode("a", "query", max_len=8).ids)

    def encode(self, text: str) -> QueryVectors:
        t0 = time.perf_counter()
        enc = self.tok.encode(text, "query", max_len=self.slot.query_max_len,
                              prefix=None if self.late else self.slot.query_instruction)
        res = self.backend.embed_ids([list(enc.ids)])
        v = res.vectors[0]
        if self.late:
            m = pp.colbert_tokens(self.spec, v, enc.keep, head=self.heads.get("colbert_w"),
                                  head_bias=self.heads.get("colbert_b"), dim=self.slot.dimension)
            return QueryVectors("late", self.spec.key, None, m, len(enc.ids), self.signature,
                                round((time.perf_counter() - t0) * 1e3, 2))
        pooled = v if np.asarray(v).ndim == 1 else pp.pool(v, self.spec.pooling)
        d = pp.finalize_dense(self.spec, pooled, dim=self.slot.dimension)
        vec = d.vector
        if not self.spec.normalize:          # cosine space in OpenSearch: normalise unnormalised models (mDenseOn, pplx)
            vec = pp.l2_normalize(vec)
        return QueryVectors(self.slot.role, self.spec.key, vec, None, len(enc.ids), self.signature,
                            round((time.perf_counter() - t0) * 1e3, 2))

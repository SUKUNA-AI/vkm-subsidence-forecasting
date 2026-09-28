"""Exact vector search for the benchmark: dense (cosine), vector precisions (§21), Matryoshka truncation (§19), learned
sparse (dot product of term weights) and late interaction (MaxSim, §9).

Exact search is deliberate: the benchmark measures the models, not ANN errors (HNSW in OpenSearch is measured
separately). Unit counts are in the thousands, so brute force is cheap.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np

PRECISIONS = ("fp32", "fp16", "int8", "binary")


def l2_normalize(x: np.ndarray, axis: int = -1) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    norm = np.linalg.norm(x, axis=axis, keepdims=True)
    return x / np.maximum(norm, 1e-12)


def truncate(x: np.ndarray, dim: int) -> np.ndarray:
    """Matryoshka truncation to the first ``dim`` components, re-normalised."""
    if dim > x.shape[-1]:
        raise ValueError(f"dim {dim} > {x.shape[-1]}")
    return l2_normalize(np.asarray(x)[..., :dim])


def quantize(x: np.ndarray, precision: str) -> np.ndarray:
    """Storage precision of document vectors; returns float32 values as they would be scored.

    int8: symmetric per-vector scaling to [-127, 127]; binary: sign (scored by cosine of ±1 vectors, which is a
    monotone transform of the Hamming distance)."""
    x = np.asarray(x, dtype=np.float32)
    if precision == "fp32":
        return x
    if precision == "fp16":
        return x.astype(np.float16).astype(np.float32)
    if precision == "int8":
        scale = np.maximum(np.abs(x).max(axis=-1, keepdims=True), 1e-12) / 127.0
        return np.round(x / scale).clip(-127, 127) * scale
    if precision == "binary":
        return l2_normalize(np.where(x >= 0, 1.0, -1.0).astype(np.float32))
    raise ValueError(f"precision must be one of {PRECISIONS}")


def bytes_per_vector(dim: int, precision: str) -> int:
    return {"fp32": 4 * dim, "fp16": 2 * dim, "int8": dim, "binary": (dim + 7) // 8}[precision]


@dataclass
class DenseIndex:
    ids: list[str]
    matrix: np.ndarray                     # (n, dim), L2-normalised

    @classmethod
    def build(cls, ids: Sequence[str], vectors: np.ndarray, *, dim: int | None = None,
              precision: str = "fp32") -> "DenseIndex":
        v = l2_normalize(vectors)
        if dim is not None and dim != v.shape[1]:
            v = truncate(v, dim)
        v = quantize(v, precision)
        if precision == "int8":
            v = l2_normalize(v)
        return cls(list(ids), v)

    def search(self, query: np.ndarray, k: int = 100, *, allowed: set[str] | None = None) -> list[tuple[str, float]]:
        q = l2_normalize(np.asarray(query, dtype=np.float32).reshape(1, -1))[0]
        if q.shape[0] != self.matrix.shape[1]:
            q = truncate(q, self.matrix.shape[1])
        scores = self.matrix @ q
        order = np.lexsort((np.array(self.ids), -scores))          # score desc, id asc (deterministic)
        out = []
        for i in order:
            if allowed is not None and self.ids[i] not in allowed:
                continue
            out.append((self.ids[i], float(scores[i])))
            if len(out) >= k:
                break
        return out

    def search_many(self, queries: np.ndarray, k: int = 100) -> list[list[tuple[str, float]]]:
        return [self.search(q, k) for q in np.asarray(queries)]

    @property
    def dim(self) -> int:
        return int(self.matrix.shape[1])


# ---------------------------------------------------------------- learned sparse
SparseVec = Mapping[str, float]


@dataclass
class SparseIndex:
    ids: list[str]
    postings: dict[str, list[tuple[int, float]]]

    @classmethod
    def build(cls, ids: Sequence[str], vectors: Sequence[SparseVec]) -> "SparseIndex":
        postings: dict[str, list[tuple[int, float]]] = {}
        for i, vec in enumerate(vectors):
            for term, w in vec.items():
                if w > 0:
                    postings.setdefault(term, []).append((i, float(w)))
        return cls(list(ids), postings)

    def search(self, query: SparseVec, k: int = 100) -> list[tuple[str, float]]:
        acc: dict[int, float] = {}
        for term, qw in query.items():
            for i, dw in self.postings.get(term, ()):
                acc[i] = acc.get(i, 0.0) + qw * dw
        ranked = sorted(acc.items(), key=lambda x: (-x[1], self.ids[x[0]]))[:k]
        return [(self.ids[i], s) for i, s in ranked]

    def n_postings(self) -> int:
        return sum(len(p) for p in self.postings.values())


# ---------------------------------------------------------------- late interaction
def maxsim(query_tokens: np.ndarray, doc_tokens: np.ndarray) -> float:
    """ColBERT score Σ_i max_j ⟨q_i, d_j⟩ over L2-normalised token vectors."""
    if len(query_tokens) == 0 or len(doc_tokens) == 0:
        return 0.0
    sims = np.asarray(query_tokens, dtype=np.float32) @ np.asarray(doc_tokens, dtype=np.float32).T
    return float(sims.max(axis=1).sum())


@dataclass
class LateIndex:
    ids: list[str]
    docs: list[np.ndarray]                  # per unit: (n_tokens, dim), normalised

    @classmethod
    def build(cls, ids: Sequence[str], token_vectors: Sequence[np.ndarray], *, dim: int | None = None,
              precision: str = "fp32") -> "LateIndex":
        docs = []
        for t in token_vectors:
            t = l2_normalize(np.asarray(t, dtype=np.float32))
            if dim is not None and dim != t.shape[1]:
                t = truncate(t, dim)
            docs.append(quantize(t, precision))
        return cls(list(ids), docs)

    def score(self, query_tokens: np.ndarray, candidates: Sequence[str] | None = None) -> list[tuple[str, float]]:
        q = l2_normalize(np.asarray(query_tokens, dtype=np.float32))
        if self.docs and q.shape[1] != self.docs[0].shape[1]:
            q = truncate(q, self.docs[0].shape[1])
        pos = {uid: i for i, uid in enumerate(self.ids)}
        pool = [pos[c] for c in candidates if c in pos] if candidates is not None else range(len(self.ids))
        scored = [(self.ids[i], maxsim(q, self.docs[i])) for i in pool]
        return sorted(scored, key=lambda x: (-x[1], x[0]))

    def storage(self, precision: str = "fp32") -> dict[str, float]:
        n_tokens = [len(d) for d in self.docs]
        dim = self.docs[0].shape[1] if self.docs else 0
        total = sum(n_tokens)
        return {"units": len(self.docs), "tokens_total": total,
                "tokens_mean": total / len(n_tokens) if n_tokens else 0.0,
                "tokens_p95": float(np.percentile(n_tokens, 95)) if n_tokens else 0.0,
                "dim": dim, "bytes": total * bytes_per_vector(dim, precision)}

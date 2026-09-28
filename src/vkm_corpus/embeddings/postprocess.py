"""Backend-independent post-processing of encoder outputs (numpy only; used by the service, the RX580 worker and the
parity harness, so parity measures exactly the production path).

Inputs are what llama.cpp returns: a pooled vector (``pooling`` cls/mean/last in the GGUF graph) or the token matrix
``H[n_tokens, d]`` (pooling none; after the in-graph ``dense_2`` head when the spec says so). Everything else —
Matryoshka truncation, normalisation, the pplx int8 transform, ColBERT token selection, BGE-M3 sparse/ColBERT heads
and contextual chunk pooling — happens here, identically for every backend.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np

from vkm_corpus.embeddings.specs import EncoderSpec

EPS = 1e-12


def l2_normalize(x: np.ndarray, axis: int = -1) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    n = np.linalg.norm(x, axis=axis, keepdims=True)
    return x / np.maximum(n, EPS)


def pool(H: np.ndarray, how: str, mask: Sequence[bool] | None = None) -> np.ndarray:
    """Pool a token matrix; ``mask`` marks valid positions (default: all)."""
    H = np.asarray(H, dtype=np.float32)
    if mask is not None:
        idx = np.flatnonzero(np.asarray(mask, dtype=bool))
    else:
        idx = np.arange(H.shape[0])
    if idx.size == 0:
        return np.zeros(H.shape[1], dtype=np.float32)
    if how == "cls":
        return H[idx[0]]
    if how == "last":
        return H[idx[-1]]
    if how == "mean":
        return H[idx].mean(axis=0)
    raise ValueError(f"unknown pooling {how!r}")


def truncate(v: np.ndarray, dim: int | None, *, renormalize: bool) -> np.ndarray:
    """Matryoshka prefix of the last axis; re-normalised when the spec normalises."""
    v = np.asarray(v, dtype=np.float32)
    if dim is None or dim >= v.shape[-1]:
        return l2_normalize(v) if renormalize else v
    out = v[..., :dim]
    return l2_normalize(out) if renormalize else out


def tanh_int8(v: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """pplx-embed native output: ``t = tanh(v)`` (float) and ``q = clip(round(127 t), -128, 127)`` (int8)."""
    t = np.tanh(np.asarray(v, dtype=np.float32))
    q = np.clip(np.rint(t * 127.0), -128, 127).astype(np.int8)
    return t, q


@dataclass(frozen=True)
class DenseOut:
    vector: np.ndarray           # float32, final (normalised if the spec normalises, else raw/tanh)
    int8: np.ndarray | None = None


def finalize_dense(spec: EncoderSpec, pooled: np.ndarray, *, dim: int | None = None) -> DenseOut:
    """Pooled backbone vector → final dense vector of the spec (transform, truncation, normalisation)."""
    v = np.asarray(pooled, dtype=np.float32)
    q = None
    if spec.output_transform == "tanh_int8":
        # transform first (it is elementwise), then truncate; cosine is used on the (unnormalised) result
        t, q = tanh_int8(v)
        v = t
        if dim is not None and dim < v.shape[-1]:
            v, q = v[..., :dim], q[..., :dim]
        return DenseOut(v, q)
    return DenseOut(truncate(v, dim, renormalize=spec.normalize))


def dense_from_tokens(spec: EncoderSpec, H: np.ndarray, *, mask: Sequence[bool] | None = None,
                      dim: int | None = None) -> DenseOut:
    return finalize_dense(spec, pool(H, spec.pooling if spec.pooling != "none" else "mean", mask), dim=dim)


def context_chunks(spec: EncoderSpec, H: np.ndarray, spans: Iterable[tuple[int, int]], *,
                   dim: int | None = None) -> list[DenseOut]:
    """Late chunking: mean-pool every chunk span of one contextual forward pass (pplx-embed-context)."""
    H = np.asarray(H, dtype=np.float32)
    out = []
    for a, b in spans:
        v = H[a:b].mean(axis=0) if b > a else np.zeros(H.shape[1], dtype=np.float32)
        out.append(finalize_dense(spec, v, dim=dim))
    return out


def colbert_tokens(spec: EncoderSpec, H: np.ndarray, keep: Sequence[bool] | None, *,
                   head: np.ndarray | None = None, head_bias: np.ndarray | None = None,
                   dim: int | None = None) -> np.ndarray:
    """Token vectors of a late-interaction model: select kept positions, apply a gateway-side head if any, normalise,
    Matryoshka prefix (re-normalised)."""
    cb = spec.colbert
    if cb is None:
        raise ValueError(f"{spec.key} has no late-interaction output")
    H = np.asarray(H, dtype=np.float32)
    if keep is not None:
        H = H[np.asarray(keep, dtype=bool)]
    if head is not None:
        H = H @ np.asarray(head, dtype=np.float32).T
        if head_bias is not None:
            H = H + np.asarray(head_bias, dtype=np.float32)
    if cb.normalize_tokens:
        H = l2_normalize(H)
    if dim is not None and dim < H.shape[-1]:
        H = H[:, :dim]
        if cb.normalize_tokens:
            H = l2_normalize(H)
    return np.ascontiguousarray(H, dtype=np.float32)


@dataclass(frozen=True)
class SparseOut:
    token_ids: np.ndarray        # int32, sorted ascending
    weights: np.ndarray          # float32


def bge_m3_sparse(H: np.ndarray, ids: Sequence[int], w: np.ndarray, b: np.ndarray, *,
                  unused_ids: Iterable[int]) -> SparseOut:
    """BGE-M3 lexical weights: ``relu(H·wᵀ + b)`` per position, max per token id, special ids removed, zeros dropped
    (FlagEmbedding ``_process_token_weights``)."""
    H = np.asarray(H, dtype=np.float32)
    s = np.maximum(H @ np.asarray(w, dtype=np.float32).reshape(-1) + float(np.asarray(b).reshape(-1)[0]), 0.0)
    best: dict[int, float] = {}
    unused = set(int(x) for x in unused_ids)
    for tid, weight in zip(ids, s.tolist()):
        tid = int(tid)
        if tid in unused or weight <= 0.0:
            continue
        if weight > best.get(tid, 0.0):
            best[tid] = weight
    keys = np.array(sorted(best), dtype=np.int32)
    return SparseOut(keys, np.array([best[k] for k in keys.tolist()], dtype=np.float32))


def maxsim(Q: np.ndarray, D: np.ndarray) -> float:
    """ColBERT late-interaction score: Σ_q max_d ⟨q, d⟩ (vectors already normalised)."""
    if Q.size == 0 or D.size == 0:
        return 0.0
    return float((np.asarray(Q, np.float32) @ np.asarray(D, np.float32).T).max(axis=1).sum())


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    a = np.asarray(a, np.float32)
    b = np.asarray(b, np.float32)
    return float(a @ b / max(np.linalg.norm(a) * np.linalg.norm(b), EPS))

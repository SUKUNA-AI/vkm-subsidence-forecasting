"""Lab signatures of document embeddings (task §37) and queries (§38).

The production signature module belongs to agent K (``vkm_corpus.embeddings``); the fields here are the same so lab
caches and production artifacts can be compared. Hashes use D's ``config_hash`` (canonical JSON, sha256).
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from vkm_corpus.contracts.signatures import config_hash
from vkm_corpus.retrieval_lab import LAB_VERSION
from vkm_corpus.retrieval_lab.encoders import ModelSpec


def file_sha256(path: str | Path) -> str | None:
    p = Path(path)
    if not p.is_file():
        return None
    return hashlib.sha256(p.read_bytes()).hexdigest()


def doc_signature(spec: ModelSpec, *, mode: str, unit_rule: str, context_variant: str, compute_precision: str,
                  storage_precision: str = "fp32", dim: int | None = None, tokenizer_sha256: str | None = None
                  ) -> dict[str, Any]:
    body = {
        "model_id": spec.model_id, "model_revision": spec.revision, "embedding_mode": mode,
        "dimension": dim or spec.dim, "pooling": spec.pooling, "normalization": "l2" if spec.normalize else "none",
        "document_instruction": spec.doc_prefix, "max_doc_tokens": spec.max_doc_tokens,
        "precision": storage_precision, "weights_dtype": spec.weights_dtype, "compute_precision": compute_precision,
        "quantization": spec.native_precision, "unit_rule": unit_rule, "context_variant": context_variant,
        "late_config": spec.late or None, "tokenizer_sha256": tokenizer_sha256, "pipeline_version": f"lab-{LAB_VERSION}",
        "backend": spec.backend if spec.family == "multivector" else None,
    }
    return {**body, "config_hash": config_hash(body)}


def query_signature(spec: ModelSpec, *, mode: str, compute_precision: str, dim: int | None = None,
                    tokenizer_sha256: str | None = None) -> dict[str, Any]:
    body = {
        "model_id": spec.model_id, "model_revision": spec.revision, "embedding_mode": mode,
        "query_instruction": spec.query_prefix, "dimension": dim or spec.dim,
        "normalization": "l2" if spec.normalize else "none", "tokenizer_sha256": tokenizer_sha256,
        "max_query_tokens": spec.max_query_tokens, "late_config": spec.late or None,
        "compute_precision": compute_precision, "backend": spec.backend if spec.family == "multivector" else None,
    }
    return {**body, "config_hash": config_hash(body)}


def cache_key(signature: dict[str, Any]) -> str:
    """Encoding cache key: the signature without the dimension/storage precision (they are applied after encoding)."""
    body = {k: v for k, v in signature.items() if k not in ("dimension", "precision", "config_hash")}
    return config_hash(body)[:24]

"""Embedding signature (постановка лаборатории §37) and query signature (§38).

Two levels, one meaning each (the same principle as H-05 for model calls):

* :class:`EmbeddingConfig` — everything that changes a document vector: model id + revision, the weights artifact
  (file, sha256, quantisation), mode, dimension, pooling, normalisation, output transform, document instruction,
  text rule (how a canonical object becomes the embedded text, §17), truncation length, tokenizer sha256, gateway
  heads sha256, vector storage precision and the embeddings pipeline version. ``config_signature`` = sha256 of its
  canonical JSON; it names the artifact directory (``<config-hash>``).
* ``embedding_signature(object_id, text_hash, config)`` — identity of one stored vector. Same text hash + same config
  signature ⇒ no inference (§46).

The *backend* (llama.cpp Vulkan on the RX580, llama.cpp CUDA or torch on the RTX) and the worker are recorded on every
row but are **not** part of the signature: parity-approved backends running the same weights artifact produce
interchangeable vectors (§31). Different weights (e.g. Q8_0 GGUF vs bf16 safetensors) are a different signature.

:class:`QueryConfig` / ``query_signature`` cover the query side (§38): model, revision, weights, query instruction,
dimension, normalisation, tokenizer, max length and the late-interaction query settings.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from vkm_corpus.embeddings.specs import EncoderSpec

EMBEDDINGS_PIPELINE_VERSION = "emb-0.1.0"

Mode = Literal["dense", "sparse", "multivector", "visual", "context"]
StoragePrecision = Literal["float32", "float16", "int8", "binary"]


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def text_hash(text: str) -> str:
    """Hash of the exact embedded text (UTF-8, no further normalisation): the re-embed key of §46."""
    return sha256_text(text)


@dataclass(frozen=True)
class EmbeddingConfig:
    model_id: str
    model_revision: str
    weights_file: str
    weights_sha256: str
    quantization: str                    # Q8_0 | Q6_K | Q5_K_M | F16 | BF16 | F32
    mode: Mode
    dimension: int
    pooling: str
    normalization: Literal["l2", "none"]
    output_transform: str = "none"
    document_instruction: str = ""
    text_rule: str = "vkm-units-v1/A"     # J's embedding unit rule + context variant (retrieval_lab.units)
    max_len: int = 512
    tokenizer_sha256: str = ""
    heads_sha256: str = ""
    storage_precision: StoragePrecision = "float32"
    late: dict[str, Any] = field(default_factory=dict)   # ColBERT document settings (marker, skiplist, token dim)
    pipeline_version: str = EMBEDDINGS_PIPELINE_VERSION
    # A reference implementation with its own numerics (e.g. "flagembedding-cpu-fp32") is part of the signature; the
    # default "" (llama.cpp GGUF on any GPU backend, DN-K2) is omitted, so existing signatures are unchanged.
    backend: str = ""
    # image input of a visual document encoder (agent VIS: which stored image, processor file sha256, pixel bounds);
    # omitted when empty, so every text signature is unchanged
    image: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("backend", "image"):
            if not d.get(k):
                d.pop(k, None)
        return d

    def signature(self) -> str:
        return sha256_text("vkm-emb-config-v1|" + canonical_json(self.as_dict()))

    @property
    def config_hash(self) -> str:
        return self.signature()


def embedding_signature(object_id: str, text_sha256: str, config: EmbeddingConfig | str) -> str:
    cfg = config if isinstance(config, str) else config.signature()
    return sha256_text("|".join(("vkm-emb-v1", object_id, text_sha256, cfg)))


@dataclass(frozen=True)
class QueryConfig:
    model_id: str
    model_revision: str
    weights_sha256: str
    quantization: str
    query_instruction: str
    dimension: int
    pooling: str
    normalization: Literal["l2", "none"]
    tokenizer_sha256: str
    max_len: int
    output_transform: str = "none"
    late: dict[str, Any] = field(default_factory=dict)   # query marker, query_maxlen, expansion token, attend flag
    heads_sha256: str = ""
    pipeline_version: str = EMBEDDINGS_PIPELINE_VERSION
    backend: str = ""                                  # as in EmbeddingConfig: in the signature only when set
    template: str = ""                                 # chat-style query rendering (visual towers); only when set

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("backend", "template"):
            if not d.get(k):
                d.pop(k, None)
        return d

    def signature(self) -> str:
        return sha256_text("vkm-query-config-v1|" + canonical_json(self.as_dict()))


def _late_settings(spec: EncoderSpec, role: str) -> dict[str, Any]:
    cb = spec.colbert
    if cb is None:
        return {}
    if role == "query":
        return {"marker": cb.query_marker, "query_maxlen": cb.query_maxlen, "expansion_token": cb.expansion_token,
                "attend_to_expansion": cb.attend_to_expansion, "style": cb.style, "token_dim": cb.dim,
                "normalize_tokens": cb.normalize_tokens, "head": cb.head}
    return {"marker": cb.doc_marker, "doc_maxlen": cb.doc_maxlen, "skip_punctuation": cb.skip_punctuation_in_docs,
            "drop_special_tokens": list(cb.drop_special_tokens), "style": cb.style, "token_dim": cb.dim,
            "normalize_tokens": cb.normalize_tokens, "head": cb.head}


def document_config(spec: EncoderSpec, *, mode: Mode, weights_file: str, weights_sha256: str, quantization: str,
                    tokenizer_sha256: str, dimension: int | None = None, max_len: int = 512,
                    heads_sha256: str = "", text_rule: str = "vkm-units-v1/A",
                    storage_precision: StoragePrecision = "float32") -> EmbeddingConfig:
    late = _late_settings(spec, "document") if mode == "multivector" else {}
    dim = dimension or (spec.colbert.dim if mode == "multivector" and spec.colbert else spec.output_dim)
    return EmbeddingConfig(
        model_id=spec.model_id, model_revision=spec.model_revision, weights_file=weights_file,
        weights_sha256=weights_sha256, quantization=quantization, mode=mode, dimension=int(dim),
        pooling=spec.pooling if mode in ("dense", "context") else "none",
        normalization="l2" if (spec.normalize or (mode == "multivector" and spec.colbert
                                                  and spec.colbert.normalize_tokens)) else "none",
        output_transform=spec.output_transform, document_instruction=spec.doc_prefix, text_rule=text_rule,
        max_len=int(max_len), tokenizer_sha256=tokenizer_sha256, heads_sha256=heads_sha256,
        storage_precision=storage_precision, late=late)


def query_config(spec: EncoderSpec, *, weights_sha256: str, quantization: str, tokenizer_sha256: str,
                 dimension: int | None = None, max_len: int = 512, late: bool = False,
                 heads_sha256: str = "", query_instruction: str | None = None) -> QueryConfig:
    dim = dimension or (spec.colbert.dim if late and spec.colbert else spec.output_dim)
    return QueryConfig(
        model_id=spec.model_id, model_revision=spec.model_revision, weights_sha256=weights_sha256,
        quantization=quantization,
        query_instruction=spec.query_prefix if query_instruction is None else query_instruction,
        dimension=int(dim), pooling="none" if late else spec.pooling,
        normalization="l2" if (spec.normalize or (late and spec.colbert and spec.colbert.normalize_tokens))
        else "none",
        tokenizer_sha256=tokenizer_sha256, max_len=int(max_len), output_transform=spec.output_transform,
        late=_late_settings(spec, "query") if late else {}, heads_sha256=heads_sha256,
        template=spec.query_template)

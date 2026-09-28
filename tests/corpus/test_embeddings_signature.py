"""Embedding signature (§37) and query signature (§38): deterministic and sensitive to every field."""
from __future__ import annotations

import dataclasses

import pytest

from vkm_corpus.embeddings.signature import (
    EmbeddingConfig,
    QueryConfig,
    document_config,
    embedding_signature,
    query_config,
    text_hash,
)
from vkm_corpus.embeddings.specs import get

SHA = "a" * 64


def _doc_cfg(**kw) -> EmbeddingConfig:
    base = dict(model_id="org/model", model_revision="r" * 40, weights_file="m-Q8_0.gguf", weights_sha256=SHA,
                quantization="Q8_0", mode="dense", dimension=768, pooling="cls", normalization="l2",
                output_transform="none", document_instruction="", text_rule="embed_text_v1", max_len=512,
                tokenizer_sha256="b" * 64, heads_sha256="", storage_precision="float32", late={},
                pipeline_version="emb-0.1.0")
    base.update(kw)
    return EmbeddingConfig(**base)


ALTERED = {
    "model_id": "org/other", "model_revision": "s" * 40, "weights_file": "m-Q6_K.gguf", "weights_sha256": "c" * 64,
    "quantization": "Q6_K", "mode": "multivector", "dimension": 512, "pooling": "mean", "normalization": "none",
    "output_transform": "tanh_int8", "document_instruction": "Document: ", "text_rule": "embed_text_v2",
    "max_len": 1024, "tokenizer_sha256": "d" * 64, "heads_sha256": "e" * 64, "storage_precision": "float16",
    "late": {"token_dim": 128}, "pipeline_version": "emb-0.2.0", "backend": "flagembedding-cpu-fp32",
}


def test_config_signature_is_deterministic():
    assert _doc_cfg().signature() == _doc_cfg().signature()
    assert len(_doc_cfg().signature()) == 64


@pytest.mark.parametrize("field_name", sorted(ALTERED))
def test_config_signature_changes_with_every_field(field_name):
    assert set(ALTERED) == {f.name for f in dataclasses.fields(EmbeddingConfig)}
    assert _doc_cfg(**{field_name: ALTERED[field_name]}).signature() != _doc_cfg().signature()


def test_embedding_signature_depends_on_object_text_and_config():
    cfg = _doc_cfg()
    base = embedding_signature("VKM-SRC-001:p0001:b000000000001", text_hash("x"), cfg)
    assert base == embedding_signature("VKM-SRC-001:p0001:b000000000001", text_hash("x"), cfg.signature())
    assert base != embedding_signature("VKM-SRC-001:p0001:b000000000002", text_hash("x"), cfg)
    assert base != embedding_signature("VKM-SRC-001:p0001:b000000000001", text_hash("y"), cfg)
    assert base != embedding_signature("VKM-SRC-001:p0001:b000000000001", text_hash("x"), _doc_cfg(max_len=256))


def _q_cfg(**kw) -> QueryConfig:
    base = dict(model_id="org/model", model_revision="r" * 40, weights_sha256=SHA, quantization="Q8_0",
                query_instruction="Query: ", dimension=768, pooling="cls", normalization="l2",
                tokenizer_sha256="b" * 64, max_len=64, output_transform="none", late={}, heads_sha256="",
                pipeline_version="emb-0.1.0")
    base.update(kw)
    return QueryConfig(**base)


Q_ALTERED = {"model_id": "x/y", "model_revision": "s" * 40, "weights_sha256": "c" * 64, "quantization": "F16",
             "query_instruction": "Instruct: salt\nQuery:", "dimension": 256, "pooling": "last",
             "normalization": "none", "tokenizer_sha256": "d" * 64, "max_len": 32, "output_transform": "tanh_int8",
             "late": {"query_maxlen": 32}, "heads_sha256": "e" * 64, "pipeline_version": "emb-9",
             "backend": "flagembedding-cpu-fp32"}


@pytest.mark.parametrize("field_name", sorted(Q_ALTERED))
def test_query_signature_changes_with_every_field(field_name):
    assert set(Q_ALTERED) == {f.name for f in dataclasses.fields(QueryConfig)}
    assert _q_cfg(**{field_name: Q_ALTERED[field_name]}).signature() != _q_cfg().signature()
    assert _q_cfg().signature() == _q_cfg().signature()


def test_backend_is_not_part_of_the_signature():
    """Same weights on RX580 Vulkan and RTX CUDA → same document signature (backend recorded per row only)."""
    spec = get("granite-311m-r2")
    a = document_config(spec, mode="dense", weights_file="g-Q8_0.gguf", weights_sha256=SHA, quantization="Q8_0",
                        tokenizer_sha256="b" * 64)
    b = document_config(spec, mode="dense", weights_file="g-Q8_0.gguf", weights_sha256=SHA, quantization="Q8_0",
                        tokenizer_sha256="b" * 64)
    assert a.signature() == b.signature()
    assert "backend" not in a.as_dict()


def test_builders_fill_late_and_matryoshka_settings():
    late = get("jina-colbert-v2")
    d = document_config(late, mode="multivector", weights_file="j.gguf", weights_sha256=SHA, quantization="Q8_0",
                        tokenizer_sha256="b" * 64, dimension=96)
    assert d.dimension == 96 and d.late["token_dim"] == 128 and d.late["skip_punctuation"] is True
    q = query_config(late, weights_sha256=SHA, quantization="Q8_0", tokenizer_sha256="b" * 64, late=True)
    assert q.late["query_maxlen"] == 32 and q.late["expansion_token"] == "<mask>" and q.dimension == 128
    dense = query_config(get("qwen3-emb-0.6b"), weights_sha256=SHA, quantization="Q8_0", tokenizer_sha256="b" * 64,
                         dimension=512, query_instruction="Instruct: VKM\nQuery:")
    assert dense.dimension == 512 and dense.query_instruction.startswith("Instruct: VKM")


def test_reference_backend_enters_the_signature_only_when_set():
    """GGUF encodings keep one signature across GPU backends (DN-K2, backend ""); a reference implementation with its
    own numerics (FlagEmbedding on CPU) is a different config."""
    plain = _doc_cfg()
    ref = _doc_cfg(backend="flagembedding-cpu-fp32")
    assert "backend" not in plain.as_dict() and ref.as_dict()["backend"] == "flagembedding-cpu-fp32"
    assert plain.signature() != ref.signature()
    assert _q_cfg().signature() != _q_cfg(backend="flagembedding-cpu-fp32").signature()

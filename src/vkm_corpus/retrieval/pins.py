"""Pinned model artifacts of the EDGE rerankers (CP-18, project F §6.4; sha256 verified at download, receipts in
``work/corpus_platform/impl/edge_receipts/``). Weights are never committed; they live in the model directories of
WORKSTATION and ``$VKM_EDGE_ROOT/models`` on EDGE."""
from __future__ import annotations

LICENSE = "CC-BY-NC-4.0"
LLAMA_CPP_COMMIT = "4da6337767f973e2b4d0797e5b323d77d8565e4a"   # tag b11223, 2026-09-27

TEXT = {
    "model_id": "jinaai/jina-reranker-v3.5",
    "model_revision": "e8a93f33f0b22108f8c2364f8484ce3422552fbc",
    "weights_file": "model.safetensors",
    "weights_sha256": "50c684b703a1770389cd2992c6ca3c5f76ad0a270e975374c182cde3e5dfb6fe",
    "tokenizer_file": "tokenizer.json",
    "tokenizer_sha256": "4e95945ab0cef486709f760b81efcc7a6e75747f9165d13ead29159737455803",
    "quant": "fp16 (no quantization)",
    "placement": "GPU: all layers (existing service, transformers-cuda)",
    "backend": "transformers-cuda (existing careerops-reranker service, unchanged)",
    "score_semantics": "cosine(projector(h_doc), projector(h_query)) in [-1, 1]; listwise over the call",
}

VISUAL = {
    "model_id": "jinaai/jina-reranker-m0-GGUF",
    "model_revision": "61490ce6a4799192781ab7beab4ee4661675488c",
    "weights_file": "jina-reranker-m0-Q6_K.gguf",
    "weights_sha256": "115f0be6a38939fc7a960d088588c2d28ad1db26da1a89a0e465c932d8ace6d5",
    "quant": "Q6_K",
    "head_file": "mlp_weights.npz",
    "head_sha256": "b7bea0fbe19940fbd1a98ffb3287bc74aec064646b4f602d287605ead9e36448",
    "source_model_id": "jinaai/jina-reranker-m0",
    "source_revision": "94bfe0aeb2d4dd7978362699cddd5893d4e0adc8",
    "mmproj_file": "mmproj-jina-reranker-m0-Q8_0.gguf",
    "mmproj_quant": "Q8_0",
    "mmproj_sha256": "91628a15717f4aceeafe36b529329e57ee01af6ce4707545413be7c0844337b9",
    "tokenizer_file": "tokenizer.json",
    "tokenizer_sha256": "091aa7594dc2fcfbfa06b9e3c22a5f0562ac14f30375c13af7309407a0e67b8a",
    "n_layers": 28,
    "backend": "llama.cpp llama-server (embeddings, pooling last) + MLP head in the gateway",
    "score_semantics": "sigmoid(mlp(h_last) - 2.65) in (0, 1); pointwise, comparable within one call",
}

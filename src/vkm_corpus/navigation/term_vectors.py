"""Term vectors of the term dictionary (NAV part ``translations``): the phrases to encode and their encoding.

Two steps, because they need different environments:

1. :func:`phrases` (extra ``navigation``: pymorphy3) — every phrase the dictionary methods look at: the N3 terms
   and the phrases found in keyword lists, bracketed glosses and seeds (``lang``, ``key``, ``text``); deterministic
   for a snapshot (``vkm-corpus nav term-phrases``);
2. :func:`encode` (torch + sentence-transformers, the retrieval lab's encoder adapters) — one L2-normalised vector
   per phrase with jina-embeddings-v5-text-nano-retrieval (the dense model of the search, multilingual; spec ``D2``
   of ``benchmarks/retrieval_v0/configs/models.json``; weights from ``$VKM_MODELS_DIR``, offline)
   (``vkm-corpus nav term-vectors``). GPU memory can be capped (``max_vram_fraction``); CPU works too.

The vectors are a build input of ``term_dictionary.build`` (option ``term_vectors``), never evidence by themselves.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import pyarrow as pa

DEFAULT_SPEC = "D2"                  # jina-embeddings-v5-text-nano-retrieval
PROMPT = "query"                     # both sides of a term pair are short phrases: the query prompt («Query: »)
METADATA_KEY = b"vkm_nav_term_vectors"


def phrases(con: Any, *, terms: Any, term_edges: Any = None, term_mentions: Any = None, formula_symbols: Any = None,
            seeds: Any = None) -> pa.Table:
    from vkm_corpus.navigation import term_dictionary as TD  # noqa: PLC0415

    ctx = TD.make_context(con, terms=terms, term_mentions=term_mentions)
    TD.collect(con, ctx, terms=terms, term_edges=term_edges, formula_symbols=formula_symbols, seeds=seeds)
    table = TD.phrases_table(ctx)
    digest = hashlib.sha256("\n".join(f"{a}\t{b}\t{c}" for a, b, c in zip(
        table.column("lang").to_pylist(), table.column("key").to_pylist(),
        table.column("text").to_pylist())).encode("utf-8")).hexdigest()
    meta = {"rule_version": TD.RULE_VERSION, "morphology": ctx.morph.name, "phrases": table.num_rows,
            "phrases_sha256": digest}
    return table.replace_schema_metadata({METADATA_KEY: json.dumps(meta).encode("utf-8")})


def encode(table: pa.Table, *, models_dir: str | Path | None, spec_path: str | Path, spec_key: str = DEFAULT_SPEC,
           device: str = "cpu", precision: str | None = None, batch_size: int = 256,
           max_vram_fraction: float | None = None, encoder: Any = None) -> pa.Table:
    """``table`` (lang, key, text) → the same rows + ``vector`` (list<float32>); ``encoder`` may be injected
    (tests: the lab's FakeEncoder)."""
    import numpy as np  # noqa: PLC0415

    t0 = time.time()
    texts = table.column("text").to_pylist()
    spec_info: dict[str, Any] = {}
    if encoder is None:
        from vkm_corpus.retrieval_lab.encoders import load_specs, make_encoder  # noqa: PLC0415

        spec = load_specs(spec_path)[spec_key]
        if device.startswith("cuda") and max_vram_fraction:
            import torch  # noqa: PLC0415

            torch.cuda.set_per_process_memory_fraction(float(max_vram_fraction))
        encoder = make_encoder(spec, models_dir, device=device, precision=precision or
                               ("bf16" if device.startswith("cuda") else "fp32"))
        spec_info = {"model_id": spec.model_id, "revision": spec.revision, "spec": spec_key}
    parts = []
    for s in range(0, len(texts), batch_size):
        chunk = texts[s:s + batch_size]
        vec = encoder.encode_queries(chunk) if PROMPT == "query" else encoder.encode_docs(chunk)
        parts.append(np.asarray(vec, dtype=np.float32))
    mat = np.concatenate(parts, axis=0) if parts else np.zeros((0, 1), dtype=np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    mat = mat / np.where(norms > 0, norms, 1.0)
    dim = int(mat.shape[1]) if mat.size else 0
    vectors = pa.FixedSizeListArray.from_arrays(pa.array(mat.reshape(-1), pa.float32()), dim) if dim else \
        pa.array([], pa.list_(pa.float32()))
    out = table.select(["lang", "key", "text"]).append_column("vector", vectors)
    meta = {**spec_info, "prompt": PROMPT, "dimension": dim, "device": device, "rows": out.num_rows,
            "seconds": round(time.time() - t0, 1)}
    old = (table.schema.metadata or {}).get(METADATA_KEY)
    if old:
        meta["phrases"] = json.loads(old)
    return out.replace_schema_metadata({METADATA_KEY: json.dumps(meta).encode("utf-8")})


def info(table: pa.Table) -> dict[str, Any]:
    md = table.schema.metadata or {}
    return json.loads(md.get(METADATA_KEY, b"{}"))

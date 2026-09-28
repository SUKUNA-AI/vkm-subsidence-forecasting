"""Embeddings of canonical objects (Retrieval Lab, постановка лаборатории §37–46; owner: agent K).

* :mod:`specs` — encoder specifications (model, revision, tokenizer rules, pooling, heads) of the candidates;
* :mod:`tokenize` — token ids exactly as the reference libraries build them (the backend never tokenizes);
* :mod:`postprocess` — pooling, Matryoshka, normalisation, int8, ColBERT/BGE-M3 heads (numpy only);
* :mod:`signature` — embedding signature §37 and query signature §38;
* :mod:`artifacts` — derived Parquet artifacts dense/sparse/multivector/visual (§39–43) and the §64 checks;
* :mod:`reembed` — re-embed policy §46 (same content hash + same signature → no inference);
* :mod:`llama` — client of a pinned ``llama-server`` (token ids in, vectors out);
* :mod:`gpu` — AMD GPU facts from sysfs / DRM fdinfo (VRAM, GTT, busy, runtime PM);
* :mod:`worker` — embedding job worker interface for the coordinator's PostgreSQL queue;
* :mod:`parity` — RX580 vs reference parity metrics (§30);
* :mod:`bench` — RX580 measurement harness (model matrix §26, residency §27, concurrency §29);
* :mod:`reference` — reference encoder (HF / sentence-transformers / PyLate on WORKSTATION; torch imported lazily).

Heavy dependencies (numpy, pyarrow, tokenizers, torch) are imported by the modules that need them, never here.
"""

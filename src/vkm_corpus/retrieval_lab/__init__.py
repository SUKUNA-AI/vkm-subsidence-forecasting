"""VKM retrieval lab (agent J, CP-26): benchmark harness for choosing the retrieval architecture of the corpus.

Canonical objects of a published snapshot → embedding units (rule ``vkm-units-v1``) → BM25 / dense / learned sparse /
late interaction → fusion → optional late re-scoring → optional EDGE reranker → page-level and object-level metrics
against graded qrels (``benchmarks/retrieval_v0``). Design: ``docs/implementation_work/AGENT_J_RETRIEVAL_BENCHMARK_DESIGN.md``.

Heavy dependencies (torch, sentence-transformers, duckdb, opensearch-py) are imported lazily inside the functions
that need them; the pure parts (benchmark files, units from rows, fusion, metrics, local BM25) run with numpy only.
"""
from __future__ import annotations

LAB_VERSION = "0.1.0"

"""OpenSearch retrieval projection of one CANONICAL snapshot (decisions CP-17, H-04, H-13, H-18, H-19, H-30, H-44).

The index returns candidates — stable canonical IDs, BM25 scores, highlights (marked ``SEARCH_INDEX``) and metadata
for filters. It is never a source of object text: the API reads canonical text from DuckDB, and reranking reads the
view ``rerank_text`` of agent D by the passage references this package returns.

Modules: ``analysis`` (analyzers, protected terms), ``mappings`` (strict versioned index bodies, names, aliases),
``documents`` (projection input → documents, ``doc_stream_sha256``), ``indexer`` (build, checks, alias swap,
rollback, retention, status), ``query`` (filters, BM25, collapse, RRF, rerank candidates), ``smoke`` (Russian smoke of
acceptance §59), ``cli`` (``vkm-corpus search build|status|query|smoke|rollback``).

The canonical input layer (CANONICAL-root guard, snapshot manifest, D → E field mapping, projection rules) is shared
with the graph projection: ``vkm_corpus.graph.common``, ``vkm_corpus.graph.canon``, ``vkm_corpus.graph.rules``.
"""
from __future__ import annotations

__all__: list[str] = []

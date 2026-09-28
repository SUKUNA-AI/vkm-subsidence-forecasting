"""VKM API (FastAPI, ``/v1``) — the one semantic access layer to the corpus for agents and MCP (task §33, CP-19).

* :mod:`~vkm_corpus.api.envelope` — response envelope ``vkm.envelope/1`` and ``ApiResponse``;
* :mod:`~vkm_corpus.api.canon` — read-only DuckDB over the CANONICAL snapshot (the only module that knows D's views);
* :mod:`~vkm_corpus.api.backends` — OpenSearch/Neo4j (IDs only), rerank gateway, control plane, artifact bytes;
* :mod:`~vkm_corpus.api.service` — endpoint logic (hydration, envelopes, rerank limits, plan-first reprocess);
* :mod:`~vkm_corpus.api.app` — routes, bearer tokens, error bodies, request logs;
* :mod:`~vkm_corpus.api.fixtures` — synthetic canon and fake backends for tests and the local §60 dry run.

FastAPI, DuckDB and the other service dependencies are imported only by these submodules (extras ``corpus``,
``corpus-services``).
"""

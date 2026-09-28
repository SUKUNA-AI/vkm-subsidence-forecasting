"""Retrieval services of VKM Corpus Platform v0 on EDGE (agent F; decisions CP-18, H-13, H-41, H-45).

* ``models`` — the rerank contract ``vkm.rerank/1`` shared with the VKM API (agent G imports it);
* ``gateway`` — ``vkm-rerank-gateway`` (FastAPI): text → the existing jina-reranker-v3.5 service, visual → pinned
  llama-server with jina-reranker-m0 (Q6_K + mmproj Q8_0) and the MLP score head;
* ``client`` — async/sync client for the VKM API and the CLI;
* ``images``, ``tokens``, ``m0_head`` — image normalisation, token counting, score head;
* ``parity``, ``fixtures``, ``acceptance`` — the blocking parity gate, synthetic fixtures and the 9-point acceptance.

Rerank scores are a retrieval signal (layer SERVICE, review NOT_APPLICABLE), never evidence. Heavy dependencies
(FastAPI, httpx, numpy, Pillow, tokenizers, torch) are imported lazily by the modules that need them.
"""

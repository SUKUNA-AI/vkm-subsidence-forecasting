"""VKM RX580 retrieval service on CORE (постановка лаборатории §4, §49–56; owner: agent K).

One process (FastAPI) that keeps the production encoders **resident together** on the RX580 — as independent
``llama-server`` child processes (dense and late-interaction), each with its own Vulkan context — and serves:

``GET /health`` (every model: loaded, resident, VRAM), ``GET /model-info``, ``POST /embed/query``,
``POST /search/dense``, ``POST /search/hybrid``, ``POST /search/late``, ``GET /metrics``.

There is no global GPU lock, semaphore or single worker: requests to the two models run concurrently (the RADV compute
queues and the kernel scheduler interleave them). Vectors are searched through OpenSearch k-NN (a projection of the
derived artifacts); late interaction re-scores candidates with MaxSim over the stored token vectors. A keepalive keeps
the GPU out of runtime-PM suspend (BACO), which would otherwise evict the models from VRAM after 5 s idle.

Modules: :mod:`config`, :mod:`residency`, :mod:`encoders`, :mod:`search`, :mod:`metrics`, :mod:`app`, :mod:`cli`.
FastAPI, uvicorn, numpy, tokenizers and opensearch-py are imported lazily.
"""
SERVICE_NAME = "vkm-rx580-retrieval"
SERVICE_VERSION = "0.1.0"

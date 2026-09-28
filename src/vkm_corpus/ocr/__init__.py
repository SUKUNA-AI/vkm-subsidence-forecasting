"""GLM-OCR integration (agent C): a thin client of the local OpenAI-compatible server, raw-output records, normalisation
and quality/stop rules.

The ``glmocr`` SDK is not used at all (H-06: its default MaaS mode sends documents to a cloud API, its formatter
rewrites model output, it drops header/footer/number regions and sends JPEG). A test forbids ``import glmocr`` anywhere
under ``src/``. The client only talks to loopback or the compose service name.
"""

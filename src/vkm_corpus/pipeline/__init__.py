"""Document pipeline orchestration (agent C): planning, per-source isolation, GPU stages, commits.

Phases of ``vkm-corpus run extract`` (all idempotent, each writes its caches as it goes):

1. **prepare** (CPU, one subprocess per source): inspect → paginate (two methods) → native extraction → classify;
2. **render + layout** (GPU, one subprocess): 200-dpi render of every fixed-layout page, 1024-px preview, PP-DocLayoutV3
   raw detections (``LAYOUT_RAW``), cached by call signature – never re-run for a cached page;
3. **ocr** (async client): regions that need recognition → lossless PNG crops → GLM-OCR, one ``OCR_RAW`` per attempt,
   cached by call signature; CP-22 stop window; ``--max-model-calls``; scenario-B sample and decision;
4. **commit** (CPU, one subprocess per source): the source is rebuilt entirely from the stage caches (no model call),
   normalised, mapped to canonical rows (``extract.to_canon``) and committed through the canonical writer.
"""

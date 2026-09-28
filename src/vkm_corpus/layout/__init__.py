"""Page layout (agent C): PP-DocLayoutV3 through ``transformers`` on the GPU (WSL venv, offline).

Raw detections of every page are stored as a ``LAYOUT_RAW`` artifact (KEEP_RAW, H-03). Regions and the IDs of
detected objects are computed only from the stored raw detections (``vkm_corpus.layout.regions``); the model is never
re-run for a page whose layout stage signature is already cached. A CPU/no-model path exists only as an explicit
configuration with a different extractor id.
"""

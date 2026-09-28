"""Idempotency signatures (H-05, task §15) and hashing helpers shared by the pipeline and the canon.

Two signatures, one meaning each:

* ``stage_signature`` — decides whether the rows of a stage (page or source level) must be rebuilt. It contains the
  source sha256, the page, the stage, ``pipeline_version``, extractor id/version/generation, the models and the stage
  configuration hash plus the input artifact ids. Bumping ``pipeline_version`` rebuilds rows…
* ``call_signature`` — …but not model calls: the key of an expensive model call is
  ``sha256(model_id | model_revision | weights_sha256 | prompt | sampling | input_pixel_sha256)``. The backend and its
  version are recorded, never part of the key (an engine update does not silently re-run OCR). Raw outputs are
  content-addressed blobs indexed by ``producer_signature = call_signature`` and ``attempt``.

``--force`` has one meaning: rebuild rows, use the model cache. A new model call for an existing call_signature is a
separate, explicit ``--recall-model`` path through ``--plan-only`` (C, G).
"""
from __future__ import annotations

import hashlib
from typing import Any, Iterable, Mapping

from vkm_world.core.io import canonical_json, sha256_json

from vkm_corpus.ids import grammar


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def config_hash(config: Mapping[str, Any]) -> str:
    """sha256 of the canonical JSON of a configuration mapping (sorted keys, compact separators)."""
    return sha256_json(dict(config))


def _models_part(models: Iterable[Mapping[str, Any] | Any]) -> str:
    items = []
    for m in models:
        get = m.get if isinstance(m, Mapping) else (lambda k, _m=m: getattr(_m, k))
        items.append(f"{get('role')}={get('model_id')}@{get('model_revision')}")
    return ",".join(sorted(items)) or "-"


def stage_signature(*, source_sha256: str, stage: str, pipeline_version: str, extractor_id: str,
                    extractor_version: str, stage_config_hash: str, page_unit: str | None = None,
                    page_index: int | None = None, extraction_generation: int = 1,
                    models: Iterable[Mapping[str, Any] | Any] = (),
                    input_artifact_ids: Iterable[str] = ()) -> str:
    """Signature of a stage for one page (``page_unit``/``page_index``) or for the whole source (both ``None``)."""
    if not grammar.matches("sha256", source_sha256):
        raise ValueError("source_sha256 must be 64 lowercase hex")
    if not grammar.matches("sha256", stage_config_hash):
        raise ValueError("stage_config_hash must be 64 lowercase hex")
    inputs = sorted(set(input_artifact_ids))
    for a in inputs:
        if not grammar.matches("artifact", a):
            raise ValueError(f"input artifact ids must be sha256:<hex>: {a!r}")
    page = "-" if page_index is None else f"{page_unit}{int(page_index)}"
    parts = ["vkm-sig-v2", source_sha256, page, str(stage), pipeline_version, extractor_id, extractor_version,
             str(int(extraction_generation)), _models_part(models), stage_config_hash, ",".join(inputs)]
    return sha256_bytes("|".join(parts).encode("utf-8"))


def call_signature(*, model_id: str, model_revision: str, weights_sha256: str | None, prompt: str,
                   sampling: Mapping[str, Any], input_pixel_sha256: str) -> str:
    """Cache key of a model call (H-05). ``input_pixel_sha256`` is ``pixel_sha256`` of the decoded input image."""
    if not grammar.matches("sha256", input_pixel_sha256):
        raise ValueError("input_pixel_sha256 must be 64 lowercase hex")
    parts = ["vkm-call-v1", model_id, model_revision, weights_sha256 or "-", prompt, canonical_json(dict(sampling)),
             input_pixel_sha256]
    return sha256_bytes("\x1f".join(parts).encode("utf-8"))


def pixel_sha256(mode: str, width: int, height: int, buffer: bytes) -> str:
    """Hash of a decoded pixel buffer (e.g. Pillow ``image.mode``, ``image.size``, ``image.tobytes()``): independent
    of the PNG/WebP encoder, so a library update does not invalidate the model cache (H-05, H-23)."""
    h = hashlib.sha256(f"vkm-pixels-v1|{mode}|{int(width)}x{int(height)}|".encode("ascii"))
    h.update(buffer)
    return h.hexdigest()

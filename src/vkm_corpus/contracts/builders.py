"""Helpers to build valid canonical rows: envelope contexts, ``content_sha256``, validation.

The pipeline (agent C) and the synthetic canon use the same path:

    src = SourceContext.from_source_row(source_row)
    prod = ProducerContext(pipeline_version=..., processing_run_id=run, extractor_id="pdf-native",
                           extractor_version="1.28.2", config_hash=..., raw_config_hash=...)
    row = build_row("blocks", doc_envelope(src, prod, object_kind="BLOCK", object_id=oid, page_id=pid,
                                           origin="NATIVE", created_at=now), text=..., ...)

``build_row`` computes ``content_sha256`` over the non-envelope fields and validates the row with its model.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping

from pydantic import BaseModel

from vkm_corpus.contracts.arrow import canonical_value
from vkm_corpus.contracts.base import ModelRef
from vkm_corpus.contracts.datasets import dataset
from vkm_corpus.contracts.models import ENVELOPE_FIELDS
from vkm_corpus.contracts.vocab import EnvelopeProfile, ModelRole, ReviewStatus
from vkm_corpus.ids import object_id as make_object_id
from vkm_corpus.ids import producer_key


def content_sha256(dataset_name: str, row: Mapping[str, Any] | BaseModel) -> str:
    """sha256 of the canonical JSON of the non-envelope fields of a row (the row's *content*)."""
    spec = dataset(dataset_name)
    if spec.profile == EnvelopeProfile.LOG:
        raise ValueError(f"{dataset_name} rows have no content hash")
    data = row.model_dump(mode="python") if isinstance(row, BaseModel) else dict(row)
    env = ENVELOPE_FIELDS[spec.profile]
    payload = {k: canonical_value(data.get(k)) for k in spec.fields if k not in env}
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_row(dataset_name: str, envelope: Mapping[str, Any], /, **fields: Any) -> BaseModel:
    """Merge envelope + fields, compute ``content_sha256`` when absent and validate with the dataset model."""
    spec = dataset(dataset_name)
    data = {**envelope, **fields}
    data.setdefault("schema_version", spec.version)
    if spec.profile != EnvelopeProfile.LOG and "content_sha256" in spec.fields and not data.get("content_sha256"):
        data["content_sha256"] = content_sha256(dataset_name, data)
    return spec.model.model_validate(data)


@dataclass(frozen=True)
class SourceContext:
    """Source-level envelope values (copied from the ``sources`` row; checked by the validator)."""

    source_id: str
    source_sha256: str
    site_scope: tuple[str, ...]
    site_scope_raw: str
    site_scope_mapping: str

    @classmethod
    def from_source_row(cls, row: Mapping[str, Any] | BaseModel) -> "SourceContext":
        g = row.get if isinstance(row, Mapping) else (lambda k: getattr(row, k))
        return cls(g("source_id"), g("source_sha256"), tuple(g("site_scope")), g("site_scope_raw"),
                   str(g("site_scope_mapping")))


@dataclass(frozen=True)
class ProducerContext:
    """Producer-level envelope values of one extractor within one run."""

    pipeline_version: str
    processing_run_id: str
    extractor_id: str
    extractor_version: str
    config_hash: str
    raw_config_hash: str
    extraction_generation: int = 1
    models: tuple[ModelRef, ...] = field(default_factory=tuple)

    def with_models(self, *models: ModelRef | Mapping[str, Any]) -> "ProducerContext":
        refs = tuple(m if isinstance(m, ModelRef) else ModelRef.model_validate(m) for m in models)
        return ProducerContext(self.pipeline_version, self.processing_run_id, self.extractor_id,
                               self.extractor_version, self.config_hash, self.raw_config_hash,
                               self.extraction_generation, refs)

    @property
    def recognition(self) -> ModelRef | None:
        for m in self.models:
            if m.role == ModelRole.RECOGNITION:
                return m
        return None

    def producer_key(self) -> str:
        return producer_key(self.extractor_id, self.extraction_generation, self.raw_config_hash, self.models)


def doc_envelope(src: SourceContext, prod: ProducerContext, *, object_kind: str, object_id: str,
                 origin: str, created_at: datetime, page_id: str | None = None,
                 raw_artifact_id: str | None = None, raw_artifacts: Iterable[Mapping[str, Any]] = (),
                 raw_content_sha256: str | None = None, extraction_signature: str | None = None,
                 quality_flags: Iterable[str] = ()) -> dict[str, Any]:
    """Envelope dict of a document object (DOC profile)."""
    recog = prod.recognition
    return {
        "object_id": object_id, "object_kind": object_kind, "source_id": src.source_id, "page_id": page_id,
        "source_sha256": src.source_sha256, "source_site_scope": list(src.site_scope),
        "source_site_scope_raw": src.site_scope_raw, "source_site_scope_mapping": src.site_scope_mapping,
        "origin": origin, "pipeline_version": prod.pipeline_version, "processing_run_id": prod.processing_run_id,
        "extractor_id": prod.extractor_id, "extractor_version": prod.extractor_version,
        "extraction_generation": prod.extraction_generation,
        "model_id": recog.model_id if recog else None, "model_revision": recog.model_revision if recog else None,
        "models": [m.model_dump() for m in prod.models], "config_hash": prod.config_hash,
        "raw_config_hash": prod.raw_config_hash, "extraction_signature": extraction_signature,
        "raw_content_sha256": raw_content_sha256, "raw_artifact_id": raw_artifact_id,
        "raw_artifacts": [dict(r) for r in raw_artifacts], "created_at": created_at,
        "review_status": ReviewStatus.AUTO_EXTRACTED_UNREVIEWED.value, "quality_flags": list(quality_flags),
    }


def page_object_id(prod: ProducerContext, *, scope_id: str, object_kind: str, origin: str, region_origin: str,
                   anchor: str, dup: int = 0) -> str:
    """Object id for a producer context (H-14 producer key)."""
    return make_object_id(scope_id, object_kind, origin, region_origin, anchor, prod.producer_key(), dup)

"""Minimal content-addressed blob writer used by the canon itself (validation reports, registry run configs) and by
the synthetic canon. The pipeline's artifact store (agent C) uses the same layout: ``CanonLayout.blob_relpath``."""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from vkm_corpus.contracts.vocab import ARTIFACT_KIND_RETENTION
from vkm_corpus.parquet.atomic import sha256_of, write_bytes
from vkm_corpus.parquet.layout import CanonLayout


def put_blob(layout: CanonLayout, kind: str, data: bytes, media_type: str, *, run_id: str, created_at: datetime,
             source_id: str | None = None, page_id: str | None = None, **extra: Any) -> dict[str, Any]:
    """Store ``data`` once and return the ``artifacts`` index row (dict) describing it."""
    aid = "sha256:" + hashlib.sha256(data).hexdigest()
    rel = layout.blob_relpath(kind, aid, media_type)
    write_bytes(layout.tmp, layout.blob_path(rel), data)
    row = {"artifact_id": aid, "artifact_kind": kind, "media_type": media_type, "size_bytes": len(data),
           "storage_relpath": rel, "retention_class": ARTIFACT_KIND_RETENTION[kind], "materialization": "STORED",
           "created_by_run_id": run_id, "registered_source_id": source_id, "registered_page_id": page_id,
           "created_at": created_at}
    row.update(extra)
    return row


def check_blob(layout: CanonLayout, storage_relpath: str, artifact_id: str, deep: bool = True) -> str | None:
    """None if the blob exists (and, with ``deep``, hashes to its id); otherwise a short reason."""
    path = layout.blob_path(storage_relpath)
    if not path.is_file():
        return "missing"
    if deep and "sha256:" + sha256_of(path) != artifact_id:
        return "sha256 mismatch"
    return None

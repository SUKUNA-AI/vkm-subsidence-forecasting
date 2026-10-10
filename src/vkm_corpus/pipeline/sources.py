"""Register rows the pipeline needs (source id, path, sha256, size, lifecycle).

The lifecycle rule mirrors CP-05: ``ORIGINAL_ARCHIVE_DELETED_AFTER_PDF_ASSEMBLY`` → ABSENT_BY_REGISTER /
ARCHIVE_DELETED_AFTER_ASSEMBLY (013); retired rows (``LEGACY_RETIRED`` scope or ``RETIRED_FROM_CURRENT_RESEARCH``) →
RETIRED / RETIRED_NOT_EVIDENCE (022). Such sources are reported ``SKIPPED_BY_REGISTER``, never UNSUPPORTED, and are
never opened or unpacked. When the canonical registry module of agent D exposes the lifecycle, it is used instead.
"""
from __future__ import annotations

import csv
from pathlib import Path

from vkm_corpus.extract.model import SourceInput

REGISTER_RELPATH = "00_registry/SOURCE_REGISTER.csv"


def _lifecycle(row: dict[str, str]) -> tuple[str, str | None]:
    migration = (row.get("migration_status") or "").strip().upper()
    scope = (row.get("evidence_scope") or "").strip().upper()
    klass = (row.get("source_class") or "").strip().lower()
    if migration == "ORIGINAL_ARCHIVE_DELETED_AFTER_PDF_ASSEMBLY":
        return "ABSENT_BY_REGISTER", "ARCHIVE_DELETED_AFTER_ASSEMBLY"
    if scope == "LEGACY_RETIRED" or migration.startswith("RETIRED") or klass == "retired_legacy_archive":
        return "RETIRED", "RETIRED_NOT_EVIDENCE"
    return "ACTIVE", None


def load_sources(resources_root: Path) -> list[SourceInput]:
    from vkm_corpus.registry.sources import resource_path, RegisterError
    from vkm_corpus import ids

    path = resource_path(resources_root, REGISTER_RELPATH)
    out: list[SourceInput] = []
    seen = set()
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            sid = row["resource_id"].strip()
            if not ids.grammar.matches("source", sid):
                raise RegisterError("invalid source identity in pipeline register")
            if sid in seen:
                raise RegisterError("duplicate resource_id in pipeline register")
            seen.add(sid)
            resource_path(resources_root, row["canonical_path"].strip())
            lifecycle, reason = _lifecycle(row)
            try:
                size = int(row.get("size_bytes") or 0) or None
            except ValueError:
                size = None
            out.append(SourceInput(source_id=row["resource_id"].strip(), canonical_path=row["canonical_path"].strip(),
                                   sha256=(row.get("sha256") or "").strip().lower(), size_bytes=size,
                                   lifecycle=lifecycle, skip_reason=reason, register_notes=row.get("notes") or "",
                                   evidence_scope=(row.get("evidence_scope") or "").strip()))
    return out


def select(sources: list[SourceInput], ids: list[str] | None) -> list[SourceInput]:
    if not ids:
        return sources
    norm = set()
    for x in ids:
        x = x.strip()
        if not x:
            continue
        norm.add(x if x.startswith("VKM-SRC-") else f"VKM-SRC-{int(x):03d}")
    missing = norm - {s.source_id for s in sources}
    if missing:
        raise ValueError(f"unknown source ids: {sorted(missing)}")
    return [s for s in sources if s.source_id in norm]

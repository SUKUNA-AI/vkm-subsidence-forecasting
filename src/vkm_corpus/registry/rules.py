"""Pure rules of the registry import: lifecycle (CP-05), review status (CP-06), format by signature, row hashes."""
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Mapping

from vkm_corpus.contracts.vocab import (
    FileFormat,
    LifecycleStatus,
    QualityFlag,
    ReviewStatus,
    ReviewStatusBasis,
    SkipReason,
)

REGISTER_COLUMNS = ("resource_id", "canonical_path", "original_filename", "sha256", "size_bytes", "source_class",
                    "evidence_scope", "priority", "scientific_role", "migration_source", "migration_status", "notes")
QUICK_LOOK_MARKER = "INTAKE_QUICK_LOOK_NOT_EVIDENCE"
LFS_POINTER_PREFIX = b"version https://git-lfs"

# register values that take a source out of processing (CP-05)
LIFECYCLE_BY_MIGRATION_STATUS = {
    "ORIGINAL_ARCHIVE_DELETED_AFTER_PDF_ASSEMBLY": (LifecycleStatus.ABSENT_BY_REGISTER,
                                                    SkipReason.ARCHIVE_DELETED_AFTER_ASSEMBLY),
    "RETIRED_FROM_CURRENT_RESEARCH": (LifecycleStatus.RETIRED, SkipReason.RETIRED_NOT_EVIDENCE),
}
LIFECYCLE_BY_SCOPE = {"LEGACY_RETIRED": (LifecycleStatus.RETIRED, SkipReason.RETIRED_NOT_EVIDENCE)}

# Phase-1 coverage (CoverageLevel) → review status for 001–041 (CP-06)
COVERAGE_TO_REVIEW = {
    "FULLY_REVIEWED": ReviewStatus.FULLY_REVIEWED,
    "RELEVANT_SECTIONS_REVIEWED": ReviewStatus.RELEVANT_SECTIONS_REVIEWED,
}


def lifecycle_for(migration_status: str, evidence_scope: str) -> tuple[LifecycleStatus, SkipReason | None]:
    if migration_status in LIFECYCLE_BY_MIGRATION_STATUS:
        return LIFECYCLE_BY_MIGRATION_STATUS[migration_status]
    if evidence_scope in LIFECYCLE_BY_SCOPE:
        return LIFECYCLE_BY_SCOPE[evidence_scope]
    return LifecycleStatus.ACTIVE, None


class ReviewRuleError(ValueError):
    """The review rule of CP-06 cannot be applied (e.g. no Phase-1 coverage for a source 001–041)."""


def review_for(source_number: int, lifecycle: str, coverage_raw: str | None,
               notes: str) -> tuple[ReviewStatus, ReviewStatusBasis]:
    """CP-06: 013/022 (not ACTIVE) → NOT_APPLICABLE; 001–041 from Phase-1 coverage; 042–195 UNSEEN; 196–251
    QUICK_LOOK_ONLY (the quick-look marker must be present in the register notes)."""
    if lifecycle != LifecycleStatus.ACTIVE:
        return ReviewStatus.NOT_APPLICABLE, ReviewStatusBasis.LIFECYCLE
    if source_number <= 41:
        status = COVERAGE_TO_REVIEW.get(coverage_raw or "")
        if status is None:
            raise ReviewRuleError(f"source {source_number:03d}: Phase-1 coverage {coverage_raw!r} has no review "
                                  "mapping")
        return status, ReviewStatusBasis.PHASE1_COVERAGE_MASTER
    if source_number <= 195:
        return ReviewStatus.UNSEEN, ReviewStatusBasis.DEFAULT_UNSEEN
    if QUICK_LOOK_MARKER not in notes:
        raise ReviewRuleError(f"source {source_number:03d}: quick-look marker missing in the register notes")
    return ReviewStatus.QUICK_LOOK_ONLY, ReviewStatusBasis.INTAKE_QUICK_LOOK_MARKER


def detect_format(path: Path) -> tuple[FileFormat, list[str]]:
    """File format by signature (not by extension) with the flags it implies."""
    if not path.is_file():
        return FileFormat.UNKNOWN, []
    with open(path, "rb") as f:
        head = f.read(1024)
    flags: list[str] = []
    if head.startswith(LFS_POINTER_PREFIX):
        return FileFormat.UNKNOWN, []
    pos = head.find(b"%PDF")
    if pos >= 0:
        if pos > 0:
            flags.append(QualityFlag.LEADING_BYTES_BEFORE_HEADER.value)
        return FileFormat.PDF, flags
    if head.startswith(b"AT&TFORM"):
        return FileFormat.DJVU, flags
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(path) as z:
                names = set(z.namelist())
                if "mimetype" in names and z.read("mimetype").strip() == b"application/epub+zip":
                    return FileFormat.EPUB, flags
                if "[Content_Types].xml" in names and "word/document.xml" in names:
                    return FileFormat.DOCX, flags
        except zipfile.BadZipFile:
            return FileFormat.UNKNOWN, flags
        return FileFormat.ZIP, flags
    if head.startswith((b"\x89PNG", b"\xff\xd8\xff", b"II*\x00", b"MM\x00*")):
        return FileFormat.IMAGE, flags
    return FileFormat.UNKNOWN, flags


def is_lfs_pointer(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size > 1024:
        return False
    with open(path, "rb") as f:
        return f.read(len(LFS_POINTER_PREFIX)) == LFS_POINTER_PREFIX


def register_row_sha256(row: Mapping[str, str]) -> str:
    """sha256 of the canonical JSON of the raw register row (the 12 columns, verbatim)."""
    payload = json.dumps({k: row.get(k, "") for k in REGISTER_COLUMNS}, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def file_extension(canonical_path: str) -> str:
    return Path(canonical_path).suffix.lower().lstrip(".")

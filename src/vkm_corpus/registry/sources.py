"""``sources`` rows from ``SOURCE_REGISTER.csv`` (CP-05, CP-06, CP-07): one row per register entry, raw fields
verbatim, file presence and sha256 verified (in parallel, cached by (path, size, mtime) in the data root's cache)."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from vkm_corpus import ids
from vkm_corpus.contracts.builders import build_row
from vkm_corpus.contracts.site_scope import SITE_SCOPE_MAP_VERSION, map_site_scope
from vkm_corpus.contracts.vocab import FileFormat, FileStatus, LifecycleStatus, ProcessingStatus, QualityFlag
from vkm_corpus.parquet.atomic import sha256_of
from vkm_corpus.registry.rules import (
    REGISTER_COLUMNS,
    detect_format,
    file_extension,
    is_lfs_pointer,
    lifecycle_for,
    register_row_sha256,
    review_for,
)
from vkm_corpus.versions import PIPELINE_VERSION

REGISTER_REL = "00_registry/SOURCE_REGISTER.csv"
REGISTER_REF = "PRIVATE:" + REGISTER_REL
EXTRACTOR_ID = "registry-import"


class RegisterError(ValueError):
    """The register file does not have the expected structure."""


def load_register(path: Path) -> tuple[list[dict[str, str]], str]:
    """Rows of the register (utf-8-sig; values verbatim) and the sha256 of the file."""
    data = path.read_bytes()
    text = data.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if tuple(reader.fieldnames or ()) != REGISTER_COLUMNS:
        raise RegisterError(f"unexpected register columns: {reader.fieldnames}")
    rows = [dict(r) for r in reader]
    seen = set()
    for i, r in enumerate(rows, 1):
        if not ids.grammar.matches("source", r["resource_id"]):
            raise RegisterError(f"row {i}: bad resource_id {r['resource_id']!r}")
        if r["resource_id"] in seen:
            raise RegisterError(f"row {i}: duplicate resource_id {r['resource_id']}")
        seen.add(r["resource_id"])
    return rows, hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class FileCheck:
    status: FileStatus
    observed_sha256: str | None
    observed_size: int | None
    format: FileFormat
    flags: tuple[str, ...] = ()


@dataclass
class ShaCache:
    """sha256 cache keyed by (canonical_path, size, mtime_ns); lives in ``<data root>/cache`` (not canonical)."""

    path: Path | None
    entries: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def open(cls, path: Path | None) -> "ShaCache":
        if path is not None and path.is_file():
            return cls(path, json.loads(path.read_text(encoding="utf-8")))
        return cls(path, {})

    def get(self, rel: str, st: os.stat_result) -> str | None:
        e = self.entries.get(rel)
        if e and e["size"] == st.st_size and e["mtime_ns"] == st.st_mtime_ns:
            return e["sha256"]
        return None

    def put(self, rel: str, st: os.stat_result, sha: str) -> None:
        self.entries[rel] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": sha}

    def save(self) -> None:
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.entries, sort_keys=True, indent=0), encoding="utf-8")
            os.replace(tmp, self.path)


def check_file(resources_root: Path, row: Mapping[str, str], cache: ShaCache) -> FileCheck:
    path = resources_root / row["canonical_path"]
    if not path.is_file():
        return FileCheck(FileStatus.MISSING, None, None, FileFormat.UNKNOWN)
    if is_lfs_pointer(path):
        return FileCheck(FileStatus.LFS_POINTER_ONLY, None, path.stat().st_size, FileFormat.UNKNOWN)
    try:
        st = path.stat()
        sha = cache.get(row["canonical_path"], st)
        if sha is None:
            sha = sha256_of(path)
            cache.put(row["canonical_path"], st, sha)
        fmt, flags = detect_format(path)
    except OSError:
        return FileCheck(FileStatus.UNREADABLE, None, None, FileFormat.UNKNOWN)
    if st.st_size != int(row["size_bytes"]):
        status = FileStatus.SIZE_MISMATCH
    elif sha != row["sha256"]:
        status = FileStatus.SHA256_MISMATCH
    else:
        status = FileStatus.PRESENT_VERIFIED
    return FileCheck(status, sha, st.st_size, fmt, tuple(flags))


def verify_files(resources_root: Path, rows: Iterable[Mapping[str, str]], *, cache_path: Path | None = None,
                 workers: int = 8) -> dict[str, FileCheck]:
    """File checks of every register row, hashing in parallel; the cache is saved afterwards."""
    cache = ShaCache.open(cache_path)
    rows = list(rows)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(lambda r: check_file(resources_root, r, cache), rows))
    cache.save()
    return {r["resource_id"]: c for r, c in zip(rows, results)}


def build_source_rows(rows: list[dict[str, str]], checks: Mapping[str, FileCheck], *, run_id: str,
                      created_at: datetime, register_sha256: str, coverage: Mapping[str, str] | None = None,
                      ingestion: Mapping[str, tuple[date | None, str, str]] | None = None,
                      config_hash: str) -> list[Any]:
    """Validated SourceRow models (one per register row, same order)."""
    coverage = coverage or {}
    ingestion = ingestion or {}
    out = []
    for i, r in enumerate(rows, 1):
        sid = r["resource_id"]
        n = ids.source_number(sid)
        lifecycle, reason = lifecycle_for(r["migration_status"], r["evidence_scope"])
        review, basis = review_for(n, lifecycle, coverage.get(sid), r["notes"])
        scope = map_site_scope(r["evidence_scope"])
        chk = checks.get(sid) or FileCheck(FileStatus.MISSING, None, None, FileFormat.UNKNOWN)
        flags = list(chk.flags)
        if r["notes"].strip():
            flags.append(QualityFlag.REGISTER_NOTES_NOT_EVIDENCE.value)
        ing_date, ing_prec, ing_basis = ingestion.get(sid, (None, "unknown", "UNKNOWN"))
        env = {"object_id": sid, "object_kind": "SOURCE", "origin": "REGISTRY", "pipeline_version": PIPELINE_VERSION,
               "processing_run_id": run_id, "extractor_id": EXTRACTOR_ID, "extractor_version": PIPELINE_VERSION,
               "config_hash": config_hash, "created_at": created_at, "review_status": review.value,
               "quality_flags": sorted(set(flags)), "input_ref": REGISTER_REF, "input_sha256": register_sha256,
               "input_row": i}
        out.append(build_row(
            "sources", env, source_id=sid, source_sha256=r["sha256"], size_bytes=int(r["size_bytes"]),
            canonical_path=r["canonical_path"], original_filename=r["original_filename"],
            file_extension=file_extension(r["canonical_path"]), format_detected=chk.format.value,
            file_status=chk.status.value, observed_sha256=chk.observed_sha256, observed_size_bytes=chk.observed_size,
            lifecycle_status=lifecycle.value,
            register_skip_status=None if lifecycle == LifecycleStatus.ACTIVE else ProcessingStatus.SKIPPED_BY_REGISTER,
            register_skip_reason=reason.value if reason else None, source_class_raw=r["source_class"],
            priority=r["priority"], site_scope_raw=r["evidence_scope"], site_scope=list(scope.scopes),
            site_scope_candidates=list(scope.candidates), site_scope_mapping=scope.mapping.value,
            site_scope_map_version=SITE_SCOPE_MAP_VERSION, review_status_basis=basis.value,
            evidence_coverage_raw=coverage.get(sid), register_scientific_role=r["scientific_role"],
            register_migration_source=r["migration_source"], register_migration_status=r["migration_status"],
            register_notes=r["notes"], ingestion_date=ing_date, ingestion_date_precision=ing_prec,
            ingestion_basis=ing_basis, register_row_sha256=register_row_sha256(r)))
    return out


def load_phase1_coverage(path: Path) -> dict[str, str]:
    """``source_id → coverage_level`` of the Phase-1 coverage master (PUBLIC evidence/sources)."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        return {r["source_id"]: r["coverage_level"] for r in csv.DictReader(f)}

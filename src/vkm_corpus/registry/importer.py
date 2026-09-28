"""Registry import: SOURCE_REGISTER.csv + curated work registry → one REGISTRY commit (STAGING root).

    result = import_registry(layout, resources_root, work_registry_dir=..., coverage_path=...)

Steps: load the register (utf-8-sig), verify every file (parallel sha256 with cache), build 251 ``sources`` rows,
load the curated Work files, commit the eight registry datasets under the REGISTRY lease, journal the run (errors for
ACTIVE sources without a verified file; SKIPPED_BY_REGISTER steps for 013/022).
"""
from __future__ import annotations

import csv
import io
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from vkm_corpus import ids
from vkm_corpus.contracts.signatures import config_hash as make_config_hash
from vkm_corpus.contracts.vocab import ErrorCode, FileStatus, RunKind, RunStatus
from vkm_corpus.parquet.commits import REGISTRY_KEY, acquire_lease, commit_registry
from vkm_corpus.parquet.layout import CanonLayout
from vkm_corpus.parquet.runs import RunRecorder
from vkm_corpus.registry.sources import (
    REGISTER_REL,
    build_source_rows,
    load_phase1_coverage,
    load_register,
    verify_files,
)
from vkm_corpus.registry.works import WORK_REGISTRY_REL, load_work_registry
from vkm_corpus.versions import PIPELINE_VERSION

FILE_STATUS_ERROR = {
    FileStatus.MISSING: ErrorCode.SOURCE_FILE_MISSING, FileStatus.SHA256_MISMATCH: ErrorCode.SOURCE_SHA256_MISMATCH,
    FileStatus.SIZE_MISMATCH: ErrorCode.SOURCE_SIZE_MISMATCH, FileStatus.LFS_POINTER_ONLY: ErrorCode.SOURCE_LFS_POINTER,
    FileStatus.UNREADABLE: ErrorCode.SOURCE_UNREADABLE,
}
_DATE = re.compile(r"(20[0-9]{2}-[0-9]{2}-[0-9]{2})")


def work_registry_dir(resources_root: Path | None) -> Path:
    """``$VKM_WORK_REGISTRY_DIR`` (tests) or ``$VKM_RESOURCES_ROOT/00_registry/work_registry``."""
    env = os.environ.get("VKM_WORK_REGISTRY_DIR", "").strip()
    if env:
        return Path(env)
    if resources_root is None:
        raise ValueError("no work registry directory: set VKM_RESOURCES_ROOT or VKM_WORK_REGISTRY_DIR")
    return resources_root / WORK_REGISTRY_REL


def ingestion_dates(resources_root: Path, rows: list[dict[str, str]]) -> dict[str, tuple[date | None, str, str]]:
    """Day a file entered the project: intake manifest (dated folder) first, else a date in ``migration_source``."""
    out: dict[str, tuple[date | None, str, str]] = {}
    intake = resources_root / "00_registry" / "intake"
    if intake.is_dir():
        for d in sorted(intake.iterdir()):
            m = _DATE.match(d.name)
            mf = d / "IMPORT_MANIFEST.csv"
            if not (m and mf.is_file()):
                continue
            with open(mf, encoding="utf-8-sig", newline="") as f:
                for r in csv.DictReader(f):
                    sid = (r.get("resource_id") or r.get("source_id") or "").strip()
                    if ids.grammar.matches("source", sid) and sid not in out:
                        out[sid] = (date.fromisoformat(m.group(1)), "day", "INTAKE_MANIFEST")
    for r in rows:
        sid = r["resource_id"]
        if sid in out:
            continue
        m = _DATE.search(r["migration_source"])
        out[sid] = (date.fromisoformat(m.group(1)), "day", "MIGRATION_SOURCE_TEXT") if m else (None, "unknown",
                                                                                            "UNKNOWN")
    return out


def import_registry(layout: CanonLayout, resources_root: Path, *, work_registry: Path | None = None,
                    coverage_path: Path | None = None, host_role: str = "WORKSTATION", code_revision: str = "unknown",
                    private_revision: str | None = None, workers: int = 8, now: datetime | None = None,
                    cli_command: str = "vkm-corpus registry import") -> dict[str, Any]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    rows, reg_sha = load_register(resources_root / REGISTER_REL)
    checks = verify_files(resources_root, rows, cache_path=layout.cache / "source_sha256_cache.json",
                          workers=workers)
    coverage = load_phase1_coverage(coverage_path) if coverage_path else {}
    config = {"register": "PRIVATE:" + REGISTER_REL, "work_registry": "PRIVATE:" + WORK_REGISTRY_REL,
              "coverage": "PUBLIC:evidence/sources/SOURCE_COVERAGE_MASTER.csv" if coverage_path else None,
              "site_scope_map_version": "1"}
    chash = make_config_hash(config)
    wdir = work_registry or work_registry_dir(resources_root)
    run = RunRecorder(layout, run_kind=RunKind.REGISTRY_IMPORT, cli_command=cli_command, host_role=host_role,
                      config=config, code_revision=code_revision,
                      extra={"source_register_sha256": reg_sha, "private_registry_revision": private_revision})
    run.start(now)
    sources = build_source_rows(rows, checks, run_id=run.run_id, created_at=now, register_sha256=reg_sha,
                                coverage=coverage, ingestion=ingestion_dates(resources_root, rows), config_hash=chash)
    wr = load_work_registry(wdir, run_id=run.run_id, created_at=now, config_hash=chash,
                            known_sources={r["resource_id"] for r in rows})
    tables = {"sources": sources, "works": wr.works, "source_work_links": wr.source_work_links,
              "work_relations": wr.work_relations, "source_relations": wr.source_relations, "authors": wr.authors,
              "work_authors": wr.work_authors, "venues": wr.venues}
    with acquire_lease(layout, REGISTRY_KEY, run.run_id, host_role):
        res = commit_registry(layout, run_id=run.run_id, tables=tables,
                              inputs={"source_register_sha256": reg_sha, **wr.inputs}, host_role=host_role,
                              code_revision=code_revision, committed_at=now)
    run.note_commit(res.commit_id)
    steps, errors = [], []
    for s in sources:
        chk = checks[s.source_id]
        step_base = {"schema_version": "0.1.0", "processing_run_id": run.run_id, "source_id": s.source_id,
                     "stage": "REGISTRY", "attempt": 1, "source_sha256": s.source_sha256,
                     "pipeline_version": PIPELINE_VERSION, "extractor_id": "registry-import",
                     "extractor_version": PIPELINE_VERSION, "config_hash": chash, "started_at": now,
                     "finished_at": now, "duration_ms": 0, "host_role": host_role,
                     "stage_signature": make_config_hash({"source": s.source_id, "sha": s.source_sha256})}
        sid_step = ids.step_id(run.run_id, s.source_id, None, "REGISTRY", 1)
        if s.lifecycle_status != "ACTIVE":
            steps.append({**step_base, "step_id": sid_step, "outcome": "SKIPPED_BY_POLICY",
                          "status": "SKIPPED_BY_REGISTER", "reason_code": s.register_skip_reason})
        elif chk.status != FileStatus.PRESENT_VERIFIED:
            code = FILE_STATUS_ERROR[chk.status]
            steps.append({**step_base, "step_id": sid_step, "outcome": "EXECUTED", "status": "FAILED",
                          "reason_code": code.value})
            errors.append({"schema_version": "0.1.0", "error_id": ids.error_id(run.run_id, sid_step, code, 1),
                           "processing_run_id": run.run_id, "step_id": sid_step, "source_id": s.source_id,
                           "stage": "REGISTRY", "code": code.value, "tool": "registry-import",
                           "tool_version": PIPELINE_VERSION, "message": f"file check: {chk.status.value}",
                           "retryable": chk.status == FileStatus.LFS_POINTER_ONLY, "severity": "ERROR",
                           "created_at": now})
    if steps:
        run.add_steps(steps)
    if errors:
        run.add_errors(errors)
    status = RunStatus.PARTIAL if errors else RunStatus.SUCCEEDED
    run.end(status, now=now, counters={"n_sources_planned": len(rows), "n_steps_executed": len(steps),
                                       "n_steps_failed": len(errors)})
    counts = {}
    for s in sources:
        counts[s.file_status] = counts.get(s.file_status, 0) + 1
    return {"run_id": run.run_id, "commit_id": res.commit_id, "noop": res.noop, "sources": len(sources),
            "file_status": counts, "works": len(wr.works), "source_work_links": len(wr.source_work_links),
            "errors": len(errors), "register_sha256": reg_sha, **wr.inputs}


def read_csv_text(text: str) -> list[dict[str, str]]:
    return [dict(r) for r in csv.DictReader(io.StringIO(text, newline=""))]

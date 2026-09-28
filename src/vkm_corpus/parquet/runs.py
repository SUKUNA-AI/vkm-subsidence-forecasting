"""Run markers and run journals (H-11): every run is visible in the canon, committed or not.

A run writes ``processing_runs/run=<RUN>/start.parquet`` and ``_runs/run=<RUN>/START.json`` at start, log parts
(``processing_steps``, ``errors``, run-level ``artifacts``) while it works, and ``end.parquet`` + ``END.json`` listing
every file at the end. A snapshot includes the files of *all* runs: a START without END is a visible crash, and the
parts written before the crash are discovered in the run's own directories (immutable, atomically written files).
"""
from __future__ import annotations

import json
import platform
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from vkm_corpus import ids
from vkm_corpus.contracts.signatures import config_hash as make_config_hash
from vkm_corpus.contracts.vocab import ArtifactKind, RecordPhase, RunStatus
from vkm_corpus.parquet.atomic import write_json
from vkm_corpus.parquet.blobs import put_blob
from vkm_corpus.parquet.layout import CanonLayout
from vkm_corpus.parquet.writer import FileEntry, write_partition
from vkm_corpus.versions import PIPELINE_VERSION

RUN_FORMAT = "1"
RUN_LOG_DATASETS = ("processing_steps", "errors", "artifacts")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass
class RunRecorder:
    """Writes the START/END records and markers of one processing run and its log parts."""

    layout: CanonLayout
    run_kind: str
    cli_command: str
    host_role: str
    config: dict[str, Any] = field(default_factory=dict)
    run_id: str | None = None
    code_revision: str = "unknown"
    code_dirty: bool = False
    pipeline_version: str = PIPELINE_VERSION
    models: list[dict[str, Any]] = field(default_factory=list)
    cli_flags: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    started_at: datetime | None = None
    files: list[FileEntry] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)
    _base: dict[str, Any] = field(default_factory=dict)

    def start(self, now: datetime | None = None) -> "RunRecorder":
        self.started_at = now or _utcnow()
        self.run_id = self.run_id or ids.new_run_id(self.started_at)
        self.layout.ensure_dirs()
        cfg_text = json.dumps(self.config, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
        cfg_row = put_blob(self.layout, ArtifactKind.RUN_CONFIG, cfg_text.encode("utf-8"), "application/json",
                           run_id=self.run_id, created_at=self.started_at)
        self._base = {
            "schema_version": "0.1.0", "processing_run_id": self.run_id, "run_kind": self.run_kind,
            "started_at": self.started_at, "cli_command": self.cli_command, "cli_flags": list(self.cli_flags),
            "pipeline_version": self.pipeline_version, "code_revision": self.code_revision,
            "code_dirty": self.code_dirty, "host_role": self.host_role,
            "python_version": platform.python_version(), "platform": sys.platform,
            "config_hash": make_config_hash(json.loads(cfg_text)), "config_artifact_id": cfg_row["artifact_id"],
            "models": list(self.models), **self.extra,
        }
        start_row = {**self._base, "record_phase": RecordPhase.START, "status": RunStatus.STARTED,
                     "finished_at": None, "created_at": self.started_at}
        entry = write_partition(self.layout, "processing_runs", [start_row], run_id=self.run_id,
                                file_name="start.parquet")
        art = write_partition(self.layout, "artifacts", [cfg_row], run_id=self.run_id, scope="RUN")
        self.files += [entry, art]
        write_json(self.layout.tmp, self.layout.path(self.layout.run_marker(self.run_id, "START")), {
            "run_format": RUN_FORMAT, "processing_run_id": self.run_id, "run_kind": self.run_kind,
            "started_at": _iso(self.started_at), "host_role": self.host_role,
            "files": [f.to_json() for f in self.files]})
        return self

    # ------------------------------------------------------------ journals
    def add_steps(self, rows: Iterable[Any]) -> FileEntry:
        return self._add("processing_steps", rows)

    def add_errors(self, rows: Iterable[Any]) -> FileEntry:
        return self._add("errors", rows)

    def add_artifacts(self, rows: Iterable[Any]) -> FileEntry:
        return self._add("artifacts", rows, scope="RUN")

    def _add(self, name: str, rows: Iterable[Any], scope: str | None = None) -> FileEntry:
        if self.run_id is None:
            raise RuntimeError("run not started")
        entry = write_partition(self.layout, name, list(rows), run_id=self.run_id, scope=scope)
        self.files.append(entry)
        return entry

    def note_commit(self, commit_id: str | None) -> None:
        if commit_id:
            self.commits.append(commit_id)

    def end(self, status: str = RunStatus.SUCCEEDED, *, now: datetime | None = None, log_bytes: bytes | None = None,
            counters: dict[str, int] | None = None) -> str:
        """Write END (status, counters, RUN_LOG artifact) and the END marker listing every file of the run."""
        finished = now or _utcnow()
        log_id = None
        if log_bytes is not None:
            log_row = put_blob(self.layout, ArtifactKind.RUN_LOG, log_bytes, "application/x-ndjson",
                               run_id=self.run_id, created_at=finished)
            log_id = log_row["artifact_id"]
            self.add_artifacts([log_row])
        end_row = {**self._base, **(counters or {}), "record_phase": RecordPhase.END, "status": status,
                   "finished_at": finished, "log_artifact_id": log_id, "created_at": finished}
        entry = write_partition(self.layout, "processing_runs", [end_row], run_id=self.run_id,
                                file_name="end.parquet")
        self.files.append(entry)
        write_json(self.layout.tmp, self.layout.path(self.layout.run_marker(self.run_id, "END")), {
            "run_format": RUN_FORMAT, "processing_run_id": self.run_id, "status": str(status),
            "finished_at": _iso(finished), "files": [f.to_json() for f in self.files],
            "commits": sorted(set(self.commits))})
        return self.run_id


@dataclass(frozen=True)
class RunInfo:
    run_id: str
    start: dict[str, Any] | None
    end: dict[str, Any] | None

    @property
    def crashed(self) -> bool:
        return self.start is not None and self.end is None


def list_runs(layout: CanonLayout) -> dict[str, RunInfo]:
    out: dict[str, RunInfo] = {}
    base = layout.canonical / "_runs"
    if not base.is_dir():
        return out
    for d in sorted(base.glob("run=*")):
        rid = d.name[4:]
        start = json.loads((d / "START.json").read_text(encoding="utf-8")) if (d / "START.json").is_file() else None
        end = json.loads((d / "END.json").read_text(encoding="utf-8")) if (d / "END.json").is_file() else None
        out[rid] = RunInfo(rid, start, end)
    return out


def run_files(layout: CanonLayout, info: RunInfo) -> list[FileEntry]:
    """Files of a run: the END list, or START + parts discovered in the run's journal directories (crash)."""
    if info.end is not None:
        return [FileEntry.from_json(f) for f in info.end["files"]]
    files = [FileEntry.from_json(f) for f in (info.start or {}).get("files", [])]
    known = {f.path for f in files}
    for name in RUN_LOG_DATASETS:
        if name == "artifacts":
            d = layout.canonical / "artifacts" / f"run={info.run_id}" / "scope=RUN"
        else:
            d = layout.canonical / name / f"run={info.run_id}"
        if not d.is_dir():
            continue
        for p in sorted(d.glob("part-*.parquet")):
            rel = layout.rel(p)
            if rel not in known:
                files.append(describe_file(layout, name, rel))
    return files


def describe_file(layout: CanonLayout, name: str, rel: str) -> FileEntry:
    """FileEntry of an existing partition (reads it: sha256, rows, digests)."""
    import pyarrow.parquet as pq

    from vkm_corpus.contracts import arrow as ca
    from vkm_corpus.contracts.datasets import VOLATILE_COLUMNS, dataset
    from vkm_corpus.parquet.atomic import sha256_of

    path = layout.path(rel)
    table = pq.read_table(path)
    return FileEntry(dataset=name, path=rel, sha256=sha256_of(path), bytes=path.stat().st_size, rows=table.num_rows,
                     schema_version=dataset(name).version, schema_fingerprint=ca.schema_fingerprint(name),
                     digest=ca.digest_table(name, table).hex(),
                     content_digest=ca.digest_table(name, table, VOLATILE_COLUMNS).hex())


def run_marker_path(layout: CanonLayout, run_id: str, phase: str) -> Path:
    return layout.path(layout.run_marker(run_id, phase))

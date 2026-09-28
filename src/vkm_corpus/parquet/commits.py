"""Commits of a source (or of the registry) and per-key leases (H-08, H-34, H §5 п. 1).

A commit always rebuilds the whole source from stage caches: every document dataset gets a new partition file (empty
datasets included), plus the artifact index of every artifact the rows reference. The JSON marker
``_commits/run=<RUN>/<KEY>__<CMT>.json`` is the commit point; it names its ``parent_commit_id`` — the head of the key
at commit time. Order is defined by the parent chain only (wall-clock time is informational): a commit whose parent
is not the current head is a CONFLICT, never resolved automatically. A commit whose content fingerprints equal the
parent's is a no-op and is not written.
"""
from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from vkm_corpus import ids
from vkm_corpus.contracts import arrow as ca
from vkm_corpus.contracts.datasets import DOCUMENT_DATASETS, REGISTRY_DATASETS, VOLATILE_COLUMNS
from vkm_corpus.contracts.vocab import CommitScope, RootKind
from vkm_corpus.parquet.atomic import create_exclusive, dump_json, write_json
from vkm_corpus.parquet.layout import CanonLayout
from vkm_corpus.parquet.writer import FileEntry, next_part, to_table, write_table_file
from vkm_corpus.versions import PIPELINE_VERSION

COMMIT_FORMAT = "1"
REGISTRY_KEY = "REGISTRY"
AUTO = object()


class CommitConflict(RuntimeError):
    """The expected parent is not the current head of the key (COMMIT_CONFLICT)."""


class LeaseError(RuntimeError):
    """The key is leased by another writer, or the writer holds no lease."""


# ================================================================= leases (STAGING)
@dataclass(frozen=True)
class Lease:
    layout: CanonLayout
    key: str
    run_id: str

    @property
    def path(self) -> Path:
        return self.layout.path(self.layout.lease(self.key))

    def release(self) -> None:
        holder = read_lease(self.layout, self.key)
        if holder and holder.get("run_id") == self.run_id:
            self.path.unlink(missing_ok=True)

    def __enter__(self) -> "Lease":
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def acquire_lease(layout: CanonLayout, key: str, run_id: str, host_role: str = "WORKSTATION") -> Lease:
    body = {"key": key, "run_id": run_id, "pid": os.getpid(), "host": socket.gethostname(), "host_role": host_role,
            "acquired_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if not create_exclusive(layout.path(layout.lease(key)), dump_json(body)):
        holder = read_lease(layout, key) or {}
        if holder.get("run_id") == run_id:
            return Lease(layout, key, run_id)
        raise LeaseError(f"{key} is leased by run {holder.get('run_id')} (break it explicitly if that run is dead)")
    return Lease(layout, key, run_id)


def read_lease(layout: CanonLayout, key: str) -> dict[str, Any] | None:
    p = layout.path(layout.lease(key))
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def break_lease(layout: CanonLayout, key: str) -> dict[str, Any] | None:
    holder = read_lease(layout, key)
    layout.path(layout.lease(key)).unlink(missing_ok=True)
    return holder


# ================================================================= markers
def list_markers(layout: CanonLayout) -> list[dict[str, Any]]:
    """All commit markers of the root (parsed), with ``_path`` relative to canonical/."""
    out = []
    base = layout.canonical / "_commits"
    if not base.is_dir():
        return out
    for p in sorted(base.glob("run=*/*.json")):
        data = json.loads(p.read_text(encoding="utf-8"))
        data["_path"] = layout.rel(p)
        out.append(data)
    return out


def chain_head(markers: Iterable[Mapping[str, Any]], key: str,
               allowed: set[str] | None = None) -> tuple[str | None, list[str]]:
    """Head of the parent chain of ``key`` among ``markers`` (optionally only commit ids in ``allowed``).

    Returns ``(head, forks)``; ``forks`` lists commits that share a parent with another commit (conflicts)."""
    mine = [m for m in markers if m["key"] == key and (allowed is None or m["commit_id"] in allowed)]
    by_parent: dict[str | None, list[str]] = {}
    for m in mine:
        by_parent.setdefault(m.get("parent_commit_id"), []).append(m["commit_id"])
    forks = sorted(c for cs in by_parent.values() if len(cs) > 1 for c in cs)
    head, seen = None, set()
    cur = by_parent.get(None, [])
    while len(cur) == 1 and cur[0] not in seen:
        head = cur[0]
        seen.add(head)
        cur = by_parent.get(head, [])
    return head, forks


def local_head(layout: CanonLayout, key: str) -> str | None:
    head, forks = chain_head(list_markers(layout), key)
    if forks:
        raise CommitConflict(f"{key}: forked commit chain {forks}")
    return head


def marker_by_id(layout: CanonLayout, commit_id: str) -> dict[str, Any] | None:
    for m in list_markers(layout):
        if m["commit_id"] == commit_id:
            return m
    return None


# ================================================================= commit writer
@dataclass(frozen=True)
class CommitResult:
    key: str
    commit_id: str
    marker_path: str | None
    parent_commit_id: str | None
    noop: bool
    datasets: dict[str, FileEntry]


def _digests_equal(parent: Mapping[str, Any], digests: Mapping[str, tuple[int, str]]) -> bool:
    pds = parent.get("datasets", {})
    if set(pds) != set(digests):
        return False
    return all(int(pds[n]["rows"]) == rows and pds[n]["content_digest"] == cd for n, (rows, cd) in digests.items())


def _commit(layout: CanonLayout, *, scope: CommitScope, key: str, run_id: str, tables: Mapping[str, Any],
            required: tuple[str, ...], artifact_rows: Iterable[Any], parent_commit_id: Any, extra: dict[str, Any],
            host_role: str, code_revision: str, committed_at: datetime | None, require_lease: bool) -> CommitResult:
    kind = layout.kind()
    if kind == RootKind.STAGING and require_lease:
        holder = read_lease(layout, key)
        if not holder or holder.get("run_id") != run_id:
            raise LeaseError(f"run {run_id} holds no lease on {key}")
    missing = [n for n in required if n not in tables]
    unknown = [n for n in tables if n not in required]
    if missing or unknown:
        raise ValueError(f"a {scope.value} commit lists every dataset of its scope: missing {missing}, "
                         f"unexpected {unknown}")
    head = local_head(layout, key)
    parent = head if parent_commit_id is AUTO else parent_commit_id
    if parent != head:
        raise CommitConflict(f"{key}: expected parent {parent}, current head {head}")
    built = {n: to_table(n, tables[n]) for n in required}
    source_id = extra.get("source_id")
    if scope == CommitScope.SOURCE:
        for n, t in built.items():
            if t.num_rows:
                sids = set(t.column("source_id").to_pylist())
                shas = set(t.column("source_sha256").to_pylist())
                if sids != {source_id} or shas != {extra["source_sha256"]}:
                    raise ValueError(f"{n}: rows of another source or another source_sha256 in a commit of {key}")
    digests = {n: (t.num_rows, ca.digest_table(n, t, VOLATILE_COLUMNS).hex()) for n, t in built.items()}
    if parent is not None:
        pm = marker_by_id(layout, parent)
        if pm is not None and _digests_equal(pm, digests):
            return CommitResult(key, parent, pm["_path"], pm.get("parent_commit_id"), True,
                                {n: FileEntry.from_json(e) for n, e in pm["datasets"].items()})
    entries: dict[str, FileEntry] = {}
    for n, t in built.items():
        part = next_part(layout, n, run_id, source_id, None)
        rel = layout.partition(n, run_id, source_id, part)
        entries[n] = write_table_file(layout, n, t, rel, run_id=run_id, source_id=source_id)
    art_rows = list(artifact_rows)
    art_scope = None if source_id else "REGISTRY"
    art_rel = layout.partition("artifacts", run_id, source_id, next_part(layout, "artifacts", run_id, source_id,
                                                                           art_scope), art_scope)
    art_entry = write_table_file(layout, "artifacts", art_rows, art_rel, run_id=run_id, source_id=source_id)
    art_table = to_table("artifacts", art_rows)
    blobs = sorted({(r["artifact_id"], r["storage_relpath"], r["size_bytes"])
                    for r in art_table.select(["artifact_id", "storage_relpath", "size_bytes",
                                               "materialization"]).to_pylist()
                    if r["materialization"] == "STORED"})
    body = {
        "commit_format": COMMIT_FORMAT, "scope": scope.value, "key": key, "processing_run_id": run_id,
        "parent_commit_id": parent, "pipeline_version": PIPELINE_VERSION, "code_revision": code_revision,
        "host_role": host_role,
        "committed_at": (committed_at or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"),
        "datasets": {n: e.to_json() for n, e in sorted(entries.items())},
        "artifact_index_files": [art_entry.to_json()],
        "artifact_blobs": [{"artifact_id": a, "storage_relpath": p, "bytes": b} for a, p, b in blobs],
        **extra,
    }
    cid = ids.commit_id(body)
    body["commit_id"] = cid
    rel = layout.commit_marker(run_id, key, cid)
    write_json(layout.tmp, layout.path(rel), body)
    return CommitResult(key, cid, rel, parent, False, entries)


def commit_source(layout: CanonLayout, *, source_id: str, source_sha256: str, run_id: str,
                  tables: Mapping[str, Any], artifact_rows: Iterable[Any] = (), parent_commit_id: Any = AUTO,
                  document_processing_status: str | None = None, page_count: int | None = None,
                  host_role: str = "WORKSTATION", code_revision: str = "unknown",
                  committed_at: datetime | None = None, require_lease: bool = True) -> CommitResult:
    """Commit the complete rebuilt state of one source (all document datasets, possibly empty)."""
    extra = {"source_id": source_id, "source_sha256": source_sha256,
             "document_processing_status": document_processing_status, "page_count": page_count}
    return _commit(layout, scope=CommitScope.SOURCE, key=source_id, run_id=run_id, tables=tables,
                   required=DOCUMENT_DATASETS, artifact_rows=artifact_rows, parent_commit_id=parent_commit_id,
                   extra=extra, host_role=host_role, code_revision=code_revision, committed_at=committed_at,
                   require_lease=require_lease)


def commit_registry(layout: CanonLayout, *, run_id: str, tables: Mapping[str, Any], inputs: Mapping[str, Any],
                    artifact_rows: Iterable[Any] = (), parent_commit_id: Any = AUTO, host_role: str = "WORKSTATION",
                    code_revision: str = "unknown", committed_at: datetime | None = None,
                    require_lease: bool = True) -> CommitResult:
    """Commit the registry datasets (sources, works, links, authors, venues) built from PRIVATE 00_registry."""
    extra = {"source_id": None, "source_sha256": None, "inputs": dict(sorted(inputs.items()))}
    return _commit(layout, scope=CommitScope.REGISTRY, key=REGISTRY_KEY, run_id=run_id, tables=tables,
                   required=REGISTRY_DATASETS, artifact_rows=artifact_rows, parent_commit_id=parent_commit_id,
                   extra=extra, host_role=host_role, code_revision=code_revision, committed_at=committed_at,
                   require_lease=require_lease)

"""Layout of a data root and its root marker (H-07, H-32).

    <root>/.vkm_root.json                      root marker: {"root_kind": "STAGING" | "CANONICAL", ...}
    <root>/canonical/                          Parquet partitions and JSON markers (paths below are relative to it)
        <ds>/run=<RUN>/part-00000.parquet                        REGISTRY_GLOBAL (one registry commit)
        <ds>/source_id=<SID>/run=<RUN>/part-NNNNN.parquet        HEAD_PER_SOURCE
        processing_runs/run=<RUN>/{start,end}.parquet            APPEND_LOG
        processing_steps|errors/run=<RUN>/part-NNNNN.parquet
        artifacts/run=<RUN>/source_id=<SID>|scope=REGISTRY|scope=RUN/part-NNNNN.parquet   artifact index
        _commits/run=<RUN>/<KEY>__<CMT>.json    commit markers (KEY = VKM-SRC-NNN or REGISTRY)
        _runs/run=<RUN>/START.json, END.json    run markers (H-11)
        _leases/<KEY>.lease                     per-source lease of a writer (STAGING, H-08)
        _admission/<CMT>.json                   admission records (CANONICAL, H-09)
        _snapshots/<snapshot_id>.json, candidates/, CURRENT       CANONICAL only
        .lock                                   single writer of admission/snapshots (CANONICAL)
    <root>/artifacts/<kind_dir>/<hh>/<hh>/<sha256>.<ext>          content-addressed blobs
    <root>/duckdb/vkm_corpus.duckdb            rebuildable projection (CANONICAL only)
    <root>/tmp/                                 temporary files (same file system as canonical/)
    <root>/cache/                               non-canonical caches (e.g. source sha256 cache)

IDs never become file names: partitions use ``key=value`` without ``:``; blobs are named by their hash.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from vkm_corpus.contracts.vocab import ARTIFACT_KIND_DIR, MEDIA_TYPE_EXT, RootKind
from vkm_corpus.ids import grammar

LAYOUT_VERSION = "1"
ROOT_MARKER = ".vkm_root.json"
DATA_ROLE_KIND = {"producer": RootKind.STAGING, "canonical": RootKind.CANONICAL}


class RootError(RuntimeError):
    """The data root is missing its marker, or an operation is not allowed on its kind (H-07)."""


@dataclass(frozen=True)
class CanonLayout:
    root: Path

    # ------------------------------------------------------------ top-level directories
    @property
    def marker(self) -> Path:
        return self.root / ROOT_MARKER

    @property
    def canonical(self) -> Path:
        return self.root / "canonical"

    @property
    def artifacts(self) -> Path:
        return self.root / "artifacts"

    @property
    def duckdb_dir(self) -> Path:
        return self.root / "duckdb"

    @property
    def duckdb_file(self) -> Path:
        return self.duckdb_dir / "vkm_corpus.duckdb"

    @property
    def tmp(self) -> Path:
        return self.root / "tmp"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    # ------------------------------------------------------------ canonical-relative paths (POSIX strings)
    @staticmethod
    def partition(dataset: str, run_id: str, source_id: str | None = None, part: int = 0,
                  scope: str | None = None, name: str | None = None) -> str:
        grammar.COMPILED["run"].fullmatch(run_id) or _bad("run id", run_id)
        parts = [dataset]
        if dataset == "artifacts":
            parts.append(f"run={run_id}")
            if source_id:
                parts.append(f"source_id={_sid(source_id)}")
            else:
                parts.append(f"scope={scope or 'RUN'}")
        else:
            if source_id is not None:
                parts.append(f"source_id={_sid(source_id)}")
            parts.append(f"run={run_id}")
        parts.append(name or f"part-{part:05d}.parquet")
        return "/".join(parts)

    @staticmethod
    def commit_marker(run_id: str, key: str, commit_id: str) -> str:
        grammar.COMPILED["commit_key"].fullmatch(key) or _bad("commit key", key)
        return f"_commits/run={run_id}/{key}__{commit_id}.json"

    @staticmethod
    def run_marker(run_id: str, phase: str) -> str:
        return f"_runs/run={run_id}/{phase}.json"

    @staticmethod
    def lease(key: str) -> str:
        grammar.COMPILED["commit_key"].fullmatch(key) or _bad("commit key", key)
        return f"_leases/{key}.lease"

    @staticmethod
    def admission(commit_id: str) -> str:
        grammar.COMPILED["commit"].fullmatch(commit_id) or _bad("commit id", commit_id)
        return f"_admission/{commit_id}.json"

    @staticmethod
    def snapshot_manifest(snapshot_id: str, candidate: bool = False) -> str:
        grammar.COMPILED["snapshot"].fullmatch(snapshot_id) or _bad("snapshot id", snapshot_id)
        return f"_snapshots/{'candidates/' if candidate else ''}{snapshot_id}.json"

    CURRENT = "CURRENT"
    LOCK = ".lock"

    def path(self, rel: str) -> Path:
        """Absolute path of a canonical-relative POSIX path (refuses absolute paths and ``..``)."""
        grammar.check_relative_path(rel)
        return self.canonical / PurePosixPath(rel)

    def rel(self, path: Path) -> str:
        return path.resolve().relative_to(self.canonical.resolve()).as_posix()

    # ------------------------------------------------------------ blobs
    @staticmethod
    def blob_relpath(kind: str, artifact_id: str, media_type: str) -> str:
        """``<kind_dir>/<hh>/<hh>/<hex>.<ext>`` under ``artifacts/`` (the ``storage_relpath`` of the index row)."""
        grammar.COMPILED["artifact"].fullmatch(artifact_id) or _bad("artifact id", artifact_id)
        hexd = artifact_id[7:]
        ext = MEDIA_TYPE_EXT.get(media_type, "bin")
        return f"{ARTIFACT_KIND_DIR[kind]}/{hexd[:2]}/{hexd[2:4]}/{hexd}.{ext}"

    def blob_path(self, storage_relpath: str) -> Path:
        grammar.check_relative_path(storage_relpath)
        return self.artifacts / PurePosixPath(storage_relpath)

    # ------------------------------------------------------------ root marker
    def kind(self) -> RootKind:
        if not self.marker.is_file():
            raise RootError(f"no root marker {ROOT_MARKER} in the data root (run: vkm-corpus canon init --kind ...)")
        data = json.loads(self.marker.read_text(encoding="utf-8"))
        return RootKind(data["root_kind"])

    def require(self, kind: RootKind | str) -> "CanonLayout":
        actual = self.kind()
        if actual != RootKind(kind):
            raise RootError(f"operation needs a {RootKind(kind).value} root, this root is {actual.value}")
        role = os.environ.get("VKM_DATA_ROLE", "").strip()
        if role and DATA_ROLE_KIND.get(role) != actual:
            raise RootError(f"VKM_DATA_ROLE={role} contradicts the root marker {actual.value}")
        return self

    def ensure_dirs(self) -> None:
        for d in (self.canonical, self.artifacts, self.tmp, self.cache):
            d.mkdir(parents=True, exist_ok=True)
        if os.stat(self.tmp).st_dev != os.stat(self.canonical).st_dev:
            raise RootError("tmp/ and canonical/ must be on the same file system (atomic os.replace)")


def _sid(source_id: str) -> str:
    if not grammar.matches("source", source_id):
        _bad("source id", source_id)
    return source_id


def _bad(what: str, value: str):
    raise ValueError(f"invalid {what}: {value!r}")


def init_root(root: str | Path, kind: RootKind | str, *, note: str | None = None) -> CanonLayout:
    """Create (or confirm) a data root of the given kind. Changing the kind of an existing root is refused."""
    layout = CanonLayout(Path(root))
    kind = RootKind(kind)
    layout.root.mkdir(parents=True, exist_ok=True)
    if layout.marker.exists():
        existing = layout.kind()
        if existing != kind:
            raise RootError(f"root is already {existing.value}; refusing to change it to {kind.value}")
    else:
        body = {"root_kind": kind.value, "layout_version": LAYOUT_VERSION,
                "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        if note:
            body["note"] = note
        tmp = layout.root / (ROOT_MARKER + ".tmp")
        tmp.write_text(json.dumps(body, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        os.replace(tmp, layout.marker)
    layout.ensure_dirs()
    return layout


def open_root(root: str | Path, kind: RootKind | str | None = None) -> CanonLayout:
    layout = CanonLayout(Path(root))
    if kind is not None:
        layout.require(kind)
    else:
        layout.kind()
    return layout

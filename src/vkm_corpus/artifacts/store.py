"""Content-addressed blob store with atomic, write-once semantics.

Layout: ``<root>/<kind_dir>/<hex[0:2]>/<hex[2:4]>/<hex>.<ext>`` where ``root = $VKM_DATA_ROOT/artifacts``. IDs contain
``:`` and never become file names (Windows); the file name is the bare hex digest.

* ``put_bytes`` writes a blob once (temporary file on the same file system → fsync → ``os.replace``); a second put of
  the same bytes is a no-op after a size check.
* ``register_reproducible`` records an artifact whose bytes are *not* kept (``NOT_STORED_REPRODUCIBLE``) but can be
  regenerated from its ``recipe`` (renderer, version, profile, page); only kinds listed in ``REPRODUCIBLE_KINDS`` may be
  registered this way (H-23).
* Every call returns an :class:`ArtifactRecord` – the index row the canonical writer stores with the source commit.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def _vocab():
    from vkm_corpus.contracts import vocab

    return vocab


def kind_dir(kind: str) -> str:
    return _vocab().ARTIFACT_KIND_DIR[kind]


def retention_of(kind: str) -> str:
    return str(_vocab().ARTIFACT_KIND_RETENTION[kind])


def reproducible_kinds() -> frozenset[str]:
    return frozenset(str(k) for k in _vocab().REPRODUCIBLE_ARTIFACT_KINDS)


def media_ext(media_type: str) -> str:
    return _vocab().MEDIA_TYPE_EXT.get(media_type, "bin")


STORED = "STORED"
NOT_STORED_REPRODUCIBLE = "NOT_STORED_REPRODUCIBLE"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def artifact_id_of(data: bytes) -> str:
    return "sha256:" + sha256_hex(data)


def hex_of(artifact_id: str) -> str:
    if not artifact_id.startswith("sha256:") or len(artifact_id) != 71:
        raise ValueError(f"not an artifact id: {artifact_id!r}")
    return artifact_id[7:]


def canonical_json_bytes(obj: Any) -> bytes:
    """Deterministic JSON: sorted keys, no whitespace, UTF-8, NaN forbidden."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def gzip_deterministic(data: bytes, level: int = 6) -> bytes:
    """gzip with a zero timestamp and no file name, so equal input gives equal bytes."""
    return gzip.compress(data, compresslevel=level, mtime=0)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ArtifactRecord:
    """Index row of one artifact (content, not usage: usage lives in the referencing rows)."""

    artifact_id: str
    artifact_kind: str
    media_type: str
    size_bytes: int | None
    storage_relpath: str | None
    materialization: str
    retention_class: str
    recipe: dict[str, Any] | None = None
    image_width_px: int | None = None
    image_height_px: int | None = None
    image_dpi: float | None = None
    pixel_sha256: str | None = None
    producer_signature: str | None = None
    source_id: str | None = None
    page_id: str | None = None
    created_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        return d


class ArtifactStore:
    """Write-once content-addressed store rooted at ``$VKM_DATA_ROOT/artifacts``."""

    def __init__(self, root: Path, index_path: Path | None = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._tmp = self.root / ".tmp"
        self._tmp.mkdir(exist_ok=True)
        self._lock = threading.Lock()
        self.records: list[ArtifactRecord] = []
        self._index_path = index_path
        self._index_fh = None

    def _append_index(self, rec: ArtifactRecord) -> None:
        """Append the record to the staging artifact index (JSON lines; first registration of an id wins)."""
        if self._index_path is None:
            return
        if self._index_fh is None:
            self._index_path.parent.mkdir(parents=True, exist_ok=True)
            self._index_fh = open(self._index_path, "a", encoding="utf-8", newline="\n")
        self._index_fh.write(json.dumps(rec.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                                        default=str) + "\n")
        self._index_fh.flush()

    def close(self) -> None:
        if self._index_fh is not None:
            self._index_fh.close()
            self._index_fh = None

    # ------------------------------------------------------------------ paths
    @staticmethod
    def relpath(kind: str, digest: str, media_type: str) -> str:
        """``<kind_dir>/<hh>/<hh>/<hex>.<ext>`` – the rule of ``vkm_corpus.parquet.layout.CanonLayout.blob_relpath``."""
        return f"{kind_dir(kind)}/{digest[0:2]}/{digest[2:4]}/{digest}.{media_ext(media_type)}"

    def path_for(self, record: ArtifactRecord) -> Path:
        if record.storage_relpath is None:
            raise FileNotFoundError(f"{record.artifact_id} is not materialized ({record.materialization})")
        return self.root / record.storage_relpath

    def find(self, artifact_id: str) -> Path | None:
        """Locate a stored blob by id (scans the kind directories; use the index when available)."""
        digest = hex_of(artifact_id)
        for kd in sorted(set(_vocab().ARTIFACT_KIND_DIR.values())):
            base = self.root / kd / digest[0:2] / digest[2:4]
            if base.is_dir():
                for cand in base.glob(digest + ".*"):
                    return cand
        return None

    # ------------------------------------------------------------------ writes
    def put_bytes(self, data: bytes, kind: str, media_type: str, *, recipe: dict[str, Any] | None = None,
                  source_id: str | None = None, page_id: str | None = None, producer_signature: str | None = None,
                  image_width_px: int | None = None, image_height_px: int | None = None,
                  image_dpi: float | None = None, pixel_sha256: str | None = None,
                  record: bool = True) -> ArtifactRecord:
        if kind not in _vocab().ARTIFACT_KIND_DIR:
            raise ValueError(f"unknown artifact kind {kind!r}")
        digest = sha256_hex(data)
        rel = self.relpath(kind, digest, media_type)
        target = self.root / rel
        if target.exists():
            if target.stat().st_size != len(data):
                raise RuntimeError(f"ARTIFACT_HASH_MISMATCH: existing blob {rel} has a different size")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self._tmp, prefix=digest[:16] + ".")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, target)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        rec = ArtifactRecord(
            artifact_id="sha256:" + digest, artifact_kind=kind, media_type=media_type, size_bytes=len(data),
            storage_relpath=rel, materialization=STORED, retention_class=retention_of(kind),
            recipe=recipe, image_width_px=image_width_px, image_height_px=image_height_px, image_dpi=image_dpi,
            pixel_sha256=pixel_sha256, producer_signature=producer_signature, source_id=source_id, page_id=page_id)
        with self._lock:
            if record:
                self.records.append(rec)
            self._append_index(rec)
        return rec

    def put_json(self, obj: Any, kind: str, *, compress: bool = False, **kw: Any) -> ArtifactRecord:
        data = canonical_json_bytes(obj)
        if compress:
            return self.put_bytes(gzip_deterministic(data), kind, "application/gzip+json", **kw)
        return self.put_bytes(data, kind, "application/json", **kw)

    def register_reproducible(self, data_sha256: str, size_bytes: int | None, kind: str, media_type: str,
                              recipe: dict[str, Any], *, source_id: str | None = None, page_id: str | None = None,
                              image_width_px: int | None = None, image_height_px: int | None = None,
                              image_dpi: float | None = None, pixel_sha256: str | None = None,
                              record: bool = True) -> ArtifactRecord:
        if kind not in reproducible_kinds():
            raise ValueError(f"kind {kind!r} may not be registered without bytes")
        if not recipe:
            raise ValueError("a NOT_STORED_REPRODUCIBLE artifact needs a recipe")
        rec = ArtifactRecord(
            artifact_id="sha256:" + data_sha256, artifact_kind=kind, media_type=media_type, size_bytes=size_bytes,
            storage_relpath=None, materialization=NOT_STORED_REPRODUCIBLE,
            retention_class=retention_of(kind), recipe=recipe, image_width_px=image_width_px,
            image_height_px=image_height_px, image_dpi=image_dpi, pixel_sha256=pixel_sha256, source_id=source_id,
            page_id=page_id)
        with self._lock:
            if record:
                self.records.append(rec)
            self._append_index(rec)
        return rec

    # ------------------------------------------------------------------ reads
    def read_bytes(self, artifact_id: str, record: ArtifactRecord | None = None) -> bytes:
        path = self.path_for(record) if record is not None else self.find(artifact_id)
        if path is None or not path.exists():
            raise FileNotFoundError(artifact_id)
        data = path.read_bytes()
        if sha256_hex(data) != hex_of(artifact_id):
            raise RuntimeError(f"ARTIFACT_HASH_MISMATCH: {artifact_id}")
        return data

    def read_json(self, artifact_id: str) -> Any:
        data = self.read_bytes(artifact_id)
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        return json.loads(data.decode("utf-8"))

    def exists(self, artifact_id: str) -> bool:
        return self.find(artifact_id) is not None

    # ------------------------------------------------------------------ index
    def take_records(self) -> list[ArtifactRecord]:
        """Return and clear the rows registered since the last call (deduplicated by id, first registration wins)."""
        with self._lock:
            out, seen = [], set()
            for rec in self.records:
                if rec.artifact_id in seen:
                    continue
                seen.add(rec.artifact_id)
                out.append(rec)
            self.records = []
        return out


def dedupe_records(records: Iterable[ArtifactRecord]) -> list[ArtifactRecord]:
    out, seen = [], set()
    for rec in records:
        if rec.artifact_id not in seen:
            seen.add(rec.artifact_id)
            out.append(rec)
    return out


def load_index(root: Path) -> dict[str, dict[str, Any]]:
    """artifact_id → first registered record, from every ``*.jsonl`` under ``root`` (the staging artifact index)."""
    out: dict[str, dict[str, Any]] = {}
    if not Path(root).exists():
        return out
    for f in sorted(Path(root).rglob("*.jsonl")):
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                out.setdefault(rec["artifact_id"], rec)
    return out

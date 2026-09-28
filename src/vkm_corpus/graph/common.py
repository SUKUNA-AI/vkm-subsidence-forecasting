"""Infrastructure shared by agent E's projections (Neo4j graph and OpenSearch index).

* ``require_canonical_root`` — projections are built only from a CANONICAL data root (H-07, CP-15): the process role
  (``VKM_DATA_ROLE=canonical``) and the root marker ``.vkm_root.json`` must both say so; a STAGING root is refused.
* ``load_snapshot`` — the sealed snapshot manifest (``canonical/CURRENT`` → ``canonical/_snapshots/<id>.json``);
  projections read only the files the manifest lists (never a directory glob) and record its sha256.
* ``FileLock`` — one projector run per host and projection.
* ``canonical_json`` / ``StreamDigest`` — deterministic serialisation for ``expected_digest``/``content_digest`` and
  ``doc_stream_sha256``.
* ``write_receipt`` — JSON receipts under ``$VKM_DATA_ROOT/receipts/projections/<engine>/`` with logical paths only.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from vkm_corpus.config import ConfigError, Settings

# layout below $VKM_DATA_ROOT used by the projections (to be replaced by the coordinator's layout constant, H-32)
CANONICAL_DIR = "canonical"
CURRENT_FILE = "CURRENT"
SNAPSHOTS_DIR = "_snapshots"
ROOT_MARKER = ".vkm_root.json"
LOCKS_DIR = "locks"
RECEIPTS_DIR = "receipts/projections"
LOGS_DIR = "logs/projections"
TMP_DIR = "tmp"
CANONICAL_ROOT_KIND = "CANONICAL"
PROJECTOR_VERSION = "vkm-projections/0.1.0"


class ProjectionError(RuntimeError):
    """A refused or failed projection step. ``code`` is a stable machine-readable ``E_*`` string."""

    def __init__(self, code: str, message: str, *, stage: str = "", retryable: bool = False,
                 details: Mapping[str, Any] | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.stage = stage
        self.retryable = retryable
        self.details = dict(details or {})

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "stage": self.stage, "message": self.message, "retryable": self.retryable,
                "details": self.details}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_stamp(moment: datetime | None = None) -> str:
    return (moment or utc_now()).strftime("%Y%m%dT%H%M%SZ")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------- canonical root and snapshot
def _root_kind(marker: Path) -> str | None:
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict):
        for key in ("root_kind", "kind", "ROOT_KIND"):
            value = data.get(key)
            if isinstance(value, str):
                return value.upper()
    return None


def require_canonical_root(settings: Settings) -> Path:
    """The data root, if and only if it is the CANONICAL root; otherwise ``E_STAGING_ROOT`` (H-07)."""
    try:
        root = settings.require_data_root()
    except ConfigError as exc:
        raise ProjectionError("E_NO_DATA_ROOT", str(exc), stage="preflight") from exc
    if settings.data_role != "canonical":
        raise ProjectionError("E_STAGING_ROOT", "projections are built only on the CANONICAL root "
                              f"(VKM_DATA_ROLE={settings.data_role!r}); staging roots never feed Neo4j/OpenSearch",
                              stage="preflight")
    kind = _root_kind(root / ROOT_MARKER)
    if kind != CANONICAL_ROOT_KIND:
        raise ProjectionError("E_STAGING_ROOT", f"root marker {ROOT_MARKER} missing or not CANONICAL "
                              f"(found {kind!r})", stage="preflight")
    try:  # agent D's guard: marker and VKM_DATA_ROLE must agree (H-07)
        from vkm_corpus.contracts.vocab import RootKind
        from vkm_corpus.parquet.layout import CanonLayout, RootError
    except ImportError:  # pragma: no cover - D's package is part of the platform
        return root
    try:
        CanonLayout(root).require(RootKind.CANONICAL)
    except RootError as exc:
        raise ProjectionError("E_STAGING_ROOT", str(exc), stage="preflight") from exc
    return root


@dataclass(frozen=True)
class DatasetFiles:
    name: str
    kind: str | None
    paths: tuple[Path, ...]
    expected: tuple[Mapping[str, Any], ...]      # manifest file entries (path, sha256, bytes, rows)
    schema_versions: tuple[str, ...]
    rows: int | None


@dataclass(frozen=True)
class SnapshotInfo:
    """What a projection records about its input: identity, manifest hash, schema versions."""

    snapshot_id: str
    manifest_sha256: str
    schema_versions: tuple[str, ...] = ()          # "dataset=version" entries, sorted
    manifest_counts: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"snapshot_id": self.snapshot_id, "manifest_sha256": self.manifest_sha256,
                "schema_versions": list(self.schema_versions), "manifest_counts": dict(self.manifest_counts)}


@dataclass(frozen=True)
class Snapshot:
    root: Path
    info: SnapshotInfo
    manifest: Mapping[str, Any]
    datasets: Mapping[str, DatasetFiles]

    @property
    def snapshot_id(self) -> str:
        return self.info.snapshot_id

    @property
    def manifest_sha256(self) -> str:
        return self.info.manifest_sha256


def _dataset_files(canonical: Path, name: str, spec: Mapping[str, Any]) -> DatasetFiles:
    entries = tuple(spec.get("files") or ())
    paths = []
    for entry in entries:
        rel = entry.get("path") if isinstance(entry, Mapping) else entry
        if not isinstance(rel, str) or rel.startswith(("/", "\\")) or ".." in Path(rel).parts or ":" in rel.split("/")[0]:
            raise ProjectionError("E_CANON_MANIFEST_MISMATCH", f"dataset {name}: bad file path in manifest",
                                  stage="snapshot", details={"path": str(rel)})
        paths.append(canonical / rel)
    versions = spec.get("schema_versions") or ([spec["schema_version"]] if spec.get("schema_version") else [])
    return DatasetFiles(name=name, kind=spec.get("kind"), paths=tuple(paths),
                        expected=tuple(e for e in entries if isinstance(e, Mapping)),
                        schema_versions=tuple(str(v) for v in versions), rows=spec.get("rows"))


def load_snapshot(root: Path, snapshot_id: str | None = None) -> Snapshot:
    """Read ``CURRENT`` (or the given id) and its manifest. Paths in the manifest are relative to ``canonical/``."""
    canonical = root / CANONICAL_DIR
    if snapshot_id is None:
        current = canonical / CURRENT_FILE
        if not current.is_file():
            raise ProjectionError("E_NO_SNAPSHOT", "canonical/CURRENT is missing: nothing has been published",
                                  stage="snapshot")
        snapshot_id = current.read_text(encoding="utf-8").strip()
    if not snapshot_id or "/" in snapshot_id or "\\" in snapshot_id or snapshot_id.startswith("."):
        raise ProjectionError("E_NO_SNAPSHOT", f"invalid snapshot id {snapshot_id!r}", stage="snapshot")
    path = canonical / SNAPSHOTS_DIR / f"{snapshot_id}.json"
    if not path.is_file():
        raise ProjectionError("E_NO_SNAPSHOT", f"manifest of snapshot {snapshot_id} is missing", stage="snapshot")
    raw = path.read_bytes()
    manifest = json.loads(raw.decode("utf-8"))
    if manifest.get("snapshot_id") not in (None, snapshot_id):
        raise ProjectionError("E_CANON_MANIFEST_MISMATCH", "manifest snapshot_id differs from its file name",
                              stage="snapshot")
    datasets = {name: _dataset_files(canonical, name, spec)
                for name, spec in sorted((manifest.get("datasets") or {}).items())}
    versions = tuple(sorted(f"{d.name}={v}" for d in datasets.values() for v in d.schema_versions))
    info = SnapshotInfo(snapshot_id=snapshot_id, manifest_sha256=sha256_bytes(raw), schema_versions=versions,
                        manifest_counts=dict(manifest.get("counts") or {}))
    return Snapshot(root=root, info=info, manifest=manifest, datasets=datasets)


def verify_snapshot_files(snapshot: Snapshot, *, hash_files: bool = True) -> dict[str, Any]:
    """Every manifest file exists with the recorded size (and sha256). Raises ``E_CANON_MANIFEST_MISMATCH``."""
    problems: list[dict[str, Any]] = []
    n_files = n_bytes = 0
    for ds in snapshot.datasets.values():
        for path, entry in zip(ds.paths, ds.expected + ({},) * (len(ds.paths) - len(ds.expected))):
            rel = path.relative_to(snapshot.root).as_posix()
            if not path.is_file():
                problems.append({"dataset": ds.name, "path": rel, "problem": "missing"})
                continue
            size = path.stat().st_size
            n_files += 1
            n_bytes += size
            if entry.get("bytes") is not None and int(entry["bytes"]) != size:
                problems.append({"dataset": ds.name, "path": rel, "problem": "size"})
            elif hash_files and entry.get("sha256") and sha256_file(path) != entry["sha256"]:
                problems.append({"dataset": ds.name, "path": rel, "problem": "sha256"})
    if problems:
        raise ProjectionError("E_CANON_MANIFEST_MISMATCH", f"{len(problems)} manifest file(s) do not match",
                              stage="snapshot", details={"problems": problems[:50]})
    return {"files": n_files, "bytes": n_bytes, "hashed": hash_files}


# ---------------------------------------------------------------- lock
class FileLock:
    """Non-blocking exclusive advisory lock; released by the OS if the process dies."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: Any = None

    def acquire(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = self.path.open("a+b")
        try:
            if sys.platform == "win32":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            raise ProjectionError("E_PROJECTION_BUSY", f"another projector run holds {self.path.name}",
                                  stage="lock", retryable=True) from exc
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()} {utc_now().isoformat()}\n".encode())
        fh.flush()
        self._fh = fh
        return self

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "FileLock":
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()


# ---------------------------------------------------------------- canonical JSON and digests
def normalize_value(value: Any) -> Any:
    """Driver/DuckDB values → JSON-stable Python values (temporal → ISO 8601, tuples → lists)."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValueError("NaN/inf are not allowed in projections")
        return value
    if isinstance(value, (list, tuple)):
        return [normalize_value(v) for v in value]
    if isinstance(value, Mapping):
        return {str(k): normalize_value(v) for k, v in value.items()}
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    iso = getattr(value, "iso_format", None)          # neo4j.time.* types
    if callable(iso):
        return iso()
    if hasattr(value, "item") and callable(value.item):   # numpy scalars, Decimal-like
        return normalize_value(value.item())
    try:
        from decimal import Decimal
        if isinstance(value, Decimal):
            return float(value)
    except ImportError:  # pragma: no cover
        pass
    raise TypeError(f"unsupported value type in a projection: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(normalize_value(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def clean_props(props: Mapping[str, Any], *, drop: Iterable[str] = ()) -> dict[str, Any]:
    """Drop None values (Neo4j does not store null properties) and excluded keys; normalise values."""
    skip = set(drop)
    return {k: normalize_value(v) for k, v in props.items() if v is not None and k not in skip}


class DigestOrderError(ValueError):
    """Rows reached a streaming digest out of order (the two sides would not be comparable)."""


class StreamDigest:
    """sha256 over lines that arrive strictly ordered by ``sort_key``; the order is asserted, not assumed."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._h = hashlib.sha256()
        self.count = 0
        self._last: tuple[Any, ...] | None = None

    def add(self, sort_key: tuple[Any, ...], line: str) -> None:
        if self._last is not None and not sort_key > self._last:
            raise DigestOrderError(f"{self.name}: rows out of order at {sort_key!r} after {self._last!r}")
        self._last = sort_key
        self._h.update(line.encode("utf-8"))
        self._h.update(b"\n")
        self.count += 1

    def hexdigest(self) -> str:
        return self._h.hexdigest()


def combine_digests(parts: Iterable[tuple[str, int, str]]) -> str:
    """One digest over named part digests in the given (registry) order: ``name \\t count \\t sha256`` lines."""
    h = hashlib.sha256()
    for name, count, digest in parts:
        h.update(f"{name}\t{count}\t{digest}\n".encode("utf-8"))
    return h.hexdigest()


# ---------------------------------------------------------------- check results (preflight P*, graph C*, search S*)
PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"      # values of vkm_corpus.contracts.vocab.CheckStatus


@dataclass
class CheckResult:
    check_id: str
    title: str
    status: str = PASS
    code: str | None = None                  # E_* error code when FAIL
    count: int = 0                           # number of violations (or the measured value)
    examples: list[Any] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"check_id": self.check_id, "title": self.title, "status": self.status,
                               "count": self.count}
        if self.code:
            out["code"] = self.code
        if self.examples:
            out["examples"] = self.examples[:20]
        if self.details:
            out["details"] = self.details
        return out


def check(check_id: str, title: str, violations: list[Any] | int, *, code: str, warn_only: bool = False,
          details: dict[str, Any] | None = None) -> CheckResult:
    """PASS when there are no violations, else FAIL (or WARN) with ``code``."""
    n = violations if isinstance(violations, int) else len(violations)
    examples = [] if isinstance(violations, int) else list(violations[:20])
    if n == 0:
        return CheckResult(check_id, title, PASS, None, 0, [], details or {})
    return CheckResult(check_id, title, WARN if warn_only else FAIL, code, n, examples, details or {})


def failures(results: Iterable[CheckResult]) -> list[CheckResult]:
    return [r for r in results if r.status == FAIL]


# ---------------------------------------------------------------- receipts
def write_receipt(data_root: Path, engine: str, run_id: str, payload: Mapping[str, Any]) -> str:
    """Write ``receipts/projections/<engine>/<run_id>.json``; returns the path relative to the data root."""
    if not run_id or any(c in run_id for c in "/\\:"):
        raise ValueError("receipt ids must be plain file names")
    folder = data_root / RECEIPTS_DIR / engine
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{run_id}.json"
    text = json.dumps(normalize_value(payload), sort_keys=True, indent=1, ensure_ascii=False) + "\n"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    return path.relative_to(data_root).as_posix()

"""Derived embedding artifacts (постановка лаборатории §39–44, §64): the canonical store of vectors.

Layout (under ``$VKM_DATA_ROOT``; OpenSearch is only a rebuildable projection of it)::

    derived/embeddings/<kind>/<model-dir>/<revision>/<config-hash>/
        config.json                    EmbeddingConfig (signature = <config-hash>)
        part-<writer>-00000.parquet …  immutable parts; a part is never rewritten
        _manifest-<writer>.json        parts of that writer with row counts and sha256 (written last, atomically)

Every writer (e.g. ``rx580-0``, ``rtx-0``) owns its part names and its manifest, so concurrent workers on different
GPUs never race on a shared counter; readers merge all manifests.

``kind`` ∈ dense | sparse | multivector | visual; ``<model-dir>`` is the model id with ``/`` → ``__``.
Row schemas (§40–43) share ``object_id, source_id, page_id, object_type, text_hash, content_sha256, model_id,
model_revision, config_hash, embedding_signature, worker, backend, created_at``; then

* dense: ``dimension, precision, quant, vector`` (list<float32> or list<int8> for int8 storage);
* sparse: ``token_ids`` list<int32>, ``weights`` list<float32>;
* multivector: ``token_count, dimension, precision, vectors`` (flat list<halffloat|float32>, row-major token×dim),
  ``token_ids`` of the kept positions (explainability, §54);
* visual: ``artifact_sha``, ``dimension, precision, vector``.

Re-embedding appends parts. The *current* view keeps, per object, the newest row whose ``text_hash`` equals the
object's current text hash; older rows stay (history) until an explicit compaction. :func:`validate` implements the
pre-import checks of §64: expected count, no duplicates, no missing ids, one config signature / dimension /
normalisation, finite vectors of the right size and norm, manifest checksums.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Sequence

import numpy as np

from vkm_corpus.embeddings.signature import EmbeddingConfig, embedding_signature

Kind = Literal["dense", "sparse", "multivector", "visual"]
KINDS: tuple[Kind, ...] = ("dense", "sparse", "multivector", "visual")
DERIVED_SUBDIR = Path("derived") / "embeddings"
MANIFEST_PREFIX = "_manifest-"
_WRITER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
ARTIFACT_SCHEMA_VERSION = "0.1.0"
_COMMON = ("object_id", "source_id", "page_id", "object_type", "text_hash", "content_sha256", "model_id",
           "model_revision", "config_hash", "embedding_signature", "worker", "backend", "created_at")


def model_dir_name(model_id: str) -> str:
    if not model_id or ".." in model_id or model_id.startswith("/"):
        raise ValueError(f"bad model id {model_id!r}")
    return model_id.replace("/", "__")


def config_dir(data_root: Path, kind: Kind, config: EmbeddingConfig) -> Path:
    if kind not in KINDS:
        raise ValueError(f"unknown artifact kind {kind!r}")
    return Path(data_root) / DERIVED_SUBDIR / kind / model_dir_name(config.model_id) / config.model_revision / \
        config.signature()


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _atomic_write_text(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ------------------------------------------------------------------------------------------------------------ schemas
def arrow_schema(kind: Kind, precision: str = "float32"):
    import pyarrow as pa

    common = [pa.field("object_id", pa.string(), False), pa.field("source_id", pa.string()),
              pa.field("page_id", pa.string()), pa.field("object_type", pa.string()),
              pa.field("text_hash", pa.string(), False), pa.field("content_sha256", pa.string()),
              pa.field("model_id", pa.string(), False), pa.field("model_revision", pa.string(), False),
              pa.field("config_hash", pa.string(), False), pa.field("embedding_signature", pa.string(), False),
              pa.field("worker", pa.string()), pa.field("backend", pa.string()),
              pa.field("created_at", pa.timestamp("ms", tz="UTC"), False)]
    value = {"float32": pa.float32(), "float16": pa.float16(), "int8": pa.int8(), "binary": pa.uint8()}[precision]
    if kind == "dense":
        extra = [pa.field("dimension", pa.int32(), False), pa.field("precision", pa.string(), False),
                 pa.field("quant", pa.string()), pa.field("vector", pa.list_(value), False)]
    elif kind == "sparse":
        extra = [pa.field("token_ids", pa.list_(pa.int32()), False), pa.field("weights", pa.list_(pa.float32()), False)]
    elif kind == "multivector":
        extra = [pa.field("token_count", pa.int32(), False), pa.field("dimension", pa.int32(), False),
                 pa.field("precision", pa.string(), False), pa.field("vectors", pa.list_(value), False),
                 pa.field("token_ids", pa.list_(pa.int32()))]
    elif kind == "visual":
        extra = [pa.field("artifact_sha", pa.string(), False), pa.field("dimension", pa.int32(), False),
                 pa.field("precision", pa.string(), False), pa.field("vector", pa.list_(value), False)]
    else:
        raise ValueError(kind)
    return pa.schema(common + extra, metadata={"vkm.embeddings.kind": kind,
                                               "vkm.embeddings.schema_version": ARTIFACT_SCHEMA_VERSION})


# ------------------------------------------------------------------------------------------------------------- rows
@dataclass
class EmbeddingRow:
    object_id: str
    text_hash: str
    source_id: str | None = None
    page_id: str | None = None
    object_type: str | None = None
    content_sha256: str | None = None
    vector: np.ndarray | None = None              # dense / visual
    vectors: np.ndarray | None = None             # multivector [tokens, dim]
    token_ids: Sequence[int] | None = None        # multivector kept ids / sparse ids
    weights: Sequence[float] | None = None        # sparse
    artifact_sha: str | None = None               # visual
    worker: str = ""
    backend: str = ""
    created_at: datetime | None = None


def _cast(values: np.ndarray, precision: str) -> np.ndarray:
    if precision == "float32":
        return np.asarray(values, dtype=np.float32)
    if precision == "float16":
        return np.asarray(values, dtype=np.float16)
    if precision == "int8":
        v = np.asarray(values)
        if v.dtype != np.int8:
            raise ValueError("int8 storage expects int8 values (quantise before writing)")
        return v
    if precision == "binary":
        return np.packbits(np.asarray(values) >= 0, axis=-1)
    raise ValueError(precision)


class ArtifactWriter:
    """Appends immutable Parquet parts of one writer to one config directory and keeps its manifest consistent.
    Thread-safe for one writer id (a small lock around the manifest; never held during inference)."""

    def __init__(self, data_root: Path, kind: Kind, config: EmbeddingConfig, *, writer_id: str = "w0") -> None:
        if not _WRITER_RE.match(writer_id):
            raise ValueError(f"bad writer id {writer_id!r}")
        self.kind, self.config, self.writer_id = kind, config, writer_id
        self._lock = threading.Lock()
        self.dir = config_dir(data_root, kind, config)
        self.dir.mkdir(parents=True, exist_ok=True)
        cfg_path = self.dir / "config.json"
        cfg_json = json.dumps({"kind": kind, "config_signature": config.signature(), "config": config.as_dict()},
                              ensure_ascii=False, indent=1, sort_keys=True)
        if cfg_path.exists():
            if json.loads(cfg_path.read_text(encoding="utf-8"))["config_signature"] != config.signature():
                raise ValueError(f"config.json in {self.dir} belongs to another signature")
        else:
            _atomic_write_text(cfg_path, cfg_json)

    @property
    def manifest_path(self) -> Path:
        return self.dir / f"{MANIFEST_PREFIX}{self.writer_id}.json"

    def manifest(self) -> dict[str, Any]:
        path = self.manifest_path
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return {"kind": self.kind, "config_signature": self.config.signature(), "writer": self.writer_id,
                "schema_version": ARTIFACT_SCHEMA_VERSION, "parts": []}

    def _table(self, rows: Sequence[EmbeddingRow]):
        import pyarrow as pa

        cfg = self.config
        sig = cfg.signature()
        prec = cfg.storage_precision
        now = datetime.now(timezone.utc)
        cols: dict[str, list[Any]] = {k: [] for k in _COMMON}
        extra: dict[str, list[Any]] = {}
        values: list[np.ndarray] = []          # vector payloads, stored as Arrow lists built from numpy (float16-safe)
        for r in rows:
            for k in ("object_id", "source_id", "page_id", "object_type", "text_hash", "content_sha256", "worker",
                      "backend"):
                cols[k].append(getattr(r, k))
            cols["model_id"].append(cfg.model_id)
            cols["model_revision"].append(cfg.model_revision)
            cols["config_hash"].append(sig)
            cols["embedding_signature"].append(embedding_signature(r.object_id, r.text_hash, sig))
            cols["created_at"].append(r.created_at or now)
            if self.kind in ("dense", "visual"):
                if r.vector is None:
                    raise ValueError(f"{r.object_id}: dense/visual row without vector")
                values.append(_cast(np.asarray(r.vector).reshape(-1), prec))
                extra.setdefault("dimension", []).append(int(np.asarray(r.vector).size))
                extra.setdefault("precision", []).append(prec)
                if self.kind == "dense":
                    extra.setdefault("quant", []).append(cfg.quantization)
                else:
                    extra.setdefault("artifact_sha", []).append(r.artifact_sha)
            elif self.kind == "sparse":
                extra.setdefault("token_ids", []).append([int(x) for x in (r.token_ids or [])])
                extra.setdefault("weights", []).append([float(x) for x in (r.weights or [])])
            else:
                m = np.asarray(r.vectors)
                if m.ndim != 2:
                    raise ValueError(f"{r.object_id}: multivector must be [tokens, dim]")
                extra.setdefault("token_count", []).append(int(m.shape[0]))
                extra.setdefault("dimension", []).append(int(m.shape[1]))
                extra.setdefault("precision", []).append(prec)
                values.append(_cast(m, prec).reshape(-1))
                extra.setdefault("token_ids", []).append(None if r.token_ids is None else [int(x) for x in r.token_ids])
        schema = arrow_schema(self.kind, prec)
        data: dict[str, Any] = {**cols, **extra}
        if values:
            offsets = np.zeros(len(values) + 1, dtype=np.int32)
            np.cumsum([v.size for v in values], out=offsets[1:])
            payload = "vectors" if self.kind == "multivector" else "vector"
            value_type = schema.field(payload).type.value_type
            flat = pa.array(np.concatenate(values), type=value_type)
            data[payload] = pa.ListArray.from_arrays(pa.array(offsets, pa.int32()), flat)
        return pa.table({f.name: data[f.name] for f in schema}, schema=schema)

    def write_part(self, rows: Sequence[EmbeddingRow]) -> dict[str, Any]:
        import pyarrow.parquet as pq

        if not rows:
            raise ValueError("empty part")
        table = self._table(rows)
        with self._lock:
            man = self.manifest()
            prefix = f"part-{self.writer_id}-"
            idx = 1 + max((int(p["file"][len(prefix):len(prefix) + 5]) for p in man["parts"]), default=-1)
            name = f"{prefix}{idx:05d}.parquet"
            final = self.dir / name
            if final.exists():
                raise FileExistsError(f"{final} exists: parts are immutable")
            fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".parquet", dir=self.dir)
            os.close(fd)
            try:
                pq.write_table(table, tmp, compression="zstd")
                os.replace(tmp, final)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
            entry = {"file": name, "rows": table.num_rows, "sha256": _file_sha256(final),
                     "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            man["parts"].append(entry)
            man["rows_total"] = sum(p["rows"] for p in man["parts"])
            _atomic_write_text(self.manifest_path, json.dumps(man, indent=1, ensure_ascii=False))
        return entry


# ------------------------------------------------------------------------------------------------------------ reading
def read_table(directory: Path, *, verify: bool = True):
    import pyarrow as pa
    import pyarrow.parquet as pq

    manifests = sorted(Path(directory).glob(f"{MANIFEST_PREFIX}*.json"))
    if not manifests:
        raise FileNotFoundError(f"no {MANIFEST_PREFIX}*.json in {directory}")
    parts = [part for m in manifests for part in json.loads(m.read_text(encoding="utf-8"))["parts"]]
    tables = []
    for part in parts:
        path = Path(directory) / part["file"]
        if verify and _file_sha256(path) != part["sha256"]:
            raise ValueError(f"checksum mismatch: {path.name}")
        tables.append(pq.read_table(path))
    if not tables:
        return None
    return pa.concat_tables(tables)


def current_rows(table, expected: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
    """Per object the newest row (by created_at, then part order); with ``expected`` (object_id → text_hash) only
    rows of the current text are eligible."""
    out: dict[str, dict[str, Any]] = {}
    if table is None:
        return out
    for row in table.to_pylist():
        oid = row["object_id"]
        if expected is not None and expected.get(oid) != row["text_hash"]:
            continue
        prev = out.get(oid)
        if prev is None or row["created_at"] >= prev["created_at"]:
            out[oid] = row
    return out


def existing_hashes(table) -> dict[str, set[str]]:
    """object_id → text hashes already embedded under this config (input of the re-embed plan)."""
    out: dict[str, set[str]] = {}
    if table is None:
        return out
    for oid, th in zip(table.column("object_id").to_pylist(), table.column("text_hash").to_pylist()):
        out.setdefault(oid, set()).add(th)
    return out


# ---------------------------------------------------------------------------------------------------------- validate
@dataclass
class ValidationReport:
    ok: bool
    expected: int
    rows_current: int
    missing: list[str] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    stale_rows: int = 0
    bad_vectors: list[str] = field(default_factory=list)
    signature_mismatch: int = 0
    problems: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        for k in ("missing", "unexpected", "duplicates", "bad_vectors"):
            d[f"n_{k}"] = len(d[k])
            d[k] = d[k][:20]
        return d


def validate(directory: Path, config: EmbeddingConfig, expected: dict[str, str], *,
             norm_tol: float = 1e-3) -> ValidationReport:
    """§64 checks before an OpenSearch import. ``expected`` = object_id → current text hash of every object that must
    be embedded."""
    problems: list[str] = []
    try:
        table = read_table(directory, verify=True)
    except (FileNotFoundError, ValueError) as exc:
        return ValidationReport(False, len(expected), 0, problems=[str(exc)])
    sig = config.signature()
    rows = table.to_pylist() if table is not None else []
    sig_bad = sum(1 for r in rows if r["config_hash"] != sig)
    if sig_bad:
        problems.append(f"{sig_bad} rows with another config signature")
    seen: dict[tuple[str, str], int] = {}
    for r in rows:
        key = (r["object_id"], r["text_hash"])
        seen[key] = seen.get(key, 0) + 1
    duplicates = sorted({k[0] for k, n in seen.items() if n > 1})
    cur = current_rows(table, expected)
    missing = sorted(set(expected) - set(cur))
    present = {r["object_id"] for r in rows}
    unexpected = sorted(present - set(expected))
    stale = sum(1 for r in rows if r["object_id"] in expected and expected[r["object_id"]] != r["text_hash"])
    bad: list[str] = []
    for oid, r in cur.items():
        if r.get("embedding_signature") != embedding_signature(oid, r["text_hash"], sig):
            bad.append(oid)
            continue
        if "vector" in r and r.get("vector") is not None:
            v = np.asarray(r["vector"], dtype=np.float32)
            if r.get("dimension") != config.dimension and r.get("precision") != "binary":
                bad.append(oid)
            elif r.get("precision") in ("float32", "float16") and (not np.all(np.isfinite(v)) or
                                                                   (config.normalization == "l2" and
                                                                    abs(float(np.linalg.norm(v)) - 1.0) > norm_tol)):
                bad.append(oid)
        elif "vectors" in r and r.get("vectors") is not None:
            v = np.asarray(r["vectors"], dtype=np.float32)
            if r["token_count"] <= 0 or v.size != r["token_count"] * r["dimension"] or \
                    r["dimension"] != config.dimension or not np.all(np.isfinite(v)):
                bad.append(oid)
            elif config.normalization == "l2":
                norms = np.linalg.norm(v.reshape(r["token_count"], r["dimension"]), axis=1)
                if np.any(np.abs(norms - 1.0) > max(norm_tol, 5e-3)):   # float16 storage
                    bad.append(oid)
        elif "weights" in r:
            w = np.asarray(r["weights"], dtype=np.float32)
            if len(r["token_ids"]) != len(w) or not np.all(np.isfinite(w)) or np.any(w < 0):
                bad.append(oid)
    ok = not (missing or duplicates or bad or sig_bad or problems or unexpected)
    return ValidationReport(ok, len(expected), len(cur), missing, unexpected, duplicates, stale, bad, sig_bad,
                            problems)


def iter_current_vectors(directory: Path, expected: dict[str, str] | None = None) -> Iterable[tuple[str, Any]]:
    """(object_id, vector | [tokens, dim] matrix) of the current rows — the OpenSearch projection input."""
    table = read_table(directory)
    for oid, r in sorted(current_rows(table, expected).items()):
        if r.get("vector") is not None:
            yield oid, np.asarray(r["vector"])
        elif r.get("vectors") is not None:
            yield oid, np.asarray(r["vectors"], dtype=np.float32).reshape(r["token_count"], r["dimension"])
        else:
            yield oid, (r.get("token_ids"), r.get("weights"))

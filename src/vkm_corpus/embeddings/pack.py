"""Token-vector pack: the memory-safe serving store of late interaction (MaxSim) — agent L (постановка лаборатории
§44, §54, §64).

The canonical store of token vectors stays the multivector artifact directory (:mod:`.artifacts`: immutable Parquet
parts, per-writer manifests, the encoder output per row). A *pack* is a rebuildable projection of its current rows for
the units of one snapshot, laid out for random reads without loading it::

    <config dir>/packs/<pack-id>/
        tokens.f16      float16 [total_tokens, dim], row-major, little-endian, no header (``np.memmap``)
        index.parquet   one row per unit in pack order: unit_id, kind, page_id, source_id, object_ids, text_hash,
                        token_offset (int64, a row of tokens.f16), n_tokens (int32)
        pack.json       manifest, written last: schema, pack id, the embedding config (signature, model, revision,
                        weights, dimension, normalisation, text rule), snapshot, units sha256, counts, file sizes and
                        sha256, sha256 of the artifact manifests, the §64 report, timings
    <config dir>/packs/CURRENT   pointer ``{"pack_id", …}``, replaced atomically after a verified build (publish)

* Pack order groups the units of a page (``page_id``, then ``unit_id``): the token matrices of a page are one
  contiguous slice, so page-level MaxSim reads one range. Units of the *trailing kinds* (``BIB_ENTRY``) come after all
  other units, again grouped by page: CP-42 page scoring never touches them, and the bibliographic channel scans them
  as one contiguous region (``PackStore.scan``). A page's units then lie in at most two ranges.
* §64 before anything is written: part checksums, one config signature (model id and revision), expected units = the
  units of the snapshot, no duplicate (unit, text hash) rows, no missing unit, the per-row embedding signature, token
  count > 0, dimension, finite values, L2 norm of every token ≈ 1. A failed check writes no pack and never moves
  CURRENT. Rows of units that left the snapshot are orphaned history (§46): reported, never packed.
* ``pack-id = <snapshot>-<sha256 of (config signature, unit, text hash, part, row)… in pack order>[:12]``: the same
  selection and layout give the same pack, so a re-run on an unchanged artifact is ``EXISTS``.
* float16 storage of the L2-normalised token vectors is a MODEL_CHOICE of the serving path (half the bytes of the
  float32 rows; the MaxSim deviation is measured and reported); the Parquet rows keep the encoder output.

:class:`PackStore` opens one pack (``np.memmap`` of the tokens — pages are read on demand, never the whole file — and
the index in memory); :class:`PackHandle` follows ``packs/CURRENT`` (a newly published pack is picked up without a
service restart) and refuses a pack whose embedding config does not belong to the late query encoder.
"""
from __future__ import annotations

import hashlib
import json
import math
import mmap
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import numpy as np

from vkm_corpus.embeddings.artifacts import manifest_parts
from vkm_corpus.embeddings.signature import EmbeddingConfig, embedding_signature

PACK_SCHEMA = "vkm.multivector_pack/1"
UNITS_SCHEMA = "vkm.embedding_units/1"
PACKS_DIR = "packs"
POINTER = "CURRENT"
TOKENS_FILE, INDEX_FILE, MANIFEST_FILE = "tokens.f16", "index.parquet", "pack.json"
UNITS_MANIFEST, UNITS_FILE = "units.json", "units.jsonl"
DTYPE = np.dtype("<f2")
TRAILING_KINDS: tuple[str, ...] = ("BIB_ENTRY",)       # stored after all other units (one contiguous region)
OBJECT_KINDS = frozenset({"FIGURE", "TABLE", "FORMULA", "BIB_ENTRY"})     # one unit = one object
TARGET_KINDS = ("UNIT", "PAGE", *sorted(OBJECT_KINDS))
# embedding-config fields a pack must share with the late query encoder (same weights, tokenizer, heads, dimension)
COMPATIBLE_FIELDS = ("model_id", "model_revision", "weights_sha256", "quantization", "tokenizer_sha256",
                     "heads_sha256", "dimension")
_PACK_ID = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
_KEY_COLS = ["object_id", "text_hash", "config_hash", "embedding_signature", "created_at", "model_id",
             "model_revision", "token_count", "dimension"]


class PackError(RuntimeError):
    """``code``: E_PREFLIGHT | E_CHECK_FAILED | E_DIGEST_MISMATCH | E_REFUSED | E_BUSY."""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.details = code, message, dict(details or {})

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class _Lock:
    """Non-blocking exclusive advisory lock of one packs directory (released by the OS if the process dies)."""

    def __init__(self, path: Path) -> None:
        self.path, self._fh = path, None

    def __enter__(self) -> "_Lock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
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
            raise PackError("E_BUSY", "another pack build holds the packs lock") from exc
        self._fh = fh
        return self

    def __exit__(self, *exc: Any) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


# ------------------------------------------------------------------------------------------------ inputs
def read_config(directory: Path) -> tuple[str, EmbeddingConfig]:
    """(kind, config) of an artifact config directory; its name must be the config signature."""
    path = Path(directory) / "config.json"
    if not path.is_file():
        raise PackError("E_PREFLIGHT", "no config.json in the artifact directory (nothing encoded yet?)")
    raw = json.loads(path.read_text(encoding="utf-8"))
    config = EmbeddingConfig(**raw["config"])
    if raw.get("config_signature") != config.signature() or Path(directory).name != config.signature():
        raise PackError("E_DIGEST_MISMATCH", "config.json, its signature and the directory name disagree")
    return raw["kind"], config


def read_units(directory: Path, *, verify: bool = True) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """(manifest, unit_id → row) of a units export (``search export-units``); units.jsonl is checked against the
    manifest (the embedded texts in docs.jsonl are not needed here)."""
    d = Path(directory)
    path = d / UNITS_MANIFEST
    if not path.is_file():
        raise PackError("E_PREFLIGHT", f"no {UNITS_MANIFEST} in the units directory (run `search export-units`)")
    man = json.loads(path.read_text(encoding="utf-8"))
    if man.get("schema") != UNITS_SCHEMA:
        raise PackError("E_PREFLIGHT", f"units manifest schema {man.get('schema')!r} != {UNITS_SCHEMA}")
    if verify and _sha256_file(d / UNITS_FILE) != man.get("units_sha256"):
        raise PackError("E_DIGEST_MISMATCH", f"{UNITS_FILE} does not match the units manifest")
    units: dict[str, dict[str, Any]] = {}
    with open(d / UNITS_FILE, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                row = json.loads(line)
                if row["unit_id"] in units:
                    raise PackError("E_CHECK_FAILED", f"unit {row['unit_id']} listed twice")
                units[row["unit_id"]] = row
    if len(units) != man.get("count"):
        raise PackError("E_DIGEST_MISMATCH", f"{len(units)} units in {UNITS_FILE}, the manifest says {man.get('count')}")
    return man, units


# ------------------------------------------------------------------------------------------------ §64, streamed
@dataclass
class PackCheck:
    ok: bool = False
    config_signature: str = ""
    expected: int = 0
    current: int = 0
    rows: int = 0
    parts: int = 0
    missing: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    bad_vectors: list[str] = field(default_factory=list)
    signature_mismatch: int = 0
    orphaned: int = 0                    # rows of units that are not in the snapshot (history, not packed)
    stale_rows: int = 0                  # older texts of current units (history)
    problems: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        for k in ("missing", "duplicates", "bad_vectors", "problems"):
            d[f"n_{k}"] = len(d[k])
            d[k] = d[k][:20]
        return d


@dataclass(frozen=True)
class Selected:
    part: str
    row: int
    n_tokens: int
    text_hash: str


def select_current(directory: Path, config: EmbeddingConfig, expected: Mapping[str, str], *,
                   verify_checksums: bool = True) -> tuple[PackCheck, dict[str, Selected], list[dict[str, Any]]]:
    """Key columns of every part (never the vectors): the current row per expected unit + the §64 key checks.
    Returns (report, unit_id → selected row, parts in manifest order)."""
    import pyarrow.parquet as pq

    directory = Path(directory)
    sig = config.signature()
    rep = PackCheck(config_signature=sig, expected=len(expected))
    try:
        parts = manifest_parts(directory)
    except (OSError, ValueError, KeyError) as exc:
        rep.problems.append(f"unreadable manifest: {type(exc).__name__}")
        return rep, {}, []
    rep.parts = len(parts)
    seen: dict[tuple[str, str], int] = {}
    best: dict[str, tuple[Any, int, Selected, str]] = {}
    orphans: set[str] = set()
    for order, part in enumerate(parts):
        path = directory / part["file"]
        if not path.is_file():
            rep.problems.append(f"part listed in a manifest is missing: {part['file']}")
            continue
        if verify_checksums and _sha256_file(path) != part.get("sha256"):
            rep.problems.append(f"checksum mismatch: {part['file']}")
            continue
        t = pq.read_table(path, columns=_KEY_COLS)
        if t.num_rows != part.get("rows"):
            rep.problems.append(f"row count of {part['file']} differs from its manifest")
        cols = {c: t.column(c).to_pylist() for c in _KEY_COLS}
        for i in range(t.num_rows):
            rep.rows += 1
            oid, th = cols["object_id"][i], cols["text_hash"][i]
            if cols["config_hash"][i] != sig or cols["model_id"][i] != config.model_id or \
                    cols["model_revision"][i] != config.model_revision:
                rep.signature_mismatch += 1
                continue
            seen[(oid, th)] = seen.get((oid, th), 0) + 1
            want = expected.get(oid)
            if want is None:
                orphans.add(oid)
                continue
            if want != th:
                rep.stale_rows += 1
                continue
            key = (cols["created_at"][i], order)
            if oid not in best or key >= best[oid][:2]:
                sel = Selected(part["file"], i, int(cols["token_count"][i] or 0), th)
                best[oid] = (cols["created_at"][i], order, sel, cols["embedding_signature"][i])
            if cols["dimension"][i] != config.dimension or not cols["token_count"][i] or cols["token_count"][i] <= 0:
                rep.bad_vectors.append(oid)
    rep.duplicates = sorted({k[0] for k, n in seen.items() if n > 1})
    rep.orphaned = len(orphans)
    rep.missing = sorted(set(expected) - set(best))
    for oid, (_c, _o, sel, emb) in best.items():
        if emb != embedding_signature(oid, sel.text_hash, sig):
            rep.bad_vectors.append(oid)
    rep.bad_vectors = sorted(set(rep.bad_vectors))
    selection = {oid: sel for oid, (_c, _o, sel, _e) in best.items()}
    rep.current = len(selection) - len(set(rep.bad_vectors) & set(selection))
    rep.ok = not (rep.missing or rep.duplicates or rep.bad_vectors or rep.signature_mismatch or rep.problems) and \
        rep.current == rep.expected
    return rep, selection, parts


def _close_memmap(mm: Any) -> None:
    m = getattr(mm, "_mmap", None)
    if m is not None:
        try:
            m.close()
        except (BufferError, ValueError):
            pass


def _list_values(table: Any, column: str) -> tuple[np.ndarray, np.ndarray]:
    """(absolute offsets, flat values) of a list column (one chunk after combine)."""
    col = table.column(column).combine_chunks()
    return col.offsets.to_numpy(), col.values.to_numpy(zero_copy_only=False)


# ------------------------------------------------------------------------------------------------ build
def pack_order(selection: Mapping[str, Selected], units: Mapping[str, Mapping[str, Any]],
               trailing_kinds: Sequence[str] = TRAILING_KINDS) -> list[str]:
    """Units of a page together (page id, then unit id), units without a page last; the units of ``trailing_kinds``
    after all others, in the same order within their region."""
    trailing = {k: i for i, k in enumerate(trailing_kinds)}
    return sorted(selection, key=lambda u: (trailing.get(units[u].get("kind"), -1), units[u].get("page_id") is None,
                                            units[u].get("page_id") or "", u))


def pack_id_of(snapshot_id: str, config_signature: str, order: Sequence[str],
               selection: Mapping[str, Selected]) -> str:
    h = hashlib.sha256(config_signature.encode())
    for uid in order:
        s = selection[uid]
        h.update(f"\n{uid}\t{s.text_hash}\t{s.part}\t{s.row}".encode())
    return f"{snapshot_id}-{h.hexdigest()[:12]}"


def _manifests_sha256(directory: Path) -> str:
    return hashlib.sha256("".join(_sha256_file(m) for m in sorted(Path(directory).glob("_manifest-*.json")))
                          .encode()).hexdigest()


def _pointer(packs: Path) -> dict[str, Any] | None:
    path = packs / POINTER
    if not path.is_file():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not _PACK_ID.match(str(raw.get("pack_id") or "")):
        raise PackError("E_DIGEST_MISMATCH", "packs/CURRENT does not name a pack")
    return raw


def publish(packs: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Point ``packs/CURRENT`` at a verified pack (atomic replace)."""
    ptr = {"pack_id": manifest["pack_id"], "snapshot_id": manifest.get("snapshot_id"),
           "config_signature": manifest.get("config_signature"), "count": manifest.get("count"),
           "total_tokens": manifest.get("total_tokens"), "published_at": _utc()}
    _atomic_write(packs / POINTER, json.dumps(ptr, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
    return ptr


def prune(packs: Path, keep: int = 2) -> list[str]:
    """Delete (exact names) packs beyond the ``keep`` newest; the one CURRENT names is always kept, and so are
    unfinished builds of a live process (``.tmp-*``: removed only when older than a day)."""
    current = (_pointer(packs) or {}).get("pack_id")
    found = []
    for d in packs.iterdir() if packs.is_dir() else []:
        if not d.is_dir():
            continue
        if d.name.startswith(".tmp-"):
            if time.time() - d.stat().st_mtime > 86400:
                shutil.rmtree(d, ignore_errors=True)
            continue
        try:
            created = json.loads((d / MANIFEST_FILE).read_text(encoding="utf-8")).get("created_at") or ""
        except (OSError, ValueError):
            created = ""
        found.append((created, d.name))
    found.sort(reverse=True)
    keep_names = {n for _c, n in found[:max(1, keep)]} | ({current} if current else set())
    deleted = []
    for _c, name in found:
        if name not in keep_names:
            shutil.rmtree(packs / name)
            deleted.append(name)
    return deleted


def _quantiles(n: np.ndarray) -> dict[str, float]:
    if n.size == 0:
        return {}
    return {"mean": round(float(n.mean()), 1), "p50": float(np.percentile(n, 50)),
            "p95": float(np.percentile(n, 95)), "max": int(n.max())}


def build_pack(artifact_dir: Path, units_dir: Path, *, publish_current: bool = False, keep: int = 2,
               verify_checksums: bool = True, norm_tol: float | None = None,
               progress: Callable[[str], None] | None = None, packs_dir: Path | None = None,
               trailing_kinds: Sequence[str] = TRAILING_KINDS) -> dict[str, Any]:
    """§64 checks + pack of the current rows of ``artifact_dir`` for the units of ``units_dir``; returns the receipt.
    Raises :class:`PackError` (nothing written, CURRENT untouched) when a check fails. ``packs_dir`` (default
    ``<artifact_dir>/packs``) lets a copy of an artifact directory be packed elsewhere (benchmarks, read-only
    sources)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    t0 = time.monotonic()
    say = progress or (lambda _m: None)
    artifact_dir, units_dir = Path(artifact_dir), Path(units_dir)
    kind, config = read_config(artifact_dir)
    if kind != "multivector":
        raise PackError("E_REFUSED", f"artifact kind {kind!r}: only multivector artifacts are packed")
    umeta, units = read_units(units_dir)
    if umeta.get("text_rule") != config.text_rule:
        raise PackError("E_REFUSED", f"units rule {umeta.get('text_rule')} != embedding text rule {config.text_rule}")
    snapshot_id = str(umeta.get("snapshot_id") or "")
    if not _PACK_ID.match(snapshot_id):
        raise PackError("E_PREFLIGHT", "units manifest has no valid snapshot id")
    expected = {uid: u["text_hash"] for uid, u in units.items()}
    sig, dim = config.signature(), int(config.dimension)
    tol = norm_tol if norm_tol is not None else (5e-3 if config.storage_precision == "float16" else 1e-3)
    receipt: dict[str, Any] = {"schema": PACK_SCHEMA, "status": "PLANNED", "snapshot_id": snapshot_id,
                               "units_sha256": umeta.get("units_sha256"), "config_signature": sig,
                               "model_id": config.model_id, "model_revision": config.model_revision,
                               "dimension": dim, "dtype": "float16", "started_at": _utc()}
    packs = Path(packs_dir) if packs_dir else artifact_dir / PACKS_DIR
    packs.mkdir(parents=True, exist_ok=True)
    with _Lock(packs / ".lock"):
        say("key pass (§64 key columns of every part)")
        check, selection, _parts = select_current(artifact_dir, config, expected, verify_checksums=verify_checksums)
        receipt["checks_64"] = check.as_dict()
        timings = {"key_pass": round(time.monotonic() - t0, 2)}
        if not check.ok:
            raise PackError("E_CHECK_FAILED", "pre-pack checks (§64) failed; nothing was packed",
                            details=check.as_dict())
        order = pack_order(selection, units, trailing_kinds)
        pid = pack_id_of(snapshot_id, sig, order, selection)
        final = packs / pid
        receipt["pack_id"] = pid
        if (final / MANIFEST_FILE).is_file():
            man = json.loads((final / MANIFEST_FILE).read_text(encoding="utf-8"))
            size = (final / TOKENS_FILE).stat().st_size if (final / TOKENS_FILE).is_file() else -1
            if man.get("schema") == PACK_SCHEMA and size == max(1, int(man["total_tokens"])) * dim * DTYPE.itemsize:
                receipt.update({k: man.get(k) for k in ("count", "total_tokens", "bytes", "by_kind",
                                                        "tokens_per_unit")})
                receipt["status"] = "EXISTS"
                if publish_current:
                    receipt["published"] = publish(packs, man)
                    receipt["pruned"] = prune(packs, keep)
                receipt["seconds"] = {**timings, "total": round(time.monotonic() - t0, 2)}
                return receipt
            shutil.rmtree(final)                          # an unfinished or foreign directory under the id: rebuild
        ntok = np.array([selection[u].n_tokens for u in order], dtype=np.int64)
        offsets = np.zeros(len(order) + 1, dtype=np.int64)
        np.cumsum(ntok, out=offsets[1:])
        total = int(offsets[-1])
        pos = {u: i for i, u in enumerate(order)}
        tmp = Path(tempfile.mkdtemp(prefix=f".tmp-{pid}-", dir=packs))
        try:
            by_part: dict[str, list[tuple[int, str]]] = {}
            for uid, s in selection.items():
                by_part.setdefault(s.part, []).append((s.row, uid))
            bad: list[str] = []
            t1 = time.monotonic()
            mm = np.memmap(tmp / TOKENS_FILE, dtype=DTYPE, mode="w+", shape=(max(1, total), dim))
            try:
                for n, (fname, rows) in enumerate(sorted(by_part.items())):
                    t = pq.read_table(artifact_dir / fname, columns=["vectors"])
                    offs, vals = _list_values(t, "vectors")
                    for row, uid in sorted(rows):
                        k = selection[uid].n_tokens
                        a, b = int(offs[row]), int(offs[row + 1])
                        if b - a != k * dim:
                            bad.append(uid)
                            continue
                        m = np.asarray(vals[a:b], dtype=np.float32).reshape(k, dim)
                        norms = np.linalg.norm(m, axis=1)
                        if not np.all(np.isfinite(m)) or (config.normalization == "l2" and
                                                          np.any(np.abs(norms - 1.0) > tol)):
                            bad.append(uid)
                            continue
                        o = int(offsets[pos[uid]])
                        mm[o:o + k] = m
                    del t, offs, vals
                    if n % 50 == 49:
                        say(f"vectors: {n + 1}/{len(by_part)} parts")
                if not bad:
                    mm.flush()
            finally:                                  # an open mapping keeps the file (Windows: undeletable)
                _close_memmap(mm)
                del mm
            if bad:
                check.bad_vectors = sorted(set(bad))
                check.ok = False
                receipt["checks_64"] = check.as_dict()
                raise PackError("E_CHECK_FAILED", f"{len(bad)} units with bad token vectors; nothing was packed",
                                details=check.as_dict())
            with open(tmp / TOKENS_FILE, "rb+") as fh:
                os.fsync(fh.fileno())
            timings["vector_pass"] = round(time.monotonic() - t1, 2)
            say("index + checksums")
            index = pa.table({
                "unit_id": pa.array(order, pa.string()),
                "kind": pa.array([units[u].get("kind") for u in order], pa.string()),
                "page_id": pa.array([units[u].get("page_id") for u in order], pa.string()),
                "source_id": pa.array([units[u].get("source_id") for u in order], pa.string()),
                "object_ids": pa.array([list(units[u].get("object_ids") or []) for u in order],
                                       pa.list_(pa.string())),
                "text_hash": pa.array([selection[u].text_hash for u in order], pa.string()),
                "token_offset": pa.array(offsets[:-1], pa.int64()),
                "n_tokens": pa.array(ntok.astype(np.int32), pa.int32())},
                metadata={"vkm.pack.schema": PACK_SCHEMA, "vkm.pack.id": pid})
            pq.write_table(index, tmp / INDEX_FILE, compression="zstd")
            by_kind: dict[str, int] = {}
            for u in order:
                by_kind[units[u].get("kind") or "?"] = by_kind.get(units[u].get("kind") or "?", 0) + 1
            files = {name: {"bytes": (tmp / name).stat().st_size, "sha256": _sha256_file(tmp / name)}
                     for name in (TOKENS_FILE, INDEX_FILE)}
            man = {"schema": PACK_SCHEMA, "pack_id": pid, "snapshot_id": snapshot_id,
                   "units_sha256": umeta.get("units_sha256"), "text_rule": config.text_rule,
                   "config_signature": sig, "config": config.as_dict(), "model_id": config.model_id,
                   "model_revision": config.model_revision, "dimension": dim,
                   "normalization": config.normalization, "dtype": "float16", "byte_order": "little",
                   "source_precision": config.storage_precision, "count": len(order), "total_tokens": total,
                   "layout": {"order": "page_id, unit_id", "trailing_kinds": list(trailing_kinds)},
                   "bytes": files[TOKENS_FILE]["bytes"], "by_kind": dict(sorted(by_kind.items())),
                   "tokens_per_unit": _quantiles(ntok), "files": files,
                   "artifact_manifests_sha256": _manifests_sha256(artifact_dir), "artifact_parts": check.parts,
                   "checks_64": check.as_dict(), "created_at": _utc()}
            timings["total"] = round(time.monotonic() - t0, 2)
            man["seconds"] = timings
            _atomic_write(tmp / MANIFEST_FILE, json.dumps(man, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
            os.replace(tmp, final)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        receipt.update({k: man.get(k) for k in ("count", "total_tokens", "bytes", "by_kind", "tokens_per_unit",
                                                "files", "artifact_manifests_sha256", "layout")})
        receipt["status"] = "BUILT"
        if publish_current:
            receipt["published"] = publish(packs, man)
            receipt["pruned"] = prune(packs, keep)
        receipt["seconds"] = {**timings, "total": round(time.monotonic() - t0, 2)}
        receipt["dir"] = str(final)
    return receipt


def verify_pack(pack_dir: Path, artifact_dir: Path, *, sample: int = 200, seed: int = 20260928,
                query_tokens: int = 32) -> dict[str, Any]:
    """Sampled check of a pack against its Parquet rows: the float16 token values (max |Δ|) and the MaxSim deviation
    of float16 storage (pseudo-queries = the first ``query_tokens`` tokens of other sampled units)."""
    import pyarrow.parquet as pq

    from vkm_corpus.embeddings.postprocess import maxsim

    store = PackStore(Path(pack_dir), verify=False, rss_budget_bytes=None)
    _kind, config = read_config(Path(artifact_dir))
    if store.manifest.get("config_signature") != config.signature():
        return {"ok": False, "problem": "the pack belongs to another config signature"}
    rng = np.random.default_rng(seed)
    rows = sorted(rng.choice(len(store), size=min(sample, len(store)), replace=False).tolist())
    idx = pq.read_table(Path(pack_dir) / INDEX_FILE, columns=["unit_id", "text_hash"])
    expected = dict(zip(idx.column("unit_id").to_pylist(), idx.column("text_hash").to_pylist()))
    _rep, selection, _parts = select_current(Path(artifact_dir), config, expected, verify_checksums=False)
    wanted = [store.unit_ids[r] for r in rows]
    by_part: dict[str, list[tuple[int, str]]] = {}
    for u in wanted:
        if u in selection:
            by_part.setdefault(selection[u].part, []).append((selection[u].row, u))
    f32: dict[str, np.ndarray] = {}
    for fname, items in sorted(by_part.items()):
        t = pq.read_table(Path(artifact_dir) / fname, columns=["vectors"])
        offs, vals = _list_values(t, "vectors")
        for row, u in items:
            f32[u] = np.asarray(vals[int(offs[row]):int(offs[row + 1])], dtype=np.float32).reshape(-1, store.dim)
    max_abs, dev_abs, dev_rel = 0.0, [], []
    missing = [u for u in wanted if u not in f32]
    for i, u in enumerate(wanted):
        if u not in f32:
            continue
        d16 = store.matrix(store._row[u])
        if d16.shape != f32[u].shape:
            return {"ok": False, "problem": f"shape of {u} differs between the pack and the Parquet rows"}
        max_abs = max(max_abs, float(np.max(np.abs(d16 - f32[u]))))
        q = f32[wanted[(i + 1) % len(wanted)]][:query_tokens]
        a, b = maxsim(q, f32[u]), maxsim(q, d16)
        dev_abs.append(abs(a - b))
        dev_rel.append(abs(a - b) / max(abs(a), 1e-9))
    store.close()
    ok = not missing and max_abs < 1e-2
    return {"ok": ok, "pack_id": store.manifest.get("pack_id"), "sampled": len(wanted), "missing": len(missing),
            "max_abs_token_value_diff": round(max_abs, 6),
            "maxsim_abs_dev": {"max": round(max(dev_abs, default=0.0), 6),
                               "mean": round(float(np.mean(dev_abs)) if dev_abs else 0.0, 6)},
            "maxsim_rel_dev": {"max": round(max(dev_rel, default=0.0), 8),
                               "mean": round(float(np.mean(dev_rel)) if dev_rel else 0.0, 8)},
            "query_tokens": query_tokens}


# ------------------------------------------------------------------------------------------------ serving
@dataclass(frozen=True)
class TargetScore:
    id: str
    kind: str
    status: str                        # SCORED | NO_TOKENS
    late_score: float | None = None
    best_unit_id: str | None = None
    units: int = 0
    tokens: int = 0

    def as_dict(self) -> dict[str, Any]:
        d = {"id": self.id, "kind": self.kind, "status": self.status, "units": self.units, "tokens": self.tokens,
             "best_unit_id": self.best_unit_id}
        d["late_score"] = None if self.late_score is None else round(float(self.late_score), 6)
        return d


def compatibility(manifest: Mapping[str, Any], expect: Mapping[str, Any] | None) -> str | None:
    """None when the pack's embedding config matches the late query encoder on every :data:`COMPATIBLE_FIELDS`
    present in ``expect``; else a message (a query encoded by another model is never scored against these tokens)."""
    if not expect:
        return None
    cfg = manifest.get("config") or {}
    bad = [k for k in COMPATIBLE_FIELDS if k in expect and expect[k] not in (None, "") and cfg.get(k) != expect[k]]
    if bad:
        return f"the pack's embedding config differs from the late query encoder in {bad}"
    return None


class PackStore:
    """One pack, read-only: the token matrix is an ``np.memmap`` (the OS pages in what a query touches); the index
    (unit ids, pages, objects, offsets) is in memory. With ``rss_budget_bytes`` the pages this process has touched are
    dropped from its mapping (``madvise(MADV_DONTNEED)``) after that many bytes — they stay in the page cache, so the
    service's resident set stays bounded while repeated queries still read from memory."""

    def __init__(self, directory: Path, *, verify: bool = False, rss_budget_bytes: int | None = 256 << 20) -> None:
        import pyarrow.parquet as pq

        self.directory = Path(directory)
        man_path = self.directory / MANIFEST_FILE
        if not man_path.is_file():
            raise PackError("E_PREFLIGHT", f"no {MANIFEST_FILE} in the pack directory")
        self.manifest = json.loads(man_path.read_text(encoding="utf-8"))
        if self.manifest.get("schema") != PACK_SCHEMA or self.manifest.get("dtype") != "float16":
            raise PackError("E_PREFLIGHT", f"not a {PACK_SCHEMA} float16 pack")
        self.dim = int(self.manifest["dimension"])
        self.total = int(self.manifest["total_tokens"])
        tokens = self.directory / TOKENS_FILE
        if tokens.stat().st_size != max(1, self.total) * self.dim * DTYPE.itemsize:
            raise PackError("E_DIGEST_MISMATCH", f"{TOKENS_FILE} size does not match the manifest")
        if verify:
            for name, f in (self.manifest.get("files") or {}).items():
                if _sha256_file(self.directory / name) != f.get("sha256"):
                    raise PackError("E_DIGEST_MISMATCH", f"{name} does not match the manifest")
        idx = pq.read_table(self.directory / INDEX_FILE, columns=["unit_id", "kind", "page_id", "object_ids",
                                                                   "token_offset", "n_tokens"])
        self.unit_ids: list[str] = idx.column("unit_id").to_pylist()
        self.kinds: list[str] = idx.column("kind").to_pylist()
        self.offsets = idx.column("token_offset").to_numpy().astype(np.int64)
        self.ntok = idx.column("n_tokens").to_numpy().astype(np.int64)
        if len(self.unit_ids) != int(self.manifest["count"]) or \
                (len(self.unit_ids) and int(self.offsets[-1] + self.ntok[-1]) != self.total):
            raise PackError("E_DIGEST_MISMATCH", f"{INDEX_FILE} does not match the manifest")
        self._row = {u: i for i, u in enumerate(self.unit_ids)}
        # page → row ranges (one per region of the layout), page code per row (for scans), rows per kind
        self._page: dict[str, list[list[int]]] = {}
        self._page_names: list[str] = []
        codes = np.full(len(self.unit_ids), -1, dtype=np.int32)
        code_of: dict[str, int] = {}
        for i, p in enumerate(idx.column("page_id").to_pylist()):
            if not p:
                continue
            c = code_of.get(p)
            if c is None:
                c = code_of[p] = len(self._page_names)
                self._page_names.append(p)
            codes[i] = c
            ranges = self._page.setdefault(p, [])
            if ranges and ranges[-1][1] == i:
                ranges[-1][1] = i + 1
            else:
                ranges.append([i, i + 1])
        self._page_code = codes
        kinds_arr = np.asarray(self.kinds, dtype=object)
        self._kind_rows = {k: np.flatnonzero(kinds_arr == k).astype(np.int64) for k in sorted(set(self.kinds))}
        self._object: dict[str, int] = {}
        for i, (k, objs) in enumerate(zip(self.kinds, idx.column("object_ids").to_pylist())):
            if k in OBJECT_KINDS and objs and len(objs) == 1:
                self._object.setdefault(objs[0], i)
        del idx
        self._mm = np.memmap(tokens, dtype=DTYPE, mode="r", shape=(max(1, self.total), self.dim))
        self.rss_budget = rss_budget_bytes
        self._touched = 0
        self._lock = threading.Lock()
        self.releases = 0

    # -------------------------------------------------------------------------------------------- lookups
    def __len__(self) -> int:
        return len(self.unit_ids)

    def info(self) -> dict[str, Any]:
        m = self.manifest
        return {"pack_id": m.get("pack_id"), "snapshot_id": m.get("snapshot_id"),
                "config_signature": m.get("config_signature"), "model_id": m.get("model_id"),
                "dimension": self.dim, "count": len(self), "total_tokens": self.total, "bytes": m.get("bytes"),
                "units_sha256": m.get("units_sha256"), "created_at": m.get("created_at"),
                "trailing_kinds": (m.get("layout") or {}).get("trailing_kinds", [])}

    def rows_for(self, target_id: str, kind: str, *, page_exclude_kinds: Iterable[str] = ()) -> list[int]:
        """Unit rows of a target: UNIT → the unit; PAGE → every unit of the page except ``page_exclude_kinds``;
        FIGURE/TABLE/FORMULA/BIB_ENTRY → the unit of that one object."""
        if kind == "UNIT":
            r = self._row.get(target_id)
            return [] if r is None else [r]
        if kind == "PAGE":
            skip = frozenset(page_exclude_kinds)
            return [r for a, b in self._page.get(target_id, ()) for r in range(a, b) if self.kinds[r] not in skip]
        if kind in OBJECT_KINDS:
            r = self._object.get(target_id)
            return [] if r is None or self.kinds[r] != kind else [r]
        raise ValueError(f"unknown target kind {kind!r}")

    def matrix(self, row: int) -> np.ndarray:
        a = int(self.offsets[row])
        m = np.array(self._mm[a:a + int(self.ntok[row])], dtype=np.float32)
        self._touch(m.size * DTYPE.itemsize)
        return m

    def get(self, object_ids: Iterable[str]) -> dict[str, np.ndarray]:
        """``MultiVectorStore`` protocol (unit ids → float32 token matrices)."""
        out = {}
        for o in object_ids:
            r = self._row.get(o)
            if r is not None:
                out[o] = self.matrix(r)
        return out

    def _touch(self, nbytes: int) -> None:
        if not self.rss_budget:
            return
        with self._lock:
            self._touched += nbytes
            if self._touched < self.rss_budget:
                return
            self._touched = 0
        self.release()

    def release(self) -> None:
        """Drop this process's mapping of the pages touched so far (they stay in the page cache)."""
        mm = getattr(self._mm, "_mmap", None)
        if mm is not None and hasattr(mm, "madvise") and hasattr(mmap, "MADV_DONTNEED"):
            try:
                mm.madvise(mmap.MADV_DONTNEED)
                self.releases += 1
            except (OSError, ValueError):
                pass

    # -------------------------------------------------------------------------------------------- scoring
    def unit_scores_array(self, Q: np.ndarray, rows: Iterable[int] | np.ndarray, *,
                          chunk_tokens: int = 32768) -> tuple[np.ndarray, np.ndarray]:
        """(sorted unique rows, MaxSim ``Σ_q max_t ⟨q, d_t⟩`` of each) in float32. Consecutive rows are read as one
        slice of the memmap (≤ ``chunk_tokens`` tokens) and scored with one product per slice (segment maxima by
        ``reduceat``)."""
        Q = np.ascontiguousarray(np.asarray(Q, dtype=np.float32))
        rows_s = np.unique(np.asarray(list(rows) if not isinstance(rows, np.ndarray) else rows, dtype=np.int64))
        out = np.zeros(len(rows_s), dtype=np.float32)
        if not len(rows_s) or Q.size == 0:
            return rows_s, out
        starts = self.offsets[rows_s]
        ends = starts + self.ntok[rows_s]
        i, n = 0, len(rows_s)
        while i < n:
            j = i + 1
            s0 = int(starts[i])
            while j < n and rows_s[j] == rows_s[j - 1] + 1 and int(ends[j]) - s0 <= chunk_tokens:
                j += 1
            e0 = int(ends[j - 1])
            block = np.array(self._mm[s0:e0], dtype=np.float32)
            self._touch((e0 - s0) * self.dim * DTYPE.itemsize)
            sims = Q @ block.T                                            # [q, tokens of the slice]
            out[i:j] = np.maximum.reduceat(sims, (starts[i:j] - s0).astype(np.int64), axis=1).sum(axis=0)
            i = j
        return rows_s, out

    def unit_scores(self, Q: np.ndarray, rows: Iterable[int], *, chunk_tokens: int = 32768) -> dict[int, float]:
        """MaxSim of every unit row as ``{row: score}`` (see :meth:`unit_scores_array`)."""
        r, s = self.unit_scores_array(Q, rows, chunk_tokens=chunk_tokens)
        return {int(a): float(b) for a, b in zip(r.tolist(), s.tolist())}

    def kind_rows(self, kind: str) -> np.ndarray:
        return self._kind_rows.get(kind, np.zeros(0, dtype=np.int64))

    def scan(self, Q: np.ndarray, kind: str, *, top_pages: int = 100,
             chunk_tokens: int = 131072) -> list[dict[str, Any]]:
        """MaxSim over every unit of ``kind`` (e.g. all BIB_ENTRY units: the bibliographic channel) → the pages ranked
        by their best unit of that kind, with the best unit and the number of such units on the page. In the
        kind-grouped layout the units of a trailing kind are one contiguous region (a few large products)."""
        rows, scores = self.unit_scores_array(Q, self.kind_rows(kind), chunk_tokens=chunk_tokens)
        codes = self._page_code[rows] if len(rows) else np.zeros(0, dtype=np.int32)
        keep = codes >= 0
        rows, scores, codes = rows[keep], scores[keep], codes[keep]
        if not len(rows):
            return []
        order = np.lexsort((rows, -scores, codes))                   # per page: best score first (ties: lower row)
        c_sorted = codes[order]
        first = np.ones(len(order), dtype=bool)
        first[1:] = c_sorted[1:] != c_sorted[:-1]
        best = order[first]
        per_page = np.bincount(codes, minlength=int(codes.max()) + 1)
        best = best[np.lexsort((rows[best], -scores[best]))][:max(0, int(top_pages))]
        return [{"page_id": self._page_names[int(codes[t])], "late_score": round(float(scores[t]), 6),
                 "best_unit_id": self.unit_ids[int(rows[t])], "units": int(per_page[int(codes[t])])} for t in best]

    def score_targets(self, Q: np.ndarray, targets: Sequence[tuple[str, str]], *,
                      page_exclude_kinds: Iterable[str] = ()) -> list[TargetScore]:
        """Late score of each (id, kind) target = max MaxSim over its units (a page: every unit of the page except
        ``page_exclude_kinds``; an object: its unit); targets without token vectors are NO_TOKENS."""
        skip = tuple(page_exclude_kinds)
        resolved = [(tid, kind, self.rows_for(tid, kind, page_exclude_kinds=skip)) for tid, kind in targets]
        scores = self.unit_scores(Q, (r for _t, _k, rows in resolved for r in rows))
        out = []
        for tid, kind, rows in resolved:
            if not rows:
                out.append(TargetScore(tid, kind, "NO_TOKENS"))
                continue
            best = max(rows, key=lambda r: (scores[r], -r))
            out.append(TargetScore(tid, kind, "SCORED", scores[best], self.unit_ids[best], len(rows),
                                   int(sum(int(self.ntok[r]) for r in rows))))
        return out

    def close(self) -> None:
        mm = getattr(self._mm, "_mmap", None)
        self._mm = None
        if mm is not None:
            try:
                mm.close()
            except (BufferError, ValueError):
                pass                                      # a view is still alive: the GC closes it


class PackHandle:
    """The pack a service should use, from ``multivector_dir``: a pack directory itself (``pack.json``), or a
    multivector artifact directory whose ``packs/CURRENT`` names the pack. The pointer is re-read at most every
    ``check_s`` seconds; a new verified pack replaces the old one without a restart (requests in flight finish on the
    old store). A pack whose config does not match ``expect`` (the late query encoder) is refused."""

    def __init__(self, directory: str | Path, *, expect: Mapping[str, Any] | None = None, check_s: float = 10.0,
                 rss_budget_bytes: int | None = 256 << 20, clock: Callable[[], float] = time.monotonic) -> None:
        self.directory = Path(directory)
        self.expect = dict(expect or {})
        self.check_s = float(check_s)
        self.rss_budget = rss_budget_bytes
        self._clock = clock
        self._lock = threading.Lock()
        self._store: PackStore | None = None
        self._status: dict[str, Any] = {"status": "NOT_LOADED"}
        self._checked = -math.inf
        self.loads = 0
        self.refresh(force=True)

    def _target(self) -> Path | None:
        if (self.directory / MANIFEST_FILE).is_file():
            return self.directory
        ptr = _pointer(self.directory / PACKS_DIR)
        return None if ptr is None else self.directory / PACKS_DIR / ptr["pack_id"]

    def refresh(self, *, force: bool = False) -> None:
        now = self._clock()
        if not force and now - self._checked < self.check_s:
            return
        with self._lock:
            if not force and now - self._checked < self.check_s:
                return
            self._checked = now
            base = {"directory_kind": "pack" if (self.directory / MANIFEST_FILE).is_file() else "artifact"}
            try:
                target = self._target()
            except (OSError, ValueError, KeyError, PackError) as exc:
                self._status = {**self._status_of_store(), "last_error": f"pointer: {type(exc).__name__}"}
                return
            if target is None:
                if self._store is None:
                    self._status = {"status": "MISSING", **base,
                                    "reason": "no published pack (pack.json or packs/CURRENT) in multivector_dir"}
                return
            if self._store is not None and self._store.directory == target:
                return
            try:
                store = PackStore(target, rss_budget_bytes=self.rss_budget)
            except (OSError, ValueError, KeyError, PackError) as exc:
                reason = exc.message if isinstance(exc, PackError) else type(exc).__name__
                if self._store is None:
                    self._status = {"status": "ERROR", **base, "reason": f"pack {target.name}: {reason}"}
                else:
                    self._status = {**self._status_of_store(), "last_error": f"pack {target.name}: {reason}"}
                return
            problem = compatibility(store.manifest, self.expect)
            if problem:
                store.close()
                if self._store is None:
                    self._status = {"status": "MISMATCH", **base, "reason": problem, "pack_id": target.name}
                else:
                    self._status = {**self._status_of_store(), "last_error": f"pack {target.name}: {problem}"}
                return
            self._store = store
            self.loads += 1
            self._status = {**self._status_of_store(), **base, "loaded_at": _utc()}

    def _status_of_store(self) -> dict[str, Any]:
        if self._store is None:
            return {"status": self._status.get("status", "NOT_LOADED")}
        return {"status": "READY", **self._store.info(), "releases": self._store.releases, "loads": self.loads}

    def current(self) -> PackStore:
        """The store to use now; ``LookupError`` (→ HTTP 503) when there is none."""
        self.refresh()
        store = self._store
        if store is None:
            raise LookupError(f"late-interaction token store {self._status.get('status')}: "
                              f"{self._status.get('reason') or 'not loaded'}")
        return store

    def status(self) -> dict[str, Any]:
        self.refresh()
        out = dict(self._status)
        if self._store is not None:
            out.update({"status": "READY", "releases": self._store.releases, "loads": self.loads})
        return out

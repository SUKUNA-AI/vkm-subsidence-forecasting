"""Dense vector projection of the derived embedding artifacts (постановка лаборатории §44, §63–65; CP-17, CP-26).

OpenSearch holds only a rebuildable projection: the canonical vectors are K's derived artifacts
(``derived/embeddings/dense/<model>/<revision>/<config-hash>/``). One unit of rule ``vkm-units-v1`` (J, task §17) =
one document; the unit id is the document id. Documents carry the unit's ids (unit, object ids, page, source, work)
and the page-level filter fields of E's page documents (same names and mappings as the BM25 indices), so the BM25
filter whitelist applies to both legs of hybrid search (``vkm_corpus.search.hybrid``).

* ``export_units`` — units of one CANONICAL snapshot → ``derived/embeddings/units/<snapshot>/<rule>-<variant>/``:
  ``docs.jsonl`` (input of ``vkm-corpus embed encode``: ``object_id`` = unit id, embedded text), ``units.jsonl``
  (ids and ``text_hash``, no text) and ``units.json`` (manifest: snapshot, rule, counts, sha256). Idempotent.
* ``check_artifacts`` — the §64 pre-import checks, streamed part by part (key columns first, vectors per part; never
  all rows in memory): part checksums, one config signature, expected units = current rows, no duplicates, no missing
  ids, per-row embedding signature, dimension, finite vectors, L2 norm. Rows of units that left the canon are
  *orphaned* history (§46): reported, never projected.
* ``build_vectors`` — refuses unless the units belong to the CURRENT snapshot and the checks pass; then a versioned
  index ``<prefix>-vectors-m<V>-<build>`` (``knn_vector``: dimension, space and data type from the embedding
  signature; HNSW, lucene engine), bulk ``create``, count + sampled vector checks, ``_meta`` COMPLETE, one atomic
  alias swap to ``<prefix>-vectors``; old builds are deleted only by exact name (the aliased and the previous COMPLETE
  build are kept for rollback). A failed build never touches the alias.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import random
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

from vkm_corpus.config import Settings
from vkm_corpus.graph.common import (LOCKS_DIR, PROJECTOR_VERSION, FileLock, ProjectionError, Snapshot, load_snapshot,
                                     require_canonical_root, utc_now, write_receipt)
from vkm_corpus.search.mappings import COMMON, PER_TYPE, make_build_id

VECTORS_MAPPING_VERSION = "1"
VECTORS_SUFFIX = "vectors"
UNITS_SCHEMA = "vkm.embedding_units/1"
UNITS_SUBDIR = Path("derived") / "embeddings" / "units"
UNITS_MANIFEST, UNITS_FILE, DOCS_FILE = "units.json", "units.jsonl", "docs.jsonl"
BUILDING, COMPLETE = "BUILDING", "COMPLETE"
HNSW_PARAMETERS = {"m": 16, "ef_construction": 128}          # MODEL_CHOICE (K §7); recall is measured by J, not here
VECTOR_FIELD = "vector"
# page-level fields copied from E's page documents: exactly the index fields of the BM25 filter whitelist that a
# page carries (object-type fields such as block_type or figure_type are not page facts and are not projected)
PAGE_FIELDS: tuple[str, ...] = ("work_id", "page_index", "language", "origin", "review_status", "quality_flags",
                                "projection_flags", "source_site_scope", "source_site_scope_raw",
                                "source_site_scope_mapping", "available_latest_day", "available_basis",
                                "foreign_content_work_ids", "year", "has_preview", "page_status", "page_kind",
                                "page_class", "dup_group_id")
UNIT_SOURCE_FIELDS: tuple[str, ...] = ("id", "unit_kind", "object_ids", "page_id", "source_id", "work_id",
                                       "page_index", "dup_group_id", "part")
_KW = {"type": "keyword"}
_NOT_INDEXED = {"type": "keyword", "index": False, "doc_values": False}


# ---------------------------------------------------------------- names
def vectors_alias(prefix: str) -> str:
    return f"{prefix}-{VECTORS_SUFFIX}".lower()


def vectors_index_name(prefix: str, build_id: str) -> str:
    return f"{prefix}-{VECTORS_SUFFIX}-m{VECTORS_MAPPING_VERSION}-{build_id}".lower()


def parse_vectors_index(prefix: str, name: str) -> str | None:
    """Build id of one of our vector indices, else None."""
    head = f"{prefix}-{VECTORS_SUFFIX}-m{VECTORS_MAPPING_VERSION}-".lower()
    return name[len(head):] if name.startswith(head) else None


def units_dir(root: Path, snapshot_id: str, rule: str, variant: str) -> Path:
    if not snapshot_id or any(c in snapshot_id for c in "/\\") or snapshot_id.startswith("."):
        raise ProjectionError("E_NO_SNAPSHOT", f"invalid snapshot id {snapshot_id!r}", stage="units")
    return Path(root) / UNITS_SUBDIR / snapshot_id / f"{rule}-{variant}"


def split_text_rule(text_rule: str) -> tuple[str, str]:
    """``vkm-units-v1/A`` → (rule, context variant)."""
    rule, _, variant = text_rule.partition("/")
    if not rule or not variant:
        raise ProjectionError("E_UNKNOWN_VALUE", f"text rule {text_rule!r} is not <unit rule>/<variant>",
                              stage="plan")
    return rule, variant


# ---------------------------------------------------------------- mapping from the embedding signature
def knn_field(config: Any) -> dict[str, Any]:
    """``knn_vector`` of the signature: dimension; L2-normalised vectors → inner product (= cosine, no per-query
    normalisation), otherwise cosine; int8 storage → byte vectors; binary storage is not projected in v0."""
    precision = getattr(config, "storage_precision", "float32")
    if precision == "binary":
        raise ProjectionError("E_MODE_UNSUPPORTED", "binary vectors are not projected in v0", stage="plan")
    space = "innerproduct" if config.normalization == "l2" else "cosinesimil"
    out: dict[str, Any] = {"type": "knn_vector", "dimension": int(config.dimension),
                           "method": {"name": "hnsw", "engine": "lucene", "space_type": space,
                                      "parameters": dict(HNSW_PARAMETERS)}}
    if precision == "int8":
        out["data_type"] = "byte"
    return out


def vector_properties(config: Any) -> dict[str, Any]:
    props: dict[str, Any] = {k: copy.deepcopy(COMMON[k]) for k in ("id", "source_id", "page_id", *PAGE_FIELDS)
                             if k in COMMON}
    props.update({k: copy.deepcopy(PER_TYPE["pages"][k]) for k in PAGE_FIELDS if k in PER_TYPE["pages"]})
    props.update({"unit_kind": _KW, "object_ids": _KW, "part": {"type": "integer"}, "snapshot_id": _KW,
                  "config_signature": _KW, "text_hash": _NOT_INDEXED, VECTOR_FIELD: knn_field(config)})
    return props


def vectors_body(config: Any, meta: dict[str, Any]) -> dict[str, Any]:
    return {"settings": {"index": {"knn": True, "number_of_shards": 1, "number_of_replicas": 0,
                                   "refresh_interval": "-1"}},
            "mappings": {"dynamic": "strict",
                         "_meta": {**meta, "vkm_vectors_mapping_version": VECTORS_MAPPING_VERSION},
                         "properties": vector_properties(config)}}


# ---------------------------------------------------------------- units of a snapshot
def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_atomic(path: Path, lines: Iterable[str]) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            for line in lines:
                fh.write(line)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _jline(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def read_units_manifest(directory: Path, *, verify: bool = True) -> dict[str, Any]:
    path = Path(directory) / UNITS_MANIFEST
    if not path.is_file():
        raise ProjectionError("E_PREFLIGHT", f"no {UNITS_MANIFEST} in the units directory (run `search export-units`)",
                              stage="units")
    man = json.loads(path.read_text(encoding="utf-8"))
    if man.get("schema") != UNITS_SCHEMA:
        raise ProjectionError("E_PREFLIGHT", f"units manifest schema {man.get('schema')!r} != {UNITS_SCHEMA}",
                              stage="units")
    if verify:
        for key, name in (("units_sha256", UNITS_FILE), ("docs_sha256", DOCS_FILE)):
            f = Path(directory) / name
            if not f.is_file() or _sha256_file(f) != man.get(key):
                raise ProjectionError("E_DIGEST_MISMATCH", f"{name} does not match the units manifest",
                                      stage="units")
    return man


def load_units(directory: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """(manifest, unit_id → unit row) of a verified units export."""
    man = read_units_manifest(directory)
    units: dict[str, dict[str, Any]] = {}
    with open(Path(directory) / UNITS_FILE, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                row = json.loads(line)
                units[row["unit_id"]] = row
    if len(units) != man.get("count"):
        raise ProjectionError("E_DIGEST_MISMATCH", f"{len(units)} units in {UNITS_FILE}, manifest says "
                              f"{man.get('count')}", stage="units")
    return man, units


def _source_batches(ids: list[str], size: int) -> Iterator[list[str]]:
    for i in range(0, len(ids), size):
        yield ids[i:i + size]


def export_units(settings: Settings, *, snapshot_id: str | None = None, variant: str = "A",
                 out_dir: Path | None = None, force: bool = False, batch_sources: int = 20) -> dict[str, Any]:
    """Units of the CURRENT (or given) snapshot of the CANONICAL root; see the module docstring."""
    root = require_canonical_root(settings)
    return export_units_at(root, load_snapshot(root, snapshot_id), variant=variant, out_dir=out_dir, force=force,
                           batch_sources=batch_sources)


def export_units_at(root: Path, snapshot: Snapshot, *, variant: str = "A", out_dir: Path | None = None,
                    force: bool = False, batch_sources: int = 20) -> dict[str, Any]:
    from vkm_corpus.parquet.layout import CanonLayout
    from vkm_corpus.retrieval_lab.canon import CanonReader
    from vkm_corpus.retrieval_lab.units import CONTEXT_VARIANTS, UNIT_RULE, UnitConfig, build_units, render

    if variant not in CONTEXT_VARIANTS:
        raise ProjectionError("E_UNKNOWN_VALUE", f"context variant must be one of {CONTEXT_VARIANTS}", stage="units")
    out = Path(out_dir) if out_dir else units_dir(root, snapshot.snapshot_id, UNIT_RULE, variant)
    cfg = UnitConfig()
    if (out / UNITS_MANIFEST).is_file() and not force:
        try:
            man = read_units_manifest(out)
        except ProjectionError:
            man = None
        if man and man.get("snapshot_id") == snapshot.snapshot_id and man.get("unit_rule") == UNIT_RULE and \
                man.get("context_variant") == variant and man.get("unit_config") == cfg.as_dict():
            return {**man, "status": "EXISTS", "dir": str(out)}
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    reader = CanonReader.from_layout(CanonLayout(root), snapshot.snapshot_id)
    by_kind: dict[str, int] = {}
    docs: list[str] = []
    rows: list[str] = []
    try:
        try:
            meta = reader.source_meta()
        except Exception:  # noqa: BLE001 - a snapshot without the work views: no work ids, variant A/B/C unaffected
            meta = {}
        labels = reader.page_labels() if variant == "D" else {}
        seen: set[str] = set()
        for batch in _source_batches(reader.source_ids(), max(1, batch_sources)):
            data = reader.load_all(batch)
            units = build_units(data["pages"], data["blocks"], data["figures"], data["tables"], data["formulas"],
                                data["bibliography"], cfg)
            for u in units:
                if u.unit_id in seen:
                    raise ProjectionError("E_DUPLICATE_ID", f"unit id {u.unit_id} generated twice", stage="units")
                seen.add(u.unit_id)
                text = render(u, variant, meta.get(u.source_id), labels.get(u.page_id or ""))
                th = hashlib.sha256(text.encode("utf-8")).hexdigest()
                by_kind[u.kind] = by_kind.get(u.kind, 0) + 1
                docs.append(_jline({"object_id": u.unit_id, "text": text, "source_id": u.source_id,
                                    "page_id": u.page_id, "object_type": u.kind}))
                work = meta.get(u.source_id)
                rows.append(_jline({"unit_id": u.unit_id, "kind": u.kind, "source_id": u.source_id,
                                    "page_id": u.page_id, "work_id": work.work_id if work else None,
                                    "object_ids": list(u.object_ids), "part": u.part, "language": u.language,
                                    "text_hash": th}))
    finally:
        reader.close()
    _write_atomic(out / DOCS_FILE, docs)
    _write_atomic(out / UNITS_FILE, rows)
    man = {"schema": UNITS_SCHEMA, "snapshot_id": snapshot.snapshot_id,
           "canonical_manifest_sha256": snapshot.manifest_sha256, "unit_rule": UNIT_RULE, "context_variant": variant,
           "text_rule": f"{UNIT_RULE}/{variant}", "unit_config": cfg.as_dict(), "count": len(rows),
           "by_kind": dict(sorted(by_kind.items())), "units_sha256": _sha256_file(out / UNITS_FILE),
           "docs_sha256": _sha256_file(out / DOCS_FILE), "created_at": utc_now().isoformat(timespec="seconds"),
           "seconds": round(time.monotonic() - t0, 2)}
    _write_atomic(out / UNITS_MANIFEST, [json.dumps(man, ensure_ascii=False, indent=1, sort_keys=True) + "\n"])
    return {**man, "status": "WRITTEN", "dir": str(out)}


# ---------------------------------------------------------------- §64 checks, streamed
_META_COLS = ["object_id", "text_hash", "config_hash", "embedding_signature", "created_at", "model_id",
              "model_revision"]


@dataclass
class ArtifactCheck:
    ok: bool
    config_signature: str
    expected: int
    current: int = 0
    rows: int = 0
    parts: int = 0
    missing: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    bad_vectors: list[str] = field(default_factory=list)
    signature_mismatch: int = 0
    orphaned: int = 0                     # rows of units that are not in the snapshot (history, not projected)
    stale_rows: int = 0                   # older texts of current units (history)
    problems: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        for k in ("missing", "duplicates", "bad_vectors"):
            d[f"n_{k}"] = len(d[k])
            d[k] = d[k][:20]
        return d


def read_config_dir(directory: Path) -> tuple[str, Any]:
    """(kind, EmbeddingConfig) of an artifact config directory; its name must be the config signature."""
    from vkm_corpus.embeddings.signature import EmbeddingConfig

    path = Path(directory) / "config.json"
    if not path.is_file():
        raise ProjectionError("E_PREFLIGHT", "no config.json in the embeddings directory", stage="plan")
    raw = json.loads(path.read_text(encoding="utf-8"))
    config = EmbeddingConfig(**raw["config"])
    if raw.get("config_signature") != config.signature() or Path(directory).name != config.signature():
        raise ProjectionError("E_DIGEST_MISMATCH", "config.json, its signature and the directory name disagree",
                              stage="plan")
    return raw["kind"], config


def _vectors_of(table: Any, precision: str) -> Any:
    import numpy as np

    col = table.column(VECTOR_FIELD).combine_chunks()
    lengths = np.diff(col.offsets.to_numpy())
    flat = col.flatten().to_numpy(zero_copy_only=False)
    if len(lengths) and np.all(lengths == lengths[0]):
        mat = flat.reshape(len(lengths), int(lengths[0]))
    else:                                               # ragged: rows with a wrong size are flagged by the caller
        mat = None
    if precision in ("float16", "float32") and mat is not None:
        mat = mat.astype(np.float32, copy=False)
    return mat, lengths, flat


def check_artifacts(directory: Path, config: Any, expected: dict[str, str], *, norm_tol: float = 1e-3,
                    verify_checksums: bool = True) -> tuple[ArtifactCheck, dict[str, tuple[str, int]]]:
    """§64 checks; returns the report and the selection ``unit_id → (part file, row)`` of the current rows."""
    import numpy as np
    import pyarrow.parquet as pq

    from vkm_corpus.embeddings.artifacts import manifest_parts
    from vkm_corpus.embeddings.signature import embedding_signature

    directory = Path(directory)
    sig = config.signature()
    rep = ArtifactCheck(False, sig, len(expected))
    try:
        parts = manifest_parts(directory)
    except (OSError, ValueError, KeyError) as exc:
        rep.problems.append(f"unreadable manifest: {type(exc).__name__}")
        return rep, {}
    if not parts:
        rep.problems.append("no parts: nothing has been embedded under this signature")
        rep.missing = sorted(expected)
        return rep, {}
    rep.parts = len(parts)
    seen: dict[tuple[str, str], int] = {}
    best: dict[str, tuple[Any, int, str, int, str]] = {}    # oid → (created_at, part order, file, row, emb_sig)
    orphan_ids: set[str] = set()
    for order, part in enumerate(parts):
        path = directory / part["file"]
        if not path.is_file():
            rep.problems.append(f"part listed in a manifest is missing: {part['file']}")
            continue
        if verify_checksums and _sha256_file(path) != part.get("sha256"):
            rep.problems.append(f"checksum mismatch: {part['file']}")
            continue
        t = pq.read_table(path, columns=_META_COLS)
        if t.num_rows != part.get("rows"):
            rep.problems.append(f"row count of {part['file']} differs from its manifest")
        cols = {c: t.column(c).to_pylist() for c in _META_COLS}
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
                orphan_ids.add(oid)
                continue
            if want != th:
                rep.stale_rows += 1
                continue
            key = (cols["created_at"][i], order)
            if oid not in best or key >= best[oid][:2]:
                best[oid] = (cols["created_at"][i], order, part["file"], i, cols["embedding_signature"][i])
    rep.duplicates = sorted({k[0] for k, n in seen.items() if n > 1})
    rep.orphaned = len(orphan_ids)
    rep.missing = sorted(set(expected) - set(best))
    bad: list[str] = []
    for oid, (_c, _o, _f, _i, emb) in best.items():
        if emb != embedding_signature(oid, expected[oid], sig):
            bad.append(oid)
    by_part: dict[str, list[tuple[int, str]]] = {}
    for oid, (_c, _o, f, i, _e) in best.items():
        by_part.setdefault(f, []).append((i, oid))
    precision = config.storage_precision
    for f, rows in sorted(by_part.items()):
        t = pq.read_table(directory / f, columns=[VECTOR_FIELD])
        mat, lengths, _flat = _vectors_of(t, precision)
        for i, oid in rows:
            if int(lengths[i]) != int(config.dimension) or mat is None:
                bad.append(oid)
                continue
            if precision in ("float16", "float32"):
                v = mat[i]
                if not np.all(np.isfinite(v)) or (config.normalization == "l2" and
                                                  abs(float(np.linalg.norm(v)) - 1.0) > norm_tol):
                    bad.append(oid)
    rep.bad_vectors = sorted(set(bad))
    rep.current = len(best) - len(set(bad) & set(best))
    rep.ok = not (rep.missing or rep.duplicates or rep.bad_vectors or rep.signature_mismatch or rep.problems) and \
        rep.current == rep.expected
    selection = {oid: (f, i) for oid, (_c, _o, f, i, _e) in best.items()}
    return rep, selection


def iter_selected_vectors(directory: Path, selection: dict[str, tuple[str, int]],
                          precision: str) -> Iterator[tuple[str, Any]]:
    """(unit_id, vector) of the selected current rows, part by part (vectors never all in memory)."""
    import pyarrow.parquet as pq

    by_part: dict[str, list[tuple[int, str]]] = {}
    for oid, (f, i) in selection.items():
        by_part.setdefault(f, []).append((i, oid))
    for f in sorted(by_part):
        t = pq.read_table(Path(directory) / f, columns=[VECTOR_FIELD])
        mat, _lengths, _flat = _vectors_of(t, precision)
        for i, oid in sorted(by_part[f], key=lambda x: x[1]):
            yield oid, mat[i]


# ---------------------------------------------------------------- build
@dataclass
class VectorBuildOptions:
    embeddings: str
    snapshot_id: str | None = None           # assertion: must equal CURRENT
    units: str | None = None                 # units directory (default: derived/embeddings/units/<CURRENT>/<rule>-<v>)
    prefix: str | None = None
    plan_only: bool = False
    keep_failed: bool = False
    keep_builds: int = 2
    chunk_docs: int = 500
    sample: int = 32
    force_merge: bool = False
    verify_checksums: bool = True
    skip_if_current: bool = False            # alias already on a COMPLETE build of this snapshot, signature and units
    command: str = "search build-vectors"
    publish: bool = True
    policy_sha256: str | None = None


def resolve_embeddings(path: str, root: Path) -> Path:
    """An artifact config directory, or a JSON naming it: the report of ``embed encode`` (list of runs; the dense
    one is taken) or ``{"artifact_dir": ...}``. Relative paths are relative to the data root."""
    p = Path(path)
    if not p.is_absolute():
        p = root / p
    if p.is_dir():
        return p
    if not p.is_file():
        raise ProjectionError("E_PREFLIGHT", "embeddings path is neither a directory nor a JSON file",
                              stage="plan")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ProjectionError("E_PREFLIGHT", "embeddings JSON is not valid JSON", stage="plan") from exc
    if isinstance(raw, dict) and raw.get("artifact_dir"):
        target = raw["artifact_dir"]
    elif isinstance(raw, list):
        dense = [r for r in raw if isinstance(r, dict) and r.get("kind") == "dense" and r.get("dir")]
        if len(dense) != 1:
            raise ProjectionError("E_PREFLIGHT", f"the encode report names {len(dense)} dense runs (need 1)",
                                  stage="plan")
        target = dense[0]["dir"]
    else:
        raise ProjectionError("E_PREFLIGHT", "unknown embeddings JSON (need an encode report or artifact_dir)",
                              stage="plan")
    t = Path(target)
    return t if t.is_absolute() else root / t


def model_key_of(config: Any) -> str | None:
    """Encoder spec key (``vkm_corpus.embeddings.specs``) of a config: the name the RX580 service reports."""
    try:
        from vkm_corpus.embeddings.specs import SPECS
    except Exception:  # noqa: BLE001
        return None
    keys = [k for k, s in SPECS.items() if s.model_id == config.model_id and s.model_revision == config.model_revision
            and s.family in ("dense", "multi")]
    return keys[0] if len(keys) == 1 else None


def page_metadata(snapshot: Snapshot) -> dict[str, dict[str, Any]]:
    """page_id → page-level filter fields, from E's page documents of the snapshot (same values as the BM25 index)."""
    from vkm_corpus.graph.canon import ProjectionInput
    from vkm_corpus.search.documents import iter_documents

    inp = ProjectionInput.from_snapshot(snapshot)
    try:
        return {doc["page_id"]: {k: doc[k] for k in PAGE_FIELDS if k in doc} for doc in iter_documents(inp, "pages")}
    finally:
        inp.close()


def unit_document(unit: dict[str, Any], vector: Any, page: dict[str, Any], *, snapshot_id: str,
                  config_signature: str) -> dict[str, Any]:
    doc = {**page, "id": unit["unit_id"], "unit_kind": unit["kind"], "object_ids": list(unit["object_ids"]),
           "page_id": unit["page_id"], "source_id": unit["source_id"], "part": int(unit.get("part") or 0),
           "snapshot_id": snapshot_id, "config_signature": config_signature, "text_hash": unit["text_hash"],
           VECTOR_FIELD: vector.tolist()}
    if unit.get("work_id") and not doc.get("work_id"):
        doc["work_id"] = unit["work_id"]
    return {k: v for k, v in doc.items() if v is not None}


def alias_indices(client: Any, alias: str) -> list[str]:
    try:
        return sorted(client.indices.get_alias(name=alias).keys())
    except Exception as exc:  # noqa: BLE001 - NotFoundError of opensearch-py (or a fake)
        if type(exc).__name__ == "NotFoundError" or getattr(exc, "status_code", None) == 404:
            return []
        raise


def list_vector_builds(client: Any, prefix: str) -> list[dict[str, Any]]:
    mappings = client.indices.get_mapping(index=f"{prefix}-{VECTORS_SUFFIX}-*", params={"allow_no_indices": "true"})
    out = []
    for name, body in mappings.items():
        build_id = parse_vectors_index(prefix, name)
        if build_id is not None:
            out.append({"index": name, "build_id": build_id,
                        "meta": (body.get("mappings") or {}).get("_meta") or {}})
    return sorted(out, key=lambda b: b["build_id"])


def vectors_status(client: Any, prefix: str) -> dict[str, Any]:
    """Alias → index → ``_meta`` (snapshot, signature, model, dimension, count) — shown by ``search status``."""
    alias = vectors_alias(prefix)
    targets = alias_indices(client, alias)
    builds = list_vector_builds(client, prefix)
    entry: dict[str, Any] = {"alias": alias, "indices": targets, "builds": [b["build_id"] for b in builds]}
    if targets:
        meta = next((b["meta"] for b in builds if b["index"] == targets[0]), {})
        entry.update({k: meta.get(k) for k in ("build_id", "built_from_snapshot_id", "canonical_manifest_sha256",
                                               "config_signature", "model_id", "model_revision", "model_key",
                                               "quantization", "dimension", "normalization", "space_type",
                                               "storage_precision", "text_rule", "units_sha256", "build_status",
                                               "vector_count")})
        entry["count"] = int(client.count(index=alias)["count"])
    return entry


def _bulk(client: Any, index: str, docs: Iterable[dict[str, Any]], chunk: int) -> tuple[int, list[Any]]:
    ok, errors, batch = 0, [], []

    def flush() -> None:
        nonlocal ok, batch
        if not batch:
            return
        resp = client.bulk(body=batch, params={"refresh": "false"})
        for item in resp.get("items", []):
            res = item.get("create") or item.get("index") or {}
            if res.get("status") in (200, 201):
                ok += 1
            elif len(errors) < 20:
                errors.append({"id": res.get("_id"), "status": res.get("status"), "error": res.get("error")})
        batch = []

    for doc in docs:
        batch += [{"create": {"_index": index, "_id": doc["id"]}}, doc]
        if len(batch) >= 2 * chunk:
            flush()
    flush()
    return ok, errors


def prune_vector_builds(client: Any, prefix: str, keep: int = 2) -> list[str]:
    """Delete (exact names) vector builds older than the aliased one beyond ``keep - 1`` older COMPLETE builds."""
    current = set(alias_indices(client, vectors_alias(prefix)))
    builds = list_vector_builds(client, prefix)
    newest = max((parse_vectors_index(prefix, c) or "" for c in current), default=None)
    older_complete = [b["index"] for b in builds if b["index"] not in current
                      and b["meta"].get("build_status") == COMPLETE]
    kept = current | (set(older_complete[-(keep - 1):]) if keep > 1 else set())
    deleted = []
    for b in builds:
        if b["index"] in kept or (newest is not None and b["build_id"] > newest):
            continue                                       # a newer build may be in progress: never touch it
        client.indices.delete(index=b["index"])
        deleted.append(b["index"])
    return deleted


def build_vectors(settings: Settings, options: VectorBuildOptions, *, client: Any = None,
                  page_meta: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Checks + build + alias swap; returns the receipt (also written under receipts/projections/opensearch)."""
    from vkm_corpus.search.indexer import check_prefix
    from vkm_corpus.update.remote_search import validate_prepare_options, freeze_prepared_indices

    validate_prepare_options(options.publish, options.policy_sha256)
    prefix = check_prefix(options.prefix or settings.opensearch_index_prefix)
    root = require_canonical_root(settings)
    t0 = time.monotonic()
    receipt: dict[str, Any] = {"engine": "opensearch", "kind": "vectors", "prefix": prefix, "command": options.command,
                               "vectors_mapping_version": VECTORS_MAPPING_VERSION, "projector_version":
                               PROJECTOR_VERSION, "started_at": utc_now(), "status": "PLANNED"}
    created: list[str] = []
    with FileLock(root / LOCKS_DIR / f"opensearch-vectors-{prefix}.lock"):
        try:
            snapshot = load_snapshot(root, None)
            receipt["current_snapshot_id"] = snapshot.snapshot_id
            if options.snapshot_id and options.snapshot_id != snapshot.snapshot_id:
                raise ProjectionError("E_REFUSED", f"--snapshot {options.snapshot_id} is not CURRENT "
                                      f"({snapshot.snapshot_id}); vectors are built only for the current snapshot",
                                      stage="plan")
            art_dir = resolve_embeddings(options.embeddings, root)
            kind, config = read_config_dir(art_dir)
            if kind != "dense":
                raise ProjectionError("E_MODE_UNSUPPORTED", f"artifact kind {kind!r}: only dense vectors are projected",
                                      stage="plan")
            rule, variant = split_text_rule(config.text_rule)
            udir = Path(options.units) if options.units else units_dir(root, snapshot.snapshot_id, rule, variant)
            umeta, units = load_units(udir)
            if umeta.get("snapshot_id") != snapshot.snapshot_id:
                raise ProjectionError("E_REFUSED", f"units belong to snapshot {umeta.get('snapshot_id')}, "
                                      f"CURRENT is {snapshot.snapshot_id}", stage="plan")
            if umeta.get("text_rule") != config.text_rule:
                raise ProjectionError("E_REFUSED", f"units rule {umeta.get('text_rule')} != embedding text "
                                      f"rule {config.text_rule}", stage="plan")
            sig = config.signature()
            receipt["embeddings"] = {"config_signature": sig, "model_id": config.model_id,
                                     "model_revision": config.model_revision, "quantization": config.quantization,
                                     "dimension": config.dimension, "normalization": config.normalization,
                                     "storage_precision": config.storage_precision, "text_rule": config.text_rule,
                                     "model_key": model_key_of(config)}
            receipt["units"] = {k: umeta.get(k) for k in ("snapshot_id", "count", "by_kind", "units_sha256",
                                                          "text_rule")}
            receipt["mapping"] = {VECTOR_FIELD: knn_field(config)}
            if options.publish and options.skip_if_current and not options.plan_only:
                if client is None:
                    from vkm_corpus.search.client import connect

                    client = connect(settings)
                cur = vectors_status(client, prefix)
                if cur.get("build_status") == COMPLETE and cur.get("built_from_snapshot_id") == snapshot.snapshot_id                         and cur.get("config_signature") == sig and cur.get("units_sha256") == umeta.get("units_sha256"):
                    receipt.update({"status": "SKIPPED_CURRENT", "build_id": cur.get("build_id"),
                                    "index": (cur.get("indices") or [None])[0], "vectors": cur})
                    return receipt
            expected = {uid: u["text_hash"] for uid, u in units.items()}
            report, selection = check_artifacts(art_dir, config, expected, verify_checksums=options.verify_checksums)
            receipt["checks_64"] = report.as_dict()
            if not report.ok:
                raise ProjectionError("E_CHECK_FAILED", "pre-import checks (§64) failed; nothing was imported",
                                      stage="verify", details=report.as_dict())
            if options.plan_only:
                receipt["status"] = "PLAN_ONLY"
                return receipt
            meta_pages = page_meta if page_meta is not None else page_metadata(snapshot)
            no_page = sorted(uid for uid, u in units.items() if u["page_id"] not in meta_pages)
            if no_page:
                raise ProjectionError("E_DANGLING_REFERENCE", f"{len(no_page)} units point at pages missing from the "
                                      "snapshot", stage="verify", details={"examples": no_page[:20]})
            if client is None:
                from vkm_corpus.search.client import connect

                client = connect(settings)
            build_id = f"{make_build_id(snapshot.manifest_sha256)}-{sig[:8]}"
            taken = {b["build_id"] for b in list_vector_builds(client, prefix)}
            base, n = build_id, 1
            while build_id in taken:                    # two builds within one second: keep names unique and ordered
                n += 1
                build_id = f"{base}-{n}"
            index = vectors_index_name(prefix, build_id)
            receipt.update({"build_id": build_id, "index": index})
            manifests_sha = hashlib.sha256("".join(
                _sha256_file(m) for m in sorted(art_dir.glob("_manifest-*.json"))).encode()).hexdigest()
            meta = {"build_id": build_id, "built_from_snapshot_id": snapshot.snapshot_id,
                    "canonical_manifest_sha256": snapshot.manifest_sha256, "config_signature": sig,
                    "model_id": config.model_id, "model_revision": config.model_revision,
                    "model_key": model_key_of(config), "quantization": config.quantization,
                    "dimension": config.dimension, "normalization": config.normalization,
                    "space_type": knn_field(config)["method"]["space_type"],
                    "storage_precision": config.storage_precision, "text_rule": config.text_rule,
                    "units_sha256": umeta.get("units_sha256"), "artifact_manifests_sha256": manifests_sha,
                    "projector_version": PROJECTOR_VERSION, "prefix": prefix, "build_status": BUILDING}
            if options.policy_sha256 is not None:
                meta["policy_sha256"] = options.policy_sha256
            client.indices.create(index=index, body=vectors_body(config, meta))
            created.append(index)
            started = time.monotonic()
            rng = random.Random(build_id)
            sample: dict[str, list[float]] = {}

            def docs() -> Iterator[dict[str, Any]]:
                for uid, vec in iter_selected_vectors(art_dir, selection, config.storage_precision):
                    doc = unit_document(units[uid], vec, meta_pages[units[uid]["page_id"]],
                                        snapshot_id=snapshot.snapshot_id, config_signature=sig)
                    if len(sample) < options.sample and rng.random() < max(0.01, options.sample / max(1, len(units))):
                        sample[uid] = doc[VECTOR_FIELD]
                    yield doc

            ok, errors = _bulk(client, index, docs(), options.chunk_docs)
            receipt["timings_s"] = {"index": round(time.monotonic() - started, 3)}
            if errors or ok != len(selection):
                raise ProjectionError("E_BULK_FAILED", f"{len(errors)} failed bulk item(s), {ok} of {len(selection)} "
                                      "indexed", stage="index", details={"errors": errors[:20]})
            client.indices.refresh(index=index)
            client.indices.put_settings(index=index, body={"index": {"refresh_interval": None}})
            if options.force_merge:
                client.indices.forcemerge(index=index, params={"max_num_segments": 1})
                client.indices.refresh(index=index)
            count = int(client.count(index=index)["count"])
            problems = [] if count == len(expected) else [f"index holds {count} vectors, snapshot has "
                                                          f"{len(expected)} units"]
            if sample:
                found = client.mget(index=index, body={"ids": sorted(sample)})["docs"]
                for item in found:
                    got = (item.get("_source") or {}).get(VECTOR_FIELD)
                    want = sample.get(item["_id"])
                    if not item.get("found") or got is None or len(got) != len(want) or \
                            max(abs(float(a) - float(b)) for a, b in zip(got, want)) > 1e-6:
                        problems.append(f"sampled vector differs: {item['_id']}")
            receipt["checks"] = {"count": count, "expected": len(expected), "sampled": len(sample),
                                 "problems": problems[:20]}
            if problems:
                raise ProjectionError("E_COUNT_MISMATCH", "post-import checks failed", stage="verify",
                                      details={"problems": problems[:20]})
            client.indices.put_mapping(index=index, body={"_meta": {
                **meta, "vkm_vectors_mapping_version": VECTORS_MAPPING_VERSION, "build_status": COMPLETE,
                "vector_count": count, "completed_at": utc_now().isoformat()}})
            alias = vectors_alias(prefix)
            actions = [{"remove": {"index": old, "alias": alias}} for old in alias_indices(client, alias)
                       if old != index]
            actions.append({"add": {"index": index, "alias": alias}})
            if options.publish:
                client.indices.update_aliases(body={"actions": actions})
                receipt["alias_actions"] = actions
                receipt["pruned"] = prune_vector_builds(client, prefix, keep=options.keep_builds)
            else:
                freeze_prepared_indices(client, [index])
                receipt.update(alias_actions=[], pruned=[], publication="PREPARED_NOT_PUBLISHED", remote_qualification="NOT_RUN")
            receipt["status"] = COMPLETE
        except ProjectionError as exc:
            receipt["status"] = "FAILED"
            receipt["error"] = exc.as_dict()
            _drop(client, created, options, receipt)
            raise
        except Exception as exc:
            receipt["status"] = "FAILED"
            receipt["error"] = {"code": "E_INTERNAL", "stage": "index", "message": f"{type(exc).__name__}: {exc}"}
            _drop(client, created, options, receipt)
            raise
        finally:
            receipt["finished_at"] = utc_now()
            receipt.setdefault("timings_s", {})["total"] = round(time.monotonic() - t0, 3)
            run_id = receipt.get("build_id") or f"plan-{int(time.time())}"
            receipt["receipt_ref"] = write_receipt(root, "opensearch", f"{prefix}-vectors-{run_id}", receipt)
    return receipt


def _drop(client: Any, created: list[str], options: VectorBuildOptions, receipt: dict[str, Any]) -> None:
    """Delete the indices of a failed build by exact name (the alias was never moved)."""
    if client is None or not created or options.keep_failed:
        return
    deleted = []
    for name in created:
        try:
            client.indices.delete(index=name)
            deleted.append(name)
        except Exception as exc:  # noqa: BLE001 - the receipt records it; the original error is raised
            receipt.setdefault("delete_errors", []).append(f"{name}: {type(exc).__name__}")
    receipt["deleted_failed_indices"] = deleted

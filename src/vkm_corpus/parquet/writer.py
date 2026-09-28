"""Atomic writer of immutable Parquet partitions (one file per partition).

The writer itself enforces what pyarrow does not: required (non-nullable) columns without nulls, unique primary
key, rows sorted by the dataset sort key, contract schema and ``vkm.*`` key-value metadata. Settings (checked in
phase 0): zstd level 3, dictionary encoding, statistics, format 2.6, data page v1, row groups of 65 536, stored Arrow
schema — the same rows with the same pyarrow version give byte-identical files.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from vkm_corpus.contracts import arrow as ca
from vkm_corpus.contracts.datasets import VOLATILE_COLUMNS, dataset
from vkm_corpus.parquet.atomic import sha256_of, write_with
from vkm_corpus.parquet.layout import CanonLayout
from vkm_corpus.versions import PIPELINE_VERSION

WRITER_OPTIONS = dict(compression="zstd", compression_level=3, use_dictionary=True, write_statistics=True,
                      version="2.6", data_page_version="1.0", row_group_size=65536, store_schema=True)


class DuplicateKeyError(ValueError):
    """Two rows share a primary key inside one partition (ID_COLLISION or a producer bug)."""


@dataclass(frozen=True)
class FileEntry:
    """A written partition, as listed in commit markers, run markers and snapshot manifests."""

    dataset: str
    path: str                 # relative to canonical/
    sha256: str
    bytes: int
    rows: int
    schema_version: str
    schema_fingerprint: str
    digest: str               # RowDigest.hex() over all columns
    content_digest: str       # RowDigest.hex() without volatile columns

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "FileEntry":
        return cls(**{k: data[k] for k in cls.__dataclass_fields__})

    @property
    def row_digest(self) -> ca.RowDigest:
        return ca.RowDigest.parse(self.rows, self.digest)

    @property
    def content_row_digest(self) -> ca.RowDigest:
        return ca.RowDigest.parse(self.rows, self.content_digest)


def to_table(name: str, rows: Iterable[Any]):
    """Rows (models or dicts) or an existing pyarrow Table → Table with the contract schema."""
    import pyarrow as pa

    if isinstance(rows, pa.Table):
        table = rows.select(list(dataset(name).fields))
        ca.check_required_columns(table, dataset(name))        # before cast: pyarrow's own message is unclear
        return table.cast(ca.arrow_schema(name))
    return ca.rows_to_table(name, rows)


def sort_table(name: str, table):
    spec = dataset(name)
    if table.num_rows == 0:
        return table
    return table.sort_by([(k, "ascending") for k in spec.sort_key])


def check_primary_key(name: str, table) -> None:
    spec = dataset(name)
    cols = [table.column(k).to_pylist() for k in spec.primary_key]
    keys = list(zip(*cols)) if cols else []
    if len(set(keys)) != len(keys):
        seen, dups = set(), []
        for k in keys:
            if k in seen:
                dups.append(k)
            seen.add(k)
        raise DuplicateKeyError(f"{name}: duplicate primary key(s) {dups[:5]}")


def write_table_file(layout: CanonLayout, name: str, table, rel_path: str, *,
                     run_id: str, source_id: str | None = None, extra_kv: dict[str, str] | None = None) -> FileEntry:
    """Write one partition file atomically and describe it."""
    import pyarrow.parquet as pq

    spec = dataset(name)
    if not spec.stored:
        raise ValueError(f"{name} is a derived view and is never stored")
    table = sort_table(name, to_table(name, table))
    check_primary_key(name, table)
    kv = dict(table.schema.metadata or {})
    kv.update({b"vkm.pipeline_version": PIPELINE_VERSION.encode(), b"vkm.processing_run_id": run_id.encode()})
    if source_id:
        kv[b"vkm.source_id"] = source_id.encode()
    for k, v in (extra_kv or {}).items():
        kv[f"vkm.{k}".encode()] = str(v).encode()
    table = table.replace_schema_metadata(kv)
    sorting = [pq.SortingColumn(table.schema.get_field_index(spec.sort_key[0]))] if table.num_rows else None
    target = layout.path(rel_path)

    def _write(tmp: Path) -> None:
        pq.write_table(table, tmp, sorting_columns=sorting, **WRITER_OPTIONS)

    write_with(layout.tmp, target, _write)
    digest = ca.digest_table(name, table)
    content = ca.digest_table(name, table, VOLATILE_COLUMNS)
    return FileEntry(dataset=name, path=rel_path, sha256=sha256_of(target), bytes=target.stat().st_size,
                     rows=table.num_rows, schema_version=spec.version, schema_fingerprint=ca.schema_fingerprint(name),
                     digest=digest.hex(), content_digest=content.hex())


def write_partition(layout: CanonLayout, name: str, rows: Iterable[Any], *, run_id: str, source_id: str | None = None,
                    part: int | None = None, scope: str | None = None, file_name: str | None = None) -> FileEntry:
    """Write rows of ``name`` into its partition directory; ``part=None`` takes the next free part number."""
    if part is None and file_name is None:
        part = next_part(layout, name, run_id, source_id, scope)
    rel = layout.partition(name, run_id, source_id, part or 0, scope, file_name)
    return write_table_file(layout, name, rows, rel, run_id=run_id, source_id=source_id)


def next_part(layout: CanonLayout, name: str, run_id: str, source_id: str | None, scope: str | None) -> int:
    probe = layout.path(layout.partition(name, run_id, source_id, 0, scope)).parent
    if not probe.is_dir():
        return 0
    used = [int(p.stem.split("-")[1]) for p in probe.glob("part-*.parquet") if p.stem.split("-")[1].isdigit()]
    return max(used) + 1 if used else 0


def read_kv(path: Path) -> dict[str, str]:
    import pyarrow.parquet as pq

    md = pq.read_schema(path).metadata or {}
    return {k.decode(): v.decode() for k, v in md.items() if k.startswith(b"vkm.")}

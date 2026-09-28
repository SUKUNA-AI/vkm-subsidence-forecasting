"""Reading a snapshot: manifest, ``CURRENT``, dataset tables strictly from the manifest's file lists (never a glob)."""
from __future__ import annotations

import json
from typing import Any

from vkm_corpus.contracts import arrow as ca
from vkm_corpus.contracts.datasets import dataset
from vkm_corpus.contracts.vocab import RootKind
from vkm_corpus.parquet.layout import CanonLayout, RootError


def current_snapshot_id(layout: CanonLayout) -> str | None:
    p = layout.canonical / layout.CURRENT
    return p.read_text(encoding="utf-8").strip() or None if p.is_file() else None


def load_manifest(layout: CanonLayout, snapshot_id: str | None = None, *, candidate: bool = False) -> dict[str, Any]:
    """The manifest of ``snapshot_id`` (default: ``CURRENT``) of a CANONICAL root."""
    layout.require(RootKind.CANONICAL)
    sid = snapshot_id or current_snapshot_id(layout)
    if sid is None:
        raise RootError("no CURRENT snapshot in this root")
    path = layout.path(layout.snapshot_manifest(sid, candidate=candidate))
    return json.loads(path.read_text(encoding="utf-8"))


def dataset_files(manifest: dict[str, Any], name: str) -> list[dict[str, Any]]:
    return list(manifest.get("datasets", {}).get(name, {}).get("files", []))


def read_dataset(layout: CanonLayout, name: str, manifest: dict[str, Any]):
    """pyarrow Table of a dataset: union of the manifest files, contract column order, missing columns as NULL."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    schema = ca.arrow_schema(name)
    tables = []
    for f in dataset_files(manifest, name):
        t = pq.read_table(layout.path(f["path"]))
        cols = []
        for field in schema:
            if field.name in t.column_names:
                cols.append(t.column(field.name).cast(field.type))
            else:
                cols.append(pa.nulls(t.num_rows, type=field.type))
        extra = set(t.column_names) - set(schema.names)
        if extra:
            raise ValueError(f"{f['path']}: columns unknown to this code version: {sorted(extra)}")
        tables.append(pa.Table.from_arrays(cols, schema=schema.remove_metadata()))
    if not tables:
        return schema.empty_table()
    return pa.concat_tables(tables)


def rows(layout: CanonLayout, name: str, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    dataset(name)
    return read_dataset(layout, name, manifest).to_pylist()

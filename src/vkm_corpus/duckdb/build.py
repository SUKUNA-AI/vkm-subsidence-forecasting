"""DuckDB query layer rebuilt from a snapshot manifest (task §25, §46; H-07, H-17, H-48).

* ``attach_manifest(con, layout, manifest)`` — materialises ``canonical.<dataset>`` strictly from the manifest's files
  (``read_parquet([...], union_by_name=true)`` inserted BY NAME into a table created from the contract's Arrow
  schema: missing columns of older files become NULL, unknown columns are an error), plus ``meta.snapshot``,
  ``meta.commits``, ``meta.dataset_files``;
* ``apply_sql(con)`` — executes ``duckdb/sql/*.sql`` in name order (views, derived rules, status, access macros);
  the graph/search projector runs exactly these files (H-17);
* ``open_snapshot(layout)`` — in-memory connection over a snapshot (validator, projectors, tests);
* ``build_duckdb(layout)`` — builds ``duckdb/vkm_corpus.duckdb`` in a temporary file, checks the fingerprint of every
  table against the manifest (same algorithm, H-48) and only then replaces the file. Refused on STAGING roots.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

from vkm_corpus.contracts import arrow as ca
from vkm_corpus.contracts.datasets import STORED_DATASETS
from vkm_corpus.contracts.vocab import RootKind
from vkm_corpus.parquet.layout import CanonLayout
from vkm_corpus.parquet.reader import current_snapshot_id, load_manifest

SQL_DIR = Path(__file__).resolve().parent / "sql"


class FingerprintMismatch(RuntimeError):
    """A materialised table differs from the snapshot manifest (the DuckDB file is not replaced)."""


def sql_files() -> list[Path]:
    return sorted(SQL_DIR.glob("*.sql"))


def apply_sql(con) -> list[str]:
    applied = []
    for f in sql_files():
        con.execute(f.read_text(encoding="utf-8"))
        applied.append(f.name)
    return applied


def _lit(path: Path) -> str:
    return "'" + path.as_posix().replace("'", "''") + "'"


def attach_manifest(con, layout: CanonLayout, manifest: dict[str, Any], *, manifest_sha256: str = "") -> None:
    con.execute("CREATE SCHEMA IF NOT EXISTS canonical")
    con.execute("CREATE SCHEMA IF NOT EXISTS meta")
    for name in STORED_DATASETS:
        empty = ca.empty_table(name)
        con.register("_vkm_empty", empty)
        con.execute(f'CREATE OR REPLACE TABLE canonical."{name}" AS SELECT * FROM _vkm_empty')
        con.unregister("_vkm_empty")
        files = manifest.get("datasets", {}).get(name, {}).get("files", [])
        if files:
            paths = ", ".join(_lit(layout.path(f["path"])) for f in files)
            con.execute(f'INSERT INTO canonical."{name}" BY NAME SELECT * FROM read_parquet([{paths}], '
                        f"union_by_name = true, hive_partitioning = false)")
    snap_id = manifest.get("snapshot_id") or "CANDIDATE"
    con.execute("CREATE OR REPLACE TABLE meta.snapshot AS SELECT ?::VARCHAR AS snapshot_id, "
                "?::VARCHAR AS manifest_sha256, now() AS built_at, version() AS duckdb_version, "
                "?::VARCHAR AS pipeline_version",
                [snap_id, manifest_sha256, str(manifest.get("code", {}).get("pipeline_version", ""))])
    heads = dict(manifest.get("source_heads", {}))
    if manifest.get("registry_head"):
        heads["REGISTRY"] = manifest["registry_head"]
    con.execute("CREATE OR REPLACE TABLE meta.commits (commit_key VARCHAR, commit_id VARCHAR)")
    if heads:
        con.executemany("INSERT INTO meta.commits VALUES (?, ?)", sorted(heads.items()))
    con.execute("CREATE OR REPLACE TABLE meta.dataset_files (dataset VARCHAR, path VARCHAR, sha256 VARCHAR, "
                "rows BIGINT, schema_version VARCHAR)")
    rows = [(n, f["path"], f["sha256"], f["rows"], f.get("schema_version"))
            for n, d in sorted(manifest.get("datasets", {}).items()) for f in d.get("files", [])]
    if rows:
        con.executemany("INSERT INTO meta.dataset_files VALUES (?, ?, ?, ?, ?)", rows)


def connect(path: str | Path | None = None, read_only: bool = False):
    import duckdb

    return duckdb.connect(str(path) if path else ":memory:", read_only=read_only)


def open_snapshot(layout: CanonLayout, manifest: dict[str, Any] | None = None, snapshot_id: str | None = None):
    """In-memory DuckDB over a snapshot (default: CURRENT) with all views and macros."""
    manifest = manifest or load_manifest(layout, snapshot_id)
    con = connect()
    attach_manifest(con, layout, manifest)
    apply_sql(con)
    return con


def table_fingerprints(con) -> dict[str, str]:
    """Fingerprint (all columns) of every materialised canonical table, streamed in record batches."""
    out = {}
    for name in STORED_DATASETS:
        reader = con.execute(f'SELECT * FROM canonical."{name}"').to_arrow_reader(65536)
        out[name] = ca.fingerprint_of(name, ca.digest_batches(name, reader))
    return out


def verify_fingerprints(con, manifest: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """Datasets whose DuckDB table differs from the manifest: name → (expected, actual)."""
    actual = table_fingerprints(con)
    bad = {}
    for name in STORED_DATASETS:
        expected = manifest.get("datasets", {}).get(name, {}).get("table_fingerprint")
        if expected is not None and expected != actual[name]:
            bad[name] = (expected, actual[name])
    return bad


def build_duckdb(layout: CanonLayout, snapshot_id: str | None = None) -> dict[str, Any]:
    """Build ``duckdb/vkm_corpus.duckdb`` from a snapshot (default CURRENT) of a CANONICAL root."""
    layout.require(RootKind.CANONICAL)
    sid = snapshot_id or current_snapshot_id(layout)
    manifest = load_manifest(layout, sid)
    mpath = layout.path(layout.snapshot_manifest(manifest["snapshot_id"]))
    msha = hashlib.sha256(mpath.read_bytes()).hexdigest()
    layout.duckdb_dir.mkdir(parents=True, exist_ok=True)
    tmp = layout.duckdb_dir / f".vkm_corpus.{uuid.uuid4().hex}.tmp.duckdb"
    try:
        con = connect(tmp)
        try:
            attach_manifest(con, layout, manifest, manifest_sha256=msha)
            applied = apply_sql(con)
            bad = verify_fingerprints(con, manifest)
            if bad:
                raise FingerprintMismatch(f"tables differ from manifest {manifest['snapshot_id']}: {sorted(bad)}")
            counts = {n: con.execute(f'SELECT count(*) FROM canonical."{n}"').fetchone()[0] for n in STORED_DATASETS}
            con.execute("CHECKPOINT")
        finally:
            con.close()
        os.replace(tmp, layout.duckdb_file)
    finally:
        tmp.unlink(missing_ok=True)
        Path(str(tmp) + ".wal").unlink(missing_ok=True)
    return {"snapshot_id": manifest["snapshot_id"], "manifest_sha256": msha, "sql_files": applied,
            "table_rows": counts, "fingerprints_verified": True}


def duckdb_status(layout: CanonLayout) -> dict[str, Any]:
    """Which snapshot the DuckDB file was built from, versus CURRENT (H-48: /status compares them)."""
    out: dict[str, Any] = {"current": current_snapshot_id(layout), "duckdb_file": layout.duckdb_file.is_file()}
    if out["duckdb_file"]:
        con = connect(layout.duckdb_file, read_only=True)
        try:
            row = con.execute("SELECT snapshot_id, manifest_sha256, built_at::VARCHAR, duckdb_version "
                              "FROM meta.snapshot").fetchone()
        finally:
            con.close()
        out.update(dict(zip(["snapshot_id", "manifest_sha256", "built_at", "duckdb_version"], row)))
        out["up_to_date"] = out["snapshot_id"] == out["current"]
    return out


def dump_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True, default=str)

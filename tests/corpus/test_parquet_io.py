"""Parquet writer: round trip through pyarrow / polars / DuckDB, key-value metadata, byte determinism, refusals
(nulls in required columns, duplicate keys, overwriting), older-schema files read with union_by_name."""
from __future__ import annotations

import pytest

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")
duckdb = pytest.importorskip("duckdb")

from vkm_corpus.contracts import arrow as ca  # noqa: E402
from vkm_corpus.parquet.atomic import ImmutableFileError, write_bytes  # noqa: E402
from vkm_corpus.parquet.layout import init_root  # noqa: E402
from vkm_corpus.parquet.writer import DuplicateKeyError, read_kv, write_partition  # noqa: E402
from vkm_corpus.testing import rows as R  # noqa: E402

RUN = R.RUN_ID


@pytest.fixture()
def layout(tmp_path):
    return init_root(tmp_path / "root", "STAGING")


def _pages():
    return [R.make_page(index=i, text=f"страница {i} альфа") for i in (2, 1, 3)]


def test_round_trip_types_and_values(layout):
    entry = write_partition(layout, "pages", _pages(), run_id=RUN, source_id=R.SID)
    path = layout.path(entry.path)
    t = pq.read_table(path)
    assert t.schema.field("created_at").type == pa.timestamp("us", tz="UTC")
    assert t.schema.field("page_index").type == pa.int32() and t.schema.field("rotation_deg").type == pa.int16()
    assert t.column("page_index").to_pylist() == [1, 2, 3]                      # sorted by the sort key
    assert t.schema.field("models").type == pa.list_(pa.struct([pa.field("role", pa.string(), False),
                                                                pa.field("model_id", pa.string(), False),
                                                                pa.field("model_revision", pa.string(), False)]))
    row = t.to_pylist()[0]
    assert row["page_id"] == "VKM-SRC-001:p0001" and row["created_at"].tzinfo is not None
    con = duckdb.connect()
    rel = con.execute(f"SELECT page_index, typeof(created_at), typeof(quality_flags) FROM read_parquet('{path.as_posix()}') "
                      "ORDER BY 1").fetchall()
    assert rel[0][1] == "TIMESTAMP WITH TIME ZONE" and rel[0][2] == "VARCHAR[]"
    polars = pytest.importorskip("polars")
    df = polars.read_parquet(path)
    assert df.height == 3 and str(df.schema["created_at"]).startswith("Datetime")


def test_key_value_metadata_and_entry(layout):
    entry = write_partition(layout, "pages", _pages(), run_id=RUN, source_id=R.SID)
    kv = read_kv(layout.path(entry.path))
    assert kv["vkm.dataset"] == "pages" and kv["vkm.schema_version"] == "0.1.0"
    assert kv["vkm.schema_fingerprint"] == ca.schema_fingerprint("pages")
    assert kv["vkm.processing_run_id"] == RUN and kv["vkm.source_id"] == R.SID
    assert entry.rows == 3 and entry.path == f"pages/source_id={R.SID}/run={RUN}/part-00000.parquet"


def test_same_rows_give_identical_bytes_and_digests(tmp_path):
    a = init_root(tmp_path / "a", "STAGING")
    b = init_root(tmp_path / "b", "STAGING")
    ea = write_partition(a, "pages", _pages(), run_id=RUN, source_id=R.SID)
    eb = write_partition(b, "pages", list(reversed(_pages())), run_id=RUN, source_id=R.SID)
    assert ea.sha256 == eb.sha256 and ea.digest == eb.digest and ea.content_digest == eb.content_digest


def test_digest_is_additive_over_files():
    rows = [p.model_dump() for p in _pages()]
    whole = ca.digest_rows("pages", rows)
    parts = ca.digest_rows("pages", rows[:1]) + ca.digest_rows("pages", rows[1:])
    assert whole == parts
    assert ca.fingerprint_of("pages", whole) != ca.fingerprint_of("pages", ca.digest_rows("pages", rows[:2]))


def test_null_in_required_column_is_refused_before_writing(layout):
    table = ca.rows_to_table("pages", _pages())
    idx = table.schema.get_field_index("page_status")
    broken = table.set_column(idx, pa.field("page_status", pa.string()), pa.array([None, None, None], pa.string()))
    with pytest.raises(ca.NullInRequiredColumn):
        write_partition(layout, "pages", broken, run_id=RUN, source_id=R.SID)
    assert not list((layout.canonical / "pages").rglob("*.parquet"))
    assert not list(layout.tmp.glob("*.tmp"))


def test_duplicate_primary_key_is_refused(layout):
    with pytest.raises(DuplicateKeyError):
        write_partition(layout, "pages", [R.make_page(index=1), R.make_page(index=1)], run_id=RUN, source_id=R.SID)


def test_partitions_are_immutable(layout):
    target = layout.canonical / "x.bin"
    assert write_bytes(layout.tmp, target, b"one") is True
    assert write_bytes(layout.tmp, target, b"one") is False          # same bytes: idempotent no-op
    with pytest.raises(ImmutableFileError):
        write_bytes(layout.tmp, target, b"two")
    assert target.read_bytes() == b"one"


def test_next_part_numbers_do_not_overwrite(layout):
    e1 = write_partition(layout, "processing_steps", [], run_id=RUN)
    e2 = write_partition(layout, "processing_steps", [], run_id=RUN)
    assert e1.path.endswith("part-00000.parquet") and e2.path.endswith("part-00001.parquet")


def test_older_schema_file_is_read_by_name_with_nulls(layout):
    """An additive schema change: a file without a nullable column is read with NULLs (never silently dropped)."""
    from vkm_corpus.duckdb.build import attach_manifest
    from vkm_corpus.parquet.reader import read_dataset

    table = ca.rows_to_table("pages", _pages()).drop_columns(["native_ocr_cer"])
    rel = f"pages/source_id={R.SID}/run={RUN}/part-00000.parquet"
    path = layout.path(rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)
    manifest = {"datasets": {"pages": {"files": [{"path": rel, "sha256": "", "rows": 3}]}}}
    t = read_dataset(layout, "pages", manifest)
    assert t.column("native_ocr_cer").null_count == 3 and t.num_rows == 3
    con = duckdb.connect()
    attach_manifest(con, layout, manifest)
    assert con.execute("SELECT count(*), count(native_ocr_cer) FROM canonical.pages").fetchone() == (3, 0)

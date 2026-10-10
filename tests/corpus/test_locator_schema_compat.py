"""Exact 0.1.0 schema read support; original digest is preserved across NULL padding and mixed snapshots."""
import hashlib
import json

import pytest

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")
duckdb = pytest.importorskip("duckdb")

from vkm_corpus.contracts import arrow as ca
from vkm_corpus.contracts.datasets import (DATASETS, HISTORICAL_LOCATOR_SCHEMAS, HISTORICAL_CONTINUATION_SCHEMAS,
    historical_omissions)
from vkm_corpus.contracts.builders import content_sha256
from vkm_corpus.contracts.models import ENVELOPE_FIELDS
from vkm_corpus.duckdb.build import attach_manifest, verify_fingerprints, FingerprintMismatch
from vkm_corpus.parquet.layout import init_root
from vkm_corpus.parquet.validator import Validator
from vkm_corpus.testing import rows as R


def _old_file(layout, name, row, historical=None):
    old_version, old_fp = historical or HISTORICAL_LOCATOR_SCHEMAS[name]
    omitted = historical_omissions(name, old_version)
    old_description = [field for field in ca.schema_description(name) if field[0] not in omitted]
    assert hashlib.sha256(json.dumps(old_description, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest() == old_fp
    row = {**row.model_dump(), "schema_version": old_version}
    for field in omitted:
        row.pop(field, None)
    schema = ca.arrow_schema(name)
    for field in omitted:
        schema = schema.remove(schema.get_field_index(field))
    # An independent version-appropriate content hash, not the new schema's
    # nullable padding. Original bytes/IDs and both digests remain unchanged.
    payload = {k: ca.canonical_value(row.get(k)) for k in schema.names
        if k not in ENVELOPE_FIELDS[DATASETS[name].profile]}
    row["content_sha256"] = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
        separators=(",", ":")).encode()).hexdigest()
    assert content_sha256(name, row) == row["content_sha256"]
    metadata = dict(schema.metadata)
    metadata[b"vkm.schema_version"] = old_version.encode()
    metadata[b"vkm.schema_fingerprint"] = old_fp.encode()
    path = layout.path(f"{name}/legacy.parquet")
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist([row], schema=schema.with_metadata(metadata))
    pq.write_table(table, path)
    # Independently compute the old implementation's row hash and column-set fingerprint.
    encoded = ca.canonical_row(row, schema.names).encode()
    digest = ca.RowDigest(1, int.from_bytes(hashlib.sha256(encoded).digest(), "big"))
    assert ca.digest_rows(name, [{**row, **{field: None for field in omitted}}]) == digest
    entry = {"path": f"{name}/legacy.parquet", "bytes": path.stat().st_size,
             "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "rows": 1, "digest": digest.hex(),
             "schema_version": old_version, "schema_fingerprint": old_fp}
    fp_payload = f"vkm-fp-v2|table|{name}|{','.join(schema.names)}|1|{digest.hex()}"
    old_table_fp = hashlib.sha256(fp_payload.encode()).hexdigest()
    return entry, digest, old_table_fp


@pytest.mark.parametrize("name,factory", [("tables", R.make_table), ("figures", R.make_figure), ("formulas", R.make_formula)])
def test_historical_files_read_with_original_hashes(tmp_path, name, factory):
    layout = init_root(tmp_path / "canon", "CANONICAL")
    entry, digest, old_table_fp = _old_file(layout, name, factory())
    old_version, old_fp = HISTORICAL_LOCATOR_SCHEMAS[name]
    manifest = {"datasets": {name: {"files": [entry], "schema_version": old_version,
                 "schema_fingerprint": old_fp, "table_fingerprint": old_table_fp}}}
    con = duckdb.connect()
    attach_manifest(con, layout, manifest)
    assert con.execute(f'SELECT raw_locator FROM canonical."{name}"').fetchone() == (None,)
    assert verify_fingerprints(con, manifest) == {}
    validator = Validator(layout, manifest)
    validator.check_files()
    assert all(c.violations == 0 for c in validator.checks if c.check_id in ("A01", "A02", "A05"))
    # A newly produced snapshot can mix old immutable row digests and new locators.
    new_row = factory(page_index=2, raw_locator="/synthetic/item[2]").model_dump()
    new_table = ca.rows_to_table(name, [new_row])
    con.register("new_rows", new_table)
    con.execute(f'INSERT INTO canonical."{name}" BY NAME SELECT * FROM new_rows')
    mixed = digest + ca.digest_table(name, new_table)
    manifest["datasets"][name].update(schema_version=DATASETS[name].version,
                                      schema_fingerprint=ca.schema_fingerprint(name),
                                      table_fingerprint=ca.fingerprint_of(name, mixed))
    assert verify_fingerprints(con, manifest) == {}
    manifest["datasets"][name]["schema_fingerprint"] = "0" * 64
    with pytest.raises(FingerprintMismatch, match="schema"):
        verify_fingerprints(con, manifest)
    con.close()


def test_historical_row_cannot_smuggle_new_locator():
    with pytest.raises(ValueError, match="0.1.1"):
        ca.digest_rows("tables", [{"schema_version": "0.1.0", "raw_locator": "/hidden/new/field"}])
    assert not ca.readable_schema("tables", "0.0.9", HISTORICAL_LOCATOR_SCHEMAS["tables"][1])
    assert not ca.readable_schema("tables", "0.1.0", "0" * 64)


def test_pre_continuation_files_keep_original_content_and_file_fingerprints(tmp_path):
    layout = init_root(tmp_path / "canon", "CANONICAL")
    historical = HISTORICAL_CONTINUATION_SCHEMAS["tables"]
    entry, digest, table_fp = _old_file(layout, "tables", R.make_table(raw_locator="/synthetic/table[1]"), historical)
    manifest = {"datasets": {"tables": {"files": [entry], "schema_version": historical[0],
        "schema_fingerprint": historical[1], "table_fingerprint": table_fp}}}
    con = duckdb.connect()
    attach_manifest(con, layout, manifest)
    assert con.execute("SELECT continuation_provenance FROM canonical.tables").fetchone() == (None,)
    assert verify_fingerprints(con, manifest) == {}
    validator = Validator(layout, manifest)
    validator.check_files()
    assert all(c.violations == 0 for c in validator.checks if c.check_id in ("A01", "A02", "A05"))
    row = con.execute("SELECT * FROM canonical.tables").to_arrow_table().to_pylist()[0]
    assert content_sha256("tables", row) == row["content_sha256"]
    assert ca.digest_rows("tables", [row]) == digest
    row["continuation_provenance"] = {"forged": "payload"}
    with pytest.raises(ValueError, match="historical"):
        ca.digest_rows("tables", [row])
    with pytest.raises(ValueError, match="historical"):
        content_sha256("tables", row)
    assert not ca.readable_schema("tables", "0.1.1", "0" * 64)
    assert not ca.readable_schema("tables", "0.1.3", historical[1])
    with pytest.raises(ValueError, match="unknown"):
        ca.fingerprint_of("tables", digest, schema_version="0.1.3")
    con.close()

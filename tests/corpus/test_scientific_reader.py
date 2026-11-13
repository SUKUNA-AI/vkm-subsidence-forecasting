"""Contract regressions for the optional read-only scientific file reader.

Tiny synthetic rows test protocol and freshness only. Scientific calculations
and native workstation readback are verified by separate, source-bound cases.
"""
from pathlib import Path
import hashlib
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from vkm_corpus.api.errors import ApiFailure
from vkm_corpus.api.scientific import ScientificReader


def build_snapshot(tmp_path, *, role="MODEL_GEOLOGY", sql="SELECT name FROM intervals", table_name="intervals"):
    data = tmp_path / (table_name + ".parquet")
    pq.write_table(pa.table({"name": ["synthetic_interval"]}), data)
    manifest = {
        "schema": "vkm.scientific-query-snapshot/1",
        "scope": "SYNTHETIC_PROTOCOL_TEST",
        "snapshot_id": "synthetic-science-snapshot",
        "source_snapshot_id": "synthetic-document-snapshot",
        "allowed_token_labels": ["read"],
        "tables": {table_name: {"path": data.name, "data_role": role, "rows": 1,
                                "sha256": hashlib.sha256(data.read_bytes()).hexdigest()}},
        "queries": {"intervals": {"sql": sql}},
    }
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def reader(path, digest, source=lambda: "synthetic-document-snapshot"):
    return ScientificReader(path, digest, source)


def test_named_native_query_reads_bound_parquet(tmp_path):
    path, digest = build_snapshot(tmp_path)
    result = reader(path, digest).query("intervals", "synthetic-science-snapshot")
    assert result["rows"] == [{"name": "synthetic_interval"}]
    assert result["row_count"] == 1
    assert result["result_role"] == "DERIVED_PROVENANCE_QUERY"
    assert result["scientific_admission"] == "NOT_GRANTED_BY_READER"


def test_simulation_table_cannot_be_relabeled_as_observation(tmp_path):
    path, digest = build_snapshot(tmp_path, role="OBSERVATION_INDEX")
    with pytest.raises(ValueError, match="cannot become REAL measurements"):
        reader(path, digest)


@pytest.mark.parametrize("table_name,role", [
    ("stage_arrays", "MODEL_STAGE_ARRAY_CATALOG"),
    ("stage_values", "MODEL_STAGE_NUMERIC_VALUES"),
    ("storage", "STORAGE_AVAILABILITY_METADATA"),
])
def test_numeric_stage_reader_keeps_model_and_storage_roles(tmp_path, table_name, role):
    path, digest = build_snapshot(tmp_path, table_name=table_name, role=role,
                                  sql="SELECT name FROM " + table_name)
    instance = reader(path, digest)
    assert instance.status()['tables'][table_name]['data_role'] == role
    assert instance.query('intervals', 'synthetic-science-snapshot')['row_count'] == 1


@pytest.mark.parametrize("table_name", ["stage_arrays", "stage_values", "storage"])
def test_stage_and_storage_rows_cannot_be_promoted_to_observations(tmp_path, table_name):
    path, digest = build_snapshot(tmp_path, table_name=table_name, role="OBSERVATION_INDEX",
                                  sql="SELECT name FROM " + table_name)
    with pytest.raises(ValueError, match="cannot become REAL measurements"):
        reader(path, digest)


def test_nonselect_operator_query_rejected(tmp_path):
    path, digest = build_snapshot(tmp_path, sql="DELETE FROM intervals")
    with pytest.raises(ValueError, match="SELECT"):
        reader(path, digest)


def test_operator_query_cannot_read_unbound_files(tmp_path):
    outside = tmp_path / "unbound.parquet"
    pq.write_table(pa.table({"name": ["untracked_value"]}), outside)
    sql = "SELECT name FROM read_parquet('" + outside.as_posix() + "')"
    path, digest = build_snapshot(tmp_path, sql=sql)
    with pytest.raises(ValueError, match="only bound snapshot tables"):
        reader(path, digest)


def test_source_snapshot_change_closes_existing_reader(tmp_path):
    path, digest = build_snapshot(tmp_path)
    current = ["synthetic-document-snapshot"]
    instance = reader(path, digest, source=lambda: current[0])
    current[0] = "synthetic-new-document-snapshot"
    with pytest.raises(ApiFailure, match="source snapshot is stale"):
        instance.query("intervals", "synthetic-science-snapshot")


def test_client_snapshot_cannot_silently_switch(tmp_path):
    path, digest = build_snapshot(tmp_path)
    with pytest.raises(ApiFailure, match="client scientific snapshot is stale"):
        reader(path, digest).query("intervals", "synthetic-old-snapshot")


def test_input_hash_change_detected_after_startup(tmp_path):
    path, digest = build_snapshot(tmp_path)
    instance = reader(path, digest)
    pq.write_table(pa.table({"name": ["changed_synthetic_interval"]}),
                   tmp_path / "intervals.parquet")
    with pytest.raises(ApiFailure, match="table changed"):
        instance.query("intervals", "synthetic-science-snapshot")


def test_manifest_change_detected_after_startup(tmp_path):
    path, digest = build_snapshot(tmp_path)
    instance = reader(path, digest)
    path.write_text(path.read_text() + "\n", encoding="utf-8")
    with pytest.raises(ApiFailure, match="manifest changed"):
        instance.status()


def test_snapshot_cannot_open_parent_paths(tmp_path):
    path, _ = build_snapshot(tmp_path)
    manifest = json.loads(path.read_bytes())
    manifest["tables"]["intervals"]["path"] = "../outside.parquet"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="contained"):
        reader(path, digest)

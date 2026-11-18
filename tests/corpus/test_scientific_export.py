"""HTTP export against the real ScientificReader and a tiny DuckDB/Parquet snapshot."""

from datetime import date
from decimal import Decimal
import hashlib
import json

from fastapi.testclient import TestClient
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from vkm_corpus.api.app import ApiConfig, create_app
from vkm_corpus.api.fixtures import SNAPSHOT_ID, synthetic_service
from vkm_corpus.api.scientific_snapshot_verification import encoded


SNAPSHOT = "scientific-test"
URL = "/v1/science/export/series"
TOKEN = "scoped-fixture-read"
HEADERS = {"Authorization": "Bearer " + TOKEN}
COLUMNS = [
    ("bucket", "BIGINT"), ("point_id", "BIGINT"), ("value", "DECIMAL(38,19)"),
    ("observed", "DATE"), ("evidence", "BLOB"),
]
CHART_VALUE = Decimal("1234567890123456789.0123456789012345678")


def fixture(tmp_path, *, row_count=513, key=("bucket", "point_id"), wrong_digest=False,
            expected_count=None, payload=b"\xff\x00",
            sql="SELECT bucket, point_id, value, observed, evidence FROM observations"):
    rows = [(i // 4, i, CHART_VALUE if i % 2 else None, date(2025, 1, 3), payload)
            for i in range(row_count)]
    schema = pa.schema([("bucket", pa.int64()), ("point_id", pa.int64()), ("value", pa.decimal128(38, 19)),
                        ("observed", pa.date32()), ("evidence", pa.binary())])
    parquet = tmp_path / "observations.parquet"
    pq.write_table(pa.Table.from_pylist([dict(zip(schema.names, row)) for row in reversed(rows)], schema=schema),
                   parquet)
    h = hashlib.sha256(encoded(COLUMNS) + b"\n")
    for row in rows:
        h.update(encoded(row) + b"\n")
    contract = {
        "order_by": list(key), "row_count": row_count if expected_count is None else expected_count,
        "ordered_sha256": "0" * 64 if wrong_digest else h.hexdigest(),
    }
    snapshot = {
        "schema": "vkm.scientific-query-snapshot/1", "scope": "SYNTHETIC_EXPORT_TEST",
        "snapshot_id": SNAPSHOT, "source_snapshot_id": SNAPSHOT_ID,
        "allowed_token_labels": ["read"],
        "tables": {"observations": {
            "data_role": "OBSERVATION_INDEX", "sha256": hashlib.sha256(parquet.read_bytes()).hexdigest(),
            "path": parquet.name, "rows": row_count,
        }},
        "queries": {
            "series": {"sql": sql, "export": contract},
            "preview_only": {"sql": "SELECT point_id FROM observations"},
        },
    }
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(snapshot), encoding="utf-8")
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    service, _, _ = synthetic_service(tmp_path / "source")
    config = ApiConfig(
        read_tokens={TOKEN: "read", "denied": "other"}, scientific_manifest_path=str(manifest),
        scientific_manifest_sha256=digest,
    )
    app = create_app(service, config)
    return TestClient(app), config, manifest, {"snapshot_id": SNAPSHOT, "manifest_sha256": digest}, rows, contract


def records(response):
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].partition(";")[0] == "application/x-ndjson"
    return [json.loads(line) for line in response.iter_lines()]


def test_complete_export_passes_preview_limit_with_stable_ties_and_typed_values(tmp_path):
    client, _, _, params, rows, contract = fixture(tmp_path)
    preview = client.get("/v1/science/query/preview_only", params={"snapshot_id": SNAPSHOT, "limit": 500},
                         headers=HEADERS)
    assert preview.status_code == 200
    assert preview.json()["scientific"]["truncated"] is True
    response = client.get(URL, params=params, headers=HEADERS)
    header, *data, seal = records(response)
    assert header["type"] == "metadata"
    assert header["columns"] == json.loads(encoded(COLUMNS))
    assert header["manifest_sha256"] == params["manifest_sha256"]
    assert header["order_by"] == contract["order_by"]
    assert all(item["type"] == "row" for item in data)
    assert [item["values"] for item in data] == [json.loads(encoded(row)) for row in rows]
    assert seal == {"type": "complete", "row_count": len(rows), "ordered_sha256": contract["ordered_sha256"]}
    assert client.get(URL, params=params, headers=HEADERS).content == response.content


def test_empty_result_is_explicitly_complete(tmp_path):
    client, _, _, params, _, contract = fixture(tmp_path, row_count=0)
    header, seal = records(client.get(URL, params=params, headers=HEADERS))
    assert header["type"] == "metadata"
    assert seal == {"type": "complete", "row_count": 0, "ordered_sha256": contract["ordered_sha256"]}


def test_no_preview_only_export_and_cardinality_bound_request_identity(tmp_path):
    client, _, manifest, params, _, _ = fixture(tmp_path, row_count=5)
    assert client.get("/v1/science/export/preview_only", params=params, headers=HEADERS).status_code == 404
    assert client.get(URL, params={**params, "manifest_sha256": "0" * 64}, headers=HEADERS).status_code == 400
    assert client.get(URL, params={**params, "snapshot_id": "old"}, headers=HEADERS).status_code == 400
    assert client.get(URL, params={**params, "offset": 1}, headers=HEADERS).status_code == 400
    manifest.write_text(manifest.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert client.get(URL, params=params, headers=HEADERS).status_code == 503


def test_retriable_request_never_restores_revoked_or_foreign_access(tmp_path):
    client, config, _, params, _, _ = fixture(tmp_path, row_count=3)
    assert records(client.get(URL, params=params, headers=HEADERS))[-1]["type"] == "complete"
    assert client.get(URL, params=params, headers={"Authorization": "Bearer denied"}).status_code == 403
    del config.read_tokens[TOKEN]
    assert client.get(URL, params=params, headers=HEADERS).status_code == 401


def test_revocation_between_batches_stops_stream_without_success_seal(tmp_path):
    client, config, _, params, _, _ = fixture(tmp_path, row_count=260)

    class Policy:
        checks = 0

        def require_served_corpus(self, context):
            self.checks += 1
            if self.checks == 5:
                raise PermissionError("fixture revoked the read context")

    client.app.state.service.deps.access_policy = Policy()
    config.access_contexts["read"] = object()
    header, *data, stop = records(client.get(URL, params=params, headers=HEADERS))
    assert header["type"] == "metadata"
    assert len(data) == 128 and all(item["type"] == "row" for item in data)
    assert stop == {"type": "incomplete", "reason": "FORBIDDEN"}


@pytest.mark.parametrize("input_name", ["manifest.json", "observations.parquet"])
def test_changed_input_after_first_batch_never_completes(tmp_path, input_name):
    client, config, _, params, _, _ = fixture(tmp_path, row_count=260)
    target = tmp_path / input_name

    class Policy:
        checks = 0

        def require_served_corpus(self, context):
            self.checks += 1
            if self.checks == 5:
                target.write_bytes(target.read_bytes() + b"changed during export")

    client.app.state.service.deps.access_policy = Policy()
    config.access_contexts["read"] = object()
    metadata, *rows, stop = records(client.get(URL, params=params, headers=HEADERS))
    assert metadata["type"] == "metadata"
    assert len(rows) >= 128 and all(row["type"] == "row" for row in rows)
    assert stop == {"type": "incomplete", "reason": "SNAPSHOT_UNAVAILABLE"}


@pytest.mark.parametrize(("kwargs", "reason"), [
    ({"key": ("bucket",)}, "INVALID_RESULT_IDENTITY"),
    ({"sql": "SELECT bucket, point_id, value, observed, evidence, value AS VALUE FROM observations"},
     "AMBIGUOUS_RESULT_COLUMNS"),
    ({"wrong_digest": True}, "RESULT_PROOF_MISMATCH"),
    ({"expected_count": 8}, "RESULT_PROOF_MISMATCH"),
    ({"expected_count": 10}, "RESULT_PROOF_MISMATCH"),
    ({"payload": b"a" * 600_000}, "RESULT_ROW_LIMIT"),
])
def test_incomplete_result_never_carries_a_success_seal(tmp_path, kwargs, reason):
    client, _, _, params, _, _ = fixture(tmp_path, row_count=9, **kwargs)
    result = records(client.get(URL, params=params, headers=HEADERS))
    assert not any(item["type"] == "complete" for item in result)
    assert result[-1] == {"type": "incomplete", "reason": reason}

"""Synthetic contract tests; real private geometry is exercised in private receipts."""
import asyncio
import json
import struct

import pytest

from vkm_geometry.db_adapter import (COLUMNS, Conflict, SCHEMA, SCOPE, TABLES, _assert_rows, _current,
    _ddl, _scope, apply_batch, build_query_snapshot, canonical, digest, prepare_batch, readback, verify_plan)


def fixture_plan(count=3, snapshot="synthetic-source-snapshot"):
    rows, features = [], []
    for index in range(count):
        feature = {"source_feature_id": f"synthetic:{index:04d}", "source_id": "VKM-SRC-011",
                   "work_id": "VKM-WRK-011", "geometry": {"type": "Point", "coordinates": [index, index + 1]},
                   "image_coordinate_frame": {"id": "synthetic-pixel", "unit": "px", "crs": None},
                   "figure_id": "1.25", "panel_id": "a", "pdf_page": 12, "horizon": None,
                   "extraction_status": "PARTIAL", "review_status": "AUTO_EXTRACTED_UNREVIEWED",
                   "primitive_role": "OBSERVED_MARK", "epistemic_role": "DERIVATION",
                   "source_image": "private:synthetic.png", "source_image_sha256": "0" * 64}
        features.append(feature)
        row = dict(zip(COLUMNS[:-1], ("synthetic-batch", feature["source_feature_id"], "VKM-SRC-011", "VKM-WRK-011",
            "synthetic-pixel", "px", "Point", struct.pack("<BIdd", 1, 1, index, index + 1).hex(), canonical(feature), "[]", "{}")))
        row["row_sha256"] = digest(row)
        rows.append(row)
    batch = {"batch_id": "synthetic-batch", "extraction_version": "test-v1", "features": features,
             "project_matches": [], "provenance": {}}
    for row in rows:
        row["batch_provenance_json"] = canonical({k: v for k, v in batch.items() if k not in {"features", "project_matches"}})
        row["row_sha256"] = digest({k: row[k] for k in COLUMNS[:-1]})
    registry = {"source_snapshot_id": snapshot, "sources": [{"record": {"source_id": "VKM-SRC-011"}}],
                "works": [{"record": {"work_id": "VKM-WRK-011"}}],
                "source_work_links": [{"source_id": "VKM-SRC-011", "work_id": "VKM-WRK-011", "curation_status": "AUTO_PROPOSED"}]}
    return prepare_batch(batch, registry, rows=rows), batch, registry, rows


def test_source_work_reference_is_not_guessed():
    _, batch, registry, rows = fixture_plan()
    registry["source_work_links"] = []
    with pytest.raises(ValueError, match="source/work reference"):
        prepare_batch(batch, registry, rows=rows)


def test_plan_and_transport_tampering_rejected():
    plan, batch, registry, rows = fixture_plan()
    plan["rows"][0]["coordinate_unit"] = "m"
    with pytest.raises(ValueError, match="digest mismatch"):
        verify_plan(plan)
    rows[0]["wkb_hex"] = rows[1]["wkb_hex"]
    with pytest.raises(ValueError, match="row SHA mismatch"):
        prepare_batch(batch, registry, rows=rows)


@pytest.mark.parametrize("changed", ["vertices", "xyz", "nan", "frame", "unit", "type", "provenance", "matches"])
def test_resigned_supplied_rows_cannot_change_source_semantics(changed):
    _, batch, registry, rows = fixture_plan()
    if changed == "vertices":
        rows[0]["wkb_hex"] = struct.pack("<BIdd", 1, 1, 8, 9).hex()
    elif changed == "xyz":
        rows[0]["wkb_hex"] = struct.pack("<BIddd", 1, 1001, 0, 1, 2).hex()
    elif changed == "nan":
        rows[0]["wkb_hex"] = struct.pack("<BIdd", 1, 1, float("nan"), 1).hex()
    elif changed == "frame":
        rows[0]["frame_id"] = "another-frame"
    elif changed == "unit":
        rows[0]["coordinate_unit"] = "m"
    elif changed == "type":
        rows[0]["geometry_type"] = "LineString"
    elif changed == "provenance":
        rows[0]["batch_provenance_json"] = '{"other":"batch"}'
    else:
        rows[0]["matches_json"] = '[{"match_status":"CONFIRMED"}]'
    rows[0]["row_sha256"] = digest({k: rows[0][k] for k in COLUMNS[:-1]})
    with pytest.raises(ValueError):
        prepare_batch(batch, registry, rows=rows)


@pytest.mark.parametrize("changed", ["vertices", "provenance", "matches", "reference"])
def test_rehashed_crafted_plan_is_rejected_before_database_or_snapshot(changed, tmp_path):
    plan, *_ = fixture_plan()
    row = plan["rows"][0]
    if changed == "vertices":
        row["wkb_hex"] = struct.pack("<BIdd", 1, 1, 8, 9).hex()
    elif changed == "provenance":
        row["batch_provenance_json"] = "{}"
    elif changed == "matches":
        row["matches_json"] = '[{"match_status":"CONFIRMED"}]'
    else:
        plan["source_refs"][0]["work_id"] = "VKM-WRK-999"
    row["row_sha256"] = digest({key: row[key] for key in COLUMNS[:-1]})
    plan["content_digest"] = digest({key: value for key, value in plan.items() if key != "content_digest"})
    with pytest.raises(ValueError):
        verify_plan(plan)
    with pytest.raises(ValueError):
        apply_batch(plan, None, None, scope=SCOPE)  # would crash if DB access preceded validation
    with pytest.raises(ValueError):
        build_query_snapshot(plan, tmp_path / "must_not_exist", allowed_token_labels=["read"])
    assert not (tmp_path / "must_not_exist").exists()


def test_postgres_write_scope_rejects_production():
    class Connection:
        class info:
            host, dbname = "edge", "vkm_ops"
    with pytest.raises(ValueError, match="isolated"):
        _scope(Connection(), SCOPE)


@pytest.mark.duckdb_spatial
def test_duckdb_reference_wkb_idempotence_and_conflict(tmp_path):
    import duckdb
    plan, *_ = fixture_plan()
    c = duckdb.connect(str(tmp_path / "isolated.duckdb"))
    c.execute("LOAD spatial")
    c.execute("CREATE SCHEMA scientific_r3_20261112")
    _ddl(c, False)
    assert not _current(c, False, plan)
    c.execute("INSERT INTO scientific_r3_20261112.geometry_batch_projection VALUES(?,?,?,?,?)", [plan[k] for k in ("batch_id", "content_digest", "extraction_version", "registry_sha256", "batch_json")])
    for ref in plan["source_refs"]:
        c.execute("INSERT INTO scientific_r3_20261112.geometry_source_ref_projection VALUES(?,?,?,?)", [plan["batch_id"], ref["source_id"], ref["work_id"], ref["reference_json"]])
    for row in plan["rows"]:
        c.execute("INSERT INTO scientific_r3_20261112.source_geometry_features VALUES(" + ",".join("?" for _ in COLUMNS) + ")", [row[k] for k in COLUMNS])
    assert _current(c, False, plan)
    assert _assert_rows(c, False, plan) == plan["rows"]
    c.execute("UPDATE scientific_r3_20261112.source_geometry_features SET coordinate_unit='m'")
    with pytest.raises(Conflict, match="feature rows differ"):
        _current(c, False, plan)
    c.close()


def test_snapshot_named_partitions_traverse_more_than_500(tmp_path):
    from vkm_corpus.api.scientific import ScientificReader
    plan, *_ = fixture_plan(503)
    result = build_query_snapshot(plan, tmp_path / "snapshot", allowed_token_labels=["read"])
    reader = ScientificReader(result["manifest_path"], result["manifest_sha256"], lambda: plan["source_snapshot_id"])
    catalogue = [q for q in result["queries"] if q["facet"] == "all"]
    ids = []
    for query in catalogue:
        response = reader.query(query["query_id"], result["snapshot_id"], query["required_limit"])
        assert not response["truncated"]
        assert all("wkb_hex" not in r and "feature_json" not in r for r in response["rows"])
        assert all(r["horizon"] is None for r in response["rows"])
        ids.extend(row["source_feature_id"] for row in response["rows"])
    assert len(ids) == len(set(ids)) == 503
    assert reader.query(catalogue[0]["query_id"], result["snapshot_id"], 1)["truncated"]
    chunks = [q for q in result["queries"] if q["facet"] == "feature_wkb" and q["value"] == ids[0]]
    hex_back = "".join(reader.query(q["query_id"], result["snapshot_id"], 1)["rows"][0]["wkb_hex_chunk"] for q in chunks)
    assert hex_back == plan["rows"][0]["wkb_hex"]


def test_unknown_and_complex_horizon_keep_null_and_raw_basis(tmp_path):
    from vkm_corpus.api.scientific import ScientificReader
    _, batch, registry, rows = fixture_plan()
    batch["features"][1]["horizon"] = "UNKNOWN"
    batch["features"][2]["horizon"] = {"candidate": "seam-X", "basis": "UNCONFIRMED_LABEL"}
    for row, feature in zip(rows, batch["features"]):
        row["feature_json"] = canonical(feature)
        row["row_sha256"] = digest({k: row[k] for k in COLUMNS[:-1]})
    plan = prepare_batch(batch, registry, rows=rows)
    out = build_query_snapshot(plan, tmp_path / "snapshot", allowed_token_labels=["read"])
    reader = ScientificReader(out["manifest_path"], out["manifest_sha256"], lambda: plan["source_snapshot_id"])
    unknown = next(q for q in out["queries"] if q["facet"] == "horizon" and q["value"] is None)
    cards = reader.query(unknown["query_id"], out["snapshot_id"], 500)["rows"]
    assert len(cards) == 2 and all(card["horizon"] is None for card in cards)
    assert {card["horizon_json"] for card in cards} == {"null", canonical(batch["features"][2]["horizon"])}
    named = next(q for q in out["queries"] if q["facet"] == "horizon" and q["value"] == "UNKNOWN")
    assert reader.query(named["query_id"], out["snapshot_id"], 500)["row_count"] == 1


def test_bound_cad_annex_keeps_A_unchanged_and_changes_snapshot_identity(tmp_path):
    plan, *_ = fixture_plan()
    before = canonical(plan)
    original = build_query_snapshot(plan, tmp_path / "plain", allowed_token_labels=["read"])
    links = {plan["rows"][0]["source_feature_id"]: [{"uri": "private:job/readback.dwg", "kind": "NATIVE_DWG", "sha256": "a" * 64}]}
    with_cad = build_query_snapshot(plan, tmp_path / "cad", allowed_token_labels=["read"], artifact_links=links)
    assert canonical(plan) == before and original["snapshot_id"] != with_cad["snapshot_id"]
    with pytest.raises(ValueError, match="unknown feature"):
        build_query_snapshot(plan, tmp_path / "bad", allowed_token_labels=["read"], artifact_links={"missing": []})
    with pytest.raises(ValueError, match="SHA-256"):
        build_query_snapshot(plan, tmp_path / "badsha", allowed_token_labels=["read"], artifact_links={plan["rows"][0]["source_feature_id"]: [{"uri": "x", "kind": "DWG"}]})


def test_combined_projection_keeps_producer_batch_ids(tmp_path):
    from vkm_corpus.api.scientific import ScientificReader
    first, batch, registry, rows = fixture_plan(2)
    batch["batch_id"] = "other-producer-batch"
    for row, feature in zip(rows, batch["features"]):
        feature["source_feature_id"] += ":second"
        row["source_feature_id"] = feature["source_feature_id"]
        row["feature_json"] = canonical(feature)
        row["batch_id"] = batch["batch_id"]
        row["batch_provenance_json"] = canonical({k: v for k, v in batch.items() if k not in {"features", "project_matches"}})
        row["row_sha256"] = digest({k: row[k] for k in COLUMNS[:-1]})
    second = prepare_batch(batch, registry, rows=rows)
    snap = build_query_snapshot([first, second], tmp_path / "combined", allowed_token_labels=["read"])
    reader = ScientificReader(snap["manifest_path"], snap["manifest_sha256"], lambda: first["source_snapshot_id"])
    query = next(q for q in snap["queries"] if q["facet"] == "all")
    cards = reader.query(query["query_id"], snap["snapshot_id"], 500)["rows"]
    assert len(cards) == 4 and {card["batch_id"] for card in cards} == {first["batch_id"], second["batch_id"]}
    with pytest.raises(ValueError, match="unique batch IDs"):
        build_query_snapshot([first, first], tmp_path / "bad", allowed_token_labels=["read"])


def test_existing_mcp_handshake_queries_and_api_principals(tmp_path):
    import httpx
    from mcp import Client
    from vkm_corpus.api.app import ApiConfig, create_app
    from vkm_corpus.api.fixtures import synthetic_service, SNAPSHOT_ID
    from vkm_corpus.mcp.api_client import ApiClient
    from vkm_corpus.mcp.servers import build_read_server
    plan, *_ = fixture_plan(snapshot=SNAPSHOT_ID)
    snap = build_query_snapshot(plan, tmp_path / "snapshot", allowed_token_labels=["geometry-read"])
    service, _, _ = synthetic_service(tmp_path / "canon")
    good, other = "geometry-test-read-token-000000000000", "geometry-other-read-token-00000000000"
    app = create_app(service, ApiConfig(read_tokens={good: "geometry-read", other: "other"},
        scientific_manifest_path=snap["manifest_path"], scientific_manifest_sha256=snap["manifest_sha256"]))
    query = next(q for q in snap["queries"] if q["facet"] == "source_id")
    async def scenario():
        api = ApiClient("http://isolated-api", good, transport=httpx.ASGITransport(app=app))
        async with Client(build_read_server(api)) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert tools["scientific_query"].annotations.read_only_hint
            status = await client.call_tool("scientific_status", {})
            assert not status.is_error and status.structured_content["scientific"]["query_catalogue"]
            result = await client.call_tool("scientific_query", {"query_id": query["query_id"], "snapshot_id": snap["snapshot_id"], "limit": 500})
            assert not result.is_error and result.structured_content["scientific"]["row_count"] == 3
            assert result.structured_content["scientific"]["scientific_admission"] == "NOT_GRANTED_BY_READER"
            stale = await client.call_tool("scientific_query", {"query_id": query["query_id"], "snapshot_id": "stale"})
            assert stale.is_error
            invalid = await client.call_tool("scientific_query", {"query_id": "SELECT * FROM x", "snapshot_id": snap["snapshot_id"]})
            assert invalid.is_error
        await api.aclose()
        for token in (other, None):
            denied = ApiClient("http://isolated-api", token, transport=httpx.ASGITransport(app=app))
            response = await denied.call("GET", "/v1/science/status")
            assert not response["ok"]
            await denied.aclose()
    asyncio.run(scenario())


class _FakeResult:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _FakePG:
    """Existing-row PostgreSQL protocol; batch_json is driver-decoded like JSON/JSONB."""

    def __init__(self, plan):
        self.record = [plan[key] for key in ("content_digest", "extraction_version", "registry_sha256", "batch_json")]
        self.record[3] = json.loads(plan["batch_json"])
        self.rows = [tuple(row[key] for key in COLUMNS) for row in plan["rows"]]
        self.refs = [(ref["source_id"], ref["work_id"], ref["reference_json"]) for ref in plan["source_refs"]]
        self.duplicate = False
        self.commits = 0
        self.rollbacks = 0
        self.info = type("Info", (), {"host": "127.0.0.1", "dbname": "vkm_geo_test", "transaction_status": 0})()

    def execute(self, sql, params=()):
        if sql.startswith("SELECT table_name FROM information_schema.tables"):
            return _FakeResult()
        if sql.startswith("SELECT count(*) FROM information_schema.tables"):
            return _FakeResult([(1,)])
        if sql.startswith("SELECT count(*) FROM information_schema.schemata"):
            return _FakeResult([(1,)])
        if sql.startswith("SELECT content_digest,extraction_version,registry_sha256,batch_json FROM "):
            records = [] if self.record is None else [tuple(self.record)]
            return _FakeResult(records * (2 if self.duplicate else 1))
        if sql.startswith("SELECT " + ",".join(COLUMNS) + " FROM "):
            return _FakeResult(self.rows)
        if sql.startswith("SELECT source_id,work_id,reference_json FROM "):
            return _FakeResult(self.refs)
        if sql.startswith("SELECT bool_and("):
            return _FakeResult([(True,)])
        if sql.startswith("SELECT count(*) FROM "):
            return _FakeResult([(1 if self.rows or self.refs else 0,)])
        if sql.startswith(("BEGIN", "SET ", "CREATE TABLE ")):
            return _FakeResult()
        raise AssertionError(f"unexpected fake PostgreSQL SQL: {sql}")

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


def _seed_native(plan, path):
    import duckdb
    connection = duckdb.connect(str(path))
    connection.execute("LOAD spatial")
    connection.execute(f"CREATE SCHEMA {SCHEMA}")
    _ddl(connection, False)
    connection.execute(f"INSERT INTO {SCHEMA}.{TABLES[0]} VALUES(?,?,?,?,?)",
                       [plan[key] for key in ("batch_id", "content_digest", "extraction_version", "registry_sha256", "batch_json")])
    for ref in plan["source_refs"]:
        connection.execute(f"INSERT INTO {SCHEMA}.{TABLES[1]} VALUES(?,?,?,?)",
                           [plan["batch_id"], ref["source_id"], ref["work_id"], ref["reference_json"]])
    for row in plan["rows"]:
        connection.execute(f"INSERT INTO {SCHEMA}.{TABLES[2]} VALUES(" + ",".join("?" for _ in COLUMNS) + ")",
                           [row[key] for key in COLUMNS])
    connection.commit()
    connection.close()


def _native_record(path, plan):
    import duckdb
    connection = duckdb.connect(str(path))
    result = connection.execute(
        f"SELECT content_digest,extraction_version,registry_sha256,batch_json FROM {SCHEMA}.{TABLES[0]} WHERE batch_id=?",
        [plan["batch_id"]]).fetchone()
    connection.close()
    return result


def _update_native(path, plan, field, value):
    import duckdb
    connection = duckdb.connect(str(path))
    connection.execute(f"UPDATE {SCHEMA}.{TABLES[0]} SET {field}=? WHERE batch_id=?", [value, plan["batch_id"]])
    connection.commit()
    connection.close()


@pytest.mark.duckdb_spatial
@pytest.mark.parametrize("field", ["content_digest", "extraction_version", "registry_sha256", "batch_json"])
@pytest.mark.parametrize("operation", ["fresh_readback", "repeat_apply"])
def test_every_persisted_metadata_field_fails_closed_without_overwrite(tmp_path, field, operation):
    import duckdb
    plan, *_ = fixture_plan()
    path = tmp_path / "native.duckdb"
    _seed_native(plan, path)
    replacements = {"content_digest": "0" * 64, "extraction_version": "test-v2", "registry_sha256": "f" * 64}
    if field == "batch_json":
        changed = json.loads(plan["batch_json"])
        changed["provenance"] = {"semantically": "changed"}
        value = canonical(changed)
    else:
        value = replacements[field]
    _update_native(path, plan, field, value)
    damaged = _native_record(path, plan)
    if operation == "fresh_readback":
        with pytest.raises(Conflict):
            readback(plan, lambda: _FakePG(plan), lambda: duckdb.connect(str(path)))
    else:
        pg = _FakePG(plan)
        dd = duckdb.connect(str(path))
        try:
            with pytest.raises(Conflict):
                apply_batch(plan, pg, dd, commit=True, scope=SCOPE)
        finally:
            dd.close()
        assert pg.commits == 0 and pg.rollbacks == 1
    assert _native_record(path, plan) == damaged


@pytest.mark.duckdb_spatial
@pytest.mark.parametrize("kind", ["missing", "duplicate", "corrupt", "null", "json_null", "scalar", "array", "semantic"])
@pytest.mark.parametrize("operation", ["fresh_readback", "repeat_apply"])
def test_missing_duplicate_and_invalid_batch_records_fail_closed(tmp_path, kind, operation):
    plan, *_ = fixture_plan()
    path = tmp_path / "native.duckdb"
    _seed_native(plan, path)
    pg = _FakePG(plan)
    if kind == "missing":
        pg.record = None
    elif kind == "duplicate":
        pg.duplicate = True
    elif kind == "corrupt":
        pg.record[3] = "{"
    elif kind == "null":
        pg.record[3] = None
    elif kind == "json_null":
        pg.record[3] = "null"
    elif kind == "scalar":
        pg.record[3] = "1"
    elif kind == "array":
        pg.record[3] = "[]"
    else:
        changed = json.loads(plan["batch_json"])
        changed["features"][0]["image_coordinate_frame"]["unit"] = "m"
        pg.record[3] = changed
    import duckdb
    if operation == "fresh_readback":
        with pytest.raises(Conflict):
            readback(plan, lambda: pg, lambda: duckdb.connect(str(path)))
    else:
        dd = duckdb.connect(str(path))
        try:
            with pytest.raises(Conflict):
                apply_batch(plan, pg, dd, commit=True, scope=SCOPE)
        finally:
            dd.close()
        assert pg.commits == 0 and pg.rollbacks == 1


def _rich_plan():
    _, batch, registry, rows = fixture_plan()
    batch["provenance"] = {"source": {"B": {"quantity": {"value": 1, "unit": "px"}}},
                           "unknown": {"nested": [{"enabled": True}, 1, "1"]}}
    batch["quantity"] = {"value": 1.25, "frame": {"id": "nested-frame", "keep": False}}
    batch["project_matches"] = [{"source_feature_id": batch["features"][0]["source_feature_id"],
                                 "match_status": "AUTO_PROPOSED", "quantity": {"value": 2, "unit": "px"},
                                 "unknown_b": {"frame": {"id": "b-frame"}, "keep": [False, 1]}}]
    for row in rows:
        row["matches_json"] = canonical([match for match in batch["project_matches"]
                                         if match["source_feature_id"] == row["source_feature_id"]])
        row["batch_provenance_json"] = canonical({key: value for key, value in batch.items()
                                                   if key not in {"features", "project_matches"}})
        row["row_sha256"] = digest({key: row[key] for key in COLUMNS[:-1]})
    return prepare_batch(batch, registry, rows=rows)


def _reverse_objects(value):
    if isinstance(value, dict):
        return {key: _reverse_objects(value[key]) for key in reversed(list(value))}
    if isinstance(value, list):
        return [_reverse_objects(item) for item in value]
    return value


@pytest.mark.duckdb_spatial
@pytest.mark.parametrize("reordered", [False, True])
def test_original_repeat_and_reordered_pg_dict_duckdb_text_are_equivalent(tmp_path, reordered):
    import duckdb
    plan = _rich_plan()
    path = tmp_path / "native.duckdb"
    _seed_native(plan, path)
    stored = json.loads(plan["batch_json"])
    if reordered:
        stored = _reverse_objects(stored)
    text = json.dumps(stored, ensure_ascii=False, indent=2) if reordered else canonical(stored)
    _update_native(path, plan, "batch_json", text)

    def pg_factory():
        pg = _FakePG(plan)
        pg.record[3] = stored
        return pg

    pg = pg_factory()
    dd = duckdb.connect(str(path))
    try:
        receipt = apply_batch(plan, pg, dd, commit=True, scope=SCOPE)
    finally:
        dd.close()
    assert receipt["postgres_inserted"] == receipt["duckdb_inserted"] == 0
    result = readback(plan, pg_factory, lambda: duckdb.connect(str(path)))
    assert result["status"] == "PASS" and result["rows"] == len(plan["rows"])


@pytest.mark.duckdb_spatial
def test_nested_json_boolean_true_is_not_integer_one(tmp_path):
    import duckdb
    plan = _rich_plan()
    path = tmp_path / "native.duckdb"
    _seed_native(plan, path)
    pg = _FakePG(plan)
    pg.record[3]["provenance"]["unknown"]["nested"][0]["enabled"] = 1
    with pytest.raises(Conflict, match="batch_json differs"):
        readback(plan, lambda: pg, lambda: duckdb.connect(str(path)))



@pytest.mark.duckdb_spatial
@pytest.mark.parametrize('kind', ['missing', 'corrupt', 'sql_null', 'json_null', 'scalar', 'array'])
@pytest.mark.parametrize('operation', ['fresh_readback', 'repeat_apply'])
def test_native_duckdb_missing_invalid_or_schema_protected_record(tmp_path, kind, operation):
    import duckdb
    plan, *_ = fixture_plan()
    path = tmp_path / 'owner.duckdb'
    _seed_native(plan, path)
    if kind == 'sql_null':
        # Current real schema already prevents SQL NULL. Verify that constraint
        # and the unchanged valid-record positive path; never drop NOT NULL.
        before = _native_record(path, plan)
        c = duckdb.connect(str(path))
        try:
            with pytest.raises(duckdb.ConstraintException, match='NOT NULL'):
                c.execute(f'UPDATE {SCHEMA}.{TABLES[0]} SET batch_json=NULL WHERE batch_id=?', [plan['batch_id']])
        finally:
            c.close()
        assert _native_record(path, plan) == before
        pg = _FakePG(plan)
        if operation == 'fresh_readback':
            assert readback(plan, lambda: pg, lambda: duckdb.connect(str(path)))['status'] == 'PASS'
        else:
            dd = duckdb.connect(str(path))
            try:
                result = apply_batch(plan, pg, dd, commit=True, scope=SCOPE)
            finally:
                dd.close()
            assert result['postgres_inserted'] == result['duckdb_inserted'] == 0
        return
    if kind == 'missing':
        c = duckdb.connect(str(path))
        c.execute(f'DELETE FROM {SCHEMA}.{TABLES[0]} WHERE batch_id=?', [plan['batch_id']])
        c.close()
    else:
        replacement = {'corrupt': '{', 'sql_null': None, 'json_null': 'null', 'scalar': '1', 'array': '[]'}[kind]
        _update_native(path, plan, 'batch_json', replacement)
    damaged = _native_record(path, plan)
    pg = _FakePG(plan)
    if operation == 'fresh_readback':
        with pytest.raises(Conflict):
            readback(plan, lambda: pg, lambda: duckdb.connect(str(path)))
    else:
        dd = duckdb.connect(str(path))
        try:
            with pytest.raises(Conflict):
                apply_batch(plan, pg, dd, commit=True, scope=SCOPE)
        finally:
            dd.close()
        assert pg.commits == 0 and pg.rollbacks == 1
    assert _native_record(path, plan) == damaged

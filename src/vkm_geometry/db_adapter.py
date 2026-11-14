"""Additive A/B batch projection beside R3; never a canonical registry.

Writes are restricted to explicitly acknowledged isolated local test databases.
The caller owns connections and receipts. PostgreSQL and DuckDB commits are
separate; PartialApply exposes which side committed, instead of claiming atomicity.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

SCHEMA = "scientific_r3_20261112"
SCOPE = "ISOLATED_LOCAL_PRIVATE_TEST"
TABLES = ("geometry_batch_projection", "geometry_source_ref_projection", "source_geometry_features")
COLUMNS = ("batch_id", "source_feature_id", "source_id", "work_id", "frame_id", "coordinate_unit",
           "geometry_type", "wkb_hex", "feature_json", "matches_json", "batch_provenance_json", "row_sha256")


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Conflict(ValueError):
    """A key already contains other bytes/semantics; no replacement is permitted."""


class PartialApply(RuntimeError):
    def __init__(self, receipt, error):
        self.receipt = receipt
        super().__init__(f"Two-database operation incomplete: {receipt['state']}; {type(error).__name__}")


def prepare_batch(batch, registry, *, rows=None):
    """Build an immutable-content plan; registry is existing corpus reference data.

    Registry shape: source_snapshot_id, sources/works as get_source/get_work items,
    source_work_links as source_id/work_id records. It is a versioned validation
    input and a batch-local FK target, not a newly curated source/work registry.
    """
    if rows is None:
        from .transport import batch_rows
        rows = batch_rows(batch)
    rows = [{key: row[key] for key in COLUMNS} for row in rows]
    if not rows or len({row["source_feature_id"] for row in rows}) != len(rows):
        raise ValueError("nonempty unique source_feature_id required")
    if not registry.get("source_snapshot_id"):
        raise ValueError("registry reference requires source_snapshot_id")
    sources = {item["record"]["source_id"]: item for item in registry["sources"]}
    works = {item["record"]["work_id"]: item for item in registry["works"]}
    links = {(r["source_id"], r["work_id"]): r for r in registry["source_work_links"]
             if r.get("curation_status") != "REJECTED"}
    refs = {}
    features = {f["source_feature_id"]: f for f in batch["features"]}
    if len(features) != len(batch["features"]) or set(features) != {r["source_feature_id"] for r in rows}:
        raise ValueError("transport must preserve the exact A feature ID set")
    for row in rows:
        if row["batch_id"] != batch["batch_id"]:
            raise ValueError("row belongs to another batch")
        pair = row["source_id"], row["work_id"]
        if pair[0] not in sources or pair[1] not in works or pair not in links:
            raise ValueError(f"source/work reference not present in pinned registry: {pair}")
        if any(not isinstance(row[key], str) for key in COLUMNS):
            raise ValueError("transport rows must contain exact string columns")
        bytes.fromhex(row["wkb_hex"])
        for key in ("feature_json", "matches_json", "batch_provenance_json"):
            json.loads(row[key])
        if not re.fullmatch(r"[0-9a-f]{64}", row["row_sha256"]):
            raise ValueError("invalid transport row SHA")
        if digest({key: row[key] for key in COLUMNS[:-1]}) != row["row_sha256"]:
            raise ValueError("transport row SHA mismatch")
        if json.loads(row["feature_json"]) != features.get(row["source_feature_id"]):
            raise ValueError("transport feature differs from original A")
        feature = features[row["source_feature_id"]]
        if any(row[key] != feature[key] for key in ("source_id", "work_id")):
            raise ValueError("transport source/work differs from original A")
        expected_matches = [match for match in batch["project_matches"] if match["source_feature_id"] == row["source_feature_id"]]
        if json.loads(row["matches_json"]) != expected_matches:
            raise ValueError("transport differs from original B")
        refs[pair] = {"source_id": pair[0], "work_id": pair[1], "reference_json": canonical({
            "source": sources[pair[0]], "work": works[pair[1]], "link": links[pair],
            "source_snapshot_id": registry["source_snapshot_id"]})}
    payload = {"schema": "vkm.geometry-batch-projection-plan/1", "target_schema": SCHEMA,
               "batch_id": batch["batch_id"], "extraction_version": batch["extraction_version"],
               "batch_json": canonical(batch), "registry_sha256": digest(registry),
               "source_snapshot_id": registry["source_snapshot_id"],
               "source_refs": [refs[key] for key in sorted(refs)],
               "rows": sorted(rows, key=lambda row: row["source_feature_id"]),
               "scientific_admission": "NOT_GRANTED_BY_DB_LOAD"}
    payload["content_digest"] = digest(payload)
    return payload


def verify_plan(plan):
    if plan.get("target_schema") != SCHEMA or plan.get("schema") != "vkm.geometry-batch-projection-plan/1":
        raise ValueError("unsupported target/schema")
    if digest({key: value for key, value in plan.items() if key != "content_digest"}) != plan["content_digest"]:
        raise ValueError("plan content digest mismatch")


def _q(connection, pg, sql, params=()):
    return connection.execute(sql.replace("?", "%s") if pg else sql, params)


def _exists(connection, pg, table):
    return bool(_q(connection, pg, "SELECT count(*) FROM information_schema.tables WHERE table_schema=? AND table_name=?", (SCHEMA, table)).fetchone()[0])


def _guard(connection, pg):
    names = [row[0] for row in _q(connection, pg, "SELECT table_name FROM information_schema.tables WHERE table_schema=? ORDER BY table_name", (SCHEMA,)).fetchall() if row[0] not in TABLES]
    result = {}
    for name in names:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name):
            raise ValueError("unsupported baseline table identifier")
        rows = connection.execute(f'SELECT * FROM {SCHEMA}."{name}"').fetchall()
        # Comparison only; does not export scientific rows into receipts.
        serial = sorted(json.dumps(list(row), sort_keys=True, ensure_ascii=False, default=str) for row in rows)
        result[name] = digest(serial)
    return result


def _ddl(connection, pg):
    if not _q(connection, pg, "SELECT count(*) FROM information_schema.schemata WHERE schema_name=?", (SCHEMA,)).fetchone()[0]:
        raise ValueError("existing scientific schema must be prepared separately")
    connection.execute(f"CREATE TABLE IF NOT EXISTS {SCHEMA}.{TABLES[0]} (batch_id VARCHAR PRIMARY KEY, content_digest VARCHAR NOT NULL, extraction_version VARCHAR NOT NULL, registry_sha256 VARCHAR NOT NULL, batch_json VARCHAR NOT NULL)")
    batch_fk = f", FOREIGN KEY(batch_id) REFERENCES {SCHEMA}.{TABLES[0]}(batch_id)" if pg else ""
    connection.execute(f"CREATE TABLE IF NOT EXISTS {SCHEMA}.{TABLES[1]} (batch_id VARCHAR, source_id VARCHAR, work_id VARCHAR, reference_json VARCHAR NOT NULL, PRIMARY KEY(batch_id,source_id,work_id){batch_fk})")
    cols = ",".join(f"{key} VARCHAR NOT NULL" for key in COLUMNS)
    geometry = ",geom geometry NOT NULL CHECK(ST_SRID(geom)=0)" if pg else ""
    source_fk = f", FOREIGN KEY(batch_id,source_id,work_id) REFERENCES {SCHEMA}.{TABLES[1]}(batch_id,source_id,work_id)" if pg else ""
    # DuckDB 1.5 cannot delete a referencing child and parent in one transaction.
    # Keep atomic batch rollback there; enforce logical references at plan/apply/readback.
    connection.execute(f"CREATE TABLE IF NOT EXISTS {SCHEMA}.{TABLES[2]} ({cols}{geometry}, PRIMARY KEY(batch_id,source_feature_id){source_fk})")


def _current(connection, pg, plan):
    if not _exists(connection, pg, TABLES[0]):
        if any(_exists(connection, pg, table) for table in TABLES[1:]):
            raise Conflict("orphan projection tables")
        return False
    records = _q(connection, pg, f"SELECT content_digest FROM {SCHEMA}.{TABLES[0]} WHERE batch_id=?", (plan["batch_id"],)).fetchall()
    if not records:
        for table in TABLES[1:]:
            if _exists(connection, pg, table) and _q(connection, pg, f"SELECT count(*) FROM {SCHEMA}.{table} WHERE batch_id=?", (plan["batch_id"],)).fetchone()[0]:
                raise Conflict("orphan batch rows")
        return False
    if records != [(plan["content_digest"],)]:
        raise Conflict("batch_id already has another content digest")
    _assert_rows(connection, pg, plan)
    return True


def _assert_rows(connection, pg, plan):
    raw = _q(connection, pg, f"SELECT {','.join(COLUMNS)} FROM {SCHEMA}.{TABLES[2]} WHERE batch_id=? ORDER BY source_feature_id", (plan["batch_id"],)).fetchall()
    rows = [dict(zip(COLUMNS, row)) for row in raw]
    if rows != plan["rows"]:
        raise Conflict("stored feature rows differ from plan")
    refs = _q(connection, pg, f"SELECT source_id,work_id,reference_json FROM {SCHEMA}.{TABLES[1]} WHERE batch_id=? ORDER BY source_id,work_id", (plan["batch_id"],)).fetchall()
    if refs != [(r["source_id"], r["work_id"], r["reference_json"]) for r in plan["source_refs"]]:
        raise Conflict("stored source/work references differ from plan")
    if pg:
        checks = _q(connection, pg, f"SELECT bool_and(geom IS NOT NULL AND ST_SRID(geom)=0 AND encode(ST_AsBinary(geom,'NDR'),'hex')=wkb_hex) FROM {SCHEMA}.{TABLES[2]} WHERE batch_id=?", (plan["batch_id"],)).fetchone()[0]
    else:
        connection.execute("LOAD spatial")
        checks = _q(connection, pg, f"SELECT bool_and(lower(hex(ST_AsWKB(ST_GeomFromWKB(from_hex(wkb_hex)))))=lower(wkb_hex)) FROM {SCHEMA}.{TABLES[2]} WHERE batch_id=?", (plan["batch_id"],)).fetchone()[0]
    if checks is not True:
        raise Conflict("native geometry/WKB readback failed")
    return rows


def _scope(pg, scope):
    if scope != SCOPE or pg.info.host not in ("127.0.0.1", "localhost", "::1") or not pg.info.dbname.startswith("vkm_geo_"):
        raise ValueError("writes require explicit isolated local private test scope and vkm_geo_ database")
    if pg.info.transaction_status != 0:
        raise ValueError("PostgreSQL connection must be idle; refusing caller-owned transaction")


def apply_batch(plan, pg, dd, *, commit=False, scope=None):
    """Dry-run by default. Connections must be idle and owned by this operation."""
    verify_plan(plan)
    _scope(pg, scope)
    receipt = {"batch_id": plan["batch_id"], "content_digest": plan["content_digest"],
               "mode": "APPLY" if commit else "DRY_RUN", "state": {"postgres": "NOT_STARTED", "duckdb": "NOT_STARTED"}}
    try:
        pg.execute("BEGIN")
        pg.execute("SET LOCAL lock_timeout='5s'")
        dd.execute("BEGIN")
        before = {}
        for name, connection, is_pg in (("postgres", pg, True), ("duckdb", dd, False)):
            before[name] = _guard(connection, is_pg)
            exists = _current(connection, is_pg, plan)
            _ddl(connection, is_pg)
            if not exists:
                _q(connection, is_pg, f"INSERT INTO {SCHEMA}.{TABLES[0]} VALUES (?,?,?,?,?)", (plan["batch_id"], plan["content_digest"], plan["extraction_version"], plan["registry_sha256"], plan["batch_json"]))
                for ref in plan["source_refs"]:
                    _q(connection, is_pg, f"INSERT INTO {SCHEMA}.{TABLES[1]} VALUES (?,?,?,?)", (plan["batch_id"], ref["source_id"], ref["work_id"], ref["reference_json"]))
                for row in plan["rows"]:
                    extra_cols = ",geom" if is_pg else ""
                    extra_vals = ",ST_GeomFromWKB(decode(?,'hex'),0)" if is_pg else ""
                    values = [row[key] for key in COLUMNS] + ([row["wkb_hex"]] if is_pg else [])
                    _q(connection, is_pg, f"INSERT INTO {SCHEMA}.{TABLES[2]} ({','.join(COLUMNS)}{extra_cols}) VALUES ({','.join('?' for _ in COLUMNS)}{extra_vals})", values)
            _assert_rows(connection, is_pg, plan)
            if _guard(connection, is_pg) != before[name]:
                raise Conflict("unrelated scientific tables changed")
            receipt[name + "_inserted"] = 0 if exists else len(plan["rows"])
            receipt[name + "_baseline_digest"] = digest(before[name])
            receipt["state"][name] = "PREPARED"
        if commit:
            pg.commit()
            receipt["state"]["postgres"] = "COMMITTED"
            dd.execute("COMMIT")
            receipt["state"]["duckdb"] = "COMMITTED"
        else:
            pg.rollback()
            dd.execute("ROLLBACK")
            receipt["state"] = {"postgres": "ROLLED_BACK_DRY_RUN", "duckdb": "ROLLED_BACK_DRY_RUN"}
        return receipt
    except Exception as error:
        for name, connection in (("postgres", pg), ("duckdb", dd)):
            if receipt["state"][name] != "COMMITTED":
                try:
                    connection.rollback() if name == "postgres" else connection.execute("ROLLBACK")
                    receipt["state"][name] = "ROLLED_BACK"
                except Exception:
                    receipt["state"][name] = "ROLLBACK_UNKNOWN"
        if "COMMITTED" in receipt["state"].values():
            raise PartialApply(receipt, error) from error
        raise


def readback(plan, pg_factory, dd_factory):
    """Open independent fresh connections and read all rows, never LIMIT 500."""
    verify_plan(plan)
    pg = pg_factory()
    dd = dd_factory()
    try:
        pg.execute("BEGIN READ ONLY")
        rows_pg = _assert_rows(pg, True, plan)
        rows_dd = _assert_rows(dd, False, plan)
        if rows_pg != rows_dd:
            raise Conflict("two database readbacks differ")
        return {"status": "PASS", "batch_id": plan["batch_id"], "rows": len(rows_pg),
                "content_digest": plan["content_digest"], "fresh_connections": True,
                "rows_digest": digest(rows_pg), "scientific_admission": "NOT_GRANTED_BY_DB_LOAD"}
    finally:
        pg.close()
        dd.close()


def rollback_batch(plan, pg, dd, *, scope=None):
    """Delete only matching owned batch rows. Leave additive tables/schema intact."""
    verify_plan(plan)
    _scope(pg, scope)
    receipt = {"batch_id": plan["batch_id"], "mode": "ROLLBACK", "state": {"postgres": "NOT_STARTED", "duckdb": "NOT_STARTED"}}
    try:
        pg.execute("BEGIN")
        dd.execute("BEGIN")
        for name, connection, is_pg in (("postgres", pg, True), ("duckdb", dd, False)):
            before = _guard(connection, is_pg)
            exists = _current(connection, is_pg, plan)
            if exists:
                for table in reversed(TABLES):
                    _q(connection, is_pg, f"DELETE FROM {SCHEMA}.{table} WHERE batch_id=?", (plan["batch_id"],))
            if _guard(connection, is_pg) != before:
                raise Conflict("baseline changed during rollback")
            receipt[name + "_deleted"] = len(plan["rows"]) if exists else 0
            receipt["state"][name] = "PREPARED"
        pg.commit()
        receipt["state"]["postgres"] = "COMMITTED"
        dd.execute("COMMIT")
        receipt["state"]["duckdb"] = "COMMITTED"
        return receipt
    except Exception as error:
        for name, connection in (("postgres", pg), ("duckdb", dd)):
            if receipt["state"][name] != "COMMITTED":
                try:
                    connection.rollback() if name == "postgres" else connection.execute("ROLLBACK")
                    receipt["state"][name] = "ROLLED_BACK"
                except Exception:
                    receipt["state"][name] = "ROLLBACK_UNKNOWN"
        if "COMMITTED" in receipt["state"].values():
            raise PartialApply(receipt, error) from error
        raise


def build_query_snapshot(plan, output, *, allowed_token_labels, page_size=250, artifact_links=None):
    """Immutable local projection consumed by existing ScientificReader/API/MCP.

    Operator generates bounded named queries, including every page of each facet.
    No raw SQL or filter expressions are accepted from a client.
    """
    plans = plan if isinstance(plan, list) else [plan]
    if not plans:
        raise ValueError("at least one verified batch plan is required")
    for item in plans:
        verify_plan(item)
    snapshots = {item["source_snapshot_id"] for item in plans}
    if len(snapshots) != 1 or len({item["batch_id"] for item in plans}) != len(plans):
        raise ValueError("snapshot requires unique batch IDs and one document snapshot")
    batch_digests = {item["batch_id"]: item["content_digest"] for item in plans}
    snapshot_rows = sorted([row for item in plans for row in item["rows"]], key=lambda row: row["source_feature_id"])
    if len({row["source_feature_id"] for row in snapshot_rows}) != len(snapshot_rows):
        raise ValueError("ambiguous repeated feature ID across snapshot batches")
    plan = {"rows": snapshot_rows, "content_digest": digest(batch_digests),
            "source_snapshot_id": next(iter(snapshots))}
    if not 1 <= page_size <= 500 or not allowed_token_labels:
        raise ValueError("bounded page_size and explicit principals required")
    artifact_links = artifact_links or {}
    if set(artifact_links) - {row["source_feature_id"] for row in plan["rows"]}:
        raise ValueError("CAD artifact link references an unknown feature")
    for links in artifact_links.values():
        for link in links:
            if not link.get("uri") or not link.get("kind") or not re.fullmatch(r"[0-9a-f]{64}", link.get("sha256", "")):
                raise ValueError("external CAD links require URI, kind and pinned SHA-256")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    records = []
    for row in plan["rows"]:
        feature = json.loads(row["feature_json"])
        records.append({**row, "figure_id": feature.get("figure_id"), "panel_id": feature.get("panel_id"),
                        "pdf_page": feature.get("pdf_page"),
                        "horizon": feature.get("horizon") if isinstance(feature.get("horizon"), str) else None,
                        "horizon_json": canonical(feature.get("horizon")),
                        "extraction_status": feature.get("extraction_status"),
                        "review_status": feature.get("review_status"), "primitive_role": feature.get("primitive_role"),
                        "cad_links_json": canonical(feature.get("cad_links", []) + artifact_links.get(row["source_feature_id"], [])),
                        "source_image": feature.get("source_image"),
                        "source_image_sha256": feature.get("source_image_sha256"),
                        "source_artifact_json": canonical(feature.get("source_artifact")),
                        "epistemic_role": feature.get("epistemic_role"),
                        "feature_metadata_json": canonical({k: v for k, v in feature.items() if k != "geometry"}),
                        "scientific_admission": "NOT_GRANTED_BY_DB_LOAD"})
    import pyarrow as pa
    import pyarrow.parquet as pq
    path = output / "source_geometry_features.parquet"
    pq.write_table(pa.Table.from_pylist(records), path)
    queries, catalogue = {}, []
    facets = ("source_id", "figure_id", "horizon", "geometry_type", "frame_id", "extraction_status", "review_status", "cad_links_json")
    groups = [("all", None, None)]
    for field in facets:
        groups.extend((field, value, field) for value in sorted({record[field] for record in records}, key=canonical))
    for facet, value, field in groups:
        selected = records if field is None else [r for r in records if r[field] == value]
        group_id = "geometry_" + digest([facet, value])[:16]
        for index, offset in enumerate(range(0, len(selected), page_size)):
            query_id = f"{group_id}_p{index:04d}"
            where = "" if field is None else " WHERE " + field + (" IS NULL" if value is None else "='" + str(value).replace("'", "''") + "'")
            card_cols = "batch_id,source_feature_id,source_id,work_id,figure_id,panel_id,pdf_page,horizon,horizon_json,geometry_type,frame_id,coordinate_unit,extraction_status,review_status,primitive_role,epistemic_role,source_image,source_image_sha256,source_artifact_json,cad_links_json,scientific_admission"
            queries[query_id] = {"sql": f"SELECT {card_cols} FROM source_geometry_features{where} ORDER BY source_feature_id LIMIT {page_size} OFFSET {offset}"}
            catalogue.append({"query_id": query_id, "facet": facet, "value": value, "page": index,
                              "rows": min(page_size, len(selected) - offset), "required_limit": page_size,
                              "next_query_id": f"{group_id}_p{index + 1:04d}" if offset + page_size < len(selected) else None})
    for record in records:
        fid = record["source_feature_id"]
        key = digest(fid)[:16]
        where = " WHERE source_feature_id='" + fid.replace("'", "''") + "'"
        metadata_id = "geometry_metadata_" + key
        queries[metadata_id] = {"sql": "SELECT source_feature_id,feature_metadata_json,matches_json,batch_provenance_json FROM source_geometry_features" + where}
        catalogue.append({"query_id": metadata_id, "facet": "feature_metadata", "value": fid, "rows": 1, "required_limit": 1, "next_query_id": None})
        chunks = (len(record["wkb_hex"]) + 4095) // 4096
        for index in range(chunks):
            query_id = f"geometry_wkb_{key}_p{index:04d}"
            queries[query_id] = {"sql": f"SELECT source_feature_id,frame_id,coordinate_unit,geometry_type,{index} AS chunk_index,{chunks} AS chunk_count,substr(wkb_hex,{index * 4096 + 1},4096) AS wkb_hex_chunk FROM source_geometry_features{where}"}
            catalogue.append({"query_id": query_id, "facet": "feature_wkb", "value": fid, "page": index,
                              "rows": 1, "required_limit": 1,
                              "next_query_id": f"geometry_wkb_{key}_p{index + 1:04d}" if index + 1 < chunks else None})
    projection_digest = digest({"batch_content_digest": plan["content_digest"],
                                "table_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                "queries": queries, "principals": allowed_token_labels,
                                "artifact_links": artifact_links})
    manifest = {"schema": "vkm.scientific-query-snapshot/1", "snapshot_id": "geometry-" + projection_digest[:24],
                "source_snapshot_id": plan["source_snapshot_id"], "scope": "PRIVATE_SOURCE_GEOMETRY_LOCAL_TEST_PREPARED_NOT_ACTIVATED",
                "allowed_token_labels": allowed_token_labels,
                "tables": {"source_geometry_features": {"path": path.name, "rows": len(records),
                           "data_role": "SOURCE_GEOMETRY_DIGITIZATION", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}},
                "queries": queries, "query_catalogue": catalogue, "batch_content_digest": plan["content_digest"],
                "batch_content_digests": batch_digests,
                "artifact_links_sha256": digest(artifact_links)}
    mp = output / "snapshot.json"
    mp.write_text(canonical(manifest), encoding="utf-8")
    return {"manifest_path": str(mp), "manifest_sha256": hashlib.sha256(mp.read_bytes()).hexdigest(),
            "snapshot_id": manifest["snapshot_id"], "queries": catalogue,
            "status": "PREPARED_NOT_ACTIVATED"}

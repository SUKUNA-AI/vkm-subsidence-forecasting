"""Read-only, snapshot-bound scientific provenance queries beside the corpus API.

This projection stores no canonical measurements. Configured operator queries
read immutable Parquet files with explicit model/source/service roles. Clients
select a named query and an exact snapshot, and never submit SQL or role labels.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Annotated, Callable

from fastapi import Depends, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from vkm_corpus.api.errors import ApiFailure


TABLE_ROLES = {
    "stage_arrays": "MODEL_STAGE_ARRAY_CATALOG",
    "stage_values": "MODEL_STAGE_NUMERIC_VALUES",
    "storage": "STORAGE_AVAILABILITY_METADATA",
    "current_source_joins": "SOURCE_TO_CONDITIONAL_OPERATOR_RUN_FIELD_REFERENCE",
    "current_depth_intervals": "MODEL_CURRENT_DEPTH_CONDITIONAL_GEOLOGY",
    "current_depth_materials": "MODEL_CURRENT_DEPTH_CONDITIONAL_C2",
    "current_depth_patches": "MODEL_CURRENT_DEPTH_PATCH_COVERAGE",
    "current_depth_events": "MODEL_SOURCE_DRIVEN_C4",
    "final_producers": "MODEL_AND_NUMERICAL_PRODUCER_METADATA",
    "intervals": "MODEL_GEOLOGY",
    "constraints": "SOURCE_EVIDENCE_REFERENCE",
    "patches": "MODEL_GEOMETRY",
    "materials": "MODEL_MATERIAL",
    "law_coverage": "MODEL_LAW_COVERAGE",
    "observations": "OBSERVATION_INDEX",
    "families": "MODEL_OBSERVATION_COMPARISON",
    "transfers": "CONDITIONAL_PARAMETER_TRANSFER",
    "services": "SERVICE_METADATA",
    "runs": "MODEL_RUN_METADATA",
    "source_page": "DOCUMENT_READBACK",
    "ports": "MODEL_TOPOLOGY",
    "native_artifacts": "MODEL_ARTIFACT_REFERENCE",
    "identities": "SERVICE_IDENTITY",
    "figures": "THESIS_GRAPHIC_METADATA",
    # Values of figure series digitized from publications: DERIVATION, never a project measurement.
    "source_series_points": "SOURCE_FIGURE_SERIES_DIGITIZATION",
    "source_digitization_batches": "SOURCE_DIGITIZATION_BATCH_METADATA",
    "source_series_nav_links": "SOURCE_DIGITIZATION_CROSS_LINK",
}
IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class ScientificReader:
    """A native DuckDB reader over one immutable, operator-bound file snapshot."""

    def __init__(self, manifest_path: Path, manifest_sha256: str,
                 source_snapshot: Callable[[], str | None]):
        self.path = Path(manifest_path).resolve()
        self.digest = manifest_sha256
        self.source_snapshot = source_snapshot
        if not re.fullmatch(r"[0-9a-f]{64}", manifest_sha256):
            raise ValueError("scientific snapshot requires an exact SHA-256")
        if _sha256(self.path) != self.digest:
            raise ValueError("scientific manifest SHA-256 mismatch")
        manifest = json.loads(self.path.read_bytes())
        if manifest.get("schema") != "vkm.scientific-query-snapshot/1":
            raise ValueError("unsupported scientific query snapshot schema")
        if not manifest.get("snapshot_id") or not manifest.get("source_snapshot_id") or not manifest.get("scope"):
            raise ValueError("scientific snapshot identities are missing")
        self.allowed_labels = manifest.get("allowed_token_labels")
        if (not isinstance(self.allowed_labels, list) or not self.allowed_labels
                or any(not isinstance(label, str) or not label for label in self.allowed_labels)):
            raise ValueError("scientific snapshot requires explicit read principals")
        self.manifest = manifest
        self.tables = {}
        for name, table in manifest.get("tables", {}).items():
            if name not in TABLE_ROLES or not IDENTIFIER.fullmatch(name) or table.get("data_role") != TABLE_ROLES[name]:
                raise ValueError("scientific table role mismatch: model data cannot become REAL measurements")
            relative = Path(table["path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("scientific table must be contained in its snapshot")
            path = (self.path.parent / relative).resolve(strict=True)
            if not path.is_relative_to(self.path.parent):
                raise ValueError("scientific table escapes snapshot")
            self.tables[name] = path
        if not self.tables:
            raise ValueError("scientific snapshot has no data tables")
        import duckdb

        for query in manifest.get("queries", {}).values():
            statements = duckdb.extract_statements(query["sql"])
            if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
                raise ValueError("scientific reader permits one operator-owned SELECT per named query")
        if not manifest.get("queries"):
            raise ValueError("scientific snapshot has no named queries")
        self._verify()
        with duckdb.connect(":memory:") as connection:
            for name, path in self.tables.items():
                table = connection.read_parquet(str(path))
                actual = table.count("*").fetchone()[0]
                if actual != self.manifest["tables"][name]["rows"]:
                    raise ValueError("scientific table row count mismatch")
                table.create(name)
            connection.execute("SET enable_external_access = false")
            for query in manifest["queries"].values():
                try:
                    connection.execute("EXPLAIN " + query["sql"])
                except duckdb.Error as error:
                    raise ValueError("scientific queries must use only bound snapshot tables") from error

    def _verify(self) -> None:
        if _sha256(self.path) != self.digest:
            raise ApiFailure("SNAPSHOT_UNAVAILABLE", "scientific manifest changed",
                             details={"reason": "MANIFEST_HASH_MISMATCH"})
        if self.source_snapshot() != self.manifest["source_snapshot_id"]:
            raise ApiFailure("SNAPSHOT_UNAVAILABLE", "scientific projection source snapshot is stale",
                             details={"reason": "STALE_SOURCE_SNAPSHOT"})
        for name, path in self.tables.items():
            if _sha256(path) != self.manifest["tables"][name]["sha256"]:
                raise ApiFailure("SNAPSHOT_UNAVAILABLE", "scientific table changed",
                                 details={"reason": "TABLE_HASH_MISMATCH", "table": name})

    def status(self) -> dict:
        self._verify()
        return {
            "schema": "vkm.scientific-reader-status/1",
            "snapshot_id": self.manifest["snapshot_id"],
            "source_snapshot_id": self.manifest["source_snapshot_id"],
            "scope": self.manifest["scope"],
            "tables": {name: {"rows": table["rows"], "data_role": table["data_role"]}
                       for name, table in self.manifest["tables"].items()},
            "query_ids": sorted(self.manifest["queries"]),
            "native_reader": "duckdb",
            "scientific_admission": "NOT_GRANTED_BY_READER",
        }

    def query(self, query_id: str, snapshot_id: str, limit: int = 100) -> dict:
        if snapshot_id != self.manifest["snapshot_id"]:
            raise ApiFailure("INVALID_ARGUMENT", "client scientific snapshot is stale",
                             details={"reason": "STALE_CLIENT_SNAPSHOT"})
        if query_id not in self.manifest["queries"]:
            raise ApiFailure("NOT_FOUND", "no such named scientific query")
        if not 1 <= limit <= 500:
            raise ApiFailure("INVALID_ARGUMENT", "scientific query limit is outside 1..500")
        self._verify()
        import duckdb

        query = self.manifest["queries"][query_id]
        with duckdb.connect(":memory:") as connection:
            for name, path in self.tables.items():
                connection.read_parquet(str(path)).create(name)
            connection.execute("SET enable_external_access = false")
            result = connection.execute("SELECT * FROM (" + query["sql"].rstrip().rstrip(";")
                                        + ") AS scientific_result LIMIT ?", [limit + 1])
            columns = [column[0] for column in result.description]
            rows = [dict(zip(columns, row)) for row in result.fetchall()]
        self._verify()
        return {
            "schema": "vkm.scientific-query-result/1",
            "query_id": query_id,
            "snapshot_id": self.manifest["snapshot_id"],
            "source_snapshot_id": self.manifest["source_snapshot_id"],
            "scope": self.manifest["scope"],
            "result_role": "DERIVED_PROVENANCE_QUERY",
            "scientific_admission": "NOT_GRANTED_BY_READER",
            "rows": rows[:limit],
            "row_count": min(len(rows), limit),
            "truncated": len(rows) > limit,
            "input_table_sha256": {name: table["sha256"]
                                   for name, table in self.manifest["tables"].items()},
        }


def mount_scientific_routes(app, reader: ScientificReader, read_access) -> None:
    """Use the existing API authentication and exception policy for this optional reader."""
    def authenticated(request: Request):
        label = read_access(request)
        if label not in reader.allowed_labels:
            raise ApiFailure("FORBIDDEN", "principal is not admitted to this scientific snapshot")
        return label

    @app.get("/v1/science/status", tags=["scientific provenance"])
    def scientific_status(request: Request, _auth: str = Depends(authenticated)):
        return JSONResponse({"ok": True, "meta": {"request_id": request.state.request_id},
                             "scientific": reader.status()})

    @app.get("/v1/science/query/{query_id}", tags=["scientific provenance"])
    def scientific_query(
        request: Request,
        query_id: str,
        snapshot_id: str,
        _auth: str = Depends(authenticated),
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ):
        if set(request.query_params) - {"snapshot_id", "limit"}:
            raise ApiFailure("INVALID_ARGUMENT", "scientific query accepts snapshot_id and limit only")
        data = reader.query(query_id, snapshot_id, limit)
        return JSONResponse(jsonable_encoder({"ok": True,
            "meta": {"request_id": request.state.request_id}, "scientific": data}))

"""Complete NDJSON exports of explicitly qualified scientific named queries."""

from __future__ import annotations

import hashlib
import re
from typing import Annotated

from fastapi import Depends, Query, Request
from fastapi.responses import StreamingResponse

from vkm_corpus.api.errors import ApiFailure
from vkm_corpus.api.scientific_snapshot_verification import encoded


BATCH_ROWS = 128
MAX_ROW_BYTES = 1_048_576
SHA256_PATTERN = r"^[0-9a-f]{64}$"


def _contract(query):
    contract = query.get("export")
    if contract is None:
        raise ApiFailure("NOT_FOUND", "named query has no qualified full export")
    if not isinstance(contract, dict) or contract.keys() != {"order_by", "row_count", "ordered_sha256"}:
        raise ApiFailure("SNAPSHOT_UNAVAILABLE", "invalid scientific export contract")
    order, count, digest = (contract[key] for key in ("order_by", "row_count", "ordered_sha256"))
    if (not isinstance(order, list) or not order
            or any(not isinstance(name, str) or not name for name in order)
            or len(set(order)) != len(order)
            or type(count) is not int or count < 0
            or not isinstance(digest, str) or not re.fullmatch(SHA256_PATTERN, digest)):
        raise ApiFailure("SNAPSHOT_UNAVAILABLE", "invalid scientific export contract")
    return order, count, digest


def _line(record):
    return encoded(record) + b"\n"


def _incomplete(reason):
    return _line({"type": "incomplete", "reason": reason})


def _rows(reader, query_id, sql, order, expected_count, expected_sha, authorize):
    import duckdb

    try:
        authorize()
        with duckdb.connect(":memory:", config={"memory_limit": "256MB",
                                                "max_temp_directory_size": "0B", "threads": "2"}) as connection:
            for name, path in reader.tables.items():
                connection.read_parquet(str(path)).create(name)
            connection.execute("SET enable_external_access = false")
            reader._verify()
            # DESCRIBE keeps the original result names. Subquery binding can
            # silently disambiguate them, which would change the typed result.
            columns = [(name, kind) for name, kind, *_ in connection.execute("DESCRIBE " + sql).fetchall()]
            names = [name for name, _ in columns]
            if len({name.casefold() for name in names}) != len(names):
                yield _incomplete("AMBIGUOUS_RESULT_COLUMNS")
                return
            if any(name not in names for name in order):
                yield _incomplete("INVALID_RESULT_IDENTITY")
                return
            positions = [names.index(name) for name in order]
            keys = ", ".join('"' + name.replace('"', '""') + '"' for name in order)
            result = connection.execute(
                f"SELECT *, count(*) OVER (PARTITION BY {keys}) "
                f"FROM ({sql}\n) AS scientific_export_result ORDER BY {keys}"
            )
            authorize()
            yield _line({
                "type": "metadata", "schema": "vkm.scientific-query-export/1", "query_id": query_id,
                "snapshot_id": reader.manifest["snapshot_id"], "manifest_sha256": reader.digest,
                "source_snapshot_id": reader.manifest["source_snapshot_id"], "scope": reader.manifest["scope"],
                "result_role": "DERIVED_PROVENANCE_QUERY", "scientific_admission": "NOT_GRANTED_BY_READER",
                "columns": columns, "order_by": order, "expected_row_count": expected_count,
                "expected_ordered_sha256": expected_sha,
            })
            count = 0
            digest = hashlib.sha256(encoded(columns) + b"\n")
            while batch := result.fetchmany(BATCH_ROWS):
                authorize()
                for row in batch:
                    if row[-1] != 1 or any(row[index] is None for index in positions):
                        yield _incomplete("INVALID_RESULT_IDENTITY")
                        return
                    count += 1
                    if count > expected_count:
                        yield _incomplete("RESULT_PROOF_MISMATCH")
                        return
                    data = encoded(row[:-1])
                    if len(data) > MAX_ROW_BYTES:
                        yield _incomplete("RESULT_ROW_LIMIT")
                        return
                    digest.update(data + b"\n")
                    yield b'{"type":"row","values":' + data + b'}\n'
        authorize()
        reader._verify()
        actual_sha = digest.hexdigest()
        if count != expected_count or actual_sha != expected_sha:
            yield _incomplete("RESULT_PROOF_MISMATCH")
        else:
            yield _line({"type": "complete", "row_count": count, "ordered_sha256": actual_sha})
    except ApiFailure as error:
        yield _incomplete(error.code)
    except duckdb.OutOfMemoryException:
        yield _incomplete("RESULT_MEMORY_LIMIT")
    except (duckdb.Error, OSError, TypeError, ValueError):
        yield _incomplete("RESULT_UNAVAILABLE")


def mount_scientific_export_route(app, reader, read_access) -> None:
    """Use the same read principal and immutable snapshot as the preview API."""
    def authenticated(request: Request):
        label = read_access(request)
        if label not in reader.allowed_labels:
            raise ApiFailure("FORBIDDEN", "principal is not admitted to this scientific snapshot")
        return label

    @app.get("/v1/science/export/{query_id}", tags=["scientific provenance"], response_class=StreamingResponse,
             description="NDJSON full export. A result is complete only with a validated final complete record.",
             responses={200: {"content": {"application/x-ndjson": {}}}})
    def scientific_export(
        request: Request, query_id: str, snapshot_id: str,
        manifest_sha256: Annotated[str, Query(pattern=SHA256_PATTERN)],
        label: str = Depends(authenticated),
    ):
        if (set(request.query_params) != {"snapshot_id", "manifest_sha256"}
                or len(request.query_params.multi_items()) != 2):
            raise ApiFailure("INVALID_ARGUMENT", "scientific export requires a snapshot and exact manifest digest")
        if snapshot_id != reader.manifest["snapshot_id"] or manifest_sha256 != reader.digest:
            raise ApiFailure("INVALID_ARGUMENT", "client scientific export identity is stale")
        query = reader.manifest["queries"].get(query_id)
        if query is None:
            raise ApiFailure("NOT_FOUND", "no such named scientific query")
        order, expected_count, expected_sha = _contract(query)
        try:
            reader._verify()
        except OSError as error:
            raise ApiFailure("SNAPSHOT_UNAVAILABLE", "scientific export input is unavailable") from error

        def authorize():
            if authenticated(request) != label:
                raise ApiFailure("FORBIDDEN", "scientific export principal changed")

        return StreamingResponse(_rows(reader, query_id, query["sql"].rstrip().rstrip(";"),
                                      order, expected_count, expected_sha, authorize),
                                 media_type="application/x-ndjson", headers={"Cache-Control": "no-store"})

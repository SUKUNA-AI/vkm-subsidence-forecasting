"""Complete NDJSON exports of explicitly qualified scientific named queries."""

from __future__ import annotations

from collections import Counter
import hashlib
from pathlib import Path
import re
from shutil import copyfile
from tempfile import TemporaryDirectory
from typing import Annotated

from fastapi import Depends, Query, Request
from fastapi.responses import StreamingResponse

from vkm_corpus.api.errors import ApiFailure
from vkm_corpus.api.scientific import _sha256
from vkm_corpus.api.scientific_snapshot_verification import encoded


BATCH_ROWS = 128
MAX_ROW_BYTES = 1_048_576
MAX_MULTISET_ROWS = 300_000
SHA256_PATTERN = r"^[0-9a-f]{64}$"
_R3_RANGE_ROWS = 2048
# Only the verified R3_ports definition admits this range lowering. Changing
# its filter, projection or ordering requires a fresh qualification.
_R3_PORTS_SQL_SHA256 = "648502ad80e904641b8cbb029e6b89aaac901eed1ff192da2f176071eae26773"


def _contract(query):
    contract = query.get("export")
    if contract is None:
        raise ApiFailure("NOT_FOUND", "named query has no qualified full export")
    if not isinstance(contract, dict):
        raise ApiFailure("SNAPSHOT_UNAVAILABLE", "invalid scientific export contract")
    proof_fields = contract.keys() - {"order_by", "row_count"}
    if (not {"order_by", "row_count"} <= contract.keys()
            or proof_fields not in ({"ordered_sha256"}, {"multiset_sha256"})):
        raise ApiFailure("SNAPSHOT_UNAVAILABLE", "invalid scientific export contract")
    proof, = proof_fields
    order, count, digest = contract["order_by"], contract["row_count"], contract[proof]
    multiset = proof == "multiset_sha256"
    if (not isinstance(order, list)
            or any(not isinstance(name, str) or not name for name in order)
            or len(set(order)) != len(order)
            or type(count) is not int or count < 0
            or not order and multiset and count > 1
            or not isinstance(digest, str) or not re.fullmatch(SHA256_PATTERN, digest)):
        raise ApiFailure("SNAPSHOT_UNAVAILABLE", "invalid scientific export contract")
    if multiset and count > MAX_MULTISET_ROWS:
        raise ApiFailure("PAYLOAD_TOO_LARGE", "scientific multiset proof exceeds the row budget")
    return order, count, digest, multiset


def _line(record):
    return encoded(record) + b"\n"


def _incomplete(reason):
    return _line({"type": "incomplete", "reason": reason})


def _multiset_sha256(schema, counts):
    # Same byte format as full_checksum; the retained hash keys use bytes to
    # bound the extra memory of proving a large streamed result.
    digest = hashlib.sha256(schema)
    for row_hash in sorted(counts):
        digest.update(f"{row_hash.hex()}:{counts[row_hash]}\n".encode("ascii"))
    return digest.hexdigest()


def _declared_order_start(sql, order, duckdb):
    """Locate an exact terminal ORDER BY of simple identifiers, with no limit."""
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in order):
        return None
    exact = re.compile(r"ORDER\s+BY\s+" + r"\s*,\s*".join(map(re.escape, order)) + r"\s*", re.IGNORECASE)
    # A suffix inside a quoted literal or a SQL comment is not an ORDER BY.
    for offset, token in reversed(duckdb.tokenize(sql)):
        if token == duckdb.token_type.keyword and exact.fullmatch(sql[offset:]):
            return offset
    return None


def _uses_r3_ranges(query_id, sql, order, multiset):
    return (query_id == "R3_ports" and order == ["r3_entity_id"] and multiset
            and hashlib.sha256(sql.encode()).hexdigest() == _R3_PORTS_SQL_SHA256)


def _r3_boundaries(connection, unordered_sql):
    return connection.execute(
        "SELECT ordinal, r3_entity_id FROM ("
        "SELECT row_number() OVER (ORDER BY r3_entity_id) - 1 AS ordinal, r3_entity_id "
        f"FROM ({unordered_sql}\n) AS qualified_ports"
        ") AS numbered_keys WHERE ordinal % ? = 0 ORDER BY ordinal",
        [_R3_RANGE_ROWS],
    ).fetchall()


def _r3_results(connection, unordered_sql, boundaries, expected_count, authorize):
    for index, (ordinal, lower) in enumerate(boundaries):
        authorize()
        sql = unordered_sql + " AND r3_entity_id >= ?"
        params = [lower]
        end = expected_count
        if index + 1 < len(boundaries):
            end, upper = boundaries[index + 1]
            sql += " AND r3_entity_id < ?"
            params.append(upper)
        result = connection.execute(sql + " ORDER BY r3_entity_id", params)
        yield result, end - ordinal


def _rows(reader, query_id, sql, order, expected_count, expected_sha, multiset, authorize):
    import duckdb

    try:
        authorize()
        # Keep an immutable byte copy for the life of this stream. Scanning
        # views over those copies avoids materializing all Parquet tables in
        # DuckDB before a query has selected its inputs.
        with (
            TemporaryDirectory(prefix="vkm-science-export-") as directory,
            duckdb.connect(":memory:", config={
                "memory_limit": "256MB", "threads": "2",
                "temp_directory": str(Path(directory) / "spill"), "max_temp_directory_size": "512MB",
            }) as connection,
        ):
            allowed_paths = []
            for name, path in reader.tables.items():
                copy = Path(directory) / f"{name}.parquet"
                copyfile(path, copy)
                if _sha256(copy) != reader.manifest["tables"][name]["sha256"]:
                    yield _incomplete("SNAPSHOT_UNAVAILABLE")
                    return
                allowed_paths.append(copy.as_posix())
                connection.read_parquet(copy.as_posix()).create_view(name)
            connection.execute("SET allowed_paths = ?", [allowed_paths])
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
            ordered_sql = sql
            use_ranges = False
            if positions:
                keys = ['"' + name.replace('"', '""') + '"' for name in order]
                key_tuple = ", ".join(keys)
                nonnull = " AND ".join(f"{key} IS NOT NULL" for key in keys)
                order_start = _declared_order_start(sql, order, duckdb)
                census_sql = sql[:order_start].rstrip() if order_start is not None else sql
                # Keep row payloads out of the key census. A matching terminal
                # order is already enforced by DuckDB for the subsequent read.
                actual, non_null, unique = connection.execute(
                    f"SELECT count(*), count(*) FILTER (WHERE {nonnull}), count(DISTINCT ({key_tuple})) "
                    f"FROM ({census_sql}\n) AS scientific_export_keys"
                ).fetchone()
                if actual != expected_count:
                    yield _incomplete("RESULT_PROOF_MISMATCH")
                    return
                if non_null != actual or unique != actual:
                    yield _incomplete("INVALID_RESULT_IDENTITY")
                    return
                if order_start is None:
                    ordered_sql = f"SELECT * FROM ({sql}\n) AS scientific_export_result ORDER BY {key_tuple}"
                elif _uses_r3_ranges(query_id, sql, order, multiset):
                    boundaries = _r3_boundaries(connection, census_sql)
                    if [item[0] for item in boundaries] != list(range(0, expected_count, _R3_RANGE_ROWS)):
                        yield _incomplete("RESULT_PROOF_MISMATCH")
                        return
                    use_ranges = True
            if use_ranges:
                results = _r3_results(connection, census_sql, boundaries, expected_count, authorize)
            else:
                results = ((connection.execute(ordered_sql), expected_count),)
            proof_key = "multiset_sha256" if multiset else "ordered_sha256"
            authorize()
            yield _line({
                "type": "metadata", "schema": "vkm.scientific-query-export/1", "query_id": query_id,
                "snapshot_id": reader.manifest["snapshot_id"], "manifest_sha256": reader.digest,
                "source_snapshot_id": reader.manifest["source_snapshot_id"], "scope": reader.manifest["scope"],
                "result_role": "DERIVED_PROVENANCE_QUERY", "scientific_admission": "NOT_GRANTED_BY_READER",
                "columns": columns, "order_by": order, "expected_row_count": expected_count,
                f"expected_{proof_key}": expected_sha,
            })
            count = 0
            schema = encoded(columns)
            digest = hashlib.sha256(schema + b"\n")
            counts = Counter() if multiset else None
            previous_key = previous_key_encoded = None
            for result, expected_range_rows in results:
                range_start = count
                while batch := result.fetchmany(BATCH_ROWS):
                    authorize()
                    frame = bytearray() if use_ranges else None
                    for row in batch:
                        if positions:
                            row_key = tuple(row[index] for index in positions)
                            key_encoded = encoded(row_key)
                            if None in row_key or row_key == previous_key or key_encoded == previous_key_encoded:
                                yield _incomplete("INVALID_RESULT_IDENTITY")
                                return
                            previous_key, previous_key_encoded = row_key, key_encoded
                        count += 1
                        if count - range_start > expected_range_rows:
                            yield _incomplete("RESULT_PROOF_MISMATCH")
                            return
                        data = encoded(row)
                        if len(data) > MAX_ROW_BYTES:
                            yield _incomplete("RESULT_ROW_LIMIT")
                            return
                        digest.update(data + b"\n")
                        if counts is not None:
                            counts[hashlib.sha256(data).digest()] += 1
                        line = b'{"type":"row","values":' + data + b'}\n'
                        if frame is None:
                            yield line
                            continue
                        if frame and len(frame) + len(line) > MAX_ROW_BYTES:
                            yield bytes(frame)
                            frame.clear()
                        frame.extend(line)
                    if frame:
                        yield bytes(frame)
                if count - range_start != expected_range_rows:
                    yield _incomplete("RESULT_PROOF_MISMATCH")
                    return
        authorize()
        reader._verify()
        seal = {"type": "complete", "row_count": count, "ordered_sha256": digest.hexdigest()}
        if counts is not None:
            seal["multiset_sha256"] = _multiset_sha256(schema, counts)
        if count != expected_count or seal[proof_key] != expected_sha:
            yield _incomplete("RESULT_PROOF_MISMATCH")
        else:
            yield _line(seal)
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
        order, expected_count, expected_sha, multiset = _contract(query)
        try:
            reader._verify()
        except OSError as error:
            raise ApiFailure("SNAPSHOT_UNAVAILABLE", "scientific export input is unavailable") from error

        def authorize():
            if authenticated(request) != label:
                raise ApiFailure("FORBIDDEN", "scientific export principal changed")

        return StreamingResponse(_rows(reader, query_id, query["sql"].rstrip().rstrip(";"),
                                      order, expected_count, expected_sha, multiset, authorize),
                                 media_type="application/x-ndjson", headers={"Cache-Control": "no-store"})

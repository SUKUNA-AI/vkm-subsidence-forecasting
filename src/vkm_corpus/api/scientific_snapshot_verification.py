"""Full-result and historical-proof guards; no reader activation or production writes."""
from collections import Counter
import datetime as dt
from decimal import Decimal
import hashlib
import json
import math


def normalized(value):
    if isinstance(value, (dt.date, dt.datetime)):
        return {"$date": value.isoformat()}
    if isinstance(value, bytes):
        return {"$bytes": value.hex()}
    if isinstance(value, Decimal):
        # Decimal text retains every digit and the stored scale; float would
        # round DECIMAL(38,...) cells and could hide a changed full-row result.
        return {"$decimal": str(value)}
    if isinstance(value, float) and not math.isfinite(value):
        return {"$float": repr(value)}
    if isinstance(value, dict):
        return {k: normalized(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalized(v) for v in value]
    return value


def encoded(value):
    return json.dumps(normalized(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def full_checksum(connection, sql, *, capture=False):
    cursor = connection.execute(sql)
    columns = [(c[0], str(c[1])) for c in cursor.description]
    names = [c[0] for c in columns]
    counts, rows, total = Counter(), [], 0
    while True:
        chunk = cursor.fetchmany(4096)
        if not chunk:
            break
        total += len(chunk)
        for row in chunk:
            counts[hashlib.sha256(encoded(row)).hexdigest()] += 1
            if capture:
                rows.append(dict(zip(names, row)))
    h = hashlib.sha256(encoded(columns))
    for row_hash, count in sorted(counts.items()):
        h.update(row_hash.encode())
        h.update(b":")
        h.update(str(count).encode())
        h.update(b"\n")
    result = {"rows": total, "columns": columns, "multiset_sha256": h.hexdigest(),
              "preview_500_would_truncate": total > 500}
    if capture:
        result["captured_rows"] = rows
    return result


def historical_receipt_audit(receipt):
    """Require explicit complete counts; never rewrite a historical verdict.

    The two full-read aliases are integer counts, not truthy flags. Preview IDs
    may coexist with a full read; an explicit truncated-read count may not.
    """
    issues = []
    if type(receipt.get("differing")) is not list or receipt["differing"]:
        issues.append("PARENT_QUERIES_DIFFER")
    for field in ("partition_content_digest_matches_batch", "links_content_digest_matches_batch"):
        if receipt.get(field) is not True:
            issues.append(field.upper())

    def count(field):
        value = receipt.get(field)
        if type(value) is not int or value < 0:
            issues.append("INVALID_" + field.upper())
            return None
        return value

    queries, executed = count("parent_queries"), count("parent_queries_executed")
    if queries is None or queries == 0 or executed is None or executed != queries:
        issues.append("INCOMPLETE_PARENT_QUERY_COVERAGE")
    rows, unique = count("partition_rows"), count("partition_unique_points")
    if rows is None or unique is None or rows != unique:
        issues.append("PARTITION_DUPLICATES")

    full_fields = [field for field in ("parent_query_full_read_count", "parent_queries_full_read") if field in receipt]
    if not full_fields:
        issues.append("HISTORICAL_PARENT_QUERY_TRUNCATION_NOT_RECORDED")
    for field in full_fields:
        full = count(field)
        if queries is None or full is None or full != queries:
            issues.append("INCOMPLETE_PARENT_FULL_READ_COVERAGE")
    if "parent_query_truncated_count" in receipt:
        truncated = count("parent_query_truncated_count")
        if truncated is None or truncated != 0:
            issues.append("HISTORICAL_PARENT_QUERY_TRUNCATED")
    for field in ("links_truncated", "series_summary_truncated"):
        if field in receipt and receipt[field] is not False:
            issues.append("HISTORICAL_NEW_QUERY_TRUNCATED")
    # A missing truncation flag is not evidence of a full links read. Require
    # the positive field used by the saved strict receipt, without inventing
    # a links_truncated field for that historical record.
    if receipt.get("links_full_read") is not True:
        issues.append("INCOMPLETE_LINKS_FULL_READ")
    return {"historical_receipt_unchanged": True, "current_full_proof": not issues,
            "issues": issues, "exit_code": 0 if not issues else 1}

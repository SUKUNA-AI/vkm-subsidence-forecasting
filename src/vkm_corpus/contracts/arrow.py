"""Arrow schemas derived deterministically from the pydantic row models, plus canonical fingerprints.

* ``field_specs(model)`` — pure-Python description ``[(name, arrow type string, nullable)]``; the type strings are
  exactly what pyarrow prints (checked by a test), so fingerprints do not need pyarrow;
* ``schema_fingerprint(dataset)`` — sha256 of that description (stored in Parquet key-value metadata);
* ``arrow_schema(dataset)`` / ``rows_to_table(dataset, rows)`` — pyarrow objects (imported lazily);
* ``table_fingerprint`` / ``content_fingerprint`` — order-independent multiset digests of canonical JSON rows
  (``RowDigest``: count + sum of row hashes mod 2**256, additive over files); the same function is applied to
  Parquet files, commit markers, snapshot manifests and the materialised DuckDB tables (H-48).

Nullable in Arrow ⇔ the annotation admits ``None``. A bare ``int`` is refused (widths are explicit).
"""
from __future__ import annotations

import hashlib
import json
import math
import types
import typing
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from typing import Annotated, Any, Iterable, Sequence, Union, get_args, get_origin

from pydantic import BaseModel

from vkm_corpus.contracts.datasets import DATASETS, HISTORICAL_LOCATOR_SCHEMAS, VOLATILE_COLUMNS, DatasetSpec, dataset
from vkm_corpus.contracts.fieldtypes import ArrowType

_ARROW_NAMES = {
    "int16": "int16", "int32": "int32", "int64": "int64", "float64": "double",
    "timestamp[us, tz=UTC]": "timestamp[us, tz=UTC]", "date32": "date32[day]",
}


class ContractTypeError(TypeError):
    """An annotation has no Arrow mapping (e.g. a bare int)."""


@dataclass(frozen=True)
class TypeSpec:
    """Arrow type tree: ``kind`` is a primitive name, ``list`` or ``struct``."""

    kind: str
    child: "TypeSpec | None" = None
    fields: tuple["FieldSpec", ...] = ()

    def __str__(self) -> str:
        if self.kind == "list":
            return f"list<item: {self.child}>"
        if self.kind == "struct":
            return "struct<" + ", ".join(f"{f.name}: {f.type}{'' if f.nullable else ' not null'}"
                                         for f in self.fields) + ">"
        return self.kind


@dataclass(frozen=True)
class FieldSpec:
    name: str
    type: TypeSpec
    nullable: bool
    description: str | None = None

    def describe(self) -> list:
        return [self.name, str(self.type), self.nullable]


def _unwrap_optional(ann: Any) -> tuple[Any, bool]:
    origin = get_origin(ann)
    if origin in (Union, types.UnionType):
        args = [a for a in get_args(ann) if a is not type(None)]
        if len(args) != len(get_args(ann)):
            if len(args) != 1:
                raise ContractTypeError(f"only Optional[X] unions are supported: {ann!r}")
            return args[0], True
        raise ContractTypeError(f"unions are not supported: {ann!r}")
    return ann, False


def _type_of(ann: Any, meta: tuple = ()) -> TypeSpec:
    for item in meta:
        if isinstance(item, ArrowType):
            return TypeSpec(_ARROW_NAMES[item.name])
    origin = get_origin(ann)
    if origin is Annotated:
        base, *extra = get_args(ann)
        return _type_of(base, tuple(extra) + tuple(meta))
    if origin in (list, typing.List):
        (item,) = get_args(ann)
        inner, _ = _unwrap_optional(item)
        return TypeSpec("list", child=_type_of(inner))
    if isinstance(ann, type):
        if issubclass(ann, Enum) or issubclass(ann, str):
            return TypeSpec("string")
        if issubclass(ann, bool):
            return TypeSpec("bool")
        if issubclass(ann, int):
            raise ContractTypeError("bare int is not allowed: use Int16/Int32/Int64")
        if issubclass(ann, float):
            return TypeSpec("double")
        if issubclass(ann, datetime):
            raise ContractTypeError("bare datetime is not allowed: use UtcDatetime")
        if issubclass(ann, date):
            return TypeSpec("date32[day]")
        if issubclass(ann, BaseModel):
            return TypeSpec("struct", fields=tuple(field_specs(ann)))
    raise ContractTypeError(f"no Arrow mapping for {ann!r}")


def field_specs(model: type[BaseModel]) -> list[FieldSpec]:
    out = []
    for name, info in model.model_fields.items():
        ann, nullable = _unwrap_optional(info.annotation)
        spec = _type_of(ann, tuple(info.metadata))
        out.append(FieldSpec(name, spec, nullable, info.description))
    return out


def schema_description(name: str) -> list[list]:
    return [f.describe() for f in field_specs(dataset(name).model)]


def schema_fingerprint(name: str) -> str:
    payload = json.dumps(schema_description(name), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- pyarrow (lazy)
def _pa_type(spec: TypeSpec):
    import pyarrow as pa

    if spec.kind == "list":
        return pa.list_(_pa_type(spec.child))
    if spec.kind == "struct":
        return pa.struct([pa.field(f.name, _pa_type(f.type), nullable=f.nullable) for f in spec.fields])
    return {
        "string": pa.string(), "bool": pa.bool_(), "int16": pa.int16(), "int32": pa.int32(), "int64": pa.int64(),
        "double": pa.float64(), "date32[day]": pa.date32(), "timestamp[us, tz=UTC]": pa.timestamp("us", tz="UTC"),
    }[spec.kind]


def arrow_schema(name: str):
    """pyarrow schema of a dataset with ``vkm.*`` key-value metadata (dataset, version, fingerprint)."""
    import pyarrow as pa

    spec = dataset(name)
    fields = [pa.field(f.name, _pa_type(f.type), nullable=f.nullable) for f in field_specs(spec.model)]
    return pa.schema(fields, metadata={
        b"vkm.dataset": spec.name.encode(), b"vkm.schema_version": spec.version.encode(),
        b"vkm.schema_fingerprint": schema_fingerprint(name).encode(),
    })


class NullInRequiredColumn(ValueError):
    """pyarrow accepts nulls in non-nullable fields; the writer refuses them explicitly."""


def rows_to_table(name: str, rows: Iterable[BaseModel | dict]):
    """Validate rows with the dataset model and build a pyarrow Table with the contract schema."""
    import pyarrow as pa

    spec = dataset(name)
    dumped = []
    for row in rows:
        if isinstance(row, dict) and "schema_version" not in row:
            row = {**row, "schema_version": spec.version}
        obj = row if isinstance(row, spec.model) else spec.model.model_validate(row)
        dumped.append(obj.model_dump(mode="python"))
    schema = arrow_schema(name)
    table = pa.Table.from_pylist(dumped, schema=schema)
    check_required_columns(table, spec)
    return table


def check_required_columns(table, spec: DatasetSpec) -> None:
    for f in field_specs(spec.model):
        if not f.nullable and f.name in table.column_names and table.column(f.name).null_count:
            raise NullInRequiredColumn(f"{spec.name}.{f.name}: {table.column(f.name).null_count} null(s) in a "
                                       "required column")


def empty_table(name: str):
    return arrow_schema(name).empty_table()


# ---------------------------------------------------------------- canonical rows and fingerprints
def canonical_value(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {k: canonical_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [canonical_value(v) for v in value]
    return value


def canonical_row(row: dict, columns: Sequence[str]) -> str:
    payload = {c: canonical_value(row.get(c)) for c in columns}
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


FP_MOD = 1 << 256


@dataclass(frozen=True)
class RowDigest:
    """Order-independent multiset digest of rows: count and sum of sha256(canonical row) mod 2**256.

    Digests of files add up to the digest of their union, so a dataset spread over many files, a commit and a
    materialised DuckDB table are compared with the same number without sorting (H-48)."""

    rows: int
    total: int

    def __add__(self, other: "RowDigest") -> "RowDigest":
        return RowDigest(self.rows + other.rows, (self.total + other.total) % FP_MOD)

    def hex(self) -> str:
        return f"{self.total:064x}"

    @classmethod
    def parse(cls, rows: int, hex_total: str) -> "RowDigest":
        return cls(int(rows), int(hex_total, 16))


EMPTY_DIGEST = RowDigest(0, 0)


def digest_rows(name: str, rows: Iterable[dict], exclude: frozenset[str] = frozenset()) -> RowDigest:
    spec = dataset(name)
    columns = [c for c in spec.fields if c not in exclude]
    n = 0
    total = 0
    for r in rows:
        row_columns = columns
        historical = HISTORICAL_LOCATOR_SCHEMAS.get(name)
        if historical and r.get("schema_version") == historical[0]:
            if r.get("raw_locator") is not None:
                raise ValueError(f"{name}: historical 0.1.0 row cannot contain raw_locator; produce 0.1.1")
            # Old immutable file digests omitted the field; a materialised NULL
            # must not change their hashes in a mixed old/new snapshot.
            row_columns = [c for c in columns if c != "raw_locator"]
        line = canonical_row(r, row_columns)
        total += int.from_bytes(hashlib.sha256(line.encode("utf-8")).digest(), "big")
        n += 1
    return RowDigest(n, total % FP_MOD)


def digest_table(name: str, table, exclude: frozenset[str] = frozenset()) -> RowDigest:
    """Digest of a pyarrow Table (or anything with ``to_batches()``), streamed batch by batch."""
    acc = EMPTY_DIGEST
    for batch in table.to_batches(max_chunksize=65536):
        acc = acc + digest_rows(name, batch.to_pylist(), exclude)
    return acc


def digest_batches(name: str, batches: Iterable, exclude: frozenset[str] = frozenset()) -> RowDigest:
    acc = EMPTY_DIGEST
    for batch in batches:
        acc = acc + digest_rows(name, batch.to_pylist(), exclude)
    return acc


def fingerprint_of(name: str, digest: RowDigest, content: bool = False, *, schema_version: str | None = None) -> str:
    """Final fingerprint of a dataset digest: binds the dataset name, the column set and the row count."""
    spec = dataset(name)
    columns = [c for c in spec.fields if not (content and c in VOLATILE_COLUMNS)]
    if schema_version is not None and schema_version != spec.version:
        historical = HISTORICAL_LOCATOR_SCHEMAS.get(name)
        if historical is None or schema_version != historical[0]:
            raise ValueError(f"unknown {name} fingerprint schema version: {schema_version}")
        columns = [c for c in columns if c != "raw_locator"]
    kind = "content" if content else "table"
    payload = f"vkm-fp-v2|{kind}|{name}|{','.join(columns)}|{digest.rows}|{digest.hex()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def readable_schema(name: str, version: str | None, fingerprint: str | None) -> bool:
    """Only current or the exact pre-locator historical schema can be read."""
    return ((version, fingerprint) == (dataset(name).version, schema_fingerprint(name))
            or (name in HISTORICAL_LOCATOR_SCHEMAS
                and (version, fingerprint) == HISTORICAL_LOCATOR_SCHEMAS[name]))


def table_fingerprint(name: str, table) -> str:
    """Fingerprint of all contract columns of a table (missing columns count as NULL)."""
    return fingerprint_of(name, digest_table(name, table))


def content_fingerprint(name: str, table) -> str:
    """Fingerprint without volatile columns (created_at, processing_run_id): equal content ⇒ equal value."""
    return fingerprint_of(name, digest_table(name, table, VOLATILE_COLUMNS), content=True)


def all_schema_fingerprints() -> dict[str, str]:
    return {name: schema_fingerprint(name) for name in DATASETS}

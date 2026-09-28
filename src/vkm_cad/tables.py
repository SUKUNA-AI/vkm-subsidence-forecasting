"""Coordinate tables → point rows, and explicit georeference transforms.

A table is CSV/TSV (delimiter sniffed among ``,``, ``;``, TAB; ``decimal=","`` for Russian number formatting), JSON
(a list of objects) or Parquet (needs pyarrow), or inline ``rows``. ``columns`` maps the roles ``name``, ``x``, ``y``,
``z``, ``desc`` to column names. A missing or empty Z stays unknown (``None``): such points are kept 2D in DXF and
skipped by TINs and COGO points (reported), never replaced by a number.

Coordinates are used as given: ``crs_status = UNKNOWN_CRS``. A georeference transform is applied only when passed
explicitly, with a basis (≥ 10 characters); it is recorded as MODEL_CHOICE and the status becomes
``EXPLICIT_TRANSFORM``. ``target_crs_label`` is free text recorded as given — no EPSG is ever inferred.
"""
from __future__ import annotations

import csv
import io
import json
import math
from pathlib import Path
from typing import Any

from vkm_cad.errors import ToolFailure

ROLES = ("name", "x", "y", "z", "desc")
MAX_ROWS = 500_000
TABLE_SUFFIXES = frozenset({".csv", ".tsv", ".txt", ".json", ".parquet"})
TRANSFORMS = ("offset", "helmert2d", "affine2d")


def _float(value: Any, decimal: str, where: str, allow_empty: bool = False) -> float | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if allow_empty:
            return None
        raise ToolFailure("TABLE_FORMAT_ERROR", f"{where}: empty value")
    if isinstance(value, (int, float)):
        out = float(value)
    else:
        text = str(value).strip().replace(" ", "").replace(" ", "")
        if decimal == ",":
            text = text.replace(",", ".")
        try:
            out = float(text)
        except ValueError as exc:
            raise ToolFailure("TABLE_FORMAT_ERROR", f"{where}: {str(value)[:30]!r} is not a number") from exc
    if not math.isfinite(out):
        raise ToolFailure("TABLE_FORMAT_ERROR", f"{where}: not finite")
    return out


def read_records(path: Path, *, delimiter: str | None = None, encoding: str = "utf-8-sig") -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        data = json.loads(path.read_text(encoding=encoding))
        if isinstance(data, dict) and isinstance(data.get("rows"), list):
            data = data["rows"]
        if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
            raise ToolFailure("TABLE_FORMAT_ERROR", "a JSON table is a list of objects (or {\"rows\": [...]})")
        return data
    if suffix == ".parquet":
        try:
            import pyarrow.parquet as pq
        except ImportError as exc:
            raise ToolFailure("FORMAT_NOT_SUPPORTED", "Parquet tables need pyarrow in this environment; use CSV") \
                from exc
        return pq.read_table(path).to_pylist()
    if suffix not in (".csv", ".tsv", ".txt"):
        raise ToolFailure("FORMAT_NOT_SUPPORTED", f"tables are .csv, .tsv, .txt, .json or .parquet, not {suffix}")
    text = path.read_text(encoding=encoding)
    if delimiter is None:
        if suffix == ".tsv":
            delimiter = "\t"
        else:
            try:
                delimiter = csv.Sniffer().sniff(text[:20000], delimiters=",;\t").delimiter
            except csv.Error:
                delimiter = ","
    return list(csv.DictReader(io.StringIO(text), delimiter=delimiter))


def point_rows(records: list[dict[str, Any]], columns: dict[str, str] | None, *, decimal: str = ".",
               require_z: bool = False) -> list[dict[str, Any]]:
    if len(records) > MAX_ROWS:
        raise ToolFailure("PAYLOAD_TOO_LARGE", f"more than {MAX_ROWS} rows")
    columns = {**{r: r for r in ROLES}, **(columns or {})}
    unknown = set(columns) - set(ROLES)
    if unknown:
        raise ToolFailure("INVALID_ARGUMENT", f"column roles are {ROLES}, not {sorted(unknown)}")
    if records:
        keys = set(records[0])
        for role in ("x", "y"):
            if columns[role] not in keys:
                raise ToolFailure("TABLE_FORMAT_ERROR", f"column {columns[role]!r} ({role}) not in the table; "
                                                        f"columns: {sorted(keys)[:30]}")
    rows = []
    for i, rec in enumerate(records, 1):
        where = f"row {i}"
        z = _float(rec.get(columns["z"]), decimal, f"{where} z", allow_empty=not require_z) \
            if columns["z"] in rec else None
        if require_z and z is None:
            raise ToolFailure("TABLE_FORMAT_ERROR", f"{where}: z is required")
        name = rec.get(columns["name"])
        desc = rec.get(columns["desc"])
        rows.append({"name": None if name in (None, "") else str(name)[:200],
                     "x": _float(rec.get(columns["x"]), decimal, f"{where} x"),
                     "y": _float(rec.get(columns["y"]), decimal, f"{where} y"),
                     "z": z, "desc": None if desc in (None, "") else str(desc)[:200]})
    names = [r["name"] for r in rows if r["name"]]
    if len(names) != len(set(names)):
        dup = sorted({n for n in names if names.count(n) > 1})[:10]
        raise ToolFailure("TABLE_FORMAT_ERROR", f"duplicate point names: {dup}")
    return rows


def check_transform(transform: dict[str, Any] | None) -> dict[str, Any] | None:
    if transform is None:
        return None
    kind = transform.get("type")
    if kind not in TRANSFORMS:
        raise ToolFailure("CRS_STATUS_NOT_ALLOWED", f"georeference type is one of {TRANSFORMS}")
    basis = str(transform.get("basis") or "").strip()
    if len(basis) < 10:
        raise ToolFailure("CRS_STATUS_NOT_ALLOWED", "an explicit transform needs a basis (≥ 10 characters): where do "
                                                    "its parameters come from?")
    params = transform.get("params") or {}
    need = {"offset": ("dx", "dy"), "helmert2d": ("tx", "ty", "scale", "rotation_deg"),
            "affine2d": ("a", "b", "c", "d", "e", "f")}[kind]
    missing = [k for k in need if k not in params]
    if missing:
        raise ToolFailure("INVALID_ARGUMENT", f"{kind} needs params {list(need)}; missing {missing}")
    clean = {k: float(params[k]) for k in (*need, "dz") if k in params}
    return {"type": kind, "params": clean, "basis": basis,
            "target_crs_label": (str(transform["target_crs_label"])[:200]
                                 if transform.get("target_crs_label") else None)}


def apply_transform(rows: list[dict[str, Any]], transform: dict[str, Any] | None) -> list[dict[str, Any]]:
    if transform is None:
        return rows
    p = transform["params"]
    out = []
    for row in rows:
        x, y = row["x"], row["y"]
        if transform["type"] == "offset":
            nx, ny = x + p["dx"], y + p["dy"]
        elif transform["type"] == "helmert2d":
            r = math.radians(p["rotation_deg"])
            s = p["scale"]
            nx = p["tx"] + s * (math.cos(r) * x - math.sin(r) * y)
            ny = p["ty"] + s * (math.sin(r) * x + math.cos(r) * y)
        else:
            nx = p["a"] * x + p["b"] * y + p["c"]
            ny = p["d"] * x + p["e"] * y + p["f"]
        z = row["z"]
        if z is not None and "dz" in p:
            z = z + p["dz"]
        out.append({**row, "x": nx, "y": ny, "z": z})
    return out


def crs_block(transform: dict[str, Any] | None) -> dict[str, Any]:
    if transform is None:
        return {"crs_status": "UNKNOWN_CRS", "crs_status_basis": None, "epsg": None,
                "coordinate_space": "DRAWING_UNITS"}
    return {"crs_status": "EXPLICIT_TRANSFORM", "epsg": None, "coordinate_space": "DRAWING_UNITS",
            "crs_status_basis": {"kind": "MODEL_CHOICE", "transform": transform["type"],
                                 "params": transform["params"], "basis": transform["basis"],
                                 "target_crs_label": transform["target_crs_label"]}}


def transform_points(points: list[list[float]], transform: dict[str, Any] | None) -> list[list[float]]:
    rows = [{"x": p[0], "y": p[1], "z": p[2] if len(p) > 2 else None} for p in points]
    return [[r["x"], r["y"]] + ([r["z"]] if r["z"] is not None else []) for r in apply_transform(rows, transform)]

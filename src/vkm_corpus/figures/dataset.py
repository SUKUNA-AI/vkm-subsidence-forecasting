"""``figure_series``: one row per digitized series — a NAV-like derived dataset, never evidence.

Every row carries: ids (figure, source, page, work), the series label as printed, the axes (quantity and unit as
printed, scale kind, calibration with residuals and — for labels read by the local OCR helper — the engine), the
points with a per-point half-width error in value units, the route, the status ``DERIVATION`` and the review status
``AUTO_EXTRACTED_UNREVIEWED``, the availability date (publication date of the work; a year-only date is the last day
of that year and says so), flags, and provenance (PDF sha256, crop box, CAD job and DXF sha256, digitizer version,
config hash). A plotted value is not classified as observation or model result here: the caption and the legend text
decide that at review (``series_nature = UNCLASSIFIED``; words such as «расчёт», «прогноз», «МКЭ» only set a hint).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from vkm_corpus.figures import DIGITIZER_VERSION, FORBIDDEN_STATUSES, REVIEW_STATUS, STATUS
from vkm_corpus.figures.core import split_title

SCHEMA = "vkm.figure_series/0"
ROUTES = ("B_AUTOCAD_PDFIMPORT", "A_NATIVE_VECTOR", "R_RASTER")
MODEL_HINT = re.compile(r"расч[её]т|прогноз|модел|мкэ|fem|численн|simulat|predict", re.I)


class SeriesValidationError(ValueError):
    pass


@dataclass
class FigureContext:
    figure_id: str | None
    source_id: str
    page_id: str
    work_id: str | None = None
    figure_label: str | None = None
    caption_block_id: str | None = None
    caption_text_for_hints: str | None = None       # used for the model hint only; not stored
    source_sha256: str | None = None
    publication_year: int | None = None
    available_from: str | None = None               # ISO date
    available_basis: str | None = None
    site_scope_raw: str | None = None
    extra: dict = field(default_factory=dict)


def series_id(figure_key: str, route: str, index: int, label: str | None, color: str | None) -> str:
    h = hashlib.sha256(f"{figure_key}|{route}|{index}|{label}|{color}".encode()).hexdigest()[:16]
    return f"FS-{h}"


def config_hash(config: dict) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _axis_json(ax) -> dict | None:
    return None if ax is None else ax.to_json()


def build_rows(ctx: FigureContext, result: dict, route: str, provenance: dict, config: dict) -> list[dict]:
    """Rows of one digitized figure (``result`` from :func:`core.digitize` or the raster driver)."""
    if route not in ROUTES:
        raise SeriesValidationError(f"unknown route {route}")
    xa, ya = result.get("x_axis"), result.get("y_axis")
    xq, xu = split_title(result.get("x_title_raw"))
    yq, yu = split_title(result.get("y_title_raw"))
    hint = bool(ctx.caption_text_for_hints and MODEL_HINT.search(ctx.caption_text_for_hints))
    key = ctx.figure_id or f"{ctx.page_id}:{provenance.get('crop_box_pt')}"
    rows = []
    for i, s in enumerate(result.get("series", [])):
        pts = s["points"]
        yerr = [p["y_err"] for p in pts if p.get("y_err") is not None]
        xerr = [p["x_err"] for p in pts if p.get("x_err") is not None]
        label = s.get("label_raw")
        flags = sorted(set(s.get("flags", [])) | ({"MODEL_HINT_IN_CAPTION"} if hint else set())
                       | ({"SCOPE_INHERITED_FROM_SOURCE"} if ctx.site_scope_raw else set()))
        row = {
            "schema": SCHEMA,
            "series_id": series_id(key, route, i, label, s.get("color")),
            "figure_id": ctx.figure_id, "source_id": ctx.source_id, "page_id": ctx.page_id, "work_id": ctx.work_id,
            "figure_label": ctx.figure_label, "caption_block_id": ctx.caption_block_id,
            "series_index": i, "series_label_raw": label, "series_color": s.get("color"),
            "series_nature": "UNCLASSIFIED",
            "x_quantity_raw": xq, "x_unit_raw": xu, "x_title_raw": result.get("x_title_raw"),
            "x_axis_kind": None if xa is None else xa.kind, "x_is_time": bool(xa is not None and xa.kind == "DATE"),
            "y_quantity_raw": yq, "y_unit_raw": yu, "y_title_raw": result.get("y_title_raw"),
            "y_axis_kind": None if ya is None else ya.kind,
            "n_points": len(pts),
            "points": [{k: p.get(k) for k in ("i", "x", "x_date", "y", "x_err", "y_err", "x_drawing", "y_drawing")}
                       for p in pts],
            "x_err_median": None if not xerr else sorted(xerr)[len(xerr) // 2],
            "y_err_median": None if not yerr else sorted(yerr)[len(yerr) // 2],
            "error_model": ("half-width in value units: axis label-fit rms ⊕ coordinate quantum (vector); "
                            "⊕ half the native pixel and half the stroke width (raster)"),
            "calibration": json.dumps({"x": _axis_json(xa), "y": _axis_json(ya),
                                       "plot_box": result.get("plot_box"), "notes": result.get("notes", []),
                                       "axis_status": result.get("axis_status")}, ensure_ascii=False, default=float),
            "sampling": s.get("sampling", "VECTOR_VERTICES"),
            "route": route, "status": STATUS, "review_status": REVIEW_STATUS,
            "available_from": ctx.available_from, "available_basis": ctx.available_basis,
            "publication_year": ctx.publication_year, "source_site_scope_raw": ctx.site_scope_raw,
            "flags": flags,
            "provenance": json.dumps({**provenance, "digitizer_version": DIGITIZER_VERSION,
                                      "config_hash": config_hash(config)}, ensure_ascii=False, default=str),
        }
        validate_row(row)
        rows.append(row)
    return rows


def validate_row(row: dict) -> None:
    if row.get("status") != STATUS or row.get("status") in FORBIDDEN_STATUSES:
        raise SeriesValidationError(f"status must be {STATUS}")
    if row.get("review_status") != REVIEW_STATUS:
        raise SeriesValidationError(f"review_status must be {REVIEW_STATUS}")
    if row.get("route") not in ROUTES:
        raise SeriesValidationError("unknown route")
    for p in row.get("points", []):
        for v in (p.get("x"), p.get("y")):
            if v is not None and (v != v or v in (float("inf"), float("-inf"))):
                raise SeriesValidationError("non-finite value")
        if p.get("y") is not None and p.get("y_err") is None:
            raise SeriesValidationError("a calibrated value needs its error estimate")
    if not row.get("available_from") and row.get("publication_year") is None:
        raise SeriesValidationError("availability: publication date or year required")


def write_jsonl(rows: list[dict], path) -> str:
    """Deterministic JSONL (sorted by series_id); returns sha256."""
    data = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True, default=float) + "\n"
                   for r in sorted(rows, key=lambda r: r["series_id"])).encode("utf-8")
    with open(path, "wb") as f:
        f.write(data)
    return hashlib.sha256(data).hexdigest()


def write_parquet(rows: list[dict], path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    point = pa.struct([("i", pa.int32()), ("x", pa.float64()), ("x_date", pa.string()), ("y", pa.float64()),
                       ("x_err", pa.float64()), ("y_err", pa.float64()), ("x_drawing", pa.float64()),
                       ("y_drawing", pa.float64())])
    schema = pa.schema([
        ("series_id", pa.string()), ("figure_id", pa.string()), ("source_id", pa.string()), ("page_id", pa.string()),
        ("work_id", pa.string()), ("figure_label", pa.string()), ("caption_block_id", pa.string()),
        ("series_index", pa.int32()), ("series_label_raw", pa.string()), ("series_color", pa.string()),
        ("series_nature", pa.string()), ("x_quantity_raw", pa.string()), ("x_unit_raw", pa.string()),
        ("x_title_raw", pa.string()), ("x_axis_kind", pa.string()), ("x_is_time", pa.bool_()),
        ("y_quantity_raw", pa.string()), ("y_unit_raw", pa.string()), ("y_title_raw", pa.string()),
        ("y_axis_kind", pa.string()), ("n_points", pa.int32()), ("points", pa.list_(point)),
        ("x_err_median", pa.float64()), ("y_err_median", pa.float64()), ("error_model", pa.string()),
        ("calibration", pa.string()), ("sampling", pa.string()), ("route", pa.string()), ("status", pa.string()),
        ("review_status", pa.string()), ("available_from", pa.string()), ("available_basis", pa.string()),
        ("publication_year", pa.int32()), ("source_site_scope_raw", pa.string()), ("flags", pa.list_(pa.string())),
        ("provenance", pa.string()), ("schema", pa.string()),
    ])
    table = pa.Table.from_pylist(sorted(rows, key=lambda r: r["series_id"]), schema=schema)
    pq.write_table(table, path)


def summarize(rows: list[dict]) -> dict[str, Any]:
    by_route: dict[str, int] = {}
    for r in rows:
        by_route[r["route"]] = by_route.get(r["route"], 0) + 1
    return {"series": len(rows), "points": sum(r["n_points"] for r in rows),
            "figures": len({r["figure_id"] or r["page_id"] for r in rows}),
            "sources": len({r["source_id"] for r in rows}), "by_route": dict(sorted(by_route.items())),
            "time_series": sum(1 for r in rows if r["x_is_time"]),
            "flags": dict(sorted(_count(f for r in rows for f in r["flags"]).items()))}


def _count(items) -> dict[str, int]:
    out: dict[str, int] = {}
    for i in items:
        out[i] = out.get(i, 0) + 1
    return out

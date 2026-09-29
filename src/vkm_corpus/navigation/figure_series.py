"""NAV part ``figure_series`` (rule ``figure_series_v1``, agent FD2): numeric series digitized from the chart-like
vector figures of the corpus — route A of :mod:`vkm_corpus.figures` (native PDF vectors and the PDF text layer; the
local OCR helper only for tick labels drawn as glyph outlines) as a deterministic build step.

Status: every value is ``DERIVATION`` from a publication with ``review_status = AUTO_EXTRACTED_UNREVIEWED`` — never
an observation of the project, never evidence. A plotted model result and an observation are not told apart
(``series_nature = UNCLASSIFIED``; words such as «расчёт», «прогноз», «МКЭ» in the caption only set
``MODEL_HINT_IN_CAPTION``). The mine/site attribution is the source's (``source_site_scope_raw``, flag
``SCOPE_INHERITED_FROM_SOURCE``) and is never upgraded. Availability in time is the publication date of the work
(``works_availability.available_latest_day``: a year-only date is the last day of that year); no year → ``UNKNOWN``.

**Inputs.** The canon (DuckDB of the snapshot: figures, pages, blocks, sources, ``work_sources`` and
``works_availability``) and the source PDFs of the PRIVATE clone (``$VKM_RESOURCES_ROOT`` or the option
``resources``). The PDFs are needed: route A drops the paths a viewer never shows (clip state of
``get_drawings(extended=True)``) and reads word boxes of the text layer — neither is in the pipeline's
``VECTOR_PATHS_JSON`` / canonical blocks. The part therefore runs on the WORKSTATION (like ``nav outlines``); CORE
receives the built datasets with :func:`import_bundle` (``python -m vkm_corpus.navigation.figure_series import``),
byte for byte, and packs them into ``nav.duckdb`` with the rest of the layer. Without a resources root the builder
returns ``None`` (``SKIPPED_NO_INPUT``). Each PDF's sha256 must equal the canonical ``source_sha256``.

**Candidates** (rule ``chartlike_v1`` of the FD sweep, ``figures/discover.py``): a canonical figure with native
vectors and a PAGE_PT_TL box, ≥ 6 numeric tokens in the primary-layer blocks whose centre lies within 15 pt of the
box, and ≥ 20 line/curve items in the page's drawings that meet the box (``pdf_native.drawings_json`` — the function
that wrote ``VECTOR_PATHS_JSON``). No keyword filter: the caption and page keywords are kept as tags. Pages with
``/Rotate`` ≠ 0 are read in the displayed frame (fd-0.1.3, flag ``ROTATED_PAGE``).

**Datasets** (one Parquet each, sorted, zstd; two builds of one snapshot in one environment are byte-identical):

* ``figure_series_figures`` — one row per candidate figure with its ``figure_status``: ``DIGITIZED`` (both axes
  calibrated, ≥ 1 series), ``AXES_OK_NO_SERIES``, ``X_UNCALIBRATED``, ``Y_UNCALIBRATED``, ``NO_AXES``,
  ``SOURCE_UNAVAILABLE``, ``SOURCE_HASH_MISMATCH``, ``ERROR`` — the axis calibrations (method, rms in pt, labels),
  titles and units as printed, counts, quality flags. Failed and uncalibrated figures stay as rows; no value is
  invented for them.
* ``figure_series`` — one row per series: ids, label and colour as printed, axes, ranges, median errors, the full
  calibration (JSON: fitted labels, residuals, dropped labels, OCR engine when used), provenance (JSON: PDF sha256,
  PyMuPDF, digitizer version, config hash), flags.
* ``figure_series_points`` — one row per point: figure/source/page, series id and label, x and y in data units with
  their printed units (``NULL`` on an uncalibrated axis), per-point half-width errors, the calibration method and rms
  of each axis, the position on the page (``x_page_pt``/``y_page_pt``, PAGE_PT_TL) and ``in_plot_area`` (false: the
  vertex is in the PDF but outside the plot box — hidden by the chart's clip, not checkable against the printed
  figure), status, review status and availability.

* optional, clearly flagged second dataset ``figure_series_raster_figures`` / ``figure_series_raster`` /
  ``figure_series_raster_points`` (same schema, route ``R_RASTER``, flag ``RASTER`` on every row): route R on the
  raster figures of listed pages (option ``raster_pages`` — FD's monitoring-catalogue pages); low resolution, gaps
  not filled (``TRACE_GAP_NOT_FILLED``), always to be reviewed. The query tools leave it out unless asked.

Quality flags added by the part (besides FD's ``X_UNCALIBRATED``, ``LEGEND_UNMATCHED``, ``MULTI_CHAIN_STYLE``,
``LOCAL_OCR_CALIBRATION``, ``MODEL_HINT_IN_CAPTION``, ``AVAILABILITY_UNKNOWN``, ``SCOPE_INHERITED_FROM_SOURCE``):
``POINTS_OUTSIDE_PLOT_AREA``, ``EXTRAPOLATED_BEYOND_TICKS`` (> 25 % of a series' values beyond the printed ticks by
> 10 % of their span — a suspicious calibration), ``STROKES_DRAWN_AS_OUTLINES`` (curves drawn as filled outlines:
not traced), ``ROTATED_PAGE``, ``EMBEDDED_RASTER_IN_FIGURE``, ``OCR_HELPER_UNAVAILABLE``,
``CANDIDATE_RULE_NOT_EVALUATED``, ``WORK_ATTRIBUTION_AMBIGUOUS``.

IDs: ``series_id = FS-<16 hex>`` of (figure id, route, series index, label, colour) — the id of FD's sweep for the
same series; a point is ``(series_id, i)``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import statistics
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

PART = "figure_series"
RULE_VERSION = "figure_series_v1"
SCHEMA = "vkm.figure_series/1"
ROUTE = "A_NATIVE_VECTOR"
DATASETS = ("figure_series_figures", "figure_series", "figure_series_points")
STATUS = "DERIVATION"
REVIEW_STATUS = "AUTO_EXTRACTED_UNREVIEWED"
MANIFEST_FORMAT = "vkm-nav-manifest-v1"

# candidate rule chartlike_v1 (FD sweep) and route A parameters — MODEL_CHOICE, all in the config hash
NUM_MARGIN_PT = 15.0
MIN_NUMERIC_TOKENS = 6
MIN_LINE_CURVE_ITEMS = 20
REGION_MARGIN_PT = 12.0          # route A reads the figure box ± 12 pt (tick labels and titles sit outside the box)
QUANTUM_PT = 0.05                # coordinate precision assumed for native PDF vectors (error floor)
# quality flags (MODEL_CHOICE): a vertex more than 1 % of the plot size outside the plot box is outside the plot area
# (a chart's clip hides it: it cannot be checked against the printed figure); a series with more than 25 % of its
# values beyond the printed tick values by more than 10 % of their span is extrapolated (a suspicious calibration);
# ≥ 20 coloured elongated filled polygons that outnumber the coloured strokes 2:1 are curves drawn as filled outlines
# (route A does not trace them)
OUTSIDE_TOL = 0.01
BEYOND_TICKS_TOL, BEYOND_TICKS_SHARE = 0.25, 0.25
OUTLINE_MIN, OUTLINE_MAX_WIDTH_PT, OUTLINE_MIN_ASPECT = 20, 3.0, 3.0
# axis plausibility (route A): tick labels more than 3 % of the box inside it are not an axis of this plot
LABELS_INSIDE_MARGIN = 0.03
# flags that make the values of a series suspect (the query filter clean_only drops such series); a visual check of
# seeded samples kept these four and left EXTRAPOLATED_BEYOND_TICKS informational (it fired on sound charts whose
# frame runs past the last labelled tick)
SUSPECT_FLAGS = frozenset({"AXIS_LABELS_INSIDE_PLOT", "POWER_OF_TEN_LABELS", "SECOND_Y_AXIS", "SECOND_X_AXIS"})
CONFIG: dict[str, Any] = {
    "rule": RULE_VERSION, "candidate_rule": "chartlike_v1", "num_margin_pt": NUM_MARGIN_PT,
    "min_numeric_tokens": MIN_NUMERIC_TOKENS, "min_line_curve_items": MIN_LINE_CURVE_ITEMS,
    "region_margin_pt": REGION_MARGIN_PT, "quantum_pt": QUANTUM_PT, "ocr_psm": 7, "ocr_dpi": 600,
    "ocr_whitelist": "0123456789.,-/", "glyph_max_size_pt": 18.0, "glyph_default_h_pt": 6.0,
    "outside_tol": OUTSIDE_TOL, "beyond_ticks_tol": BEYOND_TICKS_TOL, "beyond_ticks_share": BEYOND_TICKS_SHARE,
    "outline_min": OUTLINE_MIN, "outline_max_width_pt": OUTLINE_MAX_WIDTH_PT, "outline_min_aspect": OUTLINE_MIN_ASPECT,
    "labels_inside_margin": LABELS_INSIDE_MARGIN, "suspect_flags": sorted(SUSPECT_FLAGS),
}
FIGURE_STATUSES = ("DIGITIZED", "AXES_OK_NO_SERIES", "X_UNCALIBRATED", "Y_UNCALIBRATED", "NO_AXES",
                   "SOURCE_UNAVAILABLE", "SOURCE_HASH_MISMATCH", "ERROR")
ERROR_MODEL = ("half-width in value units: axis label-fit rms ⊕ coordinate quantum 0.05 pt (route A, native vectors); "
               "the stroke width is not added (a vertex is the centre of the line)")

# optional second dataset: route R (raster figures) on listed pages — option ``raster_pages`` (a JSON file
# {"VKM-SRC-…": [page, …]}, FD's monitoring-catalogue pages); same schema, route R_RASTER, flag RASTER on every row
RASTER_ROUTE = "R_RASTER"
RASTER_DATASETS = ("figure_series_raster_figures", "figure_series_raster", "figure_series_raster_points")
RASTER_DPI = 300
RASTER_CONFIG: dict[str, Any] = {
    "rule": RULE_VERSION, "route": "R", "render_dpi": RASTER_DPI, "region_margin_pt": 6.0,
    "layouts": ["CHART", "MIXED", "RASTER_IMAGE"], "min_box_pt": [100.0, 60.0], "sample_markers": True,
    "ocr_psm": 7, "ocr_whitelist": "0123456789.,-/", "outside_tol": OUTSIDE_TOL,
    "beyond_ticks_tol": BEYOND_TICKS_TOL, "beyond_ticks_share": BEYOND_TICKS_SHARE,
}
RASTER_ERROR_MODEL = ("half-width in value units: axis label-fit rms ⊕ half the native pixel (y: ⊕ half the stroke "
                      "width) (route R, raster at 300 dpi); gaps of a trace are not filled")
NOTE = ("DERIVED navigation layer: values digitized by rules from a published chart (route A, native PDF vectors); "
        "DERIVATION, AUTO_EXTRACTED_UNREVIEWED, never an observation and never evidence; each value carries a "
        "half-width error; model results and observations are not told apart (series_nature UNCLASSIFIED); the "
        "site attribution is the source's; available_from = publication date of the work.")


# ================================================================================================ canon → candidates
def _q(con: Any, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    cur = con.execute(sql, list(params))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _relation_exists(con: Any, schema: str, name: str) -> bool:
    for fn, col in (("duckdb_tables()", "table_name"), ("duckdb_views()", "view_name")):
        n = con.execute(f"SELECT count(*) FROM {fn} WHERE schema_name = ? AND {col} = ?", [schema, name]).fetchone()[0]
        if n:
            return True
    return False


def _snapshot_id(con: Any) -> str | None:
    try:
        row = con.execute("SELECT snapshot_id FROM meta.snapshot").fetchone()
    except Exception:  # noqa: BLE001 - a canon copy without meta (tests)
        return None
    return row[0] if row else None


def _work_links(con: Any, stats: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Primary work links of each source with the work's availability (views ``work_sources`` and
    ``works_availability`` of the canon DuckDB); missing views → no links (availability UNKNOWN, noted)."""
    if not (_relation_exists(con, "main", "work_sources") and _relation_exists(con, "main", "works_availability")):
        stats["availability_views_missing"] = True
        return {}
    rows = _q(con, """
        SELECT ws.source_id, ws.work_id, ws.page_start, ws.page_end, wa.publication_year,
               CAST(wa.available_latest_day AS VARCHAR) AS available_latest_day, wa.available_basis
        FROM work_sources ws LEFT JOIN works_availability wa ON wa.work_id = ws.work_id
        WHERE ws.is_primary ORDER BY ws.source_id, ws.work_id""")
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(r["source_id"], []).append(r)
    return out


def _pick_work(links: list[dict[str, Any]], page_index: int) -> tuple[dict[str, Any] | None, bool]:
    """The work of a page: the only primary link, or the one whose page range holds the page; several without a
    range → the first by work id and ``ambiguous``."""
    if not links:
        return None, False
    if len(links) == 1:
        return links[0], False
    inside = [lk for lk in links if lk.get("page_start") is not None and lk.get("page_end") is not None
              and int(lk["page_start"]) <= page_index <= int(lk["page_end"])]
    if len(inside) == 1:
        return inside[0], False
    return (inside or links)[0], True


_FIGURE_SQL = """
    SELECT f.object_id AS figure_id, f.source_id, f.page_id, f.layout_class, f.figure_label, f.caption_block_id,
           f.caption_normalized AS caption, f.bbox_x0, f.bbox_y0, f.bbox_x1, f.bbox_y1,
           f.embedded_image_artifact_id IS NOT NULL AS has_embedded_raster,
           coalesce(len(f.vector_artifacts), 0) AS n_vector_artifacts,
           p.page_index, coalesce(p.rotation_deg, 0) AS rotation, p.normalized_text AS page_text
    FROM canonical.figures f JOIN canonical.pages p ON p.page_id = f.page_id
    WHERE f.bbox_space = 'PAGE_PT_TL' AND f.bbox_x0 IS NOT NULL"""


def load_candidates(con: Any, stats: dict[str, Any], sources: set[str] | None = None) -> list[dict[str, Any]]:
    """Vector figures of the canon with the canonical half of rule ``chartlike_v1`` evaluated (numeric tokens near the
    box), keyword tags, model hint, work and availability, source file — one dict per figure, sorted by id."""
    n_all = int(con.execute("SELECT count(*) FROM canonical.figures").fetchone()[0])
    figs = _q(con, _FIGURE_SQL + " AND f.vector_artifacts IS NOT NULL AND len(f.vector_artifacts) > 0 "
                                 "ORDER BY f.object_id")
    if sources:
        figs = [f for f in figs if f["source_id"] in sources]
    stats["figures_in_snapshot"] = n_all
    stats["with_native_vectors"] = len(figs)
    out = _enrich(con, figs, stats)
    stats["numeric_rule_pass"] = sum(1 for c in out if c["numeric_tokens_near"] >= MIN_NUMERIC_TOKENS)
    return out


def load_raster_candidates(con: Any, pages: dict[str, list[int]], stats: dict[str, Any]) -> list[dict[str, Any]]:
    """Route R candidates (FD's raster sweep): figures on the listed pages with layout CHART, MIXED or RASTER_IMAGE
    and a box larger than 100 × 60 pt; a figure with native vectors belongs to route A unless its layout is
    RASTER_IMAGE."""
    pids = sorted(f"{sid}:p{int(p):04d}" for sid, ps in pages.items() for p in ps)
    figs = _q(con, _FIGURE_SQL + """ AND f.page_id IN (SELECT unnest(?))
        AND f.layout_class IN ('CHART', 'MIXED', 'RASTER_IMAGE')
        AND (f.bbox_x1 - f.bbox_x0) > 100 AND (f.bbox_y1 - f.bbox_y0) > 60 ORDER BY f.object_id""", [pids])
    keep = [f for f in figs if not (f["n_vector_artifacts"] and f["layout_class"] != "RASTER_IMAGE")]
    stats.update({"pages_listed": len(pids), "figures_on_pages": len(figs), "raster_candidates": len(keep)})
    return _enrich(con, keep, stats)


def _enrich(con: Any, figs: list[dict[str, Any]], stats: dict[str, Any]) -> list[dict[str, Any]]:
    """Numeric tokens near the box, keyword tags, model hint, work and availability, source file of each figure."""
    from vkm_corpus.figures.dataset import MODEL_HINT
    from vkm_corpus.figures.discover import CORE, KW_RE, NUM_TOKEN

    pages = sorted({f["page_id"] for f in figs})
    blocks: dict[str, list[tuple[float, float, str]]] = {}
    if pages:
        for r in _q(con, """
                SELECT page_id, (bbox_x0 + bbox_x1) / 2 AS cx, (bbox_y0 + bbox_y1) / 2 AS cy, text
                FROM canonical.blocks
                WHERE is_primary_layer AND bbox_space = 'PAGE_PT_TL' AND bbox_x0 IS NOT NULL AND bbox_y0 IS NOT NULL
                  AND bbox_x1 IS NOT NULL AND bbox_y1 IS NOT NULL AND page_id IN (SELECT unnest(?))""", [pages]):
            blocks.setdefault(r["page_id"], []).append((float(r["cx"]), float(r["cy"]), r["text"] or ""))
    src = {r["source_id"]: r for r in _q(con, """
        SELECT source_id, canonical_path, source_sha256, site_scope_raw FROM canonical.sources ORDER BY source_id""")}
    links = _work_links(con, stats)
    out = []
    for f in figs:
        x0, y0, x1, y1 = (float(f[k]) for k in ("bbox_x0", "bbox_y0", "bbox_x1", "bbox_y1"))
        m = NUM_MARGIN_PT
        nums = sum(len(NUM_TOKEN.findall(t)) for cx, cy, t in blocks.get(f["page_id"], ())
                   if x0 - m <= cx <= x1 + m and y0 - m <= cy <= y1 + m)
        cap = f["caption"] or ""
        page_text = f["page_text"] or ""
        kw_cap = sorted(k for k, rx in KW_RE.items() if rx.search(cap))
        kw_page = sorted(k for k, rx in KW_RE.items() if rx.search(page_text))
        s = src.get(f["source_id"]) or {}
        work, ambiguous = _pick_work(links.get(f["source_id"], []), int(f["page_index"]))
        work = work or {}
        out.append({
            "figure_id": f["figure_id"], "source_id": f["source_id"], "page_id": f["page_id"],
            "page_index": int(f["page_index"]), "rotation": int(f["rotation"] or 0),
            "layout_class": f["layout_class"], "figure_label": f["figure_label"],
            "caption_block_id": f["caption_block_id"], "caption": cap,
            "bbox": [x0, y0, x1, y1], "has_embedded_raster": bool(f["has_embedded_raster"]),
            "numeric_tokens_near": int(nums), "keywords_caption": kw_cap, "keywords_page": kw_page,
            "core_keyword": bool((set(kw_cap) | set(kw_page)) & CORE),
            "model_hint_in_caption": bool(MODEL_HINT.search(cap)),
            "source_path_logical": s.get("canonical_path"), "source_sha256": s.get("source_sha256"),
            "site_scope_raw": s.get("site_scope_raw"), "work_id": work.get("work_id"),
            "work_ambiguous": ambiguous, "publication_year": work.get("publication_year"),
            "available_latest_day": work.get("available_latest_day"),
            "available_basis": work.get("available_basis"),
        })
    return out


# ================================================================================================ worker (one source)
def _ocr_engine():
    """Tesseract for glyph-outline tick labels when it and OpenCV/SciPy are importable, else None."""
    try:
        import cv2  # noqa: F401
        import scipy.spatial  # noqa: F401

        from vkm_corpus.figures.labels_ocr import OcrEngine
    except Exception:  # noqa: BLE001
        return None
    eng = OcrEngine()
    return eng if eng.available() else None


def ocr_provenance() -> dict[str, Any] | None:
    eng = _ocr_engine()
    if eng is None:
        return None
    p = eng.provenance()
    p.pop("calls", None)
    return p


def line_curve_items(page: Any, bbox: list[float]) -> int:
    """'l' and 'c' items of the page's drawings that meet the box — counted on ``pdf_native.drawings_json`` exactly
    as the pipeline wrote ``VECTOR_PATHS_JSON`` (FD's ``_vstats``)."""
    from vkm_corpus.extract.pdf_native import drawings_json

    doc = drawings_json(page, clip=tuple(bbox))
    return sum(1 for p in doc["paths"] for it in p["items"] if it[0] in ("l", "c"))


def _clean_error(exc: BaseException, root: str | None) -> str:
    msg = f"{type(exc).__name__}: {exc}"
    if root:
        msg = msg.replace(str(root), "$VKM_RESOURCES_ROOT")
    msg = re.sub(r"(/home/|/mnt/|[A-Za-z]:\\)\S*", "<path>", msg)
    return msg[:200]


def _axis_summary(ax: Any, pt_per_unit: float = 1.0) -> dict[str, Any]:
    if ax is None:
        return {"kind": None, "method": None, "label_source": None, "n_labels": None, "rms_pt": None}
    return {"kind": ax.kind, "method": ax.method, "label_source": ax.label_source, "n_labels": len(ax.labels),
            "rms_pt": float(ax.residual_rms) * pt_per_unit}


def _context(c: dict[str, Any]):
    from vkm_corpus.figures.dataset import FigureContext

    day, year = c.get("available_latest_day"), c.get("publication_year")
    basis = (c.get("available_basis") or "ASSUMED_FROM_PUBLICATION") if (day or year is not None) else "UNKNOWN"
    return FigureContext(figure_id=c["figure_id"], source_id=c["source_id"], page_id=c["page_id"],
                         work_id=c.get("work_id"), figure_label=c.get("figure_label"),
                         caption_block_id=c.get("caption_block_id"), caption_text_for_hints=c.get("caption") or None,
                         source_sha256=c.get("source_sha256"), publication_year=year, available_from=day,
                         available_basis=basis, site_scope_raw=c.get("site_scope_raw"))


def _figure_base(c: dict[str, Any], n_lc: int | None) -> dict[str, Any]:
    ctx = _context(c)
    flags = set()
    if c.get("rotation"):
        flags.add("ROTATED_PAGE")
    if c.get("has_embedded_raster"):
        flags.add("EMBEDDED_RASTER_IN_FIGURE")
    if c.get("model_hint_in_caption"):
        flags.add("MODEL_HINT_IN_CAPTION")
    if c.get("site_scope_raw"):
        flags.add("SCOPE_INHERITED_FROM_SOURCE")
    if ctx.available_basis == "UNKNOWN":
        flags.add("AVAILABILITY_UNKNOWN")
    if c.get("work_ambiguous"):
        flags.add("WORK_ATTRIBUTION_AMBIGUOUS")
    x0, y0, x1, y1 = c["bbox"]
    return {
        "figure_id": c["figure_id"], "source_id": c["source_id"], "page_id": c["page_id"],
        "page_index": c["page_index"], "work_id": c.get("work_id"), "figure_label": c.get("figure_label"),
        "caption_block_id": c.get("caption_block_id"), "layout_class": c.get("layout_class"),
        "bbox_x0": x0, "bbox_y0": y0, "bbox_x1": x1, "bbox_y1": y1, "page_rotation": int(c.get("rotation") or 0),
        "numeric_tokens_near": c["numeric_tokens_near"], "vector_line_curve_items": n_lc,
        "core_keyword": c["core_keyword"], "keywords_caption": list(c["keywords_caption"]),
        "keywords_page": list(c["keywords_page"]), "model_hint_in_caption": bool(c["model_hint_in_caption"]),
        "has_embedded_raster": bool(c["has_embedded_raster"]),
        "figure_status": None, "axis_status": None, "plot_box": None,
        "x_axis_kind": None, "x_cal_method": None, "x_cal_label_source": None, "x_cal_n_labels": None,
        "x_cal_rms_pt": None, "x_title_raw": None, "x_quantity_raw": None, "x_unit_raw": None, "x_is_time": None,
        "y_axis_kind": None, "y_cal_method": None, "y_cal_label_source": None, "y_cal_n_labels": None,
        "y_cal_rms_pt": None, "y_title_raw": None, "y_quantity_raw": None, "y_unit_raw": None,
        "n_series": 0, "n_series_both_axes": 0, "n_points": 0, "n_points_both_axes": 0, "n_points_outside_plot": 0,
        "n_time_series": 0, "n_marker_series": 0, "n_labelled_series": 0, "legend_without_curve": [], "notes": [],
        "flags": sorted(flags), "error": None,
        "available_from": ctx.available_from, "available_basis": ctx.available_basis,
        "publication_year": ctx.publication_year, "source_site_scope_raw": c.get("site_scope_raw"),
        "route": ROUTE, "digitizer_version": None, "config_hash": None, "rule_version": RULE_VERSION,
        "status": STATUS, "review_status": REVIEW_STATUS, "schema": SCHEMA,
    }


def in_plot_area(box: list[float] | tuple, x: float | None, y: float | None) -> bool | None:
    """The vertex lies in the plot box (± ``OUTSIDE_TOL`` of its size); None without page coordinates."""
    if x is None or y is None:
        return None
    x0, y0, x1, y1 = box
    tx, ty = OUTSIDE_TOL * abs(x1 - x0), OUTSIDE_TOL * abs(y1 - y0)
    return bool(x0 - tx <= x <= x1 + tx and y0 - ty <= y <= y1 + ty)


def beyond_ticks_share(ax: Any, values: list[float]) -> float:
    """Share of values beyond the printed tick values of the axis by more than ``BEYOND_TICKS_TOL`` of their span
    (log10 axes compared in decades)."""
    if ax is None or not values or not getattr(ax, "labels", None):
        return 0.0
    log = ax.kind == "LOG10"
    t = (lambda v: math.log10(v) if v > 0 else float("nan")) if log else (lambda v: v)
    ticks = [t(lab[1]) for lab in ax.labels]
    lo, hi = min(ticks), max(ticks)
    tol = BEYOND_TICKS_TOL * ((hi - lo) or 1.0)
    out = sum(1 for v in values if not (lo - tol <= t(v) <= hi + tol))
    return out / len(values)


def outlined_strokes(paths: list) -> tuple[int, int]:
    """(coloured elongated filled polygons, coloured strokes) of a region: many of the first and few of the second
    mean curves drawn as filled outlines of their strokes (a PDF printer's rendering) — not traced by route A."""
    import numpy as np

    from vkm_corpus.figures.primitives import is_black, is_greyish

    n_out = n_str = 0
    for p in paths:
        if is_black(p.color) or is_greyish(p.color):
            continue
        if p.kind != "FILL":
            n_str += 1
            continue
        if len(p.pts) < 4:
            continue
        q = p.pts - p.pts.mean(0)
        try:
            _, _, vt = np.linalg.svd(q, full_matrices=False)
        except np.linalg.LinAlgError:
            continue
        length, width = float(np.ptp(q @ vt[0])), float(np.ptp(q @ vt[1]))
        if width <= OUTLINE_MAX_WIDTH_PT and length >= OUTLINE_MIN_ASPECT * max(width, 1e-3):
            n_out += 1
    return n_out, n_str


# ------------------------------------------------------------------------------------------------ axis plausibility
_POWER_OF_TEN = re.compile(r"^10\d{1,2}$")


def label_centres(ax: Any, texts: list) -> list[tuple[float, float]]:
    """Page centres (x, y) of the text-layer words that gave the axis its tick labels (same text, nearest along the
    axis); none for labels read by OCR."""
    out = []
    for text, _value, pos, _snapped, _src in getattr(ax, "labels", []) or []:
        cands = [t for t in texts if t.text.strip() == text]
        if not cands:
            continue
        t = min(cands, key=lambda t: abs((t.yc if ax.orient == "y" else t.xc) - pos))
        out.append((float(t.xc), float(t.yc)))
    return out


def labels_inside_plot(ax: Any, texts: list, box) -> bool:
    """The tick labels of the axis stand inside the plot box, across the axis (a y label column between the left and
    right edges, an x label row between the top and bottom): a legend or curve labels taken for an axis."""
    cs = label_centres(ax, texts)
    if len(cs) < 3:
        return False
    x0, y0, x1, y1 = box
    if ax.orient == "y":
        xm, w = statistics.median(c[0] for c in cs), x1 - x0
        return x0 + LABELS_INSIDE_MARGIN * w < xm < x1 - LABELS_INSIDE_MARGIN * w
    ym, h = statistics.median(c[1] for c in cs), y1 - y0
    return y0 + LABELS_INSIDE_MARGIN * h < ym < y1 - LABELS_INSIDE_MARGIN * h


def other_axis(chosen: Any, cands: list, texts: list, box) -> bool:
    """Another axis candidate of the same orientation with other labels, on the opposite side of the plot box and
    with another scale: a chart with two y (or x) axes — which series belongs to which axis is not known."""
    if chosen is None or not texts:
        return False
    mine = {(lab[0], round(float(lab[2]), 1)) for lab in chosen.labels}
    cs = label_centres(chosen, texts)
    if len(cs) < 3:
        return False
    k = 0 if chosen.orient == "y" else 1
    x0, y0, x1, y1 = box
    mid = (x0 + x1) / 2 if k == 0 else (y0 + y1) / 2
    side = statistics.median(c[k] for c in cs) < mid
    lo, hi = (y0, y1) if k == 0 else (x0, x1)
    span = abs(float(chosen.value(hi)) - float(chosen.value(lo))) or 1.0
    for c in cands:
        # an axis is snapped to its tick marks at least in part; a free column of numbers (curve labels such as
        # «σ = −170 MPa» at the curve ends) is not a second axis
        if c is chosen or c.orient != chosen.orient or len(c.labels) < 3 or c.method == "TEXT_CENTRE":
            continue
        theirs = {(lab[0], round(float(lab[2]), 1)) for lab in c.labels}
        if len(theirs & mine) >= 0.5 * min(len(theirs), len(mine)):
            continue                      # the same labels clustered another way (a shared «0» is allowed)
        oc = label_centres(c, texts)
        if len(oc) < 3 or (statistics.median(p[k] for p in oc) < mid) == side:
            continue
        if max(abs(float(c.value(p)) - float(chosen.value(p))) for p in (lo, hi)) > 0.01 * span:
            return True
    return False


def power_of_ten_labels(ax: Any) -> bool:
    """Tick labels such as «100, 101, 102, 103»: powers of ten whose superscript exponent merged into the number
    (10³ read as 103) — a log axis calibrated as a linear one."""
    labs = getattr(ax, "labels", None) or []
    if len(labs) < 3 or not all(_POWER_OF_TEN.match(str(lab[0]).strip()) for lab in labs):
        return False
    vals = sorted(float(lab[1]) for lab in labs)
    return all(abs(b - a - 1.0) < 1e-9 for a, b in zip(vals, vals[1:]))


def axis_flags(res: dict[str, Any], texts: list, paths: list, region) -> set[str]:
    """Plausibility flags of the calibration of one figure (route A): labels inside the plot, two axes of one
    orientation, power-of-ten labels read as numbers."""
    from vkm_corpus.figures.calibrate import detect_axes, structure_lines
    from vkm_corpus.figures.series import legend_text_ids

    xa, ya, box = res["x_axis"], res["y_axis"], res["plot_box"]
    flags = set()
    for ax in (xa, ya):
        if ax is None:
            continue
        # labels snapped to tick marks are an axis even inside the frame (an x axis at y = 0, a top axis): only
        # labels that stand free (curve labels, legend values, node numbers) are suspect
        if ax.label_source == "NATIVE" and ax.method != "SNAPPED_TO_TICKS" and labels_inside_plot(ax, texts, box):
            flags.add("AXIS_LABELS_INSIDE_PLOT")
        if power_of_ten_labels(ax):
            flags.add("POWER_OF_TEN_LABELS")
    if (xa is not None and xa.label_source == "NATIVE") or (ya is not None and ya.label_source == "NATIVE"):
        snap_h, snap_v = structure_lines(paths)
        _, _, cands = detect_axes(texts, snap_h, snap_v, exclude=legend_text_ids(paths, texts, region))
        if ya is not None and ya.label_source == "NATIVE" and other_axis(ya, cands, texts, box):
            flags.add("SECOND_Y_AXIS")
        if xa is not None and xa.label_source == "NATIVE" and other_axis(xa, cands, texts, box):
            flags.add("SECOND_X_AXIS")
    return flags


def _strip_ocr_calls(res: dict[str, Any]) -> None:
    for ax in (res.get("x_axis"), res.get("y_axis")):
        if ax is not None and getattr(ax, "ocr", None):
            ax.ocr = {k: v for k, v in ax.ocr.items() if k != "calls"}      # a run counter, not provenance


def digitize_figure(page: Any, c: dict[str, Any], engine: Any, source_sha256: str | None) -> dict[str, Any]:
    """Route A on one candidate: → {"figure": row, "series": [series rows], "points": [point rows]}."""
    import pymupdf

    from vkm_corpus.figures import core, labels_ocr, pdf_route

    bx0, by0, bx1, by1 = c["bbox"]
    m = REGION_MARGIN_PT
    region = (bx0 - m, by0 - m, bx1 + m, by1 + m)
    texts, paths = pdf_route.load_page(page, region)
    reader = None
    if engine is not None:
        reader = labels_ocr.make_glyph_reader(page, lambda X, Y: (X, Y), engine, max_size=CONFIG["glyph_max_size_pt"],
                                              default_h=CONFIG["glyph_default_h_pt"], y_down=True)
    res = core.digitize(texts, paths, region, quantum=QUANTUM_PT, glyph_reader=reader)
    _strip_ocr_calls(res)
    prov = {"route": "A", "source_sha256": source_sha256, "vector_source": "PyMuPDF get_drawings(extended=True), "
            "clip-aware", "text_source": "PDF text layer (PyMuPDF words)", "pymupdf": str(pymupdf.VersionBind),
            "rule_version": RULE_VERSION, "region_pt": [float(v) for v in region]}
    flags = set()
    if res["axis_status"] != "OK" and engine is None:
        flags.add("OCR_HELPER_UNAVAILABLE")          # the glyph-label fallback could not be tried
    n_outline, n_stroke = outlined_strokes(paths)
    if n_outline >= OUTLINE_MIN and n_outline > 2 * n_stroke:
        flags.add("STROKES_DRAWN_AS_OUTLINES")       # curves drawn as filled outlines: not traced, series missing
    suspect = axis_flags(res, texts, paths, region)  # a suspect calibration makes every series of the figure suspect
    return assemble(c, res, ROUTE, prov, CONFIG, extra_flags=flags | suspect, series_flags=suspect,
                    n_lc=c.get("_n_lc"))


def assemble(c: dict[str, Any], res: dict[str, Any], route: str, prov: dict[str, Any], config: dict[str, Any], *,
             extra_flags: set[str] | frozenset = frozenset(), series_flags: set[str] | frozenset = frozenset(),
             n_lc: int | None = None, pt_per_unit: float = 1.0, error_model: str = ERROR_MODEL) -> dict[str, Any]:
    """A digitization result (page coordinates in PAGE_PT_TL) → figure, series and point rows (FD's ``build_rows``
    validates every series: DERIVATION, an error for every calibrated value, availability). ``pt_per_unit``
    converts the axes' residuals from drawing units (route R: analysed pixels) to points; ``series_flags`` go to
    every series of the figure."""
    from vkm_corpus.figures import DIGITIZER_VERSION
    from vkm_corpus.figures.core import split_title
    from vkm_corpus.figures.dataset import build_rows, config_hash, is_time_axis

    fd_rows = build_rows(_context(c), res, route, prov, config)
    fig = _figure_base(c, n_lc)
    fig["route"] = route
    xa, ya = res["x_axis"], res["y_axis"]
    xs, ys = _axis_summary(xa, pt_per_unit), _axis_summary(ya, pt_per_unit)
    xq, xu = split_title(res.get("x_title_raw"))
    yq, yu = split_title(res.get("y_title_raw"))
    axis_status = res["axis_status"]
    box = res["plot_box"]
    series_rows, point_rows = [], []
    for r in fd_rows:
        pts = r.pop("points")
        r.pop("schema", None)
        xv = [p["x"] for p in pts if p.get("x") is not None]
        yv = [p["y"] for p in pts if p.get("y") is not None]
        dates = [p["x_date"] for p in pts if p.get("x_date")]
        cal = "BOTH" if (xa is not None and ya is not None) else "X_ONLY" if xa is not None else \
            "Y_ONLY" if ya is not None else "NONE"
        inside = [in_plot_area(box, p.get("x_drawing"), p.get("y_drawing")) for p in pts]
        n_out = sum(1 for v in inside if v is False)
        extra = set(series_flags)
        if n_out:
            extra.add("POINTS_OUTSIDE_PLOT_AREA")
        if max(beyond_ticks_share(xa, xv), beyond_ticks_share(ya, yv)) > BEYOND_TICKS_SHARE:
            extra.add("EXTRAPOLATED_BEYOND_TICKS")
        r.update({
            "axes_calibrated": cal, "x_cal_method": xs["method"], "x_cal_rms_pt": xs["rms_pt"],
            "y_cal_method": ys["method"], "y_cal_rms_pt": ys["rms_pt"],
            "x_min": min(xv) if xv else None, "x_max": max(xv) if xv else None,
            "y_min": min(yv) if yv else None, "y_max": max(yv) if yv else None,
            "x_date_min": min(dates) if dates else None, "x_date_max": max(dates) if dates else None,
            "n_points_outside_plot": n_out, "flags": sorted(set(r["flags"]) | extra),
            "error_model": error_model, "rule_version": RULE_VERSION, "schema": SCHEMA,
        })
        series_rows.append(r)
        for p, ins in zip(pts, inside):
            point_rows.append({
                "series_id": r["series_id"], "i": int(p["i"]), "figure_id": r["figure_id"],
                "source_id": r["source_id"], "page_id": r["page_id"], "series_label_raw": r["series_label_raw"],
                "x": p.get("x"), "x_date": p.get("x_date"), "y": p.get("y"), "x_err": p.get("x_err"),
                "y_err": p.get("y_err"), "x_unit_raw": r["x_unit_raw"], "y_unit_raw": r["y_unit_raw"],
                "x_cal_method": xs["method"], "x_cal_rms_pt": xs["rms_pt"], "y_cal_method": ys["method"],
                "y_cal_rms_pt": ys["rms_pt"], "x_page_pt": p.get("x_drawing"), "y_page_pt": p.get("y_drawing"),
                "in_plot_area": ins, "status": STATUS, "review_status": REVIEW_STATUS,
                "available_from": r["available_from"], "available_basis": r["available_basis"],
            })
    both = [r for r in series_rows if r["axes_calibrated"] == "BOTH"]
    flags = set(fig["flags"]) | set(extra_flags)
    for r in series_rows:
        flags |= {f for f in r["flags"] if f in ("LOCAL_OCR_CALIBRATION", "MODEL_HINT_IN_CAPTION", "RASTER",
                                                 "TRACE_GAP_NOT_FILLED", "POINTS_OUTSIDE_PLOT_AREA",
                                                 "EXTRAPOLATED_BEYOND_TICKS") or f in SUSPECT_FLAGS}
    for ax in (xa, ya):
        if ax is not None and ax.label_source == "LOCAL_OCR":
            flags.add("LOCAL_OCR_CALIBRATION")
    if axis_status == "OK":
        fstatus = "DIGITIZED" if both else "AXES_OK_NO_SERIES"
    else:
        fstatus = axis_status
    fig.update({
        "figure_status": fstatus, "axis_status": axis_status, "plot_box": [float(v) for v in res["plot_box"]],
        "x_axis_kind": xs["kind"], "x_cal_method": xs["method"], "x_cal_label_source": xs["label_source"],
        "x_cal_n_labels": xs["n_labels"], "x_cal_rms_pt": xs["rms_pt"], "x_title_raw": res.get("x_title_raw"),
        "x_quantity_raw": xq, "x_unit_raw": xu, "x_is_time": bool(is_time_axis(xa, res.get("x_title_raw"))),
        "y_axis_kind": ys["kind"], "y_cal_method": ys["method"], "y_cal_label_source": ys["label_source"],
        "y_cal_n_labels": ys["n_labels"], "y_cal_rms_pt": ys["rms_pt"], "y_title_raw": res.get("y_title_raw"),
        "y_quantity_raw": yq, "y_unit_raw": yu,
        "n_series": len(series_rows), "n_series_both_axes": len(both), "n_points": len(point_rows),
        "n_points_both_axes": sum(r["n_points"] for r in both),
        "n_points_outside_plot": sum(r["n_points_outside_plot"] for r in series_rows),
        "n_time_series": sum(1 for r in both if r["x_is_time"]),
        "n_marker_series": sum(1 for r in series_rows if r["sampling"] in ("MARKER_CENTRES", "AT_SERIES_MARKERS",
                                                                           "AT_AXIS_MARKERS")),
        "n_labelled_series": sum(1 for r in series_rows if r["series_label_raw"]),
        "legend_without_curve": [str(x) for x in res.get("legend_without_curve") or []],
        "notes": [str(x) for x in res.get("notes") or []], "flags": sorted(flags),
        "digitizer_version": DIGITIZER_VERSION, "config_hash": config_hash(config),
    })
    return {"figure": fig, "series": series_rows, "points": point_rows}


def _failed(c: dict[str, Any], status: str, n_lc: int | None, error: str | None = None, route: str = ROUTE,
            config: dict[str, Any] | None = None) -> dict[str, Any]:
    from vkm_corpus.figures import DIGITIZER_VERSION
    from vkm_corpus.figures.dataset import config_hash

    fig = _figure_base(c, n_lc)
    fig.update({"figure_status": status, "error": error, "digitizer_version": DIGITIZER_VERSION,
                "config_hash": config_hash(config or CONFIG), "route": route})
    if route == ROUTE and n_lc is None:   # the vector half of the rule was not evaluated: a candidate by the canon only
        fig["flags"] = sorted(set(fig["flags"]) | {"CANDIDATE_RULE_NOT_EVALUATED"})
    elif route == RASTER_ROUTE:
        fig["flags"] = sorted(set(fig["flags"]) | {"RASTER"})
    return {"figure": fig, "series": [], "points": []}


def source_worker(task: dict[str, Any]) -> dict[str, Any]:
    """One source: read the PDF once (sha256 against the canon), finish rule ``chartlike_v1`` on every candidate and
    digitize the chart-like ones. Pure function of its task (runs in a pool)."""
    os.environ.setdefault("OMP_THREAD_LIMIT", "1")
    import pymupdf

    figures = sorted(task["figures"], key=lambda c: (c["page_index"], c["figure_id"]))
    out: dict[str, Any] = {"source_id": task["source_id"], "results": [], "not_chartlike": 0, "ocr_calls": 0,
                           "seconds": [], "status": "OK"}
    path = task.get("path")
    if not path or not os.path.isfile(path):
        out["status"] = "SOURCE_UNAVAILABLE"
        out["results"] = [_failed(c, "SOURCE_UNAVAILABLE", None, "source file not found under the resources root")
                          for c in figures]
        return out
    data = Path(path).read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    if task.get("expected_sha256") and sha != task["expected_sha256"]:
        out["status"] = "SOURCE_HASH_MISMATCH"
        out["results"] = [_failed(c, "SOURCE_HASH_MISMATCH", None, "PDF sha256 differs from the canonical source_sha256")
                          for c in figures]
        return out
    engine = _ocr_engine() if task.get("ocr", True) else None
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        for c in figures:
            t0 = time.perf_counter()
            try:
                page = doc[c["page_index"] - 1]
                n_lc = line_curve_items(page, c["bbox"])
            except Exception as exc:  # noqa: BLE001 — recorded per figure
                out["results"].append(_failed(c, "ERROR", None, _clean_error(exc, task.get("root"))))
                continue
            if n_lc < MIN_LINE_CURVE_ITEMS:
                out["not_chartlike"] += 1
                continue
            try:
                res = digitize_figure(page, {**c, "_n_lc": n_lc}, engine, sha)
            except Exception as exc:  # noqa: BLE001 — recorded per figure, never fatal
                res = _failed(c, "ERROR", n_lc, _clean_error(exc, task.get("root")))
            out["results"].append(res)
            out["seconds"].append(round(time.perf_counter() - t0, 3))
    finally:
        doc.close()
    out["ocr_calls"] = engine.calls if engine is not None else 0
    return out


def digitize_raster_figure(path: str, c: dict[str, Any], engine: Any, source_sha256: str | None,
                           ocr_prov: dict[str, Any] | None) -> dict[str, Any]:
    """Route R on one raster figure (rendered at 300 dpi): positions go back to PAGE_PT_TL, residuals to points."""
    from vkm_corpus.figures import raster as R
    from vkm_corpus.figures.raster_digitize import digitize_raster

    fig = R.load_region(path, c["page_index"], tuple(c["bbox"]), dpi=RASTER_DPI,
                        margin=RASTER_CONFIG["region_margin_pt"])
    res = digitize_raster(fig, engine, sample_markers=RASTER_CONFIG["sample_markers"])
    _strip_ocr_calls(res)
    for s in res["series"]:
        for p in s["points"]:
            X, Y = fig.to_page(p["x_drawing"], p["y_drawing"])
            p["x_drawing"], p["y_drawing"] = float(X), float(Y)
    bx0, by0, bx1, by1 = res["plot_box"]
    X0, Y0 = fig.to_page(bx0, by0)
    X1, Y1 = fig.to_page(bx1, by1)
    res["plot_box"] = [float(X0), float(Y0), float(X1), float(Y1)]
    res["x_title_raw"] = res["y_title_raw"] = None                    # route R does not read axis titles
    qc = {k: (round(float(v), 4) if isinstance(v, (int, float)) else v) for k, v in (res.get("qc") or {}).items()}
    prov = {"route": "R", "source_sha256": source_sha256, "render_dpi": RASTER_DPI,
            "px_per_pt": float(fig.px_per_pt), "origin_pt": [float(v) for v in fig.origin_pt],
            "native_px_per_pt": None if fig.native_px_per_pt is None else float(fig.native_px_per_pt),
            "drawing_units": "analysed pixels (calibration); page points (points)", "ocr": ocr_prov, "qc": qc,
            "rule_version": RULE_VERSION}
    return assemble(c, res, RASTER_ROUTE, prov, RASTER_CONFIG, extra_flags={"RASTER"},
                    pt_per_unit=1.0 / float(fig.px_per_pt), error_model=RASTER_ERROR_MODEL)


def raster_worker(task: dict[str, Any]) -> dict[str, Any]:
    """One source of route R: sha256 against the canon, then each listed raster figure (needs Tesseract and
    OpenCV: without them every figure is an ERROR row, nothing is invented)."""
    os.environ.setdefault("OMP_THREAD_LIMIT", "1")
    figures = sorted(task["figures"], key=lambda c: (c["page_index"], c["figure_id"]))
    out: dict[str, Any] = {"source_id": task["source_id"], "results": [], "ocr_calls": 0, "seconds": [],
                           "status": "OK"}
    fail = lambda c, st, err: _failed(c, st, None, err, route=RASTER_ROUTE, config=RASTER_CONFIG)  # noqa: E731
    path = task.get("path")
    if not path or not os.path.isfile(path):
        out["status"] = "SOURCE_UNAVAILABLE"
        out["results"] = [fail(c, "SOURCE_UNAVAILABLE", "source file not found under the resources root")
                          for c in figures]
        return out
    sha = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    if task.get("expected_sha256") and sha != task["expected_sha256"]:
        out["status"] = "SOURCE_HASH_MISMATCH"
        out["results"] = [fail(c, "SOURCE_HASH_MISMATCH", "PDF sha256 differs from the canonical source_sha256")
                          for c in figures]
        return out
    engine = _ocr_engine()
    if engine is None:
        out["status"] = "OCR_HELPER_UNAVAILABLE"
        out["results"] = [fail(c, "ERROR", "route R needs the local OCR helper (tesseract) and OpenCV")
                          for c in figures]
        return out
    ocr_prov = {k: v for k, v in engine.provenance().items() if k != "calls"}
    for c in figures:
        t0 = time.perf_counter()
        try:
            res = digitize_raster_figure(path, c, engine, sha, ocr_prov)
        except Exception as exc:  # noqa: BLE001 — recorded per figure, never fatal
            res = fail(c, "ERROR", _clean_error(exc, task.get("root")))
        out["results"].append(res)
        out["seconds"].append(round(time.perf_counter() - t0, 3))
    out["ocr_calls"] = engine.calls
    return out


# ================================================================================================ tables
def _arrow_schemas():
    import pyarrow as pa

    s, i32, f64, b = pa.string(), pa.int32(), pa.float64(), pa.bool_()
    ls = pa.list_(pa.string())
    figures = pa.schema([
        ("figure_id", s), ("source_id", s), ("page_id", s), ("page_index", i32), ("work_id", s),
        ("figure_label", s), ("caption_block_id", s), ("layout_class", s),
        ("bbox_x0", f64), ("bbox_y0", f64), ("bbox_x1", f64), ("bbox_y1", f64), ("page_rotation", pa.int16()),
        ("numeric_tokens_near", i32), ("vector_line_curve_items", i32), ("core_keyword", b),
        ("keywords_caption", ls), ("keywords_page", ls), ("model_hint_in_caption", b), ("has_embedded_raster", b),
        ("figure_status", s), ("axis_status", s), ("plot_box", pa.list_(f64)),
        ("x_axis_kind", s), ("x_cal_method", s), ("x_cal_label_source", s), ("x_cal_n_labels", i32),
        ("x_cal_rms_pt", f64), ("x_title_raw", s), ("x_quantity_raw", s), ("x_unit_raw", s), ("x_is_time", b),
        ("y_axis_kind", s), ("y_cal_method", s), ("y_cal_label_source", s), ("y_cal_n_labels", i32),
        ("y_cal_rms_pt", f64), ("y_title_raw", s), ("y_quantity_raw", s), ("y_unit_raw", s),
        ("n_series", i32), ("n_series_both_axes", i32), ("n_points", i32), ("n_points_both_axes", i32),
        ("n_points_outside_plot", i32), ("n_time_series", i32), ("n_marker_series", i32), ("n_labelled_series", i32),
        ("legend_without_curve", ls), ("notes", ls), ("flags", ls), ("error", s),
        ("available_from", s), ("available_basis", s), ("publication_year", i32), ("source_site_scope_raw", s),
        ("route", s), ("digitizer_version", s), ("config_hash", s), ("rule_version", s), ("status", s),
        ("review_status", s), ("schema", s),
    ])
    series = pa.schema([
        ("series_id", s), ("figure_id", s), ("source_id", s), ("page_id", s), ("work_id", s), ("figure_label", s),
        ("caption_block_id", s), ("series_index", i32), ("series_label_raw", s), ("series_color", s),
        ("series_nature", s), ("sampling", s), ("axes_calibrated", s),
        ("x_quantity_raw", s), ("x_unit_raw", s), ("x_title_raw", s), ("x_axis_kind", s), ("x_is_time", b),
        ("x_cal_method", s), ("x_cal_rms_pt", f64),
        ("y_quantity_raw", s), ("y_unit_raw", s), ("y_title_raw", s), ("y_axis_kind", s),
        ("y_cal_method", s), ("y_cal_rms_pt", f64),
        ("n_points", i32), ("n_points_outside_plot", i32), ("x_min", f64), ("x_max", f64), ("y_min", f64),
        ("y_max", f64), ("x_date_min", s), ("x_date_max", s), ("x_err_median", f64), ("y_err_median", f64),
        ("error_model", s), ("calibration", s),
        ("route", s), ("status", s), ("review_status", s), ("available_from", s), ("available_basis", s),
        ("publication_year", i32), ("source_site_scope_raw", s), ("flags", ls), ("provenance", s),
        ("rule_version", s), ("schema", s),
    ])
    points = pa.schema([
        ("series_id", s), ("i", i32), ("figure_id", s), ("source_id", s), ("page_id", s), ("series_label_raw", s),
        ("x", f64), ("x_date", s), ("y", f64), ("x_err", f64), ("y_err", f64), ("x_unit_raw", s), ("y_unit_raw", s),
        ("x_cal_method", s), ("x_cal_rms_pt", f64), ("y_cal_method", s), ("y_cal_rms_pt", f64),
        ("x_page_pt", f64), ("y_page_pt", f64), ("in_plot_area", b), ("status", s), ("review_status", s),
        ("available_from", s), ("available_basis", s),
    ])
    return figures, series, points


def to_tables(results: list[dict[str, Any]], names: tuple[str, str, str] = DATASETS) -> dict[str, Any]:
    """Figure, series and point rows of all results → the three Arrow tables (sorted, fixed schemas) named
    ``names`` (the route A datasets, or ``RASTER_DATASETS``)."""
    import pyarrow as pa

    fs, ss, ps = _arrow_schemas()
    figs = sorted((r["figure"] for r in results), key=lambda r: r["figure_id"])
    series = sorted((s for r in results for s in r["series"]), key=lambda r: r["series_id"])
    points = sorted((p for r in results for p in r["points"]), key=lambda p: (p["series_id"], p["i"]))
    ids = [s["series_id"] for s in series]
    if len(ids) != len(set(ids)):
        raise ValueError("figure_series: duplicate series_id")
    return {
        names[0]: pa.Table.from_pylist([{k: r.get(k) for k in fs.names} for r in figs], schema=fs),
        names[1]: pa.Table.from_pylist([{k: r.get(k) for k in ss.names} for r in series], schema=ss),
        names[2]: pa.Table.from_pylist([{k: r.get(k) for k in ps.names} for r in points], schema=ps),
    }


# ================================================================================================ builder
def _resources_root(resources: str | None) -> Path | None:
    raw = resources or os.environ.get("VKM_RESOURCES_ROOT")
    if not raw:
        return None
    p = Path(str(raw)).expanduser()
    return p if p.is_dir() else None


def _parse_sources(value: Any) -> set[str] | None:
    if value in (None, "", []):
        return None
    if isinstance(value, str):
        return {v.strip() for v in value.split(",") if v.strip()}
    return {str(v) for v in value}


def _run_tasks(tasks: list[dict[str, Any]], workers: int, fn=None) -> list[dict[str, Any]]:
    fn = fn or source_worker
    if workers <= 1 or len(tasks) <= 1:
        return [fn(t) for t in tasks]
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=min(workers, len(tasks)), mp_context=ctx) as ex:
        return list(ex.map(fn, tasks))


def _tasks(cands: list[dict[str, Any]], root: Path, **extra: Any) -> list[dict[str, Any]]:
    by_src: dict[str, list[dict[str, Any]]] = {}
    for c in cands:
        by_src.setdefault(c["source_id"], []).append(c)
    tasks = []
    for sid, cs in by_src.items():
        rel = cs[0].get("source_path_logical")
        tasks.append({"source_id": sid, "path": str(root / rel) if rel else None,
                      "expected_sha256": cs[0].get("source_sha256"), "figures": cs, "root": str(root), **extra})
    return sorted(tasks, key=lambda t: (-len(t["figures"]), t["source_id"]))      # the biggest sources first


def _summary(tables: dict[str, Any], names: tuple[str, str, str], outs: list[dict[str, Any]]) -> dict[str, Any]:
    figs = tables[names[0]].to_pylist()
    ser = tables[names[1]]
    both = [r for r in ser.select(["axes_calibrated", "x_is_time", "n_points", "source_id", "figure_id"]).to_pylist()
            if r["axes_calibrated"] == "BOTH"]
    secs = sorted(s for o in outs for s in o["seconds"])
    return {
        "candidate_rows": len(figs), "sources_with_candidates": len(outs),
        "source_status": dict(sorted(Counter(o["status"] for o in outs).items())),
        "figure_status": dict(sorted(Counter(f["figure_status"] for f in figs).items())),
        "rotated_page_figures": sum(1 for f in figs if f["page_rotation"]),
        "core_keyword_figures": sum(1 for f in figs if f["core_keyword"]),
        "series": ser.num_rows, "points": tables[names[2]].num_rows,
        "series_both_axes": len(both), "points_both_axes": sum(r["n_points"] for r in both),
        "points_outside_plot": sum(f["n_points_outside_plot"] or 0 for f in figs),
        "figures_with_series_both_axes": len({r["figure_id"] for r in both}),
        "sources_with_series_both_axes": len({r["source_id"] for r in both}),
        "time_series_both_axes": sum(1 for r in both if r["x_is_time"]),
        "flags_figures": dict(sorted(Counter(fl for f in figs for fl in f["flags"]).items())),
        "ocr_calls": sum(o["ocr_calls"] for o in outs),
        "seconds_per_figure": {"median": statistics.median(secs) if secs else None,
                               "max": secs[-1] if secs else None, "n": len(secs)},
    }


def _raster_pages(value: Any) -> dict[str, list[int]]:
    """``raster_pages``: a JSON file (or an inline mapping) {"VKM-SRC-…": [page, …]}."""
    data = value
    if isinstance(value, str):
        data = json.loads(Path(value).expanduser().read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("figure_series.raster_pages: a JSON object {source_id: [page, …]}")
    return {str(k): sorted({int(p) for p in v}) for k, v in sorted(data.items())}


def build(con: Any, *, resources: str | None = None, workers: int | None = None, ocr: str = "auto",
          sources: Any = None, raster_pages: Any = None, stats: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The part: canon + source PDFs → ``figure_series_figures``, ``figure_series``, ``figure_series_points`` (route
    A) and, with ``raster_pages``, the flagged second dataset ``figure_series_raster_*`` (route R).

    Options (``--option figure_series.KEY=VALUE``): ``resources`` (PRIVATE clone; default ``$VKM_RESOURCES_ROOT``),
    ``workers`` (processes, one source each; default min(8, CPUs)), ``ocr`` (``auto``: Tesseract when installed;
    ``off``), ``sources`` (restrict route A to these source ids — a partial build, recorded and refused by the
    import), ``raster_pages`` (JSON file {source_id: [page, …]}: route R on the raster figures of these pages)."""
    from vkm_corpus.figures import DIGITIZER_VERSION
    from vkm_corpus.figures.dataset import config_hash

    st = stats if stats is not None else {}
    root = _resources_root(resources)
    if root is None:
        st["reason"] = "no resources root (option figure_series.resources or $VKM_RESOURCES_ROOT): the source PDFs " \
                       "are on the WORKSTATION; CORE imports the datasets (python -m vkm_corpus.navigation.figure_series " \
                       "import)"
        return None
    only = _parse_sources(sources)
    t_all = time.perf_counter()
    cands = load_candidates(con, st, only)
    use_ocr = str(ocr).lower() != "off"
    tasks = _tasks([c for c in cands if c["numeric_tokens_near"] >= MIN_NUMERIC_TOKENS], root, ocr=use_ocr)
    n_workers = int(workers) if workers else min(8, os.cpu_count() or 1)
    outs = _run_tasks(tasks, n_workers)
    tables = to_tables([r for o in outs for r in o["results"]])
    figs = tables[DATASETS[0]].to_pylist()
    ocr_prov = ocr_provenance() if use_ocr else None
    summary = _summary(tables, DATASETS, outs)
    st.update({
        "rule_version": RULE_VERSION, "schema": SCHEMA, "route": ROUTE, "digitizer_version": DIGITIZER_VERSION,
        "config": CONFIG, "config_hash": config_hash(CONFIG), "resources": {"dir": root.name},
        "partial": sorted(only) if only else None, "workers": n_workers,
        "chartlike": sum(1 for f in figs if f["vector_line_curve_items"] is not None),
        "candidates_rule_not_evaluated": sum(1 for f in figs if f["vector_line_curve_items"] is None),
        "not_chartlike_after_numeric_rule": sum(o["not_chartlike"] for o in outs),
        **summary,
        "ocr": ({**ocr_prov, "calls": summary["ocr_calls"]} if ocr_prov else
                {"engine": None, "reason": "off" if not use_ocr else "tesseract/OpenCV not available"}),
    })
    if raster_pages not in (None, "", {}):
        pages = _raster_pages(raster_pages)
        rst: dict[str, Any] = {"pages": pages}
        rtasks = _tasks(load_raster_candidates(con, pages, rst), root)
        routs = _run_tasks(rtasks, n_workers, raster_worker)
        rtables = to_tables([r for o in routs for r in o["results"]], RASTER_DATASETS)
        rst.update({"route": RASTER_ROUTE, "config": RASTER_CONFIG, "config_hash": config_hash(RASTER_CONFIG),
                    **_summary(rtables, RASTER_DATASETS, routs)})
        st["raster"] = rst
        tables.update(rtables)
    st["seconds_total"] = round(time.perf_counter() - t_all, 1)
    return tables


# ================================================================================================ import on CORE
class ImportRefused(RuntimeError):
    """The bundle is not an importable build of this part for this NAV directory."""


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def import_bundle(bundle_dir: str | Path, nav_dir: str | Path, *, canon_duckdb: str | Path | None = None,
                  dry_run: bool = False, allow_partial: bool = False) -> dict[str, Any]:
    """Copy a WORKSTATION build of the part (``<bundle>/<dataset>.parquet`` + ``manifest.json`` of ``nav build``) into
    a NAV directory of the same snapshot, byte for byte, and merge its manifest entries (datasets and
    ``parts.figure_series`` with an ``imported`` note). Checks: manifest format, part BUILT with this rule version,
    not partial, dataset sha256 and row counts, the same snapshot as the NAV directory, and — with ``canon_duckdb`` —
    the same snapshot as the canon and every figure id present in ``canonical.figures``."""
    b, n = Path(bundle_dir), Path(nav_dir)
    bm_path = b / "manifest.json"
    if not bm_path.is_file():
        raise ImportRefused("the bundle has no manifest.json")
    bm_raw = bm_path.read_bytes()
    bm = json.loads(bm_raw.decode("utf-8"))
    if bm.get("format") != MANIFEST_FORMAT:
        raise ImportRefused(f"the bundle manifest is not {MANIFEST_FORMAT}")
    snap = (bm.get("snapshot") or {}).get("snapshot_id")
    part = (bm.get("parts") or {}).get(PART) or {}
    if part.get("status") != "BUILT" or part.get("rule_version") != RULE_VERSION:
        raise ImportRefused(f"the bundle has no BUILT part {PART} of rule {RULE_VERSION}")
    if (part.get("stats") or {}).get("partial") and not allow_partial:
        raise ImportRefused("the bundle is a partial build (option sources)")
    listed = bm.get("datasets") or {}
    raster = [ds for ds in RASTER_DATASETS if ds in listed]
    if raster and len(raster) != len(RASTER_DATASETS):
        raise ImportRefused("the bundle has an incomplete raster dataset (route R)")
    entries = {}
    for ds in (*DATASETS, *raster):                  # route R is optional; all three datasets or none
        e = listed.get(ds)
        f = b / f"{ds}.parquet"
        if not e or e.get("part") != PART or not f.is_file():
            raise ImportRefused(f"dataset {ds} is missing in the bundle")
        if _sha256_file(f) != e.get("sha256"):
            raise ImportRefused(f"dataset {ds}: sha256 differs from the bundle manifest")
        entries[ds] = e
    nm_path = n / "manifest.json"
    if not nm_path.is_file():
        raise ImportRefused("the NAV directory has no manifest.json")
    nm = json.loads(nm_path.read_text(encoding="utf-8"))
    nsnap = (nm.get("snapshot") or {}).get("snapshot_id")
    if nm.get("format") != MANIFEST_FORMAT or nsnap != snap:
        raise ImportRefused(f"snapshot mismatch: bundle {snap}, NAV directory {nsnap}")
    import pyarrow.parquet as pq

    for ds, e in entries.items():
        rows = pq.ParquetFile(b / f"{ds}.parquet").metadata.num_rows
        if e.get("rows") is not None and int(e["rows"]) != rows:
            raise ImportRefused(f"dataset {ds}: {rows} rows, the manifest says {e['rows']}")
    canon_check = None
    if canon_duckdb:
        import duckdb

        con = duckdb.connect(str(canon_duckdb), read_only=True)
        try:
            csnap = _snapshot_id(con)
            if csnap != snap:
                raise ImportRefused(f"snapshot mismatch: bundle {snap}, canon {csnap}")
            missing = 0
            for ds in [d for d in entries if d.endswith("_figures")]:
                fig_path = (b / f"{ds}.parquet").as_posix().replace("'", "''")
                missing += con.execute(f"""
                    SELECT count(*) FROM read_parquet('{fig_path}') x
                    WHERE x.figure_id NOT IN (SELECT object_id FROM canonical.figures)""").fetchone()[0]
        finally:
            con.close()
        if missing:
            raise ImportRefused(f"{missing} figure ids of the bundle are not in canonical.figures")
        canon_check = {"snapshot_id": csnap, "missing_figure_ids": 0}
    plan = {"part": PART, "snapshot_id": snap, "rule_version": RULE_VERSION,
            "datasets": {ds: {"rows": e.get("rows"), "sha256": e.get("sha256")} for ds, e in entries.items()},
            "bundle_manifest_sha256": hashlib.sha256(bm_raw).hexdigest(), "canon_check": canon_check,
            "dry_run": dry_run}
    if dry_run:
        return plan
    for ds in entries:
        src, dst = b / f"{ds}.parquet", n / f"{ds}.parquet"
        tmp = dst.with_suffix(".parquet.tmp")
        shutil.copyfile(src, tmp)
        if _sha256_file(tmp) != entries[ds]["sha256"]:
            tmp.unlink(missing_ok=True)
            raise ImportRefused(f"dataset {ds}: the copy does not match its sha256")
        os.replace(tmp, dst)
    nm.setdefault("datasets", {})
    nm.setdefault("parts", {})
    for ds, e in entries.items():
        nm["datasets"][ds] = {**e, "path": f"{ds}.parquet"}
    nm["parts"][PART] = {**part, "imported": {
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "bundle_built_at": bm.get("built_at"),
        "bundle_manifest_sha256": plan["bundle_manifest_sha256"]}}
    tmp = nm_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(nm, ensure_ascii=False, indent=1, sort_keys=True, default=str), encoding="utf-8")
    os.replace(tmp, nm_path)
    return {**plan, "imported": True}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m vkm_corpus.navigation.figure_series",
                                description="NAV part figure_series: import a WORKSTATION build into a NAV directory")
    sub = p.add_subparsers(dest="cmd", required=True)
    im = sub.add_parser("import", help="copy the datasets of a build (bundle) into a NAV directory of the same "
                                       "snapshot and merge the manifest (byte for byte, sha256 checked)")
    im.add_argument("--bundle", required=True, help="output directory of `nav build --part figure_series`")
    im.add_argument("--nav-dir", required=True, help="NAV directory of the snapshot (e.g. a copy of the published one)")
    im.add_argument("--canon-duckdb", default=None, help="also check the snapshot and the figure ids in this canon")
    im.add_argument("--dry-run", action="store_true", help="checks only, nothing is written")
    im.add_argument("--allow-partial", action="store_true", help="accept a build restricted to some sources")
    im.add_argument("--pack", action="store_true", help="then pack the NAV directory into nav.duckdb")
    a = p.parse_args(argv)
    try:
        out = import_bundle(a.bundle, a.nav_dir, canon_duckdb=a.canon_duckdb, dry_run=a.dry_run,
                            allow_partial=a.allow_partial)
    except ImportRefused as exc:
        print(json.dumps({"status": "REFUSED", "reason": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    if a.pack and not a.dry_run:
        from vkm_corpus.navigation.store import pack

        out["pack"] = pack(a.nav_dir)
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Read side of the NAV part ``figure_series`` (digitized chart series, agent FD2): pure query functions for the API
and the MCP tools ``find_figure_series`` / ``get_figure_series``.

``con`` exposes ``nav_figure_series_figures``, ``nav_figure_series`` and ``nav_figure_series_points`` (route A;
``NavStore`` serves them from ``nav.duckdb``; :func:`attach` makes views over a build directory), optionally the
flagged raster dataset ``nav_figure_series_raster_*`` (route R), and — for captions — ``canonical.figures`` when the
canon is attached. A build without the part raises ``NavUnavailable``.

Words are matched by the lemmas of the concept graph's morphology (``store._lemmas``: pymorphy3, else stems) plus
the numbers of the text, in the caption, the figure label, the axis titles and the series labels; all query words
must be found. Everything returned is navigation: ``DERIVATION`` values digitized from a publication with
``AUTO_EXTRACTED_UNREVIEWED`` review status and a half-width error per value — never evidence, never an observation
of the project; a plotted model result is not told apart from an observation; the site attribution is the source's.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from vkm_corpus.navigation.figure_series import (DATASETS, NOTE, RASTER_DATASETS, REVIEW_STATUS, STATUS,
                                                 SUSPECT_FLAGS)

TABLES = {"A": tuple(f"nav_{d}" for d in DATASETS), "R": tuple(f"nav_{d}" for d in RASTER_DATASETS)}
FIGURE_FIELDS = ("figure_id", "source_id", "page_id", "page_index", "work_id", "figure_label", "caption_block_id",
                 "layout_class", "page_rotation", "figure_status", "axis_status", "x_axis_kind", "x_cal_method",
                 "x_cal_label_source", "x_cal_n_labels", "x_cal_rms_pt", "x_title_raw", "x_quantity_raw", "x_unit_raw",
                 "x_is_time", "y_axis_kind", "y_cal_method", "y_cal_label_source", "y_cal_n_labels", "y_cal_rms_pt",
                 "y_title_raw", "y_quantity_raw", "y_unit_raw", "n_series", "n_series_both_axes", "n_points",
                 "n_points_both_axes", "n_points_outside_plot", "n_time_series", "flags", "error", "available_from",
                 "available_basis", "publication_year", "source_site_scope_raw", "core_keyword", "keywords_caption",
                 "model_hint_in_caption", "digitizer_version", "rule_version", "route")
SERIES_FIELDS = ("series_id", "figure_id", "source_id", "series_index", "series_label_raw", "series_color", "sampling",
                 "axes_calibrated", "x_unit_raw", "y_unit_raw", "x_is_time", "n_points", "n_points_outside_plot",
                 "x_min", "x_max", "y_min", "y_max", "x_date_min", "x_date_max", "x_err_median", "y_err_median",
                 "flags")
POINT_FIELDS = ("i", "x", "x_date", "y", "x_err", "y_err", "x_page_pt", "y_page_pt", "in_plot_area")
HOW_TO = "get_figure_series(ref = series_id or figure_id) returns the points, the calibration and the provenance"
_UNITS = {"mm": "мм", "cm": "см", "m": "м", "km": "км", "mpa": "мпа", "kpa": "кпа", "pa": "па", "gpa": "гпа",
          "d": "сут", "day": "сут", "days": "сут", "сутки": "сут", "сутках": "сут", "суток": "сут", "дни": "сут",
          "дней": "сут", "year": "год", "years": "год", "yr": "год", "г": "год", "годы": "год", "годах": "год",
          "лет": "год", "mm/year": "мм/год", "mm/yr": "мм/год", "mm/a": "мм/год", "мм/г": "мм/год",
          "mm/day": "мм/сут", "mm/d": "мм/сут", "мм/сутки": "мм/сут", "percent": "%", "проц": "%"}
_NUM = re.compile(r"\d+(?:[.,]\d+)?")
_CACHE: dict[tuple, dict[str, Any]] = {}


# ------------------------------------------------------------------------------------------------ plumbing
def attach(con: Any, nav_dir: str | Path) -> None:
    """Temporary views ``nav_<dataset>`` over ``<nav_dir>/<dataset>.parquet`` (the raster ones when present)."""
    d = Path(nav_dir)
    for name in (*DATASETS, *RASTER_DATASETS):
        f = d / f"{name}.parquet"
        if name in DATASETS or f.is_file():
            path = f.as_posix().replace("'", "''")
            con.execute(f"CREATE OR REPLACE TEMP VIEW nav_{name} AS SELECT * FROM read_parquet('{path}')")


def _dicts(con: Any, sql: str, params: list[Any] | tuple = ()) -> list[dict[str, Any]]:
    cur = con.execute(sql, list(params))
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _has(con: Any, name: str) -> bool:
    try:
        con.execute(f"SELECT 1 FROM {name} LIMIT 0")
        return True
    except Exception:  # noqa: BLE001 — not part of this build / no canon attached
        return False


def _require(con: Any) -> None:
    missing = [t for t in TABLES["A"] if not _has(con, t)]
    if missing:
        from vkm_corpus.navigation.store import NavUnavailable

        raise NavUnavailable(f"the NAV part figure_series is not in this build (missing {', '.join(missing)})")


def has_raster(con: Any) -> bool:
    return all(_has(con, t) for t in TABLES["R"])


def _lemmas(text: str) -> list[str]:
    from vkm_corpus.navigation.store import _lemmas as lemmas

    return lemmas(text)


def terms(text: str | None) -> set[str]:
    """Lemmas of the words (≥ 3 letters, stop words dropped) and the numbers of a text (decimal comma → point)."""
    t = text or ""
    return set(_lemmas(t)) | {n.replace(",", ".") for n in _NUM.findall(t)}


def norm_unit(unit: str | None) -> str | None:
    """A printed unit for comparison: lower case, ё → е, no spaces or dots, a few aliases (mm ↔ мм, сутки → сут)."""
    if not unit:
        return None
    u = re.sub(r"[\s.]+", "", unit.strip().lower().replace("ё", "е"))
    u = u.strip("()[]")
    return _UNITS.get(u, u) or None


def _found(q: str, pool: set[str]) -> bool:
    if q in pool:
        return True
    return len(q) >= 5 and not q[0].isdigit() and any(w.startswith(q) or (len(w) >= 5 and q.startswith(w))
                                                     for w in pool)


# ------------------------------------------------------------------------------------------------ search index
def _fingerprint(con: Any, tables: tuple[str, str, str]) -> tuple:
    fs = con.execute(f"SELECT count(*), min(series_id), max(series_id), sum(n_points) FROM {tables[1]}").fetchone()
    ff = con.execute(f"SELECT count(*), min(figure_id), max(figure_id), sum(n_series) FROM {tables[0]}").fetchone()
    return tuple(fs) + tuple(ff)


def _route_entries(con: Any, route: str, has_canon: bool) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    tf, ts, _tp = TABLES[route]
    figures = _dicts(con, f"SELECT {', '.join(FIGURE_FIELDS)} FROM {tf} ORDER BY figure_id")
    captions: dict[str, str] = {}
    if has_canon:
        captions = {fid: cap for fid, cap in con.execute(f"""
            SELECT f.object_id, coalesce(f.caption_normalized, f.caption)
            FROM canonical.figures f JOIN {tf} x ON x.figure_id = f.object_id""").fetchall() if cap}
    by_fig: dict[str, list[dict[str, Any]]] = {}
    for s in _dicts(con, f"SELECT {', '.join(SERIES_FIELDS)} FROM {ts} ORDER BY figure_id, series_index"):
        s["_lab"] = terms(s.get("series_label_raw"))
        s["_xu"], s["_yu"] = norm_unit(s.get("x_unit_raw")), norm_unit(s.get("y_unit_raw"))
        by_fig.setdefault(s["figure_id"], []).append(s)
    out = []
    for f in figures:
        f["caption"] = captions.get(f["figure_id"])
        f["_cap"] = terms(f["caption"])
        f["_lab"] = terms(f.get("figure_label"))
        f["_tit"] = terms(f.get("x_title_raw")) | terms(f.get("y_title_raw"))
        out.append((f, by_fig.get(f["figure_id"], [])))
    return out


def _index(con: Any, include_raster: bool = False) -> dict[str, Any]:
    """Figures and series of the build with their word sets — computed once per served build (cache by counts and
    id ranges of each route, captions only when the canon is attached)."""
    has_canon = _has(con, "canonical.figures")
    routes = ["A"] + (["R"] if include_raster and has_raster(con) else [])
    key = (has_canon, *[(r, _fingerprint(con, TABLES[r])) for r in routes])
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    entries = [e for r in routes for e in _route_entries(con, r, has_canon)]
    idx = {"entries": entries, "has_canon": has_canon, "routes": routes}
    if len(_CACHE) > 4:
        _CACHE.clear()
    _CACHE[key] = idx
    return idx


def _public(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {k: row.get(k) for k in fields if k in row}


def _axes(f: dict[str, Any]) -> dict[str, Any]:
    return {"x": {"title_raw": f.get("x_title_raw"), "quantity_raw": f.get("x_quantity_raw"),
                  "unit_raw": f.get("x_unit_raw"), "axis_kind": f.get("x_axis_kind"), "is_time": f.get("x_is_time"),
                  "cal_method": f.get("x_cal_method"), "cal_label_source": f.get("x_cal_label_source"),
                  "cal_n_labels": f.get("x_cal_n_labels"), "cal_rms_pt": f.get("x_cal_rms_pt")},
            "y": {"title_raw": f.get("y_title_raw"), "quantity_raw": f.get("y_quantity_raw"),
                  "unit_raw": f.get("y_unit_raw"), "axis_kind": f.get("y_axis_kind"),
                  "cal_method": f.get("y_cal_method"), "cal_label_source": f.get("y_cal_label_source"),
                  "cal_n_labels": f.get("y_cal_n_labels"), "cal_rms_pt": f.get("y_cal_rms_pt")}}


def _figure_view(f: dict[str, Any], caption_chars: int) -> dict[str, Any]:
    cap = f.get("caption")
    out = {k: f.get(k) for k in ("figure_id", "source_id", "page_id", "page_index", "work_id", "figure_label",
                                 "route", "figure_status", "axis_status", "n_series", "n_series_both_axes",
                                 "n_points_both_axes", "n_points_outside_plot", "n_time_series", "available_from",
                                 "available_basis", "publication_year", "source_site_scope_raw", "flags")}
    out["caption"] = cap[:caption_chars] if cap else None
    out["caption_truncated"] = bool(cap and len(cap) > caption_chars)
    out.update(_axes(f))
    return out


# ------------------------------------------------------------------------------------------------ find
def find_figure_series(con: Any, text: str | None = None, unit: str | None = None, source_id: str | None = None,
                       time_series: bool | None = None, calibrated_only: bool = True, limit: int = 10,
                       max_series: int = 12, include_raster: bool = False, clean_only: bool = False) -> dict[str, Any]:
    """Digitized figures and their series by words of the caption, figure label, axis titles or series labels
    (``text``), a unit printed on either axis (``unit``), a source, time series only (``time_series``) and — by
    default — both axes calibrated. Figures come best match first (caption > axis titles > series labels > figure
    label, then the number of calibrated points); without words in source and page order. Each figure lists at most
    ``max_series`` matching series (ranges, median errors, flags); the points are in :func:`get_figure_series`.
    ``include_raster`` adds the flagged raster dataset (route R) when the build has it; ``clean_only`` drops series
    with a suspect calibration (``SUSPECT_FLAGS``: axis labels inside the plot or far outside it, a second axis,
    power-of-ten labels, thousands read as small numbers, an axis on three chance labels)."""
    _require(con)
    idx = _index(con, include_raster)
    q_terms = terms(text) if text else set()
    q_unit = norm_unit(unit) if unit else None
    query = {"text": text, "unit": unit, "unit_normalized": q_unit, "source_id": source_id,
             "time_series": time_series, "calibrated_only": calibrated_only, "include_raster": include_raster,
             "clean_only": clean_only, "terms": sorted(q_terms)}
    hits = []
    for f, series in idx["entries"]:
        if source_id and f["source_id"] != source_id:
            continue
        matched, where_fig = [], set()
        score_fig = 0.0
        for s in series:
            if calibrated_only and s["axes_calibrated"] != "BOTH":
                continue
            if clean_only and SUSPECT_FLAGS & set(s["flags"] or ()):
                continue
            if time_series and not s["x_is_time"]:
                continue
            if q_unit and q_unit not in (s["_xu"], s["_yu"]):
                continue
            score, where = 0.0, set()
            ok = True
            for q in q_terms:
                best, where_b = 0.0, None
                for pool, w, name in ((f["_cap"], 1.0, "caption"), (f["_tit"], 0.8, "axis_title"),
                                      (s["_lab"], 0.6, "series_label"), (f["_lab"], 0.5, "figure_label")):
                    if w > best and _found(q, pool):
                        best, where_b = w, name
                if best == 0.0:
                    ok = False
                    break
                score += best
                where.add(where_b)
            if not ok:
                continue
            matched.append((score, s))
            score_fig = max(score_fig, score)
            where_fig |= where
        if not matched:
            # a figure without (matching) series: listed only when uncalibrated figures are asked for and nothing
            # series-specific (unit, time, clean) is filtered
            if calibrated_only or q_unit or time_series or clean_only:
                continue
            if q_terms and not all(_found(q, f["_cap"] | f["_tit"] | f["_lab"]) for q in q_terms):
                continue
            where_fig = {"caption"} if q_terms else set()
        hits.append((score_fig, f, matched, where_fig))
    if q_terms:
        hits.sort(key=lambda h: (-h[0], -(h[1]["n_points_both_axes"] or 0), h[1]["figure_id"], h[1]["route"] or ""))
    else:
        hits.sort(key=lambda h: (h[1]["source_id"], h[1]["page_index"] or 0, h[1]["figure_id"], h[1]["route"] or ""))
    lim, per = max(1, int(limit)), max(1, int(max_series))
    out_figs = []
    for score, f, matched, where in hits[:lim]:
        matched.sort(key=lambda m: (-m[0], m[1]["series_index"]))
        item = _figure_view(f, 240)
        item.update({"score": round(score, 3), "matched_in": sorted(where), "n_series_matched": len(matched),
                     "series": [{**_public(s, SERIES_FIELDS), "suspect": bool(SUSPECT_FLAGS & set(s["flags"] or ()))}
                                for _, s in matched[:per]],
                     "series_truncated": len(matched) > per})
        for s in item["series"]:
            s.pop("figure_id", None)
            s.pop("source_id", None)
        out_figs.append(item)
    return {"query": query, "total_figures": len(hits), "total_series": sum(len(h[2]) for h in hits),
            "figures": out_figs, "captions_searched": idx["has_canon"], "routes": idx["routes"],
            "raster_available": has_raster(con), "status": STATUS, "review_status": REVIEW_STATUS, "how_to": HOW_TO,
            "status_note": NOTE}


# ------------------------------------------------------------------------------------------------ get
def _json(v: str | None) -> Any:
    if not v:
        return None
    try:
        return json.loads(v)
    except ValueError:
        return v


def _get_in(con: Any, ref: str, route: str, max_points: int) -> dict[str, Any] | None:
    tf, ts, tp = TABLES[route]
    if ref.startswith("FS-"):
        srows = _dicts(con, f"SELECT * FROM {ts} WHERE series_id = ?", [ref])
        if not srows:
            return None
        fid = srows[0]["figure_id"]
    else:
        fid = ref
        srows = _dicts(con, f"SELECT * FROM {ts} WHERE figure_id = ? ORDER BY series_index", [fid])
    frows = _dicts(con, f"SELECT * FROM {tf} WHERE figure_id = ?", [fid])
    if not frows and not srows:
        return None
    f = frows[0] if frows else {"figure_id": fid}
    if _has(con, "canonical.figures"):
        cap = con.execute("SELECT coalesce(caption_normalized, caption) FROM canonical.figures WHERE object_id = ?",
                          [fid]).fetchone()
        f["caption"] = cap[0] if cap else None
    figure = _figure_view(f, 600)
    figure.update({k: f.get(k) for k in ("layout_class", "page_rotation", "bbox_x0", "bbox_y0", "bbox_x1", "bbox_y1",
                                         "plot_box", "frame_box", "legend_without_curve", "notes", "error",
                                         "keywords_caption",
                                         "keywords_page", "core_keyword", "model_hint_in_caption",
                                         "digitizer_version", "config_hash", "rule_version")})
    ids = [s["series_id"] for s in srows]
    points: dict[str, list[dict[str, Any]]] = {}
    if ids:
        ph = ", ".join("?" for _ in ids)
        for p in _dicts(con, f"SELECT series_id, {', '.join(POINT_FIELDS)} FROM {tp} "
                             f"WHERE series_id IN ({ph}) ORDER BY series_id, i", ids):
            points.setdefault(p.pop("series_id"), []).append(p)
    total = sum(len(v) for v in points.values())
    left = max(1, int(max_points))
    out_series = []
    for s in srows:
        pts = points.get(s["series_id"], [])
        take = pts[:max(0, left)]
        left -= len(take)
        item = {k: s.get(k) for k in (
            "series_id", "series_index", "series_label_raw", "series_color", "series_nature", "sampling",
            "axes_calibrated", "n_points", "n_points_outside_plot", "x_min", "x_max", "y_min", "y_max",
            "x_date_min", "x_date_max", "x_err_median", "y_err_median", "error_model", "flags", "status",
            "review_status", "available_from", "available_basis", "publication_year", "source_site_scope_raw",
            "route", "rule_version")}
        item["x"] = {"title_raw": s.get("x_title_raw"), "quantity_raw": s.get("x_quantity_raw"),
                     "unit_raw": s.get("x_unit_raw"), "axis_kind": s.get("x_axis_kind"), "is_time": s.get("x_is_time"),
                     "cal_method": s.get("x_cal_method"), "cal_rms_pt": s.get("x_cal_rms_pt")}
        item["y"] = {"title_raw": s.get("y_title_raw"), "quantity_raw": s.get("y_quantity_raw"),
                     "unit_raw": s.get("y_unit_raw"), "axis_kind": s.get("y_axis_kind"),
                     "cal_method": s.get("y_cal_method"), "cal_rms_pt": s.get("y_cal_rms_pt")}
        item["suspect"] = bool(SUSPECT_FLAGS & set(s.get("flags") or ()))
        item["calibration"] = _json(s.get("calibration"))
        item["provenance"] = _json(s.get("provenance"))
        item["points"] = take
        item["points_returned"] = len(take)
        item["points_truncated"] = len(take) < len(pts)
        out_series.append(item)
    return {"ref": ref, "figure": figure, "series": out_series, "points_total": total,
            "points_returned": sum(s["points_returned"] for s in out_series),
            "points_truncated": any(s["points_truncated"] for s in out_series), "status": STATUS,
            "review_status": REVIEW_STATUS, "status_note": NOTE}


def get_figure_series(con: Any, ref: str, max_points: int = 1000) -> dict[str, Any] | None:
    """One series (``FS-…``) or every series of a figure (a figure id), with the figure's status and axes, the
    calibration of each axis (fitted labels, residuals, dropped labels, OCR engine when used), the provenance, and
    the points (x, y in data units with half-width errors, the page position in pt, ``in_plot_area``) — at most
    ``max_points`` in all (``points_truncated``). Route A first, then the raster dataset when the build has it. A
    candidate figure without series comes with its status and an empty list; an id that is not in the part → None."""
    _require(con)
    for route in ("A", "R"):
        if route == "R" and not has_raster(con):
            break
        found = _get_in(con, ref, route, max_points)
        if found is not None:
            return found
    return None

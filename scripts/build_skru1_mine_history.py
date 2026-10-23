"""Build the SKRU-1 «mining in time» layer from the corpus evidence and the existing catalogues.

Inputs (all read-only):
* PRIVATE ``$VKM_RESOURCES_ROOT/11_evidence_vnext/canonical``:
  - ``SITE_SKRU1_MINE/skru1_mine_evidence.csv`` — new evidence of this build (quotes, PRIVATE);
  - ``WORLD_PARAMETERS/curated/skru1_mining_calendar_20261010.csv`` (+ conflicts), ``skru1_c4_chamber_design.csv``;
  - ``WORLD_PARAMETERS/curated/c4_map/skru1/*.geojson`` — the mine-field plan in SKRU1_LOCAL_SH1 (CRS UNKNOWN);
  - ``MINING/mining_chronology_catalog.csv``, ``mining_geometry_catalog.csv``, ``backfill_event_catalog.csv`` (Phase 1);
  - ``WORLD_OBSERVATIONS/world_observation_catalog.csv``.
* optional ``--block201`` — the SITE_BLOCK201_LINE12 stream (benchmarks of profile line 12 in SH1, block 201 history);
* ``--geology-npz`` — contact fields of the geology model v6_2 (FULL_CONTEXT_PARTIAL, absolute heights, DSM-anchored);
* ``--terrain`` — Copernicus GLO-30 DSM resampled to SH1 (EGM2008; 2011–2015; DSM, not bare earth);
* optional ``--state-1990`` — worked-out areas ≈1990 by seam digitised from VKM-SRC-037 fig. 2.7 (state ≤ 1990).

Outputs: the canonical stream tables (``--stream-out``, no coordinates, quotes only in the evidence file) and the
delivery (``--delivery``: GeoPackage in SH1 without EPSG, CSV with geometry, WorldSpec JSON, figures, checks, receipt).

Nothing here is an observation of mining: plan contours are DERIVATION of schematic figures, dates are as printed,
intervals are bounds, UNKNOWN stays UNKNOWN. Plan ≠ fact; design chambers ≠ actual extraction.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import sqlite3
import struct
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vkm_world.mining.skru1_history import (  # noqa: E402
    DECADES, SEAMS, YearInterval, areal_extraction_ratio, ascii_id, combine_intervals, decade_state, interval_from_row,
    normalize_objects, normalize_seams, overlapping_same_seam, plan_key)

csv.field_size_limit(sys.maxsize)
FIELD_OPEN_LO = {"КрII": 1930, "АБ": 1930, "Вс": 1930, "Вк": 1936, "В?": 1930, "SYLV": 1930}
FIELD_OPEN_HI = 2026
SH1 = "SKRU1_LOCAL_SH1 (X восток, Y север, м; начало — ствол № 1; EPSG UNKNOWN)"
LOCAL_WKT = ('LOCAL_CS["SKRU1_LOCAL_SH1 - local mine-field frame, global tie UNKNOWN",LOCAL_DATUM["Unknown",32767],'
             'UNIT["metre",1],AXIS["X East",EAST],AXIS["Y North",NORTH]]')
MINING_ATTR = {"MINING_PERIOD", "MINING_START", "MINING_END"}
BACKFILL_ATTR = {"BACKFILL_PERIOD", "BACKFILL_START", "BACKFILL_END"}
PARAM_ATTR = {"CHAMBER_WIDTH": "chamber_width_m", "PILLAR_WIDTH": "pillar_width_m", "AXIS_SPACING": "axis_spacing_m",
              "EXTRACTED_HEIGHT": "extracted_height_m", "EXTRACTION_RATIO": "extraction_ratio",
              "LOADING_DEGREE": "loading_degree", "BACKFILL_RATIO": "backfill_ratio", "BACKFILL_LAG": "backfill_lag_y"}
SEAM_ATTR = {"DEPTH", "ROOF_ELEVATION_ABS", "FLOOR_ELEVATION_ABS", "SEAM_THICKNESS", "INTERLAYER_THICKNESS", "KCL_CONTENT", "MGCL2_CONTENT",
             "INSOLUBLE", "DENSITY", "SEAM_COMPOSITION", "REPLACEMENT_ZONE", "VZT_THICKNESS", "DIP", "FOLDING",
             "STRAT_DEFINITION"}
EVENT_ATTR = {"ACCIDENT", "FLOODING", "BRINE_INFLOW", "SINKHOLE", "SECONDARY_MINING"}
UNIT_ATTR = MINING_ATTR | BACKFILL_ATTR | set(PARAM_ATTR) | EVENT_ATTR | {"BACKFILL_TYPE", "SYSTEM", "EXCAVATION_METHOD"}


# ----------------------------------------------------------------------------- small utilities
def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def write_csv(path: Path, rows: list[dict], cols: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with io.StringIO(newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, lineterminator="\n", extrasaction="raise")
        w.writeheader()
        for r in rows:
            w.writerow({c: "" if r.get(c) is None else r.get(c) for c in cols})
        path.write_text(fh.getvalue(), encoding="utf-8")


def fnum(v):
    if v is None:
        return None
    s = str(v).strip().replace(",", ".").replace("−", "-").replace("–", "-")
    try:
        return float(s)
    except ValueError:
        return None


def yr(v):
    if v is None:
        return None
    m = re.match(r"\s*(\d{4})", str(v))
    return int(m.group(1)) if m else None


def rel(p: Path, base: Path) -> str:
    try:
        return p.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return p.name


# ----------------------------------------------------------------------------- plan
def load_plan(c4map: Path):
    from shapely.geometry import shape
    from shapely.ops import unary_union
    work = json.loads((c4map / "skru1_workings.geojson").read_text(encoding="utf-8"))
    pts = json.loads((c4map / "skru1_points.geojson").read_text(encoding="utf-8"))
    bnd = json.loads((c4map / "skru1_boundaries.geojson").read_text(encoding="utf-8"))
    regions, layers = [], defaultdict(list)
    for f in work["features"]:
        p = f["properties"]
        g = shape(f["geometry"])
        if not g.is_valid:
            g = g.buffer(0)
        item = {"id": p["id"], "layer": p["layer"], "kind": p["kind"], "label": p.get("label_as_printed", ""),
                "parent": p.get("parent_panel", ""), "seam": p.get("seam", ""), "geom": g, "props": p}
        if p["layer"] == "PANEL_BLOCK_LAYOUT_014":
            item["key"] = plan_key(p["kind"], p.get("label_as_printed", ""), p.get("parent_panel", ""))
            regions.append(item)
        else:
            layers[p["layer"]].append(item)
    field = unary_union([r["geom"] for r in regions] + [x["geom"] for x in layers["ATTACHED_AREA_064"]])
    key_index = defaultdict(list)
    for r in regions:
        if r["key"]:
            key_index[r["key"]].append(r)
    return regions, layers, key_index, field, pts["features"], bnd["features"]


def region_error(cx: float) -> tuple[float, float, str]:
    """Plan position error class from the calibration (README of c4_map/skru1): control points are all in the east."""
    if cx > 1500:
        return 15.0, 30.0, "восток: подобие по 14 общим точкам 014/064 (LOO 10 м, max 19 м); сверка с сеткой ВКР — медиана 29 м"
    if cx > -1000:
        return 20.0, 40.0, "центр: экстраполяция подобия; сверка с сеткой ВКР — медиана 29,5 м, p90 48 м"
    return 30.0, 60.0, "запад: экстраполяция 1,2–2,3 км от опор; подобие и аффинное расходятся на 15–17 м; сверки по сетке ВКР нет"


# ----------------------------------------------------------------------------- evidence
EV_COLS = ["ev_id", "origin", "origin_row", "object_text", "object_keys", "object_flags", "seams", "attribute",
           "value_as_printed", "value_min", "value_max", "unit", "start_min", "start_max", "end_min", "end_max",
           "date_as_printed", "planned_or_actual", "site", "status", "source_id", "page_id", "locator", "known_row_id",
           "note"]


def ev(origin, row_id, obj, seams_text, attribute, poa, site, status, source, page, locator, **kw):
    keys, flags = normalize_objects(obj)
    if obj and re.search(r"целик", obj, re.I) and not re.search(r"блок|камер", obj, re.I):
        keys, flags = [], ["PILLAR_OBJECT"]  # a pillar between panels / mines is its own object, not the named panels
    if not keys and re.search(r"рудник|шахтн\w* пол|СКРУ-1|СКПРУ-1|Первый|все участки|^ВКМ$", obj or "", re.I):
        keys = ["MINE"]
    if not keys and obj:
        keys = ["TXT:" + re.sub(r"[\s;]+", " ", obj.strip())[:48]]
    seams = normalize_seams(seams_text)
    r = {"ev_id": f"{origin}:{row_id}", "origin": origin, "origin_row": row_id, "object_text": obj or "",
         "object_keys": ";".join(keys), "object_flags": ";".join(flags), "seams": ";".join(seams),
         "attribute": attribute, "planned_or_actual": poa or "UNKNOWN", "site": site, "status": status,
         "source_id": source, "page_id": page, "locator": locator}
    r.update(kw)
    for c in EV_COLS:
        r.setdefault(c, "")
    return r


CAL_EVENT = {"MINING_PERIOD": "MINING_PERIOD", "MINING_START": "MINING_START", "MINING_END": "MINING_END",
             "BACKFILL_PERIOD": "BACKFILL_PERIOD", "BACKFILL_START": "BACKFILL_START", "BACKFILL_END": "BACKFILL_END"}


def load_calendar(curated: Path) -> list[dict]:
    out = []
    for r in read_csv(curated / "skru1_mining_calendar_20261010.csv"):
        att = CAL_EVENT.get(r["event"], "OTHER")
        y0, y1 = yr(r["date_start_iso"]), yr(r["date_end_iso"])
        if att in ("MINING_PERIOD", "BACKFILL_PERIOD"):
            smin = smax = y0
            emin = emax = y1 if y1 else None  # no printed end: the period is open-ended (not a single year)
            if y0 is None and y1 is not None:
                smin, smax = None, y1
        elif att in ("MINING_START", "BACKFILL_START"):
            smin = smax = y0
            emin, emax = (y0, y1) if y1 else (y0, None)
        elif att in ("MINING_END", "BACKFILL_END"):
            # «отработано к …» / «завершены в …»: an upper bound of the end; the end itself may be earlier
            # «В 2012 году … доработаны» is the end year; «отработаны (ВКР 2026)», «к 2027 году» are upper bounds only
            exact = bool(re.match(r"\s*[Вв]\s+\d{4}", r["date_end_as_printed"] or ""))
            smin, smax, emin, emax = None, None, (y1 if exact else None), y1
        else:
            smin = smax = y0
            emin = emax = y1 if y1 else y0
        base = dict(start_min=smin, start_max=smax, end_min=emin, end_max=emax,
                    date_as_printed=" – ".join(x for x in (r["date_start_as_printed"], r["date_end_as_printed"]) if x),
                    note=(r["method_as_printed"] + ("; " + r["note"] if r["note"] else "")).strip("; "))
        obj = r["object_as_printed"] or r["object_id"]
        out.append(ev("C4CAL", r["row_id"], obj, r["seam"], att, r["planned_or_actual"], r["site_norm"], r["status"],
                      r["source_id"], r["page_id"], r["locator"], value_as_printed=r["method_as_printed"], **base))
        for col, attr in (("chamber_width_m", "CHAMBER_WIDTH"), ("pillar_width_m", "PILLAR_WIDTH"),
                          ("extracted_height_m", "EXTRACTED_HEIGHT"), ("extraction_ratio", "EXTRACTION_RATIO"),
                          ("backfill_ratio", "BACKFILL_RATIO")):
            if r[col].strip():
                nums = [float(x.replace(",", ".")) for x in re.findall(r"\d+(?:[.,]\d+)?", r[col])]
                vmin, vmax = (min(nums), max(nums)) if nums else (None, None)
                out.append(ev("C4CAL", r["row_id"] + ":" + attr, obj, r["seam"], attr, r["planned_or_actual"],
                              r["site_norm"], r["status"], r["source_id"], r["page_id"], r["locator"],
                              value_as_printed=r[col], value_min=vmin, value_max=vmax,
                              unit="1" if attr in ("EXTRACTION_RATIO", "BACKFILL_RATIO") else "m", **base))
    return out


def load_chamber_design(curated: Path) -> list[dict]:
    out = []
    for i, r in enumerate(read_csv(curated / "skru1_c4_chamber_design.csv"), 1):
        for col, attr in (("chamber_width_m", "CHAMBER_WIDTH"), ("pillar_width_m", "PILLAR_WIDTH"),
                          ("areal_extraction_ratio", "EXTRACTION_RATIO")):
            nums = [float(x.replace(",", ".")) for x in re.findall(r"\d+(?:[.,]\d+)?", r.get(col, ""))]
            if nums:
                out.append(ev("C4DESIGN", f"{i:02d}:{attr}", "шахтное поле СКРУ-1 (" + r["zone"] + ")", r["seam"], attr,
                              "DESIGN", "SKRU1", r["status"], r["source_id"], "", r["locator"],
                              value_as_printed=r[col], value_min=min(nums), value_max=max(nums),
                              unit="1" if attr == "EXTRACTION_RATIO" else "m",
                              note=f"{r['label']}; {r['extraction_ratio_status']}; {r['note']}".strip("; ")))
    return out


PH1_TYPE = {"EXTRACTION_PERIOD": "MINING_PERIOD", "EXTRACTION_START": "MINING_START", "EXTRACTION_END": "MINING_END",
            "BACKFILL_PERIOD": "BACKFILL_PERIOD", "WATER_INFLOW": "BRINE_INFLOW", "ROOF_FALL": "ACCIDENT",
            "DYNAMIC_EVENT": "ACCIDENT", "INTERSEAM_FAILURE": "ACCIDENT", "FLOODING": "FLOODING"}


def load_phase1(mining: Path, known_phase1: set[str]) -> list[dict]:
    out = []
    for r in read_csv(mining / "mining_chronology_catalog.csv"):
        if r["mine_attribution_scope"] != "СКРУ-1" or r["event_id"] in known_phase1:
            continue
        att = PH1_TYPE.get(r["event_type_vkm"])
        if not att:
            continue
        y0, y1 = yr(r["date_start_iso"]), yr(r["date_end_iso"])
        kw = dict(start_min=y0, start_max=y0, end_min=y1 or y0, end_max=y1 or (y0 if att == "MINING_PERIOD" else None),
                  date_as_printed=" – ".join(x for x in (r["date_start_as_printed"], r["date_end_as_printed"]) if x),
                  value_as_printed=r["event"][:200], note=r["notes"][:300])
        out.append(ev("PH1CHR", r["event_id"], r["object"], r["seams_named"], att, r["planned_or_actual"], "SKRU1",
                      r["status"], r["source_ids"].split(";")[0].strip(), "", r["locators"][:200], **kw))
    for r in read_csv(mining / "mining_geometry_catalog.csv"):
        if r["mine_scope"] != "СКРУ-1" or r["row_id"] in known_phase1:
            continue
        lab = r["design_actual_label"] or "UNKNOWN"
        poa = {"ACTUAL": "ACTUAL", "DESIGN": "DESIGN", "TEACHING": "TEACHING"}.get(lab.split()[0] if lab else "", lab)
        obj = (r["panel_block"] or r["object"])[:200]
        for col, attr in (("chamber_width_m", "CHAMBER_WIDTH"), ("pillar_width_m", "PILLAR_WIDTH"),
                          ("chamber_height_m", "EXTRACTED_HEIGHT"), ("axis_spacing_m", "AXIS_SPACING"),
                          ("extraction_ratio_as_printed", "EXTRACTION_RATIO"), ("loading_degree_C", "LOADING_DEGREE"),
                          ("depth_m_as_printed", "DEPTH"), ("seam_thickness_as_printed", "SEAM_THICKNESS"),
                          ("interseam_thickness_m", "INTERLAYER_THICKNESS")):
            nums = [float(x.replace(",", ".")) for x in re.findall(r"\d+(?:[.,]\d+)?", r.get(col, "") or "")]
            if nums:
                out.append(ev("PH1GEO", r["row_id"] + ":" + attr, obj, r["seam"], attr, poa, "SKRU1", r["status"],
                              r["source_ids"].split(";")[0].strip(), "", r["locators"][:200], value_as_printed=r[col],
                              value_min=min(nums), value_max=max(nums),
                              unit="1" if attr in ("EXTRACTION_RATIO", "LOADING_DEGREE") else "m",
                              date_as_printed=r["period_as_printed"], note=r["notes"][:300]))
    for r in read_csv(mining / "backfill_event_catalog.csv"):
        if r["mine_scope"] != "СКРУ-1" or r["row_id"] in known_phase1:
            continue
        per = r["period_backfill_as_printed"]
        ys = [int(y) for y in re.findall(r"(?<!\d)(19\d\d|20[0-2]\d)(?!\d)", per)]
        out.append(ev("PH1BF", r["row_id"], r["target_as_printed"], r["seam"], "BACKFILL_TYPE",
                      (r["design_actual_label"] or "UNKNOWN").split()[0] if r["design_actual_label"] else "UNKNOWN",
                      "SKRU1", r["status"], r["source_ids"].split(";")[0].strip(), "", r["locators"][:200],
                      value_as_printed=(r["method_as_printed"] + " " + r["material_as_printed"]).strip()[:200],
                      start_min=min(ys) if ys else None, start_max=min(ys) if ys else None,
                      end_min=max(ys) if ys else None, end_max=max(ys) if ys else None, date_as_printed=per,
                      note=r["notes"][:300]))
    return out


def load_new_evidence(path: Path) -> list[dict]:
    out = []
    if not path.is_file():
        return out
    for r in read_csv(path):
        out.append(ev("SKM", r["evidence_id"], r["object_norm"] or r["object_as_printed"], r["seam"], r["attribute"],
                      r["planned_or_actual"], r["site_attribution"], r["status"], r["source_id"], r["page_id"],
                      r["locator"], value_as_printed=r["value_as_printed"], value_min=fnum(r["value_min"]),
                      value_max=fnum(r["value_max"]), unit=r["unit"], start_min=yr(r["date_start_min"]),
                      start_max=yr(r["date_start_max"]), end_min=yr(r["date_end_min"]), end_max=yr(r["date_end_max"]),
                      date_as_printed=" – ".join(x for x in (r["date_start_as_printed"], r["date_end_as_printed"]) if x),
                      known_row_id=r.get("known_row_id", ""), note=r["note"]))
    return out


def load_block201(path: Path | None) -> tuple[list[dict], list[dict]]:
    if not path or not path.is_dir():
        return [], []
    hist = []
    for r in read_csv(path / "block201_mining_history.csv"):
        att = {"MINING_PERIOD": "MINING_PERIOD", "BACKFILL_PERIOD": "BACKFILL_PERIOD"}.get(r["event"], r["event"])
        hist.append(ev("B201", r["record_id"], "блок 201 (" + r["part_of_block"] + ")", r["seam"], att,
                       "ACTUAL" if "FACT" in r["status"] else "UNKNOWN", "SKRU1", r["status"].split()[0],
                       r["source_id"], r["page_id"], r["locator"], start_min=yr(r["start_min"]), start_max=yr(r["start_max"]),
                       end_min=yr(r["end_min"]), end_max=yr(r["end_max"]), value_as_printed=r["method_as_printed"],
                       date_as_printed=r["time_basis"], note=r["notes"]))
    objs = read_csv(path / "site_objects_sh1.csv")
    return hist, objs


# ----------------------------------------------------------------------------- units
UNIT_COLS = ["unit_id", "object_key", "object_label", "object_kind", "seam", "plan_region_ids", "plan_area_m2",
             "plan_match", "pos_err_m_min", "pos_err_m_max", "timing_class", "mining_start_min", "mining_start_max",
             "mining_end_min", "mining_end_max", "mining_core", "mining_n_records", "mining_conflict",
             "mining_open_bounds", "derived_periods", "planned_years", "system_as_printed", "excavation_method",
             "chamber_width_m", "pillar_width_m", "extracted_height_m", "design_chamber_width_m", "design_pillar_width_m",
             "design_extracted_height_m", "extraction_ratio", "extraction_ratio_basis", "loading_degree",
             "backfill_type", "backfill_start_min", "backfill_end_max", "backfill_ratio", "backfill_lag_y",
             "secondary_mining", "events", "state_1990_037", "state_2007_064", "state_2013_014", "design_or_actual", "status",
             "source_ids", "evidence_ids", "conflict_ids", "notes"]


def worked_1990(u: dict, seam: str) -> str | None:
    """Text of the ≈1990 bound when the unit is ≥ 0.5 worked on VKM-SRC-037 fig. 2.7; for seam В the composition of the
    worked area is not shown, so the bound is used for Вк only where the Вк extent (legend 5) or plan 064 shows Вк in
    the unit, and for Вс only where plan 064 shows Вс worked (INTERPRETATION)."""
    st = u.get("state_1990_037") or ""
    m = re.match(r"WORKED_[^_]+(?:\(Вс\|Вк\))?_(\d\.\d+)", st)
    if not m or float(m.group(1)) < 0.5:
        return None
    if seam in ("КрII", "АБ"):
        return f"рис. 2.7 VKM-SRC-037: {seam} отработан на ≥ {float(m.group(1)):.2f} площади, состояние ≤ 1990"
    vk = re.search(r"VK_EXTENT_(\d\.\d+)", st)
    if seam == "Вк" and ((vk and float(vk.group(1)) >= 0.2) or "WORKED_Вк" in u.get("state_2007_064", "")):
        return "рис. 2.7в VKM-SRC-037: пласт В отработан, состав по соседству — Вк (INTERPRETATION), состояние ≤ 1990"
    if seam == "Вс" and "WORKED_Вс" in u.get("state_2007_064", ""):
        return "рис. 2.7в VKM-SRC-037: пласт В отработан, состав — Вс по схеме 064 (INTERPRETATION), состояние ≤ 1990"
    return None


def merge_periods(iv: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Union of closed year intervals (overlapping or adjacent ones are merged)."""
    out: list[list[int]] = []
    for a0, a1 in sorted(iv):
        if out and a0 <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], a1)
        else:
            out.append([a0, a1])
    return [(a, b) for a, b in out]


def param_range(rows, attr):
    vals = [(r["value_min"], r["value_max"]) for r in rows if r["attribute"] == attr
            and r["planned_or_actual"] in ("ACTUAL", "STATE_SNAPSHOT", "UNKNOWN", "RECORDED")
            and r["value_min"] not in ("", None)]
    if not vals:
        return ""
    lo = min(float(a) for a, _ in vals)
    hi = max(float(b if b not in ("", None) else a) for a, b in vals)
    return f"{lo:g}" if lo == hi else f"{lo:g}–{hi:g}"


def zone_aliases(curated: Path, regions) -> dict[str, list[dict]]:
    """Blocks named in VKM-SRC-023 zone records but unlabelled on plan 014: the zone centre (crosswalk 10.10, DERIVATION)
    gives the plan region. Only regions without a label of their own are aliased."""
    by_id = {r["id"]: r for r in regions}
    out: dict[str, list[dict]] = {}
    p = curated / "c4_map" / "skru1" / "crosswalk_zones.csv"
    if not p.is_file():
        return out
    for r in read_csv(p):
        m = re.match(r"(\d+)\s*П?\s*блок", r.get("block_as_printed", ""))
        pid = r.get("polygon_id", "")
        if m and pid in by_id and not by_id[pid]["key"] and r.get("link_status") == "DERIVATION":
            out.setdefault(f"BLK:{int(m.group(1))}", [by_id[pid]])
    return out


def build_units(evidence, key_index, regions, layers, aliases=None, cal_sources=None):
    from shapely.ops import unary_union
    aliases = aliases or {}
    cal_sources = cal_sources or {}
    groups = defaultdict(list)
    for r in evidence:
        if r["site"] not in ("SKRU1", "SKRU1_PROBABLE", "SKRU1_SKRU2_PILLAR", "SKRU1_SKRU3_PILLAR"):
            continue
        if r["origin"] == "SKM" and r["attribute"] in MINING_ATTR | BACKFILL_ATTR and any(
                cal_sources.get(cid) == r["source_id"] for cid in re.findall(r"C4C-\d{4}", r.get("known_row_id", ""))):
            continue  # confirmation of a calendar row of the same source (kept in the evidence file, not counted twice)
        keys = [k for k in r["object_keys"].split(";") if k] or ["UNRESOLVED"]
        if r["attribute"] not in UNIT_ATTR:
            keys = [k for k in keys if not k.startswith("TXT:")]  # text objects enter the units only with mining facts
        seams = [s for s in r["seams"].split(";") if s] or ["UNSPECIFIED"]
        for k in keys:
            for s in seams:
                if s == "SYLV":
                    s = "SYLV"
                groups[(k, s)].append(r)
    # every labelled plan region × seam is a unit even without text evidence (coverage table)
    for k in key_index:
        for s in SEAMS:
            groups.setdefault((k, s), [])
    w07 = layers.get("WORKED_OUT_2007_064", [])
    rem13 = layers.get("REMAINING_RESERVES_2013_014", [])
    units = []
    for (k, s), rows in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        regs = key_index.get(k, [])
        via_alias = not regs and k in aliases
        if via_alias:
            regs = aliases[k]
        geom = unary_union([g["geom"] for g in regs]) if regs else None
        u = {c: "" for c in UNIT_COLS}
        u.update(object_key=k, seam=s, unit_id=ascii_id(f"SKU-{k.replace(':', '-')}-{s}") if not k.startswith("TXT:") else
             ascii_id(f"SKU-TXT-{hashlib.sha1(k.encode('utf-8')).hexdigest()[:8]}-{s}"))
        u["object_kind"] = {"BLK": "BLOCK", "PAN": "PANEL", "SUB": "SUBBLOCK"}.get(k.split(":")[0], k)
        u["object_label"] = (regs[0]["label"] + (f" ({regs[0]['parent']})" if regs and regs[0]["parent"] else "")
                             if regs else ("шахтное поле (весь рудник)" if k == "MINE" else
                                           (rows[0]["object_text"][:80] if rows else "")))
        u["plan_region_ids"] = ";".join(g["id"] for g in regs)
        u["plan_match"] = ("ZONE_CENTRE_CROSSWALK_023" if via_alias else "LABEL_ON_PLAN_014") if regs else (
            "MINE_LEVEL" if k == "MINE" else ("TEXT_OBJECT_NOT_ON_PLAN" if k.startswith("TXT:") else "NOT_ON_PLAN"))
        if geom is not None:
            u["plan_area_m2"] = round(geom.area)
            lo, hi, _ = region_error(geom.centroid.x)
            u["pos_err_m_min"], u["pos_err_m_max"] = lo, hi
            # states from plan snapshots (fractions of the unit area)
            if s in SEAMS:
                cls = {"КрII": "АБ|КрII", "АБ": "АБ|КрII", "Вс": "В (сильвинитовый)", "Вк": "В (карналлитовый)"}[s]
                inter = sum(geom.intersection(x["geom"]).area for x in w07 if x["seam"].startswith(cls.split()[0])
                            and (cls in x["seam"] or (cls == "АБ|КрII" and x["seam"].startswith("АБ|КрII"))))
                rk = {"КрII": "КрII", "АБ": "АБ", "Вс": "В (сильвинитовый", "Вк": "В (карналлитовый"}[s]
                inter13 = sum(geom.intersection(x["geom"]).area for x in rem13 if x["seam"].startswith(rk))
                in064 = max((g["props"].get("in_064_extent_frac") or 0) for g in regs)
                f07 = inter / geom.area if geom.area else 0
                f13 = inter13 / geom.area if geom.area else 0
                u["state_2007_064"] = ("OUTSIDE_064" if in064 < 0.5 else
                                       (f"WORKED_{'AB_OR_KR2' if cls == 'АБ|КрII' else s}_{f07:.2f}" if f07 >= 0.05 else
                                        f"NOT_SHOWN_WORKED_{f07:.2f}"))
                u["state_2013_014"] = f"REMAINING_{f13:.2f}" if f13 >= 0.05 else f"NOT_REMAINING_{f13:.2f}"
                s90 = layers.get("STATE_C1990_037", [])
                if s90:
                    key90 = {"КрII": "КрII", "АБ": "АБ", "Вс": "В (Вс|Вк)", "Вк": "В (Вс|Вк)"}[s]
                    w90 = sum(geom.intersection(x["geom"]).area for x in s90
                              if x["kind"] == "WORKED_C1990" and x["seam"] == key90) / geom.area
                    vk = sum(geom.intersection(x["geom"]).area for x in s90 if x["kind"] == "VK_EXTENT_VERTICAL") / geom.area
                    tag = "WORKED_V(Вс|Вк)" if s in ("Вс", "Вк") else f"WORKED_{s}"
                    u["state_1990_037"] = (f"{tag}_{w90:.2f}" if w90 >= 0.05 else f"NOT_SHOWN_WORKED_{w90:.2f}") + (
                        f";VK_EXTENT_{vk:.2f}" if s in ("Вс", "Вк") and vk >= 0.05 else "")
        all_acts = [r for r in rows if r["attribute"] in MINING_ATTR and r["planned_or_actual"] in ("ACTUAL", "UNKNOWN")]
        acts = [r for r in all_acts if r["status"] == "FACT"]
        derived = [r for r in all_acts if r["status"] in ("DERIVATION", "INTERPRETATION")]
        plans = [r for r in rows if r["attribute"] in MINING_ATTR and r["planned_or_actual"] in ("PLANNED", "EXPECTED",
                                                                                                 "FORECAST", "DESIGN")]
        open_lo = FIELD_OPEN_LO.get(s, 1930)
        dspans = []
        for r in derived:
            iv = interval_from_row(r["start_min"], r["start_max"], r["end_min"], r["end_max"])
            if iv is not None:
                f = iv.filled(open_lo, FIELD_OPEN_HI)
                dspans.append((f.start_min, f.end_max, r["ev_id"], r["status"]))
        u["derived_periods"] = ";".join(f"{a0}–{a1} [{st}]" for a0, a1, _, st in sorted(set(dspans)))
        closed, opened = [], []
        for r in acts:
            iv = interval_from_row(r["start_min"], r["start_max"], r["end_min"], r["end_max"])
            if iv is None:
                continue
            f = iv.filled(open_lo, FIELD_OPEN_HI)
            rec = (f.start_min, f.end_max, r["source_id"], r["ev_id"], iv)
            (closed if iv.start_min is not None and iv.end_max is not None else opened).append(rec)
        periods = merge_periods([(a0, a1) for a0, a1, *_ in closed])
        if closed or opened:
            allrec = closed + opened
            u["mining_start_min"] = min(x[0] for x in allrec)
            u["mining_start_max"] = min(x[4].filled(open_lo, FIELD_OPEN_HI).start_max for x in allrec)
            u["mining_end_min"] = max(x[4].filled(open_lo, FIELD_OPEN_HI).end_min for x in allrec)
            u["mining_end_max"] = max(x[1] for x in allrec)
            u["mining_core"] = ";".join(f"{a0}–{a1}" for a0, a1 in periods)
            u["mining_n_records"] = len(allrec)
            u["mining_open_bounds"] = ";".join(sorted({
                ("START≥%d" % open_lo if x[4].start_min is None else "") + ("END≤%d" % FIELD_OPEN_HI if x[4].end_max is None else "")
                + f" ({x[3]})" for x in opened}))
            srcs = defaultdict(list)
            for a0, a1, sid, eid, _ in closed:
                srcs[sid].append((a0, a1, eid))
            flagged = []
            sids = sorted(srcs)
            for i in range(len(sids)):
                for j in range(i + 1, len(sids)):
                    for a in srcs[sids[i]]:
                        for b_ in srcs[sids[j]]:
                            if a[1] < b_[0] or b_[1] < a[0]:
                                flagged.append(f"{sids[i]} {a[0]}–{a[1]} vs {sids[j]} {b_[0]}–{b_[1]}")
            u["mining_conflict"] = "CHECK" if flagged else ""
            u["notes"] = ("; ".join(flagged) + "; " if flagged else "") + u.get("notes", "")
            u["timing_class"] = ("TEXT_DATED" if closed and not opened else
                                 ("TEXT_PARTLY_DATED" if opened and closed else "TEXT_OPEN_ENDED"))
            parts = sorted({r["object_text"][:60] for r in acts if re.search(r"част|участ|зона|целик|южн|северн|западн|восточн",
                                                                           r["object_text"], re.I)})
            if parts:
                u["notes"] = (u["notes"] + "; " if u["notes"] else "") + "ЧАСТЬ ОБЪЕКТА: " + " | ".join(parts)[:250]
        elif dspans:
            u["timing_class"] = "DERIVED_DATES"
            u["mining_start_min"] = min(x[0] for x in dspans)
            u["mining_end_max"] = max(x[1] for x in dspans)
            u["mining_open_bounds"] = "даты выведены (DERIVATION/INTERPRETATION), не напечатаны"
        elif acts or derived:
            u["timing_class"] = "TEXT_UNDATED"
        elif worked_1990(u, s) is not None:
            u["timing_class"] = "PLAN_SNAPSHOT_ONLY"
            u["mining_start_min"], u["mining_end_max"] = open_lo, 1990
            u["mining_open_bounds"] = f"START≥{open_lo};END≤1990 ({worked_1990(u, s)})"
        elif u["state_2007_064"].startswith("WORKED"):
            u["timing_class"] = "PLAN_SNAPSHOT_ONLY"
            u["mining_start_min"], u["mining_end_max"] = open_lo, 2006
            u["mining_open_bounds"] = f"START≥{open_lo};END≤2006 (064: отработано на 01.01.2007)"
        else:
            u["timing_class"] = "UNKNOWN"
        w90b = worked_1990(u, s)
        if w90b and u["timing_class"] in ("TEXT_DATED", "TEXT_PARTLY_DATED", "TEXT_OPEN_ENDED"):
            if u["mining_start_min"] not in ("", None) and int(u["mining_start_min"]) > 1990:
                u["mining_conflict"] = "CHECK"
                u["notes"] = (f"текст: выемка с {u['mining_start_min']} г., но {w90b} — " +
                              ("; " + u["notes"] if u["notes"] else "")).rstrip("; ")
            elif u["mining_open_bounds"] and u["mining_end_max"] not in ("", None) and int(u["mining_end_max"]) > 1990 \
                    and not u["mining_core"]:
                u["mining_end_max"] = 1990
                u["mining_open_bounds"] += f"; END≤1990 ({w90b})"
        f07 = re.match(r"WORKED_(Вк|Вс)_(\d\.\d+)", u["state_2007_064"])
        if f07 and float(f07.group(2)) >= 0.5 and u["mining_end_max"] not in ("", None) and                 int(u["mining_end_max"]) > 2006 and int(u["mining_start_min"] or open_lo) <= 2006 and not u["mining_core"]:
            u["mining_end_max"] = 2006
            u["mining_open_bounds"] = (u["mining_open_bounds"] + "; " if u["mining_open_bounds"] else "") +                 "END≤2006 по схеме 064 (отработано на 01.01.2007, доля площади ≥ 0,5)"
        if plans:
            ys = sorted({y for r in plans for y in (r["start_min"], r["end_max"]) if y not in ("", None)})
            u["planned_years"] = ";".join(str(y) for y in ys)
            if not acts and u["timing_class"] == "UNKNOWN":
                u["timing_class"] = "PLANNED_ONLY"
        sysrows = [r for r in rows if r["attribute"] in ("SYSTEM", "EXCAVATION_METHOD") or
                   (r["attribute"] in MINING_ATTR and r["value_as_printed"])]
        u["system_as_printed"] = " | ".join(sorted({r["value_as_printed"][:90] for r in sysrows
                                                    if r["attribute"] in ("SYSTEM",) or r["attribute"] in MINING_ATTR}
                                                   - {""}))[:400]
        u["excavation_method"] = " | ".join(sorted({r["value_as_printed"][:80] for r in rows
                                                    if r["attribute"] == "EXCAVATION_METHOD"}))[:300]
        for attr, col in PARAM_ATTR.items():
            if col in u:
                u[col] = param_range(rows, attr)
        for attr, col in (("CHAMBER_WIDTH", "design_chamber_width_m"), ("PILLAR_WIDTH", "design_pillar_width_m"),
                          ("EXTRACTED_HEIGHT", "design_extracted_height_m")):
            u[col] = param_range([dict(r, planned_or_actual="ACTUAL") for r in rows
                                  if r["planned_or_actual"] in ("DESIGN", "PLANNED")], attr)
        if not u["extraction_ratio"] and u["chamber_width_m"] and u["pillar_width_m"]:
            try:
                a = float(u["chamber_width_m"].split("–")[0])
                b = float(u["pillar_width_m"].split("–")[0])
                u["extraction_ratio"] = f"{areal_extraction_ratio(a, b):.2f}"
                u["extraction_ratio_basis"] = "DERIVATION k = a/(a+b) по ширинам (первые значения диапазонов)"
            except ValueError:
                pass
        elif u["extraction_ratio"]:
            u["extraction_ratio_basis"] = "как напечатано"
        bfs = [r for r in rows if r["attribute"] in BACKFILL_ATTR | {"BACKFILL_TYPE"}
               and r["planned_or_actual"] in ("ACTUAL", "UNKNOWN", "STATE_SNAPSHOT")]
        if bfs:
            u["backfill_type"] = " | ".join(sorted({r["value_as_printed"][:80] for r in bfs if r["value_as_printed"]}))[:300]
            bmins = [int(r["start_min"]) for r in bfs if r["start_min"] not in ("", None)]
            bmaxs = [int(r["end_max"]) for r in bfs if r["end_max"] not in ("", None)]
            u["backfill_start_min"] = min(bmins) if bmins else ""
            u["backfill_end_max"] = max(bmaxs) if bmaxs else ""
        u["secondary_mining"] = " | ".join(sorted({f"{r['value_as_printed'][:80]} [{r['planned_or_actual']}]"
                                                   for r in rows if r["attribute"] == "SECONDARY_MINING"}))[:300]
        u["events"] = " | ".join(sorted({f"{r['attribute']}: {r['value_as_printed'][:70]} ({r['date_as_printed'][:20]})"
                                         for r in rows if r["attribute"] in EVENT_ATTR - {"SECONDARY_MINING"}}))[:400]
        poas = {r["planned_or_actual"] for r in rows if r["attribute"] in MINING_ATTR}
        u["design_or_actual"] = ("ACTUAL" if "ACTUAL" in poas else ("PLANNED" if poas & {"PLANNED", "EXPECTED"} else
                                                                    ("DESIGN" if "DESIGN" in poas else "UNKNOWN")))
        stats = {r["status"] for r in rows}
        u["status"] = ("FACT (даты как напечатаны); геометрия — DERIVATION" if acts and regs else
                       ("FACT (без геометрии)" if acts else ("DERIVATION (состояние по схемам плана)"
                                                              if u["timing_class"] == "PLAN_SNAPSHOT_ONLY" else "UNKNOWN")))
        if stats and "INTERPRETATION" in stats and not acts:
            u["status"] = "INTERPRETATION"
        u["source_ids"] = ";".join(sorted({r["source_id"] for r in rows if r["source_id"]}))
        u["evidence_ids"] = ";".join(r["ev_id"] for r in rows)[:3000]
        units.append((u, geom))
    return units


# ----------------------------------------------------------------------------- geology and terrain sampling
def load_fields(npz_path: Path | None, terrain_path: Path | None):
    import numpy as np
    geo = ter = None
    if npz_path and npz_path.is_file():
        z = np.load(npz_path, allow_pickle=False)
        geo = {"xy": z["xy"], "c": z["contacts"], "mask": z["support_mask"], "version": str(z["geometry_version"])}
    if terrain_path and terrain_path.is_file():
        t = json.loads(terrain_path.read_text(encoding="utf-8"))
        p = np.asarray(t["points"], dtype=float)
        ter = {"xy": p[:, :2], "z": p[:, 2], "id": t.get("surface_id", "")}
    return geo, ter


CONTACTS = ("TKT", "SMT", "PKS", "SKZ", "SZ", "PDKS")
# Seam position relative to the zone contacts. Definition (FACT, VKM-SRC-020 p. 7; VKM-SRC-014 p. 14): the roof of the
# sylvinite zone (SZ) is the roof of seam A = the floor of seam Б, i.e. the SZ/SKZ boundary runs inside the worked seam АБ.
# Offsets are printed averages (regional or of the Solikamsk area) — their transfer to the v6_2 contacts of SKRU-1 is an
# ENGINEERING_ASSUMPTION scenario, never a pick.
SCN = {
    "A_thickness_m": (1.4, 1.4, "VKM-SRC-020 p0007: средняя мощность пласта А по ВКМ 1,4 м (регионально)"),
    "KR2_roof_below_SZ_m": (6.1, 12.8, "VKM-SRC-014 p0013 рис. 1.2: 6,1 м (типовой разрез ВКМ, DERIVATION суммы средних); "
                                        "скв. 139 (СКРУ-2, VKM-SRC-012/045): 12,8 м"),
    "KR2_thickness_m": (5.97, 5.97, "VKM-SRC-254 p0005: средняя мощность КрII по полю СКРУ-1 5,97 м (n≈1438)"),
    "V_floor_above_AB_floor_m": (7.5, 10.5, "VKM-SRC-254 p0006: пласт В выше АБ на 7,5–10,5 м (Соликамская площадь)"),
}
FIELD_MEANS = {"КрII": -150.35, "АБ": -134.05, "В": -125.53}  # mean FLOOR elevations, VKM-SRC-254 p0005 (mine heights)


def sample_region(geom, geo, ter):
    import numpy as np
    from shapely import contains_xy
    out = {}
    if ter is not None:
        inside = contains_xy(geom, ter["xy"][:, 0], ter["xy"][:, 1])
        z = ter["z"][inside]
        if z.size == 0:
            c = geom.representative_point()
            j = int(np.argmin(np.hypot(ter["xy"][:, 0] - c.x, ter["xy"][:, 1] - c.y)))
            z = ter["z"][[j]]
        out.update(dsm_min=float(z.min()), dsm_med=float(np.median(z)), dsm_max=float(z.max()), dsm_n=int(z.size))
    if geo is not None:
        inside = contains_xy(geom, geo["xy"][:, 0], geo["xy"][:, 1])
        idx = np.nonzero(inside)[0]
        basis = "nodes_inside"
        if idx.size == 0:
            c = geom.representative_point()
            idx = np.array([int(np.argmin(np.hypot(geo["xy"][:, 0] - c.x, geo["xy"][:, 1] - c.y)))])
            basis = "nearest_node"
        cc = geo["c"][idx]
        for i, name in enumerate(CONTACTS):
            out[f"{name}_elev_min"] = float(cc[:, i].min())
            out[f"{name}_elev_med"] = float(np.median(cc[:, i]))
            out[f"{name}_elev_max"] = float(cc[:, i].max())
        m = Counter(str(x) for x in geo["mask"][idx])
        out["geo_support"] = ";".join(f"{k}:{v}" for k, v in sorted(m.items()))
        out["geo_basis"] = basis
        out["geo_n"] = int(idx.size)
    return out


# ----------------------------------------------------------------------------- GeoPackage (pure sqlite3)
def _gpkg_blob(geom, srs_id=100000):
    from shapely import to_wkb
    minx, miny, maxx, maxy = geom.bounds
    header = b"GP" + bytes([0, 0b00000011]) + struct.pack("<i", srs_id) + struct.pack("<4d", minx, maxx, miny, maxy)
    return header + to_wkb(geom, byte_order=1, output_dimension=2)


def write_gpkg(path: Path, layers: dict[str, tuple[str, list[dict], list[str]]], epoch: str, meta: dict):
    """Minimal GeoPackage 1.3 writer: one custom local SRS (no EPSG), one table per layer, geometry blob + attributes."""
    if path.exists():
        path.unlink()
    db = sqlite3.connect(path)
    db.execute("PRAGMA application_id = 1196444487")
    db.execute("PRAGMA user_version = 10300")
    db.executescript("""
    CREATE TABLE gpkg_spatial_ref_sys (srs_name TEXT NOT NULL, srs_id INTEGER PRIMARY KEY, organization TEXT NOT NULL,
      organization_coordsys_id INTEGER NOT NULL, definition TEXT NOT NULL, description TEXT);
    CREATE TABLE gpkg_contents (table_name TEXT NOT NULL PRIMARY KEY, data_type TEXT NOT NULL, identifier TEXT UNIQUE,
      description TEXT DEFAULT '', last_change DATETIME NOT NULL, min_x DOUBLE, min_y DOUBLE, max_x DOUBLE, max_y DOUBLE,
      srs_id INTEGER, CONSTRAINT fk_gc_r_srs_id FOREIGN KEY (srs_id) REFERENCES gpkg_spatial_ref_sys(srs_id));
    CREATE TABLE gpkg_geometry_columns (table_name TEXT NOT NULL, column_name TEXT NOT NULL, geometry_type_name TEXT NOT NULL,
      srs_id INTEGER NOT NULL, z TINYINT NOT NULL, m TINYINT NOT NULL, CONSTRAINT pk_geom_cols PRIMARY KEY (table_name, column_name));
    """)
    db.executemany("INSERT INTO gpkg_spatial_ref_sys VALUES (?,?,?,?,?,?)", [
        ("Undefined cartesian SRS", -1, "NONE", -1, "undefined", "undefined cartesian coordinate reference system"),
        ("Undefined geographic SRS", 0, "NONE", 0, "undefined", "undefined geographic coordinate reference system"),
        ("SKRU1_LOCAL_SH1", 100000, "VKM", 100000, LOCAL_WKT,
         "Local mine-field frame of SKRU-1: X east, Y north, metres, origin shaft No. 1; global tie UNKNOWN; no EPSG")])
    for name, (gtype, rows, cols) in layers.items():
        coldefs = ", ".join(f'"{c}" TEXT' for c in cols)
        db.execute(f'CREATE TABLE "{name}" (fid INTEGER PRIMARY KEY AUTOINCREMENT, geom {gtype}, {coldefs})')
        b = [r["geom"].bounds for r in rows if r.get("geom") is not None]
        ext = (min(x[0] for x in b), min(x[1] for x in b), max(x[2] for x in b), max(x[3] for x in b)) if b else (None,) * 4
        db.execute("INSERT INTO gpkg_contents VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (name, "features", name, meta.get(name, ""), epoch, *ext, 100000))
        db.execute("INSERT INTO gpkg_geometry_columns VALUES (?,?,?,?,?,?)", (name, "geom", gtype, 100000, 0, 0))
        for r in rows:
            g = r.get("geom")
            vals = [None if r.get(c) in (None, "") else str(r.get(c)) for c in cols]
            db.execute(f'INSERT INTO "{name}" (geom, {", ".join(chr(34) + c + chr(34) for c in cols)}) VALUES (?, {", ".join("?" * len(cols))})',
                       [_gpkg_blob(g) if g is not None else None, *vals])
    db.commit()
    ok = db.execute("PRAGMA integrity_check").fetchone()[0]
    db.close()
    if ok != "ok":
        raise RuntimeError("GeoPackage integrity check failed")


# ----------------------------------------------------------------------------- figures
SEAM_COLOURS = {"ACTIVE": "#c0392b", "POSSIBLE": "#f5b041", "DONE": "#7f8c8d", "NOT_YET": "#d6eaf8", "PLAN_1990": "#6c3483",
                "PLAN_2007": "#a569bd", "UNKNOWN": "#f2f3f4", "PLANNED": "#5dade2"}


def draw_regions(ax, regions, colour_of, field, title):
    from matplotlib.patches import PathPatch
    from matplotlib.path import Path as MPath
    import numpy as np

    def patch(g, fc):
        polys = [g] if g.geom_type == "Polygon" else list(g.geoms)
        for p in polys:
            verts = list(p.exterior.coords)
            codes = [MPath.MOVETO] + [MPath.LINETO] * (len(verts) - 2) + [MPath.CLOSEPOLY]
            for hole in p.interiors:
                hv = list(hole.coords)
                verts += hv
                codes += [MPath.MOVETO] + [MPath.LINETO] * (len(hv) - 2) + [MPath.CLOSEPOLY]
            ax.add_patch(PathPatch(MPath(np.asarray(verts), codes), facecolor=fc, edgecolor="#566573", lw=0.25))
    for r in regions:
        patch(r["geom"], colour_of(r))
    minx, miny, maxx, maxy = field.bounds
    ax.set_xlim(minx - 100, maxx + 100)
    ax.set_ylim(miny - 100, maxy + 100)
    ax.set_aspect("equal")
    ax.set_title(title, fontsize=8)
    ax.tick_params(labelsize=5)


def make_figures(out: Path, regions, units, layers, field):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch
    out.mkdir(parents=True, exist_ok=True)
    by_region_seam = defaultdict(list)
    for u, g in units:
        for rid in u["plan_region_ids"].split(";"):
            if rid:
                by_region_seam[(rid, u["seam"])].append(u)
    files = []

    def unit_state(us, decade):
        best = "UNKNOWN"
        d0, d1 = decade, decade + 9
        for u in us:
            if u["timing_class"] in ("TEXT_DATED", "TEXT_PARTLY_DATED", "TEXT_OPEN_ENDED"):
                per = [tuple(int(x) for x in p.split("–")) for p in u["mining_core"].split(";") if p]
                if any(a <= d1 and b >= d0 for a, b in per):
                    return "ACTIVE"
                env = (int(u["mining_start_min"]), int(u["mining_end_max"]))
                if u["mining_open_bounds"] and env[0] <= d1 and env[1] >= d0:
                    best = "POSSIBLE"
                elif best == "UNKNOWN":
                    best = "DONE" if env[1] < d0 else ("NOT_YET" if env[0] > d1 else "POSSIBLE")
            elif u["timing_class"] == "DERIVED_DATES" and best in ("UNKNOWN", "DONE", "NOT_YET"):
                env = (int(u["mining_start_min"]), int(u["mining_end_max"]))
                if env[0] <= d1 and env[1] >= d0:
                    best = "POSSIBLE"
            elif u["timing_class"] == "PLAN_SNAPSHOT_ONLY" and best == "UNKNOWN" and "END≤1990" in u["mining_open_bounds"] \
                    and decade >= 1990:
                best = "PLAN_1990"
            elif u["timing_class"] == "PLAN_SNAPSHOT_ONLY" and best == "UNKNOWN" and decade >= 2010:
                best = "PLAN_2007"
            elif u["timing_class"] == "PLANNED_ONLY" and best == "UNKNOWN" and decade >= 2010:
                ys = [int(y) for y in u["planned_years"].split(";") if y]
                if any(d0 <= y <= d1 for y in ys):
                    best = "PLANNED"
        return best

    legend = [Patch(color=SEAM_COLOURS[k], label=lab) for k, lab in (
        ("ACTIVE", "выемка в десятилетии (по тексту, наверняка)"),
        ("POSSIBLE", "выемка возможна (границы интервала или выведенные даты)"),
        ("DONE", "отработано раньше (по тексту)"), ("NOT_YET", "ещё не начато (по тексту)"),
        ("PLAN_1990", "отработано к ≈1990 (рис. 2.7 VKM-SRC-037), дата UNKNOWN"),
        ("PLAN_2007", "отработано к 01.01.2007 (схема 064), дата UNKNOWN"), ("PLANNED", "только план (ПРГР)"),
        ("UNKNOWN", "нет датированных сведений"))]
    for seam in SEAMS:
        fig, axes = plt.subplots(2, 5, figsize=(16, 7.6))
        for ax, dec in zip(axes.ravel(), DECADES):
            draw_regions(ax, regions, lambda r, d=dec, s=seam: SEAM_COLOURS[unit_state(by_region_seam.get((r["id"], s), []), d)],
                         field, f"{seam}: {dec}-е")
        fig.legend(handles=legend, loc="lower center", ncol=4, fontsize=7, frameon=False)
        fig.suptitle(f"СКРУ-1, пласт {seam}: выемка по десятилетиям (SH1, м; CRS UNKNOWN; контуры — схема VKM-SRC-014, ±15–60 м)",
                     fontsize=9)
        fig.tight_layout(rect=(0, 0.05, 1, 0.96))
        f = out / f"decades_{ {'КрII': 'KR2', 'АБ': 'AB', 'Вс': 'Vs', 'Вк': 'Vk'}[seam]}.png".replace(" ", "")
        fig.savefig(f, dpi=150)
        plt.close(fig)
        files.append(f)
    # state per seam: remaining 2013, worked 2007, text-dated
    fig, axes = plt.subplots(1, 4, figsize=(17, 5.2))
    for ax, seam in zip(axes, SEAMS):
        def col(r, s=seam):
            us = by_region_seam.get((r["id"], s), [])
            if any(u["timing_class"] in ("TEXT_DATED", "TEXT_PARTLY_DATED", "TEXT_OPEN_ENDED") for u in us):
                return "#c0392b"
            if any(u["timing_class"] == "TEXT_UNDATED" for u in us):
                return "#e59866"
            if any(u["timing_class"] == "DERIVED_DATES" for u in us):
                return "#f4d03f"
            if any("END≤1990" in u["mining_open_bounds"] for u in us):
                return "#6c3483"
            if any(u["state_2007_064"].startswith("WORKED") for u in us):
                return "#a569bd"
            if any(u["state_2013_014"].startswith("REMAINING") for u in us):
                return "#82e0aa"
            return "#f2f3f4"
        draw_regions(ax, regions, col, field, f"пласт {seam}")
    fig.legend(ncol=3, handles=[Patch(color="#c0392b", label="выемка с датами из текста"),
                        Patch(color="#e59866", label="выемка названа без дат"),
                        Patch(color="#f4d03f", label="даты выведены (DERIVATION/INTERPRETATION)"),
                        Patch(color="#6c3483", label="отработано к ≈1990 (037 рис. 2.7)"),
                        Patch(color="#a569bd", label="отработано к 01.01.2007 (064)"),
                        Patch(color="#82e0aa", label="остаток балансовых запасов (014, ≤2013)"),
                        Patch(color="#f2f3f4", label="нет сведений")], loc="lower center", fontsize=7, frameon=False)
    fig.suptitle("СКРУ-1: что известно о выемке по пластам (SH1)", fontsize=9)
    fig.tight_layout(rect=(0, 0.06, 1, 0.95))
    f = out / "seam_state_overview.png"
    fig.savefig(f, dpi=150)
    plt.close(fig)
    files.append(f)
    # coverage: number of independent sources per region (all seams)
    nsrc = defaultdict(set)
    for u, g in units:
        for rid in u["plan_region_ids"].split(";"):
            if rid and u["source_ids"]:
                nsrc[rid] |= set(u["source_ids"].split(";"))
    fig, ax = plt.subplots(figsize=(7.5, 7.5))
    pal = ["#f2f3f4", "#d6eaf8", "#85c1e9", "#2e86c1", "#1b4f72"]
    draw_regions(ax, regions, lambda r: pal[min(len(nsrc.get(r["id"], ())), 4)], field,
                 "Охват: число источников со сведениями о выемке/закладке/параметрах по контуру")
    ax.legend(handles=[Patch(color=pal[i], label=lab) for i, lab in enumerate(["0", "1", "2", "3", "4+"])], fontsize=7,
              loc="lower left", title="источников", title_fontsize=7)
    fig.tight_layout()
    f = out / "coverage_sources.png"
    fig.savefig(f, dpi=150)
    plt.close(fig)
    files.append(f)
    return files


# ----------------------------------------------------------------------------- main build
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--private", type=Path, default=Path(os.environ.get("VKM_RESOURCES_ROOT", "")))
    ap.add_argument("--stream-out", type=Path, required=True, help="PRIVATE canonical SITE_SKRU1_MINE directory")
    ap.add_argument("--delivery", type=Path, required=True, help="PRIVATE 12_work_exports/skru1_mine_history_<date>")
    ap.add_argument("--block201", type=Path, default=None)
    ap.add_argument("--geology-npz", type=Path, default=None)
    ap.add_argument("--terrain", type=Path, default=None)
    ap.add_argument("--state-1990", type=Path, default=None, help="VKM-SRC-037 fig. 2.7 worked areas ≈1990 (SH1 GeoJSON)")
    ap.add_argument("--epoch", default="2026-10-23T00:00:00Z", help="fixed GeoPackage timestamp (not an observation date)")
    ap.add_argument("--no-figures", action="store_true")
    a = ap.parse_args(argv)
    canon = a.private / "11_evidence_vnext" / "canonical"
    curated = canon / "WORLD_PARAMETERS" / "curated"
    c4map = curated / "c4_map" / "skru1"
    inputs = {}

    def pin(name, p: Path | None):
        if p and Path(p).is_file():
            inputs[name] = {"path": rel(Path(p), a.private) if str(Path(p).resolve()).startswith(str(a.private.resolve()))
                            else Path(p).name, "sha256": sha(Path(p))}

    for n in ("skru1_workings.geojson", "skru1_points.geojson", "skru1_boundaries.geojson"):
        pin("c4map/" + n, c4map / n)
    for n in ("skru1_mining_calendar_20261010.csv", "skru1_calendar_conflicts_20261010.csv", "skru1_c4_chamber_design.csv"):
        pin("curated/" + n, curated / n)
    for n in ("mining_chronology_catalog.csv", "mining_geometry_catalog.csv", "backfill_event_catalog.csv"):
        pin("MINING/" + n, canon / "MINING" / n)
    pin("WORLD_OBSERVATIONS/world_observation_catalog.csv", canon / "WORLD_OBSERVATIONS" / "world_observation_catalog.csv")
    new_ev = a.stream_out / "skru1_mine_evidence.csv"
    pin("SITE_SKRU1_MINE/skru1_mine_evidence.csv", new_ev)
    pin("geology_npz", a.geology_npz)
    pin("terrain", a.terrain)
    pin("state_1990_037", a.state_1990)
    if a.block201:
        for n in ("block201_mining_history.csv", "site_objects_sh1.csv"):
            pin("block201/" + n, a.block201 / n)
    pin("builder", Path(__file__))
    pin("library", ROOT / "src" / "vkm_world" / "mining" / "skru1_history.py")

    regions, layers, key_index, field, points, bounds = load_plan(c4map)
    if a.state_1990 and a.state_1990.is_file():
        from shapely.geometry import shape as _shape
        s90 = json.loads(a.state_1990.read_text(encoding="utf-8"))
        for f_ in s90["features"]:
            p_ = f_["properties"]
            g_ = _shape(f_["geometry"])
            layers["STATE_C1990_037"].append({"id": p_["id"], "layer": "STATE_C1990_037", "kind": p_["class"],
                                              "label": "", "parent": "", "seam": p_["seam"],
                                              "geom": g_ if g_.is_valid else g_.buffer(0), "props": p_})
    cal = read_csv(curated / "skru1_mining_calendar_20261010.csv")
    known_phase1 = {x.strip() for r in cal for x in r.get("phase1_ref", "").split(";") if x.strip()}
    evidence = (load_calendar(curated) + load_chamber_design(curated) + load_phase1(canon / "MINING", known_phase1)
                + load_new_evidence(new_ev))
    b201_hist, b201_objs = load_block201(a.block201)
    evidence += b201_hist
    units = build_units(evidence, key_index, regions, layers, zone_aliases(curated, regions),
                        {r["row_id"]: r["source_id"] for r in cal})
    geo, ter = load_fields(a.geology_npz, a.terrain)

    # --- conflicts: calendar conflicts + new interval conflicts
    conflicts = []  # the calendar conflicts CF-C4-01…10 stay in their own catalogue (evidence/world/c4)
    k = 0
    for u, g in units:
        if u["mining_conflict"] == "CHECK":
            k += 1
            cid = f"SKM-CF-A{k:03d}"
            u["conflict_ids"] = cid
            conflicts.append({"conflict_id": cid,
                              "topic": "периоды выемки одного объекта и пласта по разным источникам не перекрываются",
                              "object": u["object_label"] or u["object_key"], "seam": u["seam"],
                              "kind": "POSSIBLE_DATE_CONFLICT", "value_a": u["notes"].split(";")[0][:200],
                              "source_a": u["source_ids"], "value_b": "", "source_b": "",
                              "resolution": "UNRESOLVED — не усреднять: разные даты одного события или разные кампании",
                              "origin": "build_skru1_mine_history (автоматически)", "note": u["evidence_ids"][:300]})
    new_conf = a.stream_out / "skru1_mine_conflicts_manual.csv"
    if new_conf.is_file():
        pin("SITE_SKRU1_MINE/skru1_mine_conflicts_manual.csv", new_conf)
        for r in read_csv(new_conf):
            conflicts.append({c: r.get(c, "") for c in ("conflict_id", "topic", "object", "seam", "kind", "value_a",
                                                         "source_a", "value_b", "source_b", "resolution", "origin", "note")})

    # --- seams by area: printed seam attributes + v6_2 contacts and DSM over the unit polygons
    seam_rows = []
    for r in evidence:
        if r["attribute"] in SEAM_ATTR and r["site"] in ("SKRU1", "SKRU1_PROBABLE", "SKRU1_SKRU2_PILLAR"):
            seam_rows.append(r)
    area_keys = sorted({k for r in seam_rows for k in r["object_keys"].split(";") if k})
    sampled = {}
    for k_ in area_keys + [k for k in key_index]:
        regs = key_index.get(k_)
        if regs and (geo is not None or ter is not None) and k_ not in sampled:
            from shapely.ops import unary_union
            sampled[k_] = sample_region(unary_union([x["geom"] for x in regs]), geo, ter)
    seams_by_area = []
    for r in seam_rows:
        for k_ in [k for k in r["object_keys"].split(";") if k] or ["UNRESOLVED"]:
            s = sampled.get(k_, {})
            row = {"area_key": k_, "seam": r["seams"], "attribute": r["attribute"], "value_as_printed": r["value_as_printed"],
                   "value_min": r["value_min"], "value_max": r["value_max"], "unit": r["unit"],
                   "planned_or_actual": r["planned_or_actual"], "status": r["status"], "source_id": r["source_id"],
                   "page_id": r["page_id"], "evidence_id": r["ev_id"], "note": r["note"][:200]}
            if s:
                dsm = s.get("dsm_med")
                row.update({"dsm_med_m": f"{dsm:.1f}" if dsm is not None else "",
                            "v62_SZ_roof_elev_m": f"{s['SZ_elev_min']:.1f}…{s['SZ_elev_max']:.1f}" if "SZ_elev_min" in s else "",
                            "v62_PDKS_roof_elev_m": f"{s['PDKS_elev_min']:.1f}…{s['PDKS_elev_max']:.1f}" if "PDKS_elev_min" in s else "",
                            "v62_SKZ_roof_elev_m": f"{s['SKZ_elev_min']:.1f}…{s['SKZ_elev_max']:.1f}" if "SKZ_elev_min" in s else "",
                            "v62_support": s.get("geo_support", "")})
                if dsm is not None and "SZ_elev_min" in s:
                    sz_d = (dsm - s["SZ_elev_max"], dsm - s["SZ_elev_min"])
                    pd_d = (dsm - s["PDKS_elev_max"], dsm - s["PDKS_elev_min"])
                    row["v62_SZ_roof_depth_m"] = f"{sz_d[0]:.0f}…{sz_d[1]:.0f}"
                    row["v62_PDKS_roof_depth_m"] = f"{pd_d[0]:.0f}…{pd_d[1]:.0f}"
                    if r["attribute"] == "DEPTH" and "КрII" in r["seams"] and r["value_min"] not in ("", None):
                        d = float(r["value_min"])
                        row["check_KR2_in_SZ"] = ("PASS" if sz_d[0] - 10 <= d <= pd_d[1] + 10 else
                                                  f"FAIL ({d:g} м вне [{sz_d[0]:.0f}; {pd_d[1]:.0f}] ± 10 м)")
                    if r["attribute"] == "ROOF_ELEVATION_ABS" and "КрII" in r["seams"] and r["value_min"] not in ("", None):
                        lo, hi = float(r["value_min"]), float(r["value_max"] or r["value_min"])
                        row["check_KR2_in_SZ"] = ("PASS" if s["PDKS_elev_min"] - 10 <= lo and hi <= s["SZ_elev_max"] + 10 else
                                                  f"FAIL ({lo:g}…{hi:g} вне [{s['PDKS_elev_min']:.0f}; {s['SZ_elev_max']:.0f}] ± 10 м; "
                                                  "системы высот разные: рудник — не названа, v6_2 — EGM2008/DSM)")
            seams_by_area.append(row)
    # v6_2 contact summary per labelled plan region (the 3D frame of the seams; seams themselves not placed)
    region_frame = []
    for r in regions:
        if not r["key"] or r["key"] not in sampled:
            continue
        s = sampled[r["key"]]
        region_frame.append({"area_key": r["key"], "plan_region_id": r["id"], "dsm_med_m": round(s.get("dsm_med", float("nan")), 1),
                             **{f"v62_{c}_roof_elev_med_m": round(s[f"{c}_elev_med"], 1) for c in CONTACTS if f"{c}_elev_med" in s},
                             "v62_SZ_thickness_med_m": round(s["SZ_elev_med"] - s["PDKS_elev_med"], 1) if "SZ_elev_med" in s else "",
                             "v62_support": s.get("geo_support", ""), "geo_basis": s.get("geo_basis", ""),
                             "seam_placement": "UNKNOWN: поскважинных отбивок КрII/АБ/В по СКРУ-1 нет; толщи на доли не делятся"})

    # --- seam scenario bands over the v6_2 contacts and checks against printed seam elevations
    def band(z_sz, z_pd):
        a = SCN["A_thickness_m"][0]
        k0, k1 = SCN["KR2_roof_below_SZ_m"][:2]
        v0, v1 = SCN["V_floor_above_AB_floor_m"][:2]
        h = SCN["KR2_thickness_m"][0]
        return {"scn_AB_floor_elev_m": z_sz - a, "scn_KR2_roof_elev_lo_m": z_sz - k1, "scn_KR2_roof_elev_hi_m": z_sz - k0,
                "scn_KR2_floor_elev_lo_m": z_sz - k1 - h, "scn_KR2_floor_elev_hi_m": z_sz - k0 - h,
                "scn_V_floor_elev_lo_m": z_sz - a + v0, "scn_V_floor_elev_hi_m": z_sz - a + v1,
                "scn_KR2_floor_above_PDKS_m": (z_sz - k1 - h) - z_pd}
    for row in region_frame:
        if "v62_SZ_roof_elev_med_m" in row:
            bnd = band(row["v62_SZ_roof_elev_med_m"], row["v62_PDKS_roof_elev_med_m"])
            row.update({k: round(v, 1) for k, v in bnd.items()})
            row["scn_status"] = ("SCENARIO (ENGINEERING_ASSUMPTION): кровля СЗ v6_2 = кровля пласта А по определению; "
                                 "смещения — напечатанные средние (VKM-SRC-014, -020, -254), не отбивки СКРУ-1")
    seam_checks = []
    for r in seam_rows:
        if "КрII" not in r["seams"] or r["attribute"] not in ("ROOF_ELEVATION_ABS", "DEPTH"):
            continue
        for k_ in [k for k in r["object_keys"].split(";") if k]:
            s = sampled.get(k_)
            if not s or "SZ_elev_min" not in s or r["value_min"] in ("", None):
                continue
            lo_sz, hi_sz = s["SZ_elev_min"], s["SZ_elev_max"]
            k0, k1 = SCN["KR2_roof_below_SZ_m"][:2]
            scn_lo, scn_hi = lo_sz - k1, hi_sz - k0
            v_lo, v_hi = float(r["value_min"]), float(r["value_max"] if r["value_max"] not in ("", None) else r["value_min"])
            row = {"area_key": k_, "evidence_id": r["ev_id"], "source_id": r["source_id"], "attribute": r["attribute"],
                   "printed": r["value_as_printed"], "dsm_med_m": round(s.get("dsm_med", float("nan")), 1),
                   "v62_SZ_roof_elev_m": f"{lo_sz:.1f}…{hi_sz:.1f}", "v62_support": s.get("geo_support", "")}
            if r["attribute"] == "ROOF_ELEVATION_ABS":
                row["scn_KR2_roof_elev_m"] = f"{scn_lo:.1f}…{scn_hi:.1f}"
                gap = 0.0 if not (v_hi < scn_lo or v_lo > scn_hi) else (v_lo - scn_hi if v_lo > scn_hi else v_hi - scn_lo)
                row["printed_minus_scenario_m"] = round(gap, 1)
                row["verdict"] = ("CONSISTENT" if abs(gap) <= 10 else
                                  ("PRINTED_HIGHER" if gap > 0 else "PRINTED_LOWER"))
            else:
                dsm = s.get("dsm_med")
                row["implied_KR2_roof_elev_from_depth_m"] = f"{dsm - v_hi:.1f}…{dsm - v_lo:.1f}" if dsm is not None else ""
                row["verdict"] = "SEE_ROOF_ROWS"
            row["note"] = ("системы высот: рудника — не названа; v6_2 — DSM/EGM2008 со сценарным колонковым якорем; "
                           "разность — диагностика, не поправка")
            seam_checks.append(row)
    # depth vs (DSM − printed roof elevation) for the same areas (internal consistency of PRGR §1 against the DSM)
    roof_by_area = defaultdict(list)
    depth_by_area = defaultdict(list)
    for r in seam_rows:
        if "КрII" in r["seams"] and r["value_min"] not in ("", None):
            for k_ in [k for k in r["object_keys"].split(";") if k]:
                if r["attribute"] == "ROOF_ELEVATION_ABS":
                    roof_by_area[k_].append((float(r["value_min"]), float(r["value_max"] or r["value_min"]), r["ev_id"]))
                elif r["attribute"] == "DEPTH" and r["source_id"] == "VKM-SRC-252":
                    depth_by_area[k_].append((float(r["value_min"]), r["ev_id"]))
    depth_checks = []
    for k_ in sorted(set(roof_by_area) & set(depth_by_area)):
        s = sampled.get(k_, {})
        if "dsm_med" not in s:
            continue
        for lo, hi, e1 in roof_by_area[k_]:
            for d, e2 in depth_by_area[k_]:
                implied_surface = (lo + d, hi + d)
                depth_checks.append({"area_key": k_, "roof_evidence": e1, "depth_evidence": e2,
                                     "printed_roof_elev_m": f"{lo:g}…{hi:g}", "printed_depth_m": d,
                                     "implied_surface_m": f"{implied_surface[0]:.0f}…{implied_surface[1]:.0f}",
                                     "dsm_med_m": round(s["dsm_med"], 1),
                                     "dsm_minus_implied_mid_m": round(s["dsm_med"] - sum(implied_surface) / 2, 1)})
    # field level: printed field means (VKM-SRC-254) against the v6_2 scenario over the labelled plan regions
    import numpy as np
    from shapely.ops import unary_union
    field_level = {}
    if geo is not None:
        from shapely import contains_xy
        labelled = unary_union([r["geom"] for r in regions if r["key"]])
        inside = contains_xy(labelled, geo["xy"][:, 0], geo["xy"][:, 1])
        sz = geo["c"][inside, CONTACTS.index("SZ")]
        a_th = SCN["A_thickness_m"][0]
        k0, k1 = SCN["KR2_roof_below_SZ_m"][:2]
        h = SCN["KR2_thickness_m"][0]
        v0, v1 = SCN["V_floor_above_AB_floor_m"][:2]
        med = float(np.median(sz))
        field_level = {
            "v62_SZ_roof_elev_median_m": round(med, 1), "v62_SZ_roof_elev_p10_p90_m": [round(float(np.percentile(sz, 10)), 1),
                                                                                       round(float(np.percentile(sz, 90)), 1)],
            "n_nodes": int(inside.sum()), "support_mask": dict(Counter(str(x) for x in geo["mask"][inside])),
            "АБ_floor": {"printed_field_mean_m": FIELD_MEANS["АБ"], "scenario_m": round(med - a_th, 1),
                        "printed_minus_scenario_m": round(FIELD_MEANS["АБ"] - (med - a_th), 1)},
            "КрII_floor": {"printed_field_mean_m": FIELD_MEANS["КрII"],
                          "scenario_m": [round(med - k1 - h, 1), round(med - k0 - h, 1)],
                          "printed_minus_scenario_m": [round(FIELD_MEANS["КрII"] - (med - k0 - h), 1),
                                                       round(FIELD_MEANS["КрII"] - (med - k1 - h), 1)]},
            "В_floor": {"printed_field_mean_m": FIELD_MEANS["В"], "scenario_m": [round(med - a_th + v0, 1), round(med - a_th + v1, 1)],
                       "printed_minus_scenario_m": [round(FIELD_MEANS["В"] - (med - a_th + v1), 1),
                                                    round(FIELD_MEANS["В"] - (med - a_th + v0), 1)]},
            "caveat": "средние по ~1100–1700 скважинам предприятия (СКО 46–53 м) против медианы узлов v6_2 по подписанным "
                      "областям плана; системы высот разные (рудник / EGM2008+DSM); диагностика смещения, не поправка",
            "scenario_sources": {k: v[2] for k, v in SCN.items()}}

    # --- observations index
    obs = []
    for r in read_csv(canon / "WORLD_OBSERVATIONS" / "world_observation_catalog.csv"):
        if r["site_norm"] not in ("SKRU1", "SKRU1_SKRU2_PILLAR"):
            continue
        keys, _ = normalize_objects(" ".join((r["notes"], r["site_as_printed"], r["line_or_benchmark"])))
        obs.append({"obs_id": r["obs_id"], "origin": "WORLD_OBSERVATIONS", "item_kind": r["item_kind"],
                    "modality": r["modality"], "line_or_benchmark": r["line_or_benchmark"], "epochs": r["epochs"],
                    "time_start": r["time_start"], "time_end": r["time_end"], "quantity": r["quantity"][:120],
                    "value_min": r["value_min"], "value_max": r["value_max"], "unit": r["unit"],
                    "available_from": r["available_from"], "site_norm": r["site_norm"], "status": r["status"],
                    "source_id": r["source_id"], "page_id": r["page_id"], "linked_objects_text": ";".join(keys),
                    "usable_for_validation": r["usable_for_validation"], "note": r["notes"][:200]})
    if new_ev.is_file():
        for r in read_csv(new_ev):
            if r["attribute"] in ("OBS_LINE", "OBS_LINE_LENGTH", "OBS_EPOCH", "OBS_METHOD", "MAX_SUBSIDENCE",
                                  "SUBSIDENCE_RATE", "BENCHMARK_COUNT", "INSAR_EPOCH"):
                keys, _ = normalize_objects(r["note"] + " " + r["object_norm"])
                obs.append({"obs_id": r["evidence_id"], "origin": "SITE_SKRU1_MINE", "item_kind": r["attribute"],
                            "modality": "", "line_or_benchmark": r["object_norm"], "epochs": r["date_start_as_printed"],
                            "time_start": r["date_start_min"], "time_end": r["date_end_max"],
                            "quantity": r["value_as_printed"][:120], "value_min": r["value_min"], "value_max": r["value_max"],
                            "unit": r["unit"], "available_from": r.get("available_from", "") or (
                                re.search(r"available_from\s*=\s*(\d{4})", r["note"]).group(1)
                                if re.search(r"available_from\s*=\s*(\d{4})", r["note"]) else ""),
                            "site_norm": r["site_attribution"],
                            "status": r["status"], "source_id": r["source_id"], "page_id": r["page_id"],
                            "linked_objects_text": ";".join(keys), "usable_for_validation": "", "note": r["note"][:200]})
    unit_by_key = defaultdict(list)
    for u, g in units:
        unit_by_key[u["object_key"]].append(u)
    for o in obs:
        t = yr(o["time_start"]) or yr(o["epochs"])
        mined_before = []
        for k_ in [x for x in o["linked_objects_text"].split(";") if x]:
            for u in unit_by_key.get(k_, []):
                if u["mining_start_min"] not in ("", None) and t is not None:
                    st = "до эпохи" if int(u["mining_end_max"] or 9999) <= t else (
                        "частично до эпохи" if int(u["mining_start_min"]) <= t else "после эпохи")
                    mined_before.append(f"{k_}/{u['seam']}: {u['mining_start_min']}–{u['mining_end_max']} ({st})")
        o["mining_vs_epoch"] = " | ".join(mined_before)[:400]

    # --- checks
    from shapely.ops import unary_union
    checks = {"field_area_m2": round(field.area), "n_regions": len(regions), "n_units": len(units),
              "timing_class_counts": dict(Counter(u["timing_class"] for u, _ in units if u["seam"] in SEAMS))}
    # interval sanity
    bad = []
    for u, g in units:
        vals = [u[c] for c in ("mining_start_min", "mining_start_max", "mining_end_min", "mining_end_max")]
        if all(v not in ("", None) for v in vals):
            a0, a1, b0, b1 = (int(v) for v in vals)
            if not (a0 <= a1 and b0 <= b1 and a0 <= b1):
                bad.append(u["unit_id"])
    checks["interval_order_errors"] = bad
    # same seam overlap of certainly simultaneous units
    ulist = []
    for u, g in units:
        if g is not None and u["mining_core"]:
            for p in u["mining_core"].split(";"):
                c0, c1 = (int(x) for x in p.split("–"))
                ulist.append({"id": u["unit_id"] + f"@{c0}-{c1}", "seam": u["seam"], "geom": g, "core": (c0, c1)})
    checks["same_seam_simultaneous_overlaps"] = overlapping_same_seam(ulist)
    # inside the field
    outside = []
    for u, g in units:
        if g is not None:
            out_area = g.difference(field.buffer(1.0)).area
            if out_area > 100:
                outside.append({"unit": u["unit_id"], "outside_m2": round(out_area)})
    checks["units_outside_field"] = outside
    # areas by seam: remaining 2013 (014) and worked 2007 (064)
    rem = defaultdict(float)
    for x in layers.get("REMAINING_RESERVES_2013_014", []):
        rem[x["seam"]] += x["geom"].area
    w07 = defaultdict(float)
    for x in layers.get("WORKED_OUT_2007_064", []):
        w07[x["seam"]] += x["geom"].area
    checks["plan_areas_km2"] = {"remaining_2013_014": {k: round(v / 1e6, 3) for k, v in rem.items()},
                                "worked_2007_064_east_only": {k: round(v / 1e6, 3) for k, v in w07.items()},
                                "field_layout_014": round(sum(r["geom"].area for r in regions) / 1e6, 3)}
    # reserves cross-check: remaining area × thickness × density vs balance reserves 01.01.2012 (SRC-252)
    bal = {"КрII": 141465, "АБ": 81688, "Вс": 6315, "Вк": 167696}
    thick = {"КрII": (5.1, 6.3)}
    dens = {"КрII": (2.088, 2.092)}
    xr = {}
    for s_, kt in bal.items():
        key = {"КрII": "КрII", "АБ": "АБ", "Вс": "В (сильвинитовый, Вс)", "Вк": "В (карналлитовый, Вк)"}[s_]
        area = rem.get(key, 0.0)
        row = {"balance_reserves_kt_20120101": kt, "remaining_area_014_km2": round(area / 1e6, 3)}
        if s_ in thick and area:
            lo = area * thick[s_][0] * dens[s_][0] / 1e3
            hi = area * thick[s_][1] * dens[s_][1] / 1e3
            row.update(in_place_from_area_kt=f"{lo:.0f}–{hi:.0f}", ratio_reserves_to_area_tonnage=f"{kt / hi:.2f}–{kt / lo:.2f}",
                       basis="DERIVATION: площадь штриховки 014 × мощность 5,1–6,3 м × плотность 2,088–2,092 т/м³ "
                             "(ГУ-2, ПРГР 2013 — перенос на всё поле = ENGINEERING_ASSUMPTION); балансовые запасы включают "
                             "запасы в целиках и под охраняемыми объектами, поэтому отношение > 1 ожидаемо")
        xr[s_] = row
    checks["reserves_vs_plan_area"] = xr
    # 2013 plan per block: extinguished reserves vs in-place tonnage of the block polygon
    pb = []
    for r in evidence:
        if r["origin"] == "SKM" and "RESERVES_EXTINGUISHED_PLAN_2013" in r["note"]:
            for k_ in r["object_keys"].split(";"):
                regs = key_index.get(k_)
                if regs and r["value_min"] not in ("", None):
                    area = sum(x["geom"].area for x in regs)
                    tonnes = float(r["value_min"])
                    inplace = (area * 5.1 * 2.088, area * 6.3 * 2.092)
                    pb.append({"object": k_, "extinguished_plan_2013_t": tonnes, "plan_area_m2": round(area),
                               "area_equivalent_m2": f"{tonnes / (6.3 * 2.092):.0f}–{tonnes / (5.1 * 2.088):.0f}",
                               "share_of_block_area": f"{tonnes / inplace[1]:.3f}–{tonnes / inplace[0]:.3f}",
                               "verdict": ("WARN: за один год погашается > 50 % площади контура 014 — границы блока на "
                                           "схеме 014 и в ПРГР, вероятно, не совпадают (или «Бл.49-51» — общий участок)"
                                           if tonnes / inplace[1] > 0.5 else "PLAUSIBLE")})
    checks["plan2013_extinguished_vs_block_area"] = pb
    checks["seam_field_means_vs_v62_scenario"] = field_level
    worked22 = {}
    for r in evidence:
        if (r["origin"] == "SKM" and r["attribute"] == "WORKED_AREA" and r["status"] == "FACT" and r["object_keys"] == "MINE"
                and r["source_id"] == "VKM-SRC-074" and ";" not in r["seams"] and r["page_id"].endswith("p0006")):
            worked22[r["seams"]] = float(r["value_min"])
    remk = {"КрII": "КрII", "АБ": "АБ", "Вс": "В (сильвинитовый, Вс)", "Вк": "В (карналлитовый, Вк)"}
    lay = sum(r["geom"].area for r in regions) / 1e6
    wa = {}
    for s_, w in worked22.items():
        rm = rem.get(remk.get(s_, ""), 0.0) / 1e6
        wa[s_] = {"worked_2022_074_km2": w, "remaining_2013_014_km2": round(rm, 3), "field_layout_014_km2": round(lay, 3),
                  "worked_plus_remaining_km2": round(w + rm, 3),
                  "check_worked_plus_remaining_le_layout": "PASS" if w + rm <= lay else "FAIL"}
    checks["worked_areas_2022_vs_plan"] = wa
    s90 = layers.get("STATE_C1990_037", [])
    if s90:
        from shapely.ops import unary_union as _uu
        w90 = {k: _uu([x["geom"] for x in s90 if x["kind"] == "WORKED_C1990" and x["seam"] == k])
               for k in ("КрII", "АБ", "В (Вс|Вк)")}
        w07u = _uu([x["geom"] for x in layers.get("WORKED_OUT_2007_064", [])])
        checks["worked_c1990_037"] = {
            k: {"area_km2": round(g_.area / 1e6, 3), "outside_field_km2": round(g_.difference(field).area / 1e6, 3),
                "overlap_with_064_worked_2007_km2": round(g_.intersection(w07u).area / 1e6, 3),
                "worked_2022_074_km2": worked22.get({"КрII": "КрII", "АБ": "АБ"}.get(k, ""), None)}
            for k, g_ in w90.items()}
        pre, post = [], []
        for u_, g_ in units:
            m_ = re.match(r"(?:WORKED_[^_]+(?:\(Вс\|Вк\))?|NOT_SHOWN_WORKED)_(\d\.\d+)", u_.get("state_1990_037") or "")
            if (not m_ or u_["seam"] not in ("КрII", "АБ", "Вк") or u_["plan_match"] == "MINE_LEVEL"
                    or u_["timing_class"] not in ("TEXT_DATED", "TEXT_PARTLY_DATED", "TEXT_OPEN_ENDED", "DERIVED_DATES")
                    or u_["mining_end_max"] in ("", None)):
                continue
            row_ = {"unit": u_["unit_id"], "timing_class": u_["timing_class"], "years": f"{u_['mining_start_min']}–{u_['mining_end_max']}",
                    "worked_1990_frac": float(m_.group(1))}
            if int(u_["mining_end_max"]) <= 1989:
                pre.append(row_)
            elif int(u_["mining_start_min"] or 0) >= 1991:
                post.append(row_)
        checks["fig2_7_vs_text_dates"] = {
            "mined_before_1990_by_text": len(pre), "before_worked_ge_0_5": sum(r["worked_1990_frac"] >= 0.5 for r in pre),
            "before_worked_ge_0_2": sum(r["worked_1990_frac"] >= 0.2 for r in pre),
            "mined_after_1990_by_text": len(post), "after_worked_ge_0_5": sum(r["worked_1990_frac"] >= 0.5 for r in post),
            "disagreements": [r for r in pre if r["worked_1990_frac"] < 0.5] + [r for r in post if r["worked_1990_frac"] >= 0.5],
            "note": "независимая проверка оцифровки и привязки рис. 2.7: даты из текста и выведенные даты не использовались при выделении"}
        checks["worked_c1990_037"]["note"] = (
            "площади штриховки «отработанные запасы» рис. 2.7 — брутто-контуры участков (с целиками и неотработанными "
            "пятнами); с площадями VKM-SRC-074 (01.2022) напрямую не сравнимы — конфликт SKM-CF-M11")
    # chronology via vkm_world models
    checks["worldspec"] = build_worldspec(units, a.delivery / "data" / "worldspec_mining_layer.json")

    # --- write canonical stream tables (no coordinates)
    a.stream_out.mkdir(parents=True, exist_ok=True)
    unit_rows = [u for u, g in units]
    write_csv(a.stream_out / "skru1_mining_units.csv", unit_rows, UNIT_COLS)
    for i, r in enumerate(seams_by_area, 1):
        r["seam_row_id"] = f"SKS-{i:04d}"
    for r in region_frame:
        r["frame_id"] = "SKF-" + r["plan_region_id"].replace("C4MAP-014-", "")
    for i, r in enumerate(seam_checks, 1):
        r["check_id"] = f"SKC-P{i:03d}"
    for i, r in enumerate(depth_checks, 1):
        r["check_id"] = f"SKC-D{i:03d}"
    sba_cols = ["seam_row_id", "area_key", "seam", "attribute", "value_as_printed", "value_min", "value_max", "unit",
                "planned_or_actual", "status", "source_id", "page_id", "evidence_id", "dsm_med_m", "v62_SKZ_roof_elev_m",
                "v62_SZ_roof_elev_m", "v62_PDKS_roof_elev_m", "v62_SZ_roof_depth_m", "v62_PDKS_roof_depth_m",
                "v62_support", "check_KR2_in_SZ", "note"]
    write_csv(a.stream_out / "skru1_seams_by_area.csv", seams_by_area, sba_cols)
    rf_cols = ["frame_id", "area_key", "plan_region_id", "dsm_med_m"] + [f"v62_{c}_roof_elev_med_m" for c in CONTACTS] + [
        "v62_SZ_thickness_med_m", "v62_support", "geo_basis", "seam_placement"]
    rf_cols2 = rf_cols + ["scn_AB_floor_elev_m", "scn_KR2_roof_elev_lo_m", "scn_KR2_roof_elev_hi_m", "scn_KR2_floor_elev_lo_m",
                          "scn_KR2_floor_elev_hi_m", "scn_V_floor_elev_lo_m", "scn_V_floor_elev_hi_m",
                          "scn_KR2_floor_above_PDKS_m", "scn_status"]
    write_csv(a.stream_out / "skru1_region_geology_frame.csv", region_frame, rf_cols2)
    write_csv(a.stream_out / "skru1_seam_position_checks.csv", seam_checks,
              ["check_id", "area_key", "evidence_id", "source_id", "attribute", "printed", "dsm_med_m", "v62_SZ_roof_elev_m",
               "scn_KR2_roof_elev_m", "printed_minus_scenario_m", "implied_KR2_roof_elev_from_depth_m", "verdict",
               "v62_support", "note"])
    write_csv(a.stream_out / "skru1_depth_vs_dsm_checks.csv", depth_checks,
              ["check_id", "area_key", "roof_evidence", "depth_evidence", "printed_roof_elev_m", "printed_depth_m", "implied_surface_m",
               "dsm_med_m", "dsm_minus_implied_mid_m"])
    for i, o in enumerate(obs, 1):
        o["obs_index_id"] = f"SKO-{i:04d}"
    obs_cols = ["obs_index_id", "obs_id", "origin", "item_kind", "modality", "line_or_benchmark", "epochs", "time_start", "time_end",
                "quantity", "value_min", "value_max", "unit", "available_from", "site_norm", "status", "source_id",
                "page_id", "linked_objects_text", "mining_vs_epoch", "usable_for_validation", "note"]
    write_csv(a.stream_out / "skru1_observation_index.csv", obs, obs_cols)
    cf_cols = ["conflict_id", "topic", "object", "seam", "kind", "value_a", "source_a", "value_b", "source_b",
               "resolution", "origin", "note"]
    write_csv(a.stream_out / "skru1_mine_conflicts.csv", conflicts, cf_cols)
    gaps = gap_table(units, regions)
    checks_seam_summary = Counter(r["verdict"] for r in seam_checks)
    checks["seam_position_verdicts"] = dict(checks_seam_summary)
    for i, g in enumerate(gaps, 1):
        g["gap_id"] = f"SKG-{i:02d}"
    write_csv(a.stream_out / "skru1_coverage_gaps.csv", gaps, ["gap_id", "topic", "scope", "n_known", "n_total", "share",
                                                               "what_is_missing"])
    (a.stream_out / "skru1_mine_checks.json").write_text(json.dumps(checks, ensure_ascii=False, indent=1, default=str) + "\n",
                                                         encoding="utf-8")
    ev_cols = EV_COLS
    write_csv(a.delivery / "data" / "evidence_rows_all_origins.csv", evidence, ev_cols)

    # --- delivery: GeoPackage + CSV with geometry + figures
    d = a.delivery
    (d / "data").mkdir(parents=True, exist_ok=True)
    unit_feats = []
    for u, g in units:
        if g is not None:
            unit_feats.append({**u, "geom": g})
    region_feats = [{"region_id": r["id"], "kind": r["kind"], "label": r["label"], "parent": r["parent"],
                     "object_key": r["key"] or "", "area_m2": round(r["geom"].area),
                     "pos_err_m": "{:.0f}–{:.0f}".format(*region_error(r["geom"].centroid.x)[:2]),
                     "pos_err_basis": region_error(r["geom"].centroid.x)[2], "geom": r["geom"]} for r in regions]
    layer_feats = []
    for lname in ("REMAINING_RESERVES_2013_014", "WORKED_OUT_2007_064", "PROTECTIVE_PILLAR_STIPPLE_014",
                  "PLANNED_064", "ATTACHED_AREA_064", "OFFBALANCE_OR_OVERLAP_064", "STATE_C1990_037"):
        for x in layers.get(lname, []):
            layer_feats.append({"feature_id": x["id"], "layer": lname if lname != "STATE_C1990_037" else f"{lname}:{x['kind']}",
                                "seam": x["seam"],
                                "state_date": x["props"].get("state_date_as_printed", ""),
                                "meaning": x["props"].get("meaning_as_printed", ""), "source_id": x["props"]["source_id"],
                                "page_id": x["props"]["page_id"], "status": x["props"]["status"],
                                "area_m2": round(x["geom"].area), "geom": x["geom"]})
    from shapely.geometry import shape
    pt_feats = []
    for f in points:
        p = f["properties"]
        if p.get("kind") in ("SHAFT", "BOREHOLE_COMMON", "ZONE_CENTER") or "SHAFT" in p.get("kind", "") or \
                p.get("kind", "").startswith("BOREHOLE") or p.get("kind", "").startswith("ZONE"):
            pt_feats.append({"point_id": p["id"], "kind": p.get("kind", ""), "label": p.get("label_as_printed", ""),
                             "object_id": p.get("object_id", ""), "source_id": p.get("source_id", ""),
                             "status": p.get("status", ""), "geom": shape(f["geometry"])})
    for o in b201_objs:
        try:
            from shapely import wkt as _wkt
            g = _wkt.loads(o["wkt_sh1"])
        except Exception:  # noqa: BLE001 - optional input
            continue
        pt_feats.append({"point_id": o["record_id"], "kind": o["feature_kind"], "label": o["label"], "object_id": "",
                         "source_id": o["source_id"], "status": o["status"], "geom": g})
    bnd_feats = [{"boundary_id": f["properties"]["id"], "geom": shape(f["geometry"]),
                  "meaning": f["properties"].get("meaning_as_printed", "") or f["properties"].get("note", "")} for f in bounds]
    meta = {"mining_units": "Объект × пласт: интервалы выемки/закладки как границы годов; DERIVATION/FACT по столбцу status",
            "plan_regions_014": "Раскройка поля (VKM-SRC-014 рис. 3.1–3.3) в SH1; ошибки положения pos_err_m",
            "plan_states": "Состояния схем: отработано к ≈1990 (037 рис. 2.7), отработано к 01.01.2007 (064), остаток 2013 (014), целики",
            "points": "Стволы, знаки скважин, центры зон ВКР, реперы ПЛ 12 (если дан --block201)",
            "boundaries": "Границы горного отвода и линии схем"}
    unit_cols = [c for c in UNIT_COLS if c != "evidence_ids"] + ["evidence_ids"]
    gp_layers = {
        "mining_units": ("MULTIPOLYGON", [{**u, "geom": _multi(u["geom"])} for u in unit_feats], unit_cols),
        "plan_regions_014": ("MULTIPOLYGON", [{**r, "geom": _multi(r["geom"])} for r in region_feats],
                             ["region_id", "kind", "label", "parent", "object_key", "area_m2", "pos_err_m", "pos_err_basis"]),
        "plan_states": ("MULTIPOLYGON", [{**r, "geom": _multi(r["geom"])} for r in layer_feats],
                        ["feature_id", "layer", "seam", "state_date", "meaning", "source_id", "page_id", "status", "area_m2"]),
        "points": ("POINT", pt_feats, ["point_id", "kind", "label", "object_id", "source_id", "status"]),
        "boundaries": ("LINESTRING", bnd_feats, ["boundary_id", "meaning"]),
    }
    gpkg = d / "data" / "skru1_mine_history_sh1.gpkg"
    write_gpkg(gpkg, gp_layers, a.epoch, meta)
    (d / "data" / "SKRU1_LOCAL_SH1.wkt").write_text(LOCAL_WKT + "\n", encoding="utf-8")
    write_csv(d / "data" / "mining_units_with_geometry.csv",
              [{**u, "wkt_sh1": u["geom"].wkt, "centroid_x": round(u["geom"].centroid.x, 1),
                "centroid_y": round(u["geom"].centroid.y, 1)} for u in unit_feats],
              UNIT_COLS + ["centroid_x", "centroid_y", "wkt_sh1"])
    figs = [] if a.no_figures else make_figures(d / "figures", regions, units, layers, field)
    outputs = {}
    for base in (a.stream_out, d):
        for p in sorted(base.rglob("*")):
            if p.is_file() and p.name != "receipt.json" and "_work" not in p.parts:
                outputs[rel(p, a.private)] = sha(p)
    receipt = {"schema": "vkm.skru1_mine_history/1", "command": [Path(sys.argv[0]).name] + [
        x if not os.path.isabs(x) else "<path>" for x in sys.argv[1:]], "inputs": inputs,
        "geology_version": geo["version"] if geo else None, "terrain_id": ter["id"] if ter else None,
        "counts": {"evidence_rows": len(evidence), "by_origin": dict(Counter(r["origin"] for r in evidence)),
                   "units": len(units), "conflicts": len(conflicts), "observations": len(obs),
                   "seams_by_area_rows": len(seams_by_area), "figures": len(figs)},
        "checks_summary": {"interval_order_errors": len(checks["interval_order_errors"]),
                           "same_seam_simultaneous_overlaps": len(checks["same_seam_simultaneous_overlaps"]),
                           "units_outside_field": len(checks["units_outside_field"]),
                           "worldspec_errors": len(checks["worldspec"].get("errors", []))},
        "coordinate_frame": SH1, "field_validation": False, "outputs_sha256": outputs}
    for base in (a.stream_out, d):
        (base / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({"units": len(units), "evidence": len(evidence), "conflicts": len(conflicts), "obs": len(obs),
                      "checks": receipt["checks_summary"]}, ensure_ascii=False))
    return 0


def _multi(g):
    from shapely.geometry import MultiPolygon
    if g is None:
        return None
    if g.geom_type == "Polygon":
        return MultiPolygon([g])
    if g.geom_type == "MultiPolygon":
        return g
    polys = [x for x in getattr(g, "geoms", []) if x.geom_type == "Polygon"]
    return MultiPolygon(polys) if polys else None


def gap_table(units, regions):
    rows = []
    lab = [u for u, g in units if u["plan_match"] == "LABEL_ON_PLAN_014" and u["seam"] in SEAMS]
    for s in SEAMS:
        us = [u for u in lab if u["seam"] == s]
        dated = [u for u in us if u["timing_class"] in ("TEXT_DATED", "TEXT_PARTLY_DATED")]
        rows.append({"topic": "даты выемки по тексту", "scope": f"подписанные области плана × {s}", "n_known": len(dated),
                     "n_total": len(us), "share": f"{len(dated) / len(us):.3f}" if us else "",
                     "what_is_missing": "годы начала/окончания выемки пласта по блоку/панели (план горных работ рудника)"})
        params = [u for u in us if u["chamber_width_m"] or u["pillar_width_m"]]
        rows.append({"topic": "параметры камер и целиков", "scope": f"подписанные области × {s}", "n_known": len(params),
                     "n_total": len(us), "share": f"{len(params) / len(us):.3f}" if us else "",
                     "what_is_missing": "ширины камер/целиков, вынимаемая мощность по блоку"})
        b90 = [u for u in us if "END≤1990" in u["mining_open_bounds"] and u["timing_class"] == "PLAN_SNAPSHOT_ONLY"]
        rows.append({"topic": "только граница «отработано к ≈1990» (рис. 2.7 VKM-SRC-037)", "scope": f"подписанные области × {s}",
                     "n_known": len(b90), "n_total": len(us), "share": f"{len(b90) / len(us):.3f}" if us else "",
                     "what_is_missing": "годы выемки внутри интервала [начало поля; 1990]"})
        bf = [u for u in us if u["backfill_type"]]
        rows.append({"topic": "закладка", "scope": f"подписанные области × {s}", "n_known": len(bf), "n_total": len(us),
                     "share": f"{len(bf) / len(us):.3f}" if us else "",
                     "what_is_missing": "тип, годы и полнота закладки по блоку"})
    unl = sum(1 for r in regions if not r["key"])
    rows.append({"topic": "безымянные области плана", "scope": "раскройка 014", "n_known": len(regions) - unl,
                 "n_total": len(regions), "share": f"{(len(regions) - unl) / len(regions):.3f}",
                 "what_is_missing": "подпись блока/панели (обрезки, полосы целиков, промежутки)"})
    return rows


def build_worldspec(units, out_path: Path) -> dict:
    """Mining objects and extraction events as vkm_world models; validated by the chronology rules."""
    from vkm_world.chronology.events import Event, EventType, chronology_errors
    from vkm_world.core.provenance import (EpistemicStatus, EvidenceType, Provenance, Scale, Scope, SourceRef,
                                           TemporalSupport)
    from vkm_world.mining.objects import DesignOrActual, MiningObject, MiningObjectKind, mining_object_errors
    objs, events = [], []
    for u, g in units:
        if u["seam"] not in SEAMS or u["timing_class"] in ("UNKNOWN",):
            continue
        srcs = tuple(SourceRef(source_id=s) for s in u["source_ids"].split(";") if s.startswith(("VKM-SRC", "EXT"))) or ()
        oid = u["unit_id"].replace("?", "Q")
        prov = Provenance(status=EpistemicStatus.DERIVATION if srcs else EpistemicStatus.UNKNOWN, sources=srcs,
                          evidence_type=EvidenceType.DESCRIPTIVE if srcs else EvidenceType.NOT_APPLICABLE,
                          scope=Scope.SKRU1, scale=Scale.NOT_APPLICABLE,
                          method="build_skru1_mine_history: объект × пласт, контур плана 014 в SH1" if srcs else None)
        objs.append(MiningObject(id=oid, name=f"{u['object_label']} / {u['seam']}", provenance=prov,
                                 kind=MiningObjectKind.MINED_AREA,
                                 design_or_actual={"ACTUAL": DesignOrActual.ACTUAL, "PLANNED": DesignOrActual.DESIGN,
                                                   "DESIGN": DesignOrActual.DESIGN}.get(u["design_or_actual"],
                                                                                        DesignOrActual.UNKNOWN),
                                 seams=(u["seam"],), mining_system=u["system_as_printed"][:200] or None,
                                 geometry_ref=f"skru1_mine_history_sh1.gpkg:mining_units:{u['unit_id']}"))
        if u["timing_class"] in ("TEXT_DATED", "TEXT_PARTLY_DATED", "TEXT_OPEN_ENDED") and srcs:
            spans = [tuple(int(x) for x in p.split("–")) for p in u["mining_core"].split(";") if p]
            if not spans or u["mining_open_bounds"]:
                # open-ended records: one envelope event so that backfill/commissioning order is checked against it
                spans = [(int(u["mining_start_min"]), int(u["mining_end_max"]))] + spans
            for n, (s0, e1) in enumerate(spans, 1):
                events.append(Event(id=f"{oid}:EXTRACTION:{n}", event_type=EventType.EXTRACTION_PERIOD, objects=(oid,),
                                    time=TemporalSupport(event_date=date(s0, 1, 1), event_date_end=date(e1, 12, 31),
                                                         precision="year",
                                                         notes=f"период {s0}–{e1} по тексту; открытые записи: "
                                                               f"{u['mining_open_bounds'] or 'нет'}"),
                                    provenance=Provenance(status=EpistemicStatus.FACT, sources=srcs,
                                                          evidence_type=EvidenceType.DESCRIPTIVE, scope=Scope.SKRU1,
                                                          scale=Scale.NOT_APPLICABLE)))
            if u["backfill_start_min"] not in ("", None):
                b0 = int(u["backfill_start_min"])
                b1 = int(u["backfill_end_max"]) if u["backfill_end_max"] not in ("", None) else None
                events.append(Event(id=oid + ":BACKFILL", event_type=EventType.BACKFILL_PERIOD, objects=(oid,),
                                    time=TemporalSupport(event_date=date(b0, 1, 1),
                                                         event_date_end=date(b1, 12, 31) if b1 and b1 >= b0 else None,
                                                         precision="year"),
                                    provenance=Provenance(status=EpistemicStatus.FACT, sources=srcs,
                                                          evidence_type=EvidenceType.DESCRIPTIVE, scope=Scope.SKRU1,
                                                          scale=Scale.NOT_APPLICABLE)))
    errs = mining_object_errors(objs) + chronology_errors(events, known_ids={o.id for o in objs})
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"schema": "vkm.worldspec.mining_layer/1", "frame": SH1,
                                    "mining_objects": [o.model_dump(mode="json", exclude_none=True) for o in objs],
                                    "events": [e.model_dump(mode="json", exclude_none=True) for e in events],
                                    "validation_errors": errs}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return {"mining_objects": len(objs), "events": len(events), "errors": errs}


if __name__ == "__main__":
    raise SystemExit(main())

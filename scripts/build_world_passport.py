#!/usr/bin/env python3
"""World passport, stage 2 (09.10.2026): the registry of parameters and laws of the physical worlds (modules C1–C4)
from all evidence producers, with provenance, scale, site, the LAB → MASSIF branch and the status for the world.

Inputs (PRIVATE ``11_evidence_vnext/canonical/``):
- ``WORLD_PARAMETERS/world_parameter_evidence.csv``, ``world_c1_thickness_evidence.csv`` — Phase-1 catalogues in SI
  (stage 1, ``scripts/build_world_parameters.py``);
- ``WORLD_PARAMETERS/curated/`` — SKRU-1 sheets (C1 column, C4 chamber design and extraction history), the C3 sheet
  (``c3_*``), the creep-law / transfer-branch / backfill sheets (``c2_*``, ``c4_backfill_parameters.csv``);
- ``WORLD_EXTRACTION/<run>/accepted.jsonl`` — page extraction that passed the verifier (labelled producer GPT-6.1 Sol
  and the independent double entry), AUTO_EXTRACTED_UNREVIEWED;
- ``MINING/spatial_hierarchy.csv``, ``GEOLOGY_COORDS/stratigraphic_units.csv``, Phase-1 conflict tables;
- optional ``--nav`` (NAV page export) for the bibliography entries behind cited numbers.

Outputs (``WORLD_PASSPORT/``): ``passport_evidence.csv`` (one row per number, all producers, SI value, class,
branch, copy cluster, double-entry agreement; ``passport_evidence_c1.csv`` … ``_c4.csv`` — the rows of each module
behind the registry, the views published to PUBLIC), ``world_passport.csv`` (the registry), ``passport_conflicts.csv``,
``world_objects.csv``, ``cited_literature.csv``, ``build_receipt.json``.

Rules: SI conversion and material classes are imported from ``build_world_parameters.py`` (not copied). Numbers are
copied, never invented; a parameter without evidence is a row with status UNKNOWN. LAB values are branch A (needs a
scale factor), calibrated / back-analysed / massif values branch B, norms branch N — never merged into one number.
Copies of one number (same value, unit, parameter and class in several sources) form a cluster: the ORIGINAL row is
primary, CITED rows are copies. Conflicts are recorded, not averaged.

Usage:  VKM_RESOURCES_ROOT=<PRIVATE> python scripts/build_world_passport.py --runs high_full double [--nav <db>]
        [--check]
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import io
import json
import os
import re
import statistics
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

csv.field_size_limit(sys.maxsize)
ROOT = Path(__file__).resolve().parents[1]
RULE_VERSION = "world_passport_v1"
_spec = importlib.util.spec_from_file_location("build_world_parameters", ROOT / "scripts" / "build_world_parameters.py")
WP = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(WP)
convert, material_class, site_group = WP.convert, WP.material_class, WP.site_group

# parameter code (Sol schema / Phase 1) → (module, unit kind, SI unit, quantity)
PARAM = {
    "RHO": ("C2", "density", "kg/m3", "density"), "UNIT_WEIGHT": ("C2", "density", "kg/m3", "density (from unit weight)"),
    "E": ("C2", "stress", "Pa", "Young's modulus"), "EDEF": ("C2", "stress", "Pa", "deformation modulus"),
    "NU": ("C2", "ratio", "1", "Poisson's ratio"), "UCS": ("C2", "stress", "Pa", "uniaxial compressive strength"),
    "UTS": ("C2", "stress", "Pa", "tensile strength"), "COH": ("C2", "stress", "Pa", "cohesion"),
    "PHI": ("C2", "angle", "deg", "internal friction angle"), "LTS": ("C2", "stress", "Pa", "long-term strength"),
    "LTS_RATIO": ("C2", "ratio", "1", "long-term strength σ∞/σc"), "EPS_UCS": ("C2", "strain", "1", "strain at peak"),
    "CREEP_RATE": ("C2", "none", "", "creep rate"), "CREEP_STRAIN": ("C2", "strain", "1", "creep strain"),
    "CREEP_LAW_PARAM": ("C2", "none", "", "creep law parameter"), "VISCOSITY": ("C2", "none", "", "viscosity"),
    "DAMAGE_PARAM": ("C2", "none", "", "damage parameter"),
    "LAMBDA": ("C3", "ratio", "1", "lateral stress ratio σh/σv"), "SIGMA_V": ("C3", "stress", "Pa", "vertical stress"),
    "SIGMA_H": ("C3", "stress", "Pa", "horizontal stress"), "TEMPERATURE": ("C3", "temperature", "degC", "temperature"),
    "GEOTHERMAL_GRADIENT": ("C3", "none", "", "geothermal gradient"), "PORE_PRESSURE": ("C3", "stress", "Pa",
                                                                                         "pore pressure"),
    "PERMEABILITY": ("C2", "none", "", "permeability"),
    "THICKNESS": ("C1", "length", "m", "thickness"), "DEPTH": ("C1", "length", "m", "depth"),
    "ELEVATION": ("C1", "length", "m", "elevation"),
    "CHAMBER_WIDTH": ("C4", "length", "m", "chamber width"), "CHAMBER_HEIGHT": ("C4", "length", "m", "chamber height"),
    "CHAMBER_LENGTH": ("C4", "length", "m", "chamber length"), "PILLAR_WIDTH": ("C4", "length", "m", "pillar width"),
    "AXIS_SPACING": ("C4", "length", "m", "axis spacing"), "PANEL_WIDTH": ("C4", "length", "m", "panel width"),
    "PANEL_LENGTH": ("C4", "length", "m", "panel length"), "EXTRACTION_RATIO": ("C4", "fraction", "1", "extraction ratio"),
    "LOADING_DEGREE": ("C4", "fraction", "1", "loading degree of pillars C"),
    "PROTECTIVE_LAYER": ("C1", "length", "m", "protective (water-protecting) layer thickness"),
    "BACKFILL_RATIO": ("C4", "fraction", "1", "backfill ratio"), "BACKFILL_DELAY": ("C4", "none", "", "backfill delay"),
    "BACKFILL_DENSITY": ("C4", "density", "kg/m3", "backfill density"),
    "BACKFILL_STIFFNESS": ("C4", "stress", "Pa", "backfill stiffness / modulus"),
    "BACKFILL_COMPACTION": ("C4", "fraction", "1", "backfill compaction"),
    "MINING_DATE": ("C4", "none", "", "mining date / period"), "BACKFILL_DATE": ("C4", "none", "", "backfill date"),
}
OBS_CODES = {"SUBSIDENCE", "SUBSIDENCE_RATE", "SUBSIDENCE_MAX", "TILT", "CURVATURE", "HORIZONTAL_STRAIN",
             "HORIZONTAL_DISPLACEMENT", "CONVERGENCE", "CONVERGENCE_RATE", "PILLAR_STRAIN", "INSAR_LOS",
             "ANGLE_OF_DRAW", "BREAK_ANGLE", "TIME_FACTOR"}
MECH = {"E", "EDEF", "NU", "UCS", "UTS", "COH", "PHI", "LTS", "LTS_RATIO", "EPS_UCS", "CREEP_RATE", "CREEP_STRAIN",
        "CREEP_LAW_PARAM", "VISCOSITY", "DAMAGE_PARAM"}
WORLD_CLASSES = ("ROCKSALT", "SYLVINITE", "CARNALLITE", "CLAY_CONTACT", "TRANSITION_OVERBURDEN", "COVER", "BACKFILL")
# parameters the worlds need: a registry row exists even without evidence (status UNKNOWN)
REQUIRED = {"C2": ("RHO", "E", "NU", "UCS", "UTS", "COH", "PHI", "LTS_RATIO", "CREEP_LAW_PARAM"),
            "C3": ("SIGMA_V", "LAMBDA", "SIGMA_H", "TEMPERATURE", "PORE_PRESSURE"),
            "C4": ("CHAMBER_WIDTH", "CHAMBER_HEIGHT", "PILLAR_WIDTH", "EXTRACTION_RATIO", "PANEL_WIDTH",
                   "BACKFILL_RATIO", "BACKFILL_DELAY", "BACKFILL_STIFFNESS", "MINING_DATE", "BACKFILL_DATE")}
SITE_GROUP = {"SKRU1": "SKRU1", "SKRU2": "OTHER_VKM_SITE", "SKRU3": "OTHER_VKM_SITE", "BKPRU1": "OTHER_VKM_SITE",
              "BKPRU2": "OTHER_VKM_SITE", "BKPRU3": "OTHER_VKM_SITE", "BKPRU4": "OTHER_VKM_SITE",
              "UST_YAYVA": "OTHER_VKM_SITE", "BEREZNIKI_CITY": "OTHER_VKM_SITE", "SOLIKAMSK_CITY": "OTHER_VKM_SITE",
              "SKRU1_OR_SKRU2_UNATTRIBUTED": "OTHER_VKM_SITE", "VKM_UNSPECIFIED": "VKM_REGIONAL",
              "OTHER_POTASH_SITE": "NON_VKM", "NON_VKM": "NON_VKM", "UNKNOWN": "GENERAL_OR_UNSTATED"}
SCALE_NORM = {"LAB": "LAB", "MASSIF": "MASSIF", "FIELD": "FIELD", "FIELD_MEASUREMENT": "FIELD", "MODEL": "MODEL",
              "MODEL_CHOICE": "MODEL", "CALIBRATED_EFFECTIVE_MODEL": "CALIBRATED", "CALIBRATED_EFFECTIVE": "CALIBRATED",
              "NORMATIVE": "NORMATIVE", "DESIGN": "DESIGN", "DERIVATION": "DERIVATION", "UNKNOWN": "UNKNOWN", "": "UNKNOWN"}


def branch_of(param: str, scale: str) -> str:
    """A — laboratory value (needs a LAB → MASSIF scale factor); B — calibrated / effective / massif value;
    N — norm or design value; F — field measurement of a state variable; '-' — not massif mechanics."""
    if param in MECH or param in ("RHO", "UNIT_WEIGHT"):
        return {"LAB": "A", "MASSIF": "B", "CALIBRATED": "B", "MODEL": "B", "FIELD": "B", "NORMATIVE": "N",
                "DESIGN": "N"}.get(scale, "-")
    if param in ("SIGMA_V", "SIGMA_H", "LAMBDA", "TEMPERATURE", "PORE_PRESSURE"):
        return {"FIELD": "F", "MASSIF": "F", "NORMATIVE": "N", "DESIGN": "N", "MODEL": "M", "CALIBRATED": "M",
                "DERIVATION": "M"}.get(scale, "-")
    return "-"


def _f(x) -> float | None:
    try:
        return None if x in (None, "") else float(x)
    except (TypeError, ValueError):
        return None


def _key(s: str | None) -> str:
    t = unicodedata.normalize("NFKC", s or "").casefold().replace("ё", "е")
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", t)).strip()


def _rows(p: Path) -> list[dict]:
    return list(csv.DictReader(open(p, encoding="utf-8"))) if p.is_file() else []


def _jsonl(p: Path) -> list[dict]:
    return [json.loads(x) for x in open(p, encoding="utf-8")] if p.is_file() else []


# ------------------------------------------------------------------------------------------- C1 unit vocabulary
class Units:
    def __init__(self, rows: list[dict]):
        self.alias: list[tuple[re.Pattern, str]] = []
        self.name = {}
        for r in rows:
            self.name[r["unit_id"]] = r["name_ru"]
            names = {r["name_ru"]} | {a.strip() for a in (r["aliases"] or "").split(";")}
            if r["abbrev"]:
                names |= {a.strip() for a in re.split(r"[/;]", r["abbrev"])}
            for a in names:
                a = re.sub(r"\(.*?\)", "", a).strip()
                if len(a) < 2 or a.isdigit():
                    continue
                if len(a) <= 2:            # one/two-letter seams only as «пласт X»
                    rx = re.compile(r"пласт\w*\s+" + re.escape(a) + r"(?![\wА-Яа-я'])")
                else:
                    rx = re.compile(r"(?<![\wА-Яа-я])" + re.escape(a) + r"(?![\wА-Яа-я])", re.I)
                self.alias.append((rx, r["unit_id"]))
        self.alias.sort(key=lambda x: -len(x[0].pattern))

    def unit_of(self, text: str) -> str:
        for rx, uid in self.alias:
            if rx.search(text or ""):
                return uid
        return ""


# ------------------------------------------------------------------------------------------------ evidence
def phase1_rows(canon: Path) -> list[dict]:
    out = []
    for r in _rows(canon / "WORLD_PARAMETERS" / "world_parameter_evidence.csv"):
        scale = SCALE_NORM.get(r["scale"], r["scale"])
        out.append(dict(producer="PHASE1_CATALOGUE", producer_record_id=r["wpe_id"], module=r["module"],
                        parameter=r["parameter"], target=r["material_class"], material_class=r["material_class"],
                        material_as_printed=r["seam"], scale=scale, branch=branch_of(r["parameter"], scale),
                        site_norm=r["site_scope"], site_group=r["site_group"],
                        value_as_printed=r["value_as_printed"], unit_as_printed=r["unit_as_printed"],
                        si_min=r["si_min"], si_max=r["si_max"], si_unit=r["si_unit"], conversion=r["conversion"],
                        conditions=r["conditions"], method=r["test_method"], n_samples=r["n_samples"],
                        origin=(r["original_or_cited"] or "UNKNOWN").upper(), cited_ref="", source_id=r["source_id"],
                        page_id=_page_id(r["source_id"], r["pdf_page"]), locator=r["locator"], quote="",
                        status=r["status"], verification="PHASE1_REVIEWED", notes=r["phase1_notes"][:300]))
    for r in _rows(canon / "WORLD_PARAMETERS" / "world_c1_thickness_evidence.csv"):
        out.append(dict(producer="PHASE1_CATALOGUE", producer_record_id=r["obs_id"], module="C1",
                        parameter="THICKNESS", target=r["unit_id"] or r["unit"], material_class="",
                        material_as_printed=r["unit"], scale=SCALE_NORM.get(r["scale"], r["scale"]), branch="-",
                        site_norm=r["site_scope"], site_group=r["site_group"],
                        value_as_printed=r["thickness_as_printed"], unit_as_printed="м",
                        si_min=r["thickness_min_m"], si_max=r["thickness_max_m"], si_unit="m", conversion="x1",
                        conditions=r["spatial_level"], method=r["observation_type"], n_samples="",
                        origin="CITED" if r["secondary_copy_of"] else "ORIGINAL", cited_ref="",
                        source_id=r["source_id"], page_id=_page_id(r["source_id"], r["pdf_page"]),
                        locator=r["locator"], quote="", status=r["status"], verification="PHASE1_REVIEWED",
                        notes=f"secondary copy of {r['secondary_copy_of']}" if r["secondary_copy_of"] else ""))
    return out


def _page_id(src: str, pdf_page: str) -> str:
    return f"{src}:p{int(pdf_page):04d}" if (pdf_page or "").strip().isdigit() and src.startswith("VKM-") else ""


def extraction_rows(canon: Path, runs: list[str], units: Units) -> list[dict]:
    out = []
    for run in runs:
        prod = "CLAUDE_DOUBLE_ENTRY" if run == "double" else f"SOL_{run.upper()}"
        for r in _jsonl(canon / "WORLD_EXTRACTION" / run / "accepted.jsonl"):
            code = r.get("parameter_code") or "OTHER"
            if r.get("kind") in ("ENTITY", "CITATION"):
                continue
            if code in OBS_CODES or r.get("kind") == "OBSERVATION":
                module, kind, si_unit = "OBS", ("length" if code in ("SUBSIDENCE", "SUBSIDENCE_MAX", "CONVERGENCE",
                                                                      "HORIZONTAL_DISPLACEMENT", "INSAR_LOS")
                                                 else "none"), "m"
            elif code in PARAM:
                module, kind, si_unit, _q = PARAM[code]
            else:
                module, kind, si_unit = {"GEOMETRY": "C1", "HISTORY": "C4", "LAW": "C2"}.get(r.get("kind"), "OTHER"), \
                    "none", ""
            scale = SCALE_NORM.get(r.get("scale") or "UNKNOWN", "UNKNOWN")
            ctx = " ".join(x or "" for x in (r.get("conditions"), r.get("notes")))
            cls = material_class(r.get("material_as_printed") or "", "", "", ctx if code in ("RHO", "UNIT_WEIGHT")
                                 else "") if module in ("C2", "C3") else ""
            target = cls if module in ("C2", "C3") else (units.unit_of(r.get("material_as_printed") or "")
                                                          if module == "C1" else "")
            vals = [v for v in (_f(r.get("value_min")), _f(r.get("value_max"))) if v is not None]
            si, rule = [], "NO_VALUE" if not vals else ""
            if vals and kind != "none" and not r.get("multiplier_as_printed"):
                conv = [convert(v, r.get("unit_as_printed") or "", kind) for v in vals]
                si = [c[0] for c in conv if c[0] is not None]
                rule = conv[0][1]
            elif vals:
                rule = "HEADER_MULTIPLIER_NOT_APPLIED" if r.get("multiplier_as_printed") else "UNIT_NOT_CONVERTED"
            out.append(dict(producer=prod, producer_record_id=r["record_id"], module=module, parameter=code,
                            target=target, material_class=cls, material_as_printed=r.get("material_as_printed") or "",
                            scale=scale, branch=branch_of(code, scale), site_norm=r.get("site_norm") or "UNKNOWN",
                            site_group=SITE_GROUP.get(r.get("site_norm") or "UNKNOWN", "GENERAL_OR_UNSTATED"),
                            value_as_printed=r.get("value_as_printed") or "", unit_as_printed=r.get("unit_as_printed")
                            or "", si_min=f"{min(si):.6g}" if si else "", si_max=f"{max(si):.6g}" if si else "",
                            si_unit=si_unit if si else "", conversion=rule, conditions=r.get("conditions") or "",
                            method=r.get("method") or "", n_samples=r.get("n_samples") or "",
                            origin=r.get("origin") or "UNKNOWN", cited_ref=r.get("cited_ref") or "",
                            source_id=r["page_id"].split(":")[0], page_id=r["page_id"], locator=r.get("locator") or "",
                            quote=r.get("quote") or "", status="AUTO_EXTRACTED_UNREVIEWED",
                            verification="VERIFIED_PROGRAMMATIC", notes=(r.get("notes") or "")[:300],
                            kind=r.get("kind"), time_as_printed=r.get("time_as_printed") or "",
                            equation_as_printed=r.get("equation_as_printed") or "", symbol=r.get("symbol") or ""))
    return out


BACKFILL_KIND = {"FILL_RATIO": "BACKFILL_RATIO", "STRENGTH_MODULUS": "BACKFILL_STIFFNESS", "LAG": "BACKFILL_DELAY",
                 "DENSITY": "BACKFILL_DENSITY", "SHRINKAGE": "BACKFILL_COMPACTION",
                 "COMPACTION_LAW": "BACKFILL_COMPACTION", "GAP": "BACKFILL_GAP"}


def curated_rows(canon: Path) -> list[dict]:
    """C3 sheet and the creep / backfill sheets of 09.10 (Claude subagents, numbers verified on the page)."""
    cur = canon / "WORLD_PARAMETERS" / "curated"
    out = []
    c3map = {"LAMBDA": "LAMBDA", "LAMBDA_HMAX": "LAMBDA", "LAMBDA_HMIN": "LAMBDA", "LAMBDA_X": "LAMBDA",
             "LAMBDA_Y": "LAMBDA", "SIGMA_V": "SIGMA_V", "SIGMA_H": "SIGMA_H", "SIGMA_H_MAX": "SIGMA_H",
             "SIGMA_H_MIN": "SIGMA_H", "RHO": "RHO", "UNIT_WEIGHT": "UNIT_WEIGHT", "T_AT_DEPTH": "TEMPERATURE",
             "PORE_PRESSURE": "PORE_PRESSURE", "PORE_PRESSURE_GAS": "PORE_PRESSURE"}
    for r in _rows(cur / "c3_parameters.csv"):
        code = c3map.get(r["parameter"], r["parameter"])
        scale = SCALE_NORM.get(r["scale"], r["scale"] or "UNKNOWN")
        out.append(dict(producer="CURATED_C3_20261009", producer_record_id=r["c3_id"], module="C3", parameter=code,
                        target=r["hypothesis_ids"] or r["material_class"], material_class=r["material_class"],
                        material_as_printed=r["layer_or_material_as_printed"], scale=scale,
                        branch=branch_of(code, scale), site_norm=r["site_norm"],
                        site_group=SITE_GROUP.get(r["site_norm"], "GENERAL_OR_UNSTATED"),
                        value_as_printed=r["value_as_printed"], unit_as_printed=r["unit_as_printed"],
                        si_min=r["si_min"], si_max=r["si_max"], si_unit=r["si_unit"], conversion=r["conversion"],
                        conditions=r["depth_or_horizon_as_printed"], method=r["method"], n_samples="",
                        origin=r["original_or_cited"] or "UNKNOWN", cited_ref=r["cited_work"],
                        source_id=r["source_id"], page_id=r["page_id"], locator=r["locator"], quote=r["quote"],
                        status=r["status"], verification="VERIFIED_ON_PAGE_BY_AGENT" if r["verified_on_page"] == "YES"
                        else "NOT_VERIFIED", notes=f"{r['parameter']}; world_status={r['world_status']}; "
                                                   f"{r['notes']}"[:300], conflict_ids=r["conflict_ids"],
                        world_status_hint=r["world_status"]))
    for name, module in (("c2_creep_laws.csv", "C2"), ("c4_backfill_parameters.csv", "C4")):
        for r in _rows(cur / name):
            if module == "C2":
                sym = re.split(r"[\s(,/]", (r.get("parameter") or "").strip())[0] or "param"
                code = f"CREEP.{(r.get('family') or 'UNKNOWN').replace('OTHER:', '')}.{sym}"
            else:
                code = BACKFILL_KIND.get(r.get("parameter_kind") or "", f"BACKFILL_{r.get('parameter_kind') or 'OTHER'}")
            scale = SCALE_NORM.get(r.get("scale", ""), r.get("scale") or "UNKNOWN")
            out.append(dict(producer="CURATED_LAWS_20261009", producer_record_id=r.get("law_id") or r.get("bf_id") or
                            r.get("row_id") or "", module=module, parameter=code,
                            target=(r.get("material_class") or "UNCLASSIFIED") if module == "C2" else "BACKFILL",
                            material_class=r.get("material_class", ""),
                            material_as_printed=r.get("lithology_as_printed") or r.get("material_as_printed", ""),
                            scale=scale, branch=r.get("branch") or branch_of(code, scale),
                            site_norm=r.get("site_norm", ""), site_group=SITE_GROUP.get(r.get("site_norm", ""),
                                                                                       "GENERAL_OR_UNSTATED"),
                            value_as_printed=r.get("value_as_printed", ""), unit_as_printed=r.get("unit_as_printed", ""),
                            si_min=r.get("si_min", ""), si_max=r.get("si_max", ""), si_unit=r.get("si_unit", ""),
                            conversion=r.get("conversion", ""), conditions=r.get("stress_range") or
                            r.get("conditions", ""), method=r.get("calibration_data") or r.get("method", ""),
                            n_samples=r.get("n_samples", ""), origin=r.get("original_or_cited") or "UNKNOWN",
                            cited_ref=r.get("cited_work", ""), source_id=r.get("source_id", ""),
                            page_id=r.get("page_id", ""), locator=r.get("locator", ""), quote=r.get("quote", ""),
                            status=r.get("status", ""), verification="VERIFIED_ON_PAGE_BY_AGENT"
                            if r.get("verified_on_page") == "YES" else "NOT_VERIFIED",
                            notes=" ".join(x for x in (r.get("family", ""), r.get("parameter", ""),
                                                        r.get("equation_normalized", ""), r.get("notes", "")) if x)[:300],
                            conflict_ids=r.get("conflict_ids", "")))
    return out


def _nums(text: str) -> list[float]:
    return [float(x.replace(",", ".")) for x in re.findall(r"\d+(?:[.,]\d+)?", text or "")]


def skru1_rows(canon: Path, units: Units) -> list[dict]:
    """Hand sheets of 08.10 for SKRU-1: borehole column (C1), chamber design and the extraction history by zone (C4)."""
    cur = canon / "WORLD_PARAMETERS" / "curated"
    out = []

    def row(rid, module, param, target, value, unit_si, scale, origin, src, page, loc, status, notes, conditions=""):
        vals = _nums(value) if param not in ("MINING_DATE", "BACKFILL_DATE") else []
        return dict(producer="CURATED_SKRU1_20261008", producer_record_id=rid, module=module, parameter=param,
                    target=target, material_class="", material_as_printed=target, scale=scale,
                    branch=branch_of(param, scale), site_norm="SKRU1", site_group="SKRU1",
                    value_as_printed=value, unit_as_printed=unit_si, si_min=f"{min(vals):.6g}" if vals else "",
                    si_max=f"{max(vals):.6g}" if vals else "", si_unit=unit_si if vals else "",
                    conversion="x1" if vals else "NO_VALUE", conditions=conditions, method="", n_samples="",
                    origin=origin, cited_ref="", source_id=src, page_id=_page_id(src, page), locator=loc, quote="",
                    status=status, verification="HAND_SHEET_08_10", notes=notes[:300],
                    time_as_printed=value if param in ("MINING_DATE", "BACKFILL_DATE") else "")
    for i, r in enumerate(_rows(cur / "skru1_c1_column.csv"), 1):
        out.append(row(f"SKRU1-C1-{i:03d}", "C1", "THICKNESS", units.unit_of(r["unit"]) or r["unit"], r["thickness_m"],
                       "m", "FIELD", "ORIGINAL", r["source_id"], r["pdf_page"], r["locator"],
                       r["thickness_status"], f"{r['support']}; {r['unit']}; {r['reading_error_m']}; {r['note']}",
                       r["support_kind"]))
    for i, r in enumerate(_rows(cur / "skru1_c4_chamber_design.csv"), 1):
        page = (re.search(r"pdf p\.(\d+)", r["locator"]) or [None, ""])[1]
        for col, param, unit in (("chamber_width_m", "CHAMBER_WIDTH", "m"), ("pillar_width_m", "PILLAR_WIDTH", "m"),
                                 ("axis_spacing_m", "AXIS_SPACING", "m"),
                                 ("areal_extraction_ratio", "EXTRACTION_RATIO", "1")):
            if r[col]:
                out.append(row(f"SKRU1-C4D-{i:02d}-{param}", "C4", param, f"{r['seam']}: {r['zone']}", r[col], unit,
                               "DESIGN", "ORIGINAL", r["source_id"], page, r["locator"], r["status"],
                               f"{r['label']}; {r['extraction_ratio_status'] if param == 'EXTRACTION_RATIO' else ''}; "
                               f"{r['note']}"))
    for i, r in enumerate(_rows(cur / "skru1_c4_extraction_history.csv"), 1):
        page = (re.search(r"pdf p\.(\d+)", r["locator"]) or [None, ""])[1]
        tgt = f"{r['seam']}: зона {r['zone']}, {r['block']}"
        for col, param, unit in (("depth_m", "DEPTH", "m"), ("chamber_width_m", "CHAMBER_WIDTH", "m"),
                                 ("pillar_width_m", "PILLAR_WIDTH", "m"), ("axis_spacing_m", "AXIS_SPACING", "m"),
                                 ("extracted_thickness_m", "CHAMBER_HEIGHT", "m"), ("loading_degree_C", "LOADING_DEGREE", "1"),
                                 ("mining_years", "MINING_DATE", ""), ("backfill_years", "BACKFILL_DATE", ""),
                                 ("lag_mining_end_to_backfill_y", "BACKFILL_DELAY", "year")):
            if r.get(col):
                out.append(row(f"SKRU1-C4H-{i:02d}-{param}", "C1" if param == "DEPTH" else "C4", param, tgt, r[col],
                               unit, "FIELD", "CITED", r["source_id"], page, r["locator"], r["status"],
                               f"{r['label']}; {r['figure']}; {r['note']}"))
    return out


def clusters(ev: list[dict]) -> None:
    """Copies of one number: same parameter, target, SI min/max (or printed value and unit) in several rows.
    ORIGINAL rows of the earliest source are primary; others are copies or cross-producer duplicates."""
    groups = defaultdict(list)
    for x in ev:
        if x["si_min"] != "":
            k = (x["parameter"], x["target"], f"{float(x['si_min']):.4g}", f"{float(x['si_max']):.4g}")
        elif x["value_as_printed"]:
            k = (x["parameter"], x["target"], _key(x["value_as_printed"]), _key(x["unit_as_printed"]))
        else:
            continue
        groups[k].append(x)
    n = 0
    for k, xs in sorted(groups.items()):
        srcs = {x["source_id"] for x in xs}
        prods = {x["producer"] for x in xs}
        if len(xs) < 2:
            continue
        n += 1
        cid = f"CC-{n:05d}"
        orig = [x for x in xs if x["origin"] == "ORIGINAL"]
        prim = min(orig or xs, key=lambda x: (x["source_id"], x["producer"] != "PHASE1_CATALOGUE", x["page_id"]))
        for x in xs:
            x["copy_cluster"] = cid
            if x is prim:
                x["copy_role"] = "PRIMARY"
            elif x["source_id"] == prim["source_id"] and x["page_id"] == prim["page_id"]:
                x["copy_role"] = "SAME_NUMBER_OTHER_PRODUCER"
            elif x["origin"] == "CITED":
                x["copy_role"] = "COPY_CITED"
            else:
                x["copy_role"] = "SAME_VALUE_OTHER_SOURCE"
            x["cluster_sources"] = len(srcs)
            x["cluster_producers"] = len(prods)


def double_entry_flags(ev: list[dict], canon: Path, sol_run: str) -> dict:
    """For Sol records on double-entry packets: MATCH / ATTRIBUTION_DIFF:<fields> / NOT_IN_DOUBLE."""
    sys.path.insert(0, str(ROOT / "src"))
    from vkm_world.extraction import compare as C
    sol = _jsonl(canon / "WORLD_EXTRACTION" / sol_run / "accepted.jsonl")
    dbl = _jsonl(canon / "WORLD_EXTRACTION" / "double" / "accepted.jsonl")
    if not sol or not dbl:
        return {}
    man = canon / "WORLD_EXTRACTION" / "packets.jsonl"
    pages = {}
    if man.is_file():
        for line in open(man, encoding="utf-8"):
            m = json.loads(line)
            pages[m["packet_id"]] = set(m["page_ids"])
    covered_sol = {pg for pid in {r["packet_id"] for r in sol} for pg in pages.get(pid, ())}
    covered_dbl = {pg for pid in {r["packet_id"] for r in dbl} for pg in pages.get(pid, ())}
    common_pages = covered_sol & covered_dbl if pages else ({r["page_id"] for r in sol} & {r["page_id"] for r in dbl})
    common = {r["packet_id"] for r in dbl}
    a = [r for r in sol if r["page_id"] in common_pages]
    b = [r for r in dbl if r["page_id"] in common_pages]
    pairs, only_a, only_b = C.match(a, b)
    flag = {}
    for r, s in pairs:
        d = C.attribution_diff(r, s)
        flag[r["record_id"]] = "MATCH" if not d else "ATTRIBUTION_DIFF:" + ";".join(d)
        flag[s["record_id"]] = flag[r["record_id"]]
    for r in only_a:
        flag[r["record_id"]] = "NOT_IN_DOUBLE"
    for s in only_b:
        flag[s["record_id"]] = "NOT_IN_SOL"
    for x in ev:
        if x["producer_record_id"] in flag:
            x["double_entry"] = flag[x["producer_record_id"]]
    summ = C.agreement(a, b)
    summ.update(reference="CLAUDE_DOUBLE_ENTRY is B, the producer run is A", packets_double=len(common),
                common_pages=len(common_pages))
    return summ


# ------------------------------------------------------------------------------------------------ registry
def _stats(vals: list[float]) -> dict:
    if not vals:
        return {"si_min": "", "si_p25": "", "si_median": "", "si_p75": "", "si_max": ""}
    q = WP._q
    return {"si_min": f"{min(vals):.4g}", "si_p25": f"{q(vals, .25):.4g}", "si_median": f"{statistics.median(vals):.4g}",
            "si_p75": f"{q(vals, .75):.4g}", "si_max": f"{max(vals):.4g}"}


RANK = {"UNKNOWN": 0, "SCENARIO_RANGE": 1, "ANALOGUE_VIA_TRANSFER": 2, "FACT": 3}


def world_status(rows: list[dict], branch: str, site: str) -> tuple[str, str]:
    """(status for the world, basis). FACT only for SKRU-1 field / massif evidence of the source itself."""
    orig = [x for x in rows if x["origin"] != "CITED"]
    if not rows:
        return "UNKNOWN", "нет evidence"
    if branch == "N":
        return "SCENARIO_RANGE", "норматив / проектное значение: не измерение, отдельная ветка N"
    if branch == "A":
        return "SCENARIO_RANGE", "ветка A: лабораторное значение × масштабный коэффициент LAB → MASSIF (Transfer)"
    if branch in ("B", "M"):
        return "SCENARIO_RANGE", "ветка B: эффективное / расчётное значение источника (MODEL_CHOICE источника)"
    if site == "SKRU1" and orig and all(x["scale"] in ("FIELD", "MASSIF") for x in orig):
        return "FACT", "натурные данные СКРУ-1, оригинал"
    if site == "SKRU1":
        return "SCENARIO_RANGE", "СКРУ-1, но не натурный оригинал (пересказ / неизвестный масштаб)"
    if site in ("OTHER_VKM_SITE", "VKM_REGIONAL"):
        return "ANALOGUE_VIA_TRANSFER", "ВКМ, не СКРУ-1: перенос только через явный Transfer"
    if site == "NON_VKM":
        return "ANALOGUE_VIA_TRANSFER", "другое месторождение: аналог, перенос через Transfer"
    return "SCENARIO_RANGE", "площадка не указана"


def transfer_links(transfers: list[dict]) -> dict[tuple[str, str], list[str]]:
    """(parameter code, material class) → transfer rows of the curated sheet (scale factors and branch ranges)."""
    out = defaultdict(list)
    for t in transfers:
        code = re.split(r"[\s(:]", (t.get("parameter") or "").strip())[0]
        for cls in re.split(r"[;,]\s*", t.get("material_class") or ""):
            out[(code, cls.strip())].append(t["transfer_id"])
            if code in ("E", "EDEF"):
                out[("E" if code == "EDEF" else "EDEF", cls.strip())].append(t["transfer_id"])
    return out


def registry(ev: list[dict], conflicts_by_ev: dict, transfers: dict | None = None) -> list[dict]:
    transfers = transfers or {}
    groups = defaultdict(list)
    for x in ev:
        if x["module"] not in ("C1", "C2", "C3", "C4"):
            continue
        if x.get("copy_role") in ("COPY_CITED", "SAME_NUMBER_OTHER_PRODUCER"):
            continue                                 # a copy does not count twice
        groups[(x["module"], x["parameter"], x["target"] or "UNSPECIFIED", x["site_group"], x["branch"])].append(x)
    for mod, params in REQUIRED.items():
        targets = WORLD_CLASSES if mod == "C2" else ("UNSPECIFIED",)
        for p in params:
            for t in targets:
                if mod == "C2" and t == "BACKFILL" and p not in ("RHO", "E", "CREEP_LAW_PARAM"):
                    continue
                if not any(k[0] == mod and (k[2] == t or mod != "C2") and
                           (k[1] == p or (p == "CREEP_LAW_PARAM" and k[1].startswith("CREEP"))) for k in groups):
                    groups[(mod, p, t, "SKRU1", "-")] = []
    out = []
    for (mod, p, t, site, br), xs in sorted(groups.items()):
        vals = [float(v) for x in xs for v in {x["si_min"], x["si_max"]} if v != ""]
        st, basis = world_status(xs, br, site)
        hints = {x.get("world_status_hint") for x in xs} - {None, ""}
        if hints and min(RANK.get(h, 1) for h in hints) < RANK[st]:       # the curated sheet is more cautious
            st = min(hints, key=lambda h: RANK.get(h, 1))
            basis += f"; оценка листа 09.10: {st}"
        cf = sorted({c for x in xs for c in conflicts_by_ev.get(id(x), ())} |
                    {c for x in xs for c in (x.get("conflict_ids") or "").split(";") if c})
        rec = {"passport_id": "", "module": mod, "parameter": p, "quantity": PARAM.get(p, ("", "", "", p))[3],
               "target": t, "site_group": site, "branch": br, "world_status": st, "status_basis": basis,
               "n_evidence": len(xs), "n_sources": len({x["source_id"] for x in xs}),
               "n_original": sum(1 for x in xs if x["origin"] == "ORIGINAL"),
               "n_converted": sum(1 for x in xs if x["si_min"] != ""),
               "si_unit": next((x["si_unit"] for x in xs if x["si_unit"]), PARAM.get(p, ("", "", ""))[2]),
               **_stats(vals),
               "scales": ";".join(f"{k}:{v}" for k, v in sorted(Counter(x["scale"] for x in xs).items())),
               "evidence_statuses": ";".join(f"{k}:{v}" for k, v in sorted(Counter(x["status"] for x in xs).items())),
               "producers": ";".join(f"{k}:{v}" for k, v in sorted(Counter(x["producer"] for x in xs).items())),
               "sources_pages": ";".join(sorted({x["page_id"] or x["source_id"] for x in xs}))[:1500],
               "evidence_ids": ";".join(x["ev_id"] for x in xs)[:3000], "conflict_ids": ";".join(cf),
               "transfer_ids": ";".join(transfers.get((p, t), [])) if mod == "C2" else "",
               "review_status": "AUTO_EXTRACTED_UNREVIEWED" if any(x["producer"].startswith(("SOL", "CLAUDE"))
                                                                    for x in xs) else "MIXED"}
        out.append(rec)
    order = {m: i for i, m in enumerate(("C1", "C2", "C3", "C4"))}
    out.sort(key=lambda r: (order[r["module"]], r["parameter"], r["target"], r["site_group"], r["branch"]))
    for i, r in enumerate(out, 1):
        r["passport_id"] = f"WPP-{i:05d}"
    return out


def conflicts(canon: Path, ev: list[dict]) -> tuple[list[dict], dict]:
    out, by_ev = [], defaultdict(set)
    for name, idc, topic in (("MECH_RHEO/mech_rheo_conflicts.csv", "conflict_id", "variable_or_topic"),
                             ("GEOLOGY_COORDS/stratigraphy_conflicts.csv", "conflict_id", "topic"),
                             ("MINING/mining_conflicts.csv", "conflict_id", "topic"),
                             ("WORLD_PARAMETERS/curated/c3_conflicts.csv", "conflict_id", "topic"),
                             ("WORLD_PARAMETERS/curated/c2_c4_conflicts.csv", "conflict_id", "topic")):
        for r in _rows(canon / name):
            out.append({"pc_id": "", "conflict_id": r.get(idc, ""), "origin": name.split("/")[-1], "kind": "RECORDED",
                        "topic": (r.get(topic) or "")[:300], "parameter": "", "target": "", "site_group": "",
                        "values": " | ".join(x for x in (r.get("values_as_printed"), r.get("value_a"), r.get("value_b"),
                                                         r.get("competing_values_as_printed")) if x)[:600],
                        "sources": (r.get("sources_locators") or r.get("source_ids") or
                                    ";".join(x for x in (r.get("source_a"), r.get("source_b")) if x) or "")[:400],
                        "resolution_status": r.get("resolution_status") or r.get("resolution") or "OPEN",
                        "evidence_ids": ""})
    groups = defaultdict(list)                      # computed: originals of one parameter disagree by > ×3
    for x in ev:
        if x["module"] in ("C2", "C3") and x["si_min"] != "" and x["origin"] != "CITED" and x["branch"] in "ABFM":
            groups[(x["parameter"], x["target"], x["site_group"], x["branch"])].append(x)
    n = 0
    for (p, t, site, br), xs in sorted(groups.items()):
        by_src = defaultdict(list)
        for x in xs:
            by_src[x["source_id"]].append(float(x["si_min"]))
            by_src[x["source_id"]].append(float(x["si_max"]))
        if len(by_src) < 2:
            continue
        meds = {s: statistics.median(v) for s, v in by_src.items()}
        pos = [v for v in meds.values() if v > 0]
        if len(pos) >= 2 and max(pos) / min(pos) > 3:
            n += 1
            cid = f"WPC-{n:04d}"
            lo, hi = min(meds, key=meds.get), max(meds, key=meds.get)
            out.append({"pc_id": "", "conflict_id": cid, "origin": "computed", "kind": "SPREAD_GT_3X", "topic": f"{p} {t}",
                        "parameter": p, "target": t, "site_group": site,
                        "values": f"{lo}: {meds[lo]:.4g}; {hi}: {meds[hi]:.4g} (median per source, SI)",
                        "sources": ";".join(sorted(by_src)), "resolution_status": "OPEN",
                        "evidence_ids": ";".join(x["ev_id"] for x in xs)[:2000]})
            for x in xs:
                by_ev[id(x)].add(cid)
    for i, c in enumerate(out, 1):
        c["pc_id"] = f"WPK-{i:05d}"
    return out, by_ev


# ------------------------------------------------------------------------------------------------ objects
def objects(canon: Path, runs: list[str], units: "Units | None" = None) -> list[dict]:
    """Layer of objects: the Phase-1 spatial hierarchy plus the objects named in the extraction. A name becomes a key
    (kind, mine, ident) — :func:`vkm_world.extraction.objects.object_key`; names with one key are one object, linked to
    the hierarchy entity with that key or kept as a new keyed object (parent: the mine) or, without a pattern, as a
    name-only candidate."""
    sys.path.insert(0, str(ROOT / "src"))
    from vkm_world.extraction import objects as OK
    seam_of = units.unit_of if units else None
    hier = _rows(canon / "MINING" / "spatial_hierarchy.csv")
    out, by_key = {}, {}
    level_rank = {"mine": 0, "mine_field": 1, "neighbour_mine": 2}
    for h in sorted(hier, key=lambda h: (level_rank.get(h["level"], 9), h["entity_id"])):
        out[h["entity_id"]] = {"wo_id": "", "object_id": h["entity_id"], "kind": "", "mine": "", "ident": "",
                               "name": h["name"], "level": h["level"], "entity_type": h["entity_type_as_source"],
                               "parent_id": h["parent_id"], "mine_attribution": h["mine_attribution"],
                               "site_scope": h["site_scope"], "origin": "PHASE1_SPATIAL_HIERARCHY",
                               "resolution": "HIERARCHY", "n_mentions_extraction": 0, "names_as_printed": "",
                               "pages": "", "source_ids": h["source_ids"], "status": h["status"]}
        k = OK.hierarchy_key(h, seam_of)
        if k:
            by_key.setdefault(k, h["entity_id"])
            out[h["entity_id"]].update(kind=k[0], mine=k[1], ident=k[2])
    mine_ids = {k[1]: v for k, v in by_key.items() if k[0] == "MINE"}
    pages, names = defaultdict(set), defaultdict(set)
    for run in runs:
        for r in _jsonl(canon / "WORLD_EXTRACTION" / run / "accepted.jsonl"):
            if r.get("kind") != "ENTITY" or not r.get("entity_name"):
                continue
            k = OK.object_key(r["entity_name"], r.get("entity_type"), r.get("site_norm"), seam_of)
            oid = by_key.get(k)
            if oid is None and k[0] == "SEAM":               # a seam is its stratigraphic unit
                oid = f"UNIT:{k[2]}"
                by_key[k] = oid
                out.setdefault(oid, {"wo_id": "", "object_id": oid, "kind": "SEAM", "mine": "", "ident": k[2],
                                     "name": units.name.get(k[2], k[2]) if units else k[2], "level": "unit",
                                     "entity_type": "SEAM", "parent_id": "", "mine_attribution": "",
                                     "site_scope": "", "origin": "STRATIGRAPHIC_UNIT", "resolution": "UNIT",
                                     "n_mentions_extraction": 0, "names_as_printed": "", "pages": "",
                                     "source_ids": "", "status": "PHASE1_REVIEWED"})
            if oid is None:
                ident = re.sub(r"[^\w]+", "_", k[2])[:60].strip("_") or "_"
                oid = f"X:{k[0]}:{k[1] or 'DEPOSIT'}:{ident}"
                by_key[k] = oid
                keyed = k[0] != "OTHER" and (k[0] in ("BOREHOLE", "FAULT", "SEAM") or not re.search(r"\s", k[2]))
                out[oid] = {"wo_id": "", "object_id": oid, "kind": k[0], "mine": k[1], "ident": k[2],
                            "name": r["entity_name"], "level": "", "entity_type": r.get("entity_type") or "",
                            "parent_id": mine_ids.get(k[1], f"X:MINE:{k[1]}:_" if k[1] not in ("", "UNKNOWN") else "")
                            if k[0] not in ("MINE", "BOREHOLE", "FAULT", "SEAM") else "",
                            "mine_attribution": k[1] or (r.get("site_norm") or "UNKNOWN"),
                            "site_scope": r.get("site_norm") or "", "origin": "EXTRACTION_CANDIDATE",
                            "resolution": "NEW_KEYED" if keyed else "NEW_NAME_ONLY", "n_mentions_extraction": 0,
                            "names_as_printed": "", "pages": "", "source_ids": "",
                            "status": "AUTO_EXTRACTED_UNREVIEWED"}
            out[oid]["n_mentions_extraction"] += 1
            pages[oid].add(r["page_id"])
            names[oid].add(r["entity_name"].strip())
    for oid, ps in pages.items():
        out[oid]["pages"] = ";".join(sorted(ps))[:1500]
        out[oid]["names_as_printed"] = " | ".join(sorted(names[oid]))[:600]
        if out[oid]["origin"] == "EXTRACTION_CANDIDATE":
            out[oid]["source_ids"] = ";".join(sorted({p.split(":")[0] for p in ps}))
    res = sorted(out.values(), key=lambda o: (o["origin"] != "PHASE1_SPATIAL_HIERARCHY", o["object_id"]))
    for i, o in enumerate(res, 1):
        o["wo_id"] = f"WOJ-{i:05d}"
    return res


# ------------------------------------------------------------------------------------------------ literature
def _title_key(t: str) -> set[str]:
    return {w[:6] for w in _key(t).split() if len(w) >= 4}


def _surname(a: str) -> str:
    m = re.match(r"\s*([A-Za-zА-Яа-яЁё\-]{3,})", a or "")
    return _key(m.group(1)) if m else ""


def match_work(title: str, authors: str, year, works: list[dict]) -> tuple[str, float]:
    """Best corpus / external work for a parsed bibliography entry: title stems overlap ≥ 0.6 of the shorter title
    and the year or the first author's surname agrees. Returns (work id, score) or ("", 0)."""
    tk, sn = _title_key(title), _surname(authors)
    best, score = "", 0.0
    if len(tk) < 2:
        return best, score
    for w in works:
        wk = w["_tk"]
        if len(wk) < 2:
            continue
        ov = len(tk & wk) / min(len(tk), len(wk))
        if ov < 0.6:
            continue
        same_year = bool(year) and str(year) == str(w.get("year") or "")
        same_author = bool(sn) and sn == w["_sn"]
        if not (same_year or same_author):
            continue
        sc = ov + 0.2 * same_year + 0.2 * same_author
        if sc > score:
            best, score = w["id"], sc
    return best, round(score, 3)


def cited_literature(ev: list[dict], nav: Path | None, canon: Path | None = None) -> list[dict]:
    """Bibliography entries behind cited numbers: «[12]» on a page → entry 12 of that source's list (NAV) → a corpus
    work (NAV link, else a title / author / year match) or a work of the external register; otherwise the parsed entry
    stays as a work to obtain. Matches are AUTO, unreviewed."""
    cited = defaultdict(list)
    for x in ev:
        if x["origin"] == "CITED" and x["cited_ref"] and x["source_id"].startswith("VKM-"):
            labs = re.findall(r"\[(\d{1,3})", x["cited_ref"]) + re.findall(r"[,;]\s*(\d{1,3})(?=[\],;])",
                                                                              x["cited_ref"])
            if re.fullmatch(r"\s*\d{1,3}\s*", x["cited_ref"]):
                labs = [x["cited_ref"].strip()]
            for n in dict.fromkeys(labs):
                cited[(x["source_id"], n)].append(x["ev_id"])
            if not labs:
                cited[(x["source_id"], x["cited_ref"][:120])].append(x["ev_id"])
    entries, links, works = {}, {}, []
    if nav and nav.is_file():
        import duckdb
        c = duckdb.connect(str(nav), read_only=True)
        for oid, sid, lab, ordn, t, pa, pt, py in c.execute(
                "select object_id, source_id, entry_label, ordinal_in_list, normalized_text, parsed_authors::varchar, "
                "parsed_title, parsed_year from bibliography_entries order by object_id").fetchall():
            key_lab = re.sub(r"\D", "", lab or "") or (str(ordn) if ordn is not None else "")
            entries.setdefault((sid, key_lab), (oid, (t or "")[:300], pa or "", pt or "", py))
        for eid, wid, st in c.execute("select entry_id, cited_work_id, match_status from bibliography_links").fetchall():
            links[eid] = (wid, st)
        for wid, title, auth, year in c.execute("select work_id, title, authors_display, publication_year from works"
                                                ).fetchall():
            works.append({"id": wid, "year": year, "_tk": _title_key(title or ""), "_sn": _surname(auth or "")})
    ext = []
    if canon is not None:
        for r in _rows(canon / "EXTERNAL" / "external_sources.csv"):
            ext.append({"id": r["ext_id"], "year": r["year"], "_tk": _title_key(r["title"]), "_sn": _surname(r["authors"]),
                        "doi": r["doi"]})
    out = []
    for (sid, ref), evs in sorted(cited.items()):
        ent = entries.get((sid, ref))
        if ent is None and not ref.isdigit():                       # «Кудряшов А.И., Мараков В.Е., 2012»
            sn, yr = _surname(ref), (re.search(r"(?<!\d)(1[89]\d\d|20[0-2]\d)(?!\d)", ref) or [None])[0]
            if sn and yr:
                hits = [e for (s2, _l), e in entries.items() if s2 == sid and str(e[4] or "") == yr
                        and sn in _key(e[2] or e[1])]
                ent = hits[0] if len(hits) == 1 else None
        rec = {"cl_id": f"WCL-{len(out) + 1:05d}", "citing_source_id": sid, "cited_ref": ref,
               "bibliography_entry_id": "", "entry_text": "", "parsed_authors": "", "parsed_title": "",
               "parsed_year": "", "cited_work_id": "", "match_method": "", "match_score": "", "resolution": "UNRESOLVED",
               "n_evidence": len(evs), "evidence_ids": ";".join(evs)[:1500]}
        if ent:
            oid, text, pa, pt, py = ent
            rec.update(bibliography_entry_id=oid, entry_text=text, parsed_authors=pa[:200], parsed_title=pt[:300],
                       parsed_year=py or "", resolution="ENTRY_PARSED_NOT_IN_CORPUS")
            wid, st = links.get(oid, ("", ""))
            if wid:
                rec.update(cited_work_id=wid, match_method=f"NAV:{st}", match_score="1", resolution="CORPUS_WORK")
            else:
                w, sc = match_work(pt or text, pa or text, py, works)
                if w:
                    rec.update(cited_work_id=w, match_method="TITLE_STEMS+YEAR/AUTHOR", match_score=sc,
                               resolution="CORPUS_WORK_AUTO")
                else:
                    e, sc = match_work(pt or text, pa or text, py, ext)
                    if e:
                        rec.update(cited_work_id=e, match_method="TITLE_STEMS+YEAR/AUTHOR", match_score=sc,
                                   resolution="EXTERNAL_REGISTER_AUTO")
        out.append(rec)
    return out


# ------------------------------------------------------------------------------------------------ build
EV_COLS = ["ev_id", "producer", "producer_record_id", "module", "parameter", "kind", "target", "material_class",
           "material_as_printed", "scale", "branch", "site_norm", "site_group", "value_as_printed", "unit_as_printed",
           "si_min", "si_max", "si_unit", "conversion", "symbol", "equation_as_printed", "time_as_printed",
           "conditions", "method", "n_samples", "origin", "cited_ref", "source_id", "page_id", "locator", "quote",
           "status", "verification", "copy_cluster", "copy_role", "cluster_sources", "cluster_producers",
           "double_entry", "conflict_ids", "notes"]


def _csv(rows: list[dict], cols: list[str] | None = None) -> bytes:
    buf = io.StringIO()
    cols = cols or (list(rows[0]) if rows else ["empty"])
    w = csv.DictWriter(buf, fieldnames=cols, lineterminator="\n", extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c, "") for c in cols})
    return buf.getvalue().encode("utf-8")


def build(canon: Path, runs: list[str], nav: Path | None) -> dict[str, bytes]:
    units = Units(_rows(canon / "GEOLOGY_COORDS" / "stratigraphic_units.csv"))
    ev = phase1_rows(canon) + curated_rows(canon) + skru1_rows(canon, units) + extraction_rows(canon, runs, units)
    prod_order = {"PHASE1_CATALOGUE": 0, "CURATED_SKRU1_20261008": 1, "CURATED_C3_20261009": 2,
                  "CURATED_LAWS_20261009": 3}
    ev.sort(key=lambda x: (x["module"], x["parameter"], x["target"], prod_order.get(x["producer"], 5), x["producer"],
                           x["source_id"], x["page_id"], x["producer_record_id"]))
    for i, x in enumerate(ev, 1):
        x["ev_id"] = f"WPV-{i:06d}"
    clusters(ev)
    sol_runs = [r for r in runs if r != "double"]
    de = double_entry_flags(ev, canon, sol_runs[0]) if sol_runs and "double" in runs else {}
    conf, by_ev = conflicts(canon, ev)
    for x in ev:
        extra = sorted(by_ev.get(id(x), ()))
        if extra:
            x["conflict_ids"] = ";".join(filter(None, [x.get("conflict_ids", "")] + extra))
    trans = _rows(canon / "WORLD_PARAMETERS" / "curated" / "c2_transfer_branches.csv")
    reg = registry(ev, by_ev, transfer_links(trans))
    objs = objects(canon, runs, units)
    lit = cited_literature(ev, nav, canon)
    files = {"passport_evidence.csv": _csv(ev, EV_COLS)}
    for mod in ("C1", "C2", "C3", "C4"):                  # per-module views (PUBLIC text files stay under 5 MB)
        files[f"passport_evidence_{mod.lower()}.csv"] = _csv([x for x in ev if x["module"] == mod], EV_COLS)
    files.update({"world_passport.csv": _csv(reg),
             "passport_conflicts.csv": _csv(conf), "world_objects.csv": _csv(objs),
             "cited_literature.csv": _csv(lit), "transfer_branches.csv": _csv(trans)})
    inputs = {}
    for p in sorted(canon.glob("WORLD_PARAMETERS/**/*.csv")) + [canon / "WORLD_EXTRACTION" / r / "accepted.jsonl"
                                                                for r in runs]:
        if p.is_file():
            inputs[p.relative_to(canon).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    receipt = {"rule_version": RULE_VERSION, "runs": runs, "inputs": inputs,
               "outputs": {k: hashlib.sha256(v).hexdigest() for k, v in files.items()},
               "counts": {"evidence": len(ev), "by_producer": dict(Counter(x["producer"] for x in ev).most_common()),
                          "by_module": dict(Counter(x["module"] for x in ev).most_common()),
                          "copy_clusters": len({x.get("copy_cluster") for x in ev if x.get("copy_cluster")}),
                          "passport_rows": len(reg),
                          "passport_by_status": dict(Counter(r["world_status"] for r in reg).most_common()),
                          "conflicts": len(conf), "conflicts_computed": sum(1 for c in conf if c["origin"] == "computed"),
                          "objects": len(objs), "objects_by_resolution": dict(Counter(o["resolution"] for o in objs)),
                          "hierarchy_objects_mentioned": sum(1 for o in objs if o["origin"] == "PHASE1_SPATIAL_HIERARCHY"
                                                             and o["n_mentions_extraction"]),
                          "cited_refs": len(lit), "cited_by_resolution": dict(Counter(x["resolution"] for x in lit))},
               "double_entry_agreement": de,
               "nav_used": bool(nav and nav.is_file())}
    files["build_receipt.json"] = (json.dumps(receipt, ensure_ascii=False, indent=1) + "\n").encode("utf-8")
    return files


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--runs", nargs="+", default=["high_full", "double"])
    ap.add_argument("--nav", type=Path)
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    res = os.environ.get("VKM_RESOURCES_ROOT")
    if not res:
        print("set VKM_RESOURCES_ROOT", file=sys.stderr)
        return 2
    canon = Path(res) / "11_evidence_vnext" / "canonical"
    files = build(canon, a.runs, a.nav)
    out = canon / "WORLD_PASSPORT"
    if a.check:
        bad = [k for k, v in files.items() if not (out / k).is_file() or (out / k).read_bytes() != v]
        print(json.dumps({"check": "FAIL" if bad else "PASS", "differs": bad}))
        return 1 if bad else 0
    out.mkdir(parents=True, exist_ok=True)
    for k, v in files.items():
        (out / k).write_bytes(v)
    print(files["build_receipt.json"].decode("utf-8")[:3000])
    return 0


if __name__ == "__main__":
    sys.exit(main())

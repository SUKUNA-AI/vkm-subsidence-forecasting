#!/usr/bin/env python3
"""World parameter passport, stage 1: Phase-1 mechanics evidence → SI rows per world parameter and material class.

Reads the PRIVATE canonical catalogue ``11_evidence_vnext/canonical/MECH_RHEO/mechanics_evidence_catalog.csv``
(Phase-1 sources VKM-SRC-001…041 and external ones) and writes to ``11_evidence_vnext/canonical/WORLD_PARAMETERS/``:

``world_parameter_evidence.csv``  one row per numeric evidence row that maps to a world parameter of module C2
                                  (materials) or C3 (initial state): the value converted to SI, the material class of
                                  the world (7 classes, owner decision 08.10.2026), scale, site scope, status and the
                                  locator of the catalogue row. No quotes are copied.
``world_parameter_summary.csv``   per parameter × material class × scale × site group: number of rows and sources,
                                  min / quartiles / max of the SI values, the statuses behind them.
``build_receipt.json``            input and output sha256, counts, unmapped rows by reason.

Numbers are copied, never invented. A row whose unit cannot be converted is kept with ``si_value`` empty and
``conversion`` = ``UNIT_NOT_CONVERTED``. Ranges here are evidence summaries, not world ranges: LAB → MASSIF transfer
(branch A: scale factor; branch B: calibrated effective values of published VKM models) is the next stage.

Usage:  VKM_RESOURCES_ROOT=<PRIVATE checkout> python scripts/build_world_parameters.py [--check]
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

csv.field_size_limit(sys.maxsize)

G = 9.80665
RULE_VERSION = "world_parameters_v0"

# material classes of the world (owner decision 08.10.2026: 7 classes + salt rocks without a class)
MATERIAL_CLASSES = ("ROCKSALT", "SYLVINITE", "CARNALLITE", "CLAY_CONTACT", "TRANSITION_OVERBURDEN", "COVER", "BACKFILL",
                    "SALT_GENERAL", "COLUMN", "UNCLASSIFIED")    # COLUMN: an average over the whole rock column
_MATERIAL_RULES = (            # first match wins; text = lithology + layer as printed + seam code, lower case
    ("BACKFILL", r"закладк|backfill|галитов\w* отход|отходы флотац"),
    ("COLUMN", r"mean of overburden|overburden column|весь массив|средн\w* по толщ|column average"),
    ("CARNALLITE", r"карналлит|carnallit"),
    ("SYLVINITE", r"сильвинит|sylvinit"),
    ("ROCKSALT", r"каменн\w* сол|^галит|rock salt|halite"),      # before COVER: «покровная каменная соль» is salt
    ("CLAY_CONTACT", r"глин|контакт|clay|interface"),
    ("COVER", r"покровн\w* отлож|четвертичн|суглин|супес|песок|cover|quaternary"),
    ("TRANSITION_OVERBURDEN", r"мергел|ангидрит|песчаник|аргиллит|алевролит|известняк|доломит|переходн|соляно-мергел|"
                              r"пестроцвет|терригенн|взт|надсолев|marl|anhydrit|sandstone|overburden"),
    ("SALT_GENERAL", r"соляные породы|соляных пород|salt rocks|mixed|all units|соляной толщ"),
    ("ROCKSALT", r"\bсоль\b|\bsalt\b"),
)
_SEAM_TO_CLASS = {"MPS": "ROCKSALT", "PKS": "ROCKSALT", "PDKS": "ROCKSALT"}   # межпластовая / подстилающая / покровная соль

# world parameters (module, quantity) ← catalogue variables; unit kind decides the SI conversion
PARAMETERS = {
    "RHO": ("C2", "density", "kg/m3", "density",
            ("density", "unit_weight", "bulk_density", "volumetric_weight")),
    "E": ("C2", "Young's modulus (elastic)", "Pa", "stress", ("youngs_modulus", "elastic_modulus", "dynamic_modulus")),
    "EDEF": ("C2", "deformation modulus", "Pa", "stress", ("deformation_modulus",)),
    "NU": ("C2", "Poisson's ratio", "1", "ratio", ("poisson_ratio",)),
    "UCS": ("C2", "uniaxial compressive strength", "Pa", "stress", ("ucs", "ucs_cube_strength", "compressive_strength")),
    "UTS": ("C2", "tensile strength", "Pa", "stress", ("tensile_strength", "brazilian_tensile_strength")),
    "COH": ("C2", "cohesion", "Pa", "stress", ("cohesion",)),
    "PHI": ("C2", "internal friction angle", "deg", "angle", ("friction_angle", "internal_friction_angle")),
    "LTS": ("C2", "long-term strength", "Pa", "stress", ("long_term_strength",)),
    "LTS_RATIO": ("C2", "long-term strength coefficient σ∞/σc", "1", "ratio", ()),   # LTS rows printed as a fraction
    "EPS_UCS": ("C2", "strain at peak (UCS)", "1", "strain", ("strain_at_ucs",)),
    "SIGMA_V": ("C3", "vertical in-situ stress", "Pa", "stress", ("vertical_stress",)),
    "SIGMA_H": ("C3", "horizontal in-situ stress", "Pa", "stress",
                ("horizontal_stress_magnitude", "max_horizontal_stress", "min_horizontal_stress")),
    "LAMBDA": ("C3", "lateral stress ratio σh/σv", "1", "ratio", ("lateral_stress_ratio", "K0_lambda")),
}
_VAR_TO_PARAM = {v: p for p, spec in PARAMETERS.items() for v in spec[4]}

_STRESS = {"гпа": 1e9, "gpa": 1e9, "мпа": 1e6, "mpa": 1e6, "кпа": 1e3, "kpa": 1e3, "па": 1.0, "pa": 1.0,
           "тс/м2": 1e3 * G, "тс/м²": 1e3 * G, "кгс/см2": 98066.5, "кгс/см²": 98066.5, "kgf/cm2": 98066.5}
_DENSITY = {"г/см3": 1000.0, "г/см³": 1000.0, "g/cm3": 1000.0, "т/м3": 1000.0, "т/м³": 1000.0, "t/m3": 1000.0,
            "кг/м3": 1.0, "кг/м³": 1.0, "kg/m3": 1.0}
_UNIT_WEIGHT = {"10^4*n/m3": 1e4, "мн/м3": 1e6, "мн/м³": 1e6, "мпа/м": 1e6, "кн/м3": 1e3, "кн/м³": 1e3, "kn/m3": 1e3, "н/м3": 1.0,
                "n/m3": 1.0}


def material_class(lithology: str, layer: str, seam: str, context: str = "") -> str:
    """``context`` (test conditions and Phase-1 notes) only decides BACKFILL: a backfill density recorded under a rock
    lithology («кажущаяся» плотность закладки) must not become a rock value."""
    if re.search(r"закладк|not a rock density", (context or "").lower()):
        return "BACKFILL"
    text = " ".join((lithology or "", layer or "", seam or "")).lower()
    for cls, rx in _MATERIAL_RULES:
        if re.search(rx, text):
            return cls
    return _SEAM_TO_CLASS.get((seam or "").upper(), "UNCLASSIFIED")


def convert(value: float, unit: str, kind: str) -> tuple[float | None, str]:
    """(SI value, rule) or (None, reason). Unit weight converts to density through g."""
    u = (unit or "").strip().lower().replace(" ", "")
    if kind == "stress":
        f = _STRESS.get(u)
        return (value * f, f"x{f:g}") if f else (None, "UNIT_NOT_CONVERTED")
    if kind == "density":
        if u in _DENSITY:
            return value * _DENSITY[u], f"x{_DENSITY[u]:g}"
        if u in _UNIT_WEIGHT:
            return value * _UNIT_WEIGHT[u] / G, f"unit weight x{_UNIT_WEIGHT[u]:g}/g"
        return None, "UNIT_NOT_CONVERTED"
    if kind in ("ratio",):
        return (value, "x1") if u in ("", "-", "д.ед.", "1", "безразм.", "-(доляотγh)") else (None, "UNIT_NOT_CONVERTED")
    if kind == "strain":
        if u == "%":
            return value / 100.0, "%/100"
        return (value, "x1") if u in ("", "-", "д.ед.") else (None, "UNIT_NOT_CONVERTED")
    if kind == "angle":
        return (value, "deg") if u in ("°", "град", "град.", "градус", "deg", "degree", "") else (None, "UNIT_NOT_CONVERTED")
    if kind == "length":                                     # C1 thickness / depth, C4 geometry, observations
        f = _LENGTH.get(u)
        return (value * f, f"x{f:g}") if f else (None, "UNIT_NOT_CONVERTED")
    if kind == "fraction":                                   # extraction ratio, fill ratio: «%» or a fraction
        if u == "%":
            return value / 100.0, "%/100"
        return (value, "x1") if u in ("", "-", "д.ед.", "1", "доли", "долиед.") else (None, "UNIT_NOT_CONVERTED")
    if kind == "temperature":                                # kept in °C (the corpus prints °C); K = °C + 273.15
        return (value, "degC") if u in ("°c", "°с", "ºc", "ºс", "градс", "c", "с") else (None, "UNIT_NOT_CONVERTED")
    return None, "UNIT_NOT_CONVERTED"


_LENGTH = {"м": 1.0, "m": 1.0, "мм": 1e-3, "mm": 1e-3, "см": 1e-2, "cm": 1e-2, "км": 1e3, "km": 1e3, "дм": 0.1}


def _num(s: str) -> float | None:
    try:
        return float(s) if s not in (None, "") else None
    except ValueError:
        return None


def site_group(scope: str) -> str:
    s = (scope or "").upper()
    if s == "SKRU1":
        return "SKRU1"
    if s in ("VKM_REGIONAL",):
        return "VKM_REGIONAL"
    if s.startswith(("SKRU", "BKPRU")) or s in ("OTHER_VKM_SITE", "SOLIKAMSK_GROUP", "BEREZNIKI_GROUP"):
        return "OTHER_VKM_SITE"
    if s in ("GENERAL_METHOD", ""):
        return "GENERAL_OR_UNSTATED"
    return "NON_VKM"


def evidence_rows(mech: list[dict]) -> tuple[list[dict], Counter]:
    out, skipped = [], Counter()
    for r in mech:
        param = _VAR_TO_PARAM.get(r["variable"])
        if param is None:
            skipped["VARIABLE_NOT_A_WORLD_PARAMETER"] += 1
            continue
        if r.get("duplicate_of"):
            skipped["DUPLICATE_ROW"] += 1
            continue
        if param == "LTS" and r["unit_as_printed"].strip() in ("-", "д.ед.", ""):
            param = "LTS_RATIO"
        module, quantity, unit_si, kind, _ = PARAMETERS[param]
        vals = [_num(r["value_point"])] if _num(r["value_point"]) is not None else \
            [v for v in (_num(r["value_min"]), _num(r["value_max"])) if v is not None]
        if not vals:
            skipped["NO_NUMERIC_VALUE"] += 1
            continue
        conv = [convert(v, r["unit_as_printed"], kind) for v in vals]
        si = [c[0] for c in conv]
        rule = conv[0][1]
        cls = material_class(r["material_lithology"], r["layer_or_unit_as_printed"], r["unit_seam"],
                             r["conditions"] + " " + r["notes"] if param == "RHO" else "")
        out.append({
            "wpe_id": "", "param_id": f"{param}.{cls}", "parameter": param, "module": module, "quantity": quantity,
            "material_class": cls, "seam": r["unit_seam"], "scale": r["scale"], "site_scope": r["site_scope"],
            "site_group": site_group(r["site_scope"]), "status": r["status"], "evidence_type": r["evidence_type"],
            "si_min": "" if si[0] is None else f"{min(si):.6g}", "si_max": "" if si[0] is None else f"{max(si):.6g}",
            "si_unit": unit_si, "conversion": rule, "value_as_printed": r["value_as_printed"],
            "unit_as_printed": r["unit_as_printed"], "test_method": r["test_method"], "n_samples": r["n_samples"],
            "conditions": r["conditions"], "source_id": r["source_id"], "pdf_page": r["pdf_page"],
            "printed_page": r["printed_page"], "locator": r["locator"], "catalogue": "MECH_RHEO/mechanics_evidence_catalog",
            "catalogue_row_id": r["row_id"], "transfer_status_SKRU1": r["transfer_status_SKRU1"],
            "original_or_cited": r["original_or_cited"], "confidence": r["confidence"],
            "phase1_notes": " ".join(r["notes"].split())[:300]})
    out.sort(key=lambda x: (list(PARAMETERS).index(x["parameter"]), MATERIAL_CLASSES.index(x["material_class"]),
                            x["scale"], x["site_group"], x["source_id"], x["catalogue_row_id"]))
    for i, x in enumerate(out, 1):
        x["wpe_id"] = f"WPE-{i:05d}"
    return out, skipped


def _q(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * p
    lo = int(k)
    return xs[lo] + (xs[min(lo + 1, len(xs) - 1)] - xs[lo]) * (k - lo)


def summary_rows(ev: list[dict]) -> list[dict]:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for x in ev:
        groups[(x["parameter"], x["material_class"], x["scale"], x["site_group"])].append(x)
    out = []
    for (param, cls, scale, site), rows in groups.items():
        vals = [float(v) for x in rows for v in {x["si_min"], x["si_max"]} if v != ""]
        rec = {"param_id": f"{param}.{cls}", "parameter": param, "module": PARAMETERS[param][0],
               "quantity": PARAMETERS[param][1], "material_class": cls, "scale": scale, "site_group": site,
               "n_rows": len(rows), "n_sources": len({x["source_id"] for x in rows}),
               "n_converted": sum(1 for x in rows if x["si_min"] != ""), "si_unit": PARAMETERS[param][2],
               "si_min": "", "si_p25": "", "si_median": "", "si_p75": "", "si_max": "",
               "statuses": ";".join(f"{k}:{v}" for k, v in sorted(Counter(x["status"] for x in rows).items())),
               "sources": ";".join(sorted({x["source_id"] for x in rows})),
               "wpe_ids": ";".join(x["wpe_id"] for x in rows)}
        if vals:
            rec.update(si_min=f"{min(vals):.4g}", si_p25=f"{_q(vals, .25):.4g}", si_median=f"{statistics.median(vals):.4g}",
                       si_p75=f"{_q(vals, .75):.4g}", si_max=f"{max(vals):.4g}")
        out.append(rec)
    order = {s: i for i, s in enumerate(("FIELD", "MASSIF", "CALIBRATED_EFFECTIVE_MODEL", "DESIGN", "LAB"))}
    out.sort(key=lambda r: (list(PARAMETERS).index(r["parameter"]), MATERIAL_CLASSES.index(r["material_class"]),
                            order.get(r["scale"], 9), r["site_group"]))
    return out


# ------------------------------------------------------------------------------------------------ C1 geology
def thickness_rows(st: list[dict]) -> list[dict]:
    """Module C1: stratigraphic thickness observations (Phase-1 GEOLOGY_COORDS) with a number, per unit as named in the
    Phase-1 canonical vocabulary; mean when printed, else the printed min/max."""
    out = []
    for r in st:
        lo, hi, mean = _num(r["thickness_min_m"]), _num(r["thickness_max_m"]), _num(r["thickness_mean_m"])
        if lo is None and hi is None and mean is None:
            continue
        vals = [v for v in (lo, hi) if v is not None] or [mean]
        out.append({"obs_id": r["obs_id"], "unit_id": r["unit_id"], "unit": r["unit_name_canonical"] or r["unit_as_printed"],
                    "site_scope": r["scope"], "site_group": site_group(r["scope"]), "spatial_level": r["spatial_level"],
                    "observation_type": r["observation_type"], "thickness_min_m": f"{min(vals):.4g}",
                    "thickness_max_m": f"{max(vals):.4g}", "thickness_mean_m": "" if mean is None else f"{mean:.4g}",
                    "thickness_as_printed": r["thickness_as_printed"], "status": r["status"], "scale": r["scale"],
                    "transferability_to_SKRU1": r["transferability_to_SKRU1"], "source_id": r["source_id"],
                    "pdf_page": r["pdf_page"], "printed_page": r["printed_page"], "locator": r["locator"],
                    "secondary_copy_of": r["secondary_copy_of"]})
    out.sort(key=lambda x: (x["unit"], x["site_group"], x["source_id"], x["obs_id"]))
    return out


def thickness_summary(rows: list[dict]) -> list[dict]:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for x in rows:
        groups[(x["unit"], x["site_group"])].append(x)
    out = []
    for (unit, site), xs in sorted(groups.items()):
        lo = [float(x["thickness_min_m"]) for x in xs]
        hi = [float(x["thickness_max_m"]) for x in xs]
        means = [float(x["thickness_mean_m"]) for x in xs if x["thickness_mean_m"]]
        out.append({"unit": unit, "site_group": site, "n_rows": len(xs), "n_sources": len({x["source_id"] for x in xs}),
                    "n_primary": sum(1 for x in xs if not x["secondary_copy_of"]),
                    "thickness_min_m": f"{min(lo):.4g}", "thickness_max_m": f"{max(hi):.4g}",
                    "mean_of_means_m": f"{statistics.mean(means):.4g}" if means else "",
                    "statuses": ";".join(f"{k}:{v}" for k, v in sorted(Counter(x["status"] for x in xs).items())),
                    "sources": ";".join(sorted({x["source_id"] for x in xs})),
                    "obs_ids": ";".join(x["obs_id"] for x in xs)})
    return out


def _csv_bytes(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0]), lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


def build(canon: Path) -> dict[str, bytes]:
    src = canon / "MECH_RHEO" / "mechanics_evidence_catalog.csv"
    mech = list(csv.DictReader(open(src, encoding="utf-8")))
    ev, skipped = evidence_rows(mech)
    summ = summary_rows(ev)
    st_src = canon / "GEOLOGY_COORDS" / "stratigraphic_thickness_observations.csv"
    st = list(csv.DictReader(open(st_src, encoding="utf-8")))
    c1 = thickness_rows(st)
    c1s = thickness_summary(c1)
    files = {"world_parameter_evidence.csv": _csv_bytes(ev), "world_parameter_summary.csv": _csv_bytes(summ),
             "world_c1_thickness_evidence.csv": _csv_bytes(c1), "world_c1_thickness_summary.csv": _csv_bytes(c1s)}
    receipt = {"rule_version": RULE_VERSION, "inputs": {
                   "MECH_RHEO/mechanics_evidence_catalog.csv": {"sha256": hashlib.sha256(src.read_bytes()).hexdigest(),
                                                                "rows": len(mech)},
                   "GEOLOGY_COORDS/stratigraphic_thickness_observations.csv": {
                       "sha256": hashlib.sha256(st_src.read_bytes()).hexdigest(), "rows": len(st)}},
               "outputs": {k: {"sha256": hashlib.sha256(v).hexdigest()} for k, v in files.items()},
               "counts": {"evidence_rows": len(ev), "summary_rows": len(summ), "skipped": dict(sorted(skipped.items())),
                          "unit_not_converted": sum(1 for x in ev if x["si_min"] == ""),
                          "by_material_class": dict(Counter(x["material_class"] for x in ev).most_common()),
                          "by_parameter": dict(Counter(x["parameter"] for x in ev).most_common()),
                          "c1_thickness_rows": len(c1), "c1_summary_rows": len(c1s)},
               "material_classes": list(MATERIAL_CLASSES), "parameters": {k: list(v[:4]) for k, v in PARAMETERS.items()}}
    files["build_receipt.json"] = (json.dumps(receipt, ensure_ascii=False, indent=1) + "\n").encode("utf-8")
    return files


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="rebuild in memory and compare with the written files")
    args = ap.parse_args()
    res = os.environ.get("VKM_RESOURCES_ROOT")
    if not res:
        print("set VKM_RESOURCES_ROOT to the PRIVATE resources checkout", file=sys.stderr)
        return 2
    canon = Path(res) / "11_evidence_vnext" / "canonical"
    files = build(canon)
    out = canon / "WORLD_PARAMETERS"
    if args.check:
        bad = [k for k, v in files.items() if not (out / k).is_file() or (out / k).read_bytes() != v]
        print(json.dumps({"check": "FAIL" if bad else "PASS", "differs": bad}))
        return 1 if bad else 0
    out.mkdir(parents=True, exist_ok=True)
    for k, v in files.items():
        (out / k).write_bytes(v)
    print(files["build_receipt.json"].decode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

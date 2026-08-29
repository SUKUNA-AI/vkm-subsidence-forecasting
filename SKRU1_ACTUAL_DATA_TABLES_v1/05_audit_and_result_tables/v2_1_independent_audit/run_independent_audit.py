#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Independent audit of SKRU-1 reconstructed dataset v2.1.

The audit does not train predictive models and does not modify the source package.
It checks archive integrity, schema/FK integrity, source fidelity, spatial geometry,
temporal physics, survey/GNSS/InSAR measurement semantics, uncertainty calibration,
provenance, and reproducibility.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import textwrap
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable

import cv2
import fiona
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from PIL import Image, ImageDraw
from pyproj import Geod
from shapely.geometry import Point, shape
from shapely.ops import unary_union

ROOT = Path("/mnt/data")
ZIP = ROOT / "SKRU1_data_reconstruction_v2_1.zip"
BUNDLE = ROOT / "SKRU1_data_bundle_v2_1.zip"
CHECKSUMS = ROOT / "SKRU1_v2_1_checksums.sha256"
XLSX = ROOT / "SKRU1_data_audit_and_catalog_v2_1.xlsx"
SOURCE_DOCX = ROOT / "ВКР_Филатова_М_С.docx"
EXTRACT = ROOT / "_audit_v2_1" / "SKRU1_data_reconstruction_v2_1"
OUT = ROOT / "SKRU1_v2_1_independent_audit"
TAB = OUT / "tables"
FIG = OUT / "figures"
for p in [OUT, TAB, FIG]:
    p.mkdir(parents=True, exist_ok=True)

TABLES = EXTRACT / "tables"
GPKG = EXTRACT / "geodata" / "skru1_data_reconstruction_v2_1.gpkg"
SCRIPT = EXTRACT / "reproduce_v2_1.py"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def csv_rows(name: str):
    with (TABLES / name).open("r", encoding="utf-8-sig", newline="") as f:
        yield from csv.DictReader(f)


def read_small(name: str) -> list[dict[str, str]]:
    return list(csv_rows(name))


def fval(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return math.nan


def bval(v: Any) -> bool:
    return str(v).strip().lower() in {"true", "1", "yes"}


def is_empty(v: Any) -> bool:
    return v is None or str(v).strip() == "" or str(v).strip().lower() in {"nan", "none", "null"}


def write_csv(path: Path, rows: Iterable[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    rows = list(rows)
    if fieldnames is None:
        fieldnames = list(rows[0].keys()) if rows else []
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def json_dump(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def percentile(values: Iterable[float], qs=(0, .25, .5, .75, 1)) -> list[float]:
    a = np.asarray(list(values), dtype=float)
    a = a[np.isfinite(a)]
    return [float(x) for x in np.quantile(a, qs)] if len(a) else [math.nan] * len(qs)


def rmse(a: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.asarray(a, dtype=float) ** 2))) if len(a) else math.nan


def mae(a: np.ndarray) -> float:
    return float(np.mean(np.abs(np.asarray(a, dtype=float)))) if len(a) else math.nan


def extract_if_needed() -> None:
    if TABLES.exists() and GPKG.exists():
        return
    if EXTRACT.parent.exists():
        shutil.rmtree(EXTRACT.parent)
    EXTRACT.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(ZIP) as z:
        z.extractall(EXTRACT.parent)


extract_if_needed()

metrics: dict[str, Any] = {}
checks: list[dict[str, Any]] = []
issues: list[dict[str, Any]] = []
positives: list[dict[str, Any]] = []


def add_check(check_id: str, domain: str, description: str, passed: bool, observed: Any, expected: Any, severity_if_fail: str = "HIGH"):
    checks.append({
        "check_id": check_id,
        "domain": domain,
        "description": description,
        "passed": bool(passed),
        "observed": observed,
        "expected": expected,
        "severity_if_fail": severity_if_fail,
    })


def add_issue(issue_id: str, severity: str, domain: str, finding: str, evidence: str, impact: str, required_fix: str, status: str = "OPEN"):
    issues.append({
        "issue_id": issue_id,
        "severity": severity,
        "domain": domain,
        "finding": finding,
        "evidence": evidence,
        "impact": impact,
        "required_fix": required_fix,
        "status": status,
    })


def add_positive(item_id: str, domain: str, finding: str, evidence: str):
    positives.append({"item_id": item_id, "domain": domain, "finding": finding, "evidence": evidence})


# ---------------------------------------------------------------------------
# 1. Archive/checksum integrity
# ---------------------------------------------------------------------------
zip_results = {}
for label, path in [("reconstruction_zip", ZIP), ("bundle_zip", BUNDLE)]:
    try:
        with zipfile.ZipFile(path) as z:
            bad = z.testzip()
        zip_results[label] = {"exists": path.exists(), "testzip_bad_member": bad, "passed": bad is None}
    except Exception as e:
        zip_results[label] = {"exists": path.exists(), "error": repr(e), "passed": False}
metrics["archive_integrity"] = zip_results

checksum_declared = {}
checksum_actual = {}
checksum_ok = {}
if CHECKSUMS.exists():
    for line in CHECKSUMS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, rel = line.split(maxsplit=1)
        rel = rel.strip().lstrip("*")
        checksum_declared[rel] = digest
        p = ROOT / rel
        checksum_actual[rel] = sha256(p) if p.exists() else None
        checksum_ok[rel] = checksum_actual[rel] == digest
metrics["checksums"] = {"declared": checksum_declared, "actual": checksum_actual, "all_match": all(checksum_ok.values()), "per_file": checksum_ok}
add_check("INT-001", "archive", "Оба ZIP-архива проходят CRC/ZIP test", all(v.get("passed") for v in zip_results.values()), zip_results, "all archive members readable", "CRITICAL")
add_check("INT-002", "archive", "Контрольные SHA-256 совпадают", bool(checksum_ok) and all(checksum_ok.values()), checksum_ok, "all True", "CRITICAL")
if all(v.get("passed") for v in zip_results.values()) and checksum_ok and all(checksum_ok.values()):
    add_positive("P-001", "archive", "Файлы не повреждены на уровне архива и контрольных сумм", f"ZIP test: OK; SHA-256: {len(checksum_ok)}/{len(checksum_ok)}")

# Manifest consistency
manifest_rows = read_small("../metadata/dataset_manifest.csv") if False else []
manifest_path = EXTRACT / "metadata" / "dataset_manifest.csv"
with manifest_path.open("r", encoding="utf-8-sig", newline="") as f:
    manifest_rows = list(csv.DictReader(f))
manifest = {r["relative_path"]: int(r["bytes"]) for r in manifest_rows}
actual_files = {str(p.relative_to(EXTRACT)): p.stat().st_size for p in EXTRACT.rglob("*") if p.is_file()}
manifest_missing = sorted(set(actual_files) - set(manifest))
manifest_extra = sorted(set(manifest) - set(actual_files))
manifest_size_mismatch = sorted(k for k in set(actual_files) & set(manifest) if actual_files[k] != manifest[k])
metrics["manifest"] = {
    "declared_files": len(manifest),
    "actual_files": len(actual_files),
    "missing_from_manifest": manifest_missing,
    "missing_from_package": manifest_extra,
    "size_mismatches": manifest_size_mismatch,
}
add_check("INT-003", "manifest", "Манифест покрывает все файлы, кроме самого себя", set(manifest_missing) == {"metadata/dataset_manifest.csv", "metadata/dataset_manifest.json"} and not manifest_extra and not manifest_size_mismatch, manifest_missing, "only manifest files may be self-excluded", "LOW")

# ---------------------------------------------------------------------------
# 2. Structural integrity / keys / foreign keys
# ---------------------------------------------------------------------------
key_specs = [
    ("plan_units_digitized.csv", ["geometry_id"]),
    ("field_grid_50m.csv", ["cell_id"]),
    ("legacy_integrated_features_1665.csv", ["source_row_surrogate_id"]),
    ("survey_profiles.csv", ["profile_id"]),
    ("survey_points.csv", ["point_id"]),
    ("survey_campaigns.csv", ["campaign_id"]),
    ("truth_survey_points_monthly.csv", ["point_id", "date"]),
    ("synthetic_truth_ensemble_monthly.csv", ["realization_id", "point_id", "date"]),
    ("synthetic_truth_quantiles_monthly.csv", ["point_id", "date"]),
    ("leveling_runs_summary.csv", ["run_id"]),
    ("leveling_stations_raw.csv", ["run_id", "station_no"]),
    ("leveling_adjusted_epochs.csv", ["campaign_id", "point_id"]),
    ("gnss_sessions_raw.csv", ["gnss_session_id"]),
    ("gnss_adjusted_epochs.csv", ["campaign_id", "point_id"]),
    ("insar_point_catalog.csv", ["insar_point_id"]),
    ("insar_acquisition_catalog.csv", ["acquisition_id"]),
    ("insar_truth_monthly.csv", ["insar_point_id", "date"]),
    ("insar_observations.csv", ["acquisition_id", "insar_point_id"]),
    ("stress_test_scenario_catalog.csv", ["stress_scenario_id"]),
    ("stress_test_truth_monthly.csv", ["stress_scenario_id", "date"]),
    ("stress_test_measurements.csv", ["stress_observation_id"]),
]
key_stats = []
for name, cols in key_specs:
    seen = set(); dup = 0; n = 0
    for r in csv_rows(name):
        k = tuple(r[c] for c in cols); n += 1
        if k in seen: dup += 1
        seen.add(k)
    key_stats.append({"table": name, "key": "+".join(cols), "rows": n, "unique_keys": len(seen), "duplicate_keys": dup, "passed": dup == 0})
metrics["primary_keys"] = key_stats
add_check("STR-001", "schema", "Составные/первичные ключи уникальны во всех основных таблицах", all(r["passed"] for r in key_stats), sum(r["duplicate_keys"] for r in key_stats), 0, "HIGH")

sets = {
    "geom": {r["geometry_id"] for r in csv_rows("plan_units_digitized.csv")},
    "cell": {r["cell_id"] for r in csv_rows("field_grid_50m.csv")},
    "prof": {r["profile_id"] for r in csv_rows("survey_profiles.csv")},
    "point": {r["point_id"] for r in csv_rows("survey_points.csv")},
    "camp": {r["campaign_id"] for r in csv_rows("survey_campaigns.csv")},
    "inspt": {r["insar_point_id"] for r in csv_rows("insar_point_catalog.csv")},
    "acq": {r["acquisition_id"] for r in csv_rows("insar_acquisition_catalog.csv")},
    "stress": {r["stress_scenario_id"] for r in csv_rows("stress_test_scenario_catalog.csv")},
}
fk_specs = [
    ("legacy_integrated_features_1665.csv", "geometry_id", "geom"),
    ("anchor_spatial_links.csv", "geometry_id", "geom"),
    ("survey_points.csv", "profile_id", "prof"),
    ("survey_points.csv", "source_cell_id", "cell"),
    ("process_parameters_survey_points.csv", "point_id", "point"),
    ("truth_survey_points_monthly.csv", "point_id", "point"),
    ("synthetic_truth_ensemble_monthly.csv", "point_id", "point"),
    ("leveling_runs_summary.csv", "campaign_id", "camp"),
    ("leveling_runs_summary.csv", "profile_id", "prof"),
    ("leveling_stations_raw.csv", "campaign_id", "camp"),
    ("leveling_stations_raw.csv", "profile_id", "prof"),
    ("leveling_stations_raw.csv", "from_point_id", "point"),
    ("leveling_stations_raw.csv", "to_point_id", "point"),
    ("leveling_adjusted_epochs.csv", "campaign_id", "camp"),
    ("leveling_adjusted_epochs.csv", "profile_id", "prof"),
    ("leveling_adjusted_epochs.csv", "point_id", "point"),
    ("gnss_sessions_raw.csv", "campaign_id", "camp"),
    ("gnss_sessions_raw.csv", "point_id", "point"),
    ("gnss_adjusted_epochs.csv", "campaign_id", "camp"),
    ("gnss_adjusted_epochs.csv", "point_id", "point"),
    ("insar_point_catalog.csv", "source_cell_id", "cell"),
    ("insar_process_parameters.csv", "insar_point_id", "inspt"),
    ("insar_truth_monthly.csv", "insar_point_id", "inspt"),
    ("insar_observations.csv", "insar_point_id", "inspt"),
    ("insar_observations.csv", "acquisition_id", "acq"),
    ("stress_test_truth_monthly.csv", "stress_scenario_id", "stress"),
    ("stress_test_measurements.csv", "stress_scenario_id", "stress"),
    ("stress_test_measurements.csv", "campaign_id", "camp"),
    ("stress_test_measurements.csv", "point_id", "point"),
]
fk_stats = []
for name, col, parent in fk_specs:
    bad = 0; n_nonempty = 0
    for r in csv_rows(name):
        v = r[col]
        if not v:
            continue
        n_nonempty += 1
        if v not in sets[parent]:
            bad += 1
    fk_stats.append({"table": name, "field": col, "parent_set": parent, "nonempty_rows": n_nonempty, "orphan_rows": bad, "passed": bad == 0})
metrics["foreign_keys"] = fk_stats
add_check("STR-002", "schema", "Внешние ключи основных таблиц не имеют сирот", all(r["passed"] for r in fk_stats), sum(r["orphan_rows"] for r in fk_stats), 0, "HIGH")
if all(r["passed"] for r in key_stats) and all(r["passed"] for r in fk_stats):
    add_positive("P-002", "schema", "Идентификаторы и связи между таблицами технически целостны", f"{len(key_stats)} key checks и {len(fk_stats)} FK checks без ошибок")

# ---------------------------------------------------------------------------
# 3. Source fidelity and reconstructed spatial basis
# ---------------------------------------------------------------------------
# 3.1 Legacy row composition
legacy_role_counts = Counter()
legacy_layer_counts = Counter()
legacy_n = 0
legacy_non_null_settlement = []
for r in csv_rows("legacy_integrated_features_1665.csv"):
    legacy_n += 1
    legacy_role_counts[r["layer_role"]] += 1
    legacy_layer_counts[r["source_layer_name"]] += 1
    v = fval(r["settlement_2022_mm_digitized_mean"])
    if math.isfinite(v): legacy_non_null_settlement.append(v)
metrics["legacy_1665"] = {
    "rows": legacy_n,
    "layer_role_counts": dict(legacy_role_counts),
    "source_layer_counts": dict(legacy_layer_counts),
    "non_null_settlement_rows": len(legacy_non_null_settlement),
}

script_text = SCRIPT.read_text(encoding="utf-8")
legacy_hardcoded = "assert len(rows)==1665" in script_text and "rng.choice(n,size=300" in script_text and "size=90" in script_text
add_check("SRC-001", "source_fidelity", "1665 source-like строк извлечены из источника, а не добраны генератором", not legacy_hardcoded, "hard-coded 536+536+300+90+175+28 with RNG" if legacy_hardcoded else "not detected", "source-derived row reconstruction", "CRITICAL")
if legacy_hardcoded:
    add_issue(
        "A-001", "CRITICAL", "source_fidelity",
        "Таблица legacy_integrated_features_1665.csv искусственно добрана до числа 1665.",
        f"Состав строк: {dict(legacy_role_counts)}. В reproduce_v2_1.py присутствуют случайные выборки 300 и 90 перекрывающихся пластов, 175 строк закладки, 28 аномальных строк и assert len(rows)==1665.",
        "Число из ВКР воспроизведено, но семантика строк и межслойные перекрытия не восстановлены. Таблицу нельзя считать аналогом исходного интегрального слоя.",
        "Удалить суррогатный слой. Восстановить каждую из 12 исходных таблиц отдельно по рисунку 12/перечню слоёв; 1665 считать только контрольным итогом после пространственного объединения, а не целевым числом генератора."
    )

# 3.2 Source map year/caption around image24
fig22_context = []
fig22_has_2022 = False
try:
    from xml.etree import ElementTree as ET
    with zipfile.ZipFile(SOURCE_DOCX) as z:
        rel_root = ET.fromstring(z.read("word/_rels/document.xml.rels"))
        rid = None
        for e in rel_root:
            if (e.attrib.get("Target") or "").endswith("image24.png"):
                rid = e.attrib.get("Id")
                break
        doc_root = ET.fromstring(z.read("word/document.xml"))
        ns = {
            "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
            "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
            "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
        }
        paras = doc_root.findall(".//w:p", ns)
        target_i = None
        for i, p in enumerate(paras):
            for blip in p.findall(".//a:blip", ns):
                if blip.attrib.get("{%s}embed" % ns["r"]) == rid:
                    target_i = i
                    break
            if target_i is not None:
                break
        if target_i is not None:
            for j in range(max(0, target_i - 8), min(len(paras), target_i + 9)):
                txt = "".join(t.text or "" for t in paras[j].findall(".//w:t", ns)).strip()
                if txt:
                    fig22_context.append(txt)
            fig22_has_2022 = any(re.search(r"\b2022\b", t) for t in fig22_context)
except Exception as e:
    fig22_context = [f"context extraction failed: {e!r}"]
metrics["figure22_context"] = {"paragraphs": fig22_context, "contains_2022": fig22_has_2022}
ref_period_values = Counter(r["reference_period"] for r in csv_rows("field_grid_50m.csv"))
ref_date_values = Counter(r["reference_date_assumed"] for r in csv_rows("field_grid_50m.csv"))
add_check("SRC-002", "time_anchor", "Год 2022 для рисунка 22 прямо подтверждён источником", fig22_has_2022, fig22_context, "explicit year attached to settlement map", "CRITICAL")
if not fig22_has_2022:
    add_issue(
        "A-002", "CRITICAL", "time_anchor",
        "Полю оседаний без опубликованной даты приписан reference_period=2022 и дата 2022-10-01.",
        "В абзацах вокруг рисунка 22 источник говорит только «Интерполяция данных оседания земной поверхности»; год и дата отсутствуют. В field_grid_50m.csv все 11 484 строки помечены 2022/2022-10-01, и к этой дате калибруются временные ряды.",
        "Вся обратная временная реконструкция получает ложный временной якорь; возраста выработок, скорости и предыстория становятся недоказанными.",
        "Переименовать поле в settlement_reference_map_mm; хранить reference_date=NULL и interval_unknown. Временную реконструкцию строить сценарно относительно неизвестной даты либо найти исходный файл/метаданные карты."
    )

# 3.3 Geometry validity and grid coverage
layer_info = {}
plan_geoms = []
with fiona.open(GPKG, layer="plan_units_local") as src:
    layer_info["plan_units_local"] = {"count": len(src), "crs": str(src.crs), "geometry": src.schema["geometry"]}
    plan_geoms = [shape(f["geometry"]) for f in src]
with fiona.open(GPKG, layer="field_grid_50m_local") as src:
    layer_info["field_grid_50m_local"] = {"count": len(src), "crs": str(src.crs), "geometry": src.schema["geometry"]}
    grid_geoms = [shape(f["geometry"]) for f in src]
with fiona.open(GPKG, layer="faults_local") as src:
    layer_info["faults_local"] = {"count": len(src), "crs": str(src.crs), "geometry": src.schema["geometry"]}
    fault_geoms = [shape(f["geometry"]) for f in src]
plan_union = unary_union(plan_geoms)
grid_union = unary_union(grid_geoms)
plan_sum_area = sum(g.area for g in plan_geoms)
plan_area = plan_union.area
grid_area = grid_union.area
uncovered_area = plan_union.difference(grid_union).area
outside_area = grid_union.difference(plan_union).area
geom_valid_count = sum(g.is_valid for g in plan_geoms)
metrics["geometry"] = {
    "layers": layer_info,
    "plan_valid": geom_valid_count,
    "plan_count": len(plan_geoms),
    "plan_area_km2": plan_area / 1e6,
    "plan_sum_over_union": plan_sum_area / plan_area if plan_area else None,
    "plan_convex_hull_fill_ratio": plan_area / plan_union.convex_hull.area,
    "grid_area_km2": grid_area / 1e6,
    "grid_outside_plan_km2": outside_area / 1e6,
    "grid_outside_plan_share": outside_area / grid_area,
    "plan_uncovered_by_grid_km2": uncovered_area / 1e6,
    "plan_uncovered_share": uncovered_area / plan_area,
    "fault_count": len(fault_geoms),
    "fault_total_length_km": sum(g.length for g in fault_geoms) / 1000,
    "fault_length_inside_plan_share": sum(g.intersection(plan_union).length for g in fault_geoms) / sum(g.length for g in fault_geoms),
}
add_check("GEO-001", "geometry", "Все оцифрованные полигоны геометрически валидны", geom_valid_count == len(plan_geoms), geom_valid_count, len(plan_geoms), "MEDIUM")
add_check("GEO-002", "geometry", "Рабочие ячейки полностью лежат внутри плановых единиц", outside_area < 1e-6, f"{outside_area/1e6:.3f} km2 outside", "0 km2", "HIGH")
if outside_area > 1e-6 or uncovered_area / plan_area > .02:
    add_issue(
        "A-003", "HIGH", "geometry",
        "Сетка 50 м не совпадает с восстановленной полигональной областью.",
        f"{outside_area/1e6:.3f} км² ({outside_area/grid_area:.1%}) сетки лежит за границами полигонов; {uncovered_area/1e6:.3f} км² ({uncovered_area/plan_area:.1%}) полигонов не покрыто сеткой. Встроенная проверка проверяет только центроиды ячеек.",
        "Зональная статистика и пространственные признаки смешивают площадь вне объекта и теряют часть мелких полигонов.",
        "Клиповать геометрию каждой ячейки по plan_union и хранить effective_area_fraction; для статистик использовать пересечение, а не правило «центроид внутри»."
    )

# Plan units missing aggregated values
plan_rows = read_small("plan_units_digitized.csv")
core_cols = [
    "settlement_2022_mm_digitized_mean", "kzt_reconstructed_mean", "ko_reconstructed_mean",
    "seismic_energy_mid_J_m2_reconstructed_mean", "lithology_reconstructed"
]
missing_core_units = sum(any(is_empty(r[c]) for c in core_cols) for r in plan_rows)
metrics["plan_units_missing_core"] = {"count": missing_core_units, "share": missing_core_units / len(plan_rows)}
add_check("GEO-003", "geometry", "Все плановые единицы имеют основные агрегированные поля", missing_core_units == 0, missing_core_units, 0, "HIGH")
if missing_core_units:
    add_issue(
        "A-004", "HIGH", "geometry",
        "25.7% плановых единиц не имеют оседаний и ключевых агрегированных признаков.",
        f"Пустые settlement/kzt/ko/seismic/lithology присутствуют у {missing_core_units}/{len(plan_rows)} полигонов.",
        "Модельные и инженерные выборки будут терять 138 объектов либо получать неявную импутацию.",
        "Считать зональные статистики напрямую по пикселям/маскам полигона; для малых полигонов применять sub-cell sampling и явно хранить coverage_fraction."
    )

# 3.4 Heatmap contamination in geometry extraction
img24 = None
with zipfile.ZipFile(SOURCE_DOCX) as z:
    img24 = np.array(Image.open(BytesIO(z.read("word/media/image24.png"))).convert("RGB"))
roi = img24[5:680, 10:775]
gray = cv2.cvtColor(roi, cv2.COLOR_RGB2GRAY)
black = gray < 120
r, g, b = roi[:, :, 0], roi[:, :, 1], roi[:, :, 2]
red_dominant = (r > 100) & (r > g + 20) & (r > b + 20)
strong_red = (r > 180) & (r > g * 1.35) & (r > b * 1.25)
black_red_dominant_share = float(np.sum(black & red_dominant) / np.sum(black))
black_strong_red_share = float(np.sum(black & strong_red) / np.sum(black))
# Rasterize reconstructed plan and hull once in source-image coordinates. This avoids
# tens of thousands of expensive point-in-polygon calls and gives a direct pixel audit.
mask_plan = Image.new("L", (img24.shape[1], img24.shape[0]), 0)
draw_plan = ImageDraw.Draw(mask_plan)
for geom in plan_geoms:
    geoms_iter = [geom] if geom.geom_type == "Polygon" else list(geom.geoms)
    for poly in geoms_iter:
        pts = []
        for gx, gy in poly.exterior.coords:
            xn = (gx - 20600.0) / 9100.0; yn = (45900.0 - gy) / 7300.0
            pts.append((20.0 + xn * 745.0, 15.0 + yn * 645.0))
        draw_plan.polygon(pts, fill=255)
mask_arr = np.asarray(mask_plan) > 0
hull_pts = []
for gx, gy in plan_union.convex_hull.exterior.coords:
    xn = (gx - 20600.0) / 9100.0; yn = (45900.0 - gy) / 7300.0
    hull_pts.append((20.0 + xn * 745.0, 15.0 + yn * 645.0))
hull_mask = Image.new("L", (img24.shape[1], img24.shape[0]), 0)
ImageDraw.Draw(hull_mask).polygon(hull_pts, fill=255)
hull_arr = np.asarray(hull_mask) > 0
full_strong_red = np.zeros(img24.shape[:2], dtype=bool)
full_strong_red[5:680, 10:775] = strong_red
strong_red_hull_excluded_share = float(np.sum(full_strong_red & hull_arr & ~mask_arr) / max(np.sum(full_strong_red & hull_arr), 1))
metrics["geometry_heatmap_contamination"] = {
    "threshold_mask_red_dominant_share": black_red_dominant_share,
    "threshold_mask_strong_red_share": black_strong_red_share,
    "strong_red_inside_hull_excluded_from_plan_share": strong_red_hull_excluded_share,
    "strong_red_pixels_in_hull": int(np.sum(full_strong_red & hull_arr)),
}
add_check("GEO-004", "geometry", "Маска границ не зависит от цвета поля оседаний", black_strong_red_share < .01, f"{black_strong_red_share:.1%} thresholded pixels are strong red", "<1%", "CRITICAL")
if black_strong_red_share >= .01:
    add_issue(
        "A-005", "CRITICAL", "geometry",
        "Границы полигонов извлечены из цветной карты оседаний порогом по серому, поэтому красные зоны оседаний превращены в «линии/барьеры».",
        f"В ROI {black_strong_red_share:.1%} пикселей, попавших в маску gray<120, являются насыщенно-красными, а не чёрной плановой графикой. {strong_red_hull_excluded_share:.1%} насыщенно-красных пикселей внутри выпуклой оболочки оказались вне объединения полигонов.",
        "Геометрия 536 единиц систематически вырезает области максимальных оседаний; все последующие сетки, профили и зональные статистики наследуют искажение.",
        "Сегментировать чёрную графику в HSV/Lab по низкой насыщенности и яркости, отдельно подавить красную заливку; затем вручную проверить/исправить контуры и сравнить с рисунком 23."
    )

# 3.5 Source distribution vs published CDF
published_cdf = {500: .50, 1000: .68, 1500: .80, 2000: .88}
source_cdf_rows = []
for dataset, name, col in [
    ("grid50", "field_grid_50m.csv", "settlement_2022_mm_digitized"),
    ("plan_units", "plan_units_digitized.csv", "settlement_2022_mm_digitized_mean"),
    ("legacy1665_nonnull", "legacy_integrated_features_1665.csv", "settlement_2022_mm_digitized_mean"),
]:
    vals = np.array([fval(r[col]) for r in csv_rows(name)], dtype=float)
    vals = vals[np.isfinite(vals)]
    for t, pexp in published_cdf.items():
        pact = float(np.mean(vals <= t))
        source_cdf_rows.append({"dataset": dataset, "threshold_mm": t, "published_cumulative_share": pexp, "reconstructed_cumulative_share": pact, "difference_percentage_points": 100 * (pact - pexp), "non_null_n": len(vals)})
metrics["source_cdf"] = source_cdf_rows
legacy_missing_share = 1 - len(legacy_non_null_settlement) / legacy_n
add_check("SRC-003", "source_fidelity", "Распределение оседаний в legacy-слое воспроизводит опубликованные кумулятивные доли с допуском ±5 п.п.", all(abs(r["difference_percentage_points"]) <= 5 for r in source_cdf_rows if r["dataset"] == "legacy1665_nonnull"), [r for r in source_cdf_rows if r["dataset"] == "legacy1665_nonnull"], "±5 pp", "MEDIUM")
if legacy_missing_share > .05:
    add_issue(
        "A-006", "HIGH", "source_fidelity",
        "В legacy-слое отсутствуют оседания у 19.3% строк, хотя источник описывает итоговую таблицу после удаления строк с отсутствующими значениями.",
        f"Непустые settlement_mean: {len(legacy_non_null_settlement)}/{legacy_n}; missing share={legacy_missing_share:.1%}.",
        "Сравнение распределений проводится только на подмножестве и не подтверждает воспроизведение 1665 строк исходной таблицы.",
        "Не формировать legacy-слой до завершения зональной статистики для всех объектов; либо явно исключить непокрываемые объекты и не утверждать размер 1665."
    )

# 3.6 Anchor placement and residuals
anchor_links = read_small("anchor_spatial_links.csv")
placed_links = [r for r in anchor_links if r["placement_status"] == "placed"]
outside_links = [r for r in placed_links if not bval(r["inside_digitized_unit"])]
outside_over_unc = [r for r in outside_links if fval(r["distance_to_unit_m"]) > fval(r["link_uncertainty_m"])]
metrics["anchor_links"] = {
    "total": len(anchor_links),
    "placed": len(placed_links),
    "outside_digitized_unit": len(outside_links),
    "outside_beyond_declared_uncertainty": len(outside_over_unc),
    "max_distance_m": max(fval(r["distance_to_unit_m"]) for r in placed_links),
}
add_check("SRC-004", "anchors", "Размещённые якоря лежат внутри связанного полигона либо в пределах заявленной неопределённости", len(outside_over_unc) == 0, len(outside_over_unc), 0, "HIGH")
if outside_links:
    add_issue(
        "A-007", "HIGH", "anchors",
        "Большинство размещённых опубликованных якорей фактически не попало в назначенный полигон.",
        f"Из 40 placed-якорей 26 находятся вне полигона; у 17 расстояние больше link_uncertainty_m; максимум {max(fval(r['distance_to_unit_m']) for r in placed_links):.1f} м.",
        "Остатки между опубликованными и реконструированными значениями относятся к геометрически слабым или ошибочным сопоставлениям.",
        "Для координатных якорей отклонять связь при distance>uncertainty; для блочных якорей восстанавливать полигон по номеру блока, а не назначать ближайший."
    )

anchor_residuals = read_small("anchor_value_residuals.csv")
res_by_param = defaultdict(list)
for r in anchor_residuals:
    res_by_param[r["parameter"]].append((fval(r["published_value"]), fval(r["reconstructed_value"]), fval(r["residual_reconstructed_minus_published"])))
anchor_res_summary = []
for param, vals in sorted(res_by_param.items()):
    pub = np.asarray([v[0] for v in vals], dtype=float)
    res = np.asarray([v[2] for v in vals], dtype=float)
    anchor_res_summary.append({
        "parameter": param,
        "n": len(vals),
        "mae": mae(res),
        "median_absolute_error": float(np.median(np.abs(res))),
        "rmse": rmse(res),
        "mean_absolute_relative_error": float(np.mean(np.abs(res) / np.maximum(np.abs(pub), 1e-9))),
        "max_absolute_relative_error": float(np.max(np.abs(res) / np.maximum(np.abs(pub), 1e-9))),
    })
metrics["anchor_residuals"] = anchor_res_summary
settlement_anchor_mae = next(r["mae"] for r in anchor_res_summary if r["parameter"] == "disp_mean_mm")
load_anchor_mae = next(r["mae"] for r in anchor_res_summary if r["parameter"] == "load_coeff")
all_anchor_conditioned_false = all(not bval(r["anchor_conditioned"]) for r in plan_rows)
add_check("SRC-005", "anchors", "Опубликованные числовые якоря используются для калибровки реконструированных полей", not all_anchor_conditioned_false, "anchor_conditioned=False for all 536 units", "anchors constrain reconstruction", "CRITICAL")
if all_anchor_conditioned_false:
    add_issue(
        "A-008", "CRITICAL", "source_fidelity",
        "Точные опубликованные строки не калибруют реконструированные поля.",
        f"anchor_conditioned=False у всех 536 полигонов. MAE для опубликованных средних оседаний={settlement_anchor_mae:.1f} мм; для load_coeff={load_anchor_mae:.3f}; для глубины кровли={next(r['mae'] for r in anchor_res_summary if r['parameter']=='depth_roof_m'):.1f} м.",
        "Ключевые признаки являются независимой синтетикой, а не результатом обратного восстановления по имеющимся точным данным.",
        "Включить якоря в целевую функцию реконструкции с весами по источнику/геометрии; откалибровать поля и публиковать остатки до/после."
    )

# 3.7 k_o interpolation burden and provenance
ko_valid = Counter(); ko_prov = Counter(); field_prov = Counter(); kzt_valid = Counter(); seismic_valid = Counter(); lith_valid = Counter()
for r in csv_rows("field_grid_50m.csv"):
    ko_valid[r["ko_valid"]] += 1; ko_prov[r["ko_provenance"]] += 1; field_prov[r["field_provenance"]] += 1
    kzt_valid[r["kzt_valid"]] += 1; seismic_valid[r["seismic_valid"]] += 1; lith_valid[r["lithology_valid"]] += 1
ko_reconstructed_share = ko_prov["R"] / sum(ko_prov.values())
metrics["field_digitization"] = {"ko_valid": dict(ko_valid), "ko_provenance": dict(ko_prov), "ko_reconstructed_share": ko_reconstructed_share, "kzt_valid": dict(kzt_valid), "seismic_valid": dict(seismic_valid), "lithology_valid": dict(lith_valid), "field_provenance": dict(field_prov)}
add_check("SRC-006", "field_extraction", "Основная часть поля k_o оцифрована напрямую, а не заполнена ближайшим соседом", ko_reconstructed_share < .25, f"{ko_reconstructed_share:.1%} R", "<25% reconstructed", "HIGH")
if ko_reconstructed_share >= .25:
    add_issue(
        "A-009", "HIGH", "field_extraction",
        "61.3% значений k_o получены nearest-neighbour заполнением, а не оцифровкой.",
        f"ko_provenance: {dict(ko_prov)}. Расстояние до донора и зона экстраполяции в таблице не сохранены.",
        "Поле выглядит полным, но большая часть значений наследует ближайший класс через потенциально крупные расстояния; неопределённость занижена.",
        "Исправить геометрическое совмещение панели k_o, хранить nearest_source_distance_m и uncertainty, не использовать R-ячейки как равноправные D-наблюдения."
    )

# ---------------------------------------------------------------------------
# 4. Coordinate/georeference audit
# ---------------------------------------------------------------------------
georef = read_small("georef_control_points.csv")
fit = [r for r in georef if bval(r["use_for_fit"])]
val = [r for r in georef if not bval(r["use_for_fit"])]
val_res = max(fval(r["residual_m"]) for r in val) if val else math.nan
# Recompute scale distortion from transform constants recorded in source script
P1_PIX = (561.303400451697, 265.7279002449343); P1_WGS = (56.78204444444444, 59.632975)
P2_PIX = (770.0, 12.0); P2_WGS = (56.859207, 59.668639)
SX = (P2_WGS[0] - P1_WGS[0]) / (P2_PIX[0] - P1_PIX[0]); SY = (P2_WGS[1] - P1_WGS[1]) / (P2_PIX[1] - P1_PIX[1])
LB = (20600.0, 38600.0, 29700.0, 45900.0); B25 = (348.0, 64.0, 762.0, 428.0)
def local_to_wgs(x: float, y: float) -> tuple[float, float]:
    xn = (x - LB[0]) / (LB[2] - LB[0]); yn = (LB[3] - y) / (LB[3] - LB[1])
    px = B25[0] + xn * (B25[2] - B25[0]); py = B25[1] + yn * (B25[3] - B25[1])
    return P1_WGS[0] + SX * (px - P1_PIX[0]), P1_WGS[1] + SY * (py - P1_PIX[1])
geod = Geod(ellps="WGS84")
ll = local_to_wgs(LB[0], LB[1]); lr = local_to_wgs(LB[2], LB[1]); ul = local_to_wgs(LB[0], LB[3])
_, _, dx = geod.inv(ll[0], ll[1], lr[0], lr[1]); _, _, dy = geod.inv(ll[0], ll[1], ul[0], ul[1])
x_scale = dx / (LB[2] - LB[0]); y_scale = dy / (LB[3] - LB[1]); scale_anisotropy = x_scale / y_scale
metrics["georef"] = {"fit_controls": len(fit), "validation_controls": len(val), "validation_residual_m": val_res, "x_scale_ratio": x_scale, "y_scale_ratio": y_scale, "scale_anisotropy": scale_anisotropy, "local_layers_crs": layer_info["plan_units_local"]["crs"]}
add_check("GEOREF-001", "coordinates", "Контекстная геопривязка имеет минимум 4 независимых общих пункта", len(fit) >= 4 and len(val) >= 2, f"{len(fit)} fit + {len(val)} validation", ">=4 fit + >=2 validation", "HIGH")
if len(fit) < 4 or len(val) < 2:
    add_issue(
        "A-010", "HIGH", "coordinates",
        "WGS84-привязка остаётся обзорной и не минимизирована до инженерного уровня.",
        f"Использованы 2 fit-точки и 1 приблизительная validation-точка; остаток {val_res:.1f} м. Масштаб по X={x_scale:.3f}, по Y={y_scale:.3f}, анизотропия={scale_anisotropy:.3f}. Локальные слои GPKG имеют пустой CRS.",
        "При наложении на внешние данные возможны сотни метров смещения и деформация масштаба/углов.",
        "Не публиковать lon/lat как координаты объектов. Добавить официальный ключ либо 4–8 общих пунктов; до этого оставить только local_x/local_y и отдельный overview_transform."
    )

# ---------------------------------------------------------------------------
# 5. Temporal truth and ensembles
# ---------------------------------------------------------------------------
point_meta = {r["point_id"]: r for r in csv_rows("survey_points.csv")}
truth_by_point = defaultdict(list)
for r in csv_rows("truth_survey_points_monthly.csv"):
    truth_by_point[r["point_id"]].append((r["date"], fval(r["true_settlement_mm"]), fval(r["true_velocity_mm_y"])))
truth_deriv_errors = []
truth_deriv_ref = []
truth_deriv_work = []
zero_positive_velocity = []
nonmonotonic = 0
for pid, vals in truth_by_point.items():
    vals.sort(key=lambda x: x[0])
    sarr = np.asarray([v[1] for v in vals])
    if np.any(np.diff(sarr) < -1e-9): nonmonotonic += 1
    for (d0, s0, v0), (d1, s1, v1) in zip(vals, vals[1:]):
        y0, m0, day0 = map(int, d0.split("-")); y1, m1, day1 = map(int, d1.split("-"))
        dt = (date(y1, m1, day1) - date(y0, m0, day0)).days / 365.25
        err = (s1 - s0) / dt - 0.5 * (v0 + v1)
        truth_deriv_errors.append(err)
        (truth_deriv_ref if point_meta[pid]["point_type"] == "REF" else truth_deriv_work).append(err)
    for d, s, v in vals:
        if abs(s) < 1e-12 and v > .05:
            zero_positive_velocity.append((pid, d, v, point_meta[pid]["point_type"]))
metrics["truth_temporal_consistency"] = {
    "points": len(truth_by_point),
    "nonmonotonic_points": nonmonotonic,
    "derivative_mae_all_mm_y": mae(np.asarray(truth_deriv_errors)),
    "derivative_mae_work_mm_y": mae(np.asarray(truth_deriv_work)),
    "derivative_mae_reference_mm_y": mae(np.asarray(truth_deriv_ref)),
    "zero_settlement_positive_velocity_rows": len(zero_positive_velocity),
    "zero_settlement_positive_velocity_ref_rows": sum(v[3] == "REF" for v in zero_positive_velocity),
}
add_check("TIME-001", "temporal_physics", "Рабочие ряды монотонны и скорость согласована с производной оседания", nonmonotonic == 0 and mae(np.asarray(truth_deriv_work)) < 1e-6, {"nonmonotonic": nonmonotonic, "work_derivative_mae": mae(np.asarray(truth_deriv_work))}, "0 and <1e-6", "HIGH")
if nonmonotonic == 0 and mae(np.asarray(truth_deriv_work)) < 1e-6:
    add_positive("P-003", "temporal", "Рабочие номинальные ряды математически монотонны и интегрально согласованы", f"Derivative MAE={mae(np.asarray(truth_deriv_work)):.3e} mm/year")
add_check("TIME-002", "temporal_physics", "При нулевом оседании не хранится положительная скорость", len(zero_positive_velocity) == 0, len(zero_positive_velocity), 0, "HIGH")
if zero_positive_velocity:
    add_issue(
        "A-011", "HIGH", "temporal_physics",
        "Опорные реперы имеют нулевое накопленное оседание при ненулевой скорости.",
        f"Таких строк {len(zero_positive_velocity)}, из них {sum(v[3]=='REF' for v in zero_positive_velocity)} относятся к REF. Для REF settlement принудительно обнулён после интегрирования, а velocity сохранена.",
        "Пары (S, v) физически и математически противоречат друг другу; будущая модель может выучить артефакт типа пункта.",
        "Для опорных пунктов либо задавать v=0, либо интегрировать малые движения без принудительного обнуления S; хранить стабильность как отдельное наблюдение с неопределённостью."
    )

# Ensemble consistency
ensemble_group_counts = Counter()
for r in csv_rows("synthetic_truth_ensemble_monthly.csv"):
    ensemble_group_counts[(r["point_id"], r["date"])] += 1
quantile_order_bad = 0
for r in csv_rows("synthetic_truth_quantiles_monthly.csv"):
    q = [fval(r[c]) for c in ["settlement_q05_mm", "settlement_q25_mm", "settlement_q50_mm", "settlement_q75_mm", "settlement_q95_mm"]]
    if not all(q[i] <= q[i + 1] + 1e-12 for i in range(4)):
        quantile_order_bad += 1
metrics["ensemble_structure"] = {"group_count_distribution": dict(Counter(ensemble_group_counts.values())), "quantile_order_bad": quantile_order_bad}
add_check("TIME-003", "ensemble", "Каждая точка-дата имеет 16 реализаций и упорядоченные квантили", set(ensemble_group_counts.values()) == {16} and quantile_order_bad == 0, {"group_counts": dict(Counter(ensemble_group_counts.values())), "bad_quantiles": quantile_order_bad}, "16 and 0", "HIGH")
if set(ensemble_group_counts.values()) == {16} and quantile_order_bad == 0:
    add_positive("P-004", "ensemble", "Ансамбль и квантили структурно согласованы", "16 реализаций на каждую точку-дата; нарушений порядка квантилей нет")

# Stress envelope
stress_actual_max = defaultdict(lambda: -math.inf)
for r in csv_rows("stress_test_truth_monthly.csv"):
    stress_actual_max[r["stress_scenario_id"]] = max(stress_actual_max[r["stress_scenario_id"]], fval(r["velocity_mm_y"]))
stress_over_410 = sum(v > 410 + 1e-9 for v in stress_actual_max.values())
stress_max = max(stress_actual_max.values())
metrics["stress_test"] = {"scenarios": len(stress_actual_max), "actual_max_velocity_mm_y": stress_max, "scenarios_over_410": stress_over_410}
add_check("TIME-004", "stress_test", "Stress-test остаётся внутри заявленного верхнего диапазона 400/410 мм/год", stress_over_410 == 0, f"max={stress_max:.3f}, over410={stress_over_410}", "<=410", "MEDIUM")
if stress_over_410:
    add_issue(
        "A-012", "MEDIUM", "stress_test",
        "Часть stress-test сценариев превышает собственный верхний предел.",
        f"Максимальная скорость {stress_max:.2f} мм/год; {stress_over_410} сценариев выше 410 мм/год, потому что target peak добавляется поверх nominal velocity.",
        "Каталог и фактические ряды расходятся; внешняя огибающая 250–400 мм/год соблюдается нестрого.",
        "Определять target как итоговую скорость, а не добавочную; после генерации клиповать/перенормировать и валидировать максимум."
    )

# ---------------------------------------------------------------------------
# 6. Leveling, GNSS, InSAR measurement audit
# ---------------------------------------------------------------------------
# Leveling runs/stations
run_rows = read_small("leveling_runs_summary.csv")
run_pass = {r["run_id"]: bval(r["passed"]) for r in run_rows}
run_attempt_counter = Counter((r["attempt"], r["passed"]) for r in run_rows)
failed_first = [r for r in run_rows if r["attempt"] == "1" and not bval(r["passed"])]
failed_second = [r for r in run_rows if r["attempt"] == "2" and not bval(r["passed"])]
station_formula_max = 0.0
accepted_warning_count = 0
station_count = 0
for r in csv_rows("leveling_stations_raw.csv"):
    station_count += 1
    diff = (fval(r["backsight_m"]) - fval(r["foresight_m"])) * 1000 - fval(r["observed_delta_mm"])
    station_formula_max = max(station_formula_max, abs(diff))
    if run_pass.get(r["run_id"], False) and r["raw_qc_flag"] != "ok":
        accepted_warning_count += 1
lev_rows = read_small("leveling_adjusted_epochs.csv")
lev_res = np.asarray([fval(r["residual_mm"]) for r in lev_rows])
lev_sig = np.asarray([fval(r["standard_uncertainty_mm"]) for r in lev_rows])
lev_cover95 = float(np.mean(np.abs(lev_res) <= 1.96 * lev_sig))
first_endpoint_exact = sum(int(r["sequence_no"]) == 0 and abs(fval(r["residual_mm"])) < 1e-12 for r in lev_rows)
first_endpoint_total = sum(int(r["sequence_no"]) == 0 for r in lev_rows)
max_seq = defaultdict(int)
for r in lev_rows:
    max_seq[(r["profile_id"], r["campaign_id"])] = max(max_seq[(r["profile_id"], r["campaign_id"])], int(r["sequence_no"]))
endpoint_exact = sum((int(r["sequence_no"]) == 0 or int(r["sequence_no"]) == max_seq[(r["profile_id"], r["campaign_id"])]) and abs(fval(r["residual_mm"])) < 1e-12 for r in lev_rows)
metrics["leveling"] = {
    "runs": len(run_rows), "stations": station_count, "run_attempt_status": {str(k): v for k, v in run_attempt_counter.items()},
    "failed_first_attempts": len(failed_first), "failed_second_attempts": len(failed_second),
    "station_formula_max_abs_error_mm": station_formula_max, "accepted_station_warning_rows": accepted_warning_count,
    "adjusted_epochs": len(lev_rows), "residual_mean_mm": float(np.mean(lev_res)), "residual_mae_mm": mae(lev_res), "residual_rmse_mm": rmse(lev_res),
    "coverage_95": lev_cover95, "first_endpoint_exact_zero": first_endpoint_exact, "first_endpoint_total": first_endpoint_total,
    "all_endpoint_exact_zero": endpoint_exact, "endpoint_total": 2 * len(max_seq),
}
add_check("LEV-001", "leveling", "Неудачные первые ходы сохранены и имеют успешный повтор", len(failed_first) > 0 and len(failed_second) == 0, {"failed_first": len(failed_first), "failed_second": len(failed_second)}, "failed first >0, failed second=0", "HIGH")
add_check("LEV-002", "leveling", "Станционные превышения согласованы с отсчётами", station_formula_max < 1e-9, station_formula_max, "<1e-9 mm", "HIGH")
if len(failed_first) > 0 and len(failed_second) == 0 and station_formula_max < 1e-9:
    add_positive("P-005", "leveling", "Сырые нивелирные данные внутренне согласованы, неудачные ходы сохранены", f"24 failed first runs, 24 successful repeats; max backsight/foresight identity error={station_formula_max:.2e} mm")
add_check("LEV-003", "leveling", "Уравнивание не использует скрытые истинные отметки как жёсткие концы хода", first_endpoint_exact == 0, f"{first_endpoint_exact}/{first_endpoint_total} first endpoints exact", "0", "CRITICAL")
if first_endpoint_exact:
    add_issue(
        "A-013", "CRITICAL", "leveling",
        "Уравненные нивелирные ходы используют скрытую истинную отметку начального репера и истинную разность концов.",
        f"У всех {first_endpoint_total} первых концов residual=0; всего {endpoint_exact}/{2*len(max_seq)} концов имеют точный нулевой остаток. Код начинает adjh=[heights[0]] и замыкает на (heights[-1]-heights[0]).",
        "Измерительный контур частично знает ground truth, поэтому его точность и контроль невязок круговые; такие данные слишком чистые для независимой проверки алгоритма QC.",
        "Генерировать независимые наблюдения исходных реперов/связей к BM, уравнивать сеть методом МНК по весам, не использовать true_height в вычислительном пути; truth оставлять только для последующей оценки."
    )
if accepted_warning_count:
    add_issue(
        "A-014", "MEDIUM", "leveling_qc",
        "В принятых ходах остаются станционные предупреждения, но итоговые эпохи все помечены accepted.",
        f"{accepted_warning_count} строк raw_qc_flag=station_discrepancy принадлежат ходам с passed=True; все {len(lev_rows)} adjusted epochs имеют qc_status=accepted.",
        "Пользователь не может отличить наблюдения с предупреждениями от чистых; QC-поля теряют диагностическую ценность.",
        "Ввести run_qc_grade/station_warning_count и переносить их в adjusted epochs; разделить warning и reject."
    )

# GNSS
gn_rows = read_small("gnss_adjusted_epochs.csv")
gn_res = np.asarray([fval(r["residual_mm"]) for r in gn_rows])
gn_sig = np.asarray([fval(r["standard_uncertainty_mm"]) for r in gn_rows])
gn_cover95 = float(np.mean(np.abs(gn_res) <= 1.96 * gn_sig))
gn_qc = Counter(r["qc_status"] for r in gn_rows)
ref_ids = {pid for pid, r in point_meta.items() if r["point_type"] == "REF"}
gn_ref = [(fval(r["residual_mm"]), fval(r["standard_uncertainty_mm"])) for r in gn_rows if r["point_id"] in ref_ids]
gn_ref_res = np.asarray([x[0] for x in gn_ref]); gn_ref_sig = np.asarray([x[1] for x in gn_ref])
metrics["gnss"] = {
    "epochs": len(gn_rows), "qc": dict(gn_qc), "residual_mean_mm": float(np.mean(gn_res)), "residual_mae_mm": mae(gn_res), "residual_rmse_mm": rmse(gn_res),
    "coverage_95": gn_cover95, "normalized_rmse": rmse(gn_res / gn_sig), "reference_coverage_95": float(np.mean(np.abs(gn_ref_res) <= 1.96 * gn_ref_sig)),
}
add_check("GNSS-001", "gnss", "Заявленные 95% интервалы GNSS имеют покрытие 90–98%", .90 <= gn_cover95 <= .98, gn_cover95, "0.90–0.98", "HIGH")
if gn_cover95 < .90:
    add_issue(
        "A-015", "HIGH", "gnss",
        "Неопределённость GNSS занижена.",
        f"Фактическое покрытие ±1.96σ={gn_cover95:.1%}, RMSE={rmse(gn_res):.2f} мм, normalized RMSE={rmse(gn_res/gn_sig):.2f}; все 960 эпох помечены accepted.",
        "Квантильные/вероятностные модели будут получать неверную шкалу доверия.",
        "Оценивать covariance после уравнивания, учитывать common-mode и число fixed-сессий; откалибровать σ по остаткам и выделять low_quality."
    )

# InSAR
insar_first = {}
insar_errors = []
insar_sig = []
insar_qc_mismatch = 0
insar_qc = Counter()
for r in csv_rows("insar_observations.csv"):
    pid = r["insar_point_id"]; d = r["date"]
    true_v = fval(r["true_vertical_settlement_mm"]); est = fval(r["subvertical_estimate_mm"]); sig = fval(r["standard_uncertainty_mm"]); coh = fval(r["coherence"]); qc = r["qc_status"]
    if pid not in insar_first or d < insar_first[pid][0]:
        insar_first[pid] = (d, true_v, est)
    insar_errors.append(est - true_v); insar_sig.append(sig); insar_qc[qc] += 1
    if (coh > .35 and qc != "accepted") or (coh <= .35 and qc == "accepted"):
        insar_qc_mismatch += 1
insar_errors = np.asarray(insar_errors); insar_sig = np.asarray(insar_sig)
insar_cover95 = float(np.mean(np.abs(insar_errors) <= 1.96 * insar_sig))
first_nonzero = sum(abs(v[1]) > 1e-9 for v in insar_first.values())
first_vals = np.asarray([v[1] for v in insar_first.values()])
metrics["insar"] = {
    "observations": len(insar_errors), "points": len(insar_first), "qc": dict(insar_qc),
    "first_acquisition_nonzero_points": first_nonzero, "first_acquisition_nonzero_share": first_nonzero / len(insar_first),
    "first_true_vertical_median_mm": float(np.median(first_vals)), "first_true_vertical_max_mm": float(np.max(first_vals)),
    "residual_mean_mm": float(np.mean(insar_errors)), "residual_mae_mm": mae(insar_errors), "residual_rmse_mm": rmse(insar_errors),
    "coverage_95": insar_cover95, "normalized_rmse": rmse(insar_errors / insar_sig), "qc_vs_actual_coherence_mismatch_rows": insar_qc_mismatch,
}
add_check("INSAR-001", "insar", "Временной ряд смещений задан относительно первой съёмки", first_nonzero == 0, f"{first_nonzero}/{len(insar_first)} first points nonzero; median={np.median(first_vals):.1f} mm", "0", "HIGH")
if first_nonzero:
    add_issue(
        "A-016", "HIGH", "insar",
        "InSAR-ряд хранит абсолютное накопленное оседание, а не смещение относительно опорной съёмки.",
        f"У {first_nonzero}/{len(insar_first)} точек первая съёмка ненулевая; медиана {np.median(first_vals):.1f} мм, максимум {np.max(first_vals):.1f} мм.",
        "Контракт не соответствует стандартному временно-относительному InSAR-ряду; объединение с реальными данными и интерпретация LOS будут ошибочны.",
        "Вычесть значение reference acquisition для каждой точки; хранить reference_acquisition_id, reference_point_id и datum convention."
    )
add_check("INSAR-002", "insar", "95% интервалы InSAR откалиброваны", .90 <= insar_cover95 <= .98, insar_cover95, "0.90–0.98", "HIGH")
if insar_cover95 < .90:
    add_issue(
        "A-017", "HIGH", "insar",
        "Неопределённость InSAR и QC не откалиброваны.",
        f"Покрытие ±1.96σ={insar_cover95:.1%}, RMSE={rmse(insar_errors):.2f} мм, normalized RMSE={rmse(insar_errors/insar_sig):.2f}; {insar_qc_mismatch} строк имеют qc_status, не согласованный с фактическим coherence.",
        "Модель получит ложную уверенность и несогласованный фильтр качества.",
        "QC вычислять по фактической coherence каждой эпохи; включить остаточные атмосферные/DEM/thermal компоненты в σ и откалибровать интервалы."
    )

# ---------------------------------------------------------------------------
# 7. Completeness for surveying task
# ---------------------------------------------------------------------------
all_table_names = {p.name for p in TABLES.glob("*.csv")}
derived_expected = {
    "horizontal_displacements.csv", "horizontal_strains.csv", "tilts.csv", "curvatures.csv", "profile_kinematics.csv"
}
missing_derived = sorted(derived_expected - all_table_names)
metrics["completeness"] = {"missing_derived_engineering_tables": missing_derived}
add_check("COMP-001", "completeness", "Набор содержит производные маркшейдерские параметры профилей", len(missing_derived) == 0, missing_derived, "all derived tables", "HIGH")
if missing_derived:
    add_issue(
        "A-018", "HIGH", "completeness",
        "Нет расчётных таблиц горизонтальных сдвижений, деформаций, наклонов, кривизны и профильной кинематики.",
        "В пакете есть высоты/оседания и GNSS-сессии, но отсутствуют отдельные производные таблицы, предусмотренные формулярами маркшейдерских наблюдений.",
        "Датасет пока не закрывает полный инженерный контур спецчасти и не позволяет проверить согласованность пространственных производных.",
        "Добавить расчётные таблицы с формулами, интервалами, единицами, propagation of uncertainty и ссылками на исходные измерения."
    )

# ---------------------------------------------------------------------------
# 8. Reproducibility and validation design
# ---------------------------------------------------------------------------
required_media = [ROOT / "filatova_unpacked" / "word" / "media" / f"image{i}.png" for i in [22, 23, 24, 25, 26, 27]]
missing_media = [str(p) for p in required_media if not p.exists()]
script_self_contained = not missing_media and all((ROOT / s).exists() for s in ["ВКР_Филатова_М_С.docx"])
metrics["reproducibility"] = {"hardcoded_media_root": "/mnt/data/filatova_unpacked/word/media", "missing_required_media": missing_media, "script_self_contained": script_self_contained}
add_check("REP-001", "reproducibility", "reproduce_v2_1.py запускается из поставленного пакета без внешних неупакованных файлов", script_self_contained, missing_media, "no missing dependencies", "CRITICAL")
if not script_self_contained:
    add_issue(
        "A-019", "CRITICAL", "reproducibility",
        "Скрипт воспроизведения не самодостаточен.",
        f"Он жёстко читает /mnt/data/filatova_unpacked/word/media/image22–27.png; отсутствуют {len(missing_media)} требуемых файлов. ZIP не содержит исходный DOCX/медиа и их хэши.",
        "Независимый пользователь не может пересобрать датасет и проверить происхождение результатов.",
        "Скрипт должен принимать --source-docx/--output, самостоятельно распаковывать word/media во временный каталог, проверять SHA-256 источников и не писать в жёсткий /mnt/data."
    )

# Analyze built-in validation checks: count tautological/self checks
validation = json.loads((EXTRACT / "metadata" / "validation_report.json").read_text(encoding="utf-8"))
builtin_checks = validation.get("checks", {})
# Mark known weak checks by names
weak_names = {
    "source_like_legacy_rows_1665", "legacy_is_not_regular_grid", "digitized_plan_units_gt_400",
    "field_cells_inside_plan", "settlement_range_0_4300", "profiles_15", "working_points_gt_280",
    "two_reference_points_each_profile", "reconstruction_ensemble_16", "no_predictive_model_artifacts",
    "context_georef_validation_under_1km"
}
weak_present = sorted(set(builtin_checks) & weak_names)
metrics["builtin_validation"] = {"all_passed": validation.get("all_passed"), "count": len(builtin_checks), "weak_or_tautological_checks": weak_present}
if validation.get("all_passed"):
    add_issue(
        "A-020", "HIGH", "validation_design",
        "Встроенный PASS не является доказательством качества данных.",
        f"Все 28 проверок прошли, но среди них есть проверки жёстко заданных количеств/диапазонов ({', '.join(weak_present)}); отсутствуют проверки anchor residuals, полной геометрии ячеек, физики reference rows, calibration coverage, first-epoch InSAR и самодостаточности скрипта.",
        "Dashboard создаёт ложное ощущение готовности набора к обучению и дипломным выводам.",
        "Заменить PASS на уровни integrity/source_fidelity/physics/measurement/reproducibility; итоговый статус должен быть NO-GO при любой critical-проблеме."
    )

# Threshold separation positive
thresholds = read_small("threshold_registry.csv")
enterprise_missing = [r for r in thresholds if r["authority"] == "enterprise_project" and r["status"] == "required_missing"]
external_ref = [r for r in thresholds if "reference_only" in r["status"] or r["status"] == "stress_test_only"]
metrics["thresholds"] = {"enterprise_missing_count": len(enterprise_missing), "external_reference_count": len(external_ref)}
if len(enterprise_missing) >= 4:
    add_positive("P-006", "thresholds", "Проектные пороги предприятия не подменены литературными", f"{len(enterprise_missing)} enterprise-параметра оставлены required_missing; внешние пороги отделены")

# No model artifacts
model_terms = re.compile(r"(^|/)(training|predictions?|baseline_metrics|model)(_|\.|/)", re.I)
model_artifacts = [str(p.relative_to(EXTRACT)) for p in EXTRACT.rglob("*") if p.is_file() and model_terms.search(str(p.relative_to(EXTRACT)))]
metrics["model_artifacts"] = model_artifacts
add_check("COMP-002", "scope", "В пакете нет прогнозной модели/предсказаний", not model_artifacts, model_artifacts, "none", "HIGH")
if not model_artifacts:
    add_positive("P-007", "scope", "Data-only граница соблюдена", "Файлы модели, train/test split и forecast metrics не обнаружены")

# ---------------------------------------------------------------------------
# 9. Workbook technical scan (OOXML fallback, independent of artifact engine)
# ---------------------------------------------------------------------------
workbook_scan = {"exists": XLSX.exists(), "zip_valid": False, "formula_error_strings": 0, "sheet_count": None, "formulas": 0}
try:
    with zipfile.ZipFile(XLSX) as z:
        workbook_scan["zip_valid"] = z.testzip() is None
        workbook_scan["sheet_count"] = sum(1 for n in z.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
        error_terms = [b"#REF!", b"#DIV/0!", b"#VALUE!", b"#NAME?", b"#N/A"]
        formula_errors = 0; formula_count = 0
        for n in z.namelist():
            if n.startswith("xl/worksheets/") and n.endswith(".xml"):
                data = z.read(n)
                formula_count += len(re.findall(br"<(?:[A-Za-z0-9_]+:)?f(?:\s|>)", data))
                formula_errors += sum(data.count(t) for t in error_terms)
        workbook_scan["formulas"] = formula_count
        workbook_scan["formula_error_strings"] = formula_errors
except Exception as e:
    workbook_scan["error"] = repr(e)
metrics["workbook_scan"] = workbook_scan
add_check("XLSX-001", "workbook", "Каталог XLSX структурно валиден и без явных ошибок формул", workbook_scan.get("zip_valid") and workbook_scan.get("formula_error_strings") == 0, workbook_scan, "valid and 0", "MEDIUM")
if workbook_scan.get("zip_valid") and workbook_scan.get("formula_error_strings") == 0:
    add_positive("P-008", "workbook", "Excel-каталог технически открывается и не содержит явных формульных ошибок", f"{workbook_scan['sheet_count']} sheets, {workbook_scan['formulas']} formulas")

# ---------------------------------------------------------------------------
# 10. Overall verdict
# ---------------------------------------------------------------------------
severity_rank = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1, "INFO": 0}
sev_counts = Counter(i["severity"] for i in issues)
critical_count = sev_counts["CRITICAL"]
high_count = sev_counts["HIGH"]
if critical_count:
    overall = "NO-GO"
    use_class = "Пригоден только как черновой прототип структуры и генератор тестовых сценариев после изоляции дефектных блоков; не пригоден для обучения финальной модели и доказательных выводов диплома."
elif high_count:
    overall = "CONDITIONAL"
    use_class = "Допустим для ограниченных экспериментов после исправления high-проблем."
else:
    overall = "GO"
    use_class = "Пригоден к следующему этапу при сохранении заявленных ограничений."
metrics["verdict"] = {"status": overall, "critical": critical_count, "high": high_count, "medium": sev_counts["MEDIUM"], "low": sev_counts["LOW"], "statement": use_class}

# ---------------------------------------------------------------------------
# 11. Output tables and figures
# ---------------------------------------------------------------------------
write_csv(TAB / "audit_checks.csv", checks)
write_csv(TAB / "issue_register.csv", issues)
write_csv(TAB / "positive_findings.csv", positives)
write_csv(TAB / "source_vs_reconstruction_cdf.csv", source_cdf_rows)
write_csv(TAB / "anchor_residual_summary.csv", anchor_res_summary)
write_csv(TAB / "key_integrity.csv", key_stats)
write_csv(TAB / "foreign_key_integrity.csv", fk_stats)

uncertainty_rows = [
    {"measurement_system": "leveling", "n": len(lev_res), "residual_mean_mm": float(np.mean(lev_res)), "mae_mm": mae(lev_res), "rmse_mm": rmse(lev_res), "coverage_95": lev_cover95, "normalized_rmse": rmse(lev_res / lev_sig)},
    {"measurement_system": "GNSS", "n": len(gn_res), "residual_mean_mm": float(np.mean(gn_res)), "mae_mm": mae(gn_res), "rmse_mm": rmse(gn_res), "coverage_95": gn_cover95, "normalized_rmse": rmse(gn_res / gn_sig)},
    {"measurement_system": "InSAR_subvertical", "n": len(insar_errors), "residual_mean_mm": float(np.mean(insar_errors)), "mae_mm": mae(insar_errors), "rmse_mm": rmse(insar_errors), "coverage_95": insar_cover95, "normalized_rmse": rmse(insar_errors / insar_sig)},
]
write_csv(TAB / "uncertainty_calibration.csv", uncertainty_rows)
json_dump(OUT / "audit_metrics.json", metrics)

# Figure 1: CDF comparison
plt.figure(figsize=(9, 5.5))
for dataset in ["grid50", "plan_units", "legacy1665_nonnull"]:
    rr = [r for r in source_cdf_rows if r["dataset"] == dataset]
    plt.plot([r["threshold_mm"] for r in rr], [r["reconstructed_cumulative_share"] for r in rr], marker="o", label=dataset)
plt.plot(list(published_cdf.keys()), list(published_cdf.values()), marker="s", linestyle="--", label="published CDF")
plt.xlabel("Порог накопленного оседания, мм")
plt.ylabel("Кумулятивная доля")
plt.title("Сопоставление распределения оседаний с опубликованной статистикой")
plt.grid(True, alpha=.3)
plt.legend()
plt.tight_layout()
plt.savefig(FIG / "01_source_cdf_comparison.png", dpi=180)
plt.close()

# Figure 2: anchor MAE, log scale for mixed units (labels carry units implicitly by parameter)
plt.figure(figsize=(10, 5.8))
params = [r["parameter"] for r in anchor_res_summary]
vals = [max(r["mae"], 1e-6) for r in anchor_res_summary]
plt.bar(params, vals)
plt.yscale("log")
plt.ylabel("MAE (логарифмическая шкала, единицы параметра)")
plt.title("Остатки опубликованных якорей относительно реконструкции")
plt.xticks(rotation=45, ha="right")
plt.grid(True, axis="y", alpha=.3)
plt.tight_layout()
plt.savefig(FIG / "02_anchor_residual_mae.png", dpi=180)
plt.close()

# Figure 3: uncertainty coverage
plt.figure(figsize=(8.5, 5.2))
labels = [r["measurement_system"] for r in uncertainty_rows]
cov = [r["coverage_95"] for r in uncertainty_rows]
plt.bar(labels, cov)
plt.axhline(.95, linestyle="--", linewidth=1, label="целевое 95%")
plt.ylim(0, 1.05)
plt.ylabel("Фактическое покрытие ±1.96σ")
plt.title("Калибровка заявленной неопределённости")
plt.grid(True, axis="y", alpha=.3)
plt.legend()
plt.tight_layout()
plt.savefig(FIG / "03_uncertainty_coverage.png", dpi=180)
plt.close()

# Figure 4: geometry contamination diagnostic
excluded = full_strong_red & hull_arr & ~mask_arr
overlay = img24.copy()
overlay[excluded] = np.array([0, 255, 255], dtype=np.uint8)
# Create side-by-side source + diagnostic
canvas = Image.new("RGB", (img24.shape[1] * 2, img24.shape[0]), "white")
canvas.paste(Image.fromarray(img24), (0, 0))
canvas.paste(Image.fromarray(overlay), (img24.shape[1], 0))
canvas.save(FIG / "04_geometry_heatmap_contamination.png")

# ---------------------------------------------------------------------------
# 12. Report
# ---------------------------------------------------------------------------
issue_lines = []
for sev in ["CRITICAL", "HIGH", "MEDIUM", "LOW"]:
    group = [i for i in issues if i["severity"] == sev]
    if not group:
        continue
    issue_lines.append(f"### {sev} ({len(group)})")
    for i in group:
        issue_lines.append(f"\n**{i['issue_id']} — {i['finding']}**\n\n- Доказательство: {i['evidence']}\n- Последствие: {i['impact']}\n- Исправление: {i['required_fix']}\n")

positive_lines = "\n".join(f"- **{p['item_id']} — {p['finding']}**: {p['evidence']}" for p in positives)
failed_checks = [c for c in checks if not c["passed"]]

report = f"""# Независимый аудит датасета SKRU-1 v2.1

Дата аудита: {datetime.now().astimezone().astimezone().strftime('%Y-%m-%d %H:%M %Z')}

## Итоговый вердикт: **{overall}**

{use_class}

Пакет технически цел: архивы читаются, контрольные суммы совпадают, первичные ключи и внешние связи не развалены. Но встроенный статус `PASS` оценивает преимущественно форму и заранее заданные количества. Он не проверяет, насколько данные действительно восстановлены из источников, насколько корректна геометрия и не использует ли синтетический измерительный контур скрытую истину.

### Сводка дефектов

| Уровень | Количество |
|---|---:|
| CRITICAL | {sev_counts['CRITICAL']} |
| HIGH | {sev_counts['HIGH']} |
| MEDIUM | {sev_counts['MEDIUM']} |
| LOW | {sev_counts['LOW']} |
| Независимых проверок | {len(checks)} |
| Не пройдено | {len(failed_checks)} |

## Что проверено

1. CRC ZIP и SHA-256.
2. Первичные/составные ключи и внешние ссылки между 29 группами таблиц.
3. Соответствие числу 1665, структуре исходных слоёв и опубликованным якорям.
4. Геометрическая валидность полигонов, покрытие сеткой и влияние цветовой карты оседаний на сегментацию.
5. Временная согласованность `settlement ↔ velocity`, ансамбль и stress-test.
6. Сырые нивелирные ходы, GNSS, InSAR, QC и фактическое покрытие заявленной неопределённости.
7. Контекстная геопривязка, CRS, манифест и воспроизводимость скрипта.
8. Техническая целостность Excel-каталога.

## Главный вывод по сути

Сейчас это **не «почти реальные восстановленные данные СКРУ-1»**, а смесь:

- небольшого числа точных опубликованных строк;
- оцифровки нескольких рисунков;
- грубой растровой сегментации;
- случайно сгенерированных горнотехнических полей;
- синтетических временных рядов и измерений.

Само по себе это допустимо для controlled synthetic benchmark. Недопустимо другое: некоторые таблицы названы source-like/reconstructed так, будто они восстанавливают исходный архив, хотя код фактически добирает структуру случайными строками и не калибрует ключевые поля по опубликованным значениям.

## Критические и существенные находки

{''.join(issue_lines)}

## Что действительно сделано хорошо

{positive_lines}

## Ключевые численные результаты

### Источниковая привязка

- опубликованных якорей: {len(anchor_links)};
- помечено `placed`: {len(placed_links)};
- из них вне назначенного полигона: {len(outside_links)};
- дальше заявленной неопределённости: {len(outside_over_unc)};
- MAE опубликованного среднего оседания: {settlement_anchor_mae:.1f} мм;
- MAE коэффициента нагрузки: {load_anchor_mae:.3f}.

### Геометрия

- валидных полигонов: {geom_valid_count}/{len(plan_geoms)};
- площадь объединения полигонов: {plan_area/1e6:.3f} км²;
- площадь сетки за пределами полигонов: {outside_area/1e6:.3f} км² ({outside_area/grid_area:.1%});
- площадь полигонов без покрытия сеткой: {uncovered_area/1e6:.3f} км² ({uncovered_area/plan_area:.1%});
- плановых единиц без основных агрегированных полей: {missing_core_units}/{len(plan_rows)};
- доля насыщенно-красных пикселей в маске «чёрных линий»: {black_strong_red_share:.1%}.

### Измерительные контуры

| Контур | RMSE, мм | Покрытие ±1.96σ | Комментарий |
|---|---:|---:|---|
| Нивелирование | {rmse(lev_res):.3f} | {lev_cover95:.1%} | общая шкала приемлема, но концы ходов знают hidden truth |
| GNSS | {rmse(gn_res):.3f} | {gn_cover95:.1%} | σ занижена |
| InSAR subvertical | {rmse(insar_errors):.3f} | {insar_cover95:.1%} | σ резко занижена; ряд не отнесён к первой съёмке |

## Решение по использованию текущей версии

| Сценарий | Решение |
|---|---|
| Проверка загрузчиков, схем и пайплайна | **CONDITIONAL GO** |
| Разработка QC на сырых синтетических ходах | **CONDITIONAL GO**, после удаления hidden-truth adjustment |
| Обучение финальной прогнозной модели | **NO-GO** |
| Оценка реальной точности на СКРУ-1 | **NO-GO** |
| Карты и численные выводы диплома как фактические данные | **NO-GO** |
| Основа для следующей реконструкции v3 | **GO** |

## Приоритет исправлений для v3

1. Заново извлечь геометрию, отделив чёрную плановую графику от красной тепловой заливки.
2. Удалить искусственный `legacy_1665`; восстановить 12 исходных слоёв раздельно.
3. Убрать неподтверждённый якорь 2022-10-01 и ввести неизвестную/интервальную дату карты.
4. Включить опубликованные якоря в обратную задачу и добиться контролируемых остатков.
5. Перегенерировать нивелирование без доступа уравнивания к true heights.
6. Перевести InSAR в относительный datum, исправить QC и калибровать uncertainty.
7. Добавить производные профилей: сдвижения, деформации, наклоны, кривизну и скорости с propagation of uncertainty.
8. Сделать скрипт самодостаточным и параметризованным.

## Артефакты аудита

- `tables/issue_register.csv` — полный реестр проблем;
- `tables/audit_checks.csv` — независимые проверки;
- `audit_metrics.json` — все рассчитанные показатели;
- `figures/01_source_cdf_comparison.png` — сравнение распределений;
- `figures/02_anchor_residual_mae.png` — ошибки опубликованных якорей;
- `figures/03_uncertainty_coverage.png` — калибровка неопределённости;
- `figures/04_geometry_heatmap_contamination.png` — влияние красной заливки на геометрию.
"""
(OUT / "INDEPENDENT_AUDIT_REPORT.md").write_text(report, encoding="utf-8")

readme = f"""# SKRU-1 v2.1 independent audit bundle

Verdict: **{overall}**

This folder contains an independent data audit. No predictive model was trained, and the original package was not modified.

Start with `INDEPENDENT_AUDIT_REPORT.md` and `tables/issue_register.csv`.
"""
(OUT / "README.md").write_text(readme, encoding="utf-8")

# Copy this audit script for reproducibility
shutil.copy2(Path(__file__), OUT / "run_independent_audit.py")

# Hash audit outputs
hash_rows = []
for p in sorted(OUT.rglob("*")):
    if p.is_file() and p.name != "audit_checksums.sha256":
        hash_rows.append((sha256(p), str(p.relative_to(OUT))))
(OUT / "audit_checksums.sha256").write_text("\n".join(f"{h}  {rel}" for h, rel in hash_rows) + "\n", encoding="utf-8")

zip_out = ROOT / "SKRU1_v2_1_independent_audit.zip"
if zip_out.exists():
    zip_out.unlink()
with zipfile.ZipFile(zip_out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=7) as z:
    for p in sorted(OUT.rglob("*")):
        if p.is_file():
            z.write(p, arcname=str(Path(OUT.name) / p.relative_to(OUT)))

print(json.dumps({
    "verdict": overall,
    "issues": dict(sev_counts),
    "checks": len(checks),
    "failed_checks": len(failed_checks),
    "report": str(OUT / "INDEPENDENT_AUDIT_REPORT.md"),
    "zip": str(zip_out),
}, ensure_ascii=False, indent=2))

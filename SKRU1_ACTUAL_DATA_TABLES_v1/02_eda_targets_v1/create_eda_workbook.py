from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import Any

os.environ.setdefault("ARTIFACT_TOOL_RPC_DAEMON_STARTUP_TIMEOUT_S", "180")

from artifact_tool import Workbook, SpreadsheetFile

ROOT = Path("/mnt/data/SKRU1_v3_2_EDA_targets_v1")
TABLES = ROOT / "tables"
TARGETS = ROOT / "target_tables"
META = ROOT / "metadata"
OUT = ROOT / "SKRU1_v3_2_EDA_and_targets.xlsx"
PREVIEW = ROOT / "figures" / "EDA_workbook_dashboard_preview.png"

NAVY = "#17365D"
BLUE = "#4472C4"
LIGHT_BLUE = "#D9EAF7"
GREEN = "#70AD47"
LIGHT_GREEN = "#E2F0D9"
ORANGE = "#C65911"
LIGHT_ORANGE = "#FCE4D6"
YELLOW = "#FFF2CC"
RED = "#C00000"
LIGHT_RED = "#F4CCCC"
PURPLE = "#8064A2"


def read_csv(path: Path, limit: int | None = None) -> list[list[Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    if limit is not None and len(rows) > limit + 1:
        rows = rows[: limit + 1]
    return rows


def coerce(value: Any) -> Any:
    if value is None or value == "":
        return None
    text = str(value)
    if text == "True":
        return True
    if text == "False":
        return False
    try:
        if any(token in text.lower() for token in (".", "e")):
            return float(text)
        return int(text)
    except Exception:
        return text


def col_letter(number: int) -> str:
    result = ""
    while number:
        number, rem = divmod(number - 1, 26)
        result = chr(65 + rem) + result
    return result


def title(sheet, text: str, subtitle: str, end_col: str = "N") -> None:
    sheet.merge_cells(f"A1:{end_col}1")
    sheet.get_range("A1").values = [[text]]
    sheet.get_range(f"A1:{end_col}1").format = {
        "fill": NAVY,
        "font": {"bold": True, "color": "#FFFFFF", "size": 17},
        "horizontal_alignment": "center",
        "vertical_alignment": "center",
        "row_height": 34,
    }
    sheet.merge_cells(f"A2:{end_col}2")
    sheet.get_range("A2").values = [[subtitle]]
    sheet.get_range(f"A2:{end_col}2").format = {
        "fill": LIGHT_BLUE,
        "font": {"italic": True},
        "wrap_text": True,
        "vertical_alignment": "center",
        "row_height": 34,
    }


def section(sheet, row: int, text: str, end_col: str = "H", fill: str = BLUE) -> None:
    sheet.merge_cells(f"A{row}:{end_col}{row}")
    sheet.get_range(f"A{row}").values = [[text]]
    sheet.get_range(f"A{row}:{end_col}{row}").format = {
        "fill": fill,
        "font": {"bold": True, "color": "#FFFFFF", "size": 12},
        "horizontal_alignment": "center",
        "vertical_alignment": "center",
        "row_height": 25,
    }


def table(sheet, rows: list[list[Any]], start_row: int, start_col: int, name: str,
          fill: str = BLUE, widths: list[float] | None = None) -> tuple[int, int]:
    ncols = max(len(row) for row in rows)
    matrix = []
    for row in rows:
        converted = [coerce(value) for value in row]
        converted += [None] * (ncols - len(converted))
        matrix.append(converted)
    nrows = len(matrix)
    start = f"{col_letter(start_col)}{start_row}"
    end = f"{col_letter(start_col + ncols - 1)}{start_row + nrows - 1}"
    sheet.get_range(f"{start}:{end}").values = matrix
    sheet.get_range(
        f"{col_letter(start_col)}{start_row}:{col_letter(start_col+ncols-1)}{start_row}"
    ).format = {
        "fill": fill,
        "font": {"bold": True, "color": "#FFFFFF"},
        "horizontal_alignment": "center",
        "vertical_alignment": "center",
        "wrap_text": True,
        "row_height": 32,
    }
    if nrows > 1:
        sheet.get_range(
            f"{col_letter(start_col)}{start_row+1}:{col_letter(start_col+ncols-1)}{start_row+nrows-1}"
        ).format = {"vertical_alignment": "top", "wrap_text": True}
    sheet.tables.add(f"{start}:{end}", True, name)
    for index in range(ncols):
        width = widths[index] if widths and index < len(widths) else 18
        sheet.get_range(f"{col_letter(start_col+index)}:{col_letter(start_col+index)}").format.column_width = width
    return nrows, ncols


keys = json.loads((META / "eda_key_numbers.json").read_text(encoding="utf-8"))
wb = Workbook.create()
sheets = [
    "Dashboard", "Overview", "Campaigns", "TargetSemantics", "PrimaryTarget",
    "Distributions", "EarlyWarning", "Features", "Lineage", "Correlations",
    "SplitDrift", "Kinematics", "SensorQC", "Baselines", "Issues",
    "TargetCatalog", "FeatureContract", "Validation", "TargetSample",
    "Sources", "ChartData",
]
for name in sheets:
    wb.worksheets.add(name)

# Dashboard
s = wb.worksheets.get_item("Dashboard")
title(s, "СКРУ-1 v3.2 — EDA и формальная постановка targets",
      "Primary unit: рабочий репер после уравнивания текущей кампании. "
      "T1 прогнозирует скорость до следующей плановой targeted-кампании; сорванный цикл цензурируется.")
s.merge_cells("A4:D4"); s.get_range("A4").values = [["Статус этапа"]]
s.get_range("A4:D4").format = {"fill": GREEN, "font": {"bold": True, "color": "#FFFFFF", "size": 13}, "horizontal_alignment": "center"}
s.merge_cells("A5:D6"); s.get_range("A5").values = [["GO для EDA, baseline и экспериментального pipeline\nNO-GO для production claims без реальных циклов"]]
s.get_range("A5:D6").format = {"fill": LIGHT_GREEN, "font": {"bold": True, "size": 12}, "horizontal_alignment": "center", "vertical_alignment": "center", "wrap_text": True}
s.merge_cells("F4:I4"); s.get_range("F4").values = [["Исправленная семантика"]]
s.get_range("F4:I4").format = {"fill": ORANGE, "font": {"bold": True, "color": "#FFFFFF", "size": 13}, "horizontal_alignment": "center"}
s.merge_cells("F5:I6"); s.get_range("F5").values = [[f"{keys['existing_not_next_planned']} origins в старой выборке перескакивают через плановый цикл.\nNext-available теперь только auxiliary."]]
s.get_range("F5:I6").format = {"fill": LIGHT_ORANGE, "font": {"bold": True, "size": 11}, "horizontal_alignment": "center", "vertical_alignment": "center", "wrap_text": True}

s.get_range("A8:L8").values = [["WORK", None, "Campaigns", None, "T1 labels", None, "Censored", None, "EW complete", None, "EW censored", None]]
s.get_range("A9").values = [[keys["work_points"]]]
s.get_range("C9").values = [[keys["campaigns"]]]
s.get_range("E9").values = [[keys["target_labels_planned"]]]
s.get_range("G9").values = [[keys["target_censored_planned"] + keys["formal_origins_observed_but_no_adjustment"]]]
s.get_range("I9").values = [[keys["early_complete"]]]
s.get_range("K9").values = [[keys["early_right_censored"]]]
for block in ("A8:B9", "C8:D9", "E8:F9", "G8:H9", "I8:J9", "K8:L9"):
    s.get_range(block).format = {"fill": LIGHT_BLUE, "horizontal_alignment": "center", "vertical_alignment": "center"}
s.get_range("A8:L8").format.font = {"bold": True}
s.get_range("A9:L9").format.font = {"bold": True, "size": 16}
section(s, 11, "Иерархия задач", "F", PURPLE)
table(s, [
    ["ID", "Назначение", "Статус"],
    ["T1", "Скорость до следующей плановой targeted-кампании", "PRIMARY"],
    ["T1B/T1C", "Приращение и next settlement", "DERIVED"],
    ["T2", "Скорость до следующего успешного измерения", "AUXILIARY"],
    ["T3", "Fixed horizon 180d", "SYNTHETIC EVAL"],
    ["T4", "Активизация в 180 суток", "SECONDARY"],
    ["T5", "Начало нового ускорения", "EARLY WARNING"],
    ["T6", "Профильная кинематика следующего полного цикла", "DERIVED"],
], 12, 1, "DashTargetHierarchy", PURPLE, [14, 48, 20])
section(s, 23, "Ключевые решения EDA", "F")
conclusions = [
    "Primary regression target — мм/год; приращение зависит от горизонта.",
    "Отрицательные observed labels не клиппируются: это реальный измерительный шум.",
    "1274 строки — только 98 траекторий; random row split запрещён.",
    "Static features используются с provenance/uncertainty и обязательной ablation.",
    "Early warning split выполняется по концу 180-дневного окна.",
    "Reference thresholds не являются enterprise risk labels.",
]
for idx, text in enumerate(conclusions, start=24):
    s.get_range(f"A{idx}").values = [[idx - 23]]
    s.merge_cells(f"B{idx}:F{idx}")
    s.get_range(f"B{idx}").values = [[text]]
    s.get_range(f"A{idx}:F{idx}").format = {"wrap_text": True, "vertical_alignment": "center"}
s.get_range("A24:A29").format.font = {"bold": True}
s.get_range("A:A").format.column_width = 18; s.get_range("B:B").format.column_width = 46
s.get_range("C:F").format.column_width = 16; s.get_range("G:N").format.column_width = 14
s.freeze_panes.freeze_rows(2)

# Overview
s = wb.worksheets.get_item("Overview")
title(s, "Структура набора", "Объёмы, зависимость наблюдений и список ограничений.")
table(s, read_csv(TABLES / "dataset_overview.csv"), 4, 1, "OverviewTable", BLUE, [36, 18, 36])
section(s, 22, "Реестр проблем", "H", ORANGE)
table(s, read_csv(META / "eda_issue_register.csv"), 23, 1, "OverviewIssues", ORANGE, [14, 12, 72, 72])
s.get_range("B24:B60").conditional_formats.add_custom('=B24="HIGH"', {"fill": LIGHT_RED, "font": {"color": RED, "bold": True}})
s.get_range("B24:B60").conditional_formats.add_custom('=B24="MEDIUM"', {"fill": YELLOW})
s.freeze_panes.freeze_rows(4)

# Campaigns
s = wb.worksheets.get_item("Campaigns")
title(s, "Кампании и coverage", "Полные и focused-циклы, нерегулярные интервалы и типизированные пропуски.")
table(s, read_csv(TABLES / "campaign_summary.csv"), 4, 1, "CampaignSummary", ORANGE, [13, 14, 15, 18, 14, 18, 18, 18, 19, 19])
s.get_range("I5:J60").format.number_format = "0.0%"
s.get_range("D5:D60").conditional_formats.add_data_bar({"gradient": True})
s.get_range("I5:J60").conditional_formats.add_color_scale({"minColor": "#F4CCCC", "midColor": "#FFF2CC", "maxColor": "#D9EAD3"})
section(s, 36, "Причины отсутствия", "F", ORANGE)
table(s, read_csv(TABLES / "campaign_missingness_by_reason.csv"), 37, 1, "CampaignMissingness", ORANGE, [26, 24, 12])
s.freeze_panes.freeze_rows(4)

# Target semantics
s = wb.worksheets.get_item("TargetSemantics")
title(s, "Семантика целевого горизонта", "Next successful observation не равен следующей плановой targeted-кампании.")
table(s, read_csv(TABLES / "target_semantics_comparison.csv"), 4, 1, "TargetSemanticsTable", PURPLE, [42, 16, 16, 14, 24, 18, 18, 68])
section(s, 10, "Контракт T1", "H", PURPLE)
table(s, [
    ["Элемент", "Определение"],
    ["Origin", "Уравнённая отметка рабочего репера после текущей кампании."],
    ["Target", "Первая будущая campaign, где point_id заранее имеет targeted=True."],
    ["Label", "365.25 × (η_target − η_current) / horizon_days, мм/год."],
    ["Missing", "Статус censored; запрещено перескакивать к следующему успеху."],
    ["Authority", "leveling_adjusted_epochs.csv, не membership-флаг сам по себе."],
], 11, 1, "T1ContractTable", PURPLE, [28, 96])
section(s, 20, "Observed membership без уравнённой эпохи", "H", ORANGE)
table(s, read_csv(TABLES / "observed_membership_without_adjusted_leveling.csv"), 21, 1, "MembershipMismatch", ORANGE)
s.freeze_panes.freeze_rows(4)

# Primary target
s = wb.worksheets.get_item("PrimaryTarget")
title(s, "T1_RATE_NEXT_PLANNED", "Распределение labels, horizons, uncertainty и sanity baselines.")
table(s, read_csv(TABLES / "formal_primary_target_summary_by_split.csv"), 4, 1, "PrimarySplitSummary", BLUE)
s.get_range("E5:I15").format.number_format = "0.000"
section(s, 10, "Срезы по горизонту", "H")
table(s, read_csv(TABLES / "formal_target_summary_by_horizon.csv"), 11, 1, "PrimaryHorizonSummary", BLUE)
s.get_range("D12:G50").format.number_format = "0.000"
section(s, 30, "Sanity baselines", "J", PURPLE)
table(s, read_csv(TABLES / "formal_target_sanity_baselines.csv"), 31, 1, "FormalBaselines", PURPLE)
s.get_range("E32:J70").format.number_format = "0.000"
s.get_range("E32:E70").conditional_formats.add_data_bar({"gradient": True})
s.freeze_panes.freeze_rows(4)

# Distributions
s = wb.worksheets.get_item("Distributions")
title(s, "Распределения и target noise", "Heavy tail, отрицательные noisy labels и проверка uncertainty calibration.")
table(s, read_csv(TABLES / "target_distribution_summary.csv"), 4, 1, "TargetDistribution", GREEN)
s.get_range("C5:O20").format.number_format = "0.000"
section(s, 13, "Калибровка target uncertainty", "H", GREEN)
table(s, read_csv(TABLES / "target_noise_calibration.csv"), 14, 1, "TargetNoise", GREEN, [30, 18, 18])
s.get_range("B15:B30").format.number_format = "0.0000"
section(s, 27, "Топ корреляций", "H")
table(s, read_csv(TABLES / "target_correlations.csv", 30), 28, 1, "TargetCorrelations30", BLUE)
s.get_range("D29:F70").format.number_format = "0.000"
s.freeze_panes.freeze_rows(4)

# Early warning
s = wb.worksheets.get_item("EarlyWarning")
title(s, "T4/T5 — раннее обнаружение ускорения", "Rare-event labels с sustained-condition и right-censoring неполных окон.")
table(s, read_csv(TABLES / "early_warning_formal_balance.csv"), 4, 1, "EarlyWarningBalance", ORANGE)
s.get_range("F5:H20").format.number_format = "0.00%"
section(s, 12, "Формальные критерии", "H", ORANGE)
table(s, [
    ["Label", "Positive condition", "Censoring"],
    ["T4 activity", "max Δv≥25 мм/год AND max a≥15 мм/год² AND v≥v0+20 минимум 2 месяца", "horizon complete"],
    ["T5 onset", "T4=1 и новое accelerating/reactivated/step_transition после origin", "ongoing event ≠ onset"],
], 13, 1, "EWContract", ORANGE, [22, 90, 44])
section(s, 19, "Текущее ограничение", "H", RED)
s.merge_cells("A20:H22"); s.get_range("A20").values = [[f"{keys['early_right_censored']} origins имеют неполный 180-дневный горизонт. Strict test содержит мало nominal positives; truth нужно продлить минимум до марта 2026 года либо сдвинуть границу."]]
s.get_range("A20:H22").format = {"fill": LIGHT_RED, "font": {"bold": True, "color": "#7F0000"}, "wrap_text": True, "vertical_alignment": "center"}
s.freeze_panes.freeze_rows(4)

# Features
s = wb.worksheets.get_item("Features")
title(s, "Feature completeness", "Missingness, field types и quality metadata.")
table(s, read_csv(TABLES / "feature_missingness.csv"), 4, 1, "FeatureMissingness", BLUE, [46, 14, 14, 18, 14])
s.get_range("D5:D100").format.number_format = "0.0%"
s.get_range("D5:D100").conditional_formats.add_color_scale({"minColor": "#D9EAD3", "midColor": "#FFF2CC", "maxColor": "#F4CCCC"})
s.freeze_panes.freeze_rows(4)

# Lineage
s = wb.worksheets.get_item("Lineage")
title(s, "Provenance, uncertainty и donor distance", "Реконструированные признаки оцениваются вместе с качеством происхождения.")
table(s, read_csv(TABLES / "feature_lineage_summary.csv"), 4, 1, "LineageSummary", BLUE, [30, 20, 24, 20, 18, 24, 22, 22, 38])
s.get_range("B5:H30").format.number_format = "0.000"
s.freeze_panes.freeze_rows(4)

# Correlations
s = wb.worksheets.get_item("Correlations")
title(s, "Correlations и persistence residual", "Static feature importance не считается причинной связью.")
table(s, read_csv(TABLES / "target_correlations.csv", 60), 4, 1, "CorrelationTop60", BLUE)
s.get_range("D5:F80").format.number_format = "0.000"
section(s, 68, "После вычитания last-rate baseline", "H", PURPLE)
table(s, read_csv(TABLES / "persistence_residual_correlations.csv"), 69, 1, "PersistenceResidual", PURPLE)
s.get_range("D70:E100").format.number_format = "0.000"
s.freeze_panes.freeze_rows(4)

# Drift
s = wb.worksheets.get_item("SplitDrift")
title(s, "Temporal split drift", "Test отличается длиной истории, горизонтом и уровнем накопленного оседания.")
table(s, read_csv(TABLES / "numeric_split_drift.csv"), 4, 1, "NumericDrift", ORANGE)
s.get_range("E5:H200").format.number_format = "0.000"
s.freeze_panes.freeze_rows(4)

# Kinematics
s = wb.worksheets.get_item("Kinematics")
title(s, "Маркшейдерские outputs", "Оседание, скорость, наклон, кривизна и горизонтальная деформация сохраняются отдельно.")
table(s, read_csv(TABLES / "kinematic_summary.csv"), 4, 1, "KinematicSummary", GREEN)
s.get_range("C5:O20").format.number_format = "0.000"
section(s, 13, "Reference-only tilt context", "H", ORANGE)
table(s, read_csv(TABLES / "profile_reference_threshold_exceedance.csv"), 14, 1, "TiltReference", ORANGE, [36, 18, 18, 36])
s.get_range("C15:C25").format.number_format = "0.00%"
section(s, 20, "T6 derivation rule", "H", PURPLE)
s.merge_cells("A21:H23"); s.get_range("A21").values = [[f"Создано {keys['profile_target_rows']} profile transitions. Предпочтительно прогнозировать points, затем агрегировать max η, max v, max |tilt|, max |curvature| и max |strain| на следующем полном цикле с coverage≥0.8."]]
s.get_range("A21:H23").format = {"fill": YELLOW, "wrap_text": True, "vertical_alignment": "center"}
s.freeze_panes.freeze_rows(4)

# SensorQC
s = wb.worksheets.get_item("SensorQC")
title(s, "Synthetic sensor QC", "Residuals против evaluation-only truth; не производственная верификация.")
table(s, read_csv(TABLES / "sensor_quality_summary.csv"), 4, 1, "SensorQuality", GREEN)
s.get_range("C5:G20").format.number_format = "0.000"
s.get_range("F5:F20").format.number_format = "0.0%"
s.freeze_panes.freeze_rows(4)

# Baselines
s = wb.worksheets.get_item("Baselines")
title(s, "Sanity baselines", "Проверка target frame, а не финальная модель диплома.")
table(s, read_csv(TABLES / "formal_target_sanity_baselines.csv"), 4, 1, "BaselineTable", PURPLE)
s.get_range("E5:J40").format.number_format = "0.000"
s.get_range("E5:E40").conditional_formats.add_data_bar({"gradient": True})
s.freeze_panes.freeze_rows(4)

# Issues
s = wb.worksheets.get_item("Issues")
title(s, "EDA issue register", "Действия перед финальным экспериментом.")
table(s, read_csv(META / "eda_issue_register.csv"), 4, 1, "IssuesTable", ORANGE, [14, 12, 78, 78])
s.get_range("B5:B40").conditional_formats.add_custom('=B5="HIGH"', {"fill": LIGHT_RED, "font": {"color": RED, "bold": True}})
s.get_range("B5:B40").conditional_formats.add_custom('=B5="MEDIUM"', {"fill": YELLOW})
s.freeze_panes.freeze_rows(4)

# Target catalog
s = wb.worksheets.get_item("TargetCatalog")
title(s, "Каталог targets", "T1–T6: scope, formula, labels, censoring and status.")
table(s, read_csv(TARGETS / "target_catalog.csv"), 4, 1, "TargetCatalogTable", PURPLE, [30, 20, 22, 26, 52, 70, 22, 46, 52, 74, 32])
s.freeze_panes.freeze_rows(4)

# Feature contract
s = wb.worksheets.get_item("FeatureContract")
title(s, "Feature contract", "Metadata retained for joins; estimator gets only allowed features.")
table(s, read_csv(TARGETS / "formal_feature_contract.csv"), 4, 1, "FeatureContractTable", PURPLE, [44, 22, 14, 92])
s.get_range("C5:C200").conditional_formats.add_custom('=C5=TRUE', {"fill": LIGHT_GREEN, "font": {"color": "#006100"}})
s.get_range("C5:C200").conditional_formats.add_custom('=C5=FALSE', {"fill": LIGHT_RED, "font": {"color": "#9C0006"}})
s.freeze_panes.freeze_rows(4)

# Validation
s = wb.worksheets.get_item("Validation")
title(s, "Formal target checks", "12/12 checks passed.")
table(s, read_csv(META / "target_validation_checks.csv"), 4, 1, "ValidationTable", GREEN, [14, 14, 14, 28, 28, 76])
s.get_range("C5:C40").conditional_formats.add_custom('=C5="PASS"', {"fill": LIGHT_GREEN, "font": {"color": "#006100", "bold": True}})
s.get_range("C5:C40").conditional_formats.add_custom('=C5="FAIL"', {"fill": LIGHT_RED, "font": {"color": "#9C0006", "bold": True}})
s.freeze_panes.freeze_rows(4)

# Sample target rows
s = wb.worksheets.get_item("TargetSample")
title(s, "Primary target sample", "Первые 100 origins; полный файл находится в target_tables.")
table(s, read_csv(TARGETS / "next_planned_operational_targets.csv", 100), 4, 1, "TargetSampleTable", BLUE)
s.freeze_panes.freeze_rows(4)

# Sources
s = wb.worksheets.get_item("Sources")
title(s, "Источниковая опора", "Source-derived content отделено от reconstruction и synthetic evaluation.")
table(s, [
    ["Источник", "Роль", "Использовано", "URL / файл"],
    ["ВКР Филатовой М.С.", "Spatial backbone СКРУ-1", "TAB-слои, Excel-параметры, GIS integration, anchors", "ВКР_Филатова_М_С.docx"],
    ["Бабаянц и др., 2023", "Temporal dynamics", "Равномерные/замедляющиеся ряды и локальное ускорение", "https://doi.org/10.21455/gr2023.2-3"],
    ["Губанова, Глебова, 2022", "Geomechanical interpretation", "Калибровка модели по displacement fields", "https://doi.org/10.7242/echo.2022.4.6"],
    ["Мусихин, 2012", "InSAR QC", "Сопоставление с наземной геодезией и error analysis", "01006516068.pdf"],
    ["Бобровицкий, 2026", "Surveying kinematics", "Оседания, rates, tilt, curvature, strain", "НК 26 Бобровицкий Григорий.pdf"],
], 4, 1, "SourcesTable", BLUE, [34, 34, 76, 46])
s.freeze_panes.freeze_rows(4)

# Chart data and dashboard charts
s = wb.worksheets.get_item("ChartData")
s.get_range("A1:B7").values = [["Family", "Points"], ["decaying", 28], ["uniform_creep", 26], ["stable", 18], ["accelerating", 12], ["reactivated", 8], ["step_change", 6]]
s.get_range("D1:E4").values = [["Split", "Labels"], ["train", 911], ["validation", 130], ["test", 175]]
chart1 = wb.worksheets.get_item("Dashboard").charts.add("bar", s.get_range("A1:B7"))
chart1.title_text = "Баланс process families"; chart1.has_legend = False; chart1.set_position("H11", "N21")
chart2 = wb.worksheets.get_item("Dashboard").charts.add("bar", s.get_range("D1:E4"))
chart2.title_text = "T1 labels по split"; chart2.has_legend = False; chart2.set_position("H22", "N32")

SpreadsheetFile.export_xlsx(wb).save(str(OUT))
print(wb.inspect({"kind": "table", "range": "Dashboard!A1:N30", "include": "values,formulas", "table_max_rows": 30, "table_max_cols": 14}).ndjson)
print(wb.inspect({"kind": "match", "search_term": "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A", "options": {"use_regex": True, "max_results": 100}, "summary": "formula error scan"}).ndjson)
wb.render({"sheet_name": "Dashboard", "range": "A1:N32", "scale": 1.05}).save(str(PREVIEW))
print(json.dumps({"workbook": str(OUT), "preview": str(PREVIEW)}, ensure_ascii=False, indent=2))

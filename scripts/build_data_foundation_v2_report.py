#!/usr/bin/env python3
"""Render the bounded data-foundation audit from release tables; no model imports."""
from __future__ import annotations
import base64
import csv
import hashlib
import html
import json
import re
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "docs/reports/DATA_FOUNDATION_V2_RU.md"
QA = ROOT / "artifacts/data_quality/scenario_simulation_v2"
EVIDENCE = ROOT / "artifacts/reconstruction/scenario_constraints_v2"


def rows(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def table(headers, data):
    clean = lambda x: str(x).replace("|", "/").replace("\n", " ")
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines += ["| " + " | ".join(clean(x) for x in row) + " |" for row in data]
    return "\n".join(lines)


def number(value):
    if value == "":
        return "—"
    n = float(value)
    return f"{int(n):,}".replace(",", " ") if n == int(n) else f"{n:.3f}"


def csv_table(filename):
    data = rows(QA / filename)
    return table(list(data[0]), [[number(v) if k != list(r)[0] else v for k, v in r.items()] for r in data])


def write_report():
    constraints = rows(EVIDENCE / "scenario_constraints.csv")
    sources = {r["source_id"]: r["source_file"] for r in constraints}
    source_table = table(["Source ID", "Локальный первоисточник"], [[k, f"[{Path(v).name}](../../{quote(v, safe='/')})"] for k, v in sorted(sources.items())])
    audit_table = table(["Source / constraint_id", "Figure/page", "Observable", "Numeric data", "Status", "Confidence", "Current use (v1)", "New use (v2)", "Reason"], [
        [r["source_id"] + " / " + r["constraint_id"], r["source_page"], r["quantity"],
         f"{number(r['lower_bound'])} … {number(r['upper_bound'])} {r['unit']}" if r["lower_bound"] else (r["allowed_values"] or "—"),
         r["status"], r["confidence"], r["current_use"], r["new_use"], r["reason"]] for r in constraints])
    inventory = rows(EVIDENCE / "profile_series_inventory.csv")
    profile_table = table(["Линия", "Метод", "Период", "Позиций", "Distinct", "Approximate", "Unknown", "Signed min…max, мм", "Read ±, мм", "Численная генерация"], [
        [r["profile_label"], r["method"], r["interval"], r["positions"], r["distinct"], r["approximate"], r["unresolved"],
         f"{number(r['signed_min_mm'])}…{number(r['signed_max_mm'])}", f"{number(r['read_bound_min_mm'])}…{number(r['read_bound_max_mm'])}", r["numeric_calibration"]] for r in inventory])
    modality = rows(EVIDENCE / "modality_comparison.csv")
    modality_table = table(["Линия", "Период", "Пар", "Mean radar−leveling, мм", "RMS, мм", "Max abs, мм", "r"], [
        [r["profile_label"], r["interval"], r["paired_readable"], number(r["signed_radar_minus_leveling_mean_mm"]), number(r["rms_disagreement_mm"]), number(r["max_abs_mm"]), number(r["correlation"])] for r in modality])
    labels = {"base_points":"Точки основы", "profiles":"Профили основы", "proxy_zones":"Условные зоны", "campaigns":"Плановые кампании", "observation_scenarios":"Сценарии наблюдений", "latent_worlds":"Latent-world IDs", "trajectory_instances":"Point × scenario trajectories", "latent_trajectory_keys":"Point × latent-world keys", "distinct_latent_fields":"Различные полные latent fields", "campaign_rows":"Campaign × point × scenario rows", "model_origins":"Model origins", "targeted_rows":"Targeted campaign rows", "observed_rows":"Доступные наблюдения", "missing_targeted_fraction":"Missing / targeted, %", "numerically_constrained_scale_fraction":"Сценарии с численно ограниченным масштабом, %", "identified_real_temporal_law_fraction":"Идентифицированные реальные temporal laws, %", "transition_diagnostic_fraction":"Интервалы с diagnostic acceleration > 10, %"}
    count_rows = []
    for r in rows(QA / "counts_comparison.csv"):
        multiplier = 100 if r["quantity"].endswith("fraction") else 1
        count_rows.append([labels[r["quantity"]], number(float(r["old"])*multiplier), number(float(r["new"])*multiplier)])
    counts = table(["Показатель", "v1", "v2"], count_rows)
    statistics = json.loads((QA / "new_distribution.json").read_text(encoding="utf-8"))
    old = json.loads((QA / "old_distribution.json").read_text(encoding="utf-8"))
    names = {"latent_settlement_mm":"Latent settlement, мм", "observed_settlement_mm":"Observed settlement, мм", "latent_interval_rate_mm_y":"Latent interval rate, мм/год", "latent_interval_acceleration_mm_y2":"Latent interval acceleration, мм/год²", "target_latent_rate_mm_y":"Latent target rate, мм/год", "target_observed_rate_mm_y":"Observed target rate, мм/год", "reported_uncertainty_mm":"Declared observation σ, мм", "observed_history_gap_days":"Предыдущий observed gap, дни", "forecast_horizon_days":"Next-planned horizon, дни"}
    distribution = table(["Величина", "Версия", "Min", "Median", "Mean", "p95", "p99", "Max"], [[title, version] + [number(data[key][stat]) for stat in ["min", "q50", "mean", "q95", "q99", "max"]] for key,title in names.items() for version,data in [("v1",old),("v2",statistics)]])
    captions = [
        ("01_temporal_families.png", "1. Случайные temporal trajectories", "Выбор примеров фиксирован seed 1729 в random_example_selection.json. Все линии — synthetic latent truth; это не восстановленные ряды реальных реперов. Показаны разные формы динамики, а не реализации двухрежимного IMM."),
        ("02_settlement_velocity_acceleration.png", "2. Settlement / velocity / acceleration", "Скорости являются средними по интервалу. Ускорение вычислено между серединами соседних интервалов; значения при коротком интервале и скачке не трактуются как подтверждённые полевые ускорения."),
        ("03_published_profiles_and_analogues.png", "3. Published profiles и ensemble аналогов", "Оранжевые точки — только читаемые leveling values и консервативные read bounds; знак для сравнения обращён в positive-down. Синие формы имеют сопоставимый масштаб и широкий структурный тип. Ось — порядковая/нормированная, без координатного соответствия. Эти ансамбли не являются pointwise fit и не обязаны покрывать каждый маркер. Неизвестные позиции не дорисованы. Небольшой опубликованный подъём на Line 6 не воспроизводится subsidence-only latent laws."),
        ("04_empirical_vs_simulated_ranges.png", "4. Published bounds и simulated interval maxima", "Сравнение выполняется по отдельным номинальным окнам 1/5 лет, а не по полному накоплению за 2018–2025. Огибающие относятся к читаемым позициям. Это не распределение реальных annual rates и не независимая валидация: bounds уже использованы генератором."),
        ("05_old_new_distributions.png", "5. Распределения v1/v2", "Сравниваются полные проектные смеси с разными законами и весами. Изменение нельзя причинно приписать только одной новой публикации или только устранению ошибки реконструкции."),
        ("06_cadence_missingness.png", "6. Campaign cadence и missingness", "Календарь плановых кампаний сохранён; наблюдаемые интервалы меняются из-за пропусков. Зимняя недоступность относится к reflector contamination. Частоты пропусков — synthetic assumptions, не оценка реального журнала нивелирования."),
        ("07_measurement_noise_components.png", "7. Процесс наблюдений", "Показаны независимый шум, грубые выбросы, общий datum shift и эффект отражателя. Reported σ описывает обычную компоненту, не полную ошибку с bias/outlier. Отсутствующее наблюдение не означает нулевую ошибку."),
        ("08_true_vs_pseudo_transition.png", "8. True transition и pseudo-transition", "Справа latent velocity строго постоянна: кажущееся изменение создаёт measurement process. Слева true nonlinear acceleration. Температурный рисунок — стресс-аналог мотива из источника, не физически идентифицированная модель InSAR или здания."),
        ("09_extreme_cases.png", "9. Экстремальные случаи", "Выбор сделан по данным — максимальной скорости/ускорению/пропуску, без model errors. Короткие smooth-step stress episodes выходят за масштаб карты 2016. Эти случаи проверяют устойчивость и не объявлены характерными для СКРУ-1."),
        ("10_spatial_snapshots.png", "10. Пространственные snapshots", "Те же 42 точки реконструированной локальной сети, три даты, синтетическое оседание. Интерполированная поверхность и привязка к реальной карте скоростей 2016 не создавались.")]
    figures = "\n\n".join(f"### {title}\n\n{caption}\n\n![{title}](../../artifacts/data_quality/scenario_simulation_v2/figures/{file})" for file,title,caption in captions)
    text = r'''# Data foundation v2: источники → ограничения → отдельный сценарный release

Дата завершения: 2026-09-24. Область: только evidence, генерация, data contracts и воспроизводимость. Модели, Gate B/C selection и исторические holdout/test labels не использовались. Все пути ниже относительны корню репозитория, если не оформлены как ссылка из этого отчёта.

## A. EXECUTIVE SUMMARY

**Новый локальный data release готов к заморозке следующего ограниченного сравнения.** Это publication-envelope-conditioned synthetic dataset, а не real monitoring dataset и не подтверждение field accuracy. Модельный adapter и sequence tensorization должны пройти отдельный acceptance перед следующим запуском; текущая задача до data release завершена.

Разрыв v1 состоял в том, что manifest реестра источников проверялся, но численные профили Мусихина не участвовали в формулах temporal amplitude (`src/skru1/scenario_simulation.py:819`, `:977`). Константы 240–340 мм (`configs/scenario_simulation_v1.json:53`, `:90`) опирались на контекст зональной карты без доказанного интервала накопления. В v2 семь отдельных leveling series задают масштабы приращения в явных условных окнах 1/5 лет. Даты окна, temporal law и его параметры остаются допущениями. Опубликованные периоды не вычитаются.

Проверены 14 серий четырёх линий; на Line 6 нет пары 2011–2016. Приоритет принятой подробной оцифровки Line 1 исправляет конфликт двух существующих releases: 11 читаемых значений вместо 10 в общем атласе. Сохраняются 7 distinct / 4 approximate / 3 unknown. В целом — 253 численных значения и 5 unknown из 258 слотов. Старые таблицы не изменены.

Release содержит 640 observation scenarios, 128 latent-world IDs (127 различных полных полей), 42 точки, 14 профилей, 29 кампаний и 321 766 origins. Численными published bounds ограничен масштаб 98,4375% сценариев; доля temporal laws, идентифицированных по реальной временной траектории, — **0%**. Пять measurement conditions одного latent world зависимы. Рост строк отражает factorial design, а не новые реальные наблюдения.

Два независимых запуска генератора дали одинаковые 20 файлов; две сборки constraints — одинаковые 6 файлов. Независимый validator пересчитал все origins и sequence windows. Data/reconstruction tests: 40 passed. Все 1 059 файлов начального inventory сохранили hashes. Старые результаты моделей не перенесены, suite primary не менялся, auto-commit не выполнялся.

Основные deliverables: [dataset card](../../data/scenario_simulation_v2/README.md), [dataset manifest](../../data/scenario_simulation_v2/manifest.json), [constraints manifest](../../artifacts/reconstruction/scenario_constraints_v2/manifest.json), [QA tables/figures manifest](../../artifacts/data_quality/scenario_simulation_v2/manifest.json), [migration protocol](../governance/SCENARIO_EXPERIMENT_V2_PROTOCOL.md), [IMM design note](IMM_V2_IMPROVEMENT_DESIGN_RU.md).

## B. EMPIRICAL EVIDENCE AUDIT

### Фактический data path

```text
Локальные PDF/DOCX + input/source manifests
  → immutable digitization releases (общий атлас + принятая Line 1 reread)
  → scenario_constraints_v2 (42 evidence records; 7 leveling numeric donors)
  → scenario_simulation_v2: explicit scale conditioning + assumed temporal laws
  → synthetic latent truth → synthetic survey/reflector observations
  → causal history → safe features + sequence references → next-planned targets
```

Параллельная геометрическая ветвь: Filatova → принятая реконструкция → `survey_points` + normalized chainage + designed campaign membership. Из предыдущей feature table читаются только `point_id` и normalized chainage, не старые temporal values/labels. Исторический Gate B/C dataset — другая замороженная ветвь; простая подмена пути его runner запрещена.

Точки входа в код: `src/skru1/empirical_constraints_v2.py:40` (приоритет оцифровки), `:79` (profile evidence), `:120` (modality comparison); `src/skru1/scenario_simulation_v2.py:63` (catalog), `:110` (latent surface), `:161` (observation process), `:448` (geometry-only loader), `:477` (sequence windows), `:536` (preflight/manifest/release). Manifest фиксирует точные bytes использованного кода.

### Локальные источники и охват

@@SOURCES@@

Проверены существующие source manifests, acceptance reports, figure atlas, points/linkage tables и код их чтения. Исходные 17 входов проверены по SHA256 до извлечения. Для применимых evidence просмотрены страницы SUP01 11–15, SRC03 13–14, SRC05 12–13; остальные источники проверены в пределах численных/структурных кандидатов. Это целевой просмотр существующего корпуса, не новый неограниченный literature review. Извлечение и rendering остаются в `work/data_foundation_v2/`; [extraction manifest](../../artifacts/data_quality/scenario_simulation_v2/reproducibility/source_extraction_manifest.json) фиксирует происхождение. Полнотекстовые исходники не подменены пересказом README.

### Полный реестр решений

Статус `ACCEPTED_NUMERIC_CONSTRAINT` означает, что численное содержание подтверждено в своей семантике. Разрешение использовать его в генераторе задают **allowed_use/new_use**, а не один status: карта, radar disagreement и other-site observations не становятся leveling calibration. Полный контракт с period, unit, sign, allowed/prohibited use и provenance hashes находится в [scenario_constraints.csv](../../artifacts/reconstruction/scenario_constraints_v2/scenario_constraints.csv).

@@AUDIT@@

### Четыре опубликованных профиля

@@PROFILES@@

Все строки таблицы относятся к SUP01, PDF p.15. Отрицательный знак исходного displacement сохранён. `Distinct` — однозначно считываемый маркер/кривая, не число уникальных численных значений. Read ± — консервативный предел считывания изображения, не стандартная неопределённость нивелирования. В детальной Line 1 он шире, чем в общем атласе, что тоже сохранено.

Пространственная ось имеет печатные distance labels, но фактическая раскладка категориальна: неравные/повторяющиеся подписи не дают метрическую интерполяционную ось. Геопривязки к реконструированным point IDs нет. Допускаются порядковое сравнение форм и сопоставление двух методов на одинаковых читаемых слотах одного периода. Скрытые значения не интерполированы. Перечень каждой позиции и её происхождение: [canonical_digitization.csv](../../artifacts/reconstruction/scenario_constraints_v2/canonical_digitization.csv).

Для разности `S_2011–2016 − S_2015–2016` не доказаны общий datum/reference, точные границы кампаний и отсутствие независимой нормировки. Совпадения единиц, знака и названий линий недостаточно. Поэтому такая разность **не вычисляется**; каждая серия — отдельный spatial interval constraint. Номинальные длительности 5/1 лет используются только как явно условная simulation interpretation годовых подписей, не измеренная скорость реального репера.

### Radar vs leveling

@@MODALITY@@

Это воспроизводимый `radar minus leveling` по одинаковым видимым слотам, без восполнения unknown. Между сериями среднее расхождение меняет знак; единого доказанного bias нет. Пространственная корреляция 0,691–0,967 показывает сходство нанесённых форм, но не идентичность операторов наблюдения. RMS 5,845–41,777 мм и крайнее расхождение 95,1 мм включают modality differences, пространственное сопоставление и чтение графика. В approximate-heavy radar Line 5 доказательность особенно ограничена. Эти числа сохранены как discrepancy context, **не** sigma нивелирования и **не** распределение шума обычного observation process.

В автореферате Мусихина (SRC05, p.12–13, Fig.4) независимо по опубликованному тексту приведены correlations 0,57–0,95 и RMS 10,7 мм для сравнения в Соликамске/VKM 2011. Это также comparison discrepancy. 4,8 мм на p.10 относятся к Астрахани; 8–14 мм на p.12 — atmospheric phase residual, 500–600 мм/год — солеотвалы. Эти числа не перенесены на ground-leveling network.

### Карта скорости и неопределённая временная кривая

SUP01 p.14 действительно подписан СКРУ-1, 2016; легенда −60…+60 мм/год, шаг изолиний 5 мм/год. Это **display scale и spacing**, не доказанные extrema поля и не ограничение всех будущих сценариев. Нет достаточной геореференции, соответствия зон текущим координатам или полностью описанного radar→vertical operator. Количественные координаты не изобретались; карта сохранена как справочный масштаб/структура.

Временной inset рядом с материалами ранних лет имеет ось май 2011…август 2016 и ординату оседания, мм. Не установлены point ID, ground/reflector method, зональное усреднение, absolute/relative datum. Статус `UNRESOLVED`; он не оцифрован как temporal calibration trajectory и не наложен на synthetic time series. На p.12 конфликт header СКРУ-1/2 с embedded captions «Березники 2020–2021/2021» блокирует numeric transfer; надпись «1 см» на p.13 без семантики статистики также исключена.

### Babayants: acceleration и apparent transitions

SRC03, PDF p.13 / печатная p.51: авторы сравнивают RMS остатков линейной/квадратичной аппроксимации с порогом >1,5, проверяют отрицательные linear velocity/quadratic acceleration при negative-down convention, а также отношение оседания второй половины периода к первой >1,5. Это screening rules, не закон латентной динамики и не безошибочные regime labels. В v2 пороги не используются для генерации истинных переключений.

PDF p.14 / печатная p.52, Fig.9: отражатели на крышах, вероятное температурное расширение зданий, apparent spring displacement 3–5 см; наземное нивелирование основания не показывает такого поведения. В v2 именно 30–50 мм определяют амплитуду отдельного `synthetic_reflector_contamination` stress. Latent truth при этом неизменна. Зимние пропуски мотивированы отсутствием November–December 2021 из-за снега, но ежегодное повторение, доля поражённых объектов и форма температурного эффекта являются design assumptions. Диссертация p.72–74 подтверждает тот же материал, не даёт вторую независимую выборку.

Калибровка геомеханики по Tyubegatan (SRC06, p.3–4) содержит 200 и 40 мм за 60 дней для разных пинов в 2021 году; около 2 м с 2010 — результат интерпретации/моделирования. Ни ряд с 2010, ни SKRU-1 observation из этого не создаётся. Аналогично другие месторождения не используются как источник численной динамики СКРУ-1.

## C. DATASET CHANGELOG

Название `scenario_simulation_v2` продолжает существующий отдельный v1; это не переименование исторического `SKRU1_Data_Foundation_v3_2_1`. Новый каталог создаётся только пустым. Manifest + запрет overwrite обеспечивают локальный immutable release по политике проекта; filesystem/WORM-защита не заявляется.

1. Добавлен единый v2 constraints release: 42 records, source hashes/pages, current/new/prohibited use; detailed Line 1 имеет явный приоритет. Старые releases сохранены по байтам.
2. Семь leveling envelopes теперь непосредственно меняют latent scale. Дополнительно используются порядковые широкие формы: gradient, two lobes, broad/local bowl. Digitized coordinates не копируются на synthetic network.
3. Удалён перенос старых 240–340 мм в новую temporal amplitude. Published window bounds и synthetic law формализованы отдельно.
4. Десять latent families и пять observation conditions описаны ниже; нет IMM transition matrix, режимной разметки B7 или model scores в generation path.
5. Observation errors разделены на ordinary, gross и systematic/reflector components. Парные observation conditions сохраняют один latent field. Pseudo-transition помечается для stable/uniform с measurement-only shifts; для нелинейных laws тот же error означает contamination, а не отсутствие истинной динамики.
6. Missingness больше не использует будущий максимум trajectory rate для нормировки вероятности. Используется абсолютный заранее заданный scale 100 мм/год и текущий simulated interval rate. Это MNAR stress, не input признак модели.
7. Derived acceleration вычисляется по расстоянию между серединами двух rate intervals. Старый v1 не пересчитан. Дифференциальный QA для обеих версий вычисляет latent acceleration одной формулой.
8. `model_features` содержит allowlist 16 признаков, metadata отделены от estimator inputs; `causal_history` — доступные synthetic observations; evaluator truth/errors — отдельные файлы. Train/calibration labels вынесены отдельно. Sequence windows содержат только ссылки на собственные последние ≤16 прошлых наблюдений, без интерполяции и fit preprocessing.

### Как действует численное ограничение

Пусть u — elapsed time, нормированное на полный synthetic horizon T; F(u) — заданный monotone displacement law. Для nominal donor duration d ∈ {1,5} окно [a,b] центрировано в середине horizon. Для статической spatial shape:

```text
a = 1/2 − d/(2T), b = 1/2 + d/(2T)
S_p(u) = A_p · F(u) / (F(b) − F(a))
S_p(b) − S_p(a) = A_p
```

A_p определяется случайной широкой spatial shape и read-bound envelope выбранной серии. Если в ней есть unknown позиции, synthetic floor=0 — допущение; минимум неизвестных реальных точек не восстанавливается. Для полной серии floor берётся из читаемой lower magnitude с read bound. Peak выбирается из 70–100% допустимого диапазона. Один donor применяется к latent world; разные published periods не комбинируются в одну восстановленную историю. Для moving focus интегрируется положительное движущееся поле скорости и используется общий multiplier профиля; гарантируется верхний масштаб nominal-window increment, не pointwise envelope и не полный spatial fit.

Ограничение за d лет **не ограничивает instantaneous/short-interval velocity**. Для 1-летнего donor полный 7,5-летний cumulative displacement закономерно может превышать исходное число. Это conditional simulation, а не доказанная полная SKRU-1 trajectory. Значения source displacement хранят исходный negative-down; latent/observed используют positive-down mm, rates mm/year, acceleration mm/year², год=365,25 дней.

### Происхождение полей

| Класс | Где хранится / что означает |
| --- | --- |
| OBSERVED / PUBLISHED | Исходные публикации, не строки model dataset |
| DIGITIZED | Canonical points + reading status/envelopes + table/source hashes |
| RECONSTRUCTED | Геометрия и normalized chainage; не мониторинговая temporal history |
| DERIVED | Causal rates/accelerations/profile aggregates из synthetic observations |
| SYNTHETIC LATENT TRUTH | evaluator campaign truth и next-planned latent targets |
| SYNTHETIC OBSERVATION | Latent + ordinary + gross + systematic error; доступность по synthetic schedule |

План кампаний унаследован как experimental design, а не реальный журнал. Подробный контракт: [field_provenance.json](../../data/scenario_simulation_v2/field_provenance.json).

## D. OLD VS NEW REPORT

Основной old baseline — существующий открытый `scenario_simulation_v1`, непосредственно предшествующий v2. Исторические Gate B/C labels не читались для этой таблицы. Старые benchmark MAE относятся к прежним данным и здесь не сравниваются/не объявляются результатами v2.

@@COUNTS@@

42 точки и 29 дат сохранены; период в обеих версиях 2018-05-15…2025-11-04. Latent-world IDs не равны числу независимых полевых наблюдений: два stable world дают одинаковый нулевой field, а пять conditions повторяют один latent world. 5 376 latent trajectory keys также зависимы через shared temporal law/spatial field. Численное ограничение масштаба 98,44% не означает 98,44% реальных или эмпирически идентифицированных временных рядов.

### Распределения и экстремумы

@@DISTRIBUTION@@

Полные min/p05/median/mean/p95/p99/max и число доступных значений: [differential_statistics.csv](../../artifacts/data_quality/scenario_simulation_v2/differential_statistics.csv). Latent distribution измерена на всех campaign-point-condition rows; observed distribution — только available rows; target distribution — на origins. Равные latent replicas увеличивают N, не information content. Missingness denominator — targeted, а не все campaign rows (not_targeted — не пропуск).

Порог diagnostic transition = |разность interval rates / midpoint spacing| >10 мм/год² выбран только для описания данных. Это не частота реальных переключений и не IMM label. Доля снизилась с 34,20% до 22,71%; одновременно max interval rate вырос с 137,74 до 730,07 мм/год и max latent settlement с 401,14 до 892,27 мм. Узкие step/episode laws — engineering stress, не эмпирически подтверждённые extrema СКРУ-1. Карта 2016 не использована как жёсткий global clip.

### Process-family distribution

@@FAMILIES@@

В v2 stable — exact-null negative control. Uniform мотивирован постоянной скоростью в SRC03. Exponential/log decay — альтернативные инженерные формы замедления, не оценённые по SKRU-1 trajectory. Smooth/saturating acceleration мотивированы существованием acceleration в SRC03, но power/softplus law не восстановлены из источника. Temporary episode, reactivation, smooth step и moving centre — explicit stress tests. Reactivation и moving focus целиком оставлены для heldout-mechanism evaluation. Непрерывные законы, saturating velocity, finite episodes и measurement-only shifts нарушают предположение «весь мир — два режима того же IMM», но не гарантируют отсутствие преимущества B7 на конкретной смеси.

### Profile / zone / missingness distribution

@@PROFILECOUNTS@@

В каждой строке выше campaign rows; по три базовые точки на профиль в обеих версиях. Доступность различается: фактические observed counts каждого профиля находятся в `new_distribution.json`. Published Line 1/5/17/6 не переименовывают эти 14 synthetic profiles.

@@ZONES@@

Зоны — единое для QA разбиение на четыре coordinate quadrants, cut по медианам всех 98 WORK points; это не геологические зоны шахты. Базовых точек SW=18, NE=17, SE=4, NW=3, одинаково v1/v2. В v1 observation table зоны и x/y не сериализовались: для сравнения те же point IDs сопоставлены с неизменённой source geometry. В v2 этот lookup сохранён в point_roster; estimator не получает zone ID.

@@MISSING@@

Сценарии пяти условий не являются полным декартовым произведением всех noise × missingness механизмов: пары зафиксированы для ограниченного stress design. Поэтому их отдельные причинные эффекты из общей таблицы не идентифицируются. Measurement conditions: ordinary=256, gross=128, systematic=128, thermal=128. Для clean/noise сравнений нужен одинаковый latent_world и явное сопоставление доступных origins.

### Ответы на шесть проверочных вопросов

1. **Что действительно изменило генерацию?** Семь читаемых leveling envelopes через A_p и F(b)−F(a), четыре широких spatial shape, 30–50 мм roof-reflector contamination и структурный snow-missingness motif. Тест чувствительности подтверждает изменение synthetic increments при изменении numeric envelope.
2. **Что не изменило её?** Common datum/cross-period subtraction, unidentified temporal inset, map georeference, недоказанная «1 см precision», radar-leveling RMS как leveling sigma, other-site rates, Filatova accumulation period. Все заблокированы allowed_use whitelist.
3. **Что остаётся модельным?** Все temporal laws и их durations/onsets, nominal-window placement, synthetic calendar, observation-noise distribution, outlier frequency, datum drift, большинство missingness probabilities, spatial correlation law и веса смеси. Полный список — раздел E.
4. **Зависимость от произвольных констант?** Существенная, особенно для duration/tail extremes и false transitions. Constants вынесены в generation config либо явно названы в коде/разделе E; они не оценены по B7 ошибкам. Два seeds и семь donor cohorts не заменяют sensitivity analysis. Для будущего robustness study отдельный preregistered release должен варьировать эти assumptions, не подстраивая текущий после model errors.
5. **Не стало ли легче IMM?** Неизвестно до сравнения. Меньшая доля больших ускорений и lower p95 rate могут облегчить некоторые случаи; более сильные хвосты, mismatched laws и pseudo-transitions могут осложнить другие. Распределения не являются доказательством превосходства или честной трудности для одной модели.
6. **Есть ли leakage empirical constraints?** Constraints задают simulation domain до построения split и не попадают в model features. Одни bounds в train/evaluation — общий synthetic prior, не независимая validation. QA использовал открытый synthetic evaluator, поэтому это не новый закрытый field holdout. Availability даты публикаций для прогнозиста не установлена: v2 нельзя объявлять реальным hindcast 2018 года. Будущим model workers evaluator читать нельзя; adapter/scorer должны обеспечивать это разделение.

### Reader-facing sanity plots

@@FIGURES@@

## E. REMAINING ASSUMPTIONS

| Допущение | Значение/роль | Что не доказано источниками |
| --- | --- | --- |
| Nominal period | 1/5 лет, окно в середине T | Exact campaign dates, common datum и перенос во времени |
| Spatial transfer | Source ordinal shape → synthetic normalized extent; случайное отражение | Соответствие реперов, координат, ориентации и зон |
| Spatial width/centre | Centre 0,4–0,6; width 0,16–0,30; localized ×0,65; peak fraction 0,7–1 | Реальные correlation lengths/widths и распределение extrema |
| Two lobes | Сдвиги centre ±0,37, второй lobe ×0,95 | Геометрические параметры не оценены по метрической оси |
| Moving centre | Travel 0,5 normalized extent, positive velocity floor 0,2, 2 001 integration nodes | Реальная скорость распространения/межпрофильная covariance |
| Monotone latent | Positive-down и nonnegative velocity; exact-zero stable | Небольшой реальный подъём Line 6 не моделируется; observed отрицательные значения не latent uplift |
| Temporal coefficients | base 0,2–0,5; tau 0,12–0,5; power 1,5–3; steepness 6–14; onset 0,3–0,7 | Ни один parameter distribution не является field posterior |
| Episodes | event centres 0,22–0,40 и 0,62–0,78; width 0,03–0,08; weights 0,45/0,40; second width ×1,3 | Reactivation duration и magnitudes не измерены |
| Smooth step | width 0,02 T, weight 0,5 | Большие short-interval rates — намеренный stress tail |
| Ordinary error | σ ∈ {0,4;0,8;1,6} мм, focused ×1,25, Gaussian | Реальная leveling precision не получена из digitization/radar discrepancy |
| Outliers | p=0,06; symmetric 8–20 мм | Частота/хвосты реальных ошибок не идентифицированы |
| Common shift | onset=0,58 T, +8 мм, затем +1 мм/год | Проектный datum-failure stress, не измеренный bias |
| Reflector effect | Амплитуда 30–50 мм source-motivated; доля 0,5; start day 75, rise 90 дней | Waveform, ежегодный reset, доля и timing не идентифицированы; не полноценный InSAR forward model |
| Missingness | independent 0,10; gap background 0,03, affected 0,40, 3 targeted campaigns; campaign failure background 0,02, fractions 0,46/0,72 | Частоты synthetic; snow months 11/12 мотивированы только конкретным 2021 случаем |
| State-dependent missingness | p=0,04+0,30·clip(abs(rate)/100,0,1) | MNAR dependence не оценена; legacy config key rate_power=1,5 в v2 не используется |
| Warmup/history | Первые 3 targeted observations защищены, кроме seasonal masking; ≥3 observed для origin | Операционный startup design; не реальный регламент |
| Calendar and sequence | 29 inherited planned campaigns; ≤16 last observed tokens | Не реальная съёмочная периодичность; tensorization отложена |
| Correlation | Shared profile shape/temporal law; общий datum bias; ordinary noise independent | Полная spatial/temporal error covariance не оценена |
| Factorial mixture | 2 seeds, 7 donors, 9 nonzero laws + stable, 5 conditions | Доли механизмов не prevalence ВКМ; 128 IDs не независимые field histories |

Повторяемость подтверждена в указанном environment, не как bitwise portability между всеми версиями NumPy/pandas/OS. Published bounds и чтение графиков тоже имеют неопределённость; она сохраняется как отдельный provenance layer, не становится случайным Gaussian measurement noise.

## F. MODEL MIGRATION PLAN

Полный [SCENARIO_EXPERIMENT_V2_PROTOCOL.md](../governance/SCENARIO_EXPERIMENT_V2_PROTOCOL.md) фиксирует следующий этап без его запуска. Минимум: B1 last-rate persistence; B5 fixed Kalman при acceptance adapter; B6 adaptive Kalman; неизменённый B7 two-regime IMM; один B8 robust contrast для error stress; C01 compact GRU только после проверки sequence tensorization/masks/elapsed Δt/train-only normalization. Все 22 старых кандидата, C2 и новый model zoo не нужны.

Train target end 2022-10-18; calibration 2023-01-17…2023-11-07; future evaluation с 2024-01-30. Число origins по roles: train 122 047, calibration 35 148, evaluation 133 833, excluded 30 738. Reactivation/moving focus не входят в train/calibration. Одинаковые bounds всех roles не доказывают out-of-domain performance.

Нужен отдельный manifest-checked v2 adapter, не замена строки пути старого runner. Использовать одинаковые origin IDs и next-planned горизонты, time-forward inner folds, target-overlap embargo; при spatial holdout исключать профиль во всех observation replicas и из агрегатов. Для split по реализациям держать latent-world replicas вместе. Только train fit для scales/preprocessing; calibration не fit. Old fixed parameters допустимы отдельным control, не как перенесённая оптимальность.

Primary comparison — latent average-rate/increment MAE/RMSE на одинаковых origins, observed estimand отдельно. Нужны family/profile/source-cohort macro averages, gap/error strata и false transitions на pseudo cases. Интервалы неопределённости группировать по latent worlds; два seeds ограничивают надёжность оценки. Выигрыш должен быть одновременно над persistence и single-regime Kalman, с заранее заданным practically meaningful threshold и guardrails на stable/gap/outlier. Преимущество/primary сейчас не выбирается.

## G. IMM IMPROVEMENT DESIGN NOTE

[Полный technical design](IMM_V2_IMPROVEMENT_DESIGN_RU.md) проверен по текущему `imm_kalman.py`; реализация и обучение отложены.

1. `_regime_transition_matrix` использует одинаковые stay probabilities на observation step; state transition и acceleration retention уже учитывают Δt. Рассмотреть Q с интенсивностями α,β в год и P(Δt)=exp(QΔt); ablations должны отделять смену time scaling от числа/подбора параметров и сравнивать фиксированные и train-tuned варианты.
2. Settlement, finite-difference rate и acceleration получены из одних наблюдений, но обновляются последовательными scalar likelihoods. Это риск повторного учёта информации. Вариант position-only с лаговой adaptation — простой control. Joint covariance R=AΣAᵀ учитывает within-window correlation, но **сама по себе не устраняет повторный учёт прошлых positions, уже попавших в prior**: нужен augmented-state/cross-covariance treatment или update независимой новой информацией. Условия и эксперименты перечислены в note.
3. CUSUM/GLR по normalized innovations — diagnostic либо ограниченное влияние на следующий regime prior, не отдельный predictor. Проверить true acceleration, isolated outlier, long gap, common shift и reflector pseudo-transition; единичный выброс не должен создавать уверенное переключение. По одной scalar trajectory datum step и настоящий displacement step могут быть неидентифицируемы без reference/multi-point context — отсутствие ложного confidence важнее обещания идеальной классификации.

## H. REPRODUCIBILITY REPORT

Environment: Python 3.13.13, NumPy 2.5.2, pandas 3.0.5, Windows 11 build 26100; полный [environment.json](../../data/scenario_simulation_v2/environment.json). Seeds: 104729, 130363; независимые seed streams для temporal/spatial/error частей. Code base commit `93463fbea798697183c1b0b1d3180eea934dea0f`; source data commit `9d58f7bebcd323c23b319ab34b4e7ba62362fea6`. Новые файлы не закоммичены; авторитетны commit **плюс source/config SHA256**, перечисленные manifest, и сохранённый patch. Нельзя воспроизвести v2 одним checkout старого commit.

Dataset manifest SHA256:

```text
7bb67939314fbcf548686893e6868c90a2303e1303c401377f197e077fec8c13
```

Constraints manifest SHA256:

```text
96b88b61cf766062c1f831e32b50bdd82441da63c50ce41569a5ffad297de6ef
```

Команды из корня (использовать новые пустые scratch names; существующий release не перезаписывается):

```powershell
.\.venv\Scripts\python.exe scripts/build_scenario_constraints_v2.py --output work/data_foundation_v2/reproduce_constraints_a
.\.venv\Scripts\python.exe scripts/build_scenario_constraints_v2.py --output work/data_foundation_v2/reproduce_constraints_b
.\.venv\Scripts\python.exe scripts/generate_scenario_v2.py --output work/data_foundation_v2/reproduce_data_a
.\.venv\Scripts\python.exe scripts/generate_scenario_v2.py --output work/data_foundation_v2/reproduce_data_b
.\.venv\Scripts\python.exe scripts/validate_scenario_v2.py --dataset data/scenario_simulation_v2 --baseline artifacts/data_quality/scenario_simulation_v2/reproducibility/baseline_inventory.json --output work/data_foundation_v2/recheck.json
.\.venv\Scripts\python.exe -m pytest tests/test_data_foundation_v2.py tests/test_reconstruction_data.py tests/test_reconstruction_atlas.py -q --basetemp=work/data_foundation_v2/recheck_tests
.\.venv\Scripts\python.exe -m pytest tests/test_scenario_simulation_v1.py -k "catalog_is_full or static_temporal or generated_release or adapter_exposes" -q --basetemp=work/data_foundation_v2/recheck_v1
.\.venv\Scripts\python.exe scripts/audit_scenario_v2.py --output work/data_foundation_v2/reproduce_audit
.\.venv\Scripts\python.exe scripts/build_data_foundation_v2_report.py
```

Сборка dataset использует уже замороженный constraints release, проверяя его против config; scratch constraints предназначены для побайтового сравнения, не для неявной подмены входов. [Reproducibility receipt](../../artifacts/data_quality/scenario_simulation_v2/reproducibility/dataset_reproducibility.json) содержит hashes всех 20 файлов и подтверждает равенство двух runs и опубликованной копии. [Constraints receipt](../../artifacts/data_quality/scenario_simulation_v2/reproducibility/constraints_reproducibility.json) подтверждает 6 файлов.

### Проверки и их границы

| Проверка | Результат |
| --- | --- |
| New data/reconstruction/atlas tests | 36 passed |
| Релевантные v1 data-only regression tests | 4 passed, 3 tests вне data-generation scope deselected |
| Serialized independent validation | PASS, 321 766 origins и 321 766 windows |
| Feature causality | Recomputed rates/accelerations/profile aggregates; future-mutation test passed |
| Targets/splits | Next planned, не next successful; dates/labels boundaries и missingness сохранены |
| Truth isolation | Allowlist 16 features; hidden truth/error columns исключены |
| Source lineage / unresolved exclusion | Input hashes до parsing; numeric donor whitelist; common datum/inset не участвуют |
| Pseudo-transition | Constant latent velocity; 5 conditions сохраняют identical latent field |
| Two-run reproducibility | Все 20 dataset files и 6 constraints files byte-identical |
| Старые материалы | 1 059 из 1 059 initial inventory files без изменений |
| Reader QA | Все 10 финальных PNG просмотрены, ограничения сравнений подписаны |
| Models / legacy holdout | 0 models, 0 legacy sealed labels parsed |

Полные `tests/test_target_contract.py` и `test_feature_contract.py` здесь не запускались: их `load_canonical_bundle` открывает historical labels, что запрещено текущим заданием. Они не отключены и не переписаны. Соответствующие v2 contracts проверены новым serialized validator. Явный `-k` исключает три v1 tests: IMM fit, аудит benchmark results и calibration-rank utility вне текущего data-generation scope. Импорт тестового модуля не выполняет fit. Поэтому утверждается проверка релевантного data path, а не полный repository-wide test pass.

Доказательства: [independent validation](../../artifacts/data_quality/scenario_simulation_v2/reproducibility/independent_validation_release.json), [test execution](../../artifacts/data_quality/scenario_simulation_v2/reproducibility/test_execution.json), [reader QA](../../artifacts/data_quality/scenario_simulation_v2/reproducibility/reader_qa.json), [initial inventory](../../artifacts/data_quality/scenario_simulation_v2/reproducibility/baseline_inventory.json), [git status](../../artifacts/data_quality/scenario_simulation_v2/reproducibility/git_status.txt), [change inventory](../../artifacts/data_quality/scenario_simulation_v2/reproducibility/change_inventory.json), [new text files patch](../../artifacts/data_quality/scenario_simulation_v2/reproducibility/new_text_files.patch), [finalization manifest](../../artifacts/data_quality/scenario_simulation_v2/finalization_manifest.json).

Начальный Git уже содержал untracked v1 scenario/benchmark files. Они сохранены отдельно от новых v2 additions в change inventory. Tracked diff пуст; содержательный diff новых untracked файлов сохранён явно, generated releases представлены manifest hashes. Никакие источники/ZIP, старые releases и frozen experiments не перезаписаны. Коммит, обучение, выбор победителя и изменение suite primary не выполнялись.
'''
    replacements = {"SOURCES":source_table,"AUDIT":audit_table,"PROFILES":profile_table,"MODALITY":modality_table,"COUNTS":counts,"DISTRIBUTION":distribution,"FAMILIES":csv_table("process_family_comparison.csv"),"PROFILECOUNTS":csv_table("profile_comparison.csv"),"ZONES":csv_table("zone_comparison.csv"),"MISSING":csv_table("missing_reason_comparison.csv"),"FIGURES":figures}
    for key,value in replacements.items():
        text=text.replace("@@"+key+"@@",value)
    REPORT.parent.mkdir(parents=True,exist_ok=True)
    REPORT.write_text(text.strip()+"\n",encoding="utf-8")
    return text


def render_html(md):
    """Small renderer for this report's controlled Markdown subset; embeds figures."""
    def inline(value):
        value=html.escape(value)
        value=re.sub(r"`([^`]+)`",r"<code>\1</code>",value)
        value=re.sub(r"\*\*([^*]+)\*\*",r"<strong>\1</strong>",value)
        return re.sub(r"\[([^\]]+)\]\(([^)]+)\)",r'<a href="\2">\1</a>',value)
    out=[]; lines=md.splitlines(); i=0
    while i<len(lines):
        s=lines[i]
        if not s.strip(): i+=1; continue
        if s.startswith("```"):
            block=[];i+=1
            while i<len(lines) and not lines[i].startswith("```"): block.append(lines[i]);i+=1
            out.append("<pre><code>"+html.escape("\n".join(block))+"</code></pre>")
        elif s.startswith("|"):
            data=[]
            while i<len(lines) and lines[i].startswith("|"):
                data.append([x.strip() for x in lines[i].strip().strip("|").split("|")]);i+=1
            out.append('<div class="table-scroll"><table><thead><tr>'+"".join("<th>"+inline(x)+"</th>" for x in data[0])+"</tr></thead><tbody>")
            out.extend("<tr>"+"".join("<td>"+inline(x)+"</td>" for x in row)+"</tr>" for row in data[2:])
            out.append("</tbody></table></div>");continue
        elif s.startswith("#"):
            n=len(s)-len(s.lstrip("#"));label=s[n:].strip();anchor=re.sub(r"[^a-zA-Z0-9]+","-",label).strip("-")
            if n!=2:anchor=f"section-{i}-{anchor}"
            out.append(f'<h{n} id="{anchor}">'+inline(label)+f"</h{n}>")
        elif s.startswith("!["):
            m=re.fullmatch(r"!\[([^\]]*)\]\(([^)]+)\)",s);assert m
            data=(REPORT.parent/m[2]).read_bytes()
            out.append('<img alt="'+html.escape(m[1])+'" src="data:image/png;base64,'+base64.b64encode(data).decode()+'">')
        else:
            out.append("<p>"+inline(s)+"</p>")
        i+=1
    css="body{font:17px/1.65 system-ui,sans-serif;color:#192735;background:#f5f7f9;margin:0}main{max-width:1180px;margin:auto;padding:36px;background:white}h1,h2,h3{line-height:1.25}h2{margin-top:3em;border-bottom:2px solid #207b88;padding-bottom:.5em}p{max-width:100ch}a{color:#006984}code{font-size:.9em;overflow-wrap:anywhere}pre{background:#eef3f5;padding:18px;overflow:auto}table{border-collapse:collapse;min-width:100%;font-size:13px}td,th{border:1px solid #cbd4db;text-align:left;padding:9px;vertical-align:top;overflow-wrap:anywhere}th{background:#e7f0f2}.table-scroll{overflow:auto;max-height:850px}thead th{position:sticky;top:0}img{display:block;max-width:100%;height:auto;margin:22px auto}nav{padding:18px;background:#e7f0f2;display:flex;gap:16px;flex-wrap:wrap}@media(max-width:700px){main{padding:16px}body{font-size:16px}}"
    nav="<nav>"+"".join(f'<a href="#{letter}-{label}">{letter}. {label.replace("-"," ")}</a>' for letter,label in [("A","EXECUTIVE-SUMMARY"),("B","EMPIRICAL-EVIDENCE-AUDIT"),("C","DATASET-CHANGELOG"),("D","OLD-VS-NEW-REPORT"),("E","REMAINING-ASSUMPTIONS"),("F","MODEL-MIGRATION-PLAN"),("G","IMM-IMPROVEMENT-DESIGN-NOTE"),("H","REPRODUCIBILITY-REPORT")])+"</nav>"
    doc='<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>SKRU-1 — Data foundation v2</title><style>'+css+"</style><main>"+nav+"\n".join(out)+"</main></html>"
    REPORT.with_suffix(".html").write_text(doc,encoding="utf-8")


if __name__=="__main__":
    render_html(write_report())
    print(json.dumps({"report":REPORT.relative_to(ROOT).as_posix(),"html":REPORT.with_suffix('.html').relative_to(ROOT).as_posix(),"sha256":hashlib.sha256(REPORT.read_bytes()).hexdigest()},ensure_ascii=False))

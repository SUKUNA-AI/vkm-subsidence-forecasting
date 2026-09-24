# Data foundation v2: источники → ограничения → отдельный сценарный release

## Статус после консолидации

Описание ниже фиксирует foundation predecessor v2. Current release — v2.1;
актуальные source/data claims ограничены R2, включая donor attribution,
pure-stress reflector и инженерные доли temporal families. См.
[каноническое состояние](../CANONICAL_RESEARCH_STATE_RU.md).
Ссылки на удалённые промежуточные свидетельства адресуют их Git snapshot.

Дата завершения: 2026-09-24. Область: только evidence, генерация, data contracts и воспроизводимость. Модели, Gate B/C selection и исторические holdout/test labels не использовались. Все пути ниже относительны корню репозитория, если не оформлены как ссылка из этого отчёта.

## A. EXECUTIVE SUMMARY

**Новый локальный data release готов к заморозке следующего ограниченного сравнения.** Это publication-envelope-conditioned synthetic dataset, а не real monitoring dataset и не подтверждение field accuracy. Модельный adapter и sequence tensorization должны пройти отдельный acceptance перед следующим запуском; текущая задача до data release завершена.

Разрыв v1 состоял в том, что manifest реестра источников проверялся, но численные профили Мусихина не участвовали в формулах temporal amplitude (`src/skru1/scenario_simulation.py:819`, `:977`). Константы 240–340 мм (`configs/scenario_simulation_v1.json:53`, `:90`) опирались на контекст зональной карты без доказанного интервала накопления. В v2 семь отдельных leveling series задают масштабы приращения в явных условных окнах 1/5 лет. Даты окна, temporal law и его параметры остаются допущениями. Опубликованные периоды не вычитаются.

Проверены 14 серий четырёх линий; на Line 6 нет пары 2011–2016. Приоритет принятой подробной оцифровки Line 1 исправляет конфликт двух существующих releases: 11 читаемых значений вместо 10 в общем атласе. Сохраняются 7 distinct / 4 approximate / 3 unknown. В целом — 253 численных значения и 5 unknown из 258 слотов. Старые таблицы не изменены.

Release содержит 640 observation scenarios, 128 latent-world IDs (127 различных полных полей), 42 точки, 14 профилей, 29 кампаний и 321 766 origins. Численными published bounds ограничен масштаб 98,4375% сценариев; доля temporal laws, идентифицированных по реальной временной траектории, — **0%**. Пять measurement conditions одного latent world зависимы. Рост строк отражает factorial design, а не новые реальные наблюдения.

Два независимых запуска генератора дали одинаковые 20 файлов; две сборки constraints — одинаковые 6 файлов. Независимый validator пересчитал все origins и sequence windows. Data/reconstruction tests: 40 passed. Все 1 059 файлов начального inventory сохранили hashes. Старые результаты моделей не перенесены, suite primary не менялся, auto-commit не выполнялся.

Основные deliverables: [dataset card](../../data/scenario_simulation_v2/README.md), [dataset manifest](../../data/scenario_simulation_v2/manifest.json), [constraints manifest](../../artifacts/reconstruction/scenario_constraints_v2/manifest.json), [QA tables/figures manifest](https://github.com/SUKUNA-AI/vkm-subsidence-forecasting/blob/c5da1103154c29e2ffe9c52bbceacaa032f1690f/artifacts/data_quality/scenario_simulation_v2/manifest.json), [migration protocol](../governance/SCENARIO_EXPERIMENT_V2_PROTOCOL.md), [IMM design note](IMM_V2_IMPROVEMENT_DESIGN_RU.md).

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

| Source ID | Локальный первоисточник |
| --- | --- |
| SRC01 | [ВКР_Филатова_М_С.docx](../../inputs/sources/primary/%D0%92%D0%9A%D0%A0_%D0%A4%D0%B8%D0%BB%D0%B0%D1%82%D0%BE%D0%B2%D0%B0_%D0%9C_%D0%A1.docx) |
| SRC02 | [НК 26 Бобровицкий Григорий.pdf](../../inputs/sources/primary/%D0%9D%D0%9A%2026%20%D0%91%D0%BE%D0%B1%D1%80%D0%BE%D0%B2%D0%B8%D1%86%D0%BA%D0%B8%D0%B9%20%D0%93%D1%80%D0%B8%D0%B3%D0%BE%D1%80%D0%B8%D0%B9.pdf) |
| SRC03 | [03-GR-24-2.pdf](../../inputs/sources/primary/03-GR-24-2.pdf) |
| SRC04 | [Babayants-Disser.pdf](../../inputs/sources/primary/Babayants-Disser.pdf) |
| SRC05 | [01006516068.pdf](../../inputs/sources/primary/01006516068.pdf) |
| SRC06 | [106_Губанова__Глебова.pdf](../../inputs/sources/primary/106_%D0%93%D1%83%D0%B1%D0%B0%D0%BD%D0%BE%D0%B2%D0%B0__%D0%93%D0%BB%D0%B5%D0%B1%D0%BE%D0%B2%D0%B0.pdf) |
| SRC07 | [Гусев_АП_Спутниковый_мониторинг_геодинамики.pdf](../../inputs/sources/primary/%D0%93%D1%83%D1%81%D0%B5%D0%B2_%D0%90%D0%9F_%D0%A1%D0%BF%D1%83%D1%82%D0%BD%D0%B8%D0%BA%D0%BE%D0%B2%D1%8B%D0%B9_%D0%BC%D0%BE%D0%BD%D0%B8%D1%82%D0%BE%D1%80%D0%B8%D0%BD%D0%B3_%D0%B3%D0%B5%D0%BE%D0%B4%D0%B8%D0%BD%D0%B0%D0%BC%D0%B8%D0%BA%D0%B8.pdf) |
| SRC08 | [Shirshova (1).pdf](../../inputs/sources/primary/Shirshova%20%281%29.pdf) |
| SRC09 | [sputnikovaya_radarnaya_interferometriya_informatsionno_vychislitelnye.pdf](../../inputs/sources/primary/sputnikovaya_radarnaya_interferometriya_informatsionno_vychislitelnye.pdf) |
| SRC10 | [02f4a17c7c7ba99cfc068395238c5ff9.pdf](../../inputs/sources/primary/02f4a17c7c7ba99cfc068395238c5ff9.pdf) |
| SRC11 | [88q34t4oapf2ibwjcw2ie0oz2bjbtmc4.pdf](../../inputs/sources/primary/88q34t4oapf2ibwjcw2ie0oz2bjbtmc4.pdf) |
| SUP01 | [geokniga-02obrabotka.pdf](../../inputs/sources/supplementary/geokniga-02obrabotka.pdf) |

Проверены существующие source manifests, acceptance reports, figure atlas, points/linkage tables и код их чтения. Исходные 17 входов проверены по SHA256 до извлечения. Для применимых evidence просмотрены страницы SUP01 11–15, SRC03 13–14, SRC05 12–13; остальные источники проверены в пределах численных/структурных кандидатов. Это целевой просмотр существующего корпуса, не новый неограниченный literature review. Извлечение и rendering остаются в `work/data_foundation_v2/`; [extraction manifest](https://github.com/SUKUNA-AI/vkm-subsidence-forecasting/blob/c5da1103154c29e2ffe9c52bbceacaa032f1690f/artifacts/data_quality/scenario_simulation_v2/reproducibility/source_extraction_manifest.json) фиксирует происхождение. Полнотекстовые исходники не подменены пересказом README.

### Полный реестр решений

Статус `ACCEPTED_NUMERIC_CONSTRAINT` означает, что численное содержание подтверждено в своей семантике. Разрешение использовать его в генераторе задают **allowed_use/new_use**, а не один status: карта, radar disagreement и other-site observations не становятся leveling calibration. Полный контракт с period, unit, sign, allowed/prohibited use и provenance hashes находится в [scenario_constraints.csv](../../artifacts/reconstruction/scenario_constraints_v2/scenario_constraints.csv).

| Source / constraint_id | Figure/page | Observable | Numeric data | Status | Confidence | Current use (v1) | New use (v2) | Reason |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| SUP01 / MUS-P1-LEVELING-2011-2016-SIGNED-DISP | 15 | signed_displacement_envelope | -364.300 … -76.400 mm | ACCEPTED_NUMERIC_CONSTRAINT | medium_reading_low_temporal_transfer | registry_only_not_read_by_v1_formula | interval_displacement_envelope | published_interval_profile_is_not_a_temporal_trajectory |
| SUP01 / MUS-P1-LEVELING-2015-2016-SIGNED-DISP | 15 | signed_displacement_envelope | -73.500 … 4.200 mm | ACCEPTED_NUMERIC_CONSTRAINT | medium_reading_low_temporal_transfer | registry_only_not_read_by_v1_formula | interval_displacement_envelope | published_interval_profile_is_not_a_temporal_trajectory |
| SUP01 / MUS-P1-RADAR-2011-2016-SIGNED-DISP | 15 | signed_displacement_envelope | -373.200 … -11.900 mm | ACCEPTED_NUMERIC_CONSTRAINT | medium_reading_low_temporal_transfer | registry_only_not_read_by_v1_formula | modality_comparison_only | published_interval_profile_is_not_a_temporal_trajectory |
| SUP01 / MUS-P1-RADAR-2015-2016-SIGNED-DISP | 15 | signed_displacement_envelope | -75.800 … 3.600 mm | ACCEPTED_NUMERIC_CONSTRAINT | medium_reading_low_temporal_transfer | registry_only_not_read_by_v1_formula | modality_comparison_only | published_interval_profile_is_not_a_temporal_trajectory |
| SUP01 / MUS-P17-LEVELING-2011-2016-SIGNED-DISP | 15 | signed_displacement_envelope | -177.600 … -68.900 mm | ACCEPTED_NUMERIC_CONSTRAINT | medium_reading_low_temporal_transfer | registry_only_not_read_by_v1_formula | interval_displacement_envelope | published_interval_profile_is_not_a_temporal_trajectory |
| SUP01 / MUS-P17-LEVELING-2015-2016-SIGNED-DISP | 15 | signed_displacement_envelope | -28.100 … -11 mm | ACCEPTED_NUMERIC_CONSTRAINT | medium_reading_low_temporal_transfer | registry_only_not_read_by_v1_formula | interval_displacement_envelope | published_interval_profile_is_not_a_temporal_trajectory |
| SUP01 / MUS-P17-RADAR-2011-2016-SIGNED-DISP | 15 | signed_displacement_envelope | -162.700 … -49.200 mm | ACCEPTED_NUMERIC_CONSTRAINT | medium_reading_low_temporal_transfer | registry_only_not_read_by_v1_formula | modality_comparison_only | published_interval_profile_is_not_a_temporal_trajectory |
| SUP01 / MUS-P17-RADAR-2015-2016-SIGNED-DISP | 15 | signed_displacement_envelope | -23.900 … 0.600 mm | ACCEPTED_NUMERIC_CONSTRAINT | medium_reading_low_temporal_transfer | registry_only_not_read_by_v1_formula | modality_comparison_only | published_interval_profile_is_not_a_temporal_trajectory |
| SUP01 / MUS-P5-LEVELING-2011-2016-SIGNED-DISP | 15 | signed_displacement_envelope | -311.800 … -111.200 mm | ACCEPTED_NUMERIC_CONSTRAINT | medium_reading_low_temporal_transfer | registry_only_not_read_by_v1_formula | interval_displacement_envelope | published_interval_profile_is_not_a_temporal_trajectory |
| SUP01 / MUS-P5-LEVELING-2015-2016-SIGNED-DISP | 15 | signed_displacement_envelope | -59 … -12.600 mm | ACCEPTED_NUMERIC_CONSTRAINT | medium_reading_low_temporal_transfer | registry_only_not_read_by_v1_formula | interval_displacement_envelope | published_interval_profile_is_not_a_temporal_trajectory |
| SUP01 / MUS-P5-RADAR-2011-2016-SIGNED-DISP | 15 | signed_displacement_envelope | -293.100 … -114.400 mm | ACCEPTED_NUMERIC_CONSTRAINT | medium_reading_low_temporal_transfer | registry_only_not_read_by_v1_formula | modality_comparison_only | published_interval_profile_is_not_a_temporal_trajectory |
| SUP01 / MUS-P5-RADAR-2015-2016-SIGNED-DISP | 15 | signed_displacement_envelope | -54.400 … -13.500 mm | ACCEPTED_NUMERIC_CONSTRAINT | medium_reading_low_temporal_transfer | registry_only_not_read_by_v1_formula | modality_comparison_only | published_interval_profile_is_not_a_temporal_trajectory |
| SUP01 / MUS-P6-LEVELING-2015-2016-SIGNED-DISP | 15 | signed_displacement_envelope | -49.100 … 4.800 mm | ACCEPTED_NUMERIC_CONSTRAINT | medium_reading_low_temporal_transfer | registry_only_not_read_by_v1_formula | interval_displacement_envelope | published_interval_profile_is_not_a_temporal_trajectory |
| SUP01 / MUS-P6-RADAR-2015-2016-SIGNED-DISP | 15 | signed_displacement_envelope | -51.500 … -0.300 mm | ACCEPTED_NUMERIC_CONSTRAINT | medium_reading_low_temporal_transfer | registry_only_not_read_by_v1_formula | modality_comparison_only | published_interval_profile_is_not_a_temporal_trajectory |
| SUP01 / MUS-PERIOD-DATUM | 15 | common_datum | — | UNRESOLVED | medium | unused_or_context_only | block_cross_period_subtraction | No documented common datum, exact campaign endpoints or normalization. |
| SUP01 / MUS-SPATIAL-SHAPES | 15 | ordered_profile_shape | — | ACCEPTED_STRUCTURAL_CONSTRAINT | medium | unused_or_context_only | randomized_shape_family | Monotone gradient, two lobes, broad bowl, localized bowl visible; ordinal transfer only. |
| SUP01 / MUS-MAP-2016-LEGEND | 14 | velocity_display_scale | -60 … 60 mm/year | ACCEPTED_NUMERIC_CONSTRAINT | medium | unused_or_context_only | reader_plausibility_reference_only | Caption explicitly identifies SKRU-1 2016; legend limits are not actual extrema. |
| SUP01 / MUS-MAP-CONTOUR-STEP | 14 | contour_spacing | 5 … 5 mm/year | ACCEPTED_NUMERIC_CONSTRAINT | medium | unused_or_context_only | map_resolution_context_only | Caption says 5 mm/year; cannot assign map zones to reconstructed coordinates. |
| SUP01 / MUS-UNIDENTIFIED-TIME-CURVE | 14 inset | temporal_trajectory | — | UNRESOLVED | medium | unused_or_context_only | prohibit_numeric_calibration | Axis May 2011 to Aug 2016 and settlement mm visible, object ID, method, datum and aggregation unspecified. |
| SUP01 / MUS-SLIDE12-CONFLICT | 12 | site_period | — | REJECTED | medium | unused_or_context_only | exclude_from_skru1_calibration | SKRU-1/2 slide header conflicts with embedded captions Berezniki 2020-2021/2021. |
| SUP01 / MUS-SLIDE13-ONE-CM | 13 inset | mean_deviation | 10 … 10 mm | UNRESOLVED | medium | unused_or_context_only | exclude_numeric_calibration | 1 cm annotation lacks sufficient method, site, period and deviation definition. |
| SRC03 / BAB-ACCELERATION-SCREEN | 13 (printed 51), Fig.8 | two_screening_ratios | — | ACCEPTED_STRUCTURAL_CONSTRAINT | medium | unused_or_context_only | acceleration_and_false_acceleration_motivation | Linear/quadratic residual RMS ratio >1.5, c<0 a<0 under negative-down sign, second/first half >1.5; selection heuristic, not law. |
| SRC03 / BAB-THERMAL-SHIFT | 14 (printed 52), Fig.9 | apparent_spring_displacement | 30 … 50 mm | ACCEPTED_NUMERIC_CONSTRAINT | high_text_probable_mechanism_low_transfer | unused_or_context_only | reflector_contamination_stress_amplitude | Authors suggest thermal building expansion; not geodynamics, not ground leveling error. |
| SRC03 / BAB-SEASONAL-MISSING | 14 (printed 52) | snow_related_unavailability | — | ACCEPTED_STRUCTURAL_CONSTRAINT | medium | unused_or_context_only | reflector_winter_missingness_stress | No November-December 2021 data because of snow. |
| SRC03 / BAB-UNIFORM-RATE | 9 (printed 47), Fig.5 | approximately_constant_velocity | — | ACCEPTED_STRUCTURAL_CONSTRAINT | medium | unused_or_context_only | uniform_velocity_motivation | Source says selected intense subsidence time series nearly constant. |
| SRC03 / BAB-OTHER-SITE-RATES | 7,9,15 (printed 45,47,53) | local_rate_examples | 200 … 320 mm/year | ACCEPTED_NUMERIC_CONSTRAINT | medium | unused_or_context_only | external_scale_context_only | Up to 320 mm/year near sinkhole, most strong southern areas 200-250; different site and population. |
| SRC04 / BAB-DISSER-CORROBORATION | 72-74, Fig.3.23-3.24 | acceleration_screen_and_roof_effect | — | ACCEPTED_STRUCTURAL_CONSTRAINT | medium | unused_or_context_only | duplicate_source_corroboration_only | Same analysis corroborates journal article; do not count as independent evidence. |
| SRC05 / MUS-2011-MODALITY-CORRELATION | 12-13, Fig.4 | radar_leveling_correlation | 0.570 … 0.950 correlation | ACCEPTED_NUMERIC_CONSTRAINT | medium | unused_or_context_only | modality_diagnostic_context_only | Text range 0.57-0.95; comparison depends on interpolation from scatterers. |
| SRC05 / MUS-2011-MODALITY-RMS | 13, Fig.4 | published_rms_disagreement | 10.700 … 10.700 mm | ACCEPTED_NUMERIC_CONSTRAINT | medium | unused_or_context_only | modality_diagnostic_context_only | Figure label SКО 10.7 mm; not leveling instrument sigma. |
| SRC05 / MUS-ATMOSPHERIC-RESIDUAL | 12 | atmospheric_phase_residual_scale | 8 … 14 mm | ACCEPTED_NUMERIC_CONSTRAINT | medium | unused_or_context_only | modality_diagnostic_context_only | Source reports 0.8-1.4 cm; not transferable into ordinary leveling noise. |
| SRC05 / MUS-SALT-DUMP | 12 | subsidence_rate | 500 … 600 mm/year | ACCEPTED_NUMERIC_CONSTRAINT | medium | unused_or_context_only | excluded_from_ground_network_numeric_calibration | 50-60 cm/year for salt dumps, only one season; different surface type. |
| SRC05 / MUS-ASTRAKHAN-RMS | 10, Fig.2 | radar_leveling_rms | 4.800 … 4.800 mm | ACCEPTED_NUMERIC_CONSTRAINT | medium | unused_or_context_only | excluded_other_site_precision | 4.8 mm is another field; not SKRU-1 precision. |
| SRC05 / MUS-RADAR-CADENCE | 17-18 | first_year_scene_count | 13 … 15 scenes/year | ACCEPTED_NUMERIC_CONSTRAINT | medium | unused_or_context_only | exclude_leveling_campaign_calibration | 13-15 scenes is methodological radar recommendation, not observed leveling cadence. |
| SRC06 / GUB-TYUBEGATAN | 3-4 (printed 41-42), Fig.2-3 | displacement_over_60_days | 40 … 200 mm | ACCEPTED_NUMERIC_CONSTRAINT | medium | unused_or_context_only | exclude_cross_site_numeric_transfer | Pin5 20cm and Pin6 4cm during Sep26-Nov25 2021; ~2m since 2010 is inferred/modelled, not measured history. |
| SRC01 / FIL-ZONAL-STATISTICS | Fig.18,22; paragraph at extracted line 492 | zonal_disp_min_mean_max | — | QUALITATIVE_ONLY | medium | unused_or_context_only | geometry_context_only_no_temporal_amplitude | Zonal amplitudes exist but observation accumulation period/datum not established; old 240-340mm temporal transfer is not calibration. |
| SRC01 / FIL-PLAN-TOPOLOGY | Fig.13b | spatial_topology | — | ACCEPTED_STRUCTURAL_CONSTRAINT | medium | unused_or_context_only | preserve_existing_network_geometry | Existing accepted reconstructed geometry retained; no claim of official metric georeference. |
| SRC10 / GORSKY-STAROBIN | 9 (printed 73) | short_interval_bowls | — | QUALITATIVE_ONLY | medium | unused_or_context_only | bowl_shape_context_only | 9cm/13days and 150-300m bowl sizes describe another deposit; do not calibrate SKRU-1 spatial scale. |
| SRC02 / BOBROVITSKY-OTHER-SITE | title and contents | site_scope | — | REJECTED | medium | unused_or_context_only | out_of_scope | Another deposit; no identified transferable numerical SKRU-1 constraint. |
| SRC07 / GUSEV-GENERAL | title and remote sensing chapters | methodology | — | QUALITATIVE_ONLY | medium | unused_or_context_only | no_new_generator_parameter | No site-specific numeric calibration selected. |
| SRC08 / SHIRSHOVA-OTHER-SITE | 1-10 | DInSAR workflow | — | QUALITATIVE_ONLY | medium | unused_or_context_only | no_new_generator_parameter | Other site; no transferable leveling sigma or temporal trajectory. |
| SRC09 / SHOKIN-COMPUTATION | 1-11 | InSAR computational methodology | — | QUALITATIVE_ONLY | medium | unused_or_context_only | no_new_generator_parameter | Computational workflow does not identify SKRU-1 dynamics. |
| SRC11 / MIKOV-KUZBASS | 1-10 | thermodynamic_and_geomechanical_remote_signals | — | REJECTED | medium | unused_or_context_only | no_new_generator_parameter | Previously corrected source identity: not SKRU-1/2; coal setting not numerical donor. |

### Четыре опубликованных профиля

| Линия | Метод | Период | Позиций | Distinct | Approximate | Unknown | Signed min…max, мм | Read ±, мм | Численная генерация |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | leveling | 2011-2016 | 14 | 7 | 4 | 3 | -349.900…-96.700 | 14.400…20.300 | True |
| 1 | leveling | 2015-2016 | 14 | 14 | 0 | 0 | -69.900…0.600 | 3.600…3.600 | True |
| 1 | radar | 2011-2016 | 14 | 14 | 0 | 0 | -369.600…-15.500 | 3.600…3.600 | False |
| 1 | radar | 2015-2016 | 14 | 7 | 7 | 0 | -72.200…-0.600 | 3.600…9.600 | False |
| 17 | leveling | 2011-2016 | 11 | 9 | 0 | 2 | -175.800…-70.700 | 1.800…1.800 | True |
| 17 | leveling | 2015-2016 | 11 | 11 | 0 | 0 | -26.300…-12.800 | 1.800…1.800 | True |
| 17 | radar | 2011-2016 | 11 | 11 | 0 | 0 | -160.900…-51 | 1.800…1.800 | False |
| 17 | radar | 2015-2016 | 11 | 3 | 8 | 0 | -20.600…-4.200 | 1.800…4.800 | False |
| 5 | leveling | 2011-2016 | 24 | 19 | 5 | 0 | -308.700…-119.600 | 3.100…8.400 | True |
| 5 | leveling | 2015-2016 | 24 | 24 | 0 | 0 | -55.900…-15.700 | 3.100…3.100 | True |
| 5 | radar | 2011-2016 | 24 | 5 | 19 | 0 | -284.700…-122.800 | 3.100…8.400 | False |
| 5 | radar | 2015-2016 | 24 | 0 | 24 | 0 | -46…-21.900 | 8.400…8.400 | False |
| 6 | leveling | 2015-2016 | 31 | 30 | 1 | 0 | -48.600…4.300 | 0.500…1.300 | True |
| 6 | radar | 2015-2016 | 31 | 31 | 0 | 0 | -51…-0.800 | 0.500…0.500 | False |

Все строки таблицы относятся к SUP01, PDF p.15. Отрицательный знак исходного displacement сохранён. `Distinct` — однозначно считываемый маркер/кривая, не число уникальных численных значений. Read ± — консервативный предел считывания изображения, не стандартная неопределённость нивелирования. В детальной Line 1 он шире, чем в общем атласе, что тоже сохранено.

Пространственная ось имеет печатные distance labels, но фактическая раскладка категориальна: неравные/повторяющиеся подписи не дают метрическую интерполяционную ось. Геопривязки к реконструированным point IDs нет. Допускаются порядковое сравнение форм и сопоставление двух методов на одинаковых читаемых слотах одного периода. Скрытые значения не интерполированы. Перечень каждой позиции и её происхождение: [canonical_digitization.csv](../../artifacts/reconstruction/scenario_constraints_v2/canonical_digitization.csv).

Для разности `S_2011–2016 − S_2015–2016` не доказаны общий datum/reference, точные границы кампаний и отсутствие независимой нормировки. Совпадения единиц, знака и названий линий недостаточно. Поэтому такая разность **не вычисляется**; каждая серия — отдельный spatial interval constraint. Номинальные длительности 5/1 лет используются только как явно условная simulation interpretation годовых подписей, не измеренная скорость реального репера.

### Radar vs leveling

| Линия | Период | Пар | Mean radar−leveling, мм | RMS, мм | Max abs, мм | r |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 2011-2016 | 11 | -1.355 | 27.445 | 46 | 0.967 |
| 1 | 2015-2016 | 14 | 5.893 | 8.799 | 15 | 0.966 |
| 17 | 2011-2016 | 9 | 15.167 | 23.524 | 31 | 0.917 |
| 17 | 2015-2016 | 11 | 5.100 | 5.845 | 8.600 | 0.869 |
| 5 | 2011-2016 | 24 | 23.825 | 41.777 | 95.100 | 0.858 |
| 5 | 2015-2016 | 24 | -1.837 | 9.090 | 14.100 | 0.691 |
| 6 | 2015-2016 | 31 | -2.158 | 7.600 | 17.400 | 0.877 |

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

| Показатель | v1 | v2 |
| --- | --- | --- |
| Точки основы | 42 | 42 |
| Профили основы | 14 | 14 |
| Условные зоны | 4 | 4 |
| Плановые кампании | 29 | 29 |
| Сценарии наблюдений | 60 | 640 |
| Latent-world IDs | 5 | 128 |
| Point × scenario trajectories | 2 520 | 26 880 |
| Point × latent-world keys | 210 | 5 376 |
| Различные полные latent fields | 5 | 127 |
| Campaign × point × scenario rows | 73 080 | 779 520 |
| Model origins | 31 134 | 321 766 |
| Targeted campaign rows | 42 780 | 456 320 |
| Доступные наблюдения | 38 464 | 395 334 |
| Missing / targeted, % | 10.089 | 13.365 |
| Сценарии с численно ограниченным масштабом, % | 0 | 98.438 |
| Идентифицированные реальные temporal laws, % | 0 | 0 |
| Интервалы с diagnostic acceleration > 10, % | 34.198 | 22.711 |

42 точки и 29 дат сохранены; период в обеих версиях 2018-05-15…2025-11-04. Latent-world IDs не равны числу независимых полевых наблюдений: два stable world дают одинаковый нулевой field, а пять conditions повторяют один latent world. 5 376 latent trajectory keys также зависимы через shared temporal law/spatial field. Численное ограничение масштаба 98,44% не означает 98,44% реальных или эмпирически идентифицированных временных рядов.

### Распределения и экстремумы

| Величина | Версия | Min | Median | Mean | p95 | p99 | Max |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Latent settlement, мм | v1 | 0 | 114.579 | 119.081 | 275.372 | 332.773 | 401.137 |
| Latent settlement, мм | v2 | 0 | 94.849 | 119.881 | 329.411 | 483.667 | 892.265 |
| Observed settlement, мм | v1 | -20.253 | 103.876 | 113.198 | 275.970 | 336.480 | 413.298 |
| Observed settlement, мм | v2 | -50.909 | 82.399 | 110.670 | 324.756 | 476.207 | 905.603 |
| Latent interval rate, мм/год | v1 | 0.209 | 27.253 | 33.596 | 82.844 | 107.553 | 137.744 |
| Latent interval rate, мм/год | v2 | 0 | 25.769 | 31.568 | 72.709 | 137.021 | 730.073 |
| Latent interval acceleration, мм/год² | v1 | -168.701 | 0 | -0.896 | 31.562 | 101.925 | 165.562 |
| Latent interval acceleration, мм/год² | v2 | -1575.693 | 0 | -0.212 | 23.098 | 117.263 | 1524.341 |
| Latent target rate, мм/год | v1 | 0.580 | 27.335 | 32.793 | 76.368 | 99.342 | 127.031 |
| Latent target rate, мм/год | v2 | 0 | 26.271 | 32.108 | 72.512 | 142.632 | 649.971 |
| Observed target rate, мм/год | v1 | -176.111 | 28.530 | 33.230 | 79.536 | 103.051 | 267.663 |
| Observed target rate, мм/год | v2 | -339.357 | 26.456 | 31.302 | 77.124 | 159.122 | 663.212 |
| Declared observation σ, мм | v1 | 0.800 | 0.800 | 0.823 | 1 | 1 | 1 |
| Declared observation σ, мм | v2 | 0.400 | 0.800 | 0.863 | 1.600 | 2 | 2 |
| Предыдущий observed gap, дни | v1 | 35 | 175 | 197.692 | 433 | 574 | 1 162 |
| Предыдущий observed gap, дни | v2 | 35 | 182 | 204.672 | 433 | 574 | 1 525 |
| Next-planned horizon, дни | v1 | 35 | 175 | 171.478 | 434 | 434 | 434 |
| Next-planned horizon, дни | v2 | 35 | 168 | 171.423 | 434 | 434 | 434 |

Полные min/p05/median/mean/p95/p99/max и число доступных значений: [differential_statistics.csv](https://github.com/SUKUNA-AI/vkm-subsidence-forecasting/blob/c5da1103154c29e2ffe9c52bbceacaa032f1690f/artifacts/data_quality/scenario_simulation_v2/differential_statistics.csv). Latent distribution измерена на всех campaign-point-condition rows; observed distribution — только available rows; target distribution — на origins. Равные latent replicas увеличивают N, не information content. Missingness denominator — targeted, а не все campaign rows (not_targeted — не пропуск).

Порог diagnostic transition = |разность interval rates / midpoint spacing| >10 мм/год² выбран только для описания данных. Это не частота реальных переключений и не IMM label. Доля снизилась с 34,20% до 22,71%; одновременно max interval rate вырос с 137,74 до 730,07 мм/год и max latent settlement с 401,14 до 892,27 мм. Узкие step/episode laws — engineering stress, не эмпирически подтверждённые extrema СКРУ-1. Карта 2016 не использована как жёсткий global clip.

### Process-family distribution

| process_family | old | new |
| --- | --- | --- |
| uniform | 12 | 70 |
| creep_decay | 12 | 0 |
| saturating_acceleration | 12 | 70 |
| reactivation | 12 | 70 |
| moving_spatial_focus | 12 | 70 |
| exponential_decay | 0 | 70 |
| logarithmic_decay | 0 | 70 |
| smooth_acceleration | 0 | 70 |
| temporary_acceleration | 0 | 70 |
| smooth_step | 0 | 70 |
| stable | 0 | 10 |

В v2 stable — exact-null negative control. Uniform мотивирован постоянной скоростью в SRC03. Exponential/log decay — альтернативные инженерные формы замедления, не оценённые по SKRU-1 trajectory. Smooth/saturating acceleration мотивированы существованием acceleration в SRC03, но power/softplus law не восстановлены из источника. Temporary episode, reactivation, smooth step и moving centre — explicit stress tests. Reactivation и moving focus целиком оставлены для heldout-mechanism evaluation. Непрерывные законы, saturating velocity, finite episodes и measurement-only shifts нарушают предположение «весь мир — два режима того же IMM», но не гарантируют отсутствие преимущества B7 на конкретной смеси.

### Profile / zone / missingness distribution

| profile | old | new |
| --- | --- | --- |
| P-D01 | 5 220 | 55 680 |
| P-H01 | 5 220 | 55 680 |
| P-H02 | 5 220 | 55 680 |
| P-H03 | 5 220 | 55 680 |
| P-H04 | 5 220 | 55 680 |
| P-H05 | 5 220 | 55 680 |
| P-H06 | 5 220 | 55 680 |
| P-H07 | 5 220 | 55 680 |
| P-V01 | 5 220 | 55 680 |
| P-V02 | 5 220 | 55 680 |
| P-V03 | 5 220 | 55 680 |
| P-V04 | 5 220 | 55 680 |
| P-V05 | 5 220 | 55 680 |
| P-V06 | 5 220 | 55 680 |

В каждой строке выше campaign rows; по три базовые точки на профиль в обеих версиях. Доступность различается: фактические observed counts каждого профиля находятся в `new_distribution.json`. Published Line 1/5/17/6 не переименовывают эти 14 synthetic profiles.

| proxy_zone | old | new |
| --- | --- | --- |
| GEO_SW | 31 320 | 334 080 |
| GEO_NE | 29 580 | 315 520 |
| GEO_SE | 6 960 | 74 240 |
| GEO_NW | 5 220 | 55 680 |

Зоны — единое для QA разбиение на четыре coordinate quadrants, cut по медианам всех 98 WORK points; это не геологические зоны шахты. Базовых точек SW=18, NE=17, SE=4, NW=3, одинаково v1/v2. В v1 observation table зоны и x/y не сериализовались: для сравнения те же point IDs сопоставлены с неизменённой source geometry. В v2 этот lookup сохранён в point_roster; estimator не получает zone ID.

| missing_reason | old | new |
| --- | --- | --- |
| state_dependent_missing | 1 674 | 9 964 |
| independent_missing | 851 | 13 286 |
| planned_campaign_failure | 720 | 6 144 |
| long_gap | 681 | 6 588 |
| long_gap_background_missing | 232 | 2 070 |
| campaign_failure_background_missing | 158 | 1 430 |
| reflector_snow_unavailability | 0 | 21 504 |

Сценарии пяти условий не являются полным декартовым произведением всех noise × missingness механизмов: пары зафиксированы для ограниченного stress design. Поэтому их отдельные причинные эффекты из общей таблицы не идентифицируются. Measurement conditions: ordinary=256, gross=128, systematic=128, thermal=128. Для clean/noise сравнений нужен одинаковый latent_world и явное сопоставление доступных origins.

### Ответы на шесть проверочных вопросов

1. **Что действительно изменило генерацию?** Семь читаемых leveling envelopes через A_p и F(b)−F(a), четыре широких spatial shape, 30–50 мм roof-reflector contamination и структурный snow-missingness motif. Тест чувствительности подтверждает изменение synthetic increments при изменении numeric envelope.
2. **Что не изменило её?** Common datum/cross-period subtraction, unidentified temporal inset, map georeference, недоказанная «1 см precision», radar-leveling RMS как leveling sigma, other-site rates, Filatova accumulation period. Все заблокированы allowed_use whitelist.
3. **Что остаётся модельным?** Все temporal laws и их durations/onsets, nominal-window placement, synthetic calendar, observation-noise distribution, outlier frequency, datum drift, большинство missingness probabilities, spatial correlation law и веса смеси. Полный список — раздел E.
4. **Зависимость от произвольных констант?** Существенная, особенно для duration/tail extremes и false transitions. Constants вынесены в generation config либо явно названы в коде/разделе E; они не оценены по B7 ошибкам. Два seeds и семь donor cohorts не заменяют sensitivity analysis. Для будущего robustness study отдельный preregistered release должен варьировать эти assumptions, не подстраивая текущий после model errors.
5. **Не стало ли легче IMM?** Неизвестно до сравнения. Меньшая доля больших ускорений и lower p95 rate могут облегчить некоторые случаи; более сильные хвосты, mismatched laws и pseudo-transitions могут осложнить другие. Распределения не являются доказательством превосходства или честной трудности для одной модели.
6. **Есть ли leakage empirical constraints?** Constraints задают simulation domain до построения split и не попадают в model features. Одни bounds в train/evaluation — общий synthetic prior, не независимая validation. QA использовал открытый synthetic evaluator, поэтому это не новый закрытый field holdout. Availability даты публикаций для прогнозиста не установлена: v2 нельзя объявлять реальным hindcast 2018 года. Будущим model workers evaluator читать нельзя; adapter/scorer должны обеспечивать это разделение.

### Reader-facing sanity plots

### 1. Случайные temporal trajectories

Выбор примеров фиксирован seed 1729 в random_example_selection.json. Все линии — synthetic latent truth; это не восстановленные ряды реальных реперов. Показаны разные формы динамики, а не реализации двухрежимного IMM.

[Historical figure: 1. Случайные temporal trajectories](https://github.com/SUKUNA-AI/vkm-subsidence-forecasting/blob/c5da1103154c29e2ffe9c52bbceacaa032f1690f/artifacts/data_quality/scenario_simulation_v2/figures/01_temporal_families.png)

### 2. Settlement / velocity / acceleration

Скорости являются средними по интервалу. Ускорение вычислено между серединами соседних интервалов; значения при коротком интервале и скачке не трактуются как подтверждённые полевые ускорения.

[Historical figure: 2. Settlement / velocity / acceleration](https://github.com/SUKUNA-AI/vkm-subsidence-forecasting/blob/c5da1103154c29e2ffe9c52bbceacaa032f1690f/artifacts/data_quality/scenario_simulation_v2/figures/02_settlement_velocity_acceleration.png)

### 3. Published profiles и ensemble аналогов

Оранжевые точки — только читаемые leveling values и консервативные read bounds; знак для сравнения обращён в positive-down. Синие формы имеют сопоставимый масштаб и широкий структурный тип. Ось — порядковая/нормированная, без координатного соответствия. Эти ансамбли не являются pointwise fit и не обязаны покрывать каждый маркер. Неизвестные позиции не дорисованы. Небольшой опубликованный подъём на Line 6 не воспроизводится subsidence-only latent laws.

[Historical figure: 3. Published profiles и ensemble аналогов](https://github.com/SUKUNA-AI/vkm-subsidence-forecasting/blob/c5da1103154c29e2ffe9c52bbceacaa032f1690f/artifacts/data_quality/scenario_simulation_v2/figures/03_published_profiles_and_analogues.png)

### 4. Published bounds и simulated interval maxima

Сравнение выполняется по отдельным номинальным окнам 1/5 лет, а не по полному накоплению за 2018–2025. Огибающие относятся к читаемым позициям. Это не распределение реальных annual rates и не независимая валидация: bounds уже использованы генератором.

[Historical figure: 4. Published bounds и simulated interval maxima](https://github.com/SUKUNA-AI/vkm-subsidence-forecasting/blob/c5da1103154c29e2ffe9c52bbceacaa032f1690f/artifacts/data_quality/scenario_simulation_v2/figures/04_empirical_vs_simulated_ranges.png)

### 5. Распределения v1/v2

Сравниваются полные проектные смеси с разными законами и весами. Изменение нельзя причинно приписать только одной новой публикации или только устранению ошибки реконструкции.

[Historical figure: 5. Распределения v1/v2](https://github.com/SUKUNA-AI/vkm-subsidence-forecasting/blob/c5da1103154c29e2ffe9c52bbceacaa032f1690f/artifacts/data_quality/scenario_simulation_v2/figures/05_old_new_distributions.png)

### 6. Campaign cadence и missingness

Календарь плановых кампаний сохранён; наблюдаемые интервалы меняются из-за пропусков. Зимняя недоступность относится к reflector contamination. Частоты пропусков — synthetic assumptions, не оценка реального журнала нивелирования.

[Historical figure: 6. Campaign cadence и missingness](https://github.com/SUKUNA-AI/vkm-subsidence-forecasting/blob/c5da1103154c29e2ffe9c52bbceacaa032f1690f/artifacts/data_quality/scenario_simulation_v2/figures/06_cadence_missingness.png)

### 7. Процесс наблюдений

Показаны независимый шум, грубые выбросы, общий datum shift и эффект отражателя. Reported σ описывает обычную компоненту, не полную ошибку с bias/outlier. Отсутствующее наблюдение не означает нулевую ошибку.

[Historical figure: 7. Процесс наблюдений](https://github.com/SUKUNA-AI/vkm-subsidence-forecasting/blob/c5da1103154c29e2ffe9c52bbceacaa032f1690f/artifacts/data_quality/scenario_simulation_v2/figures/07_measurement_noise_components.png)

### 8. True transition и pseudo-transition

Справа latent velocity строго постоянна: кажущееся изменение создаёт measurement process. Слева true nonlinear acceleration. Температурный рисунок — стресс-аналог мотива из источника, не физически идентифицированная модель InSAR или здания.

[Historical figure: 8. True transition и pseudo-transition](https://github.com/SUKUNA-AI/vkm-subsidence-forecasting/blob/c5da1103154c29e2ffe9c52bbceacaa032f1690f/artifacts/data_quality/scenario_simulation_v2/figures/08_true_vs_pseudo_transition.png)

### 9. Экстремальные случаи

Выбор сделан по данным — максимальной скорости/ускорению/пропуску, без model errors. Короткие smooth-step stress episodes выходят за масштаб карты 2016. Эти случаи проверяют устойчивость и не объявлены характерными для СКРУ-1.

[Historical figure: 9. Экстремальные случаи](https://github.com/SUKUNA-AI/vkm-subsidence-forecasting/blob/c5da1103154c29e2ffe9c52bbceacaa032f1690f/artifacts/data_quality/scenario_simulation_v2/figures/09_extreme_cases.png)

### 10. Пространственные snapshots

Те же 42 точки реконструированной локальной сети, три даты, синтетическое оседание. Интерполированная поверхность и привязка к реальной карте скоростей 2016 не создавались.

[Historical figure: 10. Пространственные snapshots](https://github.com/SUKUNA-AI/vkm-subsidence-forecasting/blob/c5da1103154c29e2ffe9c52bbceacaa032f1690f/artifacts/data_quality/scenario_simulation_v2/figures/10_spatial_snapshots.png)

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

Сборка dataset использует уже замороженный constraints release, проверяя его против config; scratch constraints предназначены для побайтового сравнения, не для неявной подмены входов. [Reproducibility receipt](https://github.com/SUKUNA-AI/vkm-subsidence-forecasting/blob/c5da1103154c29e2ffe9c52bbceacaa032f1690f/artifacts/data_quality/scenario_simulation_v2/reproducibility/dataset_reproducibility.json) содержит hashes всех 20 файлов и подтверждает равенство двух runs и опубликованной копии. [Constraints receipt](https://github.com/SUKUNA-AI/vkm-subsidence-forecasting/blob/c5da1103154c29e2ffe9c52bbceacaa032f1690f/artifacts/data_quality/scenario_simulation_v2/reproducibility/constraints_reproducibility.json) подтверждает 6 файлов.

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

Доказательства: [independent validation](https://github.com/SUKUNA-AI/vkm-subsidence-forecasting/blob/c5da1103154c29e2ffe9c52bbceacaa032f1690f/artifacts/data_quality/scenario_simulation_v2/reproducibility/independent_validation_release.json), [test execution](https://github.com/SUKUNA-AI/vkm-subsidence-forecasting/blob/c5da1103154c29e2ffe9c52bbceacaa032f1690f/artifacts/data_quality/scenario_simulation_v2/reproducibility/test_execution.json), [reader QA](https://github.com/SUKUNA-AI/vkm-subsidence-forecasting/blob/c5da1103154c29e2ffe9c52bbceacaa032f1690f/artifacts/data_quality/scenario_simulation_v2/reproducibility/reader_qa.json), [initial inventory](https://github.com/SUKUNA-AI/vkm-subsidence-forecasting/blob/c5da1103154c29e2ffe9c52bbceacaa032f1690f/artifacts/data_quality/scenario_simulation_v2/reproducibility/baseline_inventory.json), [git status](https://github.com/SUKUNA-AI/vkm-subsidence-forecasting/blob/c5da1103154c29e2ffe9c52bbceacaa032f1690f/artifacts/data_quality/scenario_simulation_v2/reproducibility/git_status.txt), [change inventory](https://github.com/SUKUNA-AI/vkm-subsidence-forecasting/blob/c5da1103154c29e2ffe9c52bbceacaa032f1690f/artifacts/data_quality/scenario_simulation_v2/reproducibility/change_inventory.json), [new text files patch](https://github.com/SUKUNA-AI/vkm-subsidence-forecasting/blob/c5da1103154c29e2ffe9c52bbceacaa032f1690f/artifacts/data_quality/scenario_simulation_v2/reproducibility/new_text_files.patch), [finalization manifest](https://github.com/SUKUNA-AI/vkm-subsidence-forecasting/blob/c5da1103154c29e2ffe9c52bbceacaa032f1690f/artifacts/data_quality/scenario_simulation_v2/finalization_manifest.json).

Начальный Git уже содержал untracked v1 scenario/benchmark files. Они сохранены отдельно от новых v2 additions в change inventory. Tracked diff пуст; содержательный diff новых untracked файлов сохранён явно, generated releases представлены manifest hashes. Никакие источники/ZIP, старые releases и frozen experiments не перезаписаны. Коммит, обучение, выбор победителя и изменение suite primary не выполнялись.

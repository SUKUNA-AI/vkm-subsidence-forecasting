<!-- Аудит Phase 1 (cloud, 26.09.2026), выполнен до reset на дереве старого main. Пути cloud VM заменены логическими именами. Статус исправлений — REPRODUCIBILITY_REPORT_RU.md. -->
# Структурный аудит PUBLIC-репозитория `vkm-subsidence-forecasting` (Task A)

Дата: 26.09.2026. Режим: read-only (репозитории не изменялись; эксперименты — в копии
`$VKM_WORK/run/audit/scratch_sim`, полученной через `git archive`).

Машиночитаемая таблица: [`public_repo_component_audit.csv`](public_repo_component_audit.csv)
(219 строк: path, kind, size_bytes, n_files, classification, reason_ru, reusable_parts,
depends_on, referenced_by). Разбор падающих тестов по одному: [`public_repo_test_failures.csv`](public_repo_test_failures.csv).

## 0. Снимок и оговорки

| Параметр | Значение |
|---|---|
| Ветка | `research/evidence-worldspec-reset` |
| HEAD в начале аудита | `1c1f816` (= PUBLIC `main`) |
| HEAD при финальной классификации | `d95344e` (координатор параллельно закоммитил `c68db75`, `e9f3a57`, `d353c84`, `fb460de`, `d95344e`: WorldSpec vNext, schemas, docs/worldspec, docs/architecture, новые governance-документы, `scripts/build_public_catalogues.py`) |
| `legacy` | `d54025d`, является предком HEAD |
| Отслеживаемых файлов | 525; 189 005 029 байт в рабочем дереве (из них 17 LFS-объектов = 162 865 967 байт) |
| Git pack | 49.8 МиБ |
| LFS | materialized (17/17), `git lfs ls-files` помечает `*` |

Pytest запускался при HEAD `1c1f816`. Новые коммиты координатора не трогали `src/skru1`, `tests/test_*.py`,
`configs`, `data`, `artifacts`. Поэтому результат старого стека не изменился. Тесты `tests/world` перезапущены
на `e9f3a57`: 29 passed.

Классы в этом аудите:

- **KEEP_CURRENT** остаётся в новом `main` без изменений или с минимальной правкой.
- **REUSE_GENERIC** — generic-код и правила. Их переносят в `vkm_world` или в будущий validation-слой, после чего исходник удаляется.
- **REBUILD** — компонент нужен, но пересобирается: переписывается или переезжает с новым ID.
- **FROZEN_REFERENCE** — байты уходят из `main`. Идентичность остаётся как SHA/commit-ссылка, проверяемая из git-объектов (раздел 6).
- **MOVE_TO_LEGACY** — уже сохранено в `legacy`, в новом `main` не нужно.
- **REMOVE_FROM_NEW_MAIN** — служебный мусор или устаревшие правила.

## 1. Сводка по классам

| Класс | Строк CSV | Файлов | Байт |
|---|---:|---:|---:|
| KEEP_CURRENT | 25 | 59 | 355 252 |
| REUSE_GENERIC | 12 | 12 | 100 051 |
| REBUILD | 16 | 57 | 4 320 886 |
| FROZEN_REFERENCE | 65 | 230 | 174 735 550 |
| MOVE_TO_LEGACY | 96 | 162 | 9 156 803 |
| REMOVE_FROM_NEW_MAIN | 5 | 5 | 336 487 |
| **Итого** | **219** | **525** | **189 005 029** |

## 2. Области: файлы, байты, классы

| Область | Файлов | Байт | Классы (по файлам) |
|---|---:|---:|---|
| `(root files)` | 9 | 56 067 | REBUILD 6, KEEP 3 |
| `artifacts/.gitattributes` | 1 | 293 | REMOVE 1 |
| `artifacts/data_quality` | 27 | 383 736 | LEGACY 23, FROZEN 4 |
| `artifacts/environment` | 2 | 5 235 | LEGACY 2 |
| `artifacts/governance` | 3 | 32 814 | LEGACY 3 |
| `artifacts/inventory` | 4 | 40 094 | LEGACY 4 |
| `artifacts/model_selection` | 54 | 4 213 929 | LEGACY 34, FROZEN 20 (t1_b3_v1) |
| `artifacts/reconstruction` | 70 | 10 769 568 | REBUILD 31, FROZEN 26, LEGACY 13 |
| `artifacts/repository_cleanup` | 3 | 50 515 | FROZEN 2, KEEP 1 |
| `artifacts/research` | 5 | 869 890 | FROZEN 5 |
| `artifacts/splits` | 59 | 2 229 533 | FROZEN 50, LEGACY 9 |
| `artifacts/status`, `artifacts/verification` | 2 | 165 169 | LEGACY 2 |
| `configs` | 23 | 145 071 | FROZEN 12, LEGACY 8, REBUILD 2, REMOVE 1 |
| `data/.gitattributes` | 1 | 273 | REMOVE 1 |
| `data/published_figure_digitization_v1` | 7 | 283 939 | REBUILD 7 |
| `data/reconstruction_research_v1` | 13 | 2 954 705 | FROZEN 13 |
| `data/scenario_simulation_v2` | 20 | 82 174 146 | FROZEN 20 |
| `data/scenario_simulation_v2_1` | 20 | 82 176 171 | FROZEN 20 |
| `docs` (корень) | 4 | 33 347 | REMOVE 1, REBUILD 1, KEEP 1, LEGACY 1 |
| `docs/architecture` | 1 | 8 154 | KEEP 1 |
| `docs/governance` | 12 | 54 296 | LEGACY 5, FROZEN 3, KEEP 2, REUSE 1, REBUILD 1 |
| `docs/model_cards` | 1 | 4 326 | FROZEN 1 |
| `docs/reports` | 13 | 215 328 | LEGACY 7, FROZEN 5, REBUILD 1 |
| `docs/research` | 22 | 489 255 | FROZEN 18, LEGACY 4 |
| `docs/reset_2026_09` | 20 | 455 993 | KEEP 13, REBUILD 6 (tools), REMOVE 1 |
| `docs/worldspec` | 1 | 18 961 | KEEP 1 |
| `requirements` | 5 | 1 967 | LEGACY 5 |
| `schemas` | 1 | 66 643 | KEEP 1 |
| `scripts` | 32 | 323 537 | LEGACY 17, FROZEN 11, KEEP 2, REUSE 1, REBUILD 1 |
| `scripts/research` | 2 | 7 137 | FROZEN 2 |
| `src/skru1` | 34 | 574 002 | FROZEN 16, LEGACY 12, REUSE 6 |
| `src/vkm_world` | 32 | 87 660 | KEEP 32 |
| `tests` (корень) | 20 | 98 492 | LEGACY 13, REUSE 4, FROZEN 2, REBUILD 1 |
| `tests/world` | 2 | 14 783 | KEEP 2 |

## 3. Тесты: состояние и причины падений

Команда: `. <cloud research venv>/bin/activate; cd PUBLIC; python -m pytest -q -p no:cacheprovider` (HEAD `1c1f816`).

**Итог: 105 passed, 3 failed, 60 errors, 1 skipped, 163 с.** Preflight-результат: 95 passed + 10 новых `tests/world`.

**Все 63 нерабочих теста падают по одной причине: нет retired-данных. Genuine-багов среди них нет.**

| Причина | Тестов | Файлы |
|---|---:|---|
| `load_canonical_bundle` / `configs/gate_a1.yaml` → `SKRU1_ACTUAL_DATA_TABLES_v1/**` (`FileNotFoundError: Gate A1 inputs are missing`) | 46 (43 error + 3 failed) | test_feature_contract (6), test_gate_b0_b1_models (9), test_gate_b0_b1_protocol (1F), test_gate_b2_adaptive_kalman (4), test_gate_b2_protocol (1F), test_gate_b3_imm (4), test_gate_b3_protocol (1F), test_gate_b4_robust_imm (3), test_leakage_guards (4), test_split_contract (8), test_target_contract (5) |
| фикстура `foundation` → `SKRU1_ACTUAL_DATA_TABLES_v1/01_reconstruction_v3_2/tables/survey_points.csv` | 17 error | test_data_foundation_v2 |
| by design: Gate C1 torch-адаптеры проверяются только в окружении `gate_c_torch` | 1 skipped | test_gate_c1_models |

Проходят 105 тестов:

- v2.1: representation 29, erratum 15;
- reconstruction: data 12, atlas 4;
- без данных: benchmark_metrics 4, split guards 6, leakage guards 3, prepare_gate_a0 3, C1 probabilistic 2, B4 Student-t 1;
- протокольные hash-проверки Gate B0/B2/B3: 13;
- data_foundation_v2, не зависящие от фикстуры: 3;
- world: 10.

Без `git lfs pull` результат хуже: 66 passed / 7 failed / 85 errors (preflight).

**Моделирование нового дерева.** Копия HEAD `d95344e`, из которой удалены 451 файл списков A+B (раздел 7), содержит 74 файла:

- `pytest`: **29 passed** за 0.27 с. Все тесты из `tests/world`, включая `test_public_tree_has_no_private_leakage`.
- `vkm_world.governance.leakage.scan`: 0 проблем.
- Старый `verify_canonical_repository.py` **падает с исключением**: `FileNotFoundError: configs/source_manifest.csv`, exit 1. Формального FAIL-JSON при этом нет.
- Сломанных Markdown-ссылок 11, все в REBUILD-документах: README.md (9), README_FIRST.md (1), SPECIAL_SECTION_STRUCTURE.md (1). Целевые файлы: REPOSITORY_CONSOLIDATION, GATE_B3_IMM, v2.1 manifest, representation manifest, R1, R2 verdict, PHYSICAL_WORLD_EVIDENCE_STATE, `requirements/`, PATH_POLICY.

## 4. Ключевые компоненты: разбор

### 4.1 Реконструкции Мусихина и Филатовой: что это и можно ли использовать

**Мусихин** (`data/published_figure_digitization_v1`, `artifacts/reconstruction/musikhin_profiles_2011_2016_v1`,
`artifacts/reconstruction/musikhin_line1_2011_2016_v1`, `artifacts/reconstruction/scenario_constraints_v2/canonical_digitization.csv`).

- Источник: `geokniga-02obrabotka.pdf`, стр. 15 (SUP01). SHA `4d20f03c…`, **совпадает с PRIVATE VKM-SRC-002** «Musikhin VKM radar interferometry slides».
  В реестре источник помечен scope `VKM_Solikamsk` и «discovery-only до нахождения первичной публикации профильных линий».
- Содержимое: ручная оцифровка 4 профильных линий (1, 5, 17, 6).
  - Методы: `leveling` (нивелирование) и `radar` (InSAR).
  - Интервалы: `2011–2016` и `2015–2016`. Для линии 6 есть только 2015–2016.
  - 258 позиций, 252 численных. Статусы: 186 `digitized_marker_or_visible_curve`, 66 `partially_occluded_approximation`, 6 `unresolved_occlusion`.
  - Значения: интервальные смещения, мм, negative-down. Размах от −369.6 до +4.3 мм.
  - Ось X категориальная: печатные подписи км. Метрического георефенса нет.
  - Погрешность — это погрешность считывания графика (`digitization_uncertainty_mm_not_measurement_sigma`), а не σ нивелирования.
  - Это **не временной ряд**. Связь с синтетическими точками P-H/P-V/P-D не найдена (`NO_NUMERIC_PROFILE_LINK_FOUND`).
- Линия 1 leveling 2011–2016 переоцифрована детально: 14 позиций, 11 численно. Повторное считывание: RMSE 0.63 мм, max 0.88 мм; это повторяемость метода, а не точность поля.
- `modality_comparison.csv` содержит расхождения radar−leveling по линиям: RMS 5.8–41.8 мм, corr 0.69–0.97. Это полезный материал для спецификации observation operator InSAR vs нивелирование.
- Ограничение R2: доноры — Соликамск, СКРУ-1/СКРУ-2; конкретный рудник не доказан; источник — непрорецензированные слайды.
- **Вердикт: REBUILD как observation evidence.** Порядок:
  1. Новый ID и запись `ObservationDataset`: modality levelling / InSAR, `TemporalSupport` как интервал, scope `VKM_Solikamsk`, статус FACT(digitized) с ограничениями.
  2. Поля `eligible_as_t1_time_series` и `mapped_synthetic_point_id` отбросить.
  3. CSV (значения + локаторы) положить в PUBLIC `evidence/monitoring/`. Это разрешено DATA_AND_PATH_POLICY.
  4. Нативные растры и overlay PNG — вырезки из PDF корпуса. Их хранить в PRIVATE.
- Воспроизводимость:
  - `reconstruct_musikhin_profiles.py` воспроизводим, ему нужен только VKM-SRC-002.
  - `reconstruct_musikhin_line1.py` сейчас **невоспроизводим**: он читает retired `SKRU1_ACTUAL…/reconstruction_config.json` и `data/reconstruction_research_v1`.

**Филатова, рис. 13б** (`artifacts/reconstruction/filatova_figure13b_geometry_v1`):

- 538 замкнутых областей красного планового контура в **пиксельных** координатах растра. 13 областей чувствительны к порогу.
- Метрической привязки и исходных MapInfo-слоёв нет.
- Конфиг зависит от retired `reproduce_v3.py`, `plan_units_reconstructed.csv`, `overview_georeference.csv`, поэтому артефакт невоспроизводим.
- **MOVE_TO_LEGACY.** Метод сегментации и threshold sensitivity можно использовать заново при повторной оцифровке VKM-SRC-023 с provenance и георефенсом.

**`data/reconstruction_research_v1`** — это не наблюдения:

- «ремонт» синтетических таблиц retired v3.2 (`observation_origin=synthetic_monitoring_simulation`, provenance R/S);
- 3654 записи участия, 1274 целевых кандидата.

Входит в hash-closure v2.1, поэтому **FROZEN_REFERENCE**. Как образец описания поля полезен `field_catalog.csv`: `source_available_at ≠ source_reference_year`, availability rule, assumption_id.

### 4.2 v2/v2.1 scenario simulation, frozen manifests и canonical verifier

- `data/scenario_simulation_v2` и `data/scenario_simulation_v2_1`: по 20 файлов и 82 МБ. В каждом 8 LFS `.gz`, из них 7 совпадают по oid; различается только `model_features.csv.gz`.
- Pinned manifests:
  - v2.1 `a268cd78…2c95`, frozen в `71d705d`;
  - constraints v2 `96b88b61…de6ef`;
  - representation R1 `0e5501cb…a5d`, frozen в `71d705d`;
  - предшественник v2 `7bb67939…8c13`, frozen в `ffcf869`.
- **Что проверяет verifier.** При HEAD `1c1f816` результат PASS:
  - 197 файлов main и 12 файлов PRIVATE, 7 manifests, 57 Markdown;
  - 14 retired-ссылок пропущены, все в `SKRU1_ACTUAL_DATA_TABLES_v1/01_reconstruction_v3_2/**` и `02_eda_targets_v1/**`.
  - Hash-closure из трёх pinned manifests охватывает 197 файлов: v2 и v2.1 (40), representation (50), musikhin line1/profiles (47), constraints v1/v2 (7), reconstruction_research_v1 (12), published digitization (5), 13 configs, 8 scripts, 13 модулей `src/skru1`, `SCENARIO_V2_1_REPRESENTATION_PROTOCOL.md` (1), correction_receipt (1).
- Внешние источники verifier ищет в PRIVATE `08_data_archives/main_repo_snapshots/inputs_sources/**`, а это **байтовые дубликаты** канонических `01_…/03_…`. Если PRIVATE удалит дубликаты, verifier сломается даже без изменений в PUBLIC.
- **Если v2/v2.1 уходят из main**, verifier перестаёт работать. Проверено в симуляции: pinned-файлы помечаются missing, затем скрипт падает на чтении `configs/source_manifest.csv`.
  Кроме того, на verifier ссылаются PRIVATE `README.md:40`, `00_registry/MAIN_CONSOLIDATION_RECEIPT.md:37` и `physical_evidence_v1` receipts. Имя скрипта лучше сохранить.
- **Решение (REBUILD, прототип проверен).** Проверять FROZEN_REFERENCE из git-объектов, без checkout и без LFS-загрузки. Прототип: `scratch_build/verify_frozen_refs_prototype.py`, результат в `scratch_frozen_ref_prototype_result.json`.
  - `git show legacy:<path>`: sha256 обычного blob или `oid sha256` LFS-pointer. **LFS oid и есть sha256 содержимого**, он совпадает с manifest.
  - Результат при ref `legacy`=`d54025d`: **PASS, 196 файлов (180 blob + 16 LFS oid), 7 manifests, 14 ссылок `inputs/sources/*` найдены по SHA в PRIVATE `SOURCE_REGISTER.csv`, 0 ошибок**.
  - Все **14/14 retired-ссылок проверяются по SHA на `10452b0`** — последнем commit перед удалением `SKRU1_ACTUAL_DATA_TABLES_v1`/`inputs/bootstrap` в `d25f458`. То есть retired-ссылки можно не пропускать, а проверять по истории.
  - Новый verifier должен:
    1. проверять реестр FROZEN_REFERENCE (раздел 6) из git-объектов;
    2. сверять PUBLIC-каталоги источников с PRIVATE `SOURCE_REGISTER.csv` по SHA, а не по путям дубликатов;
    3. проверять Markdown-ссылки;
    4. вызывать `vkm_world.governance.leakage.scan`;
    5. искать абсолютные и Windows-пути;
    6. выдавать структурированный FAIL вместо исключения.

### 4.3 Gate A/B/C

| Gate | Код | Конфиг | Тесты | Артефакты | Класс |
|---|---|---|---|---|---|
| A0 | scripts/prepare_gate_a0.py | — | test_prepare_gate_a0 (3 pass) | artifacts/{inventory,status,verification} | LEGACY; zip-slip guard → REUSE-идея |
| A1 | scripts/run_gate_a1.py, build_gate_a1_notebook, data_contracts, leakage, splits, preprocessing | gate_a1.yaml (в closure) | feature/target/split/leakage contracts | artifacts/data_quality/*.csv, splits/t1_v1, t5_v1, spatial_quadrants_v1 | LEGACY; leakage/splits → REUSE |
| B0/B1 | baselines, evaluation, metrics, model_selection | gate_b0_b1.yaml | b0_b1_models/protocol | model_selection/t1_b0_b1_v1 (+ frozen_candidate.joblib LFS), frozen_candidate.json | LEGACY; one-time test ledger → REUSE |
| B2 | adaptive_kalman, gate_b2, transition_validation, uncertainty | gate_b2.yaml | b2_* | t1_b2_v1 | LEGACY; conformal → REUSE |
| **B3** | gate_b3, gate_b3_audit, imm_kalman | gate_b3.yaml | b3_* | t1_b3_v1 (идентичен `ce8e57e`) | **FROZEN_REFERENCE @ `ce8e57e`** (B7: MAE 6.545 мм/год, cov95 0.962 — только historical) |
| B4 | robust_imm | — | b4_robust_imm | — | LEGACY |
| B5/B6 | benchmarking, benchmark_metrics | — | benchmark_metrics | artifacts/governance/final_candidate_suite_v3/v4 | LEGACY; метрики → REUSE |
| C1 | gate_c1_models (GRU/TCN), gate_c1_probabilistic | gate_c1.yaml, requirements/gate_c_torch.lock.txt | gate_c1_* | — | LEGACY |

`gate_b3.py` после `ce8e57e` дополнен приёмкой «governed suite v4 successor» (+82/−26 строк). Точное повторение возможно только на `ce8e57e`.

### 4.4 Leakage- и split-контракты, метрики: что переносить (REUSE_GENERIC)

| Исходник | Что именно переносить | Куда |
|---|---|---|
| `src/skru1/leakage.py` | `forbidden_field_reason` + `FUTURE_NAME_PATTERN`, `FORBIDDEN_PREFIXES` (true_/hidden_/generator_/private_), `assert_t1_time_alignment`, `assert_disjoint_sample_sets`, `assert_positive_horizon`, `find_forbidden_split_api_usage`/`assert_no_forbidden_split_api_usage` | `vkm_world/governance/` (сейчас там только guard PRIVATE→PUBLIC) или будущий `vkm_world/validation/` |
| `src/skru1/splits.py` | `reject_random_train_test_split`, `reject_plain_kfold`, `validate_splitter_name`, `rolling_origin_assignments` + `_assert_rolling_origin_order` (forward-only), `leave_one_group_out_assignments`, `sample_id_list_sha256`, `SealedTestError`/`_authorize_test_access` | `vkm_world/validation/splits.py` |
| `src/skru1/model_selection.py` | одноразовый ledger доступа к test (`claim_test_access`/`finalize_test_access`, `RepeatedTestAccessError`), иммутабельный `freeze_candidate` | `vkm_world/validation/` |
| `src/skru1/benchmark_metrics.py`, `uncertainty.py` | interval score, WIS, normal CRPS/NLL, finite-sample conformal quantile, scaled conformal, LOCO jackknife, paired cluster sensitivity | `vkm_world/validation/metrics.py` |
| `src/skru1/artifact_io.py` | `resolve_repo_path` (запрет абсолютных путей и выхода за корень), `write_*_atomic`, `snapshot_paths`, `artifact_inventory` | `vkm_world/core/io.py` (нужно `build_public_catalogues.py` и verifier) |
| `scripts/capture_environment.py` | `package_versions`, `run_command`, `total_memory_bytes` — receipt окружения | `scripts/` receipts |
| тесты | data-free: `test_cross_split_overlap_is_rejected`, `test_source_scanner_finds_forbidden_split_calls`, `test_unsafe_row_split_guards_raise`, `test_unsafe_splitter_names_raise`, 4 теста метрик; availability-тесты из `test_reconstruction_data.py` (unknown/future source unavailable, known future timestamp checked, next successful observation не заменяет пропущенную плановую цель) | `tests/world/` → проверки `TemporalSupport.usable_at` |
| `docs/governance/FINAL_EVALUATION_POLICY_V2.md` | раскрытый test = только historical diagnostic; ledger однократного доступа; схема, метрики и критерии фиксируются до открытия labels; любая попытка доступа считается израсходованной | будущая VALIDATION_POLICY |
| `configs/experiment_protocol.yaml` (LEGACY) | блок `forbidden:` (random_row_split, test tuning, hidden_truth/generator_params as feature, future_campaign_information, raw_point_id_memorization) | будущая VALIDATION_POLICY |

Идеи без переноса кода:

- `scenario_boundary_v2_1` — audit-hook, запрещающий model worker открывать evaluator payload;
- `scenario_simulation` — разделение latent / missingness / measurement error, соответствует World / Process / Observation;
- `prepare_gate_a0` — `safe_relative_path`/`assert_inside` (zip-slip) и сравнение двух независимых прогонов.

### 4.5 Устаревшие manifests и конфиги

| Файл | Проблема |
|---|---|
| `configs/input_manifest.csv` | 5 записей `inputs/bootstrap/**` (retired); используется только `verify_inputs.py` и `bootstrap_repo.*` |
| `configs/reconstruction_parent_manifest.csv` | 7 путей `SKRU1_ACTUAL_DATA_TABLES_v1/**` |
| `configs/gate_a1.yaml` | canonical bundle полностью на `SKRU1_ACTUAL…`; `discover_project_root` требует его наличия |
| `configs/source_manifest.csv`, `supplementary_source_manifest.csv` | пути `inputs/sources/**` отсутствуют в main; ID `SRC01–11/SUP01` ≠ PRIVATE `VKM-SRC-xxx` (SHA совпадают) → REBUILD как mapping |
| `.gitattributes` | **23 правила** уже сейчас ни на что не указывают (codex/**, inputs/**, repo_bootstrap/**, SKRU1_ACTUAL/**, data/scenario_simulation_v1/**, 6 удалённых скриптов v2, DATA_FOUNDATION_V2_RU.html и др.); после выноса v2/v2.1 устаревает большая часть остальных |
| `configs/.gitattributes`, `data/.gitattributes`, `docs/.gitattributes`, `artifacts/.gitattributes` | только правила для v2.1 → REMOVE |
| `docs/reports/DATA_FOUNDATION_V2_RU.md` | команды воспроизведения ссылаются на удалённые в `f9e811b` `audit_scenario_v2.py`, `validate_scenario_v2.py`, `build_data_foundation_v2_report.py`, `tests/test_scenario_simulation_v1.py` → невоспроизводим даже на `legacy` |
| `scripts/verify_inputs.py` | пишет `INPUT_VERIFICATION.json` в корень репозитория, а не в `work/` (нарушение PATH_POLICY п.12) |
| `requirements/*.lock.txt` | Windows-lock'и старого стека; текущее окружение описано только `run_kit/venv_research_freeze.txt` |

### 4.6 Правила из документов, которые стоит сохранить

Часть правил уже перенесена в новые `docs/governance/SCIENTIFIC_RULES_RU.md` и `DATA_AND_PATH_POLICY_RU.md` (`fb460de`). Не перенесены следующие:

- PATH_POLICY п.1: корень определяется по маркер-файлу или `--root`.
- PATH_POLICY п.10: missing или mismatch current-core/active evidence блокирует работу; retired-ссылки идут отдельным отчётом, а не FAIL.
- DATA_GOVERNANCE: каждый эксперимент фиксирует dataset hash, contract hashes, commit, seed, environment. Модель, обученная после просмотра test/evaluator, получает новую версию оценки. Historical results привязаны к своему dataset/commit.
- FINAL_EVALUATION_POLICY_V2 и `experiment_protocol.forbidden`: см. 4.4.
- MODEL_RESEARCH_PROGRAM: «число моделей и нейроархитектура не доказательство качества»; «IMM — кинематическая модель, не геомеханический prior».
- SPECIAL_SECTION_STRUCTURE: **утверждённые названия** диплома и спецчасти неизменны. Пункт о запрете выдавать синтетические ряды за первичные измерения СКРУ-1 оставить; пп. 4–8 пересобрать под WorldSpec.
- `acceptance_criteria.yaml`: `absolute_host_paths_in_runtime_config: 0`, `tuning_uses_test: false`.

### 4.7 Жёстко заданные и Windows-пути

Всего 192 совпадения в tracked-файлах при `1c1f816`, без CSV.

| Где | Что | Действие |
|---|---|---|
| README.md:69, README_FIRST.md:45, docs/CANONICAL_RESEARCH_STATE_RU.md:194, docs/REPOSITORY_CONSOLIDATION…:336 | `$env:VKM_RESOURCES_ROOT = "<локальный каталог диплома>\…"` | REBUILD-доки |
| docs/research/PHYSICAL_WORLD_EVIDENCE_STATE_RU.md:539–543 | `E:\vkm-…`, `<локальный каталог диплома>\work\resource_inbox\` | LEGACY |
| scripts/reconstruct_{musikhin_line1,musikhin_profiles,filatova_figure13b}.py, build_scenario_constraints.py, run_gate_a1.py:870–872, src/skru1/scenario_simulation.py:966 | `.\.venv\Scripts\python.exe …` внутри генерируемых README и отчётов | уходят с кодом |
| 4 README в `artifacts/reconstruction/*`, docs/reports (DATA_FOUNDATION_V2 ×9, GATE_A1 ×3, B0_B1 ×6, B2 ×3, B3 ×4, RECONSTRUCTION_ATLAS ×4, MUSIKHIN_LINE1 ×1 + JSON ×2) | Windows-команды воспроизведения | уходят |
| artifacts/environment/environment.json:40 | `C:\WINDOWS\system32\WindowsPowerShell…` | LEGACY |
| docs/reset_2026_09/run_kit/tools/{render_page,grep_vocab,merge_sweep}.py | **исполняемый код** с `$VKM_WORK/…`, `$VKM_RESOURCES_ROOT` (абсолютный путь VM) | REBUILD: env `PUB/RES/WORK` (сейчас это прикрыто исключением `PATH_CHECK_EXEMPT_PREFIXES`) |
| run_kit/{MATH,SWEEP}_PROTOCOL.md, workflows/*.js, localize_paths.sh, preflight_verify_canonical.json, NIGHT_RUN_LEDGER, PREFLIGHT_REPORT, CLOUD_TO_LOCAL_HANDOFF | VM-пути в тексте и prompts | допустимо как журнал (переписываются `localize_paths.sh`) |
| run_kit/preflight_public_pytest.txt | 80 строк трассировок с VM-путями, 335 КБ | REMOVE (заменён `public_repo_test_failures.csv`) |

В исполняемом коде `src/`, `scripts/` и в конфигах абсолютных путей к данным нет. Все Windows-вхождения там — это строки-подсказки воспроизведения.

### 4.8 Упоминания скважины 75, Physical World v1, OGS, GRU/Transformer/IMM в документах, которые остаются

| Документ (остаётся или пересобирается) | PW v1 | OGS | 2D/2.5D | GRU/Transformer/neural | IMM/Kalman | v2.1 | Gate B3 | Скв. 75 |
|---|---|---|---|---|---|---|---|---|
| README.md | 45 | 46 | 23 | — | 9 | 10,15,17,28,47 | 9,81 | — |
| README_FIRST.md | 33 | 33 | 62 | — | — | 13,36,61 | 12 | — |
| AGENTS.md | 27 | 27 | — | — | — | 31,44 | — | — |
| CLAUDE.md | 7 | 7,25,41,45,49,52,70,74,91,101 (cloud-провижининг OGS как текущий приоритет) | — | — | — | — | — | — |
| docs/CANONICAL_RESEARCH_STATE_RU.md | 55,238 | 55,234,238 | 152,238 | 106–112 (R1: GRU, Transformer, Neural CDE) | 15,130 | 10 мест | 16–28 | — |
| docs/LEGACY_DATA_RETIREMENT_2026-09-25_RU.md | 16,73 | 16,73 | 73 | — | — | 32–48 | 26 | — |
| docs/governance/SPECIAL_SECTION_STRUCTURE.md | 22 | — | 22 | — | 20 | 19 | — | — |
| artifacts/repository_cleanup/legacy_data_retirement_receipt.json | поле `not_required_for_physical_world_v1` | `not_required_for_opengEOSys` | — | — | — | — | — | — |
| docs/reset_2026_09/NIGHT_RUN_LEDGER_RU.md | — | — | — | — | — | — | — | **27** («Скважина 75 была model choice reduced-case») |
| docs/reset_2026_09/run_kit/SWEEP_PROTOCOL.md | — | 1 | — | — | — | — | — | **91** («Borehole 75 ≠ SKRU-1») |
| docs/reset_2026_09/run_kit/workflows/repo_audit.js | 3 | 4 | — | 1 | 1 | — | — | 3 (prompt) |
| src/vkm_world/__init__.py, docs/worldspec/WORLD_SPEC_VNEXT_RU.md | — | 16; 9,28,191 (OGS как **будущий адаптер** — согласовано с reset) | — | — | — | — | — | — |
| tests/world/test_foundation.py | — | — | — | — | — | — | — | 81–85 (`BH-75` как тест дубликата ID; не научное утверждение) |

LEGACY-документы тоже содержат эти упоминания, но уходят целиком:

- `MODEL_RESEARCH_PROGRAM`, `PROJECT_STATE`, `RESEARCH_DIRECTION`, `DATA_GOVERNANCE`, `REPOSITORY_CONSOLIDATION`;
- `docs/research/*` (R1: 108 упоминаний нейросетевых архитектур);
- `configs/experiment_protocol.yaml` (stage 3–5: LSTM/GRU/TCN/Transformer/foundation models).

В PUBLIC скважина 75 как **научная опора** нигде не записана: она живёт в PRIVATE `physical_evidence_v1` (Task B).

## 5. Что остаётся в новом `main` и минимальные изменения

**Остаётся** 74 файла, 416 КБ при `d95344e`:

- корень (9 файлов);
- `src/vkm_world/**` (32), `tests/world/**` (2), `schemas/`;
- `scripts/{export_worldspec_schema,build_public_catalogues,verify_canonical_repository}.py`;
- `docs/{worldspec,architecture,reset_2026_09}/**` без `preflight_public_pytest.txt`;
- `docs/governance/{SCIENTIFIC_RULES_RU,DATA_AND_PATH_POLICY_RU,SPECIAL_SECTION_STRUCTURE}.md`;
- `docs/{CANONICAL_RESEARCH_STATE_RU,LEGACY_DATA_RETIREMENT_2026-09-25_RU}.md`;
- `artifacts/repository_cleanup/legacy_data_retirement_receipt.json`.

**Минимальный набор изменений для самосогласованности:**

1. `git rm` по спискам A+B (раздел 7) — после переноса REUSE-кода (п.4).
2. **verifier:** переписать `scripts/verify_canonical_repository.py` под тем же именем. Проверки: реестр FROZEN_REFERENCE из git-объектов (`legacy`, `ce8e57e`, `10452b0`), PRIVATE `SOURCE_REGISTER` по SHA, Markdown-ссылки, leakage scan, абсолютные пути. Структурированный FAIL вместо исключения. Вывод — в `work/`. Добавить тест в `tests/world` (он работает и без LFS).
3. **README.md / README_FIRST.md / AGENTS.md / CLAUDE.md:** переписать под WorldSpec. Убрать 11 битых ссылок, пути `E:\`, цепочку PW v1 → OGS. Список обязательного чтения в AGENTS заменить на `SCIENTIFIC_RULES_RU`, `DATA_AND_PATH_POLICY_RU`, `REPOSITORY_ARCHITECTURE_RU`, `WORLD_SPEC_VNEXT_RU`.
4. **REUSE-перенос:** `vkm_world/core/io.py` (из artifact_io), `vkm_world/validation/{splits,metrics,access}.py` (из splits/leakage/model_selection/benchmark_metrics/uncertainty) плюс их data-free тесты. Нельзя импортировать `skru1` из нового кода: `data_contracts.discover_project_root` требует `configs/gate_a1.yaml`.
5. **`.gitattributes`:** оставить общие LFS-паттерны и eol. Удалить 23 уже устаревших правила и все правила v2/v2.1. Удалить 4 вложенных `.gitattributes`.
6. **`pyproject.toml`:** name → `vkm-world` (packages.find и так найдёт только `vkm_world`). **`requirements/`:** создать `research.lock.txt` из `venv_research_freeze.txt`. README ссылается на `requirements/`.
7. **Observation evidence Мусихина:** CSV → `evidence/monitoring/` с новым ID и manifest. PNG → PRIVATE. Тест целостности из `test_reconstruction_atlas.assert_manifest_outputs` → `tests/world`.
8. **`configs/source_manifest.csv` + `supplementary`** → public-safe `evidence/sources/` mapping `SRCxx/SUP01 → VKM-SRC-xxx` по SHA.
9. **`docs/legacy/LEGACY_INDEX_RU.md`:** реестр FROZEN_REFERENCE (раздел 6) и команды воспроизведения.
10. **run_kit/tools:** параметризовать пути через env. После этого снять исключение `PATH_CHECK_EXEMPT_PREFIXES` для `.py`.
11. **Защитить ссылки тегами.** Тегов в репозитории нет, а ветку можно сдвинуть. Нужны `legacy/final`→`d54025d`, `frozen/gate-b3`→`ce8e57e`, `frozen/scenario-v2.1`→`71d705d`, `frozen/scenario-v2`→`ffcf869`, `archive/v3.2-last`→`10452b0`, `frozen/r2`→`2794c87`. В GitHub включить защиту ветки `legacy`.
12. PRIVATE ссылается на `scripts/verify_canonical_repository.py` (README:40, MAIN_CONSOLIDATION_RECEIPT:37, physical_evidence_v1 receipts). Сохранить имя или обновить PRIVATE (Task C).

После шагов 1 и 3–6 новое дерево проходит `pytest` (подтверждено симуляцией: 29 passed) и leakage scan. Verifier становится осмысленным после шага 2.

## 6. Реестр FROZEN_REFERENCE (для `docs/legacy/`)

| ID | Pinned объект | SHA-256 | Commit | Проверка без checkout |
|---|---|---|---|---|
| SKRU1_SCENARIO_SIMULATION_V2_1 | data/scenario_simulation_v2_1/manifest.json | `a268cd7800e1320a1cc61172f95cf458c7ac7434237b32b288f23a83a97a2c95` | 71d705d (в legacy d54025d) | `git show legacy:<path> \| sha256sum`; closure 196 файлов, прототип PASS |
| SKRU1 constraints v2 | artifacts/reconstruction/scenario_constraints_v2/manifest.json | `96b88b61cf766062c1f831e32b50bdd82441da63c50ce41569a5ffad297de6ef` | legacy | то же |
| SKRU1_SCENARIO_REPRESENTATION_V2_1_R1 | artifacts/splits/scenario_representation_v2_1/representation_manifest.json | `0e5501cb4c3cff570fbd3763047a8dd8738d45706dd6f549556ac428521f5a6d` | 71d705d | то же |
| v2.1 correction receipt | artifacts/data_quality/scenario_simulation_v2_1/correction_receipt.json | `5fc8b24111503e997e6896e7ca6ee1cd7a8628be633d70f7a2febb840d0a2cf5` | 71d705d | pinned в representation manifest |
| SKRU1_SCENARIO_SIMULATION_V2 (predecessor) | data/scenario_simulation_v2/manifest.json | `7bb67939314fbcf548686893e6868c90a2303e1303c401377f197e077fec8c13` | ffcf869 | входит в closure v2.1 |
| SKRU1_RECONSTRUCTION_RESEARCH_V1 (synthetic) | data/reconstruction_research_v1/manifest.json | `c999d2ef98da1645de4831e30b131c8da72abde2f4f1372d1cb9868b98df0dd2` | 90e2e29 | closure |
| Musikhin line1 / profiles atlas | artifacts/reconstruction/musikhin_{line1_2011_2016_v1,profiles_2011_2016_v1}/manifest.json | `5081dc8c…9f06` / `7740a926…bac4` | 9d58f7b | closure |
| Gate B3 (B7 IMM) | artifacts/model_selection/t1_b3_v1/artifact_inventory.csv | `127fa1943159c1bf8b605c1926601dc664f7f15196b5f245410b79650dfaacd4` | **ce8e57e** | inventory перечисляет sha всех выходов |
| Retired v3.2 inputs (14 refs) | SKRU1_ACTUAL_DATA_TABLES_v1/01_reconstruction_v3_2/** | см. `historical_expected_sha256` в verifier-выводе | **10452b0** | 14/14 совпадают через `git show 10452b0:<path>` |
| R2 audit | artifacts/research/r2_adversarial_audit/audit_receipt.json | `f8d0b1d42fc9aae27179ba766364e51e5eb235c848738f704dfffa68363136d6` | 2794c87 | — |
| R1 neural comparators | docs/research/NEURAL_COMPARATORS_V2_1_RESEARCH_RU.md | `932c4ee3bf45a68b1b61fea4d82dc1a20f0dd8f60a41b14344bfb0bcb5151bf3` | c5da110 | — |

## 7. Список удаления из нового `main`

**A. Сразу** (FROZEN_REFERENCE + MOVE_TO_LEGACY + REMOVE): 397 файлов, 184 228 840 байт.
**B. После переноса или пересборки** (REUSE_GENERIC + переезжающие REBUILD): 54 файла, 4 360 144 байт.
**Итого A+B: 451 файл, 188 588 984 байт** (99.8 % байт дерева; 17/17 LFS-объектов). Удаление безопасно: `legacy`=`d54025d` — предок HEAD, история и LFS-объекты сохраняются.

Компактная форма (18 каталогов + 64 файла; полный пофайловый список — `scratch_rm_A_files.txt`, `scratch_rm_B_files.txt`):

```bash
git rm -r -q configs data requirements src/skru1 scripts/research \
  docs/model_cards docs/reports docs/research \
  artifacts/data_quality artifacts/environment artifacts/governance artifacts/inventory \
  artifacts/model_selection artifacts/reconstruction artifacts/research artifacts/splits \
  artifacts/status artifacts/verification
git rm -q artifacts/.gitattributes docs/.gitattributes \
  artifacts/repository_cleanup/canonical_cleanup_receipt.json \
  artifacts/repository_cleanup/resources_externalization_receipt.json \
  docs/REPOSITORY_CONSOLIDATION_2026-09-25_RU.md \
  docs/governance/{DATA_GOVERNANCE_AND_CONTRACTS,FINAL_EVALUATION_POLICY_V2,GATE_B3_PROTOCOL,MODEL_RESEARCH_PROGRAM,PATH_POLICY,PROJECT_STATE,RESEARCH_DIRECTION_RU,SCENARIO_EXPERIMENT_V2_PROTOCOL,SCENARIO_V2_1_REPRESENTATION_PROTOCOL}.md \
  docs/reset_2026_09/run_kit/preflight_public_pytest.txt
git ls-files scripts | grep -v -E '^scripts/(export_worldspec_schema|build_public_catalogues|verify_canonical_repository)\.py$' | xargs git rm -q
git rm -q tests/test_*.py
```

Перед удалением группы B перенести REUSE-части (раздел 4.4) и observation evidence Мусихина (раздел 4.1).

## 8. Главные риски

1. **Verifier.** Без замены (раздел 4.2) удаление v2/v2.1 делает его нерабочим: исключение, а не FAIL-отчёт. На него ссылаются PRIVATE README и receipts.
2. **Хрупкость FROZEN_REFERENCE.** Тегов нет. Ссылки держатся на ветке `legacy` и на недостижимых без тегов commit (`ce8e57e`, `10452b0` — предки HEAD, пока история `main` не переписывается). Force-push или squash истории `main` уничтожит проверяемость.
3. **Observation evidence Мусихина.**
   - Статус discovery-only (слайды, рудник не доказан, ось X категориальная, нет георефенса).
   - Не превращать в «временные ряды СКРУ-1».
   - Без переноса в `evidence/monitoring/` единственная численная наблюдательная evidence PUBLIC уйдёт вместе с v2.1-closure.
   - Line1 невоспроизводим без retired v3.2.
4. **Двойная зависимость verifier от PRIVATE-дубликатов** `08_data_archives/main_repo_snapshots`. Дедупликация PRIVATE ломает проверку.
5. **Параллельные коммиты.** Аудит классифицирует дерево `d95344e`. Всё, что координатор добавит позже, требует дельта-классификации. Правило по умолчанию: новое в `src/vkm_world`, `docs/{worldspec,architecture,governance}`, `evidence/`, `catalogues/` → KEEP_CURRENT.
6. **Инструкции.** README / README_FIRST / AGENTS / CLAUDE и `CANONICAL_RESEARCH_STATE` всё ещё предписывают PW v1 → 2D/2.5D OGS. Новая сессия без явного reset-запроса вернётся к старой логике.
7. **Исполняемые run_kit tools с VM-путями** прикрыты исключением в leakage-guard. Это нарушает PATH_POLICY и маскирует такие пути в будущем коде внутри `docs/reset_2026_09/run_kit/`.

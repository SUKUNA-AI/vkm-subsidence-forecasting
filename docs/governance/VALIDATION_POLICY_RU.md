# Политика валидации прогнозов

Документ задаёт правила честной проверки будущего алгоритма прогноза оседаний. Правила проверяются
кодом `vkm_world.validation` и data-free тестами `tests/world/test_validation.py`.

## 0. Статус и границы

- **Прогнозный бенчмарк — это будущая фаза.** Сейчас (Phase 1) разрешены только контракты,
  guard-код и тесты без данных. Для прогнозного бенчмарка, обучения и тюнинга ML, физического
  ансамбля нужно отдельное решение (`SCIENTIFIC_RULES_RU.md`, §6).
- Правила перенесены из документов старой архитектуры: `FINAL_EVALUATION_POLICY_V2.md`, блок
  `forbidden:` в `configs/experiment_protocol.yaml`, `DATA_GOVERNANCE_AND_CONTRACTS.md` и
  `MODEL_RESEARCH_PROGRAM.md`. Все они остаются в ветке `legacy` (`d54025d`). Код переписан заново,
  данные и артефакты старых Gate не переносятся.
- Если правило этого документа противоречит `SCIENTIFIC_RULES_RU.md`, приоритет у
  `SCIENTIFIC_RULES_RU.md`.

## 1. Порядок эксперимента (preregistration)

Порядок жёсткий, шаги не переставляются:

1. **До открытия меток** фиксируются и хешируются контракты признаков и целей, схема разбиения
   и holdout, набор метрик и критерии приёмки. В записи кандидата это поля `contract_hashes`,
   `manifest_hashes` и `evaluation_spec_sha256`.
2. **Кандидат замораживается** функциями `build_candidate_record` и `freeze_candidate`.
   - Запись неизменяема: `candidate_id` — это хеш её содержимого.
   - Правка после заморозки обнаруживается (`verify_candidate_record`).
   - Повторная заморозка другого содержимого по тому же пути запрещена.
3. **Доступ к test однократный.**
   - Сначала `claim_test_access`: ledger создаётся эксклюзивно, до чтения любой метки.
   - Затем метки открываются.
   - В конце `finalize_test_access` со статусом `consumed` или `failed_after_claim`.
4. **После открытия запрещены:**
   - подбор параметров;
   - смена признаков;
   - перекалибровка интервалов;
   - выбор другой модели по результату holdout.

**Любая попытка доступа считается израсходованной**, в том числе аварийная после claim. Повторный
claim даёт `RepeatedTestAccessError`. Без замороженного кандидата, совпадающего с текущими
контрактами и manifest, test закрыт (`authorize_test_access` → `SealedTestError`).

Модель, изменённую после просмотра test/evaluator truth, оценивают только как **новую версию**
на новом holdout. Старый test для неё не может быть нетронутым доказательством.

## 2. Что фиксирует каждый эксперимент

| Поле записи кандидата | Смысл |
|---|---|
| `dataset_sha256` | хеш входного датасета |
| `contract_hashes` | хеши контрактов признаков и целей |
| `manifest_hashes` | хеши упорядоченных списков `sample_id` (`sample_id_list_sha256`), минимум `train` |
| `evaluation_spec_sha256` | хеш спецификации метрик и критериев приёмки |
| `code_commit` | git commit кода и конфигурации |
| `random_seed` | seed |
| `environment_sha256` | хеш receipt окружения (версии пакетов, платформа) |
| `artifact_hashes` | хеши замороженных артефактов: конфиг, модель, отчёт разработки |

Исторические результаты привязаны к своему датасету и commit и не переносятся в новые сравнения
без повторного запуска.

## 3. Раскрытый test — только историческая диагностика

- Набор `t1_v1/test` старой архитектуры был однократно раскрыт для кандидата Gate B0/B1. С тех пор
  он годится только для исторической диагностики. Им нельзя подбирать параметры, выбирать
  семейство моделей, калибровать интервалы или подтверждать критерии.
- Итог Gate B5/B6 (suite v4; единственный primary — B7/IMM из Gate B3, `ce8e57e`) имеет статус
  `train_only_internal_research` и построен на retired-пакете `SKRU1_ACTUAL_DATA_TABLES_v1`. Как и
  результаты остальных Gate старой архитектуры, в новую цепочку доказательств он не входит.
- Любой новый holdout, раскрытый хотя бы раз, получает тот же статус.

## 4. Запрещено

| Запрет | Источник | Чем обеспечивается |
|---|---|---|
| случайное разбиение строк, обычный/стратифицированный K-fold, `ShuffleSplit`, `shuffle=True` | `experiment_protocol.forbidden.random_row_split` | `validate_splitter_name`, `reject_random_train_test_split`, `reject_plain_kfold`; AST-сканер `find_forbidden_split_api_usage` |
| тюнинг на test | `test_hyperparameter_tuning`, FINAL_EVALUATION_POLICY_V2 | закрытый test, `authorize_test_access`, одноразовый ledger |
| hidden truth как признак | `hidden_truth_as_feature` | префиксы `true_`/`hidden_`/`private_` (`forbidden_field_reason`) |
| параметры генератора как признак | `generator_parameters_as_feature` | префикс `generator_` |
| терминальная карта/итог как признак | `terminal_map_as_feature` | явный список контракта (`extra_forbidden`) |
| информация будущих кампаний | `future_campaign_information` | токены `future`/`next`/`target` в имени поля, доступность на дату origin, плановая цель (§5) |
| запоминание сырых ID точек и кампаний | `raw_point_id_memorization` | `IDENTIFIER_FIELDS`, суффикс `_campaign_id` |
| метаданные метки (`label_status`, `missing_reason`, …) как признак | контракт целей | `POST_OUTCOME_FIELDS` |
| LLM как прямой численный прогнозист primary-кандидата | `direct_llm_numeric_forecaster_as_primary` | правило политики |
| подбор физических параметров по test/evaluator truth | SCIENTIFIC_RULES §5 | правило политики, закрытый test |

## 5. Время и доступность информации

- Прогноз на дату origin `t0` использует только данные с известной доступностью
  `available_from ≤ t0` (`TemporalSupport.usable_at`, `chronology.known_at`, `assert_available_at`).
- **Неизвестная доступность = данные не используются** (fail closed).
- Дата измерения, год публикации или snapshot сами по себе доступность не задают.
  - Значение по умолчанию назначается явно при каталогизации по решению D-03
    (`PHASE1_DESIGN_DECISIONS_RU.md`): для литературы это дата публикации с пометкой
    `ASSUMED_FROM_PUBLICATION`, для внутренних данных предприятия — UNKNOWN, пока нет свидетельства.
  - Guard доступность не выводит.
- Роль прогнозиста по умолчанию — внешний исследователь (`EXTERNAL`) без доступа к внутренним данным
  предприятия (D-03, D-16). Роль оператора (`OPERATOR`) допускается, только если бенчмарк явно объявит её при
  предрегистрации. Правила по классам данных для обеих ролей — колонки `forecaster_role` и `benchmark_rule`
  матрицы `evidence/lifecycle/information_lifecycle_matrix.csv` (находка CHRONOLOGY-022).
- Измерение до `t0`, обработанное после `t0`, на дату `t0` недоступно.
- Для каждого образца `origin < target`, горизонт положителен и равен `target − origin` в днях
  (`assert_time_alignment`, `assert_positive_horizon`).
- **Целью служит плановая эпоха** (`planned_target`, `assert_planned_target`):
  - если плановая эпоха пропущена (не измерена или отбракована QC), цель цензурирована;
  - следующее успешное наблюдение её **не заменяет**, иначе выборка отбирается по исходу.
- Исключение для плановой информации: в признаки допускаются только поля, известные по плану
  наблюдений на дату origin (`forecast_horizon_days`, `target_campaign_type`).

## 6. Схемы валидации

- **Rolling origin, только вперёд** (`rolling_origin_assignments`, `assert_forward_only`).
  - На срезе `c` в обучение идут только метки, известные к `c` (`target ≤ c`, для строгости —
    даты доступности меток).
  - Валидация — образцы с origin `c`.
- **Групповые отложенные выборки** проверяют пространственную экстраполяцию:
  - leave-one-borehole-out, leave-one-line-out;
  - leave-one-profile/benchmark/point/zone/panel/site-out;
  - `GroupKFold`.

  Для временной экстраполяции групповые схемы комбинируются с rolling origin.
- **Запрещены:**
  - построчный leave-one-out;
  - leave-one-campaign-out: временная группа не даёт схему «только вперёд»;
  - любые схемы с `random`/`shuffle`;
  - K-fold без групп.

  Нераспознанное имя схемы отклоняется (fail closed).
- Схемы разработки не принимают образцы закрытого test (`sealed=` → `SealedTestError`). Множества
  train/validation/test не пересекаются (`assert_disjoint_sample_sets`).
- Manifest фиксируется хешем упорядоченного списка `sample_id`. Изменённый manifest — это новая
  версия разбиения, а не правка старой.

## 7. Метрики

- **Точечные:** MAE, RMSE, bias; ошибка = прогноз − истина.
- **Интервальные:** покрытие, interval score, WIS (Bracher et al., 2021).
- **Вероятностные:** CRPS и NLL нормального прогноза.
- **Конформные интервалы:** конечновыборочный квантиль ранга `⌈(n+1)·c⌉`. Если ранг больше `n`,
  интервал бесконечен и к максимуму выборки не обрезается.
- Нечисловые и бесконечные значения отклоняются. Цензурированные цели исключаются вызывающим кодом
  явно, их число записывается в отчёт.
- Не перенесены: scaled conformal, LOCO jackknife, paired cluster sensitivity, MASE/skill.
  Их исходники — в `legacy` (`src/skru1/benchmark_metrics.py`, `uncertainty.py`). Переносить их
  при необходимости будут вместе с бенчмарком.

## 8. Интерпретация результатов

- **Число моделей и нейроархитектура не являются доказательством качества.** Качество
  подтверждается только замороженным кандидатом на однократно открытом holdout.
- **IMM — кинематическая модель** режимов скорости, а не геомеханический prior. Её параметры не
  являются свойствами массива и не заменяют физическую постановку WorldSpec.
- Результат модели или решателя не является измерением.
- Синтетические стенды (v2/v2.1) остаются стресс-тестами и не дают field validation.

## 9. Карта: правило → код → тест

| Правило | Код | Тест (`tests/world/test_validation.py`) |
|---|---|---|
| запрещённые поля | `validation.leakage.forbidden_field_reason`, `assert_feature_fields_safe` | `test_forbidden_estimator_fields`, `test_allowed_and_planned_fields_pass` |
| выравнивание во времени | `assert_time_alignment`, `assert_positive_horizon` | `test_time_alignment_requires_origin_before_target_and_positive_horizon` |
| доступность на origin | `core.provenance.TemporalSupport.usable_at`, `chronology.events.known_at`, `assert_available_at` | `test_unknown_or_future_availability_is_unusable`, `test_known_at_checks_availability_timestamp_not_measurement_date` |
| плановая цель | `planned_target`, `assert_planned_target` | `test_next_successful_observation_does_not_replace_missed_planned_target` |
| непересекающиеся выборки | `assert_disjoint_sample_sets` | `test_cross_split_overlap_is_rejected` |
| запрет случайных разбиений | `validation.splits.validate_splitter_name`, `reject_*`, `leakage.find_forbidden_split_api_usage` | `test_unsafe_*`, `test_source_scanner_finds_forbidden_split_calls`, `test_new_code_has_no_forbidden_split_calls` |
| схемы только вперёд и групповые | `rolling_origin_assignments`, `assert_forward_only`, `leave_one_borehole_out`, `leave_one_line_out` | `test_rolling_origin_is_forward_only_and_deterministic`, `test_leave_one_group_out_designs` |
| заморозка и однократный доступ | `validation.access.*` | `test_candidate_record_is_content_addressed_and_immutable`, `test_test_is_sealed_without_matching_frozen_candidate`, `test_test_access_ledger_is_one_time` |
| метрики | `validation.metrics.*` | `test_point_metrics_manual_fixture`, `test_interval_score_and_wis_manual_fixture`, `test_normal_crps_and_nll_closed_forms`, `test_finite_sample_conformal_quantile` |
| пути и атомарная запись | `core.io.resolve_repo_path`, `write_*_atomic`, `snapshot_paths`, `artifact_inventory` | `test_resolve_repo_path_*`, `test_atomic_writers_inventory_and_snapshot` |

# Gate B5/B6: train-only evidence and expanded T1 benchmark

## Статус и область действия

Этот документ фиксирует научную границу двух последовательных этапов:

- **B5 — Evidence & Benchmark Protocol**: заморозка resampling design,
  feature views, metric suite, model registry, grids и окружений;
- **B6 — Expanded Classical, Probabilistic & Small-Data Screening**:
  внутреннее сравнение моделей по замороженному B5-протоколу.

Единственный model-facing источник обоих этапов — 911 строк
`t1_v1/train`. Исторический validation, ранее раскрытый `t1_v1/test` и
отсутствующий future/external holdout не являются входами worker-процессов.
Допустимый scope результата — только `train_only_internal_research`.

Gate C не является синонимом B6. Он остаётся зарезервированным под полноценные
temporal sequence-модели: LSTM, GRU, TCN, TFT, PatchTST и другие архитектуры,
которым требуется отдельный протокол.

## Замороженный benchmark

Версия design: `t1_train_benchmark_v1`. Его машинная authority находится в
`artifacts/splits/t1_train_benchmark_v1/benchmark_plan.json`; SHA-256 plan:
`d143f1c057f176f49c78d9433e4305557339854d1c1faaad77bc356aca8fd926`.

Outer design содержит 65 folds:

- 11 expanding rolling-origin folds с target dates от 2021-05-18 до
  2023-11-07;
- 42 spatio-temporal leave-profile-out folds: 14 профилей на трёх полных
  target campaigns 2022-10-18, 2023-05-16 и 2023-11-07;
- 12 spatio-temporal leave-zone-out folds: четыре замороженные proxy-зоны на
  тех же трёх target campaigns.

Для каждого outer train заморожены последние три допустимых forward-only
inner folds — всего 195 inner contexts. Любой train sample имеет
`target_date` строго раньше outer validation target date. В spatial folds
held-out profile или zone исключается как из outer train, так и из каждого
inner tuning train/validation. Focused campaigns 2023-01-17 и 2023-07-25
участвуют в rolling-origin, но не в spatial CV из-за поддержки только двумя
профилями и одной-двумя зонами.

Random split и обычный KFold запрещены executable guard-ами. Разбиение
проверяется через exact ordered sample-ID hashes, а не только через количество
строк.

## Feature views и structural metadata

Все estimator features сначала проходят исполняемый
`formal_feature_contract.csv`. Зафиксированы три views:

- `SAFE_ALL` — полный allowlist с preprocessing, обучаемым только на train;
- `DYNAMIC_CORE_17` — заранее заданные 17 динамических и profile-aggregate
  признаков для малодатовых kernel/statistical/neural моделей;
- `NATIVE_CATEGORICAL` — `SAFE_ALL` с train-fitted categorical schema для
  CatBoost и EBM.

`point_id`, `profile_id`, campaign identifiers и zone identifiers остаются
только metadata/resampling keys. GEE получает `point_id` отдельно в
`FitContext` как working-correlation group; runtime guard проверяет, что этот
столбец отсутствует в exogenous matrix и prediction features.

Исторический frozen registry содержит 23 preregistered строки. До первой B6
screen aggregation пользователь явно исключил `Z15_tabpfn_v2_6`. Поправка
`configs/gate_b6_amendment_no_tabpfn.yaml` (`B6-GOV-001`) не переписывает B5
freeze, но задаёт исполняемый каталог из 22 моделей. Для Z15 не принималась
лицензия, не загружались веса, не создавались prediction shards и не разрешён
worker/API path. Исключение принято без model evidence и не является
результатом отбора по качеству.

## Двухступенчатая оценка

Сначала все 22 executable preregistered/frozen модели обязаны завершить 11
rolling folds. Историческая 23-я строка Z15 включается в screening register
только как governance exclusion и не создаёт model-facing job.
Новая модель допускается к 42+12 spatial folds, если:

- exact expected sample IDs предсказаны без дублей и нечисловых значений;
- pooled и median rolling MAE не хуже B1 более чем на 10%;
- ни один fold не хуже соответствующего B1 более чем вдвое;
- нет preprocessing, leakage, convergence или environment failure.

Замороженные B1, B3, B5, B6, B7, B8, M1 и M2 проходят spatial audit
независимо от широкого screen. Ошибка качества является научным отклонением
модели, а нарушение data boundary, manifest или environment contract —
`FAIL_PROTOCOL`.

Hyperparameter selection выполняется только на трех inner forward folds.
Основной objective point-моделей — inner MAE с transition guardrail. Для
NGBoost/native probabilistic output используется inner CRPS при условии, что
point MAE не хуже B1 более чем на 5%. При support менее 20 transition origins
guardrail получает статус `UNAVAILABLE_LOW_SUPPORT`, без искусственного
штрафа.

## Interval protocol

Native predictive distributions и quantiles сохраняются отдельно. Для каждой
point-модели строится общий scaled conformal wrapper:

1. residuals берутся только из selected inner rolling OOF predictions;
2. scale использует только train-known horizon и uncertainty;
3. outer label не участвует ни в fit, ни в calibration;
4. публикуются покрытия 50%, 80% и 95%, ширины, central interval score, WIS и
   CRPS;
5. conditional coverage публикуется только при support не менее 30 origins.

MAPE/sMAPE не используются из-за отрицательных и близких к нулю targets. R²
является descriptive statistic. Строки не считаются независимыми: uncertainty
comparisons используют profile-cluster и target-date block resampling по 2000
replicates с seed 42117, а также leave-one-profile-out jackknife.

## Изолированные окружения

`requirements/modeling.lock.txt` не изменяется. B6 использует три независимых
lock-среды:

- `b6_cpu`: sklearn/statsmodels, XGBoost, LightGBM, CatBoost и InterpretML;
- `b6_ngboost`: отдельный совместимый sklearn диапазон для NGBoost;
- `b6_torch`: PyTorch `cp313/cu130`, residual MLP и ENFS; effective runtime
  lock — `requirements/b6_torch_runtime.lock.txt`.

Каждая prediction row несёт environment ID. Aggregator не импортирует
NGBoost или PyTorch: он принимает только schema-validated CSV shards,
проверяет environment/model/fold/hash provenance и независимо пересчитывает
metrics. Durable environment evidence содержит exact `pip freeze`,
нормализованный wheel URL/SHA inventory, hardware/runtime capture, smoke и
two-run determinism reports. Содержимое виртуальных окружений и временные
fold-модели остаются в `work/`.

Boosters работают на CPU. CUDA разрешена только MLP и ENFS. Исторический
`b6_torch.lock.txt` сохраняется неизменным как часть B5 freeze; наличие в нём
старой dependency-записи не создаёт runtime capability. Исполняемый код не
импортирует TabPFN, staging-скрипт удалён, а validator требует отсутствия
license marker, checkpoint и любых Z15 prediction/tuning/worker shards.
Поправка `B6-GOV-001` отдельно связывает torch environment с runtime lock без
этого package; durable `pip_freeze` и wheel inventory также обязаны его не
содержать.

## Suite v4

Новая primary допустима, только если одновременно проходит все
preregistered point, transition, spatial, interval, sign-consistency,
environment, reproducibility и leakage gates. Среди eligible моделей порядок
лексикографический: rolling MAE, transition MAE, worst-zone MAE, 95% WIS, fit
time, model ID. Непрозрачного weighted score нет.

Если ни одна новая модель не проходит все gates, B7 автоматически остаётся
primary, а run получает корректный статус `PASS_NO_NEW_PRIMARY`. Плохое
качество новой модели не является software failure. Suite v4 замораживается
до появления новых labels и содержит `new_holdout_seen=false` и
`primary_selected_from_holdout=false`.

Фактический outcome B6 — `PASS_NO_NEW_PRIMARY`: B7 остаётся primary, а
future-holdout intake v3 принимает именно
`artifacts/governance/final_candidate_suite_v4.json` и проверяет, что suite
имеет ровно один primary и не видел holdout labels.

## Порядок выполнения

Обязательная последовательность:

`B5 freeze → B5 analyze → B5 validate → B6 preflight → B6 screen → B6 robustness → B6 calibrate → B6 freeze → B6 validate`.

Worker CLI не принимает validation/test manifests. Любое расширение data
scope требует нового governance-решения и нового frozen protocol, а не
параметра командной строки.

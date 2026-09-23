# SKRU1_SCENARIO_SIMULATION_V2: data release и следующий ограниченный benchmark

Дата: 2026-09-23. Текущее разрешённое действие — data generation/validation only.
Этот протокол не разрешает запуск моделей без следующего отдельного задания.
Historical Gate B/C, suite v4 и legacy holdout policy сохраняют прежний смысл.

## Data path и provenance

1. Проверенные локальные источники и immutable digitization releases.
2. `artifacts/reconstruction/scenario_constraints_v2`: 42 evidence records;
   каноническая оцифровка с приоритетом принятой детальной Line 1.
3. `configs/scenario_simulation_v2.json`: design assumptions, seeds и input hashes.
4. `src/skru1/scenario_simulation_v2.py`: geometry + constraints → synthetic latent
   truth → synthetic observations → causal derived features / next-planned labels.
5. `data/scenario_simulation_v2`: append-only release, manifest и evaluator boundary.

Запрещено называть эти временные значения реальным мониторингом. Метрическая
геопривязка опубликованных линий к искусственной сети отсутствует. Published,
digitized, reconstructed, derived и два synthetic класса перечислены в
`field_provenance.json`. Source sign negative-down сохранён в evidence;
симулятор использует positive-down. Uncertainty считывания не является σ нивелирования.

Только семь leveling interval envelopes допускаются в численное ограничение
latent scale. Radar/leveling disagreement, карта 2016, other-site rates остаются
контекстом; неизвестная временная кривая и common datum исключены. Roof shift
30–50 mm разрешён только для synthetic reflector-contamination stress.

Номинальные окна 1/5 лет центрированы относительно синтетического периода.
Это явная проектная интерпретация подписи года, не доказанные реальные даты.
Profile extrema относятся к читаемым значениям. Где опубликованные позиции
неизвестны, нулевой floor синтетической формы — допущение, а не восстановление
пропущенных точек. Вероятности, доли и temporal laws не оценены как field priors.

## Разбиение и estimand

Primary estimand: средняя latent rate от origin до следующей **плановой targeted**
кампании. Secondary estimand: observed target rate на подмножестве с доступной
целью. Нельзя подменять неуспешную следующую кампанию более поздней успешной.
Признаки — только 16 полей `feature_contract.json`; ID служат lookup/group keys.
Они, coordinates, proxy zones, constraints, seeds, process families и hidden truth
не входят в estimator features.

Development train: target_date ≤ 2022-10-18; calibration: 2023-01-17…2023-11-07;
future development evaluation: target_date ≥ 2024-01-30. Реактивация и moving focus
целиком исключены из train/calibration. Missing train/calibration targets исключены
из fit, сохраняя evaluator truth. Эти roles относятся к новой simulation study,
не переименовывают исторические validation/test или отсутствующий final holdout.

Численные источник-ограничения заданы до модели/разбиения; empirical data не
являются model features. Использование одних опубликованных bounds во всех roles
— общий simulation domain prior, не независимая field validation. Дата доступности
публикаций прогнозисту неизвестна: это не hindcast реальных событий 2018 года.

Все synthetic evaluator values доступны для разрешённого data QA и графиков.
После заморозки нельзя менять generator/config/mixture по ошибкам моделей.
Будущим model workers запрещён evaluator directory. Scorer получает его отдельно.
Отдельный adapter должен выдавать train targets из `targets/train_observed.csv.gz`,
fit-only preprocessing и causal history до origin. Calibration targets не fit-data.

## Минимальный migration plan (без исполнения)

| Модель | Следующее действие | Граница |
|---|---|---|
| B1 persistence last rate | Без подбора; тот же origin/history/horizon | Обязательный контроль |
| B5 fixed Kalman | Совместим по четырём history columns, `baselines.py:224–228`; зафиксировать q | Обязателен при прохождении adapter smoke tests |
| B6 adaptive Kalman | Новый train scale; небольшой прежний grid в train-only rolling folds | Основной single-regime comparator |
| B7 two-regime IMM | Текущий алгоритм; отдельно fixed-historical parameters и fair bounded train tuning | Не переносить статус primary/старую MAE |
| B8 robust IMM | Один robustness contrast при наличии gross/systematic errors в v2 | Те же dynamics, ограниченный likelihood grid |
| C01 compact GRU | Sequence control после отдельного representation acceptance | Только C01, без C2/model zoo |

Для B5 прогноз постоянной скорости совместим с average-rate target при любом
плановом горизонте. Его fit всё же оценивает fallback median по train — поэтому
даже этот технический fit в текущем задании не выполнялся. B6/B7 требуют train
scales заново: synthetic distribution и acceleration representation изменились.

Сначала реализовать отдельный manifest-checked v2 adapter и проверить равенство
history cutoff, target pairing и sign у всех моделей. Старый v1 adapter загружает
единый samples-table; подмена пути недопустима. Для sequences `sequence_windows`
задаёт IDs последних ≤16 наблюдений, left padding и cutoff. Нужно ещё проверить
runtime tensorization, mask, elapsed Δt и train-only normalization; только после
этого запускать прежнюю compact GRU с пятью фиксированными seeds. Данные для этого
готовы, модельная tensor/runtime migration не объявляется выполненной.

Затем заморозить небольшие forward-only inner folds внутри нового train.
Нельзя случайно разделять строки: пять observation conditions одной latent_world
должны оставаться вместе при разделении реализаций. Для forward temporal scores
история тех же точек разрешена до cutoff, но target intervals train не должны
пересекать validation origins. В spatial folds исключать held profile/zone из
train во всех observation replicas и из cross-profile aggregations. Proxy zones
— прежние четыре coordinate quadrants по 98 WORK points, не реальные блоки шахты.

Основной paired отчёт: одинаковые origin IDs, latent rate/increment MAE и RMSE,
bias, observed-target scores отдельно, coverage/width отдельно для latent и
observed estimands. Bootstrap по latent worlds, а не по строкам; profile macro,
family macro, source-cohort macro, measurement mechanism и gap strata обязательны.
Два seed на cohort/family — мало для стабильной оценки population variance;
интервалы внутри такого factorial не выдавать за неопределённость field accuracy.
Не усреднять reflector-contamination stress с обычным нивелированием без
отдельного разреза: объекты наблюдения различаются.

Minimum question: превосходит ли B7 одновременно last-rate continuation и
одно-режимный Kalman, сохраняя преимущество на разных temporal laws и не создавая
ложных уверенных переключений на measurement-only shifts? Предварительно
зафиксировать критерий практически значимого выигрыша и noninferiority на
stable/ordinary/gap группах. Значения не выбирать после просмотра результатов.
Single primary decision по этому выпуску сейчас не принимается.

## Возможные улучшения

`docs/reports/IMM_V2_IMPROVEMENT_DESIGN_RU.md` описывает Q(Δt), double counting и
CUSUM/GLR. Они не входят в первоначальный frozen v2 comparison. Новые варианты
исследовать отдельными ablations, не меняя generator и evaluator после просмотра
ошибок. Запуск всех 22 моделей Gate B6 или C2 не является migration requirement.

## Release controls

Непустой output запрещён. Повторные сборки — только в новом каталоге
`work/data_foundation_v2/`. Проверять manifests до parsing; генератор не импортирует
модели. Для повторения использовать исходный commit плюс перечисленные working
source hashes: текущий data release сделан без auto-commit. Начальный inventory
защищает старые источники, releases, эксперименты и governance-файлы по байтам.

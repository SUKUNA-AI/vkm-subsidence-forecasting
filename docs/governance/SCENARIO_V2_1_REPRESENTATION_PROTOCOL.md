# Scenario v2.1: model-facing representation protocol v1

Version: SKRU1_SCENARIO_REPRESENTATION_V2_1_R1. Freeze follows full acceptance.

Дата: 2026-09-24. Область: representation acceptance, без fit/predict/scoring моделей.
Parent v2 commit: `ffcf86972d31bd9f0dbd62d1265001f5c722e253`,
`data/scenario_simulation_v2_1/manifest.json` SHA256
`a268cd7800e1320a1cc61172f95cf458c7ac7434237b32b288f23a83a97a2c95`;
constraints manifest SHA256
`96b88b61cf766062c1f831e32b50bdd82441da63c50ce41569a5ffad297de6ef`.
Этот документ дополняет SCENARIO_EXPERIMENT_V2_PROTOCOL; outer roles не меняются.

## Границы доступа

ModelDataBundle содержит target-free frames всех origins, train ManifestDataset с
отдельно присоединённым observed target, target-free calibration/evaluation,
causal history и безопасные identity hashes. Ровно 16 признаков определены frozen
feature_contract.json. Metadata нужны для lookup, joins и grouping; estimator_matrix
возвращает только allowlist. Отдельный CalibrationTargetStore не входит в bundle.
EvaluatorTruthStore загружается только scorer/QA module и не является аргументом
estimator API. Prediction interface: sample_id и prediction; scorer join по sample_id.
В этой задаче scorer loader проверяется, метрики моделей не вычисляются.

Model-worker scope разрешает чтение только model-facing payloads выпуска и
feature_contract; даже проверка manifest не читает evaluator outputs. Python audit
guard запрещает evaluator, calibration targets и прочие неразрешённые payloads
выпуска, legacy data/holdout paths и запуск дочерних процессов внутри worker scope.
Guard действует на Python file-open во всех threads процесса; это проверяемая
граница прикладного API, не OS sandbox против произвольного native кода. Scorer
должен запускаться отдельно; truth object никогда не передаётся worker.

## Inner folds: заранее заданные правила

Только outer train (122047 origins); calibration 35148, evaluation 133833,
excluded 30738 сохраняются. Три validation origin windows: календарные 2020,
2021 и 2022 годы, последний ограничен доступными outer train origins.
Fit target_date строго раньше validation_start минус 30 суток. Validation current_date
в соответствующем окне. Порог фиксирован до любых результатов моделей.

World ID — проверенный prefix scenario_id до суффикса -O[1-5]. Bucket равен
int(SHA256(world ID),16) mod 3. Fold i валидирует bucket i, fit использует остальные.
Это одновременно forward time и unseen-world validation, не random row KFold.
Ни один world и ни одна его observation replica не пересекает fit/validation
внутри fold. Мир может быть fit в другом fold; preprocessing для каждого fold
отдельный и не использует его validation rows. Зависимость двух идентичных zero
latent fields сохраняется в bootstrap caveat; world ID — заданная единица release,
не утверждение статистической независимости всех 128 полей.

## Spatial audits

14 leave-profile-out и 4 leave-zone-out folds используют temporal cutoff последнего
inner окна, только outer train. Единица holdout — base profile/zone во всех worlds
и observation replicas. Здесь миры могут совпадать: estimand — перенос на другую
пространственную группу, а не на unseen world. Group manifests это явно отражают.
Zones — synthetic coordinate quadrants GEO_*, не геологические зоны.

Во всех spatial fold views четыре profile aggregate features вычисляются заново
по contemporaneous history только non-held points. Это относится к fit и validation
frames. В leave-profile-out held profile поэтому имеет count=0, остальные агрегаты
NaN; в leave-zone-out остаются доступные non-held соседи того же profile. Frozen
таблицы не меняются. Held point собственная история разрешена при прогнозе этой
точки; она не входит в fit rows, fit histories или aggregate context. Missing
aggregates проходят fold-only imputation. Tensor channels не содержат агрегатов.

## Network channels (зафиксированы до реализации)

Все value channels float32 после transform, raw вычисления float64. Порядок ниже.
Имена — subset ровно того же feature allowlist; новые predictors не вводятся.

| name | unit | derivation | missing | padding | normalization |
|---|---|---|---|---|---|
| last_settlement_mm | mm positive-down | frozen history value | forbidden for real token | 0 | fold median/mean/std |
| last_rate_mm_y | mm/year | frozen history value; signed delta/elapsed years | first observation NaN | 0 | fold median/mean/std |
| current_standard_uncertainty_mm | mm | frozen history value | forbidden for real token | 0 | fold median/mean/std |
| days_since_previous_observation | day | actual gap to previous observed token in full causal history, including predecessor outside truncated window | first observation NaN | 0 | fold median/mean/std |
| missing_campaigns_since_previous | count | frozen history count, no inserted rows | first observation NaN | 0 | fold median/mean/std |

Structural arrays: padding_mask bool (1 only left padding); observation_mask bool
(1 every real observed token); missing_campaign_mask bool (1 on observed token if
one or more targeted campaigns missed since predecessor; first unknown maps false);
value_valid_mask bool [N,16,5] (finite raw value, false for padding); lengths int64.
Masks не нормируются и не обозначают механизм пропуска. Unknown count сохраняется
как NaN и value_valid_mask=false до imputation, а не как наблюдённый zero.
IDs не входят в network x. Max length=16, порядок и IDs берутся буквально из
sequence_windows. Нет интерполяции, uniform-grid resampling или новых tokens.
Последний real token равен текущему наблюдению. Padding остаётся zero после scaling.

Тот же current_campaign_type для ранних tokens из history не восстанавливается:
исторический C01 config требует категориальный канал, которого в разрешённом
history нет. Generic PackedRecurrentRegressor(input_size=5) совместим по форме;
старый SequenceModelSpec/worker/checkpoint и scaler несовместимы и не переиспользуются.
Результат означает acceptance входа для отдельного будущего v2 worker, не завершённый
neural benchmark. В этой задаче model forward не исполняется.

## Fit-only preprocessing и freeze

Tabular: 14 numeric, 2 categorical features. Numeric median imputation, population
mean/std; all-missing median=0, constant std=1. Category vocabulary только fit,
unknown=0, known=1..K. Sequence: те же правила для пяти каналов; fitting на real
tokens конкретных fit origins (повторение token в разных windows явно допустимо).
State содержит fold ID, ordered fit sample SHA256, schema SHA256, fitted parameters.
IDs fit должны точно совпадать с frozen fold manifest; subsets/supersets/calibration
не допускаются. Полный outer-train fit разрешается только отдельным последующим
benchmark protocol; current acceptance проверяет inner/spatial fit scopes.

Representation manifests write-once; повторение возможно в новом work directory.
После acceptance не менять channels/folds/preprocessing по model errors. Изменения
требуют новой representation version/preregistered sensitivity. Generator, data,
constraints, truth и outer roles остаются immutable. Acceptance не разрешает benchmark.

## V2.1 identity and acceptance extension

Data authority is exclusively data/scenario_simulation_v2_1/manifest.json. No fallback to v2.
Correction receipt must be PASS before loading/generating representation. V2 BLOCKED
reports and the original draft remain historical. Fold dates, embargo, world buckets,
spatial context rules, channels and normalization remain those of the pre-model draft.
Preprocessor creation identity records dataset hash and representation version; no
clock timestamp enters deterministic artifacts. Canonical runtime hashes cover every
origin in frozen order; no giant tensor file is stored.

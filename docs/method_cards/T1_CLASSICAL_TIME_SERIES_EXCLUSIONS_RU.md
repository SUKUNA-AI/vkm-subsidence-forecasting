# Method cards: ETS, ARIMA/ARIMAX и профильный VAR

## Общий статус

`NOT_ELIGIBLE_DATA_GEOMETRY` на этапе `B5_pre_screening`.

Это не список отсутствующих реализаций. Семейства были рассмотрены до запуска
benchmark и исключены по заранее проверяемой геометрии `t1_v1/train`.

Общие наблюдаемые ограничения:

- 4–14 model origins на point trajectory;
- 3–16 доступных historical observations в момент прогноза;
- one-step forecast horizons 42–210 дней;
- point-level inter-observation gaps до 560 дней после пропущенных campaigns;
- 14 profiles и неполная синхронность campaigns;
- повторяющиеся points/profiles, поэтому число строк не равно числу
  независимых рядов.

## ETS / Holt family

**Научная роль, если бы модель была допустима:** регулярный статистический
контроль level/trend dynamics.

**Причина исключения:** короткие нерегулярные ряды с gaps требуют сначала
перевести данные на равномерную сетку. Интерполяция level/rate между campaigns
создала бы значения, которых нет в измерениях, и могла бы искусственно
сгладить именно transitions, являющиеся целевой проблемой T1. При доступной
длине рядов независимая проверка seasonal/damped specification невозможна.

**Что потребовалось бы для пересмотра:** существенно более длинные series на
одинаковой cadence либо отдельный доказательный protocol интерполяции с новым
holdout. Эти условия в B5/B6 не выполнены.

## ARIMA / ARIMAX

**Научная роль, если бы модель была допустима:** autoregressive benchmark с
опциональными origin-known covariates.

**Причина исключения:** 3–16 observations недостаточны для устойчивой
идентификации order/differencing на большинстве origins. Нерегулярная cadence
и пропущенные campaigns нарушают стандартную дискретную индексацию. Любая
регуляризация времени через imputation/interpolation стала бы отдельной,
непредзарегистрированной observation model. Поиск `(p,d,q)` внутри малых inner
folds также был бы статистически неустойчив.

**Что потребовалось бы для пересмотра:** длинные point histories, frozen
irregular-time AR specification либо заранее валидированный regularization
operator и новый evaluation resource.

## Profile VAR / VARX

**Научная роль, если бы модель была допустима:** оценка взаимной динамики
профилей или points внутри профиля.

**Причина исключения:** 14 профилей при небольшом числе неполных synchronous
campaigns дают число параметров, несоразмерное числу независимых temporal
units. Missing campaigns меняют состав наблюдаемого вектора, а построение
полной panel matrix потребовало бы масштабной недоказанной иммутации. Spatial
dependence при этом уже проверяется честнее через held-profile/held-zone
design, не подменяя его memorization профиля.

**Что потребовалось бы для пересмотра:** существенно больше синхронных
campaigns и profiles, заранее ограниченная sparse/low-rank VAR specification и
независимый внешний spatial holdout.

## Машинная authority

Полные structured cards сохранены в
`artifacts/model_selection/t1_b5_evidence_v1/method_cards.json`. Различие
между one-step horizon 210 дней и point-level gap 560 дней записано в
`artifacts/model_selection/t1_b6_expanded_v1/protocol_errata.json` как
`B6-ERRATUM-002`; решение об исключении не менялось.

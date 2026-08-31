# Gate B5: доказательный train-only benchmark T1

## Результат

Gate B5 завершён со статусом **`PASS_PROTOCOL_FROZEN`**. Поверх неизменяемых
911 origins `t1_v1/train` создан и захэширован расширенный
`t1_train_benchmark_v1`: 11 rolling-origin, 42 spatio-temporal
leave-profile-out и 12 spatio-temporal leave-zone-out folds. Для каждого из 65
outer folds зафиксированы три последних допустимых forward-only inner folds —
всего 195 tuning contexts.

Ни исторический validation, ни ранее раскрытый test, ни синтетическая замена
future holdout не загружались. Независимый validator повторно построил design
и прошёл **20 из 20** проверок. Этот Gate фиксирует способ получения
внутренних доказательств; он не утверждает внешнее качество какой-либо модели.

## Геометрия данных

Train содержит:

- 911 model origins;
- 98 повторяющихся point trajectories;
- 14 профилей;
- четыре замороженные spatial proxy zones;
- 19 target dates.

Следовательно, 911 строк нельзя трактовать как 911 независимых наблюдений.
Для старого B4 rolling evidence, например, 292 origins соответствуют только
пяти temporal units, 14 profiles, четырём zones и 98 trajectories.
Внутрипрофильная pairwise correlation residuals в пяти rolling campaigns
достигает примерно 0,46–0,53 для frozen comparators. Именно поэтому B5
запрещает i.i.d. row bootstrap, random split и обычный KFold.

## Forward-only и spatial доказательства

Первый rolling fold оценивает target date 2021-05-18 после восьми предыдущих
campaigns и 316 доступных train origins. Последний fold оценивает 2023-11-07.
В каждом случае максимальный train `target_date` строго меньше validation
`target_date`.

Spatial design удерживает не только группу, но и время:

- для каждого из 14 profiles построены folds на трёх последних полных
  campaigns;
- для каждой из четырёх zones построены folds на тех же campaigns;
- held-out group отсутствует во всём outer train и во всех inner tuning folds;
- focused campaigns 2023-01-17 и 2023-07-25 сохранены в temporal evidence, но
  исключены из spatial CV из-за недостаточной географической поддержки.

Каждый fold contract хранит row/point/profile/zone counts, диапазоны
`current_date` и `target_date`, target/transition distributions, ordered
sample-ID SHA-256 и явное доказательство forward boundary.

## Error atlas B4 без переоценки моделей

B5 не изменял B1/B5/B6/B7/B8 и не подбирал их параметры. Он прочитал 2780 уже
существующих B4 outer predictions и построил 2620 строк error atlas:

- target date, profile, zone и point;
- stable, accelerating, decelerating и volatile-or-gap;
- horizon, history, uncertainty и missing-campaign bins;
- pooled micro, equal-profile и equal-zone macro;
- worst profile, worst zone и worst 10% points;
- residual dependence и фактическое число temporal/profile/zone units.

Сегмент с менее чем 20 origins или менее чем пятью профилями получает
`DESCRIPTIVE_LOW_SUPPORT`. Такой результат остаётся видимым, но не участвует в
model selection.

## Диагностические learning curves

После freeze были построены кривые на неизменяемом audit tail 2023-11-07 для
восьми frozen comparators. Размеры train windows: 217, 423, 708 и 823 строки
(5, 9, 14 и 18 core campaigns). Ни на одном размере параметры не
перенастраивались.

На полном core из 823 строк audit-tail MAE составляет:

| Модель | MAE, мм/год | B1 skill |
|---|---:|---:|
| B8 Student-t robust IMM | 5,831 | +4,47% |
| B7 two-regime IMM | 6,015 | +1,46% |
| B1 persistence | 6,104 | 0,00% |
| M2 ExtraTrees | 6,598 | −8,10% |
| M1 Ridge | 6,799 | −11,39% |
| B6 adaptive Kalman | 6,775 | −11,00% |
| B5 fixed Kalman | 7,194 | −17,86% |
| B3 profile robust trend | 8,486 | −39,02% |

Эта таблица — диагностика одного audit date, а не ranking для финального
выбора. B6 обязан учитывать все 11 temporal dates, profile/zone macro,
transitions, intervals и stability gates.

## Feature contract

Allowlist превращён в исполняемую границу трёх feature views: `SAFE_ALL`,
`DYNAMIC_CORE_17` и `NATIVE_CATEGORICAL`. Идентификаторы point/profile,
campaign и zone не входят ни в один estimator feature set. Все imputation,
scaling и categorical schema обучаются только на соответствующем train.

Special case GEE заранее ограничен: `point_id` разрешён исключительно как
structural working-correlation group, передаваемый отдельно от `X`.

## Formal pre-screening ETS, ARIMA и VAR

Три семейства получили статус `NOT_ELIGIBLE_DATA_GEOMETRY`, а не были
механически обучены:

- на точку приходится только 4–14 model origins и 3–16 доступных наблюдений;
- campaign spacing нерегулярен, one-step forecast horizons лежат в диапазоне
  42–210 дней;
- из-за пропущенных campaigns point-level inter-observation gap достигает 560
  дней;
- ARIMA/ETS потребовали бы недоказанной интерполяции на регулярную сетку;
- профильный VAR при 14 profiles и неполных синхронных campaigns
  неидентифицируем либо крайне неустойчив.

Различие между максимальным one-step horizon 210 дней и максимальным
point-level gap 560 дней отдельно записано как semantic clarification
`B6-ERRATUM-002`; модельная eligibility и протокол не менялись.

## Воспроизводимость

Машинные артефакты B5 содержат:

- benchmark plan, outer/inner assignments и fold contracts;
- feature views и metric suite;
- B4 error atlas, residual dependence и independent-unit counts;
- learning-curve predictions/metrics;
- ETS/ARIMA/VAR method cards;
- SHA-256 snapshot B0–B4 и suite v3;
- полный artifact inventory и independent validation report.

Plan SHA-256:
`d143f1c057f176f49c78d9433e4305557339854d1c1faaad77bc356aca8fd926`.
Feature-contract SHA-256:
`241bad48e67659ffc953349cd2bce59e61f2a2867d57698abd6574edd4a05272`.

Executed notebook `notebooks/06_gate_b5_evidence_audit.ipynb` читает только
сохранённые machine artifacts и ничего не переобучает. Его code cells
дополнительно проверяются на отсутствие model fit и validation/test loaders.

## Научная граница и следующий Gate

B5 подтверждает, что B6 можно запускать по заранее заданному и проверяемому
protocol. Он не создаёт новый holdout и не восстанавливает независимость уже
раскрытого test. Любой B6 outcome допустимо называть только
`train_only_internal_research`.

Следующий шаг — выполнить broad temporal screen всех preregistered моделей,
после чего допустить прошедшие модели и frozen comparators к полному
spatial/transition audit, interval calibration и learning curves. Suite v4
замораживается до появления любых новых labels; если hard gates не пройдены,
B7 остаётся primary со статусом `PASS_NO_NEW_PRIMARY`.

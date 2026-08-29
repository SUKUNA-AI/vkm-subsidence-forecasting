# Формальная постановка целевых переменных SKRU-1 v3.2

## Единица прогнозирования

Рабочий репер `point_id` после контроля качества и уравнивания текущей кампании. Оседание положительно вниз.

## T1 — скорость до следующей плановой targeted-кампании

`target_campaign` — первая будущая кампания, где данный пункт заранее имеет `targeted=True`.

`y_rate_obs = 365.25 * (eta_obs_target - eta_obs_current) / horizon_days`.

Если target measurement отсутствует, label цензурируется. Перескакивать к следующему успешному измерению запрещено.

## Производные T1B/T1C

`pred_increment_mm = pred_rate_mm_y * horizon_days / 365.25`.

`pred_next_settlement_mm = current_observed_settlement_mm + pred_increment_mm`.

## Неопределённость и веса

`sigma_increment = sqrt(sigma_current^2 + sigma_target^2)` до появления ковариационной матрицы.

`sigma_rate = 365.25 * sigma_increment / horizon_days`.

`w = clip(median(sigma_rate^2)/sigma_rate_i^2, 0.25, 4.0)` с последующей нормировкой до среднего 1.

## T2 — next available observation

Auxiliary target для совместимости. Может пропустить сорванную плановую кампанию и потому не является основной операционной задачей.

## T3 — fixed horizon 180d

Synthetic evaluation only: `365.25*(eta_true(t+180)-eta_true(t))/180`.

## T4 — activity in 180d

Positive при `max delta_v >= 25 mm/y`, `max acceleration >= 15 mm/y^2` и сохранении `v >= v0+20 mm/y` минимум два последовательных месяца.

## T5 — onset in 180d

Positive, если T4=1 и новое событие accelerating/reactivated/step_transition начинается после origin. Неполное окно — right-censored.

## T6 — profile outputs

На следующем полном цикле с coverage >=0.8 рассчитываются max settlement, max rate, max absolute tilt, curvature и horizontal strain. Предпочтительная реализация — агрегация point-level прогнозов.

## Split

Regression: по target date. Early warning: по label horizon end. Headline evaluation: temporal test и leave-profile/leave-zone-out. Random row split запрещён.

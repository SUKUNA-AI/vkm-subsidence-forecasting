# Gate B2: adaptive B6 Kalman, интервальная калибровка и transition-specific validation

## Техническое резюме

Gate B2 реализован и воспроизведён без доступа к раскрытому `t1_v1/test`. Адаптивный `B6_adaptive_kalman` настроен вложенным rolling-origin только на train-частях, интервалы откалиброваны по 292 nested out-of-fold train-остаткам, а transition-specific validation построена только на признаках, известных в момент origin.

Машинная проверка завершилась статусом **PASS: 169 checks, 0 failures**. При этом сама модель не прошла полный screening и поэтому зафиксирована как `validation_recorded`, а не как финальный кандидат:

- temporal MAE B6: **7.454 мм/год** против **7.311 мм/год** у B1 — ухудшение на 1.95%, формально в пределах лимита 2%;
- transition MAE B6: **13.189 мм/год** против **11.545 мм/год** у B1 — ухудшение на 14.25% вместо требуемого улучшения не менее чем на 10%;
- leave-zone MAE B6: **8.405 мм/год**, что на 12.77% хуже собственного temporal MAE при лимите 5%;
- 95% conformal coverage: **0.938**, то есть требуемый диапазон 0.90–0.97 выполнен, но средняя ширина интервала составляет 47.09 мм/год.

Главный вывод: адаптивный Kalman существенно лучше fixed B5 и полезен как обязательный comparator, но не заменяет persistence B1. Он улучшает stable и decelerating origins, однако запаздывает на accelerating и volatile-or-gap переходах. Следующий исследовательский шаг должен менять динамическую структуру модели, а не подбирать текущую B6 по validation. Финальная оценка остаётся заблокированной до появления нового временного/внешнего holdout или отдельного governance-решения.

Машинные источники результата: `artifacts/model_selection/t1_b2_v1/gate_b2_report.json`, `validation_report.json`, `aggregate_metrics.csv`, `transition_metrics.csv` и `interval_metrics.csv`.

## B6 улучшил fixed Kalman, но не превзошёл persistence

В temporal holdout B6 снизил MAE fixed B5 на 7.25%, а в rolling-origin — на 9.59%. Однако persistence B1 остался лучшим или практически равным по всем основным design.

| Validation design | B1 MAE | B5 MAE | B6 MAE | B6 vs B1 | B6 vs B5 |
|---|---:|---:|---:|---:|---:|
| temporal holdout | 7.311 | 8.036 | 7.454 | −1.95% | +7.25% |
| rolling-origin | 6.824 | 7.696 | 6.959 | −1.98% | +9.59% |
| leave-profile-out | 7.311 | 8.036 | 7.311 | −0.01% | +9.02% |
| leave-zone-out | 7.311 | 8.036 | 8.405 | −14.97% | −4.59% |

Знак «+» означает улучшение MAE, знак «−» — ухудшение. Leave-profile-out почти совпадает с B1, но leave-zone-out показывает явную нестабильность: B6 не только проигрывает B1, но и оказывается хуже fixed B5. Поэтому пространственный критерий screening не выполнен.

Train-only tuning для главного temporal fold выбрал `q_base=10` и `acceleration_gain=0`. Нулевой gain важен содержательно: rolling evidence внутри train не поддержал прямую экстраполяцию недавнего ускорения в средний будущий темп. Из 29 независимых tuning-контекстов комбинация `q=10, gain=0` была выбрана в 18, `q=25, gain=0` — в 7, `q=400, gain=0` — в 2; ненулевой gain выбран только один раз. Это указывает не на отсутствие переходов, а на недостаточную устойчивость простой constant-acceleration экстраполяции.

## Ошибка B6 сосредоточена в accelerating и volatile-or-gap origins

Transition-proxy был определён до оценки метрик и использует только доступные на origin признаки. Пороги для каждого outer fold оцениваются на его train-части:

- `accelerating`: `recent_acceleration_mm_y2` не ниже train-fold q80 абсолютного ускорения;
- `decelerating`: ускорение не выше отрицательного train-fold q80;
- `volatile_or_gap`: после исключения первых двух категорий `std_last_3_rates_mm_y` не ниже train-fold q80 или пропущено не менее двух кампаний;
- `stable`: остальные origins.

На temporal validation получено 88 stable и 42 transition origins. Transition-набор охватывает 32 точки и 10 профилей, но эти 42 строки всё равно не следует трактовать как 42 независимые траектории.

| Сегмент | Origins | Points | Profiles | B1 MAE | B5 MAE | B6 MAE | B6 vs B1 |
|---|---:|---:|---:|---:|---:|---:|---:|
| stable | 88 | 75 | 14 | 5.290 | 4.746 | 4.716 | +10.85% |
| accelerating | 17 | 14 | 5 | 15.900 | 23.178 | 19.406 | −22.05% |
| decelerating | 11 | 11 | 4 | 4.766 | 4.035 | 4.014 | +15.77% |
| volatile-or-gap | 14 | 14 | 9 | 11.583 | 13.472 | 12.849 | −10.94% |
| все transition | 42 | 32 | 10 | 11.545 | 14.929 | 13.189 | −14.25% |

B6 успешно сглаживает stable origins и лучше B1 на decelerating origins. Но на accelerating переходах фильтр всё ещё запаздывает: даже с adaptive process noise текущая constant-velocity структура не даёт устойчивого упреждения. Нулевой acceleration gain, выбранный train-only tuning, снижает риск нестабильной экстраполяции, но одновременно оставляет эту структурную слабость нерешённой.

Это отрицательный, но практически полезный результат. Он отвергает гипотезу, что одной адаптации `q` достаточно для требуемого 10% transition-улучшения. Подбирать веса адаптации по 42 validation transition rows после этого результата запрещено протоколом: это превратило бы validation в скрытый test для следующей версии B6.

## Интервалы имеют корректный coverage, но остаются широкими

Raw predictive sigma B6 включает апостериорную неопределённость скорости, process uncertainty на прогнозном горизонте и measurement-derived компоненту. Поскольку rate anchor и режим адаптации не образуют полностью специфицированную вероятностную модель, raw sigma не интерпретируется как готовый доверительный интервал.

Для калибровки использованы 292 уникальных nested rolling OOF origins из `t1_v1/train`, относящиеся к 98 точкам и 14 профилям, с `target_date` от 2022-10-18 до 2023-11-07. В каждом calibration fold параметры B6 снова выбирались только на более ранней части train. Nonconformity score определён как:

`abs(y_true - y_pred) / max(raw_sigma, 1 мм/год)`.

Квантили рассчитаны finite-sample методом `higher` с рангом `ceil((n + 1) × coverage)`. Validation не участвовал ни в выборе параметров, ни в оценке `qhat`.

| Номинальный coverage | Эмпирический coverage | Средняя ширина, мм/год | Median width, мм/год | qhat |
|---:|---:|---:|---:|---:|
| 0.80 | 0.831 | 20.09 | 18.12 | 2.006 |
| 0.90 | 0.908 | 30.28 | 27.32 | 3.023 |
| 0.95 | 0.938 | 47.09 | 42.48 | 4.701 |

95% coverage проходит критерий, но средний интервал шириной 47.09 мм/год велик относительно temporal MAE 7.45 мм/год. Следовательно, интервалы достаточно честны для текущего validation, но не достаточно узки для точного инженерного решения без дополнительной градации риска. Coverage также оценен только на 130 origins; его неопределённость и зависимость повторных точек должны учитываться при интерпретации.

## Данные, определения и область применимости

Gate B2 использует:

| Split | Origins | Target date max | Роль |
|---|---:|---|---|
| `t1_v1/train` | 911 | 2023-11-07 | fit, nested tuning и OOF calibration |
| `t1_v1/validation` | 130 | 2024-09-03 | outer development evaluation |
| `t1_v1/test` | не загружался | ранее раскрыт | только историческая диагностика B0/B1 |

Target — `observed_rate_mm_y`, единица измерения — мм/год. Валидационные 130 origins относятся к 90 точкам и 14 профилям. Поэтому строковые MAE и coverage являются описанием набора origins, а не оценками по 130 независимым объектам.

Leave-zone-out использует четыре зафиксированные геометрические proxy-zone, поскольку авторитетного operational `zone_id` в каноническом пакете нет. Полученная LZO-деградация важна как стресс-тест пространственного переноса, но не равна проверке переноса между официальными инженерными зонами.

## Спецификация adaptive B6

Состояние фильтра — settlement и velocity. Между наблюдениями используется constant-velocity переход, а process covariance масштабируется текущим `q_t`:

`q_t = q_base × clip(1 + 0.75 × acceleration_ratio + 0.50 × volatility_ratio + 0.25 × gap_excess, 1, 10)`.

`acceleration_ratio` и `volatility_ratio` нормируются на q80 соответствующей train-части и ограничиваются сверху четырьмя. `gap_excess=max(missing_campaigns−1, 0)`. Все scale-параметры fit заново внутри train scope.

Settlement остаётся основным измерением. Allowlisted `last_rate_mm_y` ассимилируется как rate anchor; его дисперсия выводится из текущей и предыдущей measurement uncertainty, длительности интервала и заранее фиксированного multiplier 4. Идентификаторы используются только для поиска причинной истории точки и не поступают как estimator features.

Grid:

- `q_base ∈ {0.5, 2, 10, 25, 100, 400}`;
- `acceleration_gain ∈ {0, 0.25, 0.5}`;
- headline tuning metric — pooled inner-fold MAE;
- tie-break — MAE, затем меньший acceleration gain, затем меньший `q_base`.

Для каждого outer fold применён собственный three-fold expanding-window inner tuning. Для интервальной калибровки поверх пяти OOF train folds применён ещё один уровень inner tuning. Таким образом, метка прогнозируемой строки не влияет ни на fit параметров модели, ни на scale-параметры, ни на выбор hyperparameters.

## Robustness и машинная проверка

Независимый validator подтвердил:

- 24 outer folds с точными количествами 1/5/14/4;
- строгий порядок `max(train.target_date) < min(validation.target_date)` для outer и всех inner folds;
- ровно одну выбранную комбинацию параметров в каждом из 29 tuning-контекстов;
- calibration IDs являются подмножеством train и не пересекаются с validation;
- все aggregate MAE/RMSE/bias/R² независимо пересчитаны из prediction rows;
- все interval coverage, widths, interval scores и conformal `qhat` независимо пересчитаны;
- все transition MAE и числа строк независимо пересчитаны;
- в исходниках Gate B2 отсутствует вызов model-facing test loader;
- пять раскрытых B0/B1 test-артефактов не изменились ни на байт;
- final holdout имеет статус `PENDING_DATA`, текущий test помечен как ineligible.

Итоговая аналитическая оценка: **Share with caveats**. Она относится к воспроизводимости и корректности development evidence, а не к прохождению модельного screening.

## Ограничения и нерешённые вопросы

1. Transition-proxy — операционное определение на origin-признаках, а не независимая экспертная разметка режима и не причинное утверждение.
2. Малая численность accelerating/decelerating сегментов повышает дисперсию segment-level MAE.
3. Adaptive Kalman использует приближённую covariance model; conformal calibration исправляет marginal coverage, но не доказывает корректность условных распределений для каждой точки или зоны.
4. LZO proxy-zones не заменяют внешний геологический перенос.
5. Текущий test уже раскрыт. Его повторное использование для выбора B6, изменения transition thresholds или перекалибровки интервалов было бы leakage на уровне исследования.

## Рекомендованный следующий шаг

B6 следует сохранить как обязательный comparator, но не продолжать его ручную настройку по текущему validation. Следующий предобъявленный эксперимент должен адресовать структурную ошибку accelerating-переходов. Практически разумные кандидаты:

1. IMM или switching state-space с заранее ограниченным числом режимов;
2. robust innovation-adaptive Kalman с train-only change-score и без private regime labels;
3. отдельный transition gate, обученный только на origin features, с nested threshold selection;
4. sensitivity-анализ proxy-zone instability и profile-balanced objective.

До запуска следующего семейства нужно заранее зафиксировать его grid, decision rule и критерий остановки. Для финальной модели требуется новый future holdout с `target_date > 2025-11-04` и не раньше 2026-01-01 либо независимый внешний пакет. Минимальная governance-цель: 100 observed origins, 75 точек, 12 профилей и две target campaign dates. Подробный протокол записан в `configs/final_holdout_v2.yaml` и `docs/governance/FINAL_EVALUATION_POLICY_V2.md`.

## Воспроизведение

```powershell
.\.venv\Scripts\python.exe scripts\run_gate_b2.py --phase all
.\.venv\Scripts\python.exe scripts\build_gate_b2_notebook.py
.\.venv\Scripts\python.exe -m pytest
```

У runner нет `final-test` phase. Reader-facing companion: `notebooks/03_gate_b2_adaptive_kalman.ipynb`. Полный SHA-256 inventory: `artifacts/model_selection/t1_b2_v1/artifact_inventory.csv`; сам inventory исключён из самореферентного списка.

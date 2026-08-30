# Gate B4: robust innovation IMM только внутри `t1_v1/train`

## Техническое резюме

Gate B4 проверил одну заранее специфицированную гипотезу: может ли
bounded-influence Student-t observation model исправить известную ошибку B7 на
`volatile_or_gap`, не ухудшая accelerating и пространственную устойчивость.
Никакая другая часть B7 не менялась. Сетка содержала только четыре значения
числа степеней свободы: `3`, `5`, `10`, `30`; фиксированный нижний предел
influence weight равен `0.05`.

Исследование выполнено исключительно на 911 origins замороженного
`t1_v1/train`. Исторический validation, раскрытый test и новый final holdout не
загружались. Внутренний audit-tail — 88 origins на target campaign
2023-11-07; core — предшествующие 823 origins. Результаты воспроизводимы, но
узкий критерий не пройден: выбранный `B8_student_t_robust_imm` (`ν=30`) улучшил
общую internal-temporal MAE на 3,06% против B7, однако ухудшил
`volatile_or_gap` MAE на 0,45% вместо требуемого улучшения не менее 10%.

Итоговый статус B8 — `train_only_research_recorded`. В замороженном до нового
holdout candidate suite primary остаётся `B7_two_regime_imm`; результаты
B1/B5/B6/B8 на будущем holdout разрешены только как контекст, а не как источник
post-holdout выбора модели.

## Ключевые результаты

| Проверка | B7 MAE, мм/год | B8 MAE, мм/год | Изменение B8 к B7 | Решение |
|---|---:|---:|---:|---|
| Internal temporal, 88 origins | 6,015 | 5,831 | лучше на 3,06% | проходит общий guardrail |
| `volatile_or_gap`, 8 origins | 12,760 | 12,818 | хуже на 0,45% | не проходит порог +10% |
| `accelerating`, 17 origins | 8,734 | 8,878 | хуже на 1,66% | проходит лимит +2% |
| Leave-profile-out, 88 origins | 6,007 | 5,821 | лучше на 3,09% | проходит |
| Leave-zone-out, 88 origins | 6,046 | 5,858 | лучше на 3,12% | проходит |
| Rolling-origin, 292 pooled origins | 6,227 | 6,509 | хуже на 4,52% | существенная оговорка |

Полная визуальная сверка находится в исполняемом notebook
`notebooks/05_gate_b4_robust_innovation.ipynb`: он показывает MAE по четырём
governed designs, transition segments, full-train tuning curves и ошибки по
четырём proxy-зонам.

## Scope, данные и определения

- Источник выбора и оценки: только `artifacts/splits/t1_v1/train.csv`, 911
  origins, 98 points, 14 profiles, target dates от 2019-02-12 до 2023-11-07.
- `core.csv`: 823 origins строго раньше 2023-11-07.
- `audit_tail.csv`: 88 origins, 88 points, 14 profiles и 4 spatial proxy zones
  на 2023-11-07.
- Outer resampling: 1 internal temporal, 5 expanding rolling-origin, 14
  forward leave-profile-out и 4 forward leave-zone-out folds.
- Inner selection: три expanding rolling folds внутри каждого outer-train;
  thresholds transition proxy fit только на соответствующем train.
- `volatile_or_gap` определяется только origin-known признаками:
  `std_last_3_rates_mm_y` и `missing_campaigns_since_previous`, после исключения
  accelerating/decelerating по train-fitted acceleration threshold.

Canonical validation не является частью этого scope: он уже использовался для
исторической разработки Gate B2/B3 и поэтому не может продолжать направлять
выбор. Ранее раскрытый `t1_v1/test` имеет только историко-диагностическую роль.

## Методология и спецификация модели

Оба режима используют состояние `[settlement, velocity, acceleration]`.
Process noise, acceleration retention, Markov transition probabilities,
initial covariance и причинное отсечение истории полностью совпадают с
замороженным B7: `q_stable=0.5`, `q_transition=200`,
`p_stable_stay=0.99`, `p_transition_stay=0.75`.

Для каждой scalar observation innovation с базовой дисперсией `S` вычисляется
`z²=e²/S`. Influence weight равен `min(1,(ν+1)/(ν+z²))`, но ограничен снизу
значением 0,05. Measurement variance для Kalman update увеличивается как
`R/weight`, а regime evidence рассчитывается по Student-t log likelihood.
Одинаковый `ν` применяется к settlement, rate и acceleration channels; это
сознательное ограничение модели, которое удерживает эксперимент однофакторным.

Inner objective — 50% B8/B7 normalized overall MAE и 50% B8/B7 normalized
`volatile_or_gap` MAE. На full-train nested resampling результаты были:

| `ν` | Overall / B7 | Volatile-gap / B7 | Objective |
|---:|---:|---:|---:|
| 3 | 1,0422 | 1,2766 | 1,1594 |
| 5 | 1,0472 | 1,3082 | 1,1777 |
| 10 | 1,0263 | 1,1466 | 1,0865 |
| 30 | 1,0201 | 1,0072 | 1,0136 |

`ν=30` выбран как минимум заранее заданного objective, но даже лучший вариант
не превосходит B7 по обеим составляющим. Большое `ν` также означает, что данные
предпочитают likelihood, близкий к Gaussian, а не сильную heavy-tail
робастизацию.

## Механизм и устойчивость

На internal temporal tail B8 downweighted 897 из 4488 regime-channel updates
(19,99%); минимальный weight достиг фиксированного floor 0,05. Следовательно,
механизм действительно активен и большие innovations существуют. Но activation
не равна predictive benefit: на целевых восьми `volatile_or_gap` origins MAE не
улучшилась, а на пяти rolling folds pooled MAE стала хуже B7 на 4,52%.

Spatial evidence выглядит лучше: leave-profile и leave-zone MAE ниже B7, а
degradation относительно собственного internal-temporal результата B8 равен
-0,17% и +0,46% соответственно, существенно внутри лимита 5%. Это полезная
диагностика observation robustness, но она не отменяет провал основного
transition-specific критерия.

## Валидация качества и проверяемость

Машинный audit повторно построил split contracts, доказал принадлежность всех
prediction IDs исходному train manifest, пересчитал fold/aggregate/transition
metrics и influence diagnostics, подтвердил четыре кандидата на каждый tuning
context и deterministic minimum-score selection. Результат: **54 проверки,
0 ошибок**, assessment `Share with caveats`.

Хэши старых validation/test manifests, раскрытого test ledger и outputs,
кандидатов B1/B6/B7, конфигураций и model source были проверены до и после
Gate B4. AST guard не нашёл вызовов model-facing loader для validation или
test. Все четыре train-research manifests и полный inventory сохранены.

Уверенность в воспроизводимости вычислений — высокая. Уверенность в выводе о
внешней валидности — низкая до получения нового holdout. Уверенность в
segment-specific оценке ограничена восемью `volatile_or_gap` rows внутреннего
tail и зависимостью repeated trajectories.

## Новый final holdout v3

Локальный scan не нашёл пригодного future/external пакета. Старые synthetic
smoke fixtures и model predictions не являются истинными новыми labels.
Машинный статус — `PENDING_DATA`, sealed target values не читались.

Подготовлен terminal one-shot protocol:

1. `status` проверяет только наличие пакета, origin schema, scope, overlap и
   хэши; sealed target file только хэшируется;
2. `freeze` фиксирует ordered sample manifest, candidate suite, primary B7,
   source hashes и commit SHA до чтения labels;
3. `evaluate-once` сначала переводит ledger в consumed state, затем читает
   labels и один раз оценивает primary B7 вместе с контекстными B1/B5/B6/B8;
4. любая ошибка после начала доступа тоже потребляет попытку; post-access tuning
   и смена primary запрещены.

Без реального пакета запускать `freeze` и тем более `evaluate-once` нельзя.

## Следующие действия

1. Получить от владельца данных новый future-пакет не раньше 2026-01-01 либо
   независимый external package, удовлетворяющий минимумам 100 origins, 75
   points, 12 profiles и 2 target campaign dates.
2. Выполнить `scripts/run_holdout_v3.py --phase status`; устранить только
   schema/provenance defects без открытия labels.
3. После review закоммитить frozen intake record и ordered manifest.
4. Выполнить `evaluate-once` ровно один раз. Интерпретировать B7 как заранее
   объявленный primary, а остальные модели — только как контекст.
5. Не продолжать подбирать observation model по текущему validation или по
   будущему holdout. Следующая модельная гипотеза, если понадобится, должна
   получить новую preregistration и новый независимый evaluation resource.

## Открытые вопросы

- Являются ли gaps источником outliers в settlement, derived rate или
  acceleration channel по отдельности? Текущий однофакторный B8 намеренно не
  различает каналы.
- Сохранится ли локальное spatial улучшение на независимой площадке?
- Достаточны ли governance-минимумы holdout для узких transition segments, или
  до открытия требуется заранее зафиксировать больший segment-specific минимум?

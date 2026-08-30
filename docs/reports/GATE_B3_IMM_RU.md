# Gate B3: двухрежимный IMM для accelerating/volatile переходов

## Техническое резюме

Gate B3 реализован как заранее специфицированная проверка одной гипотезы:
двухрежимный `Interacting Multiple Model` с состоянием
`[settlement, velocity, acceleration]` должен исправить ошибки B6 на
`accelerating` и `volatile_or_gap` origins и снизить leave-zone instability.
Сетка, train-only objective и критерии успеха были записаны в
`configs/gate_b3.yaml` и `docs/governance/GATE_B3_PROTOCOL.md` до первого
полного outer-run.

Результат смешанный и потому зафиксирован как **`validation_recorded`**, а не
как прошедший screening кандидат:

- temporal MAE B7 равен **6.545 мм/год** — лучше B1 на **10.48%**, B5 на
  **18.55%** и B6 на **12.19%**;
- rolling-origin MAE равен **6.331 мм/год** — лучше B1 на **7.21%** и B6 на
  **9.01%**;
- accelerating MAE снизился до **12.878 мм/год**, то есть B7 лучше B1 на
  **19.00%** и B6 на **33.64%**;
- на заранее заданном объединении `accelerating + volatile_or_gap` улучшение
  относительно B6 составляет **17.99%**, но относительно B1 — только
  **3.32%** вместо требуемых 10%;
- `volatile_or_gap` MAE вырос до **14.226 мм/год**, что на **22.83% хуже B1**;
- leave-zone MAE улучшился до **7.064 мм/год** и стал лучше B1 на **3.37%** и
  B6 на **15.95%**, однако относительно собственного temporal MAE B7 остаётся
  degradation **7.93%** при лимите 5%;
- 95% conformal coverage равен **0.962** при средней ширине
  **47.62 мм/год**, поэтому интервальный критерий выполнен.

Иными словами, IMM действительно устранил главную accelerating-ошибку B6 и
сделал абсолютный spatial result заметно устойчивее. Но строгая гипотеза не
подтверждена полностью: volatile/gap origins остаются проблемой, а
leave-zone gap к собственному сильному temporal результату всё ещё выше
порогового значения. Подкручивать этот IMM по текущему validation нельзя.

Машинный аудит: **PASS, 64 проверки, 0 failures**, аналитическая оценка
`Share with caveats`. Первичный валидатор обнаружил один ложный mismatch:
пустой `held_out_group` после CSV round-trip читается Pandas как `NA`.
Отдельный audit-adapter нормализовал только это представление и подтвердил,
что ни predictions, ни параметры, ни comparators не изменились. Evidence:
`artifacts/model_selection/t1_b3_v1/audit_reconciliation.json`.

## Ключевой результат: B7 сильнее всех comparators по общей MAE

B1, B5 и B6 не переобучались. Их строки прочитаны из hash-защищённого
`artifacts/model_selection/t1_b2_v1/outer_fold_predictions.csv` с SHA-256
`e1b933e35aa126719fac31ca0d9b468e4feb70c296beaa8faa1b25cf3120a79f`.
Использованы те же 24 forward-only folds и те же outer transition assignments.

| Validation design | B1 MAE | B5 MAE | B6 MAE | B7 MAE | B7 vs B1 | B7 vs B6 |
|---|---:|---:|---:|---:|---:|---:|
| temporal holdout | 7.311 | 8.036 | 7.454 | **6.545** | +10.48% | +12.19% |
| rolling-origin | 6.824 | 7.696 | 6.959 | **6.331** | +7.21% | +9.01% |
| leave-profile-out | 7.311 | 8.036 | 7.311 | **6.573** | +10.09% | +10.10% |
| leave-zone-out | 7.311 | 8.036 | 8.405 | **7.064** | +3.37% | +15.95% |

Знак «+» означает снижение MAE. B7 — единственная из четырёх моделей, которая
лучше B1 во всех design. Это сильный development-сигнал, но не финальная
оценка: текущий T1 test уже раскрыт, а новый holdout отсутствует.

Leave-profile degradation относительно temporal B7 составляет только 0.43%
и проходит лимит 5%. Leave-zone degradation равен 7.93% и не проходит его,
хотя абсолютная leave-zone MAE стала лучше обоих главных comparators. Строгий
критерий намеренно не заменяется более удобным после наблюдения результата.

## Transition-specific validation: accelerating исправлен, volatile — нет

Outer transition labels перенесены из frozen Gate B2 prediction rows. Поэтому
B1/B5/B6/B7 оцениваются на идентичных origins и при идентичных train-fitted
thresholds.

| Temporal segment | Origins | Points | Profiles | B1 MAE | B6 MAE | B7 MAE | B7 vs B1 |
|---|---:|---:|---:|---:|---:|---:|---:|
| stable | 88 | 75 | 14 | 5.290 | 4.716 | **4.453** | +15.83% |
| accelerating | 17 | 14 | 5 | 15.900 | 19.406 | **12.878** | +19.00% |
| decelerating | 11 | 11 | 4 | 4.766 | 4.014 | **3.720** | +21.95% |
| volatile-or-gap | 14 | 14 | 9 | **11.583** | 12.849 | 14.226 | −22.83% |
| все transition | 42 | 32 | 10 | 11.545 | 13.189 | **10.929** | +5.33% |
| accelerating + volatile-or-gap | 31 | 23 | 9 | 13.950 | 16.445 | **13.487** | +3.32% |

На accelerating origins структурная замена constant-velocity фильтра на
damped-acceleration IMM сработала именно в ожидаемом направлении. Отрицательный
bias переходного набора уменьшился, а MAE стала существенно ниже и B1, и B6.
Stable и decelerating origins также не были принесены в жертву.

Однако режим `transition` оказался слишком широким статистическим механизмом
для `volatile_or_gap`. Эти origins могут соответствовать разным причинам:
реальному изменению динамики, редким кампаниям, шумному rate estimate или
одиночному innovation. Высокий process noise помогает accelerating trajectories,
но для части volatile/gap origins расширяет движение состояния в неверном
направлении. Поэтому объединённый заранее заданный критерий не достиг 10%
улучшения против B1.

## Поведение режимов действительно различается, но разделение неполное

Temporal train-only tuning выбрал:

- `q_stable = 0.5`;
- `q_transition = 200`;
- `p(stable→stable) = 0.99`;
- `p(transition→transition) = 0.75`.

Та же комбинация выбрана в 23 из 29 независимых tuning contexts. Высокий
`q_transition=200` выбран в 28 из 29 contexts, а меньшая persistence переходного
режима `0.75` — во всех 29. Это не post-hoc выбор: каждый context использовал
только собственные inner train/validation dates и заранее заданную сетку из
16 вариантов.

| Temporal proxy segment | Mean P(transition) | Median | P90 | Доля P≥0.5 |
|---|---:|---:|---:|---:|
| stable | 0.107 | 0.035 | 0.326 | 0.045 |
| accelerating | 0.425 | 0.412 | 1.000 | 0.471 |
| decelerating | 0.170 | 0.079 | 0.189 | 0.091 |
| volatile-or-gap | 0.412 | 0.284 | 0.944 | 0.429 |

IMM innovations действительно повышают transition probability для accelerating
и volatile/gap origins относительно stable. Но вероятность сама по себе не
различает полезный accelerating maneuver и неоднородный noisy/gap случай. Это
объясняет, почему механизм хорошо решает первую часть гипотезы и ухудшает вторую.
Режимы нельзя интерпретировать как доказанные физические состояния массива:
это статистические фильтры.

## Leave-zone evidence: абсолютный перенос улучшен, residual gap локализован

| Held-out proxy zone | B1 MAE | B6 MAE | B7 MAE | Лучший |
|---|---:|---:|---:|---|
| GEO_NE | 6.236 | 5.644 | **5.335** | B7 |
| GEO_NW | 3.845 | 3.548 | **2.995** | B7 |
| GEO_SE | **5.169** | 5.955 | 6.128 | B1 |
| GEO_SW | 8.570 | 10.667 | **8.487** | B7 |

B7 лучше B6 во всех четырёх aggregate contributions кроме того, что в GEO_SE
он немного хуже B6 и заметно хуже B1. На наиболее крупном и трудном GEO_SW B7
почти сравнялся с B1 и значительно исправил B6. Следовательно, прежняя грубая
leave-zone instability B6 в абсолютных величинах в основном устранена, но
GEO_SE остаётся переносным failure mode, а собственный temporal B7 стал
настолько сильнее, что относительный 5%-критерий всё ещё не выполнен.

Эти четыре зоны являются заранее построенными геометрическими proxy-zones, а
не авторитетными operational geology labels. Их нельзя подавать в estimator как
признаки; они используются только для stress-test.

## Интервалы и неопределённость

Scaled conformal calibration использовала 292 уникальных nested rolling OOF
origins исключительно из `t1_v1/train`. Внутри каждого calibration fold IMM
снова настраивался только на более ранних train dates. Validation labels не
участвовали в выборе параметров или `qhat`.

| Nominal coverage | Empirical coverage | Mean width, мм/год | Median width, мм/год | qhat |
|---:|---:|---:|---:|---:|
| 0.80 | 0.885 | 20.25 | 18.19 | 2.165 |
| 0.90 | 0.923 | 30.21 | 27.14 | 3.230 |
| 0.95 | 0.962 | 47.62 | 42.79 | 5.092 |

95% coverage попадает в заранее заданный диапазон 0.90–0.97. Однако средняя
ширина почти 48 мм/год остаётся большой относительно MAE 6.55 мм/год. Интервалы
полезны как честная marginal uncertainty band, но пока слишком широки для
точной инженерной градации на отдельных volatile origins. Coverage посчитан по
130 повторяющим точки origins, а не по 130 независимым траекториям.

## Данные, область применения и causal boundary

| Split | Origins | Роль в Gate B3 |
|---|---:|---|
| `t1_v1/train` | 911 | fit, nested tuning, OOF calibration |
| `t1_v1/validation` | 130 | outer development evidence |
| `t1_v1/test` | не загружался | ранее раскрыт; historical diagnostic only |

Validation designs:

- 1 temporal holdout;
- 5 rolling-origin folds;
- 14 forward leave-profile-out folds;
- 4 forward leave-zone-out folds.

Во всех folds выполняется
`max(train.target_date) < min(validation.target_date)`. История каждой точки
обрезается по `current_date` текущего origin. Идентификаторы используются только
для поиска причинной истории и группового stress-test, но не входят в estimator
state или features. Test loader в исходниках Gate B3 отсутствует.

## Спецификация модели

Каждый режим хранит состояние:

`x = [settlement_mm, velocity_mm_y, acceleration_mm_y2]`.

Режим `stable` использует сильное годовое затухание acceleration (`0.20`) и
малый jerk noise. Режим `transition` использует retention `0.95` и высокий jerk
noise. Standard IMM на каждом причинном measurement step:

1. смешивает два posterior state/covariance по фиксированной Markov matrix;
2. выполняет regime-specific prediction;
3. обновляет состояние по settlement, uncertainty-derived last-rate и
   origin-known recent-acceleration;
4. обновляет regime probabilities по innovation likelihood;
5. формирует mixture mean и mixture variance с between-regime компонентой.

Все fallback, acceleration scale и measurement scales fit только на train scope.
Acceleration ограничивается train-fitted q80 scale исключительно для numerical
robustness. Никакие `true_*`, event dates, private regimes, zone IDs или profile
IDs не подаются в динамическое состояние.

Inner objective был зафиксирован как:

`0.5 × (IMM overall MAE / B1 overall MAE) + 0.5 × (IMM problem-transition MAE / B1 problem-transition MAE)`.

Problem-transition — только `accelerating + volatile_or_gap`, thresholds каждого
inner fold fit только на его train. Такой objective адресует заранее известную
структурную проблему B6, но не использует outer validation result B7.

## Screening и robustness

| Критерий | Результат | Статус |
|---|---:|---|
| temporal MAE / B6 ≤ 1.02 | 0.878 | PASS |
| problem-transition improvement vs B1 ≥ 10% | 3.32% | **FAIL** |
| problem-transition improvement vs B6 ≥ 10% | 17.99% | PASS |
| accelerating MAE ≤ B1 | 12.878 ≤ 15.900 | PASS |
| volatile-or-gap MAE ≤ B1 | 14.226 ≤ 11.583 | **FAIL** |
| LPO degradation vs temporal ≤ 5% | 0.43% | PASS |
| LZO degradation vs temporal ≤ 5% | 7.93% | **FAIL** |
| LZO MAE ≤ B1 | 7.064 ≤ 7.311 | PASS |
| coverage 95% ∈ [0.90, 0.97] | 0.962 | PASS |

Независимый validator подтвердил:

- точное совпадение frozen B1/B5/B6 prediction rows;
- точное совпадение B2/B3 fold contracts;
- полную 16-комбинационную сетку и один minimum-score candidate во всех 29
  tuning contexts;
- forward-only порядок outer, inner и calibration folds;
- train-only calibration IDs без пересечения с validation;
- независимый пересчёт fold, aggregate, transition, problem-transition,
  regime и interval metrics;
- корректность regime probabilities и отсутствие numerical fallbacks;
- неизменность 12 защищённых test/B2 artifacts;
- совпадение source hashes frozen candidate;
- отсутствие test-loading path и запрет финального claim.

Итоговая оценка **Share with caveats** относится к корректности и
воспроизводимости evidence. Она не означает прохождение модельного screening.

## Ограничения

1. Гипотеза Gate B3 мотивирована уже известными ошибками B6 на этом validation.
   Предрегистрация B7 защищает от настройки внутри gate, но не превращает период
   в новый unseen holdout.
2. `volatile_or_gap` объединяет неоднородные механизмы. Дальнейшее разделение по
   текущему validation создало бы post-hoc selection bias.
3. Только 17 accelerating и 14 volatile/gap origins ограничивают точность
   segment-level сравнений; строки повторяют точки и профили.
4. IMM state-space и covariance — приближённая модель. Conformal calibration
   обеспечивает marginal, а не point-conditional coverage.
5. Proxy-zones не заменяют внешний геологический перенос.
6. Текущий test ранее раскрыт и не может разрешить неоднозначность результатов.

## Решение и следующий научно корректный шаг

B7 следует сохранить вместе с B1, B5 и B6 как новый сильный comparator. Его
нельзя объявлять финальным кандидатом, потому что три заранее заданных проверки
не пройдены и нового holdout нет.

Следующий шаг не должен быть новой настройкой `q`, Markov probabilities или
volatile threshold на текущем validation. Корректны два варианта:

1. **Сначала получить новый future/external holdout.** Тогда B7 остаётся frozen
   candidate family, а его проверка проводится один раз по governance v2.
2. **До появления holdout выполнить только train-resampling research**, не
   используя текущий validation для выбора: например, заранее специфицировать
   innovation-robust observation model для gap/noise или отдельный missingness
   measurement channel и выбирать его исключительно nested rolling/profile/
   zone folds внутри текущего train. Внешняя проверка всё равно потребуется.

Наиболее содержательный нерешённый вопрос: являются ли 14 volatile/gap ошибок
настоящими change-points или преимущественно measurement/gap artifacts. Ответ
требует независимой разметки/новых кампаний, а не постфактум изменения proxy.

## Воспроизведение

```powershell
.\.venv\Scripts\python.exe scripts\run_gate_b3.py --phase develop
.\.venv\Scripts\python.exe scripts\run_gate_b3_audit.py
.\.venv\Scripts\python.exe scripts\build_gate_b3_notebook.py
.\.venv\Scripts\python.exe -m pytest
```

Reader-facing companion: `notebooks/04_gate_b3_imm.ipynb`. Машинный report:
`artifacts/model_selection/t1_b3_v1/gate_b3_report.json`; authoritative QA:
`artifacts/model_selection/t1_b3_v1/validation_report.json`; полный inventory:
`artifacts/model_selection/t1_b3_v1/artifact_inventory.csv`.

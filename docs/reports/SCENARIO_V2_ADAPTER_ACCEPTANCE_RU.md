# Scenario v2 adapter acceptance — BLOCKED_NOT_FROZEN

Дата: 2026-09-24. HEAD: `ffcf86972d31bd9f0dbd62d1265001f5c722e253`, branch `main`.
Representation acceptance **не пройдена**. Data release не изменён; commit/push,
benchmark, обучение и model scoring не выполнялись.

## Blocker V2-REP-001

Независимый проход по всем 321766 origins обнаружил 31283 строки (9.7222826526%),
где `days_since_previous_observation` в frozen model_features на один день меньше
фактической разности дат двух соседних наблюдений. Это содержательная ошибка
численного model input, а не форматирование или допустимая uncertainty.

| Outer role | Затронуто origins |
|---|---:|
| train | 4403 |
| calibration | 39 |
| evaluation | 24824 |
| excluded | 2017 |
| Всего | 31283 |

Пример `V2-W001-O1::P-H04-W001::C012::C013`: предыдущая observation 2021-01-26,
current 2021-05-18; реальные elapsed days = 112, frozen feature = 111.
Все расхождения равны −1 дню. На интервале 56 дней сохранено 55.

Причина в неизменённом `src/skru1/scenario_simulation_v2.py`:

- строка 303: calendar days делятся на YEAR_DAYS=365.25;
- строка 310: значение переводится обратно в дни;
- строка 416: `int(days_since_previous[current_index, point_index])` усекает float.

Воспроизводимая арифметика Python:

```python
112 / 365.25 * 365.25       # 111.99999999999999
int(112 / 365.25 * 365.25)  # 111
56 / 365.25 * 365.25        # 55.99999999999999
```

Frozen rate и acceleration вычисляются до этого int conversion: независимая
проверка signed rate и midpoint-spacing acceleration прошла. Forecast horizon
вычисляется непосредственно из дат (строки 359–361) и также прошёл проверку.
Никакие значения, distributions, seeds, truth или source constraints не исправлялись.

## Реализованный, но не принятый draft

Созданы отдельные v2 modules: adapter; Python worker file boundary; scorer-side
loader; origin worker inputs; train-only temporal/world и spatial split helpers;
lazy sequences; fold-only scaler/imputer/category encoder. Старые v1 modules и
их guards не менялись.

ModelDataBundle содержит target-free общий frame, train ManifestDataset с observed
target, target-free calibration/evaluation, history object, windows и identity hashes.
CalibrationTargetStore и EvaluatorTruthStore отделены. Estimator matrix имеет ровно
16 allowlisted features; IDs только metadata. Worker не принимает truth store.

Guard проверен против Path.open, os.open, загрузчиков evaluator/calibration и
открытия из другого Python thread. Он не является OS sandbox против native кода.
Будущий scorer должен работать отдельно, без передачи truth objects worker.

Три draft forward/world folds и 18 spatial folds собраны только в scratch
`work/scenario_representation_v2/build01/`. Они **не являются frozen representation
release**. Проверки embargo, world grouping, исключения held profiles/zones и
удаления indirect aggregate contamination прошли. Proxy zones обозначены как
synthetic coordinate quadrants. В leave-zone/profile view пересчитываются только
четыре aggregate features, без изменения исходных таблиц.

## Исполненная проверка

Команды из корня репозитория:

```powershell
.venv/Scripts/python.exe scripts/build_scenario_representation_v2.py --output work/scenario_representation_v2/build01
.venv/Scripts/python.exe -m pytest tests/test_scenario_representation_v2.py -q --basetemp work/scenario_representation_v2/pytest01 --junitxml work/scenario_representation_v2/pytest01.xml
.venv/Scripts/python.exe scripts/validate_scenario_representation_v2.py --output work/scenario_representation_v2/independent02.json
```

Для повтора нужен новый output path. Первая команда была исполнена до обнаружения
blocker; её scratch source manifest — промежуточный, не freeze authority.

- Новый pytest suite: **25 passed, 1 failed**, 154.92 s.
- Независимый validator: **67/68 checks PASS**; единственный FAIL `origin_gap`.
- Проверены 321766 origins, 395334 history rows и все 321766 frozen windows.
- Next-planned pairing совпал, включая 50082 origins с недоступной следующей целью.
- Missing targets остаются NaN. Rates, midpoint acceleration, history counts,
  missing-campaign counts, observed targets и outer roles проверены независимо.
- B1/B5/B6/B7/B8: только constructors, signature inspection и отдельный history
  conversion; ни fit, ни predict не вызваны.
- Проверка всех 1286 файлов initial inventory: изменённых нет.
- Full runtime sequence parity остановился на первой batch из 4096 из-за blocker;
  нельзя утверждать полную runtime acceptance всех origins.
- Two-run representation reproducibility, C01 construction smoke и полный regression
  rerun не завершались после обнаружения blocker. Старые receipts не выдаются за rerun.

Машинные evidence в `artifacts/data_quality/scenario_representation_v2/`:
`blocker_receipt.json`, все 31283 строки `elapsed_days_mismatches.csv.gz`,
`independent_validation.json`, `test_execution_receipt.json`,
`preexisting_integrity_receipt.json`, итоговый `acceptance_receipt.json`.

## Идентичность и дальнейшее решение

Dataset manifest SHA256:
`7bb67939314fbcf548686893e6868c90a2303e1303c401377f197e077fec8c13`.
Constraints manifest SHA256:
`96b88b61cf766062c1f831e32b50bdd82441da63c50ce41569a5ffad297de6ef`.
Ordered frozen window identity SHA256:
`48d49b3213dbf887467f47e581e744a4e9fad2a773598f938a66127fd25660db`.

Нельзя одновременно сохранить ошибочный frozen elapsed-days feature и объявить
его равным реальному sequence Δt. Необходима отдельная явная версия исправления:
либо утверждённый representation erratum с пересчётом этого производного признака
по датам (frozen dataset bytes сохраняются), либо новый data release. Автоматический
выбор и реализация любого варианта сейчас выходят за разрешённый scope.

До этого решения benchmark **не запускать**. Затем повторить полную adapter/sequence
acceptance, заморозить representation и только отдельным заданием запускать minimal
B1/B5/B6/B7 (+B8 robustness, C01 control) comparison без старых MAE и old scalers.

`models_executed=0`; `legacy_holdout_labels_parsed=0`; frozen v2 bytes/semantics unchanged.
Tracked diff пуст. Новые draft files и blocker evidence остаются untracked для review;
три исторических untracked v1-пути сохранены.

# Scenario v2.1 adapter acceptance

**PASS_REPRESENTATION_FROZEN**, 2026-09-24. Data correction prerequisite: PASS.
Authority: `SKRU1_SCENARIO_SIMULATION_V2_1`, `a268cd7800e1320a1cc61172f95cf458c7ac7434237b32b288f23a83a97a2c95`.
Representation: `SKRU1_SCENARIO_REPRESENTATION_V2_1_R1`,
manifest SHA256 `0e5501cb4c3cff570fbd3763047a8dd8738d45706dd6f549556ac428521f5a6d`.

Frozen original v2, BLOCKED reports, mismatch evidence и прежний draft сохранены.
Новый код получен versioned копированием draft с новой pinned identity; не создан
fallback на v2. Functional additions: exact gap invariant, полная runtime parity,
creation identity/category mappings в preprocessing state, persisted states и
construction-only C01 spec. Законы данных, channels, folds и hyperparameters моделей
не подбирались. Generator v2.1 после DATA PASS не менялся.

## Model/scorer boundary

`scenario_adapter_v2_1.py` читает model-facing release files и feature contract.
Общий frame, calibration/evaluation frames target-free. Train observed target
присоединён отдельно, точно по sample_id. CalibrationTargetStore — отдельный API;
его загрузка в model worker запрещена. EvaluatorTruthStore только в scorer loader.
Join проверен dummy prediction rows, включая перестановку ID, duplicates и unknown
IDs; metrics не вычислялись. Ни один estimator не получил evaluator object.

Ровно 16 predictors; IDs только lookup/group metadata. Source donor, seeds, latent
truth, mechanisms, proxy zones, coordinates и target availability в matrix отсутствуют.
Проверены Path.open, os.open, новые Python threads, запрещённый child-process launch,
отказ scorer/calibration loaders внутри worker и отказ worker принять truth store.
Это Python application boundary, не OS sandbox и не защита от произвольного native
кода или заранее переданного внешнего truth object.

## Full origin acceptance

Независимый validator проверил **321766 origins**, **395334 history rows**,
**321766 frozen windows**. `origin_gap`: **0 mismatch**, exact integer equality,
без tolerance/xfail/специальной обработки старой ошибки. Current observation,
previous observed date/gap, horizon, history count, signed rate, midpoint acceleration,
missing-campaign count, target values и role проверены. Rate/acceleration tolerance
относится только к округлению frozen CSV decimals; elapsed days сравниваются точно.

Next target — следующая planned targeted campaign, включая **50082 origins** с
недоступным следующим наблюдением. Переход к более поздней успешной кампании запрещён.
Outer counts сохранены: train 122047, calibration 35148, evaluation 133833,
excluded 30738. Held reactivation/moving-focus в train/calibration отсутствуют.
Positive-down sign сохранён; данные остаются synthetic, не real monitoring.

## Folds и spatial isolation

| Inner fold | Fit | Validation |
|---|---:|---:|
| inner_2020_world_0 | 14860 | 13507 |
| inner_2021_world_1 | 38456 | 8192 |
| inner_2022_world_2 | 61960 | 6871 |

Forward-only windows 2020/2021/2022, embargo **30 days**, strict fit target interval
before validation origin. World bucket = int(SHA256(world_id),16) mod 3;
внутри temporal fold все пять observation replicas мира на одной стороне.
Shuffle/random KFold не используется. 128 IDs не объявлены 128 независимыми полями:
два zero controls имеют одинаковую truth; это прежнее ограничение factorial design.

Также проверены **14 leave-profile-out** и **4 leave-zone-out** manifests.
Held group исключён из всех fit replicas и aggregate context; четыре profile
features пересчитаны только по contemporaneous non-held observations. Held profile
без контекста имеет count=0 и NaN aggregates до fit-only imputation. Для own-point
validation разрешена собственная прошлая history, но она не участвует в fit/aggregate
context. Zones — synthetic coordinate quadrants, не геологические блоки.
Negative perturbation held-zone values не изменила fit/validation aggregates.

## Preprocessing и reproducibility

**42 preprocessing states**: tabular+sequence для каждого из 21 folds.
Каждый содержит fold ID, sample count, ordered sample SHA256, schema SHA256,
median/mean/scale, category mappings и creation identity. State single-fit;
изменение record/fit membership отвергается. Отравление validation worlds,
calibration/evaluation и future history не меняет fitted parameters.

Две независимые сборки: `work/scenario_representation_v2_1/build01` и `build02`.
Все **50 deterministic files** byte-identical, включая все 42 states; финальный
`artifacts/splits/scenario_representation_v2_1` совпал с обеими. Giant tensors не
сохранялись. Runtime hash и подробности — в sequence report.

## Tests и interfaces

- Representation tests: **29 passed** (28 core + 1 constructor test).
- Existing relevant NONMODEL regression: **49 passed**.
- Data correction tests ранее: 15 новых + 36 existing = **51 passed**.
- B1/B5/B6/B7/B8: import, constructor, signatures, history preparation — PASS.
  Fit parameters не валидировались как benchmark configuration; это следующая
  preregistration, не запуск моделей.
- C01 architecture construction: PASS, пять numeric channels, без checkpoints,
  fit, forward или historical C01 reproduction.
- Models fitted/executed/scored = **0**; legacy holdout labels parsed = **0**.

Historical workflows, old canonical-label fixtures и сохранённый BLOCKED v2 suite
не запускались. Последний остаётся evidence ошибки v2, не тестом принятой v2.1.
Все 1307 первоначальных файлов сохранили hashes.

Machine authority: `artifacts/data_quality/scenario_representation_v2_1/acceptance_receipt.json`.
Reproducible commands:

```powershell
.venv/Scripts/python.exe scripts/validate_scenario_representation_v2_1.py --output work/scenario_representation_v2_1/independent01.json
.venv/Scripts/python.exe scripts/build_scenario_representation_v2_1.py --output work/scenario_representation_v2_1/build01
.venv/Scripts/python.exe scripts/build_scenario_representation_v2_1.py --output work/scenario_representation_v2_1/build02
.venv/Scripts/python.exe -m pytest tests/test_scenario_representation_v2_1.py -k 'not model_interface_construction_only' -q --basetemp work/scenario_representation_v2_1/pytest02 --junitxml work/scenario_representation_v2_1/pytest02.xml
.venv/Scripts/python.exe scripts/finalize_scenario_representation_v2_1.py --stage core
.venv/Scripts/python.exe scripts/smoke_scenario_v2_1_interfaces.py --kind baselines --core-receipt work/scenario_representation_v2_1/core_receipt.json --output work/scenario_representation_v2_1/baseline_smoke.json
work/environments/gate_c_torch/Scripts/python.exe scripts/smoke_scenario_v2_1_interfaces.py --kind c01 --core-receipt work/scenario_representation_v2_1/core_receipt.json --output work/scenario_representation_v2_1/c01_smoke.json
.venv/Scripts/python.exe -m pytest tests/test_scenario_representation_v2_1.py::test_model_interface_construction_only -q --junitxml work/scenario_representation_v2_1/interface_test.xml
.venv/Scripts/python.exe scripts/finalize_scenario_representation_v2_1.py --stage freeze
```

Для повторения выбрать новые scratch output paths; final destinations/receipts
write-once. Список regression tests содержится в machine test_execution_receipt.
C01 initial import был медленным, но исходная команда успешно завершилась. Поздний
диагностический retry отказался перезаписывать уже существующий receipt; его ошибка
write-once не является failed architecture test и не меняет PASS исходного smoke.

Commit/push не выполнялись. Следующий разрешаемый отдельно этап:
**PRE-REGISTERED V2.1 BASELINE BENCHMARK**. До него зафиксировать ограниченный набор
параметров/критериев сравнения; не менять frozen data, channels, folds или evaluator
после просмотра результатов. Никаких benchmark results этот выпуск не содержит.

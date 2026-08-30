# Gate B0/B1: базовые модели T1 и замороженный кандидат

## Решение

Gate B0/B1 реализован и воспроизведён. Пять предобъявленных моделей обучены и сравнены без доступа к T1 test на этапе выбора. По составному development-критерию заморожен `B1_persistence_last_rate` (`candidate_id=t1-b0b1-v1-3bfcff231705`). После фиксации candidate record test открыт ровно один раз; ledger имеет терминальный статус `consumed`.

Итоговая оценка аналитической валидации: **Share with caveats**. Все 93 независимые проверки метрик и хэшей прошли, но это stage-candidate Gate B0/B1, а не финальная модель диплома.

## Данные и границы

- задача: `T1_RATE_NEXT_PLANNED`;
- target: `observed_rate_mm_y`, мм/год;
- train: 911 origins;
- validation: 130 origins;
- test: 175 origins;
- split version: `t1_v1`;
- feature contract SHA-256: `241bad48e67659ffc953349cd2bce59e61f2a2867d57698abd6574edd4a05272`;
- train/validation загружались только через frozen manifests;
- test не загружался до появления `artifacts/model_selection/frozen_candidate.json`.

Канонический `training_weight` не использовался для fit: inverse-variance веса пересчитывались из `sigma_rate_mm_y` отдельно внутри каждого train scope с заранее заданным clipping `[0.25, 4.0]`. Это исключает перенос статистики будущего split через глобально нормализованные веса.

## Реализованные модели

| Model ID | Семейство | Предобъявленная конфигурация |
|---|---|---|
| `B1_persistence_last_rate` | persistence | последний доступный темп, train-median fallback |
| `B3_profile_robust_trend` | robust trend | clipped acceleration + last-3 mean + безопасный profile aggregate |
| `B5_fixed_kalman` | fixed state-space | `q=25`, constant velocity, причинная история до origin |
| `M1_ridge` | regularized regression | Ridge, `alpha=10`, exact feature allowlist |
| `M2_extra_trees` | tree baseline | 300 ExtraTrees, depth 8, leaf 4, deterministic seed |

`point_id`, `profile_id`, `sample_id` и campaign IDs не поступают в estimator matrix. В Kalman `point_id` служит только ключом локального состояния; строки истории после текущего origin программно отбрасываются.

## Протокол оценки

Использованы 24 forward-only фолда:

| Design | Фолдов | Принцип |
|---|---:|---|
| temporal holdout | 1 | train → validation 2024 |
| rolling-origin | 5 | expanding window, следующий target date |
| leave-profile-out | 14 | pre-2024 labels других профилей → validation held profile |
| leave-zone-out | 4 | pre-2024 labels других proxy-zones → validation held zone |

Во всех фолдах `max(train.target_date) < min(validation.target_date)`. Random split, `train_test_split` и обычный `KFold` запрещены executable guards.

Candidate score рассчитан до test как:

`0.50 × temporal normalized MAE + 0.20 × rolling normalized MAE + 0.20 × LPO normalized MAE + 0.10 × LZO normalized MAE + complexity penalty`,

где MAE нормирован на `B1_persistence_last_rate` внутри каждого design.

## Development-результаты

| Rank | Модель | Score | Temporal MAE | Rolling MAE | LPO MAE | LZO MAE |
|---:|---|---:|---:|---:|---:|---:|
| 1 | `B1_persistence_last_rate` | 1.000 | 7.311 | 6.824 | 7.311 | 7.311 |
| 2 | `B5_fixed_kalman` | 1.108 | 8.036 | 7.696 | 8.036 | 8.036 |
| 3 | `M2_extra_trees` | 1.184 | 7.528 | 6.765 | 8.681 | 16.340 |
| 4 | `M1_ridge` | 1.207 | 8.134 | 7.506 | 9.660 | 11.777 |
| 5 | `B3_profile_robust_trend` | 1.427 | 10.414 | 9.636 | 10.444 | 10.544 |

Persistence выиграл не только за счёт complexity penalty: у него минимальный temporal MAE и существенно более стабильный spatial holdout. ExtraTrees немного лучше на rolling-origin, но его LZO MAE вырос до 16.340 мм/год.

Temporal validation для замороженного кандидата:

- MAE: 7.311 мм/год;
- RMSE: 11.071 мм/год;
- bias: −3.046 мм/год;
- R²: 0.942;
- precision-weighted MAE: 5.883 мм/год.

## Однократный T1 test

После заморозки кандидата выполнена отдельная команда `--phase final-test`. До model-facing загрузки test был атомарно записан ledger со статусом `opening`; после сохранения predictions/metrics он переведён в `consumed`. Повторная попытка claim программно завершается ошибкой.

| Метрика | T1 test |
|---|---:|
| n | 175 |
| MAE | 10.135 мм/год |
| RMSE | 19.878 мм/год |
| bias | −5.165 мм/год |
| R² | 0.848 |
| precision-weighted MAE | 9.137 мм/год |

Test MAE на 38.6% выше temporal validation MAE. Это терминальный результат данного frozen candidate: изменять его параметры, состав признаков или правило выбора после просмотра test запрещено.

## Что именно проверено

- canonical Gate A1 inputs и manifests не изменены;
- development-артефакты не содержат target dates 2025+;
- все 3 040 development predictions конечны и не дублируются на `design/model/sample_id`;
- fit preprocessing принимает только provenance `train`;
- все пять model families проходят единый интерфейс manifest-aware fit/predict;
- Kalman инвариантен к искусственно добавленной строке будущего;
- frozen model, candidate config, development report и test outputs проверяются по SHA-256;
- MAE/RMSE/bias/R² для всех design/model и test независимо пересчитаны из prediction rows;
- итог: 93 checks, 0 failures; полный pytest: 47 tests до генерации артефактов (финальный прогон фиксируется отдельно).

## Ограничения и статус acceptance criteria

1. В полном `screening`-gate требуется сравнение с адаптивным B6 Kalman. Gate B0/B1 содержит только запрошенный fixed Kalman B5, поэтому общий критерий `every_model_compared_to_B1_and_B6` пока не закрыт.
2. Интервальные прогнозы, coverage 95% и transition-specific MAE не входят в этот этап.
3. 130 validation origins относятся к 90 точкам, поэтому строки не являются 130 независимыми траекториями.
4. LZO использует зафиксированные геометрические proxy-zones, а не авторитетные инженерные зоны.
5. Test уже раскрыт для этого stage-candidate. Последующие модели нельзя подбирать по его результату; честное финальное сравнение потребует заранее утверждённого нового временного/внешнего holdout либо отдельного governance-решения.

## Воспроизведение

Чистая development-фаза для новой версии кандидата:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements\modeling.lock.txt
.\.venv\Scripts\python.exe scripts\run_gate_b0_b1.py --phase develop
```

Однократный final test допускается только после ручной проверки candidate record:

```powershell
.\.venv\Scripts\python.exe scripts\run_gate_b0_b1.py --phase final-test
```

Текущий ledger уже `consumed`, поэтому повтор этой команды штатно должен завершиться отказом. Повторяемая проверка сохранённых результатов test-loader не вызывает:

```powershell
.\.venv\Scripts\python.exe scripts\run_gate_b0_b1.py --phase validate
.\.venv\Scripts\python.exe scripts\build_gate_b0_b1_notebook.py
.\.venv\Scripts\python.exe -m pytest
```

Машинная authority: `artifacts/model_selection/t1_b0_b1_v1/validation_report.json`. Reader-facing companion: `notebooks/02_gate_b0_b1_t1_baselines.ipynb`.

SHA-256 и размеры всех опубликованных Gate B0/B1 артефактов зафиксированы в `artifacts/model_selection/t1_b0_b1_v1/artifact_inventory.csv` (сам inventory исключён из самореферентного списка).

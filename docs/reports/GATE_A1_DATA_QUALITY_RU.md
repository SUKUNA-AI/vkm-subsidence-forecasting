# Gate A1 — аудит качества данных и leakage-контракт

**Вердикт: `PASS_WITH_WARNINGS`. Критических сбоев: 0.**

Gate A1 подтверждает пригодность канонических T1-данных для первого baseline-контура при обязательном использовании frozen manifests. T5 подготовлена технически, но из-за малого числа положительных случаев её выводы должны оставаться исследовательскими. Test закрыт model-facing загрузчиком до фиксации финального кандидата.

## 1. Канонические источники

Основная выборка строится только из `next_planned_features.csv`, `next_planned_operational_targets.csv`, `formal_feature_contract.csv` и `target_contract.json`. `early_warning_labels_formal.csv` является канонической таблицей labels для T5. Старые `next_cycle_features.csv` и `next_cycle_targets.csv` зарегистрированы как `historical_comparison_only` и не возвращаются модельным загрузчиком.

Каждый вход записан в `gate_a1_report.json` с относительным путём и SHA-256.

## 2. Grain и зависимости

Каноническая таблица содержит 1274 origin-строк, 98 временных траекторий точек и 14 профилей. Все 98 точек повторяются во времени; 26 campaign dates одновременно представлены более чем в одном профиле. Поэтому строка не является независимой статистической единицей.

Обычные `KFold`, `train_test_split`, `ShuffleSplit` и `shuffle=True` запрещены и контролируются тестами/сканированием исходников. Статические признаки неизменны внутри point trajectory и не должны создавать иллюзию дополнительных независимых наблюдений.

## 3. Frozen split manifests

| task | split | rows | current_date_min | current_date_max | target_date_min | target_date_max | points | profiles | missing_feature_fraction | positive | negative | censored |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T1 | train | 911 | 2018-10-16 | 2023-07-25 | 2019-02-12 | 2023-11-07 | 98 | 14 | 0.032382 | 0 | 0 | 0 |
| T1 | validation | 130 | 2023-11-07 | 2024-05-14 | 2024-01-30 | 2024-09-03 | 90 | 14 | 0.0341538 | 0 | 0 | 0 |
| T1 | test | 175 | 2024-05-14 | 2025-08-26 | 2025-07-22 | 2025-11-04 | 89 | 14 | 0.0334857 | 0 | 0 | 0 |
| T5 | train | 942 | 2018-10-16 | 2023-05-16 | 2019-02-12 | 2023-11-07 | 98 | 14 | 0.032569 | 12 | 930 | 0 |
| T5 | validation | 211 | 2023-07-25 | 2024-05-14 | 2023-11-07 | 2025-07-22 | 96 | 14 | 0.0336493 | 4 | 207 | 0 |
| T5 | test_complete | 28 | 2024-07-09 | 2024-09-03 | 2025-07-22 | 2025-07-22 | 28 | 5 | 0.0371429 | 1 | 27 | 0 |
| T5 | test_censored | 93 | 2025-07-22 | 2025-08-26 | 2025-08-26 | 2025-11-04 | 84 | 14 | 0.0341935 | 0 | 0 | 93 |

`missing_feature_fraction` рассчитана по всем ячейкам исполняемого allowlist. SHA-256 упорядоченного списка `sample_id`, диапазоны дат и распределения target находятся в полном `split_summary.csv` и JSON-отчёте.

T1 manifests содержат только 1 216 строк с `target_available=True` и статусом `observed`: train=911, validation=130, test=175. T5: train=942 complete, validation=211 complete, test_complete=28, test_censored=93.

## 4. Почему 18 membership-строк превращаются в 6 origins

По ключу `(campaign_id, point_id)` найдено 18 строк `observed=True` без строки в `leveling_adjusted_epochs.csv`. Из них 6 являются целевой плановой эпохой для существующего WORK-origin и поэтому дают ровно 6 строк `observed_but_no_adjusted_leveling`. Ещё 4 относятся к REF-точкам, не входящим в модельный WORK-universe, а 8 WORK-строк не имеют допустимого предыдущего origin в каноническом candidate frame.

Полная построчная трассировка сохранена в `artifacts/data_quality/membership_inconsistency_mapping.csv`. Шесть затронутых origins не входят ни в один T1 manifest и не участвуют в loss.

## 5. Leakage и preprocessing

`formal_feature_contract.csv` исполняется кодом: estimator получает ровно строки `allowed=True`. Идентификаторы, campaign IDs, private/generator поля, `true_*`, onset/regime/process fields и outcome-поля блокируются. Единственное target-поле в estimator — `target_campaign_type`, которое контракт трактует как заранее известную часть замороженного плана наблюдений; `forecast_horizon_days` также известен в момент прогноза.

`TrainOnlyPreprocessor.fit()` принимает только `ManifestDataset` со split=`train`. Fit на validation/test завершается ошибкой. Model-facing test load требует frozen-candidate record с совпадающими хэшами train, validation и feature contract.

> Ограничение: это программный барьер воспроизводимого проекта. Исходные CSV физически читаемы для аудита и не являются криптографически blinded.

## 6. Схемы оценки

| design | folds |
| --- | --- |
| leave_profile_out | 14 |
| leave_zone_out | 4 |
| rolling_origin | 5 |

Rolling-origin использует expanding window и никогда не помещает более позднюю дату в train относительно validation. Leave-profile-out создаёт 14 folds. Так как авторитетного `zone_id` для 98 WORK-точек нет, leave-zone-out v1 использует четыре замороженных геометрических квадранта, определённых медианами `x_local_m/y_local_m`. Координаты служат только split metadata и запрещены как estimator features; эти proxy-зоны не являются инженерным районированием.

## 7. Основные находки и ограничения

| finding_id | severity | status | evidence | remediation |
| --- | --- | --- | --- | --- |
| A1-F-001 | high | CONTROLLED | 1274 origins collapse to 98 point trajectories and 14 profiles. | Use temporal manifests, rolling origin, leave-profile-out, and leave-zone-out only. |
| A1-F-002 | high | CONTROLLED | 18 inconsistent membership rows map to 6 unlabeled origins; 4 are REF and 8 WORK rows have no eligible prior origin. | Keep the 6 origins out of loss; patch membership status in the next source-data revision. |
| A1-F-003 | high | OPEN | 58 sample_id values encode a historical target token different from canonical target_campaign_id. | Treat sample_id as an opaque join key in v1; regenerate IDs and version all manifests in a future data revision. |
| A1-F-004 | medium | OPEN | terrain_TRI_relative missingness=32.418%; lithology uncertainty missingness=100.000%. | Fit imputation on train only, retain missing indicators, and report ablations for sparse terrain/uncertainty fields. |
| A1-F-005 | high | OPEN | T5 has 17 complete positive labels in total and only 1 in test_complete. | Use average precision, fixed-FPR recall, confidence intervals, and treat T5 conclusions as exploratory. |
| A1-F-006 | high | OPEN | Largest T1 train-to-test numeric drift is n_history with \|SMD\|=2.320. | Report drift-aware temporal results and avoid random resampling. |
| A1-F-007 | medium | CONTROLLED | No authoritative zone_id exists; spatial_quadrants_v1 freezes coordinate-median quadrants for split-only use. | Replace with a domain-governed zone map in a new split version when available. |
| A1-F-008 | medium | CONTROLLED | The model-facing loader seals test until a matching frozen-candidate record is present; raw source CSVs remain audit-readable. | Run models through skru1.splits.load_split_dataset and retain source-code scanning in CI. |

Особенно важно: 58 `sample_id` сохранили target-токен старой next-available семантики, который не совпадает с каноническим `target_campaign_id`. В v1 ID допустим только как непрозрачный ключ. Парсить из него target campaign запрещено.

## 8. Воспроизведение

```powershell
.\.venv\Scripts\python.exe scripts\run_gate_a1.py --root .
.\.venv\Scripts\python.exe scripts\build_gate_a1_notebook.py --root .
.\.venv\Scripts\python.exe -m pytest
```

Машинный источник истины: `artifacts/data_quality/gate_a1_report.json`. Notebook предназначен для инспекции и не заменяет исполняемые тесты.

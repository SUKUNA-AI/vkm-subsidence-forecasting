# SKRU-1: воспроизводимый контур специальной части

Репозиторий содержит проверяемый data foundation, train-only model research,
контракты против leakage и governance-процедуру однократной будущей оценки
прогноза оседаний T1. Все operational paths относительны корню; исходные ZIP и
первичные таблицы не изменяются на месте; временные результаты создаются
только в `work/`.

## Текущее состояние: Gate B5/B6 завершены

Gate B5 заморозил расширенный train-only benchmark. Gate B6 исполнил
классические, вероятностные, longitudinal, boosting, glassbox, small neural и
neuro-fuzzy comparators только на `t1_v1/train`.

Итог B6 — **`PASS_NO_NEW_PRIMARY`**:

- `B7_two_regime_imm` остаётся единственным primary suite v4;
- rolling-origin pooled MAE B7 — **5,640 мм/год**;
- MAE B1 persistence — **6,311 мм/год**;
- train-only B7 skill относительно B1 — **+10,64%**;
- 95% conformal coverage B7 — **0,951**;
- ни одна новая модель не прошла одновременно rolling, audit-tail,
  transition, profile/zone, interval, sign-consistency и protocol gates;
- historical validation, раскрытый T1 test и новый holdout в B5/B6 не
  загружались.

Это **не финальная оценка внешней валидности**. Научный scope артефактов —
`train_only_internal_research`; production/final claim запрещён до появления
и однократного открытия заранее замороженного future/external holdout.

Подробный результат с семью проверенными графиками:
[`docs/reports/GATE_B6_EXPANDED_SCREENING_RU.md`](docs/reports/GATE_B6_EXPANDED_SCREENING_RU.md).

## Геометрия benchmark

Неизменяемый источник `t1_v1/train` содержит 911 model origins, но только 98
point trajectories, 14 profiles, четыре frozen spatial proxy zones и 19
target dates. Строки зависимы во времени и пространстве, поэтому random split,
обычный KFold и i.i.d. row bootstrap запрещены.

`t1_train_benchmark_v1` содержит:

- 11 rolling-origin outer folds и по три forward-only inner folds;
- 42 spatio-temporal leave-profile-out folds;
- 12 spatio-temporal leave-zone-out folds;
- всего 65 outer и 195 inner tuning contexts;
- learning curves на audit tail 2023-11-07 для 217, 423, 708 и 823 train
  origins без перенастройки параметров.

Held-out profile/zone исключается не только из outer train, но и из каждого
inner tuning fold. Focused campaigns 2023-01-17 и 2023-07-25 участвуют в
temporal evidence, но не в spatial CV из-за узкой географической поддержки.

## Исполненный model zoo

Исторически замороженный каталог содержит 23 записи, из которых реально
исполнены 22:

- frozen B1/B3/B5/B6/B7/B8, M1 Ridge и M2 ExtraTrees;
- ElasticNet, Huber, RBF-SVR, GPR и Gaussian GEE;
- HistGradientBoosting, quantile HGB, XGBoost, LightGBM и CatBoost;
- EBM и NGBoost;
- небольшой residual MLP и protocol-safe ENFS replica.

`Z15_tabpfn_v2_6` **исключён** governance-поправкой `B6-GOV-001` по решению
владельца проекта до лицензии, загрузки весов, predictions и scoring. Веса,
API-вызовы и TabPFN shards отсутствуют; runtime import/dispatch/network
запрещены. Historical row и старый lock остаются только как неизменяемая часть
B5 freeze, а current torch environment использует отдельный
`requirements/b6_torch_runtime.lock.txt` без этого package.

Полный статус моделей:
[`docs/governance/MODEL_CATALOG_B6.md`](docs/governance/MODEL_CATALOG_B6.md).

ETS, ARIMA/ARIMAX и VAR не обучаются механически: короткие нерегулярные
истории, пропуски campaigns и неполная синхронность делают такую оценку
зависимой от недоказанной интерполяции. Формальные cards имеют статус
`NOT_ELIGIBLE_DATA_GEOMETRY`.

## Быстрый запуск в PowerShell

Требуется CPython 3.13. Базовая среда для contracts, orchestration, tests и
reports:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements\bootstrap.lock.txt
.\.venv\Scripts\python.exe -m pip install -r requirements\modeling.lock.txt
.\.venv\Scripts\python.exe scripts\verify_inputs.py --root .
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider
```

Gate B5:

```powershell
.\.venv\Scripts\python.exe scripts\run_gate_b5.py --phase all
.\.venv\Scripts\python.exe scripts\verify_gate_b5_two_run.py
```

Изолированные B6 environments создаются отдельно; exact package versions,
wheel URLs и SHA-256 сохраняются в durable manifests:

```powershell
.\.venv\Scripts\python.exe scripts\stage_b6_environment.py --environment-id b6_cpu
.\.venv\Scripts\python.exe scripts\stage_b6_environment.py --environment-id b6_ngboost
.\.venv\Scripts\python.exe scripts\stage_b6_environment.py --environment-id b6_torch

.\.venv\Scripts\python.exe scripts\run_b6_environment_smoke.py --environment-id b6_cpu
.\.venv\Scripts\python.exe scripts\run_b6_environment_smoke.py --environment-id b6_ngboost
.\.venv\Scripts\python.exe scripts\run_b6_environment_smoke.py --environment-id b6_torch
```

Полный B6 workflow:

```powershell
.\.venv\Scripts\python.exe scripts\run_gate_b6.py --phase preflight
.\.venv\Scripts\python.exe scripts\run_gate_b6.py --phase screen
.\.venv\Scripts\python.exe scripts\run_gate_b6.py --phase robustness
.\.venv\Scripts\python.exe scripts\run_gate_b6.py --phase calibrate
.\.venv\Scripts\python.exe scripts\run_gate_b6.py --phase freeze
.\.venv\Scripts\python.exe scripts\run_gate_b6.py --phase validate
```

Worker CLI не принимает validation/test manifests. Каждый shard проверяется по
environment ID, frozen model/job/fold hashes, exact sample IDs, duplicate
constraints и train-only provenance.

Графики и executed notebooks строятся только из сохранённых machine artifacts
и ничего не переобучают:

```powershell
.\.venv\Scripts\python.exe scripts\build_gate_b6_figures.py --root .
.\.venv\Scripts\python.exe scripts\build_gate_b5_b6_notebooks.py --gate all --root .
```

## Ключевые артефакты

- B5 protocol: [`docs/governance/GATE_B5_B6_TRAIN_ONLY_PROTOCOL.md`](docs/governance/GATE_B5_B6_TRAIN_ONLY_PROTOCOL.md);
- B5 split design: `artifacts/splits/t1_train_benchmark_v1/`;
- B5 evidence: `artifacts/model_selection/t1_b5_evidence_v1/`;
- B6 evidence: `artifacts/model_selection/t1_b6_expanded_v1/`;
- frozen suite v4: `artifacts/governance/final_candidate_suite_v4.json`;
- B5 report: [`docs/reports/GATE_B5_EVIDENCE_BENCHMARK_RU.md`](docs/reports/GATE_B5_EVIDENCE_BENCHMARK_RU.md);
- B6 report: [`docs/reports/GATE_B6_EXPANDED_SCREENING_RU.md`](docs/reports/GATE_B6_EXPANDED_SCREENING_RU.md);
- B5 notebook: `notebooks/06_gate_b5_evidence_audit.ipynb`;
- B6 notebook: `notebooks/07_gate_b6_model_comparison.ipynb`;
- B6 model cards: `docs/model_cards/`;
- independent validation: `artifacts/model_selection/t1_b6_expanded_v1/validation_report.json`;
- SHA-256 inventory: `artifacts/model_selection/t1_b6_expanded_v1/artifact_inventory.csv`.

Предыдущие Gate A0/A1 и B0–B4 сохранены как историческая, hash-protected
цепочка в `artifacts/`, notebooks и reader-facing reports.

## Новый final holdout v3

Реального future/external holdout пока нет. Безопасная status-фаза не читает
target values:

```powershell
.\.venv\Scripts\python.exe scripts\run_holdout_v3.py --phase status --root .
```

Авторизованный владелец данных размещает локальный пакет в
`inputs/holdout_candidates/t1_final_v3/`; содержимое, кроме инструкции,
игнорируется Git. Перед доступом фиксируются eligibility, ordered sample
manifest, suite-v4 SHA-256, commit SHA, origins hash и sealed target hash.

После независимого review порядок один:

```powershell
.\.venv\Scripts\python.exe scripts\run_holdout_v3.py --phase status --root .
.\.venv\Scripts\python.exe scripts\run_holdout_v3.py --phase freeze --root .
# frozen record и ordered manifest проверяются и коммитятся до доступа
.\.venv\Scripts\python.exe scripts\run_holdout_v3.py --phase evaluate-once --root .
```

`evaluate-once` сначала необратимо расходует access ledger и только затем
читает labels. Любая ошибка после начала доступа также расходует попытку.
Post-access tuning, смена primary и выбор победителя среди context models
запрещены. Suite v4 заранее содержит B7 primary и B1/B5/B6/B8/Z01 только как
контекст.

## Окружения и ПК

Проверенная конфигурация пользователя: RTX 5070 Ti, Core i7-14700KF, 64 ГБ
DDR5-6400 и Samsung 990 Pro. Classical/state-space/boosting jobs запускаются на
CPU. CUDA используется только для residual MLP и ENFS; это уменьшает
недетерминизм boosters и сохраняет отдельную доказательную границу между
средами.

Полноценный Gate C для LSTM/GRU/TCN/TFT/PatchTST и других sequence models ещё
не начат. Малый B6 MLP и ENFS не заменяют sequence-specific protocol. Gate E
foundation models также остаётся отдельным будущим этапом; B6 после
`B6-GOV-001` не содержит foundation comparator.

## Научная граница и следующий шаг

Главный текущий вывод — B7 остаётся наиболее устойчивым внутренним primary,
но это утверждение ограничено train-only evidence. Следующий внешний выбор
разрешён только после появления нового real future/external holdout,
замороженного до доступа к labels. До этого допустимы новые заранее
специфицированные nested train-only исследования и подготовка Gate C, но не
дальнейшая настройка по историческому validation или раскрытому test.

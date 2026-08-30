# SKRU-1: воспроизводимый контур специальной части

Репозиторий фиксирует исходные данные дипломного проекта, контракты качества,
временные разбиения, модели T1 и governance-процедуру честной финальной оценки.
Все operational paths относительны корню репозитория; исходные ZIP и первичные
таблицы не изменяются на месте; временные результаты создаются только в
`work/`.

## Текущее состояние

Завершены Gate A0/A1 и модельные этапы B0/B1–B4. Последний эксперимент,
**Gate B4**, исследует новую observation model только внутри
`artifacts/splits/t1_v1/train.csv`:

- 911 train origins, 98 points и 14 profiles;
- внутренний temporal core — 823 origins, audit-tail — 88 origins на
  2023-11-07;
- 1 internal temporal, 5 rolling-origin, 14 forward leave-profile-out и 4
  forward leave-zone-out folds;
- Student-t degrees of freedom выбирается только по трём nested rolling folds
  внутри каждого outer-train;
- B1, B5, B6 и B7 используются с замороженными спецификациями;
- исторический validation и раскрытый test не загружаются.

Выбран `B8_student_t_robust_imm` с `ν=30`. На внутреннем temporal tail его MAE
равна 5,831 мм/год против 6,015 у B7; leave-zone MAE — 5,858 против 6,046.
Однако на целевом `volatile_or_gap` сегменте B8 хуже B7 на 0,45%, а
заранее заданный порог требовал улучшение не менее 10%. На pooled rolling
origins B8 также хуже B7 на 4,52%.

Поэтому B8 имеет статус `train_only_research_recorded`, а заранее объявленным
primary для будущего one-shot holdout остаётся `B7_two_regime_imm`. Машинный
audit Gate B4 прошёл 54 проверки без ошибок.

Нового future/external holdout в данных пока нет. Статус intake v3 —
`PENDING_DATA`; synthetic smoke fixtures, model predictions, старый validation
и раскрытый test не являются заменой независимому holdout.

## Быстрый запуск в PowerShell

Требуется CPython 3.13. Проверенная локальная среда:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements\bootstrap.lock.txt
.\.venv\Scripts\python.exe -m pip install -r requirements\modeling.lock.txt
.\.venv\Scripts\python.exe scripts\verify_inputs.py --root .
.\.venv\Scripts\python.exe -m pytest
```

Повторный воспроизводимый Gate B4 и notebook:

```powershell
.\.venv\Scripts\python.exe scripts\run_gate_b4.py --phase all --root .
.\.venv\Scripts\python.exe scripts\build_gate_b4_notebook.py --root .
```

Gate B4 runner не имеет phase для validation/test/final-test. Он работает
только с `t1_v1/train` и откажется менять уже замороженные
`t1_train_research_v1` manifests.

## Новый final holdout v3

Безопасная проверка текущего состояния не читает target values:

```powershell
.\.venv\Scripts\python.exe scripts\run_holdout_v3.py --phase status --root .
```

Реальный пакет предоставляет владелец данных локально в
`inputs/holdout_candidates/t1_final_v3/`; содержимое этой папки, кроме
инструкции, игнорируется Git. Контракт пакета и критерии eligibility описаны в
`docs/governance/FINAL_HOLDOUT_INTAKE_V3.md` и
`configs/final_holdout_v3.yaml`.

После независимого review порядок строго такой:

```powershell
.\.venv\Scripts\python.exe scripts\run_holdout_v3.py --phase status --root .
.\.venv\Scripts\python.exe scripts\run_holdout_v3.py --phase freeze --root .
# frozen record и ordered manifest должны быть проверены и закоммичены
.\.venv\Scripts\python.exe scripts\run_holdout_v3.py --phase evaluate-once --root .
```

`evaluate-once` сначала переводит ledger в consumed state и только затем читает
labels. Любая ошибка после начала доступа также расходует единственную попытку.
Post-access tuning, смена primary, feature engineering по результатам holdout и
повторный доступ запрещены. B1/B5/B6/B8 оцениваются только как контекстные
comparators; выбирать из них победителя после открытия нельзя.

## Основные артефакты Gate B4

- протокол: `docs/governance/GATE_B4_TRAIN_ONLY_PROTOCOL.md`;
- конфигурация: `configs/gate_b4.yaml`;
- train-only split manifests: `artifacts/splits/t1_train_research_v1/`;
- machine report и validation: `artifacts/model_selection/t1_b4_train_only_v1/`;
- frozen future-holdout suite:
  `artifacts/governance/final_candidate_suite_v3.json`;
- reader-facing report: `docs/reports/GATE_B4_ROBUST_INNOVATION_RU.md`;
- executed notebook: `notebooks/05_gate_b4_robust_innovation.ipynb`;
- holdout status: `artifacts/governance/final_holdout_v3_status.json`.

Предыдущие Gate B0/B1, B2 и B3 сохранены как неизменяемая история в
`artifacts/model_selection/`, соответствующих notebooks и reports.

## Источники и воспроизводимость

- Канонические bootstrap ZIP: `inputs/bootstrap/*.zip`.
- Внешний контроль: `configs/input_manifest.csv` и
  `configs/source_manifest.csv`.
- Внутренний контроль: manifests внутри архивов.
- Канонические T1 tables и feature/target contracts задаются Gate A1.
- Любая ошибка размера, SHA-256, структуры ZIP, schema, grain, временного
  порядка или split membership завершает соответствующую фазу ненулевым кодом.
- `work/run_01` и `work/run_02` воспроизводят распаковку независимо и не
  коммитятся; проверяемые inventories и отчёты находятся в `artifacts/`.

Конфигурация ПК пользователя (RTX 5070 Ti, Core i7-14700KF, 64 ГБ DDR5,
Samsung 990 Pro) более чем достаточна для текущих classical/state-space gates.
Gate B4 выполняется на CPU; отдельная CUDA/PyTorch lock-среда должна быть
зафиксирована до начала DL/GNN/foundation-model этапов, а не смешиваться с
текущим reproducible environment.

## Научная граница

Текущие результаты являются train-only и historical validation evidence, а не
финальной оценкой внешней валидности. Ни B7, ни B8 нельзя объявлять
production-quality/final model до однократного нового future/external holdout.
Количество сложных моделей само по себе не считается доказательством; каждая
следующая гипотеза требует preregistration, причинных признаков, временной и
пространственной проверки и нового независимого evaluation resource.

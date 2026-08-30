# SKRU-1: воспроизводимый контур специальной части

Репозиторий фиксирует исходные материалы дипломного проекта и формирует проверяемый контур для анализа данных, построения собственных моделей и сравнения с готовыми моделями.

Текущий этап — **Gate B0/B1**. Gate A0 и A1 пройдены; для T1 реализованы persistence, profile-aware robust trend, fixed Kalman, Ridge и ExtraTrees, выполнены temporal/rolling/profile/zone проверки, заморожен stage-candidate и однократно открыт test через candidate record.

## Быстрый запуск в PowerShell

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements\bootstrap.lock.txt
.\.venv\Scripts\python.exe -m pip install -r requirements\modeling.lock.txt
.\.venv\Scripts\python.exe scripts\verify_inputs.py --root .
.\.venv\Scripts\python.exe scripts\prepare_gate_a0.py --root . --replace
.\.venv\Scripts\python.exe scripts\capture_environment.py --root .
.\.venv\Scripts\python.exe -m pytest
```

Gate B0/B1 запускается по фазам. `develop` не загружает test; `final-test` допустим только один раз для уже замороженного кандидата; `validate` пересчитывает сохранённые результаты без повторного открытия test:

```powershell
.\.venv\Scripts\python.exe scripts\run_gate_b0_b1.py --phase develop
.\.venv\Scripts\python.exe scripts\run_gate_b0_b1.py --phase final-test
.\.venv\Scripts\python.exe scripts\run_gate_b0_b1.py --phase validate
.\.venv\Scripts\python.exe scripts\build_gate_b0_b1_notebook.py
```

Для текущей версии `final-test` уже потреблён и повторный запуск штатно блокируется. Итоговый отчёт: `docs/reports/GATE_B0_B1_T1_BASELINES_RU.md`.

Результаты распаковки создаются локально в `work/run_01` и `work/run_02` и не коммитятся. Проверяемые inventories, отчёты сверки и снимок окружения находятся в `artifacts/`.

`requirements/bootstrap.in` перечисляет базовые зависимости, `requirements/bootstrap.lock.txt` фиксирует контур A0/A1, а `requirements/modeling.lock.txt` — проверенные зависимости классических моделей Gate B0/B1. PyTorch/CUDA будут зафиксированы отдельно перед deep-learning этапом.

## Источники и границы воспроизводимости

- Канонические архивы: `inputs/bootstrap/*.zip`.
- Внешний контроль: `configs/input_manifest.csv` и `configs/source_manifest.csv`.
- Внутренний контроль: manifests, находящиеся внутри каждого архива.
- Любая ошибка размера, SHA-256, структуры ZIP или внутреннего manifest завершает подготовку ненулевым кодом.
- Каталоги `run_01` и `run_02` строятся независимо; совпадение их inventories является обязательным условием Gate A0.

Gate B0/B1 устанавливает воспроизводимую baseline-планку, но не доказывает готовность финальной производственной модели: adaptive B6, интервальная калибровка, transition validation и новый честный финальный holdout ещё не реализованы.

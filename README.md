# SKRU-1: воспроизводимый контур специальной части

Репозиторий фиксирует исходные материалы дипломного проекта и формирует проверяемый контур для анализа данных, построения собственных моделей и сравнения с готовыми моделями.

Текущий этап — **Gate A0**: проверить входы, безопасно и независимо распаковать доверенные ZIP, сверить внутренние manifests, записать inventories и подготовить два одинаковых чистых запуска. Моделирование начинается только после прохождения этого этапа.

## Быстрый запуск в PowerShell

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements\bootstrap.lock.txt
.\.venv\Scripts\python.exe scripts\verify_inputs.py --root .
.\.venv\Scripts\python.exe scripts\prepare_gate_a0.py --root . --replace
.\.venv\Scripts\python.exe scripts\capture_environment.py --root .
.\.venv\Scripts\python.exe -m pytest
```

Результаты распаковки создаются локально в `work/run_01` и `work/run_02` и не коммитятся. Проверяемые inventories, отчёты сверки и снимок окружения находятся в `artifacts/`.

`requirements/bootstrap.in` перечисляет прямые зависимости, а `requirements/bootstrap.lock.txt` фиксирует фактически проверенные версии. PyTorch/CUDA и библиотеки моделирования будут зафиксированы отдельным lock-файлом перед началом модельных экспериментов.

## Источники и границы воспроизводимости

- Канонические архивы: `inputs/bootstrap/*.zip`.
- Внешний контроль: `configs/input_manifest.csv` и `configs/source_manifest.csv`.
- Внутренний контроль: manifests, находящиеся внутри каждого архива.
- Любая ошибка размера, SHA-256, структуры ZIP или внутреннего manifest завершает подготовку ненулевым кодом.
- Каталоги `run_01` и `run_02` строятся независимо; совпадение их inventories является обязательным условием Gate A0.

Научные выводы, качество моделей и пригодность данных для конкретной целевой переменной этим этапом ещё не доказываются: Gate A0 подтверждает целостность и воспроизводимость файлового контура.

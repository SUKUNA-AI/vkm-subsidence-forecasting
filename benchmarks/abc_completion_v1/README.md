# Приёмка A/B/C и независимый source-first review

Пакет продолжает `figure_readings_v2` и `geometry_skru1_v1`. Он сохраняет первый
прогон, отдельно записывает решения Sol и разделяет записи на `ACCEPTED`,
`CONDITIONAL`, `UNRESOLVED`. Приёмка чтения или координат источника сама по себе
не допускает запись в scientific evidence или WorldSpec.

## Входы и результаты

Все исходные изображения, OCR, значения, координаты, source-linked decisions,
новые слои и полный пакет review находятся в игнорируемом
`work/abc_completion_2026-09-30/`. PRIVATE-клон обнаруживается через
`VKM_RESOURCES_ROOT`; машинные пути и содержимое источников в Git не попадают.

- `retries/`: отдельные immutable ответы GLM, overlapping crops, terminal
  source-review decisions и `closure.json`. Завершённый OCR остаётся
  `AUTO_EXTRACTED_UNREVIEWED`. Отсутствие текста и нечитаемый текст различаются.
- `coverage/`: все зарегистрированные источники, диапазоны страниц, SHA,
  caption/context screening, native checks и отбор новых объектов. Screening
  всех источников не доказывает полноту визуального recall на каждой странице.
- `readings/`, `geometry/`, `geology/`: отдельные наборы A/B/C, решения по прежней
  очереди, provenance и receipts. Неполное значение остаётся UNKNOWN.
- `accepted/`: родительские наборы и отдельные файлы трёх статусов для A/B/C.
- `ASTRA_REVIEW_PACKAGE/source_first_index.json`: вход следующего reviewer.
  Изображения и локаторы открываются до отдельных `sol_decisions/`. Затем
  сравнивается собственная интерпретация с Sol и записывается
  `PASS / CORRECT / UNRESOLVED`.
- `frozen_integrity.json`: фактическое сравнение прежних файлов с хэшами
  предыдущих receipts, включая raw responses, собранные readings, GeoJSON и GPKG.

PUBLIC хранит этот код, synthetic tests, методы и агрегированную квитанцию.
Источник научного содержания остаётся PRIVATE. Код не назначает неизвестный
CRS, не переносит региональную геологию на СКРУ-1 и не восстанавливает число
по его обрезанному префиксу.

## Воспроизведение

Требуются существующие входные пакеты и сохранённые source-review decisions.
Подготовка решения человеком не заменяется повторным запуском materializer.

```console
python -B benchmarks/abc_completion_v1/readings.py
python -B -m benchmarks.abc_completion_v1.close_retries
python -B -m benchmarks.abc_completion_v1.frozen_integrity
python -B -m benchmarks.abc_completion_v1.package
python -B -m pytest -q tests/corpus/test_abc_*.py
```

PowerShell не раскрывает wildcard в аргументах pytest; передайте существующие
файлы тестов явно или сформируйте список через `Get-ChildItem`.

`retry_glm.py` требует принадлежащий исполнителю GPU-lock `SOL-ABC` и готовый
локальный GLM endpoint. Первые ответы и ранее принятый gold не перезаписываются;
повтор использует сохранённый request SHA. Повторное создание серверов или
массовое чтение источников не является побочным действием сборки accepted package.

Метры принимаются только при явно напечатанных единицах и поддержанных
соответствиях. Pixel registration, roundtrip сериализации и структурные tests
не являются геодезической точностью или field validation. Неразрешённые rings
не становятся площадями или принятой geometry после автоматического MakeValid.

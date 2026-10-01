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
  `screening_finalization_receipt.json` отдельно фиксирует visual role checks,
  dedup и disposition; исходная квитанция screening сохраняется.
- `readings/`, `geometry/`, `geology/`: отдельные наборы A/B/C, решения по прежней
  очереди, provenance и receipts. Неполное значение остаётся UNKNOWN.
- `accepted/`: родительские наборы и отдельные файлы трёх статусов для A/B/C.
- `ASTRA_REVIEW_PACKAGE/source_first_index.json`: вход следующего reviewer.
  Изображения и локаторы открываются до отдельных `sol_decisions/`. Затем
  сравнивается собственная интерпретация с Sol и записывается
  `PASS / CORRECT / UNRESOLVED`.
- `frozen_integrity.json`: фактическое сравнение прежних файлов с хэшами
  предыдущих receipts, включая raw responses, собранные readings, GeoJSON и GPKG.
- `geometry/topology/`: классификация прежних invalid candidates и отдельные
  производные слои. Разбиение допустимо только по существующим повторным
  вершинам при сохранении ориентированных рёбер; строгая проверка QGIS остаётся
  отдельным условием. Валидный graphic candidate ещё не принят как выработка.

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
python -B -m benchmarks.abc_completion_v1.public_receipt
python -B -m pytest -q tests/corpus/test_abc_*.py
```

PowerShell не раскрывает wildcard в аргументах pytest; передайте существующие
файлы тестов явно или сформируйте список через `Get-ChildItem`.
Publisher требует завершённый `verification_summary.json`; создание пакета
до этого не означает успешного прохождения всех QA-гейтов.

### Контракт QA перед PUBLIC-публикацией

`verification_summary.json` создаёт оператор в PRIVATE work после фактических прогонов QA;
автоматического producer этого файла нет. Publisher не запускает pytest или canonical verifier и
не присваивает им PASS. Старые квитанции остаются историческими артефактами; для новой публикации
нужен актуальный файл схемы `vkm.sol_abc_verification/1` с ровно следующими полями:

| Поле | Допустимое содержимое |
|---|---|
| `schema` | `vkm.sol_abc_verification/1` |
| `status` | `PASS` или `PASS_WITH_DISCLOSED_LIMITATIONS` |
| `private_output_hashes` | Все ключи `OUTPUT_PATHS` из `public_receipt.py` и SHA-256 текущих файлов относительно `--work`; сюда входит сам `completion_receipt.json` |
| `code_hashes` | Относительный PUBLIC-путь → SHA-256 всех `benchmarks/abc_completion_v1/*.py`, а также `src/vkm_world/governance/publication.py` и `leakage.py` |
| `checks` | Ровно `accepted_datasets`, `queue_closure`, `glm_retry_closure`, `coverage`, `topology`, `frozen_integrity`, `public_leakage`, `tests`, `canonical_repository` |
| `limitations` | Только счётчики `optional_runtime_skips` и `unavailable_frozen_git_refs`, целые неотрицательные числа; при отсутствии ограничений `{}` |

Значения всех `checks` — `PASS`; только `tests` и `canonical_repository` допускают
`PASS_WITH_DISCLOSED_LIMITATIONS`, соответственно при ненулевых `optional_runtime_skips` и
`unavailable_frozen_git_refs`. Итоговый `status` обязан совпадать с наличием этих ограничений.
FAIL, NOT_RUN, отсутствующий required check и произвольный вложенный текст отклоняются.

Оператор сохраняет фактические логи и код выхода в PRIVATE work, выполняет проверки accepted datasets,
закрытия очереди и retries, coverage, строгую topology/QGIS-приёмку, frozen bytes и leakage, затем
тематические tests из команды выше и `scripts/verify_canonical_repository.py` с настроенным PRIVATE.
Сводку заполняют по этим результатам; хеши вычисляют после последнего изменения входов и кода.
Любое последующее изменение требует повторной проверки и обновления сводки. Publisher сопоставляет
полный набор хешей и заново проверяет frozen bytes по предыдущим квитанциям и полному file table.
Разбор completion, frozen receipt и QA использует те же зафиксированные байты, по которым вычислены
хеши. После staging, leakage scan и подготовки резервных копий publisher под process lock повторно
сверяет текущие входы, QA, инвентарь кода, frozen files и ссылки предыдущих квитанций до замены PUBLIC-файла.
Хеш-привязка не заменяет фактическое выполнение заявленных оператором проверок.

PUBLIC содержит только известные поля агрегатов, фиксированные категории статусов и целые
неотрицательные counts; неизвестный вложенный ключ, bool вместо count, отрицательное, дробное или
нечисловое значение блокируют публикацию. Исходные partitions, record IDs, literal readings и пути
остаются PRIVATE. Проверка выполняется до замены существующей PUBLIC-квитанции.

`retry_glm.py` требует принадлежащий исполнителю GPU-lock `SOL-ABC` и готовый
локальный GLM endpoint. Первые ответы и ранее принятый gold не перезаписываются;
повтор использует сохранённый request SHA. Повторное создание серверов или
массовое чтение источников не является побочным действием сборки accepted package.

Метры принимаются только при явно напечатанных единицах и поддержанных
соответствиях. Pixel registration, roundtrip сериализации и структурные tests
не являются геодезической точностью или field validation. Неразрешённые rings
не становятся площадями или принятой geometry после автоматического MakeValid.

Следующая Astra-сессия также должна независимо разметить новые поисковые hits
по отдельному протоколу из [отчёта поиска](../../docs/corpus_platform/SEARCH_METRICS_2026-09-30_RU.md).
Это отдельная relevance-разметка: scientific evidence и старые frozen labels
не перезаписываются. По решению владельца новый pool и labels в Sol-сессии
не создаются.

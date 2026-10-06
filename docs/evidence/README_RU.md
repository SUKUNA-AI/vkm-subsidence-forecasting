# EVIDENCE: происхождение, проверка и научный допуск

Этот слой дополняет DOCUMENT/DATASET и использует provenance, единицы и время
WorldSpec. Он не превращает автоматическую транскрипцию в факт. Статус реализации
всей программы находится в [живом плане](../planning/PRODUCTION_DATA_PROGRAM_2026-10-01_RU.md).

## Канон и запись

`canonical/evidence/` располагается в защищённом runtime data root, вне Git.
Оригиналы сохраняются отдельно. Хешированные Parquet partitions, parent-linked
commits и один `HEAD` являются каноном; PostgreSQL queue и производные базы могут
быть восстановлены. Единственный publisher использует exclusive lock. Зависшую
lease нельзя автоматически удалять по возрасту: сначала оператор устанавливает,
что прежний процесс остановлен.

Публикация требует `request_id`, точный `base_revision`, настроенного principal
и resolver исходных объектов. ACK выдаётся после durable HEAD. Повтор с тем же ID
и содержимым возвращает прежний receipt; другое содержимое и stale base отклоняются.
Исправление создаёт следующую revision с точным `supersedes`, а прежняя остаётся.
Проверка original locator, source/content hashes и bounded text spans выполняется
по каноническому объекту, а не по полям запроса.

Типы: Mention, Claim, Observation, ObservationSet, Entity,
EntityResolutionDecision, FormulaInterpretation, EventAssertion, EvidenceRelation,
ReviewDecision и ScientificUseAdmission. Их [JSON schemas](../../schemas/evidence/evidence_batch.schema.json)
генерируются `scripts/export_evidence_schema.py`; `--check` проверяет совпадение.
L1 document rows остаются документной структурой.

## Policy и scientific use

`access_class` и `experimental_role` — разные поля. Разрешённое владельцем CLOUD
чтение TARGET оформляется настроенным AccessContext с `allow_targets=true` и нужным
классом доступа. Оно не доказывает слепую независимую валидацию. PUBLIC не является
неявным default. Restricted/local/sealed доступ проверяется до чтения содержимого.
Policy derived records наследует ограничения original supports и точных зависимостей.

Reviewer authority определяется конфигурацией publisher, а не записью автора.
VERIFIED_TRANSCRIPTION/SEMANTIC_REVIEWED требуют проверенных original supports,
включая определения символов формулы. Отдельный immutable ReviewDecision хранит
основание, автора проверки и версию target. Запись с собственным `review_state`
без соответствующего решения не принимается.

ScientificUseAdmission связывает цель использования, `t0`, полный набор точных
зависимостей, policy hash и действующие semantic reviews. Неизвестная availability,
неразрешённая identity, неподтверждённый первичный origin, unreadable value или
неразобранная формула закрывают допуск. Проверка новой версии сущности или другого
semantic reference инвалидирует старый допуск; история не переписывается.
Две публикации одного observation set не становятся двумя независимыми наблюдениями.

Для численного использования Observation одного наличия Quantity недостаточно: нужны явные point, полный interval
или discrete set, разрешённая единица и согласованная размерность. Архивные UNKNOWN и unresolved units сохраняются,
но дают NOT_READY. Quantity provenance связывается с проверенными original supports по source ID и конкретному
locator/pdf page либо через точные версии evidence; произвольный `inputs` без dependency pin не заменяет источник.
Native XML locator остаётся XML locator и не получает выдуманную PDF page. Причины отказа содержат коды без raw values.

SEMANTIC_REVIEWED — утверждение configured reviewer; `checks` описывает выполненную проверку, а не выдаёт полномочия
и не отключает структурные gates. Свободный текст `purpose` не задаёт target site/scale. READY подтверждает использование
в исходном контексте и не разрешает LAB→MASSIF или перенос на СКРУ-1: потребитель обязан отдельно вызвать существующие
WorldSpec `check_scale_use`/`check_site_use`/`transfer_use_errors` для явно заданного требуемого контекста. Масштаб и scope
исходного Quantity не меняются, а разрешение владельца читать TARGET не заменяет правила независимого теста.

`record.time` и существующий WorldSpec `provenance.temporal` сохраняются раздельно.
Пустой default не отменяет явное время provenance; несовместимые интервалы дат
закрывают known-at. Совместимые даты разной точности не становятся противоречием:
доступность разрешается по самому позднему допустимому bound, без молчаливого уточнения.
`recorded_at` — время регистрации решения,
оно не подменяет событие, измерение, публикацию или `available_from`. Срез `as_of`
консервативно исключает UNKNOWN, будущие и устаревшие version pins во всей closure.
Живая policy проверяет оригиналы сохранённых версий зависимостей: исправление записи
не обходит отзыв доступа к исходному источнику. Эта cache view пока не квалифицирована
на полном размере корпуса; окончательный memory/latency gate остаётся обязательным.

## Полнота

CoverageLedger учитывает expected units, detected candidates, собственные attempts
и точные output identities. Suppressed, failed, unreadable, duplicate и unsupported
кандидаты имеют явный disposition. Чужую успешную попытку заимствовать нельзя.
`ACCOUNTED` относится к учёту и не устанавливает detector recall или scientific use.

Existing snapshot inventory — отдельная metadata baseline. Она читает один source
bounded partitions, фиксирует document denominator и пропущенные страницы, проверяет
source/object identity. Она не открывает originals и не доказывает их свежие bytes.
Старый результат помечается `LEGACY_NOT_ATTESTED`; автоматическое повторное извлечение
не запускается. Summary counts доступны только оператору с правами на served corpus.

## CLI

Runtime roots задаются существующими `VKM_DATA_ROOT` и `VKM_RESOURCES_ROOT`.
Файлы context/policy/reviewers содержатся вне Git. Ниже логические имена файлов,
а не готовая конфигурация какой-либо машины:

```text
vkm-corpus evidence validate-batch --input batch.json
vkm-corpus evidence coverage --input coverage.json
vkm-corpus evidence inventory-summary --policy policy.json --context context.json
vkm-corpus evidence inventory-source --policy policy.json --context context.json --source-id VKM-SRC-001 --campaign-sha256 <SHA256>
vkm-corpus evidence publish --input batch.json --context context.json --policy policy.json --reviewers reviewers.json --request-id <ID> --base-revision <HEAD>
vkm-corpus evidence get <RECORD_ID> --context context.json --policy policy.json
vkm-corpus evidence dependencies <RECORD_ID> --context context.json --policy policy.json --limit 500
vkm-corpus evidence export --context context.json --policy policy.json --output evidence.jsonl
vkm-corpus evidence project --context context.json --policy policy.json --output projections
```

Перенос опор на новый снимок канона (ревизии+1, только с разрешения владельца) — `evidence rebase-plan` и
`evidence rebase-publish`, см. [REBASE_RU.md](REBASE_RU.md).

SCHEMA_VALID означает только схему. Incomplete/invalid inventory возвращает nonzero.
Export проходит все generation-bound cursor pages и создаёт SHA manifest только
после полного durable output; уже существующий файл не перезаписывается.

## API и согласованное поколение

Существующий API предоставляет `/v1/evidence`, `/v1/evidence/records/{id}`,
`/v1/evidence/dependencies/{id}` и ограниченную POST `/v1/evidence/review`.
`/v1/evidence/review-packet/{id}` готовит [пакет проверки](REVIEW_PACKET_RU.md)
с исходными locators, preview limits и точными зависимостями; готовый пакет сам
не является проверкой. READ tools `vkm-corpus` добавляют record/list/dependencies/
review-packet без shell/SQL/Cypher.
Постраничная выдача привязана к поколению, объекту, фильтрам, principal и живой
source policy. Изменённый cursor отклоняется; counts относятся к разрешённой выдаче.
Длинные таблицы доступны через существующий table cursor без обрезания dataset.

Новый `api serve` по умолчанию использует production profile и требует
`VKM_SOURCE_POLICY_FILE`, `VKM_ACCESS_CONTEXT_FILE`, `VKM_UPDATE_RUNTIME_FILE`.
До начала content serving проверяются actual selected canonical/NAV databases,
manifest, policy generation, code identity и acceptance receipt. Проверка выполняется
и на запросах; maintenance или mismatch закрывают выдачу. GRAPH/SEARCH/remote packs
нельзя пропустить из обязательных observers. Пока их live adapters не квалифицированы,
полный remote profile BLOCKED. Явный `VKM_API_PROFILE=compatibility` сохраняет старый
reader для перехода и сообщает свой профиль; он не является production acceptance.
Действующий сервер автоматически не перезапускается.

Projection reuse зависит от evidence revision, AccessContext, live source policy,
source/rule fingerprints, dependency versions и pinned production producer identity.
На выходе подготовлены Parquet/DuckDB/Neo4j/OpenSearch files одного поколения.
Это staging outputs, а не подтверждение загрузки удалённых баз. Manifest записывается
последним. Crash после записи DuckDB допускает retry только после точной проверки
его логической эквивалентности verified Parquet; разные physical bytes не объявляются
сами по себе различием научных данных.

Projection contract v2 хранит original occurrences, exact semantic/version references,
review/admission edges и первичные observation origins отдельно от публикаций.
Graph node keys включают hash версии: старое ребро нельзя перенаправить на исправленное
значение по одному logical ID. Отсутствующая referenced version даёт
`BLOCKED_MISSING_REFERENCED_VERSIONS`; remote load всегда `NOT_RUN`, пока отдельный
adapter не прошёл qualification. Предыдущие outputs не переписываются и не удаляются.

## Оставшиеся production gates

Полный корпус не обрабатывался; независимый gold, проверка всех выбранных научных
inputs, full-scale capacity, power-loss durability и restore реальных уникальных
решений не установлены. Single-writer журнал и текущая metadata view должны пройти
квалификацию memory/latency на размере запланированной кампании. Target p95 не
публикуется как измеренная производительность. Следующие стадии выполняются по
[update runbook](../corpus_platform/UPDATE_RUNTIME_RUNBOOK_RU.md), с отдельными
gates приёма originals 07.10 и полной семантической программы.

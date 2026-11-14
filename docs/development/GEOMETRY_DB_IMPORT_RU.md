# A/B → локальная научная проекция и существующий MCP

Адаптер `src/vkm_geometry/db_adapter.py` добавляет партийную проекцию в
существующую `scientific_r3_20261112`. Это производные источниковые геометрии,
не новый канон, не измеренные координаты рудника и не научный допуск.
Production CORE/EDGE и активация ScientificReader не входят в рецепт.

## Вход и проверка ссылок

`prepare_batch(batch, registry)` использует `transport.batch_rows(batch)`.
Сохраняются stable source_feature_id, весь A, все связанные B, provenance,
исходные multipart/вершины и отдельная coordinate frame. Пиксели не становятся
метрами из-за единицы подписанного значения. WKB имеет SRID 0; CRS UNKNOWN/null
не заменяется EPSG. `work_id` не выводится из номера источника.

Вход `registry` — закреплённые ответы существующих `get_source`/`get_work`:
`source_snapshot_id`, `sources:[{record,envelope}]`, `works:[{record,envelope}]`,
`source_work_links:[{source_id,work_id,...}]`. Нужны существующий источник,
произведение и неотклонённая связь между ними. Статусы AUTO_PROPOSED, basis,
locator/version и source snapshot сохраняются целиком. Наличие FK не повышает
статус этой связи до FACT/HUMAN_REVIEWED. Неверный fallback sourceID-as-workID
отклоняется; исходный A не исправляется адаптером.

План содержит полный batch JSON, source references, точные transport rows,
registry SHA и content digest. Подмена строк/плана отклоняется. Исходные A/B,
изображения, host-local paths и receipts остаются PRIVATE/ignored work.
Публичные тесты используют только synthetic records.
Проверки prepare и verify_plan независимо декодируют WKB, сравнивают тип,
все конечные 2D vertices/components, frame/unit, исходные A/B и batch provenance.
Правильно пересчитанный SHA не разрешает несогласованные данные; такой план
отклоняется до DB write и до создания query snapshot.

## Таблицы и границы FK

Нужная схема создаётся отдельно разрешённым изолированным setup. Адаптер не
восстанавливает существующую научную базу и не запускает SDB-01 importer,
который исключает SRC-011. Переиспользуется механизм SDB: plan/digests,
guard незатронутых таблиц, dry-run, apply, новые соединения, повтор и откат.

- `geometry_batch_projection`: batch key, digest, версия, полный batch JSON.
- `geometry_source_ref_projection`: batch-local ссылки на существующий registry.
- `source_geometry_features`: WKB, frame/unit, полный A/B и row SHA.

В PostgreSQL действуют PK и SQL FK feature→batch-local source/work reference→batch,
а также geometry NOT NULL/CHECK SRID 0. Это FK к производной проверенной ссылке,
не выдуманный FK к отсутствующей canonical source table. Actual R3 DDL имеет
`source_page`/`observations` с `_package_row` PK; source/work uniqueness/FK там
не заявляются. Эти таблицы адаптер не меняет.

В DuckDB действуют PK, а source/work references проверяются логически перед
записью и при полном readback. Native DuckDB FK намеренно не ставятся:
проверенная версия 1.5.6 запрещает удалить child и referenced parent в одной
транзакции даже в правильном порядке. Это мешает атомарному откату партии.
Прямые SQL writers DuckDB должны проходить тот же адаптер; native FK-защита
обходных SQL-записей не заявлена. См. [ограничения FK](https://duckdb.org/docs/stable/sql/indexes).

PostGIS хранит геометрию без изменения исходных координат. DuckDB сохраняет WKB
и проверяет native `ST_GeomFromWKB`/`ST_AsWKB` через установленный `spatial`.
Рецепт не устанавливает runtime/расширения при чтении и не вызывает spatial
операции между несовместимыми рамками.

## Использование и отказоустойчивость

```python
from vkm_geometry.db_adapter import (
    SCOPE, prepare_batch, apply_batch, readback, rollback_batch,
    build_query_snapshot,
)

plan = prepare_batch(batch, pinned_registry_reference)
dry = apply_batch(plan, pg, duckdb_connection, scope=SCOPE)  # rollback обеих сторон
applied = apply_batch(plan, pg, duckdb_connection, commit=True, scope=SCOPE)
verified = readback(plan, fresh_pg_factory, fresh_readonly_duckdb_factory)
repeated = apply_batch(plan, pg, duckdb_connection, commit=True, scope=SCOPE)
# repeated: postgres_inserted=0, duckdb_inserted=0
rolled_back = rollback_batch(plan, pg, duckdb_connection, scope=SCOPE)
```

Write scope должен быть ровно `ISOLATED_LOCAL_PRIVATE_TEST`; PostgreSQL host —
loopback, database name начинается `vkm_geo_`, connection idle. Не передавать
соединение с чужой незавершённой транзакцией. DuckDB connection также выделяется
этой операции, а readback factory открывает новый файл read-only. Перед actual
write сохранить конкретные DB/container/volume/image/input SHA в private checkpoint.
Один writer на локальную пару; секреты в примеры/логи не включать.

Повтор проверяет весь существующий batch, все строки и references. Same batch ID
с иным digest/геометрией/атрибутами даёт Conflict, без ON CONFLICT UPDATE.
Коррекция — новая явная версия/партия с provenance, не скрытая замена raw.
Rollback удаляет только собственный совпадающий batch; schema/таблицы сохраняются,
чужие партии и исходные scientific tables не удаляются.

Две БД не имеют общей транзакции. PostgreSQL commit идёт первым. Если DuckDB
commit не выполнен, `PartialApply.receipt` явно сообщает COMMITTED/ROLLED_BACK
либо UNKNOWN. Не включать такую пару в current reader. Повтор того же плана
дописывает отсутствующую сторону и заново проверяет существующую; возможен
согласованный compensating rollback. Не объявлять atomic success по одному commit.

## Локальный неизменяемый снимок и чтение MCP

После полного fresh readback:

```python
snapshot = build_query_snapshot(
    plan, new_output_directory,
    allowed_token_labels=["geometry-local-read"], page_size=250,
    artifact_links=verified_cad_links_by_feature,  # необязательно
)
```

Первый аргумент также принимает список проверенных планов одного document
snapshot: producer batch IDs в строках сохраняются. Повторный feature ID между
планами отклоняется как неоднозначный; это объединение поисковой проекции,
не объединение источниковых научных утверждений или физических рамок.

Снимок построен из точных плановых строк, равенство которых с обеими БД
устанавливает отдельный readback receipt. Native Parquet и manifest закрепляются
SHA-256. Snapshot ID зависит также от файлов, запросов, principals и CAD annex,
поэтому изменение проекции не маскируется прежним ID.
`artifact_links` — отдельные `{uri,kind,sha256}` на проверенные CAD outputs,
привязанные к существующим feature ID. Они не переписывают исходный A/DB batch;
проверку существования и байтов артефактов выполняет владелец native CAD receipt.

Используется existing ScientificReader role
`source_geometry_features=SOURCE_GEOMETRY_DIGITIZATION`, existing API
`/v1/science/status`, `/v1/science/query/{query_id}`, existing read MCP server.
Добавлены только read-only `scientific_status` и `scientific_query`:

1. `scientific_status {}` возвращает snapshot ID и query catalogue.
2. В catalogue выбрать source/figure/horizon/type/frame/status/CAD facet.
3. `scientific_query {query_id, snapshot_id, limit}` выполняет только
   операторский SELECT; limit 1–500, чужой principal и stale snapshot отклоняются.
4. Для полного обхода следовать `next_query_id` и `required_limit`. `truncated=true`
   означает неполный ответ; отсутствие его проверки нельзя считать полной сверкой.

Карточки компактны: координаты и полный A не вкладываются в каждую выдачу.
NULL horizon остаётся SQL NULL/JSON null; UNKNOWN остаётся исходной строкой.
Сложный horizon сохраняется отдельно в `horizon_json`, без придуманной трактовки.
Для NULL объявлен отдельный `IS NULL` facet. Original/metadata и обе стороны B
читаются адресным metadata query. WKB выдаётся частями по 4096 hex symbols с
chunk index/count и linked query IDs; после сборки сравниваются все vertices и
multipart structure, а не только count/hash. Метризация/геологическая семантика
не меняются чтением.

Проекция имеет статус PREPARED_NOT_ACTIVATED. Локальный MCP SDK Client→existing
MCP server→existing API ASGI/ScientificReader проверяет initialize/list_tools/
call_tool и principals без production config. Это протокольная локальная
проверка, не доступность новой партии в уже запущенном клиенте/CORE.

## Приёмка ограниченной поставки

Private handoff содержит source/work proof, input/code SHA, DDL source/hash,
scope до записи, dry-run/apply/fresh read/repeat/rollback/reapply, expected
conflict, partial-commit fault injection/recovery, local MCP calls, all-ID WKB
reassembly и vertex counts. Native CAD/QGIS артефакты проверяются своими владельцами.
Schema readiness, synthetic tests и успешный import не дают научного допуска.

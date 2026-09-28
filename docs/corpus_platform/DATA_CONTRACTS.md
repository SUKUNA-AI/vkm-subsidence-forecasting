# VKM Corpus Platform v0 — контракты данных (канонический слой L1)

Практическое описание канонического структурированного слоя: датасеты, словари, ID, провенанс, текстовые правила,
раскладка корня данных, подписи, коммиты, снимки, валидатор, DuckDB. Решения — в
[журнале координатора](../implementation_work/COORDINATOR_DECISIONS.md) (CP-05…CP-09, CP-12, CP-15, CP-16) и
[ревью H](../implementation_work/AGENT_H_ARCHITECTURE_REVIEW.md); обоснования — в
[проекте D](../implementation_work/AGENT_D_CANONICAL_DATA_DESIGN.md). Там, где проект D расходится с CP-16 или с ответом
на H, действует этот документ.

Источник истины схемы — pydantic-модели `vkm_corpus.contracts.models`. Из них детерминированно генерируются Arrow-схемы
и JSON Schema в `schemas/corpus/` (например, [pages.schema.json](../../schemas/corpus/pages.schema.json),
[arrow_schemas.json](../../schemas/corpus/arrow_schemas.json), [vocabularies.json](../../schemas/corpus/vocabularies.json),
[id_grammar.json](../../schemas/corpus/id_grammar.json)). Версия схемы всех датасетов — `0.1.0`; до выполнения H-46
она остаётся `0.x`, и повышение MINOR считается несовместимым.

## 1. Что где лежит

| Модуль | Назначение |
|---|---|
| `vkm_corpus.contracts.vocab` | все закрытые словари (`StrEnum`) платформы и таблицы применимости; AST-тест запрещает объявлять `StrEnum` вне `vkm_corpus/contracts` |
| `vkm_corpus.contracts.models`, `.base` | модели строк 20 датасетов и вложенные struct |
| `vkm_corpus.contracts.datasets` | реестр датасетов: класс, версия, PK, ключ сортировки, партиции |
| `vkm_corpus.contracts.arrow` | Arrow-схемы, fingerprint схемы, мультимножественные дайджесты строк |
| `vkm_corpus.contracts.text_rules` | `normalize_text_v1`, `page_text_v1`, `rerank_text_v1` |
| `vkm_corpus.contracts.signatures` | `stage_signature`, `call_signature`, `pixel_sha256`, `config_hash` |
| `vkm_corpus.contracts.builders` | `SourceContext`, `ProducerContext`, `doc_envelope`, `build_row` |
| `vkm_corpus.contracts.site_scope` | таблица области источника (18 сырых значений) |
| `vkm_corpus.contracts.export` | экспорт в `schemas/corpus/` (`python -m vkm_corpus.contracts.export [--check]`) |
| `vkm_corpus.ids`, `vkm_corpus.ids.grammar` | грамматики, конструкторы и разбор ID |
| `vkm_corpus.registry` | реестр источников и курируемый реестр Work → REGISTRY-коммит |
| `vkm_corpus.parquet` | раскладка корня, писатель партиций, коммиты, прогоны, допуск, снимки, валидатор, GC |
| `vkm_corpus.duckdb` | сборка DuckDB из снимка; `duckdb/sql/*.sql` — все view, macro и производные правила |
| `vkm_corpus.testing` | фабрики валидных строк и синтетический канонический корень для тестов |

CLI: `vkm-corpus registry import|verify|propose-works`, `vkm-corpus canon init|status|admit|snapshot|validate|gc|
lease-break|unlock|schemas`, `vkm-corpus duckdb build|status`. Корень данных берётся только из
`vkm_corpus.config.load_settings()` (`VKM_DATA_ROOT`, `VKM_DATA_ROLE`, `VKM_RESOURCES_ROOT`).

## 2. Корень данных и его роль (H-07, H-32)

Маркер `.vkm_root.json` в корне задаёт `root_kind`: `STAGING` (WORKSTATION, пишет pipeline) или `CANONICAL` (CORE,
единственный канон). Снимки, `CURRENT`, допуск коммитов и сборка DuckDB работают только на `CANONICAL`; на `STAGING`
они отказываются (`RootError`). `VKM_DATA_ROLE=producer|canonical` обязан совпадать с маркером. Роль корня не меняется.

Раскладка (все пути в маркерах и manifest — POSIX относительно `canonical/`; ID никогда не становятся именами файлов):

```text
.vkm_root.json
canonical/
  <dataset>/run=<RUN>/part-00000.parquet                        реестровые датасеты (REGISTRY-коммит)
  <dataset>/source_id=<SID>/run=<RUN>/part-NNNNN.parquet        документные датасеты (коммит источника)
  processing_runs/run=<RUN>/{start,end}.parquet                 журналы прогона
  processing_steps|errors/run=<RUN>/part-NNNNN.parquet
  artifacts/run=<RUN>/{source_id=<SID>|scope=REGISTRY|scope=RUN}/part-NNNNN.parquet   индекс артефактов
  _commits/run=<RUN>/<KEY>__<CMT>.json                          маркеры коммитов, KEY = VKM-SRC-NNN | REGISTRY
  _runs/run=<RUN>/{START,END}.json                              маркеры прогонов
  _leases/<KEY>.lease                                           аренда ключа писателем (STAGING)
  _admission/<CMT>.json                                         решения допуска (CANONICAL)
  _snapshots/<snapshot_id>.json, _snapshots/candidates/, CURRENT       только CANONICAL
artifacts/<kind_dir>/<hh>/<hh>/<sha256>.<ext>                   content-addressed blobs
duckdb/vkm_corpus.duckdb                                        пересобираемая проекция (CANONICAL)
tmp/  cache/                                                    временные файлы и кеши (не канон)
```

Файлы данных неизменяемы: запись — во временный файл в `tmp/`, fsync (на Windows дескриптор `r+b`), `os.replace`.
Повторная запись тех же байтов — no-op, других байтов — ошибка.

## 3. Датасеты

| Датасет | Класс | PK | Модель |
|---|---|---|---|
| `sources` | REGISTRY_GLOBAL | `source_id` | `SourceRow` — 251 строка, включая 013 и 022 (CP-05) |
| `works` | REGISTRY_GLOBAL | `work_id` | `WorkRow` (tombstone вечны) |
| `source_work_links` | REGISTRY_GLOBAL | `object_id` (SWL) | `SourceWorkLinkRow` |
| `work_relations` | REGISTRY_GLOBAL | `object_id` (WRL) | `WorkRelationRow` |
| `source_relations` | REGISTRY_GLOBAL | `object_id` (SRL) | `SourceRelationRow` |
| `authors` | REGISTRY_GLOBAL | `author_id` | `AuthorRow` (кластер ключа имени) |
| `work_authors` | REGISTRY_GLOBAL | `object_id` (WAU) | `WorkAuthorRow` (`name_as_listed` обязателен) |
| `venues` | REGISTRY_GLOBAL | `venue_id` | `VenueRow` |
| `documents` | HEAD_PER_SOURCE | `object_id` = `<SID>:doc` | `DocumentRow` |
| `pages` | HEAD_PER_SOURCE | `page_id` | `PageRow` (строка на каждую страницу пагинации) |
| `blocks` | HEAD_PER_SOURCE | `object_id` | `BlockRow` (оба текстовых слоя; `is_primary_layer`) |
| `figures` | HEAD_PER_SOURCE | `object_id` | `FigureRow` |
| `tables` | HEAD_PER_SOURCE | `object_id` | `TableRow` |
| `formulas` | HEAD_PER_SOURCE | `object_id` | `FormulaRow` (без семантики переменных) |
| `bibliography_entries` | HEAD_PER_SOURCE | `object_id` | `BibliographyEntryRow` |
| `processing_runs` | APPEND_LOG | (`processing_run_id`, `record_phase`) | `ProcessingRunRow` (START и END) |
| `processing_steps` | APPEND_LOG | `step_id` | `ProcessingStepRow` |
| `errors` | APPEND_LOG | `error_id` | `ErrorRow` |
| `artifacts` | APPEND_LOG | (`artifact_id`, `created_by_run_id`) | `ArtifactRow` (индекс blob-ов) |
| `bibliography_links` | DERIVED_VIEW | `object_id` (BML) | `BibliographyLinkRow` — форма SQL-view, в Parquet не хранится |

Классы: REGISTRY_GLOBAL пересобирается одним REGISTRY-коммитом из PRIVATE `00_registry/`; HEAD_PER_SOURCE — в снимке
ровно одна партиция на источник (файлы head-коммита); APPEND_LOG — история всех прогонов; DERIVED_VIEW — правило
SQL поверх снимка.

Типы Arrow: строки, `int16/int32/int64` (голый `int` запрещён тестом), `double`, `bool`, `date32`,
`timestamp[us, tz=UTC]` (только aware UTC), `list<…>`, `struct<…>`. Перечисления хранятся строками. `null` — значения
нет или UNKNOWN; списки никогда не null. Nullable в Arrow ⇔ аннотация допускает `None`; писатель сам проверяет null в
обязательных колонках (pyarrow этого не делает), уникальность PK и сортирует строки по ключу сортировки.
Key-value метаданные каждого файла: `vkm.dataset`, `vkm.schema_version`, `vkm.schema_fingerprint`,
`vkm.pipeline_version`, `vkm.processing_run_id`, `vkm.source_id`.

## 4. Провенанс-конверт

**DOC** (документные объекты, постановка §13 + CP-16): `schema_version, object_id, object_kind, source_id, page_id,
source_sha256, source_site_scope, source_site_scope_raw, source_site_scope_mapping, origin, pipeline_version,
processing_run_id, extractor_id, extractor_version, extraction_generation, model_id, model_revision, models[],
config_hash, raw_config_hash, extraction_signature, raw_content_sha256, content_sha256, raw_artifact_id,
raw_artifacts[], created_at, review_status, quality_flags`. У объектов страницы добавляются `region_origin`,
`text_layer`, `bbox_x0..bbox_y1`, `bbox_space`, `docx_paragraph_path`.

- `origin` ∈ `NATIVE` (собственные структуры файла), `EMBEDDED_OCR` (чужой OCR-слой скана: невидимый текст PDF, TXTz
  DjVu — никогда не NATIVE, H-02), `OCR` (наша модель; `model_id`/`model_revision` и RECOGNITION в `models[]`
  обязательны), `DERIVED`, `REGISTRY`, `CURATED` (в v0 не пишется). Если хоть часть содержимого сделала модель,
  объект — `OCR`.
- `text_layer` подтверждает `origin`: `PDF_TEXT_LAYER`/`EPUB_XHTML`/`DOCX_XML` → NATIVE,
  `PDF_EMBEDDED_OCR_LAYER`/`DJVU_EMBEDDED_OCR_LAYER` → EMBEDDED_OCR, `GLM_OCR` → OCR. Для страниц-сканов
  (`page_class = RASTER_SCAN`) и страниц DjVu NATIVE-текста не бывает.
- `region_origin` — чем порождён регион; `LAYOUT_MODEL` ⇒ модель с ролью LAYOUT в `models[]` (H-03). `models[]`
  перечисляет модели, породившие регион или содержимое; `model_id/model_revision` — модель содержимого.
- `raw_artifact_id` — главный сырой выход (строка всегда canonical); `raw_artifacts[]` — все сырые выходы с ролью
  (`NATIVE_TABLE_FINDER`, `LAYOUT_DETECTIONS`, `OCR_RESPONSE`…, H-24).
- `content_sha256` — хеш всех полей вне конверта (`builders.content_sha256`); `raw_content_sha256` — хеш сырого
  содержимого, инвариант для данного `object_id` во всех снимках (проверка B07).
- `review_status` документных объектов — всегда `AUTO_EXTRACTED_UNREVIEWED`; FACT, REVIEWED_MEASUREMENT,
  ACCEPTED_PARAMETER, ACCEPTED_FORMULA и эпистемические статусы непредставимы (закрытые enum) и ловятся валидатором.
- `source_site_scope*` — область всего источника, не объекта (H-18); копируется из строки `sources` и сверяется
  валидатором (D08). Флаг `SCOPE_INHERITED_FROM_SOURCE` — для индекса и API.

**REG/LINK** (реестр): `object_id, object_kind, origin, pipeline_version, processing_run_id, extractor_id,
extractor_version, config_hash, content_sha256, created_at, review_status, quality_flags, input_ref, input_sha256,
input_row`; у связей ещё `curation_status, basis, notes`. `input_ref` — логический путь (`PRIVATE:…`, `PUBLIC:…`).
`review_status`: у источников — по CP-06, у Work, авторов, venue и курируемых связей — `NOT_APPLICABLE` (H-25, H-28).

**LOG**: прогоны, шаги, ошибки, индекс артефактов — поля см. модели; ошибка несёт `code, stage, tool, message,
retryable, source_id, page_id, log_ref` (постановка §44).

Ответы на вопросы §50 даёт macro `provenance_trace(object_id)` (раздел 10); валидатор проверяет его на выборке каждого
класса (T01).

## 5. Словари

Полный список значений — [vocabularies.json](../../schemas/corpus/vocabularies.json). Главное:

- `ProcessingStatus`: NATIVE_OK, EMBEDDED_TEXT_OK, OCR_REQUIRED, OCR_OK, PARTIAL, UNSUPPORTED, FAILED, NEEDS_REVIEW,
  SKIPPED_BY_REGISTER (только шаги и источники), NOT_PROCESSED. Страница: NATIVE_OK ⇔ основной текст NATIVE,
  EMBEDDED_TEXT_OK ⇔ EMBEDDED_OCR, OCR_OK ⇔ OCR.
- `SourceProcessingStatus` (свёртка): COMPLETE, PARTIAL, NEEDS_REVIEW, FAILED, UNSUPPORTED, NOT_PROCESSED,
  SKIPPED_BY_REGISTER; сумма классов = число источников (251).
- `LifecycleStatus`: ACTIVE, ABSENT_BY_REGISTER (013, `ARCHIVE_DELETED_AFTER_ASSEMBLY`), RETIRED (022,
  `RETIRED_NOT_EVIDENCE`); неактивный источник ⇔ `register_skip_status = SKIPPED_BY_REGISTER` ⇔ причина ⇔
  `review_status = NOT_APPLICABLE`.
- `Stage` — объединение стадий C и D с CLASSIFY и LAYOUT; `ErrorCode` поглощает коды C `E_*` по
  `C_ERROR_CODE_MAP` (`E_RETIRED_ARCHIVE` больше не ошибка — это SKIPPED_BY_REGISTER) и содержит 46 кодов проекций
  агента E со значениями `E_*` как есть (`PROJECTION_ERROR_CODES`; тест собирает литералы `E_*` из
  `vkm_corpus/graph` и `vkm_corpus/search`).
- `ArtifactKind` включает LAYOUT_RAW, OCR_RAW, OCR_INPUT, RUN_LOG, RUN_PLAN, VECTOR_PATHS_JSON, VECTOR_SVG и CAD-виды;
  REPAIRED_COPY нет (H-37). `RetentionClass`: KEEP_RAW (сырьё, конфиги, логи, отчёты — никогда не удаляются
  автоматически) и KEEP_REFERENCED. `Materialization`: STORED или NOT_STORED_REPRODUCIBLE (только PAGE_RENDER,
  NATIVE_RAW, VECTOR_SVG и только с `recipe`, H-23).
- `BboxSpace`: PAGE_PT_TL, IMAGE_PIXEL, DRAWING_UNITS, GEO, NONE; `crs_status` обязателен для GEO и DRAWING_UNITS и
  запрещён иначе; объекты страниц — только PAGE_PT_TL или NONE; выдуманного EPSG нет (H-20).
- `QualityFlag` закрыт; у каждого флага своя область датасетов (`QUALITY_FLAG_SCOPE`, проверка E06).
- `MatchStatus`: CANDIDATE, AUTO_EXACT_ID_MATCH, REJECTED (H-29). `IdentityStatus` автора — только NAME_KEY_ONLY
  (H-15).

## 6. Идентификаторы

| ID | Грамматика | Как строится |
|---|---|---|
| источник | `VKM-SRC-NNN` | реестр |
| документ | `VKM-SRC-NNN:doc` | `ids.document_id(sid)` |
| страница | `VKM-SRC-NNN:<p\|r\|s>NNNN` | `ids.page_id(sid, unit, index)`: `p` — физическая страница PDF/DjVu (= `pdf_page` evidence), `r` — страница закреплённого рендера DOCX 023, `s` — spine-элемент EPUB 249; индекс с 1 |
| объект страницы | `<page_id>:<b\|f\|t\|m\|c><12 hex>` | b блок, f рисунок, t таблица, m формула, c запись библиографии |
| объект DOCX | `VKM-SRC-NNN:doc:<kind><12 hex>` | объекты из XML DOCX (`region_origin = DOCX_ELEMENT`, H-50) |
| Work | `VKM-WRK-NNN` | номер якорного (наименьшего) источника группы |
| автор / venue | `AUT-<12 hex>` / `VEN-<12 hex>` | ключ имени / ISSN-L или нормализованное заглавие |
| артефакт | `sha256:<64 hex>` | sha256 байтов |
| прогон | `RUN-<UTC>-<8 hex>` | `ids.new_run_id()` (событие, не детерминирован) |
| шаг / ошибка / коммит | `STP-`, `ERR-`, `CMT-` + 16 hex | хеш составного ключа / тела маркера |
| снимок | `snap-<UTC>-<8 hex>` | время + хеш manifest |
| связи | `SWL-`, `WRL-`, `SRL-`, `WAU-`, `BML-`, `OLN-` + 16 hex | sha256 от `<PREFIX>-v1\|<части ключа>` |

**ID объекта** (`ids.object_id`) = хеш (`vkm-docobj-v2`, область, вид, `origin`, `region_origin`, якорь,
`producer_key`, `dup`). Якорь:

- `bbox_anchor(x0, y0, x1, y1)` — регион в PAGE_PT_TL, квант 0,1 pt (шум float не меняет ID);
- `xml_anchor(docx_paragraph_path)` — объекты DOCX (область — документ; `page_id` строки = страница рендера,
  RENDER_DEPENDENT);
- `ordinal_anchor(reading_order, text)` — объекты без геометрии и пути (EPUB): порядок в документе + хеш сырого
  текста.

`producer_key = sha256(extractor_id | extraction_generation | models | raw_config_hash)` (H-14): версии ПО в ID не
входят (они в конверте); `models` — модели, породившие регион или содержимое (ровно список `models[]` строки);
`raw_config_hash` — часть конфигурации, влияющая на сырой выход. Смена `extraction_generation` — осознанная смена
смысла: новые ID и строки `object_lineage`. Одинаковые якоря одного производителя различает `dup:n`
(`ids.ObjectIdAllocator`, флаг `DUPLICATE_DETECTION_DISAMBIGUATED`); коллизия разных якорей — ошибка
`ID_COLLISION`. Валидатор пересчитывает ID из строк (B04).

`author_id` навсегда означает кластер ключа имени «фамилия|инициалы|полные имена|письменность» (NFKC, casefold, ё→е);
разные письменности и «инициалы vs полные имена» не сливаются; человек — будущий `person_id`.

## 7. Текстовые правила (H-04)

- `normalize_text_v1(text)`: NFC; CR/LF; мягкие переносы, zero-width и управляющие символы удаляются; лигатуры
  раскрываются; перенос «буква-\nстрочная» склеивается; все пробелы и переводы строк → один пробел. Буквы, регистр,
  «ё» и U+FFFD сохраняются. Агент C применяет его к `blocks.normalized_text` после своих ремонтов (CP1251 и т. п.).
- `page_text_v1(blocks_одной_страницы)`: только `is_primary_layer`, порядок `(reading_order, object_id)`, без
  `PAGE_HEADER`, `PAGE_FOOTER`, `PAGE_NUMBER`, разделитель `\n\n`; возвращает `normalized_text`, `text_sha256`
  (sha256 UTF-8), `char_count`, `block_ids`. Валидатор пересчитывает текст каждой страницы (E12).
- `rerank_text_v1(kind, row)`: страница, блок, запись библиографии — `normalized_text`; рисунок — метка + нормализованная
  подпись; таблица — метка, подпись, текст таблицы построчно; формула — номер + `normalized_latex` (иначе сырой вывод,
  если это не IMAGE_ONLY). SQL-view `rerank_text` реализует то же правило (тест сравнивает) и — единственный источник
  текста для E, F, G.

Любое изменение поведения — новый идентификатор правила (`page_text_v2`), не правка v1.

## 8. Подписи и кеш (H-05)

- `stage_signature(source_sha256, stage, pipeline_version, extractor_id, extractor_version, stage_config_hash,
  page_unit, page_index, extraction_generation, models, input_artifact_ids)` решает, пересобирать ли строки стадии.
- `call_signature(model_id, model_revision, weights_sha256, prompt, sampling, input_pixel_sha256)` — ключ кеша вызова
  модели; бэкенд и его версия записываются, но в ключ не входят. `pixel_sha256(mode, w, h, buffer)` — хеш
  декодированных пикселей (не зависит от кодека PNG).
- Сырой выход модели — content-addressed blob с `producer_signature = call_signature` и `attempt` в индексе.
- `--force` означает одно: пересчитать строки, используя кеш модели. Повторный вызов модели — отдельный путь через
  `--plan-only` (агенты C, G).

## 9. Запись: коммиты, прогоны, допуск, снимки

1. **Прогон.** `parquet.runs.RunRecorder(...).start()` пишет START (`processing_runs/.../start.parquet`,
   `_runs/run=<RUN>/START.json`, артефакт RUN_CONFIG); журналы — `add_steps`, `add_errors`, `add_artifacts`; `end()`
   пишет END со статусом, счётчиками, артефактом RUN_LOG и списком всех файлов. Прогон без END виден в снимке как падение
   (`processing_runs.has_end = false`), его части журналов находятся в его каталогах (H-11).
2. **Коммит источника.** Под арендой ключа (`acquire_lease(layout, source_id, run_id)`) вызвать
   `commit_source(layout, source_id=, source_sha256=, run_id=, tables={все 7 документных датасетов, пустые тоже},
   artifact_rows=[строки индекса всех артефактов, на которые ссылаются строки])`. Коммит всегда пересобирает источник
   целиком. Родитель — текущая голова ключа (или явный `parent_commit_id`; чужой родитель → `CommitConflict`).
   Если дайджесты содержимого равны родителю — no-op, маркер не пишется (H-34). Реестр коммитится так же
   (`commit_registry`, ключ `REGISTRY`).
3. **Публикация** STAGING → CANONICAL: rsync без `--inplace`, blob-ы и партиции первыми, маркеры последними.
4. **Допуск** (`vkm-corpus canon admit`, H-09): формат и `commit_id`, sha256 и размер каждой партиции, известная коду
   версия схемы и fingerprint, существование и sha256 каждого нового blob-а, родитель = допущенная голова, sha256
   источника = реестр. Отклонённый коммит (COMMIT_CONFLICT, SCHEMA_UNKNOWN, ARTIFACT_MISSING, …) не блокирует остальные;
   коммит без опубликованного родителя ждёт.
5. **Снимок** (`vkm-corpus canon snapshot`): manifest из допущенных голов и журналов всех прогонов, дайджесты,
   валидация, отчёт-артефакт VALIDATION_REPORT; `CURRENT` меняется атомарно только при PASS, иначе manifest ложится в
   `_snapshots/candidates/`.
6. **GC** (`vkm-corpus canon gc [--delete]`): партиции без маркеров и старые временные файлы — только старше 24 ч; blob-ы
   не трогаются.

Отпечатки: `RowDigest` = (число строк, сумма sha256 канонических JSON-строк mod 2^256) — аддитивен по файлам;
`fingerprint_of(dataset, digest)` связывает имя датасета, набор колонок и число строк. `content_*` исключает
`created_at` и `processing_run_id`. Тем же алгоритмом сборка DuckDB сверяет каждую материализованную таблицу с
manifest (H-48).

## 10. DuckDB

`vkm-corpus duckdb build` материализует `canonical.<dataset>` строго по файлам manifest (`INSERT … BY NAME` из
`read_parquet([...], union_by_name = true)` в таблицу, созданную по Arrow-схеме контракта: колонки старых файлов →
NULL, неизвестные колонки → ошибка), выполняет `sql/*.sql`, сверяет отпечатки и только затем заменяет файл. На
STAGING — отказ. `meta.snapshot` и `meta.commits` говорят, из какого снимка собран файл; `duckdb status` сравнивает с
`CURRENT`.

View: `sources, works` (ACTIVE), `works_all, documents, pages, blocks, figures, "tables", formulas,
bibliography_entries, source_work_links, work_relations` (CURATED), `source_relations` (CURATED), `authors,
work_authors, venues, artifacts` (одна строка на blob), `processing_runs` (+`has_end`), `processing_steps, errors,
all_objects, page_coverage, processing_status, source_status_summary, corpus_counts, rerank_text, works_availability`.
Macro: `provenance_trace(oid)`, `registry_trace(oid)`, `objects_on_page(pid)`, `objects_by_source(sid)`,
`resolve_work(wid)`.

**Производные правила — только здесь** (H-17; проекторы Neo4j и OpenSearch выполняют эти же файлы через
`duckdb.build.attach_manifest` + `apply_sql`), у каждой производной строки `rule_version`:

| View | Правило |
|---|---|
| `work_sources` | Source INSTANCE_OF Work: все основные (`is_primary`) связи FULL_COPY, PARTIAL_COPY, FRONT_MATTER_ONLY, PART независимо от lifecycle источника (CP-25; 013 тоже), никогда FOREIGN_CONTENT (H-16); `canonical_row_id`, `lifecycle_status` |
| `foreign_content_pages` | страницы с чужим содержимым → Work (или NULL, если работа не опознана) |
| `bibliography` (`citing_work_v1`) | запись на странице с FOREIGN_CONTENT никогда не цитирует от имени хоста (`citing_work_id` NULL); единственный кандидат → UNIQUE_LINK; иначе AMBIGUOUS; `citing_work_is_container` (H-49) |
| `bibliography_links` (`bibliography_match_v1`) | DOI/ISBN → AUTO_EXACT_ID_MATCH; нормализованное заглавие + год → CANDIDATE |
| `cites` (`cites_v1`) | Work CITES Work только из AUTO_EXACT_ID_MATCH с известным цитирующим Work; `n_citing_entries`, `n_citing_sources`; цитирование ≠ согласие |
| `page_sequence` (`page_sequence_v1`) | порядок страниц внутри источника |
| `duplicate_page_candidates` (`duplicate_pages_v1`) | страницы разных источников с одинаковым `text_sha256` (≥ 200 символов), `dup_group_id`; только кандидаты |
| `work_copy_counts` | копии Work: `n_sources_total` (все зарегистрированные), `n_sources_active` (файл доступен) и `work_copy_count` = `n_sources_active` (две копии ≠ два свидетельства, H-49, CP-25) |
| `works_availability` | `available_latest_day` + `available_basis` ∈ UNKNOWN, CURATED, ASSUMED_FROM_PUBLICATION (H-19); в каноне `available_from` курируется, NULL = UNKNOWN |

`corpus_counts`: `sources_total` = COMPLETE + PARTIAL + NEEDS_REVIEW + FAILED + UNSUPPORTED + NOT_PROCESSED +
SKIPPED_BY_REGISTER (`rollup_closed`), счётчики страниц, рисунков, таблиц, формул и записей библиографии (§48).

Замечание: `fetchall()` над TIMESTAMPTZ требует `pytz`; используйте `.to_arrow_table()` или `::VARCHAR`.

## 11. Валидатор канона

`vkm-corpus canon validate [--deep] [--acceptance] [--expected-sources 251]`; тот же валидатор — внутри `snapshot`.

| Группа | Проверки |
|---|---|
| A файлы | A01 sha256 и размер файлов manifest; A02 KV-метаданные и известная схема; A03 одна head-партиция на (датасет, источник); A05 отпечатки = сумма дайджестов |
| B ключи | B01 уникальность PK; B02 грамматики ID; B03 page_id ↔ page_kind/page_index, object_id ↔ page_id; B04 пересчёт ID объектов; B07 тот же ID ⇒ тот же `raw_content_sha256`, что в родительском снимке |
| C ссылки | C01–C08: страницы и документы → источники; объекты → страницы своего источника; подписи, продолжения, блоки записей; все `*_artifact_id` в индексе; прогоны START (END — WARN); FK связей; цепочки MERGED_INTO |
| D провенанс | D01 нет NULL в обязательных колонках (и у старых файлов); D02 OCR ⇒ модель, LAYOUT_MODEL ⇒ модель LAYOUT; D03 сырьё у NATIVE/EMBEDDED_OCR/OCR; D04 sha256 строк и маркеров = реестр; D06 время в окне прогона (WARN); D08 область объектов = область источника |
| E наука | E01 нет FACT/ACCEPTED_*/эпистемических статусов; E02 авто-объекты AUTO_EXTRACTED_UNREVIEWED, реестровые NOT_APPLICABLE; E03 review источников по CP-06; E04 тип рисунка только при уверенности ≥ порога; E05 `crs_status` только GEO/DRAWING_UNITS; E06 флаги из словаря и по области; E07 нет колонок L2; E08 группы Work только из CURATED-связей; E09 чужие страницы не цитируют; E10 таблица области; E11 нет NATIVE-текста на сканах и DjVu; E12 текст страницы = `page_text_v1` |
| F полнота | F01 число источников (251 в приёмке); F02 у каждого присутствующего ACTIVE-источника есть head-коммит (WARN, в `--acceptance` блокирует); F03 `page_count` = строкам `pages`, индексы 1..N; F05 у FAILED/UNSUPPORTED/PARTIAL-страниц есть ошибка; F06 у ACTIVE-источника без проверенного файла есть ошибка SOURCE_*; F07 одна главная связь на источник, у ACTIVE Work есть экземпляр; F08 свёртка замкнута; F09 у FAILED-шага есть ошибка; F10 blob-ы существуют (`--deep`: и хешируются в свой ID) |
| G гигиена | G01 запрещённые имена колонок (`quote`, `ocr_text`, `page_text`, …); G02 пути относительные/логические; G03 нет абсолютных путей в сообщениях ошибок (WARN) |
| T §50 | T01 `provenance_trace` отвечает без NULL в обязательных полях на выборке каждого вида объектов |

## 12. Реестр источников и Work

`vkm-corpus registry import` (STAGING): `SOURCE_REGISTER.csv` читается как utf-8-sig, сырые поля сохраняются
дословно (`source_class_raw`, `site_scope_raw`, `register_*`), файлы проверяются параллельно (sha256 с кешем по пути,
размеру и mtime в `cache/`), формат — по сигнатуре (`%PDF` с допуском ведущих байтов → флаг
`LEADING_BYTES_BEFORE_HEADER`, `AT&TFORM`, ZIP с `mimetype` EPUB или `word/document.xml`). Правила:

- lifecycle (CP-05): `ORIGINAL_ARCHIVE_DELETED_AFTER_PDF_ASSEMBLY` → ABSENT_BY_REGISTER; `RETIRED_FROM_CURRENT_RESEARCH`
  или область `LEGACY_RETIRED` → RETIRED;
- review (CP-06): 001–041 — из покрытия Phase 1 (`PUBLIC:evidence/sources/SOURCE_COVERAGE_MASTER.csv`: FULLY →
  FULLY_REVIEWED, RELEVANT → RELEVANT_SECTIONS_REVIEWED; основание — покрытие чтением, а не проверка объектов), 042–195 —
  UNSEEN, 196–251 — QUICK_LOOK_ONLY (маркер быстрого просмотра в notes обязателен), неактивные — NOT_APPLICABLE;
- область (CP-07): таблица `contracts.site_scope` покрывает все 18 сырых значений; AMBIGUOUS (002, 012, 045) и
  NOT_A_SCOPE дают `site_scope = []`, кандидаты — в `site_scope_candidates` (не для фильтров); имена контракта §46 —
  только crosswalk (`SKRU1_EXACT` ≙ SKRU1, `ANALOG` ≙ NON_VKM_ANALOG → NON_VKM);
- у ACTIVE-источника без проверенного файла прогон импорта пишет ошибку `SOURCE_*`; 013/022 получают шаг
  SKIPPED_BY_REGISTER с причиной.

Курируемый реестр Work — PRIVATE `00_registry/work_registry/` (для тестов — `VKM_WORK_REGISTRY_DIR`):

- `WORK_REGISTER.csv` — строка на `work_id` (tombstone тоже): `work_id, status, merged_into, anchor_source_id,
  work_type, title, title_en, title_variants, authors, year_raw, year, venue, venue_type, volume, issue, pages, edition,
  publisher, city, doi, isbn, language, external_ids, basis_title, basis_authors, basis_year, basis_venue,
  basis_identifiers, identity_status, available_from, available_from_precision, available_from_basis,
  curation_status, curated_by, curated_at, curation_receipt, notes`. Списки — через `;`; автор — `Имя` или
  `Имя|ROLE`; внешний ID — `SCHEME:value:RELATION`.
- `WORK_LINKS.csv`: `link_kind, from_id, relation, to_id, from_page_start, from_page_end, to_page_start, to_page_end,
  part_label, printed_range, is_primary, basis, curation_status, notes`. Сигнатуры: SOURCE_WORK (источник → Work;
  `SourceWorkLinkType`; пустой `to_id` только у FOREIGN_CONTENT), WORK_WORK (Work ↔ Work; `WorkRelationType`,
  симметричные хранятся один раз с `from < to`), SOURCE_SOURCE (источник ↔ источник; `SourceRelationType`).

Crosswalk названий CP-09 / аудита A §2.8 со словарём: SAME_WORK — несколько источников с FULL_COPY/PARTIAL_COPY/
FRONT_MATTER_ONLY на один Work; PART_OF — связь PART; VOLUME_OF → VOLUME_SET_SIBLING; SERIES_PART → SERIES_SIBLING;
COMPANION → COMPANION_OF; CONTAINS_COPY → CONTAINS_COPY_OF; ABSTRACT_OF, EDITION_OF, NOT_SAME — без изменений.
Группа из нескольких источников допускается только из CURATED-связей (E08); автоматически — только «файл = своя
работа» (BOOTSTRAP_SINGLETON). Каноническая сборка `work_id` не выдумывает.

Черновик реестра Work готовит `vkm-corpus registry propose-works --out <каталог>`: группы аудита A §2.8 — CURATED
(основание REPOSITORY_AUDIT), страницы с чужой библиографией из evidence Phase 1 — CURATED, выведенные из общих страниц
«Горного эха» — AUTO_PROPOSED, одиночки — AUTO_PROPOSED; метаданные — покрытие Phase 1 → intake-манифест → таблица
разведки (только `acquired_2026_09_27` или первый токен `ALREADY_LOCAL:`) → notes реестра; неоднозначности — в
`AMBIGUITIES.json`, входы и хеши — в `RECEIPT.json`. Черновик курирует человек/координатор и коммитит в PRIVATE.

## 13. Как пользоваться (коротко)

- **C (pipeline):** строки собирать через `SourceContext.from_source_row(source_row)`, `ProducerContext(...)
  .with_models(...)`, `doc_envelope(...)`, `build_row(dataset, env, **fields)`; ID — `ids.page_id`,
  `ids.bbox_anchor`/`xml_anchor`/`ordinal_anchor`, `ProducerContext.producer_key()`, `ids.object_id` (или
  `ObjectIdAllocator`); текст страницы — `page_text_v1`; подписи — `signatures`; коммит — `acquire_lease` +
  `commit_source`; журнал — `RunRecorder`.
- **E (граф, поиск):** читать снимок через `vkm_corpus.duckdb.build.open_snapshot(layout)` (или готовый файл DuckDB),
  производные рёбра брать только из view раздела 10, текст — только из `rerank_text`.
- **G (API, MCP):** гидратация — DuckDB read-only; происхождение — `provenance_trace`/`registry_trace`;
  `object_version = content_sha256@commit_id`; сырьё — через `raw_artifact_id` и индекс `artifacts`.
- **Тесты:** `vkm_corpus.testing.rows` (валидные строки) и `vkm_corpus.testing.synthetic_canon(root, with_duckdb=True)`
  (синтетический CANONICAL-корень с CURRENT, ≈3 с).

# AGENT D — канонический структурированный слой (Arrow/Parquet + DuckDB): дизайн

Дата: 28.09.2026. Роль: DATA / PARQUET / DUCKDB ENGINEER, Phase 0 (design), репозитории только читались.

- PUBLIC: ветка `claude/corpus-platform-v0-2026-09-28` = `main` `f461fb6`.
- PRIVATE: `main` `c575a07`.
- Входы:
  - бриф координатора;
  - постановка (§9–16, §20–27, §38, §44, §46, §48–50, §57, §64);
  - контракт данных v0.2 (L0–L4, §5–12, §36–42, §46–47, §51–53, §58, §61) и framework v0.2 (бегло);
  - код `vkm_world` (`core`, `evidence`, `governance/leakage.py`), политики `docs/governance/`, WorldSpec vNext;
  - PRIVATE: `SOURCE_REGISTER.csv`, литературная разведка 27.09, intake-манифесты, `11_evidence_vnext`.
- Учтены [аудит A](AGENT_A_REPO_CONTRACT_AUDIT.md) (§2.4, §2.5, §2.8, §5, §7 п. 6–14, §8) и
  [решения координатора](COORDINATOR_DECISIONS.md) CP-01…CP-13. Где я расхожусь с A, это сказано явно (§0.3).
- Пробы: одноразовый venv в `<PUBLIC>/work/corpus_platform/phase0/agent_d/` (git-ignored), только синтетические
  данные. Приватные файлы только читались: подсчёт страниц PDF и структура EPUB. Единственная запись в репозитории —
  этот файл.

Обозначения путей: `<PUBLIC>`, `$VKM_RESOURCES_ROOT` (PRIVATE), `$VKM_DATA_ROOT` (runtime-данные), `$VKM_WORK`.

## 0. Итог

### 0.1 Решения в 12 пунктах

1. **20 датасетов, типизированные связи вместо «одной таблицы с JSON» (контракт §61).**
   - Реестровые (8): `sources`, `works`, `source_work_links`, `work_relations`, `source_relations`, `authors`,
     `work_authors`, `venues`.
   - Документные, по одному файлу на источник (7): `documents`, `pages`, `blocks`, `figures`, `tables`, `formulas`,
     `bibliography_entries`.
   - Производный (1): `bibliography_links`.
   - Журналы (4): `processing_runs`, `processing_steps`, `errors`, `artifacts`.
   - Зарезервированы, в v0 не создаются: `object_lineage`, `review_events`, `source_members`.
2. **Физическая модель.**
   - Неизменяемые Parquet-партиции `<dataset>/source_id=<SID>/run=<RUN>/part-00000.parquet`.
   - Коммит источника — JSON-маркер со списком всех его файлов. Снимок корпуса — manifest плюс атомарный указатель
     `CURRENT`.
   - Повторная обработка источника атомарно заменяет все его датасеты сразу. «Висящих» объектов не остаётся, история
     сохраняется.
3. **Provenance-конверт — плоские колонки в каждой строке**, не вложенный JSON (§2).
   - Три профиля: DOC, REG, LINK.
   - `origin` ∈ NATIVE | OCR | DERIVED | REGISTRY | CURATED.
   - Строка Parquet всегда canonical; raw-выход модели — только артефакт `raw_artifact_id`.
4. **ID (§4).**
   - `page_id = VKM-SRC-NNN:pNNNN` — физический индекс с 1, совпадает с `pdf_page` evidence.
   - `rNNNN` — страница закреплённого рендера (DOCX 023), `sNNNN` — spine-элемент (EPUB 249).
   - Объекты: `<page_id>:<b|f|t|m|c><12 hex>`. Якорь — регион (страница, вид, origin, bbox) плюс ключ
     производителя (экстрактор или модель и их версии). Идентификатор version-aware; `content_sha256` хранится
     рядом.
   - `work_id = VKM-WRK-NNN` от якорного источника (CP-09), слияния — tombstone.
   - Артефакты: `sha256:<hex>`.
   - Прогоны: `RUN-<UTC>-<8 hex>`.
5. **Словари закрыты (§3).**
   - `review_status`: минимум §47 плюс `NOT_APPLICABLE` для 013 и 022. Авто-объекты — только
     `AUTO_EXTRACTED_UNREVIEWED`. FACT, REVIEWED_MEASUREMENT, ACCEPTED_PARAMETER и ACCEPTED_FORMULA валидатор
     отвергает в любой колонке статуса.
   - Статусы обработки: минимум §44 плюс `NOT_PROCESSED` и `SKIPPED_BY_REGISTER` (CP-05).
6. **Версии (§5).**
   - semver на датасет: в key-value метаданных Parquet и в строке, с fingerprint Arrow-схемы.
   - `0.x` до заморозки на полном прогоне, затем `1.0.0`.
   - Изменения только аддитивные. Миграции — пересборкой из raw, без мутации.
7. **DuckDB (§9).**
   - `vkm_corpus.duckdb` пересобирается из manifest.
   - Базовые таблицы материализуются: в пробе запросы быстрее в 20 раз, чем view поверх 1510 файлов. Интерфейс —
     view и macro.
   - SQL проверен на DuckDB 1.5.5 на синтетике с пограничными случаями.
8. **Pydantic — единственный источник истины схемы (§10).**
   - Из него детерминированно генерируются Arrow-схемы, fingerprints и JSON Schema в `schemas/corpus/`.
   - Экспорт — модулем `python -m vkm_corpus.contracts.export`: `scripts/*` не меняются по CP-01.
9. **Валидатор канона (§11)** — 7 групп, около 45 проверок. Блокирующий FAIL не даёт сдвинуть `CURRENT`.
10. **Work ≠ Source.**
    - Курируемые файлы связей лежат в PRIVATE `00_registry/work_registry/` (CP-09).
    - Автоматические связи — только кандидаты, кроме «один файл — одна работа по умолчанию».
    - Страницы с чужим содержимым («Горное эхо») не приписывают библиографию Work хоста.
11. **Библиографические метаданные Work.** Таблица разведки и покрытие Phase 1 вместе дают заглавие для 246 из 251
    источников. Оставшиеся 5 закрываются заметками реестра (§13.2).
12. **Время (§7).** В документном слое есть только `created_at` (processing_time), `ingestion_date`,
    `publication_*` и курируемый `available_from`. Эвристика D-03 применяется только во view с меткой.
    `event_time` и `measurement_time` в L1 запрещены.

### 0.2 Версии и результаты проб

Окружение: CPython 3.13.13, Windows 11; pyarrow **25.0.1**, polars **1.44.2**, duckdb **1.5.5**, pydantic
**2.13.5** / pydantic-core **2.46.5** (как в `worldspec.lock.txt`). Для подсчёта страниц — pypdf 6.19.0. pyarrow и
duckdb совпадают с freeze Phase 1 (аудит A §7 п. 3).

| Проба | Результат |
|---|---|
| Детерминизм записи | 2 независимые записи одних строк (251 синтетический источник, 1510 файлов) дали побайтово одинаковые файлы и одинаковый `snapshot_id` |
| Метаданные Parquet | key-value `vkm.*` читаются и pyarrow, и `parquet_kv_metadata()` DuckDB. `created_by = parquet-cpp-arrow version 25.0.1`: временных меток в файле нет |
| Типы при чтении | polars: `Datetime(us, UTC)`, `List(String)`, `Float64`, `List(Struct)`. DuckDB: `TIMESTAMP WITH TIME ZONE`, `VARCHAR[]`, `STRUCT(...)[]` |
| View поверх 1510 файлов | 21 проверка и macro — 4,2 с |
| Материализованные таблицы | сборка 6,2 с, те же запросы 0,21 с (в 20 раз быстрее). Решение: материализация по умолчанию (§9) |
| Сканирование 251 файла | polars `scan_parquet` — 21 мс, `count(*)` в DuckDB — 28 мс |
| SQL view и macro §9 | работают на 1.5.5, включая рекурсивный macro `resolve_work`, `QUALIFY`, `UNION ALL BY NAME`, view `"tables"` |
| Пограничные случаи §9 | чужие страницы, цепочки tombstone, SKIPPED_BY_REGISTER, провал повтора при хорошем прежнем результате — результаты верные |
| Страж утечки | JSON Schema 10 моделей: запрещённых ключей нет (проверено `leakage._json_keys`). Контрольная модель с полем `page_text` поймана |
| Защита pydantic | отвергнуты FACT, авто-объект с FULLY_REVIEWED, OCR без модели, naive timestamp, неизвестный quality flag |

Ловушки, найденные пробами (учтены в дизайне):

- **DuckDB `read_parquet([...])` без `union_by_name`** берёт схему первого файла и **молча отбрасывает** лишние
  колонки остальных. Поэтому всегда `union_by_name = true` плюс проверка fingerprint (§6, §9).
  - polars по умолчанию на лишнюю колонку падает — это безопасно; нужно передавать `schema=` с `missing_columns`.
- **`pa.Table.from_pylist(..., schema)` принимает null в non-nullable полях**, и `validate(full=True)` проходит.
  Отказывает только writer Parquet, и с худшей диагностикой. Поэтому писатель проверяет `null_count` до записи.
- **Windows:** `os.fsync` на дескрипторе, открытом `"rb"`, падает с EBADF. Нужен `"r+b"`.

Масштаб корпуса:

- PDF — 23 105 страниц: pypdf, 238 файлов, максимум 871, медиана 13, 90-й перцентиль 300. У 63 PDF есть
  `/PageLabels`, у 4 в первых 50 страницах встречается `/Rotate`, 1 файл зашифрован.
- DjVu — 2870 страниц (аудит A §2.9: у 053 их 384, а не 423).
- EPUB 249 — 391 spine-элемент; EPUB 2.0, `page-list` нет.
- DOCX 023 — 115 страниц рендера.
- Итого ≈ **26 480 единиц-страниц**. 4-значного индекса хватает.
- Проверка локаторов Phase 1: у всех 39 документов evidence максимальный `pdf_page` не превышает числа физических
  страниц (у 037 — 236 DjVu-страниц, у 023 — 115 страниц рендера). Это подтверждает crosswalk `pdf_page → page_id`.

### 0.3 Где я расхожусь с аудитом A (аргументы)

| Тема | A | D | Почему |
|---|---|---|---|
| ID объектов внутри страницы | `page_id` + версия экстрактора + порядковый номер | `page_id` + вид + origin + **bbox** + ключ производителя; ординал только без bbox (EPUB, DOCX) | Ординал зависит от алгоритма порядка чтения. Его улучшение сдвинуло бы все ID при неизменных регионах. Урок `vn_index` A о том же: не перенумеровывать молча. Version-awareness сохраняется: новый производитель даёт новые ID и строки `object_lineage` |
| Буква EPUB | `x0001` | `s0001` (spine) | мнемоника; `r` — рендер DOCX. По ID видно, «физическая» ли это страница |
| `citing_work_id` в BibliographyEntry | поле записи | вычисляется во view `bibliography` и в `bibliography_links` | запись — извлечение из одного источника, а привязка к Work — глобальная курируемая связь. При смене связей не нужно переписывать документные партиции. Итог тот же: `citing_work_id` может быть NULL = UNKNOWN |
| Раскладка артефактов | `<source_id>/p0001.<ext>` | content-addressed `artifacts/<kind>/<hh>/<hh>/<sha256>.<ext>` | дедупликация, неизменяемость, проверка целостности. ID с `:` в имена файлов не попадают, чего и хотел A |
| Контейнеры | один Work на файл, PWL — в `external_ids` | **согласен**; грамматика дочерних Work только зарезервирована | — |
| `review_status` для 013 и 022 | «при необходимости NOT_APPLICABLE» | ввожу `NOT_APPLICABLE`: допустим только при `lifecycle_status ≠ ACTIVE` | колонка не-null, фильтры простые, подмены нет |

## 1. Датасеты и схемы

### 1.0 Состав

| Датасет | Класс | Партиция | PK | Главные FK | Пишет | Строк v0 (оценка) |
|---|---|---|---|---|---|---|
| `sources` | REGISTRY_GLOBAL | `run=` (сборка реестра) | `source_id` | — | registry import (D) | 251 |
| `works` | REGISTRY_GLOBAL | `run=` | `work_id` | `venue_id`, `merged_into_work_id` | registry import | ≈ 247 |
| `source_work_links` | REGISTRY_GLOBAL | `run=` | `object_id` (SWL) | `source_id`, `work_id` | registry import | ≈ 255 |
| `work_relations` | REGISTRY_GLOBAL | `run=` | `object_id` (WRL) | `from_work_id`, `to_work_id` | registry import | ≈ 10–20 |
| `source_relations` | REGISTRY_GLOBAL | `run=` | `object_id` (SRL) | `from_source_id`, `to_source_id` | registry import | ≈ 5–10 |
| `authors` | REGISTRY_GLOBAL | `run=` | `author_id` | `same_as_author_id` | registry import | ≈ 400–600 |
| `work_authors` | REGISTRY_GLOBAL | `run=` | `object_id` (WAU) | `work_id`, `author_id` | registry import | ≈ 600–900 |
| `venues` | REGISTRY_GLOBAL | `run=` | `venue_id` | `same_as_venue_id` | registry import | ≈ 80–120 |
| `documents` | HEAD_PER_SOURCE | `source_id=/run=` | `object_id` = `<SID>:doc` | `source_id` | extraction (C) | 249 |
| `pages` | HEAD_PER_SOURCE | `source_id=/run=` | `page_id` | `source_id` | extraction | ≈ 26 480 |
| `blocks` | HEAD_PER_SOURCE | `source_id=/run=` | `object_id` | `page_id` | extraction / OCR | 10⁵–10⁶ |
| `figures` | HEAD_PER_SOURCE | `source_id=/run=` | `object_id` | `page_id`, `caption_block_id` | extraction / OCR | ≈ 10⁴ |
| `tables` | HEAD_PER_SOURCE | `source_id=/run=` | `object_id` | `page_id`, `continues_object_id` | extraction / OCR | 10³–10⁴ |
| `formulas` | HEAD_PER_SOURCE | `source_id=/run=` | `object_id` | `page_id` | extraction / OCR | ≈ 10⁴ |
| `bibliography_entries` | HEAD_PER_SOURCE | `source_id=/run=` | `object_id` | `page_id`, `list_block_ids` | extraction | ≈ 10⁴ |
| `bibliography_links` | DERIVED_GLOBAL | `run=` | `object_id` (BML) | `entry_id`, `cited_work_id` | derivation (D) | ≤ 10⁴ |
| `processing_runs` | APPEND_LOG | `run=` (`start`/`end`) | (`processing_run_id`, `record_phase`) | — | CLI | по прогонам |
| `processing_steps` | APPEND_LOG | `run=/part-NNNNN` | `step_id` | `processing_run_id`, `page_id` | pipeline | ≈ 10⁵ на полный прогон |
| `errors` | APPEND_LOG | `run=/part-NNNNN` | `error_id` | `step_id`, `page_id` | pipeline | малое |
| `artifacts` | APPEND_LOG | `run=/source_id=` | `artifact_id` (дедуп во view) | — | artifact store (C) | ≈ 10⁵ |

Классы:

- **REGISTRY_GLOBAL** — целиком пересобирается из файлов PRIVATE `00_registry/`, по одному файлу на сборку.
- **HEAD_PER_SOURCE** — в снимке ровно одна партиция на источник (head).
- **DERIVED_GLOBAL** — пересобирается из head и реестра.
- **APPEND_LOG** — в снимок входят все файлы: это история.

### 1.1 Общие соглашения

- **Типы Arrow:**
  - `string` (UTF-8);
  - `int16`, `int32`, `int64`, `float64`, `bool`;
  - `date32`, `timestamp[us, tz=UTC]`;
  - `list<string>`, `list<struct<…>>`, `struct<…>`.
- **Перечисления хранятся как `string`** из закрытого словаря (§3). Проверку делают pydantic и валидатор.
  - Arrow `dictionary` не используется. Parquet сам кодирует такие колонки словарём, а DuckDB и polars читают их без
    унификации категорий.
- **`null`** означает «значения нет или неизвестно». Списки никогда не null: пустой список — `[]`.
- **Порядок колонок:** сначала конверт (§2), затем поля датасета в объявленном порядке.
- **bbox** — четыре колонки `float64`: `bbox_x0, bbox_y0, bbox_x1, bbox_y1`, плюс `bbox_space`.
  - Инвариант: `x1 > x0`, `y1 > y0`, bbox внутри страницы с допуском 1 pt.
  - Система PAGE_PT_TL — §3.5.
- **sha256** — 64 hex в нижнем регистре.
- **Пути** — относительные, POSIX, от логического корня: `canonical_path` от `$VKM_RESOURCES_ROOT`, `storage_relpath`
  от `$VKM_DATA_ROOT/artifacts`.
- **Имена полей (CP-04).**
  - Текст: `text` (как извлечено), `normalized_text`, `raw_output` (сырой выход экстрактора или модели), `caption`.
  - Нигде нет `quote`, `verbatim_quote`, `ocr_text`, `page_text`, `full_text`.
  - Нет и имён pydantic-классов, которые в нижнем регистре совпадают с ними (класс `Quote` даёт ключ `$defs` `quote`).
- **Обозначения в таблицах.** `ENV-DOC`, `ENV-REG`, `ENV-LINK` — профили конверта (§2), общие поля конверта в таблицах
  не повторяются. «нет» в колонке null — поле обязательно (non-nullable).

### 1.2 `sources` — зарегистрированный файл (REGISTRY_GLOBAL, ENV-REG)

Одна строка на каждую запись реестра: 251, включая 013 и 022 (CP-05). `object_id = source_id`,
`origin = REGISTRY`.

| Поле | Arrow | null | Семантика | Пример |
|---|---|---|---|---|
| `source_id` | string | нет | ID реестра; не меняется; связан с `source_sha256` 1:1 на всю жизнь (§4.9) | `VKM-SRC-042` |
| `source_sha256` | string | нет | sha256 из реестра (= LFS oid) | 64 hex |
| `size_bytes` | int64 | нет | размер из реестра | `160429` |
| `canonical_path` | string | нет | путь от `$VKM_RESOURCES_ROOT` | `04_articles/geomechanics/x.pdf` |
| `original_filename` | string | нет | дословно из реестра | `x.pdf` |
| `file_extension` | string | нет | расширение `canonical_path` в нижнем регистре | `pdf` |
| `format_detected` | string | нет | FileFormat по сигнатуре, с допуском UTF-8 BOM (§3.3); `UNKNOWN`, если файла нет | `PDF` |
| `file_status` | string | нет | FileStatus: наличие и сверка sha256 и размера | `PRESENT_VERIFIED` |
| `observed_sha256` | string | да | посчитанный sha256; NULL, если файла нет | 64 hex |
| `observed_size_bytes` | int64 | да | фактический размер | `160429` |
| `lifecycle_status` | string | нет | ACTIVE, ABSENT_BY_REGISTER или RETIRED (CP-05) | `ACTIVE` |
| `lifecycle_reason_code` | string | да | ARCHIVE_DELETED_AFTER_ASSEMBLY (013), RETIRED_NOT_EVIDENCE (022) | NULL |
| `source_class_raw` | string | нет | `source_class` реестра дословно (27 значений; новое значение — WARN) | `journal_article` |
| `priority` | string | нет | A/B/C/D дословно | `A` |
| `site_scope_raw` | string | нет | `evidence_scope` дословно (CP-07) | `VKM_regional` |
| `site_scope` | list<string> | нет | значения `vkm_world` `Scope`; `[]` при AMBIGUOUS и NOT_A_SCOPE | `["VKM_REGIONAL"]` |
| `site_scope_candidates` | list<string> | нет | кандидаты только для AMBIGUOUS; для «истинных» фильтров не использовать | `["SKRU1_OR_SKRU2_UNATTRIBUTED"]` |
| `site_scope_mapping` | string | нет | EXACT/CASE/SYNONYM/LOSSY/MULTI/AMBIGUOUS/NOT_A_SCOPE | `CASE` |
| `site_scope_map_version` | string | нет | версия таблицы §3.4 | `1` |
| `review_status` | string | нет | ReviewStatus источника (CP-06); `NOT_APPLICABLE` только при lifecycle ≠ ACTIVE | `QUICK_LOOK_ONLY` |
| `review_status_basis` | string | нет | PHASE1_COVERAGE_MASTER / INTAKE_QUICK_LOOK_MARKER / DEFAULT_UNSEEN / LIFECYCLE | `INTAKE_QUICK_LOOK_MARKER` |
| `evidence_coverage_raw` | string | да | `CoverageLevel` Phase 1 дословно, только 001–041 | `FULLY_REVIEWED` |
| `register_scientific_role` | string | нет | дословно | — |
| `register_migration_source` | string | нет | дословно | — |
| `register_migration_status` | string | нет | дословно | `ADDED_BY_USER_EXACT` |
| `register_notes` | string | нет | дословно. Заметки quick look — **не evidence** | — |
| `ingestion_date` | date32 | да | дата приёмки файла в реестр | `2026-09-27` |
| `ingestion_date_precision` | string | нет | day/month/year/decade/unknown | `day` |
| `ingestion_basis` | string | нет | INTAKE_MANIFEST / MIGRATION_SOURCE_TEXT / REGISTER_GIT_HISTORY / UNKNOWN | `INTAKE_MANIFEST` |
| `input_ref` | string | нет | логическое имя входа | `PRIVATE:00_registry/SOURCE_REGISTER.csv` |
| `input_sha256` | string | нет | sha256 входного файла | 64 hex |
| `input_row` | int32 | нет | номер строки данных с 1. Только провенанс, не идентичность | `42` |
| `register_row_sha256` | string | нет | sha256 канонизированной строки реестра; ловит правки строки между снимками | 64 hex |

Инварианты:

- 251 строка, `source_id` уникален.
- `file_status = MISSING` допустим только при `lifecycle_status ≠ ACTIVE`; иначе блокирующая ошибка
  `SOURCE_FILE_MISSING`.
- `lifecycle_status ≠ ACTIVE` ⇔ задан `lifecycle_reason_code` ⇔ `review_status = NOT_APPLICABLE`.
- Любое значение `site_scope_raw` есть в таблице §3.4. Неотображённое значение — блокирующая ошибка.

### 1.3 `works` — библиографическое произведение (REGISTRY_GLOBAL, ENV-REG)

Строится из курируемого PRIVATE `00_registry/work_registry/WORK_REGISTER.csv` (§4.5, §13). `object_id = work_id`,
`source_id = NULL`. Tombstone-строки сохраняются навсегда.

| Поле | Arrow | null | Семантика | Пример |
|---|---|---|---|---|
| `work_id` | string | нет | `VKM-WRK-NNN` по якорному источнику (CP-09) | `VKM-WRK-013` |
| `status` | string | нет | ACTIVE, MERGED_INTO или WITHDRAWN | `ACTIVE` |
| `merged_into_work_id` | string | да | цель tombstone | NULL |
| `anchor_source_id` | string | нет | якорь: наименьший VKM-SRC группы (§4.5) | `VKM-SRC-013` |
| `work_type` | string | нет | WorkType — жанр. Природа копии — в `source_work_links.link_type` | `TEACHING_MANUAL` |
| `title` | string | да | заглавие на языке оригинала (библиографические метаданные) | — |
| `title_en` | string | да | английское заглавие или перевод | — |
| `title_variants` | list<string> | нет | варианты заглавия | `[]` |
| `authors_display` | string | да | авторы как курировано, разделитель `; ` | `Иванов И. И.; Петров П. П.` |
| `publication_year` | int16 | да | год публикации | `2008` |
| `publication_year_raw` | string | да | как в метаданных («1984–2022», «undated …») | `2008` |
| `publication_date` | date32 | да | первый день периода | `2008-01-01` |
| `publication_date_precision` | string | нет | day/month/year/decade/unknown | `year` |
| `venue_id` | string | да | FK `venues` (только для сериальных изданий) | `VEN-3f…` |
| `venue_display` | string | да | как напечатано | — |
| `volume`, `issue` | string | да | том, выпуск | `70` |
| `pages_range` | string | да | печатные страницы | `191-209` |
| `edition` | string | да | издание | `2-е изд.` |
| `publisher`, `publisher_city` | string | да | издательство и город | — |
| `doi` | string | да | нормализованный: нижний регистр, без URL-префикса | `10.9999/example.1` |
| `isbn` | list<string> | нет | ISBN-13 без дефисов | `[]` |
| `languages` | list<string> | нет | ISO 639-1 | `["ru"]` |
| `external_ids` | list<struct<scheme:string, value:string, relation:string>> | нет | crosswalk: PWL, CW, WG, EXT_SRC, EXTWEB, ASIN, URN_UUID, OPENALEX; relation — SAME_WORK / COMPONENT / OTHER_EDITION / NOT_SAME / CITED_AS | `[{"scheme":"PWL","value":"PWL-0062","relation":"SAME_WORK"}]` |
| `metadata_basis` | struct<title:string, authors:string, year:string, venue:string, identifiers:string> | нет | MetadataBasis по каждому полю (§3.8) | `{"title":"TITLE_PAGE_VERIFIED",…}` |
| `identity_status` | string | нет | VERIFIED_IN_FILE, CATALOGUE_UNVERIFIED или FILENAME_PAGECOUNT_UNVERIFIED | `CATALOGUE_UNVERIFIED` |
| `available_from` | date32 | да | курированная доступность; **NULL = UNKNOWN**. D-03 в каноне не применяется (§7) | NULL |
| `available_from_precision` | string | да | точность доступности | NULL |
| `available_from_basis` | string | нет | UNKNOWN или CURATED | `UNKNOWN` |
| `curation_status` | string | нет | AUTO_PROPOSED, CURATED или REJECTED | `AUTO_PROPOSED` |
| `review_status` | string | нет | покрытие чтением = максимум по ACTIVE-источникам работы (производное). **Не** проверка метаданных | `UNSEEN` |
| `notes` | string | да | заметки куратора | — |
| `input_ref`, `input_sha256`, `input_row` | string, string, int32 | нет | провенанс строки курируемого файла | — |

Инварианты:

- ACTIVE-работа имеет хотя бы одну связь с источником: в v0 работ без Source нет.
- Цепочки MERGED_INTO заканчиваются ACTIVE-работой и не образуют циклов.
- `anchor_source_id` = наименьший `source_id` среди связей FULL_COPY, PARTIAL_COPY, PART и FRONT_MATTER_ONLY
  этой работы.

### 1.4 `source_work_links` — Source → Work (REGISTRY_GLOBAL, ENV-LINK)

| Поле | Arrow | null | Семантика | Пример |
|---|---|---|---|---|
| `object_id` | string | нет | `SWL-<16 hex>` из составного ключа (§4.8) | `SWL-9c…` |
| `source_id` | string | нет | FK `sources` | `VKM-SRC-209` |
| `work_id` | string | да | FK `works`. NULL допустим только для `FOREIGN_CONTENT` с неопознанной работой | `VKM-WRK-208` |
| `link_type` | string | нет | FULL_COPY / PARTIAL_COPY / FRONT_MATTER_ONLY / PART / FOREIGN_CONTENT; CONTAINS_WORK зарезервирован | `PART` |
| `is_primary` | bool | нет | главная работа файла: ровно одна на источник | `true` |
| `page_start`, `page_end` | int32 | да | диапазон физических индексов в единицах страниц источника; NULL — весь файл | `1`, `99` |
| `part_label` | string | да | обозначение части или тома | `2` |
| `printed_range` | string | да | печатные страницы части | `101-199` |
| `basis` | string | нет | BOOTSTRAP_SINGLETON / REGISTER_NOTES / INTAKE_MANIFEST / HUNT_TABLE / TITLE_PAGE_VERIFIED / CURATED_MANUAL | `REGISTER_NOTES` |
| `curation_status` | string | нет | AUTO_PROPOSED, CURATED или REJECTED | `CURATED` |
| `notes` | string | да | заметки | — |
| `input_ref`, `input_sha256`, `input_row` | — | нет | провенанс строки | — |

Правила:

- Ровно один `is_primary` на источник; тип главной связи — FULL_COPY, PARTIAL_COPY, FRONT_MATTER_ONLY или PART.
- `FOREIGN_CONTENT` никогда не главная и всегда с диапазоном страниц. Смысл: «эти страницы несут чужое произведение»
  (общие страницы «Горного эха», конец предыдущей статьи).
- **Группировка (CP-09).** Если у работы больше одного источника со связями FULL, PARTIAL, PART или FRONT_MATTER,
  все эти связи обязаны быть CURATED. Автоматически разрешена только одиночная связь «файл = своя работа»
  (`BOOTSTRAP_SINGLETON`).
- REJECTED в проекции не попадает.

### 1.5 `work_relations` — Work ↔ Work (REGISTRY_GLOBAL, ENV-LINK)

| Поле | Arrow | null | Семантика | Пример |
|---|---|---|---|---|
| `object_id` | string | нет | `WRL-<16 hex>` | — |
| `from_work_id` | string | нет | FK `works` | `VKM-WRK-001` |
| `relation` | string | нет | ABSTRACT_OF, EDITION_OF, TRANSLATION_OF, VOLUME_SET_SIBLING, SERIES_SIBLING, COMPANION_OF, NOT_SAME (§3.8) | `ABSTRACT_OF` |
| `to_work_id` | string | нет | FK `works` | `VKM-WRK-196` |
| `is_symmetric` | bool | нет | для симметричных отношений пара хранится один раз, с `from < to` | `false` |
| `basis`, `curation_status`, `notes`, `input_*` | — | — | как в 1.4 | — |

Правила:

- В проекцию попадают только CURATED.
- `NOT_SAME` запрещает любую автоматическую группировку пары.
- Связи, распарсенные из notes, существуют только как AUTO_PROPOSED-кандидаты (CP-09).

### 1.6 `source_relations` — Source ↔ Source (REGISTRY_GLOBAL, ENV-LINK)

| Поле | Arrow | null | Семантика | Пример |
|---|---|---|---|---|
| `object_id` | string | нет | `SRL-<16 hex>` | — |
| `from_source_id` | string | нет | FK `sources` | `VKM-SRC-025` |
| `relation` | string | нет | DERIVED_FROM, CONTAINS_COPY_OF или SHARES_PAGES_WITH | `DERIVED_FROM` |
| `to_source_id` | string | нет | FK `sources` | `VKM-SRC-013` |
| `from_page_start`, `from_page_end`, `to_page_start`, `to_page_end` | int32 | да | соответствие страниц для SHARES_PAGES_WITH | `5,5,1,1` |
| `basis`, `curation_status`, `notes`, `input_*` | — | — | как в 1.4 | — |

Назначение — только явные факты уровня файла (аудит A §2.8):

- 025 DERIVED_FROM 013;
- 022 CONTAINS_COPY_OF 023: 022 не распаковывается;
- общие страницы «Горного эха»: 031 ↔ 005, 032 ↔ 005, 034 ↔ 199.

Дубль страницы ≠ независимое evidence. Автоматические кандидаты по совпадению текста — только во view
`duplicate_page_candidates` (§9), без слияния.

### 1.7 `authors`, 1.8 `work_authors`, 1.9 `venues` (REGISTRY_GLOBAL)

`authors` (ENV-REG, `origin = DERIVED` из авторских строк `works`):

| Поле | Arrow | null | Семантика |
|---|---|---|---|
| `author_id` | string | нет | `AUT-<12 hex>` от ключа имени (§4.6) |
| `name_display` | string | нет | первая по порядку форма в курируемых данных |
| `name_key` | string | нет | нормализованный ключ `фамилия\|инициалы\|полные имена\|письменность` |
| `script` | string | нет | CYRL, LATN, MIXED или OTHER |
| `identity_status` | string | нет | NAME_KEY_ONLY (v0) или CURATED |
| `same_as_author_id` | string | да | только курированная связь, например кириллица ↔ латиница |
| `orcid` | string | да | только курированно |

`work_authors` (ENV-LINK):

| Поле | Arrow | null | Семантика |
|---|---|---|---|
| `object_id` | string | нет | `WAU-<16 hex>` от (`work_id`, `ordinal`) |
| `work_id`, `author_id` | string | нет | FK |
| `ordinal` | int16 | нет | позиция в списке авторов, с 1 |
| `role` | string | нет | AUTHOR, EDITOR, COMPILER, SUPERVISOR, TRANSLATOR, CORPORATE_AUTHOR или UNKNOWN |
| `name_as_listed` | string | нет | имя как в курируемой строке работы |

`venues` (ENV-REG): `venue_id` (`VEN-<12 hex>`), `venue_type` (JOURNAL, PROCEEDINGS_SERIES, BOOK_SERIES или UNKNOWN),
`title_display`, `title_key`, `script`, `issn` list<string>, `identity_status` (NAME_KEY_ONLY, ISSN или CURATED),
`same_as_venue_id`.

Переводной журнал и оригинал — разные venue: автоматически не сливаются.

### 1.10 `documents` — разобранный файл источника (HEAD_PER_SOURCE, ENV-DOC)

`object_id = "<source_id>:doc"`, `origin = NATIVE`, `page_id = NULL`. Строки есть только у источников, чей файл
открыт; у 013 и 022 строки нет — их статус выводится из `sources` (§9).

| Поле | Arrow | null | Семантика | Пример |
|---|---|---|---|---|
| `format_detected` | string | нет | FileFormat по сигнатуре | `DJVU` |
| `format_version` | string | да | версия формата | `1.4` (PDF), `2.0` (EPUB) |
| `container_detail` | string | да | например DjVu bundled/indirect | `bundled` |
| `pagination_basis` | string | нет | PDF_PAGE_TREE / DJVU_PAGE_ORDER / DOCX_PINNED_RENDER / EPUB_SPINE / IMAGE_FRAMES | `DJVU_PAGE_ORDER` |
| `page_unit` | string | нет | `p`, `r` или `s` (§4.3) | `p` |
| `page_count` | int32 | нет | число единиц-страниц: столько строк `pages` обязано быть | `384` |
| `page_count_check` | int32 | да | тот же счёт вторым независимым методом | `384` |
| `page_count_check_method` | string | да | чем считали | `IFF_FORM_DJVU_COUNT` |
| `register_page_count_hint` | int32 | да | число страниц из notes реестра или intake, если оно есть | `423` |
| `shared_component_count` | int32 | да | DjVu DJVI — не страницы | `39` |
| `pagination_render_profile` | string | да | для DOCX: рендерер, версия, хеш шрифтов | `libreoffice-24.2.7.2;fonts=<sha8>` |
| `pagination_artifact_id` | string | да | артефакт закреплённого рендера DOCX → PDF | `sha256:…` |
| `document_class` | string | нет | NATIVE_TEXT / VECTOR_HEAVY / RASTER_SCAN / MIXED / BROKEN_TEXT_LAYER / REFLOWABLE / UNKNOWN (критерии задаёт C) | `RASTER_SCAN` |
| `is_encrypted` | bool | нет | есть ли шифрование | `false` |
| `text_extraction_permitted` | bool | да | права на извлечение текста | `true` |
| `has_native_page_labels` | bool | да | есть ли PDF `/PageLabels` | `true` |
| `file_meta_title`, `file_meta_author`, `file_meta_subject`, `file_meta_keywords`, `file_meta_creator`, `file_meta_producer`, `file_meta_created_raw`, `file_meta_modified_raw` | string | да | встроенные метаданные дословно (Info/XMP, OPF, docProps). **Подсказка, не истина** | — |
| `file_identifiers` | list<string> | нет | например dc:identifier EPUB | `[]` |
| `file_languages` | list<string> | нет | языки из метаданных файла | `["ru"]` |
| `processing_status` | string | нет | SourceProcessingStatus на момент коммита: COMPLETE, PARTIAL, NEEDS_REVIEW, FAILED или UNSUPPORTED | `PARTIAL` |

Флаги: `PAGECOUNT_DIFFERS_FROM_REGISTER_HINT` (053: 384 против 423 — не молча, CP-10), `SIGNATURE_AFTER_BOM`
(020, 028, 050, 052, 064), `ENCRYPTED_SOURCE` (239), `REPAIRED_DERIVED_COPY_USED`.

### 1.11 `pages` (HEAD_PER_SOURCE, ENV-DOC)

- `object_id = page_id`.
- `origin`: NATIVE для PDF, DjVu и EPUB; DERIVED для страниц рендера DOCX.
- Строка есть у каждой страницы пагинации, даже необработанной или упавшей: страницы не теряются молча.

| Поле | Arrow | null | Семантика | Пример |
|---|---|---|---|---|
| `page_id` | string | нет | §4.3 | `VKM-SRC-037:p0126` |
| `page_index` | int32 | нет | физический индекс с 1 в единицах `page_unit` | `126` |
| `page_kind` | string | нет | PDF_PAGE / DJVU_PAGE / DOCX_RENDERED_PAGE / EPUB_SPINE_ITEM / IMAGE_FRAME | `DJVU_PAGE` |
| `printed_page_raw` | string | да | печатная метка как есть: значение `/PageLabels` или найденный колонтитул | `250-251` |
| `printed_page_labels` | list<string> | нет | разобранные метки; у разворота их две | `["250","251"]` |
| `printed_label_origin` | string | нет | PDF_PAGE_LABELS / DETECTED_RUNNING_HEAD / CURATED / NONE | `DETECTED_RUNNING_HEAD` |
| `printed_label_parse_status` | string | нет | PARSED, UNPARSED или NONE | `PARSED` |
| `is_spread` | bool | да | разворот «2 печатные на физическую» (037, 243); полустраниц в v0 нет (CP-08) | `true` |
| `width_pt`, `height_pt` | float64 | да | размер отображаемой страницы в PAGE_PT_TL; у EPUB NULL | `1190.5`, `841.9` |
| `rotation_deg` | int16 | нет | применённый поворот: 0, 90, 180 или 270 | `0` |
| `page_box` | string | да | CROPBOX, MEDIABOX или DJVU_IMAGE | `DJVU_IMAGE` |
| `native_dpi` | float64 | да | DPI из INFO DjVu | `300` |
| `bbox_space` | string | нет | PAGE_PT_TL или NONE (EPUB) | `PAGE_PT_TL` |
| `native_text_status` | string | нет | NativeTextStatus | `ABSENT` |
| `native_char_count` | int32 | да | символов в родном слое | NULL |
| `ocr_status` | string | нет | OcrStatus | `DONE` |
| `recognized_char_count` | int32 | да | символов после OCR | `3412` |
| `page_status` | string | нет | ProcessingStatus страницы | `OCR_OK` |
| `primary_text_origin` | string | нет | какой слой — канонический текст для поиска: NATIVE, OCR или NONE | `OCR` |
| `text_sha256` | string | да | sha256 нормализованного текста основного слоя: для поиска дублей и drift | 64 hex |
| `native_raw_artifact_id` | string | да | сырой выход нативного извлечения страницы | `sha256:…` |
| `ocr_raw_artifact_id` | string | да | сырой выход OCR страницы | `sha256:…` |
| `render_artifact_id` | string | да | канонический рендер для визуального rerank | `sha256:…` |
| `render_dpi` | int16 | да | DPI рендера | `200` |
| `spine_href` | string | да | путь XHTML внутри EPUB (не ФС) | `OEBPS/ch03.xhtml` |

Конверт: `raw_artifact_id` = сырой выход **основного** слоя; `raw_content_sha256` = хеш геометрии и
`text_sha256`.

### 1.12 `blocks` (HEAD_PER_SOURCE, ENV-DOC)

| Поле | Arrow | null | Семантика | Пример |
|---|---|---|---|---|
| `text_layer` | string | нет | PDF_TEXT_LAYER / DJVU_TEXT_LAYER / OCR_MODEL / EPUB_XHTML / DOCX_XML | `OCR_MODEL` |
| `block_type` | string | нет | BlockType | `TEXT` |
| `reading_order` | int32 | нет | порядок внутри (страница, слой), с 1 | `3` |
| `bbox_x0`…`bbox_y1` | float64 | да | PAGE_PT_TL; у EPUB NULL | `56.7, 60.0, 538.6, 100.0` |
| `bbox_space` | string | нет | PAGE_PT_TL или NONE | `PAGE_PT_TL` |
| `text` | string | нет | текст как извлечён или распознан (не нормализован) | — |
| `normalized_text` | string | нет | нормализация для поиска (правила задаёт C) | — |
| `char_count` | int32 | нет | длина `text` | `412` |
| `language` | string | да | ISO 639-1 | `ru` |
| `language_confidence` | float64 | да | уверенность определения языка | `0.97` |
| `recognition_confidence` | float64 | да | средняя уверенность OCR | `0.91` |
| `raw_locator` | string | да | указатель внутрь сырого артефакта: JSON Pointer или номер нативного блока | `/pages/0/blocks/12` |

Основной это слой или нет — видно по `pages.primary_text_origin`: оба слоя хранятся, ничего не выбрасывается.

### 1.13 `figures` (HEAD_PER_SOURCE, ENV-DOC)

| Поле | Arrow | null | Семантика | Пример |
|---|---|---|---|---|
| `bbox_*`, `bbox_space` | float64, string | как выше | регион рисунка | — |
| `figure_label` | string | да | найденная подпись-номер как есть | `Рис. 1.3` |
| `caption` | string | да | текст подписи как извлечён | — |
| `caption_normalized` | string | да | нормализованная подпись | — |
| `caption_block_id` | string | да | FK `blocks` (блок `CAPTION`) | `…:b…` |
| `layout_class` | string | нет | RASTER_IMAGE / VECTOR_GRAPHICS / MIXED / LAYOUT_REGION | `VECTOR_GRAPHICS` |
| `detected_figure_type` | string | нет | FigureType; `UNKNOWN_FIGURE_TYPE`, если метода нет или уверенность ниже порога | `UNKNOWN_FIGURE_TYPE` |
| `figure_type_method` | string | нет | MODEL_CLASSIFIER, HEURISTIC, CURATED или NONE | `NONE` |
| `figure_type_confidence` | float64 | да | уверенность классификатора | `0.42` |
| `figure_type_threshold` | float64 | да | порог из конфигурации прогона (правило самодостаточно) | `0.80` |
| `image_artifact_id` | string | да | кроп рендера | `sha256:…` |
| `image_dpi` | int16 | да | DPI кропа | `200` |
| `embedded_image_artifact_id` | string | да | нативное растровое изображение (XObject PDF, картинка EPUB) байт в байт | `sha256:…` |
| `vector_artifact_id` | string | да | нативные векторные пути и трансформации; `coordinate_space = PAGE_SPACE`; растеризация их не заменяет (§23) | `sha256:…` |

CRS-полей у рисунка нет. bbox — page space, а географии в v0 нет (§3.5).

### 1.14 `tables` (HEAD_PER_SOURCE, ENV-DOC)

| Поле | Arrow | null | Семантика |
|---|---|---|---|
| `bbox_*`, `bbox_space` | — | как выше | регион таблицы |
| `table_label` | string | да | найденный номер таблицы как есть |
| `caption`, `caption_normalized`, `caption_block_id` | string | да | подпись |
| `raw_format` | string | нет | HTML / MARKDOWN / OTSL / JSON / TEXT / DOCX_XML / XHTML |
| `raw_output` | string | нет | сырое распознавание или нативная разметка, **всегда сохраняется** (§20) |
| `n_rows`, `n_cols` | int32 | да | размер сетки |
| `cells` | list<struct<row:int32, col:int32, row_span:int16, col_span:int16, is_header:bool, text:string>> | нет | нормализованная сетка; `[]`, если структура не восстановлена |
| `normalized_text` | string | да | плоский текст или Markdown для поиска |
| `structure_confidence` | float64 | да | уверенность восстановления структуры |
| `image_artifact_id`, `image_dpi` | string, int16 | да | кроп |
| `continues_object_id` | string | да | фрагмент на следующей странице |

### 1.15 `formulas` (HEAD_PER_SOURCE, ENV-DOC) — Formula v0 без семантики (§21, контракт §15)

| Поле | Arrow | null | Семантика |
|---|---|---|---|
| `bbox_*`, `bbox_space` | — | как выше | регион формулы |
| `formula_kind` | string | нет | DISPLAY, INLINE или UNKNOWN |
| `equation_label` | string | да | найденный номер как есть, например `(3.2)` |
| `raw_format` | string | нет | LATEX, OMML (160 нативных формул DOCX 023), MATHML, TEXT или IMAGE_ONLY (GIF-формулы EPUB 249) |
| `raw_output` | string | да | сырое распознавание или нативная разметка; NULL только при IMAGE_ONLY без распознавания |
| `normalized_latex` | string | да | нормализованный LaTeX, если есть |
| `latex_parse_ok` | bool | да | разобрал ли LaTeX-парсер нормализованную запись |
| `recognition_confidence` | float64 | да | уверенность распознавания |
| `image_artifact_id` | string | да | кроп или нативная картинка |

Переменных, единиц и допущений нет: это будущий ReviewedFormula (L2 и L3).

### 1.16 `bibliography_entries` (HEAD_PER_SOURCE, ENV-DOC)

| Поле | Arrow | null | Семантика |
|---|---|---|---|
| `bbox_*`, `bbox_space` | — | как выше | первый фрагмент записи |
| `entry_label` | string | да | печатный номер как есть: `72`, `[12]` |
| `ordinal_in_list` | int32 | да | порядковый номер в списке литературы |
| `list_block_ids` | list<string> | нет | блоки, из которых собрана запись |
| `continues_on_page_id` | string | да | запись продолжается на следующей странице |
| `text` | string | нет | запись как извлечена |
| `normalized_text` | string | нет | нормализованная запись |
| `parsed_authors` | list<string> | нет | авто-разбор, DERIVED-догадка |
| `parsed_title`, `parsed_year_raw`, `parsed_venue`, `parsed_volume`, `parsed_issue`, `parsed_pages`, `parsed_url` | string | да | авто-разбор |
| `parsed_year` | int16 | да | авто-разбор |
| `parsed_doi` | string | да | нормализованный DOI |
| `parsed_isbn` | list<string> | нет | нормализованные ISBN |
| `parse_method` | string | да | метод разбора |
| `parse_confidence` | float64 | да | уверенность разбора |
| `language` | string | да | язык записи |

`origin` = происхождение текста (NATIVE или OCR). Сегментацию и разбор выполняет `extractor_id` (например
`bib-segmenter`). `citing_work_id` здесь не хранится: он во view `bibliography` и в `bibliography_links`, §0.3.

### 1.17 `bibliography_links` (DERIVED_GLOBAL, ENV-LINK)

| Поле | Arrow | null | Семантика |
|---|---|---|---|
| `object_id` | string | нет | `BML-<16 hex>` от (`entry_id`, `cited_work_id`, `match_method`) |
| `entry_id` | string | нет | FK `bibliography_entries.object_id` |
| `citing_source_id`, `citing_page_id` | string | нет | источник и страница, где стоит запись |
| `citing_work_id` | string | да | по правилу §9 (`bibliography`); NULL = UNKNOWN |
| `citing_work_resolution` | string | нет | UNIQUE_LINK / RANGED_COMPONENT / AMBIGUOUS / FOREIGN_CONTENT / NO_LINK |
| `cited_work_id` | string | нет | FK `works` (в v0 только работы корпуса) |
| `match_method` | string | нет | DOI_EXACT / ISBN_EXACT / TITLE_AUTHOR_YEAR / TITLE_YEAR / PHASE1_CITATION_GRAPH / CURATED |
| `match_score` | float64 | нет | 0…1 |
| `match_status` | string | нет | CANDIDATE / ACCEPTED_EXACT_ID / ACCEPTED_CURATED / REJECTED |
| `matched_fields` | list<string> | нет | совпавшие поля, например `["doi"]` |
| `curation_status` | string | нет | AUTO_PROPOSED, CURATED или REJECTED |

- `origin = DERIVED`, `review_status = AUTO_EXTRACTED_UNREVIEWED`.
- Ребро CITES (view `cites`, Neo4j) строится **только** из ACCEPTED_*.
- Нечёткие совпадения остаются кандидатами.
- Цитирование ≠ согласие (§49).

### 1.18 `artifacts` — индекс артефактов (APPEND_LOG)

| Поле | Arrow | null | Семантика |
|---|---|---|---|
| `schema_version` | string | нет | semver датасета |
| `artifact_id` | string | нет | `sha256:<64 hex>` — хеш байтов |
| `artifact_kind` | string | нет | ArtifactKind при первой регистрации |
| `media_type` | string | нет | например `image/png` |
| `size_bytes` | int64 | нет | размер |
| `storage_relpath` | string | нет | `<kind_dir>/<hh>/<hh>/<sha256>.<ext>` от `$VKM_DATA_ROOT/artifacts`, выводится детерминированно |
| `retention_class` | string | нет | KEEP_RAW или KEEP_REFERENCED |
| `image_width_px`, `image_height_px` | int32 | да | размер изображения |
| `image_dpi` | float64 | да | DPI изображения |
| `coordinate_space` | string | да | PAGE_SPACE для VECTOR_PATHS; GEO в v0 не бывает |
| `crs_status` | string | да | только при GEO (§3.5) |
| `producer_signature` | string | да | для OCR_RAW — `call_signature` (кеш модели, §6.7) |
| `producer_step_id` | string | да | шаг-производитель |
| `created_by_run_id` | string | нет | прогон |
| `registered_source_id`, `registered_page_id` | string | да | контекст первой регистрации, справочно |
| `created_at` | timestamp[us, UTC] | нет | время регистрации |

Строка описывает **содержимое**. Контекст использования — в ссылающихся строках: `render_artifact_id` у Page,
`image_artifact_id` у Figure. Повторная регистрация того же blob допустима: view `artifacts` дедуплицирует по первой
регистрации, валидатор сверяет, что sha, размер и тип совпадают.

### 1.19 `processing_runs` (APPEND_LOG)

Две неизменяемые записи на прогон: `record_phase = START` при старте и `END` в конце. START без END — видимый
признак падения.

| Поле | Arrow | null | Семантика |
|---|---|---|---|
| `schema_version` | string | нет | semver датасета |
| `processing_run_id` | string | нет | ID прогона |
| `record_phase` | string | нет | START или END |
| `run_kind` | string | нет | REGISTRY_IMPORT / EXTRACTION / OCR / DERIVATION / SNAPSHOT_BUILD / SCHEMA_MIGRATION / VALIDATION |
| `status` | string | нет | STARTED, SUCCEEDED, PARTIAL, FAILED или ABORTED |
| `started_at`, `finished_at` | timestamp[us, UTC] | нет / да | время |
| `cli_command` | string | нет | argv; пути заменены логическими корнями |
| `cli_flags` | list<string> | нет | `--resume`, `--force`, `--source`, `--page`, `--failed-only`, `--plan-only` |
| `plan_only` | bool | нет | прогон только с планом |
| `selection` | string | да | выражение выбора источников и страниц |
| `pipeline_version` | string | нет | §6.7 |
| `code_revision` | string | нет | git-коммит PUBLIC |
| `code_dirty` | bool | нет | были ли незакоммиченные изменения |
| `dependency_lock_sha256` | string | нет | хеш lock-файла окружения |
| `private_registry_revision` | string | нет | git-коммит PRIVATE |
| `source_register_sha256`, `work_register_sha256`, `work_links_sha256` | string | нет | снимок входов реестра |
| `host_role` | string | нет | WORKSTATION, CORE или EDGE (без имён хостов и IP) |
| `python_version`, `platform` | string | нет | окружение |
| `config_hash` | string | нет | хеш конфигурации |
| `config_artifact_id` | string | нет | полная разрешённая конфигурация как артефакт RUN_CONFIG |
| `models` | list<struct<model_id:string, model_revision:string, backend:string, backend_version:string, quantization:string, device:string>> | нет | модели прогона |
| `n_sources_planned`, `n_pages_planned`, `n_steps_executed`, `n_steps_reused`, `n_steps_failed` | int32 | да | счётчики (у START — NULL) |
| `log_ref` | string | да | структурный лог в `$VKM_DATA_ROOT/logs`, относительный путь |
| `parent_run_id` | string | да | для `--resume` |
| `control_plane_ref` | string | да | внешний ключ PostgreSQL; необязателен, потеря PostgreSQL канон не ломает |
| `created_at` | timestamp[us, UTC] | нет | время записи |

### 1.20 `processing_steps` (APPEND_LOG)

| Поле | Arrow | null | Семантика |
|---|---|---|---|
| `schema_version`, `step_id`, `processing_run_id` | string | нет | ID |
| `source_id` | string | да | NULL — шаг уровня прогона |
| `page_id` | string | да | NULL — шаг уровня источника |
| `page_index` | int32 | да | индекс страницы |
| `stage` | string | нет | Stage (§3.2) |
| `attempt` | int16 | нет | номер попытки |
| `outcome` | string | нет | EXECUTED / REUSED_CACHED / SKIPPED_UP_TO_DATE / SKIPPED_BY_POLICY / NOT_ATTEMPTED |
| `status` | string | нет | ProcessingStatus, включая SKIPPED_BY_REGISTER |
| `reason_code` | string | да | например ARCHIVE_DELETED_AFTER_ASSEMBLY |
| `stage_signature` | string | нет | подпись идемпотентности (§6.7) |
| `call_signature` | string | да | ключ кеша вызова модели |
| `source_sha256` | string | да | sha256 источника |
| `pipeline_version`, `extractor_id`, `extractor_version`, `config_hash` | string | нет | компоненты подписи — явными колонками |
| `model_id`, `model_revision` | string | да | компоненты подписи |
| `input_artifact_ids`, `output_artifact_ids` | list<string> | нет | входные и выходные артефакты |
| `n_objects_out` | int32 | нет | сколько объектов создано |
| `started_at`, `finished_at` | timestamp[us, UTC] | нет | время |
| `duration_ms` | int64 | нет | длительность |
| `commit_id` | string | да | коммит источника, в который попал выход |
| `log_ref` | string | да | ссылка в лог |
| `host_role` | string | нет | роль хоста |
| `control_job_ref` | string | да | ключ задания в control plane |

Шаги записываются частями `part-NNNNN.parquet` после каждого коммита источника. Пропуск 013 и 022 тоже шаг:
`stage = INSPECT`, `outcome = SKIPPED_BY_POLICY`, `status = SKIPPED_BY_REGISTER`, с кодом причины.

### 1.21 `errors` (APPEND_LOG)

| Поле | Arrow | null | Семантика |
|---|---|---|---|
| `schema_version`, `error_id`, `processing_run_id` | string | нет | ID |
| `step_id`, `source_id`, `page_id` | string | да | привязка |
| `page_index` | int32 | да | нужен, когда пагинация упала и `page_id` не создан |
| `stage` | string | нет | Stage |
| `code` | string | нет | ErrorCode (§3.2) |
| `tool`, `tool_version` | string | нет / да | инструмент и версия |
| `message` | string | нет | санитизировано: без абсолютных путей |
| `retryable` | bool | нет | можно ли повторить |
| `severity` | string | нет | ERROR или FATAL |
| `log_ref` | string | да | ссылка в лог |
| `exception_type` | string | да | тип исключения |
| `created_at` | timestamp[us, UTC] | нет | время |

Набор полей закрывает требование §44: code, stage, tool, message, retryable, source, page, log reference.

### 1.22 Зарезервировано (схема описана, в v0 не создаётся)

- **`object_lineage`.** Поля: `old_object_id`, `new_object_id`, `relation` (SAME_REGION_REEXTRACTED / SPLIT /
  MERGED / NO_SUCCESSOR), `match_method` (BBOX_IOU / TEXT_SIMILARITY / CURATED), `match_score`, `old_run_id`,
  `new_run_id`.
  - Появляется при первой смене производителя: новая ревизия модели или экстрактора.
- **`review_events`** — научный провенанс ревью (L2): кто, когда, что проверено, новый статус.
  - Пайплайн сюда не пишет. `review_status` строк L1 он не меняет никогда.
  - В v0 UI ревью нет, поэтому датасет не создаётся.
- **`source_members`** — члены ZIP. Оба ZIP реестра отсутствуют по реестру (013, 022), 022 не распаковывается.
  - Зарезервирована грамматика `VKM-SRC-NNN/mNNN` (§4.2).

## 2. Provenance-конверт

Колонки конверта плоские, одинаково названы во всех датасетах объектов.

| Поле | Arrow | DOC | REG | LINK | Семантика |
|---|---|---|---|---|---|
| `schema_version` | string | ✓ | ✓ | ✓ | semver датасета при записи; дублирует KV-метаданные файла |
| `object_id` | string | ✓ | ✓ | ✓ | стабильный ID строки (§4); для `sources`, `works`, `pages` совпадает с сущностным ID |
| `object_kind` | string | ✓ | ✓ | ✓ | SOURCE, WORK, DOCUMENT, PAGE, BLOCK, FIGURE, TABLE, FORMULA, BIBLIOGRAPHY_ENTRY, AUTHOR, VENUE, SOURCE_WORK_LINK, WORK_RELATION, SOURCE_RELATION, WORK_AUTHOR, BIBLIOGRAPHY_LINK |
| `source_id` | string | ✓ | только sources | где применимо | исходный файл |
| `page_id` | string | для объектов страницы | — | — | страница |
| `source_sha256` | string | ✓ = реестр | sources | — | sha обработанного файла; должен равняться реестру |
| `origin` | string | NATIVE / OCR / DERIVED | REGISTRY | REGISTRY / DERIVED | §2.1 |
| `pipeline_version` | string | ✓ | ✓ | ✓ | версия контракта обработки (§6.7) |
| `processing_run_id` | string | ✓ | ✓ (прогон импорта) | ✓ | прогон |
| `extractor_id` | string | ✓ | `registry-import` | импорт или matcher | кто извлёк |
| `extractor_version` | string | ✓ | ✓ | ✓ | версия |
| `model_id`, `model_revision` | string | ✓ при OCR | NULL | NULL | модель и точная ревизия |
| `config_hash` | string | ✓ | ✓ | ✓ | sha256 канонического JSON конфигурации **этапа** |
| `extraction_signature` | string | ✓ | NULL | ✓ | `stage_signature` шага-производителя |
| `raw_content_sha256` | string | ✓ | NULL | NULL | хеш сырого содержимого; инвариант при том же `object_id` (§4.4) |
| `content_sha256` | string | ✓ | ✓ | ✓ | хеш всех полей содержимого, без полей конверта |
| `raw_artifact_id` | string | ✓ для NATIVE и OCR | NULL | NULL | где лежит сырой выход; строка сама всегда canonical |
| `created_at` | timestamp[us, UTC] | ✓ | ✓ | ✓ | processing_time = `finished_at` шага-производителя; не входит в контент-хеши |
| `review_status` | string | AUTO_EXTRACTED_UNREVIEWED | sources, works — §3.1; authors, venues — NULL | bibliography_links — AUTO_…; реестровые связи — NULL | ревью содержания |
| `quality_flags` | list<string> | ✓ | ✓ | ✓ | закрытый словарь §3.3 |

У REG- и LINK-строк есть ещё `input_ref`, `input_sha256`, `input_row`: курируемый файл, его хеш и строка. Это ответ
на вопрос «где оригинал» для строк реестра. Проверка курирования для связей — `curation_status`, а не
`review_status`: ревью содержания и проверка метаданных — разные понятия.

### 2.1 Origin

| Значение | Когда |
|---|---|
| NATIVE | содержимое из собственных структур файла: текстовый слой и векторы PDF, встроенные изображения, текстовый слой DjVu, XHTML и картинки EPUB, XML и OMML DOCX. Эвристическая сегментация такого содержимого тоже NATIVE |
| OCR | любой выход модели распознавания или разметки по пикселям (GLM-OCR и др.). Обязательны `model_id` и `model_revision` |
| DERIVED | вычислено из других канонических строк, без повторного чтения файла: совпадения библиографии, авторы из строк работ, кандидаты дублей, страницы рендера DOCX |
| REGISTRY | загружено из файлов PRIVATE `00_registry/`: реестр, WORK_REGISTER, WORK_LINKS |
| CURATED | правка человека через будущий оверлей ревью; в v0 не пишется |

Если хоть часть содержимого получена моделью, объект — OCR (консервативно). Детали — в `text_layer` и во флаге
`MIXED_TEXT_LAYER`.

### 2.2 Canonical и raw

- Каждая строка canonical Parquet — **canonical**.
- Сырой выход модели или экстрактора — **только артефакт** (`raw_artifact_id`, `native_raw_artifact_id`,
  `ocr_raw_artifact_id`, `retention_class = KEEP_RAW`).
- Сырое распознавание объекта дублируется в строку (`text`, `raw_output`): для поиска и пересборки без разбора
  blob.
- MCP-ответ несёт `record_role = CANONICAL` и `raw_artifact_id` (macro `provenance_trace`, §9).

### 2.3 Три вида провенанса (контракт §36)

| Вид | Где | Поля |
|---|---|---|
| processing (L1, сейчас) | конверт выше | `extractor_*`, `model_*`, `pipeline_version`, `processing_run_id`, `extraction_signature`, `config_hash` |
| scientific (L2, будущее) | будущие датасеты evidence и `review_events` | reviewer, reviewed_at, epistemic status, локатор (`object_id` + `raw_content_sha256` + `snapshot_id`) |
| computation (L4, будущее) | будущий SolverRun | world, representation, solver, git commit, окружение, seed, input/output manifests |

**Зарезервированные имена.** В L1 не используются с иным смыслом: `epistemic_status`, `evidence_type`, голые
`scope` и `scale`, `reviewer`, `reviewed_at`, `event_time`, `measurement_time`, `world_id`, `representation_id`,
`solver_run_id`, `seed`. Документный слой не несёт `EpistemicStatus` (K2, CP-03).

### 2.4 Ответ на вопросы §50

| Вопрос | Поля |
|---|---|
| Что это? | `object_kind`, датасет, `schema_version` |
| Где оригинал? | `source_id` → `sources.canonical_path` и `source_sha256`; `page_id`, `page_kind`, bbox |
| Кто/что создал? | `extractor_id`, `extractor_version`, `model_id`, `model_revision`, `processing_run_id` → `processing_runs` (`code_revision`, `host_role`, `models`) |
| Native или OCR? | `origin`, у блоков `text_layer` |
| Auto или reviewed? | `review_status`; у реестровых связей — `curation_status` |
| Версия pipeline? | `pipeline_version`, у прогона `code_revision` |
| Revision модели? | `model_revision`, у прогона `models[]` |
| Source/page? | `source_id`, `page_id`, `page_index`, `printed_page_raw` |
| Можно ли пересобрать? | Да. Сырой выход хранится (`raw_artifact_id`, KEEP_RAW), есть подпись и конфиг прогона (`config_artifact_id`). Нормализация пересобирается без GPU |
| Можно ли удалить projection? | Да: DuckDB, Neo4j и OpenSearch строятся из manifest снимка. `meta.snapshot` говорит, из какого |

## 3. Словари (закрытые, версионируются вместе со схемой)

### 3.1 Ревью и курирование

- **ReviewStatus.** Значения:
  - `UNSEEN`, `QUICK_LOOK_ONLY`, `AUTO_EXTRACTED_UNREVIEWED`, `RELEVANT_SECTIONS_REVIEWED`, `FULLY_REVIEWED` — минимум
    §47;
  - `NOT_APPLICABLE` — только `sources` с lifecycle ≠ ACTIVE.
- **Отображение для `sources` (CP-06):**
  - 001–041 — из покрытия Phase 1: FULLY → FULLY_REVIEWED, RELEVANT → RELEVANT_SECTIONS_REVIEWED;
  - 013 и 022 — NOT_APPLICABLE;
  - 042–195 — UNSEEN;
  - 196–251 — QUICK_LOOK_ONLY. Основание — маркер `INTAKE_QUICK_LOOK_NOT_EVIDENCE` в notes. Проверено: маркер стоит
    ровно у 196–251 (56 источников).
- **Запрещено валидатором в любой колонке статуса L1:** `FACT`, `REVIEWED_MEASUREMENT`, `ACCEPTED_PARAMETER`,
  `ACCEPTED_FORMULA`, а также все значения `EpistemicStatus`. Для origin NATIVE, OCR и DERIVED допустим только
  `AUTO_EXTRACTED_UNREVIEWED`. Статус источника объекты не наследуют.
- **CurationStatus:** `AUTO_PROPOSED`, `CURATED`, `REJECTED`.

### 3.2 Обработка

- **ProcessingStatus** (страница и шаг):
  - минимум §44: `NATIVE_OK`, `OCR_REQUIRED`, `OCR_OK`, `PARTIAL`, `UNSUPPORTED`, `FAILED`, `NEEDS_REVIEW`;
  - добавлены `NOT_PROCESSED` (страница известна по пагинации, но не обработана) и `SKIPPED_BY_REGISTER` (только
    шаг или источник, CP-05).
- **SourceProcessingStatus** (свёртка): `COMPLETE`, `PARTIAL`, `NEEDS_REVIEW`, `FAILED`, `UNSUPPORTED`,
  `NOT_PROCESSED`, `SKIPPED_BY_REGISTER`.
  - Правило свёртки — view `source_status_summary` (§9).
  - Итог всегда сходится: 251 = COMPLETE + PARTIAL/NEEDS_REVIEW + FAILED/UNSUPPORTED + NOT_PROCESSED +
    SKIPPED_BY_REGISTER.
- **SkipReason:** `ARCHIVE_DELETED_AFTER_ASSEMBLY` (013), `RETIRED_NOT_EVIDENCE` (022).
- **NativeTextStatus:** `PRESENT_OK`, `PRESENT_BROKEN`, `PRESENT_PARTIAL`, `ABSENT`, `NOT_APPLICABLE`, `NOT_CHECKED`.
- **OcrStatus:** `NOT_REQUIRED`, `REQUIRED`, `DONE`, `FAILED`, `SKIPPED_BY_POLICY`, `NOT_RUN`.
- **StepOutcome:** `EXECUTED`, `REUSED_CACHED`, `SKIPPED_UP_TO_DATE`, `SKIPPED_BY_POLICY`, `NOT_ATTEMPTED`.
- **RunStatus:** `STARTED`, `SUCCEEDED`, `PARTIAL`, `FAILED`, `ABORTED`.
- **RunKind:** `REGISTRY_IMPORT`, `EXTRACTION`, `OCR`, `DERIVATION`, `SNAPSHOT_BUILD`, `SCHEMA_MIGRATION`,
  `VALIDATION`.
- **Stage.** Предложение; окончательный список — за агентом C, но закрытым enum: `INSPECT`, `PAGINATE`, `RENDER`,
  `NATIVE_TEXT`, `NATIVE_LAYOUT`, `NATIVE_VECTOR`, `OCR`, `TABLES`, `FORMULAS`, `FIGURES`, `CAPTIONS`, `BIBLIOGRAPHY`,
  `NORMALIZE`, `COMMIT`.
- **ErrorCode:** `SOURCE_FILE_MISSING`, `SOURCE_SHA256_MISMATCH`, `SOURCE_SIZE_MISMATCH`, `SOURCE_UNREADABLE`,
  `FORMAT_UNSUPPORTED`, `ENCRYPTED_NO_PERMISSION`, `PAGINATION_FAILED`, `PAGECOUNT_MISMATCH`, `RENDER_FAILED`,
  `NATIVE_EXTRACT_FAILED`, `OCR_FAILED`, `OCR_TIMEOUT`, `OCR_OOM`, `MODEL_UNAVAILABLE`, `RAW_OUTPUT_UNPARSEABLE`,
  `ARTIFACT_WRITE_FAILED`, `ARTIFACT_HASH_MISMATCH`, `ID_COLLISION`, `SCHEMA_VALIDATION_FAILED`, `COMMIT_FAILED`,
  `INTERNAL_ERROR`. Новый код — MINOR-версия схемы.
- **Severity:** `ERROR`, `FATAL`. **HostRole:** `WORKSTATION`, `CORE`, `EDGE`.

### 3.3 Флаги качества, форматы, объекты

**QualityFlag** — закрытый список; у каждого флага есть область применения:

| Флаг | Где |
|---|---|
| `LOW_OCR_CONFIDENCE`, `BROKEN_TEXT_LAYER`, `MIXED_TEXT_LAYER`, `NATIVE_OCR_DISAGREE`, `ENCODING_REPAIRED`, `LANGUAGE_UNCERTAIN`, `TRUNCATED` | pages, blocks, tables, formulas, bibliography_entries |
| `ROTATED`, `SKEW_CORRECTED`, `LOW_RESOLUTION_SCAN`, `TWO_UP_SPREAD`, `EMPTY_PAGE`, `DUPLICATE_PAGE_CANDIDATE`, `FOREIGN_WORK_CONTENT` | pages |
| `BBOX_APPROX`, `CROSS_PAGE_CONTINUATION`, `DUPLICATE_DETECTION_DISAMBIGUATED` | объекты страницы |
| `CAPTION_NOT_FOUND`, `CAPTION_ASSOCIATION_UNCERTAIN`, `FIGURE_TYPE_LOW_CONFIDENCE` | figures, tables |
| `TABLE_STRUCTURE_UNCERTAIN` | tables |
| `FORMULA_LATEX_UNPARSEABLE` | formulas |
| `SIGNATURE_AFTER_BOM`, `ENCRYPTED_SOURCE`, `REPAIRED_DERIVED_COPY_USED`, `PAGECOUNT_DIFFERS_FROM_REGISTER_HINT` | sources, documents |

Прочие словари:

- **FigureType:** `MAP`, `MINE_PLAN`, `GEOLOGICAL_SECTION`, `GEOLOGICAL_COLUMN`, `CHART`, `PHOTO`,
  `SCHEMATIC_DIAGRAM`, `RADARGRAM`, `TABLE_IMAGE`, `OTHER`, `UNKNOWN_FIGURE_TYPE`.
  - Тип ≠ UNKNOWN только если `figure_type_method ≠ NONE` и `figure_type_confidence ≥ figure_type_threshold`.
  - Иначе `UNKNOWN_FIGURE_TYPE` и флаг `FIGURE_TYPE_LOW_CONFIDENCE`: не MINE_PLAN «по похожести» (§22).
- **FigureTypeMethod:** `MODEL_CLASSIFIER`, `HEURISTIC`, `CURATED`, `NONE`.
- **FigureLayoutClass:** `RASTER_IMAGE`, `VECTOR_GRAPHICS`, `MIXED`, `LAYOUT_REGION`.
- **BlockType:** `TEXT`, `HEADING`, `CAPTION`, `LIST_ITEM`, `FOOTNOTE`, `PAGE_HEADER`, `PAGE_FOOTER`, `PAGE_NUMBER`,
  `REFERENCE_LIST`, `TABLE_OF_CONTENTS`, `OTHER`, `UNKNOWN`.
- **TextLayer:** `PDF_TEXT_LAYER`, `DJVU_TEXT_LAYER`, `OCR_MODEL`, `EPUB_XHTML`, `DOCX_XML`.
- **TableRawFormat:** `HTML`, `MARKDOWN`, `OTSL`, `JSON`, `TEXT`, `DOCX_XML`, `XHTML`.
- **FormulaKind:** `DISPLAY`, `INLINE`, `UNKNOWN`. **FormulaRawFormat:** `LATEX`, `OMML`, `MATHML`, `TEXT`,
  `IMAGE_ONLY`.
- **PageKind:** `PDF_PAGE`, `DJVU_PAGE`, `DOCX_RENDERED_PAGE`, `EPUB_SPINE_ITEM`, `IMAGE_FRAME`.
- **PaginationBasis:** `PDF_PAGE_TREE`, `DJVU_PAGE_ORDER`, `DOCX_PINNED_RENDER`, `EPUB_SPINE`, `IMAGE_FRAMES`.
- **DocumentClass:** `NATIVE_TEXT`, `VECTOR_HEAVY`, `RASTER_SCAN`, `MIXED`, `BROKEN_TEXT_LAYER`, `REFLOWABLE`,
  `UNKNOWN`.
- **FileFormat:** `PDF`, `DJVU`, `EPUB`, `DOCX`, `ZIP`, `IMAGE`, `UNKNOWN`.
  - Сигнатура: `%PDF` (допускается перед ним UTF-8 BOM, тогда флаг `SIGNATURE_AFTER_BOM`), `AT&TFORM`, `PK` +
    `mimetype` (EPUB) или `[Content_Types].xml` (DOCX).
- **FileStatus:** `PRESENT_VERIFIED`, `MISSING`, `SHA256_MISMATCH`, `SIZE_MISMATCH`, `LFS_POINTER_ONLY`, `UNREADABLE`.
- **LifecycleStatus:** `ACTIVE`, `ABSENT_BY_REGISTER`, `RETIRED` (CP-05).
- **ArtifactKind:** `PAGE_RENDER`, `FIGURE_CROP`, `TABLE_CROP`, `FORMULA_CROP`, `EMBEDDED_IMAGE`, `VECTOR_PATHS`,
  `NATIVE_RAW`, `OCR_RAW`, `DOCX_RENDERED_PDF`, `REPAIRED_COPY`, `RUN_CONFIG`, `VALIDATION_REPORT`.
- **RetentionClass:**
  - `KEEP_RAW` — `OCR_RAW`, `NATIVE_RAW`, `DOCX_RENDERED_PDF`, `REPAIRED_COPY`, `RUN_CONFIG`, `VALIDATION_REPORT`:
    автоматически не удаляются никогда;
  - `KEEP_REFERENCED` — рендеры и кропы: удаляются только когда на них не ссылается ни один сохранённый снимок.
- **DatePrecision:** `day`, `month`, `year`, `decade`, `unknown` — как `vkm_world.core.provenance.DATE_PRECISIONS`,
  в нижнем регистре.

### 3.4 Область источника (CP-07): сырое значение → `vkm_world.Scope` → тип отображения

| `site_scope_raw` | n | `site_scope` | `site_scope_mapping` |
|---|---|---|---|
| GENERAL_METHOD | 79 | [GENERAL_METHOD] | EXACT |
| VKM_REGIONAL | 76 | [VKM_REGIONAL] | EXACT |
| OTHER_VKM_SITE | 33 | [OTHER_VKM_SITE] | EXACT |
| NON_VKM_ANALOG | 30 | [NON_VKM] | SYNONYM (метка «ANALOG» — не эпистемический ANALOGUE) |
| SKRU1 | 9 | [SKRU1] | EXACT (уровень источника) |
| METHOD_GENERAL | 4 | [GENERAL_METHOD] | SYNONYM |
| VKM_Berezniki | 3 | [OTHER_VKM_SITE] | LOSSY |
| VKM_REGIONAL_and_SKRU1 | 3 | [VKM_REGIONAL, SKRU1] | MULTI |
| OTHER_POTASH_SITE | 2 | [OTHER_POTASH_SITE] | EXACT |
| SKRU1_SKRU2_SKRU3 | 2 | [] — кандидаты: SOLIKAMSK_GROUP; SKRU1, SKRU2, SKRU3 | AMBIGUOUS |
| VKM_Uralkali | 2 | [VKM_REGIONAL] | LOSSY |
| SKRU1_SKRU2_PILLAR | 2 | [SKRU1_SKRU2_PILLAR] | EXACT |
| VKM_regional | 1 | [VKM_REGIONAL] | CASE |
| VKM_Solikamsk | 1 | [] — кандидат: SKRU1_OR_SKRU2_UNATTRIBUTED | AMBIGUOUS |
| other_potash_deposit | 1 | [OTHER_POTASH_SITE] | SYNONYM (регистр и синоним) |
| BKPRU4_VKM | 1 | [BKPRU4] | SYNONYM |
| LEGACY_RETIRED | 1 | [] | NOT_A_SCOPE (это lifecycle RETIRED) |
| SALT_DEPOSITS_USSR_incl_VKM | 1 | [NON_VKM, VKM_REGIONAL] | MULTI |

- Сумма — 251.
- Таблица — версионируемая константа `vkm_corpus.contracts.site_scope`, тест требует, чтобы все 18 значений были
  отображены.
- Реестр на месте не чистится.
- Имена контракта §46 — только crosswalk в документации: `SKRU1_EXACT` ≙ `SKRU1`, `ANALOG` ≙ NON_VKM_ANALOG → NON_VKM.

### 3.5 Координаты: PAGE_SPACE и статус CRS

**PAGE_PT_TL** — единственная система bbox в v0:

- единица — пункт PostScript (1/72 дюйма);
- начало — **левый верхний** угол страницы в отображаемом (upright) виде, после `/Rotate`; x вправо, y вниз;
- бокс — CropBox, пересечённый с MediaBox (без CropBox — MediaBox); смещение бокса вычтено;
- `width_pt` и `height_pt` — размер после поворота.

Как в неё приводятся другие представления:

| Представление | Преобразование |
|---|---|
| PDF | нативная геометрия: по правилам выше. PyMuPDF отдаёт координаты неповёрнутой страницы, их надо умножить на матрицу поворота (реализует C) |
| DjVu | пиксели изображения из INFO: `pt = px · 72 / dpi`, поворот по флагам INFO; `native_dpi` сохраняется |
| Растровое распознавание (OCR по рендеру с d dpi) | `pt = px · 72 / d`, флаг `BBOX_APPROX` |
| DOCX | через закреплённый рендер в PDF, по правилам PDF |
| EPUB | `bbox_space = NONE`, bbox NULL; положение — `reading_order` и будущий text span (§4.10) |

Нормированные координаты (`x / width_pt`) и пиксели при любом DPI (`px = pt · dpi / 72`) — производные, во view.

PAGE_PT_TL — **не CRS**. Географические координаты:

- **CoordinateSpace:** `PAGE_SPACE`, `IMAGE_PIXEL_SPACE`, `GEO`, `NONE`.
- **CrsStatus:** `EXACT_COORDINATED`, `LOCAL_COORDINATES`, `UNKNOWN_CRS`, `MAP_DIGITIZED`, `RELATIVE`, `SCHEMATIC`,
  `UNKNOWN`. Поле допустимо только при `coordinate_space = GEO`.
- EPSG — только вместе с `EXACT_COORDINATED` и основанием `PRINTED_IN_SOURCE` или `CURATED`. **Никогда не выводится
  автоматически.**
- В v0 строк с GEO нет: валидатор это проверяет.
- Векторные артефакты несут `coordinate_space = PAGE_SPACE` и матрицу PDF user space → PAGE_PT_TL. Оцифровка в
  географию — будущая DERIVATION (контракт §41), не факт источника.

### 3.6–3.8 Work, связи, авторы, сопоставление

- **WorkType:** `JOURNAL_ARTICLE`, `CONFERENCE_PAPER`, `BOOK_CHAPTER`, `MONOGRAPH`, `TEXTBOOK`, `TEACHING_MANUAL`,
  `TRAINING_MANUAL`, `PRACTICE_MANUAL`, `DISSERTATION`, `DISSERTATION_ABSTRACT`, `THESIS`, `PROCEEDINGS_VOLUME`,
  `JOURNAL_ISSUE`, `NORMATIVE_DOCUMENT`, `METHODICAL_GUIDANCE`, `TECHNICAL_REPORT`, `INSTITUTIONAL_REPORT`,
  `PRESENTATION`, `PATENT`, `DATASET`, `BIBLIOGRAPHIC_INDEX`, `REFERENCE_TABLES`, `PROJECT_DATA_PACKAGE`, `OTHER`,
  `UNKNOWN`.
  - Жанр работы. «Природа копии» (`book_archive`, `derived_convenience_pdf`, `front_matter`, `retired_legacy_archive`)
    в тип работы не переходит: она выражается `link_type` и `source_relations` (K4).
- **WorkStatus:** `ACTIVE`, `MERGED_INTO`, `WITHDRAWN`.
- **SourceWorkLinkType:** `FULL_COPY`, `PARTIAL_COPY`, `FRONT_MATTER_ONLY`, `PART`, `FOREIGN_CONTENT`; `CONTAINS_WORK`
  зарезервирован для курированных дочерних работ.
- **WorkRelationType:**
  - направленные: `ABSTRACT_OF` (автореферат → диссертация), `EDITION_OF`, `TRANSLATION_OF`;
  - симметричные: `VOLUME_SET_SIBLING` (229 ↔ 230), `SERIES_SIBLING` (020 ↔ 028), `COMPANION_OF` (002 ↔ 201),
    `NOT_SAME` (037 ↔ 014 и др.).
- **SourceRelationType:** `DERIVED_FROM`, `CONTAINS_COPY_OF`, `SHARES_PAGES_WITH`.
- **MetadataBasis:** `TITLE_PAGE_VERIFIED`, `TEXT_LAYER_VERIFIED`, `EPUB_OPF_METADATA`, `EXTERNAL_CATALOGUE`
  (Crossref, OpenAlex, dissercat), `HUNT_TABLE`, `PHASE1_COVERAGE_MASTER`, `REGISTER_NOTES`,
  `CATALOGUE_DATA_UNVERIFIED`, `FILENAME_PAGECOUNT_UNVERIFIED`, `FILE_EMBEDDED_METADATA`, `CURATED_MANUAL`, `UNKNOWN`.
- **IdentityStatus (Work):** `VERIFIED_IN_FILE`, `CATALOGUE_UNVERIFIED`, `FILENAME_PAGECOUNT_UNVERIFIED`.
- **ExternalIdScheme:** `PWL`, `CW`, `WG`, `EXT_SRC`, `EXTWEB`, `DOI`, `ISBN`, `ASIN`, `URN_UUID`, `OPENALEX`.
  **ExternalIdRelation:** `SAME_WORK`, `COMPONENT`, `OTHER_EDITION`, `NOT_SAME`, `CITED_AS`.
- **AuthorRole:** `AUTHOR`, `EDITOR`, `COMPILER`, `SUPERVISOR`, `TRANSLATOR`, `CORPORATE_AUTHOR`, `UNKNOWN`.
  **Script:** `CYRL`, `LATN`, `MIXED`, `OTHER`.
- **Идентичность:** `NAME_KEY_ONLY`, `CURATED`; у venue ещё `ISSN`. **VenueType:** `JOURNAL`, `PROCEEDINGS_SERIES`,
  `BOOK_SERIES`, `UNKNOWN`.
- **MatchMethod:** `DOI_EXACT`, `ISBN_EXACT`, `TITLE_AUTHOR_YEAR`, `TITLE_YEAR`, `PHASE1_CITATION_GRAPH`, `CURATED`.
  **MatchStatus:** `CANDIDATE`, `ACCEPTED_EXACT_ID`, `ACCEPTED_CURATED`, `REJECTED`.
- **CitingWorkResolution:** `UNIQUE_LINK`, `RANGED_COMPONENT`, `AMBIGUOUS`, `FOREIGN_CONTENT`, `NO_LINK`.
- **AvailableFromBasis:** `UNKNOWN`, `CURATED`. Вне канона, только во view: `ASSUMED_FROM_PUBLICATION`.
- **IngestionBasis:** `INTAKE_MANIFEST`, `MIGRATION_SOURCE_TEXT`, `REGISTER_GIT_HISTORY`, `UNKNOWN`.
- **PrintedLabelOrigin:** `PDF_PAGE_LABELS`, `DETECTED_RUNNING_HEAD`, `CURATED`, `NONE`. **PrintedLabelParseStatus:**
  `PARSED`, `UNPARSED`, `NONE`.

## 4. Контракт ID (владелец — D, проверка — H)

### 4.1 Принципы

1. ID не зависит от внутренних ID баз (Neo4j, DuckDB, PostgreSQL), от номеров строк и порядка файлов.
2. ID детерминирован там, где у объекта есть естественный якорь. Прогон и снимок — события, их ID уникальны, а не
   детерминированы.
3. ID никогда молча не меняет смысла. Если меняется то, что ID обозначает, появляется новый ID и связь lineage или
   tombstone.
4. ID не бывает именем файла: `:` запрещён на Windows (K6). Файлы адресуются хешем содержимого или партициями
   `key=value` без `:`.
5. Новые префиксы не пересекаются с занятыми: `VKM-SRC-`, `EXT-SRC-`, `EXTWEB-`, `EV-…`, `PAR-`, `CW-`, `WG-`, `CG-`,
   `LIN-`, `FAM-`, `PWL-`, `MM-`, `MGEO-`, `MON-`, `PCF-`, `QA-C-`, `XF-`, `PC-`, `F-…` (аудит A K6).

### 4.2 Грамматика

| ID | Регулярное выражение | Как строится | Пример |
|---|---|---|---|
| source | `^VKM-SRC-[0-9]{3}$` | реестр | `VKM-SRC-243` |
| document | `^VKM-SRC-[0-9]{3}:doc$` | `<source_id>:doc` | `VKM-SRC-243:doc` |
| page | `^VKM-SRC-[0-9]{3}:[prs][0-9]{4}$` | `<source_id>:<unit><индекс с 1, 4 знака>` | `VKM-SRC-243:p0001`, `VKM-SRC-023:r0042`, `VKM-SRC-249:s0017` |
| объект страницы | `^VKM-SRC-[0-9]{3}:[prs][0-9]{4}:[bftmc][0-9a-f]{12}$` | §4.4; b блок, f рисунок, t таблица, m формула, c запись библиографии | `VKM-SRC-243:p0012:f3a91c07d2e4b` |
| work | `^VKM-WRK-[0-9]{3}$` | номер якорного источника (§4.5) | `VKM-WRK-013` |
| author | `^AUT-[0-9a-f]{12}$` | §4.6 | `AUT-5e0c91a2b7d4` |
| venue | `^VEN-[0-9a-f]{12}$` | §4.6 | `VEN-0b1c2d3e4f50` |
| artifact | `^sha256:[0-9a-f]{64}$` | sha256 байтов | `sha256:ab12…` |
| processing run | `^RUN-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$` | UTC-старт + 8 случайных hex | `RUN-20260928T142233Z-3f9a2c1b` |
| step | `^STP-[0-9a-f]{16}$` | sha256(run, source, page, stage, attempt) | — |
| error | `^ERR-[0-9a-f]{16}$` | sha256(run, step, code, seq) | — |
| commit | `^CMT-[0-9a-f]{16}$` | sha256 канонического тела маркера без `commit_id` и времени | — |
| snapshot | `^snap-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$` | UTC + sha256 тела manifest | `snap-20260928T150000Z-1a2b3c4d` |

Строки связей: `SWL-`, `WRL-`, `SRL-`, `WAU-`, `BML-`, `OLN-` плюс 16 hex. Префикс — sha256 от строки
`<prefix>-v1` и составного ключа, поля разделены `|`:

- SWL — `source_id`, `work_id` или `-`, `link_type`, `page_start` или `-`, `page_end` или `-`;
- WRL — `from_work_id`, `relation`, `to_work_id`;
- SRL — `from_source_id`, `relation`, `to_source_id`, `from_page_start` или `-`, `to_page_start` или `-`;
- WAU — `work_id`, `ordinal`;
- BML — `entry_id`, `cited_work_id`, `match_method`;
- OLN — `old_object_id`, `new_object_id`.

Зарезервировано (в v0 невалидно; добавление — MINOR, только расширение регулярного выражения):

- `VKM-SRC-[0-9]{4}` — после 999 источников;
- `VKM-WRK-[0-9]{3}\.[0-9]{2}` — курированные дочерние работы контейнера;
- `VKM-WRK-X[0-9]{5}` — работы без Source: внешние EXT-SRC, EXTWEB, CW;
- `VKM-SRC-[0-9]{3}/m[0-9]{3}` — члены ZIP.

### 4.3 `page_id`: единицы страниц

| Буква | Единица | Правило индекса | Связь с evidence Phase 1 |
|---|---|---|---|
| `p` | физическая страница PDF или DjVu (или кадр изображения) | порядок дерева страниц PDF, как его перечисляет библиотека; порядок `FORM:DJVU` DjVu. `DJVI` — не страницы (053: 384) | `pdf_page` = индекс → `EXACT_PHYSICAL_INDEX` |
| `r` | страница **закреплённого** рендера (DOCX 023) | рендер DOCX → PDF с профилем (версия LibreOffice, хеш шрифтов). PDF рендера — артефакт `DOCX_RENDERED_PDF`, на него ссылается `documents.pagination_artifact_id` | `RENDER_DEPENDENT`: 538 записей, пока не подтверждено совпадение 115 страниц и хешей текстов Phase 1 (CP-08) |
| `s` | spine-элемент EPUB | порядок `itemref` в OPF (391 у 249), все элементы, с 1 | `NOT_APPLICABLE`: печатной пагинации нет |

Что ещё важно:

- Разворот (037, 243) — одна физическая страница со списком `printed_page_labels`. Логических полустраниц в v0 нет
  (CP-08). Позже они выражаются регионом (§4.10), новой Page не становятся.
- Печатные метки никогда не входят в ID.
- Смена рендерера DOCX меняет пагинацию. Это новый профиль, явная миграция с receipt и `object_lineage`. Тихая
  переинтерпретация `r0042` запрещена: валидатор сверяет `pagination_render_profile` с прошлым снимком.

### 4.4 ID объекта страницы: регион плюс производитель

Сначала вычисляется ключ производителя:

```text
producer_key = sha256("vkm-producer-v1|" + extractor_id + "|" + extractor_version + "|" +
                      (model_id or "-") + "|" + (model_revision or "-") + "|" + raw_config_hash)
```

Затем якорь:

```text
anchor       = "bbox:" + f(x0) + "," + f(y0) + "," + f(x1) + "," + f(y1)
               # PAGE_PT_TL; f(v) = формат "%.1f" от round(v, 1), -0.0 → 0.0
             | "ord:" + "%05d" % reading_order + "|txt:" + sha256(nfkc_casefold(text))[:16]
               # только bbox_space = NONE: EPUB и блоки DOCX без геометрии
             [+ "|dup:" + n]   # n-й одинаковый якорь того же (page, kind, origin, producer); флаг DUPLICATE_DETECTION_DISAMBIGUATED
```

И сам ID:

```text
h12       = sha256("vkm-docobj-v1|" + page_id + "|" + object_kind + "|" + origin + "|" + anchor + "|" + producer_key)[:12]
object_id = page_id + ":" + kind_code + h12        # kind_code: b f t m c
```

`raw_config_hash` — хеш части конфигурации, влияющей на **сырой** выход: DPI рендера, режим и промпт OCR, пороги
детектора. Настройки нормализатора в него не входят, `pipeline_version` тоже.

Свойства:

- **Тот же прогон повторно** даёт те же ID: всё входное детерминировано. Проба: сдвиг float на 1e-8 не меняет ID.
- **Изменилась только нормализация** — ID тот же. `raw_content_sha256` не меняется, меняется `content_sha256`.
  Будущие ссылки L2 закрепляют `raw_content_sha256`, значит, смысл ID стабилен.
- **Новый детектор, распознаватель или ревизия модели** дают новый `producer_key` и новые ID. Связь старого с новым —
  строки `object_lineage`: IoU bbox ≥ порога и тот же вид; иначе `NO_SUCCESSOR`. Молчаливой перенумерации нет (урок
  `vn_index`).
- **Другой слой** (NATIVE или OCR) с тем же bbox даёт другой ID: `origin` входит в хеш.
- **Коллизии.** Уникальность нужна только внутри префикса `page_id:kind`. На страницу приходятся сотни объектов, а
  48 бит дают вероятность коллизии около 10⁻¹⁰. Всё равно писатель проверяет уникальность и при коллизии падает с
  `ID_COLLISION` — никакой тихой дедупликации.
- **Инвариант для H:** один `object_id` ⇒ один `raw_content_sha256` во всех снимках. Валидатор сверяет с прошлым
  снимком (§11, B07).

### 4.5 `work_id` (CP-09) и курируемые файлы Work

**Якорь** — наименьший номер VKM-SRC в явно подтверждённой группе. В группу входят связи FULL_COPY, PARTIAL_COPY,
PART и FRONT_MATTER_ONLY, в том числе с источниками ABSENT_BY_REGISTER. Пример: 013/025/202 → `VKM-WRK-013`.
Одиночный источник — свой номер.

**Слияние, найденное позже.** Выживает ID с наименьшим якорем. Остальные становятся tombstone `MERGED_INTO`.
tombstone не удаляется и не переиспользуется. macro `resolve_work` разворачивает цепочку (проверено, §9).

**Разделение.** Якорь сохраняет свой ID. Отделённая группа получает ID своего наименьшего номера. Если этот ID был
tombstone *той же* сущности, он реактивируется курированной записью. Чужой сущности его не выдают никогда.

**Контейнеры** (089, 193, 203, 204, 205–207): в v0 один Work на файл. PWL-строки компонентов лежат в
`external_ids` с `relation = COMPONENT`. Противоречивое «contains» у 205 в кандидаты не берётся (аудит A).

**Файлы** (CP-09) — PRIVATE `00_registry/work_registry/`:

- `WORK_REGISTER.csv` — строка на `work_id`, включая tombstone. Колонки:
  - идентичность: `work_id, status, merged_into, anchor_source_id, work_type`;
  - описание: `title, title_en, title_variants, authors, year_raw, year, venue, volume, issue, pages, edition,
    publisher, city, doi, isbn, language, external_ids`;
  - основания: `basis_title, basis_authors, basis_year, basis_venue, basis_identifiers, identity_status`;
  - доступность и статус: `available_from, available_from_precision, available_from_basis, curation_status, notes`.
  - Списки внутри ячейки — через `;`. `external_ids` — `scheme:value:relation`.
- `WORK_LINKS.csv` — все курируемые рёбра одного закрытого словаря: `link_kind` (SOURCE_WORK | WORK_WORK |
  SOURCE_SOURCE), `from_id, relation, to_id, from_page_start, from_page_end, to_page_start, to_page_end, part_label,
  printed_range, basis, curation_status, notes`.
  - У каждого `relation` фиксированная сигнатура типов: загрузчик проверяет её и раскладывает строки по трём
    канонических датасетам.
  - Имена `from_id` и `to_id` выбраны, чтобы не путать с `object_id` конверта.
- `RECEIPT.json` — входы, их sha256, версия инструмента, дата.
- `README_RU.md` — правила.

**Seed** из аудита A §2.8 (все строки CURATED после проверки человеком):

- SAME_WORK 013/025/202 и 147/232 — одна работа, связи FULL_COPY, PARTIAL_COPY или FRONT_MATTER_ONLY;
- PART 208/209: печатные 1–100 и 101–199;
- VOLUME_SET_SIBLING 229/230; SERIES_SIBLING 020/028; ABSTRACT_OF 001 → 196; COMPANION_OF 002/201;
- NOT_SAME 037/014;
- 011 — второе издание: первое вне корпуса, поэтому `external_ids` с `OTHER_EDITION`;
- 025 DERIVED_FROM 013; 022 CONTAINS_COPY_OF 023;
- SHARES_PAGES_WITH и FOREIGN_CONTENT для «Горного эха» и чужих начальных страниц.

**Bootstrap.**

- Команда `vkm-corpus registry propose-works` (код в PUBLIC) пишет предложения в `$VKM_WORK/work_registry_proposal/`.
  В них:
  - одиночные связи `BOOTSTRAP_SINGLETON`;
  - метаданные по порядку источников из §13.2;
  - кандидаты из notes — `AUTO_PROPOSED`;
  - отчёт неоднозначностей: PWL-0007 и PWL-0127, контейнеры, 205.
- Человек или координатор проверяет и коммитит в PRIVATE с receipt.
- Каноническая сборка **никогда не выдумывает** `work_id`: если у источника нет главной связи, это блокирующая
  ошибка.

### 4.6 Авторы и venue: ключи без агрессивного слияния

Ключ имени строится так:

1. NFKC, casefold, `ё → е`.
2. Точки и дефисы-разделители → пробел, пробелы схлопнуть.
3. Первый токен — фамилия; однобуквенные токены — инициалы, склеенные вместе; остальные — полные имена.
4. Добавить письменность (CYRL, LATN, MIXED).

Результат: `фамилия|инициалы|полные|письменность`, `author_id = "AUT-" + sha256("vkm-author-v1|" + key)[:12]`.

Проверено пробой:

- `Иванов И. И.` ≡ `Иванов И.И.`;
- `Королёв` ≡ `Королев`;
- `Ivanov I.I.` ≠ `Иванов И.И.` — письменность разная;
- `Иванов Иван Иванович` ≠ `Иванов И.И.` — инициалы и полное имя не сливаются.

Ограничение (DN-D-11): у однофамильцев с одинаковыми инициалами общий ключ. Поэтому `identity_status =
NAME_KEY_ONLY` помечает узел как «строку имени», а не проверенного человека. Для оценки независимости источников
авторитетны курируемые семьи Phase 1, а не граф авторов. Транслитерационные пары связываются только курированным
`same_as_author_id`.

**venue:** `VEN-` + sha256 от `ISSN-L`, если он курирован, иначе от нормализованного заглавия с письменностью.
Переводной журнал ≠ оригинал.

### 4.7 Артефакты и раскладка

- `artifact_id = "sha256:" + sha256(bytes)`.
- Хранилище: `$VKM_DATA_ROOT/artifacts/<kind_dir>/<hex[0:2]>/<hex[2:4]>/<hex>.<ext>`.
  - `kind_dir` ∈ `page_renders, figures, tables, formulas, embedded, vector, native_raw, ocr_raw, docx_render,
    repaired, configs, reports`.
  - `ext` выводится из `media_type`.
- Имя файла — хеш содержимого: ID с `:` в путь не попадают.
- Blob пишется один раз: если файл уже есть и хеш сверен, повтор — no-op. Записанный файл не изменяется.
- Дедупликация по содержимому. Контекст (страница, DPI) — в ссылающейся строке.

### 4.8 Прогон, шаг, ошибка, коммит, снимок

См. таблицу §4.2. Для PostgreSQL `processing_run_id` — внешний ключ, а не порядковый номер control plane. Канон
самодостаточен: его можно восстановить, потеряв PostgreSQL.

### 4.9 Если меняется sha256 файла источника

Связка `source_id ↔ source_sha256` **неизменна**. Такова практика реестра: другая цифровая копия получает новый id,
например 202 рядом с 025.

- Сборка `sources` сравнивает sha с предыдущим снимком. Если sha сменился, это блокирующая ошибка
  `SOURCE_BINDING_CHANGED`.
- Снять блок может только явная correction-запись PRIVATE с receipt (решение координатора).
- При разрешённой перепривязке все head-партиции источника становятся недействительными и пересобираются: подпись
  содержит `source_sha256`.
- Старые `page_id` и `object_id` остаются в прошлых снимках. Будущие ссылки L2 хранят `source_sha256` и
  `raw_content_sha256`, поэтому drift обнаруживается.
- У каждой документной строки есть `source_sha256`. Валидатор требует: строка = маркер коммита = реестр.

### 4.10 Будущие якоря — только грамматика, без таблиц

- **Регион страницы:** `<page_id>#bbox=<x0>,<y0>,<x1>,<y1>`. Координаты PAGE_PT_TL с шагом 0,1 pt. Пример:
  `VKM-SRC-037:p0126#bbox=0.0,0.0,595.2,841.9` — левая полустраница разворота.
- **Фрагмент текста:** `<object_id>#chars=<start>-<end>@<raw_content_sha256[:12]>`. Смещения — по `text` объекта с
  закреплённым содержимым.

### 4.11 Crosswalk с Phase 1

- **Evidence → страница.** `record_index.csv` (PUBLIC; 13 572 записи: `vn_id, source_id, pdf_page`) даёт `page_id =
  source_id + ":p" + %04d(pdf_page)` для PDF и DjVu. Для 023 — `:r…` со статусом `RENDER_DEPENDENT`. Будущая
  производная таблица `evidence_page_link` (аудит A §5); в v0 не строится.
- **Граф цитирований.** `SOURCE_CITATION_GRAPH.csv` с `citing_locator` вида «pdf p.N | ref K» ведёт к кандидату
  (`page_id`, `entry_label = K`) → `bibliography_entries`, `match_method = PHASE1_CITATION_GRAPH`, статус CANDIDATE
  до курирования. Граф Phase 1 — оракул, не авто-объекты (K11).
- **Метки рисунков.** `figure_label` из `locator_extra` сопоставляется с `figures.figure_label`. Пример из
  синтетики: «Рис. 1.3» на `p0011`. Результат — кандидат, не факт.

## 5. Версионирование схем и manifest

### 5.1 semver датасета

У каждого датасета своя версия: `vkm_corpus.contracts.registry.DATASETS[name].version`. Правила:

| Изменение | Уровень |
|---|---|
| удаление или переименование колонки, смена типа или nullability в сторону строгости, смена семантики, грамматики ID или хеш-алгоритма (`vkm-docobj-v1` → `v2`) | MAJOR |
| новая nullable-колонка, новое значение enum, новый код ошибки | MINOR |
| описания, ужесточение валидатора без изменения данных | PATCH |

До заморозки полного прогона (Phase 7) версии имеют вид `0.MINOR.PATCH`, и повышение MINOR считается несовместимым
(как в Cargo). На снимке полного прогона схема замораживается как `1.0.0` (DN-D-09).

### 5.2 Где записана версия

Key-value метаданные каждого Parquet (проверено: читаются pyarrow и DuckDB `parquet_kv_metadata`):

- `vkm.dataset`, `vkm.schema_version`;
- `vkm.schema_fingerprint` — sha256 канонического списка `[имя, тип Arrow, nullable]`;
- `vkm.pipeline_version`, `vkm.processing_run_id`;
- `vkm.source_id` (для партиций источника), `vkm.commit_id`.

Строка несёт `schema_version`: колонка с одним значением, словарное кодирование почти не занимает места. Полная
Arrow-схема хранится в файле (`store_schema=True`).

### 5.3 Совместимость и чтение

- В одном снимке датасет может смешивать только совместимые версии: один MAJOR, до 1.0 — один MINOR. Список версий
  — в manifest.
- DuckDB читает только с `union_by_name = true`: без него лишние колонки молча теряются (проба §0.2).
- polars читает с явной `schema=` текущей версии и `missing_columns="insert"`.
- Неизвестное значение enum в старом коде — FAIL валидатора. Код обновляется вместе со схемой: это MINOR с
  обязательным обновлением проверок.

### 5.4 Миграции — только воспроизводимые

Старые файлы не мутируются. Есть два пути:

1. **Пересборка.** Прогон с новой версией. Сырой OCR хранится, поэтому нормализация и переупаковка идут без GPU.
2. **Детерминированное преобразование** `run_kind = SCHEMA_MIGRATION`: читает старые партиции, пишет новые.
   Конверт строк (провенанс содержимого) сохраняется. В KV и `processing_steps` пишется `migrated_by_run_id`,
   входные и выходные хеши попадают в receipt.

Старые файлы остаются в прошлых снимках до GC.

### 5.5 Маркер коммита источника: `canonical/_commits/run=<RUN>/<SID>.json`

```json
{
 "commit_format": "1",
 "commit_id": "CMT-5d0c2f9a1b3e4c6d",
 "source_id": "VKM-SRC-243",
 "source_sha256": "<64 hex>",
 "processing_run_id": "RUN-20260928T142233Z-3f9a2c1b",
 "parent_commit_id": null,
 "committed_at": "2026-09-28T14:40:02Z",
 "pipeline_version": "0.1.0",
 "document_processing_status": "COMPLETE",
 "page_count": 74,
 "datasets": {
  "pages": {
   "path": "pages/source_id=VKM-SRC-243/run=RUN-20260928T142233Z-3f9a2c1b/part-00000.parquet",
   "sha256": "<64 hex>", "bytes": 48213, "rows": 74,
   "schema_version": "0.1.0", "schema_fingerprint": "<64 hex>", "content_fingerprint": "<64 hex>"
  },
  "blocks": {"path": "blocks/source_id=VKM-SRC-243/run=RUN-20260928T142233Z-3f9a2c1b/part-00000.parquet", "rows": 1911}
 },
 "log_files": ["processing_steps/run=RUN-20260928T142233Z-3f9a2c1b/part-00003.parquet"],
 "artifact_index_files": ["artifacts/run=RUN-20260928T142233Z-3f9a2c1b/source_id=VKM-SRC-243/part-00000.parquet"],
 "carried_over_rows": {"pages": 70, "blocks": 1850}
}
```

Правила маркера:

- В маркере перечислены **все** документные датасеты источника, даже с 0 строк: файл-«пустышка» со схемой.
- `commit_id` = sha256 канонического тела без `commit_id` и `committed_at`. Одинаковое содержимое даёт одинаковый
  id, и такой коммит можно пропустить как no-op.
- `content_fingerprint` — sha256 канонических JSON-строк, отсортированных по PK, без `created_at` и
  `processing_run_id`. Это «детерминизм на уровне содержимого» из рекомендации A п. 11.

### 5.6 Manifest снимка: `canonical/_snapshots/<snapshot_id>.json` и `canonical/CURRENT`

```json
{
 "manifest_version": "1.0.0",
 "snapshot_id": "snap-20260928T150000Z-1a2b3c4d",
 "parent_snapshot_id": "snap-20260928T120000Z-0a1b2c3d",
 "created_at": "2026-09-28T15:00:00Z",
 "created_by_run_id": "RUN-20260928T150000Z-77aa01bc",
 "inputs": {
  "public_code_revision": "<git sha>", "code_dirty": false, "pipeline_version": "0.1.0",
  "private_registry_revision": "<git sha>", "source_register_sha256": "<64 hex>",
  "work_register_sha256": "<64 hex>", "work_links_sha256": "<64 hex>"
 },
 "datasets": {
  "sources": {"kind": "REGISTRY_GLOBAL", "schema_version": "0.1.0", "schema_fingerprint": "<64 hex>",
              "files": [{"path": "sources/run=RUN-20260928T120501Z-0c0c0c0c/part-00000.parquet", "sha256": "<64 hex>", "bytes": 91234, "rows": 251}],
              "rows": 251, "content_fingerprint": "<64 hex>"},
  "pages": {"kind": "HEAD_PER_SOURCE", "schema_versions": ["0.1.0"], "files": [], "rows": 26481},
  "processing_steps": {"kind": "APPEND_LOG", "files": [], "rows": 131052}
 },
 "source_heads": {"VKM-SRC-001": "CMT-…", "VKM-SRC-243": "CMT-5d0c2f9a1b3e4c6d"},
 "superseded_commits": [],
 "validation": {"status": "PASS", "blocking_failures": 0, "warnings": 3, "report_artifact_id": "sha256:…"},
 "counts": {"sources_total": 251, "sources_complete": 0, "sources_skipped_by_register": 2}
}
```

- JSON детерминирован: отсортированные ключи и списки файлов, LF.
- Пути — относительно `$VKM_DATA_ROOT/canonical/`.
- `CURRENT` — одна строка с `snapshot_id`, заменяется атомарно и только после PASS валидатора.
- Снимок с FAIL можно записать как кандидат (`_snapshots/candidates/`) для разбора, но `CURRENT` на него не
  переключается.

### 5.7 Релиз корпуса и receipt

Релиз — снимок с именем: `release_name` в `_snapshots/releases.json`, например `corpus-v0.1`. Receipt релиза
публикуется в PUBLIC `docs/corpus_platform/receipts/` (CP-12). В нём только числа, хеши, версии и логические пути:
`snapshot_id`, sha256 manifest, счётчики `corpus_counts`, версии схем, `code_revision`,
`private_registry_revision`.

## 6. Физическая раскладка, партиционирование, атомарность

### 6.1 Дерево `$VKM_DATA_ROOT`

```text
canonical/
  CURRENT                                   # snapshot_id; атомарная замена
  .snapshot.lock                            # единственный писатель снимков
  _snapshots/<snapshot_id>.json             # неизменяемые manifest'ы (+ candidates/, releases.json)
  _commits/run=<RUN>/<SID>.json             # маркеры коммитов источников (неизменяемые)
  sources/run=<RUN>/part-00000.parquet      # REGISTRY_GLOBAL: так же works, source_work_links, work_relations,
                                            #   source_relations, authors, work_authors, venues
  documents/source_id=<SID>/run=<RUN>/part-00000.parquet   # HEAD_PER_SOURCE: так же pages, blocks, figures,
                                                           #   tables, formulas, bibliography_entries
  bibliography_links/run=<RUN>/part-00000.parquet          # DERIVED_GLOBAL
  processing_runs/run=<RUN>/{start,end}.parquet            # APPEND_LOG
  processing_steps/run=<RUN>/part-NNNNN.parquet
  errors/run=<RUN>/part-NNNNN.parquet
  artifacts/run=<RUN>/source_id=<SID>/part-00000.parquet   # индекс артефактов, коммитится с источником
artifacts/<kind_dir>/<hh>/<hh>/<sha256>.<ext>              # content-addressed blobs (§4.7)
duckdb/vkm_corpus.duckdb                    # пересобираемый
neo4j/  opensearch/  logs/  tmp/            # tmp — на той же ФС, что canonical/
```

Имена партиций `key=value` без `:` безопасны на Windows. Ни один файл данных не перезаписывается: меняются только
`CURRENT` и lock.

### 6.2 Протокол записи

1. **Preflight.**
   - `$VKM_DATA_ROOT` не лежит внутри клонов PUBLIC и PRIVATE (CP-12).
   - `tmp/` и `canonical/` на одной ФС: проверка `st_dev`, иначе `os.replace` не атомарен.
   - Реестр загружен и сверен.
2. **START.** `processing_runs/run=<RUN>/start.parquet`.
3. **Источник.** Страницы обрабатываются, строки собираются в pydantic-модели и валидируются.
   - Для каждого датасета: запись во временный файл в `tmp/`, `flush`, `fsync` (на Windows дескриптор `r+b`),
     `os.replace` в `…/source_id=<SID>/run=<RUN>/part-00000.parquet`.
   - Затем индекс артефактов и части шагов и ошибок.
   - Затем маркер `_commits/run=<RUN>/<SID>.json` — атомарно. **Маркер — точка коммита источника.**
4. **END.** `processing_runs/run=<RUN>/end.parquet`.
5. **Снимок.**
   - Взять lock (`O_CREAT|O_EXCL`, с pid, ролью хоста и временем; устаревший lock снимается явной командой).
   - Прочитать `CURRENT` и собрать маркеры новее родителя.
   - Для каждого источника взять последний маркер. Порядок: `committed_at`, затем `commit_id`. Вытесненные маркеры
     записываются в `superseded_commits`.
   - Собрать manifest и прогнать валидатор (§11: DuckDB в памяти по файлам manifest).
   - Сохранить отчёт как артефакт. Если PASS — записать manifest и атомарно заменить `CURRENT`.
6. **DuckDB.** Сборка во временный файл, затем замена (§9).

Сбои:

- до маркера — файлы-сироты, снимок их не видит; `vkm-corpus gc --orphans --dry-run` их покажет;
- после маркера, до снимка — коммит подхватит следующий снимок;
- во время снимка — `CURRENT` не менялся.

### 6.3 Настройки writer (проверены)

- `compression="zstd"`, `compression_level=3`;
- `use_dictionary=True`, `write_statistics=True`;
- `version="2.6"`, `data_page_version="1.0"` — максимальная совместимость читателей;
- `row_group_size=65536`;
- `store_schema=True`;
- `sorting_columns` — по первому ключу сортировки;
- KV-метаданные §5.2.

Один файл на партицию. Если строк больше 10⁶, файл детерминированно делится на `part-00000…` по диапазонам PK.

Порядок строк, он же ключ сортировки:

| Датасеты | Ключ сортировки |
|---|---|
| `sources` | `source_id` |
| `works` | `work_id` |
| `source_work_links` | `source_id`, `is_primary desc`, `link_type`, `work_id` |
| связи и авторы | их PK |
| `documents` | `object_id` |
| `pages` | `page_index` |
| `blocks` | `page_id`, `text_layer`, `reading_order`, `object_id` |
| `figures`, `tables`, `formulas` | `page_id`, `bbox_y0`, `bbox_x0`, `object_id` |
| `bibliography_entries` | `page_id`, `ordinal_in_list`, `object_id` |
| `bibliography_links` | `citing_source_id`, `entry_id`, `cited_work_id` |
| `processing_steps` | `source_id`, `page_index`, `stage`, `attempt`, `step_id` |
| `errors` | `source_id`, `page_id`, `created_at`, `error_id` |
| `artifacts` | `artifact_id` |

### 6.4 Детерминизм

- **Файл.** Одинаковые строки при той же версии pyarrow и настройках дают побайтово одинаковый файл: проверено на
  1510 файлах. Wall-clock в файле нет, есть только `created_by` pyarrow.
- **Канон** определяется на уровне содержимого: `content_fingerprint` (§5.5). Смена версии pyarrow не ломает
  сравнение.

### 6.5 Идемпотентность: две подписи

- **`stage_signature`** решает, пересобирать ли строки этапа страницы. Содержит минимум §15:

  ```text
  stage_signature = sha256("vkm-sig-v1|" + source_sha256 + "|" + page_unit + page_index + "|" + stage + "|" +
                           pipeline_version + "|" + extractor_id + "|" + extractor_version + "|" +
                           (model_id or "-") + "|" + (model_revision or "-") + "|" + stage_config_hash + "|" +
                           ",".join(sorted(input_artifact_ids)))
  ```

- **`call_signature`** — ключ кеша дорогого вызова модели:
  `sha256(model_id | model_revision | backend | backend_version | call_config_hash | input_blob_sha256)`. Сырой
  выход OCR ищется в `artifacts.producer_signature`. При совпадении `outcome = REUSED_CACHED`, **без GPU**. Так
  повышение `pipeline_version` пересобирает строки, но не OCR-ит заново неизменившиеся страницы (§15).
- **`pipeline_version`** — semver контракта обработки документа: поднимается, только когда меняется поведение
  извлечения или нормализации. Это не git-коммит (он в `code_revision`), иначе каждый релиз вызывал бы полную
  пересборку (DN-D-05).
- **Флаги CLI:**
  - план: страница или этап пропускается (`SKIPPED_UP_TO_DATE`), если у head та же `stage_signature` и статус OK
    или UNSUPPORTED;
  - `--force` пересчитывает, но кеш вызовов использует (`--force --no-cache` — и его нет);
  - `--failed-only` — страницы со статусом FAILED, PARTIAL, NOT_PROCESSED или OCR_REQUIRED;
  - `--resume` продолжает прогон с незакоммиченных источников;
  - `--plan-only` пишет только отчёт-план как артефакт, канонических строк нет.
- **Перенос (carry-over).** Новый коммит источника копирует строки неизменившихся страниц из родительского коммита
  байт в байт, с их исходным провенансом. Заменяются только пересчитанные страницы. Если все `content_fingerprint`
  равны родителю, коммит не создаётся.
- **Провал повтора** не затирает хороший результат. Страница остаётся с прежними строками. Попытка видна в
  `processing_steps` и `errors`, а view `processing_status` показывает `last_attempt_status` рядом с
  `committed_page_status`.

### 6.6 Нет «висящих» объектов

- Head источника — ровно файлы его последнего маркера.
- Датасет без строк всё равно представлен файлом, поэтому исчезнувшие объекты не «переживают» пересборку.
- Валидатор требует один head-файл на пару (датасет, закоммиченный источник) и совпадение `source_sha256` строк с
  маркером и реестром.

### 6.7 Параллельность, хосты, GC

- **Параллельность.**
  - Источники обрабатываются параллельно: они не пересекаются.
  - Снимки пишет один писатель (lock).
  - Два прогона одного источника дают два коммита, в head попадает последний, другой — в `superseded_commits`.
    Блокировку на уровне задания даёт control plane.
- **Хосты** (зависит от CP-15). WORKSTATION может писать в локальный staging-корень той же раскладки и синхронизировать
  на CORE неизменяемые файлы, например `rsync --ignore-existing`:
  - сначала `artifacts/` и `canonical/<dataset>/…`;
  - маркеры `_commits/` — **последними**;
  - снимок — только на CORE.
- **GC.** `vkm-corpus gc --dry-run` перечисляет файлы, на которые не ссылается ни один сохранённый снимок. KEEP_RAW
  не удаляются. Политика v0 — хранить все снимки, GC только вручную.

### 6.8 Мелкие файлы

Партиция на источник даёт около 7 документных файлов × 249 источников. Для запросов это решает материализация в
DuckDB (§9). Для Neo4j и OpenSearch построители читают через DuckDB или `polars.scan_parquet(list)`: 251 файл
сканируется за 21 мс. Компакции в v0 не нужно.

## 7. Время

| Время (контракт §42) | Поле | Где | Тип | Заполнение в v0 |
|---|---|---|---|---|
| processing_time | `created_at` (конверт) | все объекты | timestamp[us, UTC] | = `finished_at` шага-производителя. Вне `content_sha256` и `content_fingerprint`; при переносе строк не меняется |
| processing_time прогона и шага | `started_at`, `finished_at` | `processing_runs`, `processing_steps` | timestamp[us, UTC] | фактическое время |
| ingestion_time | `ingestion_date`, `ingestion_date_precision`, `ingestion_basis` | `sources` | date32 | intake-манифест (все 210 id 042–251 имеют строку манифеста); текст `migration_source`; для 001–041 — git-история реестра (первый коммит с id); иначе UNKNOWN |
| publication_time | `publication_year`, `publication_year_raw`, `publication_date`, `publication_date_precision` | `works` | int16, string, date32 | курированные метаданные; год с точностью `year` |
| available_from | `available_from`, `available_from_precision`, `available_from_basis` | `works` | date32 | NULL = UNKNOWN, пока не курировано. D-03 и D-16 — только во view `works_availability` (последний день периода, основание `ASSUMED_FROM_PUBLICATION`) |
| даты из метаданных файла | `file_meta_created_raw`, `file_meta_modified_raw` | `documents` | string | подсказки; не publication_time |
| event_time, measurement_time | — | **нет в L1** | — | для будущего L2; валидатор запрещает эти колонки в схемах L1 |

Доступность — свойство Work, а не копии: у Source есть только `ingestion_date` — когда файл получил проект.

## 8. Расширение в будущем (контракт §51–53, постановка §27)

1. **Ссылки L2 на документный слой** — без перестройки. Будущие записи evidence несут локатор:

   ```text
   struct<object_id, page_id, region, text_span, raw_content_sha256, snapshot_id, locator_status>
   ```

   - `region` и `text_span` — грамматика §4.10.
   - Перечитывание документа даёт новые `object_id`. Старые остаются в снимках, `object_lineage` связывает их. Drift
     виден по `raw_content_sha256`.
2. **Neo4j** (решает E). Новые метки и рёбра (`Claim SUPPORTED_BY Page`, `Measurement EXTRACTED_FROM Table`,
   `Law DEFINED_BY Formula`) цепляются к существующим узлам по ID. Документные узлы не меняются. Логические графы
   различаются метками (§52).
3. **Что есть уже сейчас:**
   - стабильные ID и грамматика регионов;
   - `raw_content_sha256` и `content_sha256`;
   - `snapshot_id` и manifest;
   - `external_ids` (PWL, CW, EXT-SRC);
   - `work_relations` и `source_relations`;
   - зарезервированные имена §2.3 и грамматики ID §4.2.
4. **Чего сейчас нет** — «пустой мегасхемы» не создаётся:
   - Claim, Measurement, Experiment, Law, Parameter, PhysicalEntity, PhysicalWorld, WorldRepresentation,
     ObservationWorld, SolverRun;
   - `epistemic_status` в L1;
   - единицы и переменные формул;
   - географические координаты;
   - строки `review_events`;
   - работы без Source.
5. **Crosswalk Phase 1 → page** (§4.11). Проба: у всех 39 документов evidence `pdf_page` не превышает числа
   физических страниц. Связь `vn_id → page_id` детерминирована для PDF и DjVu. Для DOCX она `RENDER_DEPENDENT`.

## 9. DuckDB

### 9.1 Файл и режимы

- Файл: `$VKM_DATA_ROOT/duckdb/vkm_corpus.duckdb`.
- Сборка: `vkm-corpus duckdb build [--snapshot ID] [--mode materialized|views]`. По умолчанию `materialized`, так как
  в пробе это в 20 раз быстрее.
- Схемы:
  - `canonical` — базовые таблицы;
  - `meta` — `snapshot` и `dataset_files`;
  - `main` — стабильные view и macro, интерфейс для API, MCP и построителей.
- Сборка идёт в `duckdb/.vkm_corpus.<uuid>.tmp` и заменяет файл целиком. Читатели открывают файл в `read_only`. На
  Linux открытые соединения продолжают видеть старый inode. API сверяет `meta.snapshot` и переоткрывает файл.
- Файл хост-специфичен: абсолютные пути в `read_parquet` материализованы. Его никогда не коммитят; при переезде его
  пересобирают.

### 9.2 Базовые таблицы (генерирует построитель по manifest)

```sql
CREATE SCHEMA IF NOT EXISTS canonical; CREATE SCHEMA IF NOT EXISTS meta;
-- для каждого датасета: файлы строго из manifest (без glob), всегда union_by_name
CREATE OR REPLACE TABLE canonical.pages AS
  SELECT * FROM read_parquet([<файлы pages из manifest>], union_by_name = true, hive_partitioning = false);
-- датасет без файлов: CREATE TABLE по DDL, выведенному из Arrow-схемы контракта (типы 1:1)
CREATE OR REPLACE TABLE meta.snapshot AS
  SELECT '<snapshot_id>' AS snapshot_id, '<sha256 manifest>' AS manifest_sha256, now() AS built_at,
         version() AS duckdb_version, '<code_revision>' AS code_revision;
CREATE OR REPLACE TABLE meta.dataset_files AS SELECT * FROM (VALUES ...) t(dataset, path, sha256, rows, schema_version);
```

Датасет `tables` в SQL заключается в кавычки: `canonical."tables"`, `main."tables"`.

### 9.3 View и macro (проверено на DuckDB 1.5.5)

View-минимум постановки §25: `sources`, `works`, `pages`, `figures`, `"tables"`, `formulas`, `bibliography`,
`processing_status`. Дополнительно: `documents`, `blocks`, `artifacts`, `processing_runs`, `work_sources`,
`work_of_page`, `cites`, `page_sequence`, `duplicate_page_candidates`, `all_objects`, `page_coverage`,
`source_status_summary`, `corpus_counts`, `works_availability`. Macro: `provenance_trace`, `objects_by_source`,
`objects_on_page`, `resolve_work`.

```sql
CREATE OR REPLACE VIEW sources   AS SELECT * FROM canonical.sources;
CREATE OR REPLACE VIEW works     AS SELECT * FROM canonical.works WHERE status = 'ACTIVE';
CREATE OR REPLACE VIEW works_all AS SELECT * FROM canonical.works;          -- incl. MERGED_INTO tombstones
CREATE OR REPLACE VIEW documents AS SELECT * FROM canonical.documents;
CREATE OR REPLACE VIEW pages     AS SELECT * FROM canonical.pages;
CREATE OR REPLACE VIEW blocks    AS SELECT * FROM canonical.blocks;
CREATE OR REPLACE VIEW figures   AS SELECT * FROM canonical.figures;
CREATE OR REPLACE VIEW "tables"  AS SELECT * FROM canonical."tables";
CREATE OR REPLACE VIEW formulas  AS SELECT * FROM canonical.formulas;
CREATE OR REPLACE VIEW artifacts AS                                          -- one row per blob (first registration)
  SELECT * FROM canonical.artifacts
  QUALIFY row_number() OVER (PARTITION BY artifact_id ORDER BY created_at, created_by_run_id) = 1;
CREATE OR REPLACE VIEW processing_runs AS                                    -- END record wins over START
  SELECT * FROM canonical.processing_runs
  QUALIFY row_number() OVER (PARTITION BY processing_run_id ORDER BY (record_phase = 'END') DESC) = 1;

-- Source INSTANCE_OF Work edges for projections (REJECTED never projects; grouping rule enforced by the validator)
CREATE OR REPLACE VIEW work_sources AS
  SELECT l.* FROM canonical.source_work_links l
  WHERE l.curation_status <> 'REJECTED' AND l.work_id IS NOT NULL;

CREATE OR REPLACE MACRO resolve_work(wid) AS TABLE
  WITH RECURSIVE chain(work_id, status, merged_into_work_id, depth) AS (
    SELECT work_id, status, merged_into_work_id, 0 FROM canonical.works WHERE work_id = wid
    UNION ALL
    SELECT w.work_id, w.status, w.merged_into_work_id, c.depth + 1
    FROM chain c JOIN canonical.works w ON w.work_id = c.merged_into_work_id
    WHERE c.status = 'MERGED_INTO' AND c.depth < 16
  )
  SELECT wid AS requested_work_id, work_id AS resolved_work_id, status, depth FROM chain
  ORDER BY depth DESC LIMIT 1;

CREATE OR REPLACE VIEW work_of_page AS
  SELECT p.page_id, p.source_id, p.page_index, l.work_id, l.link_type, l.is_primary,
         (l.page_start IS NOT NULL OR l.page_end IS NOT NULL) AS has_range
  FROM canonical.pages p
  JOIN canonical.source_work_links l
    ON l.source_id = p.source_id AND l.curation_status <> 'REJECTED'
   AND (l.page_start IS NULL OR p.page_index >= l.page_start)
   AND (l.page_end   IS NULL OR p.page_index <= l.page_end);

-- citing work: unique candidate; FOREIGN_CONTENT on the page -> NULL (never attribute to the host);
-- a single ranged CONTAINS_WORK (future, curated) -> the component; otherwise NULL (AMBIGUOUS)
CREATE OR REPLACE VIEW bibliography AS
  WITH cand AS (
    SELECT e.object_id AS entry_id, w.work_id, w.link_type, w.has_range
    FROM canonical.bibliography_entries e JOIN work_of_page w ON w.page_id = e.page_id
  ), pick AS (
    SELECT entry_id, count(*) AS n_candidate_works,
           CASE WHEN count(*) FILTER (WHERE link_type = 'FOREIGN_CONTENT') > 0 THEN NULL
                WHEN count(*) = 1 THEN any_value(work_id)
                WHEN count(*) FILTER (WHERE link_type = 'CONTAINS_WORK' AND has_range) = 1
                 AND count(*) FILTER (WHERE link_type = 'CONTAINS_WORK' AND NOT has_range) = 0
                THEN any_value(work_id) FILTER (WHERE link_type = 'CONTAINS_WORK' AND has_range)
                ELSE NULL END AS citing_work_id,
           CASE WHEN count(*) FILTER (WHERE link_type = 'FOREIGN_CONTENT') > 0 THEN 'FOREIGN_CONTENT'
                WHEN count(*) = 1 THEN 'UNIQUE_LINK'
                WHEN count(*) FILTER (WHERE link_type = 'CONTAINS_WORK' AND has_range) = 1
                 AND count(*) FILTER (WHERE link_type = 'CONTAINS_WORK' AND NOT has_range) = 0 THEN 'RANGED_COMPONENT'
                ELSE 'AMBIGUOUS' END AS citing_work_resolution
    FROM cand GROUP BY entry_id
  )
  SELECT e.*, p.citing_work_id, coalesce(p.citing_work_resolution, 'NO_LINK') AS citing_work_resolution,
         coalesce(p.n_candidate_works, 0) AS n_candidate_works
  FROM canonical.bibliography_entries e LEFT JOIN pick p ON p.entry_id = e.object_id;

CREATE OR REPLACE VIEW cites AS                                              -- citation != agreement
  SELECT l.citing_work_id, l.cited_work_id, l.match_method, l.match_status, l.entry_id, l.citing_source_id
  FROM canonical.bibliography_links l
  WHERE l.match_status IN ('ACCEPTED_EXACT_ID', 'ACCEPTED_CURATED')
    AND l.citing_work_id IS NOT NULL AND l.citing_work_id <> l.cited_work_id;

CREATE OR REPLACE VIEW page_sequence AS                                      -- Page PRECEDES Page
  SELECT source_id, page_id AS from_page_id,
         lead(page_id) OVER (PARTITION BY source_id ORDER BY page_index) AS to_page_id
  FROM canonical.pages
  QUALIFY to_page_id IS NOT NULL;

CREATE OR REPLACE VIEW duplicate_page_candidates AS                          -- candidates only, never merged
  SELECT text_sha256, list(page_id ORDER BY page_id) AS page_ids, count(DISTINCT source_id) AS n_sources
  FROM canonical.pages
  WHERE text_sha256 IS NOT NULL AND coalesce(native_char_count, recognized_char_count, 0) >= 200
  GROUP BY text_sha256 HAVING count(DISTINCT source_id) > 1;

CREATE OR REPLACE VIEW all_objects AS
  SELECT object_id, object_kind, source_id, page_id, source_sha256, origin, pipeline_version, processing_run_id,
         extractor_id, extractor_version, model_id, model_revision, config_hash, extraction_signature,
         raw_content_sha256, content_sha256, raw_artifact_id, created_at, review_status, quality_flags, schema_version
  FROM canonical.documents
  UNION ALL BY NAME SELECT /* same column list */ object_id, object_kind, source_id, page_id, source_sha256, origin,
         pipeline_version, processing_run_id, extractor_id, extractor_version, model_id, model_revision, config_hash,
         extraction_signature, raw_content_sha256, content_sha256, raw_artifact_id, created_at, review_status,
         quality_flags, schema_version FROM canonical.pages
  -- ... the same SELECT for canonical.blocks, canonical.figures, canonical."tables", canonical.formulas,
  --     canonical.bibliography_entries (UNION ALL BY NAME)
  ;

CREATE OR REPLACE VIEW processing_status AS                                  -- latest attempt vs committed state
  SELECT st.source_id, st.page_id, st.stage, st.status AS last_attempt_status, st.outcome AS last_attempt_outcome,
         st.reason_code, st.processing_run_id AS last_attempt_run_id, st.finished_at AS last_attempt_at,
         st.stage_signature, p.page_status AS committed_page_status
  FROM canonical.processing_steps st
  LEFT JOIN canonical.pages p ON p.page_id = st.page_id
  QUALIFY row_number() OVER (PARTITION BY coalesce(st.source_id, ''), coalesce(st.page_id, ''), st.stage
                             ORDER BY st.finished_at DESC, st.step_id DESC) = 1;

CREATE OR REPLACE VIEW page_coverage AS
  SELECT source_id, count(*) AS pages_total,
         count(*) FILTER (WHERE page_status = 'NATIVE_OK')     AS pages_native_ok,
         count(*) FILTER (WHERE page_status = 'OCR_OK')        AS pages_ocr_ok,
         count(*) FILTER (WHERE page_status = 'OCR_REQUIRED')  AS pages_ocr_required,
         count(*) FILTER (WHERE page_status = 'PARTIAL')       AS pages_partial,
         count(*) FILTER (WHERE page_status = 'NEEDS_REVIEW')  AS pages_needs_review,
         count(*) FILTER (WHERE page_status = 'UNSUPPORTED')   AS pages_unsupported,
         count(*) FILTER (WHERE page_status = 'FAILED')        AS pages_failed,
         count(*) FILTER (WHERE page_status = 'NOT_PROCESSED') AS pages_not_processed,
         min(page_index) AS min_page_index, max(page_index) AS max_page_index,
         (min(page_index) = 1 AND max(page_index) = count(*) AND count(DISTINCT page_index) = count(*)) AS contiguous
  FROM canonical.pages GROUP BY source_id;

CREATE OR REPLACE VIEW source_status_summary AS
  WITH obj AS (
    SELECT source_id,
           count(*) FILTER (WHERE object_kind = 'FIGURE')             AS n_figures,
           count(*) FILTER (WHERE object_kind = 'TABLE')              AS n_tables,
           count(*) FILTER (WHERE object_kind = 'FORMULA')            AS n_formulas,
           count(*) FILTER (WHERE object_kind = 'BIBLIOGRAPHY_ENTRY') AS n_bibliography_entries
    FROM all_objects GROUP BY source_id
  ), err AS (
    SELECT source_id, count(*) AS n_errors, count(*) FILTER (WHERE retryable) AS n_errors_retryable
    FROM canonical.errors GROUP BY source_id
  )
  SELECT s.source_id, s.file_status, s.lifecycle_status, s.lifecycle_reason_code, s.format_detected, s.review_status,
         d.document_class, d.page_count, coalesce(pc.pages_total, 0) AS pages_total,
         coalesce(pc.pages_native_ok, 0) AS pages_native_ok, coalesce(pc.pages_ocr_ok, 0) AS pages_ocr_ok,
         coalesce(pc.pages_failed, 0) AS pages_failed, coalesce(pc.pages_needs_review, 0) AS pages_needs_review,
         coalesce(o.n_figures, 0) AS n_figures, coalesce(o.n_tables, 0) AS n_tables,
         coalesce(o.n_formulas, 0) AS n_formulas, coalesce(o.n_bibliography_entries, 0) AS n_bibliography_entries,
         coalesce(e.n_errors, 0) AS n_errors,
         CASE
           WHEN s.lifecycle_status IN ('ABSENT_BY_REGISTER', 'RETIRED') THEN 'SKIPPED_BY_REGISTER'
           WHEN s.file_status <> 'PRESENT_VERIFIED' THEN 'FAILED'
           WHEN d.source_id IS NULL THEN 'NOT_PROCESSED'
           WHEN d.processing_status IN ('FAILED', 'UNSUPPORTED') THEN d.processing_status
           WHEN coalesce(pc.pages_total, 0) <> d.page_count OR NOT coalesce(pc.contiguous, false) THEN 'PARTIAL'
           WHEN pc.pages_failed + pc.pages_unsupported + pc.pages_not_processed + pc.pages_ocr_required + pc.pages_partial > 0 THEN 'PARTIAL'
           WHEN pc.pages_needs_review > 0 THEN 'NEEDS_REVIEW'
           ELSE 'COMPLETE' END AS source_rollup
  FROM canonical.sources s
  LEFT JOIN canonical.documents d USING (source_id)
  LEFT JOIN page_coverage pc USING (source_id)
  LEFT JOIN obj o USING (source_id)
  LEFT JOIN err e USING (source_id);

-- acceptance counts (task §48): 251 = complete + partial + failed + not processed + skipped by register
CREATE OR REPLACE VIEW corpus_counts AS
  SELECT count(*) AS sources_total,
         count(*) FILTER (WHERE source_rollup = 'COMPLETE')                   AS sources_complete,
         count(*) FILTER (WHERE source_rollup IN ('PARTIAL', 'NEEDS_REVIEW')) AS sources_partial,
         count(*) FILTER (WHERE source_rollup IN ('FAILED', 'UNSUPPORTED'))   AS sources_failed,
         count(*) FILTER (WHERE source_rollup = 'NOT_PROCESSED')              AS sources_not_processed,
         count(*) FILTER (WHERE source_rollup = 'SKIPPED_BY_REGISTER')        AS sources_skipped_by_register,
         sum(pages_total) AS pages_total, sum(pages_native_ok) AS pages_native, sum(pages_ocr_ok) AS pages_ocr,
         sum(pages_failed) AS pages_failed, sum(n_figures) AS figures, sum(n_tables) AS "tables",
         sum(n_formulas) AS formulas, sum(n_bibliography_entries) AS bibliography_entries
  FROM source_status_summary;

-- D-03/D-16 applied only here and labelled; canonical works.available_from stays curated (NULL = UNKNOWN)
CREATE OR REPLACE VIEW works_availability AS
  SELECT work_id, publication_year, available_from, available_from_precision, available_from_basis,
         CASE WHEN available_from IS NOT NULL THEN
                CASE available_from_precision
                  WHEN 'day'    THEN available_from
                  WHEN 'month'  THEN last_day(available_from)
                  WHEN 'decade' THEN make_date(year(available_from) - year(available_from) % 10 + 9, 12, 31)
                  ELSE make_date(year(available_from), 12, 31) END
              WHEN publication_year IS NOT NULL THEN make_date(publication_year, 12, 31)
              ELSE NULL END AS available_latest_day,
         CASE WHEN available_from IS NOT NULL THEN available_from_basis
              WHEN publication_year IS NOT NULL THEN 'ASSUMED_FROM_PUBLICATION'
              ELSE 'UNKNOWN' END AS available_latest_basis
  FROM works;

CREATE OR REPLACE MACRO objects_on_page(pid) AS TABLE
  SELECT object_kind, object_id, origin, review_status, quality_flags FROM all_objects
  WHERE page_id = pid ORDER BY object_kind, object_id;

CREATE OR REPLACE MACRO objects_by_source(sid) AS TABLE
  SELECT object_kind, origin, count(*) AS n, count(*) FILTER (WHERE len(quality_flags) > 0) AS n_flagged
  FROM all_objects WHERE source_id = sid GROUP BY ALL ORDER BY object_kind, origin;

CREATE OR REPLACE MACRO provenance_trace(oid) AS TABLE
  SELECT o.object_kind, o.object_id, o.schema_version, o.source_id, o.page_id,
         p.page_index, p.page_kind, p.printed_page_raw, p.page_status,
         o.origin, o.review_status, o.quality_flags,
         o.processing_run_id, r.run_kind, r.status AS run_status, r.code_revision, r.host_role,
         o.pipeline_version, o.extractor_id, o.extractor_version, o.model_id, o.model_revision,
         o.config_hash, o.extraction_signature, o.raw_content_sha256, o.content_sha256, o.created_at,
         o.raw_artifact_id, a.storage_relpath AS raw_artifact_relpath, a.retention_class AS raw_retention_class,
         s.canonical_path AS source_canonical_path, s.lifecycle_status, s.source_sha256 AS register_sha256,
         (o.source_sha256 IS NOT DISTINCT FROM s.source_sha256) AS source_sha256_matches_register,
         'CANONICAL' AS record_role,
         (SELECT snapshot_id FROM meta.snapshot) AS snapshot_id
  FROM all_objects o
  LEFT JOIN processing_runs r ON r.processing_run_id = o.processing_run_id
  LEFT JOIN artifacts a       ON a.artifact_id = o.raw_artifact_id
  LEFT JOIN canonical.sources s ON s.source_id = o.source_id
  LEFT JOIN canonical.pages p   ON p.page_id = o.page_id
  WHERE o.object_id = oid;
```

SQL лежит в `src/vkm_corpus/duckdb/sql/*.sql` и выполняется построителем по порядку. Проверенная копия — в
git-ignored каталоге проб, `duckdb_views.sql`; в `all_objects` там раскрыты все семь SELECT.

### 9.4 Что показала проба на пограничных синтетических случаях

- Запись на странице с `FOREIGN_CONTENT` получает `citing_work_id = NULL` и `FOREIGN_CONTENT`, как для неопознанной
  предыдущей статьи, так и для страницы другой работы корпуса. Остальные записи — `UNIQUE_LINK`.
- `cites` берёт только `DOI_EXACT` или `ACCEPTED_*`. Нечёткий кандидат ребром не стал.
- `resolve_work('VKM-WRK-006')` проходит 006 → 005 → 003 (ACTIVE, depth 2).
- `duplicate_page_candidates` нашёл пару страниц разных источников с одинаковым хешем текста.
- `source_status_summary` и `corpus_counts` на 7 источниках дали: 2 COMPLETE, 2 PARTIAL или NEEDS_REVIEW
  (FAILED-страница + NOT_PROCESSED-страница; NEEDS_REVIEW-страница), 1 FAILED (SHA256_MISMATCH),
  2 SKIPPED_BY_REGISTER с кодами причин. Сумма — 7.
- `processing_status` показывает `last_attempt_status = FAILED` рядом с `committed_page_status`.
- `provenance_trace` вернул всю цепочку: прогон END, `code_revision`, путь сырого артефакта, KEEP_RAW,
  совпадение sha с реестром, `record_role`, `snapshot_id`.

### 9.5 Как тестируется

См. §12, `test_duckdb_views.py`: сборка из синтетического снимка во `tmp_path`, наличие view и macro, эталонные
ответы на фикстурах §9.4, пересборка с нуля даёт те же хеши результатов запросов, `meta.snapshot` совпадает с
manifest.

## 10. Pydantic ↔ Arrow ↔ JSON Schema

### 10.1 Модули (каталоги агента D по брифу §7)

| Модуль | Назначение |
|---|---|
| `src/vkm_corpus/contracts/` | `vocab.py` (enum §3); `envelope.py`; `sources.py`, `works.py`, `document.py`, `logs.py` — модели; `site_scope.py` (таблица §3.4); `registry.py` — декларативный реестр датасетов (имя, модель, класс, версия, PK, FK, ключи сортировки, партиция, профиль конверта); `arrow.py` (отображение); `export.py` (`python -m vkm_corpus.contracts.export [--check]`) |
| `src/vkm_corpus/ids/` | построители и парсеры ID (§4), регулярные выражения, ключи имён |
| `src/vkm_corpus/parquet/` | атомарный писатель, читатель по manifest, коммиты, снимки, GC, валидатор канона |
| `src/vkm_corpus/duckdb/` | построитель и `sql/*.sql` |

Зависимость только `vkm_corpus → vkm_world` (CP-01). Импортируются: `core.io.sha256_file`, `canonical_json`,
`sha256_json`, `Scope`, `CoverageLevel`, `DATE_PRECISIONS`, `date_bounds`, `parse_partial_date`. `Provenance`,
`SourceRef` и `WorldObject` не наследуются (CP-03).

### 10.2 Отображение типов

| Pydantic | Arrow | JSON Schema |
|---|---|---|
| `str` | `string` | `string` |
| `Annotated[int, Arrow(int16/int32/int64)]` (голый `int` запрещён тестом) | `int16/int32/int64` | `integer` с `minimum/maximum` |
| `float` | `float64` | `number` |
| `bool` | `bool` | `boolean` |
| `date` | `date32` | `string, format=date` |
| `datetime` (обязательно aware, валидатор UTC) | `timestamp[us, tz=UTC]` | `string, format=date-time` |
| `StrEnum` из `vocab.py` | `string` | `enum` |
| `list[X]` | `list<X>` | `array` |
| вложенная `BaseModel` | `struct` | `object` в `$defs` |
| `X \| None` | nullable | не в `required` |

- Nullable в Arrow ⇔ аннотация допускает `None`. Required в pydantic ⇔ non-null.
- Колонки идут в порядке полей модели: конверт через явный `COLUMN_ORDER` в `registry.py`.
- Писатель перед записью проверяет `null_count == 0` для non-nullable колонок: `from_pylist` их не проверяет (проба
  §0.2).
- Fingerprint = sha256 канонического JSON `[[имя, str(тип), nullable], …]`.

### 10.3 Экспорт

- `python -m vkm_corpus.contracts.export` пишет в `schemas/corpus/`:
  - `<dataset>.schema.json` — pydantic `model_json_schema()` плюс `$id`, `title`, `x-vkm-schema-version`;
  - `arrow_schemas.json` — поля, типы, nullable, fingerprint, PK, FK, ключи сортировки;
  - `vocabularies.json` — все enum.
- Формат детерминирован: `sort_keys`, `indent=1`, LF, `ensure_ascii=False`, как `worldspec/io.json_schema`.
- `--check` сравнивает закоммиченное с кодом. Тест делает то же.
- `scripts/*` не трогаются (CP-01). Команда CLI `vkm-corpus schemas export|check` — обёртка над модулем.
- Схема зависит от закреплённой пары pydantic 2.13.5 / pydantic-core 2.46.5 (CP-02, K18).

### 10.4 Гигиена

Тест экспортирует все JSON Schema во `tmp_path` и прогоняет `vkm_world.governance.leakage.scan(files=…)` и
`FORBIDDEN_COLUMNS & _json_keys(schema)`, включая ключи `$defs`. Проверяются также:

- имена Arrow-полей;
- имена pydantic-классов в нижнем регистре;
- заголовки CSV в `WORK_REGISTER.csv` и `WORK_LINKS.csv` из синтетических фикстур.

Описания полей в схемах — свои формулировки: без текста источников и без путей.

## 11. Валидатор канона

`vkm-corpus validate [--snapshot ID | --candidate] [--deep]`. Работает по файлам manifest: DuckDB в памяти плюс
Python для хешей и регулярных выражений.

- Отчёт — JSON: `check_id`, статус PASS, FAIL или WARN, число нарушений, до 20 примеров ID. Хранится как артефакт
  `VALIDATION_REPORT`, ссылка на него — в manifest.
- Блокирующий FAIL не даёт переключить `CURRENT`.
- Режим `--acceptance` (для Phase 7) делает F02 блокирующей.

| ID | Группа | Проверка | Блок. |
|---|---|---|---|
| A01 | Файлы | каждый файл manifest существует; sha256, байты и строки совпадают | да |
| A02 | Файлы | KV `vkm.dataset`, версия и fingerprint совпадают с manifest и с известной коду схемой этой версии | да |
| A03 | Файлы | один head-файл на пару (датасет, закоммиченный источник); маркер перечисляет все документные датасеты | да |
| A04 | Файлы | в head один коммит на источник; вытесненные перечислены | да |
| A05 | Файлы | версии внутри датасета совместимы (§5.3) | да |
| B01 | Ключи | PK уникален в каждом датасете | да |
| B02 | Ключи | регулярные выражения ID по колонкам (§4.2) | да |
| B03 | Ключи | `object_id` начинается с `page_id:`; `page_id` = `source_id:` + буква + `%04d(page_index)`; буква соответствует `page_kind` | да |
| B04 | Ключи | пересчёт `object_id` из (`page_id`, вид, origin, якорь, `producer_key`) совпадает с записанным | да |
| B05 | Ключи | ID связей пересчитываются из составных ключей | да |
| B06 | Ключи | `object_id` = сущностный ID для sources, works, pages, documents | да |
| B07 | Ключи | один `object_id` ⇒ один `raw_content_sha256` по сравнению с родительским снимком; нарушение — сигнал недетерминизма или смены производителя без смены ID | да |
| C01 | Ссылки | `pages` и `documents` → `sources` | да |
| C02 | Ссылки | `page_id` объектов → `pages`; `source_id` объекта = `source_id` страницы | да |
| C03 | Ссылки | `caption_block_id`, `continues_object_id`, `continues_on_page_id`, `list_block_ids` ведут на объекты того же источника | да |
| C04 | Ссылки | все `*_artifact_id` есть в индексе `artifacts` | да |
| C05 | Ссылки | `processing_run_id` каждой строки → START-запись; для закоммитивших прогонов END (иначе WARN: падение после коммита) | да / WARN |
| C06 | Ссылки | FK связей: links → sources и works; relations → works и sources; `work_authors` → works и authors; `works.venue_id` → venues; `bibliography_links` → entries и works | да |
| C07 | Ссылки | цепочки `merged_into` без циклов, заканчиваются ACTIVE | да |
| C08 | Ссылки | `page_id` и `source_id` у `errors` и `processing_steps` существуют или NULL | да |
| D01 | Провенанс | непустые поля конверта по профилю (§2) | да |
| D02 | Провенанс | OCR ⇒ `model_id` и `model_revision`; модель есть в `processing_runs.models` | да |
| D03 | Провенанс | у NATIVE и OCR есть `raw_artifact_id` | да |
| D04 | Провенанс | `source_sha256` строк = маркер = `sources.source_sha256` = реестр | да |
| D05 | Провенанс | `extraction_signature` = подпись, пересчитанная из компонентов шага | да |
| D06 | Провенанс | `created_at` aware UTC и попадает в окно прогона | WARN |
| D07 | Провенанс | `config_hash` есть в конфиге прогона (`config_artifact_id`) | WARN |
| E01 | Наука | ни в одной колонке статуса нет FACT, REVIEWED_MEASUREMENT, ACCEPTED_PARAMETER, ACCEPTED_FORMULA и значений EpistemicStatus | да |
| E02 | Наука | NATIVE, OCR и DERIVED ⇒ `review_status = AUTO_EXTRACTED_UNREVIEWED` | да |
| E03 | Наука | `review_status` источников по правилам CP-06 и с основанием; NOT_APPLICABLE ⇔ lifecycle ≠ ACTIVE | да |
| E04 | Наука | тип рисунка ≠ UNKNOWN ⇒ метод ≠ NONE и уверенность ≥ порога | да |
| E05 | Наука | `crs_status` ⇔ `coordinate_space = GEO`; в v0 GEO нет; EPSG без EXACT и основания запрещён | да |
| E06 | Наука | `quality_flags` из словаря и применимы к датасету | да |
| E07 | Наука | в схемах L1 нет зарезервированных будущих колонок (`event_time`, `measurement_time`, `epistemic_status`, `world_id`…) | да |
| E08 | Наука | группировка: у работы с несколькими источниками все связи CURATED; пары NOT_SAME не делят одну работу | да |
| E09 | Наука | CITES только из ACCEPTED_*; страница с FOREIGN_CONTENT никогда не даёт `citing_work_id` | да |
| E10 | Наука | `site_scope` из `Scope`; таблица §3.4 покрывает все сырые значения; AMBIGUOUS и NOT_A_SCOPE ⇒ `site_scope = []` | да |
| F01 | Полнота | строк `sources` = строк реестра (251), id совпадают | да |
| F02 | Полнота | у каждого ACTIVE-источника с PRESENT_VERIFIED есть head-коммит, иначе он явно NOT_PROCESSED | WARN (да в `--acceptance`) |
| F03 | Полнота | `documents.page_count` = число `pages`; `page_index` непрерывен 1..N | да |
| F04 | Полнота | `page_count_check` совпадает или расхождение объяснено флагом | WARN |
| F05 | Полнота | у FAILED, UNSUPPORTED и PARTIAL страниц есть хотя бы одна ошибка | да |
| F06 | Полнота | ACTIVE-источник без PRESENT_VERIFIED ⇒ свёртка FAILED и ошибка `SOURCE_*` | да |
| F07 | Полнота | ровно одна главная связь на источник; у ACTIVE-работы есть хотя бы одна связь | да |
| F08 | Полнота | lifecycle ≠ ACTIVE ⇒ SKIPPED_BY_REGISTER с кодом; сумма классов свёртки = 251 | да |
| F09 | Полнота | у шага со статусом FAILED есть ошибка | да |
| F10 | Полнота | blob артефакта существует, sha256 совпадает: полная проверка в `--deep`, выборка иначе | да (`--deep`) |
| G01 | Гигиена | запрещённые имена колонок не встречаются ни в одной Arrow-схеме и ни в одном Parquet | да |
| G02 | Гигиена | путевые колонки (`canonical_path`, `storage_relpath`, `log_ref`, `input_ref`) относительны или логические: без букв дисков, `/home`, `..` | да |
| G03 | Гигиена | в `errors.message` нет абсолютных путей | WARN |

## 12. Тест-план (`tests/corpus/`, только синтетика, CP-11)

Для всех тестов:

- `pytest.importorskip("pyarrow")` и `importorskip("duckdb")`; маркеров `services` и `gpu` у этих тестов нет;
- фикстуры генерируются в `tmp_path`: синтетические строки из «lorem»-токенов, без текста источников;
- бинарные фикстуры не коммитятся;
- хешируемый текст пишется байтами с `\n`.

| Файл | Что проверяет |
|---|---|
| `test_contracts_schema.py` | каждая модель → Arrow-схема; экспорт детерминирован и равен закоммиченному `schemas/corpus/`; нет запрещённых ключей и имён классов; fingerprint стабилен; nullable ⇔ `Optional`; голый `int` запрещён; enum закрыты (ReviewStatus без FACT); словари совпадают с §3 |
| `test_ids.py` | формат и разбор `page_id` (p, r, s); детерминизм `object_id`: те же входы → тот же ID, float-шум → тот же, другие origin, bbox, вид или `producer_key` → другой; якорь без bbox; `dup:n`; ID не зависят от порядка строк (перемешивание); регулярные выражения принимают и отвергают примеры; ключи имён (ё/е, точки и пробелы, письменность, инициалы ≠ полные имена); `artifact_id` = sha256 байтов; формат run, commit и snapshot; `work_id` по якорю и цепочке tombstone |
| `test_registry_import.py` | синтетический реестр и синтетические файлы: PRESENT_VERIFIED, MISSING при ACTIVE (ошибка), MISSING при ABSENT_BY_REGISTER (SKIPPED), SHA256_MISMATCH, SIZE_MISMATCH, LFS-указатель; сигнатура после BOM; 18 значений области → §3.4, неизвестное — ошибка; правила review CP-06; смена sha vs прошлый снимок → `SOURCE_BINDING_CHANGED`; загрузка `WORK_REGISTER` и `WORK_LINKS` с проверкой сигнатур отношений |
| `test_parquet_io.py` | модели → Parquet → pyarrow, polars и DuckDB: значения и типы совпадают (timestamp UTC, `list<string>`, `list<struct>`, `date32`, int16/32); KV-метаданные на месте; одинаковые строки → одинаковые байты; null в non-nullable → понятная ошибка до записи; сбой посреди записи → цели нет, tmp убран; `union_by_name` на аддитивной версии; на Windows `fsync` через `r+b` |
| `test_commit_snapshot.py` | маркер → manifest (сортировка, хеши, строки); повторная обработка одного источника меняет в head только его файлы; сироты без маркера невидимы и находятся `gc --dry-run`; `CURRENT` меняется атомарно и только после PASS; датасет с 0 строк представлен файлом; перенос строк сохраняет провенанс; одинаковый контент → no-op коммит; два коммита одного источника → последний, другой в `superseded_commits` |
| `test_duckdb_views.py` | сборка из синтетического снимка: view и macro из §9.3 существуют; эталонные ответы §9.4; `provenance_trace` возвращает полный набор колонок; повторная сборка даёт тот же хеш результатов; материализованный режим и режим view дают одинаковые ответы; `meta.snapshot` = manifest |
| `test_canonical_validator.py` | хороший снимок → PASS; каждая внесённая ошибка роняет свою проверку §11: дубль PK, висящие page, artifact и run, FACT в статусе, OCR без модели, sha ≠ реестр, дыра в страницах, FAILED без ошибки, неизвестный флаг, тип рисунка ниже порога, `crs_status` при PAGE_SPACE, пустое поле провенанса, источник без главной связи, `page_count` ≠ числу `pages`, AUTO_PROPOSED-группировка, запрещённая колонка, абсолютный путь |

Интеграция с реальным корпусом (`VKM_RESOURCES_ROOT`) — отдельные тесты с маркером `corpus`. По умолчанию они skip
(NOT_RUN) и нужны канарейке Phase 2.

## 13. Открытые вопросы и decision notes координатору

### 13.1 Decision notes

| ID | Вопрос | Предложение D | Кто решает |
|---|---|---|---|
| DN-D-01 | Где хранить назначения Work | PRIVATE `00_registry/work_registry/`: `WORK_REGISTER.csv` + `WORK_LINKS.csv` + `RECEIPT.json` + `README_RU.md`. Bootstrap-команда в PUBLIC пишет предложения в `$VKM_WORK`, коммит в PRIVATE — координатор с receipt. Сборка канона `work_id` не выдумывает | координатор (согласуется с CP-09) |
| DN-D-02 | Откуда метаданные Work | порядок источников §13.2; конфликты не усредняются — выбранное значение + `metadata_basis` + запись в отчёт неоднозначностей | координатор |
| DN-D-03 | Буквы единиц страниц | `p` физическая, `r` закреплённый рендер DOCX, `s` spine EPUB; A предлагал `x` | координатор, H |
| DN-D-04 | ID объектов | регион (bbox) + origin + `producer_key`, не ординал (§0.3, §4.4). Инвариант B07. `object_lineage` при смене производителя | H |
| DN-D-05 | Смысл `pipeline_version` | semver контракта обработки, не git-коммит; две подписи — `stage_signature` и `call_signature` (кеш модели без GPU) | координатор + C |
| DN-D-06 | Физическая модель | неизменяемые партиции + маркеры коммитов + manifest + `CURRENT`. Альтернатива — hive-перезапись по датасетам — отвергнута: нет атомарности между датасетами, «висящие» файлы | координатор, H |
| DN-D-07 | Режим DuckDB | материализация по умолчанию (в 20 раз быстрее в пробе), view — интерфейс; файл пересобирается по manifest | координатор |
| DN-D-08 | Добавления к словарям | `NOT_PROCESSED` (страница и источник), `SKIPPED_BY_REGISTER` (CP-05), `review_status = NOT_APPLICABLE` (013, 022) | H |
| DN-D-09 | Политика версий | `0.x` (MINOR несовместим) до снимка полного прогона, затем `1.0.0` и строгий semver | координатор |
| DN-D-10 | `available_from` | в каноне NULL/UNKNOWN, пока не курировано; D-03 и D-16 — только во view `works_availability` с основанием `ASSUMED_FROM_PUBLICATION`. A допускал любой из двух вариантов | координатор |
| DN-D-11 | Идентичность авторов | `NAME_KEY_ONLY` без межписьменного слияния; риск однофамильцев явный. Альтернатива «автор только внутри работы» теряет граф соавторства | координатор, H |
| DN-D-12 | Чужое содержимое страниц | `source_work_links.link_type = FOREIGN_CONTENT`, `work_id` может быть NULL для неопознанной чужой работы. Альтернатива — работы без Source (`VKM-WRK-X…`) — в v0 отложена | H |
| DN-D-13 | Дубли страниц | курированные `SHARES_PAGES_WITH` с соответствием страниц + авто-кандидаты во view по `text_sha256`; ничего не сливается | координатор |
| DN-D-14 | Смена sha источника | связка неизменна; перепривязка только через correction-запись PRIVATE с receipt, иначе `SOURCE_BINDING_CHANGED` | координатор |
| DN-D-15 | Экспорт схем | модуль `vkm_corpus.contracts.export` и CLI, `schemas/corpus/` коммитится; `scripts/` не трогаем (CP-01) | координатор |
| DN-D-16 | Lock | `requirements/corpus.lock.txt`: pyarrow 25.0.1, duckdb 1.5.5, polars 1.44.2; `-c worldspec.lock.txt` (pydantic 2.13.5 / 2.46.5) | координатор (CP-02) |
| DN-D-17 | Запись между хостами | staging на WORKSTATION → синхронизация неизменяемых файлов → маркеры последними → снимок только на CORE (единственный писатель) | координатор (CP-15), B, C |
| DN-D-18 | Неоднозначные строки разведки | PWL-0007 (066 против «связанной» 036) и PWL-0127 (103 против 016): `local_status` не парсить регулярным выражением. Связь PWL → Source — колонка `acquired_2026_09_27` + **первый** токен `ALREADY_LOCAL:`; спорное — в отчёт куратору | координатор |
| DN-D-19 | Контейнеры и 205 | один Work на файл; компоненты — `external_ids` COMPONENT; «contains» у 205 не брать | согласовано с A, CP-09 |
| DN-D-20 | Расхождение числа страниц 053 | `documents.page_count` — из файла (384); подсказка реестра (423) — `register_page_count_hint` + флаг; правка реестра отдельно (CP-10) | координатор |
| DN-D-21 | Приёмочные счётчики | view `corpus_counts`: 251 = COMPLETE + PARTIAL/NEEDS_REVIEW + FAILED/UNSUPPORTED + NOT_PROCESSED + SKIPPED_BY_REGISTER; PARTIAL и NEEDS_REVIEW складываются в `sources_partial` | координатор, I |

### 13.2 Источники библиографических метаданных Work: что нашлось и что покрывает

| # | Источник | Где | Поля | Покрытие (из 251) | Оговорки |
|---|---|---|---|---|---|
| 1 | Таблица литературной разведки `PHYSICAL_WORLD_SOURCE_PRIORITY.csv` (765 строк) | PRIVATE `00_registry/literature_hunt_2026-09-27/` | `title_ru`, `title_en`, `title_variants`, `authors_ru`, `authors_translit`, `year`, `source_type`, `venue`, `volume`, `issue`, `pages`, `doi`, `isbn`, `publisher_city`, `language`, `alt_editions` | **225 источников** через 240 строк PWL. Связь: `acquired_2026_09_27` (221 строка → 205 источников) + первый токен `ALREADY_LOCAL:VKM-SRC-NNN` в `local_status` | в `local_status` бывает «связана с ALREADY_LOCAL:…» (не та же работа) — DN-D-18; контейнеры 193 (10 PWL), 203 (5), 089 (4), 037 (2) |
| 2 | Покрытие Phase 1 `SOURCE_COVERAGE_MASTER.csv` | PRIVATE `11_evidence_vnext/merged/` (public-safe копия — PUBLIC `evidence/sources/`) | `title`, `authors`, `year`, `document_type`, `pages` | **41** (001–041); 20 пересекаются с №1 | прочитано в Phase 1 — лучшее основание для 001–041 |
| 3 | Реестр, колонка `notes` | PRIVATE `00_registry/SOURCE_REGISTER.csv` | полуструктурированная библиография «Автор; год; заглавие; [EN]; …», `Identity:`, `Relations:`, DOI и ISBN | `Identity:` — 187 строк; `Relations:` — 56; DOI — 121; ISBN — 36; строк вида «Автор; год; …» — 57 | свободный текст: только `AUTO_PROPOSED` |
| 4 | Intake-манифесты | PRIVATE `00_registry/intake/*/IMPORT_MANIFEST.csv` | `exact_identity` (9), `identity_basis` (65 строк, 45 id), `hunt_link` (24), `global_id → resource_id` (131), `duplicate_of`, `pages` | **все 210** источников 042–251 (+034 как `duplicate_of`) | основания идентичности; у DjVu — «по имени и числу страниц» |
| 5 | OA-манифесты разведки | PRIVATE `…/literature_hunt_2026-09-27/oa/` | DOI или URL, `first_author`, `title`, `year` (OpenAlex, Semantic Scholar, CORE, сайт журнала) | через PWL (часть из №1) | внешний каталог, по файлу не проверен → `EXTERNAL_CATALOGUE` |
| 6 | Встроенные метаданные файла | `documents.file_meta_*` при извлечении | Info и XMP PDF, OPF EPUB (dc:identifier, dc:language), docProps DOCX | до 249 присутствующих файлов; заполненность неизвестна до извлечения | только подсказка (`FILE_EMBEDDED_METADATA`), никогда не перекрывает №1–4 |
| 7 | `source_family_membership.csv` | PUBLIC `evidence/sources/` | `title`, `authors`, `year` | 40 | производное от №2 |

Объединение №1 и №2:

| Поле | Покрыто |
|---|---|
| заглавие | 246/251 |
| авторы | 245 |
| год | 236 |
| venue | 177 (только из №1) |
| DOI | 129 (121 из разведки ∪ 121 из notes) |
| ISBN | 37 |

Оставшиеся 5 — 147, 209, 230, 241, 242 — есть только в notes реестра (№3).

Порядок для bootstrap:

1. №2 — для 001–041;
2. №4 `exact_identity` — проверено по титулу;
3. №1 — разведка;
4. №3 — notes;
5. №6 — только если пусто, с флагом.

`identity_status = FILENAME_PAGECOUNT_UNVERIFIED` остаётся у 8 DjVu без проверки титула (CP-10), пока C не проверит
титулы.

### 13.3 Вопросы к другим агентам

- **C:**
  - окончательный список Stage и критерии `document_class`;
  - формат сырых артефактов GLM-OCR и JSON векторных путей (с матрицей → PAGE_PT_TL);
  - правила нормализации текста и LaTeX, закреплённый профиль рендера DOCX;
  - подтвердить, что детекторы детерминированы при фиксированных входах: на этом держится B04 и B07.
- **E:** отображение `link_type` в рёбра графа. Рекомендация: FULL_COPY, PARTIAL_COPY, FRONT_MATTER_ONLY и PART →
  `INSTANCE_OF` со свойством `link_type`; FOREIGN_CONTENT — отдельное ребро `CARRIES_FOREIGN_CONTENT_OF` или
  свойство страницы. CITES — только из view `cites`.
- **G:** MCP-ответ несёт `object_id`, `source_id`, `page_id`, `page_kind`, `review_status`, `quality_flags`,
  `record_role`, `raw_artifact_id`, `snapshot_id`. Источник — macro `provenance_trace`.
- **H:** DN-D-03, 04, 08, 11, 12; инварианты B04, B07, E08, E09, F08.

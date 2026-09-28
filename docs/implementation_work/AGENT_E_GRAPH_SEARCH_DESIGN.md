# AGENT E: Neo4j DOCUMENT graph и OpenSearch retrieval-проекция. Дизайн (VKM Corpus Platform v0)

Дата: 28.09.2026 · Агент E (GRAPH / SEARCH ENGINEER) · Phase 0 (design, read-only).
Статус: ПРЕДЛОЖЕНИЕ координатору, ничего не реализовано и не развёрнуто.

**Основания.**

- `00_COORDINATOR_BRIEF.md`.
- Постановка пользователя (копия координатора): §2 роль E, §24, §26–28, §32, §42, §46, §50, §58–59, §64.
- `THEORY_TO_SCIENTIFIC_DATA_SYSTEM_CONTRACT_V0_2_RU.md`: §4–8, §36, §50–53, §58, §61.
- Документация Neo4j и OpenSearch (Context7); версии сверены по endoflife.date, GitHub releases и PyPI.
- PRIVATE `SOURCE_REGISTER.csv`: только распределения значений колонок, read-only.
- Ревизия того же дня по сообщению координатора. Учтены:
  - `AGENT_A_REPO_CONTRACT_AUDIT.md`: §2.8 (группы Work ≠ Source, общие и чужие страницы, контейнеры), K6, K11, K12,
    K14, §7 п. 12–18;
  - `COORDINATOR_DECISIONS.md` CP-02…CP-13, CP-18;
  - `AGENT_B_INFRA_INVENTORY.md`: CORE, порты, память, Docker;
  - `AGENT_G_API_MCP_CAD_DESIGN.md`: ожидания G от E — `build_id`, `built_from_snapshot_id`, типы рёбер, поля
    фильтров.

Документа агента D (`AGENT_D_CANONICAL_DATA_DESIGN.md`) на момент написания **нет**. Поэтому все предположения о полях
канона перечислены в §1.1 и требуют сверки с D. Проектор написан так, что сопоставление полей декларативно (одна таблица
на label или индекс), и расхождение с D правится в одном месте.

**Пробы Phase 0.** Одноразовый git-ignored venv в `<PUBLIC>/work/corpus_platform/phase0/agent_e/`.

- Установлены и импортированы `neo4j` 6.3.1, `opensearch-py` 3.2.0, `pyarrow` 25.0.1 и `duckdb` 1.5.5 под CPython 3.13.
  Сверены сигнатуры API (`Driver.execute_query`, `Session.run`, `helpers.streaming_bulk`, keyword-only
  `indices.create/update_aliases`).
- Все JSON-mappings и запросы этого документа сгенерированы и проверены: они валидны как JSON, ссылки на
  analyzer/normalizer разрешаются, ключей из `FORBIDDEN_COLUMNS` стража утечки нет.
- Регулярное выражение LaTeX-токенайзера проверено на синтетической формуле.
- Алгоритмы Snowball-russian и Porter-english опробованы на общих терминах предметной области, не на тексте источников.
  Результаты — в §4.7.

Живые Neo4j и OpenSearch **не запускались**: Docker Desktop остановлен по условию. Всё, что требует сервера, помечено
«проверить на этапе 3».

---

## 0. Итог

| Вопрос | Решение | Раздел |
|---|---|---|
| Версия Neo4j | Community **5.26 LTS**, последний патч на 28.09.2026 — 5.26.31, поддержка до 06.2028. Код и Cypher совместимы с CalVer **2026.09.x** — это альтернатива (DN-E03) | §7.1 |
| Версия OpenSearch | **3.8.0** (текущая линия 3.x, релиз 05.08.2026). Линия 2.19.x — maintenance, для новой установки не берём | §7.1 |
| Python-клиенты | `neo4j` 6.3.1 (поддерживает серверы 4.4/5.x/2025.x/2026.x), `opensearch-py` 3.2.0. Канон читается через `duckdb` 1.5.5 поверх Parquet (`pyarrow` 25.0.1) | §2.3, §6.6 |
| Метка логического слоя | Дополнительный label **`:DocumentLayer`**, а не свойство `layer`. Type labels глобально уникальны. Каждый relationship type принадлежит ровно одному слою (реестр в коде) | §1.2, §3 |
| Что лежит в графе | Структура, идентичность, статусы и указатели провенанса. **Текстов нет**: ни страниц, ни блоков, ни подписей, ни LaTeX, ни строк библиографии. Текст хранят Parquet (канон) и OpenSearch (поиск) | §1.3 |
| Узлы / рёбра | 10 labels по постановке. Relationship types: 10 обязательных, плюс `Page HAS_BIBLIOGRAPHY_ENTRY` (страница-носитель), `BibliographyEntry REFERENCE_OF Work` (чьему списку литературы принадлежит запись; только если `citing_work_id` известен) и `BibliographyEntry RESOLVES_TO Work`. Рёбер `Work/Source HAS_BIBLIOGRAPHY_ENTRY` нет: запись на чужой странице нельзя приписывать Work хоста (A K11). `CITES` — одно ребро на пару работ, только из принятых связей | §1.4–1.5 |
| Связи Work/Source и дубли | Типизированные рёбра из курируемой таблицы связей (CP-09): `ABSTRACT_OF`, `VOLUME_OF`, `EDITION_OF`, `NOT_SAME_AS` и т. д. (Work → Work); `DERIVED_FROM`, `CONTAINS_COPY_OF` (Source → Source). `SAME_WORK` выражается через `INSTANCE_OF` к одной Work. Дубли страниц — `Page DUPLICATE_CANDIDATE_OF Page` без слияния (A K12) и `dup_group_id` в индексе. Кандидаты автоматического разбора в граф не идут | §1.4–1.5 |
| Rebuild Neo4j | Онлайн-проектор на Python-драйвере. Режим `refresh` (MERGE + sweep устаревшего, по умолчанию) и режим `wipe` (удалить слой DOCUMENT батчами, затем загрузить; для acceptance и смены схемы). `neo4j-admin database import full` — только документированный резерв: нужна полная остановка СУБД, в Community одна БД, `--schema` есть только в Enterprise | §2.2 |
| Проверки после сборки | Counts по labels и types равны counts канона. Нет «сирот». Инварианты заменяют недоступные в Community existence-ограничения. «Повышенные» статусы в слое DOCUMENT запрещены. Выборочная трассировка к канону. `content_digest` графа равен `expected_digest`, посчитанному из канона | §2.7–2.8 |
| Модель OpenSearch | **Индекс на тип объекта**: pages, blocks, figures, tables, formulas, bibliography; works — опционально. Group alias `vkm-objects` объединяет figures + tables + formulas. `dynamic: strict`, версия в имени индекса, атомарный alias swap | §4.1, §6 |
| Анализаторы | Один `vkm_text`: цепочка ru-Snowball → en-Porter, безопасная на смешанном тексте (проверено). Подполе `.exact` без стемминга. Свёртка ё→е. `keyword_marker` для названий минералов на «-ит»: без него все 10 проверенных минералов разваливаются на два токена. Отдельный case-sensitive LaTeX-анализатор | §4.3, §4.7 |
| Путь к реранку | OpenSearch возвращает канонические ID и подсветку. Канонический текст API берёт **из DuckDB по ID**, а не из индекса, и сверяет `text_sha256`. В реранк идут пассажи (blocks, collapse по `page_id`) | §5 |
| Безопасность | Neo4j и OpenSearch слушают только loopback CORE: на CORE есть tailnet с чужими устройствами, а Docker обходит firewall (инвентарь B). Security plugin OpenSearch выключен (обоснование в §7.2). Neo4j с паролем из Docker secret. API/MCP читают граф только READ-транзакциями и без Cypher-пасса | §7.2 |
| Тесты | Офлайн контракт-тесты на синтетическом каноне: генерация Cypher, строк, bulk-документов, mappings, запросов, digest. Live-тесты с маркером `services` (CP-11): без сервиса — NOT_RUN, а флаг `--require-live` для агента I превращает NOT_RUN в FAIL | §8 |

**Оценки времени.** Масштаб: ~0,3–0,9 млн узлов и ~0,3–0,95 млн рёбер; ~0,3–0,8 млн документов OpenSearch.

- Полный `wipe`-rebuild Neo4j с проверками и digest: **~4–12 мин**.
- `refresh`: сопоставимо.
- Полная пересборка всех индексов OpenSearch с forcemerge и проверками: **~5–12 мин**.

Это расчётные оценки. Фактические числа дадут canary-прогоны (§2.9, §6.6), и receipts запишут измеренное время.

**Что нужно от других агентов**, подробнее в §9.3.

- От D:
  - запечатанный манифест снапшота канона (`snapshot_id`);
  - `host_page_id` и `citing_work_id` у записей библиографии (A K11);
  - канонический датасет `citations`;
  - курируемые связи Work/Source (CP-09) и кандидаты дублей страниц (A K12) как датасеты;
  - фильтр текущих версий объектов (`supersedes`, A п. 12);
  - `available_from` или явное UNKNOWN.
- От F и G: окно усечения rerank-текста. Транспорт изображений решён в CP-18: base64 от API.
- Ответы B, CP-07 и A уже закрыли вопросы о портах, Docker, `vm.max_map_count`, нормализации скоупа и источниках
  метаданных Work (§9.2).

---

## 1. Neo4j DOCUMENT graph: модель

### 1.1 Входные допущения о каноне (до документа D)

Имена датасетов и полей ниже — **допущения** (ASSUMPTION). Проектор берёт из канона только перечисленное.

| Датасет (допущение) | Поля, которые использует E | Где |
|---|---|---|
| манифест снапшота | `snapshot_id`; список файлов по датасетам (относительные пути под `$VKM_DATA_ROOT/canonical/`); `row_count`; `sha256` файлов; `schema_version` датасетов | Neo4j, OS |
| `sources` | `source_id` (`^VKM-SRC-\d{3}$`, A K7), `work_id`, `sha256`, `canonical_path` (относительный путь в PRIVATE), `file_type`, `source_class`, `copy_kind` (FULL, PARTIAL_FRONT_MATTER, PART_n, DERIVED_MERGED, CONTAINER, RETIRED; A K4), `lifecycle_status` (ACTIVE, ABSENT_BY_REGISTER, RETIRED; CP-05), `processing_status` (включая `SKIPPED_BY_REGISTER` с кодом причины; CP-05), `review_status` и `review_status_basis` (CP-06), `site_scope_raw` (дословно `evidence_scope`), `site_scope` (список `vkm_world.Scope`), `site_scope_mapping` (CP-07), `priority`, `page_count`, envelope | Neo4j, OS (денормализация) |
| `works` | `work_id` (предложение A: `VKM-WRK-NNN` от якорного Source; CP-09), `title`, `year`, `available_from` (в v0 — UNKNOWN или основание `ASSUMED_FROM_PUBLICATION`, A п. 11), `work_type`, `language`, `doi`, `isbn`, `venue_id`, `volume`, `issue`, `page_range`, `in_corpus`, `identity_basis`, `metadata_basis`, `external_ids` (`CW-`, `PWL-`, `EXT-SRC-`…; CP-09), `review_status`, `quality_flags`, `schema_version` | Neo4j, OS |
| `work_relations` (CP-09, курируемая таблица из PRIVATE `00_registry/`) | `relation_id`, `relation_type` (SAME_WORK, PART_OF, VOLUME_OF, SERIES_PART, ABSTRACT_OF, COMPANION, EDITION_OF, NOT_SAME, CONTAINS_COPY; плюс `DERIVED_FROM` из A §2.8, если D его вводит), `from_id`, `to_id` (Work или Source), `basis`, `review_status` (курировано или AUTO_PARSED_UNREVIEWED) | Neo4j (типизированные рёбра, только курированные) |
| `page_duplicate_candidates` (A K12) | `pair_id` или `dup_group_id`, `page_id_a`, `page_id_b`, `basis` (хеш нормализованного текста или рендера), `score`, `review_status` | Neo4j (`DUPLICATE_CANDIDATE_OF`), OS (`dup_group_id`) |
| `supersedes` (A п. 12) | `old_object_id`, `new_object_id`, `basis`; у объектов — `object_status` (CURRENT, SUPERSEDED) и `content_sha256` | фильтр текущих объектов; будущая переякорка (§3.3) |
| `work_authors` (связь) | `work_id`, `author_id`, `position` (с 1), `role` (не null), `name_as_printed`, `review_status` | Neo4j (AUTHORED_BY), OS (`authors`) |
| `authors` | `author_id`, `display_name`, `name_key`, `name_variants`, `review_status`, `schema_version` | Neo4j, OS |
| `venues` | `venue_id`, `name`, `venue_type`, `issn`, `review_status`, `schema_version` | Neo4j |
| `pages` | `page_id` (предложение A: `VKM-SRC-NNN:pNNNN`; EPUB — spine; CP-08), `source_id`, `page_number` (физический индекс 1..n без пропусков), `page_kind`, `printed_page_raw` и `printed_page_labels` (развороты 037 и 243 дают несколько меток; CP-08), `page_status` (NATIVE_OK, OCR_REQUIRED, OCR_OK, PARTIAL, UNSUPPORTED, FAILED, NEEDS_REVIEW), `native_text_status`, `ocr_status`, `origin`, `language`, `render_artifact_id`, нормализованный `text`, `text_sha256`, envelope | Neo4j (без `text`), OS |
| `blocks` | `block_id`, `page_id`, `source_id`, `reading_order`, `block_type`, `bbox` + `bbox_space` (page space), `text`, `text_sha256`, `origin`, `language`, envelope | Neo4j (без `text`), OS |
| `figures` | `figure_id`, `page_id`, `source_id`, `bbox`, `bbox_space`, `caption`, `caption_block_id`, `object_label` (например `3.1`), `object_label_raw` (например «Рис. 3.1»), `figure_type` (или `UNKNOWN_FIGURE_TYPE`), `figure_type_confidence`, `layout_class`, `is_vector`, `geometry_status`, `image_artifact_id`, `vector_artifact_id`, `text` (текст внутри рисунка, необязателен), envelope | Neo4j (без текстов), OS |
| `tables` | `table_id`, `page_id`, `source_id`, `bbox`, `bbox_space`, `caption`, `object_label`, `object_label_raw`, `n_rows`, `n_cols`, `text` (линеаризованная нормализованная таблица), `image_artifact_id` (crop), `raw_artifact_id`, `normalized_artifact_id`, envelope | Neo4j (без текстов), OS |
| `formulas` | `formula_id`, `page_id`, `source_id`, `bbox`, `bbox_space`, `equation_number`, `formula_kind`, `latex` (нормализованный, nullable), `text` (необязательная плоская форма), `image_artifact_id` (crop), `raw_artifact_id`, envelope | Neo4j (без LaTeX), OS |
| `bibliography` | `entry_id`, `source_id`, `host_page_id` (страница-носитель, стартовая; **не null**), `citing_work_id` (чей это список литературы; может быть UNKNOWN — например, список предыдущей статьи на первой странице выпуска; A K11), `ordinal`, `text` (строка записи), разобранные `bib_title`, `bib_authors`, `bib_year`, `doi`, `isbn`, envelope | Neo4j (без текстов), OS |
| `bibliography_links` | `link_id`, `entry_id`, `work_id`, `link_status` (принята / кандидат / отклонена), `match_method`, `match_score`, `matcher_version`, `review_status` | Neo4j (RESOLVES_TO), OS |
| `citations` (рекомендуется, DN-E06) | `citation_id`, `citing_work_id`, `cited_work_id`, `via_entry_ids`, `link_ids`, `match_methods`, `match_scores`, `support_count`, `rule_version`, `review_status` | Neo4j (CITES) |
| `artifacts`, `processing_runs` | в граф и индекс идут только ID | API (G) |

«Envelope» — это провенанс-конверт брифа §5: `schema_version, object_id, source_id, page_id, source_sha256,
pipeline_version, processing_run_id, extractor_id, extractor_version, model_id, model_revision, config_hash, created_at,
review_status, quality_flags`. Graph и index несут **подмножество** конверта, достаточное для фильтров и трассировки.
Полный конверт по ID отдаёт DuckDB (MCP `trace_document_provenance`).

Общие правила чтения канона проектором:

- **ID непрозрачны.** Грамматику ID проверяет только `vkm_corpus.ids` (D). Проектор ID не парсит и не строит, кроме
  собственных системных (`VKM-PRJ-…`). Занятые префиксы (A K6) проекторы не используют.
- **Только текущие версии объектов.** Если в снапшоте лежат и заменённые версии (`object_status = SUPERSEDED`), в граф и
  индекс идут только `CURRENT`. Ожидаемые counts считаются с тем же фильтром. Таблица `supersedes` в v0 остаётся в
  каноне (§3.3).
- **Граф цитирований Phase 1 не импортируется.** Он получен LLM-чтением (evidence), а не автоматикой v0, поэтому служит
  оракулом для оценки матчера и crosswalk: `CW-`/`WG-` → `Work.external_ids`. Авто-объектами он не становится (A K11).
- **Курируемые связи — да, кандидаты — нет.** В граф идут связи Work/Source только курированной таблицы. Связи,
  автоматически разобранные из notes (`AUTO_PARSED_UNREVIEWED`), и спорные строки (например, «contains» у 205 из A §2.8)
  остаются в каноне.

### 1.2 Разметка логического слоя: label, а не свойство

Решение: каждый узел DOCUMENT несёт свой type label (`:Page`, `:Figure` и т. д.) **и** layer label `:DocumentLayer`.
Будущие слои получают свои layer labels: `:EvidenceLayer`, `:PhysicsLayer`, `:RepresentationLayer`, `:ObservationLayer`,
`:ProvenanceLayer` (контракт §52).

Почему label, а не свойство `layer`:

1. Принадлежность к label хранится в записи узла и обслуживается встроенным token lookup index. Поэтому
   `MATCH (n:DocumentLayer)` — дешёвый label scan без дополнительного индекса. Со свойством `layer` пришлось бы строить
   range index на каждый type label или сканировать все узлы.
2. Ограничения в Neo4j задаются на label. Глобальное ограничение уникальности `(:DocumentLayer).id` ловит коллизию ID
   между типами. Оно же позволяет искать узел по ID, не зная его типа: для MCP `trace_document_provenance` и для будущих
   слоёв, которые ссылаются на document ID.
3. Операции над слоем (wipe, sweep, подсчёт, проверка) записываются одной строкой и физически не могут задеть узлы
   других слоёв.
4. Добавление слоя не меняет схему DOCUMENT: несколько labels на узле — штатная модель Neo4j.
5. Свойство легко забыть выставить. Label ставится централизованно в шаблоне MERGE (§2.5) и проверяется после сборки.

У связей labels нет, поэтому **каждый relationship type принадлежит ровно одному слою** по реестру `GRAPH_SCHEMA` в коде.
Межслойные рёбра принадлежат *зависимому* (верхнему) слою и направлены от его узлов к узлам нижнего слоя (§3).
Префиксы в именах типов (`DOC_HAS_PAGE`) не вводятся: уникальность обеспечивает реестр, а имена совпадают с постановкой и
контрактом.

Инварианты, которые проверяются после сборки:

- у каждого узла ровно один layer label;
- у каждого узла DOCUMENT ровно один type label из реестра;
- type labels глобально уникальны. Будущий слой не переиспользует `Formula` в другом смысле: reviewed-формула — это
  отдельный label `ReviewedFormula` (контракт §15).

### 1.3 Узлы и свойства

**Общий конверт каждого узла `:DocumentLayer`.** Имена одинаковы для всех labels.

| Свойство | Тип | Смысл |
|---|---|---|
| `id` | STRING | Стабильный канонический ID объекта. Единое имя поля для всех labels. Уникален внутри label и во всём слое |
| `source_id` / `page_id` / `work_id` | STRING | Типизированные ссылки на якоря (где применимо). У якорных типов дублируют собственный `id`: `Source.source_id`, `Page.page_id`, `Work.work_id`. Тогда предикат «у узла есть `source_id` и `page_id`» единообразен для acceptance §58 |
| `schema_version` | STRING | Версия схемы канонического датасета, из которого пришла строка |
| `review_status` | STRING | Из канона, значение проверяется по enum D. В слое DOCUMENT запрещены `FACT`, `REVIEWED_MEASUREMENT`, `ACCEPTED_PARAMETER`, `ACCEPTED_FORMULA` (проверка §2.7) |
| `quality_flags` | LIST<STRING> | Из канона. Всегда список, возможно пустой, без null-элементов |
| `origin` | STRING | NATIVE, OCR, MIXED, REGISTRY, METADATA или DERIVED (enum D) |
| `processing_run_id` | STRING | Указатель на `processing_runs` канона (processing provenance, контракт §37) |
| `projection_run_id` | STRING | Какой прогон проектора записал узел. Через узел `:ProjectionRun` ведёт к манифесту снапшота (§1.6) |

Датасет канона однозначно определяется type label по реестру (`Page` → `pages`). Поэтому `canonical_dataset` на узле не
хранится: это избыточные ~1 млн строк. Манифест тоже не хранится на узле: к нему ведёт `projection_run_id` →
`ProjectionRun.canonical_manifest_sha256`. Цепочка трассировки узла:
`(label, id)` → строка канона → `source_id`/`page_id` → `SOURCE_REGISTER` / sha256 источника.

**Свойства по labels.** Ниже только то, что добавляется к конверту.

| Label | Дополнительные свойства |
|---|---|
| `Work` | `work_id`, `title`, `year` (INTEGER), `available_from` (DATE; контракт §42; отсутствует при UNKNOWN), `work_type`, `language`, `doi`, `isbn`, `in_corpus` (BOOLEAN), `identity_basis`, `metadata_basis`, `external_ids` (LIST) |
| `Source` | `source_id`, `work_id`, `sha256`, `canonical_path` (относительный путь в PRIVATE, без машинных путей), `file_type`, `source_class`, `copy_kind`, `lifecycle_status`, `processing_status`, `skip_reason`, `review_status_basis`, `site_scope_raw`, `site_scope` (LIST), `site_scope_mapping` (CP-07), `priority`, `page_count` |
| `Page` | `page_id`, `source_id`, `page_number` (физический индекс 1..n), `page_kind`, `printed_page_raw`, `printed_page_labels` (LIST), `page_status`, `native_text_status`, `ocr_status`, `language`, `render_artifact_id`, `dup_group_id`, `text_sha256` (хэш канонического текста без самого текста) |
| `Block` | `source_id`, `page_id`, `reading_order`, `block_type`, `bbox` (LIST<FLOAT> x0,y0,x1,y1), `bbox_space`, `language`, `text_sha256` |
| `Figure` | `source_id`, `page_id`, `bbox`, `bbox_space`, `object_label`, `figure_type`, `figure_type_confidence`, `layout_class`, `is_vector`, `geometry_status` (статус CRS из брифа §3, без выдуманного EPSG), `image_artifact_id`, `vector_artifact_id`, `caption_block_id` |
| `Table` | `source_id`, `page_id`, `bbox`, `bbox_space`, `object_label`, `n_rows`, `n_cols`, `image_artifact_id`, `raw_artifact_id`, `normalized_artifact_id` |
| `Formula` | `source_id`, `page_id`, `bbox`, `bbox_space`, `equation_number`, `formula_kind`, `has_latex` (BOOLEAN), `image_artifact_id`, `raw_artifact_id` |
| `BibliographyEntry` | `source_id`, `page_id` (= канонический `host_page_id`), `citing_work_id` (отсутствует при UNKNOWN; тогда флаг `CITING_WORK_UNKNOWN`), `ordinal`, `doi`, `isbn`, `bib_year`, `link_status` |
| Block, Figure, Table, Formula, BibliographyEntry | плюс `content_sha256`, если его даёт D (A п. 12): по нему видно, что при новом ID содержимое не изменилось |
| `Author` | `display_name`, `name_key`, `name_variants` (LIST) |
| `Venue` | `name`, `venue_type`, `issn` (LIST) |

**Что в графе НЕ хранится.** Проектор работает по белому списку свойств: всё, чего нет в таблице выше, в граф не
попадает.

- Тексты любого вида: страница, блок, подпись, текст внутри рисунка, линеаризованная таблица, LaTeX, строка
  библиографии, разобранные `bib_title`/`bib_authors`. Их хранят Parquet (канон) и OpenSearch (поиск). Если граф
  хранит тексты, он становится вторым источником истины и незаметно устаревает. `text_sha256` на узле позволяет сверять
  граф, индекс и канон без копии текста.
- Сырые выходы моделей, векторы и embeddings. Байты изображений и пути к файлам — в графе только ID артефактов; пути
  разрешает API через канонический датасет `artifacts`.
- Свободный текст реестра (`scientific_role`, `notes`), абсолютные машинные пути, секреты.
- Map-свойства: Neo4j их не хранит. Структуры (bbox) плоско сворачиваются в список и `bbox_space`.
- Строки длиннее 1 KB. Проверка проектора; исключение по белому списку — только `Work.title`.

### 1.4 Relationship types

| Type | От → к | Кардинальность | Свойства | Канонический источник |
|---|---|---|---|---|
| `INSTANCE_OF` | Source → Work | ровно 1 у Source (0 → флаг `WORK_UNKNOWN` в отчёте). SAME_WORK-группа (013/025/202, 147/232, 208/209) — несколько Source одной Work | `copy_kind`, `part_index` и `printed_page_offset` (например, для частей 208/209), `link_method`, `review_status`, `projection_run_id` | `sources.work_id`, `copy_kind` и т. д. |
| `AUTHORED_BY` | Work → Author | N:M, упорядочено | `position` (INTEGER, с 1), `role` (AUTHOR, EDITOR, TRANSLATOR…), `name_as_printed`, `review_status`, `projection_run_id` | `work_authors` |
| `PUBLISHED_IN` | Work → Venue | 0..1 у Work | `volume`, `issue`, `page_range`, `review_status`, `projection_run_id` | `works.venue_id` и поля |
| `HAS_PAGE` | Source → Page | 1:N; у Page ровно 1 входящее | `projection_run_id` | `pages.source_id` |
| `PRECEDES` | Page → Page | цепочка внутри Source: `page_number` → `page_number + 1` | `projection_run_id` | производное правило `precedes_v1` (§2.6) |
| `HAS_BLOCK` | Page → Block | 1:N; у Block ровно 1 входящее | `projection_run_id` | `blocks.page_id` |
| `HAS_FIGURE` | Page → Figure | 1:N | `projection_run_id` | `figures.page_id` |
| `HAS_TABLE` | Page → Table | 1:N | `projection_run_id` | `tables.page_id` |
| `HAS_FORMULA` | Page → Formula | 1:N | `projection_run_id` | `formulas.page_id` |
| `HAS_BIBLIOGRAPHY_ENTRY` | Page → BibliographyEntry | 1:N; страница-носитель (`host_page_id`), может быть «чужой» | `projection_run_id` | `bibliography.host_page_id` |
| `REFERENCE_OF` | BibliographyEntry → Work | 0..1 у записи: только если `citing_work_id` известен | `review_status`, `basis`, `projection_run_id` | `bibliography.citing_work_id` (A K11) |
| `RESOLVES_TO` | BibliographyEntry → Work | 0..1 у записи; **только принятые** связи | `link_id` (уникален), `match_method`, `match_score`, `matcher_version`, `review_status`, `projection_run_id` | `bibliography_links` с принятым `link_status` |
| `CITES` | Work → Work | не больше одного ребра на упорядоченную пару | `id` (= `citation_id`, уникален), `via_entry_ids`, `link_ids`, `match_methods`, `match_scores` (списки выровнены по записям), `support_count`, `rule_version`, `review_status`, `projection_run_id` | `citations` (DN-E06) или правило `cites_v1` (§2.6) |
| `ABSTRACT_OF`, `VOLUME_OF`, `SERIES_PART_OF`, `PART_OF`, `EDITION_OF`, `COMPANION_OF`, `NOT_SAME_AS` | Work → Work | по строкам курируемой таблицы; направление по определению типа у D. Симметричные (`COMPANION_OF`, `NOT_SAME_AS`) хранятся одним ребром от меньшего `id` к большему, `symmetric: true` | `relation_id`, `basis`, `review_status`, `symmetric`, `projection_run_id` | `work_relations` (CP-09) |
| `DERIVED_FROM`, `CONTAINS_COPY_OF` | Source → Source | по курируемой таблице: 025 → 013; 022 ⊃ 023 | `relation_id`, `basis`, `review_status`, `projection_run_id` | `work_relations` (A §2.8) |
| `DUPLICATE_CANDIDATE_OF` | Page → Page | симметрично, одно ребро на пару (от меньшего `id`); только страницы разных Source | `pair_id`, `basis`, `score`, `review_status`, `symmetric: true`, `projection_run_id` | `page_duplicate_candidates` (A K12) |

Родовых связей нет: ни `RELATED`, ни `Relation(type=…)`, ни `LINKED_TO` (контракт §61). Тест реестра запрещает такие
имена (§8.3).

Связи Work/Source и дубли страниц — это **тоже слой DOCUMENT**: библиографические и документные факты с курированным
каноном, а не научные сущности. Реестр `GRAPH_SCHEMA` содержит закрытый список их типов, синхронный с enum
`relation_type` D. Новое значение enum без обновления реестра даёт `E_UNKNOWN_RELATION_TYPE`; «общий» тип при этом не
создаётся. Если таблица D ещё не готова, этих рёбер просто нет. Обязательные 10 типов от неё не зависят.

### 1.5 Обоснование решений по связям

**Bibliography.**

Размещение записи и её принадлежность — **два разных факта** (A K11). «Где напечатано» — это страница-носитель.
«Чей это список литературы» — это `citing_work_id`. Они расходятся на общих и чужих страницах. Например, на первой
странице статьи выпуска «Горного эха» может стоять конец списка литературы предыдущей статьи (A §2.8,
`foreign_page_references`). Поэтому в графе это два ребра.

- **`Page HAS_BIBLIOGRAPHY_ENTRY` — да, страница-носитель.** Запись библиографии — объект, расположенный на странице, как
  рисунок или таблица. Паттерн «Source → Page → объект» (контракт §4, document/provenance graph) остаётся единым. Запись,
  занимающая две страницы, привязывается к стартовой; `ordinal` задаёт порядок в списке литературы на носителе (индекс
  `(source_id, ordinal)`). Требование к D: `host_page_id` не null. Если D допускает записи без страницы (например, для
  EPUB без page-эквивалента), это оформляется явным резервом `Source HAS_BIBLIOGRAPHY_ENTRY` с флагом `PAGE_UNKNOWN`.
  Молча записи не теряются (DN-E05).
- **`BibliographyEntry REFERENCE_OF Work` — да, но только из канонического `citing_work_id`.** Принадлежность **не
  выводится** путём «Work хоста → Source → Page → запись». На чужой странице такой путь приписал бы запись не той работе.
  При `citing_work_id = UNKNOWN` ребра нет. Запись остаётся на странице-носителе, получает флаг `CITING_WORK_UNKNOWN` и
  не участвует в `CITES`. Две копии одной Work дают две записи с одним `citing_work_id`. Это ожидаемо: записи различают
  копии, а `CITES` агрегирует на уровне Work.
- **`Work HAS_BIBLIOGRAPHY_ENTRY` и `Source HAS_BIBLIOGRAPHY_ENTRY` — нет**, кроме резерва выше. Первое заменяет
  `REFERENCE_OF` с правильным направлением и явным основанием. Второе дублировало бы путь через Page, а каждое лишнее ребро
  — это риск рассогласования.
- **`BibliographyEntry RESOLVES_TO Work` — да.** Это факт уровня записи: какая запись с какой работой сопоставлена, каким
  методом (DOI, ISBN, title+authors+year…), с каким score, какой версией матчера и в каком статусе ревью. Постановка §24
  требует хранить именно это. В граф попадают **только принятые** связи — по правилу приёмки D, «хорошая уверенность» по
  §24. Кандидаты и отклонённые связи остаются в каноне и DuckDB для очереди ревью. Если проецировать кандидатов как
  `RESOLVES_TO`, потребители графа прочитают их как установленные.
- **`CITES` — одно ребро на пару (citing Work, cited Work).** Это библиографический факт уровня Work. Постановка §24
  требует связывать надёжно, и per-entry метаданные остаются на `RESOLVES_TO`. На `CITES` они сведены в выровненные
  списки (`via_entry_ids`, `match_methods`, `match_scores`) и `support_count`, **без схлопывания в одно число**: нет ни
  «максимального score», ни «среднего». Одно ребро на пару не даёт двойного счёта, когда две копии одной Work цитируют
  одно и то же. `support_count` — это число записей-носителей, **а не число независимых подтверждений**: копии одной
  Work дают несколько записей одного факта. `CITES` не несёт семантики согласия или поддержки, свойства `stance` нет
  намеренно: «цитирование ≠ согласие» (бриф §3). Самоцитирование через ошибочное слияние (citing = cited) не создаётся и
  попадает в отчёт. Цитирующая сторона берётся из `citing_work_id`, а не из Work хоста (§2.6).
- **Граф цитирований Phase 1** (`CW-`, `WG-`, `CG-`) в `CITES` **не превращается**. Это результат evidence-чтения, а не
  авто-извлечения v0. Он служит оракулом качества матчера для C и D и crosswalk для `Work.external_ids` (A K11).

**Прочие связи.**

- **`PRECEDES`** — физический порядок единиц-страниц внутри одного Source, по `page_number`. Это не печатная нумерация.
  Страницы со статусом FAILED — тоже узлы (без тихой потери страниц), поэтому цепочка полная: число `PRECEDES` равно
  `#Pages − #Sources_with_pages`. Для EPUB/DOCX смысл «страницы» задают C и D; PRECEDES просто следует за
  `page_number`.
- **`INSTANCE_OF`.** Source → Work, связь многие-к-одному: дубликаты, сканы и издания — разные Source одной Work
  (контракт §6). Курируемая группа SAME_WORK — это несколько `INSTANCE_OF` к одной Work: 013/025/202; 147/232 с
  `copy_kind = PARTIAL_FRONT_MATTER` у 147; 208/209 с `part_index` и `printed_page_offset`. Отдельного ребра
  `SAME_WORK` нет, иначе один факт хранился бы дважды. Пагинации копий автоматически не выравниваются (A §2.8).
  **Контейнеры** (089, 193, 203, 204, 205–207): в v0 один Work на файл, дочерние Work автоматически не создаются
  (CP-09). Их `PWL-` ID лежат в `external_ids`.
- **Типизированные связи Work и Source** (CP-09, A §2.8): `ABSTRACT_OF` (001 → 196), `VOLUME_OF` (229, 230),
  `SERIES_PART_OF` (020, 028), `COMPANION_OF` (002, 201), `EDITION_OF` (011), `NOT_SAME_AS` (037 и 014, 050, 034),
  `DERIVED_FROM` (025 → 013) и `CONTAINS_COPY_OF` (022 ⊃ 023).
  - **`NOT_SAME_AS` — отрицательная связь.** Она хранится, чтобы автоматика не склеила похожие работы. Проверка C13 и
    preflight запрещают слить две `NOT_SAME_AS`-работы в одну Work: в графе это была бы петля.
  - **Конец связи вне канона.** Если конец связи — внешняя работа, которой нет в канонических `works` (например, первое
    издание для `EDITION_OF`), ребро не создаётся. Связь остаётся в каноне и перечисляется в отчёте как
    `RELATION_ENDPOINT_EXTERNAL`.
- **`DUPLICATE_CANDIDATE_OF`** (A K12): совпадение нормализованного текста или рендера у страниц **разных** Source.
  Например, общие страницы выпусков «Горного эха» (031 с. 5 и 005 с. 1). Страницы не сливаются. Ребро только помечает,
  что это один физический текст: дубль страницы ≠ независимое evidence. В индексе тот же факт выражен полем
  `dup_group_id` для схлопывания выдачи (§5.3).
- **`AUTHORED_BY`.** `name_as_printed` сохраняет исходное написание имени в данной работе. Узлы `Author` создаются
  **только** из канонических метаданных работ. Из разобранных строк библиографии они не создаются: слияние идентичностей
  по строкам ссылок было бы агрессивным и ненадёжным.

### 1.6 Узел `:ProjectionRun` (системные метаданные проекции)

`(:ProjectionRun)` не принадлежит ни одному научному слою: layer label у него нет, и wipe или sweep слоя его не удаляют.
Узел создаётся на каждый прогон и хранит историю. Свойства:

- `id`, например `VKM-PRJ-DOC-20260928T101500Z-3fa9c2d1`. Префикс не входит в занятые (A K6), двоеточий нет: ID
  годится и как имя receipt-файла. Для API (G, A6) это `build_id` графа;
- `layer` = `DOCUMENT`;
- `graph_schema_version`, например `doc-graph/1.0`;
- `mode` (`refresh`, `wipe`, `scoped`) и `scope` (список `source_id` или пусто);
- `status` (`WIPING`, `REFRESHING`, `COMPLETE`, `FAILED`);
- `started_at`, `finished_at`;
- `built_from_snapshot_id` (термин G), `canonical_manifest_sha256`, `canonical_schema_versions` (LIST
  `датасет=версия`);
- `projector_version` (git commit PUBLIC и версия пакета), `neo4j_version`, `neo4j_edition`;
- `expected_digest`, `content_digest`;
- `counts_json` и `checks_json` — строки, потому что Neo4j не хранит map-свойства;
- `receipt_ref` — относительный путь receipt под `$VKM_DATA_ROOT`.

API (агент G) проверяет последний `ProjectionRun` слоя перед графовыми ответами:

| Статус | Поведение API |
|---|---|
| `WIPING` | 503 `GRAPH_REBUILDING` |
| `REFRESHING` | Отвечает с пометкой `graph_state: REFRESHING`: в refresh старые узлы живут до sweep |
| `FAILED` после wipe | 503 до успешной пересборки |
| `COMPLETE` | Обычный режим |

### 1.7 DDL (Cypher): ограничения и индексы

Особенности Neo4j Community, которые учитывает DDL (по документации Cypher Manual и Operations Manual):

- В Community доступны **только property uniqueness constraints** на узлы и связи.
- Existence (`IS NOT NULL`), property type (`IS :: TYPE`) и key constraints (`IS NODE KEY`, `IS RELATIONSHIP KEY`)
  доступны только в Enterprise. Graph types (`ALTER CURRENT GRAPH TYPE`) — тоже только Enterprise.
- Поэтому существование и типы свойств, а также метки концов рёбер проверяет проектор: до загрузки валидирует строки,
  после загрузки выполняет Cypher-инварианты (§2.7).
- Всё DDL идемпотентно (`IF NOT EXISTS`). Имена с префиксами `doc_` (слой) и `sys_` (метаданные), чтобы DDL будущих
  слоёв не пересекалось.

```cypher
// ---- слой DOCUMENT, graph schema doc-graph/1.0 ----
CREATE CONSTRAINT doc_layer_id_unique IF NOT EXISTS FOR (n:DocumentLayer) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT doc_work_id_unique IF NOT EXISTS FOR (n:Work) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT doc_source_id_unique IF NOT EXISTS FOR (n:Source) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT doc_page_id_unique IF NOT EXISTS FOR (n:Page) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT doc_block_id_unique IF NOT EXISTS FOR (n:Block) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT doc_figure_id_unique IF NOT EXISTS FOR (n:Figure) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT doc_table_id_unique IF NOT EXISTS FOR (n:Table) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT doc_formula_id_unique IF NOT EXISTS FOR (n:Formula) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT doc_bibliography_entry_id_unique IF NOT EXISTS FOR (n:BibliographyEntry) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT doc_author_id_unique IF NOT EXISTS FOR (n:Author) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT doc_venue_id_unique IF NOT EXISTS FOR (n:Venue) REQUIRE n.id IS UNIQUE;
// relationship property uniqueness (доступна в Community)
CREATE CONSTRAINT doc_resolves_to_link_unique IF NOT EXISTS FOR ()-[r:RESOLVES_TO]-() REQUIRE r.link_id IS UNIQUE;
CREATE CONSTRAINT doc_cites_id_unique IF NOT EXISTS FOR ()-[r:CITES]-() REQUIRE r.id IS UNIQUE;
// ---- системные метаданные проекции (вне научных слоёв) ----
CREATE CONSTRAINT sys_projection_run_id_unique IF NOT EXISTS FOR (n:ProjectionRun) REQUIRE n.id IS UNIQUE;

// ---- range-индексы: навигация, scoped-операции, поиск по идентификаторам ----
CREATE INDEX doc_page_source_number IF NOT EXISTS FOR (n:Page) ON (n.source_id, n.page_number);
CREATE INDEX doc_block_source IF NOT EXISTS FOR (n:Block) ON (n.source_id);
CREATE INDEX doc_figure_source IF NOT EXISTS FOR (n:Figure) ON (n.source_id);
CREATE INDEX doc_table_source IF NOT EXISTS FOR (n:Table) ON (n.source_id);
CREATE INDEX doc_formula_source IF NOT EXISTS FOR (n:Formula) ON (n.source_id);
CREATE INDEX doc_bibliography_entry_source_ordinal IF NOT EXISTS FOR (n:BibliographyEntry) ON (n.source_id, n.ordinal);
CREATE INDEX doc_bibliography_entry_citing_work IF NOT EXISTS FOR (n:BibliographyEntry) ON (n.citing_work_id);
CREATE INDEX doc_page_dup_group IF NOT EXISTS FOR (n:Page) ON (n.dup_group_id);
CREATE INDEX doc_work_doi IF NOT EXISTS FOR (n:Work) ON (n.doi);
CREATE INDEX doc_author_name_key IF NOT EXISTS FOR (n:Author) ON (n.name_key);
CREATE INDEX sys_projection_run_layer_started IF NOT EXISTS FOR (n:ProjectionRun) ON (n.layer, n.started_at);
```

Замечания к DDL:

- Ограничения уникальности сами создают backing range indexes, поэтому отдельный индекс на `id` не нужен.
- Full-text индексы Neo4j и vector indexes не создаются: поиск — роль OpenSearch (одна технология — одна роль).
- Индекс по `DOI` у Work без уникальности: совпадение DOI у двух Work — это дефект канона, его ловит валидация D, а не
  граф.
- Уникальность `relation_id` и `pair_id` у рёбер связей Work/Source и дублей проверяет preflight. Отдельные
  relationship constraints на каждый такой тип не заводятся: строк десятки, ограничений было бы больше, чем данных.
- DDL коммитится как `schemas/graph/document_graph_v1.cypher` с `eol=lf` (CP-13). Хэш DDL в receipt считается по байтам
  с `\n`.

---

## 2. Загрузка и rebuild Neo4j

### 2.1 Особенности Community, существенные для rebuild

- **Одна пользовательская БД.** «In Community Edition, the default database is the only database available, other
  than the system database». `CREATE DATABASE`, `STOP DATABASE` и `dbms.setDefaultDatabase` к Community не относятся.
  Параметр `initial.dbms.default_database` действует только до первого старта СУБД. Blue/green внутри одного инстанса
  невозможен.
- **`neo4j-admin database import full`** есть в Community. Ограничения:
  - он не работает с запущенной целевой БД — нужна остановка;
  - для существующей БД нужен `--overwrite-destination`, который уничтожает прежние файлы;
  - `--schema` (индексы и ограничения во время импорта) — только Enterprise, формат `block`. В Community DDL выполняется
    после старта;
  - `--input-type=parquet` есть в опциях актуальной документации. Доступность в Community для закреплённой версии
    проверить на этапе 3;
  - incremental import — только Enterprise.
- **RBAC нет.** Все пользователи Community имеют полный доступ. Read-only обеспечивается на стороне клиентов (§7.2).
- **Нет online backup, метрик и query log.** Граф не бэкапится вовсе: это проекция. Метрики и лог запросов ведут наши
  клиенты.

### 2.2 Стратегии rebuild: сравнение и выбор

| Критерий | A. Онлайн-проектор (Python driver, UNWIND-батчи, MERGE по `id`) | B. Офлайн `neo4j-admin database import full` |
|---|---|---|
| Простой | `refresh`: нет. `wipe`: граф «мягко недоступен» на минуты (статус `WIPING`) | Полная остановка СУБД на время импорта, рестарт и DDL (~2–5 мин) |
| Управление Docker/systemd из билдера | не нужно, достаточно Bolt-доступа | нужно: stop, one-off контейнер импорта на тот же volume, start |
| Вход | Parquet → DuckDB/Arrow → dict-батчи | промежуточный CSV (`id:ID(Page)`, `:START_ID`…) или Parquet в `$VKM_DATA_ROOT/tmp` |
| Скорость на ~1 млн узлов и рёбер | ~4–12 мин с проверками | ~1–3 мин импорта, плюс рестарт и DDL |
| Ограничения и индексы | DDL до загрузки (`IF NOT EXISTS`) | после старта; `--schema` только в Enterprise |
| Затрагивает | **только слой DOCUMENT** | **всю БД**, все будущие слои |
| Будущие слои | их узлы живут. В `refresh` межслойные рёбра к неизменившимся ID сохраняются | должны переимпортироваться вместе из своих канонов |
| Scoped refresh одного Source | да (P1) | нет |
| Атомарность для читателя | нет, её заменяет статус `ProjectionRun` | «старая БД → простой → новая БД» |
| Отказ посреди операции | частичный граф, статус `FAILED`, повторный запуск идемпотентен | при `--overwrite-destination` старая БД уже уничтожена. Нужен вариант «импорт в новый каталог + подмена bind-mount» |

**Решение: A — основной путь v0**, в двух режимах:

- `refresh` — по умолчанию. MERGE всех узлов и рёбер снапшота с новым `projection_run_id`, затем удаление узлов и рёбер
  DOCUMENT со старым `projection_run_id`. Онлайн, идемпотентно, сохраняет межслойные рёбра к неизменившимся ID.
- `wipe` — acceptance «удалить и восстановить», смена мажорной версии graph schema, подозрение на ручные правки. Слой
  DOCUMENT удаляется батчами (`CALL … IN TRANSACTIONS`), затем загружается.

Для acceptance §46 и §58 («удалить Neo4j и восстановить из канона») используется тот же код: остановить контейнер,
удалить `$VKM_DATA_ROOT/neo4j/data`, запустить, выполнить DDL и `wipe`-загрузку.

**B документируется как резерв (§2.11)** на случай роста масштаба на порядок или если измеренное время A на полном
корпусе превысит 30 мин. В v0 не реализуется.

### 2.3 Архитектура проектора

```
canonical snapshot manifest (D)            ← единственный вход: перечисленные файлы, а не glob каталога
  → DuckDB in-memory над read_parquet(...) ← проекции строятся из канона напрямую,
                                              не из файла vkm_corpus.duckdb (другая проекция)
  → preflight: ссылочная целостность, дубликаты ID, непрерывность page_number, enum-ы
  → row builders (по label / type; белый список свойств; производные правила)
  → batch writer (neo4j driver: execute_query с UNWIND $rows, по 5000 строк)
  → sweep / wipe (session.run: CALL … IN TRANSACTIONS)
  → verify (counts, сироты, инварианты, трассировка, digest)
  → ProjectionRun COMPLETE | FAILED + receipt JSON + JSON-логи
```

Правило для всех проекций (DuckDB, Neo4j, OpenSearch): **каждая строится прямо из одного и того же снапшота канона и
записывает его `canonical_manifest_sha256`**. Цепочек «проекция из проекции» нет. `/status` API показывает снапшоты всех
трёх проекций и помечает рассинхрон.

### 2.4 Алгоритм (`vkm-corpus graph rebuild`)

1. **Lock.** Файловый lock `$VKM_DATA_ROOT/locks/neo4j-document-projection.lock`. Если существует `ProjectionRun` в
   статусе `WIPING` или `REFRESHING`, а lock не удерживается, прежний прогон помечается `FAILED(stale)`. Если lock
   удерживается, запуск отклоняется с `E_PROJECTION_BUSY`.
2. **Preflight канона.**
   - Прочитать манифест, сверить sha256 файлов и поддерживаемые мажорные версии схем (`E_SCHEMA_VERSION_UNSUPPORTED`).
   - SQL-проверки в DuckDB. Каждая даёт полный список нарушителей в receipt, а не только первого:
     - дубликаты ID внутри датасета и между датасетами → `E_DUPLICATE_ID`;
     - FK: `pages.source_id ∈ sources`, `blocks.page_id ∈ pages`, согласованность `blocks.source_id` со страницей,
       figures/tables/formulas/bibliography аналогично, `links.entry_id ∈ bibliography`, `links.work_id ∈ works`,
       `work_authors`, `works.venue_id` → `E_DANGLING_REFERENCE`;
     - `page_number` каждого источника — ровно 1..n и `n = sources.page_count` → `E_PAGE_GAP`. У Source со статусом
       `SKIPPED_BY_REGISTER` (013, 022; CP-05) страниц 0 — это ожидаемо, не ошибка;
     - enum-ы `review_status`, `origin`, `page_status`; запрет повышенных статусов → `E_FORBIDDEN_STATUS`;
     - `citing_work_id ∈ works` или UNKNOWN;
     - концы `work_relations` существуют либо помечены как внешние; `relation_type` из закрытого списка →
       `E_UNKNOWN_RELATION_TYPE`;
     - симметричные пары не дублируются;
     - `NOT_SAME` не противоречит группировке Source → Work → `E_RELATION_CONFLICT`;
     - фильтр `object_status = CURRENT` применяется до подсчёта ожидаемых counts.
3. **Сервер.** `CALL dbms.components()` даёт версию и редакцию; обе записываются. DDL §1.7 применяется идемпотентно.
   `SHOW CONSTRAINTS` сверяется с ожидаемым.
4. **Старт прогона.** `CREATE (:ProjectionRun {status: 'REFRESHING' | 'WIPING', …})`. В receipt пишется скелет с
   входами и командой.
5. **(wipe)** Межслойный guard: если к узлам DOCUMENT есть рёбра из других слоёв и `--cascade` не задан, выход с
   `E_CROSS_LAYER_LOSS`. Иначе слой удаляется батчами.
6. **Узлы**, в порядке Work, Author, Venue, Source, Page, Block, Figure, Table, Formula, BibliographyEntry:
   `SELECT … WHERE <только CURRENT> ORDER BY id` → батчи по 5000 → шаблон MERGE (§2.5). Счётчики `summary.counters`
   идут в receipt.
7. **Рёбра**, в порядке INSTANCE_OF, AUTHORED_BY, PUBLISHED_IN, HAS_PAGE, PRECEDES, HAS_BLOCK, HAS_FIGURE, HAS_TABLE,
   HAS_FORMULA, HAS_BIBLIOGRAPHY_ENTRY, REFERENCE_OF, RESOLVES_TO, CITES, затем связи Work/Source из курируемой таблицы
   и DUPLICATE_CANDIDATE_OF. Каждый батч возвращает `written`. Если
   `written < len(rows)`, запускается диагностический запрос отсутствующих концов, затем `E_DANGLING_REFERENCE`.
   **Строки не теряются молча**: `MATCH` без совпадения иначе дал бы именно такую тихую потерю.
8. **(refresh) Sweep.**
   - Сначала отчёт о межслойных рёбрах у удаляемых узлов (`BROKEN_CROSS_LAYER_REFERENCES`; в v0 всегда пусто).
   - Затем удаление узлов `:DocumentLayer` со старым `projection_run_id`.
   - Затем удаление рёбер DOCUMENT-типов со старым `projection_run_id`. Такие рёбра нужно удалять отдельно: их концы
     живы, а ребро исчезло из канона (исправлен список авторов, отозвана связь библиографии).
9. **Проверки** §2.7 и digest §2.8.
10. **Завершение.** `ProjectionRun.status = COMPLETE` с counts, checks и digests; receipt; код выхода 0. При любой ошибке —
    `FAILED`, receipt с ошибкой (`code, stage, tool, message, retryable, source, page, log reference`, бриф §5) и
    ненулевой код выхода.

Параметры:

- батч узлов и рёбер — 5000 (настраивается 1000–20000);
- батч внутренних транзакций wipe/sweep — 10000;
- таймаут транзакции проектора — явный, 300 с (перекрывает серверный `db.transaction.timeout`);
- транзиентные ошибки ретраит сам драйвер: `execute_query` — managed transaction;
- загрузка последовательная. Параллелить рёбра на общих узлах — риск lock contention и deadlock. При масштабе в минуты
  это не нужно.

### 2.5 Cypher-шаблоны записи

Label и type подставляются **только из реестра** (идентификаторы проверяются регуляркой `^[A-Z][A-Za-z]+$` /
`^[A-Z_]+$`). Все значения передаются параметрами. Используется общее подмножество Cypher 5 и Cypher 25: без
`$(…)`-динамических меток и без конструкций только из Cypher 25. Поэтому один код работает на 5.26 и 2026.x.

```cypher
// узлы (пример Page). row.props содержит id и projection_run_id.
// SET n = map заменяет все свойства: проектор — единственный владелец узлов DOCUMENT (§3, правило R3)
UNWIND $rows AS row
MERGE (n:Page {id: row.id})
SET n = row.props, n:DocumentLayer
RETURN count(*) AS written
```

```cypher
// структурное ребро (пример HAS_BLOCK)
UNWIND $rows AS row
MATCH (a:Page {id: row.from_id})
MATCH (b:Block {id: row.to_id})
MERGE (a)-[r:HAS_BLOCK]->(b)
SET r = row.props
RETURN count(*) AS written
```

```cypher
// AUTHORED_BY: идентичность ребра включает позицию и роль (role не null: контракт D)
UNWIND $rows AS row
MATCH (w:Work {id: row.from_id})
MATCH (p:Author {id: row.to_id})
MERGE (w)-[r:AUTHORED_BY {position: row.props.position, role: row.props.role}]->(p)
SET r = row.props
RETURN count(*) AS written
```

```cypher
// RESOLVES_TO и CITES: MERGE по паре. r.link_id / r.id защищены relationship uniqueness constraint
UNWIND $rows AS row
MATCH (a:Work {id: row.from_id})
MATCH (b:Work {id: row.to_id})
MERGE (a)-[r:CITES]->(b)
SET r = row.props
RETURN count(*) AS written
```

```cypher
// диагностика при written < len(rows)
UNWIND $rows AS row
OPTIONAL MATCH (a:Page {id: row.from_id})
OPTIONAL MATCH (b:Block {id: row.to_id})
WITH row, a, b WHERE a IS NULL OR b IS NULL
RETURN row.from_id AS from_id, row.to_id AS to_id, a IS NULL AS missing_from, b IS NULL AS missing_to
```

```cypher
// межслойный guard (до wipe) и отчёт перед sweep
MATCH (d:DocumentLayer)-[r]-(x)
WHERE NOT x:DocumentLayer
RETURN type(r) AS rel_type, count(r) AS n;

MATCH (d:DocumentLayer)-[r]-(x)
WHERE d.projection_run_id <> $run_id AND NOT x:DocumentLayer
RETURN labels(d) AS doc_labels, d.id AS doc_id, type(r) AS rel_type, x.id AS other_id
LIMIT 1000;
```

```cypher
// wipe слоя (только auto-commit: session.run, не managed transaction)
MATCH (n:DocumentLayer)
CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 10000 ROWS;

// sweep устаревших узлов (refresh)
MATCH (n:DocumentLayer) WHERE n.projection_run_id <> $run_id
CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 10000 ROWS;

// sweep устаревших рёбер — по каждому DOCUMENT-типу из реестра (пример AUTHORED_BY)
MATCH ()-[r:AUTHORED_BY]->() WHERE r.projection_run_id <> $run_id
CALL (r) { DELETE r } IN TRANSACTIONS OF 10000 ROWS;
```

```cypher
// старт и финиш прогона
CREATE (r:ProjectionRun {id: $run_id, layer: 'DOCUMENT', status: $status, mode: $mode, scope: $scope,
  started_at: datetime(), graph_schema_version: $gsv, built_from_snapshot_id: $snapshot_id,
  canonical_manifest_sha256: $manifest_sha256, canonical_schema_versions: $schema_versions,
  projector_version: $projector_version});

MATCH (r:ProjectionRun {id: $run_id})
SET r.status = $status, r.finished_at = datetime(), r.counts_json = $counts_json, r.checks_json = $checks_json,
    r.expected_digest = $expected_digest, r.content_digest = $content_digest,
    r.neo4j_version = $neo4j_version, r.neo4j_edition = $neo4j_edition, r.receipt_ref = $receipt_ref;
```

Синтаксис `CALL (n) { … } IN TRANSACTIONS` (variable scope clause) есть в 5.23+ и 2025.x/2026.x, а значит и в 5.26.
Он допустим только в неявной (auto-commit) транзакции.

**Шаблоны чтения для API/MCP (агент G).** Все выполняются в READ-транзакциях (`execute_query(routing_=READ)` или
`execute_read`).

```cypher
// get_document_neighbors(page_id)
MATCH (p:Page {id: $page_id})
OPTIONAL MATCH (prev:Page)-[:PRECEDES]->(p)
OPTIONAL MATCH (p)-[:PRECEDES]->(next:Page)
OPTIONAL MATCH (s:Source)-[:HAS_PAGE]->(p)
OPTIONAL MATCH (s)-[:INSTANCE_OF]->(w:Work)
CALL (p) {
  MATCH (p)-[r:HAS_FIGURE|HAS_TABLE|HAS_FORMULA|HAS_BIBLIOGRAPHY_ENTRY]->(o)
  RETURN collect({rel: type(r), id: o.id, label: o.object_label, review_status: o.review_status}) AS objects
}
RETURN p {.*} AS page, prev.id AS prev_page_id, next.id AS next_page_id, s.id AS source_id, w.id AS work_id, objects;

// get_citations(work_id): исходящие; входящие — симметрично (x)-[c:CITES]->(w)
MATCH (w:Work {id: $work_id})-[c:CITES]->(x:Work)
RETURN x.id AS cited_work_id, x.title AS title, x.year AS year, c.support_count AS support_count,
       c.via_entry_ids AS via_entry_ids, c.match_methods AS match_methods, c.match_scores AS match_scores,
       c.review_status AS review_status
ORDER BY cited_work_id;

// записи списка литературы работы — по принадлежности (REFERENCE_OF), а не по странице-носителю
MATCH (e:BibliographyEntry)-[:REFERENCE_OF]->(w:Work {id: $work_id})
OPTIONAL MATCH (e)-[r:RESOLVES_TO]->(x:Work)
RETURN e.id AS entry_id, e.source_id AS source_id, e.page_id AS host_page_id, e.ordinal AS ordinal,
       x.id AS resolved_work_id, r.match_method AS match_method, r.match_score AS match_score,
       r.review_status AS link_review_status
ORDER BY source_id, ordinal;

// trace_document_provenance(id): цепочка вложенности из графа (полный конверт — из DuckDB)
MATCH (n:DocumentLayer {id: $id})
OPTIONAL MATCH (p:Page)-[:HAS_BLOCK|HAS_FIGURE|HAS_TABLE|HAS_FORMULA|HAS_BIBLIOGRAPHY_ENTRY]->(n)
WITH n, coalesce(p, CASE WHEN n:Page THEN n END) AS page
OPTIONAL MATCH (s:Source)-[:HAS_PAGE]->(page)
WITH n, page, coalesce(s, CASE WHEN n:Source THEN n END) AS src
OPTIONAL MATCH (src)-[:INSTANCE_OF]->(w:Work)
RETURN labels(n) AS labels, n {.*} AS node, page.id AS page_id, src.id AS source_id, w.id AS work_id;
```

### 2.6 Производные правила, исполняемые проектором

**`precedes_v1`** — чистая функция канонического порядка страниц; новой информации не вносит:

```sql
SELECT page_id AS from_id, next_page_id AS to_id
FROM (SELECT page_id, LEAD(page_id) OVER (PARTITION BY source_id ORDER BY page_number) AS next_page_id
      FROM pages)
WHERE next_page_id IS NOT NULL
ORDER BY from_id;
```

**`cites_v1`** — резервное правило, если D не материализует `citations` (рекомендация DN-E06: материализовать в каноне,
чтобы DuckDB, Neo4j и API видели одно и то же множество CITES). Имена статусов — плейсхолдеры D.

```sql
-- цитирующая сторона — canonical citing_work_id записи (A K11), а НЕ Work Source-хоста:
-- на чужой странице Work хоста ошибочен; при UNKNOWN запись в CITES не участвует (отчёт)
WITH cw AS (
  SELECT l.link_id, l.entry_id, l.work_id AS cited_work_id, l.match_method, l.match_score,
         e.citing_work_id
  FROM bibliography_links l JOIN bibliography e USING (entry_id)
  WHERE l.link_status IN ('<ACCEPTED_AUTO_HIGH_CONFIDENCE>', '<REVIEWED_CONFIRMED>')
    AND e.citing_work_id IS NOT NULL AND e.citing_work_id <> '<UNKNOWN>'
)
SELECT citing_work_id, cited_work_id,
       list(entry_id ORDER BY entry_id)     AS via_entry_ids,
       list(link_id ORDER BY entry_id)      AS link_ids,
       list(match_method ORDER BY entry_id) AS match_methods,
       list(match_score ORDER BY entry_id)  AS match_scores,
       count(*)                             AS support_count
FROM cw
WHERE citing_work_id <> cited_work_id          -- самоцитирование: в отчёт, не в граф
GROUP BY citing_work_id, cited_work_id
ORDER BY citing_work_id, cited_work_id;
```

`rule_version` правила пишется на ребро и в `ProjectionRun`. `citation_id` детерминирован, например
`VKM-CIT-<первые 16 hex sha256(citing_work_id, cited_work_id)>`. Префикс вне занятых (A K6); точную форму задаёт D.

**Симметричные связи** (`COMPANION_OF`, `NOT_SAME_AS`, `DUPLICATE_CANDIDATE_OF`) проектор нормализует в одно ребро от
меньшего `id` к большему. Если канон содержит обе ориентации одной пары, это ошибка preflight, а не повод создать два
ребра.

### 2.7 Проверки после сборки

Ожидаемые значения считаются из канона (DuckDB), фактические — из графа. Все результаты попадают в `checks_json` и
receipt. Любой FAIL переводит прогон в `FAILED`.

| # | Проверка | Как |
|---|---|---|
| C1 | counts по label равны строкам канона | `MATCH (n:Page) RETURN count(n)` — O(1) по count store, для каждого label |
| C2 | counts по type равны ожидаемым | `INSTANCE_OF` = sources с `work_id`; `HAS_PAGE` = #pages; `PRECEDES` = #pages − #sources с страницами; `HAS_BLOCK` = #blocks; …; `RESOLVES_TO` = #принятых links; `CITES` = #строк citations |
| C3 | нет сирот и лишних родителей | `MATCH (p:Page) WHERE COUNT { (:Source)-[:HAS_PAGE]->(p) } <> 1 RETURN count(p)`; то же для Block, Figure, Table, Formula, BibliographyEntry с их `HAS_*`; `MATCH (a:Author) WHERE NOT ()-[:AUTHORED_BY]->(a)`; `MATCH (v:Venue) WHERE NOT ()-[:PUBLISHED_IN]->(v)`. Source без `INSTANCE_OF` → список (WORK_UNKNOWN) |
| C4 | страницы на Source | `MATCH (s:Source) OPTIONAL MATCH (s)-[:HAS_PAGE]->(p) WITH s, count(p) AS k WHERE k <> s.page_count RETURN s.id` — пусто (нет тихой потери страниц) |
| C5 | цепочка PRECEDES | `MATCH (a:Page)-[:PRECEDES]->(b:Page) WHERE a.source_id <> b.source_id OR b.page_number <> a.page_number + 1 RETURN count(*)` = 0; ни у одной Page нет больше одного входящего или исходящего PRECEDES |
| C6 | гигиена labels | `MATCH (n) WHERE NOT n:DocumentLayer AND NOT n:ProjectionRun RETURN labels(n), count(*)` — пусто в v0; `MATCH (n:DocumentLayer) WHERE size([l IN labels(n) WHERE l IN $type_labels]) <> 1 RETURN count(n)` = 0 |
| C7 | реестр типов и концов | `MATCH ()-[r]->() RETURN type(r), count(*)` ⊆ реестр; по каждому типу `MATCH (a)-[r:HAS_BLOCK]->(b) WHERE NOT (a:Page AND b:Block) RETURN count(r)` = 0 |
| C8 | обязательные свойства (замена Enterprise existence constraints) | по каждому label: `WHERE n.id IS NULL OR n.schema_version IS NULL OR n.review_status IS NULL OR n.projection_run_id IS NULL OR <типизированные id по label> IS NULL` = 0; белый список: ключи `properties(n)` ⊆ реестр label |
| C9 | научная безопасность | `MATCH (n:DocumentLayer) WHERE n.review_status IN ['FACT','REVIEWED_MEASUREMENT','ACCEPTED_PARAMETER','ACCEPTED_FORMULA'] RETURN count(n)` = 0; `review_status ∈ enum D`; у Block, Figure, Table, Formula, BibliographyEntry в v0 только `AUTO_EXTRACTED_UNREVIEWED`, без наследования статуса Source (CP-06); Figure с `figure_type <> 'UNKNOWN_FIGURE_TYPE'` и уверенностью ниже порога D = 0 (зеркало правила D); у BibliographyEntry не больше одного `RESOLVES_TO` |
| C10 | трассировка | детерминированная выборка (seed = hash(`run_id`)) по 200 ID на label, мелкие labels целиком: узел графа ↔ строка канона по отображённым свойствам — 100 % совпадение |
| C11 | digest | `content_digest` (экспорт графа) = `expected_digest` (поток строк проектора из канона), §2.8 |
| C12 | идемпотентность (acceptance / по флагу) | повторный `refresh` того же снапшота: `nodes_created = 0`, `relationships_created = 0`, удалено 0, digest тот же |
| C13 | непротиворечивость связей Work | `MATCH (a:Work)-[:NOT_SAME_AS]->(a) RETURN count(*)` = 0: петля означает, что две «не те же» работы слиты в одну Work. Preflight проверяет то же раньше, на отображении Source → Work из курируемой таблицы (`E_RELATION_CONFLICT`). У каждого Source ровно одно `INSTANCE_OF` (C3), поэтому SAME_WORK-группа видна как «несколько Source → одна Work» |
| C14 | атрибуция библиографии (A K11) | `REFERENCE_OF` = число записей с известным `citing_work_id`; у каждой записи не больше одного `REFERENCE_OF`; записи с `CITING_WORK_UNKNOWN` перечислены в отчёте; `CITES` получен только из пар (`REFERENCE_OF`-работа, `RESOLVES_TO`-работа): `MATCH (a:Work)-[c:CITES]->(b:Work) WHERE NOT EXISTS { (e:BibliographyEntry)-[:REFERENCE_OF]->(a) WHERE (e)-[:RESOLVES_TO]->(b) } RETURN count(c)` = 0 |
| C15 | симметричные связи и дубли | для `COMPANION_OF`, `NOT_SAME_AS`, `DUPLICATE_CANDIDATE_OF` нет пары в обе стороны; у `DUPLICATE_CANDIDATE_OF` концы в разных Source; `dup_group_id` согласован с рёбрами |
| C16 | учёт источников (CP-05) | 251 Source = обработанные + `SKIPPED_BY_REGISTER` + FAILED; у пропущенных 0 страниц и `lifecycle_status` задан |

Коды ошибок проектора: `E_PROJECTION_BUSY`, `E_CANON_MANIFEST_MISMATCH`, `E_SCHEMA_VERSION_UNSUPPORTED`,
`E_DUPLICATE_ID`, `E_DANGLING_REFERENCE`, `E_PAGE_GAP`, `E_FORBIDDEN_STATUS`, `E_UNKNOWN_RELATION_TYPE`,
`E_RELATION_CONFLICT`, `E_CROSS_LAYER_LOSS`, `E_COUNT_MISMATCH`, `E_ORPHAN_NODES`, `E_INVARIANT_VIOLATION`,
`E_DIGEST_MISMATCH`, `E_SERVER_UNAVAILABLE` (retryable).

### 2.8 Детерминизм и идемпотентность

- **Идемпотентность.** Она определяется результирующим состоянием, а не счётчиками записи. MERGE по `id` и
  `SET n = props` дают то же состояние при повторе; повтор того же снапшота даёт тот же `content_digest`.
- **Детерминизм.** Строки сортируются по `id`, связи — по `(from_id, to_id, …)`. Производные правила версионированы.
  `projection_run_id` и время из digest исключены.
- **`expected_digest`.** Для каждого label в фиксированном порядке пишется строка `label \t id \t canonical_json(props
  без projection_run_id)`. Для каждого type — `type \t from_id \t to_id \t canonical_json(props)`. Строки сортируются
  на клиенте, результат — SHA-256. Canonical JSON: ключи отсортированы; float в кратчайшем repr; даты и время в ISO
  8601; списки как есть.
- **`content_digest`.** Та же сериализация по экспорту графа (`MATCH (n:Page) RETURN n.id, properties(n)`; для рёбер —
  `a.id, b.id, properties(r)`), с нормализацией типов драйвера (`neo4j.time.*` → ISO).
- Равенство двух digest доказывает, что граф — ровно проекция канона: без ручных правок, без потерь, без лишнего. Этот же
  механизм ловит ручную запись в граф между прогонами.

### 2.9 Время и память (оценки; заменить измерениями canary)

| Операция | Масштаб | Оценка | Допущения |
|---|---|---|---|
| wipe слоя | ~0,3–0,9 млн узлов + ~0,3–0,95 млн рёбер | 0,5–3 мин | `CALL … IN TRANSACTIONS OF 10000 ROWS`, SSD |
| загрузка узлов | ~0,3–0,9 млн | 0,5–2 мин | UNWIND по 5000, MERGE по уникальному индексу, ~10–30 тыс. узлов/с |
| загрузка рёбер | ~0,3–0,95 млн | 1–3 мин | два index lookup на строку, ~5–15 тыс. рёбер/с |
| preflight в DuckDB | ~1 млн строк | < 30 с | anti-join по Parquet |
| проверки C1–C10 | — | < 1 мин | count store, label scans |
| digest (канон + граф) | ~0,6–1,9 млн элементов | 1–3 мин | Bolt streaming, хэширование в Python |
| **итого wipe-rebuild** | | **~4–12 мин** | |
| резерв B (import) | | ~1–3 мин + рестарт + DDL | |

Состав узлов по оценкам координатора:

- sources — 251;
- works — ~0,3–15 тыс., включая внешние DOI-работы, если их введёт D;
- authors — ~1–20 тыс.;
- venues — ~0,5–3 тыс.;
- pages — 25–40 тыс.;
- blocks — 200–600 тыс.;
- figures + tables + formulas — ~30–150 тыс.;
- bibliography — 20–50 тыс.

Store — ~0,5–2 ГБ: record format Community (`aligned`), ~12–15 коротких свойств на узел, строки ID в dynamic store.

Память Neo4j:

- heap 2 ГБ;
- page cache 2 ГБ;
- `db.memory.transaction.total.max` 1 ГБ;
- лимит контейнера 6 ГБ (§7.3; на CORE свободно ≈23 GiB по инвентарю B).

После canary уточнить через `neo4j-admin server memory-recommendation --memory=6g --docker`. Если store > 2 ГБ, page
cache поднять до 3 ГБ: CORE стоит на DRAM-less SATA SSD, промахи кэша там дороже.

### 2.10 CLI, receipts, логи

CLI принадлежит координатору (`src/vkm_corpus/cli/`) и вызывает `vkm_corpus.graph`:

- `vkm-corpus graph ddl` — идемпотентное DDL и сверка `SHOW CONSTRAINTS`.
- `vkm-corpus graph rebuild --mode refresh|wipe [--cascade] [--source VKM-SRC-xxx …] [--batch-size N] [--plan-only]
  [--verify quick|full]`. `--plan-only` печатает снапшот, ожидаемые counts, действия и найденные дефекты preflight без
  записи. `--source` — scoped refresh, P1. `--verify full` добавляет C10 целиком и C12.
- `vkm-corpus graph verify` — проверки C1–C11 и C13–C16 без записи.
- `vkm-corpus graph status` — последний `ProjectionRun`, снапшот, версия и редакция сервера.

**Receipt:** `$VKM_DATA_ROOT/receipts/projections/neo4j/<projection_run_id>.json`. Содержимое:

- входы: манифест, его sha256, файлы и их row counts;
- команда, версия проектора (git commit), версия и редакция сервера;
- время по стадиям, counts ожидаемые и фактические, результаты C1–C16, оба digest;
- ошибки, код выхода.

Компактная public-safe выжимка (числа, хеши, версии, логические пути, без текстов и ID-списков сверх необходимого)
кладётся координатором в `docs/corpus_platform/receipts/` (CP-12). Полный receipt остаётся в `$VKM_DATA_ROOT`.
Временные файлы проектора — DuckDB `temp_directory` и CSV резерва B — пишутся в `$VKM_DATA_ROOT/tmp`, а не в `/tmp`:
на CORE `/tmp` — это tmpfs в RAM (инвентарь B).

**Логи:** JSON lines в `$VKM_DATA_ROOT/logs/projections/graph.jsonl`, с ротацией по размеру (например 50 МБ × 10). Поля
по постановке §43:

- `ts`;
- `service = vkm-graph-projector`;
- `run_id`;
- `job_id` — если прогон заведён в control plane PostgreSQL. Это опционально: потеря PostgreSQL не блокирует rebuild;
- `source_id` и `page_id` — где применимо;
- `stage`, `duration_ms`, `status`, `error_code`, `counts`.

### 2.11 Резерв B: офлайн-импорт (не реализуется в v0)

1. Экспорт строк проектора в CSV `$VKM_DATA_ROOT/tmp/neo4j-import/<run>/`.
   - Заголовки узлов: `id:ID(Page),page_id,source_id,page_number:int,…,quality_flags:string[],:LABEL`, где `:LABEL` =
     `Page;DocumentLayer`.
   - Заголовки рёбер: `:START_ID(Source),:END_ID(Page),projection_run_id,:TYPE`.
2. Остановить `neo4j`. Запустить one-off контейнер того же образа: `neo4j-admin database import full neo4j
   --overwrite-destination --id-type=string --array-delimiter=';' --nodes=… --relationships=…
   --report-file=/logs/import.report`. Безопасный вариант: импортировать в новый каталог данных и переключить bind-mount,
   а старый каталог держать до успешной проверки.
3. Запустить `neo4j`, выполнить `graph ddl` (Community не создаёт ограничения при импорте) и `graph verify`.
4. Применимость: только «вся БД из всех канонов сразу». В v0 существует один слой, поэтому это эквивалентно.

---

## 3. Расширяемость графа (постановка §27, контракт §51–53)

### 3.1 Правила (предлагаются как общеграфовый контракт, DN-E01)

- **R1. Слои.** Каждый узел несёт ровно один layer label: `:DocumentLayer`, `:EvidenceLayer`, `:PhysicsLayer`,
  `:RepresentationLayer`, `:ObservationLayer` или `:ProvenanceLayer`. Системные `:ProjectionRun` — вне слоёв.
- **R2. Типы.** Type labels и relationship types глобально уникальны. Каждый принадлежит одному слою по реестру
  `GRAPH_SCHEMA` в коде; экспорт реестра — `schemas/graph/*.json`. Смысл типа не переиспользуется: document `Formula` ≠
  `ReviewedFormula`, `Figure` ≠ `PhysicalEntity`.
- **R3. Владение.** Проектор слоя создаёт, меняет и удаляет **только** свои узлы и свои relationship types. Межслойное
  ребро принадлежит зависимому слою и идёт **от** его узла **к** узлу нижнего слоя, например
  `(:Claim)-[:SUPPORTED_BY]->(:Page)`. Чужим узлам нельзя ставить свойства и labels. Поэтому `SET n = props` в проекторе
  DOCUMENT безопасен.
- **R4. Канон на каждый слой.** Каждый будущий слой пересобирается из **своих** канонических записей (контракт §53).
  Межслойные ссылки хранятся в каноне верхнего слоя как стабильные document ID, например `claim.supported_by_page_id`,
  `bbox`, `object_id_at_review`. Существовать только в графе они не могут.
- **R5. Порядок зависимостей (DAG).** DOCUMENT ← EVIDENCE ← PHYSICS ← REPRESENTATION ← OBSERVATION; PROVENANCE может
  ссылаться на любой слой. Rebuild слоя X требует пересборки всех слоёв, у которых есть рёбра в X: флаг `--cascade`
  запускает их проекторы по порядку.
- **R6. `refresh` DOCUMENT сохраняет** узлы с неизменившимися ID, а значит и межслойные рёбра к ним. Узел, исчезнувший из
  канона, удаляется вместе с рёбрами. Каждое потерянное межслойное ребро перечисляется в receipt
  (`BROKEN_CROSS_LAYER_REFERENCES`), а зависимый слой помечается `STALE`: его проверки падают, пока не исправлен канон.
  Тихой потери нет. `wipe` без `--cascade` при наличии межслойных рёбер отклоняется (`E_CROSS_LAYER_LOSS`).
- **R7. Ручные правки в Neo4j запрещены.** Пишут только проекторы. У MCP нет raw Cypher. Созданное руками исчезнет при
  rebuild **по замыслу**, а digest-проверка (§2.8) обнаружит его ещё раньше.
- **R8. DDL слоя** именуется с префиксом слоя (`doc_`, `ev_`, `phys_`, `rep_`, `obs_`, `prov_`) и добавляется через
  `IF NOT EXISTS`. DDL и данные DOCUMENT при этом не меняются.

### 3.2 Примеры будущих рёбер

Ничего из этого **не создаётся в v0**; мегасхемы нет (постановка §27).

| Будущий слой | Новый label | Ребро к существующему узлу | Замечание |
|---|---|---|---|
| EVIDENCE | `Claim` | `(Claim)-[:SUPPORTED_BY {bbox, object_id_at_review, pipeline_version_at_review}]->(Page)` | якорь — Page + bbox (§3.3) |
| EVIDENCE | `Measurement` | `(Measurement)-[:EXTRACTED_FROM]->(Table)` | контракт §51 |
| EVIDENCE | `ReviewedFormula` | `(ReviewedFormula)-[:TRANSCRIBES]->(Formula)` | document Formula ≠ reviewed (контракт §15) |
| PHYSICS | `Law` | `(Law)-[:DEFINED_BY]->(ReviewedFormula)` | «Law DEFINED_BY Formula» из контракта §51 реализуется **через** ReviewedFormula, иначе смешаются OCR-формула и reviewed-формула |
| PHYSICS | `Parameter` | `(Parameter)-[:ESTIMATED_FROM]->(Measurement)` | внутри верхних слоёв |
| PHYSICS | `PhysicalEntity` | `(PhysicalEntity)-[:DEPICTED_IN]->(Figure)` | только через reviewed-запись оцифровки (контракт §41: digitization = DERIVATION) |
| PHYSICS / REPRESENTATION | `PhysicalWorld`, `WorldRepresentation` | `(PhysicalWorld)-[:USES]->(Law)`, `(WorldRepresentation)-[:REPRESENTS]->(PhysicalWorld)` | PhysicalWorld ≠ WorldRepresentation |
| OBSERVATION | `ObservationWorld`, `ObservationDataset` | `(ObservationDataset)-[:REPORTED_IN]->(Table)` или `->(Source)` | ObservationWorld ≠ PhysicalWorld |
| PROVENANCE | `SolverRun` | `(SolverRun)-[:RUNS]->(WorldRepresentation)` | computation provenance ≠ processing provenance (контракт §36) |

Принадлежность будущих типов к слоям окончательно решается при проектировании этих слоёв. Правило требует только одного:
каждый тип — ровно в одном слое.

### 3.3 Стабильность якорей (вопрос к D и H)

- `page_id` зависит только от `source_id` и физического индекса страницы, то есть от `sha256` зарегистрированного файла.
  От версии pipeline он **не** зависит. Это самый стабильный якорь.
- ID блоков, рисунков, таблиц и формул зависят от результата извлечения. При смене pipeline или модели нумерация объектов
  может измениться; это version-aware ID по брифу §5.
- A (п. 12) предлагает для ID объектов «`page_id` + версия экстрактора + порядковый номер» плюс `content_sha256` и
  каноническую таблицу `supersedes`; молчаливая перенумерация запрещена.
- Рекомендация для будущего EVIDENCE: ребро к `Page` (стабильно) плюс `bbox`, `object_id_at_review` и
  `content_sha256_at_review` как свойства. После переизвлечения ссылка переякоривается **по канонической `supersedes`**. Если
  `supersedes` не даёт ответа, пересечение bbox даёт только кандидата: он сохраняется с пометкой `REANCHOR_CANDIDATE` и
  идёт на ревью, автоматически не принимается. Ребро только к Block/Figure без Page-якоря ломалось бы при каждой смене
  extractor (DN-E08).
- В v0 граф содержит только текущие версии объектов (§1.1). Ребра `SUPERSEDES` в графе нет: история версий живёт в
  каноне.

---

## 4. OpenSearch: индексы, mappings, analyzers

### 4.1 Модель индексов: индекс на тип плюс group alias

| Критерий | Индекс на тип (выбрано) | Общий индекс с `object_type` |
|---|---|---|
| Смешение гранулярностей | нет: страницы и блоки не конкурируют в одном ранжировании. Блоки — части страниц, в общем индексе один и тот же текст дал бы дубли-кандидаты | в общем ранжировании дубли страница/блок; нужен постоянный фильтр |
| BM25-статистика | IDF и средняя длина поля — внутри однородной коллекции (подписи с подписями) | миллион коротких блоков смещает avgdl длинных страниц |
| Mappings | строгие, без «разреженной суммы» полей | объединение всех полей, половина пустует |
| Пересборка части | индекс одного типа и свой alias swap (например formulas после исправления OCR) | только целиком или delete_by_query внутри живого индекса |
| Кросс-типовый поиск | multi-index alias `vkm-objects` (figures + tables + formulas) | естественен |
| Стоимость | 6–7 индексов по одному шарду — пренебрежимо на одном узле | один индекс |

**Решение.** Основные индексы: `pages`, `blocks`, `figures`, `tables`, `formulas` — минимум постановки §28. К ним
`bibliography`: 20–50 тыс. документов, дёшево и полезно для поиска неразрешённых ссылок. `works` — опционально (P1),
поиск работы по названию и автору для MCP.

Общие поля имеют **одинаковые имена и mappings во всех индексах**: они генерируются из одного определения в коде
(`COMMON`, §4.4). Поле `object_type` есть в каждом документе, и в кросс-индексных выдачах тип виден без разбора
`_index`.

| Alias (чтение) | Указывает на | Документов (оценка) |
|---|---|---|
| `vkm-pages` | `vkm-pages-m1-<build_id>` | 25–40 тыс. |
| `vkm-blocks` | `vkm-blocks-m1-<build_id>` | 200–600 тыс. |
| `vkm-figures` | `vkm-figures-m1-<build_id>` | ~10–40 тыс. |
| `vkm-tables` | `vkm-tables-m1-<build_id>` | ~5–20 тыс. |
| `vkm-formulas` | `vkm-formulas-m1-<build_id>` | ~10–60 тыс. |
| `vkm-bibliography` | `vkm-bibliography-m1-<build_id>` | 20–50 тыс. |
| `vkm-works` (P1) | `vkm-works-m1-<build_id>` | ~0,3–15 тыс. |
| `vkm-objects` | figures + tables + formulas текущих builds | сумма |

Оценка общего размера индексов — ~1–3 ГБ. Предыдущий build удерживается для отката, итого до ~6 ГБ.

### 4.2 Правила построения документов

1. **Одна каноническая строка — ровно один документ**, в том числе страница со статусом FAILED и пустым текстом. Тогда
   counts индекса равны counts канона, а «не нашлось» не путается с «нет объекта». Фильтрации на этапе индексации нет.
2. `_id` = `id` = канонический ID. Bulk-операция — `create`: в свежем индексе дубликат ID падает с ошибкой, а не
   перезаписывается молча.
3. `text` — **нормализованный канонический текст** (C/D). Сырые выходы моделей не индексируются, они остаются
   артефактами. Для блоков — текст блока. Для таблиц — линеаризованная нормализованная таблица. Для рисунков — текст
   внутри рисунка, если он есть; поиск ведёт `caption`. Для формул — плоская форма, если есть; LaTeX лежит в поле `latex`.
   Для библиографии — строка записи.
4. `text_sha256` = SHA-256 ровно того канонического текста, который индексирован (UTF-8, без дополнительной
   нормализации). Считается индексатором. API пересчитывает его по тексту, взятому из канона, и сверяет (§5.4). Поэтому
   от D это поле не требуется.
5. Денормализованные поля (`title`, `authors`, `author_ids`, `year`, `available_from`, `venue_id`, `source_class`,
   `site_scope_raw`, `site_scope`, `site_scope_mapping`, `page_kind`, `printed_page_labels`) берутся join-ами канона в
   DuckDB в момент сборки. Имена скоупа — по CP-07:
   - `site_scope_raw` — дословно `evidence_scope` реестра;
   - `site_scope` — список значений `vkm_world.Scope`;
   - `site_scope_mapping` — EXACT, CASE, SYNONYM, LOSSY, MULTI, AMBIGUOUS или NOT_A_SCOPE.

   У AMBIGUOUS-источников (002, 012, 045) `site_scope` пуст. Фильтр по `site_scope` их **не** вернёт, и API обязан это
   показывать.
6. **`work_id` в документе — это Work Source-хоста** (`INSTANCE_OF`), а не атрибуция содержимого. Для библиографии
   принадлежность записи — отдельное поле `citing_work_id` (A K11); «записи работы W» фильтруются по нему. Для
   общих и чужих страниц содержимое может относиться к другой работе, и это видно по флагам и `dup_group_id`, а не по
   `work_id`.
7. **`dup_group_id`** (pages, blocks) — ID группы дублей из канона (A K12). Если страница ни в какой группе не состоит,
   это её собственный `page_id`: collapse по полю работает для всех документов, а дубли схлопываются в выдаче (§5.3).
8. Неприменимые к типу поля envelope **отсутствуют**. Null-заглушек нет.
9. Документы генерируются отсортированными по `id`. SHA-256 канонического JSON-потока записывается как
   `doc_stream_sha256` в `_meta` индекса и в receipt: одинаковый снапшот даёт одинаковый digest.
10. В индекс идут только текущие версии объектов (`object_status = CURRENT`, §1.1).

### 4.3 Настройки и анализ (общие для всех индексов; JSON проверен)

```json
{
  "index": {
    "number_of_shards": 1,
    "number_of_replicas": 0,
    "refresh_interval": "-1",
    "max_result_window": 10000
  },
  "analysis": {
    "char_filter": {
      "vkm_invisible_strip": {
        "type": "pattern_replace",
        "pattern": "[\\u00AD\\u200B-\\u200D\\uFEFF]",
        "replacement": ""
      },
      "vkm_supsub_fold": {
        "type": "mapping",
        "mappings": ["⁰ => 0", "¹ => 1", "² => 2", "³ => 3", "⁴ => 4", "⁵ => 5", "⁶ => 6", "⁷ => 7", "⁸ => 8",
                     "⁹ => 9", "⁻ => -", "⁺ => +", "₀ => 0", "₁ => 1", "₂ => 2", "₃ => 3", "₄ => 4", "₅ => 5",
                     "₆ => 6", "₇ => 7", "₈ => 8", "₉ => 9"]
      },
      "vkm_yo_fold": {
        "type": "mapping",
        "mappings": ["ё => е", "Ё => Е"]
      }
    },
    "filter": {
      "vkm_ru_stop": {"type": "stop", "stopwords": "_russian_"},
      "vkm_en_stop": {"type": "stop", "stopwords": "_english_"},
      "vkm_protected_terms": {
        "type": "keyword_marker",
        "keywords": ["сильвинит", "карналлит", "галит", "ангидрит", "доломит", "кизерит", "каинит", "бишофит",
                     "полигалит", "лангбейнит"]
      },
      "vkm_ru_stem": {"type": "stemmer", "language": "russian"},
      "vkm_en_possessive": {"type": "stemmer", "language": "possessive_english"},
      "vkm_en_stem": {"type": "stemmer", "language": "english"}
    },
    "tokenizer": {
      "vkm_latex_tokenizer": {
        "type": "pattern",
        "pattern": "(\\\\[A-Za-z]+|[A-Za-z]+|[0-9]+(?:\\.[0-9]+)?)",
        "group": 1
      }
    },
    "normalizer": {
      "vkm_keyword_norm": {"type": "custom", "char_filter": ["vkm_yo_fold"], "filter": ["lowercase"]}
    },
    "analyzer": {
      "vkm_text": {
        "type": "custom",
        "char_filter": ["vkm_invisible_strip", "vkm_supsub_fold", "vkm_yo_fold"],
        "tokenizer": "standard",
        "filter": ["vkm_en_possessive", "lowercase", "vkm_ru_stop", "vkm_en_stop", "vkm_protected_terms",
                   "vkm_ru_stem", "vkm_en_stem"]
      },
      "vkm_exact": {
        "type": "custom",
        "char_filter": ["vkm_invisible_strip", "vkm_supsub_fold", "vkm_yo_fold"],
        "tokenizer": "standard",
        "filter": ["lowercase"]
      },
      "vkm_latex": {"type": "custom", "tokenizer": "vkm_latex_tokenizer"}
    }
  }
}
```

### 4.4 Общий конверт полей (`COMMON`; одинаков во всех индексах)

```json
{
    "id": {"type": "keyword"},
    "object_type": {"type": "keyword"},
    "source_id": {"type": "keyword"},
    "work_id": {"type": "keyword"},
    "page_id": {"type": "keyword"},
    "page_number": {"type": "integer"},
    "page_kind": {"type": "keyword"},
    "printed_page_labels": {"type": "keyword"},
    "printed_page_raw": {"type": "keyword"},
    "title": {"type": "text", "analyzer": "vkm_text", "fields": {"exact": {"type": "text", "analyzer": "vkm_exact"}, "kw": {"type": "keyword", "normalizer": "vkm_keyword_norm", "ignore_above": 512}}},
    "authors": {"type": "text", "analyzer": "vkm_exact", "fields": {"stem": {"type": "text", "analyzer": "vkm_text"}, "kw": {"type": "keyword", "normalizer": "vkm_keyword_norm", "ignore_above": 256}}},
    "author_ids": {"type": "keyword"},
    "year": {"type": "short"},
    "available_from": {"type": "date", "format": "strict_date_optional_time||yyyy-MM||yyyy"},
    "venue_id": {"type": "keyword"},
    "source_class": {"type": "keyword"},
    "site_scope_raw": {"type": "keyword"},
    "site_scope": {"type": "keyword"},
    "site_scope_mapping": {"type": "keyword"},
    "language": {"type": "keyword"},
    "origin": {"type": "keyword"},
    "review_status": {"type": "keyword"},
    "quality_flags": {"type": "keyword"},
    "schema_version": {"type": "keyword"},
    "processing_run_id": {"type": "keyword"},
    "pipeline_version": {"type": "keyword"},
    "extractor_id": {"type": "keyword"},
    "model_id": {"type": "keyword"},
    "model_revision": {"type": "keyword"},
    "text": {"type": "text", "analyzer": "vkm_text", "fields": {"exact": {"type": "text", "analyzer": "vkm_exact"}}},
    "text_sha256": {"type": "keyword", "index": false, "doc_values": false},
    "text_chars": {"type": "integer"},
    "image_artifact_id": {"type": "keyword"},
    "has_image": {"type": "boolean"}
}
```

Обёртка каждого индекса:

```json
{"mappings": {"dynamic": "strict",
              "_meta": {"vkm_mapping_version": "1", "object_kind": "<type>", "build_id": "<build_id>",
                        "built_from_snapshot_id": "<snapshot_id>", "canonical_manifest_sha256": "<sha256>",
                        "projector_version": "<git commit>", "doc_stream_sha256": "<sha256>"},
              "properties": {"<COMMON + поля типа>": {}}}}
```

`dynamic: strict` отклоняет документ с неизвестным полем, поэтому дрейф контракта ловится при индексации.

### 4.5 Поля по типам (добавляются к `COMMON`)

```json
{
  "pages": {
    "text": {"type": "text", "analyzer": "vkm_text", "index_options": "offsets", "fields": {"exact": {"type": "text", "analyzer": "vkm_exact"}}},
    "page_status": {"type": "keyword"},
    "native_text_status": {"type": "keyword"},
    "ocr_status": {"type": "keyword"},
    "dup_group_id": {"type": "keyword"},
    "n_blocks": {"type": "short"},
    "n_figures": {"type": "short"},
    "n_tables": {"type": "short"},
    "n_formulas": {"type": "short"}
  },
  "blocks": {
    "block_type": {"type": "keyword"},
    "reading_order": {"type": "integer"},
    "dup_group_id": {"type": "keyword"},
    "bbox": {"type": "object", "enabled": false}
  },
  "figures": {
    "caption": {"type": "text", "analyzer": "vkm_text", "fields": {"exact": {"type": "text", "analyzer": "vkm_exact"}}},
    "object_label": {"type": "keyword", "normalizer": "vkm_keyword_norm"},
    "object_label_raw": {"type": "keyword"},
    "figure_type": {"type": "keyword"},
    "figure_type_confidence": {"type": "half_float"},
    "layout_class": {"type": "keyword"},
    "is_vector": {"type": "boolean"},
    "geometry_status": {"type": "keyword"},
    "vector_artifact_id": {"type": "keyword"},
    "bbox": {"type": "object", "enabled": false}
  },
  "tables": {
    "caption": {"type": "text", "analyzer": "vkm_text", "fields": {"exact": {"type": "text", "analyzer": "vkm_exact"}}},
    "object_label": {"type": "keyword", "normalizer": "vkm_keyword_norm"},
    "object_label_raw": {"type": "keyword"},
    "n_rows": {"type": "short"},
    "n_cols": {"type": "short"},
    "normalized_artifact_id": {"type": "keyword"},
    "raw_artifact_id": {"type": "keyword"},
    "bbox": {"type": "object", "enabled": false}
  },
  "formulas": {
    "latex": {"type": "text", "analyzer": "vkm_latex", "fields": {"kw": {"type": "keyword", "ignore_above": 2048}}},
    "equation_number": {"type": "keyword", "normalizer": "vkm_keyword_norm"},
    "formula_kind": {"type": "keyword"},
    "raw_artifact_id": {"type": "keyword"},
    "bbox": {"type": "object", "enabled": false}
  },
  "bibliography": {
    "ordinal": {"type": "integer"},
    "bib_title": {"type": "text", "analyzer": "vkm_text", "fields": {"exact": {"type": "text", "analyzer": "vkm_exact"}}},
    "bib_authors": {"type": "text", "analyzer": "vkm_exact", "fields": {"stem": {"type": "text", "analyzer": "vkm_text"}}},
    "bib_year": {"type": "short"},
    "doi": {"type": "keyword", "normalizer": "vkm_keyword_norm"},
    "isbn": {"type": "keyword"},
    "citing_work_id": {"type": "keyword"},
    "resolved_work_id": {"type": "keyword"},
    "link_status": {"type": "keyword"},
    "match_method": {"type": "keyword"},
    "match_score": {"type": "float"}
  },
  "works": {
    "work_type": {"type": "keyword"},
    "in_corpus": {"type": "boolean"},
    "identity_basis": {"type": "keyword"},
    "metadata_basis": {"type": "keyword"},
    "source_ids": {"type": "keyword"},
    "external_ids": {"type": "keyword"},
    "doi": {"type": "keyword", "normalizer": "vkm_keyword_norm"},
    "isbn": {"type": "keyword"},
    "venue_name": {"type": "text", "analyzer": "vkm_text", "fields": {"exact": {"type": "text", "analyzer": "vkm_exact"}}}
  }
}
```

Пояснения к полям:

- **`bbox`** хранится только в `_source` (`enabled: false`): это координаты page space для отображения и визуальной
  навигации, фильтровать по ним не нужно. Внутри — `x0, y0, x1, y1, space`.
- **LaTeX** — поле `text` с case-sensitive `vkm_latex` (`\Delta` ≠ `\delta`, `E` ≠ `e`) плюс `latex.kw` для точного
  совпадения и поиска дублей. Проба токенайзера на
  `\dot{\varepsilon} = A \sigma^{n} \exp(-Q/RT), \; E_{1.5}` дала
  `['\dot', '\varepsilon', 'A', '\sigma', 'n', '\exp', 'Q', 'RT', 'E', '1.5']`.
- **Табличный текст** — `text` таблицы: строки через перевод строки, ячейки через ` | `. Числа токенизируются
  стандартно; «3.1» остаётся одним токеном.
- **`index_options: offsets`** только у `pages.text`: у длинных страниц unified highlighter подсвечивает по postings без
  повторного анализа.
- **`site_scope_raw`** — дословно `evidence_scope` реестра, keyword без нормализатора: регистр и синонимы не склеиваются
  молча. **`site_scope`** и **`site_scope_mapping`** берутся **только из канонической таблицы соответствия** (CP-07,
  версионируемая константа `vkm_corpus.contracts`). Индексатор сам ничего не нормализует. Для API G: публичный параметр
  `evidence_scope` отображается на `site_scope_raw`.
- **Нет полей** с именами, запрещёнными CP-04 и стражем утечки. Все сгенерированные тела проверены функцией
  `_json_keys` стража; экспорт mappings дополнительно проходит тест гигиены платформы
  `tests/corpus/test_public_hygiene.py` (CP-04).

### 4.6 Размер батча, refresh, клиент (кратко; подробнее §6.6)

- Build: `refresh_interval: -1`, реплик 0, bulk-чанки ~1000 документов или ≤ 10 МБ.
- После загрузки: `_refresh`, возврат `refresh_interval` к значению по умолчанию, `forcemerge max_num_segments=1`.
  Индекс после сборки неизменяем.

### 4.7 Анализаторы: обоснование и результаты пробы

**Один анализатор на смешанный текст.** Вместо `text.ru` и `text.en` используется `vkm_text`: стоп-слова обоих языков и
последовательно ru-Snowball → en-Porter. Проба алгоритмов (библиотека `snowballstemmer` реализует те же алгоритмы, что
`stemmer` Lucene: `russian` = Snowball, `english` = Porter):

- Русский стеммер **не трогает** латинские токены.
- Porter **не трогает** кириллицу, в том числе уже обрезанные русские основы.

Поэтому цепочка корректна на смешанном тексте, а одно поле не даёт двойного счёта одного совпадения в двух подполях.
Подполе `.exact` (без стемминга и стоп-слов) служит для фраз и точных терминов. Альтернатива из брифа (`text.ru`,
`text.en`, `text.exact`) допустима, но дороже в индексе и требует настройки `tie_breaker` между подполями. Оставлена как
запасной вариант при смене mapping version.

**Результаты пробы на общих терминах:**

| Слова | Токены | Вывод |
|---|---|---|
| оседание / оседания / оседаний / оседанию / оседаниями | `оседан` ×5 | морфология работает |
| ползучесть / ползучести / ползучестью | `ползучест` ×3 | ok |
| маркшейдерский / -их / -ие / -ая | `маркшейдерск` ×4 | ok |
| наблюдение / наблюдения / наблюдений | `наблюден` | ok |
| сдвижение / сдвижения / сдвижений | `сдвижен` | ok |
| расчётная / расчетная / расчётной | `расчетн` | ё→е работает |
| закладка / закладки / закладку vs закладочный | `закладк` vs `закладочн` | словообразование стеммер не склеивает; ожидаемо |
| выработка / выработки vs выработок | `выработк` vs `выработок` | беглая гласная не склеивается; известное ограничение |
| **сильвинит vs сильвинита** | **`сильвин` vs `сильвинит`** | **дефект**: «-ит» режется как глагольное окончание. Хуже того, «сильвинит» (порода) совпадает с «сильвин» (минерал) |
| карналлит, галит, ангидрит, доломит, кизерит, каинит, бишофит, полигалит, лангбейнит | именительный ≠ косвенные | тот же дефект у всех 10 |
| то же с `keyword_marker` (`vkm_protected_terms`) | все формы → одна основа; «сильвин» ≠ «сильвинит» | **исправлено** |
| subsidence / subsidences; creep / creeping; backfill / backfilling | `subsid`, `creep`, `backfil` | английский ok |
| СКРУ-1, рис. 3.1, KCl | `скру`,`1`; `рис`,`3.1`; `kcl` | «3.1» — один токен; фраза «скру 1» находится через `.exact` |

Вывод. Список защищённых терминов — **MODEL_CHOICE retrieval**, а не научные данные. Он версионируется вместе с mapping
version (DN-E14); изменение означает новый build. Кандидаты на расширение после canary: названия других пород и
минералов на «-ит», аббревиатуры рудников.

**Не используется в v0** (нет измеренной пользы; только официальный образ без плагинов):

- `analysis-icu` (NFKC, транслитерация): ё и надстрочные цифры закрыты mapping-фильтрами. Транслитерацию авторов
  (Барях / Baryakh) лучше хранить в каноне как `name_variants` (D), чем получать анализатором.
- morfologik — это польский словарь и к русскому неприменим.
- hunspell-словари для русского: лемматизация лучше Snowball, но требует словарей в конфиге и медленнее. Кандидат на
  измеримое улучшение позже.
- Синонимы (ВКМ ↔ Верхнекамское месторождение) — терминологическое решение. Позже файлом под версией.

**Номера объектов.** «Рис. 3.1», «рисунок 3.1», «Fig. 3.1», «табл. 2», «(3.12)» нормализуются при извлечении (C/D) в
`object_label` (`3.1`) и `object_label_raw`. Query builder распознаёт такие шаблоны в запросе и добавляет `term`-буст по
`object_label` и фильтр `object_type` (§5.3).

---

## 5. Запросы и путь OpenSearch → rerank

### 5.1 Шаблоны запросов (JSON проверен)

Страницы (BM25 + бусты + фильтры + подсветка + детерминированный tie-break):

```json
{
  "size": 50,
  "track_total_hits": true,
  "_source": {"includes": ["id", "object_type", "source_id", "work_id", "page_id", "page_number",
                           "printed_page_labels", "title", "year", "language", "origin", "review_status",
                           "quality_flags", "site_scope_raw", "site_scope", "site_scope_mapping", "dup_group_id",
                           "image_artifact_id", "has_image", "text_sha256", "schema_version", "processing_run_id"]},
  "query": {"bool": {
    "must": [{"multi_match": {"query": "оседание земной поверхности", "type": "best_fields",
                              "fields": ["text^1.0", "text.exact^0.5", "title^0.3"], "tie_breaker": 0.2,
                              "minimum_should_match": "3<70%"}}],
    "should": [{"match_phrase": {"text.exact": {"query": "оседание земной поверхности", "slop": 3, "boost": 2.0}}}],
    "filter": [
      {"terms": {"site_scope": ["<значение из нормализованного словаря D>"]}},
      {"range": {"year": {"gte": 1990}}},
      {"range": {"available_from": {"lte": "2020-12-31"}}},
      {"terms": {"origin": ["NATIVE", "OCR"]}}],
    "must_not": [{"terms": {"quality_flags": ["<флаг исключения, напр. низкое качество OCR>"]}}]}},
  "highlight": {"type": "unified", "fields": {"text": {"fragment_size": 180, "number_of_fragments": 3}}},
  "sort": [{"_score": "desc"}, {"id": "asc"}]
}
```

Пассажи: блоки со сворачиванием по странице. Это путь по умолчанию для кандидатов-страниц перед текстовым реранком
(§5.4).

```json
{
  "size": 50,
  "_source": {"includes": ["id", "object_type", "source_id", "work_id", "page_id", "page_number", "reading_order",
                           "review_status", "quality_flags", "origin", "text_sha256"]},
  "query": {"bool": {
    "must": [{"multi_match": {"query": "маркшейдерские наблюдения за сдвижением", "type": "best_fields",
                              "fields": ["text^1.0", "text.exact^0.5"], "tie_breaker": 0.2,
                              "minimum_should_match": "3<70%"}}],
    "should": [{"match_phrase": {"text.exact": {"query": "маркшейдерские наблюдения за сдвижением", "slop": 3,
                                                "boost": 2.0}}}],
    "filter": [{"terms": {"source_id": ["VKM-SRC-NNN"]}}]}},
  "collapse": {"field": "page_id",
               "inner_hits": {"name": "best_blocks", "size": 3,
                              "_source": {"includes": ["id", "reading_order", "text_sha256"]},
                              "sort": [{"_score": "desc"}, {"id": "asc"}]}},
  "highlight": {"fields": {"text": {"fragment_size": 180, "number_of_fragments": 2}}},
  "sort": [{"_score": "desc"}, {"id": "asc"}]
}
```

Объекты: alias `vkm-objects` с бустом номера объекта и визуальным фильтром.

```json
{
  "size": 30,
  "_source": {"includes": ["id", "object_type", "source_id", "page_id", "page_number", "object_label",
                           "figure_type", "image_artifact_id", "has_image", "review_status", "quality_flags"]},
  "query": {"bool": {
    "must": [{"multi_match": {"query": "мульда сдвижения", "type": "best_fields",
                              "fields": ["caption^3.0", "caption.exact^1.5", "text^1.0", "text.exact^0.5",
                                         "latex^1.0", "title^0.3"], "tie_breaker": 0.2}}],
    "should": [{"term": {"object_label": {"value": "3.1", "boost": 5.0}}}],
    "filter": [{"terms": {"object_type": ["figure"]}}, {"term": {"has_image": true}}]}},
  "highlight": {"fields": {"caption": {"number_of_fragments": 0},
                           "text": {"fragment_size": 150, "number_of_fragments": 1}}},
  "sort": [{"_score": "desc"}, {"id": "asc"}]
}
```

Поля, которых нет в индексе, multi_match по alias просто игнорирует: `latex` существует только в formulas.

### 5.2 Бусты полей (начальные значения; MODEL_CHOICE, калибруются только на размеченных синтетических или отдельно утверждённых запросах, не на тестовой истине)

| Индекс | Поля и веса | Почему |
|---|---|---|
| pages | `text^1.0`, `text.exact^0.5`, `title^0.3`; фраза `text.exact` ×2 | название работы — слабый приоритет. Иначе все страницы книги «про оседание» обгонят страницу, где тема действительно раскрыта |
| blocks | `text^1.0`, `text.exact^0.5`; фраза ×2 | пассажи |
| figures, tables | `caption^3.0`, `caption.exact^1.5`, `text^1.0`, `title^0.3`; `object_label` term ×5 | подпись — главный носитель смысла объекта |
| formulas | `latex^1.0`, `text^1.0`, `equation_number` term ×5 | |
| bibliography | `bib_title^2.0`, `bib_authors.stem^1.5`, `text^1.0`, `doi` term ×10 | |
| works (P1) | `title^3.0`, `authors.stem^2.0`, `venue_name^1.0` | |

`minimum_should_match: "3<70%"`: до трёх термов обязательны все, дальше — 70 %. Для OCR-шумных страниц значение
калибруется после canary.

### 5.3 Фильтры и разбор запроса (query builder в `vkm_corpus.search.query`)

- **Белый список фильтров.** Неизвестное имя → ошибка 400, а не проброс в DSL.

  | Фильтр | Как задаётся |
  |---|---|
  | `source_id`, `work_id`, `page_id` | terms |
  | `year` | range |
  | `available_from_lte` | обязательно для прогноза «на момент t0», CLAUDE.md и контракт §42 |
  | `object_type` | выбирает alias или фильтр |
  | `review_status`, `site_scope`, `site_scope_raw` (публичное имя G — `evidence_scope`), `site_scope_mapping`, `language`, `origin`, `page_status`, `page_kind`, `figure_type`, `block_type` | terms |
  | `citing_work_id` | terms; только bibliography: «записи списка литературы работы W» |
  | `has_image` | term |
  | `quality_flags_exclude` | must_not |

- **Дубли страниц** (A K12). Выдача pages сворачивается по `dup_group_id` (`collapse`), и общая страница двух выпусков
  приходит одним кандидатом со списком `duplicates`. Для blocks сначала идёт collapse по `page_id`, затем в API —
  дедупликация по `dup_group_id` страницы. По умолчанию дубли свёрнуты; `include_duplicates=true` отключает свёртку.
  Так дубль не выглядит как независимое подтверждение.

- **Шаблоны номеров объектов.** Regex
  `(рис(унок)?|fig(ure)?|табл(ица)?|table)\.?\s*(\d+(\.\d+)*)` и `\((\d+(\.\d+)*)\)` для номеров уравнений. Даёт
  `term object_label` ×5 и сужает `object_type`. Исходный запрос остаётся в `multi_match`.
- Размер выдачи ограничен (`size ≤ 200`), глубокая пагинация идёт через `search_after` с тем же sort.
- **Детерминизм.** Сортировка `[_score desc, id asc]`. Индекс одношардовый и после forcemerge в один сегмент. Одинаковый
  снапшот и запрос дают одинаковый порядок ID; это проверяется в §6.5.

### 5.4 Путь к реранку (постановка §32)

**Текстовый реранк (Jina v3.5, EDGE).**

1. `search_text` (API/MCP, G) выполняет запрос к `vkm-blocks` с `collapse` по `page_id`. Результат — кандидаты-страницы:
   `page_id`, bm25 `score`, `best_blocks` (ID и подсветка) и страница из `_source`.
   Альтернативный путь `granularity=page_bm25` — `vkm-pages`, для диагностики и страниц без разбиения на блоки.
2. `rerank_text`: API по ID берёт **канонический текст из DuckDB**. Это view D над Parquet, например
   `v_rerank_text(id, object_type, text, text_sha256)`.
   - Для страницы-кандидата rerank-текст — конкатенация канонических текстов её `best_blocks` в порядке
     `(score desc, reading_order)`, не длиннее окна реранкера. Окно задают F и G.
   - Для figure/table — `object_label_raw` + caption + текст внутри рисунка или линеаризованная таблица.
   - Для formula — LaTeX плюс текст предыдущего блока (P1).
   - Правило состава версионируется: `rerank_text_v1`.
3. **Сверка.** `text_sha256` из хита сравнивается с каноническим. При несовпадении кандидат получает флаг
   `INDEX_STALE`, в лог уходит предупреждение, и в реранк всё равно идёт канонический текст. Индекс никогда не
   становится источником текста.
4. Jina v3.5 получает `{query, candidates: [{id, text}], top_n}` и возвращает `model_id`, `model_revision`, `score` и
   `rank` по ID, а также `latency` (постановка §29). API объединяет результат с bm25-данными.

**Визуальный реранк (Jina m0, EDGE).**

1. Discovery: `vkm-objects` (`has_image=true`, figures и tables) и/или `vkm-pages` (`image_artifact_id` = рендер
   страницы).
2. `image_artifact_id` разрешается API через канонический датасет `artifacts`: путь относительно `$VKM_DATA_ROOT` и
   sha256. Изображение уходит в gateway EDGE inline в base64 от VKM API (CP-18); лимиты кандидатов и размера задают F и
   G.
3. Ответ: ранжированные визуальные кандидаты с теми же каноническими ID.

**Контракт хита `SearchHit`** (возвращает `vkm_corpus.search`, API обогащает):

- `id`, `object_type`, `source_id`, `work_id`, `page_id`, `page_number`;
- `score_bm25`, `rank`;
- `highlights[]` с пометкой `origin: SEARCH_INDEX`;
- `best_blocks[]`;
- `index`, `build_id` (из `_index`);
- `text_sha256`, `image_artifact_id`, `review_status`, `quality_flags`, `origin` (NATIVE/OCR).

`_source`-копии текстов API **не выдаёт** как текст объекта. Канонический текст всегда приходит отдельным полем с
пометкой `CANONICAL`. Так выполняется требование различать canonical и raw (постановка §35).

Это совпадает с дизайном G: OpenSearch даёт только кандидатов, гидратация идёт из канона, фолбэка «текст из OpenSearch»
нет. ID из проекции, которого нет в каноне, исключается с warning `STALE_PROJECTION`. Для проверки
`PROJECTION_BUILD_MISMATCH` у G модули E отдают `status()`: `{build_id, built_from_snapshot_id,
canonical_manifest_sha256, counts, mapping_version | graph_schema_version, server_version}`. Для Neo4j это последний
`ProjectionRun`, для OpenSearch — `_meta` индексов за alias.

---

## 6. Rebuild через alias

### 6.1 Имена

- Индекс: `vkm-<type>-m<mapping_version>-<build_id>`.
- `build_id` = `<UTC yyyymmddThhmmssZ>-<первые 8 hex canonical_manifest_sha256>` в нижнем регистре, например
  `vkm-pages-m1-20260928t101500z-3fa9c2d1`.
- Aliases: `vkm-<type>` (чтение) и group alias `vkm-objects`. Шаблоны индексов (index templates) **не используются**:
  тела индексов генерируются кодом целиком и явно. Это исключает скрытое состояние кластера, а sha256 тела пишется в
  receipt.

### 6.2 Алгоритм (`vkm-corpus search build`)

1. **Lock** `$VKM_DATA_ROOT/locks/opensearch-build.lock`.
2. **Preflight.**
   - Манифест снапшота, `GET /`: версия и build hash.
   - Кластер `green`/`yellow`.
   - Свободный диск больше 2× ожидаемого размера. Иначе `E_DISK` до записи, иначе сработает watermark.
3. **План.** Типы, имена индексов, ожидаемые counts из канона, sha256 тел. `--plan-only` на этом шаге завершает работу.
4. **Создание индексов.** Settings §4.3 и mappings §4.4–4.5, `refresh_interval: -1`, реплик 0.
5. **Поток документов.** DuckDB `SELECT … ORDER BY id` → документ → action
   `{"_op_type": "create", "_index": …, "_id": id}` → `helpers.streaming_bulk(chunk_size=1000,
   max_chunk_bytes=10 МБ, raise_on_error=False, raise_on_exception=False, max_retries=5, initial_backoff=2)`.
   Параллельно считается `doc_stream_sha256`. **Любая неуспешная операция** (кроме ретраев 429) → `FAILED` с первыми N
   ошибками в receipt; тихих потерь нет.
6. **Финализация.** `_refresh`, `refresh_interval` → значение по умолчанию, `_forcemerge?max_num_segments=1`.
   `_meta` обновляется через put mapping: `doc_stream_sha256`, время.
7. **Проверки** §6.3 — на новых индексах напрямую, **до** переключения alias.
8. **Атомарное переключение.** Один запрос `POST /_aliases` со всеми `remove` и `add` для собранных типов и
   `vkm-objects`:

   ```json
   {"actions": [
     {"remove": {"index": "vkm-pages-m1-20260901t080000z-1a2b3c4d", "alias": "vkm-pages"}},
     {"add": {"index": "vkm-pages-m1-20260928t101500z-3fa9c2d1", "alias": "vkm-pages"}},
     {"remove": {"index": "vkm-figures-m1-20260901t080000z-1a2b3c4d", "alias": "vkm-objects"}},
     {"add": {"index": "vkm-figures-m1-20260928t101500z-3fa9c2d1", "alias": "vkm-objects"}}]}
   ```

9. **Receipt** `$VKM_DATA_ROOT/receipts/projections/opensearch/<build_id>.json`: входы, sha256 тел, counts, digests,
   проверки, время, версия сервера, код выхода. Логи — JSON lines, `service = vkm-search-indexer`, поля как в §2.10.
   Компактная выжимка идёт в `docs/corpus_platform/receipts/` (CP-12).
10. **Retention.** Текущий и предыдущий build хранятся. Более старые неалиасные `vkm-*` индексы удаляются **только** с
    флагом `--prune` по утверждённой политике (DN-E13). Кластерная настройка `action.destructive_requires_name=true`
    запрещает wildcard-удаление.

Частичная пересборка: `--types formulas` переключает alias только этих типов. Receipt фиксирует смешанный набор builds,
`/status` показывает build по каждому alias. Scoped in-place обновление одного Source (delete_by_query + bulk в живой
индекс) в v0 **не делается**: полная пересборка типа занимает минуты и сохраняет неизменяемость builds (P1, DN-E13).

### 6.3 Проверки build

| # | Проверка |
|---|---|
| S1 | `_count` каждого нового индекса равен строкам канона |
| S2 | (`--verify full`) множество ID индекса равно множеству ID канона: скан `search_after` по `id`, разности пусты |
| S3 | выборка 200 ID на тип (seed = hash(`build_id`)) через `mget`: поля конверта и `text_sha256` совпадают с каноном |
| S4 | `_analyze` (корпус-независимые): группы словоформ дают один токен; ё=е; защищённые минералы — один токен на все формы, и «сильвин» ≠ «сильвинит»; невидимые символы и надстрочные цифры сворачиваются; LaTeX-токены |
| S5 | smoke-запросы §6.5 на новых индексах: непустые выдачи, фильтры соблюдены, детерминированный порядок |
| S6 | strict mapping: пробная запись документа с лишним полем в **тестовый** индекс отклоняется (в live-тестах §8, не на production-build) |

### 6.4 Откат и повторяемость

- `vkm-corpus search rollback --types pages` переключает alias на предыдущий build (из receipts и `_meta`).
- `vkm-corpus search status` показывает alias → индекс → `_meta` (снапшот, digest), counts и совпадение снапшота с
  Neo4j и DuckDB.
- Повторный build того же снапшота даёт тот же `doc_stream_sha256` и те же top-20 ID smoke-запросов (acceptance
  «rebuild works»).

### 6.5 Russian-search smoke для acceptance §59

Ожидаются **свойства**, а не конкретные документы: корпус меняется, конкретные ответы в тесты не зашиваются. Запросы —
общие термины, не цитаты источников.

| # | Запрос | Куда | Фильтры | Ожидаемые свойства |
|---|---|---|---|---|
| Q1 | «оседание земной поверхности» | `vkm-pages` | нет | total ≥ 10; у каждого из top-20 `_id == id`, ID проходит грамматику page_id D и находится в DuckDB (канонический текст получен, `text_sha256` совпал); у каждого хита есть подсветка; в подсветках есть словоформы, **отличные** от формы запроса, но с тем же токеном (`_analyze` → `оседан`), — морфология работает на реальных данных; повторный запрос даёт тот же порядок ID |
| Q2 | «ползучесть каменной соли» и «ползучести каменной соли» | `vkm-blocks` (collapse `page_id`) | нет | top-20 обоих запросов совпадают: одинаковые токены дают одинаковый BM25; не больше одного хита на `page_id`; `inner_hits` возвращают ID блоков |
| Q3 | «закладка выработанного пространства» | `vkm-pages` | прогоны: `year ≥ 2000`; `origin = OCR`; фильтр по несуществующему значению | все хиты удовлетворяют фильтрам; total(filtered) ≤ total(unfiltered); несуществующее значение даёт 0 хитов — фильтр действительно применяется |
| Q4 | «маркшейдерские наблюдения за сдвижением» | `vkm-blocks` (collapse) | `source_id` = один источник; отдельно `site_scope` (например, SKRU1) и `site_scope_raw`; `available_from_lte` = дата t0 | все хиты из заданного источника или скоупа; ни у одного хита `available_from` > t0. У источников с `site_scope_mapping = AMBIGUOUS` при фильтре по `site_scope` нет хитов — ожидаемо и видно в ответе |
| Q5 | «рис. 3.1 мульда сдвижения» | `vkm-objects` | `object_type = figure`, `has_image = true` | все хиты — рисунки с `image_artifact_id`, который резолвится в канонический `artifacts` (файл есть, sha256 совпал): кандидаты пригодны для визуального реранка; при прочих равных хиты с `object_label = 3.1` выше |

Дополнительные корпус-независимые проверки (S4) плюс:

- Q6: «расчётная схема» = «расчетная схема», идентичные выдачи;
- Q7: «сильвинит» = «сильвинита», а «сильвин» даёт иную выдачу;
- Q8: «salt creep» с фильтром `language = en` — непусто, «creeping» подсвечивается.

### 6.6 Клиент, bulk, время

- **Клиент.** `opensearch-py` 3.2.0: `OpenSearch(hosts=[VKM_OPENSEARCH_URL], http_compress=True, timeout=60,
  max_retries=3, retry_on_timeout=True)`. API 3.x keyword-only: `indices.create(index=…, body=…)`,
  `indices.update_aliases(body=…)`.
- **Bulk.** `streaming_bulk` в один поток, детерминированно. Если canary покажет узкое место, допускается
  `parallel_bulk(thread_count=2)`: порядок записи на результат не влияет, digest считается по потоку генерации.
- **Refresh.** Во время build — `-1`; после — refresh, default и forcemerge. В API запросах `refresh` не используется.

**Время** (оценки; заменяются измерениями canary):

| Тип | Документов | JSON | Оценка |
|---|---|---|---|
| pages | 25–40 тыс. | ~150–250 МБ | 1–2 мин |
| blocks | 200–600 тыс. | ~150–500 МБ | 1–4 мин |
| figures + tables + formulas + bibliography | < 200 тыс. | < 150 МБ | < 1 мин |
| forcemerge + S1, S3–S5 | | | 1–3 мин |
| S2 (full) | ~0,3–0,8 млн | | 1–2 мин |
| **итого** | | | **~5–12 мин** |

---

## 7. Развёртывание на CORE

### 7.1 Версии и образы

| Компонент | Версия | Образ (digest фиксировать при развёртывании) |
|---|---|---|
| Neo4j Community | **5.26 LTS**: последний патч на 28.09.2026 — 5.26.31, поддержка до 06.06.2028. Альтернатива — 2026.09.x CalVer (DN-E03) | `neo4j:5.26.31-community@sha256:<pin>`; альтернатива `neo4j:2026.09.<patch>-community@sha256:<pin>` |
| OpenSearch | **3.8.0** (05.08.2026, текущая линия 3.x) | `opensearchproject/opensearch:3.8.0@sha256:<pin>` |
| Python | 3.13 (проект) | `neo4j==6.3.1` (→ `pytz`) и `opensearch-py==3.2.0` (→ `requests`, `urllib3`, `certifi`, `python-dateutil`, `Events`, `opensearch-protobufs` → `grpcio`, `protobuf`) — в extra `corpus-services`. `duckdb==1.5.5`, `pyarrow==25.0.1` — в extra `corpus`. Lock собирается с `-c requirements/worldspec.lock.txt` (CP-02); в пробе замыкание не конфликтует с pin `typing-extensions 4.16.0` |

Почему LTS для Neo4j:

- Проекция пересобираема, поэтому миграции store-формата не нужны: обновление — это новый образ плюс rebuild.
- Все нужные возможности есть в 5.26: `CALL (…) IN TRANSACTIONS`, relationship uniqueness, `COUNT {}`.
- CalVer поддерживает только последний минор. Исправления там приходят с новыми поведенческими изменениями (например в
  2026.08 менялась семантика Cypher), а исправления выходят только в следующих релизах. LTS выпускает патчи до 2028 года
  без этого дрейфа, а это покрывает горизонт диплома.
- Шаблоны §2.5 используют общее подмножество Cypher. Live-тесты можно параметризовать образом и прогонять на обоих.

Плагины Neo4j (APOC, GDS) **не ставятся**: проектор — чистый Cypher. Плагины OpenSearch сверх штатных не ставятся.

### 7.2 Безопасность

| Вариант OpenSearch | Плюсы | Минусы |
|---|---|---|
| **A (выбрано): security plugin выключен** (`DISABLE_SECURITY_PLUGIN=true`, `DISABLE_INSTALL_DEMO_CONFIG=true`), порт опубликован **только на loopback CORE**. Клиенты — проекторы и VKM API/MCP на том же хосте; админ ходит через SSH-туннель | просто; в клиентах нет сертификатов и паролей; нет demo-конфигурации; меньше точек отказа | любой локальный процесс CORE может изменить индекс. Это смягчено: индекс пересобираем, канон не затронут, `destructive_requires_name`, доступ к CORE только по SSH. Открывать в LAN в этом режиме нельзя |
| B: plugin включён с demo-конфигурацией и `OPENSEARCH_INITIAL_ADMIN_PASSWORD` | есть аутентификация | demo-сертификаты «не для production»; self-signed TLS ведёт к `verify_certs=False`; admin-пароль в env; сложнее healthcheck |
| C: plugin с собственным CA, internal users и ролями (reader, indexer) | правильная изоляция, можно открыть в LAN | генерация и ротация сертификатов, `securityadmin` — избыточно для v0 |

**Рекомендация: A.** Переход на C нужен, если какому-либо клиенту **вне** CORE понадобится прямой доступ (DN-E12).

**Neo4j.**

- Аутентификация включена всегда. Пароль передаётся через Docker secret: `NEO4J_AUTH_FILE=/run/secrets/neo4j_auth`,
  файл `neo4j/<пароль>` host-local, права 600, вне git (постановка §42).
- Bolt и HTTP слушают только loopback. Neo4j Browser доступен через SSH-туннель.
- RBAC в Community нет, поэтому read-only обеспечивается клиентами:
  - VKM API/MCP открывают только READ-транзакции (`routing_=READ`, `execute_read`). Попытка записи в read-транзакции
    отклоняется сервером (`Neo.ClientError.Statement.AccessMode`); это закрепить live-тестом §8.4;
  - MCP не принимает Cypher;
  - пишущие MCP-инструменты (`reprocess_*`) меняют канон через pipeline, а граф обновляет проектор.
- `dbms.usage_report.enabled=false`: без отправки телеметрии.

**Общее.**

- Docker публикует порты в обход host-firewall через цепочки DOCKER. Поэтому нужна **явная** привязка
  `127.0.0.1:<port>:<port>`, а не только правило firewall. На CORE это особенно важно (инвентарь B): ufw неактивен,
  INPUT policy accept, а активный Tailscale держит tailnet с устройствами других пользователей. Публикация на `0.0.0.0`
  открыла бы Neo4j и OpenSearch и туда.
- B допускает для Neo4j привязку ещё и к LAN-адресу CORE. E рекомендует **только loopback** и SSH-туннель для Neo4j
  Browser с WORKSTATION. Если пользователю нужен прямой доступ Neo4j Desktop с WORKSTATION, допустимо добавить LAN-адрес
  **только для Neo4j**: у него включена аутентификация. OpenSearch без security plugin в LAN не публикуется никогда
  (DN-E12).
- Никаких LAN-IP, логинов и секретов в git. В `.env.example` только плейсхолдеры.

### 7.3 Ресурсы CORE

Факты из инвентаря B:

- 31 GiB RAM, из них ≈23 GiB доступно; после удаления VM `k8s-cp01` будет ≈27;
- swapfile 23 GiB;
- DRAM-less SATA SSD, 377 GB свободно;
- Debian 13.6, Docker 26.1.5, Compose 2.26.1, cgroup systemd;
- YouTrack (SHARED) остаётся на хосте.

| Компонент | Лимит | Внутри |
|---|---|---|
| OpenSearch | 6 ГБ | heap 3 ГБ (≤ 50 % лимита), остальное — файловый кэш Lucene (индексы ~1–3 ГБ) |
| Neo4j | 6 ГБ | heap 2 ГБ, page cache 2 ГБ (→ 3 ГБ, если store > 2 ГБ), транзакции ≤ 1 ГБ, остальное — native/netty |
| VKM API + MCP + DuckDB | ~4–5 ГБ | DuckDB `memory_limit` задаёт D или G |
| Проекторы во время rebuild (транзиентно) | ~2–3 ГБ | Arrow-таблицы, dict-батчи |
| Итого VKM | ~18–20 ГБ | остаток — YouTrack, ОС и кэш |

Лимиты — нижняя разумная граница. Поднимать их по измерениям canary, а не заранее. `bootstrap.memory_lock` защищает
heap OpenSearch от swap.

**Диск** `$VKM_DATA_ROOT` — по B на CORE, ext4 на SATA SSD:

- opensearch/data — до ~6 ГБ (два builds);
- neo4j/data — ~1–3 ГБ плюс транзакционные логи;
- logs и receipts — сотни МБ.

Итого с запасом **≥ 20 ГБ свободно** под проекции.

- Retention транзакционных логов Neo4j ограничить: граф пересобираем, длинная история не нужна. Пример:
  `db.tx_log.rotation.retention_policy=1G size`; синтаксис проверить в закреплённой версии.
- **Хост.** `vm.max_map_count` должен быть ≥ 262 144: это bootstrap check OpenSearch при не-loopback bind внутри
  контейнера. По инвентарю B на CORE уже **1 048 576**, так что системных изменений не требуется. Deploy-скрипт только
  проверяет значение и падает, если оно ниже.
- На CORE `/tmp` — tmpfs. Временные файлы проекторов живут в `$VKM_DATA_ROOT/tmp` (§2.10).
- `ulimits`: memlock без ограничений, nofile 65536.
- Bind-mount каталоги принадлежат uid контейнеров: neo4j — 7474, opensearch — 1000.

### 7.4 Compose-фрагмент

Итоговый файл собирает координатор в `infra/core/`. Значения — плейсхолдеры.

```yaml
name: vkm-core

services:
  neo4j:
    image: neo4j:5.26.31-community@sha256:<pin-at-deploy>
    restart: unless-stopped
    ports:
      - "127.0.0.1:7474:7474"   # HTTP / Neo4j Browser: только loopback, доступ через SSH-туннель
      - "127.0.0.1:7687:7687"   # Bolt: проектор, VKM API/MCP на этом же хосте
    environment:
      NEO4J_AUTH_FILE: /run/secrets/neo4j_auth
      NEO4J_server_memory_heap_initial__size: 2g
      NEO4J_server_memory_heap_max__size: 2g
      NEO4J_server_memory_pagecache_size: 2g
      NEO4J_db_memory_transaction_total_max: 1g
      NEO4J_server_jvm_additional: "-XX:+ExitOnOutOfMemoryError"
      NEO4J_dbms_usage__report_enabled: "false"
    secrets:
      - neo4j_auth
    volumes:
      - ${VKM_DATA_ROOT}/neo4j/data:/data
      - ${VKM_DATA_ROOT}/neo4j/logs:/logs
      - ${VKM_DATA_ROOT}/neo4j/import:/import   # только для резерва B (§2.11)
    mem_limit: 6g
    healthcheck:
      test: ["CMD-SHELL", "wget --no-verbose --tries=1 --spider http://localhost:7474 || exit 1"]
      interval: 30s
      timeout: 10s
      retries: 5
      start_period: 90s
    logging:
      driver: json-file
      options: {max-size: "50m", max-file: "5"}

  opensearch:
    image: opensearchproject/opensearch:3.8.0@sha256:<pin-at-deploy>
    restart: unless-stopped
    ports:
      - "127.0.0.1:9200:9200"   # REST: только loopback; 9600 (Performance Analyzer) не публикуется
    environment:
      cluster.name: vkm-core
      node.name: vkm-os-1
      discovery.type: single-node
      bootstrap.memory_lock: "true"
      OPENSEARCH_JAVA_OPTS: "-Xms3g -Xmx3g"
      DISABLE_INSTALL_DEMO_CONFIG: "true"
      DISABLE_SECURITY_PLUGIN: "true"
      action.destructive_requires_name: "true"
    ulimits:
      memlock: {soft: -1, hard: -1}
      nofile: {soft: 65536, hard: 65536}
    volumes:
      - ${VKM_DATA_ROOT}/opensearch/data:/usr/share/opensearch/data
    mem_limit: 6g
    healthcheck:
      test: ["CMD-SHELL", "curl -fsS 'http://localhost:9200/_cluster/health?wait_for_status=yellow&timeout=5s' >/dev/null || exit 1"]
      interval: 30s
      timeout: 10s
      retries: 5
      start_period: 120s
    logging:
      driver: json-file
      options: {max-size: "50m", max-file: "5"}

secrets:
  neo4j_auth:
    file: ${VKM_SECRETS_DIR}/neo4j_auth   # host-local файл «neo4j/<пароль>», права 600, вне любых репозиториев
```

На этапе 3 проверить:

- наличие `wget` в образе Neo4j (его используют примеры документации Neo4j; альтернатива — `cypher-shell 'RETURN 1'`);
- наличие `curl` в образе OpenSearch;
- что настройки с точкой в имени принимаются через env entrypoint'ом образа.

Healthcheck показывает только, что процесс жив. Готовность проекции (`ProjectionRun COMPLETE`, alias существует)
проверяют `vkm-corpus graph status` и `search status` и `/status` API.

Требования к этому фрагменту:

- Compose-файл и `.env.example` коммитятся с `eol=lf` (CP-13).
- Хост-пути задаются только через `${VKM_DATA_ROOT}` и `${VKM_SECRETS_DIR}` (A п. 16).
- Тест гигиены `tests/corpus/test_public_hygiene.py` (CP-04) проверяет, что в них нет host-путей и частных IPv4.
  Loopback `127.0.0.1` к частным диапазонам 10/8, 172.16/12 и 192.168/16 не относится.

### 7.5 Конфигурация подключения (`.env.example`, только плейсхолдеры)

```
# корень runtime-данных на хосте (канон, проекции, логи, receipts)
VKM_DATA_ROOT=<path-on-host>
# каталог host-local секретов (вне репозиториев)
VKM_SECRETS_DIR=<path-on-host>
VKM_NEO4J_URI=bolt://127.0.0.1:7687
VKM_NEO4J_USER=neo4j
# пароль — только host-local: либо файл, либо переменная окружения сервиса; в git не попадает
VKM_NEO4J_PASSWORD_FILE=<path-on-host>
VKM_NEO4J_DATABASE=neo4j
VKM_OPENSEARCH_URL=http://127.0.0.1:9200
# пусто при выключенном security plugin
VKM_OPENSEARCH_USER=
VKM_OPENSEARCH_PASSWORD_FILE=
VKM_OPENSEARCH_INDEX_PREFIX=vkm
VKM_GRAPH_BATCH_SIZE=5000
VKM_SEARCH_BULK_CHUNK_DOCS=1000
VKM_SEARCH_BULK_CHUNK_BYTES=10485760
# только для live-тестов: одноразовые инстансы, никогда не production
VKM_TEST_NEO4J_URI=
VKM_TEST_OPENSEARCH_URL=
```

Конфигурацию читает `src/vkm_corpus/config.py` (координатор). Пароли читаются из `*_FILE` или из окружения процесса и
не логируются. В receipts и `/status` URI выводятся без учётных данных.

### 7.6 Операционные сценарии

- **Первый запуск.**
  1. Каталоги под `$VKM_DATA_ROOT`, права uid, проверка `vm.max_map_count` (по B уже достаточно).
  2. Секрет Neo4j.
  3. `docker compose up -d`.
  4. `vkm-corpus graph ddl`.
  5. `graph rebuild --mode wipe`.
  6. `search build`.
  7. `graph verify` и `search status`.
- **Новый снапшот канона.** `graph rebuild` (refresh) и `search build`. Оба пишут receipts; `/status` сверяет снапшоты
  трёх проекций.
- **Обновление образа.** Новый pinned digest → `up -d` → `graph rebuild --mode wipe` и `search build`. Миграций нет.
- **Катастрофа или acceptance §46.** Остановить сервисы, удалить `$VKM_DATA_ROOT/neo4j/data` и
  `$VKM_DATA_ROOT/opensearch/data`, запустить, выполнить DDL, rebuild и build. Сравнить digests с прошлыми receipts того
  же снапшота.

---

## 8. Тест-план

### 8.1 Принципы

- Unit и контракт-тесты **не требуют** серверов и приватного корпуса. Используется только синтетический канон,
  генерируемый тестом в `tmp_path` (бриф §6). Binary-фикстуры и Parquet не коммитятся, текст фикстур — синтетические
  слова.
- Live-тесты помечаются общим маркером координатора **`@pytest.mark.services`** (CP-11) и узкими `neo4j` /
  `opensearch` для выборки. Драйверы подключаются через `pytest.importorskip("neo4j")` / `("opensearchpy")`, потому что
  они лежат в extra `corpus-services`. Тесты используют **одноразовые** инстансы: `VKM_TEST_NEO4J_URI`,
  `VKM_TEST_OPENSEARCH_URL` и префикс индексов `vkmtest-<random>`.
  - Если переменных нет или сервис недоступен — `pytest.skip("NOT_RUN: <причина>")`.
  - Тест **отказывается** работать, если тестовый URI совпадает с production (`VKM_NEO4J_URI` и `VKM_OPENSEARCH_URL`):
    live-тесты делают wipe.
- Флаг `--require-live` (conftest) превращает NOT_RUN в FAIL. Его использует агент I на CORE.
- Итоги тестов попадают в receipt: skip с префиксом `NOT_RUN:` учитываются отдельно как NOT_RUN, не как PASS
  (постановка §47).

### 8.2 Синтетический канон (фикстура)

- 2 Work; 3 Source, из них две копии одной Work.
- 6 Pages: одна со статусом FAILED и пустым текстом, одна OCR.
- Blocks на страницах; 2 Figures: одна `UNKNOWN_FIGURE_TYPE`, одна с `object_label` `3.1` и артефактом.
- 1 Table; 2 Formulas: одна с LaTeX, одна без.
- 5 Bibliography entries: 2 принятые связи на одну cited Work из двух копий, 1 кандидат, 1 без связи, 1 самоцитирование.
- 3 Authors, один с двумя ролями в одной работе; 1 Venue.
- Словоформы для проверок анализатора: оседание, оседания, расчётная, сильвинит, сильвинита, сильвин.
- Кейсы из A §2.8 и K11/K12 (все синтетические):
  - запись библиографии на «чужой» странице с `citing_work_id = UNKNOWN`;
  - запись на странице выпуска, принадлежащая другой Work корпуса;
  - пара страниц разных Source с одинаковым нормализованным текстом (`DUPLICATE_CANDIDATE_OF`, общий `dup_group_id`);
  - курированные связи `ABSTRACT_OF`, `NOT_SAME_AS` (обе Work существуют) и `EDITION_OF` на внешнюю работу (ребро не
    создаётся, отчёт `RELATION_ENDPOINT_EXTERNAL`);
  - AUTO_PARSED-кандидат связи (в граф не идёт);
  - Source со статусом `SKIPPED_BY_REGISTER` без страниц;
  - заменённая версия блока (`SUPERSEDED`, в проекции отсутствует).
- Для негативных тестов — генераторы дефектов:
  - висячий FK;
  - дубликат ID между датасетами;
  - разрыв `page_number`;
  - повышенный статус `FACT`;
  - неизвестное поле;
  - неизвестный `relation_type`;
  - `NOT_SAME` внутри одной Work;
  - симметричная пара в обе стороны.

### 8.3 Офлайн контракт-тесты (всегда)

| Тест | Что проверяет |
|---|---|
| `tests/corpus/graph/test_graph_schema_registry.py` | Каждый label в ровно одном слое. Labels DOCUMENT = список постановки. Types = 10 обязательных + `HAS_BIBLIOGRAPHY_ENTRY` + `RESOLVES_TO`. Родовых типов (`RELATED*`, `RELATION*`, `LINKED_TO`) нет. Концы типов определены. DDL: по одному ограничению уникальности на label + `doc_layer_id_unique`, всё `IF NOT EXISTS`, префиксы `doc_`/`sys_`. **Нет Enterprise-only конструкций** (`IS NOT NULL`, `IS NODE KEY`, `IS RELATIONSHIP KEY`, `IS ::`, `GRAPH TYPE`) |
| `tests/corpus/graph/test_graph_rows.py` | Белый список свойств: в свойствах графа нет текстовых полей и LaTeX. Нет map-значений и null в списках, списки однородны. Конверт и типизированные ID заполнены. Сортировка по `id`. Число рёбер = числу FK. `precedes_v1`: n−1 на Source, тот же Source, +1. `cites_v1`: только принятые, одно ребро на пару, отсортированные `via_entry_ids`, `support_count = 2` для двух копий, самоцитирование исключено и отражено в отчёте. Висячий FK, дубликат ID, разрыв страниц, запрещённый статус → соответствующие `E_*`, а не тихий пропуск |
| `tests/corpus/graph/test_graph_cypher_templates.py` | В шаблоны подставляются только label и type из реестра. Значения — только `$`-параметры. `CALL … IN TRANSACTIONS` только в wipe/sweep (auto-commit путь). Нет `apoc.` и Cypher-25-only синтаксиса |
| `tests/corpus/graph/test_graph_bibliography_attribution.py` | `REFERENCE_OF` только из `citing_work_id`. Запись на чужой странице не приписана Work хоста. UNKNOWN → нет ребра, флаг, отчёт. `CITES` использует `citing_work_id`, а не Work Source-хоста. Две копии одной Work дают одно ребро `CITES` с `support_count = 2`. Граф Phase 1 не импортируется (в строках нет рёбер с `CG-` основанием) |
| `tests/corpus/graph/test_graph_work_relations.py` | Закрытый список типов синхронен enum D. Курированные связи дают типизированные рёбра, кандидаты — нет. Внешний конец → нет ребра + отчёт. Симметричные хранятся одним ребром. `NOT_SAME` внутри одной Work → `E_RELATION_CONFLICT`. SAME_WORK выражен через `INSTANCE_OF` (`copy_kind`, `part_index`, `printed_page_offset`), отдельного ребра нет |
| `tests/corpus/graph/test_graph_duplicates_and_versions.py` | `DUPLICATE_CANDIDATE_OF` только между разными Source, одно ребро на пару. `dup_group_id` по умолчанию равен `page_id`. `SUPERSEDED` объекты отсутствуют в строках графа и документах индекса. 251 Source = обработанные + SKIPPED + FAILED (C16) |
| `tests/corpus/graph/test_graph_digest.py` | `expected_digest` инвариантен к перестановке входных строк, меняется при изменении любого свойства, не зависит от `projection_run_id`. Golden digest синтетического канона (намеренное изменение требует обновления версии) |
| `tests/corpus/graph/test_graph_expected_counts.py` | Ожидаемые counts и C4/C5-ожидания из канона |
| `tests/corpus/search/test_search_mappings.py` | `dynamic: strict`. Все ссылки на analyzer/normalizer разрешаются. `COMMON` одинаков во всех типах. `_meta` содержит версию и `built_from_snapshot_id`. Защищённые термины в нижнем регистре и ё-свёрнуты. **Ни одного запрещённого ключа** (CP-04) на любой глубине (функция `_json_keys` стража). Экспорт `schemas/search/*.json` и `schemas/graph/*.cypher` проходит `vkm_world.governance.leakage.scan` и `tests/corpus/test_public_hygiene.py`. Экспортированные файлы в LF (CP-13) |
| `tests/corpus/search/test_search_documents.py` | `_id == id`, `_op_type == create`, имя индекса. Ключи документа ⊆ properties mapping; типы совместимы (int, float, bool, date). `text_sha256 == sha256(канонический текст)`. Одна строка — один документ, включая FAILED-страницу. Денормализация (`title`, `authors`, `year`, `available_from`, `site_scope_raw`, `site_scope`, `site_scope_mapping`). У библиографии есть `citing_work_id`, а `work_id` — Work хоста. `dup_group_id` задан у всех страниц и блоков. Детерминизм `doc_stream_sha256` |
| `tests/corpus/search/test_search_query_builder.py` | Белый список фильтров (неизвестный → ошибка). Terms и range, включая `available_from_lte`. Маршрутизация `object_type` → alias. Разбор «рис. 3.1», «Рисунок 3.1», «Fig. 3.1», «табл. 2», «(3.12)». Tie-break в sort. `_source.includes` без текстов. Ограничения `size` |
| `tests/corpus/search/test_search_alias_plan.py` | Действия `_aliases` для пустого кластера, для swap и для частичной пересборки. Rollback. Retention никогда не удаляет alias-индекс и предыдущий build. Согласованность `vkm-objects` |
| `tests/corpus/search/test_search_hit_contract.py` | Разбор записанного синтетического ответа OpenSearch (JSON-фикстура без текстов источников) в `SearchHit`. Подсветка помечена как `SEARCH_INDEX`, collapse даёт `best_blocks` |

### 8.4 Live-интеграция (одноразовые инстансы; иначе NOT_RUN)

**Neo4j:**

1. DDL применён дважды — число ограничений стабильно.
2. `wipe`-загрузка синтетики — C1–C11 проходят.
3. Повторный `refresh` — 0 создано, 0 удалено, digest тот же (C12).
4. Изменение канона (удалён блок, изменён список авторов, отозвана связь) → `refresh` → устаревшее удалено, digest равен
   ожидаемому.
5. Межслойный guard: вручную в **тестовой** БД создано `(:TestNode:EvidenceLayer)-[:SUPPORTED_BY]->(:Page)`.
   - `wipe` без `--cascade` → `E_CROSS_LAYER_LOSS`;
   - `refresh` сохраняет ребро к неизменившейся Page;
   - при удалении Page ребро попадает в `BROKEN_CROSS_LAYER_REFERENCES`.
6. Висячая ссылка → `ProjectionRun FAILED`.
7. Запись в READ-транзакции отклоняется (`AccessMode`).
8. Второй параллельный прогон → `E_PROJECTION_BUSY`.
9. Параметризация образом: 5.26 LTS и (опционально) 2026.09.x.

**OpenSearch:**

1. Создание индексов из тел §4.
2. `_analyze`-проверки S4.
3. Bulk синтетики, counts, выборка S3.
4. Фильтры соблюдены, подсветка есть, детерминированный порядок, collapse работает.
5. Duplicate ID при `create` → ошибка поднята.
6. Strict mapping отклоняет лишнее поле.
7. Alias swap под нагрузкой чтения: поиск по alias ни разу не вернул 404. Rollback.

### 8.5 Acceptance на реальном корпусе (агент I, CORE, `--require-live`)

**Neo4j (§58):**

- counts = канон;
- Source → Work для всех источников с известной работой; исключения перечислены;
- отчёт покрытия Work → Author;
- Source → Pages = `page_count` для каждого источника;
- Page → objects;
- CITES только из принятых связей;
- трассировка C10 — 100 %;
- rebuild с пустого volume даёт тот же `content_digest`.
- Курируемые группы A §2.8 видны в графе:
  - 013/025/202 — одна Work, три `INSTANCE_OF`;
  - `ABSTRACT_OF` 001 → 196;
  - `NOT_SAME_AS` не слит (C13).
- Записи на страницах из `foreign_page_references` не приписаны Work хоста (C14).
- Общие страницы «Горного эха» связаны `DUPLICATE_CANDIDATE_OF`, если D их обнаружил.
- 013 и 022 присутствуют как Source без страниц (C16).
- **Внешний оракул** (A п. 22): граф цитирований Phase 1 и `SKRU1_OBJECT_SOURCE_INDEX` используются только для **оценки**
  полноты. Сравнивается число связанных записей и найденных Figure/Table. Как истина для сборки они не используются.

**OpenSearch (§59):**

- Q1–Q5 (+ Q6–Q8) по §6.5;
- фильтры, включая `site_scope` и `site_scope_raw` (CP-07) и `available_from_lte`;
- свёртка дублей: общая страница двух выпусков приходит одним кандидатом;
- стабильные ID;
- ID пригодны для реранка: канонический текст получен, sha совпал, артефакт разрешается;
- rebuild: удалить все `vkm-*` → собрать → те же counts, `doc_stream_sha256` и top-20 ID.

**Canary (§45):** те же сборки на канареечном снапшоте. Измерить время и память, настроить батчи и heap, записать
измерения в receipts.

### 8.6 Соответствие постановке §47

- «Neo4j projection» — §8.3 (graph) и §8.4 (Neo4j).
- «OpenSearch indexing/search» — §8.3 (search) и §8.4 (OpenSearch).
- «stable ID» — частично: грамматику ID тестирует D, здесь — сквозная неизменность ID в графе и индексе.
- «rebuildability» — §8.4 и §8.5.

---

## 9. Риски, открытые вопросы и decision notes

### 9.1 Риски

| # | Риск | Влияние | Смягчение |
|---|---|---|---|
| RK1 | Контракт D отличается от допущений §1.1 | переделка сопоставления полей | Сопоставление декларативно (таблица на label или индекс). Контракт-тесты на схемах D. Сверка после выхода `AGENT_D_*` |
| RK2 | Граф или индекс случайно становится каноном: ручные правки, производные связи только в графе, текст из `_source` | научная семантика и провенанс | R3, R4, R7. Белый список свойств. Без текстов в графе. `CITES` материализуется в каноне (DN-E06). Digest-равенство. API берёт текст только из канона, со сверкой sha |
| RK3 | `wipe` неатомарен: API видит частичный граф | неверные ответы | Статус `ProjectionRun` как шлюз (§1.6). `refresh` по умолчанию |
| RK4 | Потеря межслойных рёбер при rebuild (в будущем) | тихая потеря evidence-ссылок | R5, R6: `--cascade`, отчёт `BROKEN_CROSS_LAYER_REFERENCES`, якорь Page + bbox (§3.3) |
| RK5 | Несогласованные словари скоупов: 18 сырых значений реестра; AMBIGUOUS у 002, 012, 045 | фильтр по `site_scope` не вернёт AMBIGUOUS-источники | CP-07: `site_scope_raw` + `site_scope` + `site_scope_mapping` из канонической таблицы; API показывает `site_scope_mapping`; acceptance Q4 это учитывает |
| RK6 | Ошибки русского стемминга: словообразование, беглые гласные, «-ит» | потери полноты | Защищённые термины, `.exact`, реранкер после BM25. Оценка hunspell или ICU по данным canary |
| RK7 | В реестре нет авторов и названий | acceptance §58 «Work → Author» частичен | A п. 13 указал источники: `SOURCE_COVERAGE_MASTER.csv` для 001–041; таблица разведки, intake-манифесты и notes для 042–251; поле `metadata_basis`. Покрытие считает отчёт C3 и §8.5 (DN-E09) |
| RK8 | Производительность MERGE ниже оценки | rebuild дольше | Настройка батчей; резерв B (§2.11) |
| RK9 | Security plugin выключен | локальный процесс может менять индексы | Loopback, пересобираемость, `destructive_requires_name`, SSH-only доступ к CORE |
| RK10 | Нехватка RAM или диска на CORE | OOM, watermark блокирует запись | Лимиты контейнеров, preflight диска, инвентарь B, измерения canary |
| RK11 | Live-тесты попадут на production | потеря проекции (пересобираемо, но простой) | Отдельные переменные и префиксы, отказ при совпадении URI |
| RK12 | Одновременные прогоны проектора | неконсистентный граф или индекс | Файловые lock'и и проверка статуса `ProjectionRun` |
| RK13 | Снапшот канона пишется во время чтения | несогласованная проекция | Запечатанный манифест; чтение только перечисленных файлов (DN-E04) |
| RK14 | Длинные страницы превышают окно реранкера | потеря релевантного фрагмента | Пассажный путь (collapse и `best_blocks`) (DN-E11) |
| RK15 | `vm.max_map_count` на CORE окажется ниже порога (после переустановки или смены ядра) | OpenSearch не стартует (bootstrap check) | По B сейчас 1 048 576. Deploy-скрипт проверяет значение перед `up` (DN-E16) |
| RK16 | Запись библиографии на чужой странице приписана Work хоста | ложные `CITES`, искажённая сеть цитирований | `REFERENCE_OF` только из канонического `citing_work_id`; UNKNOWN → нет ребра (§1.5, C14; A K11) |
| RK17 | Дубли страниц («Горное эхо»: общие страницы выпусков) дают двойной счёт в поиске и в будущем evidence | «дубликат = независимое подтверждение» | `DUPLICATE_CANDIDATE_OF` + `dup_group_id`, свёртка выдачи по умолчанию (A K12) |
| RK18 | Enum связей Work у D меняется без обновления реестра E | «потерянные» связи или соблазн ввести общий тип | Закрытый список, `E_UNKNOWN_RELATION_TYPE`, тест синхронности с enum D |

### 9.2 Открытые вопросы (кому)

**Закрыты после ревизии:**

| Вопрос | Чем закрыт |
|---|---|
| Нормализация скоупа (раньше Q-D2) | CP-07: `site_scope_raw`, `site_scope`, `site_scope_mapping`; AMBIGUOUS не разрешается |
| Статьи внутри томов (раньше Q-D4) | CP-09 и A §2.8: один Work на контейнер, дочерние Work не создаются |
| Источник метаданных Work (раньше Q-AD1) | A п. 13 и CP-09 |
| Транспорт изображений (часть Q-FG1) | CP-18: base64 от API |
| Порты | Q-B, §16 B: свободны |
| Docker и Compose на CORE | 26.1.5 / 2.26.1 |
| Диск | 377 GB, SATA SSD |
| RAM | ≈23 GiB доступно |
| `vm.max_map_count` | 1 048 576 |

**D (открыто):**

- **Q-D1.** Формат манифеста снапшота, `snapshot_id` и generation pointer. Общий вопрос с G (DN-G6).
- **Q-D3.** Входят ли в `works` внешние цитируемые работы (только с DOI или ISBN, `in_corpus=false`)? От этого зависят
  `EDITION_OF` на первое издание 011 и `NOT_SAME_AS` на справочник для 050.
- **Q-D5.** Enum-ы:
  - `review_status`, `origin`, `link_status`, `role`, `bbox_space`, `relation_type`, `copy_kind`;
  - порог `figure_type_confidence`;
  - представление UNKNOWN у `citing_work_id` (null или маркер).
- **Q-D7.** Направления типов связей Work: `VOLUME_OF` — к Work-набору или между томами; `SERIES_PART` — к серии или
  парой. Задаёт ли D `DERIVED_FROM` (025 → 013) отдельным типом?
- **Q-D8.** Кто и как строит `page_duplicate_candidates`: основание (хеш нормализованного текста и/или рендера), порог,
  review.
- **Q-D9.** Лежат ли `SUPERSEDED`-версии объектов в снапшоте, и как называется поле статуса версии.

**F и G (открыто):**

- **Q-FG1.** Окно и правило состава rerank-текста по типам.
- **Q-FG2.** Поведение API при `WIPING` и `FAILED` (503 или деградированный ответ).
- **Q-FG3.** Сворачивать ли дубли страниц в `search_text` по умолчанию (предложение E — да).

**H:**

- **Q-H1.** Проверить правила R1–R8 (§3.1) на совместимость с разделением World / Representation / Observation.
- **Q-H2.** Проверить, что «одно ребро CITES на пару» не теряет научной семантики.
- **Q-H3.** Проверить защитные меры против «graph becomes canonical» (RK2).
- **Q-H4.** Проверить, что связи Work/Source (`NOT_SAME_AS`, `ABSTRACT_OF`…) и `DUPLICATE_CANDIDATE_OF` остаются
  документными фактами и не читаются как научные утверждения. Проверить, что свёртка дублей в поиске не прячет
  расхождения между копиями.

### 9.3 Decision notes для координатора

| ID | Решение (предложение E) | Адресат |
|---|---|---|
| DN-E01 | Общеграфовый контракт: layer labels (`:DocumentLayer` и далее), уникальность type labels и relationship types по слоям, владение (R3), канон на каждый слой (R4), DAG и `--cascade` (R5, R6), запрет ручных записей (R7) | координатор, H |
| DN-E02 | Rebuild Neo4j: онлайн-проектор (`refresh` по умолчанию, `wipe` для acceptance). `neo4j-admin import` — только резерв | координатор |
| DN-E03 | Версия Neo4j: 5.26 LTS (5.26.31) или 2026.09.x CalVer. E рекомендует LTS; код совместим с обеими | координатор |
| DN-E04 | Запечатанный манифест снапшота канона. Все проекции читают только перечисленные файлы и пишут `canonical_manifest_sha256` | D |
| DN-E05 | Поля, обязательные для проекций: см. §1.1. Ключевые: `bibliography.host_page_id` не null, `citing_work_id` (значение или UNKNOWN); `work_authors.role` не null; `page_number` 1..n = `page_count` (кроме `SKIPPED_BY_REGISTER`); `available_from` у Work (или UNKNOWN); `bbox_space`; `image_artifact_id`; `object_status`. `text_sha256` считает сам индексатор, от D не требуется | D |
| DN-E06 | `CITES` — канонический производный датасет `citations` с `rule_version`; критерий «принятой» связи задаёт D; агрегирование по паре; самоцитирование исключено. Иначе проектор исполняет резервное правило `cites_v1` | D, H |
| DN-E07 | Скоупы — по CP-07 (принято). Проекции переносят `site_scope_raw`, `site_scope` и `site_scope_mapping` как есть. Для G: `evidence_scope` в API = `site_scope_raw` в индексе | G (сведение имён) |
| DN-E08 | `page_id` не зависит от версии pipeline (CP-08). ID объектов version-aware по A п. 12, с `content_sha256` и канонической `supersedes`. Проекции берут только `CURRENT`. Будущие evidence-ссылки якорятся на Page + bbox + `content_sha256_at_review`; переякорка — по `supersedes` | D, H |
| DN-E09 | Метаданные Work, Author, Venue — по A п. 13 и CP-09. Покрытие `AUTHORED_BY` отражается в отчёте проекции, а не подменяется догадками | D |
| DN-E10 | Внешние цитируемые Work в `works` — только с DOI или ISBN (`in_corpus=false`, `identity_basis`), без агрессивных слияний | D |
| DN-E11 | Rerank-текст берётся из DuckDB по ID со сверкой `text_sha256`. Кандидаты-страницы идут пассажами (collapse и `best_blocks`). Состав по типам — `rerank_text_v1` | G, F |
| DN-E12 | OpenSearch без security plugin и только на loopback. Neo4j с auth через Docker secret, по умолчанию тоже только loopback: на CORE tailnet с чужими устройствами, ufw неактивен, Docker обходит firewall. LAN-привязка допустима только для Neo4j и только по явному желанию пользователя. Наружу выходят только API и MCP. Пересмотреть при смене топологии | координатор, B |
| DN-E13 | Retention: хранить текущий и предыдущий build OpenSearch и всю историю `ProjectionRun`. Удалять старые builds только флагом `--prune`. Scoped in-place обновления индекса не делать в v0 | координатор |
| DN-E14 | Анализаторы, бусты и список защищённых терминов — MODEL_CHOICE retrieval, версионируются mapping version. Калибровка — не на тестовой истине | координатор |
| DN-E15 | API и MCP: граф только через READ-транзакции, шлюз по статусу `ProjectionRun`, без Cypher-пасса. Тексты — только из канона | G |
| DN-E16 | Предпосылки CORE подтверждены инвентарём B: Docker 26.1.5 и Compose 2.26.1, `vm.max_map_count` = 1 048 576, свободные порты, 377 GB на SSD. Остаются: проверка `vm.max_map_count` в deploy-скрипте; лимиты памяти 6 + 6 ГБ; временные файлы в `$VKM_DATA_ROOT/tmp` (на CORE `/tmp` — tmpfs) | координатор |
| DN-E17 | Типизированные связи Work и Source из курируемой таблицы (CP-09) — рёбра слоя DOCUMENT: закрытый список, без общего типа, только курированные строки. SAME_WORK — через `INSTANCE_OF` (`copy_kind`, `part_index`, `printed_page_offset`) | D, H |
| DN-E18 | Библиография: страница-носитель (`HAS_BIBLIOGRAPHY_ENTRY` от `host_page_id`) и принадлежность (`REFERENCE_OF` от `citing_work_id`) разделены. `CITES` строится только через `citing_work_id`. Граф цитирований Phase 1 — оракул и crosswalk, не авто-объекты (A K11) | D, H |
| DN-E19 | Дубли страниц: канонический датасет кандидатов (D или C), ребро `DUPLICATE_CANDIDATE_OF`, `dup_group_id` в индексе, свёртка выдачи по умолчанию. Слияния нет (A K12) | D, C, G |
| DN-E20 | Имена для G: `build_id` = ID `ProjectionRun` или build индекса; `built_from_snapshot_id` — в `ProjectionRun` и `_meta`; `status()` модулей E — единый словарь (§5.4). Системные ID проекторов — с префиксом `VKM-PRJ-`, вне занятых (A K6) | G, D |

### 9.4 Сознательно не делается в v0

- Узлы Artifact и ProcessingRun в графе: ID лежат в свойствах, провенанс отдаёт DuckDB.
- Узлы и ограничения будущих слоёв.
- APOC и GDS.
- Full-text и vector indexes Neo4j.
- Vector-поля и embeddings в OpenSearch (постановка §28).
- ICU, hunspell и синонимы.
- OpenSearch Dashboards.
- Scoped in-place обновления индекса.
- Scoped `refresh` одного Source в графе — это P1.
- `works`-индекс — P1.
- Реализация резерва B.

---

## Приложение A. Раскладка кода (владение E)

```
src/vkm_corpus/graph/
  schema.py        # реестр GRAPH_SCHEMA: labels, слои, rel types, концы, белые списки свойств; генерация DDL
  rows.py          # канон (DuckDB поверх Parquet из манифеста) → строки узлов и рёбер; только CURRENT-объекты;
                   # precedes_v1, cites_v1 (через citing_work_id); связи Work/Source; DUPLICATE_CANDIDATE_OF
  preflight.py     # ссылочная целостность, дубликаты, разрывы страниц, enum-ы, запрещённые статусы,
                   # закрытый список relation_type, NOT_SAME-конфликты, симметричные пары
  loader.py        # батчи execute_query, шаблоны MERGE, wipe и sweep (session.run), межслойный guard
  verify.py        # C1–C12, трассировка, digest
  runs.py          # ProjectionRun, lock, receipt
src/vkm_corpus/search/
  mappings.py      # SETTINGS, COMMON, PER_TYPE; mapping version; экспорт schemas/search/*.json
  documents.py     # канон → документы; text_sha256; doc_stream_sha256
  indexer.py       # создание индексов, streaming_bulk, refresh и forcemerge, alias swap, rollback, retention
  query.py         # query builder: фильтры (белый список), бусты, разбор номеров объектов, collapse, SearchHit
  verify.py        # S1–S6, smoke-наборы
schemas/graph/document_graph_v1.json, .cypher   # экспорт реестра и DDL (для H и I; проходит страж утечки)
schemas/search/vkm_<type>_m1.json               # экспорт тел индексов
tests/corpus/graph/…, tests/corpus/search/…, tests/corpus/integration/…  # §8
```

CLI (`src/vkm_corpus/cli/`, координатор): `graph ddl|rebuild|verify|status` и
`search build|verify|rollback|status|smoke`.

## Приложение B. Источники (проверено 28.09.2026)

Внутренние входы ревизии:

- `AGENT_A_REPO_CONTRACT_AUDIT.md` (§2.8, K4–K7, K11, K12, K14, §7 п. 11–18);
- `AGENT_B_INFRA_INVENTORY.md` (§1–4, §15–16);
- `AGENT_G_API_MCP_CAD_DESIGN.md` (§1.2, §1.8, §1.10 A6, §6.2);
- `COORDINATOR_DECISIONS.md` (CP-02…CP-13, CP-18).

Внешние:

- Neo4j Operations Manual. Full import (`--overwrite-destination`, `--schema` только в Enterprise с 2025.02,
  `--input-type=csv|parquet`), Community и единственная БД, `initial.dbms.default_database`, Docker (`NEO4J_*`,
  `NEO4J_AUTH_FILE`), `dbms.usage_report.enabled`:
  https://neo4j.com/docs/operations-manual/current/
- Neo4j Cypher Manual. Типы ограничений и их доступность в Community, graph types только в Enterprise,
  `CALL (…) {…} IN TRANSACTIONS`: https://neo4j.com/docs/cypher-manual/current/
- Драйверы Neo4j: серия 6.x совместима с серверами 4.4, 5.x, 2025.x и 2026.x: https://neo4j.com/docs/python-manual/current/
- Версии Neo4j: https://endoflife.date/neo4j (2026.09; 5.26 LTS до 06.06.2028); Docker Hub `neo4j` (теги
  `5.26.31-community`).
- OpenSearch 3.x docs: Docker (`DISABLE_SECURITY_PLUGIN`, `DISABLE_INSTALL_DEMO_CONFIG`, ulimits), анализаторы
  `russian` и `english`, фильтр `stemmer`: https://docs.opensearch.org/
- Релизы OpenSearch: https://github.com/opensearch-project/OpenSearch/releases (3.8.0 от 05.08.2026) и
  https://opensearch.org/releases/ (2.x в maintenance).
- opensearch-py: `helpers.streaming_bulk` и `parallel_bulk`: https://github.com/opensearch-project/opensearch-py
- PyPI на 28.09.2026: `neo4j` 6.3.1, `opensearch-py` 3.2.0, `pyarrow` 25.0.1, `duckdb` 1.5.5, `polars` 1.44.2.

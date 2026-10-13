# VKM Corpus Platform v0 — MCP-серверы и tools

Практический справочник по четырём MCP-серверам платформы: что они умеют, в каком виде отвечают, как их подключить к
Claude Code. Решения — [журнал координатора](../implementation_work/COORDINATOR_DECISIONS.md) (CP-16, CP-19, ответы на
H-12, H-13, H-18, H-20, H-38…H-40, H-45, H-47), проект — [проект G](../implementation_work/AGENT_G_API_MCP_CAD_DESIGN.md).
Схемы: [поток данных](../diagrams/platform_data_flow.svg), [топология хостов](../diagrams/platform_hosts.svg).

| Сервер | Где работает | Транспорт | Назначение | Подключается по умолчанию |
|---|---|---|---|---|
| `vkm-corpus` | CORE (compose `vkm-core`, сервис `mcp`) | streamable HTTP (stateless, JSON) + bearer | чтение корпуса через VKM API | да |
| `vkm-corpus-admin` | CORE (сервис `mcp-admin`, профиль `admin`) | streamable HTTP + отдельный bearer | переобработка plan-first | **нет** — только осознанно |
| `vkm-cad` | WORKSTATION (Windows) | stdio | AutoCAD/Civil 3D: детектор, чтение открытых чертежей, scratch-DXF; v1 — задания без GUI: рисование, команды, LISP, C#, объекты Civil 3D, листы, PDF | да (локально) |
| `vkm-drawio` | WORKSTATION (Windows) | stdio | детерминированные схемы draw.io | да (локально) |

MCP-серверы корпуса — тонкие адаптеры поверх VKM API (`/v1`): у них нет драйверов DuckDB, Neo4j, OpenSearch и
PostgreSQL (тест), поэтому «сырой SQL/Cypher» через MCP невозможен. Код: `vkm_corpus.mcp.servers`,
`vkm_corpus.mcp.http`; API — `vkm_corpus.api`; мосты — пакеты `vkm_cad` и `vkm_drawio`.

## 1. Ответ корпуса: `ApiResponse` и конверт `vkm.envelope/1`

Каждый tool серверов корпуса возвращает ответ API как `structured_content` и тем же JSON в текстовом блоке;
`is_error = not ok`. Исключения: `search_text`, `search_hybrid` и `search_objects` в MCP по умолчанию отвечают кратким
видом (§1.1; `view = "full"` — прежний `ApiResponse`), `reconstruct_topic` — markdown-оглавлением в тексте и короткой
сводкой в `structured_content` (`include_json = true` — полный `ApiResponse`). Тело `ApiResponse`:

| Поле | Смысл |
|---|---|
| `ok` | `true` / `false` |
| `meta` | `request_id` (он же `log_ref` в логах), `api_version`, `canonical_snapshot_id`, `elapsed_ms`, `warnings[]` |
| `item` / `items[]` | объект(ы): `{envelope, record}`; `record` — канонические поля без дублей конверта |
| `next_cursor` | курсор следующей страницы (смещение, строка цифр); `null` — список кончился (§1.2) |
| `error` | `code`, `message`, `retryable`, `stage`, `tool`, `object_id`, `hint`, `log_ref`, `details` |

Конверт отвечает на вопросы §50 без догадок агента:

| Вопрос | Поля конверта |
|---|---|
| что это? | `object_kind`, `object_id`, `object_version` (`content_sha256@commit_id`) |
| где оригинал? | `source_id`, `page_id`, `page_index` (физический, с 1), `page_label` (печатный), `geometry` (PAGE_PT_TL, pt, левый верхний угол), `provenance.source_sha256` |
| кто/что создал? | `provenance` (`processing_run_id`, `extractor_id/version`, `extraction_generation`, `models[]` с ролями LAYOUT/RECOGNITION, `config_hash`) |
| native или OCR? | `origin` (NATIVE / EMBEDDED_OCR / OCR / DERIVED / REGISTRY), `text_layer`, `region_origin` |
| auto или reviewed? | `review_status`: автоматические объекты всегда `AUTO_EXTRACTED_UNREVIEWED`, статус источника не наследуется |
| canonical, raw или projection? | `layer` (CANONICAL / ARTIFACT / PROJECTION / OPERATIONAL / SERVICE / WORKSPACE), `payload_form` (NORMALIZED / RAW / BINARY / REFERENCE), `projection` (движок, `build_id`, `built_from_snapshot_id`, совпадает ли со снимком) |
| область | `source_scope` — область **источника**; на объектах она унаследована (`inherited_from_source`, флаг `SCOPE_INHERITED_FROM_SOURCE`, H-18) |

Флаги интерпретации API (`flags`): `SCOPE_INHERITED_FROM_SOURCE`, `REGISTER_NOTES_NOT_EVIDENCE` (H-47),
`WORK_HAS_MULTIPLE_COPIES`, `CITING_WORK_IS_CONTAINER` (H-49), `PAGE_HAS_FOREIGN_CONTENT` (H-16: на странице
содержимое другой работы — `record.foreign_content`: `work_ids`, `unidentified_work`), `AUTHOR_NAME_KEY_ONLY` (H-15),
`TEXT_TRUNCATED`, `TOMBSTONE`. `work_id` страницы и объекта — работа, экземпляром которой является источник
(INSTANCE_OF, как в документах поиска E); для страницы с чужим содержимым он не означает авторства этой страницы. Предупреждения `meta.warnings`: `STALE_PROJECTION` (ID из индекса или графа нет в каноне — отброшен),
`PROJECTION_BUILD_MISMATCH`, `WORK_HAS_MULTIPLE_COPIES`, `DEGRADED_DEPENDENCY`, `TOMBSTONE`.

Коды ошибок и HTTP: `INVALID_ARGUMENT`/`INVALID_ID` 400, `UNAUTHORIZED` 401, `FORBIDDEN` 403, `NOT_FOUND` 404 (с
подсказкой; 410 не используется, H-38), `NOT_REPROCESSABLE`/`PLAN_NOT_READY`/`PLAN_CHANGED`/`JOB_STATE_CONFLICT`/`ARTIFACT_NOT_MATERIALIZED`
409, `PAYLOAD_TOO_LARGE` 413, `NO_IMAGE_ARTIFACT`/`NO_RERANK_TEXT` 422, `RATE_LIMITED` 429, `INTERNAL`/
`ARTIFACT_HASH_MISMATCH`/`ARTIFACT_NOT_DECODABLE` 500 (последние два — дефект данных, повтор не поможет),
`DEPENDENCY_ERROR` 502, `SNAPSHOT_UNAVAILABLE`/`DEPENDENCY_UNAVAILABLE` 503,
`DEPENDENCY_TIMEOUT` 504. Подмены канона текстом проекции нет: без DuckDB-снимка ответ — 503.

### 1.1 Краткий ответ поиска: `view = "brief"` (`vkm.brief/1`)

Полный ответ поиска тяжёл для контекста агента: 20 результатов `search_hybrid` — ≈ 79 тыс. знаков JSON (конверты
≈ 40 тыс., трассы ≈ 12 тыс.), `search_text` — ≈ 59 тыс. Поэтому у `POST/GET /v1/search`, `POST/GET /v1/search/hybrid`
и `POST /v1/objects/query` есть поле `view`: `full` — `ApiResponse` (умолчание API: бенчмарки, harness и приёмка §60
читают полные конверты), `brief` — проекция того же ответа (`vkm_corpus.api.brief`). У MCP-tools `search_text`,
`search_hybrid`, `search_objects` умолчание — `brief`; `view = "full"` возвращает прежний ответ. Ошибки в обоих видах
одинаковы (`ApiResponse` с `error`).

| Уровень | Поля краткого ответа |
|---|---|
| ответ | `ok`, `view = "brief"`, `schema = "vkm.brief/1"`, `meta` (`request_id`, `snapshot`, `warnings[]` — только коды с `count`; предупреждение E — `SEARCH_WARNING:<код>`), `note` (одна строка: автоматические кандидаты проекций, `AUTO_EXTRACTED_UNREVIEWED`, скоры — сигналы поиска, не evidence; сниппет — текст индекса, не объекта; область — источника), `defaults` (`review_status`, `origin` большинства результатов), `sources`, `items`, `next_cursor` |
| `run` (только гибридный) | `route`, `late` (вкл./выкл.), `translation` (`status`, `text`), `stages` — статусы `bib_route`, `visual_route`, `graph` (по стадиям, кроме `OFF`), `formulations` (`status`, `answered`), `source_cap` (`max_per_source`, `moved`); `formulations` — отправленные формулировки; `fused_total`, `candidates`, `totals`, `ms` (полное время) |
| `sources` | карточка источника: `name` (stem `canonical_path` реестра, ≤ 60 знаков), `work_id`, `scope` (значения области **источника**; `scope_raw` — когда сопоставление не EXACT/CASE), `work_copies` (зарегистрированных копий работы, если их > 1) |
| `items[]` поиска | `id` (источник и страница — его префиксы: `VKM-SRC-NNN:pNNNN[:объект]`; `source_id`/`page_id` пишутся, только если это не так), `rank`, `object_type`, `page_label`; гибридный — `bm25_rank`, `dense_rank`, `fused_rank`, `late_score`, `vis_rank`, `bib_rank` (что есть); BM25 — `bm25_score`; `snippet` (одна подсветка индекса без тегов `<em>`, ≤ 140 знаков, окно у первого совпадения); `object_label` и `title_or_caption` (≤ 120) рисунка/таблицы/формулы; `duplicates`, `copies` (только ID; `copies_count`, если в трассе только число); `flags` (флаги API, кроме общего `SCOPE_INHERITED_FROM_SOURCE`); `review_status`/`origin` — только если отличаются от `defaults` |
| `items[]` `search_objects` | `id`, `object_type`, `page_label`, `object_label`, `title_or_caption` (подпись ≤ 120) или `latex` формулы, `figure_type`, `has_image`, `flags`, отличия от `defaults` |

В кратком виде нет конвертов, провенанса, геометрии, сведений о проекции, `quality_flags`, полной трассы и
`rerank_candidate`: их дают `view = "full"`, `retrieval_trace` и `get_*`. Ничего не дописывается: значения нет в полном
ответе — нет и в кратком. Размер (синтетические ответы формы CORE на 20 результатов —
`tests/corpus/search_size_fixture.py`, порог — `test_api_brief.py`; знаки `json.dumps`): `search_hybrid` 74 546 → 7 469,
`search_text` 60 013 → 6 086, `search_objects` (8 объектов) 19 076 → 1 533; без пробелов-разделителей JSON — ещё ≈ 5 %
меньше. Около 2,7 тыс. знаков краткого ответа — сниппеты.

`reconstruct_topic`: текст ответа — markdown-оглавление в пределах `budget_chars` (как раньше), `structured_content` —
короткая сводка (`dossier_id`, `object_kind`, `review_status`, `navigation_only`, `mode`, `budget` с числом урезанных
записей по частям, `counts` по частям, `meta` с кодами предупреждений): Claude Code показывает модели именно
`structured_content`, и полный JSON досье (на CORE ≈ 25 тыс. знаков при `budget_chars = 4000`) обесценивал бюджет.
`include_json = true` возвращает полный `ApiResponse` (`TOPIC_DOSSIER`: ранжированные страницы по ярусам, данные каждой
записи). На синтетическом досье: 18 220 → 837 знаков.

### 1.2 Курсор страниц

`cursor` поиска (`/v1/search`, `/v1/search/hybrid`), `/v1/objects/query` и `/v1/source/{id}/pages` — смещение: строка
цифр из `next_cursor` предыдущего ответа, без изменений; без курсора (или пустой) — первая страница. Иное (буквы, знак,
пробел, не-ASCII цифры) — `400 INVALID_ARGUMENT` с подсказкой; раньше такой курсор молча означал 0. Конец списка
согласован с `next_cursor`: страница, заходящая за конец, обрезается по нему; курсор на конце или за ним — пустая
страница (`items: []`, `next_cursor: null`) и предупреждение `CURSOR_PAST_END` (`count` — длина списка, если известна).
Конец гибридного списка — `min(fused_total, candidates × число видов)`; со слиянием формулировок (`formulations`,
`expand = terms`) — длина слитого списка фиксированной глубины (каждая страница заново выполняет формулировки и листает
тот же список). `offset + limit > 1000` — `400` (предел окна поиска E). Курсор `get_table_structured` и `list_evidence` —
непрозрачная строка своего формата (не смещение).

### 1.3 Изменения 13.10.2026: формулы реестра, копии одной работы, область источника, номера объектов

- **Формулы и реестр моделей.** `get_formula_context` (NAV, [NAVIGATION_LAYER.md](NAVIGATION_LAYER.md) §5) отдаёт
  `registry_models`: модели реестра `MM-*`, связанные с формулой по каталогу
  [model_formula_links](../science/MODEL_FORMULA_LINKS_RU.md) (`model_id`, `name`, `equation_ref`, `match_status` EXACT /
  NUMBER_MATCH / FORM_MATCH / CANDIDATE, `check` CONFIRMED / UNSURE / NOT_REVIEWED; REJECTED и NOT_FOUND не
  показываются; `null` — пакет каталогов не подключён). Связь «модель ↔ запись в книге» — навигация, не проверка закона.

- **Копии одной работы схлопываются по умолчанию.** Умолчание графовых стадий `search_hybrid` — `["collapse"]` (G1;
  решение владельца 13.10; GRAPH_SEARCH_V1: G1 даёт `page_recall@50` +0,010 без вреда на retrieval_v1). Кроме
  текстовых дублей NAV страница уходит из выдачи, когда уже показана выровненная страница другого источника той же
  работы (сдвиг страниц выводится из NAV: ≥ 3 пар и ≥ 80 % согласных; не угадывается). У оставшегося хита
  `record.copies`: `{page_id, coverage, kinds}`, для выровненной страницы — ещё `kinds: ["SAME_WORK_ALIGNED_PAGE"]`,
  `work_id`, `page_offset`; у объектов — `{object_id, cluster_id, kind}`. `graph: []` или `VKM_HYBRID_GRAPH=none`
  выключает; `include_duplicates: true` снимает умолчание; нужна поздняя стадия (иначе `SKIPPED_LATE_OFF`), без NAV —
  предупреждение. Предупреждение `WORK_HAS_MULTIPLE_COPIES` в `meta.warnings` — только когда среди хитов два и более
  источника одной работы.
- **Сводки по работам.** `parameter_summary`: в строках `n_works`, `work_ids`, `copies` (`n_candidates`, `n_pages` —
  по-прежнему по файлам). `find_parameters`, `find_tables`: одна позиция на объект работы, другие экземпляры — в
  `copies`, сводка `by_work` (`merged`, `n_works`, `n_sources`); `total` — число файловых позиций NAV.
- **Область `SKRU1_SKRU2_SKRU3` — все три рудника** (таблица областей v2, решение владельца 13.10;
  [CP-07](../implementation_work/COORDINATOR_DECISIONS.md)): VKM-SRC-002, 012 (Лебедева), 045 (Ермашов) — MULTI →
  SKRU1, SKRU2, SKRU3; фильтр `source_scope=SKRU1` (SKRU2, SKRU3) их берёт. Снимок 2d71e9e8 и индексы собраны по v1:
  API отвечает по v2 (конверты, `get_source`), а фильтр добавляет эти источники по `source_id` до пересборки проекций.
  Рудник отдельного значения по-прежнему определяется по тексту.
- **Фильтр области источника и неоднозначные источники.** Источники с областью AMBIGUOUS (`VKM_Solikamsk`, кандидат
  `SKRU1_OR_SKRU2_UNATTRIBUTED`) и источники «родственной» области (для SKRU1/SKRU2: `SKRU1_SKRU2_PILLAR`,
  `SKRU1_OR_SKRU2_UNATTRIBUTED`, `SOLIKAMSK_GROUP`; для SKRU3: `SOLIKAMSK_GROUP`) фильтр `source_scope` не включает. Ответ называет их предупреждением `SCOPE_FILTER_EXCLUDED_AMBIGUOUS` (`count`, ID в
  сообщении). Параметр `include_ambiguous: true` (`search_text`, `search_hybrid`, `search_objects`; в API —
  `filters.include_ambiguous`, у `/v1/objects/query` — `include_ambiguous`) делает фильтр «область ИЛИ эти источники»
  во всех каналах (BM25, dense, изображения страниц, библиография) и отвечает `SCOPE_FILTER_INCLUDED_AMBIGUOUS`.
  Область источника наследуется объектами и не устанавливает атрибуцию конкретного значения руднику.
- **Номера объектов в запросе.** «скв. N», «скважина № N», «профильная линия N», «блок N», «панель N», «репер N»,
  «рис./табл. N.N», «формула (N.N)» распознаются (число без слова типа — нет: «75 м», «УДК О-75», «СКРУ-1»). Тема
  запроса без номера ранжирует, номер поднимает внутри первых хитов темы (правило Q5); номер рисунка, таблицы, формулы
  в поиске страниц даёт ветвь RRF `label:PAGE` по объектам с этой печатной меткой; перевод сохраняет номер
  («скважина 75» → «borehole 75»). В ответе `record.stages.object_ref` (`refs`, `topic`, `label_route`, `flagged`,
  `moved`, `promote_k`) и `trace.object_ref` (`matched`: phrase / label, `page_rank_before`, `label_rank`,
  `label_object_id`). Запросы без номера идут прежним путём.

### 1.4 Изменения 13.10.2026: поиск по автору и указатель объектов мира

- **Работы автора: `find_works` (`GET /v1/works?author=…&limit=…&cursor=…`).** Автор ищется по ключу имени реестра
  (`authors.name_key`: фамилия | инициалы | полные имена | письмо; кластер ключа имени, не установленная личность —
  H-15). Метод совпадения: `EXACT_KEY` — та же фамилия в том же письме (регистр, ё/е, порядок «А.А. Барях» не важны);
  `INFLECTED` — падежная форма («Баряха», «Кудряшову», «Лебедевой» → «Барях», «Кудряшов», «Лебедева»); `TRANSLIT` —
  другое письмо или другая латинская запись одной транслитерации («Baryakh», «Barjakh» ↔ «Барях»: общий латинский
  скелет фамилии); `FUZZY` — одна правка скелета, только если ничего другого не нашлось (предупреждение
  `AUTHOR_FUZZY_MATCH`). Инициалы запроса должны согласоваться с кластером. Несколько кластеров одной фамилии
  (однофамильцы, напр. «Лебедева О.О.» и «Лебедева А.М.») возвращаются все, с `homonym_clusters` и предупреждением
  `AUTHOR_HOMONYMS` — реестр не выбирает. Ответ: `item` (`AUTHOR_WORKS`: разбор запроса, кластеры с методом и числом
  работ, `total_works`) и `items` — конверты `WORK`: название, год, авторы как в реестре, `matched_authors` (порядок,
  роль, метод), `match_method`, экземпляры-источники (`sources`, `source_ids`), `work_copy_count`. Порядок: работы,
  где автор первый, затем новее.
- **Маршрут автора в `search_hybrid` (`stages.author_route`, `trace.author`, `trace.author_rank`).** Когда запрос
  называет автора и реестр это подтверждает (`EXACT_KEY` / `INFLECTED` / `TRANSLIT`; признаки: «работы / труды /
  статьи / публикации … X», «X и др.», «X et al.», «X А.А.», «А.А. X», «по X», «papers by X» или фамилия реестра с
  заглавной буквы — кроме эпонимов «закон / критерий / модель / метод … X» и «горные работы»), страницы **его
  собственных работ** (роль в реестре `AUTHOR`, `CORPORATE_AUTHOR` или неизвестна; работы, где он научный
  руководитель, редактор, составитель или переводчик, в маршрут не входят — `stages.author_route.works_other_roles`;
  `find_works` перечисляет их после авторских) идут ветвью RRF `author:PAGE`: с темой (запрос без имени и слов-признаков) — BM25 темы внутри
  источников его работ, и эти страницы поднимаются внутри первых 100 страниц E (порядок сохраняется); без темы
  («работы Баряха») — первые две страницы каждого источника (титульные; сначала работы, где он первый автор), до 20
  из них ставятся первыми. Ссылки на него (библиографический маршрут `bib_route` и его ветвь BIB_ENTRY) остаются,
  но его страницы идут раньше страниц, где его цитируют. Статусы: `APPLIED`, `NO_PAGES`, `NOT_DETECTED` (кандидата нет
  или только слово с заглавной вне реестра), `NO_REGISTRY_MATCH` (признак есть, реестр не подтвердил; `fuzzy_ignored`),
  `UNAVAILABLE` (нет реестра), `OFF`, `SKIPPED_NO_PAGE_KIND`. В кратком виде — `run.stages.author_route` (кроме
  `NOT_DETECTED`) и `author_rank` у хита. Запросы без автора реестра не меняются.
- **Указатель объектов мира: `find_entities` (`GET /v1/entities?kind=&number=&site=&source_id=&page_id=`).** Каталог
  [`catalogues/entities/entity_index.csv`](../../catalogues/entities/entity_index.csv) (сборка
  `scripts/build_entity_index.py`, receipt `catalogues/entities/build_receipt.json`, правило `entity_index_v1`) — из
  PUBLIC-каталогов скважин и отбивок, календарей C4, каталогов наблюдений и мониторинга, геометрии горных работ: одна
  строка на (объект, источник, страница) — вид (`BOREHOLE`, `PROFILE_LINE`, `BENCHMARK`, `BLOCK`, `PANEL`, `SHAFT`),
  ID объекта каталога, подпись как напечатано (цитаты заменены на «…»), номер и его ключ, участок **как в каталоге** с
  его атрибуцией, `source_id`, `page_id` / `page_index` (физическая страница с 1, как `pdf_page` evidence; сверено:
  страницы локаторов совпали с `pdf_page` записей 714/714, 504/504, 563/563), каталог, ID записей, статус, заметка о
  тождестве. Тождество не угадывается: номер 75 двух рудников, двух источников или двух каталогов — разные объекты;
  пары каталога скважин «возможно та же» остаются раздельными и названы в `identity_note`. Пакет каталогов отдаёт
  указатель как таблицу `entity_index` (пересобрать и опубликовать пакет; NAV не нужен). `number` можно писать со словом
  типа («скв. 75» → BOREHOLE 75); без `site` — все участки отдельно (`objects`, предупреждение
  `ENTITY_SEVERAL_OBJECTS`). Конверт `ENTITY_INDEX`, слой PROJECTION (движок `catalogues`, `build_id` = пакет),
  `AUTO_EXTRACTED_UNREVIEWED`: навигация, не evidence.
- **Маршрут указателя в `search_hybrid` (`stages.object_ref.entity_route`).** Распознанные «скв. N», «профильная
  линия N», «блок N», «панель N», «репер N» (§1.3) ищутся в указателе: страницы всех строк этого вида и номера
  (после фильтров запроса, без дублей) — ветвь RRF `entity:PAGE`, а в `trace.object_ref.matched` — `entity` (с
  `entity`: ранг, ID объектов, участки, строки указателя). С темой страницы указателя упорядочены BM25 темы и
  поднимаются вместе со страницами фразы внутри первых 100 страниц E (тема ранжирует); без темы до 20 страниц указателя
  ставятся первыми — сначала страницы, где у объекта больше записей каталога. Так «скважина 75» поднимает
  VKM-SRC-012:p0064 (табл. 3.1 Лебедевой: «Скважина» и «75» в разных ячейках, фраза их не находит). Статусы
  `APPLIED`, `NO_PAGES`, `NOT_IN_INDEX`, `UNAVAILABLE` (пакет без указателя), `OFF`.
- **Выключатели для бенчмарков:** `author_route`, `entity_route` в теле `POST /v1/search/hybrid` (`false` — выкл.,
  `null` — по запросу). Счётчики реестра и указателя — в `/v1/status` (`hybrid.registry_routes`).

### 1.5 Фрагменты одного рисунка; флаги эксперимента поздней стадии (агент Д, 13.10.2026)

- **Фрагменты одного рисунка — одна позиция (по умолчанию для вида FIGURE в `search_text` и `search_hybrid`).**
  Модель разметки дробит рисунок на области (панели «а», «б», значки легенды, подписи), а привязка подписи даёт им один
  `caption_block_id`; в выдаче они равны (одна подпись, один текст единицы) и занимают места подряд. Снимок 2d71e9e8:
  1 897 групп «страница + блок подписи» из 2–11 областей — 4 396 из 18 297 рисунков; в выборке 15 запросов по
  рисункам такие повторы занимали 49 из 196 мест первой двадцатки `search_text` и 67 из 300 — `search_hybrid`. Правило
  (`vkm_corpus.search.fragments`): рисунки одной страницы с общим `caption_block_id` — одна позиция на месте лучшего по
  рангу; представитель — наибольшая область, не мелкая (MODEL_CHOICE: мелкая — площадь < 1500 pt² или меньшая сторона
  < 20 pt; размер неизвестен — не мелкая, но после известных); остальные — `record.fragments` (ID; в кратком виде
  `fragments`), `record.fragment_group` (`caption_block_id`, `members`, `rank_before`, `position_from` — чьё место,
  `tiny`). Рисунки без блока подписи не склеиваются. Гибридный поиск склеивает после G1 и до `max_per_source`
  (`stages.fragments`: `status`, `groups`, `fragments`, `looked_up`; области ключей, которых нет в ветви BM25, берутся
  из индекса рисунков по ID), `search_text` — по своей выдаче рисунков от начала (не меньше 200), окно режется из
  склеенного списка; `totals` по-прежнему считают объекты индекса. `group_fragments: false` (API, MCP) — каждая область
  отдельно. Склейка — представление кандидатов, канон хранит каждую область. Известный случай: на VKM-SRC-014:p0099 сама
  карта (378 × 383 pt) осталась без блока подписи — подпись «Рис. 3.15» привязана к значкам легенды; это ошибка привязки
  в извлечении, поиском не исправляется.
- **Флаги эксперимента поздней стадии** (`search_hybrid`, `retrieval_trace`, `POST /v1/search/hybrid`; по умолчанию
  выключены — запрос без них идёт прежним путём, ключ в ключ; уходят в бэкенд, только когда не по умолчанию; каждая
  формулировка `formulations` получает тот же режим; без поздней стадии — предупреждение `LATE_FLAGS_IGNORED`):
  - `late_fusion`: `"replace"` (умолчание: голову каждого вида упорядочивает late-балл) | `"rrf"` — внутри голов видов
    (H-44) RRF(`rrf_k`) двух порядков: RRF-порядка BM25 + dense (+ маршруты) и late-порядка; кандидат без векторов
    слов — только с первым слагаемым; ничья — RRF-порядок; хвост — как раньше. Трасса: `late_rank`,
    `late_fusion_rank`, `late_fusion_score`; `stages.late.fusion = {mode, k, orders}`, `stages.late.ordering`.
  - `late_query`: `"original"` (умолчание: поздняя стадия оценивает исходный запрос) | `"best_translation"` — когда
    есть перевод (`translate`; `expansions`), каждый перевод оценивается своим вызовом `/search/late` по тем же целям, и
    late-балл кандидата = max нормированного MaxSim (MaxSim / число токенов запроса — суммы по токенам разных запросов
    несравнимы). Трасса: `late_score` (нормированный максимум), `late_query = {best: original | x1 …, scores}`,
    `late_unit` — единица победившего запроса; `stages.late.late_query` (`status` APPLIED / NO_TRANSLATION /
    NOT_NORMALISABLE, `queries` с числом токенов, `best_counts`). Без перевода ничего не меняется (`NO_TRANSLATION`).
  - Краткий вид (§1.1): у хита `late_fusion_rank`, `late_query` (какой запрос дал максимум), `fragments`; в `run.stages`
    — `fragments` (`status`, `groups`) и `late` (`fusion`, `late_query`), только когда они есть.

### 1.6 Перефразировки вызывающего агента (E6, E6b, 13.10.2026)

`formulations` (`search_hybrid`) и `paraphrases` (`reconstruct_topic`) — 1–2 другие формулировки вопроса **на русском,
другими терминами**: английский перевод запроса сервер добавляет сам. Замер ([результаты](../../benchmarks/topic_v1/RESULTS_EXPERIMENTS_2026-10-13.md)
§7, предрегистрации E6 и E6b): две русские формулировки модели дают к одиночному запросу +0,101 R@50 (V) на наборе
названий тем (p_Холм 0,002) и +0,050 на наборе перефразировок (p_Холм 0,076 — порог не пройден) и на обоих наборах лучше
пары «русская + английская» (+0,058 / +0,056); ручные формулировки темы по-прежнему не хуже (−0,04, не значимо).
Автоматического режима на сервере нет (на CORE нет генератора): формулировки пишет вызывающий агент.

## 2. `vkm-corpus` — read-tools

Все tools: `read_only_hint = true`, `destructive_hint = false`.

| Tool | Главные аргументы | Что возвращает | Эндпоинт API |
|---|---|---|---|
| `search_text` | `query`, `kinds` (PAGE, BLOCK, FIGURE, TABLE, FORMULA), фильтры (`source_ids`, `work_ids`, `source_scope`, `source_scope_raw`, `review_status`, `origin`, `language`, `year_from/to`, `available_until` + `unknown_policy`, `quality_flags_none`), `limit`, `cursor` (§1.2), `view` (`brief` по умолчанию, `full`) | `brief` (§1.1): `id`, `rank`, `bm25_score`, сниппет, метка и подпись, карточки источников; `full`: кандидаты с каноническими конвертами, сниппеты индекса (`highlight_origin = SEARCH_INDEX`), `rerank_candidate` | `POST /v1/search` |
| `search_hybrid` | `query`, `kinds` (PAGE, FIGURE, TABLE, FORMULA), фильтры уровня страницы (`source_ids`, `work_ids`, `source_scope`, `year_from/to`, `available_until` + `unknown_policy`), `limit`, `candidates` (кандидатов на стадию, 10–200), `cursor` (§1.2), `view` (`brief` по умолчанию: ранги, `late_score`, сниппет, сводка прогона — §1.1; `full` — всё ниже) | кандидаты BM25 + dense (эмбеддинги единиц `vkm-units-v1`, кодировщик запроса на RX580), слияние RRF (k = 60); затем поздняя стадия — late interaction (MaxSim mLateOn по векторам слов, RX580) над первыми `late_candidates`: на CORE включена по умолчанию (`VKM_HYBRID_LATE_DEFAULT=1` в compose; без переменной код её не запускает), `late = false` — без неё; у каждого `record.trace`: `bm25_rank`, `dense_rank`, `fused_rank`, `rrf_score`, `late_rank`, `late_score`, совпавшие единицы; `item` — сведения о прогоне (сборка векторов, модель, тайминги); без кодировщика или векторного индекса — `DEPENDENCY_UNAVAILABLE`, подмены на BM25 нет; с поздней стадией запрос ищется и на другом языке (словарь терминов NAV: `item.record.translation`, `trace.expansion_ranks`) | `POST /v1/search/hybrid` |
| `retrieval_trace` | `query`, `object_ids` (необязательно), `kinds`, `limit`, `candidates`, `late`, `visual_route` | компактная трасса гибридного ранжирования (без конвертов): ранги и скоры каждой стадии по кандидатам (BM25, dense, RRF, поздняя стадия, когда она идёт, визуальный маршрут), конфигурация стадий; `rerank_rank` — `null` (реранк — отдельный tool) | `POST /v1/search/hybrid` |
| `search_hybrid` → `visual_route` | `null` — решают слова-картинки запроса (рисунок, схема, карта, план, разрез, график, профиль, радарограмма, фото, таблица…), `true` — включить, `false` — выключить | визуальный маршрут (агент VIS): RRF выдачи E и страниц по векторам изображений Qwen3-VL; `route = visual`, в трассе `e_rank`, `vis_rank`, `vis_score`, `visual_rrf_score`, в `stages.visual_route` — статус, признаки, тайминги; работает, когда сервер включил маршрут (`VKM_HYBRID_VISUAL_ROUTE`), иначе статус `DISABLED`, а `true` — `DEPENDENCY_UNAVAILABLE` | `POST /v1/search/hybrid` |
| `search_hybrid` → `graph` | список стадий `collapse`, `cohesion`, `concepts`, `cites`, `topics`; `[]` — без стадий; `null` — умолчание сервера: `collapse` (копии одной работы схлопываются в один результат), остальные стадии — по запросу; `VKM_HYBRID_GRAPH` меняет умолчание без пересборки | графовые стадии над навигационным слоем, модели те же (агент GS, [NAVIGATION_LAYER.md](NAVIGATION_LAYER.md) §11): копии уходят из выдачи и видны у оригинала (`copies`), связный раздел и тема первых страниц поднимают свои страницы, синонимы и аббревиатуры словаря и цитирования дают ветви BM25; первые 10 мест ветви после поздней стадии не трогают; в `stages.graph` — статусы и что сделала каждая стадия, в трассе `graph` (`e_rank`, `legs`, `window_leg`, `copies`); нужна поздняя стадия | `POST /v1/search/hybrid` |
| `search_objects` | `kinds`, `source_ids`, `work_ids`, `page_from/to`, `figure_types`, `review_status`, `origin`, `quality_flags_any/none`, `has_image`, `source_scope*`, `caption_query`, `limit`, `cursor` (§1.2), `view` (`brief` по умолчанию, `full`) | объекты без текстовых тел: `brief` — ID, метка, подпись ≤ 120 (LaTeX формулы), тип рисунка, `has_image`; `full` — с конвертами | `POST /v1/objects/query` |
| `get_source` | `source_id` | строка реестра, `lifecycle_status` (013/022 — 200), связи с Work, сводка обработки | `GET /v1/source/{id}` |
| `get_work` | `work_id` | Work, авторы (`name_as_listed`, NAME_KEY_ONLY), экземпляры-Source, `work_copy_count`, `resolved_work_id` | `GET /v1/work/{id}` |
| `get_page` | `page_id`, `include` (text, blocks, objects), `max_chars` (≤ 60 000), `text_offset` | страница, окно `normalized_text`, объекты страницы | `GET /v1/page/{id}` |
| `get_page_image` | `page_id`, `max_side` (256–1568, по умолчанию 1024), `format` | `ImageContent` + конверт + `pixel_to_page` | `GET /v1/page/{id}/image` |
| `get_figure` | `figure_id`, `include_image` (true), `max_side` | рисунок (bbox, подпись, тип или UNKNOWN_FIGURE_TYPE, векторы) + кроп | `GET /v1/figure/{id}` (+ `/image`) |
| `get_table` | `table_id`, `include_image`, `max_chars` | таблица: сырьё распознавания, сетка, текст, кроп | `GET /v1/table/{id}` |
| `get_formula` | `formula_id`, `include_image` | формула: сырьё, LaTeX, нативные глифы | `GET /v1/formula/{id}` |
| `get_object` | `object_id` (любой ID) | объект с конвертом | `GET /v1/object/{id}` |
| `get_document_neighbors` | `object_id`, `rel_types`, `direction`, `limit` | соседи из графа (проекция), гидратированные из канона | `GET /v1/neighbors/{id}` |
| `get_citations` | `work_id`, `direction` (cites, cited_by, both), `include_unlinked` | записи библиографии (LINKED / CANDIDATE / UNLINKED), цитирующие работы | `GET /v1/citations/{id}` |
| `rerank_text` | `query`, `candidate_ids` (≤ 24), `top_n`, `passages[]` | пересчёт late interaction: MaxSim mLateOn по сохранённым векторам слов единиц кандидата (страница — по своим единицам, блок — через страницу; правило `rerank_text_late_v1`); `passages` действуют только при `VKM_RERANK_TEXT_BACKEND=gateway` (текстовый реранкер EDGE v3.5 выключен 29.09, правило `rerank_text_v1`); блок SERVICE с моделью. Это та же модель mLateOn, что и в поздней стадии `search_hybrid`: порядок выдачи `search_hybrid` с поздней стадией он не уточняет (новой информации нет); полезен для кандидатов `search_text` и собранных из разных вызовов | `POST /v1/rerank/text` |
| `rerank_visual` | `query`, `candidate_ids` (≤ 8: страницы, рисунки, таблицы, артефакты-изображения), `top_n`, `max_side` | ранжирование по изображениям; кандидаты без изображения — в `rejected` | `POST /v1/rerank/visual` |
| `get_processing_status` | один из `source_id`, `page_id`, `run_id`, `job_id` | статусы, последние попытки стадий, ошибки, задания | `GET /v1/processing/status` |
| `trace_document_provenance` | `object_id` | цепочка провенанса + ответы §50 (`interpretability`) | `GET /v1/provenance/{id}` |
| `get_artifact` | `artifact_id` | метаданные артефакта | `GET /v1/artifact/{id}` |
| `list_source_pages` | `source_id`, `from_page`, `to_page`, `limit`, `cursor` | страницы источника | `GET /v1/source/{id}/pages` |
| `get_corpus_status` | — | снимок и счётчики, сборки проекций, модели реранка и лицензии, задания; только роли хостов | `GET /v1/status` |
| `concept_paths` | `term_a`, `term_b` (фраза в любой форме или `TRM-…`), `max_len` (1–6, по умолчанию 4), `limit` (≤ 20), `via` (`concepts`, `formulas`, `sections`, `topics`, `dictionary`) | кратчайшие пути в графе NAV (Neo4j) через термины, символы формул, формулы, разделы, темы и пары словаря RU↔EN (переводы, синонимы, аббревиатуры; у шага — оценка и страницы-примеры), ранжированные по силе связи; у каждого шага — до трёх ID страниц и счётчики источников; термин только из словаря помечен `dictionary_only`; навигация, не причинная цепочка | `GET /v1/nav/graph/paths` |
| `graph_neighbourhood` | `node_id` (ID NAV: `SEC-`, `TRM-`, `FSY-`, `FPR-`, `TOP-`, `TBL-`, `PRM-`, `OCL-`; или любой ID VKM), `depth` (1–2), `limit` (≤ 200) | соседи узла NAV или DOCUMENT, сгруппированные по типу ребра и направлению, с общим числом и сильнейшими соседями (имена, ID страниц): у термина — ещё эквиваленты словаря, таблицы свойства (`TABULATES`) и значения (`VALUE_OF`); у таблицы канона — её сетка и группа повторов; `depth = 2` — соседи соседей | `GET /v1/nav/graph/neighbourhood/{id}` |
| `reconstruct_topic` | `query`, `budget_chars` (1000–60 000, по умолчанию 12 000), `source_ids` (≤ 20), `paraphrases` (≤ 4), `include_json` (по умолчанию `false`) | досье темы «от А до Я» одним вызовом: поиск по формулировкам темы (≤ 5: запрос, перефразы, синонимы и соседи понятия) со слиянием RRF; разделы NAV двумя ярусами — ядро ВКМ (источники каталогов evidence и области ВКМ/СКРУ) и остальное — со страницами, лучшими единицами и сниппетами ≤ 200 знаков; список страниц по ярусам; формулы (номер, «где…», параметры-кандидаты); рисунки и таблицы у найденных страниц; структурированные таблицы свойства, которое тема называет (≤ 6, `find_tables`); понятие и темы; источники с провенансом и CITES; процессы PC-xx каталогов с записями evidence, модели (у каждой `formula_ids` — до 8 привязанных формул корпуса, каталог [model_formula_links](../science/MODEL_FORMULA_LINKS_RU.md)), конфликты, причинные связи; пробелы UNKNOWN. Текст ответа — markdown-оглавление в пределах бюджета (не JSON), `structured_content` — короткая сводка (бюджет, что урезано, счётчики частей; §1.1), с `include_json = true` — полный `ApiResponse` (`TOPIC_DOSSIER`). Навигация, не evidence ([NAVIGATION_LAYER.md](NAVIGATION_LAYER.md) §5) | `GET/POST /v1/topic` |
| `find_topics` | `terms` (1–5 фраз), `limit` (≤ 50), `level` (1–3) | темы поперёк книг (дерево тем без LLM), совпадающие со всеми фразами по леммам и SAME_AS |
| `get_topic` | `topic_id` (`TOP-…`) | путь к корню, дети, разделы-члены с источниками и страницами, центральные разделы, соседние темы |
| `similar_sections` | `section_id`, `k` (≤ 50), `other_sources_only` | разделы других книг, ближайшие по смыслу (косинус векторов разделов) |
| `section_topics` | `section_id` | темы раздела на всех уровнях |
| `copies_of` | `ref` (единица `u1-…`, страница или блок), `limit` | где тот же текст есть в других источниках: перепечатки, копии, общие аннотации; первичный — подсказка, не авторство |
| `source_overlap` | `source_id`, `limit` | источники, повторяющие текст данного: общие фрагменты, доли, кто раньше |
| `find_parameters` | `property`, `material`, `site`, `scale` (LAB/MASSIF/NORMATIVE/MODEL/UNKNOWN), `source_id`, `limit` | кандидаты значений параметров из таблиц и текста с источником, страницей, единицей (как напечатано и СИ), материалом, масштабом; не evidence и не рекомендуемое значение |
| `parameter_summary` | `property`, `material` | по материалу и масштабу: число кандидатов, источников, работ (`n_works`, `work_ids`, `copies`; §1.3), страниц и диапазон в СИ — карта, не значение |
| `translate_term` | `term` (фраза в любой форме, аббревиатура или `TRM-…`), `target` (ru, en, de), `limit` | эквиваленты термина на других языках корпуса, его синонимы и аббревиатуры из словаря NAV `term_translations`: методы, оценка (ожидаемая точность), страницы-примеры, статус (`AUTO_EXTRACTED_UNREVIEWED`, строки сидов — `REVIEWED_BY_AGENT`); фраза без своей пары переводится по частям — навигация, не evidence ([NAVIGATION_LAYER.md](NAVIGATION_LAYER.md) §4) | `GET /v1/nav/translate` |
| `get_table_structured` | `table_id` (ID таблицы канона `VKM-SRC-NNN:pNNNN:t…` или `TBL-…`), `max_rows` (1–500, по умолчанию 200), `max_chars` (200–60 000, 8 000) | таблица как сетка (NAV §10): номер и подпись, размер, строки шапки и способ их поиска, полосы и блоки, ориентация, уверенность и флаги; колонки (путь шапки, символ, единица и откуда она, роль, свойство словаря параметров); строки с ролями и ячейками (текст как напечатан, разобранное значение, единица, флаги вроде `DECIMAL_POINT_SUSPECT`) и markdown. Значения не исправляются — сверять со страницей. Таблица без сетки — NOT_FOUND (`get_table` показывает таблицу канона) | `GET /v1/nav/table/{id}` |
| `find_tables` | `property` («модуль деформации», «σсж», «ucs»), `material` («каменная соль», «соляные породы»), `source_id`, `text` (слова подписи, шапки, меток строк), `limit` (≤ 100) — хотя бы один | структурированные таблицы, где колонка значений, строка значений или подпись называет свойство, шапка или подпись — материал; все фильтры вместе; сначала таблицы со свойством в колонках. У таблицы: ID канона и `TBL-`, источник, страница, раздел, номер, подпись, размер, ориентация, уверенность, флаги и совпавшие колонки с единицами; как разобран запрос (`query`, `unresolved`) | `GET /v1/nav/tables` |
| `copies_of_object` | `object_id` (рисунок `…:f…`, таблица `…:t…` или формула `…:m…`), `limit` (≤ 200) | где ещё в корпусе есть этот рисунок, таблица или формула (NAV §9): группы повторов (копия той же работы, перепечатка, повторно использованный рисунок или таблица, общая формула, перерисованный рисунок, шаблон), первичная копия и правило её выбора (самый ранний год — подсказка для порядка, не авторство), остальные копии с доказательством (расстояние изображений, похожесть подписи, вхождение ячеек, совпадение формулы), источники и страницы; без повторов — пустой список | `GET /v1/nav/object_copies/{id}` |
| `shared_formulas` | `ref` (ID формулы или строка LaTeX, ≤ 2 000 знаков), `renamed` (та же структура в других обозначениях, только для выразительных формул; по умолчанию да), `limit` (≤ 200) | где записана та же формула: работы от ранней к поздней с источниками, страницами, напечатанными номерами и разделами — с тем же каноническим видом (LaTeX нормализован: греческие варианты, шрифты, пробелы, десятичная запятая) и, с `renamed`, с той же структурой; тривиальные записи не группируются; группы повторов этих формул. Та же запись, не проверенный закон. Формула без ключа — `reason: NO_FORMULA_KEY` | `GET /v1/nav/shared_formulas` |
| `find_figure_series` | `text` (слова подписи, названий осей или подписей рядов в любой форме), `unit` (единица на любой оси: мм, сут, МПа; mm ↔ мм), `source_id`, `time_series` (только ряды во времени), `calibrated_only` (по умолчанию: обе оси откалиброваны), `clean_only` (без рядов с подозрительной калибровкой — для использования значений), `include_raster` (добавить помеченный растровый набор, маршрут R), `limit` (≤ 50 рисунков) | рисунки-графики с оцифрованными рядами: страница, подпись, оси (название и единица как напечатаны, вид шкалы, способ калибровки, rms в pt), статус рисунка, флаги качества; по ряду — подпись, число точек, диапазоны, медианные погрешности, точки вне рамки графика, признак `suspect`. Значения — DERIVATION из публикации (`AUTO_EXTRACTED_UNREVIEWED`, доступны с даты публикации), не наблюдения и не evidence; модель и измерение не различаются; участок — как у источника ([FIGURE_SERIES.md](FIGURE_SERIES.md)) | `GET /v1/nav/figure_series` |
| `get_figure_series` | `ref` (ряд `FS-…` или ID рисунка — все его ряды), `max_points` (≤ 5000, по умолчанию 1000) | точки ряда или всех рядов рисунка: x и y в единицах величины с полушириной погрешности, положение на странице (pt) и `in_plot_area`; калибровка каждой оси (подписи засечек, остатки, отброшенные подписи, OCR при глифовых подписях), провенанс (sha256 PDF, версия оцифровщика, хеш конфигурации); рисунок без калибровки — со статусом и без выдуманных значений | `GET /v1/nav/figure_series/{ref}` |
| `find_works` | `author` (фамилия в любом падеже или письме, с инициалами или без), `limit` (≤ 200), `cursor` | работы реестра автора по ключу имени (`EXACT_KEY` / `INFLECTED` / `TRANSLIT` / `FUZZY`; однофамильцы — все, помечены): название, год, авторы как в реестре, совпавшие авторы с методом, экземпляры-источники и их число (§1.4) | `GET /v1/works` |
| `find_entities` | `kind` (BOREHOLE, PROFILE_LINE/LINE, BENCHMARK, BLOCK, PANEL, SHAFT), `number` («75», «скв. 75», «9 ВП»), `site`, `source_id`, `page_id`, `limit` (≤ 500), `cursor` — хотя бы один | строки указателя объектов мира: где объект каталога упомянут (источник, страница), подпись, номер, участок как в каталоге с атрибуцией, каталог, ID записей, статус, `identity_note`; `objects` — разные объекты одного номера отдельно; навигация, не evidence (§1.4) | `GET /v1/entities` |

Гибридный поиск (этап 2 лаборатории retrieval, §52–55 постановки): ключи слияния — стабильные ID канона: для `PAGE`
все единицы страницы засчитываются странице (первое вхождение; дубли страниц сворачиваются по `dup_group_id`, как в
BM25), для рисунков, таблиц и формул — ID объекта. Фильтры по полям типа объекта (`figure_type`, `text_layer`) в
гибридном поиске не поддерживаются (400). Ранги BM25 и dense считаются отдельно по каждому виду, сырые скоры видов не
сравниваются. Поздняя стадия (late interaction, MaxSim mLateOn на RX580) на CORE включена по умолчанию
(`VKM_HYBRID_LATE_DEFAULT=1`): она переупорядочивает первые `late_candidates` результатов RRF, в трассе `late_rank`,
`late_score`, `late_unit`; `late = false` — выдача RRF. `rerank_text` считает ту же модель по тем же векторам, поэтому
после `search_hybrid` с поздней стадией он порядок не уточняет; он нужен для выдачи `search_text` (`rerank_candidate`;
для страницы, найденной только dense, пассаж — блоки совпавшей единицы) и для кандидатов из разных вызовов.

Лимиты (H-13, H-45): текстовый реранк — не больше 24 кандидатов за вызов (больше — 413, пачки не склеиваются);
визуальный — не больше 8 изображений, таймаут клиента 330 с (реранкер обрабатывает изображение секунды);
изображения — длинная сторона по умолчанию 1024 px, PNG при размере до 1,5 МБ, иначе JPEG q85.

## 3. `vkm-corpus-admin` — переобработка plan-first (H-12)

| Tool | Аннотации | Аргументы | Результат |
|---|---|---|---|
| `reprocess_source` | write, не destructive, idempotent | `source_id`, `reason` (≥ 10 символов), `force`, `recall_model`, `no_ocr`, `job_id`, `plan_sha256` | задание control plane |
| `reprocess_page` | то же | `page_id`, … | то же |
| `get_job` | read-only | `job_id` | состояние, план (`vkm.ops_plan/1`) и `plan_sha256` |
| `cancel_job` | write, destructive | `job_id`, `reason` | задание `CANCELLED` (только до запуска: `PLAN_REQUESTED`, `PLANNED`, `CONFIRMED`) |

Порядок: (1) вызов без `job_id` ставит задание `PLAN_REQUESTED`; (2) воркер (единственный планировщик) публикует план —
`get_job` показывает `PLANNED` и `plan_sha256`; (3) человек проверяет план, повторный вызов с `job_id` и `plan_sha256`
подтверждает именно этот план (`CONFIRMED`); воркер перед запуском пересчитывает план и отказывается при другом хеше.
Отказ человека — `cancel_job`. Опции — ровно те, что исполняет воркер v0 (`request.options`): `force` (пересчёт строк
из кешей, модели не вызываются, H-05), `recall_model` (повторный вызов моделей — время GPU), `no_ocr`; выбора стадий в
v0 нет. Повтор запроса по той же цели с теми же опциями возвращает активное задание (`deduplicated`), с другими —
`JOB_STATE_CONFLICT` (сначала `cancel_job`); источники вне жизненного цикла ACTIVE (013, 022) — `NOT_REPROCESSABLE`;
активных заданий не больше 20 для страниц и 3 для источников (`RATE_LIMITED`). Канон API и MCP не пишут никогда.

## 4. `vkm-cad` — мост Autodesk (WORKSTATION)

| Tool | Класс | Что делает | Ошибки |
|---|---|---|---|
| `cad_status` | read | установленные AutoCAD/Civil 3D и версии (реестр и файлы, без запуска AutoCAD и без COM), запущенные процессы, .NET/COM, возможности моста, scratch-корень | — |
| `cad_list_open_documents` | read | открытые документы — только attach к запущенному пользователем AutoCAD (`GetActiveObject`) | `CAD_UNAVAILABLE`, `CAD_NOT_RUNNING`, `CAD_BUSY`, `CAD_TIMEOUT` |
| `cad_get_layers` | read | слои документа | + `CAD_DOCUMENT_NOT_FOUND` |
| `cad_get_extents` | read | EXTMIN/EXTMAX, INSUNITS, MEASUREMENT | то же |
| `cad_list_entities` | read | объекты пространства модели (≤ 1000 за вызов, курсор) | то же |
| `cad_get_coordinate_system` | read | код системы координат чертежа (CGEOCS) как есть; `crs_status = UNKNOWN_CRS`, EPSG не выводится | то же |
| `cad_create_scratch_document` | scratch | пустой DXF (ezdxf) | `EZDXF_UNAVAILABLE`, `SCRATCH_UNAVAILABLE` |
| `cad_import_pdf_vector` | scratch | артефакт `VECTOR_PATHS_JSON` (`vkm.vector_paths/1`) → DXF: 1 единица = 1 pt, ось Y перевёрнута | `NO_VECTOR_ARTIFACT`, `ARTIFACT_HASH_MISMATCH`, `CRS_STATUS_NOT_ALLOWED` |
| `cad_extract_geometry` | scratch | геометрия DXF → `out/geometry.jsonl` + сводка | `SCRATCH_DOC_NOT_FOUND`, `FORMAT_NOT_SUPPORTED` |
| `cad_export_dxf` | scratch | DXF нужной версии (R2000…R2018) с фиксированными датами | `WOULD_OVERWRITE` |
| `cad_save_copy` | scratch | байтовая копия **сохранённого** файла открытого документа в scratch (sha до и после; несохранённые правки отмечаются) | `CAD_*` |

Правила v0 (H-20): DXF моста — `$INSUNITS = 0` и XDATA `VKM_UNITS=PAGE_PT`, фиксированные даты и GUID заголовка,
`coordinate_space = DRAWING_UNITS`, `crs_status = UNKNOWN_CRS` (`SCHEMATIC` — только с обоснованием, записывается как
MODEL_CHOICE), `epsg = null`, `review_status = AUTO_EXTRACTED_UNREVIEWED`; CAD-выходы никогда не вход извлечения.
Read-инструменты не вызывают команды и не сохраняют документы пользователя. Scratch-корень — `VKM_CAD_SCRATCH` или
`$VKM_WORK/cad_scratch` (не в PRIVATE, не в каноне, в PUBLIC — только в git-ignored `work/`). Векторный артефакт ищется в
`$VKM_DATA_ROOT/artifacts`, в `inbox/` scratch-корня и через VKM API.

### 4.1 v1: задания AutoCAD / Civil 3D без GUI (решения — [AGENT_CAD_V1.md](../implementation_work/AGENT_CAD_V1.md))

| Tool | Класс | Что делает | Канал |
|---|---|---|---|
| `cad_capabilities` | read | продукты, каналы (Core Console, .NET, скрытый экземпляр, резерв), виды `cad_exec`, охват API Civil 3D, проверенные команды | — |
| `cad_job_create` / `cad_job_status` / `cad_job_list` | job / read / read | каталог задания `$VKM_WORK/cad_jobs/<job_id>`, квитанция, прогоны, выходы | — |
| `cad_exec` | **code** | `scr` (командные строки), `lisp` (значение последнего выражения; в `scr`/`lisp` доступны `(vkm:result v)` и `(vkm:fail "причина")`), `csharp` (операторы C# с `ctx.Doc/Db/Ed/Tr` и `civil`, `return` — результат), `python_com` (скрытый полный AutoCAD: `app`, `doc`, `civil()`) | Core Console; `python_com` — скрытый экземпляр |
| `cad_query` | **code** | выражение LISP или C# на чертеже задания без сохранения | Core Console |
| `cad_draw` | job | чертёж из JSON-спецификации (слои, точки, полилинии 2D/3D, окружности, дуги, тексты, штриховки, блоки с атрибутами, размеры) → DXF, по желанию DWG и чертёж задания | ezdxf (+ Core Console) |
| `cad_convert` | job | DXF ↔ DWG (SAVEAS 2018 / DXFOUT 2000…2018); источник — чертёж задания, файл задания или scratch-документ `scratch:CADS-…/doc.dxf` (векторы PDF корпуса из `cad_import_pdf_vector` → DWG) | Core Console |
| `c3d_points_from_table` | job | таблица (CSV/TSV, `;` и десятичная запятая, JSON, Parquet или строки) → COGO-точки + группа; копии DXF/JSON; `name_policy` | .NET (Civil 3D) / резерв |
| `c3d_tin_surface` | job | TIN из группы или точек, структурные линии, внешняя граница, макс. ребро; статистика, DXF треугольников | .NET / резерв |
| `c3d_contours` | job | горизонтали кратно интервалу, основные — на отдельном слое | .NET / резерв |
| `c3d_difference_surface` | job | мульда как разность: поверхность объёмов (выемка/насыпь) + dz-TIN и изолинии dz | .NET / резерв |
| `c3d_alignment_profile` | job | трасса по полилинии (линия наблюдений), профили поверхностей с шагом, CSV + DXF, вид профиля | .NET / резерв |
| `cad_layout_sheet` | job | лист A4…A0 в заданиях ACAD и C3D: `DWG To PDF.pc3` (параметры листа задаются до первой активации), рамка 20/5/5/5 мм, видовой экран 1:N (или вписать) — тот, что AutoCAD создаёт при активации листа, основная надпись по ГОСТ 2.104 (упрощённая; длинный текст ужимается по ширине своей ячейки) | .NET |
| `cad_plot_pdf` | job | печать листов (их параметры) или `Model` (границы, вписать) в PDF | Core Console `-PLOT` |
| `cad_pdf_import` | job | страницы PDF → новые чертежи `-PDFIMPORT` (DWG + DXF + сводка геометрии) | Core Console |

Правила v1:

- только новые документы в каталоге задания; документы пользователя не открываются. Чертёж задания открывается из
  копии в `runs/Rxxx/` и заменяется только при маркере `END … OK`; входы копируются с SHA-256 до и после;
- один процесс AutoCAD на все задания (`_engine.lock`); сеть мостом не используется; `/isolate` — профиль моста
  отдельно от профиля пользователя; каждый прогон пишет аудит побочных эффектов (реестр AutoCAD, файлы профилей);
- авария (ненулевой код выхода, процесс-репортёр), окно (диалог) или таймаут → дерево процессов задания убивается,
  ошибка `CAD_ENGINE_CRASHED | CAD_DIALOG_BLOCKED | CAD_RUN_TIMEOUT`; отчёты об авариях не отправляются. В дерево
  входят только процессы, созданные после консоли (по времени создания; перед завершением оно сверяется ещё раз):
  Windows повторно выдаёт PID, и 29.09 чужой процесс с «родительским» PID консоли (Discord пользователя) был принят за
  процесс задания и закрыт — исправлено;
- в консоли AutoCAD без Civil 3D (`/product ACAD`) `Viewport.On = true` в транзакции, где удалён другой видовой экран
  того же листа, роняет процесс (`AccessViolationException` в `AcDbViewport::setIsOn`); консоль Civil 3D такой порядок
  переживает, поэтому smoke v1 в задании C3D проходил. Мост так больше не делает (CADFIX 29.09,
  [квитанция](receipts/cad_layout_fix.json)); в `cad_exec csharp` сначала зафиксируйте удаление или возьмите видовой
  экран, созданный AutoCAD при активации листа (`cad_capabilities` → `headless_pitfalls`);
- в `scr` пустая строка в приглашении повторяет последнюю команду; команду, оставленную в ожидании ввода, эпилог
  (LISP) обычно отменяет (проверено на `_.LINE`); если команда приняла эпилог как текст, маркера `END` нет →
  `CAD_SCRIPT_FAILED`, чертёж не меняется;
- `cad_exec` и `cad_query` исполняют произвольный код с правами пользователя (`destructive_hint = true`) — это
  ограничители, а не песочница; `python_com` включается только `VKM_CAD_ALLOW_HIDDEN_INSTANCE=1` (полный `acad.exe`
  меняет профиль пользователя) и отклоняется, пока запущен AutoCAD пользователя. Решение CP-43 (29.09): работаем через
  консоль, скрытый экземпляр выключен до отдельного решения пользователя;
- выходы — DERIVED: TIN — INTERPOLATION, остальное — DERIVATION, со списком MODEL_CHOICE; `crs_status = UNKNOWN_CRS`,
  кроме явного преобразования (`offset | helmert2d | affine2d` с основанием ≥ 10 символов → `EXPLICIT_TRANSFORM`),
  `epsg = null`, `review_status = AUTO_EXTRACTED_UNREVIEWED`, никогда не вход извлечения; неизвестная отметка точки не
  заменяется числом;
- `engine = auto` выбирает Civil 3D (задание C3D, Civil 3D и компилятор C# на месте), иначе резервный путь на Python
  (`PURE_PYTHON_FALLBACK`, scipy); поверхность обрабатывается тем движком, которым построена.

Ошибки v1: `JOBS_UNAVAILABLE`, `CAD_JOB_NOT_FOUND`, `CAD_ENGINE_UNAVAILABLE`, `CAD_ENGINE_BUSY` (повторить позже),
`CAD_RUN_TIMEOUT`, `CAD_ENGINE_CRASHED`, `CAD_DIALOG_BLOCKED`, `CAD_SCRIPT_NOT_READ`, `CAD_SCRIPT_FAILED`,
`HOST_OP_FAILED`, `CIVIL3D_UNAVAILABLE`, `DOTNET_UNAVAILABLE`, `DOTNET_COMPILE_FAILED`, `HIDDEN_INSTANCE_NOT_ALLOWED`,
`USER_SESSION_RUNNING`, `FALLBACK_UNAVAILABLE`, `INPUT_NOT_FOUND`, `TABLE_FORMAT_ERROR`, `SURFACE_NOT_FOUND`.
У неудачного прогона .NET-хоста — в том числе при аварии, когда `result.json` нет, — в деталях ошибки и в записи прогона
есть `host_trace_tail`: последние этапы хоста (`op …`, `acad.layout_sheet: …`), только имена, без путей.

## 5. `vkm-drawio` — схемы draw.io (WORKSTATION)

| Tool | Класс | Что делает | Нужен draw.io Desktop |
|---|---|---|---|
| `drawio_status` | read | найден ли draw.io (Store-пакет, `VKM_DRAWIO_EXE`, Program Files, PATH), версия, корни | нет |
| `drawio_create_diagram` | write | структурированная спецификация (страницы → узлы, рёбра) → детерминированный несжатый `.drawio`; раскладка: явные координаты, `grid` или `drawio:<preset>` (CLI + канонизация) | только для `drawio:*` |
| `drawio_read_diagram` | read | `.drawio` (обычный или сжатый), `.svg`/`.png` со встроенной моделью | нет |
| `drawio_update_diagram` | write | операции `add/update/remove_node`, `add/update/remove_edge`, `add/rename/remove_page`, `canonicalize`; требует `expected_sha256` | нет |
| `drawio_export` | write | png, svg, jpg; pdf — только в корень `work` | да |
| `drawio_render_preview` | read | PNG-превью страницы как `ImageContent` (ничего не пишет) | да |
| `drawio_open` | GUI | открывает схему в окне draw.io (отдельный процесс, без ожидания) | да |
| `drawio_list_diagrams` | read | список схем корня | нет |

Корни записи: `public` = `docs/diagrams/` (коммитится; политика утечки: без машинных путей, частных адресов, секретов и
встроенных растров) и `work` = `$VKM_WORK/diagrams`. Пути — относительные, без `..`, дисков, UNC, `:` и ссылок
(junction/symlink); перезапись — только с `overwrite = true`. Примеры в `docs/diagrams/` пересобираются из
`docs/diagrams/specs/*.spec.json` командой `python -m vkm_drawio.cli create --root public --path <имя>.drawio --spec
docs/diagrams/specs/<имя>.spec.json --overwrite` (тест сверяет байты).

## 6. Подключение к Claude Code

`.mcp.json` в репозиторий не коммитится (DN-G7): скопируйте шаблон в неотслеживаемый `.mcp.json` в корне клона или
добавьте серверы командой `claude mcp add -s local`. Значения — только переменные окружения пользователя (URL, токены,
рабочие каталоги); в шаблоне нет путей и секретов. Claude Code подставляет `${VAR}` и `${VAR:-default}`; незаданную
переменную серверы `vkm-cad` и `vkm-drawio` считают незаданной.

```json
{
  "mcpServers": {
    "vkm-corpus": {
      "type": "http",
      "url": "${VKM_MCP_URL}",
      "headers": {"Authorization": "Bearer ${VKM_MCP_TOKEN}"}
    },
    "vkm-cad": {
      "type": "stdio",
      "command": "${VKM_PYTHON:-python}",
      "args": ["-m", "vkm_cad.mcp_server"],
      "env": {"PYTHONUTF8": "1", "VKM_WORK": "${VKM_WORK}", "VKM_CAD_SCRATCH": "${VKM_CAD_SCRATCH:-}",
              "VKM_CAD_JOBS": "${VKM_CAD_JOBS:-}",
              "VKM_CAD_ALLOW_HIDDEN_INSTANCE": "${VKM_CAD_ALLOW_HIDDEN_INSTANCE:-}",
              "VKM_API_URL": "${VKM_API_URL:-}", "VKM_API_TOKEN": "${VKM_API_TOKEN:-}"}
    },
    "vkm-drawio": {
      "type": "stdio",
      "command": "${VKM_PYTHON:-python}",
      "args": ["-m", "vkm_drawio.mcp_server"],
      "env": {"PYTHONUTF8": "1", "VKM_WORK": "${VKM_WORK}", "VKM_DRAWIO_EXE": "${VKM_DRAWIO_EXE:-}"}
    }
  }
}
```

Администрирование подключается отдельно и только на время работы (каждый вызов подтверждает человек; tools
`mcp__vkm-corpus-admin__*` не добавлять в allow-списки):

```json
{"mcpServers": {"vkm-corpus-admin": {"type": "http", "url": "${VKM_MCP_ADMIN_URL}",
                                     "headers": {"Authorization": "Bearer ${VKM_MCP_ADMIN_TOKEN}"}}}}
```

`VKM_PYTHON` — интерпретатор окружения, где пакет установлен (`pip install -e ".[desktop]"` для мостов,
`".[corpus-services]"` для скриптового клиента). Stdio-серверы пишут логи в stderr и в `$VKM_WORK/logs/*.jsonl`; stdout —
только протокол (тест).

Переменные `vkm-cad` v1 (все необязательны): `VKM_CAD_JOBS` — корень заданий (по умолчанию `$VKM_WORK/cad_jobs`);
`VKM_CAD_ALLOW_HIDDEN_INSTANCE=1` — разрешить `python_com` (решение пользователя, §4.1); `VKM_ACAD_INSTALL_DIR` —
каталог AutoCAD, если реестр не подходит; `VKM_CSC` — путь к `csc.exe`/`csc.dll`; `VKM_CAD_AUDIT=0` — отключить аудит
побочных эффектов. Резервному пути нужен scipy (`pip install scipy`; проверено 1.18.1).

## 7. Развёртывание на CORE

Образ `infra/core/api/Dockerfile` (python:3.13-slim по digest, без прав root, секретов нет) используют четыре сервиса
compose-проекта `vkm-core` (`infra/core/compose.yml`): `api` (`vkm-corpus api serve`), `mcp` (`mcp serve --kind
read`), `mcp-admin` (`--kind admin`, профиль `admin`) и разовый `vkm-job` (профиль `jobs`: `canon init --kind
CANONICAL`, `core reconcile --run-id <RUN>`). Все они работают от владельца канонического корня
(`VKM_DATA_UID:VKM_DATA_GID` из host-local `.env`), с корневой ФС только для чтения и tmpfs `/tmp`. `api` монтирует
корень `:ro` (маркер, `canonical/`, `artifacts/`, `duckdb/`), пишет в корень только `vkm-job`. API отказывается
работать на корне STAGING (H-07) и в `/v1/status` сверяет снимок DuckDB с `CURRENT` (H-48). Токены — compose secrets
через `*_FILE`: `VKM_API_TOKEN_FILE` (чтение), `VKM_API_WRITE_TOKEN_FILE` (переобработка), `VKM_MCP_TOKEN_FILE`,
`VKM_MCP_ADMIN_TOKEN_FILE`, `VKM_NEO4J_PASSWORD_FILE` (отдельный файл `neo4j_password`, не `neo4j_auth` самого Neo4j);
файлы секретов принадлежат `VKM_DATA_UID`, режим 0400. Сервер `mcp` получает только токен чтения API. Разрешённые
значения `Host` — `VKM_MCP_ALLOWED_HOSTS` (чужой `Host` → 421; пусто — только loopback). Шаблон переменных —
`infra/core/.env.example`.

## 8. Приёмка §60

Сценарий: `search_text` → кандидаты-страницы → `rerank_text` → `get_page` → рисунки → `rerank_visual` → `get_object` →
`trace_document_provenance`.

1. Скриптовый клиент (gate, детерминированный): `python -m vkm_corpus.mcp.acceptance --url "$VKM_MCP_URL" --out
   receipt.json` (токен — из `VKM_MCP_TOKEN[_FILE]`). Проверяет каждый шаг, поля конвертов и ответы провенанса,
   пишет JSON-receipt без текста документов. Режим `--dry-run` прогоняет ту же цепочку на синтетическом каноне внутри
   процесса.
2. Прогон агентом (только с согласия пользователя — расходует план): `claude -p` с `--mcp-config <файл с одним
   vkm-corpus> --strict-mcp-config --tools "" --allowedTools "mcp__vkm-corpus" --permission-mode dontAsk --output-format
   stream-json`; вердикт — по аудиту транскрипта: все `tool_use` — `mcp__vkm-corpus__*`, цепочка в нужном порядке,
   ответы `rerank_visual` ссылаются на артефакты-изображения, итог называет `object_id` с `processing_run_id` и
   `review_status` и не выдаёт автоматические объекты за факты.

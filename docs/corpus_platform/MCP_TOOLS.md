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
`is_error = not ok`. Тело:

| Поле | Смысл |
|---|---|
| `ok` | `true` / `false` |
| `meta` | `request_id` (он же `log_ref` в логах), `api_version`, `canonical_snapshot_id`, `elapsed_ms`, `warnings[]` |
| `item` / `items[]` | объект(ы): `{envelope, record}`; `record` — канонические поля без дублей конверта |
| `next_cursor` | курсор следующей страницы результата |
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

## 2. `vkm-corpus` — read-tools

Все tools: `read_only_hint = true`, `destructive_hint = false`.

| Tool | Главные аргументы | Что возвращает | Эндпоинт API |
|---|---|---|---|
| `search_text` | `query`, `kinds` (PAGE, BLOCK, FIGURE, TABLE, FORMULA), фильтры (`source_ids`, `work_ids`, `source_scope`, `source_scope_raw`, `review_status`, `origin`, `language`, `year_from/to`, `available_until` + `unknown_policy`, `quality_flags_none`), `limit`, `cursor` | кандидаты с каноническими конвертами, сниппеты индекса (`highlight_origin = SEARCH_INDEX`), `rerank_candidate` | `POST /v1/search` |
| `search_hybrid` | `query`, `kinds` (PAGE, FIGURE, TABLE, FORMULA), фильтры уровня страницы (`source_ids`, `work_ids`, `source_scope`, `year_from/to`, `available_until` + `unknown_policy`), `limit`, `candidates` (кандидатов на стадию, 10–200), `cursor` | кандидаты BM25 + dense (эмбеддинги единиц `vkm-units-v1`, кодировщик запроса на RX580), слияние RRF (k = 60); у каждого `record.trace`: `bm25_rank`, `dense_rank`, `fused_rank`, `rrf_score`, совпавшая единица; `item` — сведения о прогоне (сборка векторов, модель, тайминги); без кодировщика или векторного индекса — `DEPENDENCY_UNAVAILABLE`, подмены на BM25 нет; с поздней стадией запрос ищется и на другом языке (словарь терминов NAV: `item.record.translation`, `trace.expansion_ranks`) | `POST /v1/search/hybrid` |
| `retrieval_trace` | `query`, `object_ids` (необязательно), `kinds`, `limit`, `candidates`, `late`, `visual_route` | компактная трасса гибридного ранжирования: ранги и скоры каждой стадии по кандидатам, конфигурация стадий; late interaction и реранк — следующие стадии (`null`) | `POST /v1/search/hybrid` |
| `search_hybrid` → `visual_route` | `null` — решают слова-картинки запроса (рисунок, схема, карта, план, разрез, график, профиль, радарограмма, фото, таблица…), `true` — включить, `false` — выключить | визуальный маршрут (агент VIS): RRF выдачи E и страниц по векторам изображений Qwen3-VL; `route = visual`, в трассе `e_rank`, `vis_rank`, `vis_score`, `visual_rrf_score`, в `stages.visual_route` — статус, признаки, тайминги; работает, когда сервер включил маршрут (`VKM_HYBRID_VISUAL_ROUTE`), иначе статус `DISABLED`, а `true` — `DEPENDENCY_UNAVAILABLE` | `POST /v1/search/hybrid` |
| `search_hybrid` → `graph` | список стадий `collapse`, `cohesion`, `concepts`, `cites`, `topics`; `[]` — без стадий; `null` — умолчание сервера (по GRAPH_SEARCH_V1 — все пять, `VKM_HYBRID_GRAPH` меняет без пересборки) | графовые стадии над навигационным слоем, модели те же (агент GS, [NAVIGATION_LAYER.md](NAVIGATION_LAYER.md) §11): копии уходят из выдачи и видны у оригинала (`copies`), связный раздел и тема первых страниц поднимают свои страницы, синонимы и аббревиатуры словаря и цитирования дают ветви BM25; первые 10 мест ветви после поздней стадии не трогают; в `stages.graph` — статусы и что сделала каждая стадия, в трассе `graph` (`e_rank`, `legs`, `window_leg`, `copies`); нужна поздняя стадия | `POST /v1/search/hybrid` |
| `search_objects` | `kinds`, `source_ids`, `work_ids`, `page_from/to`, `figure_types`, `review_status`, `origin`, `quality_flags_any/none`, `has_image`, `source_scope*`, `caption_query` | объекты без текстовых тел | `POST /v1/objects/query` |
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
| `rerank_text` | `query`, `candidate_ids` (≤ 24), `top_n`, `passages[]` | пересчёт late interaction: MaxSim mLateOn по сохранённым векторам слов единиц кандидата (страница — по своим единицам, блок — через страницу; правило `rerank_text_late_v1`); `passages` действуют только при `VKM_RERANK_TEXT_BACKEND=gateway` (текстовый реранкер EDGE v3.5 выключен 29.09, правило `rerank_text_v1`); блок SERVICE с моделью | `POST /v1/rerank/text` |
| `rerank_visual` | `query`, `candidate_ids` (≤ 8: страницы, рисунки, таблицы, артефакты-изображения), `top_n`, `max_side` | ранжирование по изображениям; кандидаты без изображения — в `rejected` | `POST /v1/rerank/visual` |
| `get_processing_status` | один из `source_id`, `page_id`, `run_id`, `job_id` | статусы, последние попытки стадий, ошибки, задания | `GET /v1/processing/status` |
| `trace_document_provenance` | `object_id` | цепочка провенанса + ответы §50 (`interpretability`) | `GET /v1/provenance/{id}` |
| `get_artifact` | `artifact_id` | метаданные артефакта | `GET /v1/artifact/{id}` |
| `list_source_pages` | `source_id`, `from_page`, `to_page`, `limit`, `cursor` | страницы источника | `GET /v1/source/{id}/pages` |
| `get_corpus_status` | — | снимок и счётчики, сборки проекций, модели реранка и лицензии, задания; только роли хостов | `GET /v1/status` |
| `concept_paths` | `term_a`, `term_b` (фраза в любой форме или `TRM-…`), `max_len` (1–6, по умолчанию 4), `limit` (≤ 20), `via` (`concepts`, `formulas`, `sections`, `topics`, `dictionary`) | кратчайшие пути в графе NAV (Neo4j) через термины, символы формул, формулы, разделы, темы и пары словаря RU↔EN (переводы, синонимы, аббревиатуры; у шага — оценка и страницы-примеры), ранжированные по силе связи; у каждого шага — до трёх ID страниц и счётчики источников; термин только из словаря помечен `dictionary_only`; навигация, не причинная цепочка | `GET /v1/nav/graph/paths` |
| `graph_neighbourhood` | `node_id` (ID NAV: `SEC-`, `TRM-`, `FSY-`, `FPR-`, `TOP-`, `TBL-`, `PRM-`, `OCL-`; или любой ID VKM), `depth` (1–2), `limit` (≤ 200) | соседи узла NAV или DOCUMENT, сгруппированные по типу ребра и направлению, с общим числом и сильнейшими соседями (имена, ID страниц): у термина — ещё эквиваленты словаря, таблицы свойства (`TABULATES`) и значения (`VALUE_OF`); у таблицы канона — её сетка и группа повторов; `depth = 2` — соседи соседей | `GET /v1/nav/graph/neighbourhood/{id}` |
| `reconstruct_topic` | `query`, `budget_chars` (1000–60 000, по умолчанию 12 000), `source_ids` (≤ 20), `paraphrases` (≤ 4) | досье темы «от А до Я» одним вызовом: поиск по формулировкам темы (≤ 5: запрос, перефразы, синонимы и соседи понятия) со слиянием RRF; разделы NAV двумя ярусами — ядро ВКМ (источники каталогов evidence и области ВКМ/СКРУ) и остальное — со страницами, лучшими единицами и сниппетами ≤ 200 знаков; список страниц по ярусам; формулы (номер, «где…», параметры-кандидаты); рисунки и таблицы у найденных страниц; структурированные таблицы свойства, которое тема называет (≤ 6, `find_tables`); понятие и темы; источники с провенансом и CITES; процессы PC-xx каталогов с записями evidence, модели, конфликты, причинные связи; пробелы UNKNOWN. Текст ответа — markdown-оглавление в пределах бюджета (не JSON), `structured_content` — полный `ApiResponse` (`TOPIC_DOSSIER`). Навигация, не evidence ([NAVIGATION_LAYER.md](NAVIGATION_LAYER.md) §5) | `GET/POST /v1/topic` |
| `find_topics` | `terms` (1–5 фраз), `limit` (≤ 50), `level` (1–3) | темы поперёк книг (дерево тем без LLM), совпадающие со всеми фразами по леммам и SAME_AS |
| `get_topic` | `topic_id` (`TOP-…`) | путь к корню, дети, разделы-члены с источниками и страницами, центральные разделы, соседние темы |
| `similar_sections` | `section_id`, `k` (≤ 50), `other_sources_only` | разделы других книг, ближайшие по смыслу (косинус векторов разделов) |
| `section_topics` | `section_id` | темы раздела на всех уровнях |
| `copies_of` | `ref` (единица `u1-…`, страница или блок), `limit` | где тот же текст есть в других источниках: перепечатки, копии, общие аннотации; первичный — подсказка, не авторство |
| `source_overlap` | `source_id`, `limit` | источники, повторяющие текст данного: общие фрагменты, доли, кто раньше |
| `find_parameters` | `property`, `material`, `site`, `scale` (LAB/MASSIF/NORMATIVE/MODEL/UNKNOWN), `source_id`, `limit` | кандидаты значений параметров из таблиц и текста с источником, страницей, единицей (как напечатано и СИ), материалом, масштабом; не evidence и не рекомендуемое значение |
| `parameter_summary` | `property`, `material` | по материалу и масштабу: число кандидатов, источников, страниц и диапазон в СИ — карта, не значение |
| `translate_term` | `term` (фраза в любой форме, аббревиатура или `TRM-…`), `target` (ru, en, de), `limit` | эквиваленты термина на других языках корпуса, его синонимы и аббревиатуры из словаря NAV `term_translations`: методы, оценка (ожидаемая точность), страницы-примеры, статус (`AUTO_EXTRACTED_UNREVIEWED`, строки сидов — `REVIEWED_BY_AGENT`); фраза без своей пары переводится по частям — навигация, не evidence ([NAVIGATION_LAYER.md](NAVIGATION_LAYER.md) §4) | `GET /v1/nav/translate` |
| `get_table_structured` | `table_id` (ID таблицы канона `VKM-SRC-NNN:pNNNN:t…` или `TBL-…`), `max_rows` (1–500, по умолчанию 200), `max_chars` (200–60 000, 8 000) | таблица как сетка (NAV §10): номер и подпись, размер, строки шапки и способ их поиска, полосы и блоки, ориентация, уверенность и флаги; колонки (путь шапки, символ, единица и откуда она, роль, свойство словаря параметров); строки с ролями и ячейками (текст как напечатан, разобранное значение, единица, флаги вроде `DECIMAL_POINT_SUSPECT`) и markdown. Значения не исправляются — сверять со страницей. Таблица без сетки — NOT_FOUND (`get_table` показывает таблицу канона) | `GET /v1/nav/table/{id}` |
| `find_tables` | `property` («модуль деформации», «σсж», «ucs»), `material` («каменная соль», «соляные породы»), `source_id`, `text` (слова подписи, шапки, меток строк), `limit` (≤ 100) — хотя бы один | структурированные таблицы, где колонка значений, строка значений или подпись называет свойство, шапка или подпись — материал; все фильтры вместе; сначала таблицы со свойством в колонках. У таблицы: ID канона и `TBL-`, источник, страница, раздел, номер, подпись, размер, ориентация, уверенность, флаги и совпавшие колонки с единицами; как разобран запрос (`query`, `unresolved`) | `GET /v1/nav/tables` |
| `copies_of_object` | `object_id` (рисунок `…:f…`, таблица `…:t…` или формула `…:m…`), `limit` (≤ 200) | где ещё в корпусе есть этот рисунок, таблица или формула (NAV §9): группы повторов (копия той же работы, перепечатка, повторно использованный рисунок или таблица, общая формула, перерисованный рисунок, шаблон), первичная копия и правило её выбора (самый ранний год — подсказка для порядка, не авторство), остальные копии с доказательством (расстояние изображений, похожесть подписи, вхождение ячеек, совпадение формулы), источники и страницы; без повторов — пустой список | `GET /v1/nav/object_copies/{id}` |
| `shared_formulas` | `ref` (ID формулы или строка LaTeX, ≤ 2 000 знаков), `renamed` (та же структура в других обозначениях, только для выразительных формул; по умолчанию да), `limit` (≤ 200) | где записана та же формула: работы от ранней к поздней с источниками, страницами, напечатанными номерами и разделами — с тем же каноническим видом (LaTeX нормализован: греческие варианты, шрифты, пробелы, десятичная запятая) и, с `renamed`, с той же структурой; тривиальные записи не группируются; группы повторов этих формул. Та же запись, не проверенный закон. Формула без ключа — `reason: NO_FORMULA_KEY` | `GET /v1/nav/shared_formulas` |

Гибридный поиск (этап 2 лаборатории retrieval, §52–55 постановки): ключи слияния — стабильные ID канона: для `PAGE`
все единицы страницы засчитываются странице (первое вхождение; дубли страниц сворачиваются по `dup_group_id`, как в
BM25), для рисунков, таблиц и формул — ID объекта. Фильтры по полям типа объекта (`figure_type`, `text_layer`) в
гибридном поиске не поддерживаются (400). Ранги BM25 и dense считаются отдельно по каждому виду, сырые скоры видов не
сравниваются. Последняя стадия — по-прежнему `rerank_text` по `rerank_candidate` (для страницы, найденной только dense,
пассаж — блоки совпавшей единицы). Late interaction (MaxSim на RX580) — следующий этап, в трассе `late_rank = null`.

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

# VKM Corpus Platform v0 — журнал решений координатора

Живой документ Phase 0–8. Каждое решение: номер `CP-NN`, статус, основание (отчёт агента / постановка), следствия.
Статусы: ACCEPTED — действует; PENDING — ждёт дизайна/ревью; REVISED — изменено позже (с датой и причиной).

Входы: [бриф](00_COORDINATOR_BRIEF.md), [аудит A](AGENT_A_REPO_CONTRACT_AUDIT.md). Остальные отчёты агентов
добавляются по мере готовности.

## Решения по отчёту A (28.09.2026)

**CP-01 Пакеты и направление зависимостей — ACCEPTED.**
Код платформы: `src/vkm_corpus/` (документный слой, pipeline, проекции, API, MCP), `src/vkm_cad/` (мост Autodesk),
`src/vkm_drawio/` (MCP draw.io — требование пользователя от 28.09). Зависимость только `vkm_corpus → vkm_world`;
обратный импорт запрещён тестом (AST-скан `src/vkm_world`). `vkm_world`, `schemas/worldspec_vnext.schema.json`,
`scripts/*`, `evidence/**`, `catalogues/**`, `tests/world/**` в этой задаче не меняются.

**CP-02 Зависимости и lock — ACCEPTED.**
Базовые `dependencies` (numpy, pydantic) не расширяются. Extras: `corpus` (извлечение, Parquet, DuckDB),
`corpus-services` (FastAPI, uvicorn, MCP SDK, драйверы Neo4j/OpenSearch/PostgreSQL), `desktop` (Windows-мосты CAD и
draw.io). Отдельные lock-файлы собираются с `-c requirements/worldspec.lock.txt`, чтобы pydantic 2.13.5 /
pydantic-core 2.46.5 не сдвинулись (иначе меняется сгенерированная схема WorldSpec — K18). `requirements/worldspec.*`
не меняются. GLM-OCR живёт в отдельном окружении с собственным pin (не extra пакета).

**CP-03 Документные контракты отдельно от научного провенанса — ACCEPTED.**
`vkm_corpus.contracts` не наследует `vkm_world` `Provenance`/`SourceRef`/`WorldObject`: processing provenance ≠
scientific provenance (контракт v0.2 §36). Документный слой не несёт `EpistemicStatus`. Импортом переиспользуются
только хеш-функции `vkm_world.core.io`, `Scope`, `CoverageLevel`, функции частичных дат.

**CP-04 Имена полей и гигиена PUBLIC — ACCEPTED.**
В контрактах, JSON Schema, mapping OpenSearch и фикстурах запрещены ключи `quote, verbatim_quote, ocr_text,
page_text, full_text` и класс pydantic `Quote` (ключ `$defs`). Разрешённые имена: `text`, `native_text`,
`recognized_text`, `raw_output`, `normalized_text`, `caption`. Литерал старого OCR-каталога PRIVATE в коде и данных не
используется. `vkm_world/governance/leakage.py` в этой задаче не меняется; вместо этого добавляется тест гигиены
платформы `tests/corpus/test_public_hygiene.py`: запрет коммита `.epub .djv .parquet .duckdb .arrow .feather`, частных
IPv4 и host-путей (`/home/…`, `/mnt/…`, букв дисков) в infra-файлах (`.sh .ps1 .service .conf .ini .cfg .sql .cypher
.toml .txt Dockerfile *.example .yml .yaml .json .md`) и в коде/доках платформы. Усиление самого стража — отдельная
задача (decision note).

**CP-05 Source: все 251 строка, без «тихих» пропусков — ACCEPTED.**
Канон содержит строку Source на каждую запись реестра, включая 013 и 022: `lifecycle_status` ∈ {ACTIVE,
ABSENT_BY_REGISTER, RETIRED}; статус обработки `SKIPPED_BY_REGISTER` с кодами `ARCHIVE_DELETED_AFTER_ASSEMBLY` (013) и
`RETIRED_NOT_EVIDENCE` (022). Итог всегда сводится: 251 = обработанные + пропущенные по реестру + FAILED. 022 не
распаковывается; LFS-кэш как источник не используется. Сырые поля реестра сохраняются дословно.

**CP-06 Review ≠ coverage — ACCEPTED.**
`Source.review_status` (словарь контракта v0.2 §47) с основанием `review_status_basis`: 001–041 — из покрытия
evidence (FULLY → FULLY_REVIEWED, RELEVANT → RELEVANT_SECTIONS_REVIEWED), 042–195 — UNSEEN, 196–251 — QUICK_LOOK_ONLY
(метка приёмки в notes); сырое покрытие хранится отдельно. Все автоматически извлечённые объекты L1 — только
`AUTO_EXTRACTED_UNREVIEWED`, без наследования статуса источника. Автоматическая установка FACT /
REVIEWED_MEASUREMENT / ACCEPTED_PARAMETER / ACCEPTED_FORMULA запрещена валидатором.

**CP-07 Область источника — ACCEPTED.**
`site_scope_raw` (дословно `evidence_scope`), `site_scope` (список значений `vkm_world.Scope`), `site_scope_mapping`
∈ {EXACT, CASE, SYNONYM, LOSSY, MULTI, AMBIGUOUS, NOT_A_SCOPE}. AMBIGUOUS (002, 012, 045) в v0 не разрешается.
Реестр на месте не чистится.

**CP-08 Идентичность страницы — ACCEPTED (формат ID — у агента D).**
PDF и DjVu: физический индекс с 1 (совпадает с `pdf_page` всех 13 596 записей evidence). Развороты (037, 243)
остаются физическими страницами со списком печатных меток; логические полустраницы в v0 не вводятся. DOCX 023:
страницы закреплённого рендера (`page_kind` рендера + профиль рендерера), связь с evidence — RENDER_DEPENDENT до
проверки. EPUB 249: единица — spine-элемент, отдельный вид страницы.

**CP-09 Work — ACCEPTED (детали схемы — у агента D).**
`work_id` — от «якорного» источника явно подтверждённой группы; одиночный источник = свой Work. Группы только из
курируемой таблицы связей (SAME_WORK, PART_OF, VOLUME_OF, SERIES_PART, ABSTRACT_OF, COMPANION, EDITION_OF, NOT_SAME,
CONTAINS_COPY) — без fuzzy/ML. Таблица — source-related metadata → PRIVATE `00_registry/`, с receipt; загрузчик,
валидатор и тесты — в PUBLIC. Связи, разобранные из notes автоматически, — только кандидаты.

**CP-10 DjVu-титулы и 053 — ACCEPTED.**
После установки декодера проверяются титулы всех 8 непроверенных DjVu (053, 054, 221, 229, 230, 240, 245, 248).
Результат и расхождение числа страниц 053 (реестр 423 против 384 страниц + 39 DJVI) — receipt в PRIVATE; правка
реестра, если нужна, — отдельным коммитом, не молча.

**CP-11 Тесты и среда приёмки — ACCEPTED.**
Тесты платформы — `tests/corpus/` (`pytest.importorskip`, маркеры `services`, `gpu`, `desktop`; без сервиса →
skip/NOT_RUN). Приёмочные прогоны — Linux (WSL `archlinux` и CORE). База Windows `tests/world`: 206/209, три
известных падения окружения (symlink, CRLF, autocrlf) — не регрессия платформы; `tests/world` не трогаем.

**CP-12 Runtime-данные и receipts — ACCEPTED.**
Parquet, DuckDB, рендеры, raw OCR, логи, базы — только в `$VKM_DATA_ROOT`. CLI отказывается работать, если
`$VKM_DATA_ROOT` внутри клона PUBLIC или PRIVATE. В git — только компактные public-safe receipts (числа, хеши, логические
пути) в `docs/corpus_platform/receipts/`, не в `evidence/` и `catalogues/`.

**CP-13 Окончания строк infra — ACCEPTED.**
В PUBLIC `.gitattributes` добавляются правила `eol=lf` для `*.service *.conf *.cypher *.sql *.sh *.env.example
Dockerfile *.ini *.cfg *.yml *.yaml`; строки LFS-стража не меняются.

## Решения по отчёту F и данным B о EDGE (28.09.2026)

**CP-18 Реранкеры на EDGE — ACCEPTED (пороги латентности — после первого измерения).**

- Текстовый реранкер: существующий резидентный сервис (Docker-контейнер из CareerOps-развёртывания, transformers
  fp16, `jinaai/jina-reranker-v3.5` @ `e8a93f33…`, ≈1.2 GiB VRAM idle). Сохраняется как есть и не перезапускается; при
  очистке CareerOps он классифицируется как KEEP, несмотря на имя. VKM-обёртка вызывает его, не меняя.
  Резидентный дубль v3.5 (D-F2) не нужен: сценарий T3, модель уже резидентна.
- Визуальный реранкер: pinned mainline `llama-server` + официальный `jina-reranker-m0-Q6_K.gguf` (revision
  `61490ce6…`) + mmproj Q8_0, сконвертированный из чекпойнта `jinaai/jina-reranker-m0` @ `94bfe0ae…` + MLP-голова
  `mlp_weights.npz` в gateway. Разрешены (D-F1, D-F3; пользователь подтвердил 28.09): минимальный патч llama.cpp
  «skip lm_head при `--embeddings`» с sha256 патча в receipt; скачивание чекпойнта m0 (≈4.9 GB) на WORKSTATION для
  конвертации mmproj и эталона; скачивание Q6_K GGUF и `mlp_weights.npz` на EDGE.
- Parity-тест llama.cpp против эталона HF transformers — блокирующий до развёртывания m0.
- REVISED 28.09 (редакция 2 отчёта F по фактам EDGE): доступно 3716 MiB VRAM (380 MiB держит драйвер); пик памяти
  text-сервиса растёт с длиной запроса и не возвращается до рестарта. Поэтому: (а) gateway ограничивает запрос к
  text-бэкенду ≤ 4096 токенов (тем же `tokenizer.json`); ограничение числа одновременных запросов к text-бэкенду — только
  если исходники сервиса покажут параллельную обработку (это лимит одного бэкенда, не общий lock; text и visual
  по-прежнему считаются одновременно); (б) m0 остаётся **Q6_K** (требование «≈Q6» выполнено), но размещается частично:
  mmproj Q8_0 и ≈24 из 28 слоёв LLM на GPU, выходной слой и остаток — на CPU (обе модели резидентны); бюджет m0 ≈ 1950
  MiB; ожидаемо 5–7 с на изображение — отклонение по латентности документируется; патч D-F1 не нужен; (в) развёртывание
  m0 и gateway — Docker Compose на EDGE (NVIDIA Container Toolkit 1.20.0 уже есть), llama.cpp собирается в образе CUDA
  13.2 под sm_75; (г) порт 18082 существующего сервиса открыт на всех интерфейсах — техдолг (D-F7), сервис не меняем.
- Схема API text-сервиса, его модель конкурентности и маркеры старта берутся из исходников, выгруженных в архив
  CareerOps (git bundle), без SSH-инспекции агентом.
- Запасные варианты в порядке: m0 Q5_K_M → vision encoder на CPU → transformers NF4; любое отклонение от Q6_K
  фиксируется в `/status` и receipt.
- Конкурентность: два независимых процесса, свой CUDA context у каждого; без глобального lock и без CUDA MPS.
- Gateway `vkm-rerank-gateway` на EDGE (контракт — общий с агентом G, D-F4 — после отчёта G); изображения приходят
  inline base64 от VKM API с CORE.
- Лицензии обеих моделей CC BY-NC 4.0 — фиксируются в `/status`, MODEL_SERVICES.md, манифесте; веса не
  распространяются.

**CP-21 CUDA для GLM-OCR — ACCEPTED (указание пользователя 28.09).** Самый свежий рабочий стек под Blackwell sm_120 на
CUDA 13.x (на WORKSTATION CUDA Toolkit 13.4); CUDA 12.8 — только запасной вариант с обоснованием. Пакетная подача
страниц на OpenAI-совместимый сервер с измерением пропускной способности.

## Решения по отчёту G (28.09.2026)

**CP-19 API, MCP, CAD, draw.io — ACCEPTED (поля и ID выравниваются по отчёту D после ревью H).**

- VKM API: FastAPI (0.141.1), префикс `/v1`, единый конверт ответа `vkm.envelope/1` с полями `layer` и `payload_form`
  (canonical / raw / projection). Канон читается через DuckDB read-only; OpenSearch и Neo4j возвращают только ID,
  объекты гидратируются из канона; реранкеры вызываются по ID. Эндпоинтов, меняющих review, нет. 013/022 отдаются с
  `lifecycle_status`, без 404.
- Запись (reprocess): только постановка задания в PostgreSQL control plane (план → `confirmation_token` → отдельный
  write-токен); канон напрямую не мутируется.
- MCP: `mcp==2.2.0` (SDK v2, `MCPServer`), тонкий адаптер поверх API. Два сервера: `vkm-corpus` (read) и
  `vkm-corpus-admin` (write, по умолчанию не подключается); streamable HTTP (stateless, JSON) на CORE, bearer-токен,
  allowed_hosts. Изображения — ImageContent с лимитами размера. На WORKSTATION — stdio-серверы `vkm-cad` и `vkm-drawio`.
- `.mcp.json` не коммитится (DN-G7): в `docs/corpus_platform/MCP_TOOLS.md` — шаблон с подстановкой переменных
  окружения. Неотслеживаемый `.mcp.json.example` пользователя не трогаем.
- Acceptance §60: скриптовый MCP-клиент (`mcp.Client`) как gate + прогон `claude -p` с `--strict-mcp-config` и пустым
  набором встроенных инструментов, аудит транскрипта (все tool_use — `mcp__vkm-corpus__*`). Прогон расходует лимит
  плана пользователя — сообщается пользователю до запуска.
- CAD: AutoCAD 2026 RU и Civil 3D 2026 RU установлены. В v0 без .NET-плагина (.NET 8 SDK нет; установка — только по
  решению пользователя, DN-G8). Детектор возможностей без запуска AutoCAD; чтение открытых документов — только
  attach через COM `GetActiveObject`; scratch-операции — `accoreconsole` в изолированном каталоге; основной путь
  «вектор → DXF» — нативные векторы PDF (агент C) → `ezdxf` (MIT, DN-G10). CAD-выходы — DERIVED, `crs_status =
  UNKNOWN_CRS`, без EPSG.
- draw.io: 8 tools, детерминированный XML, запись только в `docs/diagrams/` и `$VKM_WORK/diagrams/`, PDF-экспорт
  только в `$VKM_WORK`. Проверка гигиены (CP-04) включает `.drawio` и `.svg`.
- Extras: `corpus`, `corpus-services`, `desktop` (CAD + draw.io; pywin32, ezdxf). Lock-файлы с
  `-c requirements/worldspec.lock.txt` (проверено: pydantic 2.13.5 не сдвигается).

## Ответ координатора на ревью H (28.09.2026)

Вердикт H — READY_WITH_BLOCKERS ([ревью](AGENT_H_ARCHITECTURE_REVIEW.md)). Все 6 BLOCKER и 22 MAJOR приняты; из 23
MINOR приняты 22, H-41 (сужение порта существующего реранкера) — отклонён для v0 (сервис не трогаем, техдолг D-F7).
Два изменения против рекомендаций H: сценарий B OCR (H-22) расширен до точечного переOCR источников с негодным
встроенным слоем в пределах бюджета (CP-22); `drawio_open` и превью draw.io оставлены (прямое требование
пользователя; §5 п. 7 H принят только в части раскладки).

| ID | Решение | Действие | Владелец |
|---|---|---|---|
| H-01 | принято | CP-16: единственный модуль словарей и грамматик `vkm_corpus.contracts` + `vkm_corpus.ids`; остальные только импортируют; AST-тест: `StrEnum` объявляются только в `contracts` | D; C, E, F, G |
| H-02 | принято | `origin` += `EMBEDDED_OCR`; `text_layer` += `PDF_EMBEDDED_OCR_LAYER`, `DJVU_EMBEDDED_OCR_LAYER`; поля `embedded_layer_evidence`, `text_layer_producer`; статус `EMBEDDED_TEXT_OK`; валидатор: страница-скан ⇒ `origin ≠ NATIVE` | D, C |
| H-03 | принято | `ArtifactKind.LAYOUT_RAW` (KEEP_RAW, публикуется); `region_origin`; `models[]` с ролями LAYOUT/RECOGNITION; bbox и ID детектированных объектов — только из сохранённого `LAYOUT_RAW`; путь без GPU — только явной конфигурацией | D, C |
| H-04 | принято | `contracts.text_rules.page_text_v1` и `rerank_text_v1`; материализация `pages.normalized_text` + `text_rule` + `text_sha256` одной функцией; view DuckDB `rerank_text` — единственный источник текста для E, F, G | D, C; E, G |
| H-05 | принято | две подписи: `stage_signature` и `call_signature = sha256(model_id, model_revision, weights_sha256, prompt, sampling, input_pixel_sha256)`; версия бэкенда пишется, в подпись не входит; сырьё content-addressed + индекс попыток; один смысл `--force` (пересчёт строк с кешем модели); повторный вызов модели — отдельный флаг `--recall-model` только через `--plan-only`; журнал `ledger/` не вводится | D, C; G |
| H-06 | принято | SDK `glmocr` не используется вовсе; AST-тест «нет `import glmocr` в `src/`»; allowlist хостов OCR-клиента; контейнер vLLM во внутренней сети без выхода наружу, порт на loopback через прокси-контейнер; `HF_HUB_OFFLINE=1` и для layout; проверка egress в canary | C, координатор |
| H-07 | принято | маркер корня `.vkm_root.json` (`STAGING`/`CANONICAL`) + `VKM_DATA_ROLE`; snapshot, `CURRENT`, DuckDB, Neo4j, OpenSearch и API на STAGING отказываются работать (тест) | координатор, D |
| H-08 | принято | head источника — цепочка `parent_commit_id`; коммит с чужим родителем — `CONFLICT` без автоматического разрешения; lease-файл источника в staging; время в порядке не участвует | D |
| H-09 | принято | `vkm-corpus core admit` (sha всех новых файлов и blob-ов, известная схема, родитель) и `core reconcile` (admit → snapshot → validate → `CURRENT` → DuckDB → Neo4j → OpenSearch); отклонённые коммиты не блокируют остальные; rsync без `--inplace`, маркеры последними; GC сирот — старше 24 ч | координатор, D, E |
| H-10 | принято | после каждого допущенного снимка — обратная копия `canonical/` и KEEP_RAW-артефактов на WORKSTATION с sha-манифестом; `pull-index`; веса моделей уже в архиве моделей WORKSTATION; образы — по digest, `docker save` только для собственных сборок | координатор, C, F |
| H-11 | принято | маркеры прогона `_runs/run=<RUN>/{START,END}.json`; снимок включает маркеры всех прогонов; `--resume` пишет `WORKER_CRASHED`; лог прогона — артефакт `RUN_LOG` (KEEP_RAW) | D, C |
| H-12 | принято | единственный планировщик — worker; состояния `PLAN_REQUESTED → PLANNED → CONFIRMED → RUNNING → PUBLISHED → ADMITTED \| REJECTED`; подтверждение привязано к `plan_sha256` | G, координатор, C |
| H-13 | принято | текст: ≤ 24 кандидата за вызов, больше — 413, без склейки пачек; кандидат — пассаж по `rerank_text_v1`; визуальный: ≤ 8 изображений за вызов, таймаут ≥ 120 с; в ответе `input_text_sha256`, диапазоны, `placement` | F, G, E |
| H-14 | принято | `producer_key = extractor_id \| extraction_generation \| model_id \| model_revision \| raw_config_hash` (модель — только для объектов, порождённых моделью); версии ПО — только в конверте; смена generation → новые ID + `object_lineage` в том же прогоне; DjVuLibre и LibreOffice закрепляются | D, C |
| H-15 | принято | `author_id` навсегда означает кластер ключа имени (`identity_status = NAME_KEY_ONLY`); человек — будущий `person_id`; `name_as_listed` обязателен | D, E, G |
| H-16 | принято | `INSTANCE_OF` — только из основной связи, не `FOREIGN_CONTENT`; чужие страницы — связь страницы с Work; `SHARES_PAGES_WITH` — Source↔Source с диапазонами; у рёбер `canonical_row_id` | E, D |
| H-17 | принято | производные правила (CITES, дубли, citing work, порядок страниц) — только SQL D (`duckdb/sql/`); проектор E исполняет те же файлы; тест «CITES графа = CITES DuckDB»; `rule_version` на производных рёбрах | D, E |
| H-18 | принято | на объектах — `source_site_scope*` и флаг `SCOPE_INHERITED_FROM_SOURCE`; параметр API/MCP — `source_scope` | E, G |
| H-19 | принято | индекс: `available_latest_day` + `available_basis`; фильтр требует `unknown_policy`; счётчики по основаниям | E, G, D |
| H-20 | принято | `DRAWING_UNITS`, CAD-виды артефактов; `crs_status` обязателен для GEO и DRAWING_UNITS; DXF `$INSUNITS = 0` + XDATA `VKM_UNITS=PAGE_PT`, фиксированные даты; CAD-выходы никогда не вход извлечения | D, G |
| H-21 | закрыто | пользователь утвердил PyMuPDF (CP-14) | — |
| H-22 | принято с изменением | CP-22 | C, координатор |
| H-23 | принято | `materialization` ∈ {STORED, NOT_STORED_REPRODUCIBLE} + `recipe`; кеш-ключи по пиксельному хешу; кросс-хостовый тест рендера в canary | D, C |
| H-24 | принято | `recognition_method`; `raw_artifacts[]` (role, artifact_id); `native_glyph_text`; `vector_artifacts[]` (PATHS_JSON, SVG) | D, C |
| H-25 | принято | `review_status = NOT_APPLICABLE` у реестровых Author/Venue; проверка «251» — по классам свёртки D | D, E |
| H-26 | принято | один compose-проект `vkm-core` (neo4j, opensearch, api, mcp, mcp-admin, разовые задания) во внутренней сети; базы — порт только на loopback; API/MCP — на LAN-адресе CORE; `:ro`-монтирование; образы по digest | координатор, G, E |
| H-27 | принято | canary += 005, 031, 202 (CP-23) | C, координатор |
| H-28 | принято | у Work `review_status = NOT_APPLICABLE`, покрытие — структура по источникам, без максимума | D |
| H-29 | принято | `match_status` ∈ {CANDIDATE, AUTO_EXACT_ID_MATCH, REJECTED}; курированных ссылок в v0 нет | D, E |
| H-30 | принято | `is_primary_layer`; по умолчанию фильтр по основному слою | E, D |
| H-31 | принято | R5 — DAG (OBSERVATION → {DOCUMENT, EVIDENCE}; зависимость от REPRESENTATION только у синтетики через SolverRun); виды PhysicalEntity — свойство `entity_type`, не label | E |
| H-32 | принято | одна константа раскладки в `vkm_corpus.config` + тест; на EDGE свой корень `$VKM_EDGE_ROOT` | координатор |
| H-33 | принято | маркеры только `services`, `gpu`, `desktop` (уже в `pyproject.toml`) | координатор |
| H-34, H-35, H-36, H-37 | приняты | текст `commit_id` поправить; формул OMML в 023 — 121 (замер C); `printed_label_origin` с экстрактором; `embedded_image_transcoded` + `original_filter`; `REPAIRED_COPY` убрать | D, C |
| H-38, H-39 | приняты | 404 с подсказкой вместо 410; `object_version = content_sha256 + commit_id`; `payload_form = RAW` при `layer = CANONICAL` только с `raw_artifact_id` | G, D |
| H-40 | принято | публичные receipts — сериализатор по белому списку; тест гигиены по каталогу receipts; `/status` отдаёт роли хостов, не адреса | координатор, G, E |
| H-41 | отклонено для v0 | существующий text-реранкер не перезапускаем; лимит одновременных запросов к нему — только если исходники покажут параллельную обработку (CP-18); порт — техдолг | F |
| H-42 | принято | отдельная БД и роль VKM с минимальными правами; bootstrap-роль в конфиге VKM не используется; CLI работает без PG; healthcheck PostgreSQL (R1b) исправить до создания БД VKM | координатор |
| H-43, H-44, H-45 | приняты | Neo4j в v0 только `wipe` + 503 на время сборки; `vkm-objects` — слияние по рангу (RRF); изображения по умолчанию 1024 px | E, C, G, F |
| H-46 | принято | схема не замораживается в `1.0.0`, пока в таблице §50 есть «нет» и не пройдены метрики canary | координатор, D |
| H-47, H-48, H-49, H-50 | приняты | `REGISTER_NOTES_NOT_EVIDENCE`; сборка DuckDB сверяет `content_fingerprint` с manifest; `CITING_WORK_IS_CONTAINER`, `n_citing_entries` + `n_citing_sources`, `work_copy_count`; якорь блока DOCX — `docx_paragraph_path` + `render_page_id` (RENDER_DEPENDENT) | G, D, E, C |
| H-51 | решено пользователем 28.09 | 239 и любые другие PDF с ограничениями копирования/печати от издательства обрабатываются как обычные файлы: без статуса UNSUPPORTED, без кода политики прав и без флагов (флаги прав — не шифрование; корпус локальный, тексты не распространяются, цитирование допустимо) | C |

**CP-16 Единый контракт — ACCEPTED (владелец реализации — D; до первого кода других агентов).**

1. Модули: `vkm_corpus.contracts` (`vocab` — все `StrEnum`; `models` — pydantic-модели строк датасетов; `arrow` —
   Arrow-схемы из моделей; `text_rules` — `page_text_v1`, `rerank_text_v1`, нормализация; `export` — JSON Schema в
   `schemas/corpus/`) и `vkm_corpus.ids` (грамматики, regex, конструкторы, разбор). C, E, F, G только импортируют.
2. Словари (закрытые): статус обработки — `NATIVE_OK, EMBEDDED_TEXT_OK, OCR_REQUIRED, OCR_OK, PARTIAL, UNSUPPORTED,
   FAILED, NEEDS_REVIEW, SKIPPED_BY_REGISTER, NOT_PROCESSED`; review — `UNSEEN, QUICK_LOOK_ONLY,
   AUTO_EXTRACTED_UNREVIEWED, RELEVANT_SECTIONS_REVIEWED, FULLY_REVIEWED, NOT_APPLICABLE` (FACT, REVIEWED_MEASUREMENT,
   ACCEPTED_PARAMETER, ACCEPTED_FORMULA — запрещены валидатором); `origin` — `NATIVE, EMBEDDED_OCR, OCR, DERIVED,
   REGISTRY, CURATED`; `text_layer` — `PDF_TEXT_LAYER, PDF_EMBEDDED_OCR_LAYER, DJVU_EMBEDDED_OCR_LAYER, EPUB_XHTML,
   DOCX_XML, GLM_OCR, NONE`; `region_origin` — `PDF_TEXT_BLOCK, PDF_XOBJECT, VECTOR_CLUSTER, NATIVE_TABLE_FINDER,
   LAYOUT_MODEL, EPUB_ELEMENT, DOCX_ELEMENT, DJVU_TEXT_ZONE, OCR_MODEL`; `models[]` — struct(role ∈ {LAYOUT,
   RECOGNITION}, model_id, model_revision); Stage — объединение стадий C и D с `CLASSIFY` и `LAYOUT`; ErrorCode —
   закрытый словарь D, поглощающий коды C (тест соответствия).
3. ID: страницы `VKM-SRC-NNN:pNNNN` (PDF/DjVu), `…:rNNNN` (страницы закреплённого рендера DOCX 023), `…:sNNNN` (spine
   EPUB 249); объекты `<page_id>:<b|f|t|m|c><12 hex>` — хеш вида, `region_origin`, bbox (PAGE_PT_TL, квант 0,1 pt) и
   `producer_key` (H-14); блоки DOCX — якорь `docx_paragraph_path`; прогоны `RUN-<UTC>-<8hex>`; артефакты
   `sha256:<hex>`; Work `VKM-WRK-NNN`; Author — кластер ключа имени (H-15).
4. Геометрия: `bbox_space` ∈ {PAGE_PT_TL, IMAGE_PIXEL, DRAWING_UNITS, GEO, NONE}; `crs_status` — только GEO и
   DRAWING_UNITS; выдуманного EPSG нет.
5. Текст: `pages.normalized_text`, `text_rule`, `text_sha256`; `blocks.is_primary_layer`; реранк — только view
   `rerank_text`.
6. Артефакты: виды включают `LAYOUT_RAW`, `OCR_RAW`, `RUN_LOG`, `VECTOR_PATHS_JSON`, `VECTOR_SVG`, CAD-виды; без
   `REPAIRED_COPY`; `materialization` + `recipe`.
7. Таблицы/формулы/рисунки — поля H-24; область источника на объектах — `source_site_scope*` (H-18); `available_from`
   в каноне NULL, в индексе — `available_latest_day` + `available_basis` (H-19).
8. Запись: неизменяемые партиции, commit-маркеры с `parent_commit_id` (H-08), маркеры прогонов (H-11), маркер корня
   STAGING/CANONICAL (H-07), подписи H-05, коммит источника всегда пересобирает источник целиком из кешей стадий (H §5
   п. 1).
9. Версия схемы — `0.x` до выполнения H-46.

**CP-15 Producer/publisher — ACCEPTED с десятью изменениями H §4.1.** WORKSTATION (WSL `archlinux`) — единственный
producer: pipeline, layout, GLM-OCR; пишет в `STAGING`-корень. CORE `/srv/vkm/data` — единственный `CANONICAL`-корень:
публикация rsync → `core admit` → `core reconcile`. PostgreSQL (EDGE) — только операционное состояние; канон от него
не зависит. Обратная копия канона и KEEP_RAW — на WORKSTATION (H-10).

**CP-14 PyMuPDF — ACCEPTED (пользователь, 28.09).** Основной экстрактор и рендерер PDF — PyMuPDF 1.28.2 (AGPL-3.0);
pypdfium2 — второй счётчик страниц. Если репозиторию позже дадут разрешительную лицензию, вопрос пересматривается.

**CP-22 Объём OCR v0 — ACCEPTED.** Обязательно: сценарий A (страницы без пригодного текста, ≈3,4–3,6 тыс.), регионы
формул и таблиц, формулы-картинки EPUB. Встроенные чужие слои — `EMBEDDED_OCR` (не NATIVE). Сценарий B: на canary и
в начале полного прогона — стратифицированная выборка ≈5 % страниц каждого источника со встроенным слоем, метрика CER
слоя против GLM-OCR; источник целиком переOCR-ится, если CER выборки > 10 % или доля букв слоя < 0,6, в пределах
бюджета ≤ 4 GPU-часов на сценарий B (решение по источникам — в receipt). При двух слоях основной выбирается правилом
(`is_primary_layer`), расхождение — метрика `native_ocr_cer`, а не автоматический `NEEDS_REVIEW`. CLI: `--max-model-calls`;
аварийная остановка на окне 200 вызовов при `finish_reason=length` > 2 %, пустом ответе на «чернилах» > 1 % или
повторах > 1 %.

**CP-23 Canary — ACCEPTED.** 197, 044, 052, 045, 014, 037, 249 (C) + 023, 025, 064, 033, 042 + 005, 031, 202 (H-27);
проверки К-01…К-26 ревью H §6.

**CP-24 Реализация и владение — ACCEPTED.** Параллельно, каждый в своих каталогах, без коммитов (коммитит координатор):
D — `contracts/`, `ids/`, `registry/`, `parquet/`, `duckdb/`; C — `extract/`, `layout/`, `ocr/`, `artifacts/`,
`pipeline/`, `infra/workstation/`; E — `graph/`, `search/`; F — `retrieval/`, `infra/edge/`; G — `api/`, `mcp/`,
`src/vkm_cad/`, `src/vkm_drawio/`, `docs/diagrams/`; координатор — `config.py`, `logs.py`, `cli.py`, `versions.py`,
`ops/`, `publish/`, `infra/core/`, `pyproject.toml`, `docs/corpus_platform/`, общие тесты. Тесты — `tests/corpus/` с
префиксом файла по агенту (`test_contracts_*`, `test_extract_*`/`test_ocr_*`/`test_pipeline_*`, `test_graph_*`/
`test_search_*`, `test_rerank_*`, `test_api_*`/`test_mcp_*`/`test_cad_*`/`test_drawio_*`).

**CP-26 Retrieval lab — ACCEPTED (дополнительная задача пользователя 28.09, [постановка](TASK_SPEC_RETRIEVAL_LAB_RU.md)).**

- Агенты J (benchmark, выбор моделей) и K (RX580 8 GB на CORE как постоянный retrieval-ускоритель) работают параллельно
  с основной платформой и не задерживают её. Одновременная резидентность dense + late-interaction на RX580 — целевой
  сценарий (уточнение пользователя); reload — только по измерениям.
- RTX 5070 Ti: приоритет у GLM-OCR и layout pipeline C; сервер vLLM не перезапускается и не выгружается ради
  benchmark; массовые прогоны J — только когда очередь OCR пуста (метрика vLLM `num_requests_running = 0`), малые
  проверки допустимы при свободной VRAM; OOM сервера OCR недопустим.
- Векторный поиск — только OpenSearch k-NN (встроен в дистрибутив 3.8.0); Milvus/Qdrant и т.п. не используются.
  Канон эмбеддингов — derived artifacts в `$VKM_DATA_ROOT/derived/embeddings/{dense,sparse,multivector,visual}/…`
  (Parquet/Arrow; multi-vector — Arrow nested или sharded mmap); OpenSearch — пересобираемая проекция без GPU.
- Владение: J — `src/vkm_corpus/retrieval_lab/`, `benchmarks/retrieval_v0/` (запросы, qrels, hard negatives, конфиги —
  без цитат источников), экспериментальные индексы OpenSearch с префиксом `vkm-exp-`, отчёты AGENT_J_*.md,
  `tests/corpus/test_retrieval_lab_*.py`; K — `src/vkm_corpus/embeddings/` (подпись эмбеддинга §37, подпись запроса §38,
  схема и писатель/читатель derived-артефактов, бэкенды кодировщиков, воркеры заданий), `src/vkm_corpus/retrieval_service/`
  (сервис CORE `/health /model-info /embed/query /search/dense /search/hybrid /search/late /metrics`),
  `infra/core/rx580/`, отчёты AGENT_K_*.md, `tests/corpus/test_embeddings_*.py`, `test_retrieval_service_*.py`.
  Новые MCP-инструменты (`search_hybrid`, `search_visual`, `retrieval_trace`) — G после появления сервиса K;
  production-поля векторов в индексах E — после выбора победителя (решение координатора); очередь заданий
  эмбеддинга в PostgreSQL — координатор (`ops`), интерфейс воркера — K.
- Qrels: graded 0–3; источники — reviewed evidence vNext (локаторы `pdf_page` → `page_id`), метаданные источников,
  индекс объектов СКРУ-1, явная проверка страниц; LLM-предложения — только кандидаты до проверки; спорные —
  NEEDS_REVIEW. Benchmark V0 строится на canary + нативных источниках после публикации их канона.
- После benchmark — ревью H (§60 постановки лаборатории).

**CP-25 INSTANCE_OF и копии — ACCEPTED (28.09, по вопросу E).** `INSTANCE_OF` строится из всех основных
(`is_primary`) не-FOREIGN_CONTENT связей Source→Work независимо от `lifecycle_status` (013 — тоже экземпляр
`VKM-WRK-013`: регистрационный факт не зависит от наличия файла; узел Source несёт статус). Проверка К-14 ревью H
уточнена: `INSTANCE_OF` у 013, 025 и 202; ни одного `INSTANCE_OF` из `FOREIGN_CONTENT`. Счётчик копий в выдаче поиска
(`work_copy_count`) — число доступных (ACTIVE) источников работы; view D отдаёт `n_sources_total` и `n_sources_active`.
Коды ошибок проекций E входят в закрытый `ErrorCode` (H-01).

**CP-17 Проекции — ACCEPTED (по отчёту E с упрощениями H).** Neo4j Community 5.26 LTS (5.26.31), label слоя
`:DocumentLayer`, в графе только ID, статусы, структура и `text_sha256`; в v0 только rebuild `wipe` (H-43); правила
слоёв R1–R4, R6–R8 и R5 как DAG (H-31); производные правила — общий SQL D (H-17). OpenSearch 3.8.0 без security plugin,
только loopback; индекс на тип (pages, blocks, figures, tables, formulas) + alias `vkm-objects` со слиянием RRF (H-44);
анализатор `vkm_text` (русский Snowball + английский Porter + `.exact`, ё→е, защищённые термины `keyword_marker`),
отдельный анализатор LaTeX; пересборка через версионированные индексы и alias swap.

## Решения по отчёту B и ответу пользователя (28.09.2026)

**CP-20 Вывод CareerOps — ACCEPTED (пользователь подтвердил D1 в 01:2x 28.09: «Да, выводить»).**
Выполняется план `AGENT_B_CLEANUP_PLAN.md` с решениями: D2 — YouTrack и Caddy KEEP (SHARED: другие проекты и
пользователи); D3 — VM `k8s-cp01` и её базовый cloud-образ удаляются; D4 — архив
`<ARCHIVE_DRIVE>:\VKM_ARCHIVE\careerops_2026-09-28\` на WORKSTATION, секреты не архивируются (только список имён);
D5 — полная копия объектов SeaweedFS + холодный снимок каталога; D6 — KEEP-объекты с историческим именем careerops
(PostgreSQL-сервер, text reranker, NM-профили, SSH-ключи CORE↔EDGE) не переименовываются в v0; D7 — compose
PostgreSQL копируется в нейтральный путь, healthcheck переводится на БД `postgres` в окне обслуживания (до создания БД
VKM); D8 — безопасность (вход по паролю в sshd, реранкер на 0.0.0.0, публичный YouTrack, общий tailnet) —
технический долг, фиксируется; D9 — uv/git-lfs на серверах не ставятся: сервисы VKM на CORE/EDGE работают в Docker;
D10 — снимки `~/recovery` не трогаются (решение пользователя). Выдержка между DISABLE и REMOVE сокращена с 24 ч до
нескольких часов (удаление — после canary) с проверками выживания после каждого шага.
Пользователь после удаления отзывает deploy-ключ GitHub, токен YouTrack и сессии профилей внешнего сервиса.

## Решения фазы реализации (28.09.2026, утро)

**CP-27 VRAM GLM-OCR — ACCEPTED (по замечанию C).** `--gpu-memory-utilization` 0.80 → 0.66: при 0.80 хвост KV-кеша
уходил в общую системную память Windows (WDDM) и генерация падала с ≈1,5–1,9 тыс. до десятков–сотен ток/с. При 0.66
(≈10,8 GB) вместе с композитором рабочего стола (≈3,2 GB) и layout-моделью (≈1,2 GB) всё остаётся в VRAM. Политика
драйвера «Prefer No Sysmem Fallback» не используется (системная настройка пользователя).

**CP-28 Таблица произведений в PRIVATE — ACCEPTED.** Seed D проверен по аудиту A §2.8: группы нескольких файлов, связи
произведений, общие страницы и «чужие страницы» — CURATED; три выведенные «чужие страницы» (005 → 031, 032 → 005,
199 → 034) подтверждены координатором и перенесены в генератор (воспроизводимость), 244 одиночных файла —
AUTO_PROPOSED. Проверка титулов восьми DjVu (C, CP-10) внесена в генератор: основание `TITLE_PAGE_VERIFIED`, identity
`VERIFIED_IN_FILE`; у 221 и 240 убраны заглушки названий и «автор = название». `SOURCE_REGISTER.csv` **не
правится** в v0 (закреплён по SHA-256 и используется прогонами): правка числа страниц 053 (384, а не 423 файла DIRM)
и заглушка 054 стр. 270 записаны как предложение в `00_registry/djvu_title_check_2026-09-28/`.

**CP-29 Упаковка и образ CORE — ACCEPTED.** SQL DuckDB и схема `ops` объявлены package data (без этого
не-editable установка в образе CORE теряла их). До готовности compose-сервиса `vkm-job` агента G reconcile canary
выполняется образом, собранным по Dockerfile G из закоммиченного HEAD, через host-local обёртку на CORE; секреты для
uid приложений — отдельные файлы (`neo4j_password`, копия DSN), `neo4j_auth` остаётся у uid Neo4j.

**CP-30 Воркер заданий — ACCEPTED.** `vkm-corpus ops worker` на WORKSTATION — единственный планировщик и исполнитель
заданий переобработки; стадии — обычный CLI в подпроцессах; план для подтверждения — без полей конкретного прогона
(хеш стабилен); перед исполнением план пересчитывается, изменившийся план снимает подтверждение; reconcile на CORE —
командой `VKM_RECONCILE_CMD`; исход по квитанции reconcile (REJECTED — отклонены коммиты задания; ADMITTED — снимок
PASS, сбой отдельной проекции — в примечании; иначе FAILED с записью в `ops.error`).

**CP-31 Лаборатория retrieval: GPU, состав V0, индексы — ACCEPTED (вопросы J Q1–Q4).** Референсные прогоны качества —
CPU fp32, пока OCR идёт; RTX — только в паузе без запросов OCR (≥ 5 мин, ≥ 6 GB свободно) и после полного OCR; GLM-OCR
ради лаборатории не выгружается. V0 — снимок после canary и снимок нативного прохода полного прогона; V1 — итоговый
снимок. CLI-группа `retrieval-lab`. Индексы экспериментов на CORE — только `vkm-exp-*`, удаление по точному имени,
≤ 20 GB, учёт в квитанции. Лицензия моделей Jina CC BY-NC 4.0 — допустимо для диплома, фиксируется в отчёте.

## Решения завершающей фазы (28.09.2026, день; срок пользователя — 16:00 МСК)

**CP-32 Полный прогон — ACCEPTED.** Два прохода на STAGING-корне canary: нативный (`--no-ocr`, разметка всех страниц),
затем OCR (`--concurrency 32`, кропы 200 dpi — по сетке C). Окно CP-22 для полного прогона — 2000 вызовов при прежних
порогах (length 2 %, empty-on-ink 1 %, повторы 1 %): на canary общие доли 0,21 % / 0 % / 0,02 %, а стопы давали локальные
кластеры (точки-заполнители 037, плотные таблицы 052), которые в окне 2000 не превышают порога; систематический сбой
по-прежнему останавливает прогон за минуты. Размер окна не входит в подписи стадий — кеши не инвалидируются.

**CP-33 Объекты без страницы и устойчивый reconcile — ACCEPTED (по первому reconcile canary).** Канон допускает
объекты с областью документа (элементы DOCX 023, которые закреплённый рендер не поставил на страницу; 128 объектов);
проверка графа C8 требовала `page_id` у всех объектов. `page_id` для объектов стал необязательным (C3 их и так
пропускал). Reconcile после PASS снимка строит DuckDB, Neo4j и OpenSearch независимо (`RECONCILED_WITH_FAILURES`) и
никогда не падает без квитанции.

**CP-34 Развёртывание API/MCP на CORE — выполнено координатором по решению пользователя.** Агенту G политика
разрешений отказала в записи секретов и удалённых записях на CORE; обход не делался, вопрос вынесен пользователю,
пользователь выбрал «Разверни ты». Образ собран из закоммиченного HEAD; токены созданы на CORE и не выводились;
клиентские токены (MCP, чтение API) — в `.vkm/secrets` профиля пользователя.

**CP-35 Приёмка MCP.** Скриптовый клиент §60 (7 шагов через streamable HTTP) — PASS на снимке canary. Прогон
`claude -p` только с инструментами MCP — NOT_RUN, пока CLI Claude на WORKSTATION не авторизован (действие
пользователя: `claude /login`).

**CP-36 Реранкеры (по отчёту F).** Быстрая 503 для перегруженного text-сервиса не включается (H-41): всплески ждут в
очереди, таймауты шлюза 300 с. Техдолг D-F7: существующий text-сервис слушает все интерфейсы (прямые клиенты обходят
лимит 4096 токенов шлюза) — не меняется в v0 (сервис не перезапускается).

**CP-37 Лаборатория retrieval к сроку 16:00 — ACCEPTED.** Бэкенд RX580, матрица моделей, parity и резидентность двух
моделей — выполнены (K). Бенчмарк — V0 на снимке canary в сокращённом объёме (BM25, пара RX580 и 2–3 модели, fusion,
реранкеры через VKM API; без CPU-эталонов на весь корпус). Выбор победителя для всего корпуса, эмбеддинги всего
канона, векторные поля OpenSearch и гибридные инструменты MCP — следующий этап, в v0 не входят (NOT_RUN с планом).
Сервис RX580 работает отдельным контейнером (restart unless-stopped); слияние с compose CORE — техдолг.

**CP-38 Списки литературы (§24) — DEFERRED по решению пользователя (28.09, «оставим на после OCR, запишем как
техдолг… вечером сделаем аккуратно и полностью»).** Сегментация библиографии в v0 не реализована: в каноне 0 записей
`BibliographyEntry`, в графе нет `REFERENCE_OF`, `RESOLVES_TO`, `CITES` (§58 «Work → citations where reliable» —
NOT_RUN по решению, не FAIL). Контракт, таблицы, правила D (атрибуция citing work, CITES только по точным ID,
проверка E09 для страниц с чужим содержимым) и проекции E уже готовы и принимают записи. Следующий шаг — после
полного OCR: извлечение списков из канона (заголовки разделов, разбиение по нумерации и отступам, год, DOI, ISBN),
пересборка источников из кешей без вызовов моделей, публикация и reconcile; качество — выборочная ручная проверка до
включения CITES.

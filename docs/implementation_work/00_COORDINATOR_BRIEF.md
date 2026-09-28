# VKM Corpus Platform v0 — бриф координатора для агентов

Дата: 28.09.2026. Ветка PUBLIC: `claude/corpus-platform-v0-2026-09-28` (от `main` = `f461fb6`).
PRIVATE `main` = `c575a07`. Этот файл — общий контекст и правила для всех агентов (A–I). Решения координатора
фиксируются в `docs/implementation_work/COORDINATOR_DECISIONS.md`.

Названия фиксированы и не меняются:

- диплом — «Горные и маркшейдерские работы при разработке Верхнекамского месторождения»;
- специальная часть — «Алгоритм прогнозирования оседаний земной поверхности на ВКМ на основе маркшейдерских измерений».

## 1. Цель

Сделать 251 зарегистрированный источник (`VKM-SRC-001…251`, PRIVATE `00_registry/SOURCE_REGISTER.csv`)
воспроизводимо доступным агентам структурированным корпусом:

```
SOURCE_REGISTER + сырые источники (PRIVATE Git/LFS)
  → native extraction → OCR/layout fallback (GLM-OCR)
  → Document Objects → canonical Arrow/Parquet
  → DuckDB → Neo4j DOCUMENT graph → OpenSearch
  → text + visual reranking (Jina) → API / MCP
```

Главное научное правило: **AUTO EXTRACTION ≠ REVIEWED EVIDENCE**. Автоматический результат имеет
`review_status = AUTO_EXTRACTED_UNREVIEWED` и никогда не становится FACT / REVIEWED_MEASUREMENT /
ACCEPTED_PARAMETER / ACCEPTED_FORMULA автоматически.

## 2. Одна технология — одна роль

| Технология | Роль |
|---|---|
| PRIVATE Git + LFS + `SOURCE_REGISTER.csv` | канон сырья (raw). Не копируется в PostgreSQL/Neo4j/OpenSearch/S3 |
| Arrow / Parquet в `$VKM_DATA_ROOT/canonical/` | канон структурированного документного слоя |
| DuckDB | локальный SQL-слой над Parquet, пересобираемый |
| PostgreSQL (EDGE, существующий сервер) | только operational/control plane: ingest_run, job, attempt, worker, heartbeat, review_task, error, service state. Потеря не теряет научный корпус |
| Neo4j Community (CORE) | пересобираемый DOCUMENT graph. Не physics graph |
| OpenSearch (CORE) | пересобираемый retrieval-индекс (BM25, фильтры, бусты). Не научная истина |
| Jina reranker v3.5 (EDGE, уже работает — сохранить) | текстовый rerank |
| Jina reranker m0 (EDGE, та же GTX 1650, ~Q6) | визуальный rerank по настоящим изображениям |
| GLM-OCR `zai-org/GLM-OCR` (WORKSTATION, CUDA) | OCR / распознавание документов, формулы, таблицы |
| VKM API (FastAPI) + VKM Corpus MCP (CORE) | доступ агентов через семантические операции, не raw DB shell |
| Autodesk bridge (WORKSTATION, Windows, .NET/COM) | производные CAD/vector-операции, только в scratch |

Хосты (в коммитах — только роли, без IP, логинов, серийных номеров и секретов):

- WORKSTATION — Windows 11, i7-14700KF, RTX 5070 Ti 16 GB, 64 GB RAM, WSL2 (дистрибутив `archlinux`, CUDA видна;
  дистрибутив по умолчанию — `docker-desktop`, в нём нет bash). Здесь лежат рабочие клоны PUBLIC и PRIVATE.
  Роль: GLM-OCR, OCR worker, CAD bridge, тяжёлые/по требованию задачи.
- EDGE — Debian, Ryzen 5 4600H, 32 GB, GTX 1650 Mobile 4 GB. Роль: PostgreSQL control plane, оба Jina reranker.
- CORE — Debian, Ryzen 7 5800X, ~32 GB, RX 580 (не использовать как обязательный ускоритель). Роль: `$VKM_DATA_ROOT`
  (если диски подходят), DuckDB, Neo4j, OpenSearch, VKM API, VKM MCP.

## 3. Запрещено в этой задаче

- Kafka, Airflow, Spark, Flink, ClickHouse, Kubernetes, Celery, Redis, Milvus, Qdrant — не использовать и не
  предлагать «на будущее».
- PhysicsNeMo, GNN, ANSYS, OGS/MFront-запуски, MATLAB, генерация миров, World-0, алгоритм прогноза, эксперименты
  T0/T1/T2/T4/T5 и G1/G3/G4/G5, научный evidence sweep, обучение моделей, новый поиск литературы.
- Physics graph в Neo4j, пустая «мегасхема» будущих сущностей.
- RAPIDS/cuDF/cuGraph без измеренной пользы. Benchmark пяти OCR. Выбор новой embedding-модели.
- Научные подмены: OCR → FACT; оцифрованная координата → точная координата; OCR-формула → reviewed formula;
  цитирование → согласие; дубликат источника → независимое evidence; общая теория → параметр СКРУ-1;
  автоматически определённый тип рисунка → истина при низкой уверенности (тогда `UNKNOWN_FIGURE_TYPE`).
- Выдуманный EPSG/CRS. Геометрия несёт статус CRS: `EXACT_COORDINATED | LOCAL_COORDINATES | UNKNOWN_CRS |
  MAP_DIGITIZED | RELATIVE | SCHEMATIC | UNKNOWN`. Для координат страницы документа — отдельная система
  «page space» (не географическая).

## 4. Теоретические документы v0.2 (читать, НЕ реализовывать)

Файлы пользователя (вне репозиториев; путь даёт координатор в задании агента):
`COUPLED_FIELD_KERNEL_FRAMEWORK_V0_2_RU.md`, `MICROKERNEL_FORMATION_AND_AGGREGATION_RULES_V0_2_RU.md`,
`MICROKERNEL_MINIMAL_STATE_AND_PHYSICAL_LAWS_V0_2_RU.md`, `THEORY_TO_SCIENTIFIC_DATA_SYSTEM_CONTRACT_V0_2_RU.md`.

Они задают семантические ограничения и будущие точки расширения. Сохранять различия:
PhysicalWorld ≠ WorldRepresentation ≠ ObservationWorld; PhysicalEntity ≠ Microkernel; PhysicalHypothesis ≠
ForecastMethod; World uncertainty ≠ representation error. Сейчас строится только L1 DOCUMENT layer
(контракт v0.2 §2, §5, §60). Будущие слои (Claim, Measurement, Experiment, Law, Parameter, PhysicalEntity,
PhysicalWorld, WorldRepresentation, ObservationWorld, SolverRun) должны добавляться позже **без перестройки**
document graph: новые узлы и рёбра ссылаются на стабильные document ID. Различать scientific / processing /
computation provenance (контракт §36). Различать времена: event_time, measurement_time, processing_time,
publication_time, available_from, ingestion_time (контракт §42). Сохранять existing source scopes
(`evidence_scope` реестра; контракт §46). Review states минимум: `UNSEEN, QUICK_LOOK_ONLY,
AUTO_EXTRACTED_UNREVIEWED, RELEVANT_SECTIONS_REVIEWED, FULLY_REVIEWED` (контракт §47).

## 5. Обязательные элементы контракта (из постановки)

- Work ≠ Source. Work — библиографическое произведение; Source — конкретный файл `VKM-SRC-xxx`.
- Существующие `VKM-SRC-xxx` не меняются. Новые ID (work, page, block, figure, table, formula, artifact,
  processing_run, bibliography entry, author, venue) — стабильные, детерминированные где разумно, независимые от
  внутренних ID БД, version-aware. Точный контракт ID проектирует агент D, проверяет агент H.
- Provenance-конверт каждого сгенерированного объекта минимум: `schema_version, object_id, source_id, page_id
  (если применимо), source_sha256, pipeline_version, processing_run_id, extractor_id, extractor_version, model_id,
  model_revision, config_hash, created_at, review_status, quality_flags`.
- Статусы обработки минимум: `NATIVE_OK, OCR_REQUIRED, OCR_OK, PARTIAL, UNSUPPORTED, FAILED, NEEDS_REVIEW`.
  Ошибка: `code, stage, tool, message, retryable, source, page, log reference`. Никаких silent failures и
  silent page loss.
- Idempotency: сигнатура обработки минимум `source_sha256 + page + pipeline_version + extractor version +
  model revision + config hash`. Повторный run не OCR-ит неизменившиеся страницы. CLI: `--resume --force
  --source --page --failed-only --plan-only`.
- Raw-выход моделей всегда сохраняется отдельно от normalized.
- Native data first: векторный/текстовый PDF не растеризуется целиком ради OCR; векторные пути/трансформы
  сохраняются как производный артефакт с провенансом.
- Rebuildability: DuckDB, Neo4j, OpenSearch удаляются и восстанавливаются из canonical Parquet.
- Каждый объект должен отвечать: что это; где оригинал; кто/что создал; native или OCR; auto или reviewed;
  версия pipeline; revision модели; source/page; можно ли пересобрать; можно ли удалить projection без потери канона.

## 6. Гигиена репозиториев (жёстко)

- Весь код платформы — только в PUBLIC (`src/`, `schemas/`, `tests/`, `infra/`, `docs/`). В PRIVATE — только
  данные (реестр, LFS-источники, intake/private manifests, private receipts). Третий репозиторий не создавать.
- Runtime/generated данные (Parquet, DuckDB-файл, базы Neo4j/OpenSearch, логи, рендеры, OCR-кэш, веса моделей,
  временное) не коммитятся никуда: они живут в `$VKM_DATA_ROOT` на инфраструктуре.
- PUBLIC видит PRIVATE только через `VKM_RESOURCES_ROOT`. Unit-тесты PUBLIC не требуют приватного корпуса —
  только синтетические fixtures (сгенерированные тестом PDF/DjVu/EPUB/изображения с синтетическим текстом).
  Никаких фрагментов текста реальных источников в fixtures.
- Страж утечки `vkm_world.governance.leakage.scan` (тест `tests/world/test_foundation.py::
  test_public_tree_has_no_private_leakage`) проверяет все отслеживаемые файлы PUBLIC:
  - запрещённые расширения: `.pdf .djvu .docx .doc .zip .7z .rar .xlsx .xls .tif .tiff`;
  - запрещённые имена колонок CSV и **ключей JSON на любой глубине** (включая JSON Schema `properties`):
    `quote, verbatim_quote, ocr_text, page_text, full_text` — в контрактах использовать другие имена полей
    (например `text`, `raw_text`, `normalized_text`, `recognized_text`);
  - абсолютные машинные пути (домашние каталоги, буквы дисков Windows, временные каталоги агентов); вместо них логические
    имена `$VKM_RESOURCES_ROOT`, `$VKM_DATA_ROOT`, `$VKM_WORK`, `<PUBLIC>`;
  - повтор ≥ 25 слов цитат PRIVATE подряд.
- В коммитах нет IP-адресов LAN, логинов, паролей, токенов, серийных номеров. Реальные значения — в host-local
  env/systemd/Docker secrets/Windows user config. Допустимы `.env.example` с плейсхолдерами.
- Сервисы (Neo4j, OpenSearch, PostgreSQL, rerankers, API, MCP) — только LAN/localhost, не в Интернет.
- Каждое преобразование оставляет receipt (входы, команда, код выхода, SHA-256 выходов).

## 7. Порядок работы агентов

- Phase 0 (discovery/design): агенты A–G работают read-only. Единственная запись — собственный отчёт в
  `docs/implementation_work/AGENT_<X>_*.md` (русский язык, технические термины можно по-английски). Агенты не
  коммитят, не пушат, не меняют чужие файлы, не ставят системные пакеты и не качают веса моделей > 1 GB.
  Для проб библиотек допустим одноразовый venv в `<PUBLIC>/work/` (git-ignored) или во временном каталоге.
- На серверах до принятия cleanup-plan ничего не удаляется и не останавливается. Никакого `docker system prune`.
- Если два агента хотят менять общий контракт — не правят независимо, а пишут decision note координатору.
- Предварительное владение каталогами для реализации (уточняется после дизайна):

| Агент | Каталоги |
|---|---|
| C — extraction/OCR | `src/vkm_corpus/extract/`, `src/vkm_corpus/ocr/`, `src/vkm_corpus/artifacts/` |
| D — canonical data | `src/vkm_corpus/contracts/`, `src/vkm_corpus/ids/`, `src/vkm_corpus/parquet/`, `src/vkm_corpus/duckdb/` |
| E — graph/search | `src/vkm_corpus/graph/`, `src/vkm_corpus/search/` |
| F — model services | `src/vkm_corpus/retrieval/`, `infra/edge/` |
| G — API/MCP/CAD | `src/vkm_corpus/api/`, `src/vkm_corpus/mcp/`, `src/vkm_cad/` |
| координатор | `src/vkm_corpus/config.py`, `src/vkm_corpus/cli/`, `src/vkm_corpus/ops/`, `pyproject.toml`, `infra/core/`, `docs/corpus_platform/` |

## 8. Факты, уже установленные координатором

- `SOURCE_REGISTER.csv`: 251 строка, `VKM-SRC-001…251`, колонки `resource_id, canonical_path, original_filename,
  sha256, size_bytes, source_class, evidence_scope, priority, scientific_role, migration_source, migration_status,
  notes`. Типы файлов: 238 `.pdf`, 9 `.djvu`, 2 `.zip`, 1 `.docx`, 1 `.epub`; суммарно ≈ 2.13 GB. Файлы
  `VKM-SRC-013` и `VKM-SRC-022` отсутствуют в рабочем дереве PRIVATE (причина — предмет аудита A).
  LFS-указателей без содержимого в рабочем дереве нет.
- Python проекта — 3.13 (`pyproject.toml`: `>=3.13,<3.14`), существующий пакет `vkm_world` (pydantic v2).
- На WORKSTATION: есть `pdftotext`; нет poppler `pdftoppm`, PyMuPDF, DjVu-инструментов (на момент 27.09);
  Docker Desktop установлен, но не запущен; CUDA 13.x toolkits на Windows; RTX 5070 Ti видна из WSL `archlinux`.

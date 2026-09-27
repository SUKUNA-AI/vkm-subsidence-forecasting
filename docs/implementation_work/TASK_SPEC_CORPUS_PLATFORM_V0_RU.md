# VKM CORPUS PLATFORM v0 — постановка пользователя (28.09.2026), копия для агентов

(Секции сохранены по номерам исходной постановки; текст сжат только в разделителях.)

## 0. Проект и репозитории
PUBLIC: SUKUNA-AI/vkm-subsidence-forecasting. PRIVATE: SUKUNA-AI/vkm-subsidence-forecasting_resourses.
Названия диплома и специальной части фиксированы, НЕ МЕНЯТЬ.
Ожидаемые main: PUBLIC f461fb6e52056909878757862ddf75abcd071d26, PRIVATE c575a0702d352ee1f67e9a01c40ba653d60fe13b
(подтверждено координатором 28.09). Работать от актуального main в отдельной ветке; никаких force-push/reset/rebase
опубликованной истории. PRIVATE registry: VKM-SRC-001…251 — проверить по SOURCE_REGISTER.csv.

## 1. Где пишется код
ВСЯ реализация VKM Corpus Platform v0 — в PUBLIC (software repository): src/, schemas/, tests/, infra/, docs/, CLI, API,
MCP, Neo4j projection builders, OpenSearch index builders, DuckDB builders, deployment templates, systemd/Compose
templates, migration code, CAD bridge code, OCR integration code.
PRIVATE — DATA REPOSITORY: SOURCE_REGISTER.csv, Git LFS scientific sources, intake manifests, private corpus manifests,
private receipts, source-related metadata, которое по архитектуре принадлежит PRIVATE. НЕ писать application code в
PRIVATE. НЕ создавать третий repository.
Runtime/generated data НЕ коммитить никуда: Parquet datasets, DuckDB runtime file, Neo4j database, OpenSearch indexes,
logs, page renders, OCR cache, model weights, temporary artifacts — они в VKM_DATA_ROOT на инфраструктуре.
PUBLIC ссылается на PRIVATE через configurable path VKM_RESOURCES_ROOT. Unit tests PUBLIC не требуют полного private
corpus — fixtures.

## 2. Subagents (обязательно)
Главный агент — COORDINATOR/INTEGRATOR (план, архитектурные решения, сведение, Git, принятие/отклонение предложений,
финальные проверки). Роли:
- A — REPOSITORY & CONTRACT AUDITOR: изучить PUBLIC, PRIVATE, SOURCE_REGISTER, current WorldSpec, validators, tests,
  file layouts, четыре v0.2 документа, существующие schemas, legacy Phase-1 data. Не реализует код в начале.
  Выход docs/implementation_work/AGENT_A_REPO_CONTRACT_AUDIT.md: что существует; что переиспользовать; какие контракты
  нельзя сломать; какие старые сущности конфликтуют с новой Corpus Platform; gaps; рекомендации coordinator.
- B — INFRASTRUCTURE AUDITOR: через SSH изучает CORE и EDGE, только inventory до разрешённого cleanup. OS, hardware,
  disks, free space, mounts, networking, Docker, Compose, systemd, PostgreSQL, databases, ports, Jina, SeaweedFS,
  YouTrack, Caddy, libvirt, VMs, cron, filesystem, running services, CareerOps remnants.
  Выход AGENT_B_INFRA_INVENTORY.md, AGENT_B_CLEANUP_PLAN.md. До принятия cleanup-plan ничего не удаляется.
- C — DOCUMENT PIPELINE / OCR ENGINEER: native extraction, PDF classification, DjVu, EPUB, page rendering, GLM-OCR
  integration, formulas, tables, figures, processing provenance, caching/idempotency. Проверить: где native лучше OCR;
  как сохраняются raw model outputs; как определяются page/object IDs; как избежать silent page loss.
  Выход сначала AGENT_C_EXTRACTION_DESIGN.md; после approval — src/vkm_corpus/extract/, ocr/, artifacts/.
- D — DATA / PARQUET / DUCKDB ENGINEER: Arrow schemas, Parquet datasets, stable IDs, schema versioning, processing
  runs, provenance envelope, DuckDB views. Выход AGENT_D_CANONICAL_DATA_DESIGN.md; после approval —
  src/vkm_corpus/contracts/, parquet/, duckdb/.
- E — GRAPH / SEARCH ENGINEER: Neo4j DOCUMENT GRAPH + OpenSearch retrieval projection. Сейчас НЕ physics graph.
  Nodes: Work, Source, Page, Block, Figure, Table, Formula, BibliographyEntry, Author, Venue. Relations: Source
  INSTANCE_OF Work; Work AUTHORED_BY Author; Work PUBLISHED_IN Venue; Source HAS_PAGE Page; Page HAS_BLOCK Block; Page
  HAS_FIGURE Figure; Page HAS_TABLE Table; Page HAS_FORMULA Formula; Work CITES Work; Page PRECEDES Page.
  Rebuildability Parquet→Neo4j, Parquet→OpenSearch. Выход AGENT_E_GRAPH_SEARCH_DESIGN.md; затем builders.
- F — RETRIEVAL / MODEL SERVICES ENGINEER (EDGE): existing jina-reranker-v3.5 + jina-reranker-m0. Проверить реальное
  deployment text reranker, не ломать. Visual: image input обязателен; high-quality quant ~Q6, не default Q4_K_M;
  оба сервиса одновременно на GTX 1650 и могут одновременно выполнять inference; никакого global GPU mutex; никакой
  искусственной сериализации. Выход AGENT_F_RERANKER_DEPLOYMENT.md + acceptance test results.
- G — API / MCP / CAD ENGINEER: VKM API, VKM Corpus MCP, Autodesk capability bridge (не блокирует corpus pipeline).
  MCP semantic tools. Проверить на workstation AutoCAD, Civil 3D, версии, .NET API, COM API; минимальный Windows-side
  CAD bridge/capability layer. Выход AGENT_G_API_MCP_CAD_DESIGN.md.
- H — ADVERSARIAL REVIEWER: не реализует; после design stage читает A–G и ищет: hidden coupling; непонятный source of
  truth; некорректные ID; необратимые migrations; scientific semantic loss; circular dependencies; data duplication;
  небезопасные write operations; неконтролируемый OCR; graph becoming canonical accidentally; inability to rebuild;
  private/public leakage; incorrect provenance; contracts, которые позже сломают World/Representation/Observation
  separation. Выход AGENT_H_ARCHITECTURE_REVIEW.md. Coordinator отвечает на каждое существенное замечание.
- I — VERIFICATION AGENT (ближе к концу): не доверяет заявлениям; сам проверяет services, tests, paths, source counts,
  page counts, hashes, Parquet, DuckDB, Neo4j, OpenSearch, rerankers, concurrent inference, MCP, cleanup,
  rebuildability. Выход AGENT_I_FINAL_VERIFICATION.md. FINAL READY нельзя при нерешённом FAIL.

## 3. Как агенты работают с кодом
Не править одинаковые файлы одновременно. Read-only agents для discovery; directory ownership для implementation (или
worktrees/branches); coordinator — integration. Shared contract меняется только через decision note coordinator.

## 4. Theory documents (v0.2)
COUPLED_FIELD_KERNEL_FRAMEWORK_V0_2_RU.md, MICROKERNEL_FORMATION_AND_AGGREGATION_RULES_V0_2_RU.md,
MICROKERNEL_MINIMAL_STATE_AND_PHYSICAL_LAWS_V0_2_RU.md, THEORY_TO_SCIENTIFIC_DATA_SYSTEM_CONTRACT_V0_2_RU.md.
Задают семантические ограничения и future extension points; НЕ задача текущей реализации. Не доказывать research
candidates, не реализовывать World-0, не создавать microkernel simulator, не писать GNN, не переписывать v0.2, не
переводить research candidates в production truth. Сохранить: PhysicalWorld != WorldRepresentation != ObservationWorld;
PhysicalEntity != Microkernel; PhysicalHypothesis != ForecastMethod; World uncertainty != representation error.

## 5. Основная цель
SOURCE_REGISTER + RAW SOURCES → native extraction → OCR/layout fallback → Document Objects → Canonical Arrow/Parquet →
DuckDB → Neo4j DOCUMENT GRAPH → OpenSearch → text + visual reranking → API/MCP. AUTO EXTRACTION != REVIEWED EVIDENCE.

## 6. Чего не будет
Kafka, Airflow, Spark, Flink, ClickHouse, Kubernetes, Celery, Redis, Milvus, Qdrant (не предлагать «на будущее»).
Не делать: PhysicsNeMo, GNN, ANSYS integration, OGS runs, MFront runs, MATLAB modelling, World generation, World-0,
Forecast algorithm, theorem experiments, evidence scientific sweep, model training, new literature hunt.

## 7. Инфраструктура
WORKSTATION: Windows, i7-14700KF, RTX 5070 Ti 16 GB, 64 GB RAM, WSL/Linux (могут быть OGS, MFront, RAPIDS/CUDA).
Heavy/batch compute. Развернуть zai-org/GLM-OCR как OCR baseline. Постоянные DB/search services здесь без причины не держать.
EDGE: Debian, Ryzen 5 4600H, 32 GB, GTX 1650 Mobile 4 GB. Уже работает jina-reranker-v3.5 — СОХРАНИТЬ. На ту же GPU —
jina-reranker-m0 (реальные изображения; ~Q6 / ближайший high-quality quant). Оба resident одновременно и могут
одновременно выполнять inference. Без global GPU lock.
CORE: Debian, Ryzen 7 5800X, ~32 GB. RX 580 не обязательный accelerator. Neo4j Community, OpenSearch, VKM API, VKM MCP,
persistent corpus services. VKM_DATA_ROOT — после disk inventory.

## 8. CareerOps cleanup
Сначала inventory (B). После approval: INVENTORY → EXPORT → VERIFY → STOP → DISABLE → REMOVE.
Сохранить: PostgreSQL server; existing Jina text reranker; SSH; LAN/admin infrastructure.
CareerOps PostgreSQL DB: logical export (schema+data); SHA-256; copy в Windows archival directory; verify
non-empty/readable; после этого database удалить. Старая CareerOps application infrastructure — удалить после verify.
SeaweedFS/S3 CareerOps — удалить. Old buckets — удалить. YouTrack — удалить, если inventory подтверждает, что больше ни
для чего не нужен. Old containers/systemd/volumes/configs — убрать, если CareerOps-only. НЕ blind docker system prune.

## 9. Raw source of truth
PRIVATE Git + Git LFS + SOURCE_REGISTER.csv. Не переносить raw corpus в PostgreSQL/Neo4j/OpenSearch. Не делать S3 raw copy.

## 10. Canonical structured source of truth
Apache Arrow / Apache Parquet; библиотеки pyarrow, polars, duckdb, pydantic (pandas — локально, не фундамент).
Пример VKM_DATA_ROOT/: canonical/{works,sources,pages,blocks,figures,tables,formulas,bibliography,authors,relations,
processing_runs}; artifacts/{page_renders,figures,tables,formulas,ocr_raw,native}; duckdb/; neo4j/; opensearch/; logs/; tmp/.

## 11. Document model
Work, Source, Page, Block, Figure, Table, Formula, BibliographyEntry, Author, Venue, Artifact, ProcessingRun.
Work != Source (Work — библиографическое произведение; Source — конкретный файл VKM-SRC-xxx).

## 12. Stable IDs
VKM-SRC-xxx остаются. Новые: work_id, page_id, block_id, figure_id, table_id, formula_id, artifact_id,
processing_run_id — stable, deterministic где разумно, independent от DB internal IDs, version-aware. Пример
VKM-SRC-243:p0001 допустим. Точный контракт — D, проверка — H.

## 13. Processing provenance (каждый generated object минимум)
schema_version, object_id, source_id, page_id if applicable, source_sha256, pipeline_version, processing_run_id,
extractor_id, extractor_version, model_id, model_revision, config_hash, created_at, review_status, quality_flags.

## 14. Review status
Автоматический результат: AUTO_EXTRACTED_UNREVIEWED. Нельзя автоматически: FACT, REVIEWED_MEASUREMENT,
ACCEPTED_PARAMETER, ACCEPTED_FORMULA.

## 15. Idempotency
Signature минимум: source_sha256, page, pipeline_version, extractor version, model revision, config hash. Повторный run
не OCR-ит неизменившиеся страницы. CLI: --resume, --force, --source, --page, --failed-only, --plan-only.

## 16. PostgreSQL role
EDGE, только operational/control plane: ingest_run, job, attempt, worker, heartbeat, review_task, error, service state.
Не canonical scientific data. Без Celery/Redis.

## 17. PDF / document extraction
PDF, DJVU, EPUB, image formats (если реально встречаются). PDF классифицировать: native text, vector, raster scan,
mixed/broken. Native data first: при vector text/paths НЕ rasterize всё и затем OCR. Native: text, paths, images,
transforms, metadata. OCR — только где требуется.

## 18. GLM-OCR
zai-org/GLM-OCR на workstation/WSL с CUDA. Зафиксировать model id, exact revision, backend, framework, CUDA version,
dependencies, model path, config. Для raster OCR, formulas, tables, structured OCR. Сохранять RAW MODEL OUTPUT и
отдельно normalized. Не benchmark пяти OCR.

## 19. DjVu
Нормальный decoder; native/render/OCR pipeline. Два ранее зарегистрированных DjVu идентифицированы без проверки титула —
после decoder проверить титульные страницы. При mismatch НЕ менять SOURCE_REGISTER молча: receipt/report и отдельная
correction.

## 20. Table: table_id, page, bbox, caption, crop artifact, raw recognition, normalized representation, quality flags,
review status. Raw всегда сохраняется.
## 21. Formula v0: formula_id, source, page, bbox, equation number if detected, raw recognition, normalized LaTeX if
available, quality flags, review status. Без semantic variable parsing.
## 22. Figure: figure_id, source, page, bbox, caption, image artifact, layout class, detected type if sufficiently
reliable, quality flags. Если модель не уверена — UNKNOWN_FIGURE_TYPE (не MINE_PLAN по похожести).
## 23. Vector figures: native vector PDF — сохранять vector representation/paths/transforms; не уничтожать
rasterization; будущая CAD/GIS-конвертация из native vectors.
## 24. Bibliography: извлекать BibliographyEntry; link BibliographyEntry → Work только при хорошем confidence (DOI, ISBN,
title, authors, year, venue); хранить match method, match score, review status; не merge works агрессивно.
## 25. DuckDB: rebuildable query layer; vkm_corpus.duckdb; views/macros минимум sources, works, pages, figures, tables,
formulas, bibliography, processing status.
## 26. Neo4j Community — только DOCUMENT GRAPH (nodes/relations как у E); понятные labels/properties/constraints/indexes;
полностью rebuildable.
## 27. Neo4j future extension: не создавать пустую мегасхему; stable document IDs позволят позже добавить Claim,
Measurement, Experiment, Law, Parameter, PhysicalEntity, PhysicalWorld, WorldRepresentation, ObservationWorld,
SolverRun без перестройки document graph.
## 28. OpenSearch (CORE) — retrieval projection. Индексировать минимум page/block text, figures, tables, formulas.
Fields: stable IDs, source/work, page, authors, year, language, text, caption, formula text, table text, bbox, review
status, quality flags. BM25 + metadata filtering + field boosting. Без новой embedding model; vector field позже.
## 29. Text reranker: existing jina-reranker-v3.5 обернуть стабильным API, напр. POST /v1/rerank/text {query, candidates,
top_n} → model id, model revision, scores, rank, latency, candidate IDs.
## 30. Visual reranker: jina-reranker-m0 на EDGE GTX 1650. Smoke test: text query + actual image candidates (maps,
tables, page images, geological section, figure). Text-only path не успех.
## 31. Concurrent reranking: оба loaded/resident/can calculate simultaneously; без общего inference lock.
Acceptance: 1) both loaded; 2) idle VRAM; 3) text requests; 4) visual requests; 5) concurrent requests; 6) burst
concurrent; 7) no OOM; 8) no model reload; 9) latency recorded. Если Q6 не помещается — ближайший high-quality quant;
любое отклонение документировать.
## 32. OpenSearch → rerank. Text: OpenSearch → candidate stable IDs → fetch canonical text → Jina v3.5 → ranked.
Visual: OpenSearch/metadata/text discovery → candidate page/figure image IDs → Jina m0 → ranked visual candidates.
## 33. API (FastAPI допустим) минимум: /health /status /search /source/{id} /work/{id} /page/{id} /artifact/{id}
/rerank/text /rerank/visual.
## 34. MCP (обязательно; не raw unrestricted DB shell). Tools минимум: get_source, get_work, get_page, get_page_image,
get_figure, get_table, get_formula, search_text, search_objects, get_document_neighbors, get_citations, rerank_text,
rerank_visual, get_processing_status, trace_document_provenance, reprocess_source, reprocess_page. Write tools отделить
от read tools.
## 35. MCP response contract: stable object ID, source ID, page, review state, provenance pointer, canonical/raw
distinction, quality flags. Agent не угадывает происхождение.
## 36. CAD: на workstation проверить AutoCAD, Civil 3D, версии; AutoCAD .NET API, Civil 3D .NET API, COM. Предпочтение:
Windows local bridge ↔ .NET/COM ↔ MCP. Не GUI clicking по умолчанию.
## 37. CAD MCP minimum: safe/read — cad_status, cad_list_open_documents, cad_get_layers, cad_get_extents,
cad_list_entities, cad_get_coordinate_system; scratch write — cad_create_scratch_document, cad_import_pdf_vector,
cad_extract_geometry, cad_export_dxf, cad_save_copy. Write только в derived/scratch; raw source не менять. Если
Autodesk — большой этап: capability detector/contracts/minimal bridge, не блокировать corpus pipeline.
## 38. Geo future compatibility: не строить GIS; не ломать GDAL/QGIS/Rasterio/GeoPandas/Shapely/pyproj. Geometry
metadata имеет CRS status. Никакого invented EPSG.
## 39. RAPIDS: не использовать ради GPU; не нужен для ingestion; без измеренной пользы не включать.
## 40. Topology: WORKSTATION — GLM-OCR, OCR worker, CAD bridge, heavy/on-demand tools. EDGE — PostgreSQL control
plane, Jina text, Jina visual. CORE — VKM_DATA_ROOT if disk suitable, DuckDB, Neo4j, OpenSearch, VKM API, VKM MCP.
Coordinator может изменить placement по inventory с объяснением.
## 41. Docker Compose для persistent Linux services где удобно. Не контейнеризировать AutoCAD/Civil 3D. PostgreSQL не
переезжает в Docker, если native стабилен. Не контейнеризировать всё ради одинаковой картинки.
## 42. Security: без secrets в Git (.env.example допустим); credentials — host-local env/systemd env/Docker env,
secrets/Windows user config. Neo4j/OpenSearch/Postgres не публиковать в Internet; LAN/localhost only.
## 43. Logging: structured logs (timestamp, service, run_id, job_id, source_id, page_id, stage, duration, status, error
code); log rotation; без Grafana.
## 44. Failure states: NATIVE_OK, OCR_REQUIRED, OCR_OK, PARTIAL, UNSUPPORTED, FAILED, NEEDS_REVIEW. Error: code, stage,
tool, message, retryable, source, page, log reference. Никаких silent failures.
## 45. Canary (до full run): 1 modern text PDF, 1 old Russian scanned PDF, 1 formulas-heavy, 1 tables-heavy, 1
map/figure-heavy, 1 DjVu, 1 EPUB. Полный путь registry → extraction → OCR if needed → Parquet → DuckDB → Neo4j →
OpenSearch → reranker → MCP.
## 46. Rebuildability: удалить DuckDB, Neo4j, OpenSearch и восстановить из canonical. PostgreSQL можно потерять без
потери корпуса.
## 47. Tests минимум: schema, stable ID, source SHA, idempotency, native extraction fixtures, OCR parser fixtures, Parquet
round trip, DuckDB query, Neo4j projection, OpenSearch indexing/search, text reranker contract, visual image reranker
contract, concurrent reranking, MCP contract, canary E2E. Без GitHub Actions ради этапа; не запускался — NOT_RUN.
## 48. Full run: весь registry, resumable. Counts: sources total/complete/partial/failed; pages total/native/OCR/failed;
figures, tables, formulas, bibliography entries.
## 49. Scientific safety: нельзя OCR → FACT; digitized coordinate → exact coordinate; formula OCR → reviewed formula;
citation → agreement; duplicate source → independent evidence; general theory → SKRU1 parameter; auto figure type →
truth при плохой уверенности.
## 50. Contract interpretability review (H, до full run): для любого object — что это? где оригинал? кто/что создал?
native или OCR? auto или reviewed? версия pipeline? model revision? source/page? можно ли пересобрать? можно ли удалить
projection без потери канона? Если нет — исправить контракт до full run.
## 51. Phases: 0 DISCOVERY (A–G read-only, H review, decisions) → 1 FOUNDATION (contracts, stable IDs, configuration,
VKM_DATA_ROOT, CLI, PostgreSQL operational schema) → 2 DOCUMENT PIPELINE (native, DjVu, EPUB, page artifacts, GLM-OCR,
tables, formulas, figures; canary raw → Parquet) → 3 QUERY PROJECTIONS (DuckDB, Neo4j, OpenSearch) → 4 RETRIEVAL (text
Jina, visual Jina, concurrency) → 5 API/MCP → 6 CAD → 7 FULL RUN (registry, failure repair, final projection rebuild) →
8 INDEPENDENT VERIFICATION (I).
## 52. Git: commit after coherent phases; не копить giant diff. Примеры сообщений: "corpus: add canonical document
contracts", "corpus: implement native PDF extraction", "ocr: integrate pinned GLM-OCR backend", "graph: add rebuildable
Neo4j document projection", "search: add OpenSearch page/object indexes", "retrieval: deploy concurrent text and visual
rerankers", "mcp: expose corpus semantic tools", "infra: retire legacy CareerOps services after export".
## 53. Docs минимум: docs/corpus_platform/{CORPUS_PLATFORM_ARCHITECTURE, DATA_CONTRACTS, DEPLOYMENT, OPERATIONS,
MCP_TOOLS, MODEL_SERVICES, CAREEROPS_MIGRATION_RECEIPT}.md — практичная документация.
## 54. Acceptance CareerOps — PASS только если: inventory сохранён; DB exported; checksum записан; copy на Windows
существует; export проверен; old DB removed после verify; SeaweedFS removed; old CareerOps services removed; text
reranker survives; PostgreSQL server survives; SSH/network survives.
## 55. Acceptance GLM-OCR — model pinned; GPU/CUDA работает; scan OCR works; table works; formula works; raw output
preserved; reproducible startup documented.
## 56. Acceptance RERANK — text works; visual accepts actual images; both loaded on GTX 1650; simultaneous requests pass;
no OOM; no unload/reload; exact quant/revision recorded.
## 57. Acceptance CANONICAL — registry processed; hashes validated; no silent losses; Parquet readable; DuckDB usable;
provenance complete; rebuildability demonstrated.
## 58. Acceptance NEO4J — Source → Work; Work → Author; Source → Pages; Page → objects; Work → citations where reliable;
every node traceable to canonical ID/source/page; graph rebuild script works.
## 59. Acceptance OPENSEARCH — Russian search works; filters work; stable IDs returned; candidate IDs usable by
reranker; rebuild works.
## 60. Acceptance MCP — один агент только через MCP: search_text → candidate pages → rerank_text → get_page → get
figures → rerank_visual → get object → trace provenance; сценарий успешен.
## 61. Agent I проверяет все основные критерии: PASS / FAIL / BLOCKED / NOT_RUN, без эвфемизмов. FAIL → исправить или
вся задача NOT READY.
## 62. Final report: initial/final PUBLIC HEAD; initial/final PRIVATE HEAD; branch/PR; agent list; что нашёл/сделал
каждый; разногласия и решения; server inventory; CareerOps export path/hash; removed services; final topology;
VKM_DATA_ROOT; GLM-OCR model/revision/backend; Jina text state; Jina visual model/quant/backend; concurrent GPU test;
canonical schema versions; corpus counts; DuckDB status; Neo4j counts; OpenSearch indexes/counts; API endpoints; MCP
tools; Autodesk bridge status; tests; failed/partial sources; technical debt; что сознательно NOT implemented.
## 63. Stop: после Corpus Platform v0 остановиться; не переходить к PhysicalWorld generation, World-0, T*/G*
experiments, OGS, ANSYS, PhysicsNeMo, GNN, Forecasting, evidence scientific promotion.
## 64. Главное правило: не оптимизировать архитектуру ради красоты; одна технология — одна роль (Git/LFS = canonical
raw; Parquet/Arrow = canonical structured document layer; DuckDB = local SQL; PostgreSQL = operational state only;
Neo4j = rebuildable document graph; OpenSearch = rebuildable retrieval index; Jina = reranking; GLM-OCR = OCR; MCP/API =
agent access; Autodesk bridge = derived CAD/vector operations). Если контракт ведёт к потере научной семантики,
неоднозначной provenance или невозможности rebuild — остановить часть, привлечь A + H, исправить контракт, зафиксировать
решение. Главный результат — 251-source corpus стал воспроизводимой, интерпретируемой и расширяемой научной системой
данных.

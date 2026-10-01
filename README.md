# ВКМ / СКРУ-1: корпус, evidence и прогноз оседаний

Диплом — «Горные и маркшейдерские работы при разработке Верхнекамского месторождения».
Специальная часть — «Алгоритм прогнозирования оседаний земной поверхности на ВКМ на основе маркшейдерских измерений».
Названия фиксированы. Данных предприятия для полноценной калибровки не будет: научная основа — корпус и проверенное evidence.

После D-18/D-19 научная цепочка: **источник → evidence → интерпретация → WorldSpec (3D + время) → физический мир → представление → оператор наблюдения → предрегистрированная проверка**.
Планируемый метод прогноза — нейросеть на ансамбле физических миров по теории владельца v0.2.
Ansys — основной решатель геомеханики, OGS + MFront — независимая проверка согласованных сценариев.
Сейчас физические миры, решатели и ML приостановлены. Текущий проход — инженерные контракты и offline CPU CI;
дальнейшая оцифровка геометрии и новая геологическая реконструкция приостановлены до получения GIS-данных.
Каноническое состояние и границы готовности — [PROJECT_STATE_RU.md](PROJECT_STATE_RU.md), решения — [D-18/D-19](docs/governance/PHASE1_DESIGN_DECISIONS_RU.md).

Реализация производственной программы данных ведётся по [S01–S26](docs/planning/PRODUCTION_DATA_PROGRAM_2026-10-01_RU.md).
Добавлены [dataset intake](docs/datasets/README_RU.md), [evidence/review/admission](docs/evidence/README_RU.md)
и [контролируемый update runtime](docs/corpus_platform/UPDATE_RUNTIME_RUNBOOK_RU.md). Квалификация полного корпуса,
проверка научно используемых данных по оригиналам и переключение production остаются отдельными gates.

## Репозитории и источник истины

| Слой | Где находится | Роль |
|---|---|---|
| PUBLIC | этот репозиторий | код, схемы, synthetic tests, public-safe каталоги без цитат, документация, sanitized receipts |
| PRIVATE | `SUKUNA-AI/vkm-subsidence-forecasting_resourses`, через `VKM_RESOURCES_ROOT` | source files, реестр, OCR, полные evidence-записи с цитатами в `11_evidence_vnext/` |
| DOCUMENT | канонический Parquet-снимок CORE | извлечённые объекты, статусы, SHA-256 и provenance; `CURRENT` переключается только после проверки |
| Проекции | DuckDB, NAV, OpenSearch, Neo4j, embeddings | восстановимая навигация и поиск по канону; результаты поиска — кандидаты |

Содержимое источника подтверждается самим source file. Извлечённый текст и NAV автоматически имеют статус **AUTO_EXTRACTED_UNREVIEWED**.
Проверенная оцифровка подтверждает чтение изображения; принятие evidence дополнительно требует проверки происхождения, scope, scale и применимости.
Public-каталоги выпускаются через [build_public_catalogues.py](scripts/build_public_catalogues.py); изображения, цитаты, закрытые координаты и геометрия остаются PRIVATE или в git-ignored `work/`.

## Корпус и инструменты

Текущий снимок **`snap-20260929T175107Z-574daaac`**: 271 источник, 27 135 страниц; 240 COMPLETE, 29 PARTIAL, 2 SKIPPED.
Поиск: BM25 + jina-v5-nano + mLateOn; dense и late — по 209 259 единиц.
Visual retrieval на Qwen3-VL охватывает 26 744 страницы с preview. Есть NAV, структурированные таблицы, формулы, provenance и `figure_series`.
Статус развёртывания — [intake receipt](docs/corpus_platform/receipts/intake_2026-09-29.json), текущая выборочная проверка — [platform integrity](docs/corpus_platform/PLATFORM_INTEGRITY_2026-09-30_RU.md).
Полный повторный live sweep всех C1/N1 проверок в рабочей сессии не выполнялся.

| Хост | Ответственность |
|---|---|
| WORKSTATION | Windows 11 + WSL archlinux; producer, локальные extraction/OCR/vision/GIS, одна тяжёлая GPU-задача с `work/gpu.lock` |
| CORE | канон, OpenSearch, Neo4j/NAV, API/MCP; dense, late и page-visual запросы; `rerank_text` через mLateOn |
| EDGE | `rerank_visual` через m0 и резервные сервисы; text v3.5 сейчас UNAVAILABLE |

`vkm-corpus` — основной read-only MCP (45 инструментов). Начинать с `reconstruct_topic`, затем адресно использовать `search_hybrid`, `get_page`/`get_page_image`, `find_tables`, `find_figure_series`.
`vkm-drawio` редактирует архитектурные схемы. `vkm-cad` обслуживает отдельные разрешённые задачи CAD.
Локальный `vkm-qgis` — узкий PyQGIS/GDAL мост для source layers, GeoPackage, control points, transforms/residuals, overlay и render/export.
Конфиги клиента и секреты остаются host-local и не отслеживаются Git.

Архитектура — [поток данных и страница A/B/C](docs/diagrams/platform_data_flow.drawio), [хосты](docs/diagrams/platform_hosts.drawio), [поиск](docs/diagrams/hybrid_search.drawio), [MCP](docs/diagrams/mcp_servers.drawio), [научная цепочка](docs/diagrams/science_chain.drawio).
Подробности — [архитектура платформы](docs/corpus_platform/CORPUS_PLATFORM_ARCHITECTURE.md), [MCP tools](docs/corpus_platform/MCP_TOOLS.md), [операции](docs/corpus_platform/OPERATIONS.md), [квитанции](docs/corpus_platform/receipts/README.md).

## Текущая работа A / B / C

- **A:** исходные PDF/DOCX/DjVu objects, native paths/text/XObjects, regions и overlapping tiles; массовое буквальное чтение GLM-OCR и проверка по изображениям. Ответы Qwen3.5-9B сохранены как исторический сравнительный прогон. Accuracy относительно gold set считается отдельно от reader agreement.
- **B:** source geometry и 2.5D + time zonal representation; детали выработок сохраняются. Регистрация требует подтверждённых соответствий, residuals и leave-one-out. PLANNED не повышается до EXECUTED.
- **C:** колонки, разрезы, скважины, picks и мощности с отдельным provenance. Source/section coordinates допустимы; неизвестная CRS и глубины остаются UNKNOWN. Недоказанные 3D surfaces не строятся.
- Неоднозначные подписи, GCP и topology попадают в `ASTRA_REVIEW_REQUIRED` с точным crop и влиянием на результат. Массовая обработка выполняется локально.

Методы и воспроизведение: [figure_readings_v2](benchmarks/figure_readings_v2/README.md), [geometry_skru1_v1](benchmarks/geometry_skru1_v1/README.md).
Локальный GIS-мост — [QGIS](docs/qgis/README_RU.md); исполненный объём и ограничения — [отчёт сессии](docs/corpus_platform/WORK_SESSION_FIGURES_GEOMETRY_2026-09-30_RU.md).
Тематическое ревью H: [freeze](benchmarks/topic_v1/H_REVIEW_FREEZE_2026-09-30.json), [результат](benchmarks/topic_v1/H_REVIEW_RESULT_2026-09-30.json), [ограничение слепоты](benchmarks/topic_v1/H_REVIEW_NOTES_2026-09-30.md).
Высокая тематическая релевантность не означает принятие evidence или field validation.

## Работа с кодом

`src/vkm_world/` — научные контракты, provenance, validation и leakage guard; `src/vkm_corpus/` — платформа корпуса;
`src/vkm_cad/`, `src/vkm_drawio/`, `src/vkm_qgis/` — прикладные мосты. `evidence/` и `catalogues/` — public-safe каталоги.
Перед изменениями читать [AGENTS.md](AGENTS.md). Рабочие результаты — в `work/` или PRIVATE, каждое преобразование оставляет receipt и SHA-256.

```bash
python scripts/run_offline_checks.py world-integrity --output ../offline-world
python scripts/run_offline_checks.py corpus-offline --output ../offline-corpus
python scripts/verify_canonical_repository.py
vkm-corpus mcp serve --kind read
python -m vkm_drawio.mcp_server
python -m vkm_qgis.mcp_server
```

Точные lock-файлы, оба Actions jobs и учёт `NOT_RUN` описаны в
[offline checks](docs/development/OFFLINE_CHECKS.md). Выбранное научное использование и DRAFT-протокол
проверяются [отдельным контрактом](docs/worldspec/SCIENTIFIC_ADMISSION_DRAFT_RU.md): его `READY` означает
согласованность предоставленных review-входов, а не физическую истинность или готовность решателя.

Пути в tracked файлах логические или относительные. UNKNOWN не подменяется числом; LAB ≠ MASSIF, аналог ≠ СКРУ-1, норматив ≠ измерение, модельный результат ≠ наблюдение.
Обучение и solver runs требуют отдельной задачи. Научные правила — [SCIENTIFIC_RULES_RU.md](docs/governance/SCIENTIFIC_RULES_RU.md), [VALIDATION_POLICY_RU.md](docs/governance/VALIDATION_POLICY_RU.md).

## История

Physical World v1, 2D-first OGS reference case, v3/v3.2 и synthetic stress lab v2/v2.1 / Gate A/B/C — **LEGACY_RETIRED**.
Полная прежняя архитектура доступна в `legacy` (`d54025d`) и Git history. Frozen results не переписываются.
Навигация — [legacy index](docs/legacy/LEGACY_INDEX_RU.md), [аудит удаления](docs/legacy/LEGACY_REMOVAL_MANIFEST_2026-09-30.md), [frozen references](scripts/frozen_references.json).

# Агент I — независимая проверка Corpus Platform v0

Статус файла: **итог** (волны 1 и 2 завершены). Записано 28.09.2026 15:06 MSK.

**Общий вердикт: READY** — все 14 критериев брифа PASS на итоговом снимке `snap-20260928T113713Z-fa0aa127`.
Открыт один FAIL вне критериев §59 (smoke Q5: буст номера рисунка), есть отклонение D-1 по решению CP-38
(библиография) и перечисленные NOT_RUN. Три FAIL, найденные в ходе проверки, исправлены и проверены на данных.

Правила проверки — [бриф агента I](AGENT_I_VERIFICATION_BRIEF.md). Вердикты: `PASS` / `FAIL` / `BLOCKED` / `NOT_RUN`.
Доказательства — собственные команды агента I против репозиториев, данных и живых сервисов. Отчёты исполнителей
использованы только как указатели. Адреса, пути хоста и секреты в файл не пишутся. Агент I не перезапускал сервисы,
не менял данные и конфигурацию, не запускал pipeline, публикацию и reconcile.

Что проверялось:

- PUBLIC: ветка `claude/corpus-platform-v0-2026-09-28`; HEAD в начале `8ee58c5`, в конце `8c2bfbc`.
  PRIVATE: та же ветка, HEAD `3143c88`, рабочее дерево чистое.
- Снимки CORE: canary `snap-20260928T073057Z-a2696925` (15 источников, волна 1); промежуточный
  `snap-20260928T103537Z-c3461ebf` (final03); **итоговый `snap-20260928T113713Z-fa0aa127`** (reconcile
  `RUN-20260928T113641Z-final04`, `manifest_sha256` `a64578c3…01567c1`).
- Итоговые числа (§48): 251 источник = 221 COMPLETE + 28 PARTIAL + 2 SKIPPED_BY_REGISTER (013, 022), 0 FAILED;
  страниц 26 483 = NATIVE_OK 16 455 + EMBEDDED_TEXT_OK 3505 + OCR_OK 6387 + PARTIAL 135 + OCR_REQUIRED 1, FAILED 0;
  блоков 621 497, рисунков 17 784, таблиц 3391, формул 108 904, записей библиографии 0 (CP-38).

## Таблица критериев

| # | Критерий | Вердикт | Доказательство (команда → ключевые числа) | Примечание |
|---|---|---|---|---|
| 1 | CareerOps (§54) | **PASS** | sha256 архива: 16/16 файлов таблицы квитанции совпали (sha и размер); `SHA256SUMS` edge 26/26, core 4/4, vm 3/3 OK; EDGE: юнитов и таймеров careerops/hh/seaweed нет, образов и контейнеров SeaweedFS нет, порт 8333 закрыт; БД — только `postgres`, `template0/1`, `vkm_ops`; роли `careerops_app` нет; PostgreSQL 18.6 healthy; текстовый реранкер `StartedAt 2026-09-10T10:58:55.889648906Z`, `RestartCount 0` (проверено в 10:49, 11:00 и 13:36); CORE: VM нет, юнитов careerops нет, системного каталога конфигурации careerops нет; SSH WORKSTATION → CORE и → EDGE работает; CORE → EDGE по LAN работает (API → gateway) | два мелких остатка — см. «Замечания» |
| 2 | GLM-OCR (§55) | **PASS** | образ `vllm/vllm-openai@sha256:8a69ffad…` = pin; модель `/models/zai-org__GLM-OCR/2e85a628…` = `model_pins.json`; `vkm-corpus ocr check` → `revision_ok: true`; vLLM на RTX 5070 Ti: 181 359 ответов `stop`, 148 `length`, 0 `error`; итоговый снимок: 6502 страницы с OCR-текстом (`OCR_OK` 6387, из них `RASTER_SCAN` 5687), 3331 таблица и 108 783 формулы с `origin=OCR`, модель `zai-org/GLM-OCR@2e85a628…`; 176 976 артефактов `OCR_RAW` (STORED, KEEP_RAW), выборка — файл есть, sha256 = ID; `GET /v1/artifact/{id}/content` → sha256 совпал; запуск описан в DEPLOYMENT §1.3 | модель агентом I не вызывалась (запрещено брифом) |
| 3 | Реранкеры (§31, §56) | **PASS** | EDGE `nvidia-smi`: text 1674 MiB + m0 1868 MiB = 3546/4096 MiB (13:36 — 3564); text через API: 200, `jinaai/jina-reranker-v3.5@e8a93f33…`, fp16, 15,3 с; visual через MCP: настоящие изображения страниц и рисунков, 4 кандидата ≈ 30 с; одновременная пара через API: text (10 страниц) + visual (3 кропа рисунков и 1 страница из 014/037/064) — обе 200, перекрытие 30,5 с, `pmon` — оба процесса считают в одни и те же секунды; строк OOM/CUDA error в логах: 0/0/0; `StartedAt`, `RestartCount`, PID text-сервиса и m0 и `media_marker` m0 не менялись весь день | ответ несёт `Q6_K + mmproj Q8_0`, `61490ce6…`, placement `mmproj=GPU; llm_layers_gpu=20/28; output=CPU`; gateway обновлён исполнителем до 0.1.1 в 12:55 — после этого text и visual проверены MCP-прогоном на итоговом снимке, одновременная пара — только на 0.1.0 |
| 4 | Канон (§57) | **PASS** | `canon validate --deep --acceptance --expected-sources 251` на итоговом снимке → PASS, 0 блокирующих, 0 предупреждений (B07, D05 — SKIP; E13 — PASS), 3 мин; 251 источник со статусом, 0 NOT_PROCESSED; у каждой страницы есть строка со статусом (без статуса 0); `page_count` = строкам у всех 249 документов, нумерация непрерывна; `registry verify`: 251 строка, 249 PRESENT_VERIFIED, 2 MISSING = 013 и 022 по реестру; у всех объектов `source_sha256` = реестру; Parquet снимка прочитан pyarrow напрямую: 19 наборов, 0 нечитаемых; `duckdb status` → `up_to_date`; провенанс: 0 объектов без обязательных полей, 0 OCR-объектов без модели, у всех есть сырой артефакт | 053: 384 страницы против подсказки реестра 423 — известно (CP-10, CP-28) |
| 4a | Полный прогон §48 / обязательный OCR сценария A (CP-22) | **PASS** | итоговый снимок: страниц без текста `OCR_REQUIRED` — 1 (VKM-SRC-221); PARTIAL 135 в 28 источниках, ошибки записаны (`OCR_TRUNCATED` 189); дозапуск OCR (CP-40) — 116 076 вызовов модели, без аварийного стопа | на final03 было 1767 страниц без OCR — FAIL-4, закрыт |
| 5 | Пересборка (§46) | **PASS** | квитанция `receipts/rebuild_demo_20260928T114522Z/` + квитанция графа `VKM-PRJ-DOC-20260928T115006Z-656fd511`: DuckDB удалён и собран заново из `CURRENT` (до и после — один снимок и манифест `a64578c3…`, отпечатки проверены); OpenSearch — 10 индексов удалены по точному имени, собраны заново: 26 483 / 621 497 / 17 784 / 3391 / 108 904 = канону; Neo4j — wipe удалил 663 311 узлов и 716 692 связи (333 транзакции), загрузка 779 027 узлов = канону, digest `cd691c57…` = ожидаемому; агент I сам: DuckDB собран в памяти из `CURRENT` (`:ro`) — 1 890 235 строк, отпечатки = манифесту, строки = рабочему файлу; `graph verify` C1–C16 PASS; `search status` — все 5 алиасов на сборке итогового снимка | равенство «до = после» показано только для DuckDB: у Neo4j и OpenSearch «до» было другим снимком (final03 и canary), поэтому показано «после = канон»; шаг графа в скрипте-демо упал (старый wipe, OOM), удаление и пересборку графа выполнил исправленный `graph rebuild` (`dc00b78`) |
| 6 | Neo4j (§58) | **PASS**; подпункт «Work → citations» **NOT_RUN** (CP-38) | итоговый снимок: `graph status` → READY, сборка `VKM-PRJ-DOC-20260928T115006Z-656fd511` из `CURRENT`; `graph verify` — C1–C16 PASS, C12 SKIP; C11: digest графа = digest из канона; узлы: Source 251, Work 247, Author 397, Venue 73, Page 26 483, Block 621 497, Figure 17 784, Table 3391, Formula 108 904; связи: INSTANCE_OF 251, AUTHORED_BY 598, HAS_PAGE 26 483, PRECEDES 26 234, HAS_BLOCK 621 486, HAS_FIGURE 17 720, HAS_TABLE 3390, HAS_FORMULA 108 852; скрипт пересборки работает (см. 5) | 128 объектов DOCX 023 без страницы (CP-33) — без ребра HAS_*; библиография — отклонение D-1 |
| 7 | OpenSearch (§59) | **PASS** | итоговый снимок: `search status` — алиасы pages/blocks/figures/tables/formulas на сборке `20260928t114733z-a64578c3`, числа = канону; `search smoke`: русская морфология и формы (Q1, Q2, Q7), ё/е (Q6), английские формы (Q8), фильтры год/origin/источник/область/доступность (Q3, Q4), RRF (Q9), кандидаты реранка ≤ 24 (Q10) — PASS; ID из поиска приняты реранкером (MCP-прогон); пересборка описана (OPERATIONS §4) и выполнена (см. 5) | Q5 (буст номера рисунка) — FAIL, строка 7a |
| 7a | smoke Q5 «рис. 3.1 мульда сдвижения»: рисунок с подписью 3.1 в первой пятёрке | **FAIL** | canary — PASS; итоговый снимок — FAIL «no figure labelled 3.1 in the top 5» (reconcile final03, final04 и демо §46) | дефект релевантности буста подписи на корпусе из 251 книги; не пункт приёмки §59 — см. FAIL-5 |
| 8 | MCP (§60) | **PASS** | итоговый снимок, `vkm-corpus mcp acceptance` через read-сервер (streamable HTTP): 8 шагов `search_text → rerank_text → get_page ×2 → get_figure → rerank_visual → get_object → trace_document_provenance`, все ok, 0 проблем конвертов, verdict PASS, 43 с (18 текстовых и 4 визуальных кандидата; лучший визуальный — страница не из canary); read-сервер отдаёт 21 tool, все `read_only`, не `destructive`, reprocess только на admin-сервере; без токена 401, admin-сервер с read-токеном 401 | прогон `claude -p` только с MCP — NOT_RUN (CP-35) |
| 9 | Научная безопасность (§49) | **PASS** | итоговый снимок: все 778 308 документных объектов `AUTO_EXTRACTED_UNREVIEWED`; FACT / REVIEWED_MEASUREMENT / ACCEPTED_PARAMETER / ACCEPTED_FORMULA — 0 в 15 наборах; OCR/EMBEDDED-объектов с другим статусом 0; все 17 784 рисунка `UNKNOWN_FIGURE_TYPE`; блоков NATIVE на страницах DjVu 0; страниц-сканов с NATIVE-текстом 0; поиск предупреждает `WORK_HAS_MULTIPLE_COPIES: copies are not independent evidence`; Work ≠ Source (247 Work на 251 Source, 3 Work с копиями); оценки реранкеров — слой SERVICE, `NOT_APPLICABLE` | |
| 10 | Интерпретируемость (§50) | **PASS** | итоговый снимок, MCP `trace_document_provenance`: 93 объекта из 80 источников (PAGE 10: 5 OCR, 2 EMBEDDED_OCR, 3 NATIVE; BLOCK 10: 4 OCR, 2 EMBEDDED_OCR, 4 NATIVE; FIGURE, TABLE, FORMULA, DOCUMENT, WORK, AUTHOR — по 10; SOURCE 13 с 013 и 022) — ответы на все вопросы §50 у всех 93; у каждой страницы `origin` = происхождению текста, у OCR-страниц есть ревизия модели; DuckDB: 0 страниц с текстом, у которых `origin` ≠ `primary_text_origin`; 6502 OCR-страницы — модель и RECOGNITION есть у всех | на canary был FAIL-1 — закрыт |
| 11 | Тесты (§47) | **PASS** | итоговый HEAD `8c2bfbc`, чистый клон, WSL, `tests/corpus` + `tests/world`, `-m "not services and not gpu and not desktop"`: 818 passed (corpus 610, world 208), 14 skipped (13 модулей без пакетов в venv pipeline + 1 тест ветки `legacy`, которой нет в клоне), 10 deselected, 0 failed; Windows-venv G на том же клоне: 175 passed (14 модулей API/MCP/CAD/draw.io, пропущенных в WSL); `services` с DSN: 2 passed (живой plan-first в `vkm_ops`), 4 skipped (нет URL); ранее чистые клоны `b82a156` (782), `d0f31b0` (811), `e30c533` (818) — 0 failed | NOT_RUN — см. список |
| 12 | Гигиена репозиториев | **PASS** | `verify_canonical_repository.py` (с PRIVATE) на `8c2bfbc` → `PASS_WITH_NONBLOCKING`, 35 PASS, 7 SKIPPED_REF_UNAVAILABLE (теги frozen недоступны локально), exit 0; leakage 0; host paths 0 из 326 файлов; ссылки Markdown 304/0 битых (85 файлов); сверка каталогов 76/0; значения токенов WORKSTATION и LAN-адреса CORE/EDGE в отслеживаемых файлах — 0; данных выполнения в git — 0; PRIVATE: изменения только `00_registry/` и README | VM-пути облачной сессии (домашний каталог `user`) — только в старых доках `docs/reset_2026_09/` (исключение политики путей §3) |
| 13 | Мост Autodesk и draw.io | **PASS** | MCP `vkm-drawio`: `drawio_create_diagram` → `.drawio` (3 узла, 2 ребра), `drawio_export` → PNG 12 700 B и SVG 7646 B в scratch, draw.io 31.5.3; MCP `vkm-cad`: `cad_status` → AVAILABLE (AutoCAD 2026 25.1.60.0, Civil 3D 2026 13.8.280.0, COM зарегистрирован), `cad_list_open_documents` → `CAD_NOT_RUNNING` (мост не запускает AutoCAD), scratch DXF R2018 создан и экспортирован; `cad_import_pdf_vector` из артефакта canary через API: 17 022 пути → DXF, `UNKNOWN_CRS`, `epsg: null` | чтение открытого чертежа через COM — NOT_RUN (AutoCAD не запущен) |
| 14 | Топология и документация (§53) | **PASS** | 7 обязательных документов есть; сверены: CORE Debian 13, Docker 26.1.5, Compose 2.26.1, `vm.max_map_count` 1048576, digest Neo4j и OpenSearch; версии pipeline (pyarrow 25.0.1, polars 1.44.2, DuckDB 1.5.5, pydantic 2.13.5/2.46.5, PyMuPDF 1.28.2, pypdfium2 5.13.0, torch 2.13.0+cu130, transformers 5.17.0, DjVuLibre 3.5.30, uv 0.12.19, rsync 3.5.1, git-lfs 3.8.0); FastAPI 0.141.1, MCP 2.2.0; команды `config`, `canon status`, `ocr check`, `rerank health`, `ops status` и `docker compose … --profile jobs run --rm vkm-job canon status` работают как описано; tools MCP = таблице MCP_TOOLS; `/v1/status` отдаёт роли хостов без адресов; образы API и заданий на CORE совпадают с коммитами (sha256 всех файлов пакетов: `8ee58c5`, `1b838cf`, `50856db`, `53de9a2` — 0 отличий) | неточности — см. «Замечания» |

## FAIL — найденные, с воспроизведением и статусом

### FAIL-5 (открыт). Smoke Q5: буст номера рисунка не выводит «рис. 3.1» в первую пятёрку

- Факт: на итоговом снимке `search smoke` → Q5 FAIL «no figure labelled 3.1 in the top 5» (также в reconcile final03,
  final04 и в демо §46). На canary — PASS. Остальные 9 проверок smoke — PASS.
- Воспроизведение: `vkm-corpus search smoke` на CORE (в `vkm-job`); запрос `POST /v1/search`
  `{"query": "рис. 3.1 мульда сдвижения", "kinds": ["FIGURE"], "limit": 10}`.
- Оценка: исполнитель ослабил проверку с «первое место» до «первая пятёрка» (`5c4170f`), но и она не проходит.
  Это дефект релевантности (буст подписи слабее текстового совпадения на корпусе из 251 книги), не пункт приёмки
  §59. Из-за него исполнитель собрал итоговый индекс без smoke-ворот (`search build --prune`, затем smoke отдельно).

### FAIL-1. OCR-страницы помечены `origin = NATIVE` и без модели распознавания — исправлен, проверен

- Найдено на canary: 1117 страниц с `primary_text_origin = OCR` имели `origin = NATIVE`, `model_id`/`model_revision`
  = NULL, в `models[]` только LAYOUT; MCP отвечал для `VKM-SRC-044:p0125` `origin: NATIVE`, `model_revision: null`.
  Нарушение DATA_CONTRACTS §4.
- Воспроизведение: `SELECT origin, primary_text_origin, count(*) FROM pages GROUP BY ALL;`.
- Исправление: `3d65454` (origin страницы = происхождение основного текста, RECOGNITION-модель, `to_canon_v2`, E13).
- Итоговый снимок: 0 нарушений; 6502 OCR-страницы с моделью; выборка MCP — 10/10 страниц верна.

### FAIL-3. После reconcile API и MCP продолжали отдавать старый снимок — исправлен, проверен вживую

- Найдено после reconcile final03: `/v1/status` → `snapshot_id` canary, `up_to_date: false`; `GET /v1/page/
  VKM-SRC-054:p0192` → 404; выборка §50 через MCP находила только объекты canary.
- Причина: `CanonStore._connection` открывал тот же путь при открытом старом соединении; кеш экземпляров DuckDB
  возвращал старую базу.
- Исправление: `53de9a2`; образ `vkm-corpus-api:0.1.0-53de9a2f70dc` = коммиту. Живая проверка: после reconcile final04
  API без перезапуска отдал `snapshot_id` = итоговому, `up_to_date: true`; после удаления и пересборки DuckDB в демо
  §46 — тоже.

### FAIL-4. Обязательный OCR сценария A не был завершён — исправлен, проверен

- final03: 1767 страниц `OCR_REQUIRED` без текста в 21 источнике после аварийного стопа CP-22 (`OCR_QUALITY_STOP`).
- Исправление: дозапуск OCR (CP-40). Итоговый снимок: `OCR_REQUIRED` — 1 страница, PARTIAL 135 с записанными ошибками.
- Воспроизведение: `SELECT source_id, count(*) FROM pages WHERE page_status = 'OCR_REQUIRED' GROUP BY 1;`.

Бывший FAIL-2 (библиография) переведён в отклонение D-1 решением CP-38.

## Отклонения от постановки (приняты решением, не FAIL)

### D-1. Библиография не извлекается (§24); «Work → citations» (§58) — NOT_RUN

- Факт (все снимки): `bibliography_entries` = 0; в графе BibliographyEntry 0, REFERENCE_OF 0, RESOLVES_TO 0, CITES 0.
- Агент I сообщил координатору ~11:10 как FAIL: решения тогда не было (только PIPELINE §9).
- Решение: CP-38 (коммит `b82a156`) — отсрочка как техдолг. Согласие пользователя агент I знает со слов координатора
  (цитата пользователя в CP-38); сам чат агент I не видел.

## BLOCKED / NOT_RUN — с причинами

| Что | Вердикт | Причина |
|---|---|---|
| `tests/corpus/test_graph_live.py`, `test_search_live.py` | NOT_RUN | тесты пишут временные данные (свой namespace/индексы) в рабочие Neo4j и OpenSearch — агенту I запись в сервисы запрещена; пакетов `neo4j`/`opensearch-py` нет в локальных venv; вместо них — read-only `graph verify` и `search smoke` |
| `test_rerank_live.py` | NOT_RUN | бриф ограничивает число вызовов реранкеров; вместо него — ручные вызовы (критерий 3) |
| `test_ocr_live.py`, `vkm-corpus ocr smoke` | NOT_RUN | вызывают GLM-OCR мимо pipeline — запрещено брифом |
| `test_drawio_desktop.py` | NOT_RUN | opt-in `VKM_TEST_DESKTOP`; вместо него — экспорт через MCP draw.io (критерий 13) |
| `test_retrieval_service_live.py`, `test_retrieval_lab_pipelines.py`, гибридный поиск | NOT_RUN | лаборатория retrieval (J/K/V, CP-26, CP-37), вне критериев v0 |
| одновременная пара реранкеров через gateway 0.1.1 | NOT_RUN | пара проверена на 0.1.0; после обновления gateway — только последовательные вызовы (MCP), лимит вызовов брифа |
| чтение открытого чертежа AutoCAD через COM | NOT_RUN | AutoCAD не запущен; мост по дизайну его не запускает |
| прогон `claude -p` только с MCP | NOT_RUN | CP-35: CLI Claude на WORKSTATION не авторизован |
| §58 «Work → citations», §24 библиография | NOT_RUN | отложено решением CP-38 (отклонение D-1) |
| удаление и восстановление проекций своими руками (§46) | BLOCKED для агента I | запрещено брифом; проверено по квитанциям исполнителя и собственными read-only проверками (критерий 5) |
| C12 `graph verify` (повтор той же сборки) | SKIP в отчёте проверки | второй сборки того же снимка нет |

## Замечания (не FAIL)

- EDGE: остался пустой анонимный том Docker от SeaweedFS (создан 29.08, внутри пустой каталог `filerldb2`, 12 KB).
  Квитанция CareerOps говорит, что анонимный том удалён. Убрать владельцу: `docker volume rm <id>`.
- Архив CareerOps: в `workstation/` нет `SHA256SUMS`, хотя квитанция говорит «в каждом подкаталоге».
- Проверки менялись исполнителями после сбоев на полном корпусе: smoke Q5 (`5c4170f`), B04 (`50856db`: допускается
  столько индексов дублей, сколько одинаковых якорей; причина — 12 одинаковых блоков `VKM-SRC-150:p0224`). Агент I
  прочитал оба diff: смысл B04 сохранён; ослабление Q5 обосновано, но проверка всё равно не проходит (FAIL-5).
- Во время финала были ещё два сбоя, исправленные исполнителем: OOM Neo4j при wipe 663 тыс. узлов (исправлено в
  `dc00b78`: цикл ограниченных транзакций) и занятая блокировка второй сборки поиска (`E_PROJECTION_BUSY`, алиасы не
  тронуты).
- На CORE задания и reconcile идут через host-local обёртку `vkm-job` (CP-29); итоговые reconcile и пересборки —
  образами, собранными из коммитов HEAD (проверено для `50856db`, `53de9a2`). Документированный путь `docker compose …
  --profile jobs run --rm vkm-job …` тоже работает.
- Коды ревизий прогонов в каноне (`code_revision`) — настоящие коммиты ветки; у прогона импорта реестра — `unknown`.
- Часть страниц с нативным текстом несёт модель GLM-OCR в `model_id` и RECOGNITION в `models[]` (на них распознавались
  регионы формул/таблиц). Происхождение текста не искажено, но поле «модель содержимого» у такой страницы двусмысленно.
- MCP_TOOLS §8 даёт `python -m vkm_corpus.mcp.acceptance`, координатор — `vkm_corpus.cli mcp acceptance`; оба пути
  работают.
- В ответе провенанса `deletable_without_canonical_loss: true` — константа кода; доказательство — критерий 5.
- 28.09 ~11:25 в рабочем дереве PUBLIC была незакоммиченная работа агента V; на таком дереве
  `tests/corpus/test_contracts_vocab.py` падал (11 кодов `E_*` вне `ErrorCode`). Поэтому тесты агент I гонял на
  чистых клонах закоммиченного HEAD; в закоммиченном коде тест проходит.
- На CORE каталог `$VKM_DATA_ROOT/logs/` пуст: журналы API/MCP — JSON lines в docker json-file (50 MB × 5).
  OPERATIONS §3 обещает файлы `<VKM_DATA_ROOT>/logs/<service>.jsonl`; на WORKSTATION stdio-серверы их пишут.
- Роль `vkm_ops` в PostgreSQL без суперпользователя, без CREATEDB и CREATEROLE (H-42) — проверено.
- Архив образов EDGE (H-10): оба `.tar` на диске данных, sha256 совпали с `SHA256SUMS` и с квитанцией F.

## Общий вердикт

**READY.** Основание: все 14 критериев брифа — PASS на итоговом снимке `snap-20260928T113713Z-fa0aa127`, три FAIL,
найденные при проверке (FAIL-1, FAIL-3, FAIL-4), исправлены и проверены на данных и живых сервисах.

Что остаётся открытым и видно в отчёте:

- FAIL-5 — smoke Q5 (буст номера рисунка) не проходит на полном корпусе; это дефект релевантности, не пункт §59;
- D-1 — библиография (§24, «Work → citations» §58) отложена решением CP-38;
- NOT_RUN — живые тесты, пишущие в сервисы; прогон `claude -p` (CP-35); чтение открытого чертежа AutoCAD;
  одновременная пара реранкеров после обновления gateway до 0.1.1.

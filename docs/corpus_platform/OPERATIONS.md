# VKM Corpus Platform v0 — эксплуатация

Ежедневная работа с платформой: обработка источников, публикация на CORE, пересборка проекций, наблюдение,
резервные копии и восстановление после сбоев. Установка — [DEPLOYMENT.md](DEPLOYMENT.md); устройство —
[CORPUS_PLATFORM_ARCHITECTURE.md](CORPUS_PLATFORM_ARCHITECTURE.md); контракты данных — [DATA_CONTRACTS.md](DATA_CONTRACTS.md).

Команды ниже — CLI `vkm-corpus`. На WORKSTATION они запускаются в WSL из venv pipeline с переменными producer; на
CORE — внутри одноразового контейнера `vkm-job`:

```bash
docker compose --env-file .env --profile jobs run --rm vkm-job <команда vkm-corpus без имени программы>
```

## 1. Обычный цикл

```
registry verify → registry import → run plan → run extract → run status
  → core publish (WORKSTATION) → core reconcile (CORE) → проверка проекций
```

| Шаг | Где | Команда | Что делает |
|---|---|---|---|
| 1 | WORKSTATION | `vkm-corpus registry verify` | проверяет наличие, размер и SHA-256 каждого файла реестра |
| 2 | WORKSTATION | `vkm-corpus registry import` | строит наборы Source и Work из `SOURCE_REGISTER.csv` и курируемых таблиц `00_registry/work_registry/` и коммитит их в STAGING |
| 3 | WORKSTATION | `vkm-corpus run plan --source VKM-SRC-044` (или `--canary`) | план без записи: что будет пересчитано и сколько вызовов моделей |
| 4 | WORKSTATION | `vkm-corpus run extract --source …` | извлечение → layout → OCR → коммит источника |
| 5 | WORKSTATION | `vkm-corpus run status` | сводка последнего прогона: статусы страниц, ошибки, вызовы моделей |
| 6 | WORKSTATION | `vkm-corpus core publish --target core:/srv/vkm/data` | копирует новые неизменяемые файлы на CORE; маркеры коммитов — последними |
| 7 | CORE | `core reconcile --run-id <RUN>` | допуск коммитов → снимок и валидатор → DuckDB → Neo4j (wipe) → OpenSearch |
| 8 | CORE | `search smoke`, `graph verify` | русский smoke поиска и проверки графа C1–C16 |

Полезные опции `run extract`: `--no-ocr` (страницы остаются `OCR_REQUIRED`), `--concurrency N` (запросов OCR
одновременно), `--max-model-calls N` (потолок вызовов модели), `--failed-only` (только источники с незавершённой
головой), `--resume LATEST` (продолжить упавший прогон), `--page A-B` (диапазон страниц одного источника).

Повторный прогон неизменённых страниц не вызывает модели: решение принимают `stage_signature` и `call_signature`.
`--force` пересчитывает строки с использованием кеша моделей. Повторный вызов модели для уже распознанных страниц —
только `--recall-model` через подтверждённый план: сначала `run extract --plan-only …`, затем тот же запуск с
`--confirm-plan <plan_sha256>`.

`--dry-run` у `core publish` показывает, что будет скопировано. Проекции строятся только из `CURRENT`
CANONICAL-корня; `CURRENT` сдвигается только при PASS валидатора. Отчёт каждого reconcile —
`$VKM_DATA_ROOT/receipts/reconcile/<RUN>.json`. Итог в нём: `RECONCILED` — всё собрано; `RECONCILED_WITH_FAILURES` —
снимок принят, но одна из проекций не собралась (остальные собраны, упавшая названа в шагах); `SNAPSHOT_NOT_PASSED_PROJECTIONS_UNCHANGED` — валидатор не пропустил снимок; `FAILED` — сбой допуска или снимка.

## 2. Задания через API и MCP (plan-first)

Агенты не запускают обработку напрямую. Они ставят задание (`REPROCESS_SOURCE`, `REPROCESS_PAGE`) в PostgreSQL
`vkm_ops` и проходят цепочку:

```
PLAN_REQUESTED → PLANNED → CONFIRMED → RUNNING → PUBLISHED → ADMITTED | REJECTED
```

План строит только воркер на WORKSTATION. Подтверждение привязано к `plan_sha256`: если к моменту исполнения план
изменился, подтверждение сбрасывается и задание возвращается в `PLANNED`. Отмена возможна до `RUNNING`.

Воркер (WSL, окружение producer) берёт по одному заданию с арендой и продлевает её heartbeat-потоком:

```bash
vkm-corpus ops worker
```

Ему нужны `VKM_PUBLISH_TARGET` (CANONICAL-корень, `core:/srv/vkm/data`) и `VKM_RECONCILE_CMD` — команда запуска
reconcile на CORE с подстановкой `{run_id}`, например
`ssh core 'cd /srv/vkm/infra/core && docker compose --env-file .env --profile jobs run --rm -T vkm-job core reconcile --run-id {run_id}'`.
Исход задания определяется по квитанции reconcile: коммиты задания отклонены — `REJECTED`; снимок PASS — `ADMITTED`
(сбой отдельной проекции записывается в примечание); иначе — `FAILED` с записью в `ops.error`.

Те же операции, что у API и MCP admin, доступны из командной строки:

| Действие | Команда |
|---|---|
| запросить план | `vkm-corpus ops request --kind REPROCESS_SOURCE --source VKM-SRC-044 [--option force]` |
| посмотреть задание и план | `vkm-corpus ops show <job_id>` |
| подтвердить план | `vkm-corpus ops confirm <job_id> --plan-sha256 <hash>` |
| отменить | `vkm-corpus ops cancel <job_id>` |
| сводка и список | `vkm-corpus ops status`, `vkm-corpus ops jobs --state PLANNED` |

## 3. Наблюдение

| Что | Команда / место |
|---|---|
| корень данных: вид, `CURRENT`, ожидающие и отклонённые коммиты, упавшие прогоны, аренды | `vkm-corpus canon status` |
| валидатор снимка | `vkm-corpus canon validate [--deep] [--acceptance --expected-sources 251]` |
| DuckDB против `CURRENT` | `vkm-corpus duckdb status` |
| граф | `vkm-corpus graph status`, `vkm-corpus graph verify` |
| поиск | `vkm-corpus search status`, `vkm-corpus search smoke` |
| реранкеры | `vkm-corpus rerank health`, `vkm-corpus rerank status` |
| задания, воркеры, ошибки воркеров, состояние сервисов | `vkm-corpus ops status` |
| журналы сервисов | JSON lines `<VKM_DATA_ROOT>/logs/<service>.jsonl` (ротация 20 MB × 10); поля `run_id`, `job_id`, `source_id`, `page_id`, `stage`, `duration_ms`, `status`, `error_code` |
| журналы контейнеров | `docker compose logs <service>` (json-file, ротация) |

## 4. Пересборка проекций

Проекции не являются источником истины; их можно удалить и собрать заново из снимка.

| Проекция | Команда (на CORE, в `vkm-job`) |
|---|---|
| DuckDB | `duckdb build` |
| Neo4j | `graph ddl`, затем `graph rebuild --mode wipe` |
| OpenSearch | `search build --smoke --prune` |
| откат поиска на предыдущую сборку | `search rollback` |
| все три по порядку | `core reconcile --run-id <RUN>` |
| векторы (dense) | `search export-units`, затем `search build-vectors --embeddings <каталог или отчёт encode>` (§4a) |

`graph rebuild --plan-only` и `search build --plan-only` печатают ожидаемые числа без записи.

### 4a. Векторы и гибридный поиск (лаборатория retrieval, этап 2)

Канон эмбеддингов — derived-артефакты `derived/embeddings/dense/<модель>/<ревизия>/<подпись>/` (агент K); индекс
`<prefix>-vectors-m1-<build>` за алиасом `<prefix>-vectors` — пересобираемая проекция без GPU.

| Шаг | Где | Команда |
|---|---|---|
| единицы `vkm-units-v1` снимка CURRENT → `derived/embeddings/units/<снимок>/vkm-units-v1-A/` | `vkm-job` | `search export-units` |
| эмбеддинги всех единиц (уже встроенные с той же подписью пропускаются, §46) | `rx580-retrieval` (`exec`) | `python -m vkm_corpus.embeddings.cli encode --config /config/rx580.json --docs <units>/docs.jsonl --data-root /data --roles dense --skip-validate` |
| проверки §64 (потоково) + индекс + смена алиаса | `vkm-job` | `search build-vectors --embeddings <отчёт encode или каталог> [--snapshot <CURRENT>] [--skip-if-current]` |
| состояние | `vkm-job` | `search status` (раздел `vectors`: сборка, снимок, подпись, модель, число векторов) |
| запрос с трассой | API / MCP | `POST /v1/search/hybrid`, tool `search_hybrid` / `retrieval_trace`; CLI `search hybrid "<запрос>"` |

`build-vectors` отказывает (алиас не трогается), если единицы не от снимка CURRENT, эмбеддинги неполны (пропуски,
дубли, чужая подпись, NaN, норма, контрольные суммы частей) или подпись не dense. Строки единиц, ушедших из канона, —
история (`orphaned` в квитанции), в проекцию не попадают. Старые сборки удаляются только по точному имени; остаются
текущая и предыдущая (откат — переставить алиас `_aliases` на предыдущий индекс). API берёт вектор запроса у сервиса
RX580 (`VKM_EMBED_URL`) и сверяет модель и размерность со сборкой; без сервиса или сборки — `DEPENDENCY_UNAVAILABLE`.

Весь этап без участия человека выполняет `infra/core/lab_stage2.sh` (идемпотентен, один запуск за раз):

```bash
# на CORE, в каталоге compose (там же .env); модель — lab_stage2.json (по умолчанию пример: granite-311m-r2 Q8_0)
systemd-run --user --unit vkm-lab-stage2 --collect bash <каталог compose>/lab_stage2.sh [--after-snapshot <старый CURRENT>]
cat <корень данных>/receipts/lab_stage2/STATUS          # одна строка: RUNNING step=… | DONE … | FAILED step=…
journalctl --user -u vkm-lab-stage2 -f                  # журнал; полный — receipts/lab_stage2/<run>/run.log
```

Квитанция прогона — `receipts/lab_stage2/<run>/receipt.json` (и `latest.json`): снимок, единицы, план и скорость
кодирования, проверки §64, сборка индекса, результат smoke (3 русских запроса через API, у каждого ≥ 1 попадание с
трассой). Повторный запуск ничего не пересчитывает: экспорт — `EXISTS`, кодирование — 0 новых единиц, индекс —
`SKIPPED_CURRENT`.

## 5. Резервные копии

| Что | Как | Нужно ли |
|---|---|---|
| канон CORE (`canonical/`, `artifacts/`) | `vkm-corpus core backup --source core:/srv/vkm/data --archive <каталог архива>` на WORKSTATION; манифест SHA-256 | да, после каждого значимого reconcile |
| STAGING | остаётся на WORKSTATION; повторная публикация восстанавливает CORE | — |
| PostgreSQL `vkm_ops` | `pg_dump` базы `vkm_ops` | по желанию: теряется только история заданий |
| DuckDB, Neo4j, OpenSearch | не копируются — пересобираются (§4) | нет |
| сырьё | PRIVATE Git + LFS | да (Git) |

## 6. Восстановление после сбоев

| Симптом | Действие |
|---|---|
| прогон упал на середине | `vkm-corpus run extract --resume LATEST` (готовые страницы не пересчитываются) |
| аренда источника занята мёртвым процессом | `vkm-corpus canon lease-break <ключ>` (ключ — в `canon status`) |
| блокировка admit/snapshot осталась после падения | `vkm-corpus canon unlock` |
| коммит отклонён при допуске | причина — в `canon status` и в отчёте reconcile; исправить на STAGING, прогнать источник заново, опубликовать |
| валидатор снимка FAIL | `CURRENT` не сдвинулся, проекции остались на прошлом снимке; причина — в отчёте валидатора |
| мусор после аварий | `vkm-corpus canon gc` (список), `vkm-corpus canon gc --delete` (удаление старше 24 ч) |
| граф или поиск в неизвестном состоянии | пересборка по §4 |
| OCR резко замедлился (сотни ток/с вместо тысяч) | проверить, не ушла ли память vLLM в общую память Windows («GPU Process Memory», shared у `vmwp`); закрыть тяжёлые GPU-приложения рабочего стола; доля `--gpu-memory-utilization` в compose GLM-OCR — 0.66 |
| сработал аварийный стоп OCR (CP-22) | прогон останавливается сам при `finish_reason=length` > 2 %, пустых ответах на странице с «чернилами» > 1 % или повторах > 1 % в окне 200 вызовов; разобрать примеры из `OCR_RAW`, исправить конфигурацию, перезапустить `--resume` |

## 7. Чего не делать

- Не перезапускать и не менять существующий текстовый реранкер на EDGE (исторически `careerops-reranker`).
- Не выполнять `docker system prune` и массовую чистку томов на хостах.
- Не класть сырой корпус и полные тексты в PostgreSQL, Neo4j или OpenSearch сверх того, что задают проекции.
- Не запускать OCR мимо pipeline: все вызовы модели идут через кеш вызовов и оставляют `OCR_RAW`.
- Не выгружать и не перезапускать GLM-OCR ради бенчмарков.
- Не коммитить данные выполнения, `.env`, `.mcp.json`, секреты; не печатать секреты в журналы и отчёты.
- Не повышать статус автоматических объектов: всё извлечённое автоматически остаётся
  `AUTO_EXTRACTED_UNREVIEWED`; ревью — отдельная задача (`ops.review_task`), не часть v0.

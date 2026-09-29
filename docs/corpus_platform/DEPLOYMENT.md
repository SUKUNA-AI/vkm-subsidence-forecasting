# VKM Corpus Platform v0 — развёртывание

Документ описывает, как поднять платформу с нуля на трёх хостах. Роли хостов и поток данных —
[CORPUS_PLATFORM_ARCHITECTURE.md](CORPUS_PLATFORM_ARCHITECTURE.md); ежедневная работа и восстановление —
[OPERATIONS.md](OPERATIONS.md); сервисы моделей на EDGE — [MODEL_SERVICES.md](MODEL_SERVICES.md); инструменты MCP и
подключение клиентов — [MCP_TOOLS.md](MCP_TOOLS.md).

## 0. Общие правила

- **Значения хоста не попадают в Git.** Адреса, имена пользователей, пути хоста и порты публикации лежат в
  git-ignored `.env` рядом с compose-файлом. В репозитории — только `.env.example` с заглушками.
- **Секреты — только файлами.** Каждый секрет читается через переменную `*_FILE` (`VKM_PG_DSN_FILE`,
  `VKM_NEO4J_PASSWORD_FILE`, `VKM_RERANK_TOKEN_FILE`, `VKM_API_TOKEN_FILE` и т. п.). Файлы лежат вне репозиториев с
  режимом 0400/0600: на CORE — в `/srv/vkm/secrets` (каталог 0700), на WORKSTATION — в `.vkm/secrets/` профиля
  пользователя. `vkm-corpus config` печатает настройки с `<set>` вместо значений секретов.
- **Данные выполнения не лежат в Git-деревьях.** `VKM_DATA_ROOT` внутри рабочего дерева Git или внутри клона PRIVATE
  отвергается при запуске (`ConfigError`).
- **Образы — по digest, модели — по ревизии.** Таблица версий — §5. Модели скачиваются и проверяются
  `scripts/fetch_models.py` по [infra/models/model_pins.json](../../infra/models/model_pins.json).
- **Установка Python-пакетов** всегда с ограничением `-c requirements/worldspec.lock.txt`: pydantic и pydantic-core
  не сдвигаются (от них зависит схема WorldSpec, CP-02).

## 1. WORKSTATION — единственный producer

Windows 11 + WSL `archlinux` (pipeline) + Docker Desktop с GPU (GLM-OCR, рендер DOCX). RTX 5070 Ti 16 GB.

### 1.1 WSL и окружение pipeline

Пакеты Arch: `uv`, `rsync`, `openssh`, `djvulibre`, `git`, `git-lfs` (проверено: uv 0.12.19, rsync 3.5.1,
DjVuLibre 3.5.30, git-lfs 3.8.0). Python 3.13 ставит uv.

```bash
uv venv ~/vkm/venv-corpus --python 3.13
uv pip install -p ~/vkm/venv-corpus --index-url https://download.pytorch.org/whl/cu130 --extra-index-url https://pypi.org/simple --index-strategy unsafe-best-match -c requirements/worldspec.lock.txt -r requirements/corpus-layout.lock.txt
uv pip install -p ~/vkm/venv-corpus --no-deps -e .
```

Точные версии: `requirements/corpus.lock.txt` (без GPU-части), `requirements/corpus-layout.lock.txt` (pipeline с
layout-моделью, колёса CUDA 13.0), `requirements/corpus-services.lock.txt` (образ CORE). Все три собраны из
проверенных окружений и ограничены `requirements/worldspec.lock.txt`.

Переменные окружения producer (удобно держать в скрипте-обёртке вне репозитория):

| Переменная | Значение |
|---|---|
| `VKM_DATA_ROOT` | STAGING-корень, например `~/vkm/staging` (вне Git) |
| `VKM_DATA_ROLE` | `producer` |
| `VKM_RESOURCES_ROOT` | клон PRIVATE (реестр, сырьё, `00_registry/work_registry/`) |
| `VKM_MODELS_DIR` | каталог моделей (вне Git) |
| `VKM_OCR_URL` | адрес GLM-OCR на loopback, `http://127.0.0.1:8080` |
| `VKM_PG_DSN_FILE` | файл DSN базы `vkm_ops` на EDGE |
| `HF_HUB_OFFLINE`, `TRANSFORMERS_OFFLINE` | `1` — pipeline не ходит в сеть за моделями |

Инициализация STAGING-корня (один раз):

```bash
vkm-corpus canon init --kind STAGING
```

### 1.2 Модели

Скачивание работает из WSL (из Windows хаб недоступен). После скачивания — офлайн-проверка:

```bash
python scripts/fetch_models.py fetch --models-dir "$VKM_MODELS_DIR"
```

```bash
python scripts/fetch_models.py verify --models-dir "$VKM_MODELS_DIR"
```

Квитанции пишутся в `<VKM_MODELS_DIR>/receipts/`. Кандидаты лаборатории retrieval закрепляются её собственным
реестром моделей.

### 1.3 GLM-OCR (Docker Desktop)

Compose: [infra/workstation/glm-ocr/compose.yml](../../infra/workstation/glm-ocr/compose.yml). Контейнер vLLM
работает во внутренней сети без выхода наружу; наружу смотрит только прокси на `127.0.0.1:8080`. Веса монтируются
только для чтения из `${VKM_MODELS_DIR}`.

1. Скопировать `.env.example` в `.env` (git-ignored) и указать `VKM_MODELS_DIR`.
2. Запуск и проверка:

```bash
docker compose -f infra/workstation/glm-ocr/compose.yml --env-file infra/workstation/glm-ocr/.env up -d
```

```bash
vkm-corpus ocr check
```

```bash
vkm-corpus ocr smoke
```

`--gpu-memory-utilization=0.66`: при 0.80 хвост KV-кеша уходил в общую системную память Windows и генерация
замедлялась в десятки раз (рабочий стол занимает ≈ 3,2 GB VRAM, layout-модель ≈ 1,2 GB).

### 1.4 Рендер DOCX

Закреплённый рендерер LibreOffice 24.2 для DOCX-источника (страницы `rNNNN`, CP-08):
[infra/workstation/libreoffice/](../../infra/workstation/libreoffice/README.md). Образ собирается один раз, дальше
используется по digest; профиль шрифтов записывается в каждую квитанцию рендера.

### 1.5 Доступ к CORE и EDGE

У WSL свой SSH-ключ публикатора, разрешённый на CORE и EDGE (добавляет владелец хостов). `~/.ssh/config` в WSL
содержит хосты `core` и `edge`. Публикация на CORE — `vkm-corpus core publish` (rsync поверх SSH).

### 1.6 Desktop MCP

MCP-серверы draw.io и Autodesk-моста работают на WORKSTATION в Windows; подключение клиентов и список инструментов —
[MCP_TOOLS.md](MCP_TOOLS.md). Локальный `.mcp.json` не коммитится; образец — `.mcp.json.example`.

## 2. CORE — CANONICAL-корень и проекции

Debian 13, Docker 26.1 и Compose 2.26 (пакеты Debian).

### 2.1 Каталоги и секреты

| Путь | Назначение | Владелец и режим |
|---|---|---|
| `/srv/vkm/data` | `VKM_DATA_ROOT`: `canonical/`, `artifacts/`, `duckdb/`, `neo4j/`, `opensearch/`, `receipts/`, `logs/`, `tmp/` | пользователь-публикатор, 0750 (`neo4j/` — uid 7474) |
| `/srv/vkm/secrets` | файлы секретов compose | root, 0700; каждый файл 0400, владелец — uid сервиса, который его читает |
| `/srv/vkm/infra/core` | копия `infra/core/compose.yml` и host-local `.env` | пользователь-публикатор |

Секрет Neo4j `neo4j_auth` содержит строку `neo4j/<пароль>` и принадлежит uid 7474 (иначе Neo4j не стартует).
Для клиентов Neo4j (API, задания) пароль лежит отдельным файлом, читаемым их uid.

`vm.max_map_count` должен быть не меньше 262144 (на CORE — 1048576).

### 2.2 Сервисы

Compose-проект `vkm-core`: [infra/core/compose.yml](../../infra/core/compose.yml), заглушки —
[infra/core/.env.example](../../infra/core/.env.example).

| Сервис | Назначение | Публикация |
|---|---|---|
| `neo4j` | DOCUMENT graph (Community 5.26.31) | только `127.0.0.1` (админ — через SSH-туннель) |
| `opensearch` | retrieval-индексы (3.8.0, security plugin выключен, loopback) | только `127.0.0.1` |
| `api`, `mcp`, `mcp-admin` | VKM API и VKM Corpus MCP (образ `vkm-corpus-api`) | LAN-адрес CORE, токены |
| `vkm-job` (profile `jobs`) | разовые задания над CANONICAL-корнем: `canon init`, `core reconcile`, `graph ddl` | нет |
| `vkm-nightly` (profile `nightly`) | ночные проверки в 04:00: образ `vkm-job`, корень только на чтение, `cpus: 12`, `cpu_shares: 256`, `mem_limit: 6g` ([OPERATIONS.md §8](OPERATIONS.md)) | нет |

Приложения работают под uid владельца данных (`VKM_DATA_UID`/`VKM_DATA_GID` в `.env`); API и MCP монтируют корень
только для чтения, запись — только у `vkm-job`.

Первый запуск:

```bash
docker compose --env-file .env up -d neo4j opensearch
```

```bash
docker build -f infra/core/api/Dockerfile -t vkm-corpus-api:0.1.0 .
```

```bash
docker compose --env-file .env --profile jobs run --rm vkm-job canon init --kind CANONICAL
```

```bash
docker compose --env-file .env --profile jobs run --rm vkm-job graph ddl
```

```bash
docker compose --env-file .env up -d api mcp mcp-admin
```

Образ API собирается из корня PUBLIC-checkout (контекст сборки ограничен `Dockerfile.dockerignore`: только
`pyproject.toml`, `requirements/`, `src/`).

### 2.3 Retrieval-ускоритель RX 580

Экспериментальный backend лаборатории retrieval (Vulkan, 8 GB VRAM) описан в `infra/core/rx580/`. Он не нужен для
работы документного слоя v0.

## 3. EDGE — control plane и реранкеры

Debian 13, GTX 1650 4 GB.

### 3.1 PostgreSQL `vkm_ops`

Сервер PostgreSQL 18.6 уже работает в контейнере (исторически называется `careerops-postgres`; compose и `.env` —
в нейтральном host-local каталоге, см. [квитанцию вывода CareerOps](CAREEROPS_MIGRATION_RECEIPT.md)). Для платформы
созданы роль и база `vkm_ops` без прав суперпользователя. DSN хранится файлом (`VKM_PG_DSN_FILE`) на WORKSTATION и на
CORE. Схема `ops` создаётся и обновляется идемпотентно:

```bash
vkm-corpus ops init
```

Потеря базы не теряет корпус: пропадает только история заданий и прогонов (§5 архитектуры).

### 3.2 Реранкеры

- Текстовый реранкер Jina v3.5 — существующий сервис; он сохранён как есть и **не перезапускается** платформой.
- Визуальный реранкер Jina m0 (GGUF, llama.cpp CUDA sm_75) и gateway `vkm.rerank/1` — compose
  [infra/edge/compose.yml](../../infra/edge/compose.yml), сборка образов — `infra/edge/build_images.sh`.

Бюджет VRAM, лимиты gateway, parity и приёмка — [MODEL_SERVICES.md](MODEL_SERVICES.md).

## 4. Проверка развёртывания

| Проверка | Команда | Ожидается |
|---|---|---|
| настройки | `vkm-corpus config` | роли и корни верны, секреты `<set>` |
| control plane | `vkm-corpus ops status` | схема `ops-0.1.0`, подключение есть |
| корень данных | `vkm-corpus canon status` | `STAGING` на WORKSTATION, `CANONICAL` на CORE |
| OCR | `vkm-corpus ocr check` | ревизия модели совпадает с pin |
| граф | `vkm-corpus graph status` | сервер 5.26.x, ответ готовности |
| поиск | `vkm-corpus search status` | алиасы указывают на сборку текущего снимка |
| реранк | `vkm-corpus rerank health` | text и visual в состоянии готовности |
| API и MCP | см. [MCP_TOOLS.md](MCP_TOOLS.md) | health 200, список инструментов |

## 5. Закреплённые версии

| Компонент | Версия / pin |
|---|---|
| GLM-OCR (модель) | `zai-org/GLM-OCR` @ `2e85a62840ccac27daa451df36c736c4636b8628` |
| vLLM (образ GLM-OCR) | v0.30.0, `sha256:8a69ffad015f138d7170c4ddc429e230a3bc1c1719f67e14324749df200a4b90` (CUDA 13.0.3) |
| прокси GLM-OCR | `alpine/socat@sha256:5ffbd6ae916cbad86a58fabe0d6d5a6fd5c2b47ddf031e82996baac9300e732f` |
| layout | `PaddlePaddle/PP-DocLayoutV3_safetensors` @ `97d101e6db2642e162a1d05392d1b0231c91033e`; transformers 5.17.0, torch 2.13.0+cu130 |
| извлечение | PyMuPDF 1.28.2, pypdfium2 5.13.0, DjVuLibre 3.5.30, LibreOffice 24.2 (образ по digest) |
| канон | pyarrow 25.0.1, polars 1.44.2, DuckDB 1.5.5, pydantic 2.13.5 / pydantic-core 2.46.5 |
| Neo4j | `neo4j:5.26.31-community@sha256:5eb12ad77fa46ab73e23df9ea1f43f5c0f2a79523435577648e046be042b9b93`; драйвер neo4j 6.3.1 |
| OpenSearch | `opensearchproject/opensearch:3.8.0@sha256:fafe3fc3587088674669235575aa166228c48bdb940294a8cdbbc1da75236a40`; opensearch-py 3.2.0 |
| PostgreSQL | 18.6 (существующий сервер на EDGE); psycopg 3.3 |
| API / MCP | `python:3.13-slim` по digest (см. Dockerfile), FastAPI 0.141, MCP SDK 2.2.0 |
| реранкеры | см. [MODEL_SERVICES.md](MODEL_SERVICES.md) и [model_pins.json](../../infra/models/model_pins.json) |

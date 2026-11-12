# VKM Corpus Platform v0 — бриф реализации (Phase 1–6)

Общий контекст для агентов реализации. Решения: [журнал координатора](COORDINATOR_DECISIONS.md) (особенно CP-15,
CP-16, CP-22…CP-24 и таблица ответов на ревью H), [ревью H](AGENT_H_ARCHITECTURE_REVIEW.md), [постановка](TASK_SPEC_CORPUS_PLATFORM_V0_RU.md),
[бриф Phase 0](00_COORDINATOR_BRIEF.md). Проект каждого агента — его отчёт `AGENT_<X>_*.md` в этом каталоге; там, где
отчёт расходится с CP-16 или ответом на H, побеждает журнал координатора.

## 1. Правила

- Писать только в свои каталоги (CP-24). Общий контракт (`vkm_corpus.contracts`, `vkm_corpus.ids`) меняет только D;
  остальным нужен новый элемент словаря — пишут decision note в свой статус-файл, D добавляет.
- Не коммитить и не пушить: коммитит координатор. Не трогать `vkm_world`, `scripts/`, `evidence/`, `catalogues/`,
  `tests/world/`, PRIVATE (кроме явно разрешённого в задании).
- Тесты — `tests/corpus/`, имя файла с префиксом своей зоны; фикстуры только синтетические, генерируются в `tmp_path`;
  бинарные фикстуры не коммитить. Маркеры: `services`, `gpu`, `desktop` (без сервиса — skip = NOT_RUN).
- Конфигурация — только `vkm_corpus.config.load_settings()`; логи — `vkm_corpus.logs`; CLI — модуль `<pkg>/cli.py` с
  функцией `register(subparsers)` (группы — `vkm_corpus/cli.py`, `GROUPS`). Тяжёлые зависимости импортировать лениво.
- В коде, конфигах и документах — без абсолютных машинных путей, IP, логинов, секретов (тест
  `tests/corpus/test_public_hygiene.py` и `leakage.scan`). Секреты читаются из `*_FILE`.
- Не ломать `python -m pytest tests/world` (база: 206/209 на Windows, 3 известных падения окружения) и
  `tests/corpus`.
- Каждое преобразование оставляет receipt; runtime-данные — только в корнях данных, не в git.

## 2. Статус-файлы и зависимость от контракта

Каждый агент ведёт `work/corpus_platform/impl/STATUS_<X>.md` (git-ignored): готовые части, публичные функции для
других, открытые вопросы. D пишет в `STATUS_D.md` строку `CONTRACTS_READY <время>`, когда `vkm_corpus.contracts` и
`vkm_corpus.ids` стабильны, и `TESTING_READY`, когда готов генератор синтетического канона
`vkm_corpus.testing.synthetic_canon(...)`. До этого остальные пишут код, не зависящий от точных имён полей, и
изолируют отображение в канон в одном модуле.

## 3. Окружение (факты на 28.09.2026)

- WORKSTATION, WSL `archlinux`: venv pipeline `~/vkm/venv-corpus` — Python 3.13.15, pymupdf 1.28.2, pypdfium2 5.13.0,
  pyarrow 25.0.1, polars 1.44.2, duckdb 1.5.5, pydantic 2.13.5/core 2.46.5, numpy 2.5.3, torch 2.13.0+cu130 (RTX 5070 Ti,
  cc 12.0 видна), transformers 5.x, huggingface_hub, httpx, psycopg 3, lxml, pillow, python-docx, pytest; DjVuLibre
  3.5.30 (`ddjvu`, `djvused`, `djvudump`), poppler, gcc, cmake. PUBLIC и PRIVATE видны из WSL через DrvFs-монтирование
  диска клонов (`<wsl-mount-of-clone-drive>/<PUBLIC>` и вложенный клон PRIVATE). Staging-корень pipeline:
  `~/vkm/staging`.
- Windows Python 3.13.13 без этих пакетов: чистые тесты можно гонять и там, всё остальное — в WSL-venv.
- Вызовы WSL: многострочную логику класть в файл-скрипт и запускать `wsl -d archlinux -e bash <скрипт по пути DrvFs>`
  из PowerShell (кавычки в однострочниках ломаются; Git Bash переписывает POSIX-аргументы в Windows-пути).
- Hugging Face доступен только из WSL (из Windows соединение сбрасывается).
- GLM-OCR: vLLM v0.30.0 в Docker Desktop, `http://127.0.0.1:8080` (OpenAI-совместимый, модель `glm-ocr`), доступен из
  WSL и Windows. Снимки моделей с проверенными sha256 — в каталоге моделей WORKSTATION (раздел данных, каталог
  `VKM_MODELS`; из WSL — через DrvFs):
  `zai-org__GLM-OCR/2e85a62840ccac27daa451df36c736c4636b8628`, `PaddlePaddle__PP-DocLayoutV3_safetensors/97d101e6…`,
  `MODELS_RECEIPT.json`.
- CORE: Neo4j 5.26.31 Community (`bolt://127.0.0.1:7687`) и OpenSearch 3.8.0 (`http://127.0.0.1:9200`) — только
  loopback CORE; с WORKSTATION — SSH-туннель `ssh -N -L 17687:127.0.0.1:7687 -L 19200:127.0.0.1:9200 core`. Пароль
  Neo4j — файл на CORE `/srv/vkm/secrets/neo4j_auth` (формат `neo4j/<пароль>`); не печатать, не копировать в git.
  Канонический корень — `/srv/vkm/data`.
- SSH: разрешены команды вида `ssh core …`, `ssh edge …`, `ssh -o BatchMode=yes core …`, `scp`, `rsync` (без
  префиксов вроде `timeout`: таймаут задаётся параметром инструмента).
- EDGE: существующий text-реранкер — контейнер `careerops-reranker` (KEEP, не трогать), `127.0.0.1:18082`, `POST
  /v1/rerank`, `GET /readyz`; NVIDIA Container Toolkit 1.20.0, CUDA 13.2, драйвер 595.91.07; GTX 1650, доступно 3716 MiB.

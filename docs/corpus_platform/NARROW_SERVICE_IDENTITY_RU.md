# Identity узких сервисов и установка wheel

PW-01 отделяет identity процесса RERANK/RETRIEVAL от identity полного API.
`api.production` и его зависимости не импортируются узкими providers.
CONTROL остаётся частью API: его реальный backend — `PgControlPlane`.

## Профили

`update/service_identity.py` содержит закрытые профили:

- `RETRIEVAL_LLAMA_TOKENIZERS_V1`: `QueryEncoder` → `SpecTokenizer` (Rust
  `tokenizers`) → принадлежащий сервису `ModelProcess` / `LlamaServerClient`;
  NumPy pooling и heads, PyArrow чтение late pack, FastAPI/HTTPX/uvicorn.
- `RERANK_HTTP_TOKENIZERS_V1`: `TextBackend` / `VisualBackend` через HTTP,
  локальные tokenizers, NumPy scoring head и Pillow preprocessing.

Каждый профиль перечисляет конкретные Python modules, включая используемые
contracts и native helpers. Хеши берутся из фактически установленного кода.
Отсутствующий module, не обычный файл, неподдерживаемый backend/encoder или
изменение кода закрывают qualification. Список не берётся из operator JSON.

`dependency_inventory()` проверяет реальные imports прямых capabilities и
полную активную PEP 508 closure установленной metadata: versions, requirements,
markers, extras. Пропущенная транзитивная зависимость, отсутствующее native
extension или несовместимая версия не превращаются в пустую identity.
В состав входят зависимости tokenizers, включая huggingface-hub; runtime pins
зафиксированы в `infra/edge/gateway/requirements.txt` и
`requirements/retrieval-service.lock.txt` для CPython 3.13 Linux.
Известные автоматически выбираемые transports uvicorn (uvloop, httptools,
websockets/wsproto) и decoders HTTPX (brotli/brotlicffi/zstandard) учитываются
отдельно: ABSENT либо PRESENT с полной dependency closure. Их появление меняет
identity; найденный, но неисправный native import блокирует qualification.

API применяет общий closure checker со своим закрытым `API_ALL_STORES_V1`.
Включены реальные Neo4j/OpenSearch/PostgreSQL clients, `psycopg[binary]`, reader
packages и navigation morphology/dictionaries. Только успешный импорт psycopg
недостаточен: требуется actual binary implementation. Serving startup отдельно
проверяет cached morphology всех query readers; тихая деградация в crude/snowball
не допускается под прежним production acceptance. Legacy readers сохраняют fallback.
Ни incoming manifest, ни service config не выбирают произвольный список imports.

Torch/transformers/safetensors **не являются runtime текущего RX580 Python
encoder или HTTP gateway**. Они нужны отдельному HF/model-owner профилю, если
этот профиль используется; reference/parity runners не запускаются сервером.
Неизвестный backend не может наследовать identity текущего профиля.
Внешние text/visual owners по-прежнему обязаны дать `NativeModelProof` с полной
собственной dependency/code/resource identity. Отсутствие их authenticated
providers остаётся BLOCKED; изменение упаковки не создаёт такого доказательства.

## Установка

Gateway image теперь строит полный project wheel из `pyproject.toml` и `src/`,
устанавливает его без editable mode/PYTHONPATH и проверяет imports + inventories.
Прежняя частичная копия `retrieval/` не содержала native helpers и contracts.
Полный wheel не означает установку всех optional dependencies проекта.
Существующий RX580 wheel получает dependency closure своего профиля; identity
больше не требует неиспользуемых duckdb/mcp/Pillow только из-за общего API helper.

Build receipts сохраняют pip freeze, installed module hashes и
`service-identity.json`. Реальная сборка Docker, deployment, запуск моделей,
паритет и полный MCP acceptance этим изменением не выполняются.

## Проверки и пределы

`tests/corpus/test_service_wheel.py` копирует только build inputs в temp,
строит wheel существующим setuptools, offline устанавливает только этот wheel
существующим uv/pip в отдельный target и запускает `python -I` без PYTHONPATH.
Probe запрещает imports API/duckdb/mcp/Torch, использует реальные локальные
tokenizer/head loaders и synthetic HTTP model proofs; никакой inference.
Process/watch doubles изолируют именно упаковку на всех ОС. Проверка native
filesystem/process этим тестом имеет статус NOT_RUN и покрывается отдельными
environment-accounted тестами; installed-package PASS её не заменяет.

Identity fingerprints — код/зависимости, не качество модели. Сохраняются прежние
load-time file watches, loaded object checks, pack hashes, process/mmap/socket
proofs и actual downstream clients. Python implementations и installed libraries
должны быть operator-owned immutable на время процесса; изменение версии или
исходников требует нового service process/acceptance. Это не защита от
привилегированного изменения памяти и не аттестация всех native OS shared libraries.
llama binary, model weights и resource identities проверяются отдельным owned
process lease; OS/Vulkan driver qualification остаётся частью deployment profile.

Rollback к прежней image выполняется только generation/deployment процедурой.
Старый acceptance hash не подходит новым code/dependency identities. Исходные
данные, embeddings и late pack для code-only изменения не пересчитываются.

# PW-03 TEXT: конкретная load-time интеграция

04.10.2026. Code integration подготовлена; actual image/model/service qualification
**NOT_RUN**. Действующий text v3.5 по README — **UNAVAILABLE**; CORE `rerank_text`
использует mLateOn. Ни этот entrypoint, ни тесты не изменяют route/fallback и не
делают отключённый text owner обязательным READY.

## Фактические исходники и ограничения

CareerOps `careerops_reranker/runtime.py`, `server.py`, `config.py` прочитаны
адресно без изменения external tree. В root/src отсутствуют AGENTS.md/CLAUDE.md.
LICENSE — Apache-2.0; native sources в PUBLIC не копировались. Их LF hashes
зафиксированы в [source_pins.json](../../infra/edge/careerops-text-owner/source_pins.json).
Это проверенные исходники, а не подтверждение текущего deployed image или загрузки.

Исходный Dockerfile использует `pytorch/pytorch:2.14.0-cuda13.2-cudnn9-runtime`,
venv на Python 3.12, Torch 2.14.0 и Transformers 4.57.3. Runtime требует CUDA,
float16, pinned model/code/tokenizer revisions. Defaults сети — `0.0.0.0:18082`.
Фактические loaded weights, VRAM, model quality/parity и текущий process не проверены.

PUBLIC требует Python >=3.13,<3.14. Existing CareerOps image нельзя объявить
совместимым или просто установить в него PUBLIC wheel. Для isolated owner задан
**отдельный exact Python 3.13.13 image contract**. Existing services не заменяются.

## Реализованная интеграция

[text_owner_bridge.py](../../src/vkm_corpus/update/text_owner_bridge.py) — новый
installed entrypoint, импортирующий настоящие native runtime/handler/config.
Он проверяет hashes actual native files и наличие offline patch до loader.

1. Complete immutable inventory и mutation watch устанавливаются до загрузки.
2. `TextRuntimeOwner.load` вызывает настоящий `JinaRerankerRuntime.load` и
   устанавливает результат в **тот же `server.runtime`**, который читает native
   `_RerankerHandler`. Retrospective attach запрещён.
3. Native request schema, exact token budgeting, truncation, scoring и ответы
   выполняются исходным handler/runtime. Model/dtype/device/revisions не меняются.
4. Handler окружён owner checks. Ответ сохраняется до post-inference проверки;
   mutation не может дать успешный buffered ответ. Runtime/model/tokenizer/
   formatter/identity/config/handler/методы и resource files остаются привязаны.
5. Authenticated `GET /identity` находится в **этом же handler/process** и выдаёт
   актуальный `NativeModelProof`; readyz, nearby digest и sidecar не используются.
6. Identity token — отдельный mode-600 host-local файл; duplicate Authorization
   headers и неверные credentials получают 401; invalid owner получает 503.
   Responses имеют `Cache-Control: no-store`; request logs не печатают URLs,
   headers, bearer bytes или private inputs.
7. `healthz`, `readyz` и inference тоже не выдают успешный статус closed/invalid
   owner. Default limit — восемь клиентов, read timeout — 20 секунд; bounds
   включены в recipe. SYNTHETIC использует только code-owned memory container,
   не принимает server factory и не имеет transport/listener. Его `/identity`
   возвращает 503 с отдельным `SYNTHETIC_UNQUALIFIED`, никогда NativeModelProof.

## Offline loading и полный inventory

[local-load.patch](../../infra/edge/careerops-text-owner/local-load.patch) применяется
**только к independently prepared copy** native sources. Source/target hashes
обязательны. Patch добавляет keyword-only `model_snapshot`, `tokenizer_snapshot`,
`local_files_only`; bridge задаёт последний True. Legacy defaults сохраняются.

Полный model/tokenizer snapshot и prepared `HF_MODULES_CACHE` должны содержать
ordinary files без symlink/hardlink aliases. Все configs, tokenizer files,
indexes, shards и custom Python code перечисляются с SHA-256. Недостающий shard,
лишний файл и непривязанный runtime/formatter/model implementation блокируют load.
Empty `__init__.py` допустим; пустые model resources — нет. Производственный
weights/tokenizer hash должен совпадать с существующими `retrieval.pins.TEXT`.

PUBLIC bridge выбирает собственный helper closure: bridge, `model_owner`,
`remote_models`, mutation/process/file helpers, recipe/token validators и их
импортируемые PUBLIC зависимости. Все эти actual installed files обязательны
в implementation inventory до loader. Удаление bridge/helper из supplied recipe
не уменьшает проверяемый closure. В production дополнительно обязателен hash
actual resolved Python executable; другой interpreter не получает proof.

`HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, exact `HF_MODULES_CACHE` обязательны.
Cache готовится до campaign/owner start и монтируется read-only. Его генерация,
downloads и установление полных actual model/code inventories здесь **NOT_RUN**.
Если actual HF loader пытается изменить cache или загружает custom implementation
вне исходного inventory, startup fail-closed; это не обходится сохранённым digest.

## Конкретный image/packaging contract

`TextOwnerImagePlan` имеет статус **PLANNED_NOT_BUILT**. Он связывает digest
совместимого base image, independent base verification, Dockerfile, PUBLIC wheel,
scoped native wheel, полный hashed dependency lock и local dependency wheels.
`require_text_owner_image_plan` перечитывает exact original bytes immutable inputs
и native wheel inventory/code/dependency metadata, LICENSE, WHEEL и полный RECORD.
JSON formatting входит в BoundFile hash. Supplied base-verification document
является неизменяемым metadata claim; helper не аутентифицирует его producer,
не инспектирует image и не заменяет independent runtime/image verifier.
Этот claim не является image READY или loaded-model proof.

Точная среда: CPython 3.13.13, Torch 2.14.0 (возможен wheel suffix +cu132),
CUDA 13.2, Transformers 4.57.3, Pydantic 2.13.5. PUBLIC dependencies удовлетворяются
полным offline lock и `pip check`; project Python constraint не меняется.

Native wheel строится в отдельном staging по
[native-package/pyproject.toml](../../infra/edge/careerops-text-owner/native-package/pyproject.toml).
Он содержит только четыре native `careerops_reranker` Python files, dist metadata
и Apache LICENSE; full CareerOps processing/database stack не входит в package.
Имя: `careerops_reranker_native-0.1.0-py3-none-any.whl`. PUBLIC wheel:
`vkm_world-0.1.0-py3-none-any.whl`. Подготовленные inputs сопровождаются receipt.

[Dockerfile](../../infra/edge/careerops-text-owner/Dockerfile) требует immutable
`QUALIFIED_BASE_IMAGE@sha256`, проверяет точные Python/CUDA/dependency версии
**до** установки, устанавливает только prepared offline wheels с `--no-index`,
hashed lock и `pip check`, затем запускает bridge. Старый Python 3.12 base
fail-closed. Read-only snapshots/authority, separate token и prepared cache
передаются isolated service profile. Immutable compatible base digest,
wheels/lock/image build и actual image qualification здесь **NOT_RUN**.
Exact dependency versions также не доказывают неизменность всех installed
dependency files. Полная независимая проверка clean pinned image/installed wheels
остаётся runtime gate; bridge не выдаёт version metadata за такое доказательство.

## Следующий запуск после отдельных gates

1. Подтвердить owner requirement: isolated optional text route, без takeover
   действующего shared CareerOps или изменения CORE late fallback.
2. Из approved original code подготовить hashed patched native source copy,
   scoped wheel, clean PUBLIC wheel, complete lock и compatible immutable base.
   Проверить `TextOwnerImagePlan`; выполнить отдельно разрешённый build.
3. Зафиксировать unchanged actual runtime config, complete original snapshots/
   cache inventory, exact dependencies/Python и dedicated native-identity token.
4. Подать recipe через `--recipe` и `--recipe-sha256`; конфиг не выбирает loader,
   executable или HTTP handler. Только installed code вызывает native loader.
5. Bounded isolated start: actual loaded proof, same-client identity/inference,
   VRAM/functional/parity, restart/reload/invalidations, coherent-generation and
   required MCP acceptance. Ни CPU fixture, ни image metadata этого не заменяют.
6. Integration с serving profile и production switch — отдельно после backup,
   isolated qualification и owner permission. Rollback возвращает coherent
   previous; существующие disabled-text/late semantics сохраняются.

## CPU qualification

[tests](../../tests/corpus/test_text_owner_bridge.py) берут actual native source
только при явном `VKM_TEXT_OWNER_TEST_SOURCE_ROOT`; перед исполнением проверяют
original source hashes и точный patch. Native source copies живут в test temp,
не в PUBLIC. При отсутствии источника результат **NOT_RUN**, а не fake PASS.
Actual-source cases имеют `native_source`; hosted CPU checks явно deselect этот
marker с учётом NOT_RUN, без module-wide skip allowance. Image-plan raw-input
unit cases не имеют marker и выполняются без external tree. ZIP wheel fixtures
проверяют consumer, не являются package build/install или image acceptance.

Настоящие `BaseHTTPRequestHandler`, request validators, token budgeting и runtime
rerank исполняются через memory socket с tiny model/tokenizer. Один тест вызывает
настоящий patched loader с synthetic torch/transformers API, проверяя unchanged
device/dtype/revisions, local snapshot paths и отсутствие remote fallback.
Нет listening server, модели, CUDA работы, dependency installation или image build.

Обязательные negatives: до-/во-время-/после-load mutation, stale runtime/
tokenizer/formatter/identity, token/config replacement, non-ASCII/duplicate auth,
incomplete inventory/shards, source drift, Python/dependency mismatch,
ретроспективное подключение, synthetic→production, token budget/schema/revision
errors и credentials в request logs. Наличие test PASS означает только эту
CPU source-integration квалификацию. Actual deployment остаётся **NOT_RUN**.

Проверки 04.10.2026 после fail-closed synthetic/helper-closure fixes:

- Windows CPython 3.13.13: **50 PASS**, 2.54 с.
- Existing WSL Linux venv: **50 PASS**, 31.42 с.
- Без external source, explicit `-m "not native_source"`: **6 PASS**,
  **44 deselected / NOT_RUN**, 0.41 с; ни один skip не скрыт как PASS.

Это targeted CPU проверки только этого модуля. Full suite, actual image/build,
listening service, GPU/model load, MCP runtime acceptance и production switch
в этих результатах не выполнялись.

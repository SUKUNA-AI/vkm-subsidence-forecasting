# Закрытый CORE operator: контракт и границы квалификации

Реализация: `vkm_corpus.update.operator`, `operator_units`, `receiver`;
CLI `vkm-corpus deployment`. Контракты опубликованы в `schemas/evidence/`:
`core_operator`, `core_unit_control`, `receiver_identity`, `mcp_receiver_identity`.

Это часть производственной программы, не готовый deploy существующего CORE.
Native Docker/restart/restore ещё NOT_RUN. Для legacy baseline и promotion
необходимы [PW-02-B/PW-02-P](../planning/PRODUCTION_DATA_NEXT_GATES_RU.md).
До их завершения code-only update текущего CORE не допускается.

## Authority и неизменяемые releases

Operator config принадлежит владельцу среды и находится отдельно от originals,
canonical/evidence, runtime writes и qualification outputs. `authority_root`,
`protected_roots`, `control_root`, qualification root — host-local normalized
absolute paths; в PUBLIC реальные конфиги и секреты не добавляются.
Traversal, indirect ancestors, изменение BoundFile bytes и group/world-writable
authority запрещены. Новый request не может загрузить Python callback, shell,
SQL/Cypher или произвольный service ID.

`CoreOperatorConfig` закрепляет exact clean commit, actual installed code/deps,
policy, access configuration, approved release inventory и isolation. Generation
и acceptance plan refs исключены из deployment profile hash, поскольку они
содержат результаты drill/acceptance; остальные управляющие поля закреплены.
После получения новой receipt обновляют только exact immutable refs в отдельной
approved конфигурации. Предыдущий release сохраняет исходный acceptance profile.
Нельзя задним числом переписывать его receipt под новую inventory.

Каждый release содержит fully-rendered Compose JSON и exact unit image/config
pins. Это отдельная конфигурация только `api`/`mcp`; существующий общий
`infra/core/compose.yml` автоматически совместимым не считается. Runtime,
operator observer и контейнеры видят одинаковые absolute paths и один host-backed
gate inode. Все bind mounts read-only, кроме одного `admission.lock` file;
control directory read-only. Создание отсутствующих host mount paths запрещено.
CPU/memory bounds обязательны. Secrets передаются файловыми mounts; Compose
interpolation, implicit `.env`, build/pull/hooks/admin MCP и лишние capabilities
не допускаются. Допустимы только зарегистрированные binary SHA и local socket.

## Что доказывают receivers

API metadata route `/v1/internal/deployment/receiver` требует отдельный operator
credential, не входит в OpenAPI/MCP tools и не возвращает source content.
Fresh nonce/HMAC закрепляет actual generation, runtime, code/dependencies,
access configuration, components/services, PID/start/namespace и admission gate.
Native selected readers и file leases проверяются до и после доказательства.
Metadata route доступен при закрытом общем drain; остальные source requests
остаются закрытыми.

Read-MCP выдаёт отдельное signed proof из действующего SDK server и его actual
ApiClient. Проверяются actual READ inventory, upstream API proof и рабочий READ
credential/context. `mcp_read_principal_sha256` release обязан совпадать с exact
AccessContext `reader_principal` исходного acceptance plan и с контекстом live
API proof. Другой действительный READ-токен с отличающимися правами readiness
не подтверждает. Operator credential не заменяет READ-авторизацию. Missing,
wrong, dual READ/WRITE и admin credentials не дают readiness. Возвращается только
HMAC binding, не секрет. API и MCP должны использовать один отдельный operator
secret и одинаковый existing gate; у MCP это `VKM_DEPLOYMENT_TOKEN_FILE` и
`VKM_DEPLOYMENT_GATE_FILE`. Legacy apps без этих переменных сохраняют прежний режим.

Контроллер проверяет native Docker image/config, exact loopback published ports,
PID namespace/process start через host procfs и повторный container identity
после HTTP proof. MCP upstream должен быть тем же API process. Ограниченная или
rootless procfs без доказуемого join остаётся BLOCKED, а не заменяется healthz.
Прямой API proof и READ-MCP proof обязаны показывать открытый admission для READY.

Shared SH lease MCP удерживается до последнего ASGI response byte, включая
отправку уже полученного API payload. Поэтому EX writer ждёт не только API
запросы, но и завершение отдачи MCP клиентам. Replacement receivers при EX lease
не получают доступ к данным, хотя их metadata proofs остаются доступны.

## Последовательность для уже qualified baseline

1. Подготовить exact operator config, оба immutable releases, отдельный shadow
   project, generation/49-tool acceptance plan и native inventories. Config и
   credentials вне Git. Проверить disjoint roots, independent backup и бюджеты.
2. `deployment drill-plan`: отдельный confirmed plan для SHADOW_PRODUCTION.
   `deployment drill` принимает его SHA, применяет реальную selector mutation,
   инъецирует изолированный отказ и проверяет exact previous restore. Live fault
   injection запрещён. Журнал и receipt сохраняются в qualification store.
3. Закрепить drill receipt в acceptance plan. `deployment accept` принимает exact
   plan SHA, выполняет actual private API/MCP transport со всеми 49 tools,
   source policy, native components/services, original hashes и bounded responses.
   NOT_RUN/FAIL не регистрируются как полная qualification.
4. Закрепить полученную acceptance receipt в generation. `deployment plan`
   повторно доказывает healthy qualified previous и вычисляет exact intent.
5. `deployment switch` принимает его SHA: durable writer → drain → select
   approved recipe → native verify/full candidate probe → CURRENT → fixed
   recreate API/read-MCP → native proofs → admission. Image reference до запуска
   должен нативно разрешаться в qualified local image ID.
6. `deployment status` сверяет actual receiver identities, data и открытый gate;
   `--request-id` добавляет hash-linked transaction history.
7. При interruption: `deployment recovery-plan`, затем `deployment recover`
   с exact recovery SHA. Публичный factory допускает RESTORE_PREVIOUS. Нет
   автоматического продолжения failed candidate без отдельной qualification.

CLI аргументы: `--operator-config`, `--config-sha256`, для transaction actions
`--request-id`, для accept/switch/drill/recover `--confirm-plan`. `status` без
request ID показывает только current serving state. Эти команды здесь не запускались.
Native subprocess transport использует фиксированный argv, output/memory/time
bounds и убивает всю собственную process group при deadline. Ошибки CLI
санитизированы: config/secret/source content и traceback не публикуются.

Compose restart ограничен двумя receivers, локально доступными закреплёнными
образами и флагами `--no-deps --no-build --pull never --force-recreate`;
перестройка баз, pack contents, embeddings и cleanup сюда не входят.
Синтаксис: [Compose up](https://docs.docker.com/reference/cli/docker/compose/up/),
[Compose config](https://docs.docker.com/reference/cli/docker/compose/config/).

## Recovery и оставшиеся gates

Durable intent хранит immutable desired previous recipe и native generation.
Control identity читается независимо от candidate artifacts и их здоровья.
Повреждённый candidate не должен блокировать RESTORE_PREVIOUS. Неудачный restore
оставляет maintenance; generic PASS/health не открывает admission. Native recreate
проверяет замену обоих прежних контейнеров. Lost ACK не разрешает новый request
с изменённым payload и требует свежего current receiver proof.

Пока отсутствуют: one-time bootstrap существующего legacy receiver; typed mapping
shadow→live, согласующий разные roots/project/endpoint profiles; qualification
actual model owners; реальные image/filesystem/native restart/restore/full-MCP
rehearsal. Отдельно требуется capacity/latency и restore уникального canon/review.
CPU fixtures доказывают конкретные contracts/failures, не эти runtime gates и не
научную правильность извлечённого корпуса.

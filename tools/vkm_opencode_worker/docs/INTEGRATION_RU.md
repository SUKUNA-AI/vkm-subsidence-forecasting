# Интегрированный Windows runtime

Это действующее дополнение к исходной спецификации архива v2. Runtime находится
в `tools/vkm_opencode_worker/`, проектный Skill — в
`.agents/skills/vkm-opencode-worker/`. Глобальная конфигурация Codex не меняется.
Host, credentials, ledger, source packets и receipts находятся только в PRIVATE
или host-local каталогах, вне Git. Переносимый код не содержит путей машины.

## Контракты и запуск

`vkm.opencode.job/2` дополняется точным `read_paths`. Для `edit` обязательны
точные `edit_paths`; разрешения `edit` и `write` относятся к этим файлам.
Native V2 `edit` не создаёт файл через пустой oldString; создание выполняет
`write`. Builtin glob/grep закрыты до отдельной проверки confinement.

`plan --host <private-host> --job <job> --repo <repo>` проверяет реальный base SHA
и строит конфигурацию без inference/worktree writes. `Invoke-VkmWorker.ps1 run`
загружает существующий ключ из разрешённого .env или DPAPI только в дочернее
окружение и восстанавливает родительское. Сам пользователь JSON не заполняет.
Долговечный worktree остаётся для проверки; root checkout не очищается.

Обе qualification-записи должны быть приняты главным Codex и иметь статус
`APPROVED_BY_MAIN_CODEX`, связанный с текущим `implementation_hash`.
Хеш включает Python, JavaScript guard, PowerShell и native worker Skills.
Проверки установленного binary и MCP остаются отдельными от API/model smoke.

## Учёт расходов и маршруты

Используется тот же существующий TOTAL20 ledger, что у `openrouter-delegate`.
Профили переиспользуют его точные policy pins: Hydrogen/flex, Kimi/decart mxfp4,
DeepSeek/stepfun fp8 и Opus/Vertex global. Carbon блокирован. Fable и любой
другой запасной/вспомогательный профиль не используются. Переключения
model/provider/reasoning автоматически нет.

SDK получает invalid sentinel credential. Native `http.request` hook передаёт
точный captured UTF8 body в локальный accounting control endpoint, который
не перенаправляет provider traffic. Только после durable POSSIBLY_SENT
допуска hook ставит настоящий Authorization в исходный native Request.
Отсутствующий plugin не получает рабочий ключ. Явный `--title` исключает
автоматический title request; compaction auto отключён; любые maintenance
requests проверяются тем же guard. Retry запрещён.

SharedSession удерживает исходный Delegate lock весь ограниченный run,
выделяет один aggregate hold в пределах существующего profile cap и защищает
неизрасходованный diagnostic reserve $0.025. Издержки суммируются по generation
ID и response usage cost. Key usage delta — только наблюдение. Неизвестный
исход оставляет hold/blocked; pending не удаляется автоматически.

Оценка byte-length + protocol guard — консервативный estimate, не доказанная
токенизация провайдера. Поэтому независимая проверка server hard cap сохранена:
внешний лимит выше согласованного не расширяет бюджет. Неизвестный
тариф, active/unknown hold или непринятая qualification блокируют paid run.

## Инструменты и безопасность

`--standalone` сам по себе наследует ancestor config/plugins. Поэтому задаются
`OPENCODE_CONFIG_PROJECT_DISABLE=1`, точный внешний config, изолированные
HOME/XDG/TEMP и explicit plugin directory с `index.js` и exact policy grant.
Skills подключаются явным trusted path; native `skill` принимает поле `id`.

`ctx.tool.execute.before` проверяет exact read/edit/write paths, каноническую
принадлежность workspace и отсутствие reparse points. MCP получает отдельные
типизированные guards по точным argument names; Graphify project_path обязан
равняться workspace, Serena relative_path проверяется до dispatch. При
проектном сканировании запрещены любые junctions в дереве. Raw MCP readonly
не означает workspace confinement; upstream server scope не повышается до
true из-за клиентского hook. Shell разрешает только точную команду и cwd,
запрещает background и удаляет credentials из окружения дочерней команды.

Serena получает полный pinned template до запуска, включая coordinator-owned
.gitignore; cache сохраняется отдельно от code patch. Graphify строит только
локальный parser code index с backend=none, без научного графа и LLM.
Изменение подготовленного control file регистрируется как нарушение.

Это application guard доверенной рабочей станции, **не ОС-песочница**.
Разрешённые тесты исполняют код с полномочиями пользователя. Патч, тесты,
научные утверждения и перенос данных принимает главный Codex отдельно.

## Проверки и восстановление

`python -B -X utf8 tools/vkm_opencode_worker/tools/run_tests.py --output <private-new-receipt>`
проверяет offline contracts и реальные локальные процессы, без model calls.
`qualify_hook.py --exe <native-exe> --out <new-private-dir>` проверяет настоящий
native OpenCode с localhost mock; `--ledger-fixture` копирует input ledger в
output и никогда не меняет переданный файл. Это не paid/model smoke.

После сбоя не повторять task/session автоматически: session ID не является
billing idempotence. Проверить shared receipt, generation evidence, pending и
процессы; сверку выполняет Codex. Rollback — отключить только новый Skill/host
descriptor, сохранив source worktrees, ledger и все receipts; не сбрасывать
pending, не менять ключ и не удалять исходную авторизацию Codex.

> Исходная спецификация архива. Действующие дополнения интеграции: [INTEGRATION_RU.md](INTEGRATION_RU.md).

# Развёртывание: выполняет Codex, пользователь только авторизуется

## A. Сохранить текущую систему

Работать из Codex App с native Windows инструментами. Инвентарь → реальные checkout/branches/dirty → действующие AGENTS, бюджет и установленные MCP. Никаких broad reset/clean, новой клонированной копии корпуса или автоматического обновления CORE/EDGE. Снимать before/after только для реально затрагиваемых конфигураций. Расходное разрешение из истории переписки не переносить на новую партию без проверки.

Private state/runs разместить вне public/private репозиториев, в каталогах Windows-пользователя с ограниченными ACL. Runtime source можно интегрировать в public, но host manifest, credentials, receipts, prompts и native sessions — private/ignored. Не создавать parallel canonical. Сохранить рабочую авторизацию Codex без чтения/печати auth payload.

## B. Нативные зависимости

Пакет runner: Python 3.11+, stdlib. Git нужен для worktree. OpenCode: совместимая стабильная V2 >=2.2.6; точная версия обнаруживается/выбирается, хешируется и закрепляется. Это минимальный floor используемых policy actions, не утверждение, что 2.2.6 — текущий latest. Официальный Windows standalone binary предпочтительнее предположительно поддерживаемой package-manager команды. Проверить release asset архитектуры, signature/checksum при наличии и источник. Если опубликованной signature нет — не писать SIGNATURE_VERIFIED.

Установочные действия выполняются координатором один раз; worker не должен скачивать зависимости при каждом MCP запуске. Serena/Graphify/ast-grep из изолированных pinned env; Node только если нужен конкретному выбранному серверу. Не менять системные PATH/ExecutionPolicy без необходимости. Не передавать пользователю инструкции “установите Python”.

## C. Квалификация без платного inference

Первым этапом не использовать production-ключ и не вызывать внешнюю модель. На disposable Git fixture проверить:

- `opencode --version`, `run --help`, точную схему выбранного binary и конфигурационных источников;
- настоящий Windows hidden process, pipes, timeout/ошибку, остановку потомков/MCP; Windows Job Object может зависеть от уже активного job окружения Codex;
- пути с пробелами/кириллицей, relative/internal/external permission checks, недопустимый файл, неожиданный symlink/reparse;
- effective конфигурацию и server logs: неизвестный policy statement может быть отброшен с warning, это FAIL, а не успех;
- список inherited/global/system/parent config, дополнительные skills/plugins, отсутствие неожиданных providers;
- корректный event schema фактического CLI, а не guessed JSON; для contract-проверки можно использовать локальный контролируемый mock endpoint на синтетике, не закрытые материалы;
- provider-level request body на локальном fixture, title/summary/compaction model routing и отсутствие неявного другого провайдера.

Это bootstrap qualification: Codex вправе запускать бинарник на изолированной synthetic fixture напрямую для проверки оболочки; это **не обход запрета production run**. Native receipts должны ссылаться на реальные логи/артефакты, содержать hash binary/runner и PASS только для реально выполненных checks. После корректировки tools/*.py hash меняется — квалификация обновляется. Исторические receipts не переписывать.

Квалификация приложения не является проверкой научной инфраструктуры и не доказывает защиту ОС. Tests/MCP выполняются с правами того же пользователя. Для недоверенного кода нужна отдельно согласованная Windows security boundary; текущий launcher предназначен для доверенной исследовательской workstation.

## D. MCP

Точные команды/аргументы определить по установленному `--help`, официальный quickstart не копировать как гарантию совместимости. Выдавать exact executable и fixed package version. initialize/tools/list и содержательный tools/call выполнить **без модели**; это проверяет сервер, но не способность LLM выбрать tool.

Host receipt должен хранить `tools` с настоящими `name`/schema, версию server и worktree/snapshot binding. Проверить, что возвращаемые references/paths относятся к нужному repository SHA. Graphify build повторять только при изменённых входах; cache key включает parser/version и source digest. Serena привязать к текущему `{workspace}`; не оставить случайно основной dirty checkout. Нет проверенного sandbox для `replace/rename` MCP — использовать read-only сервер и обычные exact edit tools.

Для Context7 при необходимости Codex запускает auth-flow и ждёт только пользовательского входа. MCP OAuth предпочтительнее raw key в файле; при API key — environment reference. Не пересылать PRIVATE code/doc целиком сервису документации. Existing corpus/QGIS/CAD не копировать с хардкодами другой машины.

## E. OpenRouter и бюджет

Использовать существующую авторизацию/ключ и authorization scope. Если ключа нет, Codex открывает защищённое локальное окно; DPAPI scripts — один вариант хранения, а не второй аккаунт. Native runtime ключ получает через окружение. Никогда не печатать весь env/auth/config. Не выключать существующие ограничения данных ради smoke.

GET /models и metadata точного endpoint подтверждают model/provider IDs, tools/modalities/reasoning и тарифы. Missing capability — не повод сделать fallback. Требуется отдельно проверить OpenCode wire request и observed response/usage; catalog advertisement не измеряет реальную precision или effective reasoning.

Сверить конечный non-resetting key cap, remaining, baseline, прошлые расходы и authorization receipt. Требование include_byok_in_limit=true — консервативная политика этой реализации. Наличие BYOK здесь не требуется; подключение нативных provider API не добавляется. Если уже существующий разрешённый механизм контроля другой, Codex может реализовать эквивалентный доказанный guard отдельным patch+test, но не удалять проверку ради успешного запуска.

Пакет сериализует один key внутри данного state_root. Для параллельной нагрузки сначала согласовать единый atomic ledger с существующим Delegate и подтвердить aggregate accounting. Не использовать новый state_root/ключ для обхода pending. Начальная консервативная сериализация не выдаётся за высокопроизводительный многопоточный scheduler.

## F. Реальное включение

Codex сам создаёт host manifest и job по templates. Запуск только после native qualification. Для каждого реально выбранного профиля — минимальная проверка инструментального действия в общем разрешённом бюджете, затем одна содержательная задача. Не делать одинаковые длинные benchmark-вопросы ради красивого сравнения.

Проверить `skill` и нужный MCP в реальном completed tool trace; один API ответ текстом — FAIL для инструментального сценария. После run прочитать artifacts и независимые tests; model/tool/provider/receipt UNKNOWN не повышать. Accounting settle выполнить самому через generation/activity evidence; это не обращение к пользователю за ручным “снять pending”. Если данные провайдера недоступны, сохранить blocker и продолжить неоплачиваемые этапы.

Выходной статус разделять: INSTALL, NATIVE, MCP_TRANSPORT, MCP_BEHAVIOR, MODEL_ACCESS, TOOL_USE, ROUTE, ACCOUNTING, TESTS, CODEX_REVIEW. Production/scientific канон остаётся вне автоматической приёмки.

## G. Rollback и обслуживание

Вернуть только изменённые этим внедрением config из проверенного бэкапа при совпадении ожидаемого current hash. Не удалять сессии/ключи/БД пользователя и не сбрасывать Codex. Worktrees удалять только после приёмки/сохранения результатов, отдельно проверив ownership; pending accounting не удалять.

Обновление бинарника/MCP/adapters → отдельная проверка совместимости и native receipt. Не полагаться на project `update:disable`: официальные docs описывают update как global-only. Закрепить самостоятельный executable path/hash; shared service не использовать для автоматических jobs. Проверить приватность и срок хранения нативной OpenCode session DB/logs, которые wrapper не контролирует.


## Проверка публичной поставки и возврат

Перед активацией сверить `MANIFEST.sha256.json` по фактическим байтам, выполнить текущую offline-процедуру с Node SSE bridge и вызвать native PowerShell launcher с `plan` на отдельном synthetic checkout с пробелами/кириллицей. Такой plan не читает ключ, не выполняет inference и не является подтверждением платного запуска.

Файлы поставки переносятся только по явному allowlist в проектный Skill/runtime; host manifest, журнал расходов, credentials, исходные документы и native sessions остаются приватными. Другие Skills/проекты/конфигурации не заменяются. Новый экземпляр клиента должен обнаружить Skill и реальные допустимые MCP; прежний receipt не обещает hot reload.

Для возврата остановить только принадлежащий запуск, сохранить receipts/изменения и вернуть предыдущие проверенные байты Skill/runtime по заранее записанным хешам. Новый запуск требует согласованной версии qualification/host; нельзя подменять новый бинарник старым receipt. Историю расходов и UNKNOWN holds не откатывать, не обнулять и не удалять. Наличие известной стоимости не превращает провал задачи или непроверенный источник в PASS.

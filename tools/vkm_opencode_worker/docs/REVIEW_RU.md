> Исходная спецификация архива. Действующие дополнения интеграции: [INTEGRATION_RU.md](INTEGRATION_RU.md).

# Ревью прежних пакетов и исправления v2

Основание — фактически прочитанные `VKM_OpenCode_Worker_Skill_Draft.zip` и `VKM_OpenCode_Windows_Codex_Integration_v1.zip`, включая launcher, тесты, skill и README. Внешняя проверка актуальных интерфейсов отделена в SOURCES.md. Не выполнялся аудит всего репозитория ВКМ или пользовательской Windows.

## Реальные проверки исходного v1

Прежние 10 тестов проходят. Это не было достаточным покрытием: четыре дополнительных контрпримера воспроизвели дефекты. JSON результатов сохранён в `validation/v1-counterexamples.json`, воспроизводящий скрипт — `validation/reproduce_v1.py`. Для повтора скрипту передаётся путь к оригинальному launcher. Скрипт исполняет только локальный fake CLI, не OpenRouter.

### R01 · HIGH · Удаление существующей конфигурации при ошибке

В v1 `execute` сначала обнаруживает конфликт `.opencode/opencode.jsonc`, но `finally` удаляет файл независимо от того, кем он был создан. Удаляется существующая копия в новом worktree; основной checkout в контрпримере сохранился. Не утверждаем потерю оригинального репозитория.

Исправление: exclusive creation, журнал owned-files с SHA256, cleanup только совпадающего собственного файла. Изменённый файл/симлинк/изменённая parent-ссылка остаются для расследования. При исходном конфликте модель не стартует. Регрессии проверяют и исходную коллизию, и замену parent на symlink.

### R02 · HIGH · Фактический diff не проверялся по scope

Fake worker меняет `other.py`, когда разрешён только `main.py`; v1 всё равно возвращает `CLI_COMPLETE_UNVERIFIED`. Это не доказанный успешный патч, но scope violation не выявлен.

Исправление: проверка tracked/staged/untracked, включая ignored-файлы; точное сравнение edit_paths, изменение HEAD/Git pointer и control files, отдельный экспорт новых файлов. Любое нарушение даёт REVIEW_REQUIRED/отказ, без интеграции. Разрешённые runtime caches перечислены отдельно — это не доказательство отсутствия любых host-side effects.

### R03 · HIGH · “Точный путь” принимал wildcard и Windows device name

`src/*.py` и `CON` принимались старой проверкой. На Windows дополнительно существенны ADS, case collision, reparse points, точки/пробелы на конце.

Исправление: literal paths, запрет wildcard/traversal/ADS/device/control names, case-collision check, проверка каждого компонента; та же защита task_id. Пути state/run обязаны быть абсолютными вне public/private checkout. Директория не считается разрешением на все её файлы.

### R04 · HIGH · Денежный cap существовал только как утверждение caller

`budget_usd`/owner_confirmed v1 не доказывали реальный лимит OpenRouter и не ограничивали все вызовы агентного цикла. Нет блокировки будущих task IDs после неизвестной стоимости.

Исправление: GET /key, привязка к fingerprint существующего ключа, finite non-resetting cap в пределах действующей партии с учётом baseline usage, exclusive lock и persistent pending. Сверка выполняется Codex по provider evidence. Это **не** точная per-task цена, не распределённая блокировка и не защита от другого процесса того же пользователя. Нельзя объявлять usage delta точной ценой задачи при общем ключе.

### R05 · HIGH · Permissions были неэквивалентны заявленной изоляции

Одни absolute paths не покрывают documented Location-relative checks V2. Широкие MCP wildcards могут дать больше возможностей, чем предполагает сценарий. Edit policy не ограничивает операции внутри MCP/test process.

Исправление: точные relative+absolute ресурсы, deny-default permissions и tool/provider/MCP/skill/plugin policies, точные имена из tools/list, `codemode:false` для прозрачных direct calls. Записывающий MCP требует собственной проверенной scope-защиты. Глобальные/системные/parent config проверяются отдельно. Это application-level ограничения на доверенной workstation, **не OS sandbox**. Нативные deny-тесты обязательны.

### R06 · MEDIUM · Worker skill не был действительно разрешён

Иметь SKILL.md рядом недостаточно: tool `skill` при deny-default должен быть разрешён. В v1 этого не было. Codemode и execute-права также не образовывали согласованной проверенной конфигурации.

Исправление: четыре отдельных worker skills, один разрешённый skill на сценарий, проверяемое имя и explicit skill policy. Main Codex skill отдельно от навыка worker. Реальный вызов skill/tool должен быть виден в native smoke.

### R07 · MEDIUM · Windows hidden/lifecycle не подтверждались

CREATE_NEW_PROCESS_GROUP не равен скрытому окну. Обработка исключений и остановка дерева процессов требовали усиления; без очистки потомков MCP/сервер могут остаться работать.

Исправление: CREATE_NO_WINDOW, Job Object kill-on-close, always-reap, одновременное чтение обоих pipe, ограничения line/bytes/time. POSIX regressions выполнены; ctypes/Job Object и descendant cleanup на Windows — NOT_RUN до работы Codex. Между Popen и assignment существует небольшой промежуток: механизм не объявляется защитой от враждебного процесса.

### R08 · MEDIUM · Неограниченные журналы и секреты

Raw NDJSON может содержать reasoning, tool inputs/outputs и закрытые материалы. У v1 недостаточно ограничений потока и redaction.

Исправление: метаданные событий, capped redacted assistant report/stderr; reasoning и tool payload не дублируются wrapper. Inline credentials в MCP manifest/argv/URL запрещены, secret refs через env. Нативные session stores OpenCode остаются отдельной зоной хранения, требующей ACL/retention; слово “redacted” не гарантирует анонимизацию любого произвольного текста.

### R09 · MEDIUM · Exit0 и событие завершения не доказывают выполнение

API-error, обрезанный ответ и отсутствие реальных tools могли остаться незамеченными.

Исправление: parser ошибок/invalid JSON/truncated completion, наличие assistant report и completed tools, required_tools. Кандидат всё равно ожидает независимые тесты и Codex review. Структуру событий надо подтвердить на установленной версии; fake schema не является native proof.

### R10 · MEDIUM · Модельные параметры и служебные вызовы

Нельзя считать отправленный reasoning подтверждённым effective reasoning. Нельзя рассчитывать, что поле agent.request реально применяется: актуальная документация отмечает, что оно сохраняется, но не передаётся модели. Также runtime может использовать maintenance agents и внутренние retries.

Исправление: OpenRouter provider-level body, exact route policy без fallback, maintenance models pinned. Provider-only/reasoning/max_tokens подтверждаются wire-тестом. Receipt хранит requested отдельно от UNCONFIRMED observed. Project update setting удалён: документация говорит, что оно игнорируется на project level; версия защищается закреплённым binary/hash и квалификацией.

### R11 · MEDIUM · Graphify был ошибочно сужен до hosted-варианта

В v1 предложен hosted MCP как основной путь. У Graphify-Labs существует local MCP. Прямой совместимости этого code graph с существующим научным Neo4j автоматически нет.

Исправление: только local Graphify, pinned версия, queryable derived graph с repo SHA/content hashes, отдельная семантика от scientific canon. Не угадывать CLI `--graph`: сверять фактический `--help` выбранного релиза. Имена инструментов импортировать из tools/list.

### R12 · MEDIUM · Установка и пути перекладывались на пользователя

WSL/Ori варианты из draft противоречат окончательному решению. Пользователь не должен заполнять шаблоны или вручную создавать MCP-конфигурацию.

Исправление: один полный Codex task, inventory/auth/helper scripts, native qualification contract, автоматическое обнаружение окружения силами Codex. Templates с null специально неработоспособны до корректного заполнения; это не назначение пользователю дописывать пакет.

## Что не было доказано и не выдаётся за исправленное развёртывание

Ни фактическая Windows-совместимость сторонних релизов, ни реальные model/endpoint IDs, ни MCP на пользовательских данных, ни экономия tokens/cost здесь не измерены. Их приёмка поставлена Codex как исполнимая задача с evidence. Все найденные дефекты исходной реализации исправлены в поставленном коде либо, когда свойство зависит от native среды, закрыты fail-closed gate и явным проверяемым критерием. Это не обещание отсутствия любых будущих ошибок.

## Итог проверок поставки

101 offline-тест выполнен: 0 failures, 0 errors, 0 skipped. Реальные временные Git-репозитории и POSIX subprocess supervision; fake CLI вместо OpenCode. Базовые 10 тестов исходного v1 также повторно пройдены; четыре контрпримера повторно воспроизведены. Ни одного вызова реальной модели или нативного MCP в этой сессии не было.

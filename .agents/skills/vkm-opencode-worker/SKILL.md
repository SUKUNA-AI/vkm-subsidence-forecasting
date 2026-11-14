---
name: vkm-opencode-worker
description: "Запускать из Codex ограниченные инструментальные задачи ВКМ через native Windows OpenCode и OpenRouter: расследование кода, structural fixes, source research и независимое review. Codex настраивает и принимает; пользователь только авторизуется."
---

# VKM OpenCode Worker

Главный исполнитель — Codex App. Этот skill делегирует bounded multi-step работу модели внутри OpenCode, а не просто пересылает запрос API. Для одиночного анализа без инструментов переиспользуй действующий `openrouter-delegate`; не запускай runtime без необходимости.

## Перед первым использованием

Прочитай `references/OPERATIONS.md`; при незавершённой установке выполни полный `CODEX_TASK_RU.md` из корня интегрированного runtime. Обнаружь его фактическое расположение через проектный `VKM_OPENCODE_BUNDLE_ROOT`/документированную конфигурацию; не предполагай sibling-layout. Установку, host manifest и MCP выполняешь ты. Пользователю передаётся только окно авторизации.

Не запускать Ori/Hermes/DeepSeek Harness/Claude Code, не переносить OpenCode в WSL и не трогать подписку основного Codex. Не обходить native qualification, budget guard или pending state.

## Выбрать задачу и контекст

Назови недостающий результат и критерии. Установи base commit, точную область файлов, необходимый сценарий и один профиль OpenRouter. Проверь уже принятые результаты и актуальный источник проблемы. Не передавай весь репозиторий или научный корпус в prompt: дай вопрос, нужные locators и tools. Для неопределённого scope сначала отдельный review, затем edit по найденному точному списку.

Сценарии: `code_investigation`, `structural_change`, `source_research`, `independent_review`. Выбор DeepSeek/Kimi/Hydrogen/Opus определяется проверенными capabilities и задачей; профиль не считается доказательством качества. Подключай только нужные MCP-tools из сохранённого tools/list. В `required_tools` укажи конкретные действия, без которых задача не считается инструментально выполненной.

## Запуск

Runtime — `tools/vkm_opencode_worker/` от корня PUBLIC. Собери `vkm.opencode.job/2` с точными `read_paths` и `edit_paths`. Сам выполни `plan`, проверь config/model/provider/permissions/skill/base SHA; затем `run` через `tools/vkm_opencode_worker/tools/Invoke-VkmWorker.ps1`. Нет готового valid host — сначала наладь установку, не проси пользователя заполнить template. Secret только через существующий разрешённый env/credential store.

Один worker использует один отдельно закреплённый worktree. Wrapper ограничивает steps/time/output, применяет общий существующий TOTAL20 ledger и сохраняет patch/new files/report. Нужны current native и accounting qualification с hash именно текущего кода. Серверный hard cap не заменяется estimate. Не запускай несколько владельцев записи одних объектов. Конфликт существующего runtime config устрани отдельной проверяемой интеграцией, не удалением исходного файла. Автоматических повторов/merge/production activation нет.

## Завершение

Прочитай receipt/handoff/events metadata и фактические файлы, самостоятельно запусти адресные тесты. Required tools должны быть реально вызваны; заявленный тест модели без независимого исполнения — не PASS. Любое scope violation требует расследования, не переносить весь diff автоматически.

Сверь provider accounting и незавершённые запросы, создай `vkm.opencode.settlement/2` с реальными evidence, выполни reconcile. Это работа Codex, не ручная обязанность пользователя. Неизвестная стоимость не ноль. Не повышай/сбрасывай общий расходный бюджет.

В существующее хранилище перенеси только принятый краткий handoff со ссылками, версиями и статусом. Научные FACT/DERIVATION/INTERPOLATION/UNKNOWN не повышаются из ответа модели. Память агента не заменяет corpus/evidence. Отчёт должен разделять requested/observed, API/schema/tests/review/scientific acceptance.

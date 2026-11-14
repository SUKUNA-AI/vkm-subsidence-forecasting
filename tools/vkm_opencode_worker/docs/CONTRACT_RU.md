> Исходная спецификация архива. Действующие дополнения интеграции: [INTEGRATION_RU.md](INTEGRATION_RU.md).

# Исполняемые контракты v2

## Файлы и доверие

Job — ограниченный запрос координатора. Host manifest — доверенная конфигурация **Codex**, не редактируемая worker. Knowledge/capability/qualification receipts — проверяемые записи с path+sha256, но подписью третьей стороны они автоматически не становятся. Пользователь ничего из этого вручную не заполняет.

`templates/` не содержит валидных production values: null намеренно блокирует запуск, если Codex не обнаружил реальные значения. Фиктивные Windows paths, model IDs, API keys и budget numbers не поставляются как готовая настройка.

## Job: `vkm.opencode.job/2`

Обязательны `task_id`, полный immutable `base_sha`, `mode` review/edit, `profile` deepseek/kimi/hydrogen/opus, `scenario`, `objective`, `acceptance_criteria`, `mcp`, exact `edit_paths`, `max_steps`, `max_runtime_sec`. Необязательны source_locators, context_note, required_tools. Unknown/duplicate JSON fields отклоняются.

task_id — уникальный переносимый идентификатор без Windows device names. Один task ID — одна попытка; повтор не перезаписывает её. base SHA не заменяется HEAD/main. Dirty-файлы основного checkout намеренно не попадают в worker: если они нужны, Codex создаёт отдельно согласованный snapshot/patch с provenance и соответствующий base, не делает коммит чужой работы без согласования.

Review: edit_paths пуст. Edit: точный список файлов, не директорий/globs; новые файлы разрешены явно. Сначала review для исследования неизвестного scope. Agent instructions/runtime configs вне edit scope. Steps 1–64, runtime 30–7200 секунд — guards реализации, не limits модели.

`mcp` выбирает имена из host. `required_tools` содержит реальные нормализованные OpenCode actions, в том числе server_tool с сохранёнными дефисами. Они должны быть granted и присутствовать в completed events. Точный event schema подтверждается native qualification. Инструментальный output не анализируется как научная истина автоматически.

## Host: `vkm.opencode.host/2`

platform=windows-native; opencode={path,sha256,version}; run_root/state_root — абсолютные приватные каталоги вне всех protected_roots. По одному действующему профилю на нужную модель: exact OpenRouter vendor/model ID **без** дополнительного префикса openrouter/, provider_only, reasoning_effort, max_output_tokens, checked_at, capability_receipt. Live capabilities не старше 24 часов; freshness не заменяет доказательство их содержания.

`mcp.<name>`: config официальной схемы OpenCode V2, access read/scoped-write, allowed_tools exact, tools_receipt. Server command — абсолютный установленный executable, без install-on-launch. Remote HTTPS; HTTP только для явно квалифицированного private-network endpoint. Secret headers/environment — `{env:NAME}`; URL query credentials не принимаются. Graphify — local. `scope_enforced:true` допустим только при реальной проверке записи сервером, не “потому что в prompt сказали”.

`approved_plugins` — точные проверенные plugin IDs. Broad wildcard не допускается. `shell_commands.<scenario>` — точные аудированные команды. По умолчанию shell не выдаётся; тесты исполняют код с правами ОС. Устанавливать пакеты, делать push или менять infrastructure из worker запрещено. Allowed read/glob/grep не являются полноценной защитой от утечки всех файлов текущего пользователя.

`environment_names` передаёт только отдельно разрешённые VKM_/CONTEXT7_/CA variables; секреты других provider/Git/DB не наследуются wholesale. Только known supplied secrets redacted; произвольные секреты в содержимом файлов требуют отдельного ограничения входов и инструментов.

`budget`: authorization_id, authorized_total_usd, baseline_usage_usd, key_fingerprint, authorization_receipt. Runner проверяет фактический GET /key; нет active authorization — нет live, но бесплатные setup/test могут продолжаться. authorization receipt не может создаваться задним числом из одного баланса аккаунта.

## Native receipt: `vkm.opencode.qualification/2`

Содержит platform win32, opencode_sha256, implementation_sha256 (команда implementation-hash), checks и evidence[{path,sha256}]. Обязательные checks: windows_native, headless_flags, config_schema, effective_permissions, ambient_config_review, process_tree_cleanup, auth_isolation, provider_body, event_schema. Для run все должны быть PASS. Они не заменяют дальнейшие реальные model/MCP smoke.

`ambient_inputs` формируется по обнаруженным user/system/parent config источникам, с path/digest. `ambient-inventory` выдаёт известные пути и hashes без содержимого. Это автоматическая проверка отсутствия изменения после qualification, а не семантический аудит конфигураций. Codex должен сопоставить список с реальным `opencode debug config` и известными источниками установленной версии. Новая переменная/особый store → расширить обнаружение и тест.

## Ход выполнения и результаты

Порядок: validate → qualification → verify budget → lock → worktree explicit SHA → owned config/skill/task → persistent pending → hidden process → metadata/report → scope audit → artifacts → handoff. На ошибке исходный checkout не откатывается, worktree сохраняется для анализа; деньги не считаются автоматически возвращёнными.

Положительный `CANDIDATE_READY_FOR_CODEX` означает только наличие допустимого кандидата, не passed tests/accepted science. `REVIEW_REQUIRED` означает blockers. `BLOCKED_BEFORE_INFERENCE` — process ещё не запускался (были только проверки/GET); `FAILED_COST_UNRESOLVED` — запуск начат, итог/стоимость требуют сверки.

Outputs: receipt.json, handoff.json, job/plan hashes, events.meta.jsonl, stderr.redacted.log, worker-report.txt, changes.patch, new-files/. Plan/report могут содержать приватные prompts/locators: каталог остаётся private. Максимальный размер одного patch/artifact 32 MiB; больший результат сохраняется в worktree и требует отдельной передачи Codex.

`requested_model/reasoning` отделены от фактического подтверждения; effective/endpoint defaults UNCONFIRMED. Metadata provider не измеряет физическую precision. Code guard не утверждает, что суммарный cap удержит точно заданное количество billed tokens в каждом сценарии.

## Accounting settlement: `vkm.opencode.settlement/2`

После любого стартовавшего worker сохраняется pending по fingerprint. Снять его может **Codex** после проверки: task_id и key совпадают, no_inflight_requests_confirmed=true, all_generations_settled=true, basis generation_records/provider_activity, реальные evidence hashes. Команда reconcile повторно читает usage, сохраняет settlement и архивирует marker, не удаляя историю.

Это attestation координатора по evidence, а не автоматическая независимая банковская верификация. Provider activity должна учитывать скрытые retries/maintenance. Usage delta общего ключа не считается exclusive task cost. Timeout не доказывает, что upstream inference отменился. Нет данных — pending остаётся, batch не продолжается платно; научная приёмка с reconciliation не связана.

## Ограничения начальной версии

Один активный worker на ключ/state_root. Полноценной distributed queue, cross-machine leases, перехвата всех исходящих запросов и per-turn budget gateway нет и не требуется скрыто внедрять их в этот пакет. Конкуренцию с существующим Delegate решает общий policy/ledger координатора. Все server test/memory writes остаются в фактических разрешениях инструмента.

Исходный tracked `.opencode`/opencode.json конфликтует с generated config и приводит к отказу, не удалению. Если такой проект уже существует, Codex делает явную tested integration/merge конфигурации, сохраняя пользовательские файлы; не добавляет bypass. Внешние платные плагины не допускаются без отдельной оценки стоимости. Offline тесты не доказывают Windows kernel/LLM/provider behavior.

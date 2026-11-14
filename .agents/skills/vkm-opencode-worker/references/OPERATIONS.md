# Пути, команды, правила эксплуатации

Runtime проекта расположен в `tools/vkm_opencode_worker/` от корня PUBLIC. Не переносить один SKILL.md без runtime. Host-конфигурация, журнал расходов и результаты находятся вне репозитория; их точные пути основной Codex берёт из текущего приватного checkpoint.

Команды выполняет Codex, не пользователь:

```powershell
& $Python "$Runtime\tools\run_tests.py" --output "$PrivateReceipts\offline.json"
& $Python "$Runtime\tools\vkm_opencode_worker.py" plan --host $HostManifest --job $Job --repo $PublicRoot
& "$Runtime\tools\Invoke-VkmWorker.ps1" -Python $Python run --host $HostManifest --job $Job --repo $PublicRoot
& "$Runtime\tools\Invoke-VkmWorker.ps1" -Python $Python reconcile --host $HostManifest --evidence $Settlement
```

Это схема вызова с переменными, которые **Codex определяет сам**. Не предлагать пользователю копировать команды или руками подставлять пути.

- Запуск модели только после native qualification и действующего бюджета.
- One-time auth — существующий OpenRouter key через env или DPAPI script; ключ не в prompt/argv.
- `plan` не выполняет inference и не создаёт worktree, но проверяет реальный Git/base SHA.
- `run` return0 означает кандидат для приёмки Codex, не принятый патч.
- return3 — выполнено, но REVIEW_REQUIRED; return2 — validation/runtime failure.
- Нужен точный `read_paths`: разрешены отдельные файлы, без wildcard. `edit_paths` тоже точные; широкие builtin glob/grep закрыты. MCP разрешается по проверенным tool names и типизированным path guards; raw readonly не означает ограничение каталогом.
- Единственный исходный TOTAL20 ledger используется и one-shot Skill, и инструментальным исполнителем. `SharedSession` держит исходный Delegate lock весь bounded run; `http.request` резервирует каждый фактически захваченный body до отправки. Retries запрещены. `http.response` сверяет generation ID/cost; неизвестный исход сохраняет hold и blocked, не ноль.
- Серверный hard cap проверяется независимо от консервативной оценки токенов. Нельзя подменять разрешённые $20 большим внешним лимитом ключа или увеличивать лимит ради теста.
- SDK получает invalid sentinel, настоящий ключ — только native request hook после durable admission. Prompt через stdin, явный `--title`, `OPENCODE_CONFIG_PROJECT_DISABLE=1`, точный внешний config и изолированные HOME/XDG.
- `ctx.tool.execute.before` проверяет канонические пути и reparse points; shell hook удаляет credentials из дочернего окружения. Это application guard доверенной рабочей станции, не ОС-песочница.
- Сверка accounting обязательна: known generation receipts закрывают session reservation; persistent pending marker требует проверки Codex через reconcile. UNKNOWN нельзя сбросить удалением файлов или новой сессией.

Полные контракты — `docs/CONTRACT_RU.md`, развёртывание — `docs/DEPLOYMENT_RU.md` в runtime root. После обновления адаптера, бинарника или разрешающих правил повтори затронутую native qualification.

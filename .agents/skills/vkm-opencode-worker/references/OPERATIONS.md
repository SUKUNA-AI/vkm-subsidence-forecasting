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
- Единственный исходный ledger (текущий явно авторизованный TOTAL40, история TOTAL20 сохранена) используется и one-shot Skill, и инструментальным исполнителем. `SharedSession` держит исходный Delegate lock весь bounded run; `http.request` резервирует каждый фактически захваченный body до отправки. Retries запрещены. `http.response` сверяет generation ID/cost; неизвестный исход сохраняет hold и blocked, не ноль.
- Серверный hard cap проверяется независимо от консервативной оценки токенов. Нельзя подменять явно разрешённый total большим внешним лимитом ключа или увеличивать лимит ради теста. Изменение TOTAL20→40 требует отдельной авторизации владельца и сохраняет все расходы/holds.
- SDK получает invalid sentinel, настоящий ключ — только native request hook после durable admission. Prompt через stdin, явный `--title`, `OPENCODE_CONFIG_PROJECT_DISABLE=1`, точный внешний config и изолированные HOME/XDG.
- `ctx.tool.execute.before` проверяет канонические пути и reparse points; shell hook удаляет credentials из дочернего окружения. Это application guard доверенной рабочей станции, не ОС-песочница.
- Сверка accounting обязательна: known generation receipts закрывают session reservation; persistent pending marker требует проверки Codex через reconcile. UNKNOWN нельзя сбросить удалением файлов или новой сессией.

Полные контракты — `docs/CONTRACT_RU.md`, развёртывание — `docs/DEPLOYMENT_RU.md` в runtime root. После обновления адаптера, бинарника или разрешающих правил повтори затронутую native qualification.

- Текущий Kimi API-маршрут отозван владельцем; только отдельный подписочный маршрут. Старые Kimi receipts остаются историей. Новый DeepSeek task создаётся явно и не считается fallback.

- Изображения допускаются только отдельным task-bound SHA/MIME/pixel-контрактом. В imagejob генерируется `media.image.auto_resize:false` и точные максимумы исходных размеров/base64. Нативное скрытое уменьшение или JPEG-перекодирование не разрешаются. Статус read/completed сам по себе не доказывает доставку изображения: сверяются SHA в фактически допущенных запросах.
- Для imagejob единый предел любой stdout/stderr-строки выводится из максимального одобренного base64 + прежний 1MiB конверт, с прежним общим пределом 32MiB. Это JOB-level допуск для всех строк, в том числе обычного текста и stderr, а не per-content гарантия1MiB. Text-only job сохраняет 1MiB. Непомещающийся источник блокируется до запуска, а не уменьшается.
- По умолчанию резерв output использует консервативную SUM. Отдельный одобренный `output_rate_policy.hydrogen` может применить MAX только для H/Flex, доказанного общего inclusive cap и равных актуальных completion/internal_reasoning тарифов: `V+R<=N => V*pc+R*pr<=N*max(pc,pr)`. Proof, формула, фактические тарифы и неизменная надбавка 25% сохраняются в receipt. Другие профили не переключаются на эту политику.
- Неуспех выполнения и неизвестность стоимости различаются. Известная стоимость закрывается при доказанном `ActiveProcesses=0` собственного Windows Job и отсутствии незавершённых HTTP; job при этом остаётся FAILED/REVIEW_REQUIRED. Любая неопределённость процесса или possibly-sent запроса сохраняет UNKNOWN hold. Автоматической разблокировки общего ledger нет.

---
name: openrouter-delegate
description: "Делегировать ограниченную задачу ВКМ закреплённым моделям OpenRouter: визуальный анализ Hydrogen, инженерная разработка Kimi или небольшой вспомогательный код DeepSeek. Результат проверяет основной агент."
---

# OpenRouter Delegate для ВКМ

Главным исполнителем остаётся Codex. Сначала используй существующие сведения проекта; передавай только ограниченную задачу. Удалённая модель возвращает JSON, без shell, Git, SQL, инструментов или самостоятельного доступа к файлам. Её вывод проверяет и применяет основной агент.

Выбирай один профиль и одну модель на операцию: `vision_documents` — Hydrogen для визуального анализа; `engineering_research` — Kimi для сложной инженерной разработки; `cheap_code` — DeepSeek для дешёвых вспомогательных задач. Несколько моделей нужны лишь при конкретной проверяемой необходимости; зафиксируй вопрос для каждой и вызывай отдельно. Профиль `critical_review` / Carbon имеет статус `BLOCKED_403`: call, mock и discover запрещены, автоматической замены нет.

Прочитай [MODELS.md](references/MODELS.md) перед выбором профиля и [VKM_WORKFLOWS.md](references/VKM_WORKFLOWS.md) при подготовке задания. Одна установка относится только к этому проекту. Скрипт не читает ключевые файлы: `OPENROUTER_API_KEY` поступает только через окружение процесса. Не передавай ключ через argv, чат или журнал.

Клиент принимает ограниченный job JSON: prompt, inline PNG/JPEG и схему результата. Для синтетики указывай `synthetic=true`. Для разрешённого ограниченного визуального задания VKM-GEO-01 или VKM-GEO-01-MA по VKM-SRC-011 указывай `synthetic=false` и строгий `input_package` из [VKM_WORKFLOWS.md](references/VKM_WORKFLOWS.md). Пакет связывает work/source/page/figure/panel/frame с SHA256 конкретного изображения; он является явным утверждением вызывающего агента, который проверяет разрешение и исходные локаторы. Клиент не читает источники, пути или URL и не превращает утверждение в доказанный факт.

Из корня проекта:

```powershell
python .agents/skills/openrouter-delegate/scripts/openrouter_call.py discover --profile cheap_code --output work/delegate-endpoints.json
python .agents/skills/openrouter-delegate/scripts/openrouter_call.py check --job work/job.json --metadata work/delegate-endpoints.json
python .agents/skills/openrouter-delegate/scripts/openrouter_call.py mock --job work/job.json --metadata work/delegate-endpoints.json --run-dir work/mock-delegate
```

Платный `call` требует разрешённый владельцем бюджет партии `--budget-usd`, общий для партии `--ledger`, отдельный `--run-dir`, ключ в окружении и проверенный через GET `/key` конечный лимит ключа. Указывать баланс вместо бюджета нельзя. Команда выполняет ровно один POST без повторов; timeout, неизвестная стоимость или незавершённая reservation блокируют новые расходы партии до ручной сверки. Не удаляй ledger/lock ради повторного вызова.

```powershell
python .agents/skills/openrouter-delegate/scripts/openrouter_call.py call --job work/job.json --metadata work/delegate-endpoints.json --budget-usd 0.05 --ledger work/private-batch/ledger.json --run-dir work/private-batch/probe-1
```

`0.05` — пример синтаксиса, не расходное разрешение. Пиши receipts и результаты в приватный/ignored каталог. Скрипт сохраняет хеши, разрешённые metadata, usage и стоимость; для реального визуального входа — также `synthetic=false` и provenance-пакет без base64. Reasoning и секреты не сохраняются. Ошибки дают ненулевой exit code. Mock не доказывает доступность API. `PROVENANCE_UNCONFIRMED` означает известную стоимость, но недостаточное подтверждение точного endpoint; научного PASS или принятия результата моделью здесь нет.

Ответ OpenRouter может содержать подтверждённый canonical ID вместо запрошенного alias: разрешены только три фиксированные пары из [MODELS.md](references/MODELS.md). POST запрашивает routing metadata заголовком `X-OpenRouter-Metadata: enabled`. Для Kimi предусмотрен ограниченный вывод о маршруте по единственному Decart endpoint и полному router selection; receipt помечает его `UNIQUE_PINNED_ENDPOINT_AND_ROUTER_SELECTION`. Это вывод из каталога и routing metadata, не измерение физической precision; отсутствующие наблюдаемые endpoint/quantization не заполняются значениями из каталога.

Узкое явно разрешённое владельцем исключение для трёх synthetic smoke-проб: `call --owner-local-cap-only --budget-usd 1` с **одним и тем же ledger для всей партии**. Допускается бюджет не более $1; максимум три успешные reservation за всё время ledger, включая известные ошибки исполнения. Обнуление счётчика и создание нового ledger ради дополнительных вызовов не разрешены. При `GET /key` с `limit=null, limit_remaining=null` receipt честно пишет `server_key_limit_status=UNLIMITED_OBSERVED`, `override=EXPLICIT_OWNER_LOCAL_CAP_FOR_THREE_SMOKES`; лимит1 не выдаётся за серверный. Без явного флага сохраняется `LIVE_BLOCKED_KEY_LIMIT`. Unknown/pending блокируют партию и при этом исключении. Флаг не является общим разрешением на будущие расходы.

Смена активных профилей не обновляет бюджет и не сбрасывает счётчик прежней партии. Потолок вызова Kimi $1 — ограничение профиля, не новое разрешение потратить $1.

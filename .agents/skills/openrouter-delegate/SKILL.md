---
name: openrouter-delegate
description: "Делегировать ограниченную задачу ВКМ закреплённым моделям OpenRouter: небольшой код, конкретное изображение или независимое ревью. Возвращает кандидат для проверки основным агентом."
---

# OpenRouter Delegate для ВКМ

Главным исполнителем остаётся Codex. Сначала используй существующие сведения проекта; передавай только ограниченную задачу. Удалённая модель возвращает JSON, без shell, Git, SQL, инструментов или самостоятельного доступа к файлам. Её вывод проверяет и применяет основной агент.

Прочитай [MODELS.md](references/MODELS.md) перед выбором профиля и [VKM_WORKFLOWS.md](references/VKM_WORKFLOWS.md) при подготовке задания. Одна установка относится только к этому проекту. Скрипт не читает ключевые файлы: `OPENROUTER_API_KEY` поступает только через окружение процесса. Не передавай ключ через argv, чат или журнал.

Текущий ограниченный пилот принимает только явно подготовленный synthetic job JSON: prompt, необязательные inline PNG/JPEG и схема результата. Файловые пути и сетевые изображения в задании запрещены. Для закрытых исходников нужна отдельная реализация явного разрешённого пакета входов; этот пилот автоматически их не читает.

Из корня проекта:

```powershell
python .agents/skills/openrouter-delegate/scripts/openrouter_call.py discover --output work/delegate-endpoints.json
python .agents/skills/openrouter-delegate/scripts/openrouter_call.py check --job work/job.json --metadata work/delegate-endpoints.json
python .agents/skills/openrouter-delegate/scripts/openrouter_call.py mock --job work/job.json --metadata work/delegate-endpoints.json --run-dir work/mock-delegate
```

Платный `call` требует разрешённый владельцем бюджет партии `--budget-usd`, общий для партии `--ledger`, отдельный `--run-dir`, ключ в окружении и проверенный через GET `/key` конечный лимит ключа. Указывать баланс вместо бюджета нельзя. Команда выполняет ровно один POST без повторов; timeout, неизвестная стоимость или незавершённая reservation блокируют новые расходы партии до ручной сверки. Не удаляй ledger/lock ради повторного вызова.

```powershell
python .agents/skills/openrouter-delegate/scripts/openrouter_call.py call --job work/job.json --metadata work/delegate-endpoints.json --budget-usd 0.05 --ledger work/private-batch/ledger.json --run-dir work/private-batch/probe-1
```

`0.05` — пример синтаксиса, не расходное разрешение. Пиши receipts и результаты в приватный/ignored каталог. Скрипт сохраняет хеши, разрешённые metadata, usage и стоимость; reasoning и секреты не сохраняет. Ошибки дают ненулевой exit code. Mock не доказывает доступность API. `PROVENANCE_UNCONFIRMED` означает известную стоимость, но недостаточное подтверждение точного endpoint; научного PASS или принятия результата моделью здесь нет.

Узкое явно разрешённое владельцем исключение для трёх synthetic smoke-проб: `call --owner-local-cap-only --budget-usd 1` с **одним и тем же ledger для всей партии**. Допускается бюджет не более $1; максимум три успешные reservation за всё время ledger, включая известные ошибки исполнения. Обнуление счётчика и создание нового ledger ради дополнительных вызовов не разрешены. При `GET /key` с `limit=null, limit_remaining=null` receipt честно пишет `server_key_limit_status=UNLIMITED_OBSERVED`, `override=EXPLICIT_OWNER_LOCAL_CAP_FOR_THREE_SMOKES`; лимит1 не выдаётся за серверный. Без явного флага сохраняется `LIVE_BLOCKED_KEY_LIMIT`. Unknown/pending блокируют партию и при этом исключении. Флаг не является общим разрешением на будущие расходы.

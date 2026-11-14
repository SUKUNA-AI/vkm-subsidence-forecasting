# Ограниченные задания

`cheap_code`: маленькая функция с сигнатурой, входами и тестируемым контрактом. Ответ — кандидат кода внутри JSON result, исполняет и проверяет Codex.

`vision_documents`: только нужное synthetic изображение с вопросом, известными width/height и рамкой [x0,y0,x1,y1]. Не текстовый путь. Реальные документы сначала искать существующим корпусным маршрутом; этот пилот не читает PDF и не включает OCR plugin. Проверять числа/координаты детерминированно.

`critical_review`: замороженный ограниченный diff/контракт и evidence; не подсказывать ожидаемую оценку. Для smoke использовать заранее внесённый дефект. Замечание модели проверяет Codex.

Минимальный job:

```json
{"task_id":"synthetic-add","synthetic":true,"profile":"cheap_code","prompt":"Return a Python add(a,b) function in result.","max_tokens":512,"schema":{"type":"object","properties":{"result":{"type":"string"}},"required":["result"],"additionalProperties":false}}
```

Допустимые поля: task_id, synthetic, profile, prompt, max_tokens, schema, images. `images` — максимум два объекта {mime,data_base64,width,height,bbox}; только проверяемые PNG/JPEG до 1 MiB каждый, максимум 2048 по стороне/4M пикселей суммарно. Prompt до 16000 UTF-8 байтов; schema до 8192 байтов, без $ref и внешних ссылок. Не использовать изображения для передачи секретов или персональных данных. Маркер synthetic — утверждение подготовившего пакет, не автоматическое доказательство содержания.

Ledger содержит бюджет, key fingerprint, оплаченные суммы, reservations и блокировку unknown. Файл lock не удалять при конкурирующем запуске. Зависший pending или unknown снимает только оператор после сверки кабинета/generation, сохраняя исходный ledger и receipt; автоматического reset/forgive нет. Один key/партия должны использовать один ledger; отдельный серверный key limit защищает совокупные расходы других программ. Ненулевой статус запрещает считать результат успешным; reservation остаётся при неизвестном исходе.

Явный `--owner-local-cap-only` — исключение только для разрешённых трёх synthetic smoke-проб с локальным бюджетом<=1USD. При unlimited server key общий бюджет контролирует один ledger; защиты расходов других программ серверным cap здесь нет. Persistent `reservation_count` увеличивается внутри process-safe lock и не уменьшается после settle; четвёртая reservation запрещена. Менять режим/budget/key существующего ledger нельзя. Это не отменяет unknown-блокировку, stop/no retry или ограничения маршрутов.

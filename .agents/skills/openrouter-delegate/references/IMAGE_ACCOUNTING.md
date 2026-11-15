# Необязательная точная оценка изображений Hydrogen

По умолчанию действует прежняя оценка f4: UTF-8 bytes + 4096 + 4 на каждый pixel. Она не является измеренной токенизацией.

Узкий opt-in доступен только для V2 `vision_documents`, `google/gemini-4-hydrogen`, `google-ai-studio/flex`. Другие модели и маршруты используют прежнюю оценку; предъявленный несовместимый proof блокирует запрос. Параметры изображения в wire не меняются: исходные PNG/JPEG, без resize и `detail`/media-resolution override.

Основание: [Google media-resolution](https://aistudio.google.com/docs/media-resolution) содержит пример `gemini-4-hydrogen` и таблицу Gemini 4; [страница Hydrogen](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/4-hydrogen) закрепляет модель. Максимальная документированная allocation — 2240 на изображение (`ultra_high`), default1120 не используется для резерва. **Значения таблицы приблизительные. Это консервативная оценка с запасом (guarded conservative estimate), не доказанная абсолютная граница счёта и не provider tokenizer.** Перенесено существующее native правило `GEMINI4_MAX2240`, а не создана новая маршрутизация.

Расчёт: полный сериализованный request с SHA-markers вместо base64 + 4096 + N×(2240+256). В text входят prompt, все fragments и их locators, JSON schema, настройки и markers. Входная ставка — максимум prompt/cache read/cache write; выходная — сумма completion и internal_reasoning на весь прежний `max_tokens`. Сохраняются запас25%, отдельная ставка image, свежие endpoint metadata, context/completion/prompt caps и прежние финансовые проверки. Requested HIGH/media settings и effective observations разделены; отсутствие API не подтверждает effective reasoning.

В `check`, `mock`, `call` добавлены два совместно обязательных флага:

```text
--image-rule-proof <root-reviewed.json> --approved-image-rule-sha256 <root-approved-canonical-JSON-SHA256>
```

SHA здесь — `digest(proof)` существующего клиента (канонические JSON bytes через `encoded`), а не SHA pretty-printed файла; хранить оба значения в handoff. Задание не может само включить правило. Root проверяет proof и передаёт утверждённый digest отдельно. Статус `READY_FOR_REVIEW` не разрешает правило. Изменение статуса требует нового SHA и отдельного решения root; тестовый in-memory `APPROVED_BY_MAIN_CODEX` не является полномочием.

Proof `vkm.delegate.image-rule/1` имеет точные поля: `schema`, `status`, `task_id`, `input_sha256` (`digest(job)`), `model_id`, `provider_only`, `rule`, `checked_at`, `expires_at`, `sources`, `total_pixels`, `decoder`, `primary_docs`, `qualification`, `supported_parameters`. Время проверки и expiry — timezone-aware ISO8601; возраст/срок не более24h. `sources` точно повторяет упорядоченные SHA256/mime/width/height/bbox всех inline images. `decoder` закрепляет PIL version и SHA пяти реально импортированных модулей (`PIL`, `Image`, PNG/JPEG plugins, compiled core), имена файлов без произвольных путей. `primary_docs` содержит обе URL выше и SHA снимков; root проверяет снимки и применимость до утверждения. `qualification` содержит SHA принятого native алгоритма и native receipt. Параметры endpoint должны совпадать с proof и известными категориями.

Root SHA является внешней границей доверия. Клиент не читает пути/URL из proof, не скачивает документы и не выдаёт полномочия по текстовой метке. Он проверяет SHA всего задания и каждого изображения, реальные размеры, header/container, полный decode, один frame, metadata/secret guards и pins установленного decoder. Произвольные source-file reads отсутствуют. Отсутствующее/просроченное/изменённое/неутверждённое доказательство блокирует opt-in до чтения ключа в `run`; никаких fallback или reduction лимитов.

Тесты содержат только synthetic pixels и in-memory approval fixtures. PRIVATE source packets, root approvals, decoder host paths, ключи и ledger не входят в Skill. Действующие Kimi API ban, Carbon403, общий budget/leases/диагностический резерв и HTTP redaction не изменены.

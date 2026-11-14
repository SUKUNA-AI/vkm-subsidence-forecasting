# Закреплённые модели и проверка маршрута

Точный выбор модели и провайдера сохраняется. [DELEGATION_POLICY_V2.json](DELEGATION_POLICY_V2.json) задаёт исполняемую политику; [ROUTING_V2.md](ROUTING_V2.md) — выбор специалиста и учёт бюджета; [VKM_WORKFLOWS.md](VKM_WORKFLOWS.md) — явный пакет входов и native-проверку результата.

| Профиль | Requested model | `provider.only` | Provider name | Потолок запроса |
|---|---|---|---|---|
| `vision_documents` | `google/gemini-4-hydrogen` | `google-ai-studio/flex` | `Google AI Studio` | $0.30 |
| `engineering_research` | `moonshotai/kimi-k3.1-pro` | `decart/mxfp4` | `Decart` | $1.00 |
| `cheap_code` | `deepseek/deepseek-v4.1-flash-1017` | `stepfun/fp8` | `StepFun` | $0.10 |

Потолки — финансовые ограничения, не цены моделей и не дополнительный бюджет. Текущая партия использует ранее разрешённые $20 совокупно, тот же ledger с прежними расходами/резервациями и отдельным диагностическим reserve. Более высокий внешний server cap не расширяет это разрешение; новый ключ, пополнение или сброс ledger не выполняются. Запись $1 в старом budget-template не переопределяет принятое разрешение этой сессии; legacy smoke-исключение <=$1 остаётся отдельным узким режимом и не даёт V2 live bypass.

`critical_review` / Carbon остаётся **BLOCKED_403**, включая `google/gemini-4-carbon` и canonical `google/gemini-4-carbon-20261110`. Его нет в активных профилях. Check/mock/discover/call не обходят блокировку; автоматического fallback на другую модель/провайдера/тарифный маршрут нет.

## Alias и canonical

| Единственный requested alias | Допустимый canonical в наблюдаемом ответе |
|---|---|
| `google/gemini-4-hydrogen` | `google/gemini-4-hydrogen-20261112` |
| `moonshotai/kimi-k3.1-pro` | `moonshotai/kimi-k3.1-pro-20261023` |
| `deepseek/deepseek-v4.1-flash-1017` | `deepseek/deepseek-v4.1-flash-20261017` |

Запрос отправляется на alias из таблицы, маршрут содержит ровно один полный provider slug, `allow_fallbacks:false`, `require_parameters:true`. Chat/generation должны сообщить этот alias либо его единственный закреплённый canonical. Другая дата, `latest`, совпадение префикса или чужой model ID не допускаются. Requested alias, canonical pin и фактически наблюдённые model IDs записываются раздельно. Все обнаруженные противоречия model/provider/endpoint/tier/quantization отклоняются, даже если другое поле совпало с pin.

## Реальные возможности и reasoning

Перед live клиент заново получает exact endpoint metadata и модельный каталог. `discover --task-kind <kind> --profile <profile>` сохраняет оба источника. Поддержка reasoning определяется свежими `supported_efforts`/`mandatory` модели **и** обязательными параметрами закреплённого endpoint; источник этой capability evidence не является подтверждением внутреннего режима исполнения.

Снимок модельного каталога от14.11.2026 показывал:

| Модель | Catalog default | Catalog supported efforts | Проектный выбор |
|---|---|---|---|
| Hydrogen | `medium`, reasoning mandatory | `xhigh`, `high`, `medium`, `low` | `high` для содержательных задач |
| Kimi | `max` | `max`, `high`, `low`, `none` | `high`, не неявный catalog `max` |
| DeepSeek | `high` | `max`, `high`, `low`, `none` | обычно `high`; task defaults ниже |

Это датированный capability snapshot, не обещание текущего provider-effective effort. Исполняемая политика задаёт для DeepSeek `mechanical→none`, `data_transform→low`, остальные задачи→`high`. Для сложной ETL/алгоритма/преобразования native-координатор явно указывает `reasoning_effort:"high"`; общий label `data_transform` не должен незаметно снижать требуемую содержательную работу. Kimi `max` или Hydrogen `xhigh` требуют обоснованного исключения, достаточного резерва и подтверждённой поддержки; одного наличия уровня в policy `hard_efforts` недостаточно. Unsupported/unknown отклоняется до POST, без тихого снижения.

Для выбранного high wire содержит `reasoning:{enabled:true,effort:"high"}`. `reasoning_policy` сохраняет caller requested, selected, selection basis, wire, catalog default, supported efforts и область evidence. `reasoning_verification` отдельно сохраняет provider echo и effective state. Нет echo — effective effort null/UNCONFIRMED, даже при успешном HTTP200. Provider-reported режим обозначается `PROVIDER_METADATA_REPORTED_NOT_INDEPENDENTLY_MEASURED`, не HIGH_CONFIRMED и не независимое измерение reasoning. Reasoning tokens и полезный content проверяются отдельно; завершение `length` с null content не считается выполненной задачей.

Размер разрешённого задания определяется `context_length` и `max_completion_tokens` **выбранного endpoint**, modalities, ценой и необходимым содержимым задачи. Общий `/models.top_provider` с большим окном не устанавливает лимит закреплённого Flex/Decart/StepFun endpoint. Прежних smoke-пределов 16KiB prompt /4096 либо32768 completion /2 изображения /2048px в V2 нет. `max_tokens` — положительное число в реальном endpoint completion limit; общий контекст учитывает ответ и reasoning. Локальные parser/transport/decode guards и текущая поддержка inline text/PNG/JPEG — ограничения реализации клиента, не пределы возможностей модели. Video/audio/file modality каталога не означает реализованный клиентом импорт этих форматов.

Примеры **сохранённых exact-endpoint snapshots** от14.11.2026, а не постоянные лимиты или замена нового GET:

| Exact endpoint | Snapshot UTC | Context | Max completion | Prompt / completion за1M tokens |
|---|---|---:|---:|---|
| `google-ai-studio/flex` | 15:50:05 | 1048576 | 943718 | $0.75 /$3.75 |
| `stepfun/fp8` | 15:48:32 | 1000000 | 384000 | $0.105 /$0.42 |
| `decart/mxfp4` | 13:13:35 | 409600 | 368640 | $1.22 /$6.10 |

Здесь, например, Kimi Decart имеет409600 context, хотя global top_provider модели в другом снимке объявлял большее окно. Актуальное значение для каждого live берётся заново из выбранного endpoint; таблица не разрешает oversized запрос. Hydrogen snapshot отдельно сообщал internal_reasoning $3.75/1M и image $0.00000075/input image; они учитываются согласно раскрытой резервной оценке, не смешиваются с observed bill. Endpoint-supported reasoning/max_tokens/structured output параметры и modalities проверяются заново; исторический список не доказывает поддержку конкретного нового V2 запроса.

## Тариф и резерв

Тариф читается из свежей записи именно закреплённого endpoint. Историческая цена или global/top-provider не заменяют этот snapshot; upstream discount повторно не применяется. Перед POST неизвестная категория/цена отклоняется, потенциальный расход резервируется атомарно в общем ledger с учётом spent и inflight. Actual usage/cost после ответа сохраняется независимо от валидности результата; неизвестный outcome/cost не становится нулём и блокирует новые расходы до сверки.

В текущем V2 клиенте input reserve использует **консервативную оценку**: UTF-8 bytes текстовой части +4096 protocol guard +4 на pixel изображения. Последнее явно помечено `CONSERVATIVE_ESTIMATE_NOT_MODEL_TOKENIZATION_OR_ACTUAL_USAGE` / `LEGACY_SAFETY_OVERESTIMATE_PENDING_EXACT_PROVIDER_ACCOUNTING`: это не точная токенизация Hydrogen/Gemini и не наблюдение actual tokens. Для output применяется верхняя оценка completion+internal_reasoning; общий резерв имеет safety multiplier. Сохранять метод/верхнюю оценку отдельно от фактических usage.

`pricing.image` обрабатывается как USD за входное изображение, умножается на число изображений, а не на число пикселей; консервативный prompt token estimate может завышать расход дополнительно. Это явно раскрытый possible overcount, не доказанный тариф «за пиксель». При отказе context/cost estimator выясни причину: реальный лимит, финансовый cap или консервативная оценка. Не переключай профиль и не уменьши данные до нечитаемого вида автоматически. Native-владелец может подготовить другую содержательную подзадачу/новый crop с новыми SHA и provenance.

## Подтверждение маршрута

POST включает `X-OpenRouter-Metadata: enabled`. Проверяются chat и `GET /generation?id=...`, generation ID, provider responses и выбранный router endpoint. Имя провайдера подтверждает только провайдера:

- Hydrogen Flex: нужен наблюдаемый точный endpoint `google-ai-studio/flex` либо `service_tier:flex`.
- DeepSeek FP8: нужен наблюдаемый точный endpoint `stepfun/fp8` либо `quantization:fp8`. Прежние исполнения имели `PROVENANCE_UNCONFIRMED`: StepFun name или FP8 в каталоге сами по себе этот статус не закрывают. Дополнительное Kimi inference правило к DeepSeek не применяется.
- Kimi MXFP4: нужен наблюдаемый `decart/mxfp4` либо `quantization:mxfp4`, или строго ограниченный вывод ниже. Tier и BYOK, противоречащие закреплённому маршруту, отклоняются.

Недостаточное proof — ненулевой `PROVENANCE_UNCONFIRMED`; явное противоречие — `ROUTING_MISMATCH`. Отсутствующее observed endpoint/quantization остаётся null. При недостаточном provider field не повторяй оплаченный POST: сначала используй сохранённую generation/routing/catalogue evidence и отдельную переоценку, не переписывая прежний receipt.

## Строгий вывод о Kimi endpoint

Если точный endpoint/quantization не сообщён явно, `UNIQUE_PINNED_ENDPOINT_AND_ROUTER_SELECTION` допускается только при всей совокупности:

1. Свежий snapshot (не старше24ч; live заново получает metadata), корректный hash и нужный alias/canonical. Среди **всех Decart endpoints каталога этой модели** ровно один, совпадающий с выбранным: provider `Decart`, tag `decart/mxfp4`, quantization `mxfp4`, service_tier null, status целое0. Выбор одной подходящей строки из неоднозначного списка недостаточен.
2. Router `requested` точно равен requested alias Kimi, `strategy:direct`, `attempt` целое1, `is_byok:false`.
3. `endpoints.available` имеет ровно одну запись: `selected:true`, provider `Decart`, model равен закреплённому alias/canonical. `endpoints.total` не подменяет длину available.
4. Ни одно фактическое поле в chat/generation/router/provider_responses не противоречит модели, provider, endpoint, tier, quantization или BYOK pin. Явное противоречие не скрывается singleton-каталогом.

Receipt сохраняет basis, snapshot hash/date, catalog endpoint и router selection отдельно от observed полей. `physical_precision:NOT_MEASURED` остаётся таким и при подтверждённом маршруте: реклама MXFP4/FP8 не является измерением исполнения или научной валидацией. Для Hydrogen/DeepSeek эта ветка вывода недоступна.

Privacy/data collection/retention/training/ZDR фиксируются по свежему exact endpoint; отсутствующие сведения UNKNOWN. Не объявляй Flex ZDR по старой концепции или screenshot. Действующие разрешения PRIVATE и явный V2 input package не разрешают публикацию источников, раскрытие секретов или model tool execution.

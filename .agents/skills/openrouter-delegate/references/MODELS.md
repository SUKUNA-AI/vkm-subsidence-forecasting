# Профили и контракт маршрутизации

| Профиль | model | provider.only | потолок вызова USD |
|---|---|---|---|
| vision_documents | google/gemini-4-hydrogen | google-ai-studio/flex | 0.30 |
| engineering_research | moonshotai/kimi-k3.1-pro | decart/mxfp4 | 1.00 |
| cheap_code | deepseek/deepseek-v4.1-flash-1017 | stepfun/fp8 | 0.10 |

Carbon (`critical_review`, `google/gemini-4-carbon`, подтверждённый canonical `google/gemini-4-carbon-20261110`) сохранён только как `BLOCKED_403`: его нет в активных профилях, call/mock/check/discover завершаются отказом до transport. Автоматического fallback на Kimi или другую модель нет.

Точные допустимые wire-пары alias/canonical, подтверждённые каталогом14.11.2026 и закреплённые в read-only mapping клиента:

| requested alias | допустимый canonical |
|---|---|
| google/gemini-4-hydrogen | google/gemini-4-hydrogen-20261112 |
| deepseek/deepseek-v4.1-flash-1017 | deepseek/deepseek-v4.1-flash-20261017 |
| moonshotai/kimi-k3.1-pro | moonshotai/kimi-k3.1-pro-20261023 |

В chat/generation допускаются alias либо его единственный проверенный canonical; другая дата, latest, prefix match или ID чужой модели дают ROUTING_MISMATCH. Запрос остаётся на alias и прежнем exact provider pin. Receipt разделяет `requested_model`/`model_identity.requested_alias`, actual chat model и `actual_generation_model`; canonical pin отдельно помечен как pin, а не выдуманное наблюдение.

Проверенный endpoint Kimi Decart MXFP4: status0, quantization=mxfp4, context409600, max_completion_tokens368640. Snapshot GET endpoint metadata от14.11.2026: prompt$1.22 / completion$6.10 / input_cache_read$0.122 за1M токенов; указанная discount0.39 может меняться. Тарифы уже взяты из snapshot, скидка повторно не применяется. Потолок1USD переиспользован как локальное ограничение профиля; он не создаёт нового бюджета.

Потолки не являются бюджетом или ценами. `discover --profile <один профиль>` читает текущие public endpoint metadata только выбранной модели, включая альтернативные цены и доступные data-policy fields; точная запись выбирается по tag, model_id, service_tier/quantization и supported_parameters. Источник тарифов — выбранная запись, максимум возраста snapshot 24 часа; live повторно читает metadata перед POST. Параметры: reasoning.enabled=true, provider.only=[полный slug], allow_fallbacks=false, require_parameters=true, max_tokens, response_format=json_schema. Paid plugins/tools, другие модели, tier и fallback не принимаются из job.

Оценка резерва консервативна: UTF-8 байты сериализованного запроса + 4096 входных токенов, для изображения дополнительно 4 токена на пиксель; выход max_tokens, включая reasoning, оценивается по максимальному тарифу completion/internal_reasoning. Если резерв не помещается — сократи пакет; не ослабляй потолок. Это защитная оценка пилота, а не измерение токенизации. При наличии нового неизвестного платного тарифа вызов блокируется.

Фактические model/provider проверяются в chat и GET /generation?id=...; Flex требует service_tier=flex либо точный endpoint tag, FP8 — quantization=fp8 либо точный tag, Kimi MXFP4 — quantization=mxfp4 либо точный tag decart/mxfp4. Обычное provider_name подтверждает лишь провайдера. Все наблюдаемые endpoint/tier/quantization из обоих ответов проверяются: противоречие не скрывается другим полем. Неподтверждённый endpoint даёт ненулевой PROVENANCE_UNCONFIRMED; отличающийся — ROUTING_MISMATCH. Если metadata generation временно недоступна, POST не повторяется. Cost и usage проверяются независимо от успешности JSON-вывода.

## Дополнительное доказательство маршрута только для Kimi

POST включает `X-OpenRouter-Metadata: enabled`. Если API не сообщает endpoint/quantization непосредственно, допускается **вывод о маршруте** при одновременном выполнении всех условий:

- Свежий snapshot (не старше24ч; каждый live-вызов делает новый GET) содержит ровно один Decart endpoint данного каталога модели: tag `decart/mxfp4`, quantization=mxfp4, service_tier=null, status0. Список всех Decart candidates записан кратко в prepare evidence/receipt; совпадения только выбранной записи недостаточно.
- `openrouter_metadata.requested` точно равен alias Kimi, strategy=direct, attempt целое1, is_byok=false; `endpoints.available` содержит ровно одну запись с selected=true, provider=Decart и проверенным alias/canonical. Поле endpoints.total может считать все маршруты каталога и не подменяет длину available.
- Ни одно явное наблюдение model/provider/endpoint/quantization/tier в chat, generation, выбранной router-записи и provider_responses не противоречит pin. Для этого Kimi endpoint известный tier отсутствует; другой явно указанный tier требует отказа.

Receipt записывает отдельный `route_verification.basis=UNIQUE_PINNED_ENDPOINT_AND_ROUTER_SELECTION`, snapshot hash/date, catalog endpoint и router selection. `observed_endpoint` остаётся фактическим: null не заменяется рекламным quantization. `physical_precision=NOT_MEASURED`: это не измерение физической precision исполнения и не научная валидация. Имя Decart без полного proof, двусмысленный/устаревший каталог, missing fields, selected=false или attempt>1 дают PROVENANCE_UNCONFIRMED. Явное противоречие даёт ROUTING_MISMATCH. Для Hydrogen/DeepSeek дополнительная ветка не действует; прежние Flex/FP8 проверки сохранены.

Официальные маршруты (проверены 14.11.2026): [routing](https://openrouter.ai/docs/guides/routing/provider-selection), [key limits](https://openrouter.ai/docs/api/reference/limits), [generation](https://openrouter.ai/docs/api/api-reference/generations/get-generation). Google AI Studio Flex не следует считать ZDR: концепция указывает retention до 55 дней; public metadata может не раскрывать политику, в receipt это UNKNOWN. Synthetic pilot не передаёт закрытые данные.

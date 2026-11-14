# Профили и контракт маршрутизации

| Профиль | model | provider.only | потолок вызова USD |
|---|---|---|---|
| cheap_code | deepseek/deepseek-v4.1-flash-1017 | stepfun/fp8 | 0.10 |
| vision_documents | google/gemini-4-hydrogen | google-ai-studio/flex | 0.30 |
| critical_review | google/gemini-4-carbon | google-ai-studio/flex | 1.00 |

Потолки не являются бюджетом или ценами. `discover` читает текущие public endpoint metadata, включая альтернативные цены и доступные data-policy fields; точная запись выбирается по tag, model_id, service_tier/quantization и supported_parameters. Источник тарифов — выбранная запись, максимум возраста snapshot 24 часа; live повторно читает metadata перед POST. Параметры: reasoning.enabled=true, provider.only=[полный slug], allow_fallbacks=false, require_parameters=true, max_tokens, response_format=json_schema. Paid plugins/tools, другие модели, tier и fallback не принимаются из job.

Оценка резерва консервативна: UTF-8 байты сериализованного запроса + 4096 входных токенов, для изображения дополнительно 4 токена на пиксель; выход max_tokens, включая reasoning, оценивается по максимальному тарифу completion/internal_reasoning. Если резерв не помещается — сократи пакет; не ослабляй потолок. Это защитная оценка пилота, а не измерение токенизации. При наличии нового неизвестного платного тарифа вызов блокируется.

Фактические model/provider проверяются в chat и GET /generation?id=...; Flex требует service_tier=flex либо точный endpoint tag, FP8 — quantization=fp8 либо точный tag. Обычное provider_name подтверждает лишь провайдера. Неподтверждённый endpoint даёт ненулевой PROVENANCE_UNCONFIRMED; отличающийся — ROUTING_MISMATCH. Если metadata generation временно недоступна, POST не повторяется. Cost и usage проверяются независимо от успешности JSON-вывода.

Официальные маршруты (проверены 14.11.2026): [routing](https://openrouter.ai/docs/guides/routing/provider-selection), [key limits](https://openrouter.ai/docs/api/reference/limits), [generation](https://openrouter.ai/docs/api/api-reference/generations/get-generation). Google AI Studio Flex не следует считать ZDR: концепция указывает retention до 55 дней; public metadata может не раскрывать политику, в receipt это UNKNOWN. Synthetic pilot не передаёт закрытые данные.

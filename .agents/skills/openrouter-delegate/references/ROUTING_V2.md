# Выбор специалиста, reasoning и контроль выполнения V2

Точные model/provider pins и допустимые alias/canonical пары определяет [MODELS.md](MODELS.md). Удалённая модель — специалист без инструментального доступа; native-владелец формулирует задачу и отвечает за результат.

## Разделение работы

| Задача | Предпочтительный специалист | Проверяемый результат |
|---|---|---|
| Backend/CLI, ограниченный модуль, SQL/ETL, parser, преобразование A/B, provenance, GIS-helper | DeepSeek | Кандидат патча/функции и конкретные проверки на приложенных входах |
| Property/metamorphic/negative/regression tests | DeepSeek | Тесты, которые воспроизводят названное нарушение контракта |
| Шаблон, механическая перекладка, форматирование | DeepSeek `low` или `none`, если endpoint подтверждает режим | Сохранение содержания/байтовых инвариантов |
| Исходное изображение + код/наложение ошибки, легенда, подписи, геологическое противоречие | Hydrogen | Наблюдения с локаторами и явными UNKNOWN, без самовольной геопривязки |
| Математическое/геометрическое обоснование, CV/PyQGIS, содержательный code review, документация по evidence | Hydrogen | Контрпример, вывод, проверяемое изменение или замечание к утверждению |
| Сложный multipart/топологический/CAD алгоритм, численная устойчивость, межмодульный GIS/CAD контракт | Kimi | Конкретный алгоритм/патч с предпосылками и проверкой отказов |

Это рекомендации, не автоматическое переключение. Native-координатор выбирает профиль явно. Например, Kimi проектирует инварианты сложного адаптера, DeepSeek реализует отдельный согласованный parser, Hydrogen проверяет соответствие результата изображению — три разных вопроса, не три голоса об одном ответе.

На текущем этапе native-владельцы внешних моделей: Hydrogen — `delegate`, Kimi — `geo_tools`, DeepSeek — `sdb_corrections`. Назначения — состояние данной задачи; при продолжении координатор сверяет активные роли и не запускает конкурирующие владельцы одного результата.

## Reasoning по фактическим возможностям

Задание V2 указывает `task_kind`; допустимы `visual_analysis`, `geological_analysis`, `math_physics`, `engineering_design`, `code_implementation`, `code_review`, `tests`, `data_transform`, `mechanical`. Опциональный `reasoning_effort` задаёт caller; для `max`/`xhigh` требуется содержательное `reasoning_justification` и подтверждённая поддержка точного endpoint/model capability snapshot.

Обычная содержательная задача Hydrogen и Kimi — `high`. DeepSeek — `high` для кода, алгоритмов, тестов и сложных преобразований. Исполняемый policy default для `mechanical` — `none`, для `data_transform` — `low`; native-координатор явно задаёт `reasoning_effort:"high"` сложной ETL/алгоритмической задаче, названной `data_transform`. Поддержка каждого уровня проверяется. Catalog default Hydrogen — `medium`, Kimi — `max`, DeepSeek — `high` в снимке14.11.2026; они не заменяют явную проектную policy. `max` не является обычным проектным выбором Kimi; `xhigh` Hydrogen — отдельное обоснованное исключение, а не замена `high`.

Клиент сверяет свежие `reasoning.supported_efforts`, `default_effort`, `mandatory`, возможности exact endpoint и необходимые request-параметры. Snapshot различает capability уровня модели и конкретного endpoint. Неизвестный/неподдержанный режим отклоняется до POST; выбранный специалист или режим не заменяется автоматически.

Сохранять раздельно:

- `reasoning_policy.caller_requested_effort`, выбранный `selected_effort`, отправленный `wire`, модельный default и список поддерживаемых уровней с областью capability evidence.
- `reasoning_verification.echo_observations`, `provider_reports`, `effective_effort`, `effective_confirmation`, `inference`.
- Фактические reasoning/output tokens и finish_reason. Исчерпанный completion budget с null content — не полезный ответ, даже при HTTP 200 и известной стоимости.

Без фактического echo effective effort остаётся null/UNCONFIRMED. Отчёт провайдера можно записать как `PROVIDER_METADATA_REPORTED_NOT_INDEPENDENTLY_MEASURED`; это не независимое измерение внутреннего режима. Не выводить reasoning_details в общий лог, Git, поиск или отчёт.

## Размер пакета и стоимость

Свежий exact endpoint определяет `context_length`, `max_completion_tokens`, modalities и тариф. Положительный `max_tokens` ограничивается этими возможностями и безопасной оценкой полной стоимости. Общий контекст включает запрос, изображения, ответ и reasoning по опубликованным правилам модели. Старые smoke-числа не являются лимитами моделей.

Технические ограничения parser/transport/decode допустимы для защиты локальной памяти и поддерживаемых типов; фиксировать их как технические guardrails, не выдавать за контекст модели. Не уменьшать изображение или фрагмент до нечитаемого состояния ради устаревшего лимита. Неизвестная цена/категория тарифа блокирует POST. Текущий клиент применяет явно названную консервативную оценку input reserve, в том числе4 на pixel плюс текстовый/protocol guard; это не exact tokenizer и не actualusage. `pricing.image` считается за входное изображение, а не за pixel. Возможное дополнительное завышение prompt reserve и метод записываются отдельно. См. точное описание текущей реализации в [MODELS.md](MODELS.md).

Действующее разрешение этой сессии VKM-GEO-01-MA/V2 — **$20 совокупно**, включая прежние расходы, тот же ledger, без нового пополнения/ротации ключей. Это состояние текущей задачи, не бюджет по умолчанию будущих вызовов. Более высокий наблюдаемый server key cap не расширяет разрешение владельца. Profile ceilings сохраняются: Hydrogen $0.30, DeepSeek $0.10, Kimi $1.00 за запрос; диагностический reserve $0.025 остаётся отдельным. Потолок запроса не является ценой или разрешением на дополнительный расход.

Перед платным вызовом нужны: действующее разрешение scope/budget; проверенный server key limit; свежие capabilities/tariff; закрытая атомарная reservation того же ledger. Учитываются spent + все active/inflight reservations. Резервация содержит amount и подтверждённый lease владельца процесса (host, PID, process birth, deadline/state); наличие PID без проверки рождения не доказывает активность.

Известный результат погашает только собственную reservation. UNKNOWN/timeout без установленных usage/generation сохраняет reserve и блокирует новые расходы; никакого автоматического refund/forgive. Мёртвый, просроченный, PID-reused или legacy pending без подтверждённого lease также останавливает новые расходы до ручной сверки. История, счётчики и receipts сохраняются.

Legacy `--owner-local-cap-only` относится только к прежним трём synthetic smoke-пробам при их отдельном разрешении и локальном бюджете <=$1. Он не разрешает V2 live, реальные исходники или расширение бюджета. Legacy job/receipt можно проверять offline/replay без переписывания истории.

## Приёмка полезного результата

Native-исполнитель сохраняет хеши входов/выхода, requested model/profile/provider endpoint, capability snapshot, reasoning policy/verification, время, usage/cost, generation/request IDs, конкретные тесты и принятую/отклонённую часть кандидата. Повтор POST ради недостающего endpoint поля запрещён: сначала проверяется существующая generation и routing evidence.

Использовать отдельные состояния `API_SUCCESS`, `OUTPUT_SCHEMA_VALID`, `TESTS_PASS`, `SOURCE_CONSISTENT`, `AGENT_REVIEWED`, `HUMAN_REVIEWED`, `SCIENTIFICALLY_ACCEPTED`. Неизвестное не равно PASS. Главный Codex принимает изменение по source/runtime evidence; чужой ответ, provider label, реклама precision или успешный schema check не заменяют эту проверку.

Evaluation V2 связывает стоимость и время с **правильно проверенным результатом**: что принято/отклонено, почему, объём инженерной доработки, тесты, source consistency. Не расходовать бюджет ради квоты вызовов, количества агентов или синтетического рейтинга моделей.

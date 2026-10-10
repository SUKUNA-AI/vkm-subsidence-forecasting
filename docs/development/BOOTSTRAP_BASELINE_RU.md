# PW-02-B: первый закрытый baseline

Реализован отдельный `vkm bootstrap` contract. Он устраняет зависимость первого
baseline от уже квалифицированного previous. Реальное исполнение на CORE/EDGE,
независимый backup, Docker startup и production MCP acceptance **NOT_RUN**.
CPU fixtures не закрывают эти gates.

## Границы и authority

Bootstrap управляет только `vkm-core-shadow` API/read-MCP. Действующий `vkm-core`
проверяется до и после по native container IDs, image/config pins, started-at и
байтам selectors. Его restart, switch или restore этим factory не разрешены.

Нужны заранее подготовленные независимые authority/control/qualification roots,
exact clean source/dependencies, доступные pinned images и пустые external
networks с native ID/config pins. Команд build/pull/down/prune или создания сети
factory не генерирует. Конфигурация не может добавлять shell hooks. Удаление
ограничено проверенными native IDs двух owned shadow receivers; вся пара
проверяется до первого удаления.

`BootstrapStartupAuthority` разрешает только закрытые metadata. В authority
входят candidate identities, request/owner, root, isolation, legacy и backup.
Будущих receipts в ней нет: это исключает цикл startup → acceptance → startup.
`BootstrapIntent.retained_startup` содержит ту же полную identity в независимом
операторском intent. Новый recovery factory читает её, даже если candidate
startup/runtime/environment/recipe/probe files повреждены.

Публичные reads закрыты постоянно при `RuntimeConfig.bootstrap_startup`.
Подложенный OPEN этого не меняет. Metadata route требует отдельный deployment
credential. Обычный `require_serving_acceptance` startup authority отвергает.
Экспериментальные роли и научный допуск не заменяются infrastructure receipt.

## Исполнение

1. Read-only plan проверяет утверждённые bytes, legacy и EMPTY boundary.
2. Под live writer/admission leases публикуется durable CLOSED.
3. Cold create выполняется из exact recipe; фактические stopped Config,
   image/environment/argv/mounts/networks/limits сопоставляются с recipe.
   Только затем фиксируется `BootstrapPreparation`: это **не qualification**.
4. Публикуется startup generation. MAINTENANCE снимается для metadata startup,
   durable CLOSED и exclusive leases сохраняются.
5. Новый API/read-MCP проходит joined native identity, затем полный private
   READ contract, включая 49 tools и denied-principal policy challenges.
6. Owned receivers удаляются; проверяются точный EMPTY и неизменность legacy.
7. Новая cold pair проходит реальный bounded partial start: запускается только
   API, MCP остаётся остановлен. Обе native identities проверяются; выполняется
   второй точный возврат в EMPTY.
8. Третья pair запускается закрытой, повторно проходит private probes и свежий
   native join. Immutable qualification связывает все входы и receipts.
9. Durable registration остаётся `CLOSED_BASELINE_QUALIFIED`,
   `serving_admission=false`, `scientific_admission=false`.

Final и partial preparations имеют разные ссылки: native container IDs после
повторного create меняются. Их нельзя подменять одним digest. Journal хранит
отдельные attempt boundaries; interrupted attempt не становится PASS при retry.

Для вызова используются host-local конфиги и exact SHA:

```text
vkm bootstrap plan --operator-config <control.json> --config-sha256 <sha>
vkm bootstrap execute --operator-config <control.json> --config-sha256 <sha> --confirm-intent <intent-sha>
vkm bootstrap recover --operator-config <control.json> --config-sha256 <sha> --confirm-intent <intent-sha>
```

Это команды будущего разрешённого runtime, а не receipt об их выполнении.
После interruption: recovery до EMPTY, затем повтор execute того же intent.
Existing registration требует свежих intent/probes/native/legacy checks;
повторный ответ не перезапускает receivers. Потеря ACK после durable registration
не уничтожает baseline. Зарегистрированный baseline не удаляется EMPTY recovery:
для него действует coherent previous rollback.

## Backup gate

Production intent обязан содержать `IndependentBootstrapBackup`, привязанный к
candidate, legacy, inventory, counts и разным failure domains. Нужны разные
immutable copy-verification и actual restore-verification receipts. Generic
PASS, `independent=true`, один copy receipt или одинаковые failure domains
не принимаются. Consumer сверяет exact hashes и typed report closure.

Действительное копирование, проверка полного backup inventory и restore выполняют
отдельно квалифицированные operational tools. Их реальные результаты ещё не
получены; schema и metadata consumer сами не доказывают сохранность данных.
Уникальные original/evidence/review decisions входят в S06 inventory отдельно.

## Closed previous и дальнейшее продвижение

`require_closed_baseline` свежо проверяет immutable registration, intent,
preparations, native proofs, два fallback, linear journal и полный private
probe closure. Он не требует текущего CLOSED owner: во время новой транзакции
owner закономерно меняется. Original owner возвращается только при coherent
restore, после native verification. Closed drill имеет отдельную schema и
не заявляет reopened serving. Потеря ACK разрешается durable closed commit.

Shadow baseline не является live qualification. Для live нужны отдельные
[promotion binding](PROMOTION_BINDING_RU.md), actual live private probes,
native model owners и runtime rehearsal. Текущие модели
[UNWIRED](MODEL_OWNER_QUALIFICATION_RU.md); этот blocker сохраняется.

Полная DOCUMENT fidelity, научные связи/временные оси, независимая приёмка и
S26 corpus campaign остаются отдельными работами. Этот infrastructure gate
не пересчитывает данные и не переводит extraction в научный FACT.

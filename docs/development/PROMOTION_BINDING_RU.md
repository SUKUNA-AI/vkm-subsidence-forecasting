# PW-02-P: immutable shadow → live binding

Реализован preflight контракт `update.promotion`. Он проверяет разрешённое
отображение исходной shadow qualification в live recipe **без запуска сервисов,
переключения selectors и открытия admission**. Результат имеет статус
`BOUND_NOT_RUNTIME_QUALIFIED`. Это не READY, не доказательство текущей loaded
model и не scientific admission. Реальная qualification остаётся отдельным gate.

## Что закреплено

`PromotionBinding` сохраняет exact bytes/SHA исходных shadow plan, drill,
acceptance и полного замыкания immutable probe/event receipts. У shadow plan
сохраняются исходные scope, deployment profile hash и isolation hash. Live recipe
не переименовывает shadow receipt в live acceptance.

Обе `PromotionRecipe` содержат exact `CoreOperatorConfig`, ровно candidate и
previous release, обе effective API/MCP configuration и полный набор связанных
JSON control/qualification документов. `BoundFile` бинарников Docker/Compose
проверяется как bytes; бинарники не исполняются. Ссылки на оригиналы корпуса
не открываются. Compose проверяется существующим фиксированным контрактом
`CoreUnitControl._compose` без создания control object или запуска commands.

Effective configuration — полный hash input существующего
`container_fingerprint`: `config`, `host_config`, `mounts`, `networks`.
Проверяется exact digest, проект и service ownership. Сохранённая конфигурация
сама по себе не доказывает текущую running configuration; после rebind native
Docker/API/MCP proof по-прежнему обязателен.

`RetainedPrevious` закрепляет whole coherent previous generation, receiver
release и обе retained topology. `RecoveryEvidence` содержит отдельные pinned
receipts зарегистрированных backup/restore verifiers. Wrapper проверяет exact
generation/release/native bindings и source receipts. Подлинность и полноту
конкретного backup протокола повторно проверяет его runtime adapter; произвольная
строка `PASS` не разрешает switch. Synthetic recovery evidence допускается
только при synthetic mode; он не становится runtime proof.

## Разрешённые изменения

Каждое изменение перечисляется отдельной `Mapping` с document ID, JSON pointer,
kind и exact `before`/`after`. Поддерживаются только:

- `vkm-core-shadow` → `vkm-core` в registered project/name/ownership полях;
- authority/control/qualification roots;
- originals/canonical/evidence roots и явно разрешённые path-поля внутри них;
- фиксированные API и read-MCP receiver origins, указанные в operator profile.

Prefix mapping сохраняет точный suffix. Root одной роли нельзя подменить root
другой роли; endpoint API нельзя объявить endpoint MCP. Shadow writes не
пересекают live control/authority/qualification/protected roots. Все четыре
receiver origins различаются. Неиспользованные, неизвестные, дублирующиеся
mapping и неперечисленные изменения отклоняются.

Code, dependencies, image IDs, scientific data, components, policies, access
semantics, READ principal, models, capabilities, resource limits и commands
сохраняются. ServiceIdentity сейчас требует **exact совпадения**, включая
endpoint/instance/runtime: перенос внешнего model owner или service endpoint
нуждается в отдельной native qualification. Этот conservative gate нельзя
обходить добавлением произвольной mapping.

Derived hashes внутренних документов, runtime и effective configurations
сравниваются через **проверенные exact ссылки** на обе версии документа. Перед
этим повторно проверяются исходные SHA и canonical model hash. SHA не удаляется
из исходного profile/plan/receipt; helper normalization не является новым
квалифицированным profile и никуда не публикуется.

## Factory integration

До `CoreUnitControl(...)`, `NativePortal(...)` и любого startup/select/restart
вызвать:

```python
preflight_promotion(
    approval,
    approved_binding_sha256=operator_approved_sha,
    current_policy_sha256=observed_policy_sha,
    current_access_sha256=observed_access_sha,
    selected_previous_generation_sha256=observed_current_generation_sha,
)
```

`approval` и expected SHA поступают из независимой operator authority, никогда
из incoming corpus или ответа модели. Current pins должны быть свежими и
полученными от зарегистрированных наблюдателей. Успех связывается с exact live
profile и candidate/previous manifests.

В `CoreOperator._load` исходный shadow plan допускается для live candidate только
через отдельный exact promotion binding, а не исключением profile из signature.
`require_serving_acceptance` не должен принимать binding как обычную acceptance:
он различает source shadow qualification и live native requalification.
Затем под writer gate непосредственно перед первым side effect нужно повторить
preflight и сохранить exact retained previous recipe в durable intent. После
rebind повторяются actual service/native/API/read-MCP proofs перед durable OPEN.
При mismatch или недоказанном восстановлении доступ остаётся CLOSED.

`CoreOperatorConfig.promotion_approval` — independently bound wrapper input.
Live recipe содержит исходный Core operator config с `promotion_approval=None`;
wrapper отличается только этой ссылкой. Это устраняет hash cycle. Operational
profile hash не включает downstream registration/promotion receipt references,
а full operator config SHA всегда проверяется отдельно. Source shadow profile
hash остаётся в неизменяемом plan и проверяется при live load.

Factory выполняет full preflight до units/portal и снова под writer перед apply.
Порядок переключения: release select → preliminary source/file fence → CURRENT
→ native rebind → private live probes → durable OPEN. Preliminary probe не
проверяет новый native процесс, пока фактически работает previous, и не создаёт
live receipt. При actual live probes повторно проверяются writer, gate, текущий
plan/profile и CLOSED owner; rollback previous не запускает candidate probes.
Post-rebind probes используют отдельную `LivePromotionProbeReceipt` и отдельный
raw-probe schema/context, привязанный к binding и live profile. Она сохраняет SHA
source shadow plan/acceptance; ни исходный план, ни scope не переписываются.
Joined authenticated receiver proof проверяется до/после probes; изменение
instance/PID/start/gate или candidate context блокирует admission. Эта receipt
не принимается ordinary `AcceptanceRegistrar.register` и не повышает scientific
admission. Модельные owners и actual runtime остаются отдельными gates.

Для recovery CLI создаёт **recovery-only** factory. `preflight_promotion_previous`
проверяет только исходный approval/live config и retained previous closure, без
чтения candidate artifacts. Эта capability не может load/apply candidate. Поэтому
повреждение candidate не блокирует точное RESTORE_PREVIOUS; повреждение previous
или его backup/config proof блокирует. Profile и operator config fences остаются.

`OperatorRelease.baseline_registration` подключает `require_closed_baseline` к
typed `PreviousAdmission`. Возврат первого baseline сохраняет CLOSED с original
owner key. Source callback независим от CURRENT/candidate; closed drill registrar
повторно проверяет source registration. Baseline не разрешён у ordinary candidate
и не получает READY: status показывает `CLOSED_BASELINE_QUALIFIED`.

CPU wiring не является actual CORE/EDGE/Docker/backup/runtime qualification.
Реальная acceptance/restore/switch/модельные proofs этими тестами не выполняются.

## Проверки

Targeted synthetic tests проверяют valid полное отображение без файловых writes,
повтор preflight, stale policy/access/previous/approval, byte mutation всех source
документов, missing probe/event, unrelated digest, неизвестные/подменённые mapping,
endpoint crossover, partial rollback/restore, WRITE/admin/upstream/memory/image
capabilities и запрет повышения synthetic scope. Production/Docker/GPU/core
qualification этими тестами не выполняется.

Связанные runbook и gate cards: [CORE operator](CORE_OPERATOR_RU.md),
[следующие gates](../planning/PRODUCTION_DATA_NEXT_GATES_RU.md).

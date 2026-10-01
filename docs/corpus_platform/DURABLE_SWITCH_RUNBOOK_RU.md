# Переключение поколения: admission, durable intent и recovery

Код: `vkm_corpus.update.deployment`, `barrier`, `serving`, `remote_graph`,
`remote_search`; API startup — `vkm_corpus.api.production`.
Связанные контракты: [native adapters](REMOTE_GENERATION_ADAPTERS_RU.md),
[shadow acceptance](../development/SHADOW_ACCEPTANCE_RU.md),
[update runtime](UPDATE_RUNTIME_RUNBOOK_RU.md).

## Граница готовности

Реализованы durable coordinator, kernel barrier, typed native observer factory,
локальные и remote adapter primitives. Synthetic tests проверяют частичный switch,
потерю ACK, повтор, restore и запросы другого процесса. Это не квалификация CORE.
Настоящие native endpoints, service restart, 49-tool shadow acceptance,
аварийное завершение питания и целевая нагрузка остаются NOT_RUN.

`vkm-corpus update` не десериализует произвольные Python/shell callbacks из manifest.
Настоящие selector adapters и управление всеми service receivers регистрируются
операторским кодом через `SelectorAdapter` / `ReceiverControl`.
Сквозная команда эксплуатации для конкретных CORE units ещё должна связать эти
объекты, deployment profiles и внешний rerank identity producer. До её квалификации
remote accept/switch в универсальном update runtime сохраняют статус BLOCKED.

## Обязательные входы

1. Clean checkout / установленный wheel на exact commit с проверенными locks;
   actual code-tree, dependencies, access configuration и source-policy SHA.
2. Immutable DOCUMENT/DATASET/EVIDENCE/NAV manifests, actual DuckDB bytes,
   prepared graph bundle, все physical search indices и native model packs.
3. Previous и candidate `GenerationManifest` включают полный набор компонентов
   и сервисов RETRIEVAL/RERANK/CONTROL. Health JSON не заменяет `ServiceIdentity`.
4. Operator-approved shadow acceptance относится к тем же code/config/policy,
   компонентам, сервисам и byte hashes. SYNTHETIC receipt не допускается вместо
   SHADOW_PRODUCTION / PRODUCTION_SWITCH.
5. Previous selectors и referenced originals сохраняются до принятого срока
   retention. Старое поколение не удаляется при переключении.

## Native serving profile

`RuntimeConfig.native_serving` — `BoundFile` точного JSON
`NativeServingProfile`. В нём `search` задаёт полный набор physical indices,
`graph_manifest` фиксирует общий graph bundle, `control` — native PostgreSQL/worker
контракт. `late_policy_binding` и `late_qualification` фиксируют два вывода
`issue_pack_policy`. Generic JSON с `status=PASS` недостаточен.

Native observers используют реальные backend/client объекты API. При startup:

- graph проверяется полным bounded round-trip; actual native database access
  обязан быть `read-only`, затем проверяются store/server/transaction/schema fences;
- search проверяется полным bounded content scan; write block, mappings, native
  checkpoints и весь alias set должны совпадать;
- loaded retrieval pack/models подтверждаются authenticated `/identity` реального
  сервиса и его load-time resource proof; health не является таким доказательством;
- late issuer на CPU воспроизводит units/text и сравнивает весь index с canonical,
  не пересчитывая embeddings; model/scientific quality остаются NOT_RUN;
- DOCUMENT original bytes и policy совпадают у graph/search/late projections;
- DuckDB lifetime file watch устанавливается до полного hashing;
  повтор cheap stat при изменении bytes не возвращает прежний допуск.

Windows, DrvFS/NFS/FUSE и неподтверждённые filesystem profiles не получают
production qualification. Linux native supported filesystem и физическая durability
квалифицируются отдельно. ASGI lifespan должен завершить binding до обслуживания;
транспорт, обошедший startup, получает UNAVAILABLE.

## Drain и переключение

1. `DurableDeployment.plan(candidate, request_id)` собирает actual previous
   bindings/native identity и создаёт проверяемый plan hash без смены selectors.
2. Проверить candidate через isolated shadow endpoint, original locators,
   policy revocation, все 49 read tools, статус worker и generation consistency.
3. Передать exact plan SHA в `switch(candidate, request_id, confirmation)`.
   Durable intent и PREPARED сохраняются до первой mutation.
4. Закрыть local admission всех зарегистрированных receivers. Kernel
   `admission.lock` удерживает shared lease всего ASGI ответа, включая stream.
   Controller получает exclusive lease только после завершения также запросов
   других процессов, использующих тот же gate.
5. Каждый apply/restore требует writer fence: actual lock inode, kernel lease,
   MAINTENANCE ownership и pinned profile. Новые/replacement receivers также
   обязаны привязаться к тому же gate и проверить CURRENT/MAINTENANCE.
6. Применить exact selector adapters, проверить native observations и candidate
   probes; только после этого записать CURRENT и rebind receivers.
7. Удаление MAINTENANCE не открывает kernel admission преждевременно: exclusive
   gate остаётся удержанным до конца writer transaction.
8. Завершить post-switch probes, проверить status/coverage/policy/worker и записать
   receipt. Потерянный ответ после FINISHED повторяется по тому же request ID и
   не откатывает уже принятое поколение.

Нельзя держать receiver без общего gate или подключать новый процесс только по
health endpoint. Status отдельно показывает `admission_open`; локальный `paused=false`
не доказывает, что global switch разрешил запросы.

## Recovery

При частичном apply coordinator восстанавливает сохранённый previous tuple в
обратном порядке, с writer fence перед каждой mutation и перед возвратом CURRENT.
Потеря fence или failure restore оставляет MAINTENANCE закрытым. Ни новое поколение,
ни прежнее не объявляется доступным по отсутствию exception в одном adapter.

После restart прочитать journal и получить `recovery_plan(request_id, mode=...)`.
Явные режимы — `RESTORE_PREVIOUS` и `COMPLETE_CANDIDATE`. Новый exact confirmation
передаётся `recover`; previous/candidate и native bindings проверяются заново.
Не удалять lock/intent/pending files и не выполнять force-unlock.
Кernel освобождает process lock после смерти процесса; это не разрешение удалить
канонический intent или переписать сохранённые rollback bindings.

Orphan intent без PREPARED не может откатить более поздний завершённый switch.
Изменённый intent с заново вычисленным hash отклоняется журналом: PREPARED хранит
hash исходного плана.

## Что не пересчитывать при code-only update

Original bytes, действительные extraction outputs и embeddings сохраняются при
неизменных encoded inputs, extraction/chunking/model/tokenizer/rules/policy и
подтверждённой совместимости. NAV compatibility report определяет validate/repack
metadata; rebuild нужен только при реальном изменении его содержимого/контракта.
Новый graph/search projection может понадобиться для нового evidence слоя, но
само обновление serving-кода не является основанием запускать весь intake/OCR.

## Production acceptance ещё требуется

На отдельном qualified runtime проверить реальный crash/restart каждой стадии,
8 одновременно читающих клиентов, полные original-backed probes, stale endpoints,
receiver replacement, потерю сети/ACK, actual backup restore и exact rollback tuple.
Ориентиры p95 metadata/object 2 с и bounded search/timeline 5 с — цели измерения,
а не уже установленная производительность. Перед switch сохранить receipt gates
и согласовать короткое окно недоступности, выбранное владельцем.

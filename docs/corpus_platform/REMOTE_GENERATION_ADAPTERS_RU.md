# Native adapters: границы и qualification

Статус: код и синтетические fault tests; физическая qualification OpenSearch,
Neo4j, PostgreSQL, retrieval и rerank — **NOT_RUN**. Никакого переключения
production, загрузки моделей, реального индекса или графа при разработке не было.
Наличие native identity не означает функциональную либо научную qualification.

## OpenSearch

`search.indexer.BuildOptions`, `search.vectors.VectorBuildOptions` и
`search.page_vectors.PageVectorBuildOptions` имеют `publish=False` и обязательный в
этом режиме `policy_sha256`. Подготовка создаёт только новые concrete indices,
завершает загрузку и ставит `index.blocks.write=true`; aliases и старые индексы
не изменяются. Старый `publish=True` сохраняет прежнее поведение совместимости.
Production orchestration должен явно выбирать prepare.

`update.remote_search.SearchBundleSpec` перечисляет полную группу: пять базовых
индексов, vectors и pagevis при наличии этих каналов. `SearchBundleLease` получает
actual client, делает ограниченное полное чтение ID + `_source`, вычисляет
порядково-независимый digest и сравнивает до/после фактические cluster UUID,
index UUID, mappings/settings, native primary-shard sequence checkpoints и count.
Фильтрованные/routed aliases, partial shards, missing policy, незавершённая
загрузка или запись в опубликованный immutable индекс закрывают admission.
Перед возвратом proof повторно проверяются все индексы bundle.

`NativeSearchObserver(deps, spec, max_documents=..., timeout_seconds=...)`
создаёт lease именно из actual `deps.search._connect()`, проверяет общий client
и prefix у `deps.hybrid._search`, затем возвращает JSON identities SEARCH,
DENSE и VISUAL. Он проверяет и уже заполненные metadata caches HybridBackend;
старый cache не переживает переключение в качестве тихо принятого current.
Кандидатный service следует создавать с отдельными свежими backend objects.
`observer.lease.identity` сохраняет native canonical manifest SHA для сравнения
с actual DOCUMENT компонентом внешним generation observer.

`SearchBundleSwitch(previous, candidate, fence=...)` выполняет один atomic
`update_aliases` для полного набора aliases. `apply()` и `restore()` требуют
точного прежнего/нового состояния, независимо прочитанного через actual client.
Чужое/частичное состояние не исправляется догадкой. Индексы не удаляются.
Неоднозначный ответ/потерянный ACK оставляет recovery координатору; после
наблюдения полного candidate можно восстановить exact previous. `fence` —
исполняемая проверка единственного publisher и drained receiver, а не bool в JSON.
Durable intent/receipts и recovery обеспечивает deployment coordinator.

Старый индекс без policy/write block/checkpoints не квалифицируется по одному
сохранённому manifest. Metadata qualification/repack и новый content scan могут
быть нужны; это само по себе не требует пересчёта embeddings. Блокировки записи
и native seqno защищают от штатных мутаций, не от администратора кластера,
подменяющего identity. Endpoint/TLS/network trust — часть operator profile.

## EVIDENCE graph

`graph.shadow.ShadowEvidencePublisher` работает в **отдельной пустой базе** либо
продолжает свой прерванный load с тем же projection/manifest. Actual `db.info`
идентификатор должен отличаться от serving database, в том числе при разных
клиентах/адресах одного backend. Любые чужие nodes закрывают запись до первого
write. Запросы Cypher фиксированы в коде, manifest не задаёт запросы.

Publisher сверяет все artifact SHA, Parquet-derived counts/refs, точное совпадение
JSONL с Parquet, ключи nodes и endpoints edges. MERGE разрешён только без
перезаписи существующей семантики. После загрузки читаются все фактические
nodes/relations, сравниваются keys, predicates, endpoints и payload. COMPLETE
ставится лишь после round-trip и повторного source/policy/database fence.
Interrupted load можно повторить; другой manifest и конфликтующие свойства
блокируются. DELETE/DETACH DELETE/serving switch отсутствуют.

**Граница:** receipt `EVIDENCE_GRAPH_ONLY` не квалифицирует общий GRAPH для
существующих DOCUMENT/NAV запросов. Для этого предназначен отдельный combined
adapter ниже; успешный EVIDENCE-only load не заменяет его проверку.

## Общий DOCUMENT/NAV/EVIDENCE graph

`graph.shadow.CombinedGraphBundle.prepare(...)` принимает точные SHA канонического
snapshot, NAV manifest, EVIDENCE projection manifest, source policy и AccessContext.
Использует существующие чистые DOCUMENT/NAV row projectors и их preflight/accounting;
wipe/sweep loaders не вызываются. Партиции и manifests сверяются по реальным bytes.
Чтение source content следует за проверкой доступа к source IDs. NAV сохраняется
как navigation, EVIDENCE — с исходным policy/role/status; научный статус не повышается.

Промежуточный `graph.sqlite` ограничен explicit `max_records`/`max_bytes`; Python
не собирает весь граф в список. Producer запускается отдельно под OS memory/disk/
time guards. `manifest.json` публикуется последним. Неуспешная подготовка оставляет
незавершённый spool без valid manifest; он не принимается receiver. Для воспроизведения
нужен exact read-only code/dependency profile. Повторные bytes/code fences обнаруживают
штатные изменения, но не обеспечивают атомарность с произвольным редактированием host.

`OriginalOccurrence` разрешается через actual canonical row, включая source SHA,
snapshot, extraction signature/generation, content SHA, locator и span SHA, если
есть span. Отдельный `PinnedOriginalRecord` хранит этот original payload и точную
ссылку. Для старого snapshot нужен явный `SnapshotPin(root, snapshot_id, manifest_sha256)`
и сохранённые проверенные canonical files. Отсутствующая версия закрывает подготовку.
Совпадающий logical object ID в current snapshot никогда не подменяет старую версию.
Historical payload не получает current DOCUMENT label и не нарушает его unique ID.

`CombinedGraphBundle.read(directory, manifest_sha256=...)` перепроверяет реальный
SQLite SHA, целостность, counts, все payloads и endpoints. Это фиксированный формат,
а не разрешение передать SQL/Cypher в manifest. Пути содержатся в operator profile;
машинные пути не включаются в bundle identity.

`update.remote_graph.CombinedShadowPublisher(...).publish(bundle)` работает только
с отдельной пустой базой или своим interrupted generation. Native `db.info` должен
отличаться от serving database. Ownership записывается до DDL, COMPLETE — последним
атомарным изменением metadata всех трёх слоёв. При lost ACK или обрыве повторный вызов
сверяет уже загруженные записи и продолжает отсутствующие; изменённые существующие
свойства не перезаписываются. Полное bounded keyset-чтение сравнивает IDs, полный
payload, labels, типы/направления/свойства всех связей. Чужие nodes/edges, неправильные
DDL, count mismatch либо другое поколение блокируют завершение; DELETE отсутствует.
Совместимые `ProjectionRun`/`NavMeta` и прежние DOCUMENT/NAV labels/types остаются
доступны существующим API readers.

**TRANSFER и receiver qualification различны.** Publisher возвращает
`SHADOW_TRANSFER_ONLY` lease; рабочая запись нужна ему во время загрузки.
`NativeGraphObserver(deps, bundle)` требует actual native `access='read-only'`,
после операторского freeze/restart снова полностью читает граф и проверяет schema.
Никакая операция freeze/config/restart не выполняется adapter автоматически.
Для Community возможен отдельный immutable shadow instance с операторски
квалифицированной read-only конфигурацией. Применимость config и прав проверяется
на фактической версии до начала production update.

Observer связан с настоящим `deps.graph._connect()` и database. Сохраняет native
store ID, server/endpoint/start identity, committed transaction fence и фактические
constraints/indexes с полным fingerprint и ONLINE state. `SHOW DATABASES` extended
поля `databaseID`, `lastCommittedTxn`, `lastStartTime`, `replicationLag` описаны в
[официальном руководстве Neo4j](https://neo4j.com/docs/operations-manual/current/database-administration/standard-databases/listing-databases/).
Отсутствие поля/permission, неоднозначный cluster/replica результат, изменение
native transaction, access, restart или schema закрывают lease. Qualified scope
сейчас standalone; clustered routing этим адаптером не квалифицирован.
`document_origin()` возвращает canonical manifest/policy identity только после
native проверки. JSON selector и старый COMPLETE marker доказательством не служат.

`GraphReceiverSwitch(deps, previousObserver, candidateObserver, fence=...)`
меняет только точную backend reference **под drained/exclusive barrier** и допускает
идемпотентный apply/restore. Предыдущий receiver остаётся живым и qualified.
Повреждённый candidate не мешает вернуть проверенный previous. Чужая reference
блокируется. Durable intent, process restart/reconstruction, global generation
ordering и opening barrier принадлежат внешнему coordinator. Adapter не меняет
remote aliases/config, не останавливает services и не удаляет previous graph.

Код этого пути проверяется synthetic canonical/NAV/EVIDENCE Parquet и adversarial
fake-driver тестами. Реальные Neo4j права/DDL, read-only freeze, полный 49-tool MCP
acceptance, restart/lost-ACK/drain/restore drill пока **NOT_RUN**. Они нужны для
физической qualification; synthetic GREEN не открывает production.

## Файлы pack и loaded model

`PackHandle.bind_qualified_identity(expected_manifest_sha256=...)` сравнивает
фактические loaded manifest bytes, полные SHA tokens/index, load-time inode/stat
и native Linux mutation watch. Это выполняется один раз до admission. При каждом
чтении identity проверяются selector, файл и watch; hot reload внутри этой
генерации запрещён, требуется новый service/receiver и новая acceptance.
Legacy profile продолжает использовать прежний reload.

Для старого late pack, чей manifest не содержит canonical manifest SHA, применяется
`update.pack_policy.issue_pack_policy(PackPolicyRequest(...), output_dir)`.
Это CPU-only проверка, а не повторное encoding. Закрепляются canonical snapshot и
полные SHA его файлов, pack manifest/tokens/index, source policy и AccessContext.
Issuer запускает фиксированный дочерний модуль под RLIMIT_AS и hard process-tree
timeout, ограничивает disk/spool/units и размер одного source до загрузки его rows.
Выход — новая private directory; оригиналы и pack не изменяются.

Через существующие `retrieval_lab.units.build_units/render` воспроизводятся
детерминированные unit IDs и exact rendered text hashes. Default UnitConfig и
context variant берутся из проверенного pack text-rule contract; unknown rule
блокируется. Для **каждой** строки actual `index.parquet` сравниваются unit kind,
source/page/object IDs и text hash; duplicates, extras, missing units, неправильные
offsets/token counts/file bytes не допускаются. Заданный набор unit kinds является
явным scope; он не означает полноту извлечения всех объектов документа. SHA и code
fences повторяются перед receipt. Никакого чтения моделей, inference и GPU нет.

`binding.json` сохраняет пять полей старого binding contract. Отдельный
`qualification.json` формата `vkm-late-policy-qualification/1` связывает его SHA
с девятью полями actual loaded-pack identity, canonical/policy/context pins,
issuer/rule/dependency identity, точным digest и counts проверенных units.
`NativeServingProfile.late_qualification` закрепляет SHA этого receipt;
`verify_pack_policy_receipt(bytes, expected_sha256, binding, loaded_pack)`
проверяет тип, полноту проверок, текущий issuer и точное совпадение с authenticated
native identity реально загруженного pack. Generic JSON с PASS не принимается.
Approval конкретного receipt SHA относится к доверенному operator profile; receipt
не заменяет native loaded-file proof и не может быть произвольно написан sidecar.

Scope receipt — `CANONICAL_UNIT_POLICY_BINDING`. Качество векторов, качество модели,
полнота научной extraction и scientific admission остаются **NOT_RUN**. Не требуется
пересчитывать embeddings при совпадающих unit text hashes и config. При отличии
issuer/rules сначала выпускается новое CPU qualification; переобработка зависит
от actual changed inputs, а не от одной даты обновления API.

Одного `mtime/ctime` недостаточно даже на Linux: синтетический regression показал
совпадение всех timestamps при близких операциях. `update.native_files.NativeFileWatch`
устанавливает inotify **до загрузки**, отслеживает MODIFY/ATTRIB/CLOSE_WRITE,
DELETE_SELF/MOVE_SELF; overflow, потеря watch либо любое событие навсегда закрывает
lease. SHA большого файла не пересчитывается на каждом HTTP-запросе.
Поддержаны Linux local ext*, XFS, btrfs, tmpfs, overlayfs. Windows, DrvFS, FUSE и
сетевые mounts не квалифицированы. Нужны operator-owned immutable исходники;
это не защита от привилегированного атакующего. Watch следует закрывать вместе
с владельцем pack/model. Документный DuckDB должен иметь такой же lifecycle fence.

`RetrievalServiceLease` проверяет actual owned child PID/start ticks, команду,
исполняемый файл, mmap weights device/inode, владение loopback listening socket,
actual encoder/client, load-time watches и hashes GGUF/tokenizer/heads. External
process, отсутствие mmap evidence или resources вне native filesystem остаются
BLOCKED. `/identity` требует bearer и explicit qualified pack profile; legacy
health не превращается в identity. Functional parity/VRAM/49 MCP — отдельные gates.

## RERANK и CONTROL — отдельные services, не datasets

`ServiceIdentity` отделён от `ComponentIdentity`: instance/code/dependencies/
config/endpoint/runtime SHA, resource digests и capabilities. `GenerationManifest`
и shadow candidate связывают service map с data component map.

Factory для actual serving dependencies:

```python
observer = await NativeServiceObserver.bind(
    service.deps,
    control_spec=ControlSpec(
        schema_sha256=qualified_schema_sha256,
        workers=(WorkerRequirement(host_role="WORKSTATION", kind="PIPELINE"),),
        max_heartbeat_age_seconds=180,
    ),
)
services = await observer.observe()  # dict service -> JSON ServiceIdentity
```

Worker profile — явное решение deployment profile; пример не команда настройки.
Factory использует actual `GatewayRerankBackend._client._http`, actual
`HybridBackend._embed._http` и actual `PgControlPlane._run`. Подмена backend,
client или endpoint закрывает admission. Только authenticated GET `/identity`;
нет inference, alternate sidecar URL и fallback на `/health` или `/readyz`.
Все RETRIEVAL/RERANK/CONTROL нужны для полного production READ contract.

CONTROL читает фиксированные pg_catalog queries в READ ONLY REPEATABLE READ
transaction с statement timeout; всегда rollback/close. Проверяются native
`pg_control_system` system identifier, database OID, endpoint, server/start,
полный schema fingerprint, версия и свежесть явно обязательных worker roles.
Прав доступа не хватает — BLOCKED. Отсутствующий/STOPPED/будущий/stale heartbeat
не READY. Heartbeat доказывает liveness control path; он не доказывает код worker
или успешное исполнение будущего processing job. Данные jobs не становятся
канонической научной истиной. Schema hash должен быть закреплён проверенным
qualification profile, а не выведен из произвольного status и тут же принят.

RERANK gateway имеет authenticated `/identity` и explicit
`VKM_RERANK_QUALIFIED_IDENTITY=1`. Native upstream tokens задаются отдельно через
`VKM_RERANK_TEXT_NATIVE_TOKEN_FILE` / `VKM_RERANK_VISUAL_NATIVE_TOKEN_FILE`.
Код не меняет конфиги и не записывает токены. Local tokenizer/head load-time
bytes/objects и оба actual inference client связываются с native upstream proof.
Неудача bind происходит до warmup. Для model owner есть `LoadedModelLease.load`:
он окружает настоящий loader, связывает before/after hashes/watches,
actual serving object, process, implementation files и installed dependencies;
`observe()` вызывается внутри его authenticated `/identity` handler.

**Оставшийся кодовый blocker:** текущий CareerOps text service и отдельно
запущенный llama-server находятся за внешними интерфейсами и не реализуют этот
load-time identity handler. Их `/readyz`, `model_path`, environment commit либо
ретроспективный hash файлов не дают нужного доказательства. Здесь добавлены helper,
gateway и consumer, но внешний loader/handler ещё должен быть инструментирован
или заменён уже квалифицированным owned-process adapter. Нельзя вручную написать
подходящий JSON: endpoint должен наблюдать реальный serving object/process.
Отдельный llama C++ process требует собственного native load/process adapter;
Python helper не утверждает, что инструментирует его автоматически.

## До физической qualification

1. Exact clean checkout, immutable local Linux resources, pinned dependency/profile
   identity; никаких неявных обновлений service client или моделей.
2. Изолированные shadow indices/database/services и минимальные native read права.
   Проверить поддержку native endpoints/checkpoints/`db.info` на реальных версиях.
3. Квалифицировать общий DOCUMENT/NAV/EVIDENCE graph bridge и закрыть внешние rerank
   model identity hooks. Связать actual component/service observer с isolated acceptance.
4. Полный installed MCP READ набор (49 при текущем контракте), negative access
   probes, source/target policy, generation consistency. NOT_RUN не PASS.
5. Fault drill: interrupted prepare/load, lost ACK, foreign selector, active request,
   mutation after bind, service restart, failed switch и точный restore; запись
   durable lifecycle receipt и проверка после restart координатора.
6. Только после gates выполнять согласованный shadow → switch → verify. При
   ошибке сохранить maintenance, exact previous и failed receipt. Реальные
   remote/GPU/drain/switch действия требуют отдельного разрешённого запуска.

При code-only update неизменившиеся source/canonical, extraction, embeddings и
packs сохраняются. Перепроверяются совместимость, bytes/native identities,
policy, service code и full acceptance; rebuild данных нужен лишь при реальном
изменении producer/input/schema/model contract, а не из-за новой версии API.

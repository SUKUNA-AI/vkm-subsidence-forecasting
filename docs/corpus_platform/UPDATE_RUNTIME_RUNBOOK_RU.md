# Квалифицированный update runtime: локальные adapters и shadow rehearsal

Статус: код и синтетическая проверка. Эта инструкция **не является разрешением production-запуска**.
Исполнитель не пересчитывает embeddings, не выполняет OCR, не переключает CORE/EDGE и не загружает graph/search.
`PASS` стадии означает только заявленный узкий gate. Научный допуск, полнота обнаружения объектов и full MCP
acceptance остаются отдельными receipts; `NOT_RUN`, `BLOCKED` и `FAIL` не преобразуются в `PASS`.

## Входы и неизменяемые привязки

`vkm_corpus.update.runtime.RuntimeConfig` — локальный JSON, вне Git. Поля:

| Поле | Смысл |
|---|---|
| `runtime_root` | Единственное дерево записи: staging, immutable attempt receipts, shadow outputs |
| `originals_root` | PRIVATE-оригиналы, только чтение |
| `canonical_root`, `evidence_root` | Необязательные защищённые деревья DOCUMENT и EVIDENCE, только чтение |
| `policy` | `{path, sha256}` для `vkm-source-policy/1`; никогда не подразумевает PUBLIC |
| `qualification_root` | Проверенные JSON receipts `<sha256>.json`, содержимое должно иметь `status=PASS` |
| `expected_commit`, `dependency_locks` | Точный Git commit и tracked exact dependency locks |
| `memory_budget_gib`, `memory_reserve_gib`, `worker_memory_gib`, `min_free_disk_gib` | Явные положительные бюджеты |
| `artifacts` | Карта логического имени в `{path, sha256}`; никакие shell-команды не допускаются |
| `observations` | Native observers текущих обслуживаемых компонентов, см. ниже |
| `quality_gates` | Карта SHA файла qualification receipt → `{plan_artifact, prediction_artifact, required_scope}` |

Дерево записи не пересекается с originals/canonical/evidence. Полный commit и production dependency identity
проверяются перед plan и непосредственно перед запуском worker. Нечистый checkout или несовпадение pin останавливают
операцию. Модель `CampaignManifest` фиксирует каждый source ID, оригинальный SHA-256, размер, относительный путь,
lifecycle/reason, DAG стадий и qualification gates. `EXCLUDED` — запись учёта, а не разрешение молча импортировать её.

У стадий два digest: `input_sha256` связывает весь список inputs и SHA всех runtime artifacts; `config_sha256`
связывает runtime config, operation и options. Метод `UpdateRuntime.stage_bindings(campaign, stage)` вычисляет их.
`plan` также выводит ожидаемые привязки: первичный черновик с placeholder digest получит `BLOCKED`, после внесения
реальных digest нужно повторить plan. Это не требует обработки документов.

Для `vkm-qualification-report/1` обычного `status=PASS` недостаточно: `_gate` требует `quality_gates` binding,
свежие byte hashes approved FrozenPlan/PredictionSet и `qualification_gate(..., require_corpus=True)`.
`required_scope` явно задаёт обязательные metrics каждой strata. SYNTHETIC_ONLY и unbound quality reports
отвергаются. Infrastructure receipts сохраняют отдельную прежнюю проверку trusted external evidence; они не
подменяют qualification качества корпуса. Контракт и границы: [QUALIFICATION_RU.md](../evidence/QUALIFICATION_RU.md).

## Команды

Ниже `<runtime.json>` и `<campaign.json>` — пути к операторским локальным файлам, а `<plan-sha256>` — результат
**последнего READY plan**, проверенный перед исполнением.

```text
vkm-corpus update plan --config <runtime.json> --campaign <campaign.json>
vkm-corpus update dry-run --config <runtime.json> --campaign <campaign.json>
vkm-corpus update status --config <runtime.json> --campaign <campaign.json>
vkm-corpus update execute --config <runtime.json> --campaign <campaign.json> --confirm-plan <plan-sha256>
vkm-corpus update resume --config <runtime.json> --campaign <campaign.json> --confirm-plan <plan-sha256>
vkm-corpus update rollback --config <runtime.json>
```

`plan`, `dry-run`, `status` не создают каталогов/receipts и не выполняют adapters. `execute` и `resume` используют
один цикл: уже завершённые stages переиспользуются только после проверки выходных хешей. `status.campaign=PASS`
и `status.generation=UNAVAILABLE` — возможная честная комбинация: shadow готов, serving не квалифицирован.
Флаг `--allow-gpu` необходим для будущего GPU executor, но сейчас такого adapter нет: GPU остаётся `BLOCKED`.

Поддерживаемые CPU adapters:

| Operation / options | Реальный результат |
|---|---|
| `VERIFY_ORIGINALS / {}` | Свежие SHA-256 и размеры ACTIVE оригиналов, проверка containment |
| `EXTRACT / {mode: native_prepare, context_artifact: alias}` | Существующий `prepare_source`, raw native artifacts и prep cache. Без OCR/layout inference. DOCX без pinned render даёт BLOCKED; render не запускается |
| `COVERAGE / {artifact: alias}` | Проверка сохранённого CoverageLedger: ACCOUNTABILITY_ONLY; denominator относится к указанной в ledger исходной кампании |
| `CANON_VALIDATE / {artifact: alias}` | Существующий canonical validator по точному manifest |
| `EVIDENCE_VALIDATE / {revision: sha256}` | Проверка точной версии journal и графа ссылок, INTEGRITY_ONLY |
| `EVIDENCE_PROJECT / {revision: sha256, context_artifact: alias}` | Новая локальная policy-filtered Parquet/DuckDB/JSONL projection; remote stores не загружаются |
| `BUILD_SHADOW / {kind: DUCKDB, artifact: alias}` | Canon validation → новый DuckDB → fingerprint comparison, без изменения CURRENT |
| `BUILD_SHADOW / {kind: NAV_PACK, artifact: alias}` | Только проверенный NAV manifest → копии объявленных Parquet в shadow → repack, без rebuild/embeddings |
| `BUILD_SHADOW / {kind: REGISTRY, context_artifact: alias, metadata_artifact: alias}` | Существующий registry importer в новом STAGING; подробности ниже |

Для EXTRACT требуется pinned `AccessContext`; policy каждого ACTIVE source проверяется **до чтения содержимого**.
Для registry это требование распространяется на все sources, поскольку legacy importer инспектирует каждую запись.
Разрешённый владельцем TARGET доступен при `allow_targets=true`, включая CLOUD для PRIVATE_CLOUD_ALLOWED.
Это не даёт права утверждать слепую независимую валидацию. PRIVATE_LOCAL_ONLY и SEALED остаются недоступными CLOUD
по policy; локальный CPU worker не повышает контекст CLOUD до LOCAL. Отсутствующая policy или явный запрет дают
BLOCKED. EVIDENCE projection применяет тот же переданный контекст и наследует ограничения записей.

Registry adapter допускается только при полном совпадении набора SOURCE_REGISTER и campaign inputs, включая
hash/path/size/lifecycle. `metadata_artifact` имеет вид `{"files":{"00_registry/...":"sha256",...}}` и перечисляет
все файлы `00_registry/`, в том числе work registry и intake manifests. Проверяются отсутствие лишних файлов и
точные bytes до и после import. Ambient `VKM_WORK_REGISTRY_DIR` не используется. Любые importer errors означают
BLOCKED. Receipt не содержит оригинального текста registry.

ACCEPT_SHADOW, SWITCH, VERIFY_SWITCH, remote graph/search, GPU и Windows desktop adapters **не зарегистрированы**.
`rollback` возвращает `BLOCKED / QUALIFIED_DEPLOYMENT_RESTORE_ADAPTER_REQUIRED`: смена metadata pointer не выдаётся
за rollback нескольких работающих stores. Остальные явно неподдерживаемые варианты также BLOCKED.

## Deadline, память, resume и восстановление

Worker запускается только фиксированным Python argv. В отдельном child wrapper устанавливается и проверяется
Linux RLIMIT_AS, затем выполняется `execv`. `preexec_fn` не используется. BLAS/OMP работают с одним потоком.
Это лимит виртуального адресного пространства процесса, не обещание суммарного cgroup memory limit для произвольных
внешних приложений. Неподдерживаемые platform/compute не исполняются. Coordinator проверяет available memory,
reserve и свободный диск; одновременно выполняется одна stage под эксклюзивной lease.

Таймаут уничтожает process group worker вместе с его дочерними процессами. stdout/stderr не копируются в delivery
receipts: там могут быть текст документов и credentials. Диагностика результата содержит статус, тип ошибки,
счётчики и хеши; подробные локальные native artifacts остаются в защищённом runtime дереве.

До работы сохраняется RUNNING intent с детерминированным `request_id = hash(campaign, stage, input, config)`.
Каждая попытка даёт receipt с SHA всех результатов, включая prep и native CAS blobs. Повтор завершённой стадии
без изменений не исполняет worker. Изменение/исчезновение результата инвалидирует стадию и downstream; такой
результат не перезаписывается автоматически — runtime возвращает BLOCKED. Для восстановления нужен осознанный
новый campaign/config либо отдельная проверка повреждения. Retry ограничен `attempts`.

Selector проверяется против прочитанного hashed receipt: статус, campaign, stage, request и номер попытки обязаны
совпадать. Adapter не может задавать owner fields receipt. RUNNING intent связан с той же кампанией и попыткой.
Intent и завершённый receipt фиксируют `dependency_receipts`: смена receipt upstream инвалидирует downstream
даже после сбоя между двумя стадиями. `INVALIDATED` вычисляется заново и не сохраняется как противоречащий receipt
статус. Старые receipts без этих привязок не переиспользуются автоматически.

После аварийного завершения координатора lease не удаляется автоматически: сначала доказать, что все процессы
старого владельца завершены, проверить RUNNING receipt и результаты. Затем отдельной операцией восстановить lease
и выполнить свежий plan/resume. CLI не предоставляет опасное «force unlock». Ошибка восстановления оставляет
serving закрытым; cleanup и удаление оригиналов в этом runtime отсутствуют.

## Code-only compatibility и старый NAV

```text
vkm-corpus update compatibility --config <runtime.json> --document-artifact document --nav-artifact nav --packed-artifact packed-nav --required-dataset sections
```

Все имена после `*-artifact` — ключи pinned `artifacts`. Дополнительно `--late-artifact late
--encoder-artifact query-encoder` проверяет late pack. Query encoder должен явно перечислять model_id,
model_revision, weights_sha256, quantization, tokenizer_sha256, heads_sha256, dimension; отсутствие поля не
выдаётся за совместимость. Включая пустые предусмотренные контрактом values, сравниваются все эти поля.

NAV проверяется потоковым SHA-256 и Parquet footer, без загрузки всех таблиц в RAM. Полный набор из 33 datasets
поддерживается; он не зашит обязательным: требуемые capabilities задаются явно. Старый manifest без доказанного
snapshot+canonical manifest hash даёт `LEGACY_ORIGIN_ATTESTATION_REQUIRED`, **не рекомендацию rebuild**. Сначала
оператор доказывает происхождение из receipts и неизменных Parquet, создаёт новый versioned manifest в shadow,
потом repack только если `nav_meta` не содержит совпадающей identity. Совпадающие raw/dense/late/visual данные
из-за одного code update не пересчитываются. Для dense/visual пока нет общего compatibility adapter: обязательный
native loader/acceptance остаётся отдельным gate, а не автоматически заимствованным PASS late pack.

Изменение extraction generation, правил chunking, encoder/tokenizer, научных входов или access class требует
dependency diff. Проверка code-only compatibility не отвечает вместо него и не заменяет full MCP acceptance.

## Native observers и production switch

`observations` содержит component, native_manifest, policy_binding, required и для DUCKDB/NAV runtime_database.
Policy binding должен иметь `status=PASS`, `component_manifest_sha256`, `policy_sha256`; для EVIDENCE дополнительно
точный `built_from`. Это операторское свидетельство binding, не научный review.

Observer читает actual canonical manifest, readonly `meta.snapshot` DUCKDB и `nav_meta` NAV; сравнивает IDs и
manifest SHA, а не возвращает ожидаемый GenerationManifest. Для DENSE/LATE/VISUAL читает pack manifest identity,
для EVIDENCE — commit/projection revision. Проверка существования manifest не доказывает, что процесс открыл именно
его: интегратор обязан брать пути из **фактических service dependencies** и проверить native loader acceptance.
Graph/search observers здесь отсутствуют; required components без adapter не получают READY.

`UpdateRuntime.require_startup()` запрещает запуск при отсутствии READY generation. Callable
`runtime.generation_status` подключается к `ApiDeps.generation_guard`; перед запуском дополнительно вызвать
require_startup. Это wiring должно выполняться deployment entry point с реальными paths; само наличие RuntimeConfig
не меняет API. Для production режима отсутствие wiring — блокер deployment.

Production receiver требует `vkm-serving-acceptance/1` со scope `SHADOW_PRODUCTION`, совпадающими code tree,
dependency identity, component identities, source policy, полным read MCP toolset и результатами serving checks.
Поле `access_config_sha256` — `serving_access_identity(api_config)`: contexts, execution location, разрешённые
классы/targets и read/write principal roles. Bearer bytes туда не входят; обычная ротация секрета при тех же
правах не требует переквалификации. Generic PASS и synthetic qualification этот receipt не заменяют.

`duckdb_file_sha256` в acceptance — SHA-256 **фактического файла**, выбранного `deps.canon.path`, после завершения
shadow build и checkpoint. При bind receiver проверяет, что это обычный файл без symlink (включая родителей),
один раз вычисляет полный streaming SHA и сравнивает с acceptance. До и после hash сохраняются/сравниваются
`device, inode, size, mtime_ns, ctime_ns`. Та же signature проверяется по обе стороны каждого observer и после
startup observer перед установкой guard. Порча данных при прежней `meta.snapshot`, изменение или атомарная замена
файла закрывают admission; замена идентичными байтами тоже требует rebind. Memory-only CanonStore не допускается
в qualified production. Полный hash базы на каждый HTTP request не выполняется.

Это контракт operator-owned **immutable serving artifact**: после bind никто не записывает в файл, а согласованный
switch сначала останавливает/drains readers и затем делает новый bind. Он обнаруживает операционные изменения
через filesystem identity; он не заявляет защиту от root-adversary, подменяющего метаданные/память, и не заменяет
drain/maintenance между admission и исполнением запроса. Qualified production receiver **разрешён только на Linux**:
bind проверяет OS до доступа к артефактам. В Windows `st_ctime` означает время создания, поэтому rewrite того же
размера с восстановленным mtime может сохранить signature. Поддержка Windows требует отдельной реализации и
qualification native ChangeTime и открытых handles с проверками reparse points (включая junction родителей).
Другие OS также отклоняются; проверка `Linux` не квалифицирует произвольную network filesystem — для serving нужен
operator-owned immutable файл на filesystem с проверенной семантикой Linux change time. Тесты этого receiver на
Windows имеют явный `NOT_RUN` и не считаются PASS; Linux выполняет все проверки, включая фактическую порчу байта
при прежних size/mtime. Explicit `compatibility` сохраняет прежних readers на Windows и не получает статус
production qualification автоматически. Ограничение receiver не меняет доступность Windows producer-инструментов.

Будущий qualified deployment adapter использует `GenerationCoordinator.switch`: drain → MAINTENANCE → apply
actual selectors → native observations → acceptance → atomic CURRENT → повторная проверка. Restore обязан вернуть
все selectors и проверить прежнюю generation. Неудачный restore сохраняет MAINTENANCE. Production изменения по
этому runbook пока не выполнялись и без отдельной квалификации adapters не автоматизируются.

До apply проверяются предыдущий CURRENT, hash/coherence его GenerationManifest и observed identity. Пустой,
повреждённый или не совпадающий с реально наблюдаемыми компонентами previous selector оставляет MAINTENANCE,
не вызывает apply/restore и не записывает candidate manifest: неизвестная старая generation не служит rollback target.

## Ограничение DOCUMENT wipe после появления EVIDENCE

`graph rebuild --mode wipe`, включая `--cascade`, сначала выполняет read-only preflight. Наличие научного слоя,
Claim/Observation/ReviewDecision либо evidence metadata блокирует команду **до DDL, изменения ProjectionRun и
удаления NAV** с `E_EVIDENCE_DEPENDENCY`. Даже запись без materialised support edge может хранить source locator
в свойствах; отсутствие рёбер не доказывает независимость. Проверка намеренно консервативна для всего namespace.
Чужие рёбра между двумя DOCUMENT nodes также блокируются (`E_CROSS_LAYER_LOSS`). Прямые helpers wipe/test purge
проверяют то же условие; `--cascade` разрешает только зарегистрированные зависимости производного NAV.

В этой реализации **нет** remote adapter, который квалифицированно заменяет DOCUMENT вместе с EVIDENCE.
Нельзя снимать блокировку удалением scientific nodes/рёбер или считать частичный DETACH DELETE безопасным.
Нужен отдельный shadow-generation rehearsal с сохранением всех source support references, acceptance и rollback.
Preflight требует эксклюзивного управления graph writers: он не является распределённой блокировкой и не
доказывает безопасность concurrent writer между проверкой и транзакциями wipe. Production Neo4j в этой работе
не изменялся; fake-driver regression подтверждает отказ до первой записи, а не live Cypher acceptance.

## Синтетическая проверка

`tests/corpus/test_update_runtime.py` проверяет CLI без записи, свежий original hash при сохранённых size/mtime,
worker receipts/resume, policy gate, запрет произвольной команды/GPU/switch, извлечение сгенерированного PDF,
выходные CAS/cache hashes, повреждение результата, реальный deadline с дочерним процессом, shadow DuckDB без
изменения original CURRENT, native DUCKDB/NAV observers и legacy NAV из 33 синтетических Parquet.
`tests/evidence/test_update_cycle.py` покрывает DAG, bounded retry, generation switch и failed rollback.
Эти тесты не являются production qualification receipt и не запускают workload реального корпуса.

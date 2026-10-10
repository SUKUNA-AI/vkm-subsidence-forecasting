# Публикация и восстановление accounting

Контракт `vkm-accounting-publication/1` связывает существующий producer,
`vkm-corpus core publish`, CORE admission/snapshot и manifest backup. Новый сервис
или отдельный serving component не требуется. Реальная публикация и restore на
CORE/EDGE этим изменением не выполнены.

## Замораживание и доверие

`PublicationRequest` задаёт campaign SHA, SHA текущего source-policy inventory,
явный `max_total_bytes`, base snapshot (ID, manifest SHA, source heads, registry
head), конечные версии источников, точные commit markers и accounting bindings.
Изменённый источник обязан иметь binding; исторический источник без него допустим
только при неизменном commit из base. Удаление base source из набора запрещено.
Один переход источника/registry идёт от base head к новому head. Непереданная
промежуточная история требует отдельного согласованного перехода, не угадывается.

`freeze_publication` сначала проверяет policy и operator `AccessContext`, затем
собирает конечное замыкание: markers/partitions, report, перечисленные события,
raw blobs и относящиеся к выбранным runs журналы. Общие registry/run journals
требуют доступа ко всему source-policy inventory; узкая агентная роль не получает
прав публикации общих каталогов. Пути относительные, links запрещены, каждый файл
имеет размер и SHA; лимиты — 10 000 sources, 200 000 files, 64 MiB descriptor и
утверждённый byte budget. JSON sidecar проверяется до 128 MiB.

Перед хешированием и доставкой Parquet читаются только колонки атрибуции:
source IDs, anchor/from/to/registered source IDs и page IDs. Каждый фактический
источник должен присутствовать в current policy и разрешаться context; одного
перечисления известных policy keys недостаточно. Тексты, values и сообщения
не материализуются этой проверкой. Неизвестная атрибуция блокирует публикацию.

Descriptor сохраняется неизменяемо как `accounting/publications/<SHA>.json`.
Он не предоставляет разрешения сам себе. Отдельный **operator-owned**
`PublicationApproval` закрепляет descriptor SHA, campaign SHA, policy SHA,
source IDs и context. Caller/CLI должен получить этот файл из доверенной
операторской конфигурации; pin файла передаётся отдельно. Политику перечитывают
перед проверками и каждым шагом транспорта. Ни перенос, ни admission не означают
семантический review или scientific admission.

## CLI

Команды принимают локальные operator paths; они не встраиваются в descriptor:

```text
vkm-corpus core publication-freeze --staging STAGING --request REQUEST.json --request-sha256 SHA --context CONTEXT.json --context-sha256 SHA --policy POLICY.json
vkm-corpus core publish --staging STAGING --target CORE:ROOT --approval APPROVAL.json --approval-sha256 SHA --policy POLICY.json --dry-run
vkm-corpus core publish --staging STAGING --target CORE:ROOT --approval APPROVAL.json --approval-sha256 SHA --policy POLICY.json
vkm-corpus core reconcile --approval APPROVAL.json --approval-sha256 SHA --policy POLICY.json
vkm-corpus core accounting-verify --root RESTORED --approval APPROVAL.json --approval-sha256 SHA --policy POLICY.json
```

Те же три operator flags поддерживаются `canon admit`, `canon snapshot`,
`canon validate`. Pending admission возвращает ненулевой exit code. Без approval
новые accounted snapshots не проходят validate; исторический PASS отдельно
показывает `accounting=NOT_AVAILABLE`.

Существующие update adapters `CANON_VALIDATE` и `BUILD_SHADOW(kind=DUCKDB)`
используют тот же gate. `RuntimeConfig.publication_approval` — отдельный operator
BoundFile; его SHA и текущая policy перепроверяются до и после validation/build.
Approval не допускается внутри incoming canonical root или runtime write root.
Descriptor закрепляет producer campaign, поэтому последующая code-only validation
может иметь другой campaign manifest без изменения оригиналов и embeddings.

`publication-freeze` не передаёт данные; publish не выполняет admission. Remote
publish receipt имеет `receiving_status=NOT_RUN`, пока CORE не выполнит свою
проверку. `--dry-run` не передаёт файлы; локальный технический files-from list
может появиться в `accounting/tmp/`. Отклонённые inputs не печатаются в CLI.

## Доставка и receiving gate

Передаются только файлы frozen descriptor в порядке: content → run markers →
commit markers → publication marker. Root marker, cache, leases, admission,
CURRENT не копируются из staging. Из snapshots передаётся только SHA-pinned
immutable base manifest, если он явно назван в request. Новый event после freeze не
попадает в замороженный receipt молча. Если frozen run получил новый journal
part, проверка требует нового descriptor. Существующий target файл rsync не
переписывает: несовпадение bytes обнаружит receiver; автоматического repair или
cleanup нет. Ошибка remote `sync` — ошибка публикации, не успешный receipt.

Producer теперь ставит `accounting_required=true` в новые source commits.
Поэтому отсутствие целого accounting дерева нельзя принять за старый формат.
Переход accounted chain обратно в legacy writer запрещён; no-op учитывает этот
признак. Final binding не включён обратно в marker: это создало бы hash cycle.
Receiver независимо проверяет всю цепочку ancestors (не более 4096 markers),
даже если недопустимый descendant уже был ADMITTED прежней версией кода.
Удаление accounting flag из нового self-hashed child не возвращает его в legacy.
Missing ancestor остаётся незавершённой доставкой, а не разрешением на downgrade.

CORE получает approval от оператора, проверяет fresh bytes, base snapshot,
report/ledger, все events/blobs, соответствие canonical object IDs/content SHA
и нужные partitions. Missing/invalid delivery остаётся **PENDING**: permanent
REJECTED не записывается только из-за не приехавшего sidecar. До успешной проверки
новые required commits не допускаются. Другие незаявленные commits не выбираются.

Snapshot inputs содержат descriptor ref до вычисления snapshot ID. Validator A00
проверяет source heads, markers и точные maps document/registry partition files;
каждый journal/artifact partition snapshot также должен совпасть с FileRef
descriptor. Посторонние локальные runs не наследуются без approval.
одинаковых названий heads недостаточно. Без approval required snapshot блокируется
до чтения partitions. Повтор после потерянного ACK заново проверяет closure и
сохраняет существующий snapshot ID.
Перед no-op проверяется точное совпадение existing CURRENT maps, inputs, counts
и accounting с заново проверенным candidate. Повреждённый старый manifest не
получает PASS только из-за совпадения source heads; автоматического repair нет.
Исторический snapshot без accounting имеет **NOT_AVAILABLE**, а не VERIFIED.
`ACCOUNTED` по-прежнему не означает полноту
извлечения или научную готовность.

Поддерживаемый режим этого контракта — полный STAGING mirror с неизменяемыми
файлами baseline. Это не требует повторного extraction или embeddings:
неизменённые bytes повторно проверяются и переиспользуются. Pinned base journals
и markers ancestors входят в descriptor. Sparse staging без baseline пока
блокируется; для него нужен отдельный receiving base-proof contract.

## Backup и восстановление

Оба существующих backup пути включают `accounting/`, кроме `accounting/tmp/`.
Sidecars immutable: другой mtime не оправдывает изменение SHA. Перед выдачей
backup manifest стандартная библиотека проверяет CURRENT → snapshot → descriptor
→ bindings/reports/events/raw references против file manifest. Неполное required
замыкание не получает новый backup manifest. На EDGE не нужны corpus dependencies.

Restore заново хеширует bytes и проверяет эти связи. `full_accounting_recovery`
возможен только для полного набора с VERIFIED accounting. Это не доказательство
восстановления всех внешних сервисов или выдерживания физического power loss.
После переноса на CORE `core accounting-verify` с действующей operator policy
повторяет deep Parquet checks. Восстановленный subset canonical без обязательных
sidecars не считается полноценным восстановлением. Исторические backups имеют
NOT_AVAILABLE; они не получают новых доказательств задним числом.

До runtime qualification остаются: реальные approved campaign/approval и policy,
доставка обновлённого backup tool на CORE/EDGE, synthetic restore на целевой FS
и отдельный power-loss drill. Неопубликованные events прерванных workstation runs
ещё не находятся на CORE: их `accounting/events/` и связанные raw artifacts
необходимо включать в existing unique-recovery inventory workstation отдельно.

## Проверки

`tests/corpus/test_accounting_publication.py` использует настоящий synthetic
prepare/assembler/mapper/commit и receiving validator, mock OCR без сети/GPU.
Есть actual local rsync test; без установленного rsync он явно NOT_RUN. Остальные
transport fault cases работают на обеих ОС через files-from mock и не требуют SSH.
Проверяются missing tree/sidecars/blobs, interruptions, policy/pin/source mismatch,
same-name corrupt target, path/links, byte bounds, legacy compatibility, snapshot
head/partition substitution, legacy downgrade и lost ACK.

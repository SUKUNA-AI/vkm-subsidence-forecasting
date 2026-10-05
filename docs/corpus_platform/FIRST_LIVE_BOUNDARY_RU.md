# Первый LIVE: отдельная граница legacy → квалифицированные receivers

Статус: **FIRST_LIVE_NOT_READY**. Утверждение v3 о сохранности original containers
**REFUTED (P0)**: `docker rename` сохраняет Compose project/service labels, поэтому
последующее same-project `compose create` может удалить parked originals вместе
с writable layers. Удаление `--force-recreate` не решает config/image convergence.
Native Compose-create удалён. Production controller и native adapter блокируются
до создания клиентов/потоков/каталогов, firewall и Docker effects; отдельные native
container mutators также блокируют production. Это исправление блокирует опасный
путь, но не реализует готовый безопасный switch. Синтетические fake-adapter tests
проверяют только механику состояний. V3 CPU receipts не доказывают сохранность runtime.

Gate L0 (код и синтетические тесты) реализован: fixed Engine-create adapter
`engine_create.py` (см. раздел «Fixed Engine-create (Gate L0)»). Gate L1 —
фактическая изолированная квалификация Engine lifecycle — **NOT_RUN**. До неё
`NativeFirstLiveAdapters` по-прежнему не конструируется, и все production CLI
actions возвращают `FIRST_LIVE_NOT_READY` с exit 2; restore тоже не предлагается
как выполненный или квалифицированный fallback. Existing deployment не изменён.

Далее описан проектный contract, не доступный production-переход. Фактические
firewall, boot, Docker, private49 и CORE/EDGE qualification **NOT_RUN**.
Этот документ не разрешает перезагрузку CORE и не является receipt выполненного switch.

## Область и доверие

`first_live.py`, `first_live_native.py`, `frontdoor.py` реализуют отдельный одноразовый
переход. Обычные bootstrap/deployment/serving acceptance не ослабляются.
Кандидат сохраняет исходные `GenerationManifest G` и shadow acceptance `R`.
Отдельная `LiveAdmissionAuthority` разрешает впервые записать LIVE `CURRENT`.
Shadow `CURRENT`, CLOSED owner и deployment profile не копируются в LIVE.

Проектируемый fallback имеет статус `LEGACY_RESTORED_UNQUALIFIED_CLOSED`. Это возврат точных
исходных API, read-MCP и, если он существовал в legacy, admin-MCP containers.
Под CLOSED они останавливаются и получают детерминированные retained names;
исходные container IDs и writable layers обязаны сохраняться; v3 этого не обеспечивал.
После удаления только
проверенных candidate containers исходным возвращаются прежние names и start.
Missing original container блокирует fallback: свежий compose не подменяет его
уникальный writable layer. Оно не становится qualified previous и не получает
scientific admission. Общие stores, selectors, модели, query owners и сетевые
объекты этот контроллер не обновляет и не восстанавливает. Любое их изменение
за пределами закреплённых identities блокирует переход. Их обновление требует
собственной coherent closure до подготовки first-LIVE intent.

Оператор — доверенная сторона, которая одобряет неизменяемые входы. JSON с `PASS`
от произвольного producer не доказывает независимый backup. `LegacyRecoveryApproval`
является отдельной capability владельца после проверки конкретного выполненного
opaque recovery packet: frozen executor/capture/procedure, COPY и второй SSH-сеанс
с authenticated age decrypt/restore, native machine/host-key/domain pins, исходные
bytes без изменений. Consumer проверяет точное совпадение всей этой closure и
свежие восстановленные control bytes. Он не выводит физическую независимость
удалённых машин из произвольных строк. Корпусные R1/R2 и старые aggregate receipts
не подходят вместо этого пакета. Секретные Env/Labels/конфиги остаются внутри
зашифрованного пакета; PUBLIC хранит только безопасные агрегаты.

## Неизменяемые входы

Все live control-входы принадлежат отдельному `authority_root`. `control_root`,
`qualification_root`, originals/data roots не пересекаются. Intent закрепляет:

1. Исходные shadow operator/plan/R/G и полный именованный граф control-документов.
2. LIVE environment/runtime/native/policy/context, Compose и полные native effective
   configurations двух receivers. Opaque credential bytes сохраняют SHA.
3. Явные разрешённые scalar mappings: project, authority/control/qualification root.
   Семантические настройки вне этих точных mappings изменяться не могут.
4. Typed public bindings плюс loopback metadata endpoints. Compose и native
   PortBindings проверяются целиком; wildcard новых LIVE bindings запрещён.
5. Legacy receiver IDs, image IDs/config hashes, selectors и independently restored
   control closure. Независимый control backup не означает backup writable layers;
   для потери всего host нужна отдельная процедура восстановления этих слоёв.
   У каждого selector есть точная typed-связь исходного path+SHA
   с конкретным independently restored path+SHA. Оригинал проверяется отдельно;
   одинаковый SHA на другом незарегистрированном пути не заменяет эту связь.
6. Retained read-only observer environment/runtime/native profile, actual DuckDB/NAV
   byte pins и I/O budget, полный tuple компонентов и сервисов. Observer создаёт
   реальные clients из восстановленного control и остаётся работоспособен без
   candidate G/R. Native file leases и actual query connections проверяются.
7. Существующие external Docker bridge network IDs/config hashes и точные host
   bridge interface names. Создание/удаление сетей, volumes, images запрещено.
8. Отдельные operator commit/code/dependencies, pinned nft/ip/tc/systemctl/Python,
   immutable host seal profile и boot registration/checkpoint.

CORE operator, API и MCP должны использовать одну новую clean software revision
после freeze этого расширения и повторную shadow49. Нельзя запускать новый
in-process operator с прежним API wheel и ослаблять all-package code identity.
EDGE owner может иметь собственную независимо квалифицированную revision,
зафиксированную его ServiceIdentity. Retired CareerOps text не включается:
optional visual-only gateway допустим только через уже существующий strict
actual-ApiService late fallback contract.

## Внешнее закрытие

Fixed nft inet table содержит source-owned JSON rules без shell/rules из manifest.
Охвачены IPv4/IPv6, TCP sport/dport до DNAT и исходящие ответы существующих flows.
При forward проверяются оба интерфейса. Разрешены loopback и лишь actual pinned
Docker bridges для внутреннего MCP→API. Внешний reply через физический интерфейс
остаётся закрыт. XDP, tc filters, flow offload, неизвестные qdisc/hook/route paths,
физический интерфейс в доверенном bridge и неполный port inventory блокируются.
Это gate пакетов в указанных hooks; уже переданные байты не отзываются.

Привилегированный runner должен иметь CAP_NET_ADMIN. Код не настраивает sudoers,
sysctl или privileges. Native executable открыт `O_NOFOLLOW`, хешируется через
удерживаемый inode и исполняется через `/proc/self/fd` с watch, bounded stdout,
process-group timeout и child resource limits. Предположение — operator-owned
immutable executable/OS runtime; защита от привилегированного RAM/kernel adversary
не заявляется. Docker/native firewall совместимость требует отдельного реального drill.

Boot renderer создаёт только fixed oneshot seal unit и `Docker.service`
`Requires/After` drop-in. `Before=docker.socket` запрещён: при стандартных systemd
dependencies он создаёт cycle через sockets.target/basic.target. Наблюдатель
сверяет точные unit/drop-in bytes, фактически загруженный argv, success/exit,
отсутствие лишних drop-ins, текущий boot ID и порядок monotonic timestamps.
Наличие таблицы само по себе ничего не квалифицирует. До отдельного реального
boot/ordering drill переход имеет `BLOCKED_BY_ACTUAL_BOOT_QUALIFICATION`.
Текущий код не устанавливает unit и не перезагружает host. Полная host reboot
затрагивает другие сервисы и требует отдельного решения владельца. Поле
`reboot_safety=NOT_QUALIFIED` не превращается в PASS от CPU-тестов или status.

## Фазы и восстановление

CLI: `python -m vkm_corpus.update.first_live_native ACTION --authority FILE
--sha256 SHA --confirm INTENT_SHA`. `ACTION` — `rehearse`, `activate`, `restore`,
`resume`, `status`. Это будущие operator-команды, не выполненные действия.

`rehearse` сначала требует пустой новый control root и exact legacy topology.
Под writer lease закрываются host ingress, durable admission и kernel gate.
MAINTENANCE удаляется под CLOSED, чтобы нормальный startup мог прочитать G.
Исходные legacy containers сохраняются stopped под retained names; их удаление
не входит в контроллер и потребует отдельной задачи после квалифицированной
recovery closure. Кандидат создаётся без build/pull в stopped state. Полный native config каждого
контейнера проверяется до start. Запускается только API; реальные Running API и
stopped MCP проверяются повторно. Затем исходные legacy IDs восстанавливаются
под прежними names и запускаются под закрытым seal.
Свежие retained observer checks подтверждают неизменность shared state.
Только после этого записывается отдельный fallback-drill event.

`activate` требует этот drill, создаёт оба receivers, получает подписанный
HTTP↔native-process join и выполняет все 49 private READ probes с реальным
policy/native/generation contract. Missing/denied/wrong identity не становится PASS.
После повторного того же процесса записывается обычный `RECEIVERS_VERIFIED` event,
затем durable OPEN. Внешний seal всё ещё CLOSED. Лишь после снятия EX flock и
получения actual OPEN receiver proof можно открыть host seal. Отдельный activation
event/result завершает переход. Receipt не является научной проверкой.

При обычной ошибке ingress закрывается и выполняется fallback. После process death
kernel seal сохраняется; OPEN admission без exact verified event не принимается.
`restore` использует retained authority и control bytes даже при missing/corrupt
candidate artifacts; результат остаётся CLOSED. При shared-state drift успешный
rollback receipt не создаётся. `resume` после lost ACK допускается только для того
же живого native receiver, того же durable OPEN/event и полных raw49 receipts.
Повторный restart не заменяет исходную qualification: resume закрывает host и
admission и требует explicit recovery. Терминальный восстановленный legacy не
перезапускается повторно, если его exact native instance уже подтверждён.

## Fixed Engine-create (Gate L0)

`engine_create.py` создаёт кандидатов без Compose. Реализовано и проверено только
кодом и синтетическим fake Engine transport (Windows и Linux); реальный Docker не
вызывался. Активация по-прежнему невозможна (см. code gaps ниже).

* Транспорт: unix socket `units.docker_socket` intent (тот же путь, что `DOCKER_HOST`
  фиксированного legacy CLI) и закрытый allowlist (method, path-template), собранный
  кодом: `GET version`, `GET info`, `GET images/{sha256-id}/json`, `GET networks/{id}`,
  `POST containers/create?name=vkm-core-{api|mcp}-1`, `GET containers/{id|owned name}/json`,
  `POST containers/{id}/start|stop`, `DELETE containers/{id}?force=false&v=false`.
  Параметры пути строго проверяются; pull/build/exec/rename/kill и generic executor
  отсутствуют; без redirects и proxy; ограничены размер ответа, время каждого чтения
  и общий monotonic deadline запроса. Start и stop — по точному journal-owned ID:
  start только после повторной проверки daemon, image, network и contract; stop нужен,
  потому что remove без force не удаляет running/restarting container.
* Daemon pin в плане: daemon ID, точная версия Engine и `Components`, `DefaultRuntime`,
  `SecurityOptions`, cgroup driver/version, `DockerRootDir`. Значения закрепляются по
  результатам L1. Legacy CLI (`docker info`) и Engine adapter обязаны видеть тот же ID.
  Привязка adapter к intent, проверки daemon, image, network и bind sources выполняются
  до остановки и переименования originals. Если journal пуст (write-ahead: ни одного
  Engine create не запрашивалось), legacy restore не требует daemon pin: выполняется
  только read-only проверка, что на owned names нет кандидата с labels этого journal,
  а guard исходного имени по-прежнему не вытесняет неизвестный контейнер. При наличии
  записей в journal rollback требует точный pinned daemon.
* `EngineCreatePlan`/`EngineCreateProfile` (поле `engine_create` в intent) — точное
  одобренное подмножество Config/HostConfig/NetworkingConfig. Image — только pinned
  `sha256:` ID; network — существующая pinned external network; host IP явные;
  user — числовой не-root `uid:gid`; privileged запрещён; `cap_add` и `security_opt`
  только из проверенных allowlist (`no-new-privileges[:=true]`, `apparmor=docker-default`);
  restart policy только `no`; `com.docker.compose.*` labels
  запрещены, поэтому Compose не может adopt кандидата. Bind mounts не могут указывать
  на Engine socket (или каталог с ним), `/`, `/proc`, `/sys`, `/dev`, Docker data root,
  authority/qualification/protected roots; запись в control root разрешена только
  для `admission.lock`. Перед create и start bind source проверяется natively: существует,
  без symlink-компонентов, на POSIX `realpath == source`, не socket/device/FIFO, а
  разрешённый путь не попадает в запрещённые деревья. Intent связывает profile с candidate image pin, pinned network,
  точным typed ingress inventory и control/gate mounts. Production intent без плана
  невалиден.
* Env-секреты не входят в profile, intent, journal, receipts и exceptions: profile
  хранит только имена Env и `BoundFile` (path+SHA) файла в authority root. Для API это
  `release.environment`, для read MCP — только документ live control graph с именем
  `mcp-engine-environment` (сравнивается с shadow), отличный от environment API. Значения читаются с проверкой SHA только при create;
  contract сравнивает их SHA-256. `FIRST_LIVE_INTENT.json` и `FIRST_LIVE_RESULT`
  пишутся с правами 0600, journal — 0600 в каталоге 0700.
* Тело create строится только из profile и разрешённых Env. После create native
  inspect сравнивается с нормализованным contract; inspect `Mounts` сравниваются как
  множество (порядок из Go map недетерминирован). Унаследованные от pinned image
  Env/Labels/ports/Healthcheck допускаются только равными значениям image. Закреплены
  hardened значения: runtime, `IpcMode`/`CgroupnsMode=private`, cgroup parent,
  `MaskedPaths`/`ReadonlyPaths` (не слабее defaults), devices/device requests/cgroup rules,
  `GroupAdd`, ulimits, OOM, DNS, sysctls, AppArmor profile, `LogConfig`, endpoint
  IPAM/links/driver options, TTY/stdin.
* Write-ahead hash-chained JSONL journal (`<control_root>/first-live-engine/<intent>.jsonl`,
  O_EXCL, O_APPEND, проверка inode, fsync файла и каталога): `CREATE_INTENT` (attempt,
  owned name, profile SHA, preserved original IDs, deadline) до запроса, затем
  `CREATE_ACK`/`ADOPTED`/`PARTIAL`/`ABSENT`, `OCCUPANT_OWNED`, `POST_CREATE_VERIFIED`,
  `START_INTENT`/`STARTED`, `STOP_*`, `REMOVE_INTENT`/`REMOVED`. Deadlines в journal —
  wall-clock (нужны между процессами); ожидание внутри процесса — monotonic.
* Lost ACK/409/ошибка daemon: повторный create не делается вслепую; inspect точного
  owned name. 409 без видимого occupant остаётся pending. Контейнер на owned name
  принадлежит этой попытке, только если его labels (attempt, unit, profile SHA)
  соответствуют записанному `CREATE_INTENT` и он создан не раньше него; точный
  pending — adopt, иначе он owned только для удаления (никогда не start). Чужой
  контейнер — отказ без cleanup. Возобновление после crash — по journal.
* Rollback удаляет только journal-owned IDs с labels этой попытки (в том числе поздно
  появившиеся occupants записанных intents); preserved original IDs и renamed originals
  со старыми Compose labels не выбираются и не удаляются. Attempt не закрывается, пока
  остаются неразрешённые intents или occupants с labels этого journal. Fallback не
  вытесняет неизвестный контейнер с исходного имени. Потерянный journal при живом
  кандидате с его labels — отказ, а не «ничего не создано».
* Атрибуция по `Created` использует часы daemon и controller одного host с допуском
  1 s. Если wall clock был переведён назад (или daemon отстаёт), собственный поздний
  кандидат становится неатрибутируемым: rollback завершается `EngineCreateAmbiguous`,
  attempt не закрывается, контейнер не удаляется и originals остаются parked. Процедура
  оператора: сохранить journal, сравнить `Created` контейнера с `recorded_unix`
  соответствующего `CREATE_INTENT`, проверить labels (attempt, unit, profile SHA), image
  и отсутствие Compose labels, зафиксировать отдельное решение и только затем удалить
  этот единственный контейнер вручную по точному ID; после этого повторить restore.
  Автоматического расширения допуска нет; величину skew измеряет L1.
* Закрытые семейства имён: `EngineCreatePlan.name_family` — `PRODUCTION`
  (`vkm-core-{unit}-1`, по умолчанию) или `QUALIFICATION` (`vkm-l1q-{unit}-1`). Имена
  profiles обязаны принадлежать семейству плана; transport и adapter строят regex owned
  name (create, inspect, sweeps) только из этого семейства, поэтому QUALIFICATION adapter
  не может адресовать `vkm-core-*`, и наоборот. `FirstLiveIntent` принимает только
  `PRODUCTION`. `QUALIFICATION` нужен лишь для изолированного L1 на CORE рядом с
  работающими production receivers; его результат не является квалификацией самого
  first-LIVE switch и не снимает code gaps ниже.
* Torn/повреждённый journal никогда не чинится автоматически: оператор сохраняет
  копию файла, сверяет последнюю целую запись с native inspect owned names и journal
  IDs и принимает отдельное решение; удалять или обрезать journal нельзя.

## Оставшиеся фактические gates

* Gate L1 (NOT_RUN): фактическая изолированная квалификация Engine lifecycle
  (create/inspect/start/stop/remove, lost ACK, late create, сохранение original IDs и
  layers) на реальном daemon заданной версии; закрепление daemon pin и проверка
  нормализации inspect (MaskedPaths/ReadonlyPaths, NetworkID до start, CAP_-имена,
  SecurityOpt, default PATH, Created). Peer credentials socket не проверяются.
  До L1 production constructor заблокирован.
* Code gaps после L0 (активация невозможна): candidate proof
  (`CoreUnitControl._inspect`/`_joined_proof`, `container_fingerprint`) всё ещё требует
  Compose labels и несовместим с Engine-created receivers; profile не сверяется с shadow
  effective config сверх image/network/ingress/mounts/environment file; обычный Compose
  `rebind` после первого LIVE нашёл бы parked originals по labels, поэтому он
  недопустим, пока originals существуют.
* Approved actual opaque legacy control copy + independent restore, затем отдельная
  owner approval. Это не backup всего корпуса или модели.
* Реальное квалифицированное native hosted profile всех shared components/services,
  включая query encoder owners. Их device/model memory требования указываются
  отдельно; model load не маскируется под CPU metadata test.
* Новый unified CORE wheel/images и новая реальная isolated shadow49 на них.
* Privileged fixed firewall и boot-ordering qualification после отдельного host
  решения. Сначала concrete packet; никакой автоматической reboot.
* Реальные partial-start/fallback и full49 перед первым LIVE OPEN.

Синтетические tests не доказывают Docker, nft kernel, reboot, independent failure
domain, model placement или scientific validity. Реальные sources/models в них
не читаются и не загружаются.

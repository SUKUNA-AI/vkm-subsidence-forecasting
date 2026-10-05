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

Для продолжения нужен отдельный reviewed fixed Engine-create adapter: exact
approved Config/HostConfig/NetworkingConfig, pinned images и external networks,
native post-inspect до start, запрет любых remove/recreate original IDs, negative
тесты label discovery/config drift/partial create. Никакого произвольного Engine
API или manifest shell. До его реализации и квалификации все production CLI
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

## Оставшиеся фактические gates

* Code gate: безопасный native candidate-create, сохраняющий original IDs и layers.
  Он отсутствует; это не просто отложенная runtime qualification.
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

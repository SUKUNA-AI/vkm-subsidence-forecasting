# Следующая реализация и квалификация production data

Основание: [статус S01–S26](PRODUCTION_DATA_PROGRAM_2026-10-01_RU.md) и
[карточки R01–R09](PRODUCTION_DATA_REMAINING_TASKS_RU.md). Это конкретизация
подтверждённых остаточных пробелов. Наличие draft PR не закрывает программу.

## R03-PUB / R03-ADMIT / R03-BACKUP — доставка полного accounting

**Статус:** код и synthetic qualification реализованы; реальные доставка,
restore и target filesystem qualification NOT_RUN. Runtime validation/shadow
используют отдельный operator-owned `publication_approval` BoundFile.

**Причина изменения:** accounting был связан только с локальными canonical partitions;
publication/admission/backup не сохраняли его полное замыкание.
Artifact blobs не заменяют отдельные reports/events/final bindings.

**Файлы:** `src/vkm_corpus/coverage/accounting.py`, `publish/transfer.py`,
`publish/cli.py`, `publish/reconcile.py`, `parquet/admit.py`, `parquet/snapshot.py`,
`parquet/validator.py`, `infra/edge/backup/vkm_manifest.py`, существующие transfer
и backup tests. Ввести небольшой publication contract рядом с accounting.

**Изменения:** immutable publication descriptor, закреплённый SHA approved campaign.
На источник: source SHA, commit ID/marker SHA, exact accounting binding и полное
замыкание report → events → raw blobs. Каждый file имеет relative path, size, SHA.
Descriptor фиксирует base snapshot, expected source heads и policy SHA. Source
policy проверяется до чтения sidecars и передачи. Не связывать final receipt
обратно из commit marker: receipt уже хеширует marker, образуется цикл.

Порядок: freeze/verify → artifacts/partitions/accounting → run markers → commit
markers → publication descriptor → fresh receiving closure verification → admit
→ snapshot. Descriptor reference входит в `snapshot.inputs` до вычисления ID;
отдельный ACCOUNTING serving component не создавать. Неполная доставка остаётся
PENDING, не становится необратимым REJECTED из-за ещё не прибывшего файла.

Backup включает immutable `accounting/`, исключает temporary files; restore
проверяет bytes и связи snapshot/descriptor/source. Старый snapshot остаётся
`accounting: NOT_AVAILABLE`, не получает ретроспективный ACCOUNTED.

**Тесты:** synthetic producer → publish → admit/snapshot → backup/restore;
прерывание каждой стадии и повтор; потеря event/report/binding/blob; другой source
или HEAD; same-name corrupt target при `--ignore-existing`; path traversal/symlink;
новый event после freeze; restore canonical subset без обязательного accounting.
Remote sync failure должен попасть в failed receipt. Не запускать real intake/OCR.

**Acceptance:** qualified snapshot нельзя получить без требуемого exact closure;
повтор идемпотентен; CURRENT остаётся прежним до проверки. Failed unpublished
workstation events отдельно входят в unique recovery inventory.

**Зависимости:** R03, pinned policy/producer, approved campaign. **Оценка:** M+M+S/M,
7–15 ч; CPU light и отдельная synthetic rsync qualification. **Rollback:** старые
snapshots/readers сохраняются; новые immutable descriptors не удалять.

## PW-01 — полная упаковка и service-scoped native identity

**Статус:** код упаковки и service-scoped identity реализован, installed-wheel CPU
проверка выполнена; Docker build и реальный native/model runtime NOT_RUN.

**Причина изменения:** gateway build context содержал только часть `vkm_corpus`, но
`retrieval.gateway.load_resources` теперь импортирует `update` modules. API-wide
dependency identity также требует packages, не принадлежащие узким service images.

**Файлы:** `infra/edge/build_images.sh`, gateway Dockerfile/requirements,
`update/remote_rerank.py`, `update/remote_retrieval.py`; отдельный pure-stdlib
`update/service_identity.py`. API использует свой закрытый `API_ALL_STORES_V1`:
реальные DB clients, binary psycopg, navigation dictionaries и active transitive
closure. Узкие services не наследуют эти лишние capabilities.

**Изменения:** устанавливать полный project wheel с узкими exact service locks.
Закрытый identity scope фиксирует actual installed source files и версии всех
используемых runtime dependencies; scope/schema/Python/OS/architecture входят в
hash. Loaded model/tokenizer/pack hashes, ownership, watches и resource limits
сохраняются. Optional encoder требует собственного квалифицированного профиля,
не исчезает из identity ради успешного startup.

**Тесты:** isolated installed wheel без repository PYTHONPATH; tiny synthetic
tokenizer/head, compatibility startup, qualified binding с fake downstream;
missing module/dependency, изменённый relevant file/version, endpoint mismatch.
Unrelated API code не меняет узкую service identity. Не строить Docker images и
не загружать реальные GPU models на этапе CPU implementation.

**Acceptance:** package содержит все actual imports; scope inventory полный;
код не выдаёт qualified identity при missing capability. **Оценка:** M, 2–4 ч,
CPU light. **Rollback:** previous immutable image/config и matching receipts.

## PW-02 / PW-04 — operator factory и реальная перепривязка receivers

**Статус на 04.10:** code wiring и CPU regression qualification добавлены:
`update.operator`, `operator_units`, `receiver`, CLI `deployment`, signed native
API/read-MCP proofs, full-response shared gate и image-reference preflight.
Readiness проверяет actual READ credential отдельно от operator credential.
Bootstrap текущего legacy CORE и shadow→live promotion остаются **code gates**;
actual Docker/restart/shadow/full MCP/restore — **NOT_RUN**. Это не завершение PW-02/PW-04.
Runbook: [CORE_OPERATOR_RU.md](../development/CORE_OPERATOR_RU.md).

**Причина:** coordinator и native observers существуют; process-local callback
не изменяет backend уже работающего отдельного API service. CLI runtime rollback
пока требует qualified deployment restore adapter.

**Изменения:** фиксированный operator factory и deployment plan/status/accept/
switch/recovery-plan/recover. Typed profile разрешает конкретные service IDs,
image/config hashes и endpoint identities; shell/import paths/SQL/Cypher из
manifest запрещены. Actual unit adapter сохраняет selected configs, выполняет
restart/rebind под общим gate и проверяет новый receiver перед открытием.
Все receiver processes используют один host-backed `admission.lock` inode;
control root отдельно writable, canonical data read-only.

Связать существующие `DurableDeployment`, `NativeServingBindings`,
`PrivateASGIProbeTransport`, `CandidateFence` и `AcceptanceRegistrar`, actual
49-tool plan и pinned services/components. Unknown service или generic health
не дают admission. Отказ rollback сохраняет maintenance.

**Тесты:** fake systemd/container API; unknown unit/image/config; restart failure;
replacement receiver во время drain; controller crash/ACK loss; exact restore;
corrupt candidate не блокирует проверенное восстановление previous.

**Предусловия:** подтверждённая host-local inventory и способ управления units.
Текущие compose/override конфиги нельзя считать уже совместимыми с новым gate.
**Оценка:** L+M, 6–12 ч CPU, затем CORE/EDGE qualification. **Rollback:** сохранённый
previous selector/config/image tuple; gate закрыт при недоказанном восстановлении.

### PW-02-B — первый qualified baseline

**Проблема:** текущий factory принимает только уже qualified previous. Legacy
receiver не выдаёт новый proof; acceptance первого baseline не может ссылаться
на несуществующий previous→candidate→previous drill.

**Цель:** отдельный cold/bootstrap contract, не ослабляющий обычную serving
acceptance. Закрепить legacy read-only inventory и retained old selectors/images;
подготовить изолированный baseline с CLOSED admission. Native proof/полная
private 49-tool acceptance могут выполняться в этом состоянии, но оно никогда
не считается обычным READY или научным admission. Отдельная bootstrap receipt
подтверждает empty/legacy rollback boundary и проверенное восстановление исходной
топологии. Уникальное содержимое и канон не переписываются.

**Файлы:** `update/acceptance.py`, `deployment.py`, `operator.py`, `api/production.py`,
receiver contracts, bootstrap schemas и runbook. Новый режим должен быть отдельным
typed capability с operator approval; старые SHADOW receipts не переименовывать.
**Negative tests:** forged baseline/legacy identity; открытый admission до полной
qualification; отсутствие saved previous; crash после регистрации до ACK;
невосстановимый legacy receiver; попытка использовать bootstrap receipt как
обычный full acceptance. **Acceptance:** первый baseline достижим без fake PASS;
ни один source request не проходит до admission; verified fallback остаётся
доступным после каждого сбоя. **Rollback:** сохранённая legacy topology, maintenance
при недоказанном восстановлении. **Зависимости:** PW-02, native inventory/S06.
**Оценка:** L, 6–12 ч CPU; CORE/EDGE drill отдельно. **Не менять:** original bytes,
legacy/frozen releases, модели и научные статусы.

### PW-02-P — явный shadow→live promotion

**Проблема:** shadow и live закономерно имеют разные project/root/endpoint/scope
hashes. Receipt исходного shadow нельзя молча считать квалификацией live profile.

**Цель:** immutable `PromotionBinding` с exact shadow plan/drill/acceptance SHA,
candidate image IDs, effective configurations, components/services/policy/code/
dependencies, live recipe SHA и отдельным isolation attestation. Разрешить только
явно перечисленные преобразования project/host endpoints/roots; значения данных,
capabilities, READ inventory и access semantics не менять. Не выкидывать profile
из signature ради прохождения gate. Live preflight повторно проверяет binding
перед любым side effect, сохраняет native previous и повторяет proofs после rebind.

**Файлы:** `update/operator.py`, `operator_units.py`, `acceptance.py`, promotion
contracts/schema/runbook. **Предусловия:** qualified baseline, завершённая shadow
acceptance, pinned image/config inventory и независимый backup.
**Negative tests:** другой live root/project; тот же digest с другим config;
добавленный WRITE capability; другой READ principal/upstream; stale/revoked policy;
changed recipe после подтверждения; incomplete rollback. **Acceptance:** достижимый
путь shadow→live с проверяемой mapping и подтверждением exact plan; arbitrary
differences BLOCKED. **Rollback:** whole coherent previous recipe/selectors.
**Зависимости:** PW-02-B, PW-03, S06, PW-04 runtime. **Оценка:** L, 6–12 ч CPU;
actual CORE/EDGE qualification отдельно. **Не менять:** store content ради code-only
update; scientific admission не повышать.

## PW-03 — native proof от действующих владельцев RERANK моделей

**Причина:** внешние text/visual model owners пока не подключены к load-time
identity contract. Authenticated `/identity` должен принадлежать тому же serving
client/process, а не ретроспективному sidecar.

**Изменения:** Python text owner использует `LoadedModelLease.load` и actual serving
object getter. Visual: native llama-server hook либо конкретный owned child с
load-time watch, PID/start/socket/weights/mmproj proof. Выбор owner делается по
исходникам и реальной topology. Не менять модель/quantization/inference contract.

**Negative tests:** fake readyz; другой client/endpoint/serving object; process
restart; substituted weights/mmproj; отсутствие load-time capture. **Acceptance:**
identity закрывается при каждой подмене; functional/VRAM parity отдельно.
**Зависимости:** доступ к actual model-owner sources и решение о visual ownership.
**Оценка:** L, 4–8 ч Python плюс отдельная C++/owned-process работа; GPU gate отдельно.
**Rollback:** previous immutable images/configurations.

## Порядок

```text
CPU CI / default Windows CLI encoding
       ├─ accounting publication/admission/backup ── real restore
       ├─ PW-01 packaging ── PW-03 native model owners ─┐
       └─ PW-02 receiver/operator factory ────────────┤
                                                    ↓
                                  PW-04 shadow acceptance
                                                    ↓
                             isolated restart/switch/restore drill
                                                    ↓
                                  approved production update

independent gold + review + exact corpus manifest ── full campaign
```

Production update не требует повторного intake или embeddings при неизменных
input/extraction/chunking/encoder/policy contracts. Приём оригиналов и завершение
full corpus scientific review остаются разными gates. Внешние notifications
по решению владельца отложены; status работает на сервере.

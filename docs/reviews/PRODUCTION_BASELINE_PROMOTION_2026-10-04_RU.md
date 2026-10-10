# Производственная программа: bootstrap, promotion и следующий порядок

Дата: 04.10.2026. Основание: разрешение владельца на реализацию S01–S26 и
продолжение оставшихся PW-02-B, PW-02-P, PW-03. Это отчёт о реализации и CPU
квалификации. Он не подтверждает deployment, сохранность реальных уникальных
данных, извлечение всего корпуса или научный допуск.

## Что сделано в этом продолжении

| Gate | Реализовано | Фактическая граница |
|---|---|---|
| PW-02-B | Typed startup/intent/preparation/probe/boundary/qualification/registration; отдельный cold factory и bootstrap CLI | CODE_FIXED_NOT_DEPLOYED; actual CORE/EDGE qualification NOT_RUN |
| PW-02-P | Immutable shadow plan/drill/receipt → approved live recipe binding, ограниченная mapping, fresh preflight, live-private acceptance после native rebind | CODE_FIXED_NOT_DEPLOYED; actual promotion NOT_RUN |
| Closed previous | Проверяемый CLOSED_BASELINE, original CLOSED owner, повторная native identity, rollback/recovery и согласование lost ACK | Synthetic qualification; real restart/restore NOT_RUN |
| PW-03 | Text load-time owner, owned-child lifetime/endpoint/weights/mmproj guards и schemas | PARTIAL: действующие owners UNWIRED; GPU/functional parity NOT_RUN |
| READ policy challenge | Отдельные actual READ credentials, allowed/denied principals, bounded host inventory и fail-closed validation | CPU/local API fixtures; deployed config NOT_RUN |
| Delivery | Exported schemas, Windows NOT_RUN accounting, runbooks, task status и hashed CPU receipt | Draft PR; это ещё не production update |

Bootstrap не требует выдуманного qualified previous. Он сохраняет legacy
topology, запускает только отдельный закрытый baseline и получает отдельную
private acceptance. Проверяются полный start → EMPTY, частичный API-only start
→ EMPTY и повторный закрытый start. Intent сохраняет исходную startup authority,
чтобы fresh recovery не зависел от испорченного candidate config. Повтор после
потери ACK возвращает ту же durable registration. Исходная legacy topology не
перезаписывается. Полная private acceptance в tests использует synthetic transport;
реальные 49 tools ещё не проверены.

Baseline имеет CLOSED_BASELINE_QUALIFIED, а не обычный READY. Он не открывает
public source admission и не заменяет ScientificUseAdmission. Forged OPEN,
stale native identity, изменённые startup/profile/intent и missing closure
отклоняются. Bootstrap backup — typed вход с проверяемым metadata closure;
наличие этой схемы не доказывает фактическую независимую копию или restore.

Promotion сохраняет hashes исходных shadow plan/profile/drill/receipt. Менять
можно только зарегистрированные leaf mappings project/root/endpoints и связанные
configuration pins. Capabilities, policy, READ principals, модели и данные нельзя
изменить таким переносом. Последовательность финального переключения в коде:
candidate CURRENT → native rebind → separate live-private probes → fresh
identity/writer/gate/profile checks → OPEN. Failed probes восстанавливают whole
coherent previous; CLOSED previous остаётся CLOSED. Candidate corruption не
блокирует отдельный previous-only recovery preflight.

PW-03 не завершён: Python contract удерживает identity фактического serving
object; owned-child contract удерживает native process lifetime, sockets и
weights/mmproj mappings. Действующий внешний text owner ещё не подключён.
Pinned llama-server не имеет необходимого native load hook; digest файла,
readiness sidecar и прочитанный mmproj не являются доказательством loaded model.
Разбор границы и точные места подключения сохранены в
[MODEL_OWNER_QUALIFICATION_RU.md](../development/MODEL_OWNER_QUALIFICATION_RU.md).

## Проверки

Машинный receipt:
[bootstrap_promotion_cpu_2026-10-04.json](../corpus_platform/receipts/bootstrap_promotion_cpu_2026-10-04.json).
Он закрепляет source/schema/test hashes, JUnit hashes и явные runtime NOT_RUN.
Полные локальные JUnit остаются в ignored work; это не публичные corpus данные.

| Проверка | Результат | Ограничение |
|---|---|---|
| Общая Linux integration selection | 628 PASS, 0 skip/failure | До последнего startup-SHA fence closed drill |
| Та же Windows selection | 560 PASS, 68 объявленных OS-specific NOT_RUN, 0 failure | Все skips сверены с runner; unexpected=0 |
| Final closed-drill/operator/promotion regression | 78 PASS Linux и 78 PASS Windows | После финального SHA fence; пересекается с общей selection |
| Final World | 602 PASS | CPU scientific contracts; не evidence admission |
| Public hygiene и installed wheel CPU | 7 PASS | Docker image не строился |
| Evidence schema export | PASS | Schemas совпадают с models |
| Canonical PUBLIC+PRIVATE verifier | 36 PASS, 7 SKIPPED_REF_UNAVAILABLE, 0 blocking failures | Исторические anchors не подтверждены; не объявлены PASS |

Суммировать пересекающиеся test counts нельзя. Hosted CI исходного commit
`f50fa1b848f099afdb089aaf4cdbe5c116fe6d4f` был SUCCESS, run 37195005133.
Он не квалифицирует новое дерево: exact-head hosted CI остаётся отдельной
проверкой после публикации commit. Итоговый статус этого CI следует смотреть в
[draft PR 10](https://github.com/SUKUNA-AI/vkm-subsidence-forecasting/pull/10).

## Что остаётся реализовать и выполнить

1. **Подключить actual model owners.** Внести hook в контролируемый text owner;
   для visual реализовать native load-time hook по зафиксированным исходникам
   или квалифицированный owned-process вариант с реальным доказательством mmproj.
   Не менять модель, quantization и inference contract; проверить parity отдельно.
2. **Реальное защищённое хранение.** Получить operator-selected roots/inventory;
   установить UNBACKED_UNIQUE, проверить independent copy в другом failure domain
   и выполнить bounded restore drill. Synthetic backup receipt недостаточен.
3. **Actual isolated qualification.** Exact clean source, installed locks,
   pinned image/config/native IDs, closed shadow, реальные 49 tools и READ-denial
   challenge; partial-start/restart/restore/rollback drill. Оставлять NOT_RUN до
   выполнения; unsupported native proof означает BLOCKED.
4. **Production switch.** Только после предыдущих gates и отдельного разрешения
   владельца: approved live recipe, короткое окно недоступности, fresh probes,
   coherent switch и post-switch verification; при отказе whole previous rollback.
   Code-only update не требует intake/OCR/re-embedding при неизменных data contracts.
5. **Полнота научных данных.** Достроить и квалифицировать сложную DOCUMENT
   fidelity, table continuations/grids, локальные formula bindings,
   source-backed claims/entities/observation sets, origin/conflicts и обе
   временные оси. Существующие schemas/adapters не означают готовые данные корпуса.
6. **Независимая приёмка.** Зафиксировать stratified gold до настройки; проверить
   пропуски, числа/знаки, структуру/шапки, units, entity links и time. Все используемые
   научные данные и подозрительные места сверить с оригиналом; остальные проверять
   независимо. Нечитаемое и ambiguous остаётся unresolved.
7. **S26.** Frozen corpus manifest, reuse допустимых результатов, budgeted missing
   stages, resume, исправление систематических дефектов, review и публикация одного
   coherent generation. Campaign не запускалась и отдельным PASS не подменяется.
8. **Эксплуатация.** MCP/config/read-policy qualification; real backup status,
   coverage/review debt/generations; required checks после доступного owner/admin
   права и read-back. Внешний канал уведомлений отложен по решению владельца.

## Порядок

```text
actual model owners ───────────────────┐
independent backup + restore ──────────┤
exact clean source/images/config ──────┤
                                      ↓
                    CLOSED bootstrap / isolated shadow
                                      ↓
                    actual 49 tools + failure/recovery drill
                                      ↓
                    approved live recipe + owner switch approval
                                      ↓
                    rebind → live probes → OPEN → verification

document/table/formula fidelity ───┐
claims/origin/entity/time ─────────┤
independent gold + review ─────────┤
                                  ↓
                       frozen campaign + S26 + coherent publication
```

Можно параллельно реализовывать actual owners, готовить bounded backup inventory
и завершать extraction/semantic fidelity. Новые GIS не требуются для разработки
этих контрактов; квалификация новых форматов и provenance зависит от поступивших
оригиналов. До независимой копии оригинал не объявляется защищённо сохранённым.
До review использованные значения не получают научный допуск.

Продолжение не выполняло CORE/EDGE changes, Docker build/start/restart, actual
backup/restore, intake/OCR/embeddings, GPU/solver/ML или full campaign. PRIVATE,
main и GitHub settings не изменялись. Изменения PUBLIC подготовлены для review
в существующем draft PR; merge требует слова владельца.

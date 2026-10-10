# Производственная программа данных: выполнено и оставшиеся работы на 04.10.2026

Продолжение разрешённой реализации плана S01–S26. Программа не завершена:
кодовые контракты и синтетическая квалификация не равны полному извлечению корпуса,
проверке оригиналов или работающему production deployment.

Изменения готовятся в существующем [PUBLIC draft PR №10](https://github.com/SUKUNA-AI/vkm-subsidence-forecasting/pull/10).
[PRIVATE draft PR №8](https://github.com/SUKUNA-AI/vkm-subsidence-forecasting_resourses/pull/8)
содержит только четыре дополнительных LFS patterns. Оба PR открыты, draft,
не merged; PRIVATE в этом продолжении не изменялся. До текущего продолжения
PUBLIC tip — `73f2f55f494b2b6cc00e1b4e39840c61c792504b`, семь commits.
Финальный tip текущего изменения следует брать из PR, а не из этого baseline.

## Что уже реализовано

Последующее продолжение 04.10 закрыло crash-gap перед первым baseline: durable
admission record, closed metadata startup, final publication fence и idempotent
recovery ACK. [Runbook](../development/CORE_OPERATOR_RU.md#смерть-контроллера-и-durable-admission)
описывает новую границу. Bootstrap/PW-02-P/PW-03 и реальные исполнения остаются
открытыми. Старые CPU/CI counts ниже относятся к предыдущему состоянию;
Новая qualification: Linux 782 PASS (API/MCP/admission/schema + world), Windows
161 PASS/46 exact OS NOT_RUN для связанного API/MCP набора; schema check PASS.
PUBLIC/PRIVATE staged verifier: 36 PASS, 7 known unavailable historical anchors,
blocking failures отсутствуют. Новый
[source-bound CPU receipt](../corpus_platform/receipts/durable_admission_cpu_2026-10-04.json)
содержит source/code/JUnit hashes. Hosted CI нового commit — отдельный gate.

В дополнительной Windows проверке исправлены UTF-8 test readers и native DOS/UNC
alias comparison при concurrent artifact publication. False escape воспроизведён
в 4 из 6 instrumented synthetic runs; после исправления все 6 runs прошли.
Negative containment cases сохраняют root boundary и отказ для других
drive/server/share/GUID/device namespaces; native I/O path не переписывается.
Полный локальный Windows world после этих исправлений: 574 PASS, 1 FAIL из-за
отсутствия symlink privilege (WinError 1314), включая unsandboxed run. Это
ограничение среды; проверка не отключена и FAIL не повышен до PASS/NOT_RUN.
Linux world проверяет actual symlink escape. Изменение прав Windows в этом
проходе не выполнялось; новый hosted Windows job обязан проверить весь набор.

| Направление | Результат в коде | Что этим не подтверждено |
|---|---|---|
| CI / producer | Clock injection, future-time/retention guards, redaction, exact CPU locks, Linux/Windows checks, code/input/config identity и resource admission | Required main checks ещё не применены; реальный clean producer workload не квалифицирован |
| GIS / Excel | Hybrid logical DATASET registry, immutable versions/members, source links, отдельные access/experimental roles; direct GIS conversion и native Excel inspection | Новые реальные комплекты не приняты; их CRS, происхождение и scientific admission не проверены |
| DOCUMENT / accounting | Native locators/структура, candidate/disposition учёт; immutable closure включён в publication, receiving admission, snapshot и backup | Полнота обнаружения объектов и fidelity всего корпуса не доказаны |
| EVIDENCE / review | Version-pinned records, append-only decisions, idempotency/revision guards, dependence invalidation, historical read и scientific-use contracts | Реальные экспертные решения, exhaustive entity linking и полный timeline ещё не построены |
| Consistent generations | DOCUMENT/NAV/EVIDENCE graph bundle, coherent selectors, dependency-aware partial NAV, native readers и durable recovery | Production stores и текущие процессы CORE ещё не переключены на новое поколение |
| API / MCP | Policy-filtered pagination/dependencies/review; в этом продолжении — proofs действующих API/read-MCP и общая граница drain | Production полный MCP acceptance и выдача всех реальных объектов ещё NOT_RUN |
| Recovery / status | Bounded inventory, UNBACKED_UNIQUE по operator manifest, independent-copy verification, честные NOT_RUN и maintenance | Полный реальный inventory и восстановление уникальных данных не выполнены |

Полная матрица S01–S26: [PRODUCTION_DATA_PROGRAM](../planning/PRODUCTION_DATA_PROGRAM_2026-10-01_RU.md).
Здесь перечислены результаты реализации; ни одна строка не означает завершения
всего соответствующего этапа или научной программы.

## Что добавлено именно в этом продолжении

Реализован закрытый CORE operator для двух units: `api` и read-`mcp`.
Factory соединяет существующие `DurableDeployment`, native serving bindings,
private 49-tool acceptance, candidate fence и acceptance registrar. CLI:
`deployment status/plan/drill-plan/accept/switch/recovery-plan/recover/drill`.

Operator принимает owner-controlled immutable конфигурацию с exact code/dependency/
policy/release pins. Manifest не может передать shell, Python callback, SQL/Cypher,
произвольный service ID или окружение исполнения. Authority, protected data,
control writes и qualification outputs проверяются отдельно; traversal и
подмена BoundFile не допускаются. Нормальные runtime-конфиги и секреты в Git
не добавлялись.

Fixed unit adapter проверяет fully-rendered Compose, native image/config identity,
loopback ports, resource bounds и read-only mounts. Перед recreate выбранный image
reference обязан разрешаться в exact qualified local image ID. Код не допускает
implicit `.env`, interpolation, image build/pull, cleanup или управление другими
хранилищами. В этой сессии Docker transport не исполнялся: тесты используют
явно обозначенный fake transport.

API и действующий read-MCP SDK server выдают отдельные bounded nonce/HMAC proofs.
Operator связывает proofs с native container PID namespace/process start и
повторно проверяет container identity после запроса. MCP доказывает собственный
ApiClient, READ inventory и тот же upstream API process. Обычный `healthz`
не заменяет эти доказательства.

Рабочий READ token проверяется отдельно от operator credential. Wrong, write и
dual tokens отвергаются. Exact `AccessContext` рабочего MCP обязан совпадать с
`reader_principal`, прошедшим исходный acceptance plan конкретного release;
другой действительный READ context не подтверждает READY. Секрет в proof не
возвращается, сохраняется HMAC binding.

Общий native admission gate удерживается до последнего байта MCP response,
включая отдачу уже полученных от API данных. Поэтому drain ждёт завершения
MCP-выдачи. При recovery identity управляющего кода и policy не зависит от
повреждённого candidate: проверенный previous можно восстановить, даже когда
candidate configuration или service уже недоступны. Live READY требует открытый
admission на обоих фактических receivers.

Дополнены строгие JSON schemas, negative tests и точный перечень OS-specific
NOT_RUN. Независимый challenger подтвердил устранение найденных root causes;
его static review не считается отдельным runtime-прогоном.
Подробный контракт: [CORE_OPERATOR_RU](../development/CORE_OPERATOR_RU.md).

## Проверки и границы доказательств

| Проверка | Фактический результат |
|---|---|
| Frozen Linux до последнего exact-context fix: operator/receiver/lifecycle/acceptance и API/MCP regressions | 213 PASS, 0 FAIL/skip; 241.83 s |
| Соответствующий Windows frozen набор без Linux-native provider cases | 170 PASS, 11 явных POSIX NOT_RUN, 0 FAIL; 97.46 s |
| Окончательный exact-context/operator/receiver/lifecycle/acceptance Windows | 121 PASS, 11 явных POSIX NOT_RUN, 0 FAIL; 44.59 s |
| Окончательный Linux, включая actual local native provider | 164 PASS, 0 FAIL/skip; 177.64 s |
| Обязательные `tests/world` на окончательных bytes | 557 PASS, 0 FAIL/skip; 22.01 s |
| PUBLIC + PRIVATE verifier после staging новых файлов | 36 PASS, 7 known historical tag anchors `SKIPPED_REF_UNAVAILABLE`, blocking failures отсутствуют; 43 checks, exit 0 |
| Exported schema determinism | `scripts/export_evidence_schema.py --check`: PASS |

Локальные наборы пересекаются; их counts нельзя суммировать. Native provider
tests используют небольшие синтетические локальные данные; это не доказательство
Docker deployment, внешних model owners или полноты оригинального корпуса.
Windows POSIX NOT_RUN не заменяется PASS. Для CPU tests использованы существующие
exact venv: WSL Python 3.13.5; Windows Python 3.13.13. Зависимости не устанавливались.
Sanitized counts, exact source hashes и JUnit SHA:
[CPU qualification receipt](../corpus_platform/receipts/core_operator_cpu_2026-10-04.json).

Предыдущий exact hosted baseline `73f2f55`: все три jobs SUCCESS,
[run 36904526352](https://github.com/SUKUNA-AI/vkm-subsidence-forecasting/actions/runs/36904526352).
World — 554 PASS и wheel smoke PASS; corpus — 2921 PASS, 33 external NOT_RUN;
Windows — 1811 PASS, 54 exact OS/capability NOT_RUN, 6 external NOT_RUN.
Этот baseline перепроверен через GitHub; он не квалифицирует новый tip.
Новый exact tip обязан пройти все три jobs заново.

## Что остаётся реализовать и выполнить

1. **PW-02-B — первый qualified baseline.** Текущий operator требует уже
   квалифицированный previous. Нужен отдельный typed bootstrap contract для
   legacy CORE: сохранённая исходная topology, закрытый admission, полноценная
   private acceptance, отдельная receipt и доказуемый fallback. Bootstrap receipt
   не должна выдавать обычный READY или заменять scientific admission.
2. **PW-02-P — explicit shadow→live promotion.** Shadow/live различаются
   project/root/endpoints/scope. Нужен immutable binding исходных plan/drill/receipt
   к approved live recipe и строго ограниченной mapping. Нельзя просто удалить
   profile hash из проверки или объявить shadow receipt live qualification.
3. **PW-03 — actual model owners.** Подключить native load-time identity
   действующих text/visual rerank owners к serving process. Для visual определить
   по исходникам конкретный native hook/owned-process contract; сторонний
   sidecar или сохранённый digest не заменяет доказательство loaded model.
4. **Actual isolated runtime qualification.** После этих code gates — pinned
   clean source/image/config, independent backup, bounded shadow startup,
   actual 49-tool MCP acceptance, failed-switch/restart/restore drill и полный
   coherent previous rollback. Затем отдельно разрешённое production switch
   с выбранным владельцем коротким окном недоступности. Сейчас это NOT_RUN.
5. **Полнота научных данных.** Достроить и квалифицировать сложную DOCUMENT
   fidelity, продолжения/сетки таблиц, локальные symbol bindings, source-backed
   claims/entities/observation sets, конфликты и обе временные оси. Schema и
   contracts уже существуют; полные данные корпуса и связи ещё не получены.
6. **Независимая приёмка.** Зафиксировать stratified gold и метрики пропусков,
   чтения, структуры, единиц, entity links и времени. Проверить по оригиналу
   все используемые научные данные и подозрительные места; прочие извлечения
   проверить независимо. Нечитаемое сохранять явно unresolved.
7. **S26 — full managed campaign.** Зафиксировать точный corpus manifest,
   переиспользовать действительные результаты, выполнить недостающие стадии
   с budget/resume, закрыть систематические дефекты и опубликовать одно
   согласованное поколение. Это отдельное исполнение после всех применимых gates.
8. **Эксплуатация.** Real UNBACKED_UNIQUE inventory и restore; required GitHub
   checks после права владельца; current MCP/config/read policy qualification;
   server status по coverage/review/generations/backup. Внешний канал уведомлений
   отложен по решению владельца.

Task cards, negative tests, rollback и оценки:
[следующие deployment gates](../planning/PRODUCTION_DATA_NEXT_GATES_RU.md),
[остаточная полная программа](../planning/PRODUCTION_DATA_REMAINING_TASKS_RU.md).
Оценки bootstrap и promotion — по 6–12 агентных часов реализации, затем отдельно
измеряемая runtime qualification. Это диапазоны объёма, не обещание календарного срока.

## Порядок продолжения

```text
exact-tip CI + schema/leakage/link gates
                 ↓
PW-02-B first baseline ── PW-03 model-owner identity
                 └────────────┬────────────────────┘
                              ↓
                  full isolated shadow acceptance/drill
                              ↓
                  PW-02-P exact shadow→live binding
                              ↓
                  approved switch + postcheck + rollback

DOCUMENT/table/formula fidelity + origins/timeline
                 ↓
independent gold + original-source review + admission
                 ↓
locked corpus campaign → coherent publication → recovery qualification
```

Приём original bytes 07.10 имеет отдельный gate: полный multi-file manifest,
fresh SHA-256, immutable quarantine, лицензия/owner/state date/access/role и
проверенная независимая копия. Конвертация требует format/CRS/schema rehearsal;
агентная выдача — current policy и принятые derived projections; научное
использование — original-source verification и точный use admission. Завершение
всей научной программы не требуется для безопасного сохранения оригиналов.

Code-only deployment при неизменных data/encoder/policy contracts не требует
повторного intake, OCR или embeddings. Облачное чтение разрешено; это не разрешает
использовать TARGET/TEST_SEALED для подбора параметров.

## Что в этом продолжении не исполнялось

Не выполнялись main merge или GitHub settings changes, Docker build/pull/restart,
изменения CORE/EDGE, real intake/data transfer, OCR, embeddings, GPU, solver/ML,
full corpus campaign, реальные scientific admission или backup/restore данных.
Внесён код и документация; выполнялись только ограниченные CPU synthetic/read-only
checks. Научные binary originals, legacy/frozen releases и пользовательский
`.mcp.json.example` не изменялись.

# Производственная система данных ВКМ / СКРУ-1

Основание: план владельца от 01.10.2026, разрешение на реализацию в этой сессии.
Цель — полный охват действующего корпуса, проверка используемых научных данных,
связность, происхождение и хронология. Новое GIS/Excel принимается с классом
PRIVATE_CLOUD_ALLOWED по решению владельца; научная роль задаётся отдельно.

Канон: неизменяемые оригиналы и artifacts → DOCUMENT/DATASET → EVIDENCE →
review/admission → согласованные DuckDB/Neo4j/OpenSearch/NAV/MCP projections.
UNKNOWN, scope, scale, конфликты, версии и независимость наблюдений сохраняются.
Программа не включает solver/ML и автоматически не допускает данные научно.

Продолжение на 04.10: [отчёт и следующий порядок](../reviews/PRODUCTION_DATA_CONTINUATION_2026-10-04_RU.md),
[CORE operator runbook](../development/CORE_OPERATOR_RU.md). Таблица ниже сохраняет
раздельные code, qualification и actual corpus/runtime статусы.
Последующий этап: [bootstrap/promotion и оставшиеся runtime gates](../reviews/PRODUCTION_BASELINE_PROMOTION_2026-10-04_RU.md).

## Порядок и критерии

| ID | Результат | Зависимости |
|---|---|---|
| S01 | Детерминированный clock и проверка будущих receipts | — |
| S02 | Redaction без изменения фиксированных schema keys | — |
| S03 | CPU dependencies, Windows/Linux CI | S01,S02 |
| S04 | Required checks и read-back настроек | S03, GitHub admin |
| S05 | Hybrid dataset/version/member registry, access/role | — |
| S06 | Backup без допуска повреждённых immutable bytes, restore drill | — |
| S07 | TAB/MIF→GPKG, Excel inspection, synthetic rehearsal | S05 |
| S08 | Чистый producer, code/lock/input hashes, memory admission | — |
| S09 | plan/dry-run/execute/resume/status/rollback | S05,S06,S08 |
| S10 | Shadow serving, NAV validate/repack, acceptance, switch | S03,S06,S09 |
| S11 | UNKNOWN units, QGIS receipts и policy enforcement | S05 |
| S12 | Topics coverage и ranged section attribution | S09,S10 |
| S13 | Честные jobs/OCR/backup/generation статусы | S06,S09 |
| S14 | Runtime inventory, builder provenance, operational runbooks | — |
| S15 | Evidence types и immutable campaign manifest | S05 |
| S16 | Page/object candidate/disposition accounting | S08,S15 |
| S17 | Native document fidelity и exact locators | S15,S16 |
| S18 | Raw/interpreted grids, continuation, Excel/GIS links | S07,S17 |
| S19 | Formula occurrences, bindings, assumptions/conflicts | S15,S17 |
| S20 | Claims/entities/primary observation origins | S15–S19 |
| S21 | Предметная/информационная хронология, known-at | S20 |
| S22 | Durable review, idempotency, lineage/admission invalidation | S06,S09,S20,S21 |
| S23 | Полная pagination/export, source-backed review access | S22,S24 |
| S24 | Generation consistency и evidence projections | S09,S10,S20–S22 |
| S25 | Независимый gold, quality/recovery/load qualification | S17–S24 |
| S26 | Полная управляемая кампания корпуса | обязательные S01–S25 |

Этапы являются gates производственной программы. Отсутствующая runtime-проверка
имеет статус NOT_RUN; наличие кода и synthetic PASS не закрывает corpus processing.

Полнота разделяется: archive complete; extraction accounted; structure complete;
semantic reviewed; scientifically admitted. Учёт 100% обнаруженных объектов не
доказывает recall детектора. Независимый эталон и проверка всех научно используемых
значений по оригиналу обязательны. Приём оригиналов 07.10 не зависит от завершения
полной семантической программы.

## Фактический статус реализации (обновлено 04.10.2026)

Это состояние рабочего дерева, а не сертификат production-ready. `IMPLEMENTED`
означает наличие кода указанного контракта; `PARTIAL` — реализована только часть
этапа. Ни один этап не считается полностью закрытым только по этой колонке.
Тесты ниже являются синтетическими CPU-проверками, если явно не указан другой runtime.
Финальные CI receipts должны относиться к тому же точному commit, который предлагается
к публикации: результаты более раннего состояния общей рабочей ветки его не заменяют.

| ID | Код / локальный контракт | Квалификация | Production / научное исполнение |
|---|---|---|---|
| S01 | IMPLEMENTED: injected UTC clock, future/terminal backup guards; retention по snapshot identity | Целевые nightly/dossier regression tests выполнены; финальный CI на публикуемом commit остаётся gate | Изменения nightly на CORE/EDGE не развёрнуты |
| S02 | IMPLEMENTED: host-independent scanner, сохранение schema keys, отказ при key collision | Целевые redaction tests выполнены; проверка всего публикуемого дерева остаётся gate | Новая redaction не развёрнута в production |
| S03 | IMPLEMENTED: exact CPU pins, Linux/Windows jobs, dependency closure и обязательный учёт NOT_RUN | Последний hosted baseline f8b1160: world 557/wheel PASS; corpus 2973 PASS/33 external NOT_RUN; Windows 1863 PASS/57 explicit OS/capability NOT_RUN/6 external. Все 3 jobs SUCCESS, run 37189971516. Durable admission/Windows path fixes требуют нового exact-tip CI | Required checks ещё не включены; локальный Windows symlink capability остаётся обязательным, его отсутствие не скрывается skip |
| S04 | NOT_APPLIED: имена jobs определены, настройки только прочитаны | Read-only refresh 04.10: PUBLIC/PRIVATE main protected=false; PUBLIC rulesets пусты; PRIVATE rulesets: тарифный 403. Protection-detail API ранее connector 403 | BLOCKED: GitHub admin/доступ интеграции; изменения settings не выполнялись |
| S05 | IMPLEMENTED: hybrid dataset catalogue, immutable versions/members, SOURCE↔DATASET links и единый update routing; access class/experimental role отдельно | Synthetic dataset registration/resume/ACK/policy tests; реальные новые комплекты не принимались | Реальные dataset manifests/admission: NOT_RUN |
| S06 | PARTIAL: immutable mismatch запрещает promotion; явные inventory/verify и synthetic restore command | Recovery: 35 Windows PASS и 35 WSL PASS; два полных SHA-прохода в явном бюджете, original synthetic bytes неизменны; полный real restore: NOT_RUN | Новые backup scripts не развёрнуты; уникальные внешние artifacts не объявляются сохранёнными |
| S07 | IMPLEMENTED: native GIS/Excel inspection/conversion и opt-in synthetic runtime rehearsal | По результату GIS-исполнителя: 7 actual GDAL/xlrd synthetic tests PASS; 104 unit PASS, 2 Windows link tests NOT_RUN; exact-commit CI остаётся gate | Приём и конвертация реальных новых данных: NOT_RUN |
| S08 | IMPLEMENTED: production producer identity, clean relevant tree, fresh source hashes, ресурсные guards | Целевые producer/registry tests выполнены; реальный clean checkout + exact locks ещё не квалифицирован | Production processing: NOT_RUN; неподдерживаемый memory enforcement закрывает запуск |
| S09 | PARTIAL: immutable campaign, bounded CPU execute/resume, datasets; durable intent/recovery, CLOSED/OPEN, закрытый CORE operator CLI; typed bootstrap/recovery и explicit promotion | CPU regressions проверяют два EMPTY fallback, сохранение legacy, CLOSED previous rollback, private 49-tool contract, failed live probe и lost ACK. Exact-tip CI и actual units остаются gates | Реальный bootstrap/restore/switch NOT_RUN; campaign не выполнена |
| S10 | PARTIAL: shadow DuckDB/NAV, native store/model/control observers, actual API factory, signed API/read-MCP identity; ограниченная shadow→live mapping и separate live-private acceptance после rebind | CPU/fake transport qualification; native container/image/config/PID/loopback/upstream и actual READ context проверяются closed adapter. Docker/native endpoints/full MCP rehearsal: NOT_RUN | CORE/EDGE deployment/switch NOT_RUN; actual text/visual model owners UNWIRED |
| S11 | IMPLEMENTED: UNKNOWN unit handling и policy/receipt guards в узком GIS контуре | GDAL synthetic rehearsal проверяет NonEarth/unknown CRS и отказ при несовпадении bytes; это не full deployed MCP acceptance | Production MCP registration/configuration и реальные GIS admission: NOT_RUN |
| S12 | PARTIAL: ranged section attribution, sections_v2 и dependency-aware partial NAV builds реализованы | Sections/store/API/graph/status: 97 PASS на каждой ОС; отрицательные случаи mixed ranges, foreign overrun, partial outline. NAV инвалидирует зависимые parts при смене upstream bytes/rules/options | Реальный lab/NAV/topics/graph refresh и повторный подсчёт orphan sections: NOT_RUN |
| S13 | PARTIAL: явные readiness/status/error contracts; NOT_RUN не повышается до PASS | Synthetic API/jobs/status проверки; unattended delivery/recovery ещё отдельный gate | OCR debt не обработан; полный контроль удалённых jobs и delivery не квалифицирован |
| S14 | PARTIAL: producer provenance, environment observations, update runbook и file-only inventory по operator manifest | Реализованы metadata-only plan, вычисляемый UNBACKED_UNIQUE и fresh independent-copy verification; реальные roots не сканировались | Полный UNBACKED_UNIQUE inventory и его восстановление: NOT_RUN |
| S15 | IMPLEMENTED: строгие evidence types, version-pinned transfer/use context, campaign и исторический read context | Synthetic schema/provenance/policy/history tests; Phase1 migration сохраняет original bindings, unresolved map и private archive | Реальный evidence migration/journal/campaign не опубликован |
| S16 | PARTIAL: actual stage accounting и immutable publication closure подключены к publish/admit/snapshot/backup/runtime | Source lineage не допускает downgrade; policy проверяет фактические source IDs переносимого payload; incomplete delivery PENDING; base и CURRENT проверяются при restore/retry. Linux 138 targeted PASS; Windows 134 PASS/4 capability NOT_RUN; последний retry guard отдельно 4 PASS на каждой ОС | Реальная доставка/restore и независимый recall denominator: NOT_RUN; ACCOUNTED не означает полное обнаружение |
| S17 | PARTIAL: native artifact/provenance/locator contracts и native_prepare adapter | Synthetic native document checks; DOCX без pinned render остаётся BLOCKED | Полная проверка native fidelity и сложных областей по оригиналам: NOT_RUN |
| S18 | PARTIAL: table/grid representation, locators и выдача; отдельные GIS/Excel manifests | Synthetic structure/API tests; полный разбор продолжений и cross-source links не квалифицирован | Полное извлечение/проверка таблиц корпуса: NOT_RUN |
| S19 | PARTIAL: FormulaInterpretation/SymbolBinding и versioned references | Schema/reference tests; корректность всех формул из источников не подтверждена | Полная extraction/binding/formula review campaign: NOT_RUN |
| S20 | PARTIAL: typed source-backed semantic extraction adapter, private raw responses и exact cache/model/tokenizer/config identities | Synthetic original-span, untrusted-model, prompt-injection, bounded-worker и policy tests; endpoint без native model identity BLOCKED | Реальный model endpoint не квалифицирован; полный source-backed extraction/entity resolution: NOT_RUN |
| S21 | PARTIAL: temporal поля, exact historical journal revision, available-as-of и recorded-at; текущая policy перепроверяется при историческом чтении | Synthetic competing versions, future availability, historical cursor и revocation tests | Полная предметная/информационная хронология корпуса: NOT_RUN |
| S22 | IMPLEMENTED: append-only journal, single publisher, request/base guards, review/admission invalidation | Synthetic ACK-loss/corruption/concurrency tests; power-loss/restore qualification: NOT_RUN | Реальный review journal и expert admission: NOT_RUN |
| S23 | PARTIAL: policy-filtered evidence pagination/dependencies/review API и narrow MCP access | Synthetic traversal/cursor/access tests; status после revocation не возвращает устаревшие READY/data. API dependency identity включает реальные store clients/native driver и morphology closure | Полная выдача всех реальных objects/original locators и deployed MCP acceptance: NOT_RUN |
| S24 | PARTIAL: coherent generation, local projections и общий DOCUMENT/NAV/EVIDENCE graph; native receiver guards | Bounded SQLite graph bundle, historical pinned originals, readback/resume без overwrite, actual read-only receiver и cross-store SHA checks проверены синтетически | Actual remote load/switch: NOT_RUN; единого production generation ещё нет |
| S25 | PARTIAL: adversarial/recovery tests как основа квалификации | Независимый gold, full-scale load/latency, real restore, экспертная QA: NOT_RUN | Нет доказанного corpus recall или production throughput |
| S26 | NOT_RUN: контракт кампании существует, но он не является её исполнением | Полный locked campaign manifest с PASS всех обязательных gates ещё не квалифицирован | Full corpus processing, exhaustive linking/timelines и original-source expert review: NOT_RUN |

### Проверки и окружения

Точная выборка и ограничения описаны в [OFFLINE_CHECKS](../development/OFFLINE_CHECKS.md).
`world-integrity` обязан выполнить всё `tests/world`, public verifier и wheel smoke;
`corpus-offline` рекурсивно включает остальные test suites, в том числе `tests/evidence`
и `tests/datasets`. `windows-offline` явно включает эти два fast suites, world,
engineering, pure QGIS, nightly и backup tests. Неожиданные skips, collection errors,
xfail/xpass и отсутствие прошедших тестов не допускаются. Только явно исключённые
`services/gpu/desktop/matlab/ansys/qgis_runtime`, документированные host prerequisites и точные OS-specific cases
имеют отдельный NOT_RUN; это не свидетельство пригодности соответствующего runtime.

Workflow использует Python 3.13.5. Linux/Windows corpus jobs устанавливают существующие
`worldspec.lock.txt`, `corpus.lock.txt`, `corpus-services.lock.txt` и
`offline-test.lock.txt` с `--no-deps`; world job использует worldspec и pin setuptools
из `corpus-layout.lock.txt`. В отдельном новом Linux окружении установлена комбинация
четырёх CPU locks: Python 3.13.5, ровно 84 distributions, все 84 требуемые версии
совпали; независимый `uv pip check` завершился с exit 0. Baseline первого tranche `corpus-offline`:
2305 PASS, 0 failures/skips, 33 внешних cases deselected NOT_RUN, 958.04 s;
итог `PASS_WITH_NOT_RUN`. Baseline `world-integrity`: 525 PASS, wheel PASS,
verifier без object failures; 7 отсутствующих tag anchors и 2 PRIVATE-dependent
comparisons остаются NOT_RUN.
Ранее inspected WSL Python 3.13.15 и Windows Python 3.13.13 — другие окружения;
их отдельные PASS не использованы как доказательство этой новой lock qualification.
Дополнительно создано отдельное native Windows окружение Python 3.13.13:
87/87 применимых pins и `pip check` PASS. Свежая установка обнаружила и помогла
закрыть Windows dependencies `pywin32==312` и `tzdata==2026.3` с Windows-only markers.
Baseline Windows job остался FAIL: 1142 PASS, 1 WinError 1314 при создании symlink,
34 NOT_RUN (27 объявленных OS-specific и 7 неожиданных из-за symlink privileges),
6 внешних deselected; 90.10 s. Привилегии не менялись. Hosted Windows Python 3.13.5
с нужными filesystem capabilities и все три Actions jobs на публикуемом commit
остаются gates.

Linux и Windows проверили одни и те же 738 code/test/schema/lock/workflow files:
before/after SHA-256 manifest одинаковый
`84b3e82d1bbeb717737ed807a06906b3b80f5eb4424264e5cdc37cc15d82aa07`.
Во время Linux run Git HEAD перешёл на
`ecf25c6a3fedca4e04a001320ec29319b623bbdb`, а проверенные bytes не изменились.
Raw summary/JUnit/environment/per-file hash receipts сохранены вне checkout;
это local qualification, не hosted Actions или production acceptance.
Это baseline первого integration tranche на указанном commit. Следующие изменения
dataset catalogue, lifecycle/recovery и native remote/pack contracts должны получить
новые targeted и полные qualification receipts; этот baseline их не покрывает.

Повторная qualification второго tranche проверяет 801 одинаковый code/test/schema/
lock/workflow file на обеих ОС. SHA-256 before/after manifest:
`2231ea0cccabe914cf4bb470edfb409de12c18cf37c96fd7f01c8b12b43ef350`.
`world-integrity`: 552 PASS за 25.19 s; wheel install/import/resources PASS;
verifier без object failures, 7 отсутствующих tags и 2 намеренно не подключённых
PRIVATE comparisons остаются NOT_RUN. `windows-offline`: 1617 PASS, 1 FAIL
из-за WinError 1314, 59 skips и 6 внешних deselected за 200.56 s. Из skips 52 —
точно объявленные OS-specific NOT_RUN, 7 — неожиданные из-за отсутствующих
symlink privileges. Проверка намеренно сохраняет FAIL, не скрывает его skip.
`corpus-offline` второго tranche ещё выполняется; PASS заранее не заявляется.

Отдельный полный PUBLIC+PRIVATE verifier в UTF-8 profile: 43 checks, 36 PASS,
7 SKIPPED_REF_UNAVAILABLE, exit 0; включая catalogue sync, leakage и local links.
Без UTF-8 запуска найден legacy cp1251 stdout defect: JSON-файл записан, но
процесс падает при выводе символа ≥. Исправление CLI ещё не включено в указанный
801-file qualification fingerprint.

Production producer использует другой контракт: обязательный `corpus.lock.txt`,
layout lock при выбранном GPU layout и явно добавленные exact locks. Он проверяет
установленные версии и хеши tracked lock files; не устанавливает зависимости.
Подробности — [PRODUCER_GUARD](../development/PRODUCER_GUARD_RU.md).

В ходе финальной интеграции главный исполнитель сообщил 147 PASS за 17.20 s для
synthetic evidence/runtime checks, включая projection builder/policy/cursor и
production selected-file/omitted-store guards. Отдельный recovery suite выполнен
на Windows и WSL: по 35 PASS, без skips, без реального inventory/copy/restore.
Эти результаты не заменяют обязательные CI jobs. Fresh Linux/Windows dependency
closure подтверждена отдельно; Windows filesystem qualification пока не закрыта.
Команды и границы recovery — [UNIQUE_RECOVERY](../development/UNIQUE_RECOVERY_RU.md).

### Открытые gates перед production и S26

Адресная qualification следующего tranche на frozen source bytes: Linux и Windows
по 276 PASS для NAV/portable consumers/runner; по 97 PASS для ranged sections,
NAV store/API/graph и status race. Единая Linux selection serving dependency
closure, production serving, shadow acceptance, runtime publication/update и narrow
installed wheel: 133 PASS, 0 skips. Actual modern validation/shadow: 6 Windows PASS.
PUBLIC/PRIVATE verifier: 36 PASS/7 historical tags unavailable, без blocking failures.
Это synthetic/local результаты, не реальные CORE stores и не новый hosted commit.
Installed wheel receipt: 2 023 504 bytes,
SHA `20ed52ca5222f8cbbd8b7c023e465c4650a4beb0c602ee57522b3b6b2816b9c6`;
noneditable isolated import/startup, native filesystem/model inference NOT_RUN.

Карточки остаточных production gates —
[PRODUCTION_DATA_NEXT_GATES](PRODUCTION_DATA_NEXT_GATES_RU.md).
Publication/restore contract —
[ACCOUNTING_PUBLICATION](../evidence/ACCOUNTING_PUBLICATION_RU.md),
partial NAV invalidation — [NAV_INCREMENTAL](../development/NAV_INCREMENTAL_RU.md).

1. Все три hosted Actions jobs Python 3.13.5 на публикуемом commit.
   Local Linux tests и fresh dependency closure обеих ОС уже проверены;
   Hosted Windows на 3031fd9 прошёл, local workstation privilege gap сохраняется;
   production receiver на Windows намеренно неподдержан. Corpus fixture исправлен
   без ослабления accounting. Все jobs на новом tip остаются обязательными.
   Настроить required checks только после успешного появления реальных jobs.
   Read-only наблюдение GitHub: PUBLIC main `protected=false`, rulesets пусты;
   detail protection endpoint возвращает `Resource not accessible by integration` (403).
   PRIVATE rulesets возвращают требование GitHub Pro. Права владельца репозитория не
   дают автоматически admin-доступ используемому connector. Настройки не изменялись.
2. Отдельный backup/restore drill вне production, включая unique originals, scripts,
   model/geometry outputs и environment/config definitions, которые нельзя получить
   из Git/канона. Ни наличие backup manifest, ни synthetic test не доказывают их полноту.
3. Квалифицировать реализованные native graph/search/pack/model/control adapters на
   реальных endpoints; связать operator runtime со всеми CORE units; подключить
   внешний rerank load-time identity producer. Упаковка узких HTTP services исправлена:
   устанавливается полный wheel, profile-scoped dependencies учитывают actual closure.
   Полный MCP smoke и post-switch/rollback
   receipt пока NOT_RUN. Generic health/PASS descriptor не заменяет native proof.
   GPU и неподдержанные desktop stages остаются BLOCKED. Serving закрывается при
   несовпадении identity/policy или неудачном rollback.
4. Проверить source-policy revocation на API, MCP и всех projections; production entry
   point обязан подключать generation guard к реально выбранным dependencies.
   Доступ через прямое чтение файлов projection не считается контролируемым API.
   Cache identity должна учитывать builder/rules/config/dependencies и policy generation;
   code-only исправление builder не должно переиспользовать старую projection незаметно.
   Риск cache identity из финального review исправлен: ключ учитывает builder/rule
   bytes, версии зависимостей, qualified producer identity и live source-policy SHA;
   повторный synthetic regression выполнен. Реальное revocation/deployment acceptance
   остаётся отдельным gate.
5. Квалифицировать journal/projection на аварийном завершении, восстановлении и целевом
   объёме. Существующий single-writer lock после crash требует доказательства завершения
   прежнего владельца и операторского recovery; автоматического force-unlock нет.
   Synthetic exception/ACK retry не доказывают power-loss durability файловой системы.
6. Независимый, source-family-disjoint gold и экспертная проверка всех научно используемых
   значений/формул/идентичностей/времени по оригиналам. Отдельно проверить подозрительные
   области и качество остальных extraction. 100% ledger accounting не является оценкой
   detector recall. Full corpus processing и expert review пока NOT_RUN.

## Эксплуатация и обратимость

Реализованный journal использует единственный publisher; review decisions сохраняются
в каноне, request_id связан с payload hash, base_revision защищает от конфликта.
ACK следует после записи partition/commit/HEAD. Это проверено синтетически, а не
полным power-loss/restore drill. Версии evidence references фиксируются точно;
исправление инвалидирует зависимые допуски без переписывания их истории. Полный
operational recovery должен восстанавливаться из этих записей и receipts; это пока gate.

Целевой served generation объединяет DOCUMENT, DATASET, EVIDENCE, mappings, rules,
policy и необходимые projections. Реализованы manifest/coordinator и local CPU
adapters. Наличие manifest само по себе не означает, что все эти stores подключены
или переключены. Поддерживаемые и заблокированные операции перечислены в
[UPDATE_RUNTIME_RUNBOOK](../corpus_platform/UPDATE_RUNTIME_RUNBOOK_RU.md).
Финальный production switch требует квалифицированных adapters: drain → shadow
acceptance → согласованная смена selectors → native observations → post-switch
acceptance. Сохраняются проверенные inputs старого поколения и весь rollback tuple.

Реализованный [durable switch](../corpus_platform/DURABLE_SWITCH_RUNBOOK_RU.md)
сохраняет intent до mutation, удерживает общий kernel gate до rebind всех receivers,
проверяет сохранённые native bindings и восстанавливает exact previous selectors.
Эти свойства проверены synthetic process/ASGI tests; actual CORE switch не выполнялся.

Code-only update не требует OCR/re-embedding, если проверены неизменность входов,
extraction/chunking/encoder/policy contracts и совместимость packs. Необходимость
NAV metadata migration/repack определяется compatibility report; старый manifest
сам по себе не является основанием rebuild. Документный graph wipe не должен
удалять evidence references. Remote delivery пока не считается квалифицированной.

## Definition of Done

Все входы учтены, структура и полная выдача проверены, научно используемые данные
проверены по оригиналу, primary origins/время/конфликты явны, исправления трассируются,
проекции согласованы, resume/rollback/restore доказаны. Неразрешённое и NOT_RUN
перечислены отдельно. Frozen/legacy/current originals не переписываются.

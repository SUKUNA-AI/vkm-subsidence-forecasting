# Незакрытая часть производственной программы данных

Основание: [план и фактический статус S01–S26](PRODUCTION_DATA_PROGRAM_2026-10-01_RU.md).
Этот документ описывает **незавершённую реализацию**, а не только отложенный запуск.
Схемы, CPU adapters и synthetic tests не означают завершения извлечения всего корпуса.
Закрывать пункты следует отдельными проверяемыми изменениями. Указанные оценки —
инженерные диапазоны, без стоимости обработки и проверки оригиналов.

## Обновление статуса реализации

R01: code integration готов — `vkm_datasets.catalogue/update` и dataset stages
единого update runtime; actual GIS intake/backup admission остаются NOT_RUN.
R02: frozen migration input/plan/private archive/map и owner-approved publish
реализованы в `vkm_evidence.migration`. После независимого review исправлен обход
source policy в смешанной unresolved атрибуции; Windows 52 synthetic PASS.
Реальные миграция и corpus dry-run не запускались.
R05: typed scientific use/transfer и исторический version-pinned read реализованы.
Отдельная target-kind matrix закрывает numeric admission для текстовых mentions;
GEOMETRY_INPUT без typed WorldSpec interpretation остаётся BLOCKED.
R06: общий graph bundle, actual native observers/factory, typed late issuer и
durable switch/recovery реализованы и проверяются локально. Actual endpoints,
external rerank identity producer, operator CLI wiring и production acceptance
остаются незакрытыми.
R03: actual prepare/layout/OCR/assemble/commit accounting реализован и проверен
целевыми тестами обеих ОС. Accounting sidecars пока не входят в publish/transfer,
admission/snapshot и CORE→EDGE backup set: это оставшаяся реализация, а не только
runtime gate. Независимая оценка detector recall остаётся NOT_RUN.
R04: typed source-backed semantic extraction adapter реализован; original spans,
model/tokenizer/config identities, budgets, private raw-response cache и закрытые
untrusted candidates проверены синтетически. Реальный endpoint с native identity,
его квалификация, corpus extraction и review остаются NOT_RUN.
R07/R08/R09: реальные unique backup/restore, independent gold/review и полная
кампания корпуса остаются NOT_RUN. Карточки ниже задают полный acceptance scope;
наличие этих модулей не означает закрытия карточки целиком.

## Общие правила выполнения

- Сохранять original bytes, прежние IDs, exact version references и frozen releases.
- Использовать существующие `vkm_corpus.update`, `vkm_datasets`, `vkm_evidence`:
  второй orchestration или конкурирующий scientific provenance layer не создавать.
- Ни UNKNOWN, ни структурный PASS не повышать в FACT или scientific READY.
- Runtime campaign отделять от разработки. Запуск разрешён только для exact commit,
  с pinned inputs/config/locks/policy, resource admission и проверенными gates.
- В receipt разделять выполненный код, qualification и actual corpus processing.
- Неподдерживаемые стадии оставлять BLOCKED; отсутствие результатов — NOT_RUN.
- Облачное чтение разрешено владельцем; access class и experimental role независимы.
  TARGET visibility не разрешает calibration по test/evaluator truth.

## R01 — Связать registry, datasets и единый update runtime

**Соответствие:** S05, S07, S09. **Приоритет:** до реального GIS intake.

**Цель и причина:** standalone manifest и GIS conversion уже реализованы, но ещё
не образуют полный logical dataset registry с атомарной регистрацией и resume.

**Подсистемы:** `src/vkm_datasets/manifest.py`, `gis.py`, `workbooks.py`,
`src/vkm_corpus/registry/`, `src/vkm_corpus/update/contracts.py`, `runtime.py`.

**Предусловия:** CI; source policy; independent original copy; qualification GDAL
для используемых форматов. Новые данные не нужны для разработки.

**Изменения:** immutable DATASET register связывает logical dataset, version hash,
полный набор members, source/work references, validity и licence. Отдельные
operator-selected operations inspect/convert/validate/publish выполняются через
update runtime; receipts bind request payload, member hashes и predecessor stage.
Canonical GPKG и workbook objects получают exact IDs без изменения оригиналов.
Без licence/CRS/primary origin статус остаётся явным, publication закрывается по
необходимым для конкретного применения требованиям.

**Не менять:** SOURCE_REGISTER не превращать в реестр одиночных GIS companion
files; никакого intermediate GeoJSON, guessed CRS, geometry repair или overwrite.

**Проверки:** TAB/DAT/MAP/ID/IND, MIF/MID, XLS/XLSX, sparse FIDs, 257 columns,
кириллица, hidden sheets, formula/cached values. **Negative:** missing companion,
изменённый member, duplicate key, unknown CRS, request-ID collision, interrupted
conversion/publication; потерянный ACK не создаёт вторую версию.

**Acceptance:** dataset проходит plan→inspect→convert→validate→resume в одном
campaign; SOURCE↔DATASET references проверяются; registration не появляется раньше
durable outputs и проверенной копии оригиналов.

**Rollback:** previous dataset selector; новая staging version сохраняется.
**Зависимости:** S06,S08,S09. **Оценка:** L, 8–16 ч; CPU, qualified GDAL/QGIS runtime.

## R02 — Миграция действующего evidence с точными locators

**Соответствие:** S15, S22, S23. **Приоритет:** до scientific evidence publication.

**Цель и причина:** новые contracts/journal не содержат автоматически прежний
Phase1 evidence; пустой journal нельзя выдавать за полную базу.

**Подсистемы:** PRIVATE `11_evidence_vnext/canonical/`, `vkm_evidence.objects`,
`journal`, `inventory`, existing public catalogue builders.

**Предусловия:** frozen migration input manifest; точные SOURCE/DOCUMENT versions;
разрешение на чтение PRIVATE уже дано, но содержимое не публикуется в PUBLIC.

**Изменения:** dry-run migration map old IDs→new version-pinned records; preserving
UNKNOWN/scope/scale/status/source quotes PRIVATE-only. Каждая unsupported binding
становится UNRESOLVED с причиной. Publish batch идемпотентен и использует reviewer
authority только для исторически доказанных решений. Legacy catalogue hash и
original locator сохраняются. Retired evidence не возвращается в current.

**Не менять:** старые IDs/архивные записи; не создавать synthetic scientific records
ради зелёного smoke; не повторно выдавать автоматическую extraction за review.

**Проверки:** duplicate IDs, missing original/version, shared primary data,
ambiguous locators, repeated migration, public quote leakage. **Negative:** ложное
semantic review, dangling references, mismatched quote locator, source revocation.

**Acceptance:** все входные строки имеют migrated или unresolved disposition;
научные статусы не повышены; полный old↔new map и receipt восстановимы.
**Rollback:** previous evidence HEAD; migration outputs сохраняются.
**Зависимости:** R01 для dataset evidence, S06,S15,S22. **Оценка:** L, 8–16 ч плюс review;
CPU, PRIVATE, reviewer.

## R03 — Подключить полный object accounting к extraction

**Соответствие:** S16–S19. **Приоритет:** до full corpus campaign.

**Цель и причина:** ledger и native fidelity есть; ещё отсутствует сквозной учёт
всех detection/extraction branches и независимый denominator пропусков.

**Подсистемы:** pipeline prepare/layout/assemble, extractors, OCR normalize,
`vkm_evidence.coverage`, document native artifacts, NAV tables/formulas.

**Предусловия:** exact producer; bounded processing plan; native/OCR gold отдельно.

**Изменения:** receipts всех input units/candidates/attempts/output versions;
confidence/NMS/token/size/budget suppressions не исчезают. Сохранить сложные native
objects, reading order, continued tables, header/cell lineage, figure panels/axes,
formula alternatives и context. Unsupported objects имеют original representation
и disposition. Re-extraction mapping: SAME_OCCURRENCE/SPLIT/MERGED/REMOVED/AMBIGUOUS.

**Не менять:** originals; исходную запись числа/формулы; не выдумывать bbox native
XML; не схлопывать blanks/zero/unreadable/not-applicable; не заявлять detector recall
из counts зарегистрированных кандидатов.

**Проверки:** nested tables, multicolumn text, continued header, hidden native
glyphs, formula inside cell, missing OCR strip, full-page image, deleted output.
**Negative:** borrowed attempt, duplicate page, suppression без причины, output
из другой версии, rebind старого evidence после re-extraction.

**Acceptance:** каждый expected unit и найденный candidate имеет disposition;
отсутствующий output не улучшает completeness; original locators доступны; каждый
добавленный supported format квалифицирован отдельно.
**Rollback:** prior extraction generation; оба lineage maps сохраняются.
**Зависимости:** S08,S15,S16,R02. **Оценка:** XL, 24–48 ч плюс approved processing;
CPU/OCR/GPU по отдельному workload manifest.

## R04 — Source-backed claims, entity linking и observation origins

**Соответствие:** S19–S20. **Приоритет:** до claims/timeline scientific use.

**Цель и причина:** typed records и validation не извлекают семантику всех работ
сами; необходимо исполнение и контроль ложных объединений.

**Подсистемы:** `vkm_evidence.contracts`, `objects`, `validation`, source/work
families, table/formula context, model extraction adapters.

**Предусловия:** R02–R03; independent gold; разрешённый bounded model profile.

**Изменения:** occurrence-backed mentions, negated/conditional claims, observation
sets и subsets, formula bindings/conditions, support/conflict/reprint relations;
candidate links отделить от reviewed merge/split decisions. Сохранять raw model
response с model/config/input hash. Повторное использование одной съёмки не
увеличивает independent observation count. Entity join не основан на одном номере.

**Не менять:** не присваивать SKRU-1, units, material scale или independence по
умолчанию; не выполнять формулы/solver; не называть similarity causality.

**Проверки:** разные рудники с одинаковым номером, переиздание одной работы,
partially overlapping surveys, разные значения символа, contradictory readings.
**Negative:** fabricated support, cyclic derivation, guessed entity identity,
claim не поддержан original, модельные инструкции внутри документа.

**Acceptance:** claims имеют проверяемые original supports; unresolved links
видимы; computational DAG ацикличен; citation cycles разрешены; unique observations
и publications считаются отдельно.
**Rollback:** отзыв решений новыми journal entries, previous generation.
**Зависимости:** R02,R03,R08. **Оценка:** XL, 24–48 ч плюс processing/review;
CPU/model jobs/reviewer.

## R05 — Полная история знаний и контекст scientific admission

**Соответствие:** S21–S22. **Приоритет:** до temporal scientific use.

**Цель и причина:** exact historical revision, availability/recorded-at срез и typed
site/scale/use/transfer уже реализованы; необходимо квалифицировать их на реальных
versions и scientific-use inputs. Данные старых admissions нельзя принять без
новых обязательных bindings.

**Подсистемы:** `vkm_evidence.temporal`, `query`, `validation`, `journal`,
`vkm_world.chronology`, existing scale/site/transfer validators.

**Предусловия:** R02,R04; version-pinned interpretation and review records.

**Изменения:** bitemporal event/knowledge views по exact evidence revision;
planned/actual отдельно; publication version, data state, availability и recorded
time не подменяют друг друга. Admission получает typed use context и вызывает
existing WorldSpec site/scale/transfer gates. Reverse dependency invalidation
применяется к quantity/formula/date/entity/origin/policy corrections.

**Не менять:** `known_at` и точность исходной даты; свободный purpose не разрешает
LAB→MASSIF; acquisition time не становится information availability.

**Проверки:** early measurement/late publication, year-vs-day, uncertain intervals,
backdated ingestion, date correction, revised entity, target role. **Negative:**
future-dependent claim, old admission после correction, unreviewed transfer,
unknown availability, смешение historical revision и current source policy.

**Acceptance:** revision+t0+context воспроизводят тот же срез; stale admission
закрыт; спорные даты сохраняются; исходный и требуемый site/scale проверяются явно.
**Rollback:** older views/readers, historical decisions неизменны.
**Зависимости:** R02,R04. **Оценка:** L–XL, 16–32 ч; CPU, scientific reviewer.

## R06 — Реальные remote adapters и согласованный production switch

**Соответствие:** S09–S10,S12,S24. **Приоритет:** до обновления production.

**Цель и причина:** native graph/search/pack/model/control observers, общая graph
projection, typed late issuer и durable coordinator реализованы. Не завершены
operator CLI wiring всех реальных units, внешний rerank identity producer и live
qualification; эти code gaps отделены от runtime gates.

**Подсистемы:** `vkm_corpus.update.runtime`, `generation`, API `production`,
Neo4j/OpenSearch loaders, dense/late/visual/rerank/control services, NAV manifests.

**Предусловия:** CI; trusted acceptance registrar; real backup/restore; exact clean
checkout; отдельные shadow resources и operator configuration вне Git.

**Изменения:** fixed typed adapters observe/prepare/accept/switch/restore;
не shell/SQL из manifest. Native selected-store metadata и immutable content proofs
проверяются независимо от expected identity. Prepared projections загружаются в
shadow stores. Полный MCP-49 acceptance bound code/locks/component/policy/config.
Service drain и short maintenance window перед coordinated selectors switch;
failed rollback оставляет MAINTENANCE. Evidence-referenced document versions не
удаляются DOCUMENT wipe. Topics refresh только если dependency diff требует.

**Не менять:** не rebuild/re-embed только из-за serving code update; не разрешать
generic PASS receipt; не пропускать active store из observations; никакого merge
или deploy при непрошедшем gate.

**Проверки:** mixed generations, stale pack, omitted service, byte mutation,
interrupted shadow load, failed switch, ACK loss, failed restore, policy revocation.
**Acceptance:** real shadow acceptance/restore receipts; actual selected artifacts
совпадают; previous generation восстанавливается либо service остаётся закрытым.
**Rollback:** единый предыдущий selector set и runtime configuration.
**Зависимости:** R01,R02,R07,S03,S04,S08. **Оценка:** XL, 24–48 ч плюс runtime;
CPU heavy, CORE/EDGE, deployment window.

## R07 — Реальная сохранность уникальных данных и operation recovery

**Соответствие:** S06,S13,S14. **Приоритет:** до intake и irreversible processing.

**Цель и причина:** file-only inventory/verify и synthetic restore есть; уникальные
work/WSL/config/model outputs ещё не инвентаризированы и не восстановлены.

**Подсистемы:** `infra/recovery/unique_inventory.py`, CORE→EDGE backups, ops queue,
review journal, environment definitions, server status.

**Предусловия:** operator manifest actual unique files и explicit budget;
independent storage domain; определить destination без cleanup существующих данных.

**Изменения:** canonical/reproducible/unique classification; fresh hashes в двух
проходах; independent copy verification; real restore drill в отдельном root.
Ops leases/heartbeat/retry восстановимы; unique review decisions идут в canon,
не только PostgreSQL notes. Server status показывает coverage/debt/generations/
backup и честные exit statuses. Канал уведомлений пока не выбирать.

**Не менять:** массово не копировать весь work одинаково; secrets/config contents
не публиковать; existing backups и unique files не удалять.

**Проверки:** modified same-size original, missing file/copy, same-storage fake copy,
lost canonical ACK, crashed worker, stale lease, restart, loss of ops database.
**Acceptance:** UNBACKED_UNIQUE перечислен с причинами; реальный restore проверен;
потеря ops не уничтожает reviews; absence backup не выглядит зелёным.
**Rollback:** новые backup artifacts сохраняются; прежняя production схема доступна.
**Зависимости:** S06,S08,S22. **Оценка:** L, 8–16 ч плюс I/O; CPU, workstation,
WSL, CORE/EDGE, storage destination decision.

## R08 — Независимый gold, original-source review и квалификация

**Соответствие:** S22–S23,S25. **Приоритет:** до S26 и научного допуска.

**Цель и причина:** quality registrar и bounded source-backed review packets есть;
independent real gold, review workflow и capacity/durability пока NOT_RUN.

**Подсистемы:** `vkm_evidence.qualification`, `review_packet`, API/MCP, narrow review
interface, original artifact/CAS access, load/recovery qualification harness.

**Предусловия:** gold owner и independent reviewer; thresholds зафиксированы до
настройки; approved plan byte hash; storage/runtime profile.

**Изменения:** stratified original-backed gold отдельно для discovery/numbers/grid/
formula/entity/time; exclude tuning overlap. Reviewer видит оригинал, headers,
notes и context рядом с extraction; navigation by locators/conflicts/dependencies.
Publish review через limited idempotent interface; durable canon before ACK.
8-reader load, memory limits, power-loss/restart и restore проверить отдельно.

**Не менять:** synthetic gold не квалифицирует реальный корпус; agreement моделей
не заменяет original review; не исправлять gold под prediction.

**Проверки:** exact numeric/header/unit/sign mistakes, unseen object, false entity
merge, wrong formula binding, stale cursor, changed policy, interrupted export.
**Acceptance:** scope/strata и metric limits preregistered; uncertainty/counts
опубликованы; все выбранные scientific inputs и подозрительные места reviewed;
остальные extraction проходят независимый контроль; unprocessed не PASS.
**Rollback:** previous qualifier/admission versions; решения остаются в журнале.
**Зависимости:** R02–R05,R07. **Оценка:** XL, 16–32 ч плюс independent review;
CPU/Windows/CORE/EDGE, human scientific decision.

## R09 — Выполнить полную кампанию и приёмку реальных GIS

**Соответствие:** S26. **Приоритет:** после необходимых production gates.

**Цель и причина:** исполняемые primitives не означают обработки всего корпуса.

**Подсистемы:** единственный update cycle, approved extraction/model profiles,
review queues, canonical/projected generations, server status.

**Предусловия:** R01–R08 в требуемом маршруте; exact corpus manifest и resource
budget; independent originals copy; qualified runtime. Новые данные 07.10 идут в
новое поколение и получают отдельную format/provenance qualification.

**Изменения:** compute affected-only plan; reuse valid raw responses/outputs;
run missing stages ограниченными batches; reconcile counts/hashes/coverage;
repair systematic defects новыми версиями; source-original scientific review;
publish coherent generation после full acceptance, не по факту окончания worker.

**Не менять:** retired/frozen evidence; старый canonical generation; UNKNOWN;
не включать solver/ML; scientific unresolved не исключать скрытно из знаменателя.

**Проверки:** stop/resume, repeated unchanged plan, duplicate delivery, low disk/
memory, missing page/output, policy change, dirty producer, incomplete store load.
**Acceptance:** каждый active source имеет completeness passport; all scientific
use inputs reviewed/admitted; full rows/formulas доступны; origins и timelines
трассируются до оригинала; exclusions/unresolved перечислены; recovery доказан.
**Rollback:** previous coherent generation; новые outputs остаются в staging.
**Зависимости:** все обязательные gates маршрута. **Оценка:** XL, 8–16 ч сопровождения
плюс отдельно измеренные processing/review; CPU/GPU/CORE/EDGE по approved manifest.

## Очерёдность и настоящие внешние зависимости

```text
CI + exact producer ───┬─ R01 dataset/update ──┐
                      ├─ R02 migration ──────┤
                      └─ R03 accounting ─────┼─ R04 origins/claims ─ R05 time/use
R07 real recovery ───────────────────────────┘                    │
R08 independent gold/review ──────────────────────────────────────┤
R06 shadow stores/acceptance/switch ───────────────────────────────┤
                                                                ↓
                                                        R09 full campaign
```

R01/R02/R03 можно разрабатывать параллельно; R08 preregistration начинается до
настройки extraction. R06 и R07 идут параллельно семантической программе.
Новые GIS нужны для квалификации их конкретных originals, а не для разработки
adapters. Решения владельца требуются для independent gold/reviewer, threshold
profile, actual unique backup destination и runtime budget/deployment window.
Ни один из этих gates не закрывается ожиданием или synthetic PASS.

# Ночной hardening ВКМ / СКРУ-1 — 01.10.2026

Исполнительный проход: исходные гипотезы Astra перепроверены, подтверждённые дефекты исправлены.
P0, минимальный P1 и отдельный PRIVATE P2 реализованы локальными тематическими коммитами.
PUBLIC: 2218 passed, 0 failed; полный доступный offline gate — `PASS_WITH_NOT_RUN`.
PRIVATE engineering patch: 19 passed. Оба push заблокированы HTTP 401; main не изменён.
GitHub Actions: `WORKFLOW_CONFIGURED / EXECUTION_NOT_RUN`, run ID отсутствует.

`READY` нового admission означает **CONTRACT_CONSISTENCY_ONLY** относительно явно предоставленных
review-входов. Физическая истинность, готовность решателя и качество прогноза не установлены.
Никаких ML/solver/GPU/intake/новой оцифровки, чтения restricted binaries или real sealed labels.

## BASELINE

| Проверка | Фактически получено до изменения кода |
| --- | --- |
| PUBLIC initial HEAD | `cf0d1c872024bf0093cf226a291774252652e2ed` |
| PRIVATE initial HEAD | `e2a62fcc59af50b2f7ddd88648c531b4de5a38ea` |
| Расхождение с Astra | Нет; финальная проверка remote main также дала эти HEAD |
| Рабочая ветка в обоих repo | `gpt/night-hardening-2026-10-01`; отдельные branches, без merge/force push |
| Starting status | PUBLIC clean; PRIVATE engineering sparse checkout clean |
| Python | Проверки: 3.13.5; системный Python: 3.12.14 |
| Закреплённые зависимости | `requirements/worldspec.lock.txt`, существующие corpus/corpus-services locks; без обновления до latest |
| Offline collection | 1801 collected, 1776 selected, 25 deselected; 3 module skips; 0 collection errors |
| Offline execution | 1728 passed, 51 skipped **включая** 3 module skips, 25 deselected; 0 failed/errors |
| PUBLIC canonical verifier | `PASS_WITH_NONBLOCKING`: 30 PASS, 3 PRIVATE-dependent SKIPPED, 7 unavailable tag anchors |
| Catalogue comparison | 2 PASS, 0 найденных quote-boundary нарушений; это hash comparison, не fresh rebuild |
| Package smoke | Build, non-editable install, imports 8 packaged roots вне checkout, hashes 14 resources — PASS |
| GitHub Actions в PUBLIC | Workflow отсутствовал; ночной run отсутствует |

Baseline skips: отсутствовали tokenizers, cv2, snowballstemmer, shapely, scipy, matplotlib,
pymorphy3; также DjVuLibre, rsync/host prerequisites, GPU и PyQGIS runtime.
Подтверждённые существующими freeze/receipt дополнительные CPU pins внесены в
`requirements/offline-test.lock.txt`; их основания перечислены прямо в файле.
pytest 9.1.1, PyYAML 6.0.3 и setuptools 78.1.0 использованы в закреплённых версиях проекта.

Прочитаны оба AGENTS.md, приложенный основной prompt и действующие governance contracts.
Шесть независимых направлений A–E/review использовали изолированные деревья; интеграция и commits
принадлежат координатору. P0 и P1 прошли отдельные read-only reviews; найденные MUST исправлены
до продолжения. Финальный P1 review: 349 targeted passed, подтверждённых MUST/SHOULD не осталось.
PRIVATE P2 независимо проверен после PUBLIC correctness work.

## FINDINGS F1–F12

Все строки ниже проверены по исходному HEAD; номера Astra сохранены. «Да» означает воспроизведение
observable дефекта или отсутствующего обязательного guard, а не принятие аудита на веру.

| ID / гипотеза Astra | Reproduced / точная причина | Изменение | Regression evidence |
| --- | --- | --- | --- |
| F1 race freeze/ledger | Да: exists + replace позволяли двум writers публиковать разные candidates; terminal read/write не были общей межпроцессной транзакцией | Hard-link publish-if-absent; identical idempotent, different conflict; fsync staging, persistent inode lock terminal transition | `test_concurrent_freeze_is_publish_once`, `test_concurrent_finalization_cannot_overwrite_terminal_status`, crashed-finalizer и unsupported-filesystem negatives; multiprocessing с timeout |
| F2 incomplete self-hashed candidate | Да: self-hash не удостоверял полноту/current artifact identity; ещё обнаружена caller-mutation во время freeze | Закрытая проверка полного candidate; frozen target, current dataset/evaluation/environment/contracts/manifest/artifact files; собственный snapshot и повторная проверка сериализованных bytes | `test_self_rehashed_record_cannot_omit_required_field`, current/stale/missing artifact tests, `test_freeze_snapshots_nested_caller_mappings_before_validation` |
| F3 nested PUBLIC receipt | Да: открытая вложенная структура и copied assertions; текущий QA не был обязательным достаточным основанием | Рекурсивная closed projection полей/типов/counts/statuses; bool≠int; NaN/Inf/NOT_RUN/stale QA отвергаются; raw-frozen claim вычисляется из проверенных hashes | `test_unknown_nested_fields_cannot_be_published`, `test_count_requires_nonnegative_integer`, `test_publication_gate_preserves_previous_output_on_failure`, parse/hash и publication-boundary races |
| F4 missing catalogue input | Да: обязательный missing input мог оставить старый output при успешном exit/частичном обновлении | Полный preflight всех входов, schema/leakage check, publication lock, backup/rollback; ненулевой exit до публикации | `test_catalogue_validation_failure_has_no_partial_publication`, replace failure/rollback failure/concurrency tests |
| F5 truncated manifest | Да: проверялись только перечисленные entries; пустой/truncated map скрывал пропуски | Bijection authoritative map↔entries, unique paths, required OK/digests, actual PUBLIC hashes; manifest gate без PRIVATE; документация hash comparison | `test_manifest_requires_complete_bijection_and_ok_status`, `test_manifest_public_check_runs_without_private`, omitted-source quote tests |
| F6 WorldSpec references | Да: incomplete namespace/duplicate/nested closure, особенно при пустом registry | Exact separate ID namespaces, explicit registered_laws, global nested duplicate checks, nested source/evidence/provenance/spatial/event refs; empty DESIGN остаётся допустим | `test_explicit_references_fail_even_with_empty_registry`, `test_law_references_resolve_exact_identifiers_without_namespace_conversion`, nested provenance tests |
| F7 non-finite / stale validation | Да: NaN/Inf и mutation после создания модели могли миновать validation result | Finite numeric models; revalidation на validate/serialize/hash; admission привязан к actual owned input hashes, без глобального frozen rewrite | `test_nonfinite_quantities_are_rejected`, uncertainty/transform tests, `test_validation_and_serialization_revalidate_mutated_models`, stale admission/DRAFT snapshots |
| F8 Transfer source side | Да: incomplete/wrong-source Transfer мог разрешить изменение контекста; root probe дал два unsafe accepts | Общий use guard проверяет все четыре actual axes, method/rationale/status; SCI consumer проверяет family/source/context; хранение incomplete Transfer сохранено | LAB→MASSIF, ANALOGUE→SKRU1, wrong-source/target, source-context positive/negative tests |
| F9 creep/law parameter compatibility | Да: archival unit bypass не имел law-specific consumption gate; имена коэффициентов не определяли форму/семью | Узкий `ScientificUseBinding` с существующими references/hashes и явным ReviewIndex; два exact-form versioned profiles; DRAFT/CLI consumers | 135 новых SCI/DRAFT случаев падали до появления entry points; существующие use guards отдельно: 2 FAIL до, 20 PASS после; e2e positive и negative units/family/UNKNOWN/time/overlap tests |
| F10 dossier gaps | Да: linked record presence скрывала scientific gap независимо от LAB/UNKNOWN/local/verification | Linked/relevant диагностируются отдельно; applicability/binding NOT_CHECKED, field coverage NOT_ESTABLISHED; adjacency не принимает scientific decision | `tests/corpus/test_projection_qualifications.py`, topic/API regression; 9 root observable regressions FAIL до patch |
| F11 graph/snapshot identity | Да: compact constraints терялись; reuse мог считаться current без manifest/digest | Causal constraints/full-record refs/navigation_only; actual Arrow snapshot, manifest+Parquet+pack identity, capabilities; missing identity=ad-hoc/unverified, Neo4j same ID без digest=NOT_CHECKED | Manifest/stale bytes/subset/pack/merge negatives; 3 дополнительных root Neo4j identity tests FAIL до, PASS после |
| F12 non-finite checks | Да: root probe получил 3 unsafe PASS; tolerances/IEEE arithmetic, bool/int, pointer/legacy differences | Finite expected/actual/tolerances, exact mixed numeric comparison, safe malformed-spec failure; `vkm_ansys.checks` сохранён как compatibility adapter | Новые 115 случаев: 87 FAIL / 28 PASS до, 115 PASS после; broader engineering 158 PASS |

До/после основные root batches: P0-A 52 FAIL/2 PASS → world 263 PASS; snapshot supplement 3 FAIL →266;
P0-B 25 FAIL/2 PASS → focused 36 PASS, world+ABC302; P0-C32 FAIL/6 PASS →340;
P0-D30 FAIL/1 PASS →world335. P0 gate: world354 + corpus1554 PASS, с явными NOT_RUN.
Независимый publisher review выявил дополнительное расширение старого фрагмента: root3 FAIL до
prefix-bound fix; затем 305 независимых synthetic probes, 0 нарушений и 104 targeted PASS.
Отсутствие нового P1 entry point считается доказательством отсутствующего consumer, а не 135 отдельными
ошибками физики. Промежуточные проходы не заменяют финальный полный прогон ниже.

## CHANGES / batches и commits

SHA ниже относятся к локальной рабочей ветке. Каждый code commit предварён relevant tests,
`git diff --check` и проверкой staged pathspec. Случайных generated/private files в PUBLIC нет.

| Batch / commits | Главные затронутые файлы и минимальный контракт |
| --- | --- |
| P0-A `dab05a3`, `1bccca3` | `validation/access.py`, `test_access_hardening.py`, validation policy/старый synthetic fixture: immutable publish и authorization; mutable core writer сохранён |
| P0-B `db40236` | ABC `public_receipt.py`, `governance/publication.py`, leakage helper, synthetic tests/README: closed receipt и публикация проверенных bytes |
| P0-C `a5da4b7`, `37cb89e` | Catalogue builder/verifier, report publisher, publication tests, path policy: обязательная полнота, batch preflight, quote boundary не расширяется |
| P0-D `6e21a6f` | WorldSpec model/io/schema, core numeric config, materials, integrity tests: nested typed closure и конечные числа |
| P0-E `e94bd85` | Actions, общий offline runner, backed lock, runner tests/markers и prerequisite PDF test: discovery всех suites и честный NOT_RUN |
| P1-A `194b512` | `validation/scientific.py`, `validation/draft.py`, shared Transfer guard, CLI, SCI tests/doc: выбранный law/parameter/context gate с реальными consumers |
| P1-B `9ef2728` | Topic/API service, NAV manifest/CLI/store/figure-series, synthetic fixtures/tests/doc: constraints, navigation-only и content snapshot identity |
| P1-C `aa3871b` | Jobs spec/checks, retained Ansys adapter, engineering contract tests/compatibility doc: finite checks без удаления API |
| P2 docs `82dd900` | README/PROJECT_STATE: текущая пауза geometry/ML/solver, hash comparison и фактический смысл READY; D-18/D-19 не изменены |
| PRIVATE P2 `db93e5450a83cbc36b486d24c73b03b620ab9a5c` | Только 6 `.github` paths: 3 workflows, один stdlib planner, synthetic tests/README; manual default dry-run, false fail-closed, никакого intake/source mutation/push |

Для review: `git show <SHA> --stat`, `git diff <initial-HEAD>..gpt/night-hardening-2026-10-01`.
PUBLIC code HEAD полного прогона: `aa3871b85926da5e6c9f2cbe6393d418bda60064`;
следующий `82dd900490fbb5c0683eb195c5772f3026aa707b` меняет только две документации.
Этот отчёт — отдельный docs commit после checks, его SHA доступен в Git history.

| Независимый batch | Риск / rollback | Критерий DONE |
| --- | --- | --- |
| P0-A | Hardlinks/locks зависят от FS; unsupported FS fail-closed. Revert обоих commits только вместе; frozen records не переписывать | Different concurrent freeze конфликтует, identical идемпотентен, ledger terminal once, stale/current identity tests зелёные |
| P0-B/C | Строгий publisher намеренно блокирует старые неполные входы; multiple readers не получают глобальную FS transaction. Revert соответствующего publisher commit после сохранения outputs/backups, без удаления evidence | Missing/malicious/stale inputs не публикуются; полный map/manifest, rollback regressions и leakage checks пройдены |
| P0-D | Строже archival refs, но empty DESIGN/UNKNOWN сохранены. Revert model/schema совместно | Все typed refs, duplicates и non-finite negatives PASS, schema consistency PASS |
| P0-E | CPU dependency gaps явно NOT_RUN; CI ещё не выполнен на GitHub. Revert runner/workflow вместе | Два локальных jobs воспроизводимы, discovery/strict markers/no fake success; hosted run нужен до merge |
| P1-A | ReviewIndex доверенный input; только два точных profiles. Revert P1-A отключает новый admission/DRAFT consumer, не меняет каталоги | Positive e2e и отрицательные family/units/UNKNOWN/Transfer/time/overlap/stale-hash tests PASS |
| P1-B | Богаче API payload/новая rule_version; consumers должны читать qualifications. Revert NAV/API commit вместе | Linked≠applicable; missing digest не current; full metadata/pack identity negative tests PASS |
| P1-C | Две исторические text/pointer semantics сохранены явно. Revert shared checks+adapter вместе | Finite/precision/malformed checks и legacy compatibility tests PASS |
| P2 PRIVATE | Hosted planner не реализует intake; runner/Python host floating. Revert private commit восстановит старый опасный auto-write; такой rollback требует отдельного review | 19 synthetic tests и независимые read/write/ownership probes PASS; только .github changes, источники неизменны |

## SCIENTIFIC ADMISSION / threat model

Минимальная цепочка реализована:
`MathModelRecord → exact adapter/form/revision/hash → ScientificUseBinding → reviewed family/context → admission → DRAFT`.
Binding хранит references существующих MaterialParameter и evidence IDs, а не вторую копию значений.
ReviewIndex — явный узкий reviewed input, не новый научный каталог или ontology.

| Возможная ложная готовность | Что фактически проверяется сейчас | Предел проверки |
| --- | --- | --- |
| Law family вместо exact formula | Два exact equation/variables/version contracts, revision/source/hash, отсутствие conflicting_forms | Неподдержанная форма остаётся UNDETERMINED; формула не исполняется |
| Коэффициенты из разных опытов | Один reviewed family, material/method/conditions/source axes, exact parameter hashes, EV+locator в собственном provenance каждого quantity | Авторитетность/физическая корректность review не выводится из hashes |
| Символ или generic unit вместо размерности | A в stress-power законе имеет law-specific `Pa^-n/s`; normalized B и stress0 имеют другой contract; finite values и известные conversion factors | Произвольные units, affine Celsius и неразрешённая ambiguity не угадываются |
| UNKNOWN conditions / поздняя информация | Обязательные stress/temperature/duration и реально потребляемые TemporalSupport/metadata доступны к origin; recursive consumed provenance | Нет генерации temperature/ranges/time/default values; несвязанные archival UNKNOWN можно хранить |
| LAB автоматически MASSIF; ANALOGUE автоматически SKRU1 | Actual source/target scale/scope, полный applicable declared Transfer, explicit non-FACT scenario status | Transfer contract не удостоверяет физическую достоверность переноса; local pillar не field-wide FACT |
| Старая READY после mutation | Owned input snapshots и content hashes; `receipt_matches_current` пересчитывает actual result | Это identity/consistency guard, не глобальная неизменяемость всего WorldSpec |
| Graph adjacency вместо applicability | Navigation-only projections; linked/candidate diagnostics отдельно от NOT_CHECKED applicability/binding | CITES≠AGREES_WITH; NEAR_FORMULA≠PARAMETER_OF_FORMULA; SAME_SOURCE≠COMPATIBLE_SET |
| DRAFT объявлен benchmark-ready | Missing declarations=NOT_READY; incompatibility=BLOCKED; complete=READY_DRAFT при status DRAFT | Только SYNTHETIC metadata; REAL blocked, preregistration/computation отсутствуют |

DRAFT использует существующие ObservationDataset, splits и availability/leakage guards.
Metric declarations перечислены и hash-bound; scoring и test-access evaluation не выполняются.
Проверяются question/id/version, t0/horizon/availability rule, scientific binding identity, calibration/NROY/filter/
validation/test roles, independence/sample/source-lineage overlap, declared acceptance limits и actual public code/
metadata hashes. LOLO тоже требует forward-only labels; spatial grouping само по себе не разрешает forecasting.
UNKNOWN availability не считается available. Dataset value files и sealed labels не читаются.
Полный synthetic positive e2e существует; unknown/mixed-family/bad units/stale receipt/overlap/future labels — negatives.

Новый admission имеет два actual consumers: DRAFT checker и safe aggregate CLI
`scripts/check_scientific_use.py`. Он **не подключает paused solver/ML к расчётам** и не утверждает,
что все старые engineering APIs автоматически стали SolverReadyRepresentation.
Физическая истинность, scientific transfer validation, calibration/NROY/forecast execution, independent field
validation, реальные dataset bytes и complete solver-ready state здесь НЕ ПРОВЕРЕНЫ.
Подробный contract и runnable examples: [SCIENTIFIC_ADMISSION_DRAFT_RU.md](../worldspec/SCIENTIFIC_ADMISSION_DRAFT_RU.md).

## Какие проверки создавали впечатление научной строгости

Schema/finite/unique IDs/разрешённые refs проверяют shape и closure. Наличие locator, source hash,
verbatim/QA или неизменного frozen Git object удостоверяет identity/transcription/provenance, а не применимость закона.
Catalogue count/manifest/hash comparison проверяет полноту опубликованного набора и bytes, а не полноту знания.
Generic unit check и символ коэффициента без exact-form binding не доказывают совместимость constitutive набора.
Transfer axes/method/rationale/status — декларация переноса, а не экспериментальная валидация переноса.
GRAPH_READY, snapshot ID, CITES/NEAR_FORMULA/SAME_SOURCE и dossier coverage — операционные/navigation сведения.
Successful LOO transform, numerical tolerance или execution success не равны scientific acceptance.
После исправления эти границы остаются явными; даже новый READY не является физическим PASS.

## LIFECYCLE / происхождение объёма кода

| Область | Current classification / почему не удалена |
| --- | --- |
| WorldSpec/validation/publishers | CURRENT engineering contracts; structural и selected-use gates различаются |
| vkm_corpus | ACTIVE CLI/API/MCP и offline CPU paths; deployed services отдельно NOT_RUN |
| CAD/QGIS/drawio/jobs | CURRENT CPU libraries/engineering APIs; desktop/runtime/solver prerequisites отдельно paused или NOT_RUN |
| vkm_ansys/MATLAB/OGS | PAUSED computations, публичные/исторические contracts retained; отсутствие internal caller не доказывает отсутствие external consumers |
| Retired PhysicalWorld releases | LEGACY/FROZEN/history/tombstones; frozen anchors/bytes и retirement tests retained |
| Evidence builders | Build-time consumers/provenance; отсутствие runtime import не делает их dead |
| Proven dead local | Только `all_ids` ниже; оснований для массового удаления подсистем нет |
| Duplicated tests | AST scan: 1361 functions, 0 identical groups по полной AST с names/args/decorators; semantic overlap не исключён и не является причиной удаления |

Происхождение оценено по first-parent merge diff/numstat, только Git metadata. Категории по путям:
code=src/scripts, tests=tests, docs=Markdown, остальное=data/assets/receipts и другие пути.
Added lines не являются уникальными semantic LOC или оценкой авторства; binary entries не имеют line count.

| Repo / крупный merge | Added total | Code | Tests | Docs | Other |
| --- | ---: | ---: | ---: | ---: | ---: |
| PUBLIC PR#5 `02727ae` | 98033 | 45630 | 10119 | 15963 | 26321 |
| PUBLIC PR#6 `4bafe90` | 97358 | 26469 | 7842 | 9592 | 53455 |
| PUBLIC PR#7 `99e2d64` | 113894 | 16517 | 7087 | 4933 | 85357 |
| PUBLIC reset `40ca0ed` | 85575 | 4690 | 1243 | 11021 | 68621 |
| PUBLIC PR#8 `a8266c6` | 13922 | 1263 | 653 | 713 | 11293 |
| PUBLIC OGS merge `cf0d1c8` | 31017 | 0 | 0 | 309 | 30708 |
| PUBLIC ABC merge `24d6986` | 4942 | 0 | 294 | 201 | 4447 |
| PRIVATE PR#1 `07838a1` | 204837 | 25685 | 0 | 16251 | 162901 |
| PRIVATE PR#3 `dbe810e` | 6714 | 2061 | 0 | 582 | 4071 |
| PRIVATE PR#6 `af4da03` | 1547 | 877 | 0 | 148 | 522 |

Основной PUBLIC code influx — PR#5/#6/#7; основной PRIVATE influx — PR#1, преимущественно other/assets.
Крупный размер последнего OGS merge преимущественно receipts/assets, не новый runtime code.
Reset одновременно удалял значительный старый объём; additions нельзя трактовать как net expansion.
История preserved; никакие protected objects или references не переписаны.

## DELETIONS / test migration

`worldspec/model.py::all_ids`: baseline Git blob `347179510cfda4864f06802bbfada8b6e3de4cfe`,
два AST Store, ноль Load; локальная переменная, не API, нет closure/reflection consumer.
Удалена после замены duplicate validation на actual nested Counter-based check; replacement защищён integrity tests.
Других доказанных dead-code удалений нет. `order` сохранён: parent-local order_index нельзя безопасно
выдать за global `borehole_errors(column_order=...)` без отдельного контракта.

PRIVATE удалены auto-trigger/intake/source-mutation/staging/push workflow steps как исправление опасного
active contract, не как dead code. Их три известных workflow consumers теперь вызывают metadata-only planner.
Sources, registry/evidence/frozen assets не удалены и не изменены. Ansys checker implementation заменён
adapter при сохранении public functions и исторической regex/pointer/default semantics; внешние consumers
не были доказанно отсутствующими. Compatibility tests и [migration note](../development/CHECKS_COMPATIBILITY.md) retained.

Тесты не удалены; retired/frozen guards retained. Exact AST comparison старых assertions/raises выявило
только два intentional изменения: topic rule_version expectation v3→v3.1, и тот же ожидаемый `[]` для
MODEL_CHOICE с добавленным complete synthetic Transfer. Другие старые assertions/raises сохранены.
Четыре retained provenance nodeids получили complete Transfer fixtures:

- `tests/world/test_provenance.py::test_lab_to_massif_requires_transfer`;
- `tests/world/test_provenance.py::test_offsite_value_flagged`;
- `tests/world/test_provenance.py::test_pillar_zone_is_not_field_wide_skru1`;
- `tests/world/test_provenance.py::test_unattributed_pooled_legacy_and_general_values_need_explicit_status`.

Invariant переноса сохранён, добавлена недостающая source-side часть; новые negative SCI tests доказывают,
что status-only/incomplete Transfer более не разрешает use. No retired invariant был объявлен ненужным ради green CI.

## CI / TEST RESULTS

Два jobs используют один discovery runner: [OFFLINE_CHECKS.md](../development/OFFLINE_CHECKS.md).
PUBLIC-only, contents:read, strict markers, без PRIVATE checkout/secrets/solver/GPU/live services;
actions закреплены по проверенным полным upstream SHA. Zero collected/all skipped/errors/xfail/xpass/
unexpected skips fail. Новые test suites подхватываются discovery, без узкого списка текущих файлов.

```bash
python scripts/run_offline_checks.py world-integrity --output ../offline-world
python scripts/run_offline_checks.py corpus-offline --output ../offline-corpus
```

| Фактическая финальная проверка | Passed | Failed | Runtime skipped | Module skips | Deselected | Collection errors |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| world-integrity | 494 | 0 | 0 | 0 | 0 | 0 |
| corpus-offline | 1724 | 0 | 34 | 2 | 32 | 0 |
| Итого PUBLIC, непересекающиеся jobs | 2218 | 0 | 34 | 2 | 32 | 0 |
| Clean world-lock-only environment, отдельный повтор | 494 | 0 | 0 | 0 | 0 | 0 |
| PRIVATE engineering synthetic suite | 19 | 0 | 0 | 0 | 0 | 0 |

Corpus-offline passed включает corpus1537, engineering158, pure Python QGIS29.
Xfailed/xpassed/unexpected skips во всех финальных runner results: 0.
34 runtime skips: cv2=5, pymorphy3=21, DjVuLibre=1, nightly host prerequisites (отсутствует rsync)=6,
publish rsync=1. Module skips: cv2 raster, snowballstemmer retrieval pipelines.
Это NOT_RUN с node/reason в runner summary, не PASS. 32 deselected: действительные service/GPU/desktop/
PyQGIS tests; маркеры двух GPU и пяти PyQGIS prerequisites добавлены вместо недиагностированных skips.
Actions устанавливает CPU DjVu/Tesseract/rsync; hosted execution этого утверждения пока не выполнен.
OpenCV runtime receipt не задаёт exact wheel pin; Snowball/pymorphy closure тоже без exact pins — версии не выдуманы.

Финальный PUBLIC verifier после docs: 31 PASS, 7 SKIPPED_REF_UNAVAILABLE, 2 PRIVATE-dependent SKIPPED;
blocking failures=0. Runner обязательно проверил доступные frozen Git objects/legacy refs, package build/
non-editable install/8 root imports вне checkout/14 resource hashes. Семь исторических tag anchors отсутствуют
в remote; они остаются NOT_RUN, tags искусственно не создавались.
Отдельное разрешённое сравнение с 76 уже извлечёнными PRIVATE metadata inputs: manifest/public_vs_private/
verbatim **3 PASS**, без чтения restricted binaries. Это stored hash comparison и quote scan.
Временный current-builder audit дал 4 output files/7 cells с более коротким prefix, 0 новых numeric mentions;
все source numeric sequences preserved. Tracked PUBLIC catalogue outputs не перепубликовывались.

`pip check`: PASS (80 packages; также clean core environment). `git diff --check`: PASS.
PUBLIC leakage/host-path/schema/Markdown verifier: PASS. Git blob/AST boundary audit: frozen/data/core mutable IO
unchanged, no deleted tests; ровно два reviewed assertion changes описаны выше.
PRIVATE: 19 FAIL tests-first на исходном поведении, 19 PASS после; независимые дополнительные synthetic read/write/
redirection/false-input probes PASS. Исторические owned pathspecs точно 26/3/9, только .github changes.
Private workflow/intake/LFS execution NOT_RUN; artifacts remain PRIVATE.

Push: обычный PUBLIC Git не получил credentials; explicit available GitHub credential helper для обеих веток
вернул HTTP 401. Remote branches не появились, main остался исходным. Actions run ID отсутствует.
Оба repo содержат локальные commits на указанной branch; merge/rewrite/force-push не выполнялись.

## BLOCKERS / DEBT / следующий независимый проход

**MUST_FIX_BEFORE_MERGE:** подтверждённых оставшихся code MUST из финального independent review нет.
Для интеграции нужен разрешённый branch push и фактический GitHub Actions run обоих jobs.
Текущий статус workflow остаётся CONFIGURED/EXECUTION_NOT_RUN; hosted CI не объявляется PASS.

**SHOULD_FIX:** установить доказанные exact CPU pins для OpenCV/Snowball/pymorphy closure и выполнить сейчас
NOT_RUN cases; отдельно выяснить borehole global column-order contract; перед real benchmark consumer
сверять live code/model/seed identity с frozen declarations (ledger сам по себе не grants access).

**ACCEPTABLE_DEBT:** narrow exact-form profiles, trusted reviewed metadata и SYNTHETIC-only DRAFT;
missing historical tag anchors при verified доступных protected objects; отсутствие global multi-file reader
transaction при publisher rollback; нет continuous DB audit после NAV load; пустые existing package-data
declarations MATLAB entry/*.m и Ansys ladder/*.json имеют NO_SOURCE_FILES. PRIVATE metadata planner
использует hosted runner/Python, без заявления о полностью frozen environment или adversarial FS transaction.

**LIVE_NOT_RUN:** GitHub Actions execution, Windows locking/native filesystem semantics, network filesystems,
production CORE/EDGE/service/MCP deployments, PyQGIS/desktop, Ansys/OGS/MATLAB/ML/GPU, real sealed evaluation,
intake/source mutation и physical/scientific acceptance. Их отсутствие не блокировало независимый offline work.

Следующий агент может выполнить независимые batches без новой архитектуры:

1. **P0 integration:** исправить доступ push, отправить только рабочие branches, запустить оба существующих
   Actions jobs; DONE — реальные run ID/результаты, clean branches, review до merge. Rollback — revert тематического
   commit, без изменения main/history/frozen assets; не отключать failures ради CI.
2. **P1 reviewed scientific input:** заполнить binding/review только существующими evidence и metadata для
   конкретной выбранной формы. Перед REAL consumer отдельно спроектировать actual dataset/code identity guard;
   текущий DRAFT REAL blocker не убирать без отрицательных leakage/availability tests. DONE — selected-context
   positive/negative admission и независимый review; физическая валидация остаётся отдельной задачей.
3. **P2 optional CPU/debt:** подтвердить существующие exact dependency receipts, выполнить оставшиеся CPU nodes;
   column order подключать только после reproducer неверной последовательности. DONE — missing cases фактически
   выполнены, утверждения preserved, ни один frozen/retired test не удалён без invariant migration record.

Самопроверка: structural PASS не переименован в scientific PASS; UNKNOWN не заменён default; LAB/ANALOGUE/
graph adjacency не стали доказательством применения; frozen/history retained; tests не исключены ради green;
PRIVATE content не публиковался; NOT_RUN назван явно; каждый новый SCI contract имеет consumer и positive/negative e2e.

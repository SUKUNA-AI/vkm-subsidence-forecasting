# Аудит удаления legacy — 30.09.2026

Проверен PUBLIC HEAD `2babfbd3ebd0c7728a070b2b51944027e7de5427`. Удаление не выполнялось.

Reset уже удалил старые runtime `src/skru1`, Gate A/B/C, synthetic lab v2/v2.1 и retired v3.x из активного дерева. Нового удаления этих реализаций не требуется.

## Подтверждённый узкий набор

| Путь | Решение | Причина |
|---|---|---|
| `docs/legacy/CANONICAL_RESEARCH_STATE_PRE_RESET_RU.md` | yes / REMOVE_AFTER_COORDINATOR_REVIEW | D-18/D-19 отменили это направление; подробная старая инструкция дублирует историю и повышает шум поиска. |
| `src/skru1` | no / ALREADY_ABSENT_NO_DELETION | В tracked active tree уже отсутствует после reset/externalization; повторный import запрещён. |
| `configs` | no / ALREADY_ABSENT_NO_DELETION | В tracked active tree уже отсутствует после reset/externalization; повторный import запрещён. |
| `data/scenario_simulation_v2` | no / ALREADY_ABSENT_NO_DELETION | В tracked active tree уже отсутствует после reset/externalization; повторный import запрещён. |
| `data/scenario_simulation_v2_1` | no / ALREADY_ABSENT_NO_DELETION | В tracked active tree уже отсутствует после reset/externalization; повторный import запрещён. |
| `data/reconstruction_research_v1` | no / ALREADY_ABSENT_NO_DELETION | В tracked active tree уже отсутствует после reset/externalization; повторный import запрещён. |
| `SKRU1_ACTUAL_DATA_TABLES_v1` | no / ALREADY_ABSENT_NO_DELETION | В tracked active tree уже отсутствует после reset/externalization; повторный import запрещён. |
| `inputs/bootstrap` | no / ALREADY_ABSENT_NO_DELETION | В tracked active tree уже отсутствует после reset/externalization; повторный import запрещён. |
| `inputs/sources` | no / ALREADY_ABSENT_NO_DELETION | В tracked active tree уже отсутствует после reset/externalization; повторный import запрещён. |
| `scripts/build_evidence_from_legacy.py` | no / KEEP | Не retired: current test и утверждённый D-07 используют исторические git blobs; моделей нет. |
| `scripts/frozen_references.json` | no / KEEP | Обязательный якорь воспроизводимости исторических results; удалять нельзя. |
| `scripts/verify_canonical_repository.py` | no / KEEP | Активный verifier и tests; исторические проверки не являются retired solver runtime. |
| `docs/legacy/LEGACY_INDEX_RU.md` | no / KEEP | Прямой dependency canonical verifier и документации; сохраняет научные статусы и SHA. |
| `docs/legacy/LEGACY_DATA_RETIREMENT_2026-09-25_RU.md` | no / KEEP | ТЗ прямо требует сохранять retirement/migration receipts. |
| `artifacts/repository_cleanup/legacy_data_retirement_receipt.json` | no / KEEP | ТЗ прямо требует сохранять retirement/migration receipts. |
| `docs/reset_2026_09/run_kit` | no / KEEP | Не старый solver pipeline: publish_reports.py обязательный current PUBLIC boundary; reproduction tools подтверждают evidence vNext. |
| `docs/reset_2026_09/audit` | no / KEEP | Историческая provenance решений reset; это отчёты, не executable retired implementation. |
| `src/vkm_world/validation` | no / KEEP | Полезные правила переиспользованы current WorldSpec; нет imports skru1 или старых данных. |
| `src/vkm_corpus/retrieval_lab` | no / KEEP | Active search.hybrid/vectors/navigation и nightly topic score импортируют этот namespace; возраст benchmark не retirement. |
| `benchmarks/retrieval_v0` | no / KEEP | Current-source benchmarks не относятся к старой физической архитектуре. |
| `benchmarks/retrieval_v1` | no / KEEP | Historical evaluation поддерживает model/service provenance. |
| `benchmarks/retrieval_v2` | no / KEEP | Visual route конвертер и current deployment provenance ссылаются на V2. |
| `benchmarks/figure_readings_v1` | no / KEEP | Прямые входы текущего ТЗ A; не retired. |
| `benchmarks/chart_models_v1` | no / KEEP | Прямые serving/client dependencies текущего ТЗ A. |
| `schemas/worldspec_vnext.schema.json` | no / KEEP | Нет retired schema runtime; generated from current model and verifier checks hash. |
| `src/vkm_corpus/contracts/site_scope.py` | no / KEEP | LEGACY_RETIRED исключает retired archive из ingestion; удаление нарушит scientific guard. |

## Доказательства и ограничения

Полные зависимости по code, AST imports, tests, docs, scripts, schemas, infra/CI, benchmarks и entrypoints записаны в JSON рядом. Содержимое корпуса не воспроизводится.

Просканировано 925 tracked text files, 5481 AST import nodes. Старых imports `skru1` нет. Package discovery содержит только current namespaces. Tracked CI отсутствует. Весь `benchmarks/topic_v1/` и T1 label/reason материалы исключены до H freeze.

Один `yes` — pre-reset canonical-state document. Прямых references/imports/tests/links к нему не найдено; его байты доступны в `2babfbd3ebd0c7728a070b2b51944027e7de5427:docs/legacy/CANONICAL_RESEARCH_STATE_PRE_RESET_RU.md`. Он не pin в frozen registry. Source SHA и git blob SHA отдельно записаны в JSON; различие CRLF/LF при checkout допустимо.

`origin/legacy` указывает на `d54025d4c47b79b864076a33a4ecf877174ec922`, достижимый предок HEAD. Локальная ветка `legacy` отсутствует; remote tracking ref доступен. Canonical verifier подтвердил 11 frozen references и 14 retired files: 26 PASS, 7 SKIPPED_REF_UNAVAILABLE (отсутствуют annotated tags), exit 0. PRIVATE source SHA не проверялись в этом узком audit.

Run kit сохраняется: `publish_reports.py` — утверждённый current boundary PRIVATE → PUBLIC, а sweep merge/quote/coverage tools обеспечивают воспроизводимость evidence vNext. Старый digest/replay script относится historical input provenance, а не действующему Physical World runtime. Frozen registry, retirement receipts, current migration bridge и validation guards сохраняются.

Corpus benchmark helpers сохраняются: active `search.hybrid`, `search.vectors`, `navigation.topics`, `navigation.duplicates` импортируют `retrieval_lab`; nightly topic score использует `topic_bench`. Старый номер benchmark не означает retired scientific pipeline.

## Интеграция координатором

Добавлен короткий `docs/legacy/README.md`. Минимальные link fixes для предложенного удаления не нужны. Удалять после review только точный pathspec из JSON; отдельный cleanup commit без изменений history/PRIVATE/frozen bytes. После удаления — world tests, verifier, hygiene/leakage, links/imports/entrypoints/schema checks и повторный поиск.

# Продолжение production data: owners, таблицы и реальный restore

Дата: 04.10.2026. Это реализация в рабочей ветке PUBLIC и ограниченный реальный
recovery drill. Production switch, научная приёмка и полная кампания корпуса
не выполнены. Предыдущая точка — [bootstrap/promotion](PRODUCTION_BASELINE_PROMOTION_2026-10-04_RU.md).

## Что изменено

| Область | Сделано | Что это не доказывает |
|---|---|---|
| Text owner | Entrypoint вокруг исходного CareerOps loader и HTTP handler, offline snapshot loading, точные native source pins, обязательный PUBLIC helper closure и production Python executable | Реальная модель/image/base/dependency integrity и GPU parity NOT_RUN; прежний text service не включён |
| Visual owner | Патч точного llama upstream, живые model/context/mmproj/vocab handles под load/destroy mutex, epoch, nonce; собственный child и тот же inference client | Header CPU test и patch dry-run не являются full llama build или actual model qualification |
| Model identity | Embedded GGUF vocabulary отделён от JSON tokenizer gateway; ambiguous JSON отклоняется | Native identity не заменяет 49-tool acceptance или scientific admission |
| Table fidelity | Typed source-bound continuation, сохранение физических IDs, exact extraction signature passthrough, полный bounded обход явной цепочки с cell/header/unit/note locators | Actual adapters ещё не создают все candidates/signatures; auto-discovery и original review NOT_RUN |
| Compatibility | tables schema 0.1.2; exact historical 0.1.0/0.1.1 allowlist и прежние content/table hashes; additive API projection | Не выполнены rebuild, re-embedding или изменение старых corpus bytes |
| Recovery | Metadata inventory WORKSTATION/WSL/CORE/EDGE и независимый copy/restore 15 control files | Полный UNBACKED_UNIQUE inventory/restore и typed bootstrap backup остаются открытыми |

Runbooks: [text](../development/TEXT_OWNER_INTEGRATION_RU.md),
[visual](../corpus_platform/VISUAL_OWNED_LOAD_WITNESS_RU.md),
[table continuations](../development/TABLE_CONTINUATIONS_RU.md),
[реальные recovery observations](UNIQUE_RECOVERY_OBSERVATIONS_2026-10-04_RU.md).

## Независимые проверки изменений

Три агента работали с отдельными областями; после реализации проведено перекрёстное
review. До публикации исправлены выявленные ошибки:

1. Synthetic text factory мог выдавать обычный NativeModelProof. Теперь только
   source-owned memory container без listener; `/identity` возвращает 503 и
   отдельный `SYNTHETIC_UNQUALIFIED`. Production не принимает test callbacks.
2. Исходники самого text owner и helpers можно было исключить из inventory.
   Теперь code-owned transitive closure обязательна до loader, watched и hashed;
   actual Python executable в production также закреплён.
3. Native llama witness сообщал фиксированный upstream, но build ARG разрешал
   иной commit. Dockerfile теперь отклоняет override, не соответствующий hook pin.
4. BoundFile image-plan/recipe сравнивались с нормализованным record hash.
   Теперь обязательна проверка точных входных bytes; формат JSON входит в identity.

Сам main agent проверил shared vocabulary consumer, producer/canonical
compatibility и реальные source/backup/restore receipts. Synthetic semantic
interpretations не получили FACT/научный READY.

## Реальный backup / restore

Existing CORE→EDGE nightly receipt сообщает 485 643 файлов, 38 907 626 565 bytes,
совпадающий file-set digest, missing/corrupt/extra=0. В этой работе перечитаны
metadata manifests, не весь corpus; полный restore этого snapshot не выполнен.
Runtime backup recipe ещё старый: accounting closure отсутствует в его наборе;
исправление в коде **CODE_FIXED_NOT_DEPLOYED**.

Выбранный небольшой набор **15 файлов, 34 470 bytes** скопирован в новый закрытый
namespace EDGE и восстановлен отдельным чтением EDGE copy в новый ignored
workstation directory. Source/backup/restored SHA совпали для 15/15; native change
stamps и hashes оригиналов не изменились. Existing overwrite/cleanup не выполнялись.
[Public-safe receipt](../corpus_platform/receipts/control_restore_2026-10-04.json).

Из 44 явно обследованных recovery candidates новая копия защищает 10 scripts;
34 остальных ещё не получили новую verified copy. Bounded metadata обход больших
Windows/WSL trees завершился по лимитам и не доказывает полную уникальность.
Независимость подтверждена по наблюдённым host/disk domains; питание/помещение/
credentials не объявлены независимыми. Credential/config secrets в drill исключены.

Это не `IndependentBootstrapBackup`: нет полной byte closure actual candidate
stores/control/policy/review journal и полного restore. Заменять его маленьким
receipt или переименованным nightly JSON нельзя.

## CPU qualification и ограничения окружений

- Объединённый regression owners/shared identity/table/history/offline accounting:
  **393 PASS Linux**; **357 PASS Windows / 36 Linux-specific NOT_RUN**.
- Actual pinned CareerOps source-handler и packaging-input проверки:
  **50 PASS каждой ОС**, с tiny CPU model и memory handler. Без separately supplied
  source hosted CI выполняет 6 pure cases, остальные 44 явно deselected NOT_RUN.
- Original C++ lifecycle header компилирован и выполнен существующим WSL compiler;
  pinned patch dry-run проверен на exact upstream blobs. Full native build NOT_RUN.
- Обязательный World после verifier fix: **626 PASS Linux**. На workstation Windows — **625 PASS /
  1 FAIL WinError 1314**: нет права создать directory symlink. Не добавлен fake
  skip, не изменены Windows settings. Hosted Windows qualification проверяется отдельно.

Первый локальный World запуск использовал ignored temp внутри checkout и дал
12 дополнительных planted-verifier failures из-за наследования parent Git.
Повтор вне checkout устранил эти 12; это выявило отдельный verifier root boundary,
а не ошибки новых table/model contracts. Exact root guard, UTF-8 Git decoding и
единый список файлов для leakage исправлены; 55 verifier/publication checks PASS
каждой ОС, в том числе три новые регрессии. Результат старого запуска сохранён,
не назван PASS.

На Windows isolated wheel test сначала остановился из-за отсутствия обоих
existing installers (`uv` и `pip` в выбранном venv). Ничего не устанавливалось;
Linux installed-wheel и header checks PASS: combined 52 PASS с text integration.
Ничего не устанавливалось в существующие environments. Corpus/evidence schema
exports/checks PASS; PUBLIC+PRIVATE verifier: 36 PASS, 7 SKIPPED_REF_UNAVAILABLE,
без blocking failures. [CPU receipt](../corpus_platform/receipts/model_owners_tables_cpu_2026-10-04.json)
содержит точные input/JUnit hashes и раздельные failed/NOT_RUN attempts.
Exact-head hosted CI проверяется после push и сохраняется в draft PR #10;
прежний f94c50d CI не квалифицирует новые изменения.

## Следующий реальный порядок

1. Зафиксировать clean source commit и полный inventory оригиналов, uniquely
   non-reproducible work и canonical review journal. Обеспечить независимые копии
   и full restore closure для exact bootstrap candidate.
2. Подготовить pinned owner environment и immutable images. Текущий text 3.12
   image не подходит PUBLIC 3.13 contract; supplied base metadata не доказывает
   установленную dependency integrity. Не переносить целиком CareerOps в PUBLIC.
3. Изолированно запустить actual owners и matching API/MCP/receiver profile:
   реальные 49 tools, model parity, bounded resources, restart/failed-switch/
   restore/coherent previous rollback. Пока весь этот runtime gate **NOT_RUN**.
4. Только после evidence пункта 3 — отдельно разрешённый live promotion/switch
   с выбранным коротким окном недоступности. Code-only update сам по себе не
   требует нового intake, OCR или re-embedding.
5. Развивать actual DOCUMENT/table/formula adapters, source-backed claims/entities/
   observation sets, conflicts и обе временные оси; связать object signatures с
   реальными producing stages. Nullable passthrough не заполняет исторические NULL.
6. До полной кампании: независимый stratified gold, заранее закреплённые метрики,
   frozen corpus manifest и workload limits. Все используемые научные данные и
   подозрительные места проверить по оригиналу; unresolved сохранить явно.
7. S26 выполнять после всех применимых gates, с budget/resume/reuse и одним
   согласованным поколением. Required GitHub checks и actual MCP/status
   qualification остаются отдельными operational задачами; внешний канал отложен.

Изменения размещаются в draft PR #10; это не слияние в main и не production deploy.
`.mcp.json.example` владельца не включён в изменение. PRIVATE, corpus originals,
production selectors, model workloads и GitHub settings не изменялись.

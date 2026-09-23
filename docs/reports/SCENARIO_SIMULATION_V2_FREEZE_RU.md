# Инженерное завершение и Git freeze scenario_simulation_v2

Дата: 2026-09-24. Итог review и повторной data-only validation: **PASS**.
Содержательных блокеров не найдено. Generator/config/constraints/seed/mixture,
latent worlds, observations, features, evaluator и split semantics не менялись.
Freeze вступает в силу с содержащим этот документ release commit
`data: freeze scenario simulation v2 release`.

## Согласованность и состав

Повторно сверены dataset card, полный отчёт A–H, experiment protocol, IMM design
note, dataset/constraints/finalization manifests, change inventory, исходный patch,
reproducibility/validation/test receipts, QA tables и metadata графиков.
Все содержательные counts совпадают: 640 scenarios, 128 latent-world IDs,
127 distinct full latent fields, 42 points, 14 profiles, 29 campaigns,
321766 origins и столько же model windows. Split: train 122047, calibration
35148, evaluation 133833, excluded 30738. Numerical scale-bound fraction
0.984375; empirically identified temporal-law fraction 0.0.

Не обнаружено ошибки данных, stale count или typo, требующих изменения
замороженных payload/report bytes. Исправлены только вопросы технической упаковки:

- сохранение байтов покрытых SHA256 файлов при `core.autocrlf=true`;
- хранение 12 сжатых CSV через уже принятую в проекте политику Git LFS;
- включение ранее untracked v1 data/code/config/protocol как необходимых
  dependencies и baseline дифференциального отчёта;
- отдельный checkout inventory вместо требования иметь локальные `egg-info`
  и результаты v1 benchmark для проверки нового release;
- отдельная freeze declaration, объясняющая сохранённый creation-time статус
  `uncommitted_source_hashes_authoritative` и старые Git receipts.

Whitespace review оставляет один известный non-blocking formatting finding:
пустая строка в конце `src/skru1/scenario_simulation_v2.py:699`. Файл покрыт
release SHA256; косметическое удаление строки изменило бы frozen bytes и поэтому
не выполнено. CRLF в исходных hashed receipts явно разрешён в `.gitattributes`,
как уже было принято для предыдущей reconstruction release. Других whitespace
findings нет; это не отключение data tests или semantic validation.

Все statements о previous uncommitted state в отчёте A–H и receipts остаются
исторически верными. Их не переписали ради нового commit hash. Текущая authority:
[SCENARIO_SIMULATION_V2_FREEZE.md](../governance/SCENARIO_SIMULATION_V2_FREEZE.md).

Проверка на временные пути/IDE/cache/pyc/секреты не выявила нежелательных файлов
в release selection. `work/` и caches остаются ignored. Absolute operational
paths не найдены; HTTPS-ссылки не считаются локальными drive paths. Присутствующие
`work/...` в command receipts — относительные имена исполненных scratch runs,
а не включённые в Git каталоги. Сохранены только журналы, подтверждающие release
checks, не произвольные runtime logs. HTML со встроенными графиками и отдельные
PNG оставлены как самостоятельный reader artifact и исходные report figures;
patch сохранён как evidence создания, а не лишняя копия dataset.

## Provenance и methodological invariants

Проверены по `empirical_constraints_v2.py`, `scenario_simulation_v2.py`, configs,
feature contract и serialized data. Сохранены разделение provenance classes,
семь разрешённых leveling donors, unknown published points, prohibition common
datum/cross-period subtraction, exclusion unidentified temporal inset, отсутствие
radar-leveling sigma и other-site numeric transfer, context-only карта 2016.
Все temporal forms/onsets/durations остаются explicit assumptions. Roof effect —
отдельный reflector-contamination stress. Measurement-only pseudo-transitions
не меняют latent truth. Features содержат только allowlist; targets — next planned
targeted campaign; history causal. Replicas одного latent world зависимы;
reactivation/moving focus отсутствуют в train/calibration. Никакая часть freeze
не разрешает оптимизировать v2 по будущим model results.

## Что действительно повторено

| Проверка | Результат |
| --- | --- |
| Constraints: 2 новые сборки | Все 6 файлов совпали с release и друг с другом |
| Dataset: 2 новые сборки | Все 20 файлов совпали с release и друг с другом |
| Independent serialized validation | PASS, все 321766 origins и 321766 windows |
| New data/reconstruction/atlas tests | 36 passed |
| V1 data-only regression selection | 4 passed, 3 вне области deselected |
| Feature causality / target pairing / truth isolation | Повторно прошли tests и independent validation |
| Distinct full latent fields | 127 по независимо вычисленным full-field SHA256 |
| Full local old-file inventory | 1059/1059 без изменения bytes |
| Dataset, constraints, QA, finalization hashes | Все проверены |

Источник запуска: новый `work/data_foundation_v2/freeze_20260924_01`.
Команды, output logs, signatures и receipts сохранены в
[freeze evidence](../../artifacts/data_quality/scenario_simulation_v2_freeze/recheck_report.json).
Reader/source PDF review, визуальный просмотр 10 PNG и исходный old/new audit
проверены по неизменившимся hashed receipts; они не запускались повторно, поскольку
data/code/figures не изменились. Full repository test suite не запускалась:
она включает model fits и historical-label workflows вне текущего разрешения.

## Git boundary и переносимость

До этой задачи tracked diff и index были пусты; v1/v2 additions оставались
untracked. `.gitignore` уже исключал work, caches, environments и sealed package.
Менять его не потребовалось. `.gitattributes` дополнен только правилами сохранения
release bytes и scoped LFS для `.csv.gz` v1/v2. LFS уже использовался в проекте;
новый convention не вводился. Крупнейший payload — 20780157 bytes.

Исходный local inventory из 1059 записей сохранён без правок. Его subset из
993 файлов с одинаковыми Git/worktree bytes находится в
[checkout_baseline_inventory.json](../../artifacts/data_quality/scenario_simulation_v2_freeze/checkout_baseline_inventory.json).
17 записей — десять v1 benchmark artifacts, execution config/runner и пять
ignored egg-info files. Ещё 49 существующих environment/worker receipts отличаются
в Git только нормализацией CRLF→LF; они не входят в зависимости v2 и не переписаны.
Все 66 исключений перечислены в `checkout_inventory_exclusions.json`. Все 1059
файлов проверены на локальной машине. Для canonical-byte проверки clean checkout
следует клонировать с `--config core.autocrlf=false`; собственные bytes v2
защищены `.gitattributes`. Никакие старые материалы не удалены.

Data-only tests v1 требуют старые adapter и benchmark module при import; эти
два модуля включены как неизменённые test dependencies. Benchmark executable,
execution config и results не входят в commit. Выполняется только явный `-k`
из freeze protocol; публикация test module не разрешает model-related tests.

Полный staged inventory с размерами, SHA256 payload и LFS policy фиксируется в
[staged_inventory.json](../../artifacts/data_quality/scenario_simulation_v2_freeze/staged_inventory.json).
Собственные inventory/manifest records вынесены в metadata-exclusions, чтобы
не создавать циклические self hashes. Финальный index проверяется по payload
hashes; LFS pointer OID/size должны совпадать с неизменённым payload.

Branch: `main`. Remote: `origin`, `https://github.com/SUKUNA-AI/vkm-subsidence-forecasting.git`.
При первом remote check `origin/main` был на `2c5eec48d523cf3fbc15898b0b44ac929b9b7c14`;
локальный parent `93463fbea798697183c1b0b1d3180eea934dea0f` содержит три ранее созданных
коммита реконструкции. Normal push включает эти необходимые предшествующие коммиты.
Локальных и remote tags нет; tag для этого release не создаётся.

Публикация выполняется одним новым commit с указанным title; его ID и результат
push сообщаются после исполнения. В commit не записывается его собственный SHA.
Force push, rebase/reset и изменение чужой истории запрещены. При отказе remote
публикация останавливается без исправления истории.

## Freeze identity

```text
dataset manifest SHA256
7bb67939314fbcf548686893e6868c90a2303e1303c401377f197e077fec8c13
constraints manifest SHA256
96b88b61cf766062c1f831e32b50bdd82441da63c50ce41569a5ffad297de6ef
original finalization manifest SHA256
f0f36912adc515f3cbf50eb92ba7a13728d2173d31fb322d8efff9568233e2f4
```

Models executed = 0; legacy holdout labels parsed = 0; frozen v2 semantics unchanged.
Дальнейшие semantic изменения — только v3 либо отдельный preregistered sensitivity
release. Следующий разрешаемый отдельно этап — acceptance v2 adapter и sequence
tensorization, без переноса старых результатов моделей на v2.

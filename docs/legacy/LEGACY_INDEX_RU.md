# Индекс legacy: ветка `legacy`, исторические commit и FROZEN_REFERENCE

Новый `main` строится вокруг `src/vkm_world` (см. [архитектуру](../architecture/REPOSITORY_ARCHITECTURE_RU.md)).
Байтов старой архитектуры в рабочем дереве нет: это v2/v2.1 stress lab, Gate A/B/C, реконструкции, retired v3.2.
Всё сохранено в истории git. Идентичность frozen-релизов проверяется из git-объектов, без checkout и без
загрузки LFS:

- машиночитаемый реестр — [`scripts/frozen_references.json`](../../scripts/frozen_references.json);
- проверка — [`scripts/verify_canonical_repository.py`](../../scripts/verify_canonical_repository.py).

Этот документ — человекочитаемый индекс к ним. Источник классификации — структурный аудит PUBLIC
от 26.09.2026 (раздел 6, реестр FROZEN_REFERENCE).

## 1. Научный статус (не менять при цитировании)

| Что | Статус |
|---|---|
| `SKRU1_SCENARIO_SIMULATION_V2`/`V2_1`, representation R1 | **синтетический** factorial stress benchmark. Не полевые данные СКРУ-1 и не физически калиброванный мир |
| Gate A/B/C (в т.ч. Gate B3, модель B7 IMM) | исторические результаты на retired v3.2 (синтетическая реконструкция). Не оценка текущего WorldSpec |
| `data/reconstruction_research_v1` | «ремонт» синтетических таблиц v3.2 (`observation_origin=synthetic_monitoring_simulation`). Не наблюдения |
| Оцифровка Мусихина (линия 1, профили 1/5/17/6) | ручная оцифровка слайдов VKM-SRC-002 (discovery-only): интервальные смещения, категориальная ось X, без георефенса. Не временной ряд СКРУ-1 |
| R1 (нейросетевые компараторы) | только design review, обучения не было |
| R2 (adversarial-аудит) | аудит источников и физической evidence на 24.09.2026 |
| `SKRU1_ACTUAL_DATA_TABLES_v1/**`, `inputs/bootstrap/**` | **LEGACY_RETIRED**: только историческая provenance, повторно не импортируются |

Правило: legacy-результат может войти в новую цепочку только как новая запись WorldSpec. Для этого нужны
новый ID, провенанс и эпистемический статус. Байтовое совпадение с frozen-релизом его научный статус не повышает.

## 2. Якоря: ветка, теги, commit

| Якорь | Commit | Дата | Что зафиксировано |
|---|---|---|---|
| ветка `legacy`, тег `legacy/final` | `d54025d4c47b79b864076a33a4ecf877174ec922` | 25.09.2026 | финальное состояние PUBLIC до reset: `src/skru1`, `scripts/*` (Gate A/B/C, генераторы и валидаторы v2/v2.1, реконструкции), `configs/`, `data/`, `artifacts/`, `docs/{governance,reports,research,model_cards}`, старые `tests/test_*.py`, `requirements/` |
| тег `frozen/gate-b3` | `ce8e57ee3dfd82579fb00c8ab5d17bf1b925c9a4` | 30.08.2026 | Gate B3 two-regime IMM. В дереве ещё есть входной bundle `SKRU1_ACTUAL_DATA_TABLES_v1` |
| — | `90e2e293cf868a6ef191f2eddc0f52d46f94d4b8` | 05.09.2026 | `data/reconstruction_research_v1` (manifest) |
| — | `9d58f7bebcd323c23b319ab34b4e7ba62362fea6` | 06.09.2026 | атлас реконструкций: manifests Мусихина line1/profiles |
| тег `frozen/scenario-v2` | `ffcf86972d31bd9f0dbd62d1265001f5c722e253` | 24.09.2026 | `SKRU1_SCENARIO_SIMULATION_V2` и scenario constraints v2 |
| тег `frozen/scenario-v2.1` | `71d705d092403a8aae3a5300dec57938df11ad02` | 24.09.2026 | erratum v2.1 и замороженное representation R1 |
| тег `frozen/r1` | `c5da1103154c29e2ffe9c52bbceacaa032f1690f` | 24.09.2026 | R1 neural comparators review |
| тег `frozen/r2` | `2794c877369e6fc77629a7a6933e8869e8233bcc` | 24.09.2026 | R2 adversarial audit |
| тег `archive/v3.2-last` | `10452b011b1e9685efe8de29ceb22c18f5e64c30` | 25.09.2026 | последний commit с retired v3.2 и `inputs/sources/**` (LFS-указатели) в дереве |
| — | `d25f4587cb8ba90761f399b6293499eb94daaa2a` | 25.09.2026 | удаление v3.2/`inputs/**` из дерева (externalization) |

**Состояние тегов.** Семь annotated-тегов (`legacy/final`, `frozen/gate-b3`, `frozen/scenario-v2.1`,
`frozen/scenario-v2`, `frozen/r1`, `frozen/r2`, `archive/v3.2-last`) созданы 26.09.2026 локально в cloud-сессии.
Push тегов прокси отклонил (HTTP 403), поэтому их нужно отправить с рабочей станции:

```bash
git push origin legacy/final frozen/gate-b3 frozen/scenario-v2.1 frozen/scenario-v2 frozen/r1 frozen/r2 archive/v3.2-last
```

Если тегов в клоне нет, их можно создать заново, строго на указанные commit:

```bash
git tag -a legacy/final         d54025d4c47b79b864076a33a4ecf877174ec922 -m "Final pre-reset PUBLIC architecture"
git tag -a frozen/gate-b3       ce8e57ee3dfd82579fb00c8ab5d17bf1b925c9a4 -m "Gate B3 two-regime IMM (historical)"
git tag -a frozen/scenario-v2.1 71d705d092403a8aae3a5300dec57938df11ad02 -m "Scenario v2.1 + representation R1 (synthetic)"
git tag -a frozen/scenario-v2   ffcf86972d31bd9f0dbd62d1265001f5c722e253 -m "Scenario v2 (synthetic, predecessor)"
git tag -a frozen/r1            c5da1103154c29e2ffe9c52bbceacaa032f1690f -m "R1 neural comparator review"
git tag -a frozen/r2            2794c877369e6fc77629a7a6933e8869e8233bcc -m "R2 adversarial audit"
git tag -a archive/v3.2-last    10452b011b1e9685efe8de29ceb22c18f5e64c30 -m "Last commit with retired v3.2 inputs"
```

Дополнительно: включить на GitHub защиту ветки `legacy` (запрет force-push и удаления). История `main` не
переписывается (без squash и force-push). Commit `ce8e57e`, `10452b0` и другие достижимы только как предки
`main`/`legacy`. Если историю переписать, проверяемость frozen-ссылок пропадёт.

## 3. Реестр FROZEN_REFERENCE

Хеш — SHA-256 байтов файла. Для LFS-файла это `oid sha256` его LFS-указателя: LFS oid и есть SHA-256
содержимого. Каждая запись проверяется на своём commit и на `legacy`; на `legacy` все pinned-файлы
байтово совпадают.

| ID | Pinned-файл | SHA-256 | Commit (тег) | Правило closure |
|---|---|---|---|---|
| `SKRU1_SCENARIO_SIMULATION_V2_1` | `data/scenario_simulation_v2_1/manifest.json` | `a268cd7800e1320a1cc61172f95cf458c7ac7434237b32b288f23a83a97a2c95` | `71d705d` (`frozen/scenario-v2.1`) | manifest: `inputs`/`outputs` с sha256, рекурсивно во вложенные manifests |
| `SKRU1_SCENARIO_CONSTRAINTS_V2` | `artifacts/reconstruction/scenario_constraints_v2/manifest.json` | `96b88b61cf766062c1f831e32b50bdd82441da63c50ce41569a5ffad297de6ef` | `ffcf869` (`frozen/scenario-v2`) | manifest (рекурсивно в manifests Мусихина) |
| `SKRU1_SCENARIO_REPRESENTATION_V2_1_R1` | `artifacts/splits/scenario_representation_v2_1/representation_manifest.json` | `0e5501cb4c3cff570fbd3763047a8dd8738d45706dd6f549556ac428521f5a6d` | `71d705d` (`frozen/scenario-v2.1`) | manifest `sources`/`outputs`; поля `dataset_sha256`, `data_correction_receipt_sha256`, `constraints_sha256` совпадают с pin'ами реестра |
| `SKRU1_SCENARIO_V2_1_CORRECTION_RECEIPT` | `artifacts/data_quality/scenario_simulation_v2_1/correction_receipt.json` | `5fc8b24111503e997e6896e7ca6ee1cd7a8628be633d70f7a2febb840d0a2cf5` | `71d705d` (`frozen/scenario-v2.1`) | `qa_files` с sha256; поля `original_/new_/constraints_manifest_sha256` совпадают с v2, v2.1 и constraints |
| `SKRU1_SCENARIO_SIMULATION_V2` | `data/scenario_simulation_v2/manifest.json` | `7bb67939314fbcf548686893e6868c90a2303e1303c401377f197e077fec8c13` | `ffcf869` (`frozen/scenario-v2`) | manifest |
| `SKRU1_RECONSTRUCTION_RESEARCH_V1` | `data/reconstruction_research_v1/manifest.json` | `c999d2ef98da1645de4831e30b131c8da72abde2f4f1372d1cb9868b98df0dd2` | `90e2e29` | manifest |
| `MUSIKHIN_LINE1_2011_2016_V1` | `artifacts/reconstruction/musikhin_line1_2011_2016_v1/manifest.json` | `5081dc8ce463119774cb4a6e3ffdd121a1a38e9b3bd0cee75e0382dbb6ce9f06` | `9d58f7b` | manifest |
| `MUSIKHIN_PROFILES_2011_2016_V1` | `artifacts/reconstruction/musikhin_profiles_2011_2016_v1/manifest.json` | `7740a92603762c9fb7db3c264db95c257bcf28d635aea54a2775676956e2bac4` | `9d58f7b` | manifest |
| `GATE_B3_T1_B3_V1` | `artifacts/model_selection/t1_b3_v1/artifact_inventory.csv` | `127fa1943159c1bf8b605c1926601dc664f7f15196b5f245410b79650dfaacd4` | `ce8e57e` (`frozen/gate-b3`) | CSV-inventory: `relative_path`, `size_bytes`, `sha256` для всех 19 выходов |
| `R2_ADVERSARIAL_AUDIT` | `artifacts/research/r2_adversarial_audit/audit_receipt.json` | `f8d0b1d42fc9aae27179ba766364e51e5eb235c848738f704dfffa68363136d6` | `2794c87` (`frozen/r2`) | только сам файл |
| `R1_NEURAL_COMPARATORS_REVIEW` | `docs/research/NEURAL_COMPARATORS_V2_1_RESEARCH_RU.md` | `932c4ee3bf45a68b1b61fea4d82dc1a20f0dd8f60a41b14344bfb0bcb5151bf3` | `c5da110` (`frozen/r1`) | только сам файл |

Особые пути внутри closure:

- **`inputs/sources/**`** (12 научных PDF/DOCX). На `legacy` их нет, они вынесены в PRIVATE. Verifier ищет
  ожидаемый SHA-256 в PRIVATE `00_registry/SOURCE_REGISTER.csv` (нужен `VKM_RESOURCES_ROOT`), а на `71d705d`,
  `ffcf869` и `9d58f7b` дополнительно сверяет LFS oid. Сверка идёт по SHA, а не по путям.
  Дубликаты в PRIVATE `08_data_archives/main_repo_snapshots/**` не используются.
  Соответствие старых путей и ID: `01006516068.pdf`→VKM-SRC-001, `geokniga-02obrabotka.pdf`→VKM-SRC-002,
  `Babayants-Disser.pdf`→VKM-SRC-003, `03-GR-24-2.pdf`→VKM-SRC-004, `106_Губанова__Глебова.pdf`→VKM-SRC-005,
  `Гусев_АП_…`→VKM-SRC-006, `Shirshova (1).pdf`→VKM-SRC-007, `sputnikovaya_…`→VKM-SRC-008,
  `02f4a17c….pdf`→VKM-SRC-009, `88q34t4o….pdf`→VKM-SRC-010, `ВКР_Филатова_М_С.docx`→VKM-SRC-023,
  `НК 26 Бобровицкий Григорий.pdf`→VKM-SRC-024.
- **Retired v3.2** (`SKRU1_ACTUAL_DATA_TABLES_v1/**`). На эти 14 файлов ссылаются manifests v2, v2.1,
  reconstruction_research_v1 и Мусихин line1. Они перечислены в `retired_references` реестра и
  проверяются на `10452b0` (`archive/v3.2-last`): 14/14 совпадают. Verifier также проверяет, что каждая
  retired-ссылка, встреченная при обходе closure, есть в реестре с тем же SHA.

## 4. Проверка без моделей

Что делает verifier:

- только читает: `git rev-parse`, `git cat-file --batch`, хеширование, разбор manifests, CSV и Markdown;
- не запускает модели и солверы;
- не разбирает evaluator truth: synthetic truth-файлы v2.1 идентифицируются только по LFS oid;
- не обращается к сети.

```bash
python scripts/verify_canonical_repository.py                        # все проверки, JSON в stdout
python scripts/verify_canonical_repository.py --only registry frozen_references retired_references
VKM_RESOURCES_ROOT=<PRIVATE checkout> python scripts/verify_canonical_repository.py --output
#   --output без значения пишет work/verification/canonical_verification.json
```

Код выхода 0 — ни одна блокирующая проверка не дала `FAIL`. Итоговый `status`:

- `PASS` — всё прошло;
- `PASS_WITH_NONBLOCKING` — есть `SKIPPED`/`WARN`, например не задан `VKM_RESOURCES_ROOT` или нет тегов;
- `FAIL` — есть блокирующая ошибка, её id перечислены в `blocking_failures`.

Если ref не найден (shallow clone, не получены теги или ветка), проверка даёт `SKIPPED_REF_UNAVAILABLE`
(неблокирующий статус) с инструкцией. Типичное исправление:

```bash
git fetch --unshallow origin        # только для shallow clone
git fetch origin legacy:legacy
git fetch origin --tags
```

Ручная проверка одного pin:

```bash
git show frozen/scenario-v2.1:data/scenario_simulation_v2_1/manifest.json | sha256sum   # a268cd78…2c95
git cat-file -p legacy:data/scenario_simulation_v2_1/model_features.csv.gz               # LFS-указатель: oid sha256:…
```

## 5. Воспроизведение проверок v2.1 (data QA, без моделей)

Выполнять в отдельном worktree, а не в `main`. Для `.gz` нужны LFS-объекты: `git lfs pull` на рабочей
станции. В cloud-окружении LFS-загрузка может быть закрыта (см.
[политику данных](../governance/DATA_AND_PATH_POLICY_RU.md), п. 5).

```bash
git worktree add ../vkm-legacy-v2_1 legacy            # или frozen/scenario-v2.1: файлы v2.1 идентичны
cd ../vkm-legacy-v2_1 && git lfs pull
python3.13 -m venv .venv && . .venv/bin/activate
python -m pip install numpy pandas PyYAML pydantic scikit-learn pytest    # исходный lock: requirements/*.lock.txt (Windows)
python scripts/validate_scenario_v2_1_erratum.py --output work/legacy_check/erratum_differential.json
python -m pytest -q -p no:cacheprovider tests/test_scenario_v2_1_erratum.py tests/test_scenario_representation_v2_1.py \
       -k "not model_interface_construction_only"
```

- `validate_scenario_v2_1_erratum.py` — независимый дифференциальный аудит v2 → v2.1. Он проверяет SHA
  manifest v2 и constraints и пофайловые hashes и размеры обоих релизов. Затем сравнивает только model-facing
  файлы (`model_features`, `causal_history`, `sequence_windows`); evaluator truth не читает.
- В полном прогоне аудита 26.09.2026 на дереве `1c1f816` (v2.1-файлы там байтово равны `legacy`)
  прошли все тесты `test_scenario_v2_1_erratum.py` (15) и `test_scenario_representation_v2_1.py` (29).
- Исключённый тест `model_interface_construction_only` импортирует модельные классы. Он не обучает модели,
  но в исходной приёмке запускался отдельно.
- `scripts/validate_scenario_representation_v2_1.py` открывает synthetic evaluator payload через scorer-side
  loader. В текущей исследовательской цепочке его запускать не нужно; hash-closure проверяет verifier.
- Исходные команды в отчётах `legacy` записаны для Windows (`.venv/Scripts/python.exe`). Выше дан
  эквивалент для POSIX.

## 6. Воспроизведение Gate B3 (исторический результат)

Проверку идентичности без запуска моделей выполняет verifier: `frozen:GATE_B3_T1_B3_V1@ce8e57e` сверяет
inventory и все 19 выходов по SHA-256.

**Полный перезапуск — это исполнение моделей** (подбор параметров IMM, nested tuning). По правилам проекта
он требует отдельной явной задачи. Точное повторение возможно только на `ce8e57e`, по двум причинам:

- позже `src/skru1/gate_b3.py` дополнялся (+82/−26 строк: приёмка «governed suite v4 successor»);
- на `legacy` нет входного bundle `SKRU1_ACTUAL_DATA_TABLES_v1`, поэтому тесты Gate B3 там падают.

```bash
git worktree add ../vkm-gate-b3 frozen/gate-b3        # = ce8e57e
cd ../vkm-gate-b3 && git lfs pull
python3.13 -m venv .venv && . .venv/bin/activate && python -m pip install -e . pytest   # + requirements/modeling.lock.txt
python scripts/run_gate_b3.py --phase validate         # проверка сохранённых артефактов, без повторного обучения
python scripts/run_gate_b3_audit.py                    # authoritative post-run audit артефактов
# только при отдельном разрешении на исполнение моделей:
python scripts/run_gate_b3.py --phase develop
python -m pytest -q tests/test_gate_b3_protocol.py tests/test_gate_b3_imm.py
```

После `--phase develop` сравните выходы с `artifacts/model_selection/t1_b3_v1/artifact_inventory.csv`
(столбец `sha256`). Метрики B7 из отчёта Gate B3 — temporal MAE 6.545 мм/год и coverage95 0.962. Это
исторический результат на синтетической retired-реконструкции v3.2. Для текущего мира СКРУ-1 он ничего не
доказывает.

## 7. Где что лежит (коротко)

| Область на `legacy` | Содержимое | Судьба в новом `main` |
|---|---|---|
| `src/skru1/**` | контракты данных, leakage/splits, метрики, Gate A–C, генераторы v2/v2.1 | generic-части переносятся в `vkm_world` (validation, core/io), остальное только в истории |
| `data/scenario_simulation_v2{,_1}/**` | по 20 файлов, в каждом 8 LFS `.gz` | FROZEN_REFERENCE (раздел 3) |
| `artifacts/{splits,data_quality,reconstruction,model_selection,research}/**` | representation R1, QA-receipts, атлас реконструкций, Gate B*, R2 | FROZEN_REFERENCE / только история |
| `configs/*.csv`, `configs/*.yaml`, `configs/*.json` | манифесты источников (`SRC01–11`, `SUP01`), конфиги Gate и генераторов | только история; ID источников — `VKM-SRC-*` PRIVATE |
| `docs/{governance,reports,research,model_cards}/**` | протоколы Gate, отчёты, R1/R2 | только история; перенесённые правила — в `docs/governance/` |
| `tests/test_*.py` | старый набор (105 passed / 63 не работают без retired-данных, аудит 26.09.2026) | data-free тесты переносятся в `tests/world/` |

Ссылки на конкретный файл истории лучше давать постоянными URL вида
`https://github.com/SUKUNA-AI/vkm-subsidence-forecasting/blob/<commit>/<path>` с полным SHA. Имя ветки
в таких ссылках не использовать.

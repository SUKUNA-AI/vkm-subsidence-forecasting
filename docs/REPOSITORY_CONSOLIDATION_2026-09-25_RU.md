# Консолидация репозиториев и каноническое состояние исследования

**Дата фиксации:** 25 сентября 2026 года  
**Статус:** cleanup и retirement старой v3/v3.2 ветки завершены; canonical verifier — `PASS`.

---

## 1. Итоговое решение

Проект окончательно разделён на две независимые по назначению части:

1. **`SUKUNA-AI/vkm-subsidence-forecasting`** — рабочее исследовательское ядро.
2. **`SUKUNA-AI/vkm-subsidence-forecasting_resourses`** — private scientific evidence repository.

Одновременно зафиксировано принципиальное решение по старой ветке данных:

> `SKRU1_ACTUAL_DATA_TABLES_v1`, reconstruction v3/v3.2, старые model-ready/EDA/target/evaluation packages и `inputs/bootstrap/**` имеют статус **LEGACY_RETIRED**.

Они были созданы на раннем этапе проекта при представлении о данных и временной реконструкции, которое впоследствии было признано непригодным как current scientific basis.

Поэтому эти пакеты:

- не являются current evidence;
- не являются реальными исходными маркшейдерскими данными СКРУ-1;
- не требуются для Physical Evidence Consolidation;
- не требуются для `Physical World v1`;
- не требуются для OpenGeoSys;
- не поддерживаются как byte-exact snapshots в current trees;
- при необходимости доступны через Git history соответствующих historical commits.

Авторитетный retirement-документ:

`docs/LEGACY_DATA_RETIREMENT_2026-09-25_RU.md`

---

## 2. Роли репозиториев

### 2.1. Основной репозиторий

`SUKUNA-AI/vkm-subsidence-forecasting`

Он хранит:

- current frozen stress benchmark `SKRU1_SCENARIO_SIMULATION_V2_1`;
- representation `SKRU1_SCENARIO_REPRESENTATION_V2_1_R1`;
- код, тесты и конфигурации;
- current reconstruction research;
- historical Gate B3 documentation как историческую точку сравнения;
- R1 neural comparator research;
- R2 adversarial source/data/physics audit;
- current physical-world research;
- governance;
- manifests и receipts.

Основной repo больше не используется как склад raw PDF, книг, архивов, Excel/GIS-контейнеров или старых generated datasets.

### 2.2. Private scientific evidence repository

`SUKUNA-AI/vkm-subsidence-forecasting_resourses`

Он хранит **актуальные научные источники и их provenance**:

- книги;
- статьи;
- диссертации;
- primary/secondary source files;
- материалы по геологии и стратиграфии;
- физико-механические свойства;
- геомеханику и реологию;
- mining/backfill evidence;
- GPR;
- InSAR;
- monitoring evidence;
- SHA-256 registry;
- будущие curated physical-evidence tables.

Private repo **не является архивом всех когда-либо созданных project-generated datasets**.

---

## 3. Что было удалено из current main

Из current tree основного репозитория были вынесены:

- `inputs/sources/`;
- `inputs/bootstrap/`;
- `SKRU1_ACTUAL_DATA_TABLES_v1/`.

После review их статус разделён.

### 3.1. `inputs/sources/**`

Это реальные внешние научные материалы.

Их canonical copies остаются в private resources repo и являются **active scientific evidence**.

Они проверяются по SHA-256 через canonical verifier.

### 3.2. `inputs/bootstrap/**`

Это старые project-generated bootstrap scripts/packages, связанные с retired v3/v3.2 веткой.

Они имеют статус:

`LEGACY_RETIRED`

и не являются current dependency.

### 3.3. `SKRU1_ACTUAL_DATA_TABLES_v1/**`

Это старый реконструированный/model-ready пакет, включавший, среди прочего:

- reconstruction v3/v3.2;
- synthetic/evaluation truth;
- EDA;
- model-ready features;
- target tables;
- старые GIS/Excel artifacts;
- bootstrap/reproduction scripts;
- старые validation packages.

Этот пакет **не является набором исходных реальных полевых данных**, на котором должна строиться нынешняя научная ветка.

Его current-копия удалена.

Историческая доступность обеспечивается Git history, а не поддержанием отдельного exact snapshot.

---

## 4. Что было удалено из current private resources tree

После retirement из private repo удалены current-копии старой project-generated ветки, включая:

- `08_data_archives/SKRU1_ACTUAL_DATA_TABLES_v1.zip`;
- `08_data_archives/main_repo_snapshots/SKRU1_ACTUAL_DATA_TABLES_v1/**`;
- `08_data_archives/main_repo_snapshots/inputs_bootstrap/**`;
- старые v3/v3.2 Excel workbooks;
- старые model-ready/audit ZIP;
- дубли historical project documentation;
- вспомогательные bootstrap manifests/hashes, которые были нужны только для старой migration-схемы.

В `08_data_archives/main_repo_snapshots/` оставлен только active source snapshot:

`inputs_sources/`

поскольку он относится к реальным внешним scientific sources.

Старый `SKRU1_ACTUAL_DATA_TABLES_v1.zip` более не является required artifact.

Его прежний SHA-256 сохраняется только как historical provenance record:

`edcced26f827fc1d24939eabb86d1a1cf5a41669ebe2b9e680af9a955cb733a7`

Наличие локальной копии этого ZIP в `work/resource_inbox` не требуется.

---

## 5. Физико-механический источник Барях–Асанов–Паньков

Книга/учебное пособие по физико-механическим свойствам соляных пород ВКМ первоначально была получена как ZIP с отдельными page/SVG files.

Из него был собран единый читаемый PDF:

`03_books/Baryakh_Asanov_Pankov_phys_mech_salt_rocks_VKM_merged.pdf`

Параметры файла:

- size: `96179474` bytes;
- SHA-256:  
  `3c13bddaebed84da4b363a69bf154ebbe35860cad6a194cd979574fdf4b69913`.

Исходный локальный ZIP после сборки PDF был удалён.

Это не считается потерей current evidence:

- retained PDF является рабочей читаемой копией;
- hash исходного ZIP сохранён как provenance;
- восстановление исходного ZIP не требуется.

---

## 6. Current canonical scientific state

### 6.1. Historical baseline

Historical Gate B3:

`ce8e57e`

Это первый завершённый B1/B5/B6/B7 experiment с реально оценённым IMM.

Gate B3 остаётся **исторической точкой**.

Historical scores:

- не являются результатами v2.1;
- не являются результатами будущего physical ensemble;
- не переносятся на новые datasets;
- воспроизводятся через checkout соответствующего historical commit, а не через восстановление retired v3/v3.2 package в current tree.

### 6.2. Current synthetic stress lab

Authoritative synthetic benchmark:

`SKRU1_SCENARIO_SIMULATION_V2_1`

Authoritative representation:

`SKRU1_SCENARIO_REPRESENTATION_V2_1_R1`

Frozen SHA-256:

| Объект | SHA-256 |
|---|---|
| v2.1 manifest | `a268cd7800e1320a1cc61172f95cf458c7ac7434237b32b288f23a83a97a2c95` |
| constraints | `96b88b61cf766062c1f831e32b50bdd82441da63c50ce41569a5ffad297de6ef` |
| representation | `0e5501cb4c3cff570fbd3763047a8dd8738d45706dd6f549556ac428521f5a6d` |

v2.1 остаётся:

> **publication-envelope-conditioned factorial/adversarial stress benchmark**

Он не является:

- field validation;
- digital twin;
- site-specific geomechanical calibration;
- доказательством реальной forecast accuracy на СКРУ-1.

---

## 7. Historical references внутри frozen manifests

Frozen v2.1 и другие historical manifests могут содержать ссылки на старые пути, например:

`SKRU1_ACTUAL_DATA_TABLES_v1/...`

Эти manifests **не переписываются**, поскольку изменение их contents изменило бы frozen SHA-256.

Поэтому действует правило:

> Historical provenance reference на `LEGACY_RETIRED` path не создаёт current dependency на retired package.

Canonical verifier распознаёт такие записи и помещает их в:

`retired_legacy_references_skipped`

вместо `FAIL`.

Таким образом одновременно сохраняются:

- immutable frozen manifests;
- historical provenance;
- чистое current tree;
- отсутствие ложной зависимости от retired v3/v3.2 artifacts.

---

## 8. Physical evidence после R2

После R2 был собран расширенный scientific corpus, в том числе:

- **Лебедева 2023** — СКРУ-1–СКРУ-2 / СКРУ-2–СКРУ-3, геология, Vp, свойства, FEM, маркшейдерские наблюдения;
- **Кудряшов 2013** — региональная геология, стратиграфия, тектоника и гидрогеология ВКМ;
- **Барях–Асанов–Паньков** — физико-механические свойства соляных пород ВКМ;
- **Соловьёв–Секунцов** — системы разработки, камеры/целики, междупластье, закладка, устойчивость и time-to-stability;
- **Беляков–Беликов** — опубликованный FEM reference case для ВКМ;
- **GPR 2026** — георадарная гипсометрия и observation layer;
- материалы по длительным осадкам;
- геодинамические источники;
- InSAR;
- mining technology.

Следствие:

- полноценный site-specific 3D digital twin СКРУ-1 пока не обоснован;
- evidence-backed 2D/2.5D physical case по выбранному профилю уже можно проектировать научно корректно.

---

## 9. Каноническая storage policy

### Main repo

Хранит:

- current code;
- tests;
- configs;
- current derived research artifacts;
- frozen current benchmark releases;
- manifests;
- research documents;
- governance;
- receipts.

### Private resources repo

Хранит:

- raw scientific evidence;
- books/PDFs;
- dissertations;
- source DOCX/XLSX/GIS/ZIP только если они являются scientific evidence;
- source registry;
- SHA-256/provenance;
- curated physical-evidence tables.

### Git history

Хранит:

- retired project-generated datasets;
- old v3/v3.2 reconstruction;
- old model-ready/EDA/target packages;
- obsolete bootstrap trees;
- historical execution states.

### `work/`

Используется для:

- temporary files;
- local scratch;
- resource inbox;
- generated intermediate outputs, которые ещё не признаны canonical artifacts.

---

## 10. Canonical verification

Команда полной проверки:

```powershell
$env:VKM_RESOURCES_ROOT = "E:\\Диплом\\vkm-subsidence-forecasting_resourses"
python scripts/verify_canonical_repository.py
```

После retirement и cleanup получен:

```text
status: PASS
```

Проверено:

```text
files_hash_checked_main:               197
files_hash_checked_private_resources:  12
manifests_checked:                       7
markdown_files_checked:                 50
externalized_inputs_unchecked:           0
errors:                                  0
broken_local_markdown_links:             0
models_executed:                         0
evaluator_truth_parsed:              false
```

Canonical verifier также корректно классифицировал **14 historical manifest references** как:

`retired_legacy_references_skipped`

вместо ошибки.

Три frozen current hashes успешно подтверждены:

```text
scenario v2.1:   a268cd7800e1320a1cc61172f95cf458c7ac7434237b32b288f23a83a97a2c95
constraints:     96b88b61cf766062c1f831e32b50bdd82441da63c50ce41569a5ffad297de6ef
representation:  0e5501cb4c3cff570fbd3763047a8dd8738d45706dd6f549556ac428521f5a6d
```

На момент этой проверки локально были синхронизированы:

- main: состояние после `000ad6d`;
- private resources: состояние после `283823d`.

Последующие doc-only изменения не меняют scientific frozen values.

---

## 11. Externalized Markdown links

Некоторые historical/current research Markdown-документы по-прежнему содержат относительные ссылки вида:

`../../inputs/sources/...`

Это ожидаемо: исходные source materials были externalized из main.

Verifier:

- распознаёт их как `externalized_markdown_links`;
- не считает их broken local links;
- проверяет active source bytes через private resources repository.

На последней проверке:

`broken_local_markdown_links = []`

---

## 12. Что сознательно не выполнялось

В ходе cleanup, externalization и retirement:

- model runs: `0`;
- training: `0`;
- tuning: `0`;
- solver runs: `0`;
- v2.1 regeneration: `0`;
- representation regeneration: `0`;
- evaluator truth model-selection: `0`.

Scientific values frozen current releases не изменялись.

---

## 13. Текущее направление исследования

После cleanup проект больше не возвращается к старой схеме:

```text
v3/v3.2 reconstructed dataset
→ model-ready table
→ model tuning
```

Текущая исследовательская последовательность:

```text
scientific evidence
↓
Physical Evidence Consolidation
↓
claim/source graph
↓
parameter registry
↓
Physical World v1 contract
↓
2D/2.5D OpenGeoSys reference case
↓
physical ensemble
↓
новый preregistered algorithm benchmark
```

v2.1 остаётся отдельным controlled adversarial stress lab.

Будущий physical ensemble является независимым mechanistic robustness layer.

---

## 14. Следующий этап: Physical Evidence Consolidation

Private corpus должен быть преобразован в machine-readable evidence model.

Для каждого параметра/утверждения необходимо фиксировать:

- physical variable;
- value / range / formula;
- units;
- source;
- page / table / figure;
- site/object;
- lithology/layer;
- time/depth/geometry applicability;
- evidence scope:
  - `SITE_SPECIFIC`;
  - `VKM_REGIONAL`;
  - `OTHER_VKM_SITE`;
  - `GENERAL_METHOD`;
- scale:
  - `LAB`;
  - `MASSIF`;
  - `CALIBRATED_EFFECTIVE_MODEL`;
- evidence type:
  - `MEASURED`;
  - `DERIVED`;
  - `CALIBRATED`;
  - `LITERATURE_PRIOR`;
  - `ENGINEERING_ASSUMPTION`;
- transferability to SKRU-1;
- uncertainty;
- identifiability;
- usability in OpenGeoSys;
- notes/limitations.

После этого должен быть зафиксирован `Physical World v1 contract`.

Только затем строится первый 2D/2.5D OpenGeoSys reference case.

---

## 15. Итоговое состояние

На момент этой фиксации:

```text
main repository cleanup                  COMPLETE
private scientific evidence cleanup      COMPLETE
legacy v3/v3.2 retirement                COMPLETE
canonical verifier                       PASS
frozen v2.1 hashes                       VERIFIED
active private scientific sources        VERIFIED
historical retired references            SKIPPED BY POLICY
broken local Markdown links              0
model/training/tuning runs                0
```

Этап repository cleanup / resources migration / legacy retirement считается **закрытым**.

Следующая работа — уже научная:

> **Physical Evidence Consolidation → Physical World v1 → OpenGeoSys.**

---

## 16. Зафиксированные названия

### Диплом

**«Горные и маркшейдерские работы при разработке Верхнекамского месторождения»**

### Специальная часть

**«Алгоритм прогнозирования оседаний земной поверхности на ВКМ на основе маркшейдерских измерений»**

# Консолидация основного репозитория и private evidence repository

Дата исходной фиксации: **25 сентября 2026 года**.  
Корректировка после анализа legacy package: **25 сентября 2026 года**.

> **Важно:** первоначальная externalization формулировала старые
> `SKRU1_ACTUAL_DATA_TABLES_v1` / v3.x / bootstrap packages как exact external
> archive, который нужно продолжать побайтово проверять. Последующий review
> показал, что это неверная current-policy интерпретация. Эти project-generated
> packages созданы на раннем этапе при впоследствии признанном ошибочным
> представлении о данных и теперь имеют статус **LEGACY_RETIRED**.

Авторитетное решение:
[LEGACY_DATA_RETIREMENT_2026-09-25_RU.md](LEGACY_DATA_RETIREMENT_2026-09-25_RU.md).

## 1. Роли репозиториев

### Основной

`SUKUNA-AI/vkm-subsidence-forecasting`

Хранит:

- current frozen stress benchmark v2.1;
- representation v2.1 R1;
- код/tests/configs;
- historical Gate B3 documentation;
- R1/R2;
- current reconstruction research;
- future physical-world contracts;
- governance/receipts.

### Private scientific evidence

`SUKUNA-AI/vkm-subsidence-forecasting_resourses`

Хранит актуальные научные источники:

- книги;
- статьи;
- диссертации;
- primary/secondary source files;
- GPR/InSAR/geology/geomechanics/mining evidence;
- SHA-256 registry/provenance;
- будущие curated physical-evidence tables.

Private repo **не должен быть музеем всех исторических reconstructed datasets**.

## 2. Что было удалено из current main

Во время externalization из current tree были удалены:

- `inputs/sources/`;
- `inputs/bootstrap/`;
- `SKRU1_ACTUAL_DATA_TABLES_v1/`.

После последующего review эти группы разделены по смыслу.

### Active scientific sources

`inputs/sources/**` содержит реальные внешние книги/articles/theses/source
materials. Их canonical copies в private repo остаются актуальным evidence и
проверяются по SHA-256.

### Retired project-generated packages

`inputs/bootstrap/**` и `SKRU1_ACTUAL_DATA_TABLES_v1/**` являются legacy
project-generated artifacts старой reconstructed/model-ready ветки.

Они:

- не являются current evidence;
- не нужны Physical Evidence Consolidation;
- не нужны Physical World v1 / OpenGeoSys;
- не требуют exact snapshot maintenance в current private tree;
- доступны через Git history, если понадобится историческая археология.

Старый `SKRU1_ACTUAL_DATA_TABLES_v1.zip` был успешно импортирован и его SHA
когда-то проверялся (`edcced26...33a7`), но после retirement сам archive **не
является current required artifact** и может быть удалён из current resources
branch. Его наличие в локальном `work/resource_inbox` также не требуется.

## 3. Физико-механический источник Барях–Асанов–Паньков

Рабочая читаемая копия:

`03_books/Baryakh_Asanov_Pankov_phys_mech_salt_rocks_VKM_merged.pdf`

- size: `96179474` bytes;
- SHA-256: `3c13bddaebed84da4b363a69bf154ebbe35860cad6a194cd979574fdf4b69913`.

Исходный page/SVG ZIP после сборки PDF был локально удалён. Его hash остаётся
provenance record, но восстановление ZIP не требуется.

## 4. Current canonical scientific state

### Historical baseline

Gate B3 / commit `ce8e57e` — historical B1/B5/B6/B7 validation point.

Historical scores остаются привязаны к historical commit/data state.
Точное повторение выполняется checkout соответствующего commit, а не
восстановлением retired package в current main.

### Current stress lab

`SKRU1_SCENARIO_SIMULATION_V2_1`

`SKRU1_SCENARIO_REPRESENTATION_V2_1_R1`

Frozen SHA-256:

| Объект | SHA-256 |
|---|---|
| v2.1 manifest | `a268cd7800e1320a1cc61172f95cf458c7ac7434237b32b288f23a83a97a2c95` |
| constraints | `96b88b61cf766062c1f831e32b50bdd82441da63c50ce41569a5ffad297de6ef` |
| representation | `0e5501cb4c3cff570fbd3763047a8dd8738d45706dd6f549556ac428521f5a6d` |

v2.1 остаётся publication-envelope-conditioned factorial/adversarial stress
benchmark, не field validation/digital twin.

Frozen v2.1 manifest содержит historical provenance reference на retired
`SKRU1_ACTUAL_DATA_TABLES_v1/.../survey_points.csv`. Manifest не изменяется.
После retirement ссылка не создаёт current dependency на весь legacy package.

## 5. Physical evidence after R2

Собран scientific corpus:

- Лебедева 2023;
- Кудряшов 2013;
- Барях–Асанов–Паньков;
- Соловьёв–Секунцов;
- Беляков–Беликов;
- GPR 2026;
- long-term monitoring;
- geodynamic/InSAR/mining sources.

Следствие: первый evidence-backed 2D/2.5D physical case проектируем, но full
site-specific 3D digital twin пока не обоснован.

## 6. Storage policy после корректировки

- raw **scientific evidence** -> private resources;
- current code/contracts/manifests/derived research artifacts -> main;
- retired project-generated v3.x/bootstrap packages -> Git history;
- temporary/generated work -> `work/`.

Новые источники сначала регистрируются в private `SOURCE_REGISTER.csv`.
Retired reconstructed/model-ready packages обратно не импортируются.

## 7. Canonical verification

```powershell
$env:VKM_RESOURCES_ROOT = "E:\Диплом\vkm-subsidence-forecasting_resourses"
python scripts/verify_canonical_repository.py
```

Verifier обязан проверять current frozen core и active scientific source bytes.
Он **не должен** fail из-за historical references на:

- `SKRU1_ACTUAL_DATA_TABLES_v1/**`;
- `inputs/bootstrap/**`;
- old v3.x model-ready/EDA/target files.

Такие записи являются `retired_legacy_references_skipped`.

Verifier не запускает модели и не читает evaluator truth.

## 8. Что не выполнялось

В рамках cleanup/retirement:

- model runs: 0;
- training: 0;
- tuning: 0;
- solver runs: 0;
- v2.1 regeneration: 0;
- representation regeneration: 0;
- evaluator truth model-selection: 0.

Frozen scientific values current releases не менялись.

## 9. Следующий этап

**Physical Evidence Consolidation**:

`scientific sources -> claim/source graph -> parameter registry -> Physical World v1 -> 2D/2.5D OpenGeoSys reference case -> physical ensemble -> новый preregistered algorithm benchmark`.

## 10. Зафиксированные названия

Диплом:

**«Горные и маркшейдерские работы при разработке Верхнекамского месторождения»**

Специальная часть:

**«Алгоритм прогнозирования оседаний земной поверхности на ВКМ на основе маркшейдерских измерений»**

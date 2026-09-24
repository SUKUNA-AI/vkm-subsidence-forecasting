# Консолидация основного репозитория и private evidence archive

Дата фиксации: **25 сентября 2026 года**.

Этот документ фиксирует состояние проекта после второго этапа cleanup:
крупные исходные материалы и исторические data/bootstrap containers вынесены
из current `main` в отдельный private resources repository, а основной
репозиторий оставлен как рабочее исследовательское ядро.

## 1. Репозитории и роли

### Основной

`SUKUNA-AI/vkm-subsidence-forecasting`

Назначение:

- current frozen stress benchmark v2.1;
- representation v2.1 R1;
- код и тесты;
- исторический Gate B3 как минимальная reproducibility/provenance точка;
- R1 comparator research;
- R2 adversarial source/data/physics audit;
- physical-world research и будущие contracts;
- manifests, receipts и governance.

### Private evidence/archive repository

`SUKUNA-AI/vkm-subsidence-forecasting_resourses`

Зафиксированный resources baseline на момент этой консолидации:
`941f457dc62726073f972f14cca9131ee864145d`.

Назначение:

- книги, статьи, диссертации и source PDFs;
- exact snapshots бывших source/bootstrap trees основного repo;
- исторический `SKRU1_ACTUAL_DATA_TABLES_v1`;
- original ZIP/Excel/GPKG/source containers;
- physical-evidence corpus;
- SHA-256 registry и provenance.

## 2. Что было вынесено из current main

После проверки private archive из current tree удаляются:

- `inputs/sources/`;
- `inputs/bootstrap/`;
- `SKRU1_ACTUAL_DATA_TABLES_v1/`.

Это не удаление доказательств.

Exact snapshots этих деревьев сохранены в private repo:

- `08_data_archives/main_repo_snapshots/inputs_sources/`;
- `08_data_archives/main_repo_snapshots/inputs_bootstrap/`;
- `08_data_archives/main_repo_snapshots/SKRU1_ACTUAL_DATA_TABLES_v1/`.

Кроме развёрнутого snapshot сохранён исходный архив
`08_data_archives/SKRU1_ACTUAL_DATA_TABLES_v1.zip`:

- size: `63048195` bytes;
- SHA-256:
  `edcced26f827fc1d24939eabb86d1a1cf5a41669ebe2b9e680af9a955cb733a7`.

Git history основного репозитория также сохраняет старые состояния.

## 3. Физико-механический источник

Книга/учебное пособие по физико-механическим свойствам соляных пород ВКМ
изначально была получена как ZIP с отдельными page/SVG files.

Из этого source container был собран единый читаемый PDF:

`03_books/Baryakh_Asanov_Pankov_phys_mech_salt_rocks_VKM_merged.pdf`

- size: `96179474` bytes;
- SHA-256:
  `3c13bddaebed84da4b363a69bf154ebbe35860cad6a194cd979574fdf4b69913`.

Исходный локальный ZIP после сборки PDF был удалён. Это явно записано в
private `SOURCE_REGISTER.csv`; проект не считается blocked из-за отсутствия
этого ZIP. Сохранённый PDF является рабочей копией для чтения и evidence
extraction, а hash исходного контейнера оставлен как provenance record.

## 4. Что остаётся каноническим в main

### Historical baseline

Первый полный B1/B5/B6/B7 experiment: Gate B3, commit `ce8e57e`.

Он остаётся исторической точкой. Historical scores нельзя переносить на v2.1
или будущие physical worlds.

### Current stress lab

Authoritative dataset:
`SKRU1_SCENARIO_SIMULATION_V2_1`.

Authoritative representation:
`SKRU1_SCENARIO_REPRESENTATION_V2_1_R1`.

Frozen SHA-256:

| Объект | SHA-256 |
|---|---|
| v2.1 manifest | `a268cd7800e1320a1cc61172f95cf458c7ac7434237b32b288f23a83a97a2c95` |
| constraints | `96b88b61cf766062c1f831e32b50bdd82441da63c50ce41569a5ffad297de6ef` |
| representation | `0e5501cb4c3cff570fbd3763047a8dd8738d45706dd6f549556ac428521f5a6d` |

Эта консолидация не изменяет содержимое этих frozen releases.

v2.1 остаётся publication-envelope-conditioned factorial/adversarial stress
benchmark. Это не field validation и не digital twin СКРУ-1.

## 5. Что было сделано после R2

R2 первоначально фиксировал physical-world readiness как слабую из-за нехватки
site-specific parameters, initial stress, boundary conditions и chronology.

После R2 добавлен и сохранён отдельный physical-evidence corpus, в том числе:

- Лебедева 2023 — междушахтные целики СКРУ-1–СКРУ-2/СКРУ-2–СКРУ-3,
  геология, Vp, свойства, FEM, маркшейдерские наблюдения;
- Кудряшов 2013 — региональная геология ВКМ;
- Барях–Асанов–Паньков — физико-механические свойства соляных пород ВКМ;
- Соловьёв–Секунцов — технология разработки, камеры/целики, устойчивость,
  закладка и time-to-stability;
- Беляков–Беликов — опубликованный FEM reference case;
- GPR 2026 — георадарная гипсометрия и observation layer;
- длительные наблюдения оседаний по ВКМ;
- источники по геодинамическим зонам, InSAR и горной технологии.

Следствие: полноценный site-specific 3D digital twin всё ещё преждевременен,
но первый evidence-backed 2D/2.5D physical case по выбранному профилю уже
можно проектировать научно корректно.

## 6. Новый принцип хранения

Основной repo больше не является складом source PDFs и исторических архивов.

Правило:

- raw books/PDFs/dissertations/ZIP/XLSX/GIS source containers -> private resources;
- current code/contracts/manifests/derived machine-readable research artifacts -> main;
- historical exact states -> Git history + private snapshots;
- temporary/generated work -> `work/`.

Новые источники сначала регистрируются в private `00_registry/SOURCE_REGISTER.csv`
с SHA-256, scope и provenance, и только затем используются в research contracts.

## 7. Проверка после externalization

`python scripts/verify_canonical_repository.py`

проверяет current frozen core.

Для полной проверки externalized bytes verifier использует
`VKM_RESOURCES_ROOT` или автоматически найденный private repo.

Пример:

```powershell
$env:VKM_RESOURCES_ROOT = "E:\Диплом\vkm-subsidence-forecasting_resourses"
python scripts/verify_canonical_repository.py
```

Статусы:

- `PASS` — main core и externalized archive проверены;
- `PASS_CORE_EXTERNAL_UNCHECKED` — main core корректен, но private archive
  отсутствовал локально и source bytes повторно не хешировались;
- `FAIL` — нарушен frozen hash, размер, manifest dependency или локальная ссылка.

Verifier не запускает модели и не читает evaluator truth как таблицы.

## 8. Что сознательно не делалось

В рамках этой консолидации:

- модели не запускались;
- обучение не выполнялось;
- tuning не выполнялся;
- frozen v2.1 не регенерировался;
- representation не пересобирался;
- evaluator truth не использовался для model selection;
- научные значения frozen releases не менялись.

## 9. Следующий этап

Следующий этап — **Physical Evidence Consolidation**.

Нужно превратить private corpus в machine-readable evidence model:

- parameter / variable;
- value/range/formula;
- units;
- source + page/table/figure;
- object/site;
- evidence scope;
- `LAB / MASSIF / CALIBRATED_EFFECTIVE_MODEL`;
- transferability to SKRU-1;
- uncertainty;
- identifiability;
- usability in OpenGeoSys;
- assumption status.

После этого фиксируется `Physical World v1 contract` и только затем
строится первый 2D/2.5D OpenGeoSys reference case.

## 10. Зафиксированные названия

Диплом:

**«Горные и маркшейдерские работы при разработке Верхнекамского месторождения»**

Специальная часть:

**«Алгоритм прогнозирования оседаний земной поверхности на ВКМ на основе маркшейдерских измерений»**

# Каноническое состояние исследования СКРУ-1

> **Исторический документ (состояние до scientific reset 26.09.2026).** Текущее состояние — [PROJECT_STATE_RU.md](../../PROJECT_STATE_RU.md). Относительные ссылки ниже указывают на файлы, которые живут в ветке `legacy` (`d54025d`).

Дата исходной консолидации: 24 сентября 2026 года.  
Externalization resources: 25 сентября 2026 года.  
Retirement legacy v3.x package: 25 сентября 2026 года.

Этот документ определяет текущие научные слои проекта и границы между
historical, current synthetic и будущей physical ветками.

Ключевое решение после externalization описано отдельно:
[LEGACY_DATA_RETIREMENT_2026-09-25_RU.md](LEGACY_DATA_RETIREMENT_2026-09-25_RU.md).

## 1. Историческая отправная точка

Первый законченный эксперимент с B1/B5/B6/B7 и реально оценённым IMM —
Gate B3, commit
[`ce8e57ee3dfd82579fb00c8ab5d17bf1b925c9a4`](https://github.com/SUKUNA-AI/vkm-subsidence-forecasting/commit/ce8e57ee3dfd82579fb00c8ab5d17bf1b925c9a4).

Исторический результат B7:

- temporal MAE: **6.545 мм/год**;
- coverage95: **0.962**;
- полный screening не пройден.

Это `validation_recorded`, а не final/field claim. Результаты Gate B3 относятся
только к его historical dataset/commit и не являются результатами v2.1.

Для точного повторения Gate B3 используется отдельный checkout `ce8e57e`.
Текущий `main` не обязан содержать старый reconstructed/model-ready пакет,
который использовался на том историческом этапе.

## 2. Legacy v3.x package — RETIRED

`SKRU1_ACTUAL_DATA_TABLES_v1` и связанные артефакты:

- v3/v3.2 reconstruction;
- model-ready features;
- EDA/targets;
- synthetic/evaluation-only truth;
- старые Excel/GPKG containers;
- `inputs/bootstrap/**`;
- старые audit/model-ready archives

переведены в статус **LEGACY_RETIRED**.

Причина не только в cleanup. Эти артефакты были построены на раннем этапе при
более сильном и впоследствии признанном ошибочным представлении о доступных
данных и допустимости реконструкции скрытой temporal structure.

Поэтому они:

- не являются первичными маркшейдерскими измерениями СКРУ-1;
- не являются current scientific evidence;
- не используются для Physical Evidence Consolidation;
- не являются входом Physical World v1 / OpenGeoSys;
- не поддерживаются побайтово в current trees;
- не должны автоматически реимпортироваться в private resources;
- при необходимости доступны через Git history historical commits.

Старые `configs/input_manifest.csv` и аналогичные записи сохраняются как
historical provenance, но не задают current dependency graph.

## 3. Текущий synthetic stress lab

Authoritative synthetic release:

**`SKRU1_SCENARIO_SIMULATION_V2_1`**.

Authoritative representation:

**`SKRU1_SCENARIO_REPRESENTATION_V2_1_R1`**.

Назначение: **publication-envelope-conditioned factorial/adversarial stress
benchmark**. Это не field validation, physical digital twin или
геомеханически откалиброванный dataset СКРУ-1.

| Объект | SHA-256 manifest |
|---|---|
| `v2.1` | `a268cd7800e1320a1cc61172f95cf458c7ac7434237b32b288f23a83a97a2c95` |
| constraints | `96b88b61cf766062c1f831e32b50bdd82441da63c50ce41569a5ffad297de6ef` |
| representation | `0e5501cb4c3cff570fbd3763047a8dd8738d45706dd6f549556ac428521f5a6d` |

v2.1 исправляет V2-REP-001: `31 283 / 321 766` origins имели
`days_since_previous_observation` на один день меньше. Worlds, seeds, latent
physics-like laws, targets и splits при исправлении не менялись.

v2 сохранён как hash-pinned predecessor для differential validation erratum.
Это не второй current dataset.

### Historical provenance reference внутри frozen v2.1

Immutable v2.1 manifest содержит historical reference на
`SKRU1_ACTUAL_DATA_TABLES_v1/.../survey_points.csv`.

Manifest не переписывается, потому что это изменило бы frozen SHA и нарушило
provenance уже выпущенного stress benchmark.

После retirement эта ссылка трактуется как **historical provenance reference**,
а не как требование хранить или заново воспроизводить весь retired v3.x package.
Current v2.1 проверяется как замороженный release.

## 4. R1 и R2

### R1

R1 — design review будущих neural comparators:

- compact time-aware GRU;
- residual относительно B1;
- quantile/CQR uncertainty;
- optional compact continuous-time biased Transformer;
- Neural CDE как дальняя ветка.

Нового обучения в R1 не выполнялось.

### R2

R2 — adversarial source/data/physics audit.

Вердикт v2.1: **SUPPORTED_WITH_LIMITATIONS** как factorial/adversarial stress
benchmark.

Обязательные ограничения:

- donor attribution неоднозначна;
- семь series не равны семи независимым профилям;
- `reflector_seasonal` — pure stress;
- temporal-family proportions — engineering design;
- synthetic temporal laws не являются законами реального массива ВКМ;
- historical IMM — кинематический prior, а не геомеханически откалиброванная модель.

## 5. Physical evidence branch

После R2 собран отдельный scientific evidence corpus, включая:

- Лебедева 2023 — СКРУ-1–СКРУ-2/СКРУ-2–СКРУ-3, Vp, properties, FEM, surveying;
- Кудряшов — geology/stratigraphy/tectonics/hydrogeology ВКМ;
- Барях–Асанов–Паньков — phys-mech properties salt rocks ВКМ;
- Соловьёв–Секунцов — mining systems, pillars, interbeds, backfill, stability;
- Беляков–Беликов — опубликованный FEM reference problem;
- GPR 2026;
- long-term settlement monitoring;
- geodynamic-zone, InSAR и mining-technology sources.

Этот corpus хранится в private repository:

`SUKUNA-AI/vkm-subsidence-forecasting_resourses`.

Private repo предназначен для **актуальных scientific sources/evidence**, а не
для сохранения каждой когда-либо сгенерированной версии reconstructed dataset.

Первый ограниченный evidence-backed 2D/2.5D physical case уже можно
проектировать. Полный site-specific 3D digital twin СКРУ-1 пока не обоснован.

## 6. Репозитории и storage policy

### Main

`SUKUNA-AI/vkm-subsidence-forecasting`

Хранит:

- current code/tests/configs;
- v2.1 stress lab;
- representation R1;
- current derived research artifacts;
- R1/R2;
- physical-world contracts/research;
- governance/receipts.

### Private resources

`SUKUNA-AI/vkm-subsidence-forecasting_resourses`

Хранит:

- книги;
- статьи;
- диссертации;
- primary/secondary scientific source files;
- GPR/InSAR/geomechanics/geology/mining evidence;
- SHA/provenance registry;
- future physical-evidence tables.

Retired v3.x project-generated datasets, bootstrap scripts, old Excel/model-ready
packages и `SKRU1_ACTUAL_DATA_TABLES_v1.zip` не являются current evidence и могут
быть удалены из current private tree. Git history остаётся архивом.

## 7. Canonical verifier

Проверка:

```powershell
$env:VKM_RESOURCES_ROOT = "<локальный каталог диплома>\vkm-subsidence-forecasting_resourses"
python scripts/verify_canonical_repository.py
```

Verifier должен строго проверять:

- три frozen current hashes;
- active files current v2.1/representation;
- active external scientific sources по `source_manifest.csv` и
  `supplementary_source_manifest.csv`;
- correction receipt;
- current Markdown links.

Verifier не должен fail из-за retired references:

- `SKRU1_ACTUAL_DATA_TABLES_v1/**`;
- `inputs/bootstrap/**`;
- старых v3.x model-ready/EDA/target artifacts.

Такие references должны учитываться отдельно как
`retired_legacy_references_skipped`.

Verifier не запускает модели и не парсит evaluator truth.

## 8. Следующий исследовательский этап

Следующий этап — **Physical Evidence Consolidation**.

Необходимо создать machine-readable evidence model:

- physical parameter / formula / range;
- units;
- source + page/table/figure;
- object/site/layer;
- `SITE_SPECIFIC / VKM_REGIONAL / OTHER_SITE / METHOD_GENERAL`;
- `LAB / MASSIF / CALIBRATED_EFFECTIVE_MODEL`;
- measured/derived/calibrated/assumption status;
- uncertainty;
- identifiability;
- transferability to SKRU-1;
- usability in OpenGeoSys.

Затем:

`Physical Evidence Model -> Physical World v1 -> 2D/2.5D OpenGeoSys reference case -> physical ensemble -> новый preregistered algorithm benchmark`.

Новый model benchmark до фиксации physical layer не запускается.

## 9. Зафиксированные названия

Диплом:

**«Горные и маркшейдерские работы при разработке Верхнекамского месторождения»**

Специальная часть:

**«Алгоритм прогнозирования оседаний земной поверхности на ВКМ на основе маркшейдерских измерений»**

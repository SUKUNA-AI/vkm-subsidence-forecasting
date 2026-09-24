# SKRU-1: канонический исследовательский репозиторий

Начните с [канонического состояния](docs/CANONICAL_RESEARCH_STATE_RU.md),
[фиксации консолидации 25.09.2026](docs/REPOSITORY_CONSOLIDATION_2026-09-25_RU.md) и
[решения по legacy-данным](docs/LEGACY_DATA_RETIREMENT_2026-09-25_RU.md).

| Слой | Авторитетный материал |
|---|---|
| Historical baseline | [Gate B3, commit ce8e57e](docs/reports/GATE_B3_IMM_RU.md): первый завершённый B1/B5/B6/B7 experiment; screening IMM не пройден полностью |
| Current stress lab | [SKRU1_SCENARIO_SIMULATION_V2_1](data/scenario_simulation_v2_1/manifest.json) и [SKRU1_SCENARIO_REPRESENTATION_V2_1_R1](artifacts/splits/scenario_representation_v2_1/representation_manifest.json) |
| R1 | [Neural comparator review](docs/research/NEURAL_COMPARATORS_V2_1_RESEARCH_RU.md), исследовательский дизайн без нового обучения |
| R2 | [Source/data/physics audit](docs/research/r2_adversarial_audit/01_EXECUTIVE_VERDICT_RU.md), ограничения текущих научных утверждений |
| Physical branch | [Physical-world evidence state](docs/research/PHYSICAL_WORLD_EVIDENCE_STATE_RU.md), переход к evidence-backed геомеханическим мирам |

v2.1 — publication-envelope-conditioned factorial / adversarial stress benchmark.
Это не field validation, digital twin или геомеханическая калибровка СКРУ-1.
Исторические model scores не являются результатами v2.1.

R2 зафиксировал `SUPPORTED_WITH_LIMITATIONS`: атрибуция доноров неоднозначна,
`reflector_seasonal` — pure stress, доли temporal families — engineering design.
После R2 собран дополнительный корпус по геологии, механике соляных пород,
реологии, горной технологии, GPR и геомеханическому моделированию. Поэтому
первый 2D/2.5D physical case теперь научно проектируем, но полноценный 3D
digital twin СКРУ-1 по-прежнему не обоснован.

## Репозитории

Основной репозиторий содержит код, current stress lab, frozen manifests,
исследовательские протоколы, R1/R2 и будущие physical-world contracts.

Private repository:

`SUKUNA-AI/vkm-subsidence-forecasting_resourses`

хранит **актуальные научные источники/evidence**: книги, статьи, диссертации,
source PDFs, GPR/InSAR/geomechanics materials и их SHA/provenance.

### Legacy v3.x data package

`SKRU1_ACTUAL_DATA_TABLES_v1`, старые v3.x reconstructed/model-ready/EDA tables,
bootstrap scripts и связанные проектные архивы переведены в `LEGACY_RETIRED`.
Они создавались при более раннем и впоследствии признанном ошибочным
представлении о данных и **не являются current scientific evidence**.

Они не требуются для Physical Evidence Consolidation, Physical World v1 или
OpenGeoSys и не должны поддерживаться побайтово в current trees. Исторические
версии доступны через Git history. Frozen v2.1 сохраняет некоторые старые
provenance references внутри immutable manifest; это не делает retired package
текущей зависимостью.

Подробнее: [LEGACY_DATA_RETIREMENT_2026-09-25_RU.md](docs/LEGACY_DATA_RETIREMENT_2026-09-25_RU.md).

## Начало работы

Python 3.13; существующие зависимости — [pyproject.toml](pyproject.toml) и
[requirements](requirements/).

Проверка current canonical state:

```powershell
python scripts/verify_canonical_repository.py
```

Если private resources repository находится локально, verifier автоматически
ищет его в `./vkm-subsidence-forecasting_resourses` и рядом с main repo.
Можно явно указать путь:

```powershell
$env:VKM_RESOURCES_ROOT = "E:\Диплом\vkm-subsidence-forecasting_resourses"
python scripts/verify_canonical_repository.py
```

Verifier проверяет current frozen core и active external scientific sources.
Retired v3.x project package не является blocking dependency и не должен
вызывать FAIL только из-за исторических SHA/path references.

Проверка не запускает модели и не парсит evaluator truth. Отчёт сохраняется
в `work/repo_cleanup/`. Все рабочие пути относительны корню, временные outputs
допустимы только в `work/`; frozen releases не перезаписываются.

Точное повторение первого исторического эксперимента выполняют на `ce8e57e`.
Поздние B/C runs и устаревшие отчёты доступны в Git history.

Основные правила: [AGENTS.md](AGENTS.md), [README_FIRST.md](README_FIRST.md),
[path policy](docs/governance/PATH_POLICY.md).

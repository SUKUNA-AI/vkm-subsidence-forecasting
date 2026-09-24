# SKRU-1: канонический исследовательский репозиторий

Начните с [канонического состояния](docs/CANONICAL_RESEARCH_STATE_RU.md):
оно разделяет исторический эксперимент, текущие данные и будущие исследования.

| Слой | Авторитетный материал |
|---|---|
| Historical baseline | [Gate B3, commit ce8e57e](docs/reports/GATE_B3_IMM_RU.md): первый завершённый B1/B5/B6/B7 experiment; screening IMM не пройден полностью |
| Current stress lab | [SKRU1_SCENARIO_SIMULATION_V2_1](data/scenario_simulation_v2_1/manifest.json) и [SKRU1_SCENARIO_REPRESENTATION_V2_1_R1](artifacts/splits/scenario_representation_v2_1/representation_manifest.json) |
| R1 | [Neural comparator review](docs/research/NEURAL_COMPARATORS_V2_1_RESEARCH_RU.md), исследовательский дизайн без нового обучения |
| R2 | [Source/data/physics audit](docs/research/r2_adversarial_audit/01_EXECUTIVE_VERDICT_RU.md), текущие ограничения научных утверждений |

v2.1 — publication-envelope-conditioned factorial / adversarial stress benchmark.
R2: **SUPPORTED_WITH_LIMITATIONS**. Атрибуция доноров неоднозначна;
`reflector_seasonal` — pure stress; доли temporal families — engineering design.
Готовность physical worlds — **WEAK**: site-specific параметры недостаточны.
Это не field validation, digital twin или геомеханическая калибровка СКРУ-1.
Исторические model scores не являются результатами v2.1.

## Начало работы

Python 3.13; существующие зависимости — [pyproject.toml](pyproject.toml) и
[requirements](requirements/). Для versioned binary inputs требуется Git LFS.
Перед преобразованиями проверьте manifests:

```powershell
python scripts/verify_canonical_repository.py
```

Проверка не запускает модели и не парсит evaluator truth. Отчёт сохраняется
в `work/repo_cleanup/`. Все рабочие пути относительны корню, временные outputs
допустимы только в `work/`; frozen releases не перезаписываются.

Список current reproduction scripts, hashes, historical checkout и ограничения
тестового запуска приведены в [canonical state](docs/CANONICAL_RESEARCH_STATE_RU.md).
Точное повторение первого эксперимента выполняют на `ce8e57e`; v2 сохранён
только как hash-pinned predecessor для проверки календарного erratum v2.1.
Поздние B/C runs и устаревшие отчёты доступны в Git history.

Основные правила: [AGENTS.md](AGENTS.md), [README_FIRST.md](README_FIRST.md),
[path policy](docs/governance/PATH_POLICY.md).
External resources будут отдельно организованы в private resources repository;
в этой консолидации источниковая миграция не выполнялась.

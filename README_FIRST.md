# SKRU-1: порядок работы

Текущая карта проекта:
[CANONICAL_RESEARCH_STATE_RU.md](docs/CANONICAL_RESEARCH_STATE_RU.md).

Фиксация разделения main/resources:
[REPOSITORY_CONSOLIDATION_2026-09-25_RU.md](docs/REPOSITORY_CONSOLIDATION_2026-09-25_RU.md).

Решение по старому reconstructed/model-ready пакету:
[LEGACY_DATA_RETIREMENT_2026-09-25_RU.md](docs/LEGACY_DATA_RETIREMENT_2026-09-25_RU.md).

Historical baseline: Gate B3 / `ce8e57e`.
Current stress lab: scenario v2.1 и representation v2.1 R1.
R1 — comparator research без нового обучения.
R2 — authoritative source/data/physics audit.
Следующий научный этап — Physical Evidence Consolidation.

## Важное различие

Private repository `SUKUNA-AI/vkm-subsidence-forecasting_resourses` хранит
**актуальные научные источники/evidence**: книги, статьи, диссертации, GPR,
InSAR, geology/geomechanics/mining sources и их SHA/provenance.

Старые проектные пакеты:

- `SKRU1_ACTUAL_DATA_TABLES_v1/**`;
- v3.x reconstructed/model-ready/EDA/target tables;
- `inputs/bootstrap/**`;
- связанные старые Excel/model-ready/audit archives

имеют статус **LEGACY_RETIRED**. Они были созданы при более раннем и
впоследствии признанном ошибочным представлении о данных. Это не current
scientific evidence и не вход будущих Physical World v1 / OpenGeoSys.
Для исторической археологии достаточно Git history соответствующих commits.

Frozen v2.1 manifest не переписывается, даже если содержит historical provenance
reference на retired path: такая ссылка не делает legacy package текущей
обязательной зависимостью.

До изменений прочитайте документы, перечисленные в [AGENTS.md](AGENTS.md).

Проверка current canonical state:

```powershell
$env:VKM_RESOURCES_ROOT = "E:\Диплом\vkm-subsidence-forecasting_resourses"
python scripts/verify_canonical_repository.py
```

Verifier строго проверяет current frozen core и active external scientific
sources, но не должен падать на retired v3.x/bootstrap dependencies.
Он не запускает модели и не парсит evaluator truth.

Raw scientific books/PDF/dissertations/ZIP/XLSX/GIS evidence не добавляется в
main; оно регистрируется в private resources repository. Retired project-generated
packages туда повторно не импортируются.

Временные результаты создаются только в `work/`.
Frozen releases не изменяются на месте.
Запуски моделей, tuning и solver runs требуют отдельной задачи.

v2.1 остаётся factorial/adversarial stress benchmark, а не field validation.
После evidence consolidation проектируется первый ограниченный 2D/2.5D
physical case; полноценный 3D digital twin СКРУ-1 пока не обоснован.

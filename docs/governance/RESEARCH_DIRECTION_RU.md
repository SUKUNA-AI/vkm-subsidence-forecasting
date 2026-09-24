# Текущее направление

Авторитетное состояние и границы исторических/текущих результатов:
[CANONICAL_RESEARCH_STATE_RU.md](../CANONICAL_RESEARCH_STATE_RU.md).

Решение по старому reconstructed/model-ready пакету:
[LEGACY_DATA_RETIREMENT_2026-09-25_RU.md](../LEGACY_DATA_RETIREMENT_2026-09-25_RU.md).

Фиксация layout:
[REPOSITORY_CONSOLIDATION_2026-09-25_RU.md](../REPOSITORY_CONSOLIDATION_2026-09-25_RU.md).

R2 и его ограничения:
[R2 verdict](../research/r2_adversarial_audit/01_EXECUTIVE_VERDICT_RU.md).

Главный результат спецчасти — алгоритм прогноза по истории маркшейдерских
наблюдений до следующей плановой кампании. Исходные полевые журналы и полный
site-specific GIS/3D mine model не предоставлены. Публикационная реконструкция
и v2.1 stress lab поддерживают разработку и controlled robustness testing,
но не доказывают field accuracy.

`SKRU1_ACTUAL_DATA_TABLES_v1`, old v3/v3.2 reconstruction/model-ready/EDA
packages и bootstrap branch имеют статус **LEGACY_RETIRED**. Они отражают
раннюю exploratory-ветку с впоследствии признанным ошибочным представлением о
доступных данных/temporal reconstruction. Это не current evidence и не вход
будущей physical branch. Historical reproduction выполняется через historical
Git commits, а не восстановлением этих пакетов в current tree.

После R2 собран расширенный evidence corpus по геологии ВКМ, свойствам соляных
пород, реологии, горной технологии, закладке, GPR, InSAR и геомеханическому
моделированию. Поэтому текущий следующий этап — не новое обучение, а
**Physical Evidence Consolidation**.

После него:

`evidence registry -> Physical World v1 -> 2D/2.5D OpenGeoSys reference case -> physical ensemble -> новый algorithm benchmark`.

Полный 3D digital twin СКРУ-1 пока не обоснован. Historical B3/B6/C1 metrics
не становятся результатами v2.1 или будущего physical dataset.

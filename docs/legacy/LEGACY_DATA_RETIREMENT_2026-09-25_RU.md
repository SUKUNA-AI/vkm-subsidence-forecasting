# Вывод legacy-пакета `SKRU1_ACTUAL_DATA_TABLES_v1` из текущего исследования

> **Исторический документ.** Решение о выводе v3.x-пакетов остаётся в силе; текущее состояние — [PROJECT_STATE_RU.md](../../PROJECT_STATE_RU.md).

Дата фиксации: **25 сентября 2026 года**.

## Решение

Пакет `SKRU1_ACTUAL_DATA_TABLES_v1` и связанные с ним v3.x reconstructed/model-ready/EDA/bootstrap артефакты переводятся в статус **LEGACY_RETIRED**.

Они были сформированы на раннем этапе проекта при более сильном и впоследствии признанном ошибочным представлении о доступных данных и о допустимости реконструкции временной структуры наблюдений.

Поэтому этот пакет:

- **не является текущей доказательной базой**;
- **не является набором первичных маркшейдерских измерений СКРУ-1**;
- **не используется для будущего Physical Evidence Consolidation**;
- **не является обязательным входом Physical World v1 / OpenGeoSys**;
- **не должен поддерживаться побайтово в current tree только ради старых manifests**;
- **не должен автоматически импортироваться обратно в main или private evidence corpus**.

Исторические состояния остаются доступными через Git history и соответствующие historical commits.

## Что сохраняется из старой ветки

Сохраняется только то, что имеет самостоятельное значение для истории исследования:

1. Gate B3 / commit `ce8e57ee3dfd82579fb00c8ab5d17bf1b925c9a4` как исторический baseline B1/B5/B6/B7.
2. Frozen `SKRU1_SCENARIO_SIMULATION_V2_1` как уже выпущенный controlled stress benchmark.
3. Frozen `SKRU1_SCENARIO_REPRESENTATION_V2_1_R1`.
4. R1 и R2 как исследовательские документы.
5. Текущая публикационная реконструкция и physical-evidence branch.

Historical scores остаются привязаны к historical commit/data state и не переносятся на v2.1 или будущие physical worlds.

## Важное исключение: frozen v2.1 manifest

Frozen manifest v2.1 содержит историческую ссылку на:

`SKRU1_ACTUAL_DATA_TABLES_v1/01_reconstruction_v3_2/tables/survey_points.csv`.

Manifest **не переписывается**, потому что изменение изменило бы его SHA-256 и нарушило frozen provenance выпуска.

Эта ссылка теперь трактуется как **historical provenance reference**, а не как current required input. Current v2.1 сохраняется и проверяется как уже замороженный stress-lab release; требование заново воспроизводить его из retired v3.x package снимается.

## Политика verifier

`scripts/verify_canonical_repository.py` должен строго проверять:

- frozen v2.1 manifest;
- scenario constraints;
- representation R1;
- current files, на которые ссылаются эти releases;
- active scientific source files в private resources repository;
- current Markdown links.

Он **не должен падать** из-за отсутствия или несовпадения retired путей:

- `SKRU1_ACTUAL_DATA_TABLES_v1/**`;
- `inputs/bootstrap/**`;
- старых v3.x model-ready/EDA/target/bootstrap артефактов.

Такие ссылки учитываются как `retired_legacy_references_skipped`.

## Private resources repository

Private repository `SUKUNA-AI/vkm-subsidence-forecasting_resourses` предназначен для **актуальных научных источников и evidence**, а не для музея всех когда-либо созданных reconstructed datasets.

Старый ZIP `SKRU1_ACTUAL_DATA_TABLES_v1.zip`, старые Excel/model-ready/audit packages и разложенные snapshots могут быть удалены из current resources tree. Их Git history достаточно для repository archaeology.

## Текущий научный маршрут

Актуальная ветка проекта:

`scientific sources -> evidence registry -> Physical Evidence Consolidation -> Physical World v1 -> 2D/2.5D OpenGeoSys case -> physical ensemble -> новый preregistered algorithm benchmark`.

Legacy v3.x reconstruction/model-ready package в этот маршрут не входит.

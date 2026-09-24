# Правила репозитория SKRU-1

Перед изменениями прочитать:

1. `README_FIRST.md`;
2. `docs/CANONICAL_RESEARCH_STATE_RU.md`;
3. `docs/LEGACY_DATA_RETIREMENT_2026-09-25_RU.md`;
4. `docs/REPOSITORY_CONSOLIDATION_2026-09-25_RU.md`;
5. `docs/governance/PROJECT_STATE.md`;
6. `docs/governance/PATH_POLICY.md`;
7. `docs/governance/MODEL_RESEARCH_PROGRAM.md`;
8. `docs/governance/SPECIAL_SECTION_STRUCTURE.md`;
9. `configs/experiment_protocol.yaml`;
10. `configs/acceptance_criteria.yaml`.

## Current / historical boundary

`configs/input_manifest.csv` и старые ссылки на `SKRU1_ACTUAL_DATA_TABLES_v1/**`
или `inputs/bootstrap/**` являются historical provenance старого этапа проекта.
После решения 25.09.2026 эти project-generated v3.x reconstructed/model-ready/
EDA/bootstrap артефакты имеют статус **LEGACY_RETIRED**.

Они:

- не являются current scientific evidence;
- не должны реимпортироваться в main или private evidence corpus;
- не являются обязательным входом Physical Evidence Consolidation, Physical World v1 или OpenGeoSys;
- не требуют побайтового восстановления только ради старых manifests;
- восстанавливаются при необходимости из Git history соответствующего historical commit.

Frozen v2.1/representation manifests не переписываются. Historical references
внутри immutable manifest не превращают retired package в current dependency.

## Обязательные ограничения

- current code/config paths должны быть относительными к корню репозитория;
- raw scientific books/PDF/dissertations/ZIP/XLSX/GIS evidence не добавляется в main;
- active scientific evidence хранится в private `SUKUNA-AI/vkm-subsidence-forecasting_resourses` и проверяется по SHA-256;
- retired v3.x/bootstrap project packages не добавляются обратно в current trees;
- frozen current releases не перезаписываются;
- временные результаты создаются только в `work/`;
- анализ и модели не должны использовать test/evaluator truth для подбора;
- каждое новое преобразование оставляет manifest/inventory/receipt;
- historical scores не переносятся на v2.1 или будущие physical worlds;
- model execution, tuning и solver runs требуют отдельной явной задачи.

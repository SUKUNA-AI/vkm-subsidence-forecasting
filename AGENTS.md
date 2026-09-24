# Правила репозитория SKRU-1

Перед изменениями прочитать:

1. `README_FIRST.md`;
2. `docs/CANONICAL_RESEARCH_STATE_RU.md`;
3. `docs/REPOSITORY_CONSOLIDATION_2026-09-25_RU.md`;
4. `docs/governance/PROJECT_STATE.md`;
5. `docs/governance/PATH_POLICY.md`;
6. `docs/governance/MODEL_RESEARCH_PROGRAM.md`;
7. `docs/governance/SPECIAL_SECTION_STRUCTURE.md`;
8. `configs/experiment_protocol.yaml`;
9. `configs/acceptance_criteria.yaml`.

`configs/input_manifest.csv`, `configs/source_manifest.csv` и
`configs/supplementary_source_manifest.csv` сохранены как historical provenance.
После 25.09.2026 их binary payloads externalized в private repository
`SUKUNA-AI/vkm-subsidence-forecasting_resourses`; отсутствие этих payloads в
current main не означает потерю источника.

Обязательные ограничения:

- пути current code/config должны быть относительными к корню репозитория;
- raw books/PDF/dissertations/ZIP/XLSX/GIS source containers не добавляются в main;
- external source/data bytes хранятся в private resources repository и проверяются по SHA-256;
- frozen current releases не перезаписываются;
- временные результаты создаются только в `work/`;
- анализ и модели не должны нарушать временное разбиение или использовать test/evaluator truth для подбора;
- каждое новое преобразование должно оставлять проверяемый manifest, inventory или журнал;
- historical scores не переносятся на v2.1 или будущие physical worlds;
- model execution, tuning и solver runs требуют отдельной явной задачи.

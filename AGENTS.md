# Правила репозитория SKRU-1

Перед изменениями прочитать:

1. `README_FIRST.md`;
2. `docs/governance/PROJECT_STATE.md`;
3. `docs/governance/PATH_POLICY.md`;
4. `docs/governance/MODEL_RESEARCH_PROGRAM.md`;
5. `docs/governance/SPECIAL_SECTION_STRUCTURE.md`;
6. `configs/input_manifest.csv` и `configs/source_manifest.csv`;
7. `configs/experiment_protocol.yaml`;
8. `configs/acceptance_criteria.yaml`.

Обязательные ограничения:

- пути в коде и конфигурации должны быть относительными к корню репозитория;
- входы проверяются до любых преобразований;
- исходные ZIP и первичные данные не изменяются на месте;
- временные результаты создаются только в `work/`;
- анализ и модели не должны нарушать временное разбиение или использовать тест для подбора;
- каждое преобразование должно оставлять проверяемый manifest, inventory или журнал.

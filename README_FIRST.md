# SKRU-1: порядок работы

Текущая карта проекта:
[CANONICAL_RESEARCH_STATE_RU.md](docs/CANONICAL_RESEARCH_STATE_RU.md).

Итоговая фиксация разделения main/resources:
[REPOSITORY_CONSOLIDATION_2026-09-25_RU.md](docs/REPOSITORY_CONSOLIDATION_2026-09-25_RU.md).

Historical baseline: Gate B3 / `ce8e57e`.
Current data: scenario v2.1 и representation v2.1 R1.
R1 — дизайн comparator research без нового обучения.
R2 — authoritative source/data/physics audit.
Исторические scores не переносятся на новые данные.

До изменений прочитайте документы, перечисленные в [AGENTS.md](AGENTS.md).
Проверка frozen hashes:

```powershell
python scripts/verify_canonical_repository.py
```

Для полной повторной проверки externalized source bytes укажите private repo:

```powershell
$env:VKM_RESOURCES_ROOT = "E:\Диплом\vkm-subsidence-forecasting_resourses"
python scripts/verify_canonical_repository.py
```

Без private archive verifier проверяет canonical core и выдаёт
`PASS_CORE_EXTERNAL_UNCHECKED`; это не означает, что source bytes находятся
в main repository.

Raw books/PDF/source archives и исторические bootstrap/data packages больше не
добавляются в основной repo. Они хранятся в private
`SUKUNA-AI/vkm-subsidence-forecasting_resourses`.

Временные результаты создаются только в `work/`.
Frozen datasets и manifests не изменяются на месте.
Запуски моделей требуют отдельной задачи и протокола.

v2.1 остаётся factorial/adversarial stress benchmark, а не field validation.
После расширения evidence corpus первый 2D/2.5D physical case можно
проектировать, но полноценный 3D digital twin СКРУ-1 пока не обоснован.

# ВКМ / СКРУ-1: evidence-backed 3D+time мир для прогнозирования оседаний

Дипломная работа: «Горные и маркшейдерские работы при разработке Верхнекамского месторождения».
Специальная часть: «Алгоритм прогнозирования оседаний земной поверхности на ВКМ на основе маркшейдерских измерений».
Названия фиксированы.

## Что это за репозиторий сейчас

26.09.2026 проект прошёл **scientific reset**. Раньше исследование шло по цепочке «одна скважина (№ 75) →
упрощённый 2D Physical World → модель OpenGeoSys». Теперь оно строится вокруг **WorldSpec**: доказательно
трассируемого описания мира Верхнекамского месторождения и рудника СКРУ-1 в 3D и во времени, собранного
из **всего** научного корпуса.

В WorldSpec разделены четыре слоя:

- мир — что существует;
- процессы — как их считать;
- наблюдения — что и как измеряется;
- валидация — сравнение с учётом доступности данных во времени.

Мир не зависит от решателя (решение пользователя D-18). Цепочка: evidence → интерпретация → WorldSpec →
представление под конкретный решатель. 2D-сечения — только производные представления. Роли инструментов:

- OGS + MFront — основная открытая ветка геомеханики и кастомной реологии соли;
- ANSYS Mechanical — независимая промышленная FEM-сверка на согласованных benchmark-сценариях, а не источник истины;
- gprMax — оператор наблюдения для георадара, а не модель оседаний.

Field validation — по реальным маркшейдерским, GNSS и InSAR наблюдениям в пределах их применимости.

| Где | Что |
|---|---|
| [PROJECT_STATE_RU.md](PROJECT_STATE_RU.md) | **каноническое текущее состояние** проекта: что известно, что нет, что дальше |
| [docs/worldspec/WORLD_SPEC_VNEXT_RU.md](docs/worldspec/WORLD_SPEC_VNEXT_RU.md) | архитектура мира: эпистемика, иерархия, время, наблюдения |
| [docs/governance/SCIENTIFIC_RULES_RU.md](docs/governance/SCIENTIFIC_RULES_RU.md) | научные правила: статусы, запреты подмены, прослеживаемость |
| [docs/architecture/REPOSITORY_ARCHITECTURE_RU.md](docs/architecture/REPOSITORY_ARCHITECTURE_RU.md) | структура двух репозиториев |
| `src/vkm_world/` | типизированный фундамент (pydantic): провенанс, иерархия, скважины, хронология, горные объекты, материалы, процессы, наблюдения, валидация |
| `schemas/worldspec_vnext.schema.json` | машиночитаемая JSON Schema WorldSpec |
| `evidence/` | public-safe каталоги evidence (без дословных цитат): источники и покрытие, скважины, геология, координаты, горные работы, закладка, материалы, гидро/термо, геофизика, мониторинг, жизненный цикл информации |
| `catalogues/` | каталоги процессов, математических моделей, причинный граф, дизайн операторов наблюдения |
| [docs/science/](docs/science/) | предметные отчёты синтеза (скважины, геология, горные работы, механика/реология, мониторинг, причинность …) |
| [docs/legacy/LEGACY_INDEX_RU.md](docs/legacy/LEGACY_INDEX_RU.md) | что сохранено в ветке `legacy` и как это проверить/воспроизвести |
| [docs/reset_2026_09/](docs/reset_2026_09/) | журнал run, preflight, аудиты, отчёты, run kit |

## Быстрый старт

```bash
git clone https://github.com/SUKUNA-AI/vkm-subsidence-forecasting.git && cd vkm-subsidence-forecasting
git lfs install --local && git lfs pull          # LFS используется только историческими объектами
python -m venv .venv && . .venv/bin/activate     # Python 3.13
pip install -r requirements/worldspec.lock.txt && pip install -e .
python -m pytest -q tests/world                  # все тесты должны проходить
# проверка репозитория (frozen-ссылки из git-объектов, ссылки, утечка, пути, схема):
VKM_RESOURCES_ROOT=<путь к клону PRIVATE> python scripts/verify_canonical_repository.py
```

На Windows (PowerShell): `$env:VKM_RESOURCES_ROOT = "<путь к клону PRIVATE>"`.
PRIVATE-репозиторий `SUKUNA-AI/vkm-subsidence-forecasting_resourses` хранит научные источники (PDF/DjVu/DOCX),
OCR и полные evidence-записи с цитатами (`11_evidence_vnext/`). PUBLIC никогда не содержит этих бинарников и цитат.

## Что сознательно НЕ сделано в текущей фазе

Phase 1 (cloud) завершена. Это была только информация и архитектура: полный sweep корпуса, каталоги, провенанс,
хронология, дизайн WorldSpec, схемы, лёгкие тесты, самопроверка. Модельные и статистические расчёты не выполнялись
(была только арифметика над напечатанными числами — разности дат, MD − TVD, RMS и r оцифровок Мусихина, DERIVATION):

- напряжения, ползучесть;
- интерполяция, кригинг, LOO-CV;
- 3D-реконструкция, сетки;
- OGS + MFront, ANSYS Mechanical, gprMax;
- Monte Carlo, чувствительность;
- ML, прогнозный бенчмарк.

Это задачи локальной Phase 2, см. [CLOUD_TO_LOCAL_PHASE2_HANDOFF_RU.md](CLOUD_TO_LOCAL_PHASE2_HANDOFF_RU.md).

## История

Старая архитектура целиком сохранена в ветке **`legacy`** (`d54025d`):

- synthetic stress lab v2/v2.1;
- Gate A/B/C (Kalman/IMM);
- реконструкции;
- документы Physical World v1.

Frozen-релизы проверяются из git-объектов без checkout (`scripts/frozen_references.json`).

# Правила репозитория ВКМ / СКРУ-1 (для любых агентов)

## Прочитать перед изменениями

1. `README.md`;
2. `PROJECT_STATE_RU.md` — каноническое текущее состояние;
3. `docs/governance/SCIENTIFIC_RULES_RU.md`;
4. `docs/governance/DATA_AND_PATH_POLICY_RU.md`;
5. `docs/governance/VALIDATION_POLICY_RU.md`;
6. `docs/governance/PHASE1_DESIGN_DECISIONS_RU.md`;
7. `docs/worldspec/WORLD_SPEC_VNEXT_RU.md`;
8. `docs/architecture/REPOSITORY_ARCHITECTURE_RU.md`;
9. `CLOUD_TO_LOCAL_PHASE2_HANDOFF_RU.md` — если задача относится к Phase 2.

## Граница current / legacy

Старая архитектура целиком сохранена в ветке `legacy` (`d54025d`). Это:

- synthetic stress lab v2/v2.1;
- Gate A/B/C;
- реконструкции v3.x;
- Physical World v1;
- OGS-пакеты.

В `main` она не возвращается. `SKRU1_ACTUAL_DATA_TABLES_v1/**`, `inputs/bootstrap/**` и v3.x
reconstructed/model-ready/EDA пакеты — LEGACY_RETIRED:

- не являются current evidence;
- не реимпортируются;
- восстанавливаются только из git-истории.

Frozen-релизы проверяются по git-объектам (`scripts/frozen_references.json`) и не переписываются.

## Обязательные ограничения

- Пути в коде и конфигах — относительные к корню репозитория. PRIVATE находится через `VKM_RESOURCES_ROOT`.
- Научные бинарники (PDF/DjVu/DOCX/архивы/сканы), дословные цитаты и OCR-дампы в PUBLIC не добавляются.
- Public-каталоги строятся только скриптами:
  - `scripts/build_public_catalogues.py` — из PRIVATE `11_evidence_vnext/canonical/`: удаляет колонки с цитатами,
    сокращает фрагменты ≥ 25 слов, совпадающие с цитатами, и заменяет машинные пути;
  - `scripts/build_evidence_from_legacy.py` — оцифровка Мусихина и карта старых id источников из git-объектов `legacy`.
- Отчёты синтеза публикуются в `docs/science/` инструментом `docs/reset_2026_09/run_kit/tools/publish_reports.py`
  (санитизация путей, ссылки на public-каталоги).
- Проверка `catalogue_sync` верификатора (при заданном `VKM_RESOURCES_ROOT`) падает, если PUBLIC отстал от PRIVATE
  или повторяет ≥ 25 слов цитаты подряд.
- Временные результаты — только в git-ignored `work/` или во внешнем каталоге.
- Каждое преобразование оставляет manifest или receipt (входы, команда, SHA-256 выходов).
- Сгенерированные файлы детерминированы.
- Интерполяция, реконструкция, запуски решателей, Monte Carlo, ML и бенчмарк — только по явной задаче
  соответствующей фазы (см. `CLAUDE.md`).
- Test/evaluator truth не используется для подбора параметров.
- Исторические оценки моделей не переносятся на новую архитектуру.
- Эпистемические статусы, область и масштаб не повышаются молча: ANALOGUE → SKRU1, LAB → MASSIF,
  MODEL_CHOICE → FACT запрещены без явного `Transfer` или решения в журнале решений.
- Коммиты — тематические. Без force-push и переписывания истории.

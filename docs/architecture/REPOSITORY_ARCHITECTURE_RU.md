# Архитектура репозиториев после scientific reset (Phase 1)

Архитектура выводится из доменной модели WorldSpec (`docs/worldspec/WORLD_SPEC_VNEXT_RU.md`), а не
из истории проекта. Старое состояние PUBLIC целиком сохранено в ветке **`legacy` = `d54025d`**.
В новый `main` из него переносится только то, что прошло аудит как `KEEP_CURRENT` или
`REUSE_GENERIC` (см. [PUBLIC_REPO_AUDIT_RU.md](../reset_2026_09/audit/PUBLIC_REPO_AUDIT_RU.md)).

## 1. Два репозитория — два назначения

| | PUBLIC `vkm-subsidence-forecasting` | PRIVATE `vkm-subsidence-forecasting_resourses` |
|---|---|---|
| Что хранит | код, схемы, контракты данных, **public-safe** каталоги evidence (id, локаторы, значения, статусы, короткие пересказы), документы, тесты, receipts | зарегистрированные научные бинарники (PDF/DjVu/DOCX), OCR/text-извлечения, **полные** evidence-записи с дословными цитатами, snapshot'ы sweep |
| Чего не хранит | PDF/DjVu/DOCX/архивы корпуса, колонки `quote`/`ocr_text`, дампы страниц | сгенерированные устаревшие датасеты (v3.x — LEGACY_RETIRED), venv, кэши |
| Страж | `vkm_world.governance.leakage.scan` + тест `test_public_tree_has_no_private_leakage` | `SOURCE_REGISTER.csv` + SHA-256 |

Поток данных: **PRIVATE (источник → полная запись с цитатой) → PUBLIC (запись без цитаты с локатором)**.
Обратного потока научных бинарников нет.

## 2. Структура PUBLIC `main`

```
README.md  CLAUDE.md  AGENTS.md  PROJECT_STATE_RU.md  CLOUD_TO_LOCAL_PHASE2_HANDOFF_RU.md
pyproject.toml  .gitattributes  .gitignore  .python-version
schemas/                     JSON Schema WorldSpec vNext (генерируется из кода)
src/vkm_world/               типизированный фундамент
  core/                      provenance, статусы, неопределённость, метаданные единиц, базовые объекты, io (атомарная запись, sha256)
  evidence/                  реестр источников, уровни покрытия
  spatial/                   иерархия ВКМ→…→репер, CRS/высотные системы (метаданные)
  geology/                   стратиграфия, скважины (наблюдение ≠ интерпретация), горизонты, структуры
  mining/                    горные объекты (DESIGN/ACTUAL/TEACHING), закладка
  chronology/                физические и информационные события, доступность информации
  materials/                 записи параметров материалов (LAB/MASSIF/…)
  physics/                   определения процессов, планируемые статусы исполнения
  mathmeta/                  метаданные математических моделей
  observations/              системы наблюдений, датасеты, спецификации операторов наблюдения
  worldspec/                 агрегат WorldSpec, валидация, детерминированная сериализация
  governance/                страж утечки PRIVATE→PUBLIC, санитизация машинных путей
  validation/                обобщённые стражи валидации (перенесены из legacy как REUSE_GENERIC): утечка признаков,
                             разбиения по времени, журнал доступа к test, метрики — без привязки к старым моделям
evidence/                    public-safe каталоги evidence (CSV без цитат), по доменам:
  sources/ boreholes/ geology/ coordinates/ mining/ backfill/ materials/
  hydro_thermal/ geophysics/ monitoring/ lifecycle/ qa/
catalogues/                  метаданные моделей и процессов:
  mathematics/               MATHEMATICAL_MODEL_REGISTRY.csv
  physics/                   physics_coverage_and_execution_matrix.csv
  causal/                    причинный граф (узлы, рёбра, JSON)
  observations/              дизайн операторов наблюдения
world/                       (Phase 2) экземпляры WorldSpec: EVIDENCE_POPULATED без интерполяций, затем DIAGNOSTIC
scripts/                     сборка public-каталогов из private evidence, экспорт схемы, проверка репозитория
tests/world/                 лёгкие тесты схем/провенанса/хронологии/утечки
docs/
  worldspec/                 спецификация мира
  science/                   предметные отчёты (lifecycle, скважины, геология, горные работы, механика/реология,
                             гидро/термо/геофизика, мониторинг, OCR-QA, цитирование, математика, физика, причинность)
  architecture/              этот документ; контракты адаптеров решателей и визуализации
  governance/                научные правила, политика данных и путей
  reset_2026_09/             preflight, журнал run, run kit, аудиты, воспроизводимость, финальный отчёт Phase 1,
                             исторический checkpoint первой cloud-сессии
  legacy/                    индекс того, что живёт в ветке legacy, и исторические state-документы до reset
```

Зарезервирован, но не создан в Phase 1: `src/vkm_world/solver_adapters/`. Контракт: адаптер получает срез
WorldSpec и возвращает производный объект с провенансом. Сравнение предсказанных и реальных наблюдений
с учётом доступности во времени строится в Phase 2+ поверх `validation/`. Сейчас там только обобщённые стражи:
разбиения, журнал доступа к test, метрики, проверка утечки признаков.
План следующей фазы — `CLOUD_TO_LOCAL_PHASE2_HANDOFF_RU.md` в корне.

## 3. Структура PRIVATE (дополнения Phase 1)

```
00_registry/                 SOURCE_REGISTER.csv (канон), state-документы, receipts
01_…06_*/                    зарегистрированные источники (не изменяются)
10_physics_evidence/physical_evidence_v1/   предыдущий evidence-релиз (сохраняется как есть; классифицирован)
11_evidence_vnext/
  sweep_raw/<SID>/<chunk>/   сырые выходы чтецов (records.jsonl, progress.json, coverage.json)
  canonical/                 канонические каталоги С цитатами (источник для public-версий)
  receipts/                  протоколы, manifest, проверки
work/                        git-ignored: OCR, рендеры, временное
```

## 4. Правила эволюции

1. Каталог попадает в PUBLIC только через скрипт, который удаляет колонку `quote` и проверяет утечку.
2. Любой производный объект (интерполированный горизонт, сетка, 2D-сечение, результат решателя) —
   это новая запись со статусом INTERPOLATION/DERIVATION, ссылками на входы и списком MODEL_CHOICE.
   Исходные наблюдения он не перезаписывает.
3. Решатель-специфичные представления мира не хранятся как «мир». Хранятся только срезы на вход
   и производные результаты.
4. Frozen-релизы старой архитектуры (v2.1, Gate B3) не переписываются; они доступны в `legacy` по SHA.

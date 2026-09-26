# Журнал ночного UltraCode-run (scientific reset → WorldSpec vNext)

Журнал ведётся по этапам, чтобы работа была восстановима при прерывании cloud VM.
Рабочие (не коммитящиеся) материалы run: `/home/user/work/` (corpus text, OCR, выходы агентов).

## Этап 0. Preflight (завершён)

- Исходные SHA: PUBLIC `d54025d`, PRIVATE `482bec0`; оба дерева clean.
- Создана и запушена ветка `legacy` = `d54025d` (PUBLIC).
- Установлены git-lfs, poppler, djvulibre, tesseract-rus, pandoc, LibreOffice, OSMesa;
  создан `.venv-research` (Python 3.13.12).
- LFS materialized в обоих репозиториях; canonical verifier PUBLIC = PASS.
- Тесты PUBLIC до reset: 95 passed / 3 failed / 60 errors (retired-данные).
- Полный OCR VKM-SRC-025 (198 стр.) и VKM-SRC-037 (236 разворотов).
- Отчёт: `docs/reset_2026_09/PREFLIGHT_REPORT_RU.md`.

## Этап 1. Полный повторный sweep корпуса (в работе)

- Протокол чтения: единый (`SWEEP_PROTOCOL.md` в рабочей папке run; копия будет
  сохранена в PRIVATE вместе с результатами).
- 66 чтецов по чанкам 39 документов, 4 параллельных workflow
  (S1 геология/регион/напряжения/аналоги, S2 СКРУ-1/механика, S3 технология добычи,
  S4 мониторинг/геофизика/норматив 1992).
- Параллельно: аудит PUBLIC repo, классификация старого PW v1/OGS, reproducibility-аудит.

### Текущие гипотезы
- Скважина 75 была model choice reduced-case; корпус (Лебедева, Кудряшов, Филатова,
  норматив 1992) содержит больше пространственных данных, чем использовалось.
- Информационный жизненный цикл (когда данные доступны) ранее не моделировался явно.

### Открытые blockers
- VKM-SRC-013: исходный ZIP удалён (провенанс сохранён; рабочая копия VKM-SRC-025).
- VKM-SRC-022: LEGACY_RETIRED, намеренно отсутствует.

## Остановка и checkpoint (26.09.2026, ~09:40 UTC) — по указанию пользователя

- Пользователь попросил прекратить запуск новых workflow и подготовить перенос на локальную workstation.
- Все 6 workflow остановлены (`TaskStop`): 4 sweep, repo audit, math wave 1. Ни один chunk-агент
  не успел записать результаты: **0/65 чтений завершено** (план — 65 чтений; ранее в чате ошибочно
  названо 66).
- Math wave 1: RHEO завершился пустым отказом (harness переслал агенту вопрос пользователя о сроках
  как «единственный голос пользователя»); MECH/STRESS остановлены; SUBS/OBS/GEOM/PHYS не запускались.
- Сетевая политика cloud-окружения блокирует `lfs.github.com` (403): LFS-zip с OCR не запушен
  (локальный commit PRIVATE `292655a`). OCR-текст передан обычными txt-файлами.
- Создан `CLOUD_TO_LOCAL_HANDOFF_RU.md` (корень PUBLIC; копия в PRIVATE
  `00_registry/cloud_checkpoint_2026-09-26/`). Следующий этап — Stage 1 локально.

---

# PHASE 1 (cloud): информационная и архитектурная часть — продолжение reset

Parent task: «CLOUD ULTRACODE — ПРОДОЛЖЕНИЕ SCIENTIFIC RESET ПРОЕКТА VKM/SKRU-1».
Стратегия: в cloud — только чтение, извлечение, провенанс, каталоги, дизайн WorldSpec,
архитектура репозитория, лёгкие схемы и тесты. Тяжёлые вычисления отложены в локальную Phase 2.

## P1.0 Старт (выполнено)

- Исходные SHA: PUBLIC `main` = `1c1f816`, PRIVATE `main` = `9e4bf11`; `legacy` = `d54025d` не трогается.
- Рабочие ветки: `research/evidence-worldspec-reset` в обоих репозиториях (от актуального `main`), запушены.
- VM та же, что в предыдущем run: постраничный текст 39 документов, OCR, дайджесты старых claims и
  240 отрендеренных страниц сохранились — подготовка не повторялась.
- Новый протокол `SWEEP_PROTOCOL_PHASE1.md`: инкрементальное сохранение каждые ≤5 страниц
  (`progress.json` + дозапись `records.jsonl`), запрет численных расчётов, добавлены виды записей
  `information_lifecycle` и `lifecycle_stage`.
- Для каждого агента — явная context preamble (parent task, проект, шаг; промежуточные вопросы
  пользователя задачу не отменяют).

## P1.1 Полный sweep корпуса (в работе)

- 65 чтений / 39 документов, 5 параллельных workflow по 13 чтений (лимит VM — 2 агента на workflow).
- Промежуточные результаты: `/home/user/work/sweep_results/<SID>/<chunk>/`; фоновый snapshot каждые
  40 мин в PRIVATE `11_evidence_vnext/sweep_raw/` (commit + push, без LFS).
- Параллельно: read-only аудит PUBLIC repo, старого PW v1/OGS и воспроизводимости.

### P1.1a Перепаковка sweep (≈11:05 UTC)

- Плотные источники читались ~5 мин/страницу (Кудряшов, норматив 1992); при 2 агентах на workflow группы A/D
  заняли бы 10–12 ч. Загрузка VM была низкой (load ≈ 1), поэтому 5 workflow остановлены и 64 оставшихся
  чтения перепакованы в 12 workflow (R01–R12, ≈24 агента одновременно). Незавершённые чтения продолжаются
  с `progress.json` (протокол требует resume), потеря — не более одной пачки страниц.
- Готово к этому моменту: VKM-SRC-040 c2, VKM-SRC-012 c1.

## P1.2 Фундамент и дизайн (выполнено параллельно со sweep)

- `src/vkm_world/**`: типизированная схема WorldSpec vNext (pydantic), 29 тестов (`tests/world`), JSON Schema
  `schemas/worldspec_vnext.schema.json` (commit `e9f3a57`).
- `docs/worldspec/WORLD_SPEC_VNEXT_RU.md` (`d353c84`); `docs/governance/SCIENTIFIC_RULES_RU.md`,
  `DATA_AND_PATH_POLICY_RU.md`, `docs/architecture/REPOSITORY_ARCHITECTURE_RU.md` (`fb460de`).
- Подготовлены: сборщик `SOURCE_COVERAGE_MASTER` (детерминированные правила уровней покрытия),
  синтез-workflow (10 потоков), `scripts/build_public_catalogues.py` (PRIVATE canonical → PUBLIC без цитат).

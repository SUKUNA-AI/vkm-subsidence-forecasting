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

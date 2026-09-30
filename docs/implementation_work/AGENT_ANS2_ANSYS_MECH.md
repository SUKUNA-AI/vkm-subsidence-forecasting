# Агент ANS2: Mechanical, Workbench и optiSLang в `vkm-ansys` — статус WIP (пауза 28.09.2026)

Работа приостановлена по указанию пользователя: Ansys на паузе. Ветка `claude/agent-ans2-ansys-mech-2026-09-28`,
WIP-коммит. План — [ENGINEERING_TOOLS_MCP_PLAN_RU.md](../planning/ENGINEERING_TOOLS_MCP_PLAN_RU.md), §2 и §4.

## Что сделано (код, без живых лицензионных запусков)

Три необязательных модуля с `register(server, ctx)` по контракту `vkm_ansys.context` агента ANS:

| Модуль | Tools |
|---|---|
| `vkm_ansys.mechanical` | `mechanical_status`, `mechanical_api_search` (офлайн-индекс `ansys-mechanical-stubs`), `mechanical_run_script` (движки: встроенный PyMechanical; batch IronPython; batch CPython), `mechanical_import_geometry` (CAD-файл или TOY-блок STEP), `mechanical_mesh`, `mechanical_solve`, `mechanical_results`, `mechanical_project_summary` |
| `vkm_ansys.workbench` | `workbench_status`, `workbench_run_journal` (`RunWB2 -B -R`), `workbench_project_summary`, `workbench_project_update`, `workbench_project_archive` |
| `vkm_ansys.optislang` | `optislang_status`, `optislang_node_types`, `optislang_run` (гейт: `authorized_by`), `optislang_project_summary`, `optislang_results` |

Общие части: `product_jobs` (запрос задания, ссылки `job:<job_id>/<путь>`, проверки, `params` со статусом, чтение
фич лицензий через `lmutil lmstat -a` — только имена и счётчики), `product_submit` (адаптер к `JobsService` из
`vkm_jobs` агента MAT), `product_schema`, `entry_kit`. Точки входа заданий автономны и копируются в `in/` с SHA-256.
Перед стартом продукта вход берёт машинную блокировку `vkm_ansys.licence_lock` (пул `ansys` агента ANS) и держит её,
пока дерево процессов продукта не завершится. Каждый исполняющий tool умеет `dry_run`.

## Что проверено

- Один запуск без лицензии: встроенный Mechanical (PyMechanical 0.13.3, Python 3.13) в режиме read-only, помощник
  `summary`. Старт 5,6 с, всего 12,5 с, код выхода 0, список лицензий Mechanical прочитан.
- Индекс API: 93 838 записей stubs v261, построение меньше 1 с.
- `lmstat` (только чтение): 1321 фича; для Mechanical и optiSLang фичи есть.
- Находки для пользователя:
  - клиент лицензирования `ansyscl.exe` (дочерний процесс Mechanical) слушает порт не на loopback. Это установка
    лицензирования, а не наша настройка: в отчёте задания он считается отдельно (`non_loopback_licensing_listeners`).
    Ограничить его может только пользователь (брандмауэр);
  - Mechanical запускает `apip-standalone-service.exe` (программа улучшения продукта Ansys); настройки не трогались.

## Что осталось

1. Слить ветки ANS (`claude/agent-ans-ansys-mcp-2026-09-28`) и MAT (`claude/agent-mat-matlab-mcp-2026-09-28`), когда
   там появятся коммиты. Сверить `product_submit` с итоговым API `vkm_jobs` и контекста ANS. Экстра `ansys` и
   lock-файл ведёт ANS.
2. Юнит-тесты без Ansys (`tests/engineering/`): набор tools и схемы, `dry_run`, сборка заданий, STEP-генератор
   (ориентация граней), индекс stubs на фикстуре, точки входа на фейковых `App` и `Optislang`.
3. Живые smoke на TOY-задачах через задания (§4.7):
   - Mechanical: блок 1×1×10 м под собственным весом, скользящие опоры снизу и с боков (одноосная деформация).
     Аналитика: u_z(верх) = −ρgH²(1+ν)(1−2ν)/(2E(1−ν)), σ_z(низ) = −ρgH, реакция = ρgV. Цепочка import → setup-скрипт →
     mesh → solve → results → summary, плюс batch IronPython/CPython и скрипт с ошибкой;
   - Workbench: журнал создаёт Static Structural, затем update, save и archive;
   - optiSLang: Sensitivity (ALHS, 30 точек) по трём параметрам, y = x1 + 2x2 + 0·x3. Проверка — линейная подгонка
     (1; 2; 0).
4. Receipt `docs/corpus_platform/receipts/ansys_mech_smoke.json` со строкой в README, строки в `MCP_TOOLS.md`.

## Как продолжить

- Установок не нужно: в `work/venv-ansys` (его создал ANS) уже есть PyMechanical 0.13.3, PyWorkbench 0.14.0 и
  PyOptiSLang 1.6.1.
- `VKM_SIM_ROOT` (ASCII) должен быть общим с ANS и MAT: пул `ansys` в `vkm_jobs` лежит под ним. Машинная блокировка
  от него не зависит.
- Имена tools взяты из постановки задачи (`mechanical_*`, `workbench_*`, `optislang_*`). В плане §4.3 они названы
  `mech_run_script`, `wb_run_journal`, `osl_run`.

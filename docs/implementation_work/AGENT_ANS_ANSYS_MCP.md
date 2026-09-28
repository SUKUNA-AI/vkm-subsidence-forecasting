# Агент ANS: `vkm-ansys` (MAPDL + DPF) — статус работ

Статус на 28.09.2026: **ПАУЗА** по решению пользователя (быстрые проверки гипотез ведёт OGS, Ansys продолжим позже).
Процессов Ansys от агента нет, лицензионный замок свободен. Задача и план — [ENGINEERING_TOOLS_MCP_PLAN_RU.md
§4](../planning/ENGINEERING_TOOLS_MCP_PLAN_RU.md). Пути — только логические: `<VKM_SIM_ROOT>`, `<ANSYS_ROOT>`.

## Что сделано и работает

| Часть | Состояние |
|---|---|
| `work/venv-ansys` (основной клон, Python 3.13) | PyMAPDL 0.74.1, PyDPF-Core 0.16.1, PyMechanical 0.13.3, PyWorkbench 0.14.0, PyOptiSLang 1.6.1, mcp 2.2.0, pytest 9.1.1; пакет проекта editable (static pth на `src` основного клона) |
| каркас `vkm_ansys` (коммит `efd90ef`) | сервер stdio, конверт `vkm-ansys.result/1`, хук `register(server, ctx)` для `vkm_ansys.mechanical`, `.workbench`, `.optislang` (ошибки модулей не валят сервер), `ctx.licence_lock`, `ansys_status` (проверен живьём), контрактные тесты |
| общий лицензионный замок | `vkm_ansys.licence_lock`: OS-блокировка файла `<pool>.lock` в `VKM_LOCK_DIR` или `%LOCALAPPDATA%/vkm/locks` — не зависит от `VKM_SIM_ROOT`, снимается ОС при смерти держателя; ANS2 берёт тот же замок |
| `mapdl_batch` (WIP) | обёртка задания: замок на всё время жизни MAPDL, `ANSYS261.exe -b -smp -np 4 -s noread`, Job Object, свой таймаут, отказ на `/SYS` без `allow_sys`, разбор `.out`/`.err`, пост-шаг, удаление scratch по белому списку, коды выхода (75 — лицензия, 124 — таймаут) |
| `mapdl_out` (WIP) | итоги MAPDL (числа ошибок и предупреждений из самого `.out`), сборка, время лицензии; подшаги; `.mntr` (попытки → бисекции); `*VWRITE`-таблицы; `PRNSOL`; эхо колоды не считается сообщениями |
| `ladder` S01–S09 (WIP) | колоды `ladder/Sxx.inp` + заголовок параметров (TOY, ENGINEERING_ASSUMPTION), пост-обработка → `out/results.json`, аналитические проверки вида плана §2.3 (`checks.py`) |
| `dpf_job` (WIP) | DPF in-process в контексте Entry: `extract` (U/S/EPEL/EPPL/EPCR, выборки all / named selection / top / box / nodes, наборы all / last / времена), `subsidence` (w(x, y, t), профили, максимум; DERIVATION от MODEL_RESULT), `s08`, `operators` |

Живые прогоны разработки (обёртка без MCP, под замком, `<VKM_SIM_ROOT>/dev/`); все проверки пройдены, результаты —
MODEL_RESULT на игрушечных задачах:

| Ступень | Проверка | Факт |
|---|---|---|
| S01 | `*GET ACTIVE REV` = 26.1 | 26.1, exit 0 |
| S02 | uz(верх) = −pL/E, SZ = −p, SX = SY = 0 | отн. ошибка 2·10⁻¹⁵ |
| S03 | две зоны, одноосная деформация: uz = −p(L1/M1 + L2/M2) | 2·10⁻¹⁴ |
| S04 | гравитация: uz(z), SZ(z), SX/SZ = ν/(1−ν) | 1,6·10⁻¹³; 1,5·10⁻¹⁶; 1,7·10⁻¹⁶ |
| S05 | INISTATE + гравитация → равновесие | max\|u\| = 1,6·10⁻¹⁴ м |
| S06 | два шага нагрузки с TIME, 6 подшагов | 7·10⁻¹⁶ |
| S07 | Нортон (TBOPT 10): ε_cr = C1σ^C2(t − t₀) | 3,8·10⁻⁵ (допуск 10⁻³), без бисекций |
| S08 | DPF против `*VGET` и `PRNSOL` (S02, S04) | 8,7·10⁻¹⁷; PRNSOL 6·10⁻⁶ |
| S09 | слои, EKILL камеры, ползучесть, мульда | 44 с; 12 подшагов, 0 ошибок, 0 бисекций; равновесие 2,5·10⁻¹⁵ м; симметрия 3,7·10⁻¹⁵; рост монотонный; 1,11 → 1,31 мм |

Один прогон MAPDL ≈ 26 с (почти всё — старт и лицензия).

## Что осталось

1. Влить `vkm_jobs` (ветка `claude/agent-mat-matlab-mcp-2026-09-28`, когда появится `src/vkm_jobs/`) и запускать
   `mapdl_batch`/`dpf_job` через его runner: receipts `vkm.sim_receipt/1`, статусы, очередь пула, `job_*`.
2. MCP-tools: `mapdl_run`, `dpf_extract`, `ansys_ladder_list`/`run`, `mapdl_convergence`, `ansys_subsidence`,
   `ansys_license_status` (`lmutil lmstat`, без имён и хостов), `mapdl_command_help` (docstring PyMAPDL),
   `dpf_operators`; сессии `mapdl_session_*` (PyMAPDL, WNUA, проверка loopback) и `ansys_run_python`.
3. S08: для S02 `same_node_set = false` — разбор `PRNSOL` теряет узлы на постраничной выдаче (39 из 99); остальные
   сравнения S08 пройдены.
4. Unit-тесты разборщиков, лестницы и политики колод; live-тесты за `VKM_ANSYS_LIVE=1`; smoke §4.7; receipt
   `docs/corpus_platform/receipts/ansys_mcp_ladder.json` (через MCP-tools, без машинных путей); раздел в
   [MCP_TOOLS.md](../corpus_platform/MCP_TOOLS.md), строка README, `requirements/ansys.lock.txt`, записи `.mcp.json`
   для координатора; область `src/vkm_ansys/` в `test_public_hygiene`.
5. Наблюдение: DPF в контексте Entry на секунды запускает клиент лицензий `ansyscl` (процесс завершился сам) —
   проверить по `lmstat`, что лицензия не берётся.

## Как продолжить

- Интерпретатор — `work/venv-ansys` основного клона; для кода из worktree — `PYTHONPATH=<worktree>/src`.
- Окружение: `VKM_SIM_ROOT` — ASCII-путь без пробелов на диске данных, вне клонов; `AWP_ROOT261` уже задан.
- Ступень без MCP: создать `<job>/in/input.inp` из `ladder.deck_text(step, ladder.resolve_params(step, {}))` и
  `<job>/in/mapdl_spec.json` (`MapdlSpec(post={"kind": "ladder", "step": step, "params": ...})`), затем
  `mapdl_batch.run_job(<job>)` и `checks.evaluate_all(ladder.expected_checks(step, params), <job>)`. S08 —
  `dpf_job.run_extract(<job>, {"mode": "s08", "s02_rst": ..., "s04_rst": ...})`.
- Любой запуск продукта — только под `ctx.licence_lock(...).hold(timeout)`: так MAPDL-лестница и прогоны ANS2 идут
  строго по очереди.

# AGENT MAT — MCP для MATLAB: статус на паузе (28.09.2026)

Ветка `claude/agent-mat-matlab-mcp-2026-09-28` (от `claude/corpus-platform-v0-2026-09-28`, `fa956fb`). План —
[ENGINEERING_TOOLS_MCP_PLAN_RU.md](../planning/ENGINEERING_TOOLS_MCP_PLAN_RU.md) §2, §3. Работа над MATLAB
**поставлена на паузу по решению пользователя**; `.mcp.json` и настройки Claude Code не менялись.

## 1. Что готово

| Что | Состояние | Коммиты |
|---|---|---|
| общий слой `vkm_jobs` (план §2): корень `VKM_SIM_ROOT` и jail путей, спецификация `vkm.sim_job/1`, detached runner (`python -m vkm_jobs.runner`) — переживает перезапуск MCP-сервера, Windows Job Object `KILL_ON_JOB_CLOSE` (запуск suspended → assign → resume), таймаут и отмена с убийством всего дерева, пулы лицензий (OS-блокировки, FIFO по времени подачи), gate по pid-файлам сессии, receipts `vkm.sim_receipt/1` (логические пути, SHA-256 входов и выходов, checks, `MODEL_RESULT`), 7 общих MCP-tools заданий и конверт ответа | **готово**, API описан в docstring `src/vkm_jobs/__init__.py` — агент Ansys может мержить ветку | `9f3dbb6`, `90eaf88`, `d3235cb`, `9d2e684` |
| тесты `tests/engineering/` на фейковом приложении (`python -c`): состояния, дерево процессов, пулы, LOST, jail, grep, receipts, публикация, MCP-контракт | Windows venv: 34 passed; WSL: 32 passed, 1 skipped (нет `mcp`) | те же |
| гигиена: области engineering в `test_public_hygiene`; `leakage.scan` читает `.m`, `.inp`, `.mac`, `.wbjn`; LF для них в `.gitattributes` | готово | `d3235cb`, `9f3dbb6` |
| официальный MATLAB MCP Server v0.14.0 (`matlab-mcp-server-windows-x64.exe`, 19 502 944 байт) | скачан из GitHub Releases, SHA-256 `6697a8962f148628f11d93b36960235871de5614905d6a27c55341c2b83f4118` совпадает с digest ассета релиза, подпись Authenticode The MathWorks, Inc. — Valid; лежит вне клона: `<VKM_SIM_ROOT>/tools/matlab-mcp-server/0.14.0/`; проверены только `--help` и `--version`, MATLAB через него не запускался | — |
| `src/vkm_matlab/detect.py` (WIP): корень MATLAB, VersionInfo, продукты из `appdata/products`, имена фич лицензий из данных установки, пин официального сервера с проверкой SHA-256, процессы MATLAB — без запуска MATLAB | черновик, без тестов | этот коммит |

Проверено на рабочей станции (игрушечные запуски `matlab -batch`, только для разработки tools): launcher ждёт MATLAB, код
выхода передаётся (3 → 3), stdout перехватывается; `codeIssues` даёт таблицу `Location, Severity, Fixability,
Description, CheckID, LineStart, LineEnd, ColumnStart, ColumnEnd, FullFilename` (0,7 с);
`requiredFilesAndProducts` ≈ 2,7 с; `fit` + `confint` работают; `print` 16 × 10 см при 300 dpi → 1890 × 1182 px;
`quantile` — в базовом MATLAB; `license('inuse')` — только имена фич; `canUseGPU` = 0 в `-batch`.

## 2. Что осталось (порядок возобновления)

1. `vkm_matlab`: точка входа `entry/vkm_job_entry.m` (`in/payload.json` → `out/result.json`, `error.json`,
   `meta.json`, рисунки; аргументы функции — JSON-строки по одной), `batch.py`, `service.py`, `mcp_server.py`:
   `matlab_run`, discovery (`matlab_status`, `matlab_toolboxes`, `matlab_which`, `matlab_help`), typed
   (`matlab_test`, `matlab_lint`, `matlab_fit`, `matlab_profile`, `matlab_ensemble_summary`, `matlab_figure`;
   `matlab_nn_train` → `GATE_CLOSED`).
2. Пакет проекта `matlab/+vkm` (`+mcp`: `session_eval`, `session_run_file`, `toolboxes`, `which_all`, `help_text`,
   `run_tests`, `lint`; `+fit`, `+theory`, `+ens`, `+fig`) и `matlab/tests`.
3. `python -m vkm_matlab.setup_session`: `<VKM_SIM_ROOT>/matlab/{session/startup.m, code/, vkm_tools.json}` для
   `--extension-file`; smoke официального сервера (`detect_matlab_toolboxes`, `evaluate_matlab_code`,
   `vkm_session_eval`).
4. Smoke по §3.6 за флагом `VKM_MATLAB_LIVE=1`; квитанция `docs/corpus_platform/receipts/matlab_mcp_smoke.json` и
   строка в README квитанций; раздел в `MCP_TOOLS.md`; записи `.mcp.json` — координатору.
5. Небольшое расширение `vkm_jobs` (обратно совместимое): поле `embed_json` в `JobSpec` — встраивать короткий
   `out/error.json` в receipt (идентификатор ошибки MATLAB).

## 3. Как возобновить

- Переключиться на ветку; тесты: `python -m pytest -q tests/engineering` в `work/venv-desktop` (Windows).
- `VKM_SIM_ROOT` — ASCII-каталог на диске данных рабочей станции (создан); бинарник официального сервера уже там —
  повторно не скачивать, `detect.OFFICIAL_SERVER` хранит пин версии и SHA-256.
- Правила: пути в отслеживаемых файлах только логические (`<VKM_SIM_ROOT>`, `<MATLAB_ROOT>`); запуски MATLAB — только
  игрушечные задачи приёмки tools; без моделирования СКРУ-1.

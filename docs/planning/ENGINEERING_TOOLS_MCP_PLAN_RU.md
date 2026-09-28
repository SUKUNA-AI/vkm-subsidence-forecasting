# MCP-инструменты для MATLAB и Ansys: план реализации (Phase 2)

Проект агента M от 28.09.2026. Работа шла только в режиме чтения и исследования: ничего не установлено и не скачано,
решатели не запускались. MATLAB запускался один раз, только для печати версий.

Опоры:
- решение D-19 ([PHASE1_DESIGN_DECISIONS_RU.md](../governance/PHASE1_DESIGN_DECISIONS_RU.md)): Ansys — основной решатель,
  OGS + MFront — проверка;
- недели и гейты — [PHASE2_PLAN_OCT_NOV_2026_RU.md](PHASE2_PLAN_OCT_NOV_2026_RU.md);
- лестница решателя — [CLAUDE.md](../../CLAUDE.md);
- стиль серверов и подключение — [MCP_TOOLS.md](../corpus_platform/MCP_TOOLS.md);
- пути — [DATA_AND_PATH_POLICY_RU.md](../governance/DATA_AND_PATH_POLICY_RU.md). В документе только логические имена:
  `<MATLAB_ROOT>`, `<ANSYS_ROOT>` (каталог `v261`), `<ACAD_INSTALL_DIR>`, `$VKM_SIM_ROOT`, `$VKM_WORK`.

Цель — Claude пользуется всей программой, а не набором выбранных операций. У каждого приложения три слоя:
1. **универсальный канал (полный API)** — любой код, колода, скрипт или журнал. Каждый запуск — задание с receipt
   (вход, команда, лог, код выхода, выходы, проверки), таймаутом и одной лицензионной сессией за раз;
2. **типизированные tools** — ступени лестницы решателя и частые операции диплома;
3. **discovery** — версии, продукты, тулбоксы, лицензии, справка по API.

## 0. Итог

| Приложение | Рекомендация | Почему | Часы |
|---|---|---|---|
| MATLAB R2025b | гибрид: официальный **MATLAB MCP Server** (MathWorks, v0.14.0) для интерактивной сессии и наш **`vkm-matlab`** для заданий `matlab -batch` с receipt, typed и discovery | официальный сервер даёт весь MATLAB в постоянной сессии, поддерживается MathWorks и не требует Python; Engine API для R2025b требует Python ≤ 3.12, а проект на 3.13; `-batch` даёт чистый процесс, код выхода и лог при старте около 7 с | 17 |
| Ansys 2026 R1 | свой **`vkm-ansys`** поверх PyMAPDL, PyMechanical, PyWorkbench, PyDPF и PyOptiSLang | официальные PyMAPDL-MCP 0.3.0 и PyMechanical-MCP 0.2.1 — Alpha на стеке fastmcp 3–4 (не наш `mcp` 2.2.0); они исполняют произвольный Python в процессе сервера без receipt, таймаутов и блокировки лицензии | 48,5 |
| общий слой `vkm_jobs` | задания, receipts, пулы лицензий, таймауты | нужен обоим серверам | 8 |
| AutoCAD / Civil 3D 2026 | `vkm-cad` v1 делает отдельный агент; здесь только находки (§5) | — | — |

Всего ≈ 74 ч работы агентов. Критический путь до гейта «Ansys на эталонной задаче» (19.10) ≈ 36 ч (§11).
Одобрение пользователя нужно для двух загрузок (§10): бинарник MATLAB MCP Server (19,5 МБ) и окружение PyAnsys
(≈ 190–200 МБ).

## 1. Факты рабочей станции (проверено 28.09.2026, только чтение)

| Что | Значение | Как проверено |
|---|---|---|
| MATLAB | R2025b, 25.2.0.2998904 (сборка 21.08.2025, без Update). 111 продуктов 25.2 вместе с самим MATLAB, среди них Statistics and ML, Curve Fitting, Optimization, Global Optimization, System Identification, Econometrics, Mapping, Parallel Computing, Deep Learning, PDE, Symbolic Math, Signal Processing, Wavelet, MATLAB Test, MATLAB Compiler и Compiler SDK | `VersionInfo.xml`, `appdata/products`; один запуск `matlab -batch` с печатью `version` и `ver` |
| MATLAB без GUI | `-batch` работает: холодный старт 6,7 с, код выхода 0, `maxNumCompThreads = 8` | тот же запуск |
| MATLAB Engine for Python | в дереве MATLAB `matlabengine` 25.2 для Python 3.9–3.12 (на 3.13 — только предупреждение; есть модуль abi3). На PyPI 25.2.2: `requires_python <3.13` | `extern/engines/python`, PyPI |
| каталог MATLAB | `<MATLAB_ROOT>` — нестандартный каталог на втором диске; `bin` в PATH | листинг |
| Ansys | 2026 R1 (v261), единый пакет R261RC2P01 (04.02.2026), решатель «Release 2026 R1 20260202». Есть MAPDL (`ANSYS261.exe`, `MAPDL261.exe`), Mechanical (`AnsysWBU.exe` 26.1), Workbench (`RunWB2.exe`), DPF (сборка Daily-20260112), optiSLang 26.1.0 rev 1878 (`optislang.com`), LS-DYNA, LS-OPT. Встроенный CPython 3.10.19 — внутренний, не используем | `builddate.txt`, `package.id`, VersionInfo |
| каталог Ansys | `<ANSYS_ROOT>` = каталог `v261` на втором диске; переменная `AWP_ROOT261` задана, PyAnsys находит установку по ней | листинг, имена переменных окружения |
| лицензии Ansys | FlexNet `lmgrd` слушает порт 1055 **на всех интерфейсах**, демон `ansyslmd` запущен. `ANSYSLMD_LICENSE_FILE` задана (значение не читалось). `lmutil` есть в `licensingclient`. Состав фич и число мест не проверялись | `Get-NetTCPConnection`, список процессов |
| AutoCAD / Civil 3D | AutoCAD 2026 R25.1.164, `accoreconsole` 25.1.164, Civil 3D `AeccDbMgd` 13.8.1516.0 — совпадает с [AGENT_G §3.1](../implementation_work/AGENT_G_API_MCP_CAD_DESIGN.md) | VersionInfo |
| Python и инструменты | один интерпретатор 3.13.13; `uv` и Go нет; Node 24.15; .NET SDK только 6.0.201. Venv `work/venv-desktop` (mcp 2.2.0, ezdxf, pywin32) — по данным координатора | `py -0`, `dotnet --list-sdks` |
| железо | i7-14700KF (20 ядер, 28 потоков), 64 ГБ ОЗУ, RTX 5070 Ti; на системном диске ≈ 74 ГБ свободно, на дисках данных — сотни ГБ | CIM |
| путь клона | содержит кириллицу, поэтому `$VKM_WORK` (внутри клона) не годится для заданий MAPDL и `accoreconsole` | — |

## 2. Общий слой `vkm_jobs`

### 2.1 Корень и раскладка кода

- `VKM_SIM_ROOT` — обязательная переменная. Требования: только ASCII, без пробелов, локальный диск данных с запасом не
  меньше 100 ГБ, вне клонов PUBLIC и PRIVATE и вне канона. Проверка при старте такая же, как jail в `vkm_cad`.
- Внутри корня: `jobs/<job_id>/`, `sessions/<session_id>/`, `locks/`, `cache/`, `logs/`, `matlab/session/`.
- Каталог задания: `job.json` (спецификация `vkm.sim_job/1`), `in/`, `work/`, `out/`, `logs/`, `status.json`,
  `receipt.json`.
- `job_id` = `<APP>-<UTC ГГГГММДДTччммссZ>-<8 hex>`; APP ∈ MATLAB, MAPDL, MECH, WB, DPF, OSL, PY.
- Входы копируются в `in/` с SHA-256. Код проекта (пакет `matlab/`, шаблоны APDL) кладётся в `in/` снимком; в receipt
  пишутся `git_commit` и `git_dirty`.

Раскладка кода для агентов реализации:

```
src/vkm_jobs/      roots.py spec.py receipt.py runner.py pools.py checks.py redact.py mcp_tools.py
src/vkm_matlab/    detect.py batch.py service.py setup_session.py mcp_server.py entry/vkm_job_entry.m
src/vkm_ansys/     detect.py license.py mapdl_batch.py mapdl_out.py sessions.py dpf_job.py mech.py wb.py osl.py
                   ladder/ (S01…S09: *.inp + checks.json) service.py mcp_server.py
matlab/+vkm/       +mcp (session_eval, job helpers) +fit +theory +ens +fig;  matlab/tests/
tests/engineering/ test_jobs_*.py test_matlab_mcp.py test_ansys_mcp.py
docs/engineering_tools/receipts/     публичные receipts smoke и лестницы
requirements/ansys.lock.txt
```

В `pyproject.toml`: пакеты `vkm_jobs*`, `vkm_matlab*`, `vkm_ansys*`; экстра `ansys` (§4.6); маркеры `matlab` и
`ansys`. `vkm_world` от PyAnsys не зависит: генератор колоды (ступень S10) пишет текст APDL, а запускает его
`vkm_jobs`.

### 2.2 Исполнение, пулы, таймауты

- **Runner.** Задание исполняет отдельный процесс `python -m vkm_jobs.runner <job_dir>` (detached). Он переживает
  MCP-сервер: перезапуск Claude Code не убивает многочасовой расчёт. Runner держит Windows Job Object с
  KILL_ON_JOB_CLOSE над деревом процессов приложения. Он же отвечает за таймаут, отмену (файл `cancel.request`),
  `status.json` и receipt.
- **Статусы:** `QUEUED → RUNNING → SUCCEEDED | FAILED | CHECK_FAILED | TIMED_OUT | CANCELLED | LICENSE_UNAVAILABLE |
  LOST`.
- **Пулы лицензий** — файловые блокировки в `locks/` с PID и снятием «мёртвых»:

  | Пул | Ёмкость | Кто занимает |
  |---|---|---|
  | `ansys` | 1 | MAPDL (batch и сессия), Mechanical, Workbench, optiSLang |
  | `matlab` | 1 | задания MATLAB. Интерактивная сессия официального сервера — отдельный процесс; при `VKM_MATLAB_EXCLUSIVE=1` задания ждут её закрытия |
  | `dpf` | 2 | извлечение результатов в контексте Entry, без лицензии |

  Очередь ждёт пул не дольше `queue_timeout_s` (по умолчанию 24 ч).
- **Таймауты по умолчанию** (вызов может их изменить в пределах потолка):

  | Вид | По умолчанию | Потолок |
  |---|---|---|
  | задание MATLAB | 1800 с | 24 ч |
  | колода MAPDL | 7200 с | 48 ч |
  | ступень лестницы | 900 с | 2 ч |
  | DPF | 1800 с | 6 ч |
  | Mechanical, Workbench | 3600 с | 24 ч |
  | optiSLang | 24 ч | 72 ч |
  | сессия | простой 1800 с | жизнь 12 ч |

- **Лимиты Claude Code** (документация MCP): stdio-вызов прерывается после 30 мин без ответа и progress; вызов длиннее
  2 мин уходит в фон; результат — не больше 25 000 токенов. Поэтому `*_run` возвращает `job_id` сразу (или ждёт
  `wait_s ≤ 600`); `job_wait` шлёт progress не реже раза в 30 с; ответы — сводки до 20 000 символов, а логи читаются
  через `job_read` со смещением и `grep`.

`JobRef` = `{job_id, status, pool, queue_position?, job_dir (логический путь), receipt? (если готово за wait_s)}`.

### 2.3 Receipt `vkm.sim_receipt/1`

| Группа | Поля |
|---|---|
| что и чем | `job_id`, `app` с версией и сборкой, `kind`, `command` (argv с логическими путями), переменные окружения (только имена), `git_commit`, `git_dirty`, `pool` |
| как прошло | `started_at`, `ended_at` (UTC), `duration_s`, `exit_code`, `status`, `log_summary` (число ошибок и предупреждений, первая ошибка) |
| входы и выходы | `inputs[]`, `outputs[]`: путь в каталоге задания, байты, SHA-256 (scratch-файлы MAPDL не хешируются) |
| проверки | `checks[]`: имя, ожидание, факт, допуск, итог |
| наука | `params[]`: значение, статус (FACT … UNKNOWN), `source_ref`; `model_choices[]`; `result_status: MODEL_RESULT` — всегда, результат расчёта не наблюдение и не field validation; `review_status: AUTO_UNREVIEWED` |

Пути в receipt — только `<VKM_SIM_ROOT>`, `<MATLAB_ROOT>`, `<ANSYS_ROOT>`, `<PUBLIC>`. Имени пользователя, хоста и
строки лицензии в нём нет. Публичная копия создаётся только через `job_publish_receipt` в
`docs/engineering_tools/receipts/`: санитизация, затем `leakage.scan`.

**Проверки ожидаемого результата.** `Check = {name, kind, path, pointer, expected, rtol, atol}`, где
`kind` ∈ `file_exists | json_value | number_close | text_contains | text_absent | exit_code`. Runner вычисляет их
после завершения процесса; любая неуспешная даёт `CHECK_FAILED`. Шаги лестницы несут свои проверки — аналитические
решения.

### 2.4 Общие tools заданий (есть в обоих серверах)

| Tool | Класс | Сигнатура | Результат | Первый smoke (фейковое приложение `python -c …`) |
|---|---|---|---|---|
| `job_list` | read | `(app?: str, status?: str, limit=50 ≤ 200, cursor?: str)` | задания и курсор | после двух заданий — две записи; фильтр по статусу работает |
| `job_status` | read | `(job_id)` | статус, время, код выхода, прогресс (для MAPDL — шаг, подшаг, время) | `print(1)` → `SUCCEEDED`, exit 0 |
| `job_wait` | read | `(job_id, timeout_s=600 ≤ 1200)` | итог или текущий статус; progress-уведомления | задание на 5 с: при `timeout_s=1` → `RUNNING`, при `timeout_s=30` → `SUCCEEDED`; progress пришёл |
| `job_read` | read | `(job_id, path, offset=0, max_chars=20000, grep?: regex)` | фрагмент текстового файла задания | лог из трёх строк, `grep="ERROR"` → одно совпадение; путь с `..` → `PATH_OUTSIDE_ROOT` |
| `job_receipt` | read | `(job_id)` | receipt | все поля §2.3; SHA-256 входа совпадает с исходным файлом |
| `job_cancel` | write | `(job_id, reason: ≥ 10 символов)` | `CANCELLED`; дерево процессов убито | задание с дочерним процессом → `CANCELLED`, обоих процессов нет |
| `job_publish_receipt` | write (в репозиторий) | `(job_id, name: [a-z0-9_]+, overwrite=false)` | путь публичного receipt | файл в `docs/engineering_tools/receipts/`, `leakage.scan` без проблем; повтор без `overwrite` → `WOULD_OVERWRITE` |

Конверт ответа — как у `vkm-cad`: `{"schema": "vkm-<server>.result/1", ok, server, server_version, tool, request_id,
result, error}`. Коды ошибок: `INVALID_ARGUMENT`, `NOT_FOUND`, `APP_UNAVAILABLE`, `LICENSE_UNAVAILABLE`, `POOL_BUSY`,
`TIMEOUT`, `SESSION_NOT_FOUND`, `SESSION_DEAD`, `PATH_OUTSIDE_ROOT`, `WOULD_OVERWRITE`, `GATE_CLOSED`, `INTERNAL`.

### 2.5 Безопасность

- Все серверы работают только через stdio и только на WORKSTATION; наши серверы портов не открывают.
- Серверы приложений (MAPDL, Mechanical, Workbench, optiSLang) слушают только loopback. gRPC идёт в режиме WNUA
  (Windows Named User Authentication): в PyAnsys на Windows он по умолчанию, в 2026 R1 работает без сервис-паков.
  Каждый smoke сессии проверяет через `Get-NetTCPConnection`, что слушающий адрес — только loopback. DPF работает
  in-process внутри процесса задания и порта не открывает.
- Исполняющие tools равны выполнению кода: APDL `/SYS`, MATLAB `system`, Python. Это `matlab_run`, `vkm_session_*`,
  встроенные `evaluate_matlab_code` и `run_matlab_file`, `mapdl_run_deck`, `ansys_run_python`, сессии. Их не добавляют в
  allow-списки: каждый вызов подтверждает человек, либо он осознанно разрешает их на сессию. Каталог задания — рабочая
  область, а не песочница. `mapdl_run_deck` отказывает на `/SYS` без `allow_sys=true`.
- Конфигурацию лицензий не трогаем. `ansys_license_status` только читает (`lmutil lmstat`) и не выдаёт имён
  пользователей и хостов.
- Результаты решателей имеют статус `MODEL_RESULT`. Typed tools не пишут в каталоги evidence и не называют синтетику
  наблюдением (D-19). Числовые параметры typed tools несут статус и `source_ref`; без статуса вызов отклоняется.

## 3. MATLAB

### 3.1 Варианты и решение

| Вариант | Плюсы | Минусы | Решение |
|---|---|---|---|
| официальный MATLAB MCP Server (`github.com/matlab/matlab-mcp-server`; до v0.11 назывался «MATLAB MCP Core Server»; v0.14.0 от 25.09.2026) | от MathWorks, выпуски раз в 1–2 недели; stdio; бинарник на Go 19,5 МБ, Python не нужен; MATLAB R2021a и новее; постоянная сессия; 5 tools; свои tools из JSON (`--extension-file`); Claude Code поддерживается | нет receipt, таймаута и кода выхода; телеметрия включена по умолчанию; лицензия MathWorks (BSD-подобная, использование только вместе с продуктами MathWorks); один сервер — один пользователь | **берём** для интерактива |
| MATLAB Engine API for Python | полный API из Python, общий движок | для R2025b нужен Python ≤ 3.12, проект на 3.13, значит системная установка Python 3.12 | не берём |
| `matlab -batch` из нашего сервера | чистый процесс, код выхода, лог, таймаут, параллельные задания; старт около 7 с; новых пакетов не нужно | нет общей памяти между вызовами | **берём** для заданий |
| MATLAB Agentic Toolkit (навыки и тот же сервер) | готовые навыки MATLAB | инсталлятор пишет в конфиг агента | по желанию пользователя |

### 3.2 Слой 1 — универсальный канал

**Официальный сервер `matlab`.** Tools: `evaluate_matlab_code(code, project_path)`, `run_matlab_file(script_path)`,
`run_matlab_test_file(script_path)`, `check_matlab_code(script_path)`, `detect_matlab_toolboxes()`.

Настройка:
- флаги `--matlab-session-mode=new`, `--matlab-display-mode=nodesktop`, `--disable-telemetry=true`,
  `--initial-working-folder=$VKM_SIM_ROOT/matlab/session`;
- `--log-folder` задаётся явно и только ASCII: при DOS 8.3-пути temp сервер падал (issue #117);
- `startup.m` папки сессии включает `diary` (журнал сессии) и добавляет снимок пакета проекта в path;
- через `--extension-file` добавляем свои tools `vkm_session_eval(code, label)` и
  `vkm_session_run_file(path, label)`. Это те же вызовы, но с receipt: MATLAB-функция `vkm.mcp.session_eval` пишет код,
  вывод, ошибку и длительность в `matlab/session/receipts/`;
- в правилах разрешений `vkm_session_*` заменяют встроенные `evaluate_matlab_code` и `run_matlab_file` (для встроенных —
  «спросить»);
- ограничения custom tools: аргументы только string, number, integer, boolean; вывод — текст командного окна;
- жёсткого таймаута в сессии нет, срабатывает только простой MCP в 30 мин. Всё долгое идёт заданиями `vkm-matlab`.

**`vkm-matlab`: задания.**

`matlab_run(code?: str ≤ 200 000, file?: str, function?: str, args?: json, inputs?: [str], outputs?: [glob],
seed?: int, export_figures=false, timeout_s=1800, wait_s=0 ≤ 600, checks?: [Check], label?: str) → JobRef`

- Нужно ровно одно из `code`, `file`, `function`.
- Команда: `matlab -batch "vkm_job_entry('<job>')" -sd <job>/work -logfile <job>/logs/matlab.log`.
- Точка входа пишет:
  - `out/result.json` — `jsonencode` переменной `result`;
  - `out/figures/*.png|pdf` — через `exportgraphics`;
  - `out/products.json` — через `matlab.codetools.requiredFilesAndProducts`: какие тулбоксы понадобились.
- Любой тулбокс и любой код, то есть весь MATLAB, но с receipt.

### 3.3 Слой 2 — типизированные tools (`vkm-matlab`)

Функции живут в пакете проекта `matlab/+vkm/…` и покрыты юнит-тестами MATLAB Test. Формы законов имеют статус
MODEL_CHOICE. Параметры приходят со статусом и `source_ref` из корпуса и WorldSpec, без «правдоподобных» чисел.

| Tool | Сигнатура | Что делает |
|---|---|---|
| `matlab_test` | `(target: str, tags?: [str], timeout_s=1800)` | `runtests` → JUnit XML и сводка |
| `matlab_lint` | `(paths: [str])` | `codeIssues` → JSON замечаний |
| `matlab_fit` | `(data: str, x: str, y: str, model: enum из реестра +vkm/+fit, start?: json, bounds?: json, weights?: str, robust="off"\|"LAR"\|"Bisquare")` | подбор функции времени или профиля (Curve Fitting, Optimization): параметры, доверительные интервалы, GOF, остатки, рисунок |
| `matlab_profile` | `(method: enum из +vkm/+theory, params: json \| param_set_ref, grid: json)` | профиль и поле мульды, функции времени по выбранной теории; формы — из корпуса |
| `matlab_ensemble_summary` | `(table: str, by?: [str], metrics: [str], quantiles=[0.05, 0.5, 0.95])` | сводки ансамбля миров (Statistics) |
| `matlab_figure` | `(script: str, formats=["png", "pdf"], width_cm=16, height_cm=10, dpi=300)` | рисунки для диплома единого вида |
| `matlab_nn_*` | после гейта ML | до гейта (D-19: бенчмарк предрегистрирован) ответ `GATE_CLOSED` |

### 3.4 Слой 3 — discovery

| Tool | Сигнатура | Запускает MATLAB? |
|---|---|---|
| `matlab_status` | `()` | нет. Версия, продукты (из `appdata`), исполняемые файлы, версия бинарника официального сервера, процессы MATLAB, пулы, корень заданий |
| `matlab_toolboxes` | `(refresh=false)` | да, один раз на версию (кэш): `ver` и `license('test', …)` по фичам тулбоксов — проверка без checkout; имена фич берутся из данных установки |
| `matlab_which` | `(names: [str] ≤ 50)` | да (кэш): `which -all` и тулбокс-владелец |
| `matlab_help` | `(topic: str, max_chars=20000)` | да (кэш): `help` по любой функции |
| `detect_matlab_toolboxes` (официальный) | `()` | в сессии |

### 3.5 Установка

| Шаг | Что скачивается или ставится | Размер | Одобрение |
|---|---|---|---|
| 1 | `matlab-mcp-server-windows-x64.exe` v0.14.0 из GitHub Releases `matlab/matlab-mcp-server`. SHA-256 сверить с полем `digest` релиза; хранить вне клона в `%LOCALAPPDATA%\vkm\tools\matlab-mcp-server\0.14.0\` | 19,5 МБ | **да** (исполняемый файл) |
| 2 | `vkm-matlab` работает в существующем `work/venv-desktop` (mcp 2.2.0, pywin32); новых пакетов нет. `python -m vkm_matlab.setup_session` готовит `$VKM_SIM_ROOT/matlab/`: снимок `matlab/`, `startup.m`, `vkm_tools.json` | 0 | нет |
| 3 (опц.) | `MATLABMCPServerToolbox.mltbx` — нужен только для режима `existing` (подключение к открытому MATLAB пользователя через `shareMATLABSession()`) | 60 КБ | да (add-on MATLAB) |
| 4 (опц.) | MATLAB Agentic Toolkit — навыки MATLAB | малый | да (меняет конфиг Claude Code) |

### 3.6 Первый smoke по каждому tool

| Tool | Вход | Ожидание |
|---|---|---|
| `matlab_status` | — | R2025b, 25.2.0.2998904, 111 продуктов (с MATLAB); число процессов MATLAB не изменилось |
| `matlab_toolboxes` | `refresh=true` | есть «Statistics and Machine Learning Toolbox 25.2» и «Curve Fitting Toolbox 25.2»; receipt, exit 0 |
| `matlab_run` | `code="result.s = sum(magic(4), 'all');"`, check `json_value /s = 136` | `SUCCEEDED` |
| `matlab_run` (ошибка) | `code="error('vkm:smoke', 'boom')"` | `FAILED`, exit ≠ 0, идентификатор `vkm:smoke` в receipt |
| `matlab_run` (таймаут) | `code="pause(120)"`, `timeout_s=10` | `TIMED_OUT`; процессов MATLAB от задания не осталось |
| `matlab_which` | `["fit"]` | Curve Fitting Toolbox |
| `matlab_help` | `"fitlm"` | непустая справка |
| `matlab_test` | `matlab/tests/smoke` | 1 passed, JUnit XML |
| `matlab_lint` | файл с неиспользуемой переменной | одно замечание |
| `matlab_fit` | синтетика `y = 2(1 − e^(−0.5 t))` с шумом, модель `exp_saturation` | оценки в пределах 3σ от (2; 0,5) |
| `matlab_profile` | тестовый метод с аналитическим ответом | совпадение до 1e-12 |
| `matlab_ensemble_summary` | CSV из трёх строк | квантили совпадают с ручным расчётом |
| `matlab_figure` | `plot(1:3)` | PNG и PDF заданного размера |
| `detect_matlab_toolboxes`, `evaluate_matlab_code("disp(version)")` (официальные) | — | «25.2.0.2998904» |
| `vkm_session_eval` | `"disp(version)"` | то же и receipt в `matlab/session/receipts/` |

Позже, до планирования обучения в MATLAB: smoke `gpuDevice` — поддержка RTX 5070 Ti (Blackwell) в R2025b не
проверена.

## 4. Ansys

### 4.1 Варианты и решение

| Вариант | Состояние | Решение |
|---|---|---|
| PyMAPDL-MCP `ansys-mapdl-mcp` 0.3.0 (18.09.2026, Apache-2.0, Alpha). 16 tools: `launch_mapdl_session`, `run_mapdl_command`, `run_multiple_mapdl_commands`, `run_python_code`, `screenshot`, `upload_file`, `download_file` и др. | стек `ansys-common-mcp` 0.3.5 → fastmcp 3–4; Python ≥ 3.12 | не ставим: Alpha, чужой стек MCP, произвольный Python в процессе сервера, нет receipt, таймаутов и пула лицензий. Идеи набора tools берём |
| PyMechanical-MCP `ansys-mechanical-mcp` 0.2.1 (17.09.2026, Alpha), 23 tools | тот же стек | не ставим |
| наш `vkm-ansys` поверх библиотек PyAnsys (MIT) | PyMAPDL 0.74.1, PyDPF-Core 0.16.1, PyMechanical 0.13.3, PyWorkbench 0.14.0, PyOptiSLang 1.6.1; все поддерживают Python 3.13 и 2026 R1 | **берём** |

### 4.2 Каналы, лицензии, порты

| Продукт | Канал | Лицензия | Порт |
|---|---|---|---|
| MAPDL | batch: `ANSYS261.exe -b -i <колода> -o <лог> -j <имя> -dir <work> -np 4 -smp -s noread [-p <фича>]`. Сессия: PyMAPDL `launch_mapdl(..., transport_mode="wnua")` | одна лицензия решателя на процесс; до 4 ядер без HPC, дальше нужны лицензии HPC, поэтому `np ≤ 4`. Сначала SMP, DMP (MPI) — отдельным smoke | gRPC, 50052 по умолчанию (PyMAPDL берёт свободный), loopback + WNUA |
| Mechanical | batch: `ansys-mechanical --revision 261 -i <скрипт.py>` (флаги сверить по `--help`), то есть `AnsysWBU.exe -DSApplet -AppModeMech -script <скрипт> -b`. Сессия: `launch_mechanical(batch=True, transport_mode="wnua")` | держит лицензию, пока открыт (не read-only); проверить smoke по `lmstat` | 10000 по умолчанию |
| Workbench | batch: `RunWB2.exe -B [-F <проект.wbpj>] -R <журнал.wbjn>`. Сессия: `launch_workbench(show_gui=False, version="261", ...)` | сам не берёт; берут открытые системы | сервер PyWorkbench, loopback |
| DPF | задание: in-process сервер, контекст Entry | Entry без checkout (операторы source и result); операторы Premium — только явным флагом | нет |
| optiSLang | задание PyOptiSLang: `Optislang(project_path, batch=True, ini_timeout=60)` | своя фича optiSLang; его собственные вызовы MAPDL — вторая лицензия | TCP, loopback |

Коды выхода MAPDL (Operations Guide): 0 — норма, 1–4 — стек, 5 — аргументы, 7 — файл авторизации, 8 — ошибка или
конец прогона, 12 — `/STOP` в макросе, 15 — fatal, 16 — диск, 17 — файл. Отказ лицензии определяется по тексту
`.err` и `.out` и даёт статус `LICENSE_UNAVAILABLE`.

### 4.3 Слой 1 — универсальный канал (`vkm-ansys`)

| Tool | Сигнатура | Что покрывает |
|---|---|---|
| `mapdl_run_deck` | `(deck?: str ≤ 1 МБ, deck_path?: str, includes?: [str], jobname="file", np=4 ≤ 4, parallel="smp"\|"dmp", ram_mb?: int, license_type?: str, keep=["rst","db","out","err","log","mntr"], allow_sys=false, timeout_s=7200, wait_s=0, checks?: [Check], label?) → JobRef` | любая колода APDL. Scratch (`.full`, `.esav`, `.emat`) удаляется после успеха по белому списку, удаление записано в receipt |
| `mech_run_script` | `(script?: str, script_path?: str, inputs?: [str], timeout_s=3600, wait_s=0, checks?, label?) → JobRef` | весь API скриптинга Mechanical (`ExtAPI`, `Model`, `DataModel`); входы — mechdb, wbpj, геометрия |
| `wb_run_journal` | `(journal?: str, journal_path?: str, project?: str, timeout_s=3600, wait_s=0, checks?, label?) → JobRef` | журналы Workbench (IronPython) |
| `osl_run` | `(mode: "script"\|"run_project", project?: str, script?: str, timeout_s=86400, wait_s=0, checks?, label?) → JobRef` | Python API optiSLang и запуск проекта. **Гейт:** анализ чувствительности и Monte Carlo — только по явному указанию задачи (CLAUDE.md) |
| `ansys_run_python` | `(script: str, attach?: session_id, inputs?: [str], uses_license: bool, timeout_s=3600, wait_s=0, checks?, label?) → JobRef` | любой PyAnsys в процессе задания (`venv-ansys`): PyMAPDL к сессии через `vkm_ansys.connect(session_id)`, PyDPF, встроенный Mechanical `App()`, PyWorkbench, PyOptiSLang |
| `ansys_session_start` | `(product: "mapdl"\|"mechanical"\|"workbench", np=4, idle_timeout_s=1800, label?)` | интерактивная сессия. Журнал: у MAPDL — `log_apdl` (все команды), у Mechanical и Workbench — тексты скриптов |
| `ansys_session_run` | `(session_id, input: str, kind: "apdl"\|"apdl_file"\|"python", timeout_s=600)` | MAPDL — `input_strings` или `input`; Mechanical — `run_python_script`; Workbench — `run_script_string`. Каждый вызов даёт receipt вызова |
| `ansys_session_stop` | `(session_id, save=true)` | `SAVE` или сохранение проекта, выход, receipt сессии, пул освобождён |
| `ansys_session_list` | `()` | сессии: PID, порт, время простоя |

Сессии принадлежат серверу и держат пул `ansys`; batch-задание в это время ждёт в очереди. Процесс сессии сервер
включает в свой Job Object. При старте сервер ищет осиротевшие сессии по `sessions/*.json` и завершает их после
проверки имени и командной строки процесса.

### 4.4 Слой 2 — лестница решателя и операции диплома

| Tool | Сигнатура | Что делает |
|---|---|---|
| `ansys_ladder_list` | `()` | ступени, шаблоны, схемы параметров, проверки, готовность |
| `ansys_ladder_run` | `(step: "S01"…"S09", params?: json, np=4, timeout_s=900, wait_s=0) → JobRef` | колода из шаблона `src/vkm_ansys/ladder/` и аналитическая проверка |
| `mapdl_convergence` | `(job_id)` | разбор `.out` и `.mntr`: шаги, подшаги, итерации, бисекции, ошибки |
| `dpf_extract` | `(result: job_id \| path, quantity: "U"\|"UX"\|"UY"\|"UZ"\|"S"\|"EPEL"\|"EPPL"\|"EPCR", location: "nodal"\|"elemental", selection: {named_selection \| box \| surface: "top"}, times: "all" \| [float], fmt: "csv"\|"json") → JobRef` | таблицы результатов (DPF, контекст Entry) |
| `ansys_subsidence` | `(job_id, surface="top" \| named_selection, times="all", profiles?: [[x0, y0, x1, y1, n]]) → JobRef` | мульда модели: поле w(x, y, t), профили, максимум во времени. Статусы DERIVATION и `MODEL_RESULT` |
| `ansys_compare` (после S09) | `(a, b: job_id \| csv, quantity, on: "nodes"\|"profile", metrics=["max_abs", "rmse", "rel_l2"])` | сверка Ansys ↔ Ansys, OGS, World-0 (ошибка представления) |
| `ansys_worldspec_run` (S10, отдельная задача) | `(slice_ref, ...)` | адаптер среза WorldSpec: `vkm_world.representations.ansys_mapdl` строит колоду, `vkm_jobs` её запускает |

Ступени лестницы. Параметры игрушечные: статус ENGINEERING_ASSUMPTION, область TOY; это не параметры соли СКРУ-1.

| Ступень | Задача | Проверка |
|---|---|---|
| S01 установка и версия | `*GET,REV,ACTIVE,0,REV` | REV = 26.1, exit 0 |
| S02 минимальная механика | колонна SOLID185 под давлением p сверху, одноосное напряжение | u_z(верх) = −pL/E, σ_z = −p (rtol 1e-6) |
| S03 зоны материалов | два слоя с модулями E1 и E2 | u_z(верх) = −p(L1/E1 + L2/E2) |
| S04 гравитация и нагрузки | столб с плотностью ρ, боковые ролики (одноосная деформация) | σ_z(z) = −ρg(H − z); σ_x = ν/(1 − ν)·σ_z; u_z(верх) = −ρgH²(1 + ν)(1 − 2ν)/(2E(1 − ν)) |
| S05 граничные и начальные условия | `INISTATE` из поля S04 плюс гравитация | max\|u\| не больше допуска (равновесие) |
| S06 шаги по времени | несколько шагов нагрузки с `TIME` | история совпадает с линейным ростом нагрузки |
| S07 реология | `TB,CREEP`, закон Нортона (TBOPT = 10) без температурного члена, σ = const | ε_cr(t) = C1·σ^C2·t (rtol 1e-3) |
| S08 извлечение результатов | DPF против `*GET` и `PRNSOL` на S02 и S04 | совпадение до 1e-9 |
| S09 интегрированный toy-кейс | слоистый блок, камера убирается `EKILL` на шаге 2, ползучесть, поверхность | симметрия, монотонный рост оседаний во времени, сходимость, ноль ошибок |
| S10 адаптер к срезу WorldSpec | срез WorldSpec → колода | отдельная задача |

### 4.5 Слой 3 — discovery

| Tool | Сигнатура | Что делает |
|---|---|---|
| `ansys_status` | `()` | ничего не запускает. Версии и сборки (v261, R261RC2P01), продукты и исполняемые файлы, версии PyAnsys в venv, процессы Ansys, сессии, слушающие адреса портов, доступность сервера лицензий (TCP-подключение без checkout), корень заданий |
| `ansys_license_status` | `(features?: [str])` | `lmutil lmstat -a` → фича, выдано, занято; без имён пользователей и хостов |
| `mapdl_command_help` | `(command: str)` | справка по любой команде APDL офлайн, из docstring PyMAPDL (`EKILL`, `TB`, `INISTATE`, …) |
| `mech_api_search` | `(query: str, limit=20)` | поиск по `ansys-mechanical-stubs`: классы, методы, сигнатуры |
| `dpf_operators` | `(filter?: str, limit=100)` | имена операторов DPF и требование лицензии (короткое задание в контексте Entry) |

### 4.6 Установка: `work/venv-ansys`

Отдельный venv на Python 3.13 — не `venv-desktop`, чтобы тяжёлые зависимости не ломали `vkm-cad` и `vkm-drawio`.

Команды (после одобрения):
1. `python -m venv work/venv-ansys`;
2. `pip install -c requirements/worldspec.lock.txt -e ".[ansys]"`;
3. `pip freeze` → `requirements/ansys.lock.txt`, как у остальных lock-файлов; pydantic и numpy не сдвигаются.

Экстра `ansys` в `pyproject.toml`: `mcp==2.2.0`, `pywin32>=312`, `ansys-mapdl-core==0.74.1`, `ansys-dpf-core==0.16.1`,
`ansys-mechanical-core==0.13.3`, `ansys-workbench-core==0.14.0`, `ansys-optislang-core==1.6.1`.

| Пакет | Версия | Колесо | Что тянет | Класс |
|---|---|---|---|---|
| `ansys-mapdl-core` | 0.74.1 (август 2026) | 3,0 МБ | `vtk` 9.7.1 (80,4 МБ), `scipy` 1.18.1 (38,7 МБ), `matplotlib` 3.11.2 (7,5 МБ), `pillow` (≈ 7 МБ), `pyvista` 0.49.0 (1,6 МБ), `ansys-mapdl-reader` 0.56.0 (1,1 МБ), `grpcio` (≈ 5 МБ) | **крупный** |
| `ansys-dpf-core` | 0.16.1 (08.06.2026) | 8,6 МБ (Windows) | нативные библиотеки DPF | **крупный** |
| `ansys-mechanical-core` | 0.13.3 | < 1 МБ | `ansys-mechanical-stubs` 0.1.15 (5,8 МБ), `ansys-pythonnet` 3.1.0rc8 (0,2 МБ) | малый |
| `ansys-workbench-core` | 0.14.0 (07.05.2026) | 20 КБ | `WMI`, `ansys-api-workbench` | малый |
| `ansys-optislang-core` | 1.6.1 (14.09.2026) | 0,8 МБ | `pywin32` | малый |
| `mcp`, `mcp-types` | 2.2.0 | ≈ 5 МБ с зависимостями | — | малый |

Итого ≈ 190–200 МБ загрузки и ≈ 0,7–0,8 ГБ на диске (в основном vtk). Для cp313-win_amd64 есть колёса у всех
пакетов, сборка из исходников не нужна.

### 4.7 Первый smoke по каждому tool

| Tool | Вход | Ожидание |
|---|---|---|
| `ansys_status` | — | v261 и R261RC2P01; MAPDL, Mechanical, Workbench, DPF, optiSLang найдены; порт лицензий доступен; процессов не прибавилось |
| `ansys_license_status` | — | непустой список фич; нет имён пользователей и хостов |
| `mapdl_command_help` | `"EKILL"` | справка о «смерти» элементов |
| `mech_api_search` | `"StaticStructural"` | методы добавления анализа |
| `dpf_operators` | `"displacement"` | есть оператор перемещений; лицензия не требуется |
| `mapdl_run_deck` | колода S01 | exit 0, REV = 26.1, receipt |
| `mapdl_run_deck` (ошибка) | `/PREP7`, `ET,1,NOTANELEMENT`, `/EXIT` | exit 8 или число ошибок > 0 → `FAILED` |
| `mapdl_run_deck` (`/SYS`) | колода с `/SYS,dir` без `allow_sys` | `INVALID_ARGUMENT`, задание не создано |
| `ansys_ladder_run` | `S02` | `SUCCEEDED`, проверка u_z пройдена |
| `dpf_extract` | задание S02, `UZ`, `surface: top` | −pL/E; по `lmstat` лицензий не прибавилось |
| `mapdl_convergence` | задание S07 | таблица подшагов, ноль бисекций |
| `ansys_subsidence` | задание S09 | симметричный профиль, монотонный рост |
| `ansys_session_start` / `run` / `stop` (mapdl) | `/PREP7`, `ET,1,185`, `*GET,n,ETYP,0,NUM,MAX` | n = 1; сессия слушает только loopback; после `stop` лицензия возвращена, процесса нет |
| `ansys_run_python` | attach к сессии: `print(mapdl.get_value("ACTIVE", 0, "REV"))` | 26.1 |
| `mech_run_script` | скрипт пишет `work/smoke.json` с версией приложения | файл есть, версия 26.1 |
| `wb_run_journal` | журнал сохраняет пустой проект | `.wbpj` создан, exit 0 |
| `osl_run` | новый проект, `save_copy` | `.opf` создан; процесс optiSLang завершён |
| пул `ansys` | два `mapdl_run_deck` подряд | второй в `QUEUED` до конца первого |
| таймаут | S09 с `timeout_s=5` | `TIMED_OUT`; процессов MAPDL не осталось, лицензия возвращена |

## 5. AutoCAD / Civil 3D — ссылка на `vkm-cad` v1

Реализацию делает отдельный агент. `vkm-cad` v1 расширяет существующий мост ([MCP_TOOLS.md §4](../corpus_platform/MCP_TOOLS.md),
[AGENT_G §3](../implementation_work/AGENT_G_API_MCP_CAD_DESIGN.md)): headless-чертежи, команды, точки, TIN,
горизонтали, мульды и профили Civil 3D, листы → PDF. Находки этого исследования:

- **Официальные MCP Autodesk.** Сервера для настольных AutoCAD 2026 и Civil 3D 2026 на 28.09.2026 не найдено. Есть:
  - Autodesk Product Help MCP Server (апрель 2026): только чтение справки по 110+ продуктам, включая AutoCAD и
    Civil 3D; удалённый HTTP `https://developer.api.autodesk.com/knowledge/public/v1/mcp`, бесплатно. Полезен как
    справочник по API Civil 3D. Запросы уходят в сеть Autodesk, подключать — по решению пользователя;
  - Fusion MCP (28.04.2026), MCP в Revit 2027, Autodesk Assistant в AutoCAD 2027 — не наши версии.
- **Сообщество.** `Civil3D-mcp` (много форков, «180 tools», живой чертёж через плагин): происхождение и лицензия
  неясны, не ставить, только как пример. AutoCAD 2026 MCP от moisesbritez92: `accoreconsole` плюс .NET-плагин.
- **`accoreconsole /product C3D`.** Ключ существует, но у Autodesk есть две статьи об известных проблемах: «AcCoreConsole.exe
  does not load Civil 3D Modules» и «Accoreconsole.exe fails to recognize Civil 3D objects in the R2024 and R2025 .NET
  API». Сообщество догружает модули Civil 3D вручную. Для 2026 это **UNKNOWN** до smoke на этой машине:
  `accoreconsole /product C3D /l ru-RU /isolate …` и NETLOAD тестового плагина, который создаёт `TinSurface` по трём
  точкам и сохраняет чертёж; проверка — тип объекта после повторного открытия.
  - Если не работает, объекты Civil 3D доступны только в GUI-сессии Civil 3D: .NET-плагин или COM
    `AeccXUiLand.AeccApplication.13.8`.
  - Для плагина нужен .NET 8 SDK (есть только 6.0.201); установка — с одобрения пользователя (DN-G8).
- Общий слой заданий (§2) годится и для `accoreconsole`: ASCII-корень, Job Object, receipt.

## 6. Подключение к Claude Code

Серверы прописываются в локальном `.mcp.json` (git-ignored; изменения — с согласия пользователя). Значения берутся из
переменных окружения:

```json
{
  "mcpServers": {
    "matlab": {
      "type": "stdio",
      "command": "${VKM_MATLAB_MCP_EXE}",
      "args": ["--matlab-root=${VKM_MATLAB_ROOT}", "--matlab-session-mode=new", "--matlab-display-mode=nodesktop",
               "--initial-working-folder=${VKM_SIM_ROOT}/matlab/session",
               "--extension-file=${VKM_SIM_ROOT}/matlab/vkm_tools.json",
               "--log-folder=${VKM_SIM_ROOT}/logs/matlab-mcp", "--disable-telemetry=true"]
    },
    "vkm-matlab": {
      "type": "stdio",
      "command": "${VKM_PYTHON}",
      "args": ["-m", "vkm_matlab.mcp_server"],
      "env": {"PYTHONUTF8": "1", "VKM_SIM_ROOT": "${VKM_SIM_ROOT}", "VKM_MATLAB_ROOT": "${VKM_MATLAB_ROOT}"}
    },
    "vkm-ansys": {
      "type": "stdio",
      "command": "${VKM_ANSYS_PYTHON}",
      "args": ["-m", "vkm_ansys.mcp_server"],
      "env": {"PYTHONUTF8": "1", "VKM_SIM_ROOT": "${VKM_SIM_ROOT}", "VKM_ANSYS_ROOT": "${VKM_ANSYS_ROOT:-}"},
      "timeout": 1800000
    }
  }
}
```

- `VKM_PYTHON` — интерпретатор `work/venv-desktop`, `VKM_ANSYS_PYTHON` — интерпретатор `work/venv-ansys`.
- `VKM_ANSYS_ROOT` по умолчанию берётся из `AWP_ROOT261`.
- MATLAB в официальном сервере стартует при первом вызове. Держать его запущенным без нужды не стоит.

Разрешения задаёт пользователь (`.claude/settings.local.json`):
- **allow** — discovery и чтение заданий: `*_status`, `*_help`, `*_which`, `*_toolboxes`, `mapdl_command_help`,
  `mech_api_search`, `job_status`, `job_list`, `job_read`, `job_receipt`, `job_wait`;
- **ask** — всё исполняющее: `matlab_run`, `vkm_session_*`, встроенные `evaluate_matlab_code` и `run_matlab_file`,
  `mapdl_run_deck`, `mech_run_script`, `wb_run_journal`, `osl_run`, `ansys_run_python`, `ansys_session_*`,
  `ansys_ladder_run`, typed-задания; а также `job_cancel` и `job_publish_receipt`.

## 7. Тестирование

- **Контракт без приложений** — по образцу `tests/corpus/test_cad_mcp.py`: список tools, аннотации (read, write,
  exec), JSON-схемы, конверт ошибок, stdout stdio-процесса содержит только протокол. Машина состояний заданий
  проверяется на фейковом приложении (`python -c`): очередь по пулу, таймаут с убийством дерева, отмена, `LOST` при
  смерти runner, jail путей. Receipts проходят `leakage.scan` и не содержат букв дисков.
- **Интеграция** — маркеры `matlab` и `ansys` (берут лицензию). Запуск только явный (`-m matlab`, `-m ansys`) и по
  указанию задачи. Smoke — из §3.6 и §4.7; receipts кладутся в `docs/engineering_tools/receipts/`.
- **Сопутствующие правки:**
  - `leakage.scan` расширить на `.m`, `.inp`, `.mac`, `.wbjn`;
  - в [REPOSITORY_ARCHITECTURE_RU.md](../architecture/REPOSITORY_ARCHITECTURE_RU.md) добавить новые пакеты и каталог
    `matlab/`;
  - справку по tools добавить рядом с [MCP_TOOLS.md](../corpus_platform/MCP_TOOLS.md).

## 8. Риски

| Риск | Что делаем |
|---|---|
| кириллица и DOS 8.3-пути ломают MAPDL, `accoreconsole` и логи сервера MATLAB (issue #117) | ASCII `VKM_SIM_ROOT`, явный `--log-folder`, снимок кода в `in/` |
| gRPC-сервер приложения слушает не только loopback (режим insecure) | WNUA по умолчанию; smoke проверяет адрес; insecure — только явным флагом и только на loopback |
| число мест и состав лицензий неизвестны | пул `ansys` = 1, `np ≤ 4`; `ansys_license_status`; статус `LICENSE_UNAVAILABLE` с понятным текстом |
| долгие расчёты упираются в лимиты MCP (30 мин без progress, 25 000 токенов) | асинхронные задания, detached runner, `job_wait` с progress, сводки плюс `job_read` |
| scratch MAPDL занимает гигабайты, на системном диске ≈ 74 ГБ | корень на диске данных; удаление scratch по белому списку; предупреждение при свободном месте < 50 ГБ |
| официальный сервер MATLAB быстро меняется (переименование в v0.11, телеметрия в v0.14) | фиксировать версию и SHA-256, обновлять осознанно; `--disable-telemetry=true` |
| официальные MCP Ansys в статусе Alpha | не ставим; PyAnsys зафиксированы lock-файлом |
| тяжёлые зависимости PyAnsys ломают другие окружения | отдельный `venv-ansys` |
| Mechanical или Workbench долго стартуют или показывают диалоги | batch-режимы, таймауты, убийство дерева процессов |
| поддержка RTX 5070 Ti (Blackwell) в MATLAB R2025b не проверена | smoke `gpuDevice` до планирования обучения в MATLAB |
| исполнение кода (APDL `/SYS`, MATLAB `system`, Python) | исполняющие tools — только с подтверждением; `/SYS` — только флагом |
| модельный результат примут за наблюдение | `MODEL_RESULT` в каждом receipt; typed tools не пишут в evidence |
| DMP (MPI) на Windows | сначала SMP `-np 4`; DMP — отдельным smoke |
| сервер лицензий `lmgrd` (порт 1055) слушает все интерфейсы | не наша настройка, не трогаем; пользователь может ограничить его брандмауэром |

## 9. Трудозатраты (часы работы агентов)

| Блок | Часы |
|---|---|
| 0. `vkm_jobs`: runner, Job Object, пулы, receipts, jail, санитизация, общие tools, тесты | 8 |
| MATLAB: официальный сервер после одобрения (SHA, `setup_session`, `startup.m`, `vkm_tools.json`, `.mcp.json`) | 2 |
| MATLAB: `vkm-matlab`, слои 1 и 3 | 5 |
| MATLAB: слой 2 и пакет `matlab/+vkm` с тестами MATLAB Test | 8 |
| MATLAB: smoke и публичные receipts | 2 |
| Ansys: `venv-ansys`, экстра, lock-файл | 1,5 |
| Ansys: слой 3 (статус, лицензии, справка APDL, поиск API Mechanical, операторы DPF) | 4 |
| Ansys: `mapdl_run_deck`, разбор `.out`/`.err`/`.mntr`, `mapdl_convergence` | 5 |
| Ansys: сессии MAPDL и `ansys_run_python` | 6 |
| Ansys: `dpf_extract`, `ansys_subsidence` | 5 |
| Ansys: лестница S01–S09 (шаблоны и аналитические проверки) | 12 |
| Ansys: Mechanical (batch и сессия) | 5 |
| Ansys: Workbench (batch и сессия) | 3 |
| Ansys: optiSLang | 3 |
| Ansys: контрактные тесты, smoke, публичные receipts | 4 |
| **Итого** | **≈ 74** (общий слой 8, MATLAB 17, Ansys 48,5) |

## 10. Что требует одобрения пользователя

| № | Что | Источник | Размер | Зачем |
|---|---|---|---|---|
| 1 | бинарник MATLAB MCP Server v0.14.0 (`matlab-mcp-server-windows-x64.exe`) | GitHub Releases `matlab/matlab-mcp-server` | 19,5 МБ | интерактивная сессия MATLAB |
| 2 | `work/venv-ansys` и крупные пакеты PyAnsys: `ansys-mapdl-core` (с vtk, scipy, matplotlib) и `ansys-dpf-core` | PyPI | ≈ 190–200 МБ загрузки, ≈ 0,7–0,8 ГБ на диске | весь `vkm-ansys` |
| 3 | записи серверов в локальный `.mcp.json` и правила разрешений | — | — | подключение |
| 4 | первые запуски решателей (smoke, лестница) — по явному указанию задачи (CLAUDE.md) | — | время лицензии Ansys | лестница |
| 5 (опц.) | `MATLABMCPServerToolbox.mltbx` (режим `existing`) и MATLAB Agentic Toolkit | GitHub | 60 КБ; малый | подключение к открытому MATLAB; навыки |
| 6 (не рекомендуется) | Python 3.12 и `matlabengine` 25.2 | python.org; дерево MATLAB | ≈ 25–30 МБ | только если понадобится Engine API |

Малые пакеты ставятся без вопроса: `mcp` и `mcp-types` 2.2.0, `pywin32`, `ansys-mechanical-core`,
`ansys-workbench-core`, `ansys-optislang-core`, `ansys-tools-common`. Но без крупных пакетов от них нет пользы, поэтому
всё ставится вместе, по одному одобрению п. 2. `vkm-matlab` новых пакетов не требует.

## 11. Порядок работ

0. `vkm_jobs` и контрактные тесты на фейковом приложении. Делает один исполнитель (предлагается агент MATLAB: его путь
   проверяется дешевле всего), чтобы оба сервера строились на одном контракте. Агент Ansys тем временем делает то, что
   от `vkm_jobs` не зависит: слой 3, шаблоны S01–S09 с аналитическими проверками, разбор `.out`.
1. Discovery без лицензий: `matlab_status`, `ansys_status`.
2. MATLAB: `matlab_run` на `-batch` — первый сквозной receipt за секунды (проверка общего слоя на дешёвом
   приложении). Затем официальный сервер (после одобрения п. 1) и `vkm_session_*`.
3. Ansys (после одобрения п. 2 и явного указания задачи): `mapdl_run_deck` → S01 → S02 с публичными receipts.
4. `dpf_extract` → S08 сразу после S02: один путь извлечения для всех следующих ступеней.
5. S03–S07 через `ansys_ladder_run`; `mapdl_convergence`.
6. Сессии MAPDL и `ansys_run_python` — для удобной отладки колод.
7. S09 и `ansys_subsidence` — первая мульда toy-мира (`MODEL_RESULT`). Это гейт 19.10 «Ansys на эталонной задаче».
   Критический путь: пункты 0, 1, 3–5, 7 ≈ 36 ч.
8. MATLAB, слой 2 (`matlab_fit`, `matlab_profile`, `matlab_ensemble_summary`, `matlab_figure`) — параллельно с
   пунктами 5–7, для World-0 и быстрой теории.
9. Mechanical и Workbench — когда сценарию нужны 3D-геометрия и сетка. optiSLang — после явной задачи на анализ
   чувствительности.
10. S10 — адаптер WorldSpec (`vkm_world.representations.ansys_mapdl`) отдельной задачей; `ansys_compare` — для сверки с
    OGS и World-0.

Синтаксис при реализации сверять через Context7: `/ansys/pymapdl`, `/ansys/pydpf-core`, `/ansys/pymechanical`,
`/ansys/pyoptislang`, `/websites/workbench_pyansys_version_stable`. MATLAB — справка MathWorks и `matlab_help`.

## Источники (проверены 28.09.2026)

- MATLAB MCP Server: https://github.com/matlab/matlab-mcp-server (README, `guides/custom-tools.md`, LICENSE.md,
  выпуски v0.12.0–v0.14.0 через GitHub API, issues #103, #117, #120); MATLAB Agentic Toolkit:
  https://github.com/matlab/matlab-agentic-toolkit
- MATLAB Engine for Python: https://pypi.org/project/matlabengine/ (25.2.2 и 26.1.12)
- PyMAPDL-MCP: https://github.com/ansys/pymapdl-mcp, https://pypi.org/project/ansys-mapdl-mcp/;
  PyMechanical-MCP: https://github.com/ansys/pymechanical-mcp; PyAnsys Common MCP:
  https://pypi.org/project/ansys-common-mcp/
- PyMAPDL (`launch_mapdl`, режимы транспорта, CLI `pymapdl`): https://mapdl.docs.pyansys.com/ ; PyMechanical:
  https://mechanical.docs.pyansys.com/ ; PyWorkbench: https://workbench.docs.pyansys.com/ ; PyDPF-Core (контексты
  Entry и Premium): https://dpf.docs.pyansys.com/ ; PyOptiSLang: https://optislang.docs.pyansys.com/ ; защищённый gRPC:
  https://tools.docs.pyansys.com/version/stable/user_guide/secure_grpc.html
- MAPDL: лицензии HPC (до 4 ядер без HPC) и коды выхода — Operations Guide и Parallel Processing Guide,
  https://ansyshelp.ansys.com/
- Claude Code MCP (таймауты, фон, лимит вывода, `.mcp.json`): https://code.claude.com/docs/en/mcp
- Autodesk: Product Help MCP Server — https://adsknews.autodesk.com/en/news/product-help-mcp-server/ и
  https://blog.autodesk.io/autodesk-help-mcp-server/ ; статьи о Civil 3D в Core Console — поддержка Autodesk (заголовки
  выше, тексты недоступны без входа)
- Размеры колёс — страницы файлов PyPI соответствующих пакетов

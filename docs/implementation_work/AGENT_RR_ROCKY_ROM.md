# AGENT RR — Rocky DEM и ROM для `vkm-ansys` (статус: ПАУЗА, WIP)

Дата: 28.09.2026. Ветка `claude/agent-rr-rocky-rom-2026-09-28` (от `claude/corpus-platform-v0-2026-09-28`, `68e0bd2`),
в неё слиты ветки ANS (`efd90ef`: скелет сервера, `register(server, ctx)`, машинный замок лицензий) и MAT (`d3235cb`:
`vkm_jobs`). Конфликт в `pyproject.toml` разрешён: оба `package-data`, маркер `matlab` — от MAT, `ansys` — от ANS.

**Статус.** Работа остановлена по указанию координатора: пользователь поставил работы с Ansys (включая Rocky и ROM) на
паузу. Кода модулей `vkm_ansys.rocky` и `vkm_ansys.rom` ещё нет. Ни Rocky, ни MAPDL, ни ROM-инструменты не запускались.
Машинный замок `ansys` не занимался, процессов Ansys от агента нет. Всё ниже — discovery только на чтение. Любое число
из будущих прогонов — синтетика (MODEL_CHOICE / MODEL_RESULT), не наблюдение и не evidence.

## 1. Что сделано

| Что | Итог |
|---|---|
| окружение `work/venv-ansys` (Python 3.13.13) | поставлены одобренные пакеты: `ansys-rocky-core` 0.6.1 (с Pyro5 5.17, serpent 1.43, docker 7.2.0, click 8.4.1) и `pytwin` 0.12.0 (с pandas 3.0.6, tzdata 2026.4). numpy 2.5.3 и pydantic 2.13.5 не сдвинулись. Старый конфликт не наш: `skru1-research` требует PyYAML, которого в venv нет |
| Rocky | 26.1.0, сборка 44349 от 09.01.2026, `<ANSYS_ROOT>/rocky/bin`: `Rocky.exe`, `RockySolver.exe`, `RockyScheduler.exe`. Полный API PrePost Scripting 26.1 — стабы `rocky/bin/prepost_scripting_stubs/rocky30/plugins/api/*.pyi`: `RAStudy` (`ImportWall`, `CreateParticle`, `CreateVolumetricInlet`, `StartSimulation(non_blocking)`, `GetProgress`), `RAWall`, `RAParticle`, `RASolidMaterial`, `RAMaterialsInteraction`, `RAPhysics`, `RAMotionFrame` (`AddTranslationMotion`, `ApplyTo`), `RASimulatorRun` (`SetProcessingUnit` CPU / GPU / MULTI_GPU, `SetTargetGpu`, `SetSimulationDuration`, `SetTimeInterval`); результаты — `GetNumpyCurve`, `GetGridFunction`, `GetTimeSet` |
| PyRocky 0.6.1 | `launch_rocky(rocky_exe, headless=True, server_port=18615 по умолчанию)` запускает `Rocky.exe --pyrocky --pyrocky-port N --headless`. Связь идёт через Pyro5 по TCP `localhost` (Windows). Массивы numpy приходят через pickle, поэтому доверять можно только своему локальному процессу. Адрес, на котором слушает Rocky, не проверен (нужен smoke: только loopback) |
| GPU | RTX 5070 Ti, compute capability 12.0, 16 ГБ, драйвер 616.92. При проверке GPU был занят чужим процессом (≈ 6,7 ГБ, 69 %). Поддержка sm_120 GPU-решателем Rocky 26.1 — UNKNOWN до smoke |
| лицензии: read-only `lmutil lmstat -a`, только имена фич и число мест, без пользователей и хостов | `rocky_solver`, `rocky_preppost`, `rocky_hpc` — по 500; `ansrom` — 500; `twin_builder_dynamic_rom`, `twin_builder_runtime_pack`, `twin_builder_runtime_export`, `twin_builder_deployer`, `twin_builder_rapidprototype`, `simplorer_twin_models` — по 100; `anshpc`, `anshpc_gpu`, `anshpc_pack` — по 500. Занятых мест не было |

### ROM-маршруты 2026 R1 (найдены, не запускались)

1. **Static ROM Builder, CLI** — выбранный маршрут. Программа `<ANSYS_ROOT>/AnsysEM/common/ROMApps/SVDTools.exe`
   (2026 R1, сборка 22.12.2025) — её же вызывает Twin Builder. Команды:
   - `SROMbuild <conf>`: JSON с ключами `doe_file`, `snapshots_directory`, `write_rom_to`, `LOO`;
   - `SROMevaluate <conf>`: `read_rom_from`, `input_parameters_values` или `input_parameters_CSV`,
     `output_snapshots_filenames`;
   - `SROMexportFMU`: 11 аргументов и JSON свойств FMU;
   - `RSbuild <GARS|GARSFullStorage|Linear|MOP> in.csv out.csv <файл>`, `RSevaluate`,
     `RSbuildFMU <тип> in.csv out.csv <файл>.fmu <платформа> <rcDir> <withErrorPins>`, `GARSbuildFMU`.

   Библиотеки FMU лежат в `ROMApps/rc`. Формат снимка: int64 n, затем n чисел float64. Кроме снимков нужны `doe.csv`
   (Name и параметры), `points.bin` и `settings.json`. Образец — `AnsysEM/Examples/Twin Builder/Applications/
   Scripting/Ex7_StaticRomScripting`.

   План smoke:
   - MAPDL DOE игрушечного блока с 2–3 параметрами;
   - снимки поля, затем `SROMbuild` (полевой ROM) и `RSbuildFMU` (скалярный ROM, FMU 2.0);
   - оценка FMU через PyTwin (его `TwinRuntime` принимает `.twin` и FMU 2.0) и `SROMevaluate`;
   - сверка с прямыми прогонами MAPDL в точках вне DOE.

   Какую фичу лицензии берут SVDTools и PyTwin, неизвестно (UNKNOWN) до первого запуска.
2. **Workbench ROM Builder** (`<ANSYS_ROOT>/Addins/ROM`; команды журнала `RegisterSolverSystem`,
   `SwitchSolverSystemToProductionMode`, `ExportRomArchive`, `ExportFmu2`, `ExportTBLearningData`) — через 3D ROM
   DesignXplorer. Маршрут тяжелее и опирается на модули Workbench/Mechanical агента ANS2.
3. **TwinAI scripting** (`<ANSYS_ROOT>/TwinAI/scripting`, BETA) требует Docker Engine и сборки образа
   `hybrid_twin:26.1`: `app.tar.gz` на 1,16 ГБ плюс базовый образ rockylinux из сети. Использовать только после
   отдельного одобрения пользователя. Docker Desktop установлен, служба остановлена.

## 2. Что осталось

1. `src/vkm_ansys/rocky/` с `register(server, ctx)`:
   - `rocky_status` — установка, PyRocky, GPU, фичи `rocky_*`, процессы и адреса;
   - `rocky_api_help` — индекс стабов `.pyi`;
   - `rocky_run_script` — универсальный канал: задание PY в пуле `ansys`; entry в `in/`; машинный замок
     `vkm_ansys.licence_lock`; Rocky headless на свободном порту; проверка loopback; в скрипте доступны `rocky`,
     `api`, `JOB_DIR`, `OUT_DIR` и `result`;
   - `rocky_compression_box` — TOY-бокс: стенки из STL, объёмный инлет сфер, крышка с Translation, кривая силы
     крышки → σ–ε и пористость, всё MODEL_CHOICE и синтетика (процесс PC-38: закон уплотнения закладки в корпусе
     UNKNOWN);
   - `rocky_export` — кривые и статистика частиц из проекта.
2. `src/vkm_ansys/rom/`:
   - `rom_status`;
   - `rom_build`: `static_field` и `response_surface`; `toy_block` — MAPDL DOE;
   - `rom_evaluate`: FMU и `.twin` через PyTwin, `.rom` через `SROMevaluate`, `.gars`, `.lin` и `.mop` через
     `RSevaluate`.
3. Добавить `vkm_ansys.rocky` и `vkm_ansys.rom` в `vkm_ansys.context.OPTIONAL_MODULES`. Записать пины
   `ansys-rocky-core==0.6.1` и `pytwin==0.12.0` в extra.
4. Тесты без Ansys (контракт tools, запросы, парсеры) и live smoke за `VKM_ANSYS_LIVE=1` с маркером `ansys`.
   Квитанция `docs/corpus_platform/receipts/ansys_rocky_rom_smoke.json` со строкой в README квитанций. Строки tools в
   `MCP_TOOLS.md`.
5. Открытые вопросы:
   - на каком адресе слушает Pyro5-сервер Rocky;
   - GPU-решатель Rocky на sm_120;
   - фичи лицензий SVDTools и PyTwin для FMU;
   - коды выхода SVDTools: `--help` печатает только версию.

## 3. Как возобновить

- Слить свежие ветки ANS, ANS2 и MAT. У ANS2 в рабочем дереве виден общий слой продуктовых заданий
  (`product_jobs.py`, `product_submit.py`, `entry_kit.py`: entry в `in/`, машинный замок, `lmstat`-проба). На момент
  паузы он не закоммичен; если появится — Rocky и ROM строить на нём (сейчас в нём `APPS = MECH, WB, OSL`, а в
  `vkm_jobs` нет префиксов ROCKY и ROM, поэтому задания пойдут как `PY`).
- Пакеты в `work/venv-ansys` уже стоят. `VKM_SIM_ROOT` — ASCII-каталог на диске данных, как у ANS и MAT.
- Перед любым запуском проверить, свободен ли замок: `python -c "from vkm_ansys import licence_lock as L;
  print(L.status())"`. Запуски — только по явному указанию задачи.

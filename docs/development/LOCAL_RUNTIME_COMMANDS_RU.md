# Локальные runtime: команды проверки

Для подготовленного рабочего места используется небольшой `scripts/env_setup/runtime_preflight.py`. Он проверяет executable, версию Python и импорты в **уже выбранных** окружениях. Он не устанавливает зависимости, не загружает веса, не создаёт модели, не выполняет inference, не запускает MCP и не обращается к API. CPU threads ограничены двумя.

Все абсолютные пути задаются явно в **host-local JSON**, который не коммитится. Имена ролей и модулей зафиксированы в коде; JSON не принимает shell-команды или Python-код. Не следует заменять отдельные QGIS/ParaView/ML Python на общий interpreter.

| Роль | Проверяемый runtime | Основные импорты |
|---|---|---|
| `windows_mcp` | Windows Python хоста существующих MCP | mcp, vkm_cad, vkm_drawio, vkm_qgis |
| `windows_data` | Выбранный Windows Python обработки данных | NumPy, OpenCV, ezdxf, DuckDB |
| `windows_xany` | Подготовленный X-AnyLabeling venv | anylabeling, onnxruntime, PyQt6 |
| `wsl_sam` | Изолированное SAM окружение WSL | Torch, torchvision, sam2 |
| `wsl_data` | Подготовленное data окружение WSL | NumPy, GeoPandas, PyKrige, TA1 GeoPackage |
| `wsl_gempy` | Изолированное GemPy окружение WSL | gempy, gempy_engine |
| `wsl_uiuc` | Изолированный UIUC overlay WSL | Torch, torchvision, Lightning, torchmetrics, adamp |
| `qgis_worker` | Python существующего QGIS | PyQGIS, GDAL, PROJ |
| `paraview` | Python существующего ParaView | paraview, VTK |

## Preflight

Python оркестратора — 3.11 или новее. Команда из корня PUBLIC:

```powershell
python scripts/env_setup/runtime_preflight.py --config "$env:VKM_RUNTIME_CONFIG" --output work/runtime_preflight_new
```

Каталог результата должен быть новым и пустым. Все девять ролей обязательны по умолчанию. Для адресной проверки используется явный список:

```powershell
python scripts/env_setup/runtime_preflight.py --config "$env:VKM_RUNTIME_CONFIG" --output work/runtime_preflight_selected --only windows_mcp qgis_worker
```

`receipt.json` хранит реальные executable, версии, пути модулей, exit code каждого процесса и SHA конфигурации/скрипта. Отдельные stdout/stderr сохраняются рядом. Любой отсутствующий interpreter, ошибка импорта, несовпадение заданной версии Python или неуспешный subprocess даёт общий ненулевой exit. `PASS` здесь означает только готовность импортов; прежние receipts inference/GIS/CAD остаются отдельными доказательствами.

## Host-local JSON

Минимальный пример структуры для `--only windows_mcp wsl_sam`; угловые скобки заменяются фактическими абсолютными путями рабочего места:

```json
{
  "schema": "vkm-runtime-paths/1",
  "project_root": "<ABSOLUTE_WINDOWS_PUBLIC_ROOT>",
  "wsl_project_root": "<ABSOLUTE_WSL_PUBLIC_MOUNT>",
  "wsl_executable": "<ABSOLUTE_WSL_EXE>",
  "roles": {
    "windows_mcp": {
      "transport": "native",
      "interpreter": "<ABSOLUTE_MCP_PYTHON_EXE>",
      "expected_python": "3.13",
      "module_paths": ["<ABSOLUTE_PUBLIC_SRC>"]
    },
    "wsl_sam": {
      "transport": "wsl",
      "interpreter": "<ABSOLUTE_SAM_VENV_PYTHON>",
      "expected_python": "3.13",
      "distribution": "<EXISTING_WSL_DISTRIBUTION>",
      "user": "<EXISTING_WSL_USER>"
    }
  }
}
```

Для полного preflight в локальный JSON добавляют все роли из таблицы. `transport` фиксирован: `wsl` только для `wsl_*`, остальные — `native`. `expected_python` задаёт требуемую major.minor версию. Настройки не изменяют существующий venv.

QGIS дополнительно получает явные `module_paths`, `dll_dirs` и `environment_paths`: разрешены только `PYTHONHOME`, `QGIS_PREFIX_PATH`, `OSGEO4W_ROOT`, `QT_PLUGIN_PATH`. Это пути существующей поставки QGIS; pip GDAL/PyQGIS в MCP-host не устанавливается. ParaView запускается через его `pvpython` с фиксированным offscreen-флагом; рендер в preflight не выполняется.

WSL получает путь того же скрипта через явно заданные Windows/WSL корни проекта. Interpreter, distribution и user берутся только из JSON. `shell=True`, пользовательские команды и произвольные списки модулей не применяются.

## Адресные smoke-команды после изменения входов

Preflight не заменяет проверки результата. Для уже подготовленных runtime применяются существующие entrypoints:

- `core_client_smoke.py`: новый технический Codex app-server, discovery и вызовы MCP; модельные turn не запускаются. Этот тест не доказывает видимость tools в текущем UI.
- `geo_smoke.py windows|ta1|krige|gempy|paraview`: соответствующий interpreter из локального JSON, явный свежий `--output`; использовать `--help` для параметров выбранного режима.
- `cv_smoke.py sam|xany|critical`: только параметры, поддержанные `--help`, явные локальные inputs/checkpoint и свежий `--out`; отдельная GPU-блокировка для GPU-сценариев.
- `uiuc_component_smoke.py`: закреплённые официальные исходники и checkpoint через `--source-root`, `--checkpoint`, новый `--output`, общий `--gpu-lock`. Это отдельный standalone UIUC компонент; не запуск полного pipeline.

Эти команды могут выполнять workloads и поэтому не включены в preflight автоматически. Не повторять успешный inference без изменения входов или конкретного нового вопроса. Подготовленный X-AnyLabeling запускается отдельным локальным GUI launcher; preflight GUI не открывает, веса и платные API не загружает.

## Восстановление конфигурации

Репетиция rollback производится только на отдельной **shadow-копии**: восстановить backup побайтово, повторно добавить минимальный QGIS-блок и сравнить результат с текущими ожидаемыми байтами. Старые MCP/settings сравниваются семантически; SHA глобального конфига — с исходным manifest. Положительный shadow receipt не разрешает менять живой project/global config. Никаких автоматических правок/rollback в `runtime_preflight.py` нет.

Host-local JSON, launchers и receipts остаются в ignored `work/` либо разрешённом private run-каталоге. В PUBLIC хранятся только общий скрипт и эта инструкция.

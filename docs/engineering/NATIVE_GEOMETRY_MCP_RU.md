# MCP для Gmsh, ParaView и CloudCompare

`vkm-engineering` — сервер stdio на SDK `mcp==2.2.0`. Он использует существующий
`vkm_jobs`: отдельный каталог каждого задания, скрытый запуск Windows, очередь
`engineering`, тайм-аут, остановку дерева процессов и квитанции с SHA-256.
Исходные данные копируются; операции не меняют источник. Каталог задания не
является песочницей операционной системы для нативного приложения.

Установка из текущего checkout: `python -m pip install -e ".[engineering]"`.
Запуск: `python -m vkm_engineering.mcp_server` или `vkm-engineering`.
Среда сервера требует Python 3.13; установленный ParaView работает своим
`pvpython`, Gmsh и CloudCompare — своими существующими исполняемыми файлами.
Сервер не скачивает и не устанавливает приложения.

## Приватная конфигурация

Переменная `VKM_ENGINEERING_CONFIG` указывает на внешний JSON. Подставить
существующие абсолютные пути вместо маркеров; маркеры и переменные внутри JSON
не разворачиваются автоматически. Этот файл и конфигурацию MCP-клиента не
добавлять в PUBLIC.

```json
{
  "schema": "vkm.engineering_config/1",
  "job_root": "<PRIVATE_NEW_JOB_ROOT>",
  "executables": {
    "gmsh": "<EXISTING_GMSH_EXECUTABLE>",
    "paraview": "<EXISTING_PVPYTHON_EXECUTABLE>",
    "cloudcompare": null
  },
  "read_roots": {"case": "<READ_ONLY_CASE_DIRECTORY>"},
  "max_input_bytes": 1073741824
}
```

`job_root` — локальный абсолютный ASCII-путь без пробелов, вне клонов и всех
`read_roots`. Конфигурация доверенная и задаётся владельцем машины. Пути из
вызовов MCP — только `case:relative/data.vtu`; абсолютные пути, `..`, ADS,
символические ссылки и junctions внутри входного пути запрещены. Исполняемые
файлы, Python, shell, флаги и пути выходов клиент передать не может.

`engineering_status` не запускает программы: наличие файла и предыдущее
фактическое исполнение показаны отдельно, последнее связано с SHA executable.
Каталог с исходниками CloudCompare не считается установленным executable.
`engineering_probe` создаёт отдельное задание версии Gmsh либо ParaView.

## Операции

| Tool | Фиксированное действие и выходы |
|---|---|
| `gmsh_mesh` | Ровно один вход: `box_lengths:[x,y,z]` либо CAD STEP/BREP/IGES или один связный замкнутый STL; `size`, `order:1/2`; новый `out/mesh.msh` |
| `gmsh_inspect` | Нативный `-check` скопированного ASCII MSH2.2/4.1; строгие signature/sections/EOF до запуска; результат — stdout/stderr и квитанция |
| `paraview_inspect` | Фиксированные reader для VTU/VTP/legacy VTK/STL/PLY; `out/report.json`: counts, bounds, arrays, signed-volume range |
| `paraview_render` | Нативные `out/view.png`, `out/view.pvsm`, report; масштаб XYZ `[1,1,1]`, параллельная камера |
| `paraview_slice` | Явные конечные `origin:[x,y,z]`, ненулевой `normal:[x,y,z]`; дополнительно `out/slice.vtp`; пустой срез — ошибка |
| `cloudcompare_inspect` | Нативная загрузка/сохранение cloud/mesh в `out/result.bin`; для облака также `out/scalars.csv` |
| `cloudcompare_distance` | `mode:c2c/c2m`, compared cloud и reference cloud/mesh; BIN + нативный CSV со scalar fields; C2M явно UNSIGNED |

STL-профиль Gmsh требует **одну** связную замкнутую оболочку. Он не объединяет
молча независимые тела и не исправляет дефектную оболочку. Пользовательские
`.geo`, Python, PVSM и связные многокомпонентные VTK-документы не принимаются.
Ошибка нативной программы/проверки сохраняется как FAILED/CHECK_FAILED.
Для inspect поддерживаются только ASCII MSH2.2/4.1 с MeshFormat, Nodes,
Elements и необязательными Entities/PhysicalNames. Неизвестные/повторные секции,
binary MSH, Comments, scripts и содержимое после закрытых секций отклоняются;
проверяется фактически скопированный файл. Это важно: Gmsh может направить
неузнанный header даже файла `.msh` в GEO-парсер. В Gmsh4.15.2 CAD/STL
расширения направляются прямо в datareader, без этого fallback
([upstream OpenFile.cpp](https://raw.githubusercontent.com/live-clones/gmsh/gmsh_4_15_2/src/common/OpenFile.cpp)).

CloudCompare принимает облака PLY/XYZ/ASC/LAS/LAZ, сетки PLY/STL. BIN-вход
отклоняется: содержащийся в нём global shift может переопределить CLI.
При загрузке явно задаётся нулевой shift; ICP, AUTO shift, отсечки расстояний,
переименование координат и подбор регистрации не поддерживаются. Расстояния
требуют явного общего `frame` и `unit` (`m/mm/cm`); это заявление вызывающего,
а не проверенная регистрация. Нативная точность координат CloudCompare
сохраняется, метрологическая точность не утверждается.

Например, после `paraview_render(input_ref="case:mesh.vtu",
frame="DIAGNOSTIC_NOT_FIELD_ADMITTED", unit="m")` сохранить возвращённый
`job_id`. Затем использовать:

- `engineering_job_status` / `engineering_job_wait` (ожидание не более 60 с);
- `engineering_job_read(job_id, path="out/report.json")` либо `out/scalars.csv`;
- `engineering_job_receipt`: argv, executable/input/output SHA, checks, exit,
  время, timeout и состояние дерева процессов;
- `engineering_job_cancel` с причиной: остановка только собственного задания.

Запросы к чужим заданиям отклоняются. Инструментов произвольного запуска,
публикации или перезаписи PUBLIC нет. Время задания — 1–3600 с; завершённые
выходы и журналы сохраняются. Успешный exit не означает научного допуска.
Скалярный результат CC проверяется по нативному CSV, а не только по BIN.

## Проверки и границы

`python -m pytest -q tests/engineering/test_engineering_native_mcp.py` проверяет
ограничения путей/профилей, SDK-схемы, владение заданиями и фактическую остановку
процесса по тайм-ауту. Пропуск symlink-проверки при запрете создания ссылок
Windows остаётся SKIPPED. Для поставки дополнительно нужен настоящий stdio
initialize/list/call и нативные smoke на малых синтетических данных с receipts.

Координаты не трансформируются, EPSG/датум не выводятся автоматически.
Signed-volume диагностика VTK может зависеть от ориентации представления;
она не заменяет проверку физической мощности или качества FEM. Все выходы —
DERIVATION/MODEL_RESULT, `scientific_admission=NOT_GRANTED`. Исходные наблюдения,
геологические ограничения, свойства пород и механический допуск остаются
отдельными предметными проверками.

Синтаксис профилей сверяется с первичной документацией:
[Gmsh CLI и t13](https://gmsh.info/doc/texinfo/),
[ParaView Python](https://www.paraview.org/paraview-docs/latest/python/paraview.simple.html),
[CloudCompare CLI](https://cloudcompare.org/doc/wiki/index.php/Command_line_mode).

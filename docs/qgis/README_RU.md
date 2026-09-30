# vkm-qgis 0.1 — локальный MCP

Узкий мост к уже установленным PyQGIS/GDAL. Host Python 3.13 принимает MCP stdio,
а отдельный процесс Python из QGIS выполняет одну операцию. Host не импортирует
PyQGIS; worker не зависит от MCP. Установка пакетов и изменение конфигурации Codex
не требуются. Проверенный runtime: QGIS 4.2.2, GDAL 3.13.3, Python 3.12.14;
проверенный MCP host: Python 3.13.13, MCP 2.2.0.

## Запуск

Рабочий каталог — корень PUBLIC. Задать `VKM_QGIS_ROOT` для существующей установки
или `VKM_QGIS_PYTHON` для её Python launcher. На Windows при заданном root выбирается
`bin/python-qgis.bat`; также доступен поиск launcher в PATH. Никаких путей машины
в исходном коде нет. Использовать Python, в котором **уже установлен MCP**.

```powershell
$env:VKM_QGIS_ROOT = '<existing QGIS root>'
$env:PYTHONPATH = 'src'
$env:PYTHONUTF8 = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
# PRIVATE root берётся из VKM_RESOURCES_ROOT, если он задан.
& '<existing MCP host Python>' -m vkm_qgis.mcp_server
```

Entrypoint: `vkm-qgis = vkm_qgis.mcp_server:main`. Вызов без MCP для проверки runtime:
`python -m vkm_qgis.service`. При отсутствии runtime сервер сохраняет список tools,
`qgis_status` возвращает `available=false`, `status=UNAVAILABLE`, остальные операции
возвращают структурированную ошибку `QGIS_RUNTIME_UNAVAILABLE`. GUI не открывается.

## Контракт файлов и данных

- Ссылки: `work:relative/path`, `private:relative/path`, `public:relative/path`.
  Без префикса выбирается WORK. Корни: `VKM_PUBLIC_ROOT` или cwd;
  `VKM_WORK` или `PUBLIC/work`; PRIVATE — `VKM_RESOURCES_ROOT`.
- Запись разрешена только в WORK/PRIVATE. PUBLIC доступен для чтения.
  Абсолютные пути, traversal, escaping symlinks, неизвестные корни, URI и SQL/provider
  snippets отвергаются. Имена слоёв/полей — ASCII identifiers.
- Новые файлы и слои не перезаписываются. Единственные изменения существующих данных:
  explicit feature add/update, explicit vector append и регистрация GCP.
  Для feature update доступен `expected_sha256`; stable ID неизменяем.
- Операции сериализованы внутри одного host, включая input/output hashes.
  Это не распределённая блокировка между несколькими запущенными MCP host.
- Каждый вызов оставляет JSON receipt в `WORK/qgis_2026-09-30/receipts/`: timestamp,
  arguments hash, входные/выходные SHA-256, логическая команда, exit code и результат.
  stderr остаётся рядом в WORK. PUBLIC receipt содержит только санитизированную приёмку.
- Worker имеет лимит 120 s; на Windows таймаут завершает только принадлежащий вызову
  процесс и его дочерние процессы. Не затрагивает общие сервисы.
- GeoJSON source ограничен 32 MB, request — 20 MB; vector import/export — 100 000 features;
  GCP — 2–2000; overlay — 100 000 пар; PNG — 32–4096 px по каждому измерению.

Feature сохраняет `stable_id`, `source_id`, `page_id`, `figure_id`, `source_object_id`,
`extraction_method`, `verification`, `valid_from`, `valid_to`, `uncertainty`, `provenance`,
`epistemic_status`, `source_frame_id`. Дополнительные source properties сохраняются
как `properties_json`. Defaults: DERIVATION / AUTO_EXTRACTED_UNREVIEWED / UNKNOWN.
`FACT` не присваивается геометрии через этот мост. Reviewed digitization остаётся
отдельной проверкой источника, а evidence и WorldSpec создаются отдельным процессом.

## Координаты и GCP

Source geometry сохраняется в исходных px/pt/coordinates разреза. Ни WGS84, ни EPSG,
ни переворот осей не выводятся из формы рисунка. Source-coordinate FeatureCollection
не объявляется географическим RFC 7946 longitude/latitude GeoJSON. Его экспорт содержит
`vkm_coordinate_reference` с frame/unit/status. GeoPackage содержит таблицу `_vkm_layers`
с этой метаинформацией и одинаковый статус у всех features данного слоя.

Локальная метрическая система имеет `LOCAL_ENGINEERING_CRS / AUTHORITY_UNKNOWN`,
локальный WKT и пустой authority ID. У source-coordinate слоя CRS неопределённая.
Нельзя помещать pixel polygons в метрический `panels` или `mine_boundaries`.
Для каждого source frame создать отдельный слой SOURCE_COORDINATES с явными frame/unit.

По умолчанию импорт отклоняет GEOS-invalid контуры. Для архива исходных кандидатов
доступен explicit `preserve_invalid_source=true`: только SOURCE_COORDINATES и только
AUTO_EXTRACTED_UNREVIEWED / ASTRA_REVIEW_REQUIRED. Координаты и порядок колец сохраняются;
MakeValid и интерполяция не вызываются. Каждый объект получает `qgis_geometry_validity`
в `properties_json`, а результат импорта — topology status, count и причины ошибок.
Raw coordinate preservation PASS совместим с topology FAIL. Такой пакет является
архивом кандидатов. Render, overlay и apply_transform отклоняют слой с invalid geometry;
экспорт исходной геометрии разрешён. Feature update не позволяет повысить verification
некорректного кандидата. Исправления должны стать отдельными проверенными производными
объектами с provenance; исходный frozen GeoJSON остаётся неизменным.

GCP имеют source/target frame, source/target units и `target_unit_verified`.
Default units — `unknown`, verification — false. Target unit должна явно совпасть с
unit существующего слоя control_points; единица не подставляется автоматически.
В fit метры возникают только при `target_unit=m` **и** `target_unit_verified=true`.
Иначе сохранённый transform имеет `UNKNOWN_CRS`; apply сохраняет target source frame,
а не превращает px в метры. Флаг подтверждения — утверждение вызывающего проверяющего,
его основание должно сохраняться в provenance GCP.

Допустимые роли GCP: GCP, SHAFT, LABELLED_BOREHOLE, GRID_INTERSECTION,
PROFILE_INTERSECTION, WORKINGS_INTERSECTION, BOUNDARY_VERTEX, ENGINEERING_OBJECT.
Observed subsidence как GCP запрещён. Пара point/match записывается одной SQLite
транзакцией; Point имеет стандартный GeoPackage binary header + WKB, читаемый PyQGIS.

Поддерживаются similarity и affine. Сохраняются матрица, исходные соответствия,
нормализованный condition number, signed per-point residual, RMS, max residual,
leave-one-out для каждой точки и source/target extents. Проверяются rank и SVD linear
части; collapsed/singular transforms отклоняются даже при RMS=0. Для минимального числа
точек LOO остаётся NOT_RUN. RMS не подтверждает правильность correspondence или CRS.
Projective/nonlinear не реализованы без обоснованной отдельной задачи.

## API

| Tool | Результат / изменение |
|---|---|
| `qgis_status` | Реальные версии/runtime availability, ограничения |
| `qgis_project` | create NEW qgs/qgz / open rooted project; local OGR/GDAL only, macros rejected |
| `qgis_create_geopackage` | NEW GeoPackage и 15 минимальных слоёв |
| `qgis_list_layers` | open Gpkg, типы, fields, counts, frame metadata |
| `qgis_create_layer` | NEW vector/aspatial layer, explicit frame/unit |
| `qgis_import_vector` | source-coordinate FeatureCollection; NEW layer или explicit append |
| `qgis_import_raster` | NEW companion GeoTIFF; supplied pixels/georeferencing copied by GDAL |
| `qgis_feature` | get/list/add/update; stable ID, status policy, optional optimistic SHA |
| `qgis_register_control_points` | Atomic point/match pairs с provenance/units |
| `qgis_fit_transform` | NEW transform JSON, similarity/affine diagnostics |
| `qgis_residuals` | Saved diagnostics + transform SHA |
| `qgis_apply_transform` | NEW derived Gpkg, parents + transform SHA |
| `qgis_overlay` | NEW intersection/difference/union Gpkg, area в units² |
| `qgis_render` | NEW PNG, CPU renderer, common explicit frame |
| `qgis_export_layer` | NEW source/local-coordinate FeatureCollection |

Overlay требует идентичной frame metadata; mixed-dimensional results возвращают
`UNSUPPORTED_OVERLAY`, а не отбрасываются. Intersection сохраняет пары исходных
features, поэтому перекрывающиеся features могут давать повторно посчитанную area;
для площади объединённой геометрии использовать union. Растр хранится companion
GeoTIFF, не как Gpkg tile pyramid. Render сейчас поддерживает vector layers одного
GeoPackage; проект может хранить vector и raster layers вместе.
Render использует фиксированные point/line/polygon symbols и 96 DPI; повторный render
с одинаковыми входами проверяется на равенство SHA-256 PNG. Receipts содержат timestamp
и request UUID, а QGIS/GDAL container metadata может зависеть от runtime.

Минимальные слои: source_figures, control_points, control_point_matches, transforms,
mine_boundaries, shafts, boreholes, profile_lines, panels, blocks, mining_zones,
anomalies, geology_sections, stratigraphy, conflicts. Они изначально пусты.
Источник transform JSON остаётся отдельным артефактом; aspatial `transforms` служит
каталогом, его feature можно добавить explicit через `qgis_feature`.

Пример source-coordinate импорта через Python host без MCP:

```python
from vkm_qgis.service import QgisService
s = QgisService()
s.call('qgis_create_geopackage', {'path':'work:geometry/source.gpkg'})
s.call('qgis_import_vector', {
    'source_path':'work:geometry/source_plan.geojson',
    'path':'work:geometry/source.gpkg', 'name':'source_plan',
    'crs_kind':'SOURCE_COORDINATES', 'frame_id':'SRC-FIGURE-PAGE-PIXEL', 'unit':'px'})
```

## Проверка

Реальная приёмка исходного архива 30.09.2026: frozen manifest
`452cd217ffd363edfc79ecfb253ff7e37f66956e53cb7c231f3e2ea818b8c428`,
409 входных слоёв, 326 непустых и 83 пустых шаблона. PRIVATE scratch
`work:geometry_2026-09-29/geometry_sources.gpkg` содержит 341 слой
(15 пустых базовых + 326 исходных) и 207 165 объектов; SHA-256
`2c2c6a59ffb045607ed3266144aa57f9708b01c2d9562318b99ca86e87959bf0`.
Проверены все stable IDs, source objects, frames, input hashes, coordinate nesting,
source statuses, provenance и fingerprints слоёв. JSON-сравнение координат использует
допуск 1e-9 исходной единицы; максимальное наблюдённое расхождение 2.2737367544323206e-13.
Raw coordinate preservation — PASS. Topology — FAIL: 1 938 invalid candidates
в 79 слоях; ни один не исправлен MakeValid.

Статусы сохранены: 207 126 AUTO_EXTRACTED_UNREVIEWED, 19 ASTRA_REVIEW_REQUIRED,
20 VERIFIED_BY_EYE; все объекты DERIVATION. Двадцать табличных точек находятся
в `control_points_source_table`, units UNKNOWN; метрический слой `control_points` пуст.
Архив и полный per-file QA остаются WORK/PRIVATE. Чтение списка 341 слоя и проверка
downstream rejection через host завершились за 7.86 s без изменения SHA-256 пакета.
Для большого GeoPackage список слоёв читает container schema/counts SQLite и разбирает
каждую уникальную сохранённую WKT через QGIS; операции с геометрией открывают адресный
OGR layer. Это избегает повторного чтения полной схемы для каждого слоя.

```powershell
python -m pytest -q --basetemp=work/qgis_2026-09-30/unit_tmp `
  -o cache_dir=work/qgis_2026-09-30/pytest_cache tests/qgis
& '<existing MCP host Python>' tests/qgis/test_stdio.py
```

Unit tests проверяют fitting/LOO, collapse, unit/CRS gates, path/no-overwrite policy,
JSON errors и unavailable runtime. Real acceptance создаёт synthetic Gpkg/vector/raster,
проверяет add/update/get, source-coordinate roundtrip/append, known similarity/affine,
GCP geometry читается QGIS, negative calls сохраняют hashes, overlay, PNG содержимое,
GDAL pixels/geotransform и project create/open. Stdio test выполняет initialize,
list_tools и реальные call_tool, включая invalid source archive и downstream reject;
второй server проверяет UNAVAILABLE. Bowtie regression проверяет строгий отказ,
source-only/status guards, буквальное сохранение geometry и запрет downstream operations.

Все fixtures PUBLIC синтетические. Symlink test может SKIP без соответствующей
привилегии Windows; stdio test SKIP в Python без MCP, но проверяется отдельно уже
имеющимся MCP host. Реальные source geometry/координаты остаются WORK/PRIVATE.
Этот пакет не строит surfaces, не запускает CAD/solver/ML/GPU и не меняет CORE/EDGE.

Schemas: [result](schemas/result.schema.json), [transform](schemas/transform.schema.json).
Приёмка: [qgis_mcp_2026-09-30](../corpus_platform/receipts/qgis_mcp_2026-09-30.json).

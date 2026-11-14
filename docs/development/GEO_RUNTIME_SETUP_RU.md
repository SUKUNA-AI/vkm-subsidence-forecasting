# Синтетическая проверка геоинструментов

Эти команды проверяют локальный runtime, без научных данных и модели ВКМ.
Синтетические базы, DXF, изображения и квитанции хранятся в приватной рабочей
папке. Каждый запуск получает новую папку: скрипт не очищает прежние результаты.

Исправные Windows Python, QGIS и ParaView переиспользуются. В Python QGIS
нельзя устанавливать произвольные pip/GDAL/Qt. Для TA1 создан отдельный
Python 3.12: upstream `DARPA-CRITICALMAAS/ta1-geopackage`, commit
`57101150ced8f39c5e229d8234f29c41e42764f7`, пакет 0.2.1, GeoPandas 0.14.4,
NumPy 1.26.4, SQLAlchemy 2.0.46. SQLAlchemy 2.1 несовместим с используемым
SQLAlchemy-utils 0.41.2 (`ScalarAttributeImpl`). GemPy 2026.0.3 и engine
2026.0.3.post1 работают отдельно с NumPy 2.5.4 на CPU.

## Установка и повтор

В WSL/Linux задайте четыре абсолютных пути; `TA1_SOURCE` — уже клонированный
официальный репозиторий на указанном commit. Загрузки исходников и смена Git
ветки скриптом не выполняются. `PRIVATE_BASE` — отдельная новая папка, не общая среда.

```bash
bash scripts/env_setup/geo_install.sh "$PYTHON312" "$TA1_SOURCE" "$PRIVATE_BASE" "$PRIVATE_RECEIPTS"
```

Скрипт закрепляет версии всех установленных Python-зависимостей и сохраняет
`pip freeze`. Повтор переиспользует две помеченные среды `data` и `gempy`;
чужую среду без собственной метки изменять отказывается. Инсталляционный
вывод и exit code сохраняет вызывающий процесс. На проверенной машине
занято приблизительно 457 MiB и 65 MiB соответственно; точный объём зависит
от hardlinks/cache. Новые модели, CUDA или GPU для этих проверок не нужны.

## Исполнение

В примерах `OUT` — новая приватная папка, `GEO_PYTHON` — исправный Windows
Python с ezdxf, OpenCV, DuckDB и Shapely; `PVPYTHON` — штатный pvpython ParaView.
Проверены ezdxf 1.4.4, OpenCV 5.0.0, DuckDB 1.5.6, Shapely 2.1.2.

```powershell
& $env:GEO_PYTHON scripts/env_setup/geo_smoke.py windows --output "$env:OUT/windows"
& $env:PVPYTHON --force-offscreen-rendering scripts/env_setup/geo_smoke.py paraview --output "$env:OUT/paraview"
```

```bash
OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 "$PRIVATE_BASE/data/bin/python" scripts/env_setup/geo_smoke.py ta1 --output "$OUT/ta1"
OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 "$PRIVATE_BASE/data/bin/python" scripts/env_setup/geo_smoke.py krige --output "$OUT/krige"
OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 "$PRIVATE_BASE/gempy/bin/python" scripts/env_setup/geo_smoke.py gempy --output "$OUT/gempy"
```

`windows` создаёт и читает DXF с XDATA stable ID/frame/unit/ролью кольца,
проверяет слои и координаты. Полигон с отверстием представлен двумя
замкнутыми LWPOLYLINE с общим ID и ролями `shell`/`hole:0`.
DXF `$INSUNITS=0`; единицы px записаны отдельно, географическая CRS не приписана.

OpenCV сохраняет mask/overlay и координаты. На горизонтальном штрихе центр
имеет y=40, контур — границы y=35/45. Метод оси — центроид пикселей в колонке,
пригодный именно для этого синтетического теста; точность на картах не измерялась.
Для путей с кириллицей PNG кодируется через OpenCV `imencode`, записывается
через Python и читается обратно: Windows `imwrite` этой сборки не принимает
абсолютный путь с кириллицей.

DuckDB выполняет официальный `INSTALL spatial`, закрывает соединение и
в новом соединении выполняет `LOAD spatial`. В отдельной базе проверяются
WKB, Point/LineString/Polygon, одно отверстие, NULL и spatial query.
Операции сопоставляют frame/unit; отрицательная проверка отклоняет несовпадение
рамок. Production reader и живая научная база не изменяются, unsigned extensions
не включаются. Ограниченный production reader проверяется отдельно в своём контракте.

TA1 writer/reader проверяет три слоя, ссылки legend/type и foreign keys.
Закреплённые upstream fixtures оставляют SQLite application_id/user_version
равными нулю; GDAL предупреждал о плохом application_id. Наш адаптер только
для нового synthetic GPKG устанавливает application_id=0x47504B47 и
user_version=10200 согласно [GeoPackage 1.2, Requirement 2](https://www.geopackage.org/spec120/#_file_format).
Исходный header и изменение записываются в receipt; upstream не редактируется.
Это проверка header, а не полная сертификация соответствия OGC.
Используется официальный `CRITICALMAAS:pixel` (GPKG srs_id=0), дополнительная
таблица `synthetic_frame_metadata` хранит `SYNTHETIC_PIXEL`, px и synthetic scope.
Слои должны дополнительно читаться штатным QGIS; успешный Fiona reader этого
не заменяет. ParaView читает 4 точки/1 tetra VTU, проверяет scalar range 0..3
и сохраняет реальный offscreen PNG. PyKrige воспроизводит исходные значения
и интерполирует 5×5 сетку. GemPy вычисляет 10×10×10 модель горизонтальной
границы z=5 через NumPy/CPU; это проверка runtime, без научного допуска.

## Сквозной переход E19

Общий набор: Point(10,20), LineString((0,0),(5,5),(10,0)), квадрат 0..10 с
отверстием 2..4; stable IDs `point-001`, `line-001`, `polygon-001`.
Рамка `SYNTHETIC_PIXEL`, unit px, SRID=0. Допуск 1e-9 px задан до запуска.
Сравниваются топология, координаты, площадь, отверстия, ID и метаданные;
побайтовое тождество контейнеров не требуется.

```powershell
# Первый вызов экспортирует DuckDB WKB; без PostGIS readback намеренно exit=1.
& $env:GEO_PYTHON scripts/env_setup/geo_smoke.py roundtrip --gpkg-wkb "$env:OUT/ta1/gpkg_wkb.json" --output "$env:OUT/e19-export"
# Передайте e19-export/duckdb_to_postgis.json в изолированный PostGIS worker.
# После реального SQL roundtrip используйте его readback с features/id/frame/unit/srid/wkb_hex.
& $env:GEO_PYTHON scripts/env_setup/geo_smoke.py roundtrip --gpkg-wkb "$env:OUT/ta1/gpkg_wkb.json" --postgis "$env:POSTGIS_READBACK" --output "$env:OUT/e19-final"
```

Скрипт требует полный PostGIS readback, затем пишет DXF и читает его заново.
Он проверяет переносимый результат; отдельная квитанция PostGIS worker должна
подтверждать реальное исполнение SQL и SHA входа. Сам JSON не доказывает SQL.
Квитанции `*_execution.json` содержат interpreter, время, exit code и ошибку;
обязательный провал возвращает ненулевой код. E19 закрывается только после
проверки всех звеньев, E11 — после отдельного QGIS reader.

## Откат

Закройте только свои процессы. Сверьте пути и метки `.vkm-geo-synthetic-20261114`,
сохраните квитанции и нужные артефакты. После этого можно удалить только две
собственные среды из `PRIVATE_BASE` и собственные scratch outputs. Скрипты
не меняют системный Python, GIS-среду, штатные QGIS/ParaView, scientific/canonical
или CORE/EDGE. Установка DuckDB spatial использует обычный пользовательский
кэш расширений; для общего кэша автоматический rollback не предусмотрен,
поскольку расширение может использоваться другими читателями.

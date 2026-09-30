# Исходная геометрия материалов ВКМ / СКРУ-1

Код извлекает source geometry в пикселях/точках страницы, проверяет native clipping/paint state, сохраняет цветовые компоненты и linework с provenance, предлагает source-image correspondences и считает similarity/affine/projective residuals и leave-one-out. Метры, глубины, даты и семантика не назначаются без проверки.

```bash
python -B benchmarks/geometry_skru1_v1/run.py --targeted
python -B benchmarks/geometry_skru1_v1/annotate.py --annotations work/geometry_2026-09-29/manual_source_annotations.json
python -B benchmarks/geometry_skru1_v1/package.py
work/venv-desktop/Scripts/python.exe -B benchmarks/geometry_skru1_v1/export_dxf.py --manifest work/geometry_2026-09-29/qgis_import_manifest.json
python -B benchmarks/geometry_skru1_v1/finish.py --inventory-count 411 --tests-passed 17
python -B -m pytest -q tests/corpus/test_geometry_skru1_v1.py
```

Используются существующие NumPy, Pillow, OpenCV и PyMuPDF. Ничего не устанавливается. PRIVATE находится через `VKM_RESOURCES_ROOT`; для текущей локальной раскладки допустим вложенный ignored resources checkout. Outputs, literal tables, координаты, source fragments, overlays и очередь `ASTRA_REVIEW_REQUIRED` остаются в `work/geometry_2026-09-29/`.

Координаты выбранных ROI, swatches легенды и пар регистрации задаются отдельным PRIVATE `source_regions.json` через `--source-regions`. Конфигурация привязана к SHA-256 оригинальных изображений; несовпадение или выход рамки за изображение отклоняются. PUBLIC содержит только загрузчик и синтетические проверки. До публикации координатные константы исполненной версии перенесены без изменения значений; исходный исполненный код и migration receipt сохранены в PRIVATE. Геометрию при переносе не пересчитывали. Команды extraction предназначены для нового пакета: при наличии frozen manifest нужно явно выбрать новый `--out`.

`layers_manifest.json` описывает автоматические source layers. `annotate.py` материализует отдельный PRIVATE JSON с явно проверенными по изображению объектами; буквальные данные в публичном коде отсутствуют. Независимая приёмка этих annotations остаётся обязательной. `package.py` объединяет слои и проверяет SHA в `qgis_import_manifest.json`: это неизменяемый вход QGIS/DXF. После freeze не перезаписывать исходные GeoJSON: новый extraction оформляется новой версией пакета.

Один source frame на слой, explicit coordinate reference/units, SHA и overlay. Source-coordinate GeoJSON — локальный interchange container, его координаты нельзя интерпретировать как WGS84. У table xcoord/ycoord/exel_X/Y_coord единицы и CRS остаются UNKNOWN; порядок чисел не доказывает метры. Геопривязка к местной метрической сетке требует проверенных control identities и единиц. Training RMS или условная LOO после RANSAC не подтверждают точность. Источники других рудников не получают автоматически атрибуцию СКРУ-1.

Native DOCX tables сохраняют gridSpan/vMerge и cell XML locator. План не становится исполнением. Колонка с разрывами масштаба остаётся в source coordinates. EMF может содержать только bitmap: проверяются фактические records, расширение файла не считается доказательством vector content.

DXF экспорт использует уже имеющийся `ezdxf`, без запуска CAD. Каждый файл сохраняет один frame, source units и provenance XDATA; roundtrip проверяет координаты/вершины. Polygon holes записаны отдельными polylines. Невалидные contour candidates остаются source linework: MakeValid и молчаливое восстановление физической топологии не выполняются. Строгий QGIS импорт должен отклонять такую геометрию, пока она не сохранена в явно помеченный source-only archive с запретом downstream scientific use.

`finish.py` создаёт PRIVATE `ACCURACY.md`/`REVIEW_GEO_RU.md` и public-safe [receipt](../../docs/corpus_platform/receipts/geometry_skru1_v1.json), содержащую только whitelisted статистику, logical artifact IDs и SHA. Фактическая обработка B/C охватила135 source objects; inventory A411 шире. Это source extraction, не завершённая метрическая/геологическая/временная приёмка и не WorldSpec.

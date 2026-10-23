# Сборка инженерной геологии и критерии качества

Разрешение на реконструкцию относится к конкретной задаче владельца от 17.10.2026. Этот процесс не запускается автоматически при извлечении evidence. Научные ограничения и результаты — [отчёт](../science/ENGINEERING_REVIEW_20261017_RU.md).

## Данные и роли

`scripts/build_engineering_geology.py` принимает явные `--config`, `--contacts`, `--placements`, `--previous-scene`, `--three`, `--output`. Реальные выходы допускаются только в ignored `work/`, существующая директория не перезаписывается. Для повторной сборки нужен PRIVATE пакет `12_work_exports/geology_surface_review_20261017/`.

Конфигурация фиксирует методы до оценки, размер сетки, общую поправку отметок и отдельный диапазон чувствительности. `source rows` сохраняют физический ID, mine scope, исходные глубины и пропуски. Контакты расположены сверху вниз при Z вверх. Неполные записи остаются в таблице размещения, но не получают выдуманные контакты.

Проверяется гипотеза `Z_contact = DSM_collar + site_offset - depth`. Абсолютная точность datum этим не устанавливается. Для полного контекста используется единое поле глубин, привязанное к исходной DSM, с отдельной маской EXTRAPOLATION. Глубины и исходные отметки не перезаписываются. Поддержка ограничивает интерполяцию; отключение ограничения требует явного `extrapolate=True` и маркируется в результате.

## Версия v6 (23.10.2026): один предсказатель, частичные наблюдения, сечения по TIN

Изменения по повторному ревью 17.10.2026 (R2-01…R2-03):

- **Один предсказатель для сборки и проверки** (`src/vkm_world/geometry_build/contact_predictor.py`). Конфигурация
  `PredictorSpec` фиксирует метод, параметризацию (`LEVEL` — абсолютные отметки; `SURFACE` — отметка минус оператор
  поверхности + поправка), политику экстраполяции и область опор. Сборка и каждый fold вызывают один и тот же
  `ContactPredictor.predict`; fold отличается только набором опор. Раньше полная модель интерполировала глубины и
  прибавляла DSM в цели, а проверка — абсолютные отметки; разница операторов `H(q) − Σ w_i H(x_i)`.
- **Проверка** (`cross_validate`): исключение целой физической группы (копии одного объекта — через `identity_groups`)
  и фиксированные квадратные блоки (`cv_block_size_m`, привязка к floor(min XY / размер), выбирается до просмотра
  ошибок). Каждая удержанная опора помечается INTERPOLATION (внутри выпуклой оболочки обучающих) или EXTRAPOLATION;
  предсказание делает производственная политика метода. Отчёт: attempted / predicted / refused, собственный и общий
  охват, bias/MAE/RMSE отметок и мощностей. Абсолютная проверка — только по независимым отметкам устьев
  (`independent_collars` в конфигурации); отметки по DSM независимой истиной не являются.
- **Частичные наблюдения** (области `*_PARTIAL`): точечные глубины неполных колонок, интервалы «контакт не вскрыт до
  глубины X», мощности и наблюдённое отсутствие слоя (нулевая мощность) из проверенной матрицы
  `vkm.borehole_observation_matrix/1` (`--observations`, используются строки с `use_in_model: true`). В каждой цели
  решается упорядоченная взвешенная задача наименьших квадратов в «приращениях»: невязка отметки самого верхнего
  отсчёта опоры и невязки промежутков между соседними отсчётами; переменные (p₀, t_j ≥ 0), нулевая мощность допустима.
  Для полных колонок это ровно прежнее общее взвешенное среднее; недостающий контакт наследует местную мощность, а не
  среднюю отметку. Интервалы действуют только при нарушении. Контакт вне пространства отсчётов возвращается NaN —
  без скрытого регуляризатора. Сдвиг относительно оценки только по точкам сохраняется как `constraint_adjustment_m`.
  Интервал с нулевой мощностью экспортируется только контактами (PINCHOUT_CONTACT_ONLY), не вырожденным объёмом.
- **Сечения** (`src/vkm_world/geometry_build/tin_section.py`): точное P1-сечение экспортированных треугольников с
  сохранением всех пересечений рёбер, точек поворота и вершин на трассе; сравнение с непрерывной моделью выполняется
  в тех же точках и помечено как выборочное, не супремум. Скважины выносятся на трассу с расстоянием (`project_onto_trace`).
- **Версия геометрии**: `geometry_digest(nodes, triangles, contacts)` записывается в `qa.json`, `sections.json`,
  `contact_fields.npz` и FieldData каждого VTU; планы и разрезы ВКР ссылаются на этот хеш.

Команда сборки v6 (входы — как ниже, плюс матрица наблюдений):

```powershell
python scripts/build_engineering_geology.py --config <work>/engineering_config_v6.json --contacts work/geology_surface_20261017/evidence/source012_all_joint_contacts.json --placements work/geology_surface_20261017/placement/support_placement.json --previous-scene work/skru1_linked_world_20261016/delivery_complete/data/scene.js --three work/skru1_unified_3d_20261017/vendor/three.min.js --observations <work>/observations_model_input.json --output <work>/engineering_v6
```

Конфигурация v6 добавляет `cv_block_size_m`, `cv_block_basis`, области `*_PARTIAL`, `independent_collars`, `collar_flags`.
Конфигурация, матрица наблюдений и результаты хранятся в PRIVATE (`12_work_exports/geology_misis_20261023/`).

## Последовательность команд

После восстановления PRIVATE входов в указанную в квитанциях структуру `work/`:

```powershell
python scripts/build_engineering_geology.py --config work/geology_surface_20261017/engineering_config.json --contacts work/geology_surface_20261017/evidence/source012_all_joint_contacts.json --placements work/geology_surface_20261017/placement/support_placement.json --previous-scene work/skru1_linked_world_20261016/delivery_complete/data/scene.js --three work/skru1_unified_3d_20261017/vendor/three.min.js --output work/geology_surface_rebuild
python scripts/enrich_engineering_view.py --model work/geology_surface_rebuild --context work/skru1_unified_3d_20261017/data/spatial_objects.js --output work/geology_surface_view_rebuild
python scripts/build_pinchout_branch.py --field work/geology_surface_rebuild/FULL_CONTEXT/contact_fields.npz --registration work/geology_surface_20261017/evidence/source048_profile_registration.json --output work/pinchout_rebuild
python -m pytest -q tests/world
python scripts/verify_canonical_repository.py --output work/verification/canonical_verification.json
```

Верификатор запускают с `VKM_RESOURCES_ROOT`, указывающим на локальный PRIVATE. В окружении Windows без права создания symlink весь suite следует запускать в WSL; охранный тест не отключают.

`enrich_engineering_view.py` берёт из прежнего контекста только плановые линии, символы и календарные метаданные. Старые объёмные meshes не добавляются в новую геологию. Планы лежат на DSM исключительно для ориентирования; они не становятся поверхностью КрII или объёмами выемки. Геологические массивы проверяются по хешам до и после копирования. В просмотре доступны исходная поверхность, масштаб 1:1, отмеченное вертикальное преувеличение, полная область и срезы.

## Приёмка

| Объект | Обязательная проверка |
|---|---|
| Контакты | Конечные значения, порядок, отсутствие незаявленного нижнего предела мощности, воспроизведение использованных опор именно экспортированной сеткой |
| Плановая сетка | Висячие вершины, ложные внутренние границы, overlap/gap/outside в пределах вычислительного допуска, положительная ориентация |
| Объём | Знак объёма без маскирующего abs, общие границы, дубли/неманифолдные грани |
| Состояние выемки | В каждом элементарном объёме одна роль: порода, пустота либо закладка |
| Высоты | Native фактически сохраняет Points; display отвергается расчётным потребителем; independent geodetic transform требует отдельных опор |
| Поверхность | Исходный продукт, дата/эпоха, datum, маски качества; сглаживание отдельно |
| Поставка | SHA-256 входов и выходов, независимое повторное чтение, отсутствие секретов и PRIVATE данных в PUBLIC |

`scripts/audit_skru1_surface.py` выпускает паспорт и маски поверхности. `scripts/register_geology_supports.py` связывает инженерные XY с исходным растром и сохраняет сценарный диапазон по области размещения. Модули `mesh_audit.py`, `material_partition.py`, `height_frames.py`, `constrained_geology.py` содержат проверки, используемые при сборке и употреблении данных.

PINCHOUT_CONTACT_ONLY — отдельный продукт из контактных VTP/NPZ и GIS-маски. Совпадающие кровли допустимы; такой файл не выдаётся за готовый объёмный конечный элемент. Ансамбль альтернатив не складывается как одновременные материалы.

## Передача локальных координат в GIS

Для GIS предназначен GeoPackage с пользовательским `LOCAL_CS` SH1, единицами в метрах и явно неизвестной глобальной привязкой. Локальные массивы координат в файлах `.geojson` не являются географическим GeoJSON RFC 7946. Экспорт не назначает EPSG и не преобразует координаты. Z скважин сохраняет статус инженерного устья по DSM; полевые отметки этим не устанавливаются.

В существующем Python-окружении GDAL/QGIS:

```powershell
python scripts/export_engineering_gpkg.py --boreholes work/geology_surface_view_rebuild/mapped_boreholes.geojson --coverage work/geology_surface_view_rebuild/coverage.geojson --absence work/pinchout_rebuild/absence_scenarios.geojson --output work/geology_gis_rebuild --epoch 2026-10-17T00:00:00.000Z
```

Имена входных слоёв проверяют по `gis/receipt.json` из PRIVATE-поставки. Приёмка требует повторного чтения координат и атрибутов независимым GIS-клиентом, проверки SQLite и отсутствия необоснованного EPSG. Поставка от 17.10.2026 прочитана в QGIS и `ogrinfo`; повторная сборка GeoPackage побайтово идентична.

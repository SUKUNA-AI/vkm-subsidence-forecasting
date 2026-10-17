# Сборка инженерной геологии и критерии качества

Разрешение на реконструкцию относится к конкретной задаче владельца от 17.10.2026. Этот процесс не запускается автоматически при извлечении evidence. Научные ограничения и результаты — [отчёт](../science/ENGINEERING_REVIEW_20261017_RU.md).

## Данные и роли

`scripts/build_engineering_geology.py` принимает явные `--config`, `--contacts`, `--placements`, `--previous-scene`, `--three`, `--output`. Реальные выходы допускаются только в ignored `work/`, существующая директория не перезаписывается. Для повторной сборки нужен PRIVATE пакет `12_work_exports/geology_surface_review_20261017/`.

Конфигурация фиксирует методы до оценки, размер сетки, общую поправку отметок и отдельный диапазон чувствительности. `source rows` сохраняют физический ID, mine scope, исходные глубины и пропуски. Контакты расположены сверху вниз при Z вверх. Неполные записи остаются в таблице размещения, но не получают выдуманные контакты.

Проверяется гипотеза `Z_contact = DSM_collar + site_offset - depth`. Абсолютная точность datum этим не устанавливается. Для полного контекста используется единое поле глубин, привязанное к исходной DSM, с отдельной маской EXTRAPOLATION. Глубины и исходные отметки не перезаписываются. Поддержка ограничивает интерполяцию; отключение ограничения требует явного `extrapolate=True` и маркируется в результате.

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

# Обмен геологией и парные источниковые проверки

Текущие интерфейсы работают с явно заданными объектами и трассами всего рассматриваемого поля.
Локальный пример не является селектором по умолчанию. Данные и результаты остаются в PRIVATE
либо ignored `work/`; в PUBLIC используются только синтетические fixtures.

## Общие входы

`--frame` — JSON `vkm.coordinate_frame_declaration/1` со следующими полями:

```json
{
  "schema": "vkm.coordinate_frame_declaration/1",
  "horizontal_frame": "SOURCE_LOCAL_XY",
  "height_frame": "SOURCE_Z_UNKNOWN_DATUM",
  "length_unit": "m",
  "z_positive": "UP",
  "global_tie": "UNKNOWN",
  "basis": "DECLARED_IDENTITY_NO_COORDINATE_OPERATION",
  "inputs": {
    "field": {"horizontal_frame": "SOURCE_LOCAL_XY", "height_frame": "SOURCE_Z_UNKNOWN_DATUM"},
    "sections": {"horizontal_frame": "SOURCE_LOCAL_XY", "height_frame": "SOURCE_Z_UNKNOWN_DATUM"},
    "objects": {"horizontal_frame": "SOURCE_LOCAL_XY", "height_frame": "SOURCE_Z_UNKNOWN_DATUM"}
  }
}
```

Это явное утверждение вызывающей стороны о совместимости входов, а не доказательство геодезической
привязки. Для assessment перечисляются также `contacts`, `placements`, `surface`, `boundary`.
Отсутствующая декларация, несовпадающие frames, другая единица и направление Z отвергаются.
Смещение не выводится из DSM, неизвестный datum не заменяется нулём, EPSG не назначается.

## GeoPackage

```powershell
& $qgisPython scripts/export_geology_gpkg.py --field $field --sections $sections `
  --objects $objects --frame $frame --object-id $objectId --trace-id $traceId `
  --history $history --observations $observations --output work/geology_export_NEW `
  --epoch 2026-11-24T00:00:00.000Z
```

Launcher берётся из уже установленного QGIS. Несколько выбранных ID передаются после соответствующего
флага. NPZ содержит `xy`, `triangles`, `contacts` (узлы × контакты, Z вверх), `contact_names`, `support_mask`,
`dsm`, `geometry_version`, `variant`, `height_status`. Sections JSON содержит выбранные ключи с `xy`,
`levels` и той же `geometry_version`. GeoJSON содержит `properties.object_id`; экспортируются только
явно выбранные объекты и трассы. История и наблюдения переносятся как исходные записи без повышения статуса.

Выход — новый `geology.gpkg`: узлы, связность, контакты разрезов, объекты и две непространственные таблицы.
Сохраняются единицы, frames, исходный height status, IDs, входные hash и отсутствие координатной операции.
Проверяются SQLite integrity, обратное чтение XYZ, связность, IDs и количество features. Сечения TIN
не объявляются детальными геологическими чертежами и не получают механический допуск.

## Парная проверка

```powershell
python scripts/assess_geology.py --config $config --contacts $contacts --placements $placements `
  --observations $observations --surface $surface --boundary $boundary --objects $objects `
  --frame $frame --object-id $objectId --method idw2 --withdraw-collar $wellId `
  --trace-start $startId --trace-end $endId --output work/geology_assessment_NEW
```

Surface NPZ задаёт исходные `xy`, `z`, `triangles`, а boundary GeoJSON — единственный Polygon/MultiPolygon
с выбранным object_id. Trace endpoints выбираются по ID Point-объектов. Предыдущая JS-сцена не является
входом. Пустой/ошибочный объект, совпадающие концы и отсутствующая отзываемая скважина отвергаются.

Используется общий `ContactPredictor` и общий адаптер `supports_from_borehole_rows`;
старый engineering builder вызывает тот же адаптер. Алгоритм исходных интервалов, отсутствия и мощностей
не изменён. B содержит исходные точки и ограничения отсутствия, C добавляет допустимые дополнительные
интервалы/мощности. Снятие абсолютной опоры сохраняет относительные мощности; UNKNOWN не становится нулём.
Четыре варианта сопоставляются по общим точным ключам fold/group/physical-well/contact;
позиционная классификация фиксируется по B baseline. Это ретроспективная источниковая согласованность,
не независимая полевая валидация и не прогноз. Механический отклик остаётся NOT_RUN.

## История и замещение

Локальные выпуски 23–31 октября сохраняют собственные frozen receipts и источниковые данные в PRIVATE.
Их результаты ансамбля, поверхностные операции и синтетические проверки решателя не становятся новой
геологией всего поля. Текущий дипломный маршрут указан в [состоянии R3](../implementation_work/SKRU1_DIPLOMA_R3_20261124_RU.md).
Прежние локальные PUBLIC brief/continuation удалены после переключения ссылки из PROJECT_STATE.
Историческое полное воспроизведение использует соответствующие старые commits и полный payload;
новые CLI не содержат скрытого fallback на прежние локальные скрипты.

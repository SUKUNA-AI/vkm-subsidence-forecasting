# Native fidelity и полный обход таблиц

Изменения кода от 01.10.2026. Production не пересчитывался. Синтетические проверки не означают,
что реальные источники уже извлечены новым кодом или допущены к научному использованию.

## Сохранение структуры

- DOCX: разбираются основной документ, footnotes/endnotes, comments, headers/footers, textboxes,
  вложенные таблицы и OMML внутри таблиц/примечаний. Вложенная таблица имеет собственный объект;
  её строки и текст не дублируются в родительской сетке. `gridBefore/gridAfter`, `gridSpan`,
  вертикальные слияния и текст их продолжений сохраняются с локаторами фрагментов.
  Заголовок строки берётся из `tblHeader`, а не назначается первой строке автоматически.
- XML каждого разобранного Word part хранится также в base64 исходных байтов; UTF-16 не теряется.
  Вспомогательный XPath содержит имя part. Для служебных частей и textboxes физическая страница
  остаётся неизвестной, пока отдельно не доказана связь с pinned render. Они не сдвигают поиск body paragraphs.
- Удалённый текст исключён из видимого слоя. Fields, altChunk и embedded objects не считаются
  семантически разобранными: для них есть `RAW_ONLY_*` diagnostics и исходная разметка;
  видимый cached text поля может присутствовать в блоке. Package manifest перечисляет также
  неразобранные члены ZIP как `RAW_ONLY`; CRC32 является инвентарным атрибутом, не заменяет SHA-256 источника.
- EPUB сохраняет исходные байты spine item и native MathML с XPath, разметкой и линейным текстом.
  LaTeX не придумывается. Успешный MathML не закрывает отдельную задачу OCR изображения формулы.
- PDF сохраняет `texttrace`: код Unicode, glyph ID, позицию и bbox символа, font/render mode,
  opacity и признак скрытого слоя. Это наблюдение native extractor, а не восстановленная семантика формулы.

OCR HTML normalization `ocr_normalize_v3` сохраняет `raw_grid` и явные dispositions подавленных
пустых хвостовых строк/обрезанных spans. Это кандидаты на OCR loop, требующие review; они не
возвращаются в canonical grid автоматически. Для native EPUB пустые строки сохраняются.
При распознавании bands audit содержит исходную сетку каждой полосы и её нормализованный offset.
Assembler сохраняет audit JSON со статусом `DERIVATION`, правилом и ссылками на входы в artifact
с ролью `STRUCTURAL_DIAGNOSTICS`; исходный OCR response сохраняется независимо.

Контейнерные inventories и raw markup обеспечивают трассировку оставшейся работы. Они не являются
обещанием семантического разбора всех OLE/chart/style объектов или автоматического научного review.

## Локаторы и совместимость

В `figures`, `tables`, `formulas` версии `0.1.1` добавлено nullable поле `raw_locator`.
EPUB XPath теперь сохраняется в канонической строке и проверяется B04 по object ID.
У старых строк это поле остаётся NULL/UNKNOWN; код не восстанавливает его предположением.

Reader допускает только точные прежние `(0.1.0, schema_fingerprint)` для этих трёх datasets;
allowlist находится в `contracts/datasets.py`. Физическая historical schema сверяется с ожидаемой
без нового поля. Для старых строк сохраняется исходный row digest без `raw_locator`; outer
fingerprint использует версию dataset в manifest. Это позволяет читать старые snapshots и
строить смешанные snapshots без переписывания исходных Parquet. Unknown versions/fingerprints
отклоняются. Непустой locator под row version `0.1.0` запрещён: новый producer должен писать `0.1.1`.
Старый reader не обязан понимать новые строки: rollback приложения требует прежнего снимка данных.

Semantic generations native PDF/DOCX/EPUB повышены до 2, prepare signature включает
`native_fidelity_v2`, OCR normalizer имеет отдельную версию. Повторное извлечение запускается только
отдельно разрешённым processing cycle. Code-only serving update не запускает его.

Перед возвратом cached PREPARED выполняется проверка актуальных байтов оригинала: путь внутри
PRIVATE root, существование, отсутствие LFS pointer, SHA-256 из регистра. Изменение тех же bytes
count/mtime не обходит проверку. Отказ не перезаписывает старый cache.

## Table cursor API / MCP

`GET /v1/nav/table/{id}` и `get_table_structured` принимают необязательный `cursor`.
Ответ содержит `pagination.snapshot_id`, `table_version`, `total_cells`, `has_more`, `next_cursor`.
Клиент повторяет запрос с `next_cursor`, пока тот не станет NULL, и объединяет `rows` по их
исходным номерам. Это полный обход cell records, включая sparse row numbers; Markdown остаётся
ограниченным представлением и не является каналом полного экспорта.

Cursor привязан к canonical table ID, NAV snapshot и полному содержимому structure/columns/cells.
Если snapshot или содержимое изменились, API возвращает `INVALID_ARGUMENT`: обход следует начать
заново. Cursor не предоставляет полномочий; каждый запрос снова проходит API authentication и
текущую access policy. Нельзя продолжать старую выдачу после отзыва доступа.

Отдельный unlimited export endpoint не добавлен. Полный машинный экспорт выполняется последовательным
обходом страниц; клиент проверяет сумму полученных cells против `total_cells` и постоянство
`table_version`. На каждой странице хэш таблицы читается потоком, память ограничена; цена CPU/IO
пропорциональна размеру таблицы на запрос. Для очень больших таблиц будущая оптимизация должна
кэшировать fingerprint по immutable generation, сохраняя проверку политики при каждом чтении.

## Проверки

Новые synthetic tests: `test_structural_fidelity.py`, `test_prepare_admission.py`,
`test_locator_schema_compat.py`, `test_table_cursor.py`; продолжение через HTTP/MCP покрыто
`test_api_nav_tables.py`. Проверены nested MathML environments, скрытый PDF text layer,
UTF-16 footnote, nested DOCX/HTML tables, сохранение OCR raw grid, изменённые bytes при прежнем mtime,
старый/mixed Parquet snapshot и отказ при неизвестном fingerprint, неверный/устаревший cursor.

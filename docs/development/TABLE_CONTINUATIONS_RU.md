# Многостраничные таблицы: явные продолжения и точное происхождение

Дата: 04.10.2026. Срез S17/S18, правило `table_continuations_v1`.

Реализована передача явного предложения связи `TableX → CanonMapper → canonical.tables`
и отдельная ограниченная проекция продолжений в существующей выдаче NAV `table_structured`.
Это не завершение извлечения таблиц корпуса и не научная приёмка. Реальные источники,
OCR, переизвлечение, публикация корпуса и production qualification здесь **NOT_RUN**.

## Подтверждённый исходный разрыв

В [TableRow](../../src/vkm_corpus/contracts/models.py) уже существовал
`continues_object_id`: ID фрагмента на следующей странице. Однако
[TableX](../../src/vkm_corpus/extract/model.py) не представлял эту связь,
[CanonMapper.tables](../../src/vkm_corpus/extract/to_canon.py) её не переносил,
а [NAV builder](../../src/vkm_corpus/navigation/tables.py) и
[tables_query](../../src/vkm_corpus/navigation/tables_query.py) выдавали отдельный
физический объект. Внутренние OCR bands одной таблицы не решают межстраничное продолжение.

Прежний ответ сохраняется: его `table_id`, cell IDs, строки и физический cursor не заменяются.
Добавлен отдельный ключ `continuation`. Его отсутствие/недоступность не превращает
физический объект в исчезнувшую таблицу.

## Передача явного предложения

`TableX.continuation_candidate` принимает неизменяемый `TableContinuationCandidate`:

- точный hash исходного файла;
- target page index, исходный locator и hash сохранённой сырой записи target;
- locator объявления связи внутри зарегистрированного исходного артефакта;
- ID этого content-addressed артефакта;
- основание `NATIVE_SOURCE_RELATION` или `EXPLICIT_STRUCTURE_LINK`;
- единственный допустимый статус `AUTO_EXTRACTED_UNREVIEWED`.

`CanonMapper` сначала выделяет все физические IDs прежним алгоритмом и в прежнем
порядке. Затем сопоставляет target по точной тройке `(page_index, raw_locator,
raw_content_sha256)`. Ноль или несколько совпадений, другой файл, другая extraction
generation, пропуск страницы, отсутствующий page ID или несколько predecessors
останавливают преобразование. Число/подпись таблицы не используются для соединения.

Объявление должно ссылаться на raw artifact, зарегистрированный за исходным объектом.
`NATIVE_SOURCE_RELATION` разрешена только для native объекта. Полная typed декларация,
её отдельный hash и выделенный target ID сохраняются в `continuation_provenance`.
Один digest без проверяемого target и locator недостаточен.

Само наличие source-bound декларации **не доказывает**, что связь правильно прочитана
в оригинале. Парсер не создаёт reviewed/scientific admission. Проверка чтения связи
по оригиналу и проверка логического продолжения остаются самостоятельными gates.

`TableX.extraction_signature` имеет точную nullable передачу в envelope таблицы.
Значение предоставляет producing stage; mapper не придумывает его из run ID/config,
не меняет прежние NULL и не переопределяет source/raw hashes. Неверный hash отвергается
до публикации. Без непустой exact signature проекция не создаёт `ObjectRef.object_version`.

## Каноническая проекция

[table_continuations.py](../../src/vkm_corpus/navigation/table_continuations.py)
читает attached canonical snapshot и NAV, не требует нового NAV dataset и не меняет
physical canonical objects. Caller авторизует anchor source **до** вызова; reader
отвергает иной source ID/hash до чтения caption/cells этого фрагмента.

Проекция проверяет:

- одинаковые exact canonical/NAV snapshot ID и исходный manifest hash;
- только явные edges одного source/hash/generation;
- единственную ациклическую цепочку соседних страниц;
- typed declaration hash, зарегистрированный artifact, точный target locator/raw hash;
- точные физические размеры, canonical/NAV cell IDs, тексты и spans;
- отсутствие неоднозначной/небезопасной структуры, truncation и конфликтов units;
- одинаковую ширину и **точную** повторную шапку, включая напечатанные единицы.

Исторические edges без нового proof обозначены `LEGACY_UNREVIEWED`. Неизвестные
или подменённые declarations не получают статус source-declared.

Если следующий явно связанный фрагмент не содержит шапки, выдаются ссылки на шапку
первого фрагмента с `EXPLICIT_CONTINUATION_UNREVIEWED`. Значения и неизвестные единицы
этого фрагмента остаются прежними. Автоматическая ссылка на шапку не является
проверенной семантической интерпретацией.

Каждая cell occurrence содержит original table `ObjectRef`, NAV cell ID,
canonical pointer `/cells/N`, hash точного canonical cell, physical row/column и spans.
Шапки и unit context ссылаются на такие же occurrences; caption unit context имеет
pointer `/caption` и hash поля. Bbox символов и несуществующие XML координаты не создаются.

Повторная шапка сохраняет original location и логическую роль `REPEATED_HEADER`.
Примечания также остаются строками с исходными occurrences; для явного маркера
«Примечание» показывается `PRINTED_NOTE_MARKER` вместе с прежней NAV role.
Пустая запись и текст «0» не объединяются.

Раздельны `physical_row_count`, `physical_cell_count`, `data_row_count` и
`note_occurrence_count`. `data_row_count` основан на **непроверенных** existing NAV
row roles; это не число независимых измерений. Ссылки на примечания в ответе относятся
к текущей странице ответа, общий count — ко всей цепочке.

## Ограничения и cursor

Ответ возвращает ограниченную страницу **логических физических строк**, включая
шапки и примечания. `lt1:` cursor закреплён за exact anchor, generation, chain digest,
original cell order, canonical/NAV содержимым, структурами и interpretations. Изменение
любого закреплённого входа или попытка использовать cursor другой таблицы отклоняется.
Physical cursor прежней выдачи и logical cursor имеют разные области.

`pagination.has_more` и `next_cursor` позволяют обойти все разрешённые строки без
повтора/потери. `pagination.complete=true` допустим только когда весь chain поместился
в начальный ответ. Последняя страница большого обхода не объявляет весь корпус полностью
извлечённым. Неизвестные schemas/identities возвращают `NOT_AVAILABLE`; изменившийся
контракт при использовании cursor — явную ошибку.

Есть независимые limits числа фрагментов, canonical cells/bytes, NAV bytes, физических
grid positions, output cells/bytes и rows/page. Размеры проверяются SQL до materialization.
Строка шириной 257 columns выдаётся целиком, если помещается в бюджет. Если одна строка
не помещается, результат `BLOCKED`, а не усечённые columns с заявленной полнотой.

Неоднозначная цепочка, конфликт шапки/единиц, cycle, пропуск страницы или stale NAV дают
`BLOCKED` и пустую логическую выдачу. Напечатанное «Продолжение таблицы» без объявленного
predecessor или «Продолжение на следующей странице» без successor дают
`BLOCKED_UNDECLARED`. Это видимый долг, не попытка угадать соседнюю таблицу.

## Версии и совместимость

`tables` теперь `0.1.2`; новое поле nullable и добавлено после прежних колонок.
Read support принимает только текущую schema или **точные allowlisted fingerprints**
historical `0.1.0` и `0.1.1`. Из `0.1.0` исключены отсутствовавшие `raw_locator` и proof;
из `0.1.1` — только proof. NULL padding не меняет прежние content hashes, file digests
или mixed snapshot fingerprints. Подмешать новое непустое поле под старой версией нельзя.
Неизвестная schema/fingerprint не признаётся совместимой.

Для использования reader не нужен rebuild старого NAV, массовый re-embedding или
перезапись historical Parquet. Новые extraction declarations создают новое поколение
затронутых таблиц с новым content hash; original bytes/IDs/raw hashes остаются прежними.
Нельзя считать новое proof научным допуском только потому, что hash совпал.

## Проверки и оставшиеся gates

[Synthetic tests](../../tests/corpus/test_table_continuations.py) проходят actual
`TableX → CanonMapper.build → tiny canonical DuckDB → NAV build/pack/publish → NavStore.run`.
Проверяются exact identity, header/note provenance, 257 columns, UNKNOWN, pagination,
no-overwrite, ambiguous target/edge, source/version/unit/header conflicts, stale cursor,
truncation и memory limits. Compatibility проверяется independently computed old content
и file digests в [historical tests](../../tests/corpus/test_locator_schema_compat.py).
Результат synthetic pipeline не является qualification настоящего корпуса.

Следующие работы остаются открыты:

1. **NOT_READY_FOR_AUTO_DISCOVERY:** реальные adapters ещё не выдают такие candidates.
   Нужны source-original relation readers и независимая квалификация пропусков детектора.
   Typed passthrough сам по себе не делает все межстраничные таблицы обнаруженными.
2. Общий mapper provenance всех object types требует отдельной задачи: исторически
   `extraction_signature` часто NULL; таблицы получили лишь безопасный точный passthrough.
   Исходная version identity не восстанавливается догадкой.
3. Независимые решения о правильном чтении cells, headers, notes, units и declaration
   должны войти в canonical review journal и purpose-specific scientific admission.
4. Сложные layouts с несколькими блоками, несовпадающими/неполными шапками, не adjacent
   page gaps или merged continuation остаются явным backlog, не auto join.
5. Полная кампания корпуса, gold, контроль полноты обнаружения и научная приёмка —
   отдельное исполнение S25/S26 после применимых gates.

Все успешные состояния здесь имеют суффикс `UNREVIEWED`,
`scientific_admission=NOT_ESTABLISHED`, `original_read_verification=NOT_RUN`.

# DOCX native grid: сохранение ячеек и проверяемая диагностика

Этот slice закрывает конкретный дефект S17/S18 в native WordprocessingML adapter. Он не означает завершение
DOCUMENT fidelity, OCR, научной проверки или обработки корпуса. Проверки используют только synthetic DOCX.

## Подтверждённый дефект

Предыдущий `_grid` сохранял открытый `vMerge` при отсутствии соответствующей колонки в следующей строке.
Например, `restart` в row0/col0, строка row1 с `gridBefore=1`, затем `continue` в row2/col0 давали одну ячейку
`row=0, row_span=2, text="top\nlater"`. Геометрия этой ячейки покрывала row1, хотя продолжение было в row2;
отдельная ячейка row2 отсутствовала, диагностик не было.

Новый adapter держит merge state только предыдущей физической строки. Пропуск колонки, пустая строка,
обычная ячейка и несовпадающий `gridSpan` закрывают цепочку. Продолжение без непосредственно предшествующего
совместимого merge остаётся отдельной ячейкой и получает диагностику. Номера таблиц, похожие подписи и render
alignment не являются основанием для объединения.

## Реальный путь producer → canon → учёт

1. [`read_docx`](../../src/vkm_corpus/extract/docx.py) извлекает исходную таблицу, её сетку и `grid_audit`
   по правилу `docx-native-grid/2`.
2. [`_prepare_docx`](../../src/vkm_corpus/pipeline/prepare.py) после действительного native чтения сохраняет
   source SHA, rule, child-stage signature и точные версии lxml/libxml2 в NATIVE_RAW и prep summary.
   `producer_signature` артефакта совпадает с этим child signature.
3. [`Assembler`](../../src/vkm_corpus/pipeline/assemble.py) перепроверяет child identity и детерминированный
   audit по точному `raw_output`/locator. Он публикует отдельный content-addressed `STRUCTURAL_DIAGNOSTICS`
   raw artifact, связанный с source SHA, исходным NATIVE_RAW и SHA-256 UTF-8 XML таблицы.
4. Существующий CanonMapper сохраняет эту ссылку и `TABLE_STRUCTURE_UNCERTAIN`; canonical TableCell schema,
   физические IDs и raw XML anchors остаются прежними.
5. [`SourceAccounting`](../../src/vkm_corpus/coverage/accounting.py) присваивает такой native table candidate
   `NEEDS_REVIEW / NATIVE_TABLE_STRUCTURE_UNCERTAIN`, даже когда все projected rows учтены. Отчёт отдельно
   показывает review debt; `ACCOUNTED` не является полной извлечённостью или научным допуском.

`grid_audit` перечисляет все физические `w:tc`, включая пустые и merge fragments: точные part-qualified
locators, исходный текст, объявленные gridSpan/vMerge/hMerge, пропуски колонок, physical header-row declarations,
связь с projected cells и диагностики. Main-part locators сохраняют прежний XPath. В footnotes/endnotes/header/
footer используется `part#XPath`. Исходные XML bytes частей дополнительно сохраняются существующим raw-слоем.

Состояния структурного audit:

- `PARSED_UNREVIEWED`: явная native геометрия разобрана, нет обнаруженных противоречий.
- `NEEDS_REVIEW`: сетка сохранена, обнаружены orphan/mismatched merge, неверная header declaration или
  несовпадение наблюдаемой ширины с `tblGrid`; ничего не исправляется догадкой.
- `STRUCTURE_UNRESOLVED`: malformed/нулевой/отрицательный/непредставимый span или пропуск, overflow либо
  неподдержанный legacy `hMerge`. Canonical `cells=[]`, `n_cols=NULL`; исходный XML, физические cells и текст
  сохраняются. Пустая сетка здесь означает явно не восстановленную структуру.
- `NOT_AVAILABLE`: historical/missing/inconsistent/forged audit или child identity. Legacy данные читаются
  с `TABLE_STRUCTURE_UNCERTAIN`; они не получают current contract только из наличия старых значений.

Число физических исходных cells и число projected cells различаются при вертикальном объединении. Ни один
merge fragment не исчезает из audit. `tblHeader` описывает исходную Word row declaration; это не доказательство
правильного научного значения заголовка, единицы или роли данных. Полная семантика шапок и binding значений
остаётся отдельной квалификацией S18.

Геометрия ограничена явно: 16 384 колонки и 1 000 000 grid positions. Лимит применяется также без `tblGrid`,
к `gridSpan`, `gridBefore`, `gridAfter`, суммарной ширине и sparse grid area. Превышение не обрезается:
`STRUCTURE_UNRESOLVED`, `cells=[]`, `n_cols=NULL`, exact raw declarations/physical counts и review debt сохранены.
Сетка 257 колонок поддержана regression; small XML с огромным span/vMerge не попадает в downstream range loops.

## Cache и совместимость

Aggregate PREPARE signatures сохранены. Для DOCX формат определяется admitted bytes,
а не расширением: reuse требует current child identity одновременно в prep и native artifact.
[`load_state`](../../src/vkm_corpus/pipeline/commit.py) отклоняет устаревшую DOCX summary; Assembler дополнительно
перепроверяет identity raw artifact и его audit. Actual planner отклоняет stale DOCX prep; commit signature
для DOCX связывает current child signature и native artifact. Non-DOCX PREPARE/commit и OCR/model call signatures
этим правилом не изменены. Устаревшая DOCX child identity запрещает reuse и требует нового native чтения,
даже при прежнем aggregate staging PREPARE key и нестандартном имени. Staging cache не является frozen publication.

В production `fresh_source_identity` возвращает формат из фактически проверенного файла, определённый тем же
byte-signature detector, который использует native prepare. `load_state` и `commit_signature` независимо требуют
fresh source SHA/size и совпадения формата с cached summary. Поэтому DOCX под именем `.bin` нельзя вывести из
child gate, заменив `inspect.file_format` на PDF/EPUB/UNKNOWN или удалив `inspect`. Planner использует уже
полученную в этом вызове fresh identity; дополнительного полного хеширования для каждой проверки кэша нет.
Параметр внутреннего переиспользования identity не загружается из prep, manifest или сохранённого плана.
Missing/drifted originals, LFS pointers и ошибки чтения/ZIP inspection закрывают production reuse. Успешный кеш
для UNKNOWN, обычного ZIP или IMAGE не допускается: это не поддержанные native document formats. Формат в fresh
identity закономерно изменяет plan SHA, но не сигнатуры стадий. Exploratory diagnostic fixtures сохраняют прежнее поведение;
это не обход production gate и не подтверждение актуальности оригинала. В PREPARE/commit/OCR payload keys для
не-DOCX ничего не добавлено: изменился admission predicate, а не глобальная версия pipeline или модельного кэша.

Original bytes, raw XML/его SHA и ID anchors не изменяются. Новые diagnostics/metadata закономерно меняют SHA
NATIVE_RAW и content SHA новых canonical rows. Исторические артефакты/rows/releases не переписываются; дальнейшая
публикация идёт существующим versioned publisher. Семантические producer generations и IDs здесь не повышены.
Структура корректируется только при будущей явно запущенной обработке затронутых DOCX; никакого corpus rebuild
или глобального re-embedding эта реализация не выполняет.

Child grid signature включает source SHA, pipeline/extractor version, generation, rule и parser libraries. Его
scope — native grid child stage. Он не является полной `TableX.extraction_signature`, scientific object identity,
loaded-model witness или admission. `TableX.extraction_signature` не заполняется этим digest автоматически.

## Проверка и оставшиеся границы

[`test_docx_native_grid.py`](../../tests/corpus/test_docx_native_grid.py) проверяет реальный native producer,
Assembler, CanonMapper, canonical Arrow/DuckDB и coverage; gap/replacement/empty rows, span mismatch/overflow,
malformed geometry, 257 колонок, пустые ячейки, part-qualified locators, изменённые units в разных таблицах,
старые IDs/raw hashes, forged audit/signature и stale cached DOCX под `.docx` и `.bin`. Render metadata в consumer
fixture явно synthetic; actual `_prepare_docx` без renderer сохраняет `RENDER_FAILED`, а не runtime PASS.

NOT_RUN: реальные source originals/corpus campaign, DOCX rendering, OCR, runtime publication и независимый review.
Не завершены: native формулы с точным occurrence locator и локальными symbol bindings, общая object-wide
extraction identity, полный source-backed header/cell interpretation, automatic continuation discovery,
claims/entities/observations и научный допуск. Legacy `hMerge` учтён и сохраняется, но пока не проецируется.

Во всех ветках document review остаётся `AUTO_EXTRACTED_UNREVIEWED`, scientific admission — `NOT_ESTABLISHED`.

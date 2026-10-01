# R03 — учёт потерь в действительном extraction pipeline

Реализованы hooks в `prepare`, `assemble`, OCR runner и source commit, а не только
отдельная модель CoverageLedger. Проверки используют синтетические PDF/EPUB,
in-memory/native metadata, fake layout и `httpx.MockTransport`. Реальный корпус,
GPU и OCR server в этой работе не запускались. Корпусная квалификация: **NOT_RUN**.

## Что фиксируется

`src/vkm_corpus/coverage/accounting.py` ведёт учёт **одного источника и его SHA-256**.
Ожидаемое количество страниц берётся из pagination, независимо от числа
сформированных outputs. Сохраняются basis, unit, disagreement второго счётчика,
отсутствующие, лишние и повторные индексы. Отсутствующий denominator остаётся UNKNOWN;
пустой список выходов не доказывает отсутствие страниц.

DOCX-объекты имеют отдельные `NATIVE_OBJECT` units с точными XML paths. Неизвестная
привязка к rendered page не становится page 1 или придуманным bbox. Даже при
отсутствии pinned DOCX render известные native объекты остаются учтёнными, а
ожидаемая пагинация — UNKNOWN / INCOMPLETE.

До сборки фиксируются кандидаты:

- native text blocks / DjVu lines, EPUB blocks/tables/MathML/images;
- DOCX paragraphs/tables/OMML/images, включая пустые абзацы;
- **все сохранённые LAYOUT_RAW detections до фильтрации**;
- явный full-page OCR fallback, если его создаёт существующий planner.

Кандидат содержит source version, unit, raw artifact ID, исходный locator и SHA-256
соответствующей структуры. В отчёт не копируются строки источника или распознанный
текст. Для native таблиц дополнительно сохраняются объявленные размеры, количество
ячеек и hash проекции `(row,col,row_span,col_span,is_header,text)`. Изменение этой
проекции в canonical output становится `NATIVE_TABLE_CELL_PROJECTION_CHANGED`.
Полные исходные XML/HTML/строки продолжают храниться в частном NATIVE_RAW.

`regions_from_raw(..., trace=...)` сохраняет прежний результат отбора и дополнительно
возвращает решение для каждого raw detection:

| Ветка | Причина |
|---|---|
| Более слабый класс той же query | `BEST_QUERY_CLASS` + точный другой candidate |
| Ниже порога confidence | `CONFIDENCE_THRESHOLD`, score, threshold |
| Маленький bbox после clipping | `CLIPPED_BOX_TOO_SMALL` |
| NMS overlap | `NMS_OVERLAP`, IoU, точный другой candidate |
| Отключённые inline formula / неподдерживаемая метка | отдельная причина, если output отсутствует |
| Вложенный текст, исключённый OCR planner | `NESTED_TEXT_OCR_SUPPRESSED`, если нет native output |

Исходные detections **до** model top-k/raw-floor исторически не сохранялись.
Новый отчёт честно указывает `pre_raw_suppression=NOT_ENUMERATED`, параметры top-k
и raw_floor и область `STORED_RAW_DETECTIONS_ONLY`. Он не выдумывает их IDs и не
называет число сохранённых detections полнотой детектора.

## Попытки OCR и остановки

Рядом со staging root публикуются неизменяемые события:

```text
accounting/events/<source_id>/<source_sha256>/<event_sha256>.json
```

Событие PLANNED создаётся до crop/model branch. Начало работы, сохранённый raw
response, cache reuse, ошибка crop/worker, размерный guard, отсутствие crop/band,
лимит вызовов/времени и quality stop получают отдельные события. Input image hash,
task/config identity, band index/count, номер попытки, raw output hashes и code
revision сохраняются. Текст ответов и сообщения с возможными исходными значениями
в события не переносятся; для crop error сохраняется hash сообщения и код причины.

Пропущенные задания не превращаются в успешные попытки. Начатое задание без
терминального события остаётся INTERRUPTED; запланированное без запуска — NOT_RUN.
В CoverageLedger используется терминальное состояние каждой наблюдаемой попытки,
а ссылки на **все** исходные события сохраняются отдельно. Token limit и quality
flags остаются явными даже при HTTP success и наличии частичного текста.

Исторические cache entries без source SHA нельзя задним числом назвать доказанным
полным журналом попыток данной версии. Их количество показывается как
`historical_unpinned_cache_attempts`; область гарантии —
`SOURCE_SHA_PINNED_EVENTS_AND_ASSEMBLY_STEPS`. Повторное использование выходов
проверяется существующими pixel/config signatures; provenance review этим не заменяется.

## Сопоставление с выходами

После настоящего `CanonMapper.build()` проверяются counts, уникальность object IDs,
source ID/SHA и raw locator. Кандидат связывается с выходом по **точному** raw artifact
и locator. Допустимые many-to-one соответствия DjVu lines → block фиксирует сама
assembly-ветка. Native block → layout region и full-page task → OCR output также
получают явную связь от producer; близость bbox сама по себе связью не считается.

Отсутствующий выход — NEEDS_REVIEW с причиной. Формула только в изображении или
пустой формульный output не становится EXTRACTED. Truncated/repetitive/empty-on-ink
outputs сохраняют IDs/hash, но имеют NEEDS_REVIEW. Output без обнаруженного
кандидата — отдельный gap, а не созданный задним числом «успешный кандидат».

Часть native PDF image metadata пока не имеет однозначного mapping в semantic
Figure objects: `NATIVE_IMAGE_MAPPING_UNSUPPORTED`. PDF vector primitives сохраняются
отдельным диагностическим каналом, а не приравниваются к semantic figures.
Эти ограничения видимы в отчёте, raw artifacts остаются сохранены.

## Порядок durable publication

1. Собрать accounting на одной source generation и проверить raw artifact bytes.
2. Записать content-addressed `accounting/reports/<sha>.json` с fsync и проверкой hash.
   Этот файл сам по себе **не является completed receipt**.
3. Выполнить существующий source commit под lease.
4. Прочитать именно записанный commit marker; проверить его content-derived ID,
   source SHA, хеши четырёх object Parquet partitions, row counts и все
   `(object_id,source_id,source_sha256,content_sha256)` небольшими Arrow batches.
   Сравнить их с outputs отчёта.
5. Записать immutable `accounting/commits/<sha>.json`, связывающий commit и report.
6. Только затем добавить запись в существующий commit cache ledger и вернуть
   успешный результат producer.

При сбое между commit и пунктом 5 новая completed запись не появляется. Повтор
может использовать уже записанный canonical commit как no-op, но заново проверяет
accounting и публикует недостающий binding. Если старый cache ledger указывает на
другой HEAD, содержит отсутствующий/изменённый binding или report, `load_ledger`
выставляет `MISSING_OR_INVALID` и сбрасывает пригодность старого commit signature
для UP_TO_DATE. Опция failed-only также не скрывает такой источник как COMPLETE.

Проверка следующего plan читает hashes accounting и commit metadata. Повторное
хеширование всех Parquet bytes выполняется при binding, а не при каждом обычном
plan. Контроль сохранности serving snapshot после публикации остаётся обязанностью
существующего generation/manifest механизма. Accounting sidecars должны включаться
в передачу staging generation и backup; один старый document COMPLETE их не заменяет.

Отчёты и события расположены в частном runtime, не в PUBLIC. В обычный producer
receipt входит только ссылка/hash и accounting state. Windows directory durability
обозначена `NOT_QUALIFIED`; физический power-loss restore drill не имитируется
успешными unit tests и остаётся `physical_durability=NOT_QUALIFIED`.

## Значение статусов

- **ACCOUNTED**: известные units/candidates получили явный учёт и dispositions;
  это может включать FAILED, SUPPRESSED, UNSUPPORTED и NEEDS_REVIEW.
- **INCOMPLETE**: denominator неизвестен/противоречив, есть пропущенные units,
  неверное соответствие outputs или недоступен обязательный accounting channel.
- **MISSING_OR_INVALID**: окончательный commit binding отсутствует или не проходит проверку.
- `extraction_completeness`, `detector_recall`, `scientific_admission`:
  **NOT_ESTABLISHED**. `candidate_review_debt` показывает известный оставшийся долг.

Ни один из этих результатов не утверждает «100% извлечено», семантическую верность
таблицы/формулы или научный допуск. Старый document status сохраняет своё значение;
новый accounting contract проверяется отдельно. Для нового source COMPLETE
report обязан быть структурно ACCOUNTED и окончательный binding — сохранён и проверен.

## Проверка и оставшиеся gates

```text
python -m pytest -q -p no:cacheprovider tests/corpus/test_pipeline_accounting.py
```

Синтетические tests упражняют реальные prepare/assembler/mapper/commit hooks,
проверяют dropped detections, denominator, failed page, native XML, потерянные
crop/band, token/size/budget failures, tampered Parquet, missing receipt и retry
после сбоя между canonical commit и accounting ACK. Модели в тестах подменены,
сетевой OCR и GPU не используются.

Доставка, receiving gate и backup closure реализованы отдельным
[publication contract](ACCOUNTING_PUBLICATION_RU.md). До production остаются:
qualification на разрешённом реальном наборе; согласование политик/ACL и retention
частных accounting sidecars; ввод updated publication/backup tools и approved
campaign на реальных узлах; проверка восстановления на целевой файловой системе.
Исторические outputs не получают новый verified report автоматически:
старые cached артефакты можно переучесть CPU-only, но отсутствие сохранённой истории
попыток или pre-raw detections должно остаться явно указанным.

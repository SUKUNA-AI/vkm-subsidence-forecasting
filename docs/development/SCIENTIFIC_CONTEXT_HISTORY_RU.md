# Контекст научного применения и исторические чтения

`ScientificUseAdmission.use_context` связывает допуск с типом применения `use`,
площадкой `site: Scope` и масштабом `scale: Scale`. Текст `purpose` остаётся пояснением
и не задаёт эти оси. `UNSTATED` не является допустимым контекстом. Старые записи v1
без `use_context` читаются с прежним canonical SHA, но получают
`SCIENTIFIC_USE_CONTEXT_MISSING` и `NOT_READY` при проверке применения.

Допуск относится к объявленному использованию данных. Он не повышает эпистемический
статус источника, не доказывает independent validation и не разрешает запуск решателя
или обучение. Ответ содержит `admission_scope=DECLARED_DATA_USE_ONLY` и
`field_validation=NOT_ESTABLISHED` даже при READY.

Target-kind matrix закрыта: IDENTITY принимает только resolved Entity или явное
неоднозначностью не помеченное EntityResolutionDecision. Численные применения
принимают квалифицированное Observation с quantity либо VERIFIED ObservationSet
с непустым набором точных pins на такие Observation; каждый child проходит полные
проверки. Mention/Claim/formula text сами по себе не являются численным входом.
Supporting claims могут оставаться текстовыми зависимостями, проходящими собственные
provenance/review gates. Текущие EvidenceRecord не содержат typed WorldSpec geometry,
поэтому GEOMETRY_INPUT закрыт до отдельного interpreted geometry contract. Координаты
из строк и scalar quantities не придумываются.

## Перенос масштаба или площадки

Применение quantity/claim/event использует существующий
`vkm_world.core.provenance.transfer_use_errors`; поведение WorldSpec не изменено.
Исходные scope/scale/status сохраняются. Перенос требует всех четырёх осей,
метода, обоснования и явного DERIVATION/ANALOGUE/MODEL_CHOICE/ENGINEERING_ASSUMPTION.
`FACT` не является статусом переноса.

В evidence добавлен `EVIDENCE_TRANSFER`:

- `target` — точный `VersionRef` исходной записи;
- `transfer` — структурированный WorldSpec Transfer;
- original supports, availability и отдельный authorized semantic review.

В `ScientificUseContext.transfers` каждая привязка содержит точный target,
`transfer_record: VersionRef` и SHA canonical bytes самого Transfer. Его содержимое
должно совпадать с transfer в provenance исходной записи. Transfer-запись и её target
входят в dependency closure и проверку доступа; correction, revocation или иной hash
закрывают допуск. Неиспользованные, чужие и повторные bindings не принимаются.

LAB → MASSIF без этого контракта закрыт. Неизвестные единицы/значения, непроверенные
первичные origins и отсутствующие semantic reviews также остаются blockers.
`VALIDATION_OBSERVATION` требует исходного OBSERVATION с FACT, MEASURED или
FIELD_OBSERVATION, явной ролью VALIDATION и совпадающим исходным site/scale.
Перенос и модельный результат не превращаются в независимое полевое наблюдение.
Для formula interpretation SOURCE_INTERPRETATION допускает чтение в контексте
источника; применение формулы как модели требует отдельного обоснования применимости.

## Две временные оси и точная версия журнала

```python
historical = reader.at_revision(
    journal_revision=exact_retained_commit_sha256,
    as_of="2020-12-31",
    recorded_at="2026-10-01T00:00:00Z",
)
page = historical.page(access_context, limit=100)
```

Все три selectors обязательны для исторического чтения. Revision должен быть
предком текущего HEAD этого журнала; произвольный orphan/foreign commit не принимается.
`recorded_at` — timezone-aware timestamp записи, а не выдуманное время server commit.
Точный journal revision отдельно фиксирует transaction order. Из retained versions
выбирается последняя в порядке журнала запись, удовлетворяющая обоим cutoffs:
`recorded_at ≤ cutoff` и консервативной availability `≤ as_of`.
Неизвестная/конфликтующая availability не становится датой регистрации.
Exact dependency pins обязаны совпадать с выбранными историческими версиями;
зависимые записи с будущими/отсутствующими pins не выдаются.

Историческое чтение не возвращает исторические разрешения доступа. Текущие
source policies и более строгие текущие record policies применяются к старым
версиям и их dependency closure. Изменившаяся policy закрывает старые cursors.
Новая current revision также инвалидирует cursors консервативно. В каждой выдаче
есть `view_context` и `view_context_sha256`; hash связывает точную revision,
оба cutoff и текущие policy identities. Для admission отдельно выдаются
`historical_admission` и `current_admission`, которые не подменяют друг друга.

API `/v1/evidence`, record, dependencies и review-packet поддерживают optional
`journal_revision`, `as_of`, `recorded_at`. Те же параметры добавлены к четырём
существующим evidence MCP tools; набор 49 READ tools сохранён. Без selectors
действует прежний current view. Старый `list_evidence(as_of=...)` фильтрует current
versions по availability; исторические версии выбираются только явными selectors.
Cursor связан также с principal и query filters и не переносится между контекстами.

Review packet связывает исторический view с текущей policy внутри своего SHA.
Для original supports требуется точный canonical snapshot; если он отсутствует,
packet закрывается, а ссылки не перенаправляются на текущую extraction.

## Границы проверки

Контракты и negative tests используют синтетические данные и временные журналы.
Реальные scientific admissions, corpus migration, original-source expert review
и qualification production исторических snapshots не выполнялись. Наличие JSON
schema, journal revision или READY для synthetic fixture не закрывает эти gates.

Узкая квалификация 01.10.2026: совместный набор R04/R05, quantity admission,
evidence API/review, runner/schema и MCP — 275 PASS на Linux и 275 PASS на Windows,
без skips. Воспроизведённый Windows WinError 10053 на unauthorized POST исправлен
в HTTP middleware, после чего real-loopback MCP прошёл без исключения.
Полный workflow новых байтов ещё не выполнялся. Использованы свежие отдельные
bytecode namespaces, существующие `.pyc` не удалялись и не читались.

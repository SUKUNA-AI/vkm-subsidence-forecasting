# Пакет проверки по оригиналу

`vkm_evidence.review_packet.build_review_packet(reader, canon, record_id, context)`
готовит материал для проверки точной версии evidence. Создание пакета **не является**
проверкой оригинала, `SEMANTIC_REVIEWED` или scientific admission.

Вызов требует `EvidenceReader.source_policy`. Доступ к записи, всем родительским
references/dependencies, источникам и experimental roles проверяется до чтения
canonical-объектов. Отказ и отсутствие записи снаружи не различаются. `TARGET` и
`TEST_SEALED` требуют соответствующего контекста даже при разрешённом cloud access.

Пакет содержит:

- Полную evidence-запись, её SHA-256 и generation журнала.
- Полный транзитивный список exact VersionRef зависимостей, включая associations,
  специальные ссылки review/admission и supports определений символов формул.
- Все оригинальные ObjectRef, source/content hashes, snapshot/manifest, проверенные
  native/raw/page locators и результат проверки явно заданного текстового фрагмента.
- Ограниченные previews текста и canonical table cells, исходные единицы и контекст
  evidence, bbox с его исходной системой координат и предупреждения.
- Ссылки на существующие API objects/images и content-addressed image artifacts.
  Наличие и байты изображений при создании пакета не проверяются и не рендерятся.

Native DOCX XML path не превращается в номер физической страницы или искусственный
bbox. Для blocks фрагмент считается по `text`, для page — по `normalized_text`,
для table/formula — по `raw_output`, для figure — по `caption`. Поле явно названо
в пакете; соседний normalized или native field не подставляется автоматически.
Проверка canonical extraction не заменяет визуальную сверку PDF/скана или исходного
XML. Старый snapshot не перенаправляется на current. Отсутствие исходного объекта,
ошибочный locator/hash/span, stale dependency, смена snapshot, политики или generation
останавливают построение.

## Ограничения и полные таблицы

По умолчанию: 64 уникальных supports, 512 dependency edges, 12 000 символов на
preview, 100 canonical cells и 2 000 000 байт итогового JSON. Лимиты можно явно
уменьшить/увеличить в пределах жёстких верхних границ функции. Полная запись,
supports и dependency refs не усекаются: превышение лимита — `ReviewPacketLimit`.
Только previews могут быть сокращены, с `truncated`, исходным числом символов или
ячеек и предупреждением. Один canonical object читается за раз; большие rows сами
по себе требуют памяти в пределах существующего `CanonStore.row`.

Для таблиц пакет даёт стартовую ссылку `/v1/nav/table/{id}`. Клиент передаёт
`pagination.next_cursor` в параметр `cursor` и продолжает до
`pagination.has_more=false`; проверяет snapshot и стабильную `table_version`.
NAV grid — производное представление и может отсутствовать. Его полнота не доказывает
полноту исходной таблицы. Нельзя считать preview полной таблицей или подменять
сверку оригинала выдачей NAV. Ссылки на current object/image endpoints требуют
повторной policy-проверки и проверки snapshot в ответе; у image API это
`X-VKM-Snapshot`. Для artifact content дополнительно сверяется SHA-256 байтов.

## Связь с решением reviewer

`packet_sha256` — SHA-256 канонического JSON всего пакета без самого верхнего поля
`packet_sha256`. Контекст доступа, политики, лимиты и previews входят в hash.
В `decision_binding` находятся точная target VersionRef и полные original supports.
Reviewer сохраняет пакет и включает `review-packet-sha256:<hash>` в rationale/checks
решения; API может закреплять этот hash отдельным полем контракта решения.

Перед подтверждением reviewer должен действительно открыть оригиналы, проверить
нужные фрагменты, единицы, scope, временную доступность и существенные зависимости.
Усечённые previews нужно дополнить полной выдачей/оригиналом. Значения
`source_verified` и `SEMANTIC_REVIEWED` устанавливает отдельное авторизованное
решение, никогда функция построения пакета. При изменении evidence/dependencies,
source policy или canonical identity нужен новый пакет и новая проверка.

HTTP-интеграция: `GET /v1/evidence/review-packet/{record_id}` с текущим Read/access
контекстом. Функция не создаёт HTTP-сервис, не записывает журнал и не запускает
извлечение, OCR, модели или production processing.

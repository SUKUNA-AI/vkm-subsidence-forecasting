# Источники: соответствие legacy-ID и реестра PRIVATE

`legacy_source_id_map.csv` сопоставляет ID старого PUBLIC-стека с каноническими ID PRIVATE-реестра
`00_registry/SOURCE_REGISTER.csv`. Старые ID: `SRC01`–`SRC11` из `configs/source_manifest.csv` и `SUP01` из
`configs/supplementary_source_manifest.csv`, оба на legacy-commit `d54025d`. Новые ID — `VKM-SRC-xxx`.
Сопоставление выполнено по точному совпадению SHA-256 и размера файла. Путей PRIVATE в таблице нет.

| Поле | Значение |
|---|---|
| `legacy_source_id`, `legacy_manifest` | старый ID и manifest, где он объявлен (`<путь>@<commit>`) |
| `legacy_file_name` | имя файла в старом `inputs/sources/**` (retired-дерево; байты живут в PRIVATE) |
| `legacy_authority_class`, `legacy_site_scope` | классификация старого стека (историческая; канон — реестр PRIVATE) |
| `vkm_src_id`, `sha256`, `size_bytes` | канонический ID и идентичность файла |
| `register_source_class`, `register_evidence_scope` | классификация из реестра PRIVATE на момент сборки |
| `match_method` | `sha256_exact+size` |

Текущий вход — `source_identifier_aliases.csv`, проекция PRIVATE
`SOURCES/source_identifier_aliases.csv`. Она сохраняет исходные ID, имена, классы,
область, SHA-256/размер и исторические manifest/commit как provenance.
Второй вход — актуальный PRIVATE `00_registry/SOURCE_REGISTER.csv`.
Сам исторический Git commit для пересборки не открывается.

Пересборка и сверка:

```bash
VKM_RESOURCES_ROOT=<private checkout> python scripts/build_monitoring_evidence.py sources [--check]
```

`scripts/verify_canonical_repository.py` при заданном `VKM_RESOURCES_ROOT` сверяет каждую строку
(`vkm_src_id` + `sha256`) с реестром PRIVATE (проверка `private:public_catalogues`).

## Индекс записей сплошного чтения и их маршрутизация

| Файл | Что |
|---|---|
| `record_index.csv` | 13 572 записи сплошного чтения: `vn_id` → источник, чанк, строка `records.jsonl`, страница, вид, SHA-1 записи. Закрепляет позиционные `vn_id` (находка COVERAGE_DUPLICATES-018). Строится `build_public_catalogues.py` из PRIVATE `canonical/SOURCES/vn_index.csv` |
| `record_routing.csv` | для каждой записи — PUBLIC-каталоги, которые цитируют её `vn_id` (находка COVERAGE_DUPLICATES-016, механическая часть). Строится `scripts/build_record_routing.py` из PUBLIC-каталогов; тест `test_record_routing_index_is_current` сверяет файл со сборкой |
| `lineage_affected_records.csv` | записи, затронутые линиями перепечаток и изданий: какая версия эталонная, какая повторяет её с искажением, считать ли её независимой |
| `foreign_page_references.csv` | страницы одного источника, на которых напечатан список литературы другой работы |

`routing` в `record_routing.csv`:

- `DOMAIN` — запись цитирует хотя бы один предметный каталог (всё, кроме `evidence/qa/`);
- `QA_ONLY` — только таблицы контроля качества: журнал OCR-QA, поправки, журналы исправлений;
- `UNROUTED` — ни один PUBLIC-каталог записи не цитирует. Сама запись с цитатой остаётся в PRIVATE `sweep_raw`.
  Причина не классифицирована (дубль под другим id, вне области потока, не перенесено). Для этого нужно прочитать
  каждую запись — это задача Phase 2.

Состояние на 26.09.2026: DOMAIN 10 777, QA_ONLY 1 608, UNROUTED 1 187. Записи типизированных видов
(скважины, формулы, геометрия, хронология, стратиграфия, наблюдения) попали в каталоги все. Остаток приходится
на свободные виды:

- `claim`: UNROUTED 731, QA_ONLY 504;
- `parameter`: UNROUTED 261, QA_ONLY 250;
- `open_question`: UNROUTED 82;
- `conflict_note`: UNROUTED 58;
- `process_mechanism`: UNROUTED 44;
- `hydro`: UNROUTED 10.

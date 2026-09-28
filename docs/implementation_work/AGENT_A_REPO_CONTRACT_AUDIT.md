# AGENT A — аудит репозитория и контрактов (VKM Corpus Platform v0)

Дата: 28.09.2026. Роль: REPOSITORY & CONTRACT AUDITOR, Phase 0, только чтение.

- PUBLIC: ветка `claude/corpus-platform-v0-2026-09-28` = `main` `f461fb6`. В рабочем дереве, кроме `main`, только
  неотслеживаемые `docs/implementation_work/` и `.mcp.json.example`.
- PRIVATE: `main` `c575a07`, рабочее дерево чистое. Тегов нет ни в одном клоне.
- Legacy: `origin/legacy` = `d54025d`, читался только через git-объекты (`git ls-tree`, `git show`).

Что сделано:

- прочитаны документы и код обоих репозиториев и четыре документа теории v0.2;
- запущены только read-only проверки: `pytest tests/world`, `scripts/verify_canonical_repository.py` с PRIVATE,
  `scripts/build_evidence_from_legacy.py {sources,monitoring} --check`;
- хеши, сигнатуры и внутренняя структура PDF/DjVu/EPUB/DOCX/ZIP проверены из Python в памяти: `zipfile`, разбор IFF
  DjVu, без распаковки на диск и без декодеров.

Ничего не устанавливалось и не коммитилось. Байт-код не писался (`PYTHONDONTWRITEBYTECODE=1`). Единственная запись —
этот файл.

Обозначения путей: `<PUBLIC>` — корень PUBLIC, `$VKM_RESOURCES_ROOT` — корень PRIVATE, `$VKM_DATA_ROOT` — будущий корень
runtime-данных платформы.

## 0. Итог в 10 пунктах

1. **Базовая линия на WORKSTATION.**
   - `tests/world`: 209 тестов, 206 PASS, 3 FAIL, 4,1 с. Все три падения — от окружения Windows, не от кода:
     - нет права на symlink (WinError 1314);
     - `Path.write_text` на Windows пишет CRLF, а тест хеширует LF;
     - глобальный `core.autocrlf=true`. Третий тест проходит, если переопределить `core.autocrlf=false`.
   - Верификатор: `PASS_WITH_NONBLOCKING`, exit 0, 42 проверки: 35 PASS и 7 `SKIPPED_REF_UNAVAILABLE` (тегов в клоне нет).
     SHA-256 совпал у 249 файлов из 249, ещё 2 отсутствуют по статусу реестра.
   - Сборщики в режиме `--check`: OK.
2. **Окружения по lock нет.**
   - Системный Python 3.13.13: pydantic 2.13.0 (lock: 2.13.5), numpy 2.4.4 (ниже нижней границы 2.5.3 в
     `pyproject.toml`), pytest 9.0.3.
   - `vkm_world` не установлен: тесты видят его только через `pythonpath=src`.
   - `.venv` в корне PUBLIC — устаревшее legacy-окружение без pydantic.
   - Нет pyarrow, duckdb, polars, PyMuPDF, pypdf, DjVuLibre, tesseract, LibreOffice, pandoc, poppler `pdftoppm`.
     `pdftotext` есть только из Git for Windows.
3. **SOURCE_REGISTER.**
   - 251 строка, `VKM-SRC-001…251` подряд. id, пути и sha256 уникальны; байтовых дублей нет.
   - На диске 249 файлов, 2 049 518 735 байт.
   - Нет двух ZIP, и это решение реестра:
     - `VKM-SRC-013` в PRIVATE никогда не коммитился: архив страниц удалён после склейки в `VKM-SRC-025`;
     - `VKM-SRC-022` снят с дерева в `b1b63f9` (LEGACY_RETIRED). Его LFS-объект ещё лежит в локальном
       `.git/lfs`, и внутри — байтовая копия `VKM-SRC-023`.
4. **Словари «грязные».**
   - `evidence_scope` имеет 18 сырых значений: `VKM_regional`/`VKM_REGIONAL`, `METHOD_GENERAL`/`GENERAL_METHOD`,
     `other_potash_deposit`/`OTHER_POTASH_SITE`, многозначные `VKM_REGIONAL_and_SKRU1`, `SKRU1_SKRU2_SKRU3`,
     `SALT_DEPOSITS_USSR_incl_VKM`, а также `LEGACY_RETIRED` — это не область, а жизненный цикл.
   - `source_class` имеет 27 значений и смешивает жанр работы с природой копии (`derived_convenience_pdf`,
     `front_matter`, `book_archive`).
   - В проекте минимум пять разных словарей «области».
   - Нормализовать реестр на месте нельзя: сырые значения зашиты в PUBLIC-каталоги, и проверка
     `build_evidence_from_legacy.py sources --check` сравнивает их с живым реестром.
5. **DjVu.**
   - Из 9 DjVu титул проверен только у `VKM-SRC-037`.
   - Кроме двух файлов из §19 (`VKM-SRC-245`, `-248`), статус `FILENAME_PAGECOUNT_UNVERIFIED` ещё у шести:
     053, 054, 221, 229, 230, 240.
   - У `VKM-SRC-053` в реестре «423 pages», а страниц-компонентов в файле 384. 423 = 384 страницы + 39 разделяемых
     компонентов `DJVI`.
   - Текстовый слой есть у 053, 229, 230 и 248; нет у 037, 054, 221, 240 и 245.
6. **Локатор страницы в evidence vNext.**
   - Физический индекс `pdf_page`: целое, с 1, есть во всех 13 596 сырых записях.
   - Печатный номер `printed_page` — свободная строка вида «N», «N-N», «N-N (spread)», …; в 406 записях пуст.
   - Тексты страниц Phase 1 назывались `<SID>/p0001.txt`.
   - Значит, `page_id = VKM-SRC-NNN:pNNNN` даёт бесплатный crosswalk со всеми записями evidence.
   - Исключения:
     - 037 — DjVu-развороты «2 печатные страницы на физическую»;
     - 023 — DOCX: страницы рендера LibreOffice 24.2.7.2, 115 стр.;
     - 249 — EPUB без пагинации.
7. **Work ≠ Source нетривиально.** Встречаются:
   - одно произведение в нескольких копиях (013/025/202; 147/232);
   - части одной книги (208/209) и тома (229/230);
   - автореферат ≠ диссертация (001/196);
   - контейнеры-сборники и выпуски (089, 193, 203, 204, 205–207);
   - общие страницы соседних статей «Горного эха» (005/031/032, 034/199);
   - издания (011 — 2-е изд.).
   Группы можно строить только по явным признакам. Часть связей «contains» в notes противоречива.
8. **Текст и OCR корпуса.**
   - Готовый OCR есть только для 025 и 037 (tesseract 5.3.4, в `00_registry/cloud_checkpoint_2026-09-26/`).
   - Постраничный текстовый слой 001–041 в git есть только в виде хешей (`corpus_text_manifest.csv`, 3473 файла).
     PRIVATE `work/` на этой машине нет.
   - Всё это годится только как база сравнения и регрессии (в том числе `quote_check_v2`). Каноническим входом это
     быть не может.
9. **Контракты, которые платформу реально «кусают».**
   - Страж утечки запрещает ключи `quote`, `verbatim_quote`, `ocr_text`, `page_text`, `full_text` на любой глубине
     JSON, в том числе в `properties` и `$defs` JSON Schema. Отсюда: pydantic-модель нельзя называть `Quote`.
   - Запрещён литерал `work/ocr/` в коде и данных.
   - Запрещены расширения `.pdf .djvu .docx .doc .zip .7z .rar .xlsx .xls .tif .tiff`. `.epub`, `.parquet` и
     `.duckdb` страж не ловит.
   - Верификатор проверяет host-пути `/home/…`, `/mnt/…`, `X:/…` в `.py/.yaml/.yml/.json/.toml`, включая
     неотслеживаемые файлы. `/srv/…` и `/opt/…` он не видит.
   - Верификатор проверяет, что ссылки во всех `.md` разрешаются, и что в тексте нет ≥ 25 слов подряд из цитат PRIVATE.
   - Схема WorldSpec привязана к паре pydantic/pydantic-core.
   - Новые каталоги сами по себе ничего не ломают.
10. **Главные рекомендации.**
    - Пакет `src/vkm_corpus/` с односторонней зависимостью от `vkm_world`.
    - Extras и отдельный lock с ограничениями из `worldspec.lock.txt`.
    - Собственный конверт processing-provenance, а не `vkm_world.core.provenance.Provenance`.
    - Область источника хранить как raw + нормализованное значение (`Scope`) + тип отображения.
    - Детерминированный `work_id` по «якорному» источнику и курируемая таблица связей.
    - Явный статус `SKIPPED_BY_REGISTER` для 013/022.
    - Усиление стража через decision note: `.epub`, `.parquet`, `.duckdb`, infra-суффиксы, IP.
    - `evidence/`, `catalogues/`, реестр и `vkm_world` в этой задаче не трогать.

## 1. Что существует

### 1.1 PUBLIC (222 отслеживаемых файла)

| Путь | Что | Значение для Corpus Platform |
|---|---|---|
| `src/vkm_world/` | 38 файлов, ≈ 3,1 тыс. строк, pydantic v2: типизированный фундамент WorldSpec | переиспользовать выборочно (§1.2); не менять |
| `schemas/worldspec_vnext.schema.json` | JSON Schema, генерируется `scripts/export_worldspec_schema.py` из `WorldSpec.model_json_schema()` | не трогать; равенство коду проверяют тест и верификатор |
| `scripts/` | `verify_canonical_repository.py` (v2.0.0), `build_public_catalogues.py` + `public_catalogue_map.json` (76 каталогов), `build_evidence_from_legacy.py`, `build_record_routing.py`, `export_worldspec_schema.py`, `frozen_references.json` | не трогать; образец детерминированной сборки + manifest |
| `tests/world/` | 6 файлов, 209 тестов | не трогать; эталон стиля (синтетика, `tmp_path`) |
| `evidence/` (74), `catalogues/` (11) | public-safe каталоги Phase 1 (без цитат) + `evidence/PUBLIC_CATALOGUE_MANIFEST.json` | только чтение: источник crosswalk (`record_index.csv`) и метаданных 001–041 (`SOURCE_COVERAGE_MASTER.csv`) |
| `docs/reset_2026_09/run_kit/` | исторический run kit облачного прогона: `tools/extract_corpus_text.py` (PyMuPDF), `split_ocr_all.py`, `render_page.py`, `merge_sweep.py`, `quote_check_v2.py`, `_roots.py`; `corpus_extraction_summary.json` | REUSE как reference и регрессия; сам каталог не менять (он исключён из проверок путей) |
| `requirements/` | `worldspec.in`, `worldspec.lock.txt` (точные pin; схема WorldSpec генерируется именно этой парой pydantic/pydantic-core) | не менять; новый lock — рядом |
| `pyproject.toml` | дистрибутив `vkm-world` 0.1.0; `requires-python >=3.13,<3.14`; deps numpy, pydantic; extra `test`; `packages.find include = ["vkm_world*"]`; pytest: `testpaths=["tests"]`, `pythonpath=["src"]`, `addopts="-ra -p no:cacheprovider"` | точка расширения (координатор) |
| `.gitattributes` | `text eol=lf` для py/md/csv/json/yaml/yml/sh/js/toml/txt; LFS-страж для zip/xlsx/xls/gpkg/pdf/docx и pt/pth/ckpt/safetensors/onnx/joblib/pkl/h5/hdf5/**parquet**/feather/npy/npz | только добавлять правила |
| `.gitignore` | `work/`, `.venv*/`, `.env` и `.env.*` (кроме `.env.example`), `*.pem`, `*.key`, `credentials*.json`; вложенный клон PRIVATE и `SKRU1_ACTUAL_DATA_TABLES_v1/` игнорируются | учесть для `infra/` |
| локально, игнорируется | `.venv` (29.08, без pydantic), `work/` (legacy-скретч до reset), `src/skru1/__pycache__`, `src/skru1_research.egg-info` (остатки legacy-пакета), вложенный клон PRIVATE | не использовать; не удалять в этой задаче |

### 1.2 Пакет `vkm_world`: модули, типы, пригодность для платформы

| Модуль | Ключевые типы и функции | Для Corpus Platform |
|---|---|---|
| `core/base.py` | `WorldObject(id, name, provenance, notes; extra=forbid)`; `ID_RE = ^[A-Za-z0-9][A-Za-z0-9_.:\-/+]*$`; `SOURCE_ID_RE = ^(VKM-SRC-\d{3}\|EXT-SRC-\d{3}\|EXTWEB-…)$`; `duplicate_ids` | `ID_RE` допускает `VKM-SRC-243:p0001`. `SOURCE_ID_RE` шире понятия Source платформы (EXT-SRC, EXTWEB) и ровно 3 цифры. `WorldObject` для документных объектов не брать |
| `core/provenance.py` | `EpistemicStatus` (7), `EvidenceType` (13), `Scope` (19), `SKRU1_*_SCOPES`, `Scale` (7), `SpatialLevel` (27), `SourceRef(source_id, pdf_page:int, printed_page:str, locator, evidence_ids, extraction_method)`, `TemporalSupport(event_date, event_date_end, measurement_date, processing_date, publication_date, available_from, precision, available_from_precision; usable_at)`, `DATE_PRECISIONS`, `date_bounds`, `parse_partial_date`, `Uncertainty*`, `Transfer`, `Provenance` (+ валидаторы FACT/DERIVATION/…), `Quantity`, `check_scale_use`, `check_site_use` | `Scope`, `DATE_PRECISIONS`, `date_bounds`, `parse_partial_date` — переиспользовать импортом. `Provenance`, `Quantity`, `SourceRef` — это научный провенанс (контракт §36, §38): для документного слоя не использовать |
| `core/io.py` | `sha256_file`, `sha256_bytes`, `canonical_json`, `sha256_json`, `resolve_repo_path` (отклоняет абсолютные и `\`-пути, выход за корень), `write_*_atomic` (tmp в `<root>/work/<scope>/`, `os.replace`), `artifact_inventory`, `snapshot_paths` | хеши и канонический JSON — переиспользовать. Атомарные писатели работают только внутри переданного `root` (для `$VKM_DATA_ROOT` пришлось бы передать его как root, и tmp окажется в `$VKM_DATA_ROOT/work/io`, а не `tmp/`) |
| `core/units.py` | `Dimension`, реестр единиц, `check_unit` | не нужно в v0 (единицы — будущий L2/L3, контракт §57) |
| `evidence/sources.py` | `CoverageLevel` (8), `FINAL_ALLOWED`, `SourceKind` (14), `SourceRecord(extra=allow; kind: SourceKind \| str; scope: str; coverage_level; coverage_basis)`, `load_register_ids(root)` (читает только `resource_id`), `coverage_errors` | `CoverageLevel` — импортировать как сырое покрытие evidence-ревью источника. `SourceRecord` ≠ Source платформы (§5) |
| `spatial/crs.py` | `CRSKind` (STATE_GEODETIC, STATE_PROJECTED, LOCAL_MINE_GRID, MAP_ONLY, PROJECT_ENGINEERING, UNKNOWN), `VerticalKind`, `AxisConvention`, `TransformKind`, `CoordinateSystem`, `CoordinateTransform`, `crs_reference_errors` | вид системы координат, а не статус геометрии (§5). Для bbox страницы не использовать |
| `spatial/hierarchy.py`, `geology/*`, `mining/*`, `materials/*`, `physics/*`, `observations/*` | предметные объекты мира | не касаются L1; будущие L2–L4 ссылаются на document ID |
| `chronology/events.py` | `EventClass` (2), `EventType` (35, в том числе PUBLICATION, DATA_AVAILABILITY), `Event`, `known_at`, `known_physical_at` | семантика доступности (D-03, D-16) — для `available_from` в Work |
| `mathmeta/models.py` | `MathClass`, `Origin`, `MathModelRecord` (source_ids и locator обязательны; реестр MM-… на 279 моделей) | document Formula ≠ MM-запись (контракт §15). В v0 MM-id не создавать |
| `worldspec/model.py`, `worldspec/io.py` | `SCHEMA_VERSION = worldspec-vnext/0.1`, `WorldStatus`, `WorldMeta`, `UnknownItem`, агрегат `WorldSpec`; `to_json` (канонический), `content_hash`, `json_schema()` | не трогать: любое изменение моделей меняет JSON Schema |
| `governance/leakage.py` | `scan`, `sanitize_paths`, `FORBIDDEN_*`, verbatim-shingles | общий страж (§4.1): использовать в тестах платформы; менять только через decision note |
| `validation/*` | заморозка кандидатов, однократный доступ к test, метрики, split-guards | не относится к L1 |

Документных типов (Work, Page, Block, Figure, Table, Formula, BibliographyEntry, Author, Venue, Artifact, ProcessingRun)
в коде нет ни в каком виде.

### 1.3 Уже существующие словари статусов

| Ось | Где | Значения | Замечание |
|---|---|---|---|
| Эпистемический статус | `EpistemicStatus` | FACT, DERIVATION, INTERPOLATION, MODEL_CHOICE, ENGINEERING_ASSUMPTION, ANALOGUE, UNKNOWN | значения «авто» нет. Документный объект не должен нести этот статус |
| Покрытие/ревью источника | `CoverageLevel`; `evidence/sources/SOURCE_COVERAGE_MASTER.csv` (41 строка) | FULLY_REVIEWED 34, RELEVANT_SECTIONS_REVIEWED 5 (004, 017, 020, 028, 040), SUPERSEDED_BY_COPY 1 (013), RETIRED_NOT_EVIDENCE 1 (022); ещё в enum: LOW_RELEVANCE_CONFIRMED, UNREADABLE_BLOCKER, PARTIAL_IN_PROGRESS, UNSEEN | это человеческое evidence-ревью источника, а не ревью объектов |
| Метка ревью в notes реестра | `SOURCE_REGISTER.csv` notes | 065–195: «UNSEEN» (131); 196–251: `INTAKE_QUICK_LOOK_NOT_EVIDENCE` + UNSEEN (56); 001–064: без метки (64) | исходник для QUICK_LOOK_ONLY |
| Способ извлечения записи | `SourceRef.extraction_method`; sweep | TEXT_LAYER 7081, VISUAL_READ 3399, OCR_VISUALLY_CONFIRMED 2537, DOCX_TEXT 319, GRAPH_DIGITIZED_APPROX 199, OCR 61 | native/OCR платформы стыкуются с TEXT_LAYER/OCR |
| Проверка цитат | PRIVATE `11_evidence_vnext/merged/quote_check_v2.csv` | EXACT, EXACT_ADJACENT_PAGE, EXACT_SEGMENTS, PARTIAL_SEGMENTS, FUZZY, VISUAL_NOT_IN_TEXT_LAYER, OCR_TEXT_MISMATCH, NOT_FOUND, NO_QUOTE | готовая регрессионная метрика качества текста страниц |
| Идентичность файла при приёмке | intake-манифесты, notes | TITLE_PAGE, DOI, ISBN, AUTHOR_TITLE, AUTHOR_HEADER, FILENAME_PART_LABEL, BYTE_DUPLICATE, BIBLIOGRAPHIC_DUPLICATE, SAME_WORK_AS, PART_OF, FILENAME_PAGECOUNT_UNVERIFIED, DJVU_TITLE_NOT_VERIFIED, SCAN_NO_TEXT_LAYER, EPUB_FORMAT, FILENAME_EDITION_MISMATCH, CD_ROM_NOT_INCLUDED | прототип `identity_basis` и `quality_flags` Source |
| Доступ к внешним работам | D-12; `evidence/external/` | METADATA_ONLY, ABSTRACT_ONLY, PARTIAL_TEXT_QUERY, NOT_FOUND, PAYWALL, FULL_TEXT_READ | для Work без локального Source |
| Область: реестр | `evidence_scope` | 18 сырых значений (§2.4) | «грязный» |
| Область: код | `Scope` | SKRU1, SKRU1_SKRU2_PILLAR, SKRU1_OR_SKRU2_UNATTRIBUTED, SOLIKAMSK_GROUP, SKRU1_SKRU2 (legacy), SKRU2, SKRU3, BKPRU1–4, USOLSKY, OTHER_VKM_SITE, VKM_REGIONAL, OTHER_POTASH_SITE, NON_VKM, GENERAL_METHOD, PROJECT, UNSTATED | лучший кандидат в нормализованный словарь |
| Область: разведка | `literature_hunt_2026-09-27/PHYSICAL_WORLD_SOURCE_PRIORITY.csv` `site_scope` | VKM_REGIONAL 268, GENERAL_METHOD 227, NON_VKM_ANALOGUE 166, OTHER_VKM_SITE 83, SKRU1 12, SKRU1_SKRU2_PILLAR 6, BLOCK201 3 | третий словарь |
| Область: поиск объектов | `skru1_object_search_2026-09-27/SKRU1_OBJECT_SOURCE_INDEX.csv` `site_scope` | SKRU1_EXACT 422, SOLIKAMSK_AREA 166, SOLIKAMSK_AREA/UNVERIFIED 33, VKM_REGIONAL 14, OTHER_VKM_SITE 10, UNKNOWN 1 | четвёртый словарь |
| Область: контракт v0.2 §46 | теория | GENERAL_METHOD, SKRU1_EXACT, OTHER_VKM_SITE, ANALOG | пятый набор имён; `ANALOG` ≠ эпистемический ANALOGUE |
| Координаты | `CRSKind`; object-search `coordinate_status`/`crs_status`/`location_quality`; контракт §40 | EXACT_COORDINATED, LOCAL_COORDINATES, UNKNOWN_CRS, MAP_DIGITIZED, RELATIVE, SCHEMATIC, UNKNOWN | три разных оси (§5) |
| Время | `TemporalSupport`; D-03, D-16 | event/measurement/processing/publication/available_from + точность | `ingestion_time` (контракт §42) нет |
| Статусы обработки (прецеденты) | run kit `corpus_extraction_summary.json`; PE v1 `text_extraction_receipts/extraction_manifest.json` | OK / MISSING / NEEDS_OCR_.djvu; EXTRACTED / FILE_ABSENT / SKIPPED_LEGACY_RETIRED | 013/022 уже обрабатывались как «пропуск по реестру» |

### 1.4 PRIVATE (`$VKM_RESOURCES_ROOT`)

| Путь | Что | Статус для платформы |
|---|---|---|
| `00_registry/SOURCE_REGISTER.csv` | канон реестра: 251 строка, UTF-8 без BOM, LF, SHA-256 `e3dd4751…163c` (совпадает с `register_sha256_after` квитанции третьей приёмки) | вход; менять молча нельзя |
| `00_registry/intake/<дата>_*/` (8 каталогов приёмок) | `IMPORT_MANIFEST.csv` (колонки у каждой приёмки свои), `SHA256SUMS.txt`, `RECEIPT.json`, `README_IMPORT.md`; у третьей ещё `INVENTORY.json`, `VERIFICATION.json`, `QUICK_LOOK_NAVIGATION.csv`, `tools/` | семена метаданных Work (`exact_identity`, `identity_basis`) и `ingestion_time` |
| `00_registry/literature_hunt_2026-09-27/` | 765 строк разведки (PWL-…) с библиографией: title, authors, year, venue, volume, issue, pages, doi, isbn, publisher; колонка `acquired_2026_09_27` связывает 205 источников | семя метаданных Work 042–251; не evidence |
| `00_registry/skru1_object_search_2026-09-27/` | индекс «источник → страница/рисунок → объект» (646 строк: FIGURE 372, TABLE 39, TEXT 235); `discovery/textless_pdfs.json` | оракул полноты для детекции рисунков и таблиц (§7) |
| `00_registry/cloud_checkpoint_2026-09-26/` | `ocr_all_VKM-SRC-025.txt`, `ocr_all_VKM-SRC-037.txt`, `ocr_manifest_*.json`, `SHA256SUMS.txt` | база сравнения для OCR (§3) |
| `01_…06_*/`, `08_data_archives/` | 249 бинарников в LFS | неизменяемы |
| `10_physics_evidence/physical_evidence_v1/` | прошлый релиз (D-06): `text_extraction_receipts/`, `scripts/extract_corpus_text.py`, `ocr_source_pages.py` (pdftoppm + tesseract → `work/ocr/<SID>/p0001.txt/.tsv`) | reference, не трогать |
| `11_evidence_vnext/` | `sweep_raw` (65 чтений, 13 572 записи), `merged/` (`vn_index.csv`, `quote_check_v2.csv`, `parse_errors.json` — 24 строки), `canonical/<STREAM>/` (с `quote`), `receipts/` (в том числе `corpus_text_manifest.csv`) | текущий evidence-слой; только чтение |
| `.gitattributes` | первая строка `* -text` (D-08); LFS для pdf/zip/7z/rar/docx/xlsx/xls/gpkg/tab/dat/map/id/shp/shx/dbf/tif/tiff/bin/**djvu**/**epub** | не менять |
| `.github/workflows/*.yml` (3), `scripts/*.ps1` (2) | исторический служебный код; workflow запускаются push'ем в `main` при изменении их собственного YAML | не трогать: правка запустит fetch/canonicalize |
| `work/` | в `.gitignore`; на этой машине отсутствует | постраничного OCR Phase 1 здесь нет |

### 1.5 Ветка `legacy` (`d54025d`, 463 blob)

- OCR-пайплайна и каталога страниц в legacy нет.
- REUSE_GENERIC (как образец, без копирования):
  - `data/published_figure_digitization_v1/extraction_manifest.json` — пример манифеста нативного извлечения растра
    рисунка: имя PDF XObject, система координат растра `zero_based…x_right_y_down`, sha256 каждого файла;
  - `scripts/capture_environment.py` — снимок окружения, образец receipt окружения GLM-OCR. Список пакетов в нём —
    под старый стек;
  - `src/skru1/artifact_io.py` уже перенесён в `vkm_world.core.io`.
- Не трогать:
  - саму ветку;
  - frozen-релизы (11 references, 8 якорей в `scripts/frozen_references.json`);
  - `configs/source_manifest.csv` — вход `legacy_source_id_map.csv`;
  - legacy-OCR PUBLIC `work/data_foundation_v2/source_text`: 485 страниц 12 старых источников SRC01–SRC11 и SUP01,
    pypdf, старые id. Это не вход.

### 1.6 Окружение WORKSTATION (факт 28.09, только наблюдение)

- Python — системный 3.13.13.
  - Есть: pydantic 2.13.0, pydantic-core 2.46.0, numpy 2.4.4, pytest 9.0.3, fastapi 0.135.3, uvicorn 0.44.0,
    psycopg 3.3.4, lxml 6.1.1, pillow 12.2.0, torch 2.12.0+cu132.
  - Нет: pyarrow, polars, duckdb, pymupdf, pypdf, pypdfium2, pdfplumber, ebooklib, neo4j, opensearch-py, mcp,
    transformers, python-djvulibre.
- CLI в PATH Windows.
  - Есть: `pdftotext` (из Git for Windows), `java`, `docker`, `wsl`.
  - Нет: `pdftoppm`, `pdfinfo`, `ddjvu`, `djvused`, `djvutxt`, `tesseract`, `soffice`, `pandoc`, `qpdf`, `mutool`, `gs`,
    `magick`. WSL (`archlinux`) не проверялся — это зона агента C.
- git: глобальный `core.autocrlf=true`.
- Консоль в кодировке cp1251. Без `PYTHONIOENCODING=utf-8` вывод Python с `→` падает `UnicodeEncodeError`
  (наблюдалось).
- Путь клона содержит кириллицу.

## 2. SOURCE_REGISTER

### 2.1 Колонки и семантика

| Колонка | Семантика | Наблюдения |
|---|---|---|
| `resource_id` | `VKM-SRC-NNN`, 3 цифры | 001…251 подряд, пропусков и дублей нет |
| `canonical_path` | путь относительно корня PRIVATE | уникален. Верхние каталоги: `04_articles` 122, `03_books` 64, `06_geophysics` 26, `02_dissertations` 17, `04_analogue_projects` 16, `05_regulations` 3, `01_primary_sources` 2, `08_data_archives` 1 |
| `original_filename` | имя файла при получении (неверные имена сохраняются) | кириллица, пробелы |
| `sha256` | 64 hex, идентичность содержимого (= LFS oid) | уникальны: байтовых дублей в реестре нет |
| `size_bytes` | размер | итого 2 132 489 212 B (2,132 GB), на диске 2 049 518 735 B |
| `source_class` | тип/жанр (27 значений) | смешаны жанр и природа копии (§2.4) |
| `evidence_scope` | область источника (18 значений) | «грязный» словарь (§2.4). Область всего источника ≠ область каждого значения: например, у 023 область SKRU1, но рис. 10 относится к СКРУ-2 |
| `priority` | A 74 / B 81 / C 81 / D 15 | план чтения |
| `scientific_role` | свободный текст (242 различных) | метки ролей, семьи миров PW-x/OW-x, SEED-id |
| `migration_source` | происхождение получения (14 значений; даты и коммиты внутри строки) | источник `ingestion_time` (день) |
| `migration_status` | 7 значений: ADDED_OA_DOWNLOAD_EXACT 133, ADDED_BY_USER_EXACT 93, IMPORTED_FROM_MAIN_EXACT 12, IMPORTED_EXACT_PUBLIC 8, IMPORTED_EXACT_LOCAL 3, ORIGINAL_ARCHIVE_DELETED_AFTER_PDF_ASSEMBLY 1 (013), RETIRED_FROM_CURRENT_RESEARCH 1 (022) | верификатор считает отсутствие файла нормой только при `DELETED\|RETIRED` в `migration_status`/`evidence_scope` |
| `notes` | свободный текст: библиография, основание идентичности, связи (SAME_WORK_AS, PART_OF, «contains …», «FULL TEXT, distinct from …»), флаги quick look и UNSEEN | связи — неструктурированные; парсинг только как AUTO |

### 2.2 Кто пишет и кто проверяет

- **Пишут** (все — коммиты PRIVATE):
  - скрипты приёмок: `00_registry/intake/2026-09-27_user_general_theory_books_intake/tools/do_intake4.py`,
    `skru1_object_search_2026-09-27/tools/do_intake3.py`, `literature_hunt_2026-09-27/tools/oa_intake.py` и др.;
  - ручные коммиты, например `49e7a99` (013/025) и `7f57eae` (022).
- **Читают и проверяют:**
  - `scripts/verify_canonical_repository.py` (группа `private_sources`, §4.2);
  - `vkm_world.evidence.sources.load_register_ids` (только `resource_id`);
  - `scripts/build_evidence_from_legacy.py sources --check` (12 строк `legacy_source_id_map.csv` с сырыми
    `register_source_class`/`register_evidence_scope`);
  - run kit `extract_corpus_text.py` и `render_page.py`;
  - `literature_hunt_2026-09-27/tools/state_counts.py`;
  - скрипты PE v1 (legacy).
- Отдельной схемы или валидатора колонок, кроме трёх обязательных колонок верификатора, нет.

### 2.3 Хеши

- Верификатор (`private:registered_files`): 249 файлов sha256-verified, 0 LFS-указателей, 0 неожиданно отсутствующих,
  2 отсутствуют по статусу реестра (013, 022), 0 несовпадений.
- Ручная выборка (hashlib, полное чтение): совпали SHA-256 и размер у шести файлов. Сигнатуры верные:
  - `VKM-SRC-001` (PDF, `%PDF-1.3`);
  - `VKM-SRC-023` (DOCX, `PK`);
  - `VKM-SRC-037` (DjVu, `AT&TFORM`); контрольно пересчитан `certutil`, совпало;
  - `VKM-SRC-245` (DjVu);
  - `VKM-SRC-249` (EPUB, `PK`, mimetype `application/epub+zip`);
  - `VKM-SRC-251` (PDF).
- LFS-объект `VKM-SRC-022` из локального кэша `.git/lfs` тоже совпал с реестром.

### 2.4 Распределения

Форматы:

| Расширение | Строк | Зарегистрировано | На диске |
|---|---|---|---|
| `.pdf` | 238 | 1951,7 MB | 1951,7 MB |
| `.djvu` | 9 | 57,9 MB | 57,9 MB |
| `.epub` | 1 | 22,1 MB | 22,1 MB |
| `.docx` | 1 | 17,8 MB | 17,8 MB |
| `.zip` | 2 | 83,0 MB | 0 |

Крупнейшие файлы: 025 (96 MB), 227 (94 MB), 048 (91 MB), 209 (68 MB), 208 (67 MB).

`evidence_scope` — все 18 значений и предлагаемая нормализация в `vkm_world.core.provenance.Scope`:

| Сырое значение | n | Источники (если ≤ 9) | Нормализованное | Тип отображения |
|---|---|---|---|---|
| GENERAL_METHOD | 79 | | GENERAL_METHOD | EXACT |
| VKM_REGIONAL | 76 | | VKM_REGIONAL | EXACT |
| OTHER_VKM_SITE | 33 | | OTHER_VKM_SITE | EXACT |
| NON_VKM_ANALOG | 30 | | NON_VKM | SYNONYM (метка «ANALOG» — не эпистемический ANALOGUE) |
| SKRU1 | 9 | 023, 046, 067, 069, 074, 092, 096, 197, 241 | SKRU1 | EXACT, но уровня источника |
| METHOD_GENERAL | 4 | 006, 007, 008, 010 | GENERAL_METHOD | SYNONYM |
| VKM_Berezniki | 3 | 003, 004, 017 | OTHER_VKM_SITE | LOSSY (рудник БКПРУ не назван) |
| VKM_REGIONAL_and_SKRU1 | 3 | 014, 026, 037 | {VKM_REGIONAL, SKRU1} | MULTI |
| OTHER_POTASH_SITE | 2 | 009, 024 | OTHER_POTASH_SITE | EXACT |
| SKRU1_SKRU2_SKRU3 | 2 | 012, 045 | SOLIKAMSK_GROUP или {SKRU1, SKRU2, SKRU3} | AMBIGUOUS: пул (D-17) ≠ перечень рудников |
| VKM_Uralkali | 2 | 020, 028 | VKM_REGIONAL | LOSSY |
| SKRU1_SKRU2_PILLAR | 2 | 110, 168 | SKRU1_SKRU2_PILLAR | EXACT |
| VKM_regional | 1 | 001 | VKM_REGIONAL | CASE |
| VKM_Solikamsk | 1 | 002 | SKRU1_OR_SKRU2_UNATTRIBUTED? | AMBIGUOUS (слайды СКРУ-1/СКРУ-2 и других объектов) |
| other_potash_deposit | 1 | 005 | OTHER_POTASH_SITE | CASE + SYNONYM |
| BKPRU4_VKM | 1 | 016 | BKPRU4 | SYNONYM |
| LEGACY_RETIRED | 1 | 022 | — | NOT_A_SCOPE → жизненный цикл RETIRED |
| SALT_DEPOSITS_USSR_incl_VKM | 1 | 054 | {NON_VKM, VKM_REGIONAL} | MULTI |

`source_class` — 27 значений:

| Группа | Значения |
|---|---|
| Жанр | journal_article 148, monograph 27, textbook 22, dissertation 16, conference_paper 5, technical_report 3, training_manual 2 (020, 028), teaching_manual 2 (026, 201), normative_document 2 (098, 148), institutional_report 2 (214, 215), dissertation_abstract 1 (001), presentation 1 (002), practice_manual 1 (014), thesis_secondary 1 (023), thesis 1 (024), methodical_guidance 1 (037), dataset 1 (068), book_chapter 1 (124), patent 1 (144), bibliographic_index 1 (216), reference_tables 1 (221) |
| Контейнер | proceedings_volume 4 (089, 193, 203, 204), journal_issue 3 (205, 206, 207) |
| Природа копии | book_archive 1 (013), derived_convenience_pdf 1 (025), front_matter 1 (147), retired_legacy_archive 1 (022) |

`SourceKind` в коде знает только 14 значений. У 13 значений реестра соответствия нет: conference_paper,
proceedings_volume, technical_report, journal_issue, institutional_report, derived_convenience_pdf, book_chapter,
patent, front_matter, bibliographic_index, reference_tables, dataset, methodical_guidance.

`migration_source` — 14 значений: 131 OA-загрузка 27.09; 45 вторая приёмка пользователя; 23 приёмка после разведки;
11 из `main_repo_inputs_sources_primary`; 11 `user_intake_2026-09-25_curated_evidence`; 10 `resource_inbox`; 9 третья
приёмка (для DjVu основание — «по имени файла и числу страниц»); 3 + 2 пользовательских коммита `11b509f`/`ab2941a`
(без intake-манифеста); 2 поиск по СКРУ-1; по 1 `main_repo_inputs_sources_supplementary`, `local_source_archive`,
`local_project_archive`, `local_project_file`.

### 2.5 Почему нет `VKM-SRC-013` и `VKM-SRC-022`

**VKM-SRC-013** (`03_books/Baryakh_Asanov_Pankov_phys_mech_salt_rocks_VKM.zip`, book_archive, 19 922 282 B):

- ZIP страниц учебного пособия.
- В истории PRIVATE путь не встречается ни в одном коммите: файл никогда не коммитился.
- Он намеренно удалён после склейки страниц в `VKM-SRC-025` (derived_convenience_pdf).
- Коммит `49e7a99` («registry: document deleted source archive and retained merged PDF») перевёл его в
  `ORIGINAL_ARCHIVE_DELETED_AFTER_PDF_ASSEMBLY`. В notes: сохранить hash и провенанс, проект восстановлением не
  блокировать.
- Покрытие — SUPERSEDED_BY_COPY.
- Содержимое перечислить нельзя: байтов нет нигде в PRIVATE.

**VKM-SRC-022** (`08_data_archives/SKRU1_ACTUAL_DATA_TABLES_v1.zip`, retired_legacy_archive, 63 048 195 B):

- Закоммичен как LFS в `ddec539` 25.09, удалён из дерева в `b1b63f9` 25.09 вместе с другими retired-пакетами.
- Статус `RETIRED_FROM_CURRENT_RESEARCH` и область `LEGACY_RETIRED` поставил коммит `7f57eae`. Покрытие —
  RETIRED_NOT_EVIDENCE.
- LFS-объект ещё лежит в локальном кэше `.git/lfs/objects` клона; SHA совпадает. Содержимое прочитано `zipfile` без
  распаковки:
  - 277 записей, 95 659 724 B несжатых;
  - 151 CSV, 30 PNG, 19 MD, 16 JSON, 8 PY, 8 XLSX, 4 вложенных ZIP (`07_original_archives/`: reconstruction v3.2,
    model_ready v3.2, EDA targets, independent audit), 1 GPKG, 1 DOCX, 2 TXT, 1 `.sha256`, 36 каталогов;
  - разделы: `01_reconstruction_v3_2` (131), `02_eda_targets_v1` (73), `03_model_ready_only_v3_2` (26),
    `04_excel_workbooks` (7), `05_audit_and_result_tables` (27), `06_manifests` (5), `07_original_archives` (5),
    `README_FIRST.md`, `WHERE_THE_DATA_IS.txt`;
  - вложенный DOCX (17 826 165 B) — **байтовая копия `VKM-SRC-023`**: sha256 совпал с реестром.
- Это не научный документ, а retired-пакет. Его никогда нельзя распаковывать в корпус: получился бы
  дубль-«источник».

### 2.6 Не-PDF источники

| id | Формат | Путь (от `$VKM_RESOURCES_ROOT`) | Суть |
|---|---|---|---|
| 023 | DOCX | `01_primary_sources/Filatova_VKR_SKRU1.docx` | ВКР, thesis_secondary, область SKRU1 |
| 249 | EPUB | `03_books/Lowrie_Fichtner_2020_fundamentals_of_geophysics_3rd_ed.epub` | учебник, GENERAL_METHOD |
| 013, 022 | ZIP | см. §2.5 | отсутствуют по статусу реестра |
| 9 DjVu | DjVu | см. §2.9 | |

**DOCX 023.**

- 85 частей. Медиа: 41 PNG, 19 JPEG, 4 EMF, 1 GIF.
- 160 формул OMML (`m:oMath`) — нативные формулы, OCR им не нужен.
- `docProps/app.xml` Pages = 115; маркеров `lastRenderedPageBreak` — 110; явных разрывов страницы — 17.
- Phase 1 читала его по PDF-рендеру LibreOffice 24.2.7.2 (headless, Linux VM, 115 стр.). 538 записей evidence
  ссылаются на `pdf_page` этого рендера. Пагинация производная и зависит от версии рендерера и шрифтов.

**EPUB 249.**

- EPUB 2.0, конвертация Kindle (dc:identifier — ASIN и `urn:uuid`).
- 391 HTML в spine, 881 GIF (вероятно, формулы-картинки), 376 JPEG.
- `page-list` и `pagebreak` нет: печатной пагинации нет.
- `META-INF/encryption.xml` обфусцирует только 4 TTF-шрифта (Adobe font obfuscation). Текст не защищён DRM.

### 2.7 Аномалии PDF (для агента C)

- У 5 PDF перед `%PDF` стоит UTF-8 BOM: 020, 028, 050, 052, 064. Смещения xref, вероятно, сдвинуты на 3 байта, и
  читателю потребуется repair. 064 — один из двух лучших планов поля СКРУ-1 (рис. 5.6).
  - Исходник не «чинить»: при необходимости делать производную копию-артефакт с провенансом и quality flag.
- У `VKM-SRC-239` в PDF есть словарь `/Encrypt`. Нужно проверить права на извлечение текста.
- Версии PDF у 233 файлов без BOM: 1.1 — 1, 1.2 — 2, 1.3 — 20, 1.4 — 75, 1.5 — 43, 1.6 — 56, 1.7 — 36. У пяти
  файлов с BOM: 1.3 — 2, 1.4 — 1, 1.5 — 2.
- Подсказки о сканах без текстового слоя:
  - `textless_pdfs.json` поиска объектов: 025, 044, 208, 209, 227, 238;
  - notes реестра: 037, 044, 056, 240, 243, 245 (нет текстового слоя); 044, 056, 148, 208, 209, 227, 228, 232, 243,
    246 (скан);
  - 042 и 140 — нестандартные или битые текстовые слои (R03 и др.).

### 2.8 Work ≠ Source: известные группы и связи (только явные признаки)

| Группа | Sources | Связь | Основание | Предложение для v0 |
|---|---|---|---|---|
| Барях–Асанов–Паньков, учеб. пособие ПГТУ | 013 (ZIP, нет на диске), 025 (склейка страниц 013; 198 стр. PDF, скан), 202 (другая цифровая копия с текстовым слоем; 199 с. по выходным данным) | одно произведение, три файла; 025 DERIVED_FROM 013; 202 SAME_WORK_AS 025 | notes 013/025/202; README второй приёмки; `local_copy_match` графа цитирований | один Work, три Source. Пагинацию 025 и 202 автоматически не выравнивать |
| Hanssen 2001 | 147 (только начальные страницы, 15 с.), 232 (полный скан) | та же работа, частичная копия | notes 147/232 | один Work; у 147 флаг «partial/front matter» |
| Орлов 2010 | 208 (с. 1–100), 209 (с. 101–199) | части одной книги (сквозная печатная нумерация) | notes 208/209 | один Work, два Source со смещением страниц |
| Седов, «Механика сплошной среды» | 229 (т. 1), 230 (т. 2) | тома двухтомника | notes; `merge_dupe_report.txt` разведки | два Work-тома + связь с набором; не сливать |
| Уралкалий, учебное пособие ч. 1 (1998) / ч. 2 (1999) | 020, 028 | части серии, разные годы | notes 028 | два Work + связь |
| Мусихин 2012 | 001 (автореферат), 196 (диссертация) | ABSTRACT_OF — разные произведения | notes 196 | два Work; не сливать |
| Мусихин, ПНИПУ | 002 (слайды), 201 (материалы 2022) | companion (серия «02 обработка») | notes 201 | два Work |
| «Горное эхо»: общие страницы | 031 с. 5 ≡ 005 с. 1; 032 с. 1 ≡ 005 с. 6; конец 034 — начало 199; у 030 верх с. 1 — конец предыдущей статьи; у 021 на с. 1 — список литературы предыдущей статьи (`evidence/sources/foreign_page_references.csv`, 24 строки) | общие или чужие страницы | notes 030/031/032/034/199 | разные Work. Связь на уровне Page: дубль страницы ≠ независимое evidence. BibliographyEntry на чужой странице не приписывать Work хоста |
| Контейнеры | 089 (том 16 «Стратегия и процессы…»: 4 строки разведки), 193 (SaltMech X: том + 9 глав, 10 строк), 203 (5 строк), 204, 205–207 (выпуски «Горного эха») | CONTAINS | колонка `acquired_2026_09_27` разведки; notes | v0: один Work на контейнер, PWL-id — во `external_ids`. Дочерние Work автоматически не создавать |
| Противоречивое «contains» | 205 (выпуск 2020 № 3 (80)) «содержит» 9 строк разведки. Им соответствуют отдельные Source 079, 081, 084, 120, 121, 122, 168, 170, 030. У части этих строк в таблице разведки стоят № 1, 2 или 4 того же года (у 081 и 122 колонтитул PDF при этом говорит «№ 3 (80)») | не проверено | notes 205 против таблицы разведки | UNVERIFIED; связь не строить. Если подтвердится — это статья-Source плюс те же страницы внутри выпуска-Source, то есть дубль страниц |
| Издания | 011 — 2-е изд. 2013 (1-е изд. 2001 цитируется в корпусе как другое издание) | EDITION_OF | граф цитирований (`OTHER EDITION`), линии EDITION_DRIFT | Work уровня издания; семейства изданий — позже |
| Похожие, но разные | 037 ≠ 014; 050 ≠ справочник Борзаковского–Папулова; 034 ≠ S01 (ГЖ 2023 № 11) | NOT_SAME | notes; `PHYSICAL_WORLD_DUPLICATES.md` §8.3 | хранить явные отрицательные связи, чтобы автоматика их не склеила |
| Retired-архив | 022 содержит байтовую копию 023 | CONTAINS_COPY | sha256 | 022 не распаковывать |
| Отклонены при приёмке, в реестре их нет | байтовые дубли 002, 096, 067; библиографические дубли 047 и 148; «не та работа» — Водопьянов С.К. | — | IMPORT_MANIFEST второй приёмки | подтверждает: в реестре нет байтовых дублей |

Готовые пространства id для crosswalk Work:

- `CW-` — 1953 цитируемые работы; `WG-` — 1832 группы работ; `CG-` — 2089 рёбер. Граф цитирований:
  `evidence/sources/SOURCE_CITATION_GRAPH.csv`, где 24 ребра указывают на локальные копии в 8 источниках.
- `PWL-` — разведка.
- `EXT-SRC-` и `EXTWEB-` — внешние работы: в `vkm_world` это `source_id`, в платформе это Work без Source.
- `LIN-` (линии перепечаток), `FAM-` (школы).

### 2.9 DjVu: что проверять по §19

Страницы посчитаны разбором IFF-контейнера: число `FORM:DJVU` — страницы, `FORM:DJVI` — разделяемые компоненты.
Текстовый слой — наличие чанка `TXTz`/`TXTa` в странице. Декодером это ещё нужно подтвердить.

| id | Файл (`03_books/`, у 037 — `05_regulations/`) | Страниц | DJVI | Стр. с текстом | В реестре | Титул проверен |
|---|---|---|---|---|---|---|
| 037 | `Solovyev_1992_VKM_mining_methodical_guidance.djvu` | 236 | 24 | 0 | 236 разворотов | да (OCR tesseract в VM, чтение Phase 1) |
| 053 | `Rabotnov_1977_elements_hereditary_solid_mechanics.djvu` | **384** | 39 | 384 | «423 pages (DIRM)» — **расхождение** | нет |
| 054 | `Proskuryakov_Permyakov_Chernikov_1973_phys_mech_properties_salt_rocks.djvu` | 270 | 0 | 0 | 270 | нет |
| 221 | `Gauss_Kruger_coordinate_tables_and_sheet_frame_tables.djvu` | 328 | 33 | 0 | 328 | нет (издание не установлено) |
| 229 | `Sedov_continuum_mechanics_vol1.djvu` | 528 | 53 | 528 | 528 | нет |
| 230 | `Sedov_continuum_mechanics_vol2.djvu` | 562 | 1 | 560 | 562 | нет |
| 240 | `math_methods_hydrogeology_engineering_geology.djvu` | 87 | 9 | 0 | 87 | нет (авторы не установлены) |
| 245 | `Wunsch_1978_system_theory_ru.djvu` | 290 | 29 | 0 | 290 | нет (**§19**) |
| 248 | `Bellman_Kalaba_1968_quasilinearization_nonlinear_boundary_value_problems_ru.djvu` | 185 | 1 | 185 | 185 | нет (**§19**) |

- Всего 2870 DjVu-страниц.
- «Два ранее зарегистрированных DjVu без проверки титула» из §19 — это 245 (Вунш 1978) и 248 (Беллман–Калаба 1968).
  Основания:
  - `00_registry/intake/2026-09-27_user_general_theory_books_intake/RECEIPT.json`: `identity_flags.DJVU_TITLE_NOT_VERIFIED = 2`;
  - `VERIFICATION.json`: проверка «identity of the two DjVu files from the title page» в статусе BLOCKED.
- Тот же статус у 053 и 054 (README приёмки после разведки) и у 221, 229, 230, 240 (README второй приёмки).
- Проверять титулы нужно у 8 файлов.
- Найденное расхождение числа страниц 053 оформить receipt'ом и отдельной правкой реестра, не молча.

## 3. Существующие text/OCR-артефакты

| Артефакт | Где | Формат | Покрытие | Провенанс | Как использовать |
|---|---|---|---|---|---|
| Сплошной OCR 025 и 037 | PRIVATE `00_registry/cloud_checkpoint_2026-09-26/ocr_all_VKM-SRC-{025,037}.txt` (503 KB, 1,28 MB) + `ocr_manifest_*.json` + `SHA256SUMS.txt` | UTF-8, маркеры `===== PDF_PAGE N (OCR) =====` (N — физический индекс с 1; у 037 — DjVu-разворот) | 198 + 236 страниц | tesseract 5.3.4 `rus`, psm 3, 300 dpi, рендер pymupdf / ddjvu, 25.09.2026. На каждую страницу chars, words, `mean_word_conf` (у 037 37 разворотов < 75) | база сравнения для GLM-OCR: символы на страницу, доля совпадений, страницы с низким conf. Не канон и не вход |
| Хеши постраничного текста Phase 1 | PRIVATE `11_evidence_vnext/receipts/corpus_text_manifest.csv` | `layer, path, bytes, sha256` | 3473 строки: `corpus` 2601 (`<SID>/p0001.txt` + `all.txt` для 001–041, кроме 013, 022 и 037; 023 через pandoc), `ocr` 872 (txt + tsv + manifest для 025 и 037) | PyMuPDF 1.28.2, облачная VM (`docs/reset_2026_09/run_kit/venv_research_freeze.txt`) | регрессия: нативный экстрактор с тем же PyMuPDF должен воспроизвести хеши 001–041 или отчитаться о diff. Самих текстов в git нет |
| Сводка извлечения Phase 1 | PUBLIC `docs/reset_2026_09/run_kit/corpus_extraction_summary.json` | JSON по источникам | 41: OK 38, MISSING 2 (013, 022), NEEDS_OCR 1 (037); 37 PDF — 2447 страниц, 2204 с текстом (> 40 символов) | run kit `extract_corpus_text.py` | контрольные числа страниц для 001–041 |
| Квитанции PE v1 | PRIVATE `10_physics_evidence/physical_evidence_v1/text_extraction_receipts/` | `extraction_manifest.json` (41: EXTRACTED 39, FILE_ABSENT 1, SKIPPED_LEGACY_RETIRED 1) + OCR-квитанции 001, 025, 037 | 001–041 | 25.09.2026, legacy | reference статусов; не трогать |
| Проверка цитат | PRIVATE `11_evidence_vnext/merged/quote_check_v2.csv` | вердикт на запись | 13 572 записи: EXACT 7585, EXACT_ADJACENT_PAGE 60, EXACT_SEGMENTS 381, PARTIAL_SEGMENTS 331, FUZZY 2030, VISUAL_NOT_IN_TEXT_LAYER 1111, OCR_TEXT_MISMATCH 1025, NOT_FOUND 500, NO_QUOTE 549 | run kit `merge_sweep.py` + `quote_check_v2.py` | **evidence-anchored регрессия**: прогнать ту же проверку по новому каноническому тексту страниц (экспорт в раскладку `<SID>/pNNNN.txt` во временный каталог). EXACT/FUZZY не должны молча падать. Публикуются только числа |
| Журнал визуальной OCR-QA | PUBLIC `evidence/qa/visual_ocr_qa_ledger.csv` (6902 строки) + `evidence_corrections.csv` (200) | колонки `text_layer_value`, `ocr_value`, `visual_value`, `render_png` (`VKM-SRC-NNN_p<N>_<dpi>.png`) | 001–041 | Phase 1 | список «трудных» страниц для canary и QA; рендеры в git не вошли |
| Индекс объектов СКРУ-1 | PRIVATE `00_registry/skru1_object_search_2026-09-27/SKRU1_OBJECT_SOURCE_INDEX.csv` | locator в свободной форме («pdf p.N (printed N); Рис. N.N») | 646 строк | поиск 27.09 | оракул полноты: на указанных страницах детектор должен найти Figure или Table. Тип рисунка отсюда не брать |
| Legacy-тексты | PUBLIC `work/data_foundation_v2/source_text` (игнорируется) | `SRCxx_pNNN.txt` | 12 старых источников | pypdf, до reset, старые id | не использовать |

Инструменты Phase 1 (REUSE как reference, без импорта):

- `extract_corpus_text.py` — `pymupdf` `page.get_text()`, `pNNNN.txt`;
- `split_ocr_all.py`;
- `render_page.py` — PyMuPDF / `ddjvu`; для 023 — рендер LibreOffice;
- `_roots.py` — корни из окружения и `roots.env`.

Они завязаны на `sys.exit`, глобальные корни и `$VKM_WORK`, поэтому как библиотеку их не импортировать. Соглашение о
физическом индексе с 1 и 4-значном `pNNNN` стоит сохранить.

## 4. Контракты, которые нельзя сломать

### 4.1 Страж утечки `vkm_world.governance.leakage.scan` — точные правила

- **Что сканирует.** Только отслеживаемые файлы (`git ls-files`). Неотслеживаемые не сканируются до коммита;
  несуществующие пропускаются. `ALLOWLIST` пуст («never extended silently»).
- **Расширения.** Если суффикс (регистр не важен) входит в `FORBIDDEN_EXT` = `.pdf .djvu .docx .doc .zip .7z .rar .xlsx
  .xls .tif .tiff`, это ошибка. **Не входят:** `.epub`, `.djv`, `.pptx`, `.odt`, `.rtf`, `.jp2`, `.parquet`, `.duckdb`,
  `.arrow`, `.feather`, `.png`, `.jpg`.
- **CSV и TSV.** Первая строка (strip, lower) не должна пересекаться с `FORBIDDEN_COLUMNS` = `quote, verbatim_quote,
  ocr_text, page_text, full_text`.
- **JSON и JSONL.** Ключи объектов на любой глубине, в нижнем регистре, не должны пересекаться с `FORBIDDEN_COLUMNS`.
  - Проверяется и каждая строка JSONL.
  - Попадают имена в `properties` JSON Schema, в mapping OpenSearch, а также ключи `$defs` — туда pydantic пишет имена
    классов. Класс `Quote` даст ключ `quote`, и это FAIL. Строки внутри `required: [...]` — значения, их страж не
    проверяет.
- **Размер.** `.md .csv .json .jsonl .py .yaml .yml .txt` больше 5 000 000 байт — ошибка.
- **Путевые фрагменты.** Проверяются в `.py .yaml .yml .json .jsonl .csv .md`: `.toml` не проходит внешний фильтр,
  `.txt` не входит в `PATH_CHECK_SUFFIXES`.
  - Фрагменты:
    - путь клона PRIVATE в домашнем каталоге облачной VM;
    - Windows-путь этого клона диплома вида `<буква>:\Диплом` (в коде — с конкретной буквой);
    - `work/ocr/` — в `.md` разрешён, в коде и данных запрещён;
    - `/home/` + `user/`, `/tmp/` + `claude-`, `/root/` + `.claude/`.
  - Исключения: `docs/reset_2026_09/run_kit/` для всего, кроме `.py`; файлы, в пути которых есть
    `governance/leakage.py` или `test_leakage`.
- **Не проверяется ничем:**
  - пути в `.sh .ps1 .js .service .conf .ini .cfg .sql .cypher .toml .txt`, `Dockerfile`, `*.env.example`;
  - IP-адреса, логины, токены;
  - содержимое Parquet и DuckDB.
- **Связанные правила.**
  - `sanitize_paths`: облачные пути переписываются в `$VKM_RESOURCES_ROOT/`, `<PUBLIC>/`, `$VKM_WORK/`; в ячейках
    данных `work/ocr/` → `PRIVATE:ocr/`.
  - Verbatim: шинглы по 12 слов (`SHINGLE_WORDS`), предел 25 слов прозы подряд (`VERBATIM_LIMIT_WORDS`), числа не
    считаются. Колонки с подсказками `cited_work`, `citation`, `title`, `authors`, `bibliograph`, `reference`
    пропускаются.

### 4.2 `scripts/verify_canonical_repository.py`: группы и реакция на новые каталоги

| Группа | Что проверяет | Блокирует |
|---|---|---|
| `registry` | схема `scripts/frozen_references.json` | да |
| `frozen_references` | 8 якорей (ветка `legacy` + 7 тегов) и 11 references в pinned-коммитах и в `legacy`, с обходом manifest'ов. Нет тега → `SKIPPED_REF_UNAVAILABLE`; сдвинутый тег → FAIL | FAIL — да |
| `retired_references` | 14 файлов v3.2 в `10452b0` | да |
| `private_sources` | только с `VKM_RESOURCES_ROOT`. Реестр читается (utf-8-sig); обязательны `resource_id`, `canonical_path`, `sha256`; дубль id — FAIL; формат sha; хеш каждого файла. Отсутствие допустимо только при `DELETED\|RETIRED`. LFS-указатель пропускается, но oid сверяется. Строки в `08_data_archives/main_repo_snapshots/` не читаются. **PUBLIC CSV в `evidence/` и `catalogues/`** с колонкой id (`source_id`, `resource_id`, `vkm_src_id`, `vkm_source_id`, `src_id`) и колонкой sha (`sha256`, `source_sha256`, `file_sha256`, `register_sha256`) сверяются с реестром построчно | да |
| `markdown_links` | все `.md` из отслеживаемых и неотслеживаемых-неигнорируемых файлов (то есть уже и `docs/implementation_work/`). Inline-ссылки, картинки и ref-definitions вне кода должны указывать на существующий файл или каталог репозитория. Абсолютный путь, выход за корень, ссылка на игнорируемый файл (`work/`) — FAIL; ссылка во externalized/retired-префиксы — WARN | да |
| `leakage` | `leakage.scan()` (§4.1) | да |
| `host_paths` | `.py .yaml .yml .json .toml` из отслеживаемых и неотслеживаемых-неигнорируемых, ≤ 5 MB, не LFS. Шаблоны: `[A-Z]:\` или `[A-Z]:/` перед словом (только заглавная буква), `.venv\Scripts\python`, POSIX `/(home\|Users\|root\|mnt\|media)/…`. Исключения: run kit, **весь `tests/`**, `leakage.py`; прагма `host-path-ok` на строке. **Не ловит** `/srv/…`, `/opt/…`, `/var/lib/…` и строчную букву диска | да |
| `worldspec_schema` | закоммиченная схема равна `json_schema()` из кода. Чувствительна к версии pydantic | да |
| `catalogue_sync` | `public_vs_private`: 76 каталогов `PUBLIC_CATALOGUE_MANIFEST.json` против хешей PRIVATE canonical. `verbatim`: шинглы цитат PRIVATE против **всех `.md`** (кроме run kit, включая неотслеживаемые) и ячеек CSV длиннее 80 символов в `evidence/` и `catalogues/` | да |

- **Новые каталоги.** Белого списка каталогов нет. `src/vkm_corpus/`, `infra/`, `docs/corpus_platform/`,
  `docs/implementation_work/` сами по себе проверку не роняют.
- **Какие группы их затронут.** `markdown_links`, `host_paths`, `catalogue_sync:verbatim` — сразу, в том числе до
  коммита; `leakage` — после коммита.
- **Новые зависимости.** На верификатор не влияют: он использует stdlib и `vkm_world`. Исключение — версия pydantic
  (группа `worldspec_schema`).
- **Прочее.** Отчёт — JSON в stdout, с `--output` — ещё в `work/verification/…`. Exit 1 только при блокирующем FAIL.

### 4.3 `tests/world`

- **Запуск.** `python -m pytest -q tests/world`; `pyproject` добавляет `-ra -p no:cacheprovider` и `pythonpath=src`.
  Без аргументов собирается то же (`testpaths=["tests"]`): 209 тестов.
- **Состав.** `test_evidence_tables` 5, `test_foundation` 27, `test_provenance` 18, `test_public_catalogues` 86,
  `test_validation` 63, `test_verifier` 10.
- **Требования.**
  - Python-пакеты: numpy, pydantic, pytest.
  - git CLI: `ls-files` для сканирования дерева и синтетические репозитории в тестах верификатора.
  - legacy-коммит нужен одному тесту; без него тест skip.
  - PRIVATE не нужен.
- **Тесты, которые смотрят на всё дерево или на контракты.**
  - `test_public_tree_has_no_private_leakage`.
  - `test_committed_schema_matches_code`.
  - Каталожные тесты по `scripts/public_catalogue_map.json`.
  - `record_index.csv` = 13 572 записи; `vn_id[7:10]` — номер источника.
  - Все `EV-VN-…` в `docs/science/*.md`, `PROJECT_STATE_RU.md`, `CLOUD_TO_LOCAL_PHASE2_HANDOFF_RU.md` должны
    разрешаться.
  - `record_routing.csv` строится из map, не glob'ом.

### 4.4 Frozen и история

- `scripts/frozen_references.json`:
  - якоря `legacy`, `legacy/final`, `frozen/gate-b3`, `frozen/scenario-v2.1`, `frozen/scenario-v2`, `frozen/r1`,
    `frozen/r2`, `archive/v3.2-last`;
  - retired-префиксы `SKRU1_ACTUAL_DATA_TABLES_v1/`, `inputs/bootstrap/`; externalized — `inputs/sources/`.
- Тегов в клоне нет. По handoff их ставят с workstation отдельно, это вне этой задачи.
- Не делать force-push и rebase; не трогать ветку `legacy`.

### 4.5 Пути, детерминизм, receipts

- **Пути.**
  - В коде и конфигах пути относительны; PRIVATE находится только через `VKM_RESOURCES_ROOT`.
  - Run kit дополнительно использует `VKM_WORK`, `VKM_PUB` и `roots.env`. `VKM_DATA_ROOT` нигде ещё не
    используется.
  - `resolve_repo_path` отклоняет абсолютные пути, пути с `\` и выход за корень.
- **Детерминизм.** По `DATA_AND_PATH_POLICY_RU.md` §9 генерируемые файлы детерминированы: сортировка, без временных
  меток внутри хешируемого. Канонический JSON — `sort_keys`, LF.
- **Receipt.** Каждое преобразование оставляет receipt: входы, команда, SHA-256 выходов.
- **Решения D-…**
  - D-08: PRIVATE `* -text`.
  - D-10: LFS не коммитится из cloud.
  - D-13: поправки OCR-QA — только оверлей.
  - D-14: исправления — в сборщике, точечные правки через таблицу.
  - D-06: PE v1 не переписывается.
- **Публикация каталогов в PUBLIC** — только `build_public_catalogues.py` по map.
  - `catalogue_sync` требует, чтобы каждый каталог map имел источник в PRIVATE `canonical/`.
  - Поэтому runtime-вывод платформы в map добавлять нельзя.

### 4.6 Базовая линия (28.09.2026, WORKSTATION, до любых изменений)

| Проверка | Результат |
|---|---|
| `python -m pytest -q tests/world` (`PYTHONIOENCODING=utf-8`, системный Python 3.13.13) | **209 собрано: 206 passed, 3 failed**, 4,12 с (wall ≈ 5 с) |
| — `test_validation.py::test_resolve_repo_path_rejects_symlink_escape` | FAIL: `WinError 1314`, нет права на symlink (Developer Mode выключен) |
| — `test_validation.py::test_candidate_record_is_content_addressed_and_immutable` | FAIL: `write_text("{}\n")` пишет CRLF, ожидаемый хеш посчитан по LF |
| — `test_verifier.py::test_closures_from_git_objects_in_synthetic_repo` | FAIL при `core.autocrlf=true`; **PASS** при `GIT_CONFIG_PARAMETERS="'core.autocrlf=false'"` (проверено) |
| `VKM_RESOURCES_ROOT=… python scripts/verify_canonical_repository.py` | **PASS_WITH_NONBLOCKING, exit 0**, 42 проверки: 35 PASS + 7 `SKIPPED_REF_UNAVAILABLE` (7 тегов), 3,6 с |
| — `private_sources` | 251 id; 249 файлов sha256-OK; 2 отсутствуют по статусу; 0 mismatch; 0 LFS-указателей; 12 строк PUBLIC-каталогов сверены |
| — `markdown_links` / `leakage` / `host_paths` / `worldspec_schema` | 51 `.md`, 214 ссылок, 0 битых / 0 проблем / 50 файлов, 0 попаданий / PASS при pydantic 2.13.0 |
| — `catalogue_sync` | 76 каталогов синхронны; 45 669 текстов против 175 736 шинглов, 0 совпадений ≥ 25 слов |
| `build_evidence_from_legacy.py sources --check` / `monitoring --check` | OK / OK |

Эти три падения — прежняя база WORKSTATION. Регрессией Corpus Platform их считать нельзя. Для приёмки нужно либо
гонять тесты в Linux/WSL, либо исправить переносимость тестов отдельной задачей.

## 5. Конфликты старых сущностей с Corpus Platform

| № | Где конфликт | Что есть | Что требует платформа | Риск | Как развести |
|---|---|---|---|---|---|
| K1 | Review и coverage | `CoverageLevel` — покрытие источника человеческим evidence-ревью; FULLY_REVIEWED у 34 источников | контракт §47: ReviewState объектов, авто → AUTO_EXTRACTED_UNREVIEWED | объекты 001–041 унаследуют FULLY_REVIEWED — подмена «AUTO ≠ REVIEWED» | два разных поля: `Source.evidence_coverage_raw` (CoverageLevel как есть) и `review_status` (ReviewState). Объекты L1 — всегда AUTO_EXTRACTED_UNREVIEWED. Отображение — §7 п. 9 |
| K2 | Эпистемический статус | `EpistemicStatus` и `Provenance` с правилами FACT/DERIVATION | у авто-объекта нет эпистемического статуса | чтобы положить объект в `Provenance`, придётся выбрать FACT и т. п. | документный слой `EpistemicStatus` не несёт; научный провенанс ≠ processing-провенанс (контракт §36) |
| K3 | Область | 5+ словарей (§1.3); `ANALOG` в контракте против ANALOGUE в коде; `SKRU1_EXACT` против `SKRU1` | контракт §9 `site_scope`, §46 «сохранять existing scopes» | потеря сырого значения или новый шестой словарь | raw + нормализация в `Scope` + тип отображения; crosswalk имён контракта в документе, без новых enum |
| K4 | Тип источника | `source_class` (27) против `SourceKind` (14), `source_type` разведки (14 других) | контракт §9 `source_type`; Work ≠ Source | жанр и природа копии в одном поле | `Work.work_type` (жанр) отдельно от `Source.copy_kind`/флагов (FULL, PARTIAL_FRONT_MATTER, PART_n, DERIVED_MERGED, CONTAINER, RETIRED); raw сохранить |
| K5 | Страница | evidence: `pdf_page` (int, физический, с 1) + `printed_page` (свободная строка) + `locator_extra`; OCR-маркеры `PDF_PAGE N`; тексты `pNNNN`; рендеры `VKM-SRC-NNN_p<N>_<dpi>.png` | контракт §10 `page_number`; `page_id` | неверный выбор ключа (печатный номер) ломает crosswalk с 13 572 записями | `page_id` на физическом индексе для PDF и DjVu; печатные метки — raw + разбор. У 037 и 243 развороты: логическая половина — производная область, не новая Page. DOCX 023 — страницы закреплённого рендера. EPUB 249 — spine |
| K6 | Формат id | префиксы заняты: `VKM-SRC-`, `EXT-SRC-`, `EXTWEB-`, `EV-VN-S…-…`, `EV-GEO-`, `PAR-`, `CW-`, `WG-`, `CG-`, `LIN-`, `FAM-`, `PWL-`, `MM-`, `MGEO-`, `MON-`, `PCF-`, `QA-C-`, `XF-`, `PC-`, `F-…` | новые work, page, block, figure и прочие id | коллизии; `:` запрещён в именах файлов Windows; позиционные id хрупки (урок COVERAGE_DUPLICATES-018: `vn_id` пришлось закрепить `vn_index.csv`) | новые префиксы вне списка; id не использовать как имя файла; объекты внутри страницы — version-aware + индекс id, молчаливая перенумерация запрещена |
| K7 | Источник и Work | в `vkm_world` `source_id` включает EXT-SRC и EXTWEB (внешние работы) | Source = только файл VKM-SRC | смешение понятий | внешние id → `Work.external_ids`; Source строго `^VKM-SRC-\d{3}$` |
| K8 | Координаты | `CRSKind` (вид системы), контракт §40 (статус геометрии), object-search `coordinate_status`/`crs_status` | bbox объекта на странице | bbox «в CRS» или выдуманный EPSG | bbox — только page space (единицы и начало координат заданы явно). GeometryStatus контракта — отдельный enum для будущей оцифровки; crosswalk с `CRSKind`, без слияния |
| K9 | Время | `TemporalSupport` без `ingestion_time`; D-03 и D-16 для `available_from` | контракт §42: шесть времён; конверт с `created_at` | `created_at` в хешируемом содержимом ломает детерминизм | `created_at` и `processing_time` — атрибуты прогона, из контент-хеша исключены. `ingestion_time` — дата приёмки (день) из intake или git. `publication_time` — год с точностью |
| K10 | Формулы | реестр MM-… (279) с локаторами, класс `MathModelRecord` | Formula v0 без семантики | авто-формулы попадут в реестр MM | не писать в MM; будущая связь Law DEFINED_BY Formula |
| K11 | Библиография | граф цитирований Phase 1 (evidence, LLM-чтение с quote-check); чужие списки литературы на страницах источника | BibliographyEntry; ссылка на Work только при надёжном совпадении | запись припишут Work хоста; граф Phase 1 выдадут за авто | в BibliographyEntry отдельно хранить `host_page_id` и `citing_work_id` (может быть UNKNOWN). Граф Phase 1 — оракул и crosswalk, не авто-объекты |
| K12 | Дубли страниц | общие страницы «Горного эха», выпуски, содержащие статьи-источники | контракт: дубль ≠ независимое evidence | двойной счёт | флаг или связь PAGE_DUPLICATE_CANDIDATE по хешу нормализованного текста или рендера; без слияния |
| K13 | Нормализация реестра на месте | сырые значения в `legacy_source_id_map.csv` (12 строк), в `SOURCE_COVERAGE_MASTER.csv` (`register_scope`), в `--check` | чистые значения | drift и FAIL проверок | нормализовать только в слое платформы |
| K14 | Имена полей | запрет ключей `page_text`, `full_text`, `ocr_text`, `quote` | естественные имена для Page и OCR | FAIL стража на JSON Schema, mapping, фикстурах | `text`, `native_text`, `recognized_text`, `raw_output`, `normalized_text`; не называть модели `Quote` |
| K15 | Пути OCR | прецеденты `work/ocr/<SID>/p0001.*` (PE v1, run kit) | артефакты в `$VKM_DATA_ROOT` | литерал `work/ocr/` в `.py`/`.json` — FAIL стража | логические корни `$VKM_DATA_ROOT/artifacts/…` |
| K16 | Статусы обработки | OK/MISSING/NEEDS_OCR_*; EXTRACTED/FILE_ABSENT/SKIPPED_LEGACY_RETIRED | NATIVE_OK, OCR_REQUIRED, OCR_OK, PARTIAL, UNSUPPORTED, FAILED, NEEDS_REVIEW | 013 и 022 станут FAILED или «тихо» пропадут | добавить `SKIPPED_BY_REGISTER` с кодами причин |
| K17 | Окончания строк | WORKSTATION `autocrlf=true`; PUBLIC `.gitattributes` не знает `.service`, `.conf`, `.cypher`, `.sql`, `Dockerfile`, `.env.example` | infra-шаблоны для Linux-хостов | CRLF в systemd, compose и скриптах; расхождение хешей в тестах | eol-правила для новых типов; тесты пишут байты с `\n` |
| K18 | pydantic | схема WorldSpec генерируется закреплённой парой 2.13.5/2.46.5 | FastAPI, MCP SDK и прочие зависимости сервисов | обновление pydantic меняет схему → FAIL теста и верификатора | lock платформы с `-c requirements/worldspec.lock.txt` |

**Как будущий L2 EVIDENCE сошлётся на `page_id` и `figure_id`, ничего не ломая.**

- Записи evidence не переписываются: `sweep_raw`, `canonical`, PUBLIC-каталоги остаются как есть.
- Строится производная таблица `evidence_page_link` из PUBLIC `evidence/sources/record_index.csv`
  (`vn_id, source_id, pdf_page` — без цитат). Колонки: `vn_id → page_id`, `link_status`.
  - EXACT_PHYSICAL_INDEX — PDF и DjVu;
  - RENDER_DEPENDENT — 023: нужна проверка совпадения текста;
  - NOT_APPLICABLE.
- Рисунки: crosswalk `figure_label` (из `locator_extra`, колонки `figure_table` журнала QA, `figure_or_table` индекса
  объектов) → `figure_id` со статусом `AUTO_MATCHED_UNREVIEWED`.
- Позже можно добавить необязательное поле `page_id` в `vkm_world.SourceRef`. Но это изменение схемы WorldSpec: нужно
  отдельное решение и регенерация `schemas/worldspec_vnext.schema.json` в том же коммите. В v0 это не нужно.

## 6. Gaps: чего не хватает

1. Пакета документного слоя нет: `vkm_corpus` отсутствует, нет ни одного типа Work, Source (как документного объекта),
   Page, Block, Figure, Table, Formula, BibliographyEntry, Author, Venue, Artifact, ProcessingRun.
2. Нет конфигурации `VKM_DATA_ROOT`: переменная нигде не используется. Нет правила, запрещающего data root внутри
   клонов.
3. В `pyproject.toml` нет `[project.scripts]` (CLI), нет extras для corpus и сервисов, `include` не охватывает новые
   пакеты.
4. Нет lock для зависимостей платформы: pyarrow, polars, duckdb, PDF, DjVu и EPUB-библиотеки, fastapi, uvicorn, mcp,
   neo4j, opensearch-py, psycopg. Нет отдельного runtime-окружения GLM-OCR (torch, transformers или vLLM под CUDA —
   вне `requires-python` проекта).
5. На WORKSTATION нет воспроизводимого окружения по lock. `.venv` устарел, системный Python не соответствует pin.
6. На Windows-стороне нет декодеров DjVu, LibreOffice, tesseract и poppler-рендера.
7. Нет схем JSON и Arrow для документного слоя, их экспорта и теста «закоммиченная схема = код».
8. Нет тестов платформы, генераторов синтетических фикстур (PDF, DjVu, EPUB, DOCX) и маркеров для сервисов и GPU
   (NOT_RUN).
9. Нет таблицы связей Work: SAME_WORK, PART_OF, CONTAINS, ABSTRACT_OF, EDITION_OF, NOT_SAME. Связи есть только в
   свободном тексте notes.
10. Нет таблицы нормализации области и типа источника.
11. Страж утечки и верификатор слепы к `.epub`, `.parquet`, `.duckdb`, `.arrow`, к infra-суффиксам (`.sh`, `.ps1`,
    `.service`, `.conf`, `.cypher`, `.sql`, `Dockerfile`, `.env.example`), к `/srv`, `/opt`, `/var/lib`, к IP и секретам.
12. Нет `docs/corpus_platform/`, `infra/`, eol-правил для infra-файлов.
13. Нет crosswalk-таблиц: evidence → page, CW, PWL и EXT-SRC → Work, render-имена Phase 1 → artifact.
14. Нет receipt-формата для прогонов платформы. Прецедент есть: manifest с sha входов и выходов, `RECEIPT.json` приёмок.

## 7. Рекомендации координатору

**Пакет, зависимости, lock**

1. Создать пакет `src/vkm_corpus/`, CAD — `src/vkm_cad/`, по брифу, рядом с `vkm_world`.
   - Зависимость только `vkm_corpus → vkm_world`, обратная запрещена.
   - Добавить тест: AST-скан `src/vkm_world` не видит `import vkm_corpus`. Это держит схему WorldSpec и верификатор
     независимыми.
2. В `pyproject.toml` (владелец — координатор):
   - `include = ["vkm_world*", "vkm_corpus*", "vkm_cad*"]`;
   - `[project.scripts] vkm-corpus = "vkm_corpus.cli:main"`;
   - extras: `corpus` (pyarrow, polars, duckdb, выбранные PDF, DjVu и EPUB-библиотеки, lxml, pillow) и
     `corpus-services` (fastapi, uvicorn, mcp, neo4j, opensearch-py, psycopg).
   - Базовые `dependencies` (numpy, pydantic) не расширять.
   - GLM-OCR — отдельное окружение на WORKSTATION/WSL с собственным pin или digest образа, не extra этого пакета.
3. Lock:
   - новые `requirements/corpus.in`, `requirements/corpus.lock.txt`, `corpus-services.lock.txt` собирать с
     ограничением `-c requirements/worldspec.lock.txt` — pydantic 2.13.5, pydantic-core 2.46.5, numpy 2.5.3 те же;
   - `worldspec.{in,lock.txt}` не менять;
   - прецедент версий Phase 1: pymupdf 1.28.2, pdfplumber 0.11.10, pypdfium2 5.13.0, pyarrow 25.0.1, duckdb 1.5.5
     (`docs/reset_2026_09/run_kit/venv_research_freeze.txt`).
4. Окружение WORKSTATION:
   - завести git-ignored `work/.venv-corpus` из lock;
   - для приёмки тестов записать базу «206/209 + 3 известных Windows-падения» или гонять в WSL;
   - в CLI и тестах включать UTF-8 (`PYTHONUTF8=1` или явный `encoding`) — cp1251-консоль падает.
5. Верхний уровень `vkm_corpus` импортируется без optional-зависимостей (ленивые импорты).
   - Тесты — в `tests/corpus/` с `pytest.importorskip` и маркерами `services` и `gpu`: при отсутствии статус
     NOT_RUN/skip.
   - `python -m pytest -q tests/world` и голый `pytest` не должны ломаться.

**Контракты данных**

6. Документные контракты держать отдельно — `vkm_corpus.contracts`, pydantic на границах + Arrow-схемы. Не
   наследовать `Provenance`, `SourceRef`, `WorldObject`: там научный провенанс с правилами FACT и DERIVATION (контракт
   §36–§38).
   - Импортом переиспользовать:
     - `vkm_world.core.io.sha256_file`, `canonical_json`, `sha256_json` — единые хеши;
     - `Scope` — нормализованная область;
     - `CoverageLevel` — сырое покрытие источника;
     - `DATE_PRECISIONS`, `date_bounds`, `parse_partial_date` — время публикации и доступности по D-03 и D-16;
     - `leakage.scan` и `FORBIDDEN_COLUMNS` — в тестах платформы.
7. Имена полей только из разрешённых: `text`, `native_text`, `recognized_text`, `raw_output`, `normalized_text`,
   `caption`.
   - Тест платформы экспортирует все её JSON Schema (и mapping OpenSearch, если он коммитится) во временный каталог и
     прогоняет `leakage.scan(files=…)`. Ключи `$defs` проверяются тоже.
8. Source платформы:
   - одна строка на каждую из 251 записей реестра, включая 013 и 022;
   - `source_id` (`^VKM-SRC-\d{3}$`), `source_sha256`, `canonical_path` (относительно PRIVATE), `size_bytes`,
     `format_detected` (по сигнатуре, не по расширению), `present_on_disk`;
   - `lifecycle_status`: ACTIVE, ABSENT_BY_REGISTER (013), RETIRED (022);
   - `register_sha256` снимка — в ProcessingRun;
   - сырьё реестра целиком: `source_class_raw`, `evidence_scope_raw`, `priority`, `migration_*`, `notes`.
   Статус обработки 013 и 022 — `SKIPPED_BY_REGISTER` с кодами `ARCHIVE_DELETED_AFTER_ASSEMBLY` и
   `RETIRED_NOT_EVIDENCE`. Итоговые числа: 251 = 249 обработанных + 2 пропущенных, ни одного «тихого». 022 не
   распаковывать, LFS-кэш как источник не использовать.
9. Review:
   - `Source.review_status` (ReviewState контракта §47 + при необходимости `NOT_APPLICABLE`) плюс
     `review_status_basis`;
   - отображение:
     - 001–041 из `SOURCE_COVERAGE_MASTER.csv`: FULLY → FULLY_REVIEWED, RELEVANT → RELEVANT_SECTIONS_REVIEWED;
     - 013 и 022 — не review, а `lifecycle_status`;
     - 042–195 → UNSEEN;
     - 196–251 → QUICK_LOOK_ONLY (основание — метка `INTAKE_QUICK_LOOK_NOT_EVIDENCE` в notes);
   - `evidence_coverage_raw` хранить отдельно;
   - все авто-объекты L1 — только AUTO_EXTRACTED_UNREVIEWED, без наследования от Source.
10. Область:
    - `site_scope_raw` — сырое `evidence_scope`, дословно;
    - `site_scope` — список значений `vkm_world.Scope`;
    - `site_scope_mapping` ∈ {EXACT, CASE, SYNONYM, LOSSY, MULTI, AMBIGUOUS, NOT_A_SCOPE};
    - таблица §2.4 — версионируемая константа в `vkm_corpus.contracts` с тестом «все 18 значений отображены»;
    - имена контракта §46 (`SKRU1_EXACT`, `ANALOG`) — только crosswalk в документации;
    - реестр на месте не чистить.
11. Время:
    - `created_at` и `processing_time` — атрибуты ProcessingRun и объекта, но вне контент-хеша;
    - `content_sha256` объекта считать канонически по полям содержимого;
    - `ingestion_time` Source — дата приёмки (точность «день») из intake-манифеста или `migration_source`, для 001–041 —
      из git;
    - `publication_time` Work — с точностью (год);
    - `available_from` в v0 не вычислять, либо только с основанием `ASSUMED_FROM_PUBLICATION` (D-03).
    - Детерминизм определить на уровне содержимого таблицы: хеш отсортированных канонических строк. Побайтовое
      совпадение Parquet ожидать только при одной и той же версии pyarrow и одинаковых настройках writer.

**ID**

12. Предложение для агента D; проверка — агент H.
    - Для PDF и DjVu: `page_id = VKM-SRC-NNN:pNNNN` — физический индекс с 1, как `pdf_page` evidence и `pNNNN.txt`
      Phase 1.
    - Для EPUB — свой вид, например `VKM-SRC-249:x0001` (spine).
    - Для DOCX 023 — страницы закреплённого рендер-профиля. Профиль (LibreOffice-версия, шрифты) хранится в Page.
      Совпадение с 538 записями evidence проверяется quote-check'ом.
    - В Page хранить `page_kind` и `printed_page_raw` (+ разобранный список и статус разбора).
    - В путях артефактов id не использовать как имя файла: `:` на Windows запрещён. Раскладка
      `<source_id>/p0001.<ext>`.
    - id объектов внутри страницы — version-aware: `page_id` + версия экстрактора + порядковый номер, и `content_sha256`
      рядом. При новой версии — новые id и таблица `supersedes`; молчаливая перенумерация запрещена, как с `vn_index`.
13. Work (агент D).
    - `work_id = VKM-WRK-NNN`, где NNN — номер «якорного» Source: наименьший `VKM-SRC` в явно подтверждённой группе.
      Для одиночек это номер самого Source.
    - Стабильность: новые Source получают большие номера, якорь не сдвигается. Слияние найденных позже групп —
      записью `MERGED_INTO` (tombstone), id не переиспользуется.
    - Группы — только из курируемой таблицы связей (п. 14). Ни fuzzy, ни ML, ни «похожие названия» группы не создают.
    - Для контейнеров в v0 один Work на файл. `PWL-`, `CW-`, `EXT-SRC-` — в `Work.external_ids`.
    - Метаданные Work: 001–041 из `SOURCE_COVERAGE_MASTER.csv`; 042–251 из таблицы разведки, `exact_identity`
      intake-манифестов и notes. У каждого поля — `metadata_basis`: TITLE_PAGE_VERIFIED, CATALOGUE_DATA_UNVERIFIED и т.
      п. Для DjVu без проверки титула — CATALOGUE_DATA_UNVERIFIED.
14. Таблица связей Work (seed) — малый курируемый файл с receipt, по строкам §2.8:
    - SAME_WORK 013/025/202 и 147/232;
    - PART_OF 208/209;
    - VOLUME_OF 229/230;
    - SERIES_PART 020/028;
    - ABSTRACT_OF 001→196;
    - COMPANION 002/201;
    - EDITION_OF 011;
    - NOT_SAME 037/014, 050, 034;
    - CONTAINS_COPY 022⊃023.
    Место — решение координатора (§8). Моя рекомендация — PRIVATE `00_registry/` рядом с реестром (source-related
    metadata, в брифе §1). В PUBLIC — загрузчик, валидация и тесты на синтетике. Связи, распарсенные из notes
    автоматически, — только кандидаты со статусом AUTO_PARSED_UNREVIEWED. Противоречивое «contains» 205 — в кандидаты
    не включать.

**Страж и верификатор**

15. Decision note координатору об усилении стража. Только ужесточение; нынешнее дерево это не ломает — проверено
    пробным сканом отслеживаемых файлов.
    - Добавить в `FORBIDDEN_EXT` `.epub`, `.djv`, `.parquet`, `.duckdb`, `.arrow`, `.feather`.
    - Путевые шаблоны на любой `/home/<login>/`, Windows-диск (обе косые, любой регистр), `/mnt/<диск>/`. Прогон по
      дереву вне run kit и `leakage.py` дал одно попадание: негативная фикстура в `tests/world/test_validation.py` —
      нужно исключение для `tests/`, как у верификатора.
    - Расширить суффиксы на `.sh`, `.ps1`, `.service`, `.conf`, `.ini`, `.cfg`, `.sql`, `.cypher`, `.toml`, `.txt`,
      `Dockerfile`, `*.example`.
    - Добавить проверку приватных IPv4 (10/8, 172.16/12, 192.168/16) и явных токенов в infra и docs.
    - До принятия решения — ручная проверка infra-файлов по тем же правилам.
16. `infra/`:
    - volume и путь данных — только через `${VKM_DATA_ROOT}`, placeholder'ы в `.env.example`;
    - прагма `host-path-ok` — только для документированных примеров;
    - добавить в `.gitattributes` `eol=lf` для `*.service`, `*.conf`, `*.cypher`, `*.sql`, `*.env.example`,
      `Dockerfile`, `*.ini`, `*.cfg`;
    - `*.ps1` оставить в CRLF.
17. `docs/corpus_platform/*.md` и отчёты агентов: пути `$VKM_DATA_ROOT/...` писать только в inline-коде, не
    Markdown-ссылкой; ссылки — только на существующие файлы репозитория; источники не цитировать (verbatim-guard
    проверяет все `.md` сразу).
18. В git не коммитить:
    - runtime-вывод: Parquet, DuckDB, рендеры, raw OCR, логи;
    - большие манифесты (> 5 MB — FAIL стража);
    - новые строки в `scripts/public_catalogue_map.json`.
    Коммитить только компактные receipt'ы: числа, хеши, логические пути.
    CLI отказывается работать, если `$VKM_DATA_ROOT` лежит внутри `<PUBLIC>` или `$VKM_RESOURCES_ROOT`.
19. Фикстуры тестов генерировать в `tmp_path` синтетическим текстом. Бинарные фикстуры не коммитить: `.pdf`, `.djvu` и
    `.docx` дают FAIL стража; `.parquet` уходит в LFS по `.gitattributes`. Хешируемый текст в тестах писать байтами или с
    `newline="\n"`, symlink-тесты пропускать без привилегии.

**Приёмка и регрессия с опорой на существующее**

20. Регрессия текста 001–041:
    - нативный экстрактор с PyMuPDF 1.28.2 сравнить с хешами `corpus_text_manifest.csv` (2601 файл);
    - прогнать `quote_check_v2` по новому каноническому тексту;
    - опубликовать только агрегаты (числа EXACT, FUZZY, NOT_FOUND и т. д.) против базы Phase 1.
21. OCR:
    - GLM-OCR для 025 и 037 сравнить с tesseract из `cloud_checkpoint` — символы на страницу, страницы с низким conf;
    - как истину это не использовать.
22. Полнота Figure и Table: страницы строк FIGURE и TABLE `SKRU1_OBJECT_SOURCE_INDEX.csv` (372 и 39) — оракул
    «объект есть на странице». Тип рисунка отсюда не брать.
23. Кандидаты canary (решает агент C), с готовыми базами сравнения:
    - скан — 025 (есть tesseract и 1175 записей evidence);
    - DjVu — 037 (развороты, tesseract, 1254 записи);
    - EPUB — 249;
    - DOCX — 023 (OMML, рендер);
    - PDF с BOM и формулами — 052 (Колтунов 1976);
    - карта — 014 или 064 (064 с BOM);
    - современный текстовый PDF — 197.
24. По §19:
    - после появления декодера проверить титулы всех 8 непроверенных DjVu (053, 054, 221, 229, 230, 240, 245, 248);
    - при несовпадении и для числа страниц 053 (423 против 384) — receipt в PRIVATE и отдельный коммит-правка реестра
      по решению координатора; не молча.

**Что НЕ трогать**

25. PUBLIC:
    - `src/vkm_world/**` — изменения только через decision note; `leakage.py` только ужесточать;
    - `schemas/worldspec_vnext.schema.json`, `requirements/worldspec.*`;
    - `scripts/*`, в том числе `frozen_references.json` и `public_catalogue_map.json`;
    - `evidence/**` с `PUBLIC_CATALOGUE_MANIFEST.json`, `catalogues/**`, `tests/world/**`;
    - `docs/reset_2026_09/**` и `docs/legacy/**`;
    - строки LFS-стража в `.gitattributes` — только добавлять;
    - legacy-скретч `work/` не чистить в этой задаче;
    - ветку `legacy` и frozen-релизы.
26. PRIVATE:
    - `00_registry/SOURCE_REGISTER.csv` — никаких правок без receipt и отдельного коммита;
    - `01_…06_*/` и `08_data_archives/**`;
    - `11_evidence_vnext/**` и `10_physics_evidence/**`;
    - `00_registry/{intake, literature_hunt_2026-09-27, skru1_object_search_2026-09-27, cloud_checkpoint_2026-09-26,
      cloud_phase1_2026-09-26}/**`;
    - `.gitattributes` (`* -text`);
    - `.github/workflows/*.yml` — правка запускает workflow;
    - `scripts/*.ps1`;
    - код платформы в PRIVATE не писать.

## 8. Открытые вопросы (к координатору, D и H)

1. Где живёт курируемая таблица связей Work и seed метаданных Work? Варианты: PRIVATE `00_registry/` (моя
   рекомендация) или PUBLIC, так как в ней только id и типы связей.
2. Какую пагинацию считать канонической для DOCX 023?
   - Воспроизвести рендер LibreOffice 24.2.7.2 и сверить с 538 записями evidence;
   - или ввести `page_kind = DOCX_RENDERED` с профилем рендера и `link_status = RENDER_DEPENDENT`.
   Нужны ли собственные границы Word (`lastRenderedPageBreak`, Pages = 115) как контрольные?
3. Развороты 037 и 243 (и строки evidence вида «N-N (spread)»): вводить ли в v0 логические полустраницы (производные
   области) или оставить физическую страницу и список печатных меток?
4. EPUB 249: единица «страницы» — spine-элемент или производная пагинация рендера? Как тогда должен выглядеть
   `page_number` (контракт §10)?
5. Статус `SKIPPED_BY_REGISTER` и `lifecycle_status`: добавлять к минимальному набору статусов (решение D и H)?
6. Нормализация области: `VKM_Solikamsk` (002) и `SKRU1_SKRU2_SKRU3` (012, 045) — AMBIGUOUS. Кто и когда решает: оставить
   так в v0 или ждать evidence-ревью?
7. Лицензии библиотек: PyMuPDF — AGPL, в Phase 1 использовался. Допустим ли он как зависимость PUBLIC-пакета, или
   основным взять pypdfium2 или pdfplumber?
8. Где гонять приёмочные тесты:
   - WORKSTATION (Windows, 3 известных падения);
   - WSL `archlinux`;
   - CORE.
   И нужно ли отдельной задачей чинить переносимость трёх тестов `tests/world`, не трогая их смысла?
9. Усиление стража утечки (п. 15) — принять до реализации Phase 1 FOUNDATION или после? От этого зависит, какие файлы
   infra можно коммитить.
10. Нужны ли PUBLIC-safe агрегаты прогона (числа по источникам без текста) в git как receipt, и в каком каталоге?
    Не в `evidence/` и `catalogues/`: там их зацепит `private:public_catalogues`, и возникнет соблазн добавить их в map.

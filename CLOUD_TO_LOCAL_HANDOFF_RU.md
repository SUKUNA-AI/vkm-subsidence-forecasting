# CLOUD → LOCAL HANDOFF: ночной scientific reset VKM/SKRU-1

Дата: 26.09.2026. Составлено в cloud VM по прямому указанию пользователя: остановить запуск
новых исследовательских workflow и передать работу на локальную workstation.
Ничего не удалялось, reset не выполнялся.

> **Главное.** Выполнена подготовительная фаза: preflight, ветка `legacy`, toolkit,
> Git LFS, полный OCR двух сканов, постраничное извлечение текста всех 39 документов.
> Также готовы протоколы, план из 65 чтений, инструменты и ядро provenance нового пакета.
> **Ни одно из 65 чтений корпуса не завершено.** Все агенты были остановлены в процессе
> работы, а результаты агент пишет только в конце. Поэтому научных выходов sweep, math и
> audit **нет**. Следующий локальный этап — запуск Stage 1 (sweep + audit + math wave 1) по
> готовому run kit.

> **Обновление 26.09.2026 (после checkpoint).** По просьбе пользователя рабочая ветка
> `claude/practical-volta-l67oia` влита в `main` обоих репозиториев fast-forward'ом: PUBLIC и
> PRIVATE, включая этот handoff и OCR-checkpoint. Старое состояние PUBLIC `main` сохранено в ветке
> `legacy` = `d54025d`. Локально можно работать от `main`. Незапушенный LFS-commit `292655a`
> (раздел 1) в `main` не входит.

---

## 1. Commits и ветки

### PUBLIC `SUKUNA-AI/vkm-subsidence-forecasting`

| Что | Ветка / SHA | Статус |
|---|---|---|
| Исходный `main` | `d54025d4c47b79b864076a33a4ecf877174ec922` | не изменялся |
| **`legacy`** (создана до reset) | `d54025d4c47b79b864076a33a4ecf877174ec922` | запушена, заморожена |
| Рабочая ветка | `claude/practical-volta-l67oia` | запушена |
| commit 1 | `6a80a4a` — `reset: preflight report and night-run ledger (legacy branch = d54025d)` | `docs/reset_2026_09/PREFLIGHT_REPORT_RU.md`, `NIGHT_RUN_LEDGER_RU.md` |
| commit 2 | `ce41e03e185348cc9702a79e284c40c4303a3237` — `reset checkpoint: run kit … and vkm_world provenance core` | `docs/reset_2026_09/run_kit/**`, `src/vkm_world/**`, `tests/world/test_provenance.py`, `pyproject.toml` (+pydantic) |
| commit 3 | commit, содержащий этот файл (`checkpoint: cloud-to-local handoff`) | handoff + обновлённый ledger + `split_ocr_all.py` |

PR **не создавался**. Рабочая ветка по смыслу соответствует `research/worldspec-vnext`; отдельная
ветка с таким именем не создавалась, потому что cloud-задача назначила ветку `claude/practical-volta-l67oia`.

### PRIVATE `SUKUNA-AI/vkm-subsidence-forecasting_resourses`

| Что | Ветка / SHA | Статус |
|---|---|---|
| Исходный `main` | `482bec01767d4888fe88ff72dcef7375e7f552e9` | не изменялся |
| Рабочая ветка (remote) | `claude/practical-volta-l67oia` = checkpoint-commit поверх `482bec0` | запушена: OCR-текст (обычный git, не LFS), OCR-manifests, копия этого handoff |
| Локальный commit `292655a` | OCR в LFS-zip `ocr_VKM-SRC-025_037_tesseract_rus_300dpi.zip` | **НЕ запушен**: сетевая политика cloud-окружения блокирует `lfs.github.com` (403). Остаётся только в VM и пропадёт вместе с ней. Его содержимое продублировано в запушенном checkpoint без TSV-файлов с пословной уверенностью; сводка уверенности по страницам есть в manifests |

Remote-ветки PRIVATE, существовавшие до run: `main`, `claude/eloquent-goodall-ka7n0u` (`3a5761a`),
`claude/eloquent-goodall-ka7n0u-integration` (= `482bec0`). Их не трогали.

---

## 2. Что уже выполнено

1. **Preflight** (`docs/reset_2026_09/PREFLIGHT_REPORT_RU.md`): SHA, clean state, ветки, LFS,
   frozen manifests. Canonical verifier = **PASS**, но только после `git lfs pull`: без него
   выдаётся ложный FAIL по `*.csv.gz`. Tests до reset: **95 passed / 3 failed / 60 errors / 1 skipped**;
   ошибки дают тесты старого стека, которым нужны retired v3.x данные.
2. **Ветка `legacy`** = `d54025d` создана и запушена до любых изменений.
3. **Toolkit** в VM (раздел 9).
4. **Git LFS** materialized в обоих репозиториях (PRIVATE 52 объекта / 470 МБ; PUBLIC 17 объектов).
5. **Постраничное извлечение text layer** для всех зарегистрированных PDF и docx
   (сводка: `docs/reset_2026_09/run_kit/corpus_extraction_summary.json`).
6. **Полный OCR** VKM-SRC-025 (198 стр.) и VKM-SRC-037 (236 DjVu-разворотов): раздел 4.
7. **VKM-SRC-023** (docx Филатовой) отрендерен LibreOffice в `filatova.pdf` (115 стр.) для
   постраничных локаторов и визуальной проверки рисунков; 4 EMF → PNG.
8. **Протоколы и план**: `SWEEP_PROTOCOL.md` (единая схема записей: 22 вида записей, статусы
   FACT/DERIVATION/INTERPOLATION/MODEL_CHOICE/ENGINEERING_ASSUMPTION/ANALOGUE/UNKNOWN,
   spatial/temporal support, visual checks, citations), `MATH_PROTOCOL.md`, `chunk_plan.json`
   (65 чтений), инструменты render/grep/merge/extract/digest, скрипты workflow.
9. **Ядро нового пакета** `src/vkm_world/core/provenance.py`: статусы, scope/scale, SpatialLevel
   (иерархия ВКМ → … → репер), `TemporalSupport` с семантикой доступности информации
   (`available_from`, `usable_at(origin)`), неопределённость, явный `Transfer` для LAB→MASSIF и
   аналог→СКРУ-1. Правила «UNKNOWN stays UNKNOWN», «TEACHING_EXAMPLE ≠ FACT» и «ANALOGUE ≠ SKRU1»
   проверяются тестами `tests/world/test_provenance.py` (**10 passed**).
10. **Журнал run**: `docs/reset_2026_09/NIGHT_RUN_LEDGER_RU.md`.

---

## 3. Корпус: 39 документов / 65 чтений

**Завершено чтений: 0 из 65.** Ни один chunk-агент не записал `records.jsonl`/`coverage.json`.

В предыдущей консолидации (`10_physics_evidence/physical_evidence_v1`, 3019 записей) все эти
документы уже обрабатывались с разной глубиной. Новый повторный sweep, который требует
исходная задача (раздел 6: «ни один источник не остаётся UNSEEN»), **не выполнен ни для одного
документа**.

### 3.1 Чтения, остановленные в процессе (10)

Частичная работа осталась только в transcript'ах агентов в VM; в репозитории её нет, для
продолжения она непригодна.

| Группа | Чтение | Документ | Прогресс до остановки |
|---|---|---|---|
| S1 | VKM-SRC-011 c1 (стр. 1–24), c2 (25–48) | Кудряшов 2013 | ~364/376 шагов, 44 страницы отрендерены |
| S2 | VKM-SRC-012 c1 (1–33), c2 (34–66) | Лебедева 2023 | ~251/271 шаг, 18 страниц отрендерено |
| S3 | VKM-SRC-014 c1 (1–55), c2 (56–110) | Соловьёв–Секунцов 2013 | ~288/461 шаг, 54 страницы |
| S4 | VKM-SRC-037 c1 (разв. 1–30), c2 (31–60) | Методические указания ВКМКС 1992 | ~417/448 шагов, 115 разворотов |
| audit | public-repo, legacy-pw-ogs | — | остановлены, выходов нет |

### 3.2 Не запускались (55 чтений)

- **S1** (16): VKM-SRC-011 c3–c8; 018+030+019 (g6); 004+017 (g1); 029 c1–c3; 024 c1–c3; 006 c1–c2.
- **S2** (13): VKM-SRC-012 c3–c4; 023 c1–c3; 025 c1–c6; 005+031+032+033 (g2); 036 (g8).
- **S3** (13): VKM-SRC-014 c3–c5; 026; 027+034+038+039 (g7); 016+035+021 (g5); 020 c1–c3; 028 c1–c4.
- **S4** (15): VKM-SRC-037 c3–c8; 001+002 (g0); 003 c1–c3; 040 c1–c3; 015+041 (g4); 007+008+009+010 (g3).

### 3.3 Источники, ещё не просмотренные в этом run

Все 39 документов (VKM-SRC-001…041, кроме 013 и 022) ждут полного повторного sweep:

| ID | Документ | Стр. | Text layer |
|---|---|---|---|
| 001 | Мусихин 2012, автореферат | 22 | есть (шумный скан) |
| 002 | Мусихин, слайды InSAR | 17 | 13/17 |
| 003 | Бабаянц, дисс. SAR | 126 | 122 |
| 004 | Бабаянц и др. 2023, PS InSAR | 19 | есть |
| 005 | Губанова–Глебова 2022 | 6 | есть |
| 006 | Гусев, спутниковый мониторинг | 94 | есть |
| 007 | Ширшова 2020 | 10 | есть |
| 008 | Шокин и др. 2016 | 11 | есть |
| 009 | Горский–Хоменков 2012 (Старобин) | 11 | есть |
| 010 | Миков–Потапов 2024 | 10 | есть |
| 011 | Кудряшов 2013 (монография) | 186 | 182 |
| 012 | Лебедева 2023 (дисс.) | 131 | 129 |
| 014 | Соловьёв–Секунцов 2013 | 265 | есть (много таблиц картинками) |
| 015 | Ворошилов и др. 2026 GPR | 15 | есть |
| 016 | Беляков–Беликов 2022 | 14 | есть |
| 017 | Гусев–Эпин–Цветков 2022 | 10 | есть |
| 018 | Копылов, геодинамические зоны | 8 | есть |
| 019 | Аникин 2019 | 5 | есть |
| 020 | Уралкали 1998 ч.1 | 273 | 267 |
| 021 | Евсеев–Жукова 2024 | 5 | есть |
| 023 | Филатова, ВКР (docx → 115 стр.) | 115 | docx |
| 024 | Бобровицкий, ВКР (аналог) | 158 | 157 |
| 025 | Барях–Асанов–Паньков (скан) | 198 | **нет → OCR сделан** |
| 026 | Нестерова 2018 | 42 | есть |
| 027 | Токсаров–Костин 2017 | 7 | есть |
| 028 | Уралкали 1999 ч.2 | 426 | 403 |
| 029 | Бельтюков 2019 (дисс.) | 159 | 158 |
| 030 | Токсаров 2020 | 6 | есть |
| 031 | Глебова 2022 | 5 | есть |
| 032 | Ломакин 2022 | 6 | есть |
| 033 | Барях–Ломакин 2011 | 5 | есть (cp1251-артефакты) |
| 034 | Евсеев–Васильева 2023 | 5 | есть |
| 035 | Барях–Самоделкина 2011 | 5 | есть (cp1251-артефакты) |
| 036 | Барях–Цаюков 2024 | 18 | есть |
| 037 | Методические указания ВКМКС 1992 (DjVu) | 236 разв. | **нет → OCR сделан** |
| 038 | Самоделкина–Барях 2009 | 6 | есть |
| 039 | Лобанов–Федосеев 2022 | 5 | есть |
| 040 | Жуков 2017 (дисс. GPR) | 154 | есть |
| 041 | Ковин–Жуков–Ворошилов 2017 | 4 | есть |

Особые записи: **VKM-SRC-013** (исходный ZIP удалён после сборки PDF; рабочая копия — 025) и
**VKM-SRC-022** (`SKRU1_ACTUAL_DATA_TABLES_v1.zip`, LEGACY_RETIRED, намеренно отсутствует).
В `SOURCE_COVERAGE_MASTER` они должны получить явные статусы blocker/retired, а не UNSEEN.
Derived excerpt Жукова (`00_registry/intake/.../derived/Zhukov_GPR_VKM_ch4-5_pages117-147.pdf`) и
байтовые копии в `08_data_archives/main_repo_snapshots` — дубликаты, отдельного чтения не требуют.

---

## 4. Состояние OCR

| Источник | Объём | Движок | Результат |
|---|---|---|---|
| VKM-SRC-025 Барях–Асанов–Паньков | 198 стр. | tesseract 5.3.4 `rus`, 300 dpi, psm 3, рендер PyMuPDF | готово |
| VKM-SRC-037 Методические указания 1992 | 236 DjVu-разворотов (2 печатные стр. на разворот) | tesseract 5.3.4 `rus`, 300 dpi, рендер ddjvu | готово; **37 разворотов со средней уверенностью < 75**: 14, 16, 18, 20, 26, 38, 39, 40, 49, 53, 58, 61, 65, 67, 68, 70, 84, 92, 101, 106, 110, 111, 127, 139, 146, 170, 178, 181, 183, 185, 192, 196, 201, 212, 215, 231, 232 |

- Скрипт: PRIVATE `10_physics_evidence/physical_evidence_v1/scripts/ocr_source_pages.py`
  (поддерживает resume).
- Сохранено в PRIVATE (запушено): `00_registry/cloud_checkpoint_2026-09-26/ocr_all_VKM-SRC-025.txt`,
  `ocr_all_VKM-SRC-037.txt` (с маркерами `===== PDF_PAGE N (OCR) =====`) и `ocr_manifest_*.json`
  (постраничная статистика уверенности). Разрезать на страницы:
  `python docs/reset_2026_09/run_kit/tools/split_ocr_all.py <all.txt> <RES>/work/ocr/<SID>/`.
- Пословные TSV лежат только в VM и в незапушенном LFS-zip. Локально при необходимости
  OCR можно перезапустить: около 30 мин на 4 CPU.
- OCR **не является источником истины**: критичные числа проверяются по цепочке
  text → OCR → рендер страницы → контекст.

---

## 5. Состояние `MATH_PROTOCOL.md`

- Файл готов: `docs/reset_2026_09/run_kit/MATH_PROTOCOL.md`. В нём заданы источники evidence,
  владение файлами кода, формат `MATHEMATICAL_MODEL_REGISTRY` (23 колонки, словари `math_class`,
  `origin`, `execution_status`, `status`), обязательные pint-тесты размерности и формат
  русских нарративов.
- Math wave 1 состоит из 7 потоков (скрипт `workflows/math_foundation_wave1.js`):
  - **MECH** (поля/тензоры/weak form/линейная алгебра) — остановлен в процессе, выходов нет;
  - **STRESS** (гравитация, σv, K0, размерности, `core/units.py`) — остановлен в процессе, выходов нет;
  - **RHEO** (реология) — **ложно завершился без работы** (см. раздел 7, п. 3);
  - **SUBS**, **OBS**, **GEOM**, **PHYS** — не запускались.
- `MATHEMATICAL_MODEL_REGISTRY.csv`, `physics_coverage_and_execution_matrix.csv`, causal graph:
  **строк 0, не созданы**.

---

## 6. Агенты и workflows

| Workflow (run id) | Назначение | Агентов в плане | Итог |
|---|---|---|---|
| `wf_a42b5b6b-b72` | sweep S1 геология/регион | 18 | 2 в процессе → остановлены; 16 не запускались |
| `wf_b8844679-138` | sweep S2 СКРУ-1/механика | 15 | 2 → остановлены; 13 не запускались |
| `wf_e1d031b9-220` | sweep S3 технология добычи | 15 | 2 → остановлены; 13 не запускались |
| `wf_f11b2b1a-754` | sweep S4 мониторинг/геофизика/норматив | 17 | 2 → остановлены; 15 не запускались |
| `wf_2cfc3078-43c` | repo audit (A public, B legacy PW/OGS, C reproducibility) | 3 | A, B остановлены; C не запускался |
| `wf_6a754dd3-812` | math wave 1 | 7 | RHEO завершён пусто; MECH, STRESS остановлены; 4 не запускались |

Все 6 workflow остановлены по указанию пользователя (`TaskStop`). Завершённых агентов с
полезным результатом нет. Resume по run id локально невозможен (он работает только внутри той же
сессии): workflow запускаются заново из `run_kit/workflows/*.js`.

---

## 7. Найденные ошибки и противоречия

### Технические / воспроизводимость

1. **Ложный FAIL canonical verifier без `git lfs pull`.** PUBLIC хранит `*.csv.gz` frozen v2/v2.1 в
   LFS; без materialization verifier сравнивает хеши pointer-файлов. В README это не оговорено.
2. **Старый main не зелёный без retired-данных:** 3 failed + 60 errors (split/target contracts,
   Gate B0–B3) после retirement v3.x 25.09.2026. Детально не разобрано: audit-агент остановлен.
3. **Harness пересылает последнее сообщение пользователя в workflow-агентов как «единственный
   голос пользователя».** Вопрос о сроках, заданный посреди run, привёл к тому, что агент RHEO отказался от
   задачи. Меры: запускать workflow новым явным запросом или добавлять в prompt
   `run_kit/CONTEXT_PREAMBLE.md` (на локальной машине адаптировать текст).
4. **Сетевая политика cloud-окружения блокирует `lfs.github.com`** (403). Новые LFS-объекты
   (zip, pdf, png по `.gitattributes` PRIVATE) из VM не пушатся.
5. Фоновый процесс, запущенный через `&` внутри background-команды, был убит на 42-й странице OCR.
   Перезапуск в foreground-режиме background-задачи прошёл успешно (OCR resumable).
6. У VKM-SRC-025 text layer пуст. Инструмент `grep_vocab.py` исправлен: он выбирает более полный
   из двух текстов (text layer / OCR).
7. Часы VM «прыгнули»: OCR-manifests датированы 25.09 ~23:51–23:55 UTC, коммиты — 26.09
   ~09:40 UTC (контейнер был приостановлен/возобновлён). На данные это не влияет.

### Научные

**Новых научных противоречий в этом run не установлено**, потому что sweep не дал выходов. Ранее
зафиксированные в `SOURCE_REGISTER.csv` проблемы в этом run **не перепроверялись**:

- VKM-SRC-021: исходное имя файла ошибочное (это не книга Борзаковского–Папулова 1994);
- VKM-SRC-031 PDF p.5 ≡ VKM-SRC-005 p.1; VKM-SRC-032 p.1 ≡ VKM-SRC-005 p.6 (перекрытие страниц);
- VKM-SRC-029: год защиты — 2018 или 2019 (расхождение ссылок); λ = 0.6 (БКПРУ-2, своё), λ = 0.71
  (цитата Токсаров–Асанов), 0.9–1.0 (Гремячинское, не ВКМ). Это конфликт гипотез K0, а не одно
  значение;
- VKM-SRC-033/035/038: cp1251-артефакты text layer;
- VKM-SRC-002: слайды — discovery-only до нахождения первичной публикации профильных линий;
- старый Physical World v1 опирался на скважину 75 как на model choice reduced-case. По
  установке reset это **не** геологическая истина СКРУ-1; полная трассировка не выполнена
  (audit-агент B остановлен).

---

## 8. Созданные артефакты

**PUBLIC (запушено):**

- `docs/reset_2026_09/PREFLIGHT_REPORT_RU.md`, `NIGHT_RUN_LEDGER_RU.md`;
- `docs/reset_2026_09/run_kit/`: `SWEEP_PROTOCOL.md`, `MATH_PROTOCOL.md`, `CONTEXT_PREAMBLE.md`,
  `chunk_plan.json`, `localize_paths.sh`, `venv_research_freeze.txt`, `preflight_public_pytest.txt`,
  `preflight_verify_canonical.json`, `corpus_extraction_summary.json`,
  `tools/{render_page,grep_vocab,merge_sweep,extract_corpus_text,build_existing_digests,split_ocr_all}.py`,
  `workflows/{corpus_sweep,repo_audit,math_foundation_wave1}.js`;
- `src/vkm_world/__init__.py`, `src/vkm_world/core/{__init__,provenance}.py`;
- `tests/world/test_provenance.py`;
- `CLOUD_TO_LOCAL_HANDOFF_RU.md` (этот файл).

**PRIVATE (запушено):** `00_registry/cloud_checkpoint_2026-09-26/` — OCR all.txt ×2,
ocr_manifest ×2, копия handoff.

**Только в VM (не в Git; регенерируемо):** `/home/user/work/corpus/**` (постраничный текст),
`filatova.pdf` + конвертированные EMF, 240 отрендеренных PNG (`/home/user/work/run/img`),
per-source дайджесты старых claims (`/home/user/work/run/existing`), OCR TSV,
transcript'ы остановленных агентов, `.venv-research`.

---

## 9. Пакеты в VM

**apt:** git-lfs 3.4.1, poppler-utils 24.02.0, djvulibre-bin, tesseract-ocr 5.3.4 + tesseract-ocr-rus,
pandoc, LibreOffice 24.2.7.2 (writer/draw, headless), libosmesa6, libegl1, libgl1, xvfb.

**Python 3.13.12, `.venv-research` (uv 0.8.17):** numpy 2.5.3, scipy 1.18.1, pandas 3.0.6, sympy 1.14.0,
mpmath 1.3.0, pint 0.26.1, numba 0.67.0, scikit-learn 1.9.1, statsmodels 0.15.0, SALib 1.6.0,
pyproj 3.8.0, shapely 2.1.2, geopandas 1.1.4, xarray 2026.7.0, networkx 3.7, pydantic 2.13.5,
pyarrow 25.0.1, duckdb 1.5.5, PyMuPDF 1.28.2, pdfplumber 0.11.10, Pillow 12.3.0, matplotlib 3.11.2,
meshio 5.3.5, trimesh 5.1.0, gstools 1.7.0, PyKrige 1.7.3, pyvista 0.49.0 (vtk 9.7.0; off-screen
проверен), plotly 7.1.0, gempy 2026.0.3 (+gempy-engine 2026.0.3.post1, numpy backend),
python-docx 1.2.0, openpyxl 3.1.5, PyYAML 6.0.3, pytest 9.1.1.
Полный freeze (92 пакета): `docs/reset_2026_09/run_kit/venv_research_freeze.txt`.

OGS **не устанавливался и не запускался** (запрет исходной задачи).

---

## 10. Pending stages исходной UltraCode-задачи

| § | Этап | Статус |
|---|---|---|
| 5 | Git: SHA, legacy, рабочая ветка | **DONE** (PR — pending, §96) |
| 6 | `SOURCE_COVERAGE_MASTER.csv` | PENDING (план и инструменты готовы) |
| 7 | Полный research книг/пособий/диссертаций/статей | PENDING (0/65 чтений) |
| 8 | `SOURCE_CITATION_GRAPH.csv` | PENDING |
| 9–10 | `MINE_LIFECYCLE_AND_INFORMATION_FLOW_RU.md`, физический и информационный графы, availability semantics | PENDING (семантика доступности заложена в `TemporalSupport`) |
| 11 | Пространственная иерархия | PARTIAL (`SpatialLevel`) |
| 12 | `borehole_catalog.csv` | PENDING |
| 13 | Координаты, высшая геодезия, теория поля | PENDING (поток GEOM) |
| 14–17 | WorldSpec 3D+время, provenance/uncertainty | PARTIAL (ядро provenance) |
| 18–30 | Physical-process audit, `physics_coverage_and_execution_matrix.csv` | PENDING (поток PHYS) |
| 31–56 | Математический фундамент, registry, расчёты, геостатистика, обратные задачи, UQ, чувствительность | PENDING (потоки MECH/RHEO/STRESS/SUBS/OBS/GEOM) |
| 57–66 | Cloud toolkit | DONE в VM (локально переустановить) |
| 67 | Интеграция MATLAB/AutoCAD/OGS/gprMax/ParaView/QGIS (roadmap) | PENDING |
| 68–69 | OCR/visual/attribution audit, `visual_ocr_qa_ledger.csv` | PENDING (OCR готов) |
| 70–71 | Внешний интернет-research, проверка формул | PENDING |
| 72–74 | Диагностический 3D-мир, визуальные статусы, LOO CV | PENDING |
| 75–76 | `monitoring_observation_catalog.csv`, observation operators | PENDING |
| 77 | Разделение World / Process / Observation / Validation | DESIGN зафиксирован в docstring пакета; реализация PENDING |
| 78 | Инженерный аудит горных работ | PENDING |
| 79 | `CAUSAL_GRAPH_RU.md` + machine-readable граф | PENDING |
| 80 | Классификация старого PW/OGS | PENDING (агент остановлен) |
| 81 | Аудит PUBLIC repo | PENDING (агент остановлен) |
| 82–83 | Новая структура и foundation implementation | PARTIAL |
| 84 | Тесты | PARTIAL (provenance) |
| 85 | Reproducibility | PARTIAL (находки раздела 7) |
| 86 | Run ledger | DONE (ведётся) |
| 88–91 | Обновление README/CLAUDE.md/AGENTS.md/state в обоих репо; PROJECT_STATE | **PENDING — `CLAUDE.md`/`AGENTS.md` всё ещё описывают старую логику** |
| 92 | Обязательные научные deliverables | PENDING |
| 93–95 | Финальный отчёт и постановка следующего OGS-этапа | PENDING |
| 96 | Commits/branches/PR | PARTIAL |
| 97 | Критический self-review | PENDING |

---

## 11. Точные команды для продолжения на workstation

Рекомендуется Linux или WSL2 (bash, apt-инструменты). На Windows без WSL нужны Git Bash и
Windows-сборки tesseract (+rus), poppler, djvulibre, pandoc, LibreOffice.

```bash
# 0. Корень работы (пример)
export ROOT=$HOME/vkm && mkdir -p $ROOT && cd $ROOT
export PUB=$ROOT/vkm-subsidence-forecasting RES=$ROOT/vkm-subsidence-forecasting_resourses
export WORK=$ROOT/work VENV=$ROOT/.venv-research

# 1. Репозитории и ветки
git clone https://github.com/SUKUNA-AI/vkm-subsidence-forecasting.git $PUB
git clone https://github.com/SUKUNA-AI/vkm-subsidence-forecasting_resourses.git $RES
git -C $PUB checkout main   # рабочая ветка влита в main (claude/practical-volta-l67oia = тот же снимок)
git -C $RES checkout main
git -C $PUB lfs install --local && git -C $PUB lfs pull
git -C $RES lfs install --local && git -C $RES lfs pull

# 2. Системные инструменты (Ubuntu/WSL)
sudo apt-get install -y git-lfs poppler-utils djvulibre-bin tesseract-ocr tesseract-ocr-rus pandoc \
  libreoffice-writer-nogui libreoffice-draw-nogui libosmesa6 libegl1

# 3. Python-окружение
uv venv $VENV --python 3.13 && . $VENV/bin/activate
uv pip install -r $PUB/docs/reset_2026_09/run_kit/venv_research_freeze.txt

# 4. Проверки preflight
cd $PUB && VKM_RESOURCES_ROOT=$RES python scripts/verify_canonical_repository.py | head -3
python -m pytest -q tests/world            # ожидается 10 passed

# 5. Восстановить OCR из checkpoint (или перезапустить ocr_source_pages.py)
for s in 025 037; do python $PUB/docs/reset_2026_09/run_kit/tools/split_ocr_all.py \
  $RES/00_registry/cloud_checkpoint_2026-09-26/ocr_all_VKM-SRC-$s.txt $RES/work/ocr/VKM-SRC-$s; done
#   альтернатива: python $RES/10_physics_evidence/physical_evidence_v1/scripts/ocr_source_pages.py VKM-SRC-025 --dpi 300 --jobs 8

# 6. Постраничный текст, Филатова, дайджесты старых claims
python $PUB/docs/reset_2026_09/run_kit/tools/extract_corpus_text.py $RES $WORK/corpus
soffice --headless --convert-to pdf --outdir $WORK/corpus/VKM-SRC-023 $RES/01_primary_sources/Filatova_VKR_SKRU1.docx
mv $WORK/corpus/VKM-SRC-023/Filatova_VKR_SKRU1.pdf $WORK/corpus/VKM-SRC-023/filatova.pdf
python - <<'EOF'
import pymupdf, os
d = os.environ['WORK'] + '/corpus/VKM-SRC-023'
for i, p in enumerate(pymupdf.open(d + '/filatova.pdf'), 1):
    open(f'{d}/p{i:04d}.txt', 'w', encoding='utf-8').write(p.get_text())
EOF
for e in $WORK/corpus/VKM-SRC-023/media/media/*.emf; do soffice --headless --convert-to png --outdir $(dirname $e) $e; done
python $PUB/docs/reset_2026_09/run_kit/tools/build_existing_digests.py $RES $WORK/run/existing
mkdir -p $WORK/run/{sweep,img,audit,math/figs,math/out,merged}

# 7. Переписать облачные пути run kit на локальные (правит файлы run kit в рабочем дереве)
cd $PUB/docs/reset_2026_09/run_kit && PUB=$PUB RES=$RES WORK=$WORK VENV=$VENV bash localize_paths.sh
cp SWEEP_PROTOCOL.md MATH_PROTOCOL.md $WORK/run/ && cp -r tools $WORK/run/

# 8. args для одного общего sweep-workflow (все 65 чтений)
python - <<'EOF' > $WORK/run/sweep_args_all.json
import json
d = json.load(open('chunk_plan.json', encoding='utf-8'))
print(json.dumps({'group': 'ALL', 'chunks': [c for g in d['groups'].values() for c in g]}, ensure_ascii=False))
EOF
```

**Запуск в Claude Code на workstation** (новая сессия в `$PUB`). Первым сообщением дать явный
запрос продолжить reset по этому handoff (или повторить исходный текст ночной задачи). Затем
запустить workflow:

- sweep: `Workflow({scriptPath: "docs/reset_2026_09/run_kit/workflows/corpus_sweep.js", args: <содержимое $WORK/run/sweep_args_all.json>})`.
  Лимит параллельности равен `min(16, CPU − 2)` агентов на workflow: на 18+ CPU достаточно одного
  workflow на все 65 чтений, на меньшем числе CPU запускайте 4 группы из `chunk_plan.json` параллельно;
- audit: `Workflow({scriptPath: "docs/reset_2026_09/run_kit/workflows/repo_audit.js"})`;
- math wave 1: `Workflow({scriptPath: "docs/reset_2026_09/run_kit/workflows/math_foundation_wave1.js"})`;
- после sweep: `python $WORK/run/tools/merge_sweep.py` → `$WORK/run/merged/` (сводка по видам
  записей и детерминированная проверка цитат по text/OCR-слою).

Если посреди run пользователь задаст вопрос, у агентов, запущенных после него, добавьте в начало
prompt текст из `CONTEXT_PREAMBLE.md` (см. раздел 7, п. 3).

---

## 12. Blockers

1. **Научный:** sweep 0/65 → нет evidence для синтеза (coverage master, скважины, citation graph,
   OCR ledger), расчётов и WorldSpec. Это главный блокер и первый локальный этап.
2. **Инфраструктура cloud:** `lfs.github.com` заблокирован сетевой политикой окружения. На
   workstation это не мешает.
3. **Документы-инструкции:** `CLAUDE.md`/`AGENTS.md`/README обоих репозиториев ещё описывают старую
   цепочку «PW v1 → OGS reference case». До их обновления (§88–91) каждая новая сессия должна
   получать явный запрос на reset, иначе агент вернётся к старой логике.
4. **Корпус:** VKM-SRC-013 (ZIP удалён; провенанс сохранён), VKM-SRC-022 (retired, отсутствует
   намеренно). VKM-SRC-037: 37 разворотов OCR низкого качества требуют визуальной проверки.
5. **Старые тесты** красные без retired-данных; решение (перенос в legacy или починка) — часть §81.

---

## 13. Следующий этап, который нужно запустить локально

**Stage 1 = полный повторный sweep корпуса (65 чтений) + audit (3 агента) + math wave 1 (7 потоков)**,
одновременно, по run kit из раздела 11. Затем, не раньше:

- **Stage 2 — центральный синтез:** `merge_sweep.py` → `SOURCE_COVERAGE_MASTER.csv`,
  `SOURCE_CITATION_GRAPH.csv`, `borehole_catalog.csv`, `visual_ocr_qa_ledger.csv`,
  `monitoring_observation_catalog.csv`, хронология, иерархия, `MINE_LIFECYCLE_AND_INFORMATION_FLOW_RU.md`;
- **Stage 3** — адресный внешний research по найденным gaps и проверка формул;
- **Stage 4** — math wave 2: расчёты на реальных данных (стратиграфия скважин, LOO CV, σv/K0,
  геометрия и закладка, методы оседания, чувствительность);
- **Stage 5** — WorldSpec vNext (typed schema поверх `vkm_world.core.provenance`) и диагностический
  3D-мир;
- **Stage 6** — reset `main` (legacy уже сохранена), обновление документов обоих репозиториев,
  адверсариальный self-review, финальный отчёт, PR.

Новый OGS-этап — отдельная задача после Stage 6 (исходная задача §95).

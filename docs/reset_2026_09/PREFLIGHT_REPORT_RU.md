# Preflight-отчёт ночного scientific reset (26.09.2026)

Фиксация состояния **до** любых изменений архитектуры. Все команды выполнены в
cloud VM (Linux, 4 CPU, 15 ГБ RAM, Python 3.13.12).

## 1. Исходные SHA

| Репозиторий | Ветка | SHA | Состояние дерева |
|---|---|---|---|
| PUBLIC `SUKUNA-AI/vkm-subsidence-forecasting` | `main` | `d54025d4c47b79b864076a33a4ecf877174ec922` | clean |
| PUBLIC | рабочая `claude/practical-volta-l67oia` | = `d54025d` на старте | clean |
| PRIVATE `SUKUNA-AI/vkm-subsidence-forecasting_resourses` | `main` | `482bec01767d4888fe88ff72dcef7375e7f552e9` | clean |
| PRIVATE | рабочая `claude/practical-volta-l67oia` | = `482bec0` на старте | clean |

Remote-ветки PRIVATE на старте: `main`, `claude/eloquent-goodall-ka7n0u` (`3a5761a`),
`claude/eloquent-goodall-ka7n0u-integration` (= `482bec0`).
Remote-ветки PUBLIC на старте: только `main`.

## 2. Ветка `legacy`

Создана в PUBLIC **до начала reset** прямо от `main` HEAD:

```
git branch legacy d54025d4c47b79b864076a33a4ecf877174ec922
git push -u origin legacy      # -> refs/heads/legacy = d54025d
```

`legacy` — замороженное состояние старой архитектуры (v2.1 stress lab, Gate A/B/C,
reconstruction, документы Physical Evidence / Physical World v1). Не force-push,
history не переписывается.

Рабочая ветка reset: `claude/practical-volta-l67oia` (назначенная ветка cloud-задачи;
по смыслу это `research/worldspec-vnext`). Разрушительные изменения в `main`
напрямую не выполнялись.

## 3. Git LFS

Оба репозитория используют Git LFS. В VM не было `git-lfs`; установлен
`git-lfs 3.4.1` (apt), затем `git lfs pull`:

- PRIVATE: 52 LFS-объекта, рабочее дерево 470 МБ; SHA-256 всех зарегистрированных
  источников сверяется в reproducibility-аудите;
- PUBLIC: 17 LFS-объектов (`*.gz` frozen v2/v2.1 и др.).

Важно: без `git lfs pull` canonical verifier PUBLIC выдаёт ложный FAIL по
`data/scenario_simulation_v2_1/*.csv.gz` (в дереве лежат LFS-pointer'ы).

## 4. Frozen artifacts и manifests

`scripts/verify_canonical_repository.py` (с `VKM_RESOURCES_ROOT=<клон PRIVATE>`)
после `git lfs pull`: **PASS**; 197 файлов main и 12 файлов private проверены по SHA,
7 manifests, 51 Markdown-файл; retired-ссылки v3.x пропущены как historical;
`models_executed = 0`, `evaluator_truth_parsed = false`.

Pinned hashes:

- v2.1 manifest `a268cd78…2c95`;
- constraints `96b88b61…de6ef`;
- representation `0e5501cb…6f549556ac428521f5a6d`.

## 5. Tests PUBLIC (до reset)

`python -m pytest -q` (venv `.venv-research`, numpy 2.5.3, pandas 3.0.6):

**95 passed, 3 failed, 60 errors, 1 skipped** (после `git lfs pull`; до него —
66 passed, 7 failed, 85 errors).

Ошибки/падения относятся к тестам старого forecasting-стека (split/target contracts,
Gate B0–B3), которые требуют retired v3.x reconstructed/model-ready данных
(`SKRU1_ACTUAL_DATA_TABLES_v1/**`, `inputs/bootstrap/**`), удалённых решением
LEGACY_DATA_RETIREMENT 25.09.2026. То есть старый main уже не был полностью
зелёным без retired-данных. Подробный разбор — в repository audit.

## 6. Private corpus preflight

- `00_registry/SOURCE_REGISTER.csv`: 41 запись (VKM-SRC-001…041).
- Реально присутствуют 39 уникальных документов-источников (+ derived excerpt Жукова,
  + байтовые дубликаты в `08_data_archives/main_repo_snapshots`).
- `VKM-SRC-013` — исходный ZIP Баряха–Асанова–Панькова удалён после сборки PDF
  (`ORIGINAL_ARCHIVE_DELETED_AFTER_PDF_ASSEMBLY`); рабочая копия — `VKM-SRC-025`.
- `VKM-SRC-022` — `SKRU1_ACTUAL_DATA_TABLES_v1.zip`, `LEGACY_RETIRED`, в дереве отсутствует
  намеренно.
- Без text layer: `VKM-SRC-025` (198 стр., скан) и `VKM-SRC-037` (DjVu, 236 разворотов).
  Полный OCR выполнен в VM: tesseract 5.3.4 `rus`, 300 dpi (скрипт
  `10_physics_evidence/physical_evidence_v1/scripts/ocr_source_pages.py`, вывод в
  git-ignored `work/ocr/`). VKM-SRC-037: 37 разворотов со средней уверенностью < 75.
- `VKM-SRC-023` (docx Филатовой) дополнительно отрендерен LibreOffice в PDF (115 стр.)
  для постраничных локаторов и визуальной проверки рисунков; 4 EMF-рисунка
  сконвертированы в PNG.
- Существующий evidence-релиз `10_physics_evidence/physical_evidence_v1`: 3019 записей
  claims, 2163 параметра, 22 закона ползучести, CASE 00–22 OGS.

## 7. Cloud toolkit (`<cloud research venv>`, не коммитится)

Команда: `uv venv .venv-research --python /usr/bin/python3.13` +
`uv pip install …` (полный freeze — в финальном отчёте, раздел 35).

Ключевые версии: numpy 2.5.3, scipy 1.18.1, pandas 3.0.6, sympy 1.14.0, mpmath 1.3.0,
pint 0.26.1, numba 0.67.0, scikit-learn 1.9.1, statsmodels 0.15.0, SALib 1.6.0,
pyproj 3.8.0, shapely 2.1.2, geopandas 1.1.4, xarray 2026.7.0, networkx 3.7,
pydantic 2.13.5, pyarrow 25.0.1, duckdb 1.5.5, PyMuPDF 1.28.2, pdfplumber 0.11.10,
Pillow 12.3.0, matplotlib 3.11.2, meshio 5.3.5, trimesh 5.1.0, gstools 1.7.0,
PyKrige 1.7.3, pyvista 0.49.0 (vtk 9.7.0, off-screen OSMesa/EGL — smoke test OK),
plotly 7.1.0, gempy 2026.0.3 (numpy backend), python-docx 1.2.0, pytest 9.1.1.

Системные пакеты (apt): git-lfs 3.4.1, poppler-utils 24.02, djvulibre-bin,
tesseract-ocr 5.3.4 + rus, pandoc, LibreOffice 24.2 (writer/draw, headless),
libosmesa6/libegl1 (off-screen VTK).

OGS в этом run **не устанавливался и не запускался** (запрет задачи).

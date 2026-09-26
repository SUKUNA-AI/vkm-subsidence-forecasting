<!-- Аудит Phase 1 (cloud, 26.09.2026), выполнен до reset на дереве старого main. Пути cloud VM заменены логическими именами. Статус исправлений — REPRODUCIBILITY_REPORT_RU.md. -->
# Аудит воспроизводимости PUBLIC и PRIVATE (Task C)

Дата: 26.09.2026. Аудит выполнен по состоянию `d95344e` (PUBLIC) и `9e4bf11` (PRIVATE). За время аудита
координатор добавил коммиты `2d8d43a` и `b3e3737`; на выводы это не влияет. Режим: **только чтение**. Оба репозитория не изменялись. Все запуски шли в scratch-копиях
`git worktree add … HEAD` в `$VKM_WORK/run/audit/scratch_repro_*`, все worktree удалены в конце.
Solver runs, генерация датасетов и обучение моделей не выполнялись.

| | PUBLIC `vkm-subsidence-forecasting` | PRIVATE `vkm-subsidence-forecasting_resourses` |
|---|---|---|
| Ветка / HEAD | `research/evidence-worldspec-reset` = `d95344e` (main = `1c1f816`) | `research/evidence-worldspec-reset` = `9e4bf11` (= main) |
| Tracked files / LFS | 525 / 17 | 1445 / 52 (40 уникальных OID) |
| Окружение аудита | Python 3.13.12 (`.venv-research`), git 2.43.0, git-lfs 3.4.1 | то же + tesseract 5.3.4, ddjvu, pymupdf 1.28.2 |

Машиночитаемый список: `reproducibility_findings.csv` (36 строк: **4 HIGH, 16 MEDIUM, 16 LOW**;
PUBLIC 20, PRIVATE 12, BOTH 4). Ниже приведены сводка, доказательства и план исправлений.

---

## 1. Вердикт

1. **PUBLIC воспроизводим «по байтам» при двух условиях: выполнен `git lfs pull` и задан `VKM_RESOURCES_ROOT`.**
   Canonical verifier даёт PASS: проверены 197 файлов main и 12 файлов PRIVATE, 7 manifests, 61 Markdown-файл.
   Результат сохраняется и при Windows-checkout с `core.autocrlf=true`: hash-pinned файлы защищены атрибутами
   `-text`/`eol=lf`. Слабые места:
   - без LFS verifier даёт ложный FAIL и не подсказывает причину;
   - без PRIVATE он возвращает exit 0;
   - `verify_inputs.py` и bootstrap-скрипты падают всегда (16/16 FAIL) и оставляют в корне untracked-файл;
   - `build_public_catalogues.py` не запускается;
   - lock-файлы описывают старый Windows-стек и не содержат `pydantic`.
2. **PRIVATE `physical_evidence_v1` на чистом клоне не проходит собственный валидатор.** Committed
   `validation_report.json` = PASS, но получен на «грязном» дереве. Причина: `manifest.json` перечисляет 4 файла
   `.pytest_cache/*`, которых нет в Git (V20). На Windows (Git for Windows по умолчанию ставит `core.autocrlf=true`)
   валидатор выдаёт **1462 hard errors**: в `.gitattributes` нет политики EOL. Исправление `* -text` проверено
   симуляцией: после него остаются те же 4 ошибки, что и на Linux.
3. **Полная пересборка evidence-пайплайна с чистого клона не байт-стабильна.** Меняются 33 записи `claims.jsonl`,
   потому что web-capture EXT-SRC-004 лежит только в git-ignored `work/text`. Научного эффекта нет: эти записи
   withdrawn. Пайплайн при каждом запуске переписывает tracked `manifest.json` (timestamp, HEAD, версия python)
   и `validation_report.json`.
4. **LFS-выгрузка из cloud невозможна:** CONNECT `lfs.github.com` → 403, проверено сейчас. В VM есть локальная
   ветка PRIVATE `claude/practical-volta-l67oia` = `292655a` с LFS-объектом, которого нет на сервере.
5. **Провенанс частично держится на изменяемых ветках.** На старте аудита тегов не было ни в одном репозитории. По ходу
   аудита координатор создал в PUBLIC **локальные** теги (`legacy/final`, `frozen/*`, `archive/v3.2-last`). По журналу
   `2d8d43a` их push заблокирован (403), поэтому пока они существуют только в VM. В PRIVATE тегов нет. Версии
   `pw2d_generator.py`, по которым получены замороженные OGS cases 03–10, достижимы только из
   `origin/claude/eloquent-goodall-ka7n0u`, а не из `main`.
6. **Целостность корпуса в порядке.** 39 из 41 зарегистрированных источников совпадают по size и SHA-256,
   `git lfs fsck` OK. VKM-SRC-013 и 022 отсутствуют по решению. При этом 12 бинарников в
   `08_data_archives/main_repo_snapshots` — точные дубликаты канонических путей. Нужны они только из-за
   маппинга в PUBLIC verifier.

---

## 2. Методика (что запускалось)

| Проверка | Команда (кратко) | Результат |
|---|---|---|
| EOL индекса | `git ls-files --eol` | PUBLIC: 392 LF, 81 CRLF (все под `-text`), 34 binary, 17 LFS. PRIVATE: 1222 LF, **63 CRLF, 64 mixed**, 44 binary, 52 LFS |
| Атрибуты | `git check-attr`, сверка паттернов с `git ls-files` | PUBLIC: 23 path-паттерна не матчат ни одного файла; PRIVATE: нет ни одного text/eol-правила |
| PUBLIC verifier | `VKM_RESOURCES_ROOT=<scratch PRIVATE> python scripts/verify_canonical_repository.py` | **PASS**, exit 0; 14 retired-ссылок пропущены штатно |
| — без PRIVATE | то же без env | `PASS_CORE_EXTERNAL_UNCHECKED`, 26 внешних файлов не проверены, **exit 0** |
| — без LFS | worktree со skip-smudge | **FAIL**, 16 × `hash_or_size` по `*.csv.gz` |
| — Windows EOL | worktree с `core.autocrlf=true` | PASS; `tests/world` 29 passed; `localize_paths.sh` падает (`pipefail` + CRLF) |
| `verify_inputs.py` / `bootstrap_repo.sh` | `python scripts/verify_inputs.py --root .` | **FAIL 16/16**, exit 1, создаёт `?? INPUT_VERIFICATION.json` |
| Схема WorldSpec | `python scripts/export_worldspec_schema.py` | детерминирована: `git diff` пуст (pydantic 2.13.5) |
| Каталоги | `build_public_catalogues.py` | `FileNotFoundError scripts/public_catalogue_map.json` |
| Тесты PUBLIC | `pytest tests` в scratch | 3 failed / 124 passed / 1 skipped / 60 errors; **tracked-файлы не меняются** (пишут только в `work/tests`) |
| PRIVATE validator | `validate_physical_evidence.py --report <вне repo>` | **FAIL 4** (V20 `.pytest_cache/*`); с `--no-manifest-check` = PASS, 3 warnings |
| — Windows EOL | worktree с `core.autocrlf=true` | **FAIL 1462** (V23 × 241, V20, V26 × 12, V27 × 9) |
| — с исправлением | + attributesFile `* -text` | FAIL 4, те же ошибки, что на Linux → исправление работает |
| Пересборка PRIVATE | `extract_corpus_text` → OCR (025/037 восстановлены, 001 пересчитан) → 8 build-скриптов → validate → pytest | 116 passed; **изменены 3 tracked-файла** (`claims.jsonl`, `manifest.json`, `validation_report.json`) |
| LFS-целостность | sha256/size каждого `canonical_path` из `SOURCE_REGISTER.csv`; `git lfs fsck` | 39/41 OK, fsck OK |
| Дубликаты | группировка `git lfs ls-files -l` по OID | 12 пар snapshot ↔ canonical |
| Все manifests | универсальный сканер пар (path, sha256) во всех tracked JSON/CSV | см. §5 |
| Workflows PRIVATE | чтение YAML; шаг реестра прогнан на копии `SOURCE_REGISTER.csv` | переписывает реестр в CRLF (sha меняется) |
| LFS 403 | `curl https://lfs.github.com` через proxy; `git lfs push --dry-run` | 403; неотправленный объект `bd983e29…` |

Логи и промежуточные файлы лежат рядом, в `scratch_repro_*`: verifier JSON, выводы валидатора,
`scratch_repro_manifest_scan_{pub,res}.tsv`, `scratch_repro_lfs_integrity.tsv`, логи пайплайна.

---

## 3. Скрипты, меняющие tracked-файлы

| Репозиторий | Скрипт | Меняет tracked? | Доказательство / комментарий |
|---|---|---|---|
| PUBLIC | `scripts/verify_canonical_repository.py` | нет | пишет только `work/repo_cleanup/canonical_verification.json` (ignored) |
| PUBLIC | `scripts/verify_inputs.py` (+ `bootstrap_repo.sh/.ps1`) | **создаёт untracked** | `INPUT_VERIFICATION.json` в корне, файл не в `.gitignore` |
| PUBLIC | `docs/reset_2026_09/run_kit/localize_paths.sh` | **да, 8 файлов** | `sed -i` по run kit (by design, но оставляет грязное дерево) |
| PUBLIC | `scripts/export_worldspec_schema.py` | перезаписывает, diff пуст | детерминирован |
| PUBLIC | `scripts/build_public_catalogues.py` | не запускается | нет map-файла |
| PUBLIC | `scripts/capture_environment.py` (не запускался) | **да, по коду** | перезаписывает `artifacts/environment/*` |
| PUBLIC | `scripts/generate_scenario_v2_1.py` / `_v2.py` (не запускались) | **да, по коду** | по умолчанию `--output data/scenario_simulation_v2_1`, то есть frozen release |
| PUBLIC | `pytest tests` | нет | только `work/tests/**` |
| PRIVATE | `validate_physical_evidence.py` (по умолчанию) | **да** | пишет tracked `validation_report.json`; check-only возможен только с `--report` |
| PRIVATE | `run_pipeline.sh` / `build_manifest.py` | **да** | `manifest.json` меняется при каждом запуске (`generated_utc`, `git_head_at_generation`, `python`) |
| PRIVATE | `merge_streams.py` и др. | **да, при отсутствии capture** | 33 записи `claims.jsonl` |
| PRIVATE | `ogs/run_all.sh`, `cases/*/run.sh` (не запускались) | да, by design | receipts, logs, `ogs_test_registry.csv` |
| PRIVATE | 3 GitHub workflows (не запускались) | **да, и push в main** | `git add -A && git push origin main` |
| PRIVATE | `scripts/import_remaining_local_only.ps1` (не запускался) | **да, commit и push в main** | `git add -A`, `git push origin main` |

---

## 4. Line endings и `.gitattributes`

**PUBLIC.** Схема в целом правильная: `* text=auto`, `eol=lf` для py/md/csv/json/yaml, `-text -eol` для frozen-путей,
вложенные `.gitattributes` в `artifacts/`, `configs/`, `data/`, `docs/`. Все 81 CRLF-файла намеренные и hash-pinned
(receipts v2.1, `configs/scenario_*`, `data/reconstruction_research_v1/*`, `src/skru1/scenario_simulation_v2.py`).
Проблемы:

- Без `eol` остались `*.sh`, `*.js`, `*.toml`, `*.txt`, `.python-version`. На Windows `localize_paths.sh` ломается.
- 23 устаревших паттерна указывают на retired или удалённые пути: `SKRU1_ACTUAL_DATA_TABLES_v1/**`, `inputs/**`,
  `codex/**`, `repo_bootstrap/**`, v1, `*_freeze/**` и т. д.
- 31 PNG (10,4 МБ) — обычные blobs с `text=auto`. Сейчас это безопасно, потому что Git сам распознаёт binary.
  Три `artifacts/**/*.csv.gz` (2,2 МБ) лежат не в LFS, хотя `data/scenario_simulation_v2*/**/*.gz` — в LFS.

**PRIVATE.** EOL-политики нет вообще. Evidence CSV записаны `csv`-модулем с `\r\n`, отсюда 63 CRLF и 64 mixed файла.
При `autocrlf=true` Git переписывает LF-файлы в CRLF, и все хеши V20/V23/V26/V27 ломаются.

Предлагаемый патч для PRIVATE `.gitattributes` (первая строка файла):

```
# Preserve exact bytes of every hashed evidence/OGS file on every platform.
* -text
```

Предлагаемые добавления в PUBLIC `.gitattributes` (ни один из этих файлов не hash-pinned):

```
*.sh text eol=lf
*.js text eol=lf
*.toml text eol=lf
*.txt text eol=lf
.python-version text eol=lf
*.ps1 text eol=crlf
*.png binary
artifacts/**/*.gz -text -diff
```

---

## 5. Manifests: генератор и проверка по текущим байтам

| Manifest | Генератор | Статус по текущим байтам |
|---|---|---|
| `data/scenario_simulation_v2_1/manifest.json` (pinned) | `scripts/generate_scenario_v2_1.py` + `finalize_scenario_v2_1_erratum.py` | MATCH (через verifier); 1 retired-ссылка `survey_points.csv`, пропуск штатный |
| `artifacts/reconstruction/scenario_constraints_v2/manifest.json` (pinned) | `build_scenario_constraints_v2.py` | MATCH; 12 внешних файлов проверены через PRIVATE snapshots |
| `artifacts/splits/scenario_representation_v2_1/representation_manifest.json` (pinned) | `build_/finalize_scenario_representation_v2_1.py` | MATCH |
| `data/scenario_simulation_v2/manifest.json`, `musikhin_*`, `filatova_*`, `data/reconstruction_research_v1`, `scenario_constraints_v1` | соответствующие `reconstruct_*`, `repair_reconstruction_data.py`, `build_scenario_constraints.py` | MATCH по current-файлам; регенерация невозможна без retired SKRU1 (снято политикой 25.09) |
| Старые receipts (`scenario_representation_v2/acceptance_receipt.json`, `final_candidate_suite_v4.json`, `MUSIKHIN_LINE1_ACCEPTANCE_*.json`, `artifacts/inventory/*`, `gate_a0_report.json`) | старый стек | висячие ссылки: 15 файлов удалены в `f9e811b`, есть ссылки на `work/…`, `t1_b6_expanded_v1/*.joblib` |
| PRIVATE `physical_evidence_v1/manifest.json` | `scripts/build_manifest.py` | **FAIL 4** (`.pytest_cache`), см. §1 |
| PRIVATE `ogs/cases/*/receipt.json` | `cases/*/run.sh` | 15 ссылок на `ogs/tools/pw2d_generator.py`/`basin_metrics.py` не совпадают с текущими байтами. Исторические версии есть только в `origin/claude/eloquent-goodall-ka7n0u` |
| PRIVATE `ogs/figures/receipt_stage*.json`, `inventory_stage*.csv` | `ogs/figures/src` | 8 mismatch (`figlib/draw.py`, `figure_manifest.csv`, `research_decisions.csv` изменены позже); 192 ссылки на git-ignored `work/ogs/runs/**` |
| PRIVATE `00_registry/cloud_checkpoint_2026-09-26/SHA256SUMS.txt` | ручной checkpoint | `sha256sum -c`: 4/4 OK |
| PRIVATE `00_registry/intake/…/IMPORT_MANIFEST.csv`, `SHA256SUMS.txt` | intake | пути относятся к inbox, а не к repo; по SHA 11/12 записей есть в реестре, 1 помечена DO_NOT_REGISTER, но лежит в LFS |

Сводка сканера: PUBLIC — 1514 ссылок, 504 MATCH, 140 retired, 87 externalized, 764 missing (в основном внутренности
retired zip-пакетов), 19 mismatch (снимок R2 и README внутри zip). PRIVATE — 2945 ссылок, 2708 MATCH,
212 missing, 23 mismatch.

**Timestamps внутри хешируемых артефактов.** В 197 hash-pinned файлах PUBLIC ISO-timestamp'ов нет, frozen v2.1
детерминирован. В PRIVATE timestamps есть в `manifest.json` (сам себя не хеширует, но меняется при каждом прогоне),
а также в `reproducibility_receipt.json`, `text_extraction_receipts/*.json` и OGS receipts. Последние хешируются
в manifest, поэтому их обновление меняет manifest. Рекомендация: timestamp, HEAD и python хранить в отдельном
receipt, а не в manifest.

---

## 6. Git LFS

- **Целостность:** в `SOURCE_REGISTER.csv` 41 запись. Для 39 файл существует, LFS materialized, size и SHA-256
  совпадают; `git lfs fsck` OK. VKM-SRC-013 (`ORIGINAL_ARCHIVE_DELETED_AFTER_PDF_ASSEMBLY`) никогда не был закоммичен
  ни в один ref, поэтому его байты невосстановимы из Git. VKM-SRC-022 (retired zip, OID `edcced26…`) отсутствует
  в локальном LFS-store. Тезис «истории Git достаточно» верен, только пока объект хранится на GitHub LFS.
- **Дубликаты:** 12 файлов `08_data_archives/main_repo_snapshots/inputs_sources/**` имеют те же OID, что и канонические
  VKM-SRC-001…010, 023, 024. Кроме того, `geokniga-02obrabotka.pdf` = `Musikhin_VKM_radar_interferometry_slides.pdf`.
  Место в LFS-store не тратится, но дерево раздувается примерно на 68 МБ, и появляется второй «источник истины».
- **Последствия 403 на `lfs.github.com`** (проверено: CONNECT 403, запись proxy от 2026-09-26T11:28Z):
  1. Из cloud нельзя запушить ни один новый файл под LFS-паттерн. Push целиком отклоняется на pre-push, вместе
     с текстовыми коммитами.
  2. Обход через `--no-verify`/`GIT_LFS_SKIP_PUSH` опубликует pointer без объекта. Из-за
     `filter.lfs.required=true` checkout такого коммита сломается у всех.
  3. Всё бинарное, что создаётся в cloud (OCR TSV с пословной уверенностью, zip `292655a`, рендеры страниц), живёт
     только в VM. Локальную ветку PRIVATE `claude/practical-volta-l67oia` (`292655a` ≠ origin) пушить нельзя.
  4. Удаление LFS-файлов (например, snapshots) и коммит текстов из cloud возможны: upload для этого не нужен.
  5. Миграцию PNG/gz в LFS и добавление новых PDF в корпус делать только на workstation.

---

## 7. Окружения и версии

- PUBLIC: `pyproject` требует `>=3.13,<3.14`; `.python-version` = `3.13.13` (patch-pin), в cloud 3.13.12.
  `requirements/*.lock.txt` сняты на Windows 11 для Gate-стека, без хешей и без `pydantic`, хотя он runtime-зависимость
  `vkm_world`. Фактический research freeze лежит в `docs/reset_2026_09/run_kit/venv_research_freeze.txt`,
  и версии numpy/pandas там отличаются от lock-файлов.
- PRIVATE: lock для evidence-пайплайна отсутствует (pymupdf, PyYAML, pytest, системные tesseract 5.3.4 rus, ddjvu).
  Manifest записан на python 3.11.15. Для OGS есть `ogs/environment_freeze.txt` (ogs 6.5.9, ogstools 0.8.2).
- Предложение: единый `requirements/worldspec.lock.txt` с хешами (`uv pip compile --generate-hashes`, pydantic==2.13.5)
  в PUBLIC; `requirements/evidence.lock.txt` и список apt-пакетов с версиями в PRIVATE; `.python-version` = `3.13`.

## 8. Пути и платформенная зависимость

- PowerShell-only: инструкции в `README.md`, `README_FIRST.md`, `CANONICAL_RESEARCH_STATE_RU.md`,
  `REPOSITORY_CONSOLIDATION_*.md` (`$env:VKM_RESOURCES_ROOT = "E:\Диплом\…"`); PRIVATE `scripts/*.ps1`;
  PUBLIC `bootstrap_repo.ps1`; receipt erratum (`.venv/Scripts/python.exe`).
- В исполняемом коде PUBLIC абсолютных путей нет, кроме run kit (разрешённое исключение политики).
  В committed-логах есть `/home/user` (80 строк в `preflight_public_pytest.txt`, `preflight_verify_canonical.json`)
  и `C:\WINDOWS` (`artifacts/environment/environment.json`). В PRIVATE 222 строки `/home/user` в review-логах.
- Противоречие по раскладке: PRIVATE README советует `E:\Диплом\vkm-subsidence-forecasting_resourses\`, PUBLIC
  `PHYSICAL_WORLD_EVIDENCE_STATE_RU.md` советует не вкладывать туда private repo. `.gitignore` и verifier
  поддерживают обе схемы.
- `.venv-ogs/` и `.venv-research/` не игнорируются ни в одном репозитории. `CLAUDE.md` предлагает создавать
  `.venv-ogs` в корне, а PRIVATE-скрипты используют `git add -A`.

## 9. Ссылки на retired-пути и устаревшие конфиги

Current-дерево PUBLIC всё ещё содержит конфиги и скрипты со ссылками на `SKRU1_ACTUAL_DATA_TABLES_v1/**` и
`inputs/bootstrap/**`: `configs/gate_a1.yaml` (12 путей), `input_manifest.csv` (5), `reconstruction_parent_manifest.csv` (7),
`reconstruction_research_v1.json`, `scenario_constraints_v1.json`, `prepare_gate_a0.py`, `reconstruct_musikhin_line1.py`,
guard-списки в `src/skru1/*`. **Важно:** `configs/input_manifest.csv`, `configs/gate_a1.yaml` и `scripts/verify_inputs.py`
захешированы frozen-цепочкой v2.1 (через musikhin-manifest). Удалить или отредактировать их на месте нельзя:
verifier упадёт. Правильный путь — перенести всю frozen-цепочку в `legacy` при reset и ссылаться на неё по SHA.
В PRIVATE ссылки на retired-пути есть только в классификациях (`LEGACY_RETIRED`) и документации. Это корректно.

## 10. GitHub workflows PRIVATE

Все три (`canonicalize_imported_sources`, `fetch_public_physical_sources`, `fetch_extra_geokniga_sources`) —
одноразовые bootstrap-задачи. Их устройство:

- запуск при push в `main`, если изменён сам файл workflow;
- `permissions: contents: write`;
- `git add -A` и `git push origin main`;
- `checkout lfs: true`, то есть около 470 МБ LFS-трафика за прогон.

Функционально они устарели: исходных путей `01_primary_sources/main_repo/*` нет, `GITHUB_BOOTSTRAP_HASHES.csv` удалён.
Кроме того, они опасны: шаг реестра переписывает `SOURCE_REGISTER.csv` в CRLF с другим quoting
(sha `0d63373f…` → `9d7a6849…`). Проверяющего workflow нет. Рекомендация: удалить все три или перевести на
`workflow_dispatch` + `contents: read` + PR. Добавить read-only CI: SHA-проверка реестра и
`validate_physical_evidence.py --report` в artifact.

---

## 11. План исправлений (по приоритету)

**P0: можно сделать текстовыми коммитами, в том числе из cloud (LFS не нужен)**
1. PRIVATE `.gitattributes`: первой строкой `* -text` (RPR-002). Проверено симуляцией.
2. PRIVATE `build_manifest.py` и V20 перечисляют `git ls-files`; `.pytest_cache/` в `.gitignore`; pytest с
   `-p no:cacheprovider`; пересобрать `manifest.json` (RPR-001).
3. `.gitignore` обоих репозиториев: `.venv*/`, `.pytest_cache/`, `INPUT_VERIFICATION.json`, `.tmp_fetch/`;
   в `CLAUDE.md` venv создавать в `work/` (RPR-003, IGNORE_GAPS_VENV).
4. Pre-commit guard в cloud: отказ, если `git lfs ls-files --staged` не пуст. Локальную ветку
   `claude/practical-volta-l67oia` в PRIVATE не пушить (LFS_UPLOAD_BLOCKED_CLOUD).
5. Запушить локальные PUBLIC-теги (`legacy/final`, `frozen/*`, `archive/v3.2-last`) с workstation и защитить их.
   Создать в PRIVATE `archive/eloquent-goodall-ka7n0u` и `pe-v1-pass4`→`9b53896`.

**P1**
6. PUBLIC verifier: детект LFS-pointer с подсказкой; `--require-external` (exit ≠ 0 без PRIVATE); резолв внешних
   входов по SHA через `SOURCE_REGISTER.csv`. Затем `git rm -r 08_data_archives/main_repo_snapshots` в PRIVATE.
7. PUBLIC `.gitattributes`: eol для sh/js/toml/txt, `*.png binary`, чистка 23 устаревших паттернов.
8. Удалить `bootstrap_repo.sh/.ps1`. `verify_inputs.py` не трогать (он pinned), пометить HISTORICAL.
9. `localize_paths.sh` пишет копии в `$WORK/run`, не делает `sed -i` tracked-файлов.
10. `build_public_catalogues.py`: закоммитить map, exit ≠ 0 при MISSING.
11. PRIVATE: закоммитить `external_captures/EXT-SRC-004.txt`; validator по умолчанию пишет отчёт в `work/`;
    timestamp вынести из manifest; `run_pipeline.sh` падает, если LFS не materialized.
12. PRIVATE workflows удалить или обезвредить; `.ps1`-импортёр заменить на python, без commit/push.
13. Lock-файлы: `requirements/worldspec.lock.txt` (PUBLIC) и `requirements/evidence.lock.txt` (PRIVATE),
    `.python-version = 3.13`.

**P2: при reset `main` и на workstation**
14. Перенести old-stack тесты, скрипты, конфиги и frozen-цепочку v2.1/B3 в `legacy`. Добавить минимальный CI
    PUBLIC (`tests/world` + verifier).
15. Инструкции: bash + PowerShell, шаг `git lfs pull`, единая раскладка каталогов.
16. `ogs/run_all.sh` на все cases, `chmod +x` для 8 `run.sh`. Задокументировать, что figures требуют
    `work/ogs/runs`.
17. Решение по LFS для PNG/`*.csv.gz` принимать только на workstation. История не переписывается.

## 12. Ограничения аудита

- Генераторы frozen-данных, `capture_environment.py`, OGS `run.sh` и workflows не запускались: это регенерация
  данных, solver runs или push. Их поведение оценено по коду.
- Доступность retired LFS-объекта VKM-SRC-022 на сервере GitHub не проверялась (скачивание 63 МБ не требовалось).
- Проверка Windows моделировалась через `core.autocrlf=true` на Linux. Поведение PowerShell-скриптов не исполнялось.
- Для пересборки PRIVATE OCR 025/037 взяты из существующего `work/ocr` VM, а не пересчитаны.

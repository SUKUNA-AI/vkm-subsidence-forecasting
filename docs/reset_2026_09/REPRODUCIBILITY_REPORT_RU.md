# Отчёт о воспроизводимости после reset (Phase 1, cloud)

Дата: 26.09.2026. Здесь 36 находок [аудита воспроизводимости](audit/REPRODUCIBILITY_AUDIT_RU.md) сверены с фактическим
состоянием ветки `research/evidence-worldspec-reset`. Аудит проводился до reset, на PUBLIC `d95344e` и PRIVATE
`9e4bf11`; машиночитаемый список находок — [reproducibility_findings.csv](audit/reproducibility_findings.csv).

Проверенные состояния:

- PUBLIC — `5f00aba`, PRIVATE — `1da1550`. Что координатор исправил после сверки, перечислено в §6.
- Старое состояние: PUBLIC `legacy` = `d54025d`, PRIVATE `main` = `9e4bf11`. PUBLIC `origin/main` пока указывает на
  `1c1f816`: новая ветка попадёт в `main` через PR.
- Каждый статус проверен по git-объектам, файлам и повторным запускам. Текст `proposed_fix` и сводки
  координатора доказательством не считались.

Как выполнялись проверки:

- Окружение cloud VM: CPython 3.13.12 (research venv); системный `python3` 3.11.15 — на нём, как и в прогоне,
  запускались сборщики потоков; git 2.43; git-lfs 3.4.1.
- Все пересборки шли в scratch-копиях. Ни один файл репозиториев не изменён, кроме этого отчёта.
  `build_public_catalogues.py`, запущенный в дереве, записал те же байты: `git status` пуст.

Пути записаны логическими именами: `$VKM_RESOURCES_ROOT` (клон PRIVATE), `$VKM_WORK` (рабочий каталог),
`<PUBLIC>` (клон PUBLIC).

## 1. Вердикт

**Что воспроизводится сейчас (проверено повторным запуском):**

| Что | Из чего | Команда | Результат |
|---|---|---|---|
| Код, тесты и схема WorldSpec PUBLIC | `requirements/worldspec.lock.txt` | `pip install -r requirements/worldspec.lock.txt`, затем `python -m pytest -q tests/world` | Venv, собранный с нуля только из lock (CPython 3.13.12): 162 passed. `schema:worldspec_vnext` — PASS. `export_worldspec_schema.py` на копии HEAD даёт пустой `git diff` |
| Canonical verifier | PUBLIC + PRIVATE | `VKM_RESOURCES_ROOT=… python scripts/verify_canonical_repository.py` | Итог — в §5 |
| Frozen-релизы старой архитектуры | git-объекты: `legacy`, 7 закреплённых commit, `10452b0` | тот же verifier | 11 ссылок × 2 ref — PASS; retired-файлы 14/14. Все закреплённые commit — предки и `research/…`, и `origin/main` |
| Объединённые записи sweep | PRIVATE `11_evidence_vnext/sweep_raw/` (243 файла = 81 чанк) + тексты страниц | `run_kit/tools/{merge_sweep,quote_check_v2,build_coverage_master,batch_loss_audit,status}.py` ([порядок](run_kit/README_RU.md)) | Побайтово совпадают 5 файлов (§3.1). `status.py`: 81 чанк, все 81 DONE |
| PUBLIC-каталоги: 46 файлов + manifest | PRIVATE `11_evidence_vnext/canonical/` | `VKM_RESOURCES_ROOT=… python scripts/build_public_catalogues.py` | Записано 46 каталогов, утечек 0. `git status evidence catalogues` пуст — и на scratch-копии, и в дереве |
| Валидатор PE v1 (PRIVATE) | HEAD PRIVATE | `validate_physical_evidence.py --report <scratch>` | PASS: 0 hard errors, 3 warnings, 1367 файлов, режим `git`; дерево не изменилось |
| Локализация run kit | — | `PUB=… RES=… WORK=<scratch> VENV=… bash localize_paths.sh` | 24 копии + `roots.env`; tracked-файлы не изменены |

**Что повторным исполнением не воспроизводится:**

- **Сам sweep**: страницы читали LLM-агенты. Первичная запись — `sweep_raw/`; повторное чтение дало бы другие
  формулировки.
- **Суждения агентов в синтезе.** Это отчёты `*_RU.md`, `progress.md`, таблицы визуальных проверок, а также данные
  внутри сборщиков (`defs/`, `data_*.py`, `proc_defs_*.py`). Всё это закоммичено как данные, но пересоздать
  его нечем.
- **Рендеры визуальных проверок**: 2 238 PNG (≈684 МБ) в `$VKM_WORK/run/img`, в git не входят. В git есть только
  26 растров Мусихина: PRIVATE `11_evidence_vnext/figures/musikhin/`. `render_page.py` воссоздаёт растр,
  но не суждение агента по нему.
- ~~Два каталога синтеза~~ — исправлено после сверки (§6). Сборщики `geology_evidence.csv` (GEOLOGY_COORDS) и
  `interfaces_damage_catalog.csv` (MECH_RHEO) читали файлы, существовавшие только во временных каталогах агентов.
  Эти файлы закоммичены (PRIVATE `7929cc5`), и в строгом режиме оба каталога пересобираются побайтово.
- **Потоки OCR_VISUAL_QA и внешнего поиска** на момент проверки не завершены и не оценивались.

**Что воспроизводится с оговорками:**

- Метки `quote_check` вычисляются по текстам страниц: `$VKM_WORK/corpus`, 2 562 файла ≈ 13 МБ (PyMuPDF 1.28.2,
  pandoc 3.1.3), плюс 434 страницы постраничного OCR в git-ignored `work/` PRIVATE. Сами тексты не закоммичены.
  После сверки добавлен хеш-манифест: PRIVATE `11_evidence_vnext/receipts/corpus_text_manifest.csv` (3473 файла,
  `7929cc5`). Он позволяет проверить, что тексты на другой машине те же.
- Три сборщика потоков зависят от `PYTHONHASHSEED` (§3.2). PHYSICS_CAUSAL после сверки пересобран по финальному
  FORMULAS и переопубликован (§6).

## 2. Статус 36 находок

Словарь статусов:

- **FIXED** — исправлено коммитом текущей ветки;
- **RESOLVED_BY_RESET** — затронутые файлы удалены из новой ветки (в основном `ac7795d`) и сохранены в `legacy`;
  отсутствие в `research/…` и наличие в `legacy` проверены через `git cat-file -e`;
- **PARTIAL** — исправлено частично;
- **OPEN_PHASE2** — нужна workstation: push тегов, LFS upload, защита веток. Из cloud такие операции получают 403;
- **OPEN** — не сделано;
- **WONT_FIX** — принято без изменений, причина указана.

**Итого: FIXED 11, RESOLVED_BY_RESET 7, PARTIAL 9, OPEN_PHASE2 2, OPEN 6, WONT_FIX 1.**

| Severity | Всего | FIXED | RESOLVED_BY_RESET | PARTIAL | OPEN_PHASE2 | OPEN | WONT_FIX |
|---|---|---|---|---|---|---|---|
| HIGH | 4 | 3 | — | — | 1 | — | — |
| MEDIUM | 16 | 3 | 2 | 7 | 1 | 3 | — |
| LOW | 16 | 5 | 5 | 2 | — | 3 | 1 |

Номера строк («стр.») относятся к файлам на проверенных HEAD.

| ID | Репо | Sev | Статус | Доказательство | Что осталось |
|---|---|---|---|---|---|
| RPR-001 | PRIVATE | HIGH | FIXED | `941492d`: `build_manifest.package_files()` перечисляет файлы через `git ls-files` (tracked и untracked, кроме ignored). V20 использует ту же функцию, pytest запускается с `-p no:cacheprovider`, manifest пересобран (`n_files` 1367, `file_enumeration: git`). Сейчас валидатор с `--report <scratch>` даёт PASS: 0 hard errors, 3 warnings | Исторический `validation_report.json` пакета (1371 файл) намеренно не переписан |
| RPR-002 | PRIVATE | HIGH | FIXED | `941492d`: в `.gitattributes`, стр. 7, стоит `* -text` (решение D-08). `git check-attr` для `README.md`, `SOURCE_REGISTER.csv`, `run_pipeline.sh` показывает `text: unset`. Симуляция autocrlf из receipt: PASS против 1453 hard errors без правки | Реальный checkout в Git for Windows не проверялся |
| RPR-003 | PRIVATE | HIGH | FIXED | `941492d`: `.gitignore`, стр. 18–21 (`.venv*/`, `.pytest_cache/`, `.tmp_fetch/`, `**/__pycache__/`); `git check-ignore -v` подтверждает. `c026bd6`: CLAUDE.md велит ставить venv в `work/` | Workflows и `.ps1` по-прежнему делают `git add -A` (RPR-026/027). Legacy OGS-кейсы ищут `work/ogs/.venv-ogs`, а пример в CLAUDE.md — `work/.venv-ogs`; оба пути игнорируются |
| RPR-004 | BOTH | HIGH | OPEN_PHASE2 | Правило D-10 записано в PUBLIC CLAUDE.md, в `.gitattributes` и в PRIVATE CLAUDE.md (`c026bd6`). Ветки Phase 1 без LFS: `git lfs push --dry-run origin research/evidence-worldspec-reset` в PRIVATE пуст, в PUBLIC 0 LFS-файлов. Но локальная ветка PRIVATE `claude/practical-volta-l67oia` = `292655a` хранит объект `bd983e29…` (OCR zip), которого нет на сервере. Guard не установлен: в `.git/hooks` только хуки LFS | Загрузить объект с workstation или пересчитать OCR там; удалить ветку; добавить guard (хук или тест) |
| RPR-005 | PUBLIC | MEDIUM | FIXED | `75eac8a` (verifier v2): frozen-файлы читаются из git-объектов, LFS-указатель узнаётся по `oid` (`parse_lfs_pointer`, стр. 124). Указатели PRIVATE получают `SKIPPED_LFS_POINTER` (стр. 689), oid сверяется с реестром. Тесты: `test_private_register_and_public_catalogue`, `test_closures_from_git_objects_in_synthetic_repo`. Симуляция PRIVATE из 39 указателей: WARN «39 LFS pointers skipped», ложного FAIL нет. README (`dd174ff`) содержит `git lfs pull` | Явной подсказки `git lfs pull` в выводе нет. Если в PRIVATE одни указатели, exit 0 (см. RPR-006). Handoff §2.1 ошибочно пишет, что проверка «ложно падает»: на деле WARN и exit 0 |
| RPR-006 | PUBLIC | MEDIUM | PARTIAL | Без `VKM_RESOURCES_ROOT`: `private:configured` = SKIPPED с `how_to_enable` (стр. 659), итог `PASS_WITH_NONBLOCKING`, **exit 0**, 38 externalized closure-записей не проверены. Семантика описана в [LEGACY_INDEX_RU.md](../legacy/LEGACY_INDEX_RU.md) §4 | Нужен `--require-external`: exit ≠ 0 без PRIVATE и при 0 проверенных файлов (для workstation и CI) |
| RPR-007 | BOTH | MEDIUM | PARTIAL | `75eac8a`: externalized-входы находятся по sha256 через `SOURCE_REGISTER.csv` (стр. 394, 415). Копии `08_data_archives/main_repo_snapshots/` не читаются (стр. 69; в отчёте `duplicate_snapshot_copies_read: false`). В текущем прогоне сопоставлены VKM-SRC-001…010, 023, 024 | PRIVATE-дубликаты не удалены (RPR-028); таблицы «старое имя → VKM-SRC» нет |
| RPR-008 | PUBLIC | MEDIUM | RESOLVED_BY_RESET | `scripts/verify_inputs.py` и `bootstrap_repo.sh/.ps1` удалены в `ac7795d`, в `legacy` есть. `INPUT_VERIFICATION.json` добавлен в `.gitignore` (`7abc33c`, стр. 56) | — |
| RPR-009 | PUBLIC | MEDIUM | FIXED | `7abc33c`: `.gitattributes`, стр. 8–15 (`*.sh`, `*.js`, `*.toml`, `*.txt`, `.python-version` → `text eol=lf`). `git check-attr` даёт `eol: lf` для `localize_paths.sh`, `pyproject.toml`, lock и `*.js`. `git ls-files --eol`: 177 файлов i/lf + 1 пустой | `*.ps1 eol=crlf` не нужен: `.ps1` в PUBLIC нет |
| RPR-010 | PUBLIC | LOW | FIXED | `7abc33c` удалил все правила для конкретных путей; вложенные `.gitattributes` в `artifacts/`, `configs/`, `data/`, `docs/` удалены в `ac7795d`. Остались правила по расширениям и для 3 корневых файлов | — |
| RPR-011 | PUBLIC | LOW | RESOLVED_BY_RESET | 31 PNG и 3 `artifacts/**/*.csv.gz` удалены в `ac7795d`, в `legacy` есть. В текущем дереве 0 PNG, 0 gz, 0 LFS-файлов. `*.png binary` добавлен как защита (`7abc33c`, стр. 16) | — |
| RPR-012 | PUBLIC | MEDIUM | FIXED | `2898a7e`, `5e08baa`: `localize_paths.sh` пишет копии в `$WORK/run/` и правит только их, создаёт `roots.env`, самопроверку делает по копиям. Инструменты берут корни из окружения (`tools/_roots.py`). Прогон с фиктивными корнями: 24 копии, `git status` не изменился, путей VM в копиях нет | — |
| RPR-013 | PUBLIC | MEDIUM | PARTIAL | `95218c5`, `b641c12`: карта `scripts/public_catalogue_map.json` (46 записей) закоммичена, PRIVATE `canonical/` существует. Сборка детерминирована (см. §1). Тест `test_manifest_matches_committed_catalogues` требует `status == OK` у каждого файла manifest | [build_public_catalogues.py](../../scripts/build_public_catalogues.py): при MISSING только запись в manifest (стр. 45–46); manifest пишется всегда; при отсутствии утечек exit 0 (стр. 70). Теста сборщика на fixture нет |
| RPR-014 | PUBLIC | MEDIUM | PARTIAL | `7abc33c`: `requirements/worldspec.{in,lock.txt}` (pydantic 2.13.5, pydantic-core 2.46.5, numpy 2.5.3, pytest 9.1.1 и зависимости). Gate-locks удалены (`ac7795d`), README ссылается на lock (`dd174ff`). Venv с нуля только из lock: 162 passed, схема PASS | Нет `--hash`. Lock снят с Linux-freeze, на Windows не проверен. Pymupdf, networkx и pandoc для run kit и PRIVATE закреплены только в `run_kit/venv_research_freeze.txt` |
| RPR-015 | BOTH | LOW | PARTIAL | PUBLIC: `.python-version` = `3.13` (`7abc33c`). PRIVATE: manifest PE v1 фиксирует python 3.13.12 (`941492d`) | Нет PRIVATE lock и списка системных инструментов (tesseract 5.3.4 rus, ddjvu, pandoc 3.1.3, pymupdf 1.28.2). То, что сборщики потоков шли на `python3` 3.11.15, нигде не записано |
| RPR-016 | PUBLIC | LOW | FIXED | `dd174ff`: в README команды bash с плейсхолдером, вариант для PowerShell и `git lfs install --local && git lfs pull`. `README_FIRST.md` удалён, state-документ перенесён в `docs/legacy/`, `REPOSITORY_CONSOLIDATION_*.md` удалён (`ac7795d`). PRIVATE README исправлен так же (`c026bd6`) | — |
| RPR-017 | PUBLIC | LOW | FIXED | `2898a7e`: `resources_root` в `preflight_verify_canonical.json` заменён на `<RES>`. `ac7795d` удалил `preflight_public_pytest.txt`, `artifacts/environment/*` и `scripts/capture_environment.py`. `f0c5e86`: страж утечки ловит машинные пути во всех tracked-текстах; `leakage:scan` — PASS | Протоколы и workflow run kit сохраняют пути VM по исключению [политики путей](../governance/DATA_AND_PATH_POLICY_RU.md) §3 |
| RPR-018 | PUBLIC | MEDIUM | PARTIAL | Старые тесты удалены в `ac7795d` и лежат в `legacy`; `pytest -q tests/world` → 162 passed. В PUBLIC нет `.github/` (0 файлов) | Нужен минимальный CI: lock → `tests/world` → verifier. Для push workflow-файлов может понадобиться право `workflow` |
| RPR-019 | PUBLIC | MEDIUM | RESOLVED_BY_RESET | `scripts/generate_scenario_v2{,_1}.py` удалены в `ac7795d`, в `legacy` есть | — |
| RPR-020 | PUBLIC | LOW | RESOLVED_BY_RESET | 5 конфигов, `prepare_gate_a0.py` и `reconstruct_musikhin_line1.py` удалены в `ac7795d`, в `legacy` есть. Frozen-цепочка проверяется из git-объектов: `scripts/frozen_references.json` — 11 ссылок, 8 якорей, 14 retired | — |
| RPR-021 | PUBLIC | LOW | RESOLVED_BY_RESET | Все перечисленные receipts удалены из новой ветки (`ac7795d`) и есть в `legacy`. Единственный оставшийся файл `artifacts/repository_cleanup/legacy_data_retirement_receipt.json` не содержит пар (path, sha256) | — |
| RPR-022 | PUBLIC | MEDIUM | PARTIAL | `7abc33c`: в `.gitignore` добавлены `.venv*/` (стр. 18) и `INPUT_VERIFICATION.json`. CLAUDE.md (`dd174ff`): venv — вне репозитория или в `work/` | В `.gitignore` остались `inputs/holdout_candidates/t1_final_v3/*` и комментарий о вложенной раскладке PRIVATE (стр. 66–68), хотя обе README предписывают клоны рядом (sibling) |
| RPR-023 | BOTH | MEDIUM | OPEN_PHASE2 | PUBLIC: 7 аннотированных тегов существуют только локально (`git tag -l`); `git ls-remote --tags origin` пуст в обоих репозиториях. Push тегов → 403 (D-09). Verifier проверяет теги локально: PASS. PRIVATE: тегов нет вообще. `3a5761a` (якорь V23 валидатора PE v1), а также `b1fbb69`, `03ab2e0`, `12da8a6` (версии `pw2d_generator.py`) достижимы только из `origin/claude/eloquent-goodall-ka7n0u` | Запушить теги, защитить `legacy`. В PRIVATE создать `archive/eloquent-goodall-ka7n0u` → `3a5761a` и `pe-v1-pass4` → `9b53896` |
| RPR-024 | PRIVATE | MEDIUM | OPEN | В `external_captures/` нет EXT-SRC-004/005 (`git ls-files`). После `941492d` пакет не менялся. Решение D-06: пакет сохраняется как есть | Закоммитить capture как текст или заморозить `locator_verification` у отозванных записей. Другой вариант — оформить WONT_FIX по D-06 |
| RPR-025 | PRIVATE | MEDIUM | PARTIAL | `941492d`: отчёт валидатора по умолчанию пишется в `work/physical_evidence_v1/` (ignored). Прогон валидатора не изменил дерево | В `manifest.json` остались `generated_utc`, `git_head_at_generation` и `python`. `run_pipeline.sh`, стр. 17: шаг 0 игнорирует ошибку `git lfs pull` и идёт дальше без LFS. Версии системных инструментов не закреплены |
| RPR-026 | PRIVATE | MEDIUM | OPEN | Три workflow не менялись с `145507e`: запуск при push в `main`, `contents: write`, `lfs: true`, `git add -A`, `git push origin main`. Проверяющего CI нет. Смягчение: `.gitignore` (`941492d`) не даёт `git add -A` подхватить venv и кеши | Удалить или перевести на `workflow_dispatch` с правами только на чтение; добавить read-only CI |
| RPR-027 | PRIVATE | MEDIUM | OPEN | `scripts/import_remaining_local_only.ps1` и `import_local_resources.ps1` не менялись с `145507e`. README (`c026bd6`) только помечает `scripts/` как устаревшие со ссылкой на этот отчёт | Удалить или заменить python-импортёром без commit/push |
| RPR-028 | PRIVATE | LOW | OPEN | `git ls-files 08_data_archives/main_repo_snapshots` → 12 файлов. Выдержка Жукова (`00_registry/intake/…/derived/…pages117-147.pdf`) лежит в LFS, в реестре её нет | `git rm -r` снимков: verifier их больше не читает, upload не нужен, возможно даже из cloud. Решить, что делать с выдержкой |
| RPR-029 | PRIVATE | LOW | OPEN | Заметки реестра о VKM-SRC-013/022 не менялись с `145507e`. У 013 нет явной записи «в Git никогда не было, байты невосстановимы». У 022 записано «Git history is the archive», но объекта нет в локальном LFS-store. Verifier: 39 файлов совпали по sha, 2 отсутствуют по статусу | Дописать заметки; проверить, есть ли 022 на GitHub LFS, и сделать офлайн-копию (workstation) |
| RPR-030 | PRIVATE | LOW | OPEN | `ogs/run_all.sh` по-прежнему перечисляет только cases 00–11. У 8 `run.sh` (cases 13, 14, 16, 17, 19–22) режим `100644`. Figures ссылаются на `work/ogs/runs/**` | Правки текстовые, но проверить их можно только запуском OGS (Phase 2). OGS-кейсы — legacy; TOY 00–07 переносятся как регрессионный набор |
| RPR-031 | PRIVATE | LOW | FIXED | `c026bd6`: раздел PRIVATE README «Раскладка и первые команды» — клоны рядом + `VKM_RESOURCES_ROOT`, команды bash и PowerShell; совет вкладывать репозиторий в каталог диплома удалён. PUBLIC-документ с противоположным советом удалён (`ac7795d`) | Устаревший комментарий в PUBLIC `.gitignore` (RPR-022) |
| RPR-032 | PRIVATE | LOW | WONT_FIX | Так решил аудит («no change»): исторические логи PRIVATE хранят пути VM. PUBLIC защищён стражем утечки и `sanitize_paths` (`f0c5e86`) | Класс вырос: пути VM или временных каталогов агентов есть в 44 из 89 сборщиков `canonical/` и в `merged/parse_errors.json` (§3.2) |
| RPR-033 | PUBLIC | LOW | RESOLVED_BY_RESET | `tests/test_prepare_gate_a0.py` и `tests/test_*_protocol.py` удалены в `ac7795d`, лежат в `legacy`. `tests/world` пишут только в `tmp_path` | — |
| RPR-034 | PUBLIC | LOW | RESOLVED_BY_RESET | `data/scenario_simulation_v2*/**` и `frozen_candidate.joblib` удалены в `ac7795d`, лежат в `legacy`. Verifier узнаёт LFS-объекты по oid и ничего не загружает (`models_executed: 0`) | — |
| RPR-035 | PUBLIC | LOW | PARTIAL | Verifier v2: в отчёте нет абсолютных путей (`private_resources.source`, `repository.root_name`); файл пишется только с `--output` | `repo_files()` по-прежнему вызывает `ls-files --cached --others --exclude-standard` (стр. 246). Поэтому untracked `.md` влияют на `markdown:links`: так ссылка handoff на ещё не написанный отчёт дала FAIL. При этом `leakage.scan` видит только tracked-файлы: области двух проверок расходятся |
| RPR-036 | PUBLIC | LOW | FIXED | `dd174ff`: старый handoff стал `CLOUD_CHECKPOINT_HANDOFF_2026-09-26_RU.md` с пометкой «исторический»; строка «10 passed» осталась в нём как история. README требует «все тесты должны проходить», Phase-2 handoff чисел не фиксирует | — |

## 3. Новые факты воспроизводимости Phase 1

### 3.1 Цепочка `sweep_raw` → `merged`

- **Корни**:
  - `VKM_WORK` — рабочий каталог прогона, только чтение;
  - `VKM_RESOURCES_ROOT` — клон PRIVATE;
  - `VKM_PUB` — клон PUBLIC;
  - `VKM_MERGED_DIR` — scratch.
- **Вход**: закоммиченный `sweep_raw` (решение D-15). Рабочий каталог sweep прогона побайтово равен `sweep_raw`:
  243 из 243 файлов.
- **Время**: 4,5 с.

| Файл | sha256 (первые 16) | Результат |
|---|---|---|
| `all_records.jsonl` (13 572 строки, 22 243 732 байта) | `8a5f5ae02b6ae2a1` | Совпадает с `$VKM_WORK/merged/all_records.jsonl` прогона. Тот же хеш записан как `inputs_sha256` в PRIVATE `canonical/BOREHOLES/build/build_receipt.json` |
| `merge_summary.json` | `ae9d071dd5adfa1e` | Совпадает с PRIVATE `11_evidence_vnext/merged/` |
| `quote_check_v2.csv` | `b26d5485fd761f7c` | Совпадает |
| `SOURCE_COVERAGE_MASTER.csv` | `2a3ca15472fce041` | Совпадает; совпадает и с `canonical/SOURCES/` |
| `batch_loss_audit.csv` | `2ad95a0fd773076c` | Совпадает |
| `parse_errors.json` | — | 24 записи эквивалентны. Отличается только абсолютный путь входного файла: закоммиченная версия хранит путь VM |

### 3.2 PRIVATE `canonical/<STREAM>`: повторный запуск сборщиков

**Метод.**

- Сборщики извлечены из `git archive HEAD` в scratch. В копиях корни VM заменены на корни scratch, как в
  `localize_paths.sh`. Исходные файлы не менялись.
- Входы подключены только на чтение:
  - `all_records.jsonl`, пересобранный из `sweep_raw` (§3.1);
  - рабочий каталог sweep, равный `sweep_raw`;
  - тексты страниц `$VKM_WORK/corpus`.
- Порядок шагов взят из receipts потоков: `RUN_ORDER.txt`, `manifest.json`/`MANIFEST.json`, `progress.md`,
  `run_all.sh`.
- Два режима:
  - **строгий** — доступны только закоммиченные файлы;
  - **VM** — дополнительно подключены копии файлов из временных каталогов агентов, только на чтение.
- Отдельно, в строгом режиме, проверены два обходных прогона: GEOLOGY_COORDS без шага `build_geology.py`
  (`PYTHONHASHSEED=2`) и `mr_build_conf.py` в одиночку.
- Зависимость от seed проверена так:
  - BOREHOLES и CITATIONS — прогоны с `PYTHONHASHSEED` = 0, 0, 1;
  - `build_coords.py` — seed 0, 1, 2, 3, 5, 7, 11.

| Поток | Строгий режим | Совпало с закоммиченным | Замечания |
|---|---|---|---|
| FORMULAS | OK | 4/4 | — |
| HYDRO_THERMAL_GEOPHYS | OK | 4/4 | — |
| MONITORING_LIFECYCLE | OK | 9/9 | — |
| MINING | OK | 8/8 каталогов | `progress.md` — журнал, в который сборщик дописывает строки |
| SOURCES | копия | 1/1 | Копия `merged/SOURCE_COVERAGE_MASTER.csv` |
| BOREHOLES | OK | 4/6 | `borehole_identity_issues.csv` и `build/_intermediate.json`: порядок строк зависит от `PYTHONHASHSEED`, набор строк тот же. С фиксированным seed результат повторяется, с другим seed — нет |
| CITATIONS | OK | 7/9 | В `source_families.csv`, колонка `shared_cited_works`, выбор среди работ с равной частотой зависит от `PYTHONHASHSEED`: меняется у 2 из 15 семейств. `validation_receipt.json` хранит хеш этого файла и потому тоже меняется |
| PHYSICS_CAUSAL | OK, детерминирован (оба режима совпали) | 6/9 | Колонка `math_model_ids_FORMULAS_draft` собрана по **черновику** `FORMULAS/RECORD_TO_MODEL_MAP.csv`. С финальным закоммиченным реестром добавляется 19 id в 13 из 72 строк матрицы и 5 id в 4 из 24 операторов; ничего не удаляется. `MANIFEST.json` содержит время сборки |
| GEOLOGY_COORDS | **FAIL** на `build_geology.py`: нет `geo_keep.json` | По `RUN_ORDER.txt` 1/5. Если пропустить `build_geology.py`, остальные 4/4 совпадают (при подходящем seed). С копией из VM 5/5 | `geo_keep.json` (≈40 КБ) лежит только во временном каталоге агента; сборщика для него в git нет. Без этого файла не собирается только `geology_evidence.csv`, но цепочка `RUN_ORDER.txt` обрывается на нём. `build_coords.py` зависит от `PYTHONHASHSEED`: при части seed меняется категория 1 строки |
| MECH_RHEO | **FAIL** на `mr_build_idc.py`: нет `idc_cand.json` | По порядку из manifest 2/5. `mr_build_conf.py`, запущенный отдельно, даёт ещё 2/2. С копией из VM 5/5 | `idc_cand.json` (≈2 МБ) лежит только в scratchpad сессии; сборщика для него в git нет. Без него не собирается только `interfaces_damage_catalog.csv`. То, что сборщики ждут вспомогательные файлы во временных каталогах, отмечено в `MECH_RHEO/manifest.json` и в `RUN_ORDER.txt` GEOLOGY_COORDS |

**Что не создаётся ни одним сборщиком** (результат работы агентов):

- отчёты `*_RU.md`, `progress.md`;
- визуальные проверки CITATIONS, MONITORING_LIFECYCLE, PHYSICS_CAUSAL;
- `BOREHOLES/build/{fig32_label_ocr_check.csv, report_stats.txt}`;
- `FORMULAS/vn_snapshot.json`;
- manifests потоков GEOLOGY_COORDS и MECH_RHEO.

**Пути в сборщиках.** 44 из 89 скриптов `canonical/` содержат пути VM или временных каталогов агентов: это все
потоки синтеза Phase 1. Два новых сборщика, `EXTERNAL/build_external.py` и `OCR_VISUAL_QA/correction_impact.py`,
читают корни из окружения. Входы EXTERNAL лежат в `$VKM_WORK/external` и пока не закоммичены.

**Что это значит для PUBLIC.** Сборка PUBLIC из PRIVATE детерминирована, поэтому свойства PRIVATE-каталогов
переходят в PUBLIC без изменений:

- устаревшие ссылки на модели:
  [physics_coverage_and_execution_matrix.csv](../../catalogues/physics/physics_coverage_and_execution_matrix.csv),
  [observation_operator_design.csv](../../catalogues/observations/observation_operator_design.csv);
- зависимость от seed: [borehole_identity_issues.csv](../../evidence/boreholes/borehole_identity_issues.csv),
  [source_families.csv](../../evidence/sources/source_families.csv),
  [coordinate_datum_evidence.csv](../../evidence/coordinates/coordinate_datum_evidence.csv);
- без незакоммиченных файлов не пересобираются
  [geology_evidence.csv](../../evidence/geology/geology_evidence.csv) и
  [interfaces_damage_catalog.csv](../../evidence/materials/interfaces_damage_catalog.csv).

По правилу D-14 ([журнал решений](../governance/PHASE1_DESIGN_DECISIONS_RU.md)) каталог после правки сборщика
пересобирают и проверяют, что diff затрагивает только нужные строки. Сейчас это правило выполнимо не везде:

- для BOREHOLES, CITATIONS и GEOLOGY_COORDS нужно фиксировать seed;
- PHYSICS_CAUSAL даст дополнительные 17 изменённых строк (13 в матрице и 4 в операторах);
- в GEOLOGY_COORDS и MECH_RHEO шаги `build_geology.py` и `mr_build_idc.py` без двух файлов не выполнятся.

### 3.3 Сводка: сборка из закоммиченного или нет

- **Детерминированно пересобирается из закоммиченного** (проверено повторным запуском):
  - `merged/*` из `sweep_raw` — с оговоркой о текстах страниц;
  - потоки FORMULAS, HYDRO_THERMAL_GEOPHYS, MONITORING_LIFECYCLE, MINING, SOURCES;
  - потоки BOREHOLES и CITATIONS, кроме seed-зависимых файлов;
  - потоки GEOLOGY_COORDS и MECH_RHEO, кроме `geology_evidence.csv` и `interfaces_damage_catalog.csv`;
    `coordinate_datum_evidence.csv` совпадает только при подходящем seed;
  - PUBLIC `evidence/` и `catalogues/` из PRIVATE `canonical/`;
  - `schemas/worldspec_vnext.schema.json` из кода;
  - проверка PE v1.
- **Пересобирается, но не совпадает с закоммиченным**: PHYSICS_CAUSAL (собран по черновику FORMULAS) и
  seed-зависимые файлы.
- **Повторным исполнением не воспроизводится**, потому что это первичная запись или суждение:
  - `sweep_raw` — чтение агентами;
  - OCR сканов (tesseract); текст закоммичен в PRIVATE `00_registry/cloud_checkpoint_2026-09-26/`, из него
    `split_ocr_all.py` восстанавливает постраничный OCR;
  - отчёты и визуальные проверки вместе с рендерами;
  - промежуточные файлы агентов `geo_keep.json` и `idc_cand.json` — **не закоммичены**;
  - внешний поиск (ещё идёт; решение D-12).

## 4. Остаточные риски и приоритеты

Пункты 4.1.1, 4.1.2 и часть 4.2.4–4.2.5 закрыты после сверки (§6).

### 4.1 Срочно, пока жива cloud VM (текстовые коммиты, LFS не нужен)

1. **Сохранить входы двух потоков.** Закоммитить в PRIVATE `canonical/GEOLOGY_COORDS/` и `canonical/MECH_RHEO/`
   файлы `geo_keep.json` и `idc_cand.json` из временных каталогов агентов и направить на них сборщики.
   Без этого после закрытия VM нельзя будет пересобрать `geology_evidence.csv` и `interfaces_damage_catalog.csv`
   вместе с их PUBLIC-копиями, а цепочки сборки обоих потоков обрываются на этих шагах.
2. **Решить судьбу объекта `bd983e29…`** на локальной ветке PRIVATE `claude/practical-volta-l67oia`. Объект
   исчезнет вместе с VM, а загрузить его из cloud нельзя.
   - Если пословная уверенность OCR нужна, её пересчитывают на workstation (tesseract 5.3.4 rus, 300 dpi).
   - Если нет — записать решение.

### 4.2 Workstation (Phase 2), по приоритету

1. **Теги и защита веток** (RPR-023): команды в [handoff](../../CLOUD_TO_LOCAL_PHASE2_HANDOFF_RU.md) §2.2.
   - Запушить 7 тегов PUBLIC, защитить `legacy`.
   - В PRIVATE поставить теги на `3a5761a` и `9b53896`. Иначе удаление ветки `claude/eloquent-goodall-ka7n0u`
     лишит валидатор PE v1 якоря V23, а receipts OGS — версий генератора.
2. **Реальный checkout в Git for Windows** (`core.autocrlf=true`) обоих репозиториев: verifier, `tests/world`,
   валидатор PE v1, `localize_paths.sh`. До сих пор это проверялось только симуляцией (RPR-002, RPR-009).
3. **Строгий режим verifier и CI** (RPR-005/006/018). Флаг `--require-external`: exit ≠ 0 без PRIVATE или когда
   0 файлов проверено. Минимальный CI PUBLIC: lock → `tests/world` → verifier. В handoff §2.1 исправить фразу
   «ложно падает».
4. **Детерминизм и переносимость сборщиков потоков.**
   - Фиксировать `PYTHONHASHSEED=0` или сортировать явно (BOREHOLES, CITATIONS, GEOLOGY_COORDS).
   - Брать корни из окружения, как в run kit.
   - Пересобрать PHYSICS_CAUSAL по финальному FORMULAS и переопубликовать 2 PUBLIC-каталога по D-14.
   - Закоммитить входы EXTERNAL как первичную запись, по аналогии с `sweep_raw`.
5. **Тексты страниц.** Добавить хеш-манифест для `$VKM_WORK/corpus` и постраничного OCR, либо закоммитить сами
   тексты в PRIVATE как текст (≈13 МБ). Завести PRIVATE lock: pymupdf, networkx, версия `python3` для сборщиков,
   системные tesseract, ddjvu, pandoc (RPR-014/015).
6. **Гигиена PRIVATE** (RPR-026/027/028):
   - удалить или обезвредить 3 workflow и `.ps1`-импортёр;
   - выполнить `git rm -r 08_data_archives/main_repo_snapshots` — это уже ничему не мешает;
   - решить, что делать с выдержкой Жукова.
7. **Инструменты PUBLIC** (RPR-013, RPR-035):
   - `build_public_catalogues.py`: exit ≠ 0 при MISSING, manifest не писать; тест на fixture;
   - verifier: ссылки по умолчанию проверять только в tracked-файлах, как и утечку.
8. **PE v1** (сохраняется как есть по D-06): capture EXT-SRC-004/005 или формальный WONT_FIX; волатильные поля
   manifest; `git lfs pull` без проверки результата; `run_all.sh` и `chmod +x` для OGS (RPR-024/025/030).
9. **Мелочи**:
   - устаревшие строки PUBLIC `.gitignore`;
   - заметки реестра о VKM-SRC-013/022 и офлайн-копия 022;
   - `--hash` в lock (RPR-014/022/029).

## 5. Проверка этого отчёта

Отчёт написан как untracked-файл. Страж утечки `leakage.scan` без аргументов видит только tracked-файлы, поэтому
этот файл проверен им явно: `scan(root, files=[…])` — 0 проблем. `markdown:links` учитывает и untracked-файлы:
41 Markdown-файл, 75 локальных ссылок, 0 битых. Итог полного verifier с PRIVATE после записи файла: 40 проверок,
**PASS**, exit 0. `pytest -q tests/world`: 162 passed; `-k leakage`: 2 passed.

## 6. Изменения после сверки (координатор, 26.09.2026)

| Что | Коммит | Проверка |
|---|---|---|
| `geo_keep.json` и `idc_cand.json` из временных каталогов агентов закоммичены рядом со сборщиками: `canonical/GEOLOGY_COORDS/scripts/`, `canonical/MECH_RHEO/scripts/`. Карта путей для повторного запуска — `canonical/BUILD_PATHS_RU.md`; харнесс — `receipts/tools/rerun_streams.py` | PRIVATE `7929cc5` | Повторный запуск всех 10 потоков в строгом режиме (только закоммиченные файлы). Шаги всех потоков OK. GEOLOGY_COORDS: 4 файла совпали, отличается только `coordinate_datum_evidence.csv` (seed). MECH_RHEO: 5/5 совпали |
| Хеш-манифест текстов страниц корпуса и постраничного OCR (3473 файла) | PRIVATE `7929cc5` | — |
| PHYSICS_CAUSAL пересобран по финальному реестру FORMULAS. Меняется только колонка `math_model_ids_FORMULAS_draft`: +19 id в 13 строках матрицы, +5 id в 4 операторах. PUBLIC-копии переопубликованы | PRIVATE `9922e0f`; PUBLIC — коммит с этим отчётом | Строгий режим: сборка детерминирована; diff затрагивает только названную колонку |
| Объект LFS `bd983e29…` (OCR-архив с пословной уверенностью, локальная ветка PRIVATE `claude/practical-volta-l67oia`, `292655a`) | решение | **Не переносится.** Текст OCR закоммичен как обычный текст (`00_registry/cloud_checkpoint_2026-09-26/`). Пословную уверенность при необходимости пересчитывают на workstation: tesseract 5.3.4 rus, 300 dpi. Локальная ветка не пушится; удалённая ветка обновляется из `research/evidence-worldspec-reset` |
| Фраза handoff §2.1 про «ложное падение» без `git lfs pull` исправлена: на деле WARN «LFS pointers skipped» и exit 0 | PUBLIC — коммит с этим отчётом | — |

Не сделано в cloud и передано в Phase 2 или в правки по итогам самопроверки:

- детерминизм трёх seed-зависимых сборщиков (сортировка вместо зависимости от хеша);
- `--require-external` и CI;
- гигиена PRIVATE (RPR-026/027/028);
- теги.

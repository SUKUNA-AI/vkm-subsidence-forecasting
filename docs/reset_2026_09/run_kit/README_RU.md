# Run kit облачных прогонов 26.09.2026

Здесь лежат протоколы, workflow-скрипты и инструменты двух cloud-прогонов:

- **ночной run** (первая сессия): preflight, извлечение текста корпуса, OCR, первый sweep, math wave 1, аудит
  репозиториев. Файлы: `SWEEP_PROTOCOL.md`, `MATH_PROTOCOL.md`, `CONTEXT_PREAMBLE.md`, `chunk_plan.json`,
  `workflows/{corpus_sweep,math_foundation_wave1,repo_audit}.js`;
- **Phase 1** (вторая сессия, «CLOUD ULTRACODE»): полный sweep 65 чтений, синтез, внешний поиск.
  Файлы: `SWEEP_PROTOCOL_PHASE1.md`, `CONTEXT_PREAMBLE_PHASE1.md`, `phase1_repack_keys.json`,
  `workflows/phase1_{corpus_sweep,synthesis,external_research,resume_after_limit}.js`;
- **самопроверка Phase 1**: шесть линз adversarial review, затем исправления по потокам (решение D-14).
  Файлы: `workflows/phase1_adversarial_review.js`, `workflows/phase1_review_fixes.js`. Находки и журналы —
  PRIVATE `11_evidence_vnext/receipts/review_phase1/` и `canonical/<STREAM>/FIX_LOG.csv`.

Протоколы и workflow-скрипты — это данные прогона: в них остались пути облачной VM. Код инструментов (`tools/*.py`)
путей машины не содержит и читает корни из окружения (`tools/_roots.py`).

## Корни

| Переменная | Что | По умолчанию |
|---|---|---|
| `VKM_PUB` | клон PUBLIC | — (обязательна для `publish_reports.py`) |
| `VKM_RESOURCES_ROOT` | клон PRIVATE | — (обязательна) |
| `VKM_WORK` | рабочий каталог вне репозиториев (тексты страниц `corpus/`, выходы) | — (обязательна) |
| `VKM_SWEEP_DIR` | выходы чтецов `<SID>/<chunk>/` | `$VKM_RESOURCES_ROOT/11_evidence_vnext/sweep_raw` (закоммиченная копия) |
| `VKM_MERGED_DIR` | объединённые записи и проверки | `$VKM_WORK/merged` |
| `VKM_SYNTH_DIR` | каталоги потоков синтеза `<STREAM>/` | `$VKM_RESOURCES_ROOT/11_evidence_vnext/canonical` |

`localize_paths.sh` пишет копии протоколов и workflow-скриптов с локальными путями в `$WORK/run/`, а также
`$WORK/run/roots.env`. Отслеживаемые файлы он не меняет.

## Воспроизведение объединения sweep Phase 1

Нужны тексты страниц `$VKM_WORK/corpus/<SID>/pNNNN.txt` (`tools/extract_corpus_text.py` из бинарников PRIVATE после
`git lfs pull`) и постраничный OCR `$VKM_RESOURCES_ROOT/work/ocr/<SID>/` (`tools/split_ocr_all.py` из
`00_registry/cloud_checkpoint_2026-09-26/`).

```bash
export VKM_PUB=... VKM_RESOURCES_ROOT=... VKM_WORK=... VKM_MERGED_DIR=$VKM_WORK/merged
python tools/merge_sweep.py            # all_records.jsonl, kind_*.csv, merge_summary.json, parse_errors.json
python tools/quote_check_v2.py         # quote_check_v2.csv
python tools/build_coverage_master.py  # SOURCE_COVERAGE_MASTER.csv (поправки страниц и отложенных вопросов — в сборщике)
python tools/batch_loss_audit.py       # batch_loss_audit.csv
python tools/status.py                 # 81 чанков, 81 DONE
```

Проверено 26.09.2026 в cloud VM: результат совпадает побайтово с закоммиченными PRIVATE
`11_evidence_vnext/merged/{merge_summary.json, quote_check_v2.csv, SOURCE_COVERAGE_MASTER.csv, batch_loss_audit.csv}`.
Также совпадает `all_records.jsonl` рабочего каталога, в git он не входит. `parse_errors.json` отличается только
абсолютным путём входного файла (24 записи эквивалентны).

Сборщики отдельных потоков синтеза лежат рядом с каталогами в PRIVATE `11_evidence_vnext/canonical/<STREAM>/`
(`build/`, `scripts/`, `_build/`, `defs/`). Это квитанции прогона: пути VM в них не переписаны.
Для повторного запуска на другой машине их копируют и заменяют корни так же, как в `localize_paths.sh`.

После самопроверки `build_coverage_master.py` получил оверлей поправок страниц (COVERAGE_DUPLICATES-002) и пометки
вопросов, закрытых в другом чанке (COVERAGE_DUPLICATES-011). Пересборка и её хеши —
PRIVATE `11_evidence_vnext/receipts/coverage_master_rebuild_2026-09-26.txt`. Уровни покрытия не изменились.

## Публикация отчётов в PUBLIC

```bash
VKM_PUB=... VKM_RESOURCES_ROOT=... python tools/publish_reports.py --all
```

`--all` публикует все 18 отчётов Phase 1:

- 12 отчётов синтеза — в `docs/science/`;
- 6 отчётов внешнего поиска — в `docs/science/external/EXTERNAL_RESEARCH_*`.

Ссылки на опубликованные каталоги переписываются в относительные пути PUBLIC. Имя файла, которое встречается
в нескольких потоках (`FIX_LOG.csv`), разрешается сначала внутри своего потока. Пути машины заменяются
логическими корнями.

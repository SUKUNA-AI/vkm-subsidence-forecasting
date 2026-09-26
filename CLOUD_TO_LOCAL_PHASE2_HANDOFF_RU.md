# CLOUD → LOCAL: передача Phase 2 (вычисления на workstation)

Дата: 26.09.2026.

- Составлено в cloud-сессии «CLOUD ULTRACODE — продолжение scientific reset».
- Phase 1 (cloud) — информация и архитектура: сплошное чтение корпуса, каталоги evidence, WorldSpec-схема,
  фундамент кода, документы.
- Этот документ — вход для Phase 2 на локальной workstation.
- Текущее состояние науки — [PROJECT_STATE_RU.md](PROJECT_STATE_RU.md).
- Итог cloud-run — `docs/reset_2026_09/CLOUD_ULTRACODE_PHASE1_FINAL_REPORT_RU.md`.

## 1. Где что лежит

| Репозиторий | Ветка | Что |
|---|---|---|
| PUBLIC `SUKUNA-AI/vkm-subsidence-forecasting` | `research/evidence-worldspec-reset` (PR в `main`) | код `src/vkm_world`, схема, public-safe каталоги `evidence/`, `catalogues/`, документы |
| PRIVATE `SUKUNA-AI/vkm-subsidence-forecasting_resourses` | `research/evidence-worldspec-reset` (PR в `main`) | `11_evidence_vnext/`: сырые выходы sweep, merged-записи, канонические каталоги с цитатами, receipts |
| PUBLIC | `legacy` = `d54025d` | вся старая архитектура (v2.1, Gate A/B/C, PW v1, OGS-пакеты) — не трогать |

Тяжёлый объединённый файл `all_records.jsonl` (13 572 записи, 22 МБ) в git не входит.
Он пересобирается детерминированно из `11_evidence_vnext/sweep_raw/` (§ 3.3).

## 2. Первые действия на workstation

### 2.1 Окружение

```bash
git clone https://github.com/SUKUNA-AI/vkm-subsidence-forecasting.git
git clone https://github.com/SUKUNA-AI/vkm-subsidence-forecasting_resourses.git
# пока PR не слиты, main в обоих репозиториях — состояние до reset: нужна ветка Phase 1
cd vkm-subsidence-forecasting_resourses && git checkout research/evidence-worldspec-reset
git lfs install --local && git lfs pull && cd ..
cd vkm-subsidence-forecasting && git checkout research/evidence-worldspec-reset
python -m venv .venv && . .venv/bin/activate            # Python 3.13
pip install -r requirements/worldspec.lock.txt && pip install -e .
python -m pytest -q tests/world
VKM_RESOURCES_ROOT=../vkm-subsidence-forecasting_resourses python scripts/verify_canonical_repository.py
```

Без `git lfs pull` бинарники PRIVATE остаются LFS-указателями. Верификатор тогда их не сверяет: пишет WARN
«LFS pointers skipped» и завершается с кодом 0. Строгого режима (`--require-external`) пока нет, поэтому
перед проверкой всегда нужен `git lfs pull`.

### 2.2 Действия, которые cloud выполнить не смог (политика среды → 403)

1. **Push тегов** frozen-релизов (созданы локально в cloud, на GitHub не отправлены):

   ```bash
   git tag -a legacy/final         d54025d4c47b79b864076a33a4ecf877174ec922 -m "pre-reset architecture"
   git tag -a frozen/gate-b3       ce8e57ee3dfd82579fb00c8ab5d17bf1b925c9a4 -m "Gate B3"
   git tag -a frozen/scenario-v2.1 71d705d092403a8aae3a5300dec57938df11ad02 -m "scenario v2.1"
   git tag -a frozen/scenario-v2   ffcf86972d31bd9f0dbd62d1265001f5c722e253 -m "scenario v2"
   git tag -a frozen/r1            c5da1103154c29e2ffe9c52bbceacaa032f1690f -m "representation R1"
   git tag -a frozen/r2            2794c877369e6fc77629a7a6933e8869e8233bcc -m "representation R2"
   git tag -a archive/v3.2-last    10452b011b1e9685efe8de29ceb22c18f5e64c30 -m "last v3.2 state"
   git push origin --tags
   ```

   Затем на GitHub защитить ветку `legacy` (branch protection).
   В PRIVATE тегов нет. Якорь V23 валидатора PE v1 (`3a5761a`) и версии `pw2d_generator.py` достижимы только из
   `origin/claude/eloquent-goodall-ka7n0u`. Поставить теги до любой чистки веток:

   ```bash
   git tag -a archive/eloquent-goodall-ka7n0u 3a5761a -m "PE v1 validator V23 anchor"
   git tag -a pe-v1-pass4 9b53896 -m "physical_evidence_v1 pass 4"
   git push origin --tags
   ```

2. **LFS.** Cloud не индексировал файлы под LFS-паттернами. OCR первой сессии закоммичен как обычный текст
   (`00_registry/cloud_checkpoint_2026-09-26/`). Если нужны растры рендеров или сканы, коммитить их с workstation.
   OCR-архив с пословной уверенностью (объект `bd983e29…`, локальный коммит `292655a` cloud VM) не переносится.
   При необходимости его пересчитывают: tesseract 5.3.4 rus, 300 dpi.
3. **Рендеры визуальных проверок** — рабочие PNG cloud (`$VKM_WORK/run/img`). В git они не вошли,
   кроме `11_evidence_vnext/figures/musikhin/`. Каталоги ссылаются на них по имени.
   Воспроизводятся `docs/reset_2026_09/run_kit/tools/render_page.py <SID> <page>`.

## 3. Воспроизведение Phase 1

### 3.1 Каталоги PUBLIC из PRIVATE

```bash
VKM_RESOURCES_ROOT=../vkm-subsidence-forecasting_resourses python scripts/build_public_catalogues.py
git diff --stat evidence catalogues      # должно быть пусто: сборка детерминирована
```

### 3.2 Схема

```bash
python scripts/export_worldspec_schema.py && git diff --exit-code schemas/
```

### 3.3 Объединённые записи sweep

Переносимые инструменты Phase 1 лежат в `docs/reset_2026_09/run_kit/tools/`; порядок и переменные описаны в
[run_kit/README_RU.md](docs/reset_2026_09/run_kit/README_RU.md). Версии «как запускалось в VM» — квитанции
в PRIVATE `11_evidence_vnext/receipts/tools/`.

Порядок:

1. `merge_sweep.py` — объединение и дедупликация сырых выходов (`11_evidence_vnext/sweep_raw/`);
2. `quote_check_v2.py` — вторая проверка цитат;
3. `build_coverage_master.py` — мастер-таблица покрытия;
4. `batch_loss_audit.py` — аудит потерянных пачек.

В cloud VM цепочка воспроизведена побайтово: 13 572 записи, 24 точных дубликата отброшены, счётчики совпадают
с `11_evidence_vnext/merged/merge_summary.json`. Подробности — в
[REPRODUCIBILITY_REPORT_RU.md](docs/reset_2026_09/REPRODUCIBILITY_REPORT_RU.md).

## 4. Что исследовано полностью

- **Корпус.** 39 документов прочитаны сплошь: 65 чтений, 81 чанк, 13 572 записи.
  - Покрытие: 39 FULLY_REVIEWED; VKM-SRC-013 — SUPERSEDED_BY_COPY (его содержание прочитано через VKM-SRC-025);
    VKM-SRC-022 — RETIRED_NOT_EVIDENCE.
  - Статуса UNSEEN нет.
  - Таблица: `evidence/sources/SOURCE_COVERAGE_MASTER.csv` (по каждому источнику: страницы, главы, таблицы, рисунки,
    домены, число записей, метод просмотра, основание уровня покрытия).
- **Книги, учебные пособия и ВКР** прочитаны как полноценные научные источники.
  - Учебные примеры помечены TEACHING_EXAMPLE и не становятся фактами о СКРУ-1 (решение D-02).
- **Граф цитирований.** 2091 ребро, 15 семей источников, 257 целей поиска первоисточников (P1 — 14, P2 — 20).
  - Файлы: `evidence/sources/`, отчёт [CITATION_GRAPH_RU.md](docs/science/CITATION_GRAPH_RU.md).
  - Корпус «мелкий»: ключевые первоисточники лежат вне него. Это главный источник внешних запросов.

## 5. Что передаётся по доменам

### 5.1 Скважины — `evidence/boreholes/`

- **Каталог.** `borehole_catalog.csv` — 1110 строк.
  - Одна строка на скважину в контексте нумерации (43 контекста). Упоминания объединялись по пяти правилам
    идентичности (отчёт, § 1). Из 273 объединений 101 имеет `identity_confidence = MEDIUM`: основание, как правило, —
    воспроизведение одного рисунка в разных книгах. Прежде чем использовать объединённую строку в 3D, проверьте
    уверенность идентичности.
  - Типы: 676 разведочных, 55 нефтяных, 39 геотехнических, 39 подземных, 197 неизвестного типа, 16 безымянных групп и др.
  - С рудником «СКРУ-1» явно связаны 42 строки.
- **Отбивки.** `borehole_picks.csv` — 714 отбивок «как напечатано».
  - По типу опоры: 476 — глубина от устья, 72 — абсолютные отметки, 103 — только мощность, 63 — опора неизвестна.
  - Конвенция табл. 3.1 Лебедевой (кровля или почва; от земли или от стола ротора) не решена: ISS-PICK-CONV-T31.
- **Вопросы идентичности.** `borehole_identity_issues.csv` — 241.
  - 154 — одинаковый номер в разных контекстах;
  - 46 — упоминания без привязки;
  - прочие — варианты чтения и суффиксов, дубль подписи «129».
- **Пригодность для 3D.**
  - `usable_for_3d`: 997 NO, 113 PARTIAL, YES — ни одной, потому что напечатанных координат устьев в корпусе нет.
  - Плановые положения 31 скважины — оцифровка подписей карт Лебедевой в безымянной сетке (DERIVATION, ±150–200 м).
- Отбивки, относящиеся к СКРУ-1, есть у 5 скважин у целика СКРУ-1/СКРУ-2.
- Скв. 75 — обычная строка каталога, не представитель рудника (D-05).
- Отчёт: [BOREHOLE_AND_3D_RECONSTRUCTION_RU.md](docs/science/BOREHOLE_AND_3D_RECONSTRUCTION_RU.md).

### 5.2 Хронология и закладка — `evidence/mining/`, `evidence/backfill/`

- **Хронология.** `mining_chronology_catalog.csv` — 384 события: 237 физических, 147 информационных.
  - По руднику: СКРУ-1 прямо — 57; другие рудники ВКМ — 152; регионально — 90; аналоги — 34; не ВКМ — 17.
  - Даты — «как напечатано», с точностью. ISO-дата ставится только при точном печатном совпадении.
- **Конфликты хронологии.** `chronology_conflicts.csv` — 18 (CC-01…CC-18). Для СКРУ-1 важны:
  - CC-05 — провал 1995 г. у целика СКРУ-1/2;
  - CC-12 — карналлит на СКРУ-1;
  - CC-16/17 — зоны ГИС 101 и 88;
  - CC-18 — задержка закладки: десятилетия по ГИС против нормативных сроков.
- **Закладка.** `backfill_event_catalog.csv` — 184 строки.
  - По области: СКРУ-1 — 23, регионально — 103, прочие — остальные.
  - Для СКРУ-1 есть срез ГИС предприятия: 14 зон с годами отработки и закладки, факт закладки на 01.10.2022.
    Поблочных дат начала и окончания нет.
- **Иерархия.** `spatial_hierarchy.csv` — 636 узлов ВКМ → район → рудник → поле → панель/блок/зона → выработка/целик,
  плюс 173 разлома.
- **Геометрия.** `mining_geometry_catalog.csv` — 563 записи с метками DESIGN / ACTUAL / TEACHING_EXAMPLE /
  CALCULATED_BY_AUTHOR / UNRESOLVED.
- Отчёт: [MINING_ENGINEERING_EVIDENCE_RU.md](docs/science/MINING_ENGINEERING_EVIDENCE_RU.md).

### 5.3 WorldSpec — `src/vkm_world/`, `schemas/worldspec_vnext.schema.json`

- Дизайн описан в [WORLD_SPEC_VNEXT_RU.md](docs/worldspec/WORLD_SPEC_VNEXT_RU.md).
- Агрегат `WorldSpec` содержит:
  - CRS и трансформации;
  - пространственную иерархию, стратиграфию;
  - скважины, отбивки и интерпретации (наблюдение ≠ интерпретация);
  - горизонты, контакты, структуры;
  - горные объекты с их кросс-соответствиями, закладку, события;
  - материалы, процессы, математические модели;
  - системы, датасеты и операторы наблюдения;
  - явные UNKNOWN.
- `validate_world()` проверяет ссылки, иерархию, хронологию, порядок толщ и регистрацию источников.
- Статус мира `DESIGN` → `EVIDENCE_POPULATED` (P2-01) → `DIAGNOSTIC` (интерполяции) → `FROZEN`.

### 5.4 Физика и причинность — `catalogues/physics/`, `catalogues/causal/`, `catalogues/observations/`

- **Процессы.** 72 процесса PC-01…PC-72 с планируемым статусом исполнения:
  - COMPUTED_3D 17, OBSERVATION_OPERATOR 14, RESEARCH_REQUIRED 8, COMPUTED_LOCAL 7, CONTEXT_ONLY 7 и др.
- **Готовность процессов:**
  - READY_FOR_LOCAL — 7: PC-01 собственный вес, PC-02 σv(z), PC-18 длительная прочность, PC-60 нивелирование,
    PC-61 горизонтальные деформации, PC-63 InSAR, PC-72 лабораторные испытания;
  - EVIDENCE_WEAK 23, PARAMETERS_MISSING 21, GEOMETRY_MISSING 13, NOT_APPLICABLE_NORMAL_SCENARIO 8.
- **Причинный граф.** 110 узлов, 241 ребро, валидирован.
- **Операторы наблюдения.** 24 дизайна OO-01…OO-24 (DESIGN).
- Отчёты: [PHYSICS_COVERAGE_RU.md](docs/science/PHYSICS_COVERAGE_RU.md), [CAUSAL_GRAPH_RU.md](docs/science/CAUSAL_GRAPH_RU.md).

### 5.5 Формулы — `catalogues/mathematics/`

- **Реестр.** 276 моделей, у каждой есть источник и локатор.
  - Классы: ALGEBRAIC 148, STATISTICAL 41, GEOMETRIC 15, ODE 14, OTHER 11, EMPIRICAL_TABLE 10, HEREDITARY 6,
    INTEGRAL 6, INVERSE 5, PDE_SYSTEM 4, PDE_ELLIPTIC 4, PDE_PARABOLIC 3, GREEN_FUNCTION 2 и др.
- **Конфликты форм.** 38 (`formula_conflicts.csv`). Пример: операция LOS → вертикаль — умножение или деление на cos θ.
- Отчёт: [MATHEMATICAL_FOUNDATION_CATALOGUE_RU.md](docs/science/MATHEMATICAL_FOUNDATION_CATALOGUE_RU.md).

### 5.6 Механика, реология, гидро, термо, геофизика, мониторинг

- Механика: 1720 записей, из них 1415 LAB.
- Реология: 56 законов; закона, откалиброванного на СКРУ-1, нет.
- Интерфейсы и повреждение: 486 записей.
- Гидро: 155, термо: 41, сейсмо: 61, георадар: 111 (данных GPR по СКРУ-1 нет).
- Мониторинг: 489 позиций по модальностям.
  - Численные значения в корпусе есть у меньшинства: 99 — текст, 23 — таблицы, 32 — приближённая оцифровка графиков.
  - Остальное — описания методов.
- Отчёты — в [docs/science/](docs/science/).

## 6. Нерешённые вопросы

### 6.1 Пространственные

- **CRS и высоты.** Система координат СКРУ-1 и высотная система отметок — UNKNOWN.
  - Есть две несвязанные местные сетки: карты Лебедевой и центроиды ГИС в ВКР Филатовой.
  - Ориентация осей и единицы не указаны.
  - Отчёт: [COORDINATE_DATUM_AUDIT_RU.md](docs/science/COORDINATE_DATUM_AUDIT_RU.md).
- **Скважины:**
  - ISS-NO-PRINTED-XY — напечатанных координат устьев нет;
  - ISS-129-DUP — две подписи «129» на плане СКРУ-1;
  - ISS-MAP135-LEGEND — одна карта с разной легендой;
  - ISS-14XX — 1401/1403/1404 без позиций; трилатерация отложена;
  - ISS-131-131C;
  - ISS-PICK-CONV-T31.
- **Горные работы.** Пространственные конфликты MC-SP-01…15:
  - отнесение СКРУ-1 к участку детальной разведки;
  - число стволов, глубина ствола 2-бис;
  - номенклатура панелей 1992 vs 2013 (нужна `NomenclatureCrosswalk`);
  - мощность ВЗТ, приписанная СКРУ-1.

### 6.2 OCR и визуальные значения

- Журнал визуальной проверки и поправки: `evidence/qa/` (после завершения потока OCR_VISUAL_QA).
- Итог и нерешённое — в финальном отчёте Phase 1, раздел 22.
- Числа из строк с quote_check NOT_FOUND (500) и OCR_TEXT_MISMATCH (1025) перед использованием проверяются по рендеру.

### 6.3 Атрибуция

Каждый набор данных проверяется на рудник:

- рис. 10 ВКР Филатовой относится к СКРУ-2;
- линии № 1 и № 7 проходят через целик СКРУ-1/2 и атрибутированы частично;
- «Соликамск» ≠ СКРУ-1.

## 7. Архитектура

- [REPOSITORY_ARCHITECTURE_RU.md](docs/architecture/REPOSITORY_ARCHITECTURE_RU.md) — два репозитория, поток PRIVATE → PUBLIC,
  структура `src/vkm_world`.
- [PHASE1_DESIGN_DECISIONS_RU.md](docs/governance/PHASE1_DESIGN_DECISIONS_RU.md) — решения D-01…D-11.
- Зарезервировано место для `src/vkm_world/solver_adapters/`. Контракт: адаптер получает срез WorldSpec и возвращает
  производный объект с провенансом. Мир адаптер не перезаписывает.

## 8. Backlog Phase 2

Общее правило: любой производный объект — новая запись со статусом INTERPOLATION или DERIVATION, с входами,
методом и списком MODEL_CHOICE. Исходные записи не перезаписываются. Каждый шаг оставляет receipt.

### 8.0 Мир из evidence (сначала)

| # | Задача | Критерий готовности |
|---|---|---|
| P2-01 | Сборщик `world/` → `WorldSpec(status=EVIDENCE_POPULATED)` из канонических каталогов, без интерполяций. Сборщик применяет поправки `evidence/qa/evidence_corrections.csv` | `validate_world(registered_sources)` = 0 ошибок; детерминированный JSON; content hash в receipt |
| P2-02 | Ревизия автоклассов: геология RULE_BASED, механика и интерфейсы tier B; строки NOT_FOUND / OCR_TEXT_MISMATCH, влияющие на числа | доля ошибок оценена; критичные строки исправлены с receipt |
| P2-03 | Геопривязка: связать две местные сетки через общие объекты (граничные скважины 109/74/232/221, стволы). CRS остаётся UNKNOWN до ответа предприятия | `CoordinateTransform` со статусом DERIVATION и оценкой ошибки |

### 8.1 Математика (исполнение реестра `catalogues/mathematics/`)

| Задача | Что конкретно |
|---|---|
| Размерная проверка | все 276 моделей: колонка `variables` ↔ единицы, согласование систем (сут⁻¹ vs с⁻¹; МПа vs кгс/см²). Конфликты форм — `formula_conflicts.csv` |
| Анализ ОДУ | 14 моделей ODE (законы ползучести MM-CREEP-001/003/005/010/011/014/015/018 …): устойчивость, асимптоты, области применимости по напряжению и температуре |
| Кривые реологии | интегрирование законов при постоянной нагрузке по лабораторным параметрам. Сравнение с опубликованными кривыми (сначала воспроизвести, потом использовать) |
| Интегралы | 6 моделей INTEGRAL (MM-ELAST-004, MM-GEOMECH-003, MM-FRAC-002, MM-HYDRO-002/005, MM-RISK-002); функции Грина MM-ELAST-001, MM-PDE-013 |
| Проверка постановок PDE | MM-PDE-001…013, MM-GEOMECH-001/002/004: граничные и начальные условия, согласованность с WorldSpec |
| Наследственные ядра | 6 моделей HEREDITARY (ядро Абеля MM-CREEP-102, MM-CREEP-017, MM-CONTACT-005): единицы δ, α; сверка с первоисточником (внешний поиск был ограничен сетью) |
| Воспроизведение опубликованных расчётов | типовые кривые оседаний ВКМ S(z), ξ0 = a·η0 (конфликт a = 0,2–0,26 vs m_ξ = 0,4), примеры из нормативов и учебников |
| Неопределённость | распространение неопределённости, анализ чувствительности, Monte Carlo по дискретным гипотезам: λ ∈ {0,45; 0,6; 0,71; 1,0}, закон ползучести, LAB→MASSIF |

### 8.2 Геостатистика

| Задача | Что конкретно |
|---|---|
| Вариограммы | по единицам (кровли КрII, АБ, ПКС, СМТ и мощности) по `borehole_picks.csv` с учётом типа опоры. Только после P2-03 |
| Кригинг | обычный или универсальный, с картами дисперсии кригинга |
| Условное моделирование | SGS-ансамбли поверхностей как вход неопределённости геометрии |
| LOO-CV | метрики по каждой поверхности; результат — `HorizonRepresentation(INTERPOLATED_SURFACE)` |

### 8.3 3D-реконструкция

| Задача | Что конкретно |
|---|---|
| GemPy | неявное моделирование по отбивкам и ориентациям; разломы из каталога |
| Micromine | если есть лицензия — сравнение с GemPy |
| Альтернативная интерполяция | RBF / IDW как контроль чувствительности к методу |
| Поверхности и контуры | горизонты, контуры отработки и закладки по зонам ГИС (DERIVATION) |
| Визуализация | 3D (например PyVista) как производный продукт, не как мир |

### 8.4 Физика

| Задача | Что конкретно |
|---|---|
| Собственный вес, начальное состояние | PC-01, PC-02 (READY_FOR_LOCAL); σh по сценариям λ (D-04), без усреднения |
| Напряжения у камер и целиков | геометрия из `mining_geometry_catalog.csv` (только DESIGN/ACTUAL, не TEACHING) |
| Горно-геометрические расчёты | коэффициент извлечения, степень нагружения по пластам и блокам |
| Закладка | MM-BACKFILL-*; степень заполнения, задержки; ГИС-зоны СКРУ-1 |
| OpenGeoSys | лестница из `CLAUDE.md`. Регрессионный набор — TOY-кейсы 00–07 предыдущего релиза (`LEGACY_PW_OGS_REVIEW_RU.md`, п. 3), адаптер к срезу WorldSpec |
| MATLAB, PLAXIS / ANSYS / FLAC | при наличии лицензий — сверка с OGS на toy-кейсах |

### 8.5 Геофизика

| Задача | Что конкретно |
|---|---|
| gprMax | низкий приоритет: GPR по СКРУ-1 нет. Только методическая проверка с диэлектрическими свойствами из `gpr_evidence_catalog.csv` |
| Сейсмика | прямые и обратные задачи — только если появятся данные по СКРУ-1; сейчас контекст |

### 8.6 Наблюдения и бенчмарк

| Задача | Что конкретно |
|---|---|
| Операторы наблюдения | OO-01…OO-24 из DESIGN → IMPLEMENTED с тестами на синтетике. Решить конфликт знака LOS; нулевые эпохи, классы точности |
| Бенчмарк (отдельный gate) | предрегистрация: t0, горизонты, разбиения по времени с `available_from ≤ t0`. Поле «ОСЕДАНИЯ_2022» не является наблюдением и признаком |

## 9. Запросы данных (снимают больше неопределённости, чем любая обработка корпуса)

1. Система координат и высот рудника СКРУ-1, параметры перехода к МСК-59 / ГСК-2011 (маркшейдерская служба).
2. Каталог координат и отметок устьев солеразведочных скважин СКРУ-1/2/3 (фонды Уралкалия, ГИ УрО РАН, ВНИИГ).
3. База отбивок и проект Petrel модели Лебедевой: табл. 3.1 с опорной поверхностью, LAS 14xx.
4. Поскважинная таблица SRC-025 табл. 4.10: по СКРУ-1 45 скважин КрII и 23 АБ.
5. Поблочная хронология выемки и закладки СКРУ-1: журналы, ГИС-слои с датами.
6. Цифровые ряды нивелирования профильных линий СКРУ-1 (линия 12 и другие) с классом точности и нулевой эпохой.
7. Оригинал плана СКРУ-1 с координатной сеткой (две подписи «129»).
8. Работа Дубининой 1960 г. и паспорт скв. 75.
9. Первоисточники, недоступные из cloud. Список целей — `evidence/sources/primary_source_targets.csv`.
   Результат внешнего поиска по ним — `evidence/external/target_resolution.csv`. Искать через библиотеку или научные
   порталы с workstation нужно цели без найденного источника и цели с доступом METADATA_ONLY / ABSTRACT_ONLY /
   PARTIAL_TEXT_QUERY / NOT_FOUND / PAYWALL (`evidence/external/external_sources.csv`, колонка `access`).
   Списки «что не удалось открыть» есть в конце каждого отчёта `docs/science/external/`.

## 10. Известные ограничения Phase 1

- Классы части строк (геология RULE_BASED, механика и интерфейсы tier B) назначены правилами по ключевым словам.
- 500 цитат не найдены в текстовом слое (NOT_FOUND). 1025 не совпадают с OCR (OCR_TEXT_MISMATCH).
- Графические значения — «как прочитано», с погрешностью чтения.
- Внешний поиск (131 источник) был ограничен:
  - до исчерпания кредитов Firecrawl полностью прочитано 18 источников (FULL_TEXT_READ), 6 — частично
    (PARTIAL_TEXT_QUERY: первые страницы или ответы на запросы);
  - затем научные домены оказались закрыты egress-политикой окружения, и работал только WebSearch:
    24 ABSTRACT_ONLY, 73 METADATA_ONLY, 9 NOT_FOUND, 1 PAYWALL;
  - утверждения, взятые только из машинной сводки поисковика, помечены как наводки (confidence LOW).
- Сборщики потоков в PRIVATE `canonical/<STREAM>/` содержат пути cloud VM.
  Воспроизведение описано в `docs/reset_2026_09/run_kit/README_RU.md`.

## 11. Правила, которые нельзя нарушать в Phase 2

См. [CLAUDE.md](CLAUDE.md), [SCIENTIFIC_RULES_RU.md](docs/governance/SCIENTIFIC_RULES_RU.md),
[VALIDATION_POLICY_RU.md](docs/governance/VALIDATION_POLICY_RU.md). Коротко:

- UNKNOWN остаётся UNKNOWN; нет данных ≠ ноль; нет информации ≠ однородность.
- LAB ≠ MASSIF; аналог ≠ СКРУ-1; MODEL_CHOICE ≠ FACT; 2D ≠ весь 3D-участок.
- Без утечки из будущего и без калибровки по test truth.
- Без force-push; `legacy` и frozen-релизы не трогать.

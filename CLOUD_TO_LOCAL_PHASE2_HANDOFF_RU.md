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
cd vkm-subsidence-forecasting_resourses && git lfs install --local && git lfs pull && cd ..
cd vkm-subsidence-forecasting && git checkout research/evidence-worldspec-reset
python -m venv .venv && . .venv/bin/activate            # Python 3.13
pip install -r requirements/worldspec.lock.txt && pip install -e .
python -m pytest -q tests/world
VKM_RESOURCES_ROOT=../vkm-subsidence-forecasting_resourses python scripts/verify_canonical_repository.py
```

Без `git lfs pull` проверка PRIVATE-регистра ложно падает: вместо бинарников остаются LFS-указатели.

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
2. **LFS.** Cloud не индексировал файлы под LFS-паттернами. OCR первой сессии закоммичен как обычный текст
   (`00_registry/cloud_checkpoint_2026-09-26/`). Если нужны растры рендеров или сканы, коммитить их с workstation.
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

Инструменты Phase 1 лежат в PRIVATE `11_evidence_vnext/receipts/tools/` и в PUBLIC `docs/reset_2026_09/run_kit/`.
Порядок:

1. `merge_sweep.py` — объединение и дедупликация сырых выходов;
2. `build_coverage_master.py` — мастер-таблица покрытия;
3. `batch_loss_audit.py` — аудит потерянных пачек.

Корни задаются переменными `VKM_PUB`, `VKM_RESOURCES_ROOT` и `VKM_WORK`. Эталонные счётчики —
`11_evidence_vnext/merged/merge_summary.json`: 13 572 записи, 24 точных дубликата отброшены.

## 4. Backlog Phase 2 (по порядку)

Правило для всех пакетов: любой производный объект — новая запись со статусом INTERPOLATION или DERIVATION,
с входами и списком MODEL_CHOICE. Исходные записи не перезаписываются.

| # | Пакет | Что сделать | Критерий готовности |
|---|---|---|---|
| P2-01 | Мир из evidence | Написать сборщик `world/` → `WorldSpec(status=EVIDENCE_POPULATED)` из канонических каталогов: иерархия (636), стратиграфия (172), скважины и отбивки (1110/714), горные объекты, хронология (384), закладка (184), материалы, процессы (72), математические модели, системы и датасеты наблюдений, операторы (24), UNKNOWN-пробелы. Без интерполяций | `validate_world(registered_sources)` = 0 ошибок; детерминированный JSON; content hash в receipt |
| P2-02 | Ревизия автоклассов | Выборочно (стратифицированно) просмотреть строки с `basis_assignment=RULE_BASED` (геология) и tier B (механика, интерфейсы). Отдельно просмотреть записи quote_check NOT_FOUND (500) и OCR_TEXT_MISMATCH (1025), влияющие на числа | доля ошибок оценена; критичные строки исправлены с receipt |
| P2-03 | Геопривязка | Совместить две локальные сетки СКРУ-1 (карты Лебедевой и центроиды ГИС Филатовой) по общим объектам. Кандидаты опорных точек — граничные солеразведочные скважины 109/74/232/221 и стволы | трансформация как `CoordinateTransform` со статусом DERIVATION и оценкой ошибки; CRS остаётся UNKNOWN до ответа предприятия |
| P2-04 | Математика | Для моделей реестра воспроизвести опубликованные числовые примеры; проверить размерности и единицы; зафиксировать расхождения с первоисточниками | каждая модель: REPRODUCED / DISCREPANCY / NOT_REPRODUCIBLE с receipt |
| P2-05 | Геостатистика | Поверхности кровли и почвы КрII, АБ, ПКС и мощности по отбивкам (с учётом конвенций табл. 3.1). Вариограммы, кригинг, LOO-CV, карты неопределённости | `HorizonRepresentation(INTERPOLATED_SURFACE)` с методом, входами и метриками LOO-CV; наблюдения не изменены |
| P2-06 | 3D-реконструкция | Каркас горизонтов, контуры отработки и закладки из ГИС-зон, сетки как производные представления | derived view + receipt; мир не перезаписан |
| P2-07 | Операторы наблюдения | Нивелирование (класс точности, нулевые эпохи), InSAR LOS (решить конфликт знака проекции), модель ошибки, сезонность | операторы OO-xx из DESIGN → IMPLEMENTED с тестами на синтетике |
| P2-08 | Решатели | OGS: установка, лестница из `CLAUDE.md`, адаптер к срезу WorldSpec. Реология — ANALOGUE с явным Transfer; λ — перебор дискретного набора гипотез | toy-кейсы проходят; адаптер пишет результат как DERIVATION с MODEL_CHOICE |
| P2-09 | Неопределённость | Monte Carlo по дискретным гипотезам (λ, закон ползучести, LAB→MASSIF) и диапазонам; анализ чувствительности | распределения выходов с провенансом сценариев |
| P2-10 | gprMax (низкий приоритет) | Только если появятся данные GPR по СКРУ-1; сейчас их нет | — |
| P2-11 | Бенчмарк (отдельный gate) | Предрегистрация: t0, горизонты, разбиения по времени с `available_from ≤ t0`, запрет поля «ОСЕДАНИЯ_2022» как признака | протокол утверждён до первого запуска модели |

## 5. Запросы данных (снимают больше неопределённости, чем любая обработка корпуса)

1. Система координат и высот рудника СКРУ-1, параметры перехода к МСК-59 / ГСК-2011 (маркшейдерская служба).
2. Каталог координат и отметок устьев солеразведочных скважин СКРУ-1/2/3 (фонды Уралкалия, ГИ УрО РАН, ВНИИГ).
3. База отбивок и проект Petrel модели Лебедевой: табл. 3.1 с опорной поверхностью, LAS 14xx.
4. Поскважинная таблица SRC-025 табл. 4.10: по СКРУ-1 45 скважин КрII и 23 АБ.
5. Поблочная хронология выемки и закладки СКРУ-1: журналы, ГИС-слои с датами.
6. Цифровые ряды нивелирования профильных линий СКРУ-1 (линия 12 и другие) с классом точности и нулевой эпохой.
7. Оригинал плана СКРУ-1 с координатной сеткой (две подписи «129»).
8. Работа Дубининой 1960 г. и паспорт скв. 75.

## 6. Известные ограничения Phase 1

- Классы части строк (геология RULE_BASED, механика и интерфейсы tier B) назначены правилами по ключевым словам.
- 500 цитат не найдены в текстовом слое (NOT_FOUND). 1025 отличаются от OCR (OCR_TEXT_MISMATCH).
  Числа из таких строк перед использованием проверяются по рендеру страницы.
- Графические значения (оцифровка рисунков) — только «как прочитано», с погрешностью чтения.
- Ни одна скважина не получила `usable_for_3d = YES`: координат устьев нет.
- Внешний поиск первоисточников ограничен доступностью в сети. Недоступное помечено в `external_sources`.

## 7. Правила, которые нельзя нарушать в Phase 2

См. [CLAUDE.md](CLAUDE.md), [SCIENTIFIC_RULES_RU.md](docs/governance/SCIENTIFIC_RULES_RU.md),
[VALIDATION_POLICY_RU.md](docs/governance/VALIDATION_POLICY_RU.md). Коротко:

- UNKNOWN остаётся UNKNOWN; LAB ≠ MASSIF; аналог ≠ СКРУ-1; MODEL_CHOICE ≠ FACT;
- без утечки из будущего и без калибровки по test truth;
- без force-push; `legacy` и frozen-релизы не трогать.

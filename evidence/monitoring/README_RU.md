# Наблюдательная evidence: профили Мусихина (VKM-SRC-002, PDF с. 15)

Решение [D-07](../../docs/governance/PHASE1_DESIGN_DECISIONS_RU.md): оцифровка четырёх графиков со с. 15
слайдов Мусихина переходит в наблюдательную evidence со статусом **discovery-only**. Выдавать их за временные ряды
СКРУ-1 нельзя.

Кроме этой оцифровки, численные значения наблюдений есть в каталоге Phase 1 `monitoring_observation_catalog.csv`:
текстовые и табличные значения, приближённые оцифровки графиков. Их атрибуция и статус указаны построчно.

## 1. Файлы

| Файл | Строк | Что это |
|---|---:|---|
| `musikhin_vkm_src002_p15_profiles.csv` | 258 | одна строка на оцифрованную позицию графика: 4 линии × 2 метода × 1–2 интервала |
| `musikhin_modality_comparison.csv` | 7 | расхождение InSAR − нивелирование по общим позициям, для каждой пары (линия, интервал) |
| `musikhin_vkm_src002_p15_manifest.json` | — | receipt: входы (legacy-путь + SHA-256), выходы (SHA-256), счётчики, правила отображения, исправления подписей |

Сборка детерминирована: [`scripts/build_evidence_from_legacy.py`](../../scripts/build_evidence_from_legacy.py).
Входы скрипт читает из git-объектов legacy-commit `d54025d` (ветка `legacy`, тег `legacy/final`), без
checkout и без LFS, и проверяет SHA-256 каждого входа:

```bash
python scripts/build_evidence_from_legacy.py monitoring           # пересобрать
python scripts/build_evidence_from_legacy.py monitoring --check   # сравнить с закоммиченными файлами
```

## 2. Поля `musikhin_vkm_src002_p15_profiles.csv`

| Поле | Значение |
|---|---|
| `id` | `OBS-MUS-P15-<line>-<METHOD>-<nnn>`. `nnn` нумерует позиции внутри (линия, метод): сначала интервал 2011–2016, затем 2015–2016, внутри — по `position_index` |
| `source_id`, `locator` | `VKM-SRC-002`, `PDF p.15`. Панели графиков: линия 1 — `/Image157`, 5 — `/Image159`, 17 — `/Image161`, 6 — `/Image163` |
| `line` | подпись линии на графике: `1`, `5`, `17`, `6` |
| `method` | `levelling` (нивелирование) или `insar` (радарная интерферометрия, на слайде — «радарный мониторинг») |
| `interval_start`, `interval_end` | годы из подписи легенды: `2011`–`2016` или `2015`–`2016`. Точные даты циклов и общий репер неизвестны |
| `x_label_as_printed` | категориальная подпись оси X («Расстояние от первого репера, км») строкой, как напечатано. Повторы (линия 17) и пропуски (чётные позиции линии 6) сохранены. Это **не** метрическая координата |
| `value_mm` | смещение за интервал, мм, знак «минус — вниз», как на графике. Шаг считывания 0,1 мм. Пусто у нечитаемых позиций |
| `digitization_status` | `digitized_marker_or_visible_curve` / `partially_occluded_approximation` / `unresolved_occlusion` |
| `digitization_uncertainty_mm` | консервативная полуширина **погрешности считывания графика**. Это не σ нивелирования и не доверительный интервал |
| `status` | `FACT` (оцифровка опубликованного графика) при наличии значения; `UNKNOWN` у 5 нечитаемых позиций (значение не восстанавливается) |
| `scope` | `VKM_Solikamsk (SKRU-1 or SKRU-2 not proven)` |
| `evidence_note` | `discovery-only; not a time series` |
| `position_index` | порядковый номер позиции на оси X (1…n) |
| `reading_origin` | `atlas_digitization` или `independent_detailed_reread` (линия 1, нивелирование 2011–2016: 14 позиций перечитаны отдельно, этот отсчёт принят как контрольный) |
| `raster_x_px`, `raster_y_px` | координаты отсчёта в нативном растре панели: пиксели от нуля, начало в левом верхнем углу, y вниз. Растры хранятся в PRIVATE (раздел 5) |
| `legacy_point_id` | ID строки в legacy-таблице `canonical_digitization.csv` (для сверки с frozen-релизом) |

Счётчики: 258 позиций, 253 значения. Статусы считывания: 185 `digitized_marker_or_visible_curve`,
68 `partially_occluded_approximation`, 5 `unresolved_occlusion`. Атлас давал 252 значения. Детальное
перечитывание линии 1 восстановило позицию 12 нивелирования 2011–2016 (−314,0 мм, частично перекрыта).
Позиции 1–3 этой серии и позиции 7–8 линии 17 (нивелирование 2011–2016) остаются нечитаемыми.

Полуширины погрешности считывания различаются: 0,5–9,6 мм в атласе и 14,4/20,3 мм в детальном
перечитывании линии 1. Второе — более консервативная оценка той же процедуры. Повторное независимое
считывание линии 1 (5 позиций) даёт RMSE 0,63 мм, максимум 0,88 мм. Это повторяемость метода, а не
точность поля.

## 3. `musikhin_modality_comparison.csv`

Для каждой пары (линия, интервал) сравниваются позиции, где прочитаны обе модальности. Скрытые точки не
интерполируются. Поля: `n_paired_positions`, `insar_minus_levelling_mean_mm`, `_median_mm`,
`rms_disagreement_mm`, `max_abs_disagreement_mm`, `pearson_correlation`, `fraction_within_reading_bounds`
(доля пар с |Δ| ≤ сумма полуширин считывания). Статус `DERIVATION`.

Скрипт пересчитывает таблицу из `musikhin_vkm_src002_p15_profiles.csv`. Пересчёт совпадает с
legacy-таблицей `modality_comparison.csv` с точностью 1e-6. RMS расхождения — 5,8–41,8 мм, корреляция —
0,69–0,97.

Это расхождение **нарисованных** кривых двух модальностей. Это не ошибка нивелирования, не точность InSAR
и не независимая валидация. Таблица — материал для спецификации оператора наблюдения
(`ObservationOperatorSpec`: InSAR vs нивелирование).

## 4. Ограничения (обязательны при любом использовании)

1. **Discovery-only.** Источник — непрорецензированные слайды. В реестре PRIVATE VKM-SRC-002 помечен
   «discovery-only до нахождения первичной публикации профильных линий».
2. **Рудник не доказан.** Слайды относятся к району Соликамска: СКРУ-1 и/или СКРУ-2. Scope
   `VKM_Solikamsk`, не `SKRU1`.
3. **Не временной ряд.** Каждое значение — смещение за один интервал, подписанный годами. Интервалы
   2011–2016 и 2015–2016 нельзя вычитать друг из друга: общий репер и даты циклов неизвестны. Нельзя
   достраивать «историю» внутри интервала.
4. **Ось X категориальная.** Георефенса нет. Метрические градиенты и наклоны не вычисляются. Связи с
   реперами сети или с синтетическими точками P-H/P-V/P-D нет (legacy-проверка:
   `NO_NUMERIC_PROFILE_LINK_FOUND`).
5. **Погрешность считывания ≠ погрешность измерения.** Как σ поля её использовать нельзя.
6. **Модальности раздельны.** Нивелирование не является «истиной» для InSAR, и наоборот.
7. **Доступность информации.** Дата публикации слайдов не установлена. Для прогноза в момент t0
   `available_from` = UNKNOWN, пока нет свидетельства (правило D-03).

## 5. Провенанс

**Источник.** VKM-SRC-002 (в legacy — `SUP01`, файл `geokniga-02obrabotka.pdf`), SHA-256
`4d20f03c0c3a7bb7039c405e2aff2780a30d9f6e52e99fe5da50fa4c9ccbe947`, 3 576 355 байт, PDF с. 15.
Соответствие legacy-ID и VKM-SRC — в [`../sources/legacy_source_id_map.csv`](../sources/legacy_source_id_map.csv).

**Legacy-артефакты, из которых собраны таблицы** (commit `d54025d`; удалены из рабочего дерева `main`,
проверяются из git-объектов, см. [индекс legacy](../../docs/legacy/LEGACY_INDEX_RU.md)):

| Legacy-путь | SHA-256 | Роль |
|---|---|---|
| `artifacts/reconstruction/scenario_constraints_v2/canonical_digitization.csv` | `f33c22a8b6af4a8def6aeffc696cedc73434fb704fe174907a453c11f224abe0` | принятые отсчёты: значения, статусы, полуширины, пиксели |
| `artifacts/reconstruction/scenario_constraints_v2/modality_comparison.csv` | `a35539cb7c4e1f112d771ac5fae8faaacbee3ac2697dd9a7572f031cd87fde7e` | сверка пересчёта сравнения модальностей |
| `artifacts/reconstruction/scenario_constraints_v2/manifest.json` | `96b88b61cf766062c1f831e32b50bdd82441da63c50ce41569a5ffad297de6ef` | FROZEN_REFERENCE `SKRU1_SCENARIO_CONSTRAINTS_V2`; пинует оба CSV выше и таблицы ниже |
| `data/published_figure_digitization_v1/musikhin_profiles_digitization.csv` | `2fc8ac5cd0a40eff9c230e641fe5a3c1a7e1522d41dd989d9abf7eda4af02761` | замороженная ручная оцифровка: подписи оси X, сверка значений |
| `data/published_figure_digitization_v1/extraction_manifest.json` | `e8ce3f949feb747a55b99340a7491cb29e8f80546e8235e09e3dfc4727096278` | провенанс оцифровки и нативных растров |
| `artifacts/reconstruction/musikhin_line1_2011_2016_v1/points.csv` | `257f643f300a58fce6699cd9938292ae63f0958857b2a996f58b1db13d8274cb` | детальное перечитывание линии 1 (сверка) |
| `artifacts/reconstruction/musikhin_line1_2011_2016_v1/manifest.json` | `5081dc8ce463119774cb4a6e3ffdd121a1a38e9b3bd0cee75e0382dbb6ce9f06` | провенанс |
| `artifacts/reconstruction/musikhin_profiles_2011_2016_v1/manifest.json` | `7740a92603762c9fb7db3c264db95c257bcf28d635aea54a2775676956e2bac4` | провенанс |
| `artifacts/reconstruction/musikhin_profiles_2011_2016_v1/profile_1/points.csv` | `aa60e9e03de1ef5d4caa1a78962efb5287e2984cf17c3da5d0054fba1bb3d109` | отсчёты атласа (сверка) |
| `artifacts/reconstruction/musikhin_profiles_2011_2016_v1/profile_5/points.csv` | `63553b4642da045cbdf9c324574e96728bff5becae7c8f9005dbde9068c89f95` | отсчёты атласа (сверка) |
| `artifacts/reconstruction/musikhin_profiles_2011_2016_v1/profile_17/points.csv` | `425aa933d91e523941b576464ffb82cbb5cba462b9d0d5462940a801a191eac4` | отсчёты атласа (сверка) |
| `artifacts/reconstruction/musikhin_profiles_2011_2016_v1/profile_6/points.csv` | `cffccc80e392dde55522139528602ee024b86cf40fa59b8c19f91bc49f74bf27` | отсчёты атласа (сверка) |

**Преобразования** (все проверяются скриптом, при расхождении сборка останавливается):

- значения, статусы и полуширины взяты из `canonical_digitization.csv` без изменений. Для каждой строки
  проверено равенство исходной таблице: атласу или детальному перечитыванию линии 1;
- `leveling` → `levelling`, `radar` → `insar`; интервал `2011-2016` → `interval_start=2011`, `interval_end=2016`;
- поля `eligible_as_t1_time_series` и `mapped_synthetic_point_id` отброшены: во всех строках они были
  `False` и пусты, скрипт это проверяет. Поле `distance_value_from_label_km` отброшено, так как ось X
  категориальная;
- **исправление подписей оси X.** Замороженная оцифровка нормализовала часть подписей (конечные нули).
  26.09.2026 подписи сверены визуально с нативными растрами и записаны как напечатано:
  - линия 1, позиция 10: `1.0` → `1`;
  - линия 5, позиции 2, 4, …, 24: `0.10`→`0.1`, `0.20`→`0.2`, …, `1.00`→`1`, `1.10`→`1.1`, `1.20`→`1.2`.

  Подписи линий 17 (`0.0`, повторы) и 6 (подписаны только нечётные позиции) совпали с напечатанными.
  Полный список исправлений — в `musikhin_vkm_src002_p15_manifest.json`.

## 6. Воспроизводимость исходной оцифровки

- Атлас профилей (legacy `scripts/reconstruct_musikhin_profiles.py`, конфиг
  `configs/musikhin_profiles_v1.json`) **воспроизводим**. Из внешних файлов ему нужен только VKM-SRC-002;
  остальные входы — замороженный CSV оцифровки и `dataset_linkage.json` — лежат в `legacy`.
- Детальное перечитывание линии 1 (legacy `scripts/reconstruct_musikhin_line1.py`, конфиг
  `configs/musikhin_line1_digitization_v1.json`) **невоспроизводимо** из текущих входов. Скрипт читает
  retired v3.2: `SKRU1_ACTUAL_DATA_TABLES_v1/01_reconstruction_v3_2/metadata/reconstruction_config.json` и
  таблицы `survey_*`, а также `data/reconstruction_research_v1`. Его выходы проверяемы только по SHA-256
  из legacy-commit (FROZEN_REFERENCE, `scripts/frozen_references.json`).
- Эти таблицы (раздел 1) воспроизводимы в любом клоне, где есть объекты commit `d54025d`.

## 7. Растры и фигуры → PRIVATE

В PUBLIC не копируются ни растры (вырезки из PDF корпуса), ни производные фигуры. Они должны храниться в
PRIVATE `11_evidence_vnext/figures/musikhin/` с сохранением legacy-пути ниже этого каталога. Байты
доступны из legacy-commit `d54025d` по указанным путям. Идентичные по SHA-256 файлы отмечены повтором хеша.

| Legacy-путь | SHA-256 | Байт | Тип |
|---|---|---:|---|
| `data/published_figure_digitization_v1/source_profile_1_native.png` | `254b82c1eb8aecb1dbacd071257cbade8d69bfca628367af6f067bafc114ea97` | 59637 | нативный растр PDF (`/Image157`) |
| `data/published_figure_digitization_v1/source_profile_5_native.png` | `7ccbcd0bc9300472354f1e53cdc54851e682998be9d97b3463a8de6567936fb2` | 62192 | нативный растр PDF (`/Image159`) |
| `data/published_figure_digitization_v1/source_profile_17_native.png` | `bfef815b174e50db7add90a0debca2d9f4c06e2c6fdc0fded600a5e5b558eef0` | 51207 | нативный растр PDF (`/Image161`) |
| `data/published_figure_digitization_v1/source_profile_6_native.png` | `3d68aef28ce7c2398a2bed91cb6894bdcd95d7d0f894ede10b57e2ae2bcdf325` | 52950 | нативный растр PDF (`/Image163`) |
| `artifacts/reconstruction/musikhin_line1_2011_2016_v1/inputs/source_line1_native.png` | `254b82c1eb8aecb1dbacd071257cbade8d69bfca628367af6f067bafc114ea97` | 59637 | нативный растр PDF (= линия 1) |
| `artifacts/reconstruction/musikhin_line1_2011_2016_v1/source_fragment.png` | `5e7f3a6ba5a2bf71f17977297ea01f2fc431ac1b9273152a1208093e1d7a1246` | 58625 | вырезка страницы PDF |
| `artifacts/reconstruction/musikhin_line1_2011_2016_v1/source_overlay.png` | `4f277ed6714cc084bae02af5f3e2f67c4bb6946921ac07a515ef63020d14bd1f` | 352180 | наложение отсчётов |
| `artifacts/reconstruction/musikhin_line1_2011_2016_v1/reconstructed_profile.png` | `fd4af5cd2d021e179cdb348760cbd49e9416aa684b1b4c93607c269a80e934ad` | 107690 | построенный профиль |
| `artifacts/reconstruction/musikhin_line1_2011_2016_v1/comparison.png` | `8138e9b059f5136485c19c3b09e2cf70a7a44a5e41c1fb28545aad08d82aeda7` | 478199 | сравнение отсчётов |
| `artifacts/reconstruction/musikhin_profiles_2011_2016_v1/atlas.png` | `250724282cf7b7e8a8693d79b73d0ab657dcf0621bc02773baac6bddeaf9ac07` | 459918 | сводный атлас 4 линий |
| `artifacts/reconstruction/musikhin_profiles_2011_2016_v1/profile_1/source_fragment.png` | `5e7f3a6ba5a2bf71f17977297ea01f2fc431ac1b9273152a1208093e1d7a1246` | 58625 | вырезка страницы PDF |
| `artifacts/reconstruction/musikhin_profiles_2011_2016_v1/profile_1/source_overlay.png` | `d8fe902a8b459bb286b0c9b6fe491c3047dcec11fc1dfefbaaa3b9ff2bb00c08` | 266151 | наложение отсчётов |
| `artifacts/reconstruction/musikhin_profiles_2011_2016_v1/profile_1/reconstructed_profile.png` | `ee8384c7365dc98fa7f21b69cfed74eebe1821b0f7dce09c0434988451f4905c` | 140600 | построенный профиль |
| `artifacts/reconstruction/musikhin_profiles_2011_2016_v1/profile_1/comparison.png` | `6b06a38c6060a118cde5eb30470a30de71baf0748a4099170725956b7ba1ce9b` | 371830 | сравнение модальностей |
| `artifacts/reconstruction/musikhin_profiles_2011_2016_v1/profile_5/source_fragment.png` | `6b559a19c0655dc18f10d69af90857dfc940ff881914896c803fa828c412c7a2` | 60039 | вырезка страницы PDF |
| `artifacts/reconstruction/musikhin_profiles_2011_2016_v1/profile_5/source_overlay.png` | `5a7861f054fa8c1a4e3d7fc03a0e9fe9f6dd33e3645e2a9ecee5ee1767a80780` | 308523 | наложение отсчётов |
| `artifacts/reconstruction/musikhin_profiles_2011_2016_v1/profile_5/reconstructed_profile.png` | `937cfd4f005b72f28de22f3c221ae49a2d020b555768007b1fa30f0c4cda2b6d` | 191236 | построенный профиль |
| `artifacts/reconstruction/musikhin_profiles_2011_2016_v1/profile_5/comparison.png` | `b0df90fa83a0a5ab47e4f207909bedf5cb46400160e407f6caa34c84ce3b827c` | 448859 | сравнение модальностей |
| `artifacts/reconstruction/musikhin_profiles_2011_2016_v1/profile_17/source_fragment.png` | `1e19d06dab0fbf59ad86b90d6d5af6d3374fca67664393d35eed77e06efa9aa9` | 50078 | вырезка страницы PDF |
| `artifacts/reconstruction/musikhin_profiles_2011_2016_v1/profile_17/source_overlay.png` | `71ddbfbba01d7bcc60276c5dd7fc80346825406fb8f66ca97e1e8adb719ecd97` | 248769 | наложение отсчётов |
| `artifacts/reconstruction/musikhin_profiles_2011_2016_v1/profile_17/reconstructed_profile.png` | `2f579dbfa31183bcaa73a39ffb1e15bfb5fe6c9d97068d56a43eeaff819935ed` | 123894 | построенный профиль |
| `artifacts/reconstruction/musikhin_profiles_2011_2016_v1/profile_17/comparison.png` | `2adbb37742c91919081a66189dc9249a9e4812c337f2c91140897b5b5111e444` | 339981 | сравнение модальностей |
| `artifacts/reconstruction/musikhin_profiles_2011_2016_v1/profile_6/source_fragment.png` | `2b6bc5bfd1e1640d59f5702f4cfce13ccf5b643c2c0013dc62cce53edd76d1d8` | 51124 | вырезка страницы PDF |
| `artifacts/reconstruction/musikhin_profiles_2011_2016_v1/profile_6/source_overlay.png` | `992ce6c319efa3184da80a27369c8ca61824c8503a3feea4dd019359c8e704b0` | 244818 | наложение отсчётов |
| `artifacts/reconstruction/musikhin_profiles_2011_2016_v1/profile_6/reconstructed_profile.png` | `25b197181fe62f510f0bd531aced1990675875909e7ba5d74df1f85bdbac16e4` | 135078 | построенный профиль |
| `artifacts/reconstruction/musikhin_profiles_2011_2016_v1/profile_6/comparison.png` | `55c68ef1f2a6072c0929df7c8dfcb25fce818f6130e1945e4e79b8000ee1b7f3` | 344080 | сравнение модальностей |

26 файлов, 22 уникальных по SHA-256. В PRIVATE они переносятся как исторические байты, без правки.

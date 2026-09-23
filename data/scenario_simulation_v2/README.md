# SKRU1_SCENARIO_SIMULATION_V2

Готовый data release; обучение моделей не выполнялось. Версия v2 следует за
отдельным сценарием v1 и не заменяет исторический SKRU1_Data_Foundation_v3_2_1.

640 сценариев наблюдений, 128 latent-world IDs
(126 ненулевых вариантов и два повтора нулевого контроля), 42 точек / 14 профилей,
29 кампаний, 321766 origin rows. Реплики observation
conditions одной latent world зависимы; число строк не равно числу независимых опытов.

Период: ['2018-05-15', '2025-11-04']; positive_down_mm. Геометрия и расписание взяты из
исправленной реконструкции. Все временные значения синтетические. Семь отдельных
leveling profiles/periods Мусихина задают амплитудные огибающие с границами чтения.
Длительности 1 и 5 лет — условная интерпретация годовых подписей. Условное окно
расположено в середине моделируемого времени. Это не восстановление реальных дат,
реперов, скорости или общего datum. Разности опубликованных периодов не вычисляются.

Статическая spatial shape допускает случайные центр, ширину, отражение порядка,
амплитуду; точечные значения опубликованного графика не копируются. Moving focus
интегрирует движущееся поле скорости. Temporal laws не используют IMM, матрицу его
переходов или исторические prediction scores. Их параметры и доли — design assumptions.

model_features.csv.gz — только 16 разрешённых признаков и metadata. causal_history
содержит только реально сгенерированные доступные наблюдения. sequence_windows
содержит ID последних максимум 16 доступных токенов до origin; padding указан явно.
Нормализация и обучение любых sequence controls отложены до отдельного протокола.
targets/train_observed и calibration_observed отделены от evaluator. Пропущенная
следующая плановая цель не заменяется следующей успешной; latent target доступен
только evaluator. Исторические validation/test/final holdout не являются входами.

Сценарии reflector contamination отдельно помечены: 30–50 mm мотивированы
Бабаянцем для отражателей на зданиях, это не ошибка нивелирования. sigma 0.4/0.8/1.6
mm, gross outliers, generic datum shift, waveform/timing и missingness вероятности
остаются инженерными допущениями. Зимние пропуски отражателя не перенесены на обычное
нивелирование. Нулевой stable control намеренно не калибруется по опубликованным точкам.

Разбиение по target date сохранено как отдельный synthetic protocol: train до
2022-10-18, calibration 2023-01-17…2023-11-07, future evaluation с 2024-01-30.
Реактивация и moving focus исключены из fit/calibration. Для будущей nested selection
нужны группировка по latent_world и temporal/profile/zone embargo, см. migration plan.
Открытый при QA synthetic evaluator не является новым запечатанным внешним holdout.

distribution_summary.json содержит распределения оседаний, скоростей, ускорений,
targets, интервалов, uncertainty, missingness, профилей, proxy zones и механизмов.
manifest.json фиксирует входы, выходы, code hashes и seeds. Команда не перезаписывает
заполненный каталог. Для повторения задайте новый --output work/data_foundation_v2/<run>.
См. docs/reports/DATA_FOUNDATION_V2_RU.md, docs/governance/SCENARIO_EXPERIMENT_V2_PROTOCOL.md.

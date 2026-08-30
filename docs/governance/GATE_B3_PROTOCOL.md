# Gate B3: заранее специфицированный протокол IMM

Статус документа: **зафиксирован до первого полного расчёта Gate B3 на outer validation**.

## Узкая гипотеза

Двухрежимная `Interacting Multiple Model`-модель с состоянием
`[settlement, velocity, acceleration]` должна уменьшить ошибку B6 на переходных
origin-строках `accelerating` и `volatile_or_gap` и одновременно устранить
нестабильность `leave-zone-out`. Общая temporal MAE не должна ухудшиться более
чем на 2% относительно B6.

Gate B3 проверяет только эту гипотезу. Innovation-adaptive Kalman и другие
switching-модели в текущий gate не добавляются: их параллельный перебор после
наблюдения validation создал бы model-selection bias.

## Неизменяемые comparators

B1, B5 и B6 не переобучаются и не пересчитываются. Gate B3 читает их строки
предсказаний из `artifacts/model_selection/t1_b2_v1/outer_fold_predictions.csv`
только после проверки SHA-256. Вместе с predictions защищены fold contracts,
transition thresholds, aggregate metrics, B2 candidate, B2 validation report и
полный B2 artifact inventory. Уже раскрытые T1 test-артефакты также проверяются
только по SHA-256; пути загрузки test в runner Gate B3 нет.

## Модель и выбор параметров

Используются два режима одного трёхмерного state-space:

- `stable`: сильное затухание acceleration и низкий jerk/process noise;
- `transition`: почти сохраняющаяся acceleration и высокий jerk/process noise.

Межрежимное смешивание выполняется стандартным IMM с фиксированной марковской
матрицей. Вероятности режимов обновляются только по причинным innovations
settlement, last-rate и origin-known recent-acceleration. Все масштабы,
fallback и measurement uncertainty оцениваются внутри соответствующего train.

Сетка содержит ровно 16 комбинаций:

- `q_stable`: 0.5, 2.0;
- `q_transition`: 50, 200;
- `p(stable→stable)`: 0.97, 0.99;
- `p(transition→transition)`: 0.75, 0.90.

В каждом outer fold параметры выбираются на трёх expanding-window inner folds.
Целевая функция заранее определена как 50% normalized overall MAE и 50%
normalized MAE на объединении `accelerating + volatile_or_gap`; нормирование
выполняется относительно B1 на тех же inner validation строках. Thresholds
переходного proxy оцениваются отдельно только на inner train.

## Неизменяемые схемы оценки

Используются те же 24 forward-only folds, что и в Gate B2:

- один temporal holdout;
- пять rolling-origin;
- 14 leave-profile-out;
- четыре leave-zone-out.

Transition labels для outer comparison переносятся из hash-защищённых строк
Gate B2, поэтому определения сегментов у B1/B5/B6/B7 совпадают побитно.
Интервалы B7 калибруются scaled conformal методом только по пяти nested
rolling-origin OOF частям исходного train.

## Заранее заданный критерий успеха

Gate B3 проходит screening только при одновременном выполнении условий:

1. temporal MAE B7 не выше 1.02 × temporal MAE B6;
2. MAE на `accelerating + volatile_or_gap` лучше B1 минимум на 10%;
3. та же MAE лучше B6 минимум на 10%;
4. отдельно `accelerating` и `volatile_or_gap` не хуже B1;
5. leave-profile-out degradation относительно temporal B7 не выше 5%;
6. leave-zone-out degradation относительно temporal B7 не выше 5%;
7. leave-zone-out MAE B7 не выше B1;
8. empirical coverage 95% интервала находится в [0.90, 0.97].

Невыполнение любого пункта не ведёт к подкручиванию параметров в этом gate.
Результат фиксируется как отрицательная или смешанная проверка гипотезы.

## Граница окончательного вывода

Текущий T1 test уже раскрыт на предыдущем этапе и не является допустимым
финальным holdout. Даже успешно прошедший Gate B3 получает только статус
`VALIDATION_FROZEN_PENDING_NEW_HOLDOUT`. Финальный вывод требует заранее
замороженного нового временного/внешнего holdout либо отдельного governance-
решения, которое запрещает последующую настройку.

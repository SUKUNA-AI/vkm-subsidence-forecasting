# Model card: `C02_compact_lstm`

## Назначение

Compact sequence comparator для nested temporal screening задачи T1. Модель не является производственной и не оценивалась на новом future/external holdout.

## Спецификация

- Family: `long_short_term_memory`; probabilistic: `false`.
- Training objective: `huber_delta_1_standardized`; selection objective: `pooled_inner_mae`.
- Frozen grid: `16` configurations; parameter limit: `100000`.
- Seeds: `42117, 42118, 42119, 42120, 42121`; environment: `gate_c_torch`.
- Model spec SHA-256: `f99687d640a37074a07552ad50b3eae061763384f09506e48598c8ac438cf425`.

## Входы и leakage boundary

Пять numeric channels, train-fitted one-hot `current_campaign_type`, padding/observation/missing masks и фактическая длина. point/profile/zone/campaign IDs не передаются в tensor. Preprocessing и target scaling fit выполняются только по train role; historical validation, disclosed test и holdout недоступны worker.

## Temporal evidence

- Canonical mean-of-five-seeds MAE: **6.467 мм/год**; RMSE: **11.341 мм/год**.
- Median fold MAE: **7.127 мм/год**; maximum fold/B1 ratio: **1.944**.
- B1 skill: **-2.5%**; seed MAE IQR: **0.087 мм/год**; seed CV: **1.92%**.
- Temporal screen status: **`REJECTED_TEMPORAL_SCREEN`**; admitted to C2: `false`.

## Вычислительная трасса

Logical inner evaluations: `2640`; physical inner fits: `1040`; cache reuse: `1600`; outer refits: `55`. Selected outer parameter count: `1681–13857`; epoch count: `13–159`.
Для каждого fit сохранены пять полных training states и recovery checkpoint. Inner prediction восстановлен из rank 1 по frozen objective; outer prediction — из preregistered final epoch без доступа к outer labels.

## Неопределённость

Модель возвращает только point prediction; интервалы требуют общего conformal wrapper в Gate C2.

## Ограничения и запрещённые выводы

C1 не содержит leave-profile, leave-zone, transition/gap или common conformal evidence и не создаёт suite v5. Статус `PASSED_TEMPORAL_SCREEN` означает только право перейти в C2. Нельзя утверждать окончательную, промышленную или внешнюю точность модели.

# Model card: `C01_compact_gru`

## Назначение

Compact sequence comparator для nested temporal screening задачи T1. Модель не является производственной и не оценивалась на новом future/external holdout.

## Спецификация

- Family: `gated_recurrent_unit`; probabilistic: `false`.
- Training objective: `huber_delta_1_standardized`; selection objective: `pooled_inner_mae`.
- Frozen grid: `16` configurations; parameter limit: `100000`.
- Seeds: `42117, 42118, 42119, 42120, 42121`; environment: `gate_c_torch`.
- Model spec SHA-256: `93d6a231e88abd7219722c0fb59328fbdb4063caf9d8ab33a5570465fdfe8a6d`.

## Входы и leakage boundary

Пять numeric channels, train-fitted one-hot `current_campaign_type`, padding/observation/missing masks и фактическая длина. point/profile/zone/campaign IDs не передаются в tensor. Preprocessing и target scaling fit выполняются только по train role; historical validation, disclosed test и holdout недоступны worker.

## Temporal evidence

- Canonical mean-of-five-seeds MAE: **6.288 мм/год**; RMSE: **10.817 мм/год**.
- Median fold MAE: **6.457 мм/год**; maximum fold/B1 ratio: **1.807**.
- B1 skill: **0.4%**; seed MAE IQR: **0.158 мм/год**; seed CV: **1.32%**.
- Temporal screen status: **`PASSED_TEMPORAL_SCREEN`**; admitted to C2: `true`.

## Вычислительная трасса

Logical inner evaluations: `2640`; physical inner fits: `1040`; cache reuse: `1600`; outer refits: `55`. Selected outer parameter count: `1265–10401`; epoch count: `21–193`.
Для каждого fit сохранены пять полных training states и recovery checkpoint. Inner prediction восстановлен из rank 1 по frozen objective; outer prediction — из preregistered final epoch без доступа к outer labels.

## Неопределённость

Модель возвращает только point prediction; интервалы требуют общего conformal wrapper в Gate C2.

## Ограничения и запрещённые выводы

C1 не содержит leave-profile, leave-zone, transition/gap или common conformal evidence и не создаёт suite v5. Статус `PASSED_TEMPORAL_SCREEN` означает только право перейти в C2. Нельзя утверждать окончательную, промышленную или внешнюю точность модели.

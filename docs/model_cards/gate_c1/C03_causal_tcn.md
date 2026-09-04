# Model card: `C03_causal_tcn`

## Назначение

Compact sequence comparator для nested temporal screening задачи T1. Модель не является производственной и не оценивалась на новом future/external holdout.

## Спецификация

- Family: `temporal_convolutional_network`; probabilistic: `false`.
- Training objective: `huber_delta_1_standardized`; selection objective: `pooled_inner_mae`.
- Frozen grid: `16` configurations; parameter limit: `100000`.
- Seeds: `42117, 42118, 42119, 42120, 42121`; environment: `gate_c_torch`.
- Model spec SHA-256: `24e95f611985cd7e566c7782e9a8a39fe5820cc6f4de0c848135a7d1ca297046`.

## Входы и leakage boundary

Пять numeric channels, train-fitted one-hot `current_campaign_type`, padding/observation/missing masks и фактическая длина. point/profile/zone/campaign IDs не передаются в tensor. Preprocessing и target scaling fit выполняются только по train role; historical validation, disclosed test и holdout недоступны worker.

## Temporal evidence

- Canonical mean-of-five-seeds MAE: **6.552 мм/год**; RMSE: **11.126 мм/год**.
- Median fold MAE: **6.950 мм/год**; maximum fold/B1 ratio: **1.626**.
- B1 skill: **-3.8%**; seed MAE IQR: **0.194 мм/год**; seed CV: **2.46%**.
- Temporal screen status: **`REJECTED_TEMPORAL_SCREEN`**; admitted to C2: `false`.

## Вычислительная трасса

Logical inner evaluations: `2640`; physical inner fits: `1040`; cache reuse: `1600`; outer refits: `55`. Selected outer parameter count: `961–4225`; epoch count: `9–171`.
Для каждого fit сохранены пять полных training states и recovery checkpoint. Inner prediction восстановлен из rank 1 по frozen objective; outer prediction — из preregistered final epoch без доступа к outer labels.

## Неопределённость

Модель возвращает только point prediction; интервалы требуют общего conformal wrapper в Gate C2.

## Ограничения и запрещённые выводы

C1 не содержит leave-profile, leave-zone, transition/gap или common conformal evidence и не создаёт suite v5. Статус `PASSED_TEMPORAL_SCREEN` означает только право перейти в C2. Нельзя утверждать окончательную, промышленную или внешнюю точность модели.

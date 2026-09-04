# Model card: `C04_probabilistic_gru_student_t`

## Назначение

Compact sequence comparator для nested temporal screening задачи T1. Модель не является производственной и не оценивалась на новом future/external holdout.

## Спецификация

- Family: `irregular_time_probabilistic_recurrent`; probabilistic: `true`.
- Training objective: `student_t_nll`; selection objective: `pooled_inner_crps`.
- Frozen grid: `8` configurations; parameter limit: `100000`.
- Seeds: `42117, 42118, 42119, 42120, 42121`; environment: `gate_c_torch`.
- Model spec SHA-256: `cfcbdc11c6feb4758ee49b08dc482ca665a6bdeac3fc36ccd96efbed7617f16e`.

## Входы и leakage boundary

Пять numeric channels, train-fitted one-hot `current_campaign_type`, padding/observation/missing masks и фактическая длина. point/profile/zone/campaign IDs не передаются в tensor. Preprocessing и target scaling fit выполняются только по train role; historical validation, disclosed test и holdout недоступны worker.

## Temporal evidence

- Canonical mean-of-five-seeds MAE: **7.177 мм/год**; RMSE: **13.698 мм/год**.
- Median fold MAE: **7.714 мм/год**; maximum fold/B1 ratio: **1.726**.
- B1 skill: **-13.7%**; seed MAE IQR: **0.187 мм/год**; seed CV: **2.53%**.
- Temporal screen status: **`REJECTED_TEMPORAL_SCREEN`**; admitted to C2: `false`.

## Вычислительная трасса

Logical inner evaluations: `1320`; physical inner fits: `520`; cache reuse: `800`; outer refits: `55`. Selected outer parameter count: `1299–4131`; epoch count: `11–66`.
Для каждого fit сохранены пять полных training states и recovery checkpoint. Inner prediction восстановлен из rank 1 по frozen objective; outer prediction — из preregistered final epoch без доступа к outer labels.

## Неопределённость

Модель возвращает Student-t loc/scale/df и native quantiles по каждому seed. Canonical ensemble содержит только point mean; объединённое распределение не публикуется в C1.

## Ограничения и запрещённые выводы

C1 не содержит leave-profile, leave-zone, transition/gap или common conformal evidence и не создаёт suite v5. Статус `PASSED_TEMPORAL_SCREEN` означает только право перейти в C2. Нельзя утверждать окончательную, промышленную или внешнюю точность модели.

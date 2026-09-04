# Model Catalog C1

Каталог фиксирует только четыре обязательные compact sequence-архитектуры Gate C1. Числа относятся к nested temporal screen внутри `t1_v1/train`; spatial/transition/calibration evidence отсутствует до Gate C2.
Все adapters используют единый work-only checkpoint contract: recovery state после 50-epoch стадии и terminal epoch, top-5 по inner objective, fixed final epoch для outer refit. Outer labels не влияют на checkpoint selection.

## `C01_compact_gru`

- Family: `gated_recurrent_unit`; probabilistic: `false`.
- Objective: `huber_delta_1_standardized`; selection: `pooled_inner_mae`.
- Frozen configurations: `16`; fixed seeds: `42117, 42118, 42119, 42120, 42121`.
- Input: пять numeric channels, train-fitted one-hot `current_campaign_type`, три masks; identifiers только metadata.
- Canonical ensemble MAE: **6.288 мм/год**; median fold MAE: **6.457 мм/год**; B1 skill: **0.4%**.
- Parameter count across selected outer specs: `1265–10401`; outer epochs: `21–193`.
- Temporal status: **`PASSED_TEMPORAL_SCREEN`**; admitted to C2: `true`.
- Model spec SHA-256: `93d6a231e88abd7219722c0fb59328fbdb4063caf9d8ab33a5570465fdfe8a6d`.
- Claim boundary: `train_only_internal_research`; final/external quality claim prohibited.

## `C02_compact_lstm`

- Family: `long_short_term_memory`; probabilistic: `false`.
- Objective: `huber_delta_1_standardized`; selection: `pooled_inner_mae`.
- Frozen configurations: `16`; fixed seeds: `42117, 42118, 42119, 42120, 42121`.
- Input: пять numeric channels, train-fitted one-hot `current_campaign_type`, три masks; identifiers только metadata.
- Canonical ensemble MAE: **6.467 мм/год**; median fold MAE: **7.127 мм/год**; B1 skill: **-2.5%**.
- Parameter count across selected outer specs: `1681–13857`; outer epochs: `13–159`.
- Temporal status: **`REJECTED_TEMPORAL_SCREEN`**; admitted to C2: `false`.
- Model spec SHA-256: `f99687d640a37074a07552ad50b3eae061763384f09506e48598c8ac438cf425`.
- Claim boundary: `train_only_internal_research`; final/external quality claim prohibited.

## `C03_causal_tcn`

- Family: `temporal_convolutional_network`; probabilistic: `false`.
- Objective: `huber_delta_1_standardized`; selection: `pooled_inner_mae`.
- Frozen configurations: `16`; fixed seeds: `42117, 42118, 42119, 42120, 42121`.
- Input: пять numeric channels, train-fitted one-hot `current_campaign_type`, три masks; identifiers только metadata.
- Canonical ensemble MAE: **6.552 мм/год**; median fold MAE: **6.950 мм/год**; B1 skill: **-3.8%**.
- Parameter count across selected outer specs: `961–4225`; outer epochs: `9–171`.
- Temporal status: **`REJECTED_TEMPORAL_SCREEN`**; admitted to C2: `false`.
- Model spec SHA-256: `24e95f611985cd7e566c7782e9a8a39fe5820cc6f4de0c848135a7d1ca297046`.
- Claim boundary: `train_only_internal_research`; final/external quality claim prohibited.

## `C04_probabilistic_gru_student_t`

- Family: `irregular_time_probabilistic_recurrent`; probabilistic: `true`.
- Objective: `student_t_nll`; selection: `pooled_inner_crps`.
- Frozen configurations: `8`; fixed seeds: `42117, 42118, 42119, 42120, 42121`.
- Input: пять numeric channels, train-fitted one-hot `current_campaign_type`, три masks; identifiers только metadata.
- Canonical ensemble MAE: **7.177 мм/год**; median fold MAE: **7.714 мм/год**; B1 skill: **-13.7%**.
- Parameter count across selected outer specs: `1299–4131`; outer epochs: `11–66`.
- Temporal status: **`REJECTED_TEMPORAL_SCREEN`**; admitted to C2: `false`.
- Model spec SHA-256: `cfcbdc11c6feb4758ee49b08dc482ca665a6bdeac3fc36ccd96efbed7617f16e`.
- Claim boundary: `train_only_internal_research`; final/external quality claim prohibited.

## Frozen context comparators

`B1_persistence_last_rate`, `B7_two_regime_imm` и `B8_student_t_robust_imm` перенесены из frozen B6 artifacts на exact 595-origin universe. Они не участвуют в deep-model admission как новые кандидаты и не перенастраиваются.

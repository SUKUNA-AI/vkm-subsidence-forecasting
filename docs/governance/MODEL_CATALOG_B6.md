# Model Catalog B6

## Статус каталога

Каталог был заморожен в Gate B5 до исполнения Gate B6. Он содержит 23
исторические model records: восемь frozen comparators и 15 preregistered
candidates. Governance-поправка `B6-GOV-001` не переписывает эту историю, но
исключает `Z15_tabpfn_v2_6` из execution catalog. Поэтому фактически
исполнены 22 модели: восемь comparators и 14 новых candidates.

Научная граница всех записей — `train_only_internal_research`. Статус
`ADVANCED` означает только допуск к B6 spatial/transition audit, а не
внешнюю валидность или production readiness.

## Исполняемый каталог и outcome

| Model ID | Семейство | Environment | Feature view | B6 outcome | Suite v4 |
|---|---|---|---|---|---|
| `B1_persistence_last_rate` | persistence | `b6_cpu` | `SAFE_ALL` | frozen comparator, advanced | context-only comparator |
| `B3_profile_robust_trend` | profile robust trend | `b6_cpu` | `SAFE_ALL` | frozen comparator, advanced despite screen | — |
| `B5_fixed_kalman` | fixed Kalman | `b6_cpu` | `SAFE_ALL` | frozen comparator, advanced | context-only comparator |
| `B6_adaptive_kalman` | adaptive Kalman | `b6_cpu` | `SAFE_ALL` | frozen comparator, advanced | context-only comparator |
| `B7_two_regime_imm` | two-regime IMM | `b6_cpu` | `SAFE_ALL` | frozen comparator, best rolling MAE | **primary** |
| `B8_student_t_robust_imm` | robust-observation IMM | `b6_cpu` | `SAFE_ALL` | frozen comparator, advanced | context-only comparator |
| `M1_ridge` | Ridge | `b6_cpu` | `SAFE_ALL` | historical fixed spec, advanced | — |
| `M2_extra_trees` | ExtraTrees | `b6_cpu` | `SAFE_ALL` | historical fixed spec, advanced | — |
| `Z01_elastic_net` | ElasticNet | `b6_cpu` | `SAFE_ALL` | advanced; not suite-eligible | interpretable context-only |
| `Z02_huber` | Huber | `b6_cpu` | `SAFE_ALL` | `REJECTED_TEMPORAL_SCREEN` | — |
| `Z03_rbf_svr` | RBF-SVR | `b6_cpu` | `DYNAMIC_CORE_17` | `REJECTED_TEMPORAL_SCREEN` | — |
| `Z04_gaussian_process` | Gaussian Process | `b6_cpu` | `DYNAMIC_CORE_17` | `REJECTED_TEMPORAL_SCREEN` | — |
| `Z05_gaussian_gee` | Gaussian GEE | `b6_cpu` | `DYNAMIC_CORE_17` | `REJECTED_TEMPORAL_SCREEN` | — |
| `Z06_hist_gradient_boosting` | HistGradientBoosting | `b6_cpu` | `SAFE_ALL` | `REJECTED_TEMPORAL_SCREEN` | — |
| `Z07_quantile_hist_gradient_boosting` | quantile HGB | `b6_cpu` | `SAFE_ALL` | `REJECTED_TEMPORAL_SCREEN` | — |
| `Z08_xgboost` | XGBoost | `b6_cpu` | `SAFE_ALL` | advanced; spatial inner guardrail failed | — |
| `Z09_lightgbm` | LightGBM | `b6_cpu` | `SAFE_ALL` | advanced; not suite-eligible | — |
| `Z10_catboost` | CatBoost | `b6_cpu` | `NATIVE_CATEGORICAL` | `REJECTED_TEMPORAL_SCREEN` | — |
| `Z11_ebm` | Explainable Boosting Machine | `b6_cpu` | `NATIVE_CATEGORICAL` | `REJECTED_TEMPORAL_SCREEN` | — |
| `Z12_ngboost` | NGBoost Normal | `b6_ngboost` | `SAFE_ALL` | `REJECTED_TEMPORAL_SCREEN` | — |
| `Z13_residual_mlp` | residual MLP | `b6_torch` | `DYNAMIC_CORE_17` | `REJECTED_TEMPORAL_SCREEN` | — |
| `Z14_enfs_replica` | protocol-safe ENFS replica | `b6_torch` | `DYNAMIC_CORE_17` | `REJECTED_TEMPORAL_SCREEN` | — |

## Historical non-executable record

| Model ID | Historical environment | Historical feature view | Governance status | Scientific status |
|---|---|---|---|---|
| `Z15_tabpfn_v2_6` | `b6_torch` | `SAFE_ALL` | `EXCLUDED_GOVERNANCE_USER_WITHDRAWAL` | `NOT_EVALUATED` |

Для Z15 не принималась лицензия, не загружались веса, не создавались shards и
не выполнялся scoring. Runtime import, worker dispatch, checkpoint staging,
network access и API mode запрещены. Сохранение записи в historical registry
нужно только для неизменяемости B5 freeze; оно не делает модель частью B6
evidence. Исполняемый `b6_torch` environment строится по
`requirements/b6_torch_runtime.lock.txt`, где package отсутствует.

## Роли feature views

- `SAFE_ALL` — полный исполняемый allowlist из formal feature contract с
  train-fitted preprocessing.
- `DYNAMIC_CORE_17` — заранее заданный маломерный view для small-data,
  longitudinal и neural controls.
- `NATIVE_CATEGORICAL` — SAFE_ALL с train-fitted categorical schema для
  моделей, которые обрабатывают категории нативно.

`point_id`, `profile_id`, campaign IDs и zone IDs не входят ни в одну
estimator matrix. Единственное исключение по назначению — `point_id` как
отдельный structural group для working correlation GEE; runtime guard
проверяет его отсутствие в `X`.

## Источники истины

- frozen specification: `artifacts/model_selection/t1_b6_expanded_v1/model_registry.json`;
- frozen jobs: `artifacts/model_selection/t1_b6_expanded_v1/frozen_job_manifest.json`;
- execution amendment: `configs/gate_b6_amendment_no_tabpfn.yaml`;
- screen decisions: `artifacts/model_selection/t1_b6_expanded_v1/screening_register.csv`;
- rejection reasons: `artifacts/model_selection/t1_b6_expanded_v1/rejection_register.csv`;
- final roles: `artifacts/governance/final_candidate_suite_v4.json`.

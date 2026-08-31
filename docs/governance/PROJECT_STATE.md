# Project State v4

## Verified bundled inputs

The five bootstrap artifacts and eleven primary sources are physically included in this bundle and verified against SHA-256 manifests. Their status is `bundled_verified`, not merely `present_in_previous_runtime`.

## Current scientific state

- spatial reconstruction v3.2 exists;
- EDA and target contracts exist;
- Gate A0 input verification and two-run reconstruction are complete;
- Gate A1 is `PASS_WITH_WARNINGS`: canonical next-planned tables and v1 manifests are frozen, executable leakage guards pass, and model-facing test access is sealed;
- Gate B0/B1 is complete with caveats: five T1 baselines were evaluated over 1 temporal, 5 rolling-origin, 14 forward leave-profile-out, and 4 forward leave-zone-out folds;
- `B1_persistence_last_rate` is frozen as stage candidate `t1-b0b1-v1-3bfcff231705` (validation MAE 7.311 mm/year);
- that candidate consumed its single governed T1 test access (175 origins; MAE 10.135 mm/year); the result is terminal and may not be used for post-test tuning;
- Gate B2 is complete on train/validation only: adaptive `B6_adaptive_kalman`, nested train-only hyperparameter tuning, scaled conformal intervals, and origin-only transition validation were evaluated without a current-test loading path;
- B6 selected `q_base=10` and `acceleration_gain=0`; temporal validation MAE is 7.454 mm/year, 95% coverage is 0.938, but transition MAE is 14.25% worse than B1 and leave-zone degradation is 12.77%, so the record is not eligible for a final claim;
- the Gate B2 development record is `t1-b2-v1-54f5e3756c2f` with status `validation_recorded`; machine validation passed 169 checks with zero failures;
- Gate B3 is complete on train/validation only under the protocol frozen in `configs/gate_b3.yaml` and `docs/governance/GATE_B3_PROTOCOL.md`; the two-regime `B7_two_regime_imm` reused hash-protected B1/B5/B6 predictions without refitting comparators or loading the current test;
- B7 selected `q_stable=0.5`, `q_transition=200`, `p_stable_stay=0.99`, and `p_transition_stay=0.75`; temporal MAE is 6.545 mm/year (12.19% better than B6), accelerating MAE is 12.878 mm/year (19.00% better than B1), and leave-zone MAE is 7.064 mm/year (15.95% better than B6);
- Gate B3 nevertheless failed its complete predeclared screening: accelerating plus volatile/gap improvement versus B1 is only 3.32% instead of 10%, volatile/gap MAE is 22.83% worse than B1, and leave-zone degradation versus B7 temporal is 7.93% instead of at most 5%; 95% coverage is 0.962 and passes;
- the Gate B3 development record is `t1-b3-v1-15208e3b1684` with status `validation_recorded`; the authoritative post-run audit passed 64 checks with zero failures and documented one CSV empty-string/NA serialization reconciliation without changing any model output;
- Gate B4 is complete entirely inside `t1_v1/train`: 823 origins form the internal temporal core, 88 origins at target date 2023-11-07 form the audit tail, and the frozen design contains 1 internal temporal, 5 rolling-origin, 14 forward leave-profile-out, and 4 forward leave-zone-out folds;
- B8 changes only the B7 scalar observation model to a bounded-influence Student-t likelihood; nested train-only tuning selected `student_t_df=30` from the fixed grid `[3, 5, 10, 30]` while all B7 dynamics remained frozen;
- B8 internal-temporal MAE is 5.831 mm/year versus 6.015 for B7, and leave-zone MAE is 5.858 versus 6.046; however `volatile_or_gap` MAE is 0.45% worse than B7 instead of at least 10% better, and pooled rolling-origin MAE is 4.52% worse;
- the Gate B4 record is `t1-b4-train-v1-0dedd1296459` with status `train_only_research_recorded`; machine validation passed 54 checks with zero failures, historical validation/test rows loaded are both zero, and B8 is not eligible for a final claim;
- Gate B5 is complete with status `PASS_PROTOCOL_FROZEN`: `t1_train_benchmark_v1` freezes 11 rolling-origin, 42 spatio-temporal leave-profile-out and 12 leave-zone-out outer folds over the immutable 911-row T1 train set, with three forward-only inner folds per outer context (65 outer and 195 inner folds total);
- B5 records executable `SAFE_ALL`, `DYNAMIC_CORE_17` and `NATIVE_CATEGORICAL` feature views, an error atlas, residual-dependence evidence, independent-unit counts, fixed-parameter learning curves, formal ETS/ARIMA/VAR data-geometry exclusions, and SHA-256 protection of B0–B4 plus suite v3; the independent validator passed 20 checks and the two-run manifest comparison is byte-identical;
- Gate B6 is complete entirely inside `t1_v1/train` with status `PASS_NO_NEW_PRIMARY`: 22 executable models completed the 11-fold temporal screen, 11 models advanced to 42 profile plus 12 zone folds, 18,942 calibrated prediction rows were evaluated, and historical validation/current test/new holdout rows loaded are all zero;
- B7 has the best B6 rolling MAE at 5.640 mm/year versus 6.311 for B1 (10.64% skill), improves on B1 for 10 of 11 target dates and 13 of 14 profiles, has equal-profile macro MAE 5.676, equal-zone macro MAE 4.975, worst-zone MAE 8.478, and 95% conformal coverage 0.951;
- no new model passed every preregistered suite-v4 gate; ElasticNet, XGBoost and LightGBM reached the full spatial audit but failed rolling/audit-tail/transition/spatial/sign-consistency requirements, and XGBoost additionally failed a spatial inner guardrail; NGBoost was rejected at temporal screening because three inner selections had no eligible probabilistic candidate;
- the historical B5 registry retains 23 rows, but `Z15_tabpfn_v2_6` is excluded before scoring by frozen amendment `B6-GOV-001`; no license was accepted, no weights were downloaded, no API/network access occurred, no predictions exist, and the executable catalog contains 22 models;
- final candidate suite v3 remains immutable historical evidence; suite v4 is the current pre-holdout authority with B7 as its single primary, B1/B5/B6/B8 as context-only comparators and Z01 ElasticNet as interpretable context-only; primary selection after observing a future holdout is prohibited;
- final T1 holdout policy v3 is `PENDING_DATA`: no eligible local future/external package exists, target values have not been read, and a sealed status/freeze/evaluate-once ledger is implemented; the disclosed `t1_v1/test` and historical validation remain diagnostic only;
- T5 is technically prepared but remains exploratory because only 17 complete positive labels exist;
- hotfix and baseline scripts exist;
- full temporal deep models, graph models, foundation models and the LLM support layer remain future gates after independent B5/B6 validation; the small B6 MLP and ENFS replica do not consume Gate C;
- no final production-quality model claim is allowed from synthetic data.

The machine-readable Gate A1 authority is `artifacts/data_quality/gate_a1_report.json`; the reader-facing report is `docs/reports/GATE_A1_DATA_QUALITY_RU.md`.

The machine-readable Gate B0/B1 validation authority is `artifacts/model_selection/t1_b0_b1_v1/validation_report.json`; the reader-facing report is `docs/reports/GATE_B0_B1_T1_BASELINES_RU.md`.

The machine-readable Gate B2 validation authority is `artifacts/model_selection/t1_b2_v1/validation_report.json`; the reader-facing report is `docs/reports/GATE_B2_ADAPTIVE_KALMAN_RU.md`, and the executed notebook is `notebooks/03_gate_b2_adaptive_kalman.ipynb`. Adaptive B6 and interval acceptance are now measured, but full screening remains failed on transition and leave-zone criteria. Because the current T1 test has been seen, later final model comparison needs the newly governed temporal/external holdout defined by `configs/final_holdout_v2.yaml` or an explicit governance decision.

The machine-readable Gate B3 validation authority is `artifacts/model_selection/t1_b3_v1/validation_report.json`; the immutable-run reconciliation is `artifacts/model_selection/t1_b3_v1/audit_reconciliation.json`; the reader-facing report is `docs/reports/GATE_B3_IMM_RU.md`; and the executed notebook is `notebooks/04_gate_b3_imm.ipynb`. B7 is retained as a strong comparator because it improves overall and accelerating errors, but it is not a final candidate because volatile/gap and relative leave-zone criteria failed. No B7 hyperparameter may be changed from the current validation evidence.

The machine-readable Gate B4 validation authority is `artifacts/model_selection/t1_b4_train_only_v1/validation_report.json`; the reader-facing report is `docs/reports/GATE_B4_ROBUST_INNOVATION_RU.md`; and the executed notebook is `notebooks/05_gate_b4_robust_innovation.ipynb`. Suite v3 remains its immutable historical governance record. Gate B4 found useful overall/spatial effects from robust innovations but did not confirm the predeclared volatile/gap hypothesis.

The Gate B5 benchmark authority is `artifacts/splits/t1_train_benchmark_v1/benchmark_plan.json`; the machine validation authority is `artifacts/model_selection/t1_b5_evidence_v1/validation_report.json`; the reader-facing report is `docs/reports/GATE_B5_EVIDENCE_BENCHMARK_RU.md`; and the executed notebook is `notebooks/06_gate_b5_evidence_audit.ipynb`.

The Gate B6 machine authority is `artifacts/model_selection/t1_b6_expanded_v1/validation_report.json`; detailed point/group/transition/probabilistic metrics and prediction provenance live under the same artifact root. The reader-facing report is `docs/reports/GATE_B6_EXPANDED_SCREENING_RU.md`, the executed artifact-only notebook is `notebooks/07_gate_b6_model_comparison.ipynb`, and the model catalog is `docs/governance/MODEL_CATALOG_B6.md`. The current governed future-holdout suite is `artifacts/governance/final_candidate_suite_v4.json`; non-consuming intake status remains `artifacts/governance/final_holdout_v3_status.json`.

## Path policy

All paths in manifests are repository-relative. Absolute paths from previous environments are historical provenance only and may not be used as execution inputs.

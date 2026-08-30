# Project State v3

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
- final T1 evaluation policy v2 is `PENDING_DATA`: a new future/external holdout or an explicit governance decision is required, and the disclosed `t1_v1/test` remains historical-diagnostic only;
- T5 is technically prepared but remains exploratory because only 17 complete positive labels exist;
- hotfix and baseline scripts exist;
- deep model zoo, graph models, foundation models and LLM layer are planned but must be implemented and evaluated after Gate A/B;
- no final production-quality model claim is allowed from synthetic data.

The machine-readable Gate A1 authority is `artifacts/data_quality/gate_a1_report.json`; the reader-facing report is `docs/reports/GATE_A1_DATA_QUALITY_RU.md`.

The machine-readable Gate B0/B1 validation authority is `artifacts/model_selection/t1_b0_b1_v1/validation_report.json`; the reader-facing report is `docs/reports/GATE_B0_B1_T1_BASELINES_RU.md`.

The machine-readable Gate B2 validation authority is `artifacts/model_selection/t1_b2_v1/validation_report.json`; the reader-facing report is `docs/reports/GATE_B2_ADAPTIVE_KALMAN_RU.md`, and the executed notebook is `notebooks/03_gate_b2_adaptive_kalman.ipynb`. Adaptive B6 and interval acceptance are now measured, but full screening remains failed on transition and leave-zone criteria. Because the current T1 test has been seen, later final model comparison needs the newly governed temporal/external holdout defined by `configs/final_holdout_v2.yaml` or an explicit governance decision.

The machine-readable Gate B3 validation authority is `artifacts/model_selection/t1_b3_v1/validation_report.json`; the immutable-run reconciliation is `artifacts/model_selection/t1_b3_v1/audit_reconciliation.json`; the reader-facing report is `docs/reports/GATE_B3_IMM_RU.md`; and the executed notebook is `notebooks/04_gate_b3_imm.ipynb`. B7 is retained as a strong comparator because it improves overall and accelerating errors, but it is not a final candidate because volatile/gap and relative leave-zone criteria failed. No B7 hyperparameter may be changed from the current validation evidence.

## Path policy

All paths in manifests are repository-relative. Absolute paths from previous environments are historical provenance only and may not be used as execution inputs.

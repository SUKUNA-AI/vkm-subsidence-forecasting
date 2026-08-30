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
- final T1 evaluation policy v2 is `PENDING_DATA`: a new future/external holdout or an explicit governance decision is required, and the disclosed `t1_v1/test` remains historical-diagnostic only;
- T5 is technically prepared but remains exploratory because only 17 complete positive labels exist;
- hotfix and baseline scripts exist;
- deep model zoo, graph models, foundation models and LLM layer are planned but must be implemented and evaluated after Gate A/B;
- no final production-quality model claim is allowed from synthetic data.

The machine-readable Gate A1 authority is `artifacts/data_quality/gate_a1_report.json`; the reader-facing report is `docs/reports/GATE_A1_DATA_QUALITY_RU.md`.

The machine-readable Gate B0/B1 validation authority is `artifacts/model_selection/t1_b0_b1_v1/validation_report.json`; the reader-facing report is `docs/reports/GATE_B0_B1_T1_BASELINES_RU.md`.

The machine-readable Gate B2 validation authority is `artifacts/model_selection/t1_b2_v1/validation_report.json`; the reader-facing report is `docs/reports/GATE_B2_ADAPTIVE_KALMAN_RU.md`, and the executed notebook is `notebooks/03_gate_b2_adaptive_kalman.ipynb`. Adaptive B6 and interval acceptance are now measured, but full screening remains failed on transition and leave-zone criteria. Because the current T1 test has been seen, later final model comparison needs the newly governed temporal/external holdout defined by `configs/final_holdout_v2.yaml` or an explicit governance decision.

## Path policy

All paths in manifests are repository-relative. Absolute paths from previous environments are historical provenance only and may not be used as execution inputs.

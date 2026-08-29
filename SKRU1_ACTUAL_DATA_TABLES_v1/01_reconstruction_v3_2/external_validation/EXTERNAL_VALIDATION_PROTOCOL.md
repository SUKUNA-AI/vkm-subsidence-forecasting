# External validation protocol (no retraining)

Status: READY_PENDING_REAL_DATA.

1. Freeze this v3.2 dataset, feature contract and baseline configuration.
2. Provide real repeated cycles in `external_cycle_schema.csv` format.
3. Do not tune q, thresholds, features or filters on the external sequence.
4. Run `run_external_validation.py`.
5. Report all points, missing rows, MAE/RMSE/Bias and coverage by profile.
6. Synthetic smoke fixtures verify software only and are not external evidence.

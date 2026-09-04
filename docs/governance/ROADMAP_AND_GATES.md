# Corrected Roadmap and Gates v3

## Gate 0 — verify physical inputs

Run `scripts/verify_inputs.py`. All five bootstrap artifacts and eleven sources must be present and hash-valid.

## Gate A — Data Foundation

Clean rebuild, contracts, manifests, no leakage, reproducible hashes.

## Gate B — Classical, probabilistic and small-data evidence

- B0–B4: persistence, robust trend, regularized/tree baselines, Kalman,
  adaptive Kalman, IMM and robust-innovation IMM;
- B5: frozen train-only benchmark, error atlas, executable feature views,
  metric suite and environment/model registry;
- B6: nested rolling/profile/zone screening of classical, probabilistic,
  longitudinal, boosting, glassbox, small neural and neuro-fuzzy comparators;
- ETS, ARIMA/ARIMAX and VAR receive formal data-geometry eligibility cards
  instead of interpolation-driven mechanical fits.

Gate B5/B6 may use only `t1_v1/train` and may produce only
`train_only_internal_research`. Suite v4 is frozen before any new holdout
labels are available.

Current status: B5 is `PASS_PROTOCOL_FROZEN`; B6 is
`PASS_NO_NEW_PRIMARY`. B7 remains the suite-v4 primary. One excluded
external-model specification remains only in the immutable historical B5
registry and is absent from the 22-model executable catalog.

## Gate C — Deep temporal models

Gate C0 freezes the sequence representation, causal masks, fold bindings,
architecture eligibility, grids, five seeds, suite-v5 governance and compute
budget before any model fit. Early stopping is allowed only on inner rolling
validation inside `t1_v1/train`; historical validation and outer labels are
not epoch-selection inputs.

Current status: C0 is `PASS_PROTOCOL_FROZEN`; C1 is
`PASS_C1_TEMPORAL_SCREEN`. All four required compact models completed 11
nested rolling-origin folds and five fixed seeds. Only `C01_compact_gru` was
admitted to C2: its canonical MAE is 6.288 mm/year versus 6.311 for B1 and
5.640 for B7. C02 LSTM and C03 TCN failed the median-fold guard, while C04
Student-t GRU failed pooled and median-fold guards. C1 loaded zero historical
validation, disclosed-test and new-holdout rows. TSMixer and compact TFT remain
conditional; N-BEATS/N-HiTS, PatchTST and iTransformer remain
`NOT_ELIGIBLE_DATA_GEOMETRY` on 3–16 irregular observations unless a future
protocol version establishes a defensible representation.

The small residual MLP and ENFS replica in B6 do not consume Gate C. C1 used
the frozen 11 rolling folds with three forward-only inner folds per context;
it published five-seed stability, parameter count, fit/inference time and
RAM/VRAM evidence. Its 3,860 physical fits each have a work-only top-five
full-state checkpoint manifest. CUDA-side batching and fused AdamW reduced
matched mean fit time from 4.449 to 3.301 seconds (1.35x), including checkpoint
I/O, without changing the frozen model grid or scientific objective.

The next executable stage is C2. It may run the 42 leave-profile-out and 12
leave-zone-out folds, transition audit and conformal calibration only for
`C01_compact_gru`, with B1/B7/B8 retained as frozen context comparators. C2
then freezes suite v5 or applies the preregistered B7 fallback.

## Gate D — Spatio-temporal models

STGCN, DCRNN, Graph WaveNet, GAT+TCN/GRU, ST Transformer. Graph must be built from geometry and mining context without future data.

## Gate E — Temporal foundation models and hybrid models

Zero/few-shot time-series foundation models, IMM+CatBoost, KalmanNet, Neural
ODE/CDE, physics-guided residuals and ensembles. The B6 executable catalog
does not substitute for this temporal foundation-model gate.

## Gate F — Final validation

Temporal, rolling-origin, leave-profile, leave-zone, leave-block, transition and Monte Carlo OOD.

## Gate G — LLM/RAG support layer

Source-grounded explanation and reporting only. No numeric-primary or safety authority.

## Gate H — Thesis artefacts

Tables, figures, error atlas, model cards, reproducibility appendix and written sections.

Current status: the 41-page Russian Word draft of the special section is
generated from frozen Gate A/B/C0 and validated C1 artifacts at
`docs/thesis/SPECIAL_SECTION_SKRU1_RU.docx`. It includes C1 temporal,
seed-stability, checkpoint and CUDA evidence. C2 spatial/transition/calibration
results, suite v5 and final future/external validation remain explicitly
pending.

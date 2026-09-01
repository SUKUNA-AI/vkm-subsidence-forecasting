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

Current status: C0 is `PASS_PROTOCOL_FROZEN`. The required C1 compact screen
contains GRU, LSTM, causal TCN and probabilistic Student-t GRU. TSMixer and
compact TFT are conditional. N-BEATS/N-HiTS, PatchTST and iTransformer are
`NOT_ELIGIBLE_DATA_GEOMETRY` on 3–16 irregular observations unless a future
protocol version establishes a defensible representation. C0 performed zero
training calls and did not load historical validation, disclosed test or new
holdout rows.

The small residual MLP and ENFS replica in B6 do not consume Gate C. Gate C
uses the already frozen 11 rolling, 42 profile and 12 zone outer folds, with
three forward-only inner folds per context. C1 must report five-seed stability,
parameter count, CPU/GPU time, RAM/VRAM and inference latency.

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

Current status: a 35-page Russian Word draft of the special section is built
from frozen Gate A/B/C0 artifacts at
`docs/thesis/SPECIAL_SECTION_SKRU1_RU.docx`; deep-model result subsections stay
explicitly pending until Gate C1/C2 produce machine evidence.

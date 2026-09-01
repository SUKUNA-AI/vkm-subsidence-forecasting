# Expanded Model Research Programme

## Why the v1 list was insufficient

The prior roadmap overfocused on a single sensible operational line — Kalman/IMM/CatBoost — and therefore underspecified the comparative research required for a diploma whose direct predecessor explicitly studied neural, neuro-fuzzy and ensemble architectures. The corrected programme separates broad screening from final selection.

## Principle

More models are allowed; more unstructured model noise is not. The repository may test 40+ candidates, but the thesis reports only scientifically representative families and the models that pass frozen gates.

## Current B5/B6/C0 execution boundary

The current expanded comparison is deliberately narrower than the complete
programme. Gate B5 freezes a 65-outer-fold train-only benchmark and Gate B6
preserves 23 historical preregistered specifications but executes 22 models on
the 911 origins of `t1_v1/train`. One external-model specification was
governance-excluded before scoring by `B6-GOV-001`, with no license, weights,
API or prediction shards. Historical validation, the disclosed T1 test and a synthetic
replacement for the missing future holdout are prohibited model inputs.
The completed B6 outcome is `PASS_NO_NEW_PRIMARY`: B7 remains the single
suite-v4 primary and Z01 ElasticNet is retained only as interpretable context.

The B6 residual MLP tests whether modest neural nonlinearity helps on this
small tabular sample, and the ENFS model is a protocol-safe method replica.
Neither is a full deep temporal model. Gate C0 now freezes the causal sequence
representation, inner/outer fold provenance, train-only preprocessing, five
seeds and suite-v5 fallback without fitting a model. The required C1 screen is
compact GRU/LSTM/causal-TCN plus a probabilistic Student-t GRU; TSMixer and
compact TFT are conditional. Architectures requiring an unjustified regular
grid or long patch context are formally pre-screened as ineligible for the
current 3–16-observation geometry.

## Families

### A. Sanity and statistical baselines

Zero increment, last rate, mean rate, robust trend, quadratic trend, Holt/ETS, ARIMA/ARIMAX, Gaussian Processes, VAR/VARX.

### B. State-space and regime switching

Fixed/adaptive Kalman, EKF/UKF, IMM, switching LDS, HMM/HSMM, BOCPD, CUSUM and GLR.

### C. Classical machine learning

Ridge, ElasticNet, SVR, Random Forest, ExtraTrees, XGBoost, LightGBM, CatBoost, quantile boosting, ANFIS/ENFS.

### D. Deep temporal models

MLP, LSTM, GRU, TCN, DeepAR, N-BEATS, N-HiTS, TSMixer, Temporal Fusion Transformer, PatchTST, iTransformer, Informer/Autoformer/FEDformer.

### E. Spatio-temporal deep learning

STGCN, DCRNN, Graph WaveNet, GAT+TCN/GRU, spatio-temporal transformers and temporal graph networks.

### F. Physics-guided and neural state-space models

KalmanNet, Deep Kalman Filter, Neural ODE/CDE, physics-guided residual networks, mixture-of-experts and stacked ensembles.

### G. Time-series foundation models

Zero/few-shot evaluation of locally runnable Chronos-like, TimesFM-like, Moirai/Lag-Llama-like and tabular foundation models. Licensing, model weights and reproducibility must be verified before use.

### H. LLM layer

LLMs may:

- extract candidate metadata from sources with mandatory human verification;
- provide RAG over project sources;
- explain error-atlas cases from structured evidence;
- draft experiment reports from machine-readable results;
- orchestrate tools under AGENTS.md constraints.

LLMs may not:

- be the primary numeric subsidence forecaster;
- invent missing measurements or thresholds;
- assign enterprise danger classes;
- override frozen metrics or source provenance.

## Screening stages

1. B5 evidence/protocol freeze on canonical train only.
2. B6 broad temporal screening with nested forward-only tuning.
3. B6 spatio-temporal profile/zone and transition audit.
4. Interval calibration, learning curves and frozen internal suite v4.
5. C0 causal sequence/protocol freeze with zero model fitting.
6. C1 compact deep-temporal screen with five seeds and B1/B7/B8 comparators.
7. C2 spatial/transition audit, calibration and suite-v5 freeze or B7 fallback.
8. Graph/spatial models.
9. Temporal foundation-model zero/few-shot benchmarks.
10. Hybrid and ensemble selection.
11. Final temporal/spatial/OOD evaluation once candidates and a genuinely new
   holdout are frozen.

## Complexity accounting

Every DL/FM model must report parameter count, GPU/CPU time, memory, inference latency and sensitivity across seeds. A larger model that fails to improve transition/OOD performance is not selected.

# Expanded Model Research Programme

## Why the v1 list was insufficient

The prior roadmap overfocused on a single sensible operational line — Kalman/IMM/CatBoost — and therefore underspecified the comparative research required for a diploma whose direct predecessor explicitly studied neural, neuro-fuzzy and ensemble architectures. The corrected programme separates broad screening from final selection.

## Principle

More models are allowed; more unstructured model noise is not. The repository may test 40+ candidates, but the thesis reports only scientifically representative families and the models that pass frozen gates.

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

1. Broad classical screening on train/validation.
2. State-space and regime models.
3. Deep temporal models with at least five seeds.
4. Graph/spatial models.
5. Foundation-model zero/few-shot benchmarks.
6. Hybrid and ensemble selection.
7. Final temporal/spatial/OOD evaluation once candidates are frozen.

## Complexity accounting

Every DL/FM model must report parameter count, GPU/CPU time, memory, inference latency and sensitivity across seeds. A larger model that fails to improve transition/OOD performance is not selected.

# Corrected Roadmap and Gates v2

## Gate 0 — verify physical inputs

Run `scripts/verify_inputs.py`. All five bootstrap artifacts and eleven sources must be present and hash-valid.

## Gate A — Data Foundation

Clean rebuild, contracts, manifests, no leakage, reproducible hashes.

## Gate B — Classical and state-space baselines

B0–B6, ETS/ARIMA/GP/VAR, ML/ENFS screening, frozen validation.

## Gate C — Deep temporal models

LSTM, GRU, TCN, DeepAR, N-BEATS/N-HiTS, TFT, PatchTST, iTransformer. Five seeds, early stopping on validation, compute report.

## Gate D — Spatio-temporal models

STGCN, DCRNN, Graph WaveNet, GAT+TCN/GRU, ST Transformer. Graph must be built from geometry and mining context without future data.

## Gate E — Foundation models and hybrid models

Zero/few-shot TS foundation models, IMM+CatBoost, KalmanNet, Neural ODE/CDE, physics-guided residuals and ensembles.

## Gate F — Final validation

Temporal, rolling-origin, leave-profile, leave-zone, leave-block, transition and Monte Carlo OOD.

## Gate G — LLM/RAG support layer

Source-grounded explanation and reporting only. No numeric-primary or safety authority.

## Gate H — Thesis artefacts

Tables, figures, error atlas, model cards, reproducibility appendix and written sections.

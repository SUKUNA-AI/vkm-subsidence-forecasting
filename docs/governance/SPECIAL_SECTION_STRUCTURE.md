# Corrected Special Section Structure

## 6.1 Problem statement

Object, horizons, units, censoring and limitations.

## 6.2 Source materials

TAB layers, Excel attributes, maps, provenance and uncertainty.

## 6.3 Reconstruction and monitoring network

Spatial database, profiles, campaigns and QC.

## 6.4 EDA and target formation

Regimes, intervals, missingness, autocorrelation and formal target semantics.

## 6.5 Algorithms and model families

### 6.5.1 Statistical and persistence baselines
### 6.5.2 State-space models and regime switching
### 6.5.3 Classical ML and neuro-fuzzy systems
### 6.5.4 Deep temporal models: LSTM, GRU, TCN, N-BEATS/N-HiTS
### 6.5.5 Transformers: TFT, PatchTST, iTransformer and long-sequence models
### 6.5.6 Spatio-temporal GNNs and graph transformers
### 6.5.7 Time-series foundation models and zero/few-shot evaluation
### 6.5.8 Physics-guided, neural state-space and residual hybrids
### 6.5.9 Uncertainty, ensembles and conformal calibration
### 6.5.10 LLM/RAG explanatory and orchestration layer

## 6.6 Software implementation

Repository architecture, contracts, manifests, checksums and reproducibility.

## 6.7 Experimental study

### 6.7.1 Broad screening
### 6.7.2 Temporal and rolling-origin tests
### 6.7.3 Spatial and graph validation
### 6.7.4 Regime-transition and OOD tests
### 6.7.5 Ablation and sensor fusion
### 6.7.6 Model complexity, compute cost and seed stability
### 6.7.7 External/frozen validation

## 6.8 Error analysis and adaptive monitoring

Error Atlas, repeat-observation priorities, GNSS/InSAR and limits of deployment.

## 6.9 Conclusions of the special section

Verified findings, scientific claim boundary, Gate C status and requirements
for a genuinely new future/external holdout.

## Evidence-to-section mapping

- Gate B5 supplies the reproducible benchmark, Error Atlas, dependence audit,
  independent-unit accounting and formal exclusions for 6.4, 6.6, 6.7 and
  6.8.
- Gate B6 supplies the classical/probabilistic/neuro-fuzzy
  comparison, spatial stability, conformal calibration and compute evidence
  for 6.5.1–6.5.3, 6.5.9 and 6.7.1–6.7.6. It does not supply evidence for
  6.5.7 after the foundation comparator was governance-excluded.
- Gate C0 supplies the causal sequence representation, masking, fold and
  preprocessing protocol, architecture eligibility and suite-v5 governance
  for 6.5.4–6.5.5 and 6.6. It deliberately supplies no deep-model quality
  numbers; these remain pending Gate C1/C2.
- External/frozen validation in 6.7.7 remains pending until a genuinely new
  future or external holdout is obtained.

## Current Word draft

The reader-facing special section is generated at
`docs/thesis/SPECIAL_SECTION_SKRU1_RU.docx` with A4 page geometry, 30/15/20/20
mm margins, Times New Roman 14 pt, 1.5 line spacing and bibliography formatted
against GOST 7.32-2017, GOST R 7.0.100-2018 and GOST R 7.0.5-2008. Local
university requirements remain authoritative over the repository baseline.

The current draft has 35 pages, 14 tables and 13 figures. Its machine-readable
provenance is `docs/thesis/SPECIAL_SECTION_SKRU1_RU_SOURCE_MAP.json`; figures
are sourced from frozen Gate A/B/C0 artifacts. The document explicitly marks
Gate C model training and the final future/external evaluation as pending.

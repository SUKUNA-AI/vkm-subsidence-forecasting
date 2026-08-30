# Gate B4: train-only robust innovation protocol

## Scientific question

Gate B4 tests one narrow hypothesis: replacing the Gaussian scalar observation
channel of frozen B7 with a bounded-influence Student-t channel can reduce
`volatile_or_gap` error without sacrificing accelerating behaviour or spatial
stability.

The state vector, two regimes, process noises, acceleration retention,
transition matrix, history cutoff, B1/B5/B6/B7 parameters, and estimator
feature contract are unchanged. The only selected parameter is the Student-t
degrees of freedom from the predeclared grid `3, 5, 10, 30`; the minimum
influence weight is fixed at `0.05`.

## Data boundary

Only the 911 origins listed in `artifacts/splits/t1_v1/train.csv` may enter
model selection, fitting, threshold estimation, or scoring. The canonical
validation split is a historical diagnostic already used through Gate B3 and
is not loaded by Gate B4. The disclosed canonical test is also not loaded.

The latest train target campaign, 2023-11-07, is frozen as an 88-origin
internal audit tail. The earlier 823 train origins form its fitting core.
Five expanding-window folds are built inside train. Forward-only profile and
zone stress tests fit on the core after excluding the held-out group and score
only the held-out group in the internal audit tail. The expected design is
therefore 1 internal temporal, 5 rolling-origin, 14 leave-profile-out, and 4
leave-zone-out folds.

Every outer fold selects Student-t degrees of freedom using three expanding
inner rolling folds drawn only from that outer training subset. Transition
thresholds are fitted independently on each inner or outer training subset.

## Selection objective and acceptance

The inner objective is an equal-weighted sum of B8/B7 normalized overall MAE
and B8/B7 normalized `volatile_or_gap` MAE. A tuning context is invalid if it
contains fewer than five pooled `volatile_or_gap` rows. Ties are resolved by
the recorded deterministic rule in `configs/gate_b4.yaml`.

The research candidate passes only if all predeclared checks pass: internal
temporal MAE no worse than 1.02 times B7; at least 10% `volatile_or_gap`
improvement against B7; accelerating MAE no worse than 1.02 times B7;
leave-profile and leave-zone degradation against B8 internal temporal at most
5%; leave-zone MAE no worse than B7; and at least one robust update is actually
downweighted.

A pass produces only a `train_only_frozen_research_candidate`. It is not a
validation result and is not eligible for a final claim before the separately
frozen future/external holdout is opened once.

## Reproducibility and prohibited actions

- random split, ordinary KFold, canonical validation loading, and canonical
  test loading are prohibited;
- all model fits require train provenance;
- causal histories are cut at each origin `current_date`;
- predecessor hashes, split manifests, fold assignments, tuning rows,
  predictions, metrics, diagnostics, source hashes, and an artifact inventory
  are persisted;
- rerunning against different train-only manifests must fail instead of
  replacing the frozen v1 design;
- no Gate B4 parameter may be changed in response to historical validation or
  future holdout results.

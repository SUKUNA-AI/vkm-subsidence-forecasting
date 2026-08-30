# Intake and one-shot access policy for T1 final holdout v3

## Current status

No eligible future or independent external labelled package is present in the
repository. Gate B4 therefore records `PENDING_DATA`; synthetic smoke fixtures,
model predictions, the disclosed `t1_v1/test`, and the historical validation
split cannot be relabelled as a new holdout.

## Local package contract

An authorized data owner places the following ignored local files under
`inputs/holdout_candidates/t1_final_v3/`:

- `package_manifest.json` with package identity, holdout type, provenance
  declarations, file names, and SHA-256 values;
- `holdout_origins.csv` with `sample_id`, origin metadata, and the complete
  allowlisted model-ready feature schema;
- `holdout_targets.sealed.csv` with exactly `sample_id` and
  `observed_rate_mm_y`.

The status and freeze phases may parse the package manifest and origin table,
but they only hash the sealed target file as raw bytes. Target values are first
parsed after the access ledger has irrevocably entered a consumed state.

## Eligibility and freeze

The intake verifies hashes, exact schema, unique IDs, positive horizons,
sample overlap, required scope, and either the future-temporal or external
eligibility contract in `configs/final_holdout_v3.yaml`. A future package must
start no earlier than 2026-01-01 and be strictly beyond the old 2025-11-04
boundary. An external package must have independent point IDs and a schema
mapping frozen before labels.

Before access the repository records the candidate-suite hash, primary model,
code commit, origin and target hashes, and a frozen ordered sample manifest.
The suite contains one predeclared primary, B7. B1/B5/B6 and the retained B8
are contextual comparators, not alternatives from which a winner may be
selected after observing holdout scores.

## One-shot evaluation

`evaluate-once` first marks the ledger consumed, including for any later
failure. It then verifies all frozen hashes, parses labels, fits only the
predeclared model specifications on frozen `t1_v1/train`, and writes predictions
and metrics. Post-access tuning, feature changes, interval recalibration, model
selection, or a second access are prohibited.

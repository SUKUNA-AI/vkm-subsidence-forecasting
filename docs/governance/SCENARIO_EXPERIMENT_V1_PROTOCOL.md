# Scenario experiment v1 protocol

## Status and scope

`SKRU1_SCENARIO_SIMULATION_V1` is a publication-constrained simulation
benchmark, not a reconstruction of the missing field history. It is derived
from the point/profile roster and planned campaign membership of
`SKRU1_RECONSTRUCTION_RESEARCH_V1`. The published atlas constrains scale and
topological context only. All temporal laws and observation-process parameters
remain explicit design assumptions.

`SKRU1_SCENARIO_B1_IMM_V1` is a separate B1/IMM execution on this new release.
It does not replace, rename or modify any historical Gate B/C score, split,
candidate suite or holdout ledger. The disclosed legacy test and the absent
external holdout are not inputs.

## Estimand and information boundary

The primary estimand is the average settlement rate from an observed origin to
the **next planned targeted campaign**. A missing target is not replaced by the
next successful campaign. In simulation, every target has a hidden latent
value for evaluation, while an observed-target metric is reported only when
the planned target observation exists.

Model inputs are the 16 fields allowed by the corrected formal feature
contract. Hidden latent values, mechanism names, random seeds and generator
parameters are prohibited model features. Point and profile identifiers are
used only for causal history lookup and grouped reporting.

## Generator

The latent surface separates spatial and temporal components. Four static-focus
mechanisms use a source-scale amplitude, a deterministic profile multiplier and
a smooth chainage weight. The moving-focus mechanism integrates a positive
spatial influence whose centre changes over time. The scenario sign convention
is positive-down settlement in millimetres; source profiles with the opposite
display sign are used only as magnitude context and are not silently remapped.

Five temporal mechanisms are frozen:

1. uniform;
2. creep decay;
3. acceleration whose rate saturates;
4. two-event reactivation;
5. moving spatial focus.

The first three are development mechanisms. Reactivation and moving focus are
held out from model fit and interval calibration as mechanism challenges.
This distinction tests structural transfer inside a simulation and is not an
external validation claim.

The full 4 x 3 observation factorial crosses independent missingness, long gaps,
state-dependent missingness and complete planned-campaign failures with ordinary
Gaussian noise, occasional gross errors and a campaign-shared reference-datum
failure. Observation error is generated only after the latent surface.
Three chainage-spaced points from every corrected profile bound the factorial
workload while preserving all 14 profiles. The first three targeted observations of each point are protected only to
provide the declared minimum history; that rule is an explicit design choice.

## Time roles

For development mechanisms:

- target dates through 2022-10-18 with observed targets form model fit;
- 2023-01-17 through 2023-11-07 with observed targets form interval calibration;
- target dates from 2024-01-30 form future development evaluation.

All eligible origins of the two held-out mechanisms form the mechanism
challenge. B1 and IMM are fitted once on development-train rows. IMM parameters
are copied from the historical fixed B7 configuration and are not tuned against
new scenario outcomes.

## Metrics and intervals

Both methods are compared on exactly the same evaluation rows. Reported metrics
include latent-truth MAE for rate and increment, observed-target MAE on the
available subset, bias, RMSE, interval coverage/width and runtime. Results are
grouped by evaluation scope, dynamic mechanism, missingness and error mechanism.

B1 uses an unscaled absolute-residual conformal radius. IMM uses the absolute
residual divided by its raw predictive sigma. Both radii are fitted only on the
development calibration role with the finite-sample higher rule. Coverage is
empirical simulation coverage, not a universal guarantee.

## Reproducibility and completion boundary

Every input and output is hashed in a manifest. A second build must reproduce
all outputs marked deterministic. Wall-clock runtime is machine- and load-
dependent, remains hashed for provenance, and is explicitly marked
non-deterministic. Completion of v1 means that generation, adapter, B1/IMM
execution and leakage/causality checks pass. It does not select a final
industrial model, establish field accuracy or consume the legacy holdout.

"""vkm_world.validation — data-free guards for honest forecast validation.

* ``leakage``  forbidden estimator fields, time alignment, availability at origin, planned targets,
               disjoint sample sets, scanner for random-split API calls in source code;
* ``splits``   forward-only (rolling-origin) and grouped (leave-one-borehole/line-out) designs,
               rejection of random/plain k-fold splitters, sample-id list hashes, sealed test;
* ``access``   immutable frozen-candidate record and one-time test-access ledger;
* ``metrics``  point, interval and probabilistic scores, finite-sample conformal quantile.

Policy: ``docs/governance/VALIDATION_POLICY_RU.md``. These are guards and scores only; the preregistered
forecasting benchmark itself is a future phase and is not run from this package.
"""

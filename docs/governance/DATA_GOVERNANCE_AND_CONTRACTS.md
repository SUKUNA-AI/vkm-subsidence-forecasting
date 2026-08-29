# Data Governance and Contracts v2

- All operational paths are relative.
- `bundled_verified` requires existence, size and SHA-256 verification inside this archive.
- Model-ready, evaluation-only and private-generation data remain physically separated.
- Every experiment records dataset hash, feature-contract hash, target-contract hash, code commit, seed and environment.
- A model trained after test inspection receives a new test version; it cannot reuse the old test as untouched evidence.

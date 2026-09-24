# Data Governance and Contracts v3

- Current operational paths in main are relative to the repository root.
- Raw books/PDF/dissertations/ZIP/XLSX/GIS source containers live in the private `vkm-subsidence-forecasting_resourses` repository.
- Historical `configs/input_manifest.csv`, `source_manifest.csv` and `supplementary_source_manifest.csv` are provenance records; their original relative paths may refer to externalized bytes.
- An externalized payload is considered verified only when size and SHA-256 match the corresponding private snapshot/registry entry.
- Frozen v2.1, constraints and representation R1 manifests are immutable.
- Model-ready, evaluation-only and scorer/private data remain physically and logically separated.
- Every future experiment records dataset hash, feature-contract hash, target-contract hash, code commit, seed and environment.
- A model trained after test/evaluator inspection receives a new evaluation version; it cannot reuse the old test as untouched evidence.
- Historical results remain bound to their historical dataset and commit.
- Physical-world parameters must additionally record source scope, parameter scale (`LAB`, `MASSIF`, `CALIBRATED_EFFECTIVE_MODEL`), transferability, uncertainty and identifiability.

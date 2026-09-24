# Data Governance and Contracts v4

- Current operational paths in main are relative to the repository root.
- Raw scientific books/PDF/dissertations/ZIP/XLSX/GIS evidence lives in the private `vkm-subsidence-forecasting_resourses` repository.
- `configs/source_manifest.csv` and `configs/supplementary_source_manifest.csv` remain active provenance for external scientific sources.
- `configs/input_manifest.csv` and references to `SKRU1_ACTUAL_DATA_TABLES_v1/**` / `inputs/bootstrap/**` are historical provenance of a retired project-generated branch, not current input requirements.
- `SKRU1_ACTUAL_DATA_TABLES_v1`, old v3.x reconstructed/model-ready/EDA/target files and bootstrap packages have status **LEGACY_RETIRED**.
- Retired packages are not current scientific evidence, are not required for Physical Evidence Consolidation / Physical World v1 / OpenGeoSys, and are not re-imported into current main/private trees.
- Historical reproduction uses the corresponding historical Git commit; current trees do not maintain retired packages byte-for-byte.
- Frozen v2.1, constraints and representation R1 manifests are immutable. Historical provenance references inside them are not rewritten and do not create a current dependency on retired packages.
- Active external scientific evidence is verified by size/SHA-256 against the private registry/canonical copy.
- Model-ready, evaluation-only and scorer/private data remain logically separated for current experiments.
- Every future experiment records dataset hash, feature/target contract hashes, code commit, seed and environment.
- A model trained after test/evaluator inspection receives a new evaluation version; it cannot reuse the old test as untouched evidence.
- Historical results remain bound to their historical dataset and commit.
- Physical-world parameters additionally record source scope, parameter scale (`LAB`, `MASSIF`, `CALIBRATED_EFFECTIVE_MODEL`), transferability, uncertainty and identifiability.

Authoritative retirement policy:
[`LEGACY_DATA_RETIREMENT_2026-09-25_RU.md`](../LEGACY_DATA_RETIREMENT_2026-09-25_RU.md).

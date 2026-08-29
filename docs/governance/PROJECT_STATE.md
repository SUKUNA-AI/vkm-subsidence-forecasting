# Project State v2

## Verified bundled inputs

The five bootstrap artifacts and eleven primary sources are physically included in this bundle and verified against SHA-256 manifests. Their status is `bundled_verified`, not merely `present_in_previous_runtime`.

## Current scientific state

- spatial reconstruction v3.2 exists;
- EDA and target contracts exist;
- Gate A0 input verification and two-run reconstruction are complete;
- Gate A1 is `PASS_WITH_WARNINGS`: canonical next-planned tables and v1 manifests are frozen, executable leakage guards pass, and model-facing test access is sealed;
- T1 is ready for controlled train/validation baseline development through `skru1.splits.load_split_dataset`;
- T5 is technically prepared but remains exploratory because only 17 complete positive labels exist;
- hotfix and baseline scripts exist;
- deep model zoo, graph models, foundation models and LLM layer are planned but must be implemented and evaluated after Gate A/B;
- no final production-quality model claim is allowed from synthetic data.

The machine-readable Gate A1 authority is `artifacts/data_quality/gate_a1_report.json`; the reader-facing report is `docs/reports/GATE_A1_DATA_QUALITY_RU.md`.

## Path policy

All paths in manifests are repository-relative. Absolute paths from previous environments are historical provenance only and may not be used as execution inputs.

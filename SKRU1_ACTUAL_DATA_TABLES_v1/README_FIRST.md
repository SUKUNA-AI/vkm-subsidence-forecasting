# Historical data foundation snapshot

This directory preserves the inputs and provenance used by the first B1/B5/B6/B7
experiment (Gate B3, commit ce8e57e), plus inputs required by current reconstruction.
Current scenario data are SKRU1_SCENARIO_SIMULATION_V2_1; see
[canonical research state](../docs/CANONICAL_RESEARCH_STATE_RU.md).

- `01_reconstruction_v3_2/tables/` and `model_ready/`: historical generated data.
- `01_reconstruction_v3_2/evaluation_only/` and `private_generation/`: restricted historical layers, never model features.
- `02_eda_targets_v1/target_tables/`: historical next-planned target/feature contracts.
- `06_manifests/`: immutable inventories and hashes of the ORIGINAL full delivery.
- `04_excel_workbooks/` and remaining `07_original_archives/`: retained pending source-container review.

The duplicate convenience model-ready copy, superseded audit snapshots and two
byte-identical ZIP copies were removed during consolidation. They remain in
[Git history](https://github.com/SUKUNA-AI/vkm-subsidence-forecasting/tree/c5da1103154c29e2ffe9c52bbceacaa032f1690f/SKRU1_ACTUAL_DATA_TABLES_v1).
Original delivery inventories are historical evidence, not current-tree validators.
They were not rewritten. Primary ZIP copies remain under `inputs/bootstrap/` at
repository root. No historical numeric table or model score was rewritten.

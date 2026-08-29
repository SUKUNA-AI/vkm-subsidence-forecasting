# SKRU-1: actual data tables

This archive contains the actual generated data files, not merely documentation and not only nested ZIPs.

## Where the tables are

- `01_reconstruction_v3_2/tables/` — core reconstructed spatial, surveying, leveling, GNSS, InSAR and deformation tables.
- `01_reconstruction_v3_2/model_ready/` — leakage-safe model input tables.
- `01_reconstruction_v3_2/evaluation_only/` — targets and hidden synthetic truth for evaluation only.
- `01_reconstruction_v3_2/private_generation/` — generator parameters; never use as model features.
- `02_eda_targets_v1/tables/` — EDA summaries.
- `02_eda_targets_v1/target_tables/` — formal T1/T5/T6 targets and feature contracts.
- `03_model_ready_only_v3_2/` — convenience copy of the model-ready package.
- `04_excel_workbooks/` — Excel catalogs and dashboards.
- `05_audit_and_result_tables/` — audit and baseline result CSVs.
- `06_manifests/TABLE_INVENTORY.csv` — every CSV table with row count, column count, size and SHA-256.

The primary working dataset is v3.2. Older workbooks and audit snapshots are included only for traceability.

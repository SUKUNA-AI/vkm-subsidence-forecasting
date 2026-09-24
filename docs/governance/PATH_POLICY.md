# Portable Path Policy

1. Resolve the main project root from the directory containing `AGENTS.md` or from `--root`.
2. Current operational paths inside main must be relative to the main repository root.
3. Never hard-code `/mnt/data`, `E:\Диплом`, a user home directory or a drive letter in committed research code/config.
4. Active scientific source PDFs/books/dissertations and other evidence are externalized into the private resources repository.
5. `SKRU1_ACTUAL_DATA_TABLES_v1/**`, `inputs/bootstrap/**` and old v3.x reconstructed/model-ready/EDA/target packages are **LEGACY_RETIRED**. They are not active external dependencies and must not be re-imported into current trees.
6. The resources repository is resolved from `VKM_RESOURCES_ROOT` or by an explicitly documented local discovery rule; committed scientific code must not assume one machine-specific absolute path.
7. `configs/source_manifest.csv` and `configs/supplementary_source_manifest.csv` retain active source provenance and their external scientific bytes are hash-verified when the resources repository is available.
8. `configs/input_manifest.csv` and historical references to retired paths are historical provenance only. They do not require current byte payloads.
9. Frozen current releases (`v2.1`, constraints, representation R1) remain immutable. Historical path strings inside frozen manifests are not rewritten merely because their source branch was retired.
10. Missing/mismatched **current-core or active scientific evidence** is blocking. Retired legacy references are reported separately and are not verification failures.
11. Historical reproduction of the old v3.x/Gate B3 branch uses the corresponding historical Git commit, not restoration of retired packages into current main/private trees.
12. Temporary outputs belong only under `work/`.

See [`LEGACY_DATA_RETIREMENT_2026-09-25_RU.md`](../LEGACY_DATA_RETIREMENT_2026-09-25_RU.md).

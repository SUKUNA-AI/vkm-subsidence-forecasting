# Portable Path Policy

1. Resolve the main project root from the directory containing `AGENTS.md` or from `--root`.
2. Current operational paths inside main must be relative to the main repository root.
3. Never hard-code `/mnt/data`, `E:\Диплом`, a user home directory or a drive letter in committed research code/config.
4. Source PDFs, books, dissertations, historical bootstrap packages and `SKRU1_ACTUAL_DATA_TABLES_v1` are externalized into the private resources repository.
5. The resources repository is resolved from `VKM_RESOURCES_ROOT` or by an explicitly documented local discovery rule; committed scientific code must not assume one machine-specific absolute path.
6. Historical `configs/input_manifest.csv`, `source_manifest.csv` and `supplementary_source_manifest.csv` retain their original paths/hashes as provenance records; they are not proof that those bytes remain bundled in current main.
7. An externalized file is valid only when the corresponding private snapshot has the expected size and SHA-256.
8. Frozen current releases (`v2.1`, constraints, representation R1) remain immutable; externalization must not change their manifests or scientific values.
9. A stale absolute path is not evidence of file presence.
10. Missing/mismatched current-core files are blocking failures. Missing private resources may produce `PASS_CORE_EXTERNAL_UNCHECKED`, but a full reproducibility check requires the private archive and must hash-verify it.

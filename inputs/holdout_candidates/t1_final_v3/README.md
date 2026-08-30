# Local T1 final-holdout package

This directory intentionally contains no holdout data. Real candidate files
are ignored by Git and must be supplied by an authorized data owner:

1. `package_manifest.json`;
2. `holdout_origins.csv`;
3. `holdout_targets.sealed.csv`.

Before preparing a package, read
`docs/governance/FINAL_HOLDOUT_INTAKE_V3.md`. Run only the non-consuming status
check first:

```powershell
.\.venv\Scripts\python.exe scripts\run_holdout_v3.py --phase status
```

Do not open the sealed targets manually. `--phase evaluate-once` is terminal:
the access is consumed even if validation fails after labels are parsed.

# SKRU1 Project Documentation v2 — FULL, self-contained

This bundle fixes two defects of v1:

1. The modelling programme was too narrow and underrepresented DL, spatio-temporal models, foundation models and LLM-support experiments.
2. The inventory used stale absolute paths such as `/mnt/data/...` and marked files `present` even though the files were not included in the documentation archive.

## What is physically bundled

- 5 verified bootstrap artifacts in `inputs/bootstrap/`;
- 11 primary sources in `inputs/sources/primary/`;
- 1 supplementary source in `inputs/sources/supplementary/`;
- portable relative-path manifests;
- expanded model research programme;
- Codex prompts for classical ML, DL, GNN, time-series foundation models, hybrid models and LLM support;
- a cross-platform verifier.

## First action

```bash
python scripts/verify_inputs.py --root .
```

On Windows PowerShell:

```powershell
python .\scripts\verify_inputs.py --root .
```

Do not use `/mnt/data`, `E:\Диплом` or any other hard-coded host path in code. All operational paths are relative to the extracted bundle/repository root.

## Scientific boundary

DL breadth is expanded aggressively, but model count is not treated as evidence. Every complex model must beat strong temporal baselines under temporal, spatial and OOD validation. LLMs are an auxiliary source-grounded interface and experiment-analysis layer; direct LLM numeric prediction is not accepted as the primary scientific algorithm.

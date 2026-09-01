# SKRU1 reproducible research entrypoint

## Current execution boundary

The repository now contains the frozen Gate B5 train-only benchmark and the
Gate B6 expanded classical/probabilistic/small-data workflow. Gate B5/B6 model
workers can access only `t1_v1/train`; historical validation, the disclosed T1
test and the missing future/external holdout are not worker inputs. Any B6
result is therefore `train_only_internal_research`, not a final quality claim.
Gate B6 completed with `PASS_NO_NEW_PRIMARY`: B7 remains the single suite-v4
primary with rolling MAE 5.640 mm/year and 10.64% train-only skill versus B1.
Gate C0 then froze a causal sequence protocol over the same 911 train origins
with status `PASS_PROTOCOL_FROZEN`; it performed zero model-training calls and
loaded zero historical-validation, disclosed-test or future-holdout rows.

Read these authorities before running experiments:

1. `docs/governance/PROJECT_STATE.md`;
2. `docs/governance/PATH_POLICY.md`;
3. `docs/governance/GATE_B5_B6_TRAIN_ONLY_PROTOCOL.md`;
4. `configs/gate_b5.yaml` and `configs/gate_b6.yaml`;
5. `docs/governance/GATE_C_PROTOCOL.md` and `configs/gate_c.yaml`;
6. `docs/reports/GATE_B6_EXPANDED_SCREENING_RU.md`;
7. `docs/reports/GATE_C0_SEQUENCE_PROTOCOL_RU.md`.

The reproducibility entrypoints are:

```powershell
.\.venv\Scripts\python.exe scripts\verify_inputs.py --root .
.\.venv\Scripts\python.exe scripts\run_gate_b5.py --phase validate
.\.venv\Scripts\python.exe scripts\run_gate_b6.py --phase preflight
.\.venv\Scripts\python.exe scripts\run_gate_b6.py --phase validate
.\.venv\Scripts\python.exe scripts\run_gate_c.py --phase validate
```

`run_gate_b6.py --phase all` is intentionally expensive: it dispatches the
complete frozen temporal and spatial job catalogue. Reuse of completed shards
is hash/schema guarded. A governance-excluded external-model row remains only
in the immutable historical B5 registry; no adapter, checkpoint, API path or
prediction shard is available. The executable B6 catalogue contains 22
models.
The one-shot future holdout policy now hashes and consumes
`artifacts/governance/final_candidate_suite_v4.json`; it remains
`PENDING_DATA` until a real eligible package is supplied.

The next executable research stage is Gate C1: five-seed compact GRU/LSTM/TCN
and probabilistic recurrent screening under the frozen Gate C0 manifests.
Early stopping means only inner rolling validation inside `t1_v1/train`, never
the historical validation split.

## Bundled data foundation

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

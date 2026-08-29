#!/usr/bin/env python3
"""Build and execute the inspectable Gate A1 audit notebook."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

# Jupyter's Windows ACL hardening cannot run inside the restricted workspace
# sandbox. Connection files are kept under ignored ``work/`` and may therefore
# use the documented insecure-write fallback without touching user profiles.
os.environ.setdefault("JUPYTER_ALLOW_INSECURE_WRITES", "true")

import nbformat
from nbclient import NotebookClient


SCRIPT_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=SCRIPT_ROOT, help="Repository root")
    parser.add_argument("--timeout", type=int, default=180, help="Per-cell timeout in seconds")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    runtime_root = root / "work" / "jupyter_runtime"
    for name, directory in {
        "IPYTHONDIR": runtime_root / "ipython",
        "JUPYTER_CONFIG_DIR": runtime_root / "config",
        "JUPYTER_DATA_DIR": runtime_root / "data",
        "JUPYTER_RUNTIME_DIR": runtime_root / "runtime",
    }.items():
        directory.mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(directory)
    report_path = root / "artifacts" / "data_quality" / "gate_a1_report.json"
    if not report_path.is_file():
        raise FileNotFoundError("Run scripts/run_gate_a1.py before building the notebook")

    notebook = build_notebook()
    notebook_path = root / "notebooks" / "01_gate_a1_data_audit.ipynb"
    notebook_path.parent.mkdir(parents=True, exist_ok=True)
    client = NotebookClient(
        notebook,
        timeout=args.timeout,
        kernel_name="python3",
        resources={"metadata": {"path": str(root)}},
        allow_errors=False,
    )
    executed = client.execute(cwd=str(root))
    nbformat.write(executed, notebook_path)
    print(notebook_path)
    return 0


def build_notebook() -> nbformat.NotebookNode:
    cells = [
        nbformat.v4.new_markdown_cell(
            """# Gate A1 — inspectable data audit

## TL;DR

This notebook is a reader-facing view of the executable Gate A1 evidence. It does not create an alternative split or feature set: all tables below come from the machine report and frozen manifests produced by `scripts/run_gate_a1.py`.

The expected decision is `PASS_WITH_WARNINGS`: no critical contract/leakage failures, T1 ready for baseline development through manifests, T5 technically frozen but statistically sparse, and test sealed until a final candidate is frozen."""
        ),
        nbformat.v4.new_markdown_cell(
            """## Context and methods

Canonical T1 sources are the next-planned feature/target tables plus the formal feature and target contracts. The audit checks grain, uniqueness, temporal split boundaries, leakage, train-only preprocessing, test access, missingness, drift, join coverage, and three dependency-aware validation designs. Historical `next_cycle_*` tables are comparison-only."""
        ),
        nbformat.v4.new_code_cell(
            """from pathlib import Path
import json
import pandas as pd

root = Path.cwd().resolve()
while not (root / 'pyproject.toml').is_file():
    if root.parent == root:
        raise RuntimeError('Repository root not found')
    root = root.parent

artifact_dir = root / 'artifacts' / 'data_quality'
report = json.loads((artifact_dir / 'gate_a1_report.json').read_text(encoding='utf-8'))
split_summary = pd.read_csv(artifact_dir / 'split_summary.csv')
checks = pd.read_csv(artifact_dir / 'duplicate_and_grain_checks.csv')
findings = pd.read_csv(artifact_dir / 'gate_a1_findings.csv')
membership = pd.read_csv(artifact_dir / 'membership_inconsistency_mapping.csv')
missingness = pd.read_csv(artifact_dir / 'feature_missingness_by_split.csv')
drift = pd.read_csv(artifact_dir / 'drift_summary.csv')
validation_design = pd.read_csv(artifact_dir / 'validation_design_summary.csv')

pd.set_option('display.max_colwidth', 100)
{
    'gate': report['gate'],
    'status': report['status'],
    'critical_failures': report['summary']['critical_failures'],
    'checks': report['summary']['checks'],
}"""
        ),
        nbformat.v4.new_markdown_cell("## Data: governed inputs and frozen manifests"),
        nbformat.v4.new_code_cell(
            """pd.DataFrame([
    {'role': 'canonical', 'name': name, **meta}
    for name, meta in report['canonical_inputs'].items()
] + [
    {'role': 'historical_only', 'name': name, **meta}
    for name, meta in report['historical_only_inputs'].items()
])[['role', 'name', 'path', 'sha256']]"""
        ),
        nbformat.v4.new_code_cell(
            """split_summary[[
    'task', 'version', 'split', 'rows',
    'current_date_min', 'current_date_max',
    'target_date_min', 'target_date_max',
    'points', 'profiles', 'missing_feature_fraction',
    'positive', 'negative', 'censored', 'sample_ids_sha256'
]]"""
        ),
        nbformat.v4.new_markdown_cell("## Results: executable checks and dependency structure"),
        nbformat.v4.new_code_cell(
            """checks.groupby(['severity', 'status'], as_index=False).size().sort_values(['severity', 'status'])"""
        ),
        nbformat.v4.new_code_cell(
            """checks.loc[checks['status'].ne('PASS'), ['check_id', 'severity', 'dimension', 'observed', 'expected', 'details']]"""
        ),
        nbformat.v4.new_code_cell("report['grain']"),
        nbformat.v4.new_markdown_cell(
            """### Membership reconciliation

The row-level mapping below proves why 18 source membership inconsistencies affect only 6 model origins. It distinguishes mapped WORK targets, REF rows outside the model universe, and WORK rows with no eligible prior origin."""
        ),
        nbformat.v4.new_code_cell(
            """membership.groupby(['mapping_reason', 'maps_to_unlabeled_origin'], as_index=False).size()"""
        ),
        nbformat.v4.new_code_cell(
            """membership[[
    'campaign_id', 'point_id', 'profile_id', 'point_type',
    'target_origin_sample_id', 'label_status', 'mapping_reason'
]]"""
        ),
        nbformat.v4.new_markdown_cell("### Missingness and temporal drift"),
        nbformat.v4.new_code_cell(
            """missingness.sort_values('missing_fraction', ascending=False).head(20)[[
    'task', 'split', 'feature', 'missing_count', 'missing_fraction'
]]"""
        ),
        nbformat.v4.new_code_cell(
            """drift.loc[
    drift['feature_type'].eq('numeric') & drift['comparison_split'].str.startswith('test')
].sort_values('absolute_smd', ascending=False).head(20)[[
    'task', 'comparison_split', 'feature', 'absolute_smd',
    'missing_fraction_reference', 'missing_fraction_comparison'
]]"""
        ),
        nbformat.v4.new_markdown_cell("### Dependency-aware validation designs"),
        nbformat.v4.new_code_cell(
            """validation_design.groupby('design').agg(
    folds=('fold_id', 'nunique'),
    min_train_rows=('train_rows', 'min'),
    max_train_rows=('train_rows', 'max'),
    min_validation_rows=('validation_rows', 'min'),
    max_validation_rows=('validation_rows', 'max'),
)"""
        ),
        nbformat.v4.new_markdown_cell("## Findings"),
        nbformat.v4.new_code_cell(
            """findings[['finding_id', 'severity', 'status', 'evidence', 'impact', 'remediation']]"""
        ),
        nbformat.v4.new_markdown_cell(
            """## Takeaways

1. T1 can proceed to controlled baselines, but only through `skru1.splits.load_split_dataset`; test remains sealed.
2. Random row splitting is invalid because 1,274 origins represent only 98 repeated point trajectories inside 14 profiles.
3. The 18-versus-6 discrepancy is reconciled and no longer an unexplained inconsistency.
4. T5 has too few positives for strong safety claims; use it as an exploratory secondary task with uncertainty-aware reporting.
5. The leave-zone-out v1 groups are geometric proxy quadrants, not authoritative engineering zones.
6. `sample_id` must be treated as opaque in v1 because 58 IDs retain a historical target token."""
        ),
    ]
    return nbformat.v4.new_notebook(
        cells=cells,
        metadata={
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python", "version": "3.13"},
            "gate_a1": {"source": "artifacts/data_quality/gate_a1_report.json", "schema_version": 1},
        },
    )


if __name__ == "__main__":
    raise SystemExit(main())

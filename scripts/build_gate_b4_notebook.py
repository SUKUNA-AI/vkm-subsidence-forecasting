#!/usr/bin/env python3
"""Build and execute the reader-facing Gate B4 train-only notebook."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

os.environ.setdefault("JUPYTER_ALLOW_INSECURE_WRITES", "true")

import nbformat
from nbclient import NotebookClient


SCRIPT_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=SCRIPT_ROOT)
    parser.add_argument("--timeout", type=int, default=240)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    artifact_dir = root / "artifacts" / "model_selection" / "t1_b4_train_only_v1"
    required = [
        artifact_dir / "gate_b4_report.json",
        artifact_dir / "validation_report.json",
        artifact_dir / "aggregate_metrics.csv",
        artifact_dir / "fold_metrics.csv",
        artifact_dir / "transition_metrics.csv",
        artifact_dir / "robust_tuning.csv",
        artifact_dir / "robustness_diagnostics.csv",
        root / "artifacts" / "governance" / "final_holdout_v3_status.json",
        root / "artifacts" / "governance" / "final_candidate_suite_v3.json",
    ]
    missing = [path for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            "Run Gate B4 and holdout status before building the notebook: "
            + ", ".join(str(path) for path in missing)
        )
    report = json.loads(
        (artifact_dir / "gate_b4_report.json").read_text(encoding="utf-8")
    )
    notebook = build_notebook(report)
    runtime_root = root / "work" / "jupyter_runtime_b4"
    for name, directory in {
        "IPYTHONDIR": runtime_root / "ipython",
        "JUPYTER_CONFIG_DIR": runtime_root / "config",
        "JUPYTER_DATA_DIR": runtime_root / "data",
        "JUPYTER_RUNTIME_DIR": runtime_root / "runtime",
    }.items():
        directory.mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(directory)
    notebook_path = root / "notebooks" / "05_gate_b4_robust_innovation.ipynb"
    notebook_path.parent.mkdir(parents=True, exist_ok=True)
    executed = NotebookClient(
        notebook,
        timeout=args.timeout,
        kernel_name="python3",
        resources={"metadata": {"path": str(root)}},
        allow_errors=False,
    ).execute(cwd=str(root))
    nbformat.write(executed, notebook_path)
    print(notebook_path)
    return 0


def build_notebook(report: dict) -> nbformat.NotebookNode:
    observed = report["screening"]["observed"]
    cells = [
        nbformat.v4.new_markdown_cell(
            f"""# Gate B4 — robust innovation IMM только внутри train

## tl;dr

Gate B4 использовал только 911 origins из `t1_v1/train`; исторические validation и раскрытый test не загружались. Последняя train-кампания 2023-11-07 (88 origins) была заранее отделена как внутренний audit-tail, а все параметры выбирались nested rolling resampling внутри outer-train.

`B8_student_t_robust_imm` меняет только Gaussian observation likelihood замороженного B7 на bounded-influence Student-t. Из фиксированной сетки `ν={{3,5,10,30}}` выбран `ν={report['model']['selected_student_t_df']:g}`.

На внутреннем temporal tail общая MAE B8 равна **{observed['internal_temporal_b8_mae']:.3f} мм/год** против **{observed['internal_temporal_b7_mae']:.3f}** у B7; leave-zone MAE — **{observed['leave_zone_b8_mae']:.3f}** против **{observed['leave_zone_b7_mae']:.3f}**. Но целевой `volatile_or_gap` MAE стал хуже на **{-observed['volatile_or_gap_improvement_vs_b7_percent']:.2f}%**, а требовалось улучшение не менее 10%.

Итог: screening не пройден, B8 имеет статус `train_only_research_recorded`, а primary в заранее замороженном наборе для будущего holdout остаётся B7. Новый holdout пока отсутствует (`PENDING_DATA`)."""
        ),
        nbformat.v4.new_markdown_cell(
            """## Context & Methods

Узкая гипотеза: тяжёлые хвосты observation likelihood должны уменьшить влияние больших innovations после gaps/noisy measurements, сохранив динамическую структуру B7.

Для каждой scalar assimilation — settlement, origin-known rate и recent acceleration — Student-t likelihood рассчитывается по базовой innovation variance. Influence weight равен `min(1, (ν+1)/(ν+z²))`, но не ниже 0.05; measurement variance увеличивается обратно пропорционально weight. Все process-noise, retention и Markov-transition параметры B7 неизменны.

Outer evidence включает 1 внутренний temporal tail, 5 rolling-origin, 14 forward leave-profile-out и 4 forward leave-zone-out folds. В каждом outer fit `ν` выбирается на трёх expanding-window inner folds. Objective: 50% normalized overall MAE и 50% normalized `volatile_or_gap` MAE, обе относительно frozen B7 на тех же rows. Thresholds transition proxy fit только на соответствующем train.

Канонический validation остаётся исторической описательной диагностикой Gate B2/B3, но не является источником параметров B8. Test и будущий holdout не имеют executable path в Gate B4 runner."""
        ),
        nbformat.v4.new_code_cell(
            """from pathlib import Path
import json
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

root = Path.cwd().resolve()
while not (root / 'pyproject.toml').is_file():
    if root.parent == root:
        raise RuntimeError('Repository root not found')
    root = root.parent

artifact_dir = root / 'artifacts' / 'model_selection' / 't1_b4_train_only_v1'
report = json.loads((artifact_dir / 'gate_b4_report.json').read_text(encoding='utf-8'))
validation = json.loads((artifact_dir / 'validation_report.json').read_text(encoding='utf-8'))
candidate = json.loads((artifact_dir / 'research_candidate.json').read_text(encoding='utf-8'))
suite = json.loads((root / 'artifacts' / 'governance' / 'final_candidate_suite_v3.json').read_text(encoding='utf-8'))
holdout = json.loads((root / 'artifacts' / 'governance' / 'final_holdout_v3_status.json').read_text(encoding='utf-8'))
folds = pd.read_csv(artifact_dir / 'fold_contracts.csv')
tuning = pd.read_csv(artifact_dir / 'robust_tuning.csv')
aggregate = pd.read_csv(artifact_dir / 'aggregate_metrics.csv')
fold_metrics = pd.read_csv(artifact_dir / 'fold_metrics.csv')
transition = pd.read_csv(artifact_dir / 'transition_metrics.csv')
robustness = pd.read_csv(artifact_dir / 'robustness_diagnostics.csv')

pd.set_option('display.max_columns', 30)
{
    'candidate_id': candidate['candidate_id'],
    'candidate_status': candidate['status'],
    'selected_student_t_df': candidate['selected_parameters']['student_t_df'],
    'predeclared_holdout_primary': suite['primary_model_id'],
    'holdout_status': holdout['status'],
    'machine_validation': validation['summary'],
}"""
        ),
        nbformat.v4.new_markdown_cell("## Data"),
        nbformat.v4.new_code_cell(
            """pd.DataFrame([
    {
        'source': report['data']['source'],
        'rows': report['data']['rows'],
        'points': report['data']['points'],
        'profiles': report['data']['profiles'],
        'target_date_min': report['data']['target_date_min'],
        'target_date_max': report['data']['target_date_max'],
        'role': 'nested model selection and internal audit',
    },
    {
        'source': 't1_v1/validation',
        'rows': report['data']['historical_validation_rows_loaded'],
        'points': np.nan,
        'profiles': np.nan,
        'target_date_min': None,
        'target_date_max': None,
        'role': 'historical only; not loaded',
    },
    {
        'source': 't1_v1/test',
        'rows': report['data']['current_test_rows_loaded'],
        'points': np.nan,
        'profiles': np.nan,
        'target_date_min': None,
        'target_date_max': None,
        'role': 'disclosed historical diagnostic; not loaded',
    },
])"""
        ),
        nbformat.v4.new_code_cell(
            """fold_summary = folds.groupby('design').agg(
    folds=('fold_id', 'nunique'),
    min_train_rows=('train_rows', 'min'),
    max_train_rows=('train_rows', 'max'),
    validation_rows=('validation_rows', 'sum'),
)
assert (pd.to_datetime(folds['train_target_date_max']) < pd.to_datetime(folds['validation_target_date_min'])).all()
fold_summary"""
        ),
        nbformat.v4.new_markdown_cell("## Results"),
        nbformat.v4.new_code_cell(
            """model_order = [
    'B1_persistence_last_rate',
    'B5_fixed_kalman',
    'B6_adaptive_kalman',
    'B7_two_regime_imm',
    'B8_student_t_robust_imm',
]
aggregate.pivot(index='model_id', columns='design', values='mae').loc[model_order].round(3)"""
        ),
        nbformat.v4.new_code_cell(
            """full_tuning = tuning.loc[
    tuning['tuning_context'].eq('final_spec::full_train'),
    ['student_t_df', 'overall_normalized_mae', 'volatile_or_gap_normalized_mae', 'tuning_score', 'selected'],
]
full_tuning.round(4)"""
        ),
        nbformat.v4.new_code_cell(
            """internal_transition = transition.loc[
    transition['design'].eq('internal_temporal')
    & transition['scope'].eq('mechanism')
    & transition['model_id'].isin(['B7_two_regime_imm', 'B8_student_t_robust_imm']),
    ['model_id', 'segment', 'rows', 'points', 'profiles', 'mae'],
]
internal_transition.round(3)"""
        ),
        nbformat.v4.new_code_cell(
            """labels = {
    'B1_persistence_last_rate': 'B1 persistence',
    'B7_two_regime_imm': 'B7 Gaussian IMM',
    'B8_student_t_robust_imm': 'B8 Student-t IMM',
}
fig, axes = plt.subplots(2, 2, figsize=(15, 10))

overall = aggregate.loc[aggregate['model_id'].isin(labels)].pivot(
    index='design', columns='model_id', values='mae'
)[list(labels)].rename(columns=labels)
overall.plot(kind='bar', ax=axes[0, 0], width=0.82)
axes[0, 0].set_title('MAE by train-only validation design')
axes[0, 0].set_ylabel('MAE, mm/year')
axes[0, 0].set_xlabel('Design')
axes[0, 0].tick_params(axis='x', rotation=20)
axes[0, 0].grid(axis='y', alpha=0.25)

transition_plot = internal_transition.pivot(index='segment', columns='model_id', values='mae')[
    ['B7_two_regime_imm', 'B8_student_t_robust_imm']
].rename(columns=labels)
transition_plot.plot(kind='bar', ax=axes[0, 1], width=0.75)
axes[0, 1].set_title('Internal temporal MAE by transition proxy')
axes[0, 1].set_ylabel('MAE, mm/year')
axes[0, 1].set_xlabel('Segment')
axes[0, 1].tick_params(axis='x', rotation=20)
axes[0, 1].grid(axis='y', alpha=0.25)

axes[1, 0].plot(full_tuning['student_t_df'], full_tuning['overall_normalized_mae'], marker='o', label='Overall / B7')
axes[1, 0].plot(full_tuning['student_t_df'], full_tuning['volatile_or_gap_normalized_mae'], marker='o', label='Volatile-gap / B7')
axes[1, 0].axhline(1.0, color='black', linestyle='--', linewidth=1)
axes[1, 0].set_xscale('log')
axes[1, 0].set_title('Nested full-train tuning ratios')
axes[1, 0].set_xlabel('Student-t degrees of freedom')
axes[1, 0].set_ylabel('Normalized MAE')
axes[1, 0].grid(alpha=0.25)

zone = fold_metrics.loc[
    fold_metrics['design'].eq('train_leave_zone_out')
    & fold_metrics['model_id'].isin(['B7_two_regime_imm', 'B8_student_t_robust_imm'])
].pivot(index='held_out_group', columns='model_id', values='mae').rename(columns=labels)
zone.plot(kind='bar', ax=axes[1, 1], width=0.75)
axes[1, 1].set_title('Leave-zone-out MAE on train audit tail')
axes[1, 1].set_ylabel('MAE, mm/year')
axes[1, 1].set_xlabel('Held-out proxy zone')
axes[1, 1].tick_params(axis='x', rotation=0)
axes[1, 1].grid(axis='y', alpha=0.25)

for ax in axes.flat:
    ax.legend(fontsize=8)
plt.tight_layout()
plt.show()"""
        ),
        nbformat.v4.new_code_cell(
            """temporal_robustness = robustness.loc[robustness['design'].eq('internal_temporal')]
temporal_robustness.round(4)"""
        ),
        nbformat.v4.new_code_cell(
            """screening = pd.DataFrame([
    {'criterion': key, 'passed': value}
    for key, value in report['screening']['checks'].items()
])
screening"""
        ),
        nbformat.v4.new_code_cell(
            """assert validation['status'] == 'PASS'
assert validation['summary']['failed'] == 0
assert validation['summary']['checks'] >= 41
assert report['historical_validation_loaded'] is False
assert report['current_t1_test_loaded'] is False
assert report['new_final_holdout_loaded'] is False
assert candidate['selection_data'] == ['t1_v1/train']
assert suite['primary_model_id'] == 'B7_two_regime_imm'
assert suite['primary_selected_from_holdout'] is False
assert holdout['status'] == 'PENDING_DATA'
{
    'qa': validation['summary'],
    'candidate_status': candidate['status'],
    'screening_passed': candidate['screening_passed'],
    'holdout_primary': suite['primary_model_id'],
    'new_holdout_status': holdout['status'],
}"""
        ),
        nbformat.v4.new_markdown_cell(
            """## Takeaways

1. **B8 не подтвердил узкую гипотезу.** В full-train nested tuning даже лучший `ν=30` имеет objective 1.0136; `volatile_or_gap` normalized MAE равна 1.0072, то есть хуже B7. На 8 rows внутреннего temporal tail B8 также хуже B7 на 0.45%, далеко от порога +10%.
2. **Общая и spatial ошибки улучшились локально.** Internal temporal MAE ниже B7 на 3.06%, leave-profile на 3.09%, leave-zone на 3.12%; spatial degradation к собственному temporal B8 менее 0.5%.
3. **Rolling evidence предостерегает от повышения статуса.** На 292 pooled rolling origins B8 хуже B7 по MAE на 4.52%.
4. **Robust mechanism действительно активен.** На внутреннем temporal tail downweighted 897 из 4488 regime-channel updates; это доказывает работу механизма, но не целевое улучшение качества.
5. **Governance не меняется по результату.** B8 остаётся `train_only_research_recorded`; заранее объявленный primary для one-shot future/external holdout — B7. B1/B5/B6/B8 будут только контекстными comparators, без выбора победителя после открытия.
6. **Финальная внешняя валидность пока неизвестна.** Реального нового пакета нет, статус intake — `PENDING_DATA`; synthetic fixtures и старые validation/test не заменяют честный holdout."""
        ),
    ]
    return nbformat.v4.new_notebook(
        cells=cells,
        metadata={
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python", "version": "3.13"},
            "gate_b4": {
                "source": "artifacts/model_selection/t1_b4_train_only_v1",
                "schema_version": 1,
                "selection_data": ["t1_v1/train"],
                "historical_validation_loaded": False,
                "current_test_loaded": False,
                "new_holdout_status": "PENDING_DATA",
            },
        },
    )


if __name__ == "__main__":
    raise SystemExit(main())

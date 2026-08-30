#!/usr/bin/env python3
"""Build and execute the reader-facing Gate B3 IMM notebook."""

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
    artifact_dir = root / "artifacts" / "model_selection" / "t1_b3_v1"
    required = [
        artifact_dir / "gate_b3_report.json",
        artifact_dir / "validation_report.json",
        artifact_dir / "audit_reconciliation.json",
        artifact_dir / "aggregate_metrics.csv",
        artifact_dir / "transition_metrics.csv",
        artifact_dir / "problem_transition_metrics.csv",
        artifact_dir / "interval_metrics.csv",
        artifact_dir / "regime_summary.csv",
    ]
    missing = [path for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            "Run Gate B3 development and authoritative audit before the notebook: "
            + ", ".join(str(path) for path in missing)
        )
    report = json.loads(
        (artifact_dir / "gate_b3_report.json").read_text(encoding="utf-8")
    )
    notebook = build_notebook(report)
    runtime_root = root / "work" / "jupyter_runtime_b3"
    for name, directory in {
        "IPYTHONDIR": runtime_root / "ipython",
        "JUPYTER_CONFIG_DIR": runtime_root / "config",
        "JUPYTER_DATA_DIR": runtime_root / "data",
        "JUPYTER_RUNTIME_DIR": runtime_root / "runtime",
    }.items():
        directory.mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(directory)
    notebook_path = root / "notebooks" / "04_gate_b3_imm.ipynb"
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


def build_notebook(report: dict) -> nbformat.NotebookNode:
    observed = report["screening"]["observed"]
    selected = report["model"]["selected_parameters"]
    cells = [
        nbformat.v4.new_markdown_cell(
            f"""# Gate B3 — two-regime IMM для переходных режимов

## tl;dr

Gate B3 выполнен по заранее зафиксированному протоколу только на 911 train- и 130 validation-origins. B1, B5 и B6 прочитаны как неизменяемые prediction rows Gate B2; они не переобучались. Текущий T1 test не загружался.

`B7_two_regime_imm` выбрал `q_stable={selected['q_stable']:g}`, `q_transition={selected['q_transition']:g}`, `p_stable_stay={selected['p_stable_stay']:g}`, `p_transition_stay={selected['p_transition_stay']:g}`. Temporal MAE снизилась до **{observed['temporal_mae']:.3f} мм/год** против **{observed['temporal_b6_mae']:.3f}** у B6. Accelerating MAE стала **{observed['accelerating_mae']:.3f}** против **{observed['accelerating_b1_mae']:.3f}** у B1.

Строгий screening всё же не пройден: `accelerating + volatile_or_gap` улучшен против B1 только на **{observed['problem_transition_improvement_vs_b1_percent']:.1f}%** вместо 10%, volatile/gap MAE хуже B1, а leave-zone degradation к собственному temporal B7 равен **{observed['leave_zone_mae_degradation_vs_temporal_percent']:.1f}%** при лимите 5%. 95% coverage равен **{observed['coverage_95_empirical']:.3f}**.

Итог — сильный новый comparator и частично подтверждённая структурная гипотеза, но статус остаётся `validation_recorded`. Финальный вывод требует нового future/external holdout."""
        ),
        nbformat.v4.new_markdown_cell(
            """## Context & Methods

Узкая гипотеза Gate B3: два динамических режима должны исправить известную accelerating/volatile transition error B6 и оставить temporal/spatial performance устойчивой. До outer-run были заморожены архитектура, сетка из 16 вариантов, train-only selection objective и девять критериев screening.

Оба режима используют состояние `[settlement, velocity, acceleration]`. Stable regime имеет сильное затухание acceleration и малый jerk noise; transition regime — почти сохраняющуюся acceleration и высокий jerk noise. Standard IMM смешивает состояния по Markov matrix и обновляет regime probabilities по причинным innovations settlement, uncertainty-derived last-rate и origin-known recent-acceleration.

В каждом outer fold параметры выбираются на трёх expanding-window inner folds. Objective — 50% normalized overall MAE и 50% normalized MAE на `accelerating + volatile_or_gap`, обе относительно B1 на тех же inner validation rows. Thresholds proxy fit только на inner train.

### Контрольные границы

- 1 temporal, 5 rolling-origin, 14 leave-profile-out и 4 leave-zone-out folds идентичны Gate B2;
- B1/B5/B6 prediction rows защищены SHA-256 и не пересчитываются;
- интервалы калибруются по 292 nested OOF origins только из train;
- причинная история обрезается по current_date каждого origin;
- model-facing test loader отсутствует;
- пустой held-out group и его CSV-представление NA примирены отдельным audit-adapter без изменения model outputs."""
        ),
        nbformat.v4.new_code_cell(
            """from pathlib import Path
import json
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import yaml

root = Path.cwd().resolve()
while not (root / 'pyproject.toml').is_file():
    if root.parent == root:
        raise RuntimeError('Repository root not found')
    root = root.parent

artifact_dir = root / 'artifacts' / 'model_selection' / 't1_b3_v1'
report = json.loads((artifact_dir / 'gate_b3_report.json').read_text(encoding='utf-8'))
validation_report = json.loads((artifact_dir / 'validation_report.json').read_text(encoding='utf-8'))
reconciliation = json.loads((artifact_dir / 'audit_reconciliation.json').read_text(encoding='utf-8'))
candidate = json.loads((artifact_dir / 'development_candidate.json').read_text(encoding='utf-8'))
holdout_policy = yaml.safe_load((root / 'configs' / 'final_holdout_v2.yaml').read_text(encoding='utf-8'))
folds = pd.read_csv(artifact_dir / 'fold_contracts.csv')
tuning = pd.read_csv(artifact_dir / 'imm_tuning.csv')
aggregate = pd.read_csv(artifact_dir / 'aggregate_metrics.csv')
fold_metrics = pd.read_csv(artifact_dir / 'fold_metrics.csv')
transition = pd.read_csv(artifact_dir / 'transition_metrics.csv')
problem = pd.read_csv(artifact_dir / 'problem_transition_metrics.csv')
regimes = pd.read_csv(artifact_dir / 'regime_summary.csv')
intervals = pd.read_csv(artifact_dir / 'interval_metrics.csv')

pd.set_option('display.max_columns', 30)
{
    'candidate_id': candidate['candidate_id'],
    'candidate_status': candidate['status'],
    'screening_passed': candidate['screening_passed'],
    'current_t1_test_used': candidate['current_t1_test_used'],
    'final_holdout_status': holdout_policy['status'],
    'machine_validation': validation_report['summary'],
    'audit_model_outputs_changed': reconciliation['model_outputs_changed'],
}"""
        ),
        nbformat.v4.new_markdown_cell("## Data"),
        nbformat.v4.new_code_cell(
            """pd.DataFrame([
    {
        'split': 'train',
        'rows': report['data']['train_rows'],
        'target_date_max': report['data']['train_target_date_max'],
        'sample_ids_sha256': report['data']['train_sample_ids_sha256'],
        'role': 'fit + nested tuning + OOF calibration',
    },
    {
        'split': 'validation',
        'rows': report['data']['validation_rows'],
        'target_date_max': report['data']['validation_target_date_max'],
        'sample_ids_sha256': report['data']['validation_sample_ids_sha256'],
        'role': 'outer development evidence',
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
assert (pd.to_datetime(tuning['train_target_date_max']) < pd.to_datetime(tuning['validation_target_date_min'])).all()
assert tuning.groupby('tuning_context')['candidate_key'].nunique().eq(16).all()
fold_summary"""
        ),
        nbformat.v4.new_code_cell(
            """selected_by_context = (
    tuning.loc[tuning['selected'].astype(bool), ['tuning_context', 'candidate_key']]
    .drop_duplicates()
)
selected_by_context['candidate_key'].value_counts().rename('selected_contexts').to_frame()"""
        ),
        nbformat.v4.new_markdown_cell("## Results"),
        nbformat.v4.new_code_cell(
            """model_order = [
    'B1_persistence_last_rate',
    'B5_fixed_kalman',
    'B6_adaptive_kalman',
    'B7_two_regime_imm',
]
mae_table = aggregate.pivot(index='model_id', columns='design', values='mae').loc[model_order]
mae_table.round(3)"""
        ),
        nbformat.v4.new_code_cell(
            """screening = pd.DataFrame([
    {'criterion': name, 'passed': passed}
    for name, passed in report['screening']['checks'].items()
])
screening"""
        ),
        nbformat.v4.new_code_cell(
            """labels = {
    'B1_persistence_last_rate': 'B1 persistence',
    'B5_fixed_kalman': 'B5 fixed KF',
    'B6_adaptive_kalman': 'B6 adaptive KF',
    'B7_two_regime_imm': 'B7 IMM',
}
fig, axes = plt.subplots(2, 2, figsize=(16, 10))

overall_plot = mae_table.rename(index=labels).T
overall_plot.plot(kind='bar', ax=axes[0, 0], width=0.82)
axes[0, 0].set_title('MAE by governed design')
axes[0, 0].set_ylabel('MAE, mm/year')
axes[0, 0].set_xlabel('Validation design')
axes[0, 0].tick_params(axis='x', rotation=22)
axes[0, 0].grid(axis='y', alpha=0.25)

temporal_transition = transition.loc[
    transition['design'].eq('temporal_holdout')
    & transition['scope'].eq('mechanism')
].pivot(index='segment', columns='model_id', values='mae')[model_order].rename(columns=labels)
temporal_transition.plot(kind='bar', ax=axes[0, 1], width=0.82)
axes[0, 1].set_title('Temporal MAE by origin-only segment')
axes[0, 1].set_ylabel('MAE, mm/year')
axes[0, 1].set_xlabel('Transition proxy')
axes[0, 1].tick_params(axis='x', rotation=22)
axes[0, 1].grid(axis='y', alpha=0.25)

zone_models = ['B1_persistence_last_rate', 'B6_adaptive_kalman', 'B7_two_regime_imm']
zone = fold_metrics.loc[
    fold_metrics['design'].eq('leave_zone_out')
    & fold_metrics['model_id'].isin(zone_models)
].pivot(index='held_out_group', columns='model_id', values='mae')[zone_models].rename(columns=labels)
zone.plot(kind='bar', ax=axes[1, 0], width=0.82)
axes[1, 0].set_title('Leave-zone-out MAE by proxy zone')
axes[1, 0].set_ylabel('MAE, mm/year')
axes[1, 0].set_xlabel('Held-out proxy zone')
axes[1, 0].tick_params(axis='x', rotation=0)
axes[1, 0].grid(axis='y', alpha=0.25)

regime_temporal = regimes.loc[regimes['design'].eq('temporal_holdout')].set_index('transition_segment')
regime_temporal['transition_probability_mean'].plot(kind='bar', ax=axes[1, 1], color='#5B8FF9')
axes[1, 1].axhline(0.5, linestyle='--', color='black', linewidth=1)
axes[1, 1].set_title('Mean posterior P(transition)')
axes[1, 1].set_ylabel('Probability')
axes[1, 1].set_xlabel('Origin-only segment')
axes[1, 1].tick_params(axis='x', rotation=22)
axes[1, 1].grid(axis='y', alpha=0.25)

for ax in axes.flat[:3]:
    ax.legend(fontsize=8)
plt.tight_layout()
plt.show()"""
        ),
        nbformat.v4.new_markdown_cell("### Transition-specific evidence"),
        nbformat.v4.new_code_cell(
            """transition.loc[
    transition['design'].eq('temporal_holdout')
    & transition['scope'].isin(['stable_vs_transition', 'mechanism']),
    ['model_id', 'scope', 'segment', 'rows', 'points', 'profiles', 'mae', 'improvement_vs_b1_percent'],
].round(3)"""
        ),
        nbformat.v4.new_code_cell(
            """problem.loc[
    problem['design'].eq('temporal_holdout'),
    ['model_id', 'rows', 'points', 'profiles', 'mae', 'improvement_vs_b1_percent', 'improvement_vs_b6_percent'],
].round(3)"""
        ),
        nbformat.v4.new_markdown_cell("### Regime and leave-zone diagnostics"),
        nbformat.v4.new_code_cell(
            """regimes.loc[
    regimes['design'].eq('temporal_holdout'),
    [
        'transition_segment', 'rows', 'transition_probability_mean',
        'transition_probability_median', 'transition_probability_q90',
        'transition_probability_ge_0_5_rate', 'mae',
    ],
].round(3)"""
        ),
        nbformat.v4.new_code_cell("zone.round(3)"),
        nbformat.v4.new_markdown_cell("### Interval calibration and machine QA"),
        nbformat.v4.new_code_cell("intervals.round(3)"),
        nbformat.v4.new_code_cell(
            """assert validation_report['status'] == 'PASS'
assert validation_report['summary'] == {'checks': 64, 'failed': 0}
assert reconciliation['model_outputs_changed'] is False
assert reconciliation['authoritative_validation_failed'] == 0
assert report['test_data_loaded'] is False
assert report['test_phase_available'] is False
assert candidate['current_t1_test_used'] is False
assert candidate['eligible_for_final_claim'] is False
assert holdout_policy['status'] == 'PENDING_DATA'
{
    'independent_checks': validation_report['summary'],
    'assessment': validation_report['overall_assessment'],
    'protected_predecessors': 12,
    'comparators_refit': False,
    'model_outputs_changed_by_audit': reconciliation['model_outputs_changed'],
}"""
        ),
        nbformat.v4.new_markdown_cell(
            """## Takeaways

1. **B7 — новый сильнейший development comparator.** Он снижает общую MAE относительно B1/B5/B6 во всех четырёх governed designs; temporal улучшение против B6 равно 12.19%.
2. **Accelerating-гипотеза подтверждена.** На 17 accelerating origins B7 лучше B1 на 19.00% и B6 на 33.64%; stable и decelerating performance также улучшились.
3. **Volatile/gap-гипотеза не подтверждена.** На 14 неоднородных volatile-or-gap origins B7 хуже B1 на 22.83%. Поэтому заранее заданное объединение problem transitions улучшено против B1 только на 3.32%, а не на требуемые 10%.
4. **Spatial stability улучшилась, но строгий gap остался.** Leave-zone MAE B7 лучше B1 на 3.37% и B6 на 15.95%; всё же собственный LZO-vs-temporal degradation равен 7.93% при лимите 5%. Основной residual failure — GEO_SE.
5. **Интервальная проверка проходит.** 95% empirical coverage 0.962 находится в коридоре 0.90–0.97, но средняя ширина 47.62 мм/год остаётся большой.
6. **Governance-решение однозначно.** Screening в целом не пройден, candidate status — `validation_recorded`, current test недопустим. Параметры B7 нельзя менять по этому validation; финальная проверка требует нового future/external holdout."""
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
            "gate_b3": {
                "source": "artifacts/model_selection/t1_b3_v1",
                "schema_version": 1,
                "test_loader_called": False,
                "comparators_refit": False,
                "final_holdout_status": "PENDING_DATA",
            },
        },
    )


if __name__ == "__main__":
    raise SystemExit(main())

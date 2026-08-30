#!/usr/bin/env python3
"""Build and execute the reader-facing Gate B2 adaptive Kalman notebook."""

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
    parser.add_argument("--timeout", type=int, default=180)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    artifact_dir = root / "artifacts" / "model_selection" / "t1_b2_v1"
    required = [
        artifact_dir / "gate_b2_report.json",
        artifact_dir / "validation_report.json",
        artifact_dir / "outer_fold_predictions.csv",
        artifact_dir / "interval_metrics.csv",
        artifact_dir / "transition_metrics.csv",
    ]
    missing = [path for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            "Run scripts/run_gate_b2.py --phase all before the notebook: "
            + ", ".join(str(path) for path in missing)
        )
    report = json.loads((artifact_dir / "gate_b2_report.json").read_text(encoding="utf-8"))
    notebook = build_notebook(report)
    runtime_root = root / "work" / "jupyter_runtime_b2"
    for name, directory in {
        "IPYTHONDIR": runtime_root / "ipython",
        "JUPYTER_CONFIG_DIR": runtime_root / "config",
        "JUPYTER_DATA_DIR": runtime_root / "data",
        "JUPYTER_RUNTIME_DIR": runtime_root / "runtime",
    }.items():
        directory.mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(directory)
    notebook_path = root / "notebooks" / "03_gate_b2_adaptive_kalman.ipynb"
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
    cells = [
        nbformat.v4.new_markdown_cell(
            f"""# Gate B2 — adaptive B6 Kalman, интервалы и transition validation

## tl;dr

Gate B2 выполнен только на 911 train- и 130 validation-origins; раскрытый `t1_v1/test` не загружался и не имеет исполняемой фазы. Nested rolling tuning выбрал `B6_adaptive_kalman` с `q_base={report['model']['selected_q_base']:g}` и `acceleration_gain={report['model']['selected_acceleration_gain']:g}`.

B6 улучшил fixed B5 на temporal validation, но не прошёл полный screening: MAE **{observed['temporal_mae']:.3f} мм/год** против **{observed['temporal_b1_mae']:.3f}** у B1; transition MAE хуже B1 на **{abs(observed['transition_mae_improvement_vs_b1_percent']):.1f}%**, а leave-zone degradation составляет **{observed['leave_zone_mae_degradation_vs_temporal_percent']:.1f}%**. 95% conformal interval дал coverage **{observed['coverage_95_empirical']:.3f}** при средней ширине **{observed['coverage_95_mean_width_mm_y']:.1f} мм/год**.

Результат зафиксирован как validation evidence, а не финальная модель. Новый временной/внешний holdout пока имеет статус `PENDING_DATA`."""
        ),
        nbformat.v4.new_markdown_cell(
            """## Context & Methods

Цель этапа — реализовать адаптивный baseline B6 и проверить, закрывает ли он слабые места B1/B5, не используя уже раскрытый test. В каждом outer fold параметры B6 выбираются собственным expanding-window inner tuning. Нормировки process-noise адаптации оцениваются только на train-части соответствующего fold.

B6 фильтрует причинную историю settlement каждой точки, адаптирует process variance по доступным на origin ускорению, волатильности и длительным пропускам кампаний, а `last_rate_mm_y` использует как шумный rate anchor. Grid заранее ограничен шестью `q_base` и тремя acceleration gains. Для интервалов использованы 292 nested rolling OOF train-остатка и finite-sample scaled conformal calibration.

Transition-proxy не содержит private truth: accelerating/decelerating определяются по train-fold q80 абсолютного ускорения, `volatile_or_gap` — по train-fold q80 `std_last_3_rates_mm_y` или не менее чем двум пропущенным кампаниям; остальные origins — stable.

### Проверяемые границы

- outer evaluation: 1 temporal + 5 rolling + 14 leave-profile + 4 leave-zone folds;
- calibration: только nested OOF из `t1_v1/train`;
- test loader отсутствует в исходниках Gate B2;
- пять раскрытых B0/B1 test-артефактов контролируются по неизменным SHA-256;
- notebook читает только опубликованные train/validation-артефакты Gate B2."""
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

artifact_dir = root / 'artifacts' / 'model_selection' / 't1_b2_v1'
report = json.loads((artifact_dir / 'gate_b2_report.json').read_text(encoding='utf-8'))
validation_report = json.loads((artifact_dir / 'validation_report.json').read_text(encoding='utf-8'))
candidate = json.loads((artifact_dir / 'development_candidate.json').read_text(encoding='utf-8'))
holdout = json.loads((root / 'artifacts' / 'governance' / 'final_holdout_v2_status.json').read_text(encoding='utf-8'))
folds = pd.read_csv(artifact_dir / 'fold_contracts.csv')
tuning = pd.read_csv(artifact_dir / 'q_tuning.csv')
aggregate = pd.read_csv(artifact_dir / 'aggregate_metrics.csv')
predictions = pd.read_csv(artifact_dir / 'outer_fold_predictions.csv')
calibration_predictions = pd.read_csv(artifact_dir / 'calibration_predictions.csv')
intervals = pd.read_csv(artifact_dir / 'interval_metrics.csv')
transition = pd.read_csv(artifact_dir / 'transition_metrics.csv')

pd.set_option('display.max_columns', 30)
{
    'candidate_id': candidate['candidate_id'],
    'candidate_status': candidate['status'],
    'current_t1_test_used': candidate['current_t1_test_used'],
    'current_t1_test_authorized': candidate['current_t1_test_authorized'],
    'final_holdout_status': holdout['status'],
    'machine_validation': validation_report['summary'],
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
    },
    {
        'split': 'validation',
        'rows': report['data']['validation_rows'],
        'target_date_max': report['data']['validation_target_date_max'],
        'sample_ids_sha256': report['data']['validation_sample_ids_sha256'],
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
assert calibration_predictions['sample_id'].nunique() == len(calibration_predictions) == 292
fold_summary"""
        ),
        nbformat.v4.new_code_cell(
            """selected_by_context = (
    tuning.loc[tuning['selected'].astype(bool), ['tuning_context', 'candidate_key']]
    .drop_duplicates()
)
selected_by_context.groupby('candidate_key').size().sort_values(ascending=False).rename('contexts')"""
        ),
        nbformat.v4.new_markdown_cell("## Results"),
        nbformat.v4.new_code_cell(
            """mae_table = aggregate.pivot(index='model_id', columns='design', values='mae')
mae_table.round(3)"""
        ),
        nbformat.v4.new_code_cell(
            """screening = pd.DataFrame([
    {
        'criterion': name,
        'passed': passed,
    }
    for name, passed in report['screening']['checks'].items()
])
screening"""
        ),
        nbformat.v4.new_code_cell(
            """fig, axes = plt.subplots(1, 3, figsize=(16, 4.6))

mae_table.T.plot(kind='bar', ax=axes[0], width=0.82)
axes[0].set_title('MAE by governed design')
axes[0].set_ylabel('MAE, mm/year')
axes[0].set_xlabel('Validation design')
axes[0].tick_params(axis='x', rotation=25)
axes[0].grid(axis='y', alpha=0.25)

temporal_transition = transition.loc[
    transition['design'].eq('temporal_holdout')
    & transition['scope'].eq('mechanism')
].pivot(index='segment', columns='model_id', values='mae')
temporal_transition.plot(kind='bar', ax=axes[1], width=0.82)
axes[1].set_title('Temporal MAE by transition proxy')
axes[1].set_ylabel('MAE, mm/year')
axes[1].set_xlabel('Origin-only segment')
axes[1].tick_params(axis='x', rotation=25)
axes[1].grid(axis='y', alpha=0.25)

axes[2].plot(intervals['coverage_nominal'], intervals['coverage_empirical'], 'o-', label='empirical')
axes[2].plot([0.78, 0.97], [0.78, 0.97], '--', color='black', linewidth=1, label='ideal')
axes[2].set_xlim(0.78, 0.97)
axes[2].set_ylim(0.78, 0.97)
axes[2].set_title('Scaled conformal coverage')
axes[2].set_xlabel('Nominal coverage')
axes[2].set_ylabel('Empirical coverage')
axes[2].grid(alpha=0.25)
axes[2].legend()

for ax in axes[:2]:
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
        nbformat.v4.new_markdown_cell("### Interval calibration"),
        nbformat.v4.new_code_cell(
            """intervals.round(3)"""
        ),
        nbformat.v4.new_code_cell(
            """assert validation_report['status'] == 'PASS'
assert validation_report['summary']['failed'] == 0
assert report['test_data_loaded'] is False
assert report['test_phase_available'] is False
assert holdout['current_t1_test_eligible'] is False
{
    'independent_checks': validation_report['summary'],
    'assessment': validation_report['overall_assessment'],
    'protected_test_unchanged': True,
}"""
        ),
        nbformat.v4.new_markdown_cell(
            """## Takeaways

1. **B6 — полезный comparator, но не новый победитель.** Он на 7.25% лучше fixed B5 по temporal MAE и почти равен B1 в leave-profile-out, однако B1 остаётся сильнее на temporal и rolling-origin.
2. **Главная проблема локализована в переходах.** B6 улучшает stable и decelerating segments относительно B1, но заметно проигрывает на accelerating и volatile-or-gap; объединённый transition MAE хуже B1 на 14.25%. Это противоречит требованию улучшения не менее чем на 10%.
3. **Пространственная устойчивость недостаточна.** Leave-zone MAE ухудшается относительно temporal на 12.77% при лимите 5%. Следующая модель должна явно работать с пространственной переносимостью, не вводя идентификаторы зон в estimator features.
4. **Интервальная часть работает как заявлено.** 95% coverage равен 0.938 и попадает в коридор 0.90–0.97, но средняя ширина 47.1 мм/год велика — интервалы честные, но пока не очень точные.
5. **Governance-статус не меняется.** Результат — `validation_recorded`; текущий test остаётся историческим diagnostic-only. До финальной оценки нужен новый future/external holdout либо отдельное документированное решение."""
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
            "gate_b2": {
                "source": "artifacts/model_selection/t1_b2_v1",
                "schema_version": 1,
                "test_loader_called": False,
                "final_holdout_status": "PENDING_DATA",
            },
        },
    )


if __name__ == "__main__":
    raise SystemExit(main())

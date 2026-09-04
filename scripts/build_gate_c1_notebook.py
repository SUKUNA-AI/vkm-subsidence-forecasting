#!/usr/bin/env python
"""Build, execute, and validate the artifact-only Gate C1 notebook."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from uuid import uuid4

os.environ.setdefault("JUPYTER_ALLOW_INSECURE_WRITES", "true")

import nbformat
import pandas as pd
from nbclient import NotebookClient


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_RELATIVE = Path("artifacts/model_selection/t1_gate_c1_compact_screen_v1")
NOTEBOOK_RELATIVE = Path("notebooks/09_gate_c1_compact_sequence_screen.ipynb")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--timeout", type=int, default=300)
    return parser.parse_args()


def build_notebook(root: Path) -> nbformat.NotebookNode:
    artifact = root / ARTIFACT_RELATIVE
    aggregate = pd.read_csv(artifact / "temporal_aggregate_metrics.csv")
    screening = pd.read_csv(artifact / "screening_register.csv")
    deep = aggregate.loc[
        aggregate["model_id"].str.startswith("C0")
        & aggregate["aggregation"].eq("mean_of_fixed_seeds")
    ].sort_values(["mae", "model_id"])
    best = deep.iloc[0]
    passed = screening.loc[
        screening["status"].eq("PASSED_TEMPORAL_SCREEN"), "model_id"
    ].astype(str).tolist()
    admitted_text = ", ".join(passed) if passed else "нет"
    cells = [
        nbformat.v4.new_markdown_cell(
            f"""# Gate C1 — пятиseedовый compact sequence temporal screen

## TL;DR

Четыре заранее зафиксированные sequence-архитектуры прошли nested train-only temporal screen на 11 rolling-origin folds и пяти seeds. Лучшая deep-модель по canonical ensemble MAE — **`{best['model_id']}`**, MAE **{float(best['mae']):.3f} мм/год**. В C2 допущены: **{admitted_text}**.

Результат имеет границу `train_only_internal_research`. Исторический validation, раскрытый test и отсутствующий future/external holdout не загружались. Leave-profile, leave-zone, transition audit, conformal calibration и suite v5 в C1 не выполнялись."""
        ),
        nbformat.v4.new_markdown_cell(
            """## Метод и provenance

- 11 forward-only outer folds, последние 3 допустимых inner folds на каждый outer context;
- 56 frozen configurations, пять seeds `42117–42121` без отбора seed;
- canonical point prediction — среднее пяти фиксированных seeds;
- worker сохраняет unlabeled shards, scorer присоединяет outer labels только после hash freeze всех 44 shards;
- B1/B7/B8 импортированы как неизменяемые comparators на тех же 595 origins.

Notebook читает только сохранённые C1 CSV/JSON/PNG, ничего не обучает и не импортирует model adapters."""
        ),
        nbformat.v4.new_code_cell(
            """from pathlib import Path
import json
import pandas as pd
from IPython.display import Image, display

root = Path.cwd().resolve()
while not (root / 'pyproject.toml').is_file():
    root = root.parent
artifact = root / 'artifacts' / 'model_selection' / 't1_gate_c1_compact_screen_v1'
aggregate = pd.read_csv(artifact / 'temporal_aggregate_metrics.csv')
folds = pd.read_csv(artifact / 'temporal_fold_metrics.csv')
seeds = pd.read_csv(artifact / 'seed_stability_metrics.csv')
screen = pd.read_csv(artifact / 'screening_register.csv')
native = pd.read_csv(artifact / 'student_t_native_metrics.csv')
compute = pd.read_csv(artifact / 'compute_resource_inventory.csv')
checkpoints = pd.read_csv(artifact / 'checkpoint_inventory.csv')
incidents = json.loads((artifact / 'execution_incident_register.json').read_text(encoding='utf-8'))
validation = json.loads((artifact / 'validation_report.json').read_text(encoding='utf-8'))
admission = json.loads((artifact / 'c2_admission_manifest.json').read_text(encoding='utf-8'))
ledger = json.loads((artifact / 'outer_label_access_ledger.json').read_text(encoding='utf-8'))
figures = json.loads((artifact / 'figure_manifest.json').read_text(encoding='utf-8'))
{
    'status': validation['status'],
    'scope': validation['scientific_scope'],
    'admitted_to_c2': admission['admitted_model_ids'],
    'checks': (validation['check_count'], validation['failed_checks']),
    'scored_rows': validation['scored_prediction_rows'],
}"""
        ),
        nbformat.v4.new_markdown_cell("## 1. Temporal quality относительно B1/B7/B8"),
        nbformat.v4.new_code_cell(
            """display(Image(filename=str(artifact / 'figures' / '01_ensemble_temporal_mae.png'), width=1150))"""
        ),
        nbformat.v4.new_code_cell(
            """canonical = pd.concat([
    aggregate.loc[aggregate['model_id'].isin(['B1_persistence_last_rate', 'B7_two_regime_imm', 'B8_student_t_robust_imm'])],
    aggregate.loc[aggregate['model_id'].str.startswith('C0') & aggregate['aggregation'].eq('mean_of_fixed_seeds')],
]).sort_values(['mae', 'model_id'])
canonical[['model_id', 'mae', 'median_fold_mae', 'rmse', 'bias', 'b1_skill', 'max_fold_mae_ratio_vs_b1']].round(4)"""
        ),
        nbformat.v4.new_code_cell(
            """display(Image(filename=str(artifact / 'figures' / '02_rolling_mae_by_target_date.png'), width=1250))"""
        ),
        nbformat.v4.new_markdown_cell(
            """Покампанийная кривая нужна не для нового подбора, а для проверки того, не скрывает ли pooled MAE отдельный провальный outer fold. Admission использует только заранее замороженные temporal guards."""
        ),
        nbformat.v4.new_markdown_cell("## 2. Seed stability и вычислительная цена"),
        nbformat.v4.new_code_cell(
            """display(Image(filename=str(artifact / 'figures' / '03_seed_stability.png'), width=1150))
seeds.round(4)"""
        ),
        nbformat.v4.new_code_cell(
            """display(Image(filename=str(artifact / 'figures' / '04_mae_vs_complexity.png'), width=1250))
compute.round(3)"""
        ),
        nbformat.v4.new_markdown_cell("### Checkpoint trace и matched CUDA benchmark"),
        nbformat.v4.new_code_cell(
            """checkpoint_summary = checkpoints.groupby('role', as_index=False).agg(
    manifests=('fit_id', 'nunique'),
    retained_states=('retained_checkpoint_count', 'sum'),
    bytes=('ranked_checkpoint_bytes', 'sum'),
    resumed=('resumed_from_recovery', 'sum'),
)
assert len(checkpoints) == 3860
assert checkpoints['keep_top_k'].eq(5).all()
assert not checkpoints['outer_labels_used_for_ranking'].astype(bool).any()
checkpoint_summary"""
        ),
        nbformat.v4.new_code_cell(
            """benchmark = incidents['incidents'][-1]['matched_runtime_benchmark']
{
    'scope': benchmark['comparison_scope'],
    'fits_each_run': benchmark['fits_each_run'],
    'old_mean_seconds': round(benchmark['old_mean_fit_seconds'], 3),
    'new_mean_seconds_including_top5_io': round(benchmark['new_mean_fit_seconds_including_checkpointing'], 3),
    'mean_speedup': round(benchmark['mean_speedup_ratio'], 3),
    'median_speedup': round(benchmark['median_speedup_ratio'], 3),
}"""
        ),
        nbformat.v4.new_markdown_cell(
            """Recovery state фиксируется после каждой завершённой 50-epoch стадии и на terminal epoch. Inner fits хранят top-5 по frozen early-stopping metric и восстанавливают rank 1. Outer refits хранят последние пять эпох и всегда выбирают preregistered final epoch; outer labels никогда не участвуют в ранжировании."""
        ),
        nbformat.v4.new_markdown_cell("## 3. Native Student-t diagnostics"),
        nbformat.v4.new_code_cell(
            """native.loc[native['scope'].eq('seed_aggregate')].round(4)"""
        ),
        nbformat.v4.new_markdown_cell(
            """Native Student-t метрики в C1 публикуются отдельно по seeds. Ensemble содержит только арифметическое среднее point predictions: усреднённая псевдо-Student-t distribution не создаётся. Полная mixture/calibration процедура остаётся Gate C2."""
        ),
        nbformat.v4.new_markdown_cell("## 4. Admission и leakage boundary"),
        nbformat.v4.new_code_cell(
            """screen.set_index('model_id')[[
    'status', 'pooled_mae', 'pooled_mae_ratio_vs_b1',
    'median_fold_mae_ratio_vs_b1', 'max_fold_mae_ratio_vs_b1', 'admitted_to_c2'
]].round(4)"""
        ),
        nbformat.v4.new_code_cell(
            """assert validation['status'] == 'PASS_C1_TEMPORAL_SCREEN'
assert validation['failed_checks'] == 0
assert ledger['access_event'] == 1
assert ledger['all_shards_hash_frozen_before_access'] is True
assert ledger['worker_outer_validation_labels_loaded'] is False
assert admission['historical_validation_loaded'] is False
assert admission['current_test_loaded'] is False
assert admission['new_holdout_seen'] is False
assert admission['profile_zone_transition_audit_executed'] is False
assert admission['conformal_calibration_executed'] is False
assert admission['suite_v5_created'] is False
assert figures['model_training_calls'] == 0
{
    'outer_label_access_events': ledger['access_event'],
    'historical_validation_loaded': admission['historical_validation_loaded'],
    'current_test_loaded': admission['current_test_loaded'],
    'new_holdout_seen': admission['new_holdout_seen'],
    'profile_zone_transition_audit': admission['profile_zone_transition_audit_executed'],
    'suite_v5_created': admission['suite_v5_created'],
}"""
        ),
        nbformat.v4.new_markdown_cell(
            """## Ограничения и следующий этап

Gate C1 отвечает только на вопрос о temporal admission. Он не доказывает пространственную переносимость, устойчивость transition/gap режимов, калибровку общих conformal-интервалов или внешнюю обобщаемость. Эти проверки остаются `PENDING` в Gate C2; единственная финальная проверка должна выполняться один раз на новом real future/external holdout после заморозки suite v5 или fallback B7."""
        ),
    ]
    return nbformat.v4.new_notebook(
        cells=cells,
        metadata={
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python", "version": "3.13"},
            "gate": "C1_COMPACT_SEQUENCE_TEMPORAL_SCREEN",
            "scientific_scope": "train_only_internal_research",
            "artifact_only": True,
            "model_training_calls": 0,
        },
    )


def validate_notebook(notebook: nbformat.NotebookNode) -> dict[str, int | str]:
    code_cells = [cell for cell in notebook.cells if cell.cell_type == "code"]
    if not code_cells or any(cell.execution_count is None for cell in code_cells):
        raise RuntimeError("Gate C1 notebook contains an unexecuted code cell")
    errors = [
        output
        for cell in code_cells
        for output in cell.get("outputs", [])
        if output.get("output_type") == "error"
    ]
    if errors:
        raise RuntimeError(f"Gate C1 notebook contains execution errors: {errors}")
    rich_outputs = sum(
        output.get("output_type") in {"display_data", "execute_result"}
        for cell in code_cells
        for output in cell.get("outputs", [])
    )
    if rich_outputs < 8:
        raise RuntimeError("Gate C1 notebook has too few inspectable outputs")
    source = "\n".join(cell.source.lower() for cell in code_cells)
    forbidden = (
        "import torch",
        ".fit(",
        "run_gate_c1",
        "gate_c1_models",
        "gate_c1_worker",
        "load_split_dataset",
        "t1_v1/validation",
        "t1_v1/test",
    )
    hits = [token for token in forbidden if token in source]
    if hits:
        raise RuntimeError(f"Artifact-only Gate C1 notebook contains prohibited calls: {hits}")
    return {
        "status": "PASS",
        "executed_code_cells": len(code_cells),
        "rich_outputs": rich_outputs,
        "error_outputs": 0,
        "model_training_calls": 0,
    }


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    artifact = root / ARTIFACT_RELATIVE
    required = (
        artifact / "validation_report.json",
        artifact / "figure_manifest.json",
        artifact / "temporal_aggregate_metrics.csv",
    )
    missing = [path.relative_to(root).as_posix() for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Build and validate Gate C1 artifacts first: {missing}")
    runtime = root / "work" / "jupyter_runtime_gate_c1"
    for name, path in {
        "IPYTHONDIR": runtime / "ipython",
        "JUPYTER_CONFIG_DIR": runtime / "config",
        "JUPYTER_DATA_DIR": runtime / "data",
        "JUPYTER_RUNTIME_DIR": runtime / "runtime",
        "MPLCONFIGDIR": runtime / "matplotlib",
    }.items():
        path.mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(path)
    notebook = build_notebook(root)
    executed = NotebookClient(
        notebook,
        timeout=args.timeout,
        kernel_name="python3",
        resources={"metadata": {"path": str(root)}},
        allow_errors=False,
    ).execute(cwd=str(root))
    report = validate_notebook(executed)
    output = root / NOTEBOOK_RELATIVE
    output.parent.mkdir(parents=True, exist_ok=True)
    work_output = runtime / f"{output.stem}.{uuid4().hex}.ipynb"
    nbformat.write(executed, work_output)
    work_output.replace(output)
    report.update(
        {
            "gate": "C1_COMPACT_SEQUENCE_TEMPORAL_SCREEN",
            "scientific_scope": "train_only_internal_research",
            "notebook": output.relative_to(root).as_posix(),
            "artifact_only": True,
        }
    )
    report_path = artifact / "notebook_execution_report.json"
    write_text_atomic(
        root,
        report_path,
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def write_text_atomic(root: Path, path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    work = root / "work" / "gate_c1_reporting"
    work.mkdir(parents=True, exist_ok=True)
    temporary = work / f"{path.name}.{uuid4().hex}.tmp"
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())

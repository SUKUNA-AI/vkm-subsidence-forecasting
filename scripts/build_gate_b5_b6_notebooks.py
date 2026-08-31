#!/usr/bin/env python
"""Build, execute, and structurally validate the artifact-only Gate B5/B6 notebooks."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

os.environ.setdefault("JUPYTER_ALLOW_INSECURE_WRITES", "true")

import nbformat
from nbclient import NotebookClient


ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--gate", choices=("b5", "b6", "all"), default="all")
    parser.add_argument("--timeout", type=int, default=300)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    runtime = root / "work" / "jupyter_runtime_b5_b6"
    for name, path in {
        "IPYTHONDIR": runtime / "ipython",
        "JUPYTER_CONFIG_DIR": runtime / "config",
        "JUPYTER_DATA_DIR": runtime / "data",
        "JUPYTER_RUNTIME_DIR": runtime / "runtime",
        "MPLCONFIGDIR": runtime / "matplotlib",
    }.items():
        path.mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(path)
    builders = {
        "b5": (build_b5_notebook, root / "notebooks" / "06_gate_b5_evidence_audit.ipynb"),
        "b6": (build_b6_notebook, root / "notebooks" / "07_gate_b6_model_comparison.ipynb"),
    }
    selected = list(builders) if args.gate == "all" else [args.gate]
    results = []
    for gate in selected:
        builder, output = builders[gate]
        notebook = builder(root)
        executed = NotebookClient(
            notebook,
            timeout=args.timeout,
            kernel_name="python3",
            resources={"metadata": {"path": str(root)}},
            allow_errors=False,
        ).execute(cwd=str(root))
        validation = validate_executed_notebook(executed)
        output.parent.mkdir(parents=True, exist_ok=True)
        nbformat.write(executed, output)
        results.append({"gate": gate.upper(), "path": output.relative_to(root).as_posix(), **validation})
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


def validate_executed_notebook(notebook: nbformat.NotebookNode) -> dict[str, int | str]:
    code_cells = [cell for cell in notebook.cells if cell.cell_type == "code"]
    if not code_cells or any(cell.execution_count is None for cell in code_cells):
        raise RuntimeError("Notebook contains an unexecuted code cell")
    errors = [
        output
        for cell in code_cells
        for output in cell.get("outputs", [])
        if output.get("output_type") == "error"
    ]
    if errors:
        raise RuntimeError(f"Notebook contains execution errors: {errors}")
    rich_outputs = sum(
        output.get("output_type") in {"display_data", "execute_result"}
        for cell in code_cells
        for output in cell.get("outputs", [])
    )
    if rich_outputs < 3:
        raise RuntimeError("Notebook has too few inspectable outputs")
    source = "\n".join(cell.source for cell in code_cells)
    forbidden = ("fit(", "run_gate_b6", "load_split_dataset", "t1_v1/validation", "t1_v1/test")
    if any(token in source for token in forbidden):
        raise RuntimeError("Artifact-only notebook contains a model-facing or prohibited input call")
    return {
        "status": "PASS",
        "executed_code_cells": len(code_cells),
        "rich_outputs": rich_outputs,
        "model_training_calls": 0,
    }


def build_b5_notebook(root: Path) -> nbformat.NotebookNode:
    artifact = root / "artifacts" / "model_selection" / "t1_b5_evidence_v1"
    report_path = artifact / "gate_b5_report.json"
    if not report_path.is_file():
        raise FileNotFoundError("Run Gate B5 before building its notebook")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    cells = [
        nbformat.v4.new_markdown_cell(
            f"""# Gate B5 — доказательный аудит и benchmark protocol

Gate B5 заморозил train-only benchmark поверх **{report['data']['rows']} origins**, {report['data']['points']} points, {report['data']['profiles']} profiles и {report['data']['zones']} зон. В плане ровно **{report['benchmark']['outer_folds']} outer folds**: 11 rolling-origin, 42 spatio-temporal leave-profile-out и 12 leave-zone-out; к ним относятся 195 forward-only inner folds.

Это протокол `train_only_internal_research`. Исторический validation, раскрытый T1 test и новый holdout не загружались. Notebook читает только сохранённые machine artifacts и ничего не переобучает."""
        ),
        nbformat.v4.new_code_cell(
            """from pathlib import Path
import json
import pandas as pd
import matplotlib.pyplot as plt

root = Path.cwd().resolve()
while not (root / 'pyproject.toml').is_file():
    root = root.parent
artifact = root / 'artifacts' / 'model_selection' / 't1_b5_evidence_v1'
split_root = root / 'artifacts' / 'splits' / 't1_train_benchmark_v1'
report = json.loads((artifact / 'gate_b5_report.json').read_text(encoding='utf-8'))
validation = json.loads((artifact / 'validation_report.json').read_text(encoding='utf-8'))
contracts = pd.read_csv(split_root / 'fold_contracts.csv')
atlas = pd.read_csv(artifact / 'error_atlas.csv')
dependence = pd.read_csv(artifact / 'residual_dependence.csv')
units = pd.read_csv(artifact / 'independent_units.csv')
learning = pd.read_csv(artifact / 'learning_curves.csv')
methods = json.loads((artifact / 'method_cards.json').read_text(encoding='utf-8'))
{'gate_status': report['status'], 'validation': validation['summary'], 'plan_sha256': report['benchmark']['plan_sha256']}"""
        ),
        nbformat.v4.new_markdown_cell("## Замороженная геометрия folds"),
        nbformat.v4.new_code_cell(
            """outer = contracts.loc[contracts['level'].eq('outer')]
inner = contracts.loc[contracts['level'].eq('inner')]
summary = outer.groupby('design').agg(
    folds=('fold_id', 'nunique'),
    train_rows_min=('train_rows', 'min'),
    train_rows_max=('train_rows', 'max'),
    validation_rows=('validation_rows', 'sum'),
)
assert len(outer) == 65 and len(inner) == 195
assert (pd.to_datetime(contracts['train_target_date_max']) < pd.to_datetime(contracts['validation_target_date_min'])).all()
summary"""
        ),
        nbformat.v4.new_markdown_cell("## Реальные независимые единицы и residual dependence"),
        nbformat.v4.new_code_cell(
            """units.groupby('design').agg(
    origins=('origins', 'max'),
    temporal_units=('temporal_units', 'max'),
    profiles=('profile_units', 'max'),
    zones=('zone_units', 'max'),
    point_trajectories=('point_trajectories', 'max'),
)"""
        ),
        nbformat.v4.new_code_cell(
            """dependence.loc[
    dependence['dependence_measure'].eq('within_profile_icc1'),
    ['design', 'model_id', 'groups', 'rows', 'residual_correlation'],
].head(20)"""
        ),
        nbformat.v4.new_markdown_cell("## Error atlas и low-support граница"),
        nbformat.v4.new_code_cell(
            """atlas.groupby(['support_status', 'dimension'], dropna=False).agg(
    segments=('segment', 'nunique'), origins=('origins', 'sum')
).sort_index()"""
        ),
        nbformat.v4.new_markdown_cell("## Learning curves замороженных comparators"),
        nbformat.v4.new_code_cell(
            """pivot = learning.pivot(index='training_rows', columns='model_id', values='mae')
ax = pivot.plot(marker='o', figsize=(12, 6))
ax.set(title='Audit-tail MAE без перенастройки гиперпараметров', xlabel='Train rows', ylabel='MAE, mm/year')
ax.grid(alpha=.25)
ax.legend(loc='upper left', bbox_to_anchor=(1.01, 1.0), borderaxespad=0)
plt.tight_layout()
plt.show()
pivot.round(3)"""
        ),
        nbformat.v4.new_markdown_cell("## Формальные исключения"),
        nbformat.v4.new_code_cell(
            """pd.DataFrame(methods['methods'])[['method_id', 'status', 'reason']]"""
        ),
        nbformat.v4.new_code_cell(
            """assert report['historical_validation_loaded'] is False
assert report['current_t1_test_loaded'] is False
assert report['new_holdout_seen'] is False
assert report['protected_predecessors_match'] is True
assert validation['status'] == 'PASS' and validation['summary']['failed'] == 0
{'scope': report['scientific_scope'], 'claims_allowed': report['claim_boundary'], 'qa': validation['summary']}"""
        ),
    ]
    return _notebook(cells, "B5", "artifacts/model_selection/t1_b5_evidence_v1")


def build_b6_notebook(root: Path) -> nbformat.NotebookNode:
    artifact = root / "artifacts" / "model_selection" / "t1_b6_expanded_v1"
    report_path = artifact / "gate_b6_report.json"
    if not report_path.is_file():
        raise FileNotFoundError("Run and freeze Gate B6 before building its notebook")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    analytics = json.loads((artifact / "analytics_summary.json").read_text(encoding="utf-8"))
    b7_mae = float(analytics["b7_rolling_mae_mm_per_year"])
    b1_mae = float(analytics["b1_rolling_mae_mm_per_year"])
    b7_skill_pct = 100.0 * float(analytics["b7_skill_vs_b1"])
    cells = [
        nbformat.v4.new_markdown_cell(
            f"""# Gate B6 — расширенный train-only benchmark T1

## TL;DR

Статус Gate B6: **`{report['status']}`**. Новый primary не выбран: **`{report['primary_model_id']}`** остаётся frozen primary suite v4. Его pooled rolling-origin MAE составляет **{b7_mae:.3f} мм/год** против **{b1_mae:.3f} мм/год** у B1, то есть train-only skill равен **{b7_skill_pct:.1f}%**.

Из 23 исторически замороженных записей каталога исполнены 22; 11 прошли broad temporal screen. TabPFN исключён governance-поправкой **до загрузки весов, лицензии, predictions и scoring**. Это `train_only_internal_research`: исторический validation, раскрытый T1 test и новый holdout не загружались, поэтому утверждение о внешнем качестве запрещено.

Notebook читает только сохранённые machine artifacts и готовые PNG; обучение и скрытая перенастройка отсутствуют."""
        ),
        nbformat.v4.new_markdown_cell(
            """## Контекст и метод

- 11 forward-only rolling-origin outer folds — broad screen;
- 42 leave-profile-out и 12 leave-zone-out folds — spatial audit;
- transition, horizon, missingness и history diagnostics;
- conformal calibration только по inner rolling OOF residuals;
- learning curves на audit tail с неизменными гиперпараметрами;
- suite v4 выбирается по preregistered gates, а не по одному weighted score."""
        ),
        nbformat.v4.new_code_cell(
            """from pathlib import Path
import json
import pandas as pd
from IPython.display import Image, display

root = Path.cwd().resolve()
while not (root / 'pyproject.toml').is_file():
    root = root.parent
artifact = root / 'artifacts' / 'model_selection' / 't1_b6_expanded_v1'
figure_root = artifact / 'figures'
report = json.loads((artifact / 'gate_b6_report.json').read_text(encoding='utf-8'))
validation = json.loads((artifact / 'validation_report.json').read_text(encoding='utf-8'))
suite = json.loads((root / 'artifacts' / 'governance' / 'final_candidate_suite_v4.json').read_text(encoding='utf-8'))
environment = json.loads((artifact / 'environment_manifest.json').read_text(encoding='utf-8'))
external = json.loads((artifact / 'external_model_manifest.json').read_text(encoding='utf-8'))
analytics = json.loads((artifact / 'analytics_summary.json').read_text(encoding='utf-8'))
chart_map = json.loads((artifact / 'chart_map.json').read_text(encoding='utf-8'))
screen = pd.read_csv(artifact / 'screening_register.csv')
aggregate = pd.read_csv(artifact / 'temporal_aggregate_metrics.csv')
folds = pd.read_csv(artifact / 'temporal_fold_metrics.csv')
groups = pd.read_csv(artifact / 'group_metrics.csv')
transition = pd.read_csv(artifact / 'transition_metrics.csv')
probabilistic = pd.read_csv(artifact / 'probabilistic_metrics.csv')
learning = pd.read_csv(artifact / 'learning_curves.csv')
{
    'status': report['status'],
    'scope': report['scientific_scope'],
    'primary': suite['primary_model_id'],
    'registry/executed/advanced': (report['registry_models'], report['models_screened'], report['models_advanced']),
    'validation': validation['summary'],
}"""
        ),
        nbformat.v4.new_markdown_cell("## 1. Broad temporal screen"),
        nbformat.v4.new_code_cell(
            """display(Image(filename=str(figure_root / '01_temporal_screen_mae.png'), width=1200))"""
        ),
        nbformat.v4.new_code_cell(
            """screen_summary = screen.groupby(['screen_status', 'registry_status']).size().rename('models').to_frame()
top = aggregate.sort_values(['mae', 'model_id']).head(12)
display(screen_summary)
top[['model_id', 'mae', 'median_fold_mae', 'b1_skill', 'worst_zone_mae']].round(3)"""
        ),
        nbformat.v4.new_code_cell(
            """display(Image(filename=str(figure_root / '02_rolling_mae_by_date.png'), width=1200))"""
        ),
        nbformat.v4.new_markdown_cell(
            """**Интерпретация.** B7 имеет лучший pooled MAE, но ошибка заметно меняется между датами. Поэтому решение не опирается только на среднее: обязательны sign consistency, spatial holdouts и transition guardrails."""
        ),
        nbformat.v4.new_markdown_cell("## 2. Пространственная устойчивость и transition evidence"),
        nbformat.v4.new_code_cell(
            """display(Image(filename=str(figure_root / '03_spatial_stability.png'), width=1200))
display(Image(filename=str(figure_root / '04_transition_error_heatmap.png'), width=1100))"""
        ),
        nbformat.v4.new_code_cell(
            """macro = groups.loc[
    groups['scope'].isin(['equal_profile_macro', 'equal_zone_macro']),
    ['design', 'model_id', 'scope', 'mae'],
]
macro.pivot_table(index='model_id', columns='scope', values='mae').sort_values('equal_zone_macro').head(15).round(3)"""
        ),
        nbformat.v4.new_code_cell(
            """transition.loc[
    transition['design'].eq('rolling_origin')
    & transition['dimension'].isin(['pooled_transition', 'transition']),
    ['model_id', 'dimension', 'segment', 'rows', 'support_status', 'mae'],
].sort_values(['dimension', 'segment', 'mae']).head(40).round(3)"""
        ),
        nbformat.v4.new_markdown_cell(
            """**Интерпретация.** B7 сохраняет сильный профильный и зональный результат и лучше B1 на accelerating origins, но не улучшает B1 на volatile-or-gap. Именно поэтому volatile-or-gap остаётся жёстким guardrail, а четыре зоны трактуются описательно, без псевдо-точных confidence intervals."""
        ),
        nbformat.v4.new_markdown_cell("## 3. Калибровка и probabilistic output"),
        nbformat.v4.new_code_cell(
            """display(Image(filename=str(figure_root / '05_conformal_calibration.png'), width=900))"""
        ),
        nbformat.v4.new_code_cell(
            """overall_prob = probabilistic.loc[
    probabilistic['design'].eq('rolling_origin')
    & probabilistic['dimension'].eq('overall'),
    ['model_id', 'interval_source', 'coverage_50', 'coverage_80', 'coverage_95', 'weighted_interval_score', 'crps', 'nll'],
]
overall_prob.sort_values(['interval_source', 'weighted_interval_score']).round(4)"""
        ),
        nbformat.v4.new_markdown_cell(
            """Conformal intervals публикуются отдельно от native intervals. Калибровочные residuals получены только из inner OOF predictions; outer labels используются лишь для честной оценки покрытия."""
        ),
        nbformat.v4.new_markdown_cell("## 4. Learning curves и paired sensitivity"),
        nbformat.v4.new_code_cell(
            """display(Image(filename=str(figure_root / '06_learning_curves.png'), width=1100))
display(Image(filename=str(figure_root / '07_profile_cluster_sensitivity.png'), width=1100))"""
        ),
        nbformat.v4.new_code_cell(
            """learning.pivot(index='training_rows', columns='model_id', values='mae').round(3)"""
        ),
        nbformat.v4.new_markdown_cell(
            """Learning curves являются диагностикой объёма данных: параметры заморожены и на кривых не перенастраиваются. Cluster-resampling intervals — sensitivity evidence при малом числе кластеров, а не i.i.d. inferential confidence intervals."""
        ),
        nbformat.v4.new_markdown_cell("## 5. Governance, исключения и frozen suite v4"),
        nbformat.v4.new_code_cell(
            """pd.DataFrame(suite['models'])[['model_id', 'role', 'model_spec_sha256']]"""
        ),
        nbformat.v4.new_code_cell(
            """pd.DataFrame([{
    'model_id': external['model_id'],
    'status': external['status'],
    'weights_downloaded': external['weights_downloaded'],
    'license_marker_present': external['license_marker_present'],
    'execution_allowed': external['execution_allowed'],
    'scientific_status': external['scientific_status'],
}])"""
        ),
        nbformat.v4.new_code_cell(
            """assert report['historical_validation_loaded'] is False
assert report['current_t1_test_loaded'] is False
assert report['new_holdout_seen'] is False
assert suite['primary_count'] == 1
assert suite['primary_selected_from_holdout'] is False
assert validation['status'] == 'PASS' and validation['summary']['failed'] == 0
assert all(item['status'] == 'READY' for item in environment['environments'].values())
assert external['status'] == 'EXCLUDED_GOVERNANCE_USER_WITHDRAWAL'
assert external['weights_downloaded'] is False
assert external['license_marker_present'] is False
assert external['execution_allowed'] is False
assert analytics['model_training_calls'] == 0
assert chart_map['model_training_calls'] == 0
assert len(chart_map['charts']) == 7
{
    'scope': report['scientific_scope'],
    'primary': suite['primary_model_id'],
    'qa': validation['summary'],
    'tabpfn': external['status'],
    'external_quality_claim_allowed': report['final_quality_claim_allowed'],
}"""
        ),
        nbformat.v4.new_markdown_cell(
            """## Вывод и следующий шаг

Gate B6 завершён корректным статусом `PASS_NO_NEW_PRIMARY`: ни одна новая модель не прошла все заранее зафиксированные eligibility gates, поэтому B7 остаётся primary. Это не отрицательный software result, а ожидаемый научный исход строгого screening.

Следующий выбор модели разрешён только после появления заранее замороженного future/external holdout. До этого можно улучшать только доказательную базу и проводить новые nested train-only исследования с новой preregistration; текущие validation/test не могут стать источником настройки."""
        ),
    ]
    return _notebook(cells, "B6", "artifacts/model_selection/t1_b6_expanded_v1")


def _notebook(cells, gate: str, artifact_root: str) -> nbformat.NotebookNode:
    return nbformat.v4.new_notebook(
        cells=cells,
        metadata={
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python", "version": "3.13"},
            "artifact_only_audit": {
                "gate": gate,
                "artifact_root": artifact_root,
                "source_split": "t1_v1/train",
                "model_training": False,
                "historical_validation_loaded": False,
                "current_test_loaded": False,
            },
        },
    )


if __name__ == "__main__":
    raise SystemExit(main())

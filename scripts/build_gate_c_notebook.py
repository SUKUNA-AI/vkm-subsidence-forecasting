#!/usr/bin/env python
"""Build and execute the inspectable Gate C0 sequence-audit notebook."""

from __future__ import annotations

import json
import os
from pathlib import Path
import textwrap

ROOT = Path(__file__).resolve().parents[1]
JUPYTER_WORK_ROOT = ROOT / "work" / "gate_c0" / "jupyter"
JUPYTER_WORK_ROOT.mkdir(parents=True, exist_ok=True)
MATPLOTLIB_WORK_ROOT = JUPYTER_WORK_ROOT / "matplotlib"
MATPLOTLIB_WORK_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("IPYTHONDIR", str(JUPYTER_WORK_ROOT / "ipython"))
os.environ.setdefault("JUPYTER_CONFIG_DIR", str(JUPYTER_WORK_ROOT / "config"))
os.environ.setdefault("JUPYTER_DATA_DIR", str(JUPYTER_WORK_ROOT / "data"))
os.environ.setdefault("JUPYTER_RUNTIME_DIR", str(JUPYTER_WORK_ROOT / "runtime"))
os.environ.setdefault("JUPYTER_ALLOW_INSECURE_WRITES", "1")
os.environ.setdefault("MPLCONFIGDIR", str(MATPLOTLIB_WORK_ROOT))

import nbformat as nbf
from nbclient import NotebookClient


NOTEBOOK_PATH = ROOT / "notebooks" / "08_gate_c_sequence_audit.ipynb"
REPORT_PATH = (
    ROOT
    / "artifacts"
    / "model_selection"
    / "t1_gate_c0_sequence_audit_v1"
    / "notebook_execution_report.json"
)


def markdown(text: str):
    return nbf.v4.new_markdown_cell(textwrap.dedent(text).strip())


def code(text: str):
    return nbf.v4.new_code_cell(textwrap.dedent(text).strip())


def build_notebook():
    notebook = nbf.v4.new_notebook()
    notebook["metadata"] = {
        "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
        "language_info": {"name": "python", "version": "3.13"},
        "skru1": {
            "gate": "C0_SEQUENCE_PROTOCOL_FREEZE",
            "data_boundary": "t1_v1/train_only",
            "model_training_allowed": False,
        },
    }
    notebook["cells"] = [
        markdown(
            """
            # Gate C0 — аудит последовательностей T1

            **TL;DR.** Notebook читает только сохранённые machine artifacts Gate C0, ничего не
            обучает и не обращается к `t1_v1/validation`, раскрытому `t1_v1/test` или будущему
            holdout. Проверяются геометрия 911 forecast origins, causal masks, gaps и
            пригодность компактных sequence-архитектур.
            """
        ),
        code(
            """
            from pathlib import Path
            import json
            import pandas as pd
            import numpy as np
            import matplotlib.pyplot as plt
            from IPython.display import display

            ROOT = Path.cwd()
            ARTIFACT_ROOT = ROOT / "artifacts" / "model_selection" / "t1_gate_c0_sequence_audit_v1"
            SPLIT_ROOT = ROOT / "artifacts" / "splits" / "t1_train_gate_c_v1"
            FIGURE_ROOT = ARTIFACT_ROOT / "figures"
            FIGURE_ROOT.mkdir(parents=True, exist_ok=True)

            report = json.loads((ARTIFACT_ROOT / "gate_c0_report.json").read_text(encoding="utf-8"))
            validation = json.loads((ARTIFACT_ROOT / "validation_report.json").read_text(encoding="utf-8"))
            lengths = pd.read_csv(ARTIFACT_ROOT / "sequence_length_distribution.csv")
            gaps = pd.read_csv(ARTIFACT_ROOT / "sequence_gap_summary.csv")
            architecture = pd.read_csv(ARTIFACT_ROOT / "architecture_eligibility.csv")
            folds = pd.read_csv(ARTIFACT_ROOT / "fold_summary.csv")
            sequence_manifest = pd.read_csv(SPLIT_ROOT / "sequence_manifest.csv")

            assert report["status"] == "PASS_PROTOCOL_FROZEN"
            assert validation["failed_checks"] == 0
            assert report["model_training_calls"] == 0
            assert report["historical_validation_loaded"] is False
            assert report["current_t1_test_loaded"] is False
            assert len(sequence_manifest) == 911

            plt.rcParams.update({
                "figure.facecolor": "white",
                "axes.facecolor": "white",
                "font.family": "DejaVu Sans",
                "font.size": 10,
                "axes.titlesize": 12,
                "axes.labelsize": 10,
                "axes.spines.top": False,
                "axes.spines.right": False,
            })

            print("Источники: machine artifacts Gate C0; model training calls =", report["model_training_calls"])
            """
        ),
        markdown(
            """
            ## Контекст и метод

            Sequence representation строится по одной точке и заканчивается на `current_date`.
            Observation после origin и target observation запрещены. До длины 16 используется
            left padding; идентификаторы сохранены только как metadata и не входят в сеть.
            Fold provenance наследует неизменяемые 11 rolling, 42 leave-profile-out и 12
            leave-zone-out outer folds Gate B5; у каждого outer fold остаются три inner folds.
            """
        ),
        code(
            """
            key_facts = pd.DataFrame([
                ("Forecast origins", report["data"]["origins"]),
                ("Траектории точек", report["data"]["points"]),
                ("Профили", report["data"]["profiles"]),
                ("Зоны", report["data"]["zones"]),
                ("Длина истории, min/median/max", f'{report["data"]["history_length_min"]} / {report["data"]["history_length_median"]:.0f} / {report["data"]["history_length_max"]}'),
                ("Положительный delta_t, min/median/max, дней", f'{report["data"]["delta_t_days_min_positive"]} / {report["data"]["delta_t_days_median_positive"]:.0f} / {report["data"]["delta_t_days_max"]}'),
                ("Future observations во входах", report["sequence_contract"]["future_observations_in_inputs"]),
                ("Target observations во входах", report["sequence_contract"]["target_observations_in_inputs"]),
            ], columns=["Показатель", "Значение"])
            display(key_facts)
            """
        ),
        markdown("## Данные: длина истории и padding"),
        code(
            """
            fig, ax = plt.subplots(figsize=(9.0, 4.8))
            colors = ["#315A7D" if value <= 10 else "#D58A3A" for value in lengths["history_length"]]
            ax.bar(lengths["history_length"], lengths["origins"], color=colors, width=0.78)
            ax.axvline(report["data"]["history_length_median"], color="#8C2F39", linestyle="--", linewidth=1.5, label="медиана = 7")
            ax.set_title("Распределение фактической длины истории")
            ax.set_xlabel("Число наблюдений до forecast origin включительно")
            ax.set_ylabel("Число origins")
            ax.set_xticks(lengths["history_length"])
            ax.grid(axis="y", alpha=0.2)
            ax.legend(frameon=False)
            fig.tight_layout()
            fig.savefig(FIGURE_ROOT / "01_sequence_length_distribution.png", dpi=220, bbox_inches="tight")
            plt.show()
            """
        ),
        markdown(
            """
            История существенно короче типичных контекстов современных transformer-моделей:
            более половины origins имеют не более семи наблюдений, а длина 15–16 встречается
            только у семи origins. Поэтому compact recurrent/causal convolution models являются
            обязательным первым screen, а patch-based architectures исключены заранее.
            """
        ),
        markdown("## Нерегулярность календаря и пропущенные кампании"),
        code(
            """
            gap_days = gaps[gaps["dimension"].eq("gap_days")].copy()
            gap_order = ["origin_token", "1_90", "91_150", "151_210", "gt_210"]
            gap_days["segment"] = pd.Categorical(gap_days["segment"], gap_order, ordered=True)
            gap_days = gap_days.sort_values("segment")
            missing = gaps[gaps["dimension"].eq("missing_campaigns")].copy()
            missing["segment"] = pd.Categorical(missing["segment"], ["0", "1", "ge_2"], ordered=True)
            missing = missing.sort_values("segment")

            fig, axes = plt.subplots(1, 2, figsize=(11.0, 4.5))
            axes[0].bar(gap_days["segment"].astype(str), gap_days["observation_tokens"], color="#315A7D")
            axes[0].set_title("Интервалы между наблюдениями")
            axes[0].set_xlabel("delta_t, дней")
            axes[0].set_ylabel("Observation tokens")
            axes[0].tick_params(axis="x", rotation=25)
            axes[0].grid(axis="y", alpha=0.2)

            axes[1].bar(missing["segment"].astype(str), missing["observation_tokens"], color="#D58A3A")
            axes[1].set_title("Пропущенные кампании между наблюдениями")
            axes[1].set_xlabel("Число пропущенных кампаний")
            axes[1].set_ylabel("Observation tokens")
            axes[1].grid(axis="y", alpha=0.2)
            fig.tight_layout()
            fig.savefig(FIGURE_ROOT / "02_gap_and_missingness_geometry.png", dpi=220, bbox_inches="tight")
            plt.show()
            """
        ),
        markdown(
            """
            Медианный положительный интервал равен 168 дням, диапазон — 42–560 дней. У 243
            origins в истории есть интервал с двумя и более пропущенными кампаниями. Это делает
            `delta_t`, uncertainty и missingness masks обязательными входами; интерполяция на
            равномерную сетку не вводится без отдельного доказательного этапа.
            """
        ),
        markdown("## Architecture pre-screen"),
        code(
            """
            status_order = ["REQUIRED_COMPACT_SCREEN", "CONDITIONAL_COMPACT_SCREEN", "NOT_ELIGIBLE_DATA_GEOMETRY"]
            status_color = {
                "REQUIRED_COMPACT_SCREEN": "#2E6F57",
                "CONDITIONAL_COMPACT_SCREEN": "#D58A3A",
                "NOT_ELIGIBLE_DATA_GEOMETRY": "#9A9A9A",
            }
            plot_arch = architecture.copy()
            plot_arch["status_rank"] = plot_arch["status"].map({value: index for index, value in enumerate(status_order)})
            plot_arch = plot_arch.sort_values(["status_rank", "model_id"], ascending=[False, True])

            fig, ax = plt.subplots(figsize=(10.0, 5.6))
            ax.barh(plot_arch["model_id"], np.ones(len(plot_arch)), color=plot_arch["status"].map(status_color))
            ax.set_xlim(0, 1.02)
            ax.set_xticks([])
            ax.set_title("Предварительная пригодность sequence-архитектур")
            for index, row in enumerate(plot_arch.itertuples(index=False)):
                ax.text(0.02, index, row.status, va="center", ha="left", color="white", fontsize=8.5, fontweight="bold")
            fig.tight_layout()
            fig.savefig(FIGURE_ROOT / "03_architecture_eligibility.png", dpi=220, bbox_inches="tight")
            plt.show()

            display(architecture[["model_id", "family", "status", "condition", "reason"]])
            """
        ),
        markdown("## Fold geometry и leakage controls"),
        code(
            """
            outer = folds[folds["level"].eq("outer")].copy()
            label_map = {
                "rolling_origin": "Rolling origin",
                "spatiotemporal_leave_profile_out": "Leave profile out",
                "spatiotemporal_leave_zone_out": "Leave zone out",
            }
            outer["label"] = outer["design"].map(label_map)
            fig, ax = plt.subplots(figsize=(8.8, 4.5))
            bars = ax.bar(outer["label"], outer["folds"], color=["#315A7D", "#2E6F57", "#D58A3A"])
            ax.set_title("Замороженные outer folds Gate C")
            ax.set_ylabel("Число folds")
            ax.grid(axis="y", alpha=0.2)
            for bar in bars:
                ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.7, f"{int(bar.get_height())}", ha="center")
            fig.tight_layout()
            fig.savefig(FIGURE_ROOT / "04_fold_design.png", dpi=220, bbox_inches="tight")
            plt.show()

            display(folds)
            assert folds["all_forward_only"].all()
            assert folds["all_held_groups_excluded"].all()
            """
        ),
        markdown(
            """
            ## Выводы

            1. Представление `t1_train_gate_c_v1` пригодно для компактного GRU/LSTM/TCN и
               probabilistic recurrent screen, но не обосновывает тяжёлые patch-transformers.
            2. Нерегулярность 42–560 дней и missing-campaign gaps требуют явных time/gap masks.
            3. Все preprocessing и early stopping operations должны оставаться внутри fold train /
               nested inner validation; старые validation/test недоступны runner.
            4. Gate C0 не даёт нового результата качества. Следующий этап — C1 compact screen с
               пятью seeds и B1/B7/B8 как неизменяемыми comparators.
            5. Без нового future/external holdout окончательная научная оценка остаётся
               `PENDING_DATA`.
            """
        ),
    ]
    return notebook


def main() -> int:
    NOTEBOOK_PATH.parent.mkdir(parents=True, exist_ok=True)
    notebook = build_notebook()
    client = NotebookClient(
        notebook,
        timeout=180,
        kernel_name="python3",
        resources={"metadata": {"path": str(ROOT)}},
        allow_errors=False,
    )
    executed = client.execute()
    nbf.write(executed, NOTEBOOK_PATH)
    executed_code_cells = [cell for cell in executed.cells if cell.cell_type == "code"]
    report = {
        "schema_version": 1,
        "notebook": NOTEBOOK_PATH.relative_to(ROOT).as_posix(),
        "status": "PASS_EXECUTED_TOP_TO_BOTTOM",
        "code_cells": len(executed_code_cells),
        "executed_code_cells": sum(cell.get("execution_count") is not None for cell in executed_code_cells),
        "error_outputs": sum(
            output.get("output_type") == "error"
            for cell in executed_code_cells
            for output in cell.get("outputs", [])
        ),
        "model_training_calls": 0,
        "historical_validation_loaded": False,
        "current_test_loaded": False,
        "figures": [
            "figures/01_sequence_length_distribution.png",
            "figures/02_gap_and_missingness_geometry.png",
            "figures/03_architecture_eligibility.png",
            "figures/04_fold_design.png",
        ],
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

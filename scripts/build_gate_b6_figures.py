#!/usr/bin/env python
"""Build artifact-only Gate B6 analytics summaries and publication figures."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

os.environ.setdefault(
    "MPLCONFIGDIR",
    str(Path(__file__).resolve().parents[1] / "work" / "matplotlib"),
)

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from skru1.artifact_io import write_json_atomic


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_RELATIVE = Path("artifacts/model_selection/t1_b6_expanded_v1")
BLUE = "#2463A8"
BLUE_LIGHT = "#BFD5EA"
GOLD = "#D59B2D"
ORANGE = "#D96B32"
INK = "#222831"
GREY = "#8A939E"
GRID = "#D9DEE5"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    artifact = root / ARTIFACT_RELATIVE
    required = {
        "aggregate": artifact / "temporal_aggregate_metrics.csv",
        "folds": artifact / "temporal_fold_metrics.csv",
        "screen": artifact / "screening_register.csv",
        "groups": artifact / "group_metrics.csv",
        "transition": artifact / "transition_metrics.csv",
        "probabilistic": artifact / "probabilistic_metrics.csv",
        "learning": artifact / "learning_curves.csv",
        "sensitivity": artifact / "paired_sensitivity.csv",
        "report": artifact / "gate_b6_report.json",
        "suite": root / "artifacts/governance/final_candidate_suite_v4.json",
    }
    missing = [path.relative_to(root).as_posix() for path in required.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Gate B6 must be frozen before figure generation: {missing}")

    data = {
        key: pd.read_csv(path)
        for key, path in required.items()
        if path.suffix.lower() == ".csv"
    }
    report = json.loads(required["report"].read_text(encoding="utf-8"))
    suite = json.loads(required["suite"].read_text(encoding="utf-8"))
    figure_root = artifact / "figures"
    work_root = root / "work/gate_b6_figures"
    figure_root.mkdir(parents=True, exist_ok=True)
    work_root.mkdir(parents=True, exist_ok=True)

    advanced = sorted(
        data["screen"].loc[
            data["screen"]["advanced_to_robustness"].astype(bool), "model_id"
        ].astype(str)
    )
    top_models = _top_models(data["aggregate"], advanced)
    figure_specs = [
        _temporal_ranking(data["aggregate"], data["screen"], figure_root, work_root),
        _rolling_by_date(data["folds"], top_models, figure_root, work_root),
        _spatial_stability(data["groups"], advanced, figure_root, work_root),
        _transition_heatmap(data["transition"], advanced, figure_root, work_root),
        _calibration_plot(data["probabilistic"], top_models, figure_root, work_root),
        _learning_curves(data["learning"], top_models, figure_root, work_root),
        _paired_sensitivity(data["sensitivity"], advanced, figure_root, work_root),
    ]
    summary = _analytics_summary(data, report, suite, advanced)
    write_json_atomic(
        root,
        artifact / "analytics_summary.json",
        summary,
        work_scope="gate_b6_figures",
    )
    write_json_atomic(
        root,
        artifact / "chart_map.json",
        {
            "schema_version": 1,
            "delivery_surface": "static_png_and_artifact_only_notebook",
            "audience": "technical",
            "source_scope": "saved Gate B6 machine artifacts only",
            "model_training_calls": 0,
            "charts": figure_specs,
        },
        work_scope="gate_b6_figures",
    )
    manifest = {
        "schema_version": 1,
        "figures": [
            {
                "path": spec["output"],
                "bytes": (root / spec["output"]).stat().st_size,
                "sha256": _sha256(root / spec["output"]),
                "width_px": spec["width_px"],
                "height_px": spec["height_px"],
            }
            for spec in figure_specs
        ],
        "visual_qa_required": True,
        "model_training_calls": 0,
    }
    write_json_atomic(
        root,
        artifact / "figure_manifest.json",
        manifest,
        work_scope="gate_b6_figures",
    )
    print(json.dumps({"figures": len(figure_specs), "primary": suite["primary_model_id"]}, ensure_ascii=False))
    return 0


def _top_models(aggregate: pd.DataFrame, advanced: list[str]) -> list[str]:
    ranked = list(
        aggregate.loc[aggregate["model_id"].isin(advanced)]
        .sort_values(["mae", "model_id"])["model_id"]
        .astype(str)
        .head(6)
    )
    for required in ("B1_persistence_last_rate", "B7_two_regime_imm"):
        if required not in ranked:
            ranked.append(required)
    return list(dict.fromkeys(ranked))


def _style_axis(ax: plt.Axes, *, grid_axis: str = "y") -> None:
    ax.set_facecolor("white")
    ax.grid(axis=grid_axis, color=GRID, linewidth=0.8, alpha=0.8)
    ax.set_axisbelow(True)
    ax.spines[["top", "right"]].set_visible(False)
    ax.spines[["left", "bottom"]].set_color(GREY)
    ax.tick_params(colors=INK, labelsize=9)


def _title_block(fig: plt.Figure, title: str, subtitle: str) -> None:
    """Reserve a dedicated title band so title and evidence note cannot overlap."""

    fig.suptitle(
        title,
        x=0.125,
        y=0.985,
        ha="left",
        va="top",
        fontsize=15,
        color=INK,
        weight="bold",
    )
    fig.text(0.125, 0.946, subtitle, ha="left", va="top", fontsize=9, color=GREY)
    fig.subplots_adjust(top=0.875)


def _save(fig: plt.Figure, root: Path, work_root: Path, name: str) -> tuple[str, int, int]:
    temporary = work_root / name
    destination = root / name
    fig.savefig(temporary, dpi=180, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    temporary.replace(destination)
    from PIL import Image

    with Image.open(destination) as image:
        width, height = image.size
    return destination.as_posix(), width, height


def _spec(
    *,
    root: Path,
    output: Path,
    width: int,
    height: int,
    question: str,
    takeaway: str,
    family: str,
    fields: list[str],
) -> dict[str, object]:
    return {
        "analytical_question": question,
        "supported_takeaway": takeaway,
        "family": family,
        "renderer": "matplotlib_static_png",
        "fields": fields,
        "palette_policy": "hard_two_root_cap_plus_neutrals",
        "non_color_distinction": "direct labels, ordering, markers, and reference lines",
        "output": output.relative_to(root).as_posix(),
        "width_px": width,
        "height_px": height,
        "final_qa_surface": "rendered PNG and executed notebook",
    }


def _temporal_ranking(
    aggregate: pd.DataFrame,
    screen: pd.DataFrame,
    figure_root: Path,
    work_root: Path,
) -> dict[str, object]:
    excluded = set(
        screen.loc[
            screen["screen_status"].astype(str).str.startswith("EXCLUDED_GOVERNANCE"), "model_id"
        ].astype(str)
    )
    view = aggregate.loc[~aggregate["model_id"].isin(excluded)].sort_values("mae", ascending=True)
    fig, ax = plt.subplots(figsize=(13, 10))
    colors = [GOLD if model == "B7_two_regime_imm" else BLUE_LIGHT for model in view["model_id"]]
    bars = ax.barh(view["model_id"], view["mae"], color=colors, edgecolor=BLUE, linewidth=0.7)
    b1 = float(view.loc[view["model_id"].eq("B1_persistence_last_rate"), "mae"].iloc[0])
    ax.axvline(b1, color=INK, linestyle="--", linewidth=1.4, label=f"B1 = {b1:.3f}")
    ax.bar_label(bars, labels=[f"{value:.3f}" for value in view["mae"]], padding=3, fontsize=8, color=INK)
    ax.invert_yaxis()
    ax.set_xlim(0, max(8.0, float(view["mae"].max()) * 1.12))
    ax.set_xlabel("MAE, мм/год — меньше лучше")
    ax.set_ylabel("")
    ax.legend(frameon=False, loc="lower right")
    _style_axis(ax, grid_axis="x")
    _title_block(
        fig,
        "Rolling-origin MAE: train-only скрининг моделей",
        "11 целевых дат; 595 outer-прогнозов на модель; TabPFN исключён до расчёта метрик",
    )
    path, width, height = _save(fig, figure_root, work_root, "01_temporal_screen_mae.png")
    best = view.iloc[0]
    return _spec(
        root=ROOT,
        output=Path(path),
        width=width,
        height=height,
        question="Which executed model has the lowest pooled rolling-origin MAE?",
        takeaway=f"{best['model_id']} ranks first at {float(best['mae']):.3f} mm/year.",
        family="comparison_and_ranking_horizontal_bar",
        fields=["model_id", "mae", "screen_status"],
    )


def _rolling_by_date(
    folds: pd.DataFrame,
    models: list[str],
    figure_root: Path,
    work_root: Path,
) -> dict[str, object]:
    view = folds.loc[folds["model_id"].isin(models)].copy()
    view["target_date"] = pd.to_datetime(view["target_date"])
    fig, ax = plt.subplots(figsize=(13, 6.5))
    for model in models:
        row = view.loc[view["model_id"].eq(model)].sort_values("target_date")
        focal = model == "B7_two_regime_imm"
        ax.plot(
            row["target_date"],
            row["mae"],
            marker="o" if focal else "s",
            markersize=5 if focal else 3,
            linewidth=2.5 if focal else 1.1,
            color=GOLD if focal else (INK if model == "B1_persistence_last_rate" else BLUE),
            alpha=1.0 if focal or model == "B1_persistence_last_rate" else 0.40,
            label=model,
        )
    ax.set_xlabel("Целевая дата")
    ax.set_ylabel("MAE, мм/год")
    _style_axis(ax)
    ax.legend(frameon=False, ncol=2, fontsize=8, loc="upper left")
    fig.autofmt_xdate(rotation=30)
    _title_block(
        fig,
        "Rolling-origin MAE по целевой дате",
        "Лучшие прошедшие модели вместе с B1/B7; полная таблица — temporal_fold_metrics.csv",
    )
    path, width, height = _save(fig, figure_root, work_root, "02_rolling_mae_by_date.png")
    return _spec(
        root=ROOT,
        output=Path(path),
        width=width,
        height=height,
        question="Is pooled model ranking stable across the 11 forward target dates?",
        takeaway="Performance varies materially by date, so pooled MAE is not used without fold stability checks.",
        family="trend_highlighted_multi_series_line",
        fields=["target_date", "model_id", "mae"],
    )


def _spatial_stability(
    groups: pd.DataFrame,
    advanced: list[str],
    figure_root: Path,
    work_root: Path,
) -> dict[str, object]:
    view = groups.loc[
        groups["model_id"].isin(advanced)
        & groups["scope"].isin(["equal_profile_macro", "equal_zone_macro"]),
        ["model_id", "scope", "mae"],
    ].pivot_table(index="model_id", columns="scope", values="mae")
    view = view.dropna().sort_values("equal_zone_macro", ascending=False)
    y = np.arange(len(view))
    fig, ax = plt.subplots(figsize=(12, 7.5))
    ax.hlines(y, view["equal_profile_macro"], view["equal_zone_macro"], color=GRID, linewidth=2)
    ax.scatter(view["equal_profile_macro"], y, marker="o", color=BLUE, s=55, label="равный вес профилей")
    ax.scatter(view["equal_zone_macro"], y, marker="D", facecolors="white", edgecolors=ORANGE, s=55, linewidth=1.5, label="равный вес зон")
    ax.set_yticks(y, view.index)
    ax.set_xlim(left=0)
    ax.set_xlabel("MAE, мм/год — меньше лучше")
    ax.set_ylabel("")
    ax.legend(frameon=False, loc="upper right")
    _style_axis(ax, grid_axis="x")
    _title_block(
        fig,
        "Пространственная устойчивость: macro MAE",
        "42 leave-profile-out и 12 leave-zone-out folds; каждая группа получает равный вес",
    )
    path, width, height = _save(fig, figure_root, work_root, "03_spatial_stability.png")
    return _spec(
        root=ROOT,
        output=Path(path),
        width=width,
        height=height,
        question="How much do advanced models degrade under profile and zone holdouts?",
        takeaway="Profile and zone macro MAE are shown separately because the four-zone result is descriptive, not inferential.",
        family="uncertainty_and_benchmark_connected_dot",
        fields=["model_id", "scope", "mae"],
    )


def _transition_heatmap(
    transition: pd.DataFrame,
    advanced: list[str],
    figure_root: Path,
    work_root: Path,
) -> dict[str, object]:
    order = ["stable", "accelerating", "decelerating", "volatile_or_gap"]
    view = transition.loc[
        transition["design"].eq("rolling_origin")
        & transition["dimension"].eq("transition")
        & transition["model_id"].isin(advanced)
        & transition["segment"].isin(order)
    ]
    matrix = view.pivot(index="model_id", columns="segment", values="mae").reindex(columns=order)
    support = view.pivot(index="model_id", columns="segment", values="support_status").reindex(columns=order)
    matrix = matrix.sort_values("volatile_or_gap", ascending=True)
    support = support.reindex(matrix.index)
    fig, ax = plt.subplots(figsize=(10, 7.5))
    image = ax.imshow(matrix.to_numpy(float), cmap="Blues", aspect="auto")
    segment_labels = {
        "stable": "стабильный",
        "accelerating": "ускорение",
        "decelerating": "замедление",
        "volatile_or_gap": "волатильность / пропуск",
    }
    ax.set_xticks(
        np.arange(len(matrix.columns)),
        [segment_labels[column] for column in matrix.columns],
        rotation=18,
        ha="right",
    )
    ax.set_yticks(np.arange(len(matrix.index)), matrix.index)
    for row in range(len(matrix.index)):
        for column in range(len(matrix.columns)):
            value = matrix.iloc[row, column]
            if np.isfinite(value):
                low_support = support.iloc[row, column] == "DESCRIPTIVE_LOW_SUPPORT"
                ax.text(
                    column,
                    row,
                    f"{value:.2f}{'*' if low_support else ''}",
                    ha="center",
                    va="center",
                    fontsize=8,
                    color=INK,
                )
    colorbar = fig.colorbar(image, ax=ax, pad=0.02)
    colorbar.set_label("MAE, мм/год")
    ax.set_xlabel("")
    ax.set_ylabel("")
    _title_block(
        fig,
        "Rolling-origin MAE по режимам перехода",
        "* DESCRIPTIVE_LOW_SUPPORT: менее 20 origins или 5 профилей; такие ячейки не участвуют в выборе",
    )
    path, width, height = _save(fig, figure_root, work_root, "04_transition_error_heatmap.png")
    return _spec(
        root=ROOT,
        output=Path(path),
        width=width,
        height=height,
        question="Which advanced models remain stable across transition regimes?",
        takeaway="Transition regimes expose error patterns hidden by pooled MAE; low-support cells remain descriptive.",
        family="matrix_and_cohort_heatmap",
        fields=["model_id", "transition_segment", "mae", "support_status"],
    )


def _calibration_plot(
    probabilistic: pd.DataFrame,
    models: list[str],
    figure_root: Path,
    work_root: Path,
) -> dict[str, object]:
    view = probabilistic.loc[
        probabilistic["design"].eq("rolling_origin")
        & probabilistic["interval_source"].eq("conformalized")
        & probabilistic["dimension"].eq("overall")
        & probabilistic["model_id"].isin(models)
    ]
    nominal = np.array([0.50, 0.80, 0.95])
    fig, ax = plt.subplots(figsize=(8.5, 7))
    ax.plot(nominal, nominal, color=INK, linestyle="--", linewidth=1.4, label="идеальная калибровка")
    for _, row in view.iterrows():
        observed = np.array([row["coverage_50"], row["coverage_80"], row["coverage_95"]], float)
        focal = row["model_id"] == "B7_two_regime_imm"
        ax.plot(
            nominal,
            observed,
            marker="o" if focal else "s",
            linewidth=2.4 if focal else 1.1,
            color=GOLD if focal else BLUE,
            alpha=1.0 if focal else 0.45,
            label=row["model_id"],
        )
    ax.set_xlim(0.46, 0.99)
    ax.set_ylim(0.46, 0.99)
    ax.set_xlabel("Номинальное покрытие")
    ax.set_ylabel("Эмпирическое покрытие")
    ax.legend(frameon=False, fontsize=8, ncol=2, loc="upper left")
    _style_axis(ax)
    _title_block(
        fig,
        "Калибровка conformal-интервалов",
        "Калибровка — только по inner rolling OOF residuals; outer labels никогда не используются как вход",
    )
    path, width, height = _save(fig, figure_root, work_root, "05_conformal_calibration.png")
    return _spec(
        root=ROOT,
        output=Path(path),
        width=width,
        height=height,
        question="Do common conformal intervals achieve their nominal coverage?",
        takeaway="Coverage is assessed against the ideal diagonal using inner-OOF-only calibration.",
        family="uncertainty_and_benchmark_calibration_line",
        fields=["model_id", "coverage_50", "coverage_80", "coverage_95"],
    )


def _learning_curves(
    learning: pd.DataFrame,
    models: list[str],
    figure_root: Path,
    work_root: Path,
) -> dict[str, object]:
    view = learning.loc[learning["model_id"].isin(models)].copy()
    fig, ax = plt.subplots(figsize=(10.5, 6.5))
    for model in models:
        row = view.loc[view["model_id"].eq(model)].sort_values("training_rows")
        focal = model == "B7_two_regime_imm"
        ax.plot(
            row["training_rows"],
            row["mae"],
            marker="o",
            linewidth=2.5 if focal else 1.2,
            color=GOLD if focal else (INK if model == "B1_persistence_last_rate" else BLUE),
            alpha=1.0 if focal or model == "B1_persistence_last_rate" else 0.45,
            label=model,
        )
    ax.set_xlabel("Число train origins")
    ax.set_ylabel("MAE на audit tail 2023-11-07, мм/год")
    ax.legend(frameon=False, fontsize=8, ncol=2)
    _style_axis(ax)
    _title_block(
        fig,
        "Learning curves на audit tail с замороженными параметрами",
        "217, 423, 708 и 823 train origins; гиперпараметры на кривых не перенастраиваются",
    )
    path, width, height = _save(fig, figure_root, work_root, "06_learning_curves.png")
    return _spec(
        root=ROOT,
        output=Path(path),
        width=width,
        height=height,
        question="Does audit-tail error change as more train campaigns become available?",
        takeaway="The fixed-parameter curves diagnose data sufficiency without creating a new tuning loop.",
        family="trend_highlighted_multi_series_line",
        fields=["training_rows", "model_id", "mae"],
    )


def _paired_sensitivity(
    sensitivity: pd.DataFrame,
    advanced: list[str],
    figure_root: Path,
    work_root: Path,
) -> dict[str, object]:
    view = sensitivity.loc[
        sensitivity["model_id"].isin(advanced)
        & sensitivity["reference_model_id"].eq("B7_two_regime_imm")
        & sensitivity["cluster_column"].eq("profile_id")
    ].sort_values("mean_absolute_error_delta", ascending=False)
    y = np.arange(len(view))
    fig, ax = plt.subplots(figsize=(11, 7))
    ax.hlines(y, view["lower_025"], view["upper_975"], color=BLUE, linewidth=2)
    ax.scatter(view["mean_absolute_error_delta"], y, color=GOLD, edgecolor=INK, s=55, zorder=3)
    ax.axvline(0, color=INK, linestyle="--", linewidth=1.3)
    ax.set_yticks(y, view["model_id"])
    ax.set_xlabel("Парная дельта absolute error, мм/год — отрицательная в пользу модели")
    ax.set_ylabel("")
    _style_axis(ax, grid_axis="x")
    _title_block(
        fig,
        "Profile-cluster sensitivity относительно B7",
        "2 000 cluster-resampling повторов; интервалы чувствительности, а не i.i.d. доверительные интервалы",
    )
    path, width, height = _save(fig, figure_root, work_root, "07_profile_cluster_sensitivity.png")
    return _spec(
        root=ROOT,
        output=Path(path),
        width=width,
        height=height,
        question="Are paired MAE differences versus B7 robust to profile-cluster resampling?",
        takeaway="Intervals are reported as small-cluster sensitivity evidence rather than naive row-level inference.",
        family="uncertainty_and_benchmark_forest_interval",
        fields=["model_id", "mean_absolute_error_delta", "lower_025", "upper_975"],
    )


def _analytics_summary(
    data: dict[str, pd.DataFrame],
    report: dict[str, object],
    suite: dict[str, object],
    advanced: list[str],
) -> dict[str, object]:
    aggregate = data["aggregate"].sort_values(["mae", "model_id"])
    b1 = aggregate.loc[aggregate["model_id"].eq("B1_persistence_last_rate")].iloc[0]
    b7 = aggregate.loc[aggregate["model_id"].eq("B7_two_regime_imm")].iloc[0]
    best = aggregate.iloc[0]
    focus_models = [
        "B1_persistence_last_rate",
        "B7_two_regime_imm",
        "B8_student_t_robust_imm",
        "Z01_elastic_net",
        "Z08_xgboost",
        "Z09_lightgbm",
    ]
    focus_temporal = aggregate.loc[
        aggregate["model_id"].isin(focus_models),
        [
            "model_id",
            "pooled_rows",
            "mae",
            "median_absolute_error",
            "rmse",
            "bias",
            "p95_absolute_error",
            "max_absolute_error",
            "median_fold_mae",
            "b1_skill",
        ],
    ]
    focus_spatial = data["groups"].loc[
        data["groups"]["model_id"].isin(focus_models)
        & data["groups"]["scope"].isin(
            ["equal_profile_macro", "worst_profile", "equal_zone_macro", "worst_zone"]
        ),
        ["design", "model_id", "scope", "mae"],
    ]
    focus_transition = data["transition"].loc[
        data["transition"]["design"].eq("rolling_origin")
        & data["transition"]["model_id"].isin(focus_models)
        & (
            data["transition"]["dimension"].eq("pooled_transition")
            | (
                data["transition"]["dimension"].eq("transition")
                & data["transition"]["segment"].isin(["accelerating", "volatile_or_gap"])
            )
        ),
        ["model_id", "dimension", "segment", "rows", "profiles", "support_status", "mae"],
    ]
    focus_calibration = data["probabilistic"].loc[
        data["probabilistic"]["design"].eq("rolling_origin")
        & data["probabilistic"]["model_id"].isin(focus_models)
        & data["probabilistic"]["interval_source"].eq("conformalized")
        & data["probabilistic"]["dimension"].eq("overall"),
        [
            "model_id",
            "n",
            "coverage_50",
            "coverage_80",
            "coverage_95",
            "mean_width_95",
            "weighted_interval_score",
        ],
    ]
    focus_sensitivity = data["sensitivity"].loc[
        data["sensitivity"]["model_id"].isin(focus_models)
        & data["sensitivity"]["reference_model_id"].eq("B7_two_regime_imm")
        & data["sensitivity"]["cluster_column"].eq("profile_id"),
        [
            "model_id",
            "clusters",
            "paired_rows",
            "mean_absolute_error_delta",
            "lower_025",
            "upper_975",
            "improved_cluster_fraction",
        ],
    ]
    screen_counts = (
        data["screen"].groupby("screen_status", dropna=False).size().rename("models").reset_index()
    )

    def records(frame: pd.DataFrame) -> list[dict[str, object]]:
        return json.loads(frame.to_json(orient="records"))

    return {
        "schema_version": 1,
        "scientific_scope": "train_only_internal_research",
        "final_quality_claim_allowed": False,
        "status": report["status"],
        "primary_model_id": suite["primary_model_id"],
        "registry_models": int(len(data["screen"])),
        "executed_models": int((~data["screen"]["screen_status"].astype(str).str.startswith("EXCLUDED_GOVERNANCE")).sum()),
        "excluded_models": list(
            data["screen"].loc[
                data["screen"]["screen_status"].astype(str).str.startswith("EXCLUDED_GOVERNANCE"),
                "model_id",
            ].astype(str)
        ),
        "advanced_models": advanced,
        "screen_status_counts": records(screen_counts),
        "best_rolling_model_id": str(best["model_id"]),
        "best_rolling_mae_mm_per_year": float(best["mae"]),
        "b1_rolling_mae_mm_per_year": float(b1["mae"]),
        "b7_rolling_mae_mm_per_year": float(b7["mae"]),
        "b7_skill_vs_b1": float(b7["b1_skill"]),
        "top_models": aggregate.loc[:, ["model_id", "mae", "median_fold_mae", "b1_skill"]]
        .head(10)
        .to_dict(orient="records"),
        "focus_temporal_metrics": records(focus_temporal),
        "focus_spatial_metrics": records(focus_spatial),
        "focus_transition_metrics": records(focus_transition),
        "focus_conformal_metrics": records(focus_calibration),
        "focus_profile_cluster_sensitivity": records(focus_sensitivity),
        "benchmark_counts": {
            "rolling_outer_folds": 11,
            "leave_profile_outer_folds": 42,
            "leave_zone_outer_folds": 12,
            "robustness_model_folds": 594,
            "temporal_prediction_rows": int(len(data["folds"]) and aggregate["pooled_rows"].sum()),
        },
        "historical_validation_loaded": False,
        "current_test_loaded": False,
        "new_holdout_seen": False,
        "model_training_calls": 0,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())

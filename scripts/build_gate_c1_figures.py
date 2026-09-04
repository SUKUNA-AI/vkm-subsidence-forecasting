#!/usr/bin/env python
"""Build the four frozen, artifact-only Gate C1 reader figures.

This script is deliberately outside the Gate C1 training-code hash.  It reads
only independently scored C1 artifacts and never imports an adapter, Torch, or
the canonical T1 split loader.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("MPLCONFIGDIR", str(ROOT / "work" / "matplotlib_gate_c1"))

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from PIL import Image  # noqa: E402


ARTIFACT_RELATIVE = Path("artifacts/model_selection/t1_gate_c1_compact_screen_v1")
DEEP_MODELS = (
    "C01_compact_gru",
    "C02_compact_lstm",
    "C03_causal_tcn",
    "C04_probabilistic_gru_student_t",
)
COMPARATORS = (
    "B1_persistence_last_rate",
    "B7_two_regime_imm",
    "B8_student_t_robust_imm",
)
LABELS = {
    "B1_persistence_last_rate": "B1 persistence / last-rate",
    "B7_two_regime_imm": "B7 two-regime IMM",
    "B8_student_t_robust_imm": "B8 robust Student-t IMM",
    "C01_compact_gru": "C01 compact GRU",
    "C02_compact_lstm": "C02 compact LSTM",
    "C03_causal_tcn": "C03 causal TCN",
    "C04_probabilistic_gru_student_t": "C04 Student-t GRU",
}
BLUE = "#2463A8"
BLUE_LIGHT = "#9EC4E6"
GOLD = "#D59B2D"
ORANGE = "#D96B32"
INK = "#25313C"
GREY = "#87929D"
GRID = "#DCE3E9"
GREEN = "#2F855A"
RED = "#B54747"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_rows(aggregate: pd.DataFrame) -> pd.DataFrame:
    deep = aggregate.loc[
        aggregate["model_id"].isin(DEEP_MODELS)
        & aggregate["aggregation"].eq("mean_of_fixed_seeds")
    ].copy()
    comparators = aggregate.loc[aggregate["model_id"].isin(COMPARATORS)].copy()
    result = pd.concat((comparators, deep), ignore_index=True)
    if set(result["model_id"].astype(str)) != set((*COMPARATORS, *DEEP_MODELS)):
        raise RuntimeError("Gate C1 canonical aggregate rows are incomplete")
    if result["model_id"].duplicated().any():
        raise RuntimeError("Gate C1 canonical aggregate rows are not unique")
    return result


def canonical_folds(folds: pd.DataFrame) -> pd.DataFrame:
    deep = folds.loc[
        folds["model_id"].isin(DEEP_MODELS)
        & folds["aggregation"].eq("mean_of_fixed_seeds")
    ].copy()
    comparators = folds.loc[folds["model_id"].isin(COMPARATORS)].copy()
    result = pd.concat((comparators, deep), ignore_index=True)
    counts = result.groupby("model_id")["fold_id"].nunique()
    if len(counts) != 7 or not counts.eq(11).all():
        raise RuntimeError("Gate C1 canonical fold rows must cover 7 models x 11 folds")
    return result


def apply_style() -> None:
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "axes.edgecolor": "#8A98A5",
            "axes.labelcolor": INK,
            "axes.titlecolor": INK,
            "xtick.color": "#46535F",
            "ytick.color": "#46535F",
            "figure.facecolor": "white",
            "axes.facecolor": "white",
        }
    )


def save(fig: plt.Figure, path: Path, *, root: Path) -> None:
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    work = root / "work" / "gate_c1_reporting"
    work.mkdir(parents=True, exist_ok=True)
    temporary = work / f"{path.stem}.{uuid4().hex}.png"
    fig.savefig(temporary, dpi=190, bbox_inches="tight", facecolor="white", format="png")
    plt.close(fig)
    temporary.replace(path)


def figure_temporal_mae(
    canonical: pd.DataFrame, screening: pd.DataFrame, output: Path, *, root: Path
) -> None:
    view = canonical[["model_id", "mae"]].sort_values(["mae", "model_id"], ascending=[True, True])
    status = screening.set_index("model_id")["status"].to_dict()
    colors = []
    for model_id in view["model_id"]:
        if model_id == "B7_two_regime_imm":
            colors.append(GOLD)
        elif model_id == "B1_persistence_last_rate":
            colors.append(INK)
        elif model_id in COMPARATORS:
            colors.append(GREY)
        elif status.get(model_id) == "PASSED_TEMPORAL_SCREEN":
            colors.append(GREEN)
        else:
            colors.append(BLUE_LIGHT)
    fig, ax = plt.subplots(figsize=(11.8, 6.8))
    y = np.arange(len(view))
    bars = ax.barh(y, view["mae"], color=colors, edgecolor="white", linewidth=0.8)
    ax.set_yticks(y, [LABELS[item] for item in view["model_id"]])
    ax.invert_yaxis()
    b1 = float(view.loc[view["model_id"].eq("B1_persistence_last_rate"), "mae"].iloc[0])
    b7 = float(view.loc[view["model_id"].eq("B7_two_regime_imm"), "mae"].iloc[0])
    ax.axvline(b1, color=INK, linestyle="--", linewidth=1.3, label=f"B1 = {b1:.3f}")
    ax.axvline(b7, color=GOLD, linestyle=":", linewidth=1.8, label=f"B7 = {b7:.3f}")
    ax.set_xlabel("Pooled rolling-origin MAE, мм/год (меньше — лучше)")
    ax.set_title(
        "Gate C1: пятиseedовый temporal screen compact sequence-моделей\n"
        "595 одинаковых train-only origins; deep result = mean of five fixed seeds",
        loc="left",
        fontsize=14,
        fontweight="bold",
    )
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.spines[["top", "right", "left"]].set_visible(False)
    for bar, value in zip(bars, view["mae"], strict=True):
        ax.text(
            float(value) + max(0.025, 0.006 * b1),
            bar.get_y() + bar.get_height() / 2,
            f"{float(value):.3f}",
            va="center",
            fontsize=9,
            color=INK,
        )
    ax.legend(frameon=False, loc="lower right")
    save(fig, output, root=root)


def figure_rolling_dates(folds: pd.DataFrame, output: Path, *, root: Path) -> None:
    view = folds.copy()
    view["target_date"] = pd.to_datetime(view["target_date"])
    styles = {
        "B1_persistence_last_rate": (INK, "--", 2.0),
        "B7_two_regime_imm": (GOLD, "-", 2.5),
        "B8_student_t_robust_imm": (GREY, ":", 2.0),
        "C01_compact_gru": ("#2878B5", "-", 1.7),
        "C02_compact_lstm": ("#59A14F", "-", 1.7),
        "C03_causal_tcn": (ORANGE, "-", 1.7),
        "C04_probabilistic_gru_student_t": ("#8C6BB1", "-", 1.7),
    }
    fig, ax = plt.subplots(figsize=(13.2, 7.1))
    for model_id in (*COMPARATORS, *DEEP_MODELS):
        part = view.loc[view["model_id"].eq(model_id)].sort_values("target_date")
        color, linestyle, width = styles[model_id]
        ax.plot(
            part["target_date"],
            part["mae"],
            marker="o",
            markersize=4.2,
            color=color,
            linestyle=linestyle,
            linewidth=width,
            label=LABELS[model_id],
        )
    ax.set_ylabel("MAE по outer fold, мм/год")
    ax.set_xlabel("Target date")
    ax.set_title(
        "Rolling-origin устойчивость по 11 будущим кампаниям внутри train",
        loc="left",
        fontsize=14,
        fontweight="bold",
    )
    ax.grid(color=GRID, linewidth=0.8)
    ax.spines[["top", "right"]].set_visible(False)
    ax.tick_params(axis="x", rotation=35)
    ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1.0), frameon=False)
    save(fig, output, root=root)


def figure_seed_stability(
    aggregate: pd.DataFrame, seed_stability: pd.DataFrame, output: Path, *, root: Path
) -> None:
    fig, ax = plt.subplots(figsize=(11.8, 6.8))
    x = np.arange(len(DEEP_MODELS))
    rng = np.random.default_rng(42117)
    for idx, model_id in enumerate(DEEP_MODELS):
        values = aggregate.loc[
            aggregate["model_id"].eq(model_id)
            & aggregate["aggregation"].eq("single_seed"),
            "mae",
        ].to_numpy(float)
        if len(values) != 5:
            raise RuntimeError(f"Gate C1 seed metrics incomplete for {model_id}")
        jitter = rng.uniform(-0.06, 0.06, size=len(values))
        ax.scatter(np.full(len(values), idx) + jitter, values, s=48, color=BLUE, alpha=0.82)
        ensemble = float(
            seed_stability.loc[seed_stability["model_id"].eq(model_id), "ensemble_mae"].iloc[0]
        )
        ax.scatter(idx, ensemble, marker="D", s=72, color=GOLD, edgecolor=INK, zorder=4)
        lo, hi = float(np.min(values)), float(np.max(values))
        ax.vlines(idx, lo, hi, color=GREY, linewidth=1.3, zorder=0)
    ax.set_xticks(
        x,
        [
            "C01\ncompact GRU",
            "C02\ncompact LSTM",
            "C03\ncausal TCN",
            "C04\nStudent-t GRU",
        ],
    )
    ax.set_ylabel("Pooled MAE, мм/год")
    ax.set_title(
        "Seed stability: пять фиксированных seeds и canonical ensemble",
        loc="left",
        fontsize=14,
        fontweight="bold",
    )
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.spines[["top", "right"]].set_visible(False)
    ax.scatter([], [], s=48, color=BLUE, label="single seed")
    ax.scatter([], [], marker="D", s=72, color=GOLD, edgecolor=INK, label="mean of five seeds")
    ax.legend(frameon=False)
    save(fig, output, root=root)


def figure_cost_quality(canonical: pd.DataFrame, output: Path, *, root: Path) -> None:
    view = canonical.loc[canonical["model_id"].isin(DEEP_MODELS)].copy()
    fig, axes = plt.subplots(1, 2, figsize=(13.2, 5.9), sharey=True)
    colors = [BLUE, GREEN, ORANGE, "#8C6BB1"]
    for ax, column, xlabel in (
        (axes[0], "parameter_count_max", "Parameter count (log scale)"),
        (axes[1], "fit_seconds_total", "Суммарное время 55 outer refits, с"),
    ):
        for color, model_id in zip(colors, DEEP_MODELS, strict=True):
            row = view.loc[view["model_id"].eq(model_id)].iloc[0]
            ax.scatter(float(row[column]), float(row["mae"]), s=105, color=color, edgecolor="white")
            ax.annotate(
                model_id.split("_")[0],
                (float(row[column]), float(row["mae"])),
                xytext=(5, 5),
                textcoords="offset points",
                fontsize=9,
            )
        if column == "parameter_count_max":
            ax.set_xscale("log")
        ax.set_xlabel(xlabel)
        ax.grid(color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)
        ax.spines[["top", "right"]].set_visible(False)
    axes[0].set_ylabel("Canonical ensemble MAE, мм/год")
    fig.suptitle(
        "Качество и вычислительная сложность compact sequence-моделей",
        x=0.03,
        ha="left",
        fontsize=14,
        fontweight="bold",
        color=INK,
    )
    save(fig, output, root=root)


def image_checks(path: Path, *, root: Path) -> dict[str, Any]:
    with Image.open(path) as image:
        rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
        width, height = image.size
    non_white = float(np.mean(np.any(rgb < 248, axis=2)))
    passed = width >= 1400 and height >= 800 and non_white >= 0.025
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": sha256_file(path),
        "width_px": width,
        "height_px": height,
        "non_white_fraction": non_white,
        "status": "PASS" if passed else "FAIL",
    }


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    artifact = root / ARTIFACT_RELATIVE
    required = {
        "aggregate": artifact / "temporal_aggregate_metrics.csv",
        "folds": artifact / "temporal_fold_metrics.csv",
        "seed": artifact / "seed_stability_metrics.csv",
        "compute": artifact / "compute_resource_inventory.csv",
        "screening": artifact / "screening_register.csv",
        "admission": artifact / "c2_admission_manifest.json",
        "validation": artifact / "validation_report.json",
    }
    missing = [path.relative_to(root).as_posix() for path in required.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Validate Gate C1 before figure generation: {missing}")
    validation = json.loads(required["validation"].read_text(encoding="utf-8"))
    if validation.get("status") != "PASS_C1_TEMPORAL_SCREEN":
        raise RuntimeError("Gate C1 validation authority is not PASS_C1_TEMPORAL_SCREEN")
    aggregate = pd.read_csv(required["aggregate"])
    folds = pd.read_csv(required["folds"])
    seed = pd.read_csv(required["seed"])
    screening = pd.read_csv(required["screening"])
    canonical = canonical_rows(aggregate)
    canonical_by_fold = canonical_folds(folds)
    figure_root = artifact / "figures"
    figure_root.mkdir(parents=True, exist_ok=True)
    apply_style()
    outputs = (
        figure_root / "01_ensemble_temporal_mae.png",
        figure_root / "02_rolling_mae_by_target_date.png",
        figure_root / "03_seed_stability.png",
        figure_root / "04_mae_vs_complexity.png",
    )
    figure_temporal_mae(canonical, screening, outputs[0], root=root)
    figure_rolling_dates(canonical_by_fold, outputs[1], root=root)
    figure_seed_stability(aggregate, seed, outputs[2], root=root)
    figure_cost_quality(canonical, outputs[3], root=root)
    checks = [image_checks(path, root=root) for path in outputs]
    if any(item["status"] != "PASS" for item in checks):
        raise RuntimeError(f"Gate C1 figure QA failed: {checks}")
    admission = json.loads(required["admission"].read_text(encoding="utf-8"))
    analytics = {
        "schema_version": 1,
        "gate": "C1_COMPACT_SEQUENCE_TEMPORAL_SCREEN",
        "status": validation["status"],
        "scientific_scope": "train_only_internal_research",
        "canonical_models": canonical.sort_values("mae")[
            [
                "model_id",
                "mae",
                "median_fold_mae",
                "rmse",
                "b1_skill",
                "parameter_count_max",
                "fit_seconds_total",
            ]
        ].to_dict("records"),
        "admitted_model_ids": admission["admitted_model_ids"],
        "historical_validation_loaded": False,
        "current_test_loaded": False,
        "new_holdout_seen": False,
        "profile_zone_transition_audit_executed": False,
        "suite_v5_created": False,
        "model_training_calls": 0,
    }
    analytics_path = artifact / "analytics_summary.json"
    write_text_atomic(
        root,
        analytics_path,
        json.dumps(analytics, ensure_ascii=False, indent=2) + "\n",
    )
    manifest = {
        "schema_version": 1,
        "gate": "C1_COMPACT_SEQUENCE_TEMPORAL_SCREEN",
        "scientific_scope": "train_only_internal_research",
        "sources": {
            name: {
                "path": path.relative_to(root).as_posix(),
                "sha256": sha256_file(path),
            }
            for name, path in required.items()
        },
        "figures": checks,
        "chart_map": [
            {
                "figure": outputs[0].relative_to(root).as_posix(),
                "analytical_question": "Как canonical temporal MAE deep-моделей соотносится с B1, B7 и B8?",
                "family": "Comparison & Ranking",
                "variant": "sorted horizontal bar with frozen benchmark rules",
                "fields": ["model_id", "mae", "status"],
                "supported_takeaway": "C01 — единственная deep-модель около уровня B1; B7 остаётся сильнее всех C1 architectures.",
                "palette_policy": "hard two-root cap plus neutral context",
            },
            {
                "figure": outputs[1].relative_to(root).as_posix(),
                "analytical_question": "Насколько ошибки меняются по 11 последовательным target dates?",
                "family": "Trend",
                "variant": "highlighted multi-series line",
                "fields": ["target_date", "model_id", "mae"],
                "supported_takeaway": "Aggregate MAE скрывает сильную fold-to-fold неоднородность; ни одна deep-модель не доминирует на всех датах.",
                "palette_policy": "relaxed multi-category with line-style context",
            },
            {
                "figure": outputs[2].relative_to(root).as_posix(),
                "analytical_question": "Насколько canonical результат зависит от пяти фиксированных seeds?",
                "family": "Distribution across groups",
                "variant": "labeled dot-and-range with ensemble marker",
                "fields": ["model_id", "seed", "mae", "ensemble_mae"],
                "supported_takeaway": "Все четыре модели проходят описательные IQR/CV guards; ensemble улучшает median single-seed MAE.",
                "palette_policy": "single-root preferred with gold ensemble marker",
            },
            {
                "figure": outputs[3].relative_to(root).as_posix(),
                "analytical_question": "Как temporal MAE четырёх заранее заданных architectures связано с размером и временем outer refit?",
                "family": "Relationship",
                "variant": "two labeled small-multiple scatters; four preregistered models are intentionally exhaustive",
                "fields": ["model_id", "mae", "parameter_count_max", "fit_seconds_total"],
                "supported_takeaway": "C01 даёт лучший deep MAE, но является крупнейшей и самой дорогой из четырёх выбранных outer specifications.",
                "palette_policy": "relaxed multi-category for four model identities",
            },
        ],
        "analytics_summary": {
            "path": analytics_path.relative_to(root).as_posix(),
            "sha256": sha256_file(analytics_path),
        },
        "model_training_calls": 0,
    }
    manifest_path = artifact / "figure_manifest.json"
    write_text_atomic(
        root,
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )
    visual_qa = {
        "schema_version": 1,
        "status": "PASS",
        "checks": checks,
        "manual_visual_review_required": True,
        "manual_visual_review_completed": False,
        "model_training_calls": 0,
    }
    qa_path = artifact / "visual_qa_report.json"
    write_text_atomic(
        root,
        qa_path,
        json.dumps(visual_qa, ensure_ascii=False, indent=2) + "\n",
    )
    print(json.dumps({"status": "PASS", "figures": len(outputs), "model_training_calls": 0}, ensure_ascii=False, indent=2))
    return 0


def write_text_atomic(root: Path, path: Path, content: str) -> None:
    """Publish one reporting text artifact through the repository work area."""

    path.parent.mkdir(parents=True, exist_ok=True)
    work = root / "work" / "gate_c1_reporting"
    work.mkdir(parents=True, exist_ok=True)
    temporary = work / f"{path.name}.{uuid4().hex}.tmp"
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())

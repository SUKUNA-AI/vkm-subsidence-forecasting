"""Build reader-facing figures for the Word special section from frozen artifacts."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("MPLCONFIGDIR", str(ROOT / "work" / "matplotlib"))

import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402


SOURCE = (
    ROOT
    / "artifacts"
    / "model_selection"
    / "t1_b6_expanded_v1"
    / "temporal_aggregate_metrics.csv"
)
OUTPUT_DIR = ROOT / "docs" / "thesis" / "figures"
OUTPUT = OUTPUT_DIR / "01_temporal_screen_mae.png"
MANIFEST = OUTPUT_DIR / "figure_manifest.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_temporal_screen() -> None:
    frame = pd.read_csv(SOURCE).sort_values(["mae", "model_id"], ascending=[True, True])
    b1_mae = float(
        frame.loc[frame["model_id"] == "B1_persistence_last_rate", "mae"].iloc[0]
    )
    colors = [
        "#D59A24"
        if model_id == "B7_two_regime_imm"
        else "#263238"
        if model_id == "B1_persistence_last_rate"
        else "#AFCBE5"
        for model_id in frame["model_id"]
    ]

    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "axes.edgecolor": "#90A4AE",
            "axes.labelcolor": "#263238",
            "xtick.color": "#455A64",
            "ytick.color": "#263238",
        }
    )
    fig, ax = plt.subplots(figsize=(12, 9.5), dpi=180)
    bars = ax.barh(frame["model_id"], frame["mae"], color=colors, edgecolor="#5B8DB8")
    ax.invert_yaxis()
    ax.axvline(b1_mae, color="#263238", linestyle="--", linewidth=1.5, label=f"B1 = {b1_mae:.3f}")
    ax.set_xlim(0, max(8.0, float(frame["mae"].max()) + 0.7))
    ax.set_xlabel("MAE, мм/год — меньше лучше")
    ax.set_ylabel("")
    ax.grid(axis="x", color="#DDE5EC", linewidth=0.8)
    ax.set_axisbelow(True)
    ax.spines[["top", "right"]].set_visible(False)
    ax.set_title(
        "Rolling-origin MAE: train-only screening моделей\n"
        "11 target dates; 595 outer predictions на модель; ниже — лучше",
        loc="left",
        fontsize=15,
        fontweight="bold",
        color="#263238",
        pad=14,
    )
    for bar, value in zip(bars, frame["mae"], strict=True):
        ax.text(
            float(value) + 0.05,
            bar.get_y() + bar.get_height() / 2,
            f"{float(value):.3f}",
            va="center",
            ha="left",
            fontsize=8.5,
            color="#263238",
        )
    ax.legend(loc="lower right", frameon=False)
    fig.tight_layout()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUTPUT, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def main() -> None:
    build_temporal_screen()
    payload = {
        "schema_version": 1,
        "source": SOURCE.relative_to(ROOT).as_posix(),
        "source_sha256": _sha256(SOURCE),
        "figure": OUTPUT.relative_to(ROOT).as_posix(),
        "figure_sha256": _sha256(OUTPUT),
        "claim_boundary": "train_only_internal_research",
    }
    MANIFEST.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

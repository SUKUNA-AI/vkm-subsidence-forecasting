#!/usr/bin/env python3
"""Build the four-profile Musikhin publication digitization atlas.

The frozen CSV and native source rasters are derived inputs. The script verifies
them and the parent PDF before producing charts, inventories, limitations and a
hash manifest. It does not train models or create temporal observations.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
from collections import Counter
from pathlib import Path
import platform
import shutil

os.environ["MPLCONFIGDIR"] = str(
    Path(__file__).resolve().parents[1] / "work/reconstruction_atlas/matplotlib"
)
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from PIL import Image


DEFAULT_CONFIG = "configs/musikhin_profiles_v1.json"
REQUIRED_COLUMNS = {
    "source_file",
    "source_sha256",
    "pdf_page",
    "profile_label",
    "marker_index",
    "distance_label_as_printed_km",
    "distance_value_from_label_km",
    "method",
    "observation_interval_label",
    "source_pixel_x",
    "source_pixel_y",
    "displacement_negative_down_mm",
    "digitization_uncertainty_mm_not_measurement_sigma",
    "status",
    "provenance",
    "eligible_as_t1_time_series",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--output", default=None)
    return parser.parse_args()


def relative_path(root: Path, value: str) -> Path:
    path = Path(value)
    candidate = (root / path).resolve()
    if path.is_absolute() or not candidate.is_relative_to(root):
        raise ValueError(f"Path must remain inside the repository: {value}")
    return candidate


def sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def verify_file(path: Path, expected_hash: str, expected_size: int) -> dict:
    if not path.is_file():
        raise FileNotFoundError(path)
    actual_size = path.stat().st_size
    actual_hash = sha256(path)
    if actual_size != expected_size:
        raise ValueError(f"Size mismatch for {path}: {actual_size} != {expected_size}")
    if actual_hash != expected_hash:
        raise ValueError(f"SHA-256 mismatch for {path}: {actual_hash} != {expected_hash}")
    return {"path": path, "sha256": actual_hash, "size_bytes": actual_size}


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if set(reader.fieldnames or []) != REQUIRED_COLUMNS:
            missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
            extra = set(reader.fieldnames or []) - REQUIRED_COLUMNS
            raise ValueError(f"Digitization schema mismatch; missing={sorted(missing)}, extra={sorted(extra)}")
        return list(reader)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def write_rows(path: Path, rows: list[dict], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    names = fieldnames or (list(rows[0]) if rows else [])
    if not names:
        raise ValueError(f"Cannot write headerless empty CSV: {path}")
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=names, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def as_float(value: str) -> float | None:
    return None if value == "" else float(value)


def axis_transform(profile: dict) -> tuple[float, float]:
    first, last = profile["vertical_anchors"]
    slope = (last["signed_mm"] - first["signed_mm"]) / (
        last["pixel_y"] - first["pixel_y"]
    )
    intercept = first["signed_mm"] - slope * first["pixel_y"]
    return float(slope), float(intercept)


def normalize_rows(rows: list[dict[str, str]], profiles: dict[str, dict]) -> list[dict]:
    result = []
    for raw in rows:
        label = raw["profile_label"]
        if label not in profiles:
            raise ValueError(f"Unexpected profile: {label}")
        slope, intercept = axis_transform(profiles[label])
        pixel_y = as_float(raw["source_pixel_y"])
        value = as_float(raw["displacement_negative_down_mm"])
        calibrated = None if pixel_y is None else slope * pixel_y + intercept
        residual = None if value is None or calibrated is None else value - calibrated
        status = raw["status"]
        if (status == "unresolved_occlusion") != (value is None):
            raise ValueError(f"Resolution/value mismatch in profile {label}, row {raw['marker_index']}")
        if raw["eligible_as_t1_time_series"].strip().lower() != "false":
            raise ValueError("Published profile rows must remain ineligible as T1 time series")
        result.append(
            {
                "point_id": (
                    f"SUP01-P{label}-{raw['method'].upper()}-"
                    f"{raw['observation_interval_label'].replace('-', '_')}-"
                    f"{int(raw['marker_index']):03d}"
                ),
                "profile_label": label,
                "figure_order": int(raw["marker_index"]),
                "distance_label_as_printed_km": raw["distance_label_as_printed_km"],
                "distance_value_from_label_km": as_float(raw["distance_value_from_label_km"]),
                "method": raw["method"],
                "observation_interval_label": raw["observation_interval_label"],
                "source_pixel_x": as_float(raw["source_pixel_x"]),
                "source_pixel_y": pixel_y,
                "signed_displacement_mm": value,
                "axis_calibrated_signed_mm": None if calibrated is None else round(calibrated, 6),
                "digitization_minus_axis_calibration_mm": None if residual is None else round(residual, 6),
                "digitization_envelope_half_width_mm": as_float(
                    raw["digitization_uncertainty_mm_not_measurement_sigma"]
                ),
                "reading_status": status,
                "reading_envelope_interpretation": (
                    "digitization_bound_not_measurement_sigma_or_confidence_interval"
                ),
                "source_file": raw["source_file"],
                "source_sha256": raw["source_sha256"],
                "pdf_page": int(raw["pdf_page"]),
                "provenance": raw["provenance"],
                "eligible_as_t1_time_series": False,
                "mapped_synthetic_point_id": "",
            }
        )
    return result


def series_key(row: dict) -> tuple[str, str]:
    return row["method"], row["observation_interval_label"]


def profile_positions(rows: list[dict]) -> tuple[list[int], list[str]]:
    labels: dict[int, str] = {}
    for row in rows:
        labels.setdefault(row["figure_order"], row["distance_label_as_printed_km"])
    orders = sorted(labels)
    return orders, [labels[order] for order in orders]


def draw_overlay(ax, image: Image.Image, rows: list[dict]) -> None:
    ax.imshow(image)
    for row in rows:
        x, y = row["source_pixel_x"], row["source_pixel_y"]
        if y is None:
            ax.axvspan(x - 5, x + 5, facecolor="#D5D5D5", alpha=0.22)
            ax.text(x, 55, "?", ha="center", va="center", fontsize=7, color="#4A4A4A")
        elif row["reading_status"] == "partially_occluded_approximation":
            ax.scatter(
                [x], [y], s=34, facecolors="none", edgecolors="#CC8A24",
                marker="o", linewidths=1.0, zorder=4,
            )
        else:
            ax.scatter(
                [x], [y], s=18, facecolors="none", edgecolors="#202020",
                marker="o", linewidths=0.7, zorder=3,
            )
    ax.set_xlim(0, image.width)
    ax.set_ylim(image.height, 0)
    ax.axis("off")


def draw_reconstruction(
    ax, rows: list[dict], series_specs: dict[tuple[str, str], dict], profile: dict,
    *, compact: bool = False,
) -> None:
    orders, labels = profile_positions(rows)
    position = {order: index for index, order in enumerate(orders)}
    present = []
    for key, spec in series_specs.items():
        group = sorted((row for row in rows if series_key(row) == key), key=lambda row: row["figure_order"])
        if not group:
            continue
        present.append(spec["label_ru"])
        xs = np.array([position[row["figure_order"]] for row in group], dtype=float)
        ys = np.array([
            np.nan if row["signed_displacement_mm"] is None else row["signed_displacement_mm"]
            for row in group
        ])
        errs = np.array([
            0.0 if row["digitization_envelope_half_width_mm"] is None
            else row["digitization_envelope_half_width_mm"]
            for row in group
        ])
        face = spec["color"] if spec["filled"] and spec["marker"] != "x" else "white"
        ax.plot(
            xs, ys, color=spec["color"], linestyle=spec["line_style"], linewidth=1.35,
            marker=spec["marker"], markersize=4.5, markerfacecolor=face,
            markeredgecolor=spec["color"], label=spec["label_ru"], zorder=3,
        )
        finite = np.isfinite(ys)
        ax.errorbar(
            xs[finite], ys[finite], yerr=errs[finite], fmt="none", ecolor=spec["color"],
            alpha=0.32, elinewidth=0.65, capsize=1.5, zorder=2,
        )
        partial = [row for row in group if row["reading_status"] == "partially_occluded_approximation"]
        if partial:
            ax.scatter(
                [position[row["figure_order"]] for row in partial],
                [row["signed_displacement_mm"] for row in partial],
                s=45, facecolors="none", edgecolors="#CC8A24", linewidths=0.9,
                marker="o", zorder=5,
            )
    first, last = profile["vertical_anchors"]
    span = abs(first["signed_mm"] - last["signed_mm"])
    ax.set_ylim(min(first["signed_mm"], last["signed_mm"]) - span * 0.05,
                max(first["signed_mm"], last["signed_mm"]) + span * 0.08)
    ax.axhline(0, color="#696969", linewidth=0.7)
    step = 1 if len(orders) <= 16 else 2
    tick_positions = list(range(0, len(orders), step))
    ax.set_xticks(tick_positions, [labels[index] for index in tick_positions], rotation=45 if len(orders) > 18 else 0)
    ax.set_xlabel("Расстояние по напечатанной подписи, км")
    ax.set_ylabel("Оседание, мм (знак источника)")
    ax.set_title(f"Профильная линия {profile['profile_label']}", loc="left", fontsize=11)
    ax.grid(axis="y", color="#DDE1E5", linewidth=0.6)
    ax.spines[["top", "right"]].set_visible(False)
    if not compact:
        ax.legend(loc="best", frameon=False, fontsize=8, ncols=2)


def render_profile(
    output: Path, profile: dict, rows: list[dict], source: Image.Image,
    series_specs: dict[tuple[str, str], dict],
) -> list[str]:
    profile_dir = output / f"profile_{profile['profile_label']}"
    profile_dir.mkdir(parents=True, exist_ok=True)
    source_path = profile_dir / "source_fragment.png"
    source.save(source_path)

    fig, ax = plt.subplots(figsize=(9, 7.3))
    draw_overlay(ax, source, rows)
    fig.suptitle(
        f"Линия {profile['profile_label']}: наложение оцифровки",
        x=0.06, ha="left", fontsize=12,
    )
    fig.text(
        0.06, 0.025,
        "Чёрный круг: различимое значение; оранжевое кольцо: частичное перекрытие; серая полоса: значение не назначено.",
        fontsize=8.5,
    )
    fig.subplots_adjust(left=0.04, right=0.98, top=0.92, bottom=0.08)
    overlay_path = profile_dir / "source_overlay.png"
    fig.savefig(overlay_path, dpi=180)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(10.5, 5.8))
    draw_reconstruction(ax, rows, series_specs, profile)
    unresolved = sum(row["reading_status"] == "unresolved_occlusion" for row in rows)
    fig.text(
        0.08, 0.02,
        f"Оранжевые кольца: частично перекрытые значения. Пропусков без назначенного значения: {unresolved}.\n"
        "Полосы погрешности относятся к считыванию рисунка, а не к точности полевых измерений.",
        fontsize=8.5,
    )
    fig.subplots_adjust(left=0.08, right=0.98, top=0.91, bottom=0.22)
    reconstruction_path = profile_dir / "reconstructed_profile.png"
    fig.savefig(reconstruction_path, dpi=190)
    plt.close(fig)

    fig = plt.figure(figsize=(12, 14.5))
    grid = fig.add_gridspec(3, 1, height_ratios=[1, 1, 1.05], hspace=0.16)
    ax_source = fig.add_subplot(grid[0])
    ax_source.imshow(source)
    ax_source.axis("off")
    ax_source.set_title("А. Исходный встроенный растр", loc="left", fontsize=11)
    ax_overlay = fig.add_subplot(grid[1])
    draw_overlay(ax_overlay, source, rows)
    ax_overlay.set_title("Б. Наложение выбранных позиций", loc="left", fontsize=11)
    ax_rebuilt = fig.add_subplot(grid[2])
    draw_reconstruction(ax_rebuilt, rows, series_specs, profile)
    ax_rebuilt.set_title("В. Восстановленные значения в порядке категорий", loc="left", fontsize=11)
    fig.suptitle(
        f"Профильная линия {profile['profile_label']}: источник, оцифровка и восстановление",
        x=0.075, ha="left", fontsize=14,
    )
    fig.text(
        0.075, 0.012,
        "Ось X категориальная: сохранены порядок и напечатанные подписи. Геодезическая привязка к искусственной сети отсутствует.",
        fontsize=9,
    )
    comparison_path = profile_dir / "comparison.png"
    fig.savefig(comparison_path, dpi=180, bbox_inches="tight")
    plt.close(fig)

    point_fields = list(rows[0])
    write_rows(profile_dir / "points.csv", rows, point_fields)
    limitation = {
        "profile_label": profile["profile_label"],
        "source": "SUP01, geokniga-02obrabotka.pdf, page 15",
        "pdf_xobject": profile["pdf_xobject"],
        "horizontal_axis": "equally_spaced_categories_with_printed_distance_labels",
        "horizontal_numeric_affine_calibration_allowed": False,
        "vertical_axis": "linear calibration from printed grid anchors",
        "metric_georeferencing_accuracy_established": False,
        "source_profile_to_synthetic_id_mapping": None,
        "eligible_as_t1_time_series": False,
        "reading_status_counts": dict(Counter(row["reading_status"] for row in rows)),
        "limits": [
            "A published interval profile does not identify a temporal history.",
            "Printed distance labels and figure order are preserved; repeated or omitted labels are not repaired.",
            "Digitization envelopes are not field-measurement sigma or confidence intervals.",
            "No geographic correspondence ties the published line to synthetic P-H/P-V/P-D profiles.",
        ],
    }
    write_json(profile_dir / "limitations.json", limitation)
    return [
        source_path.relative_to(output).as_posix(),
        overlay_path.relative_to(output).as_posix(),
        reconstruction_path.relative_to(output).as_posix(),
        comparison_path.relative_to(output).as_posix(),
        (profile_dir / "points.csv").relative_to(output).as_posix(),
        (profile_dir / "limitations.json").relative_to(output).as_posix(),
    ]


def render_atlas(
    output: Path, profiles: list[dict], by_profile: dict[str, list[dict]],
    series_specs: dict[tuple[str, str], dict],
) -> str:
    fig, axes = plt.subplots(2, 2, figsize=(15, 11))
    for ax, profile in zip(axes.flat, profiles, strict=True):
        draw_reconstruction(ax, by_profile[profile["profile_label"]], series_specs, profile, compact=True)
    handles, labels = axes.flat[0].get_legend_handles_labels()
    if handles:
        fig.legend(handles, labels, loc="upper center", ncols=4, frameon=False, fontsize=9)
    fig.suptitle("Опубликованные профили Мусихина: воспроизведённая оцифровка", fontsize=15)
    fig.text(
        0.06, 0.015,
        "252 численных значения из 258 позиций. Оранжевые кольца отмечают частичное перекрытие; пропуски не интерполированы.\n"
        "Расстояния показаны в категориальном порядке исходника; профили не связаны с искусственной сетью проекта.",
        fontsize=9,
    )
    fig.subplots_adjust(left=0.07, right=0.98, top=0.9, bottom=0.1, hspace=0.34, wspace=0.18)
    path = output / "atlas.png"
    fig.savefig(path, dpi=190)
    plt.close(fig)
    return path.relative_to(output).as_posix()


def main() -> None:
    args = parse_args()
    root = args.root.resolve()
    config_path = relative_path(root, args.config)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    output = relative_path(root, args.output or config["output_directory"])

    verified = []
    source_spec = config["source_pdf"]
    source_pdf = relative_path(root, source_spec["path"])
    verified.append(verify_file(source_pdf, source_spec["sha256"], source_spec["size_bytes"]))
    digitization_spec = config["digitization"]
    digitization_path = relative_path(root, digitization_spec["path"])
    verified.append(
        verify_file(digitization_path, digitization_spec["sha256"], digitization_spec["size_bytes"])
    )
    linkage_spec = config["dataset_linkage_evidence"]
    linkage_path = relative_path(root, linkage_spec["path"])
    verified.append(verify_file(linkage_path, linkage_spec["sha256"], linkage_spec["size_bytes"]))
    linkage = json.loads(linkage_path.read_text(encoding="utf-8"))
    if linkage.get("status") != linkage_spec["required_status"]:
        raise ValueError("Dataset-linkage status changed")

    profiles = config["profiles"]
    profile_specs = {profile["profile_label"]: profile for profile in profiles}
    sources: dict[str, Image.Image] = {}
    for profile in profiles:
        path = relative_path(root, profile["source_image_path"])
        verified.append(
            verify_file(path, profile["source_image_sha256"], profile["source_image_size_bytes"])
        )
        image = Image.open(path).convert("RGB")
        if list(image.size) != profile["native_size_px"]:
            raise ValueError(f"Native image size changed for profile {profile['profile_label']}")
        sources[profile["profile_label"]] = image

    raw_rows = read_rows(digitization_path)
    rows = normalize_rows(raw_rows, profile_specs)
    if len(rows) != digitization_spec["expected_rows"]:
        raise ValueError("Unexpected digitization row count")
    status_counts = Counter(row["reading_status"] for row in rows)
    if dict(status_counts) != digitization_spec["expected_status_counts"]:
        raise ValueError(f"Status counts changed: {status_counts}")
    finite = sum(row["signed_displacement_mm"] is not None for row in rows)
    if finite != digitization_spec["expected_finite_values"]:
        raise ValueError("Unexpected finite-value count")

    by_profile = {label: [row for row in rows if row["profile_label"] == label] for label in profile_specs}
    for profile in profiles:
        if len(by_profile[profile["profile_label"]]) != profile["expected_rows"]:
            raise ValueError(f"Unexpected row count for profile {profile['profile_label']}")
        x0, y0, x1, y1 = profile["plot_bbox_native_px"]
        width, height = profile["native_size_px"]
        if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
            raise ValueError(f"Plot box is outside profile {profile['profile_label']}")
        for row in by_profile[profile["profile_label"]]:
            if not (0 <= row["source_pixel_x"] < width):
                raise ValueError("Digitized x coordinate is outside the source image")
            if row["source_pixel_y"] is not None and not (0 <= row["source_pixel_y"] < height):
                raise ValueError("Digitized y coordinate is outside the source image")

    residuals = [
        abs(row["digitization_minus_axis_calibration_mm"])
        for row in rows
        if row["digitization_minus_axis_calibration_mm"] is not None
    ]
    if max(residuals) > 0.1:
        raise ValueError("Stored values no longer agree with the independently recorded axis anchors")

    output.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9.5, "savefig.facecolor": "white"})
    series_specs = {
        (spec["method"], spec["interval"]): spec for spec in config["series"]
    }
    generated = []
    inventory = []
    for profile in profiles:
        label = profile["profile_label"]
        profile_rows = by_profile[label]
        generated.extend(render_profile(output, profile, profile_rows, sources[label], series_specs))
        profile_status = Counter(row["reading_status"] for row in profile_rows)
        values = [row["signed_displacement_mm"] for row in profile_rows if row["signed_displacement_mm"] is not None]
        inventory.append(
            {
                "profile_label": label,
                "pdf_page": source_spec["pdf_page"],
                "pdf_xobject": profile["pdf_xobject"],
                "candidate_positions": len(profile_rows),
                "finite_values": len(values),
                "clear_or_visible": profile_status.get("digitized_marker_or_visible_curve", 0),
                "partially_occluded": profile_status.get("partially_occluded_approximation", 0),
                "unresolved": profile_status.get("unresolved_occlusion", 0),
                "signed_min_mm": min(values),
                "signed_max_mm": max(values),
                "horizontal_axis": "categorical_printed_labels",
                "metric_georeferencing_accuracy_established": False,
                "eligible_as_t1_time_series": False,
            }
        )
    write_rows(output / "profile_inventory.csv", inventory)
    generated.append("profile_inventory.csv")
    unresolved = [row for row in rows if row["reading_status"] == "unresolved_occlusion"]
    write_rows(output / "unresolved_positions.csv", unresolved, list(rows[0]))
    generated.append("unresolved_positions.csv")

    axis_diagnostics = {
        profile["profile_label"]: {
            "vertical_anchors": profile["vertical_anchors"],
            "slope_mm_per_native_pixel": axis_transform(profile)[0],
            "intercept_mm": axis_transform(profile)[1],
            "maximum_absolute_saved_value_residual_mm": max(
                abs(row["digitization_minus_axis_calibration_mm"])
                for row in by_profile[profile["profile_label"]]
                if row["digitization_minus_axis_calibration_mm"] is not None
            ),
            "horizontal_numeric_affine_calibration_allowed": False,
        }
        for profile in profiles
    }
    write_json(output / "axis_diagnostics.json", axis_diagnostics)
    generated.append("axis_diagnostics.json")
    generated.append(render_atlas(output, profiles, by_profile, series_specs))

    validation = {
        "status": "PASS",
        "checks": {
            "source_pdf_hash_verified": True,
            "native_source_images_hash_verified": True,
            "digitization_input_hash_verified": True,
            "four_profiles_present": set(by_profile) == {"1", "5", "17", "6"},
            "candidate_and_finite_counts_match": len(rows) == 258 and finite == 252,
            "readability_counts_match": dict(status_counts) == digitization_spec["expected_status_counts"],
            "unresolved_values_not_interpolated": all(row["signed_displacement_mm"] is None for row in unresolved),
            "axis_calibration_reproduces_saved_values": max(residuals) <= 0.1,
            "categorical_axes_preserved": True,
            "no_synthetic_profile_mapping_invented": linkage["status"] == "NO_NUMERIC_PROFILE_LINK_FOUND",
            "not_a_time_series": all(not row["eligible_as_t1_time_series"] for row in rows),
        },
        "candidate_positions": len(rows),
        "finite_digitized_values": finite,
        "readability_counts": dict(status_counts),
        "maximum_absolute_axis_calibration_residual_mm": max(residuals),
        "dataset_linkage": linkage["status"],
        "models_trained": 0,
        "generator_executed": False,
        "new_holdout_opened": False,
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "matplotlib": matplotlib.__version__,
        },
    }
    if not all(validation["checks"].values()):
        validation["status"] = "FAIL"
    write_json(output / "validation_report.json", validation)
    generated.append("validation_report.json")

    readme = """# MUSIKHIN_PUBLISHED_PROFILES_V1

Четыре опубликованных пространственных профиля из дополнительного источника
SUP01, страница 15: линии 1, 5, 17 и 6. Сохранены исходные растры, наложения,
оцифрованные значения, пропуски, консервативные погрешности считывания и
ограничения интерпретации.

Воспроизведение:

```powershell
.\\.venv\\Scripts\\python.exe scripts\\reconstruct_musikhin_profiles.py --root .
```

Ось X в исходнике категориальная. Повторяющиеся и отсутствующие подписи не
исправляются. Эти профили описывают пространственные срезы за подписанные
интервалы и не являются временными рядами или журналами наблюдений. Связь с
искусственными профилями P-H/P-V/P-D не установлена.
"""
    (output / "README.md").write_text(readme, encoding="utf-8", newline="\n")
    generated.append("README.md")

    script_path = Path(__file__).resolve()
    input_items = [
        {"path": item["path"].relative_to(root).as_posix(), "sha256": item["sha256"], "size_bytes": item["size_bytes"]}
        for item in verified
    ]
    input_items.extend(
        [
            {"path": config_path.relative_to(root).as_posix(), "sha256": sha256(config_path), "size_bytes": config_path.stat().st_size},
            {"path": script_path.relative_to(root).as_posix(), "sha256": sha256(script_path), "size_bytes": script_path.stat().st_size},
        ]
    )
    output_items = [
        {"path": name, "sha256": sha256(output / name), "size_bytes": (output / name).stat().st_size}
        for name in sorted(set(generated))
    ]
    manifest = {
        "schema_version": 1,
        "artifact_id": config["artifact_id"],
        "source_data_commit": config["accepted_data_commit"],
        "command": "python scripts/reconstruct_musikhin_profiles.py --root .",
        "inputs": input_items,
        "outputs": output_items,
    }
    write_json(output / "manifest.json", manifest)
    print(json.dumps({
        "status": validation["status"],
        "profiles": len(profiles),
        "candidate_positions": len(rows),
        "finite_digitized_values": finite,
        "readability_counts": dict(status_counts),
        "dataset_linkage": linkage["status"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

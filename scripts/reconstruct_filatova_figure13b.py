#!/usr/bin/env python3
"""Reproduce and audit the red plan geometry in Filatova Figure 13b.

The output remains in source-raster coordinates. Threshold sensitivity is a
repeatability diagnostic, not a geodetic or field-accuracy estimate.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import platform
import zipfile

os.environ["MPLCONFIGDIR"] = str(
    Path(__file__).resolve().parents[1] / "work/reconstruction_atlas/matplotlib"
)
import cv2
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from PIL import Image


DEFAULT_CONFIG = "configs/filatova_figure13b_geometry_v1.json"


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


def verify_file(path: Path, spec: dict) -> dict:
    if not path.is_file():
        raise FileNotFoundError(path)
    actual_hash = sha256(path)
    actual_size = path.stat().st_size
    if actual_hash != spec["sha256"] or actual_size != spec["size_bytes"]:
        raise ValueError(f"Input integrity failure: {path}")
    return {"path": path, "sha256": actual_hash, "size_bytes": actual_size}


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def write_rows(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def extract_archive_member(docx: Path, member: str) -> bytes:
    with zipfile.ZipFile(docx) as archive:
        try:
            return archive.read(member)
        except KeyError as error:
            raise ValueError(f"DOCX member is missing: {member}") from error


def red_mask(crop: np.ndarray, thresholds: dict) -> np.ndarray:
    values = crop.astype(np.int16)
    r, g, b = values[:, :, 0], values[:, :, 1], values[:, :, 2]
    return (
        (r > thresholds["r_min"])
        & ((r - g) > thresholds["r_minus_g_min"])
        & ((r - b) > thresholds["r_minus_b_min"])
        & (g < thresholds["g_max"])
        & (b < thresholds["b_max"])
    ).astype(np.uint8)


def segment(crop: np.ndarray, config: dict) -> dict:
    raw = red_mask(crop, config["thresholds"])
    size = config["close_kernel_px"]
    lines = cv2.morphologyEx(
        raw,
        cv2.MORPH_CLOSE,
        np.ones((size, size), np.uint8),
        iterations=config["close_iterations"],
    )
    free = (1 - lines).astype(np.uint8)
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(free, connectivity=8)
    height, width = free.shape
    components = []
    for label_id in range(1, count):
        x, y, box_width, box_height, area = [int(value) for value in stats[label_id]]
        touches_edge = x <= 0 or y <= 0 or x + box_width >= width or y + box_height >= height
        if touches_edge or area <= config["minimum_component_area_px"]:
            continue
        component_mask = (labels == label_id).astype(np.uint8) * 255
        contours, _ = cv2.findContours(component_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            continue
        contour = max(contours, key=cv2.contourArea)
        polygon = cv2.approxPolyDP(
            contour, config["polygon_approximation_epsilon_px"], True
        )[:, 0, :]
        if len(polygon) < 3 or cv2.contourArea(polygon) <= 2.0:
            continue
        components.append(
            {
                "label_id": label_id,
                "area_px2": area,
                "centroid_x_px": float(centroids[label_id][0]),
                "centroid_y_px": float(centroids[label_id][1]),
                "bbox_x_px": x,
                "bbox_y_px": y,
                "bbox_width_px": box_width,
                "bbox_height_px": box_height,
                "perimeter_px": float(cv2.arcLength(polygon, True)),
                "polygon": polygon,
            }
        )
    return {
        "raw_red_mask": raw,
        "line_mask": lines,
        "labels": labels,
        "stats": stats,
        "centroids": centroids,
        "components": components,
    }


def best_matches(nominal: dict, variant: dict) -> dict[int, dict]:
    nominal_labels = nominal["labels"].astype(np.int64)
    variant_labels = variant["labels"].astype(np.int64)
    stride = int(variant_labels.max()) + 1
    joint = np.bincount(
        (nominal_labels.ravel() * stride + variant_labels.ravel()),
        minlength=(int(nominal_labels.max()) + 1) * stride,
    )
    allowed = {component["label_id"] for component in variant["components"]}
    variant_by_label = {component["label_id"]: component for component in variant["components"]}
    result = {}
    for component in nominal["components"]:
        label_id = component["label_id"]
        overlaps = joint[label_id * stride : (label_id + 1) * stride]
        candidates = [other for other in allowed if overlaps[other] > 0]
        if not candidates:
            result[label_id] = {"matched_label_id": None, "iou": 0.0, "centroid_shift_px": None}
            continue
        def iou(other: int) -> float:
            overlap = int(overlaps[other])
            other_area = int(variant["stats"][other, cv2.CC_STAT_AREA])
            return overlap / (component["area_px2"] + other_area - overlap)
        best = max(candidates, key=iou)
        matched = variant_by_label[best]
        shift = float(np.hypot(
            component["centroid_x_px"] - matched["centroid_x_px"],
            component["centroid_y_px"] - matched["centroid_y_px"],
        ))
        result[label_id] = {
            "matched_label_id": best,
            "iou": float(iou(best)),
            "centroid_shift_px": shift,
        }
    return result


def polygon_wkt(polygon: np.ndarray) -> str:
    points = [(float(x), float(y)) for x, y in polygon]
    if points[0] != points[-1]:
        points.append(points[0])
    return "POLYGON ((" + ", ".join(f"{x:.3f} {y:.3f}" for x, y in points) + "))"


def draw_polygons(ax, components: list[dict], crop: np.ndarray | None, *, stable_fill: bool) -> None:
    if crop is not None:
        ax.imshow(crop)
    else:
        ax.set_facecolor("#FBFBFA")
    for component in components:
        polygon = component["polygon"]
        closed = np.vstack([polygon, polygon[0]])
        color = "#2864A8" if component["stable"] else "#CC8A24"
        linestyle = "-" if component["stable"] else "--"
        if stable_fill and crop is None:
            ax.fill(polygon[:, 0], polygon[:, 1], color=color, alpha=0.06)
        ax.plot(closed[:, 0], closed[:, 1], color=color, linewidth=0.55, linestyle=linestyle)
    ax.set_xlim(0, 550)
    ax.set_ylim(505, 0)
    ax.set_aspect("equal")
    ax.set_xlabel("X исходного crop, px")
    ax.set_ylabel("Y исходного crop, px")


def render_figures(output: Path, source_image: Image.Image, crop: np.ndarray, components: list[dict]) -> list[str]:
    source_path = output / "source_figure13.png"
    source_image.save(source_path)
    crop_path = output / "source_plan_crop.png"
    Image.fromarray(crop).save(crop_path)

    fig, ax = plt.subplots(figsize=(10.7, 9.6))
    draw_polygons(ax, components, crop, stable_fill=False)
    ax.set_title("Рисунок 13б: наложение извлечённых замкнутых областей", loc="left", fontsize=12)
    fig.text(
        0.09, 0.025,
        "Синий контур: устойчив к двум пороговым вариантам; оранжевый пунктир: чувствителен к порогу.\n"
        "Координаты пиксельные. Метрическая и инженерная точность не установлены.",
        fontsize=9,
    )
    fig.subplots_adjust(left=0.09, right=0.98, top=0.92, bottom=0.12)
    overlay_path = output / "source_overlay.png"
    fig.savefig(overlay_path, dpi=190)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(10.7, 9.6))
    draw_polygons(ax, components, None, stable_fill=True)
    ax.set_title("Рисунок 13б: воспроизведённая пиксельная топология", loc="left", fontsize=12)
    fig.text(
        0.09, 0.025,
        "Области построены по красным линиям исходного растра. Фоновая карта и метрическая система не переносятся.",
        fontsize=9,
    )
    fig.subplots_adjust(left=0.09, right=0.98, top=0.92, bottom=0.1)
    reconstructed_path = output / "reconstructed_scheme.png"
    fig.savefig(reconstructed_path, dpi=190)
    plt.close(fig)

    fig, axes = plt.subplots(1, 3, figsize=(18, 7.1))
    axes[0].imshow(crop)
    axes[0].set_title("А. Исходный crop рисунка 13б", loc="left")
    axes[0].axis("off")
    draw_polygons(axes[1], components, crop, stable_fill=False)
    axes[1].set_title("Б. Наложение оцифровки", loc="left")
    draw_polygons(axes[2], components, None, stable_fill=True)
    axes[2].set_title("В. Восстановленная топология", loc="left")
    fig.suptitle("Филатова, рисунок 13б: источник, наложение и реконструкция", fontsize=15)
    fig.text(
        0.04, 0.025,
        "Проверяется воспроизводимость чтения красной схемы в пикселях. Результат не является исходным GIS-слоем или геодезической привязкой.",
        fontsize=9.5,
    )
    fig.subplots_adjust(left=0.035, right=0.99, top=0.88, bottom=0.11, wspace=0.18)
    comparison_path = output / "comparison.png"
    fig.savefig(comparison_path, dpi=190)
    plt.close(fig)
    return [path.name for path in (source_path, crop_path, overlay_path, reconstructed_path, comparison_path)]


def main() -> None:
    args = parse_args()
    root = args.root.resolve()
    config_path = relative_path(root, args.config)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    output = relative_path(root, args.output or config["output_directory"])

    verified = []
    for spec in config["inputs"]:
        path = relative_path(root, spec["path"])
        verified.append(verify_file(path, spec))
    source_docx = relative_path(root, config["inputs"][0]["path"])
    source_spec = config["source"]
    image_bytes = extract_archive_member(source_docx, source_spec["archive_member"])
    if len(image_bytes) != source_spec["archive_member_size_bytes"]:
        raise ValueError("Figure 13 source image size changed")
    if hashlib.sha256(image_bytes).hexdigest() != source_spec["archive_member_sha256"]:
        raise ValueError("Figure 13 source image hash changed")

    from io import BytesIO

    source_image = Image.open(BytesIO(image_bytes)).convert("RGB")
    source_array = np.asarray(source_image)
    x0, y0, x1, y1 = source_spec["crop_bbox_native_px"]
    height, width = source_array.shape[:2]
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise ValueError("Configured Figure 13b crop is outside the source image")
    crop = source_array[y0:y1, x0:x1]

    segmentation = config["segmentation"]
    variants = {}
    for name, thresholds in segmentation["variants"].items():
        variants[name] = segment(
            crop,
            {
                "thresholds": thresholds,
                "close_kernel_px": segmentation["close_kernel_px"],
                "close_iterations": segmentation["close_iterations"],
                "minimum_component_area_px": segmentation["minimum_component_area_px"],
                "polygon_approximation_epsilon_px": segmentation["polygon_approximation_epsilon_px"],
            },
        )
    nominal = variants["nominal"]
    strict_matches = best_matches(nominal, variants["strict"])
    lenient_matches = best_matches(nominal, variants["lenient"])
    stable_threshold = segmentation["stable_iou_min"]

    unit_rows = []
    sensitivity_rows = []
    for index, component in enumerate(nominal["components"], start=1):
        strict = strict_matches[component["label_id"]]
        lenient = lenient_matches[component["label_id"]]
        stable = strict["iou"] >= stable_threshold and lenient["iou"] >= stable_threshold
        component["stable"] = stable
        unit_id = f"F13B-PX-{index:04d}"
        unit_rows.append(
            {
                "unit_id": unit_id,
                "source_component_label": component["label_id"],
                "area_px2": component["area_px2"],
                "perimeter_px": round(component["perimeter_px"], 6),
                "centroid_x_crop_px": round(component["centroid_x_px"], 6),
                "centroid_y_crop_px": round(component["centroid_y_px"], 6),
                "centroid_x_source_px": round(component["centroid_x_px"] + x0, 6),
                "centroid_y_source_px": round(component["centroid_y_px"] + y0, 6),
                "bbox_x_crop_px": component["bbox_x_px"],
                "bbox_y_crop_px": component["bbox_y_px"],
                "bbox_width_px": component["bbox_width_px"],
                "bbox_height_px": component["bbox_height_px"],
                "polygon_wkt_crop_px": polygon_wkt(component["polygon"]),
                "threshold_stability": "stable" if stable else "threshold_sensitive",
                "coordinate_system": source_spec["coordinate_system"],
                "metric_georeferencing_accuracy_established": False,
                "provenance": "D_published_figure_segmentation",
            }
        )
        sensitivity_rows.append(
            {
                "unit_id": unit_id,
                "strict_iou": round(strict["iou"], 6),
                "strict_centroid_shift_px": None if strict["centroid_shift_px"] is None else round(strict["centroid_shift_px"], 6),
                "lenient_iou": round(lenient["iou"], 6),
                "lenient_centroid_shift_px": None if lenient["centroid_shift_px"] is None else round(lenient["centroid_shift_px"], 6),
                "classification": "stable" if stable else "threshold_sensitive",
                "interpretation": "color-threshold repeatability_not_geodetic_accuracy",
            }
        )

    parent_plan_path = relative_path(root, config["inputs"][2]["path"])
    with parent_plan_path.open(encoding="utf-8-sig", newline="") as handle:
        parent_count = sum(1 for _ in csv.DictReader(handle))
    nominal_count = len(unit_rows)
    if nominal_count != segmentation["expected_nominal_regions"]:
        raise ValueError(f"Nominal region count changed: {nominal_count}")

    output.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9.5, "savefig.facecolor": "white"})
    generated = render_figures(output, source_image, crop, nominal["components"])
    unit_fields = list(unit_rows[0])
    write_rows(output / "plan_units_pixel.csv", unit_rows, unit_fields)
    generated.append("plan_units_pixel.csv")
    sensitivity_fields = list(sensitivity_rows[0])
    write_rows(output / "threshold_sensitivity.csv", sensitivity_rows, sensitivity_fields)
    generated.append("threshold_sensitivity.csv")
    uncertain = [row for row in sensitivity_rows if row["classification"] == "threshold_sensitive"]
    write_rows(output / "unreadable_regions.csv", uncertain, sensitivity_fields)
    generated.append("unreadable_regions.csv")

    stable_count = nominal_count - len(uncertain)
    strict_ious = [row["strict_iou"] for row in sensitivity_rows]
    lenient_ious = [row["lenient_iou"] for row in sensitivity_rows]
    strict_shifts = [row["strict_centroid_shift_px"] for row in sensitivity_rows if row["strict_centroid_shift_px"] is not None]
    lenient_shifts = [row["lenient_centroid_shift_px"] for row in sensitivity_rows if row["lenient_centroid_shift_px"] is not None]
    sensitivity = {
        "interpretation": "method sensitivity in source pixels; not field or geodetic accuracy",
        "component_counts": {name: len(value["components"]) for name, value in variants.items()},
        "line_mask_iou": {
            "strict_vs_nominal": float(
                np.logical_and(variants["strict"]["line_mask"], nominal["line_mask"]).sum()
                / np.logical_or(variants["strict"]["line_mask"], nominal["line_mask"]).sum()
            ),
            "lenient_vs_nominal": float(
                np.logical_and(variants["lenient"]["line_mask"], nominal["line_mask"]).sum()
                / np.logical_or(variants["lenient"]["line_mask"], nominal["line_mask"]).sum()
            ),
        },
        "stable_iou_min": stable_threshold,
        "stable_nominal_regions": stable_count,
        "threshold_sensitive_nominal_regions": len(uncertain),
        "strict_unit_iou_median": float(np.median(strict_ious)),
        "strict_unit_iou_min": float(min(strict_ious)),
        "lenient_unit_iou_median": float(np.median(lenient_ious)),
        "lenient_unit_iou_min": float(min(lenient_ious)),
        "strict_centroid_shift_px_median": float(np.median(strict_shifts)) if strict_shifts else None,
        "strict_centroid_shift_px_max": float(max(strict_shifts)) if strict_shifts else None,
        "lenient_centroid_shift_px_median": float(np.median(lenient_shifts)) if lenient_shifts else None,
        "lenient_centroid_shift_px_max": float(max(lenient_shifts)) if lenient_shifts else None,
    }
    write_json(output / "method_sensitivity.json", sensitivity)
    generated.append("method_sensitivity.json")

    limitations = {
        "source": "ВКР_Филатова_М_С.docx",
        "source_location": source_spec["source_location"],
        "reconstructed_object": "closed regions bounded by the red plan overlay",
        "coordinate_system": source_spec["coordinate_system"],
        "metric_georeferencing_accuracy_established": False,
        "original_mapinfo_or_gis_available": False,
        "background_map_role": "visual context only",
        "unreadable_or_sensitive_region_count": len(uncertain),
        "limits": [
            "The 538 regions reproduce the nominal segmentation topology; they are not 538 independent measurements.",
            "Threshold sensitivity measures image-processing repeatability only.",
            "No official coordinate key or independent control points establish metre-level accuracy.",
            "Labels, symbols and map background may interrupt or merge red linework.",
            "The source figure cannot recover the original 1665-row, 257-field GIS table.",
        ],
    }
    write_json(output / "limitations.json", limitations)
    generated.append("limitations.json")

    validation = {
        "status": "PASS",
        "checks": {
            "all_inputs_hash_verified": True,
            "embedded_source_hash_verified": True,
            "configured_crop_inside_source": True,
            "nominal_region_count_matches_frozen_parent": nominal_count == parent_count == 538,
            "pixel_polygons_are_nonempty": all(row["area_px2"] > 20 for row in unit_rows),
            "threshold_sensitivity_recorded": len(sensitivity_rows) == nominal_count,
            "uncertain_regions_explicit": len(uncertain) + stable_count == nominal_count,
            "no_metric_accuracy_claim": all(not row["metric_georeferencing_accuracy_established"] for row in unit_rows),
            "no_model_training": True,
        },
        "source_image_size_px": list(source_image.size),
        "crop_bbox_native_px": source_spec["crop_bbox_native_px"],
        "nominal_regions": nominal_count,
        "frozen_parent_regions": parent_count,
        "stable_regions": stable_count,
        "threshold_sensitive_regions": len(uncertain),
        "metric_georeferencing_accuracy_established": False,
        "models_trained": 0,
        "generator_executed": False,
        "new_holdout_opened": False,
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "opencv": cv2.__version__,
            "matplotlib": matplotlib.__version__,
        },
    }
    if not all(validation["checks"].values()):
        validation["status"] = "FAIL"
    write_json(output / "validation_report.json", validation)
    generated.append("validation_report.json")

    readme = f"""# FILATOVA_FIGURE13B_GEOMETRY_V1

Проверяемое воспроизведение красной плановой схемы на рисунке 13б ВКР
Филатовой. Номинальный алгоритм повторно выделяет {nominal_count} замкнутых
областей, как и замороженная родительская реконструкция.

Воспроизведение:

```powershell
.\\.venv\\Scripts\\python.exe scripts\\reconstruct_filatova_figure13b.py --root .
```

Координаты результатов остаются пиксельными. Пороговая чувствительность
показывает повторяемость сегментации, но не является геодезической или полевой
точностью. Исходные MapInfo/GIS-слои и официальный ключ координат отсутствуют.
"""
    (output / "README.md").write_text(readme, encoding="utf-8", newline="\n")
    generated.append("README.md")

    script_path = Path(__file__).resolve()
    inputs = [
        {"path": item["path"].relative_to(root).as_posix(), "sha256": item["sha256"], "size_bytes": item["size_bytes"]}
        for item in verified
    ]
    inputs.extend(
        [
            {"path": config_path.relative_to(root).as_posix(), "sha256": sha256(config_path), "size_bytes": config_path.stat().st_size},
            {"path": script_path.relative_to(root).as_posix(), "sha256": sha256(script_path), "size_bytes": script_path.stat().st_size},
        ]
    )
    outputs = [
        {"path": name, "sha256": sha256(output / name), "size_bytes": (output / name).stat().st_size}
        for name in sorted(set(generated))
    ]
    write_json(
        output / "manifest.json",
        {
            "schema_version": 1,
            "artifact_id": config["artifact_id"],
            "source_data_commit": config["accepted_data_commit"],
            "command": "python scripts/reconstruct_filatova_figure13b.py --root .",
            "inputs": inputs,
            "outputs": outputs,
        },
    )
    print(json.dumps({
        "status": validation["status"],
        "nominal_regions": nominal_count,
        "stable_regions": stable_count,
        "threshold_sensitive_regions": len(uncertain),
        "metric_georeferencing_accuracy_established": False,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

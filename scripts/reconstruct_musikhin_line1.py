#!/usr/bin/env python3
"""Reproduce one published spatial profile and its reading audit, without fitting models.

The native raster and initial handoff rows are frozen inputs. A second reading
uses a fresh Poppler rendering of the original PDF and a separate calibration.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import shutil
import subprocess

os.environ["MPLCONFIGDIR"] = str(Path(__file__).resolve().parents[1] / "work/profile_line1/matplotlib")
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from PIL import Image

CONFIG = "configs/musikhin_line1_digitization_v1.json"


def sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def read_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def write_rows(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def relative_path(root: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or not (root / path).resolve().is_relative_to(root):
        raise ValueError(f"Path must remain inside the repository: {value}")
    return root / path


def blue_mask(rgb: np.ndarray, parameters: dict) -> np.ndarray:
    data = np.asarray(rgb, dtype=np.int16)
    return ((data[..., 2] > parameters["minimum_B"])
            & (data[..., 2] - data[..., 0] > parameters["minimum_B_minus_R"])
            & (data[..., 1] - data[..., 0] > parameters["minimum_G_minus_R"]))


def longest_run(values: np.ndarray) -> np.ndarray:
    if not len(values):
        raise ValueError("No independently readable blue pixels")
    return max(np.split(values, np.where(np.diff(values) > 1)[0] + 1), key=len)


def primary_reading(config: dict, native: Image.Image, initial: list[dict]):
    first, last = config["axis"]["vertical_anchors"]
    scale = (last["signed_mm"] - first["signed_mm"]) / (last["pixel_y"] - first["pixel_y"])
    intercept = first["signed_mm"] - scale * first["pixel_y"]
    mask = blue_mask(np.asarray(native), config["repeat_reading"]["blue_mask"])
    widths = []
    points = {row["figure_order"]: row for row in config["points"]}
    for order in config["reading_error"]["marker_diameter_measurement_orders"]:
        x = points[order]["selected_pixel_x"]
        run = longest_run(np.flatnonzero(mask[120:420, x]) + 120)
        widths.append({"figure_order": order, "pixel_x": x, "top_pixel_y": int(run[0]),
                       "bottom_pixel_y": int(run[-1]), "diameter_px": len(run)})
    radius = float(np.median([row["diameter_px"] for row in widths])) / 2
    if radius * 2 != config["reading_error"]["expected_marker_vertical_diameter_px"]:
        raise ValueError("Marker thickness differs from reviewed source")
    base_envelope = radius + config["reading_error"]["pixel_quantization_half_width_px"] + config["reading_error"]["axis_anchor_half_width_px"]
    by_order = {int(row["marker_index"]): row for row in initial}
    result = []
    for point in config["points"]:
        order = point["figure_order"]
        original = by_order[order]
        y = point["selected_pixel_y"]
        partial = point["readability"] == "partially_occluded_approximation"
        value = round(scale * y + intercept, 1) if y is not None else None
        error_px = base_envelope + (config["reading_error"]["additional_occlusion_half_width_px"] if partial else 0)
        envelope = math.ceil(abs(scale) * error_px * 10) / 10 if y is not None else None
        result.append({"point_id": f"SUP01-L1-LVL-2011_2016-{order:02d}", "figure_order": order,
                       "distance_label_as_printed": point["distance_label_as_printed"],
                       "distance_from_label_km": float(point["distance_label_as_printed"]),
                       "signed_subsidence_mm": value, "selected_pixel_x": point["selected_pixel_x"],
                       "selected_pixel_y": y, "pixel_coordinate_system": config["source"]["pixel_coordinate_system"],
                       "pixel_selection_kind": "horizontal_slot_only" if y is None else "estimated_occluded_center" if partial else "visible_center",
                       "readability": point["readability"], "reading_envelope_half_width_mm": envelope,
                       "reading_envelope_interpretation": "conservative_digitization_bound_not_measurement_sigma_or_95pct_interval",
                       "pdf_page": 15, "pdf_xobject": "/Image157", "series": "leveling", "interval_label": "2011-2016",
                       "provenance": "D_published_graph", "source_available_at": "",
                       "eligible_as_t1_time_series": False, "mapped_synthetic_point_id": "",
                       "handoff_pixel_x": original["source_pixel_x"], "handoff_pixel_y": original["source_pixel_y"],
                       "handoff_signed_mm": original["displacement_negative_down_mm"], "handoff_status": original["status"],
                       "handoff_distance_label": original["distance_label_as_printed_km"],
                       "difference_from_handoff_mm": round(value - float(original["displacement_negative_down_mm"]), 1)
                       if value is not None and original["displacement_negative_down_mm"] else None,
                       "selection_note_ru": point["selection_note_ru"]})
    xs = np.array([row["selected_pixel_x"] for row in result], dtype=float)
    ds = np.array([row["distance_from_label_km"] for row in result])
    predicted_numeric_x = xs[0] + (ds - ds[0]) * (xs[-1] - xs[0]) / (ds[-1] - ds[0])
    axis = {"horizontal_axis": config["axis"]["horizontal_type"],
            "printed_labels_in_figure_order": [row["distance_label_as_printed"] for row in result],
            "adjacent_pixel_gaps": np.diff(xs).tolist(), "adjacent_label_gaps_km": np.round(np.diff(ds), 3).tolist(),
            "first_gap_px": float(xs[1] - xs[0]), "median_gap_px": float(np.median(np.diff(xs))),
            "max_error_if_numeric_affine_x_used_px": float(np.max(np.abs(xs - predicted_numeric_x))),
            "numeric_affine_x_rejected": True, "metric_georeferencing_accuracy_established": False,
            "millimetres_per_native_y_pixel": scale, "vertical_intercept_mm": intercept,
            "vertical_grid_residuals_mm": [round(scale * row["pixel_y"] + intercept - row["signed_mm"], 4)
                                           for row in config["axis"]["grid_ticks"]],
            "marker_diameters": widths, "marker_half_height_px": radius,
            "reading_error_interpretation": config["reading_error"]["interpretation"]}
    return result, axis


def independent_repeat(root: Path, config: dict, points: list[dict], scratch: Path):
    executable = shutil.which("pdftoppm")
    if not executable:
        raise RuntimeError("Poppler pdftoppm is required for the independent PDF reading")
    dpi = config["repeat_reading"]["dpi"]
    prefix = scratch / "repeat_page15"
    command = [executable, "-f", "15", "-l", "15", "-singlefile", "-r", str(dpi), "-png",
               str(root / config["inputs"][0]["path"]), str(prefix)]
    process = subprocess.run(command, check=True, capture_output=True)
    (scratch / "poppler_render.log").write_bytes(process.stdout + process.stderr)
    rendered = np.asarray(Image.open(prefix.with_suffix(".png")).convert("RGB"), dtype=np.int16)
    width, _, _, height, left, bottom = config["source"]["pdf_placement_points"]
    factor = dpi / 72
    page_height = config["source"]["pdf_page_size_points"][1]
    top = (page_height - bottom - height) * factor
    left *= factor
    sx, sy = width * factor / 900, height * factor / 731
    grid = []
    for tick in config["axis"]["grid_ticks"]:
        guess = round(top + tick["pixel_y"] * sy)
        candidates = list(range(guess - 4, guess + 5))
        scores = []
        for y in candidates:
            row = rendered[y, int(left + 120 * sx):int(left + 865 * sx)]
            scores.append(int(((row.max(axis=1) - row.min(axis=1) < 6) & (row.mean(axis=1) < 225)).sum()))
        grid.append({"rendered_pixel_y": candidates[int(np.argmax(scores))], "signed_mm": tick["signed_mm"]})
    slope, intercept = np.polyfit([row["rendered_pixel_y"] for row in grid], [row["signed_mm"] for row in grid], 1)
    mask = blue_mask(rendered, config["repeat_reading"]["blue_mask"])
    by_order = {row["figure_order"]: row for row in points}
    readings = []
    for order in config["repeat_reading"]["orders"]:
        primary = by_order[order]
        x = round(left + primary["selected_pixel_x"] * sx)
        # Search the complete plot band; the primary y value is not a search seed.
        y0, y1 = int(top + 110 * sy), int(top + 420 * sy)
        candidates = []
        for column in range(x - 2, x + 3):
            run = longest_run(np.flatnonzero(mask[y0:y1, column]) + y0)
            candidates.append((len(run), column, float((run[0] + run[-1]) / 2)))
        span, px, py = max(candidates, key=lambda item: item[0])
        value = float(slope * py + intercept)
        readings.append({"figure_order": order, "distance_label_as_printed": primary["distance_label_as_printed"],
                         "repeat_render_dpi": dpi, "repeat_pixel_x": px, "repeat_pixel_y": py,
                         "blue_vertical_span_render_pixels": span, "repeat_signed_mm": round(value, 3),
                         "primary_signed_mm": primary["signed_subsidence_mm"],
                         "repeat_minus_primary_mm": round(value - primary["signed_subsidence_mm"], 3),
                         "method": "independent_PDF_render_blue_span_midpoint_and_nine_gridlines"})
    return readings, {"gridline_calibration": grid, "mm_per_rendered_y_pixel": float(slope),
                      "intercept_mm": float(intercept), "method": config["repeat_reading"]["method"],
                      "operator_independence": config["repeat_reading"]["operator_independence"]}


def linkage_evidence(root: Path, config: dict) -> dict:
    raw = "SKRU1_ACTUAL_DATA_TABLES_v1/01_reconstruction_v3_2/"
    source = json.loads((root / raw / "metadata/reconstruction_config.json").read_text(encoding="utf-8"))
    lineage = read_rows(root / "data/reconstruction_research_v1/point_feature_lineage.csv")
    registry = read_rows(root / raw / "tables/source_registry.csv")
    profiles = read_rows(root / raw / "tables/survey_profiles.csv")
    campaigns = read_rows(root / raw / "tables/survey_campaigns.csv")
    scripts = [raw + "reproduce_v3.py", raw + "reproduce_v3_2.py"]
    names = ["geokniga", "musikhin", "мусихин", "sup01", "musikhin_p15_digitized_profiles"]
    hits = [{"path": rel, "token": name} for rel in scripts for name in names
            if name in (root / rel).read_text(encoding="utf-8").lower()]
    return {"status": "NO_NUMERIC_PROFILE_LINK_FOUND", "scope": "Available generation code, parent source registry, repaired lineage and release input paths",
            "generation_source_docx": source["source_docx"], "generation_source_docx_sha256": source["source_docx_sha256"],
            "published_profile_references_in_generation_scripts": hits,
            "parent_source_registry_contains_SUP01_or_geokniga": any("geokniga" in str(row).lower() or "SUP01" in str(row) for row in registry),
            "repaired_lineage_source_ids": sorted({row["source_id"] for row in lineage if row["source_id"]}),
            "synthetic_profile_ids": [row["profile_id"] for row in profiles],
            "synthetic_campaign_date_min": min(row["date"] for row in campaigns),
            "synthetic_campaign_date_max": max(row["date"] for row in campaigns),
            "source_profile_to_synthetic_id_mapping": None,
            "supplementary_pdf_role_in_repair": "File integrity verification only; no profile values are parsed into the seven parent tables",
            "evidence": [{"path": raw + "reproduce_v3.py", "lines": [815, 846], "meaning": "Constructed P-H/P-V/P-D lines, nominal spacing and synthetic point identifiers"},
                         {"path": raw + "reproduce_v3.py", "lines": [1331, 1349], "meaning": "Filatova DOCX media and published anchors feed the reconstructed grid and designed network"},
                         {"path": raw + "reproduce_v3_2.py", "lines": [1954, 1966], "meaning": "Inherited point/grid tables feed temporal generation and measurement simulation"},
                         {"path": raw + "reproduce_v3_2.py", "lines": [173, 330], "meaning": "Scenario dynamics and a 2022 map anchor; not a 2011-2016 leveling-history import"},
                         {"path": "src/skru1/reconstruction_data.py", "lines": [302, 336], "meaning": "Seven repair-table inputs and separate source hash verification"}],
            "limits": "No geographic correspondences or original benchmark IDs tie Line 1 to this network. Absence of a numeric link does not erase its separately catalogued documentary role."}


def draw_reconstruction(ax, points: list[dict], palette: dict):
    x = np.array([row["distance_from_label_km"] for row in points])
    y = np.array([row["signed_subsidence_mm"] if row["signed_subsidence_mm"] is not None else np.nan for row in points])
    ax.plot(x, y, color="#6A7887", linewidth=1.3, zorder=1)
    for state, marker, color, label in [("clear_marker_center", "D", palette["clear"], "Центр различим"),
                                       ("partially_occluded_approximation", "^", palette["approximate"], "Частичное перекрытие")]:
        selected = [row for row in points if row["readability"] == state]
        ax.errorbar([row["distance_from_label_km"] for row in selected], [row["signed_subsidence_mm"] for row in selected],
                    yerr=[row["reading_envelope_half_width_mm"] for row in selected], fmt=marker, linestyle="none",
                    color=color, markerfacecolor=color if marker == "D" else "white", markersize=6,
                    elinewidth=1, capsize=3, label=label, zorder=3)
    ax.axhline(0, color="#777777", linewidth=.8)
    ax.set_xticks(x, [row["distance_label_as_printed"] for row in points])
    ax.set(xlim=(-.04, 1.45), ylim=(-405, 35), xlabel="Расстояние по напечатанной подписи, км", ylabel="Оседание, мм (знак источника)")
    ax.grid(axis="y", color="#DDE1E5", linewidth=.6)
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(loc="lower left", frameon=False, ncols=2, fontsize=9)
    ax.annotate("№ 1-3: центры закрыты, значения не назначены", xy=(.03, .865), xycoords="axes fraction", fontsize=8.5, ha="left", color="#555555")
    ax.set_title("Нивелирование, 2011-2016: восстановленный пространственный профиль", fontsize=11, loc="left", pad=12)


def draw_overlay(ax, native: Image.Image, points: list[dict]):
    ax.imshow(native)
    for row in points:
        x, y = row["selected_pixel_x"], row["selected_pixel_y"]
        if y is None:
            ax.axvspan(x - 13, x + 13, ymin=.37, ymax=.9, facecolor="#DADADA", alpha=.25)
            ax.text(x, 56, f'{row["figure_order"]}?', ha="center", fontsize=8, color="#444444")
        else:
            partial = row["readability"] == "partially_occluded_approximation"
            ax.scatter([x], [y], marker="^" if partial else "o", s=42, facecolors="none", edgecolors="#141414", linewidths=.9)
            ax.text(x + 9, y - 17, str(row["figure_order"]), fontsize=8, color="black")
    ax.set_xlim(0, 900)
    ax.set_ylim(731, 0)
    ax.axis("off")


def figures(output: Path, config: dict, native: Image.Image, points: list[dict]):
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "savefig.facecolor": "white"})
    palette = config["chart_contract"]["palette"]
    native.save(output / "source_fragment.png")
    fig, ax = plt.subplots(figsize=(10.8, 5.7))
    draw_reconstruction(ax, points, palette)
    fig.text(.08, .018, "Полосы: консервативная погрешность считывания. Первые три значения не заполнены.\nОсь X построена по подписям; в исходнике категории равноотстоящие. Геопривязка не установлена.", fontsize=9)
    fig.subplots_adjust(left=.08, right=.98, top=.88, bottom=.22)
    fig.savefig(output / "reconstructed_profile.png", dpi=200)
    plt.close(fig)
    fig, ax = plt.subplots(figsize=(9, 7.5))
    draw_overlay(ax, native, points)
    ax.set_title("Выбранные центры: кружки — различимые, треугольники — приблизительные", fontsize=9)
    fig.subplots_adjust(left=.01, right=.99, bottom=.01, top=.95)
    fig.savefig(output / "source_overlay.png", dpi=200)
    plt.close(fig)
    fig = plt.figure(figsize=(14, 12.8), layout=None)
    grid = fig.add_gridspec(2, 2, height_ratios=[1.1, .83], left=.06, right=.98, bottom=.17, top=.90, wspace=.06, hspace=.22)
    ax = fig.add_subplot(grid[0, 0]); ax.imshow(native); ax.axis("off")
    ax.set_title("A. Исходный фрагмент PDF, с. 15", fontsize=12, loc="left", pad=8)
    ax = fig.add_subplot(grid[0, 1]); draw_overlay(ax, native, points)
    ax.set_title("Б. Выбранные центры и порядок точек", fontsize=12, loc="left", pad=8)
    draw_reconstruction(fig.add_subplot(grid[1, :]), points, palette)
    fig.suptitle("Линия 1: проверка опубликованного профиля нивелирования", x=.06, ha="left", y=.965, fontsize=17)
    fig.text(.06, .932, "SUP01, Мусихин • интервал 2011-2016 • 14 позиций: 7 различимых, 4 приблизительных, 3 без значения", fontsize=10)
    fig.text(.06, .035, "В исходнике X — равноотстоящие категории 0, 0.2, 0.3 … 1.4 км; на нижнем графике используются сами подписи расстояний.\nПогрешность считывания: ±14.4 мм для различимых центров, ±20.3 мм при перекрытии. Это не точность нивелирования.\nОдин пространственный профиль за интервал не восстанавливает временную историю измерений.", fontsize=9.5, linespacing=1.5)
    fig.savefig(output / "comparison.png", dpi=180)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", help="Default artifact directory or a work/profile_line1 subdirectory")
    args = parser.parse_args()
    root = args.root.resolve()
    config = json.loads((root / CONFIG).read_text(encoding="utf-8"))
    verified = []
    for entry in config["inputs"] + config["trace_inputs"]:
        path = relative_path(root, entry["path"])
        if path.stat().st_size != entry["size_bytes"] or sha256(path) != entry["sha256"]:
            raise ValueError(f"Input changed before transformation: {entry['path']}")
        verified.append(entry)
    output = relative_path(root, args.output or config["output_directory"])
    if output != root / config["output_directory"] and not output.resolve().is_relative_to((root / "work/profile_line1").resolve()):
        raise ValueError("Output must be the declared artifact or work/profile_line1")
    frozen_manifest = json.loads((root / "data/reconstruction_research_v1/manifest.json").read_text(encoding="utf-8"))
    frozen_hashes = {row["path"]: sha256(root / "data/reconstruction_research_v1" / row["path"]) for row in frozen_manifest["outputs"]}
    native = Image.open(root / config["inputs"][1]["path"]).convert("RGB")
    if native.size != (900, 731):
        raise ValueError("Native raster geometry changed")
    initial = read_rows(root / config["inputs"][2]["path"])
    if len(initial) != 14 or [int(row["marker_index"]) for row in initial] != list(range(1, 15)):
        raise ValueError("Handoff selection must contain all 14 ordered positions")
    points, axis = primary_reading(config, native, initial)
    scratch = root / "work/profile_line1/render_cache"
    scratch.mkdir(parents=True, exist_ok=True)
    repeated, repeat_calibration = independent_repeat(root, config, points, scratch)
    linkage = linkage_evidence(root, config)
    status_counts = {name: sum(row["readability"] == name for row in points)
                     for name in ("clear_marker_center", "partially_occluded_approximation", "unresolved_occlusion")}
    max_repeat = max(abs(row["repeat_minus_primary_mm"]) for row in repeated)
    checks = {"fourteen_ordered_positions": len(points) == 14,
              "readability_counts": status_counts == {"clear_marker_center": 7, "partially_occluded_approximation": 4, "unresolved_occlusion": 3},
              "signed_values_preserved": all(row["signed_subsidence_mm"] is None or row["signed_subsidence_mm"] < 0 for row in points),
              "unresolved_not_interpolated": all(row["selected_pixel_y"] is None and row["signed_subsidence_mm"] is None for row in points[:3]),
              "native_pixel_coordinates_inside_image": all(0 <= row["selected_pixel_x"] < 900 and (row["selected_pixel_y"] is None or 0 <= row["selected_pixel_y"] < 731) for row in points),
              "nonmetric_source_x_recognized": axis["max_error_if_numeric_affine_x_used_px"] > 40,
              "repeat_reading_within_two_native_pixels": max_repeat <= config["repeat_reading"]["max_difference_mm"],
              "no_numeric_profile_link_in_available_generator": not linkage["published_profile_references_in_generation_scripts"] and linkage["repaired_lineage_source_ids"] == ["SRC01"],
              "no_invented_date_or_synthetic_id": all(not row["source_available_at"] and not row["mapped_synthetic_point_id"] for row in points),
              "not_a_time_series": all(row["eligible_as_t1_time_series"] is False for row in points)}
    if not all(checks.values()):
        raise ValueError(f"Digitization checks failed: {checks}")
    output.mkdir(parents=True, exist_ok=True)
    write_rows(output / "points.csv", points)
    write_rows(output / "repeat_readings.csv", repeated)
    write_json(output / "axis_diagnostics.json", axis)
    write_json(output / "repeat_calibration.json", repeat_calibration)
    write_json(output / "dataset_linkage.json", linkage)
    figures(output, config, native, points)
    checks["accepted_repaired_tables_unchanged"] = all(sha256(root / "data/reconstruction_research_v1" / name) == value for name, value in frozen_hashes.items())
    if not checks["accepted_repaired_tables_unchanged"]:
        raise AssertionError("Protected release changed")
    validation = {"status": "PASS", "checks": checks, "inputs_verified": len(verified), "readability_counts": status_counts,
                  "numerically_reconstructed_positions": 11, "independent_repeat_count": len(repeated),
                  "max_abs_repeat_difference_mm": max_repeat,
                  "repeat_rmse_mm": float(np.sqrt(np.mean([row["repeat_minus_primary_mm"] ** 2 for row in repeated]))),
                  "repeat_error_meaning": "same-source method repeatability only; neither field accuracy nor a second-observer study",
                  "models_trained": 0, "generator_executed": False, "new_holdout_opened": False,
                  "dataset_linkage": linkage["status"],
                  "environment": {"python": platform.python_version(), "numpy": np.__version__, "matplotlib": matplotlib.__version__,
                                  "renderer": "Poppler pdftoppm 300 dpi"}}
    write_json(output / "validation_report.json", validation)
    limitations = {
        "source": "geokniga-02obrabotka.pdf",
        "source_location": "page 15, profile 1, leveling, 2011-2016",
        "reconstructed_object": "published spatial interval profile",
        "horizontal_axis": "printed distance labels treated as categorical positions",
        "metric_georeferencing_accuracy_established": False,
        "eligible_as_t1_time_series": False,
        "unresolved_position_count": status_counts["unresolved_occlusion"],
        "limits": [
            "The first three values are unreadable and remain unfilled.",
            "Digitization envelopes and repeat differences are not field-measurement accuracy.",
            "One interval profile does not identify the temporal history within 2011-2016.",
            "No geographic or identifier correspondence links the profile to synthetic P-H/P-V/P-D points.",
        ],
    }
    write_json(output / "limitations.json", limitations)
    readme = """# MUSIKHIN_LINE1_LEVELING_2011_2016_V1

Детальная независимая проверка одной опубликованной серии: профиль 1,
нивелирование, интервал 2011–2016. Сохранены исходный фрагмент, наложение,
оцифрованные точки, повторное считывание, диагностика осей, нечитаемые места,
погрешность считывания и проверка связи с принятой реконструкцией данных.

Из 14 позиций численно восстановлены 11; первые три оставлены без значения из-за
перекрытия. Границы считывания и повторяемость относятся только к оцифровке
графика, а не к точности полевого нивелирования. Профиль не является временным
рядом и не сопоставлен с синтетическими идентификаторами точек.

Воспроизведение:

```powershell
.\\.venv\\Scripts\\python.exe scripts\\reconstruct_musikhin_line1.py --root .
```
"""
    (output / "README.md").write_text(readme, encoding="utf-8", newline="\n")
    names = ["points.csv", "repeat_readings.csv", "axis_diagnostics.json", "repeat_calibration.json", "dataset_linkage.json",
             "source_fragment.png", "source_overlay.png", "reconstructed_profile.png", "comparison.png", "validation_report.json",
             "limitations.json", "README.md"]
    manifest = {"artifact_id": config["artifact_id"], "source_data_commit": config["accepted_data_commit"],
                "command": "python scripts/reconstruct_musikhin_line1.py --root .",
                "inputs": verified + [{"path": rel, "sha256": sha256(root / rel), "size_bytes": (root / rel).stat().st_size}
                                       for rel in (CONFIG, "scripts/reconstruct_musikhin_line1.py")],
                "outputs": [{"path": name, "sha256": sha256(output / name), "size_bytes": (output / name).stat().st_size} for name in names]}
    write_json(output / "manifest.json", manifest)
    print(json.dumps({key: validation[key] for key in ("status", "readability_counts", "max_abs_repeat_difference_mm", "repeat_rmse_mm", "models_trained", "dataset_linkage")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Build a source-aware registry of scenario constraints and open assumptions."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path


DEFAULT_CONFIG = "configs/scenario_constraints_v1.json"
FIELDS = [
    "constraint_id",
    "domain",
    "quantity",
    "lower_bound",
    "upper_bound",
    "unit",
    "allowed_values",
    "basis_type",
    "source_id",
    "source_location",
    "dependency",
    "confidence",
    "use_scope",
    "status",
    "notes",
]


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


def verify_file(root: Path, spec: dict) -> Path:
    path = relative_path(root, spec["path"])
    if not path.is_file():
        raise FileNotFoundError(path)
    if path.stat().st_size != spec["size_bytes"] or sha256(path) != spec["sha256"]:
        raise ValueError(f"Input integrity failure: {spec['path']}")
    return path


def verify_manifest(root: Path, spec: dict) -> tuple[Path, dict]:
    manifest_path = relative_path(root, spec["manifest"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("artifact_id") != spec["artifact_id"]:
        raise ValueError(f"Unexpected upstream artifact: {manifest_path}")
    for item in manifest.get("inputs", []):
        verify_file(root, item)
    for item in manifest.get("outputs", []):
        output_path = manifest_path.parent / item["path"]
        if not output_path.is_file():
            raise FileNotFoundError(output_path)
        if output_path.stat().st_size != item["size_bytes"] or sha256(output_path) != item["sha256"]:
            raise ValueError(f"Upstream output integrity failure: {output_path}")
    return manifest_path, manifest


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def write_rows(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def fmt(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def base_row(**values: object) -> dict[str, object]:
    row = {field: "" for field in FIELDS}
    row.update(values)
    return row


def musikhin_rows(root: Path, manifest_path: Path) -> tuple[list[dict], dict]:
    groups: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    envelopes: dict[tuple[str, str], list[float]] = defaultdict(list)
    profile_root = manifest_path.parent
    profile_labels = ["1", "5", "17", "6"]
    finite_count = 0
    for profile in profile_labels:
        for row in read_rows(profile_root / f"profile_{profile}" / "points.csv"):
            raw_value = row["signed_displacement_mm"].strip()
            if not raw_value:
                continue
            finite_count += 1
            key = (profile, row["method"], row["observation_interval_label"])
            groups[key].append(float(raw_value))
            raw_envelope = row["digitization_envelope_half_width_mm"].strip()
            if raw_envelope:
                envelopes[(profile, row["reading_status"])].append(float(raw_envelope))

    rows = []
    for (profile, method, interval), values in sorted(groups.items()):
        token = f"P{profile}-{method}-{interval}".upper().replace("_", "-")
        rows.append(
            base_row(
                constraint_id=f"MUS-{token}-SIGNED-DISP",
                domain="published_spatial_profile",
                quantity="signed_displacement_envelope",
                lower_bound=fmt(min(values)),
                upper_bound=fmt(max(values)),
                unit="mm",
                basis_type="published_graph_digitization",
                source_id="SUP01",
                source_location=f"geokniga-02obrabotka.pdf, p.15, profile {profile}, {method}, {interval}",
                dependency="profile label, method, observation interval and printed categorical position order",
                confidence="medium",
                use_scope="scenario_conditioning_and_stress_design_not_parameter_prior",
                status="SOURCE_ENVELOPE",
                notes=f"Envelope of {len(values)} readable digitized values; not a time series and not mapped to synthetic points.",
            )
        )
    for (profile, status), values in sorted(envelopes.items()):
        status_token = "VISIBLE" if status == "digitized_marker_or_visible_curve" else "PARTIAL"
        rows.append(
            base_row(
                constraint_id=f"MUS-P{profile}-{status_token}-DIGITIZATION",
                domain="digitization_uncertainty",
                quantity="reading_envelope_half_width",
                lower_bound=fmt(min(values)),
                upper_bound=fmt(max(values)),
                unit="mm",
                basis_type="explicit_digitization_envelope",
                source_id="SUP01",
                source_location=f"geokniga-02obrabotka.pdf, p.15, profile {profile}",
                dependency="reading visibility class; applies only to the graph digitization",
                confidence="medium",
                use_scope="digitization_sensitivity_only",
                status="READING_ENVELOPE",
                notes="This is not field-measurement sigma and not a confidence interval.",
            )
        )
    return rows, {
        "profiles": profile_labels,
        "finite_digitized_values": finite_count,
        "profile_series_constraints": len(groups),
        "digitization_envelope_constraints": len(envelopes),
    }


def filatova_rows(
    anchors_path: Path,
    overview_path: Path,
    figure_manifest_path: Path,
) -> tuple[list[dict], dict]:
    anchors = [
        row for row in read_rows(anchors_path)
        if row["anchor_type"] == "subsidence_zonal_statistics"
    ]
    if not anchors:
        raise ValueError("No published subsidence zonal statistics were found")
    rows = []
    for field, quantity in [
        ("disp_min_mm", "published_zonal_minimum_envelope"),
        ("disp_mean_mm", "published_zonal_mean_envelope"),
        ("disp_max_mm", "published_zonal_maximum_envelope"),
    ]:
        values = [float(row[field]) for row in anchors if row[field].strip()]
        rows.append(
            base_row(
                constraint_id=f"FIL-{field.upper().replace('_MM', '')}-ENVELOPE",
                domain="published_subsidence_statistics",
                quantity=quantity,
                lower_bound=fmt(min(values)),
                upper_bound=fmt(max(values)),
                unit="mm",
                basis_type="published_table_transcription",
                source_id="ВКР_Филатова_М_С.docx",
                source_location="Рисунок 18; eight transcribed zonal-statistics rows",
                dependency="published zone aggregation and original displacement sign convention",
                confidence="high_transcription_low_transfer",
                use_scope="scenario_scale_sensitivity_not_direct_calibration",
                status="SOURCE_ENVELOPE",
                notes="Positive published magnitudes are preserved; no sign conversion or spatial transfer is inferred.",
            )
        )

    figure_validation = json.loads(
        (figure_manifest_path.parent / "validation_report.json").read_text(encoding="utf-8")
    )
    region_count = int(figure_validation["nominal_regions"])
    rows.append(
        base_row(
            constraint_id="FIL-FIG13B-CLOSED-REGIONS",
            domain="published_plan_topology",
            quantity="nominal_closed_region_count",
            lower_bound=str(region_count),
            upper_bound=str(region_count),
            unit="regions",
            basis_type="published_figure_segmentation",
            source_id="ВКР_Филатова_М_С.docx",
            source_location="Рисунок 13б, red plan overlay",
            dependency="source raster crop, red-color threshold and closed-component rule",
            confidence="high_internal_low_geodetic",
            use_scope="spatial_topology_stress_design_only",
            status="RECONSTRUCTED_TOPOLOGY",
            notes=f"{figure_validation['threshold_sensitive_regions']} of {region_count} regions are threshold-sensitive; coordinates are pixels.",
        )
    )

    overview = read_rows(overview_path)
    if len(overview) != 1:
        raise ValueError("Expected exactly one overview georeference row")
    uncertainty = float(overview[0]["estimated_horizontal_uncertainty_m"])
    rows.append(
        base_row(
            constraint_id="FIL-OVERVIEW-HORIZONTAL-UNCERTAINTY",
            domain="georeferencing_context",
            quantity="estimated_horizontal_uncertainty",
            lower_bound=fmt(uncertainty),
            upper_bound=fmt(uncertainty),
            unit="m",
            basis_type="context_estimate",
            source_id="OVERVIEW-01",
            source_location="Figure 13 OSM screenshot plus public SKRU-1/Gorodishche context points",
            dependency="unofficial context alignment; official coordinate key and control points absent",
            confidence="low",
            use_scope="visual_context_only_not_engineering",
            status="CONTEXT_ONLY",
            notes="Must not be used as an engineering georeferencing accuracy claim.",
        )
    )
    return rows, {
        "zonal_statistics_rows": len(anchors),
        "zonal_envelope_constraints": 3,
        "nominal_closed_regions": region_count,
        "threshold_sensitive_regions": int(figure_validation["threshold_sensitive_regions"]),
        "overview_horizontal_uncertainty_m": uncertainty,
    }


def assumption_rows(config: dict) -> list[dict]:
    rows = []
    for assumption in config["required_scenario_assumptions"]:
        rows.append(
            base_row(
                constraint_id=assumption["constraint_id"],
                domain=assumption["domain"],
                quantity=assumption["quantity"],
                unit="categorical",
                allowed_values=assumption["allowed_values"],
                basis_type="explicit_design_assumption",
                source_id="ASSUMPTION-RESEARCH-DIRECTION",
                source_location="docs/governance/RESEARCH_DIRECTION_RU.md, sections 8-9",
                dependency=assumption["dependency"],
                confidence=assumption["confidence"],
                use_scope="scenario_protocol_design",
                status=assumption["status"],
                notes=assumption["notes"],
            )
        )
    return rows


def main() -> None:
    args = parse_args()
    root = args.root.resolve()
    config_path = relative_path(root, args.config)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    output = relative_path(root, args.output or config["output_directory"])

    upstream = [verify_manifest(root, item) for item in config["upstream_artifacts"]]
    fixed_paths = [verify_file(root, item) for item in config["fixed_inputs"]]
    musikhin_manifest_path = next(
        path for path, manifest in upstream
        if manifest["artifact_id"] == "MUSIKHIN_PUBLISHED_PROFILES_V1"
    )
    figure_manifest_path = next(
        path for path, manifest in upstream
        if manifest["artifact_id"] == "FILATOVA_FIGURE13B_GEOMETRY_V1"
    )
    anchors_path = next(path for path in fixed_paths if path.name == "published_anchors_transcribed.csv")
    overview_path = next(path for path in fixed_paths if path.name == "overview_georeference.csv")

    musikhin, musikhin_summary = musikhin_rows(root, musikhin_manifest_path)
    filatova, filatova_summary = filatova_rows(anchors_path, overview_path, figure_manifest_path)
    assumptions = assumption_rows(config)
    rows = musikhin + filatova + assumptions

    duplicate_ids = len(rows) != len({row["constraint_id"] for row in rows})
    numeric_ordered = True
    for row in rows:
        if row["lower_bound"] != "" and row["upper_bound"] != "":
            numeric_ordered &= float(row["lower_bound"]) <= float(row["upper_bound"])
    assumptions_without_numeric_bounds = all(
        row["lower_bound"] == "" and row["upper_bound"] == ""
        for row in assumptions
    )
    checks = {
        "all_inputs_hash_verified": True,
        "upstream_manifests_and_outputs_verified": True,
        "required_columns_present": all(list(row) == FIELDS for row in rows),
        "constraint_ids_unique": not duplicate_ids,
        "dependencies_explicit": all(str(row["dependency"]).strip() for row in rows),
        "confidence_explicit": all(str(row["confidence"]).strip() for row in rows),
        "source_or_assumption_explicit": all(str(row["source_id"]).strip() for row in rows),
        "numeric_bounds_ordered": numeric_ordered,
        "unsupported_assumption_numeric_bounds_blank": assumptions_without_numeric_bounds,
        "digitization_not_labeled_measurement_sigma": all(
            "sigma" not in str(row["quantity"]).lower()
            for row in rows if row["basis_type"] == "explicit_digitization_envelope"
        ),
        "no_model_training": True,
    }

    output.mkdir(parents=True, exist_ok=True)
    write_rows(output / "scenario_constraints.csv", rows)
    validation = {
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "constraint_count": len(rows),
        "basis_counts": dict(sorted(
            (basis, sum(row["basis_type"] == basis for row in rows))
            for basis in {row["basis_type"] for row in rows}
        )),
        "status_counts": dict(sorted(
            (status, sum(row["status"] == status for row in rows))
            for status in {row["status"] for row in rows}
        )),
        "musikhin": musikhin_summary,
        "filatova": filatova_summary,
        "open_design_assumptions": len(assumptions),
        "models_trained": 0,
        "new_holdout_opened": False,
    }
    write_json(output / "validation_report.json", validation)
    readme = f"""# SKRU1_SCENARIO_CONSTRAINTS_V1

Реестр из {len(rows)} ограничений и явно открытых проектных допущений для
следующего генератора сценариев. Каждая строка указывает диапазон или набор
допустимых значений, основание, источник, зависимость, уверенность и область
допустимого использования.

Публикационные огибающие предназначены для кондиционирования и стресс-дизайна,
но не объявляются априорными распределениями параметров. Пиксельная топология
рисунка 13б не является GIS-слоем. Границы оцифровки профилей не являются
погрешностью полевого измерения. Четыре строки со статусом `TO_BE_FROZEN`
сознательно не содержат численных границ: источник их не задаёт.

Воспроизведение:

```powershell
.\\.venv\\Scripts\\python.exe scripts\\build_scenario_constraints.py --root .
```
"""
    (output / "README.md").write_text(readme, encoding="utf-8", newline="\n")

    script_path = Path(__file__).resolve()
    inputs = [
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": sha256(path),
            "size_bytes": path.stat().st_size,
        }
        for path in [*(path for path, _ in upstream), *fixed_paths, config_path, script_path]
    ]
    outputs = [
        {
            "path": path.name,
            "sha256": sha256(path),
            "size_bytes": path.stat().st_size,
        }
        for path in [output / "README.md", output / "scenario_constraints.csv", output / "validation_report.json"]
    ]
    source_commits = sorted({manifest["source_data_commit"] for _, manifest in upstream})
    write_json(
        output / "manifest.json",
        {
            "schema_version": 1,
            "artifact_id": config["artifact_id"],
            "source_data_commits": source_commits,
            "command": "python scripts/build_scenario_constraints.py --root .",
            "inputs": inputs,
            "outputs": outputs,
        },
    )
    print(json.dumps(validation, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

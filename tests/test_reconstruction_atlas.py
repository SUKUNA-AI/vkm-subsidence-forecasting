from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ATLAS_ROOT = ROOT / "artifacts/reconstruction"


def file_hash(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def assert_manifest_outputs(manifest_path: Path) -> None:
    manifest = read_json(manifest_path)
    assert manifest["outputs"]
    for item in manifest["outputs"]:
        path = manifest_path.parent / item["path"]
        assert path.is_file(), item["path"]
        assert path.stat().st_size == item["size_bytes"]
        assert file_hash(path) == item["sha256"]


def test_detailed_musikhin_line1_is_complete_and_explicit_about_limits() -> None:
    artifact = ATLAS_ROOT / "musikhin_line1_2011_2016_v1"
    validation = read_json(artifact / "validation_report.json")
    assert validation["status"] == "PASS"
    assert validation["readability_counts"] == {
        "clear_marker_center": 7,
        "partially_occluded_approximation": 4,
        "unresolved_occlusion": 3,
    }
    assert validation["numerically_reconstructed_positions"] == 11
    assert validation["max_abs_repeat_difference_mm"] <= 1.0
    assert validation["dataset_linkage"] == "NO_NUMERIC_PROFILE_LINK_FOUND"
    assert validation["models_trained"] == 0
    limitations = read_json(artifact / "limitations.json")
    assert limitations["unresolved_position_count"] == 3
    assert limitations["metric_georeferencing_accuracy_established"] is False
    assert limitations["eligible_as_t1_time_series"] is False
    assert_manifest_outputs(artifact / "manifest.json")


def test_musikhin_atlas_preserves_all_profiles_and_unreadable_points() -> None:
    artifact = ATLAS_ROOT / "musikhin_profiles_2011_2016_v1"
    validation = read_json(artifact / "validation_report.json")
    assert validation["status"] == "PASS"
    assert validation["checks"]["four_profiles_present"] is True
    assert validation["candidate_positions"] == 258
    assert validation["finite_digitized_values"] == 252
    assert validation["readability_counts"] == {
        "unresolved_occlusion": 6,
        "digitized_marker_or_visible_curve": 186,
        "partially_occluded_approximation": 66,
    }
    assert validation["checks"]["categorical_axes_preserved"] is True
    assert validation["checks"]["no_synthetic_profile_mapping_invented"] is True
    assert validation["models_trained"] == 0
    assert len(read_rows(artifact / "unresolved_positions.csv")) == 6
    assert {path.name for path in artifact.glob("profile_*") if path.is_dir()} == {
        "profile_1", "profile_5", "profile_17", "profile_6"
    }
    assert_manifest_outputs(artifact / "manifest.json")


def test_filatova_scheme_records_pixel_topology_and_sensitivity() -> None:
    artifact = ATLAS_ROOT / "filatova_figure13b_geometry_v1"
    validation = read_json(artifact / "validation_report.json")
    assert validation["status"] == "PASS"
    assert validation["nominal_regions"] == 538
    assert validation["stable_regions"] == 525
    assert validation["threshold_sensitive_regions"] == 13
    assert validation["metric_georeferencing_accuracy_established"] is False
    assert validation["models_trained"] == 0
    units = read_rows(artifact / "plan_units_pixel.csv")
    assert len(units) == 538
    assert {row["coordinate_system"] for row in units} == {
        "zero_based_source_raster_upper_left_x_right_y_down"
    }
    assert all(row["metric_georeferencing_accuracy_established"] == "False" for row in units)
    assert len(read_rows(artifact / "unreadable_regions.csv")) == 13
    assert_manifest_outputs(artifact / "manifest.json")


def test_scenario_constraint_registry_is_complete_and_source_aware() -> None:
    artifact = ATLAS_ROOT / "scenario_constraints_v1"
    validation = read_json(artifact / "validation_report.json")
    assert validation["status"] == "PASS"
    assert validation["constraint_count"] == 31
    assert validation["open_design_assumptions"] == 4
    rows = read_rows(artifact / "scenario_constraints.csv")
    assert len(rows) == 31
    expected = {
        "constraint_id", "domain", "quantity", "lower_bound", "upper_bound",
        "unit", "allowed_values", "basis_type", "source_id", "source_location",
        "dependency", "confidence", "use_scope", "status", "notes",
    }
    assert set(rows[0]) == expected
    assert len({row["constraint_id"] for row in rows}) == len(rows)
    assert all(row["dependency"] and row["confidence"] and row["source_id"] for row in rows)
    numeric = [row for row in rows if row["lower_bound"] and row["upper_bound"]]
    assert all(float(row["lower_bound"]) <= float(row["upper_bound"]) for row in numeric)
    assumptions = [row for row in rows if row["status"] == "TO_BE_FROZEN"]
    assert len(assumptions) == 4
    assert all(not row["lower_bound"] and not row["upper_bound"] for row in assumptions)
    assert all(row["allowed_values"] for row in assumptions)
    assert_manifest_outputs(artifact / "manifest.json")

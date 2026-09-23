"""Manifest-checked adapter from scenario v1 tables to B1/IMM interfaces."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .scenario_simulation import REQUIRED_FEATURES, repository_path, sha256_file, verify_file
from .splits import ManifestDataset, SplitProvenance, sample_id_list_sha256


DEFAULT_MANIFEST = "data/scenario_simulation_v1/manifest.json"
NUMERIC_FEATURES = tuple(
    field
    for field in REQUIRED_FEATURES
    if field not in {"current_campaign_type", "target_campaign_type"}
)
HISTORY_NUMERIC = (
    "last_settlement_mm",
    "last_rate_mm_y",
    "current_standard_uncertainty_mm",
    "recent_acceleration_mm_y2",
    "std_last_3_rates_mm_y",
    "missing_campaigns_since_previous",
)


@dataclass(frozen=True)
class ScenarioModelBundle:
    root: Path
    manifest_path: Path
    manifest: dict
    catalog: pd.DataFrame
    samples: pd.DataFrame
    train: ManifestDataset
    calibration: ManifestDataset
    evaluation: ManifestDataset
    causal_history: pd.DataFrame


def _bool_series(series: pd.Series, name: str) -> pd.Series:
    values = series.astype(str).str.strip().str.lower().map(
        {"true": True, "false": False, "1": True, "0": False}
    )
    if values.isna().any():
        raise ValueError(f"Invalid boolean values in {name}")
    return values.astype(bool)


def _verify_manifest(root: Path, manifest_path: Path, expected_dataset_id: str) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("dataset_id") != expected_dataset_id:
        raise ValueError(f"Unexpected scenario dataset: {manifest.get('dataset_id')}")
    for item in manifest.get("inputs", []):
        path = repository_path(root, str(item["path"]))
        verify_file(
            path,
            expected_hash=str(item["sha256"]),
            expected_size=int(item["size_bytes"]),
        )
    for item in manifest.get("outputs", []):
        path = (manifest_path.parent / str(item["path"])).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"Scenario output escapes repository: {path}")
        verify_file(
            path,
            expected_hash=str(item["sha256"]),
            expected_size=int(item["size_bytes"]),
        )
    return manifest


def _provenance(
    manifest: dict,
    split_manifest_path: Path,
    role: str,
    sample_ids: tuple[str, ...],
) -> SplitProvenance:
    return SplitProvenance(
        task="scenario_t1",
        split=role,
        version=str(manifest["dataset_id"]),
        manifest_path=split_manifest_path,
        manifest_file_sha256=sha256_file(split_manifest_path),
        sample_ids_sha256=sample_id_list_sha256(sample_ids),
        row_count=len(sample_ids),
        test_authorized=False,
    )


def _dataset(
    samples: pd.DataFrame,
    manifest: dict,
    split_manifest_path: Path,
    role: str,
) -> ManifestDataset:
    frame = samples.loc[samples["model_role"].eq(role)].copy().reset_index(drop=True)
    sample_ids = tuple(frame["sample_id"].astype(str))
    if not sample_ids or len(sample_ids) != len(set(sample_ids)):
        raise ValueError(f"Scenario {role} split is empty or has duplicate IDs")
    if role in {"train", "calibration"} and not np.isfinite(
        pd.to_numeric(frame["observed_rate_mm_y"], errors="coerce")
    ).all():
        raise ValueError(f"Scenario {role} split contains unavailable observed targets")
    return ManifestDataset(
        frame=frame,
        feature_columns=REQUIRED_FEATURES,
        provenance=_provenance(manifest, split_manifest_path, role, sample_ids),
    )


def load_scenario_model_bundle(
    root: str | Path,
    *,
    manifest_relative_path: str = DEFAULT_MANIFEST,
    expected_dataset_id: str = "SKRU1_SCENARIO_SIMULATION_V1",
) -> ScenarioModelBundle:
    root_path = Path(root).resolve()
    manifest_path = repository_path(root_path, manifest_relative_path)
    if not manifest_path.is_file():
        raise FileNotFoundError(manifest_path)
    manifest = _verify_manifest(root_path, manifest_path, expected_dataset_id)
    data_root = manifest_path.parent
    samples = pd.read_csv(
        data_root / "next_planned_samples.csv.gz",
        dtype={
            "sample_id": str,
            "scenario_id": str,
            "point_id": str,
            "profile_id": str,
            "base_point_id": str,
            "base_profile_id": str,
        },
        low_memory=False,
    )
    assignments = pd.read_csv(data_root / "split_assignments.csv.gz", dtype=str)
    catalog = pd.read_csv(data_root / "scenario_catalog.csv", dtype=str)
    if samples["sample_id"].duplicated().any() or assignments["sample_id"].duplicated().any():
        raise ValueError("Scenario samples or split assignments have duplicate IDs")
    sample_roles = samples.set_index("sample_id")["model_role"].astype(str).sort_index()
    assigned_roles = assignments.set_index("sample_id")["model_role"].astype(str).sort_index()
    if not sample_roles.equals(assigned_roles):
        raise ValueError("Split assignments do not match scenario samples")
    samples["target_observed_available"] = _bool_series(
        samples["target_observed_available"], "target_observed_available"
    )
    samples["current_date"] = pd.to_datetime(samples["current_date"], errors="raise")
    samples["target_date"] = pd.to_datetime(samples["target_date"], errors="raise")
    for column in (
        *NUMERIC_FEATURES,
        "observed_increment_mm",
        "observed_rate_mm_y",
        "latent_current_settlement_mm",
        "latent_target_settlement_mm",
        "latent_increment_mm",
        "latent_rate_mm_y",
    ):
        samples[column] = pd.to_numeric(samples[column], errors="coerce")
    if samples.loc[samples["model_role"].eq("evaluation"), "latent_rate_mm_y"].isna().any():
        raise ValueError("Evaluation rows have missing latent truth")
    forbidden = {
        "latent_current_settlement_mm",
        "latent_target_settlement_mm",
        "latent_increment_mm",
        "latent_rate_mm_y",
        "dynamic_mechanism",
        "missingness_mechanism",
        "measurement_error_mechanism",
        "generator_seed",
    }
    if forbidden.intersection(REQUIRED_FEATURES):
        raise ValueError("Generator truth leaked into the formal feature allowlist")

    history = pd.read_csv(data_root / "causal_history.csv.gz", low_memory=False)
    history["current_date"] = pd.to_datetime(history["current_date"], errors="raise")
    for column in HISTORY_NUMERIC:
        history[column] = pd.to_numeric(history[column], errors="coerce")
    if any(column.startswith("latent_") for column in history):
        raise ValueError("Causal history contains hidden generator truth")
    history_key = history[["point_id", "current_date"]].drop_duplicates()
    origin_key = samples[["point_id", "current_date"]].drop_duplicates()
    coverage = origin_key.merge(history_key, on=["point_id", "current_date"], how="left", indicator=True)
    if not coverage["_merge"].eq("both").all():
        raise ValueError("Causal history does not cover every model origin")

    split_manifest_path = data_root / "split_assignments.csv.gz"
    train = _dataset(samples, manifest, split_manifest_path, "train")
    calibration = _dataset(samples, manifest, split_manifest_path, "calibration")
    evaluation = _dataset(samples, manifest, split_manifest_path, "evaluation")
    heldout = set(catalog.loc[catalog["experiment_role"].eq("heldout_mechanism"), "dynamic_mechanism"])
    if heldout.intersection(train.frame["dynamic_mechanism"]) or heldout.intersection(
        calibration.frame["dynamic_mechanism"]
    ):
        raise ValueError("Held-out mechanisms entered fit or calibration")
    return ScenarioModelBundle(
        root=root_path,
        manifest_path=manifest_path,
        manifest=manifest,
        catalog=catalog,
        samples=samples,
        train=train,
        calibration=calibration,
        evaluation=evaluation,
        causal_history=history,
    )

from __future__ import annotations

from pathlib import Path
import shutil

import pandas as pd
import pytest

from skru1.data_contracts import load_canonical_bundle
from skru1.leakage import assert_disjoint_sample_sets
from skru1.splits import (
    FrozenManifestError,
    MANIFEST_LAYOUT,
    SealedTestError,
    UnsafeSplitError,
    attach_spatial_zones,
    build_spatial_zone_map,
    combine_development_datasets,
    expected_manifest_frames,
    leave_one_group_out_assignments,
    load_split_dataset,
    reject_plain_kfold,
    reject_random_train_test_split,
    sample_id_list_sha256,
    rolling_origin_assignments,
    validate_splitter_name,
    write_frozen_manifests,
)


ROOT = Path(__file__).resolve().parents[1]


def local_test_directory(name: str) -> Path:
    directory = ROOT / "work" / "tests" / name
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    return directory


@pytest.fixture(scope="module")
def bundle():
    return load_canonical_bundle(ROOT)


def test_frozen_manifest_counts_and_disjointness(bundle) -> None:
    manifests = expected_manifest_frames(bundle)
    expected_counts = {
        ("t1", "train"): 911,
        ("t1", "validation"): 130,
        ("t1", "test"): 175,
        ("t5", "train"): 942,
        ("t5", "validation"): 211,
        ("t5", "test_complete"): 28,
        ("t5", "test_censored"): 93,
    }
    assert {key: len(frame) for key, frame in manifests.items()} == expected_counts
    assert_disjoint_sample_sets(
        {split: manifests[("t1", split)]["sample_id"] for split in MANIFEST_LAYOUT["t1"]}
    )
    assert_disjoint_sample_sets(
        {split: manifests[("t5", split)]["sample_id"] for split in MANIFEST_LAYOUT["t5"]}
    )


def test_manifest_id_hash_is_order_sensitive_and_stable(bundle) -> None:
    train_ids = expected_manifest_frames(bundle)[("t1", "train")]["sample_id"].astype(str).tolist()
    first = sample_id_list_sha256(train_ids)
    second = sample_id_list_sha256(list(train_ids))
    reversed_hash = sample_id_list_sha256(list(reversed(train_ids)))
    assert first == second
    assert first != reversed_hash
    assert len(first) == 64


def test_t1_boundaries_are_strictly_target_date(bundle) -> None:
    target = bundle.operational_targets
    assert target.loc[target["split"].eq("train"), "target_date"].max() <= pd.Timestamp("2023-12-31")
    validation = target[target["split"].eq("validation")]["target_date"]
    assert validation.min() >= pd.Timestamp("2024-01-01")
    assert validation.max() <= pd.Timestamp("2024-12-31")
    assert target.loc[target["split"].eq("test"), "target_date"].min() >= pd.Timestamp("2025-01-01")


def test_t5_boundaries_are_strictly_horizon_end(bundle) -> None:
    labels = bundle.early_warning_labels
    assert labels.loc[labels["split_by_horizon_end"].eq("train"), "label_horizon_end"].max() <= pd.Timestamp("2023-12-31")
    validation = labels[labels["split_by_horizon_end"].eq("validation")]["label_horizon_end"]
    assert validation.min() >= pd.Timestamp("2024-01-01")
    assert validation.max() <= pd.Timestamp("2024-12-31")
    assert labels.loc[labels["split_by_horizon_end"].eq("test"), "label_horizon_end"].min() >= pd.Timestamp("2025-01-01")


def test_existing_frozen_manifests_match_canonical_inputs(bundle) -> None:
    evidence = write_frozen_manifests(bundle)
    for key, item in evidence.items():
        assert item.row_count == len(expected_manifest_frames(bundle)[key])
        assert item.manifest_path.is_file()


def test_frozen_manifest_refuses_mutation_in_temporary_root(bundle) -> None:
    temporary_root = local_test_directory("gate_a1_frozen_manifest")
    write_frozen_manifests(bundle, output_root=temporary_root)
    path = temporary_root / MANIFEST_LAYOUT["t1"]["train"]
    frame = pd.read_csv(path)
    frame.iloc[0, 0] = "tampered-sample-id"
    frame.to_csv(path, index=False)
    with pytest.raises(FrozenManifestError):
        write_frozen_manifests(bundle, output_root=temporary_root)


@pytest.mark.parametrize("function", [reject_random_train_test_split, reject_plain_kfold])
def test_unsafe_row_split_guards_raise(function) -> None:
    with pytest.raises(UnsafeSplitError):
        function()


@pytest.mark.parametrize("name", ["KFold", "train_test_split", "ShuffleSplit", "random split"])
def test_unsafe_splitter_names_raise(name: str) -> None:
    with pytest.raises(UnsafeSplitError):
        validate_splitter_name(name)


def test_model_facing_test_is_sealed(bundle) -> None:
    missing_record = ROOT / "work" / "tests" / "missing_candidate_record.json"
    with pytest.raises(SealedTestError):
        load_split_dataset(
            "t1",
            "test",
            root=bundle.root,
            candidate_record=missing_record,
        )


def test_dependency_aware_validation_designs_are_deterministic(bundle) -> None:
    train = load_split_dataset("t1", "train", root=bundle.root)
    validation = load_split_dataset("t1", "validation", root=bundle.root)
    assert train.provenance.manifest_path.name == "train.csv"
    assert validation.provenance.manifest_path.name == "validation.csv"

    rolling = rolling_origin_assignments([train, validation])
    assert rolling["fold_id"].nunique() >= 3
    development = combine_development_datasets([train, validation])
    profile = leave_one_group_out_assignments(development, group_field="profile_id")
    assert profile["fold_id"].nunique() == 14
    zone_map, _ = build_spatial_zone_map(bundle)
    zoned = attach_spatial_zones(development, zone_map)
    zone = leave_one_group_out_assignments(zoned, group_field="zone_id")
    assert zone["fold_id"].nunique() == zone_map["zone_id"].nunique() == 4

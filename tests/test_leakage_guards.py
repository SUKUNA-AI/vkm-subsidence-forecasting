from __future__ import annotations

from pathlib import Path
import shutil

import pandas as pd
import pytest

from skru1.data_contracts import load_canonical_bundle
from skru1.leakage import (
    LeakageViolation,
    assert_disjoint_sample_sets,
    assert_expected_origin_grain,
    assert_unique_sample_ids,
    find_forbidden_split_api_usage,
)
from skru1.preprocessing import TrainOnlyPreprocessor
from skru1.splits import (
    ManifestDataset,
    SplitProvenance,
    expected_manifest_frames,
    load_split_dataset,
    sample_id_list_sha256,
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


def make_dataset(bundle, split: str) -> ManifestDataset:
    ids = expected_manifest_frames(bundle)[("t1", split)]["sample_id"].astype(str).tolist()
    features = bundle.features.set_index("sample_id", drop=False).loc[ids].reset_index(drop=True)
    provenance = SplitProvenance(
        task="t1",
        split=split,
        version="t1_v1",
        manifest_path=Path(f"{split}.csv"),
        manifest_file_sha256="0" * 64,
        sample_ids_sha256=sample_id_list_sha256(ids),
        row_count=len(ids),
        test_authorized=False,
    )
    return ManifestDataset(features, bundle.feature_contract.allowed_features, provenance)


def test_duplicate_sample_id_is_rejected(bundle) -> None:
    frame = pd.concat([bundle.features.head(2), bundle.features.head(1)], ignore_index=True)
    with pytest.raises(LeakageViolation):
        assert_unique_sample_ids(frame, "injected duplicate")


def test_duplicate_expected_grain_is_rejected(bundle) -> None:
    duplicate = bundle.features.head(1).copy()
    duplicate["sample_id"] = duplicate["sample_id"] + "::duplicate"
    frame = pd.concat([bundle.features.head(2), duplicate], ignore_index=True)
    with pytest.raises(LeakageViolation):
        assert_expected_origin_grain(frame, "injected grain duplicate")


def test_cross_split_overlap_is_rejected() -> None:
    with pytest.raises(LeakageViolation):
        assert_disjoint_sample_sets({"train": ["a", "b"], "validation": ["b", "c"]})


def test_preprocessor_can_fit_train_only(bundle) -> None:
    train = make_dataset(bundle, "train")
    validation = make_dataset(bundle, "validation")
    preprocessor = TrainOnlyPreprocessor(bundle.feature_contract)
    transformed_train = preprocessor.fit_transform(train)
    transformed_validation = preprocessor.transform(validation)
    assert preprocessor.fitted_train_sample_hash_ == train.provenance.sample_ids_sha256
    assert transformed_train["sample_id"].tolist() == train.frame["sample_id"].astype(str).tolist()
    assert tuple(transformed_train.columns) == tuple(transformed_validation.columns)
    assert not transformed_train.drop(columns="sample_id").isna().any().any()
    with pytest.raises(LeakageViolation):
        TrainOnlyPreprocessor(bundle.feature_contract).fit(validation)


def test_source_scanner_finds_forbidden_split_calls() -> None:
    source = local_test_directory("gate_a1_source_scanner") / "bad_model.py"
    source.write_text(
        "from sklearn.model_selection import train_test_split\n"
        "parts = train_test_split(X, y, shuffle=True)\n",
        encoding="utf-8",
    )
    findings = find_forbidden_split_api_usage([source])
    assert {finding["api"] for finding in findings} == {"train_test_split", "shuffle_true"}


def test_repository_model_sources_have_no_forbidden_split_calls() -> None:
    sources = [*(ROOT / "src").rglob("*.py"), *(ROOT / "scripts").rglob("*.py")]
    assert find_forbidden_split_api_usage(sources) == []


def test_t5_model_loader_strips_private_evaluation_diagnostics(bundle) -> None:
    frame = load_split_dataset("t5", "train", root=bundle.root).frame
    forbidden = {
        "activity_180d",
        "ongoing_acceleration_180d",
        "current_true_rate_mm_y",
        "max_delta_rate_next_180d_mm_y",
        "max_acceleration_next_180d_mm_y2",
        "sustained_two_months",
        "current_regime_stage",
        "first_onset_date",
        "use_class",
    }
    assert forbidden.isdisjoint(frame.columns)
    assert "onset_180d" in frame.columns

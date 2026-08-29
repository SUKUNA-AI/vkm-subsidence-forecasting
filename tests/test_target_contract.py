from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from skru1.data_contracts import load_canonical_bundle
from skru1.leakage import assert_positive_horizon, assert_t1_time_alignment
from skru1.splits import expected_manifest_frames


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def bundle():
    return load_canonical_bundle(ROOT)


def test_target_contract_names_primary_target_and_split_authorities(bundle) -> None:
    payload = bundle.target_contract.payload
    assert payload["primary_target"] == "T1_RATE_NEXT_PLANNED"
    assert payload["split_protocol"]["regression"] == "by target_date"
    assert payload["split_protocol"]["early_warning"] == "by label_horizon_end"
    assert payload["split_protocol"]["forbidden"] == "random row split"


def test_t1_horizon_and_target_availability(bundle) -> None:
    target = bundle.operational_targets
    assert_positive_horizon(target, "forecast_horizon_days", "T1")
    assert_t1_time_alignment(target)
    available = target["target_available"].eq(True)
    assert int(available.sum()) == 1216
    assert target.loc[available, "observed_rate_mm_y"].notna().all()
    assert target.loc[~available, "observed_rate_mm_y"].isna().all()


def test_t1_manifests_contain_only_available_observed_labels(bundle) -> None:
    manifests = expected_manifest_frames(bundle)
    manifest_ids = set(
        pd.concat([manifests[("t1", split)] for split in ("train", "validation", "test")])["sample_id"]
    )
    target = bundle.operational_targets
    expected = set(target.loc[target["target_available"].eq(True) & target["label_status"].eq("observed"), "sample_id"])
    assert manifest_ids == expected


def test_t5_complete_and_censored_contract(bundle) -> None:
    labels = bundle.early_warning_labels
    assert_positive_horizon(labels, "label_horizon_days", "T5")
    complete = labels["horizon_complete"].eq(True)
    censored = ~complete
    assert int(complete.sum()) == 1181
    assert int(censored.sum()) == 93
    assert labels.loc[complete, "onset_180d"].isin([0, 1]).all()
    assert labels.loc[censored, "onset_180d"].isna().all()
    assert int(labels.loc[complete, "onset_180d"].eq(1).sum()) == 17


def test_t5_test_is_explicitly_partitioned_by_censoring(bundle) -> None:
    manifests = expected_manifest_frames(bundle)
    complete_ids = set(manifests[("t5", "test_complete")]["sample_id"])
    censored_ids = set(manifests[("t5", "test_censored")]["sample_id"])
    test_ids = set(
        bundle.early_warning_labels.loc[
            bundle.early_warning_labels["split_by_horizon_end"].eq("test"), "sample_id"
        ]
    )
    assert not complete_ids & censored_ids
    assert complete_ids | censored_ids == test_ids
    assert len(complete_ids) == 28
    assert len(censored_ids) == 93

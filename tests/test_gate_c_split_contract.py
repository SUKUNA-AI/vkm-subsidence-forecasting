from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from skru1.artifact_io import snapshot_paths
from skru1.data_contracts import load_canonical_bundle, sha256_file
from skru1.gate_c import load_gate_c_config
from skru1.sequences import build_fold_sequence_contracts, build_sequence_bundle


ROOT = Path(__file__).resolve().parents[1]


def _as_bool(value) -> bool:
    return value if isinstance(value, bool) else str(value).strip().lower() == "true"


def test_gate_c_reuses_exact_frozen_b5_fold_geometry() -> None:
    _, config = load_gate_c_config(ROOT)
    canonical = load_canonical_bundle(ROOT)
    sequence = build_sequence_bundle(ROOT, config, canonical)
    contracts = build_fold_sequence_contracts(ROOT, sequence, config)
    outer_counts = (
        contracts.loc[contracts["level"].eq("outer")]
        .groupby("design")["fold_id"]
        .nunique()
        .to_dict()
    )
    assert outer_counts == {
        "rolling_origin": 11,
        "spatiotemporal_leave_profile_out": 42,
        "spatiotemporal_leave_zone_out": 12,
    }
    assert len(contracts.loc[contracts["level"].eq("outer")]) == 65
    assert len(contracts.loc[contracts["level"].eq("inner")]) == 195
    assert contracts.loc[contracts["level"].eq("inner")].groupby("parent_fold_id").size().eq(3).all()


def test_all_gate_c_folds_are_forward_only_and_group_safe() -> None:
    _, config = load_gate_c_config(ROOT)
    path = ROOT / config["artifacts"]["fold_sequence_contracts"]
    contracts = pd.read_csv(path)
    assert contracts["forward_only"].map(_as_bool).all()
    assert contracts["held_group_absent_from_train"].map(_as_bool).all()
    assert contracts["held_group_validation_contract"].map(_as_bool).all()
    assert contracts["future_observations_in_inputs"].astype(int).eq(0).all()
    assert contracts["target_observations_in_inputs"].astype(int).eq(0).all()
    assert contracts["preprocessing_fit_role"].eq("train").all()
    assert contracts["early_stopping_scope"].eq(
        "inner_rolling_validation_within_t1_v1_train"
    ).all()


def test_gate_c_fold_provenance_hashes_b5_inputs() -> None:
    _, config = load_gate_c_config(ROOT)
    contracts = pd.read_csv(ROOT / config["artifacts"]["fold_sequence_contracts"])
    provenance = config["sequence_contract"]["fold_provenance"]
    expected = {
        provenance["outer_assignments"]: sha256_file(ROOT / provenance["outer_assignments"]),
        provenance["inner_assignments"]: sha256_file(ROOT / provenance["inner_assignments"]),
    }
    for source, frame in contracts.groupby("assignment_source"):
        assert frame["assignment_sha256"].nunique() == 1
        assert frame["assignment_sha256"].iloc[0] == expected[source]
    assert contracts["benchmark_plan_sha256"].nunique() == 1
    assert contracts["benchmark_plan_sha256"].iloc[0] == sha256_file(ROOT / provenance["benchmark_plan"])


def test_gate_c_governance_preserves_suite_v4_and_holdout_v3() -> None:
    _, config = load_gate_c_config(ROOT)
    governance = config["governance"]
    assert governance["predecessor_suite"] == "artifacts/governance/final_candidate_suite_v4.json"
    assert governance["predecessor_primary"] == "B7_two_regime_imm"
    assert governance["predecessor_suite_mutable"] is False
    assert governance["fallback_primary"] == "B7_two_regime_imm"
    assert governance["primary_change_after_holdout_access"] == "prohibited"
    assert governance["suite_v5_requires_new_holdout_policy_version"] is True

    frozen_snapshot = json.loads(
        (ROOT / config["artifacts"]["protected_snapshot"]).read_text(encoding="utf-8")
    )
    assert frozen_snapshot == snapshot_paths(ROOT, config["protected_roots"])

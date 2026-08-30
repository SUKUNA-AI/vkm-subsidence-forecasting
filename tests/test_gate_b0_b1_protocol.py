from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import shutil

import pandas as pd
import pytest

from skru1.data_contracts import load_canonical_bundle
from skru1.evaluation import build_gate_b0_b1_folds, rank_candidates
from skru1.model_selection import (
    RepeatedTestAccessError,
    claim_test_access,
    finalize_test_access,
    load_gate_b_config,
)
from skru1.splits import load_split_dataset


ROOT = Path(__file__).resolve().parents[1]


def local_test_directory(name: str) -> Path:
    directory = ROOT / "work" / "tests" / name
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    return directory


def test_gate_b0_b1_fold_contract_is_exact_and_forward_only() -> None:
    bundle = load_canonical_bundle(ROOT)
    train = load_split_dataset("t1", "train", root=ROOT)
    validation = load_split_dataset("t1", "validation", root=ROOT)
    _, folds, summary = build_gate_b0_b1_folds(train, validation, bundle)
    assert len(folds) == 24
    assert summary.groupby("design")["fold_id"].nunique().to_dict() == {
        "leave_profile_out": 14,
        "leave_zone_out": 4,
        "rolling_origin": 5,
        "temporal_holdout": 1,
    }
    assert (
        pd.to_datetime(summary["train_target_date_max"])
        < pd.to_datetime(summary["validation_target_date_min"])
    ).all()


def test_candidate_ranking_uses_only_declared_development_designs() -> None:
    _, config = load_gate_b_config(ROOT)
    designs = list(config["selection"]["normalized_mae_weights"])
    rows = []
    for model_number, spec in enumerate(config["models"], start=1):
        for design_number, design in enumerate(designs, start=1):
            rows.append(
                {
                    "design": design,
                    "model_id": spec["model_id"],
                    "mae": float(10 + model_number + design_number / 10),
                }
            )
    aggregate = pd.DataFrame(rows)
    ranking = rank_candidates(
        aggregate,
        model_specs=config["models"],
        selection_config=config["selection"],
    )
    assert ranking["selected"].sum() == 1
    assert ranking.iloc[0]["model_id"] == "B1_persistence_last_rate"
    assert set(column.removesuffix("_mae") for column in ranking if column.endswith("_mae")) >= set(designs)


def test_test_access_ledger_refuses_second_claim() -> None:
    temporary_root = local_test_directory("gate_b0_b1_test_ledger")
    _, base_config = load_gate_b_config(ROOT)
    config = deepcopy(base_config)
    manifest_target = temporary_root / "artifacts" / "splits" / "t1_v1" / "test.csv"
    manifest_target.parent.mkdir(parents=True)
    shutil.copy2(ROOT / "artifacts" / "splits" / "t1_v1" / "test.csv", manifest_target)
    record_path = temporary_root / config["artifacts"]["candidate_record"]
    record_path.parent.mkdir(parents=True)
    candidate = {
        "candidate_id": "unit-test-candidate",
        "status": "frozen",
    }
    record_path.write_text(json.dumps(candidate), encoding="utf-8")
    first = claim_test_access(
        root=temporary_root, config=config, candidate_record=candidate
    )
    assert first["status"] == "opening"
    with pytest.raises(RepeatedTestAccessError):
        claim_test_access(root=temporary_root, config=config, candidate_record=candidate)
    final = finalize_test_access(
        root=temporary_root, config=config, status="consumed"
    )
    assert final["status"] == "consumed"
    with pytest.raises(RepeatedTestAccessError):
        finalize_test_access(root=temporary_root, config=config, status="consumed")


def test_published_gate_b_artifact_inventory_matches_files() -> None:
    inventory_path = (
        ROOT
        / "artifacts"
        / "model_selection"
        / "t1_b0_b1_v1"
        / "artifact_inventory.csv"
    )
    if not inventory_path.is_file():
        pytest.skip("Gate B0/B1 generated artifacts have not been published yet")
    from skru1.data_contracts import sha256_file

    inventory = pd.read_csv(inventory_path)
    assert inventory["relative_path"].is_unique
    assert "artifacts/model_selection/frozen_candidate.json" in set(inventory["relative_path"])
    for row in inventory.itertuples(index=False):
        path = ROOT / row.relative_path
        assert path.is_file()
        assert path.stat().st_size == row.size_bytes
        assert sha256_file(path) == row.sha256

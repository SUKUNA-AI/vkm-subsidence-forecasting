from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest


torch = pytest.importorskip("torch", reason="Gate C1 checkpoint tests require torch")

from skru1.data_contracts import ContractViolation  # noqa: E402
from skru1.gate_c1_checkpoints import (  # noqa: E402
    INNER_POLICY,
    OUTER_POLICY,
    TopKCheckpointManager,
    validate_checkpoint_manifest,
)


def _local_test_directory(name: str) -> Path:
    directory = Path("work") / "tests" / "gate_c1_checkpoints" / name
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    return directory


def _exercise_manager(test_root: Path, *, role: str, metrics: list[float]):
    torch.manual_seed(42117)
    model = torch.nn.Linear(2, 1)
    optimizer = torch.optim.AdamW(model.parameters(), lr=0.01)
    generator = torch.Generator(device="cpu").manual_seed(42117)
    manager = TopKCheckpointManager(
        root=test_root,
        fit_id=("a" if role == "inner" else "b") * 64,
        role=role,
        keep_top_k=5,
        stage_interval_epochs=2,
        provenance={"model_id": "C01_compact_gru", "fold_id": "fixture", "seed": 42117},
    )
    x = torch.tensor([[1.0, -1.0], [0.5, 2.0]])
    y = torch.tensor([[0.2], [-0.5]])
    for epoch, metric in enumerate(metrics, start=1):
        optimizer.zero_grad(set_to_none=True)
        loss = torch.nn.functional.mse_loss(model(x), y)
        loss.backward()
        optimizer.step()
        manager.observe(
            epoch=epoch,
            metric=metric,
            model=model,
            optimizer=optimizer,
            shuffle_generator=generator,
            stale_epochs=0,
            device=torch.device("cpu"),
            terminal=epoch == len(metrics),
        )
    summary = manager.finalize(model=model)
    manifest = validate_checkpoint_manifest(
        test_root,
        summary["checkpoint_manifest"],
        summary["checkpoint_manifest_sha256"],
        expected_role=role,
    )
    return manager, summary, manifest


def test_inner_checkpoint_keeps_top_five_and_restores_rank_one() -> None:
    test_root = _local_test_directory("inner_top_five")
    manager, summary, manifest = _exercise_manager(
        test_root, role="inner", metrics=[5.0, 1.0, 4.0, 2.0, 3.0, 0.5]
    )
    assert manager.ranking_policy == INNER_POLICY
    assert manifest["retained_checkpoint_count"] == 5
    assert [record["epoch"] for record in manifest["checkpoints"]] == [6, 2, 4, 5, 3]
    assert manifest["selected_epoch"] == summary["selected_checkpoint_epoch"] == 6
    assert manifest["latest_stage"]["terminal"] is True
    assert manifest["outer_labels_used_for_ranking"] is False


def test_outer_checkpoint_keeps_latest_five_and_selects_fixed_final() -> None:
    test_root = _local_test_directory("outer_latest_five")
    manager, summary, manifest = _exercise_manager(
        test_root, role="outer", metrics=[1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]
    )
    assert manager.ranking_policy == OUTER_POLICY
    assert [record["epoch"] for record in manifest["checkpoints"]] == [7, 6, 5, 4, 3]
    assert manifest["selection_policy"] == "fixed_final_epoch"
    assert manifest["selected_epoch"] == summary["selected_checkpoint_epoch"] == 7


def test_terminal_checkpoint_can_be_resumed_fail_closed() -> None:
    test_root = _local_test_directory("terminal_resume")
    original, _, manifest = _exercise_manager(
        test_root, role="inner", metrics=[3.0, 2.0, 1.0, 0.8, 0.7, 0.6]
    )
    model = torch.nn.Linear(2, 1)
    optimizer = torch.optim.AdamW(model.parameters(), lr=0.01)
    generator = torch.Generator(device="cpu").manual_seed(1)
    resumed = TopKCheckpointManager(
        root=test_root,
        fit_id="a" * 64,
        role="inner",
        keep_top_k=5,
        stage_interval_epochs=2,
        provenance={"model_id": "C01_compact_gru", "fold_id": "fixture", "seed": 42117},
    )
    recovery = resumed.resume(
        model=model,
        optimizer=optimizer,
        shuffle_generator=generator,
        device=torch.device("cpu"),
    )
    assert recovery == {"epoch": 6, "stale_epochs": 0, "terminal": True}
    assert resumed.resumed_from_recovery is True
    assert len(resumed.ranked) == 5
    assert manifest["context_sha256"] == original.context_sha256 == resumed.context_sha256


def test_checkpoint_rejects_outer_label_provenance_and_manifest_tampering() -> None:
    test_root = _local_test_directory("tampering")
    with pytest.raises(ContractViolation):
        TopKCheckpointManager(
            root=test_root,
            fit_id="c" * 64,
            role="inner",
            keep_top_k=5,
            stage_interval_epochs=2,
            provenance={"outer_labels": [1.0]},
        )
    _, summary, _ = _exercise_manager(
        test_root, role="inner", metrics=[5.0, 4.0, 3.0, 2.0, 1.0]
    )
    path = test_root / summary["checkpoint_manifest"]
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["selected_epoch"] = 999
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ContractViolation):
        validate_checkpoint_manifest(
            test_root,
            summary["checkpoint_manifest"],
            summary["checkpoint_manifest_sha256"],
            expected_role="inner",
        )

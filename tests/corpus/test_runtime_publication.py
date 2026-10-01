"""Modern canonical validation uses the operator's separately pinned approval."""
from types import SimpleNamespace

import pytest

from test_accounting_publication import publication_seed, prepared, admitted_snapshot
from vkm_corpus.parquet.atomic import sha256_of
from vkm_corpus.update.runtime import BoundFile, RuntimeConfig, UpdateRuntime, _run_operation
from vkm_evidence.contracts import canonical_bytes


def setup_runtime(p, tmp_path, *, approval=True):
    manifest = admitted_snapshot(p)
    manifest_path = tmp_path / "snapshot.json"
    manifest_path.write_bytes(canonical_bytes(manifest))
    approval_path = tmp_path / "operator-approval.json"
    approval_path.write_bytes(canonical_bytes(p.approval))
    config = RuntimeConfig(runtime_root=str(tmp_path / "runtime"), originals_root=str(tmp_path / "originals"),
        canonical_root=str(p.target.root), policy=BoundFile(path=str(p.policy), sha256=sha256_of(p.policy)),
        qualification_root=str(tmp_path / "qualification"), expected_commit="b" * 40,
        memory_budget_gib=3, memory_reserve_gib=1, worker_memory_gib=2, min_free_disk_gib=.001,
        artifacts={"snapshot": BoundFile(path=str(manifest_path), sha256=sha256_of(manifest_path))},
        publication_approval=BoundFile(path=str(approval_path), sha256=sha256_of(approval_path)) if approval else None)
    return UpdateRuntime(config), approval_path


@pytest.mark.parametrize("operation", ["CANON_VALIDATE", "BUILD_SHADOW"])
def test_actual_modern_snapshot_passes_with_operator_approval(prepared, tmp_path, operation):
    rt, _ = setup_runtime(prepared, tmp_path)
    stage = SimpleNamespace(operation=operation, options={"artifact": "snapshot", "kind": "DUCKDB"})
    folder = tmp_path / "attempt"
    folder.mkdir()
    result = _run_operation(rt, None, stage, folder)
    assert result["status"] == "PASS", result
    assert (folder / "shadow.duckdb").is_file() == (operation == "BUILD_SHADOW")


def test_incoming_descriptor_does_not_authorize_runtime_without_operator_binding(prepared, tmp_path):
    rt, _ = setup_runtime(prepared, tmp_path, approval=False)
    stage = SimpleNamespace(operation="CANON_VALIDATE", options={"artifact": "snapshot"})
    result = _run_operation(rt, None, stage, tmp_path)
    assert result["status"] == "FAIL"


def test_changed_operator_approval_and_policy_close_before_validation(prepared, tmp_path):
    rt, path = setup_runtime(prepared, tmp_path)
    stage = SimpleNamespace(operation="CANON_VALIDATE", options={"artifact": "snapshot"})
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="bound input"):
        _run_operation(rt, None, stage, tmp_path)


def test_policy_change_closes_modern_shadow_builder(prepared, tmp_path):
    rt, _ = setup_runtime(prepared, tmp_path)
    prepared.policy.write_bytes(prepared.policy.read_bytes() + b" ")
    stage = SimpleNamespace(operation="BUILD_SHADOW", options={"artifact": "snapshot", "kind": "DUCKDB"})
    with pytest.raises(ValueError, match="bound input"):
        _run_operation(rt, None, stage, tmp_path)
    assert not (tmp_path / "shadow.duckdb").exists()


def test_operator_authority_cannot_be_in_the_incoming_delivery(prepared, tmp_path):
    rt, _ = setup_runtime(prepared, tmp_path)
    values = rt.config.model_dump(mode="python")
    values["publication_approval"] = {"path": str(prepared.target.root / "approval.json"), "sha256": "a" * 64}
    with pytest.raises(ValueError, match="operator-owned"):
        RuntimeConfig.model_validate(values)

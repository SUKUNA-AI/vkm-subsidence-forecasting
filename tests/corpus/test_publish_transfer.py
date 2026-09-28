"""Publication STAGING → CANONICAL and reconcile (CP-15, H-09): markers last, immutable copies, snapshot on PASS."""
from __future__ import annotations

import shutil
import subprocess

import pytest

pytest.importorskip("pyarrow")
pytest.importorskip("duckdb")

from vkm_corpus.config import load_settings  # noqa: E402
from vkm_corpus.publish import transfer  # noqa: E402


def test_publish_order_puts_markers_last_and_never_copies_root_state():
    names = [name for name, _, _ in transfer.PUBLISH_STEPS]
    assert names[-2:] == ["run-markers", "commit-markers"]
    data_excludes = dict((n, ex) for n, _, ex in transfer.PUBLISH_STEPS)["canonical-files"]
    for never in ("/_leases/", "/_admission/", "/_snapshots/", "/CURRENT", "/_commits/", "/_runs/"):
        assert never in data_excludes
    cmd = transfer.rsync_command("a/", "b/", (), dry_run=False)
    assert "--ignore-existing" in cmd and "--inplace" not in cmd


def test_rsync_stats_are_parsed():
    class Proc:
        returncode = 0
        stdout = "Number of regular files transferred: 1,234\nTotal transferred file size: 5,678,901 bytes\n"
        stderr = ""

    step = transfer._run("x", ["rsync"], runner=lambda *a, **k: Proc())
    assert (step.files_transferred, step.bytes_transferred) == (1234, 5678901)


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed: NOT_RUN")
def test_publish_then_reconcile_moves_current(tmp_path, monkeypatch):
    from vkm_corpus.contracts.vocab import RootKind
    from vkm_corpus.parquet.layout import init_root
    from vkm_corpus.parquet.reader import current_snapshot_id
    from vkm_corpus.publish.reconcile import reconcile
    from vkm_corpus.testing import synthetic_canon

    canon = synthetic_canon(tmp_path / "data")
    target = init_root(tmp_path / "core_data", RootKind.CANONICAL)
    first = transfer.publish(canon.staging.root, str(target.root))
    assert first.to_dict()["files_transferred"] > 0
    assert not (target.canonical / "_leases").exists()
    again = transfer.publish(canon.staging.root, str(target.root))
    assert again.to_dict()["files_transferred"] == 0          # immutable: nothing rewritten

    monkeypatch.setenv("VKM_DATA_ROOT", str(target.root))
    monkeypatch.setenv("VKM_DATA_ROLE", "canonical")
    receipt = reconcile(load_settings(), run_id="RUN-20260928T000000Z-0000abcd", graph=False, search=False)
    assert receipt["snapshot"]["status"] == "PASS", receipt
    assert receipt["result"] == "RECONCILED"
    assert current_snapshot_id(target) == receipt["snapshot"]["snapshot_id"]
    assert (target.root / "receipts" / "reconcile" / "RUN-20260928T000000Z-0000abcd.json").is_file()
    with pytest.raises(Exception):
        transfer.publish(target.root, str(tmp_path / "elsewhere"))   # a CANONICAL root is never a publish source

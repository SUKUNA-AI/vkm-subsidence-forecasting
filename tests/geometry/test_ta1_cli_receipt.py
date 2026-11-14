"""Portable CLI control-flow tests; TA1 helpers are explicit fakes, no native runtime."""
import copy
import hashlib
import json
import sys

import pytest

from vkm_geometry import ta1_worker as worker
from vkm_geometry.transport import assert_rows_equal, batch_rows, read_wkb_jsonl, write_wkb_jsonl
from test_transport import synthetic_batch


def cli_fixture(monkeypatch, tmp_path, action, failure=None):
    rows = batch_rows(synthetic_batch())
    source_rows = tmp_path / "source.jsonl"
    write_wkb_jsonl(rows, source_rows)
    source = tmp_path / "original.gpkg"
    source.write_bytes(b"synthetic original: never modified")
    target = tmp_path / "target.gpkg"
    readback = tmp_path / "readback.jsonl"
    if failure == "missing_parent": readback = tmp_path / "absent" / "readback.jsonl"
    receipt = tmp_path / "receipt.json"
    helper_called = []

    def completed_helper(first, destination):
        if action == "write": assert_rows_equal(rows, first)
        else: assert first == source and first.read_bytes() == b"synthetic original: never modified"
        assert destination == target
        helper_called.append(action)
        destination.write_bytes(b"synthetic helper output, not a real GeoPackage")
        return {"status": "PASS", "helper_phase": "COMPLETED"}

    def actual_readback(path):
        assert path == target
        if failure == "read": raise ValueError("synthetic post-helper read failure")
        return copy.deepcopy(rows)

    def failed_write(actual, path):
        # A real partial artifact must not make the final receipt a success.
        path.write_bytes(b"partial output")
        raise OSError("synthetic output write failure")

    def disappeared_package(actual, path):
        write_wkb_jsonl(actual, path)
        target.unlink()

    monkeypatch.setattr(worker, "write_ta1" if action == "write" else "repair_srs_copy", completed_helper)
    monkeypatch.setattr(worker, "read_ta1", actual_readback)
    if failure == "write": monkeypatch.setattr(worker, "write_wkb_jsonl", failed_write)
    elif failure == "checksum": monkeypatch.setattr(worker, "write_wkb_jsonl", disappeared_package)
    argv = ["ta1_worker", action, "--gpkg", str(target), "--readback", str(readback), "--receipt", str(receipt)]
    argv += ["--rows", str(source_rows)] if action == "write" else ["--source-gpkg", str(source)]
    monkeypatch.setattr(sys, "argv", argv)
    return rows, source, source_rows, target, readback, receipt, helper_called


@pytest.mark.parametrize("action", ["write", "repair-srs"])
@pytest.mark.parametrize("failure,error", [("read", "ValueError"), ("missing_parent", "FileNotFoundError"),
                                           ("write", "OSError"), ("checksum", "FileNotFoundError")])
def test_cli_post_helper_failure_cannot_keep_pass(monkeypatch, tmp_path, capsys, action, failure, error):
    rows, source, source_rows, target, readback, receipt, called = cli_fixture(monkeypatch, tmp_path, action, failure)
    original = source.read_bytes(); original_rows = source_rows.read_bytes()
    exit_code = worker.main()
    emitted = json.loads(capsys.readouterr().out)
    saved = json.loads(receipt.read_text(encoding="utf-8"))
    assert called == [action] and saved["helper_phase"] == "COMPLETED"
    assert exit_code == 1 and saved["status"] == emitted["status"] == "FAIL"
    assert saved == emitted and saved["error"].startswith(error + ":")
    assert source.read_bytes() == original and source_rows.read_bytes() == original_rows
    if failure == "missing_parent": assert not readback.exists()
    elif failure == "write": assert readback.read_bytes() == b"partial output"
    elif failure == "checksum": assert_rows_equal(rows, read_wkb_jsonl(readback))


@pytest.mark.parametrize("action", ["write", "repair-srs"])
def test_cli_pass_requires_completed_readback_and_checksum(monkeypatch, tmp_path, capsys, action):
    rows, source, source_rows, target, readback, receipt, called = cli_fixture(monkeypatch, tmp_path, action)
    original = source.read_bytes(); original_rows = source_rows.read_bytes()
    assert worker.main() == 0
    saved = json.loads(receipt.read_text(encoding="utf-8"))
    assert saved == json.loads(capsys.readouterr().out)
    assert saved["status"] == "PASS" and called == [action]
    assert saved["readback_features"] == len(rows)
    assert saved["gpkg_sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert_rows_equal(rows, read_wkb_jsonl(readback))
    assert source.read_bytes() == original and source_rows.read_bytes() == original_rows

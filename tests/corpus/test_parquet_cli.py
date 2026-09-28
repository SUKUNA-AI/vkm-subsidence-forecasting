"""CLI groups of agent D (``canon``, ``duckdb``, ``registry``) through ``vkm_corpus.cli.main`` with env configuration."""
from __future__ import annotations

import json

import pytest

pytest.importorskip("pyarrow")
pytest.importorskip("duckdb")

from vkm_corpus import cli  # noqa: E402


def run(capsys, *argv) -> tuple[int, dict]:
    code = cli.main(list(argv))
    out = capsys.readouterr().out
    return code, (json.loads(out) if out.strip().startswith("{") else {})


def test_canon_init_and_status(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VKM_DATA_ROOT", str(tmp_path / "root"))
    monkeypatch.delenv("VKM_DATA_ROLE", raising=False)
    assert run(capsys, "canon", "init", "--kind", "STAGING")[1] == {"root_kind": "STAGING"}
    code, status = run(capsys, "canon", "status")
    assert code == 0 and status["root_kind"] == "STAGING" and status["current"] is None


def test_canon_validate_and_duckdb_on_synthetic(tmp_path, monkeypatch, capsys):
    from vkm_corpus.testing.synthetic import synthetic_canon

    canon = synthetic_canon(tmp_path / "data")
    monkeypatch.setenv("VKM_DATA_ROOT", str(canon.root))
    monkeypatch.setenv("VKM_DATA_ROLE", "canonical")
    code, rep = run(capsys, "canon", "validate", "--deep")
    assert code == 0 and rep["status"] == "PASS" and rep["counts"]["sources_total"] == 11
    code, rep = run(capsys, "canon", "validate", "--acceptance")
    assert code == 1 and any(c["check_id"] == "F02" for c in rep["not_pass"])
    code, built = run(capsys, "duckdb", "build")
    assert code == 0 and built["fingerprints_verified"] and built["snapshot_id"] == canon.snapshot_id
    code, st = run(capsys, "duckdb", "status")
    assert code == 0 and st["up_to_date"]
    code, adm = run(capsys, "canon", "admit")
    assert code == 0 and adm == {"admitted": [], "pending": [], "rejected": []}
    code, gc = run(capsys, "canon", "gc")
    assert code == 0 and gc["orphans"] == [] and gc["deleted"] is False

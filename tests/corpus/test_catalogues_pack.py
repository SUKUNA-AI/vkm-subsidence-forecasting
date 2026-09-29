"""Catalogue pack (``vkm_corpus.catalogues``): CSV → one DuckDB file + manifest, publish with CURRENT, the read-only
store, the CLI; synthetic CSVs plus one integration pass over the repository's own PUBLIC catalogues (row counts)."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")

from vkm_corpus.catalogues import pack as cpack  # noqa: E402
from vkm_corpus.catalogues.store import CatalogueStore, CataloguesUnavailable  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
COMMIT = "0123456789abcdef0123456789abcdef01234567"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "public"
    _write(r / "catalogues" / "physics" / "processes.csv",
           '﻿process_id,value,notes,\nPC-01,"0,45",,x\nPC-02,,"строка 1\nстрока 2",\n')
    _write(r / "catalogues" / "maths" / "MODELS.csv", "model_id,Name,name\nMM-1,a,b\n")
    _write(r / "evidence" / "a" / "conflicts.csv", "conflict_id\nC-1\n")
    _write(r / "evidence" / "b" / "conflicts.csv", "conflict_id\nC-2\nC-3\n")
    _write(r / "evidence" / "b" / "notes.md", "not a catalogue\n")
    return r


def test_table_names_are_stems_or_paths():
    names = cpack.table_names(["evidence/a/conflicts.csv", "evidence/b/conflicts.csv", "catalogues/x/MODELS.csv",
                               "catalogues/x/catalogue_meta.csv", "catalogues/x/2020 list.csv"])
    assert names["catalogues/x/MODELS.csv"] == "models"
    assert names["evidence/a/conflicts.csv"] == "evidence_a_conflicts"
    assert names["evidence/b/conflicts.csv"] == "evidence_b_conflicts"
    assert names["catalogues/x/catalogue_meta.csv"] == "catalogues_x_catalogue_meta"   # never a meta table
    assert names["catalogues/x/2020 list.csv"] == "t_2020_list"


def test_pack_keeps_cells_verbatim_and_writes_the_manifest(repo, tmp_path):
    out = tmp_path / "pack"
    manifest = cpack.pack(repo, out, commit=COMMIT)
    assert manifest["pack_id"] == COMMIT[:12] and manifest["git_commit"] == COMMIT
    assert manifest["working_tree"] == "not verified (git unavailable)" and manifest["commit_source"] == "argument"
    files = {f["path"]: f for f in manifest["files"]}
    assert set(files) == {"catalogues/physics/processes.csv", "catalogues/maths/MODELS.csv",
                          "evidence/a/conflicts.csv", "evidence/b/conflicts.csv"}             # .md skipped
    assert files["catalogues/physics/processes.csv"]["rows"] == 2
    assert files["catalogues/physics/processes.csv"]["columns"] == ["process_id", "value", "notes", "column_4"]
    assert files["catalogues/maths/MODELS.csv"]["columns"] == ["model_id", "Name", "name_2"]   # case-insensitive
    assert files["evidence/b/conflicts.csv"]["sha256"] == cpack.sha256_file(repo / "evidence/b/conflicts.csv")
    assert (out / "manifest.json").is_file() and json.loads((out / "manifest.json").read_text("utf-8")) == manifest
    assert manifest["db"]["sha256"] == cpack.sha256_file(out / "catalogues.duckdb")
    con = duckdb.connect(str(out / "catalogues.duckdb"), read_only=True)
    try:
        rows = con.execute('SELECT process_id, value, notes, "_csv_row" FROM processes ORDER BY "_csv_row"').fetchall()
        assert rows == [("PC-01", "0,45", None, 1), ("PC-02", None, "строка 1\nстрока 2", 2)]   # no type inference
        assert con.execute("SELECT count(*) FROM evidence_b_conflicts").fetchone()[0] == 2
        types = {t for (t,) in con.execute("SELECT DISTINCT data_type FROM duckdb_columns() WHERE table_name = "
                                           "'processes' AND column_name <> '_csv_row'").fetchall()}
        assert types == {"VARCHAR"}
        meta = json.loads(con.execute("SELECT meta_json FROM catalogue_meta").fetchone()[0])
        assert meta["pack_id"] == COMMIT[:12] and meta["content_sha256"] == manifest["content_sha256"]
    finally:
        con.close()


def test_pack_refusals(repo, tmp_path, monkeypatch):
    with pytest.raises(cpack.PackError, match="outside the repository"):
        cpack.pack(repo, repo / "out", commit=COMMIT)
    with pytest.raises(cpack.PackError, match="unknown"):
        cpack.pack(repo, tmp_path / "p")                                  # no git, no --commit
    with pytest.raises(cpack.PackError, match="hex"):
        cpack.pack(repo, tmp_path / "p", commit="not-a-commit")
    monkeypatch.setattr(cpack, "git_state", lambda *a, **k: {"commit": COMMIT, "dirty_paths": ["evidence/a/x.csv"]})
    with pytest.raises(cpack.PackError, match="differ from commit"):
        cpack.pack(repo, tmp_path / "p")
    dirty = cpack.pack(repo, tmp_path / "p", allow_dirty=True)
    assert dirty["pack_id"].startswith(COMMIT[:12] + "-dirty-") and dirty["working_tree"] == "dirty"
    with pytest.raises(cpack.PackError, match="HEAD"):
        cpack.pack(repo, tmp_path / "q", commit="f" * 40)                 # the files are those of HEAD
    monkeypatch.setattr(cpack, "git_state", lambda *a, **k: {"commit": COMMIT, "dirty_paths": []})
    assert cpack.pack(repo, tmp_path / "r")["working_tree"] == "clean"


def test_publish_store_and_republish(repo, tmp_path):
    data_root = tmp_path / "data"
    store = CatalogueStore(data_root)
    with pytest.raises(CataloguesUnavailable):
        store.meta()
    cpack.pack(repo, tmp_path / "pack1", commit=COMMIT)
    info = cpack.publish(data_root, tmp_path / "pack1")
    assert info["pack_id"] == COMMIT[:12]
    assert (data_root / "derived" / "catalogues" / "CURRENT").read_bytes() == (COMMIT[:12] + "\n").encode()
    assert store.pack_id() == COMMIT[:12] and store.meta()["n_files"] == 4 and "files" not in store.meta()
    assert set(store.tables()) == {"processes", "models", "evidence_a_conflicts", "evidence_b_conflicts"}
    rows = store.rows("processes", ["process_id", "value", "nope"])
    assert rows == [{"process_id": "PC-01", "value": "0,45"}, {"process_id": "PC-02", "value": None}]
    assert store.rows("processes", ["process_id", "value", "nope"]) is rows            # cached per pack
    assert store.rows("no_such_table") == []
    assert store.query("SELECT count(*) AS n FROM models")[0]["n"] == 1
    # a new pack (another commit, one more row) replaces the served one on the next call
    _write(repo / "evidence" / "a" / "conflicts.csv", "conflict_id\nC-1\nC-9\n")
    other = "fedcba9876543210fedcba9876543210fedcba98"
    cpack.pack(repo, tmp_path / "pack2", commit=other)
    cpack.publish(data_root, tmp_path / "pack2")
    assert store.pack_id() == other[:12] and len(store.rows("evidence_a_conflicts")) == 2
    # a pack whose DB does not match its manifest is refused
    (tmp_path / "pack2" / "catalogues.duckdb").write_bytes(b"broken")
    with pytest.raises(cpack.PackError, match="does not match"):
        cpack.publish(data_root, tmp_path / "pack2")
    with pytest.raises(cpack.PackError, match="manifest"):
        cpack.publish(data_root, tmp_path / "nothing")


def test_cli_pack_publish_status(repo, tmp_path, capsys):
    from vkm_corpus.cli import main

    assert main(["catalogues", "pack", "--repo", str(repo), "--out", str(tmp_path / "pk"), "--commit", COMMIT]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["pack_id"] == COMMIT[:12] and out["tables"]["processes"] == 2
    assert main(["catalogues", "publish", "--pack", str(tmp_path / "pk"), "--data-root", str(tmp_path / "d")]) == 0
    assert json.loads(capsys.readouterr().out)["pack_id"] == COMMIT[:12]
    assert main(["catalogues", "status", "--data-root", str(tmp_path / "d")]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["published"] and status["n_tables"] == 4
    assert main(["catalogues", "status", "--data-root", str(tmp_path / "none")]) == 1
    assert json.loads(capsys.readouterr().out)["published"] is False


def test_the_public_catalogues_pack_with_their_row_counts(tmp_path, monkeypatch):
    """Integration: every CSV under catalogues/ and evidence/ of this checkout parses into its table (the git state
    is fixed here: a worktree may not be readable by git in every environment)."""
    monkeypatch.setattr(cpack, "git_state", lambda *a, **k: {"commit": COMMIT, "dirty_paths": []})
    manifest = cpack.pack(ROOT, tmp_path / "pack")
    expected = {}
    for rel in cpack.find_files(ROOT):
        with open(ROOT / rel, encoding="utf-8-sig", newline="") as fh:
            expected[rel] = sum(1 for row in csv.reader(fh) if row) - 1
    assert {f["path"]: f["rows"] for f in manifest["files"]} == expected
    names = [f["table"] for f in manifest["files"]]
    assert len(names) == len(set(names))
    for needed in ("physics_coverage_and_execution_matrix", "process_evidence_links", "physics_conflicts",
                   "mathematical_model_registry", "formula_conflicts", "record_to_model_map", "causal_graph_nodes",
                   "causal_graph_edges", "observation_operator_design", "source_coverage_master"):
        assert needed in names

"""Serving store of the navigation layer (navigation.store) and its API/MCP plumbing, on synthetic data."""
from __future__ import annotations

from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")
pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")

from vkm_corpus.navigation import store  # noqa: E402

SNAP = "snap-20260928T160616Z-5d669f09"


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    (root / "duckdb").mkdir(parents=True)
    con = duckdb.connect(str(root / "duckdb" / "vkm_corpus.duckdb"))
    con.execute("CREATE SCHEMA canonical")
    con.execute("CREATE TABLE canonical.pages AS SELECT 'VKM-SRC-901:p0001' AS page_id, 'VKM-SRC-901' AS source_id")
    con.close()
    nav_dir = root / "derived" / "navigation" / SNAP
    nav_dir.mkdir(parents=True)
    pq.write_table(pa.table({"section_id": ["SEC-0000000000000001", "SEC-0000000000000002"],
                             "source_id": ["VKM-SRC-901", "VKM-SRC-901"], "level": [1, 2],
                             "title": ["Глава 3. Ползучесть соли", "3.2 Длительная прочность"],
                             "title_path": ["Глава 3. Ползучесть соли", "Глава 3 › 3.2 Длительная прочность"],
                             "page_start_id": ["VKM-SRC-901:p0001", "VKM-SRC-901:p0001"],
                             "page_end_id": ["VKM-SRC-901:p0001", "VKM-SRC-901:p0001"],
                             "method": ["PDF_OUTLINE", "PDF_OUTLINE"]}), nav_dir / "sections.parquet")
    (nav_dir / "manifest.json").write_text('{"snapshot_id": "%s", "rule_versions": {"sections": "sections_v1"}}' % SNAP,
                                           encoding="utf-8")
    return root


def test_pack_publish_and_serve(tmp_path):
    root = _root(tmp_path)
    nav = store.NavStore(root, functions={"outline": lambda con, sid: con.execute(
        "SELECT s.section_id, s.level FROM sections s JOIN canonical.pages p ON p.source_id = s.source_id "
        "WHERE s.source_id = ? ORDER BY s.level", [sid]).fetchall()})
    with pytest.raises(store.NavUnavailable):
        nav.snapshot_id()                                     # nothing published yet
    info = store.pack(root / "derived" / "navigation" / SNAP)
    assert info["tables"] == {"sections": 2}
    store.publish(root, SNAP)
    assert nav.snapshot_id() == SNAP and nav.meta()["counts"] == {"sections": 2}
    assert nav.run("outline", "VKM-SRC-901") == [("SEC-0000000000000001", 1), ("SEC-0000000000000002", 2)]
    hits = nav.search_sections("ползучесть соли")
    assert hits and hits[0]["section_id"] == "SEC-0000000000000001" and hits[0]["score"] == 2
    assert nav.search_sections("прочность", source_id="VKM-SRC-901")[0]["level"] == 2


def test_missing_query_module_is_unavailable(tmp_path):
    root = _root(tmp_path)
    store.pack(root / "derived" / "navigation" / SNAP)
    store.publish(root, SNAP)
    nav = store.NavStore(root)
    store.QUERY_FUNCTIONS["__missing__"] = "vkm_corpus.navigation.no_such_module:fn"
    try:
        with pytest.raises(store.NavUnavailable):
            nav.run("__missing__")
    finally:
        del store.QUERY_FUNCTIONS["__missing__"]


def test_publish_requires_a_packed_layer(tmp_path):
    root = _root(tmp_path)
    with pytest.raises(store.NavUnavailable):
        store.publish(root, SNAP)

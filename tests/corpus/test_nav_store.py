"""Serving store of the navigation layer (navigation.store) and its API/MCP plumbing, on synthetic data."""
from __future__ import annotations

from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")
pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")

from nav_manifest_fixture import write_nav_manifest  # noqa: E402
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
    write_nav_manifest(root / "derived" / "navigation" / SNAP, SNAP)
    info = store.pack(root / "derived" / "navigation" / SNAP)
    assert info["tables"] == {"sections": 2}
    store.publish(root, SNAP)
    assert nav.snapshot_id() == SNAP and nav.meta()["counts"] == {"sections": 2}
    assert nav.run("outline", "VKM-SRC-901") == [("SEC-0000000000000001", 1), ("SEC-0000000000000002", 2)]
    hits = nav.search_sections("ползучести солей")                    # lemmas, not substrings of the query
    assert hits and hits[0]["section_id"] == "SEC-0000000000000001" and 1.0 <= hits[0]["score"] <= 1.15
    assert nav.search_sections("прочность", source_id="VKM-SRC-901")[0]["level"] == 2
    assert nav.search_sections("гидрогеология") == [] and nav.search_sections("и в на") == []
    # the datasets of the served build: a part not built is a missing dependency, not a crash of its query
    assert nav.datasets() == frozenset({"sections"})
    nav.require("sections")
    with pytest.raises(store.NavUnavailable, match="has no table_structure, table_cells"):
        nav.require("sections", "table_structure", "table_cells")


def test_search_sections_lemmas_key_terms_and_depth(tmp_path):
    root = tmp_path / "data"
    (root / "duckdb").mkdir(parents=True)
    duckdb.connect(str(root / "duckdb" / "vkm_corpus.duckdb")).close()
    nav_dir = root / "derived" / "navigation" / SNAP
    nav_dir.mkdir(parents=True)
    ids = [f"SEC-000000000000000{i}" for i in range(1, 5)]
    pq.write_table(pa.table({
        "section_id": ids, "source_id": ["VKM-SRC-901"] * 4, "level": [1, 2, 2, 1],
        "title": ["Глава 1. Закладка", "1.1 Закладка камер", "1.2 Консолидация", "Глава 2. Прочее"],
        "title_path": ["Глава 1. Закладка", "Глава 1 › 1.1 Закладка камер", "Глава 1 › 1.2 Консолидация",
                       "Глава 2. Прочее"],
        "key_terms": [[], [], ["закладочный массив", "закладка"], []],
        "page_start_index": [1, 1, 5, 20], "page_end_index": [19, 4, 19, 30],
        "page_start_id": ["VKM-SRC-901:p0001"] * 4, "page_end_id": ["VKM-SRC-901:p0019"] * 4,
        "method": ["PDF_OUTLINE"] * 4}), nav_dir / "sections.parquet")
    write_nav_manifest(nav_dir, SNAP)
    store.pack(nav_dir)
    store.publish(root, SNAP)
    nav = store.NavStore(root)
    hits = nav.search_sections("закладки")
    got = [h["section_id"] for h in hits]
    assert got[:2] == [ids[1], ids[0]]                    # equal title score: the subsection before the chapter
    assert ids[2] in got and ids[3] not in got            # a key-term match counts (less than a title)
    by = {h["section_id"]: h for h in hits}
    assert by[ids[2]]["score"] < by[ids[0]]["score"] and by[ids[1]]["matched"]


def test_missing_query_module_is_unavailable(tmp_path):
    root = _root(tmp_path)
    write_nav_manifest(root / "derived" / "navigation" / SNAP, SNAP)
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

"""Traversal is exact even for sparse row numbers; stale content or snapshot is rejected."""
import json

import pytest

duckdb = pytest.importorskip("duckdb")

from vkm_corpus.navigation.tables_query import get_table_structured


@pytest.fixture()
def con():
    c = duckdb.connect()
    c.execute("CREATE TABLE table_structure(table_id VARCHAR, nav_table_id VARCHAR, n_cols INTEGER, n_rows INTEGER)")
    c.execute("INSERT INTO table_structure VALUES ('table-a','TBL-a', 1, 1000), ('table-b','TBL-b',1,1000)")
    c.execute("CREATE TABLE table_columns(table_id VARCHAR, block INTEGER, col INTEGER, role VARCHAR, header_text VARCHAR)")
    c.execute('''CREATE TABLE table_cells(table_id VARCHAR, cell_id VARCHAR, "row" INTEGER, col INTEGER,
              text VARCHAR, text_clean VARCHAR, row_role VARCHAR, block INTEGER, band INTEGER, is_header BOOLEAN)''')
    c.executemany("INSERT INTO table_cells VALUES (?,?,?,?,?,?,?,?,?,?)", [
        (tid, tid + str(row), row, 0, "raw " + str(row), str(row), "DATA", 0, 0, False)
        for tid in ("table-a", "table-b") for row in (0, 2, 999)])
    c.execute("CREATE TABLE nav_meta(meta_json VARCHAR)")
    c.execute("INSERT INTO nav_meta VALUES (?)", [json.dumps({"snapshot_id": "snap-one"})])
    yield c
    c.close()


def test_sparse_pagination_returns_every_cell_once(con):
    first = get_table_structured(con, "table-a", max_rows=2)
    assert [r["row"] for r in first["rows"]] == [0, 2]
    assert first["pagination"]["total_cells"] == 3
    last = get_table_structured(con, "TBL-a", max_rows=2, cursor=first["pagination"]["next_cursor"])
    assert [r["row"] for r in last["rows"]] == [999]
    assert not last["pagination"]["has_more"] and last["pagination"]["next_cursor"] is None


@pytest.mark.parametrize("change", ["content", "snapshot", "other_table", "malformed"])
def test_changed_content_or_snapshot_or_wrong_cursor_rejected(con, change):
    cursor = get_table_structured(con, "table-a", max_rows=1)["pagination"]["next_cursor"]
    table = "table-a"
    if change == "content":
        con.execute("UPDATE table_cells SET text='changed raw only' WHERE cell_id='table-a999'")
    elif change == "snapshot":
        con.execute("UPDATE nav_meta SET meta_json=?", [json.dumps({"snapshot_id": "snap-two"})])
    elif change == "other_table":
        table = "table-b"
    else:
        cursor = "[]"
    with pytest.raises(ValueError, match="cursor"):
        get_table_structured(con, table, max_rows=1, cursor=cursor)


def test_ambiguous_table_identity_is_rejected(con):
    con.execute("INSERT INTO table_structure SELECT * FROM table_structure WHERE table_id='table-a'")
    with pytest.raises(ValueError, match="ambiguous"):
        get_table_structured(con, "table-a")

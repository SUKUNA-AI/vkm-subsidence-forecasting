"""Native connection identity must match the file hashed by production admission."""
import shutil

import duckdb
import pytest

from vkm_corpus.api.canon import CanonStore
from vkm_corpus.api.production import bind_generation_guard
from test_production_serving import setup, requires_linux_receiver


@requires_linux_receiver
@pytest.mark.parametrize("mutation", ["backend", "connection"])
def test_native_canon_connection_cannot_borrow_another_qualified_path(tmp_path, mutation):
    deps, runtime, cfg, root = setup(tmp_path)
    guard = bind_generation_guard(deps, runtime, cfg, root)
    assert guard()["status"] == "READY"
    selected = deps.canon.path
    # A different, real DuckDB contains content absent from the qualified file.
    other = tmp_path / "unqualified.duckdb"
    shutil.copyfile(selected, other)
    with duckdb.connect(str(other)) as con:
        con.execute("ALTER TABLE meta.snapshot ADD COLUMN IF NOT EXISTS built_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP")
        con.execute("ALTER TABLE meta.snapshot ADD COLUMN IF NOT EXISTS duckdb_version VARCHAR DEFAULT 'synthetic'")
        con.execute("CREATE TABLE IF NOT EXISTS meta.commits(commit_key VARCHAR, commit_id VARCHAR)")
        con.execute("CREATE TABLE unqualified_payload AS SELECT 'synthetic-private-canary' AS value")
    alternate = CanonStore(other)
    assert alternate.query("SELECT value FROM unqualified_payload") == [{"value": "synthetic-private-canary"}]
    if mutation == "backend":
        # A stale/reconfigured backend still reports the pinned path. Its
        # connected database is independently observable via duckdb_databases().
        alternate.path = selected
        alternate._stamp = alternate._file_stamp()
        deps.canon = alternate
    else:
        if not isinstance(deps.canon, CanonStore):
            # Historical qualification fixtures used a path-only facade. The
            # challenge nevertheless exercises a genuine CanonStore connection.
            deps.canon = CanonStore(selected)
        deps.canon._con = alternate._con
        deps.canon._stamp = deps.canon._file_stamp()
    assert deps.canon.query("SELECT value FROM unqualified_payload") == [{"value": "synthetic-private-canary"}]
    assert guard()["status"] == "UNAVAILABLE", "native database differs despite the qualified path attribute"

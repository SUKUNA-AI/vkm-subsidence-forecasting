"""Root marker (H-07), leases and parent chains (H-08), no-op commits (H-34), admission (H-09), run markers (H-11),
snapshots and CURRENT, GC of orphans older than 24 h."""
from __future__ import annotations

import json
import os
import time

import pytest

pytest.importorskip("pyarrow")
pytest.importorskip("duckdb")

from vkm_corpus.parquet import admit as admit_mod  # noqa: E402
from vkm_corpus.parquet.commits import (  # noqa: E402
    CommitConflict,
    LeaseError,
    acquire_lease,
    commit_source,
    list_markers,
    local_head,
)
from vkm_corpus.parquet.gc import collect_garbage, find_orphans  # noqa: E402
from vkm_corpus.parquet.layout import RootError, init_root  # noqa: E402
from vkm_corpus.parquet.runs import RunRecorder, list_runs, run_files  # noqa: E402
from vkm_corpus.parquet.snapshot import build_snapshot  # noqa: E402
from vkm_corpus.testing import rows as R  # noqa: E402

DOC = ("documents", "pages", "blocks", "figures", "tables", "formulas", "bibliography_entries")


def _tables(text="альфа бета"):
    t = {n: [] for n in DOC}
    t["documents"] = [R.make_document(page_count=1)]
    t["pages"] = [R.make_page(index=1, text=text)]
    t["blocks"] = [R.make_block(text=text)]
    return t


def _artifacts():
    return [{"artifact_id": R.artifact(f"native-raw-{R.SID}-1"), "artifact_kind": "NATIVE_RAW",
             "media_type": "application/json", "size_bytes": None, "storage_relpath": None,
             "retention_class": "KEEP_RAW", "materialization": "NOT_STORED_REPRODUCIBLE", "created_by_run_id": R.RUN_ID,
             "created_at": R.T0, "recipe": {"tool": "t", "tool_version": "1", "profile": "p", "params_json": "{}"}}]


def test_root_marker_rules(tmp_path, monkeypatch):
    st = init_root(tmp_path / "s", "STAGING")
    with pytest.raises(RootError):
        init_root(tmp_path / "s", "CANONICAL")                     # a root never changes kind
    with pytest.raises(RootError):
        build_snapshot(st)                                         # no snapshots on STAGING
    with pytest.raises(RootError):
        admit_mod.admit(st)
    canon = init_root(tmp_path / "c", "CANONICAL")
    monkeypatch.setenv("VKM_DATA_ROLE", "producer")
    with pytest.raises(RootError, match="contradicts"):
        canon.require("CANONICAL")
    with pytest.raises(RootError):
        from vkm_corpus.parquet.layout import CanonLayout
        CanonLayout(tmp_path / "unmarked").kind()


def test_lease_is_exclusive_and_required(tmp_path):
    st = init_root(tmp_path / "s", "STAGING")
    lease = acquire_lease(st, R.SID, R.RUN_ID)
    with pytest.raises(LeaseError):
        acquire_lease(st, R.SID, "RUN-20260928T100000Z-ffffffff")
    lease.release()
    with pytest.raises(LeaseError):
        commit_source(st, source_id=R.SID, source_sha256=R.SOURCE_SHA, run_id=R.RUN_ID, tables=_tables())


def test_parent_chain_noop_and_conflict(tmp_path):
    st = init_root(tmp_path / "s", "STAGING")
    with acquire_lease(st, R.SID, R.RUN_ID):
        c1 = commit_source(st, source_id=R.SID, source_sha256=R.SOURCE_SHA, run_id=R.RUN_ID, tables=_tables(),
                           artifact_rows=_artifacts())
        assert c1.parent_commit_id is None and not c1.noop
        again = commit_source(st, source_id=R.SID, source_sha256=R.SOURCE_SHA, run_id=R.RUN_ID, tables=_tables(),
                              artifact_rows=_artifacts())
        assert again.noop and again.commit_id == c1.commit_id and len(list_markers(st)) == 1
        c2 = commit_source(st, source_id=R.SID, source_sha256=R.SOURCE_SHA, run_id=R.RUN_ID,
                           tables=_tables("другой текст"), artifact_rows=_artifacts())
        assert c2.parent_commit_id == c1.commit_id and local_head(st, R.SID) == c2.commit_id
        with pytest.raises(CommitConflict):
            commit_source(st, source_id=R.SID, source_sha256=R.SOURCE_SHA, run_id=R.RUN_ID,
                          tables=_tables("третий"), parent_commit_id=c1.commit_id)
    with pytest.raises(ValueError, match="every dataset"):
        with acquire_lease(st, R.SID, R.RUN_ID):
            commit_source(st, source_id=R.SID, source_sha256=R.SOURCE_SHA, run_id=R.RUN_ID,
                          tables={"pages": []})


def test_commit_rejects_rows_of_another_source(tmp_path):
    st = init_root(tmp_path / "s", "STAGING")
    with acquire_lease(st, R.SID, R.RUN_ID):
        with pytest.raises(ValueError, match="another source"):
            commit_source(st, source_id=R.SID, source_sha256="f" * 64, run_id=R.RUN_ID, tables=_tables())


@pytest.fixture(scope="module")
def canon(tmp_path_factory):
    from vkm_corpus.testing.synthetic import synthetic_canon

    return synthetic_canon(tmp_path_factory.mktemp("commits") / "data")


def test_synthetic_canon_is_admitted_and_current(canon):
    from vkm_corpus.parquet.reader import current_snapshot_id

    assert current_snapshot_id(canon.layout) == canon.snapshot_id
    adm = admit_mod.load_admissions(canon.layout)
    assert adm and all(r["status"] == "ADMITTED" for r in adm.values())
    assert set(canon.manifest["source_heads"]) == {f"VKM-SRC-{n:03d}" for n in (1, 2, 5, 23, 25, 42, 202, 249)}
    assert canon.manifest["registry_head"]
    runs = canon.manifest["runs"]
    assert runs[canon.ids["crashed_run"]] == {"has_end": False, "status": "STARTED", "run_kind": "EXTRACTION"}


def test_crashed_run_journal_is_visible(canon):
    info = list_runs(canon.layout)[canon.ids["crashed_run"]]
    assert info.crashed
    files = run_files(canon.layout, info)
    assert any(f.dataset == "processing_steps" for f in files)                # discovered part of the crash
    paths = {f["path"] for f in canon.manifest["datasets"]["processing_steps"]["files"]}
    assert any(canon.ids["crashed_run"] in p for p in paths)


def _publish_commit(canon, text, parent="auto"):
    """A new commit of 202 on the staging root, published (copied) to the canonical root."""
    from vkm_corpus.testing.synthetic import publish_to_canonical

    st = canon.staging
    run = RunRecorder(st, run_kind="EXTRACTION", cli_command="test", host_role="WORKSTATION").start()
    sid = "VKM-SRC-202"
    src = next(r for r in __import__("vkm_corpus.parquet.reader", fromlist=["rows"]).rows(
        canon.layout, "sources", canon.manifest) if r["source_id"] == sid)
    from vkm_corpus.contracts.builders import SourceContext
    from vkm_corpus.testing.synthetic import _SourceBuild, build_source_objects

    b = _SourceBuild(SourceContext.from_source_row(src), run.started_at, run.run_id, st)
    build_source_objects(b, 202, {})
    if text:
        b.rows["documents"] = [d.model_copy(update={"file_meta_title": text}) for d in b.rows["documents"]]
        from vkm_corpus.contracts.builders import content_sha256
        b.rows["documents"] = [d.model_copy(update={"content_sha256": content_sha256("documents", d)})
                               for d in b.rows["documents"]]
    kwargs = {} if parent == "auto" else {"parent_commit_id": parent}
    with acquire_lease(st, sid, run.run_id):
        res = commit_source(st, source_id=sid, source_sha256=src["source_sha256"], run_id=run.run_id,
                            tables=b.rows, artifact_rows=list(b.artifacts.values()), **kwargs)
    run.note_commit(res.commit_id)
    run.end("SUCCEEDED")
    publish_to_canonical(st, canon.root)
    return res


def test_admission_conflict_does_not_block_others(tmp_path):
    from vkm_corpus.testing.synthetic import synthetic_canon

    canon = synthetic_canon(tmp_path / "data")
    head = canon.manifest["source_heads"]["VKM-SRC-202"]
    good = _publish_commit(canon, "новое название")
    assert good.parent_commit_id == head
    # a stale writer: parent = the old head (a fork) → CONFLICT on CORE, the good commit is admitted
    st_markers = [m for m in list_markers(canon.staging) if m["commit_id"] == good.commit_id]
    assert st_markers
    stale = _forge_fork(canon, head)
    res = admit_mod.admit(canon.layout)
    admitted = {r["commit_id"] for r in res["admitted"]}
    rejected = {r["commit_id"]: r["problems"] for r in res["rejected"]}
    assert good.commit_id in admitted
    assert stale in rejected and "COMMIT_CONFLICT" in rejected[stale][0][0]


def _forge_fork(canon, parent):
    """Write a second child of ``parent`` directly on the canonical root (as a stale rsync would)."""
    from vkm_corpus import ids
    from vkm_corpus.parquet.atomic import write_json

    base = next(m for m in list_markers(canon.layout) if m["commit_id"] == parent)
    body = {k: v for k, v in base.items() if not k.startswith("_")}
    body["parent_commit_id"] = parent
    body["committed_at"] = "2026-09-28T13:00:00.000000Z"
    body.pop("commit_id")
    cid = ids.commit_id(body)
    body["commit_id"] = cid
    rel = canon.layout.commit_marker(body["processing_run_id"], body["key"], cid)
    write_json(canon.layout.tmp, canon.layout.path(rel), body)
    return cid


def test_admission_rejects_missing_blob_and_unknown_schema(tmp_path):
    from vkm_corpus.testing.synthetic import synthetic_canon

    c = synthetic_canon(tmp_path / "data")
    res = _publish_commit(c, "с потерянным blob")
    new_marker = next(m for m in list_markers(c.layout) if m["commit_id"] == res.commit_id)
    victim = next(b for b in new_marker["artifact_blobs"])
    c.layout.blob_path(victim["storage_relpath"]).unlink()
    out = admit_mod.admit(c.layout)
    assert out["rejected"] and out["rejected"][0]["problems"][0][0] == "ARTIFACT_MISSING"
    # a commit naming a schema version unknown to this code is rejected too
    from vkm_corpus import ids
    from vkm_corpus.parquet.atomic import write_json

    marker = next(m for m in list_markers(c.layout) if m["commit_id"] == c.manifest["source_heads"]["VKM-SRC-249"])
    body = {k: v for k, v in marker.items() if not k.startswith("_") and k != "commit_id"}
    body["parent_commit_id"] = marker["commit_id"]
    body["datasets"] = {**body["datasets"], "pages": {**body["datasets"]["pages"], "schema_version": "0.9.0"}}
    body["commit_id"] = ids.commit_id(body)
    write_json(c.layout.tmp, c.layout.path(c.layout.commit_marker(body["processing_run_id"], body["key"],
                                                                  body["commit_id"])), body)
    out = admit_mod.admit(c.layout)
    assert out["rejected"][0]["problems"][0][0] == "SCHEMA_UNKNOWN"


def test_snapshot_is_candidate_when_validation_fails(tmp_path):
    from vkm_corpus.parquet.reader import current_snapshot_id
    from vkm_corpus.testing.synthetic import synthetic_canon

    c = synthetic_canon(tmp_path / "data")
    before = current_snapshot_id(c.layout)
    # corrupt a published partition: A01 fails, CURRENT does not move
    f = c.manifest["datasets"]["pages"]["files"][0]
    path = c.layout.path(f["path"])
    os.chmod(path, 0o644)
    path.write_bytes(path.read_bytes() + b"x")
    res = build_snapshot(c.layout)
    assert res["status"] == "FAIL" and not res["current_moved"]
    assert current_snapshot_id(c.layout) == before
    assert res["manifest_path"].startswith("_snapshots/candidates/")


def test_gc_lists_only_old_orphans(tmp_path):
    st = init_root(tmp_path / "s", "STAGING")
    from vkm_corpus.parquet.writer import write_partition

    e = write_partition(st, "pages", [R.make_page()], run_id=R.RUN_ID, source_id=R.SID)   # never committed
    found = find_orphans(st)
    assert found["orphans"] == [] and found["too_young"] == [e.path]
    old = time.time() - 25 * 3600
    os.utime(st.path(e.path), (old, old))
    assert find_orphans(st)["orphans"] == [e.path]
    res = collect_garbage(st, delete=True)
    assert res["deleted"] and not st.path(e.path).exists()


def test_markers_are_deterministic_json(canon):
    m = list_markers(canon.layout)[0]
    text = canon.layout.path(m["_path"]).read_text(encoding="utf-8")
    assert text.endswith("\n") and "\r" not in text
    assert json.loads(text)["commit_format"] == "1"

"""Backup manifests of the CORE data root (agent OPS, ``infra/edge/backup/vkm_manifest.py``): the backup set (only the
CURRENT late pack, no projections or transient files), sha256 with hashes reused for unchanged files, the comparison
of the source manifest with the manifest of a copy (missing / corrupt / changed after the manifest / extra), the
verification of a restored tree and the grandfather-father-son retention with the free-space guard. Synthetic data
only; standard library; runs on Windows and Linux."""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import importlib.util
import json
import os
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "infra" / "edge" / "backup" / "vkm_manifest.py"


def _load():
    spec = importlib.util.spec_from_file_location("vkm_manifest", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load()
PACKS = "derived/embeddings/multivector/model/rev/sig/packs"


def _write(root: Path, rel: str, data: bytes | str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    return p


@pytest.fixture()
def data_root(tmp_path):
    """A small CANONICAL data root with everything the backup set must include or leave out."""
    root = tmp_path / "data"
    files = {
        ".vkm_root.json": '{"root_kind": "CANONICAL"}',
        "canonical/CURRENT": "snap-20260929T000000Z-0000aaaa\n",
        "canonical/pages/source_id=VKM-SRC-001/run=RUN-1/part-00000.parquet": b"PAR1" + b"\x00" * 64,
        "canonical/_snapshots/snap-20260929T000000Z-0000aaaa.json": "{}",
        "canonical/.lock": "",                                           # admission lock: never copied
        "artifacts/png/ab/cd/abcd.png": b"\x89PNG" + b"\x01" * 100,
        "duckdb/vkm_corpus.duckdb": b"DUCK" * 50,
        "derived/navigation/CURRENT": "snap-20260929T000000Z-0000aaaa\n",
        "derived/navigation/snap-20260929T000000Z-0000aaaa/nav.duckdb": b"NAV" * 30,
        "derived/embeddings/units/snap-1/vkm-units-v1-A/units.json": '{"count": 3}',
        "derived/embeddings/multivector/model/rev/sig/part-00000.parquet": b"MV" * 40,
        f"{PACKS}/CURRENT": json.dumps({"pack_id": "snap-1-aaaa", "count": 3}),
        f"{PACKS}/.lock": "",
        f"{PACKS}/snap-1-aaaa/tokens.f16": b"\x00\x01" * 64,
        f"{PACKS}/snap-0-bbbb/tokens.f16": b"\x02\x03" * 64,          # the previous pack: not in the set
        "receipts/lab_stage3/latest.json": '{"status": "DONE"}',
        "receipts/backup/source/2026-09-29T0200.manifest.jsonl.gz": b"old manifest",   # never inside the set
        "logs/api.jsonl": '{"x": 1}\n',
        "tmp/scratch.bin": b"tmp",
        "cache/x": b"cache",
        "neo4j/data/store": b"graph",
        "opensearch/data/idx": b"os",
        "derived/dossiers/.tmp-run/PC-01.json": "{}",
    }
    for rel, data in files.items():
        _write(root, rel, data)
    return root


def _set(root, packs="current"):
    spec = json.loads(json.dumps(M.CORE_SET))
    spec["packs"] = packs
    sel = M.Selector(root, spec)
    return spec, sel, dict(M.iter_files(root, spec, sel))


# ---------------------------------------------------------------- the backup set
def test_backup_set_keeps_canonical_data_and_only_the_current_pack(data_root):
    _spec, sel, files = _set(data_root)
    assert set(files) == {
        ".vkm_root.json", "canonical/CURRENT", "canonical/pages/source_id=VKM-SRC-001/run=RUN-1/part-00000.parquet",
        "canonical/_snapshots/snap-20260929T000000Z-0000aaaa.json", "artifacts/png/ab/cd/abcd.png",
        "duckdb/vkm_corpus.duckdb", "derived/navigation/CURRENT",
        "derived/navigation/snap-20260929T000000Z-0000aaaa/nav.duckdb",
        "derived/embeddings/units/snap-1/vkm-units-v1-A/units.json",
        "derived/embeddings/multivector/model/rev/sig/part-00000.parquet", f"{PACKS}/CURRENT",
        f"{PACKS}/snap-1-aaaa/tokens.f16", "receipts/lab_stage3/latest.json", "logs/api.jsonl"}
    assert sel.resolved() == {"packs_mode": "current", "current_packs": {PACKS: "snap-1-aaaa"}}


@pytest.mark.parametrize("mode,expected", [("none", set()),
                                           ("all", {"CURRENT", "snap-1-aaaa/tokens.f16", "snap-0-bbbb/tokens.f16"})])
def test_pack_modes(data_root, mode, expected):
    _spec, _sel, files = _set(data_root, mode)
    assert {r[len(PACKS) + 1:] for r in files if r.startswith(PACKS + "/")} == expected


def test_unknown_pack_mode_is_refused(data_root):
    with pytest.raises(ValueError):
        M.Selector(data_root, {**M.CORE_SET, "packs": "latest"})


def test_patterns_match_names_or_paths():
    assert M.matches("a/b/.lock", ".lock") and M.matches("receipts/backup/x/y", "receipts/backup/*")
    assert not M.matches("receipts/backupx", "receipts/backup/*") and M.matches("x/CURRENT", "CURRENT")
    assert M.read_current_pack(Path("/nonexistent"), "p") is None


def test_symlinks_are_never_followed(data_root):
    target = data_root / "tmp" / "scratch.bin"
    try:
        os.symlink(target, data_root / "canonical" / "link.bin")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not permitted here")
    stats: dict = {}
    files = dict(M.iter_files(data_root, M.CORE_SET, stats=stats))
    assert "canonical/link.bin" not in files and stats["symlinks"] == 1


# ---------------------------------------------------------------- manifests
def test_build_hashes_every_file_and_the_digest_is_stable(data_root, tmp_path):
    spec, sel, files = _set(data_root)
    header, entries = M.build(data_root, files.items(), header_extra={"set": spec, "label": "core-source"})
    rel = "artifacts/png/ab/cd/abcd.png"
    assert entries[rel][3] == hashlib.sha256((data_root / rel).read_bytes()).hexdigest()
    assert header["files"] == len(files) == header["hashed"] and header["reused"] == 0
    assert header["bytes"] == sum(st.st_size for st in files.values())
    out = tmp_path / "m.manifest.jsonl.gz"
    M.write_manifest(out, header, entries)
    h2, e2 = M.read_manifest(out)
    assert e2 == entries and h2["digest"] == M.digest_of(entries) == header["digest"]
    with gzip.open(out, "rt", encoding="utf-8") as fh:
        assert json.loads(fh.readline())["schema"] == M.SCHEMA
        paths = [json.loads(line)[0] for line in fh]
    assert paths == sorted(paths)


def test_unchanged_files_reuse_hashes_and_changed_files_are_hashed(data_root):
    _spec, _sel, files = _set(data_root)
    _h, first = M.build(data_root, files.items())
    calls = []

    def hasher(p):
        calls.append(p)
        return M.sha256_file(p)

    h, _e = M.build(data_root, files.items(), cache=first, hasher=hasher)
    assert h["hashed"] == 0 and h["reused"] == len(files) and calls == []
    p = data_root / "duckdb" / "vkm_corpus.duckdb"
    p.write_bytes(b"new duckdb build")
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
    _spec, _sel, files = _set(data_root)
    h, e = M.build(data_root, files.items(), cache=first, hasher=hasher)
    assert h["hashed"] == 1 and len(calls) == 1 and e["duckdb/vkm_corpus.duckdb"][3] != first["duckdb/vkm_corpus.duckdb"][3]


def test_rehash_all_reports_content_changed_without_new_mtime(data_root):
    _spec, _sel, files = _set(data_root)
    _h, first = M.build(data_root, files.items())
    p = data_root / "canonical/pages/source_id=VKM-SRC-001/run=RUN-1/part-00000.parquet"
    st = p.stat()
    data = bytearray(p.read_bytes())
    data[10] ^= 0xFF                                         # a flipped byte, same size and mtime: bit rot
    p.write_bytes(bytes(data))
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    _spec, _sel, files = _set(data_root)
    h, _e = M.build(data_root, files.items(), cache=first)
    assert h["cache_conflicts"]["count"] == 0                # the cache trusts size/mtime/inode …
    h, _e = M.build(data_root, files.items(), cache=first, rehash_all=True)
    assert h["cache_conflicts"]["count"] == 1 and h["cache_conflicts"]["examples"] == [str(p.relative_to(data_root)
                                                                                           ).replace("\\", "/")]


# ---------------------------------------------------------------- comparison and verification
def _copy_tree(src: Path, files, dst: Path) -> None:
    for rel in files:
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / rel, dst / rel)


def _manifests(data_root, tmp_path):
    spec, sel, files = _set(data_root)
    sh, se = M.build(data_root, files.items(), header_extra={"set": spec})
    copy = tmp_path / "snap"
    _copy_tree(data_root, files, copy)
    return sh, se, copy


def test_identical_copy_passes(data_root, tmp_path):
    sh, se, copy = _manifests(data_root, tmp_path)
    th, te = M.build(copy, M.iter_tree(copy))
    r = M.compare(sh, se, th, te)
    assert r["verdict"] == "PASS" and r["equal"] == len(se) and r["digest_equal"]


def test_missing_immutable_file_fails_and_missing_pointer_warns(data_root, tmp_path):
    sh, se, copy = _manifests(data_root, tmp_path)
    (copy / "artifacts/png/ab/cd/abcd.png").unlink()
    th, te = M.build(copy, M.iter_tree(copy))
    r = M.compare(sh, se, th, te)
    assert r["verdict"] == "FAIL" and r["missing"]["examples"] == ["artifacts/png/ab/cd/abcd.png"]
    sh, se, copy = _manifests(data_root, tmp_path / "b")
    (copy / "receipts/lab_stage3/latest.json").unlink()
    th, te = M.build(copy, M.iter_tree(copy))
    r = M.compare(sh, se, th, te)
    assert r["verdict"] == "WARN" and r["missing_volatile"]["count"] == 1


def test_same_mtime_other_content_is_corruption(data_root, tmp_path):
    sh, se, copy = _manifests(data_root, tmp_path)
    p = copy / "canonical/pages/source_id=VKM-SRC-001/run=RUN-1/part-00000.parquet"
    st = p.stat()
    p.write_bytes(b"PAR1" + b"\xff" * 64)
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    th, te = M.build(copy, M.iter_tree(copy))
    r = M.compare(sh, se, th, te)
    assert r["verdict"] == "FAIL" and r["corrupt"]["count"] == 1


def test_file_changed_on_core_after_the_manifest_is_a_warning(data_root, tmp_path):
    sh, se, copy = _manifests(data_root, tmp_path)
    for rel in ("canonical/_snapshots/snap-20260929T000000Z-0000aaaa.json", "duckdb/vkm_corpus.duckdb"):
        p = copy / rel
        st = p.stat()
        p.write_bytes(b"rewritten later")
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 60_000_000_000))
    th, te = M.build(copy, M.iter_tree(copy))
    r = M.compare(sh, se, th, te)
    assert r["verdict"] == "WARN" and r["changed_after_manifest"]["count"] == 2
    assert r["changed_after_manifest"]["stable"] == 1          # the snapshot manifest is immutable: worth a look


def test_volatile_file_with_other_content_is_never_corruption(data_root, tmp_path):
    sh, se, copy = _manifests(data_root, tmp_path)
    p = copy / "logs/api.jsonl"
    st = p.stat()
    p.write_bytes(b'{"x": 2}\n')
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    th, te = M.build(copy, M.iter_tree(copy))
    r = M.compare(sh, se, th, te)
    assert r["verdict"] == "WARN" and r["corrupt"]["count"] == 0


def test_extra_file_in_the_copy_is_a_warning(data_root, tmp_path):
    sh, se, copy = _manifests(data_root, tmp_path)
    _write(copy, "canonical/unexpected.bin", b"?")
    th, te = M.build(copy, M.iter_tree(copy))
    assert M.compare(sh, se, th, te)["extra"]["examples"] == ["canonical/unexpected.bin"]


def test_tree_manifest_skips_the_backup_metadata(tmp_path):
    _write(tmp_path, "a/x.bin", b"1")
    _write(tmp_path, ".vkm_backup/source_manifest.jsonl.gz", b"m")
    assert [r for r, _st in M.iter_tree(tmp_path, skip_prefixes=[".vkm_backup"])] == ["a/x.bin"]


def test_verify_restored_subset(data_root, tmp_path):
    sh, se, copy = _manifests(data_root, tmp_path)
    restored = tmp_path / "restore"
    _copy_tree(copy, [r for r in se if r.startswith("receipts/") or r.startswith("canonical/_snapshots/")], restored)
    r = M.verify(restored, se, ["receipts", "canonical/_snapshots"])
    assert r["verdict"] == "PASS" and r["files"] == r["ok"] == 2
    (restored / "receipts/lab_stage3/latest.json").write_text("tampered", encoding="utf-8")
    r = M.verify(restored, se, ["receipts"])
    assert r["verdict"] == "FAIL" and r["corrupt"]["count"] == 1
    assert M.verify(restored, se, ["artifacts"])["missing"]["count"] == 1
    assert M.verify(restored, se, ["no/such/prefix"])["verdict"] == "FAIL"


def test_new_bytes_against_the_previous_snapshot(data_root):
    _spec, _sel, files = _set(data_root)
    _h, prev = M.build(data_root, files.items())
    cur = dict(prev)
    cur["artifacts/new.png"] = (10, 1, 1, "f" * 64)
    size, mtime, ino, _sha = cur["duckdb/vkm_corpus.duckdb"]
    cur["duckdb/vkm_corpus.duckdb"] = (size, mtime, ino, "e" * 64)
    assert M.new_bytes(cur, prev) == {"files": 2, "bytes": 10 + size}
    assert M.new_bytes(cur, {})["files"] == len(cur)


# ---------------------------------------------------------------- retention
def _names(start: dt.date, days: int) -> list[str]:
    return [(start + dt.timedelta(days=i)).isoformat() for i in range(days)]


def test_snapshot_names():
    assert M.snapshot_date("2026-09-30") == dt.date(2026, 9, 30)
    assert M.snapshot_date("2026-09-30T0230") == dt.date(2026, 9, 30)
    for bad in ("latest", "2026-09-30.partial", "2026-09-30.failed", "2026-13-01", "2026-09-30Tabcd"):
        assert M.snapshot_date(bad) is None


def test_gfs_retention_over_a_hundred_days():
    names = _names(dt.date(2026, 6, 1), 100) + ["latest", "2026-09-08.failed"]
    plan = M.plan_retention(names, daily=7, weekly=4, monthly=6, minimum=3)
    keep = plan["keep"]
    newest = sorted(n for n in names if M.snapshot_date(n))[-1]
    assert keep[0] == newest and len(set(keep)) == len(keep)
    assert set(_names(dt.date(2026, 9, 2), 7)) <= set(keep)          # the 7 newest days
    months = {n[:7] for n in keep}
    assert months == {"2026-06", "2026-07", "2026-08", "2026-09"}      # one per month (only 4 months exist)
    assert "2026-08-31" in keep and "2026-07-31" in keep and "2026-06-30" in keep
    weeks = {tuple(dt.date.fromisoformat(n).isocalendar())[:2] for n in keep}
    assert len(weeks) >= 4
    assert len(keep) + len(plan["delete"]) == 100 and "latest" not in keep + plan["delete"]
    assert len(keep) < 20


def test_retention_keeps_the_minimum_and_protected():
    names = ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"]
    plan = M.plan_retention(names, daily=1, weekly=0, monthly=0, minimum=2, protect=["2026-01-01"])
    assert plan["keep"] == ["2026-01-04", "2026-01-03", "2026-01-01"] and plan["delete"] == ["2026-01-02"]
    assert M.plan_retention(["2026-01-01T0100", "2026-01-01"], daily=1, weekly=0, monthly=0,
                            minimum=0)["keep"] == ["2026-01-01T0100"]


def test_prune_applies_retention_keeps_one_failed_and_frees_space(tmp_path):
    snaps = tmp_path / "snapshots"
    for n in _names(dt.date(2026, 9, 1), 12) + ["2026-09-05.failed", "2026-09-11.failed", "2026-09-12.partial"]:
        (snaps / n).mkdir(parents=True)
    removed: list[str] = []
    free = {"v": 50}

    def remove(p: Path) -> None:
        removed.append(p.name)
        shutil.rmtree(p)
        free["v"] += 10

    res = M.prune(snaps, daily=5, weekly=0, monthly=0, minimum=3, min_free_bytes=100, protect=["2026-09-12"],
                  keep_failed=1, free_bytes=lambda p: free["v"], remove=remove)
    assert res["plan"]["delete"] == _names(dt.date(2026, 9, 1), 7)
    assert "2026-09-12.partial" not in removed and "2026-09-11.failed" in removed and "2026-09-05.failed" in removed
    # 50 + 10 per removed dir (7 by retention, 2 failed) = 140 ≥ 100: no deletion for space
    assert res["deleted_for_space"] == [] and res["space_ok"]
    free["v"] = 0
    res = M.prune(snaps, daily=5, weekly=0, monthly=0, minimum=3, min_free_bytes=15, protect=["2026-09-12"],
                  free_bytes=lambda p: free["v"], remove=remove)
    assert res["deleted_for_space"] == ["2026-09-08", "2026-09-09"]      # oldest first, never the newest 3
    assert sorted(p.name for p in snaps.iterdir()) == ["2026-09-10", "2026-09-11", "2026-09-12", "2026-09-12.partial"]


def test_prune_dry_run_changes_nothing_and_a_missing_directory_is_empty(tmp_path):
    snaps = tmp_path / "snapshots"
    for n in _names(dt.date(2026, 9, 1), 6):
        (snaps / n).mkdir(parents=True)
    res = M.prune(snaps, daily=2, weekly=0, monthly=0, minimum=2, min_free_bytes=10**30, dry_run=True,
                  free_bytes=lambda p: 0, remove=lambda p: pytest.fail("dry run removed something"))
    assert res["deleted"] == _names(dt.date(2026, 9, 1), 4) and len(list(snaps.iterdir())) == 6
    assert res["space_candidates"] == [] and not res["space_ok"]
    res = M.prune(tmp_path / "nope", daily=1, weekly=0, monthly=0, minimum=1, free_bytes=lambda p: 1)
    assert res["deleted"] == [] and res["plan"]["keep"] == []


def test_prune_protects_the_latest_symlink_target(tmp_path):
    snaps = tmp_path / "snapshots"
    for n in _names(dt.date(2026, 9, 1), 4):
        (snaps / n).mkdir(parents=True)
    try:
        os.symlink("2026-09-01", snaps / "latest", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not permitted here")
    res = M.prune(snaps, daily=1, weekly=0, monthly=0, minimum=1, free_bytes=lambda p: 1)
    assert "2026-09-01" not in res["deleted"] and res["deleted"] == ["2026-09-02", "2026-09-03"]


# ---------------------------------------------------------------- command line
def test_cli_build_compare_files_estimate(data_root, tmp_path, capsys):
    src = tmp_path / "src.manifest.jsonl.gz"
    assert M.main(["build", "--root", str(data_root), "--out", str(src), "--label", "core-source"]) == 0
    head = M.read_header(src)
    assert head["label"] == "core-source" and head["resolved"]["current_packs"] == {PACKS: "snap-1-aaaa"}
    capsys.readouterr()
    assert M.main(["build", "--root", str(data_root), "--dry-run", "--cache", str(src)]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["to_hash"] == 0 and dry["files"] == head["files"]
    _h, entries = M.read_manifest(src)
    copy = tmp_path / "snap"
    _copy_tree(data_root, entries, copy)
    _write(copy, ".vkm_backup/source_manifest.jsonl.gz", b"x")
    dst = tmp_path / "dst.manifest.jsonl.gz"
    assert M.main(["build", "--tree", "--root", str(copy), "--skip", ".vkm_backup", "--out", str(dst)]) == 0
    assert M.main(["compare", str(src), str(dst), "--out", str(tmp_path / "cmp.json")]) == 0
    assert json.loads((tmp_path / "cmp.json").read_text(encoding="utf-8"))["verdict"] == "PASS"
    (copy / "artifacts/png/ab/cd/abcd.png").unlink()
    assert M.main(["build", "--tree", "--root", str(copy), "--skip", ".vkm_backup", "--out", str(dst)]) == 0
    assert M.main(["compare", str(src), str(dst)]) == 1
    lst = tmp_path / "files.list"
    assert M.main(["files", "--manifest", str(src), "--out", str(lst)]) == 0
    assert lst.read_text(encoding="utf-8").splitlines() == sorted(entries)
    capsys.readouterr()
    assert M.main(["estimate", "--manifest", str(src), "--previous", str(src)]) == 0
    assert json.loads(capsys.readouterr().out)["files"] == 0
    assert M.main(["verify", "--root", str(copy), "--manifest", str(src), "--path", "receipts"]) == 0
    assert M.main(["verify", "--root", str(copy), "--manifest", str(src), "--path", "artifacts"]) == 1


def test_cli_rejects_a_missing_root(tmp_path):
    assert M.main(["build", "--root", str(tmp_path / "missing"), "--out", str(tmp_path / "x.gz")]) == 2

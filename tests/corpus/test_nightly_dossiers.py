"""Nightly topic dossiers (agent OPS): the in-container client ``infra/core/nightly/dossiers.py`` asks
``reconstruct_topic`` once per topic of the frozen TOPIC_BENCHMARK_V1 set (NAME + 2 paraphrases), and the host-side
``dossier_store.py`` keeps the answers under ``derived/dossiers/<snapshot>/`` with an index (counts and ids of sections
and processes, self-hits) and compares it with the previous nightly run. Synthetic answers; no API."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NIGHTLY = ROOT / "infra" / "core" / "nightly"
SET = ROOT / "benchmarks" / "topic_v1" / "topic_set_v1.jsonl"
SNAP = "snap-20260929T193550Z-738eebee"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, NIGHTLY / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


D = _load("dossier_store")


def bundle() -> str:
    """What nightly_checks.sh pipes into the API container: the frozen set as SET_JSONL + dossiers.py."""
    return 'SET_JSONL = r"""\n' + SET.read_text(encoding="utf-8") + '"""\n' + (NIGHTLY / "dossiers.py").read_text(
        encoding="utf-8")


# ---------------------------------------------------------------- the in-container client
def test_dry_run_plans_all_117_topics_with_their_paraphrases():
    proc = subprocess.run([sys.executable, "-", "--dry-run", "--budget", "9000"], input=bundle(), text=True,
                          capture_output=True, encoding="utf-8", timeout=120)
    assert proc.returncode == 0, proc.stderr
    lines = [json.loads(x) for x in proc.stdout.splitlines()]
    plans = [x for x in lines if x["kind"] == "plan"]
    assert lines[0]["kind"] == "meta" and lines[0]["topics"] == 117 and lines[-1]["kind"] == "end"
    assert len(plans) == 117 and len({p["topic_id"] for p in plans}) == 117
    assert sum(1 for p in plans if p["track"] == "PROCESS") == 72
    assert sum(1 for p in plans if p["track"] == "MODEL_FAMILY") == 45
    assert all(len(p["paraphrases"]) == 2 and p["query"] for p in plans)
    first = plans[0]
    assert first["path"].startswith("/v1/topic?q=") and "&budget=9000&paraphrase=" in first["path"]
    assert first["path"].count("paraphrase=") == 2


def test_client_needs_the_embedded_set():
    proc = subprocess.run([sys.executable, str(NIGHTLY / "dossiers.py"), "--dry-run"], capture_output=True,
                          text=True, timeout=60)
    assert proc.returncode != 0 and "SET_JSONL" in proc.stderr


def test_topics_of_uses_the_name_variant():
    ns: dict = {}
    exec(compile((NIGHTLY / "dossiers.py").read_text(encoding="utf-8"), "dossiers.py", "exec"), ns)
    line = json.dumps({"topic_id": "PC-99", "track": "PROCESS", "group": "RHEO", "title": "T",
                       "queries": [{"variant": "PARA1", "text": "p1"}, {"variant": "NAME", "text": "n"},
                                   {"variant": "PARA2", "text": "n"}]})
    assert ns["topics_of"](line) == [{"topic_id": "PC-99", "track": "PROCESS", "group": "RHEO", "title": "T",
                                      "query": "n", "paraphrases": ["p1"]}]


# ---------------------------------------------------------------- storing and indexing
def answer(sections, processes=(), models=(), warnings=()):
    return {"ok": True, "meta": {"warnings": [{"code": w, "message": "m"} for w in warnings]},
            "item": {"envelope": {"object_kind": "TOPIC_DOSSIER"},
                     "record": {"mode": "HYBRID", "rule_version": "topic_dossier_v2",
                                "sections": [{"section_id": s, "tier": t} for s, t in sections],
                                "catalogue": {"processes": [{"process_id": p} for p in processes],
                                              "models": [{"model_id": m} for m in models]},
                                "formulas": [{"formula_id": "f"}], "sources": [{"source_id": "VKM-SRC-001"}],
                                "pages": {"core": [{"page_id": "p1"}], "rest": []}, "gaps": [{}, {}],
                                "markdown": "# досье\n", "inputs": {"canonical_snapshot_id": SNAP}}}}


def dossier_lines(pc01_sections=2, pc02_ok=False):
    secs = [(f"SEC-{i:016x}", "CORE" if i % 2 else "REST") for i in range(1, pc01_sections + 1)]
    return [
        {"kind": "meta", "version": "nightly-dossiers-1", "topics": 3, "budget": 12000},
        {"kind": "dossier", "topic_id": "PC-01", "track": "PROCESS", "group": "INIT", "title": "t", "http": 200,
         "ok": True, "ms": 3100.0, "response": answer(secs, ["PC-05", "PC-01"], warnings=["NAV_SNAPSHOT_BEHIND"])},
        {"kind": "dossier", "topic_id": "MM-CREEP", "track": "MODEL_FAMILY", "group": "CREEP", "title": "t",
         "http": 200, "ok": True, "ms": 2900.0, "response": answer([("SEC-" + "a" * 16, "CORE")], [],
                                                                    ["MM-CREEP-001", "MM-DAMAGE-002"])},
        {"kind": "dossier", "topic_id": "PC-02", "track": "PROCESS", "group": "INIT", "title": "t",
         "http": 200 if pc02_ok else 503, "ok": pc02_ok, "ms": 10.0,
         "error_code": None if pc02_ok else "DEPENDENCY_UNAVAILABLE",
         "response": answer([], ["PC-07"]) if pc02_ok else None},
        {"kind": "end", "done": 3, "errors": 0 if pc02_ok else 1, "truncated": False},
    ]


def test_store_writes_the_cache_index_and_current(tmp_path):
    ddir = tmp_path / "derived" / "dossiers"
    index, stored = D.store(dossier_lines(), ddir, SNAP, run_tag="r1")
    assert stored and (ddir / "CURRENT").read_text(encoding="utf-8").strip() == SNAP
    snapdir = ddir / SNAP
    assert sorted(p.name for p in snapdir.iterdir()) == ["MM-CREEP.json", "PC-01.json", "index.json"]
    assert json.loads((snapdir / "PC-01.json").read_text(encoding="utf-8"))["item"]["record"]["mode"] == "HYBRID"
    s = index["summary"]
    assert (s["topics"], s["ok"], s["errors"]) == (3, 2, 1) and s["error_codes"] == ["DEPENDENCY_UNAVAILABLE"]
    assert s["process_self_hit"] == 1.0 and s["model_self_hit"] == 1.0
    pc01 = next(e for e in index["topics"] if e["topic_id"] == "PC-01")
    assert pc01["sections"] == 2 and pc01["sections_core"] == 1 and pc01["processes"] == 2
    assert pc01["warnings"] == ["NAV_SNAPSHOT_BEHIND"] and pc01["sha256"] and pc01["self_hit"] is True
    assert not any(p.name.startswith((".tmp-", ".old-")) for p in ddir.iterdir())


def test_self_hit_rules():
    assert D.self_hit("PC-01", "PROCESS", ["PC-01"], []) is True
    assert D.self_hit("PC-01", "PROCESS", ["PC-011"], []) is False
    assert D.self_hit("MM-CREEP", "MODEL_FAMILY", [], ["MM-CREEPX-001"]) is False
    assert D.self_hit("MM-CREEP", "MODEL_FAMILY", [], ["MM-CREEP-003"]) is True
    assert D.self_hit("X", None, [], []) is None


def test_rerun_on_the_same_snapshot_replaces_the_directory_and_the_diff_counts_changes(tmp_path):
    ddir = tmp_path / "dossiers"
    first, _ = D.store(dossier_lines(), ddir, SNAP, run_tag="r1")
    second, stored = D.store(dossier_lines(pc01_sections=8, pc02_ok=True), ddir, SNAP, run_tag="r2")
    assert stored and len(list((ddir / SNAP).glob("*.json"))) == 4
    diff = D.diff(second, first)
    assert diff["changed"] == 1 and diff["big_changes"] == 1 and diff["big"] == ["PC-01"]
    assert diff["examples"][0]["sections"] == [2, 8] and diff["lost"] == []
    assert D.diff(second, None) is None
    third, _ = D.store(dossier_lines(), ddir, SNAP, run_tag="r3")
    assert D.diff(third, second)["lost"] == ["PC-02"]


def test_nothing_succeeded_keeps_the_previous_cache(tmp_path):
    ddir = tmp_path / "dossiers"
    D.store(dossier_lines(), ddir, SNAP, run_tag="r1")
    failed = [x if x.get("kind") != "dossier" else {**x, "ok": False, "response": None, "error_code": "TIMEOUT"}
              for x in dossier_lines()]
    index, stored = D.store(failed, ddir, "snap-20260930T000000Z-00000001", run_tag="r2")
    assert not stored and index["summary"]["ok"] == 0
    assert (ddir / "CURRENT").read_text(encoding="utf-8").strip() == SNAP and (ddir / SNAP / "index.json").is_file()


def test_keep_limits_snapshot_directories_and_dry_run_writes_nothing(tmp_path):
    ddir = tmp_path / "dossiers"
    for i, snap in enumerate(("snap-20260901T000000Z-00000001", "snap-20260902T000000Z-00000002", SNAP)):
        D.store(dossier_lines(), ddir, snap, run_tag=f"r{i}", keep=2)
    assert sorted(p.name for p in ddir.iterdir() if p.is_dir()) == ["snap-20260902T000000Z-00000002", SNAP]
    empty = tmp_path / "empty"
    _index, stored = D.store(dossier_lines(), empty, SNAP, run_tag="d", dry_run=True)
    assert not stored and not empty.exists()
    with pytest.raises(ValueError):
        D.store(dossier_lines(), ddir, "../evil", run_tag="x")


def test_retention_uses_snapshot_identity_even_when_mtimes_tie_or_reverse(tmp_path):
    ddir = tmp_path / "dossiers"
    early, middle = "snap-20260901T000000Z-00000001", "snap-20260902T000000Z-00000002"
    for i, snap in enumerate((middle, early)):
        D.store(dossier_lines(), ddir, snap, run_tag=f"r{i}", keep=3)
        os.utime(ddir / snap, (2_000_000_000, 2_000_000_000))
    unknown = ddir / "not-a-snapshot"
    unknown.mkdir()
    index, _ = D.store(dossier_lines(), ddir, SNAP, run_tag="new", keep=2)
    assert set(p.name for p in ddir.iterdir() if p.is_dir()) == {middle, SNAP, unknown.name}
    assert index["retention"]["delete"] == [early]
    assert index["retention"]["unclassified_preserved"] == [unknown.name]


def test_retention_current_counts_toward_keep_after_rollback():
    old = "snap-20260901T000000Z-00000001"
    plan = D.retention_plan([SNAP, old], old, 1)
    assert plan["keep"] == [old] and plan["delete"] == [SNAP]
    assert D.retention_plan([old, SNAP], old, 1) == plan
    assert D.retention_plan(["snap-20269999T000000Z-01", SNAP], SNAP, 2)["unclassified_preserved"] == [
        "snap-20269999T000000Z-01"]


@pytest.mark.parametrize("keep", [0, -1, True])
def test_invalid_retention_is_rejected_before_writing(tmp_path, keep):
    root = tmp_path / "cache"
    with pytest.raises(ValueError):
        D.store(dossier_lines(), root, SNAP, run_tag="x", keep=keep)
    assert not root.exists()


@pytest.mark.parametrize("field,bad", [("run_tag", "../../escape"), ("topic_id", "../escape"),
                                       ("topic_id", "index"), ("snapshot", "..")])
def test_dossier_paths_are_preflighted(tmp_path, field, bad):
    lines = dossier_lines()
    kwargs = {"run_tag": "x"}
    snapshot = SNAP
    if field == "topic_id":
        lines[1]["topic_id"] = bad
    elif field == "snapshot":
        snapshot = bad
    else:
        kwargs[field] = bad
    with pytest.raises(ValueError):
        D.store(lines, tmp_path / "cache", snapshot, **kwargs)
    assert not (tmp_path / "cache").exists()


def test_retention_preserves_other_writer_staging(tmp_path):
    staged = tmp_path / "cache/.tmp-other"
    staged.mkdir(parents=True)
    (staged / "sentinel").write_bytes(b"active or interrupted")
    D.store(dossier_lines(), staged.parent, SNAP, run_tag="ours")
    assert (staged / "sentinel").read_bytes() == b"active or interrupted"


def test_topic_case_collision_is_rejected_on_every_host(tmp_path):
    lines = dossier_lines()
    first = next(line for line in lines if line.get("kind") == "dossier")
    lines.append({**first, "topic_id": first["topic_id"].lower()})
    with pytest.raises(ValueError, match="duplicate"):
        D.store(lines, tmp_path / "cache", SNAP, run_tag="collision")
    assert not (tmp_path / "cache").exists()


def test_cli_writes_the_run_index_with_the_diff(tmp_path):
    inp = tmp_path / "dossiers.jsonl"
    inp.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in dossier_lines()) + "\n", encoding="utf-8")
    prev = tmp_path / "prev_index.json"
    prev_index, _ = D.store(dossier_lines(pc01_sections=5), tmp_path / "other", SNAP, run_tag="p", dry_run=True)
    prev.write_text(json.dumps(prev_index), encoding="utf-8")
    out = tmp_path / "run" / "dossiers_index.json"
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = D.main(["--in", str(inp), "--dossiers-dir", str(tmp_path / "d"), "--snapshot", SNAP,
                     "--out-index", str(out), "--prev-index", str(prev), "--run-tag", "t"])
    assert rc == 0
    printed = json.loads(buf.getvalue())
    assert printed["stored"] and printed["ok"] == 2 and printed["diff"]["changed"] == 1
    index = json.loads(out.read_text(encoding="utf-8"))
    assert index["diff"]["examples"][0]["topic_id"] == "PC-01" and index["stored"] is True

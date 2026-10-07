"""TOPIC_BENCHMARK_V1 delta pool T3 (benchmarks/topic_v1/scripts/pool_t3.py): the T2 code with the T3 constants in a
private module copy (nothing leaks into pool_t2), and the T3 exclusion rule — a pair judged in T1 or T2, through its
page, a unit alias or a T2 ``t1_alias_links`` page, is not labelled again; catalogue targets stay the V1 rule."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from vkm_corpus.retrieval_lab import topic_bench as TB

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "benchmarks" / "topic_v1" / "scripts"


def load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def t3():
    return load("topic_v1_pool_t3", "pool_t3.py")


def topic(tid: str, targets=()) -> TB.Topic:
    qs = tuple(TB.Query(f"TQ-{tid}-{i}", tid, v, f"оседание {tid} {v.lower()}")
               for i, v in enumerate(TB.VARIANTS))
    tg = tuple(TB.Target(f"{tid}/T{i + 1:03d}", p, p.split(":")[0], ()) for i, p in enumerate(targets))
    return TB.Topic(tid, "PROCESS", "OBS", f"Тема {tid}", qs, tg)


def row(tid: str, page: str, grade: int, source: str) -> dict[str, str]:
    return {"query_id": tid, "level": "PAGE", "doc_id": page, "grade": str(grade), "status": "CANDIDATE",
            "basis": "POOL_JUDGMENT", "label_source": source, "pooled_from": "bm25@1", "rationale": "SUP_DISCUSS"}


TOPICS = [topic("PC-01", ["VKM-SRC-001:p0001"])]
T1_ROWS = [row("PC-01", "VKM-SRC-004:p0007", 2, "LLM_AGENT_T1")]
T1_GROUPS = [{"topic_id": "PC-01", "pages": ["VKM-SRC-004:p0007"], "aliases": ["VKM-SRC-006:p0003"]}]
T2_ROWS = [row("PC-01", "VKM-SRC-252:p0001", 0, "LLM_AGENT_T2"), row("PC-01", "VKM-SRC-253:p0002", 3, "LLM_AGENT_T2")]
T2_GROUPS = [{"topic_id": "PC-01", "pages": ["VKM-SRC-253:p0002"], "aliases": ["VKM-SRC-254:p0002"]},
             # a T2 t1_alias_links page, already turned into an alias of its T1 unit by pool_t3.t2_labels
             {"topic_id": "PC-01", "pages": ["VKM-SRC-004:p0007"], "aliases": ["VKM-SRC-005:p0001"]}]


def test_t3_constants_live_in_a_private_copy(t3):
    t2 = load("topic_v1_pool_t2_check", "pool_t2.py")
    assert (t3.P2.LABEL_SOURCE, t3.P2.POOL_FILE, t3.P2.CID_PREFIX) == ("LLM_AGENT_T3", "pool_t3.json", "e")
    assert t3.P2.OUT_TSV.name == "qrels_topic_v1_pooled_t3.tsv"
    assert t3.T2_TSV.name == "qrels_topic_v1_pooled_t2.tsv"
    assert (t2.LABEL_SOURCE, t2.POOL_FILE, t2.CID_PREFIX) == ("LLM_AGENT_T2", "pool_t2.json", "d")
    assert t2.OUT_TSV.name == "qrels_topic_v1_pooled_t2.tsv"
    assert len(t3.OCR_V2_SOURCES) == 94 and {"VKM-SRC-053", "VKM-SRC-230"} <= t3.OCR_V2_SOURCES


def test_judged_covers_t1_t2_and_their_aliases_not_targets(t3, monkeypatch):
    monkeypatch.setattr(t3, "t2_labels", lambda: (T2_ROWS, T2_GROUPS))
    judged = t3.judged_t1_t2(TOPICS, T1_ROWS, T1_GROUPS)
    assert judged["PC-01"] == {"VKM-SRC-004:p0007", "VKM-SRC-006:p0003", "VKM-SRC-005:p0001",
                               "VKM-SRC-252:p0001", "VKM-SRC-253:p0002", "VKM-SRC-254:p0002"}
    assert t3.P2.t1_judged is t3.judged_t1_t2


def test_delta_excludes_t1_and_t2_pairs(t3, monkeypatch):
    monkeypatch.setattr(t3, "t2_labels", lambda: (T2_ROWS, T2_GROUPS))
    judged = t3.judged_t1_t2(TOPICS, T1_ROWS, T1_GROUPS)
    rk = {s: {} for s in t3.P2.SYSTEMS}
    rk["bm25"]["TQ-PC-01-0"] = [(p, frozenset({p})) for p in (
        "VKM-SRC-001:p0001", "VKM-SRC-004:p0007", "VKM-SRC-252:p0001", "VKM-SRC-254:p0002", "VKM-SRC-266:p0010")]
    delta = t3.P2.delta_candidates(TOPICS, rk, {s: t3.P2.DEPTH for s in t3.P2.SYSTEMS}, {}, judged)["PC-01"]
    assert delta["excluded_catalogue_matches"] == ["VKM-SRC-001:p0001"]
    assert delta["excluded_t1_judged"] == ["VKM-SRC-004:p0007", "VKM-SRC-252:p0001", "VKM-SRC-254:p0002"]
    assert set(delta["candidates"]) == {"VKM-SRC-266:p0010"}
    units = t3.P2.make_units(TOPICS, {"PC-01": delta})
    assert [u["cid"] for u in units["PC-01"]["units"]] == ["e001"]


def test_main_refuses_without_the_snapshot(t3, monkeypatch):
    monkeypatch.setattr(t3, "SNAPSHOT", "snap-PENDING")
    monkeypatch.setattr("sys.argv", ["pool_t3.py", "status"])
    with pytest.raises(SystemExit, match="SNAPSHOT is not set"):
        t3.main()


def test_ingest_writes_the_t3_files_never_t1_or_t2(t3, monkeypatch):
    calls = []
    monkeypatch.setattr(t3, "_t2_ingest", lambda partial, out_tsv, out_groups: calls.append(
        (partial, out_tsv, out_groups)))
    monkeypatch.setattr("sys.argv", ["pool_t3.py", "ingest", "--partial"])
    t3.main()
    assert calls == [(True, t3.BENCH / "qrels_topic_v1_pooled_t3.tsv",
                      t3.BENCH / "qrels_topic_v1_pooled_t3_groups.json")]
    monkeypatch.setattr(t3.P2, "OUT_TSV", t3.T2_TSV)
    with pytest.raises(SystemExit, match="never written"):
        t3.ingest()

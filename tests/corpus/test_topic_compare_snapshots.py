"""TOPIC_BENCHMARK_V1 before/after OCR v2 comparison (benchmarks/topic_v1/scripts/compare_snapshots.py): the target
subset keeps only accepted targets and drops topics left without any; metric rows carry the snapshot in the system
name."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from vkm_corpus.retrieval_lab import topic_bench as TB

SCRIPT = Path(__file__).resolve().parents[2] / "benchmarks" / "topic_v1" / "scripts" / "compare_snapshots.py"


@pytest.fixture(scope="module")
def cs():
    spec = importlib.util.spec_from_file_location("topic_v1_compare_snapshots", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def topic(tid: str, pages) -> TB.Topic:
    qs = tuple(TB.Query(f"TQ-{tid}-{i}", tid, v, f"оседание {tid}") for i, v in enumerate(TB.VARIANTS))
    tg = tuple(TB.Target(f"{tid}/T{i + 1:03d}", p, p.split(":")[0], ()) for i, p in enumerate(pages))
    return TB.Topic(tid, "PROCESS", "OBS", f"Тема {tid}", qs, tg)


def test_subset_keeps_accepted_targets_and_drops_empty_topics(cs):
    topics = [topic("PC-01", ["VKM-SRC-001:p0001", "VKM-SRC-002:p0002"]), topic("PC-02", ["VKM-SRC-003:p0003"])]
    changed = {"VKM-SRC-002:p0002"}
    got = cs.subset(topics, lambda t: bool(t.pages & changed))
    assert [t.topic_id for t in got] == ["PC-01"] and [x.page_id for x in got[0].targets] == ["VKM-SRC-002:p0002"]
    rest = cs.subset(topics, lambda t: not (t.pages & changed))
    assert [(t.topic_id, len(t.targets)) for t in rest] == [("PC-01", 1), ("PC-02", 1)]
    assert topics[0].targets[1].page_id == "VKM-SRC-002:p0002"          # the input topics are not changed


def test_metric_rows_name_the_snapshot(cs):
    t = topic("PC-01", ["VKM-SRC-001:p0001"])
    r = TB.Ranking(pages=["VKM-SRC-001:p0001"], aliases=[frozenset({"VKM-SRC-001:p0001"})])
    rows = cs.metric_rows([t], {cs.OLD: {"bm25": {"TQ-PC-01-0": r}}, cs.NEW: {"bm25": {"TQ-PC-01-0": r}}})
    assert sorted(x["system"] for x in rows) == ["bm25@2d71e9e8", "bm25@574daaac"]
    assert all(x["page_recall@10"] == 1.0 and x["topic_id"] == "PC-01" for x in rows)

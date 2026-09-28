"""TOPIC_BENCHMARK_V1 (agent Q): frozen set, metric definitions and system rankings on synthetic data."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from vkm_corpus.retrieval_lab import topic_bench as TB

ROOT = Path(__file__).resolve().parents[2]
BENCH = ROOT / "benchmarks" / "topic_v1"


def _sha_lf(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _topic(targets, sources=None) -> TB.Topic:
    qs = tuple(TB.Query(f"TQ-X-{i}", "X", v, f"запрос {v}") for i, v in enumerate(TB.VARIANTS))
    return TB.Topic("X", "PROCESS", "RHEO", "тема", qs, tuple(targets))


def _t(n: int, page: str, alts=()) -> TB.Target:
    return TB.Target(f"X/T{n:02d}", page, page.split(":")[0], tuple(alts))


OUTLINES = {
    "VKM-SRC-001": [
        {"section_id": "SEC-" + "a" * 16, "level": 1, "page_start_index": 1, "page_end_index": 10,
         "page_start_id": "VKM-SRC-001:p0001"},
        {"section_id": "SEC-" + "b" * 16, "level": 2, "page_start_index": 2, "page_end_index": 5,
         "page_start_id": "VKM-SRC-001:p0002"},
        {"section_id": "SEC-" + "c" * 16, "level": 2, "page_start_index": 5, "page_end_index": 8,
         "page_start_id": "VKM-SRC-001:p0005"},
    ],
    "VKM-SRC-023": [
        {"section_id": "SEC-" + "d" * 16, "level": 1, "page_start_index": 1, "page_end_index": 3,
         "page_start_id": "VKM-SRC-023:r0001"},
    ],
}


# ---------------------------------------------------------------- the frozen set
def test_real_set_is_valid_and_frozen():
    lines = (BENCH / "topic_set_v1.jsonl").read_text(encoding="utf-8").splitlines()
    assert TB.validate_set(lines) == []
    topics = TB.load_set(BENCH / "topic_set_v1.jsonl")
    assert len(topics) == 117 and sum(len(t.queries) for t in topics) == 351
    assert {t.track for t in topics} == set(TB.TRACKS)
    assert sum(1 for t in topics if t.track == "PROCESS") == 72
    spec = json.loads((BENCH / "metrics_spec_v1.json").read_text(encoding="utf-8"))
    assert _sha_lf(BENCH / "topic_set_v1.jsonl") == spec["set"]["sha256"]
    assert _sha_lf(BENCH / "topic_queries_v1.tsv") == spec["set"]["queries_sha256"]
    assert spec["set"]["targets"] == sum(len(t.targets) for t in topics)
    sums = dict(reversed(line.split(maxsplit=1)) for line in
                (BENCH / "SHA256SUMS").read_text(encoding="utf-8").splitlines() if line.strip())
    for name, digest in sums.items():
        assert _sha_lf(BENCH / name.lstrip("*")) == digest, name


def test_published_outputs_carry_ids_and_numbers_only():
    for name in ("results_v1.json", "failure_analysis_v1.json", "page_mapping_v1.json"):
        path = BENCH / name
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        assert not (TB._keys(data) & TB.FORBIDDEN_KEYS), name
    receipt = ROOT / "docs" / "corpus_platform" / "receipts" / "topic_benchmark_v1.json"
    if receipt.exists():
        data = json.loads(receipt.read_text(encoding="utf-8"))
        assert data["preregistration"]["set_sha256"] == _sha_lf(BENCH / "topic_set_v1.jsonl")
        assert not (TB._keys(data) & TB.FORBIDDEN_KEYS)


def test_validation_catches_problems():
    good = {"topic_id": "PC-99", "track": "PROCESS", "group": "RHEO", "title": "t",
            "queries": [{"query_id": f"TQ-PC-99-{i}", "variant": v, "text": f"q{i}"}
                        for i, v in enumerate(TB.VARIANTS)],
            "targets": [{"target_id": "PC-99/T01", "page_id": "VKM-SRC-001:p0001", "source_id": "VKM-SRC-001"}]}
    assert TB.validate_set([json.dumps(good)]) == []
    bad = json.loads(json.dumps(good))
    bad["targets"][0]["page_id"] = "VKM-SRC-002:p0001"
    bad["queries"][2]["text"] = "q0"
    bad["targets"][0]["quote"] = "текст"
    problems = TB.validate_set([json.dumps(bad)])
    assert any("page id" in p for p in problems)
    assert any("repeated" in p for p in problems)
    assert any("forbidden" in p for p in problems)


# ---------------------------------------------------------------- metrics
def test_page_metrics_alternates_and_sources():
    idx = TB.SectionIndex.from_outlines(OUTLINES)
    topic = _topic([_t(1, "VKM-SRC-001:p0003"), _t(2, "VKM-SRC-001:p0009", ["VKM-SRC-001:p0010"]),
                    _t(3, "VKM-SRC-002:p0004")])
    hits = [{"page_id": f"VKM-SRC-009:p{i:04d}"} for i in range(1, 4)] + \
           [{"page_id": "VKM-SRC-001:p0010"}] + [{"page_id": f"VKM-SRC-008:p{i:04d}"} for i in range(1, 20)] + \
           [{"page_id": "VKM-SRC-001:p0003"}]
    r = TB.ranking_from_hits(hits, idx)
    ranks = TB.target_ranks(topic.targets, r)
    assert ranks == {"X/T01": 24, "X/T02": 4, "X/T03": None}
    m = TB.query_metrics(topic, r)
    assert m["page_recall@10"] == pytest.approx(1 / 3)
    assert m["page_recall@20"] == pytest.approx(1 / 3)
    assert m["page_recall@50"] == pytest.approx(2 / 3)
    assert m["capped_recall@10"] == pytest.approx(1 / 3)
    assert m["mrr@50"] == pytest.approx(1 / 4)
    assert m["success@10"] == 1.0
    assert m["source_recall@10"] == pytest.approx(1 / 2)


def test_duplicates_collapsed_by_the_api_count_as_found():
    idx = TB.SectionIndex.from_outlines({})
    topic = _topic([_t(1, "VKM-SRC-005:p0001")])
    r = TB.ranking_from_hits([{"page_id": "VKM-SRC-006:p0001", "duplicates": ["VKM-SRC-005:p0001"]}], idx)
    m = TB.query_metrics(topic, r)
    assert m["page_recall@10"] == 1.0 and m["mrr@50"] == 1.0 and m["source_recall@10"] == 1.0


def test_empty_ranking_scores_zero():
    idx = TB.SectionIndex.from_outlines(OUTLINES)
    topic = _topic([_t(1, "VKM-SRC-001:p0003")])
    m = TB.query_metrics(topic, TB.ranking_from_hits([], idx, error="HTTP503:X"))
    assert all(m[k] == 0.0 for k in m)


def test_section_index_deepest_boundary_and_prefix():
    idx = TB.SectionIndex.from_outlines(OUTLINES)
    assert idx.deepest("VKM-SRC-001:p0003") == ["SEC-" + "b" * 16]
    assert sorted(idx.deepest("VKM-SRC-001:p0005")) == ["SEC-" + "b" * 16, "SEC-" + "c" * 16]
    assert idx.deepest("VKM-SRC-001:p0010") == ["SEC-" + "a" * 16]
    assert idx.deepest("VKM-SRC-001:p0011") == []
    assert idx.pages_of("SEC-" + "d" * 16) == ["VKM-SRC-023:r0001", "VKM-SRC-023:r0002", "VKM-SRC-023:r0003"]


def test_section_metrics_of_a_page_system():
    idx = TB.SectionIndex.from_outlines(OUTLINES)
    topic = _topic([_t(1, "VKM-SRC-001:p0004"), _t(2, "VKM-SRC-001:p0020")])
    r = TB.ranking_from_hits([{"page_id": "VKM-SRC-001:p0002"}, {"page_id": "VKM-SRC-001:p0030"}], idx)
    m = TB.query_metrics(topic, r)
    assert m["page_recall@50"] == 0.0
    assert m["section_hit@10"] == 1.0 and m["section_recall@10"] == 0.5
    assert m["section_pages@10"] == 5.0          # p0002..p0005 of section b + the page outside every section


# ---------------------------------------------------------------- NAV and dossier rankings
def test_nav_ranking_fuses_lists_and_reads_entry_pages_first():
    idx = TB.SectionIndex.from_outlines(OUTLINES)
    a, b, c = ("SEC-" + x * 16 for x in "abc")
    raw = {"sections": [{"section_id": a}, {"section_id": c}],
           "term_units": [{"unit_id": "U1", "section_id": c, "page_ids": ["VKM-SRC-001:p0007"]},
                          {"unit_id": "U2", "section_id": None, "page_ids": ["VKM-SRC-002:p0003"]}],
           "neighbour_units": [[{"unit_id": "U3", "section_id": b, "page_ids": ["VKM-SRC-001:p0004"]}]]}
    r = TB.ranking_from_nav(raw, idx)
    # c: 1/62 + 1/61 > a: 1/61 > U2: 1/62 > b: 0.5/61
    assert r.node_ids == [c, a, "UNIT:U2", b]
    assert r.pages[:4] == ["VKM-SRC-001:p0007", "VKM-SRC-001:p0001", "VKM-SRC-002:p0003", "VKM-SRC-001:p0004"]
    assert r.pages[4:8] == ["VKM-SRC-001:p0005", "VKM-SRC-001:p0006", "VKM-SRC-001:p0008", "VKM-SRC-001:p0002"]
    assert len(r.pages) == len(set(r.pages))
    topic = _topic([_t(1, "VKM-SRC-001:p0004")])
    m = TB.query_metrics(topic, r)
    assert m["mrr@50"] == pytest.approx(1 / 4) and m["section_hit@10"] == 1.0


def test_drill_down_ranking_interleaves_sources_after_the_head():
    idx = TB.SectionIndex.from_outlines({})
    base = [{"page_id": f"VKM-SRC-001:p{i:04d}"} for i in range(1, 13)]
    line = {"hits": base, "drill": [{"hits": [{"page_id": "VKM-SRC-001:p0020"}, {"page_id": "VKM-SRC-001:p0021"}]},
                                    {"hits": [{"page_id": "VKM-SRC-002:p0005"}, {"page_id": "VKM-SRC-001:p0003"}]}]}
    r = TB.ranking_from_drill(line, idx, head=10, budget=13)
    assert r.pages[:10] == [h["page_id"] for h in base[:10]]
    assert r.pages[10:13] == ["VKM-SRC-001:p0020", "VKM-SRC-002:p0005", "VKM-SRC-001:p0021"]
    assert r.pages[13:] == ["VKM-SRC-001:p0011", "VKM-SRC-001:p0012"]


def _load_harness():
    spec = importlib.util.spec_from_file_location("topic_harness_core", BENCH / "scripts" / "harness_core.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_dossier_ids_extraction_matches_the_harness():
    answer = {"item": {"record": {"sections": [{"section_id": "SEC-" + "c" * 16, "pages": ["VKM-SRC-001:p0006"]}],
                                  "evidence": [{"block_id": "VKM-SRC-002:p0003:b0004"}, "см. VKM-SRC-023:r0002"],
                                  "again": "VKM-SRC-001:p0006"}}}
    pages, secs = TB.extract_ids(answer)
    assert pages == ["VKM-SRC-001:p0006", "VKM-SRC-002:p0003", "VKM-SRC-023:r0002"]
    assert secs == ["SEC-" + "c" * 16]
    assert _load_harness().extract_ids(answer) == (pages, secs)
    idx = TB.SectionIndex.from_outlines(OUTLINES)
    r = TB.ranking_from_dossier({"page_ids": pages, "section_ids": secs}, idx)
    assert r.pages[:3] == pages and r.pages[3:] == ["VKM-SRC-001:p0005", "VKM-SRC-001:p0007", "VKM-SRC-001:p0008"]
    assert r.node_ids == secs


def test_harness_page_hits_keep_ids_only():
    h = _load_harness()
    payload = {"items": [{"envelope": {"page_id": "VKM-SRC-001:p0002", "source_id": "VKM-SRC-001"},
                          "record": {"highlights": ["текст"], "duplicates": ["VKM-SRC-006:p0002"]}},
                         {"envelope": {"object_id": "VKM-SRC-001:p0002:f0001"}, "record": {}}]}
    assert h.page_hits(payload) == [{"page_id": "VKM-SRC-001:p0002", "source_id": "VKM-SRC-001",
                                     "duplicates": ["VKM-SRC-006:p0002"]}]


# ---------------------------------------------------------------- aggregation and tests
def test_holm_is_monotone_and_capped():
    adj = TB.holm({"a": 0.01, "b": 0.04, "c": 0.03})
    assert adj == pytest.approx({"a": 0.03, "c": 0.06, "b": 0.06})
    assert TB.holm({"x": 0.9, "y": 0.8}) == {"y": 1.0, "x": 1.0}


def test_compare_systems_on_topic_means():
    rows = []
    for i in range(12):
        for v in TB.VARIANTS:
            rows.append({"system": "hybrid_late", "topic_id": f"T{i}", "page_recall@20": 0.6, "mrr@50": 0.5})
            rows.append({"system": "nav", "topic_id": f"T{i}", "page_recall@20": 0.2, "mrr@50": 0.5})
    res = TB.compare_systems(rows, [("hybrid_late", "nav")], n=2000)
    r20 = res["hybrid_late - nav | page_recall@20"]
    assert r20["n_topics"] == 12 and r20["delta"] == pytest.approx(0.4) and r20["p_value"] < 0.01
    assert res["hybrid_late - nav | mrr@50"]["delta"] == 0.0
    agg = TB.aggregate([{**r, "query_id": "q", "track": "PROCESS"} for r in rows], ["system"])
    assert agg["nav"]["n_topics"] == 12 and agg["nav"]["page_recall@20"] == 0.2


def test_acceptance_levels():
    res = TB.acceptance({"success@10": 0.85, "page_recall@50": 0.3}, {"success@10": 0.8, "page_recall@50": 0.4})
    assert res["levels"]["success@10"]["pass"] and not res["levels"]["page_recall@50"]["pass"] and not res["pass"]

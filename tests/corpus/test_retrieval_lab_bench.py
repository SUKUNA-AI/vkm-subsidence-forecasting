"""Benchmark files of the retrieval lab (agent J): schema, ID grammar, splits, public hygiene."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from vkm_corpus.retrieval_lab import bench as B

ROOT = Path(__file__).resolve().parents[2]
BENCH = ROOT / "benchmarks" / "retrieval_v0"
V1 = ROOT / "benchmarks" / "retrieval_v1"


def _q(qid: str, cat: str, track: str = "text", lang: str = "ru", target: str = "ru", **kw) -> B.Query:
    return B.Query(qid, track, kw.pop("text", f"запрос {qid}"), lang, target, cat, **kw)


def test_real_benchmark_is_valid():
    bench = B.load_benchmark(BENCH)
    problems = B.validate_benchmark(bench, min_text=120, min_visual=30)
    assert problems == [], "\n".join(problems[:30])


def test_real_benchmark_covers_every_category_and_slice():
    bench = B.load_benchmark(BENCH)
    text = [q for q in bench.queries if q.track == "text"]
    assert {q.category for q in text} == set(B.CATEGORIES)
    assert all(sum(1 for q in text if q.category == c) >= 5 for c in B.CATEGORIES)
    slices = {s for q in text for s in q.slices}
    assert slices == set(B.SLICES)
    assert len({q.text for q in bench.queries}) == len(bench.queries), "duplicate query texts"


def test_real_benchmark_has_no_forbidden_keys_or_text_columns():
    for line in (BENCH / "queries.jsonl").read_text(encoding="utf-8").splitlines():
        assert not (B._json_keys(json.loads(line)) & B.FORBIDDEN_KEYS)
    header = (BENCH / "qrels.tsv").read_text(encoding="utf-8").splitlines()[0].split("\t")
    assert tuple(header) == B.QRELS_COLUMNS


def test_real_splits_match_the_rule():
    bench = B.load_benchmark(BENCH)
    assert bench.splits == B.make_splits(bench.queries), "splits.json is stale: run `retrieval-lab splits --write`"


def test_splits_are_deterministic_stratified_and_local():
    qs = [_q(f"T-SALT-{i:03d}", "salt_mechanics") for i in range(1, 11)] + \
         [_q(f"T-LEX-{i:03d}", "lexical") for i in range(1, 8)]
    a, b = B.make_splits(qs), B.make_splits(list(reversed(qs)))
    assert a == b
    salt = [a[q.query_id] for q in qs if q.category == "salt_mechanics"]
    assert (salt.count("train"), salt.count("dev"), salt.count("test")) == (3, 2, 5)
    c = B.make_splits(qs + [_q("T-LEX-008", "lexical")])
    assert all(c[q.query_id] == a[q.query_id] for q in qs if q.category == "salt_mechanics")


def test_validation_catches_schema_errors(tmp_path):
    qs = [_q("T-SALT-001", "salt_mechanics"), _q("T-SALT-001", "salt_mechanics"), _q("T-LEX-002", "salt_mechanics"),
          _q("T-RUEN-001", "ru_to_en", lang="ru", target="ru"), _q("V-FIELD-001", "mine_field_map", track="visual")]
    qrels = [B.Qrel("T-SALT-001", "PAGE", "VKM-SRC-014:p0186", 3, "VERIFIED", "EVIDENCE_VNEXT"),
             B.Qrel("T-SALT-001", "PAGE", "VKM-SRC-14:p186", 2, "VERIFIED", "EVIDENCE_VNEXT"),
             B.Qrel("T-SALT-001", "PAGE", "VKM-SRC-014:p0187", 5, "GUESS", "LLM"),
             B.Qrel("T-XXX-001", "PAGE", "VKM-SRC-014:p0188", 1, "VERIFIED", "PAGE_INSPECTION")]
    hn = [B.HardNegative("T-SALT-001", "PAGE", "VKM-SRC-052:p0010", "GENERAL_TEXTBOOK_NOT_SITE", "PAGE_INSPECTION",
                         "VERIFIED")]
    problems = B.validate_benchmark(B.Benchmark(qs, qrels, hn))
    joined = "\n".join(problems)
    for needle in ("duplicate query_id", "code LEX", "ru_to_en must be", "bad level/doc_id", "grade 5",
                   "status GUESS", "basis LLM", "unknown query", "must also be in qrels"):
        assert needle in joined, needle


def test_roundtrip_files(tmp_path):
    qs = [_q("T-FORM-001", "formula_method", slices=("russian", "physics"), expected_kinds=("FORMULA",))]
    qrels = [B.Qrel("T-FORM-001", "PAGE", "VKM-SRC-014:p0186", 3, "VERIFIED", "EVIDENCE_VNEXT+PAGE_INSPECTION",
                    ("EV-VN-S014-0101",)),
             B.Qrel("T-FORM-001", "OBJECT", "VKM-SRC-014:p0186:m0123456789ab", 3, "CANDIDATE", "PAGE_INSPECTION")]
    B.write_queries(tmp_path / "queries.jsonl", qs)
    B.write_qrels(tmp_path / "qrels.tsv", qrels)
    B.write_hard_negatives(tmp_path / "hard_negatives.tsv", [])
    bench = B.load_benchmark(tmp_path)
    assert bench.queries == qs and sorted(bench.qrels, key=str) == sorted(qrels, key=str)
    assert bench.judgments() == {"T-FORM-001": {"VKM-SRC-014:p0186": 3}}
    assert bench.judgments(level="OBJECT", statuses=("VERIFIED", "CANDIDATE")) == {
        "T-FORM-001": {"VKM-SRC-014:p0186:m0123456789ab": 3}}
    assert B.validate_benchmark(bench) == []


# ------------------------------------------------------------------------------------ V1: pooled labels (§12)
def _pooled_row(**kw) -> dict[str, str]:
    row = {"query_id": "T-FORM-001", "level": "PAGE", "doc_id": "VKM-SRC-014:p0187", "grade": "2",
           "status": "CANDIDATE", "basis": "POOL_JUDGMENT", "label_source": "LLM_AGENT_V1",
           "pooled_from": "bm25@3,E@1", "rationale": "формула времени устойчивости"}
    row.update(kw)
    return row


def test_pooled_labels_validation_and_merge():
    qs = [_q("T-FORM-001", "formula_method")]
    bench = B.Benchmark(qs, [B.Qrel("T-FORM-001", "PAGE", "VKM-SRC-014:p0186", 3, "VERIFIED", "EVIDENCE_VNEXT")], [])
    good = _pooled_row()
    assert B.validate_pooled_qrels([good], bench) == []
    bad = [_pooled_row(query_id="T-XXX-001"), _pooled_row(doc_id="VKM-SRC-14:p187"), _pooled_row(grade="5"),
           _pooled_row(status="VERIFIED"), _pooled_row(label_source="GUESS"), _pooled_row(pooled_from="bm25"),
           _pooled_row(rationale="x" * 200), _pooled_row(doc_id="VKM-SRC-014:p0186"), good, good]
    joined = "\n".join(B.validate_pooled_qrels(bad, bench))
    for needle in ("unknown query", "bad level/doc_id", "grade", "status/basis", "label_source", "pooled_from",
                   "rationale", "already has a VERIFIED label", "duplicate pair"):
        assert needle in joined, needle
    merged = B.merge_judgments(bench.judgments(), {"T-FORM-001": {"VKM-SRC-014:p0186": 0, "VKM-SRC-014:p0187": 2}})
    assert merged == {"T-FORM-001": {"VKM-SRC-014:p0186": 3, "VKM-SRC-014:p0187": 2}}     # VERIFIED wins


def test_real_v1_pooled_labels_are_valid():
    path = V1 / "qrels_v1_pooled.tsv"
    if not path.is_file():
        pytest.skip("V1 pooled labels not present")
    bench = B.load_benchmark(BENCH)
    rows = B.load_pooled_qrels(path)
    assert rows, "empty pooled labels"
    problems = B.validate_pooled_qrels(rows, bench)
    assert problems == [], "\n".join(problems[:30])
    assert {r["label_source"] for r in rows} == {"LLM_AGENT_V1"}


def test_pooled_rounds_label_only_new_pages():
    qs = [_q("T-FORM-001", "formula_method")]
    bench = B.Benchmark(qs, [], [])
    v1 = [_pooled_row()]
    v2_new = [_pooled_row(doc_id="VKM-SRC-014:p0188", label_source="LLM_AGENT_V2", pooled_from="E_qwen3_0_6b@4")]
    v2_dup = [_pooled_row(label_source="LLM_AGENT_V2", pooled_from="B_bge_m3@1")]
    assert B.validate_pooled_qrels(v2_new, bench) == []                  # V2 is a known label source
    assert B.pooled_round_overlaps(v1, v2_new) == []
    assert "already labelled by LLM_AGENT_V1" in B.pooled_round_overlaps(v1, v2_dup)[0]
    merged = B.merge_judgments({}, B.pooled_judgments(v1 + v2_new))
    assert merged == {"T-FORM-001": {"VKM-SRC-014:p0187": 2, "VKM-SRC-014:p0188": 2}}


def test_real_v2_pooled_labels_are_valid_and_new():
    path2 = ROOT / "benchmarks" / "retrieval_v2" / "qrels_v2_pooled.tsv"
    if not path2.is_file():
        pytest.skip("V2 pooled labels not present")
    bench = B.load_benchmark(BENCH)
    rows = B.load_pooled_qrels(path2)
    assert rows, "empty pooled labels"
    problems = B.validate_pooled_qrels(rows, bench)
    assert problems == [], "\n".join(problems[:30])
    assert {r["label_source"] for r in rows} == {"LLM_AGENT_V2"}
    assert B.pooled_round_overlaps(B.load_pooled_qrels(V1 / "qrels_v1_pooled.tsv"), rows) == []


def test_real_v1_h_review_sample_matches_pooled_labels():
    sample, path = V1 / "H_REVIEW_SAMPLE.md", V1 / "qrels_v1_pooled.tsv"
    if not (sample.is_file() and path.is_file()):
        pytest.skip("V1 files not present")
    grade = {(r["query_id"], r["doc_id"]): r["grade"] for r in B.load_pooled_qrels(path)}
    # the sample table only (the H review appended a table of disagreements with numeric first cells too)
    rows = [line.split("|")[1:-1] for line in sample.read_text(encoding="utf-8").splitlines()
            if line.startswith("| ") and line.split("|")[1].strip().isdigit()
            and line.split("|")[2].strip()[:2] in ("T-", "V-")]
    assert len(rows) == 60
    for cells in rows:
        qid, page, g = cells[1].strip(), cells[3].strip(), cells[4].strip()
        assert grade.get((qid, page)) == g, (qid, page)


def test_real_v1_results_are_ids_and_numbers_only():
    path = V1 / "results_v1.json"
    if not path.is_file():
        pytest.skip("V1 results not present")
    res = json.loads(path.read_text(encoding="utf-8"))
    assert res["benchmark"] == "RETRIEVAL_BENCHMARK_V1" and res["snapshot"]["snapshot_id"].startswith("snap-")
    assert set(res["sets"]) == {"verified", "verified+pooled"}
    assert not (B._json_keys(res) & B.FORBIDDEN_KEYS)
    for tracks in res["sets"].values():
        for tr in tracks.values():
            for name, sysr in tr["systems"].items():
                for qid, vals in (sysr.get("per_query") or {}).items():
                    assert qid[:2] in ("T-", "V-") and len(vals) == 3
